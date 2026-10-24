"""Contract tests for the PC-to-AWTRIX Now Playing state bridge.

The bridge intentionally has a very small job: expose the latest Windows media
state to one panel on the LAN and install the device-side renderer only when its
source changes.  These tests keep that transport separate from the legacy
pushed-app loop so visual animation can run locally on the clock.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import FrozenInstanceError, is_dataclass
import http.client
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import ANY, Mock, patch

import now_playing_bridge as bridge
import now_playing_push as app


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeResponse:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")

    def raise_for_status(self) -> None:
        if not 200 <= self.status_code < 300:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, object]:
        return {}


@contextmanager
def running_server(
    store: bridge.BridgeStateStore,
    *,
    panel_ip: str = "127.0.0.1",
):
    """Run one bridge server on an ephemeral loopback port for a test."""
    server = bridge.make_server("127.0.0.1", 0, store, panel_ip)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def request(server, method: str, path: str) -> tuple[int, dict[str, str], bytes]:
    host, port = server.server_address[:2]
    connection = http.client.HTTPConnection(host, port, timeout=2.0)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        body = response.read()
        headers = {key.lower(): value for key, value in response.getheaders()}
        return response.status, headers, body
    finally:
        connection.close()


class BridgeStateTests(unittest.TestCase):
    def test_state_is_an_immutable_value_object_with_safe_defaults(self) -> None:
        state = bridge.BridgeState()

        self.assertTrue(is_dataclass(state))
        self.assertEqual(
            (state.playing, state.identity, state.text, state.progress, state.revision),
            (False, "", "", -1, 0),
        )
        with self.assertRaises(FrozenInstanceError):
            state.text = "STALE"  # type: ignore[misc]

    def test_wire_dictionary_uses_only_the_agreed_compact_keys(self) -> None:
        state = bridge.BridgeState(
            playing=True,
            identity="spotify:midnight-city:m83",
            text="MIDNIGHT CITY - M83",
            progress=12,
            revision=7,
        )

        self.assertEqual(
            state.to_dict(),
            {
                "p": True,
                "i": "spotify:midnight-city:m83",
                "t": "MIDNIGHT CITY - M83",
                "g": 12,
                "r": 7,
            },
        )

    def test_wire_types_remain_native_json_scalars(self) -> None:
        encoded = json.dumps(bridge.BridgeState(playing=True).to_dict())
        decoded = json.loads(encoded)

        self.assertIs(decoded["p"], True)
        self.assertIsInstance(decoded["g"], int)
        self.assertIsInstance(decoded["r"], int)


class SnapshotConversionTests(unittest.TestCase):
    def test_windows_metadata_is_normalized_before_crossing_the_bridge(self) -> None:
        snapshot = app.MediaSnapshot(
            "Beyonc\u00e9 \u2014 Don\u2019t!",
            "M83",
            "SpotifyAB.SpotifyMusic",
            0.5,
        )

        state = app.bridge_state_from_snapshot(snapshot, revision=9)

        self.assertTrue(state.playing)
        self.assertEqual(state.text, "BEYONCE - DON'T! - M83")
        self.assertEqual(state.progress, 12)
        self.assertEqual(state.revision, 9)
        self.assertTrue(state.identity)

    def test_identity_is_stable_but_changes_with_the_media_identity(self) -> None:
        first = app.MediaSnapshot("Midnight City", "M83", "Spotify", 0.1)
        later = app.MediaSnapshot("Midnight City", "M83", "Spotify", 0.8)
        different = app.MediaSnapshot("Wait", "M83", "Spotify", 0.1)

        first_state = app.bridge_state_from_snapshot(first)
        later_state = app.bridge_state_from_snapshot(later)
        different_state = app.bridge_state_from_snapshot(different)

        self.assertEqual(first_state.identity, later_state.identity)
        self.assertNotEqual(first_state.identity, different_state.identity)

    def test_progress_is_a_truthful_24_pixel_bucket_or_unknown(self) -> None:
        cases = (
            (None, -1),
            (float("nan"), -1),
            (-10.0, 0),
            (0.0, 0),
            (0.5, 12),
            (1.0, 24),
            (9.0, 24),
        )

        for ratio, expected in cases:
            with self.subTest(ratio=ratio):
                state = app.bridge_state_from_snapshot(
                    app.MediaSnapshot("Track", progress_ratio=ratio)
                )
                self.assertEqual(state.progress, expected)


class BridgeStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock(100.0)
        self.store = bridge.BridgeStateStore(stale_after=30.0, clock=self.clock)
        self.playing = bridge.BridgeState(
            True,
            "spotify:midnight-city:m83",
            "MIDNIGHT CITY - M83",
            12,
            4,
        )

    def assert_inactive(self, state: bridge.BridgeState) -> None:
        self.assertFalse(state.playing)
        self.assertEqual(state.identity, "")
        self.assertEqual(state.text, "")
        self.assertEqual(state.progress, -1)

    def test_update_publishes_one_complete_state(self) -> None:
        self.store.update(self.playing)
        self.assertEqual(self.store.snapshot(), self.playing)

    def test_clear_immediately_removes_stop_state_and_metadata(self) -> None:
        self.store.update(self.playing)
        self.store.clear()

        self.assert_inactive(self.store.snapshot())

    def test_stale_state_expires_instead_of_freezing_a_track_on_the_panel(self) -> None:
        self.store.update(self.playing)
        self.clock.now = 129.999
        self.assertEqual(self.store.snapshot(), self.playing)

        self.clock.now = 130.0
        self.assert_inactive(self.store.snapshot())

    def test_fresh_inactive_state_does_not_reveal_old_metadata(self) -> None:
        self.store.update(self.playing)
        self.store.update(bridge.BridgeState(revision=5))

        self.assert_inactive(self.store.snapshot())

    def test_concurrent_reads_never_observe_torn_state_fields(self) -> None:
        first = bridge.BridgeState(True, "one", "ONE", 1, 1)
        second = bridge.BridgeState(True, "two", "TWO", 2, 2)
        allowed = {first, second}
        self.store.update(first)
        observed: list[bridge.BridgeState] = []

        def writer() -> None:
            for index in range(2_000):
                self.store.update(first if index % 2 else second)

        def reader() -> None:
            for _ in range(2_000):
                observed.append(self.store.snapshot())

        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2.0)

        self.assertTrue(observed)
        self.assertTrue(all(state in allowed for state in observed))


class RecordingStore(bridge.BridgeStateStore):
    """Real store plus an audit trail for deterministic polling-loop tests."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.updates: list[bridge.BridgeState] = []
        self.clears = 0

    def update(self, state: bridge.BridgeState) -> None:
        self.updates.append(state)
        super().update(state)

    def clear(self) -> None:
        self.clears += 1
        super().clear()


class ScriptBridgeRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_polling_normalizes_media_then_clears_on_shutdown(self) -> None:
        store = RecordingStore()
        stop = threading.Event()
        snapshot = app.MediaSnapshot(
            "Beyonc\u00e9 \u2014 Don\u2019t!",
            "M83",
            "Spotify",
            0.5,
        )

        async def manager():
            return object()

        async def read(_manager):
            return snapshot

        async def stop_after_sample(_delay):
            stop.set()

        with patch.object(app, "configure_logging", return_value=Mock()):
            with patch.object(app, "request_media_manager", manager):
                with patch.object(app, "read_snapshot", read):
                    with patch.object(app.asyncio, "sleep", stop_after_sample):
                        with patch.object(app.requests, "put") as put:
                            with patch.object(app.requests, "delete") as delete:
                                await app.run_script_bridge(
                                    store,
                                    stop_event=stop,
                                    poll_seconds=0,
                                )

        self.assertEqual(len(store.updates), 1)
        active = store.updates[0]
        self.assertTrue(active.playing)
        self.assertEqual(active.text, "BEYONCE - DON'T! - M83")
        self.assertEqual(active.progress, 12)
        self.assertEqual(active.revision, 1)
        self.assertFalse(store.snapshot().playing)
        self.assertEqual(store.clears, 1)
        put.assert_not_called()
        delete.assert_not_called()

    async def test_pause_becomes_inactive_after_grace_without_panel_writes(self) -> None:
        store = RecordingStore()
        stop = threading.Event()
        clock = FakeClock(0.0)
        samples = iter((app.MediaSnapshot("Midnight City", "M83", "Spotify", 0.5),))

        async def manager():
            return object()

        async def read(_manager):
            return next(samples, None)

        async def advance(_delay):
            clock.now += 1.0
            if clock.now >= 5.0:
                stop.set()

        with patch.object(app, "configure_logging", return_value=Mock()):
            with patch.object(app, "request_media_manager", manager):
                with patch.object(app, "read_snapshot", read):
                    with patch.object(app.time, "monotonic", clock):
                        with patch.object(app.asyncio, "sleep", advance):
                            with patch.object(app.requests, "put") as put:
                                with patch.object(app.requests, "delete") as delete:
                                    await app.run_script_bridge(
                                        store,
                                        stop_event=stop,
                                        poll_seconds=0,
                                    )

        self.assertGreaterEqual(len(store.updates), 2)
        self.assertTrue(store.updates[0].playing)
        stopped = store.updates[-1]
        self.assertFalse(stopped.playing)
        self.assertEqual(stopped.text, "")
        self.assertEqual(stopped.progress, -1)
        self.assertGreater(stopped.revision, store.updates[0].revision)
        put.assert_not_called()
        delete.assert_not_called()

    async def test_revision_changes_only_when_visible_semantics_change(self) -> None:
        store = RecordingStore()
        stop = threading.Event()
        snapshots = iter(
            (
                app.MediaSnapshot("Track", "Artist", "Spotify", 0.25),
                app.MediaSnapshot("Track", "Artist", "Spotify", 0.25),
                app.MediaSnapshot("Track", "Artist", "Spotify", 0.5),
            )
        )

        async def manager():
            return object()

        async def read(_manager):
            return next(snapshots)

        async def advance(_delay):
            if len(store.updates) >= 3:
                stop.set()

        with patch.object(app, "configure_logging", return_value=Mock()):
            with patch.object(app, "request_media_manager", manager):
                with patch.object(app, "read_snapshot", read):
                    with patch.object(app.asyncio, "sleep", advance):
                        await app.run_script_bridge(
                            store,
                            stop_event=stop,
                            poll_seconds=0,
                        )

        self.assertEqual([state.progress for state in store.updates], [6, 6, 12])
        self.assertEqual([state.revision for state in store.updates], [1, 1, 2])
        self.assertEqual(len({state.identity for state in store.updates}), 1)


