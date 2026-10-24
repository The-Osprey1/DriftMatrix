"""v2.1 of the Moon page (``moon-panel-next.ax``) against v2.0 (``moon-panel.ax``).

v2.1 is a pure speed-up: it has to draw exactly what v2.0 draws.  Four lines
of evidence, each independent of the others' mistakes:

1. Live frames.  Four ``/display/screen`` captures of the installed v2.0 are
   reproduced pixel for pixel, glyphs included, by the v2.0 emulator, by the
   v2.1 mirror, and by both ``.ax`` files run through ``berry_lite``.
2. Identity.  v2.0 and v2.1 agree frame for frame over every lunar state,
   every camera offset, the nebula at every drift position, the shooting
   star, real dates (eclipses included) and timelines across the boundaries
   where v2.1's caches are rebuilt.
3. The sources themselves.  ``berry_lite`` compiles the exact ``.ax`` text,
   so the hand mirrors are checked against what will be uploaded, and the
   uploaded v2.1 against the installed v2.0 directly.
4. Static guards.  Unchanged methods are byte-identical, the packed layouts
   the Berry spells match the mirror's constants, every field fits its bits,
   and the minified bytes are pinned.

What none of it can prove is a firmware fact the emulation assumes.  The ones
v2.1 leans on and v2.0 did not: ``line()`` (checked against a live ocean
frame below) and nothing else -- v2.1 no longer relies on ``resize()``
zero-filling (the build investigated on 2026-10-09 did) or on ``list.resize``.
Upstream Berry does zero-fill, so that build would most likely have drawn
correctly on the panel; the staged decode writes every byte, so it is correct
whichever way this firmware behaves.

Most identity frames compare the two Python mirrors.  ``RandomisedSourceTests``
compares the exact upload bytes (both minified ``.ax`` files run through
``berry_lite``) under random clocks, so the claim also rests on the sources.
v2.1's Berry has still never executed on the panel itself.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import inspect
import json
import os
import random
import re
import struct
import sys
import unittest
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import moon_panel_sim as sim  # noqa: E402
from minify_ax import minify  # noqa: E402

INSTALLED = ROOT / "moon-panel-v2.0.ax"  # v2.0, the moon until 2026-10-10
STAGED = ROOT / "moon-panel.ax"  # v2.1, installed on the new board 2026-10-10
CAPTURES = ROOT / "moon_panel_captures.json"
OCEAN = ROOT.parent / "ocean.ax"

# The panel's moon since 2026-10-06 (panel_snapshot rebuilds it from moon-panel.ax).
INSTALLED_MIN_BYTES = 10574
INSTALLED_MIN_SHA256 = "f6b17ba2500719b088241d61847df5d28a9d10f66bd9a6a4f5b72ce58d7d86cf"
# What to upload.  Any edit to moon-panel-next.ax must re-run this file and re-pin.
STAGED_MIN_BYTES = 10918
STAGED_MIN_SHA256 = "90cd90e057a969d3011d17c3d8b6e5b35d838c5be28f9967237cab9d1f9f0669"
# The build investigated on 2026-10-09, before the two fixes below.
INVESTIGATED_MIN_SHA256 = "ae0766a6c6fbf9b2cb782320a5083923cdfd34802a47fb7f24454dbff32079cb"
FIXES = (
    # (as staged, as investigated) in minified text
    ("if v == 0xFF\nvar e = p + z[k + 1]\nwhile p < e\nnb[p] = 0\np += 1\nend\nk += 2\n",
     "if v == 0xFF\np += z[k + 1]\nk += 2\n"),
    ("self.nr = []\nk = 0\nwhile k < 64\nself.nr.push(0)\nk += 1\nend\n",
     "self.nr = []\nself.nr.resize(64)\n"),
)

FIXTURE = json.loads(CAPTURES.read_text(encoding="utf-8"))
FONT = sim.Font(FIXTURE["glyphs"])
PDT = dt.timezone(dt.timedelta(hours=-7))

UNCHANGED = {
    "on_show", "sn", "cs", "mix", "fade_pixel", "days", "name_phase", "update_hour_tint",
    "shade", "build_pal", "loop", "should_show", "duration", "draw", "draw_shooting_star",
}
CHANGED = {"init", "compute_phase", "compute_position", "build_map", "draw_nebula",
           "draw_stars", "draw_moon_body", "draw_labels"}
ADDED = {"build_disc"}

# (hour, minute) -> dim 100, 57 (morning ramp) and 15 (night)
DIMS = ((12, 0), (6, 15), (22, 0))
EPOCH = 1_791_565_704_545  # 2026-10-09, the capture day


def _cap_render(page_factory, cap):
    i = cap["inputs"]
    clock = sim.Clock(i["shown_ms"], i["epoch_ms"], i["year"], i["hour"], i["minute"])
    page = page_factory(clock)
    page.on_show()
    clock.now_ms = i["loop_ms"]
    page.loop()
    clock.now_ms = i["draw_ms"]
    return page.draw()


def _offsets():
    """One camera value for every distinct moon x offset ``bx`` (both glides)."""

    probe = sim.MoonV20(sim.Clock())
    seen: dict[int, object] = {}
    for c in list(range(3800, 4900)) + list(range(8800, 9900)):
        cam = probe.camera(c)
        bx = int(sim.R(12.0) - cam + sim.R(0.5))
        seen.setdefault(bx, cam)
    return seen


OFFSETS = _offsets()
# Camera positions that matter for the disc: every offset where the left fade
# band clips it, plus the plain, edge and gone cases.
BODY_OFFSETS = sorted(bx for bx in OFFSETS if bx >= -11 or bx == min(OFFSETS))


def _pair(phase, hour=12, minute=0, now=40_000_000, epoch=EPOCH, font=None):
    clocks = (sim.Clock(now, epoch, 2026, hour, minute), sim.Clock(now, epoch, 2026, hour, minute))
    a = sim.MoonV20(clocks[0], phase=phase, font=font)
    b = sim.MoonV21(clocks[1], phase=phase, font=font)
    return a, b, clocks


class LiveCaptureTests(unittest.TestCase):
    """The panel itself, 2026-10-09: GET /display/screen of the installed v2.0."""

    def test_v20_emulator_reproduces_every_live_frame(self) -> None:
        for n, cap in enumerate(FIXTURE["captures"]):
            with self.subTest(capture=n):
                frame = _cap_render(lambda c: sim.MoonV20(c, font=FONT), cap)
                self.assertEqual(list(frame.pixels), cap["pixels"])

    def test_v21_mirror_reproduces_every_live_frame(self) -> None:
        for n, cap in enumerate(FIXTURE["captures"]):
            with self.subTest(capture=n):
                frame = _cap_render(lambda c: sim.MoonV21(c, font=FONT), cap)
                self.assertEqual(list(frame.pixels), cap["pixels"])

    def test_both_sources_reproduce_every_live_frame(self) -> None:
        for path in (INSTALLED, STAGED):
            for n, cap in enumerate(FIXTURE["captures"]):
                with self.subTest(source=path.name, capture=n):
                    frame = _cap_render(lambda c: sim.BerryMoon(path, c, font=FONT), cap)
                    self.assertEqual(list(frame.pixels), cap["pixels"])

    def test_the_captures_pin_the_clock_to_a_few_milliseconds(self) -> None:
        """Every draw time in the fitted range fits; one step outside it does not."""

        for n, cap in enumerate(FIXTURE["captures"]):
            i = cap["inputs"]
            lo, hi = i["draw_ms_range"]
            self.assertLessEqual(hi - lo, 20)
            for td in range(lo - 1, hi + 2):
                frame = sim.render("2.0", td, epoch_ms=i["epoch_ms"], hour=i["hour"], minute=i["minute"],
                                   year=i["year"], loop_ms=min(i["loop_ms"], td), shown_ms=i["shown_ms"], font=FONT)
                with self.subTest(capture=n, draw_ms=td):
                    self.assertEqual(list(frame.pixels) == cap["pixels"], lo <= td <= hi)

    def test_the_fitted_inputs_hang_together(self) -> None:
        caps = FIXTURE["captures"]
        self.assertEqual(len({c["inputs"]["shown_ms"] for c in caps}), 1, "one on_show explains all four")
        first = caps[0]
        for cap in caps:
            i = cap["inputs"]
            local = dt.datetime.fromtimestamp(cap["wall_time"], PDT)
            self.assertEqual((i["year"], i["hour"], i["minute"]), (local.year, local.hour, local.minute))
            self.assertEqual(i["epoch_ms"], round(cap["wall_time"] * 1000))
            self.assertGreaterEqual(i["draw_ms"] - i["loop_ms"], 0)
            self.assertLess(i["draw_ms"] - i["loop_ms"], 1500)
            wall = (cap["wall_time"] - first["wall_time"]) * 1000
            self.assertLess(abs((i["draw_ms"] - first["inputs"]["draw_ms"]) - wall), 30,
                            "the panel clock and the PC clock must advance together")
        phases = [sim.tmod(c["inputs"]["draw_ms"] - c["inputs"]["shown_ms"], 11000) for c in caps]
        self.assertTrue(phases[0] < 3800 and phases[1] < 3800, "first two on the disc")
        self.assertTrue(all(4900 <= c < 8800 for c in phases[2:]), "last two on the info screen")

    def test_the_fixture_keeps_only_frames_timestamps_and_inputs(self) -> None:
        self.assertEqual(set(FIXTURE), {"about", "glyphs", "captures", "line_evidence"})
        for cap in FIXTURE["captures"]:
            self.assertEqual(set(cap), {"wall_time", "inputs", "pixels"})
            self.assertEqual(set(cap["inputs"]), {"epoch_ms", "year", "hour", "minute", "draw_ms",
                                                  "loop_ms", "shown_ms", "draw_ms_range"})
            self.assertEqual(len(cap["pixels"]), 256)
        self.assertEqual(set(FIXTURE["line_evidence"]), {"wall_time", "source", "pixels"})

    @unittest.skipUnless(OCEAN.exists(), "ocean.ax not beside the weather folder")
    def test_line_paints_both_endpoints_in_exact_24_bit_colour(self) -> None:
        """v2.1 draws runs with line(); v2.0 used only pixel().

        Ocean paints every row with line(0, y, 31, y, w[y]) before anything
        else, so wherever nothing covers them, x=0 and x=31 read back as w[y]:
        both endpoints inclusive, and no RGB565 rounding.
        """

        w = [int(v, 16) for v in re.search(r"self\.w=\[([^\]]*)\]", OCEAN.read_text(encoding="utf-8")).group(1).replace("0x", "").split(",")]
        px = FIXTURE["line_evidence"]["pixels"]
        for y in range(5):
            row = px[y * 32:(y + 1) * 32]
            with self.subTest(row=y):
                self.assertEqual(row[0], w[y])
                self.assertEqual(row[31], w[y])
                self.assertGreaterEqual(row.count(w[y]), 12)

        def through_rgb565(c: int, replicate: bool) -> int:
            r5, g6, b5 = (c >> 19) & 0x1F, (c >> 10) & 0x3F, (c >> 3) & 0x1F
            if replicate:
                return (r5 << 3 | r5 >> 2) << 16 | (g6 << 2 | g6 >> 4) << 8 | (b5 << 3 | b5 >> 2)
            return r5 << 19 | g6 << 10 | b5 << 3

        for replicate in (False, True):
            with self.subTest(rgb565_expansion="replicated" if replicate else "zero-filled"):
                self.assertTrue(any(through_rgb565(c, replicate) != c for c in w[:5]),
                                "these colours would have come back changed through RGB565")


class IdentityTests(unittest.TestCase):
    """v2.0 and v2.1, frame for frame."""

    STATES = (
        [(illum, 0.3, 0) for illum in (0, 1, 2, 5, 12, 19, 20, 21, 30, 39, 40, 41, 48, 49, 50, 51, 52, 75, 95, 96, 98, 99, 100)]
        + [(illum, 0.8, 0) for illum in (0, 1, 2, 5, 12, 19, 20, 21, 30, 39, 40, 41, 48, 49, 50, 51, 52, 75, 95, 96, 98, 99, 100)]
        + [(99, 0.5, 1), (100, 0.5, 30), (100, 0.5, 100), (99, 0.49, 57), (100, 0.52, 30)]
    )

    def assertSameFrame(self, a, b, msg: str) -> None:
        if a != b:
            diff = [(k % 32, k // 32, x, y) for k, (x, y) in enumerate(zip(a.pixels, b.pixels)) if x != y]
            self.fail(f"{msg}: {len(diff)} pixels differ, first {diff[:4]}; texts {a.texts} vs {b.texts}")

    def test_every_lunar_state_at_every_camera_offset(self) -> None:
        cams = sorted({0, 2500, 3799, 4900, 6000, 8799, 9900, 10999})
        glide = {}
        probe = sim.MoonV20(sim.Clock())
        for c in list(range(3800, 4900)) + list(range(8800, 9900)):
            bx = int(sim.R(12.0) - probe.camera(c) + sim.R(0.5))
            glide.setdefault((bx, c >= 8800), c)
        frames = 0
        for phase in self.STATES:
            a, b, clocks = _pair(phase)
            for p in (a, b):
                p.on_show()
            base = a.shown
            for c in cams + sorted(glide.values()):
                for clk in clocks:
                    clk.now_ms = base + c
                self.assertSameFrame(a.draw(), b.draw(), f"state {phase} c={c}")
                frames += 1
        self.assertGreater(frames, 3000)

    def test_every_dim_level(self) -> None:
        for phase in self.STATES[::3]:
            for hour, minute in DIMS:
                a, b, clocks = _pair(phase, hour, minute)
                for p in (a, b):
                    p.on_show()
                for c in (0, 4200, 6000, 9300):
                    for clk in clocks:
                        clk.now_ms = a.shown + c
                    self.assertSameFrame(a.draw(), b.draw(), f"state {phase} at {hour}:{minute:02d} c={c}")
        dims = set()
        for hour, minute in DIMS:
            page = sim.MoonV20(sim.Clock(0, EPOCH, 2026, hour, minute))
            page.loop()
            dims.add(page.dim)
        self.assertEqual(dims, {100, 57, 15})

    def test_disc_for_every_illumination_and_offset(self) -> None:
        """The disc alone, exhaustively: illum 0..100, both limbs, eclipse depths."""

        phases = [(illum, phf, 0) for illum in range(101) for phf in (0.3, 0.8)]
        phases += [(illum, 0.5, ecl) for illum in (99, 100) for ecl in (1, 50, 100)]
        warm = [(5, 0.95, 0), (50, 0.25, 0), (100, 0.5, 30)]  # low moon at 17:30: horizon tint
        checked = 0
        for phase, hour in [(p, 12) for p in phases] + [(p, 17) for p in warm]:
            a, b, _ = _pair(phase, hour, 30)
            for bx in BODY_OFFSETS:
                cam = OFFSETS[bx]
                a.screen.clear()
                b.screen.clear()
                a.draw_moon_body(cam)
                b.draw_moon_body(cam)
                self.assertEqual(a.screen.px, b.screen.px, f"state {phase} bx={bx}")
                checked += 1
        self.assertGreater(checked, 4000)

    def test_nebula_at_every_drift_position_tint_and_dim(self) -> None:
        a, b, clocks = _pair((5, 0.8, 0))
        # every hour tint, and dims 15 / 57 / 58 / 100
        for hour, minute in ((2, 0), (5, 0), (6, 15), (8, 0), (12, 0), (19, 0), (20, 45), (22, 0)):
            for clk in clocks:
                clk.hour, clk.minute = hour, minute
            for d in range(64):
                # 64 drift steps are 544 palette steps, so k moves the palette by 44
                for k in (0, 1, 2):
                    for clk in clocks:
                        clk.now_ms = 64 * 3400 * k + 3400 * d
                    for p in (a, b):
                        p.shade()
                        p.update_hour_tint()
                        p.build_pal()
                        p.build_map()
                        p.screen.clear()
                        p.draw_nebula()
                    self.assertEqual(a.lastd, d)
                    self.assertEqual(a.screen.px, b.screen.px, f"{hour}:{minute:02d} d={d} k={k}")
        self.assertLessEqual(b.max_runs, b.tb.run_cap)

    def test_shooting_star_and_stars(self) -> None:
        a, b, clocks = _pair((30, 0.3, 0), 22, 0)
        for p in (a, b):
            p.on_show()
        for k in (900, 901, 902, 903, 917, 1000):
            for j in range(0, 300, 7):
                for c_off in (0, 4100, 6000):
                    t = k * 47000 + j
                    for clk in clocks:
                        clk.now_ms = t
                    a.shown = b.shown = t - c_off
                    fa, fb = a.draw(), b.draw()
                    self.assertSameFrame(fa, fb, f"shooting star t={t} c={c_off}")

    def test_real_dates_across_a_lunation_and_the_eclipses(self) -> None:
        epochs = [1_790_000_000_000 + n * 61_000_000 for n in range(43)]  # every ~17 h for 30 days
        epochs += [
            int(dt.datetime(y, m, d, h, tzinfo=dt.timezone.utc).timestamp() * 1000)
            for y, m, d, h in ((2026, 3, 3, 11), (2026, 8, 28, 4), (2027, 2, 20, 23), (2027, 8, 17, 7))
        ]
        seen = set()
        for epoch in epochs:
            a, b, clocks = _pair(None, 19, 40, epoch=epoch)
            self.assertEqual((a.illum, a.wax, a.ecl, a.pnm, a.pcts), (b.illum, b.wax, b.ecl, b.pnm, b.pcts))
            seen.add(a.pnm)
            for p in (a, b):
                p.on_show()
            for c in (0, 4300, 6000, 9300):
                for clk in clocks:
                    clk.now_ms = a.shown + c
                self.assertSameFrame(a.draw(), b.draw(), f"epoch {epoch} c={c}")
        self.assertEqual(seen, {"NEW", "CRES", "QRTR", "GIBB", "FULL", "ECL"})

    def test_timelines_across_every_cache_rebuild(self) -> None:
        """Continuous runs: drift steps every 3.4 s, a shooting star, the
        once-a-minute phase recompute switching state, the hour tint and the
        dim ramp changing, and a re-show restarting the camera."""

        runs = (
            # (start h, m, s, phase before, phase after)
            (20, 29, 10, (5, 0.8, 0), (100, 0.5, 100)),
            (17, 59, 20, (100, 0.5, 30), (49, 0.3, 0)),
        )
        frames = 0
        for hour, minute, sec, before, after in runs:
            t0 = 47000 * 1000 - 20000
            a, b, clocks = _pair(before, hour, minute, now=t0)
            for p in (a, b):
                p.on_show()
            t = t0
            next_loop = t0
            reshown = False
            while t < t0 + 75000:
                elapsed = (t - t0) // 1000 + sec
                for clk in clocks:
                    clk.now_ms = t
                    clk.minute = (minute + elapsed // 60) % 60
                    clk.hour = hour + (minute + elapsed // 60) // 60
                if t >= next_loop:
                    if t - t0 > 30000:
                        a.phase_override = b.phase_override = after
                    a.loop()
                    b.loop()
                    next_loop = t + 1000 + (t // 7) % 53  # a slightly irregular slow tick
                if not reshown and t - t0 >= 33000:
                    a.on_show()
                    b.on_show()
                    reshown = True
                self.assertSameFrame(a.draw(), b.draw(), f"timeline {hour}:{minute} t={t}")
                frames += 1
                t += 90
            self.assertEqual((a.illum, a.ecl), (after[0], after[2]), "the recompute must have happened")
        self.assertGreater(frames, 1600)


class SourceExecutionTests(unittest.TestCase):
    """The .ax text itself, compiled by berry_lite, against the mirrors and each other."""

    def _timeline(self, make_a, make_b, epoch, hour, minute, span=12000, step=97):
        ca = sim.Clock(45_000_000, epoch, 2026, hour, minute)
        cb = sim.Clock(45_000_000, epoch, 2026, hour, minute)
        a, b = make_a(ca), make_b(cb)
        a.on_show()
        b.on_show()
        t = last = 45_000_000
        n = 0
        while t < 45_000_000 + span:
            if t - last >= 700:
                ca.now_ms = cb.now_ms = t
                a.loop()
                b.loop()
                last = t
            ca.now_ms = cb.now_ms = t
            fa, fb = a.draw(), b.draw()
            self.assertEqual(fa, fb, f"epoch {epoch} t={t}")
            n += 1
            t += step
        return n

    CASES = (
        (EPOCH, 10, 8),  # capture day: new moon
        (int(dt.datetime(2026, 3, 3, 11, tzinfo=dt.timezone.utc).timestamp() * 1000), 3, 0),  # total eclipse
        (1_790_000_000_000, 20, 45),  # gibbous, evening dim ramp
        (1_788_000_000_000, 6, 15),  # morning ramp
        (1_789_000_000_000, 17, 30),  # another phase, late afternoon
    )

    def test_installed_source_runs_exactly_as_the_v20_emulator(self) -> None:
        for epoch, hour, minute in self.CASES:
            self._timeline(lambda c: sim.BerryMoon(INSTALLED, c), sim.MoonV20, epoch, hour, minute)

    def test_staged_source_runs_exactly_as_the_v21_mirror(self) -> None:
        for epoch, hour, minute in self.CASES:
            self._timeline(lambda c: sim.BerryMoon(STAGED, c), sim.MoonV21, epoch, hour, minute)

    def test_staged_source_draws_exactly_what_the_installed_source_draws(self) -> None:
        for epoch, hour, minute in self.CASES[:3]:
            self._timeline(lambda c: sim.BerryMoon(INSTALLED, c), lambda c: sim.BerryMoon(STAGED, c),
                           epoch, hour, minute, span=11000, step=131)

    def test_sources_agree_when_the_phase_changes_under_them(self) -> None:
        """The minute recompute lands a new phase: v2.1 must rebuild its disc
        and re-measure its label ("1%" -> "100" is a wider label)."""

        eclipse = self.CASES[1][0]
        for make_a, make_b in (
            (lambda c: sim.BerryMoon(INSTALLED, c), lambda c: sim.BerryMoon(STAGED, c)),
            (sim.MoonV21, lambda c: sim.BerryMoon(STAGED, c)),
        ):
            ca = sim.Clock(45_000_000, EPOCH, 2026, 21, 0)
            cb = sim.Clock(45_000_000, EPOCH, 2026, 21, 0)
            a, b = make_a(ca), make_b(cb)
            t = last = 45_000_000
            texts = set()
            while t < 45_000_000 + 66000:
                if t >= 45_000_000 + 30000:
                    ca.epoch_ms = cb.epoch_ms = eclipse
                if t - last >= 1000:
                    ca.now_ms = cb.now_ms = t
                    a.loop()
                    b.loop()
                    last = t
                ca.now_ms = cb.now_ms = t
                if (t - 45_000_000) % 11000 == 0:
                    a.on_show()
                    b.on_show()
                fa, fb = a.draw(), b.draw()
                self.assertEqual(fa, fb, f"t={t}")
                texts.update(s for _, _, s, _ in fa.texts)
                t += 250
            self.assertTrue({"1%", "NEW", "100", "ECL"} <= texts, texts)

    def test_neither_source_relies_on_resize_zero_filling(self) -> None:
        for path in (INSTALLED, STAGED):
            source = path.read_text(encoding="utf-8")
            with self.subTest(source=path.name):
                self.assertEqual(_drift_frames(source, 0x00), _drift_frames(source, sim.RESIZE_FILL))
        self.assertEqual(_drift_frames(INSTALLED.read_text(encoding="utf-8"), 0x00),
                         _drift_frames(STAGED.read_text(encoding="utf-8"), sim.RESIZE_FILL))

    def test_the_investigated_build_did_rely_on_it(self) -> None:
        """Why the RLE decode now writes its zeros: the 2026-10-09 build skipped
        them, so it would draw garbage nebula if resize() left non-zero bytes.
        Upstream Berry zero-fills (so on the panel it most likely drew
        correctly); the fill here is a pessimistic model, not a measurement."""

        investigated = _investigated_minified().decode("utf-8")
        reference = _drift_frames(INSTALLED.read_text(encoding="utf-8"), 0x00)
        self.assertEqual(_drift_frames(investigated, 0x00), reference)
        garbage = _drift_frames(investigated, sim.RESIZE_FILL)
        self.assertEqual(sum(a != b for a, b in zip(garbage, reference)), 64, "every drift position")


@lru_cache(maxsize=None)
def _drift_frames(source: str, fill: int) -> tuple:
    """A source run by berry_lite through all 64 nebula drift positions."""

    import berry_lite

    program = berry_lite.Program(source, real_type=sim.R, resize_fill=fill)
    clock = sim.Clock(0, EPOCH, 2026, 12, 0)
    screen = sim.Screen()
    natives = dict(now_ms=lambda: clock.now_ms, epoch_ms=lambda: clock.epoch_ms, year=lambda: clock.year,
                   hour=lambda: clock.hour, minute=lambda: clock.minute, clear=screen.clear,
                   pixel=screen.pixel, line=screen.line, text=screen.text,
                   text_ink_width=screen.text_ink_width)
    page = program.run(natives)
    frames = []
    for d in range(64):
        clock.now_ms = d * 3400 + 1000
        page.loop()
        page.draw()
        frames.append(screen.frame())
    return tuple(frames)


# Runs of FUZZ_FRAMES frames per seed; MOON_FUZZ_RUNS=500 gives a long soak.
FUZZ_RUNS = int(os.environ.get("MOON_FUZZ_RUNS", "12"))
FUZZ_FRAMES = 150
FUZZ_SEEDS = (1, 2)
FUZZ_GLYPHS = {c: ["#" * (1 + (ord(c) % 3))] * 5 for c in "0123456789%NEWCRSQTGIBFULMO"}
EPOCH_2026, EPOCH_2036 = 1_767_225_600_000, 2_082_758_400_000


@lru_cache(maxsize=None)
def _program(text: str, fill: int):
    import berry_lite

    return berry_lite.Program(text, real_type=sim.R, resize_fill=fill)


def _berry_page(text: str, fill: int, clock: "sim.Clock"):
    screen = sim.Screen(sim.Font(FUZZ_GLYPHS))
    natives = dict(now_ms=lambda: clock.now_ms, epoch_ms=lambda: clock.epoch_ms, year=lambda: clock.year,
                   hour=lambda: clock.hour, minute=lambda: clock.minute, clear=screen.clear,
                   pixel=screen.pixel, line=screen.line, text=screen.text,
                   text_ink_width=screen.text_ink_width)
    return _program(text, fill).run(natives), screen


def fuzz_sources(text_a: str, text_b: str, seed: int, runs: int, frames: int = FUZZ_FRAMES):
    """Run two Berry sources side by side under one random clock.

    Random epochs 2026-2036 (eclipses included), ``now_ms`` up to 2.147e9,
    ``hour() == -1`` with year 1970 (before NTP) and NTP landing mid-run,
    minute and hour rollovers, epoch jumps, random interleaving of
    ``loop()`` / ``draw()`` / ``on_show()`` / ``should_show()``, and a
    ``resize`` fill of 0x00, 0xA5 or 0xFF.  Returns ``(frames, mismatch)``,
    where ``mismatch`` describes the first difference or is None.
    """

    rng = random.Random(seed)
    done = 0
    for run in range(runs):
        epoch = rng.randrange(EPOCH_2026, EPOCH_2036)
        now = rng.choice([rng.randrange(0, 10_000_000), rng.randrange(10_000_000, 800_000_000),
                          rng.randrange(2_000_000_000, 2_147_000_000)])
        hour = rng.choice([-1, 0, 3, 5, 6, 6, 9, 12, 17, 18, 20, 20, 21, 23, rng.randrange(24)])
        minute = rng.randrange(60)
        year = 1970 if hour == -1 and rng.random() < 0.7 else 2026
        fill = rng.choice([0x00, 0xA5, 0xFF])
        clocks = (sim.Clock(now, epoch, year, hour, minute), sim.Clock(now, epoch, year, hour, minute))
        (a, sa), (b, sb) = _berry_page(text_a, fill, clocks[0]), _berry_page(text_b, fill, clocks[1])
        t = now
        for frame in range(frames):
            event = rng.random()
            step = rng.choice([1, 7, 24, 25, 90, 400, 1000, 3400, 11000, 47000, 60001])
            t += step
            for c in clocks:
                c.now_ms = t
            if event < 0.03:  # NTP lands, or the minute (and maybe the hour) rolls over
                nm = (clocks[0].minute + 1) % 60
                nh = ((rng.randrange(24) if clocks[0].hour == -1 else clocks[0].hour) + (1 if nm == 0 else 0)) % 24
                for c in clocks:
                    c.year, c.minute, c.hour = 2026, nm, nh
                    c.epoch_ms += step
            elif event < 0.06:
                jump = rng.randrange(EPOCH_2026, EPOCH_2036)
                for c in clocks:
                    c.epoch_ms = jump
            else:
                for c in clocks:
                    c.epoch_ms += step
            r = rng.random()
            if r < 0.4:
                a.loop()
                b.loop()
            if r > 0.95:
                a.on_show()
                b.on_show()
            if rng.random() < 0.1 and a.should_show() != b.should_show():
                return done, f"seed {seed} run {run} frame {frame}: should_show differs"
            a.draw()
            b.draw()
            fa, fb = sa.frame(), sb.frame()
            done += 1
            if fa != fb:
                diff = [(k % 32, k // 32) for k, (x, y) in enumerate(zip(fa.pixels, fb.pixels)) if x != y]
                return done, (f"seed {seed} run {run} frame {frame}: t={t} epoch={clocks[0].epoch_ms} "
                              f"h:m={clocks[0].hour}:{clocks[0].minute} year={clocks[0].year} fill={fill:#x} "
                              f"pixels {diff[:6]} texts {fa.texts} vs {fb.texts}")
    return done, None


class RandomisedSourceTests(unittest.TestCase):
    """The exact upload bytes, v2.0 against v2.1, under random clocks.

    Most of IdentityTests compares the two Python mirrors; this compares the
    two minified ``.ax`` texts directly, as berry_lite runs them.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.installed = minify(INSTALLED.read_text(encoding="utf-8"))
        cls.staged = minify(STAGED.read_text(encoding="utf-8"))

    def test_upload_bytes_draw_identically_under_random_clocks(self) -> None:
        self.assertEqual(hashlib.sha256(self.installed.encode("utf-8")).hexdigest(), INSTALLED_MIN_SHA256)
        self.assertEqual(hashlib.sha256(self.staged.encode("utf-8")).hexdigest(), STAGED_MIN_SHA256)
        total = 0
        for seed in FUZZ_SEEDS:
            frames, mismatch = fuzz_sources(self.installed, self.staged, seed, FUZZ_RUNS)
            self.assertIsNone(mismatch)
            total += frames
        self.assertEqual(total, len(FUZZ_SEEDS) * FUZZ_RUNS * FUZZ_FRAMES)

    def test_the_comparison_catches_one_wrong_pixel_per_row(self) -> None:
        """The harness is sensitive: shortening every nebula run by one pixel
        (the run's last column) shows within the first seed's runs."""

        good = "if a == b pixel(a, y, col) else line(a, y, b, y, col) end"
        self.assertEqual(self.staged.count(good), 1)
        mutant = self.staged.replace(good, "if a == b pixel(a, y, col) else line(a, y, b - 1, y, col) end")
        _, mismatch = fuzz_sources(self.installed, mutant, FUZZ_SEEDS[0], FUZZ_RUNS)
        self.assertIsNotNone(mismatch)


