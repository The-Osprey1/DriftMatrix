"""The panel-down alert: outage timing, one popup per outage, gap, games, launch."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from unittest.mock import Mock, patch

import pytest

import game_mode
import hub_status
import panel_down_alert as alert
from panel_client import PanelTransportError
import utility_hub as hub

T0 = 1_791_590_000.0  # a wall time on 2026-10-09
MINUTE = 60.0
BREAKAWAY = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
# Seconds between the failed probes of 2026-10-09 16:26:57-17:49:45 in the real
# panel_health.jsonl. The PC was awake throughout; the Hub's ticks slowed
# because panel writes inside them waited 25 s on the gate and then timed out.
OUTAGE_GAPS_10_09 = (
    126, 123, 125, 120, 103, 142, 117, 158, 134, 43, 62, 49, 76, 51, 62, 53, 44,
    65, 55, 61, 42, 96, 48, 53, 71, 91, 114, 54, 61, 98, 65, 97, 54, 116, 113, 91,
    142, 115, 139, 159, 92, 194, 270, 292, 257, 66, 50, 40, 38, 42, 39,
)


class Clock:
    """Wall, monotonic and awake time that advance together while the PC is awake.

    ``sleep`` is the PC asleep: wall and monotonic time move (on this PC
    ``time.monotonic`` is QueryPerformanceCounter, which counts through
    sleep), awake time does not.
    """

    def __init__(self) -> None:
        self.wall = T0
        self.mono = 5_000.0
        self.awake = 40_000.0

    def advance(self, seconds: float) -> None:
        self.wall += seconds
        self.mono += seconds
        self.awake += seconds

    def sleep(self, seconds: float) -> None:
        self.wall += seconds
        self.mono += seconds


class FakeReporter:
    """The attributes PanelDownAlert reads from hub_status.HubStatusReporter."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.reachable: bool | None = None
        self.checked_at: float | None = None
        self.pc_offline: bool | None = None


class FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


class Harness:
    def __init__(self, tmp_path: Path, **overrides) -> None:
        self.clock = Clock()
        self.reporter = FakeReporter(tmp_path / "hub_status.json")
        self.executable = str(tmp_path / "pythonw.exe")
        self.launches: list[tuple[list[str], dict]] = []
        self.processes: list[FakeProcess] = []
        self.popen_error: Exception | None = None
        self.game: object | None = None
        self.scan_error: Exception | None = None
        self.scans = 0
        self.attempts = []
        self.logger = Mock()
        self.refuse_breakaway = False
        options = dict(
            clock=lambda: self.clock.wall,
            monotonic=lambda: self.clock.mono,
            awake=lambda: self.clock.awake,
            game_check=self._game_check,
            popen=self._popen,
            executable=self.executable,
            logger=self.logger,
        )
        options.update(overrides)
        self.alert = alert.PanelDownAlert(self.reporter, **options)

    def _game_check(self) -> object | None:
        self.scans += 1
        if self.scan_error is not None:
            raise self.scan_error
        return self.game

    def _popen(self, argv: list[str], **kwargs) -> FakeProcess:
        self.attempts.append(kwargs.get("creationflags"))
        if self.popen_error is not None:
            raise self.popen_error
        if self.refuse_breakaway and kwargs.get("creationflags", 0) & BREAKAWAY:
            # What CreateProcess does in a job without JOB_OBJECT_LIMIT_BREAKAWAY_OK.
            raise PermissionError(13, "Access is denied", None, 5)
        process = FakeProcess()
        self.launches.append((argv, kwargs))
        self.processes.append(process)
        return process

    def probe(self, ok: bool, *, pc_offline: bool | None = None) -> str:
        """The reporter probes now, then the alert ticks (the Hub's order)."""
        self.reporter.reachable = ok
        self.reporter.checked_at = self.clock.wall
        self.reporter.pc_offline = pc_offline
        return self.alert.tick()

    def infos(self) -> list[str]:
        return [call.args[0] % call.args[1:] for call in self.logger.info.call_args_list]

    def run(self, seconds: float, ok: bool, *, probe_every: float = 30.0, tick_every: float = 5.0) -> list[str]:
        """The Hub loop: a tick every 5 s, a probe every 30 s."""
        states = []
        elapsed = since_probe = 0.0
        while elapsed + tick_every <= seconds + 1e-9:
            self.clock.advance(tick_every)
            elapsed += tick_every
            since_probe += tick_every
            if since_probe >= probe_every - 1e-9:
                since_probe = 0.0
                states.append(self.probe(ok))
            else:
                states.append(self.alert.tick())
        return states

    def since_argument(self, index: int = -1) -> float:
        argv = self.launches[index][0]
        return float(argv[argv.index("--since") + 1])


