"""Render reviewable previews of the real 32x8 Now Playing artwork."""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

import now_playing_push as app


CONTACT_SHEET_FRAMES = 7
CONTACT_SHEET_COLUMNS = 4
CONTACT_SHEET_GAP = 8
CONTACT_SHEET_LABEL_HEIGHT = 18
# Sample the native composition independently of the 150ms record keyframes.
# At 30ms, the speed-100 text moves by at most one physical LED per sample.
PREVIEW_FRAME_MS = 30


def render_preview(title: str, progress: float | None, output: Path, scale: int) -> Path:
    if scale <= 0:
        raise ValueError("scale must be a positive integer")
    frame = app.render_frame(title, progress, elapsed_ms=0)
    image = Image.frombytes("RGB", (app.WIDTH, app.HEIGHT), frame)
    image = image.resize((app.WIDTH * scale, app.HEIGHT * scale), Image.Resampling.NEAREST)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output)
    return output


def _validate_scale(scale: int) -> None:
    if scale <= 0:
        raise ValueError("scale must be a positive integer")


def _panel_frames(
    title: str,
    progress: float | None,
    paused: bool,
) -> tuple[list[Image.Image], list[int]]:
    """Render a review loop of the exact 32x8 composition.

    The panel receives only the 8x8 record as a GIF; AWTRIX itself draws the
    title and progress. These frames combine those native regions for review.
    """
    if paused:
        timestamps = [0]
        durations = [round(app.PAUSE_GRACE_SECONDS * 1000)]
    else:
        normalized = app.limit_display_text(app.normalize_media_text(title) or "NOW PLAYING")
        text_width = app.measure_text(normalized)
        pixels_per_second = 21 * app.SCROLL_SPEED / 100
        scroll_duration_ms = app.SCROLL_HOLD_MS + math.ceil(
            (text_width + app.SCROLL_GAP) * 1000 / pixels_per_second
        )
        review_duration_ms = (
            scroll_duration_ms if text_width > app.TEXT_WIDTH else app.PAGE_DURATION_MS
        )
        frame_count = math.ceil(review_duration_ms / PREVIEW_FRAME_MS)
        timestamps = [index * PREVIEW_FRAME_MS for index in range(frame_count)]
        durations = [PREVIEW_FRAME_MS] * frame_count
    frames = [
        Image.frombytes(
            "RGB",
            (app.WIDTH, app.HEIGHT),
            app.render_frame(
                title,
                progress,
                elapsed_ms,
                paused=paused,
                progress_phase=(
                    app.PROGRESS_SWEEP_FINAL_PHASE
                    if paused
                    else app.progress_sweep_phase_at(elapsed_ms)
                ),
            ),
        )
        for elapsed_ms in timestamps
    ]
    return frames, durations


def _scaled_frame(frame: Image.Image, scale: int) -> Image.Image:
    return frame.resize(
        (app.WIDTH * scale, app.HEIGHT * scale),
        Image.Resampling.NEAREST,
    )


def render_animation_preview(
    title: str,
    progress: float | None,
    output: Path,
    scale: int,
    paused: bool = False,
) -> Path:
    """Write the panel's real local animation as an enlarged looping GIF."""
    _validate_scale(scale)
    frames, durations = _panel_frames(title, progress, paused)
    enlarged = [_scaled_frame(frame, scale) for frame in frames]

    output.parent.mkdir(parents=True, exist_ok=True)
    enlarged[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=enlarged[1:],
        duration=durations,
        loop=0,
        disposal=1,
        optimize=True,
    )
    return output


def _evenly_spaced_indices(total: int, wanted: int) -> list[int]:
    if total <= 0 or wanted <= 0:
        raise ValueError("frame counts must be positive")
    if wanted == 1:
        return [0]
    if total == 1:
        return [0] * wanted
    return [round(index * (total - 1) / (wanted - 1)) for index in range(wanted)]


