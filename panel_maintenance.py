"""Panel maintenance: restart the panel nightly while it is dark, or sooner
when its memory runs low, then check it.

Why this exists.  The ESP32's heap fragments with uptime.  Free heap stays
roughly level, but the *largest contiguous block* -- the number that decides
whether a script installs, a TLS handshake fits, or an animation can be pushed
-- falls from ~28-32 KB after a boot to ~12 KB after a week, and at around nine
days the API starts returning empty bodies.  Only a reboot gives it back.
Until now that reboot was a manual step people had to remember.

What the nightly run does.  Once per night, inside a window that sits within
Nightkeeper's blackout, if the panel has been up long enough and its matrix is
off, it asks the panel to restart.  Then it waits for the panel to come back,
lets the scripts load, and writes a report: per-script VM heap from the boot
log, the fresh largest block, any script that came back with an error, and
whether the display came back dark.  The report is the honest per-script heap
measurement CLAUDE.md asks for, collected every night instead of once in a
while.

What the nightly run will not do.  It never reboots while the matrix is lit:
a lit panel inside the window means a person turned it on, and Nightkeeper
already treats that as a manual hold.  It tries once per night, whatever
happens.  A panel that does not come back is logged loudly and left alone.
(Low-memory reboots, below, are the exception to the first two rules.)

Low-memory reboots.  Waiting for the window is not always safe.  On 2026-10-07
the panel stopped answering HTTP for 81 minutes from 20:29 -- still running,
its minimum free heap since boot down to 6,740 and then 5,444 bytes -- and
only the 21:52 nightly reboot brought it back.  So, when enabled, the panel is
also read every minute at any hour and restarted, lit or not, as soon as its
minimum free heap since boot is under a floor (it was under 8 KB from 19:43,
46 minutes before that outage, while the largest block was still ~13 KB).
That signal waits two hours after a boot: it is a low-water mark that one
burst can drop by 7.4 KB, and no 8 KB crossing in the logs came sooner.

A largest-block signal is also available -- the block under a floor for
several readings one interval apart on one boot, so a PC sleep, a frozen Hub
or a reboot in between starts the count again -- but it is off by default.
Replayed over 2026-10-04..08 it would only have moved the dark nightly reboot
to a lit evening one, and it never came before an outage; the nightly run
already deals with a low block.

The first reading waits a few minutes after the Hub starts, so the logon
burst of every sender has settled first.  A minimum uptime, and a cap per 24
hours, keep a panel that boots short of memory from being restarted in a
loop.  The cap is counted from the
report (so a Hub restart does not reset it; an attempt is written there before
the reboot is asked for, and trimming never drops the restarts it counts) and
from this process's own memory (so an unwritable report does not lift it).

A restart clears Nightkeeper's in-RAM manual hold, and the matrix boots lit.
So a low-memory restart waits while the matrix is off Nightkeeper's schedule
(lit in the blackout, dark by day): a person is holding it, and the nightly
run does not argue with that either.  It is noted once per boot.  Afterwards
the check expects Nightkeeper's schedule (either state when the attempt spans
21:30 or 07:00), and if a restart still moved a held matrix -- its state was
unknown beforehand -- the report says ``power_override_lost`` and the outcome
is ``attention``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import json
import logging
import math
import os
from pathlib import Path
import re
import time
from typing import Any, Callable

import nightkeeper_logic
from panel_client import DEVICE_PATH, APPS_PATH

DISPLAY_PATH = "/api/v1/display"
LOGS_PATH = "/api/v1/logs"
HISTORY_LIMIT = 30
# Written before the reboot is asked for and replaced by the outcome after it.
IN_PROGRESS = "in-progress"
# Low-block readings further apart than this many check intervals (in panel
# uptime, which keeps counting while the PC sleeps) are not "in a row".
LOW_MEMORY_GAP_INTERVALS = 2
# Wall clock minus uptime estimates the boot instant to within HTTP latency
# and whole-second uptime; another boot moves it by at least the minimum
# uptime, since nothing is noted before then.
SAME_BOOT_TOLERANCE_SECONDS = 120.0
# Clock difference between PC and panel, plus Nightkeeper's reaction time,
# around its 21:30 blackout and 07:00 wake.
POWER_TRANSITION_MARGIN = timedelta(minutes=2)

_VM_HEAP = re.compile(
    r"\[script:(?P<name>[^\]]+)\] vm heap \+(?P<heap>\d+) bytes.*?max block (?P<block>\d+)"
)


def default_report_path() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    base = Path(root) if root else Path.home() / "AppData" / "Local"
    return base / "AWTRIX" / "maintenance.json"


@dataclass(frozen=True)
class MaintenanceConfig:
    enabled: bool = False
    # 21:35-23:00: five minutes into the 21:30-07:00 blackout, while the PC
    # is still awake.  The first window (03:30-05:00) never ran -- the PC
    # sleeps overnight, and the hub with it.
    window_start_minute: int = 21 * 60 + 35
    window_end_minute: int = 23 * 60
    # A panel rebooted by hand yesterday evening does not need another.
    min_uptime_hours: float = 20.0
    require_dark: bool = True
    check_seconds: float = 300.0
    poll_seconds: float = 10.0
    boot_timeout_seconds: float = 300.0
    # Scripts restore in sequence after boot; moon finished 35 s after the
    # others on 2026-09-26.  Read the log after they have all had their turn.
    settle_seconds: float = 90.0
    # Low-memory reboots (see the module docstring).  Off here so a controller
    # built without a config keeps the nightly-only contract; the Hub turns
    # them on.
    low_memory_enabled: bool = False
    # 0 turns the largest-block signal off, the default since 2026-10-09
    # (see the module docstring).
    low_memory_block_bytes: int = 0
    low_memory_min_free_bytes: int = 8192
    # Minimum free heap only ever falls, and one sender burst has dropped it
    # 7.4 KB, so it is not believed this soon after a boot.  The block signal
    # keeps the shorter low_memory_min_uptime_minutes below.
    low_memory_min_free_min_uptime_minutes: float = 120.0
    # Readings in a row: a single low-block reading has recovered within a
    # minute before.  Minimum free heap only ever falls, so one reading of it
    # is enough.
    low_memory_block_checks: int = 3
    low_memory_check_seconds: float = 60.0
    low_memory_min_uptime_minutes: float = 30.0
    low_memory_max_per_day: int = 3
    # Every sender starts at logon with the Hub and pushes at once (2026-10-07
    # 19:43: min_free fell under 8 KB in that burst).  Restarting in the middle
    # of it would wipe the fresh pages and race the Now Playing bridge's
    # install loop, so the first memory reading waits this long.
    low_memory_start_delay_seconds: float = 180.0

    def __post_init__(self) -> None:
        for name in ("window_start_minute", "window_end_minute"):
            value = getattr(self, name)
            if not 0 <= value < 24 * 60:
                raise ValueError(f"{name} must be a minute of the day")
        if self.window_start_minute == self.window_end_minute:
            raise ValueError("maintenance window cannot be empty")
        if self.min_uptime_hours < 0:
            raise ValueError("min_uptime_hours cannot be negative")
        for name in ("low_memory_block_bytes", "low_memory_min_free_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative whole number of bytes")
        for name in ("low_memory_block_checks", "low_memory_max_per_day"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be at least 1")
        if not self.low_memory_check_seconds > 0:
            raise ValueError("low_memory_check_seconds must be positive")
        for name in ("low_memory_min_uptime_minutes", "low_memory_min_free_min_uptime_minutes"):
            if not getattr(self, name) >= 0:
                raise ValueError(f"{name} cannot be negative")
        if not self.low_memory_start_delay_seconds >= 0:
            raise ValueError("low_memory_start_delay_seconds cannot be negative")


def in_window(moment: datetime, start_minute: int, end_minute: int) -> bool:
    minute = moment.hour * 60 + moment.minute
    if start_minute < end_minute:
        return start_minute <= minute < end_minute
    return minute >= start_minute or minute < end_minute


def should_reboot(device: dict[str, Any], display: dict[str, Any],
                  config: MaintenanceConfig) -> tuple[bool, str]:
    """Decide from one readback whether tonight's restart should happen."""
    try:
        uptime = _uptime(device)
    except (KeyError, TypeError, ValueError):
        return False, "device readback has no uptime"
    if uptime < config.min_uptime_hours * 3600:
        return False, f"uptime {uptime / 3600:.1f} h is under {config.min_uptime_hours:g} h"
    if config.require_dark and display.get("power") is not False:
        return False, "matrix is lit; someone is using the panel"
    return True, f"uptime {uptime / 3600:.1f} h, largest block {device.get('largestFreeBlockBytes')}"


