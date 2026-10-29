"""Firmware transitions exercise real transport boundaries with no live panel."""
import base64
import json
from unittest.mock import Mock

import pytest
import requests

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


@pytest.mark.parametrize('version,modern', [('1.1.2', False), ('1.2.2', True)])
@pytest.mark.parametrize('image,mime', [(GIF, 'gif'), (JPEG, 'jpeg')])
def test_cold_client_observes_health_before_its_first_image_write(version, modern, image, mime):
    events = []
    response = Mock(status_code=200)
    response.json.return_value = {'version': version, 'uptimeSeconds': 3600}
    session = Mock()
    session.get.side_effect = lambda *a, **k: (events.append('health'), response)[1]
    session.put.side_effect = lambda *a, **k: (events.append('push'), response)[1]
    client = panel_client.PanelClient(URL, session=session,
        policy=panel_client.PanelPolicy(minimum_mutation_interval_seconds=0))
    original = {'icon': image, 'text': 'First frame'}
    client.push_app('first', original)
    assert events == ['health', 'push']
    assert session.put.call_args.kwargs['json']['icon'] == (f'data:image/{mime};base64,' + image if modern else image)
    assert original['icon'] == image
    client.push_app('second', original)
    assert events == ['health', 'push', 'push']


@pytest.mark.parametrize('invalid', [requests.Timeout('offline'), {'uptimeSeconds': 'invalid'}])
def test_cold_probe_failure_defers_delivery_without_latching_mutation_failures(invalid):
    session = Mock()
    if isinstance(invalid, Exception):
        session.get.side_effect = invalid
    else:
        session.get.return_value.status_code = 200
        session.get.return_value.json.return_value = invalid
    client = panel_client.PanelClient(URL, session=session)
    with pytest.raises(panel_client.PanelClientError):
        client.push_app('first', {'icon': GIF})
    session.put.assert_not_called()
    assert not client.static_latched
    session.get.side_effect = None
    session.get.return_value.status_code = session.put.return_value.status_code = 200
    session.get.return_value.json.return_value = {'version': '1.2.2', 'uptimeSeconds': 30}
    client.push_app('first', {'icon': GIF})
    assert session.put.call_args.kwargs['json']['icon'].startswith('data:image/gif;base64,')


@pytest.mark.parametrize('payload', [{'text': 'No image'}, {'icon': 'weather.gif'}, {'icon': 'not base64'},
    {'icon': 'data:image/gif;base64,' + GIF}])
def test_nonbare_icons_and_text_keep_the_existing_request_behavior(payload):
    session = Mock()
    session.put.return_value.status_code = 200
    session.get.side_effect = AssertionError('No cold image-format lookup is needed')
    client = panel_client.PanelClient(URL, session=session)
    client.push_app('test', payload)
    session.get.assert_not_called()
    assert session.put.call_args.kwargs['json'] == payload


def test_unavailable_first_probe_preserves_focus_commands_and_elapsed_time(tmp_path):
    from datetime import datetime, timezone
    from focus_control import FocusCommandQueue
    from focus_controller import FocusController
    from test_focus_controller import FakeClock

    clock = FakeClock()
    queue = FocusCommandQueue(tmp_path / 'commands',
        now=lambda: datetime.fromtimestamp(clock.wall_time(), timezone.utc))
    command = queue.enqueue('start', minutes=25)
    session = Mock()
    session.get.side_effect = requests.Timeout('unavailable during startup')
    client = panel_client.PanelClient(URL, session=session,
        policy=panel_client.PanelPolicy(minimum_mutation_interval_seconds=0))
    path = tmp_path / 'state.json'
    controller = FocusController(client, queue=queue, state_path=path, clock=clock)
    first = controller.tick()
    saved = json.loads(path.read_text())
    assert first.commands_processed == 1 and first.panel_deferred
    assert saved['phase'] == 'focus_running' and command.id in saved['consumed_command_ids']
    assert not queue.read()
    session.put.assert_not_called()
    assert client.consecutive_mutation_failures == 0

    clock.advance(60)
    session.get.side_effect = None
    session.get.return_value.status_code = session.put.return_value.status_code = 200
    session.get.return_value.json.return_value = {'version': '1.2.2', 'uptimeSeconds': 3600}
    recovered = controller.tick()
    assert recovered.pushed and recovered.remaining_seconds == 24 * 60
    pushes = [call for call in session.put.call_args_list if call.args[0].endswith('/apps/pushed/focus')]
    assert len(pushes) == 1 and pushes[0].kwargs['json']['icon'].startswith('data:image/gif;base64,')
