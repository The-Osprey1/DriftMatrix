"""Exact-grid and Berry source contracts for Timepiece."""

from __future__ import annotations

import ast
import re
from pathlib import Path
import unittest

import timepiece_preview as preview


ROOT = Path(__file__).resolve().parent
SOURCE_PATH = ROOT / "timepiece.ax"


class TimepieceVisualTests(unittest.TestCase):
    def test_every_numeral_is_unique_and_exactly_four_by_seven(self) -> None:
        self.assertEqual(len(preview.DIGIT_ROWS), 10)
        self.assertEqual(len(set(preview.DIGIT_ROWS)), 10)
        for rows in preview.DIGIT_ROWS:
            self.assertEqual(len(rows), 7)
            self.assertTrue(all(0 <= row <= 0xF for row in rows))

    def test_one_and_two_digit_hours_are_centered_on_the_full_panel(self) -> None:
        single = preview.draw_timepiece(2, 37, 0)
        double = preview.draw_timepiece(12, 37, 0)
        single_x = [x for y in range(7) for x in range(32) if single[y][x] != preview.BLACK]
        double_x = [x for y in range(7) for x in range(32) if double[y][x] != preview.BLACK]
        single_start, _ = preview.time_layout(2)
        double_start, _ = preview.time_layout(12)
        self.assertEqual((single_start, single_start + 15), (8, 23))
        self.assertEqual((double_start, double_start + 20), (5, 25))
        # A numeral such as 1 intentionally has an empty outer source column;
        # its ink may not touch the whole logical box, but it may never escape it.
        self.assertGreaterEqual(min(single_x), single_start)
        self.assertLessEqual(max(single_x), single_start + 15)
        self.assertGreaterEqual(min(double_x), double_start)
        self.assertLessEqual(max(double_x), double_start + 20)

    def test_hours_minutes_and_colon_use_stable_distinct_roles(self) -> None:
        frame = preview.draw_timepiece(14, 37, 10_000)
        self.assertEqual(frame[2][13], preview.MINT)
        self.assertEqual(frame[4][13], preview.MINT)
        self.assertEqual(frame[0][9], preview.GOLD)
        self.assertEqual(frame[0][15], preview.PEARL)

    def test_daypart_color_changes_only_the_hour_role(self) -> None:
        cases = ((2, preview.VIOLET), (7, preview.ICE), (14, preview.GOLD), (19, preview.CORAL))
        for hour, expected in cases:
            with self.subTest(hour=hour):
                frame = preview.draw_timepiece(hour, 37, 10_000)
                hour_pixels = {
                    frame[y][x]
                    for y in range(7)
                    for x in range(5, 13)
                    if frame[y][x] != preview.BLACK
                }
                self.assertIn(expected, hour_pixels)
                self.assertIn(preview.PEARL, {frame[y][x] for y in range(7) for x in range(15, 26)})

    def test_comet_advances_one_physical_column_at_a_time(self) -> None:
        heads = [preview.comet_head_from_ms(ms) for ms in range(60_000)]
        self.assertEqual(heads[0], 0)
        self.assertEqual(heads[-1], 31)
        self.assertEqual(sorted(set(heads)), list(range(32)))
        self.assertTrue(all(after - before in (0, 1) for before, after in zip(heads, heads[1:])))

    def test_seconds_horizon_is_truthful_connected_and_never_changes_digits(self) -> None:
        base = preview.draw_timepiece(14, 37, 600)
        for head in range(32):
            ms = max(600, int((head + 0.1) * 60_000 / 32))
            frame = preview.draw_timepiece(14, 37, ms)
            self.assertEqual(frame[:7], base[:7])
            actual_head = preview.comet_head_from_ms(ms)
            elapsed = [x for x, color in enumerate(frame[7]) if color != preview.TRACK]
            self.assertEqual(elapsed, list(range(actual_head + 1)))
            self.assertEqual(frame[7][actual_head], preview.PEARL)
            if actual_head >= 1:
                self.assertEqual(frame[7][actual_head - 1], preview.GOLD)
            if actual_head >= 2:
                self.assertEqual(frame[7][actual_head - 2], preview.MINT)

    def test_minute_roll_uses_seven_crisp_one_row_poses_then_settles(self) -> None:
        phases = (0, 75, 150, 225, 300, 375, 450)
        self.assertEqual([preview.roll_step_from_ms(ms) for ms in phases], list(range(1, 8)))
        self.assertEqual(preview.roll_step_from_ms(525), 0)
        self.assertEqual(preview.roll_step_from_ms(59_999), 0)

        settled = preview.draw_timepiece(14, 38, 600)
        landing = preview.draw_timepiece(14, 38, 450)
        for y in range(7):
            for x in range(32):
                if (x, y) not in ((13, 2), (13, 4)):
                    self.assertEqual(landing[y][x], settled[y][x])

    def test_colon_is_a_stationary_transition_cue_and_never_blinks_off(self) -> None:
        for phase in (0, 75, 450, 524, 59_800, 59_999):
            with self.subTest(phase=phase):
                frame = preview.draw_timepiece(14, 38, phase)
                self.assertEqual(frame[2][13], preview.PEARL)
                self.assertEqual(frame[4][13], preview.PEARL)
        steady = preview.draw_timepiece(14, 38, 10_000)
        self.assertEqual(steady[2][13], preview.MINT)
        self.assertEqual(steady[4][13], preview.MINT)

    def test_all_palette_roles_survive_the_live_gamma_and_brightness(self) -> None:
        for color in (
            preview.PEARL,
            preview.ICE,
            preview.MINT,
            preview.GOLD,
            preview.CORAL,
            preview.VIOLET,
            preview.TRACK,
            preview.TAIL,
        ):
            with self.subTest(color=color):
                self.assertNotEqual(
                    preview.grade_color(color, gamma=1.3, brightness=45, saturation=90),
                    preview.BLACK,
                )

    def test_all_times_render_inside_exact_32_by_8_bounds(self) -> None:
        for hour in range(24):
            for minute in (0, 7, 42, 59):
                frame = preview.draw_timepiece(hour, minute, 59_999)
                self.assertEqual(len(frame), 8)
                self.assertTrue(all(len(row) == 32 for row in frame))


class TimepieceBerryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SOURCE_PATH.read_text(encoding="utf-8")

    def test_script_is_compact_self_contained_and_has_metadata(self) -> None:
        self.assertLess(len(self.source.encode("utf-8")), 5_000)
        self.assertIn("# @name Timepiece", self.source)
        self.assertIn("# @version 2.0", self.source)
        self.assertNotIn("http.", self.source)
        self.assertNotIn("settings.set", self.source)
        self.assertNotIn("rotation.", self.source)
        self.assertNotIn("import ", self.source)

    def test_source_uses_local_time_and_a_clock_locked_minute_phase(self) -> None:
        for token in (
            "hour()",
            "minute()",
            "second()",
            "epoch_ms()",
            "% 60000",
            "phase / 75",
            "phase * 32 / 60000",
        ):
            self.assertIn(token, self.source)
        self.assertIn("return hour() >= 0", self.source)
        self.assertIn("return 15000", self.source)

    def test_source_glyphs_match_the_verified_preview_exactly(self) -> None:
        match = re.search(r"self\.glyphs = (\[.*?\])\nself\.pearl", self.source, re.DOTALL)
        self.assertIsNotNone(match)
        assert match is not None
        berry_rows = ast.literal_eval(match.group(1))
        self.assertEqual(tuple(tuple(row) for row in berry_rows), preview.DIGIT_ROWS)

    def test_draw_path_clears_once_and_uses_only_integer_pixels(self) -> None:
        draw = self.source[self.source.index("def draw()") :]
        self.assertEqual(draw.count("clear()"), 1)
        self.assertIn("pixel(head, 7, self.pearl)", draw)
        self.assertIn("self.face(ph, pm, -step)", draw)
        self.assertIn("colon_ink", draw)
        self.assertNotIn("line(", draw)
        self.assertNotIn("rect", draw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
