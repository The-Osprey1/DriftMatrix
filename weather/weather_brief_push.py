"""Fetch a concise forecast and push the Jewelbox Weather Brief to AWTRIX.

The PC does the network work.  The panel receives one small pushed app: an
opaque 8x8 animated condition jewel plus a static 23x8 information lane.  The
animation is decoded locally by AWTRIX, so there are no per-frame HTTP writes
and no full-screen GIFs.

Setup:
    C:/Users/devyn/anaconda3/Scripts/pip.exe install requests pillow

Run once without touching the panel:
    C:/Users/devyn/anaconda3/python.exe weather/weather_brief_push.py --dry-run --once

Run continuously:
    C:/Users/devyn/anaconda3/python.exe weather/weather_brief_push.py
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

try:
    from . import weather_canvas as canvas
except ImportError:  # Direct execution from the weather directory.
    import weather_canvas as canvas  # type: ignore[no-redef]

# The panel-wide calm-glance lane lives in the repository root's
# visual_system; the scheduled task runs this file directly, which puts only
# weather/ on sys.path.
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.append(_ROOT)
from visual_system import MIST, render_glance_lane  # noqa: E402
import panel_gate  # noqa: E402
import threading
from service_runtime import DriftMatrixConfig, run_owned, worker_adapter, wait
from focus_control import default_data_dir

try:
    from .weather_brief_preview import (
        AMBER,
        AZURE,
        BLACK,
        GOLD,
        ICE,
        INDIGO,
        LANE_X0,
        MINT,
        PEARL,
        SKY,
        TRACK,
        Frame,
        draw_condition_jewel,
        draw_text,
        gamma_lerp,
        grade_frame,
        led_image,
        new_frame,
        put,
        temperature_rail,
        text_width,
    )
except ImportError:  # Direct execution from the weather directory.
    from weather_brief_preview import (  # type: ignore[no-redef]
        AMBER,
        AZURE,
        BLACK,
        GOLD,
        ICE,
        INDIGO,
        LANE_X0,
        MINT,
        PEARL,
        SKY,
        TRACK,
        Frame,
        draw_condition_jewel,
        draw_text,
        gamma_lerp,
        grade_frame,
        led_image,
        new_frame,
        put,
        temperature_rail,
        text_width,
    )


RGB = tuple[int, int, int]

# mDNS currently advertises a stale second address on this LAN. Keep the known
# panel address explicit until the reservation/hostname is proven stable.
AWTRIX = "http://192.168.1.90"
APP_NAME = "weatherbrief"
GATE_SOURCE = "weather"
PUSH_PATH = f"/api/v1/apps/pushed/{APP_NAME}"
DEVICE_PATH = "/api/v1/device"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
LATITUDE = 33.87529
LONGITUDE = -117.56644

REFRESH_SECONDS = 300
LIFETIME_MS = 900_000
PAGE_DURATION_MS = 7_000
REQUEST_TIMEOUT_SECONDS = 10

ICON_WIDTH = 8
ICON_HEIGHT = 8
# The firmware imposes none of these: the body cap is 8192 bytes and there is
# no icon byte or frame limit in AWTRIX NG 1.1.0 (verified in source and by
# pushing a 12-frame GIF to the panel).  The old 6-frame / 1024-byte rails were
# self-imposed, and they are what kept the panel static.  These are now the
# canvas's own guard rails, raised deliberately rather than inherited.
ICON_FRAME_MS = 120
ICON_MAX_FRAMES = 32
ICON_MAX_BYTES = 4_200
MAX_BODY_BYTES = 8_192
SAFE_BODY_BYTES = 2_048
LANE_WIDTH = 23
LANE_HEIGHT = 8

RAIN_WINDOW_MINUTES = 120
RAIN_PROBABILITY_THRESHOLD = 45
RAIN_AMOUNT_THRESHOLD_MM = 0.10
MAX_OBSERVATION_AGE_SECONDS = 45 * 60
MAX_OBSERVATION_LEAD_SECONDS = 20 * 60
SAFE_MODE_FAILURES = 5


@dataclass(frozen=True)
class WeatherSnapshot:
    observed_at: datetime
    timezone_name: str
    temperature_f: float
    weather_code: int
    is_day: bool
    low_f: float
    high_f: float
    forecast_times: tuple[datetime, ...]
    precipitation_probabilities: tuple[int, ...]
    precipitation_mm: tuple[float, ...]
    forecast_codes: tuple[int, ...]


@dataclass(frozen=True)
class WeatherScene:
    kind: str
    text: str
    icon_kind: str
    is_day: bool
    temperature_f: float
    low_f: float
    high_f: float
    eta_minutes: int | None
    precipitation_probabilities: tuple[int, ...] = ()
    precipitation_mm: tuple[float, ...] = ()


@dataclass(frozen=True)
class PanelHealth:
    uptime_seconds: int
    reset_reason: str


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _finite_number(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be a finite number") from error
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _integer(value: Any, name: str) -> int:
    return int(round(_finite_number(value, name)))


def _first(value: Any, name: str) -> Any:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError(f"{name} must contain at least one value")
    return value[0]


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


def _coerce_now(now: datetime | None, zone: timezone | ZoneInfo) -> datetime:
    if now is None:
        return datetime.now(zone)
    if now.tzinfo is None:
        return now.replace(tzinfo=zone)
    return now.astimezone(zone)


def parse_weather(payload: Mapping[str, Any], now: datetime | None = None) -> WeatherSnapshot:
    """Validate and normalize one Open-Meteo response."""

    root = _mapping(payload, "weather response")
    zone_name = root.get("timezone") if isinstance(root.get("timezone"), str) else "UTC"
    zone = _timezone(zone_name)
    current = _mapping(root.get("current"), "current")
    daily = _mapping(root.get("daily"), "daily")
    minute = _mapping(root.get("minutely_15", {}), "minutely_15")

    observed_raw = current.get("time")
    observed_at = (
        _local_datetime(observed_raw, zone, "current.time")
        if observed_raw is not None
        else _coerce_now(now, zone)
    )

    times_raw = minute.get("time", ())
    probabilities_raw = minute.get("precipitation_probability", ())
    precipitation_raw = minute.get("precipitation", ())
    codes_raw = minute.get("weather_code", ())
    sequences = (times_raw, probabilities_raw, precipitation_raw, codes_raw)
    if any(not isinstance(values, Sequence) or isinstance(values, (str, bytes)) for values in sequences):
        raise ValueError("minutely_15 fields must be arrays")
    lengths = {len(values) for values in sequences}
    if len(lengths) > 1:
        raise ValueError("minutely_15 arrays must have matching lengths")
    count = len(times_raw)

    forecast_times: list[datetime] = []
    probabilities: list[int] = []
    precipitation: list[float] = []
    codes: list[int] = []
    for index in range(count):
        forecast_times.append(_local_datetime(times_raw[index], zone, f"minutely_15.time[{index}]"))
        probability = _integer(probabilities_raw[index], f"precipitation_probability[{index}]")
        probabilities.append(max(0, min(100, probability)))
        amount = _finite_number(precipitation_raw[index], f"precipitation[{index}]")
        precipitation.append(max(0.0, amount))
        codes.append(_integer(codes_raw[index], f"weather_code[{index}]"))

    reference_now = _coerce_now(now, zone)
    observation_age = (reference_now - observed_at).total_seconds()
    if observation_age > MAX_OBSERVATION_AGE_SECONDS:
        raise ValueError("Open-Meteo observation is stale")
    if observation_age < -MAX_OBSERVATION_LEAD_SECONDS:
        raise ValueError("Open-Meteo observation is unexpectedly in the future")

    return WeatherSnapshot(
        observed_at=observed_at,
        timezone_name=str(zone_name),
        temperature_f=_finite_number(current.get("temperature_2m"), "temperature_2m"),
        weather_code=_integer(current.get("weather_code"), "current.weather_code"),
        is_day=bool(_integer(current.get("is_day"), "is_day")),
        low_f=_finite_number(_first(daily.get("temperature_2m_min"), "temperature_2m_min"), "low"),
        high_f=_finite_number(_first(daily.get("temperature_2m_max"), "temperature_2m_max"), "high"),
        forecast_times=tuple(forecast_times),
        precipitation_probabilities=tuple(probabilities),
        precipitation_mm=tuple(precipitation),
        forecast_codes=tuple(codes),
    )


def weather_category(code: int) -> str:
    code = int(code)
    if code == 0:
        return "clear"
    if code <= 2:
        return "part"
    if code == 3:
        return "cloud"
    if 45 <= code <= 48:
        return "fog"
    if 51 <= code <= 67 or 80 <= code <= 82:
        return "rain"
    if 71 <= code <= 77 or code in (85, 86):
        return "snow"
    if 95 <= code <= 99:
        return "storm"
    return "cloud"


def _temperature_text(value: float) -> str:
    rounded = max(-99, min(199, int(round(value))))
    return f"{rounded}\N{DEGREE SIGN}"


def _eta_text(minutes: int) -> str:
    if minutes <= 0:
        return "NOW"
    if minutes <= 99:
        return f"{minutes}M"
    return f"{max(1, round(minutes / 60))}H"


def _rain_candidate(probability: int, amount_mm: float, code: int) -> bool:
    category = weather_category(code)
    return (
        probability >= RAIN_PROBABILITY_THRESHOLD
        or amount_mm >= RAIN_AMOUNT_THRESHOLD_MM
        or (category in {"rain", "snow", "storm"} and probability >= 25)
    )


def choose_scene(snapshot: WeatherSnapshot, now: datetime | None = None) -> WeatherScene:
    """Prefer a useful precipitation countdown; otherwise show current weather."""

    zone = snapshot.observed_at.tzinfo or timezone.utc
    local_now = _coerce_now(now, zone)
    current_kind = weather_category(snapshot.weather_code)

    if current_kind in {"rain", "snow", "storm"}:
        return WeatherScene(
            kind="rain",
            text="NOW",
            icon_kind=current_kind,
            is_day=snapshot.is_day,
            temperature_f=snapshot.temperature_f,
            low_f=snapshot.low_f,
            high_f=snapshot.high_f,
            eta_minutes=0,
            precipitation_probabilities=snapshot.precipitation_probabilities,
            precipitation_mm=snapshot.precipitation_mm,
        )

    for index, forecast_time in enumerate(snapshot.forecast_times):
        delta_minutes = (forecast_time - local_now).total_seconds() / 60.0
        if delta_minutes < -15:
            continue
        if delta_minutes > RAIN_WINDOW_MINUTES:
            break
        probability = snapshot.precipitation_probabilities[index]
        amount = snapshot.precipitation_mm[index]
        code = snapshot.forecast_codes[index]
        if _rain_candidate(probability, amount, code):
            forecast_kind = weather_category(code)
            if (
                delta_minutes <= 0
                and amount < RAIN_AMOUNT_THRESHOLD_MM
                and forecast_kind not in {"rain", "snow", "storm"}
            ):
                # A probability attached to the already-started dry interval
                # is not evidence that rain is falling NOW.
                continue
            eta = max(0, int(math.ceil(delta_minutes)))
            icon_kind = forecast_kind
            if icon_kind not in {"rain", "snow", "storm"}:
                icon_kind = "rain"
            return WeatherScene(
                kind="rain",
                text=_eta_text(eta),
                icon_kind=icon_kind,
                is_day=snapshot.is_day,
                temperature_f=snapshot.temperature_f,
                low_f=snapshot.low_f,
                high_f=snapshot.high_f,
                eta_minutes=eta,
                precipitation_probabilities=snapshot.precipitation_probabilities,
                precipitation_mm=snapshot.precipitation_mm,
            )

    return WeatherScene(
        kind="current",
        text=_temperature_text(snapshot.temperature_f),
        icon_kind=current_kind,
        is_day=snapshot.is_day,
        temperature_f=snapshot.temperature_f,
        low_f=snapshot.low_f,
        high_f=snapshot.high_f,
        eta_minutes=None,
        precipitation_probabilities=snapshot.precipitation_probabilities,
        precipitation_mm=snapshot.precipitation_mm,
    )


# Shared with the AQI page, which bins five hourly samples the same way.
sample_bounds = canvas.sample_bounds


def timeline_columns(
    probabilities: Sequence[int | float],
    precipitation_mm: Sequence[int | float] | None = None,
    width: int = LANE_WIDTH,
) -> list[RGB]:
    """Map forecast bins to the physical LEDs without inventing samples."""

    if not probabilities:
        return [TRACK] * width
    amounts = precipitation_mm or ()
    spans = sample_bounds(len(probabilities), width)
    column_to_sample: list[int] = [0] * width
    for sample, (start, stop) in enumerate(spans):
        for column in range(start, stop):
            column_to_sample[column] = sample
    result: list[RGB] = []
    for column in range(width):
        index = column_to_sample[column]
        try:
            probability = max(0.0, min(100.0, float(probabilities[index])))
        except (TypeError, ValueError, OverflowError):
            probability = 0.0
        try:
            amount = max(0.0, float(amounts[index])) if index < len(amounts) else 0.0
        except (TypeError, ValueError, OverflowError):
            amount = 0.0
        strength = max(probability / 100.0, min(1.0, amount / 1.5))
        if strength < 0.10:
            result.append(TRACK)
        elif strength < 0.50:
            result.append(gamma_lerp(AZURE, SKY, (strength - 0.10) / 0.40))
        else:
            result.append(gamma_lerp(SKY, ICE, (strength - 0.50) / 0.50))
    return result


def _draw_current_lane(frame: Frame, scene: WeatherScene) -> None:
    rail, marker = temperature_rail(scene.low_f, scene.temperature_f, scene.high_f)
    for index, color in enumerate(rail):
        put(frame, LANE_X0 + index, 7, color)
    marker_x = LANE_X0 + marker
    put(frame, marker_x, 6, PEARL)
    put(frame, marker_x, 7, PEARL)


def _draw_rain_lane(frame: Frame, scene: WeatherScene) -> None:
    # The selected Jewelbox board reads time toward a fixed NOW cap at right.
    # timeline_columns itself stays chronological for testing/data integrity;
    # reversing only at draw time makes the screen-space direction explicit.
    forecast = list(
        reversed(timeline_columns(scene.precipitation_probabilities, scene.precipitation_mm))
    )
    for index, color in enumerate(forecast[:-1]):
        put(frame, LANE_X0 + index, 7, color)
    put(frame, 31, 6, PEARL)
    put(frame, 31, 7, PEARL)


def scene_intensities(scene: WeatherScene, width: int) -> list[float]:
    """Per-column precipitation amount, used as bar height on the strip.

    Colour on the strip carries probability; height carries how much actually
    falls.  Splitting the two means a 90%-chance drizzle and a 90%-chance
    downpour no longer render identically.
    """

    amounts = scene.precipitation_mm or ()
    if not amounts:
        return [0.0] * width
    out = [0.0] * width
    for sample, (start, stop) in enumerate(sample_bounds(len(amounts), width)):
        try:
            value = float(amounts[sample])
        except (TypeError, ValueError, OverflowError):
            value = 0.0
        for column in range(start, stop):
            out[column] = 1.0 if value >= RAIN_AMOUNT_THRESHOLD_MM * 5 else 0.0
    return out


def _paint_strip(scene: WeatherScene):
    """The bottom two rows: a precipitation timeline, or a temperature gauge."""

    if scene.kind == "rain":
        columns = timeline_columns(
            scene.precipitation_probabilities,
            scene.precipitation_mm,
            width=canvas.TIMELINE_W,
        )
        heights = scene_intensities(scene, canvas.TIMELINE_W)
        return lambda frame, step: canvas.paint_timeline(frame, columns, heights)
    rail, marker = temperature_rail(
        scene.low_f, scene.temperature_f, scene.high_f, width=canvas.STRIP_W
    )
    return lambda frame, step: canvas.paint_rail(frame, rail, marker)


def _paint_text(scene: WeatherScene):
    def paint(frame: Frame, step: int) -> None:
        x = canvas.STRIP_X0 + (canvas.STRIP_W - text_width(scene.text)) // 2
        if scene.kind == "rain" and scene.text[-1:] in {"M", "H"}:
            number = scene.text[:-1]
            draw_text(frame, number, x, 1, PEARL)
            draw_text(frame, scene.text[-1], x + text_width(number) + 1, 1, ICE)
        elif scene.kind == "rain":
            draw_text(frame, scene.text, x, 1, SKY)
        else:
            draw_text(frame, scene.text, x, 1, PEARL, degree_color=MIST)

    return paint


# Calm glance (2026-09-26): the lane is the panel-wide layout -- the value in
# white and one meter on row 7.  Current conditions: temperature on a 40-110 F
# scale, coloured by how it feels.  Rain coming: minutes until it starts, with
# the meter showing the highest chance in the next two hours.  The icon keeps
# the condition's colour and motion.
TEMP_SCALE_F = (40, 110)


def temperature_level(temp_f: float) -> str:
    if temp_f < 50:
        return "info"
    if temp_f < 85:
        return "good"
    if temp_f < 95:
        return "fair"
    if temp_f < 105:
        return "poor"
    return "bad"


def glance_lane(scene: WeatherScene) -> Frame:
    if scene.kind == "rain":
        chances = [p for p in scene.precipitation_probabilities if isinstance(p, (int, float))]
        meter = max(chances) / 100 if chances else None
        return render_glance_lane(scene.text, meter=meter, level="info")
    lo, hi = TEMP_SCALE_F
    return render_glance_lane(
        scene.text,
        meter=(scene.temperature_f - lo) / (hi - lo),
        level=temperature_level(scene.temperature_f),
    )


def scene_frames(scene: WeatherScene, animated: bool = True) -> tuple[list[Frame], int]:
    """The whole 32x8 panel, composed, one entry per animation frame."""

    frames, delay = canvas.build(
        scene.icon_kind, bool(scene.is_day), _paint_strip(scene), _paint_text(scene)
    )
    if not animated:
        return frames[:1], delay
    return frames, delay


def render_scene_frame(scene: WeatherScene, phase: int = 0) -> Frame:
    """One composed frame, for previews and for the legacy lane helpers."""

    frames, _ = scene_frames(scene)
    return frames[phase % len(frames)]


def _legacy_scene_frame(scene: WeatherScene, phase: int = 0) -> Frame:
    frame = new_frame()
    draw_condition_jewel(frame, scene.icon_kind, scene.is_day, phase)
    if scene.kind == "rain":
        _draw_rain_lane(frame, scene)
    else:
        _draw_current_lane(frame, scene)
    x = LANE_X0 + (LANE_WIDTH - text_width(scene.text)) // 2
    if scene.kind == "rain" and scene.text[-1:] in {"M", "H"}:
        number = scene.text[:-1]
        draw_text(frame, number, x, 1, PEARL)
        draw_text(frame, scene.text[-1], x + text_width(number) + 1, 1, ICE)
    elif scene.kind == "rain":
        draw_text(frame, scene.text, x, 1, SKY)
    else:
        draw_text(frame, scene.text, x, 1, PEARL, degree_color=MIST)
    return frame


def _lane_bytes(scene: WeatherScene) -> bytes:
    """Retained for the legacy renderer's tests; no longer sent to the panel."""

    frame = _legacy_scene_frame(scene, phase=0)
    return bytes(
        channel
        for row in frame
        for color in row[LANE_X0 : LANE_X0 + LANE_WIDTH]
        for channel in color
    )


