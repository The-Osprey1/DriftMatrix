from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import focus_control as control


BASE_TIME = datetime(2026, 8, 21, 20, 0, 0, 123456, tzinfo=timezone.utc)
ID_ONE = "00000000-0000-4000-8000-000000000001"
ID_TWO = "00000000-0000-4000-8000-000000000002"
ID_THREE = "00000000-0000-4000-8000-000000000003"


def command_mapping(
    *,
    identifier: str = ID_ONE,
    command: str = "pause",
    created_at: str = "2026-08-21T20:00:00.123456Z",
    args: dict | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "id": identifier,
        "command": command,
        "created_at": created_at,
        "args": {} if args is None else args,
    }


def saved_state(**overrides) -> dict:
    value = {
        "schema_version": 1,
        "phase": "focus_running",
        "remaining_seconds": 61,
        "wall_checkpoint": 1_777_000_000.0,
        "monotonic_checkpoint": 1234.5,
        "cycle_count": 2,
        "consumed_command_ids": [ID_ONE],
        "pause_reason": None,
    }
    value.update(overrides)
    return value


class DefaultPathTests(unittest.TestCase):
    def test_default_paths_are_under_local_appdata_awtrix(self) -> None:
        with patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Temp\PrivateProfile"}):
            self.assertEqual(
                control.default_queue_dir(),
                Path(r"C:\Temp\PrivateProfile") / "AWTRIX" / "focus_commands",
            )
            self.assertEqual(
                control.default_state_path(),
                Path(r"C:\Temp\PrivateProfile") / "AWTRIX" / "focus_state.json",
            )

    def test_path_helpers_do_not_create_directories(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "not-created"
            with patch.dict(os.environ, {"LOCALAPPDATA": str(root)}):
                control.default_queue_dir()
                control.default_state_path()
            self.assertFalse(root.exists())


class FocusCommandTests(unittest.TestCase):
    def test_create_start_sanitizes_caps_and_privately_preserves_label(self) -> None:
        secret_prefix = "Quarterly\nPlan\u202e"
        command = control.FocusCommand.create(
            "start",
            minutes=50,
            preset="deep",
            label=secret_prefix + "x" * 300,
            command_id=ID_ONE,
            created_at=BASE_TIME,
        )
        self.assertEqual(command.command, control.CommandType.START)
        self.assertEqual(command.command_id, ID_ONE)
        self.assertEqual(command.kind, control.CommandType.START)
        self.assertEqual(command.minutes, 50)
        self.assertEqual(command.args["preset"], "deep")
        self.assertNotIn("\n", command.label or "")
        self.assertNotIn("\u202e", command.label or "")
        self.assertLessEqual(len(command.label or ""), control.MAX_LABEL_CHARS)
        self.assertNotIn("Quarterly", repr(command))
        self.assertNotIn("Plan", repr(command))
        self.assertIn("label", repr(command))
        self.assertEqual(command.to_mapping()["args"]["label"], command.label)

    def test_add_maps_minutes_to_domain_seconds(self) -> None:
        command = control.FocusCommand.create(
            control.CommandType.ADD,
            minutes=7,
            command_id=ID_ONE,
            created_at=BASE_TIME,
        )
        self.assertEqual(command.amount_seconds, 420.0)
        self.assertEqual(command.args, {"minutes": 7})

    def test_non_add_has_zero_domain_amount(self) -> None:
        command = control.FocusCommand.create(
            "start", minutes=25, command_id=ID_ONE, created_at=BASE_TIME
        )
        self.assertEqual(command.amount_seconds, 0.0)

    def test_command_schema_is_exact_and_typed(self) -> None:
        cases = []
        extra = command_mapping()
        extra["surprise"] = True
        cases.append(extra)
        boolean_version = command_mapping()
        boolean_version["schema_version"] = True
        cases.append(boolean_version)
        boolean_minutes = command_mapping(command="add", args={"minutes": True})
        cases.append(boolean_minutes)
        extra_arg = command_mapping(command="pause", args={"minutes": 1})
        cases.append(extra_arg)
        unknown_preset = command_mapping(
            command="start", args={"preset": "private-custom"}
        )
        cases.append(unknown_preset)
        unsafe_label = command_mapping(command="start", args={"label": "one\ntwo"})
        cases.append(unsafe_label)
        for value in cases:
            with self.subTest(value=value.get("command")):
                with self.assertRaises(control.InvalidCommandError):
                    control.FocusCommand.from_mapping(value)

    def test_uuid_and_utc_timestamp_are_canonical(self) -> None:
        uppercase = command_mapping(
            identifier="AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
        )
        offset_time = command_mapping(created_at="2026-08-21T13:00:00-07:00")
        naive_time = command_mapping(created_at="2026-08-21T20:00:00")
        for value in (uppercase, offset_time, naive_time):
            with self.assertRaises(control.InvalidCommandError):
                control.FocusCommand.from_mapping(value)

    def test_bounds_are_enforced_without_accepting_bool(self) -> None:
        for kind, minutes in (
            ("start", 0),
            ("start", control.MAX_START_MINUTES + 1),
            ("add", 0),
            ("add", control.MAX_ADD_MINUTES + 1),
            ("add", True),
        ):
            with self.subTest(kind=kind, minutes=minutes):
                with self.assertRaises(control.InvalidCommandError):
                    control.FocusCommand.create(
                        kind,
                        minutes=minutes,
                        command_id=ID_ONE,
                        created_at=BASE_TIME,
                    )

    def test_explicit_empty_or_all_control_label_is_invalid(self) -> None:
        for label in ("", "   ", "\r\n\t"):
            with self.subTest(label=label):
                with self.assertRaises(control.InvalidCommandError):
                    control.FocusCommand.create(
                        "start",
                        label=label,
                        command_id=ID_ONE,
                        created_at=BASE_TIME,
                    )


class CommandQueueTests(unittest.TestCase):
    def queue(self, folder: str | Path, identifiers=(ID_ONE, ID_TWO, ID_THREE)):
        values = iter(identifiers)
        return control.FocusCommandQueue(
            folder,
            id_factory=lambda: next(values),
            now=lambda: BASE_TIME,
        )

    def test_enqueue_uses_uuid_filename_complete_json_and_no_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            queue = self.queue(folder)
            receipt = queue.enqueue(
                "start", minutes=25, preset="classic", label="Private plan"
            )
            files = list(Path(folder).iterdir())
            self.assertEqual(files, [Path(folder) / f"{ID_ONE}.json"])
            payload = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(
                set(payload), {"schema_version", "id", "command", "created_at", "args"}
            )
            self.assertEqual(payload["id"], files[0].stem)
            self.assertEqual(payload["command"], "start")
            self.assertTrue(payload["created_at"].endswith("Z"))
            self.assertEqual(payload["args"]["label"], "Private plan")
            self.assertFalse(any(path.suffix == ".tmp" for path in files))
            self.assertNotIn("Private plan", repr(receipt))

    def test_read_orders_by_timestamp_then_id_not_directory_order(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            queue = control.FocusCommandQueue(folder)
            later = control.FocusCommand.create(
                "resume",
                command_id=ID_ONE,
                created_at=BASE_TIME + timedelta(seconds=1),
            )
            tie_high = control.FocusCommand.create(
                "stop", command_id=ID_THREE, created_at=BASE_TIME
            )
            tie_low = control.FocusCommand.create(
                "pause", command_id=ID_TWO, created_at=BASE_TIME
            )
            queue.write(later)
            queue.write(tie_high)
            queue.write(tie_low)
            self.assertEqual(
                [item.id for item in queue.read()], [ID_TWO, ID_THREE, ID_ONE]
            )

    def test_malformed_files_are_quarantined_without_blocking_valid_commands(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            queue = self.queue(root)
            valid = queue.enqueue("pause")
            (root / "not-a-uuid.json").write_text(
                "private label from corrupt data", encoding="utf-8"
            )
            wrong_id = command_mapping(identifier=ID_TWO)
            (root / f"{ID_THREE}.json").write_text(
                json.dumps(wrong_id), encoding="utf-8"
            )
            oversized_id = "00000000-0000-4000-8000-000000000099"
            (root / f"{oversized_id}.json").write_bytes(
                b"{" + b"x" * control.MAX_COMMAND_BYTES
            )
            read = queue.read()
            self.assertEqual([item.id for item in read], [valid.id])
            quarantined = list(
                (root / control.QUARANTINE_DIRECTORY_NAME).glob("*.invalid")
            )
            self.assertEqual(len(quarantined), 3)
            self.assertTrue(all("not-a-uuid" not in item.name for item in quarantined))

    def test_reader_rejects_duplicate_keys_and_nonfinite_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            duplicate_id = ID_ONE
            duplicate = (
                '{"schema_version":1,"id":"%s","id":"%s",'
                '"command":"pause","created_at":"2026-08-21T20:00:00Z","args":{}}'
                % (duplicate_id, duplicate_id)
            )
            (root / f"{duplicate_id}.json").write_text(duplicate, encoding="utf-8")
            nonfinite = command_mapping(identifier=ID_TWO, command="add", args={"minutes": 1})
            raw = json.dumps(nonfinite).replace('"minutes": 1', '"minutes": NaN')
            (root / f"{ID_TWO}.json").write_text(raw, encoding="utf-8")
            self.assertEqual(control.FocusCommandQueue(root).read(), ())
            self.assertEqual(
                len(list((root / control.QUARANTINE_DIRECTORY_NAME).iterdir())), 2
            )

    def test_reader_can_ignore_malformed_files_without_mutating_them(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.json"
            path.write_text("private corrupt content", encoding="utf-8")
            queue = control.FocusCommandQueue(folder, quarantine_malformed=False)
            self.assertEqual(queue.read(), ())
            self.assertTrue(path.exists())
            self.assertFalse((Path(folder) / control.QUARANTINE_DIRECTORY_NAME).exists())

    def test_acknowledge_removes_only_exact_unchanged_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            queue = self.queue(folder)
            receipt = queue.enqueue("pause")
            self.assertTrue(queue.acknowledge(receipt))
            self.assertFalse(queue.ack(receipt))
            self.assertEqual(queue.read(), ())

    def test_constructed_command_cannot_acknowledge_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            queue = self.queue(folder)
            queue.enqueue("pause")
            constructed = control.FocusCommand.create(
                "pause", command_id=ID_ONE, created_at=BASE_TIME
            )
            with self.assertRaises(control.UnsafeAcknowledgementError):
                queue.acknowledge(constructed)
            self.assertTrue((Path(folder) / f"{ID_ONE}.json").exists())

    def test_receipt_from_a_different_queue_cannot_acknowledge(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            receipt = self.queue(first).enqueue("pause")
            with self.assertRaises(control.UnsafeAcknowledgementError):
                control.FocusCommandQueue(second).acknowledge(receipt)
            self.assertTrue((Path(first) / f"{ID_ONE}.json").exists())

    def test_changed_file_fails_closed_during_acknowledgement(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            queue = self.queue(folder)
            receipt = queue.enqueue("pause")
            target = Path(folder) / f"{ID_ONE}.json"
            replacement = command_mapping(identifier=ID_ONE, command="resume")
            target.write_text(json.dumps(replacement), encoding="utf-8")
            with self.assertRaises(control.UnsafeAcknowledgementError):
                queue.acknowledge(receipt)
            self.assertTrue(target.exists())

    def test_duplicate_command_id_does_not_overwrite_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            queue = control.FocusCommandQueue(folder)
            command = control.FocusCommand.create(
                "pause", command_id=ID_ONE, created_at=BASE_TIME
            )
            queue.write(command)
            original = (Path(folder) / f"{ID_ONE}.json").read_bytes()
            with self.assertRaises(control.CommandQueueError):
                queue.write(command)
            self.assertEqual(
                (Path(folder) / f"{ID_ONE}.json").read_bytes(), original
            )

    def test_nonexistent_queue_reads_as_empty_without_creating_it(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "missing" / "focus_commands"
            self.assertEqual(control.FocusCommandQueue(path).read(), ())
            self.assertFalse(path.exists())


class FocusStatusTests(unittest.TestCase):
    def test_missing_state_is_graceful_idle_and_strictly_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "missing" / "focus_state.json"
            status = control.read_focus_status(path)
            self.assertEqual(status.kind, control.StatusKind.IDLE)
            self.assertFalse(status.available)
            self.assertIn("idle", status.summary())
            self.assertFalse(path.parent.exists())

    def test_valid_domain_state_produces_concise_status(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "focus_state.json"
            value = saved_state(private_future_field="ignored safely")
            path.write_text(json.dumps(value), encoding="utf-8")
            status = control.read_focus_status(path)
            self.assertEqual(status.kind, control.StatusKind.WORK)
            self.assertEqual(status.remaining_seconds, 61)
            self.assertEqual(status.summary(), "Focus Sprint: work, 1:01 remaining.")
            self.assertNotIn("ignored safely", repr(status))

    def test_every_domain_phase_maps_to_a_safe_status(self) -> None:
        expected = {
            "idle": control.StatusKind.IDLE,
            "focus_running": control.StatusKind.WORK,
            "focus_paused": control.StatusKind.PAUSED,
            "focus_done_cue": control.StatusKind.COMPLETE,
            "short_break_running": control.StatusKind.REST,
            "long_break_running": control.StatusKind.REST,
            "break_paused": control.StatusKind.PAUSED,
            "ready_next": control.StatusKind.READY,
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "focus_state.json"
            for phase, kind in expected.items():
                with self.subTest(phase=phase):
                    path.write_text(
                        json.dumps(saved_state(phase=phase)), encoding="utf-8"
                    )
                    self.assertEqual(control.read_focus_status(path).kind, kind)

    def test_corrupt_or_wrongly_typed_state_is_rejected(self) -> None:
        cases = [
            b"not-json private label",
            json.dumps(saved_state(remaining_seconds=True)).encode(),
            json.dumps(saved_state(cycle_count=-1)).encode(),
            json.dumps(saved_state(wall_checkpoint=float("nan"))).encode(),
            json.dumps(saved_state(consumed_command_ids=["not-a-uuid"])).encode(),
            json.dumps(saved_state(pause_reason="secret reason")).encode(),
            json.dumps({"schema_version": 1, "phase": "idle"}).encode(),
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "focus_state.json"
            for body in cases:
                with self.subTest(body=body[:20]):
                    path.write_bytes(body)
                    with self.assertRaises(control.InvalidStateError):
                        control.read_focus_status(path)

    def test_oversized_state_is_rejected_without_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "focus_state.json"
            path.write_bytes(b"{" + b"x" * control.MAX_STATE_BYTES)
            with self.assertRaises(control.InvalidStateError):
                control.read_focus_status(path)


class CLITests(unittest.TestCase):
    def queue(self, folder: str | Path):
        return control.FocusCommandQueue(
            folder, id_factory=lambda: ID_ONE, now=lambda: BASE_TIME
        )

    def run_control(self, argv, *, queue=None, state_path=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = control.run_cli(
            argv,
            queue=queue,
            state_path=state_path,
            stdout=stdout,
            stderr=stderr,
        )
        return code, stdout.getvalue(), stderr.getvalue()

    def test_start_cli_preserves_label_but_never_prints_it(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            queue = self.queue(folder)
            secret = "Private\nAcquisition"
            code, stdout, stderr = self.run_control(
                ["start", "45", "--preset", "deep", "--label", secret],
                queue=queue,
            )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(stdout, "Queued Focus Sprint start.\n")
            self.assertNotIn("Private", stdout + stderr)
            command = queue.read()[0]
            self.assertEqual(command.args["minutes"], 45)
            self.assertEqual(command.args["preset"], "deep")
            self.assertEqual(command.args["label"], "Private Acquisition")

    def test_start_defaults_to_classic_preset(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            queue = self.queue(folder)
            code, _, _ = self.run_control(["start"], queue=queue)
            self.assertEqual(code, 0)
            self.assertEqual(queue.read()[0].args, {"preset": "classic"})

    def test_every_simple_command_and_add_are_queued(self) -> None:
        actions = ["pause", "resume", "toggle", "finish", "skip", "stop"]
        for action in actions:
            with self.subTest(action=action), tempfile.TemporaryDirectory() as folder:
                queue = self.queue(folder)
                code, _, stderr = self.run_control([action], queue=queue)
                self.assertEqual(code, 0)
                self.assertEqual(stderr, "")
                self.assertEqual(queue.read()[0].command.value, action)
                self.assertEqual(queue.read()[0].args, {})
        with tempfile.TemporaryDirectory() as folder:
            queue = self.queue(folder)
            code, _, _ = self.run_control(["add", "9"], queue=queue)
            self.assertEqual(code, 0)
            self.assertEqual(queue.read()[0].args, {"minutes": 9})

    def test_invalid_arguments_return_nonzero_without_echoing_input(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            queue = self.queue(folder)
            secret = "private-argument-value"
            code, stdout, stderr = self.run_control(
                ["start", "--preset", secret], queue=queue
            )
            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertNotIn(secret, stderr)
            self.assertEqual(queue.read(), ())

    def test_out_of_range_minutes_return_nonzero_and_do_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            queue = self.queue(folder)
            code, _, _ = self.run_control(["add", "0"], queue=queue)
            self.assertEqual(code, 2)
            self.assertEqual(queue.read(), ())

    def test_status_missing_is_successful_idle_and_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "missing" / "focus_state.json"
            code, stdout, stderr = self.run_control(["status"], state_path=path)
            self.assertEqual(code, 0)
            self.assertIn("idle", stdout)
            self.assertEqual(stderr, "")
            self.assertFalse(path.parent.exists())

    def test_invalid_status_is_nonzero_and_never_dumps_private_data(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "focus_state.json"
            secret = "private sprint label"
            path.write_text('{"label":"' + secret + '"}', encoding="utf-8")
            before = path.read_bytes()
            code, stdout, stderr = self.run_control(["status"], state_path=path)
            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertEqual(stderr, "Focus Sprint status unavailable.\n")
            self.assertNotIn(secret, stdout + stderr)
            self.assertEqual(path.read_bytes(), before)

    def test_valid_status_does_not_require_or_create_a_command_queue(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "focus_state.json"
            queue_path = Path(folder) / "never-create-queue"
            state.write_text(json.dumps(saved_state()), encoding="utf-8")
            queue = control.FocusCommandQueue(queue_path)
            code, stdout, stderr = self.run_control(
                ["status"], queue=queue, state_path=state
            )
            self.assertEqual(code, 0)
            self.assertIn("1:01", stdout)
            self.assertEqual(stderr, "")
            self.assertFalse(queue_path.exists())


class IsolationTests(unittest.TestCase):
    def test_module_has_no_panel_or_network_dependency(self) -> None:
        source = Path(control.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import panel_client", source)
        self.assertNotIn("import requests", source)
        self.assertNotIn("import focus_sprint", source)


if __name__ == "__main__":
    unittest.main()
