"""Observed weather, the outlook page and NWS alerts.

The fixtures follow the shape api.weather.gov returned for KAJO and the
alerts endpoint on 2026-10-31.  Nothing here contacts a network or the panel.
"""

from __future__ import annotations

import base64
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
from pathlib import Path
import json
import sys
import unittest
from unittest.mock import Mock, patch

from PIL import Image
import requests

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import nws  # noqa: E402
import weather_brief_push as app  # noqa: E402
import weather_icons as icons  # noqa: E402
import wx_pages  # noqa: E402
from test_weather_brief_push import NOW, open_meteo_payload  # noqa: E402
from visual_system import LANE_WIDTH, LOW_BRIGHTNESS_DOMINANT_MIN, measure_text  # noqa: E402


def station_payload(
    *,
    timestamp: str = "2026-08-20T12:05:00+00:00",
    text: str = "Clear",
    celsius: float | None = 20.0,
    layers: list[str] | None = None,
    present: list[str] | None = None,
    gust_kmh: float | None = None,
) -> dict[str, object]:
    return {
        "properties": {
            "timestamp": timestamp,
            "textDescription": text,
            "temperature": {"unitCode": "wmoUnit:degC", "value": celsius},
            "windGust": {"unitCode": "wmoUnit:km_h-1", "value": gust_kmh},
            "cloudLayers": [{"base": {"value": 900}, "amount": a} for a in (layers or ["CLR"])],
            "presentWeather": [{"weather": w, "intensity": None} for w in (present or [])],
        }
    }


def alert_feature(event: str, severity: str = "Moderate", *, onset: datetime | None = None,
                  ends: datetime | None = None, message_type: str = "Alert",
                  status: str = "Actual") -> dict[str, object]:
    onset = onset or NOW - timedelta(hours=1)
    ends = ends or NOW + timedelta(hours=3)
    return {"properties": {
        "event": event, "severity": severity, "status": status, "messageType": message_type,
        "onset": onset.isoformat(), "effective": onset.isoformat(),
        "ends": ends.isoformat(), "expires": ends.isoformat(),
    }}


def response(payload: object, status: int = 200) -> Mock:
    result = Mock(status_code=status)
    result.json.return_value = payload
    result.raise_for_status = Mock(
        side_effect=None if status < 400 else requests.HTTPError(f"HTTP {status}"))
    return result


def two_day_payload(**overrides: object) -> dict[str, object]:
    raw = open_meteo_payload()
    raw["daily"] = {
        "time": ["2026-08-20", "2026-08-21"],
        "temperature_2m_max": [84.1, 79.4],
        "temperature_2m_min": [63.3, 61.2],
        "weather_code": [2, 61],
        "sunrise": ["2026-08-20T05:16", "2026-08-21T05:17"],
        "sunset": ["2026-08-20T18:31", "2026-08-21T18:30"],
        "precipitation_probability_max": [10, 70],
    }
    current = raw["current"]
    assert isinstance(current, dict)
    current.update(overrides)
    return raw


