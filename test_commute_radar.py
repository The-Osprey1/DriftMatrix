"""Deterministic unit tests for Commute & Drive Time Radar."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from io import BytesIO
import json
import unittest
from unittest.mock import MagicMock

from PIL import Image

import commute_radar as cr


class CommuteRadarTests(unittest.TestCase):
    def test_commute_window_detection(self) -> None:
        window = cr.CommuteWindow(
            start_hour=7,
            start_minute=30,
            end_hour=9,
            end_minute=0,
            destination="WORK",
            days=(0, 1, 2, 3, 4),  # Mon-Fri
        )

        # Tuesday at 08:15 -> inside window
        tuesday_815 = datetime(2026, 9, 8, 8, 15, tzinfo=timezone.utc)
        self.assertEqual(tuesday_815.weekday(), 1)  # Tuesday
        self.assertTrue(cr.is_in_commute_window(window, tuesday_815))

        # Tuesday at 07:15 -> before window
        tuesday_715 = datetime(2026, 9, 8, 7, 15, tzinfo=timezone.utc)
        self.assertFalse(cr.is_in_commute_window(window, tuesday_715))

        # Tuesday at 09:30 -> after window
        tuesday_930 = datetime(2026, 9, 8, 9, 30, tzinfo=timezone.utc)
        self.assertFalse(cr.is_in_commute_window(window, tuesday_930))

        # Sunday at 08:15 -> weekend outside window
        sunday_815 = datetime(2026, 9, 13, 8, 15, tzinfo=timezone.utc)
        self.assertEqual(sunday_815.weekday(), 6)  # Sunday
        self.assertFalse(cr.is_in_commute_window(window, sunday_815))

    def test_car_icons_are_valid_gifs_on_120ms_beat(self) -> None:
        for traffic in cr.TrafficCondition:
            for animated in (True, False):
                with self.subTest(traffic=traffic, animated=animated):
                    data = cr.build_commute_icon_gif(traffic, animated=animated)
                    self.assertLessEqual(len(data), cr.MAX_ICON_BYTES)
                    with Image.open(BytesIO(data)) as img:
                        self.assertEqual(img.size, (8, 8))
                        self.assertNotIn("transparency", img.info)
                        for idx in range(img.n_frames):
                            img.seek(idx)
                            dur = int(img.info.get("duration", 0))
                            if dur:
                                self.assertEqual(dur % cr.FRAME_BEAT_MS, 0)

    def test_lane_rendering_keeps_safety_gutter_and_dimensions(self) -> None:
        for traffic in cr.TrafficCondition:
            estimate = cr.CommuteEstimate(
                destination="WORK",
                duration_minutes=28,
                typical_minutes=25,
                traffic=traffic,
            )
            frame = cr.render_commute_lane(estimate)
            for y in range(cr.HEIGHT):
                self.assertEqual(frame[y][cr.GUTTER_X], cr.BLACK)

    def test_payload_safety_budget(self) -> None:
        estimate = cr.CommuteEstimate(
            destination="OFFICE",
            duration_minutes=42,
            typical_minutes=25,
            traffic=cr.TrafficCondition.HEAVY,
        )
        payload = cr.build_commute_payload(estimate, animated=True)
        self.assertEqual(payload["iconMode"], "fixed")
        self.assertEqual(payload["backgroundColor"], "#000000")
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.assertLess(len(raw), cr.MAX_PAYLOAD_BYTES)

    def test_controller_pushes_in_window_and_deletes_outside(self) -> None:
        panel = MagicMock()
        clock = MagicMock()
        clock.monotonic.return_value = 100.0

        # Create a window that always matches by setting 0..24
        open_window = cr.CommuteWindow(0, 0, 23, 59, "WORK", days=tuple(range(7)))
        config = cr.CommuteConfig(enabled=True, windows=(open_window,), refresh_seconds=30.0)

        controller = cr.CommuteRadarController(
            panel=panel,
            config=config,
            clock=clock,
        )

        # Tick inside window -> pushes
        t1 = controller.tick()
        self.assertTrue(t1.active)
        self.assertTrue(t1.pushed)
        panel.push_app.assert_called_once()

        panel.push_app.reset_mock()

        # Tick again before refresh interval -> no push
        clock.monotonic.return_value = 110.0
        t2 = controller.tick()
        self.assertTrue(t2.active)
        self.assertFalse(t2.pushed)
        panel.push_app.assert_not_called()

        # When windows are closed -> deletes
        closed_config = cr.CommuteConfig(enabled=True, windows=(), refresh_seconds=30.0)
        controller.config = closed_config
        t3 = controller.tick()
        self.assertFalse(t3.active)
        self.assertTrue(t3.deleted)
        panel.delete_pushed_app.assert_called_once_with(cr.APP_NAME)


if __name__ == "__main__":
    unittest.main()
