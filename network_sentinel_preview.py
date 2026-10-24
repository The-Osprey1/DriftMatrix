"""Exact 32x8 artwork and compact AWTRIX payloads for Network Sentinel.

The screen has two deliberately independent regions::

    x=0..7   a tiny, truthful PC -> router -> internet packet path
    x=8      an always-black safety gutter
    x=9..31  one stable, centered 3x5 diagnosis

Only the packet path moves.  The diagnostic lane is raw RGB888 data, so it
cannot jitter or inherit AWTRIX text scrolling.  The icon is an opaque 8x8 GIF
whose six 120 ms frames stay well below the panel's memory-risky full-screen
animation size.
"""

from __future__ import annotations

import base64
from enum import Enum
from functools import lru_cache
from io import BytesIO
import json
import math
from typing import Iterable

from PIL import Image

from visual_system import LANE_X0 as GLANCE_LANE_X0, gamma_lerp, render_glance_lane


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

PANEL_GAMMA = 2.2
PANEL_SATURATION = 90
FRAME_COUNT = 6
FRAME_MS = 120
ICON_MAX_BYTES = 900
PAYLOAD_MAX_BYTES = 2_048
PAGE_DURATION_MS = 7_000
LIFETIME_MS = 45_000

# Canonical high-energy source colours selected for gamma 2.2.  The panel does
# the gamma conversion; these are intentionally not pre-darkened desktop RGB.
BLACK: RGB = (0, 0, 0)
PEARL: RGB = (255, 247, 232)
ICE: RGB = (222, 235, 250)
SLATE: RGB = (174, 190, 214)
SKY: RGB = (88, 205, 255)
GOLD: RGB = (255, 214, 78)
AMBER: RGB = (255, 158, 58)
CORAL: RGB = (255, 82, 104)
VIOLET: RGB = (204, 116, 255)
MINT: RGB = (82, 240, 168)
TRACK_COOL: RGB = (90, 106, 142)
TRACK_WARM: RGB = (112, 78, 96)


class NetworkState(str, Enum):
    """Visual states shared with the Network Sentinel state machine."""

    ROUTER = "router"
    WAN = "wan"
    DNS = "dns"
    LOGIN = "login"
    SLOW = "slow"
    LOSS = "loss"
    RECOVERY = "recovery"


STATE_ALIASES = {
    "captive": NetworkState.LOGIN,
    "captive_portal": NetworkState.LOGIN,
    "back": NetworkState.RECOVERY,
    "recovered": NetworkState.RECOVERY,
}


# Fixed 3x5 cells make the longest label, ROUTER, exactly 23 pixels wide.
FONT_3X5: dict[str, tuple[str, ...]] = {
    " ": ("000", "000", "000", "000", "000"),
    "%": ("101", "001", "010", "100", "101"),
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("110", "001", "010", "100", "111"),
    "3": ("110", "001", "010", "001", "110"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "110", "001", "110"),
    "6": ("011", "100", "111", "101", "111"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "110"),
    "A": ("010", "101", "111", "101", "101"),
    "B": ("110", "101", "110", "101", "110"),
    "C": ("111", "100", "100", "100", "111"),
    "D": ("110", "101", "101", "101", "110"),
    "E": ("111", "100", "110", "100", "111"),
    "G": ("111", "100", "101", "101", "111"),
    "I": ("111", "010", "010", "010", "111"),
    "K": ("101", "101", "110", "101", "101"),
    "L": ("100", "100", "100", "100", "111"),
    "M": ("101", "111", "111", "101", "101"),
    "N": ("101", "111", "111", "111", "101"),
    # Rounded, so the letter O is not byte-identical to the digit 0.
    # It was, in every font in this repo.  Rounding the letter rather
    # than altering the digit also dissolves O~8, O~G and O~U, which
    # were each a single pixel apart, and costs no width.
    "O": ("010", "101", "101", "101", "010"),
    "R": ("110", "101", "110", "101", "101"),
    "S": ("111", "100", "111", "001", "111"),
    "T": ("111", "010", "010", "010", "010"),
    "U": ("101", "101", "101", "101", "111"),
    "W": ("101", "101", "111", "111", "101"),
}


