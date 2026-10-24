"""Centralized Gamma 2.2 Token System and Visual Design Core for AWTRIX.

Harmonizes color palettes, perceptual gamma 2.2 interpolation, low-brightness
legibility safeguards (min dominant channel >= 174 for 1-bit nightkeeper modes),
proportional 3x5 typography, high-contrast silhouettes, and payload wire validators.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import json
import math
from typing import Any, Callable, Iterable, Sequence

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
FRAME_BEAT_MS = 120
LOW_BRIGHTNESS_DOMINANT_MIN = 174
MAX_PAYLOAD_BYTES = 2_048
MAX_ICON_BYTES = 1_024

# ---------------------------------------------------------------- Tokens
# Standardized palette across all apps, tuned for WS2812B RGB LEDs behind a diffuser
PEARL: RGB = (255, 247, 232)
ICE: RGB = (196, 239, 255)
MINT: RGB = (72, 245, 160)
GOLD: RGB = (255, 213, 78)
AMBER: RGB = (255, 145, 45)
CORAL: RGB = (255, 76, 91)
RED_HOT: RGB = (255, 25, 35)
VIOLET: RGB = (194, 97, 255)
EMERALD: RGB = (0, 230, 118)
TRACK: RGB = (32, 40, 54)
DIM_GRAY: RGB = (45, 52, 64)
BLACK: RGB = (0, 0, 0)

SEMANTIC_TOKENS: dict[str, RGB] = {
    "pearl": PEARL,
    "ice": ICE,
    "mint": MINT,
    "gold": GOLD,
    "amber": AMBER,
    "coral": CORAL,
    "red": RED_HOT,
    "violet": VIOLET,
    "emerald": EMERALD,
    "track": TRACK,
    "dim_gray": DIM_GRAY,
    "black": BLACK,
}


# ---------------------------------------------------------------- Gamma & Grading

def gamma_correct(color: RGB, gamma: float = PANEL_GAMMA) -> RGB:
    """Apply perceptual gamma correction to an 8-bit RGB color."""
    inv_gamma = 1.0 / gamma
    return tuple(
        max(0, min(255, int(round(255.0 * ((channel / 255.0) ** inv_gamma)))))
        for channel in color
    )  # type: ignore[return-value]


def gamma_lerp(c1: RGB, c2: RGB, t: float, gamma: float = PANEL_GAMMA) -> RGB:
    """Perceptually uniform color blend in linear space."""
    t_clamped = max(0.0, min(1.0, float(t)))
    r1, g1, b1 = [(c / 255.0) ** gamma for c in c1]
    r2, g2, b2 = [(c / 255.0) ** gamma for c in c2]
    r = (r1 + (r2 - r1) * t_clamped) ** (1.0 / gamma)
    g = (g1 + (g2 - g1) * t_clamped) ** (1.0 / gamma)
    b = (b1 + (b2 - b1) * t_clamped) ** (1.0 / gamma)
    return (
        max(0, min(255, int(round(r * 255.0)))),
        max(0, min(255, int(round(g * 255.0)))),
        max(0, min(255, int(round(b * 255.0)))),
    )


def is_safe_at_low_brightness(color: RGB, min_dominant: int = LOW_BRIGHTNESS_DOMINANT_MIN) -> bool:
    """Whether the color has at least one channel meeting the 1-bit Nightkeeper threshold."""
    if color == BLACK:
        return True
    return max(color) >= min_dominant


def ensure_low_brightness_legibility(
    color: RGB,
    min_dominant: int = LOW_BRIGHTNESS_DOMINANT_MIN,
) -> RGB:
    """Scale color up proportionally if it falls below the panel's 1-bit threshold."""
    if color == BLACK:
        return BLACK
    dominant = max(color)
    if dominant >= min_dominant:
        return color
    scale = min_dominant / float(dominant)
    return tuple(min(255, int(round(channel * scale))) for channel in color)  # type: ignore[return-value]


# ---------------------------------------------------------------- Proportional 3x5 Typography