class StateUrlRefreshTests(unittest.TestCase):
    """The panel must not be left polling an address this PC no longer has.

    The URL baked into the Berry script is this machine's LAN address,
    resolved once at startup. A DHCP renewal, an undock, or a VPN changing the
    route all move it, and from that moment the panel polls nothing. Every log
    line stayed a success, because all the observability here is write-side:
    the media loop kept updating the store and reporting track changes while
    the feature was completely dark.
    """

    # A template with no __STATE_URL__ token makes install_source raise, which
    # the helper correctly treats as a failed reinstall -- so the fake has to
    # be a real one or the test measures the error path by accident.
    TEMPLATE = 'self.url = "__STATE_URL__"\n'

    def setUp(self) -> None:
        import logging

        self.logger = logging.getLogger("test.nowplaying.url")
        self.logger.handlers.clear()
        self.messages: list[str] = []
        collected = self.messages

        class Collector(logging.Handler):
            def emit(self, record):
                collected.append(record.getMessage())

        self.logger.addHandler(Collector())
        self.logger.setLevel(logging.WARNING)
        self.logger.propagate = False

    def test_an_unchanged_address_reinstalls_nothing(self) -> None:
        installs: list[tuple] = []
        url = app.state_url_for("192.168.1.133")
        result = app.refresh_state_url(
            "192.168.1.90", url, self.TEMPLATE, self.logger,
            resolve=lambda _ip: "192.168.1.133",
            install=lambda *args: installs.append(args),
        )
        self.assertEqual(result, url)
        self.assertEqual(installs, [])
        self.assertEqual(self.messages, [])

    def test_a_changed_address_reinstalls_and_says_so(self) -> None:
        installs: list[tuple] = []
        old = app.state_url_for("192.168.1.42")
        result = app.refresh_state_url(
            "192.168.1.90", old, self.TEMPLATE, self.logger,
            resolve=lambda _ip: "192.168.1.133",
            install=lambda *args: installs.append(args),
        )
        self.assertEqual(result, app.state_url_for("192.168.1.133"))
        self.assertEqual(len(installs), 1)
        self.assertTrue(any("address changed" in m for m in self.messages))

    def test_a_failed_reinstall_keeps_the_old_url_so_it_retries(self) -> None:
        """Recording the new URL after a failed write would strand it silently."""
        old = app.state_url_for("192.168.1.42")

        def explode(*_args):
            raise RuntimeError("panel offline")

        result = app.refresh_state_url(
            "192.168.1.90", old, self.TEMPLATE, self.logger,
            resolve=lambda _ip: "192.168.1.133",
            install=explode,
        )
        self.assertEqual(result, old)
        self.assertTrue(any("Could not reinstall" in m for m in self.messages))

    def test_an_unresolvable_route_is_reported_and_changes_nothing(self) -> None:
        old = app.state_url_for("192.168.1.42")

        def unreachable(_ip):
            raise OSError("no route to host")

        result = app.refresh_state_url(
            "192.168.1.90", old, self.TEMPLATE, self.logger,
            resolve=unreachable,
            install=lambda *args: self.fail("must not install without an address"),
        )
        self.assertEqual(result, old)
        self.assertTrue(any("re-check" in m for m in self.messages))