def test_popup_starts_once_ten_minutes_after_the_first_failed_probe(tmp_path):
    h = Harness(tmp_path)
    assert h.probe(True) == alert.IDLE
    h.clock.advance(30)
    assert h.probe(False) == alert.COUNTING
    first_failure = h.clock.wall
    states = h.run(9 * MINUTE + 25, ok=False)  # 9:25 of outage, ticks every 5 s
    assert h.launches == [] and set(states) == {alert.COUNTING}
    h.run(35, ok=False)  # through 10:00 of outage
    assert len(h.launches) == 1
    assert h.since_argument() == pytest.approx(first_failure, abs=0.001)

    states = h.run(2 * 60 * MINUTE, ok=False)
    h.processes[0].returncode = 0  # closed by hand; still the same outage
    states += h.run(30 * MINUTE, ok=False)
    assert len(h.launches) == 1
    assert set(states) == {alert.ALERTED}


def test_launch_command_is_detached_windowless_and_names_the_status_file(tmp_path):
    h = Harness(tmp_path)
    h.probe(False)
    since = h.clock.wall
    h.run(10 * MINUTE, ok=False)
    argv, kwargs = h.launches[0]
    assert argv == [h.executable, str(alert.POPUP_SCRIPT), "--since", f"{since:.3f}",
                    "--status", str(tmp_path / "hub_status.json")]
    assert alert.POPUP_SCRIPT.name == "panel_down_popup.py" and alert.POPUP_SCRIPT.exists()
    assert kwargs["close_fds"] is True
    assert kwargs["stdin"] is kwargs["stdout"] is kwargs["stderr"] is subprocess.DEVNULL
    if os.name == "nt":
        # Out of the Hub's Task Scheduler job, so ending the task neither
        # kills the popup nor waits for it.
        assert kwargs["creationflags"] == (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                                           | subprocess.CREATE_BREAKAWAY_FROM_JOB)
    else:
        assert kwargs["creationflags"] == 0
    assert len(h.attempts) == 1
    h.logger.info.assert_called()


@pytest.mark.skipif(os.name != "nt", reason="job objects are Windows-only")
def test_a_job_that_forbids_breakaway_gets_the_popup_started_inside_it(tmp_path):
    h = Harness(tmp_path)
    h.refuse_breakaway = True
    h.probe(False)
    states = h.run(10 * MINUTE, ok=False)
    assert states[-1] == alert.LAUNCHED and len(h.launches) == 1
    plain = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    assert h.attempts == [plain | BREAKAWAY, plain]
    assert h.launches[0][1]["creationflags"] == plain
    assert any("cannot leave the Hub's job" in line for line in h.infos())
    h.logger.warning.assert_not_called()


def test_other_start_errors_are_not_retried_without_breakaway(tmp_path):
    h = Harness(tmp_path)
    h.popen_error = FileNotFoundError(2, "pythonw.exe is missing")
    h.probe(False)
    assert h.run(10 * MINUTE, ok=False)[-1] == alert.LAUNCH_FAILED
    assert len(h.attempts) == 1


