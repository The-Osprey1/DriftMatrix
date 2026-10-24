"""Deterministic checks for the AWTRIX countdown app."""

from __future__ import annotations

import datetime as dt
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent

CAPS = [0xFF, 0, 0, 0, 0, 0, 0, 0xFF]
GLASS = [0, 0x42, 0x24, 0x18, 0x18, 0x24, 0x42, 0]
SAND = [
    [0, 0x3C, 0x18, 0, 0, 0, 0, 0],
    [0, 0x18, 0x18, 0x18, 0x18, 0x18, 0x18, 0],
    [0, 0, 0x18, 0x18, 0x18, 0x18, 0x3C, 0],
    [0, 0, 0, 0x18, 0x18, 0x18, 0x3C, 0],
    [0, 0, 0, 0, 0, 0x18, 0x3C, 0],
]
INTERIOR = [0, 0x3C, 0x18, 0x18, 0x18, 0x18, 0x3C, 0]


def jdn(year: int, month: int, day: int) -> int:
    a = (14 - month) // 12
    y = year + 4800 - a
    m = month + 12 * a - 3
    return day + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400 - 32045


def parse_target(value: str | None) -> int | None:
    if value is None or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    year, month, day = map(int, value.split("-"))
    if not 1970 <= year <= 9999:
        return None
    try:
        dt.date(year, month, day)
    except ValueError:
        return None
    return jdn(year, month, day)


def normalize_label(value: str | None) -> str:
    return (value or "PHD").upper()[:5] or "PHD"


def urgency(days: int | None) -> str:
    if days is None:
        return "invalid"
    if days < 0:
        return "done"
    if days == 0:
        return "today"
    if days <= 7:
        return "urgent"
    if days <= 30:
        return "soon"
    return "calm"


def value_text(days: int | None) -> str:
    if days is None:
        return "DATE"
    if days < 0:
        return "DONE"
    if days == 0:
        return "TODAY"
    if days > 999:
        return "999+"
    return f"{days}d"


def display_text(days: int | None, label: str | None, elapsed_ms: int) -> str:
    if days is None or days <= 0:
        return value_text(days)
    if elapsed_ms < 1400:
        return normalize_label(label)
    return value_text(days)


def mirror_8_bits(row: int) -> int:
    return sum(((row >> bit) & 1) << (7 - bit) for bit in range(8))


class CountdownTests(unittest.TestCase):
    def test_jdn_anchors_and_date_boundaries(self) -> None:
        self.assertEqual(jdn(2000, 1, 1), 2451545)
        self.assertEqual(jdn(2024, 2, 29) + 1, jdn(2024, 3, 1))
        self.assertEqual(jdn(2026, 12, 31) + 1, jdn(2027, 1, 1))

    def test_target_parser_is_strict(self) -> None:
        for value in ("2024-02-29", "2026-12-01"):
            with self.subTest(value=value):
                self.assertIsNotNone(parse_target(value))
        for value in (
            None, "", "2026/12/01", "2026-12", "2026-12-01-extra",
            "abcd-12-01", "2026-00-01", "2026-13-01", "2026-04-31",
            "2026-02-29", "2026-01-00",
        ):
            with self.subTest(value=value):
                self.assertIsNone(parse_target(value))

    def test_day_difference_preserves_today_and_past(self) -> None:
        target = parse_target("2027-01-01")
        self.assertEqual(target - parse_target("2026-12-31"), 1)
        self.assertEqual(target - parse_target("2027-01-01"), 0)
        self.assertEqual(target - parse_target("2027-01-02"), -1)

    def test_urgency_boundaries(self) -> None:
        cases = {
            31: "calm", 30: "soon", 8: "soon", 7: "urgent",
            1: "urgent", 0: "today", -1: "done", None: "invalid",
        }
        for days, expected in cases.items():
            with self.subTest(days=days):
                self.assertEqual(urgency(days), expected)

    def test_value_copy_is_bounded_and_unambiguous(self) -> None:
        cases = {115: "115d", 1: "1d", 0: "TODAY", -1: "DONE", 999: "999d", 1000: "999+", None: "DATE"}
        for days, expected in cases.items():
            with self.subTest(days=days):
                self.assertEqual(value_text(days), expected)

    def test_label_is_short_and_normalized(self) -> None:
        self.assertEqual(normalize_label(None), "PHD")
        self.assertEqual(normalize_label(""), "PHD")
        self.assertEqual(normalize_label("phd"), "PHD")
        self.assertEqual(normalize_label("LaunchDay"), "LAUNC")

    def test_display_sequence_moves_once_then_holds(self) -> None:
        self.assertEqual(
            [display_text(115, "PHD", t) for t in (0, 1399, 1400, 5999, 6500)],
            ["PHD", "PHD", "115d", "115d", "115d"],
        )
        self.assertEqual(display_text(0, "PHD", 0), "TODAY")
        self.assertEqual(display_text(-1, "PHD", 0), "DONE")

    def test_hourglass_silhouette_is_symmetric(self) -> None:
        silhouette = [cap | glass for cap, glass in zip(CAPS, GLASS)]
        self.assertEqual(silhouette, list(reversed(silhouette)))
        for row in silhouette:
            self.assertEqual(row, mirror_8_bits(row))

    def test_sand_moves_down_in_connected_pairs(self) -> None:
        centroids = []
        for frame in SAND:
            weighted = 0
            pixels = 0
            for y, row in enumerate(frame):
                self.assertEqual(row & ~INTERIOR[y], 0)
                if row:
                    self.assertGreaterEqual(row.bit_count(), 2)
                weighted += y * row.bit_count()
                pixels += row.bit_count()
            centroids.append(weighted / pixels)
        self.assertEqual(centroids, sorted(centroids))

    def test_source_preserves_the_countdown_contract(self) -> None:
        path = ROOT / "soon.ax"
        source = path.read_text(encoding="utf-8")
        self.assertIn('default="2026-12-01"', source)
        self.assertIn('default="PHD"', source)
        self.assertIn('target = "2026-12-01"', source)
        self.assertIn('tag = "PHD"', source)
        self.assertIn('string.tr(', source)
        self.assertIn('d > last', source)
        self.assertIn('def on_show()', source)
        self.assertIn('now_ms() - self.shown', source)
        self.assertIn('elapsed < 1400', source)
        self.assertNotIn('elapsed %', source)
        self.assertNotIn('/ 120.0', source)
        self.assertNotIn('? "0d"', source)
        self.assertIn('return "TODAY"', source)
        self.assertIn('return "DONE"', source)
        self.assertRegex(source, r"def duration\(\)\s*return 6000")
        self.assertIn("clear()", source)
        self.assertLessEqual(path.stat().st_size, 5000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
