"""8x8 weather icons for the panel, built silhouette-first.

The set this replaces drew every condition as the same rounded lozenge and
distinguished them only by colour plus one- or two-pixel appendages.  That
fails twice over: at 8x8 a feature smaller than 2x1 is decoration rather than
information, and at brightness 1 the panel collapses to one bit per channel
(source values below 174 go to black), so colour-only distinctions vanish
entirely in the dark.

So the grammar here is topological, not chromatic:

* Sky family - what is overhead.  Cloud *size* encodes sky coverage: a small
  lobed cloud is "cloudy", a full-width slab is "overcast".
* Precipitation family - rows 0..4 are an identical cloud in every member, and
  rows 5..7 carry a band whose *shape* names the precipitation:

      rain      vertical runs of 2, in four separate columns
      drizzle   every pixel isolated - no run anywhere
      snow      clustered horizontal runs
      sleet     rain's verticals plus one snow cross
      storm     the only diagonal in the whole set

  Read the band as a silhouette with the colour off and the five are still
  distinguishable, which is exactly the 3 a.m. case.

Exactly one system moves per icon.  A GIF frame costs about 100 bytes, which
buys 8-12 changed pixels, and more than one moving thing on 64 pixels reads as
noise rather than weather.
"""

from __future__ import annotations

RGB = tuple[int, int, int]

# Every ink here sits at or above 174 in its dominant channel so the shape
# survives the 1-bit threshold the panel applies at brightness 1.
PALETTE: dict[str, RGB] = {
    "S": (255, 176, 30),    # sun rim
    "H": (255, 224, 120),   # sun core
    "M": (236, 240, 255),   # moon, lit limb
    "m": (180, 190, 220),   # moon, terminator side (>= 174 for 1-bit mode)
    "*": (208, 216, 255),   # star
    "C": (176, 190, 218),   # cloud, lit
    "c": (145, 158, 185),   # cloud, underside (>= 174 dominant for 1-bit mode)
    "R": (80, 178, 255),    # rain
    "r": (72, 150, 226),    # drizzle
    "W": (226, 244, 255),   # snow
    "B": (255, 214, 60),    # lightning
    "F": (168, 182, 210),   # fog
}

BLACK: RGB = (0, 0, 0)

# --- sky family -----------------------------------------------------------

CLEAR_DAY = (
    "...SS...",
    ".S.SS.S.",
    "..SHHS..",
    "SSHHHHSS",
    "SSHHHHSS",
    "..SHHS..",
    ".S.SS.S.",
    "...SS...",
)
CLEAR_NIGHT = (
    "..MMm...",
    ".MMMm..*",
    ".MMm....",
    "MMm.....",
    "MMm.....",
    ".MMm.*..",
    ".MMMm...",
    "..MMm...",
)
PARTLY_DAY = (
    "..S.....",
    ".SHS....",
    "SHHHS...",
    ".SHS.CCC",
    "...CCCCC",
    "..CCCCCC",
    ".CCCCCCC",
    ".ccccccc",
)
PARTLY_NIGHT = (
    "..MMm..*",
    ".MMm....",
    "MMm.....",
    "MMm..CCC",
    ".MMmCCCC",
    "..CCCCCC",
    ".CCCCCCC",
    ".ccccccc",
)
CLOUDY = (
    "........",
    "........",
    "..CCC...",
    ".CCCCCC.",
    "CCCCCCCC",
    "cccccccc",
    "........",
    "........",
)
OVERCAST = (
    "........",
    "CCCCCCCC",
    "CCCCCCCC",
    "cccccccc",
    ".cc.ccc.",
    "........",
    "........",
    "........",
)
FOG = (
    "........",
    ".FFFFF..",
    "........",
    "FFFFFFF.",
    "........",
    "..FFFFFF",
    "........",
    ".FFFFF..",
)

# --- precipitation family -------------------------------------------------
# Rows 0..4 are shared verbatim; only the band below it changes.

PRECIP_CLOUD = (
    "........",
    "..CCC...",
    ".CCCCCC.",
    "cccccccc",
    "........",
)
BANDS: dict[str, tuple[str, ...]] = {
    "rain": (".R...R..", ".R.R.R.R", "...R...R"),
    "drizzle": ("........", "r.r.r.r.", ".r.r.r.r"),
    "snow": (".W...W..", "WWW.WWW.", ".W...W.."),
    "sleet": (".....W..", "R.R.WWW.", "R.R..W.."),
    # A clean 45-degree streak. It is the only diagonal anywhere in the set,
    # which is what makes it unmistakable once colour is gone.
    "storm": ("...BB...", "..BB....", ".BB....."),
}