class ProductionBridgeStartupTests(unittest.IsolatedAsyncioTestCase):
    TEMPLATE = 'self.url = "__STATE_URL__"\n'
    PANEL_IP = "192.168.1.90"
    LAN_IP = "192.168.1.42"

    @staticmethod
    async def immediate_to_thread(function, *args):
        return function(*args)

    @staticmethod
    def template_path() -> Mock:
        path = Mock()
        path.name = "now_playing.ax"
        path.read_text.return_value = ProductionBridgeStartupTests.TEMPLATE
        return path

    @staticmethod
    def inert_server() -> Mock:
        server = Mock()
        server.serve_forever.return_value = None
        return server

    async def test_route_failure_retries_then_binds_and_installs(self) -> None:
        stop = threading.Event()
        server = self.inert_server()
        events: list[str] = []
        delays: list[float] = []
        route_attempts = 0

        def resolve(_panel_ip: str) -> str:
            nonlocal route_attempts
            route_attempts += 1
            events.append(f"route-{route_attempts}")
            if route_attempts == 1:
                raise OSError("LAN not ready")
            return self.LAN_IP

        def bind(*_args) -> Mock:
            events.append("bind")
            return server

        def inspect_legacy(*_args) -> bool:
            events.append("inspect-origin")
            return False

        def install(_base_url: str, _name: str, source: str) -> bool:
            events.append("install")
            self.assertIn(
                f"http://{self.LAN_IP}:{app.STATE_SERVER_PORT}/nowplaying",
                source,
            )
            return True

        async def manager():
            return object()

        async def read(_manager):
            stop.set()
            return None

        async def advance(delay: float):
            delays.append(delay)

        with patch.object(app, "configure_logging", return_value=Mock()):
            with patch.object(app, "SCRIPT_TEMPLATE_PATH", self.template_path()):
                with patch.object(app.socket, "gethostbyname", return_value=self.PANEL_IP):
                    with patch.object(app, "resolve_lan_ip", resolve):
                        with patch.object(app, "make_server", side_effect=bind) as make_server:
                            with patch.object(app, "remove_legacy_pushed_app", inspect_legacy):
                                with patch.object(
                                    app,
                                    "ensure_script_installed",
                                    side_effect=install,
                                ) as installer:
                                    with patch.object(app, "request_media_manager", manager):
                                        with patch.object(app, "read_snapshot", read):
                                            with patch.object(app.asyncio, "sleep", advance):
                                                with patch.object(
                                                    app.asyncio,
                                                    "to_thread",
                                                    self.immediate_to_thread,
                                                ):
                                                    with patch.object(app.requests, "delete") as delete:
                                                        await app.run_script_bridge(
                                                            stop_event=stop,
                                                            poll_seconds=0,
                                                        )

        self.assertEqual(route_attempts, 2)
        self.assertEqual(events[:5], ["route-1", "route-2", "bind", "inspect-origin", "install"])
        self.assertEqual(delays[0], app.network_retry_delay(1))
        make_server.assert_called_once_with(
            "0.0.0.0",
            app.STATE_SERVER_PORT,
            ANY,
            self.PANEL_IP,
        )
        installer.assert_called_once()
        server.shutdown.assert_called_once_with()
        server.server_close.assert_called_once_with()
        delete.assert_not_called()

    async def test_install_failure_retries_without_rebinding(self) -> None:
        stop = threading.Event()
        server = self.inert_server()
        delays: list[float] = []
        install_attempts = 0

        def install(*_args) -> bool:
            nonlocal install_attempts
            install_attempts += 1
            if install_attempts == 1:
                raise RuntimeError("panel rebooting")
            return False

        async def manager():
            return object()

        async def read(_manager):
            stop.set()
            return None

        async def advance(delay: float):
            delays.append(delay)

        with patch.object(app, "configure_logging", return_value=Mock()):
            with patch.object(app, "SCRIPT_TEMPLATE_PATH", self.template_path()):
                with patch.object(app.socket, "gethostbyname", return_value=self.PANEL_IP):
                    with patch.object(app, "resolve_lan_ip", return_value=self.LAN_IP):
                        with patch.object(app, "make_server", return_value=server) as make_server:
                            with patch.object(
                                app,
                                "remove_legacy_pushed_app",
                                return_value=False,
                            ) as inspect_origin:
                                with patch.object(app, "ensure_script_installed", install):
                                    with patch.object(app, "request_media_manager", manager):
                                        with patch.object(app, "read_snapshot", read):
                                            with patch.object(app.asyncio, "sleep", advance):
                                                with patch.object(
                                                    app.asyncio,
                                                    "to_thread",
                                                    self.immediate_to_thread,
                                                ):
                                                    await app.run_script_bridge(
                                                        stop_event=stop,
                                                        poll_seconds=0,
                                                    )

        self.assertEqual(install_attempts, 2)
        self.assertEqual(delays[0], app.network_retry_delay(1))
        make_server.assert_called_once()
        self.assertEqual(inspect_origin.call_count, 2)

    async def test_existing_script_survives_full_startup_handoff(self) -> None:
        stop = threading.Event()
        server = self.inert_server()
        rendered = bridge.render_script_source(
            self.TEMPLATE,
            f"http://{self.LAN_IP}:{app.STATE_SERVER_PORT}/nowplaying",
        )
        inventory = FakeResponse(200)
        inventory.json = Mock(
            return_value=[
                {
                    "name": app.APP_NAME,
                    "present": True,
                    "origin": "script",
                    "slot": 3,
                }
            ]
        )

        def get(url: str, **_kwargs) -> FakeResponse:
            if url.endswith("/api/v1/apps"):
                return inventory
            if url.endswith(f"/api/v1/apps/script/{app.APP_NAME}"):
                return FakeResponse(200, rendered)
            raise AssertionError(f"unexpected GET {url}")

        async def manager():
            return object()

        async def read(_manager):
            stop.set()
            return None

        async def no_wait(_delay):
            return None

        with patch.object(app, "configure_logging", return_value=Mock()):
            with patch.object(app, "SCRIPT_TEMPLATE_PATH", self.template_path()):
                with patch.object(app.socket, "gethostbyname", return_value=self.PANEL_IP):
                    with patch.object(app, "resolve_lan_ip", return_value=self.LAN_IP):
                        with patch.object(app, "make_server", return_value=server):
                            with patch.object(app.requests, "get", side_effect=get) as http_get:
                                with patch.object(app.requests, "put") as http_put:
                                    with patch.object(app.requests, "delete") as http_delete:
                                        with patch.object(app, "request_media_manager", manager):
                                            with patch.object(app, "read_snapshot", read):
                                                with patch.object(app.asyncio, "sleep", no_wait):
                                                    with patch.object(
                                                        app.asyncio,
                                                        "to_thread",
                                                        self.immediate_to_thread,
                                                    ):
                                                        await app.run_script_bridge(
                                                            stop_event=stop,
                                                            poll_seconds=0,
                                                        )

        self.assertEqual(http_get.call_count, 2)
        http_put.assert_not_called()
        http_delete.assert_not_called()


class StateServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = bridge.BridgeState(
            True,
            "spotify:midnight-city:m83",
            "MIDNIGHT CITY - M83",
            12,
            7,
        )
        self.store = bridge.BridgeStateStore()
        self.store.update(self.state)

    def test_allowed_panel_gets_compact_uncached_json(self) -> None:
        with running_server(self.store) as server:
            status, headers, body = request(server, "GET", "/nowplaying")

        self.assertEqual(status, 200)
        self.assertEqual(headers.get("content-type"), "application/json")
        self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertEqual(
            body,
            json.dumps(self.state.to_dict(), separators=(",", ":")).encode("utf-8"),
        )

    def test_only_the_nowplaying_route_is_exposed(self) -> None:
        with running_server(self.store) as server:
            root_status, _, root_body = request(server, "GET", "/")
            post_status, _, post_body = request(server, "POST", "/nowplaying")

        self.assertEqual(root_status, 404)
        self.assertIn(post_status, (404, 405, 501))
        self.assertNotIn(self.state.text.encode(), root_body + post_body)

    def test_non_panel_client_is_forbidden_without_leaking_state(self) -> None:
        with running_server(self.store, panel_ip="192.0.2.10") as server:
            status, _, body = request(server, "GET", "/nowplaying")

        self.assertEqual(status, 403)
        self.assertNotIn(self.state.text.encode(), body)

    def test_server_binds_only_to_the_requested_interface(self) -> None:
        with running_server(self.store) as server:
            self.assertEqual(server.server_address[0], "127.0.0.1")

    def test_stale_store_is_serialized_as_inactive(self) -> None:
        clock = FakeClock(0.0)
        store = bridge.BridgeStateStore(stale_after=1.0, clock=clock)
        store.update(self.state)
        clock.now = 2.0

        with running_server(store) as server:
            status, _, body = request(server, "GET", "/nowplaying")

        self.assertEqual(status, 200)
        decoded = json.loads(body)
        self.assertIs(decoded["p"], False)
        self.assertEqual(decoded["i"], "")
        self.assertEqual(decoded["t"], "")
        self.assertEqual(decoded["g"], -1)


