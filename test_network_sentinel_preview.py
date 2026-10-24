from __future__ import annotations

import base64
from enum import Enum
from io import BytesIO
import json
import unittest

from PIL import Image

import network_sentinel_preview as art


class ForeignState(Enum):
    DNS = "dns"


def lit_bounds(frame: art.Frame) -> tuple[int, int, int, int]:
    points = [
        (x, y)
        for y, row in enumerate(frame)
        for x, color in enumerate(row)
        if color != art.BLACK
    ]
    return (
        min(x for x, _ in points),
        min(y for _, y in points),
        max(x for x, _ in points),
        max(y for _, y in points),
    )


def connected_components(frame: art.Frame) -> list[set[tuple[int, int]]]:
    remaining = {
        (x, y)
        for y, row in enumerate(frame)
        for x, color in enumerate(row)
        if color != art.BLACK
    }
    components: list[set[tuple[int, int]]] = []
    while remaining:
        seed = remaining.pop()
        component = {seed}
        stack = [seed]
        while stack:
            x, y = stack.pop()
            for neighbor in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    component.add(neighbor)
                    stack.append(neighbor)
        components.append(component)
    return components


class StateAndTypographyTests(unittest.TestCase):
    def test_normalization_accepts_strings_and_foreign_string_enums(self) -> None:
        self.assertIs(art.normalize_state("dns"), art.NetworkState.DNS)
        self.assertIs(art.normalize_state(ForeignState.DNS), art.NetworkState.DNS)
        self.assertIs(art.normalize_state("captive portal"), art.NetworkState.LOGIN)
        self.assertIs(art.normalize_state("back"), art.NetworkState.RECOVERY)
        with self.assertRaises(ValueError):
            art.normalize_state("healthy")
        with self.assertRaises(ValueError):
            art.normalize_state(42)  # type: ignore[arg-type]

    def test_canonical_labels_are_exact(self) -> None:
        expected = {
            "router": "ROUTER",
            "wan": "WAN",
            "dns": "DNS",
            "login": "LOGIN",
            "slow": "180MS",
            "loss": "L 30%",
            "recovery": "BACK",
        }
        for state, label in expected.items():
            with self.subTest(state=state):
                self.assertEqual(art.state_label(state), label)

    def test_dynamic_values_are_rounded_clamped_and_still_fit(self) -> None:
        self.assertEqual(art.state_label("slow", latency_ms=179.6), "180MS")
        self.assertEqual(art.state_label("slow", latency_ms=5_000), "999MS")
        self.assertEqual(art.state_label("loss", loss_percent=29.6), "L 30%")
        self.assertEqual(art.state_label("loss", loss_percent=-5), "L 0%")
        for state in art.NetworkState:
            label = art.state_label(state)
            self.assertLessEqual(art.text_width(label), art.LANE_WIDTH)

    def test_every_label_is_centered_in_a_true_black_lane(self) -> None:
        for state in art.NetworkState:
            with self.subTest(state=state):
                frame = art.lane_frame(state)
                self.assertEqual(len(frame), 8)
                self.assertTrue(all(len(row) == 23 for row in frame))
                # Calm glance: text on rows 1-5, rows 0 and 6 black, row 7
                # the meter or plain track line.
                x0, y0, x1, y1 = lit_bounds(frame[:7])
                self.assertLessEqual(abs(x0 - (22 - x1)), 1)
                self.assertEqual((y0, y1), (1, 5))
                self.assertTrue(all(color == art.BLACK for color in frame[0]))
                self.assertTrue(all(color == art.BLACK for color in frame[6]))
                self.assertTrue(all(color != art.BLACK for color in frame[7]))

    def test_longest_label_uses_the_exact_physical_lane(self) -> None:
        frame = art.lane_frame("router")
        self.assertEqual(lit_bounds(frame[:7]), (0, 1, 22, 5))
        self.assertEqual(art.text_width("ROUTER"), 23)


