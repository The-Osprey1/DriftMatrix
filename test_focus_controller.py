"""Tests for the Focus Sprint controller."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from focus_control import (
    CommandType,
    FocusCommand,
    FocusCommandQueue,
    default_queue_dir,
)
import focus_controller as fc
from focus_sprint import (
    DEFAULT_CONFIG,
    EventType,
    FocusSprintEngine,
    Phase,
    SprintConfig,
    SprintState,
    save_state,
)
from panel_client import MutationPriority, PanelClientError


class FakeClock:
    def __init__(self, monotonic_now: float = 1000.0, wall_now: float = 1750000000.0) -> None:
        self._mono = monotonic_now
        self._wall = wall_now

    def monotonic(self) -> float:
        return self._mono

    def wall_time(self) -> float:
        return self._wall

    def advance(self, seconds: float) -> None:
        self._mono += seconds
        self._wall += seconds


class FakePanel:
    def __init__(self) -> None:
        self.pushes: list[tuple[str, dict, dict | None]] = []
        self.activations: list[tuple[str, Any]] = []
        self.deletes: list[str] = []
        self.fail_next_push: Exception | None = None

    def push_app(self, name: str, payload: dict, *, static_payload: dict | None = None) -> None:
        if self.fail_next_push is not None:
            err = self.fail_next_push
            self.fail_next_push = None
            raise err
        self.pushes.append((name, payload, static_payload))

    def activate_app(self, name: str, *, priority: Any = None) -> SimpleNamespace:
        self.activations.append((name, priority))
        return SimpleNamespace(sent=True)

    def delete_pushed_app(self, name: str) -> bool:
        self.deletes.append(name)
        return True


class FakeQueue:
    def __init__(self, commands: list[FocusCommand] | None = None) -> None:
        self._commands = list(commands or [])
        self.acked: list[FocusCommand] = []

    def read(self) -> tuple[FocusCommand, ...]:
        return tuple(self._commands)

    def ack(self, command: FocusCommand) -> bool:
        self.acked.append(command)
        if command in self._commands:
            self._commands.remove(command)
        return True

    def add(self, command: FocusCommand) -> None:
        self._commands.append(command)


def make_command(
    kind: CommandType,
    *,
    command_id: str = "00000000-0000-0000-0000-000000000001",
    minutes: int | None = None,
    preset: str | None = None,
    label: str | None = None,
) -> FocusCommand:
    args: dict[str, Any] = {}
    if minutes is not None:
        args["minutes"] = minutes
    if preset is not None:
        args["preset"] = preset
    if label is not None:
        args["label"] = label
    return FocusCommand(
        command=kind,
        id=command_id,
        created_at=datetime.now(timezone.utc),
        args=args,
    )


class FocusControllerTests(unittest.TestCase):
    def setUpself(self) -> tuple[FakePanel, FakeClock, FakeQueue, Path]:
        panel = FakePanel()
        clock = FakeClock()
        queue = FakeQueue()
        temp_dir = tempfile.mkdtemp()
        state_path = Path(temp_dir) / "focus_state.json"
        return panel, clock, queue, state_path

    def test_idle_tick_pushes_nothing(self) -> None:
        panel, clock, queue, state_path = self.setUpself()
        controller = fc.FocusController(
            panel,  # type: ignore[arg-type]
            queue=queue,  # type: ignore[arg-type]
            state_path=state_path,
            clock=clock,
        )

        tick = controller.tick()
        self.assertEqual(tick.phase, Phase.IDLE)
        self.assertFalse(tick.visible)
        self.assertFalse(tick.pushed)
        self.assertFalse(tick.deleted)
        self.assertTrue(tick.state_saved)
        self.assertEqual(len(panel.pushes), 0)
        self.assertTrue(state_path.exists())

    def test_start_command_enters_running_pushes_and_activates(self) -> None:
        panel, clock, queue, state_path = self.setUpself()
        cmd = make_command(CommandType.START, minutes=25)
        queue.add(cmd)

        controller = fc.FocusController(
            panel,  # type: ignore[arg-type]
            queue=queue,  # type: ignore[arg-type]
            state_path=state_path,
            clock=clock,
        )

        tick = controller.tick()
        self.assertEqual(tick.phase, Phase.FOCUS_RUNNING)
        self.assertTrue(tick.visible)
        self.assertEqual(tick.commands_processed, 1)
        self.assertTrue(tick.pushed)
        self.assertTrue(tick.activated)
        self.assertIn(cmd, queue.acked)

        self.assertEqual(len(panel.pushes), 1)
        app_name, primary, static = panel.pushes[0]
        self.assertEqual(app_name, "focus")
        self.assertIn("icon", primary)
        self.assertIn("draw", primary)

        self.assertEqual(len(panel.activations), 1)
        self.assertEqual(panel.activations[0][0], "focus")

    def test_pause_and_resume_lifecycle(self) -> None:
        panel, clock, queue, state_path = self.setUpself()
        controller = fc.FocusController(
            panel,  # type: ignore[arg-type]
            queue=queue,  # type: ignore[arg-type]
            state_path=state_path,
            clock=clock,
        )

        # Start
        queue.add(make_command(CommandType.START, command_id="00000000-0000-0000-0000-000000000001", minutes=25))
        controller.tick()

        # Pause
        queue.add(make_command(CommandType.PAUSE, command_id="00000000-0000-0000-0000-000000000002"))
        tick = controller.tick()
        self.assertEqual(tick.phase, Phase.FOCUS_PAUSED)
        self.assertTrue(tick.visible)

        # Resume
        queue.add(make_command(CommandType.RESUME, command_id="00000000-0000-0000-0000-000000000003"))
        tick = controller.tick()
        self.assertEqual(tick.phase, Phase.FOCUS_RUNNING)

    def test_stop_deletes_app_from_panel(self) -> None:
        panel, clock, queue, state_path = self.setUpself()
        controller = fc.FocusController(
            panel,  # type: ignore[arg-type]
            queue=queue,  # type: ignore[arg-type]
            state_path=state_path,
            clock=clock,
        )

        # Start
        queue.add(make_command(CommandType.START, command_id="00000000-0000-0000-0000-000000000001", minutes=25))
        controller.tick()
        self.assertTrue(controller.remote_present)

        # Stop
        queue.add(make_command(CommandType.STOP, command_id="00000000-0000-0000-0000-000000000002"))
        tick = controller.tick()
        self.assertEqual(tick.phase, Phase.IDLE)
        self.assertFalse(tick.visible)
        self.assertTrue(tick.deleted)
        self.assertIn("focus", panel.deletes)
        self.assertFalse(controller.remote_present)

    def test_panel_failure_defers_and_backs_off(self) -> None:
        panel, clock, queue, state_path = self.setUpself()
        panel.fail_next_push = PanelClientError("HTTP 500")

        controller = fc.FocusController(
            panel,  # type: ignore[arg-type]
            queue=queue,  # type: ignore[arg-type]
            state_path=state_path,
            clock=clock,
        )

        queue.add(make_command(CommandType.START, minutes=25))
        tick = controller.tick()
        self.assertTrue(tick.panel_deferred)
        self.assertFalse(tick.pushed)
        self.assertGreater(controller.panel_failures, 0)

        # Next tick immediately fails can_contact_panel because backoff is active
        tick2 = controller.tick()
        self.assertTrue(tick2.panel_deferred)

        # After advancing clock past backoff delay, push succeeds
        clock.advance(5.0)
        tick3 = controller.tick()
        self.assertTrue(tick3.pushed)
        self.assertEqual(controller.panel_failures, 0)

    def test_recovers_existing_saved_state(self) -> None:
        panel, clock, queue, state_path = self.setUpself()
        saved = SprintState(
            phase=Phase.FOCUS_RUNNING,
            remaining_seconds=1200.0,
            phase_total_seconds=1500.0,
            wall_checkpoint=clock.wall_time() - 10.0,
            monotonic_checkpoint=clock.monotonic() - 10.0,
            cycle_count=1,
            consumed_command_ids=("00000000-0000-0000-0000-000000000099",),
        )
        save_state(state_path, saved)

        controller = fc.FocusController(
            panel,  # type: ignore[arg-type]
            queue=queue,  # type: ignore[arg-type]
            state_path=state_path,
            clock=clock,
        )
        # Should have recovered and advanced by 10 seconds
        self.assertEqual(controller.engine.state.phase, Phase.FOCUS_RUNNING)
        self.assertAlmostEqual(controller.engine.state.remaining_seconds, 1190.0, places=0)


if __name__ == "__main__":
    unittest.main()
