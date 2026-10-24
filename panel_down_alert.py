"""Tell the person at the PC, once, when the panel stays unreachable.

On 2026-10-09 the panel stopped answering HTTP at 13:46 and was still down
three and a half hours later; nothing said so.  This Utility Hub service runs
right after ``hub_status.HubStatusReporter`` and reads the result of its 30 s
probe (``reporter.reachable`` / ``reporter.checked_at``).  It never contacts
the panel and never writes a file: the window is its own process,
``panel_down_popup.py``, started detached so it can neither block nor crash
the Hub.

Rules:

- An outage starts at the first failed probe after a success, or at the first
  failed probe after the Hub starts.  A successful probe ends it.
- Once the outage has lasted ``after_minutes``, one popup for that outage.
- At most one popup per ``min_gap_minutes`` overall, so a flapping link cannot
  stack windows.  An outage that reaches its threshold inside the gap waits
  for the gap to end (and then shows only if the panel is still down).
- While a game is running (``game_mode.detect_game``) the popup waits, and the
  scan repeats every 30 s.  A failed process scan counts as "no game", so a
  broken scan can never hold the alert back forever.  Once open, the popup
  runs the same scan itself and steps aside while a game runs.
- Only observed time counts.  If the PC slept between two probes, the outage
  is re-anchored (before the alert) at the first failure after waking, so
  waking the PC does not pop a window at once.  Sleep is told from awake time
  (``hub_status.awake_seconds``, which stops while the PC sleeps), never from
  the probe gap alone: during the 2026-10-09 outage the Hub was awake but its
  ticks stretched the gaps to 292 s, because panel writes inside each tick
  waited out their timeouts.  Without an awake clock, a gap over 15 minutes
  counts as sleep.  Every re-anchor is logged.
- A failed probe while this PC's own network is down (``reporter.pc_offline``:
  the Network Sentinel cannot reach the router) says nothing about the panel.
  Before the alert such probes do not count, and the outage starts at the
  first failure after the PC's network is back.
- A popup this Hub started that is still open blocks another launch.  The
  popup is started outside the Hub's Task Scheduler job when the job allows
  breakaway, so ending the Hub task does not kill it and the task does not
  wait for it to close; if the job forbids breakaway it is started inside
  (ending the task then closes it too).  The popup closes itself once the Hub
  reports the panel reachable, on Dismiss, or after 12 hours.
"""

from __future__ import annotations

from datetime import datetime
import logging
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable, Iterable

import game_mode
import hub_status

POPUP_SCRIPT = Path(__file__).with_name("panel_down_popup.py")
DEFAULT_AFTER_MINUTES = 10.0
DEFAULT_MIN_GAP_MINUTES = 30.0
GAME_RECHECK_SECONDS = 30.0
# Wall time between two probes beyond the awake time between them: the PC
# slept. Slack for wall-clock adjustment and for the moment each probe is read.
SLEEP_SLACK_SECONDS = 120.0
# With no awake clock, a probe gap this long counts as sleep: well above the
# 292 s an awake Hub reached on 2026-10-09 with its writes timing out.
FALLBACK_SLEEP_GAP_SECONDS = 15 * 60.0
ERROR_ACCESS_DENIED = 5

# What tick() reports; handy in tests and when reading the service by hand.
IDLE = "idle"                    # panel answering, or not probed yet
PC_OFFLINE = "pc_offline"        # failing, but this PC's own network is down: not counted
COUNTING = "counting"            # down, threshold not reached
WAITING = "waiting"              # threshold reached; min gap or an open popup holds it
DEFERRED = "deferred"            # threshold reached; a game is running
LAUNCHED = "launched"            # popup started on this tick
LAUNCH_FAILED = "launch_failed"  # popup could not start on this tick (logged)
ALERTED = "alerted"              # this outage already had its one attempt


def pythonw_executable(executable: str | None = None) -> str:
    """The windowless interpreter: ``sys.executable`` if it is pythonw, else its sibling."""
    current = Path(executable if executable is not None else (sys.executable or "python"))
    if current.name.lower() == "pythonw.exe":
        return str(current)
    sibling = current.with_name("pythonw.exe")
    if sibling.is_file():
        return str(sibling)
    # No pythonw (not Windows, or an unusual install): a detached python.exe
    # still gets no console.
    return str(current)


def detached_flags(breakaway: bool = True) -> int:
    """No console, own process group, and (by default) out of the Hub's job."""
    if os.name != "nt":
        return 0
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    if breakaway:
        flags |= subprocess.CREATE_BREAKAWAY_FROM_JOB
    return flags


