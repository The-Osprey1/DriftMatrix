"""Layout guards for the four pushed scenes that share the 32x8 panel.

These apps shipped without a preview renderer, and the panel absorbs bad
geometry in silence: ``put_pixel`` clamps, so a glyph drawn past the last row
is simply discarded and the lane still encodes to a well-formed payload.

Two faults reached the repository that way, and neither could fail any of the
existing tests, because both produce valid 23x8 bitmaps of exactly the right
byte length:

* Three of the four drew a second 5-row text line at ``y=6`` on an eight-row
  panel, losing three of its five rows off the bottom edge.  The primary value
  -- the ETA, the build status, the call timer -- was the half that vanished.
* Two of them then painted an accent rail across row 4, drawn *after* the text,
  straight through the middle of the line above it.

The guards below assert on what each renderer *attempts*, by wrapping
``put_pixel``.  That is the only vantage point from which the first fault is
visible at all.

Note which modules get wrapped.  ``desk_companion`` and ``commute_radar`` do
``from visual_system import put_pixel``, which binds a *separate* name in each
module: patching ``visual_system`` alone catches their text (``draw_text``
resolves ``put_pixel`` in its own module globals) but misses the direct
``put_pixel`` calls in their lane code.  Every scene therefore names every
module that can draw on its behalf.
"""

from __future__ import annotations

import unittest
from io import BytesIO

from PIL import Image

import commute_radar as cr
import desk_companion as dc
import github_sentinel as gs
import meeting_sentinel as ms
import visual_system as vs

WIDTH = 32
HEIGHT = 8
GUTTER_X = 8
LANE_X0 = 9
BLACK = (0, 0, 0)
TEXT_BAND = range(1, 6)


class _RecordingPutPixel:
    """Wrap put_pixel across several modules and record off-panel draws."""

    def __init__(self, *modules) -> None:
        self.modules = modules
        self.originals: dict[object, object] = {}
        self.off_panel: list[tuple[int, int]] = []

    def __enter__(self) -> "_RecordingPutPixel":
        for module in self.modules:
            original = module.put_pixel
            self.originals[module] = original

            def counting(frame, x, y, color, _original=original):
                if not (0 <= x < WIDTH and 0 <= y < HEIGHT):
                    self.off_panel.append((x, y))
                return _original(frame, x, y, color)

            module.put_pixel = counting
        return self

    def __exit__(self, *exc) -> None:
        for module, original in self.originals.items():
            module.put_pixel = original


def lane_scenes():
    """Every reachable lane state, as (label, drawing modules, renderer)."""
    for mood in dc.CompanionMood:
        yield (
            f"companion/{mood.value}",
            (vs, dc),
            lambda m=mood: dc.render_companion_lane(m),
        )

    for traffic in cr.TrafficCondition:
        estimate = cr.CommuteEstimate("WORK", 52, 25, traffic)
        yield (
            f"commute/{traffic.value}",
            (vs, cr),
            lambda e=estimate: cr.render_commute_lane(e),
        )

    runs = (
        ("pass", gs.WorkflowRun("owner/miku-jarvis", 412, "completed", "success")),
        ("fail", gs.WorkflowRun("owner/miku-jarvis", 413, "completed", "failure")),
        ("running", gs.WorkflowRun("owner/miku-jarvis", 414, "in_progress", None)),
    )
    for name, run in runs:
        yield f"github-ci/{name}", (gs,), lambda r=run: gs.render_ci_lane_frame(r)

    reports = (
        ("fresh", gs.PRRadarReport("owner/awtrix", 3, 1, 2 * 3600)),
        ("24h", gs.PRRadarReport("owner/awtrix", 7, 2, 25 * 3600)),
        ("48h", gs.PRRadarReport("owner/awtrix", 12, 4, 50 * 3600)),
    )
    for name, report in reports:
        yield f"github-pr/{name}", (gs,), lambda r=report: gs.render_pr_lane_frame(r)

    sessions = (
        ("short", ms.ActiveMicSession("Discord", "d.exe", 100.0, 125.0)),
        ("hour", ms.ActiveMicSession("Zoom", "z.exe", 100.0, 3665.0)),
    )
    for name, session in sessions:
        yield f"meeting/{name}", (ms,), lambda s=session: ms.render_lane_frame(s)