def render_contact_sheet(
    title: str,
    progress: float | None,
    output: Path,
    scale: int,
    paused: bool = False,
    frame_count: int = CONTACT_SHEET_FRAMES,
) -> Path:
    """Write seven evenly spaced moments from the real panel animation."""
    _validate_scale(scale)
    frames, durations = _panel_frames(title, progress, paused)
    indices = _evenly_spaced_indices(len(frames), frame_count)

    frame_width = app.WIDTH * scale
    frame_height = app.HEIGHT * scale
    cell_height = frame_height + CONTACT_SHEET_LABEL_HEIGHT
    columns = min(CONTACT_SHEET_COLUMNS, frame_count)
    rows = math.ceil(frame_count / columns)
    canvas = Image.new(
        "RGB",
        (
            columns * frame_width + (columns - 1) * CONTACT_SHEET_GAP,
            rows * cell_height + (rows - 1) * CONTACT_SHEET_GAP,
        ),
        "black",
    )
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    starts_ms: list[int] = []
    elapsed = 0
    for duration in durations:
        starts_ms.append(elapsed)
        elapsed += duration

    for sheet_index, frame_index in enumerate(indices):
        column = sheet_index % columns
        row = sheet_index // columns
        x = column * (frame_width + CONTACT_SHEET_GAP)
        y = row * (cell_height + CONTACT_SHEET_GAP)
        canvas.paste(_scaled_frame(frames[frame_index], scale), (x, y))
        label = f"F{frame_index:02d}  {starts_ms[frame_index] / 1000:.2f}s"
        draw.text(
            (x + 2, y + frame_height + 3),
            label,
            fill=(174, 238, 255),
            font=font,
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output)
    return output


def _nonblack_crop(image: Image.Image) -> Image.Image:
    rgb = image.convert("RGB")
    # Generated concepts can contain imperceptible near-black noise across the
    # full canvas. Threshold it so the QA comparison crops to the visible art.
    mask = ImageChops.difference(rgb, Image.new("RGB", rgb.size, "black")).convert("L")
    mask = mask.point(lambda value: 255 if value > 18 else 0)
    bbox = mask.getbbox()
    return rgb.crop(bbox) if bbox else rgb


def build_comparison(source_path: Path, implementation_path: Path, output: Path) -> Path:
    source = _nonblack_crop(Image.open(source_path))
    implementation = Image.open(implementation_path).convert("RGB")
    target_width = max(source.width, implementation.width)

    def fit_width(image: Image.Image) -> Image.Image:
        height = round(image.height * target_width / image.width)
        return image.resize((target_width, height), Image.Resampling.NEAREST)

    source = fit_width(source)
    implementation = fit_width(implementation)
    label_height = 28
    gap = 16
    canvas = Image.new(
        "RGB",
        (target_width, source.height + implementation.height + label_height * 2 + gap),
        "black",
    )
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    draw.text((6, 8), "SOURCE CONCEPT", fill=(160, 200, 224), font=font)
    source_y = label_height
    canvas.paste(source, (0, source_y))
    implementation_label_y = source_y + source.height + gap
    draw.text((6, implementation_label_y + 8), "32 x 8 IMPLEMENTATION", fill=(160, 200, 224), font=font)
    canvas.paste(implementation, (0, implementation_label_y + label_height))
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output)
    return output


def parse_progress(raw: str) -> float | None:
    if raw.lower() in {"none", "unknown", "live"}:
        return None
    return float(raw)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render static and animated previews of the 32x8 Now Playing app."
    )
    parser.add_argument("--title", default="VERIDIS")
    parser.add_argument("--progress", type=parse_progress, default=0.5)
    parser.add_argument("--scale", type=int, default=24)
    parser.add_argument("--paused", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("now_playing_preview.png"))
    parser.add_argument(
        "--animation-output",
        type=Path,
        default=Path("now_playing_animation.gif"),
    )
    parser.add_argument(
        "--contact-sheet-output",
        type=Path,
        default=Path("now_playing_contact_sheet.png"),
    )
    parser.add_argument("--source", type=Path)
    parser.add_argument("--comparison-output", type=Path, default=Path("now_playing_comparison.png"))
    args = parser.parse_args()

    preview = render_preview(args.title, args.progress, args.output, args.scale)
    print(preview.resolve())
    animation = render_animation_preview(
        args.title,
        args.progress,
        args.animation_output,
        args.scale,
        paused=args.paused,
    )
    print(animation.resolve())
    contact_sheet = render_contact_sheet(
        args.title,
        args.progress,
        args.contact_sheet_output,
        args.scale,
        paused=args.paused,
    )
    print(contact_sheet.resolve())
    if args.source:
        comparison = build_comparison(args.source, preview, args.comparison_output)
        print(comparison.resolve())


if __name__ == "__main__":
    main()
