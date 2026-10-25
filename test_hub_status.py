import json
import os
from unittest.mock import Mock

import hub_status as status
from panel_client import DeviceHealth, PanelTransportError


def test_reporter_limits_probes_and_reports_current_config_and_delivery(tmp_path):
    now = [1000.0]
    panel = Mock()
    reminder = Mock(delivery_status={"id": "test", "phase": "countdown", "sent_at": 1000})
    reporter = status.HubStatusReporter(panel, reminders_enabled=True, reminder=reminder,
        path=tmp_path / 'status.json', clock=lambda: now[0], monotonic=lambda: now[0])
    reporter.tick()
    now[0] += 5
    reporter.tick()
    assert panel.probe_device.call_count == 1
    data = status.read_status(reporter.path)
    assert status.indicator_text(data, now[0], 'test') == ('Panel · reachable (5s ago)', 'Reminders · sent to panel')
    assert status.indicator_text(data, now[0], 'replacement')[1] == 'Reminders · waiting for delivery'
    now[0] += 30
    panel.probe_device.side_effect = PanelTransportError('offline')
    reporter.tick()
    data = status.read_status(reporter.path)
    assert status.indicator_text(data, now[0], 'test') == ('Panel · unreachable', 'Reminders · waiting for delivery')
    reminder.delivery_status = None
    reporter.reminders_enabled = False
    reporter.tick()
    assert status.indicator_text(status.read_status(reporter.path), now[0])[1] == 'Reminders · disabled'


def test_missing_corrupt_and_stale_status_never_claim_connection(tmp_path):
    path = tmp_path / 'status.json'
    assert status.read_status(path) is None
    for body in [b'[]', b'\xff', b'{}', b'x'*4097, b'{"schema_version":true}']:
        path.write_bytes(body)
        assert status.read_status(path) is None
    for data in [None, {'updated_at': 1}, {'updated_at': 1001}, {'updated_at': float('nan')}]:
        assert status.indicator_text(data, 1000)[0] == 'Panel · status unavailable'
    assert not status.fresh(True, 1)
    assert status.indicator_text({'updated_at':1000, 'panel_checked_at':1, 'reminders_enabled':True},1000)[0] == 'Panel · last check is stale'


def test_each_probe_leaves_a_heap_line_and_the_log_rotates(tmp_path):
    from panel_client import DeviceHealth
    import json
    now = [1000.0]
    panel = Mock()
    panel.probe_device.return_value = DeviceHealth(3600, "software", 52000, 41000, 36852, 42)
    reporter = status.HubStatusReporter(panel, reminders_enabled=False,
        path=tmp_path / 'status.json', clock=lambda: now[0], monotonic=lambda: now[0])
    reporter.tick()
    now[0] += 30
    panel.probe_device.return_value = DeviceHealth(12, "panic", 60000, 60000, 40000, 40)
    reporter.tick()
    lines = [json.loads(l) for l in reporter.health_log.read_text().splitlines()]
    assert [l["uptime"] for l in lines] == [3600, 12]
    assert lines[0]["block"] == 36852 and lines[1]["reason"] == "panic"
    assert lines[0]["app"] is None and lines[0]["power"] is None

    log = tmp_path / 'h.jsonl'
    for i in range(30):
        status.append_health(log, DeviceHealth(i), i, max_bytes=500)
    assert log.with_suffix('.1.jsonl').exists() and log.stat().st_size < 600


def test_health_line_names_the_app_on_screen_and_matrix_power(tmp_path):
    from panel_client import DeviceHealth
    import json
    panel = Mock()
    panel.probe_device.return_value = DeviceHealth(
        60, "software", 50000, 40000, 30000, 33, current_app="ocean", matrix_power=True)
    reporter = status.HubStatusReporter(panel, reminders_enabled=False,
        path=tmp_path / 'status.json', clock=lambda: 1.0, monotonic=lambda: 1.0)
    reporter.tick()
    line = json.loads(reporter.health_log.read_text())
    assert line["app"] == "ocean" and line["power"] is True and line["fps"] == 33


def test_health_line_carries_wifi_with_a_stable_schema(tmp_path):
    from panel_client import DeviceHealth
    import json
    log = tmp_path / 'h.jsonl'
    status.append_health(log, DeviceHealth(
        60, "software", 50000, 40000, 30000, 33, current_app="ocean", matrix_power=True,
        wifi_rssi=-71, wifi_connects=2, wifi_attempts=0, wifi_state="connected",
        wifi_last_error="beacon timeout"), 1.0)
    status.append_health(log, DeviceHealth(61, "software", wifi_rssi=-69, wifi_connects=2,
                                           wifi_attempts=0, wifi_state="connected"), 2.0)
    status.append_health(log, DeviceHealth(62), 3.0)
    first, second, bare = [json.loads(l) for l in log.read_text().splitlines()]
    assert (first["rssi"], first["wconn"], first["watt"], first["wstate"], first["werr"]) == (
        -71, 2, 0, "connected", "beacon timeout")
    assert "werr" not in second and second["rssi"] == -69
    # The four short keys stay even when the panel reported nothing; werr does not.
    assert {k: bare[k] for k in ("rssi", "wconn", "watt", "wstate")} == dict.fromkeys(
        ("rssi", "wconn", "watt", "wstate"))
    assert "werr" not in bare
    assert set(first) - {"werr"} == set(second) == set(bare)


