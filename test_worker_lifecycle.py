"""Worker lifecycle acceptance without real feeds, panel writes, or OS hotkeys."""
import asyncio
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import queue
import signal
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests

import config_loader
import drift_ecosystem as link
from drift_link_core import DriftEventBus, DriftEvent, DriftEventType
import focus_control
import now_playing_push as media
import panel_gate
import pc_stats_push as pc
from runtime_paths import data_directory
import sender_logging
import service_runtime as runtime
import utility_hub as hub

MODULES = {"hub": "utility_hub", "media": "now_playing_push", "pc_stats": "pc_stats_push",
           "weather": "weather.weather_brief_push", "aqi": "weather.aqi_brief_push", "drift_link": "drift_ecosystem"}


@pytest.fixture
def make_config(tmp_path):
    def make(name=None, options=None):
        document = config_loader.example_document()
        document["panel"]["url"] = "http://192.0.2.45"
        document["runtime"]["data_dir"] = str(tmp_path / "state")
        if name:
            document["services"][name] = {"enabled": True, "options": options or {}}
        return config_loader.config_from_mapping(document, source_path=tmp_path / "config.json")
    return make


class StopOnWait(threading.Event):
    def __init__(self, count=1):
        super().__init__()
        self.delays = []
        self.count = count

    def wait(self, timeout=None):
        self.delays.append(timeout)
        if timeout != 0.2:
            self.count -= 1
            if self.count <= 0:
                self.set()
        return self.is_set()


@pytest.mark.parametrize("name", MODULES)
@pytest.mark.parametrize("enabled", [False, True])
def test_disabled_or_pre_stopped_worker_does_no_io(make_config, name, enabled):
    config = make_config(name if enabled else None, {"latitude": 1, "longitude": 2} if name in {"weather", "aqi"} else {})
    event = threading.Event()
    if enabled:
        event.set()
    module = importlib.import_module(MODULES[name])
    with patch.object(runtime, "service_ownership") as ownership, patch.object(requests, "Session") as session:
        assert module.run_worker(config, event) == 0
    ownership.assert_not_called()
    session.assert_not_called()


@pytest.mark.parametrize("name", MODULES)
def test_busy_worker_returns_three_without_startup(make_config, name):
    options = {"latitude": 1, "longitude": 2} if name in {"weather", "aqi"} else {}
    config = make_config(name, options)
    module = importlib.import_module(MODULES[name])
    with runtime.service_ownership(name, config.panel.url, config.runtime.data_dir):
        with patch.object(requests, "Session") as session:
            assert module.run_worker(config, threading.Event()) == 3
        session.assert_not_called()


def test_scoped_paths_restore_after_errors(make_config):
    before = focus_control.default_data_dir()
    config = make_config()
    with pytest.raises(RuntimeError):
        with data_directory(config.runtime.data_dir):
            assert focus_control.default_data_dir() == config.runtime.data_dir
            assert sender_logging.log_folder() == config.runtime.data_dir
            raise RuntimeError()
    assert focus_control.default_data_dir() == before


def test_hub_adapter_translates_typed_config_and_keeps_timezone(make_config, monkeypatch):
    config = make_config("hub", {"companion": {"enabled": True}})
    run = Mock()
    monkeypatch.setattr(hub, "run_hub", run)
    event = threading.Event()
    assert hub.run_worker(config, event) == 0
    typed = run.call_args.args[0]
    assert isinstance(typed, hub.HubConfig)
    assert typed.panel_url == config.panel.url
    assert typed.companion.enabled is True
    assert typed.next_up.timezone_name == config.panel.timezone
    assert run.call_args.args[1] is event
    assert run.call_args.kwargs["timezone_name"] == config.panel.timezone


def test_hub_stops_between_services_and_while_idle():
    event = threading.Event()
    first = Mock()
    first.tick.side_effect = event.set
    second = Mock()
    hub.UtilityHubRunner([first, second], interval_seconds=3600).run_forever(event)
    first.tick.assert_called_once()
    second.tick.assert_not_called()


