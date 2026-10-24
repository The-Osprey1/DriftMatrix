from __future__ import annotations

import json
import math
from pathlib import Path
import tempfile
import unittest
from uuid import UUID, uuid4

import focus_sprint as fs


class FakeClock:
    def __init__(self, wall: float = 1_000.0, monotonic: float = 500.0) -> None:
        self.wall = wall
        self.monotonic = monotonic

    def wall_time(self) -> float:
        return self.wall

    def monotonic_time(self) -> float:
        return self.monotonic

    def advance(self, seconds: float, *, wall_seconds: float | None = None) -> None:
        self.monotonic += seconds
        self.wall += seconds if wall_seconds is None else wall_seconds


TEST_CONFIG = fs.SprintConfig(
    focus_seconds=10,
    short_break_seconds=4,
    long_break_seconds=8,
    long_break_every=2,
    cue_seconds=5,
    ready_seconds=2,
    recovery_window_seconds=120,
    sleep_drift_seconds=60,
    default_add_seconds=3,
    max_command_ids=8,
)


def command(kind: fs.CommandType, *, amount: float = 0) -> fs.SprintCommand:
    return fs.SprintCommand(kind, str(uuid4()), amount)


class PresetAndValueTests(unittest.TestCase):
    def test_convenience_presets_have_agreed_durations(self) -> None:
        expected = {
            "classic": (25, 5, 15, 4),
            "deep": (50, 10, 20, 3),
            "sprint": (15, 3, 10, 4),
        }
        self.assertEqual(set(fs.PRESETS), set(expected))
        for name, minutes in expected.items():
            preset = fs.get_preset(name.upper())
            self.assertEqual(
                (
                    preset.focus_seconds / 60,
                    preset.short_break_seconds / 60,
                    preset.long_break_seconds / 60,
                    preset.long_break_every,
                ),
                minutes,
            )

    def test_presets_are_conveniences_and_config_can_be_overridden(self) -> None:
        config = fs.SprintConfig.from_preset(
            "deep", focus_seconds=42, auto_start_break=False
        )
        self.assertEqual(config.focus_seconds, 42)
        self.assertEqual(config.short_break_seconds, 600)
        self.assertEqual(config.long_break_seconds, 1_200)
        self.assertEqual(config.long_break_every, 3)
        self.assertFalse(config.auto_start_break)

    def test_invalid_config_values_are_rejected(self) -> None:
        for invalid in (0, -1, math.inf, math.nan, True):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    fs.SprintConfig(focus_seconds=invalid)
        with self.assertRaises(ValueError):
            fs.SprintConfig(long_break_every=0)

    def test_commands_require_uuid_ids_and_add_requires_time(self) -> None:
        with self.assertRaises(ValueError):
            fs.SprintCommand(fs.CommandType.START, "not-an-id")
        with self.assertRaises(ValueError):
            fs.SprintCommand(fs.CommandType.ADD, str(uuid4()))
        with self.assertRaises(ValueError):
            fs.SprintCommand(fs.CommandType.PAUSE, str(uuid4()), 1)
        created = fs.SprintCommand.start(label=" private task ")
        UUID(created.command_id)
        self.assertEqual(created.label, "private task")
        self.assertNotIn("private task", repr(created))

    def test_command_mapping_accepts_queue_field_aliases(self) -> None:
        identifier = str(uuid4())
        parsed = fs.SprintCommand.from_mapping(
            {
                "id": identifier,
                "command": "add",
                "args": {"minutes": 2},
            }
        )
        self.assertEqual(parsed.kind, fs.CommandType.ADD)
        self.assertEqual(parsed.command_id, identifier)
        self.assertEqual(parsed.amount_seconds, 120)


