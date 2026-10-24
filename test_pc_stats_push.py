"""Deterministic checks for the exception-driven AWTRIX PC stats sender."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from io import BytesIO
import json
import unittest
from unittest.mock import Mock, patch

from PIL import Image

import pc_stats_push as app
import pc_stats_preview as preview
import pc_alert_native_concepts as concepts


def response(status_code: int = 200, text: str = "") -> Mock:
    return Mock(status_code=status_code, text=text)


class MetricGateTests(unittest.TestCase):
    def test_cpu_boundary_requires_two_consecutive_high_samples(self) -> None:
        gate = app.MetricGate(80, 65)
        self.assertFalse(gate.update(79))
        self.assertFalse(gate.update(80))
        self.assertTrue(gate.update(80))

    def test_ram_boundary_requires_two_consecutive_high_samples(self) -> None:
        gate = app.MetricGate(85, 75)
        self.assertFalse(gate.update(84))
        self.assertFalse(gate.update(85))
        self.assertTrue(gate.update(85))

    def test_transient_spikes_never_activate(self) -> None:
        gate = app.MetricGate(80, 65)
        for value in (80, 79, 99, 20, 80, 79):
            self.assertFalse(gate.update(value))

    def test_active_gate_uses_lower_clear_threshold_and_two_samples(self) -> None:
        gate = app.MetricGate(80, 65)
        gate.update(90)
        self.assertTrue(gate.update(90))
        self.assertTrue(gate.update(70))  # hysteresis band remains visible
        self.assertTrue(gate.update(64))  # first low sample is debounced
        self.assertTrue(gate.update(65))  # boundary is not below the threshold
        self.assertTrue(gate.update(64))
        self.assertFalse(gate.update(64))

    def test_invalid_gate_configuration_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            app.MetricGate(80, 80)
        with self.assertRaises(ValueError):
            app.MetricGate(80, 65, trigger_samples=0)

    def test_invalid_samples_do_not_change_state_or_debounce_streaks(self) -> None:
        gate = app.MetricGate(80, 65)
        self.assertFalse(gate.update(90))
        self.assertFalse(gate.update(float("nan")))
        self.assertTrue(gate.update(90))

        self.assertTrue(gate.update(50))
        self.assertTrue(gate.update(float("inf")))
        self.assertFalse(gate.update(50))

    def test_missing_samples_carry_the_alert_only_as_long_as_clearing_would_take(self) -> None:
        gate = app.MetricGate(75, 60)
        gate.update(95)
        self.assertTrue(gate.update(95))
        for _ in range(gate.clear_samples):
            self.assertTrue(gate.update(None))
            self.assertEqual(gate.last_value, 95)
        # A good reading resets the count, so isolated failures never add up.
        self.assertTrue(gate.update(94))
        for _ in range(gate.clear_samples):
            self.assertTrue(gate.update(None))
        # A sensor that stays silent is no evidence for the alert either.
        self.assertFalse(gate.update(None))
        # Coming back, it has to trigger again like any new alert.
        self.assertFalse(gate.update(95))
        self.assertTrue(gate.update(95))

    def test_custom_max_value_for_rate_metrics(self) -> None:
        gate = app.MetricGate(15.0, 8.0, trigger_samples=2, max_value=10_000.0)
        self.assertFalse(gate.update(12.0))
        self.assertFalse(gate.update(250.0))
        self.assertTrue(gate.update(250.0))
        self.assertEqual(gate.last_value, 250.0)
        self.assertTrue(gate.update(10.0))
        self.assertTrue(gate.update(5.0))
        self.assertFalse(gate.update(5.0))

    def test_sample_network_mb_s(self) -> None:
        rate = app.sample_network_mb_s(1_000_000, 100.0, 1_000_000 + 10 * 1024 * 1024, 101.0)
        self.assertAlmostEqual(rate, 10.0, places=4)
        self.assertEqual(app.sample_network_mb_s(None, None, 1000, 100.0), 0.0)
        self.assertEqual(app.sample_network_mb_s(100, 100.0, 200, 100.0), 0.0)


class PayloadTests(unittest.TestCase):
    def test_metric_page_is_one_tiny_icon_plus_one_stable_lane(self) -> None:
        payload = app.metric_payload("CPU", 84.4)
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
        self.assertEqual(payload["backgroundColor"], "#000000")
        self.assertEqual(payload["durationMs"], 7_000)
        self.assertEqual(payload["lifetimeMs"], app.LIFETIME_MS)
        # The point of the lifetime is that a dead sender cannot leave a stale
        # warning up, so it still has to be short in human terms.
        self.assertLessEqual(app.LIFETIME_MS, 60_000)
        self.assertEqual(payload["lifetimeExpiry"], "remove")
        operation = payload["draw"][0]
        self.assertEqual(operation[:5], ["bitmap", 9, 0, 23, 8])
        self.assertEqual(len(base64.b64decode(operation[5], validate=True)), 23 * 8 * 3)
        self.assertLess(len(json.dumps(payload).encode("utf-8")), 2_048)

    def test_payload_contains_only_currently_active_metrics(self) -> None:
        one = app.build_payload([("RAM", 87.6)])
        self.assertEqual(one["backgroundColor"], "#000000")

        both = app.build_payload([("CPU", 91), ("RAM", 94)])
        self.assertEqual(both["backgroundColor"], "#000000")
        self.assertNotEqual(one["icon"], both["icon"])
        self.assertLess(len(json.dumps(both).encode("utf-8")), 2_048)
        json.dumps(both)

    def test_compact_copy_fits_two_and_three_digit_percentages(self) -> None:
        self.assertEqual(preview.text_value("CPU", 87), "CPU87%")
        self.assertEqual(preview.text_value("RAM", 93), "RAM93%")
        self.assertEqual(preview.text_value("CPU", 100), "CPU100")
        for label in ("CPU", "RAM"):
            for value in (0, 9, 80, 99, 100):
                self.assertLessEqual(
                    preview.text_width(preview.text_value(label, value)),
                    preview.LANE_WIDTH,
                )

    def test_severity_boundaries_change_cadence_without_changing_metric_identity(self) -> None:
        self.assertEqual(preview.severity_level(89.4), 0)
        self.assertEqual(preview.severity_level(90), 1)
        self.assertEqual(preview.severity_level(95), 2)
        for label in ("CPU", "RAM"):
            icons = [app.build_icon_gif(label, value) for value in (85, 92, 98)]
            self.assertEqual(len(set(icons)), 3)

    def test_percentages_are_clamped_and_non_finite_values_are_safe(self) -> None:
        self.assertEqual(app.normalize_percent(-1), 0)
        self.assertEqual(app.normalize_percent(101), 100)
        self.assertEqual(app.normalize_percent(float("nan")), 0)

    def test_choose_metrics_keeps_fixed_cpu_then_ram_order(self) -> None:
        cpu_gate = app.MetricGate(80, 65, trigger_samples=1)
        ram_gate = app.MetricGate(85, 75, trigger_samples=1)
        self.assertEqual(
            app.choose_metrics(90, 95, cpu_gate, ram_gate),
            [("CPU", 90), ("RAM", 95)],
        )

    def test_active_metric_reuses_last_good_value_during_bad_sample(self) -> None:
        cpu_gate = app.MetricGate(80, 65, trigger_samples=1)
        ram_gate = app.MetricGate(85, 75, trigger_samples=1)
        self.assertEqual(app.choose_metrics(90, 20, cpu_gate, ram_gate), [("CPU", 90)])
        self.assertEqual(
            app.choose_metrics(float("nan"), 20, cpu_gate, ram_gate),
            [("CPU", 90)],
        )

    def test_gpu_and_net_payloads(self) -> None:
        gpu_payload = app.build_payload([("GPU", 82.0)])
        self.assertEqual(gpu_payload["backgroundColor"], "#000000")
        self.assertLess(len(json.dumps(gpu_payload).encode("utf-8")), 2_048)

        net_payload = app.build_payload([("NET", 25.0)])
        self.assertEqual(net_payload["backgroundColor"], "#000000")
        self.assertLess(len(json.dumps(net_payload).encode("utf-8")), 2_048)

    def test_choose_metrics_with_gpu_and_net(self) -> None:
        cpu_gate = app.MetricGate(80, 65, trigger_samples=1)
        ram_gate = app.MetricGate(85, 75, trigger_samples=1)
        gpu_gate = app.MetricGate(75, 60, trigger_samples=1)
        net_gate = app.MetricGate(15.0, 8.0, trigger_samples=1, max_value=10_000.0)

        metrics = app.choose_metrics(
            50, 60, cpu_gate, ram_gate,
            gpu=80.0, net=25.0,
            gpu_gate=gpu_gate, net_gate=net_gate,
        )
        self.assertEqual(metrics, [("GPU", 80.0), ("NET", 25.0)])

        # Multi-metric priority: critical GPU is picked when both are active
        multi_payload = app.build_payload([("GPU", 92.0), ("NET", 25.0)])
        self.assertLess(len(json.dumps(multi_payload).encode("utf-8")), 2_048)

    def test_a_failed_gpu_or_net_sample_keeps_an_active_alert(self) -> None:
        # nvidia-smi failing once used to drop GPU from the active set, so the
        # page was deleted and re-created (and re-activated) a reading later.
        cpu_gate = app.MetricGate(80, 65, trigger_samples=1)
        ram_gate = app.MetricGate(85, 75, trigger_samples=1)
        gpu_gate = app.MetricGate(75, 60, trigger_samples=1)
        net_gate = app.MetricGate(15.0, 8.0, trigger_samples=1, max_value=10_000.0)
        gates = dict(gpu_gate=gpu_gate, net_gate=net_gate)
        self.assertEqual(app.choose_metrics(50, 60, cpu_gate, ram_gate, gpu=95.0, net=25.0, **gates),
                         [("GPU", 95.0), ("NET", 25.0)])
        self.assertEqual(app.choose_metrics(50, 60, cpu_gate, ram_gate, gpu=None, net=None, **gates),
                         [("GPU", 95.0), ("NET", 25.0)])
        # No sampler at all never raises an alert.
        idle_gpu = app.MetricGate(75, 60, trigger_samples=1)
        for _ in range(5):
            self.assertEqual(app.choose_metrics(50, 60, cpu_gate, ram_gate, gpu=None,
                                                gpu_gate=idle_gpu), [])



class ArtAndAnimationTests(unittest.TestCase):
    @staticmethod
    def gif_frames(data: bytes) -> tuple[list[Image.Image], list[int], int | None]:
        frames: list[Image.Image] = []
        durations: list[int] = []
        with Image.open(BytesIO(data)) as image:
            loop = image.info.get("loop")
            for index in range(image.n_frames):
                image.seek(index)
                frames.append(image.convert("RGB").copy())
                durations.append(int(image.info.get("duration", 0)))
        return frames, durations, loop

    def test_shared_palette_preserves_every_animated_pixel(self) -> None:
        cases = [(label, level) for label in ("CPU", "RAM", "GPU", "NET") for level in range(3)]
        for label, level in cases:
            with self.subTest(label=label, level=level):
                representative = (85.0, 92.0, 98.0)[level]
                frames, _durations, _loop = self.gif_frames(app._animated_icon(label, level))
                for phase, frame in enumerate(frames):
                    expected = app._icon_frame(label, representative, phase)
                    self.assertEqual(frame.tobytes(), expected.tobytes())
        for cpu_level in range(3):
            for ram_level in range(3):
                with self.subTest(combined=(cpu_level, ram_level)):
                    data = app._combined_animated_icon(cpu_level, ram_level)
                    frames, _durations, _loop = self.gif_frames(data)
                    self.assertEqual(len(frames), app.ICON_FRAME_COUNT)
                    self.assertLess(len(data), 600, "per-frame colour tables are back")
                    cpu = (85.0, 92.0, 98.0)[cpu_level]
                    ram = (85.0, 92.0, 98.0)[ram_level]
                    for phase, frame in enumerate(frames):
                        expected = app._icon_frame("BOTH", cpu, phase, other=ram)
                        self.assertEqual(frame.tobytes(), expected.tobytes())

    def test_combined_page_keeps_headroom_under_the_push_budget(self) -> None:
        page = app.combined_payload(100, 100, cpu_history=(100.0,) * 23, ram_history=(100.0,) * 23)
        size = len(json.dumps(page, separators=(",", ":")).encode("utf-8"))
        self.assertLess(size, 1_800, "the combined CPU+RAM page is near the 2 KB budget again")

    def test_hardware_icons_are_small_opaque_continuous_loops(self) -> None:
        for label in ("CPU", "RAM", "GPU"):
            for level, value in enumerate((85, 92, 98)):
                with self.subTest(label=label, value=value):
                    data = app.build_icon_gif(label, value)
                    frames, durations, loop = self.gif_frames(data)
                    expected_frames = app.ICON_FRAME_COUNT
                    self.assertLessEqual(len(data), app.ICON_MAX_BYTES)
                    self.assertEqual(len(frames), expected_frames)
                    self.assertEqual(durations, [240] * expected_frames)
                    self.assertEqual(loop, 0)
                    self.assertEqual(len({frame.tobytes() for frame in frames}), expected_frames)
                    self.assertTrue(all(frame.size == (8, 8) for frame in frames))
                    with Image.open(BytesIO(data)) as image:
                        self.assertNotIn("transparency", image.info)

    def test_every_frame_delay_lands_on_the_120ms_beat(self) -> None:
        # The panel renders on a 24ms tick and GIF delays quantise to 10ms, so
        # they only coincide at multiples of 120. This app shipped 110ms, which
        # held some frames four ticks and some five and visibly stuttered.
        # Asserted against the literal beat, not against ICON_STEP_MS, because
        # the old test compared the file to itself and could never fail.
        self.assertEqual(app.FRAME_BEAT_MS, 120)
        for label in ("CPU", "RAM"):
            for value in (85, 92, 98):
                for animated in (True, False):
                    with self.subTest(label=label, value=value, animated=animated):
                        data = app.build_icon_gif(label, value, animated=animated)
                        _, durations, _ = self.gif_frames(data)
                        for duration in durations:
                            self.assertEqual(duration % 120, 0)

    def test_combined_frame_delays_land_on_the_beat_too(self) -> None:
        for cpu_level in range(3):
            for ram_level in range(3):
                with self.subTest(cpu=cpu_level, ram=ram_level):
                    data = app._combined_animated_icon(cpu_level, ram_level)
                    _, durations, _ = self.gif_frames(data)
                    for duration in durations:
                        self.assertEqual(duration % 120, 0)

    def test_gpu_and_net_icons_are_valid_and_on_beat(self) -> None:
        for label, val in (("GPU", 85), ("GPU", 92), ("NET", 25)):
            for animated in (True, False):
                with self.subTest(label=label, val=val, animated=animated):
                    data = app.build_icon_gif(label, val, animated=animated)
                    self.assertLessEqual(len(data), app.ICON_MAX_BYTES)
                    frames, durations, loop = self.gif_frames(data)
                    self.assertEqual(loop, 0 if label == "GPU" and animated else None)
                    self.assertTrue(all(f.size == (8, 8) for f in frames))
                    for d in durations:
                        self.assertEqual(d % 120, 0)

    def test_validation_rejects_an_off_beat_icon(self) -> None:
        # The guard has to catch a bad cadence even when it comes from a
        # constant someone edited, so build one deliberately and check it dies.
        frames = [app._icon_frame("CPU", 98.0, phase) for phase in range(3)]
        stream = BytesIO()
        frames[0].save(
            stream,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=[110, 110, 1_800],
            disposal=1,
            optimize=False,
        )
        with self.assertRaisesRegex(ValueError, "off the 120ms beat"):
            app.validate_icon_gif(stream.getvalue())

    def test_static_safety_mode_keeps_one_frame(self) -> None:
        for label in ("CPU", "RAM"):
            frames, _, _ = self.gif_frames(app.build_icon_gif(label, 92, animated=False))
            self.assertEqual(len(frames), 1)

    def test_dial_needles_advance_then_settle_without_spinner_loop(self) -> None:
        cpu_frames = [preview.panel_frame("CPU", 87, phase) for phase in range(6)]
        ram_frames = [preview.panel_frame("RAM", 93, phase) for phase in range(6)]
        self.assertEqual(preview._needle_target(87), 3)
        self.assertEqual(preview._needle_target(93), 4)
        self.assertEqual(cpu_frames[3], cpu_frames[4])
        self.assertEqual(cpu_frames[4], cpu_frames[5])
        self.assertEqual(ram_frames[4], ram_frames[5])
        self.assertTrue(
            all(frame[y][x] == preview.GOLD for frame in cpu_frames for x, y in preview.DIAL_BEZEL)
        )
        self.assertEqual(len({tuple(pixel for row in frame for pixel in row[:8]) for frame in cpu_frames}), 4)
        self.assertEqual(len({tuple(pixel for row in frame for pixel in row[:8]) for frame in ram_frames}), 5)

    def test_text_and_rail_never_move_across_icon_frames(self) -> None:
        for label, value in (("CPU", 87), ("RAM", 93)):
            history = (70, 75, 80, 82, 85, 88, 91, value)
            frames = [preview.panel_frame(label, value, phase, history) for phase in range(6)]
            lane = tuple(tuple(row[9:]) for row in frames[0])
            self.assertTrue(all(tuple(tuple(row[9:]) for row in frame) == lane for frame in frames[1:]))
            self.assertTrue(all(frame[y][8] == preview.BLACK for frame in frames for y in range(8)))

    def test_combined_state_is_one_page_with_two_truthful_histories(self) -> None:
        cpu_history = (72, 78, 82, 85, 88, 91, 94, 87)
        ram_history = (79, 82, 84, 86, 88, 90, 92, 93)
        frames = [
            preview.combined_frame(87, 93, phase, cpu_history, ram_history)
            for phase in range(6)
        ]
        lane = tuple(tuple(row[9:]) for row in frames[0])
        self.assertTrue(all(tuple(tuple(row[9:]) for row in frame) == lane for frame in frames[1:]))
        self.assertTrue(all(frame[y][8] == preview.BLACK for frame in frames for y in range(8)))
        self.assertEqual(preview.text_width("C87R93"), 23)
        payload = app.build_payload(
            [("CPU", 87), ("RAM", 93)],
            histories={"CPU": cpu_history, "RAM": ram_history},
        )
        self.assertIsInstance(payload, dict)
        self.assertLess(len(json.dumps(payload).encode("utf-8")), 2_048)

        for y in (0, 7):
            for index in range(8):
                x = 9 + index * 3
                self.assertEqual(frames[-1][y][x], frames[-1][y][x + 1])
                if index < 7:
                    self.assertEqual(frames[-1][y][x + 2], preview.BLACK)
            self.assertEqual(frames[-1][y][30], preview.PEARL)
            self.assertEqual(frames[-1][y][31], preview.PEARL)

    def test_history_normalization_ignores_invalid_values_and_keeps_latest_eight(self) -> None:
        history = (1, 2, float("nan"), 3, 4, 5, 6, 7, 8, 9)
        self.assertEqual(preview.normalize_history(history, 10), (3, 4, 5, 6, 7, 8, 9, 10))

    def test_history_no_longer_changes_the_page(self) -> None:
        # Calm glance: the lane shows the value and one meter; the old history
        # timeline is gone, so a new history must not cause a repush.
        first = app.build_payload(
            [("CPU", 87), ("RAM", 93)],
            histories={"CPU": (70, 75, 80, 87), "RAM": (80, 84, 87, 93)},
        )
        second = app.build_payload(
            [("CPU", 87), ("RAM", 93)],
            histories={"CPU": (75, 80, 85, 87), "RAM": (84, 87, 90, 93)},
        )
        self.assertEqual(first, second)

    def test_production_combined_frame_matches_selected_composite_target(self) -> None:
        cpu_history = (72, 78, 82, 85, 88, 91, 94, 87)
        ram_history = (79, 82, 84, 86, 88, 90, 92, 93)
        implementation = preview.combined_frame(
            87,
            93,
            phase=5,
            cpu_history=cpu_history,
            ram_history=ram_history,
        )
        self.assertEqual(implementation, concepts.composite_target(5))

    def test_palette_survives_gamma_at_brightness_45(self) -> None:
        for color in (
            preview.PEARL,
            preview.ICE,
            preview.MINT,
            preview.GOLD,
            preview.AMBER,
            preview.CORAL,
            preview.VIOLET,
        ):
            self.assertNotEqual(
                preview.grade_color(color, gamma=1.8, brightness=45, saturation=90),
                preview.BLACK,
            )


class PanelSyncTests(unittest.TestCase):
    def test_alert_activation_uses_one_non_waking_switch(self) -> None:
        with patch.object(app.requests, "put", return_value=response(200)) as put:
            self.assertIsNone(app.activate_app())
        put.assert_called_once_with(
            app.ACTIVE_URL,
            json={"name": app.APP_NAME, "fast": False},
            timeout=app.REQUEST_TIMEOUT,
        )

    def test_quiet_hours_match_nightkeeper_blackout(self) -> None:
        self.assertFalse(app.is_quiet_time(datetime(2026, 8, 20, 21, 29, tzinfo=timezone.utc)))
        self.assertTrue(app.is_quiet_time(datetime(2026, 8, 20, 21, 30, tzinfo=timezone.utc)))
        self.assertTrue(app.is_quiet_time(datetime(2026, 8, 21, 6, 59, tzinfo=timezone.utc)))
        self.assertFalse(app.is_quiet_time(datetime(2026, 8, 21, 7, 0, tzinfo=timezone.utc)))

    def test_startup_quiet_state_deletes_old_family_and_does_not_push(self) -> None:
        with patch.object(app.requests, "delete", return_value=response()) as delete:
            with patch.object(app.requests, "put") as put:
                state, error = app.sync_panel([], None)
        self.assertEqual(state, ())
        self.assertIsNone(error)
        # pcstats1 is included because deleting the base name does not remove
        # the children the old array sender made, and the one nothing ever
        # names again was being left on the panel as a permanent empty slot.
        self.assertEqual(
            [call.args[0] for call in delete.call_args_list],
            [app.LEGACY_DELETE_URL, *app.LEGACY_CHILD_URLS, app.DELETE_URL],
        )
        self.assertIn(f"{app.AWTRIX}/api/v1/apps/pcstats1", app.LEGACY_CHILD_URLS)
        put.assert_not_called()

    def test_steady_quiet_state_makes_no_network_requests(self) -> None:
        with patch.object(app.requests, "delete") as delete:
            with patch.object(app.requests, "put") as put:
                state, error = app.sync_panel([], ())
        self.assertEqual(state, ())
        self.assertIsNone(error)
        delete.assert_not_called()
        put.assert_not_called()

    def test_membership_change_updates_one_direct_page_without_array_cleanup(self) -> None:
        calls: list[str] = []

        def delete_side_effect(*args, **kwargs):
            calls.append("delete")
            return response()

        def put_side_effect(*args, **kwargs):
            calls.append("put")
            return response(204)

        with patch.object(app.requests, "delete", side_effect=delete_side_effect):
            with patch.object(app.requests, "put", side_effect=put_side_effect) as put:
                state, error = app.sync_panel([("RAM", 90)], ("CPU",))

        self.assertEqual(calls, ["put"])
        self.assertEqual(state, ("RAM",))
        self.assertIsNone(error)
        page = put.call_args.kwargs["json"]
        self.assertEqual(page["backgroundColor"], "#000000")
        self.assertEqual(page["draw"][0][:5], ["bitmap", 9, 0, 23, 8])

    def test_same_active_set_refreshes_without_deleting(self) -> None:
        with patch.object(app.requests, "delete") as delete:
            with patch.object(app.requests, "put", return_value=response()) as put:
                state, error = app.sync_panel([("CPU", 88)], ("CPU",))
        self.assertEqual(state, ("CPU",))
        self.assertIsNone(error)
        delete.assert_not_called()
        put.assert_called_once()

    def test_a_stuck_legacy_slot_does_not_suppress_the_alert(self) -> None:
        # A legacy name that will not delete is a dead row in the app list.
        # Blocking on it meant no CPU alert ever appeared again.
        def by_url(url, timeout=None):
            return response(200) if url == app.DELETE_URL else response(500, "stuck")

        with patch.object(app.requests, "delete", side_effect=by_url):
            with patch.object(app.requests, "put", return_value=response(200)) as put:
                state, error = app.sync_panel([("CPU", 92)], None)
        self.assertIsNone(error)
        self.assertEqual(state, ("CPU",))
        put.assert_called_once()

    def test_failed_cleanup_prevents_a_conflicting_push(self) -> None:
        with patch.object(app.requests, "delete", return_value=response(500, "no")):
            with patch.object(app.requests, "put") as put:
                state, error = app.sync_panel([("RAM", 90)], None)
        self.assertIsNone(state)
        self.assertIn("cleanup HTTP 500", error)
        put.assert_not_called()

    def test_failed_push_after_cleanup_records_the_empty_panel_state(self) -> None:
        with patch.object(app.requests, "delete", return_value=response()):
            with patch.object(app.requests, "put", return_value=response(422, "bad")):
                state, error = app.sync_panel([("CPU", 90)], None)
        self.assertEqual(state, ())
        self.assertIn("push HTTP 422", error)

    def test_missing_family_is_already_clean(self) -> None:
        with patch.object(app.requests, "delete", return_value=response(404)) as delete:
            with patch.object(app.requests, "put") as put:
                state, error = app.sync_panel([], None)
        self.assertEqual(state, ())
        self.assertIsNone(error)
        # A 404 on every slot is the normal already-clean case, not a failure.
        self.assertEqual(
            [call.args[0] for call in delete.call_args_list],
            [app.LEGACY_DELETE_URL, *app.LEGACY_CHILD_URLS, app.DELETE_URL],
        )
        put.assert_not_called()

    def test_active_to_quiet_deletes_only_the_direct_slot(self) -> None:
        with patch.object(app.requests, "delete", return_value=response()) as delete:
            with patch.object(app.requests, "put") as put:
                state, error = app.sync_panel([], ("CPU",))
        self.assertEqual(state, ())
        self.assertIsNone(error)
        delete.assert_called_once_with(app.DELETE_URL, timeout=app.REQUEST_TIMEOUT)
        put.assert_not_called()

    def test_health_probe_is_read_only_and_avoids_the_framebuffer(self) -> None:
        with patch.object(
            app.requests,
            "get",
            return_value=Mock(
                status_code=200,
                json=Mock(return_value={"uptimeSeconds": 123, "resetReason": "poweron"}),
            ),
        ) as get:
            self.assertEqual(app.read_panel_health(), app.PanelHealth(123, "poweron"))
        get.assert_called_once_with(app.DEVICE_URL, timeout=app.REQUEST_TIMEOUT)
        self.assertNotIn("screen", get.call_args.args[0])

    def test_uptime_rollback_is_the_only_reboot_signal(self) -> None:
        old = app.PanelHealth(500, "poweron")
        self.assertTrue(app.panel_rebooted_since(old, app.PanelHealth(12, "panic")))
        self.assertFalse(app.panel_rebooted_since(old, app.PanelHealth(500, "unknown")))
        self.assertFalse(app.panel_rebooted_since(old, app.PanelHealth(900, "unknown")))
        self.assertFalse(app.panel_rebooted_since(None, old))
        self.assertFalse(app.panel_rebooted_since(old, None))


class AnimationGuardTests(unittest.TestCase):
    """A dropped packet must not cost the app its animation forever.

    v1 latched static icons after five consecutive failures and never unlatched.
    At a five second interval that is twenty-five seconds of unreachable panel,
    which on this LAN is an ordinary WiFi dropout, so the art was permanently
    downgraded by an event that proved nothing about the panel's memory.
    """

    def test_animates_by_default(self) -> None:
        self.assertTrue(app.AnimationGuard().animate(0.0))

    def test_unreachable_panel_never_costs_the_animation(self) -> None:
        guard = app.AnimationGuard()
        for _ in range(50):
            guard.unreachable()
        self.assertFalse(guard.holding)
        self.assertTrue(guard.animate(0.0))

    def test_a_refusal_holds_static_icons_immediately(self) -> None:
        guard = app.AnimationGuard()
        guard.refused(1_000.0)
        self.assertTrue(guard.holding)
        self.assertFalse(guard.animate(1_000.0))

    def test_the_hold_expires_into_one_animated_probe(self) -> None:
        guard = app.AnimationGuard()
        guard.refused(1_000.0)
        self.assertFalse(guard.animate(1_000.0 + app.STATIC_HOLD_SECONDS - 1))
        self.assertTrue(guard.animate(1_000.0 + app.STATIC_HOLD_SECONDS))
        self.assertTrue(guard.probing)

    def test_a_probe_the_panel_accepts_clears_the_hold(self) -> None:
        guard = app.AnimationGuard()
        guard.refused(1_000.0)
        guard.animate(1_000.0 + app.STATIC_HOLD_SECONDS)
        guard.succeeded(1_000.0 + app.STATIC_HOLD_SECONDS)
        self.assertFalse(guard.holding)
        self.assertTrue(guard.animate(1_000.0 + app.STATIC_HOLD_SECONDS))

    def test_a_probe_the_panel_refuses_doubles_the_wait_up_to_the_cap(self) -> None:
        guard = app.AnimationGuard()
        now = 0.0
        waits = []
        for _ in range(8):
            guard.refused(now)
            waits.append(guard.wait_seconds)
            now += guard.wait_seconds
            guard.animate(now)
        self.assertEqual(waits[0], app.STATIC_HOLD_SECONDS)
        self.assertEqual(waits[1], app.STATIC_HOLD_SECONDS * 2)
        self.assertEqual(waits[-1], app.STATIC_HOLD_MAX_SECONDS)
        self.assertTrue(all(b >= a for a, b in zip(waits, waits[1:])))

    def test_an_accepted_probe_does_not_reset_the_ladder_until_it_has_held(self) -> None:
        # HTTP 200 only means the panel *stored* the page. It decodes the GIF
        # later, when the app next comes up in rotation -- which is exactly the
        # accept-then-reboot cycle the reboot detector exists to catch. Zeroing
        # the backoff on the 200 restarted the wait at its shortest rung every
        # time, so the panel would be rebooted every few minutes forever.
        guard = app.AnimationGuard()
        now = 0.0
        guard.refused(now)                      # 300
        now += guard.wait_seconds
        guard.animate(now)
        guard.refused(now)                      # 600
        now += guard.wait_seconds
        guard.animate(now)
        self.assertTrue(guard.probing)
        guard.succeeded(now)                    # panel stores the page...
        self.assertEqual(guard.wait_seconds, 600, "ladder must survive the 200")

        # ...and reboots when it renders it, seen at the next health probe.
        guard.refused(now + app.HEALTH_PROBE_SECONDS)
        self.assertEqual(guard.wait_seconds, 1_200, "reboot must double the wait")

    def test_a_probe_that_survives_probation_does_reset_the_ladder(self) -> None:
        guard = app.AnimationGuard()
        guard.refused(0.0)
        guard.animate(guard.wait_seconds)
        guard.succeeded(guard.wait_seconds)
        settled_at = guard.wait_seconds + app.ANIMATION_PROBATION_SECONDS
        guard.settle(settled_at - 1)
        self.assertGreater(guard.wait_seconds, 0, "still on probation")
        guard.settle(settled_at)
        self.assertEqual(guard.wait_seconds, 0.0, "survived, so the trouble is over")
        self.assertTrue(guard.animate(settled_at))

    def test_probation_is_long_enough_for_a_reboot_to_be_noticed(self) -> None:
        self.assertGreater(app.ANIMATION_PROBATION_SECONDS, app.HEALTH_PROBE_SECONDS)

    def test_success_while_not_probing_cannot_clear_a_live_hold(self) -> None:
        # A quiet-state delete also "succeeds". It must not be mistaken for
        # evidence that the panel will take a GIF.
        guard = app.AnimationGuard()
        guard.refused(1_000.0)
        guard.animate(1_000.0)  # still inside the hold, so not a probe
        guard.succeeded(1_000.0)
        self.assertTrue(guard.holding)


class CombinedTextTests(unittest.TestCase):
    """The combined lane must stay readable and buildable at saturation.

    This is the one page that has to be right when the machine is in the worst
    trouble, and it was the one page that failed there.
    """

    def test_no_metric_is_ever_misreported_by_an_order_of_magnitude(self) -> None:
        # "C100R9" for a RAM of 93 is not a rounded reading, it is a wrong one.
        for cpu, ram in ((100, 93), (100, 87), (93, 100), (100, 100), (87, 93)):
            with self.subTest(cpu=cpu, ram=ram):
                self.assertGreaterEqual(preview.combined_percent(cpu), min(cpu, 99) - 1)
                self.assertGreaterEqual(preview.combined_percent(ram), min(ram, 99) - 1)

    def test_saturation_still_renders_instead_of_raising(self) -> None:
        # CPU 100 with RAM 100 produced "C100R10", which was too wide to draw.
        # The ValueError went straight out of sync_panel and killed the watcher,
        # silently, because this runs under pythonw with no console.
        for cpu, ram in ((100.0, 100.0), (100.0, 93.0), (93.0, 100.0)):
            with self.subTest(cpu=cpu, ram=ram):
                frame = preview.combined_frame(cpu, ram)
                self.assertEqual(len(frame), preview.HEIGHT)

    def test_the_combined_page_builds_at_full_saturation(self) -> None:
        page = app.build_payload([("CPU", 100.0), ("RAM", 100.0)])
        self.assertEqual(page["draw"][0][:5], ["bitmap", 9, 0, 23, 8])

    def test_combined_text_always_fits_the_lane(self) -> None:
        for cpu in range(0, 101, 7):
            for ram in range(0, 101, 7):
                with self.subTest(cpu=cpu, ram=ram):
                    text = f"C{preview.combined_percent(cpu):02d}R{preview.combined_percent(ram):02d}"
                    self.assertLessEqual(preview.text_width(text), preview.LANE_WIDTH)
                    self.assertEqual(len(text), 6)


class SyncErrorTests(unittest.TestCase):
    def test_an_http_error_records_that_the_panel_answered(self) -> None:
        error = app._http_error("push", response(507, "insufficientStorage"))
        self.assertTrue(error.refused)
        self.assertIn("push HTTP 507", error)

    def test_a_transport_error_records_that_it_did_not(self) -> None:
        error = app._transport_error(app.requests.ConnectionError("no route"))
        self.assertFalse(error.refused)
        self.assertIn("unreachable", error)

    def test_sync_panel_classifies_both_kinds(self) -> None:
        with patch.object(app.requests, "put", return_value=response(507, "full")):
            _, refused = app.sync_panel([("CPU", 90)], ("CPU",))
        with patch.object(
            app.requests, "put", side_effect=app.requests.ConnectionError("down")
        ):
            _, unreachable = app.sync_panel([("CPU", 90)], ("CPU",))
        self.assertTrue(refused.refused)
        self.assertFalse(unreachable.refused)


class LocalPageRejectionTests(unittest.TestCase):
    """A size guard must degrade the page, not kill the watcher.

    This runs under pythonw as a scheduled task, so an uncaught ValueError
    disappears with no traceback anywhere -- the alert would simply stop, and
    only at the moment the machine was busy enough to trigger it.
    """

    def test_an_oversize_animated_icon_falls_back_to_static(self) -> None:
        real = app.build_payload

        def only_static_fits(metrics, animated=True, histories=None):
            if animated:
                raise ValueError("PC alert icon is 9999 bytes; limit is 1024")
            return real(metrics, animated=False, histories=histories)

        with patch.object(app, "build_payload", side_effect=only_static_fits):
            page, error = app._safe_page([("CPU", 98.0)], True, None)
        self.assertIsNone(error)
        self.assertIsNotNone(page)

    def test_a_page_that_cannot_be_built_at_all_is_reported_as_a_refusal(self) -> None:
        with patch.object(app, "build_payload", side_effect=ValueError("too big")):
            page, error = app._safe_page([("CPU", 98.0)], True, None)
        self.assertIsNone(page)
        self.assertTrue(error.refused)
        self.assertIn("too big", error)

    def test_sync_panel_reports_the_rejection_instead_of_raising(self) -> None:
        with patch.object(app, "build_payload", side_effect=ValueError("too big")):
            with patch.object(app.requests, "put") as put:
                state, error = app.sync_panel([("CPU", 98.0)], ("CPU",))
        put.assert_not_called()
        self.assertEqual(state, ("CPU",))
        self.assertIn("page rejected locally", error)


class SamplingAndLoggingTests(unittest.TestCase):
    def test_read_stats_samples_each_metric_once(self) -> None:
        memory = Mock(percent=72.4)
        with patch.object(app.psutil, "cpu_percent", return_value=31.6) as cpu:
            with patch.object(app.psutil, "virtual_memory", return_value=memory) as ram:
                self.assertEqual(app.read_stats(), (31.6, 72.4))
        cpu.assert_called_once_with(interval=None)
        ram.assert_called_once_with()

    def test_log_throttling_contract(self) -> None:
        logged = [miss for miss in range(1, 181) if app.should_log(miss)]
        self.assertEqual(logged, [1, 5, 60, 120, 180])

    def test_membership_changes_are_immediate_and_values_wait_for_a_reason(self) -> None:
        cpu86 = app.page_view([("CPU", 86)])
        self.assertTrue(app.sync_due([], None, now=0))
        self.assertTrue(app.sync_due([("CPU", 85)], (), now=0, published_at=0))
        self.assertTrue(app.sync_due([], ("CPU",), now=0, published_at=0))
        self.assertTrue(
            app.sync_due([("CPU", 86), ("RAM", 90)], ("CPU",), now=0.1, published_at=0,
                         published_view=cpu86)
        )
        # The same page, a jitter away, is not worth a write...
        self.assertFalse(
            app.sync_due([("CPU", 87)], ("CPU",), now=20, published_at=0, published_view=cpu86)
        )
        # ...but a move the viewer would read is, once it has lasted
        # REFRESH_CONFIRM_SAMPLES readings and PUSH_INTERVAL has passed.
        confirmed = app.REFRESH_CONFIRM_SAMPLES
        self.assertFalse(
            app.sync_due([("CPU", 89)], ("CPU",), now=app.PUSH_INTERVAL - 0.1, published_at=0,
                         published_view=cpu86, change_streak=confirmed)
        )
        self.assertFalse(
            app.sync_due([("CPU", 89)], ("CPU",), now=app.PUSH_INTERVAL, published_at=0,
                         published_view=cpu86, change_streak=confirmed - 1)
        )
        self.assertTrue(
            app.sync_due([("CPU", 89)], ("CPU",), now=app.PUSH_INTERVAL, published_at=0,
                         published_view=cpu86, change_streak=confirmed)
        )
        # Nothing known about what the panel shows: write.
        self.assertTrue(app.sync_due([("CPU", 86)], ("CPU",), now=1, published_at=None))
        self.assertTrue(app.sync_due([("CPU", 86)], ("CPU",), now=1, published_at=0))

    def test_a_change_of_animation_mode_pushes_without_waiting_for_the_timer(self) -> None:
        # Dropping to static after a reboot does nothing until it is sent. The
        # page on the panel is still the animated one, so leaving it there
        # means the panel keeps re-rendering the GIF that just brought it down.
        shown = app.page_view([("CPU", 90)])
        for animated, published_animated, due in (
            (False, True, True),
            (True, False, True),
            (True, True, False),
        ):
            with self.subTest(animated=animated, published_animated=published_animated):
                self.assertEqual(
                    app.sync_due(
                        [("CPU", 90)], ("CPU",), now=1, published_at=0, published_view=shown,
                        animated=animated, published_animated=published_animated,
                    ),
                    due,
                )
        # A held page still drops to static at once: that is safety, not cadence.
        self.assertTrue(
            app.sync_due(
                [("CPU", 40)], ("CPU",), now=1, published_at=0, published_view=shown,
                animated=False, published_animated=True, held=True,
            )
        )
        # But it is never the target of an animated probe: it is about to go.
        self.assertFalse(
            app.sync_due(
                [("CPU", 40)], ("CPU",), now=1, published_at=0, published_view=shown,
                animated=True, published_animated=False, held=True,
            )
        )

    def test_refresh_outruns_the_lifetime_with_room_for_a_retry(self) -> None:
        # Readings come every INTERVAL plus the loop's sampling overhead (up to
        # nvidia-smi's own 5 s timeout, and beyond). For every overhead and
        # every phase of the readings against the last push, the keep-alive
        # must land by two thirds of the lifetime, and a lost one must still
        # leave a full retry -- started after REQUEST_TIMEOUT waiting, INTERVAL
        # sleeping and another overhead, with its own REQUEST_TIMEOUT to
        # finish -- before the page expires and the alert blinks out. The old
        # fixed 25 s threshold failed this with 3.0-3.3 s and 5 s of overhead.
        lifetime = app.LIFETIME_MS / 1000
        for step in range(0, 241):
            overhead = step * 0.05  # 0 .. 12 s
            period = app.INTERVAL + overhead
            for phase in range(20):
                first = period * (phase + 1) / 20  # first reading after the push
                readings = (first + k * period for k in range(200))
                sent = next(t for t in readings if app.keepalive_due(t, overhead))
                with self.subTest(overhead=overhead, first=first):
                    self.assertLess(sent, lifetime * 2 / 3)
                    retry_starts = sent + app.REQUEST_TIMEOUT + period
                    self.assertLessEqual(retry_starts + app.REQUEST_TIMEOUT, lifetime)
        # With no overhead it is the 25 s the write savings were measured at.
        self.assertEqual(app.KEEPALIVE_SECONDS, 25)
        self.assertFalse(app.keepalive_due(app.KEEPALIVE_SECONDS - 0.01))
        self.assertTrue(app.keepalive_due(app.KEEPALIVE_SECONDS))
        # The first backoff step must not delay that retry.
        self.assertLessEqual(app.retry_delay(1), app.REQUEST_TIMEOUT + app.INTERVAL)
        # A value change can never rewrite faster than the old fixed cadence.
        self.assertEqual(app.PUSH_INTERVAL, 10)
        self.assertGreater(app.KEEPALIVE_SECONDS, app.PUSH_INTERVAL)

    def test_activation_is_rate_limited(self) -> None:
        self.assertGreaterEqual(app.ACTIVATION_COOLDOWN_SECONDS, app.PUSH_INTERVAL)


class PageViewTests(unittest.TestCase):
    """The refresh logic's idea of the page has to be the page itself."""

    def test_equal_views_are_byte_identical_pages_and_different_views_are_not(self) -> None:
        import random

        rng = random.Random(20261009)
        label_sets = (("CPU",), ("RAM",), ("GPU",), ("NET",), ("CPU", "RAM"),
                      ("CPU", "RAM", "NET"), ("GPU", "NET"), ("CPU", "GPU"), ("CPU", "RAM", "GPU"))
        for labels in label_sets:
            seen: dict[app.PageView, str] = {}
            for _ in range(160):
                metrics = [(label, rng.uniform(60, 100)) for label in labels]
                view = app.page_view(metrics)
                page = json.dumps(app.build_payload(metrics), sort_keys=True)
                with self.subTest(labels=labels, metrics=metrics):
                    if view in seen:
                        self.assertEqual(seen[view], page, "same view, different page")
                    self.assertNotIn(page, [p for v, p in seen.items() if v != view],
                                     "different view, same page")
                seen[view] = page

    def test_view_follows_the_payloads_choice_of_metric(self) -> None:
        self.assertEqual(app.page_view([("CPU", 91), ("RAM", 94)]).face, app.COMBINED_FACE)
        self.assertEqual(app.page_view([("CPU", 91), ("RAM", 96)]).icon_levels, (1, 2))
        self.assertEqual(app.page_view([("CPU", 91), ("RAM", 94)]).percent, 94)
        self.assertEqual(app.page_view([("GPU", 92.0), ("NET", 25.0)]).face, "GPU")
        self.assertEqual(app.page_view([("GPU", 82.0), ("NET", 95.0)]).face, "NET")
        self.assertEqual(app.page_view([("CPU", 84.4)]).percent, 84)
        self.assertEqual(app.page_view([("CPU", 84.4)]).lane_level, "fair")


