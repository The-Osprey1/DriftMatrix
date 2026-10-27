from __future__ import annotations

import base64
from dataclasses import dataclass
from enum import Enum
from io import BytesIO
import json
import unittest

from PIL import Image

import focus_sprint_preview as art


class ForeignPhase(Enum):
    FOCUS = "focus_running"


@dataclass
class ForeignSnapshot:
    phase: ForeignPhase
    remaining_seconds: float
    phase_total_seconds: float
    cycle_count: int
    pause_reason: str | None = None
    visible: bool = True


def lit_points(frame: art.Frame) -> set[tuple[int, int]]:
    return {
        (x, y)
        for y, row in enumerate(frame)
        for x, color in enumerate(row)
        if color != art.BLACK
    }


def lane_text_bounds(frame: art.Frame) -> tuple[int, int, int, int]:
    points = [
        (x, y)
        for y in range(1, 6)
        for x in range(art.LANE_WIDTH)
        if frame[y][x] != art.BLACK
    ]
    return (
        min(x for x, _ in points),
        min(y for _, y in points),
        max(x for x, _ in points),
        max(y for _, y in points),
    )


def manhattan(a: tuple[int, int], b: tuple[int, int]) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


class InputContractTests(unittest.TestCase):
    def test_exact_domain_names_and_friendly_aliases_are_accepted(self) -> None:
        expected = {
            "idle": art.FocusState.IDLE,
            "focus_running": art.FocusState.FOCUS_RUNNING,
            "focus_paused": art.FocusState.FOCUS_PAUSED,
            "focus_done_cue": art.FocusState.FOCUS_DONE_CUE,
            "short_break_running": art.FocusState.SHORT_BREAK_RUNNING,
            "long_break_running": art.FocusState.LONG_BREAK_RUNNING,
            "break_paused": art.FocusState.BREAK_PAUSED,
            "ready_next": art.FocusState.READY_NEXT,
            "focus": art.FocusState.FOCUS_RUNNING,
            "long break": art.FocusState.LONG_BREAK_RUNNING,
            "ready": art.FocusState.READY_NEXT,
            "recovered-paused": art.FocusState.FOCUS_PAUSED,
        }
        for value, state in expected.items():
            with self.subTest(value=value):
                self.assertIs(art.normalize_state(value), state)
        self.assertIs(
            art.normalize_state(ForeignPhase.FOCUS), art.FocusState.FOCUS_RUNNING
        )
        with self.assertRaises(ValueError):
            art.normalize_state("unknown")

    def test_generic_snapshot_adapter_reads_truthful_domain_fields(self) -> None:
        snapshot = ForeignSnapshot(ForeignPhase.FOCUS, 712.5, 1_200, 7)
        visual = art.coerce_sprint(snapshot)
        self.assertIs(visual.state, art.FocusState.FOCUS_RUNNING)
        self.assertEqual(visual.remaining_seconds, 712.5)
        self.assertEqual(visual.total_seconds, 1_200)
        self.assertEqual(visual.cycle, 7)
        self.assertIs(visual.motion, art.Motion.CALM)

        overridden = art.coerce_sprint(snapshot, total=900, cycle=2, motion="static")
        self.assertEqual(overridden.total_seconds, 900)
        self.assertEqual(overridden.cycle, 2)
        self.assertIs(overridden.motion, art.Motion.STATIC)

    def test_recovery_reason_selects_paused_art(self) -> None:
        focus = art.coerce_sprint(
            {"phase": "focus_running", "pause_reason": "recovered_after_restart"}
        )
        rest = art.coerce_sprint(
            {"phase": "long_break_running", "pause_reason": "recovered_paused"},
            total=900,
        )
        self.assertIs(focus.state, art.FocusState.FOCUS_PAUSED)
        self.assertIs(rest.state, art.FocusState.BREAK_PAUSED)
        self.assertTrue(rest.is_long_break)
        cue = art.coerce_sprint(
            {
                "phase": "focus_done_cue",
                "remaining_seconds": 4,
                "phase_total_seconds": 5,
                "pause_reason": "recovered",
            }
        )
        self.assertIs(cue.state, art.FocusState.FOCUS_PAUSED)
        self.assertEqual(cue.remaining_seconds, 4)

    def test_idle_or_invisible_snapshots_cannot_create_a_page(self) -> None:
        with self.assertRaises(ValueError):
            art.build_payload("idle")
        with self.assertRaises(ValueError):
            art.build_payload(
                {"phase": "focus_running", "visible": False},
                remaining=20,
                total=30,
            )

    def test_invalid_numeric_and_motion_inputs_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            art.coerce_sprint("focus", total=0)
        with self.assertRaises(ValueError):
            art.coerce_sprint("focus", remaining=float("nan"))
        with self.assertRaises(ValueError):
            art.coerce_sprint("focus", cycle=-1)
        with self.assertRaises(ValueError):
            art.normalize_motion("busy")


