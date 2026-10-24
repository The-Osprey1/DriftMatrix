"""Deterministic contract tests for the AWTRIX Nightkeeper scheduler."""

from __future__ import annotations

import datetime as dt
import re
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

import nightkeeper_logic as nk


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "nightkeeper.ax"
PACIFIC = ZoneInfo("America/Los_Angeles")


def at(hour: int, minute: int = 0, second: int = 0) -> dt.datetime:
    return dt.datetime(2026, 8, 10, hour, minute, second)


class ScheduleValidationTests(unittest.TestCase):
    def test_shipped_defaults_are_explicit(self) -> None:
        schedule = nk.NightSchedule()
        self.assertEqual(
            (
                schedule.sleep_hour,
                schedule.sleep_minute,
                schedule.wake_hour,
                schedule.wake_minute,
                schedule.fade_minutes,
                schedule.fade_steps,
                schedule.day_brightness,
                schedule.night_brightness,
            ),
            (21, 30, 7, 0, 30, 15, 45, 1),
        )

    def test_invalid_or_ambiguous_schedules_are_rejected(self) -> None:
        bad = (
            {"sleep_hour": 24},
            {"wake_minute": 60},
            {"fade_minutes": 0},
            {"fade_steps": 0},
            {"fade_minutes": 1, "fade_steps": 61},
            {"day_brightness": 256},
            {"night_brightness": 45},
            {"sleep_hour": 7, "wake_hour": 7},
            {"sleep_hour": 8, "sleep_minute": 0, "wake_hour": 7, "fade_minutes": 31},
        )
        for kwargs in bad:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                nk.NightSchedule(**kwargs)


