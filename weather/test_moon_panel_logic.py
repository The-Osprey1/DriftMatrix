"""Accuracy checks for the panel's Moon page.

`moon-panel.ax` is the program that runs on the device, and it is a different
program from `moon.ax` in this folder.  These tests mirror its astronomy
exactly and check it against `astropy`, which is an independent ephemeris
rather than a restatement of the same formulae.

v1 counted synodic months from a fixed epoch and was wrong by up to 13 points
of illuminated fraction; its epoch constant was also half a day out, because a
Julian Day fraction was used without removing JD's noon offset.  v2 uses the
Meeus ch.48 phase angle and holds to about 0.4 points.
"""

from __future__ import annotations

import math
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "moon-panel.ax"

try:
    import numpy as np
    from astropy.time import Time, TimeDelta
    from astropy.coordinates import get_body, solar_system_ephemeris
    HAVE_ASTROPY = True
except Exception:  # pragma: no cover - environment without astropy
    HAVE_ASTROPY = False


# --- exact mirror of moon-panel.ax ----------------------------------------


def sn(x: float) -> float:
    """Bhaskara sine on turns: sn(x) == sin(2*pi*x)."""

    x = x - int(x)
    if x < 0.0:
        x += 1.0
    negate = False
    if x > 0.5:
        x -= 0.5
        negate = True
    f = x * 2.0
    u = f * (1.0 - f)
    v = 16.0 * u / (5.0 - 4.0 * u)
    return -v if negate else v


def cs(x: float) -> float:
    return sn(x + 0.25)


def phase(unix_days: float) -> dict:
    """Mirror of Moon.compute_phase."""

    T = (unix_days - 10957.5) / 36525.0
    D = 297.8501921 + 445267.1114034 * T
    M = 357.5291092 + 35999.0502909 * T
    Mp = 134.9633964 + 477198.8675055 * T
    F = 93.2720950 + 483202.0175233 * T

    i = 180.0 - D
    i -= 6.289 * sn(Mp / 360.0)
    i += 2.100 * sn(M / 360.0)
    i -= 1.274 * sn((2.0 * D - Mp) / 360.0)
    i -= 0.658 * sn(D / 180.0)
    i -= 0.214 * sn(Mp / 180.0)
    i -= 0.110 * sn(D / 360.0)

    k = min(1.0, max(0.0, (1.0 + cs(i / 360.0)) / 2.0))
    illum = int(k * 100.0 + 0.5)

    dd = D / 360.0
    dd -= int(dd)
    if dd < 0.0:
        dd += 1.0

    ecl = 0
    if illum >= 99:
        sf = abs(sn(F / 360.0))
        if sf < 0.36:
            ecl = min(100, int((0.36 - sf) * 278.0))

    return {"illum": illum, "wax": dd < 0.5, "phf": dd, "ecl": ecl}


def name_phase(illum: int, ecl: int) -> str:
    if ecl > 0:
        return "ECL"
    if illum <= 1:
        return "NEW"
    if illum < 49:
        return "CRES"
    if illum <= 51:
        return "QRTR"
    if illum < 99:
        return "GIBB"
    return "FULL"


def legacy_illum(unix_days: float) -> int:
    """v1's model, kept so the regression it fixed stays visible."""

    syn = 29.530588853
    d = unix_days - 10962.26
    cd = d + 0.25 * sn(d / 27.55455)
    a = cd - int(cd / syn) * syn
    if a < 0:
        a += syn
    f = a / syn
    u = f * (1.0 - f)
    s = 16.0 * u / (5.0 - 4.0 * u)
    return int(s * s * 100.0 + 0.5)


def unix_days(y: int, m: int, d: int, h: int = 0) -> float:
    import datetime

    delta = datetime.datetime(y, m, d, h) - datetime.datetime(1970, 1, 1)
    return delta.total_seconds() / 86400.0


# --- tests ----------------------------------------------------------------