class ObservationTests(unittest.TestCase):
    def test_clear_station_report(self) -> None:
        obs = nws.parse_observation(station_payload(celsius=20.0, gust_kmh=40.0), "KAJO")
        self.assertEqual(obs.icon_kind, "clear")
        self.assertFalse(obs.precipitating)
        self.assertAlmostEqual(obs.temperature_f, 68.0)
        self.assertAlmostEqual(obs.wind_gust_mph, 24.85, places=1)

    def test_precipitation_beats_clouds_and_storm_beats_rain(self) -> None:
        rain = nws.parse_observation(station_payload(layers=["OVC"], present=["rain"]), "KAJO")
        self.assertEqual((rain.icon_kind, rain.precipitating), ("rain", True))
        storm = nws.parse_observation(
            station_payload(present=["rain", "thunderstorms"]), "KAJO")
        self.assertEqual(storm.icon_kind, "storm")
        mist = nws.parse_observation(station_payload(present=["fog_mist"]), "KAJO")
        self.assertEqual((mist.icon_kind, mist.precipitating), ("fog", False))

    def test_the_most_covered_layer_sets_the_sky(self) -> None:
        self.assertEqual(nws.sky_kind([], [{"amount": "FEW"}, {"amount": "BKN"}], ""), "cloudy")
        self.assertEqual(nws.sky_kind([], [{"amount": "SCT"}], ""), "part")
        self.assertEqual(nws.sky_kind([], [{"amount": "OVC"}], ""), "overcast")

    def test_text_is_the_fallback_when_layers_are_missing(self) -> None:
        self.assertEqual(nws.sky_kind([], [], "Mostly Cloudy"), "cloudy")
        self.assertEqual(nws.sky_kind([], [], "Partly Cloudy"), "part")
        self.assertEqual(nws.sky_kind([], [], "Light Drizzle"), "drizzle")
        self.assertIsNone(nws.sky_kind([], [], ""))

    def test_first_fresh_station_with_a_temperature_wins(self) -> None:
        session = Mock()
        session.get.side_effect = [
            requests.ConnectionError("down"),
            response(station_payload(timestamp="2026-08-20T09:00:00+00:00")),  # stale
            response(station_payload(celsius=None)),  # no temperature
            response(station_payload(celsius=25.0)),
        ]
        obs = nws.latest_observation(session, ("KAJO", "KRAL", "KONT", "KRIV"), now=NOW)
        assert obs is not None
        self.assertEqual(obs.station, "KRIV")
        self.assertAlmostEqual(obs.temperature_f, 77.0)
        self.assertIn("User-Agent", session.get.call_args.kwargs["headers"])

    def test_no_fresh_station_returns_none(self) -> None:
        session = Mock()
        session.get.return_value = response({}, status=500)
        self.assertIsNone(nws.latest_observation(session, ("KAJO", "KRAL"), now=NOW))


class ObservedSceneTests(unittest.TestCase):
    def test_model_overcast_observed_clear_shows_clear_and_the_station_temperature(self) -> None:
        # The 2026-10-31 case: the model said WMO 3 while the airport said Clear.
        snapshot = app.parse_weather(two_day_payload(weather_code=3), now=NOW)
        obs = nws.parse_observation(station_payload(celsius=20.0), "KAJO")
        scene = app.choose_scene(snapshot, now=NOW, observation=obs)
        self.assertEqual((scene.kind, scene.icon_kind, scene.text), ("current", "clear", "68\N{DEGREE SIGN}"))

    def test_model_rain_code_with_a_dry_station_is_not_rain_now(self) -> None:
        snapshot = app.parse_weather(two_day_payload(weather_code=61), now=NOW)
        obs = nws.parse_observation(station_payload(layers=["OVC"]), "KAJO")
        scene = app.choose_scene(snapshot, now=NOW, observation=obs)
        self.assertEqual(scene.kind, "current")
        self.assertEqual(scene.icon_kind, "overcast")

    def test_station_rain_is_rain_now(self) -> None:
        snapshot = app.parse_weather(two_day_payload(weather_code=2), now=NOW)
        obs = nws.parse_observation(station_payload(present=["drizzle"]), "KAJO")
        scene = app.choose_scene(snapshot, now=NOW, observation=obs)
        self.assertEqual((scene.kind, scene.text, scene.icon_kind), ("rain", "NOW", "drizzle"))

    def test_without_a_station_the_model_still_decides(self) -> None:
        snapshot = app.parse_weather(two_day_payload(weather_code=61), now=NOW)
        self.assertEqual(app.choose_scene(snapshot, now=NOW).text, "NOW")

    def test_drizzle_code_with_nothing_falling_does_not_start_a_countdown(self) -> None:
        raw = open_meteo_payload(
            probabilities=[5, 5, 30, 30, 30, 5, 5, 5],
            precipitation=[0.0] * 8,
            weather_codes=[3, 3, 51, 51, 51, 3, 3, 3],
        )
        scene = app.choose_scene(app.parse_weather(raw, now=NOW), now=NOW)
        self.assertEqual(scene.kind, "current")


