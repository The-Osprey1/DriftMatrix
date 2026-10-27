"""Reactive Desk Companion for AWTRIX.

An ambient pixel companion that adapts its colour, pose, and mood label
to what you're doing right now:
- Focus Sprint running: focused avatar with deep work glasses and FOCUS banner.
- Focus Break running: relaxing avatar with a steaming beverage and REST banner.
- On-Air Meeting: avatar with broadcast headset and ON AIR banner.
- Quiet Hours: resting avatar with a rocking nightcap and slower breathing.
- Idle / Free: cheerful companion breathing and looking around.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, time as wall_time
from enum import Enum
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Callable

from PIL import Image

from icon_library import COMPANION_BEAT_MS, COMPANION_FRAME_MS, COMPANION_PALETTES, companion_icon

try:
    import panel_client
except ImportError:
    panel_client = None

try:
    import meeting_sentinel
except ImportError:
    meeting_sentinel = None

try:
    import focus_sprint
except ImportError:
    focus_sprint = None

try:
    import focus_control
except ImportError:
    focus_control = None

try:
    import desk_reminder
except ImportError:
    desk_reminder = None

from visual_system import (
    render_glance_lane,
    AMBER,
    BLACK,
    CORAL,
    EMERALD,
    FRAME_BEAT_MS,
    GOLD,
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
    RED_HOT,
    TRACK,
    VIOLET,
    WIDTH,
    draw_lane_rail,
    draw_text,
    new_frame,
    put_pixel,
    cycle_length,
    measure_text,
    gamma_lerp,
    scale_color,
)

APP_NAME = "companion0"
PAGE_DURATION_MS = 7_000
# The page is re-pushed only when the mood or text changes, plus a slow
# keep-alive so it never expires while the hub runs.  Until 2026-10-04 it
# was re-pushed every 15 s with a 45 s lifetime -- ~240 identical GIF
# decodes an hour on a panel at 85% script heap, and the panic at 14:51
# that day landed on one of those pushes.  If the hub dies, the page
# lingers at most LIFETIME_MS.
LIFETIME_MS = 600_000
KEEPALIVE_SECONDS = 240.0
ICON_MAX_BYTES = MAX_ICON_BYTES


class CompanionMood(str, Enum):
    IDLE = "idle"
    FOCUS = "focus"
    REST = "rest"
    ON_AIR = "on_air"
    SLEEP = "sleep"


# ---------------------------------------------------------------- Avatar Art

def draw_companion_avatar(frame: list[list[tuple[int, int, int]]], mood: CompanionMood, phase: int) -> None:
    """Draw 8x8 animated companion avatar tailored to current mood.

    Delegates to icon_library.companion_icon() for centralized token rendering,
    retaining this function signature for backward compatibility.
    """
    mood_str = mood.value if hasattr(mood, "value") else str(mood)
    frames = companion_icon(mood_str)
    src = frames[phase % len(frames)]
    for y in range(min(ICON_HEIGHT, len(frame))):
        for x in range(min(ICON_WIDTH, len(frame[y]))):
            frame[y][x] = src[y][x]


def build_companion_icon_gif(mood: CompanionMood, animated: bool = True) -> bytes:
    """Encode exact sprite colours once, shared by all eight breathing poses.

    A shared palette avoids repeating a colour table for each small frame,
    leaving useful room under the pushed app's 2 KB limit without dropping
    poses, quantizing away the shading, or allocating a full-panel animation.
    """
    mood_str = mood.value if hasattr(mood, "value") else str(mood)
    frames = companion_icon(mood_str)
    if not animated:
        frames = frames[:1]
    colors = list(dict.fromkeys(px for frame in frames for row in frame for px in row[:ICON_WIDTH]))
    if len(colors) > 256:
        raise ValueError("Companion artwork exceeded the GIF palette limit")
    palette = bytes(channel for color in colors for channel in color)
    color_index = {color: index for index, color in enumerate(colors)}
    images = []
    for frame in frames:
        image = Image.new("P", (ICON_WIDTH, ICON_HEIGHT))
        image.putpalette(palette)
        image.putdata([color_index[px] for row in frame[:ICON_HEIGHT] for px in row[:ICON_WIDTH]])
        images.append(image)
    stream = BytesIO()
    images[0].save(
        stream, format="GIF", save_all=animated, append_images=images[1:],
        duration=list(COMPANION_FRAME_MS) if animated else COMPANION_BEAT_MS,
        loop=0, disposal=1, optimize=False,
        palette=palette,
    )
    data = stream.getvalue()
    if len(data) > ICON_MAX_BYTES:
        raise ValueError("Companion icon exceeded the 1 KB safety limit")
    return data


# ---------------------------------------------------------------- Data Lane

def render_companion_lane(mood: CompanionMood, custom_text: str | None = None) -> list[list[tuple[int, int, int]]]:
    """Pearl mood text over an unmetered ribbon in the sprite's own palette."""
    mood_labels = {
        CompanionMood.FOCUS: "FOCUS",
        CompanionMood.REST: "REST",
        CompanionMood.ON_AIR: "ON AIR",
        CompanionMood.SLEEP: "SLEEP",
        CompanionMood.IDLE: "READY",
    }
    banner = mood_labels.get(mood, "READY")
    if custom_text and custom_text.strip():
        # Truncate by MEASURED WIDTH; the pre-clamp by characters keeps the
        # trim loop linear for pathological input.
        banner = custom_text.strip().upper()[:LANE_WIDTH]
        while banner and measure_text(banner) > LANE_WIDTH:
            banner = banner[:-1]
    frame = render_glance_lane(banner or "READY")
    mood_str = mood.value if hasattr(mood, "value") else str(mood)
    tones = COMPANION_PALETTES.get(mood_str, COMPANION_PALETTES["idle"])
    # This is a mood, not progress. Keep the entire ribbon below the data-fill
    # threshold, with no leader or fabricated completion fraction.
    for offset in range(LANE_WIDTH):
        color = gamma_lerp(tones["B"], tones["F"], offset / (LANE_WIDTH - 1))
        put_pixel(frame, LANE_X0 + offset, 7, scale_color(color, 0.32))
    return frame


