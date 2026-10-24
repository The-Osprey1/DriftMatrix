import unittest

import requests
import sky_watch as sw
from test_new_app_support import FakeClock, SpyPanel
import visual_system as vs


def plane(dist=1.0, alt=4000, cs="SWA1234", t="B738", cat="A3", track=90.0, hex_id="a1"):
    return sw.Aircraft(hex_id, cs, t, cat, alt, track, dist, 0.0)


def meter(frame):
    """(filled LEDs, their colour) on the row-7 meter; (0, None) for a bare track."""
    return vs.read_meter(frame)


class ParseTests(unittest.TestCase):
    def test_parse_aircraft_readsb_payload(self):
        doc = {
            "ac": [
                {
                    "hex": "a1234b",
                    "flight": "UAL456 ",
                    "r": "N123UA",
                    "t": "B772",
                    "category": "A5",
                    "alt_baro": 8500,
                    "alt_geom": 8600,
                    "track": 245.5,
                    "dst": 2.1,
                    "dir": 180.2,
                    "lat": 33.85,
                    "lon": -117.56,
                    "gs": 280.0,
                },
                {
                    "hex": "ground01",
                    "alt_baro": "ground",
                    "dst": 0.5,
                },
            ],
            "total": 1,
            "ctime": 1700000000.0,
        }
        found = sw.parse_aircraft(doc)
        self.assertEqual(len(found), 1)
        ac = found[0]
        self.assertEqual(ac.hex_id, "a1234b")
        self.assertEqual(ac.callsign, "UAL456")
        self.assertEqual(ac.type_code, "B772")
        self.assertEqual(ac.category, "A5")
        self.assertEqual(ac.altitude_ft, 8500)
        self.assertEqual(ac.track_deg, 245.5)
        self.assertEqual(ac.distance_nm, 2.1)
        self.assertEqual(ac.bearing_deg, 180.2)

    def test_ground_and_positionless_traffic_is_dropped(self):
        doc = {"ac": [
            {"hex": "a", "flight": "SWA1 ", "alt_baro": "ground", "dst": 1.0},
            {"hex": "b", "flight": "SWA2 ", "alt_baro": 3000},
            {"hex": "c", "flight": "SWA3 ", "alt_baro": 3000, "dst": 1.2, "t": "B738", "track": 90},
        ]}
        found = sw.parse_aircraft(doc)
        self.assertEqual([a.hex_id for a in found], ["c"])
        self.assertEqual(found[0].callsign, "SWA3")

    def test_nearest_inside_the_circle_and_ceiling_wins(self):
        near_high = plane(dist=0.2, alt=20_000, hex_id="high")
        far = plane(dist=3.0, hex_id="far")
        near = plane(dist=1.5, hex_id="near")
        nearer = plane(dist=0.9, hex_id="nearer")
        pick = sw.pick_overhead([near_high, far, near, nearer], 2.5, 12_000)
        self.assertEqual(pick.hex_id, "nearer")
        self.assertIsNone(sw.pick_overhead([far, near_high], 2.5, 12_000))


class LabelTests(unittest.TestCase):
    def test_families_collapse_to_the_name_people_say(self):
        cases = {"B738": "737", "B38M": "737", "A20N": "A320", "BCS3": "A220", "B789": "787",
                 "E75L": "E175", "C172": "CESSNA", "P28A": "PIPER", "SR22": "CIRRUS", "GLF6": "JET"}
        for code, label in cases.items():
            with self.subTest(code=code):
                self.assertEqual(sw.model_label(plane(t=code)), label)

    def test_helicopters_and_unknown_small_planes(self):
        self.assertEqual(sw.model_label(plane(t="R44", cat="A7")), "HELI")
        self.assertEqual(sw.model_label(plane(t="", cat="A1")), "SMALL")
        self.assertEqual(sw.model_label(plane(t="", cat="A3")), "PLANE")

    def test_every_label_fits_the_lane(self):
        for _, label in sw.MODEL_PREFIXES + (("", "HELI"), ("", "SMALL"), ("", "PLANE")):
            with self.subTest(label=label):
                self.assertLessEqual(sw.measure_text(label), sw.LANE_WIDTH)

    def test_meter_is_altitude_against_the_ceiling(self):
        low = meter(sw.render_lane(plane(alt=1200)))
        high = meter(sw.render_lane(plane(alt=11_500)))
        self.assertLess(low[0], high[0])
        self.assertEqual(high[1], vs.LEVEL_COLORS["info"])
        self.assertEqual(meter(sw.render_lane(plane(alt=6000), max_altitude_ft=6000))[0],
                         vs.LANE_WIDTH)


