r"""
Show PC CPU/RAM on AWTRIX only while a reading is meaningfully elevated.

CPU and RAM readings exist only on this PC, so this script must run here and
send alerts to the panel. During normal use it stays quiet and occupies no
place in the AWTRIX rotation.

SETUP
    C:\Users\devyn\anaconda3\python.exe -m pip install psutil requests pillow

RUN
    C:\Users\devyn\anaconda3\python.exe pc_stats_push.py

STOP
    Ctrl+C. Any visible alert disappears on its own within 45 seconds.

RUN IT AUTOMATICALLY AT LOGIN (optional)
    Task Scheduler -> Create Task
      General : "AWTRIX PC stats", check "Run whether user is logged on or not"
      Triggers: New -> At log on
      Actions : Start a program
                Program:   C:\Users\devyn\anaconda3\pythonw.exe
                Arguments: "<full path to this file>"
"""

import base64
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
from functools import lru_cache
from io import BytesIO
import json
import math
import sys
import time

try:
    import psutil
    import requests
    from PIL import Image
except ImportError:
    sys.exit(
        "Missing dependencies. Run:  "
        r"C:\Users\devyn\anaconda3\python.exe -m pip install psutil requests pillow"
    )

import panel_gate
from visual_system import (
    CORAL, GOLD, ICE, MINT, PEARL, RGB, VIOLET, Frame, draw_comet, gamma_lerp, render_glance_lane,
    scale_color,
)
from pc_stats_preview import (
    BLACK,
    DIAL_BEZEL,
    EMERALD,
    GPU_SHOW_AT,
    GUTTER_X,
    HEIGHT,
    ICE,
    LANE_X0,
    LANE_WIDTH,
    METRIC_COLORS,
    NET_SHOW_AT,
    WIDTH,
    combined_frame,
    draw_combined_jewel,
    draw_cpu_jewel,
    draw_gpu_jewel,
    draw_net_jewel,
    draw_ram_jewel,
    new_frame,
    panel_frame,
    severity_level,
)

try:
    from pc_readouts import sample_gpu
except ImportError:
    sample_gpu = None

# ---------------------------------------------------------------- config
# mDNS advertises a stale second address on this LAN. Keep the proven raw IP.
AWTRIX = "http://192.168.1.90"
APP_NAME = "pcstats0"
GATE_SOURCE = "pcstats"
INTERVAL = 5  # seconds between readings
REQUEST_TIMEOUT = 5

# Panel writes follow what the page shows, not a clock (2026-10-09). The
# panel's largest free block drains with this sender's writes, and the journal
# had it re-pushing an unchanged page every 10 s for whole alerts. A visible
# page is now rewritten only when
#   - it appears, goes, draws a different metric (or the CPU+RAM jewel instead
#     of one), or animation is switched on or off (at once, as before). A
#     metric joining or leaving the active set beside the one the page draws
#     -- NET crossing 10 MB/s next to a CPU alert -- changes no pixel, so it is
#     recorded without a write (relabelled_in_place);
#   - its icon level or its rail colour changes, or the percent it prints has
#     moved REFRESH_DELTA_POINTS from what the panel is showing, on
#     REFRESH_CONFIRM_SAMPLES readings in a row -- no sooner than
#     PUSH_INTERVAL after the last write;
#   - or its keep-alive is due (keepalive_due, below LIFETIME_MS).
# A page held after its alert cleared (MIN_DISPLAY_SECONDS) is never
# rewritten at all, except to drop to static icons.
PUSH_INTERVAL = 10  # a change of value rewrites a visible page at most this often
# The same two-reading debounce MetricGate applies before an alert appears.
# A change that lasts is shown at most one INTERVAL after the first reading
# that sees it, which is the old fixed 10 s cadence's worst case too. What it
# drops is a single-reading blip: one noisy sample across a band edge or 3
# points off, which the old timer painted whenever the two coincided and then
# left on screen for 10 s after the load had already gone back.
REFRESH_CONFIRM_SAMPLES = 2
# 3 points: the lane prints an integer and a 23-pixel rail (4.3 points a
# pixel), so a smaller move changes the number and at most one rail pixel.
# psutil's 5 s CPU reading jitters by 1-2 points under a steady load, and 3 is
# the smallest step that ignores that. It is under the narrowest band (poor
# and bad are 5 points wide), and a band crossing pushes regardless, so it can
# never hide a colour or icon change. The comparison is against the value the
# panel shows, not the previous sample, so a slow creep cannot drift further
# than 2 points from the truth unseen.
REFRESH_DELTA_POINTS = 3

# A reading must cross its show threshold twice in a row before appearing.
# Once visible, the lower clear threshold prevents the page flickering on/off.
CPU_SHOW_AT = 80
CPU_CLEAR_BELOW = 65
RAM_SHOW_AT = 85
RAM_CLEAR_BELOW = 75
GPU_CLEAR_BELOW = 60
NET_CLEAR_BELOW = 8.0
TRIGGER_SAMPLES = 2
CLEAR_SAMPLES = 2

