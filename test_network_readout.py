"""Throughput units, real payloads, refresh policy and supervised settings."""
from io import BytesIO
import random
import threading
from unittest.mock import Mock

from PIL import Image
import pytest

import config_loader
import firmware_compat
from information_preview import decode
import network_readout as net
import network_preview
import pc_stats_push as pc
import visual_system as visual


@pytest.mark.parametrize('rate,text,unit', [
    (0, '0.0', 'MiB/s'), (8.4, '8.4', 'MiB/s'), (25, '25.0', 'MiB/s'),
    (150, '150.0', 'MiB/s'), (250, '250.0', 'MiB/s'), (1023.9, '1023.9', 'MiB/s'),
    (1024, '1.00', 'GiB/s'), (2048, '2.00', 'GiB/s'), (10000, '9.77', 'GiB/s'),
    (-1, '0.0', 'MiB/s'), (None, '--', 'MiB/s'), (float('nan'), '--', 'MiB/s'),
    (float('inf'), '--', 'MiB/s'), (True, '--', 'MiB/s'), (1e300, '999+', 'GiB/s'),
])
def test_binary_units_are_explicit_and_supported_labels_fit(rate, text, unit):
    reading = net.reading(rate)
    assert (reading.text, reading.unit) == (text, unit)
    assert visual.measure_text_3x3(text) <= 23
    assert visual.measure_text_3x3(unit) <= 23
    frame = net.render_lane(reading)
    assert all(frame[y][8] == visual.BLACK for y in range(8))
    assert all(frame[3][x] == visual.BLACK for x in range(9, 32))


def test_receive_sampler_units_remain_binary_without_changing_alert_thresholds():
    assert pc.sample_network_mb_s(0, 0, 10 * 1024 ** 2, 1) == 10
    assert (pc.NET_SHOW_AT, pc.NET_CLEAR_BELOW) == (10, 8)
    assert pc.sample_network_mb_s(100, 1, 50, 2) == 0
    assert pc.sample_network_mb_s(0, 1, 100, 1) == 0


def test_150_to_250_is_visible_and_refreshes_after_confirmation_and_pacing():
    before, after = pc.page_view([('NET', 150)]), pc.page_view([('NET', 250)])
    assert before.percent is None and after.percent is None
    assert before.network.text == '150.0' and after.network.text == '250.0'
    assert pc.metric_payload('NET', 150) != pc.metric_payload('NET', 250)
    assert pc.shows_change(after, before)
    assert not pc.refresh_due(after, before, pc.PUSH_INTERVAL - .1, change_streak=10)
    assert not pc.refresh_due(after, before, pc.PUSH_INTERVAL, change_streak=0)
    assert pc.refresh_due(after, before, pc.PUSH_INTERVAL, change_streak=pc.REFRESH_CONFIRM_SAMPLES)


def test_network_refresh_delta_is_separate_from_percentage_points():
    settings = pc.PCSettings('http://example.invalid', refresh_delta_points=90,
        network_refresh_delta_mib_s=5)
    before = pc.page_view([('NET', 150)], settings=settings)
    assert not pc.shows_change(pc.page_view([('NET', 151)], settings=settings), before, settings=settings)
    assert pc.shows_change(pc.page_view([('NET', 155)], settings=settings), before, settings=settings)
    assert pc.shows_change(pc.page_view([('NET', 153)]), before)  # default 3 MiB/s


def test_visible_quantization_and_scale_are_part_of_page_identity():
    before = pc.page_view([('NET', 2000)])
    after = pc.page_view([('NET', 2000.01)])
    assert before == after
    assert pc.build_payload([('NET', 2000)]) == pc.build_payload([('NET', 2000.01)])
    scaled = pc.PCSettings('http://example.invalid', network_full_scale_mib_s=500)
    assert pc.page_view([('NET', 250)]).network.rail_fill == 23
    changed = pc.page_view([('NET', 250)], settings=scaled)
    assert changed.network.rail_fill == 12
    assert pc.shows_change(changed, pc.page_view([('NET', 250)]), settings=scaled)
    assert pc.build_payload([('NET', 250)], settings=scaled) != pc.build_payload([('NET', 250)])


def test_real_bitmap_contains_rate_and_unit_without_a_percent_sign():
    payload = pc.metric_payload('NET', 150)
    decoded = decode(payload)['frames'][0]
    expected = visual.render_split_lane('150.0', 'MiB/s', bottom_color=visual.MIST)
    for y in range(7):
        assert decoded[y * 32 + 9:(y + 1) * 32] == ['%02x%02x%02x' % p for p in expected[y][9:]]
    for animated in (True, False):
        assert pc.metric_payload('NET', 150, animated=animated)['draw'] == payload['draw']


@pytest.mark.parametrize('metrics,face', [
    ([('CPU', 88), ('NET', 250)], 'CPU'), ([('GPU', 82), ('NET', 95)], 'GPU'),
    ([('CPU', 88), ('RAM', 90), ('NET', 10000)], 'BOTH'),
    ([('CPU', 99), ('GPU', 92), ('NET', 10000)], 'GPU'),
    ([('NET', 250)], 'NET'),
])
def test_hardware_priority_does_not_compare_throughput_to_percentages(metrics, face):
    assert pc.page_view(metrics).face == face
    if face != 'NET':
        hardware = [(label, rate) for label, rate in metrics if label != 'NET']
        assert pc.build_payload(metrics) == pc.build_payload(hardware)