class IconTests(unittest.TestCase):
    def test_every_heading_has_a_distinct_silhouette_for_every_kind(self):
        for kind in sw.SILHOUETTES:
            with self.subTest(kind=kind):
                patterns = {sw.heading_pattern(deg, kind) for deg in range(0, 360, 45)}
                self.assertEqual(len(patterns), 8)

    def test_kinds_look_different(self):
        for deg in (0, 45):
            shapes = {sw.heading_pattern(deg, kind) for kind in sw.SILHOUETTES}
            self.assertEqual(len(shapes), len(sw.SILHOUETTES))

    def test_rotation_keeps_the_nose_leading(self):
        for kind in sw.SILHOUETTES:
            # Heading north puts the nose on row 0; heading south on row 6.
            self.assertIn("B", sw.heading_pattern(0, kind)[0])
            self.assertIn("B", sw.heading_pattern(180, kind)[6])
            # Heading NE puts the nose in the top-right corner of the box
            # (the light plane's short nose sits one pixel in from it).
            ne = sw.heading_pattern(45, kind)
            self.assertTrue(ne[0][6] == "B" or ne[1][5] == "B")

    def test_diagonals_mirror_across_the_fuselage(self):
        # (x, y) <-> (6-y, 6-x); the beacon may sit on either side.
        for kind, (_, ne) in sw.SILHOUETTES.items():
            with self.subTest(kind=kind):
                for y in range(7):
                    for x in range(7):
                        a, b = ne[y][x], ne[6 - x][6 - y]
                        self.assertTrue(a == b or "L" in (a, b), (kind, x, y))

    def test_red_is_always_on_the_left_of_the_direction_of_travel(self):
        # Screen y grows downward; "left" of a heading (hx, hy) is (hy, -hx).
        for kind in ("airliner", "jet", "light"):
            for deg in range(0, 360, 45):
                with self.subTest(kind=kind, deg=deg):
                    pattern = sw.heading_pattern(deg, kind)
                    port, starboard = sw.wingtips(pattern, deg)
                    hx, hy = sw.OCTANT_VECTORS[sw._octant(deg)]
                    side = lambda p: (p[0] - 3) * hy + (p[1] - 3) * -hx
                    self.assertGreater(side(port), side(starboard))

    def test_lights_and_strobe_animate_on_the_beat(self):
        a = plane(t="B738", track=270.0)
        frames = sw.icon_frames(a)
        self.assertEqual(len(frames), sw.ICON_BEATS)
        lit = lambda f, colour: any(px == colour for row in f for px in row)
        self.assertTrue(all(lit(f, sw.NAV_PORT) and lit(f, sw.NAV_STARBOARD) for f in frames))
        self.assertEqual([lit(f, sw.STROBE) for f in frames],
                         [beat in sw.STROBE_BEATS for beat in range(sw.ICON_BEATS)])

    def test_every_silhouette_stays_in_its_box_with_one_beacon(self):
        for kind, pair in sw.SILHOUETTES.items():
            for pattern in pair:
                with self.subTest(kind=kind):
                    self.assertEqual(len(pattern), 7)
                    self.assertTrue(all(len(row) == 7 for row in pattern))
                    self.assertEqual("".join(pattern).count("L"), 1)

    def test_aircraft_are_sorted_into_kinds(self):
        self.assertEqual(sw.plane_class(plane(t="B738")), "airliner")
        self.assertEqual(sw.plane_class(plane(t="C172", cat="A1")), "light")
        self.assertEqual(sw.plane_class(plane(t="SR22", cat="A1")), "light")
        self.assertEqual(sw.plane_class(plane(t="GLF6", cat="A2")), "jet")
        self.assertEqual(sw.plane_class(plane(t="R44", cat="A7")), "heli")

    def test_payload_fits_budget_for_every_kind(self):
        for cat in ("A1", "A3", "A7"):
            sw.build_payload(plane(cat=cat))  # raises if over budget


