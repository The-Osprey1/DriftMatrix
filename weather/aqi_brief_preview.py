"""Render the uncluttered AQI Brief at the panel's exact 32x8 resolution.

The preview is deliberately production-shaped: the left eight columns are the
only animated area, column eight is always black, and the right 23 columns are
a static information lane.  This lets AWTRIX animate one tiny icon locally
without receiving full-screen animation frames from the PC.

The six category hues follow the US AQI order, but their source RGB values are
lifted for this panel's gamma 2.2 / brightness 45 pipeline.  Meaning is never
color-only: the lane says AQI explicitly and gives the current numeric value.
There are intentionally no forecast or reference bars competing with it.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image

try:
    from .weather_brief_preview import (
        BLACK,
        HEIGHT,
        WIDTH,
        Frame,
        gamma_lerp,
        grade_color,
        grade_frame,
        led_image,
        new_frame,
        put,
        raw_image,
        storyboard,
    )
except ImportError:  # Direct execution from the weather directory.
    from weather_brief_preview import (  # type: ignore[no-redef]
        BLACK,
        HEIGHT,
        WIDTH,
        Frame,
        gamma_lerp,
        grade_color,
        grade_frame,
        led_image,
        new_frame,
        put,
        raw_image,
        storyboard,
    )


RGB = tuple[int, int, int]

PANEL_GAMMA = 2.2
PANEL_BRIGHTNESS = 45
PANEL_SATURATION = 90

ICON_X0 = 0
ICON_X1 = 7
GUTTER_X = 8
LANE_X0 = 9
LANE_X1 = 31
LANE_WIDTH = 23

PEARL: RGB = (255, 247, 232)
ICE: RGB = (196, 239, 255)

# EPA category order, perceptually lifted for a gamma-corrected low-brightness
# LED panel.  The hues remain green/yellow/orange/red/purple/maroon while the
# high source values keep every category visible after grading.
AQI_COLORS: tuple[RGB, ...] = (
    (69, 255, 157),   # Good
    (255, 226, 72),   # Moderate
    (255, 151, 47),   # Unhealthy for sensitive groups
    (255, 77, 91),    # Unhealthy
    (202, 100, 255),  # Very unhealthy
    (255, 65, 143),   # Hazardous; lifted maroon for this panel
)

AQI_LABELS = ("GOOD", "MOD", "USG", "BAD", "V.BAD", "HAZ")
AQI_LIMITS = (50, 100, 150, 200, 300)


# Proportional 3x5 alphabet.  I is one column, which is what allows AQI plus a
# three-digit reading to use the exact 23-pixel information lane.
FONT_3X5: dict[str, tuple[str, ...]] = {
    "+": ("000", "010", "111", "010", "000"),
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
    # Stays a single column.  Widening it to match the other fonts
    # pushes "AQI 126" to 25px against this app's 23px lane, and this
    # module raises on overflow rather than clipping.  It is not
    # ambiguous here: unlike the other fonts, "1" is already 3px.
    "I": ("1", "1", "1", "1", "1"),
    "Q": ("111", "101", "101", "111", "001"),
    " ": ("0", "0", "0", "0", "0"),
}


# The occupied silhouette never changes.  Breathing is represented by a
# connected highlight expanding inside each lobe, not by isolated pixels
# blinking at the perimeter.
LEFT_LUNG = frozenset(
    {
        (1, 2), (2, 2), (3, 2),
        (0, 3), (1, 3), (2, 3),
        (0, 4), (1, 4), (2, 4),
        (0, 5), (1, 5), (2, 5),
        (1, 6), (2, 6),
        (2, 7),
    }
)
RIGHT_LUNG = frozenset((7 - x, y) for x, y in LEFT_LUNG)
AIRWAY = frozenset({(3, 0), (4, 0), (3, 1), (4, 1), (2, 2), (3, 2), (4, 2), (5, 2)})

LEFT_HIGHLIGHTS: tuple[frozenset[tuple[int, int]], ...] = (
    frozenset({(2, 2), (2, 3), (2, 4), (1, 4), (1, 5)}),
    frozenset({(2, 2), (1, 3), (2, 3), (1, 4), (2, 4), (1, 5), (2, 5)}),
    frozenset({(1, 2), (2, 2), (1, 3), (2, 3), (0, 4), (1, 4), (2, 4), (0, 5), (1, 5), (1, 6)}),
    frozenset({(2, 2), (1, 3), (2, 3), (1, 4), (2, 4), (1, 5), (2, 5)}),
)


def category_index(aqi: int | float) -> int:
    value = max(0, int(round(float(aqi))))
    for index, limit in enumerate(AQI_LIMITS):
        if value <= limit:
            return index
    return 5


def display_text(aqi: int | float) -> str:
    """Return an explicit, lane-safe US AQI reading.

    US AQI is conventionally capped at 500. ``AQI 500`` is exactly 23 columns
    in the proportional font, so the label stays present even at the maximum
    instead of leaving viewers to infer what a bare percentage-like number is.
    """

    value = min(500, max(0, int(round(float(aqi)))))
    return f"AQI {value}"


def text_width(value: str) -> int:
    glyphs = [FONT_3X5[char] for char in value]
    return sum(len(glyph[0]) for glyph in glyphs) + max(0, len(glyphs) - 1)


def text_columns(value: str) -> tuple[int, ...]:
    columns: list[int] = []
    for char_index, char in enumerate(value):
        glyph = FONT_3X5[char]
        for x in range(len(glyph[0])):
            mask = 0
            for y, row in enumerate(glyph):
                if row[x] == "1":
                    mask |= 1 << y
            columns.append(mask)
        if char_index < len(value) - 1:
            columns.append(0)
    return tuple(columns)


def draw_text(frame: Frame, value: str, x0: int, y0: int, label_color: RGB, value_color: RGB) -> None:
    x = x0
    for char_index, char in enumerate(value):
        glyph = FONT_3X5[char]
        color = label_color if char_index < 3 else value_color
        for gy, row in enumerate(glyph):
            for gx, bit in enumerate(row):
                if bit == "1":
                    put(frame, x + gx, y0 + gy, color)
        x += len(glyph[0])
        if char_index < len(value) - 1:
            x += 1


def _dim(color: RGB, strength: float) -> RGB:
    # Interpolating in emitted-light space preserves a useful difference after
    # the panel's nonlinear gamma stage.
    return gamma_lerp(BLACK, color, strength, gamma=PANEL_GAMMA)


def draw_lungs_jewel(frame: Frame, level: int, phase: int, *, color: RGB | None = None) -> None:
    color = AQI_COLORS[level] if color is None else color
    # These emitted-light weights are deliberately stronger than desktop
    # preview colors.  At gamma 2.2 / brightness 45 the old 42% body could read
    # as two disconnected dark bars; 58% keeps the anatomical silhouette whole
    # while the full-color breath remains clearly brighter.
    body = _dim(color, 0.58)
    highlight = color

    for x, y in LEFT_LUNG | RIGHT_LUNG:
        put(frame, x, y, body)

    left_highlight = LEFT_HIGHLIGHTS[phase % len(LEFT_HIGHLIGHTS)]
    for x, y in left_highlight:
        put(frame, x, y, highlight)
        put(frame, 7 - x, y, highlight)

    # The bright Y-shaped airway is a stable anchor through the breath cycle.
    for x, y in AIRWAY:
        put(frame, x, y, ICE)
    put(frame, 2, 2, PEARL)
    put(frame, 5, 2, PEARL)


def panel_frame(
    aqi: int | float,
    phase: int = 0,
) -> Frame:
    frame = new_frame()
    level = category_index(aqi)
    text = display_text(aqi)
    width = text_width(text)
    if width > LANE_WIDTH:
        raise ValueError(f"AQI text {text!r} is {width}px wide; lane is {LANE_WIDTH}px")

    draw_lungs_jewel(frame, level, phase)
    x = LANE_X0 + (LANE_WIDTH - width) // 2
    draw_text(frame, text, x, 1, AQI_COLORS[level], PEARL)
    return frame


def validate(frames: Iterable[Frame]) -> None:
    count = 0
    expected_silhouette = LEFT_LUNG | RIGHT_LUNG | AIRWAY
    for frame in frames:
        assert len(frame) == HEIGHT
        assert all(len(row) == WIDTH for row in frame)
        assert all(frame[y][GUTTER_X] == BLACK for y in range(HEIGHT))
        occupied = {
            (x, y)
            for y in range(HEIGHT)
            for x in range(ICON_X0, ICON_X1 + 1)
            if frame[y][x] != BLACK
        }
        assert occupied == expected_silhouette
        assert all(
            frame[y][x] == BLACK
            for y in (0, 6, 7)
            for x in range(LANE_X0, LANE_X1 + 1)
        )
        count += 1
    if count == 0:
        raise AssertionError("no AQI frames rendered")


@dataclass(frozen=True)
class RenderConfig:
    output: Path
    scale: int = 18
    gap: int = 1
    gamma: float = PANEL_GAMMA
    brightness: int = PANEL_BRIGHTNESS
    saturation: int = PANEL_SATURATION
    frame_ms: int = 600


def render(config: RenderConfig) -> list[Path]:
    config.output.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []

    source_animation = [
        panel_frame(82, phase)
        for phase in range(4)
    ]
    validate(source_animation)
    animation = [
        grade_frame(frame, config.gamma, config.brightness, config.saturation)
        for frame in source_animation
    ]

    raw_path = config.output / "aqi_brief_jewelbox_32x8.gif"
    raw = [raw_image(frame) for frame in animation]
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

    led_path = config.output / "aqi_brief_jewelbox_led.gif"
    led = [led_image(frame, config.scale, config.gap) for frame in animation]
    led[0].save(
        led_path,
        save_all=True,
        append_images=led[1:],
        duration=config.frame_ms,
        loop=0,
        disposal=2,
        optimize=False,
    )
    paths.append(led_path)

    motion_path = config.output / "aqi_brief_breath_keyframes.png"
    storyboard(animation, config.scale, config.gap).save(motion_path)
    paths.append(motion_path)

    category_frames = [
        panel_frame(value, 2)
        for value in (32, 82, 126, 178, 245, 360)
    ]
    validate(category_frames)
    category_graded = [
        grade_frame(frame, config.gamma, config.brightness, config.saturation)
        for frame in category_frames
    ]
    categories_path = config.output / "aqi_brief_categories.png"
    storyboard(category_graded, config.scale, config.gap).save(categories_path)
    paths.append(categories_path)

    source_path = config.output / "aqi_brief_categories_source.png"
    storyboard(category_frames, config.scale, config.gap).save(source_path)
    paths.append(source_path)
    return paths


def parse_args() -> RenderConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path.home() / ".codex" / "visualizations" / "aqi_brief_jewelbox",
    )
    parser.add_argument("--scale", type=int, default=18)
    parser.add_argument("--gap", type=int, default=1)
    parser.add_argument("--gamma", type=float, default=PANEL_GAMMA)
    parser.add_argument("--brightness", type=int, default=PANEL_BRIGHTNESS)
    parser.add_argument("--saturation", type=int, default=PANEL_SATURATION)
    parser.add_argument("--frame-ms", type=int, default=600)
    args = parser.parse_args()
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
        f"validated four exact {WIDTH}x{HEIGHT} AQI frames; "
        f"gamma={config.gamma:g}, brightness={config.brightness}, "
        f"saturation={config.saturation}"
    )
    for path in paths:
        print(path)


if __name__ == "__main__":
    main()