class LanResolverTests(unittest.TestCase):
    def test_resolver_uses_the_route_to_the_panel_not_hostname_guessing(self) -> None:
        udp_socket = Mock()
        udp_socket.getsockname.return_value = ("192.168.1.42", 53123)

        with patch.object(bridge.socket, "socket", return_value=udp_socket) as socket_factory:
            result = bridge.resolve_lan_ip("192.168.1.90")

        self.assertEqual(result, "192.168.1.42")
        socket_factory.assert_called_once_with(bridge.socket.AF_INET, bridge.socket.SOCK_DGRAM)
        connect_target = udp_socket.connect.call_args.args[0]
        self.assertEqual(connect_target[0], "192.168.1.90")
        udp_socket.close.assert_called_once_with()

    def test_resolver_rejects_a_wildcard_result(self) -> None:
        udp_socket = Mock()
        udp_socket.getsockname.return_value = ("0.0.0.0", 0)

        with patch.object(bridge.socket, "socket", return_value=udp_socket):
            with self.assertRaises((OSError, RuntimeError, ValueError)):
                bridge.resolve_lan_ip("192.168.1.90")

        udp_socket.close.assert_called_once_with()

    def test_socket_is_closed_when_route_discovery_fails(self) -> None:
        udp_socket = Mock()
        udp_socket.connect.side_effect = OSError("no route")

        with patch.object(bridge.socket, "socket", return_value=udp_socket):
            with self.assertRaises(OSError):
                bridge.resolve_lan_ip("192.168.1.90")

        udp_socket.close.assert_called_once_with()


