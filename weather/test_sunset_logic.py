"""Deterministic checks for the Sunset page (v3).

The panel is the only real Berry compiler, so these tests protect the semantic
choices instead: the quality model, the descent curve, the three-stop colour
ramp, the readout choreography, and the layout guarantees.

Every helper here is an exact integer mirror of `sunset.ax`.  Berry's `int()`
truncates toward zero and so does Python's, so `int(a / b)` transfers directly.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "sunset.ax"

# Importable whether pytest is run from this folder or from the repo root.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# --- constants mirrored from sunset.ax ------------------------------------

KEYS = [
    -75, 18, 38, 96, 70, 96, 150, 170, 190, 215,
    -35, 22, 44, 110, 96, 100, 150, 250, 196, 120,
    -12, 34, 40, 108, 130, 84, 130, 255, 150, 72,
    0, 40, 30, 92, 150, 60, 105, 255, 102, 60,
    8, 34, 20, 74, 150, 45, 100, 255, 80, 96,
    18, 20, 12, 50, 95, 30, 78, 150, 45, 95,
    28, 6, 5, 22, 24, 14, 44, 40, 18, 48,
]
DECKS = [1, 11, 2600, 0, 4, 8, 4100, 19]
LEAD = 75
BASE = 5
CREAM = (255, 236, 205)
ALWAYS_SHOW = False
LOOP_LEAD = 60


def mix(a: int, b: int, k: int) -> int:
    return a + int((b - a) * k / 100)


def lum(c) -> int:
    return int((c[0] * 30 + c[1] * 59 + c[2] * 11) / 100)


# --- model ----------------------------------------------------------------


def quality(low: int, mid: int, high: int) -> int:
    """Mirror of Sunset.quality: how good the sunset will actually look."""

    if high <= 10:
        h = int(high * 15 / 10)
    elif high <= 40:
        h = 15 + int((high - 10) * 15 / 10)
    elif high <= 70:
        h = 60
    else:
        h = 60 - int((high - 70) * 8 / 10)

    m = int(mid * 3 / 10) if mid <= 50 else 15 - int((mid - 50) * 3 / 10)

    if low > 50:
        l = 24 + int((low - 50) * 16 / 10)
    elif low > 20:
        l = int((low - 20) * 8 / 10)
    else:
        l = 0

    return max(0, min(100, 32 + h + m - l))


def word(q: int) -> str:
    """Four letters, because the readout owns only half the panel."""

    if q < 0:
        return ""
    if q >= 75:
        return "EPIC"
    if q >= 50:
        return "GOOD"
    if q >= 30:
        return "FAIR"
    return "DULL"


def word_tint(q: int):
    if q >= 75:
        return (255, 120, 190)
    if q >= 50:
        return (255, 200, 100)
    if q >= 30:
        return (214, 214, 206)
    return (150, 156, 170)


def dusk_offset(daylen: int) -> int:
    """Civil twilight length, derived from day length instead of fetched."""

    return max(21, min(28, 21 + int((daylen - 600) / 30)))


def in_window(t: int, dusk: int) -> bool:
    """The window should_show() opens; inside it the page reminds on a timer."""
    if ALWAYS_SHOW:
        return True
    return -LOOP_LEAD <= t <= dusk


def phase(minute_of_day: int, sunrise: int, t: int, dusk: int) -> str:
    """Mirror of the shared "sunset.phase" that ocean and moon follow."""
    if minute_of_day < sunrise or t > dusk:
        return "night"
    if t < -LOOP_LEAD:
        return "day"
    return "sunset"


def flatten(c, amt):
    l = lum(c)
    return [mix(v, l, amt) for v in c]


def anchors(t: int, q: int):
    """Three stops now: top, middle, horizon."""

    n = len(KEYS) - 10
    i = 0
    while i < n and t > KEYS[i + 10]:
        i += 10
    j = i + 10
    if j > n:
        j = i
    span = KEYS[j] - KEYS[i]
    k = 0 if span <= 0 else int((t - KEYS[i]) * 100 / span)
    k = max(0, min(100, k))

    top = [mix(KEYS[i + 1 + c], KEYS[j + 1 + c], k) for c in range(3)]
    mid = [mix(KEYS[i + 4 + c], KEYS[j + 4 + c], k) for c in range(3)]
    hor = [mix(KEYS[i + 7 + c], KEYS[j + 7 + c], k) for c in range(3)]

    amt = 20 if q < 0 else int((100 - q) / 2)
    if amt > 0:
        top, mid, hor = flatten(top, amt), flatten(mid, amt), flatten(hor, amt)
    return top, mid, hor


def band(top, mid, hor, y: int, w: int, i: int) -> int:
    if y <= 4:
        return mix(top[i], mid[i], w)
    return mix(mid[i], hor[i], w)


def stop(top, mid, hor, y: int, w: int):
    return tuple(band(top, mid, hor, y, w, i) for i in range(3))


def row_color(top, mid, hor, y: int):
    """The row's base tone, ignoring the dither checkerboard."""

    w = y * 25 if y <= 4 else (y - 4) * 50
    return stop(top, mid, hor, y, w)


