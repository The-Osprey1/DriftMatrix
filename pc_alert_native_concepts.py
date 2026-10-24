"""Render three literal 32x8 PC-alert design directions.

These are design previews only.  They do not push anything to the panel or
change the running PC-stats service.  Every source frame is exactly 32 by 8.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image

from pc_stats_preview import (
    BLACK,
    CORAL,
    FONT_3X5,
    GOLD,
    PEARL,
    VIOLET,
    Frame,
    grade_frame,
    led_image,
    new_frame,
    put,
    raw_image,
)


WIDTH = 32
HEIGHT = 8


FONT_3X3: dict[str, tuple[str, ...]] = {
    "0": ("111", "101", "111"),
    "1": ("010", "110", "010"),
    # Was ("110", "011", "110") -- byte-identical to "3" below, in a font
    # whose only job is rendering two-digit values, so 23, 22 and 33 all drew
    # the same pixels.  A centred waist separates it from the 3's right-hand
    # bumps by three pixels.
    "2": ("111", "010", "111"),
    "3": ("110", "011", "110"),
    "4": ("101", "111", "001"),
    "5": ("111", "110", "011"),
    "6": ("011", "110", "111"),
    "7": ("111", "001", "010"),
    "8": ("111", "111", "111"),
    "9": ("111", "111", "001"),
    "C": ("111", "100", "111"),
    "R": ("110", "111", "101"),
}


def _paint_glyph(
    frame: Frame,
    glyph: tuple[str, ...],
    x0: int,
    y0: int,
    color: tuple[int, int, int],
) -> None:
    for y, row in enumerate(glyph):
        for x, bit in enumerate(row):
            if bit == "1":
                put(frame, x0 + x, y0 + y, color)


def _combined_text(frame: Frame) -> None:
    text = "C87R93"
    x = 9
    for char in text:
        color = CORAL if char == "C" else VIOLET if char == "R" else PEARL
        _paint_glyph(frame, FONT_3X5[char], x, 1, color)
        x += 4
    assert x - 1 == WIDTH


def _line(frame: Frame, points: tuple[tuple[int, int], ...], color: tuple[int, int, int]) -> None:
    for x, y in points:
        put(frame, x, y, color)


def twin_dials(phase: int) -> Frame:
    """One jewel-like dial with two truthful needles that settle once."""
    frame = new_frame()

    # A single stable gold bezel is quieter and reads more clearly than the
    # earlier pair of cramped 8x4 housings.
    bezel = (
        (2, 0), (3, 0), (4, 0), (5, 0),
        (1, 1), (6, 1), (0, 2), (7, 2), (0, 3), (7, 3),
        (0, 4), (7, 4), (0, 5), (7, 5), (1, 6), (6, 6),
        (2, 7), (3, 7), (4, 7), (5, 7),
    )
    _line(frame, bezel, GOLD)
    _line(frame, ((2, 1), (1, 3), (1, 5)), CORAL)
    _line(frame, ((5, 1), (6, 3), (6, 5)), VIOLET)

    cpu_needles = (
        ((3, 3), (2, 4), (1, 5)),
        ((3, 3), (2, 3), (1, 4)),
        ((3, 3), (2, 2), (1, 2)),
        ((3, 3), (3, 2), (3, 1)),
        ((3, 3), (4, 2), (5, 1)),
        ((3, 3), (4, 2), (5, 2), (6, 1)),
    )
    ram_needles = (
        ((4, 4), (3, 5), (2, 6)),
        ((4, 4), (4, 5), (4, 6)),
        ((4, 4), (5, 5), (6, 5)),
        ((4, 4), (5, 4), (6, 4)),
        ((4, 4), (5, 3), (6, 3)),
        ((4, 4), (5, 3), (6, 2)),
    )
    cpu_pose = min(3, phase)  # CPU 87% is elevated, below the 90% band.
    ram_pose = min(4, phase)  # RAM 93% is in the high band.
    _line(frame, cpu_needles[cpu_pose], CORAL)
    _line(frame, ram_needles[ram_pose], VIOLET)
    _line(frame, ((3, 3), (4, 3), (3, 4), (4, 4)), PEARL)

    _combined_text(frame)
    for x in (10, 14, 18, 22, 26):
        put(frame, x, 7, GOLD)
    put(frame, 30, 7, CORAL)
    return frame


def pressure_core(phase: int) -> Frame:
    """A connected bimetal reactor expands collectively, never spins."""
    frame = new_frame()
    shell = (
        (3, 0), (4, 0), (2, 1), (5, 1), (1, 2), (6, 2),
        (0, 3), (7, 3), (0, 4), (7, 4), (1, 5), (6, 5),
        (2, 6), (5, 6), (3, 7), (4, 7),
    )
    _line(frame, shell, GOLD)

    # All poses stay attached to the central 2x2 core.
    _line(frame, ((3, 3), (3, 4)), CORAL)
    _line(frame, ((4, 3), (4, 4)), VIOLET)
    if phase >= 1:
        _line(frame, ((3, 2), (2, 3), (2, 4), (3, 5)), CORAL)
        _line(frame, ((4, 2), (5, 3), (5, 4), (4, 5)), VIOLET)
    if phase >= 2:
        _line(frame, ((3, 1), (1, 3), (1, 4), (3, 6)), CORAL)
        _line(frame, ((4, 1), (6, 3), (6, 4), (4, 6)), VIOLET)
    put(frame, 3, 3, PEARL)
    put(frame, 4, 4, PEARL)

    # Value-first typography makes this direction distinct from the labeled
    # dial.  Color and ordering carry CPU (coral) then RAM (violet).
    value = "87|93"
    glyphs: dict[str, tuple[str, ...]] = {**FONT_3X5, "|": ("1", "1", "1", "1", "1")}
    x = 12
    for index, char in enumerate(value):
        color = CORAL if index < 2 else VIOLET if index > 2 else PEARL
        _paint_glyph(frame, glyphs[char], x, 1, color)
        x += len(glyphs[char][0]) + (1 if index < len(value) - 1 else 0)

    for x, color in ((10, CORAL), (13, CORAL), (16, CORAL), (24, VIOLET), (27, VIOLET), (30, VIOLET)):
        put(frame, x, 7, color)
    return frame


def _small_value(
    frame: Frame,
    label: str,
    value: str,
    y0: int,
    color: tuple[int, int, int],
    x0: int = 9,
) -> None:
    _paint_glyph(frame, FONT_3X3[label], x0, y0, color)
    _paint_glyph(frame, FONT_3X3[value[0]], x0 + 4, y0, PEARL)
    _paint_glyph(frame, FONT_3X3[value[1]], x0 + 8, y0, PEARL)


def dual_lanes(phase: int) -> Frame:
    """Two recent-history sparklines plus compact current-value instruments."""
    frame = new_frame()
    _small_value(frame, "C", "87", 0, CORAL)
    _small_value(frame, "R", "93", 4, VIOLET)

    # The icon well is useful rather than decorative: eight recent samples for
    # each metric.  A new sample shifts the graph by exactly one LED column.
    cpu_history = (1, 2, 2, 3, 2, 3, 3, 4) if phase == 0 else (2, 2, 3, 2, 3, 3, 4, 4)
    ram_history = (1, 1, 2, 2, 2, 3, 3, 4) if phase == 0 else (1, 2, 2, 2, 3, 3, 4, 4)
    for x, height in enumerate(cpu_history):
        for y in range(4 - height, 4):
            put(frame, x, y, PEARL if x == 7 else CORAL)
    for x, height in enumerate(ram_history):
        for y in range(8 - height, 8):
            put(frame, x, y, PEARL if x == 7 else VIOLET)

    # Five threshold jewels summarize the current value without becoming a
    # full-width generic bar: 87%=four, 93%=five.
    _line(frame, ((21, 0), (21, 1), (21, 2), (21, 4), (21, 5), (21, 6)), GOLD)
    ticks = (23, 25, 27, 29, 31)
    for index, x in enumerate(ticks):
        if index < 4:
            color = PEARL if index == 3 else CORAL
            put(frame, x, 1, color)
            put(frame, x, 2, color)
        color = PEARL if index == 4 else VIOLET
        put(frame, x, 5, color)
        put(frame, x, 6, color)
    return frame


def _timeline_beads(
    frame: Frame,
    y: int,
    values: tuple[int, ...],
    threshold: int,
    metric_color: tuple[int, int, int],
) -> None:
    """Draw eight two-pixel recent-history beads across the 23px lane."""
    if len(values) != 8:
        raise ValueError("timeline needs exactly eight samples")
    for index, value in enumerate(values):
        color = GOLD if value < threshold else metric_color
        if index == len(values) - 1:
            color = PEARL
        x = 9 + index * 3
        put(frame, x, y, color)
        put(frame, x + 1, y, color)


def composite_target(phase: int) -> Frame:
    """Selected hybrid: jeweled dial, labeled values, and useful history."""
    frame = new_frame()
    bezel = (
        (2, 0), (3, 0), (4, 0), (5, 0),
        (1, 1), (6, 1), (0, 2), (7, 2), (0, 3), (7, 3),
        (0, 4), (7, 4), (0, 5), (7, 5), (1, 6), (6, 6),
        (2, 7), (3, 7), (4, 7), (5, 7),
    )
    _line(frame, bezel, GOLD)
    _line(frame, ((2, 1), (1, 3), (1, 5)), CORAL)
    _line(frame, ((5, 1), (6, 3), (6, 5)), VIOLET)

    cpu_needles = (
        ((3, 3), (2, 4), (1, 5)),
        ((3, 3), (2, 3), (1, 4)),
        ((3, 3), (2, 2), (1, 2)),
        ((3, 3), (3, 2), (3, 1)),
        ((3, 3), (4, 2), (5, 1)),
        ((3, 3), (4, 2), (5, 2), (6, 1)),
    )
    ram_needles = (
        ((4, 4), (3, 5), (2, 6)),
        ((4, 4), (4, 5), (4, 6)),
        ((4, 4), (5, 5), (6, 5)),
        ((4, 4), (5, 4), (6, 4)),
        ((4, 4), (5, 3), (6, 3)),
        ((4, 4), (5, 3), (6, 2)),
    )
    cpu_pose = min(3, phase)
    ram_pose = min(4, phase)
    _line(frame, cpu_needles[cpu_pose], CORAL)
    _line(frame, ram_needles[ram_pose], VIOLET)
    _line(frame, ((3, 3), (4, 3), (3, 4), (4, 4)), PEARL)

    _combined_text(frame)
    _timeline_beads(frame, 0, (72, 78, 82, 85, 88, 91, 94, 87), 80, CORAL)
    _timeline_beads(frame, 7, (79, 82, 84, 86, 88, 90, 92, 93), 85, VIOLET)
    return frame


def _validate(frames: list[Frame]) -> None:
    for frame in frames:
        assert len(frame) == HEIGHT
        assert all(len(row) == WIDTH for row in frame)
        assert sum(pixel != BLACK for row in frame for pixel in row) <= WIDTH * HEIGHT


def _save_gif(frames: list[Frame], path: Path, scale: int, gap: int) -> None:
    # Motion advances by one native pose per tick, then the truthful settled
    # state rests long enough to remain the visual anchor.
    durations = [110] * max(0, len(frames) - 1) + [1_800]
    images = [led_image(frame, scale, gap) for frame in frames]
    images[0].save(
        path,
        save_all=True,
        append_images=images[1:],
        duration=durations,
        loop=0,
        disposal=2,
        optimize=False,
    )


def render(output: Path, scale: int = 28, gap: int = 2) -> list[Path]:
    output.mkdir(parents=True, exist_ok=True)
    concepts = {
        "native_twin_dials": [twin_dials(phase) for phase in range(6)],
        "native_pressure_core": [pressure_core(phase) for phase in (0, 1, 2, 1, 0)],
        "native_dual_lanes": [dual_lanes(phase) for phase in (0, 1)],
        "native_composite": [composite_target(phase) for phase in range(6)],
    }
    _validate([frame for frames in concepts.values() for frame in frames])

    paths: list[Path] = []
    for name, frames in concepts.items():
        # Keep exact native frames as a separately inspectable proof artifact.
        raw_path = output / f"{name}_32x8.png"
        raw_image(frames[-1]).save(raw_path)
        paths.append(raw_path)

        led_path = output / f"{name}_led.gif"
        _save_gif(frames, led_path, scale, gap)
        paths.append(led_path)

        # This second artifact predicts the panel's configured gamma 1.8,
        # brightness 45, and saturation 90.  The bright source preview remains
        # easier to judge on an ordinary monitor.
        graded = [grade_frame(frame, gamma=1.8, brightness=45, saturation=90) for frame in frames]
        graded_path = output / f"{name}_graded_led.gif"
        _save_gif(graded, graded_path, scale, gap)
        paths.append(graded_path)
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scale", type=int, default=28)
    parser.add_argument("--gap", type=int, default=2)
    args = parser.parse_args()
    for path in render(args.output.resolve(), args.scale, args.gap):
        print(path)


if __name__ == "__main__":
    main()