@unittest.skipUnless(HAVE_ASTROPY, "astropy not installed")
class AccuracyTests(unittest.TestCase):
    """Compared against astropy's ephemeris, not against our own formulae."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.times = Time("2026-01-01") + TimeDelta(np.arange(0, 730, 1), format="jd")
        cls.ud = cls.times.unix / 86400.0
        with solar_system_ephemeris.set("builtin"):
            sun = get_body("sun", cls.times)
            moon = get_body("moon", cls.times)
        elong = moon.separation(sun).radian
        cls.reference = (1 - np.cos(elong)) / 2 * 100

    def errors(self, model) -> np.ndarray:
        return np.array([model(u) - r for u, r in zip(self.ud, self.reference)])

    def test_illumination_is_within_one_point_over_two_years(self) -> None:
        err = np.abs(self.errors(lambda u: phase(u)["illum"]))
        self.assertLess(err.max(), 1.0, f"max error {err.max():.2f} points")
        self.assertLess(err.mean(), 0.5)

    def test_v2_is_a_large_improvement_on_v1(self) -> None:
        new = np.abs(self.errors(lambda u: phase(u)["illum"])).max()
        old = np.abs(self.errors(legacy_illum)).max()
        self.assertGreater(old, 10.0, "v1 should still look bad")
        self.assertLess(new * 10, old, "v2 should be at least 10x better")

    def test_waxing_flag_matches_a_rising_illumination(self) -> None:
        for u in self.ud[:400]:
            here = phase(u)
            later = phase(u + 1.0)
            # Skip the two turning points, where the direction genuinely flips.
            if here["illum"] in (0, 100) or later["illum"] in (0, 100):
                continue
            if here["illum"] == later["illum"]:
                continue
            self.assertEqual(
                here["wax"],
                later["illum"] > here["illum"],
                f"waxing flag disagrees with illumination trend at {u}",
            )


class EclipseTests(unittest.TestCase):
    """The v1 node test never fired once in two years, including on real ones."""

    REAL = [(2026, 3, 3, 11), (2026, 8, 28, 4), (2027, 2, 20, 23), (2027, 8, 17, 7)]

    def test_every_real_lunar_eclipse_is_flagged(self) -> None:
        for y, m, d, h in self.REAL:
            with self.subTest(date=f"{y}-{m:02d}-{d:02d}"):
                self.assertGreater(phase(unix_days(y, m, d, h))["ecl"], 0)

    def test_the_deepest_flag_is_the_total_eclipse(self) -> None:
        depths = {f"{y}-{m:02d}": phase(unix_days(y, m, d, h))["ecl"] for y, m, d, h in self.REAL}
        self.assertEqual(max(depths, key=depths.get), "2026-03")

    def test_ordinary_full_moons_are_not_flagged(self) -> None:
        quiet = [(2026, 1, 3), (2026, 5, 1), (2026, 5, 30), (2026, 6, 29)]
        for y, m, d in quiet:
            with self.subTest(date=f"{y}-{m:02d}-{d:02d}"):
                self.assertEqual(phase(unix_days(y, m, d, 12))["ecl"], 0)

    def test_eclipse_requires_a_full_moon(self) -> None:
        for u in (unix_days(2026, 3, 3, 11) - 7, unix_days(2026, 3, 3, 11) - 14):
            self.assertEqual(phase(u)["ecl"], 0)


class NamingTests(unittest.TestCase):
    def test_names_cover_the_whole_range(self) -> None:
        self.assertEqual(name_phase(0, 0), "NEW")
        self.assertEqual(name_phase(1, 0), "NEW")
        self.assertEqual(name_phase(2, 0), "CRES")
        self.assertEqual(name_phase(48, 0), "CRES")
        self.assertEqual(name_phase(50, 0), "QRTR")
        self.assertEqual(name_phase(52, 0), "GIBB")
        self.assertEqual(name_phase(98, 0), "GIBB")
        self.assertEqual(name_phase(100, 0), "FULL")

    def test_an_eclipse_outranks_the_phase_name(self) -> None:
        self.assertEqual(name_phase(100, 60), "ECL")

    def test_names_fit_the_label_slot(self) -> None:
        for illum in range(0, 101):
            self.assertLessEqual(len(name_phase(illum, 0)), 4)


HORIZON_ALT = 0.30
HORIZON_WARM = 70


def horizon_warmth(phf: float, hour_of_day: float) -> int:
    """Mirror of Moon.compute_position's warmth term.

    Altitude is estimated as cos(hour angle): 1 at transit, 0 at the horizon,
    negative below it.
    """

    transit = 12.0 + phf * 24.0
    while transit >= 24.0:
        transit -= 24.0
    h = hour_of_day - transit
    while h > 12.0:
        h -= 24.0
    while h < -12.0:
        h += 24.0
    altitude = math.cos(2 * math.pi * h / 24.0)
    if 0.0 <= altitude < HORIZON_ALT:
        return int((HORIZON_ALT - altitude) * HORIZON_WARM / HORIZON_ALT)
    return 0


def legacy_warmth(phf: float, hour_of_day: float) -> int:
    """The rule this replaced, kept so the regression stays visible."""

    transit = 12.0 + phf * 24.0
    while transit >= 24.0:
        transit -= 24.0
    h = hour_of_day - transit
    while h > 12.0:
        h -= 24.0
    while h < -12.0:
        h += 24.0
    am = min(1.0, abs(h) / 6.0)
    return int(am * am * 100)


class HorizonColourTests(unittest.TestCase):
    """Red on this page must mean an eclipse and nothing else.

    The old rule blended toward fire-red on distance from transit, saturating
    six hours out - which is roughly where the moon sets.  Full red was reached
    AT the horizon and stayed pinned there below it, so every phase that
    transits near noon or dawn rendered solid red all evening.
    """

    EVENING = (18.0, 19.0, 20.0, 21.0, 21.5)

    def test_a_moon_below_the_horizon_is_never_warmed(self) -> None:
        for phf in (0.0, 0.75, 0.88):  # new, last quarter, waning crescent
            for hour in self.EVENING:
                if self._altitude(phf, hour) < 0:
                    with self.subTest(phf=phf, hour=hour):
                        self.assertEqual(horizon_warmth(phf, hour), 0)

    def test_the_old_rule_reddened_those_same_moments_completely(self) -> None:
        offenders = [
            (phf, hour)
            for phf in (0.0, 0.75, 0.88)
            for hour in self.EVENING
            if self._altitude(phf, hour) < 0
        ]
        self.assertTrue(offenders, "expected some below-horizon evening moments")
        for phf, hour in offenders:
            self.assertEqual(legacy_warmth(phf, hour), 100)

    def test_a_high_moon_is_not_warmed(self) -> None:
        for phf in (0.25, 0.38):  # first quarter, waxing gibbous
            for hour in self.EVENING:
                if self._altitude(phf, hour) >= HORIZON_ALT:
                    with self.subTest(phf=phf, hour=hour):
                        self.assertEqual(horizon_warmth(phf, hour), 0)

    def test_a_low_but_visible_moon_is_warmed(self) -> None:
        warmed = [
            horizon_warmth(phf, hour)
            for phf in (0.0, 0.5, 0.62)
            for hour in self.EVENING
            if 0.0 <= self._altitude(phf, hour) < HORIZON_ALT
        ]
        self.assertTrue(warmed, "expected some low-but-up moments")
        self.assertTrue(all(0 < w <= HORIZON_WARM for w in warmed))

    def test_warmth_never_reaches_full_saturation(self) -> None:
        every = [
            horizon_warmth(p / 100.0, hour / 2.0)
            for p in range(100)
            for hour in range(48)
        ]
        self.assertLessEqual(max(every), HORIZON_WARM)
        self.assertLess(max(every), 100, "full blend is reserved for eclipses")

    def test_warmth_rises_monotonically_as_the_moon_sets(self) -> None:
        """Approaching the horizon should only ever add warmth."""

        series = []
        for step in range(30):
            hour = 12.0 + 4.0 + step * (2.0 / 30.0)  # transit+4h to transit+6h
            series.append(horizon_warmth(0.0, hour))
        self.assertEqual(series, sorted(series))

    @staticmethod
    def _altitude(phf: float, hour_of_day: float) -> float:
        transit = 12.0 + phf * 24.0
        while transit >= 24.0:
            transit -= 24.0
        h = hour_of_day - transit
        while h > 12.0:
            h -= 24.0
        while h < -12.0:
            h += 24.0
        return math.cos(2 * math.pi * h / 24.0)


class SourceTests(unittest.TestCase):
    def test_red_is_reserved_for_eclipses(self) -> None:
        body = SOURCE.read_text(encoding="utf-8")
        self.assertIn("var HORIZON_TINT = 0xE8A24C", body)
        self.assertIn("var HORIZON_WARM = 70", body)
        # The fire-red the horizon tint used to blend toward is gone entirely.
        self.assertNotIn("0xE65C2B", body)
        # Blood red survives, but only on the eclipse path.
        self.assertIn("0x8B2500", body)
        eclipse = body.index("0x8B2500")
        self.assertIn("self.ecl", body[max(0, eclipse - 200) : eclipse])

    def test_warmth_is_gated_on_altitude_not_distance_from_transit(self) -> None:
        body = SOURCE.read_text(encoding="utf-8")
        self.assertIn("self.cs(h / 24.0)", body)
        self.assertRegex(body, r"alt\s*>=\s*0\.0\s*&&\s*alt\s*<\s*HORIZON_ALT")

    def test_page_is_always_available_once_the_clock_is_valid(self) -> None:
        body = SOURCE.read_text(encoding="utf-8")
        self.assertIn("var MOON_START_MINUTE = 1080", body)
        self.assertIn("var MOON_END_MINUTE = 1290", body)
        self.assertIn("var MOON_REMINDER_MS = 900000", body)
        self.assertIn("var ALWAYS_SHOW = true", body)
        self.assertIn("self.last_shown = self.shown", body)
        self.assertIn("now_ms() - self.last_shown >= MOON_REMINDER_MS", body)

    def test_no_sentinel_the_clock_can_return(self) -> None:
        """hour() returns -1 before NTP sync; -1 as an 'unset' marker crashes."""

        body = SOURCE.read_text(encoding="utf-8")
        self.assertIn("self.lh = -99", body)
        self.assertNotIn("self.lh = -1\n", body)

    def test_tint_is_assigned_unconditionally(self) -> None:
        """v1 assigned ct only inside update_hour_tint and died on nil."""

        body = SOURCE.read_text(encoding="utf-8")
        init = body.split("def init()")[1].split("def on_show")[0]
        self.assertIn("self.ct = [", init)

    def test_page_hides_itself_without_a_clock(self) -> None:
        body = SOURCE.read_text(encoding="utf-8")
        self.assertIn("if !self.valid return false end", body)
        self.assertIn("if year() < 2020 return nil end", body)

    def test_dead_fields_are_gone(self) -> None:
        body = SOURCE.read_text(encoding="utf-8")
        for dead in ("self.prv", "self.up ", "self.eboot", "self.edays"):
            self.assertNotIn(dead, body, f"{dead} was unused in v1")

    def test_fits_the_panel_source_limit(self) -> None:
        self.assertLessEqual(SOURCE.stat().st_size, 16384)


if __name__ == "__main__":
    unittest.main(verbosity=2)
