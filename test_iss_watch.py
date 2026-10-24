from __future__ import annotations

from datetime import datetime
import json
import unittest

import iss_watch as iw
import visual_system as vs
from test_new_app_support import FakeClock, SpyPanel

HOME = (iw.HOME_LAT, iw.HOME_LON)
EVENING = datetime(2026, 10, 3, 19, 45)  # sun well below -6 deg
NOON = datetime(2026, 10, 3, 12, 0)
BLACKOUT = datetime(2026, 10, 3, 23, 0)


def fix(dlat: float = 0.0, dlon: float = 0.0, sunlit: bool = True, alt: float = 420.0) -> iw.StationFix:
    return iw.StationFix(HOME[0] + dlat, HOME[1] + dlon, alt, sunlit)


class ActivatingPanel(SpyPanel):
    def __init__(self):
        super().__init__()
        self.activations = []

    def activate_app(self, name):
        self.activations.append(name)


class GeometryTests(unittest.TestCase):
    def test_straight_overhead_is_ninety_degrees(self) -> None:
        self.assertAlmostEqual(iw.look_angle(fix()).elevation_deg, 90.0, places=3)

    def test_compass_direction_follows_the_station(self) -> None:
        self.assertEqual(iw.look_label(iw.look_angle(fix(dlat=3))).split()[0], "N")
        self.assertEqual(iw.look_label(iw.look_angle(fix(dlon=4))).split()[0], "E")
        self.assertEqual(iw.look_label(iw.look_angle(fix(dlat=-3, dlon=-3))).split()[0], "SW")

    def test_elevation_falls_with_distance_and_crosses_ten_degrees_near_1400_km(self) -> None:
        angles = [iw.look_angle(fix(dlat=km / 111.2)).elevation_deg for km in (300, 800, 1400, 2000)]
        self.assertEqual(angles, sorted(angles, reverse=True))
        self.assertAlmostEqual(angles[2], iw.MIN_ELEVATION_DEG, delta=1.0)

    def test_approach_radius_leaves_two_minutes_of_warning(self) -> None:
        visible_edge_km = 1400
        km_per_minute = 7.66 * 60
        self.assertGreaterEqual((iw.APPROACH_KM - visible_edge_km) / km_per_minute, 2.0)

    def test_parse_reads_sunlight(self) -> None:
        doc = {"latitude": 1, "longitude": 2, "altitude": 420, "visibility": "eclipsed"}
        self.assertFalse(iw.parse_fix(doc).sunlit)
        self.assertTrue(iw.parse_fix({**doc, "visibility": "daylight"}).sunlit)


class VisibilityTests(unittest.TestCase):
    def test_needs_dark_sky_sunlit_station_and_height(self) -> None:
        self.assertTrue(iw.is_visible(fix(dlat=3), EVENING))
        self.assertFalse(iw.is_visible(fix(dlat=3), NOON))
        self.assertFalse(iw.is_visible(fix(dlat=3, sunlit=False), EVENING))
        self.assertFalse(iw.is_visible(fix(dlat=16), EVENING))  # below 10 deg

    def test_dusk_is_not_dark_yet(self) -> None:
        self.assertFalse(iw.sky_is_dark(datetime(2026, 10, 3, 18, 30)))
        self.assertTrue(iw.sky_is_dark(datetime(2026, 10, 3, 19, 30)))


