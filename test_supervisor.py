"""Supervisor state, restart, identity, publication, and bounded shutdown tests."""
from collections import deque
import json
import os
from pathlib import Path
import signal
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest

from config_loader import ConfigError, config_from_mapping, example_document
import driftmatrix
import service_runtime as runtime
import supervisor as app


@pytest.fixture(autouse=True)
def actual_scope(monkeypatch):
    monkeypatch.setattr(runtime, "ownership_root", lambda path: Path(path) / "service_locks")


@pytest.fixture
def make_config(tmp_path):
    def make(enabled=("pc_stats",), attempts=2, window=10, timeout=0.15):
        document = example_document()
        document["runtime"].update(data_dir=str(tmp_path / "state"), shutdown_timeout_seconds=timeout,
                                   restart={"max_attempts": attempts, "window_seconds": window})
        for name in enabled:
            document["services"][name]["enabled"] = True
        return config_from_mapping(document, source_path=tmp_path / "config.json")
    return make


class Connection:
    def __init__(self, messages=()):
        self.messages = deque(messages)
        self.closed = False
    def poll(self):
        return bool(self.messages)
    def recv(self):
        return self.messages.popleft()
    def close(self):
        self.closed = True


class Process:
    def __init__(self, stop_event, *, responsive=True, kill_required=False):
        self.pid = 98765
        self.exitcode = None
        self.closed = False
        self.stop_event = stop_event
        self.responsive = responsive
        self.kill_required = kill_required
        self.terminated = self.killed = False
    def is_alive(self):
        if self.stop_event.is_set() and self.responsive and self.exitcode is None:
            self.exitcode = 0
        return self.exitcode is None
    def join(self, timeout=None):
        self.is_alive()
    def terminate(self):
        self.terminated = True
        if not self.kill_required:
            self.exitcode = -15
    def kill(self):
        self.killed = True
        self.exitcode = -9
    def close(self):
        self.closed = True


def fake_child(*, responsive=True, kill_required=False, ready=True):
    event = threading.Event()
    return app.Child(Process(event, responsive=responsive, kill_required=kill_required), event,
                     Connection([{"state": "RUNNING"}] if ready else []), 123.0)


def test_supervisor_lock_is_distinct_but_prevents_duplicate_owner(make_config):
    config = make_config()
    with runtime.supervisor_ownership(config.panel.url, config.runtime.data_dir) as path:
        assert path.name == "supervisor.lock"
        with runtime.service_ownership("pc_stats", config.panel.url, config.runtime.data_dir):
            assert app.Supervisor(config).run() == 3
        with pytest.raises(runtime.ServiceAlreadyRunning):
            with runtime.supervisor_ownership(config.panel.url + "/", config.runtime.data_dir):
                pass
    with runtime.supervisor_ownership(config.panel.url, config.runtime.data_dir):
        pass


def test_startup_acknowledgement_and_disabled_services(make_config):
    child = fake_child(ready=False)
    spawn = Mock(return_value=child)
    supervisor = app.Supervisor(make_config(), spawn=spawn)
    supervisor.step()
    assert supervisor.services["pc_stats"].state == "STARTING"
    assert all(item.state == "DISABLED" for name, item in supervisor.services.items() if name != "pc_stats")
    supervisor.step()
    assert supervisor.services["pc_stats"].state == "STARTING"
    child.connection.messages.append({"state": "RUNNING"})
    supervisor.step()
    assert supervisor.services["pc_stats"].state == "RUNNING"
    assert spawn.call_count == 1
    supervisor.shutdown()
    assert child.stop_event.is_set() and child.process.closed and child.connection.closed


@pytest.mark.parametrize("exit_code", [0, 1, 7, -9])
def test_restarts_are_bounded_and_backed_off(make_config, exit_code):
    now = [0.0]
    children = []
    def spawn(*args):
        child = fake_child()
        children.append(child)
        return child
    supervisor = app.Supervisor(make_config(), spawn=spawn, monotonic=lambda: now[0])
    service = supervisor.services["pc_stats"]
    supervisor.step()
    for index, delay in enumerate((1, 2)):
        children[-1].process.exitcode = exit_code
        supervisor.step()
        assert service.state == "FAILED" and service.restart_at == now[0] + delay
        now[0] += delay - 0.01
        supervisor.step()
        assert len(children) == index + 1
        now[0] += 0.01
        supervisor.step()
        assert len(children) == index + 2
    children[-1].process.exitcode = exit_code
    supervisor.step()
    assert service.circuit_open and service.restart_count == 2
    now[0] = 1000
    supervisor.step()
    assert len(children) == 3  # Manual run is required to reset a tripped circuit.