class ControllerTests(unittest.TestCase):
    def test_shows_while_overhead_and_removes_when_gone(self):
        panel, clock, sky = SpyPanel(), FakeClock(), [[plane()]]
        c = sw.SkyWatchController(panel, fetcher=lambda cfg: sky[0], clock=clock)
        self.assertIsNotNone(c.tick())
        sky[0] = []
        clock.advance(20); c.tick()
        clock.advance(20); c.tick()
        self.assertEqual(len(panel.pushes), 1)
        self.assertEqual(panel.deletes, ["planes0"])

    def test_small_planes_are_ignored_unless_asked_for(self):
        cessna = plane(t="C172", cat="A1", dist=0.5, hex_id="cessna")
        airliner = plane(t="B738", cat="A3", dist=2.0, hex_id="jet")
        c = sw.SkyWatchController(SpyPanel(), fetcher=lambda cfg: [cessna, airliner], clock=FakeClock())
        self.assertEqual(c.tick().hex_id, "jet")
        c = sw.SkyWatchController(SpyPanel(), sw.SkyWatchConfig(include_light_aircraft=True),
                                  fetcher=lambda cfg: [cessna, airliner], clock=FakeClock())
        self.assertEqual(c.tick().hex_id, "cessna")

    def test_polls_the_feed_once_per_interval(self):
        calls, clock = [], FakeClock()
        c = sw.SkyWatchController(SpyPanel(), fetcher=lambda cfg: calls.append(1) or [], clock=clock)
        for _ in range(5):
            c.tick(); clock.advance(3)   # hub ticks every 3 s
        self.assertEqual(len(calls), 1)

    def test_feed_failure_leaves_the_page_to_its_lifetime(self):
        panel = SpyPanel()

        def boom(cfg):
            raise OSError("down")

        self.assertIsNone(sw.SkyWatchController(panel, fetcher=boom, clock=FakeClock()).tick())
        self.assertEqual((panel.pushes, panel.deletes), ([], []))

    def test_feed_errors_back_off_and_recover(self):
        panel, clock, calls = SpyPanel(), FakeClock(), []

        def flaky(cfg):
            calls.append(clock.t)
            if len(calls) <= 2:
                raise OSError("429")
            return [plane()]

        c = sw.SkyWatchController(panel, fetcher=flaky, clock=clock)
        c.tick()                        # fails: wait 20 s
        clock.advance(10); c.tick()     # still backing off, no request
        clock.advance(10); c.tick()     # fails again: wait 40 s
        clock.advance(20); c.tick()     # still backing off
        clock.advance(20)
        self.assertIsNotNone(c.tick())  # recovers
        self.assertEqual(len(calls), 3)
        self.assertEqual(c.backoff_seconds, 0.0)

    def test_failover_to_secondary_on_http_429(self):
        panel, clock = SpyPanel(), FakeClock()

        def primary_fail(cfg):
            raise requests.HTTPError("429 Client Error: Too Many Requests")

        secondary_plane = plane(hex_id="secondary1")
        c = sw.SkyWatchController(
            panel,
            primary_fetcher=primary_fail,
            secondary_fetcher=lambda cfg: [secondary_plane],
            clock=clock,
        )
        result = c.tick()
        self.assertIsNotNone(result)
        self.assertEqual(result.hex_id, "secondary1")
        self.assertEqual(c.active_source, "secondary")
        self.assertEqual(c.backoff_seconds, 0.0)
        self.assertEqual(c.primary_backoff_seconds, 60.0)
        self.assertEqual(len(panel.pushes), 1)

    def test_failover_on_network_timeout(self):
        panel, clock = SpyPanel(), FakeClock()

        def primary_timeout(cfg):
            raise requests.Timeout("Connection timed out after 8s")

        secondary_plane = plane(hex_id="timeout_fallback")
        c = sw.SkyWatchController(
            panel,
            primary_fetcher=primary_timeout,
            secondary_fetcher=lambda cfg: [secondary_plane],
            clock=clock,
        )
        result = c.tick()
        self.assertIsNotNone(result)
        self.assertEqual(result.hex_id, "timeout_fallback")
        self.assertEqual(c.active_source, "secondary")
        self.assertEqual(c.backoff_seconds, 0.0)
        self.assertEqual(c.primary_backoff_seconds, 60.0)

    def test_primary_cooldown_prevents_hammering(self):
        panel, clock = SpyPanel(), FakeClock()
        primary_calls = []
        secondary_calls = []

        def mock_primary(cfg):
            primary_calls.append(clock.t)
            raise OSError("429 Too Many Requests")

        def mock_secondary(cfg):
            secondary_calls.append(clock.t)
            return [plane(hex_id="sec")]

        c = sw.SkyWatchController(
            panel,
            primary_fetcher=mock_primary,
            secondary_fetcher=mock_secondary,
            clock=clock,
        )
        # Tick 1 at t=0: primary fails, falls back to secondary
        c.tick()
        self.assertEqual(len(primary_calls), 1)
        self.assertEqual(len(secondary_calls), 1)
        self.assertEqual(c.primary_backoff_seconds, 60.0)

        # Tick 2 at t=20: within 60s cooldown, primary must NOT be called
        clock.advance(20)
        c.tick()
        self.assertEqual(len(primary_calls), 1)
        self.assertEqual(len(secondary_calls), 2)

        # Tick 3 at t=40: still within cooldown, primary must NOT be called
        clock.advance(20)
        c.tick()
        self.assertEqual(len(primary_calls), 1)
        self.assertEqual(len(secondary_calls), 3)

    def test_primary_recovery_after_cooldown(self):
        panel, clock = SpyPanel(), FakeClock()
        primary_calls = []
        primary_healthy = False

        def mock_primary(cfg):
            primary_calls.append(clock.t)
            if not primary_healthy:
                raise OSError("Primary down")
            return [plane(hex_id="prim_restored")]

        def mock_secondary(cfg):
            return [plane(hex_id="sec_during_cooldown")]

        c = sw.SkyWatchController(
            panel,
            primary_fetcher=mock_primary,
            secondary_fetcher=mock_secondary,
            clock=clock,
        )
        # t=0: primary fails, switches to secondary
        res1 = c.tick()
        self.assertEqual(res1.hex_id, "sec_during_cooldown")
        self.assertEqual(c.active_source, "secondary")

        # t=20: during cooldown
        clock.advance(20)
        res2 = c.tick()
        self.assertEqual(res2.hex_id, "sec_during_cooldown")

        # t=60: cooldown (60s) has expired, probe primary
        clock.advance(40)
        primary_healthy = True
        res3 = c.tick()
        self.assertEqual(res3.hex_id, "prim_restored")
        self.assertEqual(c.active_source, "primary")
        self.assertEqual(c.primary_backoff_seconds, 0.0)
        self.assertEqual(len(primary_calls), 2)  # probed at t=0 and t=60

    def test_all_feeds_fail_enters_controller_backoff(self):
        panel, clock = SpyPanel(), FakeClock()

        def fail_primary(cfg):
            raise OSError("Primary down")

        def fail_secondary(cfg):
            raise OSError("Secondary down")

        c = sw.SkyWatchController(
            panel,
            primary_fetcher=fail_primary,
            secondary_fetcher=fail_secondary,
            clock=clock,
        )
        res = c.tick()
        self.assertIsNone(res)
        self.assertEqual(c.backoff_seconds, 20.0)
        self.assertEqual(c.resume_at, clock.t + 20.0)

        # Still backing off at t=10
        clock.advance(10)
        self.assertIsNone(c.tick())

        # At t=20, retries and fails again -> backoff doubles to 40.0
        clock.advance(10)
        self.assertIsNone(c.tick())
        self.assertEqual(c.backoff_seconds, 40.0)
        self.assertEqual(c.resume_at, clock.t + 40.0)

    def test_adaptive_backoff_relaxed_when_clear(self):
        panel, clock = SpyPanel(), FakeClock()
        c = sw.SkyWatchController(
            panel,
            fetcher=lambda cfg: [],
            clock=clock,
        )
        res = c.tick()
        self.assertIsNone(res)
        self.assertEqual(c.resume_at, clock.t + 60.0)

    def test_adaptive_backoff_burst_when_plane_overhead(self):
        panel, clock = SpyPanel(), FakeClock()
        overhead_plane = plane(dist=1.5, alt=5000, hex_id="burst1")
        c = sw.SkyWatchController(
            panel,
            fetcher=lambda cfg: [overhead_plane],
            clock=clock,
        )
        res = c.tick()
        self.assertIsNotNone(res)
        self.assertEqual(res.hex_id, "burst1")
        self.assertEqual(c.resume_at, clock.t + 20.0)

    def test_fetch_feed_uses_primary_and_secondary_urls(self):
        cfg = sw.SkyWatchConfig(lat=33.87529, lon=-117.56644, radius_nm=2.5)
        calls = []

        class MockResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"ac": []}

        def mock_get(url, timeout):
            calls.append((url, timeout))
            return MockResponse()

        # Primary default
        sw.fetch_feed(cfg, http_get=mock_get)
        self.assertIn("api.adsb.lol/v2/lat/33.87529/lon/-117.56644/dist/4", calls[0][0])
        self.assertEqual(calls[0][1], sw.FEED_TIMEOUT_SECONDS)

        # Secondary URL template
        sw.fetch_feed(cfg, url_template=cfg.secondary_feed_url, http_get=mock_get)
        self.assertIn("opendata.adsb.fi/api/v3/lat/33.87529/lon/-117.56644/dist/4", calls[1][0])
        self.assertEqual(calls[1][1], sw.FEED_TIMEOUT_SECONDS)

    def test_primary_cooldown_doubling_to_max(self):
        panel, clock = SpyPanel(), FakeClock()

        def fail_primary(cfg):
            raise OSError("fail")

        c = sw.SkyWatchController(
            panel,
            primary_fetcher=fail_primary,
            secondary_fetcher=lambda cfg: [plane()],
            clock=clock,
        )
        # 1st failure at t=0: cooldown = 60s
        c.tick()
        self.assertEqual(c.primary_backoff_seconds, 60.0)
        self.assertEqual(c.primary_resume_at, clock.t + 60.0)

        # In cooldown at t=20: no primary probe
        clock.advance(20)
        c.tick()
        self.assertEqual(c.primary_backoff_seconds, 60.0)

        # At t=60: probe fails -> doubles to 120s
        clock.advance(40)
        c.tick()
        self.assertEqual(c.primary_backoff_seconds, 120.0)
        self.assertEqual(c.primary_resume_at, clock.t + 120.0)

        # At t=180: probe fails -> doubles to 240s
        clock.advance(120)
        c.tick()
        self.assertEqual(c.primary_backoff_seconds, 240.0)
        self.assertEqual(c.primary_resume_at, clock.t + 240.0)

        # At t=420: probe fails -> doubles to 300s (capped at max_backoff_seconds)
        clock.advance(240)
        c.tick()
        self.assertEqual(c.primary_backoff_seconds, 300.0)
        self.assertEqual(c.primary_resume_at, clock.t + 300.0)

    def test_zero_log_spam_during_clear_airspace(self):
        import logging
        panel, clock = SpyPanel(), FakeClock()
        logger = logging.getLogger("test_sky_watch_quiet")
        records = []

        class TestHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        logger.addHandler(TestHandler())
        logger.setLevel(logging.DEBUG)

        c = sw.SkyWatchController(panel, fetcher=lambda cfg: [], clock=clock, logger=logger)
        for _ in range(10):
            c.tick()
            clock.advance(60)

        self.assertEqual(len(records), 0)

    def test_feed_transition_logs_info_once(self):
        import logging
        panel, clock = SpyPanel(), FakeClock()
        logger = logging.getLogger("test_sky_watch_transition")
        records = []

        class TestHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        logger.addHandler(TestHandler())
        logger.setLevel(logging.INFO)

        primary_healthy = False

        def mock_primary(cfg):
            if not primary_healthy:
                raise OSError("Primary 429")
            return [plane()]

        c = sw.SkyWatchController(
            panel,
            primary_fetcher=mock_primary,
            secondary_fetcher=lambda cfg: [plane()],
            clock=clock,
            logger=logger,
        )
        # Initial failover: should log switch to secondary once
        c.tick()
        info_msgs = [r.getMessage() for r in records if r.levelno == logging.INFO]
        switch_msgs = [m for m in info_msgs if "switching to secondary feed" in m]
        self.assertEqual(len(switch_msgs), 1)

        # Next tick in cooldown: should NOT log another transition message
        clock.advance(20)
        c.tick()
        switch_msgs = [m for m in [r.getMessage() for r in records if r.levelno == logging.INFO] if "switching to secondary feed" in m]
        self.assertEqual(len(switch_msgs), 1)

        # Primary recovery at t=60: should log primary restored once
        clock.advance(40)
        primary_healthy = True
        c.tick()
        restore_msgs = [r.getMessage() for r in records if r.levelno == logging.INFO and "resuming primary" in r.getMessage()]
        self.assertEqual(len(restore_msgs), 1)

    def test_backward_compatible_poll_seconds(self):
        cfg1 = sw.SkyWatchConfig(poll_seconds=15.0)
        self.assertEqual(cfg1.poll_seconds, 15.0)
        self.assertEqual(cfg1.burst_poll_seconds, 15.0)

        cfg2 = sw.SkyWatchConfig(burst_poll_seconds=12.0)
        self.assertEqual(cfg2.poll_seconds, 12.0)
        self.assertEqual(cfg2.burst_poll_seconds, 12.0)

        cfg_default = sw.SkyWatchConfig()
        self.assertEqual(cfg_default.poll_seconds, 20.0)
        self.assertEqual(cfg_default.burst_poll_seconds, 20.0)
        self.assertEqual(cfg_default.relaxed_poll_seconds, 60.0)
        self.assertEqual(cfg_default.primary_backoff_seconds, 60.0)
        self.assertEqual(cfg_default.primary_feed_url, sw.PRIMARY_FEED_URL)
        self.assertEqual(cfg_default.secondary_feed_url, sw.SECONDARY_FEED_URL)


if __name__ == "__main__":
    unittest.main()