class PayloadTests(unittest.TestCase):
    def test_every_label_fits_and_payload_is_small(self) -> None:
        for az in range(0, 360, 15):
            for el in (10, 45, 90):
                with self.subTest(az=az, el=el):
                    p = iw.build_payload(iw.LookAngle(az, el))
                    vs.validate_awtrix_payload(p)
                    self.assertLessEqual(len(json.dumps(p, separators=(",", ":"))), 2048)

    def test_gutter_stays_black_and_meter_grows_with_elevation(self) -> None:
        low, high = iw.render_lane(iw.LookAngle(0, 15)), iw.render_lane(iw.LookAngle(0, 80))
        for frame in (low, high):
            self.assertTrue(all(frame[y][vs.GUTTER_X] == vs.BLACK for y in range(8)))

        self.assertGreater(vs.meter_fill(high), vs.meter_fill(low))

    def test_icon_survives_brightness_one(self) -> None:
        # The station itself stays bright; only the faint star levels may fade.
        station = {(x, y) for y, row in enumerate(iw.STATION) for x, ch in enumerate(row) if ch != "."}
        for frame in iw.icon_frames():
            for x, y in station:
                self.assertTrue(vs.is_safe_at_low_brightness(frame[y][x]), (x, y))

    def test_icon_animation_is_on_the_clean_beat(self) -> None:
        frames = iw.icon_frames()
        self.assertGreater(len(frames), 1)
        self.assertNotEqual(frames[0], frames[-1])  # glint frame differs from rest


class ControllerTests(unittest.TestCase):
    def make(self, fixes, wall=EVENING, panel=None):
        self.clock = FakeClock()
        self.wall = [wall]
        self.panel = panel or ActivatingPanel()
        self.fetches = 0
        queue = list(fixes)

        def fetch():
            self.fetches += 1
            item = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(item, Exception):
                raise item
            return item
        return iw.IssWatchController(self.panel, clock=self.clock, wall_now=lambda: self.wall[0], fetcher=fetch)

    def test_daylight_and_blackout_fetch_nothing(self) -> None:
        for wall in (NOON, BLACKOUT):
            with self.subTest(wall=wall):
                c = self.make([fix()], wall=wall)
                for _ in range(100):
                    c.tick()
                    self.clock.advance(5)
                self.assertEqual(self.fetches, 0)
                self.assertEqual(self.panel.pushes, [])

    def test_far_station_polls_slowly_near_station_bursts(self) -> None:
        c = self.make([fix(dlat=60)])
        for _ in range(60):
            c.tick()
            self.clock.advance(5)
        far = self.fetches
        c2 = self.make([fix(dlat=18)])  # ~2,000 km: approaching but not yet up
        for _ in range(60):
            c2.tick()
            self.clock.advance(5)
        self.assertEqual(self.panel.pushes, [])
        self.assertGreater(self.fetches, far * 3)

    def test_visible_pass_pushes_follows_and_announces_once(self) -> None:
        path = [fix(dlat=-8, dlon=-8), fix(dlat=-3, dlon=-3), fix(dlat=2, dlon=2), fix(dlat=30)]
        c = self.make(path)
        labels = []
        for _ in range(4):
            angle = c.tick()
            if angle is not None:
                labels.append(iw.look_label(angle))
            self.clock.advance(c.config.burst_poll_seconds)
        self.assertEqual(len(self.panel.pushes), 3)
        self.assertEqual(self.panel.activations, ["iss0"])
        self.assertEqual(self.panel.deletes, ["iss0"])  # gone once it sets
        self.assertTrue(labels[0].startswith("SW"))
        self.assertTrue(labels[-1].startswith("NE"))

    def test_eclipsed_station_never_shows(self) -> None:
        c = self.make([fix(dlat=1, sunlit=False)])
        for _ in range(20):
            c.tick()
            self.clock.advance(10)
        self.assertEqual(self.panel.pushes, [])

    def test_feed_failure_backs_off(self) -> None:
        c = self.make([RuntimeError("503")])
        for _ in range(240):
            c.tick()
            self.clock.advance(5)
        # 20 min of failures: 60, 120, 240, 480 s... a handful of attempts, not 120.
        self.assertLessEqual(self.fetches, 6)

    def test_works_with_a_panel_that_cannot_activate(self) -> None:
        c = self.make([fix(dlat=1)], panel=SpyPanel())
        self.assertIsNotNone(c.tick())
        self.assertEqual(len(c.panel.pushes), 1)


if __name__ == "__main__":
    unittest.main()
