"""Compose the whole 32x8 panel into one animated GIF.

Why this exists
---------------
The old payload sent an 8x8 icon GIF plus a 23x8 *static* bitmap drawn with a
`draw` op.  Every `draw` primitive in AWTRIX NG is static - there is no time
parameter in `DrawOp` - so the data strip could never move no matter how often
the sender re-pushed.

But an icon GIF is allowed to be the full 32x8, and at that width the firmware
blits it first, as the background, and composites text on top.  I verified this
on the panel: an 8-frame 32x8 GIF pushed with text rendered with the animation
sweeping *behind* the glyphs.  So the whole canvas can animate from one push,
at the panel's ~41.7 fps, costing nothing from the Berry script heap.

Two measured rules shape everything here:

* **120 ms is the only clean frame delay.**  The render tick is 24 ms and GIF
  delays quantise to 10 ms; they coincide first at 120 ms.  Any other value
  lands on a 2-tick/3-tick beat, and that judder is what makes LED animation
  look cheap.  Every delay below is 120 or 240.
* **One moving system.**  A frame costs roughly 100 bytes, which buys 8-12
  changed pixels.  Both the byte budget and good taste point the same way, so
  the icon animates and the strip holds, or the reverse - never both at once.
"""

from __future__ import annotations

import base64
import io
from typing import Sequence

from PIL import Image

try:
    from . import weather_icons as icons
except ImportError:  # Direct standalone execution from weather/.
    import weather_icons as icons

RGB = tuple[int, int, int]
Frame = list[list[RGB]]

WIDTH = 32
HEIGHT = 8
ICON_W = 8
GUTTER_X = 8          # a deliberate 1px separator, not dead space
STRIP_X0 = 9
STRIP_W = WIDTH - STRIP_X0          # 23
# The timeline could take the gutter column too, which would divide 8
# quarter-hour samples into exactly 3 columns each.  It does not: keeping x8
# black separates the icon from the data, and a viewer notices the icon
# bleeding into the graph far more than one bin being a pixel narrower.  With
# rounded bin boundaries the widths are [3,3,3,3,2,3,3,3] - the short bin sits
# mid-strip instead of always landing on the newest sample.
TIMELINE_X0 = STRIP_X0
TIMELINE_W = STRIP_W                # 23

BLACK: RGB = (0, 0, 0)
PEARL: RGB = (255, 247, 232)
FRAME_MS = 120

# Body cap is 8192; keep clear headroom for the JSON envelope and base64.
MAX_BODY_BYTES = 8192
SAFE_ICON_B64 = 5600


def sample_bounds(count: int, width: int) -> list[tuple[int, int]]:
    """Contiguous, as-equal-as-possible column runs, one per data sample.

    Truncating with `(column * count) // width` puts the whole remainder in the
    last bin, so the newest sample is the one that looks wrong.  Rounding the
    boundaries spreads it, and when width divides evenly every bin is equal.
    """

    if count <= 0 or width <= 0:
        return []
    edges = [round(index * width / count) for index in range(count + 1)]
    return [(edges[index], edges[index + 1]) for index in range(count)]


def new_frame() -> Frame:
    return [[BLACK for _ in range(WIDTH)] for _ in range(HEIGHT)]


def put(frame: Frame, x: int, y: int, colour: RGB) -> None:
    if 0 <= x < WIDTH and 0 <= y < HEIGHT:
        frame[y][x] = colour


def hline(frame: Frame, x0: int, x1: int, y: int, colour: RGB) -> None:
    for x in range(max(0, x0), min(WIDTH - 1, x1) + 1):
        put(frame, x, y, colour)


def to_gif(
    frames: Sequence[Frame],
    delay_ms: int = FRAME_MS,
    colours: int = 32,
    width: int = WIDTH,
) -> bytes:
    """Encode frames as an optimised GIF.

    Inter-frame optimisation is what makes this affordable: the sky, the text
    and the strip are identical across frames, so only the moving system costs
    bytes after the first frame.
    """

    if delay_ms % 120:
        raise ValueError(
            f"delay {delay_ms}ms is not a multiple of 120ms and will judder "
            "against the 24ms render tick"
        )
    images = []
    for frame in frames:
        img = Image.new("RGB", (width, HEIGHT))
        img.putdata([px for row in frame for px in row[:width]])
        images.append(img.convert("P", palette=Image.ADAPTIVE, colors=colours))
    buf = io.BytesIO()
    images[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=images[1:],
        duration=delay_ms,
        loop=0,
        optimize=True,
    )
    return buf.getvalue()


def encode(frames: Sequence[Frame], delay_ms: int = FRAME_MS) -> str:
    """Base64 an animated GIF, shrinking the palette before dropping frames.

    Frames carry the motion, so they are the last thing to give up.
    """

    for colours in (32, 16, 8):
        data = base64.b64encode(to_gif(frames, delay_ms, colours)).decode("ascii")
        if len(data) <= SAFE_ICON_B64:
            return data
    trimmed = list(frames)
    while len(trimmed) > 1:
        trimmed = trimmed[:-1]
        data = base64.b64encode(to_gif(trimmed, delay_ms, 8)).decode("ascii")
        if len(data) <= SAFE_ICON_B64:
            return data
    return data


def paint_icon(frame: Frame, art: list[str]) -> None:
    icons.paint(frame, art, 0, 0)


def paint_timeline(
    frame: Frame,
    columns: Sequence[RGB],
    intensities: Sequence[float] | None = None,
) -> None:
    """The precipitation strip: NOW anchored hard right, future to the left.

    Rows 6 and 7 are both free - text occupies only 1..5 - so the strip is two
    rows and reads as a bar chart rather than a hairline.  Colour carries
    probability and *height* carries amount, which is the pairing weather
    products conventionally use and which stays readable when the panel
    collapses to one bit per channel at low brightness.
    """

    ordered = list(reversed(list(columns)))
    heights = list(reversed(list(intensities))) if intensities else None
    for index, colour in enumerate(ordered):
        x = TIMELINE_X0 + index
        put(frame, x, 7, colour)
        if heights is not None and heights[index] >= 0.5:
            put(frame, x, 6, colour)


RAIL_AHEAD_DIM = 0.16  # the same tint as every other page's empty rail


def paint_rail(frame: Frame, rail: Sequence[RGB], marker: int) -> None:
    """The temperature range: row 7 carries the gradient, row 6 the needle."""

    # Full colour up to the needle, the rest of today's range dimmed: it reads
    # as a meter like every other page, and the colour still means the
    # temperature each column stands for.
    for index, colour in enumerate(rail):
        put(frame, STRIP_X0 + index, 7, colour if index <= marker else _dim(colour, RAIL_AHEAD_DIM))
    x = STRIP_X0 + marker
    put(frame, x, 6, PEARL)
    put(frame, x, 7, PEARL)


def _dim(colour: RGB, factor: float = 0.45) -> RGB:
    return tuple(max(0, min(255, round(channel * factor))) for channel in colour)  # type: ignore[return-value]


def build(
    kind: str,
    is_day: bool,
    paint_strip,
    paint_text,
) -> tuple[list[Frame], int]:
    """Compose the animated canvas.

    `paint_strip` and `paint_text` are called once per frame with (frame, step)
    so a caller can animate the strip instead of the icon when that carries
    more meaning - but only one of them should actually vary.
    """

    art_frames, delay = icons.frames(kind, is_day)
    out: list[Frame] = []
    for step, art in enumerate(art_frames):
        frame = new_frame()
        paint_icon(frame, art)
        paint_strip(frame, step)
        paint_text(frame, step)
        out.append(frame)
    return out, delay