# One Wi-Fi glyph (2026-10-03, to the Now Playing standard) instead of a
# PC -> router -> globe diagram crammed into 8x8.  The label already names
# the fault; the icon says how far the signal gets, in the fault's colour:
#   ROUTER    a lonely blinking dot - nothing past this PC
#   WAN       dot and inner arc lit, the outer arcs searching
#   DNS/LOGIN every arc lit, the outer one breathing
#   SLOW      the arcs climb one at a time, then hold
#   LOSS      the outer arcs drop out on the truthful share of frames
#   RECOVERY  the arcs fill in mint
# Unlit arcs are a dark tint of the colour, never off, so the symbol's
# footprint is constant and nothing blinks out of existence.
WIFI_ARCS: tuple[tuple[tuple[int, int], ...], ...] = (
    ((3, 7), (4, 7)),                                                  # the dot
    ((3, 5), (4, 5), (2, 6), (5, 6)),                                  # inner arc
    ((2, 3), (3, 3), (4, 3), (5, 3), (1, 4), (6, 4)),                  # middle arc
    ((2, 0), (3, 0), (4, 0), (5, 0), (1, 1), (6, 1), (0, 2), (7, 2)),  # outer arc
)
WIFI_FOOTPRINT = frozenset(point for arc in WIFI_ARCS for point in arc)
ARC_TINT = 0.22


LABEL_COLORS: dict[NetworkState, RGB] = {
    NetworkState.ROUTER: CORAL,
    NetworkState.WAN: CORAL,
    NetworkState.DNS: VIOLET,
    NetworkState.LOGIN: AMBER,
    NetworkState.SLOW: GOLD,
    NetworkState.LOSS: CORAL,
    NetworkState.RECOVERY: MINT,
}


def normalize_state(state: str | Enum) -> NetworkState:
    """Accept a lowercase string or any Enum whose value is a state string."""

    raw = state.value if isinstance(state, Enum) else state
    if not isinstance(raw, str):
        raise ValueError(f"unsupported Network Sentinel state: {state!r}")
    key = raw.strip().lower().replace("-", "_").replace(" ", "_")
    if key in STATE_ALIASES:
        return STATE_ALIASES[key]
    try:
        return NetworkState(key)
    except ValueError as error:
        raise ValueError(f"unsupported Network Sentinel state: {state!r}") from error


def _bounded_int(value: float, low: int, high: int, fallback: int) -> int:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(numeric):
        return fallback
    return max(low, min(high, int(math.floor(numeric + 0.5))))


def state_label(
    state: str | Enum,
    *,
    latency_ms: float = 180,
    loss_percent: float = 30,
) -> str:
    """Return the concise diagnosis shown in the stable 23-pixel lane."""

    state = normalize_state(state)
    if state is NetworkState.SLOW:
        return f"{_bounded_int(latency_ms, 0, 999, 180)}MS"
    if state is NetworkState.LOSS:
        return f"L {_bounded_int(loss_percent, 0, 99, 30)}%"
    return {
        NetworkState.ROUTER: "ROUTER",
        NetworkState.WAN: "WAN",
        NetworkState.DNS: "DNS",
        NetworkState.LOGIN: "LOGIN",
        NetworkState.RECOVERY: "BACK",
    }[state]


def text_width(text: str) -> int:
    if any(character not in FONT_3X5 for character in text):
        raise ValueError(f"unsupported 3x5 text: {text!r}")
    return 4 * len(text) - 1 if text else 0


def new_frame(width: int = WIDTH) -> Frame:
    return [[BLACK for _ in range(width)] for _ in range(HEIGHT)]


