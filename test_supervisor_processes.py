"""Opaque supervisor/worker acceptance using simulated senders and temporary state."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil
import pytest

from config_loader import example_document, load_config
import service_runtime as runtime
import supervisor as app

ROOT = Path(__file__).parent
BOOTSTRAP = '''
import sys
import supervisor, driftmatrix
supervisor.WORKER_MODULES = dict.fromkeys(supervisor.SERVICE_NAMES, "tests_support.supervisor_workers")
raise SystemExit(driftmatrix.main(sys.argv[1:]))
'''


@pytest.fixture(autouse=True)
def real_scope(monkeypatch):
    monkeypatch.setattr(runtime, "ownership_root", lambda path: Path(path) / "service_locks")


def child_environment():
    return dict(os.environ, PYTHONPATH=str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""))


def invoke(command, config):
    return subprocess.run([sys.executable, "-X", "utf8", "-m", "driftmatrix", command, "--config", str(config)],
        cwd=ROOT, env=child_environment(), capture_output=True, text=True, timeout=10)


def wait_for(predicate, *, process=None, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, ValueError, KeyError):
            pass
        if process is not None and process.poll() is not None:
            stdout, stderr = process.communicate(timeout=2)
            pytest.fail(f"Supervisor exited early: {process.returncode} {stdout} {stderr}")
        time.sleep(0.02)
    pytest.fail("Timed out waiting for simulated supervisor lifecycle")


@pytest.fixture
def running(tmp_path):
    processes = []
    configs = []
    locks = []
    def start(mode=1, *, enabled=True, attempts=1, timeout=0.2, occupied=False):
        path = tmp_path / f"config-{len(configs)}.json"
        document = example_document()
        document["runtime"].update(data_dir=str(tmp_path / f"state-{len(configs)}"),
            shutdown_timeout_seconds=timeout, restart={"max_attempts": attempts, "window_seconds": 600})
        document["services"]["pc_stats"] = {"enabled": enabled, "options": {"refresh_confirm_samples": mode}}
        path.write_text(json.dumps(document), encoding="utf-8")
        config = load_config(path)
        directory = runtime.scope_directory(config.panel.url, config.runtime.data_dir)
        if occupied:
            lock = runtime.service_ownership("pc_stats", config.panel.url, config.runtime.data_dir)
            lock.__enter__()
            locks.append(lock)
        process = subprocess.Popen([sys.executable, "-X", "utf8", "-c", BOOTSTRAP, "run", "--config", str(path)],
            cwd=ROOT, env=child_environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(process)
        configs.append(config)
        def snapshot():
            return json.loads((directory / app.STATUS_FILE).read_text(encoding="utf-8"))
        wait_for(lambda: snapshot()["state"] == "RUNNING", process=process)
        return process, config, directory, snapshot
    yield start
    for process, config in zip(processes, configs):
        if process.poll() is None:
            try:
                app.request_stop(config)
                process.wait(timeout=5)
            except Exception:
                process.kill()
        process.communicate(timeout=5)
        # A failed acceptance assertion must not leave its simulated child running.
        marker = config.runtime.data_dir / "test_starts_pc_stats.txt"
        if marker.exists():
            for line in marker.read_text().splitlines():
                try:
                    identity = json.loads(line)
                    child = psutil.Process(identity["pid"])
                    if app.process_matches(identity["pid"], identity["created"]) and "spawn_main" in " ".join(child.cmdline()):
                        child.kill()
                        child.wait(timeout=5)
                except psutil.Error:
                    pass
    for lock in locks:
        lock.__exit__(None, None, None)


def test_real_cli_status_stop_and_duplicate_supervisor(running):
    process, config, directory, snapshot = running()
    wait_for(lambda: snapshot()["services"]["pc_stats"]["state"] == "RUNNING", process=process)
    running_record = snapshot()
    pid = running_record["services"]["pc_stats"]["pid"]
    assert pid != process.pid
    assert app.process_matches(pid, running_record["services"]["pc_stats"]["process_created"])
    with pytest.raises(runtime.ServiceAlreadyRunning):
        with runtime.service_ownership("pc_stats", config.panel.url, config.runtime.data_dir):
            pass
    duplicate = invoke("run", config.source_path)
    assert duplicate.returncode == 3
    status = invoke("status", config.source_path)
    assert status.returncode == 0, status.stderr
    report = json.loads(status.stdout)
    assert report["live"] and report["services"]["pc_stats"]["live"]
    assert "token" not in status.stdout and config.panel.url not in status.stdout
    result = invoke("stop", config.source_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["state"] == "STOP_REQUESTED"
    assert process.wait(timeout=5) == 0
    assert snapshot()["state"] == "STOPPED"
    assert snapshot()["services"]["pc_stats"]["state"] == "STOPPED"
    assert not app.process_matches(pid, running_record["services"]["pc_stats"]["process_created"])
    assert not (directory / app.CONTROL_FILE).exists()
    with runtime.service_ownership("pc_stats", config.panel.url, config.runtime.data_dir):
        pass


@pytest.mark.parametrize("mode", [2, 3, 7])
def test_real_crash_loops_trip_restart_circuit(running, mode):
    process, config, _directory, snapshot = running(mode)
    wait_for(lambda: snapshot()["services"]["pc_stats"]["circuit_open"], process=process)
    record = snapshot()["services"]["pc_stats"]
    assert record["state"] == "FAILED" and record["restart_count"] == 1
    assert record["exit_code"] in {1, 7, 9}
    if mode == 3:
        assert record["last_error"] == "ValueError"
    assert "private-value" not in json.dumps(snapshot())
    assert len((config.runtime.data_dir / "test_starts_pc_stats.txt").read_text().splitlines()) == 2
    app.request_stop(config)
    assert process.wait(timeout=5) == 1


def test_real_transient_worker_crash_recovers_once(running):
    process, config, _directory, snapshot = running(5)
    wait_for(lambda: snapshot()["services"]["pc_stats"]["state"] == "RUNNING"
             and snapshot()["services"]["pc_stats"]["restart_count"] == 1, process=process)
    assert not snapshot()["services"]["pc_stats"]["circuit_open"]
    app.request_stop(config)
    assert process.wait(timeout=5) == 0


def test_real_unresponsive_worker_is_terminated_and_lock_released(running):
    process, config, _directory, snapshot = running(4)
    wait_for(lambda: snapshot()["services"]["pc_stats"]["state"] == "RUNNING", process=process)
    started = time.monotonic()
    app.request_stop(config)
    assert process.wait(timeout=5) == 1
    assert time.monotonic() - started < 4
    assert snapshot()["services"]["pc_stats"]["escalated"]
    assert snapshot()["services"]["pc_stats"]["state"] == "FAILED"
    with runtime.service_ownership("pc_stats", config.panel.url, config.runtime.data_dir):
        pass


@pytest.mark.parametrize("mode", [1, 4])
def test_real_parent_death_stops_owned_worker(running, mode):
    process, config, _directory, snapshot = running(mode)
    wait_for(lambda: snapshot()["services"]["pc_stats"]["state"] == "RUNNING", process=process)
    worker = snapshot()["services"]["pc_stats"]
    process.kill()
    process.wait(timeout=5)
    wait_for(lambda: not app.process_matches(worker["pid"], worker["process_created"]), timeout=5)
    assert app.status(config)["state"] == "STALE"
    with runtime.supervisor_ownership(config.panel.url, config.runtime.data_dir):
        pass
    with runtime.service_ownership("pc_stats", config.panel.url, config.runtime.data_dir):
        pass


def test_real_all_disabled_fleet_has_no_children(running):
    process, config, _directory, snapshot = running(enabled=False)
    assert all(item["state"] == "DISABLED" for item in snapshot()["services"].values())
    assert not psutil.Process(process.pid).children()
    app.request_stop(config)
    assert process.wait(timeout=5) == 0


def test_real_restarts_use_the_validated_snapshot_when_source_changes(running):
    process, config, _directory, snapshot = running(5)
    marker = config.runtime.data_dir / "test_starts_pc_stats.txt"
    wait_for(marker.exists, process=process)
    document = json.loads(config.source_path.read_text())
    document["services"]["pc_stats"]["options"]["refresh_confirm_samples"] = 2
    config.source_path.write_text(json.dumps(document))
    wait_for(lambda: snapshot()["services"]["pc_stats"]["state"] == "RUNNING"
             and snapshot()["services"]["pc_stats"]["restart_count"] == 1, process=process)
    report = json.loads(invoke("status", config.source_path).stdout)
    assert not report["config_matches"]
    assert snapshot()["config_hash"] == config.config_hash
    app.request_stop(config)
    assert process.wait(timeout=5) == 0


def test_real_legacy_owner_blocks_only_that_worker_without_restart(running):
    process, config, _directory, snapshot = running(occupied=True)
    wait_for(lambda: snapshot()["services"]["pc_stats"]["circuit_open"], process=process)
    record = snapshot()["services"]["pc_stats"]
    assert record["exit_code"] == 3 and record["restart_count"] == 0
    assert record["last_error"] == "OwnershipUnavailable"
    assert not (config.runtime.data_dir / "test_starts_pc_stats.txt").exists()
    app.request_stop(config)
    assert process.wait(timeout=5) == 1