class DesiredStateTests(unittest.TestCase):
    def assert_target(
        self,
        when: dt.datetime,
        phase: nk.Phase,
        power: bool,
        brightness: int,
    ) -> None:
        self.assertEqual(
            nk.desired_state(when),
            nk.DisplayTarget(power, brightness, phase),
        )

    def test_all_exact_boundaries_are_half_open_and_unambiguous(self) -> None:
        cases = (
            (at(20, 59, 59), "day", True, 45),
            (at(21, 0), "fade_down", True, 45),
            (at(21, 1), "fade_down", True, 42),
            (at(21, 2), "fade_down", True, 40),
            # Lands on night one minute before blackout, so the cut is unseen.
            (at(21, 29), "fade_down", True, 1),
            (at(21, 30), "sleep", False, 1),
            (at(23, 59, 59), "sleep", False, 1),
            (at(0, 0), "sleep", False, 1),
            (at(6, 59, 59), "sleep", False, 1),
            (at(7, 0), "fade_up", True, 1),
            (at(7, 1), "fade_up", True, 1),
            (at(7, 2), "fade_up", True, 2),
            # Reaches full day one minute early, so day starts continuous.
            (at(7, 29), "fade_up", True, 45),
            (at(7, 30), "day", True, 45),
        )
        for when, phase, power, brightness in cases:
            with self.subTest(when=when):
                self.assert_target(when, phase, power, brightness)

    def test_fades_are_mirror_images_and_monotonic(self) -> None:
        down = [nk.desired_state(at(21, minute)).brightness for minute in range(30)]
        up = [nk.desired_state(at(7, minute)).brightness for minute in range(30)]
        self.assertEqual(down, list(reversed(up)), "the two fades share one curve")
        self.assertEqual(down[0], 45)
        self.assertEqual(down[-1], 1)
        self.assertTrue(all(a >= b for a, b in zip(down, down[1:])))
        self.assertTrue(all(a <= b for a, b in zip(up, up[1:])))

    def test_the_fade_is_perceptually_even(self) -> None:
        """The point of v2.

        Brightness is perceived as roughly value ** (1/2.2), so a linear ramp
        appears to stall then plunge, and v1.2's smoothstep was worse: its
        perceived steps varied by about 30x across the fade.  Equal perceived
        steps are what make a bedtime fade feel gentle.
        """

        gamma = nk.PERCEPTUAL_GAMMA
        levels = [nk.desired_state(at(21, minute)).brightness for minute in range(30)]
        perceived = [(v / 45) ** (1 / gamma) for v in levels]
        steps = [a - b for a, b in zip(perceived, perceived[1:])]

        # The continuous curve is uniform by construction; what survives here
        # is integer quantisation, since 45 levels cannot express every step.
        resolved = [s for s in steps if s > 0.001]
        self.assertLess(
            max(resolved) / min(resolved),
            5.0,
            "perceived steps should be near-uniform, not 30x apart",
        )

    def test_v2_beats_the_curves_it_replaced(self) -> None:
        def spread(levels: list[float]) -> float:
            perceived = [(v / 45) ** (1 / nk.PERCEPTUAL_GAMMA) for v in levels]
            steps = [a - b for a, b in zip(perceived, perceived[1:])]
            steps = [s for s in steps if s > 1e-9]
            return max(steps) / min(steps)

        shipped = [nk.desired_state(at(21, m)).brightness for m in range(30)]
        linear = [45 - 44 * m / 29 for m in range(30)]
        smoothstep = [45 - 44 * ((m / 29) ** 2 * (3 - 2 * (m / 29))) for m in range(30)]

        self.assertLess(spread(shipped), spread(linear))
        self.assertLess(spread(shipped), spread(smoothstep))

    def test_each_level_holds_for_a_whole_minute(self) -> None:
        down = [
            nk.desired_state(at(21, 0, 0) + dt.timedelta(seconds=i)).brightness
            for i in range(1800)
        ]
        self.assertTrue(all(1 <= value <= 45 for value in down))
        for start in range(0, 1800, 60):
            self.assertEqual(
                len(set(down[start : start + 60])),
                1,
                "the panel resolves the fade per minute, not per second",
            )

    def test_midnight_does_not_wake_or_change_brightness(self) -> None:
        before = nk.desired_state(dt.datetime(2026, 8, 10, 23, 59, 59))
        after = nk.desired_state(dt.datetime(2026, 8, 11, 0, 0, 0))
        self.assertEqual(before, after)
        self.assertEqual(after.phase, "sleep")

    def test_dst_spring_gap_and_fall_fold_stay_asleep(self) -> None:
        spring_before = dt.datetime(2026, 3, 8, 1, 59, 59, tzinfo=PACIFIC)
        spring_after = dt.datetime(2026, 3, 8, 3, 0, 0, tzinfo=PACIFIC)
        fall_first = dt.datetime(2026, 11, 1, 1, 30, tzinfo=PACIFIC, fold=0)
        fall_second = dt.datetime(2026, 11, 1, 1, 30, tzinfo=PACIFIC, fold=1)
        for when in (spring_before, spring_after, fall_first, fall_second):
            with self.subTest(when=when, fold=when.fold):
                self.assert_target(when, "sleep", False, 1)

    def test_post_dst_wake_uses_local_wall_time(self) -> None:
        for day in (dt.date(2026, 3, 8), dt.date(2026, 11, 1)):
            wake = dt.datetime.combine(day, dt.time(7, 0), PACIFIC)
            full = dt.datetime.combine(day, dt.time(7, 30), PACIFIC)
            with self.subTest(day=day):
                self.assert_target(wake, "fade_up", True, 1)
                self.assert_target(full, "day", True, 45)

    def test_custom_non_midnight_sleep_window_uses_same_model(self) -> None:
        schedule = nk.NightSchedule(
            sleep_hour=12,
            sleep_minute=0,
            wake_hour=20,
            fade_minutes=30,
        )
        self.assertEqual(nk.desired_state(at(11, 30), schedule).phase, "fade_down")
        self.assertEqual(nk.desired_state(at(12), schedule).phase, "sleep")
        self.assertEqual(nk.desired_state(at(20), schedule).phase, "fade_up")
        self.assertEqual(nk.desired_state(at(20, 30), schedule).phase, "day")


