"""Small, local-only Hub heartbeat and honest panel-delivery indicators."""
from __future__ import annotations

import ctypes
import json
import math
import os
from pathlib import Path
import time
from typing import Callable

from desk_reminder import atomic_json
from focus_control import default_data_dir
from panel_client import (
    WIFI_ERROR_CHARS, WIFI_STATE_CHARS, DeviceHealth, PanelClientError, PanelHTTPError,
    PanelProtocolError, PanelTransportError, short_text,
)

STATUS_NAME = "hub_status.json"
HEALTH_LOG_NAME = "panel_health.jsonl"
# Measured 2026-10-09 by re-emitting the last 400 real probes through this
# writer: ~201 bytes a reachable line (max 210; was ~150 before the Wi-Fi keys),
# 65 bytes a failed probe. One line per 30 s: 1 MB is about 41 hours of a
# reachable panel (it was ~2.3 days), and one rotated file keeps the ~41 hours
# before that. A long outage writes short lines, so it stretches the window.
# Lines are UTF-8 (not \u escapes), and the writer caps every text field in
# characters: ``app`` and ``reason`` 32, ``wstate`` 16, ``werr`` 48. Measured
# worst case, every one at its cap in 4-byte characters and every number at
# its uint32 maximum: 751 bytes (367 if ASCII) with Windows CRLF, one byte less
# with Linux LF. Such lines would
# shorten a file's window in proportion; of the 2,997 real lines on 2026-10-09
# the longest was 158 bytes, and none held a non-ASCII character.
HEALTH_LOG_MAX_BYTES = 1_000_000
APP_CHARS = 32
REASON_CHARS = 32
MAX_AGE = 90
PROBE_INTERVAL_SECONDS = 30.0

# Why the last probe failed, as ``panel_failure`` in hub_status.json (the
# panel-down popup words its advice from it).
FAILURE_UNREACHABLE = "unreachable"  # no answer at all: refused, timed out, no route
FAILURE_HTTP = "http"                # answered, with an HTTP error status
FAILURE_PROTOCOL = "protocol"        # answered, with a body that is not the device JSON
FAILURE_OTHER = "error"


def failure_kind(error: BaseException) -> str:
    if isinstance(error, PanelHTTPError):
        return FAILURE_HTTP
    if isinstance(error, PanelProtocolError):
        return FAILURE_PROTOCOL
    if isinstance(error, (PanelTransportError, OSError)):
        return FAILURE_UNREACHABLE
    return FAILURE_OTHER


def awake_seconds() -> float | None:
    """Seconds this PC has been awake since boot, sleep and hibernation excluded.

    ``time.monotonic()`` (QueryPerformanceCounter) keeps counting while the PC
    sleeps: on 2026-10-09 it read ~35,500 s more than this clock, the length
    of the night's sleep. ``QueryUnbiasedInterruptTime`` does not. None off
    Windows or when the call fails.
    """
    if os.name != "nt":
        return None
    try:
        count = ctypes.c_ulonglong()
        if not ctypes.windll.kernel32.QueryUnbiasedInterruptTime(ctypes.byref(count)):
            return None
    except (AttributeError, OSError):
        return None
    return count.value / 10_000_000


def append_health(path: Path, health, at: float, max_bytes: int = HEALTH_LOG_MAX_BYTES) -> None:
    """One JSON line of panel heap per probe, so a crash leaves the run-up behind.

    Read it by looking for ``uptime`` going backwards: the lines before are the
    heap and largest block in the minutes before the reset, the line after
    carries the ``reason`` (``panic`` is a crash, ``software`` a planned reboot).
    ``app`` is what was on screen at the sample, so FPS and heap can be pinned
    on a page; ``power`` is false while nightkeeper has the matrix dark.

    Wi-Fi, so an outage can be told apart from a crash: ``rssi`` (dBm),
    ``wconn`` (successful connects since boot: a rise inside one boot is a
    reconnect without a reboot), ``watt`` (attempts in the current retry
    cycle), ``wstate``. Those four are written even when None, so every line
    of this writer has the same keys and "not reported" stays distinct from
    "older line". ``werr`` (the panel's last Wi-Fi error) is the exception: it
    is null nearly always and is the longest field, so it appears only when
    set. The SSID is never written; ``panel_client`` cuts it out of the text.
    """
    line = {
        "t": round(at), "reachable": True,
        "uptime": health.uptime_seconds, "reason": short_text(health.reset_reason, REASON_CHARS),
        "free": health.free_heap, "min_free": health.min_free_heap,
        "block": health.largest_block, "fps": health.fps,
        "app": short_text(getattr(health, "current_app", None), APP_CHARS),
        "power": getattr(health, "matrix_power", None),
        "rssi": getattr(health, "wifi_rssi", None),
        "wconn": getattr(health, "wifi_connects", None),
        "watt": getattr(health, "wifi_attempts", None),
        "wstate": short_text(getattr(health, "wifi_state", None), WIFI_STATE_CHARS),
    }
    last_error = short_text(getattr(health, "wifi_last_error", None), WIFI_ERROR_CHARS)
    if last_error is not None:
        line["werr"] = last_error
    _append_health_line(path, line, max_bytes)