def test_access_denied_is_recognised_by_class_or_winerror():
    assert alert._access_denied(PermissionError(13, "Access is denied", None, 5))
    error = OSError(13, "Access is denied")
    error.winerror = 5
    assert alert._access_denied(error)
    assert not alert._access_denied(FileNotFoundError(2, "missing"))


def test_extra_games_reach_the_popup_command_line(tmp_path):
    h = Harness(tmp_path, extra_games=["osu!.exe", "-dash.exe"])
    h.probe(False)
    h.run(10 * MINUTE, ok=False)
    argv = h.launches[0][0]
    assert argv[-2:] == ["--extra-game=osu!.exe", "--extra-game=-dash.exe"]
    import panel_down_popup
    assert panel_down_popup.parse_args(argv[2:]).extra_games == ["osu!.exe", "-dash.exe"]


def test_failing_from_hub_start_counts_from_the_first_failure(tmp_path):
    h = Harness(tmp_path)
    assert h.alert.tick() == alert.IDLE  # no probe yet: reachable is None
    h.clock.advance(7)
    assert h.alert.tick() == alert.IDLE
    assert h.probe(False) == alert.COUNTING
    first_failure = h.clock.wall
    h.run(9 * MINUTE + 30, ok=False)
    assert h.launches == []
    h.run(30, ok=False)
    assert h.since_argument() == pytest.approx(first_failure, abs=0.001)


def test_later_failures_do_not_move_the_outage_start(tmp_path):
    h = Harness(tmp_path)
    h.probe(True)
    h.clock.advance(30)
    h.probe(False)
    first_failure = h.clock.wall
    h.run(5 * MINUTE, ok=False)
    assert h.alert.outage_since == first_failure


def test_a_successful_probe_ends_the_outage_and_restarts_the_count(tmp_path):
    h = Harness(tmp_path)
    h.probe(False)
    h.run(9 * MINUTE, ok=False)
    assert h.probe(True) == alert.IDLE
    assert h.alert.outage_since is None
    h.clock.advance(30)
    h.probe(False)
    second_start = h.clock.wall
    h.run(9 * MINUTE, ok=False)
    assert h.launches == []  # 18 minutes down in total, but never 10 in a row
    h.run(60, ok=False)
    assert len(h.launches) == 1
    assert h.since_argument() == pytest.approx(second_start, abs=0.001)


def test_a_new_outage_alerts_again_once_the_gap_has_passed(tmp_path):
    h = Harness(tmp_path)
    h.probe(False)
    h.run(10 * MINUTE, ok=False)
    first_launch = h.clock.mono
    h.processes[0].returncode = 0  # the popup saw the recovery and closed
    h.run(25 * MINUTE, ok=True)
    h.run(30, ok=False)
    h.run(10 * MINUTE, ok=False)
    assert len(h.launches) == 2
    assert h.clock.mono - first_launch >= 30 * MINUTE


def test_a_flapping_link_gets_at_most_one_popup_per_thirty_minutes(tmp_path):
    h = Harness(tmp_path)
    h.probe(False)
    h.run(10 * MINUTE, ok=False)
    first_launch = h.clock.mono
    h.processes[0].returncode = 0
    h.run(30, ok=True)  # one good probe ends outage 1
    h.run(30, ok=False)
    second_start = h.clock.wall
    gap_left = first_launch + 30 * MINUTE - h.clock.mono
    states = h.run(gap_left - 5, ok=False)  # second threshold long passed; still inside the gap
    assert len(h.launches) == 1 and states[-1] == alert.WAITING
    assert alert.WAITING in states and alert.COUNTING in states
    h.run(5, ok=False)
    assert len(h.launches) == 2
    assert h.clock.mono - first_launch == pytest.approx(30 * MINUTE)
    assert h.since_argument() == pytest.approx(second_start, abs=0.001)


