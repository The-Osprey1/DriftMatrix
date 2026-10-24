"""A small always-on-top note that the AWTRIX panel has stopped answering.

Started by ``panel_down_alert.PanelDownAlert`` (a Utility Hub service) once
per long outage, as its own detached process:

    pythonw panel_down_popup.py --since <epoch> [--status <hub_status.json>]
                                [--extra-game=<exe> ...]

It never writes anything and never contacts the panel.  It only reads
``hub_status.json``, which the Hub rewrites on every tick, and closes itself
when the Hub reports the panel reachable again (a fresh check newer than
``--since``), when Dismiss is pressed, or after 12 hours.

What it says follows the status file.  No answer at all: most likely the
panel's Wi-Fi, so unplug it.  Answers that are only errors: unplug it to
restart it.  This PC's own network down (the Network Sentinel cannot reach the
router): check the PC first.  And once the Hub has written nothing for
``HUB_SILENT_SECONDS`` of awake time (it stopped or crashed), that the Hub
stopped checking, and when, with the count frozen there instead of growing.

While a game runs (Game Mode's own test, run off the Tk thread every
``GAME_CHECK_SECONDS``) the window is hidden, and it comes back without
taking focus once the game exits.  Without Tk it exits 0 quietly; if another
copy is already showing (a named mutex on Windows, nothing on disk) it exits
too.
"""

from __future__ import annotations

import sys

if __name__ == "__main__":
    sys.dont_write_bytecode = True  # writes nothing, not even __pycache__

import argparse
import ctypes
from datetime import datetime
import math
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable

import hub_status

TITLE = "AWTRIX panel not responding"
STATUS_POLL_SECONDS = 5.0      # a one-probe recovery lasts 30 s; 5 s polling sees it
TEXT_REFRESH_SECONDS = 30.0
MAX_OPEN_SECONDS = 12 * 60 * 60
# No new status for this long, in awake time: the Hub has stopped. During an
# outage an awake Hub legitimately goes up to ~292 s between status writes
# (2026-10-09: its panel writes wait out timeouts inside each tick).
HUB_SILENT_SECONDS = 10 * 60.0
# The process scan takes 1.6-2.5 s on this PC (405 processes), so it runs on
# a worker thread, and not often: a game starting takes longer than this.
GAME_CHECK_SECONDS = 45.0
MUTEX_NAME = "Local\\AWTRIX.PanelDownPopup"
MARGIN = 16
CAPTION_ESTIMATE = 32          # until the real frame size is known after mapping

BACKGROUND = "#10151e"         # the Desk Controls palette
FOREGROUND = "#ecf1f8"
MUTED = "#aab9cf"
BUTTON = "#202c3a"
BUTTON_ACTIVE = "#2d3d50"

# hub_status.FAILURE_* values meaning the panel answered, but wrongly.
ANSWERED_WITH_ERRORS = frozenset({hub_status.FAILURE_HTTP, hub_status.FAILURE_PROTOCOL})

ERROR_ACCESS_DENIED = 5
ERROR_ALREADY_EXISTS = 183
SPI_GETWORKAREA = 0x0030
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010


# ------------------------------------------------------------------ logic

