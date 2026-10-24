"""Tests for the durable AWTRIX release soak monitor."""

from __future__ import annotations

import unittest
from datetime import datetime

import panel_soak as soak


def base_sample(local: datetime) -> dict:
    expected = soak.desired_state(local)
    return {
        "localTime": local.isoformat(),
        "detailed": True,
        "endpoints": {
            name: {"ok": True, "latencyMs": 10} for name in (*soak.CORE_PATHS, *soak.DETAIL_PATHS)
        },
        "device": {
            "uid": "panel-one",
            "uptimeSeconds": 100,
            "scriptingRunning": True,
            "freeHeapBytes": 30_000,
            "largestFreeBlockBytes": 20_000,
        },
        "display": {"power": expected.power, "brightness": expected.brightness},
        "expectedNightkeeper": {
            "phase": expected.phase,
            "power": expected.power,
            "brightness": expected.brightness,
        },
        "settings": dict(soak.TARGET_SETTINGS),
        "apploop": soak.TARGET_STATE.payload(),
        "apps": [],
        "panelLogs": ["boot baseline"],
        "processes": {name: [{"pid": index + 10}] for index, name in enumerate(soak.AWTRIX_PROCESS_MARKERS)},
    }


class BaselineAbsenceTests(unittest.TestCase):
    """A sender that was already dead must not become its own normal.

    The first sample sets the baseline, whatever it happens to be. A sender
    that was not running when the soak started therefore recorded an empty
    process set as correct, and the comparison against it could never fire --
    so the one thing a soak is meant to catch, a sender that is not there,
    was invisible precisely when it had been missing longest.
    """

    WHEN = datetime(2026, 9, 1, 12, 0)

    def test_a_sender_missing_at_the_start_is_reported(self) -> None:
        state = soak.SoakState()
        sample = base_sample(self.WHEN)
        sample["processes"]["pcstats"] = []
        events = state.ingest(sample)
        keys = [event["key"] for event in events]
        self.assertIn("process-absent:pcstats", keys)

    def test_absence_is_a_warning_not_a_violation(self) -> None:
        """A deliberately retired sender should be visible, not fail the run."""
        state = soak.SoakState()
        sample = base_sample(self.WHEN)
        sample["processes"]["nowplaying"] = []
        state.ingest(sample)
        self.assertEqual(state.violations, [])
        self.assertTrue(any(w["key"] == "process-absent:nowplaying" for w in state.warnings))

    def test_a_running_sender_is_not_reported(self) -> None:
        state = soak.SoakState()
        events = state.ingest(base_sample(self.WHEN))
        self.assertFalse([e for e in events if e["key"].startswith("process-absent")])

    def test_it_is_reported_once_not_on_every_sample(self) -> None:
        state = soak.SoakState()
        first = base_sample(self.WHEN)
        first["processes"]["pcstats"] = []
        state.ingest(first)
        second = base_sample(self.WHEN)
        second["processes"]["pcstats"] = []
        events = state.ingest(second)
        self.assertFalse([e for e in events if e["key"].startswith("process-absent")])

    def test_a_sender_that_dies_during_the_run_fails_after_persistent_absence(self) -> None:
        """A brief restart is tolerated; persistent absence still fails."""
        state = soak.SoakState()
        state.ingest(base_sample(self.WHEN))
        for _ in range(soak.PERSISTENT_FAILURE_SAMPLES - 1):
            later = base_sample(self.WHEN)
            later["processes"]["pcstats"] = []
            state.ingest(later)
        self.assertFalse(any(v["key"] == "process-change:pcstats" for v in state.violations))
        final = base_sample(self.WHEN)
        final["processes"]["pcstats"] = []
        state.ingest(final)
        self.assertTrue(any(v["key"] == "process-change:pcstats" for v in state.violations))

    def test_a_sender_pid_restart_is_a_warning_not_a_violation(self) -> None:
        state = soak.SoakState()
        state.ingest(base_sample(self.WHEN))
        restarted = base_sample(self.WHEN)
        restarted["processes"]["pcstats"] = [{"pid": 9999}]
        events = state.ingest(restarted)
        self.assertFalse(any(v["key"] == "process-change:pcstats" for v in state.violations))
        self.assertTrue(any(w["key"] == "process-restart:pcstats" for w in events))
        self.assertEqual(state.process_restart_counts["pcstats"], 1)

    def test_started_absent_sender_does_not_fail_until_it_has_been_seen(self) -> None:
        state = soak.SoakState()
        first = base_sample(self.WHEN)
        first["processes"]["pcstats"] = []
        state.ingest(first)
        for _ in range(soak.PERSISTENT_FAILURE_SAMPLES + 1):
            absent = base_sample(self.WHEN)
            absent["processes"]["pcstats"] = []
            state.ingest(absent)
        self.assertFalse(any(v["key"] == "process-change:pcstats" for v in state.violations))

    def test_sender_that_returns_can_fail_if_it_later_disappears(self) -> None:
        state = soak.SoakState()
        first = base_sample(self.WHEN)
        first["processes"]["pcstats"] = []
        state.ingest(first)
        running = base_sample(self.WHEN)
        state.ingest(running)
        for _ in range(soak.PERSISTENT_FAILURE_SAMPLES):
            absent = base_sample(self.WHEN)
            absent["processes"]["pcstats"] = []
            state.ingest(absent)
        self.assertTrue(any(v["key"] == "process-change:pcstats" for v in state.violations))

    def test_meridian_is_no_longer_expected(self) -> None:
        """Retired on 2026-09-01; leaving it in would warn on every future run."""
        self.assertNotIn("meridian", soak.AWTRIX_PROCESS_MARKERS)


