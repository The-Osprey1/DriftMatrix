"""Ocean 3.4: frame cost, brightness and the one-visitor schedule, offline.

3.3 ran 28 FPS on the panel (minimum 18) on roughly 140 native draw calls a
frame in this model.  The emulator counts calls and interpreter work, not
ESP32 time; FPS and VM heap are measured on the panel after install.
"""
from pathlib import Path
import statistics

import pytest

from ocean_preview import OceanPreview
from weather import moon_panel_sim
from weather.minify_ax import minify

ROOT = Path(__file__).resolve().parent
PREVIOUS = (ROOT / 'ocean-v3.3.ax').read_text(encoding='utf-8')
CANDIDATE = (ROOT / 'ocean-v3.4.ax').read_text(encoding='utf-8')


class CallCounter:
    """Count native pixel()/line() calls per draw; a line's own pixels don't count."""

    def __init__(self, monkeypatch):
        self.calls, self.lines, self.depth = 0, [], 0
        pixel, line = moon_panel_sim.Screen.pixel, moon_panel_sim.Screen.line

        def counted_pixel(screen, *args):
            if self.depth == 0:
                self.calls += 1
            return pixel(screen, *args)

        def counted_line(screen, *args):
            self.calls += 1
            self.lines.append(args)
            self.depth += 1
            try:
                return line(screen, *args)
            finally:
                self.depth -= 1

        monkeypatch.setattr(moon_panel_sim.Screen, 'pixel', counted_pixel)
        monkeypatch.setattr(moon_panel_sim.Screen, 'line', counted_line)


def visit(source, seconds, *, hour=12, counter=None):
    runner = OceanPreview(source)
    runner.hour = hour
    runner.show()
    per_frame, kinds = [], []
    for _ in range(int(seconds * 25)):
        if counter is not None:
            counter.calls = 0
        runner.advance(40)
        if counter is not None:
            per_frame.append(counter.calls)
        kinds.append(getattr(runner.page, 'vk', None))
    return runner, per_frame, kinds


def test_header_names_the_new_version():
    assert '# @name Ocean' in CANDIDATE and '# @version 3.4' in CANDIDATE


def test_a_frame_costs_about_half_the_native_calls_of_3_3(monkeypatch):
    counter = CallCounter(monkeypatch)
    _, old, _ = visit(PREVIOUS, 120, counter=counter)
    _, new, _ = visit(CANDIDATE, 600, counter=counter)
    assert statistics.mean(new) <= 80 and max(new) <= 100
    assert statistics.mean(new) <= statistics.mean(old) * 0.6


def test_every_line_is_horizontal_and_on_the_panel(monkeypatch):
    counter = CallCounter(monkeypatch)
    visit(CANDIDATE, 600, counter=counter)
    assert counter.lines
    for x0, y0, x1, y1, _ in counter.lines:
        assert y0 == y1 and 0 <= y0 < 8
        assert 0 <= x0 <= x1 <= 31


def test_one_visitor_at_a_time_and_every_kind_comes_by():
    runner, _, kinds = visit(CANDIDATE, 900)
    assert set(kinds) >= {-1, 0, 1, 2, 3, 4, 5}
    assert all(kind in range(-1, 6) for kind in kinds)


def test_the_shark_is_rare():
    runner = OceanPreview(CANDIDATE)
    runner.show()
    starts, previous = [], None
    for tick in range(30 * 60 * 5):  # thirty visible minutes at 200 ms
        runner.advance(200)
        kind = runner.page.vk
        if kind == 0 and previous != 0:
            starts.append(tick * 200)
        previous = kind
    assert 4 <= len(starts) <= 9
    assert starts[0] >= 150_000
    assert all(b - a >= 240_000 for a, b in zip(starts, starts[1:]))


def test_water_is_brighter_than_3_3_and_tinted_by_the_hour():
    old, _, _ = visit(PREVIOUS, 1)
    new_rows = OceanPreview(CANDIDATE).page
    new_rows.tint(12)
    old_rows = [int.from_bytes(old.page.w[i:i + 3], 'big') for i in range(0, 24, 3)]
    for old_colour, new_colour in zip(old_rows, list(new_rows.W)):
        assert sum((new_colour >> s) & 255 for s in (16, 8, 0)) > sum((old_colour >> s) & 255 for s in (16, 8, 0))
    midday = list(new_rows.W)
    new_rows.tint(8)
    morning = list(new_rows.W)
    new_rows.tint(-1)  # before NTP: no tint, never an error
    assert list(new_rows.W) == midday
    assert (morning[0] >> 16) > (midday[0] >> 16)  # warmer at the surface
    assert morning[6:] == midday[6:]  # the deep water keeps its colour


@pytest.mark.parametrize('hour', [-1, 6, 7, 12, 16, 17, 22])
def test_schedule_and_duration_are_unchanged(hour):
    old, new = OceanPreview(PREVIOUS), OceanPreview(CANDIDATE)
    for runner in (old, new):
        runner.hour = hour
    assert old.page.should_show() == new.page.should_show()
    assert new.page.duration() == 30000


def test_minified_upload_draws_identically_and_fits_the_cap():
    compact = minify(CANDIDATE)
    assert len(compact.encode('utf-8')) <= 12288
    raw, small = OceanPreview(CANDIDATE), OceanPreview(compact)
    raw.show(); small.show()
    for _ in range(400):
        assert raw.advance(120) == small.advance(120)


def test_constructor_is_released_after_the_page_is_built():
    assert OceanPreview(CANDIDATE).page.init is None