def _put(frame: Frame, x: int, y: int, color: RGB) -> None:
    if 0 <= y < len(frame) and 0 <= x < len(frame[y]):
        frame[y][x] = color


def _paint(frame: Frame, points: Iterable[tuple[int, int]], color: RGB) -> None:
    for x, y in points:
        _put(frame, x, y, color)


def draw_text(frame: Frame, text: str, x0: int, y0: int, color: RGB) -> None:
    x = x0
    for index, character in enumerate(text):
        glyph = FONT_3X5[character]
        for gy, row in enumerate(glyph):
            for gx, bit in enumerate(row):
                if bit == "1":
                    _put(frame, x + gx, y0 + gy, color)
        x += 3 + (1 if index < len(text) - 1 else 0)


def lane_frame(
    state: str | Enum,
    *,
    latency_ms: float = 180,
    loss_percent: float = 30,
) -> Frame:
    """Calm glance: the diagnosis in white; latency or loss as the meter.

    The icon's route colours already say where the fault is; the lane says
    what, and for SLOW and LOSS the meter says how bad on a fixed scale.
    """
    state = normalize_state(state)
    label = state_label(state, latency_ms=latency_ms, loss_percent=loss_percent)
    meter, level = None, "info"
    if state is NetworkState.SLOW:
        meter, level = _bounded_int(latency_ms, 0, 999, 180) / 500, "poor"
    elif state is NetworkState.LOSS:
        meter, level = _bounded_int(loss_percent, 0, 99, 30) / 100, "bad"
    lane = render_glance_lane(label, meter=meter, level=level)
    return [row[GLANCE_LANE_X0:GLANCE_LANE_X0 + LANE_WIDTH] for row in lane]


def lane_bytes(
    state: str | Enum,
    *,
    latency_ms: float = 180,
    loss_percent: float = 30,
) -> bytes:
    frame = lane_frame(state, latency_ms=latency_ms, loss_percent=loss_percent)
    return bytes(channel for row in frame for color in row for channel in color)


def _paint_arcs(state: NetworkState, levels: tuple[float, float, float, float]) -> Frame:
    frame = new_frame(ICON_WIDTH)
    colour = LABEL_COLORS[state] if state is not NetworkState.SLOW else AMBER
    tint = tuple(max(0, min(255, round(c * ARC_TINT))) for c in colour)
    for arc, level in zip(WIFI_ARCS, levels):
        shade = gamma_lerp(tint, colour, level)  # type: ignore[arg-type]
        _paint(frame, arc, shade)
    return frame


RESTING_LEVELS: dict[NetworkState, tuple[float, float, float, float]] = {
    NetworkState.ROUTER: (1.0, 0.0, 0.0, 0.0),
    NetworkState.WAN: (1.0, 1.0, 0.0, 0.0),
    NetworkState.DNS: (1.0, 1.0, 1.0, 1.0),
    NetworkState.LOGIN: (1.0, 1.0, 1.0, 1.0),
    NetworkState.SLOW: (1.0, 1.0, 0.0, 0.0),
    NetworkState.LOSS: (1.0, 1.0, 1.0, 1.0),
    NetworkState.RECOVERY: (1.0, 1.0, 1.0, 1.0),
}


def _base_icon(state: NetworkState) -> Frame:
    """The resting glyph, used for the static safety icon."""
    return _paint_arcs(state, RESTING_LEVELS[state])


def loss_omission_count(loss_percent: float) -> int:
    """Quantize loss into the nearest truthful sixth of an animation cycle."""

    percent = _bounded_int(loss_percent, 0, 100, 30)
    return max(0, min(FRAME_COUNT, int(math.floor(percent * FRAME_COUNT / 100 + 0.5))))


