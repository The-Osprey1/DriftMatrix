"""Unit tests for Desk Companion reactive avatar."""

from __future__ import annotations

import base64
from datetime import datetime
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

import desk_companion as dc
import focus_sprint as fs
from icon_library import companion_icon
from panel_client import PanelClient, PanelPolicy
import requests
import visual_system as vs


class DeskCompanionTests(unittest.TestCase):
    def test_all_mood_icons_are_valid_gifs_on_120ms_beat(self) -> None:
        for mood in dc.CompanionMood:
            for animated in (True, False):
                with self.subTest(mood=mood, animated=animated):
                    data = dc.build_companion_icon_gif(mood, animated=animated)
                    self.assertLessEqual(len(data), dc.ICON_MAX_BYTES)
                    with Image.open(BytesIO(data)) as img:
                        self.assertEqual(img.size, (8, 8))
                        self.assertNotIn("transparency", img.info)
                        for idx in range(img.n_frames):
                            img.seek(idx)
                            dur = int(img.info.get("duration", 0))
                            if dur:
                                self.assertEqual(dur % dc.FRAME_BEAT_MS, 0)

    def test_lane_rendering_keeps_safety_gutter_and_dimensions(self) -> None:
        for mood in dc.CompanionMood:
            with self.subTest(mood=mood):
                frame = dc.render_companion_lane(mood)
                for y in range(dc.HEIGHT):
                    self.assertEqual(frame[y][dc.GUTTER_X], dc.BLACK)

    def test_payload_safety_budget(self) -> None:
        for mood in dc.CompanionMood:
            with self.subTest(mood=mood):
                payload = dc.build_companion_payload(mood, animated=True)
                self.assertEqual(payload["iconMode"], "fixed")
                self.assertEqual(payload["backgroundColor"], "#000000")
                raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                self.assertLess(len(raw), dc.MAX_PAYLOAD_BYTES)

    def test_controller_reacts_to_mood_transitions(self) -> None:
        panel = MagicMock()
        clock = MagicMock()
        clock.monotonic.return_value = 100.0

        current_mood = dc.CompanionMood.IDLE

        def detect_mood():
            return current_mood

        controller = dc.DeskCompanionController(
            panel=panel,
            policy=dc.CompanionPolicy(refresh_seconds=10.0),
            mood_detector=detect_mood,
            clock=clock,
        )

        # Initial tick
        t1 = controller.tick()
        self.assertTrue(t1.pushed)
        self.assertEqual(t1.mood, dc.CompanionMood.IDLE)
        panel.push_app.assert_called_once()

        panel.push_app.reset_mock()

        # Same mood before interval -> no push
        clock.monotonic.return_value = 105.0
        t2 = controller.tick()
        self.assertFalse(t2.pushed)
        panel.push_app.assert_not_called()

        # Mood changes to FOCUS -> immediate push!
        current_mood = dc.CompanionMood.FOCUS
        t3 = controller.tick()
        self.assertTrue(t3.pushed)
        self.assertEqual(t3.mood, dc.CompanionMood.FOCUS)
        panel.push_app.assert_called_once()

    def test_optical_1bit_legibility_and_safety_constraints(self) -> None:
        """Verify 1-bit nightkeeper legibility, gutter safety, and payload limits across all moods."""
        reminder_labels = [None, "STRETCH", "WATER", "BREAK", "REST EYES"]
        for mood in dc.CompanionMood:
            # 1. Avatar frames from icon_library
            frames = companion_icon(mood)
            for p, f in enumerate(frames):
                # Verify backward compatibility with draw_companion_avatar
                compat_f = vs.new_frame()
                dc.draw_companion_avatar(compat_f, mood, p)
                self.assertEqual(f, compat_f)

                lit = [f[y][x] for y in range(8) for x in range(8) if f[y][x] != (0, 0, 0)]
                # The creature's silhouette survives brightness 1; its shadowed
                # base and feet may fade, as Now Playing's dark rim does.
                self.assertGreaterEqual(sum(max(px) >= 174 for px in lit), 0.6 * len(lit), f"mood {mood} frame {p}")

            # 2. Data lane frames
            for label in reminder_labels:
                lane = dc.render_companion_lane(mood, custom_text=label)
                for y in range(dc.HEIGHT):
                    # Column 8 must remain strictly black
                    self.assertEqual(lane[y][dc.GUTTER_X], dc.BLACK)
                    # Text band (rows 1..5)
                    if 1 <= y <= 5:
                        for x in range(dc.LANE_X0, dc.WIDTH):
                            px = lane[y][x]
                            if px != dc.BLACK:
                                self.assertGreaterEqual(
                                    max(px), 174,
                                    f"Lane text pixel at ({x},{y}) in mood {mood} label {label} < 174: {px}"
                                )
                    # Ground rail (row 7)
                    elif y == 7:
                        self.assertTrue(vs.rail_survives_brightness_one(lane),
                                        f"Lane rail in mood {mood} loses its leader at brightness 1")

                # 3. Payload size
                payload = dc.build_companion_payload(mood, custom_text=label, animated=True)
                raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                self.assertLessEqual(len(raw), dc.MAX_PAYLOAD_BYTES)
                self.assertEqual(payload["iconMode"], "fixed")
                self.assertEqual(payload["backgroundColor"], "#000000")

    def test_ergonomic_reminder_custom_text_and_truncation(self) -> None:
        """Verify custom reminders render cleanly and truncate long strings to fit 23px."""
        # Short strings that fit within 23px
        for label in ("WATER", "BREAK", "TEA", "STAND", "REST"):
            lane = dc.render_companion_lane(dc.CompanionMood.REST, custom_text=label)
            for y in range(dc.HEIGHT):
                self.assertEqual(lane[y][dc.GUTTER_X], dc.BLACK)

        # Longer strings that require truncation
        for long_label in ("STRETCH", "REST EYES", "HYDRATE NOW"):
            lane = dc.render_companion_lane(dc.CompanionMood.REST, custom_text=long_label)
            for y in range(dc.HEIGHT):
                self.assertEqual(lane[y][dc.GUTTER_X], dc.BLACK)

        # Blank / whitespace fallback
        default_lane = dc.render_companion_lane(dc.CompanionMood.IDLE)
        empty_lane = dc.render_companion_lane(dc.CompanionMood.IDLE, custom_text="   ")
        self.assertEqual(default_lane, empty_lane)

    def test_detect_system_companion_mood_sleep_during_quiet_hours(self) -> None:
        """Verify quiet hours triggers SLEEP mood."""
        # 23:00 is within default quiet hours (21:30 - 07:00)
        night_time = datetime(2026, 10, 2, 23, 0)
        mood, text = dc.detect_system_companion_mood(now=night_time)
        self.assertEqual(mood, dc.CompanionMood.SLEEP)
        self.assertEqual(text, "SLEEP")

        # 03:00 is also within quiet hours
        early_time = datetime(2026, 10, 2, 3, 0)
        mood2, text2 = dc.detect_system_companion_mood(now=early_time)
        self.assertEqual(mood2, dc.CompanionMood.SLEEP)

    def test_detect_system_companion_mood_on_air_microphone_active(self) -> None:
        """Verify active microphone session triggers ON_AIR mood when outside quiet hours."""
        daytime = datetime(2026, 10, 2, 14, 0)
        active_session = MagicMock()
        with patch("meeting_sentinel.scan_windows_microphone_registry", return_value=active_session):
            mood, text = dc.detect_system_companion_mood(now=daytime)
            self.assertEqual(mood, dc.CompanionMood.ON_AIR)
            self.assertEqual(text, "ON AIR")

    def test_detect_system_companion_mood_focus_sprint(self) -> None:
        """Verify focus sprint state triggers FOCUS or REST mood."""
        daytime = datetime(2026, 10, 2, 14, 0)
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "focus_state.json"

            # 1. Focus running
            fs.save_state(state_path, fs.SprintState(phase=fs.Phase.FOCUS_RUNNING))
            with patch("meeting_sentinel.scan_windows_microphone_registry", return_value=None):
                mood, text = dc.detect_system_companion_mood(state_dir=tmpdir, now=daytime)
                self.assertEqual(mood, dc.CompanionMood.FOCUS)
                self.assertEqual(text, "FOCUS")

            # 2. Short break running
            fs.save_state(state_path, fs.SprintState(phase=fs.Phase.SHORT_BREAK_RUNNING))
            with patch("meeting_sentinel.scan_windows_microphone_registry", return_value=None):
                mood, text = dc.detect_system_companion_mood(state_dir=tmpdir, now=daytime)
                self.assertEqual(mood, dc.CompanionMood.REST)
                self.assertEqual(text, "REST")

            # 3. Focus sprint idle -> falls through
            fs.save_state(state_path, fs.SprintState(phase=fs.Phase.IDLE))
            with patch("meeting_sentinel.scan_windows_microphone_registry", return_value=None):
                mood, text = dc.detect_system_companion_mood(state_dir=tmpdir, now=daytime)
                self.assertEqual(mood, dc.CompanionMood.IDLE)
                self.assertEqual(text, "READY")

    def test_detect_system_companion_mood_desk_reminder(self) -> None:
        """Verify due desk reminder triggers REST mood with custom label."""
        daytime = datetime(2026, 10, 2, 14, 0)
        now_ts = daytime.timestamp()

        with tempfile.TemporaryDirectory() as tmpdir:
            reminder_path = Path(tmpdir) / "desk_reminder.json"

            # Reminder is due
            reminder_data = {
                "schema_version": 1,
                "id": "11111111-2222-3333-4444-555555555555",
                "label": "WATER",
                "created_at": now_ts - 1800,
                "due_at": now_ts - 60,  # 1 min ago -> "due"
            }
            reminder_path.write_text(json.dumps(reminder_data), encoding="utf-8")

            with patch("meeting_sentinel.scan_windows_microphone_registry", return_value=None):
                mood, text = dc.detect_system_companion_mood(state_dir=tmpdir, now=daytime)
                self.assertEqual(mood, dc.CompanionMood.REST)
                self.assertEqual(text, "WATER")

            # Test different custom reminder label
            reminder_data["label"] = "STRETCH"
            reminder_path.write_text(json.dumps(reminder_data), encoding="utf-8")
            with patch("meeting_sentinel.scan_windows_microphone_registry", return_value=None):
                mood, text = dc.detect_system_companion_mood(state_dir=tmpdir, now=daytime)
                self.assertEqual(mood, dc.CompanionMood.REST)
                self.assertEqual(text, "STRETCH")

            # Reminder is still counting down (not due)
            reminder_data["due_at"] = now_ts + 600  # 10 min in the future -> "countdown"
            reminder_path.write_text(json.dumps(reminder_data), encoding="utf-8")
            with patch("meeting_sentinel.scan_windows_microphone_registry", return_value=None):
                mood, text = dc.detect_system_companion_mood(state_dir=tmpdir, now=daytime)
                self.assertEqual(mood, dc.CompanionMood.IDLE)
                self.assertEqual(text, "READY")

    def test_detect_system_companion_mood_default_idle_and_fault_tolerance(self) -> None:
        """Verify default fallback to IDLE and resilience against corrupt/missing state."""
        daytime = datetime(2026, 10, 2, 14, 0)
        with patch("meeting_sentinel.scan_windows_microphone_registry", return_value=None):
            # Clean non-existent dir
            mood, text = dc.detect_system_companion_mood(state_dir="C:/non_existent_dir_12345", now=daytime)
            self.assertEqual(mood, dc.CompanionMood.IDLE)
            self.assertEqual(text, "READY")

            # Corrupt files in dir
            with tempfile.TemporaryDirectory() as tmpdir:
                (Path(tmpdir) / "focus_state.json").write_text("{not-valid-json", encoding="utf-8")
                (Path(tmpdir) / "desk_reminder.json").write_text("random binary garbage", encoding="utf-8")
                mood2, text2 = dc.detect_system_companion_mood(state_dir=tmpdir, now=daytime)
                self.assertEqual(mood2, dc.CompanionMood.IDLE)
                self.assertEqual(text2, "READY")

    def test_controller_handles_mood_tuples_and_custom_text(self) -> None:
        """Verify controller handles tuple (mood, custom_text) and updates lane accordingly."""
        panel = MagicMock()
        clock = MagicMock()
        clock.monotonic.return_value = 100.0

        current_result = (dc.CompanionMood.REST, "WATER")

        controller = dc.DeskCompanionController(
            panel=panel,
            policy=dc.CompanionPolicy(refresh_seconds=10.0),
            mood_detector=lambda: current_result,
            clock=clock,
        )

        # Initial tick
        t1 = controller.tick()
        self.assertTrue(t1.pushed)
        self.assertEqual(t1.mood, dc.CompanionMood.REST)
        self.assertEqual(t1.custom_text, "WATER")
        panel.push_app.assert_called_once()
        payload1 = panel.push_app.call_args[0][1]
        self.assertIn("bitmap", payload1["draw"][0][0])

        panel.push_app.reset_mock()

        # Unchanged state before refresh interval -> no push
        clock.monotonic.return_value = 105.0
        t2 = controller.tick()
        self.assertFalse(t2.pushed)
        panel.push_app.assert_not_called()

        # Custom text changes while mood stays REST -> immediate push!
        current_result = (dc.CompanionMood.REST, "STRETCH")
        t3 = controller.tick()
        self.assertTrue(t3.pushed)
        self.assertEqual(t3.mood, dc.CompanionMood.REST)
        self.assertEqual(t3.custom_text, "STRETCH")
        panel.push_app.assert_called_once()