class OutlookTests(unittest.TestCase):
    def snapshot(self, **overrides: object) -> app.WeatherSnapshot:
        return app.parse_weather(two_day_payload(**overrides), now=NOW)

    def test_daytime_shows_the_sunset_time_and_daylight_left(self) -> None:
        scene = app.choose_outlook(self.snapshot(), now=NOW)
        assert scene is not None
        self.assertEqual((scene.kind, scene.icon_kind, scene.text), ("sunset", "sunset", "6:31"))
        self.assertAlmostEqual(scene.meter, (18.31 - 12.2) / (18.31 - 5.27), places=1)

    def test_after_sunset_shows_tomorrow(self) -> None:
        evening = NOW.replace(hour=20)
        scene = app.choose_outlook(self.snapshot(), now=evening)
        assert scene is not None
        self.assertEqual((scene.kind, scene.icon_kind), ("tomorrow", "rain"))
        self.assertEqual(scene.text, "79/61\N{DEGREE SIGN}")
        self.assertAlmostEqual(scene.meter, 0.70)

    def test_before_sunrise_shows_today(self) -> None:
        scene = app.choose_outlook(self.snapshot(), now=NOW.replace(hour=4))
        assert scene is not None
        self.assertEqual((scene.kind, scene.text), ("today", "84/63\N{DEGREE SIGN}"))

    def test_strong_gusts_take_over(self) -> None:
        scene = app.choose_outlook(self.snapshot(wind_gusts_10m=31.4), now=NOW)
        assert scene is not None
        self.assertEqual((scene.kind, scene.text), ("wind", "31MPH"))
        obs = nws.parse_observation(station_payload(gust_kmh=56.0), "KAJO")
        scene = app.choose_outlook(self.snapshot(wind_gusts_10m=5.0), now=NOW, observation=obs)
        assert scene is not None
        self.assertEqual(scene.text, "35MPH")

    def test_a_one_day_response_still_yields_a_page_or_nothing(self) -> None:
        snapshot = app.parse_weather(open_meteo_payload(), now=NOW)
        self.assertEqual(app.choose_outlook(snapshot, now=NOW).kind, "sunset")
        self.assertIsNone(app.choose_outlook(snapshot, now=NOW.replace(hour=21)))


class AlertTests(unittest.TestCase):
    def test_filters_and_orders(self) -> None:
        payload = {"features": [
            alert_feature("Heat Advisory", "Moderate"),
            alert_feature("Red Flag Warning", "Severe"),
            alert_feature("Beach Hazards Statement", "Minor"),
            alert_feature("Wind Advisory", ends=NOW - timedelta(minutes=1)),
            alert_feature("Flood Watch", onset=NOW + timedelta(hours=5)),
            alert_feature("High Wind Warning", "Severe", message_type="Cancel"),
            alert_feature("Test Message", "Extreme", status="Test"),
        ]}
        alerts = nws.parse_alerts(payload, now=NOW)
        self.assertEqual([(a.label, a.kind) for a in alerts], [("FIRE", "fire"), ("HEAT", "heat")])

    def test_event_names_map_to_icons_and_short_labels(self) -> None:
        for event, expected in {
            "Excessive Heat Warning": ("heat", "HEAT"),
            "High Wind Warning": ("wind", "WIND"),
            "Flash Flood Warning": ("flood", "FLOOD"),
            "Severe Thunderstorm Warning": ("storm", "STORM"),
            "Freeze Warning": ("snow", "COLD"),
            "Air Quality Alert": ("alert", "AIR"),
            "Something New": ("alert", "ALERT"),
        }.items():
            self.assertEqual(nws.alert_kind(event), expected)
            self.assertLessEqual(measure_text(expected[1]), LANE_WIDTH)

    def test_request_uses_a_four_decimal_point(self) -> None:
        session = Mock()
        session.get.return_value = response({"features": []})
        nws.active_alerts(session, 33.87529, -117.56644, now=NOW)
        self.assertEqual(session.get.call_args.kwargs["params"]["point"], "33.8753,-117.5664")

    def test_alert_scene_lifetime_and_rail(self) -> None:
        alert = nws.parse_alerts({"features": [alert_feature("Heat Advisory")]}, now=NOW)[0]
        scene = wx_pages.alert_scene(alert, NOW)
        self.assertEqual((scene.app, scene.text, scene.level), ("wxalert", "HEAT", "fair"))
        self.assertEqual(scene.lifetime_ms, wx_pages.ALERT_MAX_LIFETIME_MS)
        self.assertAlmostEqual(scene.meter, 0.75)
        closing = wx_pages.alert_scene(alert, alert.expires - timedelta(seconds=10))
        self.assertEqual(closing.lifetime_ms, wx_pages.ALERT_MIN_LIFETIME_MS)


