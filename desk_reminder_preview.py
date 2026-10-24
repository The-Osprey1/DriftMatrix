"""Export a reproducible, local-only 32x8 reminder artwork gallery."""
import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from desk_reminder import Reminder, render
from focus_sprint_preview import panel_grade_frame


def export(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    rows = [
        ("5 MINUTES LEFT", render(Reminder("preview", "TEA", 1000, 1300), 1000)),
        ("LABEL PAGE", render(Reminder("preview", "WATER", 1000, 1300), 1100, show_label=True)),
        ("UNDER 1 MINUTE", render(Reminder("preview", "TEA", 1000, 1300), 1270)),
        ("DUE / BELL", render(Reminder("preview", "TEA", 1000, 1300), 1300)),
        ("24 HOUR TIMER", render(Reminder("preview", "BREAK", 1000, 87400), 1000)),
    ]
    sheet = Image.new("RGB", (1080, 180 + len(rows) * 122), "#0c1119")
    draw = ImageDraw.Draw(sheet)
    font_path = Path("C:/Windows/Fonts/segoeui.ttf")
    font = ImageFont.truetype(str(font_path), 18) if font_path.exists() else ImageFont.load_default()
    title_font = ImageFont.truetype(str(font_path), 30) if font_path.exists() else font
    draw.text((24, 20), "DESK REMINDER  /  32 × 8", fill="#eef5ff", font=title_font)
    draw.text((24, 65), "Synthetic design samples · local preview · no panel deployment", fill="#aab9cf", font=font)
    draw.text((245, 110), "Source pixels", fill="#c4efff", font=font)
    draw.text((660, 110), "Brightness 1 visibility mask*", fill="#c4efff", font=font)
    for index, (label, frame) in enumerate(rows):
        top = 150 + index * 122
        draw.text((24, top + 35), label, fill="#eef5ff", font=font)
        image = Image.new("RGB", (32, 8))
        image.putdata([pixel for row in frame for pixel in row])
        image.save(directory / f"reminder_{index}_32x8.png")
        graded = panel_grade_frame(frame, brightness=1)
        mask = [[tuple(255 if channel else 0 for channel in pixel) for pixel in row] for row in graded]
        for left, pixels in ((245, frame), (660, mask)):
            for y, row in enumerate(pixels):
                for x, color in enumerate(row):
                    draw.ellipse((left + x * 12, top + y * 12, left + x * 12 + 9, top + y * 12 + 9), fill=color if any(color) else "#18202c")
    draw.text((24, sheet.height - 26), "*Surviving channels amplified for inspection; not a prediction of perceived brightness.", fill="#aab9cf", font=font)
    output = directory / "reminder-gallery.png"
    sheet.save(output)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    print(export(parser.parse_args().out))