def append_probe_failure(path: Path, error: Exception, at: float) -> None:
    """Keep failed probes in the timeline without storing URLs or error bodies."""
    _append_health_line(path, {
        "t": round(at), "reachable": False, "error": type(error).__name__,
    }, HEALTH_LOG_MAX_BYTES)


def _append_health_line(path: Path, line: dict, max_bytes: int) -> None:
    try:
        if path.exists() and path.stat().st_size >= max_bytes:
            os.replace(path, path.with_suffix(".1.jsonl"))
        text = json.dumps(line, separators=(",", ":"), ensure_ascii=False) + "\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)
    except (OSError, ValueError):
        pass  # evidence is best-effort; it must never take the hub down


def fresh(timestamp, now: float, limit: float = MAX_AGE) -> bool:
    return type(timestamp) in (int, float) and math.isfinite(timestamp) and 0 <= now - timestamp <= limit


class HubStatusReporter:
    """Run after services; panel reachability is sampled at most every 30s.

    A successful PUT is a delivery receipt, not evidence of physical LEDs.
    Configuration here is the running Hub's configuration, not an edited file
    that might still be waiting for a restart.

    Each probe also records ``failure`` (``failure_kind`` of the error, None
    when it answered) and ``pc_offline``: what ``pc_offline()`` (the Network
    Sentinel: this PC's own router does not answer) said at that probe, or
    None when nothing can tell. Both go into the status file, as
    ``panel_failure`` and ``pc_offline``, for the panel-down alert and popup.
    """
    def __init__(self, panel, *, reminders_enabled: bool, reminder=None,
                 path: Path | None = None, clock=None, monotonic=None,
                 health_log: Path | None = None,
                 pc_offline: Callable[[], object] | None = None):
        self.panel = panel
        self.reminders_enabled = reminders_enabled
        self.reminder = reminder
        self.path = path if path is not None else default_data_dir() / STATUS_NAME
        self.health_log = health_log if health_log is not None else self.path.with_name(HEALTH_LOG_NAME)
        self.clock = clock or time.time
        self.monotonic = monotonic or time.monotonic
        self.pc_offline_check = pc_offline
        self.next_probe = float("-inf")
        self.checked_at = None
        self.reachable = None
        self.failure: str | None = None
        self.pc_offline: bool | None = None

    def _sample_pc_offline(self) -> bool | None:
        if self.pc_offline_check is None:
            return None
        try:
            value = self.pc_offline_check()
        except Exception:  # noqa: BLE001 - no answer is "cannot tell", never a crash
            return None
        return value if isinstance(value, bool) else None

    def tick(self):
        if self.monotonic() >= self.next_probe:
            try:
                health = self.panel.probe_device()
                self.reachable = True
                self.failure = None
                if isinstance(health, DeviceHealth):
                    append_health(self.health_log, health, self.clock())
            except (PanelClientError, OSError) as error:
                self.reachable = False
                self.failure = failure_kind(error)
                append_probe_failure(self.health_log, error, self.clock())
            self.pc_offline = self._sample_pc_offline()
            self.checked_at = self.clock()
            self.next_probe = self.monotonic() + PROBE_INTERVAL_SECONDS
        state = {
            "schema_version": 1, "updated_at": self.clock(), "pid": os.getpid(),
            "panel_reachable": self.reachable, "panel_checked_at": self.checked_at,
            "panel_failure": self.failure, "pc_offline": self.pc_offline,
            "reminders_enabled": self.reminders_enabled,
            "reminder_delivery": self.reminder.delivery_status if self.reminder else None,
        }
        atomic_json(self.path, state)
        return state


def read_status(path: Path) -> dict | None:
    try:
        with path.open("rb") as handle:
            body = handle.read(4097)
        if len(body) > 4096:
            return None
        value = json.loads(body)
        if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
            return None
        if type(value.get("reminders_enabled")) is not bool or type(value.get("panel_reachable")) is not bool:
            return None
        return value
    except (OSError, ValueError, UnicodeError):
        return None


def indicator_text(status: dict | None, now: float, reminder_id: str | None = None) -> tuple[str, str]:
    if status is None or not fresh(status.get("updated_at"), now):
        return "Panel · status unavailable", "Reminders · Hub status unavailable"
    if not fresh(status.get("panel_checked_at"), now):
        panel = "Panel · last check is stale"
    elif status.get("panel_reachable") is True:
        panel = f"Panel · reachable ({int(now - status['panel_checked_at'])}s ago)"
    else:
        panel = "Panel · unreachable"
    if not status.get("reminders_enabled"):
        return panel, "Reminders · disabled"
    if reminder_id is None:
        return panel, "Reminders · enabled"
    receipt = status.get("reminder_delivery")
    if (isinstance(receipt, dict) and receipt.get("id") == reminder_id
            and receipt.get("phase") in {"countdown", "due"}
            and fresh(receipt.get("sent_at"), now, 30)):
        return panel, "Reminders · sent to panel"
    return panel, "Reminders · waiting for delivery"