def _uptime(device: Any) -> int:
    if not isinstance(device, dict):
        raise ValueError("device readback is not an object")
    value = device.get("uptimeSeconds")
    if type(value) is not int or value < 0:
        raise ValueError("device readback has no valid uptimeSeconds")
    return value


def _byte_count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def low_memory_trigger(device: dict[str, Any], low_block_readings: int,
                       config: MaintenanceConfig) -> dict[str, Any] | None:
    """Why this reading calls for a restart, or None.

    ``low_block_readings`` counts consecutive readings, this one included,
    whose largest block was under the floor.
    """
    min_free = _byte_count(device.get("minFreeHeapBytes"))
    uptime = device.get("uptimeSeconds")
    settled = type(uptime) is int and uptime >= config.low_memory_min_free_min_uptime_minutes * 60
    if settled and min_free is not None and min_free < config.low_memory_min_free_bytes:
        return {"signal": "min_free", "bytes": min_free, "threshold": config.low_memory_min_free_bytes}
    block = _byte_count(device.get("largestFreeBlockBytes"))
    if (block is not None and block < config.low_memory_block_bytes
            and low_block_readings >= config.low_memory_block_checks):
        return {"signal": "block", "bytes": block, "threshold": config.low_memory_block_bytes,
                "readings": low_block_readings}
    return None


