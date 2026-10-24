"""On-Air / Meeting Busy Sentinel for AWTRIX.

Detects active Windows microphone sessions (e.g. Teams, Zoom, Discord, Google
Meet, Audacity) via Windows CapabilityAccessManager registry telemetry and active
process inspection.  When active, pushes a prominent ON AIR beacon with elapsed
call time to the LED matrix.  When the mic closes, immediately cleans up the page.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from io import BytesIO
import json
import logging
import math
import os
import sys
import time
from typing import Any, Callable, Mapping, Sequence

from PIL import Image

from visual_system import (
    render_glance_lane,
    AMBER,
    BLACK,
    CORAL,
    FONT_3X5,
    GOLD,
    PEARL,
    RED_HOT,
    cycle_length,
    draw_lane_rail,
)

try:
    import psutil
except ImportError:
    psutil = None

try:
    import winreg
except ImportError:
    winreg = None


RGB = tuple[int, int, int]
Frame = list[list[RGB]]

WIDTH = 32
HEIGHT = 8
ICON_WIDTH = 8
ICON_HEIGHT = 8
GUTTER_X = 8
LANE_X0 = 9
LANE_WIDTH = 23
LANE_HEIGHT = 8

APP_NAME = "onair0"
FRAME_BEAT_MS = 120
PAGE_DURATION_MS = 7_000
LIFETIME_MS = 30_000
# A microphone session whose start time is further back than this cannot be
# trusted; see scan_windows_microphone_registry.  It is a plausibility
# bound on a real call, not a display limit.
TRUSTED_ELAPSED_SECONDS = 24 * 3600
# The lane's meter spans one hour of call time.
CALL_METER_SECONDS = 3600
ICON_MAX_BYTES = 1_024
PAYLOAD_MAX_BYTES = 2_048

# Colours come from visual_system.  They used to be redeclared here, and two
# of them had drifted: RED_HOT was (255,20,35) against the shared (255,25,35)
# and DIM_GRAY was (40,45,55) against (45,52,64).  Neither difference is
# visible on its own, which is exactly the problem -- ON AIR red rendered one
# shade off the companion's ON AIR red on the adjacent page of the rotation,
# and nothing in a diff would ever show it.  GOLD, AMBER and DIM_GRAY turned
# out to be dead here; only their definitions referenced them.

# The 3x5 font is shared, not copied.  This module's former local copy was
# byte-identical to visual_system's for every glyph it defined, and defined
# strictly fewer, so importing it changes no rendering and gains coverage.

# Windows epoch FILETIME offset: 100-ns intervals between Jan 1 1601 and Jan 1 1970
FILETIME_EPOCH_DIFF = 116444736000000000


@dataclass(frozen=True)
class ActiveMicSession:
    """An active microphone recording session detected on the machine."""
    app_name: str
    exe_path: str
    started_at: float  # Unix epoch timestamp
    elapsed_seconds: float = 0.0
    # False when the registry's start time failed the sanity check, so the
    # session is real but undatable.  The lane renders "--:--" rather than a
    # number, because every numeric alternative is a lie: see the scan below.
    elapsed_known: bool = True


def filetime_to_unix(filetime: int) -> float:
    """Convert a Windows 64-bit FILETIME timestamp to Unix epoch seconds."""
    if filetime <= FILETIME_EPOCH_DIFF:
        return 0.0
    return (filetime - FILETIME_EPOCH_DIFF) / 10_000_000.0


def format_duration(seconds: float) -> str:
    """Format elapsed seconds as MM:SS under an hour, or NhMM at or above one.

    The units used to switch silently at the hour boundary with no marker, and
    the timer is the entire content of the lane -- so a 125-second call and a
    7,500-second call both rendered "02:05", and a 61-minute call rendered
    "01:01", which reads as one minute and one second.  An explicit H makes the
    two forms impossible to confuse.

    Hours are capped at 99 so the string can never exceed the 21px lane budget,
    and at the cap the minutes are replaced by "--".  Capping the hours alone
    turned an out-of-range value into a precise-looking false reading: 150
    hours rendered "99H00", indistinguishable from a genuine 99-hour call, and
    a 31-year value rendered "99H46".  A saturated form has to look saturated.
    "99H--" is 19px against the 21px budget, so nothing is paid for it.
    """
    total_seconds = max(0, int(seconds))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    if hours > 99:
        return "99H--"
    if hours > 0:
        return f"{hours}H{minutes:02d}"
    return f"{minutes:02d}:{secs:02d}"


def get_running_process_exes() -> set[str]:
    """Return lowercase normalized executable paths for currently running processes."""
    if psutil is None:
        return set()
    running: set[str] = set()
    for proc in psutil.process_iter(["exe"]):
        try:
            exe = proc.info.get("exe")
            if exe:
                running.add(os.path.normpath(exe).lower())
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return running


def scan_windows_microphone_registry(
    running_exes: set[str] | None = None,
    now: float | None = None,
) -> ActiveMicSession | None:
    """Check Windows CapabilityAccessManager for active microphone captures."""
    if winreg is None:
        return None

    if running_exes is None:
        running_exes = get_running_process_exes()
    now_ts = time.time() if now is None else now

    base_paths = [
        r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone\NonPackaged",
        r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone",
    ]

    for base in base_paths:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as root_key:
                num_subkeys = winreg.QueryInfoKey(root_key)[0]
                for i in range(num_subkeys):
                    subkey_name = winreg.EnumKey(root_key, i)
                    if subkey_name == "NonPackaged":
                        continue
                    try:
                        with winreg.OpenKey(root_key, subkey_name) as sub:
                            try:
                                stop_time, _ = winreg.QueryValueEx(sub, "LastUsedTimeStop")
                                if stop_time == 0:
                                    # Translate registry path format
                                    exe_path = os.path.normpath(subkey_name.replace("#", "\\"))
                                    if exe_path.lower() in running_exes:
                                        start_filetime, _ = winreg.QueryValueEx(sub, "LastUsedTimeStart")
                                        start_unix = filetime_to_unix(start_filetime)
                                        # "Stop == 0 and the exe is alive" is
                                        # the shape of a LIVE capture and also
                                        # the shape of a STALE one: a crashed
                                        # or force-killed capture leaves Stop
                                        # at 0 indefinitely, and Teams, Zoom
                                        # and Discord sit resident for weeks.
                                        # So an implausible elapsed is the
                                        # expected failure, not a rarity.
                                        #
                                        # Both numeric answers to it are lies.
                                        # Clamping the start to now rendered
                                        # "00:00" -- a call that just began.
                                        # Widening the bound to 30 days so a
                                        # genuinely long call kept counting
                                        # rendered "72H00" for a three-day-old
                                        # stale key: an authoritative fiction,
                                        # under a beacon that asserts you are
                                        # on air.  So the session is reported
                                        # with its duration marked unknown and
                                        # the lane says so.
                                        elapsed = now_ts - start_unix
                                        known = -60 <= elapsed <= TRUSTED_ELAPSED_SECONDS
                                        app_name = os.path.splitext(os.path.basename(exe_path))[0]
                                        return ActiveMicSession(
                                            app_name=app_name,
                                            exe_path=exe_path,
                                            started_at=start_unix if known else now_ts,
                                            elapsed_seconds=max(0.0, elapsed) if known else 0.0,
                                            elapsed_known=known,
                                        )
                            except OSError:
                                continue
                    except OSError:
                        continue
        except OSError:
            continue

    return None


def new_frame() -> Frame:
    return [[BLACK for _ in range(WIDTH)] for _ in range(HEIGHT)]


def put_pixel(frame: Frame, x: int, y: int, color: RGB) -> None:
    if 0 <= x < WIDTH and 0 <= y < HEIGHT:
        frame[y][x] = color


def draw_text(frame: Frame, text: str, x0: int, y0: int, color: RGB) -> int:
    """Draw text with 3x5 font and return the total width rendered."""
    x = x0
    for idx, char in enumerate(text):
        glyph = FONT_3X5.get(char.upper(), FONT_3X5[" "])
        for gy, row in enumerate(glyph):
            for gx, bit in enumerate(row):
                if bit == "1":
                    put_pixel(frame, x + gx, y0 + gy, color)
        x += len(glyph[0])
        if idx < len(text) - 1:
            x += 1
    return x - x0


def text_width(text: str) -> int:
    """Calculate width in pixels of text in 3x5 proportional font."""
    glyphs = [FONT_3X5.get(c.upper(), FONT_3X5[" "]) for c in text]
    return sum(len(g[0]) for g in glyphs) + max(0, len(glyphs) - 1)


# ---------------------------------------------------------------- Visual Art

def draw_onair_mic_icon(frame: Frame, phase: int) -> None:
    """Draw an 8x8 studio mic with radiating broadcast pulse waves.

    The capsule is a constant PEARL.  It used to invert PEARL <-> RED_HOT on
    every 120ms beat -- the six brightest pixels in the icon, full-intensity
    hue inversion with no shape change at all, which reads as an electrical
    fault rather than as a live microphone.  It also destroyed the icon's
    silhouette on half the frames: at brightness 1 a RED_HOT capsule is the
    same (255,0,0) as the CORAL body around it, so the mic collapsed into a
    featureless red blob every other frame.  A white capsule against a red
    body keeps the shape readable in every frame.

    The animation it was competing with is the part that works: the acoustic
    waves radiate outward by DISPLACEMENT -- silent, inner, inner+outer,
    outer -- and displacement is what survives the 1-bit collapse.
    """
    # Mic capsule at (3..4, 1..3)
    mic_color = PEARL
    put_pixel(frame, 3, 1, mic_color)
    put_pixel(frame, 4, 1, mic_color)
    put_pixel(frame, 3, 2, mic_color)
    put_pixel(frame, 4, 2, mic_color)
    put_pixel(frame, 3, 3, mic_color)
    put_pixel(frame, 4, 3, mic_color)

    # Mic stand at (3..4, 4), stem (3..4, 5), base (2..5, 6)
    put_pixel(frame, 2, 3, CORAL)
    put_pixel(frame, 5, 3, CORAL)
    put_pixel(frame, 2, 4, CORAL)
    put_pixel(frame, 5, 4, CORAL)
    put_pixel(frame, 3, 4, CORAL)
    put_pixel(frame, 4, 4, CORAL)
    put_pixel(frame, 3, 5, CORAL)
    put_pixel(frame, 4, 5, CORAL)
    put_pixel(frame, 2, 6, CORAL)
    put_pixel(frame, 3, 6, CORAL)
    put_pixel(frame, 4, 6, CORAL)
    put_pixel(frame, 5, 6, CORAL)

    # Radiating acoustic arcs based on animation phase (0..3)
    pulse = phase % 4
    if pulse in (1, 2):
        # Inner wave
        put_pixel(frame, 1, 2, RED_HOT)
        put_pixel(frame, 6, 2, RED_HOT)
    if pulse in (2, 3):
        # Outer wave
        put_pixel(frame, 0, 1, CORAL)
        put_pixel(frame, 7, 1, CORAL)
        put_pixel(frame, 0, 2, CORAL)
        put_pixel(frame, 7, 2, CORAL)


def build_onair_icon_gif(animated: bool = True) -> bytes:
    """Generate an 8x8 GIF on the required 120ms beat."""
    frame_count = 4 if animated else 1
    frames = []
    for phase in range(frame_count):
        f = new_frame()
        draw_onair_mic_icon(f, phase)
        frames.append(f)

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
    validate_icon_gif(data)
    return data


def validate_icon_gif(data: bytes) -> None:
    """Ensure icon strictly adheres to AWTRIX stability limits."""
    if len(data) > ICON_MAX_BYTES:
        raise ValueError(f"On-Air icon is {len(data)} bytes; limit is {ICON_MAX_BYTES}")
    if not data.startswith((b"GIF87a", b"GIF89a")):
        raise ValueError("On-Air icon is not a valid GIF")
    with Image.open(BytesIO(data)) as img:
        if img.size != (ICON_WIDTH, ICON_HEIGHT):
            raise ValueError(f"On-Air icon size is {img.size}; must be (8, 8)")
        if "transparency" in img.info:
            raise ValueError("On-Air icon must be fully opaque")
        for idx in range(img.n_frames):
            img.seek(idx)
            dur = int(img.info.get("duration", 0))
            if dur and dur % FRAME_BEAT_MS != 0:
                raise ValueError(f"Frame {idx} duration {dur}ms is not on {FRAME_BEAT_MS}ms beat")


def render_lane_frame(session: ActiveMicSession) -> Frame:
    """Calm glance: elapsed call time in white; the red mic icon says "on air".

    The meter is the hour, filling one LED every ~2.6 minutes, so the call's
    length reads at a glance without parsing digits; past the hour it stays
    full and turns gold.

    An undatable session still gets a lane -- the mic really is open -- but no
    number and no meter: "--:--" keeps the shape of a reading and cannot be
    misread as one, and a filled meter would be exactly that misreading.
    """
    if not session.elapsed_known:
        return render_glance_lane("--:--")
    elapsed = session.elapsed_seconds
    return render_glance_lane(
        format_duration(elapsed),
        meter=elapsed / CALL_METER_SECONDS,
        level="info" if elapsed < CALL_METER_SECONDS else "fair",
    )


def build_lane_bitmap_b64(session: ActiveMicSession) -> str:
    """Encode the 23x8 RGB888 data lane to base64."""
    frame = render_lane_frame(session)
    raw = bytes(
        channel
        for y in range(HEIGHT)
        for x in range(LANE_X0, LANE_X0 + LANE_WIDTH)
        for channel in frame[y][x]
    )
    return base64.b64encode(raw).decode("ascii")


def build_meeting_payload(session: ActiveMicSession, animated: bool = True) -> dict:
    """Create a fully compliant AWTRIX pushed app payload (< 2KB)."""
    icon_gif = build_onair_icon_gif(animated=animated)
    lane_b64 = build_lane_bitmap_b64(session)

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
    if len(raw_json) >= PAYLOAD_MAX_BYTES:
        raise ValueError(f"On-Air payload {len(raw_json)} bytes exceeded 2KB safety budget")
    return payload


# ---------------------------------------------------------------- Hub Controller

@dataclass(frozen=True)
class MeetingSentinelPolicy:
    refresh_seconds: float = 3.0
    lifetime_seconds: float = 30.0
    enabled: bool = True


@dataclass(frozen=True)
class MeetingTick:
    active: bool
    session: ActiveMicSession | None
    pushed: bool = False
    deleted: bool = False


class MeetingSentinelController:
    """Lifecycle controller managing On-Air microphone alerts on the panel."""

    def __init__(
        self,
        panel: Any,  # PanelClient
        policy: MeetingSentinelPolicy | None = None,
        *,
        detector: Callable[[], ActiveMicSession | None] | None = None,
        clock: Any | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.panel = panel
        self.policy = policy or MeetingSentinelPolicy()
        self.detector = detector or scan_windows_microphone_registry
        self.clock = clock or time
        self.logger = logger or logging.getLogger("awtrix.utility_hub.meeting")
        self.last_push_at: float | None = None
        self.active_session: ActiveMicSession | None = None
        self.is_showing = False

    def tick(self) -> MeetingTick:
        if not self.policy.enabled:
            return MeetingTick(active=False, session=None)

        now = getattr(self.clock, "monotonic", time.monotonic)()
        session = self.detector()

        if session is not None:
            # Microphone is actively capturing
            self.active_session = session
            needs_push = (
                not self.is_showing
                or self.last_push_at is None
                or (now - self.last_push_at) >= self.policy.refresh_seconds
            )
            if needs_push:
                payload = build_meeting_payload(session, animated=True)
                try:
                    # Push with high alert priority
                    self.panel.push_app(APP_NAME, payload)
                    if not self.is_showing:
                        # Bring panel to foreground on call start
                        try:
                            self.panel.activate_app(APP_NAME)
                        except Exception:
                            pass
                    self.is_showing = True
                    self.last_push_at = now
                    return MeetingTick(active=True, session=session, pushed=True)
                except Exception as err:
                    self.logger.warning("Failed to push On-Air alert: %s", err)
            return MeetingTick(active=True, session=session, pushed=False)

        # Microphone is inactive
        if self.is_showing:
            # Call ended: remove the alert immediately
            try:
                self.panel.delete_pushed_app(APP_NAME)
            except Exception as err:
                self.logger.warning("Failed to clear On-Air alert: %s", err)
            self.is_showing = False
            self.last_push_at = None
            self.active_session = None
            return MeetingTick(active=False, session=None, deleted=True)

        return MeetingTick(active=False, session=None)
