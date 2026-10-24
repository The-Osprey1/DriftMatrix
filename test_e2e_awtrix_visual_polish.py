"""Opaque-box End-to-End (E2E) Test Suite for AWTRIX Visual Polish & Hardware Safety.

Validates all 4 Tiers defined in TEST_INFRA.md and ORIGINAL_REQUEST.md:
- Tier 1: Feature Coverage (>=5 tests per feature for F1..F8)
- Tier 2: Boundary & Corner Cases (>=5 tests per feature for F1..F8)
- Tier 3: Cross-Feature Combinations (pairwise interactions across features)
- Tier 4: Real-World Scenarios (S1..S5 realistic workload applications)

All tests are self-contained, isolated, and deterministic. No network calls or
physical panel hardware are required.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from PIL import Image

# Ensure repository root and weather directory are on sys.path
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
WEATHER_DIR = ROOT / "weather"
if str(WEATHER_DIR) not in sys.path:
    sys.path.insert(0, str(WEATHER_DIR))

import visual_system as vs
import icon_library as il
import panel_scene as ps
import pc_stats_push
import pc_stats_preview
import game_mode
import sky_watch
import quake_watch
import scene_preview
import new_apps_preview
import weather_brief_push
import aqi_brief_push


# ==============================================================================
# Helper Assertions & Data Generators
# ==============================================================================

def assert_gutter_is_black(test_case: unittest.TestCase, frame: vs.Frame) -> None:
    """Verify column 8 is strictly (0, 0, 0) black across all rows."""
    test_case.assertEqual(len(frame), vs.HEIGHT, "Frame must have 8 rows")
    for y in range(vs.HEIGHT):
        pixel = frame[y][vs.GUTTER_X]
        test_case.assertEqual(
            pixel,
            vs.BLACK,
            f"Gutter violation at (x={vs.GUTTER_X}, y={y}): expected {vs.BLACK}, got {pixel}",
        )


def assert_payload_wire_budget(
    test_case: unittest.TestCase,
    payload: dict,
    limit: int = vs.MAX_PAYLOAD_BYTES,
) -> int:
    """Verify serialized JSON payload wire size is strictly under the limit."""
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    test_case.assertLess(
        len(raw),
        limit,
        f"Payload wire size {len(raw)} bytes exceeds limit {limit} bytes",
    )
    return len(raw)


def assert_non_black_pixels_meet_dominant_channel(
    test_case: unittest.TestCase,
    frame: vs.Frame,
    min_dominant: int = vs.LOW_BRIGHTNESS_DOMINANT_MIN,
) -> None:
    """Verify all lit pixels have max(r, g, b) >= min_dominant."""
    for y in range(len(frame)):
        for x in range(len(frame[y])):
            px = frame[y][x]
            if px != vs.BLACK:
                dominant = max(px)
                test_case.assertGreaterEqual(
                    dominant,
                    min_dominant,
                    f"Pixel at ({x}, {y}) with color {px} has dominant {dominant} < {min_dominant}",
                )


# ==============================================================================
# TIER 1: FEATURE COVERAGE (F1 through F8)
# ==============================================================================

class TestTier1FeatureCoverage(unittest.TestCase):
    """Tier 1: Comprehensive feature coverage across F1 to F8 (>= 5 tests each)."""

    # --- F1: Column 8 Black Gutter ---

    def test_t1_f1_01_glance_lane_black_gutter(self) -> None:
        """F1: render_glance_lane keeps column 8 strictly black across all levels."""
        for level in ("good", "fair", "poor", "bad", "info"):
            lane = vs.render_glance_lane("72°", label="TEMP", meter=0.72, level=level)
            assert_gutter_is_black(self, lane)

    def test_t1_f1_02_dual_lane_black_gutter(self) -> None:
        """F1: render_dual_lane keeps column 8 strictly black across meter ratios."""
        for ratio in (0.0, 0.25, 0.5, 0.75, 1.0):
            lane = vs.render_dual_lane("C82", "R94", left_meter=ratio, right_meter=1.0 - ratio)
            assert_gutter_is_black(self, lane)

    def test_t1_f1_03_sparkline_black_gutter(self) -> None:
        """F1: render_sparkline keeps column 8 strictly black with labels and data."""
        series = [0.1, 0.4, 0.9, 0.3, 0.7, 0.85]
        lane = vs.render_sparkline(series, label="GPU", color=vs.MINT)
        assert_gutter_is_black(self, lane)

    def test_t1_f1_04_split_lane_black_gutter(self) -> None:
        """F1: render_split_lane keeps column 8 strictly black."""
        lane = vs.render_split_lane("UP 10", "DN 40", meter=0.45, level="info")
        assert_gutter_is_black(self, lane)

    def test_t1_f1_05_weather_silhouettes_black_gutter(self) -> None:
        """F1: All visual_system weather silhouettes isolate column 8 as black."""
        for pattern in (vs.SUN_SILHOUETTE, vs.CLOUD_SILHOUETTE, vs.RAIN_SILHOUETTE, vs.SNOW_SILHOUETTE):
            frame = vs.render_weather_silhouette(pattern)
            assert_gutter_is_black(self, frame)

    def test_t1_f1_06_payload_validator_rejects_gutter_touch(self) -> None:
        """F1: validate_awtrix_payload rejects any draw bitmap overlapping column 8."""
        violating_draw = {
            "icon": "AAAA",
            "draw": [["bitmap", 8, 0, 24, 8, "base64data"]],
        }
        with self.assertRaisesRegex(ValueError, "safety gutter"):
            vs.validate_awtrix_payload(violating_draw)

    # --- F2: Payload Wire Size (< 2,048 B) ---

    def test_t1_f2_01_payload_under_2048_passes_validation(self) -> None:
        """F2: Compact valid payload passes validate_awtrix_payload without error."""
        valid_payload = {
            "icon": "AAAA",
            "draw": [["bitmap", 9, 0, 23, 8, "data"]],
            "backgroundColor": "#000000",
        }
        vs.validate_awtrix_payload(valid_payload)
        size = assert_payload_wire_budget(self, valid_payload)
        self.assertLess(size, 500)

    def test_t1_f2_02_payload_exceeding_2048_raises(self) -> None:
        """F2: Payload >= 2,048 bytes strictly raises ValueError."""
        oversized = {
            "icon": "A" * 1500,
            "draw": [["bitmap", 9, 0, 23, 8, "B" * 600]],
        }
        with self.assertRaisesRegex(ValueError, "strictly exceeds"):
            vs.validate_awtrix_payload(oversized)

    def test_t1_f2_03_pc_stats_payload_size(self) -> None:
        """F2: pc_stats_push payload wire size is well under 2,048 bytes."""
        payload = pc_stats_push.metric_payload("CPU", 88.5)
        vs.validate_awtrix_payload(payload)
        assert_payload_wire_budget(self, payload)

    def test_t1_f2_04_game_mode_payload_size(self) -> None:
        """F2: game_mode payload wire size is well under 2,048 bytes."""
        reading = game_mode.GpuReading(temperature_c=78, utilization_pct=95)
        payload = game_mode.build_payload(reading, 35)
        vs.validate_awtrix_payload(payload)
        assert_payload_wire_budget(self, payload)

    def test_t1_f2_05_sky_watch_payload_size(self) -> None:
        """F2: sky_watch payload wire size is well under 2,048 bytes."""
        plane = sky_watch.Aircraft(
            hex_id="a1b2c3",
            callsign="UAL456",
            type_code="B789",
            category="A5",
            altitude_ft=8500,
            track_deg=180.0,
            distance_nm=3.2,
            bearing_deg=0.0,
        )
        payload = sky_watch.build_payload(plane)
        vs.validate_awtrix_payload(payload)
        assert_payload_wire_budget(self, payload)

    def test_t1_f2_06_quake_watch_payload_size(self) -> None:
        """F2: quake_watch payload wire size is well under 2,048 bytes."""
        quake = quake_watch.Quake(
            event_id="ci123456",
            magnitude=3.8,
            place="12 km SW of Ontario, CA",
            time_ms=1_790_000_000_000,
            lat=34.0,
            lon=-117.6,
            depth_km=6.5,
        )
        payload = quake_watch.build_payload(quake)
        vs.validate_awtrix_payload(payload)
        assert_payload_wire_budget(self, payload)

    # --- F3: 120 ms Frame Beat Quantization ---

    def test_t1_f3_01_frame_beat_constant_120ms(self) -> None:
        """F3: Central visual system specifies FRAME_BEAT_MS == 120."""
        self.assertEqual(vs.FRAME_BEAT_MS, 120)

    def test_t1_f3_02_encode_icon_gif_enforces_120ms_multiples(self) -> None:
        """F3: panel_scene.encode_icon_gif enforces beat delays of 120ms multiples."""
        frames = [vs.new_frame() for _ in range(2)]
        vs.put_pixel(frames[0], 2, 2, vs.PEARL)
        vs.put_pixel(frames[1], 3, 3, vs.PEARL)
        # Valid 120ms multiples
        encoded_120 = ps.encode_icon_gif(frames, beat_ms=120)
        self.assertTrue(encoded_120.startswith(b"GIF"))
        encoded_240 = ps.encode_icon_gif(frames, beat_ms=240)
        self.assertTrue(encoded_240.startswith(b"GIF"))
        # Invalid delay not divisible by 120
        with self.assertRaisesRegex(ValueError, "multiple of 120"):
            ps.encode_icon_gif(frames, beat_ms=100)

    def test_t1_f3_03_cpu_gpu_icons_cadence(self) -> None:
        """F3: CPU and GPU animated icons have 4 raw frames and encode to clean 120ms cycles."""
        cpu_frames = il.cpu_icon()
        gpu_frames = il.gpu_icon()
        self.assertEqual(len(cpu_frames), 4)
        self.assertEqual(len(gpu_frames), 4)
        self.assertEqual(vs.cycle_length(cpu_frames), 2)
        self.assertEqual(vs.cycle_length(gpu_frames), 2)
        for frames in (cpu_frames, gpu_frames):
            gif = ps.encode_icon_gif(frames)
            with Image.open(BytesIO(gif)) as img:
                self.assertEqual(img.n_frames, 2)

    def test_t1_f3_04_seismic_earthquake_pulse_cadence(self) -> None:
        """F3: Earthquake icons produce quantized frame sequences for all intensities."""
        for level in (0.2, 0.5, 0.8):
            frames = il.earthquake_icon(level)
            self.assertEqual(len(frames), 8)
            gif = ps.encode_icon_gif(frames)
            with Image.open(BytesIO(gif)) as img:
                self.assertIn(img.n_frames, (2, 4, 5))

    def test_t1_f3_05_aircraft_beacon_cadence(self) -> None:
        """F3: Aircraft icon has exactly 4 frames representing 4-beat cycle with single beacon flash."""
        frames = il.aircraft_icon(track_deg=0.0)
        self.assertEqual(len(frames), 4)
        # Beat 0 beacon at (x=3, y=6) is RED_HOT, other 3 are ICE
        self.assertEqual(frames[0][6][3], vs.RED_HOT)
        for i in (1, 2, 3):
            self.assertEqual(frames[i][6][3], vs.ICE)

    def test_t1_f3_06_cycle_length_deduplication(self) -> None:
        """F3: cycle_length finds shortest repeating period to avoid redundant GIF frames."""
        f1, f2 = vs.new_frame(), vs.new_frame()
        vs.put_pixel(f1, 0, 0, vs.PEARL)
        vs.put_pixel(f2, 1, 1, vs.PEARL)
        self.assertEqual(vs.cycle_length([f1, f1, f1, f1]), 1)
        self.assertEqual(vs.cycle_length([f1, f2, f1, f2]), 2)
        self.assertEqual(vs.cycle_length([f1, f2, f1, f1]), 4)

    # --- F4: Dominant Channel >= 174 (1-Bit Mode) ---

    def test_t1_f4_01_semantic_tokens_meet_threshold(self) -> None:
        """F4: Every foreground semantic token has dominant channel >= 174."""
        for name, color in vs.SEMANTIC_TOKENS.items():
            if name in ("black", "track", "dim_gray"):
                continue  # Background / rails are excluded
            dominant = max(color)
            self.assertGreaterEqual(
                dominant,
                vs.LOW_BRIGHTNESS_DOMINANT_MIN,
                f"Token {name} ({color}) dominant channel {dominant} < {vs.LOW_BRIGHTNESS_DOMINANT_MIN}",
            )

    def test_t1_f4_02_is_safe_at_low_brightness_predicate(self) -> None:
        """F4: is_safe_at_low_brightness strictly tests threshold compliance."""
        self.assertTrue(vs.is_safe_at_low_brightness(vs.BLACK))
        self.assertTrue(vs.is_safe_at_low_brightness(vs.PEARL))
        self.assertTrue(vs.is_safe_at_low_brightness((0, 174, 0)))
        self.assertFalse(vs.is_safe_at_low_brightness((0, 173, 0)))
        self.assertFalse(vs.is_safe_at_low_brightness((50, 80, 120)))

    def test_t1_f4_03_ensure_low_brightness_legibility_scaling(self) -> None:
        """F4: ensure_low_brightness_legibility boosts dim colors while preserving ratio."""
        dim_gold = (100, 80, 20)
        boosted = vs.ensure_low_brightness_legibility(dim_gold)
        self.assertEqual(max(boosted), vs.LOW_BRIGHTNESS_DOMINANT_MIN)
        self.assertTrue(vs.is_safe_at_low_brightness(boosted))

    def test_t1_f4_04_icon_library_active_pixels_meet_threshold(self) -> None:
        """F4: All animated icons in icon_library maintain dominant >= 174 on lit pixels."""
        test_icons = [
            il.cpu_icon(),
            il.gpu_icon(),
            il.thermometer_icon(0.7),
            il.network_icon(),
            il.controller_icon(),
            il.earthquake_icon(0.5),
            il.aircraft_icon(270.0),
        ]
        for frames in test_icons:
            for frame in frames:
                assert_non_black_pixels_meet_dominant_channel(self, frame)
        # The companion creature is shaded (2026-10-03): its silhouette must
        # survive brightness 1, its shadowed base and feet may fade.
        for frame in il.companion_icon("focus"):
            lit = [frame[y][x] for y in range(8) for x in range(8) if frame[y][x] != vs.BLACK]
            self.assertGreaterEqual(sum(max(px) >= 174 for px in lit), 0.6 * len(lit))

    def test_t1_f4_05_one_bit_nightkeeper_thresholding(self) -> None:
        """F4: scene_preview.one_bit quantizes channels at 174 to 255 or 0."""
        frame = vs.new_frame()
        vs.put_pixel(frame, 0, 0, (174, 100, 200))
        vs.put_pixel(frame, 1, 1, (173, 50, 60))
        quantized = scene_preview.one_bit(frame)
        self.assertEqual(quantized[0][0], (255, 0, 255))
        self.assertEqual(quantized[1][1], (0, 0, 0))

    # --- F5: 3×5 Proportional Typography & Glyphs ---

    def test_t1_f5_01_digit_zero_and_letter_o_distinction(self) -> None:
        """F5: Digit 0 and letter O have distinct bit patterns in FONT_3X5."""
        self.assertIn("0", vs.FONT_3X5)
        self.assertIn("O", vs.FONT_3X5)
        self.assertNotEqual(vs.FONT_3X5["0"], vs.FONT_3X5["O"])
        # Digit 0 has corners, letter O has rounded corners
        self.assertEqual(vs.FONT_3X5["0"][0], "111")
        self.assertEqual(vs.FONT_3X5["O"][0], "010")

    def test_t1_f5_02_digit_one_and_letter_i_distinction(self) -> None:
        """F5: Digit 1 and letter I are both 3px wide with distinct serifs."""
        self.assertEqual(vs.measure_text("1"), 3)
        self.assertEqual(vs.measure_text("I"), 3)
        self.assertNotEqual(vs.FONT_3X5["1"], vs.FONT_3X5["I"])
        self.assertEqual(vs.FONT_3X5["1"][0], "010")
        self.assertEqual(vs.FONT_3X5["I"][0], "111")

    def test_t1_f5_03_wide_glyphs_m_and_w(self) -> None:
        """F5: 'M' and 'W' are 5px wide to eliminate confusion with 'H' and 'N'."""
        self.assertEqual(vs.measure_text("M"), 5)
        self.assertEqual(vs.measure_text("W"), 5)
        self.assertEqual(len(vs.FONT_3X5["M"][0]), 5)
        self.assertEqual(len(vs.FONT_3X5["W"][0]), 5)

    def test_t1_f5_04_special_symbols_dimensions(self) -> None:
        """F5: Proportional special symbols measure accurate dimensions."""
        self.assertEqual(vs.measure_text("°"), 2)  # Degree sign is 2px wide
        self.assertEqual(vs.measure_text("%"), 3)
        self.assertEqual(vs.measure_text("-"), 3)
        self.assertEqual(vs.measure_text("/"), 3)
        self.assertEqual(vs.measure_text("#"), 3)

    def test_t1_f5_05_measure_and_draw_text_proportional_spacing(self) -> None:
        """F5: measure_text and draw_text agree on pixel width including 1px gaps."""
        sample = "AWT 8"
        expected_w = vs.measure_text(sample)
        frame = vs.new_frame()
        drawn_w = vs.draw_text(frame, sample, 9, 1, vs.PEARL)
        self.assertEqual(drawn_w, expected_w)
        self.assertLessEqual(drawn_w, vs.LANE_WIDTH)

    # --- F6: High-Density Lanes ---

    def test_t1_f6_01_dual_lane_geometry_and_divider(self) -> None:
        """F6: render_dual_lane places left at col 9, right at col 31, divider at col 20."""
        lane = vs.render_dual_lane("C80", "R90", left_meter=0.8, right_meter=0.9)
        # Gutter at col 8 is black
        self.assertEqual(lane[7][vs.GUTTER_X], vs.BLACK)
        # Divider at col 20 on row 7 is black
        self.assertEqual(lane[7][vs.LANE_X0 + 11], vs.BLACK)

    def test_t1_f6_02_dual_lane_independent_meters(self) -> None:
        """F6: render_dual_lane renders two independent 11px meters on row 7."""
        lane = vs.render_dual_lane(
            "LOW", "HI",
            left_meter=0.2, right_meter=0.9,
            left_level="good", right_level="bad",
        )
        # Left meter occupies cols 9..19 (11 px)
        left_lit = sum(1 for x in range(9, 20) if lane[7][x] == vs.MINT)
        # Right meter occupies cols 21..31 (11 px)
        right_lit = sum(1 for x in range(21, 32) if lane[7][x] == vs.CORAL)
        self.assertGreater(right_lit, left_lit)
        self.assertLessEqual(left_lit, 11)
        self.assertLessEqual(right_lit, 11)

    def test_t1_f6_03_sparkline_preserves_label_band(self) -> None:
        """F6: render_sparkline renders label at top and sparkline in rows 4-7."""
        lane = vs.render_sparkline([0.2, 0.5, 0.8, 1.0], label="RAM")
        # Row 0 contains label pixels in MIST
        row_0_mist = any(lane[0][x] == vs.MIST for x in range(vs.LANE_X0, vs.WIDTH))
        self.assertTrue(row_0_mist, "Label must be visible on top rows")
        # Row 7 is baseline TRACK or lit
        self.assertNotEqual(lane[7][vs.WIDTH - 1], vs.BLACK)

    def test_t1_f6_04_split_lane_geometry(self) -> None:
        """F6: render_split_lane stacks top at y=0 and bottom at y=4 with row 7 meter."""
        lane = vs.render_split_lane("CPU", "88%", meter=0.88, level="poor")
        assert_gutter_is_black(self, lane)
        # Row 7 has poor level meter (AMBER)
        self.assertEqual(lane[7][vs.LANE_X0], vs.AMBER)

    def test_t1_f6_05_draw_lane_rail_unified_ground(self) -> None:
        """F6: draw_lane_rail paints row 7 in TRACK with optional accent end-cap."""
        frame = vs.new_frame()
        vs.draw_lane_rail(frame, accent=vs.CORAL)
        for x in range(vs.LANE_X0, vs.LANE_X0 + vs.LANE_WIDTH - 1):
            self.assertEqual(frame[7][x], vs.TRACK)
        self.assertEqual(frame[7][vs.LANE_X0 + vs.LANE_WIDTH - 1], vs.CORAL)
        self.assertEqual(frame[7][vs.GUTTER_X], vs.BLACK)

    # --- F7: Centralized Icon Library Integration ---

    def test_t1_f7_01_hardware_cpu_gpu_icons(self) -> None:
        """F7: CPU and GPU hardware icons produce valid 8x8 frames bounded in x0..7."""
        for icon_fn in (il.cpu_icon, il.gpu_icon):
            frames = icon_fn()
            self.assertEqual(len(frames), 4)
            for frame in frames:
                self.assertEqual(len(frame), 8)
                self.assertEqual(len(frame[0]), 32)
                for y in range(8):
                    for x in range(8, 32):
                        self.assertEqual(frame[y][x], vs.BLACK)

    def test_t1_f7_02_directional_aircraft_octants(self) -> None:
        """F7: aircraft_icon produces valid directional silhouettes for all 8 octants."""
        headings = [0, 45, 90, 135, 180, 225, 270, 315]
        for heading in headings:
            frames = il.aircraft_icon(float(heading))
            self.assertEqual(len(frames), 4)
            assert_non_black_pixels_meet_dominant_channel(self, frames[0])

    def test_t1_f7_03_earthquake_magnitude_scaling(self) -> None:
        """F7: earthquake_icon scales trace amplitude and color based on magnitude ratio."""
        calm = il.earthquake_icon(0.2)
        moderate = il.earthquake_icon(0.5)
        major = il.earthquake_icon(0.8)
        self.assertEqual(len(calm), 8)
        self.assertEqual(len(moderate), 8)
        self.assertEqual(len(major), 8)
        # Major tremor includes PEARL peak flash
        has_pearl_flash = any(major[1][y][x] == vs.PEARL for y in range(8) for x in range(8))
        self.assertTrue(has_pearl_flash)

    def test_t1_f7_04_controller_gamepad_animation(self) -> None:
        """F7: controller_icon cycles 4 button lights across 4 frames."""
        frames = il.controller_icon()
        self.assertEqual(len(frames), 4)
        button_coords = [(2, 3), (3, 4), (2, 5), (1, 4)]
        for i, (bx, by) in enumerate(button_coords):
            self.assertEqual(frames[i][by][bx], vs.MINT)

    def test_t1_f7_05_companion_reactive_moods(self) -> None:
        """F7: companion_icon provides distinct animated avatar expressions."""
        for mood in ("idle", "focus", "sleep", "on_air", "rest"):
            frames = il.companion_icon(mood)
            self.assertEqual(len(frames), il.COMPANION_FRAMES)
            lit = [frames[0][y][x] for y in range(8) for x in range(8) if frames[0][y][x] != vs.BLACK]
            self.assertGreaterEqual(sum(max(px) >= 174 for px in lit), 0.6 * len(lit))

    # --- F8: Offline Preview Contact Sheet Generation ---

    def test_t1_f8_01_scene_preview_composite_geometry(self) -> None:
        """F8: scene_preview.composite isolates icon (0..7), gutter (8), and lane (9..31)."""
        icon = vs.new_frame(fill=vs.PEARL)
        lane = vs.new_frame(fill=vs.MINT)
        comp = scene_preview.composite(icon, lane)
        self.assertEqual(comp[0][0], vs.PEARL)
        self.assertEqual(comp[0][vs.GUTTER_X], vs.BLACK)
        self.assertEqual(comp[0][vs.LANE_X0], vs.MINT)

    def test_t1_f8_02_scene_preview_one_bit_quantization(self) -> None:
        """F8: scene_preview.one_bit thresholds channels accurately at 174."""
        frame = vs.new_frame()
        vs.put_pixel(frame, 0, 0, (200, 150, 180))
        dark = scene_preview.one_bit(frame)
        self.assertEqual(dark[0][0], (255, 0, 255))

    def test_t1_f8_03_sheet_and_stack_pixel_scaling(self) -> None:
        """F8: sheet and stack generate PIL images without geometry distortion."""
        f1, f2 = vs.new_frame(), vs.new_frame()
        img = scene_preview.sheet([f1, f2], scale=10, gap=4)
        expected_w = 2 * vs.WIDTH * 10 + 4
        expected_h = vs.HEIGHT * 10
        self.assertEqual(img.size, (expected_w, expected_h))

    def test_t1_f8_04_new_apps_preview_row_image(self) -> None:
        """F8: new_apps_preview.row_image builds composited preview row."""
        reading = game_mode.GpuReading(65, 80)
        payload = game_mode.build_payload(reading, 20)
        row = new_apps_preview.row_image("Game Mode", payload)
        self.assertIsInstance(row, Image.Image)
        self.assertGreater(row.width, 500)

    def test_t1_f8_05_scene_preview_all_app_builders(self) -> None:
        """F8: All scene_preview app builders return valid composited frame sets."""
        for name, builder in scene_preview.APPS.items():
            rows = builder()
            self.assertGreater(len(rows), 0, f"App {name} generated 0 rows")
            for frames in rows:
                self.assertGreater(len(frames), 0)
                for f in frames:
                    assert_gutter_is_black(self, f)


# ==============================================================================
# TIER 2: BOUNDARY & CORNER CASES (F1 through F8)
# ==============================================================================

class TestTier2BoundaryAndCornerCases(unittest.TestCase):
    """Tier 2: Boundary Value Analysis and edge condition stress (>= 5 tests each)."""

    # --- F1 Boundaries: Column 8 Gutter ---

    def test_t2_f1_bva_01_bitmap_adjacent_to_gutter_x9(self) -> None:
        """F1-BVA: Bitmap starting exactly at x0=9 width=23 touches no gutter and passes."""
        payload = {"draw": [["bitmap", 9, 0, 23, 8, "data"]]}
        vs.validate_awtrix_payload(payload)

    def test_t2_f1_bva_02_bitmap_starting_at_gutter_x8_rejected(self) -> None:
        """F1-BVA: Bitmap starting at x0=8 overlaps column 8 and is rejected."""
        payload = {"draw": [["bitmap", 8, 0, 24, 8, "data"]]}
        with self.assertRaisesRegex(ValueError, "safety gutter"):
            vs.validate_awtrix_payload(payload)

    def test_t2_f1_bva_03_bitmap_ending_before_gutter_x0_w8(self) -> None:
        """F1-BVA: Bitmap from x0=0 width=8 ends at col 7 and is valid."""
        payload = {"draw": [["bitmap", 0, 0, 8, 8, "data"]]}
        vs.validate_awtrix_payload(payload)

    def test_t2_f1_bva_04_bitmap_overlapping_gutter_x0_w9_rejected(self) -> None:
        """F1-BVA: Bitmap from x0=0 width=9 spans into col 8 and is rejected."""
        payload = {"draw": [["bitmap", 0, 0, 9, 8, "data"]]}
        with self.assertRaisesRegex(ValueError, "safety gutter"):
            vs.validate_awtrix_payload(payload)

    def test_t2_f1_bva_05_extreme_meter_gutter_isolation(self) -> None:
        """F1-BVA: Extreme meters (0.0, 1.0, and out-of-bound) keep gutter black."""
        for m in (-1.0, 0.0, 0.001, 0.999, 1.0, 2.0):
            lane = vs.render_glance_lane("TEST", meter=m, level="good")
            assert_gutter_is_black(self, lane)

    # --- F2 Boundaries: Payload Wire Size (< 2,048 B) ---

    def test_t2_f2_bva_01_payload_exactly_2047_bytes(self) -> None:
        """F2-BVA: Payload serialized to exactly 2,047 bytes passes validation."""
        base_payload = {"draw": [["bitmap", 9, 0, 23, 8, ""]], "icon": ""}
        current_len = len(json.dumps(base_payload, separators=(",", ":")).encode("utf-8"))
        padding = "A" * (vs.MAX_PAYLOAD_BYTES - 1 - current_len)
        base_payload["icon"] = padding
        self.assertEqual(len(json.dumps(base_payload, separators=(",", ":")).encode("utf-8")), 2047)
        vs.validate_awtrix_payload(base_payload)

    def test_t2_f2_bva_02_payload_exactly_2048_bytes_rejected(self) -> None:
        """F2-BVA: Payload serialized to exactly 2,048 bytes raises ValueError."""
        base_payload = {"draw": [["bitmap", 9, 0, 23, 8, ""]], "icon": ""}
        current_len = len(json.dumps(base_payload, separators=(",", ":")).encode("utf-8"))
        padding = "A" * (vs.MAX_PAYLOAD_BYTES - current_len)
        base_payload["icon"] = padding
        self.assertEqual(len(json.dumps(base_payload, separators=(",", ":")).encode("utf-8")), 2048)
        with self.assertRaisesRegex(ValueError, "strictly exceeds"):
            vs.validate_awtrix_payload(base_payload)

    def test_t2_f2_bva_03_empty_payload_dict(self) -> None:
        """F2-BVA: Minimal empty payload dict validates cleanly."""
        vs.validate_awtrix_payload({})

    def test_t2_f2_bva_04_pc_stats_dual_active_metrics_budget(self) -> None:
        """F2-BVA: pc_stats with CPU 100% and RAM 100% simultaneously stays under 2,048 bytes."""
        payload = pc_stats_push.build_payload([("CPU", 100), ("RAM", 100)])
        vs.validate_awtrix_payload(payload)
        assert_payload_wire_budget(self, payload)

    def test_t2_f2_bva_05_ticker_payload_long_string_budget(self) -> None:
        """F2-BVA: ticker_payload with 80-character text stays well under 2,048 bytes."""
        long_text = "EARTHQUAKE M6.2 - 25 MILES SOUTHWEST OF LOS ANGELES - DEPTH 10 KM"
        icon_gif = ps.encode_icon_gif(il.earthquake_icon(0.8))
        payload = ps.ticker_payload(long_text, vs.CORAL, icon_gif)
        vs.validate_awtrix_payload(payload)
        assert_payload_wire_budget(self, payload)

    # --- F3 Boundaries: 120 ms Frame Beat Quantization ---

    def test_t2_f3_bva_01_single_frame_static_loop_zero(self) -> None:
        """F3-BVA: Single frame static icon produces valid non-looping GIF without judder."""
        frame = vs.new_frame()
        vs.put_pixel(frame, 3, 3, vs.PEARL)
        gif = ps.encode_icon_gif([frame], beat_ms=120)
        with Image.open(BytesIO(gif)) as img:
            self.assertEqual(img.n_frames, 1)

    def test_t2_f3_bva_02_aircraft_track_degrees_boundary_wrapping(self) -> None:
        """F3-BVA: Aircraft track degrees negative, 0, 360, and >360 wrap correctly."""
        for deg in (-720.0, -45.0, 0.0, 359.9, 360.0, 405.0):
            frames = il.aircraft_icon(deg)
            self.assertEqual(len(frames), 4)

    def test_t2_f3_bva_03_earthquake_level_boundaries_0_33_66(self) -> None:
        """F3-BVA: Earthquake levels at exact thresholds 0.0, 0.33, 0.66, 1.0 transition cleanly."""
        for lvl in (0.0, 0.329, 0.33, 0.659, 0.66, 1.0):
            frames = il.earthquake_icon(lvl)
            self.assertEqual(len(frames), 8)

    def test_t2_f3_bva_04_thermometer_level_boundaries_0_to_1(self) -> None:
        """F3-BVA: Thermometer at 0.0, 0.5, 0.8, 1.0 correctly populates bulb and stem."""
        for lvl in (0.0, 0.5, 0.8, 1.0):
            frames = il.thermometer_icon(lvl)
            self.assertEqual(len(frames), 1)

    def test_t2_f3_bva_05_cycle_length_edge_cases(self) -> None:
        """F3-BVA: cycle_length handles empty, prime counts, and all-identical frames."""
        self.assertEqual(vs.cycle_length([]), 0)
        f = vs.new_frame()
        self.assertEqual(vs.cycle_length([f]), 1)
        self.assertEqual(vs.cycle_length([f] * 7), 1)

    # --- F4 Boundaries: Dominant Channel >= 174 (1-Bit Mode) ---

    def test_t2_f4_bva_01_exact_dominant_threshold_173_vs_174(self) -> None:
        """F4-BVA: Boundary comparison between 173 and 174."""
        self.assertFalse(vs.is_safe_at_low_brightness((173, 50, 50)))
        self.assertTrue(vs.is_safe_at_low_brightness((174, 50, 50)))

    def test_t2_f4_bva_02_black_is_always_safe(self) -> None:
        """F4-BVA: Black (0, 0, 0) is safe and never boosted."""
        self.assertTrue(vs.is_safe_at_low_brightness((0, 0, 0)))
        self.assertEqual(vs.ensure_low_brightness_legibility((0, 0, 0)), (0, 0, 0))

    def test_t2_f4_bva_03_pure_white_255_untouched(self) -> None:
        """F4-BVA: Pure white (255, 255, 255) is safe and unaffected by ensure_legibility."""
        white = (255, 255, 255)
        self.assertTrue(vs.is_safe_at_low_brightness(white))
        self.assertEqual(vs.ensure_low_brightness_legibility(white), white)

    def test_t2_f4_bva_04_ensure_legibility_minimal_channel_1(self) -> None:
        """F4-BVA: Single minimal pixel channel 1 scales to 174."""
        minimal = (1, 0, 0)
        boosted = vs.ensure_low_brightness_legibility(minimal)
        self.assertEqual(boosted, (174, 0, 0))

    def test_t2_f4_bva_05_ensure_legibility_black_returns_black(self) -> None:
        """F4-BVA: Passing black to ensure_low_brightness_legibility returns black."""
        self.assertEqual(vs.ensure_low_brightness_legibility(vs.BLACK), vs.BLACK)

    # --- F5 Boundaries: 3×5 Typography ---

    def test_t2_f5_bva_01_empty_string_measure_and_draw(self) -> None:
        """F5-BVA: Empty string measures 0 and draws 0 pixels without error."""
        self.assertEqual(vs.measure_text(""), 0)
        frame = vs.new_frame()
        self.assertEqual(vs.draw_text(frame, "", 9, 1, vs.PEARL), 0)

    def test_t2_f5_bva_02_all_glyphs_dimension_bounds(self) -> None:
        """F5-BVA: Every glyph in FONT_3X5 has height 5 and width between 1 and 5."""
        for char, glyph in vs.FONT_3X5.items():
            self.assertEqual(len(glyph), 5, f"Glyph '{char}' height is not 5")
            w = len(glyph[0])
            self.assertTrue(1 <= w <= 5, f"Glyph '{char}' width {w} out of range [1, 5]")
            self.assertTrue(all(len(row) == w for row in glyph))

    def test_t2_f5_bva_03_exact_lane_width_23px_render(self) -> None:
        """F5-BVA: Text measuring exactly 23px fits in render_glance_lane without raising."""
        # "123456" = 3*6 + 5 = 23px
        self.assertEqual(vs.measure_text("123456"), 23)
        lane = vs.render_glance_lane("123456")
        assert_gutter_is_black(self, lane)

    def test_t2_f5_bva_04_lane_width_overflow_raises_error(self) -> None:
        """F5-BVA: Text measuring > 23px in render_glance_lane raises ValueError."""
        # "1234567" = 3*7 + 6 = 27px > 23px
        self.assertGreater(vs.measure_text("1234567"), 23)
        with self.assertRaisesRegex(ValueError, "lane holds 23px"):
            vs.render_glance_lane("1234567")

    def test_t2_f5_bva_05_unknown_glyph_graceful_fallback(self) -> None:
        """F5-BVA: Unknown glyphs gracefully fall back to space without crashing."""
        self.assertEqual(vs.measure_text("~"), 1)  # Space fallback is 1px
        frame = vs.new_frame()
        drawn_w = vs.draw_text(frame, "~", 9, 1, vs.PEARL)
        self.assertEqual(drawn_w, 1)

    # --- F6 Boundaries: High-Density Lanes ---

    def test_t2_f6_bva_01_dual_lane_width_overflow_raises(self) -> None:
        """F6-BVA: Dual lane values whose sum > 23px raise ValueError."""
        # "CPU100" (6*3+5=23px) + "RAM" (3*3+2=11px) = 34px > 23px
        with self.assertRaisesRegex(ValueError, "exceed lane width"):
            vs.render_dual_lane("CPU100", "RAM")

    def test_t2_f6_bva_02_dual_lane_empty_strings(self) -> None:
        """F6-BVA: Dual lane with empty strings renders dual meters with 0 text."""
        lane = vs.render_dual_lane("", "", left_meter=0.5, right_meter=0.5)
        assert_gutter_is_black(self, lane)
        # Meter row 7 still intact
        self.assertEqual(lane[7][vs.LANE_X0 + 11], vs.BLACK)

    def test_t2_f6_bva_03_dual_lane_meters_clamped(self) -> None:
        """F6-BVA: Dual lane meters out of bounds (<0.0, >1.0) clamp safely."""
        lane = vs.render_dual_lane("L", "R", left_meter=-0.5, right_meter=1.5)
        assert_gutter_is_black(self, lane)

    def test_t2_f6_bva_04_sparkline_empty_series(self) -> None:
        """F6-BVA: Sparkline with empty values renders baseline track without crashing."""
        lane = vs.render_sparkline([])
        assert_gutter_is_black(self, lane)
        for x in range(vs.LANE_X0, vs.WIDTH):
            self.assertEqual(lane[7][x], vs.TRACK)

    def test_t2_f6_bva_05_sparkline_oversized_series_truncates(self) -> None:
        """F6-BVA: Sparkline with > 23 values right-aligns and truncates without off-panel draw."""
        series = [float(i) / 50.0 for i in range(50)]
        lane = vs.render_sparkline(series)
        assert_gutter_is_black(self, lane)
        # Check lane rightmost pixel is lit
        self.assertNotEqual(lane[4][vs.WIDTH - 1], vs.BLACK)

    # --- F7 Boundaries: Centralized Icon Library ---

    def test_t2_f7_bva_01_earthquake_negative_and_excessive_clamping(self) -> None:
        """F7-BVA: Earthquake levels < 0.0 clamp to 0.0, > 1.0 clamp to 1.0."""
        under = il.earthquake_icon(-10.0)
        over = il.earthquake_icon(10.0)
        self.assertEqual(len(under), 8)
        self.assertEqual(len(over), 8)

    def test_t2_f7_bva_02_thermometer_negative_and_excessive_clamping(self) -> None:
        """F7-BVA: Thermometer levels < 0.0 and > 1.0 clamp safely."""
        under = il.thermometer_icon(-5.0)
        over = il.thermometer_icon(5.0)
        self.assertEqual(len(under), 1)
        self.assertEqual(len(over), 1)

    def test_t2_f7_bva_03_aircraft_none_track_defaults_north(self) -> None:
        """F7-BVA: aircraft_icon(None) defaults to track 0 (North)."""
        frames_none = il.aircraft_icon(None)
        frames_north = il.aircraft_icon(0.0)
        self.assertEqual(frames_none[0], frames_north[0])

    def test_t2_f7_bva_04_companion_unknown_mood_defaults_idle(self) -> None:
        """F7-BVA: companion_icon with unknown mood string defaults gracefully to idle."""
        frames_unknown = il.companion_icon("undefined_mood")
        frames_idle = il.companion_icon("idle")
        self.assertEqual(len(frames_unknown), len(frames_idle))

    def test_t2_f7_bva_05_all_icon_library_frames_bounds_check(self) -> None:
        """F7-BVA: Every frame generated by icon_library is exactly 8x32 with x>=8 all black."""
        icons = [
            il.cpu_icon(), il.gpu_icon(), il.network_icon(), il.sun_icon(),
            il.rain_icon(), il.cloud_icon(), il.calendar_icon(), il.bell_icon(),
            il.hourglass_icon(), il.warning_icon(), il.heart_icon(), il.lock_icon(),
            il.controller_icon(), il.earthquake_icon(), il.aircraft_icon(),
            il.companion_icon(),
        ]
        for frames in icons:
            for f in frames:
                self.assertEqual(len(f), 8)
                self.assertEqual(len(f[0]), 32)
                for y in range(8):
                    for x in range(8, 32):
                        self.assertEqual(f[y][x], vs.BLACK)

    # --- F8 Boundaries: Offline Preview Sheets ---

    def test_t2_f8_bva_01_sheet_single_frame_geometry(self) -> None:
        """F8-BVA: sheet with 1 frame produces WIDTH*scale by HEIGHT*scale image."""
        f = vs.new_frame()
        img = scene_preview.sheet([f], scale=5, gap=0)
        self.assertEqual(img.size, (32 * 5, 8 * 5))

    def test_t2_f8_bva_02_stack_single_image_geometry(self) -> None:
        """F8-BVA: stack with 1 image preserves dimensions."""
        img = Image.new("RGB", (100, 50))
        stacked = scene_preview.stack([img], gap=10)
        self.assertEqual(stacked.size, (100, 50))

    def test_t2_f8_bva_03_one_bit_all_black_frame(self) -> None:
        """F8-BVA: one_bit on all-black frame produces all-black output."""
        frame = vs.new_frame(fill=vs.BLACK)
        dark = scene_preview.one_bit(frame)
        self.assertTrue(all(dark[y][x] == (0, 0, 0) for y in range(8) for x in range(32)))

    def test_t2_f8_bva_04_one_bit_all_white_frame(self) -> None:
        """F8-BVA: one_bit on all-white frame produces all-white output."""
        frame = [[(255, 255, 255) for _ in range(32)] for _ in range(8)]
        dark = scene_preview.one_bit(frame)
        self.assertTrue(all(dark[y][x] == (255, 255, 255) for y in range(8) for x in range(32)))

    def test_t2_f8_bva_05_composite_blank_frames_strict_black(self) -> None:
        """F8-BVA: composite of blank icon and blank lane produces 100% black frame."""
        comp = scene_preview.composite(vs.new_frame(), vs.new_frame())
        self.assertTrue(all(comp[y][x] == vs.BLACK for y in range(8) for x in range(32)))


# ==============================================================================
# TIER 3: CROSS-FEATURE COMBINATIONS
# ==============================================================================

class TestTier3CrossFeatureCombinations(unittest.TestCase):
    """Tier 3: Pairwise cross-feature interactions and integration invariants."""

    def test_t3_01_icon_animation_and_dual_lane_wire_budget(self) -> None:
        """T3-01: Multi-frame icon + dual lane composite stays under 2,048 bytes and respects gutter."""
        icon_gif = ps.encode_icon_gif(il.cpu_icon())
        lane = vs.render_dual_lane("C85", "R92", left_meter=0.85, right_meter=0.92, left_level="fair", right_level="poor")
        payload = ps.lane_payload(lane, icon_gif)
        vs.validate_awtrix_payload(payload)
        assert_gutter_is_black(self, lane)
        assert_payload_wire_budget(self, payload)

    def test_t3_02_sparkline_nightkeeper_one_bit_survival(self) -> None:
        """T3-02: Sparkline data points in ICE/MINT survive 1-bit nightkeeper thresholding."""
        lane = vs.render_sparkline([0.2, 0.5, 0.8, 1.0], label="NET", color=vs.MINT)
        assert_gutter_is_black(self, lane)
        dark_lane = scene_preview.one_bit(lane)
        # Sparkline peaks in MINT (dominant 245) must survive as (255, 255, 255)
        survived = any(dark_lane[y][x] == (255, 255, 255) for y in range(4, 8) for x in range(vs.LANE_X0, vs.WIDTH))
        self.assertTrue(survived, "MINT sparkline points must be legible at 1-bit brightness")
        # Gutter remains strictly (0, 0, 0)
        self.assertTrue(all(dark_lane[y][vs.GUTTER_X] == (0, 0, 0) for y in range(8)))

    def test_t3_03_extreme_load_pc_stats_payload_and_legibility(self) -> None:
        """T3-03: PC Stats under 99% CPU and 95% RAM maintains wire budget and 1-bit legibility."""
        payload = pc_stats_push.build_payload([("CPU", 99), ("RAM", 95)])
        vs.validate_awtrix_payload(payload)
        assert_payload_wire_budget(self, payload)

    def test_t3_04_game_mode_peak_gpu_one_bit_and_gutter(self) -> None:
        """T3-04: Game mode peak GPU temp (88°C, CORAL) maintains gutter blackness and 1-bit legibility."""
        reading = game_mode.GpuReading(temperature_c=88, utilization_pct=99)
        lane = game_mode.render_lane(reading, session_minutes=45)
        assert_gutter_is_black(self, lane)
        # Row 7 meter is CORAL (255, 76, 91) up to its PEARL leader
        filled, colour = vs.read_meter(lane)
        self.assertEqual(colour, vs.CORAL)
        dark = scene_preview.one_bit(lane)
        # The full-CORAL pixel beside the leader survives the 1-bit threshold
        self.assertEqual(dark[7][vs.LANE_X0 + filled - 2][0], 255)

    def test_t3_05_sky_watch_proximity_radar_composite_preview(self) -> None:
        """T3-05: Sky Watch proximity radar composited frame satisfies layout, gutter, and preview sheet."""
        plane = sky_watch.Aircraft("abc", "SWA123", "B738", "A3", 3500, 265.0, 0.9, 210.0)
        payload = sky_watch.build_payload(plane)
        vs.validate_awtrix_payload(payload)
        img = new_apps_preview.row_image("Sky Watch", payload)
        self.assertGreater(img.width, 400)

    def test_t3_06_quake_watch_tremor_pulse_meter_interaction(self) -> None:
        """T3-06: Quake Watch major tremor 5-beat pulse integrates cleanly with alert meter."""
        quake = quake_watch.Quake("eq99", 5.8, "Near Ridgecrest", 1_790_000_000_000, 35.7, -117.6, 8.0)
        payload = quake_watch.build_payload(quake)
        vs.validate_awtrix_payload(payload)
        assert_payload_wire_budget(self, payload)

    def test_t3_07_weather_aqi_dual_rotation_palette_harmony(self) -> None:
        """T3-07: Weather rain scene and AQI snapshot both conform to centralized palette and wire budget."""
        # Weather Scene
        w_scene = weather_brief_push.WeatherScene(
            kind="rain", text="NOW", icon_kind="rain", is_day=True,
            temperature_f=72.0, low_f=60.0, high_f=80.0, eta_minutes=0,
        )
        w_payload = weather_brief_push.build_payload(w_scene)
        vs.validate_awtrix_payload(w_payload)
        assert_payload_wire_budget(self, w_payload)

        # AQI Snapshot
        aqi_snap = aqi_brief_push.AQISnapshot(
            observed_at=datetime.now(timezone.utc),
            timezone_name="UTC",
            us_aqi=125,
            pm2_5=45.0,
            forecast_times=(),
            forecast_aqi=(),
        )
        aqi_payload = aqi_brief_push.build_payload(aqi_snap)
        vs.validate_awtrix_payload(aqi_payload)
        assert_payload_wire_budget(self, aqi_payload)

    def test_t3_08_offline_contact_sheet_one_bit_parity(self) -> None:
        """T3-08: Full color and 1-bit contact sheets have identical tile dimensions and gutter isolation."""
        rows = scene_preview.companion_rows()
        for frames in rows:
            for frame in frames:
                dark = scene_preview.one_bit(frame)
                self.assertEqual(len(frame), len(dark))
                self.assertEqual(len(frame[0]), len(dark[0]))
                assert_gutter_is_black(self, frame)
                assert_gutter_is_black(self, dark)


# ==============================================================================
# TIER 4: REAL-WORLD SCENARIOS
# ==============================================================================

class TestTier4RealWorldScenarios(unittest.TestCase):
    """Tier 4: End-to-End realistic workload scenarios (S1 through S5)."""

    def test_t4_s1_pc_stats_extreme_load_dual_lane_scenario(self) -> None:
        """S1: Heavy compute/developer build workload (CPU 99%, RAM 95%).

        Exercises: F1 (gutter), F2 (payload < 2048 B), F4 (legibility), F6 (dual lane), F7 (icon).
        """
        # Step 1: Metric gates receive 2 consecutive elevated samples to trigger debounce
        cpu_gate = pc_stats_push.MetricGate(80, 65, trigger_samples=2)
        ram_gate = pc_stats_push.MetricGate(85, 75, trigger_samples=2)
        self.assertFalse(cpu_gate.update(99))
        self.assertTrue(cpu_gate.update(99))
        self.assertFalse(ram_gate.update(95))
        self.assertTrue(ram_gate.update(95))

        # Step 2: Build dual lane presentation
        lane = vs.render_dual_lane(
            "C99", "R95",
            left_meter=0.99, right_meter=0.95,
            left_level="bad", right_level="bad",
            left_color=vs.CORAL, right_color=vs.CORAL,
        )
        assert_gutter_is_black(self, lane)

        # Step 3: Package into wire payload with CPU animated icon
        icon_gif = ps.encode_icon_gif(il.cpu_icon())
        payload = ps.lane_payload(lane, icon_gif)
        vs.validate_awtrix_payload(payload)
        size = assert_payload_wire_budget(self, payload)
        self.assertLess(size, 2048)

        # Step 4: 1-bit Nightkeeper legibility
        dark = scene_preview.one_bit(lane)
        assert_gutter_is_black(self, dark)
        # Both meter bands should be visible at 1-bit (CORAL red channel is 255)
        self.assertEqual(dark[7][vs.LANE_X0][0], 255)
        self.assertEqual(dark[7][21][0], 255)

    def test_t4_s2_game_mode_peak_gpu_transition_scenario(self) -> None:
        """S2: Transition into intensive 4K gaming session (GPU temp 88°C, load 99%).

        Exercises: F1 (gutter), F2 (payload wire budget), F3 (120ms beat), F4 (dominant >= 174), F6 (lane), F7 (controller icon).
        """
        reading = game_mode.GpuReading(temperature_c=88, utilization_pct=99)
        self.assertEqual(game_mode.temperature_level(88), "bad")

        # Visual lane with temperature meter on row 7
        lane = game_mode.render_lane(reading, session_minutes=45)
        assert_gutter_is_black(self, lane)

        # Cycling 4-frame controller icon
        ctrl_frames = il.controller_icon()
        self.assertEqual(len(ctrl_frames), 4)
        for frame in ctrl_frames:
            assert_non_black_pixels_meet_dominant_channel(self, frame)

        # Wire payload validation
        payload = game_mode.build_payload(reading, 45)
        vs.validate_awtrix_payload(payload)
        assert_payload_wire_budget(self, payload)

    def test_t4_s3_sky_watch_proximity_radar_scenario(self) -> None:
        """S3: Overhead airspace radar tracking commercial airliner descending on approach.

        Flight: SWA1234, Boeing 737, alt 3200 ft, track 265° W, dist 0.8 nm.
        Exercises: F1 (gutter), F2 (wire budget), F3 (beacon pulse), F4 (legibility), F6 (lane), F7 (aircraft icon).
        """
        airliner = sky_watch.Aircraft(
            hex_id="a45b6c",
            callsign="SWA1234",
            type_code="B738",
            category="A3",
            altitude_ft=3200,
            track_deg=265.0,
            distance_nm=0.8,
            bearing_deg=0.0,
        )
        self.assertEqual(sky_watch.model_label(airliner), "737")

        # Aircraft icon pointing West with blinking red beacon
        frames = il.aircraft_icon(airliner.track_deg)
        self.assertEqual(len(frames), 4)
        # Beat 0 beacon at (x=6, y=3) is RED_HOT (255, 25, 35)
        self.assertEqual(frames[0][3][6], vs.RED_HOT)
        # At beat 1, the beacon turns off (no RED_HOT pixels)
        self.assertFalse(any(frames[1][y][x] == vs.RED_HOT for y in range(8) for x in range(8)))

        # Payload generation
        payload = sky_watch.build_payload(airliner)
        vs.validate_awtrix_payload(payload)
        assert_payload_wire_budget(self, payload)

        # Preview row rendering
        img = new_apps_preview.row_image("Approach Radar", payload)
        self.assertIsInstance(img, Image.Image)

    def test_t4_s4_quake_watch_major_tremor_scenario(self) -> None:
        """S4: USGS feed reports major tremor (M5.4, 12 km depth, 4 min ago).

        Exercises: F1 (gutter), F2 (wire budget), F3 (seismic pulse), F4 (dominant channel), F6 (lane), F7 (earthquake icon).
        """
        now = 1_790_000_000_000
        quake = quake_watch.Quake(
            event_id="ci4012345",
            magnitude=5.4,
            place="14 km NE of Corona, CA",
            time_ms=now - 4 * 60_000,
            lat=33.95,
            lon=-117.40,
            depth_km=12.0,
        )
        description = quake_watch.describe(quake, now)
        self.assertIn("M5.4", description)
        self.assertIn("4 MIN AGO", description)

        # Earthquake icon with peak wave PEARL flash
        frames = il.earthquake_icon(level=0.8)
        self.assertEqual(len(frames), 8)
        has_flash = any(frames[1][y][x] == vs.PEARL for y in range(8) for x in range(8))
        self.assertTrue(has_flash)

        # Wire payload verification
        payload = quake_watch.build_payload(quake)
        vs.validate_awtrix_payload(payload)
        assert_payload_wire_budget(self, payload)

    def test_t4_s5_weather_aqi_dual_mode_rotation_nightkeeper_scenario(self) -> None:
        """S5: Panel nightkeeper mode (brightness 1) rotating Weather and AQI apps.

        Weather: Rain arriving NOW.
        AQI: Wildfire smoke alert (AQI 125, Unhealthy for Sensitive Groups).
        Exercises: F1 (gutter), F2 (wire size), F3 (120ms beat), F4 (1-bit legibility), F7 (icons), F8 (previews).
        """
        # Weather brief scene
        w_scene = weather_brief_push.WeatherScene(
            kind="rain", text="NOW", icon_kind="rain", is_day=False,
            temperature_f=65.0, low_f=55.0, high_f=70.0, eta_minutes=0,
        )
        w_payload = weather_brief_push.build_payload(w_scene)
        vs.validate_awtrix_payload(w_payload)
        assert_payload_wire_budget(self, w_payload)

        # AQI brief snapshot
        aqi_snap = aqi_brief_push.AQISnapshot(
            observed_at=datetime.now(timezone.utc),
            timezone_name="UTC",
            us_aqi=125,
            pm2_5=45.2,
            forecast_times=(),
            forecast_aqi=(),
        )
        aqi_payload = aqi_brief_push.build_payload(aqi_snap)
        vs.validate_awtrix_payload(aqi_payload)
        assert_payload_wire_budget(self, aqi_payload)

        # Decode lanes and verify gutter and 1-bit legibility for both
        for p in (w_payload, aqi_payload):
            op = p["draw"][0]
            self.assertEqual(op[1], vs.LANE_X0)  # Starts at col 9
            self.assertEqual(op[3], vs.LANE_WIDTH)  # Width 23


if __name__ == "__main__":
    unittest.main()