class CadenceTests(unittest.TestCase):
    def test_dense_windows_cover_both_nightkeeper_transitions(self) -> None:
        for hour, minute in ((20, 55), (21, 30), (6, 55), (7, 30)):
            self.assertEqual(
                soak.sample_interval(datetime(2026, 8, 23, hour, minute)),
                soak.TRANSITION_SAMPLE_SECONDS,
            )
        self.assertEqual(
            soak.sample_interval(datetime(2026, 8, 23, 12, 0)),
            soak.NORMAL_SAMPLE_SECONDS,
        )


class EvaluationTests(unittest.TestCase):
    def test_healthy_sample_establishes_identity_and_passes(self) -> None:
        state = soak.SoakState()
        state.ingest(base_sample(datetime(2026, 8, 23, 20, 0)))
        self.assertEqual(state.expected_uid, "panel-one")
        self.assertEqual(state.violations, [])

    def test_uptime_rollback_is_a_release_failure(self) -> None:
        state = soak.SoakState()
        first = base_sample(datetime(2026, 8, 23, 20, 0))
        second = base_sample(datetime(2026, 8, 23, 20, 1))
        state.ingest(first)
        second["device"]["uptimeSeconds"] = 4
        state.ingest(second)
        self.assertEqual(state.reboot_count, 1)
        self.assertTrue(any(item["key"].startswith("reboot:") for item in state.violations))

    def test_three_nightkeeper_mismatches_fail_but_one_does_not(self) -> None:
        state = soak.SoakState()
        sample = base_sample(datetime(2026, 8, 23, 21, 5))
        sample["display"] = {"power": True, "brightness": 45}
        state.ingest(sample)
        state.ingest(sample)
        self.assertFalse(any(item["key"].startswith("nightkeeper:") for item in state.violations))
        state.ingest(sample)
        self.assertTrue(any(item["key"].startswith("nightkeeper:") for item in state.violations))

    def test_new_manual_hold_log_suppresses_schedule_mismatch(self) -> None:
        state = soak.SoakState()
        initial = base_sample(datetime(2026, 8, 23, 21, 5))
        state.ingest(initial)
        held = base_sample(datetime(2026, 8, 23, 21, 6))
        held["panelLogs"] = ["boot baseline", "21:06 nightkeeper: manual override held until the next transition"]
        held["display"] = {"power": True, "brightness": 45}
        for _ in range(4):
            state.ingest(held)
        self.assertTrue(state.manual_hold)
        self.assertFalse(any(item["key"].startswith("nightkeeper:") for item in state.violations))

    def test_app_error_and_rotation_drift_fail(self) -> None:
        state = soak.SoakState()
        sample = base_sample(datetime(2026, 8, 23, 20, 0))
        sample["apps"] = [{"name": "moon", "error": {"message": "syntax_error"}}]
        sample["apploop"] = {"order": ["Time"], "disabled": []}
        state.ingest(sample)
        keys = {item["key"] for item in state.violations}
        self.assertIn("app-error:moon", keys)
        self.assertIn("rotation-order", keys)

    def test_completed_run_with_endpoint_error_cannot_be_reported_as_pass(self) -> None:
        state = soak.SoakState()
        sample = base_sample(datetime(2026, 8, 23, 20, 0))
        sample["endpoints"]["device"] = {"ok": False, "latencyMs": 5, "error": "ReadTimeout"}
        state.ingest(sample)
        summary = state.summary(
            started_at=datetime(2026, 8, 23, 20, 0),
            ended_at=datetime(2026, 8, 23, 20, 1),
            completed=True,
        )
        self.assertEqual(summary["endpointErrorCount"], 1)
        self.assertEqual(summary["status"], "incomplete")

    def test_device_endpoint_retries_after_a_transient_failure(self) -> None:
        class Response:
            status_code = 200

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"uid": "panel-one"}

        class Session:
            def __init__(self) -> None:
                self.calls = 0

            def get(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise soak.requests.ReadTimeout("temporary")
                return Response()

        session = Session()
        result = soak._get_device_json(session, "http://panel", timeout=0.01)
        self.assertTrue(result["ok"])
        self.assertEqual(session.calls, 2)

    def test_low_contiguous_heap_has_warning_and_failure_bands(self) -> None:
        warning = soak.SoakState()
        sample = base_sample(datetime(2026, 8, 23, 20, 0))
        sample["device"]["largestFreeBlockBytes"] = 14_000
        warning.ingest(sample)
        self.assertTrue(any(item["key"] == "largest-block-warn" for item in warning.warnings))

        failure = soak.SoakState()
        sample["device"]["largestFreeBlockBytes"] = 11_000
        failure.ingest(sample)
        self.assertTrue(any(item["key"] == "largest-block-fail" for item in failure.violations))


if __name__ == "__main__":
    unittest.main()
