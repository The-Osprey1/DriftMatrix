"""Deterministic checks for the offline AWTRIX moon phase app."""

from __future__ import annotations

import math
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SYNODIC_DAYS = 29.530588853
REFERENCE_NEW_MOON = datetime(2000, 1, 6, 18, 15, tzinfo=timezone.utc)

DISC = [0x3C, 0x7E, 0xFF, 0xFF, 0xFF, 0xFF, 0x7E, 0x3C]
STARS = [(0, 0), (7, 0), (0, 7), (7, 7)]
STAR_OFFSETS = [0, 3, 7, 10]
PHASES = [
    [0x00] * 8,
    [0x04, 0x02, 0x03, 0x03, 0x03, 0x03, 0x02, 0x04],
    [0x0C, 0x0E, 0x0F, 0x0F, 0x0F, 0x0F, 0x0E, 0x0C],
    [0x1C, 0x3E, 0x7F, 0x7F, 0x7F, 0x7F, 0x3E, 0x1C],
    DISC,
    [0x38, 0x7C, 0xFE, 0xFE, 0xFE, 0xFE, 0x7C, 0x38],
    [0x30, 0x70, 0xF0, 0xF0, 0xF0, 0xF0, 0x70, 0x30],
    [0x20, 0x40, 0xC0, 0xC0, 0xC0, 0xC0, 0x40, 0x20],
]

# Bounds for the chronological previous -> current arrival.
BOUNDS = [(0, 2), (5, 7), (4, 5), (1, 3), (0, 2), (5, 7), (4, 6), (2, 3)]
DIRECTION_MS = 1500


def phase_at(moment: datetime) -> tuple[int, int, bool]:
    days = (moment - REFERENCE_NEW_MOON).total_seconds() / 86400
    fraction = (days / SYNODIC_DAYS) % 1
    illumination = round((1 - math.cos(2 * math.pi * fraction)) * 50)
    phase_index = int(fraction * 8 + 0.5) % 8
    return phase_index, illumination, fraction < 0.5


def mirror_8_bits(row: int) -> int:
    return sum(((row >> source) & 1) << (7 - source) for source in range(8))


def star_level(frame: int, offset: int) -> int:
    phase = (frame + offset) % 12
    if phase in (5, 6):
        return 2
    if 2 <= phase <= 9:
        return 1
    return 0


def moon_text(illumination: int, waxing: bool, elapsed_ms: int) -> str:
    elapsed_ms = max(0, elapsed_ms)
    if elapsed_ms < DIRECTION_MS:
        return "WAX" if waxing else "WANE"
    return f"{illumination}%"


def arrival_frame(index: int, step: int) -> tuple[list[int], list[int]]:
    """Return visible pixels and the connected accent for one arrival step."""
    previous = PHASES[(index - 1) % 8]
    current = PHASES[index]
    lo, hi = BOUNDS[index]
    width = hi - lo + 1
    if step > width:
        return current.copy(), [0] * 8

    visible = previous.copy()
    accent = [0] * 8
    for position, x in enumerate(range(hi, lo - 1, -1)):
        bit = 1 << (7 - x)
        delta_count = sum(bool((a ^ b) & bit) for a, b in zip(previous, current))
        if position < step - 1 or (position == step - 1 and delta_count <= 2):
            for y, (old, new) in enumerate(zip(previous, current)):
                if (old ^ new) & bit:
                    visible[y] = (visible[y] & ~bit) | (new & bit)
        elif position == step - 1:
            for y, (old, new) in enumerate(zip(previous, current)):
                if (old ^ new) & bit:
                    accent[y] |= bit
                    # Waxing accents are already visible; waning accents remain
                    # visible until the advancing shadow commits on the next step.
                    visible[y] |= bit
    return visible, accent


