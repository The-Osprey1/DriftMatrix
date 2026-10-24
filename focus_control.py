"""Local, privacy-safe command interface for the AWTRIX Focus Sprint.

This module deliberately has no panel, HTTP, or service imports.  Commands are
small JSON envelopes written atomically to a local queue.  The Focus Sprint
controller can consume that queue in its own process and acknowledge only the
exact file it successfully handled.

The command-line surface is::

    focus_control.py start [MINUTES] [--label TEXT]
                           [--preset classic|deep|sprint]
    focus_control.py pause|resume|toggle|finish|skip|stop
    focus_control.py add MINUTES
    focus_control.py status

By default, commands live in ``%LOCALAPPDATA%\\AWTRIX\\focus_commands`` and the
read-only status command inspects ``%LOCALAPPDATA%\\AWTRIX\\focus_state.json``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence, TextIO
import unicodedata
import uuid


SCHEMA_VERSION = 1
STATE_SCHEMA_VERSION = 1
MAX_COMMAND_BYTES = 4 * 1024
MAX_STATE_BYTES = 64 * 1024
MAX_LABEL_CHARS = 120
MIN_MINUTES = 1
MAX_START_MINUTES = 24 * 60
MAX_ADD_MINUTES = 24 * 60
QUEUE_DIRECTORY_NAME = "focus_commands"
STATE_FILENAME = "focus_state.json"
QUARANTINE_DIRECTORY_NAME = ".quarantine"

_UUID_FILENAME_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.json$"
)
_UTC_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)


class CommandType(str, Enum):
    START = "start"
    PAUSE = "pause"
    RESUME = "resume"
    TOGGLE = "toggle"
    FINISH = "finish"
    SKIP = "skip"
    ADD = "add"
    STOP = "stop"


class Preset(str, Enum):
    CLASSIC = "classic"
    DEEP = "deep"
    SPRINT = "sprint"


class FocusControlError(Exception):
    """Base class for safe, user-facing Focus Control failures."""


class InvalidCommandError(FocusControlError, ValueError):
    """A command failed strict schema or argument validation."""


class CommandQueueError(FocusControlError, OSError):
    """The queue could not be accessed safely."""


class UnsafeAcknowledgementError(CommandQueueError):
    """An acknowledgement did not identify the exact file that was read."""


class InvalidStateError(FocusControlError, ValueError):
    """The persisted status file is present but not trustworthy."""


def default_data_dir() -> Path:
    """Return the per-user AWTRIX data directory without creating it."""

    local = os.environ.get("LOCALAPPDATA")
    root = Path(local) if local else Path.home() / "AppData" / "Local"
    return root / "AWTRIX"


def default_queue_dir() -> Path:
    """Return the default command queue path without creating it."""

    return default_data_dir() / QUEUE_DIRECTORY_NAME


def default_state_path() -> Path:
    """Return the default state path without creating it."""

    return default_data_dir() / STATE_FILENAME


def sanitize_label(value: str, *, maximum_chars: int = MAX_LABEL_CHARS) -> str:
    """Return a bounded label with control/format characters made inert.

    Ordinary Unicode and spacing are preserved.  Unicode control, format, and
    surrogate code points are replaced with a single ordinary space.  The
    result is stripped and then capped by code point count.  Empty labels are
    rejected so an explicit label can never silently become misleading.
    """

    if type(value) is not str:
        raise InvalidCommandError("label must be text")
    if type(maximum_chars) is not int or maximum_chars <= 0:
        raise ValueError("maximum_chars must be a positive integer")
    safe = "".join(
        " " if unicodedata.category(character) in {"Cc", "Cf", "Cs"} else character
        for character in value
    ).strip()
    safe = safe[:maximum_chars].rstrip()
    if not safe:
        raise InvalidCommandError("label must contain visible text")
    return safe


def _canonical_command(value: str | CommandType) -> CommandType:
    if isinstance(value, CommandType):
        return value
    if type(value) is not str:
        raise InvalidCommandError("command must be text")
    try:
        return CommandType(value)
    except ValueError as error:
        raise InvalidCommandError("unknown focus command") from error


def _canonical_uuid(value: str | uuid.UUID) -> str:
    raw = str(value) if isinstance(value, uuid.UUID) else value
    if type(raw) is not str or len(raw) != 36:
        raise InvalidCommandError("command id must be a canonical UUID")
    try:
        parsed = uuid.UUID(raw)
    except (ValueError, AttributeError) as error:
        raise InvalidCommandError("command id must be a canonical UUID") from error
    if str(parsed) != raw:
        raise InvalidCommandError("command id must be a lowercase canonical UUID")
    return raw


def _is_canonical_uuid_without_error(value: str) -> bool:
    try:
        _canonical_uuid(value)
    except InvalidCommandError:
        return False
    return True


def _canonical_created_at(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise InvalidCommandError("created_at must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidCommandError("created_at must include a UTC offset")
    offset_seconds = value.utcoffset().total_seconds()
    if not math.isfinite(offset_seconds):
        raise InvalidCommandError("created_at has an invalid UTC offset")
    return value.astimezone(timezone.utc)


def _format_created_at(value: datetime) -> str:
    return _canonical_created_at(value).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_created_at(value: Any) -> datetime:
    if type(value) is not str or not _UTC_TIMESTAMP_RE.fullmatch(value):
        raise InvalidCommandError("created_at must be an ISO-8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise InvalidCommandError("created_at is not a real UTC timestamp") from error
    return _canonical_created_at(parsed)


def _validate_minutes(value: Any, *, maximum: int) -> int:
    # bool is intentionally excluded even though it subclasses int.
    if type(value) is not int or not MIN_MINUTES <= value <= maximum:
        raise InvalidCommandError(
            f"minutes must be an integer from {MIN_MINUTES} to {maximum}"
        )
    return value


def _normalize_args(
    command: CommandType,
    args: Mapping[str, Any],
    *,
    sanitize: bool,
) -> dict[str, Any]:
    if not isinstance(args, Mapping):
        raise InvalidCommandError("args must be an object")
    if any(type(key) is not str for key in args):
        raise InvalidCommandError("argument names must be text")

    values = dict(args)
    if command is CommandType.START:
        allowed = {"minutes", "preset", "label"}
        if set(values) - allowed:
            raise InvalidCommandError("start contains unsupported arguments")
        normalized: dict[str, Any] = {}
        if "minutes" in values:
            normalized["minutes"] = _validate_minutes(
                values["minutes"], maximum=MAX_START_MINUTES
            )
        if "preset" in values:
            preset = values["preset"]
            if type(preset) is not str or preset not in {item.value for item in Preset}:
                raise InvalidCommandError("unknown focus preset")
            normalized["preset"] = preset
        if "label" in values:
            raw_label = values["label"]
            safe_label = sanitize_label(raw_label)
            if not sanitize and safe_label != raw_label:
                raise InvalidCommandError("label is not in its safe canonical form")
            normalized["label"] = safe_label
        return normalized

    if command is CommandType.ADD:
        if set(values) != {"minutes"}:
            raise InvalidCommandError("add requires exactly one minutes argument")
        return {
            "minutes": _validate_minutes(values["minutes"], maximum=MAX_ADD_MINUTES)
        }

    if values:
        raise InvalidCommandError(f"{command.value} does not accept arguments")
    return {}


@dataclass(frozen=True, repr=False)
class FocusCommand:
    """A validated queue envelope.

    ``args`` may contain a private label, so the mapping is immutable and is
    always redacted by ``repr``.  The underscored receipt fields are attached
    only after an exact file has been written/read; they let acknowledgement
    fail closed if a path or file body changes.
    """

    id: str
    command: CommandType | str
    created_at: datetime
    args: Mapping[str, Any] = field(default_factory=dict, repr=False)
    schema_version: int = SCHEMA_VERSION
    _source_path: Path | None = field(default=None, repr=False, compare=False)
    _content_sha256: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise InvalidCommandError("unsupported command schema version")
        canonical_id = _canonical_uuid(self.id)
        canonical_command = _canonical_command(self.command)
        canonical_created_at = _canonical_created_at(self.created_at)
        canonical_args = _normalize_args(canonical_command, self.args, sanitize=False)
        object.__setattr__(self, "id", canonical_id)
        object.__setattr__(self, "command", canonical_command)
        object.__setattr__(self, "created_at", canonical_created_at)
        object.__setattr__(self, "args", MappingProxyType(canonical_args))

    @property
    def command_id(self) -> str:
        """Alias matching the domain ``SprintCommand`` field name."""

        return self.id

    @property
    def kind(self) -> CommandType:
        """Alias matching the domain ``SprintCommand`` field name."""

        return self.command  # type: ignore[return-value]

    @property
    def label(self) -> str | None:
        value = self.args.get("label")
        return value if isinstance(value, str) else None

    @property
    def minutes(self) -> int | None:
        value = self.args.get("minutes")
        return value if type(value) is int else None

    @property
    def amount_seconds(self) -> float:
        """Return the ADD amount in the domain command's seconds unit."""

        if self.command is CommandType.ADD and self.minutes is not None:
            return float(self.minutes * 60)
        return 0.0

    @classmethod
    def create(
        cls,
        command: str | CommandType,
        *,
        minutes: int | None = None,
        preset: str | Preset | None = None,
        label: str | None = None,
        command_id: str | uuid.UUID | None = None,
        created_at: datetime | None = None,
    ) -> "FocusCommand":
        kind = _canonical_command(command)
        raw_args: dict[str, Any] = {}
        if minutes is not None:
            raw_args["minutes"] = minutes
        if preset is not None:
            raw_args["preset"] = preset.value if isinstance(preset, Preset) else preset
        if label is not None:
            raw_args["label"] = label
        safe_args = _normalize_args(kind, raw_args, sanitize=True)
        identifier = _canonical_uuid(
            uuid.uuid4() if command_id is None else command_id
        )
        moment = _canonical_created_at(
            datetime.now(timezone.utc) if created_at is None else created_at
        )
        return cls(
            id=identifier,
            command=kind,
            created_at=moment,
            args=safe_args,
        )

    @classmethod
    def from_mapping(cls, value: Any) -> "FocusCommand":
        if type(value) is not dict:
            raise InvalidCommandError("command envelope must be an object")
        expected = {"schema_version", "id", "command", "created_at", "args"}
        if set(value) != expected:
            raise InvalidCommandError("command envelope has unexpected fields")
        if type(value["schema_version"]) is not int:
            raise InvalidCommandError("schema_version must be an integer")
        if type(value["args"]) is not dict:
            raise InvalidCommandError("args must be an object")
        return cls(
            schema_version=value["schema_version"],
            id=value["id"],
            command=value["command"],
            created_at=_parse_created_at(value["created_at"]),
            args=value["args"],
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "command": self.command.value,
            "created_at": _format_created_at(self.created_at),
            "args": dict(self.args),
        }

    def __repr__(self) -> str:
        arg_names = tuple(sorted(self.args))
        return (
            "FocusCommand("
            f"id={self.id!r}, command={self.command.value!r}, "
            f"created_at={_format_created_at(self.created_at)!r}, "
            f"arg_names={arg_names!r}, schema_version={self.schema_version!r})"
        )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidCommandError("JSON contains duplicate object keys")
        result[key] = value
    return result


