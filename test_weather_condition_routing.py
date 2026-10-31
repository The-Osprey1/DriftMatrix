"""Provider-to-payload contracts for the complete documented weather family."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

import firmware_compat
import led_redesign_preview as gallery
from weather import weather_brief_push as weather, weather_icons as icons

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
# Independently grouped from the provider's documented WMO table.
GROUPS = {
    "clear": (0,), "part": (1, 2), "overcast": (3,), "fog": (45, 48),
    "drizzle": (51, 53, 55), "freezing": (56, 57, 66, 67),
    "rain": (61, 63, 65, 80, 81, 82), "snow": (71, 73, 75, 77, 85, 86),
    "storm": (95, 96, 97, 99),
}
EXPECTED = [(code, kind) for kind, codes in GROUPS.items() for code in codes]
WET = [(code, kind) for code, kind in EXPECTED if kind in ("drizzle", "freezing", "rain", "snow", "storm")]
BASELINE = json.loads((Path(__file__).parent / "docs/weather-conditions/before.json").read_text())


def provider(code, day=True, future=()):
    return {"timezone": "UTC", "current": {"time": NOW.isoformat(), "temperature_2m": 64.0, "weather_code": code, "is_day": int(day)},
            "daily": {"temperature_2m_min": [54.0], "temperature_2m_max": [72.0]},
            "minutely_15": {"time": [row[0].isoformat() for row in future],
                            "precipitation_probability": [row[1] for row in future],
                            "precipitation": [row[2] for row in future], "weather_code": [row[3] for row in future]}}


def body(scene, animated):
    endpoint = "offline://weather-code-test"
    firmware_compat.observe_device(endpoint, {"version": "1.2.2"})
    return firmware_compat.prepare_payload(endpoint, weather.build_payload(scene, animated))


def lane(frame):
    return [frame[y * 32 + x] for y in range(8) for x in range(9, 32)]


@pytest.mark.parametrize("code,kind", EXPECTED)
@pytest.mark.parametrize("day", [False, True])
def test_every_documented_code_reaches_the_right_actual_payload(code, kind, day):
    snapshot = weather.parse_weather(provider(code, day), now=NOW)
    scene = weather.choose_scene(snapshot, now=NOW)
    assert scene.icon_kind == kind
    assert scene.is_day == day
    wet = kind in ("drizzle", "freezing", "rain", "snow", "storm")
    assert scene.kind == ("rain" if wet else "current")
    assert scene.text == ("NOW" if wet else "64°")
    expected_frames, delay = icons.frames(kind, day)
    expected_shape = {(x, y) for y in range(8) for x in range(8) if expected_frames[0][y][x] != icons.BLACK}
    for animated in (False, True):
        payload = body(scene, animated)
        decoded = gallery.decode(payload)
        actual_shape = {(x, y) for y in range(8) for x in range(8) if decoded["frames"][0][y * 32 + x] != "000000"}
        assert actual_shape == expected_shape
        assert decoded["bytes"] < 2048 and decoded["iconBytes"] <= weather.ICON_MAX_BYTES
        assert all(ms > 0 and ms % 120 == 0 for ms in decoded["durations"])
        assert all(lane(frame) == lane(decoded["frames"][0]) for frame in decoded["frames"])
        original = next(c for c in BASELINE["cases"] if c["code"] == code and c["day"] == day and c["animated"] == animated)
        assert lane(decoded["frames"][0]) == lane(gallery.decode(original["payload"])["frames"][0])
        assert {k: v for k, v in payload.items() if k != "icon"} == {k: v for k, v in original["payload"].items() if k != "icon"}


@pytest.mark.parametrize("code,kind", WET)
@pytest.mark.parametrize("day", [False, True])
def test_every_wet_forecast_keeps_exact_eta_and_its_precipitation_type(code, kind, day):
    when = NOW + timedelta(minutes=18, seconds=20)
    snapshot = weather.parse_weather(provider(0, day, [(when, 25, 0.0, code)]), now=NOW)
    scene = weather.choose_scene(snapshot, now=NOW)
    assert scene.kind == "rain" and scene.eta_minutes == 19 and scene.text == "19M"
    assert scene.icon_kind == kind
    for animated in (False, True):
        current = body(scene, animated)
        original = next(c["payload"] for c in BASELINE["forecast_cases"] if c["code"] == code and c["day"] == day and c["animated"] == animated)
        assert gallery.decode(current)["bytes"] < 2048
        assert {k: v for k, v in current.items() if k != "icon"} == {k: v for k, v in original.items() if k != "icon"}


@pytest.mark.parametrize("code", [-1, 4, 44, 46, 47, 52, 54, 58, 60, 62, 64, 68, 70, 72, 74, 76, 78, 79, 83, 84, 87, 94, 98, 100, 999])
def test_unassigned_codes_show_unknown_without_inventing_precipitation(code):
    scene = weather.choose_scene(weather.parse_weather(provider(code), now=NOW), now=NOW)
    assert scene.icon_kind == "unknown" and scene.kind == "current"
    assert scene.text == "64°" and scene.eta_minutes is None
    animated = gallery.decode(body(scene, True))
    static = gallery.decode(body(scene, False))
    assert animated["frames"] == static["frames"] and len(animated["frames"]) == 1
    assert all(animated["frames"][0][y * 32 + x] == "000000" for y in range(8) for x in range(8))


@pytest.mark.parametrize("day", [False, True])
def test_freezing_reuses_original_rain_motion_without_new_ice_art(day):
    assert icons.frames("freezing", day) == icons.frames("rain", day)


def test_dry_current_forecast_probability_does_not_claim_precipitation_now():
    for code in (0, 1, 2, 3, 45, 48, 999):
        snapshot = weather.parse_weather(provider(0, future=[(NOW - timedelta(minutes=5), 90, 0.0, code)]), now=NOW)
        scene = weather.choose_scene(snapshot, now=NOW)
        assert scene.kind == "current" and scene.text == "64°"


def test_weather_table_covers_exactly_the_documented_codes():
    assert set(weather.WMO_ICON_KIND) == {code for code, _ in EXPECTED}
    assert len(weather.WMO_ICON_KIND) == 29


def test_original_animation_frames_and_timing_match_frozen_history():
    import hashlib
    oracle = json.loads((Path(__file__).parent / "docs/weather-conditions/classic-frame-oracle.json").read_text())
    assert oracle["source_revision"] == "8f73bdc9ee5f1ee5a89ad9bd17e2bdbea271f8c4"
    for case in oracle["cases"]:
        frames, delay = icons.frames(case["kind"], case["day"])
        assert delay == case["delay_ms"]
        assert [hashlib.sha256(bytes(c for row in frame for rgb in row for c in rgb)).hexdigest() for frame in frames] == case["frames_sha256"]


@pytest.mark.parametrize("code", [51, 56, 61, 71, 95])
def test_current_precipitation_keeps_every_NOW_glyph_pixel(code):
    from weather.weather_brief_preview import FONT_3X5
    scene = weather.choose_scene(weather.parse_weather(provider(code), now=NOW), now=NOW)
    frame = gallery.decode(body(scene, False))["frames"][0]
    for y in range(5):
        bits = "0".join(FONT_3X5[c][y] for c in "NOW")
        assert [frame[(y + 1) * 32 + 15 + x] != "000000" for x in range(11)] == [b == "1" for b in bits]
