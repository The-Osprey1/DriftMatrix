"""Commute & Drive Time Radar for AWTRIX.

Monitors estimated drive time to work or home during configured commute windows.
Features an animated 8x8 transit/car icon with rolling wheels and a color-coded
23x8 lane showing destination and ETA (Emerald for clear, Amber for moderate delays,
Coral for severe congestion).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from io import BytesIO
import json
import logging
import math
import time
from typing import Any, Callable, Sequence

from PIL import Image

from visual_system import (
    render_glance_lane,
    AMBER,
    BLACK,
    CORAL,
    DIM_GRAY,
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
    WIDTH,
    draw_lane_rail,
    draw_text,
    new_frame,
    put_pixel,
    cycle_length,
    measure_text,
)

APP_NAME = "commute0"
PAGE_DURATION_MS = 7_000
LIFETIME_MS = 60_000


class TrafficCondition(str, Enum):
    CLEAR = "clear"
    MODERATE = "moderate"
    HEAVY = "heavy"


@dataclass(frozen=True)
class CommuteEstimate:
    destination: str
    duration_minutes: int
    typical_minutes: int
    traffic: TrafficCondition = TrafficCondition.CLEAR
    route_name: str = ""


@dataclass(frozen=True)
class CommuteWindow:
    start_hour: int
    start_minute: int
    end_hour: int
    end_minute: int
    destination: str
    days: tuple[int, ...] = (0, 1, 2, 3, 4)  # Mon-Fri by default


def is_in_commute_window(window: CommuteWindow, moment: datetime | None = None) -> bool:
    """Check if the current moment falls within the scheduled commute window."""
    now = datetime.now().astimezone() if moment is None else moment
    if now.weekday() not in window.days:
        return False
    current_min = now.hour * 60 + now.minute
    win_start = window.start_hour * 60 + window.start_minute
    win_end = window.end_hour * 60 + window.end_minute
    return win_start <= current_min < win_end


# ---------------------------------------------------------------- Visual Art

def draw_car_icon(frame: list[list[tuple[int, int, int]]], phase: int, traffic: TrafficCondition) -> None:
    """Draw an 8x8 car with rolling animated wheels."""
    # Body color based on traffic
    if traffic == TrafficCondition.HEAVY:
        car_color = CORAL
    elif traffic == TrafficCondition.MODERATE:
        car_color = AMBER
    else:
        car_color = EMERALD

    # The whole car sits one row lower than it first shipped, so its road line
    # lands on row 7 -- the row the lane's accent rail uses.  Before, the road
    # was on row 6 and the rail on row 7, so the ground visibly stepped down as
    # it crossed the black gutter at column 8.

    # Roof & cabin: rows 3..4
    put_pixel(frame, 3, 3, car_color)
    put_pixel(frame, 4, 3, car_color)
    put_pixel(frame, 2, 4, car_color)
    put_pixel(frame, 3, 4, ICE)  # Windshield
    put_pixel(frame, 4, 4, ICE)  # Side window
    put_pixel(frame, 5, 4, car_color)

    # Chassis / body: row 5
    for x in range(1, 7):
        put_pixel(frame, x, 5, car_color)

    # Headlights & Taillights on row 5
    put_pixel(frame, 6, 5, GOLD)     # Headlight
    put_pixel(frame, 1, 5, RED_HOT)  # Taillight

    # Severity must survive brightness 1, where AMBER and CORAL both threshold
    # to pure red and the colour-coded body stops telling MODERATE from HEAVY.
    # A congestion bar carries it in length instead, which survives the 1-bit
    # collapse that hue does not.
    #
    # Three things about the bar are deliberate, and the first version got all
    # three wrong.  It is ANCHORED at x0: a centred bar of varying length has
    # no fixed reference, so length cannot be judged from a single glance at a
    # single state -- and the states appear minutes apart, never together.  It
    # is never EMPTY: drawing nothing for CLEAR is indistinguishable from the
    # feature not existing, and leaves hue as the only CLEAR signal, which is
    # what this exists to replace.  And it sits on ROW 0, the panel's rail row:
    # at row 1 the two-pixel MODERATE bar was pixel-identical to the car's own
    # roof two rows below it, same columns and same colour, so it read as the
    # car wearing a hat rather than as a meter.
    congestion = {
        TrafficCondition.CLEAR: 1,
        TrafficCondition.MODERATE: 3,
        TrafficCondition.HEAVY: 5,
    }[traffic]
    for i in range(congestion):
        put_pixel(frame, i, 0, car_color)

    # Wheels on row 6 at x=2 and x=5, both lit on every frame.
    #
    # They first alternated PEARL against DIM_GRAY (45,52,64), whose dominant
    # channel is 64 -- under the 174 threshold -- so one wheel was unlit and
    # what shipped was a single dot teleporting between x=2 and x=5.  The fix
    # for that was to alternate PEARL against ICE, which is self-defeating:
    # both clear the threshold, so at brightness 1 they are the SAME colour and
    # the two frames become pixel-identical.  Measured: zero changing pixels in
    # all three traffic states.  A one-pixel wheel cannot roll, and recolouring
    # a stationary pixel is flicker, not motion.
    put_pixel(frame, 2, 6, PEARL)
    put_pixel(frame, 5, 6, PEARL)

    # Road line on row 7, aligned with the lane rail across the gutter, with
    # dashes that MOVE.  Displacement is the only thing that reads as motion,
    # and it is the one cue that survives the 1-bit collapse: TRACK falls to
    # black there, so at brightness 1 the whole animation is two white dashes
    # travelling under a static car -- which is what "rolling" has to mean on
    # an eight-pixel icon.
    for rx in range(8):
        put_pixel(frame, rx, 7, TRACK)
    # GOLD, not a white, for two reasons: it reads as road markings rather
    # than as flashes, and at brightness 1 it is (255,255,0) against the
    # PEARL wheels' (255,255,255).  A white dash directly under the white
    # wheel at x=5 merged the two into one blob on alternating frames.
    for dx in range(phase % 2, 8, 4):
        put_pixel(frame, dx, 7, GOLD)


def build_commute_icon_gif(traffic: TrafficCondition, animated: bool = True) -> bytes:
    """Create 8x8 GIF on 120ms beat."""
    frame_count = 2 if animated else 1
    frames = []
    for phase in range(frame_count):
        f = new_frame()
        draw_car_icon(f, phase, traffic)
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
    if len(data) > MAX_ICON_BYTES:
        raise ValueError("Commute icon exceeds byte budget")
    return data


def render_commute_lane(estimate: CommuteEstimate) -> list[list[tuple[int, int, int]]]:
    """Calm glance: destination initial as a grey label, the ETA in white.

    The meter is the drive against twice its usual time, so a normal day sits
    near half and a jam fills it; its colour is the traffic level.
    """
    level = {
        TrafficCondition.CLEAR: "good",
        TrafficCondition.MODERATE: "fair",
        TrafficCondition.HEAVY: "bad",
    }[estimate.traffic]
    typical = max(1, estimate.typical_minutes)
    return render_glance_lane(
        f"{estimate.duration_minutes}M",
        label=estimate.destination[:1].upper() or None,
        meter=estimate.duration_minutes / (2 * typical),
        level=level,
    )


def build_commute_payload(estimate: CommuteEstimate, animated: bool = True) -> dict:
    icon_gif = build_commute_icon_gif(estimate.traffic, animated=animated)
    frame = render_commute_lane(estimate)
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
        raise ValueError("Commute payload exceeds 2KB safety budget")
    return payload


# ---------------------------------------------------------------- Controller

@dataclass(frozen=True)
class CommuteConfig:
    enabled: bool = True
    windows: tuple[CommuteWindow, ...] = (
        CommuteWindow(7, 30, 9, 0, "WORK"),
        CommuteWindow(17, 0, 18, 30, "HOME"),
    )
    refresh_seconds: float = 60.0


@dataclass(frozen=True)
class CommuteTick:
    active: bool
    estimate: CommuteEstimate | None = None
    pushed: bool = False
    deleted: bool = False


class CommuteRadarController:
    """Manages commute radar display based on schedule windows."""

    def __init__(
        self,
        panel: Any,
        config: CommuteConfig | None = None,
        *,
        estimator: Callable[[str], CommuteEstimate] | None = None,
        clock: Any | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.panel = panel
        self.config = config or CommuteConfig()
        self.estimator = estimator or (
            lambda dest: CommuteEstimate(dest, duration_minutes=25, typical_minutes=24)
        )
        self.clock = clock or time
        self.logger = logger or logging.getLogger("awtrix.utility_hub.commute")
        self.last_push_at: float | None = None
        self.is_showing = False

    def tick(self) -> CommuteTick:
        if not self.config.enabled:
            return CommuteTick(active=False)

        now = getattr(self.clock, "monotonic", time.monotonic)()

        # Find active commute window
        active_window: CommuteWindow | None = None
        for win in self.config.windows:
            if is_in_commute_window(win):
                active_window = win
                break

        if active_window is not None:
            needs_push = (
                not self.is_showing
                or self.last_push_at is None
                or (now - self.last_push_at) >= self.config.refresh_seconds
            )
            if needs_push:
                estimate = self.estimator(active_window.destination)
                payload = build_commute_payload(estimate, animated=True)
                try:
                    self.panel.push_app(APP_NAME, payload)
                    self.is_showing = True
                    self.last_push_at = now
                    return CommuteTick(active=True, estimate=estimate, pushed=True)
                except Exception as err:
                    self.logger.warning("Failed to push commute radar: %s", err)
            return CommuteTick(active=True, pushed=False)

        # Outside all commute windows
        if self.is_showing:
            try:
                self.panel.delete_pushed_app(APP_NAME)
            except Exception as err:
                self.logger.warning("Failed to delete commute radar: %s", err)
            self.is_showing = False
            self.last_push_at = None
            return CommuteTick(active=False, deleted=True)

        return CommuteTick(active=False)
