"""Moon 2.5: 2.4's picture on the panel, without the nebula runs it cannot light.

A timing build of 2.4 on the panel (2026-10-31) put draw() at 12.5 ms, 5.9 ms
of it the nebula: about 41 runs a frame, about 39 in colours no channel of
which reaches the 23 the panel needs to light an LED at brightness 45.
"""
from pathlib import Path
import statistics
import sys

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'weather'))

from moon_review import MoonPreview  # noqa: E402
from weather import moon_panel_sim  # noqa: E402
from weather.minify_ax import minify  # noqa: E402
from weather_brief_preview import grade_color  # noqa: E402

V24 = (ROOT / 'weather/moon-panel-v2.4.ax').read_text(encoding='utf-8')
V25 = (ROOT / 'weather/moon-panel-v2.5.ax').read_text(encoding='utf-8')


def graded(screen):
    """What the LEDs show at the panel's brightness 45 (gamma 1.8, saturation 90)."""
    return [grade_color(((c >> 16) & 255, (c >> 8) & 255, c & 255),
                        gamma=1.8, brightness=45, saturation=90) for c in screen.px]


def pair(hour):
    old, new = MoonPreview(V24), MoonPreview(V25)
    for runner in (old, new):
        runner.clock.hour = hour
    return old, new


@pytest.mark.parametrize('hour', [3, 6, 9, 13, 19, 22])
def test_the_panel_shows_exactly_what_2_4_showed(hour):
    old, new = pair(hour)
    for _ in range(1500):  # a minute of 40 ms frames: glide, info card, meteor window
        old.step(40)
        new.step(40)
        assert graded(old.screen) == graded(new.screen)
        assert old.screen.texts == new.screen.texts


def test_only_runs_the_panel_can_light_are_drawn():
    _, new = pair(13)
    for _ in range(600):
        new.step(40)
        page = new.page
        for i in range(0, page.vn, 4):
            level = page.vr[i + 3]
            colour = int.from_bytes(page.pal[level * 3:level * 3 + 3], 'big')
            assert max((colour >> 16) & 255, (colour >> 8) & 255, colour & 255) >= 20


def test_every_visible_run_is_kept():
    _, new = pair(13)
    page = new.page
    for _ in range(300):
        new.step(40)
    runs = [int.from_bytes(page.nr[i * 3:i * 3 + 3], 'little') for i in range(page.nn)]
    lit = [p for p in runs
           if max(page.pal[(p >> 13) * 3:(p >> 13) * 3 + 3]) >= 20]
    kept = [(page.vr[i], page.vr[i + 1], page.vr[i + 2]) for i in range(0, page.vn, 4)]
    assert kept == [(p & 31, (p >> 5) & 31, (p >> 10) & 7) for p in lit]


def test_a_frame_makes_far_fewer_native_calls(monkeypatch):
    calls = [0]
    depth = [0]
    pixel, line = moon_panel_sim.Screen.pixel, moon_panel_sim.Screen.line

    def counted_pixel(screen, *args):
        if depth[0] == 0:
            calls[0] += 1
        return pixel(screen, *args)

    def counted_line(screen, *args):
        calls[0] += 1
        depth[0] += 1
        try:
            return line(screen, *args)
        finally:
            depth[0] -= 1

    monkeypatch.setattr(moon_panel_sim.Screen, 'pixel', counted_pixel)
    monkeypatch.setattr(moon_panel_sim.Screen, 'line', counted_line)
    means = []
    for runner in pair(13):
        counts = []
        for _ in range(1500):
            calls[0] = 0
            runner.step(40)
            counts.append(calls[0])
        means.append(statistics.mean(counts))
    old, new = means
    assert new <= old - 25


def test_minified_upload_draws_identically_and_stays_under_the_cap():
    compact = minify(V25)
    assert len(compact.encode('utf-8')) <= 16384
    raw, small = MoonPreview(V25), MoonPreview(compact)
    for _ in range(600):
        assert raw.step(40) == small.step(40)


def test_header_and_schedule():
    assert '# @version 2.5' in V25
    old, new = pair(13)
    assert old.page.should_show() == new.page.should_show()
    assert new.page.duration() == 11000
