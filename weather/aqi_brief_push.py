"""Fetch US AQI and push the Jewelbox AQI Brief to AWTRIX.

The PC fetches Open-Meteo air-quality data (CAMS ENSEMBLE) every 15 minutes.
The panel receives one small pushed app: an opaque 8x8 flowing-wind GIF and
a quiet static lane containing only the explicit current AQI value.
No full-screen GIF or per-frame HTTP traffic is used.

Data attribution: Weather and air-quality data by Open-Meteo.com; air-quality
forecast data from the Copernicus Atmosphere Monitoring Service (CAMS).

Dry run:
    C:/Users/devyn/anaconda3/python.exe weather/aqi_brief_push.py --dry-run --once

Continuous:
    C:/Users/devyn/anaconda3/python.exe weather/aqi_brief_push.py
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import requests
    from PIL import Image
except ImportError:
    sys.exit("Missing dependencies. Run: pip install requests pillow")

# The panel-wide calm-glance lane lives in the repository root's
# visual_system; the scheduled task runs this file directly.
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.append(_ROOT)
from visual_system import LABEL_GAP, PEARL, RGB, draw_comet, render_rich_lane, scale_color  # noqa: E402
import firmware_compat
import panel_gate  # noqa: E402
import threading
from service_runtime import DriftMatrixConfig, run_owned, worker_adapter, wait, in_quiet_hours
from focus_control import default_data_dir

try:
    from .aqi_brief_preview import (
        AQI_COLORS,
        BLACK,
        GUTTER_X,
        HEIGHT,
        LANE_X0,
        LANE_WIDTH,
        WIDTH,
        Frame,
        category_index,
        display_text,
        draw_lungs_jewel,
        grade_frame,
        led_image,
        new_frame,
        panel_frame,
    )
except ImportError:  # Direct execution from the weather directory.
    from aqi_brief_preview import (  # type: ignore[no-redef]
        AQI_COLORS,
        BLACK,
        GUTTER_X,
        HEIGHT,
        LANE_X0,
        LANE_WIDTH,
        WIDTH,
        Frame,
        category_index,
        display_text,
        draw_lungs_jewel,
        grade_frame,
        led_image,
        new_frame,
        panel_frame,
    )


# Keep the proven raw IP until this LAN's duplicate mDNS advertisement is fixed.
AWTRIX = "http://192.168.1.90"
APP_NAME = "aqibrief"
GATE_SOURCE = "aqi"
PUSH_PATH = f"/api/v1/apps/pushed/{APP_NAME}"
DELETE_PATH = f"/api/v1/apps/{APP_NAME}"
DEVICE_PATH = "/api/v1/device"
OPEN_METEO_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
LATITUDE = 33.87529
LONGITUDE = -117.56644

REFRESH_SECONDS = 900
LIFETIME_MS = 2_700_000
PAGE_DURATION_MS = 7_000
MODERATE_DURATION_MS = 5_000
REQUEST_TIMEOUT_SECONDS = 10

ICON_WIDTH = 8
ICON_HEIGHT = 8
# One comet cycle along the wind lines.  An 8x8 icon animation costs no
# contiguous block (measured); only full-width GIFs do.
ICON_MAX_FRAMES = 10
ICON_MAX_BYTES = 4_200
MAX_BODY_BYTES = 8_192
SAFE_BODY_BYTES = 2_048
LANE_HEIGHT = 8

# How long the comet holds each step.  Clean air drifts, worse air hurries.
# Every value is a multiple of 120ms because the render tick is 24ms and GIF
# delays quantise to 10ms - they only coincide at 120, and anything else beats
# 2-tick/3-tick and visibly judders.
ICON_FRAME_MS = (240, 240, 240, 120, 120, 120)

MAX_OBSERVATION_AGE_SECONDS = 90 * 60
MAX_OBSERVATION_LEAD_SECONDS = 20 * 60
SAFE_MODE_FAILURES = 5

# AQI is an attention page, not a permanent dashboard. Good air disappears
# from the rotation unless the next five hours cross into Moderate with a
# meaningful rise. This preserves screen time for information that can change
# a decision.
AQI_SHOW_AT = 51
AQI_CLEAR_BELOW = 45
AQI_UNHEALTHY_AT = 101
AQI_FORECAST_MIN_RISE = 5
QUIET_START_MINUTE = 21 * 60 + 30
QUIET_END_MINUTE = 7 * 60


@dataclass(frozen=True)
class AQISnapshot:
    observed_at: datetime
    timezone_name: str
    us_aqi: int
    pm2_5: float
    forecast_times: tuple[datetime, ...]
    forecast_aqi: tuple[int, ...]

    @property
    def level(self) -> int:
        return category_index(self.us_aqi)

    @property
    def next_five_aqi(self) -> tuple[int, ...]:
        """Return the first five strictly future hourly values."""

        return tuple(
            value
            for stamp, value in zip(self.forecast_times, self.forecast_aqi)
            if stamp > self.observed_at
        )[:5]


@dataclass(frozen=True)
class PanelHealth:
    uptime_seconds: int
    reset_reason: str


def is_relevant(snapshot: AQISnapshot, currently_visible: bool = False) -> bool:
    """Return whether AQI deserves a place in the current app loop."""

    if snapshot.us_aqi >= AQI_SHOW_AT:
        return True
    if currently_visible and snapshot.us_aqi >= AQI_CLEAR_BELOW:
        return True
    future = snapshot.next_five_aqi
    if not future:
        return False
    peak = max(future)
    return peak >= AQI_SHOW_AT and peak - snapshot.us_aqi >= AQI_FORECAST_MIN_RISE


def page_duration_ms(snapshot: AQISnapshot) -> int:
    return PAGE_DURATION_MS if snapshot.us_aqi >= AQI_UNHEALTHY_AT else MODERATE_DURATION_MS


def is_quiet_time(moment: datetime) -> bool:
    minute = moment.hour * 60 + moment.minute
    return minute >= QUIET_START_MINUTE or minute < QUIET_END_MINUTE


def relevance_reason(snapshot: AQISnapshot) -> str:
    if snapshot.us_aqi >= AQI_SHOW_AT:
        return f"current AQI {snapshot.us_aqi}"
    future = snapshot.next_five_aqi
    if future and max(future) >= AQI_SHOW_AT:
        return f"forecast AQI {max(future)}"
    return f"good air AQI {snapshot.us_aqi}"


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _finite(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be a finite number") from error
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _timezone(name: Any) -> timezone | ZoneInfo:
    if isinstance(name, str) and name:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            pass
    return timezone.utc


def _local_datetime(value: Any, zone: timezone | ZoneInfo, name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def _sequence(value: Any, name: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{name} must be an array")
    return value


def parse_air_quality(
    payload: Mapping[str, Any],
    now: datetime | None = None,
) -> AQISnapshot:
    """Validate Open-Meteo data and reject an old but successful response."""

    root = _mapping(payload, "air-quality response")
    zone_name = root.get("timezone") if isinstance(root.get("timezone"), str) else "UTC"
    zone = _timezone(zone_name)
    current = _mapping(root.get("current"), "current")
    hourly = _mapping(root.get("hourly"), "hourly")

    observed_at = _local_datetime(current.get("time"), zone, "current.time")
    local_now = datetime.now(zone) if now is None else now
    if local_now.tzinfo is None:
        local_now = local_now.replace(tzinfo=zone)
    else:
        local_now = local_now.astimezone(zone)
    age = (local_now - observed_at).total_seconds()
    if age > MAX_OBSERVATION_AGE_SECONDS:
        raise ValueError(f"AQI observation is stale by {round(age / 60)} minutes")
    if age < -MAX_OBSERVATION_LEAD_SECONDS:
        raise ValueError("AQI observation timestamp is unexpectedly in the future")

    aqi = int(round(_finite(current.get("us_aqi"), "current.us_aqi")))
    pm2_5 = _finite(current.get("pm2_5"), "current.pm2_5")
    if aqi < 0 or pm2_5 < 0:
        raise ValueError("AQI and PM2.5 must not be negative")

    raw_times = _sequence(hourly.get("time"), "hourly.time")
    raw_values = _sequence(hourly.get("us_aqi"), "hourly.us_aqi")
    if len(raw_times) != len(raw_values) or not raw_times:
        raise ValueError("hourly AQI arrays must be non-empty and equal length")
    forecast_times = tuple(
        _local_datetime(value, zone, f"hourly.time[{index}]")
        for index, value in enumerate(raw_times)
    )
    forecast_aqi = tuple(
        max(0, int(round(_finite(value, f"hourly.us_aqi[{index}]"))))
        for index, value in enumerate(raw_values)
    )
    if any(later <= earlier for earlier, later in zip(forecast_times, forecast_times[1:])):
        raise ValueError("hourly AQI times must be strictly increasing")

    return AQISnapshot(
        observed_at=observed_at,
        timezone_name=zone_name,
        us_aqi=aqi,
        pm2_5=pm2_5,
        forecast_times=forecast_times,
        forecast_aqi=forecast_aqi,
    )


def request_params(latitude: float = LATITUDE, longitude: float = LONGITUDE) -> dict[str, Any]:
    return {
        "latitude": latitude,
        "longitude": longitude,
        "current": "us_aqi,pm2_5",
        "hourly": "us_aqi",
        "forecast_hours": 6,
        "timezone": "auto",
    }


def fetch_air_quality(
    session: requests.Session,
    latitude: float = LATITUDE,
    longitude: float = LONGITUDE,
    now: datetime | None = None,
    *, timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> AQISnapshot:
    response = session.get(
        OPEN_METEO_URL,
        params=request_params(latitude, longitude),
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise ValueError("Open-Meteo returned a non-object response")
    return parse_air_quality(payload, now=now)


# Wind (2026-10-03, to the Now Playing standard): the classic two-gust glyph
# in the category colour, each line fading in from the left like Now
# Playing's title edges, with the record's groove trick -- a PEARL comet with
# a fading trail -- running along each gust.  Nothing moves off the 8px edge,
# so nothing breaks apart (the drifting gusts before it wrapped and split; a
# face after them read as a gimmick; lungs before those read as a hand).
WIND_GUSTS = (
    ((0, 2), (1, 2), (2, 2), (3, 2), (4, 2), (5, 2), (6, 1), (5, 0), (4, 0)),
    ((0, 5), (1, 5), (2, 5), (3, 5), (4, 5), (5, 5), (6, 5), (7, 6), (6, 7), (5, 7)),
)
WIND_OFFSETS = (0, 5)  # the two comets run out of step
WIND_FADE_IN = (0.30, 0.62)  # the first two pixels of each gust
# The curl carries a second hue within each severity's colour family. The
# severity itself remains the label/body colour and still grades the rail.
WIND_ACCENTS: tuple[RGB, ...] = (
    (55, 216, 255), (255, 157, 57), (255, 88, 105),
    (255, 132, 183), (126, 176, 255), (255, 103, 96),
)
AQI_METER_MAX = 300
# The rail grades through the EPA category colours at their breakpoints.
AQI_RAIL_STOPS = tuple(
    (at / AQI_METER_MAX, AQI_COLORS[index]) for index, at in enumerate((0, 50, 100, 150, 200, 300))
)


def _gust_colours(level: int, path: Sequence[tuple[int, int]]) -> list[RGB]:
    color = AQI_COLORS[level]
    return [scale_color(color, WIND_FADE_IN[i]) if i < len(WIND_FADE_IN)
            else color if i < 5 else WIND_ACCENTS[level]
            for i in range(len(path))]


def _icon_frame(level: int, phase: int) -> Image.Image:
    frame = [[BLACK] * ICON_WIDTH for _ in range(ICON_HEIGHT)]
    for path, offset in zip(WIND_GUSTS, WIND_OFFSETS):
        colours = _gust_colours(level, path)
        for (x, y), colour in zip(path, colours):
            frame[y][x] = colour
        draw_comet(frame, path, (phase + offset) % ICON_MAX_FRAMES, colours)
    image = Image.new("RGB", (ICON_WIDTH, ICON_HEIGHT), BLACK)
    for y in range(ICON_HEIGHT):
        for x in range(ICON_WIDTH):
            image.putpixel((x, y), frame[y][x])
    return image


@lru_cache(maxsize=6)
def _animated_icon(level: int) -> bytes:
    frames = [_icon_frame(level, phase) for phase in range(ICON_MAX_FRAMES)]
    # Reuse one exact palette across the whole loop. Letting each frame
    # allocate its own colour table needlessly spends the tiny payload
    # budget and makes richer shading expensive. No colours are quantized.
    colours = list(dict.fromkeys(colour for frame in frames for colour in frame.getdata()))
    indices = {colour: index for index, colour in enumerate(colours)}
    palette = [channel for colour in colours for channel in colour]
    indexed = []
    for frame in frames:
        image = Image.new("P", frame.size)
        image.putpalette(palette)
        image.putdata([indices[colour] for colour in frame.getdata()])
        indexed.append(image)
    stream = BytesIO()
    indexed[0].save(
        stream,
        format="GIF",
        save_all=True,
        append_images=indexed[1:],
        duration=ICON_FRAME_MS[level],
        loop=0,
        disposal=1,
        palette=palette,
        optimize=True,
    )
    data = stream.getvalue()
    validate_icon_gif(data)
    return data


@lru_cache(maxsize=6)
def _static_icon(level: int) -> bytes:
    stream = BytesIO()
    # Static safety icon: the wind glyph at rest, no comet.
    _icon_frame(level, ICON_MAX_FRAMES - 1).save(stream, format="GIF", optimize=True)
    data = stream.getvalue()
    validate_icon_gif(data)
    return data


def validate_icon_gif(data: bytes) -> None:
    if len(data) > ICON_MAX_BYTES:
        raise ValueError(f"AQI icon is {len(data)} bytes; limit is {ICON_MAX_BYTES}")
    if not data.startswith((b"GIF87a", b"GIF89a")):
        raise ValueError("AQI icon is not a GIF")
    expected = (ICON_WIDTH, ICON_HEIGHT)
    try:
        with Image.open(BytesIO(data)) as image:
            if image.size != expected:
                raise ValueError(f"AQI icon must be exactly {expected[0]}x{expected[1]}")
            if not 1 <= image.n_frames <= ICON_MAX_FRAMES:
                raise ValueError("AQI icon frame count exceeds the safety limit")
            if "transparency" in image.info:
                raise ValueError("AQI icon must be fully opaque")
            for frame_index in range(image.n_frames):
                image.seek(frame_index)
                if image.size != expected:
                    raise ValueError("every AQI icon frame must be 8x8")
                duration = image.info.get("duration", 0)
                if duration % 120:
                    raise ValueError(
                        f"frame delay {duration}ms is off the 120ms beat and will judder"
                    )
    except OSError as error:
        raise ValueError("AQI icon is not readable") from error


def build_icon_gif(snapshot: AQISnapshot, animated: bool = True) -> bytes:
    """Return only the 8x8 animated wind glyph; the information lane stays static."""

    return _animated_icon(snapshot.level) if animated else _static_icon(snapshot.level)


def _lane_bytes(snapshot: AQISnapshot) -> bytes:
    """Encode the stable 23x8 information lane as exact RGB888 pixels."""

    aqi = min(500, max(0, int(round(float(snapshot.us_aqi)))))
    # Keep AQI explicit even at three digits. Full-size "AQI" (11px), a
    # black column and three full-size digits (11px) use the exact 23px lane.
    # Shorter readings retain their established spacing and position.
    frame = render_rich_lane(str(aqi), fraction=aqi / AQI_METER_MAX, stops=AQI_RAIL_STOPS,
                             label="AQI", label_color=AQI_COLORS[snapshot.level],
                             label_gap=1 if aqi >= 100 else LABEL_GAP)
    assert all(frame[y][GUTTER_X] == BLACK for y in range(HEIGHT))
    return bytes(
        channel
        for row in frame
        for color in row[LANE_X0 : LANE_X0 + LANE_WIDTH]
        for channel in color
    )


def build_payload(snapshot: AQISnapshot, animated: bool = True) -> dict[str, Any]:
    icon = build_icon_gif(snapshot, animated=animated)
    lane = base64.b64encode(_lane_bytes(snapshot)).decode("ascii")
    payload: dict[str, Any] = {
        "icon": base64.b64encode(icon).decode("ascii"),
        "iconMode": "fixed",
        "iconOffsetX": 0,
        "draw": [["bitmap", LANE_X0, 0, LANE_WIDTH, LANE_HEIGHT, lane]],
        "backgroundColor": "#000000",
        "durationMs": page_duration_ms(snapshot),
        "lifetimeMs": LIFETIME_MS,
        "lifetimeExpiry": "remove",
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(body) >= MAX_BODY_BYTES:
        raise ValueError(f"AQI payload unexpectedly grew to {len(body)} bytes")
    if len(body) >= SAFE_BODY_BYTES:
        raise ValueError(f"AQI payload exceeded the {SAFE_BODY_BYTES}-byte safety budget")
    return payload


def push_payload(session: requests.Session, base_url: str, payload: Mapping[str, Any], *, timeout: float = REQUEST_TIMEOUT_SECONDS) -> None:
    body = firmware_compat.prepare_payload(base_url, payload)
    with panel_gate.mutation(GATE_SOURCE, "push", APP_NAME, body=body) as gate:
        response = session.put(
            f"{base_url.rstrip('/')}{PUSH_PATH}",
            json=body,
            timeout=timeout,
        )
        gate.observe(response)
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"AWTRIX HTTP {response.status_code}: {response.text[:200]}")


def delete_app(session: requests.Session, base_url: str, *, timeout: float = REQUEST_TIMEOUT_SECONDS) -> None:
    """Remove the AQI page when its information is not actionable."""

    with panel_gate.mutation(GATE_SOURCE, "delete", APP_NAME) as gate:
        response = session.delete(
            f"{base_url.rstrip('/')}{DELETE_PATH}",
            timeout=timeout,
        )
        gate.observe(response)
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"AWTRIX HTTP {response.status_code}: {response.text[:200]}")


def activate_app(session: requests.Session, base_url: str, *, timeout: float = REQUEST_TIMEOUT_SECONDS) -> None:
    body = {"name": APP_NAME, "fast": False}
    with panel_gate.mutation(GATE_SOURCE, "activate", APP_NAME, body=body) as gate:
        response = session.put(
            f"{base_url.rstrip('/')}/api/v1/apps/active",
            json=body,
            timeout=timeout,
        )
        gate.observe(response)
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"AWTRIX HTTP {response.status_code}: {response.text[:200]}")


def read_panel_health(session: requests.Session, base_url: str, *, timeout: float = REQUEST_TIMEOUT_SECONDS) -> PanelHealth | None:
    try:
        response = session.get(
            f"{base_url.rstrip('/')}{DEVICE_PATH}",
            timeout=timeout,
        )
    except requests.RequestException:
        return None
    if not 200 <= response.status_code < 300:
        return None
    try:
        body = response.json()
        uptime = int(body["uptimeSeconds"])
        reason = str(body.get("resetReason", ""))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if uptime < 0:
        return None
    firmware_compat.observe_device(base_url, body)
    return PanelHealth(uptime_seconds=uptime, reset_reason=reason)


def panel_rebooted_since(previous: PanelHealth | None, current: PanelHealth | None) -> bool:
    return previous is not None and current is not None and current.uptime_seconds < previous.uptime_seconds


PLANNED_RESET_REASON = "software"


def unplanned_reboot(previous: PanelHealth | None, current: PanelHealth | None) -> bool:
    """A reboot nobody asked for: the only kind that should cost animation.

    The panel reports ``software`` after a requested restart -- the API reboot
    the Utility Hub's nightly maintenance sends, an OTA, a settings change.
    A crash reports ``panic`` or a watchdog.  Latching static art on every
    reboot made the nightly maintenance freeze these icons for good.
    """
    return (panel_rebooted_since(previous, current)
            and current is not None
            and current.reset_reason.strip().lower() != PLANNED_RESET_REASON)


def save_preview(snapshot: AQISnapshot, path: Path, scale: int = 18, gap: int = 1) -> Path:
    # Preview the same wind and lane shipped by build_payload; the historic
    # preview module still contains a retired lungs design.
    icon = _icon_frame(snapshot.level, phase=2)
    lane = _lane_bytes(snapshot)
    frame = new_frame()
    for y in range(HEIGHT):
        for x in range(ICON_WIDTH):
            frame[y][x] = icon.getpixel((x, y))
        for x in range(LANE_WIDTH):
            offset = (y * LANE_WIDTH + x) * 3
            frame[y][LANE_X0 + x] = tuple(lane[offset:offset + 3])
    frame = grade_frame(frame, 2.2, 45, 90)
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    led_image(frame, scale=scale, gap=gap).save(path)
    return path


def run(
    awtrix: str = AWTRIX,
    latitude: float = LATITUDE,
    longitude: float = LONGITUDE,
    once: bool = False,
    dry_run: bool = False,
    static_only: bool = False,
    preview_path: Path | None = None,
    *, stop_event=None, refresh_seconds=None, request_timeout_seconds=None,
    quiet_hours=None, timezone_name=None,
) -> None:
    refresh = REFRESH_SECONDS if refresh_seconds is None else float(refresh_seconds)
    def http(function, *args):
        return function(*args, **({"timeout": request_timeout_seconds} if request_timeout_seconds is not None else {}))
    session = requests.Session()
    try:
        try:
            import ssl
            import truststore
            from requests.adapters import HTTPAdapter
            context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            class _Adapter(HTTPAdapter):
                def init_poolmanager(self, *args: Any, **kwargs: Any) -> Any:
                    kwargs["ssl_context"] = context
                    return super().init_poolmanager(*args, **kwargs)
            session.mount("https://", _Adapter())
        except (ImportError, AttributeError):
            pass
        session.headers.update({"User-Agent": "AWTRIX-AQIBrief/1.0 (Open-Meteo/CAMS)"})
        api_failures = 0
        panel_failures = 0
        safe_mode = bool(static_only)
        previous_health: PanelHealth | None = None
        animated_push_succeeded = False
        remote_visible: bool | None = None
        previous_aqi: int | None = None
        activation_pending = False
        print(f"AQI Brief -> {awtrix.rstrip('/')}{PUSH_PATH} every {refresh}s")
        print("Air-quality data: Open-Meteo / CAMS ENSEMBLE")

        while stop_event is None or not stop_event.is_set():
            try:
                snapshot = http(fetch_air_quality, session, latitude, longitude)
                api_failures = 0
            except (requests.RequestException, ValueError, OSError) as error:
                api_failures += 1
                if api_failures in (1, 3) or api_failures % 12 == 0:
                    print(f"AQI data unavailable ({api_failures}): {error}")
                if once:
                    raise
                wait(stop_event, min(refresh, 30 * (2 ** min(api_failures - 1, 4))), sleep=time.sleep)
                continue

            if stop_event is not None and stop_event.is_set():
                return
            from zoneinfo import ZoneInfo
            local_now = datetime.now(ZoneInfo(timezone_name) if timezone_name else snapshot.observed_at.tzinfo)
            if (
                previous_aqi is not None
                and previous_aqi < AQI_UNHEALTHY_AT <= snapshot.us_aqi
                and not (is_quiet_time(local_now) if quiet_hours is None else in_quiet_hours(local_now, quiet_hours))
            ):
                activation_pending = True
            if snapshot.us_aqi < AQI_UNHEALTHY_AT:
                activation_pending = False

            relevant = is_relevant(snapshot, currently_visible=remote_visible is True)
            if not relevant:
                if dry_run:
                    print(f"Dry run: hidden ({relevance_reason(snapshot)})")
                    remote_visible = False
                    panel_failures = 0
                elif remote_visible is not False:
                    try:
                        http(delete_app, session, awtrix)
                        remote_visible = False
                        panel_failures = 0
                        print(f"Hidden: {relevance_reason(snapshot)}")
                    except (requests.RequestException, RuntimeError, OSError) as error:
                        panel_failures += 1
                        if panel_failures in (1, 3) or panel_failures % 12 == 0:
                            print(f"Panel unavailable ({panel_failures}): {error}")
                        if once:
                            raise
                        wait(stop_event, min(refresh, 15 * (2 ** min(panel_failures - 1, 5))), sleep=time.sleep)
                        continue
                previous_aqi = snapshot.us_aqi
                if once:
                    return
                wait(stop_event, refresh, sleep=time.sleep)
                continue

            if stop_event is not None and stop_event.is_set():
                return

            if not dry_run:
                health = http(read_panel_health, session, awtrix)
                if animated_push_succeeded and unplanned_reboot(previous_health, health):
                    safe_mode = True
                    print("Panel reboot detected after animated AQI; using static icon safety mode")
                if health is not None:
                    previous_health = health

            payload = build_payload(snapshot, animated=not safe_mode)
            if preview_path is not None:
                saved = save_preview(snapshot, preview_path)
                print(f"Preview: {saved}")

            if dry_run:
                body_size = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
                print(
                    f"Dry run: {display_text(snapshot.us_aqi)}; "
                    f"PM2.5={snapshot.pm2_5:g}; payload={body_size} bytes"
                )
                panel_failures = 0
            else:
                try:
                    if stop_event is not None and stop_event.is_set():
                        return
                    http(push_payload, session, awtrix, payload)
                    remote_visible = True
                    animated_push_succeeded = animated_push_succeeded or not safe_mode
                    panel_failures = 0
                    print(
                        f"Updated: {display_text(snapshot.us_aqi)}; PM2.5={snapshot.pm2_5:g}"
                        f"{' [static safety]' if safe_mode else ''}"
                    )
                    if activation_pending:
                        try:
                            http(activate_app, session, awtrix)
                            activation_pending = False
                            print("Activated: AQI crossed into Unhealthy")
                        except (requests.RequestException, RuntimeError, OSError) as error:
                            print(f"AQI activation deferred: {error}")
                except (requests.RequestException, RuntimeError, OSError) as error:
                    panel_failures += 1
                    if panel_failures >= SAFE_MODE_FAILURES and not safe_mode:
                        safe_mode = True
                        print("Repeated panel failures; latching static AQI icon safety mode")
                    if panel_failures in (1, 3, SAFE_MODE_FAILURES) or panel_failures % 12 == 0:
                        print(f"Panel unavailable ({panel_failures}): {error}")
                    if once:
                        raise
                    wait(stop_event, min(refresh, 15 * (2 ** min(panel_failures - 1, 5))), sleep=time.sleep)
                    continue

            previous_aqi = snapshot.us_aqi

            if once:
                return
            wait(stop_event, refresh, sleep=time.sleep)
    finally:
        session.close()


@worker_adapter("aqi")
def run_worker(config: DriftMatrixConfig, stop_event: threading.Event) -> int:
    options = config.services["aqi"].options
    run(awtrix=config.panel.url, latitude=options["latitude"], longitude=options["longitude"],
        static_only=options.get("static_only", False), stop_event=stop_event,
        refresh_seconds=options.get("refresh_seconds"), request_timeout_seconds=options.get("request_timeout_seconds"),
        quiet_hours=config.panel.quiet_hours, timezone_name=config.panel.timezone)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--awtrix", default=AWTRIX)
    parser.add_argument("--latitude", type=float, default=LATITUDE)
    parser.add_argument("--longitude", type=float, default=LONGITUDE)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--static", action="store_true")
    parser.add_argument("--preview", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    stop_event = threading.Event()
    def execute():
        run(
            awtrix=args.awtrix,
            latitude=args.latitude,
            longitude=args.longitude,
            once=args.once,
            dry_run=args.dry_run,
            static_only=args.static,
            preview_path=args.preview,
            stop_event=stop_event,
        )
    return run_owned("aqi", args.awtrix, default_data_dir(), stop_event, execute)


if __name__ == "__main__":
    # pythonw under Task Scheduler has no stdout, so every print() in this file
    # is discarded. sender_logging is in the parent directory, which is not on
    # the path when a file in weather/ is the script being run.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import sender_logging

    log = sender_logging.configure("aqi_brief")
    try:
        raise SystemExit(main())
    except Exception:
        log.exception("sender stopped on an unhandled error")
        raise
