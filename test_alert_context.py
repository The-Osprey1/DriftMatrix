"""Readable alert context, configured origins and bounded offline lifecycle checks."""
from copy import deepcopy
import json
import threading
from unittest.mock import Mock

import pytest

import alert_context_preview as preview
import config_loader
import config_migration
import firmware_compat
import game_mode as game
from information_preview import decode
import panel_scene
import panel_gate
import quake_watch as quake
from test_new_app_support import FakeClock, SpyPanel
from test_panel_client import make_client
import utility_hub as hub
import visual_system as visual

NOW = 1_790_000_000


def event(**kwargs):
    values = dict(event_id='sample', magnitude=3.2, place='Sample',
                  time_ms=(NOW - 60) * 1000, lat=0, lon=.2, depth_km=8)
    return quake.Quake(**(values | kwargs))


def assert_text(frame, top, bottom):
    expected = visual.render_split_lane(top, bottom, bottom_color=visual.MIST)
    assert [row[9:] for row in frame[:7]] == [row[9:] for row in expected[:7]]
    assert all(row[8] == visual.BLACK for row in frame)
    assert frame[3][9:] == [visual.BLACK] * 23


def test_two_held_readings_have_explicit_units_and_unobstructed_glyphs():
    assert_text(game.render_lane(game.GpuReading(78, 80), 35), '78C', '35M')
    assert_text(game.render_lane(game.GpuReading(65, 80), 125), '65C', '2H05')
    assert_text(game.render_lane(None, None), 'GPU?', 'AGE?')
    assert_text(quake.render_lane(event(), home=(0, 0)), 'M3.2', '14 MI')
    assert game.render_lane(game.GpuReading(78, 80), 1)[7] == game.render_lane(game.GpuReading(78, 80), 999)[7]


@pytest.mark.parametrize('minutes,label', [(0, '0M'), (.9, '0M'), (35, '35M'),
    (59.99, '59M'), (60, '1H00'), (60.9, '1H00'), (125, '2H05'),
    (5999, '99H59'), (6000, '99H+'), (None, 'AGE?'), (-1, 'AGE?'),
    (True, 'AGE?'), (float('nan'), 'AGE?'), (float('inf'), 'AGE?')])
def test_elapsed_labels_floor_age_without_claiming_play_time(minutes, label):
    assert game.elapsed_label(minutes) == label
    assert visual.measure_text_3x3(label) <= 23


def test_custom_origin_controls_distance_and_proximity_together():
    q = event()
    assert quake.distance_label(q, (0, 0)) == '14 MI'
    assert quake.closeness(q, 150, (0, 0)) > .8
    assert quake.closeness(q, 150) == 0
    assert '14 MI E' in quake.describe(q, NOW * 1000, (0, 0))
    antipode = event(lat=0, lon=180)
    assert quake.distance_label(antipode, (0, 0)) == '12KMI'
    assert quake.haversine_km(0, 0, 0, 180) == pytest.approx(20015.087, abs=.001)
    assert quake.magnitude_text(10.2) == 'M10.2'
    assert_text(quake.render_lane(event(magnitude=10.2), home=(0, 0)), 'M10.2', '14 MI')


@pytest.mark.parametrize('home', [(91, 0), (0, -181), (True, 0), (0, None),
    (float('nan'), 0), (0, float('inf')), ('0', 0), (), (0, 0, 0)])
def test_invalid_origins_are_rejected(home):
    with pytest.raises(ValueError):
        quake.validate_home(home)