class InstallSizeTests(unittest.TestCase):
    """The real script has to fit in the block the panel can actually offer.

    A Berry install needs roughly twice its source contiguous. At 10.7KB raw
    that is ~21KB, more than this panel has even shortly after a reboot, and on
    2026-08-22 it cost the media bridge 1h51m of startup: it retried every 32
    seconds from 11:35 until 13:26, publishing no media state until it landed.
    """

    def setUp(self) -> None:
        self.template = (
            Path(__file__).with_name("now_playing.ax").read_text(encoding="utf-8")
        )
        self.state_url = "http://192.168.1.133:8765/nowplaying"

    def test_minifying_brings_the_install_inside_a_realistic_free_block(self) -> None:
        raw = len(bridge.render_script_source(self.template, self.state_url).encode())
        installed = len(bridge.install_source(self.template, self.state_url).encode())
        self.assertLess(installed, raw * 0.75, "minification is not earning its place")
        # A freshly rebooted panel has offered about 19-20KB of contiguous block.
        self.assertLess(installed * 2, 19_000, "still too large to install reliably")

    def test_identity_and_state_url_survive_minification(self) -> None:
        installed = bridge.install_source(self.template, self.state_url)
        self.assertIn(self.state_url, installed)
        self.assertIn("@version", installed)
        self.assertIn("@name", installed)
        self.assertNotIn(bridge.SCRIPT_PLACEHOLDER, installed)

    def test_minification_keeps_the_berry_block_structure(self) -> None:
        # Berry is delimited by `end`, not by indentation, so stripping leading
        # whitespace is safe -- but the block count must still balance.
        installed = bridge.install_source(self.template, self.state_url)
        opens = sum(
            len([w for w in line.split() if w in ("def", "class", "if", "while", "for")])
            for line in installed.splitlines()
        )
        self.assertGreater(opens, 0)
        self.assertGreater(len([l for l in installed.splitlines() if l.strip() == "end"]), 0)


class ScriptSourceTests(unittest.TestCase):
    def test_renderer_replaces_the_single_state_url_placeholder(self) -> None:
        template = 'class NowPlaying\n  def init()\n    self.url = "__STATE_URL__"\n  end\nend\n'
        state_url = "http://192.168.1.42:8765/nowplaying"

        installed = bridge.install_source(template, state_url)
        self.assertIn(state_url, installed)
        self.assertNotIn(bridge.SCRIPT_PLACEHOLDER, installed)

        rendered = bridge.render_script_source(template, state_url)

        self.assertIn(f'self.url = "{state_url}"', rendered)
        self.assertNotIn("__STATE_URL__", rendered)
        self.assertEqual(rendered.count(state_url), 1)

    def test_renderer_rejects_a_template_without_the_contract_placeholder(self) -> None:
        with self.assertRaises(ValueError):
            bridge.render_script_source("class NowPlaying\nend\n", "http://host/nowplaying")

    def test_renderer_rejects_multiple_url_placeholders(self) -> None:
        with self.assertRaises(ValueError):
            bridge.render_script_source(
                "__STATE_URL__ then __STATE_URL__",
                "http://host/nowplaying",
            )


