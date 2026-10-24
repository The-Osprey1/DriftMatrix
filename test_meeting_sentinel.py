"""Deterministic unit tests for Meeting Sentinel (On-Air / Mic busy indicator)."""

from __future__ import annotations

import base64
from io import BytesIO
import json
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

import meeting_sentinel as ms
import visual_system as vs


def meter(frame):
    """(filled LEDs, their colour) on the row-7 meter; (0, None) for a bare track."""
    return vs.read_meter(frame)


class MeetingSentinelTests(unittest.TestCase):
    def test_filetime_to_unix_conversion(self) -> None:
        # 134334861652647102 corresponds to ~2026-09-09 20:56:05 UTC / ~1789012565 Unix timestamp
        ft = 134334861652647102
        unix_ts = ms.filetime_to_unix(ft)
        self.assertAlmostEqual(unix_ts, 1789012565.26, delta=1.0)
        # 0 or negative/below epoch returns 0.0
        self.assertEqual(ms.filetime_to_unix(0), 0.0)
        self.assertEqual(ms.filetime_to_unix(ms.FILETIME_EPOCH_DIFF - 100), 0.0)

    def test_format_duration(self) -> None:
        self.assertEqual(ms.format_duration(0), "00:00")
        self.assertEqual(ms.format_duration(45), "00:45")
        self.assertEqual(ms.format_duration(125), "02:05")
        # At and above an hour the form changes to NhMM with an explicit H.
        # It used to stay MM:SS-shaped with no unit marker, so a 125-second
        # call and a 7,500-second call both rendered "02:05", and a 61-minute
        # call rendered "01:01" -- which reads as one minute and one second.
        self.assertEqual(ms.format_duration(3600), "1H00")
        self.assertEqual(ms.format_duration(3665), "1H01")
        self.assertEqual(ms.format_duration(7500), "2H05")
        self.assertNotEqual(ms.format_duration(125), ms.format_duration(7500))
        # Hours are capped so the string cannot outgrow the 21px lane budget,
        # and the cap has to LOOK capped.  Asserting "99H00" here encoded the
        # defect: the minutes were computed from the uncapped total, so 150
        # hours rendered as a precise-looking "99H00" that a genuine 99-hour
        # call renders identically, and 31 years rendered "99H46".
        self.assertEqual(ms.format_duration(200 * 3600), "99H--")
        self.assertEqual(ms.format_duration(10 ** 9), "99H--")
        self.assertNotEqual(ms.format_duration(99 * 3600), ms.format_duration(200 * 3600))
        self.assertLessEqual(ms.text_width(ms.format_duration(10 ** 9)), ms.LANE_WIDTH - 2)

    def test_onair_icon_is_valid_gif_on_120ms_beat(self) -> None:
        for animated in (True, False):
            with self.subTest(animated=animated):
                data = ms.build_onair_icon_gif(animated=animated)
                self.assertLessEqual(len(data), ms.ICON_MAX_BYTES)
                ms.validate_icon_gif(data)
                with Image.open(BytesIO(data)) as img:
                    self.assertEqual(img.size, (8, 8))
                    self.assertNotIn("transparency", img.info)
                    for frame_idx in range(img.n_frames):
                        img.seek(frame_idx)
                        dur = int(img.info.get("duration", 0))
                        if dur:
                            self.assertEqual(dur % ms.FRAME_BEAT_MS, 0)

    def test_meter_is_the_hour_and_turns_gold_past_it(self) -> None:
        def lane(seconds, known=True):
            return ms.render_lane_frame(
                ms.ActiveMicSession("Zoom", "zoom.exe", 0.0, seconds, elapsed_known=known))
        quarter, half = meter(lane(15 * 60)), meter(lane(30 * 60))
        self.assertLess(quarter[0], half[0])
        self.assertEqual(half[1], vs.LEVEL_COLORS["info"])
        self.assertEqual(meter(lane(75 * 60)), (vs.LANE_WIDTH, vs.LEVEL_COLORS["fair"]))
        # Undatable: no number, and no meter pretending to be one.
        self.assertEqual(meter(lane(0.0, known=False)), (0, None))

    def test_render_lane_has_black_gutter_and_fits_dimensions(self) -> None:
        session = ms.ActiveMicSession(
            app_name="Discord",
            exe_path="C:\\Discord\\Discord.exe",
            started_at=100.0,
            elapsed_seconds=145.0,
        )
        frame = ms.render_lane_frame(session)
        # Column 8 must be completely black
        for y in range(ms.HEIGHT):
            self.assertEqual(frame[y][ms.GUTTER_X], ms.BLACK)

        b64_lane = ms.build_lane_bitmap_b64(session)
        raw_lane = base64.b64decode(b64_lane)
        self.assertEqual(len(raw_lane), ms.LANE_WIDTH * ms.LANE_HEIGHT * 3)

    def test_build_meeting_payload_safety_budget(self) -> None:
        session = ms.ActiveMicSession(
            app_name="Zoom",
            exe_path="C:\\Zoom\\bin\\Zoom.exe",
            started_at=100.0,
            elapsed_seconds=3665.0,
        )
        payload = ms.build_meeting_payload(session, animated=True)
        self.assertEqual(payload["iconMode"], "fixed")
        self.assertEqual(payload["backgroundColor"], "#000000")
        self.assertEqual(payload["lifetimeExpiry"], "remove")

        serialized = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.assertLess(len(serialized), ms.PAYLOAD_MAX_BYTES)

    def test_controller_pushes_when_active_and_deletes_when_inactive(self) -> None:
        panel = MagicMock()
        mock_session = ms.ActiveMicSession(
            app_name="Teams",
            exe_path="C:\\Teams\\Teams.exe",
            started_at=1000.0,
            elapsed_seconds=30.0,
        )

        detector = MagicMock()
        detector.return_value = mock_session

        clock = MagicMock()
        clock.monotonic.return_value = 100.0

        controller = ms.MeetingSentinelController(
            panel=panel,
            policy=ms.MeetingSentinelPolicy(refresh_seconds=5.0),
            detector=detector,
            clock=clock,
        )

        # First tick: active mic -> pushes and activates
        tick1 = controller.tick()
        self.assertTrue(tick1.active)
        self.assertTrue(tick1.pushed)
        self.assertFalse(tick1.deleted)
        panel.push_app.assert_called_once()
        panel.activate_app.assert_called_once_with(ms.APP_NAME)

        panel.push_app.reset_mock()
        panel.activate_app.reset_mock()

        # Second tick before refresh_seconds: no redundant push
        clock.monotonic.return_value = 102.0
        tick2 = controller.tick()
        self.assertTrue(tick2.active)
        self.assertFalse(tick2.pushed)
        panel.push_app.assert_not_called()

        # Third tick after refresh_seconds: refreshes time
        clock.monotonic.return_value = 106.0
        tick3 = controller.tick()
        self.assertTrue(tick3.active)
        self.assertTrue(tick3.pushed)
        panel.push_app.assert_called_once()

        panel.push_app.reset_mock()

        # Mic turns off: next tick clears the alert
        detector.return_value = None
        clock.monotonic.return_value = 108.0
        tick4 = controller.tick()
        self.assertFalse(tick4.active)
        self.assertFalse(tick4.pushed)
        self.assertTrue(tick4.deleted)
        panel.delete_pushed_app.assert_called_once_with(ms.APP_NAME)

        panel.delete_pushed_app.reset_mock()

        # Subsequent tick while inactive: idle, does nothing
        tick5 = controller.tick()
        self.assertFalse(tick5.active)
        self.assertFalse(tick5.deleted)
        panel.delete_pushed_app.assert_not_called()


if __name__ == "__main__":
    unittest.main()
