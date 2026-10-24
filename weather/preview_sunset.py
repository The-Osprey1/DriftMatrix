"""Render deterministic 32x8 Sunset frames for visual review.

The panel is the only real renderer, so this mirrors `sunset.ax` v3 exactly and
draws in the same order: sky, clouds, stars, sun, ground, label.

The one approximation is the typeface.  The firmware font is proportional; this
uses the fixed 3x5 cell on a 4px advance that `preview_moon.py` established, so
glyph shapes are indicative and centring is within a pixel.

    python preview_sunset.py --out figures
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image

from test_sunset_logic import (
    BASE,
    DECKS,
    LEAD,
    anchors,
    band,
    dusk_offset,
    label_for,
    mix,
    quality,
    ragged,
    stop,
    sun_pos,
    text_alpha,
)


Frame = list[list[tuple[int, int, int]]]

FONT = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
    ":": ("000", "010", "000", "010", "000"),
    "A": ("010", "101", "111", "101", "101"),
    "C": ("111", "100", "100", "100", "111"),
    "D": ("110", "101", "101", "101", "110"),
    "E": ("111", "100", "111", "100", "111"),
    "F": ("111", "100", "111", "100", "100"),
    "G": ("111", "100", "101", "101", "111"),
    "I": ("111", "010", "010", "010", "111"),
    "K": ("101", "101", "110", "101", "101"),
    "L": ("100", "100", "100", "100", "111"),
    "M": ("101", "111", "111", "101", "101"),
    # Rounded, so the letter O is not byte-identical to the digit 0.
    # It was, in every font in this repo.  Rounding the letter rather
    # than altering the digit also dissolves O~8, O~G and O~U, which
    # were each a single pixel apart, and costs no width.
    "O": ("010", "101", "101", "101", "010"),
    "P": ("111", "101", "111", "100", "100"),
    "R": ("111", "101", "110", "101", "101"),
    # Rounded, so the letter S is not byte-identical to the digit 5.
    # It was.  This also separates S from 6 and 9, which were each a
    # single pixel away, and costs no width.
    "S": ("011", "100", "010", "001", "110"),
    "T": ("111", "010", "010", "010", "010"),
    "U": ("101", "101", "101", "101", "111"),
}


def blank() -> Frame:
    return [[(0, 0, 0) for _ in range(32)] for _ in range(8)]


def put(frame: Frame, x: int, y: int, color) -> None:
    if 0 <= x < 32 and 0 <= y < 8:
        frame[y][x] = color


def hline(frame: Frame, x0: int, x1: int, y: int, color) -> None:
    for x in range(max(0, x0), min(31, x1) + 1):
        put(frame, x, y, color)


def ink_width(s: str) -> int:
    return max(0, len(s) * 4 - 1)


def draw_text(frame: Frame, s: str, tint, alpha: int) -> None:
    x0 = 31 - ink_width(s)
    if x0 < 12:
        x0 = 12
    shadow = (
        int(8 * alpha / 100),
        int(4 * alpha / 100),
        int(10 * alpha / 100),
    )
    faded = tuple(int(c * alpha / 100) for c in tint)
    for dx_off, dy_off, color in ((1, 1, shadow), (0, 0, faded)):
        for index, char in enumerate(s):
            rows = FONT.get(char)
            if rows is None:
                continue
            for dy, bits in enumerate(rows):
                for dx, bit in enumerate(bits):
                    if bit == "1":
                        put(frame, x0 + index * 4 + dx + dx_off, 1 + dy + dy_off, color)


def render(t: int, low: int, mid_c: int, high: int, dn: int = 1171,
           daylen: int = 795, ms: int = 0, view_ms: int = 6500) -> Frame:
    """One exact frame, mirroring Sunset.draw()."""

    q = quality(low, mid_c, high) if min(low, mid_c, high) >= 0 else -1
    dusk = dusk_offset(daylen)
    top, mid, hor = anchors(t, q)
    frame = blank()

    # sky: base row plus a checkerboard of the next tone
    for y in range(7):
        w = y * 25 if y <= 4 else (y - 4) * 50
        lo = max(0, w - 7)
        hi = min(100, w + 7)
        hline(frame, 0, 31, y, stop(top, mid, hor, y, lo))
        c = stop(top, mid, hor, y, hi)
        for x in range(y % 2, 32, 2):
            put(frame, x, y, c)

    cx, cy = sun_pos(t)

    # clouds: dark bodies, fire-lit undersides, counter-drifting, lit by
    # distance from the sun, draining to silhouette after sunset
    if high >= 20:
        n = 1 if high < 55 else 2
        burn = max(0, 100 - t * 4) if t > 0 else 100
        for d in range(n):
            o = d * 4
            base, width, div, off = DECKS[o], DECKS[o + 1], DECKS[o + 2], DECKS[o + 3]
            step = int(ms / div) + off
            x0 = (step % 46) - 8 if d == 0 else 38 - (step % 46)
            x1 = x0 + width
            for x in range(x0, x1 + 1):
                if not (0 <= x < 32):
                    continue
                e = min(x - x0, x1 - x)
                k = 72 if e >= 2 else (40 if e == 1 else 18)
                row = min(5, base + (ragged(x + d * 5) if e >= 1 else 0))
                w = row * 25 if row <= 4 else (row - 4) * 50
                put(frame, x, row, (
                    mix(band(top, mid, hor, row, w, 0), 56, k),
                    mix(band(top, mid, hor, row, w, 1), 42, k),
                    mix(band(top, mid, hor, row, w, 2), 62, k)))
                r2 = row + 1
                if r2 < 7:
                    lit = max(45, 100 - abs(x - cx) * 4)
                    w2 = r2 * 25 if r2 <= 4 else (r2 - 4) * 50
                    k2 = int(k * 90 * lit * burn / 1000000)
                    put(frame, x, r2, (
                        mix(band(top, mid, hor, r2, w2, 0), 255, k2),
                        mix(band(top, mid, hor, r2, w2, 1), 198, k2),
                        mix(band(top, mid, hor, r2, w2, 2), 124, k2)))

    # sun, flattened by refraction as it drops
    sq = min(150, 100 if cy <= 3 else 100 + (cy - 3) * 16)
    for y in range(max(0, cy - 2), min(6, cy + 2) + 1):
        w = y * 25 if y <= 4 else (y - 4) * 50
        br = band(top, mid, hor, y, w, 0)
        bg = band(top, mid, hor, y, w, 1)
        bb = band(top, mid, hor, y, w, 2)
        dy = (y - cy) * sq / 100
        ey = int(dy * dy * 100 / 4)
        for x in range(max(0, cx - 4), min(31, cx + 4) + 1):
            dx = x - cx
            e = int(dx * dx * 100 / 13) + ey
            if e >= 100:
                continue
            g = min(100, int((100 - e) ** 2 * 200 / 10000))
            if g > 0:
                put(frame, x, y, (mix(br, 255, g), mix(bg, 249, g), mix(bb, 214, g)))

    # ground, doubling as a progress bar
    lr, lg, lb = int(hor[0] / 11) + 2, int(hor[1] / 12) + 2, int(hor[2] / 8) + 5
    hline(frame, 0, 31, 7, (lr, lg, lb))
    if t < 0:
        p = min(31, int((LEAD + t) * 31 / LEAD))
        if p >= 0:
            hline(frame, 0, p, 7, (mix(lr, 255, 15), mix(lg, 150, 15), mix(lb, 90, 15)))

    alpha = text_alpha(view_ms)
    if alpha > 0:
        s, tint = label_for(t, dusk, dn, q, ms)
        if s:
            draw_text(frame, s, tint, alpha)
    return frame


def to_image(frame: Frame, scale: int) -> Image.Image:
    img = Image.new("RGB", (32, 8))
    img.putdata([px for row in frame for px in row])
    return img.resize((32 * scale, 8 * scale), Image.NEAREST)


def stack(frames: list[Frame], scale: int, gap: int = 6) -> Image.Image:
    tiles = [to_image(f, scale) for f in frames]
    w = max(t.width for t in tiles)
    h = sum(t.height for t in tiles) + gap * (len(tiles) - 1)
    sheet = Image.new("RGB", (w, h), (16, 16, 18))
    y = 0
    for tile in tiles:
        sheet.paste(tile, (0, y))
        y += tile.height + gap
    return sheet


SCENARIOS = {
    "epic": (0, 10, 50),
    "clear": (0, 0, 0),
    "blocked": (85, 40, 55),
}

MOMENTS = [-75, -50, -30, -15, -5, 0, 8, 18, 26]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="figures", type=Path)
    ap.add_argument("--scale", default=12, type=int)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    for name, (low, mid_c, high) in SCENARIOS.items():
        # view_ms=2000 -> the sky-only half of the cycle
        frames = [render(t, low, mid_c, high, ms=3000, view_ms=2000) for t in MOMENTS]
        path = args.out / f"sunset_{name}.png"
        stack(frames, args.scale).save(path)
        print(f"{path}  q={quality(low, mid_c, high)}")

    # one full 9s appearance, so the readout choreography is visible
    view = [render(-30, 0, 10, 50, ms=3000 + i * 780, view_ms=i * 780) for i in range(12)]
    stack(view, args.scale).save(args.out / "sunset_view.png")
    print(args.out / "sunset_view.png")

    gif: list[Image.Image] = []
    for t in range(-LEAD, 27):
        for sub in range(3):
            gif.append(to_image(
                render(t, 0, 10, 50, ms=t * 900 + sub * 300, view_ms=(sub * 3000)), 8))
    gif[0].save(args.out / "sunset_descent.gif", save_all=True,
                append_images=gif[1:], duration=90, loop=0)
    print(args.out / "sunset_descent.gif")


if __name__ == "__main__":
    main()