ICON_SIZE = (canvas.ICON_W, canvas.HEIGHT)


def validate_icon_gif(data: bytes) -> None:
    """Check the icon GIF against the limits that are actually real.

    Only 8px and exactly 32px are safe widths - anything between is blitted
    last, painting over the text, and only indents text by 9px.  This ships the
    8px form: a full-width GIF costs up to 10 KB of the panel's largest
    contiguous free block, measured, and that block is the scarce resource.
    """

    if len(data) > ICON_MAX_BYTES:
        raise ValueError(f"weather icon is {len(data)} bytes; limit is {ICON_MAX_BYTES}")
    if not data.startswith((b"GIF87a", b"GIF89a")):
        raise ValueError("weather icon is not a GIF")
    try:
        with Image.open(BytesIO(data)) as image:
            if image.size != ICON_SIZE:
                raise ValueError(f"weather icon must be exactly {ICON_SIZE[0]}x{ICON_SIZE[1]}")
            if not 1 <= image.n_frames <= ICON_MAX_FRAMES:
                raise ValueError("weather icon frame count exceeds the safety limit")
            if "transparency" in image.info:
                raise ValueError("weather icon must be fully opaque")
            for frame_index in range(image.n_frames):
                image.seek(frame_index)
                if image.size != ICON_SIZE:
                    raise ValueError("every weather icon frame must be the same size")
                duration = image.info.get("duration", 0)
                if duration % 120:
                    raise ValueError(
                        f"frame delay {duration}ms is not a multiple of the 120ms "
                        "beat and will judder against the 24ms render tick"
                    )
    except OSError as error:
        raise ValueError("weather icon is not readable") from error


