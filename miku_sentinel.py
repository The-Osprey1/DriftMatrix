"""Miku Sentinel for AWTRIX -- is the voice assistant actually thinking?

She answered from the EchoBrain stub from 9 to 11 September and nobody
noticed for three days.  ``create_brain`` probes Ollama once at startup; the
scheduled task fires on logon with no delay and Ollama starts late from the
Startup folder, so she lost the race every morning and replied to every
question from a fallback that cannot reason.  The only trace was one line in
a log file nobody opens:

    brain  : echo  (waiting for Ollama, will switch when it answers)

That is the line this scene puts on the wall:

    LIVE    emerald   the real model is wired, with turns answered today
    ECHO    amber     she is up, but answering from the stub
    OFF     coral     she is not running at all

Everything is read from this machine: the process list, ``miku.log`` and
``metrics.jsonl`` under ``%LOCALAPPDATA%\\miku-jarvis``.  No network, no
credentials, nothing that leaves the desk.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any, Callable

from PIL import Image

from visual_system import (
    render_glance_lane,
    AMBER,
    BLACK,
    CORAL,
    EMERALD,
    FRAME_BEAT_MS,
    GUTTER_X,
    HEIGHT,
    ICE,
    ICON_HEIGHT,
    ICON_WIDTH,
    LANE_HEIGHT,
    LANE_WIDTH,
    LANE_X0,
    MAX_ICON_BYTES,
    MAX_PAYLOAD_BYTES,
    PEARL,
    VIOLET,
    cycle_length,
    draw_lane_rail,
    draw_text,
    measure_text,
    new_frame,
    put_pixel,
)

APP_NAME = "miku0"
PAGE_DURATION_MS = 7_000
LIFETIME_MS = 45_000
ICON_MAX_BYTES = MAX_ICON_BYTES
# Turns that fill the lane's meter.  Ordinary days in metrics.jsonl ran 2-17
# (Sept 2026), so a typical day lands around a third of the way along.
TURNS_METER_FULL = 50

MIKU_HOME = Path(os.environ.get("LOCALAPPDATA", "")) / "miku-jarvis"
LOG_PATH = MIKU_HOME / "miku.log"
METRICS_PATH = MIKU_HOME / "metrics.jsonl"
PRESENCE_PATH = MIKU_HOME / "presence.json"

# How long an active presence is believed. She writes on change, not on a
# timer, so "speaking" with no successor means the process died mid-sentence.
# A turn runs about eleven seconds and a held push-to-talk key longer, so the
# window is generous; what it rules out is a scene stuck on SPEAK for hours.
PRESENCE_TTL_SECONDS = 120.0

# Only the tail of each file is ever read.  miku.log is append-only and grows
# without bound, and this runs on a tick.
LOG_TAIL_BYTES = 64 * 1024
METRICS_TAIL_BYTES = 256 * 1024

# "brain  : ollama:llama3.1:8b  listener: ..." / "brain  : echo  (waiting ...)"
BRAIN_LINE = re.compile(r"brain\s*:\s*(\S+)")


class MikuState(str, Enum):
    """Her health: which brain is answering, or none at all."""

    LIVE = "live"
    ECHO = "echo"
    OFF = "off"


class MikuActivity(str, Enum):
    """What she is doing this second, independent of which brain is doing it."""

    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


@dataclass(frozen=True)
class MikuStatus:
    state: MikuState
    brain: str = ""
    turns_today: int = 0
    activity: MikuActivity = MikuActivity.IDLE


# ------------------------------------------------------------- Reading state

def _tail(path: Path, limit: int) -> str:
    """The last `limit` bytes of a file, decoded leniently.

    The file is being appended to by another process, so the first line of
    the slice is usually cut mid-way and the decode can land mid-codepoint.
    Both are dropped rather than raised.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > limit:
                handle.seek(size - limit)
                handle.readline()  # discard the partial first line
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def read_brain(log_path: Path | None = None) -> str:
    """The brain named by the most recent startup or recovery line.

    Scanned backwards: a recovery line ("recovered after 13s") comes after
    the startup line that said echo, and it is the later one that is true.
    """
    text = _tail(log_path or LOG_PATH, LOG_TAIL_BYTES)
    for line in reversed(text.splitlines()):
        found = BRAIN_LINE.search(line)
        if found:
            return found.group(1)
    return ""


