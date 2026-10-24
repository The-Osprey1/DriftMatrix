"""A killed soak must not go on claiming it is watching the panel.

Three real runs -- 2026-08-23, 2026-08-29 and 2026-08-30 -- were still
recording ``"status": "running"`` days after the machine that hosted them had
slept or logged off. That is worse than recording nothing: a stale "running"
reads as a monitor still on duty, so nobody goes looking for the gap.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from panel_soak import DETAIL_SAMPLE_SECONDS, STALE_HEARTBEATS, read_run_status

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def _run(status="running", last=None, heartbeat=DETAIL_SAMPLE_SECONDS, started=None):
    run = {"status": status, "heartbeatSeconds": heartbeat}
    if last is not None:
        run["lastSampleAt"] = last.isoformat()
    if started is not None:
        run["startedAt"] = started.isoformat()
    return run


class RunStatusTests(unittest.TestCase):
    def test_a_fresh_heartbeat_is_still_running(self) -> None:
        run = _run(last=NOW - timedelta(seconds=DETAIL_SAMPLE_SECONDS * 0.5))
        self.assertEqual(read_run_status(run, NOW), "running")

    def test_a_missed_heartbeat_is_abandoned(self) -> None:
        stale = NOW - timedelta(seconds=DETAIL_SAMPLE_SECONDS * (STALE_HEARTBEATS + 1))
        self.assertEqual(read_run_status(_run(last=stale), NOW), "abandoned")

    def test_one_slow_cycle_is_not_called_a_death(self) -> None:
        """The margin exists so a single late detail sample is forgiven."""
        late = NOW - timedelta(seconds=DETAIL_SAMPLE_SECONDS * 1.5)
        self.assertEqual(read_run_status(_run(last=late), NOW), "running")

    def test_a_finished_run_keeps_its_own_verdict(self) -> None:
        """pass/fail are written by a process that survived; believe them."""
        for verdict in ("pass", "fail", "incomplete"):
            ancient = NOW - timedelta(days=30)
            self.assertEqual(read_run_status(_run(verdict, last=ancient), NOW), verdict)

    def test_a_run_that_never_sampled_falls_back_to_its_start(self) -> None:
        run = {"status": "running", "startedAt": (NOW - timedelta(hours=4)).isoformat()}
        self.assertEqual(read_run_status(run, NOW), "abandoned")

    def test_a_record_with_no_timestamps_at_all_is_abandoned(self) -> None:
        self.assertEqual(read_run_status({"status": "running"}, NOW), "abandoned")

    def test_an_unparseable_timestamp_is_abandoned_not_an_exception(self) -> None:
        """Reading evidence must not raise; that is how evidence gets ignored."""
        run = {"status": "running", "lastSampleAt": "not a timestamp"}
        self.assertEqual(read_run_status(run, NOW), "abandoned")

    def test_the_heartbeat_field_is_honoured_when_present(self) -> None:
        run = _run(last=NOW - timedelta(seconds=30), heartbeat=5)
        self.assertEqual(read_run_status(run, NOW), "abandoned")


if __name__ == "__main__":
    unittest.main(verbosity=2)