def ragged(x: int) -> int:
    """Table-free 1-bit edge noise, so a deck is not a rectangle."""

    h = x * 37 + 11
    return (h ^ (h >> 3)) & 1


DESC = [860, 577, 460, 370, 294, 228, 167, 112, 60]


def sun_pos(t: int) -> tuple[int, int]:
    u = max(0, min(LEAD, -t))
    f = int(u * 800 / LEAD)
    i = min(7, int(f / 100))
    k = f - i * 100
    cy100 = DESC[i] + int((DESC[i + 1] - DESC[i]) * k / 100)
    cx = int((200 + (LEAD - u) * 2500 / LEAD) / 100)
    cy = int((cy100 + 50) / 100)
    return cx, cy


def text_alpha(view_ms: int) -> int:
    """The sky owns the first five seconds of every nine-second appearance."""

    v = view_ms % 9000
    if 5000 <= v < 5500:
        return int((v - 5000) / 5)
    if 5500 <= v < 8400:
        return 100
    if 8400 <= v < 8900:
        return int((8900 - v) / 5)
    return 0


def clock(m: int) -> str:
    m %= 1440
    h, mm = int(m / 60), m % 60
    h %= 12
    if h == 0:
        h = 12
    return f"{h}:{mm:02d}"


def label_for(t: int, dusk: int, dn: int, q: int, ms: int):
    if t > 0:
        return f"{dusk - t}M", CREAM
    if t == 0:
        return "SET", CREAM
    slot = int(ms / 30000) % 3
    if slot == 0:
        return f"{-t}M", CREAM
    if slot == 1:
        return clock(dn), CREAM
    w = word(q)
    if w == "":
        return f"{-t}M", CREAM
    return w, word_tint(q)


def ink_width(s: str) -> int:
    """The preview font is fixed 3x5 on a 4px advance, like preview_moon.py."""

    return max(0, len(s) * 4 - 1)


# --- tests ----------------------------------------------------------------


class QualityModelTests(unittest.TestCase):
    def test_clear_sky_is_plain_not_great(self) -> None:
        self.assertEqual(quality(0, 0, 0), 32)
        self.assertEqual(word(quality(0, 0, 0)), "FAIR")

    def test_cirrus_is_the_best_case(self) -> None:
        self.assertEqual(quality(0, 0, 50), 92)
        self.assertEqual(word(quality(0, 0, 50)), "EPIC")

    def test_low_deck_kills_the_show(self) -> None:
        self.assertEqual(quality(90, 0, 0), 0)

    def test_a_real_low_deck_beats_good_cirrus(self) -> None:
        self.assertEqual(word(quality(85, 40, 55)), "DULL")

    def test_more_low_cloud_never_improves_the_score(self) -> None:
        for high in (0, 30, 55, 90):
            scores = [quality(low, 20, high) for low in range(0, 101, 5)]
            self.assertEqual(scores, sorted(scores, reverse=True))

    def test_score_stays_in_range_across_the_whole_domain(self) -> None:
        for low in range(0, 101, 10):
            for mid in range(0, 101, 10):
                for high in range(0, 101, 10):
                    self.assertTrue(0 <= quality(low, mid, high) <= 100)