def _script_names(apps: Any) -> set[str]:
    if not isinstance(apps, list) or any(not isinstance(app, dict) for app in apps):
        raise ValueError("app inventory is not a list of objects")
    names = set()
    for app in apps:
        if app.get("origin") == "script":
            name = app.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("script inventory has no valid name")
            if type(app.get("present")) is not bool:
                raise ValueError(f"script inventory has no valid presence for {name}")
            if app["present"] is True:
                names.add(name)
    return names


def _post_boot_device(device: Any) -> None:
    _uptime(device)
    if type(device.get("matrixPower")) is not bool:
        raise ValueError("post-boot device readback has no valid matrixPower")
    for key in ("freeHeapBytes", "largestFreeBlockBytes"):
        value = device.get(key)
        if type(value) is not int or value < 0:
            raise ValueError(f"post-boot device readback has no valid {key}")
    # Reported in "after" so a restart that came back short can be seen;
    # optional because the nightly check never needed it.
    if device.get("minFreeHeapBytes") is not None and _byte_count(device["minFreeHeapBytes"]) is None:
        raise ValueError("post-boot device readback has no valid minFreeHeapBytes")
    fps = device.get("fps")
    if type(fps) not in (int, float) or not math.isfinite(fps) or fps < 0:
        raise ValueError("post-boot device readback has no valid fps")


def parse_boot_heap(log_lines: list[str]) -> dict[str, dict[str, int]]:
    """Per-script ``vm heap +N`` and ``max block`` from the panel's boot log.

    The latest line for a script wins, so a script reinstalled after boot
    reports its current cost rather than its restore cost.
    """
    found: dict[str, dict[str, int]] = {}
    for line in log_lines:
        match = _VM_HEAP.search(line)
        if match:
            found[match["name"]] = {"vm_heap": int(match["heap"]), "max_block_after": int(match["block"])}
    return found


