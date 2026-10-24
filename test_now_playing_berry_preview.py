"""Visual and cadence contracts for the device-side Berry renderer.

The panel can only show whole physical pixels.  Production therefore advances
the proportional title by exactly one column after every three *completed*
draws.  Wall-clock time gates the opening hold but never determines distance.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest

import preview_now_playing_berry as preview


SCRIPT_PATH = Path(__file__).with_name("now_playing.ax")
BLACK = (0, 0, 0)


def _text_mask(image: object) -> tuple[tuple[bool, ...], ...]:
    """Return only the 23x5 title occupancy mask (record/rail excluded)."""

    return tuple(
        tuple(image.getpixel((x, y)) != BLACK for x in range(9, 32))  # type: ignore[attr-defined]
        for y in range(1, 6)
    )


def _scroll_offset(elapsed_ms: int, completed_frames: int) -> int:
    helper = getattr(preview, "scroll_offset_px", None)
    if not callable(helper):
        raise AssertionError("preview.scroll_offset_px(elapsed_ms, completed_frames) is required")
    return int(helper(elapsed_ms, completed_frames))


def _render_at_frame(title: str, frame: int, *, elapsed_ms: int = 1000) -> object:
    return preview.render_matrix(
        title,
        elapsed_ms=elapsed_ms,
        progress=-1,
        completed_scroll_frames=frame,
    )


class BerryPreviewTests(unittest.TestCase):
    def test_now_playing_is_immediate_for_new_media_then_only_a_five_minute_reminder(self) -> None:
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertIn("var REMINDER_MS = 300000", source)
        self.assertIn("if is_new self.last_shown = 0 end", source)
        self.assertIn("self.last_shown = now_ms()", source)
        self.assertRegex(
            source,
            r"self\.visible \|\| self\.last_shown == 0 \|\| now_ms\(\) - self\.last_shown >= REMINDER_MS",
        )

    def test_proportional_font_has_expected_real_widths(self) -> None:
        self.assertEqual(len(preview.glyph_columns("I")), 1)
        self.assertEqual(len(preview.glyph_columns("!")), 1)
        self.assertEqual(len(preview.glyph_columns("W")), 3)
        self.assertEqual(len(preview.glyph_columns("M")), 5)
        self.assertEqual(len(preview.glyph_columns(" ")), 2)
        self.assertEqual(len(preview.glyph_columns(",")), 2)
        self.assertLess(preview.text_width("IIII"), preview.text_width("WWWW"))

    def test_text_columns_add_exactly_one_separator_between_glyphs(self) -> None:
        i_columns = preview.glyph_columns("I")
        w_columns = preview.glyph_columns("W")
        expected = i_columns + (0,) + w_columns + (0,) + i_columns
        self.assertEqual(preview.text_columns("IWI"), expected)
        self.assertEqual(preview.text_width("IWI"), 1 + 1 + 3 + 1 + 1)
        self.assertNotEqual(i_columns[0], 0)
        self.assertNotEqual(w_columns[0], 0)
        self.assertNotEqual(w_columns[-1], 0)

    def test_short_title_centres_by_actual_proportional_width(self) -> None:
        image = preview.render_matrix("I", elapsed_ms=0, progress=-1)
        lit_x = {
            x
            for x in range(9, 32)
            for y in range(1, 6)
            if image.getpixel((x, y)) != BLACK
        }
        # One-column I is centred in the 23-column title viewport.
        self.assertEqual(lit_x, {20})

        title = "IWI"
        width = preview.text_width(title)
        origin = 9 + (23 - width) // 2
        image = preview.render_matrix(title, elapsed_ms=0, progress=-1)
        lit_x = {
            x
            for x in range(9, 32)
            for y in range(1, 6)
            if image.getpixel((x, y)) != BLACK
        }
        self.assertEqual(min(lit_x), origin)
        self.assertEqual(max(lit_x), origin + width - 1)

    def test_overflow_edge_fade_is_exact_and_active_during_hold(self) -> None:
        # Seven W glyphs are 27 columns and row 3 lights all three glyph columns.
        image = preview.render_matrix("WWWWWWW", elapsed_ms=0, progress=-1)
        self.assertEqual(image.getpixel((9, 3)), preview.OUTER_TEXT_COLOR)
        self.assertEqual(image.getpixel((10, 3)), preview.MID_TEXT_COLOR)
        self.assertEqual(image.getpixel((11, 3)), preview.TEXT_COLOR)
        self.assertEqual(image.getpixel((30, 3)), preview.MID_TEXT_COLOR)
        self.assertEqual(image.getpixel((31, 3)), preview.OUTER_TEXT_COLOR)

    def test_fitting_text_remains_full_brightness_at_its_edges(self) -> None:
        # Twelve narrow I glyphs plus eleven separators are exactly 23 columns.
        self.assertEqual(preview.text_width("I" * 12), 23)
        image = preview.render_matrix("I" * 12, elapsed_ms=0, progress=-1)
        lit = {
            image.getpixel((x, y))
            for x in range(9, 32)
            for y in range(1, 6)
            if image.getpixel((x, y)) != BLACK
        }
        self.assertEqual(lit, {preview.TEXT_COLOR})

    def test_every_progress_phase_preserves_the_truthful_fill_count(self) -> None:
        track = preview._rgb(0x35131F)
        for amount in range(25):
            for phase in range(3):
                with self.subTest(amount=amount, phase=phase):
                    image = preview.render_matrix(
                        "ABCDEFG",
                        elapsed_ms=1500,
                        progress=amount,
                        cap_elapsed_ms=phase * 250,
                    )
                    rail = [image.getpixel((x, 7)) for x in range(8, 32)]
                    self.assertEqual(sum(color != track for color in rail), amount)

    def test_opening_hold_is_1000ms_and_never_uses_time_for_distance(self) -> None:
        self.assertEqual(getattr(preview, "SCROLL_HOLD_MS", None), 1000)
        self.assertEqual(getattr(preview, "SCROLL_FRAMES_PER_PIXEL", None), 3)
        for elapsed_ms in (-1, 0, 999):
            self.assertEqual(_scroll_offset(elapsed_ms, 999_999), 0)

        # Once the hold is over, an enormous clock jump cannot skip columns.
        for completed in range(12):
            with self.subTest(completed=completed):
                self.assertEqual(
                    _scroll_offset(1000, completed),
                    _scroll_offset(10**12, completed),
                )

    def test_each_position_is_stable_for_exactly_three_completed_frames(self) -> None:
        samples = [_scroll_offset(1000, frame) for frame in range(30)]
        runs: list[tuple[int, int]] = []
        for offset in samples:
            if not runs or runs[-1][0] != offset:
                runs.append((offset, 1))
            else:
                value, count = runs[-1]
                runs[-1] = (value, count + 1)
        self.assertEqual([value for value, _ in runs], list(range(10)))
        self.assertEqual([count for _, count in runs], [3] * 10)

    def test_scroll_transitions_are_monotonic_single_pixel_steps(self) -> None:
        offsets = [_scroll_offset(1000, frame) for frame in range(121)]
        transitions = [
            frame
            for frame in range(1, len(offsets))
            if offsets[frame] != offsets[frame - 1]
        ]
        deltas = [
            offsets[frame] - offsets[frame - 1]
            for frame in range(1, len(offsets))
            if offsets[frame] != offsets[frame - 1]
        ]
        self.assertEqual(deltas, [1] * 40)
        self.assertEqual(transitions, list(range(3, 121, 3)))

    def test_each_step_moves_binary_title_exactly_one_pixel_left(self) -> None:
        title = "MIDNIGHT CITY - M83"
        previous = _text_mask(_render_at_frame(title, 0))
        for step in range(1, 20):
            current = _text_mask(_render_at_frame(title, step * 3))
            for row in range(5):
                self.assertEqual(
                    current[row][:-1], previous[row][1:], f"non-unit move at {step=}"
                )
            previous = current

    def test_all_three_frames_at_a_position_are_pixel_identical(self) -> None:
        title = "MIDNIGHT CITY - M83"
        for step in range(20):
            frames = [
                _text_mask(_render_at_frame(title, step * 3 + phase, elapsed_ms=10**9))
                for phase in range(3)
            ]
            self.assertEqual(frames[0], frames[1], f"changed within {step=}")
            self.assertEqual(frames[1], frames[2], f"changed within {step=}")

    def test_scroll_uses_only_fixed_screen_space_fade_colors(self) -> None:
        expected_for_x = {
            **{x: preview.TEXT_COLOR for x in range(11, 30)},
            9: preview.OUTER_TEXT_COLOR,
            10: preview.MID_TEXT_COLOR,
            30: preview.MID_TEXT_COLOR,
            31: preview.OUTER_TEXT_COLOR,
        }
        seen_x: set[int] = set()
        title = "MIDNIGHT CITY - M83"
        period = preview.text_width(title) + 8
        for step in range(period):
            image = _render_at_frame(title, step * 3)
            for x in range(9, 32):
                for y in range(1, 6):
                    color = image.getpixel((x, y))
                    if color != BLACK:
                        seen_x.add(x)
                        self.assertEqual(color, expected_for_x[x])
        self.assertEqual(seen_x, set(range(9, 32)))

    def test_long_title_frame_budget_covers_one_complete_loop(self) -> None:
        for char in ("I", "W", "M"):
            for length in (13, 30, 60, 120):
                title = char * length
                text_w = preview.text_width(title)
                if text_w <= 23:
                    continue
                period = text_w + 8
                offsets = {
                    _scroll_offset(1000, step * preview.SCROLL_FRAMES_PER_PIXEL) % period
                    for step in range(period)
                }
                self.assertEqual(offsets, set(range(period)))

                self.assertEqual(preview.page_duration_ms(title), 120_000)
                nominal_complete = 1000 + period * 3 * preview.NOMINAL_FRAME_MS
                self.assertLess(nominal_complete, preview.page_duration_ms(title))

    def test_berry_uses_precomputed_columns_and_frame_locked_hot_path(self) -> None:
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        draw = source[source.index("  def draw()") :]
        draw_code = "\n".join(line.split("#", 1)[0] for line in draw.splitlines())

        # The full frame is composed in draw(), while networking, JSON, string
        # parsing, glyph conversion, allocation and RGB interpolation stay out.
        forbidden_tokens = (
            "fraction",
            "inverse",
            "source0",
            "source1",
            "rgb(",
            "http.",
            "json.",
            "string.",
            ".push(",
            "[]",
            "0.014",
            "14.0",
            "self.codes",
        )
        found = [token for token in forbidden_tokens if token in draw_code]
        self.assertEqual(found, [], f"heavy/fractional work remains in draw(): {found}")

        self.assertIn("self.columns[source]", draw_code)
        self.assertIn("self.scroll_column", draw_code)
        self.assertIn("self.scroll_frames", draw_code)
        self.assertRegex(draw_code, r"self\.scroll_frames\s*\+=\s*1")
        self.assertRegex(draw_code, r"self\.scroll_frames\s*>=\s*3")
        self.assertRegex(draw_code, r"self\.scroll_column\s*\+=\s*1")

        # Distance must never be derived from elapsed milliseconds.  Drawing
        # the current column precedes accounting for the just-completed frame.
        self.assertNotRegex(draw_code, r"\(\s*(?:stamp\s*-\s*self\.scroll_start|elapsed)\s*-?\s*1000\s*\)\s*/")
        self.assertNotIn("var whole", draw_code)
        self.assertLess(draw_code.index("self.columns[source]"), draw_code.index("self.scroll_frames += 1"))

    def test_berry_proportional_precompute_and_completion_are_safe(self) -> None:
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertLessEqual(len(source.encode("utf-8")), 16 * 1024)
        self.assertIn("var identity, label, columns, text_w", source)
        self.assertIn("def reset_scroll(stamp)", source)
        self.assertRegex(
            source,
            r"if\s+i\s*<\s*size\(title\)\s*-\s*1\s+next_columns\.push\(0\)\s+end",
        )

        # Short/static and long/scrolling paths use the same precomputed true width.
        self.assertGreaterEqual(source.count("self.text_w"), 6)
        self.assertIn("if self.text_w <= 23", source)
        self.assertRegex(source, r"var\s+origin\s*=\s*9\s*\+\s*\(23\s*-\s*self\.text_w\)\s*/\s*2")

        # Completing the frame-counted source period advances rotation.  A
        # fixed long duration is only a watchdog: the page owns its real dwell,
        # so a title changing from short to long cannot inherit seven seconds.
        self.assertRegex(
            source,
            r"if\s+self\.scroll_column\s*>=\s*self\.text_w\s*\+\s*8[\s\S]{0,180}self\.request_next\(\)",
        )
        duration_start = source.index("  def duration()")
        duration_end = source.index("\n  end", duration_start) + len("\n  end")
        self.assertIn("return 120000", source[duration_start:duration_end])
        self.assertRegex(
            source,
            r"if\s+self\.text_w\s*<=\s*23\s*&&\s*stamp\s*-\s*self\.scroll_start\s*>=\s*7000\s+self\.request_next\(\)",
        )
        self.assertIn("0x595552", source)
        self.assertIn("0xB2ABA4", source)

    def test_berry_packed_font_reconstructs_the_exact_preview_columns(self) -> None:
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        alphabet_match = re.search(r'self\.alphabet\s*=\s*"([^"]*)"', source)
        glyphs_match = re.search(r"self\.glyphs\s*=\s*\[([\s\S]*?)\]", source)
        self.assertIsNotNone(alphabet_match)
        self.assertIsNotNone(glyphs_match)
        alphabet = alphabet_match.group(1)  # type: ignore[union-attr]
        glyphs = tuple(int(value) for value in re.findall(r"\d+", glyphs_match.group(1)))  # type: ignore[union-attr]
        self.assertEqual(len(alphabet), len(glyphs))

        self.assertIn('if char == "M"', source)
        self.assertRegex(
            source,
            r"next_columns\.push\(31\)\s+next_columns\.push\(8\)\s+"
            r"next_columns\.push\(4\)\s+next_columns\.push\(8\)\s+"
            r"next_columns\.push\(31\)",
        )
        self.assertIn('char == "I" || char == "."', source)
        self.assertIn('char == " "', source)
        self.assertIn('char == "," || char == "("', source)
        self.assertIn('char == ")"', source)

        for char, bits in zip(alphabet, glyphs, strict=True):
            if char == "M":
                actual = (31, 8, 4, 8, 31)
            else:
                width, offset = 3, 0
                if char in "I.':!":
                    width, offset = 1, 1
                elif char in " ,(":
                    width = 2
                elif char == ")":
                    width, offset = 2, 1
                actual = tuple(
                    sum(
                        bool(bits & (1 << (14 - row * 3 - offset - column))) << (4 - row)
                        for row in range(5)
                    )
                    for column in range(width)
                )
            self.assertEqual(actual, preview.glyph_columns(char), f"font drift for {char!r}")

    def test_the_app_leaves_the_rotation_when_nothing_is_playing(self) -> None:
        """A media page should appear because there is media.

        Always-on was tried: it kept the app in the loop and drew a record
        labelled IDLE. It never showed a stale track, but it added a page that
        said nothing the silence had not already said, on a rotation that
        already has seven.
        """
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertIn("var ALWAYS_SHOW = false", source)
        # The real gate must still be the freshness test, not the flag.
        gate_start = source.index("  def should_show()")
        gate = source[gate_start : source.index("\n  end", gate_start)]
        self.assertIn("self.playing", gate)
        self.assertIn("now_ms() - self.received", gate)

    def test_rotation_advance_is_one_shot_and_every_reset_rearms_it(self) -> None:
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertIn("advance_sent", source)
        helper_start = source.index("  def request_next()")
        helper_end = source.index("\n  end", helper_start) + len("\n  end")
        helper = source[helper_start:helper_end]
        self.assertIn("!self.advance_sent", helper)
        self.assertIn("self.visible", helper)
        self.assertIn("self.advance_sent = true", helper)
        self.assertIn("rotation.next()", helper)
        self.assertLess(helper.index("self.advance_sent = true"), helper.index("rotation.next()"))

        reset_start = source.index("  def reset_scroll(stamp)")
        reset_end = source.index("\n  end", reset_start) + len("\n  end")
        reset = source[reset_start:reset_end]
        self.assertIn("self.advance_sent = false", reset)
        self.assertIn("self.scroll_column = 0", reset)
        self.assertIn("self.scroll_frames = 0", reset)

        # on_show always starts a fresh dwell; identity/title changes also re-arm.
        on_show_start = source.index("  def on_show()")
        on_show_end = source.index("\n  end", on_show_start) + len("\n  end")
        self.assertIn("self.reset_scroll(", source[on_show_start:on_show_end])
        self.assertRegex(source, r"if\s+is_new\s*\|\|\s*text_changed\s+self\.reset_scroll\(stamp\)\s+end")

        # All paths use the guarded helper; outgoing transition redraws cannot
        # call rotation.next repeatedly and skip following apps.
        self.assertEqual(source.count("rotation.next()"), 1)
        self.assertGreaterEqual(source.count("self.request_next()"), 4)


if __name__ == "__main__":
    unittest.main()