def loss_omission_phases(loss_percent: float) -> frozenset[int]:
    """Spread missing packets deterministically instead of clumping them."""

    count = loss_omission_count(loss_percent)
    if count == 0:
        return frozenset()
    # Midpoint sampling yields evenly spaced, deterministic omissions.  At the
    # canonical 30% bucket this selects two of six frames (33%, within half a
    # frame of the displayed observation).
    phases = {
        min(FRAME_COUNT - 1, int((index + 0.5) * FRAME_COUNT / count))
        for index in range(count)
    }
    return frozenset(phases)


def arc_levels(
    state: str | Enum,
    phase: int,
    *,
    loss_percent: float = 30,
) -> tuple[float, float, float, float]:
    """Brightness of the dot and each arc (0 = dark tint, 1 = full) for a frame."""

    state = normalize_state(state)
    phase = int(phase) % FRAME_COUNT
    # Every beat differs from the last, or the GIF encoder merges frames.
    breath = (1.0, 0.8, 0.6, 0.4, 0.6, 0.8)[phase]
    if state is NetworkState.ROUTER:
        return (breath, 0.0, 0.0, 0.0)
    if state is NetworkState.WAN:
        # A search beam sweeping out past the local link, never reaching.
        sweep = (0.0, 0.15, 0.3, 0.45, 0.3, 0.15)
        return (1.0, 1.0, sweep[phase], sweep[(phase + 3) % FRAME_COUNT])
    if state in (NetworkState.DNS, NetworkState.LOGIN):
        return (1.0, 1.0, 1.0, breath)
    if state is NetworkState.LOSS:
        if phase in loss_omission_phases(loss_percent):
            return (1.0, 1.0, 0.0, 0.0)
        return (1.0, 1.0, 1.0, 1.0 if phase % 2 == 0 else 0.8)
    # SLOW climbs and RECOVERY fills, smoothly, arc by arc.
    reach = (phase + 1) / FRAME_COUNT * 4
    return tuple(max(0.0, min(1.0, reach - i)) for i in range(4))  # type: ignore[return-value]


def icon_frame(
    state: str | Enum,
    phase: int,
    *,
    loss_percent: float = 30,
) -> Frame:
    state = normalize_state(state)
    return _paint_arcs(state, arc_levels(state, phase, loss_percent=loss_percent))


def icon_frames(
    state: str | Enum,
    *,
    loss_percent: float = 30,
) -> list[Frame]:
    return [
        icon_frame(state, phase, loss_percent=loss_percent)
        for phase in range(FRAME_COUNT)
    ]


def combined_frame(
    state: str | Enum,
    phase: int,
    *,
    latency_ms: float = 180,
    loss_percent: float = 30,
) -> Frame:
    icon = icon_frame(state, phase, loss_percent=loss_percent)
    lane = lane_frame(state, latency_ms=latency_ms, loss_percent=loss_percent)
    frame = new_frame()
    for y in range(HEIGHT):
        frame[y][:ICON_WIDTH] = icon[y]
        frame[y][GUTTER_X] = BLACK
        frame[y][LANE_X0 : LANE_X0 + LANE_WIDTH] = lane[y]
    return frame


def _image(frame: Frame) -> Image.Image:
    width = len(frame[0])
    image = Image.new("RGB", (width, HEIGHT), BLACK)
    image.putdata([color for row in frame for color in row])
    return image


@lru_cache(maxsize=64)
def _cached_icon_gif(state: NetworkState, loss_percent: int, animated: bool) -> bytes:
    frames = icon_frames(state, loss_percent=loss_percent)
    if not animated:
        frames = [_base_icon(state)]
    images = [_image(frame) for frame in frames]
    stream = BytesIO()
    if animated:
        images[0].save(
            stream,
            format="GIF",
            save_all=True,
            append_images=images[1:],
            duration=[FRAME_MS] * FRAME_COUNT,
            loop=0,
            disposal=1,
            optimize=True,
        )
    else:
        images[0].save(stream, format="GIF", optimize=True)
    data = stream.getvalue()
    validate_icon_gif(data, animated=animated)
    return data