def count_turns_today(metrics_path: Path | None = None, now: float | None = None) -> int:
    """Turns started since local midnight.

    Rows are appended by a live process, so the last line can be half
    written; a row that will not parse is skipped rather than counted.
    """
    text = _tail(metrics_path or METRICS_PATH, METRICS_TAIL_BYTES)
    if not text:
        return 0
    midnight = datetime.fromtimestamp(now or time.time()).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
    turns = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            started = json.loads(line).get("started_at")
        except (ValueError, AttributeError):
            continue
        if isinstance(started, (int, float)) and started >= midnight:
            turns += 1
    return turns


def miku_is_running() -> bool:
    """Is main.py alive on this machine?

    Import-guarded: the hub has to keep running on a box without psutil, and
    a sentinel that cannot see the process should say so rather than claim
    she is up.
    """
    try:
        import psutil
    except ImportError:
        return False
    for process in psutil.process_iter(["cmdline"]):
        try:
            cmdline = process.info.get("cmdline") or ()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        for argument in cmdline:
            if argument.endswith("main.py") and "miku-jarvis" in argument:
                return True
    return False


def read_activity(presence_path: Path | None = None, now: float | None = None) -> MikuActivity:
    """What she is doing, from the presence file she rewrites on every change.

    An active state older than the TTL is not believed: she publishes on
    change and nothing else, so a SPEAKING that was never succeeded by IDLE
    means the process went away mid-sentence. IDLE has no expiry -- being
    idle for six hours is still being idle.
    """
    text = _tail(presence_path or PRESENCE_PATH, 4096)
    if not text:
        return MikuActivity.IDLE
    try:
        row = json.loads(text)
        activity = MikuActivity(row["state"])
        written_at = float(row["at"])
    except (ValueError, KeyError, TypeError):
        return MikuActivity.IDLE
    if activity is MikuActivity.IDLE:
        return activity
    if (now or time.time()) - written_at > PRESENCE_TTL_SECONDS:
        return MikuActivity.IDLE
    return activity


def read_status(
    *,
    is_running: Callable[[], bool] | None = None,
    log_path: Path | None = None,
    metrics_path: Path | None = None,
    presence_path: Path | None = None,
    now: float | None = None,
) -> MikuStatus:
    running = (is_running or miku_is_running)()
    brain = read_brain(log_path)
    if not running:
        return MikuStatus(MikuState.OFF, brain=brain)
    # An empty brain means the log was unreadable, not that she is thinking.
    # Reporting LIVE off a missing file is exactly the blind spot this scene
    # exists to close, so an unknown brain reads as the fallback.
    if not brain or brain.startswith("echo"):
        return MikuStatus(MikuState.ECHO, brain=brain or "unknown")
    return MikuStatus(
        MikuState.LIVE,
        brain=brain,
        turns_today=count_turns_today(metrics_path, now),
        activity=read_activity(presence_path, now),
    )


# ------------------------------------------------------------------ Icon art

def draw_miku_avatar(
    frame: list[list[tuple[int, int, int]]],
    state: MikuState,
    phase: int,
    activity: MikuActivity = MikuActivity.IDLE,
) -> None:
    """An 8x8 twin-tailed avatar; the state is carried by the collar and eyes.

    Hair stays ICE in every state so it reads as the same character with
    something wrong, not as three different icons.  The animation is the
    tails swaying by one column -- at brightness 1 the panel is one bit per
    channel, so a hue shift collapses to nothing and only displacement
    survives.
    """
    p = phase % 4
    accent = {
        MikuState.LIVE: EMERALD,
        MikuState.ECHO: AMBER,
        MikuState.OFF: CORAL,
    }[state]

    # Fringe and crown.
    for x in range(2, 6):
        put_pixel(frame, x, 1, ICE)
        put_pixel(frame, x, 2, ICE)

    # Face.
    for y in range(3, 6):
        for x in range(2, 6):
            put_pixel(frame, x, y, PEARL)

    # Eyes.  OFF closes them into lids in the same cells the open pupils use,
    # which is the convention the companion's SLEEP mood already set.
    if state == MikuState.OFF:
        put_pixel(frame, 2, 4, VIOLET)
        put_pixel(frame, 5, 4, VIOLET)
    else:
        # A blink on one frame of four, and only while she is actually up.
        blinking = state == MikuState.LIVE and p == 3
        eye = VIOLET if blinking else accent
        put_pixel(frame, 2, 4, eye)
        put_pixel(frame, 5, 4, eye)

    # Collar: the state's colour, four pixels wide under the chin.
    for x in range(2, 6):
        put_pixel(frame, x, 6, accent)

    # Twin tails.  They hang straight when she is off -- a still icon is the
    # honest rendering of a process that is not running.
    sway = 0 if state == MikuState.OFF else (1 if p in (1, 2) else 0)
    for y in range(1, 7):
        put_pixel(frame, 1, y, ICE)
        put_pixel(frame, 6, y, ICE)
    put_pixel(frame, 0 + sway, 7, ICE)
    put_pixel(frame, 7 - sway, 7, ICE)

    # What she is doing, drawn last so it sits on top of the face. Only while
    # she is actually up: an OFF process is not listening to anything.
    if state is MikuState.OFF:
        return
    if activity is MikuActivity.LISTENING:
        # Sound arriving at both sides, rising a row and falling back. The
        # columns outside the tails are empty in every other state, so this
        # cannot be confused with the sway.
        y = 3 + (p % 2)
        put_pixel(frame, 0, y, accent)
        put_pixel(frame, 7, y, accent)
    elif activity is MikuActivity.THINKING:
        # One dot crossing the empty top row. Four distinct positions, so it
        # reads as travel rather than as a stuck pixel.
        put_pixel(frame, 2 + p, 0, accent)
    elif activity is MikuActivity.SPEAKING:
        # A mouth opening and closing on the face itself.
        put_pixel(frame, 3, 5, accent)
        if p % 2:
            put_pixel(frame, 4, 5, accent)