FONT_3X5: dict[str, tuple[str, ...]] = {
    " ": ("0", "0", "0", "0", "0"),
    "-": ("000", "000", "111", "000", "000"),
    ".": ("0", "0", "0", "0", "1"),
    ":": ("0", "1", "0", "1", "0"),
    "/": ("001", "001", "010", "100", "100"),
    "%": ("101", "001", "010", "100", "101"),
    # Degree sign: a 2x2 block on the cap line, so "96°" needs no second colour.
    "°": ("11", "11", "00", "00", "00"),
    "#": ("101", "111", "101", "111", "101"),
    "?": ("110", "001", "010", "000", "010"),
    "+": ("000", "010", "111", "010", "000"),
    "!": ("1", "1", "1", "0", "1"),
    "'": ("1", "1", "0", "0", "0"),
    ",": ("00", "00", "00", "01", "10"),
    "&": ("010", "101", "010", "101", "011"),
    "(": ("01", "10", "10", "10", "01"),
    ")": ("10", "01", "01", "01", "10"),
    "_": ("000", "000", "000", "000", "111"),
    "=": ("000", "111", "000", "111", "000"),
    "0": ("111", "101", "101", "101", "111"),
    # A 1px "1" is indistinguishable from "I" and reads as a stray rule between
    # 3px digits -- "01:01" rendered as "0|:0|".  Both are 3px and distinct now.
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
    "F": ("111", "100", "110", "100", "100"),
    "G": ("111", "100", "101", "101", "111"),
    "H": ("101", "101", "111", "101", "101"),
    # Serifed and 3px, matching github_sentinel and meeting_sentinel.  A 1px
    # "I" reads as a stray rule rather than a letter -- "ON AIR" rendered with
    # a bare column between the A and the R.
    "I": ("111", "010", "010", "010", "111"),
    "J": ("001", "001", "001", "101", "110"),
    "K": ("101", "110", "100", "110", "101"),
    "L": ("100", "100", "100", "100", "111"),
    # "M" is 5px.  At 3px it was one pixel from "H" AND one pixel from "N".
    # That mattered: github_sentinel rotates several repos through a single
    # app slot and distinguishes them by one initial, so "home-assistant" and
    # "miku-jarvis" rendered lanes differing by a single pixel at (11, 2).
    # Widening takes the font from 13 confusable pairs to 11 and adds none.
    "M": ("10001", "11011", "10101", "10001", "10001"),
    "N": ("101", "111", "111", "111", "101"),
    # Rounded, so the letter O is not byte-identical to the digit 0.
    # It was, in every font in this repo.  Rounding the letter rather
    # than altering the digit also dissolves O~8, O~G and O~U, which
    # were each a single pixel apart, and costs no width.
    "O": ("010", "101", "101", "101", "010"),
    "P": ("111", "101", "111", "100", "100"),
    "Q": ("111", "101", "101", "111", "001"),
    "R": ("110", "101", "110", "101", "101"),
    "S": ("111", "100", "111", "001", "111"),
    "T": ("111", "010", "010", "010", "010"),
    "U": ("101", "101", "101", "101", "111"),
    "V": ("101", "101", "101", "101", "010"),
    # "W" is 5px wide.  At 3px it differed from "H" by a single pixel and read
    # as an H -- which is exactly the WORK/HOME distinction the commute lane's
    # destination initial exists to carry.  The font is proportional, so the
    # extra two columns cost nothing but the width they use.
    "W": ("10001", "10001", "10001", "10101", "01010"),
    "X": ("101", "101", "010", "101", "101"),
    "Y": ("101", "101", "010", "010", "010"),
    "Z": ("111", "001", "010", "100", "111"),
}


def measure_text(text: str) -> int:
    """Return total width in pixels of rendered text."""
    glyphs = [FONT_3X5.get(c.upper(), FONT_3X5[" "]) for c in text]
    return sum(len(g[0]) for g in glyphs) + max(0, len(glyphs) - 1)


def new_frame(fill: RGB = BLACK) -> Frame:
    return [[fill for _ in range(WIDTH)] for _ in range(HEIGHT)]