# The panel drops an alert this long after its last successful refresh, so a
# stale warning cannot outlive the script that raised it.
LIFETIME_MS = 60_000
PAGE_DURATION_MS = 7_000
# An unchanged page is rewritten late enough to save writes and early enough
# that one lost keep-alive still has a full retry before the page expires.
# A lost write costs REQUEST_TIMEOUT; the loop then sleeps INTERVAL and spends
# its sampling overhead (psutil, and nvidia-smi, whose own timeout is 5 s)
# before the retry, which needs its own REQUEST_TIMEOUT to land before
# LIFETIME_MS. keepalive_due therefore sends the keep-alive at the last reading
# from which waiting one more reading would break that:
#     since_push >= KEEPALIVE_SECONDS - 2 * overhead
# With no overhead that is 45 - 2*5 - 2*5 = 25 s, and below about 0.75 s of
# overhead it sends exactly what a fixed 25 s did. Up to 12 s of overhead the
# keep-alive lands before 30 s (two thirds of the lifetime) and its retry
# starts before 40 s. A fixed 25 s threshold met that only by luck: with 3.0 to
# 3.3 s or 5 s of overhead per reading the retry started at 45 s and one lost
# keep-alive let the page lapse mid-alert. test_refresh_outruns_the_lifetime_*
# and test_one_lost_keepalive_never_costs_the_page_at_any_loop_overhead check it.
#
# This is a trade made on purpose. Pushing every 10 s left room for three
# retries; the keep-alive leaves room for one. A single lost write never costs
# the page, and no dropout of 10 s or less does. A longer dropout that spans
# both the keep-alive and its retry lets the page lapse, and it is re-created
# when the link returns: a remove and a create, the churn the hold below is
# there to avoid. Measured on main() with a fake clock (steady alert, 0.15 s
# overhead, 5 s timeouts, dropouts starting at 600 phases over 300 s), the page
# lapsed for 0% of 10 s dropouts, 4% of 12 s, 10% of 15 s, 26% of 20 s and 44%
# of 25 s. The old 10 s cadence lost it for none of them. panel_health.jsonl
# held five short unreachable runs (one or two probes) in 4.8 days, and a PC
# alert is up for a small share of the day. If that is too often, the lever is
# LIFETIME_MS; keep-alives every ~15 s would do it too, at two thirds more
# writes for a steady alert.
#
# 2026-10-09: LIFETIME_MS went from 45 s to 60 s (the user's choice) and the
# keep-alive stayed at the 25 s its write savings were measured at, so the
# extra 15 s is a second retry rather than fewer writes. The cost is that a
# page whose sender has stopped lingers up to 15 s longer. Re-measured the same
# way (120 dropout phases each, 0.15 s overhead): no dropout of up to 25 s
# lapses the page any more (it was 4-44% at 45 s); 30 s lapses it 16% of the
# time. The lapse figures in the paragraph above are the 45 s lifetime's.
KEEPALIVE_SECONDS = 45 - 2 * REQUEST_TIMEOUT - 2 * INTERVAL
# The loop's overhead is the slowest sampling time over this many recent
# readings (about a minute), so an intermittent nvidia-smi stall still counts.
OVERHEAD_WINDOW = 12
# Once up, a page stays at least this long, and an alert that re-triggers
# inside the window carries on on the same page. The journal's flaps
# (2026-10-07 21:53, 21:59, 22:08, 22:20) each lived exactly
# CLEAR_SAMPLES * INTERVAL = 10 s, push then delete. That is shorter than two
# 7 s rotation slots, and in quiet hours, with no activation, such a page can
# be created and destroyed without ever coming up in rotation. 30 s is three of
# those lifetimes, so a spike that dips for up to 20 s and comes back stays
# one page instead of a destroy and a re-create. It is under the 45 s
# lifetime, so a held page never needs a keep-alive of its own, and it keeps a
# cleared alert on screen at most 20 s longer than before.
#
# A held page is left exactly as the panel has it: it keeps showing the
# reading that raised the alert until it goes, as an alert should, and no
# write is spent on a page that is about to be deleted. Repainting it with the
# falling reading cost a third write per isolated flap and cancelled most of
# what the value-change refresh saves under oscillating load.
MIN_DISPLAY_SECONDS = 30
# Failed panel writes back off from INTERVAL, doubling up to this, and reset on
# the first success. That covers a panel that did not answer (the 2026-10-07
# 20:29-21:50 outage was retried every ~11 s, 368 times) and one that answered
# and refused: a heap-starved panel answering 507 is where a write costs most.
# The first retry still comes on the next reading, so a refused animated page
# falls back to static as quickly as before.
#
# While writes wait out an unreachable panel and a page is wanted, the
# read-only GET /device probe runs on every reading instead of every
# HEALTH_PROBE_SECONDS, and its first answer ends the wait. The page comes
# back within about REQUEST_TIMEOUT + INTERVAL of the panel, as it did when
# every reading retried, and the probes cost the panel nothing while it is
# unreachable (each one does cost that reading REQUEST_TIMEOUT). A
# probe that answers lifts the wait once; if the write after it is lost
# anyway, only a probe that fails first can lift it again, so a panel that
# answers GETs and still drops writes gets the backoff, not a write a reading.
RETRY_MAX_SECONDS = 60

ICON_WIDTH = 8
ICON_HEIGHT = 8
ICON_FRAME_COUNT = 6
ICON_MAX_BYTES = 1_024
# The panel renders on a 24ms tick and GIF delays quantise to 10ms, so the two
# first coincide at 120ms. Every other sender here is built on that beat; this
# one was not, and its 110ms step landed between ticks, holding some frames for
# four and some for five and making the jewel stutter. Multiples of 120 only.
FRAME_BEAT_MS = 120
ICON_STEP_MS = FRAME_BEAT_MS
ICON_HOLD_MS = FRAME_BEAT_MS * 15
HARDWARE_STEP_MS = FRAME_BEAT_MS * 2
LANE_HEIGHT = 8
HEALTH_PROBE_SECONDS = 30

# A page the panel refuses is evidence that animation is too heavy for it right
# now. A dropped packet is not. A refusal holds static icons for this long,
# doubling each time, and then one animated probe decides whether to stay.
STATIC_HOLD_SECONDS = 300
STATIC_HOLD_MAX_SECONDS = 3_600
# A page the panel accepts is not yet proof it can render one. The 200 only
# means the page was stored; the GIF is decoded later, when the app next comes
# up in rotation, so an accept-then-reboot cycle answers 200 and then falls
# over. Keep the backoff at its current rung until the panel has stayed up long
# enough for such a reboot to have shown up in a health probe.
ANIMATION_PROBATION_SECONDS = 180

URL = f"{AWTRIX}/api/v1/apps/pushed/{APP_NAME}"
DELETE_URL = f"{AWTRIX}/api/v1/apps/{APP_NAME}"
# One-time migration cleanup for the old array sender that created
# pcstats0/pcstats1 children from the base name. Deleting the base name does
# not take the children with it, and this sender only ever names pcstats0, so
# a pushed page left under pcstats1 had nothing to remove it.
#
# Note this clears a pushed *page*, not the slot row. An emptied slot stays in
# the listing as present:false/origin:null, and DELETE answers ok without
# removing it -- those rows belong to the panel's global app order, which is
# not this sender's to rewrite. They render nothing.
LEGACY_DELETE_URL = f"{AWTRIX}/api/v1/apps/pcstats"
LEGACY_CHILD_URLS = (f"{AWTRIX}/api/v1/apps/pcstats1",)
DEVICE_URL = f"{AWTRIX}/api/v1/device"
ACTIVE_URL = f"{AWTRIX}/api/v1/apps/active"
QUIET_START_MINUTE = 21 * 60 + 30
QUIET_END_MINUTE = 7 * 60
# Foregrounding the panel is an interruption, so rate-limit it. A bursty load
# crossing 80 and falling back under 65 re-triggers the rising edge every few
# samples, and without this the panel jumped to the alert each time.
ACTIVATION_COOLDOWN_SECONDS = 90
# -----------------------------------------------------------------------