def build_icon_gif(
    state: str | Enum,
    *,
    loss_percent: float = 30,
    animated: bool = True,
) -> bytes:
    state = normalize_state(state)
    loss = _bounded_int(loss_percent, 0, 100, 30)
    return _cached_icon_gif(state, loss, bool(animated))


def validate_icon_gif(data: bytes, *, animated: bool = True) -> None:
    if not data.startswith((b"GIF87a", b"GIF89a")):
        raise ValueError("Network Sentinel icon is not a GIF")
    if len(data) > ICON_MAX_BYTES:
        raise ValueError(
            f"Network Sentinel icon is {len(data)} bytes; limit is {ICON_MAX_BYTES}"
        )
    try:
        with Image.open(BytesIO(data)) as image:
            if image.size != (ICON_WIDTH, ICON_HEIGHT):
                raise ValueError("Network Sentinel icon must be exactly 8x8")
            expected = FRAME_COUNT if animated else 1
            if image.n_frames != expected:
                raise ValueError(
                    f"Network Sentinel icon has {image.n_frames} frames; expected {expected}"
                )
            if "transparency" in image.info:
                raise ValueError("Network Sentinel icon must be fully opaque")
            for index in range(image.n_frames):
                image.seek(index)
                if animated and int(image.info.get("duration", 0)) != FRAME_MS:
                    raise ValueError("Network Sentinel icon cadence must be exactly 120ms")
    except OSError as error:
        raise ValueError("Network Sentinel icon is not readable") from error


def build_payload(
    state: str | Enum,
    *,
    latency_ms: float = 180,
    loss_percent: float = 30,
    animated: bool = True,
    duration_ms: int = PAGE_DURATION_MS,
    lifetime_ms: int = LIFETIME_MS,
) -> dict:
    """Build one compact pushed-app payload with stable diagnostic text."""

    state = normalize_state(state)
    icon = build_icon_gif(state, loss_percent=loss_percent, animated=animated)
    lane = lane_bytes(state, latency_ms=latency_ms, loss_percent=loss_percent)
    payload = {
        "icon": base64.b64encode(icon).decode("ascii"),
        "iconMode": "fixed",
        "iconOffsetX": 0,
        "draw": [
            [
                "bitmap",
                LANE_X0,
                0,
                LANE_WIDTH,
                LANE_HEIGHT,
                base64.b64encode(lane).decode("ascii"),
            ]
        ],
        "backgroundColor": "#000000",
        "durationMs": int(duration_ms),
        "lifetimeMs": int(lifetime_ms),
        "lifetimeExpiry": "remove",
    }
    size = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    if size >= PAYLOAD_MAX_BYTES:
        raise ValueError(f"Network Sentinel payload is {size} bytes; limit is under 2KB")
    return payload


def _desaturate(color: RGB, saturation: int) -> RGB:
    saturation = max(0, min(100, int(saturation)))
    red, green, blue = color
    gray = (77 * red + 150 * green + 29 * blue) >> 8
    return tuple(
        gray + math.trunc((channel - gray) * saturation / 100)
        for channel in color
    )  # type: ignore[return-value]


def panel_grade_color(
    color: RGB,
    *,
    gamma: float = PANEL_GAMMA,
    brightness: int = 45,
    saturation: int = PANEL_SATURATION,
) -> RGB:
    """Approximate the panel's saturation -> gamma -> brightness pipeline."""

    mixed = _desaturate(color, saturation)
    brightness = max(0, min(255, int(brightness)))
    graded: list[int] = []
    for channel in mixed:
        if channel == 0:
            gamma16 = 0
        else:
            fraction = (channel / 255.0) ** gamma if gamma > 0 else channel / 255.0
            gamma16 = max(1, min(65535, math.floor(fraction * 65535 + 0.5)))
        lit = gamma16 * brightness // 255
        graded.append(min(255, (lit + 128) // 257))
    return tuple(graded)  # type: ignore[return-value]