def _shift_band(band: tuple[str, ...], step: int) -> tuple[str, ...]:
    """Slide a band down by `step` rows, wrapping - this is the falling motion."""

    height = len(band)
    return tuple(band[(row - step) % height] for row in range(height))


def _blank() -> list[str]:
    return ["........"] * 8


def _overlay(base: tuple[str, ...] | list[str], rows: tuple[str, ...], at: int) -> list[str]:
    out = list(base)
    for index, row in enumerate(rows):
        target = at + index
        if 0 <= target < 8:
            out[target] = row
    return out


def _pulse_sun(art: tuple[str, ...], step: int) -> list[str]:
    """The sun's outer ray ring breathes: resting disc -> cardinal rays -> full flare -> cardinal rays."""
    phase = step % 4
    if phase == 2:
        return list(art)
    if phase == 0:
        out = []
        for index, row in enumerate(art):
            if index in (0, 1, 6, 7):
                out.append("........")
            elif index in (3, 4):
                out.append("." + row[1:7] + ".")
            else:
                out.append(row)
        return out
    out = []
    for index, row in enumerate(art):
        if index in (1, 6):
            out.append("...SS...")
        else:
            out.append(row)
    return out


def _drift_fog(step: int) -> list[str]:
    """Fog bars slide horizontally at different rates - the layers separate."""

    rates = (1, -1, 1, -1)
    out = _blank()
    bars = ((1, ".FFFFF.."), (3, "FFFFFFF."), (5, "..FFFFFF"), (7, ".FFFFF.."))
    for index, (row, pattern) in enumerate(bars):
        offset = (step * rates[index]) % 8
        out[row] = pattern[-offset:] + pattern[:-offset] if offset else pattern
    return out


# --- rich icons (2026-10-03, to the Now Playing standard) --------------------
#
# Full-colour frames instead of one-ink character art: shaded objects (a sun
# with core, body and rim; a crescent with limb, body and terminator; clouds
# with a sunlit top and a shadowed underside), motion with a purpose (rays
# whose glow rotates like the record's label ring, comets along a cloud's
# edge, drops with bright heads and fading tails, a storm cloud lit from
# within when the bolt strikes).  The key silhouette stays >= 174 in its
# dominant channel; the shading tones may fade at brightness 1, as Now
# Playing's dark rim does.

import math as _math

RICH: dict[str, RGB] = {
    "core": (255, 241, 179), "gold": (255, 195, 48), "amber": (255, 103, 40),
    "ray_hi": (255, 221, 110), "ray_lo": (255, 90, 52),
    "limb": (202, 236, 255), "moon": (155, 153, 250), "terminator": (85, 67, 144),
    "star": (158, 221, 255),
    "cloud_top": (239, 246, 255), "cloud": (147, 202, 239), "cloud_under": (60, 104, 172),
    "cloud_glow": (255, 252, 232), "cloud_trail": (201, 228, 252),
    "storm_lit": (193, 112, 242),
    "rain": (49, 208, 255), "rain_tail": (40, 101, 200),
    "drizzle": (108, 227, 220), "snow": (221, 252, 255),
    "bolt": (255, 250, 225), "bolt_edge": (255, 200, 50),
    "fog": (151, 181, 255), "fog_dim": (93, 70, 157), "fog_glow": (207, 170, 255),
    "pearl": (255, 247, 232),
}


def _rgb_blank() -> list[list[RGB]]:
    return [[BLACK] * 8 for _ in range(8)]


def _put(frame: list[list[RGB]], x: int, y: int, colour: RGB) -> None:
    if 0 <= x < 8 and 0 <= y < 8:
        frame[y][x] = colour


def _scale(colour: RGB, k: float) -> RGB:
    return tuple(max(0, min(255, round(c * k))) for c in colour)  # type: ignore[return-value]


def _mix(a: RGB, b: RGB, t: float) -> RGB:
    """Gamma-correct blend, so a highlight brightens evenly on the LEDs."""
    t = max(0.0, min(1.0, t))
    return tuple(  # type: ignore[return-value]
        round(((x / 255) ** 2.2 * (1 - t) + (y / 255) ** 2.2 * t) ** (1 / 2.2) * 255)
        for x, y in zip(a, b)
    )