@pytest.mark.parametrize("exit_code", [2, 3])
def test_unsupported_or_already_owned_worker_does_not_restart(make_config, exit_code):
    child = fake_child()
    supervisor = app.Supervisor(make_config(), spawn=lambda *args: child)
    supervisor.step()
    child.process.exitcode = exit_code
    supervisor.step()
    assert supervisor.services["pc_stats"].circuit_open
    assert supervisor.services["pc_stats"].restart_at is None


def test_restart_window_forgets_old_attempts(make_config):
    supervisor = app.Supervisor(make_config())
    service = supervisor.services["pc_stats"]
    service.attempts.extend((1, 2))
    supervisor.schedule_restart(service, 12)
    assert not service.attempts
    assert service.restart_at == 13 and not service.circuit_open


def test_zero_restart_budget_and_spawn_failure_are_honored(make_config):
    supervisor = app.Supervisor(make_config(attempts=0), spawn=Mock(side_effect=OSError("private")))
    supervisor.step()
    service = supervisor.services["pc_stats"]
    assert service.state == "FAILED" and service.circuit_open
    assert service.last_error == "OSError"
    assert "private" not in json.dumps(supervisor.snapshot())


def test_healthy_process_and_stop_request_do_not_restart(make_config):
    child = fake_child()
    spawn = Mock(return_value=child)
    supervisor = app.Supervisor(make_config(), spawn=spawn)
    for _ in range(5):
        supervisor.step()
    assert spawn.call_count == 1
    supervisor.stop_event.set()
    child.process.exitcode = 1
    supervisor.step()
    assert supervisor.services["pc_stats"].restart_count == 0
    supervisor.shutdown()


@pytest.mark.parametrize("kill_required", [False, True])
def test_shutdown_deadline_escalates_only_owned_children(make_config, kill_required):
    child = fake_child(responsive=False, kill_required=kill_required)
    config = make_config(timeout=0.05)
    supervisor = app.Supervisor(config, spawn=lambda *args: child)
    supervisor.step()
    started = time.monotonic()
    supervisor.shutdown()
    assert time.monotonic() - started < 2
    assert child.stop_event.is_set() and child.process.terminated
    assert child.process.killed == kill_required
    service = supervisor.services["pc_stats"]
    assert service.state == "FAILED" and service.escalated
    assert service.child is None and child.process.closed


def test_supervisor_startup_pre_stopped_creates_no_state(make_config):
    event = threading.Event()
    event.set()
    supervisor = app.Supervisor(make_config(), event)
    assert supervisor.run() == 0
    assert not supervisor.config.runtime.data_dir.exists()


def test_status_before_start_is_read_only(make_config, monkeypatch):
    config = make_config()
    monkeypatch.setattr(socket, "create_connection", Mock(side_effect=AssertionError("No network")))
    assert app.status(config) == {"state": "NOT_RUNNING", "live": False, "services": {}}
    assert not config.runtime.data_dir.exists()


def test_status_uses_creation_time_and_reports_stale_record(make_config, monkeypatch):
    config = make_config()
    supervisor = app.Supervisor(config)
    supervisor.directory.mkdir(parents=True)
    supervisor.state = "RUNNING"
    supervisor.publish()
    monkeypatch.setattr(app, "process_matches", lambda *args: False)
    before = (supervisor.directory / app.STATUS_FILE).read_bytes()
    result = app.status(config)
    assert result["state"] == "STALE" and not result["live"] and not result["healthy"]
    assert result["config_matches"]
    assert (supervisor.directory / app.STATUS_FILE).read_bytes() == before
    assert config.panel.url not in json.dumps(result)