def script_errors(apps: list[dict[str, Any]]) -> dict[str, str]:
    errors = {}
    for app in apps:
        if app.get("origin") == "script" and app.get("error"):
            error = app["error"]
            errors[str(app.get("name"))] = str(error.get("message", error) if isinstance(error, dict) else error)
    return errors


def _scheduled(moment: datetime) -> nightkeeper_logic.DisplayTarget:
    return nightkeeper_logic.desired_state(moment)


def held_by_a_person(moment: datetime, power: Any) -> bool:
    """Whether the matrix is off Nightkeeper's schedule, away from 21:30 and 07:00.

    Near a transition the matrix may simply not have followed yet; that is not
    a person, and the caller just waits for the next reading.
    """
    if type(power) is not bool:
        return False
    margin = POWER_TRANSITION_MARGIN
    states = {_scheduled(t).power for t in (moment - margin, moment, moment + margin)}
    return len(states) == 1 and power not in states


def near_power_transition(moment: datetime) -> bool:
    margin = POWER_TRANSITION_MARGIN
    return len({_scheduled(t).power for t in (moment - margin, moment, moment + margin)}) > 1


def judge_restart_power(started: datetime, checked: datetime, before: bool | None,
                        after: bool) -> tuple[bool, bool]:
    """``(power_ok, override_lost)`` for the matrix after a low-memory restart.

    The restart clears Nightkeeper's in-RAM hold and the matrix boots lit, so
    the matrix should end where Nightkeeper's schedule puts it -- either state
    when the attempt spans 21:30 or 07:00, give or take the clocks.  A matrix
    off the schedule before the restart means a person had switched it (or
    Nightkeeper was not running); if the restart moved it, that override is
    gone -- unless a phase change in between would have ended it anyway.
    """
    margin = POWER_TRANSITION_MARGIN
    scheduled = {_scheduled(t).power for t in (started - margin, checked, checked + margin)}
    settled_before = _scheduled(started - margin).power == _scheduled(started + margin).power
    override_lost = (before is not None and settled_before and before != _scheduled(started).power
                     and after != before and _scheduled(started).phase == _scheduled(checked).phase)
    return after in scheduled, override_lost


def _local_time(value: Any) -> datetime | None:
    """A history timestamp as naive local time, or None if it is not one."""
    try:
        at = datetime.fromisoformat(str(value))
        if at.tzinfo is not None:
            at = at.astimezone().replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return at


def _is_low_memory_restart(entry: Any) -> bool:
    return (isinstance(entry, dict) and entry.get("kind") == "low-memory"
            and entry.get("outcome") != "skipped")


def _trim_history(history: list[Any], keep_restarts: int) -> list[Any]:
    """The newest ``HISTORY_LIMIT`` entries, keeping the newest restarts.

    The 24-hour cap is counted from the newest ``keep_restarts`` low-memory
    restarts, so a run of skipped or nightly entries must not push them out.
    """
    excess = len(history) - HISTORY_LIMIT
    if excess <= 0:
        return history
    protected = set([i for i, entry in enumerate(history) if _is_low_memory_restart(entry)][-keep_restarts:])
    kept = []
    for i, entry in enumerate(history):
        if excess > 0 and i not in protected:
            excess -= 1
            continue
        kept.append(entry)
    return kept


@dataclass
class _Attempt:
    started: str
    started_wall: datetime
    before: dict[str, Any]
    reason: str
    started_at: float
    pre_uptime: int
    # None: the inventory could not be read (a low-memory restart goes ahead
    # without it; the missing-script check is then skipped).
    expected_scripts: set[str] | None
    kind: str = "nightly"
    pre_power: bool | None = None
    trigger: dict[str, Any] | None = None
    settle_until: float | None = None
    result: dict[str, Any] = field(default_factory=dict)


