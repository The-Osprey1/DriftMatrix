"""Execute the actual candidate and reference sources through their lifecycles."""
from pathlib import Path

import pytest

from ocean_preview import CANDIDATE, INSTALLED, OceanPreview
from weather.moon_panel_sim import R
from weather.minify_ax import minify


def test_cache_packing_uses_fixed_bytes_and_is_independent_of_resize_fill():
    runs = [OceanPreview(fill=fill) for fill in (0, 0xA5, 0xFF)]
    for runner in runs:
        runner.show()
        assert runner.cache_storage() == {'sc': {'kind': 'bytes', 'entries': 384},
                                          'sn4': {'kind': 'bytes', 'entries': 32}}
        assert set(runner.page.sc) == {0}
        assert set(runner.page.sn4) == {0}
    for _ in range(12):
        frames = [runner.advance(40) for runner in runs]
        assert frames[0] == frames[1] == frames[2]


@pytest.mark.parametrize('start', [0, 10_000, 1_000_000_000])
def test_hidden_time_does_not_spend_encounters_or_advance_world(start):
    runner = OceanPreview(wall_ms=start)
    runner.show(); runner.advance(4000)
    page = runner.page
    before = (page.scene_ms, page.lx, page.snext, page.mnext, page.hc_next, page.seed)
    runner.hide(); runner.advance(600_000, frame_ms=1000)
    assert (page.scene_ms, page.lx, page.snext, page.mnext, page.hc_next, page.seed) == before
    runner.show(); runner.advance(40)
    assert page.scene_ms == before[0] + 40
    assert abs(float(page.lx) - float(before[1])) < .1


def test_manta_arrives_in_first_visit_and_resumes_after_hidden_interval():
    runner = OceanPreview(); runner.show()
    runner.advance(11000)
    assert runner.page.mact is False
    runner.advance(2000)
    assert runner.page.mact is True
    runner.advance(5000)
    position = runner.page.mx
    runner.hide(); runner.advance(300000, frame_ms=1000)
    assert runner.page.mact is True and runner.page.mx == position
    runner.show(); runner.advance(40)
    assert 0 < abs(float(runner.page.mx - position)) < .15


def test_timed_seafloor_encounter_is_not_spent_while_hidden():
    old = OceanPreview(INSTALLED.read_text(encoding='utf-8'))
    new = OceanPreview()
    for runner in (old, new):
        runner.show(); runner.advance(1000); runner.hide()
        runner.advance(65000, frame_ms=1000)
    assert old.page.hc_next > 45000  # The old visitor expired and was rescheduled.
    assert new.page.hc_next == 45000
    assert new.page.hc_act is False


def test_first_visit_has_visible_manta_pixels():
    runner = OceanPreview(); runner.show(); runner.advance(20000)
    assert runner.page.mact is True
    assert any(pixel in (runner.page.c[13], runner.page.c[14]) for pixel in runner.screen.px)
    assert all(max((c >> 16) & 255, (c >> 8) & 255, c & 255) >= 174
               for c in (runner.page.c[13], runner.page.c[14]))


def test_cache_packing_preserves_reference_pixels_under_equivalent_visible_time():
    old = OceanPreview(INSTALLED.read_text(encoding='utf-8'))
    new = OceanPreview()
    # Align the intentional first-visit deadline and brighter manta colors to
    # isolate cache/render changes. The reference source itself is untouched.
    old.page.mnext = 12000
    old.page.c[13:15] = new.page.c[13:15]
    old.show(); new.show()
    for index in range(100):
        assert old.advance(120) == new.advance(120), index


@pytest.mark.parametrize('field', ['sact', 'mact', 'tr_act', 'ag_act', 'ed_act', 'hc_act'])
def test_every_encounter_path_preserves_reference_rendering(field):
    old = OceanPreview(INSTALLED.read_text(encoding='utf-8'))
    new = OceanPreview()
    old.page.c[13:15] = new.page.c[13:15]
    for runner in (old, new):
        runner.show()
        setattr(runner.page, field, True)
        for name in ('sx', 'mx', 'tr_x', 'ag_x', 'ed_x', 'hc_x'):
            setattr(runner.page, name, R(12.0))
    for index in range(12):
        assert old.advance(40) == new.advance(40), (field, index)


@pytest.mark.parametrize('hour,expected', [(-1, True), (6, False), (7, True),
                                          (16, True), (17, False), (23, False)])
def test_original_daytime_fallback_is_preserved(hour, expected):
    runner = OceanPreview(); runner.hour = hour
    assert runner.page.should_show() is expected


def test_shared_sunset_schedule_and_visit_duration_are_preserved():
    runner = OceanPreview()
    runner.shared.phase = 'night'
    assert runner.page.should_show() is False
    runner.shared.phase = 'day'
    assert runner.page.should_show() is True
    assert runner.page.duration() == 30000


def test_minified_candidate_executes_and_retains_version_metadata():
    source = minify(CANDIDATE.read_text(encoding='utf-8'))
    assert '# @version 3.0' in source
    runner = OceanPreview(source); runner.show(); runner.advance(400)
    assert runner.page.scene_ms == 400
    assert len(runner.screen.frame().pixels) == 256