def _valid_metric_value(value: float, max_val: float = 100.0) -> float | None:
    """Return a clamped value in range 0..max_val, or None when a sample is unusable."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return max(0.0, min(max_val, value))


def _valid_percent(value: float) -> float | None:
    """Return a clamped percentage, or None when a sample is unusable."""
    return _valid_metric_value(value, 100.0)


def normalize_percent(value: float) -> float:
    """Return a render-safe percentage in the range 0..100."""
    normalized = _valid_percent(value)
    return 0.0 if normalized is None else normalized


# Calm glance (2026-09-26).  The icon says WHICH resource by silhouette --
# a chip, a memory stick, a graphics card -- where the old icons were three
# dials told apart only by colour, which collapses at brightness 1.  The lane
# says how much, in white, with one meter on row 7.
# The chips (2026-10-03, to the Now Playing standard): shaded objects with
# purposeful motion instead of flat shapes with one wandering pixel.
#   CPU  a package lit top-left and shadowed bottom-right, a core glowing in
#        the load colour (pulsing faster under load), and a data comet running
#        round the gold pins like the record's groove highlight
#   RAM  a module with three chips, a comet along the gold contacts, and an
#        activity LED in the load colour
#   GPU  a card whose fan visibly spins (+ then x, with a motion trail)
# The hardware icons loop on six 240ms beats: the moving light stays inside
# the 8x8 object while the value and load rail remain completely still.

CHIP_LIT = (80, 218, 255)
CHIP_SHADE = (132, 84, 204)
CHIP_BODY = (44, 98, 190)
PIN_GOLD = (255, 200, 80)
RAM_TOP = (244, 130, 255)
RAM_BOARD = (145, 56, 212)
RAM_CHIP = (178, 212, 255)
RAM_CHIP_SHADE = (82, 104, 184)
GPU_SHROUD = (52, 244, 162)
GPU_SHROUD_SHADE = (32, 184, 110)
GPU_HUB = (236, 240, 250)
CPU_PINS = ((2, 0), (5, 0), (7, 2), (7, 5), (5, 7), (2, 7), (0, 5), (0, 2))
CPU_EDGE = (tuple((x, 1) for x in range(1, 7)) +
            tuple((6, y) for y in range(2, 7)) +
            tuple((x, 6) for x in range(5, 0, -1)) +
            tuple((1, y) for y in range(5, 1, -1)))
FAN_RING = ((3, 1), (4, 1), (5, 2), (6, 3), (6, 4), (5, 5),
            (4, 6), (3, 6), (2, 5), (1, 4), (1, 3), (2, 2))
FAN_PLUS = (((3, 1), (4, 1), (3, 2), (4, 2)), ((5, 3), (6, 3), (5, 4), (6, 4)),
            ((3, 5), (4, 5), (3, 6), (4, 6)), ((1, 3), (2, 3), (1, 4), (2, 4)))
FAN_CROSS = (((1, 1), (2, 2)), ((6, 1), (5, 2)), ((6, 6), (5, 5)), ((1, 6), (2, 5)))


def load_colour(value: float) -> RGB:
    """Cool ice -> mint -> gold -> coral as the load climbs."""
    load = normalize_percent(value) / 100.0
    if load < 0.8:
        return gamma_lerp(ICE, MINT, load / 0.8)
    if load < 0.92:
        return gamma_lerp(MINT, GOLD, (load - 0.8) / 0.12)
    return gamma_lerp(GOLD, CORAL, min(1.0, (load - 0.92) / 0.06))


def _draw_cpu(frame: Frame, value: float, phase: int) -> None:
    for y in range(2, 6):
        for x in range(2, 6):
            frame[y][x] = CHIP_BODY
    for x in range(1, 7):
        frame[1][x] = CHIP_LIT
        frame[6][x] = CHIP_SHADE
    for y in range(2, 6):
        frame[y][1] = CHIP_LIT
        frame[y][6] = CHIP_SHADE
    frame[1][6], frame[6][1] = CHIP_LIT, CHIP_SHADE
    hot = load_colour(value)
    pulse = 0.89 + 0.11 * math.cos(2 * math.pi * phase / ICON_FRAME_COUNT)
    for x, y in ((3, 3), (4, 3), (3, 4), (4, 4)):
        frame[y][x] = scale_color(hot, pulse)
    for x, y in CPU_PINS:
        frame[y][x] = PIN_GOLD
    # A continuous rim glint circles the blue package without a reset jump.
    for trail, mix in enumerate((1.0, 0.55, 0.2)):
        x, y = CPU_EDGE[(phase * len(CPU_EDGE) // ICON_FRAME_COUNT - trail) % len(CPU_EDGE)]
        frame[y][x] = gamma_lerp(frame[y][x], PEARL, mix)


def _draw_ram(frame: Frame, value: float, phase: int) -> None:
    for x in range(8):
        frame[1][x] = RAM_TOP
        frame[2][x] = frame[3][x] = frame[4][x] = frame[5][x] = RAM_BOARD
    for cx in (0, 3, 6):
        for x in (cx, cx + 1):
            if x < 8:
                frame[2][x] = RAM_CHIP
                frame[3][x] = RAM_CHIP_SHADE
    contacts = tuple((x, 6) for x in range(0, 8, 2))
    for x, y in contacts:
        frame[y][x] = PIN_GOLD
    # Data light sweeps through each chip then returns along its lower edge.
    # The two rows make all six frames distinct, including the turnaround.
    position = phase % ICON_FRAME_COUNT
    chip = (0, 1, 2, 2, 1, 0)[position]
    row = 2 if position < 3 else 3
    hot = load_colour(value)
    frame[row][chip * 3] = PEARL
    frame[row][chip * 3 + 1] = hot
    for x in range(8):
        frame[5][x] = gamma_lerp(RAM_BOARD, hot, 0.3 if x // 3 != chip else 0.75)


def _draw_gpu(frame: Frame, value: float, phase: int) -> None:
    for x in range(1, 7):
        frame[0][x] = GPU_SHROUD
        frame[7][x] = GPU_SHROUD_SHADE
    for y in range(1, 7):
        frame[y][0] = GPU_SHROUD
        frame[y][7] = GPU_SHROUD_SHADE
    hot = load_colour(value)
    now, before = (FAN_PLUS, FAN_CROSS) if phase % 2 == 0 else (FAN_CROSS, FAN_PLUS)
    for blade in before:  # a trail of where the blades just were
        x, y = blade[0]
        frame[y][x] = (36, 96, 112)
    for blade in now:
        for x, y in blade:
            frame[y][x] = gamma_lerp(GPU_SHROUD, ICE, 0.25)
    # Two warm blade tips chase around the fan, giving its alternating poses
    # a clear direction and retaining the cool green graphics-card identity.
    for offset in (0, 6):
        for trail, mix in enumerate((1.0, 0.45)):
            x, y = FAN_RING[(phase * 2 + offset - trail) % len(FAN_RING)]
            frame[y][x] = gamma_lerp(hot, PEARL, mix)
    for x, y in ((3, 3), (4, 3), (3, 4), (4, 4)):
        frame[y][x] = GPU_HUB


PC_DRAWERS = {"CPU": _draw_cpu, "RAM": _draw_ram, "GPU": _draw_gpu}


def _icon_frame(label: str, value: float, phase: int, other: float | None = None) -> Image.Image:
    frame = new_frame()
    if label in PC_DRAWERS:
        PC_DRAWERS[label](frame, value, phase)
    elif label == "NET":
        draw_net_jewel(frame, value, phase)
    elif label == "BOTH" and other is not None:
        # Keep both load needles truthful and still, with two colours of light
        # orbiting the bezel so a mixed CPU/RAM alert also stays animated.
        draw_combined_jewel(frame, value, other, ICON_FRAME_COUNT - 1)
        path = tuple(sorted(DIAL_BEZEL, key=lambda p: math.atan2(p[1] - 3.5, p[0] - 3.5)))
        head = phase * len(path) // ICON_FRAME_COUNT
        for index, (x, y) in enumerate(path):
            frame[y][x] = gamma_lerp(PIN_GOLD, VIOLET, (y + x) / 18)
        for offset, accent in ((0, CORAL), (len(path) // 2, VIOLET)):
            for trail, mix in enumerate((1.0, 0.55, 0.2)):
                x, y = path[(head + offset - trail) % len(path)]
                frame[y][x] = gamma_lerp(accent, PEARL, mix)
    else:
        raise ValueError(f"unsupported PC metric: {label}")
    image = Image.new("RGB", (ICON_WIDTH, ICON_HEIGHT), BLACK)
    image.putdata([frame[y][x] for y in range(ICON_HEIGHT) for x in range(ICON_WIDTH)])
    return image


def glance_level(percent: float) -> str:
    if percent < 80:
        return "good"
    if percent < 90:
        return "fair"
    if percent < 95:
        return "poor"
    return "bad"


def glance_lane(percent: float) -> list:
    percent = int(round(normalize_percent(percent)))
    return render_glance_lane(f"{percent}%", meter=percent / 100, level=glance_level(percent))


def _encode_loop(frames: list[Image.Image], **save: object) -> bytes:
    """Save frames as one GIF with a single exact palette shared by every frame.

    Per-frame colour tables doubled the icons: the combined CPU+RAM jewel was
    957 bytes this way and 464 shared, which is what keeps that page under the
    2 KB push budget. No colour is quantized; the weather and AQI icons use the
    same scheme.
    """
    colours = list(dict.fromkeys(colour for frame in frames for colour in frame.getdata()))
    indices = {colour: index for index, colour in enumerate(colours)}
    palette = [channel for colour in colours for channel in colour]
    indexed = []
    for frame in frames:
        image = Image.new("P", frame.size)
        image.putpalette(palette)
        image.putdata([indices[colour] for colour in frame.getdata()])
        indexed.append(image)
    stream = BytesIO()
    indexed[0].save(
        stream,
        format="GIF",
        save_all=True,
        append_images=indexed[1:],
        disposal=1,
        palette=palette,
        optimize=True,
        **save,
    )
    data = stream.getvalue()
    validate_icon_gif(data)
    return data


@lru_cache(maxsize=12)
def _animated_icon(label: str, level: int) -> bytes:
    representative = (85.0, 92.0, 98.0)[level]
    hardware = label in PC_DRAWERS
    frame_count = ICON_FRAME_COUNT if hardware else level + 4
    frames = [_icon_frame(label, representative, phase) for phase in range(frame_count)]
    return _encode_loop(
        frames,
        duration=([HARDWARE_STEP_MS] * frame_count if hardware else
                  [ICON_STEP_MS] * (frame_count - 1) + [ICON_HOLD_MS]),
        **({"loop": 0} if hardware else {}),
    )


@lru_cache(maxsize=9)
def _combined_animated_icon(cpu_level: int, ram_level: int) -> bytes:
    cpu = (85.0, 92.0, 98.0)[cpu_level]
    ram = (85.0, 92.0, 98.0)[ram_level]
    frame_count = ICON_FRAME_COUNT
    frames = [_icon_frame("BOTH", cpu, phase, other=ram) for phase in range(frame_count)]
    return _encode_loop(frames, duration=[HARDWARE_STEP_MS] * frame_count, loop=0)


@lru_cache(maxsize=12)
def _static_icon(label: str, level: int) -> bytes:
    representative = (85.0, 92.0, 98.0)[level]
    stream = BytesIO()
    _icon_frame(label, representative, ICON_FRAME_COUNT - 1).save(
        stream, format="GIF", optimize=True
    )
    data = stream.getvalue()
    validate_icon_gif(data)
    return data


@lru_cache(maxsize=9)
def _combined_static_icon(cpu_level: int, ram_level: int) -> bytes:
    cpu = (85.0, 92.0, 98.0)[cpu_level]
    ram = (85.0, 92.0, 98.0)[ram_level]
    stream = BytesIO()
    _icon_frame("BOTH", cpu, ICON_FRAME_COUNT - 1, other=ram).save(
        stream, format="GIF", optimize=True
    )
    data = stream.getvalue()
    validate_icon_gif(data)
    return data


def validate_icon_gif(data: bytes) -> None:
    if len(data) > ICON_MAX_BYTES:
        raise ValueError(f"PC alert icon is {len(data)} bytes; limit is {ICON_MAX_BYTES}")
    if not data.startswith((b"GIF87a", b"GIF89a")):
        raise ValueError("PC alert icon is not a GIF")
    try:
        with Image.open(BytesIO(data)) as image:
            if image.size != (ICON_WIDTH, ICON_HEIGHT):
                raise ValueError("PC alert icon must be exactly 8x8")
            if not 1 <= image.n_frames <= ICON_FRAME_COUNT:
                raise ValueError("PC alert icon has too many frames")
            if "transparency" in image.info:
                raise ValueError("PC alert icon must be fully opaque")
            # Checked against the beat itself rather than against the constants
            # above, so that changing a constant to an off-beat value fails here
            # instead of quietly shipping a stutter to the panel.
            for index in range(image.n_frames):
                image.seek(index)
                duration = int(image.info.get("duration", 0))
                if duration % FRAME_BEAT_MS:
                    raise ValueError(
                        f"PC alert icon frame {index} runs {duration}ms, off the "
                        f"{FRAME_BEAT_MS}ms beat; it would judder"
                    )
    except OSError as error:
        raise ValueError("PC alert icon is not readable") from error


def build_icon_gif(label: str, value: float, animated: bool = True) -> bytes:
    label = label.upper()
    level = severity_level(value)
    return _animated_icon(label, level) if animated else _static_icon(label, level)


def _lane_bytes(
    label: str,
    value: float,
    history: tuple[float, ...] | None = None,
) -> bytes:
    frame = glance_lane(value)
    assert all(frame[y][GUTTER_X] == BLACK for y in range(HEIGHT))
    return bytes(
        channel
        for row in frame
        for color in row[LANE_X0 : LANE_X0 + LANE_WIDTH]
        for channel in color
    )


def _combined_lane_bytes(
    cpu: float,
    ram: float,
    cpu_history: tuple[float, ...] | None = None,
    ram_history: tuple[float, ...] | None = None,
) -> bytes:
    # Both elevated: the combined dial icon shows both needles; the lane
    # reports the worse of the two, since that is the one to act on.
    frame = glance_lane(max(cpu, ram))
    assert all(frame[y][GUTTER_X] == BLACK for y in range(HEIGHT))
    return bytes(
        channel
        for row in frame
        for color in row[LANE_X0 : LANE_X0 + LANE_WIDTH]
        for channel in color
    )


def metric_payload(
    label: str,
    value: float,
    animated: bool = True,
    history: tuple[float, ...] | None = None,
) -> dict:
    """Build one stable data lane plus a tiny locally animated metric jewel."""
    label = label.upper()
    value = normalize_percent(value)
    icon = build_icon_gif(label, value, animated=animated)
    lane = base64.b64encode(_lane_bytes(label, value, history)).decode("ascii")
    payload = {
        "icon": base64.b64encode(icon).decode("ascii"),
        "iconMode": "fixed",
        "iconOffsetX": 0,
        "draw": [["bitmap", LANE_X0, 0, LANE_WIDTH, LANE_HEIGHT, lane]],
        "backgroundColor": "#000000",
        "durationMs": PAGE_DURATION_MS,
        "lifetimeMs": LIFETIME_MS,
        "lifetimeExpiry": "remove",
    }
    if len(json.dumps(payload, separators=(",", ":")).encode("utf-8")) >= 2_048:
        raise ValueError("PC alert page exceeded the 2KB safety budget")
    return payload


def combined_payload(
    cpu: float,
    ram: float,
    animated: bool = True,
    cpu_history: tuple[float, ...] | None = None,
    ram_history: tuple[float, ...] | None = None,
) -> dict:
    cpu = normalize_percent(cpu)
    ram = normalize_percent(ram)
    levels = (severity_level(cpu), severity_level(ram))
    icon = (
        _combined_animated_icon(*levels)
        if animated
        else _combined_static_icon(*levels)
    )
    lane = base64.b64encode(
        _combined_lane_bytes(cpu, ram, cpu_history, ram_history)
    ).decode("ascii")
    payload = {
        "icon": base64.b64encode(icon).decode("ascii"),
        "iconMode": "fixed",
        "iconOffsetX": 0,
        "draw": [["bitmap", LANE_X0, 0, LANE_WIDTH, LANE_HEIGHT, lane]],
        "backgroundColor": "#000000",
        "durationMs": PAGE_DURATION_MS,
        "lifetimeMs": LIFETIME_MS,
        "lifetimeExpiry": "remove",
    }
    if len(json.dumps(payload, separators=(",", ":")).encode("utf-8")) >= 2_048:
        raise ValueError("combined PC alert page exceeded the 2KB safety budget")
    return payload


def build_payload(
    metrics: list[tuple[str, float]],
    animated: bool = True,
    histories: dict[str, tuple[float, ...]] | None = None,
) -> dict:
    """Create exactly one page, even when multiple metrics are elevated."""
    values = {label.upper(): value for label, value in metrics}
    histories = histories or {}
    if not values:
        raise ValueError("PC alert payload requires an active metric")
    if not set(values).issubset({"CPU", "RAM", "GPU", "NET"}):
        raise ValueError("unsupported PC metric")

    face = _page_face(values)
    if face == COMBINED_FACE:
        return combined_payload(
            values["CPU"],
            values["RAM"],
            animated=animated,
            cpu_history=histories.get("CPU"),
            ram_history=histories.get("RAM"),
        )
    return metric_payload(
        face,
        values[face],
        animated=animated,
        history=histories.get(face),
    )


COMBINED_FACE = "BOTH"


def _page_face(values: dict[str, float]) -> str:
    """Which metric the one page draws, or COMBINED_FACE for the CPU+RAM jewel.

    build_payload and page_view both choose through here, so the refresh logic
    can never disagree with the page about what is on screen.
    """
    if len(values) == 1:
        return next(iter(values))
    if set(values) == {"CPU", "RAM"}:
        return COMBINED_FACE
    # When multiple metrics are active, prioritize by severity
    if "GPU" in values and values["GPU"] >= 90:
        return "GPU"
    if "CPU" in values and "RAM" in values and "GPU" not in values:
        return COMBINED_FACE
    return max(values.keys(), key=lambda k: values[k])


@dataclass(frozen=True)
class PageView:
    """Everything a pushed page shows, at the resolution a viewer can see.

    Together with the animation mode these fields fix every byte of the
    payload: the icon is chosen by face and severity level, and the lane is
    drawn from the integer percent alone (its rail colour is lane_level).
    """

    labels: tuple[str, ...]
    face: str
    icon_levels: tuple[int, ...]
    lane_level: str
    percent: int


def page_view(metrics: list[tuple[str, float]]) -> PageView:
    """What build_payload would draw for these metrics."""
    values = {label.upper(): value for label, value in metrics}
    if not values:
        raise ValueError("a page view requires an active metric")
    face = _page_face(values)
    if face == COMBINED_FACE:
        cpu = normalize_percent(values["CPU"])
        ram = normalize_percent(values["RAM"])
        icon_levels = (severity_level(cpu), severity_level(ram))
        lane_value = max(cpu, ram)
    else:
        lane_value = normalize_percent(values[face])
        icon_levels = (severity_level(lane_value),)
    percent = int(round(normalize_percent(lane_value)))
    return PageView(
        labels=tuple(label for label, _ in metrics),
        face=face,
        icon_levels=icon_levels,
        lane_level=glance_level(percent),
        percent=percent,
    )


@dataclass
class MetricGate:
    """Debounce a high reading and add hysteresis before clearing it."""

    show_at: float
    clear_below: float
    trigger_samples: int = TRIGGER_SAMPLES
    clear_samples: int = CLEAR_SAMPLES
    max_value: float = 100.0
    active: bool = False
    high_samples: int = 0
    low_samples: int = 0
    last_value: float | None = None
    missing_samples: int = 0

    def __post_init__(self) -> None:
        if self.clear_below >= self.show_at:
            raise ValueError("clear_below must be lower than show_at")
        if self.trigger_samples < 1 or self.clear_samples < 1:
            raise ValueError("sample counts must be positive")

    def update(self, value: float | None) -> bool:
        value = _valid_metric_value(value, self.max_value)
        if value is None:
            # A sensor glitch is not evidence that load rose or fell. Keep the
            # current state, the last good value and both debounce streaks
            # exactly as they were, so one failed nvidia-smi call does not
            # delete a GPU alert and re-create it a reading later. But only for
            # as long as clearing would take: a sensor still silent after
            # clear_samples readings (nvidia-smi gone for good) is no evidence
            # for the alert either, and must not hold a frozen value up for ever.
            self.missing_samples += 1
            if self.active and self.missing_samples > self.clear_samples:
                self.active = False
                self.high_samples = 0
                self.low_samples = 0
            return self.active
        self.missing_samples = 0
        self.last_value = value

        if self.active:
            self.high_samples = 0
            if value < self.clear_below:
                self.low_samples += 1
                if self.low_samples >= self.clear_samples:
                    self.active = False
                    self.low_samples = 0
            else:
                self.low_samples = 0
            return self.active

        self.low_samples = 0
        if value >= self.show_at:
            self.high_samples += 1
            if self.high_samples >= self.trigger_samples:
                self.active = True
                self.high_samples = 0
        else:
            self.high_samples = 0
        return self.active


@dataclass(frozen=True)
class PanelHealth:
    uptime_seconds: int
    reset_reason: str


@dataclass
class AnimationGuard:
    """Decide whether icons animate, and recover once the panel proves they can.

    v1 latched static icons after five consecutive sync failures and never
    unlatched. Five failures is twenty-five seconds of unreachable panel, which
    on this LAN is an ordinary WiFi dropout and says nothing about whether the
    panel can render a GIF -- so a routine blip permanently downgraded the art
    until the PC was restarted, and nothing ever reported that it had.

    Only the panel answering with an error is evidence against animation, and so
    is a reboot right after an animated push landed. Either holds static icons
    at once. A transport failure holds nothing. The hold then expires into a
    single animated probe: if the panel takes it the guard clears, and if it
    refuses again the wait doubles, up to the cap.
    """

    hold_seconds: float = STATIC_HOLD_SECONDS
    max_hold_seconds: float = STATIC_HOLD_MAX_SECONDS
    probation_seconds: float = ANIMATION_PROBATION_SECONDS
    held_until: float | None = None
    probation_until: float | None = None
    wait_seconds: float = 0.0
    probing: bool = False

    @property
    def holding(self) -> bool:
        return self.held_until is not None

    def animate(self, now: float) -> bool:
        """Whether the next push may animate, recording probes so success clears."""
        self.probing = self.held_until is not None and now >= self.held_until
        return self.held_until is None or self.probing

    def refused(self, now: float) -> None:
        """The panel answered and would not take the page, or it rebooted."""
        self.wait_seconds = (
            self.hold_seconds
            if self.wait_seconds <= 0
            else min(self.wait_seconds * 2, self.max_hold_seconds)
        )
        self.held_until = now + self.wait_seconds
        self.probation_until = None
        self.probing = False

    def unreachable(self) -> None:
        """The panel never answered. That is the LAN's problem, not the GIF's."""

    def succeeded(self, now: float) -> None:
        """An animated page landed. Resume animation, but keep the backoff.

        Clearing the ladder here was wrong: the panel stores a page and decodes
        its GIF later, so a probe that is accepted and then brings the panel
        down still reports 200 first. Zeroing on that 200 restarted the wait at
        its shortest rung every time, which would have rebooted the panel every
        few minutes forever instead of backing off toward the cap.
        """
        if not self.probing:
            return
        self.probing = False
        self.held_until = None
        self.probation_until = now + self.probation_seconds

    def settle(self, now: float) -> None:
        """The panel carried the animation through probation; the trouble is over."""
        if self.probation_until is not None and now >= self.probation_until:
            self.probation_until = None
            self.wait_seconds = 0.0


def choose_metrics(
    cpu: float,
    ram: float,
    cpu_gate: MetricGate,
    ram_gate: MetricGate,
    gpu: float | None = None,
    net: float | None = None,
    gpu_gate: MetricGate | None = None,
    net_gate: MetricGate | None = None,
) -> list[tuple[str, float]]:
    """Return only the metrics whose alert gates are currently active.

    A GPU or NET reading of None (nvidia-smi failed, no previous byte count)
    goes to its gate like any other unusable sample: an active alert keeps its
    last good value through it, exactly as CPU and RAM always have. Dropping
    the label instead emptied the active set, so one failed sample deleted the
    page and the next good one re-created and re-activated it.
    """
    metrics: list[tuple[str, float]] = []
    for label, value, gate in (
        ("CPU", cpu, cpu_gate),
        ("RAM", ram, ram_gate),
        ("GPU", gpu, gpu_gate),
        ("NET", net, net_gate),
    ):
        if gate is not None and gate.update(value) and gate.last_value is not None:
            metrics.append((label, gate.last_value))
    return metrics


def sample_network_mb_s(
    prev_bytes: int | None,
    prev_time: float | None,
    current_bytes: int,
    current_time: float,
) -> float:
    """Compute download rate in MB/s between two monotonic timestamps."""
    if prev_bytes is None or prev_time is None:
        return 0.0
    elapsed = current_time - prev_time
    if elapsed <= 0:
        return 0.0
    delta_bytes = max(0, current_bytes - prev_bytes)
    return delta_bytes / (1024 * 1024 * elapsed)


def read_stats() -> tuple[float, float]:
    return (
        psutil.cpu_percent(interval=None),
        psutil.virtual_memory().percent,
    )


def should_log(misses: int) -> bool:
    return misses in (1, 5) or misses % 60 == 0


def retry_delay(failure_count: int) -> float:
    """Seconds from a failed panel write to the next write attempt.

    INTERVAL, doubling, capped at RETRY_MAX_SECONDS: 5, 10, 20, 40, 60. It
    applies whether the write was lost or refused. The first step fits inside
    the loop's own sleep, so a single dropped packet, or the static fallback
    after a refused animated page, still goes out on the next reading.
    """
    failure_count = max(1, int(failure_count))
    return float(min(RETRY_MAX_SECONDS, INTERVAL * 2 ** min(failure_count - 1, 5)))


def is_quiet_time(moment: datetime | None = None) -> bool:
    moment = datetime.now().astimezone() if moment is None else moment
    minute = moment.hour * 60 + moment.minute
    return minute >= QUIET_START_MINUTE or minute < QUIET_END_MINUTE


def activate_app() -> str | None:
    """Show a newly triggered alert once, without waking a blacked-out panel."""

    try:
        body = {"name": APP_NAME, "fast": False}
        with panel_gate.mutation(GATE_SOURCE, "activate", APP_NAME, body=body) as gate:
            response = requests.put(
                ACTIVE_URL,
                json=body,
                timeout=REQUEST_TIMEOUT,
            )
            gate.observe(response)
    except requests.RequestException as error:
        return _transport_error(error)
    if 200 <= response.status_code < 300:
        return None
    return _http_error("activation", response)


class SyncError(str):
    """A sync failure that also records whether the panel actually answered.

    The distinction matters because it is the only thing that separates "this
    page is too heavy for the panel" from "the WiFi dropped". It behaves as the
    plain message string everywhere it is printed or matched.
    """

    refused: bool

    def __new__(cls, message: str, *, refused: bool) -> "SyncError":
        error = super().__new__(cls, message)
        error.refused = refused
        return error


def _http_error(action: str, response) -> SyncError:
    """The panel answered and would not take the request."""
    detail = str(getattr(response, "text", ""))[:200]
    suffix = f": {detail}" if detail else ""
    return SyncError(f"{action} HTTP {response.status_code}{suffix}", refused=True)


def _transport_error(error: Exception) -> SyncError:
    """Nothing came back. This says nothing about what was sent."""
    return SyncError(f"unreachable: {type(error).__name__}", refused=False)


def _safe_page(
    metrics: list[tuple[str, float]],
    animated: bool,
    histories: dict[str, tuple[float, ...]] | None,
) -> tuple[dict | None, SyncError | None]:
    """Build a page, degrading to static art rather than killing the watcher.

    The size guards raise, and this loop runs under pythonw with nowhere for a
    traceback to go. An art change that pushed the icon past its budget would
    take the alert down silently, at exactly the moment the machine is busy
    enough to need it. Treat a local rejection the way a panel refusal is
    treated: drop to static, and try animation again later.
    """
    rejection: ValueError | None = None
    for attempt_animated in ((True, False) if animated else (False,)):
        try:
            return build_payload(metrics, animated=attempt_animated, histories=histories), None
        except ValueError as error:
            rejection = error
    return None, SyncError(f"page rejected locally: {rejection}", refused=True)


def sync_panel(
    metrics: list[tuple[str, float]],
    published_labels: tuple[str, ...] | None,
    animated: bool = True,
    histories: dict[str, tuple[float, ...]] | None = None,
    *,
    legacy_cleanup: bool = True,
) -> tuple[tuple[str, ...] | None, str | None]:
    """Make the panel match the active metrics and return (state, error).

    ``legacy_cleanup`` is turned off by the loop once the legacy deletes have
    been answered, so a startup cleanup the panel keeps refusing retries only
    the direct slot that is actually blocking it.
    """
    desired_labels = tuple(label for label, _ in metrics)
    current_labels = published_labels

    try:
        # First synchronization removes both the legacy array family and any
        # direct pcstats0 object left by an earlier run. Afterwards this sender
        # updates one direct object in place, avoiding array rebuild churn.
        if current_labels is None:
            # The legacy names are cosmetic. Failing to remove them leaves a
            # dead entry in the app list, which is not a reason to withhold a
            # CPU alert -- and blocking on them meant a persistent non-404
            # error suppressed every alert for the life of the process.
            for cleanup_url in (LEGACY_DELETE_URL, *LEGACY_CHILD_URLS) if legacy_cleanup else ():
                legacy = cleanup_url.rsplit("/", 1)[-1]
                with panel_gate.mutation(GATE_SOURCE, "delete", legacy) as gate:
                    gate.observe(requests.delete(cleanup_url, timeout=REQUEST_TIMEOUT))
            # The direct slot is different: an object left under this exact
            # name by an earlier run would conflict with the page below.
            with panel_gate.mutation(GATE_SOURCE, "delete", APP_NAME) as gate:
                response = requests.delete(DELETE_URL, timeout=REQUEST_TIMEOUT)
                gate.observe(response)
            if not (200 <= response.status_code < 300 or response.status_code == 404):
                return current_labels, _http_error("cleanup", response)
            current_labels = ()

        # Quiet is a real state: after cleanup, send no pushed app at all.
        if not metrics:
            if current_labels:
                with panel_gate.mutation(GATE_SOURCE, "delete", APP_NAME) as gate:
                    response = requests.delete(DELETE_URL, timeout=REQUEST_TIMEOUT)
                    gate.observe(response)
                if not (200 <= response.status_code < 300 or response.status_code == 404):
                    return current_labels, _http_error("cleanup", response)
            return (), None

        page, rejection = _safe_page(metrics, animated, histories)
        if page is None:
            return current_labels, rejection

        with panel_gate.mutation(GATE_SOURCE, "push", APP_NAME, body=page) as gate:
            response = requests.put(URL, json=page, timeout=REQUEST_TIMEOUT)
            gate.observe(response)
        if 200 <= response.status_code < 300:
            return desired_labels, None
        return current_labels, _http_error("push", response)
    except requests.RequestException as error:
        return current_labels, _transport_error(error)


def read_panel_health() -> PanelHealth | None:
    """Read only the boot marker; never touch the framebuffer endpoint."""
    try:
        response = requests.get(DEVICE_URL, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        return None
    if not 200 <= response.status_code < 300:
        return None
    try:
        body = response.json()
        uptime = int(body["uptimeSeconds"])
        reset_reason = str(body.get("resetReason", ""))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if uptime < 0:
        return None
    return PanelHealth(uptime, reset_reason)


def panel_rebooted_since(previous: PanelHealth | None, current: PanelHealth | None) -> bool:
    return previous is not None and current is not None and current.uptime_seconds < previous.uptime_seconds


PLANNED_RESET_REASON = "software"


def unplanned_reboot(previous: PanelHealth | None, current: PanelHealth | None) -> bool:
    """A reboot nobody asked for: the only kind that should cost animation.

    The panel reports ``software`` after a requested restart -- the API reboot
    the Utility Hub's nightly maintenance sends, an OTA, a settings change.
    A crash reports ``panic`` or a watchdog.  Latching static art on every
    reboot made the nightly maintenance freeze these icons for good.
    """
    return (panel_rebooted_since(previous, current)
            and current is not None
            and current.reset_reason.strip().lower() != PLANNED_RESET_REASON)


def page_predates_boot(published_at: float, probed_at: float, health: PanelHealth) -> bool:
    """Whether the panel has booted since it last took a page from this sender.

    Pushed apps live in RAM, so such a page is gone even though nothing failed.
    Both times are this PC's monotonic clock; the boot is placed at the probe
    minus the panel's uptime. A tie counts as lost: a redundant push is cheap,
    an alert missing from a busy machine is not.
    """
    return published_at <= probed_at - health.uptime_seconds


def held_metrics(
    published_labels: tuple[str, ...] | None,
    readings: dict[str, float | None],
    shown_since: float | None,
    now: float,
) -> list[tuple[str, float]]:
    """The page to keep up after its alert cleared, or [] once it may go.

    A page that has not been up MIN_DISPLAY_SECONDS stays. sync_due leaves a
    held page as the panel has it, so the current readings returned here are
    drawn only if it has to drop to static icons. If the alert re-triggers
    meanwhile, the active metrics are the same labels and the page carries on.
    """
    if not published_labels or shown_since is None:
        return []
    if now - shown_since >= MIN_DISPLAY_SECONDS:
        return []
    held = [(label, readings.get(label)) for label in published_labels]
    if any(value is None for _, value in held):
        return []
    return [(label, float(value)) for label, value in held]


def shows_change(view: PageView, published: PageView) -> bool:
    """Whether this page would look meaningfully different from the panel's."""
    return (
        view.labels != published.labels
        or view.face != published.face
        or view.icon_levels != published.icon_levels
        or view.lane_level != published.lane_level
        or abs(view.percent - published.percent) >= REFRESH_DELTA_POINTS
    )


