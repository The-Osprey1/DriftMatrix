"""Small, read-only media-state bridge for the AWTRIX Now Playing script.

The ESP32 renders the page locally.  This module deliberately serves only a
compact state document, so animation never depends on a stream of panel PUTs.
"""

from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

# A generic Berry source tool that happens to live under weather/ because the
# weather scripts needed it first; nothing about it is weather-specific.
from weather.minify_ax import minify

import panel_gate


STATE_PATH = "/nowplaying"
SCRIPT_PLACEHOLDER = "__STATE_URL__"
MAX_TEXT_CHARS = 120
MAX_IDENTITY_CHARS = 160
# The only sender that installs through this bridge is now_playing_push.
GATE_SOURCE = "nowplaying"


@dataclass(frozen=True)
class BridgeState:
    """The complete wire state consumed by the Berry renderer."""

    playing: bool = False
    identity: str = ""
    text: str = ""
    progress: int = -1
    revision: int = 0

    def __post_init__(self) -> None:
        playing = bool(self.playing)
        identity = str(self.identity or "")[:MAX_IDENTITY_CHARS]
        text = str(self.text or "")[:MAX_TEXT_CHARS]
        try:
            progress = int(self.progress)
        except (TypeError, ValueError, OverflowError):
            progress = -1
        progress = max(-1, min(24, progress))
        try:
            revision = max(0, int(self.revision))
        except (TypeError, ValueError, OverflowError):
            revision = 0

        if not playing:
            identity = ""
            text = ""
            progress = -1

        object.__setattr__(self, "playing", playing)
        object.__setattr__(self, "identity", identity)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "progress", progress)
        object.__setattr__(self, "revision", revision)

    def to_dict(self, now: float | None = None) -> dict[str, object]:
        """Return the intentionally tiny, stable Berry wire contract."""
        del now  # Kept for a convenient clock-compatible test/caller API.
        return {
            "p": self.playing,
            "i": self.identity,
            "t": self.text,
            "g": self.progress,
            "r": self.revision,
        }


