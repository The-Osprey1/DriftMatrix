"""Save the AWTRIX framebuffer as a zoomed PNG for visual verification."""

from __future__ import annotations

import argparse
from pathlib import Path

import requests
from PIL import Image, ImageDraw


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("name", help="output stem, for example wxnow-night")
    # 192.168.1.90 since at least 2026-08; mDNS has advertised a stale second
    # address on this LAN, so the raw IP is the reliable default.
    parser.add_argument("--panel", default="http://192.168.1.90")
    parser.add_argument("--scale", type=int, default=24)
    parser.add_argument("--output-dir", default=".")
    args = parser.parse_args()

    response = requests.get(f"{args.panel}/api/v1/display/screen", timeout=5)
    response.raise_for_status()
    frame = response.json()
    width = int(frame["width"])
    height = int(frame["height"])
    pixels = frame["pixels"]
    if len(pixels) != width * height:
        raise ValueError("framebuffer pixel count does not match its dimensions")

    scale = max(2, args.scale)
    image = Image.new("RGB", (width * scale, height * scale), (10, 10, 12))
    draw = ImageDraw.Draw(image)
    gap = max(1, scale // 10)
    for index, packed in enumerate(pixels):
        x = index % width
        y = index // width
        color = ((packed >> 16) & 255, (packed >> 8) & 255, packed & 255)
        draw.rectangle(
            (
                x * scale + gap,
                y * scale + gap,
                (x + 1) * scale - gap - 1,
                (y + 1) * scale - gap - 1,
            ),
            fill=color,
        )

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"capture-{args.name}.png"
    image.save(output)
    print(output)


if __name__ == "__main__":
    main()