class RefreshPolicyTests(unittest.TestCase):
    """A visible page is rewritten when what it shows changes enough to matter."""

    confirmed = app.REFRESH_CONFIRM_SAMPLES

    def view(self, *metrics: tuple[str, float]) -> app.PageView:
        return app.page_view(list(metrics))

    def test_jitter_waits_for_the_keepalive(self) -> None:
        shown = self.view(("CPU", 86))
        for value in (84.6, 85, 86.4, 87, 88.4):
            for since in (app.PUSH_INTERVAL, app.KEEPALIVE_SECONDS - 0.1):
                with self.subTest(value=value, since=since):
                    jitter = self.view(("CPU", value))
                    self.assertFalse(app.shows_change(jitter, shown))
                    self.assertFalse(app.refresh_due(jitter, shown, since,
                                                     change_streak=self.confirmed))
        self.assertTrue(app.refresh_due(self.view(("CPU", 86)), shown, app.KEEPALIVE_SECONDS))

    def test_a_move_of_the_delta_is_shown_but_never_faster_than_push_interval(self) -> None:
        shown = self.view(("CPU", 84))
        for value in (84 + app.REFRESH_DELTA_POINTS, 84 - app.REFRESH_DELTA_POINTS):
            moved = self.view(("CPU", value))
            self.assertTrue(app.shows_change(moved, shown))
            self.assertFalse(app.refresh_due(moved, shown, app.PUSH_INTERVAL - 0.1,
                                             change_streak=self.confirmed))
            self.assertTrue(app.refresh_due(moved, shown, app.PUSH_INTERVAL,
                                            change_streak=self.confirmed))

    def test_a_one_reading_blip_is_not_repainted(self) -> None:
        shown = self.view(("CPU", 86))
        blip = self.view(("CPU", 97))
        self.assertTrue(app.shows_change(blip, shown))
        for streak in range(self.confirmed):
            with self.subTest(streak=streak):
                self.assertFalse(app.refresh_due(blip, shown, app.PUSH_INTERVAL * 2,
                                                 change_streak=streak))
        # A stale streak cannot force a write for a page that no longer differs.
        self.assertFalse(app.refresh_due(shown, shown, app.PUSH_INTERVAL,
                                         change_streak=self.confirmed + 5))

    def test_a_slow_creep_is_measured_from_what_the_panel_shows(self) -> None:
        shown = self.view(("CPU", 82))
        due = [app.refresh_due(self.view(("CPU", 82 + step)), shown, app.PUSH_INTERVAL,
                               change_streak=self.confirmed)
               for step in range(1, 5)]
        self.assertEqual(due, [False, False, True, True])

    def test_band_and_icon_changes_push_even_inside_the_delta(self) -> None:
        cases = (
            (("CPU", 89), ("CPU", 90)),   # icon level and rail colour
            (("CPU", 94), ("CPU", 95)),
            (("CPU", 80), ("CPU", 79)),   # rail colour only (fair -> good)
            ((("CPU", 91), ("RAM", 89)), (("CPU", 91), ("RAM", 90))),  # second needle's level
        )
        for before, after in cases:
            before = before if isinstance(before[0], tuple) else (before,)
            after = after if isinstance(after[0], tuple) else (after,)
            with self.subTest(before=before, after=after):
                shown, now = self.view(*before), self.view(*after)
                self.assertLess(abs(shown.percent - now.percent), app.REFRESH_DELTA_POINTS)
                self.assertTrue(app.refresh_due(now, shown, app.PUSH_INTERVAL,
                                                change_streak=self.confirmed))
                self.assertFalse(app.refresh_due(now, shown, app.PUSH_INTERVAL - 0.1,
                                                 change_streak=self.confirmed))

    def test_a_change_of_drawn_metric_pushes(self) -> None:
        shown = self.view(("GPU", 85.0), ("NET", 84.0))
        swapped = self.view(("GPU", 85.0), ("NET", 86.0))
        self.assertNotEqual(shown.face, swapped.face)
        self.assertTrue(app.refresh_due(swapped, shown, app.PUSH_INTERVAL,
                                        change_streak=self.confirmed))

    def test_a_held_page_is_left_as_the_panel_has_it(self) -> None:
        # Repainting a held page with the falling reading cost a third write
        # per isolated flap, on a page about to be deleted anyway.
        shown = self.view(("CPU", 92))
        for value, since, streak in ((92, 40, 0), (30, app.PUSH_INTERVAL, self.confirmed),
                                     (30, app.KEEPALIVE_SECONDS + 10, self.confirmed + 4)):
            with self.subTest(value=value, since=since):
                live = app.sync_due([("CPU", value)], ("CPU",), now=since, published_at=0,
                                    published_view=shown, change_streak=streak)
                held = app.sync_due([("CPU", value)], ("CPU",), now=since, published_at=0,
                                    published_view=shown, change_streak=streak, held=True)
                self.assertTrue(live)
                self.assertFalse(held)
        # A held page the panel lost to a reboot is let go, not re-created.
        self.assertFalse(app.sync_due([("CPU", 30)], ("CPU",), now=5, published_at=0,
                                      published_view=None, held=True))
        self.assertTrue(app.sync_due([("CPU", 30)], ("CPU",), now=5, published_at=0,
                                     published_view=None))
        # Its end is still a write: the labels go.
        self.assertTrue(app.sync_due([], ("CPU",), now=31, published_at=0, held=True))

    def test_the_keepalive_comes_earlier_the_slower_each_reading_is(self) -> None:
        shown = self.view(("CPU", 86))
        self.assertFalse(app.refresh_due(shown, shown, 24.0))
        self.assertTrue(app.refresh_due(shown, shown, 24.0, overhead=0.5))
        self.assertFalse(app.refresh_due(shown, shown, 14.9, overhead=5.0))
        self.assertTrue(app.refresh_due(shown, shown, 15.0, overhead=5.0))
        self.assertTrue(app.keepalive_due(1.0, overhead=12.5))
        self.assertEqual(app.keepalive_due(30, overhead=-3), app.keepalive_due(30))

    def test_confirmation_costs_no_latency_over_the_old_cadence(self) -> None:
        # The old loop painted every PUSH_INTERVAL, so a change could wait up to
        # one INTERVAL past the first reading that saw it. Confirming on the
        # next reading waits exactly that long, never more.
        self.assertEqual(app.REFRESH_CONFIRM_SAMPLES, 2)
        self.assertLessEqual((app.REFRESH_CONFIRM_SAMPLES - 1) * app.INTERVAL,
                             app.PUSH_INTERVAL - app.INTERVAL)

    def test_an_unknown_or_relabelled_page_is_always_due(self) -> None:
        shown = self.view(("CPU", 86))
        self.assertTrue(app.refresh_due(shown, None, 0))
        self.assertTrue(app.refresh_due(self.view(("CPU", 86), ("RAM", 88)), shown, 0))

    def test_a_metric_the_page_does_not_draw_is_relabelled_in_place(self) -> None:
        cases = (
            # (published metrics, now active, relabelled without a write?)
            ([("CPU", 88)], [("CPU", 88), ("NET", 16.0)], True),
            ([("CPU", 88), ("NET", 16.0)], [("CPU", 88)], True),
            ([("CPU", 88), ("RAM", 90)], [("CPU", 88), ("RAM", 90), ("NET", 30.0)], True),
            ([("GPU", 95.0)], [("GPU", 95.0), ("NET", 40.0)], True),
            ([("CPU", 88)], [("CPU", 88), ("RAM", 90)], False),   # the jewel replaces the chip
            ([("CPU", 88)], [("RAM", 90)], False),
            ([("CPU", 85)], [("CPU", 85), ("NET", 95.0)], False),  # NET outranks it and is drawn
            ([("CPU", 88)], [("CPU", 88)], False),                 # nothing changed
            ([("CPU", 88)], [], False),                            # the alert went
        )
        for published, active, in_place in cases:
            with self.subTest(published=published, active=active):
                view = app.page_view(published)
                labels = tuple(label for label, _ in published)
                self.assertEqual(app.relabelled_in_place(active, labels, view), in_place)
                if in_place:
                    # ...because the page is byte for byte the one on the panel.
                    self.assertEqual(app.build_payload(active), app.build_payload(published))
        self.assertFalse(app.relabelled_in_place([("CPU", 88), ("NET", 16.0)], ("CPU",), None))
        self.assertFalse(app.relabelled_in_place([("CPU", 88), ("NET", 16.0)], None, None))

    def test_relabelling_in_place_always_means_an_identical_page(self) -> None:
        import random

        rng = random.Random(20261009)
        label_sets = (("CPU",), ("RAM",), ("GPU",), ("NET",), ("CPU", "RAM"), ("CPU", "NET"),
                      ("CPU", "RAM", "NET"), ("GPU", "NET"), ("CPU", "GPU"), ("CPU", "RAM", "GPU"))
        for _ in range(400):
            before, after = rng.sample(label_sets, 2)
            values = {"CPU": rng.uniform(60, 100), "RAM": rng.uniform(60, 100),
                      "GPU": rng.uniform(60, 100), "NET": rng.uniform(8, 120)}
            published = [(label, values[label]) for label in before]
            active = [(label, values[label]) for label in after]
            if app.relabelled_in_place(active, before, app.page_view(published)):
                with self.subTest(before=before, after=after, values=values):
                    self.assertEqual(json.dumps(app.build_payload(active), sort_keys=True),
                                     json.dumps(app.build_payload(published), sort_keys=True))