class GeometryAndMotionTests(unittest.TestCase):
    def test_layout_is_exactly_8_plus_black_gutter_plus_23(self) -> None:
        for state in art.NetworkState:
            for phase in range(art.FRAME_COUNT):
                frame = art.combined_frame(state, phase)
                self.assertEqual(len(frame), 8)
                self.assertTrue(all(len(row) == 32 for row in frame))
                self.assertTrue(all(row[8] == art.BLACK for row in frame))
                self.assertEqual(
                    [row[9:] for row in frame],
                    art.lane_frame(state),
                )

    @staticmethod
    def bright_arcs(frame) -> int:
        """How many of dot/inner/middle/outer, from the dot out, are at full colour."""
        full = {art.LABEL_COLORS[state] for state in art.NetworkState} | {art.AMBER}
        count = 0
        for arc in art.WIFI_ARCS:
            if all(frame[y][x] in full for x, y in arc):
                count += 1
            else:
                break
        return count

    def test_wifi_symbol_never_blinks_out(self) -> None:
        # Unlit arcs are a dark tint, never off: the footprint is constant.
        for state in art.NetworkState:
            for frame in art.icon_frames(state):
                lit = {(x, y) for y in range(8) for x in range(8) if frame[y][x] != art.BLACK}
                self.assertEqual(lit, set(art.WIFI_FOOTPRINT), state)

    def test_arcs_are_separate_so_they_read_as_wifi(self) -> None:
        # No pixel of one arc touches another arc side-on, so they never merge.
        owner = {p: i for i, arc in enumerate(art.WIFI_ARCS) for p in arc}
        for (x, y), i in owner.items():
            for n in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                self.assertIn(owner.get(n, i), (i,), f"arc {i} touches arc {owner.get(n)} at {(x, y)}")

    def test_each_fault_shows_how_far_the_signal_gets(self) -> None:
        reach = {state: {self.bright_arcs(f) for f in art.icon_frames(state)} for state in art.NetworkState}
        self.assertLessEqual(max(reach[art.NetworkState.ROUTER]), 1)   # nothing past the PC
        self.assertIn(0, reach[art.NetworkState.ROUTER])               # the dot blinks
        self.assertEqual(reach[art.NetworkState.WAN], {2})             # local link only
        for state in (art.NetworkState.DNS, art.NetworkState.LOGIN):
            self.assertIn(3, reach[state])

    def test_slow_climbs_and_recovery_fills(self) -> None:
        for state in (art.NetworkState.SLOW, art.NetworkState.RECOVERY):
            climb = [self.bright_arcs(f) for f in art.icon_frames(state)]
            self.assertEqual(climb, sorted(climb))
            self.assertLessEqual(climb[0], 1)
            self.assertEqual(climb[-1], 4)
        recovery = art.icon_frame("recovery", art.FRAME_COUNT - 1)
        self.assertTrue(all(recovery[y][x] == art.MINT for x, y in art.WIFI_FOOTPRINT))

    def test_loss_animation_omits_the_truthful_nearest_sixth_bucket(self) -> None:
        self.assertEqual(art.loss_omission_count(30), 2)
        omitted = art.loss_omission_phases(30)
        self.assertEqual(len(omitted), 2)
        actual_percent = 100 * len(omitted) / art.FRAME_COUNT
        self.assertLessEqual(abs(actual_percent - 30), 100 / art.FRAME_COUNT / 2)
        dropped = [self.bright_arcs(art.icon_frame("loss", phase, loss_percent=30)) == 2
                   for phase in range(art.FRAME_COUNT)]
        self.assertEqual(sum(dropped), 2)
        self.assertNotEqual(sorted(omitted), list(range(min(omitted), max(omitted) + 1)))

    def test_recovery_lane_is_the_calm_glance(self) -> None:
        from visual_system import PEARL as GLANCE_PEARL
        lane = art.lane_frame("recovery")
        self.assertEqual({color for row in lane[:7] for color in row} - {art.BLACK}, {GLANCE_PEARL})
        self.assertEqual(len(set(lane[7])), 1)  # one tinted track line
        self.assertLess(max(lane[7][0]), 90)

    def test_diagnostic_lane_never_changes_across_motion_frames(self) -> None:
        for state in art.NetworkState:
            lanes = [
                tuple(tuple(row[9:]) for row in art.combined_frame(state, phase))
                for phase in range(6)
            ]
            self.assertEqual(len(set(lanes)), 1)


