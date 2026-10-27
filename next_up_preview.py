"""Exact 32x8 art and payloads for AWTRIX Next Up.

The current payload holds ETA and compact event identity for 2.4 seconds each,
with an 8x8 calendar, black x8 gutter, and unchanged countdown rail. An exact
shared palette keeps its full-width GIF below 1KB. Static safety mode retains
the 8x8 still calendar and bitmap ETA lane. Aliases reach both representations.

The older build_animation/panel_frames APIs remain a separate marquee model;
they are not previews of build_payload. Use information_preview.py to decode
the actual current GIF and bitmap payloads, including safety variants.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from enum import Enum
from io import BytesIO
import json
import math
import re
import unicodedata
from typing import Any, Iterable, Mapping, Sequence

from panel_scene import encode_icon_gif, lane_payload
from visual_system import gamma_lerp, glance_text_width, render_glance_lane, scale_color, measure_text
from PIL import Image


RGB = tuple[int, int, int]
Frame = list[list[RGB]]

WIDTH = 32
HEIGHT = 8
ICON_WIDTH = 8
GUTTER_X = 8
LANE_X0 = 9
LANE_WIDTH = 23

PANEL_GAMMA = 2.2
PANEL_SATURATION = 90
FRAME_MS = 120
SCROLL_GAP = 6
MAX_FRAMES = 64
MAX_MESSAGE_WIDTH = MAX_FRAMES - SCROLL_GAP
MAX_BODY_BYTES = 8_192
PAGE_DURATION_MS = 8_000
LIFETIME_MS = 15 * 60 * 1_000

# Source values are intentionally bright.  The panel applies gamma 2.2 and is
# normally run at brightness 45; pre-darkening these colors would erase them.
BLACK: RGB = (0, 0, 0)
PEARL: RGB = (255, 247, 232)
ICE: RGB = (222, 235, 250)
SLATE: RGB = (174, 190, 214)
SKY: RGB = (88, 205, 255)
GOLD: RGB = (255, 214, 78)
AMBER: RGB = (255, 158, 58)
CORAL: RGB = (255, 82, 104)
VIOLET: RGB = (204, 116, 255)
MINT: RGB = (82, 240, 168)
TRACK_COOL: RGB = (90, 106, 142)
TRACK_WARM: RGB = (112, 78, 96)


class Urgency(str, Enum):
    ROUTINE = "routine"
    SOON = "soon"
    IMMINENT = "imminent"
    NOW = "now"


URGENCY_ALIASES = {
    "normal": Urgency.ROUTINE,
    "later": Urgency.ROUTINE,
    "upcoming": Urgency.ROUTINE,
    "near": Urgency.SOON,
    "urgent": Urgency.IMMINENT,
    "due": Urgency.NOW,
    "started": Urgency.NOW,
}


# Every glyph is at most 3x5, but narrow characters really are narrow.  That
# extra room matters on a 23-column lane and removes the uneven cadence of a
# fake fixed-width font.
FONT_3X5: dict[str, tuple[str, ...]] = {
    " ": ("0", "0", "0", "0", "0"),
    "-": ("000", "000", "111", "000", "000"),
    ".": ("0", "0", "0", "0", "1"),
    ":": ("0", "1", "0", "1", "0"),
    "0": ("111", "101", "101", "101", "111"),
    # A bare single column: identical to "I" in this font, and thin
    # enough to read as a gap between its neighbours.
    "1": ("010", "110", "010", "010", "111"),
    "2": ("110", "001", "010", "100", "111"),
    "3": ("110", "001", "010", "001", "110"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "110", "001", "110"),
    "6": ("011", "100", "111", "101", "111"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "110"),
    "A": ("010", "101", "111", "101", "101"),
    "B": ("110", "101", "110", "101", "110"),
    "C": ("111", "100", "100", "100", "111"),
    "D": ("110", "101", "101", "101", "110"),
    "E": ("111", "100", "110", "100", "111"),
    "F": ("111", "100", "110", "100", "100"),
    "G": ("111", "100", "101", "101", "111"),
    "H": ("101", "101", "111", "101", "101"),
    # Serifed, so it is neither a bare column nor the digit 1.
    "I": ("111", "010", "010", "010", "111"),
    "J": ("001", "001", "001", "101", "111"),
    "K": ("101", "101", "110", "101", "101"),
    "L": ("100", "100", "100", "100", "111"),
    "M": ("101", "111", "111", "101", "101"),
    "N": ("101", "111", "111", "111", "101"),
    # Rounded, so the letter O is not byte-identical to the digit 0.
    # It was, in every font in this repo.  Rounding the letter rather
    # than altering the digit also dissolves O~8, O~G and O~U, which
    # were each a single pixel apart, and costs no width.
    "O": ("010", "101", "101", "101", "010"),
    "P": ("111", "101", "111", "100", "100"),
    "Q": ("111", "101", "101", "111", "001"),
    "R": ("110", "101", "110", "101", "101"),
    "S": ("111", "100", "111", "001", "111"),
    "T": ("111", "010", "010", "010", "010"),
    "U": ("101", "101", "101", "101", "111"),
    "V": ("101", "101", "101", "101", "010"),
    "W": ("101", "101", "111", "111", "101"),
    "X": ("101", "101", "010", "101", "101"),
    "Y": ("101", "101", "010", "010", "010"),
    "Z": ("111", "001", "010", "100", "111"),
}


# A solid desk-calendar page (2026-10-03, to the Now Playing standard): pearl
# binding rings, a header in the urgency colour with a lighter top edge and a
# darker divider, a light page shaded at its sides and foot, and the date mark.
# The bead orbiting the mark is a comet with a fading trail on the page.
BINDING_TABS = frozenset({(2, 0), (5, 0)})
HEADER_TOP = frozenset((x, 1) for x in range(8))
HEADER_BODY = frozenset((x, 2) for x in range(8))
HEADER_DIVIDER = frozenset((x, 3) for x in range(8))
PAGE_SIDES = frozenset({(0, y) for y in range(4, 8)} | {(7, y) for y in range(4, 8)} | {(x, 7) for x in range(1, 7)})
PAGE = frozenset((x, y) for x in range(1, 7) for y in range(4, 7))
CALENDAR_OUTLINE = BINDING_TABS | HEADER_TOP | HEADER_BODY | HEADER_DIVIDER | PAGE_SIDES
ORBIT_PATH: tuple[tuple[int, int], ...] = (
    (2, 4), (3, 4), (4, 4), (5, 4), (5, 5),
    (5, 6), (4, 6), (3, 6), (2, 6), (2, 5),
)
ORBIT_TRACK = frozenset(ORBIT_PATH)
CALENDAR_MARK = frozenset({(3, 5), (4, 5)})
ICON_OCCUPANCY = CALENDAR_OUTLINE | PAGE
PAGE_LIGHT: RGB = (226, 230, 242)
PAGE_SHADE: RGB = (150, 156, 178)

ETA_COLORS: dict[Urgency, RGB] = {
    Urgency.ROUTINE: SKY,
    Urgency.SOON: VIOLET,
    Urgency.IMMINENT: AMBER,
    Urgency.NOW: CORAL,
}
BEAD_COLORS: dict[Urgency, RGB] = {
    Urgency.ROUTINE: MINT,
    Urgency.SOON: GOLD,
    Urgency.IMMINENT: CORAL,
    Urgency.NOW: PEARL,
}
OUTLINE_COLORS: dict[Urgency, RGB] = {
    Urgency.ROUTINE: SLATE,
    Urgency.SOON: VIOLET,
    Urgency.IMMINENT: AMBER,
    Urgency.NOW: CORAL,
}

EDGE_FADE = (0.24, 0.56, 0.82)

COMMON_ABBREVIATIONS = {
    "APPOINTMENT": "APPT",
    "BIRTHDAY": "BDAY",
    "CONFERENCE": "CONF",
    "DESIGN": "DSGN",
    "DISCUSSION": "DISC",
    "DOCTOR": "DR",
    "INTERVIEW": "INTVW",
    "MEETING": "MTG",
    "PERSONAL": "PERS",
    "PROJECT": "PROJ",
    "QUARTERLY": "QTRLY",
    "REVIEW": "REV",
    "SESSION": "SESS",
    "TOMORROW": "TMRW",
    "WORKOUT": "GYM",
}
STOPWORDS = frozenset({"A", "AN", "AND", "AT", "FOR", "IN", "OF", "ON", "THE", "TO", "WITH"})


@dataclass(frozen=True)
class VisualEvent:
    title: str
    eta: str
    urgency: Urgency
    minutes_until: float | None = None


@dataclass(frozen=True)
class GlyphColumn:
    mask: int
    role: str


BLANK_COLUMN = GlyphColumn(0, "blank")


@dataclass(frozen=True)
class TextLayout:
    eta: str
    title: str
    columns: tuple[GlyphColumn, ...]
    animated: bool
    period: int

    @property
    def width(self) -> int:
        return len(self.columns)

    @property
    def text(self) -> str:
        return f"{self.eta} {self.title}"


@dataclass(frozen=True)
class AnimationPlan:
    event: VisualEvent
    layout: TextLayout
    frames: tuple[Frame, ...]
    durations_ms: tuple[int, ...]

    @property
    def cycle_ms(self) -> int:
        return sum(self.durations_ms)


def new_frame(width: int = WIDTH) -> Frame:
    return [[BLACK for _ in range(width)] for _ in range(HEIGHT)]


def _put(frame: Frame, x: int, y: int, color: RGB) -> None:
    if 0 <= y < len(frame) and 0 <= x < len(frame[y]):
        frame[y][x] = color


def _paint(frame: Frame, points: Iterable[tuple[int, int]], color: RGB) -> None:
    for x, y in points:
        _put(frame, x, y, color)


def normalize_urgency(value: Any, minutes_until: float | None = None) -> Urgency:
    if isinstance(value, Urgency):
        return value
    if value is not None:
        raw = value.value if isinstance(value, Enum) else value
        if not isinstance(raw, str):
            raise ValueError(f"unsupported Next Up urgency: {value!r}")
        key = raw.strip().lower().replace("-", "_").replace(" ", "_")
        if key in URGENCY_ALIASES:
            return URGENCY_ALIASES[key]
        try:
            return Urgency(key)
        except ValueError as error:
            raise ValueError(f"unsupported Next Up urgency: {value!r}") from error
    if minutes_until is None:
        return Urgency.ROUTINE
    if minutes_until <= 0:
        return Urgency.NOW
    if minutes_until <= 5:
        return Urgency.IMMINENT
    if minutes_until <= 15:
        return Urgency.SOON
    return Urgency.ROUTINE


def _finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def format_eta(minutes_until: float) -> str:
    """Format a numeric countdown without claiming more precision than useful."""

    if minutes_until <= 0:
        return "NOW"
    minutes = max(1, int(math.ceil(minutes_until)))
    if minutes < 60:
        return f"{minutes}M"
    if minutes < 24 * 60:
        hours, remainder = divmod(minutes, 60)
        if hours < 10 and remainder and remainder % 5 == 0:
            return f"{hours}H{remainder:02d}"
        return f"{hours}H"
    days = max(1, int(math.ceil(minutes / (24 * 60))))
    return f"{min(days, 99)}D"


def text_width(value: str) -> int:
    if not value:
        return 0
    try:
        widths = [len(FONT_3X5[character][0]) for character in value]
    except KeyError as error:
        raise ValueError(f"unsupported 3x5 character: {error.args[0]!r}") from error
    return sum(widths) + len(widths) - 1


def _fit_prefix(value: str, pixel_budget: int) -> str:
    fitted = ""
    for character in value:
        candidate = fitted + character
        if text_width(candidate) > pixel_budget:
            break
        fitted = candidate
    return fitted.rstrip()


def _ascii_words(value: Any) -> str:
    source = str(value if value is not None else "")
    source = source.replace("&", " AND ")
    source = unicodedata.normalize("NFKD", source).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Z0-9]+", " ", source.upper()).strip()


def normalize_eta(value: Any, minutes_until: float | None = None) -> str:
    numeric = _finite_number(value)
    if numeric is not None:
        return format_eta(numeric)
    if value is None and minutes_until is not None:
        return format_eta(minutes_until)

    raw = str(value if value is not None else "SOON").strip().upper()
    if raw in {"NOW", "DUE", "STARTED", "LIVE"}:
        return "NOW"
    if "TOMORROW" in raw or raw == "TMRW":
        return "TMRW"
    if raw in {"TODAY", "LATER TODAY"}:
        return "TDAY"

    match = re.search(r"(?:IN\s*)?(\d+)\s*(MINUTES?|MINS?|M)\b", raw)
    if match:
        return f"{min(999, int(match.group(1)))}M"
    match = re.search(r"(?:IN\s*)?(\d+)\s*(HOURS?|HRS?|HR|H)\b", raw)
    if match:
        return f"{min(99, int(match.group(1)))}H"
    match = re.search(r"(?:IN\s*)?(\d+)\s*(DAYS?|D)\b", raw)
    if match:
        return f"{min(99, int(match.group(1)))}D"

    compact = _ascii_words(raw).replace(" ", "") or "SOON"
    # Fifteen pixels leaves room for a real title even in the static fallback.
    return _fit_prefix(compact, 15) or "SOON"


def normalize_title(value: Any) -> str:
    return _ascii_words(value) or "EVENT"


def _contract_word(word: str) -> str:
    replacement = COMMON_ABBREVIATIONS.get(word)
    if replacement is not None:
        return replacement
    if len(word) <= 5:
        return word
    consonants = word[0] + "".join(character for character in word[1:] if character not in "AEIOU")
    return consonants if len(consonants) >= 3 else word


def _first_fitting(candidates: Iterable[str], pixel_budget: int) -> str | None:
    seen: set[str] = set()
    for candidate in candidates:
        candidate = re.sub(r"\s+", " ", candidate).strip()
        if candidate and candidate not in seen:
            seen.add(candidate)
            if text_width(candidate) <= pixel_budget:
                return candidate
    return None


def shorten_title(value: Any, pixel_budget: int) -> str:
    """Deterministically preserve meaning while fitting a pixel budget.

    The order is intentional: whole wording, known abbreviations, filler-word
    removal, consonant contraction, then a word-plus-initials summary.  Raw
    character clipping is only the final fallback for an unusually long
    single token.
    """

    if pixel_budget < 1:
        raise ValueError("title pixel budget must be positive")
    title = normalize_title(value)
    words = title.split()
    abbreviated = [COMMON_ABBREVIATIONS.get(word, word) for word in words]
    meaningful = [word for word in abbreviated if word not in STOPWORDS] or abbreviated
    contracted = [_contract_word(word) for word in meaningful]

    direct = _first_fitting(
        (
            title,
            " ".join(abbreviated),
            " ".join(meaningful),
            " ".join(contracted),
        ),
        pixel_budget,
    )
    if direct is not None:
        return direct

    # Keep as many recognizable leading words as possible, then spend the
    # remaining pixels on initials from the omitted phrase.  Exceptionally
    # long imported descriptions can contain dozens of words, so requiring an
    # initial for *every* omitted word would perversely throw away the useful
    # leading noun just to fit an enormous acronym.
    for count in range(len(contracted) - 1, -1, -1):
        leading = contracted[:count]
        remainder = "".join(word[0] for word in contracted[count:] if word)
        leading_text = " ".join(leading)
        initial_budget = pixel_budget
        if leading_text:
            if text_width(leading_text) >= pixel_budget:
                continue
            # A rendered word space occupies one column and also has the
            # font's one-column inter-character gap on both sides.
            initial_budget -= text_width(leading_text) + 3
        initials = _fit_prefix(remainder, initial_budget)
        candidate = " ".join(part for part in (leading_text, initials) if part)
        if candidate and text_width(candidate) <= pixel_budget:
            return candidate

    prefix = _fit_prefix(contracted[0] if contracted else "EVENT", pixel_budget)
    if prefix:
        return prefix
    # All supported letters are at least one pixel wide; this only occurs with
    # a zero-sized caller budget, rejected above, but keeps the invariant clear.
    return "E"


def _read_field(event: Any, names: Sequence[str]) -> Any:
    if event is None or isinstance(event, str):
        return None
    if isinstance(event, Mapping):
        for name in names:
            if name in event and event[name] is not None:
                return event[name]
        return None
    for name in names:
        if hasattr(event, name):
            value = getattr(event, name)
            if value is not None:
                return value
    return None


def coerce_event(
    event: Any = None,
    *,
    title: Any = None,
    eta: Any = None,
    minutes_until: Any = None,
    urgency: Any = None,
) -> VisualEvent:
    """Coerce generic source fields without importing the source adapter."""

    source_title = event if isinstance(event, str) else _read_field(
        event, ("title", "summary", "name", "subject")
    )
    source_eta = _read_field(event, ("eta", "eta_text", "countdown", "when"))
    source_minutes = _read_field(event, ("minutes_until", "minutes", "eta_minutes"))
    source_urgency = _read_field(event, ("urgency", "state", "priority"))

    chosen_title = title if title is not None else source_title
    chosen_eta = eta if eta is not None else source_eta
    chosen_minutes = minutes_until if minutes_until is not None else source_minutes
    chosen_urgency = urgency if urgency is not None else source_urgency
    numeric_minutes = _finite_number(chosen_minutes)

    return VisualEvent(
        title=normalize_title(chosen_title),
        eta=normalize_eta(chosen_eta, numeric_minutes),
        urgency=normalize_urgency(chosen_urgency, numeric_minutes),
        minutes_until=numeric_minutes,
    )


def _glyph_columns(value: str, role: str) -> tuple[GlyphColumn, ...]:
    output: list[GlyphColumn] = []
    for character_index, character in enumerate(value):
        glyph = FONT_3X5[character]
        for x in range(len(glyph[0])):
            mask = 0
            for y, row in enumerate(glyph):
                if row[x] == "1":
                    mask |= 1 << y
            output.append(GlyphColumn(mask, role))
        if character_index < len(value) - 1:
            output.append(GlyphColumn(0, role))
    return tuple(output)


def build_layout(
    event: Any = None,
    *,
    title: Any = None,
    eta: Any = None,
    minutes_until: Any = None,
    urgency: Any = None,
    max_message_width: int = MAX_MESSAGE_WIDTH,
) -> TextLayout:
    visual = coerce_event(
        event,
        title=title,
        eta=eta,
        minutes_until=minutes_until,
        urgency=urgency,
    )
    if not LANE_WIDTH <= max_message_width <= MAX_MESSAGE_WIDTH:
        raise ValueError(
            f"message width must be {LANE_WIDTH}..{MAX_MESSAGE_WIDTH} pixels"
        )
    eta_width = text_width(visual.eta)
    title_budget = max_message_width - eta_width - 1
    compact_title = shorten_title(visual.title, title_budget)
    columns = (
        _glyph_columns(visual.eta, "eta")
        + (BLANK_COLUMN,)
        + _glyph_columns(compact_title, "title")
    )
    if len(columns) > max_message_width:
        raise ValueError("semantic shortening failed to fit the Next Up lane")
    animated = len(columns) > LANE_WIDTH
    period = len(columns) + SCROLL_GAP if animated else 1
    if period > MAX_FRAMES:
        raise ValueError("Next Up marquee exceeds the 64-frame safety limit")
    return TextLayout(visual.eta, compact_title, columns, animated, period)


def lane_source_columns(layout: TextLayout, phase: int = 0) -> tuple[GlyphColumn, ...]:
    """Return the unpainted 23-column viewport for exact cadence tests."""

    if not layout.animated:
        left = (LANE_WIDTH - layout.width) // 2
        right = LANE_WIDTH - left - layout.width
        return (BLANK_COLUMN,) * left + layout.columns + (BLANK_COLUMN,) * right
    stream = layout.columns + (BLANK_COLUMN,) * SCROLL_GAP
    start = int(phase) % len(stream)
    return tuple(stream[(start + x) % len(stream)] for x in range(LANE_WIDTH))


def gamma_fade(color: RGB, apparent_strength: float, gamma: float = PANEL_GAMMA) -> RGB:
    """Fade in emitted-light space, compensating for the panel's gamma stage."""

    if color == BLACK or apparent_strength <= 0:
        return BLACK
    if apparent_strength >= 1:
        return color
    source_factor = apparent_strength ** (1.0 / gamma) if gamma > 0 else apparent_strength
    return tuple(
        max(1, min(255, int(math.floor(channel * source_factor + 0.5))))
        if channel else 0
        for channel in color
    )  # type: ignore[return-value]


