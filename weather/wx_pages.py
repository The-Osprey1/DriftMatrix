"""The weather worker's two extra pushed pages.

    wxnext   the outlook.  By day: today's sunset time, the rail showing how
             much daylight is left.  After sunset: tomorrow's high/low, the
             rail showing tomorrow's rain chance.  Gusts of 25 mph or more
             (Santa Ana season) take the page over with the gust speed.
    wxalert  an active NWS warning or advisory for the panel's point, shown
             only while one is in force; the rail shows how much of it is left.

Same layout as every other page: an animated 8x8 icon, a black gutter at
x=8, one value on rows 1-5 and a level rail on row 7.  Pages are static
draws plus an icon GIF, so they cost the panel's contiguous block nothing.
"""

from __future__ import annotations

import base64
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from . import nws, weather_canvas as canvas, weather_icons as icons
except ImportError:  # Direct execution from the weather directory.
    import nws  # type: ignore[no-redef]
    import weather_canvas as canvas  # type: ignore[no-redef]
    import weather_icons as icons  # type: ignore[no-redef]

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.append(_ROOT)
from visual_system import (  # noqa: E402
    LANE_WIDTH,
    LANE_X0,
    LEVEL_COLORS,
    MAX_PAYLOAD_BYTES,
    PEARL,
    Frame,
    draw_level_rail,
    draw_text,
    measure_text,
    new_frame,
)

OUTLOOK_APP = "wxnext"
ALERT_APP = "wxalert"
PAGE_DURATION_MS = 7_000
OUTLOOK_LIFETIME_MS = 900_000
ALERT_MAX_LIFETIME_MS = 900_000
ALERT_MIN_LIFETIME_MS = 60_000

WIND_GUST_MPH = 25
GUST_SCALE_MPH = 60

# Severity -> the level whose colour the label and rail take.
ALERT_LEVEL = {"Moderate": "fair", "Severe": "poor", "Extreme": "bad", "Minor": "info"}


@dataclass(frozen=True)
class PageScene:
    app: str
    kind: str
    icon_kind: str
    is_day: bool
    text: str
    level: str
    meter: float | None
    lifetime_ms: int


def clock_text(moment: datetime) -> str:
    """12-hour clock without a leading zero or suffix: 5:52."""

    hour = moment.hour % 12 or 12
    return f"{hour}:{moment.minute:02d}"


def sunset_scene(now: datetime, sunrise: datetime, sunset: datetime) -> PageScene:
    span = (sunset - sunrise).total_seconds()
    left = (sunset - now).total_seconds()
    meter = max(0.0, min(1.0, left / span)) if span > 0 else None
    return PageScene(OUTLOOK_APP, "sunset", "sunset", True, clock_text(sunset), "fair",
                     meter, OUTLOOK_LIFETIME_MS)


def forecast_scene(icon_kind: str, high_f: float, low_f: float, rain_probability: int | None,
                   kind: str = "tomorrow") -> PageScene:
    text = f"{round(high_f)}/{round(low_f)}\N{DEGREE SIGN}"
    if measure_text(text) > LANE_WIDTH:
        text = f"{round(high_f)}\N{DEGREE SIGN}"
    meter = None if rain_probability is None else rain_probability / 100.0
    return PageScene(OUTLOOK_APP, kind, icon_kind, True, text, "info", meter, OUTLOOK_LIFETIME_MS)


def wind_scene(gust_mph: float, is_day: bool) -> PageScene:
    meter = max(0.0, min(1.0, gust_mph / GUST_SCALE_MPH))
    return PageScene(OUTLOOK_APP, "wind", "wind", is_day, f"{round(gust_mph)}MPH", "info",
                     meter, OUTLOOK_LIFETIME_MS)


def alert_scene(alert: nws.Alert, now: datetime) -> PageScene:
    remaining = (alert.expires - now).total_seconds()
    meter = None
    if alert.effective is not None:
        span = (alert.expires - alert.effective).total_seconds()
        if span > 0:
            meter = max(0.0, min(1.0, remaining / span))
    lifetime = int(max(ALERT_MIN_LIFETIME_MS, min(ALERT_MAX_LIFETIME_MS, remaining * 1000)))
    return PageScene(ALERT_APP, "alert", alert.kind, True, alert.label,
                     ALERT_LEVEL.get(alert.severity, "fair"), meter, lifetime)


def render_lane(scene: PageScene) -> Frame:
    frame = new_frame()
    colour = PEARL if scene.app == OUTLOOK_APP else LEVEL_COLORS[scene.level]
    x = LANE_X0 + max(0, (LANE_WIDTH - measure_text(scene.text)) // 2)
    draw_text(frame, scene.text, x, 1, colour)
    draw_level_rail(frame, scene.meter, scene.level)
    return frame


def icon_gif(scene: PageScene) -> bytes:
    art, delay = icons.frames(scene.icon_kind, scene.is_day)
    return canvas.to_gif([[list(row) for row in frame] for frame in art], delay,
                         width=canvas.ICON_W)


def build_payload(scene: PageScene) -> dict[str, Any]:
    frame = render_lane(scene)
    lane = bytes(
        channel
        for row in frame
        for colour in row[LANE_X0:LANE_X0 + LANE_WIDTH]
        for channel in colour
    )
    payload: dict[str, Any] = {
        "icon": base64.b64encode(icon_gif(scene)).decode("ascii"),
        "iconMode": "fixed",
        "iconOffsetX": 0,
        "draw": [["bitmap", LANE_X0, 0, LANE_WIDTH, 8, base64.b64encode(lane).decode("ascii")]],
        "backgroundColor": "#000000",
        "durationMs": PAGE_DURATION_MS,
        "lifetimeMs": scene.lifetime_ms,
        "lifetimeExpiry": "remove",
    }
    size = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    if size >= MAX_PAYLOAD_BYTES:
        raise ValueError(f"{scene.app} payload is {size} bytes; budget is {MAX_PAYLOAD_BYTES}")
    return payload


def summary(scene: PageScene) -> str:
    return f"{scene.app} {scene.kind} {scene.icon_kind} {scene.text}"
