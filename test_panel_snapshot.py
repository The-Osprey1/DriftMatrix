"""Tests for the read-only panel snapshot utility."""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import panel_snapshot as snapshot


class FakeResponse:
    def __init__(self, *, body=None, content: bytes = b"", status: int = 200):
        self._body = body
        self.content = content
        self.status_code = status

    def json(self):
        return self._body

    def raise_for_status(self) -> None:
        if not 200 <= self.status_code < 300:
            raise RuntimeError(f"HTTP {self.status_code}")


def responses(
    *,
    final_uptime: int = 102,
    empty_script: bool = False,
    script_name: str = "moon",
):
    values = []
    for filename in snapshot.JSON_SURFACES:
        if filename == "device.json":
            body = {
                "uptimeSeconds": 100,
                "uid": "device-one",
                "version": "1.1.0",
            }
        elif filename == "apps.json":
            body = [
                {"name": "Time", "present": True, "origin": "builtin"},
                {"name": script_name, "present": True, "origin": "script"},
                {"name": "missing", "present": False, "origin": "script"},
            ]
        else:
            body = {"surface": filename}
        values.append(FakeResponse(body=body))
    values.append(FakeResponse(content=b"" if empty_script else b"return Moon()\n"))
    values.append(FakeResponse(body={"uptimeSeconds": final_uptime, "uid": "device-one"}))
    return values


class PanelSnapshotTests(unittest.TestCase):
    def test_capture_is_complete_byte_exact_and_read_only(self) -> None:
        session = Mock()
        session.get.side_effect = responses()
        with tempfile.TemporaryDirectory() as folder:
            destination = snapshot.capture_snapshot(
                base_url="http://panel",
                output_root=Path(folder),
                session=session,
                now=datetime(2026, 8, 23, 20, 0, tzinfo=timezone.utc),
            )
            manifest = json.loads(
                (destination / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertFalse(destination.name.endswith(".partial"))
            source = b"return Moon()\n"
            self.assertTrue(manifest["complete"])
            self.assertTrue(manifest["readOnlyCapture"])
            self.assertEqual(manifest["uptimeSeconds"], {"before": 100, "after": 102})
            self.assertEqual(set(manifest["exactScriptBackups"]), {"moon"})
            self.assertEqual((destination / "moon.ax").read_bytes(), source)
            self.assertEqual(
                base64.b64decode((destination / "moon.ax.b64").read_bytes()), source
            )
            self.assertEqual(
                manifest["exactScriptBackups"]["moon"]["sha256"],
                hashlib.sha256(source).hexdigest(),
            )
            self.assertEqual(session.put.call_count, 0)
            self.assertEqual(session.post.call_count, 0)
            self.assertEqual(session.patch.call_count, 0)
            self.assertEqual(session.delete.call_count, 0)

    def test_an_unreadable_script_falls_back_to_the_tracked_source(self) -> None:
        """A large script cannot be read back, so the repo supplies the bytes.

        Measured 2026-08-23 with 22,516 bytes in the largest free block, so this
        is a response-size ceiling rather than heap pressure and no maintenance
        window lifts it: nightkeeper (4,185 B) and nowplaying (7,348 B) read
        fine, sunset (9,967 B) and moon (10,574 B) fail. minify() is
        deterministic and was verified to reproduce the installed bytes with an
        exact SHA-256 match for both readable scripts, so the repository is a
        genuine byte-exact rollback source rather than an approximation.
        """
        session = Mock()
        session.get.side_effect = responses(empty_script=True)
        with tempfile.TemporaryDirectory() as folder:
            destination = snapshot.capture_snapshot(
                output_root=Path(folder),
                session=session,
                now=datetime(2026, 8, 23, 20, 1, tzinfo=timezone.utc),
            )
            manifest = json.loads((destination / "manifest.json").read_text("utf-8"))
            entry = manifest["exactScriptBackups"]["moon"]
            self.assertEqual(entry["provenance"], "repository")
            self.assertIn("zero bytes", entry["note"])
            self.assertGreater(entry["bytes"], 0)
            # Derived bytes must match what an install would actually write.
            from weather.minify_ax import minify

            expected = minify(
                (Path(snapshot.__file__).resolve().parent / "weather" / "moon-panel.ax")
                .read_text(encoding="utf-8")
            ).encode("utf-8")
            self.assertEqual(entry["sha256"], hashlib.sha256(expected).hexdigest())

    def test_an_unknown_script_is_a_real_gap_not_a_guess(self) -> None:
        """No repository origin means the snapshot must fail loudly.

        Silently filling a gap with something plausible would turn an
        incomplete rollback set into one that looks complete.
        """
        session = Mock()
        session.get.side_effect = responses(empty_script=True, script_name="mystery")
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(snapshot.SnapshotError, "repository source"):
                snapshot.capture_snapshot(
                    output_root=Path(folder),
                    session=session,
                    now=datetime(2026, 8, 23, 20, 1, tzinfo=timezone.utc),
                )
            partials = list(Path(folder).glob("*.partial"))
            self.assertEqual(len(partials), 1)
            self.assertFalse((partials[0] / "manifest.json").exists())

    def test_uptime_rollback_is_rejected(self) -> None:
        session = Mock()
        session.get.side_effect = responses(final_uptime=4)
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(snapshot.SnapshotError, "rebooted during snapshot"):
                snapshot.capture_snapshot(
                    output_root=Path(folder),
                    session=session,
                    now=datetime(2026, 8, 23, 20, 2, tzinfo=timezone.utc),
                )

    def test_unsafe_script_name_is_rejected_before_path_construction(self) -> None:
        values = responses()
        values[4] = FakeResponse(
            body=[{"name": "../moon", "present": True, "origin": "script"}]
        )
        session = Mock()
        session.get.side_effect = values
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(snapshot.SnapshotError, "unsafe"):
                snapshot.capture_snapshot(
                    output_root=Path(folder),
                    session=session,
                    now=datetime(2026, 8, 23, 20, 3, tzinfo=timezone.utc),
                )


if __name__ == "__main__":
    unittest.main()