def edge_strength(local_x: int) -> float:
    distance = min(local_x, LANE_WIDTH - 1 - local_x)
    return EDGE_FADE[distance] if distance < len(EDGE_FADE) else 1.0


def _text_color(role: str, urgency: Urgency) -> RGB:
    if role == "eta":
        return ETA_COLORS[urgency]
    if role == "title":
        return PEARL if urgency is Urgency.NOW else ICE
    return BLACK


def bead_point(
    urgency: Urgency | str,
    phase: int,
    total_frames: int,
    *,
    scrolling: bool,
) -> tuple[int, int]:
    urgency = normalize_urgency(urgency)
    if urgency is Urgency.NOW:
        return min(CALENDAR_MARK)
    if total_frames <= 0:
        raise ValueError("total_frames must be positive")
    if scrolling:
        laps = {
            Urgency.ROUTINE: 1,
            Urgency.SOON: 2,
            Urgency.IMMINENT: 3,
        }[urgency]
        step = (int(phase) * len(ORBIT_PATH) * laps) // total_frames
    else:
        step = int(phase)
    return ORBIT_PATH[step % len(ORBIT_PATH)]


def calendar_frame(
    urgency: Urgency | str,
    phase: int = 0,
    total_frames: int = len(ORBIT_PATH),
    *,
    scrolling: bool = False,
) -> Frame:
    urgency = normalize_urgency(urgency)
    frame = new_frame(ICON_WIDTH)
    header = OUTLINE_COLORS[urgency]
    _paint(frame, BINDING_TABS, PEARL)
    _paint(frame, HEADER_TOP, gamma_lerp(header, PEARL, 0.35))
    _paint(frame, HEADER_BODY, header)
    _paint(frame, HEADER_DIVIDER, scale_color(header, 0.8))
    _paint(frame, PAGE, PAGE_LIGHT)
    _paint(frame, PAGE_SIDES, PAGE_SHADE)
    _paint(frame, CALENDAR_MARK, GOLD if urgency is not Urgency.NOW else AMBER)
    head = bead_point(urgency, phase, total_frames, scrolling=scrolling)
    bead = BEAD_COLORS[urgency]
    if urgency is Urgency.NOW:
        _put(frame, *head, bead)
    else:
        index = ORBIT_PATH.index(head)
        for back, mix in ((1, 0.5), (0, 1.0)):  # a two-pixel comet keeps the GIF under the cap
            x, y = ORBIT_PATH[(index - back) % len(ORBIT_PATH)]
            _put(frame, x, y, gamma_lerp(PAGE_LIGHT, bead, mix))
    return frame


