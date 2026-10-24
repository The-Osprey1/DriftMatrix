"""Deterministic checks for the P0 AWTRIX weather rules.

The panel itself is the Berry compiler/runtime check. These tests protect the
semantic choices that are easiest to regress while editing the compact scripts.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent

# Importable whether pytest is run from this folder or from the repo root.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def category(code: int | None) -> str:
    if code is None:
        return "unknown"
    if code == 0:
        return "sun"
    if code <= 2:
        return "part"
    if code == 3:
        return "cloud"
    if 45 <= code <= 48:
        return "fog"
    if 51 <= code <= 67:
        return "rain"
    if 71 <= code <= 77 or code in (85, 86):
        return "snow"
    if 80 <= code <= 82:
        return "rain"
    if 95 <= code <= 99:
        return "storm"
    return "cloud"


def choose_hazard(
    hours: list[dict],
    wind: int = 0,
    gust: int = 0,
    feels_now: int | None = None,
    high: int | None = None,
    low: int | None = None,
    uv_max: int | None = None,
    current_code: int = 0,
) -> tuple[str, int, int]:
    """Python mirror of wxfeed's score/earliest tie-break rule."""

    best = (0, "", 0, 99)

    def consider(score: int, kind: str, value: int, eta: int) -> None:
        nonlocal best
        if score > best[0] or (score == best[0] and eta < best[3]):
            best = (score, kind, value, eta)

    if 95 <= current_code <= 99:
        consider(100, "storm", current_code, 0)
    elif 56 <= current_code <= 57 or 66 <= current_code <= 67:
        consider(98, "ice", 0, 0)
    elif 71 <= current_code <= 77 or current_code in (85, 86):
        consider(95, "snow", 0, 0)
    elif current_code in (63, 65, 81, 82):
        consider(92, "rain", 0, 0)
    elif 51 <= current_code <= 65 or 80 <= current_code <= 82:
        consider(88, "rain", 0, 0)

    if gust >= 40:
        consider(85, "gust", gust, 0)
    elif gust >= 25:
        consider(65, "gust", gust, 0)
    if wind >= 15:
        consider(30, "wind", wind, 0)
    if feels_now is not None and feels_now >= 100:
        consider(90, "heat", feels_now, 0)
    elif feels_now is not None and feels_now <= 32:
        consider(90, "cold", feels_now, 0)
    elif feels_now is not None and feels_now >= 95:
        consider(70, "heat", feels_now, 0)
    if high is not None and high >= 100:
        consider(90, "heat", high, 9)
    elif low is not None and low <= 32:
        consider(90, "cold", low, 9)
    elif high is not None and high >= 95:
        consider(70, "heat", high, 9)
    if uv_max is not None and uv_max >= 8:
        consider(60, "uv", uv_max, 9)
    elif uv_max is not None and uv_max >= 6:
        consider(45, "uv", uv_max, 9)

    for eta, hour in enumerate(hours):
        code = hour.get("code")
        feels = hour.get("feels")
        hour_gust = hour.get("gust")
        pop = hour.get("pop")
        uv = hour.get("uv")

        if code is not None and 95 <= code <= 99:
            consider(100, "storm", code, eta)
        if feels is not None and feels >= 100:
            consider(90, "heat", feels, eta)
        elif feels is not None and feels <= 32:
            consider(90, "cold", feels, eta)
        elif feels is not None and feels >= 95:
            consider(70, "heat", feels, eta)
        if hour_gust is not None and hour_gust >= 40:
            consider(85, "gust", hour_gust, eta)
        elif hour_gust is not None and hour_gust >= 25:
            consider(65, "gust", hour_gust, eta)

        if code is not None and (56 <= code <= 57 or 66 <= code <= 67):
            consider(85, "ice", pop or 0, eta)
        elif code is not None and (71 <= code <= 77 or code in (85, 86)):
            consider(75, "snow", pop or 0, eta)
        elif pop is not None and pop >= 70:
            consider(80, "rain", pop, eta)
        elif pop is not None and pop >= 30:
            consider(55, "rain", pop, eta)
        if uv is not None and uv >= 8:
            consider(60, "uv", uv, eta)
        elif uv is not None and uv >= 6:
            consider(45, "uv", uv, eta)

    return best[1], best[2], 0 if best[1] == "" else best[3]