class SceneLayoutTests(unittest.TestCase):
    def test_no_lane_draws_outside_the_panel(self) -> None:
        """The panel is eight rows; a five-row font leaves room for one line."""
        for label, modules, render in lane_scenes():
            with self.subTest(scene=label):
                with _RecordingPutPixel(*modules) as recorder:
                    render()
                self.assertEqual(
                    recorder.off_panel,
                    [],
                    f"{label} drew {len(recorder.off_panel)} pixels outside the "
                    f"32x8 panel; put_pixel discards them silently",
                )

    def test_instrumentation_detects_the_layout_that_shipped(self) -> None:
        """A guard that cannot fail is worthless; reproduce the original fault."""
        with _RecordingPutPixel(vs) as recorder:
            frame = vs.new_frame()
            vs.draw_text(frame, "24M", LANE_X0 + 1, 6, vs.PEARL)
        self.assertTrue(
            recorder.off_panel,
            "a 5-row glyph drawn at y=6 must be seen to overrun an 8-row panel",
        )
        self.assertEqual(sorted({y for _, y in recorder.off_panel}), [8, 9, 10])

    def test_no_rail_crosses_the_text_band(self) -> None:
        """Decorative rails belong on row 7, below the rows 1..5 glyph band.

        Checked against the union of every rail colour in the family, not just
        the colours the scene's own modules happen to define.  The earlier
        version built the set per-scene and skipped when it was empty, which
        silently excused meeting_sentinel -- it declares no TRACK -- so two of
        the sixteen states were never checked at all and a rail painted across
        the meeting lane's type would have left the suite green.
        """
        rails = {
            getattr(m, "TRACK")
            for m in (vs, cr, dc, gs, ms)
            if hasattr(m, "TRACK")
        }
        self.assertTrue(rails, "no rail colours found to check against")

        for label, _modules, render in lane_scenes():
            with self.subTest(scene=label):
                frame = render()
                for y in TEXT_BAND:
                    for x in range(LANE_X0, WIDTH):
                        self.assertNotIn(
                            frame[y][x],
                            rails,
                            f"{label} painted rail colour across the text band "
                            f"at ({x}, {y})",
                        )

    def test_unbounded_values_stay_on_panel_and_off_the_gutter(self) -> None:
        """Values that arrive from outside must not be able to break geometry.

        The per-state guard above only covers hand-picked sample values.  CI
        run numbers come straight from the GitHub API, and #100000 measured
        27px against a 21px budget: _fit_label returned it unchecked, the
        centring arithmetic went negative, and the label drew at x=7 -- seven
        pixels clamped off the panel and the black gutter lit on rows 2 and 4.
        """
        cases = [
            ("run number", lambda n: gs.render_ci_lane_frame(
                gs.WorkflowRun("owner/miku-jarvis", n, "in_progress", None))),
        ]
        for name, render in cases:
            for n in (0, 1, 53, 414, 99999, 100000, 9999999, 2**31 - 1):
                with self.subTest(case=name, value=n):
                    off = []
                    original = gs.put_pixel

                    def counting(frame, x, y, color, _o=original):
                        if not (0 <= x < WIDTH and 0 <= y < HEIGHT):
                            off.append((x, y))
                        return _o(frame, x, y, color)

                    gs.put_pixel = counting
                    try:
                        frame = render(n)
                    finally:
                        gs.put_pixel = original

                    self.assertEqual(off, [], f"{name} {n} drew {len(off)} pixels off-panel")
                    for y in range(HEIGHT):
                        self.assertEqual(
                            frame[y][GUTTER_X], BLACK,
                            f"{name} {n} lit the column-{GUTTER_X} gutter at row {y}",
                        )

    def test_open_pr_count_stays_on_panel(self) -> None:
        """open_prs is len() of an API response and is likewise unbounded."""
        for n in (0, 3, 12, 99, 100, 99999):
            with self.subTest(open_prs=n):
                frame = gs.render_pr_lane_frame(
                    gs.PRRadarReport("owner/awtrix", n, 0, 0.0))
                for y in range(HEIGHT):
                    self.assertEqual(
                        frame[y][GUTTER_X], BLACK,
                        f"open_prs={n} lit the gutter at row {y}",
                    )

    def test_every_lane_keeps_the_black_safety_gutter(self) -> None:
        """Column 8 stays black so the icon and the lane never bleed together."""
        for label, _modules, render in lane_scenes():
            with self.subTest(scene=label):
                frame = render()
                for y in range(HEIGHT):
                    self.assertEqual(frame[y][GUTTER_X], BLACK, f"{label} lit the gutter")