def test_bad_individual_feed_records_do_not_drop_valid_neighbors():
    good = {'id': 'ok', 'properties': {'mag': 3.2, 'time': NOW * 1000},
            'geometry': {'coordinates': [.2, 0, 8]}}
    broken = [None, [], {'properties': []}, {'geometry': 'bad'}]
    for field, value in [('mag', None), ('mag', True), ('mag', float('nan')),
                          ('time', float('inf')), ('time', -1)]:
        item = deepcopy(good)
        item['properties'][field] = value
        broken.append(item)
    for coordinates in [None, [0], [181, 0], [0, 91], [True, 0], [0, 0, float('nan')]]:
        item = deepcopy(good)
        item['geometry']['coordinates'] = coordinates
        broken.append(item)
    assert [q.event_id for q in quake.parse_quakes({'features': broken + [good]})] == ['ok']
    assert quake.parse_quakes({'features': []}) == []


@pytest.mark.parametrize('document', [None, [], {}, {'features': None}, {'features': {}}])
def test_malformed_feed_root_is_a_failure_not_an_empty_success(document):
    with pytest.raises(ValueError):
        quake.parse_quakes(document)


def test_canonical_worker_carries_origin_into_hub_and_controller(tmp_path, monkeypatch):
    document = config_loader.example_document()
    document['runtime']['data_dir'] = str(tmp_path / 'state')
    document['panel']['url'] = 'http://192.0.2.41'
    document['services']['hub']['enabled'] = True
    document['services']['hub']['options']['quakes'] = {
        'enabled': True, 'latitude': 0, 'longitude': 0, 'radius_km': 75}
    config = config_loader.config_from_mapping(document, source_path=tmp_path / 'config.json')
    run = Mock()
    monkeypatch.setattr(hub, 'run_hub', run)
    assert hub.run_worker(config, threading.Event()) == 0
    settings = run.call_args.args[0].quakes
    assert (settings.latitude, settings.longitude, settings.radius_km) == (0, 0, 75)


def test_hub_constructs_quake_worker_with_validated_coordinates(monkeypatch):
    config = hub.config_from_mapping({'quakes': {
        'enabled': True, 'latitude': 0, 'longitude': 12, 'radius_km': 75}})
    monkeypatch.setattr(hub, 'configure_logging', Mock())
    monkeypatch.setattr(hub, 'build_panel_client', Mock(return_value=Mock()))
    monkeypatch.setattr(hub, 'build_controller', Mock())
    monkeypatch.setattr(hub, 'build_focus_controller', Mock())
    monkeypatch.setattr(hub, 'build_meeting_controller', Mock())
    monkeypatch.setattr(hub, 'UtilityHubRunner', Mock())
    worker = Mock()
    monkeypatch.setattr(quake, 'QuakeWatchController', worker)
    hub.run_hub(config)
    settings = worker.call_args.args[1]
    assert (settings.lat, settings.lon, settings.radius_km) == (0, 12, 75)


def test_offline_migration_preserves_origin_without_enabling_or_writing_services(tmp_path):
    path, output = tmp_path / 'hub.json', tmp_path / 'candidate.json'
    path.write_text(json.dumps({'panel_url': 'http://192.0.2.41',
        'quakes': {'enabled': True, 'latitude': 0, 'longitude': 12}}), encoding='utf-8-sig')
    original = path.read_bytes()
    result = config_migration.migrate(hub_path=path, output_path=output)
    assert result.report['status'] == 'ready'
    options = result.candidate['services']['hub']['options']['quakes']
    assert options == {'enabled': True, 'latitude': 0, 'longitude': 12}
    assert not result.candidate['services']['hub']['enabled']
    assert path.read_bytes() == original and not output.exists()
    assert any(p['field'] == 'services.hub.options.quakes.latitude'
               and p['sources'] == [{'source': 'hub', 'field': 'quakes.latitude'}]
               for p in result.report['provenance'])


@pytest.mark.parametrize('options', [{'latitude': 0}, {'longitude': 0},
    {'latitude': 91, 'longitude': 0}, {'latitude': 0, 'longitude': -181},
    {'latitude': True, 'longitude': 0}, {'latitude': 0, 'longitude': float('nan')}])
