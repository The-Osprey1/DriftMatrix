"""Canonical configuration validation, privacy, immutability, and offline behavior."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import subprocess
import sys

from jsonschema import Draft7Validator
import pytest

import config_loader as loader


@pytest.fixture
def document():
    return loader.example_document()


def parse(document, tmp_path, **kwargs):
    return loader.config_from_mapping(document, source_path=tmp_path / "driftmatrix.json", **kwargs)


def test_packaged_schema_is_valid_and_example_validates(document, tmp_path):
    Draft7Validator.check_schema(loader.schema_document())
    config = parse(document, tmp_path)
    assert config.schema_version == 1
    assert set(config.services) == set(loader.SERVICE_NAMES)
    assert not any(item.enabled for item in config.services.values())


@pytest.mark.parametrize("contents", ["{", "[]", "null", "true", '"private"',
    '{"schema_version":1,"schema_version":2}', '{"secret":NaN}',
    '{"secret":Infinity}', '{"secret":-Infinity}', '{"secret":1e999}'])
def test_malformed_json_and_wrong_root_fail_without_echoing_values(tmp_path, contents):
    path = tmp_path / "config.json"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(loader.ConfigError) as error:
        loader.load_config(path)
    assert "private" not in str(error.value)


def test_bom_is_supported_and_read_is_non_destructive(document, tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(document), encoding="utf-8-sig")
    before = path.read_bytes()
    assert loader.load_config(path).schema_version == 1
    assert path.read_bytes() == before


@pytest.mark.parametrize("blob", [b'\xff\xfe', b'\x80', b'{"x":"\xff"}'])
def test_invalid_encoding_is_reported_without_traceback(tmp_path, blob):
    path = tmp_path / "invalid.json"
    path.write_bytes(blob)
    with pytest.raises(loader.ConfigError):
        loader.load_config(path)


@pytest.mark.parametrize("key", ["schema_version", "panel", "runtime", "services"])
def test_required_sections(document, tmp_path, key):
    del document[key]
    with pytest.raises(loader.ConfigError):
        parse(document, tmp_path)


@pytest.mark.parametrize("version", [True, False, 0, 2, "1", None])
def test_unsupported_versions(document, tmp_path, version):
    document["schema_version"] = version
    with pytest.raises(loader.ConfigError):
        parse(document, tmp_path)


@pytest.mark.parametrize("value", ["false", 0, 1, [], None])
@pytest.mark.parametrize("service", loader.SERVICE_NAMES)
def test_service_flags_are_strict_booleans(document, tmp_path, value, service):
    document["services"][service]["enabled"] = value
    with pytest.raises(loader.ConfigError):
        parse(document, tmp_path)


@pytest.mark.parametrize("path", [(), ("panel",), ("runtime",), ("runtime", "restart"),
    ("services",), ("services", "hub", "options"), ("services", "media", "options")])
def test_unknown_fields_are_rejected_without_echoing_keys(document, tmp_path, path):
    item = document
    for key in path:
        item = item[key]
    item["private-token-123"] = "do-not-print"
    with pytest.raises(loader.ConfigError) as error:
        parse(document, tmp_path)
    assert "private-token-123" not in str(error.value)
    assert "do-not-print" not in str(error.value)


@pytest.mark.parametrize("url", ["file:///secret", "http://", "http://a:bad", "http://a:99999",
    "http://user:secret@host", "http://host?token=secret", "http://host#secret", "http://white space",
    "http://host:0", "http://host\\private"])
def test_invalid_panel_urls_are_private(document, tmp_path, url):
    document["panel"]["url"] = url
    with pytest.raises(loader.ConfigError) as error:
        parse(document, tmp_path)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("url,expected", [("HTTP://Example.COM:80/", "http://example.com"),
    ("https://Example.COM:443/prefix/", "https://example.com/prefix"),
    ("http://[::1]:8080/", "http://[::1]:8080")])
def test_panel_endpoint_normalization(document, tmp_path, url, expected):
    document["panel"]["url"] = url
    assert parse(document, tmp_path).panel.url == expected


@pytest.mark.parametrize("clock", ["24:00", "12:60", "7:00", "secret", "-1:00", None])
def test_quiet_hours_use_strict_24_hour_format(document, tmp_path, clock):
    document["panel"]["quiet_hours"]["start"] = clock
    with pytest.raises(loader.ConfigError):
        parse(document, tmp_path)


def test_timezone_is_checked_offline_and_redacted(document, tmp_path):
    document["panel"]["timezone"] = "private/secret"
    with pytest.raises(loader.ConfigError) as error:
        parse(document, tmp_path)
    assert "private/secret" not in str(error.value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), True, 0, -1, 86401])
def test_runtime_numeric_limits(document, tmp_path, value):
    document["runtime"]["restart"]["window_seconds"] = value
    with pytest.raises(loader.ConfigError):
        parse(document, tmp_path)


def test_paths_are_relative_to_selected_config_and_default_state_is_preserved(document, tmp_path):
    document["runtime"]["data_dir"] = "state"
    document["services"]["media"]["options"]["script_path"] = "art/media.ax"
    document["services"]["hub"]["options"]["next_up"] = {
        "sources": ["calendar.ics", "webcal://private.test/feed?token=secret"]}
    config = parse(document, tmp_path)
    assert config.runtime.data_dir == tmp_path / "state"
    assert config.services["media"].options["script_path"] == str(tmp_path / "art" / "media.ax")
    assert config.services["hub"].options["next_up"]["sources"] == (
        str(tmp_path / "calendar.ics"), "https://private.test/feed?token=secret")
    assert not (tmp_path / "state").exists()
    document["runtime"]["data_dir"] = None
    assert parse(document, tmp_path, environ={"LOCALAPPDATA": str(tmp_path)}).runtime.data_dir == tmp_path / "AWTRIX"
    assert loader.default_config_path({"LOCALAPPDATA": str(tmp_path)}) == tmp_path / "AWTRIX" / "driftmatrix.json"


def test_config_and_nested_options_are_immutable_and_repr_is_private(document, tmp_path):
    document["services"]["hub"]["options"]["github"] = {"token": "private-token"}
    config = parse(document, tmp_path)
    with pytest.raises(FrozenInstanceError):
        config.schema_version = 2
    with pytest.raises(TypeError):
        config.services["hub"] = None
    with pytest.raises(TypeError):
        config.services["hub"].options["github"]["token"] = "changed"
    copy = config.to_mapping()
    copy["services"]["hub"]["options"]["github"]["token"] = "changed"
    assert config.services["hub"].options["github"]["token"] == "private-token"
    assert "private-token" not in repr(config)
    assert "192.0.2.10" not in repr(config)
    assert document["runtime"]["data_dir"] is None


def test_snapshot_hash_is_stable_across_key_order_and_editor_schema_reference(document, tmp_path):
    first = parse(document, tmp_path)
    second = dict(reversed(list(document.items())))
    second["$schema"] = "https://untrusted.invalid/not-fetched"
    assert parse(second, tmp_path).config_hash == first.config_hash
    second["services"] = deepcopy(second["services"])
    second["services"]["hub"]["enabled"] = True
    assert parse(second, tmp_path).config_hash != first.config_hash


@pytest.mark.parametrize("service", ["weather", "aqi"])
@pytest.mark.parametrize("options", [{}, {"latitude": 2}, {"longitude": 2},
    {"latitude": 91, "longitude": 0}, {"latitude": 0, "longitude": -181}])
def test_enabled_weather_requires_valid_coordinates(document, tmp_path, service, options):
    document["services"][service] = {"enabled": True, "options": options}
    with pytest.raises(loader.ConfigError):
        parse(document, tmp_path)


@pytest.mark.parametrize("section,options", [
    ("network_sentinel", {"alert_refresh_seconds": 30, "alert_lifetime_seconds": 45}),
    ("focus_sprint", {"refresh_seconds": 30}),
    ("maintenance", {"window_start": "01:00", "window_end": "01:00"}),
    ("companion", {"refresh_seconds": 301}),
])
def test_hub_cross_field_constraints(document, tmp_path, section, options):
    document["services"]["hub"]["options"][section] = options
    with pytest.raises(loader.ConfigError):
        parse(document, tmp_path)


@pytest.mark.parametrize("key", ["F25", "Ctrl+Ctrl+1", "Hyper+1", "", "Ctrl+"])
def test_bad_hotkeys_are_rejected_without_worker_import(document, tmp_path, key):
    document["services"]["drift_link"]["options"]["macro_mapping"] = {
        "1": {"action": "cycle_app", "hotkey_primary": key}}
    with pytest.raises(loader.ConfigError):
        parse(document, tmp_path)


def test_hotkey_alias_collisions_are_rejected(document, tmp_path):
    document["services"]["drift_link"]["options"]["macro_mapping"] = {
        "1": {"action": "cycle_app", "hotkey_primary": "Ctrl+Esc"},
        "2": {"action": "toggle_power", "hotkey_primary": "Control+Escape"}}
    with pytest.raises(loader.ConfigError, match="duplicate"):
        parse(document, tmp_path)


def test_private_alias_titles_do_not_appear_in_errors(document, tmp_path):
    document["services"]["hub"]["options"]["next_up"] = {"aliases": {"private-appointment": 5}}
    with pytest.raises(loader.ConfigError) as error:
        parse(document, tmp_path)
    assert "private-appointment" not in str(error.value)


def test_import_and_validation_start_no_services_and_do_not_connect(tmp_path):
    code = '''
import socket, subprocess, sys, threading
def forbidden(*args, **kwargs): raise AssertionError("external action")
socket.socket.connect = forbidden
subprocess.Popen = forbidden
before = threading.active_count()
import config_loader, config_migration, driftmatrix
config_loader.config_from_mapping(config_loader.example_document(), source_path="example.json")
assert threading.active_count() == before
assert not set(["utility_hub", "panel_client", "panel_gate", "drift_ecosystem", "now_playing_push", "pc_stats_push"]) & set(sys.modules)
'''
    done = subprocess.run([sys.executable, "-X", "utf8", "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("source", ["   ", "ftp://private/secret", "https://", "http://host:invalid"])
def test_invalid_calendar_sources_are_redacted(document, tmp_path, source):
    document["services"]["hub"]["options"]["next_up"] = {"sources": [source]}
    with pytest.raises(loader.ConfigError) as error:
        parse(document, tmp_path)
    assert "private" not in str(error.value)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("value", [{1: "value"}, {"x": set()}, {"x": object()}])
def test_non_json_api_values_are_rejected(tmp_path, value):
    with pytest.raises(loader.ConfigError):
        parse(value, tmp_path)