@pytest.mark.parametrize("pid,created", [(0, 1), (True, 1), (1, "bad"), (-1, 0), (None, None)])
def test_invalid_process_identity_is_not_live(pid, created):
    assert not app.process_matches(pid, created)


def test_process_reuse_does_not_match():
    process = psutil.Process()
    assert app.process_matches(process.pid, process.create_time())
    assert not app.process_matches(process.pid, process.create_time() - 1)


@pytest.mark.parametrize("contents", ["null", "[]", "{", '{"pid":1}', '"private"', '{"pid":NaN}', '{"pid":1e999}'])
def test_invalid_status_fails_without_printing_values(make_config, contents):
    config = make_config()
    directory = runtime.scope_directory(config.panel.url, config.runtime.data_dir)
    directory.mkdir(parents=True)
    (directory / app.STATUS_FILE).write_text(contents)
    with pytest.raises(ConfigError) as error:
        app.status(config)
    assert "private" not in str(error.value)


def test_atomic_status_failure_preserves_old_record(tmp_path, monkeypatch):
    path = tmp_path / "status.json"
    app.atomic_json(path, {"old": True})
    monkeypatch.setattr(os, "replace", Mock(side_effect=OSError("publication failed")))
    with pytest.raises(OSError):
        app.atomic_json(path, {"new": True})
    assert json.loads(path.read_text()) == {"old": True}
    assert list(tmp_path.glob("*.tmp")) == []


def test_status_publication_failure_does_not_bypass_shutdown(make_config, monkeypatch):
    child = fake_child(responsive=False)
    supervisor = app.Supervisor(make_config(timeout=0.01), spawn=lambda *args: child)
    supervisor.step()
    monkeypatch.setattr(supervisor, "publish", Mock(side_effect=OSError("disk unavailable")))
    supervisor.shutdown()
    assert child.stop_event.is_set() and child.process.terminated and child.process.closed


def test_control_requires_token_and_matching_instance():
    event = threading.Event()
    server = app.ControlServer(event, "instance")
    server.start()
    try:
        for token, instance in (("bad", "instance"), (server.token, "old"), ("\u2603", "instance")):
            with socket.create_connection(server.socket.getsockname(), timeout=1) as client:
                client.sendall(json.dumps({"command": "stop", "instance": instance, "token": token}).encode() + b"\n")
                assert client.recv(4096) == b""
            assert not event.is_set()
        with socket.create_connection(server.socket.getsockname(), timeout=1) as client:
            client.sendall(json.dumps({"command": "stop", "instance": "instance", "token": server.token}).encode() + b"\n")
            assert app.receive_json(client) == {"accepted": True, "instance": "instance"}
        assert event.is_set()
    finally:
        server.close()


def test_stop_refuses_stale_identity_without_signaling_a_pid(make_config, monkeypatch):
    config = make_config()
    supervisor = app.Supervisor(config)
    supervisor.directory.mkdir(parents=True)
    supervisor.publish()
    app.atomic_json(supervisor.directory / app.CONTROL_FILE,
                    {"instance": supervisor.instance, "pid": supervisor.pid, "process_created": 1, "port": 1234, "token": "secret"})
    connect = Mock(side_effect=AssertionError("Must not connect"))
    monkeypatch.setattr(socket, "create_connection", connect)
    with pytest.raises(ConfigError):
        app.request_stop(config)
    connect.assert_not_called()


@pytest.mark.parametrize("command", ["run", "status", "stop"])
def test_cli_routes_lifecycle_commands_with_selected_config(make_config, monkeypatch, capsys, command):
    config = make_config()
    monkeypatch.setattr(driftmatrix, "load_config", lambda path: config)
    runner = Mock()
    runner.run.return_value = 0
    monkeypatch.setattr(app, "Supervisor", Mock(return_value=runner))
    monkeypatch.setattr(app, "status", Mock(return_value={"state": "NOT_RUNNING"}))
    monkeypatch.setattr(app, "request_stop", Mock(return_value={"state": "STOP_REQUESTED"}))
    assert driftmatrix.main([command, "--config", "chosen.json"]) == 0
    if command == "run":
        runner.run.assert_called_once()
    else:
        assert json.loads(capsys.readouterr().out)["state"] in {"NOT_RUNNING", "STOP_REQUESTED"}


