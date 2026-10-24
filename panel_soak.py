r"""Durable day/night stability monitor for the AWTRIX release gate.

The monitor is read-only.  It samples panel health, display state, rotation,
app errors, logs, and the PC-side AWTRIX processes, then compares Nightkeeper's
observed power/brightness with the executable schedule model.  Evidence is
written outside the repository under ``%LOCALAPPDATA%\AWTRIX\soaks``.

Example 24-hour run:
    C:/Users/devyn/anaconda3/python.exe panel_soak.py --duration-hours 24
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import psutil
import requests

from nightkeeper_logic import DEFAULT_SCHEDULE, desired_state
from rotation_policy import TARGET_SETTINGS, TARGET_STATE


AWTRIX = "http://192.168.1.90"
REQUEST_TIMEOUT_SECONDS = 5.0
DEVICE_RETRY_COUNT = 2
DEVICE_RETRY_DELAY_SECONDS = 0.25
NORMAL_SAMPLE_SECONDS = 60.0
TRANSITION_SAMPLE_SECONDS = 15.0
DETAIL_SAMPLE_SECONDS = 300.0
WARN_LARGEST_BLOCK_BYTES = 18_000
FAIL_LARGEST_BLOCK_BYTES = 12_000
PERSISTENT_FAILURE_SAMPLES = 3
NIGHTKEEPER_MISMATCH_SAMPLES = 3

CORE_PATHS = {
    "device": "/api/v1/device",
    "display": "/api/v1/display",
    "settings": "/api/v1/settings",
}
DETAIL_PATHS = {
    "apps": "/api/v1/apps",
    "logs": "/api/v1/logs",
    "apploop": "/apploop.json",
}
LOG_FAILURE_TERMS = (
    "panic",
    "watchdog",
    "out of memory",
    "insufficientstorage",
    "heap too fragmented",
    "heap too tight",
    "syntax_error",
)
AWTRIX_PROCESS_MARKERS = {
    "weatherbrief": "weather_brief_push.py",
    "aqibrief": "aqi_brief_push.py",
    "nowplaying": "now_playing_push.py",
    "pcstats": "pc_stats_push.py",
    "utility_hub": "utility_hub.py",
}
# meridian was removed on 2026-09-01: the app was dropped from the rotation on
# the user's call and its scheduled task unregistered. Leaving it here would
# make every future soak warn about a sender that is gone on purpose.


def default_output_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise RuntimeError("LOCALAPPDATA is unavailable; pass --output-root")
    return Path(local) / "AWTRIX" / "soaks"


# How many heartbeats may be missed before a "running" run is presumed dead.
# Two, so one slow detail cycle is not called a death.
STALE_HEARTBEATS = 2.0


def read_run_status(run: Mapping[str, Any], now: datetime | None = None) -> str:
    """The true status of a run, which is not always the one it recorded.

    A run that is killed cannot write its own epitaph. On Windows, Task
    Scheduler ends a task with TerminateProcess, so the SIGINT/SIGTERM handler
    below never fires and ``run.json`` is left saying ``"running"`` forever.
    Both soaks on 2026-08-29 and 2026-08-30 still claimed to be running days
    later, which made the evidence worse than useless -- it looked like a
    monitor that was still watching.

    So the record carries a heartbeat, and a reader that has not seen one
    recently reports ``"abandoned"`` regardless of what the file says.
    """
    status = str(run.get("status", "unknown"))
    if status != "running":
        return status
    stamp = run.get("lastSampleAt") or run.get("startedAt")
    if not stamp:
        return "abandoned"
    try:
        last = datetime.fromisoformat(str(stamp))
    except ValueError:
        return "abandoned"
    reference = now or datetime.now(last.tzinfo)
    heartbeat = float(run.get("heartbeatSeconds") or DETAIL_SAMPLE_SECONDS)
    if (reference - last).total_seconds() > heartbeat * STALE_HEARTBEATS:
        return "abandoned"
    return "running"


def _atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _append_jsonl(path: Path, value: Any) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, separators=(",", ":"), ensure_ascii=False))
        stream.write("\n")


def transition_window(local_dt: datetime) -> bool:
    """Use a denser cadence around both fade/blackout boundaries."""

    minute = local_dt.hour * 60 + local_dt.minute
    return 20 * 60 + 55 <= minute < 21 * 60 + 36 or 6 * 60 + 55 <= minute < 7 * 60 + 36


def sample_interval(local_dt: datetime) -> float:
    return TRANSITION_SAMPLE_SECONDS if transition_window(local_dt) else NORMAL_SAMPLE_SECONDS


def _get_json(
    session: requests.Session,
    root: str,
    name: str,
    path: str,
    timeout: float,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        response = session.get(f"{root}{path}", timeout=timeout)
        latency = max(0, round((time.monotonic() - started) * 1000))
        response.raise_for_status()
        body = response.json()
        return {
            "ok": True,
            "status": response.status_code,
            "latencyMs": latency,
            "body": body,
        }
    except (requests.RequestException, TypeError, ValueError) as error:
        latency = max(0, round((time.monotonic() - started) * 1000))
        return {
            "ok": False,
            "latencyMs": latency,
            "error": type(error).__name__,
            "endpoint": name,
        }


def _get_device_json(
    session: requests.Session,
    root: str,
    timeout: float,
) -> dict[str, Any]:
    """Retry the health endpoint before recording a transient timeout.

    The device endpoint can briefly miss its five-second window while the
    panel is switching power state.  Retrying only this small health request
    avoids turning a single Wi-Fi/CPU hiccup into a false reboot signal, while
    the endpoint error still remains visible if every attempt fails.
    """

    result = _get_json(session, root, "device", CORE_PATHS["device"], timeout)
    for _ in range(DEVICE_RETRY_COUNT):
        if result.get("ok"):
            break
        time.sleep(DEVICE_RETRY_DELAY_SECONDS)
        result = _get_json(session, root, "device", CORE_PATHS["device"], timeout)
    return result


def _mapping_body(result: Mapping[str, Any]) -> Mapping[str, Any] | None:
    body = result.get("body")
    return body if result.get("ok") and isinstance(body, Mapping) else None


def _app_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        value = value.get("apps", value.get("value"))
    if not isinstance(value, list):
        return []
    fields = ("name", "enabled", "inLoop", "slot", "present", "origin", "skipped", "headless", "error")
    return [
        {key: item.get(key) for key in fields if key in item}
        for item in value
        if isinstance(item, Mapping)
    ]


def collect_processes() -> dict[str, list[dict[str, Any]]]:
    found: dict[str, list[dict[str, Any]]] = {name: [] for name in AWTRIX_PROCESS_MARKERS}
    for process in psutil.process_iter(
        ["pid", "name", "create_time", "cmdline", "memory_info"]
    ):
        try:
            info = process.info
            command = " ".join(info.get("cmdline") or []).lower()
            for service, marker in AWTRIX_PROCESS_MARKERS.items():
                if marker.lower() not in command:
                    continue
                memory = info.get("memory_info")
                found[service].append(
                    {
                        "pid": int(info["pid"]),
                        "createdAt": datetime.fromtimestamp(
                            float(info["create_time"]), timezone.utc
                        ).isoformat(),
                        "rssBytes": int(memory.rss) if memory is not None else None,
                        "process": info.get("name"),
                    }
                )
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError, ValueError):
            continue
    return found


def collect_sample(
    session: requests.Session,
    root: str,
    *,
    detailed: bool,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    local_now: datetime | None = None,
) -> dict[str, Any]:
    local = local_now or datetime.now().astimezone()
    results = {
        name: (_get_device_json(session, root, timeout) if name == "device" else _get_json(session, root, name, path, timeout))
        for name, path in CORE_PATHS.items()
    }
    if detailed:
        results.update(
            {
                name: _get_json(session, root, name, path, timeout)
                for name, path in DETAIL_PATHS.items()
            }
        )

    device = _mapping_body(results["device"])
    display = _mapping_body(results["display"])
    settings = _mapping_body(results["settings"])
    apps_result = results.get("apps", {})
    apps_body = apps_result.get("body") if apps_result.get("ok") else None
    logs = _mapping_body(results.get("logs", {}))
    apploop = _mapping_body(results.get("apploop", {}))

    expected = desired_state(local, DEFAULT_SCHEDULE)
    return {
        "schemaVersion": 1,
        "localTime": local.isoformat(),
        "utcTime": local.astimezone(timezone.utc).isoformat(),
        "detailed": detailed,
        "endpoints": {
            name: {key: value for key, value in result.items() if key != "body"}
            for name, result in results.items()
        },
        "device": {
            key: device.get(key)
            for key in (
                "uid",
                "version",
                "uptimeSeconds",
                "resetReason",
                "freeHeapBytes",
                "minFreeHeapBytes",
                "largestFreeBlockBytes",
                "scriptingRunning",
                "fps",
                "wifiRssi",
                "brightness",
                "matrixPower",
                "currentApp",
            )
        }
        if device is not None
        else None,
        "display": {
            key: display.get(key) for key in ("power", "brightness")
        }
        if display is not None
        else None,
        "settings": {
            key: settings.get(key)
            for key in (
                "autoBrightness",
                "brightness",
                "gamma",
                "saturation",
                "appDurationMs",
                "transitionEffect",
                "transitionDurationMs",
            )
        }
        if settings is not None
        else None,
        "expectedNightkeeper": {
            "phase": expected.phase,
            "power": expected.power,
            "brightness": expected.brightness,
        },
        "apps": _app_rows(apps_body) if detailed else None,
        "apploop": dict(apploop) if apploop is not None else None,
        "panelLogs": list(logs.get("lines", [])) if logs is not None else None,
        "panelLogNext": logs.get("next") if logs is not None else None,
        "processes": collect_processes() if detailed else None,
    }


@dataclass
class SoakState:
    expected_uid: str | None = None
    previous_uptime: int | None = None
    previous_phase: str | None = None
    manual_hold: bool = False
    manual_hold_phase: str | None = None
    seen_log_lines: set[str] = field(default_factory=set)
    baseline_processes: dict[str, tuple[int, ...]] = field(default_factory=dict)
    process_absence_streaks: dict[str, int] = field(default_factory=dict)
    process_started_absent: set[str] = field(default_factory=set)
    process_restart_counts: dict[str, int] = field(default_factory=dict)
    endpoint_failures: dict[str, int] = field(default_factory=dict)
    mismatch_streak: int = 0
    sample_count: int = 0
    detailed_count: int = 0
    reboot_count: int = 0
    endpoint_error_count: int = 0
    min_free_heap: int | None = None
    min_largest_block: int | None = None
    max_latency_ms: int = 0
    violations: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    _event_keys: set[str] = field(default_factory=set, repr=False)

    def event(self, severity: str, key: str, message: str, sample: Mapping[str, Any]) -> None:
        if key in self._event_keys:
            return
        self._event_keys.add(key)
        item = {
            "severity": severity,
            "key": key,
            "message": message,
            "localTime": sample.get("localTime"),
        }
        (self.violations if severity == "fail" else self.warnings).append(item)

    def ingest(self, sample: Mapping[str, Any]) -> list[dict[str, Any]]:
        before_fail = len(self.violations)
        before_warn = len(self.warnings)
        self.sample_count += 1
        if sample.get("detailed"):
            self.detailed_count += 1

        endpoints = sample.get("endpoints")
        if isinstance(endpoints, Mapping):
            for name, value in endpoints.items():
                if not isinstance(value, Mapping):
                    continue
                latency = value.get("latencyMs")
                if isinstance(latency, int):
                    self.max_latency_ms = max(self.max_latency_ms, latency)
                if value.get("ok"):
                    self.endpoint_failures[str(name)] = 0
                else:
                    self.endpoint_error_count += 1
                    failures = self.endpoint_failures.get(str(name), 0) + 1
                    self.endpoint_failures[str(name)] = failures
                    if failures >= PERSISTENT_FAILURE_SAMPLES:
                        self.event(
                            "fail",
                            f"endpoint:{name}",
                            f"{name} failed {failures} consecutive samples",
                            sample,
                        )

        device = sample.get("device")
        if isinstance(device, Mapping):
            uid = device.get("uid")
            if isinstance(uid, str) and uid:
                if self.expected_uid is None:
                    self.expected_uid = uid
                elif uid != self.expected_uid:
                    self.event("fail", "uid-change", "panel UID changed during soak", sample)
            uptime = device.get("uptimeSeconds")
            if isinstance(uptime, int):
                if self.previous_uptime is not None and uptime < self.previous_uptime:
                    self.reboot_count += 1
                    self.event(
                        "fail",
                        f"reboot:{self.reboot_count}",
                        f"uptime rolled back {self.previous_uptime} -> {uptime}",
                        sample,
                    )
                self.previous_uptime = uptime
            if device.get("scriptingRunning") is False:
                self.event("fail", "scripting-stopped", "panel scripting stopped", sample)
            free_heap = device.get("freeHeapBytes")
            if isinstance(free_heap, int):
                self.min_free_heap = free_heap if self.min_free_heap is None else min(self.min_free_heap, free_heap)
            block = device.get("largestFreeBlockBytes")
            if isinstance(block, int):
                self.min_largest_block = block if self.min_largest_block is None else min(self.min_largest_block, block)
                if block < FAIL_LARGEST_BLOCK_BYTES:
                    self.event(
                        "fail",
                        "largest-block-fail",
                        f"largest free block fell below {FAIL_LARGEST_BLOCK_BYTES} bytes ({block})",
                        sample,
                    )
                elif block < WARN_LARGEST_BLOCK_BYTES:
                    self.event(
                        "warn",
                        "largest-block-warn",
                        f"largest free block is below {WARN_LARGEST_BLOCK_BYTES} bytes ({block})",
                        sample,
                    )

        expected = sample.get("expectedNightkeeper")
        display = sample.get("display")
        if isinstance(expected, Mapping):
            phase = expected.get("phase")
            if self.previous_phase is not None and phase != self.previous_phase:
                self.manual_hold = False
                self.manual_hold_phase = None
            self.previous_phase = str(phase) if phase is not None else None

        lines = sample.get("panelLogs")
        if isinstance(lines, Sequence) and not isinstance(lines, (str, bytes)):
            text_lines = [line for line in lines if isinstance(line, str)]
            if not self.seen_log_lines:
                self.seen_log_lines.update(text_lines)
            else:
                for line in text_lines:
                    if line in self.seen_log_lines:
                        continue
                    self.seen_log_lines.add(line)
                    lowered = line.lower()
                    if "manual override held" in lowered:
                        self.manual_hold = True
                        self.manual_hold_phase = self.previous_phase
                    if any(term in lowered for term in LOG_FAILURE_TERMS):
                        self.event("fail", f"panel-log:{line}", f"panel log: {line}", sample)

        if isinstance(expected, Mapping) and isinstance(display, Mapping):
            match = (
                display.get("power") == expected.get("power")
                and display.get("brightness") == expected.get("brightness")
            )
            if match or self.manual_hold:
                self.mismatch_streak = 0
            else:
                self.mismatch_streak += 1
                if self.mismatch_streak >= NIGHTKEEPER_MISMATCH_SAMPLES:
                    self.event(
                        "fail",
                        f"nightkeeper:{self.previous_phase}",
                        "Nightkeeper observed power/brightness disagreed with its schedule "
                        f"for {self.mismatch_streak} consecutive samples",
                        sample,
                    )

        settings = sample.get("settings")
        if isinstance(settings, Mapping):
            actual = {key: settings.get(key) for key in TARGET_SETTINGS}
            if actual != TARGET_SETTINGS:
                self.event("fail", "rotation-settings", f"rotation settings changed: {actual}", sample)

        apploop = sample.get("apploop")
        if isinstance(apploop, Mapping):
            current = {
                "order": list(apploop.get("order", [])),
                "disabled": list(apploop.get("disabled", [])),
            }
            if current != TARGET_STATE.payload():
                self.event("fail", "rotation-order", "saved app-loop policy changed", sample)

        apps = sample.get("apps")
        if isinstance(apps, Sequence) and not isinstance(apps, (str, bytes)):
            for app in apps:
                if isinstance(app, Mapping) and app.get("error"):
                    name = app.get("name", "unknown")
                    self.event("fail", f"app-error:{name}", f"app {name} reports {app.get('error')}", sample)

        processes = sample.get("processes")
        if isinstance(processes, Mapping):
            for service, rows in processes.items():
                if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
                    continue
                pids = tuple(
                    sorted(
                        int(row["pid"])
                        for row in rows
                        if isinstance(row, Mapping) and isinstance(row.get("pid"), int)
                    )
                )
                service_name = str(service)
                if service_name not in self.baseline_processes:
                    self.baseline_processes[service_name] = pids
                    self.process_absence_streaks[service_name] = 1 if not pids else 0
                    if not pids:
                        self.process_started_absent.add(service_name)
                        # Absence at the first sample used to become the
                        # baseline, so a sender that was already dead when the
                        # soak started was recorded as its own normal and could
                        # never be reported. A sender that dies *during* the run
                        # is caught by the comparison below; one that was never
                        # there was the blind spot.
                        #
                        # A warning rather than a violation, because a
                        # deliberately retired sender should not fail a run --
                        # it should just be visible in the evidence.
                        self.event(
                            "warn",
                            f"process-absent:{service}",
                            f"{service} was not running when the soak started",
                            sample,
                        )
                    continue

                previous = self.baseline_processes[service_name]
                if pids:
                    absence_streak = self.process_absence_streaks.get(service_name, 0)
                    if not previous and service_name in self.process_started_absent:
                        # A service that was absent at the first sample is not
                        # allowed to fail a run merely because it stayed
                        # retired. If it later appears, it is now a real
                        # participant and a later disappearance is meaningful.
                        self.process_started_absent.discard(service_name)
                        if absence_streak:
                            self.process_restart_counts[service_name] = (
                                self.process_restart_counts.get(service_name, 0) + 1
                            )
                            self.event(
                                "warn",
                                f"process-restart:{service_name}",
                                f"{service_name} returned after {absence_streak} missing detailed samples",
                                sample,
                            )
                    elif previous and pids != previous:
                        self.process_restart_counts[service_name] = (
                            self.process_restart_counts.get(service_name, 0) + 1
                        )
                        self.event(
                            "warn",
                            f"process-restart:{service_name}",
                            f"{service_name} process set changed from {previous} to {pids}",
                            sample,
                        )
                    self.process_absence_streaks[service_name] = 0
                    self.baseline_processes[service_name] = pids
                else:
                    absence_streak = self.process_absence_streaks.get(service_name, 0) + 1
                    self.process_absence_streaks[service_name] = absence_streak
                    # A sender that was already absent when the run started
                    # is a warning-only condition until it proves it can run.
                    # Once it has been seen alive, persistent absence is a
                    # release failure. Three detailed samples is fifteen
                    # minutes with the normal five-minute detail cadence and
                    # avoids failing on a short Task Scheduler restart.
                    if (
                        service_name not in self.process_started_absent
                        and absence_streak >= PERSISTENT_FAILURE_SAMPLES
                    ):
                        self.event(
                            "fail",
                            f"process-change:{service_name}",
                            f"{service_name} was not running for {absence_streak} consecutive detailed samples",
                            sample,
                        )

        return self.violations[before_fail:] + self.warnings[before_warn:]

    def summary(self, *, started_at: datetime, ended_at: datetime, completed: bool) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "status": "pass" if completed and not self.violations and self.endpoint_error_count == 0 else ("fail" if self.violations else "incomplete"),
            "completed": completed,
            "startedAt": started_at.isoformat(),
            "endedAt": ended_at.isoformat(),
            "durationSeconds": round((ended_at - started_at).total_seconds()),
            "sampleCount": self.sample_count,
            "detailedSampleCount": self.detailed_count,
            "panelUid": self.expected_uid,
            "rebootCount": self.reboot_count,
            "endpointErrorCount": self.endpoint_error_count,
            "minimumFreeHeapBytes": self.min_free_heap,
            "minimumLargestFreeBlockBytes": self.min_largest_block,
            "maximumEndpointLatencyMs": self.max_latency_ms,
            "processRestartCounts": dict(self.process_restart_counts),
            "violations": self.violations,
            "warnings": self.warnings,
        }


def _new_run_directory(root: Path, now: datetime) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    destination = root / f"soak-{now.strftime('%Y%m%d-%H%M%S')}"
    if destination.exists():
        raise FileExistsError(f"soak directory already exists: {destination}")
    destination.mkdir()
    return destination


def run_monitor(
    *,
    base_url: str,
    output_root: Path,
    duration: timedelta,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    once: bool = False,
) -> Path:
    root = base_url.rstrip("/")
    if not root.startswith(("http://", "https://")):
        raise ValueError("base_url must be HTTP(S)")
    if duration.total_seconds() <= 0:
        raise ValueError("duration must be positive")

    started = datetime.now().astimezone()
    destination = _new_run_directory(output_root, started)
    samples_path = destination / "samples.jsonl"
    events_path = destination / "events.jsonl"
    summary_path = destination / "summary.json"
    run_path = destination / "run.json"
    end_wall = started + duration
    state = SoakState()
    session = requests.Session()
    stop_requested = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    for signal_name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, signal_name, None)
        if signum is not None:
            signal.signal(signum, stop)

    _atomic_json(
        run_path,
        {
            "schemaVersion": 1,
            "status": "running",
            "heartbeatSeconds": DETAIL_SAMPLE_SECONDS,
            "pid": os.getpid(),
            "startedAt": started.isoformat(),
            "plannedEndAt": end_wall.isoformat(),
            "panel": root,
            "samples": str(samples_path),
            "summary": str(summary_path),
        },
    )

    next_detail = 0.0
    while not stop_requested:
        cycle_started = time.monotonic()
        local_now = datetime.now().astimezone()
        detailed = cycle_started >= next_detail
        sample = collect_sample(
            session,
            root,
            detailed=detailed,
            timeout=timeout,
            local_now=local_now,
        )
        _append_jsonl(samples_path, sample)
        for event in state.ingest(sample):
            _append_jsonl(events_path, event)
        if detailed:
            next_detail = cycle_started + DETAIL_SAMPLE_SECONDS
            _atomic_json(
                run_path,
                {
                    "schemaVersion": 1,
                    "status": "running",
                    "heartbeatSeconds": DETAIL_SAMPLE_SECONDS,
                    "pid": os.getpid(),
                    "startedAt": started.isoformat(),
                    "plannedEndAt": end_wall.isoformat(),
                    "lastSampleAt": sample["localTime"],
                    "sampleCount": state.sample_count,
                    "violationCount": len(state.violations),
                    "warningCount": len(state.warnings),
                    "panel": root,
                    "samples": str(samples_path),
                    "summary": str(summary_path),
                },
            )

        if once or local_now >= end_wall:
            break
        delay = max(0.0, sample_interval(local_now) - (time.monotonic() - cycle_started))
        # Short waits make SIGTERM/CTRL+C responsive without busy polling.
        deadline = time.monotonic() + delay
        while not stop_requested and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))

    ended = datetime.now().astimezone()
    completed = once or ended >= end_wall
    summary = state.summary(started_at=started, ended_at=ended, completed=completed)
    _atomic_json(summary_path, summary)
    _atomic_json(
        run_path,
        {
            "schemaVersion": 1,
            "status": summary["status"],
            "pid": os.getpid(),
            "startedAt": started.isoformat(),
            "endedAt": ended.isoformat(),
            "sampleCount": state.sample_count,
            "violationCount": len(state.violations),
            "warningCount": len(state.warnings),
            "panel": root,
            "samples": str(samples_path),
            "summary": str(summary_path),
        },
    )
    return destination


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--awtrix", default=AWTRIX)
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--duration-hours", type=float, default=24.0)
    parser.add_argument("--timeout", type=float, default=REQUEST_TIMEOUT_SECONDS)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    destination = run_monitor(
        base_url=args.awtrix,
        output_root=args.output_root or default_output_root(),
        duration=timedelta(hours=args.duration_hours),
        timeout=args.timeout,
        once=args.once,
    )
    print(destination)


if __name__ == "__main__":
    main()