def build_icon_gif(scene: WeatherScene, animated: bool = True) -> bytes:
    """An 8x8 icon GIF, cropped from the composed canvas.

    A full-width 32x8 GIF *is* legal and does animate the whole panel - I
    verified that on the hardware.  It is not used, because I also measured
    what it costs.  With one resident, the panel's largest contiguous free
    block drops in proportion to the decoded GIF:

        rain   373 B / 3 frames   ->   -3,072 bytes
        night  771 B / 8 frames   ->   -4,096 bytes
        fog  1,246 B / 8 frames   ->  -10,240 bytes

    Free heap is not the problem; the contiguous block is. That block is what
    installing a Berry script needs (roughly 2x the source) and what a TLS
    handshake needs, so spending 10 KB of it on a background animation is a bad
    trade on this panel.

    So the icon stays 8x8 and the lane stays a static bitmap. Everything else
    from the redesign - the silhouette-first icon set, the temperature rail
    that actually encodes the day, the even timeline bins, the two-row strip -
    is unaffected by this and is kept.
    """

    frames, delay = scene_frames(scene, animated=animated)
    icon_frames = [[row[0:canvas.ICON_W] for row in frame] for frame in frames]
    data = canvas.to_gif(icon_frames, delay, width=canvas.ICON_W)
    validate_icon_gif(data)
    return data