def test_the_gap_is_configurable_and_zero_means_none(tmp_path):
    h = Harness(tmp_path, min_gap_minutes=0, after_minutes=2)
    h.probe(False)
    h.run(2 * MINUTE, ok=False)
    h.processes[0].returncode = 0
    h.run(30, ok=True)
    h.run(30, ok=False)
    h.run(2 * MINUTE, ok=False)
    assert len(h.launches) == 2


def test_a_running_game_defers_the_popup_and_rechecks_every_thirty_seconds(tmp_path):
    h = Harness(tmp_path)
    h.game = game_mode.GameSession("ELDEN RING", 4242, T0)
    h.probe(False)
    since = h.clock.wall
    h.run(10 * MINUTE, ok=False)
    assert h.launches == [] and h.scans == 1
    states = h.run(2 * MINUTE, ok=False)  # ticks every 5 s, scans every 30 s
    assert h.launches == []
    assert h.scans == 5
    assert alert.DEFERRED in states
    logged = [call.args for call in h.logger.info.call_args_list]
    assert sum("ELDEN RING" in str(args) for args in logged) == 1
    h.game = None
    h.run(30, ok=False)
    assert len(h.launches) == 1
    assert h.since_argument() == pytest.approx(since, abs=0.001)


def test_recovery_while_deferred_for_a_game_shows_nothing(tmp_path):
    h = Harness(tmp_path)
    h.game = game_mode.GameSession("HADES", 1, T0)
    h.probe(False)
    h.run(15 * MINUTE, ok=False)
    h.run(30, ok=True)
    h.game = None
    h.run(5 * MINUTE, ok=True)
    assert h.launches == []


def test_a_failed_process_scan_never_defers_the_popup(tmp_path):
    h = Harness(tmp_path)
    h.scan_error = RuntimeError("psutil is broken")
    h.probe(False)
    h.run(10 * MINUTE, ok=False)
    assert len(h.launches) == 1
    h.logger.warning.assert_called_once()
    assert "process scan failed" in h.logger.warning.call_args.args[0]


def test_launch_failure_is_logged_not_raised_and_not_retried_in_that_outage(tmp_path):
    h = Harness(tmp_path)
    h.popen_error = FileNotFoundError("pythonw.exe is missing")
    h.probe(False)
    states = h.run(10 * MINUTE, ok=False)
    assert states[-1] == alert.LAUNCH_FAILED
    h.logger.warning.assert_called_once()
    assert "could not start" in h.logger.warning.call_args.args[0]
    states = h.run(30 * MINUTE, ok=False)
    assert set(states) == {alert.ALERTED} and h.launches == []
    h.logger.warning.assert_called_once()

    # A failed start opened no window, so it does not hold the next outage back.
    h.popen_error = None
    h.run(30, ok=True)
    h.run(30, ok=False)
    h.run(10 * MINUTE, ok=False)
    assert len(h.launches) == 1


def test_any_launch_exception_stays_inside_the_service(tmp_path):
    h = Harness(tmp_path)
    h.popen_error = ValueError("bad argument")
    h.probe(False)
    assert alert.LAUNCH_FAILED in h.run(10 * MINUTE, ok=False)


def test_no_second_popup_while_an_earlier_one_is_still_open(tmp_path):
    h = Harness(tmp_path)
    h.probe(False)
    h.run(10 * MINUTE, ok=False)
    h.run(30, ok=True)  # recovered, but the first popup has not closed itself yet
    h.run(30, ok=False)
    states = h.run(45 * MINUTE, ok=False)  # past the threshold and the gap
    assert len(h.launches) == 1 and states[-1] == alert.WAITING
    h.processes[0].returncode = 0
    h.run(5, ok=False, tick_every=5)
    assert len(h.launches) == 2


def test_a_broken_popup_handle_counts_as_closed(tmp_path):
    h = Harness(tmp_path)
    h.probe(False)
    h.run(10 * MINUTE, ok=False)
    h.processes[0].poll = Mock(side_effect=OSError("handle gone"))
    h.alert.tick()
    assert h.alert.popup is None