def put_pixel(frame: Frame, x: int, y: int, color: RGB) -> None:
    if 0 <= x < WIDTH and 0 <= y < HEIGHT:
        frame[y][x] = color


def draw_text(frame: Frame, text: str, x0: int, y0: int, color: RGB) -> int:
    """Render text with proportional spacing and return rendered pixel width."""
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


# ---------------------------------------------------------------- Micro 3x3 Typography (Split Lane)

FONT_3X3: dict[str, tuple[str, ...]] = {
    "0": ("111", "101", "111"),
    "1": ("010", "110", "010"),
    "2": ("111", "010", "111"),
    "3": ("110", "011", "110"),
    "4": ("101", "111", "001"),
    "5": ("111", "110", "011"),
    "6": ("011", "110", "111"),
    "7": ("111", "001", "010"),
    "8": ("111", "111", "111"),
    "9": ("111", "111", "001"),
    "A": ("010", "111", "101"),
    "B": ("110", "111", "110"),
    "C": ("111", "100", "111"),
    "D": ("110", "101", "110"),
    "E": ("111", "110", "111"),
    "F": ("111", "100", "100"),
    "G": ("011", "101", "111"),
    "H": ("101", "111", "101"),
    "I": ("010", "010", "010"),
    "J": ("001", "001", "110"),
    "K": ("101", "110", "101"),
    "L": ("100", "100", "111"),
    "M": ("111", "101", "101"),
    "N": ("110", "101", "101"),
    "O": ("010", "101", "010"),
    "P": ("111", "111", "100"),
    "Q": ("010", "101", "011"),
    "R": ("110", "111", "101"),
    "S": ("011", "010", "110"),
    "T": ("111", "010", "010"),
    "U": ("101", "101", "111"),
    "V": ("101", "101", "010"),
    "W": ("101", "111", "010"),
    "X": ("101", "010", "101"),
    "Y": ("101", "010", "010"),
    "Z": ("110", "010", "011"),
    " ": ("000", "000", "000"),
    "-": ("000", "111", "000"),
    "+": ("010", "111", "010"),
    ".": ("000", "000", "010"),
    ":": ("010", "000", "010"),
    "%": ("101", "010", "001"),
    "/": ("001", "010", "100"),
    "?": ("110", "010", "010"),
    "°": ("110", "110", "000"),
}


def measure_text_3x3(text: str) -> int:
    """Return total width in pixels of rendered 3x3 text."""
    glyphs = [FONT_3X3.get(c.upper(), FONT_3X3[" "]) for c in text]
    return sum(len(g[0]) for g in glyphs) + max(0, len(glyphs) - 1)


def draw_text_3x3(frame: Frame, text: str, x0: int, y0: int, color: RGB) -> int:
    """Render 3x3 text into frame and return rendered width in pixels."""
    x = x0
    for idx, char in enumerate(text):
        glyph = FONT_3X3.get(char.upper(), FONT_3X3[" "])
        for gy, row in enumerate(glyph):
            for gx, bit in enumerate(row):
                if bit == "1":
                    put_pixel(frame, x + gx, y0 + gy, color)
        x += len(glyph[0])
        if idx < len(text) - 1:
            x += 1
    return x - x0


# ---------------------------------------------------------------- Weather Silhouette Redesigns

# High-contrast, negative-space silhouette definitions for 8x8 weather icons
SUN_SILHOUETTE = (
    "...SS...",
    ".S....S.",
    "...SS...",
    "S.SHHS.S",
    "S.SHHS.S",
    "...SS...",
    ".S....S.",
    "...SS...",
)

CLOUD_SILHOUETTE = (
    "........",
    "........",
    "..CCC...",
    ".CCCCCC.",
    "CCCCCCCC",
    "CCCCCCCC",
    "........",
    "........",
)

RAIN_SILHOUETTE = (
    "........",
    "..CCC...",
    ".CCCCCC.",
    "CCCCCCCC",
    "........",
    ".R...R..",
    ".R.R.R.R",
    "...R...R",
)

