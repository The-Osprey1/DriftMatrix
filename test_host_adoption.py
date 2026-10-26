"""Deployment guard behavior without panel mutations."""
from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest

import config_loader
import now_playing_bridge as bridge
import now_playing_push as media
import panel_snapshot


@pytest.mark.parametrize("status,current", [(200, "old"), (404, "missing")])
def test_verify_only_media_never_installs_changed_or_missing_source(status, current):
    session = Mock()
    session.get.return_value = SimpleNamespace(status_code=status, text=current)
    with pytest.raises(RuntimeError, match="guarded script_swap"):
        bridge.ensure_script_installed("http://192.0.2.10", "nowplaying", "new", session=session, allow_install=False)
    session.put.assert_not_called()


def test_verify_only_media_accepts_identical_source_without_writes():
    session = Mock()
    session.get.return_value = SimpleNamespace(status_code=200, text="same")
    assert not bridge.ensure_script_installed("http://192.0.2.10", "nowplaying", "same", session=session, allow_install=False)
    session.put.assert_not_called()


def test_changed_lan_url_preserves_verify_only_policy():
    install = Mock(side_effect=RuntimeError("guarded script_swap required"))
    old = "http://192.0.2.20:8765/nowplaying"
    assert media.refresh_state_url("192.0.2.10", old, 'var URL = "__STATE_URL__"', Mock(),
        resolve=lambda address: "192.0.2.21", install=install, allow_script_install=False) == old
    assert install.call_args.kwargs == {"allow_install": False}


@pytest.mark.parametrize("flag", ["--awtrix", "--panel"])
def test_snapshot_panel_alias_selects_endpoint_without_capture(monkeypatch, flag):
    monkeypatch.setattr(sys, "argv", ["panel_snapshot.py", flag, "http://192.0.2.10"])
    assert panel_snapshot.parse_args().awtrix == "http://192.0.2.10"


def test_canonical_media_adapter_forwards_verify_only_flag(tmp_path, monkeypatch):
    import threading
    document = config_loader.example_document()
    document["runtime"]["data_dir"] = str(tmp_path)
    document["services"]["media"] = {"enabled": True, "options": {"allow_script_install": False}}
    config = config_loader.config_from_mapping(document, source_path=tmp_path / "config.json")
    monkeypatch.setattr(media, "MediaManager", object())
    calls = []
    async def run(**kwargs):
        calls.append(kwargs)
    monkeypatch.setattr(media, "run_script_bridge", run)
    assert media.run_worker(config, threading.Event()) == 0
    assert calls[0]["allow_script_install"] is False


def test_hidden_worker_prints_reach_rotating_log_and_streams_restore(tmp_path, monkeypatch):
    import logging
    import threading
    import supervisor_worker as worker
    from service_runtime import scope_directory, worker_adapter
    document = config_loader.example_document()
    document["runtime"]["data_dir"] = str(tmp_path)
    document["services"]["pc_stats"]["enabled"] = True
    config = config_loader.config_from_mapping(document, source_path=tmp_path / "config.json")
    @worker_adapter("pc_stats")
    def run_worker(config, event):
        print("hidden worker diagnostic")
        sys.stderr.write("hidden worker warning\n")
        event.set()
        return 0
    import_module = worker.importlib.import_module
    monkeypatch.setattr(worker.importlib, "import_module", lambda name, *args, **kwargs:
        SimpleNamespace(run_worker=run_worker) if name == "hidden_test" else import_module(name, *args, **kwargs))
    connection = Mock()
    connection.poll.return_value = False
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    try:
        with monkeypatch.context() as scope:
            scope.setattr(sys, "stdout", None)
            scope.setattr(sys, "stderr", None)
            with pytest.raises(SystemExit) as result:
                worker.worker_main(config.to_mapping(), str(config.source_path), "pc_stats", "hidden_test", threading.Event(), connection)
            assert result.value.code == 0
            assert sys.stdout is None and sys.stderr is None
    finally:
        root.setLevel(level)
    text = (scope_directory(config.panel.url, config.runtime.data_dir) / "logs" / "service_pc_stats.log").read_text()
    assert "INFO hidden worker diagnostic" in text
    assert "ERROR hidden worker warning" in text
    assert root.handlers == handlers


@pytest.mark.parametrize("failure", [False, True])
def test_aqi_uses_verified_native_context_and_closes_its_session(monkeypatch, failure):
    from datetime import datetime, timezone
    import ssl
    import requests
    from weather import aqi_brief_push as aqi
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    monkeypatch.setitem(sys.modules, "truststore", SimpleNamespace(SSLContext=lambda protocol: context))
    session = Mock(spec=requests.Session)
    session.headers = {}
    monkeypatch.setattr(aqi.requests, "Session", lambda: session)
    snapshot = SimpleNamespace(us_aqi=72, pm2_5=8.4, observed_at=datetime.now(timezone.utc))
    fetch = Mock(return_value=snapshot, side_effect=requests.exceptions.SSLError("certificate failed") if failure else None)
    monkeypatch.setattr(aqi, "fetch_air_quality", fetch)
    monkeypatch.setattr(aqi, "is_relevant", lambda *args, **kwargs: True)
    monkeypatch.setattr(aqi, "build_payload", lambda *args, **kwargs: {"durationMs": 1000})
    if failure:
        with pytest.raises(requests.exceptions.SSLError):
            aqi.run(once=True, dry_run=True)
    else:
        aqi.run(once=True, dry_run=True)
    mounted = session.mount.call_args
    assert mounted.args[0] == "https://"
    assert mounted.args[1].poolmanager.connection_pool_kw["ssl_context"] is context
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    session.close.assert_called_once()
