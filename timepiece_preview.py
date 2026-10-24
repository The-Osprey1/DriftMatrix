"""Render Timepiece on the panel's literal 32x8 pixel grid.

Version 2 keeps the numerals still for almost the entire minute, then uses a
brief seven-pose split-flap roll at the minute boundary.  The bottom horizon
fills behind a connected comet so motion remains useful, not decorative.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

from PIL import Image

from weather.weather_brief_preview import grade_color, grade_frame, led_image, raw_image, storyboard


RGB = tuple[int, int, int]
Frame = list[list[RGB]]
WIDTH = 32
HEIGHT = 8

BLACK: RGB = (0, 0, 0)
PEARL: RGB = (255, 247, 232)
ICE: RGB = (157, 220, 255)
MINT: RGB = (99, 255, 177)
GOLD: RGB = (255, 212, 90)
CORAL: RGB = (255, 102, 94)
VIOLET: RGB = (196, 139, 255)
TRACK: RGB = (64, 52, 104)
TAIL: RGB = (73, 155, 130)

ROLL_STEP_MS = 75
ROLL_STEPS = 7
ROLL_DURATION_MS = ROLL_STEP_MS * ROLL_STEPS
ANTICIPATION_MS = 200


# Seven four-bit rows per numeral.  The gently rounded zeroes and open forms
# survive the low resolution better than trying to imitate a desktop font.
DIGIT_ROWS: tuple[tuple[int, ...], ...] = (
    (0x6, 0x9, 0x9, 0x9, 0x9, 0x9, 0x6),  # 0
    (0x2, 0x6, 0x2, 0x2, 0x2, 0x2, 0x7),  # 1
    (0x6, 0x9, 0x1, 0x2, 0x4, 0x8, 0xF),  # 2
    (0xE, 0x1, 0x1, 0x6, 0x1, 0x1, 0xE),  # 3
    (0x2, 0x6, 0xA, 0xA, 0xF, 0x2, 0x2),  # 4
    (0xF, 0x8, 0x8, 0xE, 0x1, 0x1, 0xE),  # 5
    (0x6, 0x8, 0x8, 0xE, 0x9, 0x9, 0x6),  # 6
    (0xF, 0x1, 0x2, 0x2, 0x4, 0x4, 0x4),  # 7
    (0x6, 0x9, 0x9, 0x6, 0x9, 0x9, 0x6),  # 8
    (0x6, 0x9, 0x9, 0x7, 0x1, 0x1, 0x6),  # 9
)


def new_frame() -> Frame:
    return [[BLACK for _ in range(WIDTH)] for _ in range(HEIGHT)]


def put(frame: Frame, x: int, y: int, color: RGB) -> None:
    if 0 <= x < WIDTH and 0 <= y < HEIGHT:
        frame[y][x] = color


def hour_color(hour_24: int) -> RGB:
    if 6 <= hour_24 < 12:
        return ICE
    if 12 <= hour_24 < 18:
        return GOLD
    if 18 <= hour_24 < 22:
        return CORAL
    return VIOLET


def draw_digit(
    frame: Frame,
    digit: int,
    x0: int,
    color: RGB,
    y_offset: int = 0,
) -> None:
    for y, row in enumerate(DIGIT_ROWS[digit]):
        for x in range(4):
            if row & (1 << (3 - x)):
                put(frame, x0 + x, y + y_offset, color)


def time_layout(hour_24: int) -> tuple[int, tuple[int, ...]]:
    hour_12 = hour_24 % 12 or 12
    digits = (
        (hour_12 // 10, hour_12 % 10) if hour_12 >= 10 else (hour_12,)
    ) + (0, 0)
    return (5 if hour_12 >= 10 else 8), digits


def comet_head_from_ms(minute_ms: int) -> int:
    return max(0, min(31, (max(0, min(59_999, int(minute_ms))) * WIDTH) // 60_000))


def roll_step_from_ms(minute_ms: int) -> int:
    """Return a crisp one-row pose, or zero once the new minute has settled."""

    phase = max(0, min(59_999, int(minute_ms)))
    if phase >= ROLL_DURATION_MS:
        return 0
    return min(ROLL_STEPS, phase // ROLL_STEP_MS + 1)


def _draw_timeface(
    frame: Frame,
    hour_24: int,
    minute: int,
    y_offset: int = 0,
) -> int:
    """Draw the four/seventeen-wide clock face and return the colon column."""

    hour_12 = hour_24 % 12 or 12
    x = 5 if hour_12 >= 10 else 8
    hour_ink = hour_color(hour_24)

    if hour_12 >= 10:
        draw_digit(frame, hour_12 // 10, x, hour_ink, y_offset)
        x += 5
    draw_digit(frame, hour_12 % 10, x, hour_ink, y_offset)
    x += 5
    colon_x = x
    x += 2
    draw_digit(frame, minute // 10, x, PEARL, y_offset)
    x += 5
    draw_digit(frame, minute % 10, x, PEARL, y_offset)
    return colon_x


def colon_color(minute_ms: int) -> RGB:
    phase = max(0, min(59_999, int(minute_ms)))
    if phase < ROLL_DURATION_MS or phase >= 60_000 - ANTICIPATION_MS:
        return PEARL
    return MINT


def draw_timepiece(
    hour_24: int,
    minute: int,
    minute_ms: int,
) -> Frame:
    if not 0 <= hour_24 <= 23 or not 0 <= minute <= 59:
        raise ValueError("time must be a valid local hour and minute")
    frame = new_frame()
    phase = max(0, min(59_999, int(minute_ms)))
    step = roll_step_from_ms(phase)
    if step:
        previous_minute = (minute + 59) % 60
        previous_hour = (hour_24 + 23) % 24 if minute == 0 else hour_24
        _draw_timeface(frame, previous_hour, previous_minute, -step)
        colon_x = _draw_timeface(frame, hour_24, minute, ROLL_STEPS - step)
    else:
        colon_x = _draw_timeface(frame, hour_24, minute)

    colon_ink = colon_color(phase)
    put(frame, colon_x, 2, colon_ink)
    put(frame, colon_x, 4, colon_ink)

    for rail_x in range(WIDTH):
        put(frame, rail_x, 7, TRACK)
    head = comet_head_from_ms(phase)
    for rail_x in range(head):
        put(frame, rail_x, 7, TAIL)
    if head >= 2:
        put(frame, head - 2, 7, MINT)
    if head >= 1:
        put(frame, head - 1, 7, GOLD)
    put(frame, head, 7, PEARL)
    return frame


@dataclass(frozen=True)
class RenderConfig:
    output: Path
    gamma: float = 1.3
    brightness: int = 45
    saturation: int = 90
    scale: int = 18
    gap: int = 1


def _graded(frame: Frame, config: RenderConfig) -> Frame:
    return grade_frame(frame, config.gamma, config.brightness, config.saturation)


def render(config: RenderConfig) -> list[Path]:
    config.output.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []

    now = datetime.now()
    live = _graded(
        draw_timepiece(now.hour, now.minute, (now.second * 1000 + now.microsecond // 1000) % 60_000),
        config,
    )
    live_path = config.output / "timepiece_live.png"
    led_image(live, config.scale, config.gap).save(live_path)
    paths.append(live_path)

    source_frames = [draw_timepiece(14, 37, int(index * 60_000 / 32)) for index in range(32)]
    frames = [_graded(frame, config) for frame in source_frames]
    led_frames = [led_image(frame, config.scale, config.gap) for frame in frames]
    animation_path = config.output / "timepiece_seconds_comet_led.gif"
    led_frames[0].save(
        animation_path,
        save_all=True,
        append_images=led_frames[1:],
        duration=120,
        loop=0,
        disposal=2,
        optimize=False,
    )
    paths.append(animation_path)

    raw_frames = [raw_image(frame) for frame in frames]
    raw_path = config.output / "timepiece_seconds_comet_32x8.gif"
    raw_frames[0].save(
        raw_path,
        save_all=True,
        append_images=raw_frames[1:],
        duration=120,
        loop=0,
        disposal=2,
        optimize=False,
    )
    paths.append(raw_path)

    transition_phases = (
        (14, 37, 59_800),
        (14, 37, 59_900),
        (14, 38, 0),
        (14, 38, 75),
        (14, 38, 150),
        (14, 38, 225),
        (14, 38, 300),
        (14, 38, 375),
        (14, 38, 450),
        (14, 38, 600),
    )
    transition = [
        led_image(_graded(draw_timepiece(h, m, ms), config), config.scale, config.gap)
        for h, m, ms in transition_phases
    ]
    transition_path = config.output / "timepiece_split_flap_led.gif"
    transition[0].save(
        transition_path,
        save_all=True,
        append_images=transition[1:],
        duration=[100, 100, 75, 75, 75, 75, 75, 75, 150, 700],
        loop=0,
        disposal=2,
        optimize=False,
    )
    paths.append(transition_path)

    transition_contact = [
        _graded(draw_timepiece(h, m, ms), config) for h, m, ms in transition_phases[2:9]
    ]
    transition_contact_path = config.output / "timepiece_split_flap_storyboard.png"
    storyboard(transition_contact, config.scale, config.gap).save(transition_contact_path)
    paths.append(transition_contact_path)

    moments: Sequence[tuple[int, int, int]] = (
        (2, 7, 0),
        (7, 41, 8_000),
        (11, 59, 18_000),
        (12, 0, 28_000),
        (14, 37, 38_000),
        (19, 24, 48_000),
        (23, 8, 59_000),
    )
    contact = [_graded(draw_timepiece(h, m, ms), config) for h, m, ms in moments]
    contact_path = config.output / "timepiece_dayparts.png"
    storyboard(contact, config.scale, config.gap).save(contact_path)
    paths.append(contact_path)

    return paths


def validate() -> None:
    if len(set(DIGIT_ROWS)) != 10:
        raise AssertionError("every numeral must have a distinct silhouette")
    for digit in DIGIT_ROWS:
        if len(digit) != 7 or any(row < 0 or row > 0xF for row in digit):
            raise AssertionError("digits must be exactly four by seven pixels")
    for hour in range(24):
        for minute in (0, 7, 42, 59):
            frame = draw_timepiece(hour, minute, 30_000)
            if len(frame) != 8 or any(len(row) != 32 for row in frame):
                raise AssertionError("Timepiece frame is not exactly 32x8")


def parse_args() -> RenderConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path.home() / ".codex" / "visualizations" / "timepiece",
    )
    parser.add_argument("--gamma", type=float, default=1.3)
    parser.add_argument("--brightness", type=int, default=45)
    parser.add_argument("--saturation", type=int, default=90)
    parser.add_argument("--scale", type=int, default=18)
    parser.add_argument("--gap", type=int, default=1)
    args = parser.parse_args()
    return RenderConfig(
        output=args.output.resolve(),
        gamma=args.gamma,
        brightness=args.brightness,
        saturation=args.saturation,
        scale=args.scale,
        gap=args.gap,
    )


def main() -> None:
    validate()
    config = parse_args()
    paths = render(config)
    print(
        f"validated exact {WIDTH}x{HEIGHT} Timepiece; gamma={config.gamma:g}, "
        f"brightness={config.brightness}, saturation={config.saturation}"
    )
    for path in paths:
        print(path)


if __name__ == "__main__":
    main()
