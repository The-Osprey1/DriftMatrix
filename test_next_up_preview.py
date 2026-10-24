"""Contract tests for the exact 32x8 Next Up visual and payload layer."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from io import BytesIO
import json
from pathlib import Path
import re
import unittest

from PIL import Image

import next_up_preview as art


def icon_occupancy(frame: art.Frame) -> set[tuple[int, int]]:
    return {
        (x, y)
        for y in range(art.HEIGHT)
        for x in range(art.ICON_WIDTH)
        if frame[y][x] != art.BLACK
    }


def connected_component(points: set[tuple[int, int]]) -> set[tuple[int, int]]:
    if not points:
        return set()
    found = {next(iter(points))}
    stack = list(found)
    while stack:
        x, y = stack.pop()
        for neighbor in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if neighbor in points and neighbor not in found:
                found.add(neighbor)
                stack.append(neighbor)
    return found


def decoded_gif(payload: dict) -> Image.Image:
    return Image.open(BytesIO(base64.b64decode(payload["icon"])))


@dataclass
class ForeignEvent:
    summary: str
    eta_text: str
    minutes: float
    state: str


class GenericEventTests(unittest.TestCase):
    def test_title_string_mapping_and_foreign_object_are_accepted(self) -> None:
        string_event = art.coerce_event("Team sync", eta="15 min", minutes_until=15)
        self.assertEqual((string_event.title, string_event.eta), ("TEAM SYNC", "15M"))
        self.assertIs(string_event.urgency, art.Urgency.SOON)

        mapping_event = art.coerce_event(
            {"name": "Dentist appointment", "countdown": "in 2 hours", "minutes": 120}
        )
        self.assertEqual(mapping_event.title, "DENTIST APPOINTMENT")
        self.assertEqual(mapping_event.eta, "2H")
        self.assertIs(mapping_event.urgency, art.Urgency.ROUTINE)

        object_event = art.coerce_event(
            ForeignEvent("Design review", "5m", 5, "urgent")
        )
        self.assertEqual(object_event.title, "DESIGN REVIEW")
        self.assertEqual(object_event.eta, "5M")
        self.assertIs(object_event.urgency, art.Urgency.IMMINENT)

    def test_explicit_fields_override_generic_source_fields(self) -> None:
        event = {"title": "Wrong", "eta": "2H", "urgency": "routine"}
        result = art.coerce_event(
            event,
            title="Right",
            eta="NOW",
            urgency="now",
        )
        self.assertEqual((result.title, result.eta), ("RIGHT", "NOW"))
        self.assertIs(result.urgency, art.Urgency.NOW)

    def test_visual_module_has_no_source_adapter_import(self) -> None:
        source = Path(art.__file__).read_text(encoding="utf-8")
        self.assertIsNone(
            re.search(r"(?m)^\s*(?:from\s+next_up|import\s+next_up)\b", source)
        )

    def test_invalid_urgency_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            art.coerce_event("Call", eta="5M", urgency="purple emergency")


class EtaAndSemanticShorteningTests(unittest.TestCase):
    def test_numeric_eta_is_compact_and_truthful(self) -> None:
        expected = {
            -1: "NOW",
            0: "NOW",
            0.1: "1M",
            59: "59M",
            60: "1H",
            90: "1H30",
            600: "10H",
            1440: "1D",
            2881: "3D",
        }
        for minutes, label in expected.items():
            with self.subTest(minutes=minutes):
                self.assertEqual(art.format_eta(minutes), label)

    def test_human_eta_aliases_fit_the_lane(self) -> None:
        expected = {
            "in 15 minutes": "15M",
            "2 hours": "2H",
            "tomorrow morning": "TMRW",
            "later today": "TDAY",
            "live": "NOW",
        }
        for source, label in expected.items():
            with self.subTest(source=source):
                normalized = art.normalize_eta(source)
                self.assertEqual(normalized, label)
                self.assertLessEqual(art.text_width(normalized), 15)

    def test_font_is_proportional_not_fake_fixed_width(self) -> None:
        # "III" was the narrow exemplar, which only measured 5px while "I" was
        # a bare one-pixel column -- the same column the digit "1" used, so
        # the two glyphs were byte-identical and a title reading "1ST" drew
        # exactly the pixels of one reading "IST".  "I" is serifed now, so
        # punctuation carries the narrow end of the range instead.
        self.assertEqual(art.text_width("..."), 5)
        self.assertEqual(art.text_width("WWW"), 11)
        self.assertLess(art.text_width("..."), art.text_width("WWW"))
        self.assertNotEqual(art.FONT_3X5["1"], art.FONT_3X5["I"])

    def test_unicode_and_punctuation_are_normalized_deterministically(self) -> None:
        self.assertEqual(
            art.normalize_title("Café déjà-vu & résumé review"),
            "CAFE DEJA VU AND RESUME REVIEW",
        )

    def test_known_words_shorten_semantically_before_clipping(self) -> None:
        result = art.shorten_title(
            "Quarterly product design review meeting",
            42,
        )
        self.assertLessEqual(art.text_width(result), 42)
        self.assertIn("QTRLY", result)
        self.assertTrue("REV" in result or "R" in result)

    def test_extreme_titles_are_bounded_repeatable_and_still_meaningful(self) -> None:
        title = (
            "International business machines quarterly architecture governance "
            "review and planning session "
        ) * 200
        first = art.build_layout(title=title, eta="5M")
        second = art.build_layout(title=title, eta="5M")
        self.assertEqual(first, second)
        self.assertTrue(first.title.startswith("INTRNTNL"))
        self.assertGreater(len(first.title), len("INTRNTNL"))
        self.assertLessEqual(first.width, art.MAX_MESSAGE_WIDTH)
        self.assertLessEqual(first.period, art.MAX_FRAMES)


class CalendarArtTests(unittest.TestCase):
    def test_calendar_is_one_connected_stable_silhouette(self) -> None:
        expected = set(art.ICON_OCCUPANCY)
        self.assertEqual(connected_component(expected), expected)
        for urgency in art.Urgency:
            for phase in range(10):
                frame = art.calendar_frame(urgency, phase)
                occupied = {
                    (x, y)
                    for y, row in enumerate(frame)
                    for x, color in enumerate(row)
                    if color != art.BLACK
                }
                self.assertEqual(occupied, expected)

    def test_bindings_header_body_and_date_mark_make_a_calendar(self) -> None:
        self.assertEqual(art.BINDING_TABS, {(2, 0), (5, 0)})
        self.assertTrue(all((x, 1) in art.CALENDAR_OUTLINE for x in range(8)))
        self.assertTrue(all((x, 3) in art.HEADER_DIVIDER for x in range(8)))
        self.assertEqual(art.CALENDAR_MARK, {(3, 5), (4, 5)})

    def test_only_the_connected_bead_highlight_moves(self) -> None:
        frames = [art.calendar_frame("routine", phase) for phase in range(10)]
        self.assertTrue(all(icon_occupancy(frame) == set(art.ICON_OCCUPANCY) for frame in frames))
        self.assertEqual(
            [art.bead_point("routine", phase, 10, scrolling=False) for phase in range(10)],
            list(art.ORBIT_PATH),
        )

    def test_urgency_changes_palette_and_bead_speed_not_geometry(self) -> None:
        total = 60
        sequences = {
            urgency: [
                art.bead_point(urgency, phase, total, scrolling=True)
                for phase in range(total)
            ]
            for urgency in (art.Urgency.ROUTINE, art.Urgency.SOON, art.Urgency.IMMINENT)
        }
        moves = {
            urgency: sum(a != b for a, b in zip(points, points[1:] + points[:1]))
            for urgency, points in sequences.items()
        }
        self.assertLess(moves[art.Urgency.ROUTINE], moves[art.Urgency.SOON])
        self.assertLess(moves[art.Urgency.SOON], moves[art.Urgency.IMMINENT])

        occupancies = {
            urgency: icon_occupancy(art.calendar_frame(urgency, 3))
            for urgency in art.Urgency
        }
        self.assertEqual(len({frozenset(points) for points in occupancies.values()}), 1)
        self.assertNotEqual(
            art.calendar_frame("routine", 0),
            art.calendar_frame("imminent", 0),
        )

    def test_now_lands_the_bead_on_the_calendar_mark(self) -> None:
        for phase in range(20):
            self.assertIn(
                art.bead_point("now", phase, 20, scrolling=True),
                art.CALENDAR_MARK,
            )


class LayoutAndMarqueeTests(unittest.TestCase):
    def test_short_message_is_centered_and_static(self) -> None:
        layout = art.build_layout(title="CALL", eta="5M")
        self.assertFalse(layout.animated)
        viewport = art.lane_source_columns(layout)
        lit = [index for index, column in enumerate(viewport) if column.mask]
        self.assertTrue(lit)
        self.assertEqual(
            min(lit) + max(lit),
            art.LANE_WIDTH - 1,
        )
        self.assertEqual(viewport, art.lane_source_columns(layout, 99))

    def test_long_message_moves_exactly_one_column_every_120ms(self) -> None:
        plan = art.build_animation(
            title="Quarterly product design review meeting",
            eta="15M",
            urgency="soon",
        )
        self.assertTrue(plan.layout.animated)
        self.assertEqual(len(plan.frames), plan.layout.period)
        self.assertEqual(plan.layout.period, plan.layout.width + art.SCROLL_GAP)
        self.assertLessEqual(len(plan.frames), art.MAX_FRAMES)
        self.assertEqual(set(plan.durations_ms), {120})

        for phase in range(plan.layout.period):
            current = art.lane_source_columns(plan.layout, phase)
            following = art.lane_source_columns(plan.layout, phase + 1)
            self.assertEqual(current[1:], following[:-1])

    def test_one_pass_exposes_every_source_column_without_blank_viewport(self) -> None:
        layout = art.build_layout(
            title="Architecture governance review",
            eta="5M",
        )
        self.assertTrue(layout.animated)
        stream = layout.columns + (art.BLANK_COLUMN,) * art.SCROLL_GAP
        observed = tuple(
            art.lane_source_columns(layout, phase)[3]
            for phase in range(layout.period)
        )
        expected = tuple(stream[(phase + 3) % len(stream)] for phase in range(layout.period))
        self.assertEqual(observed, expected)
        for phase in range(layout.period):
            self.assertTrue(
                any(column.mask for column in art.lane_source_columns(layout, phase)),
                f"blank viewport at phase {phase}",
            )

    def test_edge_fade_is_gamma_aware_and_never_erases_lit_text(self) -> None:
        self.assertEqual(art.edge_strength(0), art.edge_strength(22))
        self.assertLess(art.edge_strength(0), art.edge_strength(1))
        self.assertLess(art.edge_strength(1), art.edge_strength(2))
        self.assertLess(art.edge_strength(2), art.edge_strength(3))

        source = art.SKY
        faded = art.gamma_fade(source, art.edge_strength(0))
        self.assertNotEqual(faded, art.BLACK)
        self.assertTrue(all(a <= b for a, b in zip(faded, source)))
        source_light = art.panel_grade_color(source, brightness=255, saturation=100)
        faded_light = art.panel_grade_color(faded, brightness=255, saturation=100)
        ratios = [f / s for f, s in zip(faded_light, source_light) if s]
        for ratio in ratios:
            self.assertAlmostEqual(ratio, art.edge_strength(0), delta=0.04)

    def test_exact_canvas_gutter_and_no_decorative_lane_bars(self) -> None:
        for frame in art.panel_frames(
            title="Architecture governance review",
            eta="5M",
            urgency="imminent",
        ):
            self.assertEqual(len(frame), 8)
            self.assertTrue(all(len(row) == 32 for row in frame))
            self.assertTrue(all(frame[y][8] == art.BLACK for y in range(8)))
            for y in (0, 6, 7):
                self.assertTrue(all(frame[y][x] == art.BLACK for x in range(9, 32)))

    def test_text_layout_is_identical_across_urgency_states(self) -> None:
        layouts = [
            art.build_animation(title="Design review", eta="5M", urgency=urgency).layout
            for urgency in art.Urgency
        ]
        self.assertTrue(all(layout.columns == layouts[0].columns for layout in layouts))
        self.assertTrue(all(layout.width == layouts[0].width for layout in layouts))


class GifAndPayloadTests(unittest.TestCase):
    def test_short_animation_changes_bead_speed_via_frame_duration(self) -> None:
        expected = {
            art.Urgency.ROUTINE: 360,
            art.Urgency.SOON: 240,
            art.Urgency.IMMINENT: 120,
        }
        for urgency, delay in expected.items():
            with self.subTest(urgency=urgency):
                payload = art.build_payload(title="CALL", eta="5M", urgency=urgency)
                with decoded_gif(payload) as image:
                    self.assertEqual(image.n_frames, len(art.ORBIT_PATH))
                    for index in range(image.n_frames):
                        image.seek(index)
                        self.assertEqual(image.info.get("duration"), delay)

    def test_now_is_a_static_landed_bead_when_text_fits(self) -> None:
        payload = art.build_payload(title="TV", eta="NOW", urgency="now")
        with decoded_gif(payload) as image:
            self.assertEqual(image.n_frames, 1)

    def test_nonpositive_durations_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            art.build_payload("Call", eta="5M", duration_ms=0)
        with self.assertRaises(ValueError):
            art.build_payload("Call", eta="5M", lifetime_ms=0)


class GammaContractTests(unittest.TestCase):
    def test_real_panel_gamma_and_true_black_are_explicit(self) -> None:
        self.assertEqual(art.PANEL_GAMMA, 2.2)
        self.assertEqual(art.PANEL_SATURATION, 90)
        self.assertEqual(art.BLACK, (0, 0, 0))
        self.assertEqual(art.panel_grade_color(art.BLACK), art.BLACK)

    def test_semantic_palette_survives_gamma_22_at_day_brightness(self) -> None:
        for color in (
            art.PEARL,
            art.ICE,
            art.SLATE,
            art.SKY,
            art.GOLD,
            art.AMBER,
            art.CORAL,
            art.VIOLET,
            art.MINT,
            art.TRACK_COOL,
            art.TRACK_WARM,
        ):
            with self.subTest(color=color):
                self.assertNotEqual(art.panel_grade_color(color, brightness=45), art.BLACK)



class CalmGlancePayloadTests(unittest.TestCase):
    """Next Up on the panel-wide layout: 8x8 calendar, static lane, no scroll."""

    def decode(self, payload):
        import base64
        from io import BytesIO
        from PIL import Image
        icon = Image.open(BytesIO(base64.b64decode(payload["icon"])))
        return icon

    def test_icon_is_8x8_and_lane_is_a_static_bitmap(self) -> None:
        payload = art.build_payload(title="Dentist appointment", minutes_until=14, eta="14M")
        self.assertEqual(self.decode(payload).size, (8, 8))
        self.assertEqual(payload["draw"][0][:5], ["bitmap", 9, 0, 23, 8])
        self.assertNotIn("text", payload)

    def test_extreme_title_never_widens_the_page(self) -> None:
        payload = art.build_payload(title="W" * 400, minutes_until=45, eta="45M")
        self.assertLess(art.serialized_size(payload), 2_048)

    def test_now_is_a_single_still_frame_with_removal_lifetime(self) -> None:
        payload = art.build_payload(title="Standup", minutes_until=0, eta="NOW", animated=True)
        self.assertEqual(getattr(self.decode(payload), "n_frames", 1), 1)
        self.assertEqual(payload["lifetimeExpiry"], "remove")
        self.assertEqual(payload["backgroundColor"], "#000000")

    def test_meter_counts_down_the_last_hour(self) -> None:
        import base64
        from visual_system import TRACK
        def lit(minutes):
            raw = base64.b64decode(art.build_payload(title="X", minutes_until=minutes, eta=f"{minutes}M")["draw"][0][5])
            row7 = [tuple(raw[(7 * 23 + x) * 3:(7 * 23 + x) * 3 + 3]) for x in range(23)]
            return sum(1 for c in row7 if max(c) > 90)
        self.assertGreater(lit(50), lit(10))


class GlanceLabelTests(unittest.TestCase):
    def _label_pixels(self, payload):
        lane = base64.b64decode(payload["draw"][0][5])
        return sum(1 for i in range(0, len(lane), 3) if lane[i:i + 3] == bytes((178, 190, 206)))

    def test_a_lone_initial_is_dropped(self) -> None:
        payload = art.build_payload(title="DENTIST APPOINTMENT TODAY", minutes_until=45)
        self.assertEqual(self._label_pixels(payload), 0)

    def test_two_letter_fragments_are_dropped_too(self) -> None:
        # At most 10px sit beside the countdown and a three-letter word needs
        # 11-13px, so in practice the lane is the countdown alone.
        for minutes in (1, 5, 12, 45, 90):
            with self.subTest(minutes=minutes):
                payload = art.build_payload(title="AB", minutes_until=minutes)
                self.assertEqual(self._label_pixels(payload), 0)


if __name__ == "__main__":
    unittest.main()
