"""Deterministic checks for the Windows Now Playing AWTRIX sender."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from io import BytesIO
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image
import requests

import now_playing_push as app
import preview_now_playing as preview


def rgb_pixel(frame: bytes, width: int, x: int, y: int) -> tuple[int, int, int]:
    start = (y * width + x) * 3
    return tuple(frame[start : start + 3])  # type: ignore[return-value]


def response(status_code: int = 200, text: str = "") -> Mock:
    return Mock(status_code=status_code, text=text)


def gif_frames(gif_bytes: bytes) -> tuple[list[bytes], list[int], int]:
    frames: list[bytes] = []
    durations: list[int] = []
    with Image.open(BytesIO(gif_bytes)) as image:
        loop = int(image.info.get("loop", 0))
        for index in range(image.n_frames):
            image.seek(index)
            frames.append(image.convert("RGB").tobytes())
            durations.append(int(image.info.get("duration", 0)))
    return frames, durations, loop


def wire_body_size(payload: dict[str, object]) -> int:
    body = requests.Request("PUT", app.PUSH_URL, json=payload).prepare().body
    if isinstance(body, str):
        return len(body.encode("utf-8"))
    assert body is not None
    return len(body)


def draw_bitmap_pixels(payload: dict[str, object]) -> tuple[tuple[int, int, int], ...]:
    """Decode the exact 24x1 RGB888 bitmap carried by a Now Playing payload."""
    draw = payload.get("draw")
    if not isinstance(draw, list) or len(draw) != 1:
        raise AssertionError("payload must contain exactly one draw operation")
    operation = draw[0]
    expected_prefix = ["bitmap", app.PROGRESS_X, app.HEIGHT - 1, app.PROGRESS_WIDTH, 1]
    if not isinstance(operation, list) or operation[:5] != expected_prefix or len(operation) != 6:
        raise AssertionError(f"unexpected progress bitmap operation: {operation!r}")
    encoded = operation[5]
    if not isinstance(encoded, str):
        raise AssertionError("progress bitmap must be base64 text")
    raw = base64.b64decode(encoded, validate=True)
    if len(raw) != app.PROGRESS_WIDTH * 3:
        raise AssertionError(f"progress bitmap decoded to {len(raw)} bytes")
    return tuple(
        tuple(raw[index : index + 3])  # type: ignore[misc]
        for index in range(0, len(raw), 3)
    )


def hex_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return tuple(int(color[index : index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]


class TypographyTests(unittest.TestCase):
    def test_veridis_fits_the_exact_native_text_viewport(self) -> None:
        self.assertEqual(app.measure_text("VERIDIS"), app.TEXT_WIDTH)

    def test_selected_m83_label_fits_with_room_to_breathe(self) -> None:
        self.assertEqual(app.measure_text("M83"), 13)
        self.assertLess(app.measure_text("M83"), app.TEXT_WIDTH)

    def test_media_text_normalizes_case_accents_and_smart_punctuation(self) -> None:
        self.assertEqual(
            app.normalize_media_text("Beyonc\u00e9 \u2014 Don\u2019t!"),
            "BEYONCE - DON'T!",
        )

    def test_display_text_adds_a_distinct_artist(self) -> None:
        snapshot = app.MediaSnapshot("Midnight City", "M83", "Spotify")
        self.assertEqual(app.display_text(snapshot), "MIDNIGHT CITY - M83")

    def test_display_text_does_not_repeat_artist_already_in_title(self) -> None:
        snapshot = app.MediaSnapshot("M83 - Midnight City", "M83", "Spotify")
        self.assertEqual(app.display_text(snapshot), "M83 - MIDNIGHT CITY")

    def test_artist_name_is_not_confused_with_a_substring(self) -> None:
        snapshot = app.MediaSnapshot("Museum", "Muse", "Spotify")
        self.assertEqual(app.display_text(snapshot), "MUSEUM - MUSE")

    def test_missing_title_has_a_clear_fallback(self) -> None:
        self.assertEqual(app.display_text(app.MediaSnapshot("")), "NOW PLAYING")

    def test_pathological_metadata_ends_with_an_explicit_ellipsis(self) -> None:
        rendered = app.display_text(app.MediaSnapshot("W" * 200))
        self.assertEqual(len(rendered), app.MAX_DISPLAY_TEXT_CHARS)
        self.assertTrue(rendered.endswith("..."))


class ProgressTests(unittest.TestCase):
    def test_progress_bucket_rounds_clamps_and_rejects_invalid_values(self) -> None:
        cases = (
            (None, None),
            (float("nan"), None),
            (float("inf"), None),
            (-2.0, 0),
            (0.0, 0),
            (0.5, 12),
            (1.0, 24),
            (5.0, 24),
        )
        for ratio, expected in cases:
            with self.subTest(ratio=ratio):
                self.assertEqual(app.progress_bucket(ratio), expected)

    def test_every_bucket_and_phase_has_exactly_the_truthful_fill_count(self) -> None:
        track = hex_rgb(app.PROGRESS_TRACK_COLOR)
        for bucket in range(app.PROGRESS_WIDTH + 1):
            for phase in range(app.PROGRESS_SWEEP_PHASES):
                with self.subTest(bucket=bucket, phase=phase):
                    pixels = app.progress_rail_pixels(bucket, phase)
                    assert pixels is not None
                    self.assertEqual(len(pixels), app.PROGRESS_WIDTH)
                    self.assertEqual(sum(pixel != track for pixel in pixels), bucket)
                    self.assertTrue(all(pixel != track for pixel in pixels[:bucket]))
                    self.assertTrue(all(pixel == track for pixel in pixels[bucket:]))

    def test_unknown_progress_has_no_rail_and_out_of_range_buckets_clamp(self) -> None:
        self.assertIsNone(app.progress_rail_pixels(None))
        self.assertIsNone(app.progress_rail_bitmap(None))
        self.assertEqual(app.progress_rail_pixels(-8), app.progress_rail_pixels(0))
        self.assertEqual(
            app.progress_rail_pixels(99),
            app.progress_rail_pixels(app.PROGRESS_WIDTH),
        )

    def test_sweep_leader_moves_right_and_settles_cream_at_the_real_endpoint(self) -> None:
        bucket = 12
        cream = app.RECORD_COLORS["W"]
        endpoints = (
            app.RECORD_COLORS["P"],
            app.RECORD_COLORS["H"],
            cream,
        )
        for phase, endpoint in enumerate(endpoints):
            with self.subTest(phase=phase):
                pixels = app.progress_rail_pixels(bucket, phase)
                assert pixels is not None
                self.assertEqual(pixels.index(cream), bucket - app.PROGRESS_SWEEP_PHASES + phase)
                self.assertEqual(pixels[bucket - 1], endpoint)
                self.assertEqual(pixels[bucket], hex_rgb(app.PROGRESS_TRACK_COLOR))

    def test_small_fills_degrade_without_overshooting_the_endpoint(self) -> None:
        track = hex_rgb(app.PROGRESS_TRACK_COLOR)
        for bucket in (0, 1, 2):
            for phase in range(app.PROGRESS_SWEEP_PHASES):
                with self.subTest(bucket=bucket, phase=phase):
                    pixels = app.progress_rail_pixels(bucket, phase)
                    assert pixels is not None
                    self.assertEqual(sum(pixel != track for pixel in pixels), bucket)
                    self.assertTrue(all(pixel == track for pixel in pixels[bucket:]))
                    if bucket and phase == app.PROGRESS_SWEEP_FINAL_PHASE:
                        self.assertEqual(pixels[bucket - 1], app.RECORD_COLORS["W"])

    def test_rail_bitmap_is_deterministic_exact_rgb888(self) -> None:
        for bucket in range(app.PROGRESS_WIDTH + 1):
            for phase in range(app.PROGRESS_SWEEP_PHASES):
                with self.subTest(bucket=bucket, phase=phase):
                    encoded = app.progress_rail_bitmap(bucket, phase)
                    assert encoded is not None
                    self.assertEqual(encoded, app.progress_rail_bitmap(bucket, phase))
                    raw = base64.b64decode(encoded, validate=True)
                    self.assertEqual(len(raw), app.PROGRESS_WIDTH * 3)
                    expected = app.progress_rail_pixels(bucket, phase)
                    assert expected is not None
                    self.assertEqual(
                        raw,
                        bytes(channel for pixel in expected for channel in pixel),
                    )


class ProgressSweepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.key: app.PayloadKey = ("MIDNIGHT CITY - M83", 12, False)
        self.identity = ("Midnight City", "M83", "Spotify")

    def test_preview_phase_boundaries_match_the_two_followup_times(self) -> None:
        frame_ms = round(app.PROGRESS_SWEEP_FRAME_SECONDS * 1000)
        cases = (
            (-1, 0),
            (0, 0),
            (frame_ms - 1, 0),
            (frame_ms, 1),
            (frame_ms * 2 - 1, 1),
            (frame_ms * 2, app.PROGRESS_SWEEP_FINAL_PHASE),
            (frame_ms * 20, app.PROGRESS_SWEEP_FINAL_PHASE),
        )
        for elapsed_ms, expected in cases:
            with self.subTest(elapsed_ms=elapsed_ms):
                self.assertEqual(app.progress_sweep_phase_at(elapsed_ms), expected)

    def test_positive_progress_schedules_exactly_two_followups(self) -> None:
        sweep = app.ProgressSweep()
        sweep.start(self.key, self.identity, scroll_revision=3, now=10.0)
        self.assertEqual(sweep.key, self.key)
        self.assertEqual(sweep.identity, self.identity)
        self.assertEqual(sweep.scroll_revision, 1)
        self.assertEqual(sweep.next_phase, 1)
        self.assertFalse(
            sweep.due(
                self.key,
                self.identity,
                10.0 + app.PROGRESS_SWEEP_FRAME_SECONDS - 0.001,
            )
        )
        first_at = 10.0 + app.PROGRESS_SWEEP_FRAME_SECONDS
        self.assertTrue(sweep.due(self.key, self.identity, first_at))

        sweep.advance(first_at)
        self.assertEqual(sweep.next_phase, app.PROGRESS_SWEEP_FINAL_PHASE)
        second_at = first_at + app.PROGRESS_SWEEP_FRAME_SECONDS
        self.assertFalse(sweep.due(self.key, self.identity, second_at - 0.001))
        self.assertTrue(sweep.due(self.key, self.identity, second_at))

        sweep.advance(second_at)
        self.assertIsNone(sweep.next_phase)
        self.assertFalse(sweep.due(self.key, self.identity, second_at + 100))

    def test_unknown_or_zero_progress_never_schedules_motion(self) -> None:
        sweep = app.ProgressSweep()
        for bucket in (None, 0):
            with self.subTest(bucket=bucket):
                key: app.PayloadKey = (self.key[0], bucket, False)
                sweep.start(key, self.identity, scroll_revision=1, now=10.0)
                self.assertIsNone(sweep.next_phase)
                self.assertFalse(sweep.due(key, self.identity, 100.0))

    def test_stale_key_or_identity_cannot_emit_a_followup(self) -> None:
        sweep = app.ProgressSweep()
        sweep.start(self.key, self.identity, scroll_revision=0, now=0.0)
        due_at = app.PROGRESS_SWEEP_FRAME_SECONDS
        changed_key: app.PayloadKey = (self.key[0], 13, False)
        changed_identity = ("Genesis", "Justice", "Spotify")
        self.assertFalse(sweep.due(changed_key, self.identity, due_at))
        self.assertFalse(sweep.due(self.key, changed_identity, due_at))
        self.assertTrue(sweep.due(self.key, self.identity, due_at))

    def test_failure_skips_directly_to_the_truthful_settled_phase(self) -> None:
        sweep = app.ProgressSweep()
        sweep.start(self.key, self.identity, scroll_revision=0, now=0.0)
        sweep.settle_after_failure(now=4.0)
        self.assertEqual(sweep.next_phase, app.PROGRESS_SWEEP_FINAL_PHASE)
        self.assertTrue(sweep.due(self.key, self.identity, 4.0))
        sweep.advance(4.0)
        self.assertIsNone(sweep.next_phase)


class RecordIconTests(unittest.TestCase):
    def setUp(self) -> None:
        self.playing = app.build_record_icon_gif(paused=False)
        self.paused = app.build_record_icon_gif(paused=True)

    def test_playing_icon_is_exactly_twelve_8_by_8_frames(self) -> None:
        frames, durations, loop = gif_frames(self.playing)
        self.assertEqual(len(frames), app.RECORD_FRAME_COUNT)
        self.assertEqual(app.RECORD_FRAME_COUNT, 12)
        self.assertEqual({len(frame) for frame in frames}, {8 * 8 * 3})
        self.assertEqual(durations, [app.RECORD_FRAME_MS] * app.RECORD_FRAME_COUNT)
        self.assertEqual(app.RECORD_FRAME_MS, 150)
        self.assertEqual(loop, 0)

    def test_paused_icon_is_one_static_8_by_8_frame(self) -> None:
        frames, durations, _ = gif_frames(self.paused)
        self.assertEqual(len(frames), 1)
        self.assertEqual(len(frames[0]), 8 * 8 * 3)
        self.assertEqual(durations, [round(app.PAUSE_GRACE_SECONDS * 1000)])

    def test_icon_is_opaque_and_has_no_transparency_metadata(self) -> None:
        for gif_bytes in (self.playing, self.paused):
            with Image.open(BytesIO(gif_bytes)) as image:
                self.assertNotIn("transparency", image.info)
                for index in range(image.n_frames):
                    image.seek(index)
                    self.assertEqual(image.convert("RGBA").getextrema()[3], (255, 255))

    def test_record_silhouette_never_wobbles(self) -> None:
        frames, _, _ = gif_frames(self.playing)
        silhouettes = []
        for frame in frames:
            silhouettes.append(
                {
                    (x, y)
                    for y in range(8)
                    for x in range(8)
                    if rgb_pixel(frame, 8, x, y) != app.BLACK
                }
            )
        self.assertTrue(all(shape == silhouettes[0] for shape in silhouettes[1:]))

    def test_groove_comet_moves_clockwise_in_one_continuous_revolution(self) -> None:
        frames, _, _ = gif_frames(self.playing)
        leader = app.RECORD_COLORS["W"]
        leader_positions: list[tuple[int, int]] = []
        for frame in frames:
            positions = [
                (x, y)
                for y in range(8)
                for x in range(8)
                if rgb_pixel(frame, 8, x, y) == leader
            ]
            self.assertEqual(len(positions), 1)
            leader_positions.append(positions[0])

        path_indices = [app.RECORD_GLINT_PATH.index(position) for position in leader_positions]
        clockwise_steps = [
            (following - current) % len(app.RECORD_GLINT_PATH)
            for current, following in zip(path_indices, path_indices[1:] + path_indices[:1])
        ]
        self.assertEqual(clockwise_steps, [1, 2, 1, 1, 2, 1, 1, 2, 1, 1, 2, 1])
        self.assertEqual(sum(clockwise_steps), len(app.RECORD_GLINT_PATH))

    def test_comet_and_rotating_label_have_bounded_adjacent_changes(self) -> None:
        frames, _, _ = gif_frames(self.playing)
        accent_colors = {
            app.RECORD_COLORS["P"],
            app.RECORD_COLORS["H"],
            app.RECORD_COLORS["W"],
        }
        label_colors = {app.RECORD_COLORS["G"], app.RECORD_COLORS["A"]}
        label_path = {(3, 2), (4, 2), (5, 3), (5, 4), (4, 5), (3, 5), (2, 4), (2, 3)}
        label_patterns: set[frozenset[tuple[int, int]]] = set()

        accents: list[set[tuple[int, int]]] = []
        for frame in frames:
            accent = {
                (x, y)
                for y in range(8)
                for x in range(8)
                if rgb_pixel(frame, 8, x, y) in accent_colors
            }
            labels = {
                (x, y)
                for y in range(8)
                for x in range(8)
                if rgb_pixel(frame, 8, x, y) in label_colors
            }
            self.assertEqual(len(accent), 4)
            self.assertEqual(labels, label_path)
            self.assertEqual(
                {position for position in labels if rgb_pixel(frame, 8, *position) == app.RECORD_COLORS["G"]}
                | {position for position in labels if rgb_pixel(frame, 8, *position) == app.RECORD_COLORS["A"]},
                label_path,
            )
            self.assertEqual(
                sum(rgb_pixel(frame, 8, *position) == app.RECORD_COLORS["G"] for position in labels),
                4,
            )
            label_patterns.add(
                frozenset(
                    position
                    for position in labels
                    if rgb_pixel(frame, 8, *position) == app.RECORD_COLORS["G"]
                )
            )
            accents.append(accent)

        # All eight label orientations occur, so the disc itself visibly turns.
        self.assertEqual(len(label_patterns), 8)
        for index, (left, right) in enumerate(zip(frames, frames[1:] + frames[:1])):
            self.assertIn(len(accents[index] & accents[(index + 1) % len(accents)]), (2, 3))
            changed = {
                (x, y)
                for y in range(8)
                for x in range(8)
                if rgb_pixel(left, 8, x, y) != rgb_pixel(right, 8, x, y)
            }
            self.assertGreaterEqual(len(changed), 4)
            self.assertLessEqual(len(changed), 8)

        hub = {(3, 3), (4, 3), (3, 4), (4, 4)}
        for frame in frames:
            self.assertTrue(all(rgb_pixel(frame, 8, x, y) == app.BLACK for x, y in hub))

    def test_paused_art_contains_two_gold_bars(self) -> None:
        frames, _, _ = gif_frames(self.paused)
        gold = app.RECORD_COLORS["A"]
        positions = {
            (x, y)
            for y in range(8)
            for x in range(8)
            if rgb_pixel(frames[0], 8, x, y) == gold
        }
        self.assertEqual(positions, {(2, 4), (5, 4), (2, 5), (5, 5)})

    def test_gif_build_is_byte_deterministic(self) -> None:
        self.assertEqual(self.playing, app.build_record_icon_gif(paused=False))
        self.assertEqual(self.paused, app.build_record_icon_gif(paused=True))

    def test_icon_stays_well_below_the_firmware_risk_budget(self) -> None:
        self.assertLessEqual(len(self.playing), app.RECORD_GIF_MAX_BYTES)
        self.assertLessEqual(len(self.paused), app.RECORD_GIF_MAX_BYTES)


class NativePayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.icon = app.build_record_icon_gif(paused=False)

    def payload(self, text: str = "M83", bucket: int | None = 12) -> dict[str, object]:
        return app.now_playing_payload(text, bucket, self.icon)

    def test_payload_uses_native_8_by_8_icon_and_23_pixel_text_layout(self) -> None:
        payload = self.payload()
        encoded = str(payload["icon"])
        # AWTRIX treats strings of 64 characters or fewer as filesystem IDs.
        # Keep this safely on the inline-image side of that parser boundary.
        self.assertGreater(len(encoded), 64)
        decoded = base64.b64decode(encoded)
        with Image.open(BytesIO(decoded)) as icon:
            self.assertEqual(icon.size, (8, 8))
        self.assertEqual(app.TEXT_X, 9)
        self.assertEqual(app.TEXT_WIDTH, 23)
        self.assertEqual(app.PROGRESS_X, 8)
        self.assertEqual(app.PROGRESS_WIDTH, 24)
        self.assertEqual(payload["iconMode"], "fixed")
        self.assertEqual(payload["iconOffsetX"], 0)

    def test_payload_matches_the_selected_coral_gold_and_cream_design(self) -> None:
        payload = self.payload()
        self.assertEqual(payload["textColor"], "#FFF4EA")
        self.assertEqual(payload["backgroundColor"], "#000000")
        pixels = draw_bitmap_pixels(payload)
        self.assertEqual(pixels[0], hex_rgb(app.PROGRESS_PALETTE[0]))
        self.assertEqual(pixels[11], app.RECORD_COLORS["W"])
        self.assertEqual(pixels[12:], (hex_rgb(app.PROGRESS_TRACK_COLOR),) * 12)
        for deprecated_key in ("progress", "progressColor", "progressTrackColor", "palette"):
            self.assertNotIn(deprecated_key, payload)

    def test_native_loop_guarantees_one_complete_seamless_pass(self) -> None:
        payload = self.payload("A VERY LONG TRACK TITLE - AN ARTIST")
        self.assertEqual(
            payload["scroll"],
            {
                "mode": "loop",
                "direction": "left",
                "entry": "inline",
                "whenFits": "static",
                "speed": app.SCROLL_SPEED,
                "gap": app.SCROLL_GAP,
                "holdMs": app.SCROLL_HOLD_MS,
            },
        )
        self.assertEqual(app.SCROLL_SPEED, 100)
        self.assertEqual(payload["repeat"], 1)
        self.assertNotIn("durationMs", payload)

    def test_native_speed_has_a_regular_one_led_cadence(self) -> None:
        # AWTRIX NG v1.1.0 advances 0.5 * speed/100 logical pixels per
        # 24ms render tick and floors the left-moving x coordinate. At speed
        # 100, every visible one-LED shift therefore lands exactly two ticks
        # apart instead of alternating between shorter and longer pauses.
        delta = 0.5 * app.SCROLL_SPEED / 100
        last_pixel = 0
        movement_ticks: list[int] = []
        for tick in range(1, 80):
            pixel = math.floor(-tick * delta)
            if pixel != last_pixel:
                movement_ticks.append(tick)
                last_pixel = pixel
        intervals = [right - left for left, right in zip(movement_ticks, movement_ticks[1:])]
        self.assertGreater(len(intervals), 20)
        self.assertEqual(set(intervals), {2})

    def test_short_titles_are_centered_and_static(self) -> None:
        payload = self.payload("M83")
        self.assertTrue(payload["textCenter"])
        self.assertEqual(payload["scroll"]["whenFits"], "static")  # type: ignore[index]

    def test_new_identity_can_toggle_scroll_revision_to_force_a_clean_start(self) -> None:
        self.assertEqual(app.measure_text("ABC"), app.measure_text("DEF"))
        first = app.now_playing_payload("ABC", 12, self.icon, scroll_revision=0)
        second = app.now_playing_payload("DEF", 12, self.icon, scroll_revision=1)
        self.assertEqual(first["scroll"]["holdMs"], app.SCROLL_HOLD_MS)  # type: ignore[index]
        self.assertEqual(second["scroll"]["holdMs"], app.SCROLL_HOLD_MS + 1)  # type: ignore[index]

    def test_long_title_is_native_text_not_rasterized_into_the_icon(self) -> None:
        short = self.payload("M83")
        long = self.payload("A VERY LONG TRACK TITLE - A VERY LONG ARTIST")
        self.assertNotEqual(short["text"], long["text"])
        self.assertEqual(short["icon"], long["icon"])
        self.assertEqual(short["scroll"], long["scroll"])

    def test_only_draw_changes_across_every_progress_bucket_and_phase(self) -> None:
        reference = self.payload("MIDNIGHT CITY - M83", None)
        stable_reference = {key: value for key, value in reference.items() if key != "draw"}
        for bucket in [None, *range(app.PROGRESS_WIDTH + 1)]:
            for phase in range(app.PROGRESS_SWEEP_PHASES):
                with self.subTest(bucket=bucket, phase=phase):
                    payload = app.now_playing_payload(
                        "MIDNIGHT CITY - M83",
                        bucket,
                        self.icon,
                        progress_phase=phase,
                    )
                    stable = {key: value for key, value in payload.items() if key != "draw"}
                    self.assertEqual(stable, stable_reference)
                    if bucket is None:
                        self.assertNotIn("draw", payload)
                    else:
                        pixels = draw_bitmap_pixels(payload)
                        self.assertEqual(pixels, app.progress_rail_pixels(bucket, phase))

    def test_phase_only_payloads_keep_icon_text_scroll_and_timing_byte_identical(self) -> None:
        phases = [
            app.now_playing_payload(
                "MIDNIGHT CITY - M83",
                12,
                self.icon,
                scroll_revision=1,
                progress_phase=phase,
            )
            for phase in range(app.PROGRESS_SWEEP_PHASES)
        ]
        stable_keys = set(phases[0]) - {"draw"}
        for left, right in zip(phases, phases[1:]):
            self.assertEqual(
                {key: left[key] for key in stable_keys},
                {key: right[key] for key in stable_keys},
            )
            self.assertNotEqual(left["draw"], right["draw"])

    def test_unknown_progress_omits_the_draw_operation_entirely(self) -> None:
        payload = self.payload(bucket=None)
        self.assertNotIn("draw", payload)
        for deprecated_key in ("progress", "progressColor", "progressTrackColor", "palette"):
            self.assertNotIn(deprecated_key, payload)

    def test_zero_and_full_progress_are_exact_24_pixel_bitmaps(self) -> None:
        track = hex_rgb(app.PROGRESS_TRACK_COLOR)
        zero = draw_bitmap_pixels(self.payload(bucket=0))
        full = draw_bitmap_pixels(self.payload(bucket=app.PROGRESS_WIDTH))
        self.assertEqual(zero, (track,) * app.PROGRESS_WIDTH)
        self.assertTrue(all(pixel != track for pixel in full))
        self.assertEqual(full[-1], app.RECORD_COLORS["W"])

    def test_lifetime_safely_exceeds_keepalive(self) -> None:
        payload = self.payload()
        self.assertEqual(payload["lifetimeMs"], app.LIFETIME_MS)
        self.assertEqual(payload["lifetimeExpiry"], "remove")
        self.assertGreater(app.LIFETIME_MS, app.KEEPALIVE_SECONDS * 1000)

    def test_worst_case_wire_body_remains_tiny(self) -> None:
        text = app.limit_display_text("W" * 200)
        for paused in (False, True):
            icon = app.build_record_icon_gif(paused=paused)
            for bucket in [None, *range(app.PROGRESS_WIDTH + 1)]:
                for phase in range(app.PROGRESS_SWEEP_PHASES):
                    with self.subTest(paused=paused, bucket=bucket, phase=phase):
                        payload = app.now_playing_payload(
                            text,
                            bucket,
                            icon,
                            paused=paused,
                            progress_phase=phase,
                        )
                        self.assertLess(wire_body_size(payload), 2_048)

    def test_invalid_icon_is_rejected_before_network_use(self) -> None:
        with self.assertRaisesRegex(ValueError, "GIF"):
            app.now_playing_payload("M83", 12, b"not an icon")

    def test_valid_full_screen_gif_is_rejected_at_the_payload_boundary(self) -> None:
        output = BytesIO()
        Image.new("RGB", (32, 8), "#FF5275").save(output, format="GIF")
        with self.assertRaisesRegex(ValueError, "exactly 8x8"):
            app.now_playing_payload("M83", 12, output.getvalue())

    def test_oversize_malformed_and_wrong_frame_count_icons_are_rejected(self) -> None:
        oversize = b"GIF89a" + bytes(app.RECORD_GIF_MAX_BYTES)
        with self.assertRaisesRegex(ValueError, "exceeds"):
            app.now_playing_payload("M83", 12, oversize)

        with self.assertRaisesRegex(ValueError, "readable GIF"):
            app.now_playing_payload("M83", 12, b"GIF89abroken")

        output = BytesIO()
        first = Image.new("RGB", (8, 8), "#FF5275")
        second = Image.new("RGB", (8, 8), "#FFB72F")
        first.save(
            output,
            format="GIF",
            save_all=True,
            append_images=[second],
            duration=[180, 180],
            loop=0,
        )
        with self.assertRaisesRegex(ValueError, "2 frames"):
            app.now_playing_payload("M83", 12, output.getvalue())


class PreviewLayoutTests(unittest.TestCase):
    def test_preview_frame_is_exactly_32_by_8_rgb888(self) -> None:
        frame = app.render_frame("M83", 0.5, elapsed_ms=0)
        self.assertEqual(len(frame), 32 * 8 * 3)

    def test_record_occupies_only_the_left_eight_columns(self) -> None:
        frame = app.render_frame("M83", None, elapsed_ms=0)
        icon = app.render_record_icon_frame(0)
        for y in range(8):
            for x in range(8):
                self.assertEqual(
                    rgb_pixel(frame, 32, x, y),
                    rgb_pixel(icon, 8, x, y),
                )
        self.assertEqual(rgb_pixel(frame, 32, 8, 3), app.BLACK)

    def test_progress_is_confined_to_row_seven_columns_eight_through_31(self) -> None:
        frame = app.render_frame("M83", 0.5, elapsed_ms=0)
        track = tuple(int(app.PROGRESS_TRACK_COLOR[index : index + 2], 16) for index in (1, 3, 5))
        self.assertNotEqual(rgb_pixel(frame, 32, 8, 7), app.BLACK)
        self.assertEqual(rgb_pixel(frame, 32, 31, 7), track)
        self.assertEqual(rgb_pixel(frame, 32, 8, 6), app.BLACK)

    def test_settled_progress_always_reaches_the_cream_endpoint(self) -> None:
        frame = app.render_frame("M83", 0.5, elapsed_ms=0)
        bucket = app.progress_bucket(0.5)
        assert bucket is not None
        self.assertEqual(
            rgb_pixel(frame, 32, app.PROGRESS_X + bucket - 1, 7),
            app.RECORD_COLORS["W"],
        )

    def test_progress_sweep_changes_only_the_bottom_rail(self) -> None:
        frames = [
            app.render_frame(
                "M83",
                0.5,
                elapsed_ms=0,
                progress_phase=phase,
            )
            for phase in range(app.PROGRESS_SWEEP_PHASES)
        ]
        bucket = app.progress_bucket(0.5)
        assert bucket is not None
        expected_endpoints = (
            app.RECORD_COLORS["P"],
            app.RECORD_COLORS["H"],
            app.RECORD_COLORS["W"],
        )
        for phase, frame in enumerate(frames):
            self.assertEqual(
                rgb_pixel(frame, app.WIDTH, app.PROGRESS_X + bucket - 1, app.HEIGHT - 1),
                expected_endpoints[phase],
            )
            for y in range(app.HEIGHT - 1):
                for x in range(app.WIDTH):
                    self.assertEqual(
                        rgb_pixel(frame, app.WIDTH, x, y),
                        rgb_pixel(frames[0], app.WIDTH, x, y),
                    )
        self.assertNotEqual(frames[0], frames[1])
        self.assertNotEqual(frames[1], frames[2])

    def test_short_title_stays_fixed_while_record_rotates(self) -> None:
        first = app.render_frame("M83", 0.5, elapsed_ms=0)
        later = app.render_frame("M83", 0.5, elapsed_ms=750)
        for y in range(1, 6):
            for x in range(app.TEXT_X, app.WIDTH):
                self.assertEqual(
                    rgb_pixel(first, 32, x, y),
                    rgb_pixel(later, 32, x, y),
                )
        def icon_region(frame: bytes) -> bytes:
            return b"".join(
                frame[(y * app.WIDTH) * 3 : (y * app.WIDTH + app.ICON_WIDTH) * 3]
                for y in range(app.HEIGHT)
            )

        self.assertNotEqual(icon_region(first), icon_region(later))

    def test_long_title_holds_then_scrolls(self) -> None:
        held = app.render_frame("A VERY LONG TRACK TITLE", None, elapsed_ms=0)
        still_held = app.render_frame(
            "A VERY LONG TRACK TITLE", None, elapsed_ms=app.SCROLL_HOLD_MS - 1
        )
        moving = app.render_frame(
            "A VERY LONG TRACK TITLE", None, elapsed_ms=app.SCROLL_HOLD_MS + 500
        )
        def title_region(frame: bytes) -> bytes:
            return b"".join(
                frame[(y * 32 + app.TEXT_X) * 3 : (y * 32 + 32) * 3]
                for y in range(1, 6)
            )
        self.assertEqual(title_region(held), title_region(still_held))
        self.assertNotEqual(title_region(held), title_region(moving))

    def test_preview_sampling_never_skips_over_a_physical_text_column(self) -> None:
        pixels_per_second = 21 * app.SCROLL_SPEED / 100
        distance_per_sample = pixels_per_second * preview.PREVIEW_FRAME_MS / 1000
        self.assertLessEqual(distance_per_sample, 1.0)

        title = "W" * 16
        period = app.measure_text(title) + app.SCROLL_GAP
        fold_ms = math.ceil(period * 1000 / pixels_per_second)
        positions = [
            math.floor(
                app.TEXT_X
                - ((elapsed_ms - app.SCROLL_HOLD_MS) * pixels_per_second / 1000)
            )
            for elapsed_ms in range(
                app.SCROLL_HOLD_MS,
                app.SCROLL_HOLD_MS + fold_ms,
                preview.PREVIEW_FRAME_MS,
            )
        ]
        visible_steps = [left - right for left, right in zip(positions, positions[1:])]
        self.assertTrue(visible_steps)
        self.assertTrue(all(step in (0, 1) for step in visible_steps))

    def test_loop_preview_never_leaves_the_text_viewport_empty(self) -> None:
        title = "W" * 8
        self.assertGreater(app.measure_text(title), app.TEXT_WIDTH)
        frames, durations = preview._panel_frames(title, None, paused=False)
        self.assertEqual(set(durations), {preview.PREVIEW_FRAME_MS})
        for index, image in enumerate(frames):
            frame = image.tobytes()
            visible_text = any(
                rgb_pixel(frame, app.WIDTH, x, y) == app.TEXT_COLOR
                for y in range(1, 6)
                for x in range(app.TEXT_X, app.WIDTH)
            )
            self.assertTrue(visible_text, f"empty text viewport in preview frame {index}")


class TrackActivationTests(unittest.TestCase):
    def test_one_activation_is_pending_per_media_identity(self) -> None:
        activation = app.TrackActivation()
        first = app.MediaSnapshot("Veridis", "Daft Punk", "Spotify")
        same = app.MediaSnapshot("Veridis", "Daft Punk", "Spotify", 0.75)
        second = app.MediaSnapshot("Genesis", "Justice", "Spotify")

        self.assertTrue(activation.observe(first))
        self.assertTrue(activation.pending)
        self.assertFalse(activation.observe(same))
        activation.complete()
        self.assertFalse(activation.pending)
        self.assertFalse(activation.observe(same))
        self.assertTrue(activation.observe(second))
        activation.clear()
        self.assertIsNone(activation.identity)
        self.assertFalse(activation.pending)

    def test_scroll_revision_tracks_last_successful_identity_not_observation_count(self) -> None:
        last_identity = ("Old", "Artist", "Spotify")
        first_unpushed = ("Skipped", "Artist", "Spotify")
        final_unpushed = ("Final", "Artist", "Spotify")
        self.assertEqual(app.scroll_revision_for(first_unpushed, last_identity, 0), 1)
        self.assertEqual(app.scroll_revision_for(final_unpushed, last_identity, 0), 1)
        self.assertEqual(app.scroll_revision_for(last_identity, last_identity, 0), 0)


class PayloadPushPolicyTests(unittest.TestCase):
    def test_unchanged_state_waits_for_keepalive(self) -> None:
        identity = ("Veridis", "Daft Punk", "Spotify")
        key = ("VERIDIS - DAFT PUNK", 12, False)
        for age in (0.0, app.PROGRESS_REFRESH_SECONDS, app.KEEPALIVE_SECONDS - 0.001):
            with self.subTest(age=age):
                self.assertFalse(app.payload_push_due(key, key, identity, identity, age))
        self.assertTrue(
            app.payload_push_due(key, key, identity, identity, app.KEEPALIVE_SECONDS)
        )

    def test_progress_change_is_rate_limited_to_ten_seconds(self) -> None:
        identity = ("Veridis", "Daft Punk", "Spotify")
        previous = ("VERIDIS - DAFT PUNK", 12, False)
        changed = (previous[0], 13, False)
        self.assertFalse(
            app.payload_push_due(
                changed, previous, identity, identity, app.PROGRESS_REFRESH_SECONDS - 0.001
            )
        )
        self.assertTrue(
            app.payload_push_due(
                changed, previous, identity, identity, app.PROGRESS_REFRESH_SECONDS
            )
        )

    def test_identity_title_and_pause_changes_push_immediately(self) -> None:
        identity = ("Veridis", "Daft Punk", "Spotify")
        previous = ("VERIDIS - DAFT PUNK", 12, False)
        cases = (
            (("GENESIS - JUSTICE", 12, False), identity, identity),
            ((previous[0], 12, True), identity, identity),
            (previous, ("Genesis", "Justice", "Spotify"), identity),
        )
        for key, desired_identity, old_identity in cases:
            with self.subTest(key=key, identity=desired_identity):
                self.assertTrue(
                    app.payload_push_due(key, previous, desired_identity, old_identity, 0.0)
                )


class TimelineTests(unittest.TestCase):
    def test_progress_advances_from_windows_last_updated_time(self) -> None:
        now = datetime(2026, 8, 9, 18, 0, tzinfo=timezone.utc)
        timeline = SimpleNamespace(
            start_time=timedelta(seconds=0),
            end_time=timedelta(seconds=200),
            position=timedelta(seconds=50),
            last_updated_time=now - timedelta(seconds=10),
        )
        self.assertAlmostEqual(
            app.progress_from_timeline(timeline, SimpleNamespace(playback_rate=1.5), now),
            0.325,
        )

    def test_progress_accounts_for_a_nonzero_start_time(self) -> None:
        timeline = SimpleNamespace(
            start_time=timedelta(seconds=20),
            end_time=timedelta(seconds=120),
            position=timedelta(seconds=45),
            last_updated_time=None,
        )
        self.assertEqual(app.progress_from_timeline(timeline, SimpleNamespace()), 0.25)

    def test_live_or_invalid_timeline_has_no_fake_progress(self) -> None:
        live = SimpleNamespace(
            start_time=timedelta(0),
            end_time=timedelta(0),
            position=timedelta(0),
            last_updated_time=None,
        )
        self.assertIsNone(app.progress_from_timeline(live, SimpleNamespace()))
        self.assertIsNone(app.progress_from_timeline(None, SimpleNamespace()))

    def test_progress_clamps_beyond_the_end(self) -> None:
        timeline = SimpleNamespace(
            start_time=timedelta(0),
            end_time=timedelta(seconds=10),
            position=timedelta(seconds=50),
            last_updated_time=None,
        )
        self.assertEqual(app.progress_from_timeline(timeline, SimpleNamespace()), 1.0)


class SessionSelectionTests(unittest.TestCase):
    @staticmethod
    def make_session(status: str, updated: datetime) -> Mock:
        session = Mock()
        session.get_playback_info.return_value = SimpleNamespace(playback_status=status)
        session.get_timeline_properties.return_value = SimpleNamespace(last_updated_time=updated)
        return session

    def test_windows_current_session_wins_when_it_is_playing(self) -> None:
        now = datetime.now(timezone.utc)
        current = self.make_session("PLAYING", now - timedelta(minutes=1))
        newer = self.make_session("PLAYING", now)
        manager = Mock()
        manager.get_current_session.return_value = current
        manager.get_sessions.return_value = [current, newer]
        with patch.object(app, "PlaybackStatus", None):
            self.assertIs(app.select_playing_session(manager), current)

    def test_newest_playing_session_is_fallback_when_current_is_paused(self) -> None:
        now = datetime.now(timezone.utc)
        paused = self.make_session("PAUSED", now)
        older = self.make_session("PLAYING", now - timedelta(minutes=1))
        newer = self.make_session("PLAYING", now)
        manager = Mock()
        manager.get_current_session.return_value = paused
        manager.get_sessions.return_value = [older, paused, newer]
        with patch.object(app, "PlaybackStatus", None):
            self.assertIs(app.select_playing_session(manager), newer)

    def test_no_playing_session_returns_none(self) -> None:
        paused = self.make_session("PAUSED", datetime.now(timezone.utc))
        manager = Mock()
        manager.get_current_session.return_value = paused
        manager.get_sessions.return_value = [paused]
        with patch.object(app, "PlaybackStatus", None):
            self.assertIsNone(app.select_playing_session(manager))


class SnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_reads_title_artist_source_and_progress(self) -> None:
        class FakeSession:
            source_app_user_model_id = "SpotifyAB.SpotifyMusic"

            def get_playback_info(self):
                return SimpleNamespace(playback_status="PLAYING", playback_rate=0)

            def get_timeline_properties(self):
                return SimpleNamespace(
                    start_time=timedelta(0),
                    end_time=timedelta(seconds=100),
                    position=timedelta(seconds=25),
                    last_updated_time=None,
                )

            async def try_get_media_properties_async(self):
                return SimpleNamespace(title="Veridis", artist="Daft Punk", album_artist="")

        session = FakeSession()
        manager = Mock()
        manager.get_current_session.return_value = session
        with patch.object(app, "PlaybackStatus", None):
            snapshot = await app.read_snapshot(manager)
        assert snapshot is not None
        self.assertEqual(snapshot.title, "Veridis")
        self.assertEqual(snapshot.artist, "Daft Punk")
        self.assertEqual(snapshot.source, "SpotifyAB.SpotifyMusic")
        self.assertEqual(snapshot.progress_ratio, 0.25)


class PanelTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = app.now_playing_payload(
            "M83", 12, app.build_record_icon_gif(paused=False)
        )

    def test_push_payload_uses_only_the_single_nowplaying_endpoint(self) -> None:
        with patch.object(app.requests, "put", return_value=response(204)) as put:
            with patch.object(app.requests, "get") as get:
                self.assertIsNone(app.push_payload(self.payload))
        put.assert_called_once_with(
            app.PUSH_URL,
            json=self.payload,
            timeout=app.REQUEST_TIMEOUT_SECONDS,
        )
        get.assert_not_called()

    def test_activate_app_requests_one_fast_show(self) -> None:
        with patch.object(app.requests, "put", return_value=response(200)) as put:
            self.assertIsNone(app.activate_app())
        put.assert_called_once_with(
            app.ACTIVE_URL,
            json={"name": app.APP_NAME, "fast": True},
            timeout=app.REQUEST_TIMEOUT_SECONDS,
        )

    def test_delete_accepts_not_found_as_already_absent(self) -> None:
        with patch.object(app.requests, "delete", return_value=response(404)):
            self.assertIsNone(app.delete_app())

    def test_http_validation_errors_are_reported(self) -> None:
        with patch.object(app.requests, "put", return_value=response(422, "bad key")):
            self.assertEqual(app.push_payload(self.payload), "push HTTP 422: bad key")

    def test_transport_errors_are_returned_for_throttled_logging(self) -> None:
        with patch.object(
            app.requests,
            "put",
            side_effect=requests.ConnectionError("offline"),
        ):
            self.assertEqual(app.push_payload(self.payload), "push unreachable: ConnectionError")
            self.assertEqual(app.activate_app(), "activation unreachable: ConnectionError")

    def test_log_throttling_contract(self) -> None:
        logged = [count for count in range(1, 121) if app.should_log_failure(count)]
        self.assertEqual(logged, [1, 5, 60, 120])

    def test_network_failures_use_bounded_exponential_backoff(self) -> None:
        self.assertEqual(
            [app.network_retry_delay(count) for count in range(1, 8)],
            [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0],
        )

    def test_uptime_monitor_tolerates_slow_response_and_clock_drift(self) -> None:
        # The local probe clock includes HTTP response latency while the uptime
        # value was sampled earlier on the panel. A response that arrives nine
        # seconds late must not turn an otherwise increasing uptime into a
        # false reboot and permanently disable the animated record.
        self.assertFalse(app.panel_rebooted_since(100, 10.0, 121, 40.0))

    def test_uptime_monitor_detects_a_direct_rollback(self) -> None:
        # Even a one-second rollback is unambiguous: uptime cannot decrease
        # without the panel restarting, regardless of local probe timing.
        self.assertTrue(app.panel_rebooted_since(100, 10.0, 99, 11.0))
        self.assertTrue(app.panel_rebooted_since(10_000, 10.0, 20, 40.0))

    def test_uptime_monitor_needs_a_complete_baseline(self) -> None:
        self.assertFalse(app.panel_rebooted_since(None, None, 20, 40.0))

    def test_only_a_requested_restart_is_planned(self) -> None:
        self.assertTrue(app.reboot_was_planned("software"))
        self.assertTrue(app.reboot_was_planned(" Software "))
        self.assertFalse(app.reboot_was_planned("panic"))
        self.assertFalse(app.reboot_was_planned(None))  # unreadable stays on the safe side


class SenderRunTests(unittest.IsolatedAsyncioTestCase):
    class StopLoop(Exception):
        pass

    def setUp(self) -> None:
        self.uptime_patcher = patch.object(app, "read_panel_uptime", return_value=None)
        self.uptime_patcher.start()
        self.addCleanup(self.uptime_patcher.stop)

    @staticmethod
    async def immediate_to_thread(function, *args):
        return function(*args)

    async def test_new_identity_is_pushed_before_its_single_activation(self) -> None:
        snapshot = app.MediaSnapshot("Midnight City", "M83", "Spotify", 0.5)
        events: list[str] = []

        async def manager():
            return object()

        async def read(_manager):
            return snapshot

        async def stop_after_first_iteration(_delay):
            raise self.StopLoop

        def cleanup():
            events.append("delete")

        def push(payload):
            events.append("push")
            self.assertEqual(payload["text"], "MIDNIGHT CITY - M83")
            with Image.open(BytesIO(base64.b64decode(payload["icon"]))) as icon:
                self.assertEqual(icon.size, (8, 8))
                self.assertEqual(icon.n_frames, app.RECORD_FRAME_COUNT)

        def activate():
            events.append("activate")

        with patch.object(app, "configure_logging", return_value=Mock()):
            with patch.object(app, "request_media_manager", manager):
                with patch.object(app, "read_snapshot", read):
                    with patch.object(app, "delete_app", cleanup):
                        with patch.object(app, "push_payload", push):
                            with patch.object(app, "activate_app", activate):
                                with patch.object(app.asyncio, "to_thread", self.immediate_to_thread):
                                    with patch.object(app.asyncio, "sleep", stop_after_first_iteration):
                                        with self.assertRaises(self.StopLoop):
                                            await app.run()

        self.assertEqual(events, ["delete", "push", "activate"])

    async def test_progress_sweep_followups_do_not_probe_uptime_or_reactivate(self) -> None:
        snapshot = app.MediaSnapshot("Midnight City", "M83", "Spotify", 0.5)
        clock = 0.0
        payloads: list[dict[str, object]] = []
        activations = 0

        async def manager():
            return object()

        async def read(_manager):
            return snapshot

        async def advance(_delay):
            nonlocal clock
            if clock >= 0.75:
                raise self.StopLoop
            clock += app.PROGRESS_SWEEP_FRAME_SECONDS

        def push(payload):
            payloads.append(dict(payload))

        def activate():
            nonlocal activations
            activations += 1

        with patch.object(app, "configure_logging", return_value=Mock()):
            with patch.object(app, "read_panel_uptime", side_effect=lambda: int(100 + clock)) as uptime:
                with patch.object(app, "request_media_manager", manager):
                    with patch.object(app, "read_snapshot", read):
                        with patch.object(app, "delete_app", return_value=None):
                            with patch.object(app, "push_payload", push):
                                with patch.object(app, "activate_app", activate):
                                    with patch.object(app.time, "monotonic", side_effect=lambda: clock):
                                        with patch.object(app.asyncio, "to_thread", self.immediate_to_thread):
                                            with patch.object(app.asyncio, "sleep", advance):
                                                with self.assertRaises(self.StopLoop):
                                                    await app.run()

        self.assertEqual(len(payloads), app.PROGRESS_SWEEP_PHASES)
        self.assertEqual(activations, 1)
        # One startup probe plus one probe for the meaningful base publish.
        # The two phase-only PUTs are deliberately not paired with health GETs.
        self.assertEqual(uptime.call_count, 2)
        stable = [
            {key: value for key, value in payload.items() if key != "draw"}
            for payload in payloads
        ]
        self.assertTrue(all(payload == stable[0] for payload in stable[1:]))
        self.assertEqual(
            [draw_bitmap_pixels(payload)[11] for payload in payloads],
            [
                app.RECORD_COLORS["P"],
                app.RECORD_COLORS["H"],
                app.RECORD_COLORS["W"],
            ],
        )

    async def test_progress_and_keepalive_updates_preserve_scroll_and_icon_state(self) -> None:
        clock = 0.0
        pushes: list[tuple[float, dict[str, object]]] = []
        activations = 0

        async def manager():
            return object()

        async def read(_manager):
            progress = 0.25 if clock < app.PROGRESS_REFRESH_SECONDS else 0.5
            return app.MediaSnapshot("Midnight City", "M83", "Spotify", progress)

        async def advance(_delay):
            nonlocal clock
            if clock >= 41.0:
                raise self.StopLoop
            clock += app.PROGRESS_SWEEP_FRAME_SECONDS

        def push(payload):
            pushes.append((clock, dict(payload)))

        def activate():
            nonlocal activations
            activations += 1

        with patch.object(app, "configure_logging", return_value=Mock()):
            with patch.object(app, "read_panel_uptime", side_effect=lambda: int(100 + clock)) as uptime:
                with patch.object(app, "request_media_manager", manager):
                    with patch.object(app, "read_snapshot", read):
                        with patch.object(app, "delete_app", return_value=None):
                            with patch.object(app, "push_payload", push):
                                with patch.object(app, "activate_app", activate):
                                    with patch.object(app.time, "monotonic", side_effect=lambda: clock):
                                        with patch.object(app.asyncio, "to_thread", self.immediate_to_thread):
                                            with patch.object(app.asyncio, "sleep", advance):
                                                with self.assertRaises(self.StopLoop):
                                                    await app.run()

        # Each meaningful publish gets phases 0/1/2, then the settled rail is
        # left alone until the ordinary 30-second keepalive.
        self.assertEqual([when for when, _ in pushes], [0.0, 0.25, 0.5, 10.5, 10.75, 11.0, 41.0])
        payloads = [payload for _, payload in pushes]
        self.assertEqual(len(payloads), 7)
        stable = [
            {key: value for key, value in payload.items() if key != "draw"}
            for payload in payloads
        ]
        self.assertTrue(all(payload == stable[0] for payload in stable[1:]))
        self.assertEqual(
            [sum(pixel != hex_rgb(app.PROGRESS_TRACK_COLOR) for pixel in draw_bitmap_pixels(payload)) for payload in payloads],
            [6, 6, 6, 12, 12, 12, 12],
        )
        self.assertEqual(payloads[5]["draw"], payloads[6]["draw"])
        # Startup plus the three meaningful base/keepalive PUTs; the four
        # phase-only frames do not add uptime traffic.
        self.assertEqual(uptime.call_count, 4)
        self.assertEqual(activations, 1)

    async def test_pause_grace_does_not_reload_icon_then_deletes_once(self) -> None:
        snapshot = app.MediaSnapshot("Midnight City", "M83", "Spotify", 0.5)
        samples = iter((snapshot, None, None, None, None))
        clock = 0.0
        events: list[str] = []
        pushed_frame_counts: list[int] = []

        async def manager():
            return object()

        async def read(_manager):
            return next(samples, None)

        async def advance(_delay):
            nonlocal clock
            if clock >= 4.0:
                raise self.StopLoop
            clock += 1.0

        def cleanup():
            events.append("delete")

        def push(payload):
            events.append("push")
            with Image.open(BytesIO(base64.b64decode(payload["icon"]))) as icon:
                pushed_frame_counts.append(icon.n_frames)

        def activate():
            events.append("activate")

        with patch.object(app, "configure_logging", return_value=Mock()):
            with patch.object(app, "request_media_manager", manager):
                with patch.object(app, "read_snapshot", read):
                    with patch.object(app, "delete_app", cleanup):
                        with patch.object(app, "push_payload", push):
                            with patch.object(app, "activate_app", activate):
                                with patch.object(app.time, "monotonic", side_effect=lambda: clock):
                                    with patch.object(app.asyncio, "to_thread", self.immediate_to_thread):
                                        with patch.object(app.asyncio, "sleep", advance):
                                            with self.assertRaises(self.StopLoop):
                                                await app.run()

        # One startup cleanup, one playing publish/activation, then one final
        # cleanup. A transient missing media poll never swaps/reloads the GIF.
        self.assertEqual(events, ["delete", "push", "activate", "delete"])
        self.assertEqual(pushed_frame_counts, [app.RECORD_FRAME_COUNT])

    async def test_failed_track_push_retries_before_activation(self) -> None:
        snapshot = app.MediaSnapshot("Midnight City", "M83", "Spotify", 0.5)
        clock = 0.0
        events: list[str] = []
        attempts = 0

        async def manager():
            return object()

        async def read(_manager):
            return snapshot

        async def advance(_delay):
            nonlocal clock
            if clock >= 1.0:
                raise self.StopLoop
            clock += 1.0

        def cleanup():
            events.append("delete")

        def push(_payload):
            nonlocal attempts
            attempts += 1
            events.append(f"push{attempts}")
            return "push unreachable: ConnectionError" if attempts == 1 else None

        def activate():
            events.append("activate")

        with patch.object(app, "configure_logging", return_value=Mock()):
            with patch.object(app, "request_media_manager", manager):
                with patch.object(app, "read_snapshot", read):
                    with patch.object(app, "delete_app", cleanup):
                        with patch.object(app, "push_payload", push):
                            with patch.object(app, "activate_app", activate):
                                with patch.object(app.time, "monotonic", side_effect=lambda: clock):
                                    with patch.object(app.asyncio, "to_thread", self.immediate_to_thread):
                                        with patch.object(app.asyncio, "sleep", advance):
                                            with self.assertRaises(self.StopLoop):
                                                await app.run()

        self.assertEqual(events, ["delete", "push1", "push2", "activate"])

    async def test_fifth_panel_failure_permanently_falls_back_to_static_icon(self) -> None:
        snapshot = app.MediaSnapshot("Midnight City", "M83", "Spotify", 0.5)
        clock = 0.0
        pushed_frame_counts: list[int] = []
        activations = 0

        async def manager():
            return object()

        async def read(_manager):
            return snapshot

        async def advance(_delay):
            nonlocal clock
            if clock >= 31.0:
                raise self.StopLoop
            clock += 1.0

        def push(payload):
            with Image.open(BytesIO(base64.b64decode(payload["icon"]))) as icon:
                pushed_frame_counts.append(icon.n_frames)
            if len(pushed_frame_counts) <= app.SAFE_MODE_FAILURES:
                return "push unreachable: ConnectionError"
            return None

        def activate():
            nonlocal activations
            activations += 1

        with patch.object(app, "configure_logging", return_value=Mock()):
            with patch.object(app, "request_media_manager", manager):
                with patch.object(app, "read_snapshot", read):
                    with patch.object(app, "delete_app", return_value=None):
                        with patch.object(app, "push_payload", push):
                            with patch.object(app, "activate_app", activate):
                                with patch.object(app.time, "monotonic", side_effect=lambda: clock):
                                    with patch.object(app.asyncio, "to_thread", self.immediate_to_thread):
                                        with patch.object(app.asyncio, "sleep", advance):
                                            with self.assertRaises(self.StopLoop):
                                                await app.run()

        self.assertEqual(
            pushed_frame_counts,
            [app.RECORD_FRAME_COUNT] * app.SAFE_MODE_FAILURES + [1],
        )
        self.assertEqual(activations, 1)

    async def test_fifth_sweep_failure_latches_static_icon_and_settled_rail(self) -> None:
        snapshot = app.MediaSnapshot("Midnight City", "M83", "Spotify", 0.5)
        clock = 0.0
        payloads: list[dict[str, object]] = []
        pushed_frame_counts: list[int] = []
        activations = 0

        async def manager():
            return object()

        async def read(_manager):
            return snapshot

        async def advance(_delay):
            nonlocal clock
            if clock >= 33.0:
                raise self.StopLoop
            clock += 1.0

        def push(payload):
            payloads.append(dict(payload))
            with Image.open(BytesIO(base64.b64decode(payload["icon"]))) as icon:
                pushed_frame_counts.append(icon.n_frames)
            attempt = len(payloads)
            if 2 <= attempt <= app.SAFE_MODE_FAILURES + 1:
                return "push unreachable: ConnectionError"
            return None

        def activate():
            nonlocal activations
            activations += 1

        with patch.object(app, "configure_logging", return_value=Mock()):
            with patch.object(app, "request_media_manager", manager):
                with patch.object(app, "read_snapshot", read):
                    with patch.object(app, "delete_app", return_value=None):
                        with patch.object(app, "push_payload", push):
                            with patch.object(app, "activate_app", activate):
                                with patch.object(app.time, "monotonic", side_effect=lambda: clock):
                                    with patch.object(app.asyncio, "to_thread", self.immediate_to_thread):
                                        with patch.object(app.asyncio, "sleep", advance):
                                            with self.assertRaises(self.StopLoop):
                                                await app.run()

        self.assertEqual(
            pushed_frame_counts,
            [app.RECORD_FRAME_COUNT] * (app.SAFE_MODE_FAILURES + 1) + [1],
        )
        self.assertEqual(
            [draw_bitmap_pixels(payload)[11] for payload in payloads],
            [
                app.RECORD_COLORS["P"],
                app.RECORD_COLORS["H"],
                *([app.RECORD_COLORS["W"]] * app.SAFE_MODE_FAILURES),
            ],
        )
        self.assertEqual(activations, 1)

    async def test_activation_failures_stop_at_the_declared_cap(self) -> None:
        snapshot = app.MediaSnapshot("Midnight City", "M83", "Spotify", 0.5)
        clock = 0.0
        activation_attempts = 0

        async def manager():
            return object()

        async def read(_manager):
            return snapshot

        async def advance(_delay):
            nonlocal clock
            if clock >= 6.0:
                raise self.StopLoop
            clock += 1.0

        def activate():
            nonlocal activation_attempts
            activation_attempts += 1
            return "activation unreachable: ConnectionError"

        with patch.object(app, "configure_logging", return_value=Mock()):
            with patch.object(app, "request_media_manager", manager):
                with patch.object(app, "read_snapshot", read):
                    with patch.object(app, "delete_app", return_value=None):
                        with patch.object(app, "push_payload", return_value=None):
                            with patch.object(app, "activate_app", activate):
                                with patch.object(app.time, "monotonic", side_effect=lambda: clock):
                                    with patch.object(app.asyncio, "to_thread", self.immediate_to_thread):
                                        with patch.object(app.asyncio, "sleep", advance):
                                            with self.assertRaises(self.StopLoop):
                                                await app.run()

        self.assertEqual(activation_attempts, app.MAX_ACTIVATION_ATTEMPTS)

    async def test_accepted_animation_then_uptime_rollback_latches_static_icon(self) -> None:
        snapshots = iter(
            (
                app.MediaSnapshot("Midnight City", "M83", "Spotify", 0.5),
                app.MediaSnapshot("Midnight City", "M83", "Spotify", 0.75),
            )
        )
        uptimes = iter((100, 100, 1))
        clock = 0.0
        pushed_frame_counts: list[int] = []
        payloads: list[dict[str, object]] = []

        async def manager():
            return object()

        async def read(_manager):
            return next(snapshots)

        async def advance(_delay):
            nonlocal clock
            if clock >= 10.0:
                raise self.StopLoop
            clock += 10.0

        def push(payload):
            payloads.append(dict(payload))
            with Image.open(BytesIO(base64.b64decode(payload["icon"]))) as icon:
                pushed_frame_counts.append(icon.n_frames)

        with patch.object(app, "configure_logging", return_value=Mock()):
            with patch.object(app, "read_panel_uptime", side_effect=lambda: next(uptimes)),                     patch.object(app, "read_panel_reset_reason", return_value="panic"):
                with patch.object(app, "request_media_manager", manager):
                    with patch.object(app, "read_snapshot", read):
                        with patch.object(app, "delete_app", return_value=None):
                            with patch.object(app, "push_payload", push):
                                with patch.object(app, "activate_app", return_value=None):
                                    with patch.object(app.time, "monotonic", side_effect=lambda: clock):
                                        with patch.object(app.asyncio, "to_thread", self.immediate_to_thread):
                                            with patch.object(app.asyncio, "sleep", advance):
                                                with self.assertRaises(self.StopLoop):
                                                    await app.run()

        self.assertEqual(pushed_frame_counts, [app.RECORD_FRAME_COUNT, 1])
        self.assertEqual(
            [
                draw_bitmap_pixels(payload)[bucket - 1]
                for payload, bucket in zip(payloads, (12, 18))
            ],
            [app.RECORD_COLORS["P"], app.RECORD_COLORS["W"]],
        )


class StartupAndPreviewTests(unittest.TestCase):
    def test_missing_winrt_dependency_exits_with_install_guidance(self) -> None:
        logger = Mock()
        with patch.object(app, "MediaManager", None):
            with patch.object(app, "configure_logging", return_value=logger):
                with self.assertRaises(SystemExit) as raised:
                    app.main()
        self.assertIn(app.WINRT_INSTALL_COMMAND, str(raised.exception))
        logger.error.assert_called_once()

    def test_preview_rejects_non_positive_scale_before_writing(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive"):
            preview.render_preview("M83", 0.5, Path("unused.png"), 0)

    def test_preview_outputs_have_the_expected_scaled_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            static_path = preview.render_preview("M83", 0.5, root / "preview.png", 10)
            animation_path = preview.render_animation_preview(
                "M83", 0.5, root / "preview.gif", 10
            )
            contact_path = preview.render_contact_sheet(
                "M83", 0.5, root / "contact.png", 10
            )
            with Image.open(static_path) as static:
                self.assertEqual(static.size, (320, 80))
            with Image.open(animation_path) as animation:
                self.assertEqual(animation.size, (320, 80))
                self.assertGreaterEqual(animation.n_frames, app.RECORD_FRAME_COUNT)
            with Image.open(contact_path) as contact:
                self.assertGreater(contact.width, 320)
                self.assertGreater(contact.height, 80)

    def test_overflow_preview_stops_after_exactly_one_loop_fold(self) -> None:
        title = "W" * 8
        width = app.measure_text(title)
        self.assertGreater(width, app.TEXT_WIDTH)
        _, durations = preview._panel_frames(title, 0.5, paused=False)
        pixels_per_second = 21 * app.SCROLL_SPEED / 100
        expected_fold_ms = app.SCROLL_HOLD_MS + math.ceil(
            (width + app.SCROLL_GAP) * 1000 / pixels_per_second
        )
        self.assertGreaterEqual(sum(durations), expected_fold_ms)
        self.assertLess(sum(durations), expected_fold_ms + preview.PREVIEW_FRAME_MS)
        self.assertLess(sum(durations), app.PAGE_DURATION_MS)

    def test_long_title_preview_covers_the_complete_native_loop_fold(self) -> None:
        title = "W" * 119 + "Z"
        frames, durations = preview._panel_frames(title, 0.5, paused=False)
        width = app.measure_text(title)
        pixels_per_second = 21 * app.SCROLL_SPEED / 100
        expected_pass_ms = app.SCROLL_HOLD_MS + math.ceil(
            (width + app.SCROLL_GAP) * 1000 / pixels_per_second
        )
        self.assertGreaterEqual(sum(durations), expected_pass_ms)
        self.assertLess(sum(durations), expected_pass_ms + preview.PREVIEW_FRAME_MS)

        reveal_ms = app.SCROLL_HOLD_MS + math.ceil(
            (width - app.TEXT_WIDTH) * 1000 / pixels_per_second
        )
        frame = app.render_frame(title, 0.5, reveal_ms)
        distance = (reveal_ms - app.SCROLL_HOLD_MS) * pixels_per_second / 1000
        title_x = math.floor(app.TEXT_X - distance)
        final_x = title_x + width - app.glyph_width("Z")
        for row, pattern in enumerate(app.FONT["Z"]):
            for column, value in enumerate(pattern):
                if value == "1":
                    self.assertEqual(
                        rgb_pixel(frame, app.WIDTH, final_x + column, 1 + row),
                        app.TEXT_COLOR,
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
