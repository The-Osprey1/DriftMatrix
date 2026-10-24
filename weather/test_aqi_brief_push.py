"""Wire and visual contract tests for the PC-pushed Jewelbox AQI Brief."""

from __future__ import annotations

import ast
import base64
import json
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from PIL import Image
import requests


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import aqi_brief_preview as preview  # noqa: E402
import aqi_brief_push as app  # noqa: E402


NOW = datetime(2026, 8, 20, 11, 12, tzinfo=timezone.utc)


def open_meteo_payload(aqi: int = 72, pm2_5: float = 19.1) -> dict[str, object]:
    return {
        "latitude": 33.9,
        "longitude": -117.6,
        "timezone": "UTC",
        "current_units": {
            "time": "iso8601",
            "interval": "seconds",
            "us_aqi": "USAQI",
            "pm2_5": "N{MICRO SIGN}g/mN{SUPERSCRIPT THREE}",
        },
        "current": {
            "time": "2026-08-20T11:00",
            "interval": 3600,
            "us_aqi": aqi,
            "pm2_5": pm2_5,
        },
        "hourly_units": {"time": "iso8601", "us_aqi": "USAQI"},
        "hourly": {
            "time": [
                "2026-08-20T11:00",
                "2026-08-20T12:00",
                "2026-08-20T13:00",
                "2026-08-20T14:00",
                "2026-08-20T15:00",
                "2026-08-20T16:00",
            ],
            "us_aqi": [aqi, aqi, aqi + 1, aqi + 1, aqi + 2, aqi + 2],
        },
    }


def snapshot(aqi: int = 72) -> app.AQISnapshot:
    return app.parse_air_quality(open_meteo_payload(aqi), now=NOW)


def snapshot_with_forecast(aqi: int, values: list[int]) -> app.AQISnapshot:
    raw = open_meteo_payload(aqi)
    assert isinstance(raw["hourly"], dict)
    raw["hourly"]["us_aqi"] = values
    return app.parse_air_quality(raw, now=NOW)


def gif_frames(data: bytes) -> tuple[list[Image.Image], list[int], int]:
    frames: list[Image.Image] = []
    durations: list[int] = []
    with Image.open(BytesIO(data)) as image:
        loop = int(image.info.get("loop", 0))
        for index in range(image.n_frames):
            image.seek(index)
            frames.append(image.convert("RGB").copy())
            durations.append(int(image.info.get("duration", 0)))
    return frames, durations, loop


def decode_lane(payload: dict[str, object]) -> bytes:
    draw = payload.get("draw")
    if not isinstance(draw, list) or len(draw) != 1:
        raise AssertionError("AQI payload must have exactly one draw command")
    operation = draw[0]
    if not isinstance(operation, list) or operation[:5] != ["bitmap", 9, 0, 23, 8]:
        raise AssertionError(f"unexpected AQI lane command: {operation!r}")
    if len(operation) != 6 or not isinstance(operation[5], str):
        raise AssertionError("AQI lane must contain base64 RGB888 data")
    return base64.b64decode(operation[5], validate=True)


def lane_pixel(data: bytes, x: int, y: int) -> tuple[int, int, int]:
    offset = (y * preview.LANE_WIDTH + (x - preview.LANE_X0)) * 3
    return tuple(data[offset : offset + 3])  # type: ignore[return-value]


