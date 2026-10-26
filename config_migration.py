"""Offline migration with redacted provenance and exclusive candidate-file writes."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
import os
from pathlib import Path, PureWindowsPath
import re
import tempfile
from typing import Any, Mapping

from config_loader import (
    ConfigError, config_from_mapping, example_document, normalize_panel_url,
    read_json, resolve_local_path, safe_path, schema_document,
)


@dataclass(frozen=True)
class MigrationResult:
    candidate: dict[str, Any] | None = field(repr=False)
    report: dict[str, Any]


def _leaves(value, path=()):
    if isinstance(value, dict) and value:
        for key, child in value.items():
            yield from _leaves(child, path + (key,))
    elif isinstance(value, list) and value:
        for index, child in enumerate(value):
            yield from _leaves(child, path + (index,))
    else:
        yield path


def _quiet(value):
    if not isinstance(value, dict) or set(value) - {"start", "end"}:
        raise ConfigError("Invalid quiet hours")
    result = {}
    for key, fallback in (("start", "21:30"), ("end", "07:00")):
        item = value.get(key, fallback)
        match = re.fullmatch(r"(\d{1,2}):(\d{1,2})", item) if isinstance(item, str) else None
        if not match or not 0 <= int(match[1]) <= 23 or not 0 <= int(match[2]) <= 59:
            raise ConfigError("Invalid quiet hours")
        result[key] = f"{int(match[1]):02}:{int(match[2]):02}"
    return result


def migrate(*, hub_path: Path | None = None, ecosystem_path: Path | None = None,
            senders_path: Path | None = None, output_path: Path,
            legacy_working_dir: Path | None = None,
            environ: Mapping[str, str] | None = None) -> MigrationResult:
    """Read explicitly selected files. Never enumerate tasks, import workers, or write."""
    if not any((hub_path, ecosystem_path, senders_path)):
        raise ConfigError("Select at least one legacy input")
    candidate = example_document()
    candidate.pop("$schema", None)  # Validation uses the packaged schema, not an external URL.
    conflicts = []
    warnings = []
    origins = {}
    shared = {"url": [], "timezone": [], "quiet_hours": []}

    def origin(source, path):
        return {"source": source, "field": safe_path(path) if path else "configuration"}

    def problem(code, field_path, sources, message):
        conflicts.append({"code": code, "field": safe_path(field_path),
                          "sources": sources, "message": message})

    def unknown(value, allowed, source, path=()):
        if not isinstance(value, dict):
            problem("invalid_structure", path, [origin(source, path)], "Expected an object")
            return True
        if set(value) - set(allowed):
            problem("unknown_setting", path, [origin(source, path)], "Unrecognized legacy settings require review")
        return False

    def install(path, value, source, source_path=()):
        parent = candidate
        for key in path[:-1]:
            parent = parent[key]
        parent[path[-1]] = deepcopy(value)
        origins[path] = [(source, source_path)]

    if hub_path is not None:
        hub = read_json(hub_path, label="Hub input")
        if isinstance(hub, dict) and "_readme" in hub:
            notes = hub.pop("_readme")
            if not (isinstance(notes, str) or isinstance(notes, list) and all(isinstance(line, str) for line in notes)):
                problem("invalid_reserved_metadata", ("_readme",), [origin("hub", ("_readme",))],
                        "Hub documentation metadata must be text or a list of text lines")
            else:
                warnings.append({"code": "reserved_metadata_omitted", "source": "hub", "field": "_readme",
                                 "message": "Hub documentation notes are omitted from runtime configuration"})
        allowed = schema_document()["definitions"]["hubOptions"]["properties"]
        if not unknown(hub, {*allowed, "panel_url", "quiet_hours"}, "hub"):
            if "panel_url" in hub:
                shared["url"].append((hub["panel_url"], "hub", ("panel_url",)))
            if "quiet_hours" in hub:
                shared["quiet_hours"].append((hub["quiet_hours"], "hub", ("quiet_hours",)))
            options = {key: deepcopy(value) for key, value in hub.items() if key in allowed}
            upcoming = options.get("next_up")
            if isinstance(upcoming, dict):
                if "timezone" in upcoming:
                    shared["timezone"].append((upcoming.pop("timezone"), "hub", ("next_up", "timezone")))
                sources = upcoming.get("sources")
                if isinstance(sources, list):
                    resolved = []
                    for index, item in enumerate(sources):
                        if isinstance(item, str) and "://" not in item and not (
                                Path(item).is_absolute() or PureWindowsPath(item).is_absolute()):
                            if legacy_working_dir is None:
                                problem("ambiguous_relative_path", ("services", "hub", "options", "next_up", "sources", index),
                                        [origin("hub", ("next_up", "sources", index))],
                                        "Supply --legacy-working-dir for relative legacy calendar paths")
                            else:
                                item = resolve_local_path(item, Path(legacy_working_dir).resolve())
                        resolved.append(item)
                    upcoming["sources"] = resolved
            focus = options.get("focus_sprint")
            if isinstance(focus, dict):
                for key in ("preset", "motion"):
                    if isinstance(focus.get(key), str):
                        focus[key] = focus[key].casefold()
            install(("services", "hub", "options"), options, "hub")
            warnings.append({"code": "launch_state_unverified", "source": "hub",
                             "message": "Hub options imported; its worker remains disabled until startup ownership is reviewed"})

    if ecosystem_path is not None:
        eco = read_json(ecosystem_path, label="ecosystem input")
        if not unknown(eco, {"$schema", "version", "ecosystem"}, "ecosystem"):
            if eco.get("version") != "1.0.0":
                problem("unsupported_version", ("version",), [origin("ecosystem", ("version",))], "Unsupported ecosystem version")
            body = eco.get("ecosystem")
            if not unknown(body, {"name", "devices"}, "ecosystem", ("ecosystem",)):
                devices = body.get("devices")
                if not unknown(devices, {"matrix", "pad", "watch"}, "ecosystem", ("ecosystem", "devices")):
                    link = candidate["services"]["drift_link"]["options"]
                    link["devices"] = {}
                    for name in ("matrix", "pad", "watch"):
                        source_path = ("ecosystem", "devices", name)
                        device = devices.get(name)
                        allowed = ({"enabled", "url", "rotation_apps", "poll_interval_s"} if name == "matrix" else
                                   {"enabled", "vid", "macro_mapping", "fallback_port"} if name == "pad" else
                                   {"enabled", "vid", "fallback_port", "udp_discovery_port", "haptic_alerts"})
                        if unknown(device, allowed, "ecosystem", source_path):
                            continue
                        if "enabled" not in device:
                            problem("missing_setting", source_path, [origin("ecosystem", source_path)], "Explicit device enable flag required")
                            continue
                        if name == "matrix":
                            if "url" in device:
                                shared["url"].append((device["url"], "ecosystem", source_path + ("url",)))
                            for old, new in (("enabled", "matrix_enabled"), ("rotation_apps", "rotation_apps"), ("poll_interval_s", "poll_interval_s")):
                                if old in device:
                                    install(("services", "drift_link", "options", new), device[old], "ecosystem", source_path + (old,))
                        else:
                            install(("services", "drift_link", "options", "devices", name),
                                    {key: value for key, value in device.items() if key in {"enabled", "vid"}},
                                    "ecosystem", source_path)
                            if name == "pad" and "macro_mapping" in device:
                                install(("services", "drift_link", "options", "macro_mapping"), device["macro_mapping"],
                                        "ecosystem", source_path + ("macro_mapping",))
                            for key in sorted({"fallback_port", "udp_discovery_port", "haptic_alerts"} & set(device)):
                                warnings.append({"code": "reserved_metadata_omitted", "source": "ecosystem",
                                                 "field": safe_path(source_path + (key,)),
                                                 "message": "Reserved legacy metadata has no implemented runtime behavior"})
                    warnings.append({"code": "launch_state_unverified", "source": "ecosystem",
                                     "message": "Device options imported; the Drift-Link worker remains disabled until startup ownership is reviewed"})

    if senders_path is not None:
        senders = read_json(senders_path, label="sender inventory")
        names = ("media", "pc_stats", "weather", "aqi")
        if not unknown(senders, names, "senders"):
            for name, sender in senders.items():
                if name not in names or unknown(sender, {"enabled", "panel_url", "quiet_hours", "timezone", "options"}, "senders", (name,)):
                    continue
                for key, shared_name in (("panel_url", "url"), ("quiet_hours", "quiet_hours"), ("timezone", "timezone")):
                    if key in sender:
                        shared[shared_name].append((sender[key], "senders", (name, key)))
                for key in ("enabled", "options"):
                    if key in sender:
                        options = deepcopy(sender[key])
                        if key == "options" and name == "media" and isinstance(options, dict) and "script_path" in options:
                            if isinstance(options["script_path"], str):
                                options["script_path"] = resolve_local_path(options["script_path"], Path(senders_path).resolve().parent)
                        install(("services", name, key), options, "senders", (name, key))

    for key, values in shared.items():
        normalized = []
        for value, source, source_path in values:
            try:
                if key == "url":
                    value = normalize_panel_url(value)
                elif key == "quiet_hours":
                    value = _quiet(value)
                elif not isinstance(value, str) or not value:
                    raise ConfigError("Invalid timezone")
                normalized.append((value, source, source_path))
            except ConfigError:
                problem("invalid_shared_setting", ("panel", key), [origin(source, source_path)], "Invalid shared setting")
        if normalized:
            origins[("panel", key)] = [(source, path) for _, source, path in normalized]
            if any(value != normalized[0][0] for value, _, _ in normalized[1:]):
                problem("conflicting_shared_setting", ("panel", key),
                        [origin(source, path) for _, source, path in normalized], "Shared settings disagree; no candidate will be written")
            candidate["panel"][key] = normalized[0][0]
            if key == "quiet_hours":
                for clock in ("start", "end"):
                    explicit = [(source, path + (clock,)) for raw, source, path in values
                                if isinstance(raw, dict) and clock in raw]
                    origins[("panel", key, clock)] = explicit or [("default", ("panel", key, clock))]
        elif key == "url":
            problem("missing_panel_url", ("panel", key), [], "An explicit legacy panel URL is required")

    if not conflicts:
        try:
            candidate = config_from_mapping(candidate, source_path=output_path, environ=environ).to_mapping()
        except ConfigError as error:
            problem("invalid_candidate", (), [], str(error))

    provenance = []
    if not conflicts:
        for path in _leaves(candidate):
            matches = [prefix for prefix in origins if path[:len(prefix)] == prefix]
            prefix = max(matches, key=len) if matches else None
            sources = ([origin(source, source_path + path[len(prefix):]) for source, source_path in origins[prefix]]
                       if prefix is not None else [{"source": "default", "field": safe_path(path)}])
            provenance.append({"field": safe_path(path), "sources": sources})
    report = {"report_version": 1, "status": "conflicts" if conflicts else "ready",
              "candidate_written": False, "conflicts": conflicts, "warnings": warnings,
              "provenance": provenance}
    return MigrationResult(None if conflicts else candidate, report)


def check_new_destination(path: Path, inputs=()) -> None:
    try:
        target = path.expanduser().resolve()
        aliases = any(target == Path(item).expanduser().resolve() for item in inputs if item is not None)
    except (OSError, RuntimeError, ValueError):
        raise ConfigError("Invalid destination path") from None
    if aliases:
        raise ConfigError("Destination aliases an input file")
    if path.exists() or path.is_symlink():
        raise ConfigError("Destination already exists; no overwrite is permitted")
    if not path.parent.is_dir():
        raise ConfigError("Destination parent directory must already exist")


def write_new_json(path: Path, value: Any, *, inputs=()) -> None:
    """Publish a fully written file atomically with an exclusive hard link.

    Unlike os.replace, the link cannot overwrite a destination created by another
    process between preflight and publication. The temporary file is private.
    """
    path = path.expanduser().absolute()
    check_new_destination(path, inputs)
    temporary = None
    try:
        payload = (json.dumps(value, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
        descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".driftmatrix-", suffix=".tmp")
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except (OSError, ValueError, TypeError):
        raise ConfigError("Cannot publish new JSON file (destination may exist or filesystem may not support atomic creation)") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