def test_sleep_re_anchors_an_outage_that_has_not_alerted_and_says_so(tmp_path):
    h = Harness(tmp_path)
    h.probe(False)
    h.run(4 * MINUTE, ok=False)
    h.clock.sleep(8 * 60 * MINUTE)  # the PC slept; the Hub made no probes
    assert h.probe(False) == alert.COUNTING
    woke = h.clock.wall
    assert h.alert.outage_since == woke
    [line] = [line for line in h.infos() if "count restarts" in line]
    assert "no panel probe for 480 min (PC asleep about 480 min)" in line
    h.run(9 * MINUTE, ok=False)
    assert h.launches == []
    h.run(60, ok=False)
    assert h.since_argument() == pytest.approx(woke, abs=0.001)


def test_sleep_after_the_alert_is_still_the_same_outage(tmp_path):
    h = Harness(tmp_path)
    h.probe(False)
    h.run(10 * MINUTE, ok=False)
    h.processes[0].returncode = 0
    h.clock.sleep(8 * 60 * MINUTE)
    states = [h.probe(False)] + h.run(30 * MINUTE, ok=False)
    assert set(states) == {alert.ALERTED} and len(h.launches) == 1


def replay_gaps(h: Harness, gaps) -> list[str]:
    """One good probe, a failure, then a failure after each gap; stops at the popup."""
    h.probe(True)
    h.clock.advance(30)
    states = [h.probe(False)]
    for gap in gaps:
        if h.launches:
            break
        h.clock.advance(gap)  # awake: the Hub is slow, not stopped
        states.append(h.probe(False))
    return states


@pytest.mark.parametrize("start", [0, 40])  # 16:26:57, and 17:28:17 (the slowest stretch)
@pytest.mark.parametrize("stretch", [0, 10, 60, 300])
def test_an_awake_hub_with_slow_ticks_never_restarts_the_outage_clock(tmp_path, stretch, start):
    # The 10-09 16:26-17:50 gap pattern, as recorded and with every gap made
    # longer. Under the old rule (any gap over 300 s is sleep) the 17:28 run
    # restarted the clock at +10 s, and +300 s restarted it at every probe,
    # so the popup never came.
    h = Harness(tmp_path)
    states = replay_gaps(h, [gap + stretch for gap in OUTAGE_GAPS_10_09[start:]])
    first_failure = h.alert.outage_since
    assert len(h.launches) == 1 and states[-1] == alert.LAUNCHED
    assert h.since_argument() == pytest.approx(first_failure, abs=0.001)
    assert first_failure == pytest.approx(T0 + 30)  # never moved
    assert 10 * MINUTE <= h.clock.wall - first_failure < 10 * MINUTE + 292 + stretch
    assert not any("count restarts" in line for line in h.infos())


def test_a_steady_305_second_probe_cadence_still_alerts(tmp_path):
    h = Harness(tmp_path)
    replay_gaps(h, [305] * int(7 * 3600 / 305))
    assert len(h.launches) == 1
    assert h.clock.wall - h.alert.outage_since == pytest.approx(2 * 305)


def test_the_sleep_test_allows_slack_for_clock_steps_and_late_reads(tmp_path):
    h = Harness(tmp_path)
    h.probe(False)
    first = h.clock.wall
    h.clock.advance(30)
    h.clock.sleep(alert.SLEEP_SLACK_SECONDS - 1)  # e.g. an NTP step
    h.probe(False)
    assert h.alert.outage_since == first
    h.clock.advance(30)
    h.clock.sleep(alert.SLEEP_SLACK_SECONDS + 1)
    h.probe(False)
    assert h.alert.outage_since == h.clock.wall


@pytest.mark.parametrize("awake", [lambda: None, Mock(side_effect=OSError("no clock")),
                                   lambda: float("nan")])