class AQILogicTests(unittest.TestCase):
    def test_all_us_aqi_boundaries_map_to_the_six_official_bands(self) -> None:
        cases = {
            0: 0,
            50: 0,
            51: 1,
            100: 1,
            101: 2,
            150: 2,
            151: 3,
            200: 3,
            201: 4,
            300: 4,
            301: 5,
            500: 5,
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(preview.category_index(value), expected)

    def test_reading_is_explicit_and_fits_the_information_lane(self) -> None:
        for value in (0, 9, 50, 72, 100, 138, 300, 500, 501, 999):
            with self.subTest(value=value):
                text = preview.display_text(value)
                width = preview.text_width(text)
                self.assertLessEqual(width, preview.LANE_WIDTH)
                self.assertTrue(text.startswith("AQI "))
        self.assertEqual(preview.display_text(138), "AQI 138")
        self.assertEqual(preview.display_text(501), "AQI 500")

    def test_current_response_is_normalized_without_false_precision(self) -> None:
        result = snapshot(72)
        self.assertEqual(result.us_aqi, 72)
        self.assertEqual(result.pm2_5, 19.1)
        self.assertEqual(result.observed_at, datetime(2026, 8, 20, 11, 0, tzinfo=timezone.utc))
        self.assertEqual(result.forecast_aqi, (72, 72, 73, 73, 74, 74))
        self.assertEqual(result.next_five_aqi, (72, 73, 73, 74, 74))
        self.assertEqual(result.level, 1)

    def test_parse_rejects_stale_or_future_observations(self) -> None:
        raw = open_meteo_payload()
        boundary = datetime(2026, 8, 20, 12, 30, tzinfo=timezone.utc)
        self.assertEqual(app.parse_air_quality(raw, now=boundary).us_aqi, 72)
        with self.assertRaisesRegex(ValueError, "stale"):
            app.parse_air_quality(raw, now=boundary + timedelta(seconds=1))
        with self.assertRaisesRegex(ValueError, "future"):
            app.parse_air_quality(raw, now=datetime(2026, 8, 20, 10, 39, 59, tzinfo=timezone.utc))

    def test_parse_fails_closed_on_bad_arrays_and_nonfinite_values(self) -> None:
        raw = open_meteo_payload()
        raw["hourly"] = {"time": ["2026-08-20T11:00"], "us_aqi": []}
        with self.assertRaises(ValueError):
            app.parse_air_quality(raw, now=NOW)
        raw = open_meteo_payload()
        assert isinstance(raw["current"], dict)
        raw["current"]["us_aqi"] = float("nan")
        with self.assertRaises(ValueError):
            app.parse_air_quality(raw, now=NOW)

    def test_query_requests_only_the_needed_contract(self) -> None:
        params = app.request_params()
        self.assertEqual(params["current"], "us_aqi,pm2_5")
        self.assertEqual(params["hourly"], "us_aqi")
        self.assertEqual(params["forecast_hours"], 6)
        self.assertEqual(params["timezone"], "auto")

    def test_relevance_hides_good_air_and_shows_moderate_air(self) -> None:
        self.assertFalse(app.is_relevant(snapshot_with_forecast(50, [50] * 6)))
        self.assertTrue(app.is_relevant(snapshot_with_forecast(51, [51] * 6)))

    def test_relevance_uses_a_clear_threshold_after_the_page_is_visible(self) -> None:
        value = snapshot_with_forecast(47, [47] * 6)
        self.assertFalse(app.is_relevant(value, currently_visible=False))
        self.assertTrue(app.is_relevant(value, currently_visible=True))
        self.assertFalse(
            app.is_relevant(snapshot_with_forecast(44, [44] * 6), currently_visible=True)
        )

    def test_dwell_and_quiet_hours_match_the_attention_policy(self) -> None:
        self.assertEqual(app.page_duration_ms(snapshot(72)), 5_000)
        self.assertEqual(app.page_duration_ms(snapshot(101)), 7_000)
        self.assertFalse(app.is_quiet_time(datetime(2026, 8, 20, 21, 29, tzinfo=timezone.utc)))
        self.assertTrue(app.is_quiet_time(datetime(2026, 8, 20, 21, 30, tzinfo=timezone.utc)))
        self.assertTrue(app.is_quiet_time(datetime(2026, 8, 21, 6, 59, tzinfo=timezone.utc)))
        self.assertFalse(app.is_quiet_time(datetime(2026, 8, 21, 7, 0, tzinfo=timezone.utc)))

    def test_relevance_shows_a_meaningful_forecast_crossing(self) -> None:
        self.assertTrue(app.is_relevant(snapshot_with_forecast(46, [46, 47, 49, 51, 52, 52])))
        self.assertFalse(app.is_relevant(snapshot_with_forecast(49, [49, 49, 50, 51, 51, 51])))


class AQIArtTests(unittest.TestCase):
    def test_exact_panel_geometry_has_a_black_gutter_and_fixed_lung_silhouette(self) -> None:
        expected = preview.LEFT_LUNG | preview.RIGHT_LUNG | preview.AIRWAY
        for level_value in (32, 82, 126, 178, 245, 360):
            for phase in range(4):
                frame = preview.panel_frame(level_value, phase)
                self.assertEqual(len(frame), 8)
                self.assertTrue(all(len(row) == 32 for row in frame))
                self.assertTrue(all(frame[y][8] == preview.BLACK for y in range(8)))
                occupied = {
                    (x, y)
                    for y in range(8)
                    for x in range(8)
                    if frame[y][x] != preview.BLACK
                }
                self.assertEqual(occupied, expected)

    def test_breathing_motion_is_symmetric_connected_and_never_blinks_the_outline(self) -> None:
        frames = [preview.panel_frame(82, phase) for phase in range(4)]
        icon_pixels = [tuple(tuple(row[:8]) for row in frame) for frame in frames]
        self.assertEqual(len(set(icon_pixels)), 3)  # middle pose repeats on the way out
        for frame in frames:
            for y in range(8):
                for x in range(8):
                    self.assertEqual(frame[y][x], frame[y][7 - x])
        for first, second in zip(frames, frames[1:] + frames[:1]):
            changed = {
                (x, y)
                for y in range(8)
                for x in range(8)
                if first[y][x] != second[y][x]
            }
            self.assertGreaterEqual(len(changed), 4)
            self.assertLessEqual(len(changed), 14)
            self.assertTrue(changed <= (preview.LEFT_LUNG | preview.RIGHT_LUNG))

    def test_airway_is_a_stable_bright_anchor(self) -> None:
        for phase in range(4):
            frame = preview.panel_frame(178, phase)
            self.assertEqual(frame[0][3], preview.ICE)
            self.assertEqual(frame[0][4], preview.ICE)
            self.assertEqual(frame[2][2], preview.PEARL)
            self.assertEqual(frame[2][5], preview.PEARL)

    def test_information_lane_has_no_top_or_bottom_bar(self) -> None:
        for value in (32, 82, 126, 178, 245, 360):
            frame = preview.panel_frame(value, 0)
            for y in (0, 6, 7):
                self.assertEqual(frame[y][preview.LANE_X0 :], [preview.BLACK] * preview.LANE_WIDTH)

    def test_forecast_affects_relevance_but_never_adds_visual_bars(self) -> None:
        steady = snapshot_with_forecast(72, [72, 72, 72, 72, 72, 72])
        worsening = snapshot_with_forecast(72, [72, 90, 120, 160, 220, 300])
        self.assertEqual(app.build_payload(steady), app.build_payload(worsening))
        for value in (steady, worsening):
            lane = decode_lane(app.build_payload(value))
            # Calm glance: rows 0 and 6 stay black; row 7 is the one meter.
            for y in (0, 6):
                for x in range(preview.LANE_X0, preview.LANE_X1 + 1):
                    self.assertEqual(lane_pixel(lane, x, y), preview.BLACK)

    def test_every_palette_role_survives_actual_panel_gamma_at_brightness_45(self) -> None:
        self.assertEqual(preview.PANEL_GAMMA, 2.2)
        for color in (*preview.AQI_COLORS, preview.PEARL, preview.ICE):
            with self.subTest(color=color):
                graded = preview.grade_color(
                    color,
                    gamma=preview.PANEL_GAMMA,
                    brightness=preview.PANEL_BRIGHTNESS,
                    saturation=preview.PANEL_SATURATION,
                )
                self.assertNotEqual(graded, preview.BLACK)

    def test_icon_is_bounded_opaque_and_severity_controls_only_timing(self) -> None:
        """Severity changes the colour and the comet's pace, never the shape.

        Only the 8x8 icon is animated. The 23x8 information lane is a separate
        static bitmap, keeping decoded animation work off the rest of the panel.
        """

        geometries: list[list[tuple[tuple[int, int], ...]]] = []
        for level, value in enumerate((32, 82, 126, 178, 245, 360)):
            data = app.build_icon_gif(snapshot(value), animated=True)
            frames, durations, loop = gif_frames(data)
            self.assertLessEqual(len(data), app.ICON_MAX_BYTES)
            self.assertEqual(len(frames), app.ICON_MAX_FRAMES)
            self.assertEqual(loop, 0)
            self.assertEqual(durations, [app.ICON_FRAME_MS[level]] * app.ICON_MAX_FRAMES)
            self.assertTrue(all(frame.size == (8, 8) for frame in frames))
            # Every delay must land on the 120ms beat or the breath judders.
            self.assertTrue(all(duration % 120 == 0 for duration in durations))
            with Image.open(BytesIO(data)) as image:
                self.assertNotIn("transparency", image.info)
            geometries.append(
                [
                    tuple(
                        (x, y)
                        for y in range(8)
                        for x in range(8)
                        if frame.getpixel((x, y)) != (0, 0, 0)
                    )
                    for frame in frames
                ]
            )
        self.assertTrue(all(item == geometries[0] for item in geometries[1:]))
        # Clean air drifts; worse air hurries.
        self.assertGreater(app.ICON_FRAME_MS[0], app.ICON_FRAME_MS[5])

    def test_static_safety_icon_is_one_frame(self) -> None:
        frames, _, _ = gif_frames(app.build_icon_gif(snapshot(), animated=False))
        self.assertEqual(len(frames), 1)


class AQIWireTests(unittest.TestCase):
    def test_payload_splits_tiny_animated_icon_from_static_lane(self) -> None:
        payload = app.build_payload(snapshot())
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
        self.assertEqual(payload["iconOffsetX"], 0)
        self.assertEqual(payload["backgroundColor"], "#000000")
        self.assertEqual(payload["durationMs"], 5000)
        self.assertEqual(payload["lifetimeMs"], 2_700_000)
        self.assertEqual(payload["lifetimeExpiry"], "remove")
        frames, _, _ = gif_frames(base64.b64decode(payload["icon"], validate=True))
        self.assertTrue(all(frame.size == (8, 8) for frame in frames))
        self.assertEqual(len(decode_lane(payload)), 23 * 8 * 3)
        self.assertLess(len(json.dumps(payload).encode("utf-8")), app.SAFE_BODY_BYTES)

    def test_icon_is_the_wind_and_lane_is_the_rich_rail(self) -> None:
        from visual_system import render_rich_lane

        value = snapshot(72)
        payload = app.build_payload(value, animated=False)
        frames, _, _ = gif_frames(base64.b64decode(payload["icon"], validate=True))
        expected_icon = app._icon_frame(1, app.ICON_MAX_FRAMES - 1)
        for y in range(8):
            for x in range(8):
                self.assertEqual(frames[0].getpixel((x, y)), expected_icon.getpixel((x, y)))
        expected_lane = render_rich_lane("72", fraction=72 / app.AQI_METER_MAX, stops=app.AQI_RAIL_STOPS,
                                         label="AQI", label_color=app.AQI_COLORS[1])
        rendered_lane = decode_lane(payload)
        for y in range(8):
            for x in range(preview.LANE_X0, preview.LANE_X1 + 1):
                self.assertEqual(lane_pixel(rendered_lane, x, y), expected_lane[y][x],
                                 f"lane differs at {x},{y}")

    def test_face_moves_every_beat(self) -> None:
        shots = [list(app._icon_frame(0, phase).getdata()) for phase in range(app.ICON_MAX_FRAMES)]
        self.assertEqual(len({tuple(s) for s in shots}), app.ICON_MAX_FRAMES)

    def test_payload_is_deterministic_for_all_categories(self) -> None:
        for value in (32, 82, 126, 178, 245, 360):
            with self.subTest(value=value):
                self.assertEqual(app.build_payload(snapshot(value)), app.build_payload(snapshot(value)))

    def test_refresh_lifetime_and_transport_have_safe_load(self) -> None:
        self.assertEqual(app.REFRESH_SECONDS, 900)
        self.assertGreaterEqual(app.LIFETIME_MS, app.REFRESH_SECONDS * 1000 * 2)
        self.assertEqual(app.AWTRIX, "http://192.168.1.90")
        self.assertNotIn("screen", app.DEVICE_PATH)

    def test_sender_deletes_only_its_own_page_never_wakes_and_has_one_activation_path(self) -> None:
        source = Path(app.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        destructive = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr.lower() == "delete"
        ]
        self.assertEqual(len(destructive), 1)
        self.assertEqual(app.DELETE_PATH, "/api/v1/apps/aqibrief")
        self.assertNotIn('"wakeup"', source)
        self.assertEqual(source.count("/api/v1/apps/active"), 1)

    def test_delete_transport_is_exact_and_idempotent_at_the_api_boundary(self) -> None:
        response = Mock(status_code=200)
        session = Mock()
        session.delete.return_value = response
        app.delete_app(session, "http://panel/")
        session.delete.assert_called_once_with(
            "http://panel/api/v1/apps/aqibrief",
            timeout=app.REQUEST_TIMEOUT_SECONDS,
        )

    def test_activation_transport_is_non_waking_and_exact(self) -> None:
        response = Mock(status_code=200)
        session = Mock()
        session.put.return_value = response
        app.activate_app(session, "http://panel/")
        session.put.assert_called_once_with(
            "http://panel/api/v1/apps/active",
            json={"name": "aqibrief", "fast": False},
            timeout=app.REQUEST_TIMEOUT_SECONDS,
        )

    def test_attribution_is_kept_with_the_sender(self) -> None:
        source = Path(app.__file__).read_text(encoding="utf-8")
        self.assertIn("Open-Meteo.com", source)
        self.assertIn("Copernicus Atmosphere Monitoring Service", source)

    def test_one_cycle_fetches_once_probes_once_and_pushes_once(self) -> None:
        value = snapshot()
        with patch.object(app, "fetch_air_quality", return_value=value) as fetch:
            with patch.object(app, "read_panel_health", return_value=None) as health:
                with patch.object(app, "push_payload") as push:
                    with redirect_stdout(StringIO()):
                        app.run(once=True)
        fetch.assert_called_once()
        health.assert_called_once()
        push.assert_called_once()

    def test_crossing_into_unhealthy_activates_once_after_a_successful_push(self) -> None:
        class StopLoop(Exception):
            pass

        values = (snapshot(72), snapshot(110))
        with patch.object(app, "fetch_air_quality", side_effect=values):
            with patch.object(app, "read_panel_health", return_value=None):
                with patch.object(app, "push_payload") as push:
                    with patch.object(app, "activate_app") as activate:
                        with patch.object(app, "is_quiet_time", return_value=False):
                            with patch.object(app.time, "sleep", side_effect=[None, StopLoop()]):
                                with redirect_stdout(StringIO()):
                                    with self.assertRaises(StopLoop):
                                        app.run()
        self.assertEqual(push.call_count, 2)
        activate.assert_called_once()

    def test_good_air_deletes_once_without_health_probe_or_push(self) -> None:
        value = snapshot_with_forecast(35, [35] * 6)
        with patch.object(app, "fetch_air_quality", return_value=value) as fetch:
            with patch.object(app, "read_panel_health") as health:
                with patch.object(app, "push_payload") as push:
                    with patch.object(app, "delete_app") as delete:
                        with redirect_stdout(StringIO()):
                            app.run(once=True)
        fetch.assert_called_once()
        delete.assert_called_once()
        health.assert_not_called()
        push.assert_not_called()

    def test_api_failure_never_touches_the_panel(self) -> None:
        with patch.object(
            app, "fetch_air_quality", side_effect=requests.RequestException("offline")
        ):
            with patch.object(app, "push_payload") as push:
                with patch.object(app, "read_panel_health") as health:
                    with self.assertRaises(requests.RequestException):
                        with redirect_stdout(StringIO()):
                            app.run(once=True)
        push.assert_not_called()
        health.assert_not_called()

    def test_health_probe_is_read_only(self) -> None:
        session = Mock()
        session.get.return_value = Mock(
            status_code=200,
            json=Mock(return_value={"uptimeSeconds": 1234, "resetReason": "POWERON"}),
        )
        self.assertEqual(app.read_panel_health(session, app.AWTRIX), app.PanelHealth(1234, "POWERON"))
        session.get.assert_called_once_with(
            f"{app.AWTRIX}{app.DEVICE_PATH}", timeout=app.REQUEST_TIMEOUT_SECONDS
        )
        session.put.assert_not_called()
        session.delete.assert_not_called()

    def test_uptime_rollback_latches_the_second_push_to_static(self) -> None:
        class StopLoop(Exception):
            pass

        pushed: list[dict[str, object]] = []
        health = (app.PanelHealth(500, "poweron"), app.PanelHealth(12, "panic"))

        def record(_session: object, _base: str, payload: object) -> None:
            assert isinstance(payload, dict)
            pushed.append(payload)

        with patch.object(app, "fetch_air_quality", return_value=snapshot()):
            with patch.object(app, "read_panel_health", side_effect=health):
                with patch.object(app, "push_payload", side_effect=record):
                    with patch.object(app.time, "sleep", side_effect=[None, StopLoop()]):
                        with redirect_stdout(StringIO()):
                            with self.assertRaises(StopLoop):
                                app.run()

        self.assertEqual(len(pushed), 2)
        first, _, _ = gif_frames(base64.b64decode(pushed[0]["icon"], validate=True))
        second, _, _ = gif_frames(base64.b64decode(pushed[1]["icon"], validate=True))
        self.assertEqual(len(first), app.ICON_MAX_FRAMES)
        self.assertEqual(len(second), 1)

    def test_five_panel_failures_switch_the_sixth_attempt_to_static(self) -> None:
        class StopLoop(Exception):
            pass

        pushed: list[dict[str, object]] = []

        def fail_then_succeed(_session: object, _base: str, payload: object) -> None:
            assert isinstance(payload, dict)
            pushed.append(payload)
            if len(pushed) <= app.SAFE_MODE_FAILURES:
                raise RuntimeError("simulated rejection")

        sleeps = [None] * app.SAFE_MODE_FAILURES + [StopLoop()]
        with patch.object(app, "fetch_air_quality", return_value=snapshot()):
            with patch.object(app, "read_panel_health", return_value=None):
                with patch.object(app, "push_payload", side_effect=fail_then_succeed):
                    with patch.object(app.time, "sleep", side_effect=sleeps):
                        with redirect_stdout(StringIO()):
                            with self.assertRaises(StopLoop):
                                app.run()

        self.assertEqual(len(pushed), app.SAFE_MODE_FAILURES + 1)
        final, _, _ = gif_frames(base64.b64decode(pushed[-1]["icon"], validate=True))
        self.assertEqual(len(final), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class PlannedRebootTests(unittest.TestCase):
    def test_requested_restart_keeps_animation_and_a_crash_does_not(self) -> None:
        old = app.PanelHealth(900, "poweron")
        self.assertTrue(app.unplanned_reboot(old, app.PanelHealth(12, "panic")))
        self.assertFalse(app.unplanned_reboot(old, app.PanelHealth(12, "software")))
        self.assertFalse(app.unplanned_reboot(old, app.PanelHealth(950, "panic")))


class WindIconTests(unittest.TestCase):
    def test_shared_palette_preserves_every_animated_pixel(self) -> None:
        for level, value in enumerate((32, 82, 126, 178, 245, 360)):
            encoded = app.build_icon_gif(snapshot(value))
            frames, _durations, _loop = gif_frames(encoded)
            self.assertLess(len(encoded), 650, "repeated colour tables consume the page budget")
            for phase, frame in enumerate(frames):
                self.assertEqual(frame.tobytes(), app._icon_frame(level, phase).tobytes())
                lit = [colour for colour in frame.getdata() if colour != (0, 0, 0)]
                surviving = [colour for colour in lit if max(colour) >= 174]
                self.assertGreaterEqual(len(surviving), len(lit) * 0.6)

    def test_comets_run_the_whole_gust_and_never_leave_the_glyph(self) -> None:
        glyph = {p for path in app.WIND_GUSTS for p in path}
        for level in range(6):
            for phase in range(app.ICON_MAX_FRAMES):
                image = app._icon_frame(level, phase)
                lit = {(x, y) for y in range(8) for x in range(8) if image.getpixel((x, y)) != (0, 0, 0)}
                self.assertEqual(lit, glyph)
        # Over one cycle every pixel of every gust is visited by a comet head.
        heads = set()
        for phase in range(app.ICON_MAX_FRAMES):
            image = app._icon_frame(1, phase)
            heads |= {p for p in glyph if image.getpixel(p) == app.PEARL}
        self.assertEqual(heads, glyph)

    def test_rail_track_is_tinted_never_grey(self) -> None:
        lane = app._lane_bytes(snapshot(72))
        row7 = [tuple(lane[(7 * app.LANE_WIDTH + x) * 3:(7 * app.LANE_WIDTH + x) * 3 + 3]) for x in range(app.LANE_WIDTH)]
        track = row7[-1]
        self.assertNotEqual(len(set(track)), 1, "the empty rail should carry its hue, not grey")
        self.assertIn(app.PEARL, row7)

    def test_every_category_fits_the_safety_budget(self) -> None:
        for value in (32, 82, 126, 178, 245, 360):
            body = json.dumps(app.build_payload(snapshot(value)), separators=(",", ":"))
            self.assertLess(len(body), app.SAFE_BODY_BYTES)

