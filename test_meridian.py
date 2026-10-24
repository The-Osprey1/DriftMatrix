"""Deterministic checks for the AWTRIX Meridian day ribbon.

The solar model is validated against physics rather than against another
implementation: peak altitude at a solstice is 90 - latitude +/- the obliquity
of the ecliptic, and that is true no matter whose algorithm computes it.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import date, datetime
import json
import unittest
from unittest.mock import Mock, patch

import meridian_preview as art
import meridian_push as app

OBLIQUITY = 23.44


def response(status_code: int = 200, text: str = "") -> Mock:
    return Mock(status_code=status_code, text=text)


def device(power: bool = True) -> Mock:
    """A /device reply, so the power gate never depends on the real panel."""
    reply = Mock(status_code=200)
    reply.json.return_value = {"matrixPower": power}
    return reply


@contextmanager
def lit_panel():
    """Pin the panel to 'switched on' for tests about pushing, not gating."""
    with patch.object(app.requests, "get", return_value=device(True)):
        yield


@contextmanager
def dark_panel():
    """Pin the panel to 'switched off'.

    Required, not optional: sync_panel asks the panel whether it is lit, so a
    test that leaves requests.get unpatched reads the real device over the LAN
    and passes or fails depending on the time of day. Both quiet-hours tests
    did exactly that and passed only while the panel happened to be dark.
    """
    with patch.object(app.requests, "get", return_value=device(False)):
        yield


class SolarModelTests(unittest.TestCase):
    def test_solstice_peak_altitude_matches_physics(self) -> None:
        summer = art.solar_day(date(2026, 6, 21)).peak_altitude
        winter = art.solar_day(date(2026, 12, 21)).peak_altitude
        self.assertAlmostEqual(summer, 90 - art.LATITUDE + OBLIQUITY, delta=0.15)
        self.assertAlmostEqual(winter, 90 - art.LATITUDE - OBLIQUITY, delta=0.15)

    def test_daylight_is_longest_at_the_summer_solstice(self) -> None:
        lengths = {
            month: art.solar_day(date(2026, month, 21)).daylight_minutes
            for month in range(1, 13)
        }
        self.assertEqual(max(lengths, key=lengths.__getitem__), 6)
        self.assertEqual(min(lengths, key=lengths.__getitem__), 12)

    def test_sunrise_precedes_noon_precedes_sunset_all_year(self) -> None:
        for day_number in range(0, 365, 7):
            day = date.fromordinal(date(2026, 1, 1).toordinal() + day_number)
            info = art.solar_day(day)
            with self.subTest(day=day):
                self.assertIsNotNone(info.sunrise_minute)
                self.assertIsNotNone(info.sunset_minute)
                self.assertLess(info.sunrise_minute, info.noon_minute)
                self.assertLess(info.noon_minute, info.sunset_minute)

    def test_altitude_is_zero_at_the_computed_sunrise_and_sunset(self) -> None:
        # The crossing solver and the altitude function must agree, or the arc
        # will not touch the horizon where the pips say it should.
        for day in (date(2026, 3, 20), date(2026, 6, 21), date(2026, 11, 3)):
            info = art.solar_day(day)
            for minute in (info.sunrise_minute, info.sunset_minute):
                with self.subTest(day=day, minute=minute):
                    altitude = art.solar_altitude(day, minute)
                    self.assertAlmostEqual(altitude, art.SUNRISE_ALTITUDE, delta=0.05)

    def test_dst_shift_moves_local_sun_times_by_an_hour(self) -> None:
        # US DST began 2026-03-08. Civil sunrise jumps roughly an hour later on
        # the clock even though the sun did not move.
        before = art.solar_day(date(2026, 3, 7)).sunrise_minute
        after = art.solar_day(date(2026, 3, 8)).sunrise_minute
        self.assertAlmostEqual(after - before, 60, delta=3)

    def test_noon_altitude_is_the_daily_maximum(self) -> None:
        day = date(2026, 8, 22)
        info = art.solar_day(day)
        peak = max(art.solar_altitude(day, m) for m in range(0, 1440, 5))
        self.assertAlmostEqual(info.peak_altitude, peak, delta=0.05)


class RibbonGeometryTests(unittest.TestCase):
    def test_the_ribbon_covers_exactly_one_day(self) -> None:
        self.assertEqual(art.WIDTH * art.MINUTES_PER_COLUMN, art.MINUTES_PER_DAY)

    def test_every_minute_maps_into_the_panel(self) -> None:
        for minute in range(0, art.MINUTES_PER_DAY, 7):
            column = art.minute_column(minute)
            self.assertGreaterEqual(column, 0)
            self.assertLess(column, art.WIDTH)

    def test_the_sun_is_absent_below_the_horizon(self) -> None:
        self.assertIsNone(art.sun_height(-0.1))
        self.assertIsNone(art.sun_height(-30.0))
        self.assertIsNotNone(art.sun_height(0.5))

    def test_a_higher_sun_sits_on_a_higher_row(self) -> None:
        # Row 0 is the top of the panel, so height must fall as altitude rises.
        heights = [art.sun_height(a) for a in (5, 20, 45, 79)]
        self.assertEqual(heights, sorted(heights, reverse=True))

    def test_altitude_scales_against_a_fixed_ceiling_so_seasons_differ(self) -> None:
        # Scaling to each day's own peak made every season look identical.
        summer = art.solar_day(date(2026, 6, 21))
        winter = art.solar_day(date(2026, 12, 21))
        summer_row = art.sun_height(summer.peak_altitude)
        winter_row = art.sun_height(winter.peak_altitude)
        self.assertLess(summer_row, 1.0, "June should reach the top of the panel")
        self.assertGreater(winter_row, summer_row + 2.0, "December should sit visibly lower")


class ArtworkTests(unittest.TestCase):
    def frame(self, when: datetime):
        return art.render(when)

    def test_frame_is_exactly_the_panel(self) -> None:
        frame = self.frame(datetime(2026, 8, 22, 12, 30))
        self.assertEqual(len(frame), art.HEIGHT)
        self.assertTrue(all(len(row) == art.WIDTH for row in frame))
        self.assertTrue(
            all(len(px) == 3 and all(0 <= c <= 255 for c in px) for row in frame for px in row)
        )

    def test_the_now_shaft_marks_the_current_column(self) -> None:
        when = datetime(2026, 8, 22, 3, 10)  # deep night, so only the shaft is lit
        frame = self.frame(when)
        shaft = art.minute_column(3 * 60 + 10)
        lit = sum(sum(frame[y][shaft]) for y in range(art.HEIGHT))
        neighbour = sum(sum(frame[y][shaft - 2]) for y in range(art.HEIGHT))
        self.assertGreater(lit, neighbour * 3)

    def test_the_shaft_is_cool_so_it_cannot_be_read_as_the_sun(self) -> None:
        frame = self.frame(datetime(2026, 8, 22, 12, 30))
        shaft = art.minute_column(12 * 60 + 30)
        red, _, blue = frame[6][shaft]
        self.assertGreater(blue, red, "the marker must stay on the cool side")

    def test_the_ground_row_carries_pips_and_never_sky(self) -> None:
        when = datetime(2026, 8, 22, 12, 30)
        frame = self.frame(when)
        info = art.solar_day(when.date())
        pip_columns = {
            art.minute_column(info.sunrise_minute),
            art.minute_column(info.noon_minute),
            art.minute_column(info.sunset_minute),
            art.minute_column(art.BLACKOUT_MINUTE),
            art.minute_column(when.hour * 60 + when.minute),
        }
        for x in range(art.WIDTH):
            if x in pip_columns:
                continue
            self.assertEqual(frame[art.HEIGHT - 1][x], art.GROUND)

    def test_night_columns_stay_dark(self) -> None:
        frame = self.frame(datetime(2026, 12, 21, 12, 0))
        # 02:15 in December is deep night at this latitude.
        column = art.minute_column(2 * 60 + 15)
        self.assertLess(max(sum(frame[y][column]) for y in range(art.SKY_ROWS)), 120)

    def test_the_arc_touches_the_horizon_row_near_sunrise(self) -> None:
        when = datetime(2026, 6, 21, 12, 0)
        info = art.solar_day(when.date())
        just_up = art.solar_altitude(when.date(), info.sunrise_minute + 20)
        height = art.sun_height(just_up)
        self.assertIsNotNone(height)
        self.assertGreater(height, art.SKY_ROWS - 3)

    def test_frame_bytes_is_row_major_rgb(self) -> None:
        frame = self.frame(datetime(2026, 8, 22, 12, 30))
        data = art.frame_bytes(frame)
        self.assertEqual(len(data), art.WIDTH * art.HEIGHT * 3)
        self.assertEqual(tuple(data[0:3]), frame[0][0])


class PayloadTests(unittest.TestCase):
    def test_page_is_one_full_width_static_bitmap(self) -> None:
        payload = app.build_payload(datetime(2026, 8, 22, 12, 30))
        self.assertEqual(payload["draw"][0][:5], ["bitmap", 0, 0, 32, 8])
        self.assertNotIn("icon", payload)
        self.assertEqual(payload["lifetimeExpiry"], "remove")

    def test_bitmap_decodes_back_to_the_rendered_frame(self) -> None:
        when = datetime(2026, 8, 22, 12, 30)
        payload = app.build_payload(when)
        decoded = base64.b64decode(payload["draw"][0][5])
        self.assertEqual(decoded, art.frame_bytes(art.render(when)))

    def test_page_stays_well_inside_the_body_cap(self) -> None:
        for hour in range(0, 24, 3):
            payload = app.build_payload(datetime(2026, 8, 22, hour, 15))
            size = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
            with self.subTest(hour=hour):
                self.assertLess(size, app.MAX_PAYLOAD_BYTES)
                self.assertLess(size, 8_192, "the firmware rejects bodies over 8KB")

    def test_lifetime_outlasts_several_failed_pushes(self) -> None:
        # A page that expires between writes blinks off the panel for no reason.
        self.assertGreater(app.LIFETIME_MS / 1000, app.PUSH_INTERVAL * 3)


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        # sync_panel asks the panel whether it is lit, so a test that leaves
        # requests.get unpatched reads the real device over the LAN and passes
        # or fails depending on the time of day. Two of these did exactly that,
        # and passed for a night only because the panel happened to be dark.
        # Fail loudly instead; lit_panel()/dark_panel() override this.
        patcher = patch.object(
            app.requests,
            "get",
            side_effect=AssertionError(
                "test reached the real panel - wrap it in lit_panel() or dark_panel()"
            ),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_quiet_hours_remove_the_page_instead_of_pushing(self) -> None:
        with dark_panel(), patch.object(app.requests, "delete", return_value=response()) as delete:
            with patch.object(app.requests, "put") as put:
                published, error = app.sync_panel(datetime(2026, 8, 22, 22, 30), True)
        self.assertFalse(published)
        self.assertIsNone(error)
        put.assert_not_called()
        delete.assert_called_once()

    def test_quiet_hours_make_no_request_when_nothing_is_published(self) -> None:
        with dark_panel(), patch.object(app.requests, "delete") as delete:
            with patch.object(app.requests, "put") as put:
                published, error = app.sync_panel(datetime(2026, 8, 22, 23, 30), False)
        self.assertFalse(published)
        self.assertIsNone(error)
        delete.assert_not_called()
        put.assert_not_called()

    def test_daytime_pushes_the_ribbon(self) -> None:
        with lit_panel(), patch.object(app.requests, "put", return_value=response(200)) as put:
            published, error = app.sync_panel(datetime(2026, 8, 22, 12, 30), False)
        self.assertTrue(published)
        self.assertIsNone(error)
        self.assertEqual(put.call_args.args[0], app.URL)

    def test_an_unreachable_panel_does_not_claim_the_page_is_published(self) -> None:
        with lit_panel(), patch.object(
            app.requests, "put", side_effect=app.requests.ConnectionError("down")
        ):
            published, error = app.sync_panel(datetime(2026, 8, 22, 12, 30), False)
        self.assertFalse(published)
        self.assertIn("unreachable", error)

    def test_a_rejected_page_is_reported_not_raised(self) -> None:
        # Under pythonw an escaping exception takes the app off the panel with
        # no traceback anywhere, and it stays off until somebody notices.
        with lit_panel(), patch.object(app, "build_payload", side_effect=ValueError("too big")):
            with patch.object(app.requests, "put") as put:
                published, error = app.sync_panel(datetime(2026, 8, 22, 12, 30), True)
        put.assert_not_called()
        self.assertTrue(published, "a local rejection must not unpublish the page")
        self.assertIn("page rejected locally", error)

    def test_the_loop_survives_an_unexpected_failure(self) -> None:
        with patch.object(app, "sync_panel", side_effect=RuntimeError("clock exploded")):
            with patch.object(app.time, "sleep") as sleep:
                app.main(["--once"])
        sleep.assert_not_called()  # --once returns before sleeping

    def test_a_manually_woken_panel_gets_the_ribbon_even_at_night(self) -> None:
        """Gating on the clock hid the app from a panel someone switched on.

        Nightkeeper honours a manual override until the next scheduled
        transition, so the panel can legitimately be lit at midnight. Meridian
        followed the clock instead of the panel and stayed hidden all night.
        """
        awake = Mock(status_code=200)
        awake.json.return_value = {"matrixPower": True}
        with patch.object(app.requests, "get", return_value=awake):
            self.assertFalse(app.should_stay_quiet(datetime(2026, 8, 22, 23, 55)))

    def test_a_dark_panel_stays_quiet_even_in_daylight(self) -> None:
        dark = Mock(status_code=200)
        dark.json.return_value = {"matrixPower": False}
        with patch.object(app.requests, "get", return_value=dark):
            self.assertTrue(app.should_stay_quiet(datetime(2026, 8, 22, 12, 30)))

    def test_an_unreachable_panel_falls_back_to_the_clock(self) -> None:
        # Never start pushing to a panel that is probably off just because the
        # network hiccupped.
        with patch.object(app.requests, "get", side_effect=app.requests.ConnectionError("x")):
            self.assertTrue(app.should_stay_quiet(datetime(2026, 8, 22, 23, 55)))
            self.assertFalse(app.should_stay_quiet(datetime(2026, 8, 22, 12, 30)))

    def test_quiet_window_matches_the_blackout_it_mirrors(self) -> None:
        self.assertEqual(app.QUIET_START_MINUTE, art.BLACKOUT_MINUTE)
        self.assertTrue(app.is_quiet_time(datetime(2026, 8, 22, 21, 30)))
        self.assertFalse(app.is_quiet_time(datetime(2026, 8, 22, 21, 29)))
        self.assertTrue(app.is_quiet_time(datetime(2026, 8, 22, 6, 59)))
        self.assertFalse(app.is_quiet_time(datetime(2026, 8, 22, 7, 0)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