class BackendTests(unittest.TestCase):
    def test_pure_python_reals_match_numpy(self) -> None:
        if sim._np is None:
            self.skipTest("numpy not installed: the pure backend is already the only one")
        cases = [(5, 0.8, 0), (100, 0.5, 100), (40, 0.3, 0)]

        def frames():
            out = []
            for phase in cases:
                a, b, clocks = _pair(phase, 20, 47)
                for p in (a, b):
                    p.on_show()
                for c in (0, 4000, 4500, 6000, 9500):
                    for clk in clocks:
                        clk.now_ms = a.shown + c + 7
                    out.append((a.draw(), b.draw()))
            out.append(_cap_render(lambda c: sim.MoonV20(c, font=FONT), FIXTURE["captures"][3]))
            return out

        reference = frames()
        try:
            sim.use_backend("pure")
            pure = frames()
        finally:
            sim.use_backend("numpy")
        self.assertEqual(pure, reference)

    def test_pure_reals_refuse_an_unconverted_number(self) -> None:
        with self.assertRaises(TypeError):
            sim.F32(1.5) * 2.0
        with self.assertRaises(TypeError):
            3 + sim.F32(1.5)
        self.assertEqual(float(sim.F32(0.1)), struct.unpack("<f", struct.pack("<f", 0.1))[0])
        self.assertNotEqual(float(sim.F32(0.1)), 0.1)