class MinimumDisplayTests(unittest.TestCase):
    readings = {"CPU": 41.0, "RAM": 52.0, "GPU": None, "NET": 0.0}

    def test_a_young_page_is_held_at_current_readings(self) -> None:
        self.assertEqual(
            app.held_metrics(("CPU",), self.readings, shown_since=100.0, now=110.0),
            [("CPU", 41.0)],
        )
        self.assertEqual(
            app.held_metrics(("CPU", "RAM"), self.readings, 100.0, 100.0 + app.MIN_DISPLAY_SECONDS - 0.1),
            [("CPU", 41.0), ("RAM", 52.0)],
        )

    def test_it_goes_once_it_has_been_up_long_enough(self) -> None:
        self.assertEqual(
            app.held_metrics(("CPU",), self.readings, 100.0, 100.0 + app.MIN_DISPLAY_SECONDS), []
        )

    def test_nothing_to_hold_without_a_page_or_a_reading(self) -> None:
        self.assertEqual(app.held_metrics((), self.readings, 100.0, 101.0), [])
        self.assertEqual(app.held_metrics(None, self.readings, 100.0, 101.0), [])
        self.assertEqual(app.held_metrics(("CPU",), self.readings, None, 101.0), [])
        self.assertEqual(app.held_metrics(("GPU",), self.readings, 100.0, 101.0), [])

    def test_window_outlasts_a_flap_and_never_needs_a_keepalive(self) -> None:
        flap_lifetime = app.CLEAR_SAMPLES * app.INTERVAL
        self.assertGreaterEqual(app.MIN_DISPLAY_SECONDS, 3 * flap_lifetime)
        self.assertLess(app.MIN_DISPLAY_SECONDS, app.LIFETIME_MS / 1000)

    def test_first_appearance_latency_is_untouched(self) -> None:
        self.assertEqual(app.TRIGGER_SAMPLES, 2)
        self.assertEqual(app.CLEAR_SAMPLES, 2)