def test_hub_closes_session_after_controller_startup_failure(monkeypatch):
    panel = Mock()
    monkeypatch.setattr(hub, "configure_logging", Mock())
    monkeypatch.setattr(hub, "build_panel_client", lambda *args, **kw: panel)
    monkeypatch.setattr(hub, "build_controller", Mock(side_effect=RuntimeError("startup")))
    with pytest.raises(RuntimeError):
        hub.run_hub(hub.HubConfig(), threading.Event())
    panel.session.close.assert_called_once()


@pytest.mark.parametrize("name", ["weather", "aqi"])
@pytest.mark.parametrize("failure", [None, "source", "push"])
def test_feed_worker_forwards_settings_wakes_and_closes_session(make_config, monkeypatch, name, failure):
    module = importlib.import_module(MODULES[name])
    config = make_config(name, {"latitude": 1, "longitude": 2, "refresh_seconds": 3600,
                                "request_timeout_seconds": 0.7, "static_only": True})
    event = StopOnWait()
    session = Mock(spec=requests.Session)
    session.headers = {}
    session.get.return_value = Mock(status_code=200)
    session.get.return_value.json.return_value = {"uptimeSeconds": 100, "resetReason": "software"}
    session.put.return_value = Mock(status_code=503 if failure == "push" else 200, text="refused")
    monkeypatch.setattr(module.requests, "Session", lambda: session)
    snapshot = SimpleNamespace(observed_at=datetime.now(timezone.utc), us_aqi=120, pm2_5=20, level=2)
    fetch = Mock(return_value=snapshot, side_effect=requests.Timeout() if failure == "source" else None)
    monkeypatch.setattr(module, "fetch_weather" if name == "weather" else "fetch_air_quality", fetch)
    if name == "weather":
        scene = SimpleNamespace(kind="clear", icon_kind="sun", text="TEST", low_f=50, high_f=70)
        monkeypatch.setattr(module, "choose_scene", lambda value: scene)
    else:
        monkeypatch.setattr(module, "is_relevant", lambda *args, **kwargs: True)
    build = Mock(return_value={"text": "TEST"})
    monkeypatch.setattr(module, "build_payload", build)
    assert module.run_worker(config, event) == 0
    fetch.assert_called_once_with(session, 1, 2, timeout=0.7)
    session.close.assert_called_once()
    assert event.delays[-1] == (3600 if failure is None else 30 if name == "aqi" and failure == "source" else 15)
    if failure != "source":
        build.assert_called_once()
        assert build.call_args.kwargs["animated"] is False
        assert session.put.call_args.args[0].startswith(config.panel.url)
        assert session.put.call_args.kwargs["timeout"] == 0.7


def test_pc_worker_uses_selected_transport_and_keeps_module_defaults(make_config, monkeypatch):
    config = make_config("pc_stats", {"interval_seconds": 1, "request_timeout_seconds": 0.7,
                                      "refresh_delta_points": 4, "refresh_confirm_samples": 3})
    event = StopOnWait(2)
    original = pc.AWTRIX, pc.INTERVAL, pc.REQUEST_TIMEOUT, pc.URL
    monkeypatch.setattr(pc, "psutil", SimpleNamespace(cpu_percent=lambda **kw: 0,
        net_io_counters=lambda: SimpleNamespace(bytes_recv=0), Error=type("ProbeError", (Exception,), {})))
    monkeypatch.setattr(pc, "read_stats", lambda: (95, 95))
    monkeypatch.setattr(pc, "sample_gpu", None)
    get, put, delete = Mock(return_value=Mock(status_code=200)), Mock(return_value=Mock(status_code=200)), Mock(return_value=Mock(status_code=404))
    get.return_value.json.return_value = {"uptimeSeconds": 100, "resetReason": "software"}
    monkeypatch.setattr(pc.requests, "get", get)
    monkeypatch.setattr(pc.requests, "put", put)
    monkeypatch.setattr(pc.requests, "delete", delete)
    assert pc.run_worker(config, event) == 0
    assert event.delays == [0.2, 1, 1]
    assert put.call_count >= 1
    for request in (get, put, delete):
        for call in request.call_args_list:
            assert call.args[0].startswith(config.panel.url)
            assert call.kwargs["timeout"] == 0.7
    assert original == (pc.AWTRIX, pc.INTERVAL, pc.REQUEST_TIMEOUT, pc.URL)