class LaneGeometryTests(unittest.TestCase):
    def test_countdown_ceil_is_truthful_and_mmss_is_bounded(self) -> None:
        expected = {
            0: "00:00",
            0.001: "00:01",
            1: "00:01",
            59.1: "01:00",
            60: "01:00",
            1_500: "25:00",
            99 * 60 + 59: "99:59",
            99 * 60 + 100: "1H41",
        }
        for remaining, label in expected.items():
            with self.subTest(remaining=remaining):
                self.assertEqual(art.format_countdown(remaining), label)
                self.assertLessEqual(art.text_width(label), art.LANE_WIDTH)

    def test_timer_is_centered_in_rows_one_through_five(self) -> None:
        for remaining in (1, 61, 11 * 60 + 11, 99 * 60 + 59):
            with self.subTest(remaining=remaining):
                frame = art.lane_frame(
                    "focus_running", remaining=remaining, total=6_000, cycle=0
                )
                x0, y0, x1, y1 = lane_text_bounds(frame)
                self.assertEqual((y0, y1), (1, 5))
                self.assertLessEqual(abs(x0 - (art.LANE_WIDTH - 1 - x1)), 1)

    def test_lane_is_calm_glance_with_one_remaining_meter(self) -> None:
        from visual_system import TRACK as GLANCE_TRACK

        frame = art.lane_frame(
            "focus_running", remaining=601, total=1_200, cycle=2
        )
        self.assertEqual(len(frame), 8)
        self.assertTrue(all(len(row) == 23 for row in frame))
        # No pips on row 0 any more; row 6 blank; row 7 the one meter.
        self.assertTrue(all(color == art.BLACK for color in frame[0]))
        self.assertTrue(all(color == art.BLACK for color in frame[6]))
        expected_rail = art.remaining_rail_pixels(601, 1_200)
        self.assertEqual(
            [x for x, color in enumerate(frame[7]) if max(color) > 90],
            list(range(expected_rail)),
        )

    def test_rail_uses_ceil_never_zeroes_early_and_drains_monotonically(self) -> None:
        self.assertEqual(art.remaining_rail_pixels(0, 1_500), 0)
        for remaining in (0.0001, 0.5, 1, 64):
            self.assertGreater(art.remaining_rail_pixels(remaining, 1_500), 0)
        self.assertEqual(art.remaining_rail_pixels(1_500, 1_500), 23)
        self.assertEqual(art.remaining_rail_pixels(1_499, 1_500), 23)

        draining = [
            art.remaining_rail_pixels(remaining, 1_500)
            for remaining in range(1_500, -1, -1)
        ]
        self.assertTrue(all(a >= b for a, b in zip(draining, draining[1:])))
        self.assertEqual(set(draining), set(range(24)))

    def test_ready_has_centered_text_and_a_plain_track(self) -> None:
        from visual_system import TRACK as GLANCE_TRACK

        frame = art.lane_frame("ready_next", remaining=17, total=300, cycle=3)
        self.assertEqual(art.text_width("READY"), 19)
        self.assertEqual(lane_text_bounds(frame), (2, 1, 20, 5))
        # A plain track: one dark tint, never the old grey.
        self.assertEqual(len(set(frame[7])), 1)
        self.assertLess(max(frame[7][0]), 90)
        self.assertNotEqual(frame[7][0], GLANCE_TRACK)

    def test_lane_rgb888_bytes_are_exact_and_deterministic(self) -> None:
        first = art.lane_bytes(
            "focus_paused", remaining=300, total=1_500, cycle=2
        )
        second = art.lane_bytes(
            "focus_paused", remaining=300, total=1_500, cycle=2
        )
        self.assertEqual(first, second)
        self.assertEqual(len(first), 23 * 8 * 3)


