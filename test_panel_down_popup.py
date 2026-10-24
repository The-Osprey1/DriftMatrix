"""The panel-down popup, with its logic tested apart from Tk."""

from __future__ import annotations

from datetime import datetime
import gc
import json
import os
from pathlib import Path
import sys
import threading
import uuid
from unittest.mock import Mock, patch

import pytest

import game_mode
import hub_status
import panel_down_popup as popup

SINCE = datetime(2026, 10, 9, 13, 46, 12).timestamp()
MINUTE = 60.0


def write_status(path: Path, *, reachable, checked_at, updated_at=None,
                 failure="unreachable", pc_offline=None) -> Path:
    """A status file shaped like the one hub_status.HubStatusReporter writes."""
    path.write_text(json.dumps({
        "schema_version": 1, "updated_at": checked_at if updated_at is None else updated_at,
        "pid": 1, "panel_reachable": reachable, "panel_checked_at": checked_at,
        "panel_failure": None if reachable else failure, "pc_offline": pc_offline,
        "reminders_enabled": False, "reminder_delivery": None,
    }), encoding="utf-8")
    return path


# ------------------------------------------------------------------ text

def test_message_names_the_minutes_the_start_and_the_fix():
    assert popup.body_text(SINCE, SINCE + 12 * MINUTE + 59) == (
        "The LED panel has not answered for 12 min (since 13:46). It is most likely "
        "its Wi-Fi. Unplug the panel for 5 seconds and plug it back in."
    )
    assert popup.TITLE == "AWTRIX panel not responding"


def test_minutes_count_whole_minutes_and_never_go_negative():
    assert popup.minutes_down(SINCE, SINCE + 10 * MINUTE) == 10
    assert popup.minutes_down(SINCE, SINCE + 10 * MINUTE - 0.1) == 9
    assert popup.minutes_down(SINCE, SINCE + 3 * 60 * MINUTE) == 180
    assert popup.minutes_down(SINCE, SINCE - 5) == 0
    assert "for 0 min" in popup.body_text(SINCE, SINCE - 5)


def test_wifi_advice_is_only_for_a_panel_that_gives_no_answer():
    now = SINCE + 12 * MINUTE
    plain = popup.body_text(SINCE, now)
    assert popup.body_text(SINCE, now, failure=hub_status.FAILURE_UNREACHABLE) == plain
    assert popup.body_text(SINCE, now, failure=hub_status.FAILURE_OTHER) == plain
    for failure in (hub_status.FAILURE_HTTP, hub_status.FAILURE_PROTOCOL):
        text = popup.body_text(SINCE, now, failure=failure)
        assert text == ("The LED panel has not worked for 12 min (since 13:46): it answers, but "
                        "only with errors. Unplug the panel for 5 seconds and plug it back in to "
                        "restart it.")
        assert "not answered" not in text and "Wi-Fi" not in text


def test_a_pc_without_network_is_not_told_to_unplug_the_panel():
    text = popup.body_text(SINCE, SINCE + 40 * MINUTE, failure="unreachable", pc_offline=True)
    assert text == ("This PC has lost its own network, so it cannot reach the LED panel either "
                    "(no answer since 13:46). Check the PC's Wi-Fi first; the panel may be fine.")
    assert "Unplug" not in text


def test_a_stopped_hub_freezes_the_count_where_it_last_looked():
    stopped = SINCE + 15 * MINUTE
    for now in (stopped + 30 * MINUTE, stopped + 11 * 60 * MINUTE):
        text = popup.body_text(SINCE, now, pc_offline=True, hub_stopped_at=stopped)
        assert text == ("The Utility Hub stopped checking the LED panel at 14:01, so this note "
                        "cannot tell whether the panel is back. When it last checked, the panel "
                        "had been failing for 15 min (since 13:46).")
        assert "Unplug" not in text


# ------------------------------------------------------------------ closing

def test_a_fresh_reachable_check_after_the_outage_began_closes_it(tmp_path):
    now = SINCE + 20 * MINUTE
    path = write_status(tmp_path / "hub_status.json", reachable=True, checked_at=now - 10)
    assert popup.close_reason(popup.read_status(path), SINCE, now, 600) == "recovered"