def test_pc_cadence_has_retry_headroom_and_new_delta(make_config):
    settings = pc.PCSettings("http://192.0.2.45", interval_seconds=1, request_timeout_seconds=1)
    assert not pc.keepalive_due(25, settings=settings)
    assert pc.keepalive_due(25)
    assert pc.retry_delay(1, settings=settings) == 1
    document = config_loader.example_document()
    document["services"]["pc_stats"]["options"] = {"interval_seconds": 30}
    with pytest.raises(config_loader.ConfigError, match="headroom"):
        config_loader.config_from_mapping(document, source_path="unused.json")


def test_media_adapter_forwards_config_and_holds_paths(make_config, monkeypatch):
    config = make_config("media", {"bridge_port": 9876, "poll_seconds": 3600, "stale_after_seconds": 9})
    monkeypatch.setattr(media, "MediaManager", object())
    calls = []
    async def bridge(**kwargs):
        calls.append(kwargs)
        assert focus_control.default_data_dir() == config.runtime.data_dir
    monkeypatch.setattr(media, "run_script_bridge", bridge)
    event = threading.Event()
    assert media.run_worker(config, event) == 0
    assert calls == [{"stop_event": event, "panel_url": config.panel.url, "bridge_port": 9876,
                     "poll_seconds": 3600, "stale_after_seconds": 9, "script_path": None}]


def test_media_idle_wait_and_cancel_release_executor_thread():
    async def exercise():
        event = threading.Event()
        task = asyncio.create_task(runtime.async_wait(event, 3600))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert event.is_set()
    asyncio.run(exercise())


@pytest.mark.parametrize("failure", [False, True])
def test_media_owned_server_closes_even_during_install_startup(monkeypatch, failure):
    event = threading.Event()
    server = Mock()
    server.serve_forever.return_value = None
    template = Mock()
    template.name = "media.ax"
    template.read_text.return_value = '# @name Now Playing\n# @version 2.1\nvar URL = "__STATE_URL__"\n'
    monkeypatch.setattr(media, "configure_logging", Mock())
    monkeypatch.setattr(media.socket, "gethostbyname", lambda name: "192.0.2.45")
    monkeypatch.setattr(media, "resolve_lan_ip", lambda ip: "192.0.2.1")
    bind = Mock(return_value=server)
    monkeypatch.setattr(media, "make_server", bind)
    monkeypatch.setattr(media, "remove_legacy_pushed_app", lambda *args: False)
    def install(*args):
        if failure:
            raise ValueError("invalid startup source")
        event.set()
        return False
    monkeypatch.setattr(media, "ensure_script_installed", install)
    coroutine = media.run_script_bridge(stop_event=event, panel_url="http://192.0.2.45",
                                      bridge_port=9876, script_path=template)
    # Use the real Path conversion contract with a temporary patched Path factory.
    monkeypatch.setattr(media, "Path", lambda value: value)
    if failure:
        with pytest.raises(ValueError, match="startup source"):
            asyncio.run(coroutine)
    else:
        asyncio.run(coroutine)
    assert bind.call_args.args[1] == 9876
    server.shutdown.assert_called_once()
    server.server_close.assert_called_once()
    assert not any(thread.name == "awtrix-now-playing-state" for thread in threading.enumerate())


class FakeUser32:
    def __init__(self):
        self.queue = queue.Queue()
        self.registered = threading.Event()
        self.unregistered = []
        self.posts = []
        self.peeked = False

    def PeekMessageW(self, *args):
        self.peeked = True

    def RegisterHotKey(self, _window, number, *_args):
        self.registered.set()
        return True

    def GetMessageW(self, *_args):
        return self.queue.get(timeout=2)

    def PostThreadMessageW(self, owner, message, first, second):
        self.posts.append((owner, message, first, second))
        self.queue.put(0)
        return True

    def UnregisterHotKey(self, _window, number):
        self.unregistered.append(number)