class EncodingAndPayloadTests(unittest.TestCase):
    @staticmethod
    def decode_gif(data: bytes) -> tuple[list[Image.Image], list[int], int | None]:
        frames: list[Image.Image] = []
        durations: list[int] = []
        with Image.open(BytesIO(data)) as image:
            loop = image.info.get("loop")
            for index in range(image.n_frames):
                image.seek(index)
                frames.append(image.convert("RGB").copy())
                durations.append(int(image.info.get("duration", 0)))
        return frames, durations, loop

    def test_animated_icons_are_deterministic_opaque_six_frame_gifs(self) -> None:
        for state in art.NetworkState:
            with self.subTest(state=state):
                first = art.build_icon_gif(state)
                second = art.build_icon_gif(state)
                self.assertEqual(first, second)
                self.assertLessEqual(len(first), art.ICON_MAX_BYTES)
                frames, durations, loop = self.decode_gif(first)
                self.assertEqual(len(frames), 6)
                self.assertEqual(durations, [120] * 6)
                self.assertEqual(loop, 0)
                self.assertTrue(all(frame.size == (8, 8) for frame in frames))
                with Image.open(BytesIO(first)) as image:
                    self.assertNotIn("transparency", image.info)

    def test_static_safety_icon_is_one_opaque_frame(self) -> None:
        data = art.build_icon_gif("wan", animated=False)
        frames, _, loop = self.decode_gif(data)
        self.assertEqual(len(frames), 1)
        self.assertIsNone(loop)
        with Image.open(BytesIO(data)) as image:
            self.assertNotIn("transparency", image.info)

    def test_lane_is_exact_rgb888_and_byte_deterministic(self) -> None:
        for state in art.NetworkState:
            first = art.lane_bytes(state)
            self.assertEqual(first, art.lane_bytes(state))
            self.assertEqual(len(first), 23 * 8 * 3)

    def test_payload_is_one_tiny_fixed_icon_and_one_static_lane(self) -> None:
        payload = art.build_payload("slow", latency_ms=180)
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
        self.assertEqual(payload["durationMs"], 7_000)
        self.assertEqual(payload["lifetimeMs"], 45_000)
        self.assertEqual(payload["lifetimeExpiry"], "remove")
        operation = payload["draw"][0]
        self.assertEqual(operation[:5], ["bitmap", 9, 0, 23, 8])
        self.assertEqual(len(base64.b64decode(operation[5], validate=True)), 23 * 8 * 3)
        self.assertLess(
            len(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
            2_048,
        )

    def test_every_state_payload_stays_below_two_kilobytes(self) -> None:
        for state in art.NetworkState:
            payload = art.build_payload(state)
            size = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
            self.assertLess(size, art.PAYLOAD_MAX_BYTES)
            art.validate_icon_gif(base64.b64decode(payload["icon"], validate=True))

    def test_payload_arguments_are_explicit_and_deterministic(self) -> None:
        first = art.build_payload(
            "loss",
            latency_ms=180,
            loss_percent=30,
            animated=True,
            duration_ms=6_000,
            lifetime_ms=40_000,
        )
        second = art.build_payload(
            art.NetworkState.LOSS,
            latency_ms=180,
            loss_percent=30,
            animated=True,
            duration_ms=6_000,
            lifetime_ms=40_000,
        )
        self.assertEqual(first, second)
        self.assertEqual(first["durationMs"], 6_000)
        self.assertEqual(first["lifetimeMs"], 40_000)


class GammaPaletteTests(unittest.TestCase):
    def test_palette_declares_the_real_panel_gamma(self) -> None:
        self.assertEqual(art.PANEL_GAMMA, 2.2)
        self.assertEqual(art.PANEL_SATURATION, 90)

    def test_semantic_colors_survive_gamma_22_at_day_brightness(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
