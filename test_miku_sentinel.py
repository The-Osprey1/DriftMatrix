"""The sentinel has one job: never say LIVE when she is on the stub."""

from __future__ import annotations

from pathlib import Path
import tempfile
import time
import unittest

import miku_sentinel as ms
from visual_system import LANE_WIDTH, measure_text
import visual_system as vs

STARTUP_ECHO = "22:34:11 INFO miku.out brain  : echo  (waiting for Ollama)"
RECOVERED = "22:34:26 INFO miku.out brain  : ollama:llama3.1:8b (recovered after 15s)"


def _log(*lines: str) -> Path:
    handle = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False, encoding="utf-8")
    handle.write("\n".join(lines) + "\n")
    handle.close()
    return Path(handle.name)


def meter(frame):
    """(filled LEDs, their colour) on the row-7 meter; (0, None) for a bare track."""
    return vs.read_meter(frame)


class StateTests(unittest.TestCase):
    def test_the_recovery_line_wins_over_the_startup_line(self) -> None:
        # Both are in the log on a normal cold boot, and the later one is the
        # true one. Reading forwards would report echo for the rest of the day.
        self.assertEqual(ms.read_brain(_log(STARTUP_ECHO, RECOVERED)), "ollama:llama3.1:8b")

    def test_the_stub_is_reported_as_echo(self) -> None:
        status = ms.read_status(is_running=lambda: True, log_path=_log(STARTUP_ECHO))
        self.assertIs(status.state, ms.MikuState.ECHO)

    def test_an_unreadable_log_is_not_reported_as_live(self) -> None:
        # The blind spot this scene exists to close. A missing brain line means
        # we do not know, and "we do not know" must not render as emerald.
        status = ms.read_status(is_running=lambda: True, log_path=Path("nope.log"))
        self.assertIs(status.state, ms.MikuState.ECHO)
        self.assertEqual(status.brain, "unknown")

    def test_a_dead_process_beats_a_healthy_log(self) -> None:
        status = ms.read_status(is_running=lambda: False, log_path=_log(RECOVERED))
        self.assertIs(status.state, ms.MikuState.OFF)

    def test_the_real_model_reads_live(self) -> None:
        status = ms.read_status(is_running=lambda: True, log_path=_log(RECOVERED))
        self.assertIs(status.state, ms.MikuState.LIVE)


class TurnCountTests(unittest.TestCase):
    def test_yesterdays_turns_are_not_todays(self) -> None:
        now = time.time()
        rows = _log(
            '{"turn_id":1,"started_at":%f}' % (now - 86_400 * 2),
            '{"turn_id":2,"started_at":%f}' % now,
        )
        self.assertEqual(ms.count_turns_today(rows, now=now), 1)

    def test_a_half_written_row_is_skipped_not_raised(self) -> None:
        # metrics.jsonl is appended to by a live process, so the tail really
        # can be cut mid-row.
        now = time.time()
        rows = _log('{"turn_id":1,"started_at":%f}' % now, '{"turn_id":2,"star')
        self.assertEqual(ms.count_turns_today(rows, now=now), 1)


class ActivityTests(unittest.TestCase):
    def test_a_stale_active_state_is_not_believed(self) -> None:
        # She publishes on change and never on a timer, so SPEAKING with no
        # successor means the process went away mid-sentence. The first cut
        # of this scene would have shown SPEAK until the next reboot.
        now = time.time()
        stale = _log('{"state":"speaking","at":%f}' % (now - ms.PRESENCE_TTL_SECONDS - 1))
        self.assertIs(ms.read_activity(stale, now=now), ms.MikuActivity.IDLE)

    def test_a_fresh_active_state_is(self) -> None:
        now = time.time()
        fresh = _log('{"state":"speaking","at":%f}' % (now - 2))
        self.assertIs(ms.read_activity(fresh, now=now), ms.MikuActivity.SPEAKING)

    def test_idle_never_goes_stale(self) -> None:
        # Being idle for six hours is still being idle.
        now = time.time()
        old = _log('{"state":"idle","at":%f}' % (now - 86_400))
        self.assertIs(ms.read_activity(old, now=now), ms.MikuActivity.IDLE)

    def test_a_missing_or_unreadable_file_reads_idle(self) -> None:
        self.assertIs(ms.read_activity(Path("nope.json")), ms.MikuActivity.IDLE)
        self.assertIs(ms.read_activity(_log("{half-writ")), ms.MikuActivity.IDLE)

    def test_a_dead_process_is_not_doing_anything(self) -> None:
        now = time.time()
        status = ms.read_status(
            is_running=lambda: False, log_path=_log(RECOVERED),
            presence_path=_log('{"state":"thinking","at":%f}' % now), now=now)
        self.assertIs(status.activity, ms.MikuActivity.IDLE)