def test_canonical_and_legacy_hub_reject_bad_or_unpaired_origin(options, tmp_path):
    with pytest.raises(ValueError):
        hub.config_from_mapping({'quakes': options})
    document = config_loader.example_document()
    document['services']['hub']['options']['quakes'] = options
    with pytest.raises(config_loader.ConfigError):
        config_loader.config_from_mapping(document, source_path=tmp_path / 'config.json')


def test_feed_query_description_and_both_payloads_use_same_origin(monkeypatch):
    response = Mock()
    response.json.return_value = {'features': []}
    request = Mock(return_value=response)
    monkeypatch.setattr(quake, 'https_get', request)
    cfg = quake.QuakeWatchConfig(lat=0, lon=0, radius_km=75)
    assert quake.fetch_feed(cfg) == []
    params = request.call_args.kwargs['params']
    assert (params['latitude'], params['longitude'], params['maxradiuskm']) == (0, 0, 75)
    panel, clock = SpyPanel(), FakeClock(NOW)
    fetch = Mock(return_value=[event()])
    controller = quake.QuakeWatchController(panel, cfg, fetcher=fetch, clock=clock)
    assert controller.tick() == event()
    assert '14 MI E' in controller.scene.key[1]
    assert panel.pushes[0][1] == quake.build_payload(event(), 75, home=(0, 0))
    assert panel.safety[0][1] == quake.build_payload(event(), 75, home=(0, 0), animated=False)
    assert controller.tick() is None and fetch.call_count == 1
    clock.advance(60)
    fetch.return_value = []
    controller.tick()
    assert panel.deletes == ['quake0']


class DualClock:
    def __init__(self):
        self.mono, self.wall = 100., float(NOW)
    def monotonic(self):
        return self.mono
    def time(self):
        return self.wall


def process(start=NOW - 600, pid=42):
    return game.ProcessInfo(pid, r'D:\steamapps\common\Sample\sample.exe', start, 2 * 1024**3)


def test_age_advances_monotonically_and_process_identity_resets_it():
    panel, clock = SpyPanel(), DualClock()
    running = [process()]
    controller = game.GameModeController(panel, processes=lambda: running,
        gpu=lambda: game.GpuReading(65, 80), clock=clock)
    controller.tick()
    assert panel.pushes[-1][1] == game.build_payload(game.GpuReading(65, 80), 10)
    clock.wall -= 3600
    clock.mono += 120
    controller.tick()
    assert panel.pushes[-1][1] == game.build_payload(game.GpuReading(65, 80), 12)
    clock.wall += 7200
    clock.mono += 60
    controller.tick()
    assert panel.pushes[-1][1] == game.build_payload(game.GpuReading(65, 80), 13)
    running[:] = [process(clock.wall - 120)]  # same PID, different creation timestamp
    clock.mono += 5
    controller.tick()
    assert panel.pushes[-1][1] == game.build_payload(game.GpuReading(65, 80), 2)
    running.clear()
    clock.mono += 5
    controller.tick()
    assert panel.deletes == ['game0']


@pytest.mark.parametrize('start', [0, None, True, float('nan'), NOW + 60])
def test_unknown_or_future_process_age_does_not_look_like_a_long_session(start):
    panel, clock = SpyPanel(), DualClock()
    controller = game.GameModeController(panel, processes=lambda: [process(start)],
        gpu=lambda: None, clock=clock)
    controller.tick()
    assert panel.pushes[0][1] == game.build_payload(None, None)
    assert panel.safety[0][1] == game.build_payload(None, None, animated=False)


def test_gpu_failure_retains_process_age_and_pacing():
    panel, clock = SpyPanel(), DualClock()
    gpu = Mock(side_effect=RuntimeError('unavailable'))
    scan = Mock(return_value=[process()])
    controller = game.GameModeController(panel, processes=scan, gpu=gpu, clock=clock)
    controller.tick()
    assert panel.pushes[0][1] == game.build_payload(None, 10)
    clock.mono += 4
    controller.tick()
    assert scan.call_count == gpu.call_count == 1
    clock.mono += 1
    controller.tick()
    assert scan.call_count == 2 and len(panel.pushes) == 1
    clock.mono += 5
    controller.tick()
    assert len(panel.pushes) == 2