class PureStateMachineTests(unittest.TestCase):
    def test_start_is_pure_and_does_not_mutate_input(self) -> None:
        before = fs.SprintState.idle(wall_checkpoint=10, monotonic_checkpoint=20)
        transition = fs.reduce(before, fs.SprintCommand.start(), TEST_CONFIG)
        self.assertEqual(before.phase, fs.Phase.IDLE)
        self.assertEqual(transition.state.phase, fs.Phase.FOCUS_RUNNING)
        self.assertEqual(transition.state.remaining_seconds, 10)
        self.assertEqual(transition.state.cycle_count, 0)
        self.assertTrue(transition.state.visible)

    def test_exact_focus_boundary_belongs_to_full_cue(self) -> None:
        state = fs.SprintState(
            phase=fs.Phase.FOCUS_RUNNING, remaining_seconds=10
        )
        before = fs.advance(state, 9.999, TEST_CONFIG).state
        self.assertEqual(before.phase, fs.Phase.FOCUS_RUNNING)
        self.assertAlmostEqual(before.remaining_seconds, 0.001)

        exact = fs.advance(state, 10, TEST_CONFIG)
        self.assertEqual(exact.state.phase, fs.Phase.FOCUS_DONE_CUE)
        self.assertEqual(exact.state.remaining_seconds, 5)
        self.assertEqual(exact.state.cycle_count, 1)
        self.assertEqual(exact.events[0].kind, fs.EventType.FOCUS_COMPLETED)

    def test_exact_cue_boundary_auto_starts_break(self) -> None:
        state = fs.SprintState(
            phase=fs.Phase.FOCUS_DONE_CUE,
            remaining_seconds=5,
            cycle_count=1,
        )
        result = fs.advance(state, 5, TEST_CONFIG)
        self.assertEqual(result.state.phase, fs.Phase.SHORT_BREAK_RUNNING)
        self.assertEqual(result.state.remaining_seconds, 4)

    def test_one_advance_can_cross_focus_cue_break_and_ready(self) -> None:
        state = fs.SprintState(
            phase=fs.Phase.FOCUS_RUNNING, remaining_seconds=10
        )
        at_ready = fs.advance(state, 10 + 5 + 4, TEST_CONFIG)
        self.assertEqual(at_ready.state.phase, fs.Phase.READY_NEXT)
        self.assertEqual(at_ready.state.remaining_seconds, 2)
        self.assertEqual(
            [event.kind for event in at_ready.events],
            [
                fs.EventType.FOCUS_COMPLETED,
                fs.EventType.BREAK_STARTED,
                fs.EventType.BREAK_COMPLETED,
            ],
        )

        absent = fs.advance(state, 10 + 5 + 4 + 2, TEST_CONFIG)
        self.assertEqual(absent.state.phase, fs.Phase.IDLE)
        self.assertFalse(absent.state.visible)
        self.assertEqual(absent.state.cycle_count, 1)

    def test_ready_never_auto_starts_the_next_focus(self) -> None:
        state = fs.SprintState(
            phase=fs.Phase.READY_NEXT,
            remaining_seconds=2,
            cycle_count=1,
        )
        result = fs.advance(state, 10_000, TEST_CONFIG)
        self.assertEqual(result.state.phase, fs.Phase.IDLE)
        self.assertEqual(result.state.remaining_seconds, 0)
        self.assertFalse(any(e.kind is fs.EventType.FOCUS_STARTED for e in result.events))

    def test_manual_start_is_allowed_during_ready(self) -> None:
        state = fs.SprintState(
            phase=fs.Phase.READY_NEXT,
            remaining_seconds=1,
            cycle_count=1,
        )
        result = fs.reduce(state, fs.SprintCommand.start(), TEST_CONFIG)
        self.assertEqual(result.state.phase, fs.Phase.FOCUS_RUNNING)
        self.assertEqual(result.state.cycle_count, 1)

    def test_pause_preserves_remaining_and_resume_uses_correct_phase(self) -> None:
        focus = fs.SprintState(
            phase=fs.Phase.FOCUS_RUNNING, remaining_seconds=7.25
        )
        paused = fs.reduce(focus, fs.SprintCommand.pause(), TEST_CONFIG).state
        self.assertEqual(paused.phase, fs.Phase.FOCUS_PAUSED)
        self.assertEqual(paused.pause_reason, fs.PauseReason.USER)
        self.assertEqual(paused.remaining_seconds, 7.25)
        self.assertEqual(fs.advance(paused, 1_000, TEST_CONFIG).state.remaining_seconds, 7.25)
        resumed = fs.reduce(paused, fs.SprintCommand.resume(), TEST_CONFIG).state
        self.assertEqual(resumed.phase, fs.Phase.FOCUS_RUNNING)
        self.assertEqual(resumed.remaining_seconds, 7.25)
        self.assertIsNone(resumed.pause_reason)

    def test_break_pause_preserves_long_break_identity_via_cadence(self) -> None:
        long_break = fs.SprintState(
            phase=fs.Phase.LONG_BREAK_RUNNING,
            remaining_seconds=7,
            cycle_count=2,
        )
        paused = fs.reduce(long_break, fs.SprintCommand.pause(), TEST_CONFIG).state
        self.assertEqual(paused.phase, fs.Phase.BREAK_PAUSED)
        resumed = fs.reduce(paused, fs.SprintCommand.resume(), TEST_CONFIG).state
        self.assertEqual(resumed.phase, fs.Phase.LONG_BREAK_RUNNING)
        self.assertEqual(resumed.remaining_seconds, 7)

    def test_long_break_occurs_on_configured_completed_focus_cadence(self) -> None:
        first = fs.SprintState(
            phase=fs.Phase.FOCUS_RUNNING,
            remaining_seconds=1,
            cycle_count=0,
        )
        first_break = fs.advance(first, 1 + 5, TEST_CONFIG).state
        self.assertEqual(first_break.phase, fs.Phase.SHORT_BREAK_RUNNING)

        second = fs.SprintState(
            phase=fs.Phase.FOCUS_RUNNING,
            remaining_seconds=1,
            cycle_count=1,
        )
        second_break = fs.advance(second, 1 + 5, TEST_CONFIG).state
        self.assertEqual(second_break.phase, fs.Phase.LONG_BREAK_RUNNING)
        self.assertEqual(second_break.remaining_seconds, 8)
        self.assertEqual(second_break.cycle_count, 2)

    def test_auto_start_break_can_be_disabled(self) -> None:
        config = fs.SprintConfig(
            focus_seconds=10,
            short_break_seconds=4,
            long_break_seconds=8,
            long_break_every=2,
            auto_start_break=False,
        )
        cue = fs.SprintState(
            phase=fs.Phase.FOCUS_DONE_CUE,
            remaining_seconds=5,
            cycle_count=1,
        )
        result = fs.advance(cue, 99, config)
        self.assertEqual(result.state.phase, fs.Phase.BREAK_PAUSED)
        self.assertEqual(result.state.remaining_seconds, 4)
        self.assertEqual(result.state.pause_reason, fs.PauseReason.AWAITING_START)

    def test_finish_forces_completion_while_skip_does_not_increment_cycle(self) -> None:
        focus = fs.SprintState(
            phase=fs.Phase.FOCUS_PAUSED,
            remaining_seconds=9,
            cycle_count=1,
            pause_reason=fs.PauseReason.USER,
        )
        finished = fs.reduce(focus, fs.SprintCommand.finish(), TEST_CONFIG).state
        self.assertEqual(finished.phase, fs.Phase.FOCUS_DONE_CUE)
        self.assertEqual(finished.cycle_count, 2)

        skipped = fs.reduce(focus, fs.SprintCommand.skip(), TEST_CONFIG).state
        self.assertEqual(skipped.phase, fs.Phase.SHORT_BREAK_RUNNING)
        self.assertEqual(skipped.cycle_count, 1)

    def test_skip_break_goes_to_brief_ready_not_focus(self) -> None:
        state = fs.SprintState(
            phase=fs.Phase.SHORT_BREAK_RUNNING,
            remaining_seconds=2,
            cycle_count=1,
        )
        result = fs.reduce(state, fs.SprintCommand.skip(), TEST_CONFIG)
        self.assertEqual(result.state.phase, fs.Phase.READY_NEXT)
        self.assertEqual(result.state.remaining_seconds, 2)

    def test_add_works_while_running_or_paused_but_not_in_cue(self) -> None:
        add = fs.SprintCommand.add(3)
        running = fs.SprintState(
            phase=fs.Phase.FOCUS_RUNNING, remaining_seconds=4
        )
        self.assertEqual(fs.reduce(running, add, TEST_CONFIG).state.remaining_seconds, 7)

        paused = fs.SprintState(
            phase=fs.Phase.BREAK_PAUSED,
            remaining_seconds=2,
            cycle_count=1,
            pause_reason=fs.PauseReason.USER,
        )
        self.assertEqual(
            fs.reduce(paused, fs.SprintCommand.add(3), TEST_CONFIG).state.remaining_seconds,
            5,
        )

        cue = fs.SprintState(
            phase=fs.Phase.FOCUS_DONE_CUE,
            remaining_seconds=5,
            cycle_count=1,
        )
        ignored = fs.reduce(cue, fs.SprintCommand.add(3), TEST_CONFIG)
        self.assertFalse(ignored.applied)
        self.assertEqual(ignored.state.remaining_seconds, 5)

    def test_stop_returns_to_idle_and_resets_cadence(self) -> None:
        state = fs.SprintState(
            phase=fs.Phase.LONG_BREAK_RUNNING,
            remaining_seconds=4,
            cycle_count=2,
            label="not persisted",
        )
        stopped = fs.reduce(state, fs.SprintCommand.stop(), TEST_CONFIG).state
        self.assertEqual(stopped.phase, fs.Phase.IDLE)
        self.assertEqual(stopped.cycle_count, 0)
        self.assertIsNone(stopped.label)

    def test_toggle_starts_pauses_and_resumes(self) -> None:
        state = fs.SprintState()
        started = fs.reduce(state, fs.SprintCommand.toggle(), TEST_CONFIG).state
        paused = fs.reduce(started, fs.SprintCommand.toggle(), TEST_CONFIG).state
        resumed = fs.reduce(paused, fs.SprintCommand.toggle(), TEST_CONFIG).state
        self.assertEqual(started.phase, fs.Phase.FOCUS_RUNNING)
        self.assertEqual(paused.phase, fs.Phase.FOCUS_PAUSED)
        self.assertEqual(resumed.phase, fs.Phase.FOCUS_RUNNING)

    def test_duplicate_command_is_not_applied_twice(self) -> None:
        state = fs.SprintState(
            phase=fs.Phase.FOCUS_RUNNING, remaining_seconds=5
        )
        add = fs.SprintCommand.add(3)
        first = fs.reduce(state, add, TEST_CONFIG)
        replay = fs.reduce(first.state, add, TEST_CONFIG)
        self.assertEqual(first.state.remaining_seconds, 8)
        self.assertEqual(replay.state.remaining_seconds, 8)
        self.assertFalse(replay.applied)
        self.assertTrue(replay.duplicate)
        self.assertEqual(replay.events, ())

    def test_no_op_command_is_consumed_so_it_cannot_replay_later(self) -> None:
        pause = fs.SprintCommand.pause()
        idle = fs.reduce(fs.SprintState(), pause, TEST_CONFIG)
        self.assertFalse(idle.applied)
        started = fs.reduce(idle.state, fs.SprintCommand.start(), TEST_CONFIG)
        replay = fs.reduce(started.state, pause, TEST_CONFIG)
        self.assertTrue(replay.duplicate)
        self.assertEqual(replay.state.phase, fs.Phase.FOCUS_RUNNING)

    def test_command_id_history_is_bounded(self) -> None:
        state = fs.SprintState()
        identifiers: list[str] = []
        for _ in range(12):
            cmd = fs.SprintCommand.pause()
            identifiers.append(cmd.command_id)
            state = fs.reduce(state, cmd, TEST_CONFIG).state
        self.assertEqual(state.consumed_command_ids, tuple(identifiers[-8:]))