def test_new_network_animation_is_exact_small_and_continuous_with_useful_static_variant():
    data = pc.build_icon_gif('NET', 250)
    assert len(data) <= 1024
    with Image.open(BytesIO(data)) as image:
        assert image.size == (8, 8) and image.n_frames == 6
        assert image.info['loop'] == 0 and 'transparency' not in image.info
        frames = []
        for phase in range(6):
            image.seek(phase)
            assert image.info['duration'] == 240
            rgb = image.convert('RGB')
            frames.append(rgb.tobytes())
            assert rgb.tobytes() == pc._icon_frame('NET', 0, phase).tobytes()
            pixels = list(rgb.getdata())
            lit = [pixel for pixel in pixels if pixel != (0, 0, 0)]
            assert sum(max(pixel) >= 174 for pixel in lit) / len(lit) >= .6
        assert len(set(frames)) == 6
    with Image.open(BytesIO(pc.build_icon_gif('NET', 250, animated=False))) as image:
        assert image.n_frames == 1


def test_previews_match_http_body_sizes_and_budgets_without_sending(monkeypatch):
    import requests
    monkeypatch.setattr(requests.Session, 'request', lambda *a, **k: pytest.fail('network access'))
    entries = network_preview.capture()
    assert len(entries) == 14
    firmware_compat.observe_device('offline://net-tests', {'version': '1.2.2'})
    for entry, (_, _, metrics) in zip(entries, network_preview.cases()):
        for animated, preview in ((True, entry), (False, entry['static'])):
            payload = firmware_compat.prepare_payload('offline://net-tests', pc.build_payload(metrics, animated))
            body = requests.Request('PUT', 'http://example.invalid', json=payload).prepare().body
            assert len(body) == preview['bytes'] < 2048
            assert all(frame[8::32] == ['000000'] * 8 for frame in preview['frames'])


@pytest.mark.parametrize('field', ['network_full_scale_mib_s', 'network_refresh_delta_mib_s'])
@pytest.mark.parametrize('value', [0, -1, 10001, True, None, float('nan')])
def test_new_settings_are_strict_in_canonical_loader_and_direct_adapter(field, value, tmp_path):
    document = config_loader.example_document()
    document['services']['pc_stats']['options'][field] = value
    with pytest.raises(config_loader.ConfigError):
        config_loader.config_from_mapping(document, source_path=tmp_path / 'config.json')
    with pytest.raises(ValueError):
        pc.PCSettings('http://example.invalid', **{field: value})


def test_canonical_worker_forwards_network_options_and_scoped_push_uses_them(tmp_path, monkeypatch):
    document = config_loader.example_document()
    document['runtime']['data_dir'] = str(tmp_path / 'state')
    document['panel']['url'] = 'http://192.0.2.45'
    document['services']['pc_stats'] = {'enabled': True, 'options': {
        'network_full_scale_mib_s': 500, 'network_refresh_delta_mib_s': 7}}
    config = config_loader.config_from_mapping(document, source_path=tmp_path / 'config.json')
    run = Mock()
    monkeypatch.setattr(pc, 'run_pc_stats', run)
    event = threading.Event()
    assert pc.run_worker(config, event) == 0
    settings = run.call_args.args[1]
    assert settings.network_full_scale_mib_s == 500 and settings.network_refresh_delta_mib_s == 7
    put = Mock(return_value=Mock(status_code=200))
    monkeypatch.setattr(pc.requests, 'put', put)
    monkeypatch.setattr(pc.requests, 'get', lambda *a, **k: pytest.fail('unexpected GET'))
    firmware_compat.observe_device(config.panel.url, {'version': '1.2.2'})
    labels, error = pc.sync_panel([('NET', 250)], ('NET',), settings=settings)
    assert error is None and labels == ('NET',)
    body = put.call_args.kwargs['json']
    decoded = decode(body)['frames'][0]
    rail = [tuple(bytes.fromhex(c)) for c in decoded[7 * 32:]]
    assert rail[9:32].index(visual.PEARL) + 1 == 12
    assert put.call_args.args[0].startswith(config.panel.url)


def test_whole_sender_loop_updates_large_receive_rates_without_lifetime_gaps():
    from test_pc_stats_push import run_loop
    def counter(t):
        return (150 * min(t, 60) + 250 * max(0, t - 60)) * 1024 ** 2
    run = run_loop(lambda t: (20, 50), 160, net=counter)
    pushes = run.panel.pushes()
    before = pc.metric_payload('NET', 150)['draw']
    after = pc.metric_payload('NET', 250)['draw']
    assert any(body['draw'] == before for _, body in pushes)
    assert any(60 <= t <= 80 and body['draw'] == after for t, body in pushes)
    assert not run.panel.lapses


def test_equal_views_still_imply_identical_network_payloads_across_scales():
    rng = random.Random(27)
    for scale in (10, 100, 500, 10000):
        settings = pc.PCSettings('http://example.invalid', network_full_scale_mib_s=scale)
        seen = {}
        for rate in [0, None, 1023.99, 1024, 10000] + [rng.uniform(0, 10000) for _ in range(100)]:
            metrics = [('NET', rate)]
            view = pc.page_view(metrics, settings=settings)
            payload = pc.build_payload(metrics, settings=settings)
            if view in seen:
                assert seen[view] == payload
            seen[view] = payload