def relabelled_in_place(
    metrics: list[tuple[str, float]],
    published_labels: tuple[str, ...] | None,
    published_view: PageView | None,
) -> bool:
    """Whether the active set changed only in metrics the page does not draw.

    NET crossing its threshold beside a CPU alert, or beside a CPU+RAM alert,
    leaves _page_face on the same metric, and build_payload draws the page
    from that metric alone, so every byte of it is unchanged. The loop records
    such a change without a write; the metric the page does draw still goes
    through the usual value refresh and keep-alive.
    """
    if not metrics or not published_labels or published_view is None:
        return False
    if tuple(label for label, _ in metrics) == published_labels:
        return False
    return page_view(metrics).face == published_view.face


def keepalive_due(since_push: float, overhead: float = 0.0) -> bool:
    """Whether an unchanged page must be rewritten now to be sure it stays up.

    Readings come every INTERVAL plus ``overhead``, the time sampling takes.
    The keep-alive waits while a keep-alive lost one reading later would still
    leave its retry a full REQUEST_TIMEOUT before LIFETIME_MS (see
    KEEPALIVE_SECONDS). The more each reading costs, the earlier it goes.
    """
    return since_push >= KEEPALIVE_SECONDS - 2 * max(0.0, overhead)


def refresh_due(
    view: PageView,
    published: PageView | None,
    since_push: float,
    change_streak: int = 0,
    overhead: float = 0.0,
) -> bool:
    """Whether a live page with the same labels has changed enough to rewrite.

    ``change_streak`` counts the readings in a row, this one included, whose
    page shows_change from the panel's; a change of value is only written once
    it reaches REFRESH_CONFIRM_SAMPLES. ``overhead`` is the loop's sampling
    time per reading, which brings the keep-alive forward (keepalive_due).
    """
    if published is None or view.labels != published.labels:
        return True
    if keepalive_due(since_push, overhead):
        return True
    if since_push < PUSH_INTERVAL or change_streak < REFRESH_CONFIRM_SAMPLES:
        return False
    return shows_change(view, published)