class OutageBackoffTests(unittest.TestCase):
    def test_delay_doubles_from_the_reading_interval_to_the_cap(self) -> None:
        delays = [app.retry_delay(count) for count in range(1, 9)]
        self.assertEqual(delays, [5.0, 10.0, 20.0, 40.0, 60.0, 60.0, 60.0, 60.0])
        self.assertEqual(delays[0], app.INTERVAL)
        self.assertEqual(max(delays), app.RETRY_MAX_SECONDS)
        self.assertEqual(app.retry_delay(0), app.INTERVAL)
        self.assertEqual(app.retry_delay(10_000), app.RETRY_MAX_SECONDS)

    def test_a_page_pushed_before_a_boot_is_known_lost(self) -> None:
        health = app.PanelHealth(uptime_seconds=20, reset_reason="software")
        self.assertTrue(app.page_predates_boot(published_at=900.0, probed_at=1_000.0, health=health))
        self.assertTrue(app.page_predates_boot(published_at=980.0, probed_at=1_000.0, health=health))
        self.assertFalse(app.page_predates_boot(published_at=981.0, probed_at=1_000.0, health=health))
        long_up = app.PanelHealth(uptime_seconds=86_400, reset_reason="poweron")
        self.assertFalse(app.page_predates_boot(published_at=900.0, probed_at=1_000.0, health=long_up))