def panel_frame(
    event: VisualEvent,
    layout: TextLayout,
    phase: int,
    total_frames: int,
) -> Frame:
    frame = new_frame()
    icon = calendar_frame(
        event.urgency,
        phase,
        total_frames,
        scrolling=layout.animated,
    )
    for y in range(HEIGHT):
        frame[y][:ICON_WIDTH] = icon[y]
        frame[y][GUTTER_X] = BLACK

    viewport = lane_source_columns(layout, phase)
    for local_x, column in enumerate(viewport):
        color = _text_color(column.role, event.urgency)
        if layout.animated:
            color = gamma_fade(color, edge_strength(local_x))
        for glyph_y in range(5):
            if column.mask & (1 << glyph_y):
                _put(frame, LANE_X0 + local_x, 1 + glyph_y, color)
    return frame


def _short_frame_duration(urgency: Urgency) -> int:
    return {
        Urgency.ROUTINE: 3 * FRAME_MS,
        Urgency.SOON: 2 * FRAME_MS,
        Urgency.IMMINENT: FRAME_MS,
        Urgency.NOW: FRAME_MS,
    }[urgency]


def build_animation(
    event: Any = None,
    *,
    title: Any = None,
    eta: Any = None,
    minutes_until: Any = None,
    urgency: Any = None,
    animated: bool = True,
    max_message_width: int = MAX_MESSAGE_WIDTH,
) -> AnimationPlan:
    visual = coerce_event(
        event,
        title=title,
        eta=eta,
        minutes_until=minutes_until,
        urgency=urgency,
    )
    layout = build_layout(
        visual,
        max_message_width=max_message_width if animated else LANE_WIDTH,
    )

    if not animated:
        frame_count = 1
        durations = (FRAME_MS,)
    elif layout.animated:
        frame_count = layout.period
        durations = (FRAME_MS,) * frame_count
    elif visual.urgency is Urgency.NOW:
        frame_count = 1
        durations = (FRAME_MS,)
    else:
        frame_count = len(ORBIT_PATH)
        durations = (_short_frame_duration(visual.urgency),) * frame_count

    frames = tuple(
        panel_frame(visual, layout, phase, frame_count)
        for phase in range(frame_count)
    )
    validate_frames(frames)
    return AnimationPlan(visual, layout, frames, durations)


