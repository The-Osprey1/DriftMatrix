"""Unit tests for centralized Gamma 2.2 Visual System & Token Palette."""

from __future__ import annotations

import unittest

import visual_system as vs


class VisualSystemTests(unittest.TestCase):
    def test_semantic_tokens_are_valid_rgb(self) -> None:
        for name, color in vs.SEMANTIC_TOKENS.items():
            with self.subTest(token=name):
                self.assertEqual(len(color), 3)
                for channel in color:
                    self.assertTrue(0 <= channel <= 255)

    def test_gamma_interpolation_is_perceptually_uniform(self) -> None:
        # Midpoint between black and full pearl should be positive and bounded
        mid = vs.gamma_lerp(vs.BLACK, vs.PEARL, 0.5)
        self.assertTrue(all(0 < ch < 255 for ch in mid))
        # Bounds check
        self.assertEqual(vs.gamma_lerp(vs.BLACK, vs.PEARL, 0.0), vs.BLACK)
        self.assertEqual(vs.gamma_lerp(vs.BLACK, vs.PEARL, 1.0), vs.PEARL)

    def test_low_brightness_legibility_guard(self) -> None:
        # Dominant channel >= 174 is safe
        self.assertTrue(vs.is_safe_at_low_brightness(vs.PEARL))
        self.assertTrue(vs.is_safe_at_low_brightness(vs.GOLD))
        # Dim color gets safely boosted
        dim = (50, 60, 100)
        self.assertFalse(vs.is_safe_at_low_brightness(dim))
        boosted = vs.ensure_low_brightness_legibility(dim)
        self.assertTrue(vs.is_safe_at_low_brightness(boosted))
        self.assertEqual(max(boosted), vs.LOW_BRIGHTNESS_DOMINANT_MIN)

    def test_proportional_typography(self) -> None:
        # "1" and "I" are both 3px and must stay distinct glyphs.  A 1px "1"
        # was identical to "I" and read as a stray rule between 3px digits.
        self.assertEqual(vs.measure_text("1"), 3)
        # "M" is 5px, not 3: at 3px it was one pixel from both "H" and "N",
        # and github_sentinel distinguishes repos by a single initial.
        self.assertEqual(vs.measure_text("M"), 5)
        self.assertNotEqual(vs.FONT_3X5["1"], vs.FONT_3X5["I"])
        # "11" should be 3 + 1 (gap) + 3 = 7px
        self.assertEqual(vs.measure_text("11"), 7)
        self.assertEqual(vs.measure_text("?"), 3)
        self.assertEqual(vs.measure_text("+"), 3)
        self.assertEqual(vs.measure_text("GPU?"), 15)
        self.assertNotEqual(vs.FONT_3X5["?"], vs.FONT_3X5["7"])
        self.assertNotEqual(vs.FONT_3X5["?"], vs.FONT_3X5["3"])
        self.assertNotEqual(vs.FONT_3X5["+"], vs.FONT_3X5["-"])

        # Verify GPU? renders visible pixels for the question mark
        frame_gpu = vs.render_glance_lane("GPU?")
        q_lit = any(frame_gpu[y][x] != vs.BLACK for y in range(1, 6) for x in range(24, 28))
        self.assertTrue(q_lit, "Question mark in GPU? must render visible pixels")

        frame = vs.new_frame()
        rendered_w = vs.draw_text(frame, "ON AIR", 9, 1, vs.CORAL)
        self.assertLessEqual(rendered_w, vs.LANE_WIDTH)
        self.assertEqual(rendered_w, vs.measure_text("ON AIR"))

    def test_micro_typography_3x3(self) -> None:
        self.assertEqual(vs.measure_text_3x3("1"), 3)
        self.assertEqual(vs.measure_text_3x3("12"), 7)
        self.assertIn("0", vs.FONT_3X3)
        self.assertIn("O", vs.FONT_3X3)
        self.assertNotEqual(vs.FONT_3X3["0"], vs.FONT_3X3["O"])
        self.assertNotEqual(vs.FONT_3X3["1"], vs.FONT_3X3["I"])
        self.assertNotEqual(vs.FONT_3X3["5"], vs.FONT_3X3["S"])
        frame = vs.new_frame()
        w = vs.draw_text_3x3(frame, "TEST", 9, 0, vs.PEARL)
        self.assertEqual(w, vs.measure_text_3x3("TEST"))

    def test_weather_silhouettes_keep_negative_space_and_high_contrast(self) -> None:
        patterns = [
            vs.SUN_SILHOUETTE,
            vs.CLOUD_SILHOUETTE,
            vs.RAIN_SILHOUETTE,
            vs.SNOW_SILHOUETTE,
        ]
        for pattern in patterns:
            frame = vs.render_weather_silhouette(pattern)
            # Column 8 must remain black safety gutter
            for y in range(vs.HEIGHT):
                self.assertEqual(frame[y][vs.GUTTER_X], vs.BLACK)
            # Every lit pixel must be safe at low brightness
            for y in range(8):
                for x in range(8):
                    pixel = frame[y][x]
                    self.assertTrue(vs.is_safe_at_low_brightness(pixel))

    def test_payload_validator_enforces_safety_budget_and_gutter(self) -> None:
        valid_payload = {
            "icon": "AAAA",
            "draw": [["bitmap", 9, 0, 23, 8, "base64data"]],
            "backgroundColor": "#000000",
        }
        vs.validate_awtrix_payload(valid_payload)

        # Gutter violation: bitmap overlaps column 8
        bad_gutter = {
            "icon": "AAAA",
            "draw": [["bitmap", 8, 0, 24, 8, "base64data"]],
        }
        with self.assertRaisesRegex(ValueError, "safety gutter"):
            vs.validate_awtrix_payload(bad_gutter)


if __name__ == "__main__":
    unittest.main()