def every_page_scene() -> list[wx_pages.PageScene]:
    alert = nws.Alert("Heat Advisory", "Moderate", "heat", "HEAT", NOW, NOW + timedelta(hours=2))
    scenes = [
        wx_pages.sunset_scene(NOW, NOW - timedelta(hours=6), NOW + timedelta(hours=6)),
        wx_pages.forecast_scene("rain", 79.4, 61.2, 70),
        wx_pages.forecast_scene("clear", 104, 99, None),
        wx_pages.wind_scene(42, True),
    ]
    for kind in ("heat", "wind", "fire", "flood", "storm", "snow", "alert"):
        scenes.append(wx_pages.PageScene("wxalert", "alert", kind, True, "ALERT", "bad", 0.5, 60_000))
    scenes.append(wx_pages.alert_scene(alert, NOW))
    return scenes


class PageContractTests(unittest.TestCase):
    def test_every_page_fits_the_budget_and_layout(self) -> None:
        for scene in every_page_scene():
            with self.subTest(scene=wx_pages.summary(scene)):
                payload = wx_pages.build_payload(scene)
                size = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
                self.assertLess(size, 2_048)
                self.assertLessEqual(measure_text(scene.text), LANE_WIDTH)
                op = payload["draw"][0]
                self.assertEqual(op[:5], ["bitmap", 9, 0, 23, 8])  # x=8 stays a black gutter
                with Image.open(BytesIO(base64.b64decode(payload["icon"]))) as image:
                    self.assertEqual(image.size, (8, 8))
                    for index in range(image.n_frames):
                        image.seek(index)
                        self.assertEqual(image.info.get("duration", 0) % 120, 0)

    def test_new_icons_keep_a_silhouette_at_brightness_one_and_never_repeat_a_frame(self) -> None:
        for kind in sorted(icons.EXTRA_KINDS):
            with self.subTest(kind=kind):
                art, delay = icons.frames(kind)
                self.assertEqual(delay % 120, 0)
                self.assertGreater(len(art), 1)
                for a, b in zip(art, art[1:] + art[:1]):
                    self.assertNotEqual(a, b)
                for frame in art:
                    lit = [px for row in frame for px in row if px != (0, 0, 0)]
                    strong = [px for px in lit if max(px) >= LOW_BRIGHTNESS_DOMINANT_MIN]
                    self.assertGreaterEqual(len(strong) / len(lit), 0.6)

    def test_text_is_the_full_3x5_font(self) -> None:
        frame = wx_pages.render_lane(wx_pages.wind_scene(31, True))
        lit_rows = {y for y in range(8) for x in range(9, 32) if frame[y][x] != (0, 0, 0)}
        self.assertTrue({1, 2, 3, 4, 5} <= lit_rows)