class ReconciliationTests(unittest.TestCase):
    def test_boot_reconciles_every_phase_instead_of_waiting_for_boundary(self) -> None:
        for when in (at(12), at(21, 15), at(2), at(7, 15)):
            target = nk.desired_state(when)
            with self.subTest(when=when, target=target):
                self.assertTrue(nk.needs_apply(None, target))

    def test_success_is_idempotent_until_actuator_state_changes(self) -> None:
        target = nk.desired_state(at(12))
        applied = nk.record_result(None, target, succeeded=True)
        self.assertFalse(nk.needs_apply(applied, target))
        self.assertFalse(
            nk.needs_apply(applied, nk.DisplayTarget(True, 45, "fade_down"))
        )

    def test_failure_is_not_committed_and_retries_on_next_tick(self) -> None:
        target = nk.desired_state(at(23))
        applied = nk.record_result(None, target, succeeded=False)
        self.assertIsNone(applied)
        self.assertTrue(nk.needs_apply(applied, target))
        applied = nk.record_result(applied, target, succeeded=True)
        self.assertFalse(nk.needs_apply(applied, target))

    def test_clock_jump_reconciles_directly_without_replaying_missed_steps(self) -> None:
        before = nk.desired_state(at(21, 5))
        applied = nk.record_result(None, before, succeeded=True)
        after = nk.desired_state(at(21, 45))
        self.assertTrue(nk.needs_apply(applied, after))
        self.assertEqual(after, nk.DisplayTarget(False, 1, "sleep"))

    def test_one_day_has_two_power_changes_and_a_bounded_write_count(self) -> None:
        previous = nk.desired_state(dt.datetime(2026, 8, 9, 23, 59))
        brightness_changes = 0
        power_changes = 0
        for minute in range(24 * 60):
            current = nk.desired_state(
                dt.datetime(2026, 8, 10) + dt.timedelta(minutes=minute)
            )
            brightness_changes += current.brightness != previous.brightness
            power_changes += current.power != previous.power
            previous = current
        # Per-minute resolution buys a smoother fade for more writes, but the
        # panel still only changes brightness during the two fade windows.
        self.assertEqual(brightness_changes, 52)
        self.assertLessEqual(brightness_changes, 2 * 30)
        self.assertEqual(power_changes, 2)


class BrightnessOverrideTests(unittest.TestCase):
    def test_a_level_this_script_did_not_write_starts_a_hold(self) -> None:
        self.assertTrue(nk.observe_brightness(False, 1, 45))

    def test_agreement_leaves_the_hold_alone(self) -> None:
        self.assertFalse(nk.observe_brightness(False, 45, 45))
        self.assertTrue(nk.observe_brightness(True, 45, 45))

    def test_nothing_known_yet_cannot_imply_an_override(self) -> None:
        # A level with no baseline to compare against is not evidence either way.
        self.assertFalse(nk.observe_brightness(False, None, 45))

    def test_the_baseline_is_adopted_on_first_sight_not_first_write(self) -> None:
        """Otherwise the first manual change is silently overwritten.

        v2.1 only recorded a level when it wrote one, so a panel already
        matching the schedule kept a null baseline forever.  Measured on the
        panel: after a reboot into the blackout, a manual 45 was back to 1
        fifteen seconds later even with the hold in place.
        """

        baseline = nk.adopt_brightness(None, 1)
        self.assertEqual(baseline, 1)
        # Now a person's change is visible as a disagreement.
        self.assertTrue(nk.observe_brightness(False, baseline, 45))
        # And an existing baseline is never clobbered by a later observation.
        self.assertEqual(nk.adopt_brightness(1, 45), 1)

    def test_source_adopts_the_level_before_comparing(self) -> None:
        source = (ROOT / "nightkeeper.ax").read_text(encoding="utf-8")
        body = source[source.index("def reconcile") :]
        adopt = body.index("if self.last_brightness == nil")
        compare = body.index("if actual_brightness != self.last_brightness")
        self.assertLess(adopt, compare, "the baseline must be seeded first")

    def test_a_scheduled_transition_releases_a_brightness_hold(self) -> None:
        holding = nk.observe_brightness(False, 1, 45)
        self.assertTrue(holding)
        self.assertFalse(nk.release_hold(holding, "sleep", "fade_up"))
        self.assertTrue(nk.release_hold(holding, "sleep", "sleep"))