def test_health_line_size_matches_the_rotation_comment(tmp_path):
    from panel_client import DeviceHealth
    log = tmp_path / 'h.jsonl'
    # A real 2026-10-09 probe plus the Wi-Fi of that day's device sample.
    status.append_health(log, DeviceHealth(
        2800, "software", 40264, 12520, 23540, 34, current_app="ocean", matrix_power=True,
        wifi_rssi=-69, wifi_connects=1, wifi_attempts=0, wifi_state="connected"), 1791578721)
    typical = len(log.read_bytes())
    assert 190 <= typical <= 215, typical
    hours = status.HEALTH_LOG_MAX_BYTES / typical * status.PROBE_INTERVAL_SECONDS / 3600
    assert 38 <= hours <= 46  # "about 41 hours" in the comment

    def line_bytes(text: str, cap_factor: int = 1) -> int:
        path = tmp_path / f'w{len(list(tmp_path.iterdir()))}.jsonl'
        big = 2**32 - 1
        status.append_health(path, DeviceHealth(
            big, text * 32 * cap_factor, big, big, big, 42.123456789, current_app=text * 32 * cap_factor,
            matrix_power=False, wifi_rssi=-120, wifi_connects=big, wifi_attempts=big,
            wifi_state=text * 16 * cap_factor, wifi_last_error=text * 48 * cap_factor), 1791604315)
        body = path.read_bytes()
        json.loads(body.decode("utf-8"))
        return len(body)

    # Every text field at its cap and numbers at their uint32 maximum.
    # Text-mode writes use CRLF on Windows and LF on Linux.
    newline_extra = len(os.linesep.encode("ascii")) - 1
    assert line_bytes("x") == 366 + newline_extra
    assert line_bytes("\U0001F600") == 750 + newline_extra
    assert line_bytes("\U0001F600", cap_factor=20) == 750 + newline_extra
    assert max(line_bytes(c) for c in ('"', "\\", "あ", "é")) < 750 + newline_extra
    # The review's case: 1,069 bytes when the line was \u-escaped ASCII.
    review = tmp_path / 'review.jsonl'
    status.append_health(review, DeviceHealth(1, current_app="é" * 40, wifi_state="é" * 20,
                                              wifi_last_error="\U0001F600" * 60), 1.0)
    assert len(review.read_bytes()) < 500
    assert "é" * 32 in review.read_text(encoding="utf-8")  # UTF-8, not é escapes


def test_reset_reason_is_bounded_and_clean_in_the_log(tmp_path):
    log = tmp_path / 'h.jsonl'
    status.append_health(log, DeviceHealth(1, "z" * 5000), 1.0)
    status.append_health(log, DeviceHealth(2, " soft\nware\x00 "), 2.0)
    status.append_health(log, DeviceHealth(3, "a\ud800b"), 3.0)  # a lone surrogate from JSON
    first, second, third = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert first["reason"] == "z" * status.REASON_CHARS
    assert second["reason"] == "software"
    assert third["reason"] == "ab"


def test_an_unencodable_line_is_skipped_not_raised(tmp_path):
    log = tmp_path / 'h.jsonl'
    status._append_health_line(log, {"t": 1, "app": "\ud800"}, 1000)  # must not raise
    assert not log.exists() or log.read_text(encoding="utf-8") == ""


def test_the_reporter_records_why_the_probe_failed_and_whether_this_pc_is_offline(tmp_path):
    from panel_client import DeviceHealth, PanelHTTPError, PanelProtocolError
    now = [1000.0]
    offline = Mock(return_value=False)
    panel = Mock()
    reporter = status.HubStatusReporter(panel, reminders_enabled=False, path=tmp_path / 'status.json',
        clock=lambda: now[0], monotonic=lambda: now[0], pc_offline=offline)
    cases = [
        (PanelTransportError("offline"), False, "unreachable"),
        (OSError("no route"), True, "unreachable"),
        (PanelHTTPError("probe device", 503, "busy"), False, "http"),
        (PanelProtocolError("probe device returned invalid JSON"), False, "protocol"),
        (DeviceHealth(60), False, None),
    ]
    for outcome, pc_offline, failure in cases:
        panel.probe_device.side_effect = outcome if isinstance(outcome, Exception) else None
        panel.probe_device.return_value = outcome
        offline.return_value = pc_offline
        state = reporter.tick()
        assert (reporter.failure, reporter.pc_offline) == (failure, pc_offline)
        assert (state["panel_failure"], state["pc_offline"]) == (failure, pc_offline)
        saved = status.read_status(reporter.path)
        assert (saved["panel_failure"], saved["pc_offline"]) == (failure, pc_offline)
        calls = offline.call_count
        reporter.tick()  # a tick between probes samples nothing
        assert offline.call_count == calls
        now[0] += 30
    assert offline.call_count == len(cases)  # once per probe