SNOW_SILHOUETTE = (
    "........",
    "..CCC...",
    ".CCCCCC.",
    "CCCCCCCC",
    "........",
    ".W...W..",
    "WWW.WWW.",
    ".W...W..",
)

WEATHER_PALETTE: dict[str, RGB] = {
    "S": GOLD,       # Sun rim: (255, 213, 78) - dominant 255
    "H": PEARL,      # Sun core: (255, 247, 232) - dominant 255
    "C": ICE,        # Cloud: (196, 239, 255) - dominant 255
    "R": ICE,        # Rain: (196, 239, 255) - dominant 255
    "W": PEARL,      # Snow: (255, 247, 232) - dominant 255
}


def render_weather_silhouette(pattern: tuple[str, ...]) -> Frame:
    """Render an 8x8 weather pattern into a frame with gutter isolation."""
    frame = new_frame()
    for y, row in enumerate(pattern[:8]):
        for x, char in enumerate(row[:8]):
            if char in WEATHER_PALETTE:
                put_pixel(frame, x, y, WEATHER_PALETTE[char])
    return frame


# ---------------------------------------------------------------- Payload Safety Verification

def validate_awtrix_payload(payload: dict[str, Any]) -> None:
    """Verify that an AWTRIX payload conforms strictly to hardware limits."""
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(raw) >= MAX_PAYLOAD_BYTES:
        raise ValueError(
            f"Payload is {len(raw)} bytes; strictly exceeds {MAX_PAYLOAD_BYTES} limit"
        )
    if "draw" in payload:
        for op in payload["draw"]:
            if isinstance(op, list) and op and op[0] == "bitmap":
                x0 = int(op[1])
                width = int(op[3])
                # Ensure column 8 safety gutter is never drawn over by the lane
                if x0 <= GUTTER_X < (x0 + width):
                    raise ValueError(f"Bitmap operation overlaps column {GUTTER_X} safety gutter")


# ---------------------------------------------------------------- Calm glance
#
# The one layout every pushed page uses (chosen 2026-09-26):
#
#   icon  x0..7    the only place with free colour; meaning lives in silhouette
#   gutter x8      always black
#   text  rows 1-5 one value in PEARL, optionally led by a short MIST label,
#                  centred, never scrolling -- anything too wide is refused
#   meter row 7    how high the value is on its own scale, coloured by level;
#                  a plain TRACK line when the value has no scale
#
# Colour therefore means the same thing on every page: the meter's colour is
# good / fair / poor / bad, never "this app's brand colour".

MIST: RGB = (178, 190, 206)  # label grey; dominant 206 still lights at brightness 1

LEVEL_COLORS: dict[str, RGB] = {
    "good": MINT,
    "fair": GOLD,
    "poor": AMBER,
    "bad": CORAL,
    "info": ICE,
}
TEXT_BUDGET = LANE_WIDTH  # the lane is 23px; text may use all of it
LABEL_GAP = 3  # one space's width between the grey label and the value


def glance_text_width(value: str, label: str | None = None) -> int:
    if not label:
        return measure_text(value)
    return measure_text(label) + LABEL_GAP + measure_text(value)


def render_glance_lane(
    value: str,
    *,
    label: str | None = None,
    meter: float | None = None,
    level: str = "info",
) -> Frame:
    """Render the 23x8 lane for one value, in the calm-glance layout.

    ``meter`` is 0..1 along the value's own scale (None = no scale).  The
    label is dropped before the value is ever squeezed; a value that does not
    fit on its own is a caller bug, so it raises rather than clipping.
    """
    if level not in LEVEL_COLORS:
        raise ValueError(f"unknown level {level!r}")
    frame = new_frame()
    # Row 7 first: nothing may be painted under the text band.  Since
    # 2026-10-03 this is Now Playing's rail: the fill brightens toward a PEARL
    # leader and the empty scale is a dark tint of the level colour, not grey.
    draw_level_rail(frame, meter, level)

    if label and glance_text_width(value, label) > TEXT_BUDGET:
        label = None
    width = glance_text_width(value, label)
    if width > TEXT_BUDGET:
        raise ValueError(f"{value!r} is {width}px; the lane holds {TEXT_BUDGET}px")
    x = LANE_X0 + (LANE_WIDTH - width) // 2
    if label:
        x += draw_text(frame, label, x, 1, MIST) + LABEL_GAP
    draw_text(frame, value, x, 1, PEARL)
    for y in range(HEIGHT):
        frame[y][GUTTER_X] = BLACK
    return frame


