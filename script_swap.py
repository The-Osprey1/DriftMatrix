r"""Replace one resident Berry script on the panel: guarded, journaled, resumable.

Built for the moon v2.0 -> v2.1 swap (2026-10-09), and general enough for any
resident script::

    C:/Users/devyn/anaconda3/python.exe script_swap.py moon weather/moon-panel.ax ^
        --expect-version 2.1 --rollback weather/moon-panel-v2.0.ax --rollback-version 2.0 ^
        --expect-sha256 90cd90e057a9 --rollback-sha256 f6b17ba25007 ^
        --require-dark --wait-for-nightly

Why this is a procedure and not one PUT (CLAUDE.md, "Panel memory")
-------------------------------------------------------------------
* An install needs about 3.5x the minified source as ONE contiguous block
  (3.33x has failed with OOM, 3.75x has worked).  The install is sent only
  with 3.5x (:data:`INSTALL_BLOCK_RATIO`); a ~10.9 KB moon needs ~38.2 KB,
  more than the ~32.7 KB the full four-script set leaves after a boot.  So
  replacing a resident script is: DELETE it, reboot (the other scripts
  restore without it and the block is fresh), PUT the minified source,
  reboot (everything restores together), verify.
* A failed install still stores the source: the PUT returns 200 with a
  non-null ``error``.  After ``out of memory`` the stored source compiles at
  the next boot, so one reboot and a fresh look decide it.  ``507
  insufficientStorage`` stores nothing.  That is why the rollback is sent
  with less room than an install (:func:`rollback_block`, max(3.0x, 30 KB)):
  an OOM reply there is fine, and the verification boot compiles it.
* ``DELETE /apps/{name}`` removes scripts too, so ``origin`` is checked to be
  ``script`` immediately before every delete.  Identity is ``meta.version``
  from ``GET /apps``, never a source comparison.
* Every write goes through :class:`panel_client.PanelClient` with source
  ``install``, so it is serialized with the other senders by ``panel_gate``
  and journaled in ``panel_events.jsonl``.  A real swap refuses to run with
  the gate off (``AWTRIX_PANEL_GATE=0`` or ``panel_gate.off``) unless
  ``--allow-ungated``, and holds ``script_swap.lock`` beside its report so
  only one swap runs at a time.

Procedure
---------
0. Preflight, no writes: both source files carry the expected ``# @version``
   (and, with ``--expect-sha256``/``--rollback-sha256``, minify to the pinned
   bytes); the panel answers and runs the expected firmware; the target is
   present, ``origin`` script, at the old version with no error (already at
   the new version: exit 0); no other script reports an error; this boot's
   log has its ``ntp:`` line; with ``--require-dark`` the matrix is off (and
   a few dark framebuffer reads are recorded as evidence); the Hub's
   maintenance is not running a reboot, its low-memory restart is not due,
   and, with ``--wait-for-nightly``, its nightly reboot has finished for
   tonight -- the flag waits out either instead of refusing (never race
   them); sizes and block requirements are worked out; a read-only snapshot
   is taken with :func:`panel_snapshot.capture_snapshot`, and if it reads the
   target back with bytes other than the rollback source, the run stops.
   ``--dry-run`` stops before the snapshot, prints the plan, and without
   ``--data-dir``/``--report`` writes no file.
1. Re-check what preflight relied on (fresh boot restored, matrix dark, no
   Hub reboot due), then DELETE the target (origin re-checked).
2. Reboot, wait for the uptime to roll back and for the log's
   ``scripts: N restored`` line (with the log unreadable: every expected
   script listed with no error at 30 s of uptime), then read the largest
   free block.  Below the requirement the new source is NOT sent: go to
   rollback.  With ``--require-dark``, a reboot is held while a person holds
   the matrix lit, and after 10 minutes the run pauses (re-run resumes).
3. Read the block once more, PUT the minified new source; check the HTTP
   status and the JSON ``error``.
4. Reboot, let everything restore, verify: target present, origin script,
   ``meta.version`` as expected, no error, a ``vm heap`` boot-log line for it,
   every pre-swap script back with no error.  A target that has dropped out
   of the rotation ends ``installed-needs-attention`` (exit 8), not ok and
   not rolled back (the rollback's own DELETE and PUT would do the same).
5. Fidelity (best effort, never blocks success unless the page is clearly
   broken): read the script back while the block allows and compare SHA-256,
   capture ``/display/screen`` while the target is current, compare with
   ``weather/moon_panel_sim.py`` when that exists.  "Clearly broken" is
   deliberately narrow: an error reported by the script, frames that are all
   black while the matrix is lit, or the moon's disc columns black in every
   frame taken while the disc must be on screen.  Only frames taken with the
   matrix lit before and after the capture, settled, count; with
   ``--require-dark`` the screen verdict is evidence only.
6. Any failure after the delete rolls back: delete the new script if present
   (retried every half second through a boot loop), reboot, PUT the old
   minified source once the block reaches its floor, reboot, verify.  A
   stalled NTP or restore ends the run instead of spending more reboots.  If
   the old version cannot be put back the run ends ``failed``, loudly, with
   the panel running the remaining scripts and the target page absent --
   safe, but a person has to act.  ``--rollback-now`` runs this step alone,
   from any state, and never sends the new source.

Every step is written to the JSON report (``%LOCALAPPDATA%\AWTRIX\
script_installs.json`` unless ``--report``/``--data-dir`` say otherwise)
*before* the write it describes, with a ``panel_state`` sentence saying what
the panel may look like if that is the last thing written.  Ctrl+C records
``interrupted`` with that sentence.  Re-running the same command resumes:
the newest earlier run for the same target, made with the same sources, that
sent (or was about to send) a write tells this tool that it deleted the
target, or that it was rolling back, and it carries on from what the panel
now reports.  Runs that wrote nothing are skipped, so a refused or
interrupted re-run never breaks that chain, and the report's history trim
never drops the entry the chain rests on.

Exit codes: 0 ok / already installed / dry run with nothing in the way /
check-only healthy; 3 aborted before any write by this run (a dry run that
found blockers, or a refusal -- the report says if an earlier run left the
panel mid-swap); 4 rolled back (new version not installed, old verified); 5
failed (panel degraded, a person must act); 6 interrupted or paused (re-run
resumes); 7 check-only found the installed script unhealthy (including out of
the rotation, or never on screen while lit); 8 installed and verified but a
person must act (out of the rotation).  2 is argparse's usage error.

``--check-only`` is read-only and meant for the morning after a night-time
install: while the matrix is dark the health log shows moon at ~42 FPS
against ~25 lit, which suggests ``draw()`` does not run in the dark, so a
night install cannot exercise ``draw()``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import importlib
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable, Mapping
from urllib.parse import quote

import requests

import panel_gate
import panel_maintenance as pm
import panel_snapshot
import sender_logging
from panel_client import (
    APPS_PATH,
    DELETE_APP_PATH,
    DEVICE_PATH,
    PanelClient,
    PanelClientError,
    PanelHTTPError,
    PanelPolicy,
    PanelTransportError,
    SystemClock,
)
from weather.minify_ax import minify


AWTRIX = panel_snapshot.AWTRIX
# Who panel_gate's journal names for this tool's writes.
GATE_SOURCE = "install"
SCRIPT_PATH = panel_snapshot.SCRIPT_PATH
LOGS_PATH = pm.LOGS_PATH
SCREEN_PATH = "/api/v1/display/screen"
APPLOOP_PATH = "/apploop.json"
EXPECTED_FIRMWARE = "1.1.2"
REPORT_NAME = "script_installs.json"
LOG_NAME = "script_swap.log"

# Contiguous block an install needs, as a multiple of the minified source.
# CLAUDE.md: ">= 3.5x"; 7,372 B failed with OOM at 3.33x (source stored; a
# reboot compiled it) and worked at 3.75x, 6,980 B got 507 at 2.35x and 1.6x
# (nothing stored) and worked at 3.96x.  No undersized PUT has panicked on
# 1.1.x, and an OOM reply is handled (the verification reboot compiles it).
INSTALL_BLOCK_RATIO = 3.5
# The rollback puts back what was running, so it accepts less room: an OOM
# reply still stores the source, which compiles at the verification boot
# (moon is first alphabetically, so it compiles into a fresh heap).  The floor
# stays clear of the ~22.5 KB block at which even a ~10 KB readback fails.
ROLLBACK_BLOCK_RATIO = 3.0
ROLLBACK_MIN_BLOCK = 30_000
# /api/v1/logs unreadable: every expected script listed with no error at this
# uptime counts as restored (the log has shown them restored by ~+5 s).
RESTORE_FALLBACK_UPTIME = 30
# Reads that follow a write wait out a short outage instead of giving up.
POST_WRITE_READ_SECONDS = 60.0
# A boot loop leaves ~1 s of HTTP per cycle; retry the DELETE this often.
BOOT_LOOP_RETRY_SECONDS = 0.5
# --require-dark: how long a reboot waits for a person to stop holding the
# matrix lit before the run pauses (resumable) instead.
DARK_WAIT_SECONDS = 600.0
DARK_POLL_SECONDS = 30.0
# Screen frames count as evidence only with the matrix lit before and after
# the capture, this long after a power change and after the boot-lit moment.
POWER_SETTLE_SECONDS = 15.0
BOOT_LIT_SECONDS = 20
DARK_SAMPLES = 3
LOCK_NAME = "script_swap.lock"
# A ~10 KB readback has worked with a block of 29,684 or more and failed with
# 22,516 or less (CLAUDE.md, "Readback of a ~10 KB script").
READBACK_MIN_BLOCK = 29_684
REQUEST_TIMEOUT_SECONDS = 15.0
BOOT_TIMEOUT_SECONDS = 300.0
# Scripts restore within seconds of NTP sync (2026-10-08: boot 21:35:25,
# "scripts: 4 restored" 21:35:30); the Hub's maintenance settles for 90 s.
SETTLE_TIMEOUT_SECONDS = 90.0
POLL_SECONDS = 3.0
NIGHTLY_POLL_SECONDS = 30.0
MAX_WAIT_MINUTES = 120.0
# How long a swap can keep the panel busy (three boots, settles, the fidelity
# watch) plus slack; a Hub reboot due inside this is a race.
RUN_MARGIN = timedelta(minutes=20)
# The Hub's own attempt takes boot (<= 300 s) + settle (90 s); an in-progress
# entry older than this was left by a Hub that died mid-run.
STALE_IN_PROGRESS = timedelta(minutes=20)
# Wall clock minus uptime is stable to a second or two within one boot; a
# panel that crashes at restore is back about 15 s later.
BOOT_EPOCH_TOLERANCE_SECONDS = 8.0
MAX_SETTLE_RESTARTS = 2
# Younger than this, the scripts may still be restoring.
FRESH_BOOT_SECONDS = 120
ROLLBACK_REBOOTS = 2
SCREEN_SECONDS = 150.0
MAX_CAPTURES = 12
MIN_BLACK_CAPTURES = 4
MIN_DISC_CAPTURES = 2
# moon-panel.ax draws its disc at x = 12..19 while the camera is home, which
# is the first 3.8 s of every visit (on_show() resets the 11 s cycle).
DISC_COLUMNS = range(12, 20)
DISC_X = 12
# A frame counts as "disc at rest" when the visit was seen to start (the poll
# before saw another app) and the frame came 0.4-1.6 s after that sighting:
# with polls 2 s apart the true age is then 0.4-3.6 s, inside the 3.8 s rest.
DISC_WINDOW_SECONDS = (0.4, 1.6)
# Minute-boundary differences between the PC's and the panel's clock move the
# horizon tint by a step; allow that much before calling the disc different.
SIM_TOLERANCE_PIXELS = 2
WIDTH, HEIGHT = 32, 8
REPORT_HISTORY_LIMIT = 50
MAX_STEPS = 300

EXIT_OK = 0
EXIT_ABORTED = 3
EXIT_ROLLED_BACK = 4
EXIT_FAILED = 5
EXIT_INTERRUPTED = 6
EXIT_CHECK_FAILED = 7
EXIT_ATTENTION = 8

OK = "ok"
NEEDS_ATTENTION = "installed-needs-attention"
ALREADY_INSTALLED = "already-installed"
DRY_RUN = "dry-run"
ABORTED = "aborted"
ROLLED_BACK = "rolled-back"
FAILED = "failed"
INTERRUPTED = "interrupted"
PAUSED = "paused"
IN_PROGRESS = "in-progress"
CHECKED_OK = "checked-ok"
CHECKED_BROKEN = "checked-broken"
UNFINISHED = frozenset({IN_PROGRESS, INTERRUPTED, PAUSED, FAILED})

# What the panel reports for the target.
ABSENT = "absent"
OLD_OK = "old"
OLD_ERROR = "old-error"
NEW_OK = "new"
NEW_ERROR = "new-error"
OTHER_VERSION = "other-version"
NOT_SCRIPT = "not-script"
UNCONFIRMED = "unconfirmed"

# Where a run starts.
START = "start"
AFTER_DELETE = "after-delete"
VERIFY_NEW = "verify-new"
CONTINUE_ROLLBACK = "continue-rollback"
INSTALLED = "installed"

SCRIPT_NAME = panel_snapshot.SCRIPT_NAME
_HEADER = re.compile(r"^#\s*@(\w+)\s*(.*?)\s*$")
_RESTORED = re.compile(r"scripts: (\d+) restored")
_SHARED = re.compile(r"\(shared (\d+)\)")
_BOOT = re.compile(r"\bboot: AWTRIX")
_NTP = re.compile(r"\bntp:")
_ERROR_STATUSES = tuple(range(300, 600))
DEVICE_KEYS = (
    "uptimeSeconds",
    "resetReason",
    "freeHeapBytes",
    "minFreeHeapBytes",
    "largestFreeBlockBytes",
    "fps",
    "matrixPower",
    "currentApp",
)


class SwapAbort(Exception):
    """Stop before any write; the panel has not been touched."""


class SwapError(Exception):
    """Something went wrong once writes had begun."""


class RollbackImpossible(SwapError):
    """The old version cannot be put back either."""


class SwapPaused(Exception):
    """Stopped on purpose mid-run (a person holds the matrix lit); re-running resumes."""


class WriteBlocked(RuntimeError):
    """A write was attempted through a read-only session (dry run, check only)."""


class ReadOnlySession:
    """Pass GETs through and refuse everything else.

    ``--dry-run`` and ``--check-only`` hand this to every reader, so they are
    write-free by construction rather than by care.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def get(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner.get(*args, **kwargs)

    def _refuse(self, *args: Any, **kwargs: Any) -> Any:
        raise WriteBlocked("this run is read-only; a panel write was refused")

    put = post = delete = patch = request = _refuse


# --------------------------------------------------------------- sources


@dataclass(frozen=True)
class SourceInfo:
    label: str
    path: str
    version: str
    name: str | None
    raw_bytes: int
    body: bytes
    sha256: str
    required_block: int
    rollback_block: int = 0
    warnings: tuple[str, ...] = ()

    @property
    def size(self) -> int:
        return len(self.body)

    def summary(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "version": self.version,
            "name": self.name,
            "raw_bytes": self.raw_bytes,
            "bytes": self.size,
            "sha256": self.sha256,
            "required_block": self.required_block,
            "rollback_block": self.rollback_block,
        }


def parse_header(text: str) -> dict[str, str]:
    """``# @key value`` metadata from the leading comment block."""
    meta: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("#"):
            break
        match = _HEADER.match(stripped)
        if match:
            meta.setdefault(match[1].lower(), match[2])
    return meta


def required_block(size: int, ratio: float = INSTALL_BLOCK_RATIO) -> int:
    return math.ceil(size * ratio)


def rollback_block(size: int) -> int:
    """The least room the rollback PUT is sent with (an OOM reply is acceptable there)."""
    return max(required_block(size, ROLLBACK_BLOCK_RATIO), ROLLBACK_MIN_BLOCK)


def resolve_source(value: str | Path) -> Path:
    """The path as given, else relative to this file (the repo root)."""
    path = Path(value)
    if path.is_absolute() or path.exists():
        return path
    beside = Path(__file__).resolve().parent / path
    return beside if beside.exists() else path


def prepare_source(path: Path, expected_version: str, label: str) -> SourceInfo:
    """Read, minify and check one source; abort on anything that would mislead."""
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        raise SwapAbort(f"cannot read the {label} source {path}: {error}") from error
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SwapAbort(f"the {label} source {path} is not UTF-8") from error
    minified = minify(text)
    body = minified.encode("utf-8")
    meta = parse_header(minified)
    version = meta.get("version")
    if version != expected_version:
        raise SwapAbort(
            f"the {label} source {path} declares '# @version {version}', not {expected_version!r}; "
            "the panel reports identity from that header"
        )
    if len(body) < 64 or not minified.strip():
        raise SwapAbort(f"the {label} source {path} is empty after minifying")
    warnings = []
    last = minified.rstrip().splitlines()[-1]
    if not last.startswith("return "):
        warnings.append(f"the {label} source does not end with 'return <Instance>()'")
    return SourceInfo(
        label=label,
        path=str(path),
        version=version,
        name=meta.get("name"),
        raw_bytes=len(raw),
        body=body,
        sha256=hashlib.sha256(body).hexdigest(),
        required_block=required_block(len(body)),
        rollback_block=rollback_block(len(body)),
        warnings=tuple(warnings),
    )


def check_pin(src: SourceInfo, pin: str | None, flag: str) -> None:
    """Refuse a source whose minified SHA-256 does not start with ``pin``."""
    if not pin:
        return
    value = pin.strip().lower()
    if len(value) < 12 or not re.fullmatch(r"[0-9a-f]+", value):
        raise SwapAbort(f"{flag} needs at least 12 hex digits of a SHA-256, got {pin!r}")
    if not src.sha256.startswith(value):
        raise SwapAbort(f"the {src.label} source {src.path} minifies to sha256 {src.sha256[:16]}..., "
                        f"not the {value[:16]}... that {flag} pins")


# --------------------------------------------------------------- panel facts


def inventory(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        value = value.get("apps", value.get("value"))
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise SwapError("app inventory has an unexpected shape")
    return [dict(item) for item in value]


def find_app(apps: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    matches = [app for app in apps if app.get("name") == name]
    if len(matches) > 1:
        raise SwapError(f"app inventory lists {name!r} {len(matches)} times")
    return matches[0] if matches else None


def app_version(app: Mapping[str, Any] | None) -> str | None:
    meta = app.get("meta") if app else None
    version = meta.get("version") if isinstance(meta, Mapping) else None
    return version if isinstance(version, str) else None


def app_error(app: Mapping[str, Any] | None) -> str | None:
    error = app.get("error") if app else None
    if not error:
        return None
    if isinstance(error, Mapping):
        return str(error.get("message", error))
    return str(error)


def is_out_of_memory(message: str | None) -> bool:
    return bool(message) and "out of memory" in str(message).lower()


def target_state(app: Mapping[str, Any] | None, old_version: str, new_version: str) -> str:
    if app is None or app.get("present") is False:
        return ABSENT
    if app.get("present") is not True:
        return UNCONFIRMED
    if app.get("origin") != "script":
        return NOT_SCRIPT
    version, error = app_version(app), app_error(app)
    if version == new_version:
        return NEW_OK if error is None else NEW_ERROR
    if version == old_version:
        return OLD_OK if error is None else OLD_ERROR
    return OTHER_VERSION


def boot_lines(logs: Any) -> list[str] | None:
    """This boot's log lines (the panel keeps its log in RAM, one boot at a time)."""
    if not isinstance(logs, Mapping):
        return None
    lines = logs.get("lines")
    if not isinstance(lines, list) or any(not isinstance(line, str) for line in lines):
        return None
    start = 0
    for index, line in enumerate(lines):
        if _BOOT.search(line):
            start = index
    return lines[start:]


def ntp_state(logs: Any) -> str:
    """``seen`` / ``missing`` for this boot's ``ntp:`` line, else ``unknown: why``.

    ``missing`` only when the log reaches back to this boot's first line; the
    panel keeps its log in RAM, and a long uptime could in principle drop it.
    """
    if not isinstance(logs, Mapping):
        return "unknown: the log could not be read"
    lines = logs.get("lines")
    if not isinstance(lines, list) or any(not isinstance(line, str) for line in lines):
        return "unknown: the log has an unexpected shape"
    starts = [index for index, line in enumerate(lines) if _BOOT.search(line)]
    if not starts:
        return "unknown: the log does not reach back to the boot"
    return "seen" if any(_NTP.search(line) for line in lines[starts[-1]:]) else "missing"


def restored_count(lines: list[str]) -> int | None:
    found = None
    for line in lines:
        match = _RESTORED.search(line)
        if match:
            found = int(match[1])
    return found


def shared_total(lines: list[str]) -> int | None:
    found = None
    for line in lines:
        match = _SHARED.search(line)
        if match:
            found = int(match[1])
    return found


def device_summary(device: Any) -> dict[str, Any] | None:
    if not isinstance(device, Mapping):
        return None
    return {key: device.get(key) for key in DEVICE_KEYS}


def _uptime(device: Any) -> int | None:
    value = device.get("uptimeSeconds") if isinstance(device, Mapping) else None
    return value if type(value) is int and value >= 0 else None


def _block(device: Any) -> int | None:
    value = device.get("largestFreeBlockBytes") if isinstance(device, Mapping) else None
    return value if type(value) is int and value >= 0 else None


# --------------------------------------------------------------- the Hub's maintenance


@dataclass(frozen=True)
class HubMaintenance:
    """The parts of the Hub's maintenance config that decide when it reboots."""

    enabled: bool = True
    window_start_minute: int = 21 * 60 + 35
    window_end_minute: int = 23 * 60
    min_uptime_hours: float = 20.0
    low_memory_reboot: bool = True
    low_memory_min_free_bytes: int = 8192
    # Not configurable in utility_hub.json; panel_maintenance's own defaults.
    low_memory_min_free_min_uptime_minutes: float = pm.MaintenanceConfig.low_memory_min_free_min_uptime_minutes
    low_memory_max_per_day: int = pm.MaintenanceConfig.low_memory_max_per_day
    source: str = "defaults"


def load_hub_maintenance(path: Path | None = None) -> HubMaintenance:
    """Read the Hub's own config the way the Hub reads it; defaults (enabled) if that fails."""
    try:
        import utility_hub  # heavy, and only needed here

        target = path or utility_hub.default_config_path()
        config = utility_hub.load_config(target).maintenance
        return HubMaintenance(
            enabled=config.enabled,
            window_start_minute=config.window_start_minute,
            window_end_minute=config.window_end_minute,
            min_uptime_hours=config.min_uptime_hours,
            low_memory_reboot=config.low_memory_reboot,
            low_memory_min_free_bytes=config.low_memory_min_free_bytes,
            source=str(target),
        )
    except Exception as error:  # noqa: BLE001 - assume the cautious default
        return HubMaintenance(source=f"defaults ({type(error).__name__}: {error})")


def low_memory_view(entries: list[dict[str, Any]], now: datetime, hub: HubMaintenance,
                    device: Any) -> dict[str, Any] | None:
    """Whether the Hub's low-memory restart holds for this reading, as the Hub judges it.

    The Hub checks every minute at any hour and restarts the panel when the
    minimum free heap is under its floor after ``low_memory_min_free_min_uptime_minutes``
    of uptime, unless a person holds the matrix off Nightkeeper's schedule or
    it has already restarted ``low_memory_max_per_day`` times in 24 h.  Minimum
    free heap only ever falls, so once this holds it holds until a reboot.
    """
    if not hub.low_memory_reboot or not isinstance(device, Mapping):
        return None
    uptime = _uptime(device)
    min_free = device.get("minFreeHeapBytes")
    if uptime is None or type(min_free) is not int:
        return None
    if uptime < hub.low_memory_min_free_min_uptime_minutes * 60 or min_free >= hub.low_memory_min_free_bytes:
        return None
    since, until = now - timedelta(hours=24), now + timedelta(hours=24)
    recent = 0
    for entry in entries:
        if pm._is_low_memory_restart(entry) and entry.get("outcome") != pm.IN_PROGRESS:
            at = pm._local_time(entry.get("at"))
            if at is not None and since <= at <= until:
                recent += 1
    held = pm.held_by_a_person(now, device.get("matrixPower"))
    capped = recent >= hub.low_memory_max_per_day
    return {
        "min_free": min_free,
        "threshold": hub.low_memory_min_free_bytes,
        "uptime_hours": round(uptime / 3600, 1),
        "restarts_in_24h": recent,
        "cap": hub.low_memory_max_per_day,
        "held_by_a_person": held,
        "due": not capped and not held,
    }


def read_maintenance_entries(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    """The maintenance report's entries, and a note when it could not be read."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], None
    except (OSError, ValueError) as error:
        return [], f"unreadable: {type(error).__name__}: {error}"
    if not isinstance(data, Mapping):
        return [], "unreadable: not an object"
    history = data.get("history")
    entries = [entry for entry in history if isinstance(entry, Mapping)] if isinstance(history, list) else []
    last = data.get("last")
    if isinstance(last, Mapping) and last not in entries:
        entries.append(last)
    return [dict(entry) for entry in entries], None


def nightly_window(now: datetime, start_minute: int, end_minute: int) -> tuple[datetime, datetime]:
    """The maintenance window containing ``now``, else the next one to open."""
    length = timedelta(minutes=(end_minute - start_minute) % (24 * 60))
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for days in (-1, 0, 1):
        start = midnight + timedelta(days=days, minutes=start_minute)
        if start <= now < start + length:
            return start, start + length
    for days in (0, 1):
        start = midnight + timedelta(days=days, minutes=start_minute)
        if start > now:
            return start, start + length
    raise AssertionError("unreachable")


def maintenance_view(
    entries: list[dict[str, Any]],
    now: datetime,
    hub: HubMaintenance,
    uptime_seconds: int | None,
    window: tuple[datetime, datetime] | None = None,
) -> dict[str, Any]:
    """What the Hub's maintenance is doing, and whether a nightly reboot is still to come.

    ``window`` pins the night being considered; a wait passes the window it
    started in, so once that window has closed nothing more is pending.
    """
    start, end = window or nightly_window(now, hub.window_start_minute, hub.window_end_minute)
    in_progress: tuple[datetime, dict[str, Any]] | None = None
    nightly_done: dict[str, Any] | None = None
    for entry in entries:
        at = pm._local_time(entry.get("at"))
        if at is None:
            continue
        if entry.get("outcome") == pm.IN_PROGRESS:
            if in_progress is None or at > in_progress[0]:
                in_progress = (at, entry)
            continue
        if (entry.get("kind") or "nightly") == "nightly" and start <= at < end:
            nightly_done = entry
    opens_in = max(0.0, (start - now).total_seconds())
    pending = False
    if hub.enabled and nightly_done is None and now < end:
        if uptime_seconds is None:
            pending = True
        else:
            at_open = uptime_seconds + opens_in + RUN_MARGIN.total_seconds()
            pending = at_open >= hub.min_uptime_hours * 3600
    view: dict[str, Any] = {
        "window": [start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes")],
        "inside_window": start <= now < end,
        "opens_in_seconds": round(opens_in),
        "nightly_done": (
            {"at": nightly_done.get("at"), "outcome": nightly_done.get("outcome")} if nightly_done else None
        ),
        "nightly_pending": pending,
        "in_progress": None,
    }
    if in_progress is not None:
        at, entry = in_progress
        view["in_progress"] = {
            "at": entry.get("at"),
            "kind": entry.get("kind") or "nightly",
            "age_seconds": round((now - at).total_seconds()),
            "stale": now - at > STALE_IN_PROGRESS,
        }
    return view


def _naive(moment: datetime) -> datetime:
    return moment if moment.tzinfo is None else moment.astimezone().replace(tzinfo=None)


# --------------------------------------------------------------- screen evidence


def frame_stats(screen: Any) -> dict[str, Any] | None:
    if not isinstance(screen, Mapping):
        return None
    pixels = screen.get("pixels")
    if screen.get("width", WIDTH) != WIDTH or screen.get("height", HEIGHT) != HEIGHT:
        return None
    if not isinstance(pixels, list) or len(pixels) != WIDTH * HEIGHT:
        return None
    if any(type(value) is not int for value in pixels):
        return None
    lit = [value & 0xFFFFFF for value in pixels]
    return {
        "nonblack": sum(1 for value in lit if value),
        "disc_nonblack": sum(1 for y in range(HEIGHT) for x in DISC_COLUMNS if lit[y * WIDTH + x]),
        "hash": hashlib.sha256(json.dumps(lit).encode("ascii")).hexdigest()[:16],
    }


def disc_mask(span0: Any, bx: int = DISC_X) -> list[int]:
    """Framebuffer indices of the moon's disc body, from the page's own ``span0``."""
    spans = list(span0)
    mask = []
    for row in range(HEIGHT):
        start, length = int(spans[2 * row]), int(spans[2 * row + 1])
        mask.extend(row * WIDTH + bx + column for column in range(start, start + length))
    return mask


def counts_as_lit(capture: Mapping[str, Any]) -> bool:
    """Whether one capture is evidence of what the page draws.

    A capture made by :meth:`ScriptSwap._watch_screen` says so itself
    (``lit``: matrix lit before and after the GET, settled after a power change
    and after the boot-lit moment).  One without that flag counts when its
    ``power`` reading was True.
    """
    if "lit" in capture:
        return capture["lit"] is True
    return capture.get("power") is True


def judge_screen(captures: list[dict[str, Any]], target: str) -> dict[str, Any]:
    """``verdict`` ok / inconclusive / broken, conservatively, frame by frame.

    Only frames taken while the matrix was lit (and settled) are evidence:
    what a dark framebuffer holds -- black, frozen or changing -- has never
    been observed, so dark frames never make a page "broken", whatever the
    other frames in the set show.
    """
    frames = [capture for capture in captures if capture.get("stats")]
    if not frames:
        return {"verdict": "inconclusive", "reason": f"no frame captured while {target} was current"}
    counted = [capture for capture in frames if counts_as_lit(capture)]
    distinct = len({capture["stats"]["hash"] for capture in counted})
    result: dict[str, Any] = {"frames": len(frames), "counted": len(counted), "distinct": distinct,
                              "lit": bool(counted), "rendering": bool(counted)}
    if not counted:
        return {**result, "verdict": "inconclusive",
                "reason": f"none of the {len(frames)} frames was taken with the matrix lit and settled; "
                          "dark frames say nothing (draw() may not run while the matrix is dark)"}
    if not any(capture["stats"]["nonblack"] for capture in counted):
        if len(counted) >= MIN_BLACK_CAPTURES:
            return {**result, "verdict": "broken",
                    "reason": f"{len(counted)} frames all black while {target} was current and the matrix lit"}
        return {**result, "verdict": "inconclusive",
                "reason": f"every lit frame was black, but only {len(counted)} of them"}
    disc = [capture for capture in counted if capture.get("disc_window")]
    if len(disc) >= MIN_DISC_CAPTURES and all(capture["stats"]["disc_nonblack"] == 0 for capture in disc):
        return {**result, "verdict": "broken",
                "reason": f"the disc columns x12-19 were black in all {len(disc)} lit frames taken "
                          "while the disc must be on screen"}
    return {**result, "verdict": "ok", "reason": None}


# --------------------------------------------------------------- the report


def resume_anchor(entry: Mapping[str, Any]) -> bool:
    """A swap entry that sent (or was about to send) a write: what a later run resumes from."""
    return entry.get("mode") == "swap" and bool(entry.get("writes"))


def trim_history(history: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """The newest ``limit`` entries, never dropping a target's newest resume anchor.

    Dry runs, checks and swaps that wrote nothing go first, oldest first, so a
    run of them cannot push out the entry that says a target was deleted.
    """
    excess = len(history) - limit
    if excess <= 0:
        return history
    newest_anchor: dict[Any, int] = {}
    for index, entry in enumerate(history):
        if resume_anchor(entry):
            newest_anchor[entry.get("target")] = index
    protected = set(newest_anchor.values()) | {len(history) - 1}
    others = [i for i, entry in enumerate(history) if i not in protected and not resume_anchor(entry)]
    anchors = [i for i, entry in enumerate(history) if i not in protected and resume_anchor(entry)]
    drop = set((others + anchors)[:excess])
    return [entry for index, entry in enumerate(history) if index not in drop]


class Report:
    """``{"last": entry, "history": [...]}``, rewritten atomically after every step.

    ``writable=False`` (a dry run or check without ``--data-dir``/``--report``)
    still reads the history, to plan from it, but never writes the file.
    """

    def __init__(self, path: Path, limit: int = REPORT_HISTORY_LIMIT, *, writable: bool = True) -> None:
        self.path = Path(path)
        self.limit = limit
        self.writable = writable

    def history(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        history = data.get("history") if isinstance(data, Mapping) else None
        return [entry for entry in history if isinstance(entry, dict)] if isinstance(history, list) else []

    def write(self, entry: dict[str, Any]) -> None:
        if not self.writable:
            return
        history = [old for old in self.history() if old.get("id") != entry.get("id")]
        history = trim_history(history + [entry], self.limit)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(json.dumps({"last": entry, "history": history}, indent=2, default=str),
                             encoding="utf-8")
        os.replace(temporary, self.path)


# --------------------------------------------------------------- the swap


@dataclass
class SwapOptions:
    target: str
    new_path: Path
    expect_version: str
    rollback_path: Path
    rollback_version: str
    report_path: Path
    snapshot_root: Path
    maintenance_report: Path
    base_url: str = AWTRIX
    hub_config: Path | None = None
    dry_run: bool = False
    check_only: bool = False
    require_dark: bool = False
    wait_for_nightly: bool = False
    install_if_absent: bool = False
    replace_broken: bool = False
    rollback_now: bool = False
    allow_ungated: bool = False
    expect_sha256: str | None = None
    rollback_sha256: str | None = None
    # False: read the report's history (to plan) but write no file.
    report_write: bool = True
    fidelity: bool = True
    firmware: str = EXPECTED_FIRMWARE
    request_timeout: float = REQUEST_TIMEOUT_SECONDS
    boot_timeout: float = BOOT_TIMEOUT_SECONDS
    settle_timeout: float = SETTLE_TIMEOUT_SECONDS
    poll_seconds: float = POLL_SECONDS
    nightly_poll_seconds: float = NIGHTLY_POLL_SECONDS
    max_wait_minutes: float = MAX_WAIT_MINUTES
    screen_seconds: float = SCREEN_SECONDS
    rollback_reboots: int = ROLLBACK_REBOOTS
    argv: list[str] = field(default_factory=list)


@dataclass
class Boot:
    """One settled boot: what the panel reported once its scripts had restored."""

    reading: dict[str, Any]
    apps: list[dict[str, Any]]
    scripts: set[str]
    heap: dict[str, dict[str, int]] | None

    @property
    def block(self) -> int | None:
        return self.reading.get("block")

    @property
    def restored_confirmed(self) -> bool:
        return bool(self.reading.get("restored_confirmed"))


class ScriptSwap:
    def __init__(
        self,
        options: SwapOptions,
        *,
        session: Any | None = None,
        clock: Any | None = None,
        snapshot: Callable[..., Path] = panel_snapshot.capture_snapshot,
        hub_maintenance: HubMaintenance | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if not SCRIPT_NAME.fullmatch(options.target):
            raise ValueError(f"invalid script name {options.target!r}")
        self.opts = options
        self.clock = clock or SystemClock()
        inner = session if session is not None else requests.Session()
        self.read_only = options.dry_run or options.check_only
        self.session = ReadOnlySession(inner) if self.read_only else inner
        self.client = PanelClient(
            options.base_url,
            session=self.session,
            clock=self.clock,
            policy=PanelPolicy(
                request_timeout_seconds=options.request_timeout,
                minimum_mutation_interval_seconds=2.0,
                quiet_start=None,
                quiet_end=None,
            ),
            source=GATE_SOURCE,
        )
        self.snapshot_fn = snapshot
        self.hub = hub_maintenance
        self.logger = logger or logging.getLogger("awtrix.script_swap")
        self.report = Report(options.report_path, writable=options.report_write)
        self.new: SourceInfo | None = None
        self.old: SourceInfo | None = None
        self.expected: set[str] = set()
        self.previous: dict[str, Any] | None = None
        # An unfinished run for this target whose sources differ from ours.
        self.foreign: dict[str, Any] | None = None
        self.resume_reason = ""
        self.writes = 0
        self.boot_loop = False
        self.attention: list[str] = []
        started = self._now()
        self.entry: dict[str, Any] = {
            "id": f"{started.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}-{os.urandom(2).hex()}",
            "tool": "script_swap",
            "mode": "check" if options.check_only else ("dry-run" if options.dry_run else "swap"),
            "rollback_now": bool(options.rollback_now),
            "argv": list(options.argv),
            "target": options.target,
            "started": started.isoformat(timespec="seconds"),
            "finished": None,
            "outcome": IN_PROGRESS,
            "exit_code": None,
            "panel_state": "untouched: no write has been sent",
            "deleted": False,
            "installed_version": None,
            "writes": 0,
            "steps": [],
            "warnings": [],
        }
        self._report_ok = False

    # ------------------------------------------------------------ plumbing

    def _now(self) -> datetime:
        return self.clock.wall_now()

    def _mono(self) -> float:
        return self.clock.monotonic()

    def _sleep(self, seconds: float) -> None:
        self.clock.sleep(seconds)

    def _write_report(self) -> None:
        self.entry["writes"] = self.writes
        if not self.report.writable:
            return
        try:
            self.report.write(self.entry)
            self._report_ok = True
        except OSError as error:
            if not self._report_ok:
                raise
            self.logger.warning("Could not write the report %s: %s", self.report.path, error)

    def _step(self, name: str, **readings: Any) -> None:
        step = {"at": self._now().isoformat(timespec="seconds"), "step": name, **readings}
        steps = self.entry["steps"]
        steps.append(step)
        if len(steps) > MAX_STEPS:
            del steps[1 : len(steps) - MAX_STEPS + 1]
        self._write_report()

    def _panel_state(self, text: str) -> None:
        self.entry["panel_state"] = text
        self._write_report()

    def _warn(self, text: str) -> None:
        self.logger.warning("Warning: %s", text)
        self.entry["warnings"].append(text)

    def _read(self, path: str, what: str) -> Any:
        return self.client.call_with_retry(
            lambda: self.client.read_json(path, operation=f"script swap {what}"), attempts=3
        )

    def _try_read(self, path: str) -> Any:
        try:
            return self.client.read_json(path, operation=f"script swap read {path}")
        except PanelClientError:
            return None

    def _apps(self) -> list[dict[str, Any]]:
        return inventory(self._read(APPS_PATH, "apps read"))

    def _device(self) -> dict[str, Any]:
        device = self._read(DEVICE_PATH, "device read")
        if not isinstance(device, dict) or _uptime(device) is None:
            raise SwapError("device readback has no valid uptimeSeconds")
        return device

    def _patiently(self, read: Callable[[], Any], seconds: float = POST_WRITE_READ_SECONDS) -> Any:
        """A read that follows a write: wait out a short outage rather than give up.

        Three quick attempts (~3 s) is a blip, not a verdict; once a write has
        gone out, a failed read would otherwise turn into a full rollback.
        """
        deadline = self._mono() + seconds
        while True:
            try:
                return read()
            except PanelClientError:
                if self._mono() >= deadline:
                    raise
                self._sleep(2.0)

    def _restored_by_apps(self, expect: set[str], device: Any) -> bool:
        """With /api/v1/logs unreadable: every expected script listed, none in error, uptime >= 30 s."""
        uptime = _uptime(device)
        if not expect or uptime is None or uptime < RESTORE_FALLBACK_UPTIME:
            return False
        if isinstance(device, Mapping) and device.get("scriptingRunning") is False:
            return False
        try:
            apps = inventory(self.client.read_json(APPS_PATH, operation="script swap restore check"))
            scripts = pm._script_names(apps)
        except (PanelClientError, SwapError, ValueError):
            return False
        return expect <= scripts and not (set(pm.script_errors(apps)) & expect)

    def _boot_epoch(self, device: Any) -> float | None:
        uptime = _uptime(device)
        return None if uptime is None else self._now().timestamp() - uptime

    def _target(self, apps: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
        assert self.new is not None and self.old is not None
        app = find_app(apps, self.opts.target)
        return app, target_state(app, self.old.version, self.new.version)

    # ------------------------------------------------------------ entry point

    def run(self) -> int:
        lock = None
        if not self.read_only:
            try:
                lock = self._acquire_lock()
            except SwapAbort as abort:
                # Another run owns the report: leave it alone.
                self.report.writable = False
                self.logger.error("Aborted, nothing written: %s", abort)
                return self._finish(ABORTED, EXIT_ABORTED, error=str(abort))
        elif self.report.writable:
            # A dry run or check may run beside a swap, but must not rewrite
            # the report under it (the write is a read-modify-write).
            try:
                lock = self._acquire_lock()
            except SwapAbort as busy:
                self.report.writable = False
                self._warn(f"{busy}; this read-only run writes no report")
        try:
            return self._run()
        except KeyboardInterrupt:
            self.logger.error("Interrupted. Panel state: %s", self.entry["panel_state"])
            return self._finish(INTERRUPTED, EXIT_INTERRUPTED, error="interrupted (Ctrl+C)")
        except SwapPaused as pause:
            self.logger.error("Paused: %s. Panel state: %s", pause, self.entry["panel_state"])
            return self._finish(PAUSED, EXIT_INTERRUPTED, error=f"paused: {pause}")
        except SwapAbort as abort:
            self.logger.error("Aborted, nothing written: %s", abort)
            return self._finish(ABORTED, EXIT_ABORTED, error=str(abort))
        except Exception as error:  # noqa: BLE001
            detail = f"{type(error).__name__}: {error}"
            if self.writes == 0:
                self.logger.error("Aborted before any write: %s", detail)
                return self._finish(ABORTED, EXIT_ABORTED, error=detail)
            return self._fail(f"unexpected error: {detail}")
        finally:
            if lock is not None:
                self._release_lock(lock)

    def _acquire_lock(self) -> int:
        """One swap at a time: an exclusive OS lock beside the report for the whole run."""
        path = self.report.path.parent / LOCK_NAME
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as error:
            raise SwapAbort(f"cannot open the single-run lock {path}: {error}") from error
        try:
            panel_gate._try_lock(fd)
        except OSError as error:
            os.close(fd)
            raise SwapAbort(f"another script_swap run holds {path}; only one may run at a time") from error
        self.entry["lock"] = str(path)
        return fd

    @staticmethod
    def _release_lock(fd: int) -> None:
        try:
            panel_gate._unlock(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _gate_problem(self) -> str | None:
        """Why this process's panel writes would go out ungated and unjournaled, or None."""
        if not panel_gate.enabled():
            return f"{panel_gate.ENABLE_ENV}={os.environ.get(panel_gate.ENABLE_ENV)!r} in this process"
        try:
            off = panel_gate.gate_dir() / panel_gate.OFF_NAME
        except Exception:  # noqa: BLE001 - no gate directory: the gate reports it per write
            return None
        if panel_gate._switched_off(off.parent):
            return f"{off} exists"
        return None

    def _run(self) -> int:
        o = self.opts
        try:
            self._write_report()
        except OSError as error:
            raise SwapAbort(f"cannot write the report {self.report.path}: {error}") from error
        if o.expect_version == o.rollback_version:
            raise SwapAbort("--expect-version and --rollback-version must differ: identity is meta.version")
        self.new = prepare_source(o.new_path, o.expect_version, "new")
        self.old = prepare_source(o.rollback_path, o.rollback_version, "rollback")
        self.entry["new"] = self.new.summary()
        self.entry["rollback"] = self.old.summary()
        check_pin(self.new, o.expect_sha256, "--expect-sha256")
        check_pin(self.old, o.rollback_sha256, "--rollback-sha256")
        for warning in self.new.warnings + self.old.warnings:
            self._warn(warning)
        if not o.check_only:
            # Read before the panel is: an abort below can then say what an
            # unfinished earlier run left behind.
            self.previous = self._previous_entry()
            gate = self._gate_problem()
            if gate:
                text = f"panel writes would go out ungated and unjournaled ({gate})"
                if o.dry_run:
                    self._warn(f"{text}; a real swap refuses that unless --allow-ungated")
                elif not o.allow_ungated:
                    raise SwapAbort(f"{text}; unset it, or pass --allow-ungated")
                else:
                    self._warn(f"{text}; going ahead because --allow-ungated was given")

        device = self._device()
        if device.get("version") != o.firmware:
            raise SwapAbort(f"panel firmware is {device.get('version')!r}, not {o.firmware!r}")
        apps = self._apps()
        logs = self._try_read(LOGS_PATH)
        lines = boot_lines(logs)
        heap = pm.parse_boot_heap(lines) if lines is not None else None
        app, state = self._target(apps)
        self.entry["before"] = {
            "device": device_summary(device),
            "target": self._app_summary(app),
            "scripts": sorted(pm._script_names(apps)),
            "script_errors": pm.script_errors(apps),
            "boot_heap": heap,
            "shared": shared_total(lines) if lines else None,
            "ntp": ntp_state(logs),
            # Only a swap compares loop positions; a dry run reads nothing it does not need.
            "apploop": None if o.dry_run else self._loop_position(),
        }
        self._step("preflight", target_state=state, device=device_summary(device))

        if o.check_only:
            return self._check_only(app, state, device)

        resume = self._resume_point(app, state)
        if resume == INSTALLED:
            self.logger.info("%s already reports %s with no error; nothing to do.", o.target, self.new.version)
            return self._finish(ALREADY_INSTALLED, EXIT_OK)

        blockers = self._maintenance_gate(device)
        if not o.dry_run:
            # The wait may have spanned a Hub reboot: look again.
            device = self._device()
            apps = self._apps()
            app, state = self._target(apps)
            resume = self._resume_point(app, state)
            if resume == INSTALLED:
                return self._finish(ALREADY_INSTALLED, EXIT_OK)
        self._set_expected(apps)
        blockers += self._preflight_blockers(apps, resume)

        if o.require_dark and device.get("matrixPower") is not False:
            blockers.append(f"the matrix is not dark (matrixPower {device.get('matrixPower')!r}) "
                            "and --require-dark was given")
        if device.get("matrixPower") is False:
            self.entry["before"]["dark_screen"] = self._sample_dark_screen()
        uptime = _uptime(device) or 0
        if uptime < FRESH_BOOT_SECONDS and not o.dry_run:
            self._wait_until_restored(device, self.expected & pm._script_names(apps))

        if o.dry_run:
            return self._dry_run(device, app, state, resume, blockers)
        if blockers:
            raise SwapAbort("; ".join(blockers))
        self._take_snapshot(state)
        return self._forward(resume)

    def _preflight_blockers(self, apps: list[dict[str, Any]], resume: str) -> list[str]:
        """What makes a fresh swap certain to fail, found before anything is written."""
        if resume != START:
            # The target is already gone or half-done: only going on can put it back.
            return []
        blockers = []
        others = {name: error for name, error in pm.script_errors(apps).items() if name != self.opts.target}
        if others:
            blockers.append(f"other scripts already report errors {others}; the post-reboot checks would refuse "
                            "the install and the rollback's verification, so a person should look first")
        ntp = self.entry["before"].get("ntp")
        if ntp == "missing":
            blockers.append("this boot's log has no 'ntp:' line: on every recorded boot the scripts restored "
                            "right after it, so a reboot now may not restore them")
        elif ntp != "seen":
            self._warn(f"NTP could not be confirmed from the boot log ({ntp})")
        return blockers

    def _sample_dark_screen(self) -> dict[str, Any]:
        """A few framebuffer reads while the matrix is dark: what a dark framebuffer holds.

        Evidence only.  The screen check counts no dark frame, and this is
        how the next run can know whether dark frames are black, frozen or
        still drawn.
        """
        samples = []
        for index in range(DARK_SAMPLES):
            if index:
                self._sleep(3.0)
            device = self._try_read(DEVICE_PATH)
            stats = frame_stats(self._try_read(SCREEN_PATH))
            samples.append({
                "power": device.get("matrixPower") if isinstance(device, Mapping) else None,
                "current": device.get("currentApp") if isinstance(device, Mapping) else None,
                "nonblack": stats["nonblack"] if stats else None,
                "hash": stats["hash"] if stats else None,
            })
        hashes = {sample["hash"] for sample in samples if sample["hash"]}
        return {"samples": samples, "distinct": len(hashes)}

    # ------------------------------------------------------------ preflight pieces

    @staticmethod
    def _app_summary(app: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if app is None:
            return None
        return {
            "present": app.get("present"),
            "origin": app.get("origin"),
            "version": app_version(app),
            "error": app_error(app),
            "slot": app.get("slot"),
            "enabled": app.get("enabled"),
            "inLoop": app.get("inLoop"),
        }

    def _loop_position(self) -> dict[str, Any] | None:
        loop = self._try_read(APPLOOP_PATH)
        if not isinstance(loop, Mapping):
            return None
        order = loop.get("order") if isinstance(loop.get("order"), list) else []
        disabled = loop.get("disabled") if isinstance(loop.get("disabled"), list) else []
        target = self.opts.target
        return {
            "index": order.index(target) if target in order else None,
            "disabled": target in disabled,
        }

    def _previous_entry(self) -> dict[str, Any] | None:
        """The run this one may resume: the newest swap of this target that wrote anything.

        A run that wrote nothing (refused at preflight, interrupted while
        waiting, an already-installed no-op) changed nothing on the panel, so
        it is skipped and the run before it still decides.  A run made with
        different sources (other versions or bytes) is never resumed: its
        rollback would put back something else.  It is remembered in
        ``self.foreign`` so a refusal can name it.
        """
        for entry in reversed(self.report.history()):
            if entry.get("id") == self.entry["id"] or entry.get("target") != self.opts.target:
                continue
            if not resume_anchor(entry):
                continue
            if self._same_sources(entry):
                return entry
            if entry.get("outcome") in UNFINISHED:
                self.foreign = entry
                self._warn(f"run {entry.get('id')} left {self.opts.target} unfinished with different sources "
                           f"({self._sources_text(entry)}); this run does not resume it")
            return None
        return None

    def _same_sources(self, entry: Mapping[str, Any]) -> bool:
        def key(summary: Any) -> tuple[Any, Any]:
            return (summary.get("version"), summary.get("sha256")) if isinstance(summary, Mapping) else (None, None)

        return (key(entry.get("new")) == (self.new.version, self.new.sha256)
                and key(entry.get("rollback")) == (self.old.version, self.old.sha256))

    @staticmethod
    def _sources_text(entry: Mapping[str, Any]) -> str:
        def one(summary: Any) -> str:
            if not isinstance(summary, Mapping):
                return "?"
            return f"{summary.get('version')} {str(summary.get('sha256'))[:12]}"

        return f"new {one(entry.get('new'))}, rollback {one(entry.get('rollback'))}"

    def _resume_point(self, app: dict[str, Any] | None, state: str) -> str:
        o, prev = self.opts, self.previous
        unfinished = prev is not None and prev.get("outcome") in UNFINISHED
        if state == NOT_SCRIPT:
            raise SwapAbort(f"{o.target!r} has origin {app.get('origin')!r}, not 'script'; refusing to touch it")
        if state == UNCONFIRMED:
            raise SwapAbort(f"the panel does not confirm whether {o.target!r} is present")
        if state == OTHER_VERSION:
            raise SwapAbort(f"{o.target!r} reports version {app_version(app)!r}, neither "
                            f"{self.old.version!r} nor {self.new.version!r}")
        if unfinished:
            self.entry["resumed_from"] = prev.get("id")
            self.entry["deleted"] = bool(prev.get("deleted"))
            if prev.get("rollback_reason"):
                self.resume_reason = (f"continuing the rollback of run {prev.get('id')}: "
                                      f"{prev.get('rollback_reason')}")
                self.entry["rollback_reason"] = self.resume_reason
                return CONTINUE_ROLLBACK
        if o.rollback_now:
            if state == OLD_OK and not (unfinished and prev.get("deleted")):
                raise SwapAbort(f"{o.target!r} already reports {self.old.version} with no error; "
                                "there is nothing to roll back")
            self.resume_reason = f"--rollback-now: put {o.target} {self.old.version} back"
            self.entry["rollback_reason"] = self.resume_reason
            return CONTINUE_ROLLBACK
        if state == NEW_OK:
            return VERIFY_NEW if unfinished else INSTALLED
        if state == NEW_ERROR:
            error = app_error(app)
            if not unfinished:
                raise SwapAbort(f"{o.target!r} already reports {self.new.version} with error {error!r}, and "
                                "no unfinished run of this tool explains it; a person should look "
                                f"(--rollback-now puts {self.old.version} back){self._foreign_note()}")
            if is_out_of_memory(error):
                return VERIFY_NEW
            self.resume_reason = f"{o.target} {self.new.version} reports error {error!r}"
            self.entry["rollback_reason"] = self.resume_reason
            return CONTINUE_ROLLBACK
        if state == ABSENT:
            if unfinished and prev.get("deleted"):
                return AFTER_DELETE
            if o.install_if_absent:
                return AFTER_DELETE
            raise SwapAbort(f"{o.target!r} is not installed and no unfinished run of this tool deleted it; "
                            f"pass --install-if-absent to install {self.new.version}, or --rollback-now to "
                            f"install {self.old.version}{self._foreign_note()}")
        if state == OLD_ERROR and not o.replace_broken:
            raise SwapAbort(f"{o.target!r} reports {self.old.version} with error {app_error(app)!r}; "
                            "pass --replace-broken to replace it anyway")
        return START

    def _foreign_note(self) -> str:
        if not self.foreign:
            return ""
        return (f" (run {self.foreign.get('id')} left it {self.foreign.get('outcome')} with different sources: "
                f"{self._sources_text(self.foreign)}; its own command resumes it)")

    def _set_expected(self, apps: list[dict[str, Any]]) -> None:
        try:
            current = pm._script_names(apps)
        except ValueError as error:
            raise SwapAbort(f"app inventory: {error}") from error
        resumed = self.previous if self.entry.get("resumed_from") else None
        recorded = resumed.get("expected_scripts") if resumed else None
        if isinstance(recorded, list) and all(isinstance(name, str) for name in recorded):
            self.expected = set(recorded) | {self.opts.target}
        else:
            self.expected = current | {self.opts.target}
        self.entry["expected_scripts"] = sorted(self.expected)
        # Where the target stood before the first run of this chain touched it.
        baseline = resumed.get("baseline") if resumed else None
        if not isinstance(baseline, Mapping):
            baseline = {"target": self.entry["before"].get("target"), "apploop": self.entry["before"].get("apploop")}
        self.entry["baseline"] = dict(baseline)

    def _hub_maintenance(self) -> HubMaintenance:
        if self.hub is None:
            self.hub = load_hub_maintenance(self.opts.hub_config)
        return self.hub

    def _maintenance_reasons(self, device: Any, window: tuple[datetime, datetime] | None = None, *,
                             imminent_only: bool = False) -> tuple[list[str], dict[str, Any]]:
        """Why a Hub reboot could race this run; ``imminent_only`` drops a nightly that is not near."""
        hub = self._hub_maintenance()
        entries, note = read_maintenance_entries(self.opts.maintenance_report)
        now = _naive(self._now())
        view = maintenance_view(entries, now, hub, _uptime(device), window)
        view["config"] = hub.source
        if note:
            view["report_note"] = note
        reasons = []
        running = view["in_progress"]
        if running and not running["stale"]:
            reasons.append(f"the Hub's {running['kind']} maintenance has been in progress since {running['at']}")
        start = view["window"][0][11:16]
        end = view["window"][1][11:16]
        near = view["inside_window"] or view["opens_in_seconds"] <= RUN_MARGIN.total_seconds()
        if view["nightly_pending"] and (near or (self.opts.wait_for_nightly and not imminent_only)):
            reasons.append(f"the Hub's nightly reboot ({start}-{end}) has not run yet tonight and would "
                           f"reboot this panel (it needs uptime >= {hub.min_uptime_hours:g} h)")
        low = low_memory_view(entries, now, hub, device)
        view["low_memory"] = low
        if low and low["due"]:
            reasons.append(f"the Hub's low-memory restart is due (minimum free heap {low['min_free']} < "
                           f"{low['threshold']} at uptime {low['uptime_hours']:g} h, {low['restarts_in_24h']} of "
                           f"{low['cap']} restarts used in 24 h) and would reboot this panel within a minute or so")
        return reasons, view

    def _maintenance_gate(self, device: dict[str, Any]) -> list[str]:
        """Wait out (or, without --wait-for-nightly, refuse) a Hub reboot; dry run only reports."""
        reasons, view = self._maintenance_reasons(device)
        self.entry["maintenance"] = view
        running = view["in_progress"]
        if running and running["stale"]:
            self._warn(f"maintenance.json has an in-progress entry from {running['at']} "
                       f"({running['age_seconds'] // 60} min old); treating it as left by a Hub that stopped")
        self._low_memory_note(device)
        if not reasons:
            return []
        if self.opts.dry_run:
            verb = "would wait" if self.opts.wait_for_nightly else "would refuse"
            return [f"{verb}: {reason}" for reason in reasons]
        if not self.opts.wait_for_nightly:
            raise SwapAbort("; ".join(reasons) + " -- re-run with --wait-for-nightly, which waits it out")
        budget = self.opts.max_wait_minutes * 60
        if view["nightly_pending"] and view["opens_in_seconds"] > budget:
            raise SwapAbort(f"the nightly window opens in {view['opens_in_seconds'] // 60} min, "
                            f"beyond --max-wait-minutes {self.opts.max_wait_minutes:g}")
        deadline = self._mono() + budget
        window = (datetime.fromisoformat(view["window"][0]), datetime.fromisoformat(view["window"][1]))
        self._panel_state("untouched: waiting for the Hub's nightly maintenance; no write has been sent")
        self._step("wait-for-maintenance", reasons=reasons)
        self.logger.info("Waiting (at most %.0f min): %s", self.opts.max_wait_minutes, "; ".join(reasons))
        last = reasons
        while reasons:
            if self._mono() >= deadline:
                raise SwapAbort(f"gave up after {self.opts.max_wait_minutes:g} min waiting: " + "; ".join(reasons))
            self._sleep(self.opts.nightly_poll_seconds)
            latest = self._try_read(DEVICE_PATH)
            if _uptime(latest) is None:
                # Unreachable usually means the Hub's reboot is under way:
                # nothing it was waiting for can be judged settled yet.
                continue
            reasons, view = self._maintenance_reasons(latest, window)
            if reasons and reasons != last:
                self.logger.info("Still waiting: %s", "; ".join(reasons))
                last = reasons
        if view["nightly_done"] is None and _naive(self._now()) >= window[1]:
            self._warn(f"the maintenance window closed at {window[1]:%H:%M} with no nightly entry in "
                       "maintenance.json (is the Hub running?); going ahead, since no nightly reboot can "
                       "race this run now")
        self.entry["maintenance"] = view
        self._step("maintenance-clear", maintenance=view)
        self._panel_state("untouched: no write has been sent")
        return []

    def _low_memory_note(self, device: Any) -> None:
        low = (self.entry.get("maintenance") or {}).get("low_memory")
        if low and not low["due"]:
            why = ("a person is holding the matrix off Nightkeeper's schedule" if low["held_by_a_person"]
                   else f"it has used {low['restarts_in_24h']} of {low['cap']} restarts in 24 h")
            self._warn(f"minimum free heap {low['min_free']} < {low['threshold']} at uptime "
                       f"{low['uptime_hours']:g} h, but the Hub will not restart the panel for it now ({why})")

    def _wait_until_restored(self, device: dict[str, Any], expect: set[str]) -> None:
        """A panel that has only just booted may still be restoring its scripts."""
        deadline = self._mono() + self.opts.settle_timeout
        while True:
            lines = boot_lines(self._try_read(LOGS_PATH))
            if lines is not None and restored_count(lines) is not None:
                return
            if lines is None and self._restored_by_apps(expect, self._try_read(DEVICE_PATH)):
                return
            if self._mono() >= deadline:
                raise SwapAbort(f"the panel booted {_uptime(device)} s ago and its scripts have not "
                                f"reported restored within {self.opts.settle_timeout:g} s")
            self._sleep(self.opts.poll_seconds)

    def _take_snapshot(self, state: str) -> None:
        purpose = f"script_swap: before replacing {self.opts.target} {self.old.version} with {self.new.version}"
        try:
            destination = self.snapshot_fn(
                base_url=self.opts.base_url,
                output_root=self.opts.snapshot_root,
                purpose=purpose,
                session=self.session,
                now=_naive(self._now()).astimezone(),
                timeout=self.opts.request_timeout,
            )
        except Exception as error:  # noqa: BLE001 - no snapshot, no write
            raise SwapAbort(f"the pre-swap snapshot failed ({type(error).__name__}: {error})") from error
        record: dict[str, Any] = {"path": str(destination)}
        try:
            manifest = json.loads((Path(destination) / "manifest.json").read_text(encoding="utf-8"))
            backup = manifest.get("exactScriptBackups", {}).get(self.opts.target)
        except (OSError, ValueError, AttributeError):
            backup = None
        mismatch = None
        if isinstance(backup, Mapping):
            record["target"] = {key: backup.get(key) for key in ("bytes", "sha256", "provenance")}
            # Only when the panel holds what this run takes for the old version
            # can its bytes be held against the rollback source.
            if (state in (OLD_OK, OLD_ERROR) and backup.get("provenance") == "panel"
                    and backup.get("sha256") != self.old.sha256):
                mismatch = (f"the installed {self.opts.target} is not byte-identical to the rollback source "
                            f"(panel {str(backup.get('sha256'))[:12]}, rollback {self.old.sha256[:12]}), so a "
                            f"rollback would not put back what is running; the exact installed bytes are in "
                            f"{destination}")
        self.entry["snapshot"] = record
        self._step("snapshot", **record)
        if mismatch:
            raise SwapAbort(mismatch)

    def _numbers(self, device: dict[str, Any]) -> list[str]:
        new, old = self.new, self.old
        lines = [
            f"new      {new.path}  {new.raw_bytes:,} B -> {new.size:,} B minified  sha256 {new.sha256[:16]}  "
            f"needs {new.required_block:,} B contiguous ({INSTALL_BLOCK_RATIO:g}x)",
            f"rollback {old.path}  {old.raw_bytes:,} B -> {old.size:,} B minified  sha256 {old.sha256[:16]}  "
            f"sent with >= {old.rollback_block:,} B (max({ROLLBACK_BLOCK_RATIO:g}x, {ROLLBACK_MIN_BLOCK:,}); "
            "an out-of-memory reply compiles at the next boot)",
            f"panel    firmware {device.get('version')}, uptime {(_uptime(device) or 0) / 3600:.1f} h, "
            f"block {device.get('largestFreeBlockBytes')}, free {device.get('freeHeapBytes')}, "
            f"min free {device.get('minFreeHeapBytes')}, matrix {'lit' if device.get('matrixPower') else 'dark'}, "
            f"current {device.get('currentApp')}",
        ]
        heap = self.entry["before"].get("boot_heap")
        if heap:
            parts = ", ".join(f"{name} +{value['vm_heap']:,} (block after {value['max_block_after']:,})"
                              for name, value in heap.items())
            lines.append(f"boot log {parts}; shared {self.entry['before'].get('shared')}")
        return lines

    def _dry_run(self, device: dict[str, Any], app: dict[str, Any] | None, state: str, resume: str,
                 blockers: list[str]) -> int:
        o, new, old = self.opts, self.new, self.old
        target = self._app_summary(app)
        view = self.entry.get("maintenance", {})
        out = [f"Plan for {o.target}: {old.version} -> {new.version} (dry run: no write, no snapshot)"]
        out += ["  " + line for line in self._numbers(device)]
        out.append(f"  target   {o.target}: state {state}" + (f", {target}" if target else ""))
        out.append(f"  scripts  {', '.join(sorted(self.expected))} (expected back after the swap)")
        done = view.get("nightly_done")
        out.append(f"  hub      maintenance window {view.get('window')}, nightly tonight: "
                   f"{done['outcome'] + ' at ' + done['at'] if done else 'not yet'}, pending reboot: "
                   f"{view.get('nightly_pending')}, in progress: {view.get('in_progress')}, "
                   f"low-memory: {view.get('low_memory')}")
        out.append(f"  ntp      {self.entry['before'].get('ntp')}")
        dark = self.entry["before"].get("dark_screen")
        if dark:
            out.append(f"  dark fb  {len(dark['samples'])} frames while dark: nonblack "
                       f"{[sample['nonblack'] for sample in dark['samples']]}, distinct {dark['distinct']}")
        out.append(f"  start    {resume}" + (f" ({self.resume_reason})" if self.resume_reason else ""))
        out.append("  steps    snapshot -> " + self._plan_steps(resume))
        out.append(f"  blockers {'; '.join(blockers) if blockers else 'none'}")
        for warning in self.entry["warnings"]:
            out.append(f"  warning  {warning}")
        for line in out:
            self.logger.info("%s", line)
        self.entry["plan"] = out
        self.entry["blockers"] = blockers
        return self._finish(DRY_RUN, EXIT_ABORTED if blockers else EXIT_OK)

    def _plan_steps(self, resume: str) -> str:
        t, new, old = self.opts.target, self.new, self.old
        install = (f"reboot, wait <= {self.opts.boot_timeout:g} s + restore <= {self.opts.settle_timeout:g} s, "
                   f"PUT {new.version} only if block >= {new.required_block:,} -> ")
        verify = (f"reboot, verify {t} {new.version} + all scripts -> fidelity "
                  f"(readback if block >= {READBACK_MIN_BLOCK:,}, screen <= {self.opts.screen_seconds:g} s)")
        rollback = (f"; on failure: delete {t}, reboot, PUT {old.version} once block >= {old.rollback_block:,} "
                    f"(<= {self.opts.rollback_reboots} more reboots), reboot, verify {old.version}")
        if resume == START:
            return f"DELETE {t} -> " + install + verify + rollback
        if resume == AFTER_DELETE:
            return install + verify + rollback
        if resume == VERIFY_NEW:
            return verify + rollback
        return (f"rollback only (never sends {new.version}): delete {t} if present, reboot, PUT {old.version} "
                f"once block >= {old.rollback_block:,} (<= {self.opts.rollback_reboots} more reboots), reboot, "
                f"verify {old.version}")

    # ------------------------------------------------------------ writes

    def _first_write_guard(self) -> None:
        """Just before the first write: nothing may have changed that preflight relied on.

        The Hub's low-memory restart can land after preflight; a DELETE sent
        while the firmware restores its scripts is untested; the matrix may
        have lit up.  Any of these stops the run with nothing written.
        """
        device = self._patiently(self._device, self.opts.boot_timeout)
        if self.opts.require_dark and device.get("matrixPower") is not False:
            raise SwapAbort(f"the matrix lit up (matrixPower {device.get('matrixPower')!r}) before the first write")
        if (_uptime(device) or 0) < FRESH_BOOT_SECONDS:
            present = pm._script_names(self._apps())
            self._wait_until_restored(device, self.expected & present)
        reasons, _view = self._maintenance_reasons(device, imminent_only=True)
        if reasons:
            raise SwapAbort("; ".join(reasons) + " -- re-run with --wait-for-nightly, which waits it out")
        self._step("first-write-guard", device=device_summary(device))

    def _delete_target(self, *, expect_version: str | None, first_write: bool = False) -> None:
        t = self.opts.target
        if first_write:
            self._first_write_guard()
        app = find_app(self._apps() if first_write else self._patiently(self._apps), t)
        if app is None or app.get("present") is False:
            self._step("delete-skipped", reason=f"{t} already absent")
            return
        if app.get("origin") != "script":
            raise SwapError(f"refusing to DELETE {t!r}: origin is {app.get('origin')!r}, not 'script'")
        if expect_version is not None and app_version(app) != expect_version:
            raise SwapError(f"refusing to DELETE {t!r}: it now reports {app_version(app)!r}, "
                            f"not {expect_version!r}")
        self.entry["deleted"] = True
        # Counted before the report says so: a run killed mid-request still
        # shows that it may have written.
        self.writes += 1
        self._panel_state(f"DELETE {t} sent: {t} may be deleted; the other scripts keep running. "
                          "Re-run the same command to resume.")
        self._step("delete", version=app_version(app))
        try:
            self.client._mutate(
                "delete",
                DELETE_APP_PATH.format(name=quote(t, safe="")),
                operation=f"delete script {t}",
                accepted_statuses=(404,),
            )
        except PanelTransportError as error:
            self.logger.warning("DELETE did not complete cleanly (%s); checking the inventory", error)
        except PanelHTTPError as error:
            still = find_app(self._patiently(self._apps), t)
            if (expect_version is not None and still is not None and still.get("present") is True
                    and app_version(still) == expect_version and app_error(still) is None):
                self.entry["deleted"] = False
                self._panel_state(f"unchanged: the panel refused DELETE {t} ({error}); {t} "
                                  f"{expect_version} is still installed")
                raise SwapAbort(f"the panel refused DELETE {t}: {error}") from error
            raise
        after = find_app(self._patiently(self._apps), t)
        if after is not None and after.get("present") is True:
            raise SwapError(f"{t} is still present after DELETE")
        self._panel_state(f"{t} deleted; the other scripts keep running; the {t} page is absent "
                          "until it is reinstalled. Re-run the same command to resume.")
        self.logger.info("Deleted %s.", t)

    def _held_lit(self, device: Any) -> bool:
        """A person holds the matrix lit against Nightkeeper's schedule (not the boot-lit moment)."""
        if not isinstance(device, Mapping) or device.get("matrixPower") is not True:
            return False
        uptime = _uptime(device)
        if uptime is None or uptime < FRESH_BOOT_SECONDS:
            return False
        return pm.held_by_a_person(_naive(self._now()), True)

    def _await_dark(self, device: Any) -> Any:
        """Under --require-dark, hold a reboot while a person holds the matrix lit; pause if they keep it."""
        if not self.opts.require_dark or not self._held_lit(device):
            return device
        self._warn("the matrix is lit against Nightkeeper's schedule (a person is using it); holding the reboot")
        self._step("wait-for-dark", device=device_summary(device))
        deadline = self._mono() + DARK_WAIT_SECONDS
        while self._mono() < deadline:
            self._sleep(DARK_POLL_SECONDS)
            device = self._try_read(DEVICE_PATH)
            if not self._held_lit(device):
                return device
        raise SwapPaused(f"the matrix stayed lit against Nightkeeper's schedule for {DARK_WAIT_SECONDS / 60:g} "
                         "min, so no reboot was sent; re-run the same command once it is dark to resume")

    def _reboot_and_settle(self, purpose: str, expect: set[str]) -> Boot:
        pre = self._await_dark(self._try_read(DEVICE_PATH))
        pre_uptime = _uptime(pre)
        self.writes += 1
        self._panel_state(f"reboot requested ({purpose}): the panel is restarting, or has restarted, with "
                          f"whatever scripts it had stored. Re-run the same command to resume.")
        self._step("reboot", purpose=purpose, before=device_summary(pre))
        self.logger.info("Rebooting the panel: %s.", purpose)
        requested = self._mono()
        try:
            self.client.reboot()
        except PanelHTTPError as error:
            raise SwapError(f"the panel refused the reboot: {error}") from error
        except PanelTransportError as error:
            # The reboot can cut its own reply off; watch for the uptime reset.
            self.logger.info("Reboot request did not complete cleanly (%s); watching for the restart", error)
        device = self._wait_for_boot(pre_uptime, requested)
        boot_seconds = round(self._mono() - requested)
        boot = self._settle(device, expect)
        boot.reading["boot_seconds"] = boot_seconds
        self._step("booted", purpose=purpose, **boot.reading)
        return boot

    def _wait_for_boot(self, pre_uptime: int | None, requested: float) -> dict[str, Any]:
        deadline = requested + self.opts.boot_timeout
        last = None
        while True:
            device = self._try_read(DEVICE_PATH)
            uptime = _uptime(device)
            if uptime is not None:
                last = uptime
                elapsed = self._mono() - requested
                if (pre_uptime is not None and uptime < pre_uptime) or uptime <= elapsed + 2:
                    return device
            if self._mono() >= deadline:
                state = "not answering" if last is None else f"answering with uptime {last} s"
                raise SwapError(f"the panel did not restart within {self.opts.boot_timeout:g} s of the "
                                f"reboot request ({state})")
            self._sleep(self.opts.poll_seconds)

    def _settle(self, device: dict[str, Any], expect: set[str]) -> Boot:
        """Wait for this boot's ``scripts: N restored`` line, watching for another restart."""
        epoch = self._boot_epoch(device)
        last_uptime = _uptime(device)
        started = self._mono()
        deadline = started + self.opts.settle_timeout
        # A readable log with no restore line yet: wait as long as a boot may
        # take (NTP can be slow) before calling the restore stalled.
        patience = started + max(self.opts.settle_timeout, self.opts.boot_timeout)
        restarts = 0
        restored: int | None = None
        heap: dict[str, dict[str, int]] | None = None
        lines: list[str] | None = None
        via: str | None = None
        while True:
            lines = boot_lines(self._try_read(LOGS_PATH))
            if lines is not None:
                restored = restored_count(lines)
                heap = pm.parse_boot_heap(lines)
                if restored is not None or (expect and expect <= set(heap)):
                    via = "log"
                    break
            latest = self._try_read(DEVICE_PATH)
            uptime, later = _uptime(latest), self._boot_epoch(latest)
            went_back = uptime is not None and last_uptime is not None and uptime < last_uptime
            jumped = later is not None and epoch is not None and later - epoch > BOOT_EPOCH_TOLERANCE_SECONDS
            if went_back or jumped:
                restarts += 1
                reason = latest.get("resetReason")
                self._warn(f"the panel restarted again while its scripts restored (reset reason {reason!r})")
                if restarts > MAX_SETTLE_RESTARTS:
                    self.boot_loop = True
                    raise SwapError(f"the panel restarted {restarts} times while its scripts restored "
                                    f"(reset reason {reason!r}): possible boot loop")
                epoch = later
                deadline = self._mono() + self.opts.settle_timeout
                patience = self._mono() + max(self.opts.settle_timeout, self.opts.boot_timeout)
            if uptime is not None:
                last_uptime = uptime
            if lines is None and self._restored_by_apps(expect, latest):
                via = "apps"
                break
            now = self._mono()
            if now >= patience or (now >= deadline and lines is None):
                break
            self._sleep(self.opts.poll_seconds)
        device = self._patiently(self._device)
        apps = self._patiently(self._apps)
        if lines is None:
            lines = boot_lines(self._try_read(LOGS_PATH))
            heap = pm.parse_boot_heap(lines) if lines is not None else None
            restored = restored_count(lines) if lines is not None else None
            if via is None and lines is not None and (
                    restored is not None or bool(expect and expect <= set(heap or {}))):
                via = "log"
        try:
            scripts = pm._script_names(apps)
        except ValueError as error:
            raise SwapError(f"app inventory after boot: {error}") from error
        confirmed = via is not None
        stalled = None
        if not confirmed:
            ntp = "the boot log could not be read" if lines is None else (
                "no 'ntp:' line: WiFi or NTP never came up" if not any(_NTP.search(line) for line in lines)
                else "NTP started but the scripts never reported restored")
            stalled = f"NTP or restore stalled ({ntp}) after {round(self._mono() - started)} s"
        reading = {
            "uptime": _uptime(device),
            "reset_reason": device.get("resetReason"),
            "block": _block(device),
            "free": device.get("freeHeapBytes"),
            "min_free": device.get("minFreeHeapBytes"),
            "fps": device.get("fps"),
            "power": device.get("matrixPower"),
            "restored": restored,
            "restored_confirmed": confirmed,
            "restored_via": via,
            "restore_stalled": stalled,
            "scripts": sorted(scripts),
            "script_errors": pm.script_errors(apps),
            "heap": heap,
            "shared": shared_total(lines) if lines else None,
            "restarts": restarts,
        }
        return Boot(reading=reading, apps=apps, scripts=scripts, heap=heap)

    def _install_problem(self, boot: Boot, remaining: set[str], src: SourceInfo, *, strict: bool,
                         need: int) -> str | None:
        """Why the panel is not ready to take ``src`` now, or None."""
        if not boot.restored_confirmed:
            return (f"{boot.reading.get('restore_stalled') or 'restore not confirmed'}; "
                    "not installing over a restore that may still be running")
        if strict:
            missing = sorted(remaining - boot.scripts)
            if missing:
                return f"scripts missing after the reboot: {', '.join(missing)}"
            errors = {name: error for name, error in boot.reading["script_errors"].items()
                      if name != self.opts.target}
            if errors:
                return f"scripts reporting errors after the reboot: {errors}"
        block = _block(self._try_read(DEVICE_PATH))
        if block is None:
            block = boot.block
        self._step("block-check", version=src.version, block=block, required=need)
        if block is None or block < need:
            return (f"largest free block {block} < {need:,} needed to send {src.size:,} B; "
                    "not sending the source")
        return None

    def _put_script(self, src: SourceInfo, need: int) -> dict[str, Any]:
        t = self.opts.target
        pre = self._try_read(DEVICE_PATH)
        pre_uptime, block = _uptime(pre), _block(pre)
        result: dict[str, Any] = {"version": src.version, "pre_uptime": pre_uptime}
        if block is not None and block < need:
            # The block can wear down within seconds of a boot: look once more.
            result.update(kind="no-room", status=None, detail=f"largest free block {block} < {need:,} "
                                                               "just before the PUT; nothing sent")
            self._step("put-skipped", **result)
            return result
        self.writes += 1
        self._panel_state(f"PUT {t} {src.version} sent: the source may be stored even if it failed to "
                          "compile, and a stored source compiles at the next boot. Re-run the same command "
                          "to resume.")
        self._step("put", version=src.version, bytes=src.size, sha256=src.sha256, block=block)
        self.logger.info("Installing %s %s (%d B).", t, src.version, src.size)
        try:
            response = self.client._mutate(
                "put",
                SCRIPT_PATH.format(name=quote(t, safe="")),
                operation=f"install script {t}",
                accepted_statuses=_ERROR_STATUSES,
                data=src.body,
                headers={"Content-Type": "text/plain; charset=utf-8"},
            )
        except PanelTransportError as error:
            result.update(kind="unknown", status=None, detail=str(error))
            self._step("put-result", **result)
            return result
        status = int(response.status_code)
        try:
            body = response.json()
        except (TypeError, ValueError):
            body = None
        error = body.get("error") if isinstance(body, Mapping) else None
        message = (str(error.get("message", error)) if isinstance(error, Mapping) else str(error)) if error else None
        if 200 <= status < 300:
            if body is None:
                kind, message = "error", "the install reply was not JSON"
            elif error is None:
                kind = "ok"
            else:
                kind = "oom" if is_out_of_memory(message) else "error"
        elif status == 507:
            kind = "insufficient-storage"
            message = message or "insufficientStorage"
        else:
            kind = "http"
            message = message or str(getattr(response, "text", ""))[:200]
        result.update(kind=kind, status=status, detail=message)
        self._step("put-result", **result)
        return result

    def _resolve_unknown_put(self, src: SourceInfo, pre_uptime: int | None) -> str:
        """After a PUT whose reply never came: did the panel restart, and what does it hold?

        A restart during an install is treated as a failure even if the stored
        source then compiled at boot: the panel crashed while taking it.
        """
        deadline = self._mono() + self.opts.boot_timeout
        device = None
        while _uptime(device) is None:
            device = self._try_read(DEVICE_PATH)
            if _uptime(device) is None:
                if self._mono() >= deadline:
                    raise SwapError(f"the panel stopped answering after the PUT for {self.opts.boot_timeout:g} s")
                self._sleep(self.opts.poll_seconds)
        uptime = _uptime(device)
        if pre_uptime is None or uptime < pre_uptime:
            self._warn(f"the panel restarted during the PUT (reset reason {device.get('resetReason')!r})")
            self._step("put-resolved", kind="panic", device=device_summary(device))
            return "panic"
        app = find_app(self._patiently(self._apps), self.opts.target)
        error = app_error(app)
        if app is None or app.get("present") is not True:
            kind = "not-stored"
        elif app_version(app) == src.version and error is None:
            kind = "ok"
        elif app_version(app) == src.version and is_out_of_memory(error):
            kind = "oom"
        else:
            kind = "error"
        self._step("put-resolved", kind=kind, target=self._app_summary(app))
        return kind

    # ------------------------------------------------------------ forward and back

    def _forward(self, resume: str) -> int:
        t, new = self.opts.target, self.new
        remaining = self.expected - {t}
        if resume == CONTINUE_ROLLBACK:
            return self._rollback(self.resume_reason)
        try:
            if resume == START:
                self._delete_target(expect_version=self.old.version, first_write=True)
            elif self.writes == 0:
                self._first_write_guard()
            if resume in (START, AFTER_DELETE):
                boot = self._reboot_and_settle(f"free the heap without {t}", remaining)
                self._note_block_without(boot)
                problem = self._install_problem(boot, remaining, new, strict=True, need=new.required_block)
                if problem:
                    return self._rollback(problem, fresh=boot)
                put = self._put_script(new, new.required_block)
                kind = put["kind"]
                if kind == "unknown":
                    kind = self._resolve_unknown_put(new, put["pre_uptime"])
                if kind == "oom":
                    self.logger.warning("Install reported out of memory; the source is stored and compiles "
                                        "at the next boot, which the verification reboot provides.")
                elif kind != "ok":
                    return self._rollback(f"installing {new.version} failed ({kind}): {put.get('detail')}",
                                          fresh=boot if kind == "no-room" else None)
                self._panel_state(f"{t} {new.version} sent ({kind}); not yet verified after a reboot. "
                                  "Re-run the same command to resume.")
            boot = self._reboot_and_settle(f"verify {t} {new.version}", self.expected)
            problems = self._verify(new, boot)
            if problems:
                return self._rollback("verification failed: " + "; ".join(problems))
            self._installed(new)
            broken = self._fidelity(new, boot)
            if broken:
                return self._rollback(f"fidelity check: {broken}")
            if self.attention:
                # Rolling back cannot help: the same DELETE and PUT would move it again.
                return self._finish(NEEDS_ATTENTION, EXIT_ATTENTION, error="; ".join(self.attention))
            return self._finish(OK, EXIT_OK)
        except (SwapAbort, SwapPaused):
            raise
        except Exception as error:  # noqa: BLE001 - anything after a write ends in rollback
            if self.writes == 0:
                raise  # nothing sent: run() records an abort, and there is nothing to roll back
            self.logger.exception("Swap step failed")
            return self._rollback(f"{type(error).__name__}: {error}")

    def _note_block_without(self, boot: Boot) -> None:
        """The largest block a boot without the target leaves: a number no one had measured."""
        self.entry.setdefault("block_without_target", []).append(
            {"block": boot.block, "uptime": boot.reading.get("uptime"), "scripts": sorted(boot.scripts),
             "shared": boot.reading.get("shared")})

    def _loop_change(self, app: Mapping[str, Any] | None, loop_after: Mapping[str, Any] | None) -> str | None:
        """How the target's place in the rotation got worse since this chain began, or None."""
        t = self.opts.target
        baseline = self.entry.get("baseline") or {}
        before = baseline.get("target") or {}
        loop_before = baseline.get("apploop") or {}
        after = self._app_summary(app) or {}
        notes = []
        if before.get("inLoop") is True and after.get("inLoop") is False:
            notes.append(f"{t} has dropped out of the rotation (inLoop True -> False)")
        if before.get("enabled") is True and after.get("enabled") is False:
            notes.append(f"{t} is disabled (enabled True -> False)")
        if loop_before and loop_after:
            if loop_before.get("index") is not None and loop_after.get("index") is None:
                notes.append(f"{t} is missing from /apploop.json's order (it was at index {loop_before['index']})")
            if loop_before.get("disabled") is False and loop_after.get("disabled") is True:
                notes.append(f"{t} is now in /apploop.json's disabled list")
        return "; ".join(notes) or None

    def _verify(self, src: SourceInfo, boot: Boot) -> list[str]:
        t = self.opts.target
        problems: list[str] = []
        app = find_app(boot.apps, t)
        if app is None or app.get("present") is not True:
            problems.append(f"{t} is not present after the reboot")
        else:
            if app.get("origin") != "script":
                problems.append(f"{t} has origin {app.get('origin')!r}")
            if app_version(app) != src.version:
                problems.append(f"{t} reports version {app_version(app)!r}, expected {src.version!r}")
            if app_error(app):
                problems.append(f"{t} reports error {app_error(app)!r}")
        missing = sorted(self.expected - boot.scripts)
        if missing:
            problems.append(f"scripts missing after the reboot: {', '.join(missing)}")
        others = {name: error for name, error in boot.reading["script_errors"].items() if name != t}
        if others:
            problems.append(f"other scripts report errors: {others}")
        reason = boot.reading.get("reset_reason")
        if isinstance(reason, str) and reason.strip().lower() == "panic":
            problems.append("the panel reports reset reason 'panic'")
        if boot.heap is None:
            self._warn("the boot log could not be read, so loading at boot is not confirmed by the log")
        elif t not in boot.heap:
            problems.append(f"the boot log has no 'vm heap' line for {t}: it did not load at boot")
        loaded = (boot.heap or {}).get(t, {})
        before = (self.entry["before"].get("boot_heap") or {}).get(t, {})
        verification = {
            "version": src.version,
            "problems": problems,
            "vm_heap": loaded.get("vm_heap"),
            "vm_heap_before": before.get("vm_heap"),
            "vm_heap_delta": (loaded["vm_heap"] - before["vm_heap"]
                              if "vm_heap" in loaded and "vm_heap" in before else None),
            "block_after": boot.block,
            "shared": boot.reading.get("shared"),
            "restored_via": boot.reading.get("restored_via"),
            "apploop": self._loop_position(),
            "target": self._app_summary(app),
        }
        change = None if problems else self._loop_change(app, verification["apploop"])
        verification["rotation"] = change
        self.entry.setdefault("verifications", []).append(verification)
        self._step("verify", **verification)
        loop_before = (self.entry.get("baseline") or {}).get("apploop")
        if change:
            # Not a reason to roll back: the rollback's own DELETE and PUT would do the same.
            self.attention.append(change)
            self.entry["attention"] = list(self.attention)
            self._warn(f"{change}; rotation_policy.py shows and can apply the intended order")
        elif not problems and loop_before and verification["apploop"] and loop_before != verification["apploop"]:
            self._warn(f"{t}'s place in the app loop changed {loop_before} -> {verification['apploop']}; "
                       "rotation_policy.py shows and can apply the intended order")
        return problems

    def _installed(self, src: SourceInfo) -> None:
        self.entry["installed_version"] = src.version
        self._panel_state(f"{self.opts.target} {src.version} installed and verified after a reboot")
        self.logger.info("%s %s verified after a reboot.", self.opts.target, src.version)

    def _rollback(self, reason: str, fresh: Boot | None = None) -> int:
        t, old = self.opts.target, self.old
        if self.writes == 0:
            # A resumed rollback or --rollback-now: its first write is here.
            self._first_write_guard()
        self.entry["rollback_reason"] = reason
        self.logger.warning("Rolling back %s to %s: %s", t, old.version, reason)
        self._step("rollback", reason=reason, boot_loop=self.boot_loop)
        remaining = self.expected - {t}
        try:
            app, state = self._target(self._wait_for_apps())
            if state != ABSENT and fresh is None and not self.boot_loop:
                # Not mid-restore: deleting while the firmware compiles is untested.
                self._await_restore()
                app, state = self._target(self._wait_for_apps())
            if state == OLD_OK and not self.entry["deleted"]:
                return self._finish(ABORTED, EXIT_ABORTED, error=reason)
            if state == NOT_SCRIPT:
                raise RollbackImpossible(f"{t} is now a non-script app (origin {app.get('origin')!r})")
            if state == OLD_OK:
                # Resumed after the old source went back in: prove it at a boot.
                boot = self._reboot_and_settle(f"verify {t} {old.version}", self.expected)
                problems = self._verify(old, boot)
                if problems:
                    raise RollbackImpossible(f"{old.version} is installed but did not verify: {'; '.join(problems)}")
                self._installed(old)
                return self._finish(ROLLED_BACK, EXIT_ROLLED_BACK, error=reason)
            if state != ABSENT:
                if self.boot_loop:
                    self._delete_during_boot_loop()
                else:
                    self._delete_target(expect_version=None)
                fresh = None
            reboots = 0
            while True:
                if fresh is None:
                    fresh = self._reboot_and_settle(f"free the heap to reinstall {t} {old.version}", remaining)
                    self._note_block_without(fresh)
                    reboots += 1
                problem = self._install_problem(fresh, remaining, old, strict=False, need=old.rollback_block)
                if problem is None:
                    put = self._put_script(old, old.rollback_block)
                    kind = put["kind"]
                    if kind == "unknown":
                        kind = self._resolve_unknown_put(old, put["pre_uptime"])
                    if kind in ("ok", "oom"):
                        break
                    if kind != "no-room":
                        raise RollbackImpossible(f"reinstalling {old.version} failed ({kind}): {put.get('detail')}")
                    problem = put.get("detail")
                if fresh.reading.get("restore_stalled"):
                    # Another reboot does not bring NTP back: stop, and say so.
                    raise RollbackImpossible(f"cannot reinstall {old.version}: {problem}; not spending more "
                                             "reboots on it -- re-run the same command once the panel restores")
                if reboots >= self.opts.rollback_reboots:
                    raise RollbackImpossible(f"cannot reinstall {old.version}: {problem}")
                self.logger.warning("Rollback install not possible yet (%s); one more reboot", problem)
                fresh = None
            if kind == "oom":
                self.logger.warning("The %s reinstall reported out of memory; the source is stored and "
                                    "compiles at the verification boot.", old.version)
            boot = self._reboot_and_settle(f"verify {t} {old.version}", self.expected)
            problems = self._verify(old, boot)
            if problems:
                raise RollbackImpossible(f"{old.version} did not verify: {'; '.join(problems)}")
            self._installed(old)
            return self._finish(ROLLED_BACK, EXIT_ROLLED_BACK, error=reason)
        except (KeyboardInterrupt, SwapPaused):
            raise
        except Exception as error:  # noqa: BLE001
            return self._fail(f"rollback failed: {type(error).__name__}: {error}")

    def _delete_during_boot_loop(self) -> None:
        """Delete the target while the panel restarts every few seconds.

        A script that crashes the panel at restore leaves HTTP up for about a
        second per cycle, and it restores before Nightkeeper, so the matrix
        stays lit.  The origin is checked once in some window; the DELETE is
        then retried every half second until an inventory read shows the
        target gone or ``boot_timeout`` runs out.
        """
        t = self.opts.target
        deadline = self._mono() + self.opts.boot_timeout
        origin_checked = False
        attempts = 0
        while True:
            try:
                apps = inventory(self.client.read_json(APPS_PATH, operation="script swap boot-loop apps read"))
                app = find_app(apps, t)
                if app is None or app.get("present") is False:
                    self._step("boot-loop-delete", attempts=attempts, result="absent")
                    if attempts:
                        self._panel_state(f"{t} deleted during a boot loop; the other scripts restore without "
                                          "it. Re-run the same command to resume.")
                    return
                if app.get("origin") != "script":
                    raise RollbackImpossible(f"refusing to DELETE {t!r}: origin is {app.get('origin')!r}")
                origin_checked = True
            except PanelClientError:
                pass
            if origin_checked:
                attempts += 1
                self.entry["deleted"] = True
                self.writes += 1
                if attempts == 1:
                    self._panel_state(f"DELETE {t} sent during a boot loop: {t} may be deleted. "
                                      "Re-run the same command to resume.")
                    self._step("delete", version=None, boot_loop=True)
                try:
                    self.client._mutate("delete", DELETE_APP_PATH.format(name=quote(t, safe="")),
                                        operation=f"delete script {t} (boot loop)", accepted_statuses=(404,))
                except PanelClientError:
                    pass
            if self._mono() >= deadline:
                raise RollbackImpossible(f"could not delete {t} during the boot loop within "
                                         f"{self.opts.boot_timeout:g} s ({attempts} DELETE attempts)")
            self._sleep(BOOT_LOOP_RETRY_SECONDS)

    def _await_restore(self) -> None:
        """Give a freshly booted panel its restore; never raises (a boot loop has none)."""
        device = self._try_read(DEVICE_PATH)
        uptime = _uptime(device)
        if uptime is None or uptime >= FRESH_BOOT_SECONDS:
            return
        deadline = self._mono() + self.opts.settle_timeout
        while self._mono() < deadline:
            lines = boot_lines(self._try_read(LOGS_PATH))
            if lines is not None and restored_count(lines) is not None:
                return
            self._sleep(self.opts.poll_seconds)

    def _wait_for_apps(self) -> list[dict[str, Any]]:
        """The inventory, waiting out a panel that is restarting (or boot-looping)."""
        deadline = self._mono() + self.opts.boot_timeout
        while True:
            try:
                return self._apps()
            except PanelClientError:
                if self._mono() >= deadline:
                    raise
                self._sleep(1.0)

    def _rollback_command(self) -> str:
        """A command that puts the old version back from any state, as written."""
        t, old, new = self.opts.target, self.old, self.new
        if self.opts.argv:
            argv = [arg for arg in self.opts.argv if arg not in ("--install-if-absent", "--dry-run", "--check-only")]
            if "--rollback-now" not in argv:
                argv.append("--rollback-now")
            return "script_swap.py " + " ".join(argv)
        flags = " --require-dark" if self.opts.require_dark else ""
        return (f"script_swap.py {t} {new.path} --expect-version {new.version} --rollback {old.path} "
                f"--rollback-version {old.version}{flags} --rollback-now")

    def _fail(self, reason: str) -> int:
        t, old, new = self.opts.target, self.old, self.new
        try:
            apps = self._apps()
            app = find_app(apps, t)
            now = f"{t}: {self._app_summary(app)}; scripts present: {sorted(pm._script_names(apps))}"
        except Exception as error:  # noqa: BLE001
            now = f"the panel could not be read ({type(error).__name__})"
        self.entry["panel_state"] = f"FAILED: {now}. Last step: {self.entry['panel_state']}"
        if old is not None and new is not None:
            again = " ".join(self.opts.argv) if self.opts.argv else "the same command"
            first = (f"Re-running '{again}' resumes the rollback to {old.version}" if self.entry.get("rollback_reason")
                     else f"Re-running '{again}' resumes from what the panel reports")
            self.entry["next"] = (
                f"{first}. To put {old.version} back from any state instead (it never sends {new.version}): "
                f"{self._rollback_command()}"
            )
        banner = "!" * 72
        self.logger.critical("%s", banner)
        self.logger.critical("SCRIPT SWAP FAILED for %s: %s", t, reason)
        self.logger.critical("Panel now: %s", now)
        if self.entry.get("next"):
            self.logger.critical("Next: %s", self.entry["next"])
        self.logger.critical("%s", banner)
        return self._finish(FAILED, EXIT_FAILED, error=reason)

    # ------------------------------------------------------------ fidelity

    def _fidelity(self, src: SourceInfo, boot: Boot) -> str | None:
        """Best-effort evidence; returns why the page is clearly broken, else None."""
        if not self.opts.fidelity:
            self._step("fidelity", skipped="--no-fidelity")
            return None
        t = self.opts.target
        result: dict[str, Any] = {"readback": self._readback(src, boot.block)}
        if result["readback"].get("status") == "mismatch":
            self._warn(f"the {t} source read back differs from what was sent "
                       f"({result['readback'].get('bytes')} B, sha256 {result['readback'].get('sha256', '')[:12]})")
        watch = self._watch_screen()
        result["screen"] = {key: value for key, value in watch.items() if key != "frames"}
        judgement = judge_screen(watch["captures"], t)
        result["screen"]["judgement"] = judgement
        result["simulator"] = self._simulator(watch, src, judgement)
        if result["simulator"].get("status") == "mismatch":
            self._warn("the disc in the captured frames differs from moon_panel_sim's render "
                       f"({result['simulator']['results']}); evidence only, not a failure")
        app = None
        apps = self._try_read(APPS_PATH)
        try:
            app = find_app(inventory(apps), t) if apps is not None else None
        except SwapError:
            app = None
        result["target_after"] = self._app_summary(app)
        broken = None
        if app is not None and app_error(app):
            broken = f"{t} reports error {app_error(app)!r} after {self.opts.screen_seconds:g} s"
        elif result["screen"]["judgement"]["verdict"] == "broken":
            if self.opts.require_dark and not self.opts.check_only:
                # A night install sees the matrix lit only at moments nobody
                # has characterised (the boot-lit second, a person): evidence
                # for the morning's --check-only, never a reason to roll back.
                result["advisory"] = result["screen"]["judgement"]["reason"]
                self._warn(f"screen check says broken ({result['advisory']}); with --require-dark that is "
                           "evidence only -- run --check-only once the matrix is lit")
            else:
                broken = result["screen"]["judgement"]["reason"]
        result["broken"] = broken
        self.entry["fidelity"] = result
        self._step("fidelity", **result)
        verdict = result["screen"]["judgement"]
        self.logger.info("Fidelity: readback %s; screen %s (%s); simulator %s.",
                         result["readback"].get("status"), verdict["verdict"], verdict.get("reason") or "-",
                         result["simulator"].get("status"))
        return broken

    def _readback(self, src: SourceInfo, block: int | None) -> dict[str, Any]:
        if block is None or block < READBACK_MIN_BLOCK:
            return {"status": "skipped",
                    "note": f"largest block {block} < {READBACK_MIN_BLOCK:,}; a ~10 KB readback fails below ~30 KB"}
        url = f"{self.client.base_url}{SCRIPT_PATH.format(name=quote(self.opts.target, safe=''))}"
        try:
            response = self.session.get(url, timeout=self.opts.request_timeout)
            status = int(response.status_code)
            data = bytes(response.content or b"")
        except requests.RequestException as error:
            return {"status": "failed", "note": type(error).__name__}
        if status != 200 or not data:
            return {"status": "failed", "note": f"HTTP {status}, {len(data)} bytes"}
        digest = hashlib.sha256(data).hexdigest()
        return {"status": "match" if digest == src.sha256 else "mismatch", "bytes": len(data), "sha256": digest}

    def _watch_screen(self) -> dict[str, Any]:
        """Capture frames while the target is the current app (bounded)."""
        t = self.opts.target
        deadline = self._mono() + self.opts.screen_seconds
        captures: list[dict[str, Any]] = []
        frames: list[list[int]] = []
        fps: list[Any] = []
        powers: set[Any] = set()
        previous = None
        visit_start = 0.0
        bracketed = False
        visits = 0
        power_seen: Any = None
        power_changed: float | None = None

        def observe(power: Any) -> None:
            nonlocal power_seen, power_changed
            if type(power) is not bool:
                return
            if power_seen is not None and power != power_seen:
                power_changed = self._mono()
            power_seen = power
            powers.add(power)

        lit_and_settled = 0
        while self._mono() < deadline and len(captures) < MAX_CAPTURES:
            device = self._try_read(DEVICE_PATH)
            if not isinstance(device, Mapping):
                previous = None
                self._sleep(2.0)
                continue
            current = device.get("currentApp")
            power = device.get("matrixPower")
            observe(power)
            settled = power_changed is None or self._mono() - power_changed >= POWER_SETTLE_SECONDS
            if power is True and settled and (_uptime(device) or 0) >= BOOT_LIT_SECONDS:
                lit_and_settled += 1
            if current != t:
                previous = current
                self._sleep(2.0)
                continue
            if previous != t:
                visits += 1
                visit_start = self._mono()
                bracketed = previous is not None
            previous = current
            fps.append(device.get("fps"))
            screen = self._try_read(SCREEN_PATH)
            age = self._mono() - visit_start
            after = self._try_read(DEVICE_PATH)
            power_after = after.get("matrixPower") if isinstance(after, Mapping) else None
            observe(power_after)
            settled = power_changed is None or self._mono() - power_changed >= POWER_SETTLE_SECONDS
            stats = frame_stats(screen)
            lit = (power is True and power_after is True and settled
                   and (_uptime(device) or 0) >= BOOT_LIT_SECONDS
                   and isinstance(after, Mapping) and after.get("currentApp") == t)
            captures.append({
                "visit": visits,
                "age": round(age, 2),
                "bracketed": bracketed,
                "power": power,
                "power_after": power_after,
                "lit": lit,
                "uptime": _uptime(device),
                "at": self._now().timestamp(),
                "stats": stats,
                "disc_window": bracketed and DISC_WINDOW_SECONDS[0] <= age <= DISC_WINDOW_SECONDS[1],
            })
            frames.append(list(screen["pixels"]) if stats else [])
            self._sleep(1.0)
        return {
            "visits": visits,
            "captures": captures,
            "frames": frames,
            "fps_while_current": fps,
            "matrix_power_seen": sorted(str(power) for power in powers),
            "lit_and_settled_readings": lit_and_settled,
        }

    def _simulator(self, watch: dict[str, Any], src: SourceInfo, judgement: Mapping[str, Any]) -> dict[str, Any]:
        """Compare the disc with ``weather/moon_panel_sim.render()``; evidence only, never a verdict.

        Only the disc body is compared, and only in frames taken while the
        camera is at rest: those pixels depend on the phase (from the epoch)
        and the hour tint, not on ``now_ms`` or the visit start, which a
        capture cannot pin down to the millisecond.
        """
        if self.opts.target != "moon":
            return {"status": "skipped", "note": "no simulator for this script"}
        try:
            sim = importlib.import_module("weather.moon_panel_sim")
        except Exception as error:  # noqa: BLE001 - absent or half-written
            return {"status": "skipped", "note": f"weather/moon_panel_sim.py not importable ({type(error).__name__})"}
        render = getattr(sim, "render", None)
        versions = getattr(sim, "VERSIONS", None)
        load_tables = getattr(sim, "load_tables", None)
        if not callable(render) or not callable(load_tables) or not isinstance(versions, Mapping) \
                or src.version not in versions:
            return {"status": "skipped", "note": f"weather.moon_panel_sim has no render() for {src.version}"}
        if not judgement.get("rendering"):
            return {"status": "skipped", "note": "the framebuffer was not being drawn (matrix dark), nothing to compare"}
        try:
            mask = disc_mask(load_tables(versions[src.version].SOURCE).span0)
        except Exception as error:  # noqa: BLE001
            return {"status": "skipped", "note": f"could not read the disc geometry ({type(error).__name__})"}
        results: list[dict[str, Any]] = []
        for capture, frame in zip(watch["captures"], watch["frames"]):
            if not frame or not capture.get("disc_window") or not counts_as_lit(capture):
                continue
            local = datetime.fromtimestamp(capture["at"])
            try:
                drawn = render(src.version, (capture["uptime"] or 0) * 1000, epoch_ms=int(capture["at"] * 1000),
                               hour=local.hour, minute=local.minute, year=local.year)
                pixels = list(drawn.pixels)
            except Exception as error:  # noqa: BLE001
                results.append({"age": capture["age"], "error": f"{type(error).__name__}: {error}"[:200]})
                continue
            differ = sum(1 for i in mask if (pixels[i] & 0xFFFFFF) != (frame[i] & 0xFFFFFF))
            results.append({"age": capture["age"], "pixels": len(mask), "differ": differ})
        compared = [result for result in results if "differ" in result]
        if not compared:
            return {"status": "skipped", "note": "no frame was taken while the disc was at rest", "results": results}
        status = "match" if all(result["differ"] <= SIM_TOLERANCE_PIXELS for result in compared) else "mismatch"
        return {"status": status, "results": results,
                "note": f"disc body at rest vs moon_panel_sim.render({src.version!r}); "
                        f"<= {SIM_TOLERANCE_PIXELS} differing pixels counts as a match"}

    # ------------------------------------------------------------ check only

    def _check_only(self, app: dict[str, Any] | None, state: str, device: dict[str, Any]) -> int:
        t, new = self.opts.target, self.new
        problems: list[str] = []
        if state != NEW_OK:
            problems.append(f"{t} state is {state!r}: {self._app_summary(app)}")
        heap = self.entry["before"].get("boot_heap")
        if heap is not None and t not in heap and state == NEW_OK:
            problems.append(f"this boot's log has no 'vm heap' line for {t}")
        if app is not None and app.get("present") is True and app.get("headless") is not True:
            if app.get("inLoop") is False or app.get("enabled") is False:
                problems.append(f"{t} is not in the rotation (inLoop {app.get('inLoop')!r}, "
                                f"enabled {app.get('enabled')!r}); rotation_policy.py shows the intended order")
            loop = self._loop_position()
            if loop is not None and (loop["index"] is None or loop["disabled"]):
                problems.append(f"/apploop.json does not rotate {t} ({loop})")
        broken = None
        if state in (NEW_OK, NEW_ERROR):
            boot = Boot(reading={"block": _block(device)}, apps=[], scripts=set(), heap=heap)
            broken = self._fidelity(new, boot)
            if broken:
                problems.append(broken)
            screen = (self.entry.get("fidelity") or {}).get("screen") or {}
            if (self.opts.fidelity and screen.get("visits") == 0
                    and screen.get("lit_and_settled_readings", 0) > 0):
                problems.append(f"{t} never came on screen during the {self.opts.screen_seconds:g} s watch "
                                "while the matrix was lit, so its page could not be checked and it may not be "
                                "rotating (a long rotation can do this too: re-run with a larger --screen-seconds)")
        self.entry["check"] = {"problems": problems}
        if problems:
            for problem in problems:
                self.logger.error("Check: %s", problem)
            return self._finish(CHECKED_BROKEN, EXIT_CHECK_FAILED, error="; ".join(problems))
        return self._finish(CHECKED_OK, EXIT_OK)

    # ------------------------------------------------------------ finish

    def _finish(self, outcome: str, code: int, *, error: str | None = None) -> int:
        t = self.opts.target
        self.entry.update(outcome=outcome, exit_code=code, finished=self._now().isoformat(timespec="seconds"))
        if error:
            self.entry["error"] = error
        inherited = self.previous if (self.previous and self.previous.get("outcome") in UNFINISHED) else None
        if self.writes == 0 and outcome in (ABORTED, INTERRUPTED, PAUSED) and inherited is not None:
            # This run sent nothing, but an earlier one left the panel mid-swap.
            self.entry["panel_state"] = (f"no write was sent by this run; the panel is as run {inherited.get('id')} "
                                         f"({inherited.get('outcome')}) left it: {inherited.get('panel_state')}")
            self.entry["next"] = ("Re-run the same command once the reason above is gone: it resumes run "
                                  f"{inherited.get('id')}.")
        elif outcome in (DRY_RUN, CHECKED_OK, CHECKED_BROKEN, ALREADY_INSTALLED) or (
                outcome in (ABORTED, INTERRUPTED, PAUSED) and self.writes == 0):
            self.entry["panel_state"] = "untouched: no write was sent by this run"
        elif outcome == ROLLED_BACK and self.old is not None:
            self.entry["panel_state"] = (f"{t} {self.old.version} reinstalled and verified; "
                                         f"{self.new.version} is not installed")
        elif outcome in (OK, NEEDS_ATTENTION) and self.new is not None:
            self.entry["panel_state"] = f"{t} {self.new.version} installed and verified"
            if outcome == NEEDS_ATTENTION:
                self.entry["panel_state"] += f", but {'; '.join(self.attention)}"
            if not self.read_only:
                self.entry["next"] = (
                    "draw() probably does not run while the matrix is dark (moon logs ~42 FPS dark vs ~25 "
                    "lit), so check the lit page after 07:30 with the same command plus --check-only"
                )
                if outcome == NEEDS_ATTENTION:
                    self.entry["next"] = ("Put it back in the rotation (rotation_policy.py shows and can apply the "
                                          "intended order); then " + self.entry["next"])
        elif outcome in (INTERRUPTED, PAUSED):
            self.entry["next"] = "Re-run the same command: it resumes from what the panel reports."
        try:
            self._write_report()
        except OSError as failure:
            self.logger.error("Could not write the report %s: %s", self.report.path, failure)
        log = self.logger.info if code in (EXIT_OK,) else self.logger.warning
        log("Outcome: %s (exit %d). Panel: %s", outcome, code, self.entry["panel_state"])
        if self._report_ok:
            log("Report: %s", self.report.path)
        return code


# --------------------------------------------------------------- command line


def configure_logging(data_dir: Path | None) -> logging.Logger:
    logger = logging.getLogger("awtrix.script_swap")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if sys.stdout is not None:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(console)
    if data_dir is not None:
        try:
            Path(data_dir).mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                Path(data_dir) / LOG_NAME,
                maxBytes=sender_logging.MAX_BYTES,
                backupCount=sender_logging.BACKUP_COUNT,
                encoding="utf-8",
            )
        except OSError as error:
            logger.warning("No log file in %s: %s", data_dir, error)
        else:
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(handler)
    return logger


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replace one resident Berry script on the AWTRIX panel, with snapshot, "
                    "verification and rollback.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit codes: 0 ok, 3 aborted (nothing written), 4 rolled back, 5 failed, "
               "6 interrupted or paused (re-run resumes), 7 check-only found a problem, "
               "8 installed but needs a person (e.g. out of the rotation).",
    )
    parser.add_argument("target", help="installed script name, e.g. moon")
    parser.add_argument("source", help="new Berry source (minified before upload)")
    parser.add_argument("--expect-version", required=True, help="the new source's # @version")
    parser.add_argument("--rollback", required=True, help="source of the version installed now")
    parser.add_argument("--rollback-version", required=True, help="the version installed now")
    parser.add_argument("--dry-run", action="store_true", help="preflight only: no write, no snapshot")
    parser.add_argument("--check-only", action="store_true",
                        help="read-only health check of the installed new version (e.g. the morning after)")
    parser.add_argument("--require-dark", action="store_true", help="refuse unless the matrix is off")
    parser.add_argument("--wait-for-nightly", action="store_true",
                        help="wait for tonight's Hub maintenance reboot to finish instead of refusing")
    parser.add_argument("--max-wait-minutes", type=float, default=MAX_WAIT_MINUTES)
    parser.add_argument("--install-if-absent", action="store_true",
                        help="install even though the target is not installed now")
    parser.add_argument("--replace-broken", action="store_true",
                        help="replace the old version even though it reports an error")
    parser.add_argument("--rollback-now", action="store_true",
                        help="put the rollback version back from any state (target absent, new version, "
                             "unfinished run); never sends the new source")
    parser.add_argument("--expect-sha256", default=None,
                        help="refuse unless the minified new source's SHA-256 starts with this (>= 12 hex)")
    parser.add_argument("--rollback-sha256", default=None,
                        help="refuse unless the minified rollback source's SHA-256 starts with this (>= 12 hex)")
    parser.add_argument("--allow-ungated", action="store_true",
                        help="write even though panel_gate is off (AWTRIX_PANEL_GATE=0 or panel_gate.off)")
    parser.add_argument("--no-fidelity", action="store_true", help="skip readback and screen checks")
    parser.add_argument("--panel", default=AWTRIX)
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="report, snapshots and log go here (default %%LOCALAPPDATA%%\\AWTRIX); "
                             "--dry-run and --check-only write no file unless this or --report is given")
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--snapshot-root", type=Path, default=None)
    parser.add_argument("--maintenance-report", type=Path, default=None,
                        help="the Hub's maintenance.json (read only)")
    parser.add_argument("--hub-config", type=Path, default=None, help="the Hub's utility_hub.json (read only)")
    parser.add_argument("--firmware", default=EXPECTED_FIRMWARE)
    parser.add_argument("--boot-timeout", type=float, default=BOOT_TIMEOUT_SECONDS)
    parser.add_argument("--settle-timeout", type=float, default=SETTLE_TIMEOUT_SECONDS)
    parser.add_argument("--screen-seconds", type=float, default=SCREEN_SECONDS)
    parser.add_argument("--rollback-reboots", type=int, default=ROLLBACK_REBOOTS)
    return parser


def options_from_args(args: argparse.Namespace, argv: list[str]) -> tuple[SwapOptions, Path | None]:
    """The options, and the directory for the log file (None: console only).

    A dry run or a check without ``--data-dir``/``--report`` still reads the
    default report (to plan from its history) but writes no file anywhere.
    """
    data_dir = args.data_dir or sender_logging.log_folder()
    read_only = args.dry_run or args.check_only
    write_files = not read_only or args.data_dir is not None or args.report is not None
    options = SwapOptions(
        target=args.target,
        new_path=resolve_source(args.source),
        expect_version=args.expect_version,
        rollback_path=resolve_source(args.rollback),
        rollback_version=args.rollback_version,
        report_path=args.report or data_dir / REPORT_NAME,
        snapshot_root=args.snapshot_root or data_dir / "backups",
        maintenance_report=args.maintenance_report or pm.default_report_path(),
        base_url=args.panel,
        hub_config=args.hub_config,
        dry_run=args.dry_run,
        check_only=args.check_only,
        require_dark=args.require_dark,
        wait_for_nightly=args.wait_for_nightly,
        install_if_absent=args.install_if_absent,
        replace_broken=args.replace_broken,
        rollback_now=args.rollback_now,
        allow_ungated=args.allow_ungated,
        expect_sha256=args.expect_sha256,
        rollback_sha256=args.rollback_sha256,
        report_write=write_files,
        fidelity=not args.no_fidelity,
        firmware=args.firmware,
        boot_timeout=args.boot_timeout,
        settle_timeout=args.settle_timeout,
        max_wait_minutes=args.max_wait_minutes,
        screen_seconds=args.screen_seconds,
        rollback_reboots=args.rollback_reboots,
        argv=list(argv),
    )
    return options, (data_dir if write_files else None)


def main(argv: list[str] | None = None, *, session: Any | None = None, clock: Any | None = None,
         hub_maintenance: HubMaintenance | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    if not SCRIPT_NAME.fullmatch(args.target):
        print(f"invalid script name {args.target!r}", file=sys.stderr)
        return 2
    options, data_dir = options_from_args(args, argv)
    logger = configure_logging(data_dir)
    swap = ScriptSwap(options, session=session, clock=clock, hub_maintenance=hub_maintenance, logger=logger)
    return swap.run()


if __name__ == "__main__":
    raise SystemExit(main())