@pytest.mark.parametrize("reachable, checked_offset", [
    (False, -10),            # still down
    (True, -120),            # reachable, but the check is stale (Hub stopped?)
    (True, 5),               # a check from the future is not fresh
])
def test_other_status_files_keep_it_open(tmp_path, reachable, checked_offset):
    now = SINCE + 20 * MINUTE
    path = write_status(tmp_path / "hub_status.json", reachable=reachable, checked_at=now + checked_offset)
    assert popup.close_reason(popup.read_status(path), SINCE, now, 600) is None


def test_a_reachable_check_from_before_the_outage_does_not_count(tmp_path):
    now = SINCE + 30
    path = write_status(tmp_path / "hub_status.json", reachable=True, checked_at=SINCE - 1)
    assert popup.close_reason(popup.read_status(path), SINCE, now, 30) is None
    write_status(path, reachable=True, checked_at=SINCE)
    assert popup.close_reason(popup.read_status(path), SINCE, now, 30) is None


def test_missing_corrupt_and_unprobed_status_keep_it_open(tmp_path):
    now = SINCE + 20 * MINUTE
    path = tmp_path / "hub_status.json"
    assert popup.close_reason(popup.read_status(path), SINCE, now, 0) is None
    for body in (b"", b"{", b"[]", b"\xff\xfe", b"x" * 5000):
        path.write_bytes(body)
        assert popup.close_reason(popup.read_status(path), SINCE, now, 0) is None
    write_status(path, reachable=None, checked_at=None, updated_at=now)  # Hub just started
    assert popup.close_reason(popup.read_status(path), SINCE, now, 0) is None


def test_a_reader_that_raises_is_no_news(tmp_path, monkeypatch):
    monkeypatch.setattr(popup.hub_status, "read_status", Mock(side_effect=RuntimeError("boom")))
    assert popup.read_status(tmp_path / "hub_status.json") is None


def test_it_closes_after_twelve_hours_regardless():
    now = SINCE + 13 * 60 * MINUTE
    assert popup.close_reason(None, SINCE, now, popup.MAX_OPEN_SECONDS - 1) is None
    assert popup.close_reason(None, SINCE, now, popup.MAX_OPEN_SECONDS) == "expired"
    assert popup.MAX_OPEN_SECONDS == 12 * 60 * 60


def test_status_fields_are_read_defensively(tmp_path):
    path = write_status(tmp_path / "hub_status.json", reachable=False, checked_at=SINCE,
                        failure="http", pc_offline=True)
    status = popup.read_status(path)
    assert popup.status_failure(status) == "http" and popup.status_pc_offline(status) is True
    assert popup.heartbeat(status) == SINCE
    for junk in (None, [], {"panel_failure": 7, "pc_offline": "yes", "updated_at": "now"},
                 {"updated_at": float("nan")}, {"updated_at": True}):
        assert (popup.status_failure(junk), popup.status_pc_offline(junk), popup.heartbeat(junk)) == (
            None, False, None)


# ------------------------------------------------------------------ a stopped Hub

def test_the_hub_counts_as_stopped_after_ten_quiet_awake_minutes():
    # Last status of a dead Hub, 15 min into the outage; the note opened at 10 min.
    watch = popup.HubWatch(opened_wall=SINCE + 10 * MINUTE, opened_awake=1_000.0)
    dead = {"updated_at": SINCE + 15 * MINUTE}
    watch.observe(dead, SINCE + 15 * MINUTE + 3, 1_000.0 + 5 * MINUTE + 3)
    awake_at_beat = 1_000.0 + 5 * MINUTE + 3
    for later in (60.0, 5 * MINUTE, popup.HUB_SILENT_SECONDS):
        watch.observe(dead, 0, 0)  # the same heartbeat again is no news
        assert watch.stopped_at(awake_at_beat + later) is None
    assert watch.stopped_at(awake_at_beat + popup.HUB_SILENT_SECONDS + 1) == SINCE + 15 * MINUTE
    # A new heartbeat (the Hub was restarted) clears it.
    watch.observe({"updated_at": SINCE + 40 * MINUTE}, SINCE + 40 * MINUTE, awake_at_beat + 20 * MINUTE)
    assert watch.stopped_at(awake_at_beat + 21 * MINUTE) is None
    assert popup.HUB_SILENT_SECONDS >= 10 * MINUTE  # an awake Hub went 292 s between writes