class MaintenanceController:
    """Hub service, cheap on every tick.

    The nightly run does real work at most once a night.  With low-memory
    reboots on, it also reads the panel once a minute at any hour and may
    restart it lit (see the module docstring).
    """

    def __init__(self, panel: Any, config: MaintenanceConfig | None = None, *,
                 clock: Any = time, wall_now: Callable[[], datetime] | None = None,
                 report_path: Path | None = None, logger: logging.Logger | None = None) -> None:
        self.panel = panel
        self.config = config or MaintenanceConfig()
        self.clock = clock
        self.wall_now = wall_now or datetime.now
        self.report_path = report_path or default_report_path()
        self.logger = logger or logging.getLogger("awtrix.maintenance")
        self.state = "idle"
        self.next_check_at = 0.0
        self.next_nightly_at = 0.0
        self.next_low_memory_at = self.clock.monotonic() + self.config.low_memory_start_delay_seconds
        self.attempted_on: date | None = None
        self.attempt: _Attempt | None = None
        self.low_block_readings = 0
        self.last_reading_uptime: int | None = None
        # Boot instant (wall clock seconds) of the boot the cap, or a person's
        # hold on the matrix, was last noted for.
        self.cap_noted_boot: float | None = None
        self.hold_noted_boot: float | None = None
        # Low-memory restarts this process asked for: the cap still holds if
        # the report cannot be read or written.
        self.restarts_started: list[datetime] = []
        self.report_unreadable_noted = False

    # ------------------------------------------------------------ states

    def tick(self) -> str:
        if not self.config.enabled:
            return "disabled"
        now = self.clock.monotonic()
        if now < self.next_check_at:
            return self.state
        if self.state == "idle":
            self._idle(now)
        elif self.state == "rebooting":
            self._rebooting(now)
        elif self.state == "settling":
            self._settling(now)
        return self.state

    def _idle(self, now: float) -> None:
        if now >= self.next_nightly_at:
            self.next_nightly_at = now + self.config.check_seconds
            self._nightly(now)
        low_memory = self.config.low_memory_enabled
        if self.state == "idle" and low_memory and now >= self.next_low_memory_at:
            self.next_low_memory_at = now + self.config.low_memory_check_seconds
            self._low_memory(now)
        if self.state == "idle":
            self.next_check_at = (min(self.next_nightly_at, self.next_low_memory_at)
                                  if low_memory else self.next_nightly_at)

    def _nightly(self, now: float) -> None:
        moment = self.wall_now()
        if self.attempted_on == moment.date():
            return
        if not in_window(moment, self.config.window_start_minute, self.config.window_end_minute):
            return
        try:
            device = self.panel.read_json(DEVICE_PATH, operation="maintenance device read")
            display = self.panel.read_json(DISPLAY_PATH, operation="maintenance display read")
        except Exception as err:  # noqa: BLE001
            self.logger.warning("Maintenance check could not read the panel: %s", err)
            return
        go, reason = should_reboot(device, display, self.config)
        if not go:
            # A lit panel is a decision for tonight; do not argue with a person
            # by retrying every five minutes after they deliberately turned it on.
            self.attempted_on = moment.date()
            self.logger.info("Nightly maintenance skipped: %s", reason)
            self._record({"at": moment.isoformat(timespec="seconds"), "outcome": "skipped", "reason": reason})
            return
        if self._start(now, moment, device, reason):
            self.attempted_on = moment.date()

    def _low_memory(self, now: float) -> None:
        try:
            device = self.panel.read_json(DEVICE_PATH, operation="maintenance low-memory read")
            uptime = _uptime(device)
        except Exception:  # noqa: BLE001 -- unreachable or malformed: no evidence either way
            return
        previous, self.last_reading_uptime = self.last_reading_uptime, uptime
        if previous is not None and uptime < previous:
            # Another boot, asked for or not: nothing from before it carries over.
            self.low_block_readings = 0
            self.cap_noted_boot = None
            self.hold_noted_boot = None
        elif (previous is not None
              and uptime - previous > LOW_MEMORY_GAP_INTERVALS * self.config.low_memory_check_seconds):
            # PC asleep, Hub frozen or panel unreachable in between.
            self.low_block_readings = 0
        if uptime < self.config.low_memory_min_uptime_minutes * 60:
            self.low_block_readings = 0
            return
        block = _byte_count(device.get("largestFreeBlockBytes"))
        if block is not None and block < self.config.low_memory_block_bytes:
            self.low_block_readings += 1
        else:
            self.low_block_readings = 0
        trigger = low_memory_trigger(device, self.low_block_readings, self.config)
        if trigger is None:
            return
        moment = self.wall_now()
        power = device.get("matrixPower")
        if near_power_transition(moment):
            return  # Nightkeeper may be mid-switch; the next reading decides
        if held_by_a_person(moment, power):
            boot = moment.timestamp() - uptime
            if self.hold_noted_boot is None or abs(boot - self.hold_noted_boot) > SAME_BOOT_TOLERANCE_SECONDS:
                reason = (f"matrix is {'lit' if power else 'dark'} against Nightkeeper's schedule, so a "
                          f"person is holding it; {trigger['signal']} {trigger['bytes']} < {trigger['threshold']}")
                self.logger.warning("Low-memory maintenance held back: %s", reason)
                self._record({"at": moment.isoformat(timespec="seconds"), "kind": "low-memory",
                              "outcome": "skipped", "reason": reason, "trigger": trigger})
            self.hold_noted_boot = boot
            return
        recent = self._recent_low_memory_reboots(moment)
        cap = self.config.low_memory_max_per_day
        if recent >= cap:
            boot = moment.timestamp() - uptime
            if self.cap_noted_boot is None or abs(boot - self.cap_noted_boot) > SAME_BOOT_TOLERANCE_SECONDS:
                # Once per boot, not once a minute: the cap means restarting is
                # not fixing this, and a person should look.
                reason = (f"{recent} low-memory restarts in 24 h already; "
                          f"{trigger['signal']} {trigger['bytes']} < {trigger['threshold']}")
                self.logger.warning("Low-memory maintenance held back: %s", reason)
                self._record({"at": moment.isoformat(timespec="seconds"), "kind": "low-memory",
                              "outcome": "skipped", "reason": reason, "trigger": trigger})
            self.cap_noted_boot = boot
            return
        label = {"min_free": "minimum free heap", "block": "largest block"}[trigger["signal"]]
        reason = (f"{label} {trigger['bytes']} < {trigger['threshold']} at uptime {uptime / 3600:.1f} h; "
                  f"low-memory restart {recent + 1} of at most {cap} in 24 h")
        if self._start(now, moment, device, reason, kind="low-memory", trigger=trigger):
            self.restarts_started.append(moment)

    def _report_history(self) -> list[Any] | None:
        """The report's history: [] when there is no report yet, None when unreadable."""
        try:
            history = json.loads(self.report_path.read_text(encoding="utf-8")).get("history", [])
        except FileNotFoundError:
            return []
        except (OSError, ValueError, AttributeError):
            return None
        return history if isinstance(history, list) else None

    def _recent_low_memory_reboots(self, moment: datetime) -> int:
        """Low-memory restarts in the 24 h before ``moment``.

        The larger of the report's count and this process's own.  Restarts
        that look later than ``moment`` count too: the wall clock can step
        back (a time correction after resume, the repeated hour when daylight
        time ends), and only one more than a day ahead is taken as bogus.
        """
        since, until = moment - timedelta(hours=24), moment + timedelta(hours=24)
        self.restarts_started = [at for at in self.restarts_started if since <= at <= until]
        history = self._report_history()
        if history is None:
            if not self.report_unreadable_noted:
                self.report_unreadable_noted = True
                self.logger.warning("Could not read maintenance report %s; counting this process's "
                                    "low-memory restarts only", self.report_path)
            history = []
        else:
            self.report_unreadable_noted = False
        reported = 0
        for entry in history:
            if _is_low_memory_restart(entry):
                at = _local_time(entry.get("at"))
                if at is not None and since <= at <= until:
                    reported += 1
        return max(reported, len(self.restarts_started))

    def _start(self, now: float, moment: datetime, device: dict[str, Any], reason: str, *,
               kind: str = "nightly", trigger: dict[str, Any] | None = None) -> bool:
        """Capture the scripts, ask for the restart and start watching for it."""
        inventory_error = None
        try:
            apps = self.panel.read_json(APPS_PATH, operation="maintenance pre-reboot apps read")
            expected_scripts: set[str] | None = _script_names(apps)
        except Exception as err:  # noqa: BLE001
            if kind != "low-memory":
                self.logger.warning("Maintenance could not capture the pre-reboot scripts: %s", err)
                return False
            # The inventory only grades the result.  A ~4 KB read failing is
            # what a panel short of memory does, and no reason to leave it so.
            self.logger.warning("Maintenance could not capture the pre-reboot scripts (%s); "
                                "restarting for memory anyway", err)
            expected_scripts, inventory_error = None, str(err)
        power = device.get("matrixPower")
        self.attempt = _Attempt(
            started=moment.isoformat(timespec="seconds"), started_wall=moment,
            before={k: device.get(k) for k in ("uptimeSeconds", "freeHeapBytes", "minFreeHeapBytes",
                                                "largestFreeBlockBytes", "fps", "matrixPower", "resetReason")},
            reason=reason, started_at=now, pre_uptime=_uptime(device),
            expected_scripts=expected_scripts, kind=kind,
            pre_power=power if type(power) is bool else None, trigger=trigger,
        )
        if inventory_error is not None:
            self.attempt.result["inventory_error"] = inventory_error
        self.low_block_readings = 0
        # On record before the request, so an attempt cut short by a Hub
        # restart is still in the report and still counts toward the cap.
        self._record(self._entry(IN_PROGRESS))
        try:
            self.panel.reboot()
        except Exception as err:  # noqa: BLE001
            # The request may have been cut off by the reboot itself, so carry
            # on and look for the uptime reset rather than giving up.
            self.logger.warning("Reboot request did not complete cleanly (%s); watching for restart", err)
        self.logger.info("%s maintenance reboot requested: %s", _title(kind), reason)
        self.state = "rebooting"
        self.next_check_at = now + self.config.poll_seconds
        return True

    def _rebooting(self, now: float) -> None:
        assert self.attempt is not None
        self.next_check_at = now + self.config.poll_seconds
        try:
            device = self.panel.read_json(DEVICE_PATH, operation="maintenance device read")
            uptime = _uptime(device)
        except Exception:  # noqa: BLE001 -- down while it restarts, expected
            uptime = None
        if uptime is not None and uptime < self.attempt.pre_uptime:
            self.attempt.result["boot_seconds"] = round(now - self.attempt.started_at)
            self.attempt.result["boot_reset_reason"] = device.get("resetReason")
            self.attempt.settle_until = now + self.config.settle_seconds
            self.state = "settling"
            self._observe_power(device, now)
            return
        if now - self.attempt.started_at >= self.config.boot_timeout_seconds:
            self.logger.error("Panel did not come back within %.0f s of the maintenance reboot",
                              self.config.boot_timeout_seconds)
            self._finish("no-return")

    def _observe_power(self, device: dict[str, Any], now: float) -> None:
        # How long the matrix stayed lit after the restart.  Nightkeeper should
        # switch it off within seconds of NTP sync; this is where we find out.
        assert self.attempt is not None
        result = self.attempt.result
        power = device.get("matrixPower")
        if power is True and "lit_since" not in result:
            result["lit_since"] = now
        elif power is False and "dark_after_boot_seconds" not in result:
            result["dark_after_boot_seconds"] = round(now - result["lit_since"]) if "lit_since" in result else 0

    def _settling(self, now: float) -> None:
        assert self.attempt is not None
        result = self.attempt.result
        if now < (self.attempt.settle_until or 0):
            self.next_check_at = now + self.config.poll_seconds
            try:
                device = self.panel.read_json(DEVICE_PATH, operation="maintenance device read")
            except Exception:  # noqa: BLE001
                return
            self._observe_power(device, now)
            return
        try:
            device = self.panel.read_json(DEVICE_PATH, operation="maintenance device read")
            _post_boot_device(device)
            result["after"] = {k: device.get(k) for k in ("uptimeSeconds", "freeHeapBytes", "minFreeHeapBytes",
                                                          "largestFreeBlockBytes", "fps", "matrixPower", "resetReason")}
            apps = self.panel.read_json(APPS_PATH, operation="maintenance apps read")
            restored_scripts = _script_names(apps)
            logs = self.panel.read_json(LOGS_PATH, operation="maintenance log read")
            if (not isinstance(logs, dict) or not isinstance(logs.get("lines"), list)
                    or any(not isinstance(line, str) for line in logs["lines"])):
                raise ValueError("post-boot log readback has no valid lines")
        except Exception as err:  # noqa: BLE001
            self.logger.warning("Panel restarted but the post-boot check failed: %s", err)
            result["verification_error"] = str(err)
            self._finish("restarted-unverified")
            return
        result["script_heap"] = parse_boot_heap(logs["lines"])
        result["script_errors"] = script_errors(apps)
        expected = self.attempt.expected_scripts
        result["missing_scripts"] = sorted(expected - restored_scripts) if expected is not None else None
        self._observe_power(device, now)
        boot_reason = result.get("boot_reset_reason")
        final_reason = device.get("resetReason")
        software_boot = all(isinstance(reason, str) and reason.strip().lower() == "software"
                            for reason in (boot_reason, final_reason))
        if self.attempt.kind == "low-memory":
            power_ok, override_lost = judge_restart_power(
                self.attempt.started_wall, self.wall_now(), self.attempt.pre_power, device["matrixPower"])
            if override_lost:
                result["power_override_lost"] = True
                power_ok = False
        else:
            # The nightly run happens inside the blackout.
            power_ok = device["matrixPower"] is False
        outcome = ("ok" if not result["script_errors"] and not result["missing_scripts"]
                   and power_ok and software_boot else "attention")
        self._finish(outcome)

    def _entry(self, outcome: str) -> dict[str, Any]:
        assert self.attempt is not None
        a = self.attempt
        result = {k: v for k, v in a.result.items() if k != "lit_since"}
        return {"at": a.started, "kind": a.kind, "outcome": outcome, "reason": a.reason,
                **({"trigger": a.trigger} if a.trigger else {}), "before": a.before,
                "expected_scripts": sorted(a.expected_scripts) if a.expected_scripts is not None else None,
                **result}

    def _finish(self, outcome: str) -> None:
        assert self.attempt is not None
        a = self.attempt
        entry = self._entry(outcome)
        before_block = a.before.get("largestFreeBlockBytes")
        after_block = (entry.get("after") or {}).get("largestFreeBlockBytes")
        log = self.logger.info if outcome == "ok" else self.logger.warning
        log("%s maintenance %s: largest block %s -> %s, script errors %s", _title(a.kind),
            outcome, before_block, after_block, entry.get("script_errors") or "none")
        self._record(entry, replaces_in_progress=True)
        self.attempt = None
        self.state = "idle"
        self.next_check_at = self.clock.monotonic() + self.config.check_seconds
        self.next_nightly_at = max(self.next_nightly_at, self.next_check_at)
        self.next_low_memory_at = max(self.next_low_memory_at, self.next_check_at)

    # ------------------------------------------------------------ report

    def _record(self, entry: dict[str, Any], *, replaces_in_progress: bool = False) -> None:
        history = self._report_history() or []
        if replaces_in_progress:
            history = [old for old in history
                       if not (isinstance(old, dict) and old.get("outcome") == IN_PROGRESS
                               and old.get("at") == entry.get("at") and old.get("kind") == entry.get("kind"))]
        history = _trim_history(history + [entry], self.config.low_memory_max_per_day)
        try:
            self.report_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.report_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"last": entry, "history": history}, indent=2), encoding="utf-8")
            tmp.replace(self.report_path)
        except OSError as err:
            self.logger.warning("Could not write maintenance report %s: %s", self.report_path, err)


def _title(kind: str) -> str:
    return {"nightly": "Nightly", "low-memory": "Low-memory"}.get(kind, kind)