def test_without_an_awake_clock_only_a_long_gap_counts_as_sleep(tmp_path, awake):
    h = Harness(tmp_path, awake=awake)
    h.probe(False)
    first = h.clock.wall
    for _ in range(3):
        h.clock.advance(alert.FALLBACK_SLEEP_GAP_SECONDS - 60)  # slow, awake Hub
        h.probe(False)
    assert h.alert.outage_since == first and len(h.launches) == 1
    h2 = Harness(tmp_path, awake=awake)
    h2.probe(False)
    h2.clock.advance(alert.FALLBACK_SLEEP_GAP_SECONDS + 1)
    h2.probe(False)
    assert h2.alert.outage_since == h2.clock.wall
    assert any("no awake clock, so counted as sleep" in line for line in h2.infos())


# ------------------------------------------------------------------ this PC offline

def test_failures_while_this_pc_has_no_network_do_not_count(tmp_path):
    h = Harness(tmp_path)
    h.probe(True)
    h.clock.advance(30)
    h.probe(False)  # the sentinel has not confirmed the router fault yet
    assert h.alert.outage_since is not None
    states = []
    for _ in range(40):  # 20 minutes of failures with the router unreachable
        h.clock.advance(30)
        states.append(h.probe(False, pc_offline=True))
    assert set(states) == {alert.PC_OFFLINE} and h.launches == []
    assert h.alert.outage_since is None
    assert sum("own network is down" in line for line in h.infos()) == 1
    h.clock.advance(30)
    assert h.probe(False, pc_offline=False) == alert.COUNTING  # PC back, panel still down
    back = h.clock.wall
    h.run(9 * MINUTE + 25, ok=False)
    assert h.launches == []
    h.run(35, ok=False)
    assert h.since_argument() == pytest.approx(back, abs=0.001)


def test_pc_offline_after_the_alert_keeps_the_outage(tmp_path):
    h = Harness(tmp_path)
    h.probe(False)
    h.run(10 * MINUTE, ok=False)
    since = h.alert.outage_since
    h.clock.advance(30)
    assert h.probe(False, pc_offline=True) == alert.ALERTED
    assert h.alert.outage_since == since and len(h.launches) == 1


def test_pc_offline_unknown_counts_as_the_panel(tmp_path):
    h = Harness(tmp_path)
    h.probe(False, pc_offline=None)
    h.run(10 * MINUTE, ok=False)
    assert len(h.launches) == 1


def test_one_probe_is_read_once_however_many_ticks_see_it(tmp_path):
    h = Harness(tmp_path)
    h.probe(False)
    since = h.alert.outage_since
    for _ in range(5):
        h.clock.advance(5)
        h.alert.tick()
    assert h.alert.outage_since == since


def test_default_game_check_uses_the_hub_extra_games(tmp_path):
    osu = game_mode.ProcessInfo(pid=7, exe=r"C:\Games\osu!\osu!.exe", create_time=T0, rss_bytes=10)
    with patch.object(game_mode, "list_processes", return_value=[osu]):
        assert alert.running_game(("osu!.exe",)).name == "OSU!"
        assert alert.running_game(()) is None
        h = Harness(tmp_path, game_check=None, extra_games=["osu!.exe"])
        h.probe(False)
        h.run(10 * MINUTE, ok=False)
    assert h.launches == [] and h.alert.next_game_check_at > h.clock.mono


def test_works_with_the_real_status_reporter(tmp_path):
    clock = Clock()
    panel = Mock()
    panel.probe_device.side_effect = PanelTransportError("offline")
    reporter = hub_status.HubStatusReporter(
        panel, reminders_enabled=False, path=tmp_path / "hub_status.json",
        clock=lambda: clock.wall, monotonic=lambda: clock.mono,
    )
    launches = []
    service = alert.PanelDownAlert(
        reporter, clock=lambda: clock.wall, monotonic=lambda: clock.mono, awake=lambda: clock.awake,
        game_check=lambda: None, popen=lambda argv, **_: launches.append(argv) or FakeProcess(),
        executable="pythonw.exe", logger=Mock(),
    )
    first_probe = clock.wall
    # The status file itself is not under test; 121 rewrites in a few ms can
    # meet a scanner's lock on Windows.
    with patch.object(hub_status, "atomic_json"):
        for _ in range(int(10 * MINUTE / 5) + 1):
            reporter.tick()
            service.tick()
            clock.advance(5)
    assert len(launches) == 1
    assert launches[0][-2:] == ["--status", str(reporter.path)]
    assert float(launches[0][launches[0].index("--since") + 1]) == pytest.approx(first_probe, abs=0.001)