class PushTests(unittest.TestCase):
    class Spy:
        def __init__(self) -> None:
            self.pushes = 0

        def push_app(self, name: str, payload: dict) -> None:
            self.pushes += 1

        deletes = 0

        def delete_pushed_app(self, name: str) -> bool:
            self.deletes += 1
            return True

    def _controller(self, spy, status) -> ms.MikuSentinelController:
        return ms.MikuSentinelController(
            spy, ms.MikuPolicy(enabled=True, refresh_seconds=3600),
            status_reader=lambda: status[0])

    def test_an_unchanged_status_is_not_pushed_again(self) -> None:
        # The comparison was against a (state, activity) pair while only the
        # state was stored, so they were unequal every time and the scene
        # pushed on every three-second hub tick. Pushing too often looks
        # exactly like pushing correctly, which is why this needs a test.
        spy, status = self.Spy(), [ms.MikuStatus(ms.MikuState.LIVE, brain="ollama")]
        controller = self._controller(spy, status)
        for _ in range(5):
            controller.tick()
        self.assertEqual(spy.pushes, 1)

    def test_a_new_activity_is_pushed_at_once(self) -> None:
        spy, status = self.Spy(), [ms.MikuStatus(ms.MikuState.LIVE, brain="ollama")]
        controller = self._controller(spy, status)
        controller.tick()
        status[0] = ms.MikuStatus(ms.MikuState.LIVE, brain="ollama",
                                  activity=ms.MikuActivity.SPEAKING)
        self.assertTrue(controller.tick().pushed)

    def test_off_removes_the_page_once_and_never_pushes(self) -> None:
        spy, status = self.Spy(), [ms.MikuStatus(ms.MikuState.OFF)]
        controller = self._controller(spy, status)
        for _ in range(3):
            self.assertFalse(controller.tick().pushed)
        self.assertEqual((spy.pushes, spy.deletes), (0, 1))

    def test_turning_miku_on_brings_the_page_back(self) -> None:
        spy, status = self.Spy(), [ms.MikuStatus(ms.MikuState.LIVE, brain="ollama")]
        controller = self._controller(spy, status)
        controller.tick()
        status[0] = ms.MikuStatus(ms.MikuState.OFF)
        controller.tick()
        status[0] = ms.MikuStatus(ms.MikuState.LIVE, brain="ollama")
        self.assertTrue(controller.tick().pushed)
        self.assertEqual((spy.pushes, spy.deletes), (2, 1))

    def test_a_reader_that_raises_does_not_kill_the_tick(self) -> None:
        def boom() -> ms.MikuStatus:
            raise OSError("log is locked")

        controller = ms.MikuSentinelController(
            self.Spy(), ms.MikuPolicy(enabled=True), status_reader=boom)
        self.assertFalse(controller.tick().pushed)  # must not raise


    def test_meter_counts_live_turns_only(self) -> None:
        few = meter(ms.render_miku_lane(ms.MikuStatus(ms.MikuState.LIVE, "claude", 5)))
        many = meter(ms.render_miku_lane(ms.MikuStatus(ms.MikuState.LIVE, "claude", 40)))
        self.assertLess(few[0], many[0])
        self.assertEqual(many[1], vs.LEVEL_COLORS["good"])
        capped = ms.MikuStatus(ms.MikuState.LIVE, "claude", 10 * ms.TURNS_METER_FULL)
        self.assertEqual(meter(ms.render_miku_lane(capped))[0], vs.LANE_WIDTH)

    def test_no_turns_or_no_live_brain_keeps_the_bare_track(self) -> None:
        # A one-LED stub would claim a turn that never happened.
        for status in (ms.MikuStatus(ms.MikuState.LIVE, "claude", 0),
                       ms.MikuStatus(ms.MikuState.ECHO, "echo", 12),
                       ms.MikuStatus(ms.MikuState.OFF, turns_today=12)):
            with self.subTest(state=status.state):
                self.assertEqual(meter(ms.render_miku_lane(status)), (0, None))

class PayloadTests(unittest.TestCase):
    def test_every_banner_fits_the_lane(self) -> None:
        # The guard that was missing. The first banners -- "MIKU UP" at 27px,
        # "MIKU OFF" at 31px -- did not fit the 21px budget, and the lane
        # trims to fit rather than overflowing, so all three states rendered
        # as the same truncated "MIKU " and the scene said nothing at all.
        # The font is proportional: this has to be measured, not counted.
        budget = vs.LANE_WIDTH - 2
        for state in ms.MikuState:
            for activity in ms.MikuActivity:
                status = ms.MikuStatus(state, activity=activity)
                banner = ms.lane_text(status)[0]
                with self.subTest(banner=banner):
                    self.assertLessEqual(
                        vs.measure_text(banner), budget,
                        f"{banner!r} measures {vs.measure_text(banner)}px in a "
                        f"{budget}px lane and will be silently truncated")

    def test_every_state_fits_the_panel_budget(self) -> None:
        for state in ms.MikuState:
            for activity in ms.MikuActivity:
                with self.subTest(state=state, activity=activity):
                    status = ms.MikuStatus(state, brain="ollama:llama3.1:8b",
                                           turns_today=999, activity=activity)
                    ms.build_miku_payload(status)  # raises past 2KB / 1KB icon

    def test_an_off_process_shows_no_activity(self) -> None:
        # She is not listening to anything; the icon must not animate as if
        # she were.
        off = ms.build_miku_icon_gif(ms.MikuState.OFF, activity=ms.MikuActivity.IDLE)
        for activity in ms.MikuActivity:
            with self.subTest(activity=activity):
                self.assertEqual(
                    ms.build_miku_icon_gif(ms.MikuState.OFF, activity=activity), off)


if __name__ == "__main__":
    unittest.main()