def test_a_missing_or_corrupt_status_is_no_news_from_the_hub():
    watch = popup.HubWatch(opened_wall=SINCE + 10 * MINUTE, opened_awake=500.0)
    for status in (None, {}, {"updated_at": "x"}):
        watch.observe(status, SINCE + 30 * MINUTE, 500.0 + 20 * MINUTE)
    assert watch.stopped_at(500.0 + 20 * MINUTE) == SINCE + 10 * MINUTE  # since the note opened


def test_a_heartbeat_from_the_future_is_shown_as_now():
    watch = popup.HubWatch(opened_wall=SINCE, opened_awake=0.0)
    watch.observe({"updated_at": SINCE + 99 * MINUTE}, SINCE + 20 * MINUTE, 0.0)
    assert watch.stopped_at(popup.HUB_SILENT_SECONDS + 1) == SINCE + 20 * MINUTE


def test_the_awake_clock_falls_back_to_monotonic_and_never_runs_backwards(monkeypatch):
    monkeypatch.setattr(popup.hub_status, "awake_seconds", lambda: None)
    assert popup.awake_clock(lambda: 42.0)() == 42.0
    readings = iter([100.0, 130.0, None, 160.0])
    monkeypatch.setattr(popup.hub_status, "awake_seconds", lambda: next(readings))
    clock = popup.awake_clock(lambda: -1.0)
    assert [clock(), clock(), clock()] == [130.0, 130.0, 160.0]


# ------------------------------------------------------------------ games

def test_game_running_uses_game_modes_test_and_never_fails():
    osu = game_mode.ProcessInfo(pid=7, exe=r"C:\Games\osu!\osu!.exe", create_time=SINCE, rss_bytes=10)
    with patch.object(game_mode, "list_processes", return_value=[osu]):
        assert popup.game_running(("osu!.exe",)) is True
        assert popup.game_running(()) is False
    with patch.object(game_mode, "list_processes", side_effect=RuntimeError("psutil broke")):
        assert popup.game_running(("osu!.exe",)) is False


def test_the_game_scan_runs_off_the_calling_thread_one_at_a_time():
    release, started, threads = threading.Event(), threading.Event(), []

    def scan():
        threads.append(threading.current_thread())
        started.set()
        assert release.wait(5)
        return True

    watch = popup.GameWatch(scan)
    assert watch.start_scan() is True
    assert started.wait(5)
    assert watch.start_scan() is False  # one in flight: no second thread
    assert watch.running is False
    release.set()
    threads[0].join(5)
    assert watch.running is True and threads[0] is not threading.current_thread()
    broken = popup.GameWatch(Mock(side_effect=OSError("scan failed")))
    broken.running = True
    broken.start_scan()
    for thread in threading.enumerate():
        if thread.name == "game-scan":
            thread.join(5)
    assert broken.running is False


class FakeRoot:
    def __init__(self, frame: str = "0x0") -> None:
        self.frame = frame
        self.calls: list[tuple] = []

    def wm_frame(self) -> str:
        return self.frame

    def withdraw(self) -> None:
        self.calls.append(("withdraw",))

    def deiconify(self) -> None:
        self.calls.append(("deiconify",))

    def attributes(self, *args) -> None:
        self.calls.append(("attributes",) + args)


def test_set_visible_falls_back_to_tk_when_there_is_no_real_window():
    root = FakeRoot("0x0")  # IsWindow(0) is false
    popup.set_visible(root, False)
    popup.set_visible(root, True)
    assert root.calls == [("withdraw",), ("deiconify",), ("attributes", "-topmost", True)]
    junk = FakeRoot("not a handle")
    popup.set_visible(junk, False)
    assert junk.calls == [("withdraw",)]


