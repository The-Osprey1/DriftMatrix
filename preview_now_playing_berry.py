"""Render the device-side Now Playing Berry artwork without touching the panel.

This mirrors ``now_playing.ax`` deliberately, including its proportional 3x5
font, screen-space edge colours, frame-locked one-pixel steps, record frames,
and truthful 24-cell progress rail.  It is a visual QA helper, not runtime code.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

import now_playing_push as legacy


WIDTH = 32
HEIGHT = 8
TEXT_COLOR = (255, 244, 234)
MID_TEXT_COLOR = (178, 171, 164)
OUTER_TEXT_COLOR = (89, 85, 82)
SCROLL_HOLD_MS = 1_000
SCROLL_FRAMES_PER_PIXEL = 3
NOMINAL_FRAME_MS = 25
ROTATION_WATCHDOG_MS = 120_000


def _rgb(hex_value: int) -> tuple[int, int, int]:
    return ((hex_value >> 16) & 255, (hex_value >> 8) & 255, hex_value & 255)


def _put(pixels: list[list[tuple[int, int, int]]], x: int, y: int, color: tuple[int, int, int]) -> None:
    if 0 <= x < WIDTH and 0 <= y < HEIGHT:
        pixels[y][x] = color


def glyph_columns(char: str) -> tuple[int, ...]:
    """Return one glyph as top-to-bottom five-bit vertical column masks."""

    rows = legacy.FONT.get(char, legacy.FONT["?"])
    width = len(rows[0])
    if any(len(row) != width for row in rows):
        raise ValueError(f"inconsistent glyph width for {char!r}")
    return tuple(
        sum((row[column] == "1") << (4 - row_index) for row_index, row in enumerate(rows))
        for column in range(width)
    )


def text_columns(title: str) -> tuple[int, ...]:
    """Precompute proportional glyph columns with one separator per pair."""

    columns: list[int] = []
    for index, char in enumerate(title):
        if index:
            columns.append(0)
        columns.extend(glyph_columns(char))
    return tuple(columns)


def text_width(title: str) -> int:
    """Return the exact proportional width used for centring and scrolling."""

    return len(text_columns(title))


def scroll_offset_px(
    elapsed_ms: int,
    completed_scroll_frames: int,
    frames_per_pixel: int = SCROLL_FRAMES_PER_PIXEL,
) -> int:
    """Return the offset after a count of completed post-hold draw frames.

    Wall-clock time only gates the initial hold.  It never controls distance,
    so a late or stalled frame cannot make the marquee catch up by skipping
    physical LED columns.
    """

    if frames_per_pixel <= 0:
        raise ValueError("frames_per_pixel must be positive")
    if completed_scroll_frames < 0:
        raise ValueError("completed_scroll_frames cannot be negative")
    if int(elapsed_ms) < SCROLL_HOLD_MS:
        return 0
    return int(completed_scroll_frames) // frames_per_pixel


def render_matrix(
    title: str,
    elapsed_ms: int,
    progress: int = 14,
    cap_elapsed_ms: int | None = None,
    *,
    completed_scroll_frames: int = 0,
    scroll_mode: str = "frame",
    step_ms: int = 100,
) -> Image.Image:
    """Return one exact 32x8 RGB frame for a Berry scroll treatment.

    ``frame`` mirrors production: every glyph column remains at full stable
    brightness for three completed draws, then advances by one physical LED.
    ``step`` and ``crossfade`` remain only for comparing superseded treatments.
    """

    if scroll_mode not in {"crossfade", "step", "frame"}:
        raise ValueError("scroll_mode must be 'crossfade', 'step', or 'frame'")
    if step_ms <= 0:
        raise ValueError("step_ms must be positive")

    pixels = [[(0, 0, 0) for _ in range(WIDTH)] for _ in range(HEIGHT)]

    # The audit separately proves that these legacy bytes equal all Berry paths.
    icon = legacy.render_record_icon_frame((elapsed_ms // 150) % 12)
    for y in range(8):
        for x in range(8):
            offset = (y * 8 + x) * 3
            pixels[y][x] = tuple(icon[offset : offset + 3])  # type: ignore[assignment]

    columns = text_columns(title)
    text_w = len(columns)
    if text_w <= 23:
        origin = 9 + (23 - text_w) // 2
        for column, bits in enumerate(columns):
            for row in range(5):
                if bits & (1 << (4 - row)):
                    _put(pixels, origin + column, row + 1, TEXT_COLOR)
    elif scroll_mode == "crossfade":
        distance = 0.0 if elapsed_ms <= 1000 else (elapsed_ms - 1000) * 0.014
        whole = int(distance)
        fraction = distance - whole
        period = text_w + 8
        whole %= period
        for x in range(9, 32):
            source0 = (x - 9 + whole) % period
            source1 = (source0 + 1) % period
            edge_color = TEXT_COLOR
            if x in (9, 31):
                edge_color = OUTER_TEXT_COLOR
            elif x in (10, 30):
                edge_color = MID_TEXT_COLOR
            for row in range(5):
                on0 = False
                on1 = False
                if source0 < text_w:
                    bits = columns[source0]
                    if bits & (1 << (4 - row)):
                        on0 = True
                if source1 < text_w:
                    bits = columns[source1]
                    if bits & (1 << (4 - row)):
                        on1 = True
                if on0 and on1:
                    color = edge_color
                elif on0:
                    color = tuple(int(channel * (1.0 - fraction) + 0.5) for channel in edge_color)
                elif on1:
                    color = tuple(int(channel * fraction + 0.5) for channel in edge_color)
                else:
                    continue
                _put(pixels, x, row + 1, color)
    else:
        # A physical 32x8 matrix has no sub-pixels.  The production treatment
        # advances only from completed draws; the legacy step mode is retained
        # solely for the offline cadence comparison.
        if scroll_mode == "frame":
            whole = scroll_offset_px(elapsed_ms, completed_scroll_frames)
        else:
            whole = max(0, int(elapsed_ms) - SCROLL_HOLD_MS) // step_ms
        period = text_w + 8
        whole %= period
        for x in range(9, 32):
            source = (x - 9 + whole) % period
            if source >= text_w:
                continue
            edge_color = TEXT_COLOR
            if x in (9, 31):
                edge_color = OUTER_TEXT_COLOR
            elif x in (10, 30):
                edge_color = MID_TEXT_COLOR
            bits = columns[source]
            for row in range(5):
                if bits & (1 << (4 - row)):
                    _put(pixels, x, row + 1, edge_color)

    if progress >= 0:
        for x in range(8, 32):
            _put(pixels, x, 7, _rgb(0x35131F))
        amount = min(24, max(0, progress))
        if amount > 0:
            for index in range(amount):
                if amount == 1:
                    color = (255, 183, 47)
                else:
                    q = index * 2.0 / (amount - 1)
                    if q <= 1.0:
                        color = (
                            int(255 - 15 * q + 0.5),
                            int(183 - 61 * q + 0.5),
                            int(47 + 3 * q + 0.5),
                        )
                    else:
                        q -= 1.0
                        color = (
                            int(240 + 15 * q + 0.5),
                            int(122 - 40 * q + 0.5),
                            int(50 + 67 * q + 0.5),
                        )
                _put(pixels, 8 + index, 7, color)

            cap_age = elapsed_ms if cap_elapsed_ms is None else cap_elapsed_ms
            phase = min(2, max(0, cap_age // 250))
            leader = max(0, amount - 3 + phase)
            glow = (_rgb(0xFFF4EA), _rgb(0xFFD4DD), _rgb(0xFF8EA6))
            for index in range(leader, amount):
                _put(pixels, 8 + index, 7, glow[min(index - leader, 2)])

    image = Image.new("RGB", (WIDTH, HEIGHT))
    image.putdata([color for row in pixels for color in row])
    return image


def pixel_scale(image: Image.Image, scale: int = 16, grid: bool = False) -> Image.Image:
    enlarged = image.resize((image.width * scale, image.height * scale), Image.Resampling.NEAREST)
    if grid:
        draw = ImageDraw.Draw(enlarged)
        grid_color = (20, 20, 24)
        for x in range(0, enlarged.width + 1, scale):
            draw.line((x, 0, x, enlarged.height), fill=grid_color)
        for y in range(0, enlarged.height + 1, scale):
            draw.line((0, y, enlarged.width, y), fill=grid_color)
    return enlarged


def page_duration_ms(title: str) -> int:
    """Mirror the script's fixed fail-safe; normal pages advance themselves."""

    del title
    return ROTATION_WATCHDOG_MS