def minutes_down(since: float, now: float) -> int:
    return max(0, int((now - since) // 60))


def since_label(since: float) -> str:
    return datetime.fromtimestamp(since).strftime("%H:%M")


def body_text(
    since: float,
    now: float,
    *,
    failure: str | None = None,
    pc_offline: bool = False,
    hub_stopped_at: float | None = None,
) -> str:
    """The note's text: what is known, and only the advice that fits it."""
    if hub_stopped_at is not None:
        return (
            f"The Utility Hub stopped checking the LED panel at {since_label(hub_stopped_at)}, "
            "so this note cannot tell whether the panel is back. When it last checked, the "
            f"panel had been failing for {minutes_down(since, hub_stopped_at)} min "
            f"(since {since_label(since)})."
        )
    if pc_offline:
        return (
            "This PC has lost its own network, so it cannot reach the LED panel either "
            f"(no answer since {since_label(since)}). Check the PC's Wi-Fi first; "
            "the panel may be fine."
        )
    if failure in ANSWERED_WITH_ERRORS:
        return (
            f"The LED panel has not worked for {minutes_down(since, now)} min "
            f"(since {since_label(since)}): it answers, but only with errors. "
            "Unplug the panel for 5 seconds and plug it back in to restart it."
        )
    return (
        f"The LED panel has not answered for {minutes_down(since, now)} min "
        f"(since {since_label(since)}). It is most likely its Wi-Fi. "
        "Unplug the panel for 5 seconds and plug it back in."
    )


def panel_recovered(status: Any, since: float, now: float) -> bool:
    """The Hub saw the panel answer, recently, after this outage began."""
    if not isinstance(status, dict) or status.get("panel_reachable") is not True:
        return False
    checked = status.get("panel_checked_at")
    return hub_status.fresh(checked, now) and checked > since


def close_reason(status: Any, since: float, now: float, open_seconds: float) -> str | None:
    if open_seconds >= MAX_OPEN_SECONDS:
        return "expired"
    if panel_recovered(status, since, now):
        return "recovered"
    return None


def heartbeat(status: Any) -> float | None:
    """The Hub's ``updated_at``: it changes on every Hub tick."""
    value = status.get("updated_at") if isinstance(status, dict) else None
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def status_failure(status: Any) -> str | None:
    value = status.get("panel_failure") if isinstance(status, dict) else None
    return value if isinstance(value, str) else None


def status_pc_offline(status: Any) -> bool:
    return isinstance(status, dict) and status.get("pc_offline") is True


def read_status(path: Path) -> dict | None:
    try:
        return hub_status.read_status(path)
    except Exception:  # noqa: BLE001 - an unreadable status is simply no news
        return None


class HubWatch:
    """Notices the Hub going quiet: no new heartbeat for HUB_SILENT_SECONDS of awake time.

    Awake time, so a PC that slept with the note open does not read as a
    stopped Hub on waking. A missing or unreadable status file is no news,
    the same as an old one. Until the first heartbeat, the time the note
    opened stands in (the Hub had just started it).
    """

    def __init__(self, opened_wall: float, opened_awake: float) -> None:
        self.beat: float | None = None
        self.heard_wall = opened_wall    # wall time of the newest news from the Hub
        self.heard_awake = opened_awake  # awake clock when it arrived

    def observe(self, status: Any, wall_now: float, awake_now: float) -> None:
        beat = heartbeat(status)
        if beat is not None and beat != self.beat:
            self.beat = beat
            self.heard_wall = min(beat, wall_now)
            self.heard_awake = awake_now

    def stopped_at(self, awake_now: float) -> float | None:
        """When the Hub was last heard from, once it has been quiet too long; else None."""
        return self.heard_wall if awake_now - self.heard_awake > HUB_SILENT_SECONDS else None


def awake_clock(monotonic: Callable[[], float] = time.monotonic) -> Callable[[], float]:
    """Awake seconds (``hub_status.awake_seconds``), else ``monotonic`` if there is none.

    Should a reading fail later, the last one is repeated: time standing still
    can only delay the "Hub stopped" text, never bring it early.
    """
    first = hub_status.awake_seconds()
    if first is None:
        return monotonic
    last = [first]

    def read() -> float:
        value = hub_status.awake_seconds()
        if value is not None:
            last[0] = value
        return last[0]

    return read


def game_running(extra_games: tuple[str, ...] = ()) -> bool:
    """Game Mode's own test; any failure counts as "no game", never hiding the note."""
    try:
        import game_mode

        return game_mode.detect_game(game_mode.list_processes(), extra_games) is not None
    except Exception:  # noqa: BLE001
        return False


class GameWatch:
    """The slow process scan on a worker thread, one at a time; Tk reads ``running``."""

    def __init__(self, scan: Callable[[], bool]) -> None:
        self.scan = scan
        self.running = False
        self._busy = False
        self._lock = threading.Lock()

    def start_scan(self) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        try:
            threading.Thread(target=self._run, name="game-scan", daemon=True).start()
        except RuntimeError:
            with self._lock:
                self._busy = False
            return False
        return True

    def _run(self) -> None:
        try:
            result = bool(self.scan())
        except Exception:  # noqa: BLE001 - a broken scan is "no game"
            result = False
        with self._lock:
            self.running = result
            self._busy = False


class SingleInstance:
    """A named mutex held for the life of the popup; nothing touches the disk.

    Off Windows there is no guard (the popup only runs on this Windows PC).
    """

    def __init__(self, name: str = MUTEX_NAME) -> None:
        self.name = name
        self.handle = None
        self._kernel32 = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateMutexW(None, False, self.name)
        error = ctypes.get_last_error()
        if not handle:
            # Access denied means it exists under another owner; any other
            # failure says nothing about a second popup, so show this one.
            return error != ERROR_ACCESS_DENIED
        if error == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self.handle, self._kernel32 = handle, kernel32
        return True

    def release(self) -> None:
        if self.handle and self._kernel32 is not None:
            self._kernel32.CloseHandle(self.handle)
        self.handle = None


def work_area(root: Any) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of the primary screen minus the taskbar."""
    if os.name == "nt":
        try:
            from ctypes import wintypes

            rect = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
                return rect.left, rect.top, rect.right, rect.bottom
        except (AttributeError, OSError):
            pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight() - 48


def _set_visible_win32(root: Any, visible: bool) -> bool:
    """ShowWindow on the frame: SW_HIDE, or SW_SHOWNOACTIVATE and back to topmost.

    In a 2026-10-09 test with an off-screen window, Tk's own deiconify made
    the window the foreground window; this did not, and Tk's state followed
    it (withdrawn, then normal). Taking focus is the one thing not to do to
    someone who has just left a game.
    """
    try:
        from ctypes import wintypes

        hwnd = int(root.wm_frame(), 16)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.IsWindow.argtypes = (wintypes.HWND,)
        user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, wintypes.UINT)
        if not user32.IsWindow(hwnd):
            return False
        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE if visible else SW_HIDE)
        if visible:
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        return True
    except Exception:  # noqa: BLE001 - fall back to Tk
        return False


def set_visible(root: Any, visible: bool) -> None:
    """Hide the note while a game runs, or bring it back without activating it."""
    if os.name == "nt" and _set_visible_win32(root, visible):
        return
    if visible:
        root.deiconify()
        root.attributes("-topmost", True)
    else:
        root.withdraw()


# ------------------------------------------------------------------ window

class PopupWindow:
    """The Tk side: build, place bottom-right, poll, close.  No grab, no focus tricks."""

    def __init__(
        self,
        root: Any,
        since: float,
        status_path: Path,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        awake: Callable[[], float] | None = None,
        reader: Callable[[Path], dict | None] = read_status,
        games: GameWatch | None = None,
        show_window: Callable[[Any, bool], None] = set_visible,
    ) -> None:
        import tkinter as tk

        self.root = root
        self.since = since
        self.status_path = status_path
        self.clock = clock
        self.monotonic = monotonic
        self.awake = awake if awake is not None else awake_clock(monotonic)
        self.reader = reader
        self.games = games
        self.show_window = show_window
        self.opened_at = monotonic()
        self.closed_because: str | None = None
        self.hub = HubWatch(clock(), self.awake())
        self.failure: str | None = None
        self.pc_offline = False
        self.mapped = False
        self.hidden_for_game = False
        self._jobs: dict[str, Any] = {}

        root.title(TITLE)
        root.resizable(False, False)
        root.configure(background=BACKGROUND)
        try:
            root.attributes("-toolwindow", True)
        except tk.TclError:
            pass  # Windows-only attribute
        root.attributes("-topmost", True)
        root.protocol("WM_DELETE_WINDOW", self.dismiss)
        root.bind("<Escape>", lambda _event: self.dismiss())

        frame = tk.Frame(root, background=BACKGROUND, padx=18, pady=14)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text=TITLE, font=("Segoe UI Semibold", 12), foreground=FOREGROUND,
                 background=BACKGROUND, anchor="w").pack(fill="x")
        self.body = tk.StringVar(master=root, value=self.message())
        tk.Label(frame, textvariable=self.body, font=("Segoe UI", 10), foreground=MUTED,
                 background=BACKGROUND, wraplength=330, justify="left",
                 anchor="w").pack(fill="x", pady=(6, 12))
        self.button = tk.Button(
            frame, text="Dismiss", command=self.dismiss, font=("Segoe UI", 10),
            foreground=FOREGROUND, background=BUTTON, activeforeground=FOREGROUND,
            activebackground=BUTTON_ACTIVE, relief="flat", borderwidth=0, padx=14, pady=5,
        )
        self.button.pack(anchor="e")

        self._schedule("status", STATUS_POLL_SECONDS, self._status_loop)
        self._schedule("text", TEXT_REFRESH_SECONDS, self._text_loop)
        if games is not None:
            self._schedule("game", GAME_CHECK_SECONDS, self._game_loop)

    @property
    def closed(self) -> bool:
        return self.closed_because is not None

    def _schedule(self, name: str, seconds: float, callback: Callable[[], None]) -> None:
        self._jobs[name] = self.root.after(int(seconds * 1000), callback)

    def show(self) -> None:
        """Place the window bottom-right above the taskbar and map it."""
        root = self.root
        root.update_idletasks()
        left, top, right, bottom = work_area(root)
        x = max(left, right - root.winfo_reqwidth() - MARGIN)
        y = max(top, bottom - root.winfo_reqheight() - MARGIN - CAPTION_ESTIMATE)
        root.geometry(f"+{x}+{y}")
        # Not deiconify: that makes the note the foreground window, so whatever
        # the person was typing lands in it -- and Escape dismisses it. On
        # 2026-10-09/10 it closed itself 4, 20 and 38 s after appearing, each
        # time with someone at the keyboard. Shown without activation, it can
        # only be dismissed on purpose.
        self.show_window(root, True)
        self.mapped = True
        root.after(150, self._settle)

    def _settle(self) -> None:
        """Correct the estimate with the real frame once Windows has drawn it."""
        if self.closed:
            return
        root = self.root
        try:
            border = max(0, root.winfo_rootx() - root.winfo_x())
            caption = max(0, root.winfo_rooty() - root.winfo_y())
            width = root.winfo_width() + 2 * border
            height = root.winfo_height() + caption + border
            left, top, right, bottom = work_area(root)
            root.geometry(f"+{max(left, right - width - MARGIN)}+{max(top, bottom - height - MARGIN)}")
        except Exception:  # noqa: BLE001 - a slightly misplaced window is fine
            pass

    def message(self) -> str:
        return body_text(self.since, self.clock(), failure=self.failure, pc_offline=self.pc_offline,
                         hub_stopped_at=self.hub.stopped_at(self.awake()))

    def check_status(self) -> str | None:
        status = self.reader(self.status_path)
        now = self.clock()
        reason = close_reason(status, self.since, now, self.monotonic() - self.opened_at)
        if reason is not None:
            self.close(reason)
            return reason
        self.hub.observe(status, now, self.awake())
        if status is not None:  # an unreadable file keeps what was known
            self.failure = status_failure(status)
            self.pc_offline = status_pc_offline(status)
        self.refresh_text()
        self.apply_game()
        return None

    def refresh_text(self) -> str:
        text = self.message()
        if text != self.body.get():
            self.body.set(text)
        return text

    def apply_game(self) -> None:
        """Step aside while a game runs; come back, unfocused, when it ends."""
        if self.games is None or not self.mapped or self.closed:
            return
        running = self.games.running
        if running == self.hidden_for_game:
            return
        self.hidden_for_game = running
        self.show_window(self.root, not running)

    def _status_loop(self) -> None:
        if self.closed or self.check_status() is not None:
            return
        self._schedule("status", STATUS_POLL_SECONDS, self._status_loop)

    def _text_loop(self) -> None:
        if self.closed:
            return
        self.refresh_text()
        self._schedule("text", TEXT_REFRESH_SECONDS, self._text_loop)

    def _game_loop(self) -> None:
        """Start a scan off the Tk thread; the status loop applies its result."""
        if self.closed or self.games is None:
            return
        self.games.start_scan()
        self._schedule("game", GAME_CHECK_SECONDS, self._game_loop)

    def dismiss(self) -> None:
        self.close("dismissed")

    def close(self, reason: str) -> None:
        if self.closed:
            return
        self.closed_because = reason
        for job in self._jobs.values():
            try:
                self.root.after_cancel(job)
            except Exception:  # noqa: BLE001
                pass
        self._jobs.clear()
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001 - already gone
            pass


# ------------------------------------------------------------------ entry

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show that the AWTRIX panel is not answering.")
    parser.add_argument("--since", type=float, required=True,
                        help="epoch seconds of the first failed probe")
    parser.add_argument("--status", type=Path,
                        default=hub_status.default_data_dir() / hub_status.STATUS_NAME,
                        help="the Hub's hub_status.json")
    parser.add_argument("--extra-game", dest="extra_games", action="append", default=[],
                        metavar="EXE", help="Game Mode's extra_games, one per flag")
    args = parser.parse_args(argv)
    if not math.isfinite(args.since):
        parser.error("--since must be a finite epoch time")
    return args


def main(
    argv: list[str] | None = None,
    *,
    tk_factory: Callable[[], Any] | None = None,
    instance: SingleInstance | None = None,
    clock: Callable[[], float] = time.time,
    reader: Callable[[Path], dict | None] = read_status,
) -> int:
    args = parse_args(argv)
    if close_reason(reader(args.status), args.since, clock(), 0.0) is not None:
        return 0  # the panel came back before the window could open
    guard = instance if instance is not None else SingleInstance()
    if not guard.acquire():
        return 0  # one is already showing
    try:
        try:
            if tk_factory is None:
                import tkinter

                tk_factory = tkinter.Tk
            root = tk_factory()
        except Exception:  # noqa: BLE001 - no Tk, no display: stay quiet
            return 0
        root.withdraw()
        extra_games = tuple(args.extra_games)
        window = PopupWindow(root, args.since, args.status, clock=clock, reader=reader,
                             games=GameWatch(lambda: game_running(extra_games)))
        window.show()
        root.mainloop()
        return 0
    finally:
        guard.release()


if __name__ == "__main__":
    sys.exit(main())