# ---------------------------------------------------------------- the loop
class _StopLoop(Exception):
    """Raised by the fake clock to end a main() run."""


class FakeClock:
    def __init__(self, end: float) -> None:
        self.t = 0.0
        self.end = end

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds
        if self.t >= self.end:
            raise _StopLoop


class FakePanel:
    """A panel on a fake clock: outages, reboots that drop pushed pages, lifetimes.

    ``writes`` records every attempted PUT and DELETE, failed or not, which is
    what panel_events.jsonl counts; ``gets`` records every health probe.
    ``lose_writes`` are spans where the panel answers GETs but every PUT and
    DELETE times out. ``refuse(t, url, body)`` may answer any write with a
    status instead of taking it.
    """

    def __init__(self, clock: FakeClock, outages=(), reboots=(), refuse=None,
                 lose_writes=()) -> None:
        self.clock = clock
        self.outages = tuple(outages)
        self.lose_writes = tuple(lose_writes)
        self.reboots = sorted(reboots)
        self.boot_at = -100_000.0
        self.reset_reason = "poweron"
        self.page: dict | None = None
        self.page_expires = 0.0
        self.writes: list[tuple[float, str, str, dict | None]] = []
        self.gets: list[float] = []
        self.lapses: list[tuple[float, float]] = []  # (expired, re-created)
        self.refuse = refuse

    def _now(self) -> float:
        t = self.clock.t
        while self.reboots and self.reboots[0][0] <= t:
            self.boot_at, self.reset_reason = self.reboots.pop(0)
            self.page = None
        return t

    def _unreachable(self, t: float, write: bool = False) -> bool:
        spans = self.outages + (self.lose_writes if write else ())
        return any(start <= t < end for start, end in spans)

    def _lost(self) -> None:
        self.clock.t += app.REQUEST_TIMEOUT
        raise app.requests.ConnectTimeout("no route to the panel")

    def put(self, url, json=None, timeout=None):
        t = self._now()
        self.writes.append((t, "put", url, json))
        if self._unreachable(t, write=True):
            self._lost()
        status = self.refuse(t, url, json) if self.refuse else None
        if status:
            return response(status, "refused")
        if url == app.URL:
            if self.page is not None and self.page_expires <= t:
                # The panel removed the page on its lifetime before this push
                # re-created it: a destroy and a create, however short the gap.
                self.lapses.append((self.page_expires, t))
            self.page = json
            self.page_expires = t + json["lifetimeMs"] / 1000
        return response(200)

    def delete(self, url, timeout=None):
        t = self._now()
        self.writes.append((t, "delete", url, None))
        if self._unreachable(t, write=True):
            self._lost()
        status = self.refuse(t, url, None) if self.refuse else None
        if status:
            return response(status, "refused")
        if url == app.DELETE_URL and self.showing(t):
            self.page = None
            return response(200)
        return response(404)

    def get(self, url, timeout=None):
        t = self._now()
        self.gets.append(t)
        if self._unreachable(t):
            self._lost()
        body = {"uptimeSeconds": int(t - self.boot_at), "resetReason": self.reset_reason}
        return Mock(status_code=200, json=Mock(return_value=body))

    def showing(self, t: float | None = None) -> bool:
        t = self._now() if t is None else t
        return self.page is not None and self.page_expires > t

    def pushes(self) -> list[tuple[float, dict]]:
        return [(t, body) for t, method, url, body in self.writes if method == "put" and url == app.URL]

    def page_deletes(self) -> list[float]:
        return [t for t, method, url, _ in self.writes if method == "delete" and url == app.DELETE_URL]


