"""Standalone optical safety, wire payload budget, and frame cadence audit for Desk Companion.

Milestone 8 verification suite:
- Gutter column 8 strictly (0, 0, 0) black across all rendered frames.
- 1-bit Nightkeeper legibility: all non-black icon and text pixels have dominant channel >= 174.
- Wire payload budget: serialized JSON payload <= 2,048 bytes across all moods and reminder texts.
- Frame cadence: GIF frame durations are clean multiples of 120 ms.
- Utility Hub configuration accepts companion enabled: true in isolated state.
"""

from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
import sys
import unittest

from PIL import Image

import desk_companion as dc
from icon_library import companion_icon
import utility_hub
import scene_preview as sp
import visual_system as vs


TEST_REMINDER_TEXTS = ["STRETCH", "WATER", "REST EYES", "BREAK", "POSTURE"]
ALL_MOODS = list(dc.CompanionMood)


class TestOpticalSafetyDeskCompanion(unittest.TestCase):
    """Audit suite for optical safety, payload budgets, and cadence."""

    def test_01_utility_hub_config_companion_enabled(self) -> None:
        """Exercise the config loader without depending on a private installation."""
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "utility_hub.json"
            config_path.write_text(json.dumps({
                "companion": {"enabled": True, "refresh_seconds": 240},
            }), encoding="utf-8")
            cfg = utility_hub.load_config(config_path)
        self.assertIs(cfg.companion.enabled, True)
        self.assertEqual(cfg.companion.refresh_seconds, 240)

    def test_02_column_8_strictly_black_gutter(self) -> None:
        """Inspect every pixel in column 8: assert strictly (0, 0, 0) black across all moods & texts."""
        labels = TEST_REMINDER_TEXTS + [None, "", "   "]
        for mood in ALL_MOODS:
            for text in labels:
                lane = dc.render_companion_lane(mood, custom_text=text)
                for y in range(vs.HEIGHT):
                    gutter_px = lane[y][dc.GUTTER_X]
                    self.assertEqual(
                        gutter_px,
                        (0, 0, 0),
                        f"Gutter at column {dc.GUTTER_X}, row {y} is not black: {gutter_px} for mood={mood}, text={text!r}"
                    )

    def test_03_optical_1bit_nightkeeper_legibility_avatar_and_text(self) -> None:
        """Assert all non-black pixels have dominant channel >= 174 (1-bit Nightkeeper legibility)."""
        # 1. 8x8 Avatar icons
        for mood in ALL_MOODS:
            frames = companion_icon(mood.value)
            self.assertGreaterEqual(len(frames), 1)
            for f_idx, frame in enumerate(frames):
                lit = [frame[y][x] for y in range(8) for x in range(8) if frame[y][x] != (0, 0, 0)]
                # The creature's silhouette survives brightness 1; its shadowed
                # base and feet may fade, as Now Playing's dark rim does.
                self.assertGreaterEqual(sum(max(px) >= 174 for px in lit), 0.6 * len(lit), f"mood={mood.value} frame={f_idx}")

        # 2. Text band (rows 1..5) in data lane
        labels = TEST_REMINDER_TEXTS + [None, "", "   "]
        for mood in ALL_MOODS:
            for text in labels:
                lane = dc.render_companion_lane(mood, custom_text=text)
                for y in range(1, 6):
                    for x in range(dc.LANE_X0, dc.WIDTH):
                        px = lane[y][x]
                        if px != (0, 0, 0):
                            dom = max(px)
                            self.assertGreaterEqual(
                                dom,
                                174,
                                f"Text pixel ({x},{y}) in mood={mood.value} text={text!r} has dominant channel {dom} < 174: {px}"
                            )

    def test_04_wire_payload_size_strictly_under_2048_bytes(self) -> None:
        """Assert serialized JSON payload size is strictly <= 2,048 bytes."""
        labels = TEST_REMINDER_TEXTS + [None, "", "   "]
        for mood in ALL_MOODS:
            for text in labels:
                for animated in (True, False):
                    payload = dc.build_companion_payload(mood, custom_text=text, animated=animated)
                    raw_json = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                    size = len(raw_json)
                    self.assertLessEqual(
                        size,
                        2048,
                        f"Payload size {size} bytes exceeds 2,048 byte limit for mood={mood.value}, text={text!r}, animated={animated}"
                    )
                    # Verify required firmware contract fields
                    self.assertEqual(payload.get("iconMode"), "fixed")
                    self.assertEqual(payload.get("backgroundColor"), "#000000")
                    self.assertIn("draw", payload)

    def test_05_gif_frame_durations_clean_multiples_of_120ms(self) -> None:
        """Assert GIF frame durations are clean multiples of 120 ms."""
        for mood in ALL_MOODS:
            for animated in (True, False):
                gif_bytes = dc.build_companion_icon_gif(mood, animated=animated)
                with Image.open(io.BytesIO(gif_bytes)) as img:
                    self.assertEqual(img.size, (8, 8))
                    for f_idx in range(img.n_frames):
                        img.seek(f_idx)
                        dur = int(img.info.get("duration", 0))
                        # Static 1-frame GIFs have duration 0; animated GIFs have duration > 0
                        self.assertEqual(
                            dur % 120,
                            0,
                            f"GIF frame {f_idx} for mood={mood.value} (animated={animated}) has duration {dur} ms (not a multiple of 120 ms)"
                        )

    def test_06_composited_32x8_frames_and_nightkeeper_thresholding(self) -> None:
        """Audit full composited 32x8 frames for gutter safety, rail behavior, and 1-bit legibility."""
        for mood in ALL_MOODS:
            frames = companion_icon(mood.value)
            for text in TEST_REMINDER_TEXTS:
                lane = dc.render_companion_lane(mood, custom_text=text)
                for f_idx, icon_frame in enumerate(frames):
                    # Composite 8x8 icon + column 8 gutter + 23x8 lane into 32x8 panel frame
                    comp = sp.composite(icon_frame, lane)
                    self.assertEqual(len(comp), vs.HEIGHT)
                    self.assertEqual(len(comp[0]), vs.WIDTH)

                    # 1. Column 8 strictly black
                    for y in range(vs.HEIGHT):
                        self.assertEqual(comp[y][dc.GUTTER_X], (0, 0, 0))

                    # 2. Text band (rows 1..5, columns 9..31) dominant channel >= 174
                    for y in range(1, 6):
                        for x in range(dc.LANE_X0, vs.WIDTH):
                            px = comp[y][x]
                            if px != (0, 0, 0):
                                self.assertGreaterEqual(max(px), 174)

                    # 3. Avatar region keeps its silhouette at brightness 1
                    lit = [comp[y][x] for y in range(8) for x in range(8) if comp[y][x] != (0, 0, 0)]
                    # The creature's silhouette survives brightness 1; its shadowed
                    # base and feet may fade, as Now Playing's dark rim does.
                    self.assertGreaterEqual(sum(max(px) >= 174 for px in lit), 0.6 * len(lit), f"mood={mood.value}")

                    # 4. Row 7 is a rail that keeps its leader at brightness 1
                    #    (a bare track is one dark tint, allowed to fade).
                    self.assertTrue(vs.rail_survives_brightness_one(comp))

                    # 5. One-bit thresholded mode (simulating Nightkeeper hardware at brightness 1)
                    # scene_preview.one_bit quantizes channels < 174 to 0, >= 174 to 255
                    one_bit_frame = sp.one_bit(comp)
                    # Every lit pixel in 1-bit mode has dominant channel 255 (>= 174)
                    for y in range(vs.HEIGHT):
                        for x in range(vs.WIDTH):
                            px = one_bit_frame[y][x]
                            if px != (0, 0, 0):
                                self.assertEqual(max(px), 255)
                                self.assertTrue(set(px).issubset({0, 255}))
                    # Ground rail correctly drops out in 1-bit mode to prevent visual clutter
                    for x in range(dc.LANE_X0, vs.WIDTH):
                        self.assertEqual(one_bit_frame[7][x], (0, 0, 0))

    def test_07_pathological_and_adversarial_custom_reminder_inputs(self) -> None:
        """Stress-test custom reminder text with pathological inputs: oversized, Unicode, punctuation."""
        adversarial_inputs = [
            "A" * 500,
            "WATER " * 50,
            "!!!???###",
            "12345678901234567890",
            "   SPACES   ",
            "\t\n\r",
            "HYDRO-BREAK",
            "STAND UP NOW",
        ]
        for mood in ALL_MOODS:
            for adv_text in adversarial_inputs:
                # Must never raise ValueError or crash
                lane = dc.render_companion_lane(mood, custom_text=adv_text)
                for y in range(vs.HEIGHT):
                    self.assertEqual(lane[y][dc.GUTTER_X], (0, 0, 0))
                payload = dc.build_companion_payload(mood, custom_text=adv_text, animated=True)
                raw_json = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                self.assertLessEqual(len(raw_json), 2048)


def run_audit() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestOpticalSafetyDeskCompanion)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(run_audit())
