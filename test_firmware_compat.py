"""Firmware transitions exercise real transport boundaries with no live panel."""
import base64
import json
from unittest.mock import Mock

import pytest

import firmware_compat as compat
import panel_client
import pc_stats_push as pc
from weather import weather_brief_push as weather, aqi_brief_push as aqi

URL = "http://panel.local"
GIF = base64.b64encode(b"GIF89a" + bytes(80)).decode("ascii")
JPEG = base64.b64encode(b"\xff\xd8\xff" + bytes(80)).decode("ascii")


@pytest.mark.parametrize("version,modern", [("1.1.2", False), ("1.2.1", False),
    ("1.2.2", True), ("v1.2.2", True), ("1.3.0", True), ("invalid", False),
    (None, False), (122, False)])
@pytest.mark.parametrize("icon,mime", [(GIF, "gif"), (JPEG, "jpeg")])
def test_version_selects_wire_format_without_altering_renderer(version, modern, icon, mime):
    original = {"text": "Forecast", "icon": icon, "draw": [["bitmap", 0, 0, 1, 1, "AAA="]]}
    compat.observe_device(URL + "/", {"version": version})
    wire = compat.prepare_payload(URL, original)
    assert wire is not original
    assert original["icon"] == icon
    assert wire["draw"] == original["draw"]
    assert wire["icon"] == (f"data:image/{mime};base64," + icon if modern else icon)


@pytest.mark.parametrize("icon", ["weather.gif", "123", "data:image/gif;base64," + GIF,
    "not base64", "YWJjZA==", None, 42, ""])
def test_names_invalid_values_and_existing_urls_are_not_reinterpreted(icon):
    compat.observe_device(URL, {"version": "1.2.2"})
    assert compat.prepare_payload(URL, {"icon": icon}) == {"icon": icon}


def test_endpoint_scope_and_downgrade():
    compat.observe_device(URL, {"version": "1.2.2"})
    assert compat.prepare_payload("http://other.local", {"icon": GIF})["icon"] == GIF
    compat.observe_device(URL, {"version": "1.1.2"})
    assert compat.prepare_payload(URL, {"icon": GIF})["icon"] == GIF


def test_shared_client_learns_version_from_health_and_journals_wire_size(panel_gate_dir):
    session = Mock()
    session.get.return_value.json.return_value = {"version": "1.2.2", "uptimeSeconds": 20}
    session.get.return_value.status_code = session.put.return_value.status_code = 200
    client = panel_client.PanelClient(URL, session=session)
    client.probe_device()
    client.push_app("test", {"icon": GIF})
    wire = session.put.call_args.kwargs["json"]
    assert wire["icon"] == "data:image/gif;base64," + GIF
    assert session.get.call_count == 1
    record = json.loads((panel_gate_dir / "panel_events.jsonl").read_text().splitlines()[-1])
    assert record["bytes"] == len(json.dumps(wire, separators=(",", ":")))


@pytest.mark.parametrize("module", [weather, aqi])
def test_feed_health_and_real_transport_use_data_urls(module):
    session = Mock()
    session.get.return_value.status_code = session.put.return_value.status_code = 200
    session.get.return_value.json.return_value = {"version": "1.2.2", "uptimeSeconds": 20}
    assert module.read_panel_health(session, URL) is not None
    module.push_payload(session, URL, {"icon": GIF})
    assert session.put.call_args.kwargs["json"]["icon"] == "data:image/gif;base64," + GIF
    assert session.get.call_count == 1


def test_pc_health_and_real_transport_use_data_urls(monkeypatch):
    response = Mock(status_code=200)
    response.json.return_value = {"version": "1.2.2", "uptimeSeconds": 20}
    monkeypatch.setattr(pc.requests, "get", Mock(return_value=response))
    put = Mock(return_value=response)
    monkeypatch.setattr(pc.requests, "put", put)
    assert pc.read_panel_health() is not None
    labels, error = pc.sync_panel([("CPU", 80)], ("CPU",), animated=False)
    assert error is None and labels == ("CPU",)
    wire = put.call_args.kwargs["json"]
    assert wire["icon"].startswith("data:image/gif;base64,")
    assert len(json.dumps(wire, separators=(",", ":")).encode()) <= 2048