class ScriptInstallTests(unittest.TestCase):
    BASE_URL = "http://192.168.1.90"
    SCRIPT_NAME = "nowplaying"
    SOURCE = 'self.url = "http://192.168.1.42:8765/nowplaying"\n'

    def make_session(self, status: int, current_source: str = "") -> Mock:
        session = Mock()
        session.get.return_value = FakeResponse(status, current_source)
        session.put.return_value = FakeResponse(200)
        return session

    def install(self, session: Mock):
        return bridge.ensure_script_installed(
            self.BASE_URL,
            self.SCRIPT_NAME,
            self.SOURCE,
            session=session,
        )

    def assert_script_endpoint_only(self, session: Mock) -> None:
        for call in session.mock_calls:
            for argument in call.args:
                if isinstance(argument, str) and argument.startswith("http"):
                    self.assertIn("/api/v1/apps/script/", argument)
                    self.assertNotIn("/api/v1/apps/pushed/", argument)

    def test_identical_remote_source_is_not_reinstalled(self) -> None:
        session = self.make_session(200, self.SOURCE)

        self.install(session)

        session.get.assert_called_once()
        session.put.assert_not_called()
        self.assert_script_endpoint_only(session)

    def test_changed_remote_source_is_installed_once(self) -> None:
        session = self.make_session(200, "old source\n")

        self.install(session)

        session.put.assert_called_once()
        put_call = session.put.call_args
        self.assertIn("/api/v1/apps/script/nowplaying", put_call.args[0])
        submitted = put_call.kwargs.get("data")
        if isinstance(submitted, bytes):
            submitted = submitted.decode("utf-8")
        self.assertEqual(submitted, self.SOURCE)
        self.assert_script_endpoint_only(session)

    def test_missing_remote_script_is_installed_once(self) -> None:
        session = self.make_session(404)

        self.install(session)

        session.put.assert_called_once()
        self.assert_script_endpoint_only(session)


class LegacyHandoffTests(unittest.TestCase):
    BASE_URL = "http://192.168.1.90"

    @staticmethod
    def session_with(entry: dict[str, object]) -> Mock:
        session = Mock()
        inventory = FakeResponse(200)
        inventory.json = Mock(return_value=[entry])
        session.get.return_value = inventory
        session.delete.return_value = FakeResponse(200)
        return session

    def test_exact_pushed_app_is_removed_before_script_install(self) -> None:
        session = self.session_with(
            {"name": "nowplaying", "present": True, "origin": "pushed", "slot": 3}
        )

        changed = bridge.remove_legacy_pushed_app(
            self.BASE_URL,
            "nowplaying",
            session=session,
        )

        self.assertTrue(changed)
        session.delete.assert_called_once_with(
            f"{self.BASE_URL}/api/v1/apps/nowplaying",
            timeout=10.0,
        )

    def test_existing_script_is_never_deleted_by_the_generic_endpoint(self) -> None:
        session = self.session_with(
            {"name": "nowplaying", "present": True, "origin": "script", "slot": 3}
        )

        changed = bridge.remove_legacy_pushed_app(
            self.BASE_URL,
            "nowplaying",
            session=session,
        )

        self.assertFalse(changed)
        session.delete.assert_not_called()

    def test_absent_order_placeholder_is_preserved(self) -> None:
        session = self.session_with(
            {"name": "nowplaying", "present": False, "origin": None, "slot": 3}
        )

        changed = bridge.remove_legacy_pushed_app(
            self.BASE_URL,
            "nowplaying",
            session=session,
        )

        self.assertFalse(changed)
        session.delete.assert_not_called()

    def test_unexpected_origin_is_refused_instead_of_deleted(self) -> None:
        session = self.session_with(
            {"name": "nowplaying", "present": True, "origin": "builtin", "slot": 3}
        )

        with self.assertRaises(RuntimeError):
            bridge.remove_legacy_pushed_app(
                self.BASE_URL,
                "nowplaying",
                session=session,
            )

        session.delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
