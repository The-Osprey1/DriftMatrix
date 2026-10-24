"""Render deterministic 32x8 moon frames for visual review."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

from test_moon_logic import DISC, arrival_frame


FONT = {
    "%": ("101", "001", "010", "100", "101"),
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("110", "001", "010", "100", "111"),
    "3": ("110", "001", "010", "001", "110"),
    "A": ("010", "101", "111", "101", "101"),
    "E": ("111", "100", "110", "100", "111"),
    "N": ("101", "111", "111", "111", "101"),
    "W": ("101", "101", "111", "111", "101"),
}

SHADE = (92, 94, 112)
MOON = (244, 233, 197)
WANE = (158, 194, 255)
EDGE = (118, 145, 210)
STAR = ((76, 88, 125), (125, 145, 190), (205, 225, 255))


def mask(
    frame: list[list[tuple[int, int, int]]],
    rows: list[int],
    color: tuple[int, int, int],
) -> None:
    for y, bits in enumerate(rows):
        for x in range(8):
            if bits & (1 << (7 - x)):
                frame[y][x] = color


def text_label(
    frame: list[list[tuple[int, int, int]]],
    value: str,
    color: tuple[int, int, int],
) -> None:
    width = len(value) * 4 - 1
    x0 = 9 + max(0, (23 - width) // 2)
    for index, char in enumerate(value):
        for y, row in enumerate(FONT[char], start=1):
            for x, bit in enumerate(row):
                if bit == "1" and x0 + index * 4 + x < 32:
                    frame[y][x0 + index * 4 + x] = color


def make_frame(step: int, label: str, star_levels: tuple[int, int, int, int]):
    frame = [[(0, 0, 0) for _ in range(32)] for _ in range(8)]
    for (x, y), level in zip(((0, 0), (7, 0), (0, 7), (7, 7)), star_levels):
        frame[y][x] = STAR[level]
    mask(frame, DISC, SHADE)
    visible, accent = arrival_frame(7, step)
    mask(frame, visible, MOON)
    mask(frame, accent, EDGE)
    text_label(frame, label, WANE)
    return frame


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", nargs="?", default="moon-preview.png")
    parser.add_argument("--scale", type=int, default=20)
    args = parser.parse_args()

    frames = [
        make_frame(0, "WANE", (0, 1, 2, 1)),
        make_frame(1, "WANE", (1, 2, 1, 0)),
        make_frame(3, "WANE", (2, 1, 0, 1)),
        make_frame(3, "23%", (1, 0, 1, 2)),
    ]
    scale = max(4, args.scale)
    gutter = scale
    width = len(frames) * 32 * scale + (len(frames) - 1) * gutter
    image = Image.new("RGB", (width, 8 * scale), (7, 8, 10))
    draw = ImageDraw.Draw(image)
    gap = max(1, scale // 10)
    for frame_index, frame in enumerate(frames):
        offset = frame_index * (32 * scale + gutter)
        for y, row in enumerate(frame):
            for x, color in enumerate(row):
                draw.rectangle(
                    (
                        offset + x * scale + gap,
                        y * scale + gap,
                        offset + (x + 1) * scale - gap - 1,
                        (y + 1) * scale - gap - 1,
                    ),
                    fill=color,
                )

    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output)
    print(output)


if __name__ == "__main__":
    main()
