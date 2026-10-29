"""Actual approved/compact sources: pixels, astronomy, buffers and lifecycle."""
from pathlib import Path
import struct

import pytest

from moon_review import MoonPreview, preview_font
from weather.berry_lite import BerryError, Program
from weather.minify_ax import minify
from weather.moon_panel_sim import Font, R

ROOT = Path(__file__).resolve().parent
REFERENCE = (ROOT / 'weather/moon-panel-v2.3.ax').read_text(encoding='utf-8')
CANDIDATE = (ROOT / 'weather/moon-panel-v2.4.ax').read_text(encoding='utf-8')


def pair(*, candidate=CANDIDATE, **kwargs):
    return MoonPreview(REFERENCE, **kwargs), MoonPreview(candidate, **kwargs)


def values(page, field):
    value = getattr(page, field)
    if field == 'hw':
        return list(struct.unpack('<8f', value))
    if field in ('ct', 'mc', 'sc', 'pal'):
        return [int.from_bytes(value[i:i+3], 'big') for i in range(0, len(value), 3)]
    if field == 'span0':
        return list(value)
    if field == 'dr':
        return list(struct.unpack('<8I', value))
    if field == 'nr':
        # Only nn entries are initialized/admitted; the remaining capacity is never read.
        return [int.from_bytes(value[i*3:i*3+3], 'little') for i in range(page.nn)]
    return value


def assert_state(old, new):
    assert old.page.__berry_members__ == new.page.__berry_members__
    for field in old.page.__berry_members__:
        expected = getattr(old.page, field)
        if field == 'nr':
            expected = expected[:old.page.nn]
        assert expected == values(new.page, field), field
    assert old.page.should_show() == new.page.should_show()
    assert old.page.duration() == new.page.duration() == 11000


def same_frame(old, new):
    assert old.screen.px == new.screen.px
    assert old.screen.texts == new.screen.texts


@pytest.mark.parametrize('fill', [0, 0xA5, 0xFF])
def test_initialization_and_complete_visit_do_not_read_uninitialized_capacity(fill):
    old, new = pair(fill=fill)
    assert_state(old, new)
    assert (len(new.page.hw), len(new.page.pal), len(new.page.nr), len(new.page.dr)) == (32, 48, 192, 32)
    for _ in range(275):
        old.step(40); new.step(40)
        same_frame(old, new)
        assert_state(old, new)


@pytest.mark.parametrize('waxing', [False, True])
@pytest.mark.parametrize('eclipse', [0, 1, 100])
def test_every_illumination_retains_disc_geometry_texture_and_fading(waxing, eclipse):
    old, new = pair()
    for illumination in range(101):
        for runner in (old, new):
            runner.phase(illumination, waxing=waxing, eclipse=eclipse)
        assert_state(old, new)
        for camera in (0, 1, 11, 20, 31, 32):
            for runner in (old, new):
                runner.screen.clear()
                runner.page.draw_moon_body(camera)
                runner.page.draw_surface(camera)
            same_frame(old, new)


def test_all_drift_positions_preserve_all_admitted_runs_and_ignore_unused_slots():
    old, new = pair(fill=0xFF)
    seen_counts = set()
    for drift in range(64):
        for runner in (old, new):
            runner.clock.now_ms = drift * 3400
            runner.page.build_map()
        assert_state(old, new)
        count = new.page.nn
        assert 0 <= count <= 64
        seen_counts.add(count)
        # Garbage beyond the admitted run count must not reach the framebuffer.
        new.page.nr[count*3:] = bytes([0xFF]) * (192-count*3)
        for runner in (old, new):
            runner.page.draw()
        same_frame(old, new)
    assert len(seen_counts) > 5


@pytest.mark.parametrize('hour,minute', [(h, m) for h, m in
    [(-1, 0), (0, 0), (3, 59), (4, 0), (6, 0), (6, 29), (6, 30), (7, 0),
     (9, 59), (10, 0), (17, 59), (18, 0), (20, 29), (20, 30), (20, 59), (21, 0), (23, 59)]])
def test_hour_tints_dimming_and_entire_breathing_cycle_are_exact(hour, minute):
    old, new = pair()
    for runner in (old, new):
        runner.clock.hour, runner.clock.minute = hour, minute
    for tick in range(201):
        for runner in (old, new):
            runner.clock.now_ms = tick * 400
            runner.page.loop()
        assert_state(old, new)
    for runner in (old, new):
        runner.page.draw()
    same_frame(old, new)


@pytest.mark.parametrize('epoch', [0, 946684800000, 1767225600000, 1772323200000,
                                  1793066400000, 1798761600000, 1893456000000])
def test_astronomy_minute_refresh_and_clock_loss_recovery(epoch):
    old, new = pair(epoch_ms=epoch)
    assert_state(old, new)
    for year, advance in [(1970, 1000), (2026, 2000), (2026, 61001), (2026, 7200001)]:
        for runner in (old, new):
            runner.clock.year = year
            runner.clock.epoch_ms = epoch + advance
            runner.clock.now_ms = advance
            runner.page.loop()
            runner.page.draw()
        assert_state(old, new)
        same_frame(old, new)


@pytest.mark.parametrize('start', [0, 46999, 47000, 47060, 47120, 47640, 47959, 47960, 94000])
def test_meteor_boundaries_match_before_and_after_late_or_interrupted_visits(start):
    old, new = pair()
    for interval in (20, 40, 120, 1000, 600000, 25, 80):
        for runner in (old, new):
            runner.clock.now_ms = start
            runner.page.on_show()
            runner.step(interval)
        assert_state(old, new)
        same_frame(old, new)


def test_large_font_fallback_preserves_label_timing_and_complete_values():
    font = Font({c: ['######'] * 5 for c in preview_font().glyphs})
    old, new = pair(font=font)
    for runner in (old, new):
        runner.phase(100)
    fields = set()
    for _ in range(440):
        old.step(25); new.step(25)
        same_frame(old, new)
        fields |= {label for _x, _y, label, _color in new.screen.texts}
    assert fields == {'FULL', '100%'}
    assert_state(old, new)


def test_minified_upload_preserves_source_pixels_state_and_existing_size_cap():
    uploaded = minify(CANDIDATE)
    assert len(uploaded.encode('utf-8')) <= 12288
    assert '# @version 2.4' in uploaded
    old, new = pair(candidate=uploaded)
    for _ in range(550):
        old.step(40); new.step(40)
        same_frame(old, new)
    assert_state(old, new)


def test_constructor_is_retired_after_initialization_and_each_load_is_independent():
    old, new = pair()
    assert new.page.init is None
    assert_state(old, new)
    other = MoonPreview(CANDIDATE)
    assert other.page is not new.page and other.page.pal is not new.page.pal
    new.page.illum = 0
    new.page.build_disc()
    assert other.page.illum == old.page.illum
    assert values(other.page, 'dr') == old.page.dr


def test_model_rejects_byte_iteration_that_the_native_runtime_rejects():
    source = 'class A def go() var n=0 for p:bytes("0102") n+=p end return n end end return A()'
    page = Program(source, real_type=R).run({})
    with pytest.raises(BerryError, match='cannot iterate'):
        page.go()
    page = Program(source.replace('bytes("0102")', '[1,2]'), real_type=R).run({})
    assert page.go() == 3