# ---------------------------------------------------------------- Rich pages
#
# The standard since 2026-10-03 is Now Playing: shaded objects, one palette
# per page carried from icon to rail, and motion with a purpose.  These are
# its techniques, shared so every pushed page draws them the same way.

TRACK_TINT = 0.16  # an empty rail is its own colour, dimmed -- never grey


def scale_color(color: RGB, k: float) -> RGB:
    """The same hue, darker (k < 1).  Shading tones may vanish at brightness 1."""
    return tuple(max(0, min(255, round(c * k))) for c in color)  # type: ignore[return-value]


def gradient_at(stops: Sequence[tuple[float, RGB]], position: float) -> RGB:
    """Colour at ``position`` (0..1) along gamma-correct gradient ``stops``."""
    position = max(0.0, min(1.0, position))
    for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
        if position <= p1:
            return gamma_lerp(c0, c1, 0.0 if p1 == p0 else (position - p0) / (p1 - p0))
    return stops[-1][1]


def draw_rich_rail(frame: Frame, fraction: float, stops: Sequence[tuple[float, RGB]],
                   *, y: int = 7, x0: int = LANE_X0, width: int = LANE_WIDTH) -> None:
    """Now Playing's progress rail: a graded fill up to the reading, a PEARL
    leader, and the rest of the scale as a dark tint of its own colours."""
    lead = max(1, min(width, round(max(0.0, min(1.0, fraction)) * width)))
    for i in range(width):
        colour = gradient_at(stops, (i + 0.5) / width)
        if i < lead - 1:
            put_pixel(frame, x0 + i, y, colour)
        elif i == lead - 1:
            put_pixel(frame, x0 + i, y, PEARL)
        else:
            put_pixel(frame, x0 + i, y, scale_color(colour, TRACK_TINT))


METER_TAIL = 0.45  # the start of a fill, relative to the level colour


def draw_level_rail(frame: Frame, meter: float | None, level: str = "info", *, y: int = 7) -> None:
    """A one-colour page's rail: the fill rises from 45% to the full level
    colour into a PEARL leader; the rest is the level colour tinted dark.
    ``meter=None`` (no scale) draws the tinted track alone."""
    colour = LEVEL_COLORS[level]
    track = scale_color(colour, TRACK_TINT)
    if meter is None:
        for i in range(LANE_WIDTH):
            put_pixel(frame, LANE_X0 + i, y, track)
        return
    lead = max(1, min(LANE_WIDTH, round(max(0.0, min(1.0, meter)) * LANE_WIDTH)))
    for i in range(LANE_WIDTH):
        if i < lead - 1:
            k = METER_TAIL + (1.0 - METER_TAIL) * (i + 1) / max(1, lead - 1)
            put_pixel(frame, LANE_X0 + i, y, gamma_lerp(scale_color(colour, METER_TAIL), colour, (k - METER_TAIL) / (1.0 - METER_TAIL)))
        elif i == lead - 1:
            put_pixel(frame, LANE_X0 + i, y, PEARL)
        else:
            put_pixel(frame, LANE_X0 + i, y, track)


def meter_fill(frame: Frame, *, y: int = 7) -> int:
    """How many rail pixels are filled (fill plus leader), for tests and previews."""
    return sum(1 for x in range(LANE_X0, LANE_X0 + LANE_WIDTH) if max(frame[y][x]) > 90)


