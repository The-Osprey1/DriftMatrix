"""Render the Jewelbox Arcade Weather Brief at the panel's exact 32x8 size.

This is an offline design prototype only.  It never connects to the AWTRIX.
The source art uses hard pixel edges and is optionally graded with the same
order of operations as awtrix-ng v1.1.0: saturation, 16-bit gamma, then linear
brightness.  The defaults match this panel (saturation 90, gamma 1.8,
brightness 45).

The two scenes deliberately share one visual grammar:

* Current: a faceted condition jewel, fixed temperature, and an open low-to-
  high rail whose white cap locates the current temperature in today's range.
* Rainline: a connected rain animation, fixed minutes-to-rain, and a 92-minute
  forecast rail ordered from future at left toward a fixed NOW cap at right.
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from PIL import Image


RGB = tuple[int, int, int]
Frame = list[list[RGB]]

WIDTH = 32
HEIGHT = 8
ICON_X0 = 0
ICON_X1 = 7
GUTTER_X = 8
LANE_X0 = 9
LANE_X1 = 31

BLACK: RGB = (0, 0, 0)

# Jewelbox Arcade palette.  The values are intentionally high in the source
# image: darker desktop-friendly colors collapse after gamma 1.8 at b45.
PEARL: RGB = (255, 247, 232)
ICE: RGB = (196, 239, 255)
SKY: RGB = (82, 206, 255)
AZURE: RGB = (38, 136, 255)
INDIGO: RGB = (91, 77, 226)
VIOLET: RGB = (189, 84, 255)
TRACK: RGB = (118, 71, 166)
GOLD: RGB = (255, 213, 78)
AMBER: RGB = (255, 151, 47)
CORAL: RGB = (255, 83, 93)
MINT: RGB = (73, 232, 165)


FONT_3X5: dict[str, tuple[str, ...]] = {
    "-": ("000", "000", "111", "000", "000"),
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
    "H": ("101", "101", "111", "101", "101"),
    "M": ("101", "111", "111", "101", "101"),
    "N": ("101", "111", "111", "111", "101"),
    # Rounded, so the letter O is not byte-identical to the digit 0.
    # It was, in every font in this repo.  Rounding the letter rather
    # than altering the digit also dissolves O~8, O~G and O~U, which
    # were each a single pixel apart, and costs no width.
    "O": ("010", "101", "101", "101", "010"),
    "W": ("101", "101", "111", "111", "101"),
}

DEGREE = ("11", "11")


def new_frame() -> Frame:
    return [[BLACK for _ in range(WIDTH)] for _ in range(HEIGHT)]


def put(frame: Frame, x: int, y: int, color: RGB) -> None:
    if 0 <= x < WIDTH and 0 <= y < HEIGHT:
        frame[y][x] = color


def hline(frame: Frame, x0: int, x1: int, y: int, color: RGB) -> None:
    for x in range(x0, x1 + 1):
        put(frame, x, y, color)


def vline(frame: Frame, x: int, y0: int, y1: int, color: RGB) -> None:
    for y in range(y0, y1 + 1):
        put(frame, x, y, color)


def gamma_lerp(a: RGB, b: RGB, t: float, gamma: float = 1.8) -> RGB:
    """Interpolate in emitted-light space so the LED steps look even."""

    t = max(0.0, min(1.0, t))
    mixed: list[int] = []
    for ca, cb in zip(a, b):
        la = (ca / 255.0) ** gamma
        lb = (cb / 255.0) ** gamma
        value = ((1.0 - t) * la + t * lb) ** (1.0 / gamma)
        mixed.append(round(value * 255.0))
    return tuple(mixed)  # type: ignore[return-value]


def text_width(value: str) -> int:
    widths = [2 if char == "\N{DEGREE SIGN}" else 3 for char in value]
    return sum(widths) + max(0, len(widths) - 1)


def draw_text(
    frame: Frame,
    value: str,
    x0: int,
    y0: int,
    color: RGB,
    degree_color: RGB | None = None,
) -> None:
    x = x0
    for char in value:
        glyph: Sequence[str]
        if char == "\N{DEGREE SIGN}":
            glyph = DEGREE
            tint = degree_color or color
        else:
            glyph = FONT_3X5[char]
            tint = color
        for gy, row in enumerate(glyph):
            for gx, bit in enumerate(row):
                if bit == "1":
                    put(frame, x + gx, y0 + gy, tint)
        x += len(glyph[0]) + 1


def draw_capsule(frame: Frame, rail: Sequence[RGB]) -> None:
    """A hollow seven-row arcade capsule; the forecast rail is its lower rim."""

    if len(rail) != 19:
        raise ValueError("capsule rail must contain 19 pixels")
    hline(frame, 11, 29, 0, ICE)
    put(frame, 10, 1, SKY)
    put(frame, 30, 1, SKY)
    vline(frame, 9, 2, 5, AZURE)
    vline(frame, 31, 2, 5, INDIGO)
    put(frame, 10, 6, INDIGO)
    put(frame, 30, 6, INDIGO)
    for index, color in enumerate(rail):
        put(frame, 11 + index, 7, color)


# Absolute temperature -> colour, in degrees Fahrenheit.  Anchors are spaced by
# how the temperature *feels* rather than evenly, so the busy 50-80F range gets
# most of the hue budget.
TEMP_ANCHORS: tuple[tuple[float, RGB], ...] = (
    (20.0, (110, 170, 255)),
    (35.0, (80, 205, 255)),
    (50.0, (90, 225, 190)),
    (65.0, (120, 230, 120)),
    (78.0, (245, 225, 90)),
    (90.0, (255, 150, 45)),
    (105.0, (255, 70, 40)),
)


def temperature_color(fahrenheit: float) -> RGB:
    """The colour of an absolute temperature, clamped at both ends."""

    if fahrenheit <= TEMP_ANCHORS[0][0]:
        return TEMP_ANCHORS[0][1]
    if fahrenheit >= TEMP_ANCHORS[-1][0]:
        return TEMP_ANCHORS[-1][1]
    for (t0, c0), (t1, c1) in zip(TEMP_ANCHORS, TEMP_ANCHORS[1:]):
        if t0 <= fahrenheit <= t1:
            return gamma_lerp(c0, c1, (fahrenheit - t0) / (t1 - t0))
    return TEMP_ANCHORS[-1][1]


def temperature_rail(
    low: float,
    current: float,
    high: float,
    width: int = 23,
) -> tuple[list[RGB], int]:
    """A rail whose COLOUR encodes the day's actual temperature span.

    The previous version built the same SKY->MINT->GOLD ramp from the column
    index alone, so `low`, `current` and `high` only moved the marker: a
    55-62F day and a 40-110F day drew a byte-identical bar.  That is a colour
    scale that encodes nothing, presented in the visual language of one.

    Now column i is the colour of the temperature it actually represents, so a
    narrow mild day reads as a short calm band and a wide extreme day sweeps
    blue to red.  The marker sits on the colour matching the current reading.
    """

    if width < 2:
        raise ValueError("temperature rail must be at least two pixels wide")
    span = high - low
    rail = [
        temperature_color(low + span * (index / float(width - 1)))
        for index in range(width)
    ]
    denominator = max(1.0, span)
    marker = round(max(0.0, min(1.0, (current - low) / denominator)) * (width - 1))
    return rail, marker


def draw_sun_cloud_jewel(frame: Frame, phase: int) -> None:
    """A stable, connected, faceted partly-cloudy silhouette."""

    # The warm sunstone remains a connected disc in every phase.
    hline(frame, 3, 5, 0, GOLD)
    hline(frame, 2, 6, 1, GOLD)
    hline(frame, 2, 6, 2, AMBER)
    hline(frame, 3, 6, 3, CORAL)
    put(frame, 2, 1, PEARL)
    put(frame, 3, 1, PEARL)

    # A two-pixel glint walks along the disc but never becomes a lone blink.
    glints = (
        ((3, 0), (4, 0)),
        ((5, 0), (6, 1)),
        ((6, 1), (6, 2)),
        ((5, 2), (6, 2)),
        ((3, 1), (4, 1)),
        ((2, 1), (2, 2)),
    )
    for x, y in glints[phase % len(glints)]:
        put(frame, x, y, PEARL)

    # The cool sapphire cloud overlaps the sun and connects all facets.
    hline(frame, 2, 4, 3, ICE)
    hline(frame, 1, 6, 4, SKY)
    hline(frame, 0, 7, 5, SKY)
    hline(frame, 1, 6, 6, AZURE)
    hline(frame, 2, 5, 7, INDIGO)
    put(frame, 2, 4, ICE)
    put(frame, 3, 4, ICE)
    put(frame, 1, 5, ICE)
    put(frame, 6, 5, AZURE)
    put(frame, 5, 6, INDIGO)


def draw_sun_jewel(frame: Frame, phase: int) -> None:
    """A round enamel sun whose attached highlight moves clockwise."""

    hline(frame, 3, 4, 0, GOLD)
    hline(frame, 2, 5, 1, GOLD)
    hline(frame, 1, 6, 2, AMBER)
    hline(frame, 0, 7, 3, AMBER)
    hline(frame, 0, 7, 4, CORAL)
    hline(frame, 1, 6, 5, CORAL)
    hline(frame, 2, 5, 6, VIOLET)
    hline(frame, 3, 4, 7, INDIGO)
    glints = (
        ((3, 0), (4, 0)),
        ((5, 1), (6, 2)),
        ((7, 3), (7, 4)),
        ((5, 6), (4, 7)),
        ((2, 6), (1, 5)),
        ((0, 4), (0, 3)),
        ((1, 2), (2, 1)),
    )
    for x, y in glints[phase % len(glints)]:
        put(frame, x, y, PEARL)


def draw_moon_jewel(frame: Frame, phase: int) -> None:
    """A full circular night jewel with a cool moving rim glint."""

    hline(frame, 3, 4, 0, ICE)
    hline(frame, 2, 5, 1, SKY)
    hline(frame, 1, 6, 2, SKY)
    hline(frame, 0, 7, 3, AZURE)
    hline(frame, 0, 7, 4, AZURE)
    hline(frame, 1, 6, 5, INDIGO)
    hline(frame, 2, 5, 6, VIOLET)
    hline(frame, 3, 4, 7, TRACK)
    put(frame, 2, 2, ICE)
    put(frame, 3, 2, ICE)
    put(frame, 5, 4, INDIGO)
    put(frame, 4, 5, INDIGO)
    glints = (
        ((3, 0), (4, 0)),
        ((5, 1), (6, 2)),
        ((7, 3), (7, 4)),
        ((5, 6), (4, 7)),
        ((2, 6), (1, 5)),
        ((0, 4), (0, 3)),
        ((1, 2), (2, 1)),
    )
    for x, y in glints[phase % len(glints)]:
        put(frame, x, y, PEARL)


def draw_cloud_jewel(frame: Frame, phase: int, y0: int = 1) -> None:
    """A stable sapphire cloud with a connected two-pixel top glint."""

    hline(frame, 2, 4, y0, ICE)
    hline(frame, 1, 6, y0 + 1, SKY)
    hline(frame, 0, 7, y0 + 2, SKY)
    hline(frame, 0, 7, y0 + 3, AZURE)
    hline(frame, 1, 6, y0 + 4, INDIGO)
    hline(frame, 2, 5, y0 + 5, TRACK)
    put(frame, 1, y0 + 2, ICE)
    put(frame, 2, y0 + 1, ICE)
    put(frame, 6, y0 + 3, INDIGO)
    glints = (((2, y0), (3, y0)), ((3, y0), (4, y0)), ((4, y0), (5, y0 + 1)))
    for x, y in glints[phase % len(glints)]:
        put(frame, x, y, PEARL)


def draw_fog_jewel(frame: Frame, phase: int) -> None:
    """A compact cloud over two coherent, one-pixel-shifting mist bands."""

    draw_cloud_jewel(frame, phase, y0=0)
    offset = phase % 2
    hline(frame, offset, 5 + offset, 6, ICE)
    hline(frame, 1 - offset, 6 - offset, 7, SKY)


def draw_snow_jewel(frame: Frame, phase: int) -> None:
    """A cloud with two connected snow crystals that trade vertical emphasis."""

    draw_cloud_jewel(frame, phase, y0=0)
    lift = phase % 2
    for cx in (2, 6):
        cy = 6 - lift if cx == 2 else 5 + lift
        put(frame, cx, cy, PEARL)
        put(frame, cx - 1, min(7, cy + 1), ICE)
        put(frame, cx + 1, min(7, cy + 1), ICE)


def draw_storm_jewel(frame: Frame, phase: int) -> None:
    """A cloud and attached bolt; the charge moves down without flashing off."""

    draw_cloud_jewel(frame, phase, y0=0)
    bolt = ((4, 4), (3, 5), (4, 5), (3, 6), (2, 7))
    head = phase % len(bolt)
    for index, (x, y) in enumerate(bolt):
        distance = (head - index) % len(bolt)
        tint = PEARL if distance == 0 else GOLD if distance == 1 else AMBER
        put(frame, x, y, tint)


def draw_condition_jewel(frame: Frame, kind: str, is_day: bool, phase: int) -> None:
    """Draw one semantic weather category in the fixed 8x8 icon well."""

    if kind == "clear":
        (draw_sun_jewel if is_day else draw_moon_jewel)(frame, phase)
    elif kind == "part":
        if is_day:
            draw_sun_cloud_jewel(frame, phase)
        else:
            draw_moon_jewel(frame, phase)
            # The compact foreground cloud keeps the moon visibly circular.
            hline(frame, 2, 4, 3, ICE)
            hline(frame, 1, 6, 4, SKY)
            hline(frame, 0, 7, 5, SKY)
            hline(frame, 1, 6, 6, AZURE)
            hline(frame, 2, 5, 7, INDIGO)
    elif kind == "fog":
        draw_fog_jewel(frame, phase)
    elif kind == "rain":
        draw_rain_jewel(frame, phase)
    elif kind == "snow":
        draw_snow_jewel(frame, phase)
    elif kind == "storm":
        draw_storm_jewel(frame, phase)
    else:
        draw_cloud_jewel(frame, phase)


def current_scene(phase: int, temperature: int = 72, low: int = 63, high: int = 84) -> Frame:
    frame = new_frame()
    draw_sun_cloud_jewel(frame, phase)

    rail, marker = temperature_rail(low, temperature, high)
    for index, color in enumerate(rail):
        put(frame, LANE_X0 + index, 7, color)
    marker_x = LANE_X0 + marker
    # The cap is connected to the open rail and locates the current value.
    put(frame, marker_x, 6, PEARL)
    put(frame, marker_x, 7, PEARL)

    value = f"{temperature}\N{DEGREE SIGN}"
    x = LANE_X0 + (23 - text_width(value)) // 2
    draw_text(frame, value, x, 1, PEARL, degree_color=GOLD)
    return frame


def rain_forecast_rail() -> list[RGB]:
    """A 92-minute rail (23 columns, roughly four minutes per column)."""

    rail: list[RGB] = []
    for index in range(23):
        # Dry for ~16 minutes, then a building shower with a softer tail.
        if index < 4:
            rail.append(TRACK)
        elif index < 10:
            rail.append(gamma_lerp(AZURE, SKY, (index - 4) / 5.0))
        elif index < 16:
            rail.append(gamma_lerp(SKY, VIOLET, (index - 10) / 5.0))
        else:
            rail.append(gamma_lerp(VIOLET, AZURE, (index - 16) / 6.0))
    return rail


def draw_rain_jewel(frame: Frame, phase: int) -> None:
    """A faceted cloud with one calm, connected two-pixel motion at a time."""

    hline(frame, 2, 5, 0, ICE)
    hline(frame, 1, 6, 1, SKY)
    hline(frame, 0, 7, 2, SKY)
    hline(frame, 1, 6, 3, AZURE)
    hline(frame, 2, 5, 4, INDIGO)
    put(frame, 2, 1, ICE)
    put(frame, 3, 1, ICE)
    put(frame, 1, 2, ICE)
    put(frame, 6, 2, AZURE)
    put(frame, 5, 3, INDIGO)

    # Both drops remain connected and lit. Only one shifts per half-cycle, so
    # a frame transition changes two physical pixels instead of a noisy field.
    left_y = 5 + (1 if phase % 6 in (1, 2) else 0)
    right_y = 5 + (1 if phase % 6 in (4, 5) else 0)
    put(frame, 1, left_y, ICE)
    put(frame, 1, left_y + 1, ICE)
    put(frame, 5, right_y, SKY)
    put(frame, 5, right_y + 1, SKY)


def rain_scene(phase: int, eta_minutes: int = 18) -> Frame:
    frame = new_frame()
    draw_rain_jewel(frame, phase)

    # Forecast reads from farther future at left toward the fixed NOW cap.
    forecast = list(reversed(rain_forecast_rail()))
    for index, color in enumerate(forecast[:-1]):
        put(frame, LANE_X0 + index, 7, color)
    put(frame, LANE_X1, 6, PEARL)
    put(frame, LANE_X1, 7, PEARL)

    value = f"{eta_minutes}M"
    x = LANE_X0 + (23 - text_width(value)) // 2
    number = str(eta_minutes)
    draw_text(frame, number, x, 1, PEARL)
    draw_text(frame, "M", x + text_width(number) + 1, 1, ICE)
    return frame


def trunc_div(numerator: int, denominator: int) -> int:
    """C/C++ integer division (toward zero), including negative values."""

    return math.trunc(numerator / denominator)


def desaturate(color: RGB, saturation: int) -> RGB:
    if saturation >= 100:
        return color
    saturation = max(0, saturation)
    r, g, b = color
    gray = (77 * r + 150 * g + 29 * b) >> 8
    return tuple(
        gray + trunc_div((channel - gray) * saturation, 100)
        for channel in color
    )  # type: ignore[return-value]


def lround_positive(value: float) -> int:
    return math.floor(value + 0.5)


def grade_channel(channel: int, gamma: float, brightness: int) -> int:
    if channel == 0:
        gamma16 = 0
    else:
        fraction = (channel / 255.0) ** gamma if gamma > 0.0 else channel / 255.0
        gamma16 = max(1, min(65535, lround_positive(fraction * 65535.0)))
    lit = gamma16 * brightness // 255
    # Default correction and tint are both white, so scale8(255, 255)=255.
    return min(255, (lit + 128) // 257)


def grade_color(color: RGB, gamma: float, brightness: int, saturation: int) -> RGB:
    mixed = desaturate(color, saturation)
    return tuple(
        grade_channel(channel, gamma, brightness) for channel in mixed
    )  # type: ignore[return-value]


def grade_frame(frame: Frame, gamma: float, brightness: int, saturation: int) -> Frame:
    return [
        [grade_color(color, gamma, brightness, saturation) for color in row]
        for row in frame
    ]


def raw_image(frame: Frame) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BLACK)
    image.putdata([color for row in frame for color in row])
    return image


def led_image(frame: Frame, scale: int, gap: int) -> Image.Image:
    """Nearest-neighbor LED-cell view; no resizing or anti-aliasing is used."""

    image = Image.new("RGB", (WIDTH * scale, HEIGHT * scale), (2, 2, 3))
    pixels = image.load()
    for y, row in enumerate(frame):
        for x, color in enumerate(row):
            for sy in range(gap, scale - gap):
                for sx in range(gap, scale - gap):
                    pixels[x * scale + sx, y * scale + sy] = color
    return image


def storyboard(frames: Sequence[Frame], scale: int, gap: int) -> Image.Image:
    gutter = scale
    cells = [led_image(frame, scale, gap) for frame in frames]
    out = Image.new(
        "RGB",
        (len(cells) * WIDTH * scale + (len(cells) - 1) * gutter, HEIGHT * scale),
        (2, 2, 3),
    )
    for index, cell in enumerate(cells):
        out.paste(cell, (index * (WIDTH * scale + gutter), 0))
    return out


def connected_components(frame: Frame, bounds: tuple[int, int, int, int]) -> list[set[tuple[int, int]]]:
    x0, y0, x1, y1 = bounds
    lit = {
        (x, y)
        for y in range(y0, y1 + 1)
        for x in range(x0, x1 + 1)
        if frame[y][x] != BLACK
    }
    components: list[set[tuple[int, int]]] = []
    while lit:
        seed = lit.pop()
        component = {seed}
        stack = [seed]
        while stack:
            x, y = stack.pop()
            for nx in range(x - 1, x + 2):
                for ny in range(y - 1, y + 2):
                    if (nx, ny) in lit:
                        lit.remove((nx, ny))
                        component.add((nx, ny))
                        stack.append((nx, ny))
        components.append(component)
    return components


def validate(frames: Iterable[Frame]) -> None:
    checked = 0
    for frame in frames:
        assert len(frame) == HEIGHT
        assert all(len(row) == WIDTH for row in frame)
        assert all(frame[y][GUTTER_X] == BLACK for y in range(HEIGHT))
        assert all(
            isinstance(channel, int) and 0 <= channel <= 255
            for row in frame
            for color in row
            for channel in color
        )
        components = connected_components(frame, (ICON_X0, 0, ICON_X1, 7))
        assert components and max(len(component) for component in components) >= 24
        assert all(len(component) != 1 for component in components)
        checked += 1
    if checked == 0:
        raise AssertionError("no frames rendered")


@dataclass(frozen=True)
class RenderConfig:
    output: Path
    scale: int = 18
    gap: int = 1
    gamma: float = 1.8
    brightness: int = 45
    saturation: int = 90
    frame_ms: int = 250


def build_animation() -> list[Frame]:
    # One exact local icon loop per semantic scene. Production chooses one
    # scene from data and never switches scenes merely for decoration.
    return [current_scene(index) for index in range(6)] + [rain_scene(index) for index in range(6)]


def condition_gallery() -> list[Frame]:
    """One production-shaped panel for every supported semantic icon state."""

    specs = (
        ("clear", True, False),
        ("clear", False, False),
        ("part", True, False),
        ("part", False, False),
        ("cloud", True, False),
        ("fog", True, False),
        ("rain", True, True),
        ("snow", True, True),
        ("storm", True, True),
    )
    panels: list[Frame] = []
    for kind, is_day, precip in specs:
        frame = rain_scene(0, eta_minutes=18) if precip else current_scene(0)
        for y in range(HEIGHT):
            for x in range(ICON_X0, ICON_X1 + 1):
                frame[y][x] = BLACK
        draw_condition_jewel(frame, kind, is_day, 0)
        panels.append(frame)
    return panels


def render(config: RenderConfig) -> list[Path]:
    source_frames = build_animation()
    validate(source_frames)
    graded_frames = [
        grade_frame(frame, config.gamma, config.brightness, config.saturation)
        for frame in source_frames
    ]

    config.output.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []

    raw_path = config.output / "weather_brief_jewelbox_32x8.gif"
    raw = [raw_image(frame) for frame in graded_frames]
    raw[0].save(
        raw_path,
        save_all=True,
        append_images=raw[1:],
        duration=config.frame_ms,
        loop=0,
        disposal=2,
        optimize=False,
    )
    paths.append(raw_path)

    preview_path = config.output / "weather_brief_jewelbox_led.gif"
    preview = [led_image(frame, config.scale, config.gap) for frame in graded_frames]
    preview[0].save(
        preview_path,
        save_all=True,
        append_images=preview[1:],
        duration=config.frame_ms,
        loop=0,
        disposal=2,
        optimize=False,
    )
    paths.append(preview_path)

    for label, segment in (
        ("current", graded_frames[:6]),
        ("rainline", graded_frames[6:]),
    ):
        segment_path = config.output / f"weather_brief_jewelbox_{label}_led.gif"
        segment_preview = [led_image(frame, config.scale, config.gap) for frame in segment]
        segment_preview[0].save(
            segment_path,
            save_all=True,
            append_images=segment_preview[1:],
            duration=config.frame_ms,
            loop=0,
            disposal=2,
            optimize=False,
        )
        paths.append(segment_path)

    # Six deliberately chosen moments communicate both scene grammars without
    # relying on animation playback in the reviewer.
    key_indices = (0, 2, 4, 6, 7, 9, 11)
    contact_path = config.output / "weather_brief_jewelbox_contact_sheet.png"
    storyboard([graded_frames[index] for index in key_indices], config.scale, config.gap).save(
        contact_path
    )
    paths.append(contact_path)

    source_path = config.output / "weather_brief_jewelbox_source_contact_sheet.png"
    storyboard([source_frames[index] for index in key_indices], config.scale, config.gap).save(
        source_path
    )
    paths.append(source_path)

    gallery = condition_gallery()
    gallery_path = config.output / "weather_brief_jewelbox_conditions.png"
    storyboard(
        [grade_frame(frame, config.gamma, config.brightness, config.saturation) for frame in gallery],
        config.scale,
        config.gap,
    ).save(gallery_path)
    paths.append(gallery_path)

    gallery_source_path = config.output / "weather_brief_jewelbox_conditions_source.png"
    storyboard(gallery, config.scale, config.gap).save(gallery_source_path)
    paths.append(gallery_source_path)

    return paths


def parse_args() -> RenderConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path.home() / ".codex" / "visualizations" / "weather_brief_jewelbox",
    )
    parser.add_argument("--scale", type=int, default=18)
    parser.add_argument("--gap", type=int, default=1)
    parser.add_argument("--gamma", type=float, default=1.8)
    parser.add_argument("--brightness", type=int, default=45)
    parser.add_argument("--saturation", type=int, default=90)
    parser.add_argument("--frame-ms", type=int, default=250)
    args = parser.parse_args()
    if args.scale < 3:
        parser.error("--scale must be at least 3")
    if not 0 <= args.gap < args.scale / 2:
        parser.error("--gap must leave at least one visible pixel per LED cell")
    if not 0 <= args.brightness <= 255:
        parser.error("--brightness must be in 0..255")
    if not 0 <= args.saturation <= 100:
        parser.error("--saturation must be in 0..100")
    if args.frame_ms < 20:
        parser.error("--frame-ms must be at least 20")
    return RenderConfig(
        output=args.output.resolve(),
        scale=args.scale,
        gap=args.gap,
        gamma=args.gamma,
        brightness=args.brightness,
        saturation=args.saturation,
        frame_ms=args.frame_ms,
    )


def main() -> None:
    config = parse_args()
    paths = render(config)
    print(
        f"validated {len(build_animation())} exact {WIDTH}x{HEIGHT} frames; "
        f"gamma={config.gamma:g}, brightness={config.brightness}, "
        f"saturation={config.saturation}"
    )
    for path in paths:
        print(path)


if __name__ == "__main__":
    main()