def preview_duration_ms(title: str, frame_ms: int = NOMINAL_FRAME_MS) -> int:
    """Return a compact offline preview covering at least one complete loop."""

    text_w = text_width(title)
    if text_w <= 23:
        return 7000
    period = text_w + 8
    return max(7000, SCROLL_HOLD_MS + period * SCROLL_FRAMES_PER_PIXEL * frame_ms + 250)


def save_gif(title: str, output: Path, progress: int, scale: int) -> Path:
    duration_ms = preview_duration_ms(title)
    # 25 ms mirrors AWTRIX NG's documented ~40 fps script renderer.
    frames = [
        pixel_scale(
            render_matrix(
                title,
                stamp,
                progress,
                completed_scroll_frames=max(0, stamp - SCROLL_HOLD_MS) // NOMINAL_FRAME_MS,
            ),
            scale,
        )
        for stamp in range(0, duration_ms, 25)
    ]
    # GIF timing is quantized to 10 ms. Alternating 20/30 ms preserves the
    # renderer's 25 ms average instead of silently shortening every frame.
    durations = [20 if index % 2 == 0 else 30 for index in range(len(frames))]
    output.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=False,
        disposal=2,
    )
    return output


def save_contact_sheet(title: str, output: Path, progress: int, scale: int) -> Path:
    stamps = (0, 975, 1025, 1250, 1750, 2500, 3500, 5000)
    frame_w = WIDTH * scale
    frame_h = HEIGHT * scale
    label_h = 24
    gap = 16
    columns = 2
    rows = (len(stamps) + columns - 1) // columns
    sheet = Image.new(
        "RGB",
        (columns * frame_w + (columns + 1) * gap, rows * (frame_h + label_h) + (rows + 1) * gap),
        (12, 13, 18),
    )
    draw = ImageDraw.Draw(sheet)
    for index, stamp in enumerate(stamps):
        col = index % columns
        row = index // columns
        x = gap + col * (frame_w + gap)
        y = gap + row * (frame_h + label_h + gap)
        frame_count = max(0, stamp - SCROLL_HOLD_MS) // NOMINAL_FRAME_MS
        sheet.paste(
            pixel_scale(
                render_matrix(
                    title,
                    stamp,
                    progress,
                    completed_scroll_frames=frame_count,
                ),
                scale,
                grid=True,
            ),
            (x, y),
        )
        draw.text((x, y + frame_h + 5), f"t={stamp / 1000:.3f}s", fill=(215, 220, 230))
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output)
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", default="MIDNIGHT CITY - M83")
    parser.add_argument("--progress", type=int, default=14)
    parser.add_argument("--scale", type=int, default=12)
    parser.add_argument("--output-dir", type=Path, default=Path("berry_preview"))
    args = parser.parse_args()
    title = args.title.upper()
    gif = save_gif(title, args.output_dir / "now_playing_berry.gif", args.progress, args.scale)
    sheet = save_contact_sheet(
        title,
        args.output_dir / "now_playing_berry_contact_sheet.png",
        args.progress,
        args.scale,
    )
    print(gif.resolve())
    print(sheet.resolve())


if __name__ == "__main__":
    main()