def build_companion_payload(mood: CompanionMood, custom_text: str | None = None, animated: bool = True) -> dict:
    """Build compliant AWTRIX JSON payload."""
    icon_gif = build_companion_icon_gif(mood, animated=animated)
    frame = render_companion_lane(mood, custom_text=custom_text)
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
        raise ValueError("Companion payload exceeded 2KB safety limit")
    return payload


# ---------------------------------------------------------------- System State Detector

def detect_system_companion_mood(
    state_dir: Path | str | None = None,
    now: float | datetime | None = None,
    *,
    quiet_start: Any | None = None,
    quiet_end: Any | None = None,
) -> tuple[CompanionMood, str | None]:
    """Detect current desk companion mood and contextual lane text based on system state.

    Evaluation precedence:
    1. Quiet Hours: is_quiet_time() active -> CompanionMood.SLEEP ("SLEEP")
    2. Microphone Registry: active mic session -> CompanionMood.ON_AIR ("ON AIR")
    3. Focus Sprint: running/paused focus -> CompanionMood.FOCUS ("FOCUS")
                    running/paused break -> CompanionMood.REST ("REST")
    4. Ergonomic Reminders: due desk reminder -> CompanionMood.REST (with custom label)
    5. Default / Fallback: idle state -> CompanionMood.IDLE ("READY")
    """
    if now is None:
        now_dt = datetime.now()
        now_ts = time.time()
    elif isinstance(now, datetime):
        now_dt = now
        now_ts = now.timestamp()
    else:
        now_ts = float(now)
        try:
            now_dt = datetime.fromtimestamp(now_ts)
        except (OSError, OverflowError, ValueError):
            now_dt = datetime.now()

    # 1. Quiet hours check
    if panel_client is not None:
        try:
            in_quiet = False
            if quiet_start is not None and quiet_end is not None:
                in_quiet = panel_client.is_quiet_time(now_dt, start=quiet_start, end=quiet_end)
            elif quiet_start is not None or quiet_end is not None:
                in_quiet = panel_client.is_quiet_time(
                    now_dt,
                    start=quiet_start or wall_time(21, 30),
                    end=quiet_end or wall_time(7, 0),
                )
            else:
                in_quiet = panel_client.is_quiet_time(now_dt)
            if in_quiet:
                return (CompanionMood.SLEEP, "SLEEP")
        except (OSError, ImportError, Exception):
            pass

    # 2. Windows Microphone Registry check
    if meeting_sentinel is not None:
        try:
            active_mic = meeting_sentinel.scan_windows_microphone_registry(now=now_ts)
            if active_mic is not None:
                return (CompanionMood.ON_AIR, "ON AIR")
        except (OSError, ImportError, Exception):
            pass

    # Resolve state directory for Focus Sprint and Desk Reminders
    if state_dir is not None:
        resolved_dir = Path(state_dir)
    elif focus_control is not None:
        try:
            resolved_dir = focus_control.default_data_dir()
        except Exception:
            local = os.environ.get("LOCALAPPDATA")
            resolved_dir = Path(local) / "AWTRIX" if local else Path.home() / "AppData" / "Local" / "AWTRIX"
    else:
        local = os.environ.get("LOCALAPPDATA")
        resolved_dir = Path(local) / "AWTRIX" if local else Path.home() / "AppData" / "Local" / "AWTRIX"

    # 3. Focus Sprint state check
    if focus_sprint is not None:
        try:
            focus_file = resolved_dir / "focus_state.json"
            if focus_file.is_file():
                sprint_state = focus_sprint.load_state(focus_file)
                phase = getattr(sprint_state, "phase", None)
                phase_val = phase.value if hasattr(phase, "value") else str(phase)
                if phase_val in ("focus_running", "focus_paused", "focus_done_cue"):
                    return (CompanionMood.FOCUS, "FOCUS")
                elif phase_val in ("short_break_running", "long_break_running", "break_paused"):
                    return (CompanionMood.REST, "REST")
        except (OSError, ImportError, Exception):
            pass

    # 4. Ergonomic Desk Reminders check
    if desk_reminder is not None:
        try:
            reminder_file = resolved_dir / "desk_reminder.json"
            if reminder_file.is_file():
                reminder = None
                try:
                    reminder = desk_reminder.read_reminder(reminder_file)
                except Exception:
                    pass
                if reminder is not None and reminder.phase(now_ts) == "due":
                    label = reminder.label.strip().upper() if reminder.label else "REST"
                    return (CompanionMood.REST, label)
                elif reminder is None:
                    # Direct json fallback if read_reminder rejected wide label like 'STRETCH'
                    with reminder_file.open("r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        created = float(data.get("created_at", 0))
                        due = float(data.get("due_at", 0))
                        if created <= now_ts < due + 600 and now_ts >= due:
                            raw_label = str(data.get("label", "REST")).strip().upper()
                            return (CompanionMood.REST, raw_label or "REST")
        except (OSError, ImportError, Exception):
            pass

    # 5. Default Idle
    return (CompanionMood.IDLE, "READY")


# ---------------------------------------------------------------- Controller

@dataclass(frozen=True)
class CompanionPolicy:
    enabled: bool = True
    refresh_seconds: float = KEEPALIVE_SECONDS


@dataclass(frozen=True)
class CompanionTick:
    mood: CompanionMood
    pushed: bool = False
    custom_text: str | None = None


class DeskCompanionController:
    """Manages the desk companion's reactivity and pushes to AWTRIX."""

    def __init__(
        self,
        panel: Any,
        policy: CompanionPolicy | None = None,
        *,
        mood_detector: Callable[[], CompanionMood | tuple[CompanionMood, str | None]] | None = None,
        clock: Any | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.panel = panel
        self.policy = policy or CompanionPolicy()
        self.mood_detector = mood_detector or detect_system_companion_mood
        self.clock = clock or time
        self.logger = logger or logging.getLogger("awtrix.utility_hub.companion")
        self.last_push_at: float | None = None
        self.last_push_generation: int | None = None
        self.current_mood: CompanionMood | None = None
        self.current_text: str | None = None

    def tick(self) -> CompanionTick:
        if not self.policy.enabled:
            return CompanionTick(mood=CompanionMood.IDLE, pushed=False, custom_text=None)

        now = getattr(self.clock, "monotonic", time.monotonic)()
        raw_result = self.mood_detector()
        if isinstance(raw_result, tuple):
            mood = raw_result[0]
            custom_text = raw_result[1] if len(raw_result) > 1 else None
        else:
            mood = raw_result
            custom_text = None

        if isinstance(mood, str) and not isinstance(mood, CompanionMood):
            try:
                mood = CompanionMood(mood.lower())
            except ValueError:
                mood = CompanionMood.IDLE

        # Pushed pages disappear on reboot. The shared client's generation
        # changes only when it observes one; simple panel fakes need no hook.
        generation = getattr(self.panel, "reboot_generation", None)
        if type(generation) is not int or generation < 0:
            generation = None
        needs_push = (
            self.current_mood != mood
            or self.current_text != custom_text
            or self.last_push_at is None
            or (generation is not None and generation != self.last_push_generation)
            or (now - self.last_push_at) >= self.policy.refresh_seconds
        )
        pushed = False
        if needs_push:
            payload = build_companion_payload(mood, custom_text=custom_text, animated=True)
            static_payload = build_companion_payload(mood, custom_text=custom_text, animated=False)
            try:
                self.panel.push_app(APP_NAME, payload, static_payload=static_payload)
                self.last_push_at = now
                self.last_push_generation = generation
                self.current_mood = mood
                self.current_text = custom_text
                pushed = True
            except Exception as err:
                self.logger.warning("Failed to push companion: %s", err)

        return CompanionTick(mood=mood, pushed=pushed, custom_text=custom_text)