def _disc(frame, cx: float, cy: float, core: float, body: float, rim: float) -> None:
    for y in range(8):
        for x in range(8):
            d = _math.hypot(x - cx, y - cy)
            if d <= core:
                _put(frame, x, y, RICH["core"])
            elif d <= body:
                _put(frame, x, y, RICH["gold"])
            elif d <= rim:
                _put(frame, x, y, RICH["amber"])


SUN_RAYS = ((3, 0), (4, 0), (6, 1), (7, 3), (7, 4), (6, 6), (4, 7), (3, 7), (1, 6), (0, 4), (0, 3), (1, 1))


def _ray(index: int, count: int, step: float) -> RGB:
    """Ray brightness rotates round the disc, like the record's label ring."""
    k = ((index - step) % count) / count
    return _mix(RICH["ray_hi"], RICH["ray_lo"], abs(_math.sin(_math.pi * k)))


def _sun(step: int) -> list[list[RGB]]:
    frame = _rgb_blank()
    _disc(frame, 3.5, 3.5, 0.8, 1.6, 2.35)
    for i, (x, y) in enumerate(SUN_RAYS):
        _put(frame, x, y, _ray(i, len(SUN_RAYS), step * 2.0))  # 6 beats per turn
    return frame


CRESCENT = ("..LLL...", ".LLbs...", "LLbs....", "LLb.....", "LLb.....", "LLbs....", ".LLbs...", "..LLL...")
STARS = ((6, 1, 0), (5, 5, 3), (7, 6, 5), (4, 3, 6))
TWINKLE = (1.0, 0.75, 0.42, 0.18, 0.10, 0.25, 0.55, 0.85)


def _moon(frame, step: int, art: tuple[str, ...] = CRESCENT, stars=STARS) -> None:
    tones = {"L": RICH["limb"], "b": RICH["moon"], "s": RICH["terminator"]}
    for y, row in enumerate(art):
        for x, ch in enumerate(row):
            if ch in tones:
                _put(frame, x, y, tones[ch])
    for sx, sy, phase in stars:
        level = TWINKLE[(step + phase) % len(TWINKLE)]
        if level > 0 and frame[sy][sx] == BLACK:
            _put(frame, sx, sy, _scale(RICH["star"], level))


CLOUD_ART = ("..TT....", ".TCCT...", "TCCCCCT.", ".uuuuu..")
# Separate rounded banks read as overcast rather than a rectangular file icon.
OVERCAST_ART = ("...TT...", "..TCCCT.", ".TTCCCu.", "TCCCCCT.", ".uuuuu..")


def _cloud(frame, top: int, step: int | None = None, art: tuple[str, ...] = CLOUD_ART,
           under: str = "cloud_under", offset: int = 0) -> None:
    edge = []
    for r, row in enumerate(art):
        for source_x, ch in enumerate(row):
            x = source_x + offset
            y = top + r
            if ch == "T":
                _put(frame, x, y, RICH["cloud_top"])
                edge.append((x, y))
            elif ch == "C":
                _put(frame, x, y, RICH["cloud"])
            elif ch == "u":
                _put(frame, x, y, RICH[under])
    if step is not None and edge:
        edge.sort()
        # Light travels back across the crown instead of jumping from its
        # right end to its left. Eight frames keep a full loop inexpensive.
        sweep = (0, 2, 4, 6, 7, 5, 3, 1)
        head = round(sweep[step % 8] * (len(edge) - 1) / 7)
        for k, tone in ((0, "cloud_glow"), (1, "cloud_trail")):
            i = head - k
            if 0 <= i < len(edge):
                _put(frame, *edge[i], RICH[tone])


def _falling(frame, drops, top: int, span: int, step: int, head: str, tail: str | None) -> None:
    for x, phase in drops:
        y = top + (step + phase) % span
        _put(frame, x, y, RICH[head])
        if tail and y - 1 >= top:
            _put(frame, x, y - 1, RICH[tail])


RAIN_DROPS = ((1, 0), (4, 1), (6, 2))
DRIZZLE_DROPS = ((1, 0), (4, 1), (6, 2), (3, 2))
SNOW_FLAKES = ((1, 0), (4, 2), (6, 1), (3, 0))
BOLT_PATH = (((4, 4), "bolt_edge"), ((3, 5), "bolt"), ((4, 5), "bolt_edge"), ((3, 6), "bolt"), ((2, 7), "bolt_edge"))
FOG_BANDS = ((1, ".fFFFFf.", 1), (3, "fFFFFFf.", -1), (5, ".fFFFFFf", 1), (7, "..fFFFf.", -1))
PARTLY_CLOUD = ("....TTT.", "..TTCCCT", ".TCCCCCC", "uuuuuuuu")
PARTLY_MOON = ("LLb.....", "Lbs.....", "Lb......", "Lbs.....", "LLb.....")