def sync_due(
    metrics: list[tuple[str, float]],
    published_labels: tuple[str, ...] | None,
    now: float,
    published_at: float | None = None,
    published_view: PageView | None = None,
    animated: bool | None = None,
    published_animated: bool | None = None,
    *,
    held: bool = False,
    change_streak: int = 0,
    overhead: float = 0.0,
) -> bool:
    """Whether the panel needs a write now.

    A change of the active set, or of animation mode, is due at once. The mode
    counts even when the labels match. Dropping to static because the panel
    just rebooted does nothing on its own: the page already on the panel is
    still the animated one, so without this the panel kept re-rendering the
    GIF that brought it down until the refresh timer came round. The same
    applies in reverse, so a probe goes out promptly.

    Otherwise a visible page is rewritten only when what it shows has changed
    enough to matter, or for its keep-alive (refresh_due). Unknown history --
    no successful push yet, or a page the panel lost to a reboot -- is due.

    ``held`` marks a page kept up only until MIN_DISPLAY_SECONDS after its
    alert cleared. It is left exactly as the panel has it: no value refresh,
    no keep-alive (it goes long before its lifetime could lapse) and no
    animated probe. Dropping to static is the one write still made on it,
    because that is safety, not cadence. A held page the panel lost is let go.
    """
    desired_labels = tuple(label for label, _ in metrics)
    if published_labels is None or desired_labels != published_labels:
        return True
    if not metrics:
        return False
    mode_changed = (
        animated is not None
        and published_animated is not None
        and animated != published_animated
    )
    if held:
        return mode_changed and not animated and published_view is not None
    if mode_changed:
        return True
    if published_at is None:
        return True
    return refresh_due(
        page_view(metrics),
        published_view,
        now - published_at,
        change_streak=change_streak,
        overhead=overhead,
    )