@pytest.mark.parametrize('payload,safety,name', [
    (game.build_payload(game.GpuReading(65, 80), 35), game.build_payload(game.GpuReading(65, 80), 35, animated=False), 'game0'),
    (quake.build_payload(event()), quake.build_payload(event(), animated=False), 'quake0')])
def test_real_panel_client_static_latch_uses_same_readings_without_animation(payload, safety, name, panel_gate_dir):
    client, session, _clock = make_client()
    session.get.return_value = Mock(status_code=200)
    session.get.return_value.json.return_value = {'version': '1.2.2', 'uptimeSeconds': 3600}
    firmware_compat.observe_device(client.base_url, {'version': '1.2.2'})
    client._static_latched = True
    scene = panel_scene.PresenceScene(client, name, 10, FakeClock(), Mock())
    assert scene.show('sample', lambda _: payload, static_build=lambda _: safety)
    assert session.put.call_count == 1
    sent = session.put.call_args.kwargs['json']
    assert sent['draw'] == payload['draw'] == safety['draw']
    assert len(decode(sent)['frames']) == 1
    journal = [json.loads(line) for line in (panel_gate_dir / panel_gate.EVENTS_NAME).read_text().splitlines()]
    assert len(journal) == 1
    assert (journal[0]['src'], journal[0]['op'], journal[0]['app'], journal[0]['status']) == ('hub', 'push', name, 200)


def test_failed_push_does_not_acknowledge_presence_or_change_retry_identity():
    panel = Mock()
    panel.push_app.side_effect = [RuntimeError('offline'), None]
    scene = panel_scene.PresenceScene(panel, 'game0', 10, FakeClock(), Mock())
    assert not scene.show('key', lambda _: {'text': 'value'}, static_build=lambda _: {'text': 'safe'})
    assert not scene.showing and scene.key is None and scene.last_push_at is None
    assert scene.show('key', lambda _: {'text': 'value'}, static_build=lambda _: {'text': 'safe'})
    assert panel.push_app.call_count == 2 and scene.showing


def test_legacy_presence_callers_keep_the_two_argument_contract(capsys):
    class LegacyPanel:
        def push_app(self, name, payload):
            self.pushed = name, payload
    panel = LegacyPanel()
    scene = panel_scene.PresenceScene(panel, 'old', 10, FakeClock(), Mock())
    assert scene.show('key', lambda _: {'text': 'same'})
    assert panel.pushed == ('old', {'text': 'same'})
    panel_scene.PrintPanel().push_app('test', {'text': 'same'}, static_payload={'text': 'same'})
    assert 'PUSH test' in capsys.readouterr().out


def test_offline_review_decodes_real_animated_and_static_wire_bodies(monkeypatch):
    import requests
    monkeypatch.setattr(requests.Session, 'request', lambda *a, **k: pytest.fail('unexpected network access'))
    cases = preview.capture()
    assert len(cases) == 13
    for entry in cases:
        assert 0 < entry['bytes'] < 2048
        assert entry['static'] is not None and entry['static']['bytes'] < 2048
        assert len(entry['static']['frames']) == 1
        for frame in entry['frames'] + entry['static']['frames']:
            assert frame[8::32] == ['000000'] * 8
        assert all(delay % 120 == 0 for delay in entry['durations'])
    for entry, (_, _, group, value, argument) in zip(cases, preview.cases()):
        for animated, record in ((True, entry), (False, entry['static'])):
            payload = (quake.build_payload(value, home=argument, animated=animated) if group == 'Earthquakes'
                       else game.build_payload(value, argument, animated=animated))
            payload = firmware_compat.prepare_payload('offline://alert-context-review', payload)
            body = requests.Request('PUT', 'http://example.invalid', json=payload).prepare().body
            assert len(body) == record['bytes']