class ManualOverrideTests(unittest.TestCase):
    """Re-asserting power on a timer made the panel fight the person using it.

    Turning the display off by hand at midday was undone within the re-assert
    interval.  A disagreement between what the panel is doing and what this
    controller last wrote means somebody else moved it, and that is a decision,
    not drift.
    """

    def test_a_manual_change_starts_a_hold(self) -> None:
        self.assertTrue(nk.observe_power(False, last_written_power=True, observed_power=False))
        self.assertTrue(nk.observe_power(False, last_written_power=False, observed_power=True))

    def test_agreement_does_not_start_a_hold(self) -> None:
        self.assertFalse(nk.observe_power(False, last_written_power=True, observed_power=True))
        self.assertFalse(nk.observe_power(False, last_written_power=False, observed_power=False))

    def test_a_hold_survives_until_the_schedule_moves(self) -> None:
        self.assertTrue(nk.release_hold(True, nk.desired_state(at(12)).phase, "day") is True)
        self.assertFalse(nk.release_hold(True, "day", "fade_down"))
        self.assertFalse(nk.release_hold(True, "sleep", "fade_up"))

    def test_boot_with_nothing_written_yet_never_holds(self) -> None:
        self.assertFalse(nk.observe_power(False, last_written_power=None, observed_power=True))
        self.assertFalse(nk.observe_power(False, last_written_power=None, observed_power=False))

    def test_a_full_day_releases_every_hold_at_a_transition(self) -> None:
        """However often you override, each hold ends at the next setpoint."""

        holding = True
        previous = nk.desired_state(at(12)).phase
        released_at = []
        for minute in range(12 * 60, 24 * 60):
            phase = nk.desired_state(at(minute // 60, minute % 60)).phase
            holding = nk.release_hold(holding, previous, phase)
            if not holding and previous != phase:
                released_at.append(phase)
            previous = phase
        self.assertIn("fade_down", released_at)


@unittest.skipUnless(SOURCE.exists(), "nightkeeper.ax has not been added yet")
class BerrySourceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SOURCE.read_text(encoding="utf-8")

    def test_source_fits_panel_limit_and_declares_shipped_configuration(self) -> None:
        # Measured after minification, because that is what gets uploaded.
        # This always-running helper should stay substantially smaller than the
        # visual apps already resident in the Berry VM.
        import sys

        sys.path.insert(0, str(ROOT / "weather"))
        from minify_ax import minify

        shipped = len(minify(self.source).encode("utf-8"))
        # The binding constraint is not a round byte count, it is the largest
        # CONTIGUOUS free block: compiling needs roughly twice the source, and
        # this panel has offered 11-20KB depending on fragmentation.
        self.assertLess(shipped * 2, 11_000, "would not install on a busy panel")
        # And it should still stay well under the visual apps it shares the VM
        # with (now_playing is ~7KB minified); this one only keeps a schedule.
        self.assertLessEqual(shipped, 5_120)
        constants = {
            "SLEEP_MIN": 1290,
            "WAKE_MIN": 420,
            "FADE_MIN": 30,
            "DAY_BRIGHTNESS": 45,
            "NIGHT_BRIGHTNESS": 1,
        }
        for name, value in constants.items():
            with self.subTest(name=name):
                self.assertRegex(self.source, rf"\b{name}\s*=\s*{value}\b")

    def test_source_is_headless_and_never_relies_on_buttons_or_gpio(self) -> None:
        self.assertRegex(self.source, r"(?m)^# @headless true\s*$")
        self.assertNotIn("on_button", self.source)
        self.assertNotRegex(self.source.lower(), r"\b(gpio|button|btn)\b")
        for slot_hook in ("should_show", "duration", "draw", "on_show", "on_hide"):
            with self.subTest(slot_hook=slot_hook):
                self.assertNotRegex(self.source, rf"\bdef\s+{slot_hook}\s*\(")
        self.assertNotRegex(self.source, r"\brotation\.")

    def test_source_uses_local_time_validation_and_runtime_display_power(self) -> None:
        for marker in ("desired_state", "time_valid", "reconcile"):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.source)
        self.assertRegex(self.source, r"\by\s*>=\s*2025\b|year\(\)\s*>=\s*2025")
        self.assertIn("hour() * 60 + minute()", self.source)
        # v3.0: the matrix is switched in-process (firmware 1.1.1+), not by
        # PATCHing the panel's own HTTP API over loopback.
        self.assertIn("display.power(self.wanted_power)", self.source)
        self.assertIn("display.is_on()", self.source)
        self.assertNotIn("http.", self.source)
        self.assertNotIn("/api/v1/", self.source)
        self.assertIn("settings.set", self.source)
        self.assertRegex(self.source, r'settings\.set\(\s*["\']brightness["\']')

    def test_source_matches_the_cyclic_schedule(self) -> None:
        self.assertRegex(
            self.source,
            r"if\s+(?:minute_now|now)\s*>=\s*SLEEP_MIN\s*\|\|\s*(?:minute_now|now)\s*<\s*WAKE_MIN",
        )
        self.assertRegex(self.source, r"(?:minute_now|now)\s*>=\s*SLEEP_MIN - FADE_MIN")
        self.assertRegex(self.source, r"(?:minute_now|now)\s*<\s*WAKE_MIN \+ FADE_MIN")
        # Both fades index one shared table, so they cannot drift apart.
        self.assertEqual(self.source.count("RAMP["), 2)

    def test_embedded_ramp_matches_the_reference_curve(self) -> None:
        """The table is precomputed, so nothing on the panel can verify it.

        If DAY_BRIGHTNESS or NIGHT_BRIGHTNESS is edited without regenerating
        RAMP, the fade silently goes wrong.  This is the check that catches it.
        """

        match = re.search(r"var RAMP = \[([0-9,\s]+)\]", self.source)
        self.assertIsNotNone(match, "RAMP table not found in the source")
        embedded = [int(v) for v in match.group(1).split(",")]

        schedule = nk.NightSchedule()
        self.assertEqual(len(embedded), schedule.fade_minutes)
        self.assertEqual(embedded, schedule.ramp)
        self.assertEqual(embedded[0], schedule.day_brightness)
        self.assertEqual(embedded[-1], schedule.night_brightness)

    def test_there_is_no_request_left_to_wedge_the_schedule(self) -> None:
        """v1.x left in_flight true forever if a callback never arrived.

        v2.x guarded that with a deadline.  v3.0 removes the request itself:
        display.power() is synchronous, so there is no callback to lose.
        """

        for gone in ("in_flight", "INFLIGHT_MS", "def on_power", "def send_power", "http.patch"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, self.source)

    def test_brightness_is_held_too_not_only_power(self) -> None:
        """A hold must cover the whole setpoint, not just the power flag.

        v2.0 forced brightness on every tick regardless of the hold, so waking
        the panel by hand during the blackout left it on at level 1 and dimming
        it by hand was undone within a second.  Measured on the panel on
        2026-08-22: a manual 45 was back to 1 six seconds later.
        """

        self.assertIn("self.last_brightness", self.source)
        body = self.source[self.source.index("def reconcile") :]
        write = body.index('settings.set("brightness"')
        # The only brightness write must be guarded by the hold.
        guard = body.rindex("if !self.hold", 0, write)
        self.assertLess(write - guard, 160, "the brightness write is not behind the hold")
        self.assertEqual(body.count('settings.set("brightness"'), 1)

    def test_the_hold_clears_before_the_setpoint_is_applied(self) -> None:
        # Otherwise a scheduled transition takes effect one tick late.
        body = self.source[self.source.index("def reconcile") :]
        self.assertLess(
            body.index("self.hold = false"),
            body.index('settings.set("brightness"'),
        )

    def test_power_is_observed_before_it_is_reasserted(self) -> None:
        """It must LOOK at the panel, not re-assert on a timer.

        Blind re-assertion undid a manual change within the interval, so the
        display argued with whoever turned it off.  v3.0 looks every tick,
        which display.is_on() makes free, and only writes behind the hold.
        """

        body = self.source[self.source.index("def reconcile") :]
        look = body.index("display.is_on()")
        write = body.index("display.power(")
        self.assertLess(look, write)
        guard = body.rindex("if !self.hold && actual_power != self.wanted_power", 0, write)
        self.assertLess(write - guard, 80, "the power write is not behind the hold")
        self.assertEqual(body.count("display.power("), 1)
        self.assertNotIn("REASSERT_MS", self.source)

    def test_a_manual_override_is_held_not_overwritten(self) -> None:
        self.assertRegex(
            self.source,
            r"actual_power\s*!=\s*self\.last_power[\s\S]{0,160}self\.hold\s*=\s*true",
        )
        # ...and the hold is cleared by a scheduled transition, not by a timer.
        self.assertRegex(
            self.source,
            r"self\.phase\s*!=\s*self\.said[\s\S]{0,80}self\.hold\s*=\s*false",
        )
        self.assertRegex(self.source, r"!self\.hold[\s\S]{0,80}display\.power\(self\.wanted_power\)")

    def test_power_baseline_is_adopted_on_first_look(self) -> None:
        # Same rule as brightness (see adopt_brightness): the first reading is
        # a baseline, not an override, or boot would hold forever.
        self.assertRegex(
            self.source,
            r"if self\.last_power == nil\s+self\.last_power = actual_power",
        )

    def test_boot_waits_for_valid_time_then_reconciles_current_state(self) -> None:
        self.assertIn("if !self.time_valid() return", self.source)
        self.assertRegex(
            self.source,
            r"if ENABLED[\s\S]{0,320}else[\s\S]{0,320}wanted_power\s*=\s*true",
        )
        self.assertRegex(
            self.source,
            r"if ENABLED[\s\S]{0,420}else[\s\S]{0,420}wanted_brightness\s*=\s*DAY_BRIGHTNESS",
        )
        self.assertIn('settings.get("brightness")', self.source)
        self.assertIn("self.last_power = nil", self.source)

    def test_disabled_mode_restores_a_normal_panel(self) -> None:
        """v1.2 regressed this: `if !ENABLED return` stranded the display off.

        Turning Nightkeeper off during the night must give the panel back, not
        leave it dark until someone notices and reboots it.
        """

        valid_time_gate = self.source.index("if !self.time_valid() return")
        enabled = self.source.index("if ENABLED", valid_time_gate)
        desired = self.source.index("self.desired_state()", enabled)
        disabled = self.source.index("else", desired)
        restore_power = self.source.index("self.wanted_power = true", disabled)
        restore_brightness = self.source.index(
            "self.wanted_brightness = DAY_BRIGHTNESS",
            restore_power,
        )
        # The guard is now hold-aware: a level a person chose is left alone
        # until the next scheduled transition, brightness as well as power.
        brightness_guard = self.source.index(
            "if !self.hold && actual_brightness != self.wanted_brightness",
            restore_brightness,
        )
        brightness_write = self.source.index(
            'settings.set("brightness", self.wanted_brightness)',
            brightness_guard,
        )
        power_guard = self.source.index(
            "actual_power != self.wanted_power",
            brightness_write,
        )
        power_patch = self.source.index("display.power(self.wanted_power)", power_guard)

        ordered = (
            valid_time_gate,
            enabled,
            desired,
            disabled,
            restore_power,
            restore_brightness,
            brightness_guard,
            brightness_write,
            power_guard,
            power_patch,
        )
        self.assertTrue(all(a < b for a, b in zip(ordered, ordered[1:])))
        # And it must not be the v1.2 shape, which simply bailed out.
        self.assertNotRegex(self.source, r"if\s*!ENABLED\s*\|\|")

    def test_cached_state_follows_every_write(self) -> None:
        # The write is synchronous, so the cache is updated right after it.
        self.assertRegex(
            self.source,
            r"display\.power\(self\.wanted_power\)\s+self\.last_power = self\.wanted_power",
        )
        self.assertRegex(
            self.source,
            r"if\s+!self\.hold\s*&&\s*actual_brightness\s*!=\s*self\.wanted_brightness[\s\S]{0,160}settings\.set\(\"brightness\",\s*self\.wanted_brightness\)",
        )

    def test_source_has_no_deep_sleep_or_persistent_zero_brightness(self) -> None:
        self.assertNotIn("/api/v1/device/sleep", self.source)
        self.assertNotRegex(
            self.source.lower(),
            r"(?:deep[_ -]?sleep|esp_deep_sleep|\bsleep\s*\()",
        )
        self.assertNotRegex(self.source, r'"brightness"\s*:\s*0\b')


if __name__ == "__main__":
    unittest.main(verbosity=2)