class MoonTests(unittest.TestCase):
    def test_reference_epoch_is_new(self) -> None:
        self.assertEqual(phase_at(REFERENCE_NEW_MOON), (0, 0, True))

    def test_matches_august_2026_primary_phase_sequence(self) -> None:
        # USNO: last quarter Aug 6 02:21 UTC; new moon Aug 12 17:37 UTC.
        self.assertEqual(
            phase_at(datetime(2026, 8, 6, 2, 21, tzinfo=timezone.utc))[0], 6
        )
        self.assertEqual(
            phase_at(datetime(2026, 8, 12, 17, 37, tzinfo=timezone.utc))[0], 0
        )

    def test_current_date_is_waning_crescent(self) -> None:
        index, illumination, waxing = phase_at(
            datetime(2026, 8, 8, 16, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(index, 7)
        self.assertTrue(18 <= illumination <= 28)
        self.assertFalse(waxing)

    def test_phase_masks_are_bounded_and_mirrored(self) -> None:
        for phase in PHASES:
            self.assertEqual(len(phase), 8)
            for light, disc in zip(phase, DISC):
                self.assertEqual(light & ~disc, 0)
        for waxing, waning in ((1, 7), (2, 6), (3, 5)):
            self.assertEqual(
                [mirror_8_bits(row) for row in PHASES[waxing]], PHASES[waning]
            )

    def test_lit_area_grows_then_shrinks(self) -> None:
        counts = [sum(row.bit_count() for row in phase) for phase in PHASES]
        self.assertEqual(counts[4], sum(row.bit_count() for row in DISC))
        self.assertLess(counts[1], counts[2])
        self.assertLess(counts[2], counts[3])
        self.assertLess(counts[3], counts[4])
        self.assertEqual(counts[1], counts[7])
        self.assertEqual(counts[2], counts[6])
        self.assertEqual(counts[3], counts[5])

    def test_waxing_adds_light_and_waning_removes_it(self) -> None:
        for index in (1, 2, 3, 4):
            previous, current = PHASES[index - 1], PHASES[index]
            for old, new in zip(previous, current):
                self.assertEqual(old & ~new, 0)

        for index in (5, 6, 7, 0):
            previous, current = PHASES[(index - 1) % 8], PHASES[index]
            for old, new in zip(previous, current):
                self.assertEqual(new & ~old, 0)

    def test_arrival_is_one_way_and_ends_on_today(self) -> None:
        for index in range(8):
            lo, hi = BOUNDS[index]
            width = hi - lo + 1
            frames = [arrival_frame(index, step)[0] for step in range(width + 2)]
            self.assertEqual(frames[0], PHASES[(index - 1) % 8])
            self.assertEqual(frames[-1], PHASES[index])
            counts = [sum(row.bit_count() for row in frame) for frame in frames]
            if 1 <= index <= 4:
                self.assertEqual(counts, sorted(counts))
            else:
                self.assertEqual(counts, sorted(counts, reverse=True))

            delta = [a ^ b for a, b in zip(PHASES[(index - 1) % 8], PHASES[index])]
            for frame in frames:
                for row, previous, changed in zip(
                    frame, PHASES[(index - 1) % 8], delta
                ):
                    self.assertEqual((row ^ previous) & ~changed, 0)

    def test_terminator_commits_columns_from_right_to_left(self) -> None:
        for index in range(8):
            previous = PHASES[(index - 1) % 8]
            current = PHASES[index]
            lo, hi = BOUNDS[index]
            expected_order = list(range(hi, lo - 1, -1))

            for step in range(hi - lo + 2):
                visible, accent = arrival_frame(index, step)
                committed = []
                for x in expected_order:
                    bit = 1 << (7 - x)
                    changed_rows = [
                        y
                        for y, (old, new) in enumerate(zip(previous, current))
                        if (old ^ new) & bit
                    ]
                    if changed_rows and all(
                        (visible[y] & bit) == (current[y] & bit)
                        and not (accent[y] & bit)
                        for y in changed_rows
                    ):
                        committed.append(x)
                self.assertEqual(committed, expected_order[: len(committed)])

    def test_full_disc_is_always_populated(self) -> None:
        for phase in PHASES:
            shadow = [disc & ~light for disc, light in zip(DISC, phase)]
            self.assertEqual(
                [light | dark for light, dark in zip(phase, shadow)], DISC
            )

    def test_stars_stay_outside_the_disc_and_never_switch_off(self) -> None:
        self.assertEqual(len(STARS), len(set(STARS)))
        for x, y in STARS:
            self.assertTrue(0 <= x < 8 and 0 <= y < 8)
            self.assertEqual(DISC[y] & (1 << (7 - x)), 0)
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if 0 <= nx < 8 and 0 <= ny < 8:
                    self.assertEqual(DISC[ny] & (1 << (7 - nx)), 0)
        for offset in STAR_OFFSETS:
            levels = [star_level(frame, offset) for frame in range(12)]
            self.assertEqual(levels.count(2), 2)
            self.assertEqual(levels.count(1), 6)
            self.assertEqual(levels.count(0), 4)

    def test_direction_cue_hands_off_once_to_plain_percentage(self) -> None:
        self.assertEqual(moon_text(23, True, -1), "WAX")
        self.assertEqual(moon_text(23, True, 1499), "WAX")
        self.assertEqual(moon_text(23, True, 1500), "23%")
        self.assertEqual(moon_text(23, True, 5999), "23%")
        self.assertEqual(moon_text(67, False, 0), "WANE")
        self.assertEqual(moon_text(67, False, 1499), "WANE")
        self.assertEqual(moon_text(67, False, 1500), "67%")
        self.assertEqual(moon_text(0, True, 1500), "0%")
        self.assertEqual(moon_text(100, False, 1500), "100%")

        sequence = [moon_text(67, False, elapsed) for elapsed in range(0, 6000, 100)]
        transitions = sum(a != b for a, b in zip(sequence, sequence[1:]))
        self.assertEqual(transitions, 1)
        self.assertNotIn("WANE", sequence[DIRECTION_MS // 100 :])

    def test_every_accent_is_a_vertical_cluster_not_a_blinking_dot(self) -> None:
        for index in range(8):
            lo, hi = BOUNDS[index]
            for step in range(1, hi - lo + 2):
                _, accent = arrival_frame(index, step)
                points = [
                    (x, y)
                    for y, row in enumerate(accent)
                    for x in range(8)
                    if row & (1 << (7 - x))
                ]
                if not points:
                    continue
                self.assertGreaterEqual(len(points), 3)
                self.assertEqual(len({x for x, _ in points}), 1)
                ys = sorted(y for _, y in points)
                self.assertEqual(ys, list(range(ys[0], ys[-1] + 1)))

    def test_source_has_one_shot_arrival_and_plain_direction_cue(self) -> None:
        source = (ROOT / "moon.ax").read_text(encoding="utf-8")
        self.assertIn("def on_show()", source)
        self.assertIn("self.recalc()", source[source.index("def on_show()") :])
        self.assertIn("if elapsed < 1500", source)
        self.assertIn('s = self.waxing ? "WAX" : "WANE"', source)
        self.assertIn('s = str(self.illum) + "%"', source)
        self.assertIn("int((elapsed - 250) / 280)", source)
        self.assertNotIn("elapsed %", source)
        self.assertNotIn("self.future", source)
        self.assertNotIn("self.outline", source)
        self.assertIn("wx.blit(self.disc, 0, 0, shade)", source)
        self.assertIn("def star(f, offset)", source)
        self.assertNotIn('str(self.illum) + "%" +', source)
        self.assertNotIn('self.waxing ? "+" : "-"', source)
        self.assertNotIn('s = "NEW"', source)
        self.assertNotIn('s = "FULL"', source)
        self.assertNotIn("def mark(", source)
        self.assertLessEqual((ROOT / "moon.ax").stat().st_size, 5000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
