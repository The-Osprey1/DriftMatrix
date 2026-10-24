"""Pure Focus Sprint timing, commands, recovery, and persistence.

The module intentionally knows nothing about AWTRIX, HTTP, artwork, or a UI.
It is suitable for a small PC-side controller: give :class:`FocusSprintEngine`
deterministic clocks, dispatch UUID-addressed commands, and persist its
immutable :class:`SprintState` at a caller-selected path.

Durations are represented as seconds (including fractional seconds) so exact
boundaries do not depend on display rounding.  The built-in presets are only
convenient starting points; callers may construct any valid
:class:`SprintConfig` they need.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
from types import MappingProxyType
from typing import Any, Final, TypeAlias
from uuid import UUID, uuid4


SCHEMA_VERSION: Final = 1
DEFAULT_CUE_SECONDS: Final = 5.0
DEFAULT_READY_SECONDS: Final = 2.0
DEFAULT_RECOVERY_WINDOW_SECONDS: Final = 120.0
DEFAULT_SLEEP_DRIFT_SECONDS: Final = 60.0
DEFAULT_ADD_SECONDS: Final = 300.0
DEFAULT_MAX_COMMAND_IDS: Final = 256
PAUSED_VISIBLE_SECONDS: Final = 30.0 * 60.0
MAX_STATE_BYTES: Final = 1_048_576


class Phase(str, Enum):
    """Every user-visible or timed phase in the Focus Sprint state machine."""

    IDLE = "idle"
    FOCUS_RUNNING = "focus_running"
    FOCUS_PAUSED = "focus_paused"
    FOCUS_DONE_CUE = "focus_done_cue"
    SHORT_BREAK_RUNNING = "short_break_running"
    LONG_BREAK_RUNNING = "long_break_running"
    BREAK_PAUSED = "break_paused"
    READY_NEXT = "ready_next"


SprintPhase = Phase


class PauseReason(str, Enum):
    """Why elapsed time is intentionally not being consumed."""

    USER = "user"
    QUIET = "quiet"
    RECOVERED = "recovered"
    AWAITING_START = "awaiting_start"


class CommandType(str, Enum):
    START = "start"
    PAUSE = "pause"
    RESUME = "resume"
    TOGGLE = "toggle"
    FINISH = "finish"
    SKIP = "skip"
    ADD = "add"
    STOP = "stop"


CommandKind = CommandType


class EventType(str, Enum):
    FOCUS_STARTED = "focus_started"
    PAUSED = "paused"
    RESUMED = "resumed"
    FOCUS_COMPLETED = "focus_completed"
    BREAK_STARTED = "break_started"
    BREAK_COMPLETED = "break_completed"
    READY_CLEARED = "ready_cleared"
    TIME_ADDED = "time_added"
    SKIPPED = "skipped"
    STOPPED = "stopped"
    QUIET_PAUSED = "quiet_paused"
    RECOVERY_PAUSED = "recovery_paused"


class PersistenceError(ValueError):
    """A persisted snapshot is missing, unsupported, or unsafe to use."""


def _finite_nonnegative(value: object, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite non-negative number")
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(
            f"{field_name} must be a finite non-negative number"
        ) from error
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{field_name} must be a finite non-negative number")
    return result


def _positive(value: object, field_name: str) -> float:
    result = _finite_nonnegative(value, field_name)
    if result <= 0:
        raise ValueError(f"{field_name} must be greater than zero")
    return result


def _canonical_uuid(value: object) -> str:
    try:
        parsed = value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise ValueError("command_id must be a UUID") from error
    return str(parsed)


@dataclass(frozen=True, slots=True)
class SprintPreset:
    """A named duration convenience, expressed entirely in seconds."""

    name: str
    focus_seconds: float
    short_break_seconds: float
    long_break_seconds: float
    long_break_every: int

    def __post_init__(self) -> None:
        name = str(self.name).strip().casefold()
        if not name:
            raise ValueError("preset name must not be empty")
        if isinstance(self.long_break_every, bool):
            raise ValueError("long_break_every must be a positive integer")
        cadence = int(self.long_break_every)
        if cadence != self.long_break_every or cadence <= 0:
            raise ValueError("long_break_every must be a positive integer")
        object.__setattr__(self, "name", name)
        object.__setattr__(
            self, "focus_seconds", _positive(self.focus_seconds, "focus_seconds")
        )
        object.__setattr__(
            self,
            "short_break_seconds",
            _positive(self.short_break_seconds, "short_break_seconds"),
        )
        object.__setattr__(
            self,
            "long_break_seconds",
            _positive(self.long_break_seconds, "long_break_seconds"),
        )
        object.__setattr__(self, "long_break_every", cadence)

    @classmethod
    def from_minutes(
        cls,
        name: str,
        *,
        focus: float,
        short_break: float,
        long_break: float,
        long_break_every: int,
    ) -> "SprintPreset":
        return cls(
            name=name,
            focus_seconds=float(focus) * 60.0,
            short_break_seconds=float(short_break) * 60.0,
            long_break_seconds=float(long_break) * 60.0,
            long_break_every=long_break_every,
        )


CLASSIC_PRESET = SprintPreset.from_minutes(
    "classic", focus=25, short_break=5, long_break=15, long_break_every=4
)
DEEP_PRESET = SprintPreset.from_minutes(
    "deep", focus=50, short_break=10, long_break=20, long_break_every=3
)
SPRINT_PRESET = SprintPreset.from_minutes(
    "sprint", focus=15, short_break=3, long_break=10, long_break_every=4
)
PRESETS: Mapping[str, SprintPreset] = MappingProxyType(
    {
        CLASSIC_PRESET.name: CLASSIC_PRESET,
        DEEP_PRESET.name: DEEP_PRESET,
        SPRINT_PRESET.name: SPRINT_PRESET,
    }
)


def get_preset(name: str) -> SprintPreset:
    """Return a built-in convenience preset by its case-insensitive name."""

    try:
        return PRESETS[str(name).strip().casefold()]
    except KeyError as error:
        raise ValueError(f"unknown Focus Sprint preset {name!r}") from error


@dataclass(frozen=True, slots=True)
class SprintConfig:
    """All policy values needed by the pure reducer."""

    focus_seconds: float = CLASSIC_PRESET.focus_seconds
    short_break_seconds: float = CLASSIC_PRESET.short_break_seconds
    long_break_seconds: float = CLASSIC_PRESET.long_break_seconds
    long_break_every: int = CLASSIC_PRESET.long_break_every
    cue_seconds: float = DEFAULT_CUE_SECONDS
    ready_seconds: float = DEFAULT_READY_SECONDS
    auto_start_break: bool = True
    recovery_window_seconds: float = DEFAULT_RECOVERY_WINDOW_SECONDS
    sleep_drift_seconds: float = DEFAULT_SLEEP_DRIFT_SECONDS
    default_add_seconds: float = DEFAULT_ADD_SECONDS
    max_command_ids: int = DEFAULT_MAX_COMMAND_IDS

    def __post_init__(self) -> None:
        for name in (
            "focus_seconds",
            "short_break_seconds",
            "long_break_seconds",
            "cue_seconds",
            "ready_seconds",
            "recovery_window_seconds",
            "sleep_drift_seconds",
            "default_add_seconds",
        ):
            object.__setattr__(self, name, _positive(getattr(self, name), name))
        for name in ("long_break_every", "max_command_ids"):
            value = getattr(self, name)
            if isinstance(value, bool):
                raise ValueError(f"{name} must be a positive integer")
            integer = int(value)
            if integer != value or integer <= 0:
                raise ValueError(f"{name} must be a positive integer")
            object.__setattr__(self, name, integer)
        if not isinstance(self.auto_start_break, bool):
            raise ValueError("auto_start_break must be a boolean")

    @classmethod
    def from_preset(
        cls,
        preset: str | SprintPreset = "classic",
        **overrides: object,
    ) -> "SprintConfig":
        selected = get_preset(preset) if isinstance(preset, str) else preset
        if not isinstance(selected, SprintPreset):
            raise TypeError("preset must be a preset name or SprintPreset")
        values: dict[str, object] = {
            "focus_seconds": selected.focus_seconds,
            "short_break_seconds": selected.short_break_seconds,
            "long_break_seconds": selected.long_break_seconds,
            "long_break_every": selected.long_break_every,
        }
        values.update(overrides)
        return cls(**values)  # type: ignore[arg-type]

    def schedule(self, name: str = "custom") -> SprintPreset:
        """Freeze the work/rest cadence that must survive a restart."""

        return SprintPreset(
            name=name,
            focus_seconds=self.focus_seconds,
            short_break_seconds=self.short_break_seconds,
            long_break_seconds=self.long_break_seconds,
            long_break_every=self.long_break_every,
        )


DEFAULT_CONFIG = SprintConfig()


@dataclass(frozen=True, slots=True)
class SprintCommand:
    """One idempotent command from a PC controller.

    ``label`` is intentionally excluded from repr, equality, domain events, and
    persistence.  It is transient presentation context, not timer identity.
    """

    kind: CommandType
    command_id: str = field(default_factory=lambda: str(uuid4()))
    amount_seconds: float = 0.0
    label: str | None = field(default=None, repr=False, compare=False)
    preset: str | None = None

    def __post_init__(self) -> None:
        try:
            kind = self.kind if isinstance(self.kind, CommandType) else CommandType(self.kind)
        except (TypeError, ValueError) as error:
            raise ValueError("unknown Focus Sprint command") from error
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "command_id", _canonical_uuid(self.command_id))
        amount = _finite_nonnegative(self.amount_seconds, "amount_seconds")
        if kind is CommandType.ADD and amount <= 0:
            raise ValueError("ADD requires a positive amount_seconds")
        if kind not in {CommandType.ADD, CommandType.START} and amount != 0:
            raise ValueError("amount_seconds is only valid for START or ADD")
        object.__setattr__(self, "amount_seconds", amount)
        if self.label is not None:
            if not isinstance(self.label, str):
                raise ValueError("label must be text or None")
            label = self.label.strip()
            object.__setattr__(self, "label", label or None)
        if self.preset is not None:
            if kind is not CommandType.START:
                raise ValueError("preset is only valid for START")
            selected = get_preset(self.preset)
            object.__setattr__(self, "preset", selected.name)

    @property
    def command(self) -> CommandType:
        """Compatibility alias useful to JSON queue adapters."""

        return self.kind

    @property
    def id(self) -> str:
        return self.command_id

    @classmethod
    def start(
        cls,
        command_id: str | UUID | None = None,
        *,
        amount_seconds: float = 0.0,
        label: str | None = None,
        preset: str | None = None,
    ) -> "SprintCommand":
        return cls(
            CommandType.START,
            _new_or_given_id(command_id),
            amount_seconds=amount_seconds,
            label=label,
            preset=preset,
        )

    @classmethod
    def pause(cls, command_id: str | UUID | None = None) -> "SprintCommand":
        return cls(CommandType.PAUSE, _new_or_given_id(command_id))

    @classmethod
    def resume(cls, command_id: str | UUID | None = None) -> "SprintCommand":
        return cls(CommandType.RESUME, _new_or_given_id(command_id))

    @classmethod
    def toggle(cls, command_id: str | UUID | None = None) -> "SprintCommand":
        return cls(CommandType.TOGGLE, _new_or_given_id(command_id))

    @classmethod
    def finish(cls, command_id: str | UUID | None = None) -> "SprintCommand":
        return cls(CommandType.FINISH, _new_or_given_id(command_id))

    @classmethod
    def skip(cls, command_id: str | UUID | None = None) -> "SprintCommand":
        return cls(CommandType.SKIP, _new_or_given_id(command_id))

    @classmethod
    def add(
        cls,
        amount_seconds: float = DEFAULT_ADD_SECONDS,
        command_id: str | UUID | None = None,
    ) -> "SprintCommand":
        return cls(CommandType.ADD, _new_or_given_id(command_id), amount_seconds)

    @classmethod
    def stop(cls, command_id: str | UUID | None = None) -> "SprintCommand":
        return cls(CommandType.STOP, _new_or_given_id(command_id))

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "SprintCommand":
        """Parse the small, stable shape commonly used by command queues."""

        if not isinstance(value, Mapping):
            raise ValueError("command must be an object")
        raw_kind = value.get("kind", value.get("command"))
        try:
            kind = raw_kind if isinstance(raw_kind, CommandType) else CommandType(raw_kind)
        except (TypeError, ValueError) as error:
            raise ValueError("unknown Focus Sprint command") from error
        command_id = value.get("command_id", value.get("id"))
        if command_id is None:
            raise ValueError("command has no UUID id")
        args = value.get("args", {})
        if args is None:
            args = {}
        if not isinstance(args, Mapping):
            raise ValueError("command args must be an object")
        amount: object = 0.0
        if kind in {CommandType.START, CommandType.ADD}:
            amount = value.get("amount_seconds", args.get("seconds", 0.0))
        if kind in {CommandType.START, CommandType.ADD} and amount == 0.0 and "minutes" in args:
            amount = _finite_nonnegative(args["minutes"], "minutes") * 60.0
        label = value.get("label", args.get("label"))
        preset = value.get("preset", args.get("preset"))
        return cls(
            kind=kind,
            command_id=command_id,  # type: ignore[arg-type]
            amount_seconds=amount,  # type: ignore[arg-type]
            label=label,  # type: ignore[arg-type]
            preset=preset,  # type: ignore[arg-type]
        )


Command = SprintCommand


def _new_or_given_id(value: str | UUID | None) -> str:
    return str(uuid4()) if value is None else _canonical_uuid(value)


@dataclass(frozen=True, slots=True)
class SprintState:
    """Immutable reducer state and the complete persistence snapshot."""

    phase: Phase = Phase.IDLE
    remaining_seconds: float = 0.0
    phase_total_seconds: float | None = None
    wall_checkpoint: float = 0.0
    monotonic_checkpoint: float = 0.0
    cycle_count: int = 0
    consumed_command_ids: tuple[str, ...] = ()
    pause_reason: PauseReason | None = None
    paused_at_wall: float | None = None
    schedule: SprintPreset | None = None
    label: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        try:
            phase = self.phase if isinstance(self.phase, Phase) else Phase(self.phase)
        except (TypeError, ValueError) as error:
            raise ValueError("unknown Focus Sprint phase") from error
        object.__setattr__(self, "phase", phase)
        object.__setattr__(
            self,
            "remaining_seconds",
            _finite_nonnegative(self.remaining_seconds, "remaining_seconds"),
        )
        total = self.phase_total_seconds
        if total is None:
            total = 0.0 if phase is Phase.IDLE else self.remaining_seconds
        total = _finite_nonnegative(total, "phase_total_seconds")
        if total < self.remaining_seconds:
            raise ValueError("phase_total_seconds must be at least remaining_seconds")
        object.__setattr__(self, "phase_total_seconds", total)
        object.__setattr__(
            self,
            "wall_checkpoint",
            _finite_nonnegative(self.wall_checkpoint, "wall_checkpoint"),
        )
        object.__setattr__(
            self,
            "monotonic_checkpoint",
            _finite_nonnegative(self.monotonic_checkpoint, "monotonic_checkpoint"),
        )
        if isinstance(self.cycle_count, bool):
            raise ValueError("cycle_count must be a non-negative integer")
        cycles = int(self.cycle_count)
        if cycles != self.cycle_count or cycles < 0:
            raise ValueError("cycle_count must be a non-negative integer")
        object.__setattr__(self, "cycle_count", cycles)
        ids = tuple(_canonical_uuid(value) for value in self.consumed_command_ids)
        if len(ids) != len(set(ids)):
            raise ValueError("consumed_command_ids must not contain duplicates")
        object.__setattr__(self, "consumed_command_ids", ids)
        reason = self.pause_reason
        if reason is not None and not isinstance(reason, PauseReason):
            try:
                reason = PauseReason(reason)
            except (TypeError, ValueError) as error:
                raise ValueError("unknown pause reason") from error
            object.__setattr__(self, "pause_reason", reason)
        paused_at = self.paused_at_wall
        if paused_at is not None:
            paused_at = _finite_nonnegative(paused_at, "paused_at_wall")
            object.__setattr__(self, "paused_at_wall", paused_at)
        if self.schedule is not None and not isinstance(self.schedule, SprintPreset):
            raise ValueError("schedule must be SprintPreset or None")
        if phase is Phase.IDLE:
            if self.remaining_seconds != 0:
                raise ValueError("IDLE must have zero remaining_seconds")
            if total != 0:
                raise ValueError("IDLE must have zero phase_total_seconds")
            if reason is not None:
                raise ValueError("IDLE cannot have a pause reason")
            if paused_at is not None:
                raise ValueError("IDLE cannot have paused_at_wall")
        if phase in {Phase.FOCUS_PAUSED, Phase.BREAK_PAUSED} and reason is None:
            object.__setattr__(self, "pause_reason", PauseReason.USER)
            reason = PauseReason.USER
        if reason is not None and paused_at is None:
            object.__setattr__(self, "paused_at_wall", self.wall_checkpoint)
            paused_at = self.wall_checkpoint
        if reason is None and paused_at is not None:
            raise ValueError("paused_at_wall requires a pause reason")
        if self.label is not None:
            if not isinstance(self.label, str):
                raise ValueError("label must be text or None")
            object.__setattr__(self, "label", self.label.strip() or None)

    @property
    def remaining(self) -> float:
        return self.remaining_seconds

    @property
    def phase_total(self) -> float:
        # __post_init__ normalizes None to a float.
        return float(self.phase_total_seconds)

    @property
    def visible(self) -> bool:
        return self.is_display_eligible()

    @property
    def is_visible(self) -> bool:
        return self.visible

    @property
    def is_paused(self) -> bool:
        return self.phase in {Phase.FOCUS_PAUSED, Phase.BREAK_PAUSED} or (
            self.pause_reason is not None
        )

    def is_display_eligible(
        self,
        now_wall: float | None = None,
        *,
        paused_visible_seconds: float = PAUSED_VISIBLE_SECONDS,
    ) -> bool:
        """Whether a renderer should include this page in rotation.

        Timer state is retained after this returns false.  If no wall value is
        supplied, the latest state checkpoint is used, making the decision
        deterministic and persistence-friendly.
        """

        if self.phase is Phase.IDLE:
            return False
        if not self.is_paused:
            return True
        window = _positive(paused_visible_seconds, "paused_visible_seconds")
        wall = self.wall_checkpoint if now_wall is None else _finite_nonnegative(
            now_wall, "now_wall"
        )
        paused_at = self.paused_at_wall
        if paused_at is None:  # defensive; normalized by __post_init__
            return True
        return wall - paused_at < window

    @property
    def is_running(self) -> bool:
        return self.phase in {
            Phase.FOCUS_RUNNING,
            Phase.SHORT_BREAK_RUNNING,
            Phase.LONG_BREAK_RUNNING,
        } and self.pause_reason is None

    @classmethod
    def idle(
        cls,
        *,
        wall_checkpoint: float = 0.0,
        monotonic_checkpoint: float = 0.0,
        schedule: SprintPreset | None = None,
    ) -> "SprintState":
        return cls(
            wall_checkpoint=wall_checkpoint,
            monotonic_checkpoint=monotonic_checkpoint,
            schedule=schedule,
        )


State = SprintState


@dataclass(frozen=True, slots=True)
class SprintEvent:
    """Privacy-safe domain event; user-provided labels never appear here."""

    kind: EventType
    phase: Phase
    previous_phase: Phase | None = None
    amount_seconds: float = 0.0


DomainEvent = SprintEvent


@dataclass(frozen=True, slots=True)
class Transition:
    state: SprintState
    events: tuple[SprintEvent, ...] = ()
    applied: bool = True
    duplicate: bool = False


_FOCUS_PHASES = frozenset({Phase.FOCUS_RUNNING, Phase.FOCUS_PAUSED})
_BREAK_RUNNING_PHASES = frozenset(
    {Phase.SHORT_BREAK_RUNNING, Phase.LONG_BREAK_RUNNING}
)
_BREAK_PHASES = frozenset((*_BREAK_RUNNING_PHASES, Phase.BREAK_PAUSED))
_TIMED_PHASES = frozenset(
    {
        Phase.FOCUS_RUNNING,
        Phase.FOCUS_DONE_CUE,
        Phase.SHORT_BREAK_RUNNING,
        Phase.LONG_BREAK_RUNNING,
        Phase.READY_NEXT,
    }
)


def _checkpoint(
    state: SprintState,
    wall_checkpoint: float | None,
    monotonic_checkpoint: float | None,
) -> SprintState:
    updates: dict[str, float] = {}
    if wall_checkpoint is not None:
        updates["wall_checkpoint"] = _finite_nonnegative(
            wall_checkpoint, "wall_checkpoint"
        )
    if monotonic_checkpoint is not None:
        updates["monotonic_checkpoint"] = _finite_nonnegative(
            monotonic_checkpoint, "monotonic_checkpoint"
        )
    return replace(state, **updates) if updates else state


def _state_schedule(state: SprintState, config: SprintConfig) -> SprintPreset:
    return state.schedule if state.schedule is not None else config.schedule()


def _break_phase(
    cycle_count: int, config: SprintConfig, schedule: SprintPreset | None = None
) -> Phase:
    policy = schedule if schedule is not None else config.schedule()
    return (
        Phase.LONG_BREAK_RUNNING
        if cycle_count > 0 and cycle_count % policy.long_break_every == 0
        else Phase.SHORT_BREAK_RUNNING
    )


def _break_seconds(
    cycle_count: int, config: SprintConfig, schedule: SprintPreset | None = None
) -> float:
    policy = schedule if schedule is not None else config.schedule()
    return (
        policy.long_break_seconds
        if _break_phase(cycle_count, config, policy) is Phase.LONG_BREAK_RUNNING
        else policy.short_break_seconds
    )


def _begin_break(
    state: SprintState,
    config: SprintConfig,
    *,
    force_short: bool = False,
    respect_auto_start: bool = True,
    paused_at_wall: float | None = None,
) -> tuple[SprintState, SprintEvent]:
    schedule = _state_schedule(state, config)
    phase = Phase.SHORT_BREAK_RUNNING if force_short else _break_phase(
        state.cycle_count, config, schedule
    )
    duration = (
        schedule.short_break_seconds
        if phase is Phase.SHORT_BREAK_RUNNING
        else schedule.long_break_seconds
    )
    previous = state.phase
    if respect_auto_start and not config.auto_start_break:
        result = replace(
            state,
            phase=Phase.BREAK_PAUSED,
            remaining_seconds=duration,
            phase_total_seconds=duration,
            pause_reason=PauseReason.AWAITING_START,
            paused_at_wall=(
                state.wall_checkpoint if paused_at_wall is None else paused_at_wall
            ),
            schedule=schedule,
        )
    else:
        result = replace(
            state,
            phase=phase,
            remaining_seconds=duration,
            phase_total_seconds=duration,
            pause_reason=None,
            paused_at_wall=None,
            schedule=schedule,
        )
    return result, SprintEvent(EventType.BREAK_STARTED, result.phase, previous)


def _ready(state: SprintState, config: SprintConfig) -> SprintState:
    return replace(
        state,
        phase=Phase.READY_NEXT,
        remaining_seconds=config.ready_seconds,
        phase_total_seconds=config.ready_seconds,
        pause_reason=None,
        paused_at_wall=None,
        label=None,
    )


def advance(
    state: SprintState,
    elapsed_seconds: float,
    config: SprintConfig = DEFAULT_CONFIG,
    *,
    wall_checkpoint: float | None = None,
    monotonic_checkpoint: float | None = None,
) -> Transition:
    """Consume exact elapsed time and cross as many boundaries as necessary.

    Equality belongs to the new phase: at exactly ``remaining_seconds`` a focus
    is already in its five-second completion cue, a cue is already in break,
    and a completed break is already in ``READY_NEXT``.
    """

    if not isinstance(state, SprintState):
        raise TypeError("state must be SprintState")
    if not isinstance(config, SprintConfig):
        raise TypeError("config must be SprintConfig")
    elapsed = _finite_nonnegative(elapsed_seconds, "elapsed_seconds")
    current = state
    events: list[SprintEvent] = []

    # A recovered transient may retain its original phase with a pause reason.
    # Like the explicit *_PAUSED phases, it consumes no time until RESUME.
    if current.pause_reason is not None or current.phase not in _TIMED_PHASES:
        return Transition(
            _checkpoint(current, wall_checkpoint, monotonic_checkpoint),
            (),
            applied=False,
        )

    while current.phase in _TIMED_PHASES and current.pause_reason is None:
        remaining = current.remaining_seconds
        if elapsed < remaining:
            current = replace(current, remaining_seconds=remaining - elapsed)
            elapsed = 0.0
            break

        elapsed -= remaining
        previous = current.phase
        if previous is Phase.FOCUS_RUNNING:
            current = replace(
                current,
                phase=Phase.FOCUS_DONE_CUE,
                remaining_seconds=config.cue_seconds,
                phase_total_seconds=config.cue_seconds,
                cycle_count=current.cycle_count + 1,
                pause_reason=None,
                paused_at_wall=None,
            )
            events.append(
                SprintEvent(EventType.FOCUS_COMPLETED, current.phase, previous)
            )
        elif previous is Phase.FOCUS_DONE_CUE:
            current, event = _begin_break(
                current, config, paused_at_wall=wall_checkpoint
            )
            events.append(event)
            if current.phase is Phase.BREAK_PAUSED:
                elapsed = 0.0
                break
        elif previous in _BREAK_RUNNING_PHASES:
            current = _ready(current, config)
            events.append(
                SprintEvent(EventType.BREAK_COMPLETED, current.phase, previous)
            )
        elif previous is Phase.READY_NEXT:
            current = replace(
                current,
                phase=Phase.IDLE,
                remaining_seconds=0.0,
                phase_total_seconds=0.0,
                pause_reason=None,
                paused_at_wall=None,
                label=None,
            )
            events.append(SprintEvent(EventType.READY_CLEARED, current.phase, previous))
            elapsed = 0.0
            break
        else:  # pragma: no cover - guarded by _TIMED_PHASES
            break

    current = _checkpoint(current, wall_checkpoint, monotonic_checkpoint)
    return Transition(current, tuple(events), applied=current != state or bool(events))


def _consume_id(
    state: SprintState, command_id: str, config: SprintConfig
) -> SprintState:
    ids = (*state.consumed_command_ids, command_id)
    if len(ids) > config.max_command_ids:
        ids = ids[-config.max_command_ids :]
    return replace(state, consumed_command_ids=ids)


def _pause(
    state: SprintState,
    reason: PauseReason,
    *,
    paused_at_wall: float | None = None,
) -> tuple[SprintState, SprintEvent | None]:
    previous = state.phase
    pause_wall = state.wall_checkpoint if paused_at_wall is None else paused_at_wall
    if previous is Phase.FOCUS_RUNNING:
        result = replace(
            state,
            phase=Phase.FOCUS_PAUSED,
            pause_reason=reason,
            paused_at_wall=pause_wall,
        )
    elif previous in _BREAK_RUNNING_PHASES:
        result = replace(
            state,
            phase=Phase.BREAK_PAUSED,
            pause_reason=reason,
            paused_at_wall=pause_wall,
        )
    else:
        return state, None
    return result, SprintEvent(EventType.PAUSED, result.phase, previous)


def _resume(
    state: SprintState, config: SprintConfig
) -> tuple[SprintState, SprintEvent | None]:
    previous = state.phase
    if previous is Phase.FOCUS_PAUSED:
        result = replace(
            state,
            phase=Phase.FOCUS_RUNNING,
            pause_reason=None,
            paused_at_wall=None,
        )
    elif previous is Phase.BREAK_PAUSED:
        result = replace(
            state,
            phase=_break_phase(
                state.cycle_count, config, _state_schedule(state, config)
            ),
            pause_reason=None,
            paused_at_wall=None,
        )
    elif previous in {Phase.FOCUS_DONE_CUE, Phase.READY_NEXT} and (
        state.pause_reason is not None
    ):
        result = replace(state, pause_reason=None, paused_at_wall=None)
    else:
        return state, None
    return result, SprintEvent(EventType.RESUMED, result.phase, previous)


def reduce(
    state: SprintState,
    command: SprintCommand,
    config: SprintConfig = DEFAULT_CONFIG,
    *,
    wall_checkpoint: float | None = None,
    monotonic_checkpoint: float | None = None,
) -> Transition:
    """Apply one command without consulting a clock.

    Valid no-op commands are still consumed.  This is important: replaying an
    old PAUSE after a later START must never pause the newer session.
    """

    if not isinstance(state, SprintState):
        raise TypeError("state must be SprintState")
    if not isinstance(command, SprintCommand):
        raise TypeError("command must be SprintCommand")
    if not isinstance(config, SprintConfig):
        raise TypeError("config must be SprintConfig")
    if command.command_id in state.consumed_command_ids:
        return Transition(
            _checkpoint(state, wall_checkpoint, monotonic_checkpoint),
            (),
            applied=False,
            duplicate=True,
        )

    current = state
    events: list[SprintEvent] = []
    changed = False
    kind = command.kind

    if kind is CommandType.START:
        if current.phase in {Phase.IDLE, Phase.READY_NEXT}:
            previous = current.phase
            previous_schedule = current.schedule
            schedule = (
                get_preset(command.preset)
                if command.preset is not None
                else _state_schedule(current, config)
            )
            duration = command.amount_seconds or schedule.focus_seconds
            cycles = current.cycle_count
            if command.preset is not None and previous_schedule not in {None, schedule}:
                cycles = 0
            current = replace(
                current,
                phase=Phase.FOCUS_RUNNING,
                remaining_seconds=duration,
                phase_total_seconds=duration,
                cycle_count=cycles,
                pause_reason=None,
                paused_at_wall=None,
                schedule=schedule,
                label=command.label,
            )
            events.append(
                SprintEvent(EventType.FOCUS_STARTED, current.phase, previous)
            )
            changed = True
    elif kind is CommandType.PAUSE:
        current, event = _pause(
            current,
            PauseReason.USER,
            paused_at_wall=wall_checkpoint,
        )
        if event is not None:
            events.append(event)
            changed = True
    elif kind is CommandType.RESUME:
        current, event = _resume(current, config)
        if event is not None:
            events.append(event)
            changed = True
    elif kind is CommandType.TOGGLE:
        if current.phase in {
            Phase.FOCUS_RUNNING,
            Phase.SHORT_BREAK_RUNNING,
            Phase.LONG_BREAK_RUNNING,
        }:
            current, event = _pause(
                current,
                PauseReason.USER,
                paused_at_wall=wall_checkpoint,
            )
        elif current.is_paused:
            current, event = _resume(current, config)
        elif current.phase in {Phase.IDLE, Phase.READY_NEXT}:
            previous = current.phase
            schedule = _state_schedule(current, config)
            duration = command.amount_seconds or schedule.focus_seconds
            current = replace(
                current,
                phase=Phase.FOCUS_RUNNING,
                remaining_seconds=duration,
                phase_total_seconds=duration,
                pause_reason=None,
                paused_at_wall=None,
                schedule=schedule,
                label=command.label,
            )
            event = SprintEvent(EventType.FOCUS_STARTED, current.phase, previous)
        else:
            event = None
        if event is not None:
            events.append(event)
            changed = True
    elif kind is CommandType.FINISH:
        previous = current.phase
        if previous in _FOCUS_PHASES:
            current = replace(
                current,
                phase=Phase.FOCUS_DONE_CUE,
                remaining_seconds=config.cue_seconds,
                phase_total_seconds=config.cue_seconds,
                cycle_count=current.cycle_count + 1,
                pause_reason=None,
                paused_at_wall=None,
            )
            events.append(
                SprintEvent(EventType.FOCUS_COMPLETED, current.phase, previous)
            )
            changed = True
        elif previous is Phase.FOCUS_DONE_CUE:
            current, event = _begin_break(
                current,
                config,
                respect_auto_start=False,
                paused_at_wall=wall_checkpoint,
            )
            events.append(event)
            changed = True
        elif previous in _BREAK_PHASES:
            current = _ready(current, config)
            events.append(
                SprintEvent(EventType.BREAK_COMPLETED, current.phase, previous)
            )
            changed = True
        elif previous is Phase.READY_NEXT:
            current = replace(
                current,
                phase=Phase.IDLE,
                remaining_seconds=0.0,
                phase_total_seconds=0.0,
                pause_reason=None,
                paused_at_wall=None,
                label=None,
            )
            events.append(SprintEvent(EventType.READY_CLEARED, current.phase, previous))
            changed = True
    elif kind is CommandType.SKIP:
        previous = current.phase
        if previous in _FOCUS_PHASES:
            current, event = _begin_break(
                current,
                config,
                force_short=True,
                respect_auto_start=False,
                paused_at_wall=wall_checkpoint,
            )
            events.extend(
                (
                    SprintEvent(EventType.SKIPPED, current.phase, previous),
                    event,
                )
            )
            changed = True
        elif previous is Phase.FOCUS_DONE_CUE:
            current, event = _begin_break(
                current,
                config,
                respect_auto_start=False,
                paused_at_wall=wall_checkpoint,
            )
            events.extend(
                (
                    SprintEvent(EventType.SKIPPED, current.phase, previous),
                    event,
                )
            )
            changed = True
        elif previous in _BREAK_PHASES:
            current = _ready(current, config)
            events.append(SprintEvent(EventType.SKIPPED, current.phase, previous))
            changed = True
        elif previous is Phase.READY_NEXT:
            current = replace(
                current,
                phase=Phase.IDLE,
                remaining_seconds=0.0,
                phase_total_seconds=0.0,
                pause_reason=None,
                paused_at_wall=None,
                label=None,
            )
            events.append(SprintEvent(EventType.SKIPPED, current.phase, previous))
            changed = True
    elif kind is CommandType.ADD:
        if current.phase in (*_FOCUS_PHASES, *_BREAK_PHASES):
            previous = current.phase
            current = replace(
                current,
                remaining_seconds=current.remaining_seconds + command.amount_seconds,
                phase_total_seconds=current.phase_total + command.amount_seconds,
            )
            events.append(
                SprintEvent(
                    EventType.TIME_ADDED,
                    current.phase,
                    previous,
                    command.amount_seconds,
                )
            )
            changed = True
    elif kind is CommandType.STOP:
        previous = current.phase
        if previous is not Phase.IDLE or current.cycle_count != 0:
            current = replace(
                current,
                phase=Phase.IDLE,
                remaining_seconds=0.0,
                phase_total_seconds=0.0,
                cycle_count=0,
                pause_reason=None,
                paused_at_wall=None,
                label=None,
            )
            events.append(SprintEvent(EventType.STOPPED, current.phase, previous))
            changed = True

    current = _consume_id(current, command.command_id, config)
    current = _checkpoint(current, wall_checkpoint, monotonic_checkpoint)
    return Transition(current, tuple(events), applied=changed)


apply_command = reduce
reduce_command = reduce
advance_state = advance


def quiet_boundary(
    state: SprintState,
    *,
    wall_checkpoint: float | None = None,
    monotonic_checkpoint: float | None = None,
) -> Transition:
    """Pause a running work/rest timer for quiet hours.

    There is deliberately no matching automatic wake operation.  A later
    RESUME or TOGGLE must be an explicit command from the controller/user.
    """

    pause_wall = state.wall_checkpoint if wall_checkpoint is None else _finite_nonnegative(
        wall_checkpoint, "wall_checkpoint"
    )
    previous = state.phase
    current, event = _pause(
        state,
        PauseReason.QUIET,
        paused_at_wall=pause_wall,
    )
    if event is None and previous is Phase.FOCUS_DONE_CUE and not state.is_paused:
        current = replace(
            state,
            pause_reason=PauseReason.QUIET,
            paused_at_wall=pause_wall,
        )
        event = SprintEvent(EventType.PAUSED, current.phase, previous)
    elif previous is Phase.READY_NEXT:
        current = replace(
            state,
            phase=Phase.IDLE,
            remaining_seconds=0.0,
            phase_total_seconds=0.0,
            pause_reason=None,
            paused_at_wall=None,
            label=None,
        )
        current = _checkpoint(current, wall_checkpoint, monotonic_checkpoint)
        return Transition(
            current,
            (SprintEvent(EventType.READY_CLEARED, current.phase, previous),),
            applied=True,
        )
    events: tuple[SprintEvent, ...] = ()
    applied = event is not None
    if event is not None:
        events = (
            SprintEvent(
                EventType.QUIET_PAUSED,
                current.phase,
                event.previous_phase,
            ),
        )
    current = _checkpoint(current, wall_checkpoint, monotonic_checkpoint)
    return Transition(current, events, applied=applied)


pause_for_quiet = quiet_boundary


def recover(
    state: SprintState,
    *,
    now_wall: float,
    now_monotonic: float,
    config: SprintConfig = DEFAULT_CONFIG,
) -> Transition:
    """Reconcile one persisted snapshot with current wall/monotonic clocks.

    Active snapshots continue only when both clocks describe a trustworthy gap
    no longer than the recovery window.  A backward wall clock, backward
    monotonic clock, or excessive wall/monotonic drift preserves the last saved
    remaining value and converts the timer to an explicit recovered pause.
    """

    wall = _finite_nonnegative(now_wall, "now_wall")
    monotonic = _finite_nonnegative(now_monotonic, "now_monotonic")
    wall_gap = wall - state.wall_checkpoint
    monotonic_gap = monotonic - state.monotonic_checkpoint

    if state.phase is Phase.IDLE or state.is_paused:
        return Transition(
            replace(state, wall_checkpoint=wall, monotonic_checkpoint=monotonic),
            (),
            applied=False,
        )

    unsafe = (
        wall_gap < 0
        or monotonic_gap < 0
        or max(wall_gap, monotonic_gap) > config.recovery_window_seconds
        or abs(wall_gap - monotonic_gap) > config.sleep_drift_seconds
    )
    if unsafe:
        previous = state.phase
        if previous is Phase.FOCUS_RUNNING:
            paused = replace(
                state,
                phase=Phase.FOCUS_PAUSED,
                pause_reason=PauseReason.RECOVERED,
                paused_at_wall=wall,
                wall_checkpoint=wall,
                monotonic_checkpoint=monotonic,
            )
        elif previous in _BREAK_RUNNING_PHASES:
            paused = replace(
                state,
                phase=Phase.BREAK_PAUSED,
                pause_reason=PauseReason.RECOVERED,
                paused_at_wall=wall,
                wall_checkpoint=wall,
                monotonic_checkpoint=monotonic,
            )
        elif previous is Phase.FOCUS_DONE_CUE:
            # FOCUS_DONE_CUE has no paired enum.  Retain the exact cue
            # remainder and use pause_reason as its explicit stopped state.
            paused = replace(
                state,
                pause_reason=PauseReason.RECOVERED,
                paused_at_wall=wall,
                wall_checkpoint=wall,
                monotonic_checkpoint=monotonic,
            )
        else:  # stale READY_NEXT should be absent, never sticky after recovery
            paused = replace(
                state,
                phase=Phase.IDLE,
                remaining_seconds=0.0,
                phase_total_seconds=0.0,
                pause_reason=None,
                paused_at_wall=None,
                label=None,
                wall_checkpoint=wall,
                monotonic_checkpoint=monotonic,
            )
            return Transition(paused, (), applied=True)
        return Transition(
            paused,
            (SprintEvent(EventType.RECOVERY_PAUSED, paused.phase, previous),),
            applied=True,
        )

    # Monotonic time is the authoritative elapsed clock once the independent
    # wall checkpoint has established that this is the same trustworthy gap.
    return advance(
        state,
        monotonic_gap,
        config,
        wall_checkpoint=wall,
        monotonic_checkpoint=monotonic,
    )


recover_state = recover


def state_to_dict(state: SprintState) -> dict[str, object]:
    """Return privacy-safe schema-v1 data (never the transient label)."""

    return {
        "schema_version": SCHEMA_VERSION,
        "phase": state.phase.value,
        "remaining_seconds": state.remaining_seconds,
        "phase_total_seconds": state.phase_total,
        "wall_checkpoint": state.wall_checkpoint,
        "monotonic_checkpoint": state.monotonic_checkpoint,
        "cycle_count": state.cycle_count,
        "consumed_command_ids": list(state.consumed_command_ids),
        "pause_reason": (
            state.pause_reason.value if state.pause_reason is not None else None
        ),
        "paused_at_wall": state.paused_at_wall,
        "schedule": (
            {
                "name": state.schedule.name,
                "focus_seconds": state.schedule.focus_seconds,
                "short_break_seconds": state.schedule.short_break_seconds,
                "long_break_seconds": state.schedule.long_break_seconds,
                "long_break_every": state.schedule.long_break_every,
            }
            if state.schedule is not None
            else None
        ),
    }


def state_from_dict(value: Mapping[str, object]) -> SprintState:
    """Validate and construct a schema-v1 snapshot without echoing its data."""

    if not isinstance(value, Mapping):
        raise PersistenceError("Focus Sprint state must be a JSON object")
    version = value.get("schema_version")
    if version != SCHEMA_VERSION:
        raise PersistenceError("unsupported Focus Sprint state schema")
    required = (
        "phase",
        "remaining_seconds",
        "phase_total_seconds",
        "wall_checkpoint",
        "monotonic_checkpoint",
        "cycle_count",
        "consumed_command_ids",
        "paused_at_wall",
        "schedule",
    )
    if any(name not in value for name in required):
        raise PersistenceError("Focus Sprint state is missing required fields")
    command_ids = value["consumed_command_ids"]
    if not isinstance(command_ids, Sequence) or isinstance(command_ids, (str, bytes)):
        raise PersistenceError("Focus Sprint command history is invalid")
    raw_schedule = value["schedule"]
    if raw_schedule is not None and not isinstance(raw_schedule, Mapping):
        raise PersistenceError("Focus Sprint schedule is invalid")
    try:
        schedule = (
            SprintPreset(
                name=raw_schedule["name"],  # type: ignore[index,arg-type]
                focus_seconds=raw_schedule["focus_seconds"],  # type: ignore[index,arg-type]
                short_break_seconds=raw_schedule["short_break_seconds"],  # type: ignore[index,arg-type]
                long_break_seconds=raw_schedule["long_break_seconds"],  # type: ignore[index,arg-type]
                long_break_every=raw_schedule["long_break_every"],  # type: ignore[index,arg-type]
            )
            if raw_schedule is not None
            else None
        )
        return SprintState(
            phase=value["phase"],  # type: ignore[arg-type]
            remaining_seconds=value["remaining_seconds"],  # type: ignore[arg-type]
            phase_total_seconds=value["phase_total_seconds"],  # type: ignore[arg-type]
            wall_checkpoint=value["wall_checkpoint"],  # type: ignore[arg-type]
            monotonic_checkpoint=value["monotonic_checkpoint"],  # type: ignore[arg-type]
            cycle_count=value["cycle_count"],  # type: ignore[arg-type]
            consumed_command_ids=tuple(command_ids),  # type: ignore[arg-type]
            pause_reason=value.get("pause_reason"),  # type: ignore[arg-type]
            paused_at_wall=value["paused_at_wall"],  # type: ignore[arg-type]
            schedule=schedule,
        )
    except (TypeError, ValueError, OverflowError) as error:
        raise PersistenceError("Focus Sprint state contains invalid fields") from error


Pathish: TypeAlias = str | os.PathLike[str]


def save_state(path: Pathish, state: SprintState) -> Path:
    """Atomically persist *state* beside the exact caller-selected path.

    The temporary file is created in the destination directory so
    :func:`os.replace` stays atomic.  Mode 0600 is requested and the JSON omits
    all optional labels.
    """

    if not isinstance(state, SprintState):
        raise TypeError("state must be SprintState")
    destination = Path(path)
    if not destination.name:
        raise ValueError("state path must name a file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(
        state_to_dict(state),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        try:
            os.chmod(temporary_name, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                descriptor = -1
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        os.replace(temporary_name, destination)
        temporary_name = None
        try:
            os.chmod(destination, 0o600)
        except OSError:
            # ACL-backed platforms may not implement POSIX mode bits; the file
            # still inherited the caller-owned directory ACL.
            pass
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
    return destination


def load_state(path: Pathish) -> SprintState:
    source = Path(path)
    try:
        size = source.stat().st_size
        if size > MAX_STATE_BYTES:
            raise PersistenceError("Focus Sprint state file is too large")
        with source.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except PersistenceError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PersistenceError("Focus Sprint state could not be loaded") from error
    return state_from_dict(value)


dump_state = save_state
read_state = load_state


class FocusSprintEngine:
    """Small mutable shell around the pure reducer with injectable clocks."""

    def __init__(
        self,
        config: SprintConfig = DEFAULT_CONFIG,
        state: SprintState | None = None,
        *,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(config, SprintConfig):
            raise TypeError("config must be SprintConfig")
        if not callable(wall_clock) or not callable(monotonic_clock):
            raise TypeError("clocks must be callable")
        self.config = config
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._lock = threading.RLock()
        self.last_events: tuple[SprintEvent, ...] = ()
        if state is None:
            wall, monotonic = self._now()
            state = SprintState.idle(
                wall_checkpoint=wall,
                monotonic_checkpoint=monotonic,
                schedule=config.schedule(),
            )
        elif not isinstance(state, SprintState):
            raise TypeError("state must be SprintState")
        self._state = state

    def _now(self) -> tuple[float, float]:
        wall = _finite_nonnegative(self._wall_clock(), "wall clock")
        monotonic = _finite_nonnegative(self._monotonic_clock(), "monotonic clock")
        return wall, monotonic

    @property
    def state(self) -> SprintState:
        with self._lock:
            return self._state

    @property
    def snapshot(self) -> SprintState:
        return self.state

    def _tick_at(self, wall: float, monotonic: float) -> Transition:
        delta = monotonic - self._state.monotonic_checkpoint
        if delta < 0:
            previous = self._state.phase
            if previous is Phase.FOCUS_RUNNING:
                state = replace(
                    self._state,
                    phase=Phase.FOCUS_PAUSED,
                    pause_reason=PauseReason.RECOVERED,
                    paused_at_wall=wall,
                    wall_checkpoint=wall,
                    monotonic_checkpoint=monotonic,
                )
            elif previous in _BREAK_RUNNING_PHASES:
                state = replace(
                    self._state,
                    phase=Phase.BREAK_PAUSED,
                    pause_reason=PauseReason.RECOVERED,
                    paused_at_wall=wall,
                    wall_checkpoint=wall,
                    monotonic_checkpoint=monotonic,
                )
            else:
                state = replace(
                    self._state,
                    wall_checkpoint=wall,
                    monotonic_checkpoint=monotonic,
                )
            events = (
                SprintEvent(EventType.RECOVERY_PAUSED, state.phase, previous),
            ) if state.pause_reason is PauseReason.RECOVERED else ()
            return Transition(state, events, applied=bool(events))
        return advance(
            self._state,
            delta,
            self.config,
            wall_checkpoint=wall,
            monotonic_checkpoint=monotonic,
        )

    def tick(self) -> Transition:
        with self._lock:
            wall, monotonic = self._now()
            transition = self._tick_at(wall, monotonic)
            self._state = transition.state
            self.last_events = transition.events
            return transition

    def dispatch(self, command: SprintCommand | Mapping[str, object]) -> Transition:
        if isinstance(command, Mapping):
            command = SprintCommand.from_mapping(command)
        if not isinstance(command, SprintCommand):
            raise TypeError("command must be SprintCommand or a command mapping")
        with self._lock:
            wall, monotonic = self._now()
            elapsed = self._tick_at(wall, monotonic)
            self._state = elapsed.state
            commanded = reduce(
                self._state,
                command,
                self.config,
                wall_checkpoint=wall,
                monotonic_checkpoint=monotonic,
            )
            self._state = commanded.state
            combined = Transition(
                self._state,
                (*elapsed.events, *commanded.events),
                applied=commanded.applied,
                duplicate=commanded.duplicate,
            )
            self.last_events = combined.events
            return combined

    apply = dispatch

    def quiet_boundary(self) -> Transition:
        with self._lock:
            wall, monotonic = self._now()
            elapsed = self._tick_at(wall, monotonic)
            self._state = elapsed.state
            quiet = quiet_boundary(
                self._state,
                wall_checkpoint=wall,
                monotonic_checkpoint=monotonic,
            )
            self._state = quiet.state
            combined = Transition(
                self._state,
                (*elapsed.events, *quiet.events),
                applied=quiet.applied,
            )
            self.last_events = combined.events
            return combined

    pause_for_quiet = quiet_boundary

    def save(self, path: Pathish) -> Path:
        with self._lock:
            self.tick()
            return save_state(path, self._state)

    @classmethod
    def restore(
        cls,
        path: Pathish,
        config: SprintConfig = DEFAULT_CONFIG,
        *,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> "FocusSprintEngine":
        saved = load_state(path)
        wall = _finite_nonnegative(wall_clock(), "wall clock")
        monotonic = _finite_nonnegative(monotonic_clock(), "monotonic clock")
        restored = recover(
            saved,
            now_wall=wall,
            now_monotonic=monotonic,
            config=config,
        )
        engine = cls(
            config=config,
            state=restored.state,
            wall_clock=wall_clock,
            monotonic_clock=monotonic_clock,
        )
        engine.last_events = restored.events
        return engine

    load = restore


SprintEngine = FocusSprintEngine


__all__ = [
    "CLASSIC_PRESET",
    "Command",
    "CommandKind",
    "CommandType",
    "DEEP_PRESET",
    "DEFAULT_CONFIG",
    "DomainEvent",
    "EventType",
    "FocusSprintEngine",
    "PAUSED_VISIBLE_SECONDS",
    "PRESETS",
    "PauseReason",
    "PersistenceError",
    "Phase",
    "SCHEMA_VERSION",
    "SPRINT_PRESET",
    "SprintCommand",
    "SprintConfig",
    "SprintEngine",
    "SprintEvent",
    "SprintPhase",
    "SprintPreset",
    "SprintState",
    "State",
    "Transition",
    "advance",
    "advance_state",
    "apply_command",
    "dump_state",
    "get_preset",
    "load_state",
    "pause_for_quiet",
    "quiet_boundary",
    "read_state",
    "recover",
    "recover_state",
    "reduce",
    "reduce_command",
    "save_state",
    "state_from_dict",
    "state_to_dict",
]