def _investigated_minified() -> bytes:
    text = minify(STAGED.read_text(encoding="utf-8"))
    for staged, investigated in FIXES:
        text = text.replace(staged, investigated)
    return text.encode("utf-8")


class StagedSourceTests(unittest.TestCase):
    """moon-panel-next.ax: the guards moon-panel.ax has, plus its own."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.body = STAGED.read_text(encoding="utf-8")
        cls.minified = minify(cls.body).encode("utf-8")

    def test_header_bumps_only_the_version(self) -> None:
        def header(text):
            return dict(re.findall(r"^# @(\w+) (.+)$", text, re.M))

        old, new = header(INSTALLED.read_text(encoding="utf-8")), header(self.body)
        self.assertEqual(old["version"], "2.0")
        self.assertEqual(new["version"], "2.1")
        self.assertEqual({k: v for k, v in old.items() if k != "version"},
                         {k: v for k, v in new.items() if k != "version"})

    def test_installed_source_is_untouched(self) -> None:
        shipped = minify(INSTALLED.read_text(encoding="utf-8")).encode("utf-8")
        self.assertEqual(len(shipped), INSTALLED_MIN_BYTES)
        self.assertEqual(hashlib.sha256(shipped).hexdigest(), INSTALLED_MIN_SHA256)

    def test_minified_bytes_are_pinned(self) -> None:
        self.assertEqual(len(self.minified), STAGED_MIN_BYTES)
        self.assertEqual(hashlib.sha256(self.minified).hexdigest(), STAGED_MIN_SHA256)

    def test_only_two_code_edits_separate_it_from_the_investigated_build(self) -> None:
        text = self.minified.decode("utf-8")
        for staged, _ in FIXES:
            self.assertEqual(text.count(staged), 1)
        self.assertEqual(hashlib.sha256(_investigated_minified()).hexdigest(), INVESTIGATED_MIN_SHA256)

    def test_fits_the_panel_source_limits(self) -> None:
        self.assertLessEqual(STAGED.stat().st_size, 16384)
        self.assertLessEqual(len(self.minified), 12288)

    def test_no_wx_import_and_returns_the_instance(self) -> None:
        self.assertNotIn("import wx", self.body)
        lines = sim.code_lines(self.body)
        self.assertEqual(lines[-1], "return Moon()")
        self.assertIn("class Moon", lines)

    def test_red_is_reserved_for_eclipses(self) -> None:
        self.assertIn("var HORIZON_TINT = 0xE8A24C", self.body)
        self.assertIn("var HORIZON_WARM = 70", self.body)
        self.assertNotIn("0xE65C2B", self.body)
        eclipse = self.body.index("0x8B2500")
        self.assertIn("self.ecl", self.body[max(0, eclipse - 200):eclipse])

    def test_warmth_is_gated_on_altitude_not_distance_from_transit(self) -> None:
        self.assertIn("self.cs(h / 24.0)", self.body)
        self.assertRegex(self.body, r"alt\s*>=\s*0\.0\s*&&\s*alt\s*<\s*HORIZON_ALT")

    def test_page_is_always_available_once_the_clock_is_valid(self) -> None:
        for needle in ("var MOON_START_MINUTE = 1080", "var MOON_END_MINUTE = 1290",
                       "var MOON_REMINDER_MS = 900000", "var ALWAYS_SHOW = true",
                       "self.last_shown = self.shown", "now_ms() - self.last_shown >= MOON_REMINDER_MS"):
            self.assertIn(needle, self.body)

    def test_no_sentinel_the_clock_can_return(self) -> None:
        self.assertIn("self.lh = -99", self.body)
        self.assertNotIn("self.lh = -1\n", self.body)

    def test_tint_is_assigned_unconditionally(self) -> None:
        init = self.body.split("def init()")[1].split("def on_show")[0]
        self.assertIn("self.ct = [", init)

    def test_page_hides_itself_without_a_clock(self) -> None:
        self.assertIn("if !self.valid return false end", self.body)
        self.assertIn("if year() < 2020 return nil end", self.body)

    def test_dead_fields_are_gone(self) -> None:
        for dead in ("self.prv", "self.up ", "self.eboot", "self.edays", "self.far", "self.near", "self.map"):
            self.assertNotIn(dead, self.body)

    def test_no_unproven_container_calls(self) -> None:
        """push(), size(), bytes resize and line() run on this panel today; list resize() does not."""

        calls = set(re.findall(r"\.(\w+)\(", "\n".join(sim.code_lines(self.body))))
        self.assertLessEqual(calls - {"push", "size", "resize"}, {
            "sn", "cs", "mix", "fade_pixel", "days", "name_phase", "compute_phase", "compute_position",
            "update_hour_tint", "shade", "build_pal", "build_map", "build_disc", "draw_nebula",
            "draw_stars", "draw_shooting_star", "draw_moon_body", "draw_labels",
        })
        self.assertNotIn(".nr.resize(", self.body)
        self.assertEqual(re.findall(r"(\w+)\.resize\(", self.body), ["nb"])


class StaticGuardTests(unittest.TestCase):
    """The Berry v2.1 spells what the mirror computes."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.old_text = INSTALLED.read_text(encoding="utf-8")
        cls.new_text = STAGED.read_text(encoding="utf-8")
        cls.old = sim.methods(cls.old_text)
        cls.new = sim.methods(cls.new_text)
        cls.code = "\n".join(sim.code_lines(cls.new_text))
        cls.t20 = sim.parse_tables(cls.old_text)
        cls.t21 = sim.parse_tables(cls.new_text)

    def test_unchanged_methods_are_textually_identical(self) -> None:
        self.assertEqual(set(self.old), UNCHANGED | CHANGED)
        self.assertEqual(set(self.new), UNCHANGED | CHANGED | ADDED)
        for name in sorted(UNCHANGED):
            with self.subTest(method=name):
                self.assertEqual(self.new[name], self.old[name])
        for name in sorted(CHANGED):
            with self.subTest(method=name):
                self.assertNotEqual(self.new[name], self.old[name])

    def test_compute_phase_and_position_only_gain_one_line(self) -> None:
        for name, extra in (("compute_phase", "self.build_disc()"),
                            ("compute_position", "self.hc = self.mix(0, self.litc, 8)")):
            with self.subTest(method=name):
                self.assertEqual(self.new[name], self.old[name][:-1] + [extra, "end"])

    def test_class_members_change_only_as_the_caches_require(self) -> None:
        def members(text):
            body = text.split("class Moon")[1].split("def init()")[0]
            return {m.strip() for line in re.findall(r"var ([^\n]+)", body) for m in line.split(",")}

        old, new = members(self.old_text), members(self.new_text)
        self.assertEqual(old - new, {"far", "near", "map"})
        self.assertEqual(new - old, {"st", "nr", "nn", "dr", "dk", "hc", "pw", "pws"})

    def test_packed_run_layout_matches_the_mirror(self) -> None:
        S = sim
        for spelled in (
            f"R[k] = s | ((x - 1) << {S.RUN_X1_SHIFT}) | (y << {S.RUN_Y_SHIFT}) | ((pv - 1) << {S.RUN_PAL_SHIFT})",
            f"var col = P[p >> {S.RUN_PAL_SHIFT}]",
            f"var a = p & {S.RUN_X_MASK}, b = (p >> {S.RUN_X1_SHIFT}) & {S.RUN_X_MASK}, y = (p >> {S.RUN_Y_SHIFT}) & {S.RUN_Y_MASK}",
            f"if pv > 0 && k < {self.t21.run_cap}",
            f"while k < {self.t21.run_slots}",
            "if a == b pixel(a, y, col) else line(a, y, b, y, col) end",
        ):
            with self.subTest(expr=spelled):
                self.assertIn(spelled, self.code)
        self.assertEqual(self.t21.run_cap, self.t21.run_slots)

    def test_packed_disc_layout_matches_the_mirror(self) -> None:
        S = sim
        for spelled in (
            f"p = p | ((0x{S.SEG_PRESENT:X} | (pk << {S.SEG_CLASS_SHIFT}) | ((c - 1) << {S.SEG_X1_SHIFT}) | s) << sh)",
            f"sh += {S.SEG_BITS}",
            f"var a = bx + (p & {S.SEG_X_MASK}), b = bx + ((p >> {S.SEG_X1_SHIFT}) & {S.SEG_X_MASK}), k = (p >> {S.SEG_CLASS_SHIFT}) & {S.SEG_CLASS_MASK}",
            f"p = p >> {S.SEG_BITS}",
            "k = d >= 0.25 ? 2 : (d >= -0.75 ? 1 : 0)",
            "var col = k == 2 ? c2 : (k == 1 ? c1 : c0)",
        ):
            with self.subTest(expr=spelled):
                self.assertIn(spelled, self.code)
        self.assertEqual(self.t21.disc_rows, 8)

    def test_star_table_layout_matches_the_mirror(self) -> None:
        S = sim
        for spelled in (
            f"while i < {S.FAR_STARS + S.NEAR_STARS}",
            f"if i == {S.FAR_STARS}",
            f"o = {S.NEAR_INDEX_OFFSET}",
            f"var i4 = i * {S.STAR_STRIDE}",
            f"var P = T[i4 + 2] * {S.PERIOD_UNIT_MS}",
            "var pr = real((t + (i + o) * 370) % P) / real(P)",
            f"if i < {S.FAR_STARS} w = w * tr end",
        ):
            with self.subTest(expr=spelled):
                self.assertIn(spelled, self.code)
        # v2.0's near stars were i + 20 for i = 0..7; v2.1 runs them as i = 16..23.
        self.assertEqual(S.FAR_STARS + S.NEAR_INDEX_OFFSET, 20)
        self.assertIn("real((t + (i + 20) * 370) % self.near[i4 + 2])", self.old_text)

    def test_star_bytes_are_v20s_star_lists(self) -> None:
        st, far, near = self.t21.st, self.t20.far, self.t20.near
        self.assertEqual(len(st), sim.STAR_STRIDE * (sim.FAR_STARS + sim.NEAR_STARS))
        self.assertEqual((len(far), len(near)), (sim.STAR_STRIDE * sim.FAR_STARS, sim.STAR_STRIDE * sim.NEAR_STARS))
        for n, (table, offset) in enumerate([(far, 0)] * sim.FAR_STARS + [(near, sim.FAR_STARS)] * sim.NEAR_STARS):
            i = n - offset
            old = list(table[i * 4:i * 4 + 4])
            with self.subTest(star=n):
                self.assertEqual(old[2] % sim.PERIOD_UNIT_MS, 0, "period must survive /100")
                self.assertLessEqual(old[2] // sim.PERIOD_UNIT_MS, 255)
                self.assertEqual([st[n * 4], st[n * 4 + 1], st[n * 4 + 2] * sim.PERIOD_UNIT_MS, st[n * 4 + 3]], old)

    def test_nebula_rle_decodes_to_the_installed_bytes(self) -> None:
        self.assertEqual(len(self.t20.neb), 505, "v2.0's 1,011-digit literal: 505 bytes")
        self.assertEqual(self.t21.neb_size, len(self.t20.neb))
        self.assertEqual(self.t21.neb, self.t20.neb)
        self.assertNotIn(0xFF, self.t20.neb, "0xFF is the RLE escape")
        with self.assertRaises(IndexError):
            sim.decode_rle(self.t21.rle, self.t21.neb_size - 1)

    def test_every_field_fits_its_bits(self) -> None:
        S = sim
        # disc: x0/x1 are columns within the 8-wide disc
        span = self.t21.span0
        cols = [span[r * 2] + span[r * 2 + 1] - 1 for r in range(8)] + [span[r * 2] for r in range(8)]
        self.assertLessEqual(max(cols), S.SEG_X_MASK)
        self.assertLessEqual(2, S.SEG_CLASS_MASK)
        self.assertLess(S.SEG_CLASS_SHIFT + S.SEG_CLASS_MASK.bit_length(), S.SEG_BITS)
        self.assertEqual(S.SEG_PRESENT, 1 << (S.SEG_BITS - 1))
        most = 0
        for illum in range(101):
            for phf in (0.3, 0.8):
                page = sim.MoonV21(sim.Clock(), phase=(illum, phf, 0))
                for p in page.dr:
                    segs = 0
                    while p > 0:
                        segs += 1
                        p >>= S.SEG_BITS
                    most = max(most, segs)
        self.assertLessEqual(most, 3)
        self.assertLessEqual(most * S.SEG_BITS, 31, "fits a signed 32-bit int")
        # nebula: x 0..31, y 0..7, palette index 0..14
        self.assertEqual(S.RUN_X_MASK, 31)
        self.assertEqual(S.RUN_Y_SHIFT - S.RUN_X1_SHIFT, 5)
        self.assertEqual(S.RUN_PAL_SHIFT - S.RUN_Y_SHIFT, 3)
        self.assertLess(14 << S.RUN_PAL_SHIFT | 31 << S.RUN_X1_SHIFT | 7 << S.RUN_Y_SHIFT | 31, 1 << 17)

    def test_no_drift_position_overflows_the_run_slots(self) -> None:
        page = sim.MoonV21(sim.Clock())
        for d in range(64):
            page.clock.now_ms = d * 3400
            page.build_map()
        self.assertEqual(page.max_runs, 60)
        self.assertLessEqual(page.max_runs, self.t21.run_cap)

    def test_shared_tables_are_identical(self) -> None:
        for field in ("horizon_alt", "horizon_warm", "horizon_tint", "start_minute", "end_minute",
                      "reminder_ms", "always_show", "ct_default", "tints", "hw", "span0", "mc", "sc"):
            with self.subTest(field=field):
                self.assertEqual(getattr(self.t21, field), getattr(self.t20, field))

    @staticmethod
    def _reals(text: str) -> set[str]:
        code = "\n".join(s for s in sim.code_lines(text) if not s.startswith("#"))
        code = re.sub(r'"[^"]*"', '""', code)
        return set(re.findall(r"(?<![\w.])\d+\.\d+", code))

    def test_every_real_literal_in_v21_is_spelled_as_in_v20(self) -> None:
        """Whatever the panel's number parser does with a literal, it does the same in both."""

        self.assertLessEqual(self._reals(self.new_text), self._reals(self.old_text))

    def test_the_mirrors_use_only_their_sources_literals(self) -> None:
        def used(*classes):
            src = "\n".join(inspect.getsource(c) for c in classes)
            return {abs(float(v)) for v in re.findall(r"\bR\((-?\d+\.\d+)\)", src)}

        def spelled(text):
            return {float(v) for v in self._reals(text)}

        self.assertLessEqual(used(sim._MoonPage, sim.MoonV20), spelled(self.old_text))
        self.assertLessEqual(used(sim._MoonPage, sim.MoonV21), spelled(self.new_text))


if __name__ == "__main__":
    unittest.main(verbosity=2)