def test_stop_posts_quit_to_owner_and_releases_hotkeys():
    eco = link.DriftEcosystem()
    eco.bus = Mock()
    event = threading.Event()
    user32 = FakeUser32()
    failures = []
    def listen():
        try:
            link.run_hotkey_listener(eco, user32=user32, stop_event=event, thread_id=99)
        except BaseException as error:
            failures.append(error)
    thread = threading.Thread(target=listen)
    thread.start()
    try:
        assert user32.registered.wait(2)
        event.set()
        thread.join(2)
        assert not thread.is_alive()
        assert not failures
        assert user32.peeked
        assert user32.posts == [(99, 0x12, 0, 0)]
        assert len(user32.unregistered) == len(eco.hotkeys)
        eco.bus.stop.assert_called_once_with(timeout=None)
    finally:
        event.set()
        user32.queue.put(0)
        thread.join(2)
        eco.session.close()


def test_event_bus_full_queue_does_not_deadlock_shutdown_or_replay_old_commands():
    bus = DriftEventBus()
    started, release = threading.Event(), threading.Event()
    received = []
    def callback(event):
        received.append(event)
        started.set()
        release.wait(2)
    bus.subscribe(None, callback)
    bus.start()
    bus.publish(DriftEvent(DriftEventType.MACRO_TRIGGERED, "first"))
    assert started.wait(2)
    for _ in range(1000):
        bus.publish(DriftEvent(DriftEventType.MACRO_TRIGGERED, "queued"))
    stopper = threading.Thread(target=lambda: bus.stop(timeout=None))
    stopper.start()
    import time
    deadline = time.monotonic() + 2
    while bus._running and time.monotonic() < deadline:
        threading.Event().wait(0.001)
    assert not bus._running
    release.set()
    stopper.join(2)
    assert not stopper.is_alive()
    assert not bus._worker_thread.is_alive()
    assert len(received) == 1
    assert bus._queue.unfinished_tasks == 0
    assert bus._queue.empty()


def test_event_bus_carries_worker_paths_to_the_callback_thread(tmp_path, monkeypatch):
    monkeypatch.delenv(panel_gate.DIR_ENV, raising=False)
    event = threading.Event()
    received = []
    bus = DriftEventBus()
    def callback(_event):
        received.append(focus_control.default_data_dir())
        with panel_gate.mutation("ecosystem", "push", "test") as record:
            record.observe(SimpleNamespace(status_code=200))
        event.set()
    bus.subscribe(None, callback)
    with data_directory(tmp_path):
        bus.start()
        bus.publish(DriftEvent(DriftEventType.ALERT_BROADCAST, "test"))
        assert event.wait(2)
        bus.stop(timeout=None)
    assert received == [tmp_path]
    assert (tmp_path / panel_gate.EVENTS_NAME).is_file()


def test_stop_signal_handlers_are_cooperative_and_restored():
    event = threading.Event()
    previous = signal.getsignal(signal.SIGTERM)
    with runtime.stop_signals(event):
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)
        assert event.is_set()
    assert signal.getsignal(signal.SIGTERM) == previous


def test_link_adapter_translates_device_flags_and_closes_session(make_config, monkeypatch):
    config = make_config("drift_link", {"matrix_enabled": False, "poll_interval_s": 60,
        "devices": {"pad": {"enabled": True, "vid": "0x1234"}, "watch": {"enabled": False}},
        "macro_mapping": {"9": {"action": "switch_app", "target": "moon", "hotkey_primary": "F20"}}})
    session = Mock(spec=requests.Session)
    monkeypatch.setattr(link.requests, "Session", lambda: session)
    captured = []
    def daemon(eco, event):
        captured.append(eco)
        event.set()
    monkeypatch.setattr(link, "run_daemon", daemon)
    assert link.run_worker(config, threading.Event()) == 0
    [eco] = captured
    assert eco.matrix_url == config.panel.url
    assert not eco.device_enabled("matrix")
    assert eco.devices["pad"]["vid"] == "0x1234"
    assert eco.hotkeys[0].target == "moon"
    session.close.assert_called_once()


