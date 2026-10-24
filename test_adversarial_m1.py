"""Adversarial stress-test suite and differential verification for Milestone 1.

Verifies:
1. Low-Brightness / 1-Bit Nightkeeper: All non-black foreground text/meter pixels
   have max(r, g, b) >= 174 across render_glance_lane, render_dual_lane,
   render_sparkline, and render_split_lane.
2. Distinctness Oracles: Pairwise distinctness and Hamming distance bounds across
   all glyphs in FONT_3X3 and FONT_3X5.
3. Memory and Frame Geometry: len(frame) == 8 and len(frame[y]) == 32 with zero
   out-of-bounds writes across all functions and boundary conditions.
4. Gutter Column 8: Strictly black (0, 0, 0) across all functions and frames.
"""

from __future__ import annotations

import itertools
import pytest
from typing import Any

import visual_system as vs


# ==============================================================================
# 1. Low-Brightness / 1-Bit Nightkeeper Oracle
# ==============================================================================

class TestLowBrightness1BitNightkeeper:
    """Every foreground pixel carrying information must satisfy max(RGB) >= 174."""

    LEVELS = ("good", "fair", "poor", "bad", "info")
    METERS = (0.0, 0.1, 0.5, 0.9, 1.0, None)

    def test_glance_lane_foreground_pixels_meet_threshold(self) -> None:
        test_values = ["0", "99", "GPU?", "+500", "TEST", "100%"]
        test_labels = [None, "", "CPU", "AQI", "TEMP"]

        for val in test_values:
            for lbl in test_labels:
                for lvl in self.LEVELS:
                    for m in self.METERS:
                        try:
                            frame = vs.render_glance_lane(val, label=lbl, meter=m, level=lvl)
                        except ValueError:
                            continue  # text budget exceeded is expected for some combos

                        for y in range(vs.HEIGHT):
                            for x in range(vs.WIDTH):
                                pixel = frame[y][x]
                                if pixel == vs.BLACK:
                                    continue
                                if y in (1, 2, 3, 4, 5):
                                    # Text band: PEARL or MIST
                                    assert max(pixel) >= vs.LOW_BRIGHTNESS_DOMINANT_MIN, (
                                        f"Glance text pixel {pixel} at ({x}, {y}) for {val!r} "
                                        f"has dominant channel < {vs.LOW_BRIGHTNESS_DOMINANT_MIN}"
                                    )
                                elif y == 7:
                                    # Row 7 rail: its soft start and tinted track may
                                    # fade at brightness 1; the leader may not.
                                    assert vs.rail_survives_brightness_one(frame), (
                                        f"Glance rail for {val!r} loses its leader at brightness 1"
                                    )

    def test_dual_lane_foreground_pixels_meet_threshold(self) -> None:
        pairs = [
            ("C82", "R94"),
            ("1", "2"),
            ("G?", "C+"),
            ("0", "0"),
            ("OK", "WN"),
        ]
        tokens = [vs.PEARL, vs.ICE, vs.MINT, vs.GOLD, vs.AMBER, vs.CORAL, vs.VIOLET, vs.EMERALD]

        for left_val, right_val in pairs:
            for lvl in self.LEVELS:
                for m in (0.0, 0.5, 1.0, None):
                    for color in tokens:
                        frame = vs.render_dual_lane(
                            left_val,
                            right_val,
                            left_color=color,
                            right_color=color,
                            left_meter=m,
                            right_meter=m,
                            left_level=lvl,
                            right_level=lvl,
                        )
                        for y in range(vs.HEIGHT):
                            for x in range(vs.WIDTH):
                                pixel = frame[y][x]
                                if pixel == vs.BLACK:
                                    continue
                                if y in (1, 2, 3, 4, 5):
                                    # Foreground text
                                    assert max(pixel) >= vs.LOW_BRIGHTNESS_DOMINANT_MIN, (
                                        f"Dual text pixel {pixel} at ({x}, {y}) too dark"
                                    )
                                elif y == 7:
                                    # Row 7 meter
                                    if pixel != vs.TRACK:
                                        assert max(pixel) >= vs.LOW_BRIGHTNESS_DOMINANT_MIN, (
                                            f"Dual meter pixel {pixel} at ({x}, {y}) too dark"
                                        )

    def test_sparkline_foreground_pixels_meet_threshold(self) -> None:
        series_cases = [
            [0.0] * 23,
            [1.0] * 23,
            [0.5] * 23,
            [0.1 * i for i in range(10)],
            [0.0, 1.0] * 11,
            [],
        ]
        tokens = [vs.ICE, vs.MINT, vs.GOLD, vs.AMBER, vs.CORAL, vs.PEARL]

        for series in series_cases:
            for lbl in (None, "CPU", "GPU?", "+"):
                for color in tokens:
                    frame = vs.render_sparkline(series, label=lbl, color=color)
                    for y in range(vs.HEIGHT):
                        for x in range(vs.WIDTH):
                            pixel = frame[y][x]
                            if pixel == vs.BLACK or pixel == vs.TRACK:
                                continue
                            # Lit bar or label text
                            assert max(pixel) >= vs.LOW_BRIGHTNESS_DOMINANT_MIN, (
                                f"Sparkline pixel {pixel} at ({x}, {y}) too dark"
                            )

    def test_split_lane_foreground_pixels_meet_threshold(self) -> None:
        pairs = [
            ("12", "34"),
            ("AB", "CD"),
            ("GPU?", "FPS+"),
            ("100%", "98°"),
            ("0", "0"),
        ]
        tokens = [vs.PEARL, vs.ICE, vs.MINT, vs.GOLD, vs.AMBER, vs.CORAL, vs.VIOLET, vs.EMERALD]

        for top, bottom in pairs:
            for lvl in self.LEVELS:
                for m in (0.0, 0.5, 1.0, None):
                    for color in tokens:
                        frame = vs.render_split_lane(
                            top, bottom,
                            top_color=color,
                            bottom_color=color,
                            meter=m,
                            level=lvl,
                        )
                        for y in range(vs.HEIGHT):
                            for x in range(vs.WIDTH):
                                pixel = frame[y][x]
                                if pixel == vs.BLACK or pixel == vs.TRACK:
                                    continue
                                assert max(pixel) >= vs.LOW_BRIGHTNESS_DOMINANT_MIN, (
                                    f"Split lane pixel {pixel} at ({x}, {y}) too dark"
                                )

    def test_all_semantic_tokens_satisfy_low_brightness_rule(self) -> None:
        exempt = {"track", "dim_gray", "black"}
        for name, color in vs.SEMANTIC_TOKENS.items():
            if name in exempt:
                continue
            assert max(color) >= vs.LOW_BRIGHTNESS_DOMINANT_MIN, (
                f"Semantic token {name} ({color}) dominant channel < {vs.LOW_BRIGHTNESS_DOMINANT_MIN}"
            )

    def test_one_bit_quantization_preserves_foreground_content(self) -> None:
        """Differential test with scene_preview.one_bit: foreground survives, track drops."""
        import scene_preview as sp

        # 1. Glance lane
        f_glance = vs.render_glance_lane("99", label="CPU", meter=0.5, level="good")
        ob_glance = sp.one_bit(f_glance)
        assert any(ob_glance[y][x] != (0, 0, 0) for y in (1, 2, 3, 4, 5) for x in range(9, 32))
        assert any(ob_glance[7][x] != (0, 0, 0) for x in range(9, 32))
        assert ob_glance[7][31] == (0, 0, 0)  # track baseline vanishes
        assert all(ob_glance[y][8] == (0, 0, 0) for y in range(8))

        # 2. Dual lane
        f_dual = vs.render_dual_lane("C82", "R94", left_meter=0.8, right_meter=0.2, left_level="good", right_level="bad")
        ob_dual = sp.one_bit(f_dual)
        assert any(ob_dual[y][x] != (0, 0, 0) for y in (1, 2, 3, 4, 5) for x in range(9, 32))
        assert any(ob_dual[7][x] != (0, 0, 0) for x in range(9, 20))
        assert any(ob_dual[7][x] != (0, 0, 0) for x in range(21, 32))
        assert ob_dual[7][20] == (0, 0, 0)  # divider remains black
        assert all(ob_dual[y][8] == (0, 0, 0) for y in range(8))

        # 3. Sparkline
        f_spark = vs.render_sparkline([0.2, 0.5, 0.8, 1.0], label="GPU")
        ob_spark = sp.one_bit(f_spark)
        assert any(ob_spark[y][x] != (0, 0, 0) for y in range(5) for x in range(9, 32))
        assert any(ob_spark[y][x] != (0, 0, 0) for y in (5, 6, 7) for x in range(9, 32))
        assert all(ob_spark[y][8] == (0, 0, 0) for y in range(8))

        # 4. Split lane
        f_split = vs.render_split_lane("GPU?", "FPS+", meter=0.7, level="info")
        ob_split = sp.one_bit(f_split)
        assert any(ob_split[y][x] != (0, 0, 0) for y in (0, 1, 2) for x in range(9, 32))
        assert all(ob_split[3][x] == (0, 0, 0) for x in range(9, 32))  # row 3 blank
        assert any(ob_split[y][x] != (0, 0, 0) for y in (4, 5, 6) for x in range(9, 32))
        assert any(ob_split[7][x] != (0, 0, 0) for x in range(9, 32))
        assert all(ob_split[y][8] == (0, 0, 0) for y in range(8))