def build_miku_icon_gif(
    state: MikuState,
    animated: bool = True,
    activity: MikuActivity = MikuActivity.IDLE,
) -> bytes:
    frame_count = 4 if animated else 1
    frames = []
    for phase in range(frame_count):
        f = new_frame()
        draw_miku_avatar(f, state, phase, activity)
        frames.append(f)

    # OFF is one frame drawn four times; paying for the repeats buys a viewer
    # nothing and costs the icon budget.
    frame_count = cycle_length(frames)
    images: list[Image.Image] = []
    for f in frames[:frame_count]:
        img = Image.new("RGB", (ICON_WIDTH, ICON_HEIGHT), BLACK)
        img.putdata([f[y][x] for y in range(ICON_HEIGHT) for x in range(ICON_WIDTH)])
        images.append(img)

    stream = BytesIO()
    if animated and frame_count > 1:
        images[0].save(
            stream,
            format="GIF",
            save_all=True,
            append_images=images[1:],
            duration=[FRAME_BEAT_MS] * frame_count,
            loop=0,
            disposal=1,
            optimize=False,
        )
    else:
        images[0].save(stream, format="GIF", optimize=True)

    data = stream.getvalue()
    if len(data) > ICON_MAX_BYTES:
        raise ValueError("Miku icon exceeds byte limit")
    return data


# ------------------------------------------------------------------ Data lane

# Every banner has to fit the 21px text budget of a 23px lane, and the first
# set written here did not: "MIKU UP" measures 27px, "MIKU OFF" 31px. The
# lane trims to fit rather than overflowing, so all three states rendered as
# the same truncated "MIKU " and the scene said nothing at all. The font is
# proportional, so this is not a character count -- it is measured, and
# test_every_banner_fits_the_lane measures it too.
BANNERS: dict[MikuActivity, str] = {
    MikuActivity.LISTENING: "HEAR",
    MikuActivity.THINKING: "THINK",
    MikuActivity.SPEAKING: "SPEAK",
}


def lane_text(status: MikuStatus) -> tuple[str, tuple[int, int, int]]:
    if status.state is MikuState.OFF:
        return "OFF", CORAL
    if status.state is MikuState.ECHO:
        return "ECHO", AMBER
    return BANNERS.get(status.activity, "LIVE"), EMERALD


def render_miku_lane(status: MikuStatus) -> list[list[tuple[int, int, int]]]:
    # Calm glance: the banner in white; the state's colour lives in the icon.
    # The meter is today's turns on a TURNS_METER_FULL scale.  Only LIVE
    # counts turns (read_status skips the metrics file otherwise), so ECHO,
    # OFF and a turn-less LIVE keep the plain line rather than a fake zero.
    turns = status.turns_today if status.state is MikuState.LIVE else 0
    if turns <= 0:
        return render_glance_lane(lane_text(status)[0])
    return render_glance_lane(
        lane_text(status)[0], meter=turns / TURNS_METER_FULL, level="good")


