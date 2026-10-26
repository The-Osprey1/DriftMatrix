"""Offline, immutable canonical configuration. Importing this starts no services."""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from importlib.resources import files
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jsonschema import Draft7Validator

SERVICE_NAMES = ("hub", "media", "pc_stats", "weather", "aqi", "drift_link")


class ConfigError(ValueError):
    """A validation error whose message contains no configuration values."""


def default_data_dir(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    local = env.get("LOCALAPPDATA")
    return (Path(local) if local else Path.home() / "AppData" / "Local") / "AWTRIX"


def default_config_path(environ: Mapping[str, str] | None = None) -> Path:
    return default_data_dir(environ) / "driftmatrix.json"


def schema_document() -> dict[str, Any]:
    return json.loads(files("driftmatrix_config").joinpath("driftmatrix.schema.json").read_text(encoding="utf-8"))


def example_document() -> dict[str, Any]:
    return json.loads(files("driftmatrix_config").joinpath("driftmatrix.example.json").read_text(encoding="utf-8"))


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("JSON contains a duplicate object key")
        result[key] = value
    return result


def _constant(_value):
    raise ConfigError("JSON contains a non-finite number")


def read_json(path: Path | str, *, label: str = "configuration") -> Any:
    """Strict JSON with BOM support; never echo a rejected value or document."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"),
                          object_pairs_hook=_object, parse_constant=_constant)
    except ConfigError:
        raise
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise ConfigError(f"Cannot read valid {label} JSON") from None


def safe_path(parts) -> str:
    result = []
    previous = None
    for part in parts:
        result.append("*" if previous in {"aliases", "macro_mapping"} else str(part))
        previous = part
    return ".".join(result) or "configuration"


def check_schema(value: Any, schema: dict[str, Any] | None = None, *, prefix: str = "") -> None:
    spec = schema_document() if schema is None else schema
    error = next(Draft7Validator(spec).iter_errors(value), None)
    if error is not None:
        location = safe_path(error.absolute_path)
        raise ConfigError(f"Invalid {prefix + location} ({error.validator})")


def normalize_panel_url(value: str) -> str:
    try:
        url = urlsplit(value)
        port = url.port
        if (url.scheme.lower() not in {"http", "https"} or not url.hostname or port == 0
                or url.username is not None or url.password is not None
                or url.query or url.fragment or "?" in value or "#" in value
                or "\\" in value or any(char.isspace() or ord(char) < 32 for char in value)):
            raise ValueError()
        host = url.hostname.lower()
        if ":" in host:
            host = "[" + host + "]"
        if port is not None and not ((url.scheme.lower() == "http" and port == 80)
                                     or (url.scheme.lower() == "https" and port == 443)):
            host += f":{port}"
        return urlunsplit((url.scheme.lower(), host, url.path.rstrip("/"), "", ""))
    except (ValueError, TypeError, AttributeError):
        raise ConfigError("Invalid panel.url (HTTP endpoint without credentials, query, or fragment required)") from None


def resolve_local_path(value: str, directory: Path) -> str:
    if any(ord(char) < 32 for char in value):
        raise ConfigError("Invalid local path")
    # Preserve an explicitly absolute Windows path when inspecting a file on Linux.
    if PureWindowsPath(value).is_absolute() and os.name != "nt":
        return str(PureWindowsPath(value))
    try:
        path = Path(value).expanduser()
        return str((path if path.is_absolute() else directory / path).resolve())
    except (OSError, RuntimeError, ValueError):
        raise ConfigError("Invalid local path") from None


def _json_copy(value: Any) -> Any:
    def check(item):
        if isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                raise ConfigError("Configuration object keys must be strings")
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
        elif isinstance(item, float) and not math.isfinite(item):
            raise ConfigError("Configuration contains a non-finite number")
        elif not isinstance(item, (str, int, float, bool, type(None))):
            raise ConfigError("Configuration contains a non-JSON value")
    try:
        check(value)
        return json.loads(json.dumps(value, allow_nan=False))
    except (RecursionError, OverflowError, UnicodeError):
        raise ConfigError("Invalid configuration document") from None


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(child) for child in value)
    return value


def _thaw(value):
    if isinstance(value, Mapping):
        return {key: _thaw(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw(child) for child in value]
    return value


def validate_hotkeys(options: Mapping[str, Any]) -> None:
    """Check bindings without importing ctypes or the live ecosystem coordinator."""
    seen = set()
    for macro in options.get("macro_mapping", {}).values():
        if not macro.get("enabled", True):
            continue
        for key in ("hotkey_primary", "hotkey_secondary"):
            if key not in macro:
                continue
            parts = macro[key].upper().split("+")
            mods = ["CTRL" if part == "CONTROL" else part for part in parts[:-1]]
            named = {"SPACE", "ENTER", "ESC", "ESCAPE", "TAB", "HOME", "END", "LEFT", "RIGHT",
                     "UP", "DOWN", "PAGEUP", "PAGEDOWN", "BACKSPACE", "DELETE", "INSERT"}
            last = parts[-1]
            if (any(part not in {"CTRL", "ALT", "SHIFT", "WIN"} for part in mods)
                    or len(set(mods)) != len(mods)
                    or not (re.fullmatch(r"[A-Z0-9]|F([1-9]|1[0-9]|2[0-4])", last) or last in named)):
                raise ConfigError("Invalid services.drift_link.options.macro_mapping.* (hotkey)")
            identity = tuple(sorted(mods)), "ESC" if last == "ESCAPE" else last
            if identity in seen:
                raise ConfigError("Invalid services.drift_link.options.macro_mapping (duplicate hotkey)")
            seen.add(identity)


def _semantics(document):
    try:
        ZoneInfo(document["panel"]["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError("Invalid panel.timezone (unknown IANA timezone)") from None
    hub = document["services"]["hub"]["options"]
    network = hub.get("network_sentinel", {})
    if network.get("alert_lifetime_seconds", 45) < 2 * network.get("alert_refresh_seconds", 15):
        raise ConfigError("Invalid services.hub.options.network_sentinel (lifetime must cover two refresh intervals)")
    focus = hub.get("focus_sprint", {})
    if focus.get("lifetime_seconds", 20) <= max(focus.get("refresh_seconds", 5), focus.get("static_refresh_seconds", 15)):
        raise ConfigError("Invalid services.hub.options.focus_sprint (lifetime must exceed refresh intervals)")
    maintenance = hub.get("maintenance", {})
    if maintenance.get("window_start", "21:35") == maintenance.get("window_end", "23:00"):
        raise ConfigError("Invalid services.hub.options.maintenance (empty window)")
    validate_hotkeys(document["services"]["drift_link"]["options"])


@dataclass(frozen=True)
class PanelConfig:
    url: str = field(repr=False)
    timezone: str
    quiet_hours: Mapping[str, str]


@dataclass(frozen=True)
class RestartPolicy:
    max_attempts: int
    window_seconds: float


@dataclass(frozen=True)
class RuntimeConfig:
    data_dir: Path = field(repr=False)
    shutdown_timeout_seconds: float
    restart: RestartPolicy
    logging: Mapping[str, Any]


@dataclass(frozen=True)
class ServiceConfig:
    enabled: bool
    options: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True)
class CanonicalConfig:
    schema_version: int
    panel: PanelConfig
    runtime: RuntimeConfig
    services: Mapping[str, ServiceConfig]
    config_hash: str
    source_path: Path = field(repr=False)
    _document: Mapping[str, Any] = field(repr=False)

    def to_mapping(self) -> dict[str, Any]:
        """A detached snapshot, with local paths resolved and no service side effects."""
        return _thaw(self._document)


def config_from_mapping(value: Any, *, source_path: Path | str,
                        environ: Mapping[str, str] | None = None) -> CanonicalConfig:
    document = _json_copy(value)
    check_schema(document)
    try:
        source = Path(source_path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        raise ConfigError("Invalid configuration source path") from None
    directory = source.parent
    document["panel"]["url"] = normalize_panel_url(document["panel"]["url"])
    runtime = document["runtime"]
    runtime["data_dir"] = (str(default_data_dir(environ).resolve()) if runtime["data_dir"] is None
                           else resolve_local_path(runtime["data_dir"], directory))
    runtime.setdefault("logging", {})
    runtime["logging"] = {"level": "INFO", "max_bytes": 524288, "backup_count": 2, **runtime["logging"]}
    hub = document["services"]["hub"]["options"]
    next_up = hub.get("next_up", {})
    if "sources" in next_up:
        sources = []
        for item in next_up["sources"]:
            item = item.strip()
            if not item:
                raise ConfigError("Invalid services.hub.options.next_up.sources (empty source)")
            if item.lower().startswith("webcal://"):
                item = "https://" + item[len("webcal://"):]
            if "://" in item:
                try:
                    url = urlsplit(item)
                    valid = url.scheme in {"http", "https"} and bool(url.hostname) and url.port != 0
                except ValueError:
                    valid = False
                if not valid:
                    raise ConfigError("Invalid services.hub.options.next_up.sources (unsupported URL scheme)")
                sources.append(item)
            else:
                sources.append(resolve_local_path(item, directory))
        next_up["sources"] = sources
    media = document["services"]["media"]["options"]
    if "script_path" in media:
        media["script_path"] = resolve_local_path(media["script_path"], directory)
    _semantics(document)
    hashed = {key: child for key, child in document.items() if key != "$schema"}
    digest = sha256(json.dumps(hashed, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
    frozen = _freeze(document)
    return CanonicalConfig(
        1, PanelConfig(document["panel"]["url"], document["panel"]["timezone"], frozen["panel"]["quiet_hours"]),
        RuntimeConfig(Path(runtime["data_dir"]), runtime["shutdown_timeout_seconds"],
                      RestartPolicy(**runtime["restart"]), frozen["runtime"]["logging"]),
        MappingProxyType({name: ServiceConfig(item["enabled"], frozen["services"][name]["options"])
                          for name, item in document["services"].items()}),
        digest, source, frozen,
    )


def load_config(path: Path | str | None = None, *, environ: Mapping[str, str] | None = None) -> CanonicalConfig:
    selected = default_config_path(environ) if path is None else Path(path)
    return config_from_mapping(read_json(selected), source_path=selected, environ=environ)