def read_meter(frame: Frame, *, y: int = 7, x0: int = LANE_X0) -> tuple[int, RGB | None]:
    """(filled pixels including the leader, level colour) of a level rail, or
    (0, None) for a bare track.  The colour is the full-strength pixel beside
    the leader, or, for a one-pixel meter, the level whose tint the track is."""
    row = list(frame[y][x0:x0 + LANE_WIDTH])
    if PEARL not in row:
        return 0, None
    lead = row.index(PEARL)
    if lead >= 1:
        return lead + 1, row[lead - 1]
    track = row[-1] if len(row) > 1 else None
    for colour in LEVEL_COLORS.values():
        if track == scale_color(colour, TRACK_TINT):
            return 1, colour
    return 1, None


def rail_survives_brightness_one(frame: Frame, *, y: int = 7, x0: int = LANE_X0) -> bool:
    """At brightness 1 the fill's soft start and the tinted track drop out, as
    Now Playing's dark rim does; the leader and the full-colour pixel beside it
    must not.  A bare track (no scale) has nothing that must survive."""
    row = list(frame[y][x0:x0 + LANE_WIDTH])
    if PEARL not in row:
        return all(max(px) < LOW_BRIGHTNESS_DOMINANT_MIN for px in row)
    lead = row.index(PEARL)
    return lead == 0 or max(row[lead - 1]) >= LOW_BRIGHTNESS_DOMINANT_MIN


def render_rich_lane(value: str, *, fraction: float, stops: Sequence[tuple[float, RGB]],
                     label: str | None = None, label_color: RGB = MIST) -> Frame:
    """The 23x8 lane at Now Playing quality: the value in PEARL, an optional
    label tinted in the page's colour (dropped before the value is squeezed),
    and a rich rail on row 7."""
    frame = new_frame()
    draw_rich_rail(frame, fraction, stops)
    if label and glance_text_width(value, label) > TEXT_BUDGET:
        label = None
    width = glance_text_width(value, label)
    if width > TEXT_BUDGET:
        raise ValueError(f"{value!r} is {width}px; the lane holds {TEXT_BUDGET}px")
    x = LANE_X0 + (LANE_WIDTH - width) // 2
    if label:
        x += draw_text(frame, label, x, 1, label_color) + LABEL_GAP
    draw_text(frame, value, x, 1, PEARL)
    for row in range(HEIGHT):
        frame[row][GUTTER_X] = BLACK
    return frame


def draw_comet(frame: Frame, path: Sequence[tuple[int, int]], head: int,
               base: Sequence[RGB], trail: Sequence[float] = (1.0, 0.6, 0.3)) -> None:
    """The record's groove highlight: a PEARL head with a fading trail along
    ``path``; ``base[i]`` is the colour under point i.  Off-path heads draw
    nothing, which is how a comet rests between runs."""
    for k, mix in enumerate(trail):
        i = head - k
        if 0 <= i < len(path):
            x, y = path[i]
            put_pixel(frame, x, y, gamma_lerp(base[i], PEARL, mix))


def draw_lane_rail(frame: Frame, accent: RGB | None = None) -> None:
    """Draw the house ground rail: row 7, full lane width, in TRACK.

    Row 7 is the one element all four pushed scenes share, and it was being
    rendered four different ways -- full width here, inset two columns with
    bright pips on rows 0 AND 7 there, inset one column in a locally
    redefined TRACK in a third, and simply absent in the fourth.  Since the
    rotation advances every seven seconds, the ground changed length, colour
    and existence between pages, so the panel read as four unrelated apps
    rather than four pages of one system.

    Full width matters: insetting even one column leaves a dark stub beside
    the icon's road and at the far edge, so the ground does not actually meet
    across the black gutter at column 8.  The optional accent end-cap gives a
    scene its colour without breaking that meeting point, which is why it
    sits at the far edge rather than beside the gutter.

    Draw this FIRST.  Row 7 is below the rows 1..5 text band, but drawing it
    first is what guarantees it can never paint over a descender.
    """
    for x in range(LANE_X0, LANE_X0 + LANE_WIDTH):
        put_pixel(frame, x, 7, TRACK)
    if accent is not None:
        put_pixel(frame, LANE_X0 + LANE_WIDTH - 1, 7, accent)


