"""Source-executed layouts, motion, calm cards and the canonical media path."""
import asyncio
import json
import random
import threading
from unittest.mock import AsyncMock, Mock
from urllib.request import urlopen

import numpy as np
import pytest

import config_loader
import now_playing_bridge as bridge
import now_playing_push as sender
from now_playing_review import MediaPreview, REFERENCE, CANDIDATE, adapted_source
import preview_now_playing_berry as old_preview
from weather.berry_lite import Program, BerryError
from weather.minify_ax import minify

BLACK = (0, 0, 0)


def prepared(title='MIDNIGHT CITY - M83', *, calm=False, progress=12, source=None):
    preview = MediaPreview(source)
    preview.receive(title, progress, reduced_motion=calm)
    preview.page.on_show()
    return preview


def title_pixels(image):
    return [image.getpixel((x, y)) for y in range(1, 6) for x in range(9, 32)]


def test_source_adapter_reproduces_existing_2_1_font_record_and_progress_pixels():
    preview = prepared(source=REFERENCE.read_text(encoding='utf-8'))
    for phase in range(12):
        preview.now = 1000 + phase * 150
        preview.page.cap_start = preview.now - 500
        completed = preview.page.scroll_column * 3 + preview.page.scroll_frames
        preview.page.draw()
        expected = old_preview.render_matrix('MIDNIGHT CITY - M83', preview.now - preview.page.scroll_start,
            progress=12, cap_elapsed_ms=500, completed_scroll_frames=completed)
        icon = sender.render_record_icon_frame((preview.now // 150) % 12)
        for y in range(8):
            for x in range(8):
                offset = (y * 8 + x) * 3
                expected.putpixel((x, y), tuple(icon[offset:offset + 3]))
        actual = [((c >> 16) & 255, (c >> 8) & 255, c & 255) for c in preview.screen.px]
        assert actual == list(expected.getdata())


@pytest.mark.parametrize('progress', [-1, 0, 1, 6, 12, 18, 23, 24])
def test_progress_uses_23_cells_with_a_black_gutter_and_truthful_rounding(progress):
    preview = prepared('WAIT', progress=progress)
    for phase in (0, 250, 500):
        image = preview.draw_at(1000 + phase)
        assert all(image.getpixel((8, y)) == BLACK for y in range(8))
        rail = [image.getpixel((x, 7)) for x in range(9, 32)]
        if progress < 0:
            assert rail == [BLACK] * 23
        else:
            fill = (progress * 23 + 12) // 24
            assert len(preview.page.rail) == fill
            assert sum(px != (53, 19, 31) for px in rail) == fill
            assert abs(fill - progress * 23 / 24) <= .5


def test_normal_title_keeps_existing_frame_locked_scrolling_and_edge_colors():
    reference = prepared(source=REFERENCE.read_text(encoding='utf-8'))
    candidate = prepared()
    for frame in range(240):
        stamp = 1000 + frame * 25
        assert title_pixels(reference.draw_at(stamp)) == title_pixels(candidate.draw_at(stamp))
        assert reference.page.scroll_column == candidate.page.scroll_column
        assert reference.page.scroll_frames == candidate.page.scroll_frames


@pytest.mark.parametrize('step', [25, 150, 333, 800])
def test_record_advances_by_at_most_one_pose_per_completed_visible_draw(step):
    preview = prepared('WAIT')
    previous = 0
    for stamp in range(1000, 6500, step):
        preview.draw_at(stamp)
        delta = (preview.page.record_phase - previous) % 12
        assert delta in (0, 1)
        previous = preview.page.record_phase
    preview.page.on_hide()
    pose, column = preview.page.record_phase, preview.page.scroll_column
    preview.draw_at(20000)
    assert (preview.page.record_phase, preview.page.scroll_column) == (pose, column)
    preview.page.on_show()
    preview.draw_at(20000)
    assert preview.page.record_phase == pose


@pytest.mark.parametrize('title', ['MIDNIGHT CITY - M83', 'LONG WORDS AND SHORT ONES',
    'M' * 120, 'I' * 120, 'WWW III MMMM', "CAN'T STOP (LIVE) - ARTIST", 'A A A A A A A A A A A A A A'])
def test_calm_cards_keep_every_nonspace_glyph_whole_and_in_order(title):
    preview = prepared(title, calm=True)
    columns = old_preview.text_columns(title)
    assert preview.page.columns == list(columns)
    starts = []
    column = 0
    for char in title:
        width = len(old_preview.glyph_columns(char))
        if char != ' ':
            starts.append((column, column + width))
        column += width + 1
    cards = [(card // 32, card % 32) for card in preview.page.cards]
    assert cards and all(0 < width <= 23 for start, width in cards)
    for first, end in starts:
        assert sum(start <= first and end <= start + width for start, width in cards) == 1
    assert all(a + width <= b for (a, width), (b, _) in zip(cards, cards[1:]))
    for index, (start, width) in enumerate(cards):
        preview.page.card_index = index
        image = preview.draw_at(1000)
        origin = 9 + (23 - width) // 2
        for x in range(9, 32):
            for row in range(5):
                bits = columns[start + x - origin] if origin <= x < origin + width else 0
                assert image.getpixel((x, row + 1)) == ((255, 244, 234) if bits & (1 << (4 - row)) else BLACK)
        assert (image.getpixel((31, 0)) != BLACK) == (index + 1 < len(cards))


def test_generated_metadata_never_loses_a_glyph_or_exceeds_card_width():
    rng = random.Random(27)
    alphabet = 'IMW ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789,.-!?'
    for _ in range(80):
        title = ''.join(rng.choice(alphabet) for _ in range(rng.randint(7, 120))).strip()
        test_calm_cards_keep_every_nonspace_glyph_whole_and_in_order(title)


def test_calm_cards_and_record_hold_still_then_advance_once_without_catchup():
    preview = prepared(calm=True)
    initial = preview.draw_at(1000)
    assert list(initial.getdata()) == list(preview.draw_at(3399).getdata())
    preview.draw_at(3400)
    assert preview.page.card_index == 1
    second = preview.draw_at(3425)
    assert title_pixels(initial) != title_pixels(second)
    assert initial.crop((0, 0, 8, 8)).tobytes() == second.crop((0, 0, 8, 8)).tobytes()
    preview.draw_at(50000)
    assert preview.page.card_index == 2
    preview.draw_at(52400)
    assert preview.page.card_index == 3
    preview.draw_at(54800)
    assert preview.rotation.advances == 1
    preview.draw_at(60000)
    assert preview.rotation.advances == 1


def test_hidden_calm_frames_do_not_consume_cards_and_next_visit_starts_at_first():
    preview = prepared(calm=True)
    preview.draw_at(3400)
    preview.page.on_hide()
    preview.draw_at(20000)
    assert preview.page.card_index == 1
    preview.page.on_show()
    assert preview.page.card_index == 0
    assert title_pixels(preview.draw_at(20000)) == title_pixels(preview.draw_at(22399))


def test_entire_calm_visit_gives_each_card_2400ms_without_background_scroll_completion():
    preview = prepared(calm=True)
    seen = set()
    for elapsed in range(0, 9601, 25):
        seen.add(preview.page.card_index)
        preview.draw_at(1000 + elapsed)
        assert preview.page.scroll_column == preview.page.scroll_frames == 0
        assert preview.rotation.advances == (1 if elapsed == 9600 else 0)
    assert seen == {0, 1, 2, 3}


def test_progress_poll_does_not_restart_title_but_title_or_motion_change_does():
    preview = prepared(calm=True)
    preview.draw_at(3400)
    preview.receive(progress=18, reduced_motion=True)
    assert preview.page.card_index == 1
    preview.receive('DIFFERENT TITLE', reduced_motion=True)
    assert preview.page.card_index == 0
    preview.receive('DIFFERENT TITLE')
    assert not preview.page.reduced_motion and preview.page.cards == []
    assert preview.page.scroll_column == 0 and preview.page.scroll_start == 3400


def test_new_identity_restarts_same_title_and_user_navigation_is_one_shot():
    preview = prepared(calm=True)
    preview.draw_at(3400)
    preview.receive(identity='next', reduced_motion=True)
    assert preview.page.card_index == 0 and preview.page.last_shown == 0
    assert preview.rotation.shows == 2
    preview.page.on_button('right')
    preview.draw_at(60000)
    assert preview.rotation.advances == 0


@pytest.mark.parametrize('cause', ['inactive', 'stale'])
def test_idle_and_stale_frames_show_idle_instead_of_last_track_and_leave_once(cause):
    preview = prepared()
    if cause == 'inactive':
        preview.receive(playing=False)
    else:
        preview.status = 500
        preview.draw_at(7000)
    assert not preview.page.should_show()
    assert preview.page.label == 'IDLE' and preview.page.progress_value == -1
    assert preview.rotation.advances == 1
    preview.draw_at(8000)
    assert preview.rotation.advances == 1


@pytest.mark.parametrize('title', ['WAIT', 'MIDNIGHT CITY - M83', 'M' * 120])
@pytest.mark.parametrize('calm', [False, True])
def test_minified_candidate_keeps_source_pixels_cards_and_rotation(title, calm):
    raw = prepared(title, calm=calm)
    small = prepared(title, calm=calm, source=minify(CANDIDATE.read_text(encoding='utf-8')))
    for stamp in range(1000, 10000, 150):
        assert raw.draw_at(stamp).tobytes() == small.draw_at(stamp).tobytes()
        assert raw.rotation.advances == small.rotation.advances


def test_overlong_state_is_rejected_without_rebuilding_a_large_column_cache():
    preview = prepared('WAIT')
    preview.receive('M' * 121)
    assert preview.page.label == 'WAIT' and preview.page.text_w == 13


@pytest.mark.parametrize('body', ['{', '[]', '{"p":1}', '{"p":true,"i":"x","t":3,"g":1}'])
def test_malformed_state_does_not_replace_valid_media_or_crash_render(body):
    preview = prepared('WAIT')
    preview.page.on_state(body, 200)
    assert preview.page.label == 'WAIT' and preview.page.playing


def test_real_progress_input_uses_modeled_float32_and_wire_clamping():
    preview = prepared(progress=12.9)
    assert preview.page.progress_value == 12
    preview.receive(progress=300)
    assert preview.page.progress_value == 24 and len(preview.page.rail) == 23


def test_default_bridge_contract_is_unchanged_and_calm_flag_survives_clear_and_stale():
    state = bridge.BridgeState(True, 'sample', 'WAIT', 12, 0)
    assert bridge.BridgeStateStore(state).payload() == state.to_dict()
    now = [1000.]
    store = bridge.BridgeStateStore(state, clock=lambda: now[0], reduced_motion=True)
    assert store.payload() == state.to_dict() | {'m': True}
    now[0] += 31
    assert store.payload()['p'] is False and store.payload()['m'] is True
    store.update(state)
    store.clear()
    assert store.payload()['p'] is False and store.payload()['m'] is True


@pytest.mark.parametrize('value', [1, 'true', None, []])
def test_reduced_motion_is_a_strict_boolean_in_config_and_store(value, tmp_path):
    with pytest.raises(ValueError):
        bridge.BridgeStateStore(reduced_motion=value)
    document = config_loader.example_document()
    document['services']['media']['options']['reduced_motion'] = value
    with pytest.raises(config_loader.ConfigError):
        config_loader.config_from_mapping(document, source_path=tmp_path / 'config.json')


def test_http_state_serves_optional_flag_to_allowed_client_without_panel_writes():
    store = bridge.BridgeStateStore(bridge.BridgeState(True, 'sample', 'WAIT', 12), reduced_motion=True)
    server = bridge.make_server('127.0.0.1', 0, store, '127.0.0.1')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(f'http://127.0.0.1:{server.server_port}/nowplaying', timeout=5) as response:
            assert json.load(response) == store.payload()
            assert response.headers['Cache-Control'] == 'no-store'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
    assert not thread.is_alive()


def test_worker_passes_calm_option_and_preserves_verification_only_install_policy(tmp_path, monkeypatch):
    document = config_loader.example_document()
    document['runtime']['data_dir'] = str(tmp_path / 'state')
    document['services']['media'] = {'enabled': True, 'options': {
        'reduced_motion': True, 'script_path': str(CANDIDATE), 'allow_script_install': False}}
    config = config_loader.config_from_mapping(document, source_path=tmp_path / 'config.json')
    monkeypatch.setattr(sender, 'MediaManager', object())
    run = AsyncMock()
    monkeypatch.setattr(sender, 'run_script_bridge', run)
    assert sender.run_worker(config, threading.Event()) == 0
    assert run.call_args.kwargs['reduced_motion'] is True
    assert run.call_args.kwargs['allow_script_install'] is False


def test_unsupported_template_fails_before_dns_server_or_panel_mutation(monkeypatch):
    monkeypatch.setattr(sender, 'configure_logging', Mock())
    dns, server, put = Mock(), Mock(), Mock()
    monkeypatch.setattr(sender.socket, 'gethostbyname', dns)
    monkeypatch.setattr(sender, 'make_server', server)
    monkeypatch.setattr(sender.requests, 'put', put)
    with pytest.raises(RuntimeError, match='compatible resident script'):
        asyncio.run(sender.run_script_bridge(reduced_motion=True, script_path=REFERENCE))
    dns.assert_not_called()
    server.assert_not_called()
    put.assert_not_called()


def test_supplied_store_owns_mode_and_conflicting_explicit_flag_fails(monkeypatch):
    store = bridge.BridgeStateStore()
    with pytest.raises(ValueError, match='supplied state store'):
        asyncio.run(sender.run_script_bridge(store=store, reduced_motion=True))


def test_all_wire_buckets_remain_monotonic_and_do_not_flash_extra_progress():
    last_fill = 0
    for bucket in range(25):
        preview = prepared('WAIT', progress=bucket)
        fill = len(preview.page.rail)
        assert fill >= last_fill
        last_fill = fill
        for stamp in (1000, 1250, 1500):
            image = preview.draw_at(stamp)
            assert sum(image.getpixel((x, 7)) != (53, 19, 31) for x in range(9, 32)) == fill


def test_hot_draw_path_uses_existing_caches_without_network_json_or_glyph_allocations():
    source = CANDIDATE.read_text(encoding='utf-8')
    draw = source[source.index('  def draw()'):]
    assert all(token not in draw for token in ('http.', 'json.', 'string.', '.push(', '[]', 'rgb('))
    assert 'self.columns[' in draw and 'self.cards[' in draw


def test_reference_source_is_unchanged_and_offline_adapters_refuse_unknown_boundaries():
    from hashlib import sha256
    assert sha256(REFERENCE.read_text(encoding='utf-8').encode()).hexdigest() == 'be1829ea3043841402133d867f0efa58f24afe6f6bcd642184129619907ce6b2'
    assert '# @version 2.2' in CANDIDATE.read_text(encoding='utf-8')


@pytest.mark.parametrize('source,expected', [
    ('var out=[] for i:0..3 out.push(i) end return out', [0, 1, 2, 3]),
    ('var out=[] for i:2..2 out.push(i) end return out', [2]),
    ('var out=[] for i:0..-1 out.push(i) end return out', []),
    ('var out=[] for i:-2..1 out.push(i) end return out', [-2, -1, 0, 1])])
def test_compiler_range_literals_match_documented_inclusive_default_step(source, expected):
    assert Program(source, real_type=np.float32).run({}) == expected


def test_compiler_rejects_noninteger_range_bounds_and_unknown_native_adapter_shapes():
    with pytest.raises(BerryError):
        Program('for i:0..2.5 end', real_type=np.float32).run({})
    with pytest.raises(ValueError):
        adapted_source(CANDIDATE.read_text().replace('import json', 'import unsupported'))