class DeliveryTests(unittest.TestCase):
    def extras(self) -> app.ExtraPages:
        return app.ExtraPages(outlook=True, alerts=True, min_severity="Moderate",
                              latitude=33.87529, longitude=-117.56644)

    def test_pages_push_on_change_reboot_or_keepalive_only(self) -> None:
        extras = self.extras()
        scene = wx_pages.sunset_scene(NOW, NOW - timedelta(hours=6), NOW + timedelta(hours=6))
        payload = wx_pages.build_payload(scene)
        self.assertTrue(extras.due(scene, payload, False, 0.0))
        extras.mark_sent(scene, payload, 0.0)
        self.assertFalse(extras.due(scene, payload, False, 300.0))
        self.assertTrue(extras.due(scene, payload, True, 300.0))
        self.assertTrue(extras.due(scene, payload, False, app.EXTRA_KEEPALIVE_SECONDS))
        changed = wx_pages.build_payload(wx_pages.forecast_scene("rain", 70, 50, 40))
        self.assertTrue(extras.due(scene, changed, False, 300.0))

    def test_an_alert_outage_keeps_the_last_answer_for_the_next_cycle(self) -> None:
        extras = self.extras()
        snapshot = app.parse_weather(two_day_payload(), now=NOW)
        heat = nws.parse_alerts({"features": [alert_feature("Heat Advisory")]}, now=NOW)
        with patch.object(nws, "active_alerts", return_value=heat):
            first = extras.scenes(Mock(), snapshot, None, 5, now=NOW)
        with patch.object(nws, "active_alerts", side_effect=requests.ConnectionError("x")):
            with redirect_stdout(StringIO()):
                second = extras.scenes(Mock(), snapshot, None, 5, now=NOW)
        self.assertEqual([s.app for s in first], ["wxnext", "wxalert"])
        self.assertEqual([s.app for s in second], ["wxnext", "wxalert"])

    def test_a_failed_extra_never_breaks_the_weather_cycle(self) -> None:
        extras = self.extras()
        snapshot = app.parse_weather(two_day_payload(), now=NOW)
        with patch.object(nws, "active_alerts", return_value=[]):
            with patch.object(app, "push_page", side_effect=RuntimeError("413")):
                with redirect_stdout(StringIO()) as out:
                    app.deliver_extra_pages(extras, Mock(), app.AWTRIX, snapshot, None, 5, False)
        self.assertIn("wxnext not delivered", out.getvalue())

    def test_one_live_cycle_pushes_weatherbrief_then_the_outlook(self) -> None:
        snapshot = app.parse_weather(two_day_payload(), now=NOW)
        obs = nws.parse_observation(station_payload(), "KAJO")
        with patch.object(app, "fetch_weather", return_value=snapshot), \
                patch.object(nws, "latest_observation", return_value=obs), \
                patch.object(nws, "active_alerts", return_value=[]), \
                patch.object(app, "read_panel_health", return_value=None), \
                patch.object(app, "push_payload") as push, \
                patch.object(app, "push_page") as page:
            with redirect_stdout(StringIO()) as out:
                app.run(once=True, observation_stations=("KAJO",), outlook=True, alerts=True)
        push.assert_called_once()
        self.assertEqual([c.args[2] for c in page.call_args_list], ["wxnext"])
        self.assertIn("[obs KAJO]", out.getvalue())

    def test_push_page_targets_the_named_app_through_the_gate(self) -> None:
        session = Mock()
        session.put.return_value = Mock(status_code=200, text="")
        app.push_page(session, app.AWTRIX, "wxnext", {"icon": "R0lGODlh"})
        self.assertTrue(session.put.call_args.args[0].endswith("/api/v1/apps/pushed/wxnext"))


if __name__ == "__main__":
    unittest.main()