class CompanionPushBudgetTests(unittest.TestCase):
    """The page is re-pushed on change, not on a fast timer (2026-10-04 panic)."""

    def test_steady_mood_pushes_rarely_but_never_expires(self):
        from unittest.mock import MagicMock
        panel, clock = MagicMock(), MagicMock()
        clock.monotonic.return_value = 0.0
        controller = dc.DeskCompanionController(panel=panel, mood_detector=lambda: dc.CompanionMood.IDLE, clock=clock)
        pushes_at = []
        for second in range(0, 3600, 5):
            clock.monotonic.return_value = float(second)
            if controller.tick().pushed:
                pushes_at.append(second)
        self.assertLessEqual(len(pushes_at), 16)  # was 240 an hour
        gaps = [b - a for a, b in zip(pushes_at, pushes_at[1:])] + [3600 - pushes_at[-1]]
        self.assertLess(max(gaps) * 1000 * 2, dc.LIFETIME_MS + 10_000)

    def test_mood_change_still_pushes_at_once(self):
        from unittest.mock import MagicMock
        panel, clock = MagicMock(), MagicMock()
        clock.monotonic.return_value = 0.0
        mood = [dc.CompanionMood.IDLE]
        controller = dc.DeskCompanionController(panel=panel, mood_detector=lambda: mood[0], clock=clock)
        controller.tick()
        clock.monotonic.return_value = 5.0
        mood[0] = dc.CompanionMood.FOCUS
        self.assertTrue(controller.tick().pushed)

    def test_hub_rejects_a_keepalive_slower_than_the_lifetime(self):
        import utility_hub
        with self.assertRaises(ValueError):
            utility_hub.config_from_mapping({"companion": {"enabled": True, "refresh_seconds": 400}})

    def test_uptime_rollback_freezes_each_mood_without_changing_lane_or_lifetime(self):
        for mood in dc.CompanionMood:
            with self.subTest(mood=mood):
                session, clock = MagicMock(), MagicMock()
                session.put.return_value.status_code = 200
                session.get.return_value.status_code = 200
                session.get.return_value.json.side_effect = [
                    {"uptimeSeconds": 100}, {"uptimeSeconds": 10, "resetReason": "panic"},
                ]
                panel = PanelClient(
                    "http://panel.invalid", session=session, clock=clock,
                    policy=PanelPolicy(minimum_mutation_interval_seconds=0),
                )
                controller = dc.DeskCompanionController(
                    panel=panel, mood_detector=lambda: (mood, "WATER"), clock=clock,
                )
                clock.monotonic.return_value = 0.0
                self.assertTrue(controller.tick().pushed)
                animated = session.put.call_args.kwargs["json"]

                panel.probe_device()
                panel.probe_device()
                self.assertTrue(panel.static_latched)
                clock.monotonic.return_value = 5.0
                self.assertTrue(controller.tick().pushed)
                static = session.put.call_args.kwargs["json"]

                self.assertEqual(
                    {key: value for key, value in static.items() if key != "icon"},
                    {key: value for key, value in animated.items() if key != "icon"},
                )
                with Image.open(BytesIO(base64.b64decode(static["icon"]))) as frozen:
                    self.assertEqual(frozen.n_frames, 1)
                    with Image.open(BytesIO(base64.b64decode(animated["icon"]))) as moving:
                        self.assertEqual(frozen.convert("RGB").tobytes(), moving.convert("RGB").tobytes())

    def test_software_reboot_restores_page_before_keepalive_without_repeated_pushes(self):
        for requested in (False, True):
            with self.subTest(requested=requested):
                session, clock = MagicMock(), MagicMock()
                session.put.return_value.status_code = 200
                session.post.return_value.status_code = 200
                session.get.return_value.status_code = 200
                session.get.return_value.json.side_effect = [
                    {"uptimeSeconds": 100}, {"uptimeSeconds": 10, "resetReason": "software"},
                ]
                panel = PanelClient(
                    "http://panel.invalid", session=session, clock=clock,
                    policy=PanelPolicy(minimum_mutation_interval_seconds=0),
                )
                controller = dc.DeskCompanionController(
                    panel=panel, mood_detector=lambda: dc.CompanionMood.IDLE, clock=clock,
                )
                clock.monotonic.return_value = 0.0
                self.assertTrue(controller.tick().pushed)
                panel.probe_device()
                if requested:
                    panel.reboot()
                panel.probe_device()
                self.assertFalse(panel.static_latched)

                clock.monotonic.return_value = 5.0
                self.assertTrue(controller.tick().pushed)
                self.assertEqual(controller.last_push_generation, panel.reboot_generation)
                self.assertEqual(session.put.call_count, 2)
                with Image.open(BytesIO(base64.b64decode(session.put.call_args.kwargs["json"]["icon"]))) as icon:
                    self.assertGreater(icon.n_frames, 1)
                clock.monotonic.return_value = 10.0
                self.assertFalse(controller.tick().pushed)
                self.assertEqual(session.put.call_count, 2)

    def test_failed_reboot_restoration_does_not_cache_generation_until_success(self):
        panel, clock = MagicMock(), MagicMock()
        panel.reboot_generation = 0
        controller = dc.DeskCompanionController(
            panel=panel, mood_detector=lambda: dc.CompanionMood.IDLE, clock=clock,
        )
        clock.monotonic.return_value = 0.0
        self.assertTrue(controller.tick().pushed)
        panel.reboot_generation = 1
        panel.push_app.side_effect = [requests.ConnectionError("offline"), None]

        clock.monotonic.return_value = 5.0
        self.assertFalse(controller.tick().pushed)
        self.assertEqual(controller.last_push_generation, 0)
        self.assertEqual(controller.last_push_at, 0.0)
        clock.monotonic.return_value = 10.0
        self.assertTrue(controller.tick().pushed)
        self.assertEqual(controller.last_push_generation, 1)
        self.assertEqual(controller.last_push_at, 10.0)
        clock.monotonic.return_value = 15.0
        self.assertFalse(controller.tick().pushed)
        self.assertEqual(panel.push_app.call_count, 3)

    def test_failed_push_retries_with_static_art_and_keeps_success_cache_empty(self):
        session, clock = MagicMock(), MagicMock()
        response = MagicMock(status_code=200)
        session.put.side_effect = [requests.ConnectionError("offline"), response]
        panel = PanelClient(
            "http://panel.invalid", session=session, clock=clock,
            policy=PanelPolicy(minimum_mutation_interval_seconds=0, static_after_failures=1),
        )
        controller = dc.DeskCompanionController(
            panel=panel, mood_detector=lambda: dc.CompanionMood.IDLE, clock=clock,
        )
        clock.monotonic.return_value = 0.0
        self.assertFalse(controller.tick().pushed)
        self.assertIsNone(controller.last_push_at)
        self.assertIsNone(controller.current_mood)
        self.assertTrue(panel.static_latched)

        clock.monotonic.return_value = 5.0
        self.assertTrue(controller.tick().pushed)
        self.assertEqual(controller.last_push_at, 5.0)
        self.assertEqual(controller.current_mood, dc.CompanionMood.IDLE)
        with Image.open(BytesIO(base64.b64decode(session.put.call_args.kwargs["json"]["icon"]))) as icon:
            self.assertEqual(icon.n_frames, 1)


if __name__ == "__main__":
    unittest.main()
