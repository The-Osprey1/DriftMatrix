"""Moon 2.6: a nebula the panel actually shows, paid for with cached stars.

2.4's nebula colours stayed below the 23 a channel needs to light an LED at
brightness 45, so 2.5 stopped drawing them.  2.6 brightens three levels so
they light at about 3, 6 and 11 of 255 (the moon and stars reach ~41).
"""
from pathlib import Path
import statistics
import sys

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'weather'))

from moon_review import MoonPreview  # noqa: E402
from test_moon_v25 import V24  # noqa: E402
from weather import moon_panel_sim  # noqa: E402
from weather.minify_ax import minify  # noqa: E402
from weather_brief_preview import grade_color  # noqa: E402

V26 = (ROOT / 'weather/moon-panel-v2.6.ax').read_text(encoding='utf-8')


def led(colour):
    return grade_color(((colour >> 16) & 255, (colour >> 8) & 255, colour & 255),
                       gamma=1.8, brightness=45, saturation=90)


def test_header():
    assert '# @version 2.6' in V26


@pytest.mark.parametrize('hour', [2, 5, 8, 12, 19])
def test_every_nebula_level_lights_the_panel_by_day_and_evening(hour):
    runner = MoonPreview(V26)
    page = runner.page
    runner.clock.hour = hour
    page.lh = -99
    page.tint()
    page.dim = 100
    for v in range(0, 200, 10):
        runner.clock.now_ms = v * 400
        page.build_pal()
        levels = [max(led(int.from_bytes(page.pal[i * 3:i * 3 + 3], 'big'))) for i in range(3)]
        assert levels[0] >= 2 and levels[0] < levels[1] < levels[2] <= 20


def test_runs_stay_few_enough_to_draw():
    runner = MoonPreview(V26)
    page = runner.page
    counts = []
    for d in range(64):
        runner.clock.now_ms = d * 3400 + 1
        page.lastd = -1
        page.build_map()
        counts.append(page.nn)
        levels = {int.from_bytes(page.nr[i * 3:i * 3 + 3], 'little') >> 13 for i in range(page.nn)}
        assert levels <= {0, 1, 2}
    assert statistics.mean(counts) <= 20 and max(counts) <= 30


def test_the_nebula_is_visible_on_the_panel():
    runner = MoonPreview(V26)
    runner.clock.hour = 13
    lit_background = 0
    for _ in range(100):
        runner.step(40)
        page = runner.page
        nebula = {(int.from_bytes(page.nr[i * 3:i * 3 + 3], 'little') >> 10 & 7,
                   x) for i in range(page.nn)
                  for p in [int.from_bytes(page.nr[i * 3:i * 3 + 3], 'little')]
                  for x in range(p & 31, ((p >> 5) & 31) + 1)}
        lit_background += sum(1 for (y, x) in nebula
                              if max(led(runner.screen.px[y * 32 + x])) >= 2)
    assert lit_background > 1000


def test_stars_are_drawn_every_frame_from_the_cache(monkeypatch):
    runner = MoonPreview(V26)
    runner.clock.hour = 13
    for _ in range(10):
        runner.step(40)
    page = runner.page
    assert len(page.sk) == 72
    assert sum(1 for i in range(24) if int.from_bytes(page.sk[i * 3:i * 3 + 3], 'big')) >= 20


def test_frame_cost_stays_near_2_4(monkeypatch):
    calls, depth = [0], [0]
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
    for source in (V24, V26):
        runner = MoonPreview(source)
        runner.clock.hour = 13
        counts = []
        for _ in range(1500):
            calls[0] = 0
            runner.step(40)
            counts.append(calls[0])
        means.append(statistics.mean(counts))
    assert means[1] <= means[0]


def test_minified_upload_draws_identically_and_fits_the_cap():
    compact = minify(V26)
    assert len(compact.encode('utf-8')) <= 12288
    raw, small = MoonPreview(V26), MoonPreview(compact)
    for _ in range(600):
        assert raw.step(40) == small.step(40)


def test_schedule_and_labels_unchanged():
    old, new = MoonPreview(V24), MoonPreview(V26)
    old.clock.hour = new.clock.hour = 13
    assert old.page.should_show() == new.page.should_show()
    assert new.page.duration() == 11000
    for _ in range(300):
        old.step(40)
        new.step(40)
        assert old.screen.texts == new.screen.texts