def _reject_nonfinite_json(_: str) -> Any:
    raise InvalidCommandError("JSON contains a non-finite number")


def _load_json_object(body: bytes, *, context: str) -> dict[str, Any]:
    try:
        text = body.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_json,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidCommandError(f"{context} is not valid UTF-8 JSON") from error
    if type(value) is not dict:
        raise InvalidCommandError(f"{context} must contain one JSON object")
    return value


def _serialize_command(command: FocusCommand) -> bytes:
    try:
        body = json.dumps(
            command.to_mapping(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise InvalidCommandError("command cannot be encoded safely") from error
    if not body or len(body) > MAX_COMMAND_BYTES:
        raise InvalidCommandError("encoded command exceeds the size limit")
    return body


def _normalized_absolute(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


class FocusCommandQueue:
    """Atomic writer, strict reader, and fail-closed acknowledger."""

    def __init__(
        self,
        directory: str | os.PathLike[str] | None = None,
        *,
        quarantine_malformed: bool = True,
        id_factory: Callable[[], uuid.UUID | str] = uuid.uuid4,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        requested = Path(directory) if directory is not None else default_queue_dir()
        self.directory = Path(os.path.abspath(os.fspath(requested)))
        self.quarantine_malformed = bool(quarantine_malformed)
        self._id_factory = id_factory
        self._now = now or (lambda: datetime.now(timezone.utc))

    def enqueue(
        self,
        command: str | CommandType,
        *,
        minutes: int | None = None,
        preset: str | Preset | None = None,
        label: str | None = None,
    ) -> FocusCommand:
        envelope = FocusCommand.create(
            command,
            minutes=minutes,
            preset=preset,
            label=label,
            command_id=self._id_factory(),
            created_at=self._now(),
        )
        return self.write(envelope)

    def write(self, command: FocusCommand) -> FocusCommand:
        if not isinstance(command, FocusCommand):
            raise InvalidCommandError("write requires a FocusCommand")
        # Round-trip through the strict constructor before any filesystem write.
        command = FocusCommand.from_mapping(command.to_mapping())
        body = _serialize_command(command)
        digest = hashlib.sha256(body).hexdigest()
        target = self.directory / f"{command.id}.json"

        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            if not self.directory.is_dir():
                raise CommandQueueError("command queue path is not a directory")
            if target.exists():
                raise CommandQueueError("a command with this id already exists")
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".focus-command-", suffix=".tmp", dir=self.directory
            )
        except CommandQueueError:
            raise
        except OSError as error:
            raise CommandQueueError("unable to prepare command queue") from error

        temporary = Path(temporary_name)
        try:
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except OSError as error:
            raise CommandQueueError("unable to write focus command atomically") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except OSError:
                pass

        return replace(command, _source_path=target, _content_sha256=digest)

    def _read_limited(self, path: Path, maximum_bytes: int) -> bytes:
        try:
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise InvalidCommandError("queue entry is not a regular file")
            if metadata.st_size <= 0 or metadata.st_size > maximum_bytes:
                raise InvalidCommandError("queue entry exceeds the size limit")
            with path.open("rb") as handle:
                body = handle.read(maximum_bytes + 1)
        except InvalidCommandError:
            raise
        except OSError as error:
            raise CommandQueueError("unable to read queue entry") from error
        if not body or len(body) > maximum_bytes:
            raise InvalidCommandError("queue entry exceeds the size limit")
        return body

    def _read_one(self, path: Path) -> FocusCommand:
        if not _UUID_FILENAME_RE.fullmatch(path.name):
            raise InvalidCommandError("queue filename is not a UUID")
        body = self._read_limited(path, MAX_COMMAND_BYTES)
        command = FocusCommand.from_mapping(_load_json_object(body, context="command"))
        if path.name != f"{command.id}.json":
            raise InvalidCommandError("queue filename does not match command id")
        return replace(
            command,
            _source_path=path,
            _content_sha256=hashlib.sha256(body).hexdigest(),
        )

    def _is_exact_child(self, path: Path) -> bool:
        return (
            path.parent == self.directory
            and _normalized_absolute(path.parent) == _normalized_absolute(self.directory)
            and _normalized_absolute(path)
            == _normalized_absolute(self.directory / path.name)
        )

    def _quarantine(self, path: Path) -> None:
        # Only a direct regular-file child can be moved.  The destination is
        # opaque so a malicious filename can never enter diagnostics or logs.
        if not self.quarantine_malformed or not self._is_exact_child(path):
            return
        try:
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                return
            destination_dir = self.directory / QUARANTINE_DIRECTORY_NAME
            destination_dir.mkdir(parents=False, exist_ok=True)
            destination = destination_dir / f"{uuid.uuid4()}.invalid"
            os.replace(path, destination)
        except OSError:
            # Quarantine is best-effort.  A malformed file must never block
            # commands that follow it.
            return

    def read(self) -> tuple[FocusCommand, ...]:
        """Return every valid command in deterministic chronological order."""

        if not self.directory.exists():
            return ()
        if not self.directory.is_dir():
            raise CommandQueueError("command queue path is not a directory")
        try:
            entries = sorted(self.directory.iterdir(), key=lambda item: item.name)
        except OSError as error:
            raise CommandQueueError("unable to enumerate command queue") from error

        commands: list[FocusCommand] = []
        for path in entries:
            if path.name == QUARANTINE_DIRECTORY_NAME or not path.name.endswith(".json"):
                continue
            try:
                commands.append(self._read_one(path))
            except (InvalidCommandError, CommandQueueError):
                self._quarantine(path)
                continue
        commands.sort(key=lambda item: (item.created_at, item.id))
        return tuple(commands)

    # Explicit alias useful to service/controller code.
    read_commands = read

    def acknowledge(self, command: FocusCommand) -> bool:
        """Delete the exact, unchanged queue file represented by ``command``.

        A constructed command, an outside path, a changed body, a symlink, or a
        command read before a later replacement all fail closed.  Missing files
        return ``False`` so duplicate acknowledgement is harmless.
        """

        if not isinstance(command, FocusCommand):
            raise UnsafeAcknowledgementError("acknowledgement requires a queue receipt")
        path = command._source_path
        digest = command._content_sha256
        if path is None or digest is None or not self._is_exact_child(path):
            raise UnsafeAcknowledgementError("command has no safe queue receipt")
        expected = self.directory / f"{command.id}.json"
        if _normalized_absolute(path) != _normalized_absolute(expected):
            raise UnsafeAcknowledgementError("command receipt path does not match its id")
        if not path.exists():
            return False
        try:
            body = self._read_limited(path, MAX_COMMAND_BYTES)
        except InvalidCommandError as error:
            raise UnsafeAcknowledgementError("queue file changed before acknowledgement") from error
        except CommandQueueError as error:
            raise UnsafeAcknowledgementError("queue file is unavailable") from error
        if hashlib.sha256(body).hexdigest() != digest:
            raise UnsafeAcknowledgementError("queue file changed before acknowledgement")
        try:
            current = FocusCommand.from_mapping(_load_json_object(body, context="command"))
        except InvalidCommandError as error:
            raise UnsafeAcknowledgementError("queue file changed before acknowledgement") from error
        if current.id != command.id or current.command is not command.command:
            raise UnsafeAcknowledgementError("queue file changed before acknowledgement")
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError as error:
            raise CommandQueueError("unable to acknowledge focus command") from error
        return True

    # Short controller-friendly alias.
    ack = acknowledge


class StatusKind(str, Enum):
    IDLE = "idle"
    WORK = "work"
    REST = "rest"
    PAUSED = "paused"
    COMPLETE = "complete"
    READY = "ready"


@dataclass(frozen=True, repr=False)
class FocusStatus:
    kind: StatusKind
    remaining_seconds: int | None = None
    available: bool = True
    checkpoint: float | None = None
    cycle_count: int = 0
    consumed_command_ids: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return (
            "FocusStatus("
            f"kind={self.kind.value!r}, remaining_seconds={self.remaining_seconds!r}, "
            f"available={self.available!r})"
        )

    def summary(self) -> str:
        if not self.available:
            return "Focus Sprint: idle (no saved state)."
        if self.kind is StatusKind.IDLE:
            return "Focus Sprint: idle."
        if self.kind is StatusKind.COMPLETE:
            return "Focus Sprint: complete."
        if self.kind is StatusKind.READY:
            return "Focus Sprint: ready for the next focus."
        if self.remaining_seconds is None:
            return f"Focus Sprint: {self.kind.value}."
        minutes, seconds = divmod(max(0, self.remaining_seconds), 60)
        return f"Focus Sprint: {self.kind.value}, {minutes}:{seconds:02d} remaining."


def _nonnegative_state_seconds(value: Any) -> int:
    if type(value) not in {int, float} or isinstance(value, bool):
        raise InvalidStateError("remaining time must be numeric")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0 or numeric > 7 * 24 * 60 * 60:
        raise InvalidStateError("remaining time is outside the supported range")
    return int(math.ceil(numeric))


def _status_from_state_mapping(value: dict[str, Any]) -> FocusStatus:
    """Interpret the bounded public portion of the domain's state envelope.

    The state writer owns additional timestamps, counters, and an optional
    private label.  Status deliberately reads only schema/version, phase,
    pause state, and remaining seconds, and never places arbitrary state data
    into an error or representation.
    """

    if type(value.get("schema_version")) is not int:
        raise InvalidStateError("state schema version is missing")
    if value["schema_version"] != STATE_SCHEMA_VERSION:
        raise InvalidStateError("state schema version is unsupported")

    required = {
        "schema_version",
        "phase",
        "remaining_seconds",
        "wall_checkpoint",
        "monotonic_checkpoint",
        "cycle_count",
        "consumed_command_ids",
    }
    if not required.issubset(value):
        raise InvalidStateError("state payload is incomplete")

    phase = value["phase"]
    if type(phase) is not str:
        raise InvalidStateError("state phase is missing")
    phase_aliases = {
        "idle": StatusKind.IDLE,
        "focus_running": StatusKind.WORK,
        "focus_paused": StatusKind.PAUSED,
        "focus_done_cue": StatusKind.COMPLETE,
        "short_break_running": StatusKind.REST,
        "long_break_running": StatusKind.REST,
        "break_paused": StatusKind.PAUSED,
        "ready_next": StatusKind.READY,
    }
    try:
        kind = phase_aliases[phase]
    except KeyError as error:
        raise InvalidStateError("state phase is unsupported") from error

    remaining = _nonnegative_state_seconds(value["remaining_seconds"])
    for checkpoint_key in ("wall_checkpoint", "monotonic_checkpoint"):
        checkpoint = value[checkpoint_key]
        if type(checkpoint) not in {int, float} or isinstance(checkpoint, bool):
            raise InvalidStateError("state checkpoint must be numeric")
        if not math.isfinite(float(checkpoint)) or float(checkpoint) < 0:
            raise InvalidStateError("state checkpoint is invalid")

    cycle_count = value["cycle_count"]
    if type(cycle_count) is not int or cycle_count < 0:
        raise InvalidStateError("cycle count must be a non-negative integer")
    consumed_ids = value["consumed_command_ids"]
    if type(consumed_ids) is not list or len(consumed_ids) > 4096:
        raise InvalidStateError("consumed command ids must be a bounded list")
    if any(
        type(identifier) is not str
        or not _is_canonical_uuid_without_error(identifier)
        for identifier in consumed_ids
    ):
        raise InvalidStateError("consumed command id is invalid")
    if len(consumed_ids) != len(set(consumed_ids)):
        raise InvalidStateError("consumed command ids contain duplicates")

    pause_reason = value.get("pause_reason")
    if pause_reason not in {None, "user", "quiet", "recovered", "awaiting_start"}:
        raise InvalidStateError("pause reason is unsupported")

    if kind in {StatusKind.IDLE, StatusKind.COMPLETE, StatusKind.READY}:
        remaining = None
    return FocusStatus(
        kind=kind, remaining_seconds=remaining, available=True,
        checkpoint=float(value["wall_checkpoint"]), cycle_count=cycle_count,
        consumed_command_ids=tuple(consumed_ids),
    )


def read_focus_status(path: str | os.PathLike[str] | None = None) -> FocusStatus:
    """Read status without creating, modifying, or quarantining any file."""

    state_path = Path(path) if path is not None else default_state_path()
    try:
        metadata = state_path.lstat()
    except FileNotFoundError:
        return FocusStatus(StatusKind.IDLE, available=False)
    except OSError as error:
        raise InvalidStateError("focus state is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise InvalidStateError("focus state is not a regular file")
    if metadata.st_size <= 0 or metadata.st_size > MAX_STATE_BYTES:
        raise InvalidStateError("focus state exceeds the size limit")
    try:
        with state_path.open("rb") as handle:
            body = handle.read(MAX_STATE_BYTES + 1)
    except OSError as error:
        raise InvalidStateError("focus state is unavailable") from error
    if not body or len(body) > MAX_STATE_BYTES:
        raise InvalidStateError("focus state exceeds the size limit")
    try:
        value = _load_json_object(body, context="state")
    except InvalidCommandError as error:
        raise InvalidStateError("focus state is invalid") from error
    return _status_from_state_mapping(value)


class _CLIExit(Exception):
    def __init__(self, status: int, message: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class _PrivateSafeArgumentParser(argparse.ArgumentParser):
    def exit(self, status: int = 0, message: str | None = None) -> None:
        raise _CLIExit(status, message)

    def error(self, message: str) -> None:
        # argparse error strings can include raw user input.  Do not retain or
        # print them because a mistyped private label could become an argument.
        raise _CLIExit(2, None)


def build_parser() -> argparse.ArgumentParser:
    parser = _PrivateSafeArgumentParser(
        description="Queue local commands for the AWTRIX Focus Sprint timer."
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    start = subparsers.add_parser("start", help="start a focus sprint")
    start.add_argument("minutes", nargs="?", type=int)
    start.add_argument("--label")
    start.add_argument(
        "--preset",
        choices=tuple(item.value for item in Preset),
        default=Preset.CLASSIC.value,
    )

    for action in ("pause", "resume", "toggle", "finish", "skip", "stop"):
        subparsers.add_parser(action)

    add = subparsers.add_parser("add", help="extend the current phase")
    add.add_argument("minutes", type=int)
    subparsers.add_parser("status", help="show saved local timer status")
    return parser


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    queue: FocusCommandQueue | None = None,
    state_path: str | os.PathLike[str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the CLI and return a process-style status code."""

    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    parser = build_parser()
    try:
        arguments = parser.parse_args(argv)
    except _CLIExit as outcome:
        if outcome.status == 0 and outcome.message:
            output.write(outcome.message)
        elif outcome.status != 0:
            errors.write("Invalid focus command arguments. Use --help for usage.\n")
        return outcome.status

    if arguments.action == "status":
        try:
            status_value = read_focus_status(state_path)
        except InvalidStateError:
            errors.write("Focus Sprint status unavailable.\n")
            return 1
        output.write(status_value.summary() + "\n")
        return 0

    command_queue = queue or FocusCommandQueue()
    try:
        if arguments.action == "start":
            command_queue.enqueue(
                CommandType.START,
                minutes=arguments.minutes,
                preset=arguments.preset,
                label=arguments.label,
            )
        elif arguments.action == "add":
            command_queue.enqueue(CommandType.ADD, minutes=arguments.minutes)
        else:
            command_queue.enqueue(CommandType(arguments.action))
    except InvalidCommandError:
        errors.write("Invalid focus command arguments.\n")
        return 2
    except (CommandQueueError, OSError):
        errors.write("Unable to queue focus command.\n")
        return 1

    output.write(f"Queued Focus Sprint {arguments.action}.\n")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return run_cli(argv)


if __name__ == "__main__":
    raise SystemExit(main())