def cycle_length(frames: list[Frame]) -> int:
    """The number of frames the animation actually has.

    Several icons asked for four frames and drew fewer: the companion's FOCUS
    and ON_AIR avatars are one frame repeated four times, REST is two, and the
    Octocat is one.  Every repeat is paid for in the GIF and in the 2KB
    payload budget, and buys nothing -- a viewer cannot see a frame that is
    identical to the one before it.

    Returns the shortest prefix length ``p`` dividing ``len(frames)`` such
    that the whole sequence is that prefix repeated.  Encoding only the first
    ``p`` frames leaves the loop looking exactly the same.
    """
    n = len(frames)
    for p in range(1, n + 1):
        if n % p == 0 and all(frames[i] == frames[i % p] for i in range(n)):
            return p
    return n


def dithered_fill(
    frame: Frame,
    x0: int, y0: int, x1: int, y1: int,
    top_color: RGB, bottom_color: RGB,
) -> None:
    """Fill a rectangular region with a dithered vertical gradient.

    Alternating pixels on each row use slightly different interpolation
    offsets, effectively doubling the apparent gradient resolution on an
    8-row display.  Ported from the sunset.ax checkerboard technique.
    """
    rows = max(1, y1 - y0 + 1)
    for y in range(y0, y1 + 1):
        t = (y - y0) / max(1, rows - 1)
        base = gamma_lerp(top_color, bottom_color, t)
        # Half-step offset for the alternate pixel
        t_half = min(1.0, t + 0.5 / max(1, rows - 1))
        half = gamma_lerp(top_color, bottom_color, t_half)
        for x in range(x0, x1 + 1):
            put_pixel(frame, x, y, half if (x + y) % 2 else base)


def render_sparkline(
    values: Sequence[float],
    *,
    label: str | None = None,
    color: RGB = ICE,
    baseline_color: RGB = TRACK,
) -> Frame:
    """Render a 23-wide sparkline in the lane area.

    ``values`` are normalized 0..1.  The most recent value is rightmost.
    The label (if given) is drawn in MIST at rows 0-4 and must fit the
    lane budget.  When a label is present, the sparkline occupies rows 5-7
    (height 3) to prevent overwriting row 4 typography.  Without a label,
    the sparkline occupies rows 4-7 (height 4).
    """
    frame = new_frame()
    # Baseline track on row 7
    for x in range(LANE_X0, LANE_X0 + LANE_WIDTH):
        put_pixel(frame, x, 7, baseline_color)
    # Gutter
    for y in range(HEIGHT):
        frame[y][GUTTER_X] = BLACK
    # Label at top of lane if provided
    has_label = False
    if label:
        lw = measure_text(label)
        if lw <= TEXT_BUDGET:
            has_label = True
            lx = LANE_X0 + (LANE_WIDTH - lw) // 2
            draw_text(frame, label, lx, 0, MIST)
    # Sparkline in rows 5-7 (height 3) if label present, rows 4-7 (height 4) if none
    spark_height = 3 if has_label else 4
    spark_y0 = 5 if has_label else 4
    n = min(len(values), LANE_WIDTH)
    if n == 0:
        return frame
    # Right-align the data
    x_start = LANE_X0 + LANE_WIDTH - n
    for i in range(n):
        v = max(0.0, min(1.0, values[len(values) - n + i]))
        bar_h = max(1, round(v * spark_height))
        x = x_start + i
        for dy in range(spark_height):
            y = spark_y0 + (spark_height - 1 - dy)
            if dy < bar_h:
                put_pixel(frame, x, y, color)
            else:
                put_pixel(frame, x, y, TRACK)
    return frame