class EmblemAndMotionTests(unittest.TestCase):
    def test_every_visible_state_has_an_exact_8x8_meaningful_emblem(self) -> None:
        for state in art.FocusState:
            if state is art.FocusState.IDLE:
                continue
            with self.subTest(state=state):
                frame = art.icon_frame(state)
                self.assertEqual(len(frame), 8)
                self.assertTrue(all(len(row) == 8 for row in frame))
                self.assertTrue(lit_points(frame))
                self.assertTrue(
                    all(frame[y][x] != art.BLACK for x, y in art.stable_icon_points(state))
                )

    def test_focus_bead_is_two_adjacent_pixels_and_advances_one_pixel(self) -> None:
        path = art.FOCUS_BEAD_PATH
        self.assertEqual(len(set(path)), len(path))
        self.assertTrue(
            all(manhattan(point, path[(index + 1) % len(path)]) == 1 for index, point in enumerate(path))
        )
        for phase in range(len(path)):
            current = art.motion_points("focus_running", phase)
            following = art.motion_points("focus_running", phase + 1)
            self.assertEqual(len(current), 2)
            self.assertEqual(manhattan(*tuple(current)), 1)
            self.assertEqual(len(current & following), 1)
            self.assertEqual(len(current ^ following), 2)

    def test_focus_ring_and_target_never_blink(self) -> None:
        frames = art.icon_frames("focus_running")
        self.assertEqual(len(frames), len(art.FOCUS_BEAD_PATH))
        occupancies = [lit_points(frame) for frame in frames]
        self.assertEqual(len(set(map(frozenset, occupancies))), 1)
        self.assertEqual(occupancies[0], set(art.FOCUS_STABLE))

    def test_break_highlight_moves_one_adjacent_pixel_on_a_dim_path(self) -> None:
        for path in (art.STEAM_PATH_A, art.STEAM_PATH_B):
            self.assertTrue(
                all(manhattan(point, path[(index + 1) % len(path)]) == 1 for index, point in enumerate(path))
            )
        frames = art.icon_frames("short_break_running")
        self.assertEqual(len(frames), len(art.STEAM_PATH_A))
        self.assertEqual(len(set(map(frozenset, map(lit_points, frames)))), 1)
        for phase in range(len(frames)):
            self.assertEqual(len(art.motion_points("short_break_running", phase)), 1)

    def test_long_break_is_same_mug_with_two_steam_paths(self) -> None:
        short = art.stable_icon_points("short_break_running")
        long = art.stable_icon_points("long_break_running")
        self.assertTrue(art.MUG_BODY <= short)
        self.assertTrue(art.MUG_BODY <= long)
        self.assertEqual(long - short, set(art.STEAM_PATH_B))
        for phase in range(len(art.STEAM_PATH_A)):
            self.assertEqual(len(art.motion_points("long_break_running", phase)), 2)

    def test_paused_states_have_same_silhouette_pause_bars_and_no_motion(self) -> None:
        focus_frames = art.icon_frames("focus_paused", motion="calm")
        break_frames = art.icon_frames("break_paused", motion="calm")
        self.assertEqual(len(focus_frames), 1)
        self.assertEqual(len(break_frames), 1)
        self.assertEqual(
            art.icon_frame("focus_paused", 0), art.icon_frame("focus_paused", 99)
        )
        self.assertEqual(
            art.icon_frame("break_paused", 0), art.icon_frame("break_paused", 99)
        )
        self.assertTrue(
            all(focus_frames[0][y][x] == art.GOLD for x, y in art.FOCUS_PAUSE_BARS)
        )
        self.assertTrue(
            all(break_frames[0][y][x] == art.PEARL for x, y in art.BREAK_PAUSE_BARS)
        )
        self.assertFalse(art.motion_points("focus_paused", 0))
        self.assertFalse(art.motion_points("break_paused", 0))

    def test_done_check_is_stable_and_sparkle_finishes_under_two_seconds(self) -> None:
        frames = art.icon_frames("focus_done_cue")
        self.assertEqual(len(frames), len(art.DONE_SPARKLE_STEPS))
        self.assertLess(len(frames) * art.SPARKLE_FRAME_MS, 2_000)
        for frame in frames:
            self.assertTrue(
                all(frame[y][x] == art.MINT for x, y in art.DONE_CHECK)
            )
        self.assertFalse(art.motion_points("focus_done_cue", 0))
        self.assertFalse(
            art.motion_points("focus_done_cue", len(art.DONE_SPARKLE_STEPS) - 1)
        )

    def test_only_the_icon_region_moves_and_the_lane_never_jitters(self) -> None:
        cases = (
            ("focus_running", 700, 1_500, 1),
            ("short_break_running", 240, 300, 1),
            ("long_break_running", 700, 900, 4),
            ("focus_done_cue", 0, 1_500, 4),
        )
        for state, remaining, total, cycle in cases:
            with self.subTest(state=state):
                frames = art.combined_frames(
                    state, remaining=remaining, total=total, cycle=cycle
                )
                art.validate_frames(frames)
                lanes = [tuple(tuple(row[9:]) for row in frame) for frame in frames]
                self.assertEqual(len(set(lanes)), 1)
                self.assertTrue(all(frame[y][8] == art.BLACK for frame in frames for y in range(8)))
                for previous, current in zip(frames, frames[1:]):
                    changed = {
                        (x, y)
                        for y in range(8)
                        for x in range(32)
                        if previous[y][x] != current[y][x]
                    }
                    self.assertTrue(changed)
                    self.assertTrue(all(x < 8 for x, _ in changed))