def panel_frames(
    event: Any = None,
    **kwargs: Any,
) -> list[Frame]:
    """Public preview helper returning independent exact-size frame lists."""

    return [
        [list(row) for row in frame]
        for frame in build_animation(event, **kwargs).frames
    ]


def validate_frames(frames: Iterable[Frame]) -> None:
    count = 0
    for frame in frames:
        if len(frame) != HEIGHT or any(len(row) != WIDTH for row in frame):
            raise ValueError("every Next Up frame must be exactly 32x8")
        for row in frame:
            for color in row:
                if len(color) != 3 or any(not isinstance(value, int) or not 0 <= value <= 255 for value in color):
                    raise ValueError("Next Up frames must contain RGB888 pixels")
        if any(frame[y][GUTTER_X] != BLACK for y in range(HEIGHT)):
            raise ValueError("Next Up gutter x=8 must remain true black")
        if any(
            frame[y][x] != BLACK
            for y in (0, 6, 7)
            for x in range(LANE_X0, WIDTH)
        ):
            raise ValueError("Next Up text lane may not contain decorative bars")
        occupied = {
            (x, y)
            for y in range(HEIGHT)
            for x in range(ICON_WIDTH)
            if frame[y][x] != BLACK
        }
        if occupied != ICON_OCCUPANCY:
            raise ValueError("calendar occupancy must stay stable; the bead may not blink")
        count += 1
    if count == 0:
        raise ValueError("Next Up animation needs at least one frame")
    if count > MAX_FRAMES:
        raise ValueError("Next Up animation exceeds 64 frames")


