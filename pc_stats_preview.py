"""Render the selected Jewelbox PC Alert at exact 32x8 resolution.

A gold 8x8 dial carries coral CPU and violet RAM needles.  The right lane keeps
the value perfectly still and uses connected two-pixel beads for recent sample
history.  Only the dial moves; column eight remains a black safety gutter.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import math
from pathlib import Path
from typing import Iterable

from PIL import Image

from weather.weather_brief_preview import (
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

ICON_X0 = 0
ICON_X1 = 7
GUTTER_X = 8
LANE_X0 = 9
LANE_X1 = 31
LANE_WIDTH = 23
CPU_SHOW_AT = 80
RAM_SHOW_AT = 85

PEARL: RGB = (255, 247, 232)
ICE: RGB = (196, 239, 255)
MINT: RGB = (72, 245, 160)
GOLD: RGB = (255, 213, 78)
AMBER: RGB = (255, 145, 45)
CORAL: RGB = (255, 76, 91)
VIOLET: RGB = (194, 97, 255)
EMERALD: RGB = (0, 230, 118)
TRACK: RGB = (107, 119, 145)

GPU_SHOW_AT = 80
NET_SHOW_AT = 10

METRIC_COLORS: dict[str, RGB] = {
    "CPU": CORAL,
    "RAM": VIOLET,
    "GPU": EMERALD,
    "NET": ICE,
}


FONT_3X5: dict[str, tuple[str, ...]] = {
    "%": ("101", "001", "010", "100", "101"),
    "/": ("001", "001", "010", "100", "100"),
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
    "E": ("111", "100", "111", "100", "111"),
    "G": ("111", "100", "101", "101", "111"),
    "K": ("101", "110", "100", "110", "101"),
    "M": ("101", "111", "111", "101", "101"),
    "N": ("101", "111", "111", "111", "101"),
    "P": ("110", "101", "110", "100", "100"),
    "R": ("110", "101", "110", "101", "101"),
    "S": ("011", "100", "010", "001", "110"),
    "T": ("111", "010", "010", "010", "010"),
    "U": ("101", "101", "101", "101", "111"),
}


DIAL_BEZEL = (
    (2, 0), (3, 0), (4, 0), (5, 0),
    (1, 1), (6, 1), (0, 2), (7, 2), (0, 3), (7, 3),
    (0, 4), (7, 4), (0, 5), (7, 5), (1, 6), (6, 6),
    (2, 7), (3, 7), (4, 7), (5, 7),
)
CPU_TICKS = ((2, 1), (1, 3), (1, 5))
RAM_TICKS = ((5, 1), (6, 3), (6, 5))
CPU_NEEDLES = (
    ((3, 3), (2, 4), (1, 5)),
    ((3, 3), (2, 3), (1, 4)),
    ((3, 3), (2, 2), (1, 2)),
    ((3, 3), (3, 2), (3, 1)),
    ((3, 3), (4, 2), (5, 1)),
    ((3, 3), (4, 2), (5, 2), (6, 1)),
)
RAM_NEEDLES = (
    ((4, 4), (3, 5), (2, 6)),
    ((4, 4), (4, 5), (4, 6)),
    ((4, 4), (5, 5), (6, 5)),
    ((4, 4), (5, 4), (6, 4)),
    ((4, 4), (5, 3), (6, 3)),
    ((4, 4), (5, 3), (6, 2)),
)
HISTORY_SAMPLES = 8


def normalize_percent(value: float) -> int:
    return max(0, min(100, int(round(float(value)))))


def text_value(label: str, value: float) -> str:
    label = label.upper()
    if label not in METRIC_COLORS:
        raise ValueError(f"unsupported PC metric: {label}")
    percent = normalize_percent(value)
    # Two digits leave room for an explicit percent sign. At 100, keeping the
    # full metric name is more useful than squeezing in a redundant symbol;
    # the truthful rail still makes the unit unambiguous.
    return f"{label}{percent}%" if percent < 100 else f"{label}100"


def text_width(value: str) -> int:
    glyphs = [FONT_3X5[char] for char in value]
    return sum(len(glyph[0]) for glyph in glyphs) + max(0, len(glyphs) - 1)


def draw_text(frame: Frame, value: str, x0: int, y0: int, metric_color: RGB) -> None:
    x = x0
    for index, char in enumerate(value):
        glyph = FONT_3X5[char]
        color = metric_color if index < 3 else PEARL
        for gy, row in enumerate(glyph):
            for gx, bit in enumerate(row):
                if bit == "1":
                    put(frame, x + gx, y0 + gy, color)
        x += len(glyph[0])
        if index < len(value) - 1:
            x += 1


def _dim(color: RGB, strength: float) -> RGB:
    return gamma_lerp(BLACK, color, strength)


def severity_level(value: float) -> int:
    value = normalize_percent(value)
    if value >= 95:
        return 2
    if value >= 90:
        return 1
    return 0


def _draw_points(frame: Frame, points: Iterable[tuple[int, int]], color: RGB) -> None:
    for x, y in points:
        put(frame, x, y, color)


def _needle_target(value: float) -> int:
    """Map the three alert severity bands to distinct settled dial poses."""
    return (3, 4, 5)[severity_level(value)]


def _dial_pose(phase: int, target: int) -> int:
    return max(0, min(target, int(phase)))


def _draw_dial_shell(frame: Frame, cpu: bool, ram: bool) -> None:
    _draw_points(frame, DIAL_BEZEL, GOLD)
    if cpu:
        _draw_points(frame, CPU_TICKS, CORAL)
    if ram:
        _draw_points(frame, RAM_TICKS, VIOLET)


def draw_cpu_jewel(frame: Frame, value: float, phase: int) -> None:
    _draw_dial_shell(frame, cpu=True, ram=False)
    _draw_points(frame, CPU_NEEDLES[_dial_pose(phase, _needle_target(value))], CORAL)
    _draw_points(frame, ((3, 3), (4, 3), (3, 4), (4, 4)), PEARL)


def draw_ram_jewel(frame: Frame, value: float, phase: int) -> None:
    _draw_dial_shell(frame, cpu=False, ram=True)
    _draw_points(frame, RAM_NEEDLES[_dial_pose(phase, _needle_target(value))], VIOLET)
    _draw_points(frame, ((3, 3), (4, 3), (3, 4), (4, 4)), PEARL)


def draw_gpu_jewel(frame: Frame, value: float, phase: int) -> None:
    _draw_dial_shell(frame, cpu=False, ram=False)
    _draw_points(frame, ((1, 2), (6, 2), (1, 5), (6, 5)), EMERALD)
    _draw_points(frame, CPU_NEEDLES[_dial_pose(phase, _needle_target(value))], EMERALD)
    _draw_points(frame, ((3, 3), (4, 3), (3, 4), (4, 4)), PEARL)


def draw_net_jewel(frame: Frame, value: float, phase: int) -> None:
    _draw_dial_shell(frame, cpu=False, ram=False)
    _draw_points(frame, ((3, 1), (4, 1), (3, 2), (4, 2)), ICE)
    _draw_points(frame, ((2, 3), (3, 3), (4, 3), (5, 3)), ICE)
    _draw_points(frame, ((1, 4), (2, 4), (3, 4), (4, 4), (5, 4), (6, 4)), ICE)
    flow = PEARL if (phase % 2 == 0) else ICE
    _draw_points(frame, ((3, 5), (4, 5)), flow)
    _draw_points(frame, ((3, 6), (4, 6)), PEARL if flow is ICE else ICE)


def draw_combined_jewel(frame: Frame, cpu: float, ram: float, phase: int) -> None:
    _draw_dial_shell(frame, cpu=True, ram=True)
    _draw_points(frame, CPU_NEEDLES[_dial_pose(phase, _needle_target(cpu))], CORAL)
    _draw_points(frame, RAM_NEEDLES[_dial_pose(phase, _needle_target(ram))], VIOLET)
    _draw_points(frame, ((3, 3), (4, 3), (3, 4), (4, 4)), PEARL)


def normalize_history(history: Iterable[float] | None, current: float) -> tuple[int, ...]:
    values: list[int] = []
    if history is not None:
        for value in history:
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(numeric):
                values.append(normalize_percent(numeric))
    if not values or values[-1] != normalize_percent(current):
        values.append(normalize_percent(current))
    return tuple(values[-HISTORY_SAMPLES:])


def draw_history_timeline(
    frame: Frame,
    history: Iterable[float] | None,
    current: float,
    threshold: int,
    metric_color: RGB,
    y: int,
) -> None:
    """Render up to eight recent readings as connected two-pixel beads."""
    values = normalize_history(history, current)
    start = HISTORY_SAMPLES - len(values)
    for offset, value in enumerate(values):
        index = start + offset
        color = metric_color if value >= threshold else GOLD
        if offset == len(values) - 1:
            color = PEARL
        x = LANE_X0 + index * 3
        put(frame, x, y, color)
        put(frame, x + 1, y, color)


COMBINED_MAX_PERCENT = 99


def combined_percent(value: float) -> int:
    """A percentage that always fits the shared two-digit field.

    The two metrics share a 23px lane, which is exactly six 3px glyphs: two
    digits each plus the C and R markers. There is no room for a third digit.

    The previous compaction kept all three digits of a saturated metric and
    dropped the *partner's* units digit, which was the wrong thing to give up:
    at CPU 100 a RAM of 93 printed as "R9" and read as nine percent. Worse, CPU
    100 with RAM 100 produced "C100R10", too wide to draw, and the ValueError
    killed the watcher at the one moment it was most needed.

    Reporting 99 for a saturated metric is the smaller lie by a wide margin. At
    the top of the scale the difference between 99 and 100 changes nothing a
    reader would do about it; a metric wrong by an order of magnitude changes
    everything. The history rail underneath still shows the true trend.
    """
    return min(COMBINED_MAX_PERCENT, normalize_percent(value))


def draw_combined_text(frame: Frame, cpu: float, ram: float) -> None:
    value = f"C{combined_percent(cpu):02d}R{combined_percent(ram):02d}"
    if text_width(value) > LANE_WIDTH:
        raise ValueError(f"combined PC alert text {value!r} is too wide")
    x = LANE_X0 + (LANE_WIDTH - text_width(value)) // 2
    for index, char in enumerate(value):
        glyph = FONT_3X5[char]
        if index == 0:
            color = CORAL
        elif char == "R":
            color = VIOLET
        else:
            color = PEARL
        for gy, row in enumerate(glyph):
            for gx, bit in enumerate(row):
                if bit == "1":
                    put(frame, x + gx, 1 + gy, color)
        x += len(glyph[0]) + (1 if index < len(value) - 1 else 0)


def panel_frame(
    label: str,
    value: float,
    phase: int = 5,
    history: Iterable[float] | None = None,
) -> Frame:
    label = label.upper()
    text = text_value(label, value)
    width = text_width(text)
    if width > LANE_WIDTH:
        raise ValueError(f"PC alert text {text!r} is {width}px wide")

    frame = new_frame()
    if label == "CPU":
        draw_cpu_jewel(frame, value, phase)
    elif label == "RAM":
        draw_ram_jewel(frame, value, phase)
    elif label == "GPU":
        draw_gpu_jewel(frame, value, phase)
    elif label == "NET":
        draw_net_jewel(frame, value, phase)
    else:
        raise ValueError(f"unsupported PC metric: {label}")
    x = LANE_X0 + (LANE_WIDTH - width) // 2
    draw_text(frame, text, x, 1, METRIC_COLORS[label])
    thresh = (
        CPU_SHOW_AT
        if label == "CPU"
        else (
            RAM_SHOW_AT
            if label == "RAM"
            else (GPU_SHOW_AT if label == "GPU" else NET_SHOW_AT)
        )
    )
    draw_history_timeline(
        frame,
        history,
        value,
        thresh,
        METRIC_COLORS[label],
        7,
    )
    return frame


def combined_frame(
    cpu: float,
    ram: float,
    phase: int = 5,
    cpu_history: Iterable[float] | None = None,
    ram_history: Iterable[float] | None = None,
) -> Frame:
    frame = new_frame()
    draw_combined_jewel(frame, cpu, ram, phase)
    draw_combined_text(frame, cpu, ram)
    draw_history_timeline(frame, cpu_history, cpu, CPU_SHOW_AT, CORAL, 0)
    draw_history_timeline(frame, ram_history, ram, RAM_SHOW_AT, VIOLET, 7)
    return frame


def validate(frames: Iterable[Frame]) -> None:
    count = 0
    for frame in frames:
        assert len(frame) == HEIGHT
        assert all(len(row) == WIDTH for row in frame)
        assert all(frame[y][GUTTER_X] == BLACK for y in range(HEIGHT))
        assert any(frame[y][x] != BLACK for y in range(8) for x in range(8))
        count += 1
    if count == 0:
        raise AssertionError("no PC alert frames rendered")


@dataclass(frozen=True)
class RenderConfig:
    output: Path
    scale: int = 18
    gap: int = 1
    gamma: float = 1.8
    brightness: int = 45
    saturation: int = 90
    frame_ms: int = 250


def render(config: RenderConfig) -> list[Path]:
    config.output.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []

    cpu_history = (72, 78, 82, 85, 88, 91, 94, 87)
    ram_history = (79, 82, 84, 86, 88, 90, 92, 93)
    source_cpu = [panel_frame("CPU", 87, phase, cpu_history) for phase in range(6)]
    source_ram = [panel_frame("RAM", 93, phase, ram_history) for phase in range(6)]
    source_both = [
        combined_frame(87, 93, phase, cpu_history, ram_history)
        for phase in range(6)
    ]
    validate(source_cpu + source_ram + source_both)

    cpu = [grade_frame(frame, config.gamma, config.brightness, config.saturation) for frame in source_cpu]
    ram = [grade_frame(frame, config.gamma, config.brightness, config.saturation) for frame in source_ram]
    both = [grade_frame(frame, config.gamma, config.brightness, config.saturation) for frame in source_both]

    for label, frames in (("cpu", cpu), ("ram", ram), ("both", both)):
        raw_path = config.output / f"pc_alert_{label}_32x8.gif"
        raw = [raw_image(frame) for frame in frames]
        raw[0].save(raw_path, save_all=True, append_images=raw[1:], duration=config.frame_ms, loop=0, disposal=2, optimize=False)
        paths.append(raw_path)

        led_path = config.output / f"pc_alert_{label}_led.gif"
        led = [led_image(frame, config.scale, config.gap) for frame in frames]
        led[0].save(led_path, save_all=True, append_images=led[1:], duration=config.frame_ms, loop=0, disposal=2, optimize=False)
        paths.append(led_path)

    contact_path = config.output / "pc_alert_motion_keyframes.png"
    storyboard(
        [cpu[index] for index in (0, 2, 4, 5)]
        + [ram[index] for index in (0, 2, 4, 5)]
        + [both[index] for index in (0, 2, 4, 5)],
        config.scale,
        config.gap,
    ).save(contact_path)
    paths.append(contact_path)

    source_contact_path = config.output / "pc_alert_motion_keyframes_source.png"
    storyboard(
        [source_cpu[index] for index in (0, 2, 4, 5)]
        + [source_ram[index] for index in (0, 2, 4, 5)]
        + [source_both[index] for index in (0, 2, 4, 5)],
        config.scale,
        config.gap,
    ).save(source_contact_path)
    paths.append(source_contact_path)

    states = (
        [panel_frame("CPU", value, 0) for value in (80, 89, 96)]
        + [panel_frame("RAM", value, 0) for value in (85, 92, 98)]
        + [combined_frame(87, 93, 0)]
    )
    state_path = config.output / "pc_alert_states_source.png"
    storyboard(states, config.scale, config.gap).save(state_path)
    paths.append(state_path)

    graded_states = [grade_frame(frame, config.gamma, config.brightness, config.saturation) for frame in states]
    graded_path = config.output / "pc_alert_states_graded.png"
    storyboard(graded_states, config.scale, config.gap).save(graded_path)
    paths.append(graded_path)
    return paths


def parse_args() -> RenderConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path.home() / ".codex" / "visualizations" / "pc_alert_jewelbox",
    )
    parser.add_argument("--scale", type=int, default=18)
    parser.add_argument("--gap", type=int, default=1)
    parser.add_argument("--gamma", type=float, default=1.8)
    parser.add_argument("--brightness", type=int, default=45)
    parser.add_argument("--saturation", type=int, default=90)
    parser.add_argument("--frame-ms", type=int, default=250)
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
        f"validated exact {WIDTH}x{HEIGHT} PC alert frames; "
        f"gamma={config.gamma:g}, brightness={config.brightness}, saturation={config.saturation}"
    )
    for path in paths:
        print(path)


if __name__ == "__main__":
    main()