def _access_denied(error: OSError) -> bool:
    """How CreateProcess refuses CREATE_BREAKAWAY_FROM_JOB in a job without
    JOB_OBJECT_LIMIT_BREAKAWAY_OK (measured 2026-10-09: PermissionError, winerror 5)."""
    return isinstance(error, PermissionError) or getattr(error, "winerror", None) == ERROR_ACCESS_DENIED


def popup_command(since: float, status_path: Path, executable: str | None = None,
                  extra_games: Iterable[str] = ()) -> list[str]:
    command = [
        pythonw_executable(executable), str(POPUP_SCRIPT),
        "--since", f"{since:.3f}", "--status", str(status_path),
    ]
    # "=" form, so a name starting with "-" is still read as the value.
    return command + [f"--extra-game={name}" for name in extra_games]


def running_game(extra_games: Iterable[str] = ()) -> game_mode.GameSession | None:
    return game_mode.detect_game(game_mode.list_processes(), extra_games)


def _clock_label(epoch: float) -> str:
    try:
        return datetime.fromtimestamp(epoch).strftime("%H:%M")
    except (OverflowError, OSError, ValueError):
        return "?"


def _minutes(seconds: float) -> str:
    return f"{max(0, int(seconds // 60))} min"


class PanelDownAlert:
    """Hub service: one detached popup per long panel outage."""

    def __init__(
        self,
        reporter: Any,
        *,
        after_minutes: float = DEFAULT_AFTER_MINUTES,
        min_gap_minutes: float = DEFAULT_MIN_GAP_MINUTES,
        extra_games: Iterable[str] = (),
        status_path: Path | None = None,
        clock: Callable[[], float] | None = None,
        monotonic: Callable[[], float] | None = None,
        awake: Callable[[], float | None] | None = None,
        game_check: Callable[[], object | None] | None = None,
        popen: Callable[..., Any] | None = None,
        executable: str | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if not after_minutes > 0:
            raise ValueError("after_minutes must be positive")
        if not min_gap_minutes >= 0:
            raise ValueError("min_gap_minutes cannot be negative")
        self.reporter = reporter
        self.after_seconds = float(after_minutes) * 60.0
        self.min_gap_seconds = float(min_gap_minutes) * 60.0
        self.extra_games = tuple(extra_games)
        path = status_path if status_path is not None else getattr(reporter, "path", None)
        self.status_path = Path(path) if path is not None else (
            hub_status.default_data_dir() / hub_status.STATUS_NAME)
        self.clock = clock or time.time
        self.monotonic = monotonic or time.monotonic
        self.awake = awake or hub_status.awake_seconds
        self.game_check = game_check or (lambda: running_game(self.extra_games))
        self.popen = popen or subprocess.Popen
        self.executable = executable
        self.logger = logger or logging.getLogger("awtrix.utility_hub.panel_down")

        self.outage_since: float | None = None   # wall time of the first failed probe
        self.alerted = False                      # this outage had its one attempt
        self.popup: Any = None                    # Popen handle while it runs
        self.last_launch_at: float | None = None  # monotonic, for the min gap
        self.next_game_check_at = float("-inf")
        self._seen_probe: tuple[float, bool | None] | None = None  # (checked_at, reachable)
        self._seen_awake: float | None = None     # awake clock when that probe was read
        self.pc_offline = False                   # failing while this PC's network is down
        self._game_logged = False
        self._scan_failure_logged = False

    # ------------------------------------------------------------ probes

    def _reset(self) -> None:
        self.outage_since = None
        self.alerted = False
        self.next_game_check_at = float("-inf")
        self._game_logged = False
        self._scan_failure_logged = False

    def _awake_now(self) -> float | None:
        try:
            value = self.awake()
        except Exception:  # noqa: BLE001 - no awake clock: fall back to the gap
            return None
        return float(value) if type(value) in (int, float) and math.isfinite(value) else None

    @staticmethod
    def _asleep(wall_gap: float, awake_before: float | None, awake_after: float | None) -> float | None:
        """Seconds the PC slept between two probes (None: it did not, as far as can be told)."""
        if awake_before is not None and awake_after is not None:
            asleep = wall_gap - (awake_after - awake_before)
            return asleep if asleep > SLEEP_SLACK_SECONDS else None
        return wall_gap if wall_gap > FALLBACK_SLEEP_GAP_SECONDS else None

    def _observe_probe(self) -> None:
        checked = getattr(self.reporter, "checked_at", None)
        reachable = getattr(self.reporter, "reachable", None)
        if type(checked) not in (int, float) or (checked, reachable) == self._seen_probe:
            return
        previous = self._seen_probe[0] if self._seen_probe is not None else None
        awake_before, awake_now = self._seen_awake, self._awake_now()
        self._seen_probe = (checked, reachable)
        self._seen_awake = awake_now
        if reachable is True:
            if self.outage_since is not None and self.alerted:
                self.logger.info(
                    "panel answering again after %d min",
                    max(0, int((checked - self.outage_since) // 60)),
                )
            self._reset()
            self.pc_offline = False
            return
        if reachable is not False:
            return
        if getattr(self.reporter, "pc_offline", None) is True:
            self._observe_offline_failure()
            return
        self.pc_offline = False
        if self.outage_since is None:
            self._reset()
            self.outage_since = float(checked)
            return
        if self.alerted or previous is None:
            return  # after the alert, sleep or not, it is the same outage
        asleep = self._asleep(checked - previous, awake_before, awake_now)
        if asleep is not None:
            how = (f"PC asleep about {_minutes(asleep)}" if awake_before is not None and awake_now is not None
                   else "no awake clock, so counted as sleep")
            self.logger.info(
                "no panel probe for %s (%s); panel-down count restarts at %s instead of %s",
                _minutes(checked - previous), how, _clock_label(checked), _clock_label(self.outage_since),
            )
            self._reset()
            self.outage_since = float(checked)

    def _observe_offline_failure(self) -> None:
        """A failed probe while this PC has no network of its own."""
        if self.alerted:
            return  # the popup is up; it words this from the status file
        if not self.pc_offline:
            self.pc_offline = True
            self.logger.info(
                "this PC's own network is down (Network Sentinel: router unreachable); "
                "failed panel probes are not counted until it is back")
        self._reset()

    def _reap_popup(self) -> None:
        if self.popup is None:
            return
        try:
            finished = self.popup.poll() is not None
        except Exception:  # noqa: BLE001 - a broken handle is a closed popup
            finished = True
        if finished:
            self.popup = None

    # ------------------------------------------------------------ games

    def _game_running(self) -> object | None:
        try:
            return self.game_check()
        except Exception as error:  # noqa: BLE001 - never defer on a broken scan
            if not self._scan_failure_logged:
                self._scan_failure_logged = True
                self.logger.warning("process scan failed (%s); not deferring the popup", error)
            return None

    # ------------------------------------------------------------ launch

    def _start(self, command: list[str]) -> Any:
        options = dict(close_fds=True, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            return self.popen(command, creationflags=detached_flags(breakaway=True), **options)
        except OSError as error:
            if detached_flags(breakaway=True) == detached_flags(breakaway=False) or not _access_denied(error):
                raise
            # The Hub's job forbids breakaway; inside it, ending the task closes the popup too.
            self.logger.info("popup cannot leave the Hub's job (%s); starting it inside the job", error)
            return self.popen(command, creationflags=detached_flags(breakaway=False), **options)

    def _launch(self, now: float) -> str:
        since = float(self.outage_since)  # type: ignore[arg-type]
        self.alerted = True  # one attempt per outage, whatever happens next
        try:
            self.popup = self._start(
                popup_command(since, self.status_path, self.executable, self.extra_games))
        except Exception as error:  # noqa: BLE001 - the Hub must never trip over a popup
            self.popup = None
            self.logger.warning("panel-down popup could not start: %s", error)
            return LAUNCH_FAILED
        self.last_launch_at = now
        self.logger.info(
            "panel unreachable since %s (%d min); showing the panel-down popup",
            _clock_label(since), max(0, int((self.clock() - since) // 60)),
        )
        return LAUNCHED

    def tick(self) -> str:
        self._observe_probe()
        self._reap_popup()
        if self.outage_since is None:
            return PC_OFFLINE if self.pc_offline else IDLE
        if self.alerted:
            return ALERTED
        if self.clock() - self.outage_since < self.after_seconds:
            return COUNTING
        now = self.monotonic()
        if self.popup is not None:
            return WAITING
        if self.last_launch_at is not None and now - self.last_launch_at < self.min_gap_seconds:
            return WAITING
        if now < self.next_game_check_at:
            return DEFERRED
        game = self._game_running()
        if game is not None:
            self.next_game_check_at = now + GAME_RECHECK_SECONDS
            if not self._game_logged:
                self._game_logged = True
                self.logger.info(
                    "panel unreachable since %s; popup waits while %s runs",
                    _clock_label(self.outage_since), getattr(game, "name", "a game"),
                )
            return DEFERRED
        return self._launch(now)