class EncodingAndPayloadTests(unittest.TestCase):
    @staticmethod
    def decode(data: bytes) -> tuple[list[Image.Image], list[int], int | None]:
        frames: list[Image.Image] = []
        durations: list[int] = []
        with Image.open(BytesIO(data)) as image:
            loop = image.info.get("loop")
            for index in range(image.n_frames):
                image.seek(index)
                frames.append(image.convert("RGBA").copy())
                durations.append(int(image.info.get("duration", 0)))
        return frames, durations, loop

    def test_calm_icons_are_tiny_deterministic_opaque_gifs(self) -> None:
        expected = {
            art.FocusState.FOCUS_RUNNING: (8, art.FOCUS_FRAME_MS, 0),
            art.FocusState.SHORT_BREAK_RUNNING: (6, art.BREAK_FRAME_MS, 0),
            art.FocusState.LONG_BREAK_RUNNING: (6, art.BREAK_FRAME_MS, 0),
            art.FocusState.FOCUS_DONE_CUE: (5, art.SPARKLE_FRAME_MS, None),
        }
        for state, (count, duration, loop) in expected.items():
            with self.subTest(state=state):
                first = art.build_icon_gif(state)
                self.assertEqual(first, art.build_icon_gif(state))
                self.assertLessEqual(len(first), art.ICON_MAX_BYTES)
                frames, durations, actual_loop = self.decode(first)
                self.assertEqual(len(frames), count)
                self.assertEqual(durations, [duration] * count)
                self.assertEqual(actual_loop, loop)
                self.assertTrue(all(frame.size == (8, 8) for frame in frames))
                self.assertTrue(
                    all(alpha == 255 for frame in frames for *_, alpha in frame.getdata())
                )

    def test_paused_ready_and_static_safety_gifs_have_one_opaque_frame(self) -> None:
        states = (
            art.FocusState.FOCUS_PAUSED,
            art.FocusState.BREAK_PAUSED,
            art.FocusState.READY_NEXT,
        )
        for state in states:
            data = art.build_icon_gif(state)
            frames, _, loop = self.decode(data)
            self.assertEqual(len(frames), 1)
            self.assertIsNone(loop)
        for state in art.FocusState:
            if state is art.FocusState.IDLE:
                continue
            data = art.build_icon_gif(state, animated=False)
            frames, _, loop = self.decode(data)
            self.assertEqual(len(frames), 1)
            self.assertIsNone(loop)
            art.validate_icon_gif(data, expected_frames=1, should_loop=False)

    def test_payload_is_fixed_icon_plus_static_23x8_bitmap(self) -> None:
        payload = art.build_payload(
            "focus_running", remaining=731.2, total=1_500, cycle=2
        )
        self.assertEqual(
            set(payload),
            {
                "icon",
                "iconMode",
                "iconOffsetX",
                "draw",
                "backgroundColor",
                "durationMs",
                "lifetimeMs",
                "lifetimeExpiry",
            },
        )
        self.assertEqual(payload["iconMode"], "fixed")
        self.assertEqual(payload["iconOffsetX"], 0)
        self.assertEqual(payload["backgroundColor"], "#000000")
        self.assertEqual(payload["durationMs"], 8_000)
        self.assertEqual(payload["lifetimeMs"], 20_000)
        self.assertEqual(payload["lifetimeExpiry"], "remove")
        operation = payload["draw"][0]
        self.assertEqual(operation[:5], ["bitmap", 9, 0, 23, 8])
        lane = base64.b64decode(operation[5], validate=True)
        self.assertEqual(len(lane), 23 * 8 * 3)
        self.assertEqual(
            lane,
            art.lane_bytes(
                "focus_running", remaining=731.2, total=1_500, cycle=2
            ),
        )
        icon = base64.b64decode(payload["icon"], validate=True)
        art.validate_icon_gif(icon, expected_frames=8, should_loop=True)

    def test_animated_and_static_payloads_differ_only_in_icon_motion(self) -> None:
        animated = art.build_payload(
            "short_break_running",
            remaining=240,
            total=300,
            cycle=1,
            animated=True,
        )
        static = art.build_payload(
            "short_break_running",
            remaining=240,
            total=300,
            cycle=1,
            animated=False,
        )
        self.assertNotEqual(animated["icon"], static["icon"])
        self.assertEqual(
            {key: value for key, value in animated.items() if key != "icon"},
            {key: value for key, value in static.items() if key != "icon"},
        )

    def test_every_payload_variant_is_strictly_below_two_kilobytes(self) -> None:
        worst = 0
        for state in art.FocusState:
            if state is art.FocusState.IDLE:
                continue
            for animated in (True, False):
                with self.subTest(state=state, animated=animated):
                    payload = art.build_payload(
                        state,
                        remaining=123.4,
                        total=900 if state is art.FocusState.LONG_BREAK_RUNNING else 1_500,
                        cycle=4,
                        animated=animated,
                    )
                    size = art.serialized_size(payload)
                    compact_size = len(
                        json.dumps(payload, separators=(",", ":")).encode("utf-8")
                    )
                    worst = max(worst, size)
                    self.assertLess(size, art.PAYLOAD_MAX_BYTES)
                    self.assertLess(compact_size, art.PAYLOAD_MAX_BYTES)
        self.assertLess(worst, 1_600)