def build_miku_payload(status: MikuStatus, animated: bool = True) -> dict:
    icon_gif = build_miku_icon_gif(status.state, animated=animated, activity=status.activity)
    frame = render_miku_lane(status)
    raw = bytes(
        ch
        for y in range(HEIGHT)
        for x in range(LANE_X0, LANE_X0 + LANE_WIDTH)
        for ch in frame[y][x]
    )
    lane_b64 = base64.b64encode(raw).decode("ascii")

    payload = {
        "icon": base64.b64encode(icon_gif).decode("ascii"),
        "iconMode": "fixed",
        "iconOffsetX": 0,
        "draw": [["bitmap", LANE_X0, 0, LANE_WIDTH, LANE_HEIGHT, lane_b64]],
        "backgroundColor": "#000000",
        "durationMs": PAGE_DURATION_MS,
        "lifetimeMs": LIFETIME_MS,
        "lifetimeExpiry": "remove",
    }
    raw_json = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(raw_json) >= MAX_PAYLOAD_BYTES:
        raise ValueError("Miku payload exceeded 2KB safety limit")
    return payload


# ----------------------------------------------------------------- Controller

@dataclass(frozen=True)
class MikuPolicy:
    enabled: bool = False
    refresh_seconds: float = 30.0


@dataclass(frozen=True)
class MikuSentinelTick:
    status: MikuStatus
    pushed: bool = False


class MikuSentinelController:
    """Keeps the panel's picture of the assistant current."""

    def __init__(
        self,
        panel: Any,
        policy: MikuPolicy | None = None,
        *,
        status_reader: Callable[[], MikuStatus] | None = None,
        clock: Any | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.panel = panel
        self.policy = policy or MikuPolicy()
        self.status_reader = status_reader or read_status
        self.clock = clock or time
        self.logger = logger or logging.getLogger("awtrix.utility_hub.miku")
        self.last_push_at: float | None = None
        self.current_state: tuple[MikuState, MikuActivity] | None = None

    def tick(self) -> MikuSentinelTick:
        idle = MikuStatus(MikuState.OFF)
        if not self.policy.enabled:
            return MikuSentinelTick(status=idle, pushed=False)

        now = getattr(self.clock, "monotonic", time.monotonic)()
        try:
            status = self.status_reader()
        except Exception as err:  # noqa: BLE001 - a tick must not kill the hub
            self.logger.warning("Failed to read Miku status: %s", err)
            return MikuSentinelTick(status=idle, pushed=False)

        # Miku no longer starts at logon; she runs only when turned on by
        # hand. While she is off the page is taken off the panel rather than
        # shown as a coral OFF badge, and it comes back on her next push.
        if status.state is MikuState.OFF:
            if self.current_state is not None and self.current_state[0] is MikuState.OFF:
                return MikuSentinelTick(status=status, pushed=False)
            try:
                self.panel.delete_pushed_app(APP_NAME)
                self.current_state = (status.state, status.activity)
                self.last_push_at = None
                self.logger.info("Miku is off; page removed from the panel")
            except Exception as err:  # noqa: BLE001
                self.logger.warning("Failed to remove Miku sentinel: %s", err)
            return MikuSentinelTick(status=status, pushed=False)

        # Activity counts as a change: the whole point of reading presence is
        # that HEAR -> THINK -> SPEAK reaches the panel while it is happening,
        # not at the next refresh interval.
        current = (status.state, status.activity)
        needs_push = (
            self.current_state != current
            or self.last_push_at is None
            or (now - self.last_push_at) >= self.policy.refresh_seconds
        )
        pushed = False
        if needs_push:
            changed = self.current_state != current
            try:
                self.panel.push_app(APP_NAME, build_miku_payload(status))
                self.last_push_at = now
                # The whole tuple. Storing only the state while comparing the
                # pair made them unequal on every tick, so the scene pushed
                # every three seconds instead of on change -- twenty times the
                # panel traffic it asked for, and invisible, because pushing
                # too often looks exactly like pushing correctly.
                self.current_state = current
                pushed = True
                # Logged only on an actual change. This scene reads two files
                # and a process list to decide what to show, and when it shows
                # the wrong thing the log is the only way to tell which of the
                # three it got wrong.
                if changed:
                    self.logger.info(
                        "Miku is %s (%s), activity %s, %d turns today",
                        status.state.value, status.brain or "unknown",
                        status.activity.value, status.turns_today)
            except Exception as err:  # noqa: BLE001
                self.logger.warning("Failed to push Miku sentinel: %s", err)

        return MikuSentinelTick(status=status, pushed=pushed)
