"""Actual approved/compact media sources, explicit JSON/HTTP preview boundaries."""
import json
from pathlib import Path
import random
import struct

import pytest

from now_playing_bridge import install_source
from now_playing_review import MediaPreview
from weather.berry_lite import BerryError, Program
from weather.minify_ax import minify
from weather.moon_panel_sim import R

ROOT = Path(__file__).resolve().parent
REFERENCE = (ROOT / 'now_playing-v2.3.ax').read_text(encoding='utf-8')
CANDIDATE = (ROOT / 'now_playing-v2.4.ax').read_text(encoding='utf-8')


def pair(*, title='MIDNIGHT CITY - M83', calm=False, progress=12, candidate=CANDIDATE):
    old, new = MediaPreview(REFERENCE), MediaPreview(candidate)
    for runner in (old, new):
        runner.receive(title, progress, reduced_motion=calm)
        runner.page.on_show()
    return old, new


def assert_state(old, new):
    for field in old.page.__berry_members__:
        expected = getattr(old.page, field)
        if field == 'heads':
            assert expected == list(range(16))  # Replaced by the identity operation.
            continue
        actual = getattr(new.page, field)
        if field == 'glyphs':
            actual = list(struct.unpack('<'+'H'*(len(actual)//2), actual))
        elif field == 'cards':
            actual = list(struct.unpack('<'+'h'*(len(actual)//2), actual))
        elif field in ('columns', 'groove', 'label_ring'):
            actual = list(actual)
        elif field == 'rail':
            actual = [int.from_bytes(actual[i:i+3], 'big') for i in range(0, len(actual), 3)]
        assert expected == actual, field
    assert old.page.should_show() == new.page.should_show()
    assert old.page.duration() == new.page.duration() == 120000
    assert (old.requests, old.rotation.shows, old.rotation.advances) == (new.requests, new.rotation.shows, new.rotation.advances)


def draw(old, new, stamp):
    a, b = old.draw_at(stamp), new.draw_at(stamp)
    assert a.tobytes() == b.tobytes()
    assert_state(old, new)


@pytest.mark.parametrize('calm', [False, True])
@pytest.mark.parametrize('title', ['WAIT', 'I'*12, 'MIDNIGHT CITY - M83', 'M'*120,
                                  'I'*120, 'M IM M IM '*12, '?.,\':/!&()+_=#', '\u2603UNKNOWN'])
def test_complete_visits_match_for_short_long_maximum_width_and_unknown_glyphs(calm, title):
    old, new = pair(title=title, calm=calm)
    assert_state(old, new)
    limit = 2400 * max(1, len(old.page.cards)) + 25 if calm else 1200 + (old.page.text_w+8)*3*25
    for stamp in range(1000, 1000+max(7100, limit), 25):
        draw(old, new, stamp)
        if new.rotation.advances:
            break
    assert new.rotation.advances == 1
    assert new.page.text_w <= 719 and len(new.page.columns) == new.page.text_w
    assert len(new.page.cards) % 2 == 0


@pytest.mark.parametrize('calm', [False, True])
def test_every_accepted_glyph_and_random_card_layout_preserve_all_columns(calm):
    rng = random.Random(314)
    old, new = pair(calm=calm)
    alphabet = old.page.alphabet
    titles = [alphabet, 'M'*120, ' '*120]
    titles += [''.join(rng.choice(alphabet) for _ in range(rng.randint(1, 120))) for _ in range(60)]
    for title in titles:
        for runner in (old, new):
            runner.receive(title, reduced_motion=calm)
        assert_state(old, new)
        # Every held card and ordinary frame starts from the same logical state.
        for index in range(max(1, len(old.page.cards))):
            old.page.card_index = new.page.card_index = index
            draw(old, new, old.now)


@pytest.mark.parametrize('progress', range(-1, 25))
def test_every_progress_bucket_retains_exact_full_rgb_and_leader_frames(progress):
    old, new = pair(title='WAIT', progress=progress)
    for stamp in (1000, 1249, 1250, 1499, 1500):
        draw(old, new, stamp)
    assert len(new.page.rail) == len(old.page.rail)*3
    assert all(new.screen.px[y*32+8] == 0 for y in range(8))


@pytest.mark.parametrize('interval', [1, 25, 120, 333, 1000, 10000])
def test_slow_draw_hidden_intervals_button_exit_and_new_identity(interval):
    old, new = pair(calm=True)
    for stamp in (1000, 1000+interval, 11000+interval):
        draw(old, new, stamp)
    for runner in (old, new):
        runner.page.on_hide()
    draw(old, new, 600000)
    for runner in (old, new):
        runner.page.on_show()
        runner.receive(identity='next', reduced_motion=False)
        runner.page.on_button('right')
    draw(old, new, 600025)
    assert_state(old, new)


@pytest.mark.parametrize('body,status', [(None,200), ('{',200), ('[]',200), ('null',200),
    ('{}',200), ('{"p":1}',200), ('{"p":true,"i":"","t":"X","g":1}',200),
    (json.dumps(dict(p=True,i='id',t='M'*121,g=1)),200),
    (json.dumps(dict(p=True,i='i'*161,t='X',g=1)),200),
    ('{"p":true,"i":"id","t":"X","g":true}',200), ('{}',500), ('{}',404)])
def test_invalid_json_state_types_lengths_and_http_failure_preserve_state(body, status):
    old, new = pair()
    for runner in (old, new):
        runner.page.on_state(body, status)
        runner.body, runner.status = body, status
    assert_state(old, new)
    draw(old, new, 7000)
    assert not new.page.should_show()
    assert new.page.label == 'IDLE'


def test_updates_switching_motion_mode_and_idle_reuse_keep_scroll_receipts():
    old, new = pair(calm=True)
    draw(old, new, 3400)
    for state in [dict(progress=18,reduced_motion=True), dict(title='DIFFERENT TITLE',reduced_motion=True),
                  dict(title='DIFFERENT TITLE',reduced_motion=False), dict(playing=False),
                  dict(title='M'*120,reduced_motion=True,identity='third'), dict(title='WAIT')]:
        for runner in (old, new):
            runner.receive(**state)
        assert_state(old, new)
        draw(old, new, old.now+25)


def test_raw_minified_and_rendered_template_keep_all_native_adapter_boundaries():
    rendered = install_source(CANDIDATE, 'http://192.0.2.189:8765/nowplaying')
    assert '__STATE_URL__' not in rendered
    assert len(minify(rendered).encode('utf-8')) <= 12288
    old, new = pair(candidate=minify(rendered), calm=True)
    old.page.url = new.page.url  # Same configured endpoint for state comparison.
    for stamp in range(1000, 11801, 25):
        draw(old, new, stamp)


def test_constructor_is_retired_and_each_script_load_owns_its_buffers():
    old, new = pair()
    assert new.page.init is None
    assert_state(old, new)
    another = MediaPreview(CANDIDATE)
    assert another.page is not new.page and another.page.glyphs is not new.page.glyphs
    another.receive('OTHER')
    assert another.page.label != new.page.label


@pytest.mark.parametrize('width', range(-4, 5))
def test_byte_append_matches_firmware_width_endian_and_self_return(width):
    source = f'var b=bytes("A5") var same=b.add(0x12345678,{width}) return [b,same==b]'
    result = Program(source, real_type=R).run({})
    count = abs(width)
    expected = b'\xA5'+(0x12345678 & ((1 << (count*8))-1)).to_bytes(count, 'big' if width < 0 else 'little')
    assert result == [bytearray(expected), True]


def test_byte_append_defaults_masks_negative_values_and_can_be_chained():
    result = Program('return bytes().add(-1).add(0x123456,-3).add(-2,2)', real_type=R).run({})
    assert result == bytearray.fromhex('FF123456FEFF')


@pytest.mark.parametrize('arguments', ['', 'true', '1.5', '1,true', '1,5', '1,-5'])
def test_byte_append_rejects_invalid_value_or_width_before_mutating(arguments):
    page = Program('class A var b def init() self.b=bytes("A5") end def go() self.b.add('+arguments+') end end return A()', real_type=R).run({})
    with pytest.raises(BerryError, match='type_error'):
        page.go()
    assert page.b == bytearray.fromhex('A5')