class BridgeStateStore:
    """Thread-safe latest-state store with an automatic stale fail-safe."""

    def __init__(
        self,
        initial: BridgeState | None = None,
        stale_after: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._stale_after = max(1.0, float(stale_after))
        self._lock = threading.Lock()
        self._state = initial or BridgeState()
        self._updated_at = self._clock()

    def update(self, state: BridgeState) -> None:
        if not isinstance(state, BridgeState):
            raise TypeError("state must be a BridgeState")
        with self._lock:
            self._state = state
            self._updated_at = self._clock()

    def clear(self) -> None:
        with self._lock:
            if self._state.playing:
                self._state = BridgeState(revision=self._state.revision + 1)
            self._updated_at = self._clock()

    def snapshot(self) -> BridgeState:
        with self._lock:
            now = self._clock()
            if self._state.playing and now - self._updated_at >= self._stale_after:
                self._state = BridgeState(revision=self._state.revision + 1)
                self._updated_at = now
            return self._state


class _BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(
    bind_host: str,
    port: int,
    store: BridgeStateStore,
    panel_ip: str,
) -> ThreadingHTTPServer:
    """Build the local endpoint, allowing only the configured panel IP."""

    allowed = {str(panel_ip)}

    class StateRequestHandler(BaseHTTPRequestHandler):
        server_version = "AWTRIXState/1"
        sys_version = ""

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            client_ip = str(self.client_address[0])
            if client_ip not in allowed:
                self.send_error(403)
                return
            if urlsplit(self.path).path != STATE_PATH:
                self.send_error(404)
                return

            payload = json.dumps(
                store.snapshot().to_dict(),
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("ascii")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = _BridgeHTTPServer((bind_host, int(port)), StateRequestHandler)
    setattr(server, "state_store", store)
    return server


def resolve_lan_ip(panel_host: str) -> str:
    """Return the LAN address this PC uses to reach the panel."""

    host = str(panel_host).strip()
    if not host:
        raise ValueError("panel_host is required")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((host, 80))
        address = str(sock.getsockname()[0])
    finally:
        sock.close()
    if not address or address == "0.0.0.0":
        raise RuntimeError(f"Could not resolve the LAN route to {host}")
    return address


def render_script_source(template: str, state_url: str) -> str:
    """Insert one validated HTTP state URL into the Berry source template."""

    if template.count(SCRIPT_PLACEHOLDER) != 1:
        raise ValueError(f"Berry template must contain exactly one {SCRIPT_PLACEHOLDER}")
    parsed = urlsplit(str(state_url))
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("state_url must be a plain LAN http:// URL")
    return template.replace(SCRIPT_PLACEHOLDER, str(state_url))


def install_source(template: str, state_url: str) -> str:
    """The exact text to install: the template with its state URL, minified.

    ``now_playing.ax`` is about 10.7KB and a Berry install needs roughly twice
    its source as one contiguous block -- more than this panel has even shortly
    after a reboot. On 2026-08-22 that cost the media bridge 1h51m of startup:
    it retried every 32 seconds from 11:35 until 13:26, when enough block
    happened to free up, and nothing published media state until it did.

    Stripping comments and indentation takes the source to about 7.0KB, so the
    install needs roughly 14KB instead of 21KB and fits with room to spare. The
    ``# @name``/``# @version`` header and the state URL both survive, so the
    panel still reports the app's identity.
    """
    return minify(render_script_source(template, state_url))


def ensure_script_installed(
    base_url: str,
    script_name: str,
    source: str,
    session: Any = requests,
    timeout: float = 10.0,
) -> bool:
    """Install changed Berry source and return whether a write was required."""

    if not source.strip():
        raise ValueError("Berry source must not be empty")
    if not script_name or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in script_name):
        raise ValueError("Invalid script name")

    endpoint = f"{str(base_url).rstrip('/')}/api/v1/apps/script/{script_name}"
    try:
        current = session.get(endpoint, timeout=timeout)
    except requests.RequestException as error:
        raise RuntimeError(f"Could not inspect device script: {type(error).__name__}") from error

    if current.status_code == 200 and current.text == source:
        return False
    if current.status_code not in (200, 404):
        raise RuntimeError(f"Script inspection HTTP {current.status_code}: {current.text[:200]}")

    try:
        body = source.encode("utf-8")
        with panel_gate.mutation(GATE_SOURCE, "install", script_name, body=body) as gate:
            response = session.put(
                endpoint,
                data=body,
                headers={"Content-Type": "text/plain; charset=utf-8"},
                timeout=timeout,
            )
            gate.observe(response)
    except requests.RequestException as error:
        raise RuntimeError(f"Could not install device script: {type(error).__name__}") from error

    if response.status_code != 200:
        raise RuntimeError(f"Script install HTTP {response.status_code}: {response.text[:200]}")
    try:
        result = response.json()
    except (TypeError, ValueError) as error:
        raise RuntimeError("Script install returned invalid JSON") from error
    if result.get("error") is not None:
        raise RuntimeError(f"Device rejected the Berry script: {result['error']}")
    return True


def remove_legacy_pushed_app(
    base_url: str,
    app_name: str,
    session: Any = requests,
    timeout: float = 10.0,
) -> bool:
    """Remove only a same-name pushed app before installing its script successor.

    The generic AWTRIX DELETE also removes scripts, so origin inspection is a
    mandatory guard.  Deleting a pushed app preserves its saved order slot as a
    placeholder; the subsequently installed script inherits that slot.
    """

    root = str(base_url).rstrip("/")
    try:
        response = session.get(f"{root}/api/v1/apps", timeout=timeout)
    except requests.RequestException as error:
        raise RuntimeError(f"Could not inspect app origin: {type(error).__name__}") from error
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"App inspection HTTP {response.status_code}: {response.text[:200]}")
    try:
        inventory = response.json()
    except (TypeError, ValueError) as error:
        raise RuntimeError("App inventory returned invalid JSON") from error
    if isinstance(inventory, dict):
        inventory = inventory.get("apps", inventory.get("value", []))
    if not isinstance(inventory, list):
        raise RuntimeError("App inventory has an unexpected shape")

    matches = [item for item in inventory if isinstance(item, dict) and item.get("name") == app_name]
    if len(matches) > 1:
        raise RuntimeError(f"App inventory contains duplicate {app_name!r} entries")
    if not matches:
        return False
    entry = matches[0]
    origin = entry.get("origin")
    present = bool(entry.get("present"))
    if origin == "script" or not present:
        return False
    if origin != "pushed":
        raise RuntimeError(f"Refusing to replace {app_name!r} with unexpected origin {origin!r}")

    try:
        with panel_gate.mutation(GATE_SOURCE, "delete", app_name) as gate:
            deleted = session.delete(f"{root}/api/v1/apps/{app_name}", timeout=timeout)
            gate.observe(deleted)
    except requests.RequestException as error:
        raise RuntimeError(f"Could not remove legacy pushed app: {type(error).__name__}") from error
    if not (200 <= deleted.status_code < 300 or deleted.status_code == 404):
        raise RuntimeError(f"Legacy cleanup HTTP {deleted.status_code}: {deleted.text[:200]}")
    return True
