"""Readable pages (2026-10-31): the rules the legibility pass set.

* Nothing a person must read is drawn in the 3x3 font.  Its letters are three
  pixels tall and could not be read at a glance; every live page uses one line
  of the 3x5 font on rows 1-5.
* A value says what it measures: PC pages name their metric, network loss
  says LOSS, never a lone letter.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import game_mode
import network_sentinel_preview as network
import pc_stats_push as pc
import quake_watch
import visual_system as visual

ROOT = Path(__file__).resolve().parent

# Modules that build pages the panel actually shows.
LIVE_PAGE_MODULES = (
    "pc_stats_push.py", "network_readout.py", "network_sentinel_preview.py",
    "quake_watch.py", "game_mode.py", "sky_watch.py", "notify_mirror.py",
    "desk_companion.py", "focus_sprint_preview.py", "next_up_preview.py",
    "weather/weather_brief_push.py", "weather/aqi_brief_push.py", "weather/wx_pages.py",
)
TINY_TYPE = {"render_split_lane", "render_dual_lane", "draw_text_3x3"}


@pytest.mark.parametrize("name", LIVE_PAGE_MODULES)
def test_live_pages_never_use_the_3x3_font(name):
    tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    used |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    used |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
             for alias in node.names}
    assert not used & TINY_TYPE, f"{name} uses {sorted(used & TINY_TYPE)}"


def text_rows(lane):
    """Rows 0-5 with lit pixels; ``lane`` is the 23px value lane."""
    return {y for y in range(6) for pixel in lane[y] if pixel != visual.BLACK}


def lane_of(frame):
    return [row[9:] for row in frame]


@pytest.mark.parametrize("lane", [
    lane_of(pc.glance_lane(82, "CPU")), lane_of(pc.glance_lane(100, "GPU")),
    lane_of(game_mode.render_lane(game_mode.GpuReading(71, 88), 42)),
    lane_of(quake_watch.render_lane(quake_watch.Quake("x", 3.4, "Corona", 0, 33.9, -117.5, 8.0))),
    network.lane_frame("loss", loss_percent=30),
])
def test_one_full_height_line_of_type(lane):
    assert len(lane[0]) == 23
    assert text_rows(lane) == {1, 2, 3, 4, 5}


@pytest.mark.parametrize("label", ["CPU", "RAM", "GPU"])
@pytest.mark.parametrize("percent", [0, 82, 99, 100])
def test_pc_pages_name_their_metric(label, percent):
    frame = pc.glance_lane(percent, label)
    named = visual.render_glance_lane(label, meter=percent / 100, level=pc.glance_level(percent))
    assert visual.MIST in frame[3][9:] or frame == named, "the lane does not name its metric"
    assert frame != visual.render_glance_lane(f"{percent}%", meter=percent / 100,
                                              level=pc.glance_level(percent))


def test_combined_page_names_the_worse_metric():
    assert pc.combined_lane_label(82, 91) == "RAM"
    assert pc.combined_lane_label(95, 91) == "CPU"
    assert pc.page_view([("CPU", 95), ("RAM", 91)]).lane_label == "CPU"


def test_no_single_letter_network_abbreviation():
    for state in network.NetworkState:
        words = network.state_label(state).split()
        assert all(len(word) > 1 or word.isdigit() for word in words), words