def _frame_image(frame: Frame) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BLACK)
    image.putdata([color for row in frame for color in row])
    return image


def _encode_plan(plan: AnimationPlan) -> bytes:
    images = [_frame_image(frame) for frame in plan.frames]
    stream = BytesIO()
    if len(images) == 1:
        images[0].save(stream, format="GIF", optimize=True)
    else:
        images[0].save(
            stream,
            format="GIF",
            save_all=True,
            append_images=images[1:],
            duration=list(plan.durations_ms),
            loop=0,
            disposal=1,
            optimize=True,
        )
    data = stream.getvalue()
    validate_gif(data, len(images), plan.durations_ms)
    return data


def validate_gif(data: bytes, expected_frames: int, durations_ms: Sequence[int]) -> None:
    if not data.startswith((b"GIF87a", b"GIF89a")):
        raise ValueError("Next Up canvas is not a GIF")
    try:
        with Image.open(BytesIO(data)) as image:
            if image.size != (WIDTH, HEIGHT):
                raise ValueError("Next Up GIF must be exactly 32x8")
            if image.n_frames != expected_frames:
                raise ValueError(
                    f"Next Up GIF has {image.n_frames} frames; expected {expected_frames}"
                )
            if "transparency" in image.info:
                raise ValueError("Next Up GIF must be fully opaque")
            if expected_frames > 1:
                for index, duration in enumerate(durations_ms):
                    image.seek(index)
                    if int(image.info.get("duration", 0)) != int(duration):
                        raise ValueError("Next Up GIF cadence changed during encoding")
    except OSError as error:
        raise ValueError("Next Up GIF is not readable") from error


