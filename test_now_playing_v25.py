"""Now Playing 2.5: 2.4's frames, with the title drawn as cached runs.

A timing build of 2.4 on the panel (2026-10-31) put a scrolling title at
13.4 ms of a 17 ms draw(); the scroll moves one column every three frames, so
2.5 rebuilds the title's runs only when the column or the title changes.
"""
from pathlib import Path
import statistics

import pytest

import now_playing_review as review
from weather import moon_panel_sim

ROOT = Path(__file__).resolve().parent
V24 = (ROOT / 'now_playing-v2.4.ax').read_text(encoding='utf-8')
V25 = (ROOT / 'now_playing-v2.5.ax').read_text(encoding='utf-8')

LONG = 'MIDNIGHT CITY - M83 - HURRY UP, WE\'RE DREAMING (DELUXE)'
SHORT = 'M83'


def run(source, script):
    preview = review.MediaPreview(source)
    frames, stamp = [], 2000
    for title, progress, motion, count in script:
        preview.receive(title, progress, reduced_motion=motion)
        if not preview.page.visible:
            preview.page.on_show()
        for _ in range(count):
            stamp += 25
            frames.append(preview.draw_at(stamp).tobytes())
    return frames


SCRIPTS = {
    'long scroll': [(LONG, 12, False, 900)],
    'short still': [(SHORT, 40, False, 300)],
    'title change mid-scroll': [(LONG, 30, False, 200), ('ANOTHER LONG TITLE THAT SCROLLS AWAY', 31, False, 300),
                                (SHORT, 32, False, 200), (LONG, 90, False, 200)],
    'reduced motion cards': [(LONG, 55, True, 400)],
    'rail moving': [(LONG, p, False, 40) for p in range(0, 100, 5)],
}


@pytest.mark.parametrize('name', sorted(SCRIPTS))
def test_frames_are_identical_to_2_4(name):
    assert run(V25, SCRIPTS[name]) == run(V24, SCRIPTS[name])


def test_header():
    assert '# @version 2.5' in V25


def test_a_scrolling_title_makes_fewer_calls(monkeypatch):
    """Runs merge little in a 3x5 font (most glyph rows are 1-2 px segments):
    the main saving is the bit tests, now done one frame in three."""
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
    for source in (V24, V25):
        preview = review.MediaPreview(source)
        preview.receive(LONG, 50)
        preview.page.on_show()
        counts = []
        for i in range(600):
            calls[0] = 0
            preview.draw_at(2000 + i * 25)
            counts.append(calls[0])
        means.append(statistics.mean(counts))
    assert means[1] < means[0] - 5


def test_the_run_buffer_never_overflows():
    preview = review.MediaPreview(V25)
    for title in (LONG, SHORT, 'W' * 40, 'MWMWMWMWMWMWMWMW', '#' * 30, '1111111111111111111111111'):
        preview.receive(title, 50)
        preview.page.on_show()
        for k in range(len(title) * 4 + 8):
            preview.page.build_title(k)
            assert preview.page.tn <= 240