def build_payload(scene: WeatherScene, animated: bool = True) -> dict[str, Any]:
    icon = build_icon_gif(scene, animated=animated)
    lane = base64.b64encode(_canvas_lane_bytes(scene)).decode("ascii")
    payload: dict[str, Any] = {
        "icon": base64.b64encode(icon).decode("ascii"),
        "iconMode": "fixed",
        "iconOffsetX": 0,
        "draw": [["bitmap", LANE_X0, 0, LANE_WIDTH, LANE_HEIGHT, lane]],
        "backgroundColor": "#000000",
        "durationMs": PAGE_DURATION_MS,
        "lifetimeMs": LIFETIME_MS,
        "lifetimeExpiry": "remove",
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(body) >= MAX_BODY_BYTES:
        raise ValueError(f"weather payload unexpectedly grew to {len(body)} bytes")
    if len(body) >= SAFE_BODY_BYTES:
        raise ValueError(f"weather payload exceeded the {SAFE_BODY_BYTES}-byte safety budget")
    return payload


def _canvas_lane_bytes(scene: WeatherScene) -> bytes:
    """The redesigned lane, frozen at frame 0, as raw RGB888."""

    frame = render_scene_frame(scene, phase=0)
    return bytes(
        channel
        for row in frame
        for color in row[LANE_X0 : LANE_X0 + LANE_WIDTH]
        for channel in color
    )


def request_params(latitude: float = LATITUDE, longitude: float = LONGITUDE) -> dict[str, Any]:
    return {
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,weather_code,is_day",
        "minutely_15": "precipitation_probability,precipitation,weather_code",
        "daily": "temperature_2m_max,temperature_2m_min,weather_code,sunrise,sunset",
        "forecast_minutely_15": 8,
        "forecast_days": 1,
        "temperature_unit": "fahrenheit",
        "precipitation_unit": "mm",
        "timezone": "auto",
    }


def fetch_weather(
    session: requests.Session,
    latitude: float = LATITUDE,
    longitude: float = LONGITUDE,
    now: datetime | None = None,
    *, timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> WeatherSnapshot:
    response = session.get(
        OPEN_METEO_URL,
        params=request_params(latitude, longitude),
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise ValueError("Open-Meteo returned a non-object response")
    return parse_weather(payload, now=now)


def push_payload(session: requests.Session, base_url: str, payload: Mapping[str, Any], *, timeout: float = REQUEST_TIMEOUT_SECONDS) -> None:
    body = dict(payload)
    with panel_gate.mutation(GATE_SOURCE, "push", APP_NAME, body=body) as gate:
        response = session.put(
            f"{base_url.rstrip('/')}{PUSH_PATH}",
            json=body,
            timeout=timeout,
        )
        gate.observe(response)
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"AWTRIX HTTP {response.status_code}: {response.text[:200]}")


def read_panel_health(session: requests.Session, base_url: str, *, timeout: float = REQUEST_TIMEOUT_SECONDS) -> PanelHealth | None:
    """Read a boot marker without touching display or framebuffer state."""

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


def save_preview(scene: WeatherScene, path: Path, scale: int = 18, gap: int = 1) -> Path:
    frame = grade_frame(render_scene_frame(scene), gamma=1.8, brightness=45, saturation=90)
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    led_image(frame, scale=scale, gap=gap).save(path)
    return path


def _scene_summary(scene: WeatherScene) -> str:
    if scene.kind == "rain":
        return f"{scene.icon_kind} {scene.text} (eta={scene.eta_minutes}m)"
    return f"{scene.icon_kind} {scene.text} ({scene.low_f:.0f}-{scene.high_f:.0f}F)"


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
                def init_poolmanager(self, *args: Any, **kw: Any) -> Any:
                    kw["ssl_context"] = context
                    return super().init_poolmanager(*args, **kw)

            session.mount("https://", _Adapter())
        except (ImportError, AttributeError):
            pass
        session.headers.update({"User-Agent": "AWTRIX-WeatherBrief/1.0"})
        api_failures = 0
        panel_failures = 0
        safe_mode = bool(static_only)
        previous_health: PanelHealth | None = None
        animated_push_succeeded = False
        print(f"Weather Brief -> {awtrix.rstrip('/')}{PUSH_PATH} every {refresh}s")
        while stop_event is None or not stop_event.is_set():
            try:
                snapshot = http(fetch_weather, session, latitude, longitude)
                scene = choose_scene(snapshot)
                api_failures = 0
            except (requests.RequestException, ValueError, OSError) as error:
                api_failures += 1
                if api_failures in (1, 3) or api_failures % 12 == 0:
                    print(f"Weather data unavailable ({api_failures}): {error}")
                if once:
                    raise
                delay = min(refresh, 15 * (2 ** min(api_failures - 1, 5)))
                wait(stop_event, delay, sleep=time.sleep)
                continue

            if stop_event is not None and stop_event.is_set():
                return

            if not dry_run:
                health = http(read_panel_health, session, awtrix)
                if animated_push_succeeded and unplanned_reboot(previous_health, health):
                    safe_mode = True
                    print("Panel reboot detected after animated weather; using static icon safety mode")
                if health is not None:
                    previous_health = health

            payload = build_payload(scene, animated=not safe_mode)
            if preview_path is not None:
                saved = save_preview(scene, preview_path)
                print(f"Preview: {saved}")
            if dry_run:
                body_size = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
                print(f"Dry run: {_scene_summary(scene)}; payload={body_size} bytes")
                panel_failures = 0
            else:
                try:
                    if stop_event is not None and stop_event.is_set():
                        return
                    http(push_payload, session, awtrix, payload)
                    animated_push_succeeded = animated_push_succeeded or not safe_mode
                    panel_failures = 0
                    print(f"Updated: {_scene_summary(scene)}{' [static safety]' if safe_mode else ''}")
                except (requests.RequestException, RuntimeError, OSError) as error:
                    panel_failures += 1
                    if panel_failures >= SAFE_MODE_FAILURES and not safe_mode:
                        safe_mode = True
                        print("Repeated panel failures; latching static icon safety mode")
                    if panel_failures in (1, 3, SAFE_MODE_FAILURES) or panel_failures % 12 == 0:
                        print(f"Panel unavailable ({panel_failures}): {error}")
                    if once:
                        raise
                    delay = min(refresh, 15 * (2 ** min(panel_failures - 1, 5)))
                    wait(stop_event, delay, sleep=time.sleep)
                    continue

            if once:
                return
            delay = refresh
            wait(stop_event, delay, sleep=time.sleep)
    finally:
        session.close()


@worker_adapter("weather")
def run_worker(config: DriftMatrixConfig, stop_event: threading.Event) -> int:
    options = config.services["weather"].options
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
    parser.add_argument(
        "--static",
        action="store_true",
        help="send a one-frame icon for the first live canary or safety fallback",
    )
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
    return run_owned("weather", args.awtrix, default_data_dir(), stop_event, execute)


if __name__ == "__main__":
    # This sender runs from Task Scheduler under pythonw, where sys.stdout is
    # None and print() does nothing at all. sender_logging lives in the parent
    # directory, which is not on the path when a file in weather/ is the script.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import sender_logging

    log = sender_logging.configure("weather_brief")
    try:
        raise SystemExit(main())
    except Exception:
        log.exception("sender stopped on an unhandled error")
        raise
