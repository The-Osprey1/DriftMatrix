"""Render deterministic 32x8 countdown frames for design review."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw


CAPS = [0xFF, 0, 0, 0, 0, 0, 0, 0xFF]
GLASS = [0, 0x42, 0x24, 0x18, 0x18, 0x24, 0x42, 0]
HIGHLIGHT = [0, 0x40, 0x20, 0x10, 0x10, 0x20, 0x40, 0]
SAND = [
    [0, 0x3C, 0x18, 0, 0, 0, 0, 0],
    [0, 0x18, 0x18, 0x18, 0x18, 0x18, 0x18, 0],
    [0, 0, 0x18, 0x18, 0x18, 0x18, 0x3C, 0],
    [0, 0, 0, 0x18, 0x18, 0x18, 0x3C, 0],
    [0, 0, 0, 0, 0, 0x18, 0x3C, 0],
]
STREAM = [0, 0, 0, 0x18, 0x18, 0x18, 0, 0]

FONT = {
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
    "D": ("110", "101", "101", "101", "110"),
    "E": ("111", "100", "110", "100", "111"),
    "H": ("101", "101", "111", "101", "101"),
    "N": ("101", "111", "111", "111", "101"),
    # Rounded, so the letter O is not byte-identical to the digit 0.
    # It was, in every font in this repo.  Rounding the letter rather
    # than altering the digit also dissolves O~8, O~G and O~U, which
    # were each a single pixel apart, and costs no width.
    "O": ("010", "101", "101", "101", "010"),
    "P": ("110", "101", "110", "100", "100"),
    "T": ("111", "010", "010", "010", "010"),
    "Y": ("101", "101", "010", "010", "010"),
    "d": ("001", "001", "111", "101", "111"),
}


def mask(frame: list[list[tuple[int, int, int]]], rows: list[int], color: tuple[int, int, int]) -> None:
    for y, bits in enumerate(rows):
        for x in range(8):
            if bits & (1 << (7 - x)):
                frame[y][x] = color


def label(frame: list[list[tuple[int, int, int]]], value: str, color: tuple[int, int, int]) -> None:
    width = len(value) * 4 - 1
    x0 = 9 + max(0, (23 - width) // 2)
    for index, char in enumerate(value):
        glyph = FONT[char]
        for y, row in enumerate(glyph, start=1):
            for x, bit in enumerate(row):
                if bit == "1" and x0 + index * 4 + x < 32:
                    frame[y][x0 + index * 4 + x] = color


def make_frame(step: int, value: str, sand: tuple[int, int, int]) -> list[list[tuple[int, int, int]]]:
    frame = [[(0, 0, 0) for _ in range(32)] for _ in range(8)]
    mask(frame, GLASS, (120, 152, 187))
    mask(frame, HIGHLIGHT, (196, 222, 246))
    mask(frame, CAPS, (227, 238, 250))
    mask(frame, SAND[step], sand)
    if 1 <= step <= 3:
        glow = tuple(round(channel + (255 - channel) * 0.3) for channel in sand)
        mask(frame, STREAM, glow)
    ink = (227, 238, 250) if value == "PHD" else sand
    label(frame, value, ink)
    return frame


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", nargs="?", default="countdown-preview.png")
    parser.add_argument("--scale", type=int, default=20)
    args = parser.parse_args()

    frames = [
        make_frame(0, "PHD", (73, 221, 166)),
        make_frame(2, "PHD", (73, 221, 166)),
        make_frame(4, "115d", (73, 221, 166)),
        make_frame(4, "TODAY", (255, 214, 90)),
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