def build_gif(
    event: Any = None,
    **kwargs: Any,
) -> bytes:
    """Build the production full-canvas GIF; pass ``animated=False`` for safe mode."""

    return _encode_plan(build_animation(event, **kwargs))


def _payload(plan: AnimationPlan, gif: bytes, duration_ms: int, lifetime_ms: int) -> dict[str, Any]:
    return {
        "icon": base64.b64encode(gif).decode("ascii"),
        "iconMode": "fixed",
        "iconOffsetX": 0,
        "backgroundColor": "#000000",
        # A page may never rotate away before one complete local pass.
        "durationMs": max(int(duration_ms), plan.cycle_ms),
        "lifetimeMs": int(lifetime_ms),
        "lifetimeExpiry": "remove",
    }


def serialized_size(payload: Mapping[str, Any]) -> int:
    # requests' json= path includes default separators, so validate that larger
    # representation rather than only the most optimistic compact encoding.
    return len(json.dumps(payload, ensure_ascii=True).encode("utf-8"))


GLANCE_LEVELS = {
    Urgency.ROUTINE: "info",
    Urgency.SOON: "fair",
    Urgency.IMMINENT: "poor",
    Urgency.NOW: "bad",
}
GLANCE_LEAD_MINUTES = 60


MIN_LABEL_LETTERS = 3
CARD_FRAME_MS = 480


