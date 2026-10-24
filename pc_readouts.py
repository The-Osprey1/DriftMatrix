"""Opt-in local CPU/GPU readouts. This module never contacts the panel."""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import time

import psutil
from PIL import Image, ImageDraw

from pc_stats_preview import FONT_3X5, panel_frame
from weather.weather_brief_preview import BLACK, led_image, new_frame, raw_image

WHITE = (235, 240, 245)
NEUTRAL = (130, 145, 160)
TRACK = (16, 24, 32)
FONT = {**FONT_3X5,
        "G": ("111", "100", "101", "101", "111"),
        "N": ("101", "111", "111", "111", "101"),
        "/": ("001", "001", "010", "100", "100")}


@dataclass(frozen=True)
class Reading:
    metric: str
    value: float | None
    sampled_at: float
    source: str
    detail: str = ""


def valid_percent(value: object) -> float | None:
    try:
        number = float(value) if value is not None else math.nan
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and 0 <= number <= 100 else None


def load_color(value: float) -> tuple[int, int, int]:
    """Load bands, not temperature or hardware-health judgments."""
    return ((0, 230, 118) if value < 50 else (255, 214, 10) if value < 75
            else (255, 149, 0) if value < 90 else (255, 59, 48))


def display_value(reading: Reading, now: float, max_age: float = 15) -> float | None:
    age = now - reading.sampled_at
    if not math.isfinite(age) or not 0 <= age <= max_age:
        return None
    return valid_percent(reading.value)


def draw_text(frame, text: str, x: int, color) -> None:
    for char in text:
        for y, row in enumerate(FONT[char]):
            for dx, bit in enumerate(row):
                if bit == "1":
                    frame[y + 1][x + dx] = color
        x += 4


def readout_frame(reading: Reading, now: float):
    if reading.metric not in ("CPU", "GPU"):
        raise ValueError("metric must be CPU or GPU")
    value = display_value(reading, now)
    percent = None if value is None else int(math.floor(value + .5))
    text = "N/A" if percent is None else f"{percent}%"
    color = NEUTRAL if percent is None else load_color(percent)
    frame = new_frame()
    draw_text(frame, reading.metric, 0, WHITE)
    draw_text(frame, text, 32 - (len(text) * 4 - 1), color)
    filled = 0 if percent is None else int(math.floor(percent * 32 / 100 + .5))
    for x in range(32):
        frame[7][x] = color if x < filled else TRACK
    return frame


def parse_gpu_csv(output: str, gpu_index: int, sampled_at: float) -> Reading:
    for row in csv.reader(output.splitlines()):
        if len(row) != 3 or row[0].strip() != str(gpu_index):
            continue
        value = valid_percent(row[2].strip())
        return Reading("GPU", value, sampled_at, "nvidia-smi utilization.gpu",
                       row[1].strip() + ("; utilization unavailable" if value is None else ""))
    return Reading("GPU", None, sampled_at, "nvidia-smi utilization.gpu",
                   f"GPU index {gpu_index} not returned")


def sample_gpu(gpu_index: int = 0) -> Reading:
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return Reading("GPU", None, time.time(), "nvidia-smi", "nvidia-smi not installed")
    try:
        result = subprocess.run(
            [executable, "--query-gpu=index,name,utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return parse_gpu_csv(result.stdout, gpu_index, time.time())
    except (OSError, subprocess.SubprocessError) as error:
        return Reading("GPU", None, time.time(), "nvidia-smi", type(error).__name__)


def export(output: Path, live: bool = False, gpu_index: int = 0) -> list[Path]:
    output.mkdir(parents=True, exist_ok=True)
    now = time.time()
    entries = []
    if live:
        cpu_detail = ""
        try:
            cpu = valid_percent(psutil.cpu_percent(interval=0.5))
            if cpu is None:
                cpu_detail = "CPU utilization unavailable"
        except (OSError, psutil.Error) as error:
            cpu = None
            cpu_detail = type(error).__name__
        entries.append(("cpu_snapshot", Reading("CPU", cpu, time.time(), "psutil 0.5-second CPU sample", cpu_detail)))
        entries.append(("gpu_snapshot", sample_gpu(gpu_index)))
        now = time.time()
    else:
        for metric in ("CPU", "GPU"):
            for value in (0, 26, 74, 87, 100, None):
                entries.append((f"{metric.lower()}_{'na' if value is None else value}",
                                Reading(metric, value, now, "synthetic design sample")))
    rows = []
    paths = []
    for name, reading in entries:
        frame = readout_frame(reading, now)
        path = output / f"{name}_32x8.png"
        raw_image(frame).save(path)
        paths.append(path)
        rows.append((name.replace("_", " "), led_image(frame, 12, 1)))
    if not live:
        rows.insert(0, ("CPU 87% BEFORE (existing alert)", led_image(panel_frame("CPU", 87), 12, 1)))
        raw_image(panel_frame("CPU", 87)).save(output / "cpu_87_before_32x8.png")
    sheet = Image.new("RGB", (670, len(rows) * 120 + 40), (8, 10, 14))
    draw = ImageDraw.Draw(sheet)
    draw.text((12, 12), "SOURCE PIXELS - local preview, no physical brightness/gamma simulation", fill=WHITE)
    for index, (label, preview) in enumerate(rows):
        y = index * 120 + 40
        draw.text((12, y + 40), label, fill=WHITE)
        sheet.paste(preview, (275, y))
    path = output / ("snapshot.png" if live else "comparison.png")
    sheet.save(path)
    paths.append(path)
    if not live:
        selected = [rows[index] for index in (0, 4, 11, 12)]
        overview = Image.new("RGB", (670, 520), (8, 10, 14))
        painter = ImageDraw.Draw(overview)
        painter.text((12, 12), "SOURCE PIXELS - CPU before/after, new GPU full-scale and unavailable", fill=WHITE)
        labels = ("CPU 87% BEFORE", "CPU 87% AFTER", "NEW GPU 100%", "NEW GPU N/A")
        for index, ((_, preview), label) in enumerate(zip(selected, labels)):
            y = index * 120 + 40
            painter.text((12, y + 40), label, fill=WHITE)
            overview.paste(preview, (275, y))
        path = output / "overview.png"
        overview.save(path)
        paths.append(path)
    metadata = {"mode": "local snapshot" if live else "synthetic preview", "rendered_at": now,
                "readings": [asdict(reading) for _, reading in entries],
                "note": "Source RGB pixels; no display changes. No existing GPU readout was found."}
    path = output / ("snapshot.json" if live else "preview.json")
    path.write_text(json.dumps(metadata, indent=2, allow_nan=False), encoding="utf-8")
    paths.append(path)
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(os.environ["LOCALAPPDATA"]) / "AWTRIX" / "previews" / "cpu-gpu")
    parser.add_argument("--snapshot", action="store_true", help="Read local CPU and NVIDIA GPU once")
    parser.add_argument("--gpu-index", type=int, default=0)
    args = parser.parse_args()
    for path in export(args.output, args.snapshot, args.gpu_index):
        print(path)


if __name__ == "__main__":
    main()