class GammaPaletteTests(unittest.TestCase):
    def test_source_palette_declares_gamma_22_and_is_not_pre_darkened(self) -> None:
        self.assertEqual(art.PANEL_GAMMA, 2.2)
        self.assertEqual(art.PANEL_SATURATION, 90)
        self.assertIn(255, art.PEARL)
        self.assertIn(255, art.MINT)
        self.assertIn(255, art.AMBER)

    def test_every_semantic_source_color_survives_qa_brightnesses(self) -> None:
        colors = (
            art.PEARL,
            art.ICE,
            art.SLATE,
            art.TEAL_DIM,
            art.TEAL,
            art.MINT,
            art.STEAM_DIM,
            art.AMBER_DIM,
            art.AMBER,
            art.GOLD,
        )
        for brightness in (45, 10, 3, 1):
            for color in colors:
                with self.subTest(brightness=brightness, color=color):
                    self.assertNotEqual(
                        art.panel_grade_color(color, brightness=brightness), art.BLACK
                    )

    def test_each_stable_silhouette_survives_gamma_at_45_10_3_and_1(self) -> None:
        states = (
            art.FocusState.FOCUS_RUNNING,
            art.FocusState.FOCUS_PAUSED,
            art.FocusState.FOCUS_DONE_CUE,
            art.FocusState.SHORT_BREAK_RUNNING,
            art.FocusState.LONG_BREAK_RUNNING,
            art.FocusState.BREAK_PAUSED,
            art.FocusState.READY_NEXT,
        )
        for brightness in (45, 10, 3, 1):
            for state in states:
                with self.subTest(brightness=brightness, state=state):
                    source = art.icon_frame(state, motion="static")
                    graded = art.panel_grade_frame(source, brightness=brightness)
                    stable = art.stable_icon_points(state)
                    self.assertTrue(
                        all(graded[y][x] != art.BLACK for x, y in stable)
                    )


if __name__ == "__main__":
    unittest.main()