class EngineTests(unittest.TestCase):
    def make_engine(self, clock: FakeClock) -> fs.FocusSprintEngine:
        return fs.FocusSprintEngine(
            TEST_CONFIG,
            wall_clock=clock.wall_time,
            monotonic_clock=clock.monotonic_time,
        )

    def test_engine_uses_injected_monotonic_clock(self) -> None:
        clock = FakeClock()
        engine = self.make_engine(clock)
        engine.dispatch(fs.SprintCommand.start())
        clock.advance(10, wall_seconds=2)
        result = engine.tick()
        self.assertEqual(result.state.phase, fs.Phase.FOCUS_DONE_CUE)
        self.assertEqual(result.state.remaining_seconds, 5)

    def test_engine_pause_discards_no_time_and_does_not_auto_resume(self) -> None:
        clock = FakeClock()
        engine = self.make_engine(clock)
        engine.dispatch(fs.SprintCommand.start())
        clock.advance(3)
        paused = engine.dispatch(fs.SprintCommand.pause()).state
        self.assertEqual(paused.remaining_seconds, 7)
        clock.advance(500)
        self.assertEqual(engine.tick().state.remaining_seconds, 7)
        resumed = engine.dispatch(fs.SprintCommand.resume()).state
        self.assertEqual(resumed.phase, fs.Phase.FOCUS_RUNNING)
        self.assertEqual(resumed.remaining_seconds, 7)

    def test_engine_syncs_elapsed_before_applying_command(self) -> None:
        clock = FakeClock()
        engine = self.make_engine(clock)
        engine.dispatch(fs.SprintCommand.start())
        clock.advance(4)
        result = engine.dispatch(fs.SprintCommand.add(3))
        self.assertEqual(result.state.remaining_seconds, 9)

    def test_quiet_boundary_pauses_and_requires_explicit_resume(self) -> None:
        clock = FakeClock()
        engine = self.make_engine(clock)
        engine.dispatch(fs.SprintCommand.start())
        clock.advance(2)
        quiet = engine.quiet_boundary()
        self.assertEqual(quiet.state.phase, fs.Phase.FOCUS_PAUSED)
        self.assertEqual(quiet.state.pause_reason, fs.PauseReason.QUIET)
        self.assertEqual(quiet.events[-1].kind, fs.EventType.QUIET_PAUSED)
        clock.advance(1_000)
        still_paused = engine.tick().state
        self.assertEqual(still_paused.remaining_seconds, 8)
        self.assertEqual(still_paused.pause_reason, fs.PauseReason.QUIET)

    def test_quiet_boundary_is_noop_while_idle(self) -> None:
        clock = FakeClock()
        engine = self.make_engine(clock)
        result = engine.quiet_boundary()
        self.assertFalse(result.applied)
        self.assertEqual(result.state.phase, fs.Phase.IDLE)