# ==============================================================================
# 2. Distinctness Oracles (FONT_3X3 and FONT_3X5)
# ==============================================================================

class TestDistinctnessOracles:
    """Ensure no two glyphs share a bitmap and confusable characters are visually distinct."""

    def test_font_3x5_zero_pairwise_collisions(self) -> None:
        font = vs.FONT_3X5
        printable = sorted(k for k in font.keys() if k.strip())
        assert len(printable) == 53  # 54 total minus space
        for a, b in itertools.combinations(printable, 2):
            assert font[a] != font[b], f"FONT_3X5 collision: {a!r} == {b!r}"

    def test_font_3x3_zero_pairwise_collisions(self) -> None:
        font = vs.FONT_3X3
        printable = sorted(k for k in font.keys() if k.strip())
        assert len(printable) == 44  # 45 total minus space
        for a, b in itertools.combinations(printable, 2):
            assert font[a] != font[b], f"FONT_3X3 collision: {a!r} == {b!r}"

    def test_font_3x5_confusable_pairs_hamming_distance(self) -> None:
        confusables = [
            ("0", "O", 2),
            ("1", "I", 2),
            ("5", "S", 2),
            ("8", "B", 2),
            ("?", "7", 2),
            ("?", "3", 2),
            ("+", "-", 2),
            ("W", "H", 4),  # W is 5px wide, H is 3px wide
            ("H", "M", 6),  # M is 5px wide
            ("M", "N", 6),
            ("2", "Z", 1),
            ("C", "G", 1),
            ("C", "E", 1),
        ]
        font = vs.FONT_3X5
        for a, b, min_dist in confusables:
            g_a = font[a]
            g_b = font[b]
            assert g_a != g_b, f"FONT_3X5 confusable collision: {a!r} and {b!r}"
            if len(g_a[0]) == len(g_b[0]):
                dist = sum(
                    c1 != c2
                    for r1, r2 in zip(g_a, g_b)
                    for c1, c2 in zip(r1, r2)
                )
                assert dist >= min_dist, (
                    f"FONT_3X5 {a!r} vs {b!r} Hamming distance {dist} < expected {min_dist}"
                )

    def test_font_3x3_confusable_pairs_hamming_distance(self) -> None:
        confusables = [
            ("0", "O", 4),
            ("1", "I", 1),
            ("5", "S", 4),
            ("?", "7", 3),
            ("?", "3", 2),
            ("+", "-", 2),
            ("+", "1", 1),
            ("+", "O", 1),
            ("8", "B", 2),
            ("B", "D", 1),
            ("C", "E", 1),
            ("F", "E", 2),
            ("M", "N", 1),
            ("V", "W", 1),
        ]
        font = vs.FONT_3X3
        for a, b, min_dist in confusables:
            g_a = font[a]
            g_b = font[b]
            assert g_a != g_b, f"FONT_3X3 confusable collision: {a!r} and {b!r}"
            dist = sum(
                c1 != c2
                for r1, r2 in zip(g_a, g_b)
                for c1, c2 in zip(r1, r2)
            )
            assert dist >= min_dist, (
                f"FONT_3X3 {a!r} vs {b!r} Hamming distance {dist} < expected {min_dist}"
            )

    def test_font_glyph_geometry_invariants(self) -> None:
        # FONT_3X3: strictly 3x3 for every glyph
        for char, glyph in vs.FONT_3X3.items():
            assert len(glyph) == 3, f"FONT_3X3 {char!r} height != 3"
            for r in glyph:
                assert len(r) == 3, f"FONT_3X3 {char!r} row length != 3"
                assert set(r).issubset({"0", "1"}), f"FONT_3X3 {char!r} invalid bits: {r}"

        # FONT_3X5: strictly height 5, proportional width
        for char, glyph in vs.FONT_3X5.items():
            assert len(glyph) == 5, f"FONT_3X5 {char!r} height != 5"
            w = len(glyph[0])
            assert 1 <= w <= 5, f"FONT_3X5 {char!r} width out of range: {w}"
            for r in glyph:
                assert len(r) == w, f"FONT_3X5 {char!r} non-uniform row width"
                assert set(r).issubset({"0", "1"}), f"FONT_3X5 {char!r} invalid bits: {r}"

    def test_text_measurement_matches_rendered_width(self) -> None:
        # FONT_3X5
        for char in vs.FONT_3X5:
            f = vs.new_frame()
            w_draw = vs.draw_text(f, char, 9, 1, vs.PEARL)
            w_meas = vs.measure_text(char)
            assert w_draw == w_meas, f"draw_text vs measure_text mismatch for {char!r}"

        # FONT_3X3
        for char in vs.FONT_3X3:
            f = vs.new_frame()
            w_draw = vs.draw_text_3x3(f, char, 9, 0, vs.PEARL)
            w_meas = vs.measure_text_3x3(char)
            assert w_draw == w_meas, f"draw_text_3x3 vs measure_text_3x3 mismatch for {char!r}"