def run_loop(load, seconds: float, *, quiet: bool = False, overhead: float = 0.0,
             gpu=None, net=None, **panel_options):
    """Drive the real main() on a fake clock, with every request answered by FakePanel.

    ``overhead`` is the sampling time each reading costs, ``gpu(t)`` a GPU
    reading (None for a failed nvidia-smi call) and ``net(t)`` the cumulative
    bytes received; without them there is no GPU and no network traffic.

    The panel gate is switched off for the run. Under pytest the conftest
    already points it at a temp directory, but this helper imported from a
    plain ``python -c`` would otherwise lock and journal in the live senders'
    directory under LOCALAPPDATA, with real 0.4 s gaps on a fake clock.
    Journaling is test_panel_gate.py's subject, not this one's.
    """
    import os
    from types import SimpleNamespace

    import panel_gate

    clock = FakeClock(seconds)
    panel = FakePanel(clock, **panel_options)
    readings: list[float] = []
    timeline: list[tuple[float, bool]] = []

    def read_stats():
        readings.append(clock.t)
        clock.t += overhead
        return load(clock.t)

    def sleep(duration: float) -> None:
        timeline.append((clock.t, panel.showing()))
        clock.sleep(duration)

    sample_gpu = None if gpu is None else (lambda: SimpleNamespace(value=gpu(clock.t)))
    fake_time = SimpleNamespace(monotonic=clock.monotonic, sleep=sleep)
    fake_psutil = SimpleNamespace(
        cpu_percent=lambda interval=None: 0.0,
        net_io_counters=lambda: SimpleNamespace(bytes_recv=int(net(clock.t)) if net else 0),
        Error=type("PsutilError", (Exception,), {}),
    )
    with patch.dict(os.environ, {panel_gate.ENABLE_ENV: "0"}), \
            patch.object(app, "time", fake_time), \
            patch.object(app, "psutil", fake_psutil), \
            patch.object(app, "read_stats", read_stats), \
            patch.object(app, "sample_gpu", sample_gpu), \
            patch.object(app, "is_quiet_time", return_value=quiet), \
            patch.object(app, "print", create=True) as printed, \
            patch.object(app.requests, "put", side_effect=panel.put), \
            patch.object(app.requests, "delete", side_effect=panel.delete), \
            patch.object(app.requests, "get", side_effect=panel.get):
        try:
            app.main()
        except _StopLoop:
            pass
    return SimpleNamespace(panel=panel, readings=readings, timeline=timeline,
                           printed=[" ".join(map(str, c.args)) for c in printed.call_args_list])


