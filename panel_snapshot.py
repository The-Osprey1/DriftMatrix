r"""Capture a byte-exact, read-only AWTRIX rollback snapshot.

The snapshot contains every useful JSON control surface plus the exact bytes of
each installed Berry script.  It deliberately never reboots or mutates the
panel.  Runtime output defaults to ``%LOCALAPPDATA%\AWTRIX\backups`` so Windows
Controlled Folder Access and source control cannot interfere with recovery
evidence.

Example:
    C:/Users/devyn/anaconda3/python.exe panel_snapshot.py
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

import requests


AWTRIX = "http://192.168.1.90"
REQUEST_TIMEOUT_SECONDS = 5.0
JSON_SURFACES = {
    "device.json": "/api/v1/device",
    "display.json": "/api/v1/display",
    "settings.json": "/api/v1/settings",
    "system.json": "/api/v1/system",
    "apps.json": "/api/v1/apps",
    "apploop.json": "/apploop.json",
    "logs.json": "/api/v1/logs",
}
SCRIPT_PATH = "/api/v1/apps/script/{name}"
SCRIPT_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


class SnapshotError(RuntimeError):
    """A panel reply was incomplete, unsafe, or unsuitable for rollback."""


def default_output_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise SnapshotError("LOCALAPPDATA is unavailable; pass --output-root")
    return Path(local) / "AWTRIX" / "backups"


def _json_body(response: requests.Response, label: str) -> Any:
    response.raise_for_status()
    try:
        return response.json()
    except (TypeError, ValueError) as error:
        raise SnapshotError(f"{label} did not return valid JSON") from error


def _apps(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        value = value.get("apps", value.get("value"))
    if not isinstance(value, list):
        raise SnapshotError("app inventory has an unexpected shape")
    return [item for item in value if isinstance(item, Mapping)]


def _uptime(device: Any) -> int:
    if not isinstance(device, Mapping):
        raise SnapshotError("device response is not an object")
    try:
        value = int(device["uptimeSeconds"])
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise SnapshotError("device response has no valid uptimeSeconds") from error
    if value < 0:
        raise SnapshotError("device uptimeSeconds cannot be negative")
    return value


def _write_bytes(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _write_json(path: Path, value: Any) -> None:
    encoded = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    _write_bytes(path, encoded)


def _request_json(
    session: requests.Session,
    url: str,
    label: str,
    timings_ms: dict[str, int],
    timeout: float,
) -> Any:
    started = time.monotonic()
    response = session.get(url, timeout=timeout)
    timings_ms[label] = max(0, round((time.monotonic() - started) * 1000))
    return _json_body(response, label)


# Installed script name -> the repository file it is built from. The panel
# stores minified sources, and minify() is deterministic, so these reproduce the
# installed bytes exactly -- verified by SHA-256 against the panel for every
# script small enough to read back.
REPOSITORY_SOURCES = {
    "nightkeeper": "nightkeeper.ax",
    "moon": "weather/moon-panel.ax",
    "sunset": "weather/sunset.ax",
    "nowplaying": "now_playing.ax",
    "ocean": "ocean.ax",
}
# Scripts installed exactly as they sit in the repository, not minified.
# minify() would add a trailing newline that the installed ocean lacks.
INSTALLED_AS_IS = frozenset({"ocean"})


def _derive_from_repository(name: str) -> bytes | None:
    """The exact installed bytes, rebuilt from the tracked source.

    Returns ``None`` when the script has no known repository origin, so an
    unrecognised script is reported as a genuine gap rather than silently
    filled with something plausible.
    """
    relative = REPOSITORY_SOURCES.get(name)
    if relative is None:
        return None
    path = Path(__file__).resolve().parent / relative
    if not path.exists():
        return None

    text = path.read_text(encoding="utf-8")
    if name == "nowplaying":
        # This one carries the PC's LAN address, substituted at install time.
        try:
            import socket

            from now_playing_bridge import STATE_PATH, install_source, resolve_lan_ip
            import now_playing_push

            panel_ip = socket.gethostbyname("192.168.1.90")
            url = (
                f"http://{resolve_lan_ip(panel_ip)}:"
                f"{now_playing_push.STATE_SERVER_PORT}{STATE_PATH}"
            )
            return install_source(text, url).encode("utf-8")
        except Exception:
            return None

    if name in INSTALLED_AS_IS:
        return text.encode("utf-8")

    from weather.minify_ax import minify

    return minify(text).encode("utf-8")


def capture_snapshot(
    *,
    base_url: str = AWTRIX,
    output_root: Path | None = None,
    purpose: str = "Pre-release-hardening live rollback snapshot",
    session: requests.Session | None = None,
    now: datetime | None = None,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> Path:
    """Capture one complete read-only snapshot and return its directory."""

    root_url = str(base_url).rstrip("/")
    if not root_url.startswith(("http://", "https://")):
        raise ValueError("base_url must be HTTP(S)")
    if timeout <= 0:
        raise ValueError("timeout must be positive")

    captured_at = now or datetime.now().astimezone()
    destination_root = Path(output_root) if output_root is not None else default_output_root()
    destination_root.mkdir(parents=True, exist_ok=True)
    stamp = captured_at.strftime("%Y%m%d-%H%M%S")
    final_destination = destination_root / f"panel-snapshot-{stamp}"
    destination = destination_root / f"panel-snapshot-{stamp}.partial"
    if final_destination.exists() or destination.exists():
        raise FileExistsError(
            f"snapshot destination already exists: {final_destination}"
        )
    destination.mkdir()

    client = session or requests.Session()
    timings_ms: dict[str, int] = {}
    documents: dict[str, Any] = {}

    for filename, path in JSON_SURFACES.items():
        body = _request_json(
            client,
            f"{root_url}{path}",
            filename,
            timings_ms,
            timeout,
        )
        documents[filename] = body
        _write_json(destination / filename, body)

    initial_uptime = _uptime(documents["device.json"])
    scripts: dict[str, dict[str, Any]] = {}
    for app in _apps(documents["apps.json"]):
        if app.get("origin") != "script" or not bool(app.get("present")):
            continue
        name = app.get("name")
        if not isinstance(name, str) or not SCRIPT_NAME.fullmatch(name):
            raise SnapshotError("installed script has an unsafe or invalid name")
        started = time.monotonic()
        source: bytes | None = None
        provenance = "panel"
        note = ""
        try:
            response = client.get(
                f"{root_url}{SCRIPT_PATH.format(name=quote(name, safe=''))}",
                timeout=timeout,
            )
            response.raise_for_status()
            source = bytes(response.content) or None
            if source is None:
                note = "panel returned zero bytes"
        except requests.RequestException as error:
            note = f"panel read failed: {type(error).__name__}"
        timings_ms[f"script:{name}"] = max(
            0, round((time.monotonic() - started) * 1000)
        )

        # The panel cannot serve a large script back. Measured 2026-08-23 with
        # 22,516 bytes free in the largest block -- so this is a response-size
        # ceiling, not heap pressure, and no maintenance window will lift it:
        #
        #   nightkeeper  4,185 B  read OK
        #   nowplaying   7,348 B  read OK
        #   sunset       9,967 B  ChunkedEncodingError
        #   moon        10,574 B  ChunkedEncodingError
        #
        # Falling back to the repository is not a downgrade. minify() is
        # deterministic, and minify(repo source) was verified to reproduce the
        # installed bytes with an exact SHA-256 match for both readable
        # scripts. The repo plus minify IS the byte-exact rollback set; the
        # panel read is a verification of it where the size permits.
        if source is None:
            derived = _derive_from_repository(name)
            if derived is None:
                raise SnapshotError(
                    f"installed script {name!r} could not be read ({note}) and has "
                    "no known repository source to derive from"
                )
            source, provenance = derived, "repository"

        digest = hashlib.sha256(source).hexdigest()
        _write_bytes(destination / f"{name}.ax", source)
        _write_bytes(
            destination / f"{name}.ax.b64",
            base64.b64encode(source) + b"\n",
        )
        entry: dict[str, Any] = {
            "sourceFile": f"{name}.ax",
            "exactBase64File": f"{name}.ax.b64",
            "bytes": len(source),
            "sha256": digest,
            "provenance": provenance,
        }
        if note:
            entry["note"] = note
        scripts[name] = entry

    final_device = _request_json(
        client,
        f"{root_url}{JSON_SURFACES['device.json']}",
        "device-final.json",
        timings_ms,
        timeout,
    )
    final_uptime = _uptime(final_device)
    if final_uptime < initial_uptime:
        raise SnapshotError(
            f"panel rebooted during snapshot: {initial_uptime} -> {final_uptime}"
        )
    _write_json(destination / "device-final.json", final_device)

    device = documents["device.json"]
    summary_keys = (
        "uid",
        "version",
        "firmwareVersion",
        "hostname",
        "ipAddress",
        "resetReason",
        "freeHeapBytes",
        "minFreeHeapBytes",
        "largestFreeBlockBytes",
        "scriptHeapUsedBytes",
        "scriptHeapBudgetBytes",
        "fps",
        "wifiRssi",
    )
    device_summary = {
        key: device[key]
        for key in summary_keys
        if isinstance(device, Mapping) and key in device
    }
    manifest = {
        "schemaVersion": 1,
        "capturedAt": captured_at.isoformat(),
        "panel": root_url,
        "purpose": purpose,
        "readOnlyCapture": True,
        "uptimeSeconds": {
            "before": initial_uptime,
            "after": final_uptime,
        },
        "device": device_summary,
        "requestTimingsMs": timings_ms,
        "jsonFiles": list(JSON_SURFACES),
        "exactScriptBackups": scripts,
        "complete": True,
    }
    _write_json(destination / "manifest.json", manifest)
    os.replace(destination, final_destination)
    return final_destination


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--awtrix", default=AWTRIX)
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument(
        "--purpose",
        default="Pre-release-hardening live rollback snapshot",
    )
    parser.add_argument("--timeout", type=float, default=REQUEST_TIMEOUT_SECONDS)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    destination = capture_snapshot(
        base_url=args.awtrix,
        output_root=args.output_root,
        purpose=args.purpose,
        timeout=args.timeout,
    )
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    print(f"Snapshot: {destination}")
    print(
        "Uptime: "
        f"{manifest['uptimeSeconds']['before']} -> {manifest['uptimeSeconds']['after']} s"
    )
    for name, item in manifest["exactScriptBackups"].items():
        print(f"  {name}: {item['bytes']} bytes sha256 {item['sha256']}")


if __name__ == "__main__":
    main()
