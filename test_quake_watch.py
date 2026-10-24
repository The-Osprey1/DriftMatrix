import unittest

import quake_watch as qw
from test_new_app_support import FakeClock, SpyPanel
import visual_system as vs

NOW_MS = 1_790_000_000_000


def quake(mag=3.2, mins_ago=5, eid="q1", lat=33.95, lon=-117.40, place="10 km NE of Corona, CA"):
    return qw.Quake(eid, mag, place, NOW_MS - mins_ago * 60_000, lat, lon, 8.0)


def meter(frame):
    """(filled LEDs, their colour) on the row-7 meter; (0, None) for a bare track."""
    return vs.read_meter(frame)


class ParseTests(unittest.TestCase):
    def test_blasts_and_magnitudeless_events_are_skipped(self):
        doc = {"features": [
            {"id": "a", "properties": {"mag": 2.7, "type": "quarry blast", "time": 1}, "geometry": {"coordinates": [0, 0, 0]}},
            {"id": "b", "properties": {"mag": None, "time": 1}, "geometry": {"coordinates": [0, 0, 0]}},
            {"id": "c", "properties": {"mag": 3.1, "place": "x", "time": 5}, "geometry": {"coordinates": [-117.0, 34.0, 7.5]}},
        ]}
        found = qw.parse_quakes(doc)
        self.assertEqual([q.event_id for q in found], ["c"])
        self.assertEqual((found[0].lat, found[0].lon, found[0].depth_km), (34.0, -117.0, 7.5))


class WindowTests(unittest.TestCase):
    def test_window_grows_with_magnitude(self):
        windows = [qw.window_minutes(m) for m in (2.5, 3.5, 4.5, 6.0)]
        self.assertEqual(windows, sorted(windows))
        self.assertEqual(len(set(windows)), 4)

    def test_biggest_live_quake_wins_and_expired_ones_do_not(self):
        small_new = quake(2.6, 2, "small")
        big_old = quake(3.5, 90, "big")
        expired = quake(2.9, 45, "gone")  # past its 30 minute window
        self.assertEqual(qw.active_quake([small_new, big_old, expired], NOW_MS).event_id, "big")
        self.assertIsNone(qw.active_quake([expired], NOW_MS))


class TextTests(unittest.TestCase):
    def test_describes_size_distance_direction_place_and_age(self):
        text = qw.describe(quake(3.24, 12), NOW_MS)
        self.assertTrue(text.startswith("M3.2  "))
        self.assertIn(" MI NE", text)
        self.assertIn("NEAR CORONA", text)
        self.assertTrue(text.endswith("12 MIN AGO"))

    def test_age_wording(self):
        self.assertEqual([qw.ago(m) for m in (0, 5, 60, 150)],
                         ["JUST NOW", "5 MIN AGO", "1 HR AGO", "2 HRS AGO"])


class LaneTests(unittest.TestCase):
    def test_distance_is_in_miles_and_always_fits(self):
        self.assertEqual(qw.distance_label(quake(lat=33.87529, lon=-117.56644)), "0 MI")
        near = qw.distance_label(quake())
        self.assertTrue(near.endswith(" MI"))
        far = qw.distance_label(quake(lat=35.5, lon=-117.56644))  # ~112 mi north
        self.assertLessEqual(qw.measure_text(far), qw.LANE_WIDTH)

    def test_meter_fills_as_the_quake_gets_closer(self):
        home = meter(qw.render_lane(quake(lat=33.87529, lon=-117.56644)))
        near = meter(qw.render_lane(quake()))
        edge = meter(qw.render_lane(quake(lat=35.2, lon=-117.56644)))  # ~148 km
        self.assertEqual(home[0], vs.LANE_WIDTH)
        self.assertGreater(near[0], edge[0])
        self.assertEqual(edge[0], 1)

    def test_meter_wears_the_magnitude_colour_of_the_icon(self):
        for mag in (2.6, 3.4, 4.6, 5.8):
            with self.subTest(mag=mag):
                self.assertEqual(meter(qw.render_lane(quake(mag=mag)))[1], qw.magnitude_color(mag))

    def test_meter_scale_follows_the_configured_radius(self):
        q = quake(lat=34.5, lon=-117.56644)  # ~70 km
        self.assertGreater(meter(qw.render_lane(q, radius_km=300))[0],
                           meter(qw.render_lane(q, radius_km=150))[0])


class IconTests(unittest.TestCase):
    def test_rings_stay_in_the_icon_box_with_the_epicentre_always_lit(self):
        for mag in (2.5, 3.8, 5.2, 7.1):
            frames = qw.icon_frames(mag)
            self.assertEqual(len(frames), qw.RING_FRAMES)
            self.assertTrue(all(a != b for a, b in zip(frames, frames[1:] + frames[:1])))  # every beat moves
            for frame in frames:
                lit = [(x, y) for y in range(8) for x in range(32) if frame[y][x] != (0, 0, 0)]
                self.assertLess(max(x for x, _ in lit), 8)
                self.assertTrue(all(frame[y][x] != (0, 0, 0) for x, y in qw.EPICENTRE))

    def test_bigger_quakes_send_more_rings(self):
        self.assertEqual([qw.ring_count(m) for m in (2.6, 3.8, 5.2)], [1, 2, 2])

        def lit(mag):
            return sum(1 for f in qw.icon_frames(mag) for row in f[:8] for px in row[:8] if px != (0, 0, 0))
        self.assertGreater(lit(4.2), lit(2.6))

    def test_payload_fits_budget(self):
        for mag in (2.5, 3.5, 4.5, 6.0):
            qw.build_payload(quake(mag))


class ControllerTests(unittest.TestCase):
    def test_appears_for_a_quake_and_leaves_when_the_window_closes(self):
        panel, clock = SpyPanel(), FakeClock(NOW_MS / 1000)
        c = qw.QuakeWatchController(panel, fetcher=lambda cfg: [quake(2.6, 1)], clock=clock)
        self.assertIsNotNone(c.tick())
        self.assertIsNone(c.tick())     # inside the poll interval: no request
        clock.advance(40 * 60)
        self.assertIsNone(c.tick())
        self.assertEqual(len(panel.pushes), 1)
        self.assertEqual(panel.deletes, ["quake0"])


if __name__ == "__main__":
    unittest.main()