class WeatherP0Tests(unittest.TestCase):
    def test_wmo_snow_showers_are_snow(self) -> None:
        self.assertEqual(category(85), "snow")
        self.assertEqual(category(86), "snow")
        self.assertEqual(category(82), "rain")
        self.assertEqual(category(95), "storm")

    def test_quiet_conditions_hide_attention_page(self) -> None:
        quiet = [
            {"code": 0, "feels": 75, "gust": 4, "pop": 0, "uv": 0}
            for _ in range(6)
        ]
        self.assertEqual(choose_hazard(quiet, wind=3, gust=4), ("", 0, 0))

    def test_real_hazard_beats_calm_wind(self) -> None:
        hours = [
            {"code": 0, "feels": 96, "gust": 8, "pop": 0, "uv": 7},
            {"code": 61, "feels": 88, "gust": 15, "pop": 75, "uv": 2},
        ]
        self.assertEqual(choose_hazard(hours, wind=12, gust=15), ("rain", 75, 1))

    def test_hot_day_is_not_silently_hidden_overnight(self) -> None:
        quiet_night = [
            {"code": 0, "feels": 74, "gust": 4, "pop": 0, "uv": 0}
            for _ in range(6)
        ]
        self.assertEqual(
            choose_hazard(quiet_night, high=102, low=66, uv_max=8),
            ("heat", 102, 9),
        )

    def test_active_precipitation_beats_later_daily_warning(self) -> None:
        clearing = [
            {"code": 0, "feels": 75, "gust": 4, "pop": 0, "uv": 0}
            for _ in range(6)
        ]
        self.assertEqual(
            choose_hazard(clearing, high=102, uv_max=8, current_code=85),
            ("snow", 0, 0),
        )

    def test_severity_then_earliest(self) -> None:
        hours = [
            {"code": 0, "feels": 101, "gust": 42, "pop": 0, "uv": 9},
            {"code": 95, "feels": 80, "gust": 20, "pop": 80, "uv": 1},
        ]
        self.assertEqual(choose_hazard(hours), ("storm", 95, 1))

    def test_snapshot_fits_shared_budget(self) -> None:
        snapshot = {
            "t": 104,
            "c": 99,
            "d": 1,
            "hi": 112,
            "lo": -15,
            "dc": 99,
            "up": 1439,
            "dn": 1439,
            "hk": "storm",
            "hv": 123,
            "he": 9,
            "hx": 359,
        }
        payload = json.dumps(snapshot, separators=(",", ":"))
        self.assertLessEqual(len("data") + len(payload), 256)

    def test_scripts_fit_panel_source_limit(self) -> None:
        """Measured after minification, because that is what gets uploaded.

        Compiling on the panel needs roughly 2x the source in *contiguous*
        heap, so the uploaded size is the number that matters; the readable
        source in this folder can be as long as it needs to be.  Firmware
        allows 16384 per script and this guard keeps headroom under that.

        The real ceiling is the 98304-byte script heap shared by everything
        installed, which no per-file check can see - keep the total in view.
        """

        from minify_ax import minify

        for path in ROOT.glob("*.ax"):
            with self.subTest(path=path.name):
                shipped = len(
                    minify(path.read_text(encoding="utf-8")).encode("utf-8")
                )
                self.assertLessEqual(shipped, 12288)

    def test_pages_use_correct_semantics(self) -> None:
        now = (ROOT / "wxnow.ax").read_text(encoding="utf-8")
        today = (ROOT / "wxrange.ax").read_text(encoding="utf-8")
        sky = (ROOT / "wxsky.ax").read_text(encoding="utf-8")
        self.assertIn("wx.current_icon(code, isday", now)
        self.assertIn('d.find("dc")', today)
        self.assertIn("return false", sky)


if __name__ == "__main__":
    unittest.main(verbosity=2)