class RecoveryTests(unittest.TestCase):
    def running_state(self, *, phase: fs.Phase = fs.Phase.FOCUS_RUNNING) -> fs.SprintState:
        return fs.SprintState(
            phase=phase,
            remaining_seconds=200,
            wall_checkpoint=1_000,
            monotonic_checkpoint=500,
            cycle_count=1 if phase is not fs.Phase.FOCUS_RUNNING else 0,
        )

    def test_recovery_at_exactly_120_seconds_continues(self) -> None:
        result = fs.recover(
            self.running_state(),
            now_wall=1_120,
            now_monotonic=620,
            config=TEST_CONFIG,
        )
        self.assertEqual(result.state.phase, fs.Phase.FOCUS_RUNNING)
        self.assertEqual(result.state.remaining_seconds, 80)
        self.assertIsNone(result.state.pause_reason)

    def test_gap_longer_than_120_seconds_preserves_remaining_and_pauses(self) -> None:
        result = fs.recover(
            self.running_state(),
            now_wall=1_120.001,
            now_monotonic=620.001,
            config=TEST_CONFIG,
        )
        self.assertEqual(result.state.phase, fs.Phase.FOCUS_PAUSED)
        self.assertEqual(result.state.remaining_seconds, 200)
        self.assertEqual(result.state.pause_reason, fs.PauseReason.RECOVERED)
        self.assertEqual(result.events[0].kind, fs.EventType.RECOVERY_PAUSED)

    def test_backwards_wall_clock_enters_recovered_pause(self) -> None:
        result = fs.recover(
            self.running_state(),
            now_wall=999,
            now_monotonic=501,
            config=TEST_CONFIG,
        )
        self.assertEqual(result.state.phase, fs.Phase.FOCUS_PAUSED)
        self.assertEqual(result.state.remaining_seconds, 200)
        self.assertEqual(result.state.pause_reason, fs.PauseReason.RECOVERED)

    def test_sleep_drift_greater_than_60_seconds_pauses(self) -> None:
        result = fs.recover(
            self.running_state(),
            now_wall=1_100,
            now_monotonic=539,
            config=TEST_CONFIG,
        )
        self.assertEqual(result.state.phase, fs.Phase.FOCUS_PAUSED)
        self.assertEqual(result.state.remaining_seconds, 200)

    def test_sleep_drift_at_exactly_60_seconds_is_allowed(self) -> None:
        result = fs.recover(
            self.running_state(),
            now_wall=1_100,
            now_monotonic=540,
            config=TEST_CONFIG,
        )
        self.assertEqual(result.state.phase, fs.Phase.FOCUS_RUNNING)
        self.assertEqual(result.state.remaining_seconds, 160)

    def test_break_recovers_to_generic_break_pause(self) -> None:
        result = fs.recover(
            self.running_state(phase=fs.Phase.LONG_BREAK_RUNNING),
            now_wall=1_121,
            now_monotonic=621,
            config=TEST_CONFIG,
        )
        self.assertEqual(result.state.phase, fs.Phase.BREAK_PAUSED)
        self.assertEqual(result.state.pause_reason, fs.PauseReason.RECOVERED)
        self.assertEqual(result.state.remaining_seconds, 200)

    def test_existing_pause_stays_paused_without_relabeling(self) -> None:
        state = fs.SprintState(
            phase=fs.Phase.FOCUS_PAUSED,
            remaining_seconds=77,
            wall_checkpoint=1,
            monotonic_checkpoint=1,
            pause_reason=fs.PauseReason.QUIET,
        )
        result = fs.recover(
            state, now_wall=100_000, now_monotonic=100_000, config=TEST_CONFIG
        )
        self.assertEqual(result.state.remaining_seconds, 77)
        self.assertEqual(result.state.pause_reason, fs.PauseReason.QUIET)

    def test_unsafe_cue_recovery_is_explicitly_paused_at_saved_remainder(self) -> None:
        cue = fs.SprintState(
            phase=fs.Phase.FOCUS_DONE_CUE,
            remaining_seconds=3,
            wall_checkpoint=1_000,
            monotonic_checkpoint=500,
            cycle_count=1,
        )
        result = fs.recover(
            cue, now_wall=1_121, now_monotonic=621, config=TEST_CONFIG
        )
        self.assertEqual(result.state.phase, fs.Phase.FOCUS_DONE_CUE)
        self.assertEqual(result.state.remaining_seconds, 3)
        self.assertEqual(result.state.pause_reason, fs.PauseReason.RECOVERED)
        resumed = fs.reduce(result.state, fs.SprintCommand.resume(), TEST_CONFIG).state
        self.assertEqual(resumed.phase, fs.Phase.FOCUS_DONE_CUE)
        self.assertIsNone(resumed.pause_reason)


