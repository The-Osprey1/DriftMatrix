"""Drift-Link protocol, configuration, and OS-hook regressions; no live devices."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from jsonschema import Draft7Validator

import drift_ecosystem as ecosystem
from drift_link_core import DriftEvent, DriftEventType
import panel_gate


@pytest.fixture
def config():
    return json.loads(ecosystem.CONFIG_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def make_eco(tmp_path, config):
    omitted = object()

    def make(value=omitted):
        path = tmp_path / "ecosystem.json"
        path.write_text(json.dumps(config if value is omitted else value), encoding="utf-8")
        eco = ecosystem.DriftEcosystem(path)
        eco.session = Mock(spec=requests.Session)
        eco.session.get.return_value = Mock(status_code=200)
        eco.session.get.return_value.json.return_value = {"matrixPower": True}
        eco.session.put.return_value = Mock(status_code=200)
        eco.session.patch.return_value = Mock(status_code=200)
        return eco
    return make


def journal(directory):
    path = directory / panel_gate.EVENTS_NAME
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("method,op,app,verb,path,payload", [
    ("switch", "activate", "moon", "put", "/api/v1/apps/active", {"name": "moon", "fast": True}),
    ("toggle", "display", None, "patch", "/api/v1/display", {"power": False}),
    ("alert", "push", "drift_alert", "put", "/api/v1/apps/pushed/drift_alert",
     {"text": "TEST", "lifetimeMs": 4000, "lifetimeExpiry": "remove"}),
])
@pytest.mark.parametrize("outcome", [200, 400, 503, "timeout"])
def test_mutations_use_real_gate_and_journal_response_or_exception(
    make_eco, panel_gate_dir, method, op, app, verb, path, payload, outcome,
):
    eco = make_eco()
    request = getattr(eco.session, verb)

    def response(*args, **kwargs):
        # This is the real shared gate, not a mocked context manager.
        assert panel_gate._local.depth == 1
        assert args[0] == eco.matrix_url + path
        assert kwargs["json"] == payload
        if outcome == "timeout":
            raise requests.Timeout("transport failed")
        return Mock(status_code=outcome)

    request.side_effect = response
    calls = {"switch": lambda: eco.switch_matrix_app("moon"),
             "toggle": eco.toggle_matrix_power,
             "alert": lambda: eco.push_matrix_notification("TEST")}
    assert calls[method]() is (outcome == 200)
    request.assert_called_once()
    [entry] = journal(panel_gate_dir)
    assert (entry["src"], entry["op"], entry["app"]) == ("ecosystem", op, app)
    assert entry["gate"] == "ok"
    assert entry["bytes"] == panel_gate.body_size(payload)
    assert entry["status"] == (None if outcome == "timeout" else outcome)
    assert entry["err"] == ("Timeout" if outcome == "timeout" else None)
    assert panel_gate._local.depth == 0
    eco.session.delete.assert_not_called()


@pytest.mark.parametrize("current", [True, False])
def test_toggle_success_is_independent_of_requested_power(make_eco, current):
    eco = make_eco()
    eco.session.get.return_value.json.return_value = {"matrixPower": current}
    assert eco.toggle_matrix_power() is True
    eco.session.patch.assert_called_once_with(
        eco.matrix_url + "/api/v1/display", json={"power": not current}, timeout=1.5,
    )
    eco.session.put.assert_not_called()


@pytest.mark.parametrize("body", [{}, {"matrixPower": "false"}, {"matrixPower": 0},
                                    {"matrixPower": None}, [], None])
def test_toggle_refuses_unknown_power_state(make_eco, body):
    eco = make_eco()
    eco.session.get.return_value.json.return_value = body
    assert eco.toggle_matrix_power() is False
    eco.session.patch.assert_not_called()


@pytest.mark.parametrize("failure", ["http", "timeout", "json"])
def test_toggle_does_not_write_after_failed_read(make_eco, failure):
    eco = make_eco()
    if failure == "http":
        eco.session.get.return_value.status_code = 503
    elif failure == "timeout":
        eco.session.get.side_effect = requests.Timeout()
    else:
        eco.session.get.return_value.json.side_effect = ValueError()
    assert eco.toggle_matrix_power() is False
    eco.session.patch.assert_not_called()


def test_notification_preserves_icon_and_lifetime(make_eco):
    eco = make_eco()
    assert eco.push_matrix_notification("HELLO", icon="abc", duration_ms=1200)
    assert eco.session.put.call_args.kwargs["json"] == {
        "text": "HELLO", "icon": "abc", "lifetimeMs": 1200, "lifetimeExpiry": "remove",
    }


def test_disabled_devices_do_no_io_and_register_no_hooks(make_eco, config, monkeypatch):
    for device in config["ecosystem"]["devices"].values():
        device["enabled"] = False
    eco = make_eco(config)
    serial = Mock()
    monkeypatch.setattr(ecosystem, "serial", serial)
    user32 = Mock()
    assert not eco.switch_matrix_app("moon")
    assert not eco.toggle_matrix_power()
    assert not eco.push_matrix_notification("TEST")
    status = eco.get_ecosystem_status()
    assert all(not device.connected for device in status.values())
    assert all(device.details["state"] == "Disabled by configuration" for device in status.values())
    ecosystem.run_hotkey_listener(eco, user32=user32)
    assert not eco.session.mock_calls
    assert not serial.mock_calls
    assert not user32.mock_calls


def test_pad_disabled_does_not_disable_watch_or_matrix(make_eco, config):
    config["ecosystem"]["devices"]["pad"]["enabled"] = False
    eco = make_eco(config)
    assert not eco.hotkeys
    assert eco.device_enabled("watch")
    assert eco.switch_matrix_app("moon")


def test_matrix_disabled_does_not_register_shortcuts(make_eco, config):
    config["ecosystem"]["devices"]["matrix"]["enabled"] = False
    eco = make_eco(config)
    user32 = Mock()
    ecosystem.run_hotkey_listener(eco, user32=user32)
    assert not user32.mock_calls


@pytest.mark.parametrize("device,method", [("pad", "probe_driftpad"), ("watch", "probe_driftwatch")])
def test_usb_presence_is_reported_without_firmware_claim(make_eco, config, monkeypatch, device, method):
    config["ecosystem"]["devices"][device]["vid"] = "0x1234"
    eco = make_eco(config)
    serial = Mock()
    serial.tools.list_ports.comports.return_value = [
        SimpleNamespace(vid=0x1234, device="COM7", description="Test adapter"),
    ]
    monkeypatch.setattr(ecosystem, "serial", serial)
    result = getattr(eco, method)()
    assert result.connected
    assert result.details["port"] == "COM7"
    assert result.details["state"] == "USB port detected; adapter present, firmware unverified"
    serial.Serial.assert_not_called()


def test_dashboard_never_calls_usb_presence_synchronization(make_eco, capsys):
    eco = make_eco()
    eco.get_ecosystem_status = Mock(return_value={
        key: ecosystem.DeviceStatus(key, True, "test", {}) for key in ("matrix", "pad", "watch")
    })
    ecosystem.print_dashboard(eco)
    output = capsys.readouterr().out
    assert "ADAPTER PRESENT" in output
    assert "API ONLINE" in output
    assert "SYNCHRONIZED" not in output


@pytest.mark.parametrize("value", [[], None, {}, {"version": "2", "ecosystem": {}},
    {"version": "1.0.0", "ecosystem": {"devices": []}}])
def test_bad_config_structure_fails_before_io(make_eco, value):
    with pytest.raises(ValueError, match="Invalid ecosystem configuration"):
        make_eco(value)


@pytest.mark.parametrize("change", ["flag", "rotation", "url", "action", "target", "unknown"])
def test_bad_config_fields_fail_before_io(make_eco, config, change):
    devices = config["ecosystem"]["devices"]
    if change == "flag":
        devices["matrix"]["enabled"] = "false"
    elif change == "rotation":
        devices["matrix"]["rotation_apps"] = []
    elif change == "url":
        devices["matrix"]["url"] = "file:///private"
    elif change == "action":
        devices["pad"]["macro_mapping"]["1"]["action"] = "unknown"
    elif change == "target":
        del devices["pad"]["macro_mapping"]["1"]["target"]
    else:
        devices["matrix"]["enabeld"] = False
    with pytest.raises(ValueError, match="Invalid ecosystem configuration"):
        make_eco(config)


@pytest.mark.parametrize("contents", ["{", "null", "[]"])
def test_invalid_json_never_silently_enables_defaults(tmp_path, contents):
    path = tmp_path / "invalid.json"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError):
        ecosystem.DriftEcosystem(path)


def test_missing_config_fails_explicitly(tmp_path):
    with pytest.raises(ValueError, match="Cannot read ecosystem configuration"):
        ecosystem.DriftEcosystem(tmp_path / "missing.json")


def test_schema_is_self_contained_and_accepts_shipped_config(config):
    schema = json.loads(ecosystem.SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft7Validator.check_schema(schema)
    Draft7Validator(schema).validate(config)


@pytest.mark.parametrize("key,modifiers,vk", [("F13", 0x4000, 0x7C),
    ("Ctrl+Alt+1", 0x4003, ord("1")), ("shift+win+space", 0x400C, 0x20),
    ("F24", 0x4000, 0x87), ("Control+Enter", 0x4002, 0x0D)])
def test_hotkey_parser(key, modifiers, vk):
    assert ecosystem.parse_hotkey(key) == (modifiers, vk)


@pytest.mark.parametrize("key", ["", "F25", "Hyper+1", "Ctrl+Ctrl+1", "Ctrl+", "F0"])
def test_invalid_hotkey_rejected_at_configuration_time(make_eco, config, key):
    config["ecosystem"]["devices"]["pad"]["macro_mapping"]["1"]["hotkey_primary"] = key
    with pytest.raises(ValueError):
        make_eco(config)


def test_alias_duplicates_are_rejected(make_eco, config):
    macros = config["ecosystem"]["devices"]["pad"]["macro_mapping"]
    macros["2"]["hotkey_primary"] = "control+alt+1"
    with pytest.raises(ValueError, match="Duplicate configured hotkey"):
        make_eco(config)


def test_custom_mapping_drives_registration_and_dispatched_action(make_eco, config):
    config["ecosystem"]["devices"]["pad"]["macro_mapping"] = {
        "9": {"name": "Custom", "action": "switch_app", "target": "ocean", "hotkey_primary": "Shift+F20"},
        "10": {"enabled": False, "action": "toggle_power", "hotkey_primary": "F21"},
    }
    eco = make_eco(config)
    eco.bus = Mock()
    user32 = Mock()
    user32.RegisterHotKey.return_value = True

    def message(pointer, *_args):
        pointer._obj.message = 0x0312
        pointer._obj.wParam = 1
        return 1

    messages = iter([1, 0])
    user32.GetMessageW.side_effect = lambda pointer, *args: message(pointer, *args) if next(messages) else 0
    ecosystem.run_hotkey_listener(eco, user32=user32)
    user32.RegisterHotKey.assert_called_once_with(None, 1, 0x4004, 0x83)
    user32.UnregisterHotKey.assert_called_once_with(None, 1)
    event = eco.bus.publish.call_args.args[0]
    assert event.data == {"macro_index": 9, "action": "switch_app", "target": "ocean"}
    eco.bus.start.assert_called_once()
    eco.bus.stop.assert_called_once()


def test_registration_failure_releases_previous_hotkeys(make_eco):
    eco = make_eco()
    eco.bus = Mock()
    user32 = Mock()
    user32.RegisterHotKey.side_effect = [True, False]
    with pytest.raises(RuntimeError, match="Could not register hotkey"):
        ecosystem.run_hotkey_listener(eco, user32=user32)
    user32.UnregisterHotKey.assert_called_once_with(None, 1)
    eco.bus.start.assert_not_called()
    eco.bus.stop.assert_called_once()


def test_message_loop_failure_releases_registered_hotkeys(make_eco):
    eco = make_eco()
    eco.bus = Mock()
    user32 = Mock()
    user32.GetMessageW.return_value = -1
    with pytest.raises(RuntimeError, match="message loop failed"):
        ecosystem.run_hotkey_listener(eco, user32=user32)
    assert user32.UnregisterHotKey.call_count == len(eco.hotkeys)
    eco.bus.stop.assert_called_once()


def test_cycle_only_advances_after_success(make_eco):
    eco = make_eco()
    eco.session.put.side_effect = [Mock(status_code=503), Mock(status_code=200), Mock(status_code=200)]
    assert eco.cycle_matrix_app() == ""
    assert eco.cycle_matrix_app() == eco.app_cycle[0]
    assert eco.cycle_matrix_app() == eco.app_cycle[1]


def test_alert_event_uses_guarded_transport(make_eco, panel_gate_dir):
    eco = make_eco()
    eco.bus.publish_sync(DriftEvent(DriftEventType.ALERT_BROADCAST, "Test", {"message": "TEST"}))
    [entry] = journal(panel_gate_dir)
    assert (entry["src"], entry["op"]) == ("ecosystem", "push")


def test_cli_alert_failure_has_nonzero_exit(make_eco, monkeypatch, capsys):
    eco = make_eco()
    eco.session.put.return_value.status_code = 503
    monkeypatch.setattr(ecosystem, "DriftEcosystem", lambda path: eco)
    monkeypatch.setattr(ecosystem.sys, "argv", ["drift_ecosystem.py", "alert", "--message", "TEST"])
    with pytest.raises(SystemExit) as failure:
        ecosystem.main()
    assert failure.value.code == 1
    assert "failed" in capsys.readouterr().err