def main() -> None:
    psutil.cpu_percent(interval=None)  # prime the counter, first call reads 0.0
    time.sleep(0.2)  # give the first real CPU sample enough time to accumulate

    cpu_gate = MetricGate(CPU_SHOW_AT, CPU_CLEAR_BELOW)
    ram_gate = MetricGate(RAM_SHOW_AT, RAM_CLEAR_BELOW)
    gpu_gate = MetricGate(GPU_SHOW_AT, GPU_CLEAR_BELOW)
    net_gate = MetricGate(NET_SHOW_AT, NET_CLEAR_BELOW, max_value=10_000.0)
    published_labels: tuple[str, ...] | None = None
    published_view: PageView | None = None  # what the panel last took from us
    published_at: float | None = None  # when it took it (keep-alive base)
    shown_since: float | None = None  # when the current page first went up
    change_streak = 0  # readings in a row whose page differs from the panel's
    next_write = 0.0  # write backoff: no panel write before this
    backoff_unreachable = False  # that backoff waits out a panel that did not answer
    probe_may_lift = True  # a health probe that answers may end that wait
    legacy_answered = False  # the one-time legacy deletes have had their answer
    overheads: deque[float] = deque(maxlen=OVERHEAD_WINDOW)  # sampling time per reading
    misses = 0
    guard = AnimationGuard()
    published_animated: bool | None = None
    animated_push_succeeded = False
    previous_health: PanelHealth | None = None
    next_health_probe = 0.0
    next_activation = 0.0
    cpu_history: deque[float] = deque(maxlen=8)
    ram_history: deque[float] = deque(maxlen=8)
    gpu_history: deque[float] = deque(maxlen=8)
    net_history: deque[float] = deque(maxlen=8)
    prev_net_bytes: int | None = None
    prev_net_time: float | None = None

    print(
        f"Watching CPU >= {CPU_SHOW_AT}%, RAM >= {RAM_SHOW_AT}%, "
        f"GPU >= {GPU_SHOW_AT}%, NET >= {NET_SHOW_AT} MB/s every "
        f"{INTERVAL}s. The panel stays quiet otherwise. Ctrl+C to stop."
    )

    while True:
        now_sample = time.monotonic()
        cpu, ram = read_stats()

        gpu_val: float | None = None
        if sample_gpu is not None:
            reading = sample_gpu()
            if reading.value is not None:
                gpu_val = reading.value

        net_val: float | None = None
        try:
            net_io = psutil.net_io_counters()
            current_bytes = net_io.bytes_recv
            if prev_net_bytes is not None and prev_net_time is not None:
                net_val = sample_network_mb_s(
                    prev_net_bytes, prev_net_time, current_bytes, now_sample
                )
            prev_net_bytes = current_bytes
            prev_net_time = now_sample
        except (OSError, psutil.Error):
            pass

        valid_cpu = _valid_percent(cpu)
        valid_ram = _valid_percent(ram)
        if valid_cpu is not None:
            cpu_history.append(valid_cpu)
        if valid_ram is not None:
            ram_history.append(valid_ram)
        if gpu_val is not None:
            gpu_history.append(gpu_val)
        if net_val is not None:
            net_history.append(net_val)

        metrics = choose_metrics(
            cpu,
            ram,
            cpu_gate,
            ram_gate,
            gpu=gpu_val,
            net=net_val,
            gpu_gate=gpu_gate,
            net_gate=net_gate,
        )
        histories = {
            "CPU": tuple(cpu_history),
            "RAM": tuple(ram_history),
            "GPU": tuple(gpu_history),
            "NET": tuple(net_history),
        }

        now = time.monotonic()
        overheads.append(max(0.0, now - now_sample))
        guard.settle(now)
        # A page that cleared before MIN_DISPLAY_SECONDS stays up as it is.
        # From here on `display` is the page this loop maintains, so a held
        # page is a real page to the guard and the health probe alike.
        winding_down = not metrics  # no gate is active; any page left is held
        display = metrics or held_metrics(
            published_labels,
            {
                "CPU": cpu_gate.last_value,
                "RAM": ram_gate.last_value,
                "GPU": gpu_gate.last_value,
                "NET": net_gate.last_value,
            },
            shown_since,
            now,
        )
        if not display:
            # Uptime samples are only worth comparing within one alert episode.
            # Kept across a quiet gap, the next episode's first probe compared
            # readings hours apart and blamed the animation for any reboot that
            # had happened in between -- an OTA, a power blip, another script.
            previous_health = None
        # While writes wait out an unreachable panel, probe on every reading so
        # the page returns within a reading or so of the panel (see
        # RETRY_MAX_SECONDS). The GET is read-only and is never journaled.
        outage_probe = backoff_unreachable and probe_may_lift and now < next_write
        if display and (now >= next_health_probe or outage_probe):
            health = read_panel_health()
            if animated_push_succeeded and unplanned_reboot(previous_health, health):
                guard.refused(now)
                print("  panel reboot detected; holding static PC alert icons")
            if health is None:
                # Seen unreachable, so its next answer is news again.
                probe_may_lift = True
            else:
                previous_health = health
                # The panel answers, so an outage backoff has nothing left to
                # wait for. Only a successful write resets the failure count,
                # and a refusal's backoff stands: that panel was answering.
                if backoff_unreachable and probe_may_lift and now < next_write:
                    next_write = 0.0
                    probe_may_lift = False
                # A planned restart costs no animation, but it does take every
                # pushed page with it. Nothing failed, so nothing else would
                # notice before the keep-alive.
                if (
                    published_labels
                    and published_at is not None
                    and page_predates_boot(published_at, time.monotonic(), health)
                ):
                    published_view = None
            next_health_probe = now + HEALTH_PROBE_SECONDS

        # Only consult the guard for a real page. A quiet-state delete would
        # otherwise count as a probe succeeding and clear a hold that nothing
        # had actually tested.
        animated = guard.animate(now) if display else True
        probing = guard.probing

        # A metric joining or leaving beside the one the page draws changes
        # no pixel: record it, and let the keep-alive carry it.
        if relabelled_in_place(display, published_labels, published_view):
            published_labels = tuple(label for label, _ in display)
            published_view = replace(published_view, labels=published_labels)

        if display and published_view is not None and shows_change(
            page_view(display), published_view
        ):
            change_streak += 1
        else:
            change_streak = 0

        if now >= next_write and sync_due(
            display,
            published_labels,
            now,
            published_at,
            published_view,
            animated,
            published_animated,
            held=winding_down,
            change_streak=change_streak,
            overhead=max(overheads),
        ):
            was_visible = bool(published_labels)
            cleaning_up = published_labels is None
            published_labels, error = sync_panel(
                display,
                published_labels,
                animated=animated,
                histories=histories,
                legacy_cleanup=not legacy_answered,
            )

            if error is None:
                misses = 0
                next_write = 0.0
                backoff_unreachable = False
                probe_may_lift = True
                published_animated = animated if display else None
                if display:
                    published_view = page_view(display)
                    published_at = now
                    change_streak = 0
                    if not was_visible:
                        shown_since = now
                    if animated:
                        if probing:
                            print("  panel took an animated page again; resuming animation")
                        guard.succeeded(now)
                        animated_push_succeeded = True
                    if (
                        not was_visible
                        and published_labels
                        and not is_quiet_time()
                        and now >= next_activation
                    ):
                        next_activation = now + ACTIVATION_COOLDOWN_SECONDS
                        activation_error = activate_app()
                        if activation_error is not None:
                            print(f"  {activation_error}")
                else:
                    published_view = None
                    published_at = None
                    shown_since = None
            else:
                misses += 1
                # Lost or refused, the next write waits (RETRY_MAX_SECONDS).
                # Only a lost one may be cut short by a probe that answers.
                next_write = now + retry_delay(misses)
                backoff_unreachable = not error.refused
                if cleaning_up and error.refused:
                    # In the startup sync only the direct-slot delete, or the
                    # push after it, can refuse, so the legacy deletes before
                    # them were answered. Retry just what is blocking.
                    legacy_answered = True
                if display and error.refused:
                    if not guard.holding:
                        print(f"  panel refused the page; holding static icons ({error})")
                    guard.refused(now)
                elif display:
                    guard.unreachable()
                # Don't spam the console when the panel is unplugged or rebooting.
                if should_log(misses):
                    print(f"  {error} ({misses} in a row)")

        time.sleep(INTERVAL)


if __name__ == "__main__":
    # Under Task Scheduler there is no console, so sys.stdout is None and every
    # print() below is a silent no-op -- including the animation guard saying
    # the panel refused a page. This gives them a file to land in, and catches
    # the traceback of anything that kills the process.
    import sender_logging

    log = sender_logging.configure("pc_stats")
    try:
        main()
    except KeyboardInterrupt:
        print(f"\nStopped. Any visible alert will expire within {LIFETIME_MS // 1000}s.")
    except Exception:
        log.exception("sender stopped on an unhandled error")
        raise
