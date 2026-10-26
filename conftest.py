"""Shared pytest fixtures for AWTRIX panel tests."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest
from unittest.mock import MagicMock

import panel_gate
import service_runtime
from panel_client import PanelClient
from visual_system import (
    WIDTH,
    HEIGHT,
    BLACK,
    PEARL,
    MINT,
    GOLD,
    AMBER,
    CORAL,
    ICE,
    TRACK,
    LEVEL_COLORS,
    Frame,
    RGB,
    new_frame,
)


# ---------------------------------------------------------------------------
# Panel mutation gate
# ---------------------------------------------------------------------------

_gate_dirs = itertools.count()


@pytest.fixture(autouse=True)
def _service_lock_isolation(tmp_path_factory, monkeypatch):
    """Legacy main() tests must never create ownership files in live state."""
    from hashlib import sha256
    root = tmp_path_factory.getbasetemp() / "service_ownership"
    monkeypatch.setattr(service_runtime, "ownership_root", lambda path:
                        root / sha256(str(Path(path).resolve()).encode()).hexdigest()[:20])


@pytest.fixture(scope="session")
def _panel_gate_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("panel_gate")


@pytest.fixture(scope="session")
def _panel_gate_shared(_panel_gate_root: Path) -> Path:
    directory = _panel_gate_root / "shared"
    directory.mkdir()
    return directory


@pytest.fixture(autouse=True)
def _panel_gate_isolation(_panel_gate_shared: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every test's panel mutations at one session directory, with no gap.

    The gate otherwise locks and journals under %LOCALAPPDATA%\\AWTRIX -- the
    live senders' directory -- and waits 0.4 s between writes. One shared
    directory, because most tests never read the journal and a fresh one each
    cost the suite most of a second; a test that reads it asks for
    ``panel_gate_dir``.
    """
    monkeypatch.setenv(panel_gate.DIR_ENV, str(_panel_gate_shared))
    monkeypatch.setenv(panel_gate.GAP_ENV, "0")
    monkeypatch.delenv(panel_gate.ENABLE_ENV, raising=False)


