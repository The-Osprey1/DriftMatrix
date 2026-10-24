"""Deterministic checks for the AWTRIX air-quality app."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent

LABELS = ["GOOD", "FAIR", "WARN", "BAD", "V.BAD", "HAZ"]
LUNGS = [0x18, 0x18, 0x3C, 0x66, 0xE7, 0xE7, 0x66, 0x24]


def level(aqi: int) -> int:
    if aqi <= 50:
        return 0
    if aqi <= 100:
        return 1
    if aqi <= 150:
        return 2
    if aqi <= 200:
        return 3
    if aqi <= 300:
        return 4
    return 5


def display_text(aqi: int, elapsed_ms: int) -> str:
    """Mirror the app's one-shot information hierarchy."""
    if elapsed_ms < 1250:
        return "AQI"
    if elapsed_ms < 3750:
        return "500+" if aqi > 500 else str(max(0, aqi))
    return LABELS[level(aqi)]


def mirror_8_bits(row: int) -> int:
    return sum(((row >> bit) & 1) << (7 - bit) for bit in range(8))


class AirQualityTests(unittest.TestCase):
    def test_epa_band_boundaries(self) -> None:
        cases = [
            (0, 0), (50, 0),
            (51, 1), (100, 1),
            (101, 2), (150, 2),
            (151, 3), (200, 3),
            (201, 4), (300, 4),
            (301, 5), (500, 5),
        ]
        for aqi, expected in cases:
            with self.subTest(aqi=aqi):
                self.assertEqual(level(aqi), expected)

    def test_severity_never_moves_backwards(self) -> None:
        levels = [level(aqi) for aqi in range(501)]
        self.assertEqual(levels, sorted(levels))
        self.assertEqual(set(levels), set(range(6)))

    def test_labels_fit_the_small_text_area(self) -> None:
        for label in LABELS:
            self.assertLessEqual(len(label), 5)

    def test_copy_sequence_is_clear_and_one_way(self) -> None:
        self.assertEqual(
            [display_text(42, t) for t in (0, 1249, 1250, 3749, 3750, 5999)],
            ["AQI", "AQI", "42", "42", "GOOD", "GOOD"],
        )
        self.assertEqual(display_text(42, 6500), "GOOD")

    def test_extreme_values_remain_displayable(self) -> None:
        self.assertEqual(display_text(-2, 2000), "0")
        self.assertEqual(display_text(501, 2000), "500+")

    def test_lung_silhouette_is_symmetric_and_distinct(self) -> None:
        self.assertEqual(len(LUNGS), 8)
        self.assertGreaterEqual(sum(row.bit_count() for row in LUNGS), 24)
        for row in LUNGS:
            self.assertEqual(row, mirror_8_bits(row))
        for row in LUNGS[3:]:
            self.assertEqual(row & 0x18, 0)

    def test_source_preserves_behavioral_contract(self) -> None:
        path = ROOT / "aqi.ax"
        source = path.read_text(encoding="utf-8")
        before_draw, draw = source.split("def draw()", 1)

        self.assertIn("# @name Air quality", source)
        self.assertIn("current=us_aqi", source)
        self.assertIn(
            "http://air-quality-api.open-meteo.com/v1/air-quality", source
        )
        self.assertIn("http.get(", before_draw)
        self.assertNotIn("http.get(", draw)
        self.assertNotIn("json.load(", draw)
        self.assertIn("clear()", draw)
        self.assertIn("wx.stale()", draw)
        self.assertIn("def on_show()", source)
        self.assertIn("now_ms() - self.shown", source)
        self.assertNotIn("elapsed %", source)
        self.assertRegex(source, r"def duration\(\)\s*return 6000")

        for label in LABELS:
            self.assertIn(f'"{label}"', source)

        mask = re.search(r"self\.lungs\s*=\s*\[([^\]]+)\]", source)
        self.assertIsNotNone(mask)
        parsed = [int(token.strip(), 0) for token in mask.group(1).split(",")]
        self.assertEqual(parsed, LUNGS)

        self.assertLessEqual(path.stat().st_size, 5000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