def test_run_publication_error_still_cleans_up_children_and_control(make_config, monkeypatch):
    config = make_config()
    child = fake_child()
    supervisor = app.Supervisor(config, spawn=lambda *args: child)
    publish = supervisor.publish
    calls = []
    def fail_after_start():
        calls.append(True)
        if len(calls) > 1:
            raise OSError("disk failure")
        publish()
    monkeypatch.setattr(supervisor, "publish", fail_after_start)
    previous = signal.getsignal(signal.SIGTERM)
    with pytest.raises(OSError, match="disk failure"):
        supervisor.run()
    assert child.stop_event.is_set() and child.process.closed
    assert not (supervisor.directory / app.CONTROL_FILE).exists()
    assert signal.getsignal(signal.SIGTERM) == previous
    with runtime.supervisor_ownership(config.panel.url, config.runtime.data_dir):
        pass


def test_reserved_config_path_is_never_overwritten(make_config):
    initial = make_config()
    directory = runtime.scope_directory(initial.panel.url, initial.runtime.data_dir)
    directory.mkdir(parents=True)
    path = directory / app.STATUS_FILE
    source = json.dumps(initial.to_mapping())
    path.write_text(source)
    config = config_from_mapping(initial.to_mapping(), source_path=path)
    with pytest.raises(ConfigError, match="reserved"):
        app.Supervisor(config).run()
    assert path.read_text() == source


@pytest.mark.parametrize("failures", [1, 5, 6])
def test_transient_windows_record_contention_has_bounded_retry(tmp_path, monkeypatch, failures):
    path = tmp_path / "record.json"
    replace = os.replace
    attempts = []
    def retry(source, destination):
        attempts.append(True)
        if len(attempts) <= failures:
            raise PermissionError("held by reader")
        replace(source, destination)
    monkeypatch.setattr(os, "replace", retry)
    monkeypatch.setattr(app.time, "sleep", lambda seconds: None)
    if failures == 6:
        with pytest.raises(PermissionError):
            app.atomic_json(path, {"ok": True})
        assert not path.exists()
    else:
        app.atomic_json(path, {"ok": True})
        assert json.loads(path.read_text()) == {"ok": True}
    assert len(attempts) <= 6
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("mode", ["normal", "failure", "parent_exit"])
def test_child_bootstrap_reports_sanitized_results_and_closes_resources(make_config, monkeypatch, mode):
    import logging
    import supervisor_worker as worker
    config = make_config()
    event = threading.Event()
    messages = []
    connection = Mock()
    connection.send.side_effect = messages.append
    connection.poll.return_value = mode == "parent_exit"
    connection.recv.side_effect = EOFError()
    @runtime.worker_adapter("pc_stats")
    def run_worker(config, stop_event):
        if mode == "failure":
            raise ValueError("private-source-value")
        if mode == "parent_exit":
            assert stop_event.wait(2)
        else:
            stop_event.set()
        return 0
    import_module = worker.importlib.import_module
    monkeypatch.setattr(worker.importlib, "import_module", lambda name, *args, **kwargs:
        SimpleNamespace(run_worker=run_worker) if name == "unused" else import_module(name, *args, **kwargs))
    root = logging.getLogger()
    previous_level, previous_handlers = root.level, list(root.handlers)
    try:
        with pytest.raises(SystemExit) as result:
            worker.worker_main(config.to_mapping(), str(config.source_path), "pc_stats", "unused", event, connection)
        assert result.value.code == (1 if mode == "failure" else 0)
    finally:
        root.setLevel(previous_level)
    assert root.handlers == previous_handlers
    assert "private-source-value" not in json.dumps(messages)
    if mode == "failure":
        assert {"error": "ValueError"} in messages
    if mode == "normal":
        assert {"state": "RUNNING"} in messages
    assert (runtime.scope_directory(config.panel.url, config.runtime.data_dir) / "logs" / "service_pc_stats.log").is_file()
    connection.close.assert_called_once()
    assert not any(thread.name == "supervisor-parent-watch" for thread in threading.enumerate())