def window(start: float, end: float, high: float, low: float = 20.0):
    """CPU at ``high`` from start to end, ``low`` otherwise; RAM idle."""
    return lambda t: ((high if start <= t < end else low), 50.0)


class LoopCadenceTests(unittest.TestCase):
    """main() itself, on a fake clock, against a fake panel."""

    def test_startup_cleanup_is_unchanged_and_a_quiet_pc_writes_nothing_more(self) -> None:
        run = run_loop(lambda t: (20.0, 50.0), 600)
        self.assertEqual(
            [(method, url) for _, method, url, _ in run.panel.writes],
            [("delete", app.LEGACY_DELETE_URL), *[("delete", u) for u in app.LEGACY_CHILD_URLS],
             ("delete", app.DELETE_URL)],
        )

    def test_first_appearance_is_on_the_second_high_reading_as_before(self) -> None:
        run = run_loop(window(20, 400, 88.0), 120)
        high = [t for t in run.readings if 20 <= t < 400]
        first_push = run.panel.pushes()[0][0]
        self.assertEqual(first_push, high[app.TRIGGER_SAMPLES - 1])
        self.assertTrue(any(url == app.ACTIVE_URL for _, _, url, _ in run.panel.writes))

    def test_a_steady_alert_is_kept_alive_not_repainted(self) -> None:
        run = run_loop(lambda t: (88.0 + (t % 3) * 0.5, 50.0), 7 * 60 + 10)
        times = [t for t, _ in run.panel.pushes()]
        gaps = [round(b - a, 6) for a, b in zip(times, times[1:])]
        self.assertTrue(gaps)
        self.assertTrue(all(app.KEEPALIVE_SECONDS <= gap <= app.KEEPALIVE_SECONDS + app.INTERVAL
                            for gap in gaps), gaps)
        # The old fixed cadence would have written every PUSH_INTERVAL.
        self.assertLessEqual(len(times), (7 * 60) // app.KEEPALIVE_SECONDS + 2)
        first = times[0]
        self.assertTrue(all(showing for t, showing in run.timeline if t >= first))

    def test_a_real_change_is_shown_within_push_interval(self) -> None:
        run = run_loop(lambda t: (86.0 if t < 60 else 96.0, 50.0), 120)
        first_high = min(t for t in run.readings if t >= 60)
        after = [(t, body) for t, body in run.panel.pushes() if t >= 60]
        self.assertTrue(after)
        self.assertLessEqual(after[0][0] - first_high, app.PUSH_INTERVAL)
        self.assertEqual(after[0][1], app.build_payload([("CPU", 96.0)]))

    def test_one_noisy_reading_does_not_repaint_the_page(self) -> None:
        # The alert goes up on the 5 s reading; no keep-alive is due before 30 s.
        # Single readings at 15 and 25 s jump 9 points (into "bad"), and one at
        # 40 s crosses the 90 band edge by 2. The old 10 s timer painted the
        # first two and left each on screen for 10 s after the load fell back.
        blips = {15: 97.0, 25: 97.0, 40: 90.0}
        load = lambda t: (blips.get(int(t) // 5 * 5, 88.0), 50.0)
        run = run_loop(load, 120)
        bodies = [body for _, body in run.panel.pushes()]
        for value in set(blips.values()):
            self.assertNotIn(app.build_payload([("CPU", value)]), bodies)
        times = [t for t, _ in run.panel.pushes()]
        gaps = [round(b - a, 6) for a, b in zip(times, times[1:])]
        self.assertTrue(all(gap >= app.KEEPALIVE_SECONDS for gap in gaps), gaps)

    def test_a_flap_stays_up_for_the_minimum_and_costs_no_extra_write(self) -> None:
        # Two high readings (the trigger), then idle: the journal's 10 s flap.
        run = run_loop(window(20, 30, 92.0, low=30.0), 120)
        pushes = run.panel.pushes()
        deletes = run.panel.page_deletes()
        shown_at = pushes[0][0]
        self.assertEqual(len(deletes), 2)  # the startup cleanup, then this page
        gone_at = deletes[-1]
        self.assertGreaterEqual(gone_at - shown_at, app.MIN_DISPLAY_SECONDS)
        self.assertLess(gone_at - shown_at, app.MIN_DISPLAY_SECONDS + app.INTERVAL)
        # The held page keeps the alert it raised; it is not repainted with
        # the falling reading. Push, activation, delete: the same three writes
        # the flap cost before the hold existed.
        self.assertEqual([body for _, body in pushes], [app.build_payload([("CPU", 92.0)])])
        flap_writes = [w for w in run.panel.writes if w[0] >= shown_at]
        self.assertEqual([(m, u) for _, m, u, _ in flap_writes],
                         [("put", app.URL), ("put", app.ACTIVE_URL), ("delete", app.DELETE_URL)])
        self.assertTrue(all(showing for t, showing in run.timeline if shown_at <= t < gone_at))

    def test_oscillating_load_never_repaints_a_held_page(self) -> None:
        # The square waves the review replayed, 85/50 for 10 minutes. While a
        # page is held its alert has cleared, so any write to it is spent on a
        # page about to go. On this fake panel the old fixed cadence wrote
        # 69, 70 and 53 times (20, 30 and 40 s periods) and the hold that
        # repainted held pages 67, 48 and 53.
        for period, ceiling in ((20, 52), (30, 38), (40, 38)):
            def load(t, period=period):
                return ((85.0 if t % period < period / 2 else 50.0), 50.0)

            with self.subTest(period=period):
                run = run_loop(load, 600)
                gate = app.MetricGate(app.CPU_SHOW_AT, app.CPU_CLEAR_BELOW)
                active = {t: gate.update(load(t)[0]) for t in run.readings}
                self.assertEqual([t for t, _ in run.panel.pushes() if not active[t]], [])
                self.assertLessEqual(len(run.panel.writes), ceiling)

    def test_an_alert_that_comes_back_inside_the_window_carries_on(self) -> None:
        load = lambda t: ((92.0 if 20 <= t < 30 or 45 <= t < 200 else 30.0), 50.0)
        run = run_loop(load, 240)
        page_deletes = run.panel.page_deletes()[1:]  # skip the startup cleanup
        first_push = run.panel.pushes()[0][0]
        self.assertTrue(all(t > 200 for t in page_deletes), page_deletes)
        self.assertTrue(all(showing for t, showing in run.timeline if first_push <= t < 200))
        activations = [t for t, _, url, _ in run.panel.writes if url == app.ACTIVE_URL]
        self.assertEqual(len(activations), 1)

    def test_an_outage_backs_off_writes_and_not_readings(self) -> None:
        outage = 80 * 60
        run = run_loop(lambda t: (20.0, 50.0), outage + 180, outages=[(0, outage)])
        attempts = [t for t, *_ in run.panel.writes if t < outage]
        gaps = [round(b - a, 6) for a, b in zip(attempts, attempts[1:])]
        self.assertTrue(all(b >= a for a, b in zip(gaps, gaps[1:])), gaps)
        self.assertGreaterEqual(gaps[-1], app.RETRY_MAX_SECONDS)
        self.assertLessEqual(gaps[-1], app.RETRY_MAX_SECONDS + app.INTERVAL)
        self.assertLess(len(attempts), outage / app.RETRY_MAX_SECONDS + 10)
        reading_gaps = [round(b - a, 6) for a, b in zip(run.readings, run.readings[1:])]
        on_time = sum(gap <= app.INTERVAL for gap in reading_gaps)
        self.assertGreater(on_time / len(reading_gaps), 0.9)
        # The cleanup completes within one capped wait of the panel returning.
        done = [t for t, method, url, _ in run.panel.writes if url == app.DELETE_URL and t >= outage]
        self.assertTrue(done)
        self.assertLessEqual(done[0] - outage, app.RETRY_MAX_SECONDS + app.INTERVAL)
        self.assertTrue(any("in a row" in line for line in run.printed))

    def test_an_alert_through_an_outage_returns_as_soon_as_the_panel_answers(self) -> None:
        # Every phase of the panel's return against the loop. The old code
        # retried on every reading and was back within one timed-out attempt
        # plus a sleep; probing every 30 s left the page out for up to 30.75 s.
        for length in (60, 120, 300, 1200):
            for offset in (0.0, 0.85, 1.7, 2.55, 3.4, 4.25, 5.1, 6.8, 8.5, 9.35):
                outage = (100 + offset, 100 + offset + length)
                with self.subTest(length=length, offset=offset):
                    run = run_loop(lambda t: (88.0, 50.0), outage[1] + 60, outages=[outage])
                    during = [t for t, *_ in run.panel.writes if outage[0] <= t < outage[1]]
                    self.assertLess(len(during), length / app.RETRY_MAX_SECONDS + 4)
                    back = min(t for t, _ in run.panel.pushes() if t >= outage[1])
                    self.assertLessEqual(back - outage[1], app.REQUEST_TIMEOUT + app.INTERVAL)

    def test_an_alert_that_starts_mid_outage_appears_once_the_panel_answers(self) -> None:
        # Probing every 30 s, one that started mid-way through a 300 s outage
        # first appeared up to 29.7 s after the panel answered.
        for length, alert_at in ((120, 120), (300, 200), (600, 360)):
            for offset in (0.0, 1.7, 3.4, 5.1, 6.8, 8.5):
                outage = (60 + offset, 60 + length + offset)
                with self.subTest(length=length, offset=offset):
                    run = run_loop(window(alert_at, 5000, 88.0), outage[1] + 60, outages=[outage])
                    first = min(t for t, _ in run.panel.pushes() if t >= outage[1])
                    self.assertLessEqual(first - outage[1], app.REQUEST_TIMEOUT + app.INTERVAL)

    def test_a_panel_that_answers_probes_but_drops_writes_still_gets_the_backoff(self) -> None:
        # Only a probe that fails first may lift the wait a second time, so a
        # GET answering is not taken as leave to write on every reading.
        lost = (100, 700)
        run = run_loop(lambda t: (88.0, 50.0), 800, lose_writes=[lost])
        attempts = [t for t, *_ in run.panel.writes if lost[0] <= t < lost[1]]
        gaps = [round(b - a, 6) for a, b in zip(attempts, attempts[1:])]
        self.assertLessEqual(len(attempts), 15)
        self.assertGreaterEqual(max(gaps), app.RETRY_MAX_SECONDS)
        self.assertTrue(any(t >= lost[1] for t, _ in run.panel.pushes()))
        # An idle panel is never probed at all.
        idle = run_loop(lambda t: (20.0, 50.0), 600, outages=[(0, 300)])
        self.assertEqual(idle.panel.gets, [])

    def test_a_refused_cleanup_backs_off_and_retries_only_the_blocking_slot(self) -> None:
        # Before, a panel answering 500 to the startup delete got all three
        # deletes again on every reading: 171 writes in 300 s.
        def refuse(t, url, body):
            return 500 if url == app.DELETE_URL and t < 300 else None

        run = run_loop(lambda t: (20.0, 50.0), 400, refuse=refuse)
        early = [(t, url) for t, method, url, _ in run.panel.writes if t < 300]
        legacy = [url for _, url in early if url != app.DELETE_URL]
        self.assertEqual(legacy, [app.LEGACY_DELETE_URL, *app.LEGACY_CHILD_URLS])
        self.assertLessEqual(len(early), 12)
        slot = [t for t, url in early if url == app.DELETE_URL]
        self.assertLessEqual(slot[1] - slot[0], app.INTERVAL + 0.01)  # first retry: next reading
        done = [t for t, method, url, _ in run.panel.writes if t >= 300]
        self.assertTrue(done)
        self.assertLessEqual(done[0] - 300, app.RETRY_MAX_SECONDS + app.INTERVAL)
        self.assertEqual(len(done), 1)

    def test_refused_pushes_back_off_like_lost_ones(self) -> None:
        # A heap-starved panel answering 507 is where a write costs the most.
        def refuse(t, url, body):
            return 507 if url == app.URL and 100 <= t < 300 else None

        run = run_loop(lambda t: (88.0, 50.0), 400, refuse=refuse)
        refused = [t for t, method, url, _ in run.panel.writes if url == app.URL and 100 <= t < 300]
        self.assertLessEqual(len(refused), 8)
        gaps = [round(b - a, 6) for a, b in zip(refused, refused[1:])]
        self.assertTrue(all(b >= a for a, b in zip(gaps, gaps[1:])), gaps)
        back = [t for t, _ in run.panel.pushes() if t >= 300]
        self.assertTrue(back)
        self.assertLessEqual(back[0] - 300, app.RETRY_MAX_SECONDS + app.INTERVAL)

    def test_one_lost_keepalive_never_costs_the_page_at_any_loop_overhead(self) -> None:
        # The fixed 25 s threshold lost the page to one dropped keep-alive with
        # 3.0-3.3 s or 5 s of sampling overhead (nvidia-smi's timeout is 5 s).
        steady = lambda t: (88.0, 50.0)  # noqa: E731
        for overhead in (0.0, 0.15, 1.0, 2.5, 2.9, 3.0, 3.25, 4.0, 5.0, 6.0):
            times = [t for t, _ in run_loop(steady, 300, overhead=overhead).panel.pushes()]
            for keepalive in times[1:4]:
                with self.subTest(overhead=overhead, keepalive=keepalive):
                    run = run_loop(steady, 300, overhead=overhead,
                                   lose_writes=[(keepalive - 0.01, keepalive + 0.01)])
                    self.assertIn(keepalive, [t for t, *_ in run.panel.writes])
                    self.assertEqual(run.panel.lapses, [])

    def test_the_sixty_second_lifetime_rides_out_dropouts_up_to_25_seconds(self) -> None:
        # 2026-10-09: LIFETIME_MS 60 s with the keep-alive still at 25 s gives
        # a lost keep-alive a second retry (see the comment above KEEPALIVE_SECONDS).
        self.assertEqual((app.LIFETIME_MS, app.KEEPALIVE_SECONDS), (60_000, 25))
        for length in (15, 20, 25):
            for k in range(0, 120, 6):
                start = 100 + k * 0.5
                with self.subTest(length=length, start=start):
                    run = run_loop(lambda t: (88.0, 50.0), start + length + 90,
                                   outages=[(start, start + length)], overhead=0.15)
                    self.assertEqual(run.panel.lapses, [])

    def test_no_dropout_of_ten_seconds_or_less_costs_the_page(self) -> None:
        # Longer dropouts that span the keep-alive and its retry can; the
        # figures are in the comment above KEEPALIVE_SECONDS.
        for length in (5, 8, 10):
            for k in range(0, 120):
                start = 100 + k * 0.5
                with self.subTest(length=length, start=start):
                    run = run_loop(lambda t: (88.0, 50.0), start + length + 60,
                                   outages=[(start, start + length)])
                    self.assertEqual(run.panel.lapses, [])

    def test_net_crossing_beside_a_cpu_alert_costs_no_write(self) -> None:
        # NET above 10 MB/s next to a CPU alert leaves the page byte for byte
        # the same, yet each crossing re-pushed it at once: 51 pushes in ten
        # minutes against 24 without NET.
        def net(t):
            # 20 MB/s and 2 MB/s in turn, about every 12 s
            whole, part = divmod(t, 24.0)
            return (whole * 12 * 22 + min(part, 12) * 20 + max(0.0, part - 12) * 2) * 1024 * 1024

        for ram in (50.0, 90.0):  # beside CPU, and beside the CPU+RAM jewel
            with self.subTest(ram=ram):
                with_net = run_loop(lambda t: (88.0, ram), 600, net=net)
                without = run_loop(lambda t: (88.0, ram), 600)
                self.assertEqual(len(with_net.panel.pushes()), len(without.panel.pushes()))
                self.assertEqual(with_net.panel.lapses, [])
                first = with_net.panel.pushes()[0][0]
                self.assertTrue(all(showing for t, showing in with_net.timeline if t >= first))

    def test_a_failed_gpu_sample_does_not_flap_a_gpu_alert(self) -> None:
        count = {"n": 0}

        def gpu(t):
            count["n"] += 1
            if 30 <= t < 600:
                return None if count["n"] % 13 == 0 else 95.0
            return 10.0

        run = run_loop(lambda t: (30.0, 50.0), 700, gpu=gpu)
        page_deletes = run.panel.page_deletes()[1:]  # skip the startup cleanup
        self.assertEqual(len(page_deletes), 1)
        self.assertGreater(page_deletes[0], 600)
        activations = [t for t, _, url, _ in run.panel.writes if url == app.ACTIVE_URL]
        self.assertEqual(len(activations), 1)
        # A sampler that stays dead does not hold a frozen GPU alert up.
        run = run_loop(lambda t: (30.0, 50.0), 400,
                       gpu=lambda t: 95.0 if 30 <= t < 200 else (None if t >= 200 else 10.0))
        page_deletes = run.panel.page_deletes()[1:]
        self.assertEqual(len(page_deletes), 1)
        self.assertLess(page_deletes[0] - 200, (app.CLEAR_SAMPLES + 2) * app.INTERVAL)

    def test_a_planned_reboot_restores_the_page_with_its_animation(self) -> None:
        reboot = 200.0
        run = run_loop(lambda t: (88.0, 50.0), 400, reboots=[(reboot, "software")],
                       outages=[(reboot, reboot + 10)])
        restored = min(t for t, _ in run.panel.pushes() if t > reboot)
        self.assertLessEqual(restored - (reboot + 10),
                             max(app.HEALTH_PROBE_SECONDS, app.KEEPALIVE_SECONDS) + app.INTERVAL)
        self.assertEqual(run.panel.pushes()[-1][1], app.build_payload([("CPU", 88.0)], animated=True))
        self.assertFalse(any("holding static" in line for line in run.printed))

    def test_a_crash_reboot_still_drops_to_static_at_once(self) -> None:
        reboot = 200.0
        run = run_loop(lambda t: (88.0, 50.0), 400, reboots=[(reboot, "panic")])
        self.assertTrue(any("holding static" in line for line in run.printed))
        detected = [t for t, body in run.panel.pushes()
                    if t > reboot and body == app.build_payload([("CPU", 88.0)], animated=False)]
        self.assertTrue(detected)
        probes_after = [t for t in run.readings if t > reboot]
        self.assertLessEqual(detected[0] - reboot, app.HEALTH_PROBE_SECONDS + app.INTERVAL)
        self.assertTrue(probes_after)

    def test_a_refused_page_falls_back_to_static_on_the_next_reading(self) -> None:
        refused = []

        def refuse_first_animated(t, url, body):
            if url == app.URL and not refused:
                refused.append(t)
                return 507
            return None

        run = run_loop(lambda t: (88.0, 50.0), 120, refuse=refuse_first_animated)
        static = app.build_payload([("CPU", 88.0)], animated=False)
        retry = [t for t, body in run.panel.pushes() if t > refused[0]]
        self.assertAlmostEqual(retry[0] - refused[0], app.INTERVAL)
        self.assertEqual(run.panel.pushes()[1][1], static)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class PlannedRebootTests(unittest.TestCase):
    def test_requested_restart_keeps_animation_and_a_crash_does_not(self) -> None:
        old = app.PanelHealth(900, "poweron")
        self.assertTrue(app.unplanned_reboot(old, app.PanelHealth(12, "panic")))
        self.assertFalse(app.unplanned_reboot(old, app.PanelHealth(12, "software")))
        self.assertFalse(app.unplanned_reboot(old, app.PanelHealth(950, "panic")))