def test_pc_offline_is_none_when_nothing_can_tell(tmp_path):
    panel = Mock()
    panel.probe_device.side_effect = PanelTransportError("offline")
    for check in (None, Mock(side_effect=RuntimeError("sentinel broke")), Mock(return_value=Mock())):
        reporter = status.HubStatusReporter(panel, reminders_enabled=False,
            path=tmp_path / 'status.json', clock=lambda: 1.0, monotonic=lambda: 1.0, pc_offline=check)
        assert reporter.tick()["pc_offline"] is None


def test_failure_kinds():
    from panel_client import PanelClientError, PanelHTTPError, PanelProtocolError, UnsafeAppDeletion
    assert status.failure_kind(PanelTransportError("x")) == status.FAILURE_UNREACHABLE
    assert status.failure_kind(ConnectionResetError()) == status.FAILURE_UNREACHABLE
    assert status.failure_kind(PanelHTTPError("probe", 500)) == status.FAILURE_HTTP
    assert status.failure_kind(PanelProtocolError("x")) == status.FAILURE_PROTOCOL
    assert status.failure_kind(UnsafeAppDeletion("x")) == status.FAILURE_OTHER
    assert status.failure_kind(PanelClientError("x")) == status.FAILURE_OTHER


def test_awake_seconds_is_a_running_clock_on_windows():
    import os
    import time
    first = status.awake_seconds()
    if os.name != "nt":
        assert first is None
        return
    time.sleep(0.05)
    second = status.awake_seconds()
    assert isinstance(first, float) and 0 < first < second


def test_the_ssid_never_reaches_the_health_log(tmp_path):
    from panel_client import PanelClient
    ssid, endpoint = "OurHouse-5G", "ourhouse-mesh-2"
    body = {"uptimeSeconds": 600, "resetReason": "software", "largestFreeBlockBytes": 30000,
            "wifiRssi": -70, "wifi": {"enabled": True, "state": "connected", "host": ssid,
                                      "endpoint": endpoint, "attempts": 0, "connects": 2,
                                      "error": None, "lastError": f"lost {ssid} via {endpoint}"}}
    session = Mock()
    session.get.return_value = Mock(status_code=200, json=Mock(return_value=body))
    client = PanelClient("http://panel.local", session=session)
    reporter = status.HubStatusReporter(client, reminders_enabled=False,
        path=tmp_path / 'status.json', clock=lambda: 1.0, monotonic=lambda: 1.0)
    assert reporter.tick()["panel_reachable"] is True
    text = reporter.health_log.read_text()
    assert '"rssi":-70' in text and '"wconn":2' in text and '"werr":"lost * via *"' in text
    for secret in (ssid, endpoint, "OurHouse", "mesh"):
        assert secret.lower() not in text.lower()
    session.put.assert_not_called()
    session.delete.assert_not_called()


def test_health_log_failures_never_break_the_reporter(tmp_path):
    from panel_client import DeviceHealth
    panel = Mock()
    panel.probe_device.return_value = DeviceHealth(5)
    reporter = status.HubStatusReporter(panel, reminders_enabled=False, path=tmp_path / 'status.json',
        health_log=tmp_path / 'missing-dir' / 'h.jsonl')
    assert reporter.tick()["panel_reachable"] is True


def test_failed_probe_keeps_outage_and_recovery_in_health_timeline(tmp_path):
    import json
    from panel_client import DeviceHealth

    now = [1000.0]
    panel = Mock()
    panel.probe_device.side_effect = [
        DeviceHealth(3600, "software", largest_block=12000),
        PanelTransportError("offline at http://private-host/secret"),
        DeviceHealth(12, "panic", largest_block=40000),
    ]
    reporter = status.HubStatusReporter(panel, reminders_enabled=False,
        path=tmp_path / 'status.json', clock=lambda: now[0], monotonic=lambda: now[0])
    assert reporter.tick()["panel_reachable"] is True
    now[0] += 30
    assert reporter.tick()["panel_reachable"] is False
    now[0] += 5
    reporter.tick()
    assert panel.probe_device.call_count == 2
    now[0] += 25
    assert reporter.tick()["panel_reachable"] is True
    lines = [json.loads(line) for line in reporter.health_log.read_text().splitlines()]
    assert len(lines) == 3
    assert lines[1] == {"t": 1030, "reachable": False, "error": "PanelTransportError"}
    assert lines[0]["uptime"] == 3600 and lines[2]["uptime"] == 12
    assert lines[2]["reason"] == "panic"
    assert "private-host" not in reporter.health_log.read_text()


def test_unwritable_outage_log_preserves_unreachable_status(tmp_path):
    panel = Mock()
    panel.probe_device.side_effect = PanelTransportError("offline")
    reporter = status.HubStatusReporter(panel, reminders_enabled=False,
        path=tmp_path / 'status.json', health_log=tmp_path / 'missing-dir' / 'h.jsonl')
    assert reporter.tick()["panel_reachable"] is False