def card_title(title: str) -> str:
    """Use the actual lane font width; aliases and abbreviations keep identity."""
    budget = LANE_WIDTH
    label = shorten_title(title, budget)
    while measure_text(label) > LANE_WIDTH and budget > 1:
        budget -= 1
        label = shorten_title(title, budget)
    return label


def calendar_cards(visual: VisualEvent) -> list[Frame]:
    """Two held cards with a continuous ten-position calendar orbit.

    Time and identity each get 2.4 seconds. NOW keeps a still calendar icon.
    The same remaining-time rail is carried through both cards.
    """
    label = card_title(visual.title)
    meter = None if visual.minutes_until is None else max(
        0.0, min(1.0, visual.minutes_until / GLANCE_LEAD_MINUTES))
    output = []
    count = len(ORBIT_PATH)
    for phase in range(count):
        text = visual.eta if phase < count // 2 else label
        frame = render_glance_lane(text, meter=meter, level=GLANCE_LEVELS[visual.urgency])
        icon = calendar_frame(visual.urgency, phase, count)
        for y in range(HEIGHT):
            frame[y][:ICON_WIDTH] = icon[y]
        output.append(frame)
    return output


def _encode_cards(frames: Sequence[Frame]) -> bytes:
    # A shared exact palette allows GIF delta rectangles, avoiding a full
    # local color table per frame. No quantization or loss of shading.
    colors = list(dict.fromkeys(pixel for frame in frames for row in frame for pixel in row))
    if len(colors) > 256:
        raise ValueError('Calendar cards exceed the exact GIF palette')
    palette = bytes(channel for color in colors for channel in color)
    palette += bytes(3 * (2 ** max(1, (len(colors) - 1).bit_length()) - len(colors)))
    indices = {color: index for index, color in enumerate(colors)}
    images = []
    for frame in frames:
        image = Image.new('P', (WIDTH, HEIGHT))
        image.putpalette(palette)
        image.putdata([indices[pixel] for row in frame for pixel in row])
        images.append(image)
    stream = BytesIO()
    images[0].save(stream, format='GIF', save_all=True, append_images=images[1:],
                   duration=CARD_FRAME_MS, loop=0, disposal=1, optimize=False, palette=palette)
    encoded = stream.getvalue()
    if len(encoded) > 1024:
        raise ValueError('Calendar cards exceed the 1KB image budget')
    return encoded


