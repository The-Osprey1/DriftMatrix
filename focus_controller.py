"""Safe orchestration for the PC-hosted AWTRIX ``Focus Sprint`` app.

This module joins the Focus Sprint pure timing engine, command queue,
state persistence, 32x8 artwork, and panel transport into one cohesive
service for the Utility Hub:

* atomic file-based command ingestion via :class:`FocusCommandQueue`;
* pure state machine execution via :class:`FocusSprintEngine`;
* safe atomic state persistence via :func:`save_state`;
* 32x8 compact payload generation via :mod:`focus_sprint_preview`;
* bounded panel mutations, activation cooldown, and automatic deletion on idle.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
import logging
import math
from pathlib import Path
import time
from typing import Any, Protocol

from focus_control import (
    CommandType,
    FocusCommand,
    FocusCommandQueue,
    default_queue_dir,
    default_state_path,
)
from focus_sprint import (
    DEFAULT_CONFIG,
    EventType,
    FocusSprintEngine,
    Phase,
    SprintCommand,
    SprintConfig,
    SprintEvent,
    SprintState,
    load_state,
    recover_state,
    save_state,
)
import focus_sprint_preview as art
from panel_client import (
    ActivationOutcome,
    MutationPriority,
    PanelClient,
    PanelClientError,
    SystemClock,
)


APP_NAME = "focus"
DEFAULT_REFRESH_SECONDS = 5.0
DEFAULT_STATIC_REFRESH_SECONDS = 15.0
DEFAULT_LIFETIME_SECONDS = 20.0
DEFAULT_PAUSED_VISIBILITY_SECONDS = 30 * 60.0
DEFAULT_PANEL_BACKOFF_SECONDS = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)

ACTIVATION_EVENTS = frozenset(
    {
        EventType.FOCUS_STARTED,
        EventType.FOCUS_COMPLETED,
        EventType.BREAK_STARTED,
    }
)


class ControllerClock(Protocol):
    """Clocks needed by the controller, injectable for deterministic testing."""

    def monotonic(self) -> float: ...

    def wall_time(self) -> float: ...


class SystemControllerClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def wall_time(self) -> float:
        return time.time()


@dataclass(frozen=True)
class FocusPolicy:
    """Operating limits and timing policy for the Focus Sprint service."""

    motion: str = "calm"
    refresh_seconds: float = DEFAULT_REFRESH_SECONDS
    static_refresh_seconds: float = DEFAULT_STATIC_REFRESH_SECONDS
    lifetime_seconds: float = DEFAULT_LIFETIME_SECONDS
    paused_visibility_seconds: float = DEFAULT_PAUSED_VISIBILITY_SECONDS
    panel_backoff_seconds: tuple[float, ...] = DEFAULT_PANEL_BACKOFF_SECONDS

    def __post_init__(self) -> None:
        for name in (
            "refresh_seconds",
            "static_refresh_seconds",
            "lifetime_seconds",
            "paused_visibility_seconds",
        ):
            value = getattr(self, name)
            if not math.isfinite(float(value)) or value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.motion not in {"calm", "static"}:
            raise ValueError("motion must be 'calm' or 'static'")


@dataclass(frozen=True)
class FocusTick:
    """Observable outcome of one Focus Sprint controller tick."""

    phase: Phase
    remaining_seconds: float
    visible: bool
    commands_processed: int = 0
    pushed: bool = False
    activated: bool = False
    deleted: bool = False
    state_saved: bool = False
    panel_deferred: bool = False
    events: tuple[SprintEvent, ...] = ()


def _focus_command_to_sprint_command(command: FocusCommand) -> SprintCommand:
    kind = CommandType(command.command.value)
    amount_seconds = 0.0
    if "minutes" in command.args:
        amount_seconds = float(command.args["minutes"]) * 60.0
    preset = command.args.get("preset")
    label = command.args.get("label")
    return SprintCommand(
        kind=kind,
        command_id=command.id,
        amount_seconds=amount_seconds,
        label=label,
        preset=preset,
    )


class FocusController:
    """Orchestrate queue ingestion, timing, persistence, and panel output."""

    def __init__(
        self,
        panel: PanelClient,
        *,
        policy: FocusPolicy | None = None,
        queue: FocusCommandQueue | None = None,
        state_path: Path | None = None,
        engine: FocusSprintEngine | None = None,
        clock: ControllerClock | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.panel = panel
        self.policy = policy or FocusPolicy()
        self.clock = clock or SystemControllerClock()
        self.logger = logger or logging.getLogger("awtrix.utility_hub.focus")

        self.queue = queue if queue is not None else FocusCommandQueue(default_queue_dir())
        self.state_path = state_path if state_path is not None else default_state_path()

        if engine is not None:
            self.engine = engine
        else:
            initial_state: SprintState | None = None
            if self.state_path.exists():
                try:
                    loaded = load_state(self.state_path)
                    wall = self._wall_time()
                    mono = self.clock.monotonic()
                    recovered = recover_state(loaded, now_wall=wall, now_monotonic=mono)
                    initial_state = recovered.state
                except Exception as error:
                    self.logger.warning("could not recover existing focus state: %s", error)
            self.engine = FocusSprintEngine(
                config=DEFAULT_CONFIG,
                state=initial_state,
                wall_clock=self._wall_time,
                monotonic_clock=self.clock.monotonic,
            )

        self.remote_present = False
        self.last_push_at: float | None = None
        self.last_phase: Phase | None = None
        self.last_remaining_int: int | None = None
        self.panel_failures = 0
        self.next_panel_attempt_at = 0.0

    def _wall_time(self) -> float:
        if hasattr(self.clock, "wall_time"):
            return float(self.clock.wall_time())
        if hasattr(self.clock, "wall_now"):
            return float(self.clock.wall_now().timestamp())
        return float(self.clock.monotonic())

    def _can_contact_panel(self, now: float) -> bool:
        return now >= self.next_panel_attempt_at

    def _panel_failed(self, now: float, error: BaseException) -> None:
        self.panel_failures += 1
        index = min(self.panel_failures, len(self.policy.panel_backoff_seconds)) - 1
        delay = self.policy.panel_backoff_seconds[index]
        self.next_panel_attempt_at = now + delay
        if self.panel_failures in (1, 3, 5) or self.panel_failures % 12 == 0:
            self.logger.warning(
                "panel unavailable (%d); retry in %.0fs: %s",
                self.panel_failures,
                delay,
                error,
            )

    def _panel_succeeded(self, now: float) -> None:
        self.panel_failures = 0
        self.next_panel_attempt_at = now

    def _build_payloads(self, state: SprintState) -> tuple[dict[str, Any], dict[str, Any]]:
        animated = self.policy.motion == "calm"
        primary = art.build_payload(
            state.phase.value,
            remaining=state.remaining_seconds,
            total=state.phase_total,
            cycle=state.cycle_count,
            animated=animated,
        )
        static = art.build_payload(
            state.phase.value,
            remaining=state.remaining_seconds,
            total=state.phase_total,
            cycle=state.cycle_count,
            animated=False,
        )
        return primary, static

    def tick(self) -> FocusTick:
        now_mono = self.clock.monotonic()
        now_wall = self._wall_time()

        # 1. Ingest commands from the queue
        commands_processed = 0
        state_saved = False
        all_events: list[SprintEvent] = []
        try:
            pending_commands = self.queue.read()
        except Exception as error:
            self.logger.warning("unable to read focus command queue: %s", error)
            pending_commands = ()

        for cmd in pending_commands:
            try:
                sprint_cmd = _focus_command_to_sprint_command(cmd)
                transition = self.engine.dispatch(sprint_cmd)
                all_events.extend(transition.events)
                commands_processed += 1
            except Exception as error:
                self.logger.warning("failed to process focus command %s: %s", cmd.id, error)
                continue
            # Persist the consumed ID before deleting this exact queue entry.
            # Commit each command separately: a large batch must not evict an
            # earlier unacknowledged ID from the bounded replay history.
            try:
                save_state(self.state_path, self.engine.state)
                state_saved = True
            except Exception as error:
                state_saved = False
                self.logger.warning("unable to save focus state: %s", error)
                break
            try:
                if not self.queue.ack(cmd):
                    break
            except Exception as error:
                self.logger.warning("unable to acknowledge focus command: %s", error)
                break

        # 2. Advance engine if no commands were processed this tick
        if commands_processed == 0:
            transition = self.engine.tick()
            all_events.extend(transition.events)

        state = self.engine.state

        # 3. Persist ordinary clock advancement. Command ticks were committed
        # above, or deliberately left queued after a failed commit.
        if commands_processed == 0:
            try:
                save_state(self.state_path, state)
                state_saved = True
            except Exception as error:
                self.logger.warning("unable to save focus state: %s", error)

        # 4. Check display eligibility
        visible = state.is_display_eligible(
            now_wall,
            paused_visible_seconds=self.policy.paused_visibility_seconds,
        )

        pushed = False
        activated = False
        deleted = False
        panel_deferred = False

        if visible:
            interval = (
                self.policy.refresh_seconds
                if self.policy.motion == "calm"
                else self.policy.static_refresh_seconds
            )
            phase_changed = state.phase != self.last_phase
            current_rem_int = int(math.ceil(state.remaining_seconds))
            time_due = (
                self.last_push_at is None
                or (now_mono - self.last_push_at) >= interval
            )
            should_push = (
                phase_changed
                or time_due
                or not self.remote_present
                or bool(all_events)
            )

            if should_push:
                if not self._can_contact_panel(now_mono):
                    panel_deferred = True
                else:
                    primary_payload, static_payload = self._build_payloads(state)
                    try:
                        self.panel.push_app(
                            APP_NAME,
                            primary_payload,
                            static_payload=static_payload,
                        )
                        self._panel_succeeded(now_mono)
                        self.remote_present = True
                        self.last_push_at = now_mono
                        self.last_phase = state.phase
                        self.last_remaining_int = current_rem_int
                        pushed = True

                        # Activate once on meaningful stage transitions
                        has_activation_event = any(e.kind in ACTIVATION_EVENTS for e in all_events)
                        if has_activation_event or phase_changed:
                            outcome = self.panel.activate_app(
                                APP_NAME,
                                priority=MutationPriority.FOCUS,
                            )
                            activated = outcome.sent
                    except (PanelClientError, OSError, RuntimeError) as error:
                        self._panel_failed(now_mono, error)
                        panel_deferred = True
        else:
            # Not visible - remove if previously pushed
            if self.remote_present:
                if not self._can_contact_panel(now_mono):
                    panel_deferred = True
                else:
                    try:
                        self.panel.delete_pushed_app(APP_NAME)
                        self._panel_succeeded(now_mono)
                        self.remote_present = False
                        self.last_push_at = None
                        self.last_phase = None
                        self.last_remaining_int = None
                        deleted = True
                    except (PanelClientError, OSError, RuntimeError) as error:
                        self._panel_failed(now_mono, error)
                        panel_deferred = True

        return FocusTick(
            phase=state.phase,
            remaining_seconds=state.remaining_seconds,
            visible=visible,
            commands_processed=commands_processed,
            pushed=pushed,
            activated=activated,
            deleted=deleted,
            state_saved=state_saved,
            panel_deferred=panel_deferred,
            events=tuple(all_events),
        )