def _rich_frames(kind: str, is_day: bool) -> tuple[list[list[list[RGB]]], int]:
    if kind == "clear":
        if is_day:
            return [_sun(step) for step in range(6)], 240
        out = []
        for step in range(8):
            frame = _rgb_blank()
            _moon(frame, step)
            out.append(frame)
        return out, 240
    if kind == "part":
        out = []
        for step in range(8):
            frame = _rgb_blank()
            if is_day:
                _disc(frame, 2.2, 2.2, 0.6, 1.3, 2.0)
                for i, (x, y) in enumerate(((4, 0), (5, 2), (0, 4), (2, 5))):
                    _put(frame, x, y, _ray(i, 4, step * 0.5))
            else:
                _moon(frame, step, PARTLY_MOON, ((5, 0, 0), (6, 2, 4)))
            _cloud(frame, 4, None, PARTLY_CLOUD)
            out.append(frame)
        return out, 240
    if kind in ("cloudy", "overcast"):
        art = CLOUD_ART if kind == "cloudy" else OVERCAST_ART
        top = 2 if kind == "cloudy" else 1
        out = []
        for step in range(8):
            frame = _rgb_blank()
            # A real bank drifts one cell and settles back. Shading stays tied
            # to the upper-left light rather than a bead racing round its rim.
            offset = (0, 0, 1, 1, 1, 1, 0, 0)[step]
            _cloud(frame, top, None, art, offset=offset)
            edge = [(x, y) for y in range(8) for x in range(8)
                    if frame[y][x] == RICH['cloud_top']]
            if edge:
                x, y = edge[0]
                level = (0.10, 0.22, 0.35, 0.48, 0.55, 0.40, 0.26, 0.15)[step]
                _put(frame, x, y, _mix(RICH['cloud_top'], RICH['cloud_glow'], level))
            out.append(frame)
        return out, 240
    if kind in ("rain", "drizzle", "snow", "sleet"):
        delay = 120 if kind == "rain" else 240
        out = []
        for step in range(6):
            frame = _rgb_blank()
            _cloud(frame, 1)
            if kind == "rain":
                _falling(frame, RAIN_DROPS, 5, 3, step, "rain", "rain_tail")
                # Keep drops the same color as they move one row per beat.
                # A small crown reflection differentiates the two falls in
                # the six-frame loop without making every drop flash white.
                _put(frame, 2, 1, _mix(RICH['cloud_top'], RICH['cloud_trail'],
                                      (0.0, 0.15, 0.30, 0.45, 0.30, 0.15)[step]))
            elif kind == "drizzle":
                _falling(frame, DRIZZLE_DROPS, 5, 3, step // 2, "drizzle" if step % 2 else "rain", None)
            elif kind == "snow":
                # A tiny cross remains a flake in one-bit night mode;
                # drizzle is deliberately isolated dots.
                centre = (2, 3, 4, 4, 3, 2)[step]
                for x, y in ((centre, 5), (centre - 1, 6), (centre, 6),
                             (centre + 1, 6), (centre, 7)):
                    _put(frame, x, y, RICH["snow"] if x == centre else RICH["rain"])
                _put(frame, 6, 5 + step % 3, RICH["snow"])
            else:
                _falling(frame, ((1, 0), (3, 2)), 5, 3, step // 2, "rain", "rain_tail")
                for x, y in ((6, 5), (5, 6), (6, 6), (7, 6), (6, 7)):
                    _put(frame, x, y, RICH["snow"] if step % 2 else RICH["drizzle"])
            out.append(frame)
        return out, delay
    if kind == "storm":
        out = []
        for step in range(8):
            frame = _rgb_blank()
            _cloud(frame, 1, None, under="storm_lit" if step < 3 else "cloud_under")
            # Keep the bolt readable throughout the loop. A warm charge
            # passes down it while cyan rain continues on either side.
            _falling(frame, ((0, 0), (6, 1)), 5, 3, step, "rain", None)
            for index, ((x, y), tone) in enumerate(BOLT_PATH):
                _put(frame, x, y, RICH["bolt"] if index == step % len(BOLT_PATH)
                     else RICH["bolt_edge"])
            out.append(frame)
        return out, 120
    if kind == "fog":
        out = []
        for step in range(8):
            frame = _rgb_blank()
            # Bounded opposing drift: bars never split across the edges.
            drift = (0, 0, 1, 1, 1, 0, -1, -1)
            for band, row in enumerate((1, 3, 5, 7)):
                offset = drift[(step + band * 2) % 8]
                for x in range(1 + offset, 7 + offset):
                    tone = "fog_dim" if x in (1 + offset, 6 + offset) else "fog"
                    if x == 2 + offset + (step + band) % 3:
                        tone = "fog_glow"
                    _put(frame, x, row, RICH[tone])
            out.append(frame)
        return out, 240
    return _rich_frames("cloudy", is_day)


def frames(kind: str, is_day: bool = True):
    """Return (frames, delay_ms) for a condition.

    Frames are full-colour 8x8 grids (since 2026-10-03); ``paint`` draws them.
    The delay is always a multiple of 120 ms.  The panel ticks every 24 ms and
    GIF delays quantise to 10 ms, so 120 ms is the shortest interval where the
    two agree; anything else lands on a 2-tick/3-tick beat and visibly judders.
    """

    return _rich_frames(kind, is_day)


def _shimmer(art: tuple[str, ...]) -> list[list[str]]:
    """A highlight travelling along the cloud's lit edge.

    A still cloud makes the panel look frozen, but a cloud that jumps a pixel
    looks broken.  Moving the *lighting* rather than the shape keeps the
    silhouette rock steady while giving the eye something alive, and it touches
    only three pixels a frame.
    """

    lit = [(y, x) for y, row in enumerate(art) for x, ch in enumerate(row) if ch == "C"]
    if not lit:
        return [list(art)]
    lit.sort(key=lambda cell: (cell[0], cell[1]))
    out: list[list[str]] = []
    for step in range(6):
        rows = [list(row) for row in art]
        head = (step * 2) % max(1, len(lit))
        for offset in range(3):
            y, x = lit[(head + offset) % len(lit)]
            rows[y][x] = "H"
        out.append(["".join(row) for row in rows])
    return out


def paint(frame: list[list[RGB]], art, x0: int = 0, y0: int = 0) -> None:
    """Stamp one 8x8 frame into a larger RGB frame buffer.

    ``art`` is either full-colour rows (the rich icons) or character-art
    strings looked up in PALETTE (kept for callers that still pass them).
    """

    if art and not isinstance(art[0], str):
        for y, row in enumerate(art):
            for x, colour in enumerate(row):
                if colour != BLACK:
                    px, py = x0 + x, y0 + y
                    if 0 <= px < len(frame[0]) and 0 <= py < len(frame):
                        frame[py][px] = tuple(colour)
        return
    for y, row in enumerate(art):
        for x, ch in enumerate(row):
            colour = PALETTE.get(ch)
            if colour is None:
                continue
            px, py = x0 + x, y0 + y
            if 0 <= px < len(frame[0]) and 0 <= py < len(frame):
                frame[py][px] = colour


def band_topology(kind: str) -> dict[str, int]:
    """Shape statistics for the precipitation band, used by the tests.

    These are what actually separate the five precipitation types once colour
    is gone, so they are asserted rather than eyeballed.
    """

    band = BANDS[kind]
    cells = {(x, y) for y, row in enumerate(band) for x, ch in enumerate(row) if ch != "."}

    def longest(step: tuple[int, int]) -> int:
        best = 0
        for cell in cells:
            if (cell[0] - step[0], cell[1] - step[1]) in cells:
                continue
            run, cur = 0, cell
            while cur in cells:
                run += 1
                cur = (cur[0] + step[0], cur[1] + step[1])
            best = max(best, run)
        return best

    seen: set[tuple[int, int]] = set()
    components = 0
    for cell in cells:
        if cell in seen:
            continue
        components += 1
        stack = [cell]
        while stack:
            cx, cy = stack.pop()
            if (cx, cy) in seen or (cx, cy) not in cells:
                continue
            seen.add((cx, cy))
            stack.extend([(cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)])

    return {
        "cells": len(cells),
        "max_vertical_run": longest((0, 1)),
        "max_horizontal_run": longest((1, 0)),
        "components": components,
    }