def build_payload(
    event: Any = None,
    *,
    title: Any = None,
    eta: Any = None,
    minutes_until: Any = None,
    urgency: Any = None,
    animated: bool = True,
    duration_ms: int = PAGE_DURATION_MS,
    lifetime_ms: int = LIFETIME_MS,
) -> dict[str, Any]:
    """Show held ETA/title cards; safety mode retains the still ETA lane.

    Cards use a bounded 32x8 GIF with exact shared palette and delta frames.
    No marquee or extra app slot is needed. The legacy build_animation API
    remains a separate marquee model; payload-exact review decodes this GIF.
    """

    if int(duration_ms) <= 0 or int(lifetime_ms) <= 0:
        raise ValueError("duration_ms and lifetime_ms must be positive")
    visual = coerce_event(event, title=title, eta=eta, minutes_until=minutes_until, urgency=urgency)
    if animated:
        payload = {
            'icon': base64.b64encode(_encode_cards(calendar_cards(visual))).decode('ascii'),
            'iconMode': 'fixed', 'iconOffsetX': 0, 'draw': [],
            'backgroundColor': '#000000', 'durationMs': max(int(duration_ms), CARD_FRAME_MS * len(ORBIT_PATH)),
            'lifetimeMs': int(lifetime_ms), 'lifetimeExpiry': 'remove',
        }
        if int(lifetime_ms) < CARD_FRAME_MS * len(ORBIT_PATH):
            raise ValueError('lifetime_ms must cover one calendar card cycle')
        if serialized_size(payload) >= 2048:
            raise ValueError('Calendar cards exceed the 2KB payload budget')
        return payload
    icon = calendar_frame(visual.urgency, 0, 1)
    gif = encode_icon_gif([icon], beat_ms=_short_frame_duration(visual.urgency))

    label = None
    room = LANE_WIDTH - 4 - glance_text_width(visual.eta)
    if room >= 3 and visual.title:
        shortened = shorten_title(visual.title, room)
        # A lone initial ("D" for DENTIST) reads as noise beside the time;
        # below three letters the countdown stands alone.  Calendar aliases
        # in the hub config are the way to give a long title a short name.
        if shortened and len(shortened.replace(" ", "")) >= MIN_LABEL_LETTERS:
            label = shortened
    meter = None
    if visual.minutes_until is not None:
        meter = max(0.0, min(1.0, visual.minutes_until / GLANCE_LEAD_MINUTES))
    lane = render_glance_lane(visual.eta, label=label, meter=meter,
                              level=GLANCE_LEVELS[visual.urgency])
    return lane_payload(lane, gif, duration_ms=int(duration_ms), lifetime_ms=int(lifetime_ms))


def _desaturate(color: RGB, saturation: int) -> RGB:
    saturation = max(0, min(100, int(saturation)))
    red, green, blue = color
    gray = (77 * red + 150 * green + 29 * blue) >> 8
    return tuple(
        gray + math.trunc((channel - gray) * saturation / 100)
        for channel in color
    )  # type: ignore[return-value]


def panel_grade_color(
    color: RGB,
    *,
    gamma: float = PANEL_GAMMA,
    brightness: int = 45,
    saturation: int = PANEL_SATURATION,
) -> RGB:
    """Approximate the panel saturation -> gamma -> brightness pipeline."""

    mixed = _desaturate(color, saturation)
    brightness = max(0, min(255, int(brightness)))
    graded: list[int] = []
    for channel in mixed:
        if channel == 0:
            gamma16 = 0
        else:
            fraction = (channel / 255.0) ** gamma if gamma > 0 else channel / 255.0
            gamma16 = max(1, min(65535, math.floor(fraction * 65535 + 0.5)))
        lit = gamma16 * brightness // 255
        graded.append(min(255, (lit + 128) // 257))
    return tuple(graded)  # type: ignore[return-value]


__all__ = [
    "AnimationPlan",
    "BLACK",
    "CALENDAR_MARK",
    "FRAME_MS",
    "Frame",
    "GUTTER_X",
    "HEIGHT",
    "LANE_WIDTH",
    "LANE_X0",
    "MAX_BODY_BYTES",
    "MAX_FRAMES",
    "PANEL_GAMMA",
    "PANEL_SATURATION",
    "TextLayout",
    "Urgency",
    "VisualEvent",
    "WIDTH",
    "bead_point",
    "build_animation",
    "build_gif",
    "build_layout",
    "build_payload",
    "calendar_frame",
    "coerce_event",
    "edge_strength",
    "format_eta",
    "gamma_fade",
    "lane_source_columns",
    "normalize_eta",
    "panel_frame",
    "panel_frames",
    "panel_grade_color",
    "serialized_size",
    "shorten_title",
    "text_width",
    "validate_frames",
    "validate_gif",
]
