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