ONE_BIT_THRESHOLD = 174


def one_bit(frame):
    """What the panel shows at brightness 1: one bit per channel."""
    return [
        [tuple(255 if ch >= ONE_BIT_THRESHOLD else 0 for ch in px) for px in row]
        for row in frame
    ]


class SceneFamilyTests(unittest.TestCase):
    """Guards for what the four scenes must share, and for motion that moves.

    Everything here was written after an adversarial review refuted four
    changes that the suite had passed at 973 green.  Each guard fails against
    the code as it stood before the fifth pass.
    """

    def _lane_row7(self, frame):
        return [frame[7][x] for x in range(vs.LANE_X0, vs.LANE_X0 + vs.LANE_WIDTH)]

    def test_every_lane_draws_the_same_ground_line(self) -> None:
        """Every scene shares the full-width rail geometry on row 7.

        It was rendered four ways -- full width, inset two with pips on rows 0
        and 7, inset one in a locally redefined TRACK, and absent -- and the
        rotation advances every seven seconds, so the ground changed length,
        colour and existence between pages of what is meant to be one system.
        The companion uses a dim mood gradient without a measured value;
        quantitative scenes retain the shared level rail and leader contract.
        """
        for label, _modules, render in lane_scenes():
            with self.subTest(scene=label):
                frame = render()
                row7 = self._lane_row7(frame)
                # Full width: every column lit, no dark stub at either end.
                self.assertTrue(
                    all(px != vs.BLACK for px in row7),
                    f"{label}: row 7 has a gap at "
                    f"{[i for i, px in enumerate(row7) if px == vs.BLACK]}",
                )
                # Row 7 is one rail in every scene (Now Playing's, since
                # 2026-10-03): a fill anchored at the left rising into a PEARL
                # leader, then ONE dark tint of the same level colour -- or
                # that tint alone when the scene has no scale.
                filled, colour = vs.read_meter([[vs.BLACK] * vs.LANE_X0 + list(row7)] * 8)
                track = row7[filled:]
                if label.startswith("companion/"):
                    self.assertEqual(filled, 0, f"{label}: mood implies a measurement")
                    self.assertEqual(vs.meter_fill(frame), 0)
                    self.assertTrue(all(max(px) < 90 for px in track),
                                    f"{label}: mood ribbon crosses the data threshold")
                    self.assertEqual(frame, render(), f"{label}: lane is not static")
                    self.assertTrue(all(row[vs.GUTTER_X] == vs.BLACK for row in frame),
                                    f"{label}: mood ribbon crosses the gutter")
                else:
                    self.assertLessEqual(len(set(track)), 1, f"{label}: track mixes colours")
                if track and not label.startswith("companion/"):
                    self.assertLess(max(track[0]), 90, f"{label}: track is not a dark tint")
                    self.assertIn(track[0], {vs.scale_color(c, vs.TRACK_TINT) for c in vs.LEVEL_COLORS.values()},
                                  f"{label}: track is not a level colour's tint")
                if filled:
                    self.assertEqual(row7[filled - 1], vs.PEARL, f"{label}: no leader")
                    if filled > 1:
                        self.assertIn(colour, set(vs.LEVEL_COLORS.values()),
                                      f"{label}: meter uses a non-level colour")
                    brightness = [sum(px) for px in row7[:filled - 1]]
                    self.assertEqual(brightness, sorted(brightness), f"{label}: fill does not rise to the leader")
                # Nothing above the text band: pips on row 0 were one scene's
                # private habit.
                self.assertTrue(
                    all(
                        render()[0][x] == vs.BLACK
                        for x in range(vs.LANE_X0, vs.LANE_X0 + vs.LANE_WIDTH)
                    ),
                    f"{label}: row 0 is not clear",
                )

    def test_github_does_not_shadow_the_shared_track(self) -> None:
        """github_sentinel defined its own TRACK, one shade off the shared one.

        A locally redefined colour constant is invisible in a diff and shows up
        on the panel as one page whose ground is subtly the wrong shade.
        """
        self.assertIs(gs.TRACK, vs.TRACK)

    def test_congestion_bar_is_anchored_never_empty_and_off_the_roof(self) -> None:
        """Severity must read from one glance at one state.

        The first version centred the bar, drew nothing for CLEAR, and put it
        on row 1 -- where the two-pixel MODERATE bar was pixel-identical to the
        car's own roof two rows below, same columns and same colour.
        """
        lengths = {}
        for traffic in cr.TrafficCondition:
            frame = vs.new_frame()
            cr.draw_car_icon(frame, 0, traffic)
            bar = [x for x in range(8) if frame[0][x] != vs.BLACK]
            roof = [x for x in range(8) if frame[3][x] != vs.BLACK]
            with self.subTest(traffic=traffic.value):
                self.assertTrue(bar, "the bar is empty, which reads as absent")
                self.assertEqual(bar[0], 0, "the bar is not anchored at x0")
                self.assertEqual(bar, list(range(len(bar))), "the bar has holes")
                self.assertNotEqual(bar, roof, "the bar duplicates the car roof")
                # Row 1 is the composite panel's text cap-line.
                self.assertTrue(
                    all(frame[1][x] == vs.BLACK for x in range(8)),
                    "the bar sits on the text band's top row",
                )
            lengths[traffic] = len(bar)
        self.assertEqual(len(set(lengths.values())), 3, f"lengths collide: {lengths}")

    def test_commute_icon_actually_moves_at_one_bit(self) -> None:
        """Recolouring a stationary pixel is flicker; motion needs displacement.

        The wheels alternated PEARL against ICE.  Both clear the 174 threshold,
        so at brightness 1 they are the same colour and the two frames were
        pixel-identical -- zero changing pixels in all three states.
        """
        for traffic in cr.TrafficCondition:
            frames = []
            for phase in (0, 1):
                f = vs.new_frame()
                cr.draw_car_icon(f, phase, traffic)
                frames.append(one_bit(f))
            moved = [
                (x, y)
                for y in range(8)
                for x in range(8)
                if frames[0][y][x] != frames[1][y][x]
            ]
            with self.subTest(traffic=traffic.value):
                self.assertTrue(
                    moved,
                    "the animation is invisible at brightness 1: "
                    "two frames, zero changing pixels",
                )

    def test_an_undatable_mic_session_shows_no_number(self) -> None:
        """A stale registry key must not render an authoritative duration.

        "Stop == 0 and the exe is alive" is the shape of a live capture and of
        a crashed one alike.  Clamping the start to now rendered "00:00"; not
        clamping rendered "72H00" for a three-day-old stale key.
        """
        stale = ms.ActiveMicSession(
            app_name="teams",
            exe_path=r"C:\x\teams.exe",
            started_at=0.0,
            elapsed_seconds=0.0,
            elapsed_known=False,
        )
        live = ms.ActiveMicSession(
            app_name="teams",
            exe_path=r"C:\x\teams.exe",
            started_at=0.0,
            elapsed_seconds=125.0,
        )
        self.assertEqual(ms.format_duration(0), "00:00")
        stale_lane = ms.render_lane_frame(stale)
        live_lane = ms.render_lane_frame(live)
        self.assertNotEqual(
            stale_lane,
            ms.render_lane_frame(
                ms.ActiveMicSession("teams", r"C:\x\teams.exe", 0.0, 0.0)
            ),
            "an undatable session is rendering as a call that just started",
        )
        self.assertNotEqual(stale_lane, live_lane)

    def test_saturated_duration_cannot_pass_as_a_reading(self) -> None:
        """The cap has to look capped, or it is a precise-looking fiction."""
        self.assertEqual(ms.format_duration(10 ** 9), "99H--")
        self.assertNotEqual(
            ms.format_duration(99 * 3600), ms.format_duration(200 * 3600)
        )
        self.assertLessEqual(
            ms.text_width(ms.format_duration(10 ** 9)), ms.LANE_WIDTH - 2
        )

    def test_no_scene_keeps_its_own_copy_of_a_shared_colour(self) -> None:
        """A duplicated palette is a divergence waiting to happen.

        github_sentinel's local TRACK had already drifted one shade from the
        shared one, and meeting_sentinel's RED_HOT was (255,20,35) against
        (255,25,35) -- so ON AIR red rendered one shade off the companion's
        ON AIR red on the adjacent page of the rotation.  Neither difference
        is visible on its own, and neither would ever show up in a diff.
        """
        shared = {
            name: value
            for name, value in vars(vs).items()
            if name.isupper() and isinstance(value, tuple) and len(value) == 3
        }
        for module in (cr, dc, gs, ms):
            for name, value in shared.items():
                local = getattr(module, name, None)
                if local is None:
                    continue
                with self.subTest(module=module.__name__, token=name):
                    self.assertEqual(
                        local,
                        value,
                        f"{module.__name__}.{name} has drifted from visual_system",
                    )

    def test_every_meter_colour_survives_the_dark(self) -> None:
        """A meter below the threshold is a meter that is not there.

        This replaced the end-cap check when the calm-glance layout dropped
        end-caps: every level colour must still light at brightness 1.
        """
        for name, color in vs.LEVEL_COLORS.items():
            with self.subTest(level=name):
                self.assertNotEqual(one_bit([[color]])[0][0], (0, 0, 0))

    def test_the_mic_keeps_its_shape_in_every_frame(self) -> None:
        """The capsule must not vanish into the body at brightness 1.

        It inverted PEARL <-> RED_HOT every 120ms -- no shape change, and on
        the red frames the capsule was the same (255,0,0) as the CORAL body,
        so the mic collapsed into a featureless blob every other frame.
        """
        capsule = [(3, 1), (4, 1), (3, 2), (4, 2), (3, 3), (4, 3)]
        body = [(3, 5), (4, 5), (2, 6), (3, 6), (4, 6), (5, 6)]
        for phase in range(4):
            frame = vs.new_frame()
            ms.draw_onair_mic_icon(frame, phase)
            bits = one_bit(frame)
            with self.subTest(phase=phase):
                self.assertEqual(
                    {bits[y][x] for x, y in capsule}, {(255, 255, 255)},
                    "the capsule is not reading as the bright part of the mic",
                )
                self.assertNotIn(
                    (255, 255, 255),
                    {bits[y][x] for x, y in body},
                    "the body has gone the same colour as the capsule",
                )

    def test_no_icon_pays_for_frames_it_does_not_have(self) -> None:
        """A shipped GIF must not encode a repeat of its own cycle.

        The companion's FOCUS and ON_AIR avatars drew one frame four times,
        REST drew two, and the Octocat drew one -- every repeat charged to the
        GIF and to the 2KB payload budget to show a viewer a frame identical
        to the one before it.  PIL already merges *consecutive* duplicates, so
        what this catches is the case it cannot: a cycle like A,B,A,B, which
        is how REST shipped 234 bytes of a 148-byte animation.

        The assertion is on the encoded bytes, not on the drawing function: an
        icon is allowed to be a still (FOCUS is), it is just not allowed to
        pay for four copies of that still.
        """
        cases = [
            (f"companion/{mood.value}", dc.build_companion_icon_gif(mood))
            for mood in dc.CompanionMood
        ] + [
            (f"commute/{t.value}", cr.build_commute_icon_gif(t))
            for t in cr.TrafficCondition
        ] + [
            ("github/gear", gs.build_icon_gif(gs.draw_ci_gear_icon, frame_count=2)),
            ("github/fail", gs.build_icon_gif(gs.draw_ci_fail_icon, frame_count=2)),
            ("github/octocat", gs.build_icon_gif(gs.draw_octocat_pr_icon, frame_count=4)),
            ("meeting/mic", ms.build_onair_icon_gif()),
        ]
        for label, data in cases:
            image = Image.open(BytesIO(data))
            frames = []
            for index in range(image.n_frames):
                image.seek(index)
                pixels = list(image.convert("RGB").getdata())
                frames.append([pixels[r * 8:(r + 1) * 8] for r in range(8)])
            with self.subTest(icon=label):
                self.assertEqual(
                    vs.cycle_length(frames),
                    len(frames),
                    f"{label} ships {len(frames)} frames but repeats after "
                    f"{vs.cycle_length(frames)}",
                )


if __name__ == "__main__":
    unittest.main()
