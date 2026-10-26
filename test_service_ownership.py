"""Real process/file ownership tests, confined to temporary directories."""
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

import focus_control
import service_runtime as runtime


@pytest.fixture
def actual_locks(tmp_path, monkeypatch):
    # Bypass the suite's private-state protection only for this temporary root.
    monkeypatch.setattr(runtime, "ownership_root", lambda path: Path(path) / "service_locks")
    return tmp_path


def test_equivalent_endpoints_contend_and_lock_file_survives_release(actual_locks):
    with runtime.service_ownership("media", "HTTP://Example.TEST:80/", actual_locks) as path:
        with pytest.raises(runtime.ServiceAlreadyRunning):
            with runtime.service_ownership("media", "http://example.test", actual_locks):
                pass
        assert path.name == "service_media.lock"
    assert path.exists()
    with runtime.service_ownership("media", "http://example.test", actual_locks) as reopened:
        assert reopened == path


def test_ownership_is_scoped_by_service_endpoint_and_data_dir(actual_locks):
    with runtime.service_ownership("media", "http://192.0.2.1", actual_locks) as first:
        with runtime.service_ownership("weather", "http://192.0.2.1", actual_locks) as service:
            with runtime.service_ownership("media", "http://192.0.2.2", actual_locks) as endpoint:
                with runtime.service_ownership("media", "http://192.0.2.1", actual_locks / "other") as directory:
                    assert len({first, service, endpoint, directory}) == 4


def test_exception_releases_ownership_and_path_context(actual_locks):
    event = threading.Event()
    before = focus_control.default_data_dir()
    def fail():
        assert focus_control.default_data_dir() == actual_locks
        raise ValueError("worker failed")
    with pytest.raises(ValueError):
        runtime.run_owned("hub", "http://192.0.2.1", actual_locks, event, fail)
    assert focus_control.default_data_dir() == before
    with runtime.service_ownership("hub", "http://192.0.2.1", actual_locks):
        pass


def child_environment():
    root = str(Path(__file__).parent)
    return dict(os.environ, PYTHONPATH=root + os.pathsep + os.environ.get("PYTHONPATH", ""))


def test_separate_process_cannot_duplicate_an_owned_sender(actual_locks):
    code = '''
from pathlib import Path
import sys, threading
from service_runtime import run_owned
status = run_owned("media", "http://192.0.2.1", Path(sys.argv[1]), threading.Event(), lambda: 99)
raise SystemExit(status)
'''
    with runtime.service_ownership("media", "http://192.0.2.1", actual_locks):
        completed = subprocess.run([sys.executable, "-X", "utf8", "-c", code, str(actual_locks)],
                                   env=child_environment(), capture_output=True, text=True, timeout=15)
        assert completed.returncode == 3, completed.stderr


def test_kernel_releases_ownership_when_process_exits(actual_locks):
    ready = actual_locks / "ready.txt"
    code = '''
from pathlib import Path
import sys, time
from service_runtime import service_ownership
root=Path(sys.argv[1])
with service_ownership("media", "http://192.0.2.1", root):
    (root / "ready.txt").write_text("owned")
    time.sleep(60)
'''
    process = subprocess.Popen([sys.executable, "-X", "utf8", "-c", code, str(actual_locks)],
                               env=child_environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        with pytest.raises(runtime.ServiceAlreadyRunning):
            with runtime.service_ownership("media", "http://192.0.2.1", actual_locks):
                pass
        process.kill()
        process.wait(timeout=5)
        with runtime.service_ownership("media", "http://192.0.2.1", actual_locks):
            pass
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_cooperative_wait_wakes_without_waiting_for_interval():
    event = threading.Event()
    entered = threading.Event()
    def wait():
        entered.set()
        runtime.wait(event, 3600)
    thread = threading.Thread(target=wait)
    thread.start()
    try:
        assert entered.wait(1)
        event.set()
        thread.join(1)
        assert not thread.is_alive()
    finally:
        event.set()
        thread.join(1)


def test_legacy_entry_points_use_same_ownership_runner(monkeypatch):
    import now_playing_push as media
    import pc_stats_push as pc
    import utility_hub as hub
    from weather import aqi_brief_push as aqi
    from weather import weather_brief_push as weather
    from unittest.mock import Mock
    common = Mock(return_value=3)
    monkeypatch.setattr(hub, "parse_args", lambda: type("Args", (), {"config": None, "once": False})())
    monkeypatch.setattr(hub, "load_config", lambda path: hub.HubConfig())
    monkeypatch.setattr(hub, "run_owned", common)
    assert hub.main() == 3
    assert common.call_args.args[0] == "hub"
    monkeypatch.setattr(pc, "run_owned", common)
    assert pc.main() == 3
    assert common.call_args.args[0] == "pc_stats"
    monkeypatch.setattr(media, "MediaManager", object())
    monkeypatch.setattr(media, "run_owned", common)
    assert media.main() == 3
    assert common.call_args.args[0] == "media"
    for module, name in ((weather, "weather"), (aqi, "aqi")):
        monkeypatch.setattr(module, "parse_args", lambda: type("Args", (), {"awtrix": "http://192.0.2.1"})())
        monkeypatch.setattr(module, "run_owned", common)
        assert module.main() == 3
        assert common.call_args.args[0] == name
