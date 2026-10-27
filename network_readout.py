"""Receive-throughput semantics and exact 32x8 artwork, without I/O."""
from dataclasses import dataclass
import math

import visual_system as visual

DEFAULT_FULL_SCALE_MIB_S = 100.0
DEFAULT_REFRESH_DELTA_MIB_S = 3.0
MAX_SAMPLE_MIB_S = 10_000.0


@dataclass(frozen=True)
class NetworkReading:
    text: str
    unit: str
    # Quantized to the visible label, expressed in the sampler's base unit.
    mib_s: float | None
    rail_fill: int


def reading(value, full_scale_mib_s=DEFAULT_FULL_SCALE_MIB_S):
    if (isinstance(full_scale_mib_s, bool) or not isinstance(full_scale_mib_s, (int, float))
            or not math.isfinite(full_scale_mib_s) or not 0 < full_scale_mib_s <= MAX_SAMPLE_MIB_S):
        raise ValueError('Network full scale must be positive and at most 10000 MiB/s')
    try:
        rate = float(value) if not isinstance(value, bool) else math.nan
    except (TypeError, ValueError, OverflowError):
        rate = math.nan
    if not math.isfinite(rate):
        return NetworkReading('--', 'MiB/s', None, 0)
    rate = max(0.0, rate)
    if rate < 1024:
        text, unit, factor = f'{rate:.1f}', 'MiB/s', 1
    else:
        text, unit, factor = f'{rate / 1024:.2f}', 'GiB/s', 1024
    if len(text) > 6:
        text = '999+'
        visible_rate = 999.0 * factor
    else:
        visible_rate = float(text) * factor
    fraction = min(1.0, rate / full_scale_mib_s)
    fill = 0 if rate == 0 else max(1, min(23, round(fraction * 23)))
    return NetworkReading(text, unit, visible_rate, fill)


def render_lane(model: NetworkReading):
    """Value and explicit binary unit are visible together, with one rich rail."""
    frame = visual.render_split_lane(model.text, model.unit, bottom_color=visual.MIST)
    visual.draw_level_rail(frame, model.rail_fill / 23 if model.rail_fill else None, 'info')
    return frame


def draw_receive_icon(frame, phase):
    """Shaded receive tray and a packet highlight descending into it."""
    body, shade, light = (52, 176, 232), (28, 88, 144), (196, 239, 255)
    for x in range(8):
        frame[4][x] = light if x < 4 else body
        frame[7][x] = shade
    for y in (5, 6):
        frame[y][0], frame[y][7] = body, shade
        for x in range(1, 7):
            frame[y][x] = (18, 46, 74)
    for x in range(2, 6):
        frame[6][x] = visual.GOLD
    arrow = {(3, 0), (4, 0), (3, 1), (4, 1), (3, 4), (4, 4)}
    arrow.update((x, 2) for x in range(1, 7))
    arrow.update((x, 3) for x in range(2, 6))
    for x, y in arrow:
        frame[y][x] = visual.MINT
    step = int(phase) % 6
    y = min(step, 4)
    mix = (.55, 1.0, 1.0, 1.0, 1.0, .25)[step]
    for x in (3, 4):
        frame[y][x] = visual.gamma_lerp(visual.MINT, visual.PEARL, mix)
        if 0 < step < 5:
            frame[y - 1][x] = visual.gamma_lerp(visual.MINT, visual.PEARL, .25)
