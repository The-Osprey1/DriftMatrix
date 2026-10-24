"""Offline A/B study for Now Playing text motion.

This script never contacts the AWTRIX panel.  It reuses the exact record,
progress rail, proportional 3x5 font, and screen-space edge colours from
``preview_now_playing_berry`` while comparing superseded wall-clock treatments
with production's one-physical-pixel-per-three-completed-frames motion.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw

import preview_now_playing_berry as preview


FRAME_MS = 25
BACKGROUND = (12, 13, 18)
LABEL = (225, 229, 237)
RECOMMENDED = (255, 183, 47)


@dataclass(frozen=True)
class Treatment:
    label: str
    mode: str
    step_ms: int = 100
    recommended: bool = False


TREATMENTS = (
    Treatment("OLD: FRACTIONAL FADE", "crossfade"),
    Treatment("OLD STEP: 80 MS", "step", 80),
    Treatment("OLD STEP: 100 MS", "step", 100),
    Treatment("FRAME LOCK: 3 DRAWS", "frame", recommended=True),
)


def _matrix(title: str, stamp: int, progress: int, treatment: Treatment) -> Image.Image:
    completed = max(0, stamp - preview.SCROLL_HOLD_MS) // FRAME_MS
    return preview.render_matrix(
        title,
        stamp,
        progress,
        completed_scroll_frames=completed,
        scroll_mode=treatment.mode,
        step_ms=treatment.step_ms,
    )


def _panel(
    title: str,
    stamp: int,
    progress: int,
    treatment: Treatment,
    scale: int,
    *,
    grid: bool = False,
) -> Image.Image:
    return preview.pixel_scale(_matrix(title, stamp, progress, treatment), scale, grid=grid)


def _tile(
    title: str,
    stamp: int,
    progress: int,
    treatment: Treatment,
    scale: int,
) -> Image.Image:
    panel = _panel(title, stamp, progress, treatment, scale)
    label_h = 21
    tile = Image.new("RGB", (panel.width, label_h + panel.height), BACKGROUND)
    draw = ImageDraw.Draw(tile)
    color = RECOMMENDED if treatment.recommended else LABEL
    draw.text((4, 5), treatment.label, fill=color)
    if treatment.recommended:
        draw.text((panel.width - 76, 5), "BEST BALANCE", fill=color)
    tile.paste(panel, (0, label_h))
    return tile


def _save_animation(frames: list[Image.Image], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    durations = [20 if index % 2 == 0 else 30 for index in range(len(frames))]
    frames[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=1,
    )
    return output


def save_ab_gif(title: str, output: Path, progress: int, scale: int, duration_ms: int) -> Path:
    current = TREATMENTS[0]
    recommended = TREATMENTS[3]
    gap = 14
    frames: list[Image.Image] = []
    for stamp in range(0, duration_ms, FRAME_MS):
        left = _tile(title, stamp, progress, current, scale)
        right = _tile(title, stamp, progress, recommended, scale)
        canvas = Image.new("RGB", (left.width + gap + right.width, left.height), BACKGROUND)
        canvas.paste(left, (0, 0))
        canvas.paste(right, (left.width + gap, 0))
        frames.append(canvas)
    return _save_animation(frames, output)


def save_all_gif(title: str, output: Path, progress: int, scale: int, duration_ms: int) -> Path:
    gap = 14
    frames: list[Image.Image] = []
    for stamp in range(0, duration_ms, FRAME_MS):
        tiles = [_tile(title, stamp, progress, item, scale) for item in TREATMENTS]
        tile_w, tile_h = tiles[0].size
        canvas = Image.new("RGB", (tile_w * 2 + gap, tile_h * 2 + gap), BACKGROUND)
        for index, tile in enumerate(tiles):
            canvas.paste(tile, ((index % 2) * (tile_w + gap), (index // 2) * (tile_h + gap)))
        frames.append(canvas)
    return _save_animation(frames, output)


def save_contact_sheet(title: str, output: Path, progress: int, scale: int) -> Path:
    # These samples expose what the eye sees between physical-pixel advances:
    # the current treatment continually changes brightness, while a crisp step
    # holds a stable glyph for its complete cadence.
    stamps = tuple(range(1000, 1226, 25))
    frame_w = preview.WIDTH * scale
    frame_h = preview.HEIGHT * scale
    left_label_w = 168
    top_label_h = 24
    gap = 5
    width = left_label_w + len(stamps) * (frame_w + gap) + gap
    height = top_label_h + len(TREATMENTS) * (frame_h + gap) + gap
    sheet = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(sheet)
    for column, stamp in enumerate(stamps):
        x = left_label_w + column * (frame_w + gap)
        draw.text((x + 2, 6), f"{stamp - 1000:03d}", fill=(160, 166, 178))
    for row, treatment in enumerate(TREATMENTS):
        y = top_label_h + row * (frame_h + gap)
        color = RECOMMENDED if treatment.recommended else LABEL
        draw.text((7, y + frame_h // 2 - 5), treatment.label, fill=color)
        for column, stamp in enumerate(stamps):
            x = left_label_w + column * (frame_w + gap)
            sheet.paste(_panel(title, stamp, progress, treatment, scale, grid=True), (x, y))
    draw.text((7, 6), "MS AFTER HOLD", fill=(160, 166, 178))
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output)
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", default="MIDNIGHT CITY - M83")
    parser.add_argument("--progress", type=int, default=14)
    parser.add_argument("--scale", type=int, default=10)
    parser.add_argument("--duration-ms", type=int, default=9000)
    parser.add_argument("--output-dir", type=Path, default=Path("scroll_cadence_study"))
    args = parser.parse_args()
    title = args.title.upper()
    outputs = (
        save_ab_gif(
            title,
            args.output_dir / "scroll_ab_current_vs_100ms.gif",
            args.progress,
            args.scale,
            args.duration_ms,
        ),
        save_all_gif(
            title,
            args.output_dir / "scroll_cadence_all.gif",
            args.progress,
            max(6, args.scale - 2),
            args.duration_ms,
        ),
        save_contact_sheet(
            title,
            args.output_dir / "scroll_cadence_contact_sheet.png",
            args.progress,
            4,
        ),
    )
    for output in outputs:
        print(output.resolve())


if __name__ == "__main__":
    main()