# ==============================================================================
# 3. Memory and Frame Geometry (8x32) & Bounds Oracles
# ==============================================================================

class TestFrameGeometryAndBounds:
    """Assert len(frame) == 8 and len(frame[y]) == 32 across all functions and boundary conditions."""

    def _assert_frame_geometry(self, frame: vs.Frame, context: str) -> None:
        assert isinstance(frame, list), f"{context}: frame is not list"
        assert len(frame) == vs.HEIGHT, f"{context}: len(frame) == {len(frame)} != {vs.HEIGHT}"
        for y in range(vs.HEIGHT):
            assert isinstance(frame[y], list), f"{context}: frame[{y}] is not list"
            assert len(frame[y]) == vs.WIDTH, f"{context}: len(frame[{y}]) == {len(frame[y])} != {vs.WIDTH}"
            for x in range(vs.WIDTH):
                pixel = frame[y][x]
                assert isinstance(pixel, tuple) and len(pixel) == 3, f"{context}: pixel at ({x}, {y}) not RGB tuple"
                assert all(0 <= c <= 255 for c in pixel), f"{context}: invalid RGB channels in {pixel}"

    def test_render_glance_lane_geometry(self) -> None:
        inputs = [
            ("", None, None, "info"),
            ("0", None, 0.0, "good"),
            ("GPU?", "STAT", 1.0, "fair"),
            ("100%", "BAT", 0.5, "poor"),
            ("+500", None, None, "bad"),
        ]
        for val, lbl, m, lvl in inputs:
            frame = vs.render_glance_lane(val, label=lbl, meter=m, level=lvl)
            self._assert_frame_geometry(frame, f"render_glance_lane({val!r})")

    def test_render_dual_lane_geometry(self) -> None:
        inputs = [
            ("", "", None, None),
            ("A", "B", 0.0, 1.0),
            ("C82", "R94", 0.82, 0.94),
            ("0", "0", 0.5, 0.5),
        ]
        for l_val, r_val, lm, rm in inputs:
            frame = vs.render_dual_lane(l_val, r_val, left_meter=lm, right_meter=rm)
            self._assert_frame_geometry(frame, f"render_dual_lane({l_val!r}, {r_val!r})")

    def test_render_sparkline_geometry(self) -> None:
        inputs = [
            ([], None),
            ([0.5], "CPU"),
            ([0.1] * 23, None),
            ([0.9] * 100, "LONGSERIES"),
        ]
        for series, lbl in inputs:
            frame = vs.render_sparkline(series, label=lbl)
            self._assert_frame_geometry(frame, f"render_sparkline(len={len(series)}, label={lbl!r})")

    def test_render_split_lane_geometry(self) -> None:
        inputs = [
            ("", "", None),
            ("12", "34", 0.5),
            ("A", "B", 0.0),
            ("GPU?", "FPS+", 1.0),
        ]
        for top, bot, m in inputs:
            frame = vs.render_split_lane(top, bot, meter=m)
            self._assert_frame_geometry(frame, f"render_split_lane({top!r}, {bot!r})")

    def test_zero_off_panel_writes_stress(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Adversarial stress-test: intercept all put_pixel calls and verify 0 off-panel writes."""
        off_panel: list[tuple[int, int]] = []
        orig_put = vs.put_pixel

        def guarded_put(f: vs.Frame, x: int, y: int, c: vs.RGB) -> None:
            if not (0 <= x < vs.WIDTH and 0 <= y < vs.HEIGHT):
                off_panel.append((x, y))
            orig_put(f, x, y, c)

        monkeypatch.setattr(vs, "put_pixel", guarded_put)

        # 1. Glance lane with maximal text and out-of-range meter
        vs.render_glance_lane("99999", meter=-2.5, level="good")
        vs.render_glance_lane("99999", meter=10.0, level="bad")

        # 2. Dual lane with exact 23px total width
        # measure_text("123456789") -> 9 * 3 + 8 = 35 (too wide)
        # Let's find strings that sum to 23
        # "12" is 3+1+3=7px. "123" is 7+1+3=11px. "1234" is 11+1+3=15px.
        # "12" (7px) and "1234" (15px) -> 22px
        vs.render_dual_lane("12", "1234", left_meter=-0.5, right_meter=1.5)

        # 3. Sparkline with extreme and long sequences
        vs.render_sparkline([-1.0, 0.5, 2.0, 10.0] * 10, label="TEST")

        # 4. Split lane with exact 23px max 3x3 text
        # In 3x3: each char is 3px, gap 1px. 6 chars = 6*3 + 5 = 23px
        split_max = "123456"
        assert vs.measure_text_3x3(split_max) == 23
        vs.render_split_lane(split_max, split_max, meter=-1.0)
        vs.render_split_lane(split_max, split_max, meter=2.0)

        assert off_panel == [], f"Off-panel writes detected: {off_panel}"

    def test_extreme_and_infinite_numeric_inputs(self) -> None:
        """Verify resilience against float('inf') and float('-inf') in meters and sparkline data."""
        # 1. Sparkline with +/- inf
        f_spark = vs.render_sparkline([float("-inf"), float("inf"), 0.5])
        self._assert_frame_geometry(f_spark, "sparkline +/- inf")

        # 2. Glance lane with +/- inf
        f_glance_pos = vs.render_glance_lane("OK", meter=float("inf"))
        self._assert_frame_geometry(f_glance_pos, "glance lane +inf")
        f_glance_neg = vs.render_glance_lane("OK", meter=float("-inf"))
        self._assert_frame_geometry(f_glance_neg, "glance lane -inf")

        # 3. Dual lane with +/- inf
        f_dual = vs.render_dual_lane("A", "B", left_meter=float("-inf"), right_meter=float("inf"))
        self._assert_frame_geometry(f_dual, "dual lane inf meters")

        # 4. Split lane with +/- inf
        f_split = vs.render_split_lane("A", "B", meter=float("inf"))
        self._assert_frame_geometry(f_split, "split lane +inf meter")



# ==============================================================================
# 4. Gutter Column 8 Safety Guard Oracle
# ==============================================================================

class TestGutterSafety:
    """Column 8 (GUTTER_X = 8) must remain strictly (0, 0, 0) across all functions."""

    def _assert_gutter_clean(self, frame: vs.Frame, context: str) -> None:
        for y in range(vs.HEIGHT):
            assert frame[y][vs.GUTTER_X] == vs.BLACK, (
                f"{context}: Column {vs.GUTTER_X} violated at y={y}! Got {frame[y][vs.GUTTER_X]}"
            )

    def test_gutter_glance_lane(self) -> None:
        for val in ["", "0", "GPU?", "100%", "TEST"]:
            for m in (None, 0.0, 0.5, 1.0):
                frame = vs.render_glance_lane(val, meter=m)
                self._assert_gutter_clean(frame, f"glance_lane({val!r}, meter={m})")

    def test_gutter_dual_lane(self) -> None:
        for l_val, r_val in [("", ""), ("C82", "R94"), ("12", "1234")]:
            for m in (None, 0.0, 1.0):
                frame = vs.render_dual_lane(l_val, r_val, left_meter=m, right_meter=m)
                self._assert_gutter_clean(frame, f"dual_lane({l_val!r}, {r_val!r})")

    def test_gutter_sparkline(self) -> None:
        for series in [[], [0.5], [1.0] * 23, [0.0] * 50]:
            for lbl in (None, "CPU", "LONG"):
                frame = vs.render_sparkline(series, label=lbl)
                self._assert_gutter_clean(frame, f"sparkline(len={len(series)}, label={lbl!r})")

    def test_gutter_split_lane(self) -> None:
        for top, bot in [("", ""), ("12", "34"), ("123456", "123456")]:
            for m in (None, 0.0, 0.5, 1.0):
                frame = vs.render_split_lane(top, bot, meter=m)
                self._assert_gutter_clean(frame, f"split_lane({top!r}, {bot!r})")

    def test_gutter_weather_silhouettes(self) -> None:
        for pat in [vs.SUN_SILHOUETTE, vs.CLOUD_SILHOUETTE, vs.RAIN_SILHOUETTE, vs.SNOW_SILHOUETTE]:
            frame = vs.render_weather_silhouette(pat)
            self._assert_gutter_clean(frame, "weather_silhouette")

    def test_validate_awtrix_payload_gutter_enforcement(self) -> None:
        # Safe payloads
        vs.validate_awtrix_payload({"draw": [["bitmap", 0, 0, 8, 8, "base64"]]})
        vs.validate_awtrix_payload({"draw": [["bitmap", 9, 0, 23, 8, "base64"]]})

        # Overlap column 8 (start at 8)
        with pytest.raises(ValueError, match="overlaps column 8"):
            vs.validate_awtrix_payload({"draw": [["bitmap", 8, 0, 24, 8, "base64"]]})

        # Overlap column 8 (start at 0, width 9 -> touches 8)
        with pytest.raises(ValueError, match="overlaps column 8"):
            vs.validate_awtrix_payload({"draw": [["bitmap", 0, 0, 9, 8, "base64"]]})

        # Overlap column 8 (start at 7, width 3 -> spans 7, 8, 9)
        with pytest.raises(ValueError, match="overlaps column 8"):
            vs.validate_awtrix_payload({"draw": [["bitmap", 7, 0, 3, 8, "base64"]]})