class PersistenceTests(unittest.TestCase):
    def test_schema_v1_round_trip_is_atomic_and_omits_private_label(self) -> None:
        identifier = str(uuid4())
        state = fs.SprintState(
            phase=fs.Phase.FOCUS_PAUSED,
            remaining_seconds=12.5,
            wall_checkpoint=1_234.5,
            monotonic_checkpoint=987.25,
            cycle_count=3,
            consumed_command_ids=(identifier,),
            pause_reason=fs.PauseReason.QUIET,
            label="confidential event title",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "caller" / "focus_state.json"
            self.assertEqual(fs.save_state(path, state), path)
            self.assertTrue(path.exists())
            self.assertEqual(list(path.parent.glob("*.tmp")), [])
            body = path.read_text(encoding="utf-8")
            self.assertNotIn("confidential", body)
            parsed = json.loads(body)
            # phase_total_seconds, paused_at_wall and schedule are all needed to
            # resume a paused sprint with the right durations, so they belong on
            # disk.  `label` is the one field that must never be written.
            self.assertEqual(
                set(parsed),
                {
                    "schema_version",
                    "phase",
                    "remaining_seconds",
                    "phase_total_seconds",
                    "wall_checkpoint",
                    "monotonic_checkpoint",
                    "cycle_count",
                    "consumed_command_ids",
                    "pause_reason",
                    "paused_at_wall",
                    "schedule",
                },
            )
            self.assertNotIn("label", parsed)
            loaded = fs.load_state(path)
        self.assertEqual(loaded, state)
        self.assertIsNone(loaded.label)

    def test_repr_and_events_never_contain_label(self) -> None:
        label = "Acquisition Codename Violet"
        started = fs.reduce(
            fs.SprintState(), fs.SprintCommand.start(label=label), TEST_CONFIG
        )
        self.assertNotIn(label, repr(started.state))
        self.assertNotIn(label, repr(started.events))
        self.assertNotIn(label, json.dumps(fs.state_to_dict(started.state)))

    def test_loader_rejects_bad_version_invalid_uuid_and_oversized_data(self) -> None:
        valid = fs.state_to_dict(fs.SprintState())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            wrong = dict(valid, schema_version=2)
            path.write_text(json.dumps(wrong), encoding="utf-8")
            with self.assertRaises(fs.PersistenceError):
                fs.load_state(path)

            invalid_id = dict(valid, consumed_command_ids=["not-a-uuid"])
            path.write_text(json.dumps(invalid_id), encoding="utf-8")
            with self.assertRaises(fs.PersistenceError):
                fs.load_state(path)

            path.write_bytes(b" " * (fs.MAX_STATE_BYTES + 1))
            with self.assertRaises(fs.PersistenceError):
                fs.load_state(path)

    def test_engine_restore_continues_short_trustworthy_gap(self) -> None:
        state = fs.SprintState(
            phase=fs.Phase.FOCUS_RUNNING,
            remaining_seconds=10,
            wall_checkpoint=1_000,
            monotonic_checkpoint=500,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            fs.save_state(path, state)
            clock = FakeClock(wall=1_003, monotonic=503)
            engine = fs.FocusSprintEngine.restore(
                path,
                TEST_CONFIG,
                wall_clock=clock.wall_time,
                monotonic_clock=clock.monotonic_time,
            )
        self.assertEqual(engine.state.phase, fs.Phase.FOCUS_RUNNING)
        self.assertEqual(engine.state.remaining_seconds, 7)

    def test_engine_restore_marks_unsafe_gap_as_recovered_pause(self) -> None:
        state = fs.SprintState(
            phase=fs.Phase.FOCUS_RUNNING,
            remaining_seconds=10,
            wall_checkpoint=1_000,
            monotonic_checkpoint=500,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            fs.save_state(path, state)
            clock = FakeClock(wall=1_121, monotonic=621)
            engine = fs.FocusSprintEngine.restore(
                path,
                TEST_CONFIG,
                wall_clock=clock.wall_time,
                monotonic_clock=clock.monotonic_time,
            )
        self.assertEqual(engine.state.phase, fs.Phase.FOCUS_PAUSED)
        self.assertEqual(engine.state.remaining_seconds, 10)
        self.assertEqual(engine.state.pause_reason, fs.PauseReason.RECOVERED)


if __name__ == "__main__":
    unittest.main()