def render_split_lane(
    top_value: str,
    bottom_value: str,
    *,
    top_color: RGB = PEARL,
    bottom_color: RGB = PEARL,
    meter: float | None = None,
    level: str = "info",
) -> Frame:
    """Render two values stacked vertically in the 23x8 lane with a row 7 meter.

    Uses compact 3x3 typography (FONT_3X3):
    - Top value at y=0 (rows 0-2)
    - Row 3 is an empty 1px separator
    - Bottom value at y=4 (rows 4-6)
    - Row 7 contains the meter or track rail
    Guarantees 0 off-panel draws, 0 meter collisions, and 0 inter-text collisions.
    """
    if level not in LEVEL_COLORS:
        raise ValueError(f"unknown level {level!r}")
    frame = new_frame()

    def _draw_half(value: str, y_start: int, color: RGB) -> None:
        w = measure_text_3x3(value)
        if w > LANE_WIDTH:
            raise ValueError(f"Split lane value {value!r} ({w}px) exceeds lane width {LANE_WIDTH}px")
        x = LANE_X0 + (LANE_WIDTH - w) // 2
        draw_text_3x3(frame, value, x, y_start, color)

    # Draw stacked values
    _draw_half(top_value, 0, top_color)
    _draw_half(bottom_value, 4, bottom_color)

    # Draw meter on row 7
    if meter is None:
        for x in range(LANE_X0, LANE_X0 + LANE_WIDTH):
            put_pixel(frame, x, 7, TRACK)
    else:
        fill = max(1, min(LANE_WIDTH, round(max(0.0, min(1.0, meter)) * LANE_WIDTH)))
        for i in range(LANE_WIDTH):
            put_pixel(frame, LANE_X0 + i, 7, LEVEL_COLORS[level] if i < fill else TRACK)

    # Safety gutter
    for y in range(HEIGHT):
        frame[y][GUTTER_X] = BLACK

    return frame


def render_dual_lane(
    left_value: str,
    right_value: str,
    *,
    left_color: RGB = PEARL,
    right_color: RGB = PEARL,
    left_meter: float | None = None,
    right_meter: float | None = None,
    left_level: str = "info",
    right_level: str = "info",
) -> Frame:
    """Render two values side-by-side in the 23x8 lane with dual meters on row 7.

    Left value is left-aligned, right value is right-aligned.
    Row 7 contains two 11-pixel meters separated by a 1-pixel black spacer at column 20.
    """
    if left_level not in LEVEL_COLORS:
        raise ValueError(f"unknown left_level {left_level!r}")
    if right_level not in LEVEL_COLORS:
        raise ValueError(f"unknown right_level {right_level!r}")

    frame = new_frame()

    # Draw meters on row 7 first
    half_width = 11
    # Left meter: columns LANE_X0 .. LANE_X0 + 10 (9..19)
    if left_meter is None:
        for x in range(LANE_X0, LANE_X0 + half_width):
            put_pixel(frame, x, 7, TRACK)
    else:
        fill_left = max(1, min(half_width, round(max(0.0, min(1.0, left_meter)) * half_width)))
        for i in range(half_width):
            put_pixel(frame, LANE_X0 + i, 7, LEVEL_COLORS[left_level] if i < fill_left else TRACK)

    # Divider between meters: column LANE_X0 + 11 (column 20)
    put_pixel(frame, LANE_X0 + half_width, 7, BLACK)

    # Right meter: columns LANE_X0 + 12 .. LANE_X0 + 22 (21..31)
    right_x0 = LANE_X0 + half_width + 1
    if right_meter is None:
        for x in range(right_x0, right_x0 + half_width):
            put_pixel(frame, x, 7, TRACK)
    else:
        fill_right = max(1, min(half_width, round(max(0.0, min(1.0, right_meter)) * half_width)))
        for i in range(half_width):
            put_pixel(frame, right_x0 + i, 7, LEVEL_COLORS[right_level] if i < fill_right else TRACK)

    # Draw text in row 1
    lw = measure_text(left_value)
    rw = measure_text(right_value)
    if lw + rw > LANE_WIDTH:
        raise ValueError(f"Dual values {left_value!r} ({lw}px) and {right_value!r} ({rw}px) exceed lane width {LANE_WIDTH}px")

    draw_text(frame, left_value, LANE_X0, 1, left_color)
    draw_text(frame, right_value, LANE_X0 + LANE_WIDTH - rw, 1, right_color)

    # Safety gutter
    for y in range(HEIGHT):
        frame[y][GUTTER_X] = BLACK

    return frame