@pytest.fixture
def panel_gate_dir(_panel_gate_isolation: None, _panel_gate_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty gate directory of the test's own, for a test that reads the journal.

    The gate needs the directory to exist to lock in it; a numbered
    subdirectory of the session folder keeps its files out of ``tmp_path``.
    """
    directory = _panel_gate_root / str(next(_gate_dirs))
    directory.mkdir()
    monkeypatch.setenv(panel_gate.DIR_ENV, str(directory))
    return directory


# ---------------------------------------------------------------------------
# Panel mock
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_device_info() -> dict[str, object]:
    """Return a realistic device info dict matching the AWTRIX NG HTTP API."""
    return {
        "uptimeSeconds": 86400,
        "resetReason": "poweron",
        "wifiSignal": -55,
        "freeHeap": 120_000,
        "version": "0.96",
        "display": "on",
        "brightness": 45,
        "temperature": 28.5,
        "humidity": 42.0,
    }


@pytest.fixture
def mock_panel(sample_device_info: dict[str, object]) -> MagicMock:
    """A ``MagicMock`` standing in for :class:`PanelClient` with sensible defaults.

    - ``get_device_info()`` returns a normal device-state dict.
    - ``get_apps()`` returns an empty dict (no apps installed).
    - ``push_app()`` returns ``True``.
    - ``get_settings()`` returns normal panel settings.
    """
    panel = MagicMock(spec=PanelClient)

    panel.get_device_info.return_value = dict(sample_device_info)
    panel.get_apps.return_value = {}
    panel.push_app.return_value = True
    panel.get_settings.return_value = {
        "brightness": 45,
        "auto_brightness": False,
        "auto_transition": True,
        "transition_speed": 500,
        "color": "#FFFFFF",
    }

    return panel


# ---------------------------------------------------------------------------
# HTTP response factory
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_http_response():
    """Factory fixture that creates mock HTTP response objects.

    Usage::

        def test_something(mock_http_response):
            resp = mock_http_response(200, json_data={"ok": True})
            assert resp.json() == {"ok": True}
    """

    def _make(
        status_code: int = 200,
        json_data: object | None = None,
        text: str = "",
        ok: bool = True,
    ) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status_code
        resp.ok = ok
        resp.text = text
        if json_data is not None:
            resp.json.return_value = json_data
            resp.text = json.dumps(json_data)
        resp.raise_for_status = MagicMock()
        if not ok:
            resp.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
        return resp

    return _make


# ---------------------------------------------------------------------------
# Utility Hub configuration
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_hub_config() -> dict[str, object]:
    """Return a valid utility-hub configuration dict matching the example config.

    This mirrors ``utility_hub.example.json`` so tests can construct a
    :class:`utility_hub.HubConfig` without touching the filesystem.
    """
    return {
        "panel_url": "http://192.168.1.90",
        "quiet_hours": {
            "start": "21:30",
            "end": "07:00",
        },
        "desk_reminder": {
            "enabled": False,
        },
        "network_sentinel": {
            "enabled": True,
            "gateway": None,
            "raw_targets": [
                {"host": "1.1.1.1", "port": 443},
                {"host": "8.8.8.8", "port": 53},
            ],
            "dns_host": "www.msftconnecttest.com",
            "connectivity_url": "http://www.msftconnecttest.com/connecttest.txt",
            "connectivity_expected": "Microsoft Connect Test",
            "probe_interval_seconds": 5,
            "dns_interval_seconds": 15,
            "connectivity_interval_seconds": 30,
            "alert_refresh_seconds": 15,
            "alert_lifetime_seconds": 45,
            "recovery_seconds": 12,
        },
        "focus_sprint": {
            "enabled": True,
            "preset": "classic",
            "motion": "calm",
            "refresh_seconds": 5,
            "static_refresh_seconds": 15,
            "lifetime_seconds": 20,
            "paused_visibility_seconds": 1800,
        },
        "next_up": {
            "enabled": False,
            "sources": [],
            "timezone": "America/Los_Angeles",
            "include_all_day": False,
            "aliases": {
                "Dentist appointment": "DENTIST",
                "Team stand-up": "STANDUP",
            },
        },
        "planes": {
            "enabled": False,
            "radius_nm": 2.5,
            "max_altitude_ft": 12000,
            "include_light_aircraft": False,
        },
        "quakes": {
            "enabled": False,
            "radius_km": 150,
            "min_magnitude": 2.5,
        },
        "game_mode": {
            "enabled": False,
            "extra_games": [],
        },
    }


# ---------------------------------------------------------------------------
# Frame helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def blank_frame() -> Frame:
    """Return a fresh 32×8 frame filled with :data:`BLACK`."""
    return new_frame()


@pytest.fixture
def assert_valid_frame():
    """Return a callable that validates a frame has the correct shape and pixel values.

    Usage::

        def test_render(assert_valid_frame):
            frame = render_something()
            assert_valid_frame(frame)
    """

    def _validate(frame: Frame) -> None:
        assert len(frame) == HEIGHT, f"Frame has {len(frame)} rows, expected {HEIGHT}"
        for y, row in enumerate(frame):
            assert len(row) == WIDTH, f"Row {y} has {len(row)} pixels, expected {WIDTH}"
            for x, pixel in enumerate(row):
                assert isinstance(pixel, tuple) and len(pixel) == 3, (
                    f"Pixel ({x}, {y}) is {pixel!r}, expected an (R, G, B) tuple"
                )
                assert all(0 <= c <= 255 for c in pixel), (
                    f"Pixel ({x}, {y}) = {pixel} has a channel out of 0..255"
                )

    return _validate


@pytest.fixture
def assert_gutter_black():
    """Return a callable that validates column 8 (the gutter) is always BLACK.

    The AWTRIX 32×8 panel uses column 8 as a visual separator between the
    icon area (columns 0–7) and the text/graph area (columns 9–31).

    Usage::

        def test_layout(assert_gutter_black):
            frame = render_something()
            assert_gutter_black(frame)
    """

    def _validate(frame: Frame) -> None:
        for y, row in enumerate(frame):
            assert row[8] == BLACK, (
                f"Gutter pixel at (8, {y}) is {row[8]}, expected BLACK {BLACK}"
            )

    return _validate