# ------------------------------------------------------------------ single instance

@pytest.mark.skipif(os.name != "nt", reason="the guard is a Windows named mutex")
def test_only_one_popup_holds_the_mutex_at_a_time():
    name = f"Local\\AWTRIX.PanelDownPopup.test.{uuid.uuid4().hex}"
    first, second, third = popup.SingleInstance(name), popup.SingleInstance(name), popup.SingleInstance(name)
    try:
        assert first.acquire() is True
        assert second.acquire() is False
        first.release()
        assert third.acquire() is True
    finally:
        for guard in (first, second, third):
            guard.release()


def test_the_real_mutex_name_is_session_local():
    assert popup.MUTEX_NAME.startswith("Local\\")


# ------------------------------------------------------------------ main

class Guard:
    def __init__(self, free: bool = True) -> None:
        self.free = free
        self.acquired = self.released = 0

    def acquire(self) -> bool:
        self.acquired += 1
        return self.free

    def release(self) -> None:
        self.released += 1


def no_tk():
    raise AssertionError("no window should be built")


def test_main_exits_without_a_window_when_the_panel_is_already_back(tmp_path):
    now = SINCE + 11 * MINUTE
    path = write_status(tmp_path / "hub_status.json", reachable=True, checked_at=now - 3)
    guard = Guard()
    code = popup.main(["--since", str(SINCE), "--status", str(path)],
                      tk_factory=no_tk, instance=guard, clock=lambda: now)
    assert code == 0 and guard.acquired == 0


def test_main_exits_when_another_popup_is_showing(tmp_path):
    guard = Guard(free=False)
    code = popup.main(["--since", str(SINCE), "--status", str(tmp_path / "missing.json")],
                      tk_factory=no_tk, instance=guard, clock=lambda: SINCE + 11 * MINUTE)
    assert code == 0 and guard.acquired == 1


def test_main_exits_quietly_without_tk(tmp_path, monkeypatch):
    guard = Guard()

    def broken_tk():
        raise RuntimeError("no display name and no $DISPLAY environment variable")

    args = ["--since", str(SINCE), "--status", str(tmp_path / "missing.json")]
    assert popup.main(args, tk_factory=broken_tk, instance=guard) == 0
    assert guard.released == 1

    monkeypatch.setitem(sys.modules, "tkinter", None)  # import tkinter -> ImportError
    assert popup.main(args, instance=Guard()) == 0


def test_since_must_be_a_number():
    for bad in ("nan", "inf", "soon"):
        with pytest.raises(SystemExit):
            popup.parse_args(["--since", bad])
    with pytest.raises(SystemExit):
        popup.parse_args([])


def test_status_defaults_to_the_hub_data_dir():
    args = popup.parse_args(["--since", "1791590000.5"])
    assert args.since == 1791590000.5
    assert args.status.name == "hub_status.json"


def test_popup_source_never_writes_or_talks_to_the_panel():
    source = Path(popup.__file__).read_text(encoding="utf-8")
    for forbidden in ("requests", "PanelClient", "urllib", "socket", "atomic_json",
                      ".write", "write_text", "write_bytes", "mkdir", "open(", "focus_force",
                      "grab_set"):
        assert forbidden not in source, forbidden
    assert "sys.dont_write_bytecode = True" in source


# ------------------------------------------------------------------ Tk window
#
# One Tk interpreter for the module and a Toplevel per test.  Under pytest's
# fd-level output capture, the second Tk() in one process (test_desk_controls
# makes the first) sometimes fails to initialise -- "couldn't read file
# init.tcl: No error", "invalid command name tcl_findLibrary" -- and the next
# attempt succeeds (measured 2026-10-09: 4 of 14 runs needed exactly one
# retry; with -s none did).  The real popup makes one Tk in a fresh process.

