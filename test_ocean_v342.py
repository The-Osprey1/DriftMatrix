"""Ocean 3.4.2: the fixes a panel timing build of 3.4.1 pointed to, offline.

On the panel (2026-10-31) 3.4.1's draw() took 12.3 ms: the 100 ms cache
rebuild 3.4 ms a frame on average (13.6 ms each time), and each replayed
call 0.1-0.16 ms whatever its length.  So 3.4.2 counts calls, not pixels.
"""
from pathlib import Path
import statistics

import pytest

from ocean_preview import OceanPreview
from test_ocean_v34 import PREVIOUS, CallCounter, visit
from weather.minify_ax import minify

ROOT = Path(__file__).resolve().parent
V341 = (ROOT / 'ocean-v3.4.1.ax').read_text(encoding='utf-8')
CANDIDATE = (ROOT / 'ocean-v3.4.2.ax').read_text(encoding='utf-8')


def test_header_names_the_new_version():
    assert '# @name Ocean' in CANDIDATE and '# @version 3.4.2' in CANDIDATE


def test_fewer_calls_than_3_4_1(monkeypatch):
    counter = CallCounter(monkeypatch)
    _, old, _ = visit(V341, 300, counter=counter)
    _, new, _ = visit(CANDIDATE, 600, counter=counter)
    assert statistics.mean(new) <= statistics.mean(old) - 10
    assert max(new) <= 75


def test_every_line_is_horizontal_and_on_the_panel(monkeypatch):
    counter = CallCounter(monkeypatch)
    visit(CANDIDATE, 600, counter=counter)
    for x0, y0, x1, y1, _ in counter.lines:
        assert y0 == y1
        assert 0 <= x0 <= x1 <= 31 and 0 <= y0 < 8


def test_shafts_are_bands_that_narrow_with_depth():
    runner = OceanPreview(CANDIDATE)
    runner.show()
    seen = 0
    for _ in range(1500):
        runner.advance(40)
        page = runner.page
        rows = {}
        for i in range(0, page.nsl, 3):
            x0, x1, y = page.SL[i], page.SL[i + 1], page.SL[i + 2]
            assert 0 <= x0 <= x1 <= 31 and 0 <= y < 4
            rows.setdefault(y, []).append((x0, x1))
        for y, runs in rows.items():
            if y:
                # Every deeper run sits inside a run on the row above it.
                assert all(any(a <= x0 and x1 <= b for a, b in rows[y - 1]) for x0, x1 in runs)
        seen += bool(rows)
    assert seen > 1000


def test_draw_caches_are_fixed_bytes_rewritten_in_place():
    runner = OceanPreview(CANDIDATE)
    runner.show()
    buffers = {name: getattr(runner.page, name) for name in ('SL', 'KL', 'bed', 'VY', 'VS')}
    sizes = {name: len(value) for name, value in buffers.items()}
    for _ in range(3000):
        runner.advance(40)
        page = runner.page
        assert page.nsl <= len(page.SL) and page.nkl <= len(page.KL) and page.nbed <= len(page.bed)
    for name, value in buffers.items():
        assert getattr(runner.page, name) is value and len(value) == sizes[name], name


def test_one_visitor_at_a_time_and_every_kind_comes_by():
    _, _, kinds = visit(CANDIDATE, 900)
    assert set(kinds) >= {-1, 0, 1, 2, 3, 4, 5}


def test_same_water_tint_and_visitors_as_3_4_1():
    old, new = OceanPreview(V341).page, OceanPreview(CANDIDATE).page
    assert bytes(old.VY) == bytes(new.VY) and bytes(old.VS) == bytes(new.VS)
    for hour in (-1, 6, 8, 12, 15, 17):
        old.tint(hour)
        new.tint(hour)
        assert list(old.W) == list(new.W) and list(old.H) == list(new.H)


@pytest.mark.parametrize('hour', [-1, 6, 7, 12, 16, 17, 22])
def test_schedule_and_duration_are_unchanged(hour):
    old, new = OceanPreview(PREVIOUS), OceanPreview(CANDIDATE)
    old.hour = new.hour = hour
    assert old.page.should_show() == new.page.should_show()
    assert new.page.duration() == 30000


def test_minified_upload_draws_identically_and_fits_the_cap():
    compact = minify(CANDIDATE)
    assert len(compact.encode('utf-8')) <= 12288
    raw, small = OceanPreview(CANDIDATE), OceanPreview(compact)
    raw.show(); small.show()
    for _ in range(400):
        assert raw.advance(120) == small.advance(120)


def test_constructor_helpers_are_released_after_the_page_is_built():
    page = OceanPreview(CANDIDATE).page
    assert page.init is None and page.runs is None
