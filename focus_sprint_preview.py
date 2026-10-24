"""Exact 32x8 art and compact AWTRIX payloads for Focus Sprint.

The production canvas is deliberately split into two independent regions::

    x=0..7   one familiar, stateful emblem; only this region may move
    x=8      an always-black safety gutter
    x=9..31  one static RGB888 timer lane

The lane never relies on AWTRIX text layout.  It contains four cycle pips,
centered proportional 3x5 ``MM:SS``, a blank separator row, and a truthful
23-pixel remaining-time rail.  The rail uses ceiling quantisation, so a live
deadline can never render as empty.  ``READY_NEXT`` replaces the timer with a
stable READY label and intentionally has no rail.

This module is a visual boundary, not a timer.  It accepts strings, foreign
Enums, mappings, or attribute-bearing snapshots and intentionally does not
import :mod:`focus_sprint`.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from io import BytesIO
import json
import math
from typing import Any, Iterable, Mapping, Sequence

from visual_system import LANE_X0 as GLANCE_LANE_X0, gamma_lerp, render_glance_lane
from PIL import Image


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
PAGE_DURATION_MS = 8_000
LIFETIME_MS = 20_000
PAYLOAD_MAX_BYTES = 2_048
ICON_MAX_BYTES = 850

FOCUS_FRAME_MS = 240
BREAK_FRAME_MS = 300
SPARKLE_FRAME_MS = 180
FOCUS_SECONDS = 25 * 60
SHORT_BREAK_SECONDS = 5 * 60
LONG_BREAK_SECONDS = 15 * 60

# Bright source RGB is intentional.  AWTRIX performs the gamma 2.2 conversion;
# pre-darkening these values would erase the quieter roles at brightness 45.
BLACK: RGB = (0, 0, 0)
PEARL: RGB = (255, 247, 232)
ICE: RGB = (222, 238, 255)
SLATE: RGB = (190, 207, 226)
TEAL_DIM: RGB = (56, 207, 198)
TEAL: RGB = (68, 246, 226)
MINT: RGB = (82, 255, 176)
STEAM_DIM: RGB = (104, 220, 192)
AMBER_DIM: RGB = (236, 143, 45)
AMBER: RGB = (255, 183, 67)
GOLD: RGB = (255, 220, 88)


class FocusState(str, Enum):
    """Names shared with the Focus Sprint domain without importing it."""

    IDLE = "idle"
    FOCUS_RUNNING = "focus_running"
    FOCUS_PAUSED = "focus_paused"
    FOCUS_DONE_CUE = "focus_done_cue"
    SHORT_BREAK_RUNNING = "short_break_running"
    LONG_BREAK_RUNNING = "long_break_running"
    BREAK_PAUSED = "break_paused"
    READY_NEXT = "ready_next"


class Motion(str, Enum):
    CALM = "calm"
    STATIC = "static"


STATE_ALIASES: dict[str, FocusState] = {
    "focus": FocusState.FOCUS_RUNNING,
    "work": FocusState.FOCUS_RUNNING,
    "working": FocusState.FOCUS_RUNNING,
    "running": FocusState.FOCUS_RUNNING,
    "sprint": FocusState.FOCUS_RUNNING,
    "paused": FocusState.FOCUS_PAUSED,
    "pause": FocusState.FOCUS_PAUSED,
    "focus_pause": FocusState.FOCUS_PAUSED,
    "paused_focus": FocusState.FOCUS_PAUSED,
    "recovered_pause": FocusState.FOCUS_PAUSED,
    "recovered_paused": FocusState.FOCUS_PAUSED,
    "recovery_paused": FocusState.FOCUS_PAUSED,
    "done": FocusState.FOCUS_DONE_CUE,
    "complete": FocusState.FOCUS_DONE_CUE,
    "completed": FocusState.FOCUS_DONE_CUE,
    "focus_done": FocusState.FOCUS_DONE_CUE,
    "short_break": FocusState.SHORT_BREAK_RUNNING,
    "break": FocusState.SHORT_BREAK_RUNNING,
    "rest": FocusState.SHORT_BREAK_RUNNING,
    "long_break": FocusState.LONG_BREAK_RUNNING,
    "long_rest": FocusState.LONG_BREAK_RUNNING,
    "paused_break": FocusState.BREAK_PAUSED,
    "break_pause": FocusState.BREAK_PAUSED,
    "ready": FocusState.READY_NEXT,
    "next": FocusState.READY_NEXT,
}

MOTION_ALIASES: dict[str, Motion] = {
    "animate": Motion.CALM,
    "animated": Motion.CALM,
    "normal": Motion.CALM,
    "safe": Motion.STATIC,
    "still": Motion.STATIC,
    "off": Motion.STATIC,
    "none": Motion.STATIC,
}


@dataclass(frozen=True)
class VisualSprint:
    """Normalised visual inputs.

    ``cycle`` is the domain's cumulative count of completed focus intervals.
    The lane derives its four-pip block with modulo-four semantics.
    """

    state: FocusState
    remaining_seconds: float
    total_seconds: float
    cycle: int
    motion: Motion

    @property
    def is_long_break(self) -> bool:
        return self.state is FocusState.LONG_BREAK_RUNNING or (
            self.state is FocusState.BREAK_PAUSED
            and self.total_seconds > SHORT_BREAK_SECONDS
        )


FONT_3X5: dict[str, tuple[str, ...]] = {
    "0": ("111", "101", "101", "101", "111"),
    # A bare single column: identical to "I" in this font, and thin
    # enough to read as a gap between its neighbours.
    "1": ("010", "110", "010", "010", "111"),
    "2": ("110", "001", "010", "100", "111"),
    "3": ("110", "001", "010", "001", "110"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "110", "001", "110"),
    "6": ("011", "100", "111", "101", "111"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "110"),
    ":": ("0", "1", "0", "1", "0"),
    "A": ("010", "101", "111", "101", "101"),
    "D": ("110", "101", "101", "101", "110"),
    "E": ("111", "100", "110", "100", "111"),
    "R": ("110", "101", "110", "101", "101"),
    "Y": ("101", "101", "010", "010", "010"),
}

PIP_X = (5, 9, 13, 17)

# A Pomodoro tomato (2026-10-03, to the Now Playing standard): a red fruit
# shaded at its lower right with a highlight, green stem and leaves, and a
# two-pixel bead sweeping a dial ring round its face like a kitchen timer.
# Every pixel stays lit; only colour moves, so the silhouette never blinks.
TOMATO_ART = (
    "...LL...",
    "..LGGL..",
    ".RRRRRR.",
    "RRRRRRRR",
    "RhRRRRRR",
    "RRRRRRRR",
    ".RRRRRs.",
    "..ssss..",
)
TOMATO_TONES: dict[str, RGB] = {
    "R": (255, 70, 70), "h": (255, 210, 200), "s": (200, 40, 50),
    "L": (60, 220, 110), "G": (130, 255, 150),
}
FOCUS_BEAD_PATH: tuple[tuple[int, int], ...] = (
    (3, 3), (4, 3), (5, 3), (5, 4), (5, 5), (4, 5), (3, 5), (3, 4),
)
FOCUS_INNER_TRACK = frozenset(FOCUS_BEAD_PATH)
FOCUS_STABLE = frozenset(
    (x, y) for y, row in enumerate(TOMATO_ART) for x, ch in enumerate(row) if ch in TOMATO_TONES
)
FOCUS_PAUSE_BARS = frozenset(
    {(3, y) for y in range(3, 6)} | {(5, y) for y in range(3, 6)}
)

# One continuous mug, now shaded: a cream rim, amber walls, a darker handle
# and foot, coffee inside.  Steam is a stable dim path with a bright head, so
# the only per-frame change is colour.  Long break adds the second path.
MUG_BODY = frozenset(
    {(x, 3) for x in range(1, 6)}
    | {(1, y) for y in range(4, 7)}
    | {(5, y) for y in range(4, 7)}
    | {(x, 7) for x in range(1, 6)}
    | {(6, 4), (7, 4), (7, 5), (7, 6), (6, 6)}
)
MUG_RIM = frozenset((x, 3) for x in range(1, 6))
MUG_FOOT = frozenset((x, 7) for x in range(1, 6))
MUG_HANDLE = frozenset({(6, 4), (7, 4), (7, 5), (7, 6), (6, 6)})
MUG_COFFEE = frozenset((x, 4) for x in range(2, 5))
MUG_TONES: dict[str, RGB] = {
    "rim": (255, 214, 140), "wall": (255, 150, 45), "dark": (220, 120, 36), "coffee": (150, 80, 30),
}
STEAM_TRACK: RGB = (170, 205, 230)
STEAM_HEAD: RGB = (236, 246, 255)
STEAM_PATH_A: tuple[tuple[int, int], ...] = (
    (2, 2), (2, 1), (3, 1), (3, 0), (3, 1), (2, 1)
)
STEAM_PATH_B: tuple[tuple[int, int], ...] = (
    (5, 2), (5, 1), (6, 1), (6, 0), (6, 1), (5, 1)
)
BREAK_PAUSE_BARS = frozenset(
    {(2, y) for y in range(4, 7)} | {(4, y) for y in range(4, 7)}
)

READY_ARROW = frozenset(
    {(x, 3) for x in range(0, 7)}
    | {(4, 1), (4, 2), (5, 2), (4, 4), (5, 4), (4, 5)}
)
READY_TIP = frozenset({(6, 3)})

DONE_CHECK = frozenset(
    {
        (1, 4), (2, 4), (2, 5), (3, 5), (3, 6),
        (4, 6), (4, 5), (5, 5), (5, 4), (6, 4), (6, 3),
    }
)
DONE_SPARKLE_STEPS: tuple[frozenset[tuple[int, int]], ...] = (
    frozenset(),
    frozenset({(1, 2), (4, 2), (6, 2)}),
    frozenset({(0, 2), (4, 1), (7, 2)}),
    frozenset({(4, 0)}),
    frozenset(),
)


def normalize_state(value: Any) -> FocusState:
    """Accept canonical state strings, friendly aliases, or foreign Enums."""

    if isinstance(value, FocusState):
        return value
    raw = value.value if isinstance(value, Enum) else value
    if not isinstance(raw, str):
        raise ValueError(f"unsupported Focus Sprint state: {value!r}")
    key = raw.strip().lower().replace("-", "_").replace(" ", "_")
    if key in STATE_ALIASES:
        return STATE_ALIASES[key]
    try:
        return FocusState(key)
    except ValueError as error:
        raise ValueError(f"unsupported Focus Sprint state: {value!r}") from error


def normalize_motion(value: Any) -> Motion:
    """Normalise the deliberately small ``calm | static`` motion vocabulary."""

    if isinstance(value, Motion):
        return value
    if isinstance(value, bool):
        return Motion.CALM if value else Motion.STATIC
    if value is None:
        return Motion.CALM
    raw = value.value if isinstance(value, Enum) else value
    if not isinstance(raw, str):
        raise ValueError(f"unsupported Focus Sprint motion: {value!r}")
    key = raw.strip().lower().replace("-", "_").replace(" ", "_")
    if key in MOTION_ALIASES:
        return MOTION_ALIASES[key]
    try:
        return Motion(key)
    except ValueError as error:
        raise ValueError(f"unsupported Focus Sprint motion: {value!r}") from error


def _read_field(source: Any, names: Sequence[str]) -> Any:
    if source is None or isinstance(source, (str, Enum)):
        return None
    if isinstance(source, Mapping):
        for name in names:
            if name in source:
                return source[name]
        return None
    for name in names:
        try:
            return getattr(source, name)
        except AttributeError:
            pass
    return None


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be a finite number") from error
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _default_total(state: FocusState) -> int:
    if state in (FocusState.SHORT_BREAK_RUNNING, FocusState.BREAK_PAUSED):
        return SHORT_BREAK_SECONDS
    if state is FocusState.LONG_BREAK_RUNNING:
        return LONG_BREAK_SECONDS
    return FOCUS_SECONDS


def _reason_requests_pause(reason: Any) -> bool:
    if reason is None:
        return False
    raw = reason.value if isinstance(reason, Enum) else reason
    if not isinstance(raw, str):
        return False
    # In the domain model the presence of pause_reason is itself authoritative;
    # values include user, quiet, recovered, and awaiting_start.
    return bool(raw.strip())


def coerce_sprint(
    source: Any = None,
    *,
    state: Any = None,
    remaining: Any = None,
    total: Any = None,
    cycle: Any = None,
    motion: Any = None,
    pause_reason: Any = None,
) -> VisualSprint:
    """Coerce generic visual inputs without depending on the timer module.

    Supported snapshot fields include ``phase``, ``remaining_seconds``,
    ``cycle_count``, and ``pause_reason``.  Explicit keyword arguments win.
    ``IDLE`` is accepted by the adapter but rejected by every renderer because
    an idle sprint must not create a visible AWTRIX page.
    """

    source_state = source if isinstance(source, (str, Enum)) else _read_field(
        source, ("phase", "state", "status")
    )
    chosen_state = state if state is not None else source_state
    if chosen_state is None:
        raise ValueError("Focus Sprint state is required")
    normal_state = normalize_state(chosen_state)

    visible = _read_field(source, ("visible", "is_visible"))
    if visible is False:
        normal_state = FocusState.IDLE

    source_reason = _read_field(
        source, ("pause_reason", "reason", "status_reason", "recovery_reason")
    )
    chosen_reason = pause_reason if pause_reason is not None else source_reason
    if _reason_requests_pause(chosen_reason):
        if normal_state in (
            FocusState.SHORT_BREAK_RUNNING,
            FocusState.LONG_BREAK_RUNNING,
            FocusState.BREAK_PAUSED,
        ):
            normal_state = FocusState.BREAK_PAUSED
        elif normal_state is not FocusState.IDLE:
            normal_state = FocusState.FOCUS_PAUSED

    source_total = _read_field(
        source,
        (
            "phase_total_seconds",
            "total_seconds",
            "total",
            "duration_seconds",
            "duration",
        ),
    )
    chosen_total = total if total is not None else source_total
    if chosen_total is None:
        chosen_total = _default_total(normal_state)
    total_seconds = _finite_number(chosen_total, "total")
    if total_seconds <= 0:
        raise ValueError("total must be positive")

    source_remaining = _read_field(
        source, ("remaining_seconds", "remaining", "seconds_left", "seconds")
    )
    chosen_remaining = remaining if remaining is not None else source_remaining
    if chosen_remaining is None:
        chosen_remaining = total_seconds
    remaining_seconds = _finite_number(chosen_remaining, "remaining")
    remaining_seconds = max(0.0, min(total_seconds, remaining_seconds))
    if normal_state is FocusState.FOCUS_DONE_CUE:
        remaining_seconds = 0.0

    source_cycle = _read_field(
        source, ("cycle_count", "cycle", "completed_cycles", "cycles_completed")
    )
    chosen_cycle = cycle if cycle is not None else source_cycle
    if chosen_cycle is None:
        chosen_cycle = 0
    cycle_number = _finite_number(chosen_cycle, "cycle")
    if cycle_number < 0:
        raise ValueError("cycle must not be negative")
    cycle_count = int(math.floor(cycle_number))

    source_motion = _read_field(source, ("motion", "animation", "motion_mode"))
    chosen_motion = motion if motion is not None else source_motion
    normal_motion = normalize_motion(chosen_motion)

    return VisualSprint(
        state=normal_state,
        remaining_seconds=remaining_seconds,
        total_seconds=total_seconds,
        cycle=cycle_count,
        motion=normal_motion,
    )


def _require_visible(state: FocusState) -> None:
    if state is FocusState.IDLE:
        raise ValueError("IDLE Focus Sprint has no visible page")


def new_frame(width: int = WIDTH) -> Frame:
    return [[BLACK for _ in range(width)] for _ in range(HEIGHT)]


def _put(frame: Frame, x: int, y: int, color: RGB) -> None:
    if 0 <= y < len(frame) and 0 <= x < len(frame[y]):
        frame[y][x] = color


def _paint(frame: Frame, points: Iterable[tuple[int, int]], color: RGB) -> None:
    for x, y in points:
        _put(frame, x, y, color)


def text_width(text: str) -> int:
    if not text:
        return 0
    try:
        widths = [len(FONT_3X5[character][0]) for character in text]
    except KeyError as error:
        raise ValueError(f"unsupported 3x5 character: {error.args[0]!r}") from error
    return sum(widths) + len(widths) - 1


def draw_text(frame: Frame, text: str, x0: int, y0: int, color: RGB) -> None:
    x = x0
    for index, character in enumerate(text):
        glyph = FONT_3X5[character]
        for glyph_y, row in enumerate(glyph):
            for glyph_x, bit in enumerate(row):
                if bit == "1":
                    _put(frame, x + glyph_x, y0 + glyph_y, color)
        x += len(glyph[0]) + (1 if index < len(text) - 1 else 0)


def format_countdown(remaining_seconds: Any) -> str:
    """Return clamped ``MM:SS``, ceiling positive fractions truthfully."""

    remaining = _finite_number(remaining_seconds, "remaining")
    display_seconds = 0 if remaining <= 0 else int(math.ceil(remaining))
    display_seconds = min(99 * 60 + 59, display_seconds)
    minutes, seconds = divmod(display_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


def remaining_rail_pixels(remaining: Any, total: Any) -> int:
    """Quantise remaining time to 0..23 pixels without premature zero."""

    remaining_value = _finite_number(remaining, "remaining")
    if remaining_value <= 0:
        return 0
    total_value = _finite_number(total, "total")
    if total_value <= 0:
        raise ValueError("total must be positive while time remains")
    ratio = min(1.0, remaining_value / total_value)
    return max(1, min(LANE_WIDTH, int(math.ceil(ratio * LANE_WIDTH))))


def _pip_colors(visual: VisualSprint) -> tuple[RGB, RGB, RGB, RGB]:
    if visual.is_long_break:
        return (MINT, MINT, MINT, MINT)

    completed = visual.cycle % 4
    colors: list[RGB] = [SLATE] * 4
    for index in range(completed):
        colors[index] = MINT

    if visual.state in (FocusState.FOCUS_RUNNING, FocusState.FOCUS_PAUSED):
        colors[completed] = GOLD
    elif (
        visual.cycle > 0
        and completed == 0
        and visual.state in (
            FocusState.FOCUS_DONE_CUE,
            FocusState.SHORT_BREAK_RUNNING,
            FocusState.BREAK_PAUSED,
            FocusState.READY_NEXT,
        )
    ):
        colors = [MINT] * 4
    return tuple(colors)  # type: ignore[return-value]


def _timer_color(state: FocusState) -> RGB:
    if state in (FocusState.FOCUS_PAUSED, FocusState.BREAK_PAUSED):
        return GOLD
    if state in (FocusState.SHORT_BREAK_RUNNING, FocusState.LONG_BREAK_RUNNING):
        return ICE
    if state is FocusState.FOCUS_DONE_CUE:
        return MINT
    return PEARL


def _rail_color(state: FocusState) -> RGB:
    if state in (FocusState.FOCUS_PAUSED, FocusState.BREAK_PAUSED):
        return GOLD
    if state in (FocusState.SHORT_BREAK_RUNNING, FocusState.LONG_BREAK_RUNNING):
        return AMBER
    return TEAL


def _lane_for_visual(visual: VisualSprint) -> Frame:
    """Calm glance: the countdown in white, time remaining as the meter.

    The cycle pips that used to sit on row 0 were a second, unlabeled meter;
    the panel-wide rule is one meter on row 7.  Focus reads as info, a break
    as good, a pause as fair.
    """
    _require_visible(visual.state)
    if visual.state is FocusState.READY_NEXT:
        lane = render_glance_lane("READY")
    else:
        if visual.state in (FocusState.FOCUS_PAUSED, FocusState.BREAK_PAUSED):
            level = "fair"
        elif visual.state in (FocusState.SHORT_BREAK_RUNNING, FocusState.LONG_BREAK_RUNNING):
            level = "good"
        else:
            level = "info"
        pixels = remaining_rail_pixels(visual.remaining_seconds, visual.total_seconds)
        lane = render_glance_lane(format_countdown(visual.remaining_seconds),
                                  meter=pixels / LANE_WIDTH if pixels else 0.0, level=level)
    return [row[GLANCE_LANE_X0:GLANCE_LANE_X0 + LANE_WIDTH] for row in lane]


def lane_frame(
    source: Any = None,
    *,
    state: Any = None,
    remaining: Any = None,
    total: Any = None,
    cycle: Any = None,
) -> Frame:
    """Return the exact static 23x8 timer lane."""

    visual = coerce_sprint(
        source,
        state=state,
        remaining=remaining,
        total=total,
        cycle=cycle,
        motion=Motion.STATIC,
    )
    return _lane_for_visual(visual)


def lane_bytes(
    source: Any = None,
    *,
    state: Any = None,
    remaining: Any = None,
    total: Any = None,
    cycle: Any = None,
) -> bytes:
    frame = lane_frame(
        source,
        state=state,
        remaining=remaining,
        total=total,
        cycle=cycle,
    )
    return bytes(channel for row in frame for color in row for channel in color)


def _focus_icon(phase: int | None, *, paused: bool) -> Frame:
    frame = new_frame(ICON_WIDTH)
    for y, row in enumerate(TOMATO_ART):
        for x, ch in enumerate(row):
            if ch in TOMATO_TONES:
                _put(frame, x, y, TOMATO_TONES[ch])
    if paused:
        _paint(frame, FOCUS_PAUSE_BARS, GOLD)
    elif phase is not None:
        index = int(phase) % len(FOCUS_BEAD_PATH)
        tail = FOCUS_BEAD_PATH[index]
        head = FOCUS_BEAD_PATH[(index + 1) % len(FOCUS_BEAD_PATH)]
        _put(frame, *tail, gamma_lerp(frame[tail[1]][tail[0]], PEARL, 0.5))
        _put(frame, *head, PEARL)
    return frame


def _mug_icon(
    phase: int | None,
    *,
    paused: bool,
    long_break: bool,
) -> Frame:
    frame = new_frame(ICON_WIDTH)
    _paint(frame, MUG_BODY, MUG_TONES["wall"])
    _paint(frame, MUG_RIM, MUG_TONES["rim"])
    _paint(frame, MUG_FOOT | MUG_HANDLE, MUG_TONES["dark"])
    _paint(frame, MUG_COFFEE, MUG_TONES["coffee"])
    paths = (STEAM_PATH_A, STEAM_PATH_B) if long_break else (STEAM_PATH_A,)
    for path in paths:
        _paint(frame, path, STEAM_TRACK)
    if paused:
        _paint(frame, BREAK_PAUSE_BARS, PEARL)
    elif phase is not None:
        index = int(phase) % len(STEAM_PATH_A)
        _put(frame, *STEAM_PATH_A[index], STEAM_HEAD)
        if long_break:
            second = (index + len(STEAM_PATH_B) // 2) % len(STEAM_PATH_B)
            _put(frame, *STEAM_PATH_B[second], STEAM_HEAD)
    return frame


def _done_icon(phase: int | None) -> Frame:
    frame = new_frame(ICON_WIDTH)
    _paint(frame, DONE_CHECK, MINT)
    if phase is not None:
        step = min(max(0, int(phase)), len(DONE_SPARKLE_STEPS) - 1)
        _paint(frame, DONE_SPARKLE_STEPS[step], PEARL)
    return frame


def _ready_icon() -> Frame:
    frame = new_frame(ICON_WIDTH)
    _paint(frame, READY_ARROW, MINT)
    _paint(frame, READY_TIP, PEARL)
    return frame


def _effective_motion(state: FocusState, motion: Motion) -> Motion:
    if state in (
        FocusState.FOCUS_PAUSED,
        FocusState.BREAK_PAUSED,
        FocusState.READY_NEXT,
    ):
        return Motion.STATIC
    return motion


def icon_frame(
    state: Any,
    phase: int = 0,
    *,
    motion: Any = Motion.CALM,
    long_break: bool = False,
) -> Frame:
    """Return one exact 8x8 source frame for a visible state."""

    normal_state = normalize_state(state)
    _require_visible(normal_state)
    normal_motion = _effective_motion(normal_state, normalize_motion(motion))
    moving_phase: int | None = int(phase) if normal_motion is Motion.CALM else None

    if normal_state is FocusState.FOCUS_RUNNING:
        return _focus_icon(moving_phase, paused=False)
    if normal_state is FocusState.FOCUS_PAUSED:
        return _focus_icon(None, paused=True)
    if normal_state is FocusState.FOCUS_DONE_CUE:
        return _done_icon(moving_phase)
    if normal_state in (
        FocusState.SHORT_BREAK_RUNNING,
        FocusState.LONG_BREAK_RUNNING,
        FocusState.BREAK_PAUSED,
    ):
        is_long = normal_state is FocusState.LONG_BREAK_RUNNING or bool(long_break)
        return _mug_icon(
            moving_phase,
            paused=normal_state is FocusState.BREAK_PAUSED,
            long_break=is_long,
        )
    if normal_state is FocusState.READY_NEXT:
        return _ready_icon()
    raise AssertionError(f"unhandled Focus Sprint state {normal_state!r}")


def motion_points(
    state: Any,
    phase: int,
    *,
    long_break: bool = False,
) -> frozenset[tuple[int, int]]:
    """Expose the semantic moving highlight for exact cadence QA."""

    normal_state = normalize_state(state)
    if normal_state is FocusState.FOCUS_RUNNING:
        index = int(phase) % len(FOCUS_BEAD_PATH)
        return frozenset(
            {FOCUS_BEAD_PATH[index], FOCUS_BEAD_PATH[(index + 1) % len(FOCUS_BEAD_PATH)]}
        )
    if normal_state in (
        FocusState.SHORT_BREAK_RUNNING,
        FocusState.LONG_BREAK_RUNNING,
    ):
        index = int(phase) % len(STEAM_PATH_A)
        points = {STEAM_PATH_A[index]}
        if normal_state is FocusState.LONG_BREAK_RUNNING or long_break:
            points.add(STEAM_PATH_B[(index + len(STEAM_PATH_B) // 2) % len(STEAM_PATH_B)])
        return frozenset(points)
    if normal_state is FocusState.FOCUS_DONE_CUE:
        index = min(max(0, int(phase)), len(DONE_SPARKLE_STEPS) - 1)
        return DONE_SPARKLE_STEPS[index]
    return frozenset()


def stable_icon_points(
    state: Any,
    *,
    long_break: bool = False,
) -> frozenset[tuple[int, int]]:
    """Return pixels whose semantic silhouette may never blink off."""

    normal_state = normalize_state(state)
    _require_visible(normal_state)
    if normal_state in (FocusState.FOCUS_RUNNING, FocusState.FOCUS_PAUSED):
        return FOCUS_STABLE
    if normal_state in (
        FocusState.SHORT_BREAK_RUNNING,
        FocusState.LONG_BREAK_RUNNING,
        FocusState.BREAK_PAUSED,
    ):
        paths = frozenset(STEAM_PATH_A)
        if normal_state is FocusState.LONG_BREAK_RUNNING or long_break:
            paths |= frozenset(STEAM_PATH_B)
        return MUG_BODY | paths
    if normal_state is FocusState.FOCUS_DONE_CUE:
        return DONE_CHECK
    return READY_ARROW | READY_TIP


def _icon_sequence(
    state: FocusState,
    motion: Motion,
    *,
    long_break: bool,
) -> tuple[list[Frame], tuple[int, ...], bool]:
    effective = _effective_motion(state, motion)
    if effective is Motion.STATIC:
        return [icon_frame(state, motion=Motion.STATIC, long_break=long_break)], (0,), False
    if state is FocusState.FOCUS_RUNNING:
        count = len(FOCUS_BEAD_PATH)
        duration = FOCUS_FRAME_MS
        loop = True
    elif state in (FocusState.SHORT_BREAK_RUNNING, FocusState.LONG_BREAK_RUNNING):
        count = len(STEAM_PATH_A)
        duration = BREAK_FRAME_MS
        loop = True
    elif state is FocusState.FOCUS_DONE_CUE:
        count = len(DONE_SPARKLE_STEPS)
        duration = SPARKLE_FRAME_MS
        loop = False
    else:
        count = 1
        duration = 0
        loop = False
    frames = [
        icon_frame(state, phase, motion=effective, long_break=long_break)
        for phase in range(count)
    ]
    return frames, (duration,) * count, loop


def icon_frames(
    state: Any,
    *,
    motion: Any = Motion.CALM,
    animated: bool | None = None,
    long_break: bool = False,
) -> list[Frame]:
    """Return independent exact-size source frames for visual review."""

    normal_state = normalize_state(state)
    _require_visible(normal_state)
    normal_motion = normalize_motion(motion)
    if animated is False:
        normal_motion = Motion.STATIC
    frames, _, _ = _icon_sequence(normal_state, normal_motion, long_break=long_break)
    return [[list(row) for row in frame] for frame in frames]


def _combined_for_visual(visual: VisualSprint, phase: int) -> Frame:
    _require_visible(visual.state)
    frame = new_frame()
    icon = icon_frame(
        visual.state,
        phase,
        motion=visual.motion,
        long_break=visual.is_long_break,
    )
    lane = _lane_for_visual(visual)
    for y in range(HEIGHT):
        frame[y][:ICON_WIDTH] = icon[y]
        frame[y][GUTTER_X] = BLACK
        frame[y][LANE_X0:] = lane[y]
    return frame


def combined_frame(
    source: Any = None,
    phase: int = 0,
    *,
    state: Any = None,
    remaining: Any = None,
    total: Any = None,
    cycle: Any = None,
    motion: Any = None,
) -> Frame:
    visual = coerce_sprint(
        source,
        state=state,
        remaining=remaining,
        total=total,
        cycle=cycle,
        motion=motion,
    )
    return _combined_for_visual(visual, phase)


def combined_frames(
    source: Any = None,
    *,
    state: Any = None,
    remaining: Any = None,
    total: Any = None,
    cycle: Any = None,
    motion: Any = None,
    animated: bool | None = None,
) -> list[Frame]:
    visual = coerce_sprint(
        source,
        state=state,
        remaining=remaining,
        total=total,
        cycle=cycle,
        motion=motion,
    )
    if animated is False:
        visual = VisualSprint(
            visual.state,
            visual.remaining_seconds,
            visual.total_seconds,
            visual.cycle,
            Motion.STATIC,
        )
    icons, _, _ = _icon_sequence(
        visual.state,
        visual.motion,
        long_break=visual.is_long_break,
    )
    return [_combined_for_visual(visual, phase) for phase in range(len(icons))]


def validate_frames(frames: Iterable[Frame]) -> None:
    count = 0
    for frame in frames:
        if len(frame) != HEIGHT or any(len(row) != WIDTH for row in frame):
            raise ValueError("every Focus Sprint frame must be exactly 32x8")
        if any(frame[y][GUTTER_X] != BLACK for y in range(HEIGHT)):
            raise ValueError("Focus Sprint gutter x=8 must remain true black")
        if any(frame[6][x] != BLACK for x in range(LANE_X0, WIDTH)):
            raise ValueError("Focus Sprint lane row 6 must remain true black")
        for row in frame:
            for color in row:
                if len(color) != 3 or any(
                    not isinstance(channel, int) or not 0 <= channel <= 255
                    for channel in color
                ):
                    raise ValueError("Focus Sprint frames must contain RGB888 pixels")
        count += 1
    if count == 0:
        raise ValueError("Focus Sprint animation needs at least one frame")


def _image(frame: Frame) -> Image.Image:
    width = len(frame[0])
    image = Image.new("RGB", (width, HEIGHT), BLACK)
    image.putdata([color for row in frame for color in row])
    return image


@lru_cache(maxsize=32)
def _cached_icon_gif(
    state: FocusState,
    motion: Motion,
    long_break: bool,
) -> bytes:
    frames, durations, loops = _icon_sequence(state, motion, long_break=long_break)
    images = [_image(frame) for frame in frames]
    stream = BytesIO()
    if len(images) == 1:
        images[0].save(stream, format="GIF", optimize=True)
    else:
        options: dict[str, Any] = {
            "format": "GIF",
            "save_all": True,
            "append_images": images[1:],
            "duration": list(durations),
            "disposal": 1,
            "optimize": True,
        }
        if loops:
            options["loop"] = 0
        images[0].save(stream, **options)
    data = stream.getvalue()
    validate_icon_gif(
        data,
        expected_frames=len(images),
        durations_ms=durations,
        should_loop=loops,
    )
    return data


def build_icon_gif(
    state: Any,
    *,
    motion: Any = Motion.CALM,
    animated: bool | None = None,
    long_break: bool = False,
) -> bytes:
    """Build an opaque 8x8 GIF; ``animated=False`` is the safety variant."""

    normal_state = normalize_state(state)
    _require_visible(normal_state)
    normal_motion = normalize_motion(motion)
    if animated is False:
        normal_motion = Motion.STATIC
    return _cached_icon_gif(normal_state, normal_motion, bool(long_break))


def validate_icon_gif(
    data: bytes,
    *,
    expected_frames: int | None = None,
    durations_ms: Sequence[int] | None = None,
    should_loop: bool | None = None,
) -> None:
    if not data.startswith((b"GIF87a", b"GIF89a")):
        raise ValueError("Focus Sprint icon is not a GIF")
    if len(data) > ICON_MAX_BYTES:
        raise ValueError(
            f"Focus Sprint icon is {len(data)} bytes; limit is {ICON_MAX_BYTES}"
        )
    try:
        with Image.open(BytesIO(data)) as image:
            if image.size != (ICON_WIDTH, ICON_HEIGHT):
                raise ValueError("Focus Sprint icon must be exactly 8x8")
            if expected_frames is not None and image.n_frames != expected_frames:
                raise ValueError(
                    f"Focus Sprint icon has {image.n_frames} frames; expected {expected_frames}"
                )
            if "transparency" in image.info:
                raise ValueError("Focus Sprint icon must be fully opaque")
            loop = image.info.get("loop")
            if should_loop is True and loop != 0:
                raise ValueError("calm Focus Sprint motion must loop")
            if should_loop is False and loop is not None:
                raise ValueError("one-shot/static Focus Sprint art must not loop")
            for index in range(image.n_frames):
                image.seek(index)
                if "transparency" in image.info:
                    raise ValueError("Focus Sprint GIF frame must be fully opaque")
                if durations_ms is not None and durations_ms[index] > 0:
                    if int(image.info.get("duration", 0)) != int(durations_ms[index]):
                        raise ValueError("Focus Sprint icon cadence changed during encoding")
                rgba = image.convert("RGBA")
                if any(alpha != 255 for *_, alpha in rgba.getdata()):
                    raise ValueError("Focus Sprint GIF contains transparent pixels")
    except OSError as error:
        raise ValueError("Focus Sprint icon is not readable") from error


def serialized_size(payload: Mapping[str, Any]) -> int:
    """Measure the conservative default JSON representation used by requests."""

    return len(json.dumps(payload, ensure_ascii=True).encode("utf-8"))


def build_payload(
    source: Any = None,
    *,
    state: Any = None,
    remaining: Any = None,
    total: Any = None,
    cycle: Any = None,
    motion: Any = None,
    pause_reason: Any = None,
    animated: bool | None = None,
    duration_ms: int = PAGE_DURATION_MS,
    lifetime_ms: int = LIFETIME_MS,
) -> dict[str, Any]:
    """Build one bounded native-layout pushed-app payload.

    Animated and safety payloads share the exact same lane and metadata; only
    the 8x8 icon's local motion changes.
    """

    if int(duration_ms) <= 0 or int(lifetime_ms) <= 0:
        raise ValueError("duration_ms and lifetime_ms must be positive")
    visual = coerce_sprint(
        source,
        state=state,
        remaining=remaining,
        total=total,
        cycle=cycle,
        motion=motion,
        pause_reason=pause_reason,
    )
    _require_visible(visual.state)
    icon_motion = visual.motion if animated is not False else Motion.STATIC
    icon = build_icon_gif(
        visual.state,
        motion=icon_motion,
        long_break=visual.is_long_break,
    )
    lane = _lane_for_visual(visual)
    lane_data = bytes(channel for row in lane for color in row for channel in color)
    payload: dict[str, Any] = {
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
                base64.b64encode(lane_data).decode("ascii"),
            ]
        ],
        "backgroundColor": "#000000",
        "durationMs": int(duration_ms),
        "lifetimeMs": int(lifetime_ms),
        "lifetimeExpiry": "remove",
    }
    size = serialized_size(payload)
    if size >= PAYLOAD_MAX_BYTES:
        raise ValueError(f"Focus Sprint payload is {size} bytes; limit is under 2KB")
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
    """Approximate AWTRIX saturation -> 16-bit gamma -> brightness grading."""

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


def panel_grade_frame(
    frame: Frame,
    *,
    gamma: float = PANEL_GAMMA,
    brightness: int = 45,
    saturation: int = PANEL_SATURATION,
) -> Frame:
    return [
        [
            panel_grade_color(
                color,
                gamma=gamma,
                brightness=brightness,
                saturation=saturation,
            )
            for color in row
        ]
        for row in frame
    ]


__all__ = [
    "AMBER",
    "AMBER_DIM",
    "BLACK",
    "BREAK_FRAME_MS",
    "DONE_CHECK",
    "DONE_SPARKLE_STEPS",
    "FOCUS_BEAD_PATH",
    "FOCUS_FRAME_MS",
    "FOCUS_STABLE",
    "FONT_3X5",
    "FocusState",
    "Frame",
    "GUTTER_X",
    "HEIGHT",
    "ICON_HEIGHT",
    "ICON_MAX_BYTES",
    "ICON_WIDTH",
    "LANE_WIDTH",
    "LANE_X0",
    "LIFETIME_MS",
    "LONG_BREAK_SECONDS",
    "MINT",
    "MUG_BODY",
    "Motion",
    "PAGE_DURATION_MS",
    "PANEL_GAMMA",
    "PANEL_SATURATION",
    "PAYLOAD_MAX_BYTES",
    "PEARL",
    "PIP_X",
    "SHORT_BREAK_SECONDS",
    "SPARKLE_FRAME_MS",
    "STEAM_PATH_A",
    "STEAM_PATH_B",
    "TEAL",
    "TEAL_DIM",
    "VisualSprint",
    "WIDTH",
    "build_icon_gif",
    "build_payload",
    "coerce_sprint",
    "combined_frame",
    "combined_frames",
    "draw_text",
    "format_countdown",
    "icon_frame",
    "icon_frames",
    "lane_bytes",
    "lane_frame",
    "motion_points",
    "normalize_motion",
    "normalize_state",
    "panel_grade_color",
    "panel_grade_frame",
    "remaining_rail_pixels",
    "serialized_size",
    "stable_icon_points",
    "text_width",
    "validate_frames",
    "validate_icon_gif",
]