@pytest.fixture(scope="module")
def tk_app():
    tk = pytest.importorskip("tkinter")
    gc.collect()  # finalise any earlier test's interpreter before this one starts
    errors = []
    for _ in range(5):
        try:
            app = tk.Tk()
            break
        except tk.TclError as error:
            errors.append(str(error).splitlines()[0])
    else:
        pytest.skip(f"Tk unavailable: {errors}")
    app.withdraw()
    yield app
    try:
        app.destroy()
    except tk.TclError:
        pass


@pytest.fixture
def tk_root(tk_app):
    import tkinter as tk

    top = tk.Toplevel(tk_app)
    top.withdraw()
    yield top
    try:
        top.destroy()
    except tk.TclError:
        pass


class Clocks:
    def __init__(self) -> None:
        self.wall = SINCE + 10 * MINUTE
        self.mono = 100.0
        self.awake = 7_000.0

    def advance(self, seconds: float) -> None:
        self.wall += seconds
        self.awake += seconds


def build_window(root, tmp_path, clocks, **options):
    return popup.PopupWindow(root, SINCE, tmp_path / "hub_status.json",
                             clock=lambda: clocks.wall, monotonic=lambda: clocks.mono,
                             awake=lambda: clocks.awake, **options)


def test_window_shows_the_text_on_top_and_updates_the_minutes(tk_root, tmp_path):
    clocks = Clocks()
    window = build_window(tk_root, tmp_path, clocks)
    assert tk_root.title() == popup.TITLE
    assert int(tk_root.attributes("-topmost")) == 1
    assert "for 10 min (since 13:46)" in window.body.get()
    assert window.button.cget("text") == "Dismiss"
    clocks.wall += 30
    assert "for 10 min" in window.refresh_text()
    clocks.wall += 30
    assert "for 11 min" in window.refresh_text()
    assert window.body.get() == popup.body_text(SINCE, clocks.wall)
    assert window.check_status() is None and not window.closed
    assert not tk_root.winfo_ismapped()  # built hidden; only show() maps it


def test_window_closes_itself_when_the_hub_sees_the_panel_again(tk_root, tmp_path):
    clocks = Clocks()
    window = build_window(tk_root, tmp_path, clocks)
    write_status(tmp_path / "hub_status.json", reachable=False, checked_at=clocks.wall - 5)
    window._status_loop()
    assert not window.closed and tk_root.winfo_exists()
    clocks.wall += 30
    write_status(tmp_path / "hub_status.json", reachable=True, checked_at=clocks.wall - 2)
    window._status_loop()
    assert window.closed_because == "recovered"
    assert not tk_root.winfo_exists()
    window._text_loop()  # a late timer after closing is harmless
    window.close("dismissed")
    assert window.closed_because == "recovered"


def test_window_closes_after_twelve_hours(tk_root, tmp_path):
    clocks = Clocks()
    window = build_window(tk_root, tmp_path, clocks)
    clocks.mono += popup.MAX_OPEN_SECONDS - 1
    assert window.check_status() is None
    clocks.mono += 1
    assert window.check_status() == "expired"
    assert not tk_root.winfo_exists()


def test_window_dismiss_button_closes_it(tk_root, tmp_path):
    window = build_window(tk_root, tmp_path, Clocks())
    window.button.invoke()
    assert window.closed_because == "dismissed"
    assert not tk_root.winfo_exists()


def test_window_text_follows_the_failure_and_this_pcs_network(tk_root, tmp_path):
    clocks = Clocks()
    path = tmp_path / "hub_status.json"
    window = build_window(tk_root, tmp_path, clocks)
    write_status(path, reachable=False, checked_at=clocks.wall - 2, failure="protocol")
    window.check_status()
    assert "only with errors" in window.body.get()
    write_status(path, reachable=False, checked_at=clocks.wall - 1, pc_offline=True)
    window.check_status()
    assert window.body.get().startswith("This PC has lost its own network")
    path.write_bytes(b"{")  # a read caught mid-replace keeps what was known
    window.check_status()
    assert window.body.get().startswith("This PC has lost its own network")


