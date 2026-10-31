"""Moon 2.5.1: 2.5 with a 64-byte visible-run buffer (was 256).

2.5 left the four resident scripts 372 B under the 75% VM ceiling.  Over every
hour's palette, dimming step, breath and drift step at most 8 nebula runs can
light the panel, so 16 slots keep twice the margin.
"""
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'weather'))

from moon_review import MoonPreview  # noqa: E402
from test_moon_v25 import V24, graded  # noqa: E402
from weather.minify_ax import minify  # noqa: E402

V251 = (ROOT / 'weather/moon-panel-v2.5.1.ax').read_text(encoding='utf-8')


def test_header_and_buffer():
    assert '# @version 2.5.1' in V251
    assert len(MoonPreview(V251).page.vr) == 64


def test_no_palette_and_drift_step_needs_more_than_the_buffer():
    runner = MoonPreview(V251)
    page = runner.page
    maps = []
    for d in range(64):
        runner.clock.now_ms = d * 3400 + 1
        page.lastd = -1
        page.build_map()
        maps.append([int.from_bytes(page.nr[i * 3:i * 3 + 3], 'little') for i in range(page.nn)])
    worst = 0
    for hour in range(0, 24, 2):
        runner.clock.hour = hour
        page.lh = -99
        page.tint()
        for minute in (0, 30):
            runner.clock.minute = minute
            page.shade()
            for v in range(0, 200, 4):
                runner.clock.now_ms = v * 400
                page.build_pal()
                lit = [max(page.pal[l * 3:l * 3 + 3]) >= 20 for l in range(16)]
                for runs in maps:
                    worst = max(worst, sum(1 for p in runs if lit[p >> 13]))
    assert worst <= 8 < 16


@pytest.mark.parametrize('hour', [3, 6, 13, 19, 22])
def test_the_panel_shows_exactly_what_2_4_showed(hour):
    old, new = MoonPreview(V24), MoonPreview(V251)
    old.clock.hour = new.clock.hour = hour
    for _ in range(1500):
        old.step(40)
        new.step(40)
        assert graded(old.screen) == graded(new.screen)
        assert old.screen.texts == new.screen.texts


def test_minified_upload_draws_identically_and_fits_the_cap():
    compact = minify(V251)
    assert len(compact.encode('utf-8')) <= 12288
    raw, small = MoonPreview(V251), MoonPreview(compact)
    for _ in range(600):
        assert raw.step(40) == small.step(40)