def test_pythonw_is_preferred_next_to_the_running_interpreter(tmp_path):
    python = tmp_path / "python.exe"
    python.write_bytes(b"")
    assert alert.pythonw_executable(str(python)) == str(python)  # no sibling: keep it
    pythonw = tmp_path / "pythonw.exe"
    pythonw.write_bytes(b"")
    assert alert.pythonw_executable(str(python)) == str(pythonw)
    assert alert.pythonw_executable(str(pythonw)) == str(pythonw)
    assert alert.pythonw_executable(str(tmp_path / "PYTHONW.EXE")) == str(tmp_path / "PYTHONW.EXE")


def test_thresholds_must_make_sense():
    reporter = FakeReporter(Path("hub_status.json"))
    with pytest.raises(ValueError):
        alert.PanelDownAlert(reporter, after_minutes=0)
    with pytest.raises(ValueError):
        alert.PanelDownAlert(reporter, min_gap_minutes=-1)


def test_status_path_defaults_to_the_reporter_then_the_data_dir():
    assert alert.PanelDownAlert(FakeReporter(Path("x/hub_status.json"))).status_path == Path("x/hub_status.json")
    bare = Mock(spec=["reachable", "checked_at"])
    assert alert.PanelDownAlert(bare).status_path.name == hub_status.STATUS_NAME


# ------------------------------------------------------------------ config

def test_config_defaults_are_on_ten_and_thirty():
    config = hub.config_from_mapping({})
    assert config.panel_down_alert == hub.PanelDownAlertHubConfig(True, 10.0, 30.0)
    assert hub.HubConfig().panel_down_alert == config.panel_down_alert


def test_config_parses_and_the_example_carries_the_section():
    config = hub.config_from_mapping({"panel_down_alert": {
        "enabled": False, "after_minutes": 15, "min_gap_minutes": 0}})
    assert config.panel_down_alert == hub.PanelDownAlertHubConfig(False, 15.0, 0.0)
    example = hub.load_config(Path(__file__).with_name("utility_hub.example.json"))
    assert example.panel_down_alert == hub.PanelDownAlertHubConfig(True, 10.0, 30.0)


@pytest.mark.parametrize("bad", [
    "on",
    {"enabled": "yes"},
    {"enabled": 1},
    {"after_minutes": 0},
    {"after_minutes": 0.5},
    {"after_minutes": 721},
    {"after_minutes": "10"},
    {"after_minutes": True},
    {"after_minutes": float("nan")},
    {"min_gap_minutes": -1},
    {"min_gap_minutes": 1441},
    {"min_gap_minutes": None},
    {"min_gap_minutes": float("inf")},
])
def test_bad_config_values_are_refused(bad):
    with pytest.raises(ValueError):
        hub.config_from_mapping({"panel_down_alert": bad})


def test_builder_maps_minutes_and_game_mode_extra_games():
    config = hub.config_from_mapping({
        "panel_down_alert": {"after_minutes": 12, "min_gap_minutes": 45},
        "game_mode": {"enabled": False, "extra_games": ["osu!.exe"]},
    })
    reporter = FakeReporter(Path("hub_status.json"))
    built = hub.build_panel_down_alert(config, reporter, logger=Mock())
    assert built.reporter is reporter
    assert (built.after_seconds, built.min_gap_seconds) == (720.0, 2700.0)
    assert built.extra_games == ("osu!.exe",)
