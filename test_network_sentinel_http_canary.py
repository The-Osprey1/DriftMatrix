"""Real localhost HTTP canary for Network Sentinel's panel transport."""

from __future__ import annotations

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import threading
import unittest

from PIL import Image

import network_sentinel_preview as art
from panel_client import PanelClient, PanelPolicy


class CanaryHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, str, object | None]] = []
    uptimes = [500, 4]

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json(self, body: object, status: int = 200) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> object | None:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length)) if length else None

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        type(self).requests.append(("GET", self.path, None))
        if self.path == "/api/v1/device":
            uptime = type(self).uptimes.pop(0)
            self._json({"uptimeSeconds": uptime, "resetReason": "panic"})
        elif self.path == "/api/v1/apps":
            self._json(
                [
                    {
                        "name": "networksentinel",
                        "present": True,
                        "origin": "pushed",
                    }
                ]
            )
        else:
            self._json({"error": "not found"}, 404)

    def do_PUT(self) -> None:  # noqa: N802 - stdlib handler API
        body = self._body()
        type(self).requests.append(("PUT", self.path, body))
        self._json({"ok": True})

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
        type(self).requests.append(("DELETE", self.path, None))
        self._json({"ok": True})


class NetworkSentinelHTTPCanaryTests(unittest.TestCase):
    def setUp(self) -> None:
        CanaryHandler.requests = []
        CanaryHandler.uptimes = [500, 4]
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CanaryHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_uptime_rollback_selects_static_then_activate_and_delete_are_exact(self) -> None:
        host, port = self.server.server_address
        client = PanelClient(
            f"http://{host}:{port}",
            policy=PanelPolicy(
                minimum_mutation_interval_seconds=0,
                activation_cooldown_seconds=0,
                quiet_start=None,
                quiet_end=None,
            ),
        )
        client.probe_device()
        client.probe_device()
        self.assertTrue(client.static_latched)

        animated = art.build_payload("wan", animated=True)
        static = art.build_payload("wan", animated=False)
        client.push_app("networksentinel", animated, static_payload=static)
        client.activate_app("networksentinel", fast=False)
        self.assertTrue(client.delete_pushed_app("networksentinel"))

        pushed = next(
            body
            for method, path, body in CanaryHandler.requests
            if method == "PUT" and path.endswith("/pushed/networksentinel")
        )
        self.assertIsInstance(pushed, dict)
        with Image.open(BytesIO(base64.b64decode(pushed["icon"], validate=True))) as image:
            self.assertEqual(image.size, (8, 8))
            self.assertEqual(image.n_frames, 1)

        self.assertIn(
            ("PUT", "/api/v1/apps/active", {"name": "networksentinel", "fast": False}),
            CanaryHandler.requests,
        )
        self.assertIn(
            ("DELETE", "/api/v1/apps/networksentinel", None),
            CanaryHandler.requests,
        )


if __name__ == "__main__":
    unittest.main()