class TwilightTests(unittest.TestCase):
    def test_dusk_offset_tracks_day_length(self) -> None:
        self.assertEqual(dusk_offset(585), 21)
        self.assertEqual(dusk_offset(870), 28)

    def test_page_is_only_offered_around_sunset(self) -> None:
        self.assertFalse(in_window(-61, 26))
        self.assertTrue(in_window(-60, 26))
        self.assertTrue(in_window(26, 26))
        self.assertFalse(in_window(27, 26))

    def test_phase_hands_the_day_to_ocean_and_the_night_to_moon(self) -> None:
        sunrise, sunset, dusk = 6 * 60 + 50, 18 * 60 + 27, 24
        def at(h, m):
            minute = h * 60 + m
            return phase(minute, sunrise, minute - sunset, dusk)
        self.assertEqual(at(6, 0), "night")
        self.assertEqual(at(12, 0), "day")
        self.assertEqual(at(17, 26), "day")
        self.assertEqual(at(17, 27), "sunset")
        self.assertEqual(at(18, 51), "sunset")
        self.assertEqual(at(18, 52), "night")
        # Every minute belongs to exactly one page.
        phases = {at(m // 60, m % 60) for m in range(1440)}
        self.assertEqual(phases, {"day", "sunset", "night"})


class DescentTests(unittest.TestCase):
    def test_descent_table_tracks_a_real_square_root(self) -> None:
        """The table replaces sqrt; it must still be the same curve."""

        import math
        for i, v in enumerate(DESC):
            expected = 860 - 800 * math.sqrt(i / 8)
            self.assertLessEqual(abs(v - expected), 2)

    def test_sun_starts_high_and_ends_under_the_land(self) -> None:
        self.assertLessEqual(sun_pos(-LEAD)[1], 1)
        self.assertGreaterEqual(sun_pos(0)[1], 8)

    def test_descent_is_monotonic(self) -> None:
        heights = [sun_pos(t)[1] for t in range(-LEAD, 1)]
        self.assertEqual(heights, sorted(heights))

    def test_the_disc_now_crosses_the_whole_panel(self) -> None:
        """v2 confined the sun to x0..12; v3 gives it the full width."""

        xs = [sun_pos(t)[0] for t in range(-LEAD, 1)]
        self.assertEqual(xs, sorted(xs))
        self.assertEqual(xs[0], 2)
        self.assertGreaterEqual(xs[-1], 26)

    def test_sun_is_gone_once_the_sun_has_set(self) -> None:
        for t in range(0, 30):
            self.assertGreaterEqual(sun_pos(t)[1], 8)


class SkyTests(unittest.TestCase):
    def test_horizon_row_is_always_the_brightest(self) -> None:
        for t in range(-LEAD, 29):
            top, mid, hor = anchors(t, 70)
            rows = [lum(row_color(top, mid, hor, y)) for y in range(7)]
            self.assertEqual(max(rows), rows[6], f"row 6 lost the burn at t={t}")

    def test_sky_darkens_overall_through_the_window(self) -> None:
        first = sum(lum(row_color(*anchors(-70, 70), y)) for y in range(7))
        last = sum(lum(row_color(*anchors(28, 70), y)) for y in range(7))
        self.assertLess(last, first / 3)

    def test_middle_stop_is_not_just_the_average(self) -> None:
        """The whole point of v3: rows 1-5 must not be a straight top-to-horizon
        ramp, which is what made v2 read as a flat slab."""

        worst = 0
        for t in (-35, -12, 0, 8):
            top, mid, hor = anchors(t, 90)
            straight = [mix(top[i], hor[i], 50) for i in range(3)]
            worst = max(worst, max(abs(mid[i] - straight[i]) for i in range(3)))
        self.assertGreater(worst, 25, "middle stop adds no colour of its own")

    def test_dither_produces_two_distinct_tones_per_row(self) -> None:
        top, mid, hor = anchors(-20, 90)
        for y in range(7):
            w = y * 25 if y <= 4 else (y - 4) * 50
            a = stop(top, mid, hor, y, max(0, w - 7))
            b = stop(top, mid, hor, y, min(100, w + 7))
            self.assertNotEqual(a, b, f"row {y} dithers to a single tone")

    def test_poor_forecast_renders_a_flatter_sky(self) -> None:
        def spread(q: int) -> int:
            _, _, hor = anchors(0, q)
            return max(hor) - min(hor)

        self.assertLess(spread(10), spread(95))


class ChoreographyTests(unittest.TestCase):
    """v3's central bet: the sky owns the frame, the readout visits."""

    def test_first_five_seconds_are_pure_sky(self) -> None:
        for v in range(0, 5000, 250):
            self.assertEqual(text_alpha(v), 0, f"text visible at {v}ms")

    def test_readout_reaches_full_strength_and_leaves(self) -> None:
        self.assertEqual(text_alpha(5000), 0)
        self.assertEqual(text_alpha(5500), 100)
        self.assertEqual(text_alpha(8000), 100)
        self.assertEqual(text_alpha(8900), 0)

    def test_fade_is_monotonic_in_and_out(self) -> None:
        rising = [text_alpha(v) for v in range(5000, 5500, 50)]
        falling = [text_alpha(v) for v in range(8400, 8900, 50)]
        self.assertEqual(rising, sorted(rising))
        self.assertEqual(falling, sorted(falling, reverse=True))

    def test_sky_is_visible_for_most_of_the_appearance(self) -> None:
        hidden = sum(1 for v in range(0, 9000, 10) if text_alpha(v) == 0)
        self.assertGreater(hidden / 900, 0.55, "readout is on screen too long")

    def test_cycle_repeats_if_on_show_is_missed(self) -> None:
        self.assertEqual(text_alpha(9000 + 6000), text_alpha(6000))


class LabelTests(unittest.TestCase):
    def test_countdown_clock_and_verdict_rotate(self) -> None:
        self.assertEqual(label_for(-42, 26, 1171, 92, 0)[0], "42M")
        self.assertEqual(label_for(-42, 26, 1171, 92, 30000)[0], "7:31")
        self.assertEqual(label_for(-42, 26, 1171, 92, 60000)[0], "EPIC")

    def test_the_moment_itself_is_named(self) -> None:
        self.assertEqual(label_for(0, 26, 1171, 50, 0)[0], "SET")

    def test_after_sunset_counts_down_to_dark(self) -> None:
        self.assertEqual(label_for(8, 26, 1171, 50, 0)[0], "18M")

    def test_unscored_sky_falls_back_to_the_countdown(self) -> None:
        self.assertEqual(label_for(-42, 26, 1171, -1, 60000)[0], "42M")

    def test_every_label_fits_the_readout_window(self) -> None:
        for t in range(-LEAD, 29):
            for ms in (0, 30000, 60000):
                for q in (-1, 0, 35, 60, 92, 100):
                    s, _ = label_for(t, 26, 1171, q, ms)
                    self.assertLessEqual(ink_width(s), 20, f"{s!r} overflows at t={t}")

    def test_labels_stay_inside_the_panel(self) -> None:
        for s in ("EPIC", "7:31", "75M", "SET", "28M"):
            x = max(12, 31 - ink_width(s))
            self.assertLessEqual(x + ink_width(s), 32)


class BudgetTests(unittest.TestCase):
    def test_rotation_cadence_is_sparse_early_and_two_minutes_near_sunset(self) -> None:
        page = SOURCE.read_text(encoding="utf-8")
        self.assertIn("var LOOP_LEAD = 60", page)
        self.assertIn("var URGENT_LEAD = 15", page)
        self.assertIn("var EARLY_REMINDER_MS = 600000", page)
        self.assertIn("var URGENT_REMINDER_MS = 120000", page)
        self.assertIn("self.last_shown = self.shown", page)
        self.assertIn("now_ms() - self.last_shown >= reminder", page)

    def test_sunset_script_fits_the_panel_source_limit(self) -> None:
        from minify_ax import minify

        shipped = len(minify(SOURCE.read_text(encoding="utf-8")).encode("utf-8"))
        self.assertLessEqual(shipped, 12288)

    def test_page_is_self_contained(self) -> None:
        """v2 dropped wx and wxfeed: three scripts did not fit the heap."""

        page = SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("import wx", page)
        self.assertNotIn("wx.", page)
        self.assertIn("http.get(URL", page)
        for field in ("cloud_cover_low", "cloud_cover_mid", "cloud_cover_high"):
            self.assertIn(field, page)

    def test_feed_was_left_unmodified(self) -> None:
        feed = (ROOT / "wxfeed.ax").read_text(encoding="utf-8")
        for field in ("cloud_cover_low", "cloud_cover_mid", "cloud_cover_high"):
            self.assertNotIn(field, feed)

    def test_page_hides_itself_outside_the_window(self) -> None:
        page = SOURCE.read_text(encoding="utf-8")
        self.assertIn("def should_show()", page)
        self.assertIn("def duration()", page)
        self.assertIn("var ALWAYS_SHOW = false", page)
        self.assertIn('shared.set("phase", p)', page)

    def test_fetch_avoids_tls(self) -> None:
        """Five scripts loaded means no heap for a TLS handshake."""

        page = SOURCE.read_text(encoding="utf-8")
        self.assertIn('"http://api.open-meteo.com', page)
        self.assertNotIn("https://api.open-meteo.com", page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
