"""Offline, payload-exact comparison gallery for the colorful LED redesign.

Capture a baseline before editing with ``--capture before.json``. Then pass
that capture to ``--before before.json --out figures/led-redesign``. The
gallery decodes the same GIF and RGB888 bitmap that the panel receives; no
network, device writes, or substitute artwork is involved.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import math
from pathlib import Path
import sys

from PIL import Image, ImageDraw, ImageFont

# weather_canvas also supports direct sender execution and uses a sibling import.
sys.path.insert(0, str(Path(__file__).resolve().parent / "weather"))

import desk_companion as companion
import pc_stats_push as pc
import firmware_compat
from weather import aqi_brief_push as aqi
from weather import weather_brief_push as weather


def examples(animated: bool = True) -> list[tuple[str, str, str, dict]]:
    """Fixed illustrative readings, identical for both versions."""
    result = []
    for key, title, condition, is_day, temp in (
        ("sun", "Sunshine", "clear", True, 78),
        ("night", "Clear night", "clear", False, 64),
        ("partly", "Partly cloudy", "part", True, 72),
        ("cloudy", "Cloudy", "cloudy", True, 68),
        ("overcast", "Overcast", "overcast", True, 62),
        ("rain", "Rain", "rain", True, 58),
        ("drizzle", "Drizzle", "drizzle", True, 60),
        ("snow", "Snow", "snow", True, 28),
        ("sleet", "Sleet", "sleet", True, 32),
        ("storm", "Thunderstorm", "storm", True, 66),
        ("fog", "Fog", "fog", True, 54),
    ):
        scene = weather.WeatherScene("current", f"{temp}°", condition, is_day,
                                     temp, temp - 8, temp + 10, None)
        result.append((key, title, "Weather", weather.build_payload(scene, animated=animated)))
    when = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
    for value, title in ((28, "Good air"), (72, "Moderate air"), (125, "Sensitive groups"),
                         (175, "Unhealthy air"), (250, "Very unhealthy air"), (350, "Hazardous air")):
        sample = aqi.AQISnapshot(when, "UTC", value, 12.0,
                                tuple(when + timedelta(hours=i) for i in range(1, 6)),
                                (value,) * 5)
        result.append((f"aqi-{value}", title, "Air quality", aqi.build_payload(sample, animated=animated)))
    for mood in companion.CompanionMood:
        result.append((f"companion-{mood.value}", mood.value.replace("_", " ").title(),
                       "Companion", companion.build_companion_payload(mood, animated=animated)))
    for label, value in (("CPU", 82), ("RAM", 91), ("GPU", 96)):
        result.append((label.lower(), f"{label} activity", "PC alerts", pc.metric_payload(label, value, animated=animated)))
    result.append(("combined", "CPU + RAM", "PC alerts", pc.combined_payload(82, 91, animated=animated)))
    return result


def decode(payload: dict) -> dict:
    """Composite opaque GIF frames and static bitmaps in firmware draw order."""
    icon = payload["icon"]
    icon_bytes = base64.b64decode(icon.split(",", 1)[1] if icon.startswith("data:") else icon, validate=True)
    frames, durations = [], []
    with Image.open(BytesIO(icon_bytes)) as gif:
        looping = gif.info.get("loop") == 0
        for phase in range(gif.n_frames):
            gif.seek(phase)
            panel = Image.new("RGB", (32, 8))
            panel.paste(gif.convert("RGB"), (payload.get("iconOffsetX", 0), 0))
            for op in payload["draw"]:
                if op[0] != "bitmap":
                    raise ValueError(f"Preview does not implement {op[0]}")
                _, x, y, width, height, data = op
                panel.paste(Image.frombytes("RGB", (width, height), base64.b64decode(data)), (x, y))
            pixels = list(panel.getdata())
            if any(pixels[y * 32 + 8] != (0, 0, 0) for y in range(8)):
                raise ValueError("The icon/value separator is not black")
            frames.append(["%02x%02x%02x" % color for color in pixels])
            durations.append(gif.info.get("duration", 120))
    return {"frames": frames, "durations": durations, "loop": looping,
            "bytes": len(json.dumps(payload, separators=(",", ":")).encode()),
            "iconBytes": len(icon_bytes)}


def capture() -> list[dict]:
    # Measure the current wire format, including MIME prefixes. Include the
    # real static safety payload instead of freezing an arbitrary moving pose.
    endpoint = "offline://preview"
    firmware_compat.observe_device(endpoint, {"version": "1.2.2"})
    static = {key: decode(firmware_compat.prepare_payload(endpoint, payload))
              for key, _, _, payload in examples(animated=False)}
    return [{"id": key, "title": title, "group": group,
             **decode(firmware_compat.prepare_payload(endpoint, payload)), "static": static[key]}
            for key, title, group, payload in examples()]


def led_image(pixels: list[str], cell: int = 16) -> Image.Image:
    img = Image.new("RGB", (32 * cell, 8 * cell), (3, 5, 10))
    draw = ImageDraw.Draw(img)
    for index, value in enumerate(pixels):
        x, y = (index % 32) * cell, (index // 32) * cell
        color = tuple(bytes.fromhex(value)) if value != "000000" else (10, 14, 22)
        draw.rectangle((x + 2, y + 2, x + cell - 3, y + cell - 3), fill=color)
    return img


def exports(after: list[dict], out: Path) -> None:
    """Small looping gallery GIF and a labeled contact sheet of real frames."""
    featured = [next(x for x in after if x["id"] == key)
                for key in ("overcast", "rain", "companion-idle", "companion-rest", "gpu", "combined")]
    font_path = Path("C:/Windows/Fonts/segoeui.ttf")
    font = ImageFont.truetype(str(font_path), 21) if font_path.exists() else ImageFont.load_default()
    title_font = ImageFont.truetype(str(font_path), 30) if font_path.exists() else font

    def board(tick: int) -> Image.Image:
        img = Image.new("RGB", (1120, 650), (12, 17, 28))
        draw = ImageDraw.Draw(img)
        draw.text((32, 20), "DRIFTMATRIX / APP VISUAL REVIEW", font=title_font, fill=(238, 242, 252))
        draw.text((32, 61), "Actual 32 × 8 artwork · illustrative readings · offline preview", font=font, fill=(153, 166, 190))
        for index, entry in enumerate(featured):
            x, y = 32 + (index % 2) * 552, 112 + (index // 2) * 176
            elapsed = tick * 120
            total = sum(entry["durations"])
            elapsed = elapsed % total if entry["loop"] else min(elapsed, total - 1)
            phase = 0
            while elapsed >= entry["durations"][phase] and phase < len(entry["frames"]) - 1:
                elapsed -= entry["durations"][phase]
                phase += 1
            draw.text((x, y), entry["title"], font=font, fill=(195, 209, 231))
            img.paste(led_image(entry["frames"][phase]), (x, y + 32))
        return img

    # Close every featured cycle exactly at the montage loop seam, too.
    ticks = math.lcm(*(sum(entry["durations"]) for entry in featured)) // 120
    board(0).save(out / "redesign.png")
    frames = [board(tick).convert("P", palette=Image.Palette.ADAPTIVE, colors=256)
              for tick in range(ticks)]
    frames[0].save(out / "redesign.gif", save_all=True, append_images=frames[1:],
                   duration=120, loop=0, disposal=1, optimize=True)


HTML = Path(__file__).with_name('app_visual_gallery.html').read_text(encoding='utf-8')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--before", type=Path)
    parser.add_argument("--out", type=Path, default=Path("figures/led-redesign"))
    args = parser.parse_args()
    after = capture()
    if args.capture:
        args.capture.parent.mkdir(parents=True, exist_ok=True)
        args.capture.write_text(json.dumps(after), encoding="utf-8")
        print(f"Captured {len(after)} scenes to {args.capture}")
        return
    if args.before is None:
        parser.error("--before is required for a comparison gallery")
    before = json.loads(args.before.read_text(encoding="utf-8"))
    args.out.mkdir(parents=True, exist_ok=True)
    data = json.dumps({"before": before, "after": after}).replace("</", "<\\/")
    (args.out / "index.html").write_text(HTML.replace("__DATA__", data), encoding="utf-8")
    (args.out / "after.json").write_text(json.dumps(after), encoding="utf-8")
    exports(after, args.out)
    print(f"Gallery: {(args.out / 'index.html').resolve()}")
    print(f"Scenes: {len(after)}; maximum payload: {max(x['bytes'] for x in after)} B")


if __name__ == "__main__":
    main()
