"""Ocean 3.4.1: 3.4's look with fixed byte caches and fewer calls, offline.

3.4 measured 37 FPS on the panel (3.3: 28) and 19,748 B of VM heap (3.3:
16,812).  3.4.1 keeps every draw cache in a preallocated bytes buffer that is
rewritten in place, so a frame allocates nothing for the collector.
"""
from pathlib import Path
import statistics

import pytest

from ocean_preview import OceanPreview
from test_ocean_v34 import PREVIOUS, CallCounter, visit
from weather.minify_ax import minify

ROOT = Path(__file__).resolve().parent
V34 = (ROOT / 'ocean-v3.4.ax').read_text(encoding='utf-8')
CANDIDATE = (ROOT / 'ocean-v3.4.1.ax').read_text(encoding='utf-8')


def test_header_names_the_new_version():
    assert '# @name Ocean' in CANDIDATE and '# @version 3.4.1' in CANDIDATE


def test_fewer_calls_than_3_4_and_half_of_3_3(monkeypatch):
    counter = CallCounter(monkeypatch)
    _, old, _ = visit(PREVIOUS, 120, counter=counter)
    _, v34, _ = visit(V34, 300, counter=counter)
    _, new, _ = visit(CANDIDATE, 600, counter=counter)
    assert statistics.mean(new) < statistics.mean(v34)
    assert statistics.mean(new) <= statistics.mean(old) * 0.55 and max(new) <= 90


def test_every_line_is_horizontal_and_on_the_panel(monkeypatch):
    counter = CallCounter(monkeypatch)
    visit(CANDIDATE, 600, counter=counter)
    for x0, y0, x1, y1, _ in counter.lines:
        assert y0 == y1 and 0 <= y0 < 8 and 0 <= x0 <= x1 <= 31


def test_draw_caches_are_fixed_bytes_rewritten_in_place():
    runner = OceanPreview(CANDIDATE)
    runner.show()
    buffers = {name: getattr(runner.page, name) for name in ('SL', 'KL', 'bed', 'VY', 'VS')}
    sizes = {name: len(value) for name, value in buffers.items()}
    for _ in range(3000):  # two visible minutes: rebuilds and seabed scrolls
        runner.advance(40)
        page = runner.page
        assert page.nsl <= len(page.SL) and page.nkl <= len(page.KL) and page.nbed <= len(page.bed)
    for name, value in buffers.items():
        assert isinstance(value, bytearray), name
        assert getattr(runner.page, name) is value, f"{name} was reallocated"
        assert len(value) == sizes[name], f"{name} grew"


def test_one_visitor_at_a_time_and_every_kind_comes_by():
    _, _, kinds = visit(CANDIDATE, 900)
    assert set(kinds) >= {-1, 0, 1, 2, 3, 4, 5}


def test_visitor_rows_and_speeds_match_3_4():
    old, new = OceanPreview(V34).page, OceanPreview(CANDIDATE).page
    assert [round(v, 1) for v in old.VY] == [b / 10 for b in new.VY]
    assert [round(v, 1) for v in old.VS] == [b / 10 for b in new.VS]


def test_same_water_and_tint_as_3_4():
    old, new = OceanPreview(V34).page, OceanPreview(CANDIDATE).page
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


def test_constructor_is_released_after_the_page_is_built():
    assert OceanPreview(CANDIDATE).page.init is None