def test_window_says_when_the_hub_stopped_and_stops_counting(tk_root, tmp_path):
    clocks = Clocks()
    path = write_status(tmp_path / "hub_status.json", reachable=False, checked_at=clocks.wall - 2)
    last_beat = clocks.wall - 2
    window = build_window(tk_root, tmp_path, clocks)
    window.check_status()
    clocks.advance(popup.HUB_SILENT_SECONDS - 30)  # a slow but living Hub
    window.check_status()
    assert window.body.get() == popup.body_text(SINCE, clocks.wall)
    clocks.advance(60)
    window.check_status()
    stopped = popup.body_text(SINCE, clocks.wall, hub_stopped_at=last_beat)
    assert window.body.get() == stopped and "stopped checking the LED panel at 13:56" in stopped
    for _ in range(3):  # 6 hours on, nothing counts up and nothing closes
        clocks.advance(2 * 60 * MINUTE)
        assert window.check_status() is None and window.refresh_text() == stopped
    # The Hub is back (restarted), the panel still down: counting resumes.
    write_status(path, reachable=False, checked_at=clocks.wall - 1)
    window.check_status()
    assert window.body.get() == popup.body_text(SINCE, clocks.wall)
    # The PC sleeps with the note open: on waking, before the Hub writes again,
    # it is not mistaken for a stopped Hub.
    clocks.wall += 8 * 60 * MINUTE
    window.check_status()
    assert window.body.get() == popup.body_text(SINCE, clocks.wall)


class FakeGames:
    def __init__(self) -> None:
        self.running = False
        self.scans = 0

    def start_scan(self) -> bool:
        self.scans += 1
        return True


def test_window_steps_aside_while_a_game_runs_and_returns_after(tk_root, tmp_path):
    clocks, games, shown = Clocks(), FakeGames(), []
    window = build_window(tk_root, tmp_path, clocks, games=games,
                          show_window=lambda root, visible: shown.append((root, visible)))
    assert "game" in window._jobs
    games.running = True
    window.check_status()
    assert shown == []  # not on screen yet: nothing to hide
    window.mapped = True  # as after show(), without putting a window on screen here
    window._game_loop()
    assert games.scans == 1
    window.check_status()
    window.check_status()
    assert shown == [(tk_root, False)] and window.hidden_for_game
    games.running = False
    window.check_status()
    assert shown == [(tk_root, False), (tk_root, True)] and not window.hidden_for_game
    games.running = True
    window.check_status()
    write_status(tmp_path / "hub_status.json", reachable=True, checked_at=clocks.wall - 1)
    assert window.check_status() == "recovered"  # still closes while hidden
    window._game_loop()
    assert games.scans == 1


def test_first_appearance_does_not_take_focus(tk_root, tmp_path, monkeypatch):
    # deiconify made the note the foreground window, so a person's typing went
    # into it and Escape dismissed it seconds after it appeared (2026-10-09/10).
    clocks, shown = Clocks(), []
    window = build_window(tk_root, tmp_path, clocks,
                          show_window=lambda root, visible: shown.append((root, visible)))
    calls = []
    monkeypatch.setattr(tk_root, "deiconify", lambda: calls.append("deiconify"))
    window.show()
    assert shown == [(tk_root, True)] and window.mapped
    assert calls == []


def test_main_hands_the_extra_games_to_the_game_watch(tmp_path, monkeypatch):
    built = {}

    class FakeWindow:
        def __init__(self, root, since, status, **options) -> None:
            built.update(options)

        def show(self) -> None:
            built["shown"] = True

    monkeypatch.setattr(popup, "PopupWindow", FakeWindow)
    root = Mock()
    code = popup.main(["--since", str(SINCE), "--status", str(tmp_path / "missing.json"),
                       "--extra-game=osu!.exe"], tk_factory=lambda: root, instance=Guard(),
                      clock=lambda: SINCE + 11 * MINUTE)
    assert code == 0 and built["shown"] and root.mainloop.called
    with patch.object(popup, "game_running", return_value=True) as scan:
        assert built["games"].scan() is True
    scan.assert_called_once_with(("osu!.exe",))