def test_daemon_propagates_listener_failure_and_joins(monkeypatch):
    eco = Mock()
    eco.hotkeys = [object()]
    eco.get_ecosystem_status.return_value = {}
    eco.devices = {"matrix": {"poll_interval_s": 1}}
    event = threading.Event()
    monkeypatch.setattr(link, "run_hotkey_listener", Mock(side_effect=RuntimeError("registration failed")))
    with pytest.raises(RuntimeError, match="listener failed"):
        link.run_daemon(eco, event)
    assert event.is_set()
    assert not any(thread.name == "HotkeyListenerThread" for thread in threading.enumerate())


def test_real_local_media_server_releases_socket_on_stop(tmp_path, monkeypatch):
    import socket
    from now_playing_bridge import BridgeStateStore, make_server
    event = threading.Event()
    real_server = make_server("127.0.0.1", 0, BridgeStateStore(), "127.0.0.1")
    address = real_server.server_address
    source = tmp_path / "media.ax"
    source.write_text('var URL = "__STATE_URL__"\n', encoding="utf-8")
    monkeypatch.setattr(media, "configure_logging", Mock())
    monkeypatch.setattr(media.socket, "gethostbyname", lambda name: "127.0.0.1")
    monkeypatch.setattr(media, "resolve_lan_ip", lambda ip: "127.0.0.1")
    monkeypatch.setattr(media, "make_server", lambda *args: real_server)
    monkeypatch.setattr(media, "remove_legacy_pushed_app", lambda *args: False)
    def installed(*args):
        event.set()
        return False
    monkeypatch.setattr(media, "ensure_script_installed", installed)
    asyncio.run(media.run_script_bridge(stop_event=event, panel_url="http://127.0.0.1",
                                       bridge_port=address[1], script_path=source))
    assert not any(thread.name == "awtrix-now-playing-state" for thread in threading.enumerate())
    with socket.socket() as probe:
        probe.bind(address)


def test_second_event_bus_stop_still_waits_for_inflight_handler():
    bus = DriftEventBus()
    started, release = threading.Event(), threading.Event()
    def callback(_event):
        started.set()
        release.wait(2)
    bus.subscribe(None, callback)
    bus.start()
    bus.publish(DriftEvent(DriftEventType.MACRO_TRIGGERED, "test"))
    assert started.wait(2)
    bus.stop(timeout=0)
    release.set()
    bus.stop(timeout=None)
    assert not bus._worker_thread.is_alive()
    assert bus._queue.empty()


def test_standalone_listener_keeps_console_signals_responsive(tmp_path, monkeypatch):
    import sys
    eco = Mock(matrix_url="http://192.0.2.45")
    monkeypatch.setattr(sys, "argv", ["drift_ecosystem.py", "listen"])
    monkeypatch.setattr(link, "DriftEcosystem", lambda *args, **kwargs: eco)
    monkeypatch.setattr(link, "default_data_dir", lambda: tmp_path)
    observed = []
    def listen(_eco, daemon_mode, *, stop_event):
        observed.append(threading.current_thread())
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)
        assert stop_event.is_set()
        assert not daemon_mode
    monkeypatch.setattr(link, "run_hotkey_listener", listen)
    assert link.main() == 0
    assert len(observed) == 1
    assert observed[0] is not threading.main_thread()
    assert not observed[0].is_alive()
    eco.session.close.assert_called_once()


def test_media_thread_start_failure_closes_server_without_waiting_for_serve_forever(tmp_path, monkeypatch):
    source = tmp_path / "media.ax"
    source.write_text('var URL = "__STATE_URL__"\n', encoding="utf-8")
    server = Mock()
    monkeypatch.setattr(media, "configure_logging", Mock())
    monkeypatch.setattr(media.socket, "gethostbyname", lambda name: "127.0.0.1")
    monkeypatch.setattr(media, "resolve_lan_ip", lambda ip: "127.0.0.1")
    monkeypatch.setattr(media, "make_server", lambda *args: server)
    monkeypatch.setattr(threading.Thread, "start", Mock(side_effect=RuntimeError("thread startup")))
    with pytest.raises(RuntimeError, match="thread startup"):
        asyncio.run(media.run_script_bridge(stop_event=threading.Event(), script_path=source))
    server.shutdown.assert_not_called()
    server.server_close.assert_called_once()
