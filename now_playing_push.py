r"""
Show the media currently playing on this Windows PC on the AWTRIX panel.

The app uses Windows' Global System Media Transport Controls, so it can read
compatible players such as Spotify, browser video, and the Windows media apps.
It appears only while something is genuinely playing and removes itself a few
seconds after playback pauses or stops.

SETUP
    C:\Users\devyn\anaconda3\Scripts\pip.exe install ^
        "winrt-Windows.Media.Control[all]==3.2.1" Pillow requests

RUN
    C:\Users\devyn\anaconda3\python.exe now_playing_push.py

The scheduled task must run only in the signed-in user's interactive session;
Windows media sessions are not available to a background service account.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from io import BytesIO
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from logging.handlers import RotatingFileHandler
import math
from pathlib import Path
import re
import socket
import sys
import threading
import time
import unicodedata
from typing import Any
from urllib.parse import urlsplit

import requests

from now_playing_bridge import (
    BridgeState,
    BridgeStateStore,
    STATE_PATH,
    ensure_script_installed,
    install_source,
    make_server,
    remove_legacy_pushed_app,
    resolve_lan_ip,
)
import panel_gate

try:
    from PIL import Image
except ImportError:  # Keep the setup error useful under pythonw.exe.
    Image = None

try:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as MediaManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
    )
except ImportError:  # Tests and the preview renderer do not require WinRT.
    MediaManager = None
    PlaybackStatus = None


# ---------------------------------------------------------------- config
# `pixelclock.local` currently advertises a stale second address on this LAN.
# Use the reachable panel directly so a new-track push cannot randomly time out.
AWTRIX = "http://192.168.1.90"
APP_NAME = "nowplaying"
GATE_SOURCE = "nowplaying"
PUSH_URL = f"{AWTRIX}/api/v1/apps/pushed/{APP_NAME}"
DELETE_URL = f"{AWTRIX}/api/v1/apps/{APP_NAME}"
ACTIVE_URL = f"{AWTRIX}/api/v1/apps/active"
DEVICE_URL = f"{AWTRIX}/api/v1/device"
USE_DEVICE_SCRIPT_RENDERER = True
STATE_SERVER_PORT = 8765
STATE_STALE_SECONDS = 15.0
SCRIPT_TEMPLATE_PATH = Path(__file__).with_name("now_playing.ax")

WIDTH = 32
HEIGHT = 8
ICON_WIDTH = 8
TEXT_X = 9
TEXT_WIDTH = 23
# A native progress bar begins one pixel before the text when an icon is present.
PROGRESS_X = 8
PROGRESS_WIDTH = 24

MEDIA_POLL_SECONDS = 1.0
# How often to check that this PC still has the address the panel was told to
# poll. Five minutes is far more often than a DHCP lease changes and costs one
# UDP socket to the routing table, no network traffic.
LAN_RECHECK_SECONDS = 300.0
KEEPALIVE_SECONDS = 30.0
PROGRESS_REFRESH_SECONDS = 10.0
PAUSE_GRACE_SECONDS = 2.5
MEDIA_ERROR_GRACE_SECONDS = 5.0
NETWORK_RETRY_MAX_SECONDS = 30.0
SAFE_MODE_FAILURES = 5
MAX_ACTIVATION_ATTEMPTS = 3
# Leave room for a busy ESP32 without turning transient work into a failure storm.
REQUEST_TIMEOUT_SECONDS = 10
LIFETIME_MS = 45_000
WINRT_INSTALL_COMMAND = (
    r'C:\Users\devyn\anaconda3\Scripts\pip.exe install '
    r'"winrt-Windows.Media.Control[all]==3.2.1" Pillow requests'
)

RECORD_FRAME_COUNT = 12
RECORD_FRAME_MS = 150
RECORD_PALETTE_COLORS = 8
RECORD_GIF_MAX_BYTES = 1_024
MAX_DISPLAY_TEXT_CHARS = 120
# Speed 100 advances the native scroller by exactly half a logical pixel per
# render tick, producing a regular one-LED step every two ticks on NG v1.1.0.
SCROLL_SPEED = 100
SCROLL_GAP = 8
SCROLL_HOLD_MS = 1_000
# Used only by the local review renderer for fitting/static text. Long native
# text is governed by repeat=1 and one complete loop fold, not a hard timeout.
PAGE_DURATION_MS = 7_000

BLACK = (0, 0, 0)
TEXT_COLOR = (255, 244, 234)
TEXT_COLOR_HEX = "#FFF4EA"
PROGRESS_TRACK_COLOR = "#35131F"
PROGRESS_PALETTE = ("#FFB72F", "#F07A32", "#FF5275")
PROGRESS_SWEEP_PHASES = 3
PROGRESS_SWEEP_FINAL_PHASE = PROGRESS_SWEEP_PHASES - 1
PROGRESS_SWEEP_FRAME_SECONDS = 0.25

RECORD_COLORS: dict[str, tuple[int, int, int]] = {
    ".": BLACK,
    "K": BLACK,
    "D": (169, 45, 80),
    "C": (255, 82, 117),
    "H": (255, 212, 221),
    "P": (255, 142, 166),
    "W": (255, 244, 234),
    "G": (255, 183, 47),
    "A": (240, 122, 50),
}
# -----------------------------------------------------------------------


# A compact variable-width 3x5 font. The narrow I and punctuation preserve
# precious columns on a 32x8 matrix; VERIDIS is exactly 23 pixels wide.
FONT: dict[str, tuple[str, str, str, str, str]] = {
    " ": ("00", "00", "00", "00", "00"),
    "A": ("010", "101", "111", "101", "101"),
    "B": ("110", "101", "110", "101", "110"),
    "C": ("011", "100", "100", "100", "011"),
    "D": ("110", "101", "101", "101", "110"),
    "E": ("111", "100", "110", "100", "111"),
    "F": ("111", "100", "110", "100", "100"),
    "G": ("011", "100", "101", "101", "011"),
    "H": ("101", "101", "111", "101", "101"),
    "I": ("1", "1", "1", "1", "1"),
    "J": ("001", "001", "001", "101", "010"),
    "K": ("101", "101", "110", "101", "101"),
    "L": ("100", "100", "100", "100", "111"),
    "M": ("10001", "11011", "10101", "10001", "10001"),
    "N": ("101", "111", "111", "111", "101"),
    "O": ("010", "101", "101", "101", "010"),
    "P": ("110", "101", "110", "100", "100"),
    "Q": ("010", "101", "101", "111", "011"),
    "R": ("110", "101", "110", "101", "101"),
    "S": ("011", "100", "010", "001", "110"),
    "T": ("111", "010", "010", "010", "010"),
    "U": ("101", "101", "101", "101", "111"),
    "V": ("101", "101", "101", "101", "010"),
    "W": ("101", "101", "111", "111", "101"),
    "X": ("101", "101", "010", "101", "101"),
    "Y": ("101", "101", "010", "010", "010"),
    "Z": ("111", "001", "010", "100", "111"),
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("110", "001", "010", "100", "111"),
    "3": ("110", "001", "010", "001", "110"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "110", "001", "110"),
    "6": ("011", "100", "110", "101", "010"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("010", "101", "010", "101", "010"),
    "9": ("010", "101", "011", "001", "110"),
    "-": ("000", "000", "111", "000", "000"),
    ".": ("0", "0", "0", "0", "1"),
    ",": ("00", "00", "00", "01", "10"),
    "'": ("1", "1", "0", "0", "0"),
    ":": ("0", "1", "0", "1", "0"),
    "/": ("001", "001", "010", "100", "100"),
    "?": ("110", "001", "010", "000", "010"),
    "!": ("1", "1", "1", "0", "1"),
    "&": ("010", "101", "010", "101", "011"),
    "(": ("01", "10", "10", "10", "01"),
    ")": ("10", "01", "01", "01", "10"),
    "+": ("000", "010", "111", "010", "000"),
    "_": ("000", "000", "000", "000", "111"),
    "=": ("000", "111", "000", "111", "000"),
    "#": ("101", "111", "101", "111", "101"),
}


RGB = tuple[int, int, int]


@dataclass(frozen=True)
class MediaSnapshot:
    title: str
    artist: str = ""
    source: str = ""
    progress_ratio: float | None = None

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.title, self.artist, self.source)


@dataclass
class TrackActivation:
    """Remember whether this media identity still needs its one immediate show."""

    identity: tuple[str, str, str] | None = None
    pending: bool = False

    def observe(self, snapshot: MediaSnapshot) -> bool:
        if snapshot.identity == self.identity:
            return False
        self.identity = snapshot.identity
        self.pending = True
        return True

    def complete(self) -> None:
        self.pending = False

    def clear(self) -> None:
        self.identity = None
        self.pending = False


PayloadKey = tuple[str, int | None, bool]


@dataclass
class ProgressSweep:
    """Schedule one short, event-driven playhead landing animation."""

    key: PayloadKey | None = None
    identity: tuple[str, str, str] | None = None
    scroll_revision: int = 0
    next_phase: int | None = None
    next_at: float = 0.0

    def start(
        self,
        key: PayloadKey,
        identity: tuple[str, str, str],
        scroll_revision: int,
        now: float,
    ) -> None:
        if key[1] is None or key[1] <= 0:
            self.clear()
            return
        self.key = key
        self.identity = identity
        self.scroll_revision = int(scroll_revision) & 1
        self.next_phase = 1
        self.next_at = now + PROGRESS_SWEEP_FRAME_SECONDS

    def due(
        self,
        key: PayloadKey,
        identity: tuple[str, str, str],
        now: float,
    ) -> bool:
        return (
            self.next_phase is not None
            and self.key == key
            and self.identity == identity
            and now >= self.next_at
        )

    def advance(self, now: float) -> None:
        if self.next_phase is None:
            return
        if self.next_phase >= PROGRESS_SWEEP_FINAL_PHASE:
            self.clear()
            return
        self.next_phase += 1
        self.next_at = now + PROGRESS_SWEEP_FRAME_SECONDS

    def settle_after_failure(self, now: float) -> None:
        if self.next_phase is not None:
            self.next_phase = PROGRESS_SWEEP_FINAL_PHASE
            self.next_at = now

    def clear(self) -> None:
        self.key = None
        self.identity = None
        self.scroll_revision = 0
        self.next_phase = None
        self.next_at = 0.0


def payload_push_due(
    desired_key: PayloadKey,
    last_pushed_key: PayloadKey | None,
    identity: tuple[str, str, str],
    last_pushed_identity: tuple[str, str, str] | None,
    age_seconds: float,
) -> bool:
    """Push meaningful state promptly and otherwise only at a bounded cadence."""
    if identity != last_pushed_identity or last_pushed_key is None:
        return True
    if desired_key[0] != last_pushed_key[0] or desired_key[2] != last_pushed_key[2]:
        return True
    if desired_key[1] != last_pushed_key[1] and age_seconds >= PROGRESS_REFRESH_SECONDS:
        return True
    return age_seconds >= KEEPALIVE_SECONDS


def _mix(start: RGB, end: RGB, amount: float) -> RGB:
    amount = max(0.0, min(1.0, amount))
    return tuple(round(a + (b - a) * amount) for a, b in zip(start, end))  # type: ignore[return-value]


def _gradient(stops: tuple[tuple[float, RGB], ...], amount: float) -> RGB:
    amount = max(0.0, min(1.0, amount))
    for index in range(1, len(stops)):
        right_pos, right_color = stops[index]
        left_pos, left_color = stops[index - 1]
        if amount <= right_pos:
            span = right_pos - left_pos
            local = 0.0 if span == 0 else (amount - left_pos) / span
            return _mix(left_color, right_color, local)
    return stops[-1][1]


def normalize_media_text(value: Any) -> str:
    """Convert media metadata to predictable characters for the pixel font."""
    if value is None:
        return ""
    text = str(value)
    text = (
        text.replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", "'")
        .replace("\u201d", "'")
    )
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = text.upper()
    text = "".join(char if char in FONT else " " for char in text)
    return re.sub(r"\s+", " ", text).strip()


def limit_display_text(text: str) -> str:
    """Cap pathological metadata explicitly instead of looking accidentally cut."""
    text = text.rstrip()
    if len(text) <= MAX_DISPLAY_TEXT_CHARS:
        return text
    return text[: MAX_DISPLAY_TEXT_CHARS - 3].rstrip() + "..."


def display_text(snapshot: MediaSnapshot) -> str:
    title = normalize_media_text(snapshot.title)
    artist = normalize_media_text(snapshot.artist)
    if not title:
        title = "NOW PLAYING"
    artist_pattern = rf"(^|[^A-Z0-9]){re.escape(artist)}([^A-Z0-9]|$)" if artist else ""
    artist_already_named = bool(artist_pattern and re.search(artist_pattern, title))
    if artist and artist != title and not artist_already_named:
        return limit_display_text(f"{title} - {artist}")
    return limit_display_text(title)


def bridge_state_from_snapshot(snapshot: MediaSnapshot, revision: int = 0) -> BridgeState:
    """Translate one Windows media sample into the compact device-script state."""

    identity_source = "\0".join(snapshot.identity).encode("utf-8", "replace")
    identity = hashlib.sha1(identity_source).hexdigest()[:16]
    bucket = progress_bucket(snapshot.progress_ratio)
    return BridgeState(
        playing=True,
        identity=identity,
        text=display_text(snapshot),
        progress=-1 if bucket is None else bucket,
        revision=revision,
    )


def glyph_width(char: str) -> int:
    return len(FONT.get(char, FONT["?"])[0])


def measure_text(text: str) -> int:
    if not text:
        return 0
    return sum(glyph_width(char) for char in text) + len(text) - 1


def progress_bucket(progress_ratio: float | None) -> int | None:
    """Quantize media progress to the native 24-pixel rail."""
    if progress_ratio is None:
        return None
    try:
        progress_ratio = float(progress_ratio)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(progress_ratio):
        return None
    progress_ratio = max(0.0, min(1.0, progress_ratio))
    return int(progress_ratio * PROGRESS_WIDTH + 0.5)


def progress_percent(bucket: int | None) -> int | None:
    """Return a percentage that paints exactly this many native rail pixels."""
    if bucket is None:
        return None
    bucket = max(0, min(PROGRESS_WIDTH, int(bucket)))
    if bucket == 0:
        return 0
    return math.ceil(bucket * 100 / PROGRESS_WIDTH)


def progress_sweep_phase_at(elapsed_ms: int | float) -> int:
    """Return the production sweep phase for a local animation preview."""
    elapsed_seconds = max(0.0, float(elapsed_ms) / 1000.0)
    return min(
        PROGRESS_SWEEP_FINAL_PHASE,
        int(elapsed_seconds / PROGRESS_SWEEP_FRAME_SECONDS),
    )


def progress_rail_pixels(bucket: int | None, phase: int = PROGRESS_SWEEP_FINAL_PHASE) -> tuple[RGB, ...] | None:
    """Build the exact 24-pixel rail without changing the truthful fill length."""
    if bucket is None:
        return None
    bucket = max(0, min(PROGRESS_WIDTH, int(bucket)))
    track = _hex_rgb(PROGRESS_TRACK_COLOR)
    pixels: list[RGB] = [track] * PROGRESS_WIDTH
    stops = tuple(
        (index / (len(PROGRESS_PALETTE) - 1), _hex_rgb(color))
        for index, color in enumerate(PROGRESS_PALETTE)
    )
    for index in range(bucket):
        pixels[index] = _gradient(stops, index / max(1, bucket - 1))

    if bucket > 0:
        phase = max(0, min(PROGRESS_SWEEP_FINAL_PHASE, int(phase)))
        # The cream leader begins three cells behind the endpoint, then lands
        # on it over two follow-up frames. Every glow pixel remains within the
        # real fill; the progress value itself never moves for visual effect.
        leader = max(0, bucket - PROGRESS_SWEEP_PHASES + phase)
        glow = (RECORD_COLORS["W"], RECORD_COLORS["H"], RECORD_COLORS["P"])
        for index in range(leader, bucket):
            pixels[index] = glow[min(index - leader, len(glow) - 1)]
    return tuple(pixels)


def progress_rail_bitmap(bucket: int | None, phase: int = PROGRESS_SWEEP_FINAL_PHASE) -> str | None:
    pixels = progress_rail_pixels(bucket, phase)
    if pixels is None:
        return None
    raw = bytes(channel for pixel in pixels for channel in pixel)
    return base64.b64encode(raw).decode("ascii")


# A four-pixel cream-to-coral comet travels clockwise around the groove while
# the gold/amber center label rotates in the same direction. Neighbouring
# frames overlap, including at the seam, so the disc reads as one continuous
# revolution instead of a sequence of blinking highlights.
_RECORD_BASE: tuple[str, ...] = (
    "..DCCD..",
    ".DCCCCD.",
    "DCCCCCCD",
    "DCCKKCCD",
    "DCCKKCCD",
    "DCCCCCCD",
    ".DCCCCD.",
    "..DCCD..",
)
RECORD_GLINT_PATH: tuple[tuple[int, int], ...] = (
    (2, 1), (3, 1), (4, 1), (5, 1),
    (6, 2), (6, 3), (6, 4), (6, 5),
    (5, 6), (4, 6), (3, 6), (2, 6),
    (1, 5), (1, 4), (1, 3), (1, 2),
)
_RECORD_GLINT_HEADS = (0, 1, 3, 4, 5, 7, 8, 9, 11, 12, 13, 15)
_RECORD_LABEL_PATH: tuple[tuple[int, int], ...] = (
    (3, 2), (4, 2), (5, 3), (5, 4),
    (4, 5), (3, 5), (2, 4), (2, 3),
)


def _build_record_playing_frames() -> tuple[tuple[str, ...], ...]:
    frames: list[tuple[str, ...]] = []
    path_length = len(RECORD_GLINT_PATH)
    label_length = len(_RECORD_LABEL_PATH)
    for head in _RECORD_GLINT_HEADS:
        pixels = [list(row) for row in _RECORD_BASE]
        label_shift = ((head + 1) // 2) % label_length
        for offset, (x, y) in enumerate(_RECORD_LABEL_PATH):
            pixels[y][x] = "G" if (offset - label_shift) % label_length < 4 else "A"
        for behind, color in ((3, "P"), (2, "P"), (1, "H"), (0, "W")):
            x, y = RECORD_GLINT_PATH[(head - behind) % path_length]
            pixels[y][x] = color
        frames.append(tuple("".join(row) for row in pixels))
    return tuple(frames)


RECORD_PLAYING_FRAMES = _build_record_playing_frames()

RECORD_PAUSED_FRAME: tuple[str, ...] = (
    "..DCCD..",
    ".HHCCCD.",
    "DHGKKGCD",
    "DCGKKGCD",
    "DCAKKACD",
    "DCAKKACD",
    ".DCCCCD.",
    "..DCCD..",
)


def render_record_icon_frame(frame_index: int = 0, paused: bool = False) -> bytes:
    """Render one exact 8x8 RGB888 record frame."""
    rows = RECORD_PAUSED_FRAME if paused else RECORD_PLAYING_FRAMES[int(frame_index) % RECORD_FRAME_COUNT]
    if len(rows) != HEIGHT or any(len(row) != ICON_WIDTH for row in rows):
        raise ValueError("record artwork must be exactly 8x8")
    pixels = [RECORD_COLORS[cell] for row in rows for cell in row]
    return bytes(channel for pixel in pixels for channel in pixel)


def _encode_record_gif(frames: list[bytes], duration_ms: int) -> bytes:
    if Image is None:
        raise RuntimeError(f"Missing Pillow dependency. Run: {WINRT_INSTALL_COMMAND}")
    expected = ICON_WIDTH * HEIGHT * 3
    if not frames or any(len(frame) != expected for frame in frames):
        raise ValueError("every record GIF frame must be exactly 8x8 RGB888")

    rgb_frames = [Image.frombytes("RGB", (ICON_WIDTH, HEIGHT), frame) for frame in frames]
    quantize = getattr(Image, "Quantize", Image)
    dither = getattr(Image, "Dither", Image)
    palette = rgb_frames[0].quantize(
        colors=RECORD_PALETTE_COLORS,
        method=quantize.FASTOCTREE,
        dither=dither.NONE,
    )
    paletted = [palette]
    paletted.extend(frame.quantize(palette=palette, dither=dither.NONE) for frame in rgb_frames[1:])

    output = BytesIO()
    paletted[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=paletted[1:],
        duration=[duration_ms] * len(paletted),
        loop=0,
        disposal=1,
        optimize=True,
    )
    gif_bytes = output.getvalue()
    if len(gif_bytes) > RECORD_GIF_MAX_BYTES:
        raise ValueError(f"record GIF is unexpectedly large ({len(gif_bytes)} bytes)")
    return gif_bytes


def build_record_icon_gif(paused: bool = False) -> bytes:
    frames = (
        [render_record_icon_frame(paused=True)]
        if paused
        else [render_record_icon_frame(index) for index in range(RECORD_FRAME_COUNT)]
    )
    duration = round(PAUSE_GRACE_SECONDS * 1000) if paused else RECORD_FRAME_MS
    return _encode_record_gif(frames, duration)


def gif_duration_ms(gif_bytes: bytes) -> int:
    """Read the actual centisecond-rounded duration encoded in a GIF."""
    if Image is None:
        raise RuntimeError(f"Missing Pillow dependency. Run: {WINRT_INSTALL_COMMAND}")
    total = 0
    with Image.open(BytesIO(gif_bytes)) as image:
        default_duration = int(image.info.get("duration", RECORD_FRAME_MS))
        for frame_index in range(image.n_frames):
            image.seek(frame_index)
            total += int(image.info.get("duration", default_duration))
    return total


def validate_record_icon_gif(icon_gif: bytes, paused: bool = False) -> None:
    """Enforce the tiny-icon boundary that keeps this payload panel-safe."""
    if Image is None:
        raise RuntimeError(f"Missing Pillow dependency. Run: {WINRT_INSTALL_COMMAND}")
    if not isinstance(icon_gif, bytes) or not icon_gif.startswith((b"GIF87a", b"GIF89a")):
        raise ValueError("icon must be GIF data")
    if len(icon_gif) > RECORD_GIF_MAX_BYTES:
        raise ValueError(f"record icon exceeds {RECORD_GIF_MAX_BYTES} bytes")
    try:
        with Image.open(BytesIO(icon_gif)) as image:
            if image.size != (ICON_WIDTH, HEIGHT):
                raise ValueError("record icon must be exactly 8x8")
            allowed_frames = {1} if paused else {1, RECORD_FRAME_COUNT}
            if image.n_frames not in allowed_frames:
                raise ValueError(
                    f"record icon has {image.n_frames} frames; expected {sorted(allowed_frames)}"
                )
            for frame_index in range(image.n_frames):
                image.seek(frame_index)
                if image.size != (ICON_WIDTH, HEIGHT):
                    raise ValueError("every record icon frame must be exactly 8x8")
    except OSError as error:
        raise ValueError("icon is not a readable GIF") from error


def now_playing_payload(
    text: str,
    bucket: int | None,
    icon_gif: bytes,
    paused: bool = False,
    scroll_revision: int = 0,
    progress_phase: int = PROGRESS_SWEEP_FINAL_PHASE,
) -> dict[str, Any]:
    """Build the small native-layout payload shown by AWTRIX."""
    validate_record_icon_gif(icon_gif, paused=paused)
    payload: dict[str, Any] = {
        "text": limit_display_text(normalize_media_text(text) or "NOW PLAYING"),
        "textCase": "asTyped",
        "font": "small",
        "textColor": TEXT_COLOR_HEX,
        "textCenter": True,
        "icon": base64.b64encode(icon_gif).decode("ascii"),
        "iconMode": "fixed",
        "iconOffsetX": 0,
        "scroll": {
            "mode": "loop",
            "direction": "left",
            "entry": "inline",
            "whenFits": "static",
            "speed": SCROLL_SPEED,
            "gap": SCROLL_GAP,
            # Toggle one invisible millisecond per media identity. AWTRIX uses
            # resolved metrics rather than text content to decide whether to
            # restart its scroller; this guarantees equal-width new titles
            # begin at their first character instead of inheriting an offset.
            "holdMs": SCROLL_HOLD_MS + (int(scroll_revision) & 1),
        },
        "backgroundColor": "#000000",
        "repeat": 1,
        "lifetimeMs": LIFETIME_MS,
        "lifetimeExpiry": "remove",
    }
    rail = progress_rail_bitmap(bucket, progress_phase)
    if rail is not None:
        payload["draw"] = [["bitmap", PROGRESS_X, HEIGHT - 1, PROGRESS_WIDTH, 1, rail]]
    return payload


def _hex_rgb(color: str) -> RGB:
    color = color.lstrip("#")
    return (int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16))


def _set_preview_pixel(pixels: list[RGB], x: int, y: int, color: RGB) -> None:
    if 0 <= x < WIDTH and 0 <= y < HEIGHT:
        pixels[y * WIDTH + x] = color


def _draw_preview_text(pixels: list[RGB], text: str, start_x: int) -> None:
    cursor = start_x
    for char in text:
        glyph = FONT.get(char, FONT["?"])
        for row, pattern in enumerate(glyph):
            for column, value in enumerate(pattern):
                x = cursor + column
                if value == "1" and TEXT_X <= x < WIDTH:
                    _set_preview_pixel(pixels, x, 1 + row, TEXT_COLOR)
        cursor += len(glyph[0]) + 1


def render_frame(
    text: str,
    progress_ratio: float | None,
    elapsed_ms: int,
    paused: bool = False,
    progress_phase: int = PROGRESS_SWEEP_FINAL_PHASE,
) -> bytes:
    """Render a faithful 32x8 review mock of the native layout."""
    elapsed_ms = max(0, int(elapsed_ms))
    icon_index = 0 if paused else (elapsed_ms // RECORD_FRAME_MS) % RECORD_FRAME_COUNT
    icon = render_record_icon_frame(icon_index, paused=paused)
    pixels: list[RGB] = [BLACK] * (WIDTH * HEIGHT)
    icon_pixels = list(zip(icon[0::3], icon[1::3], icon[2::3]))
    for y in range(HEIGHT):
        for x in range(ICON_WIDTH):
            _set_preview_pixel(pixels, x, y, icon_pixels[y * ICON_WIDTH + x])

    title = limit_display_text(normalize_media_text(text) or "NOW PLAYING")
    width = measure_text(title)
    if width <= TEXT_WIDTH:
        title_x = TEXT_X + (TEXT_WIDTH - width) // 2
        _draw_preview_text(pixels, title, title_x)
    else:
        motion_ms = max(0, elapsed_ms - SCROLL_HOLD_MS)
        pixels_per_second = 21 * SCROLL_SPEED / 100
        period = width + SCROLL_GAP
        distance = (motion_ms * pixels_per_second / 1000) % period
        title_x = math.floor(TEXT_X - distance)
        _draw_preview_text(pixels, title, title_x)
        _draw_preview_text(pixels, title, title_x + period)

    bucket = progress_bucket(progress_ratio)
    rail = progress_rail_pixels(bucket, progress_phase)
    if rail is not None:
        for index, color in enumerate(rail):
            _set_preview_pixel(pixels, PROGRESS_X + index, HEIGHT - 1, color)
    return bytes(channel for pixel in pixels for channel in pixel)


def _seconds(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if hasattr(value, "total_seconds"):
            result = float(value.total_seconds())
        else:
            result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _updated_datetime(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def progress_from_timeline(
    timeline: Any,
    playback_info: Any,
    now: datetime | None = None,
) -> float | None:
    if timeline is None:
        return None
    start = _seconds(getattr(timeline, "start_time", None))
    end = _seconds(getattr(timeline, "end_time", None))
    position = _seconds(getattr(timeline, "position", None))
    if start is None or end is None or position is None or end <= start:
        return None

    updated = _updated_datetime(getattr(timeline, "last_updated_time", None))
    if updated is not None:
        now = now or datetime.now(timezone.utc)
        elapsed = max(0.0, (now.astimezone(timezone.utc) - updated).total_seconds())
        rate = getattr(playback_info, "playback_rate", None)
        try:
            rate = 1.0 if rate is None else float(rate)
        except (TypeError, ValueError):
            rate = 1.0
        if not math.isfinite(rate):
            rate = 1.0
        position += elapsed * max(0.0, rate)

    return max(0.0, min(1.0, (position - start) / (end - start)))


def _is_playing(session: Any) -> bool:
    try:
        info = session.get_playback_info()
        status = info.playback_status
    except (AttributeError, OSError, RuntimeError):
        return False
    if PlaybackStatus is not None:
        return status == PlaybackStatus.PLAYING
    return str(status).upper().endswith("PLAYING") or status == 4


def _session_recency(session: Any) -> float:
    try:
        updated = _updated_datetime(session.get_timeline_properties().last_updated_time)
        return updated.timestamp() if updated is not None else 0.0
    except (AttributeError, OSError, RuntimeError, OverflowError):
        return 0.0


def select_playing_session(manager: Any) -> Any | None:
    """Prefer Windows' current session, otherwise the newest playing one."""
    try:
        current = manager.get_current_session()
    except (AttributeError, OSError, RuntimeError):
        current = None
    if current is not None and _is_playing(current):
        return current

    try:
        sessions = list(manager.get_sessions())
    except (AttributeError, OSError, RuntimeError):
        return None
    playing = [session for session in sessions if _is_playing(session)]
    return max(playing, key=_session_recency, default=None)


async def _media_properties(session: Any) -> Any:
    return await session.try_get_media_properties_async()


async def read_snapshot(manager: Any) -> MediaSnapshot | None:
    session = select_playing_session(manager)
    if session is None:
        return None
    properties = await asyncio.wait_for(_media_properties(session), timeout=3.0)
    if not _is_playing(session):
        return None

    title = str(getattr(properties, "title", "") or "")
    artist = str(
        getattr(properties, "artist", "")
        or getattr(properties, "album_artist", "")
        or ""
    )
    if not title:
        title = str(getattr(properties, "subtitle", "") or "")
    source = str(getattr(session, "source_app_user_model_id", "") or "")
    if not title and not artist:
        return None

    playback_info = session.get_playback_info()
    timeline = session.get_timeline_properties()
    return MediaSnapshot(
        title=title,
        artist=artist,
        source=source,
        progress_ratio=progress_from_timeline(timeline, playback_info),
    )


async def request_media_manager() -> Any:
    if MediaManager is None:
        raise RuntimeError(f"Missing Windows media dependency. Run: {WINRT_INSTALL_COMMAND}")
    return await MediaManager.request_async()


def _http_error(action: str, response: Any) -> str:
    detail = str(getattr(response, "text", ""))[:200]
    suffix = f": {detail}" if detail else ""
    return f"{action} HTTP {response.status_code}{suffix}"


def delete_app() -> str | None:
    try:
        with panel_gate.mutation(GATE_SOURCE, "delete", APP_NAME) as gate:
            response = requests.delete(DELETE_URL, timeout=REQUEST_TIMEOUT_SECONDS)
            gate.observe(response)
    except requests.RequestException as error:
        return f"cleanup unreachable: {type(error).__name__}"
    if 200 <= response.status_code < 300 or response.status_code == 404:
        return None
    return _http_error("cleanup", response)


def push_payload(payload: dict[str, Any]) -> str | None:
    try:
        with panel_gate.mutation(GATE_SOURCE, "push", APP_NAME, body=payload) as gate:
            response = requests.put(
                PUSH_URL,
                json=payload,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            gate.observe(response)
    except requests.RequestException as error:
        return f"push unreachable: {type(error).__name__}"
    if 200 <= response.status_code < 300:
        return None
    return _http_error("push", response)


def activate_app() -> str | None:
    """Show a newly started track once without changing the saved app order."""
    try:
        body = {"name": APP_NAME, "fast": True}
        with panel_gate.mutation(GATE_SOURCE, "activate", APP_NAME, body=body) as gate:
            response = requests.put(
                ACTIVE_URL,
                json=body,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            gate.observe(response)
    except requests.RequestException as error:
        return f"activation unreachable: {type(error).__name__}"
    if 200 <= response.status_code < 300:
        return None
    return _http_error("activation", response)


def should_log_failure(count: int) -> bool:
    return count in (1, 5) or count % 60 == 0


def network_retry_delay(failure_count: int) -> float:
    """Back off quickly enough that a bad payload cannot hammer a rebooting panel."""
    failure_count = max(1, int(failure_count))
    return min(NETWORK_RETRY_MAX_SECONDS, float(2 ** min(failure_count - 1, 5)))


def read_panel_uptime() -> int | None:
    """Read a cheap boot marker without touching display/framebuffer state."""
    try:
        response = requests.get(DEVICE_URL, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException:
        return None
    if not 200 <= response.status_code < 300:
        return None
    try:
        uptime = int(response.json()["uptimeSeconds"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return uptime if uptime >= 0 else None


PLANNED_RESET_REASON = "software"


def read_panel_reset_reason() -> str | None:
    """Why the panel last restarted, or None when it cannot be read."""
    try:
        response = requests.get(DEVICE_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        if not 200 <= response.status_code < 300:
            return None
        return str(response.json()["resetReason"])
    except (requests.RequestException, KeyError, TypeError, ValueError, AttributeError):
        return None


def reboot_was_planned(reset_reason: str | None) -> bool:
    """``software`` follows a requested restart (nightly maintenance, OTA).

    Unknown counts as unplanned: when in doubt, the static record is the safe
    side, exactly as before this check existed.
    """
    return reset_reason is not None and reset_reason.strip().lower() == PLANNED_RESET_REASON


def panel_rebooted_since(
    previous_uptime: int | None,
    previous_probe_at: float | None,
    current_uptime: int,
    current_probe_at: float,
) -> bool:
    """Detect a definite panel reboot without estimating uptime from PC time.

    HTTP response latency and small clock-rate differences make the PC's elapsed
    time an unsafe proxy for the ESP32 uptime. Uptime itself is monotonic, so a
    direct decrease is the reliable signal and avoids permanently latching the
    static safety icon after an otherwise healthy, merely slow response.
    """
    if previous_uptime is None or previous_probe_at is None:
        return False
    return current_uptime < previous_uptime


def scroll_revision_for(
    identity: tuple[str, str, str],
    last_pushed_identity: tuple[str, str, str] | None,
    last_pushed_revision: int,
) -> int:
    """Toggle only against the last title that actually reached the panel."""
    if identity != last_pushed_identity:
        return (int(last_pushed_revision) ^ 1) & 1
    return int(last_pushed_revision) & 1


def configure_logging() -> logging.Logger:
    logger = logging.getLogger("awtrix-now-playing")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    # Alongside the other senders' logs, not next to the source. This was the
    # only one writing into the repository, which put a 256KB rotating log
    # inside a OneDrive-synced folder for the cloud to copy on every write, and
    # left anyone looking for logs finding four in one place and one in
    # another. sender_logging owns that location; falling back to the old
    # behaviour keeps this working if it is ever imported standalone.
    try:
        import sender_logging

        folder = sender_logging.log_folder()
        folder.mkdir(parents=True, exist_ok=True)
        log_path = folder / "now_playing.log"
    except (ImportError, OSError):
        log_path = Path(__file__).with_suffix(".log")
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=256_000,
        backupCount=1,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    if sys.stdout is not None:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)
    return logger


def _stop_requested(stop_event: Any | None) -> bool:
    return bool(stop_event is not None and stop_event.is_set())


def state_url_for(lan_ip: str) -> str:
    """The URL the panel polls for media state."""
    return f"http://{lan_ip}:{STATE_SERVER_PORT}{STATE_PATH}"


def refresh_state_url(
    panel_ip: str,
    installed_url: str,
    template: str,
    logger: Any,
    *,
    resolve: Any = resolve_lan_ip,
    install: Any = None,
) -> str:
    """Reinstall the panel script if this PC's address has moved.

    The URL baked into the Berry script is this machine's LAN address,
    resolved once at startup. A DHCP renewal, an undock from Ethernet to
    Wi-Fi, or a VPN changing the route to the panel all change which local
    address is reachable, and from that moment the panel polls a URL that no
    longer resolves to anything. Now Playing goes blank for the rest of the
    process's life.

    Nothing noticed, because all the observability here is write-side: the
    media loop keeps updating the store successfully and logging track
    changes, and the store's own stale fail-safe watches the *producer*. A
    total outage of the feature was indistinguishable from perfect operation.

    Returns the URL now installed, unchanged when the address has not moved.
    """
    installer = install or ensure_script_installed
    try:
        current_url = state_url_for(resolve(panel_ip))
    except (OSError, RuntimeError) as error:
        logger.warning("Could not re-check this PC's LAN address: %s", type(error).__name__)
        return installed_url
    if current_url == installed_url:
        return installed_url

    logger.warning(
        "This PC's address changed; the panel was polling %s. Reinstalling for %s",
        installed_url,
        current_url,
    )
    try:
        installer(AWTRIX, APP_NAME, install_source(template, current_url))
    except Exception as error:  # noqa: BLE001
        # Keep the old URL recorded so the next check tries again rather than
        # believing it has already fixed this.
        logger.warning("Could not reinstall the panel script: %s", type(error).__name__)
        return installed_url
    return current_url


async def run_script_bridge(
    store: BridgeStateStore | None = None,
    stop_event: Any | None = None,
    poll_seconds: float = MEDIA_POLL_SECONDS,
) -> None:
    """Serve media state while AWTRIX renders the page locally in Berry.

    Supplying a store runs only the media-polling core, which keeps the state
    machine deterministic in tests.  Production owns the HTTP server and
    installs the rendered Berry source before polling starts.
    """

    logger = configure_logging()
    owns_store = store is None
    state_store = store or BridgeStateStore(stale_after=STATE_STALE_SECONDS)
    server: Any | None = None
    server_thread: threading.Thread | None = None
    # Kept for the address re-check in the polling loop below. The URL handed
    # to the panel is this PC's LAN address, resolved once; a DHCP renewal, an
    # undock, or a VPN changing the route silently orphans the panel until the
    # process is restarted, and nothing on either side notices.
    installed_url: str | None = None
    panel_ip: str | None = None
    template: str | None = None
    next_lan_check = 0.0

    if owns_store:
        panel_host = urlsplit(AWTRIX).hostname
        if not panel_host:
            raise RuntimeError(f"Invalid AWTRIX URL: {AWTRIX}")
        try:
            template = SCRIPT_TEMPLATE_PATH.read_text(encoding="utf-8")
        except OSError as error:
            raise RuntimeError(f"Could not read {SCRIPT_TEMPLATE_PATH.name}: {error}") from error

        setup_failures = 0
        while not _stop_requested(stop_event):
            try:
                panel_ip = socket.gethostbyname(panel_host)
                lan_ip = resolve_lan_ip(panel_ip)
                server = make_server("0.0.0.0", STATE_SERVER_PORT, state_store, panel_ip)
            except (OSError, RuntimeError) as error:
                setup_failures += 1
                if should_log_failure(setup_failures):
                    logger.warning(
                        "State bridge waiting for the LAN: %s (%d attempts)",
                        error,
                        setup_failures,
                    )
                await asyncio.sleep(network_retry_delay(setup_failures))
                continue
            break
        if server is None:
            return
        server_thread = threading.Thread(
            target=server.serve_forever,
            name="awtrix-now-playing-state",
            daemon=True,
        )
        server_thread.start()
        state_url = state_url_for(lan_ip)
        installed_url = state_url
        # One full interval from now, not immediately: the address was just
        # resolved to build this URL, so re-checking on the first pass through
        # the loop is pure duplicate work.
        next_lan_check = time.monotonic() + LAN_RECHECK_SECONDS
        source = install_source(template, state_url)
        install_failures = 0
        removed_legacy = False
        changed = False
        while not _stop_requested(stop_event):
            try:
                if not removed_legacy:
                    removed_legacy = await asyncio.to_thread(
                        remove_legacy_pushed_app,
                        AWTRIX,
                        APP_NAME,
                    )
                    if removed_legacy:
                        logger.info("Removed the legacy pushed renderer; preserved its app-loop slot")
                changed = await asyncio.to_thread(
                    ensure_script_installed,
                    AWTRIX,
                    APP_NAME,
                    source,
                )
            except RuntimeError as error:
                install_failures += 1
                if should_log_failure(install_failures):
                    logger.warning(
                        "Device renderer setup unavailable: %s (%d attempts)",
                        error,
                        install_failures,
                    )
                await asyncio.sleep(network_retry_delay(install_failures))
                continue
            break
        if _stop_requested(stop_event):
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2.0)
            return
        logger.info(
            "Device renderer %s; state bridge listening at %s",
            "installed" if changed else "already current",
            state_url,
        )

    logger.info("Starting Windows media state bridge")
    manager: Any | None = None
    idle_since: float | None = None
    media_error_since: float | None = None
    next_manager_retry = 0.0
    last_semantic: tuple[bool, str, str, int] = (False, "", "", -1)
    last_identity = ""
    revision = state_store.snapshot().revision

    try:
        while not _stop_requested(stop_event):
            now = time.monotonic()
            snapshot: MediaSnapshot | None = None
            poll_failed = False

            # Confirm the panel is still being told an address that reaches us.
            if owns_store and installed_url and template and now >= next_lan_check:
                next_lan_check = now + LAN_RECHECK_SECONDS
                installed_url = await asyncio.to_thread(
                    refresh_state_url, panel_ip, installed_url, template, logger
                )

            if manager is None and now < next_manager_retry:
                poll_failed = True
            else:
                try:
                    if manager is None:
                        manager = await asyncio.wait_for(request_media_manager(), timeout=5.0)
                    snapshot = await read_snapshot(manager)
                    media_error_since = None
                except (OSError, RuntimeError, asyncio.TimeoutError) as error:
                    manager = None
                    next_manager_retry = now + 2.0
                    poll_failed = True
                    if media_error_since is None:
                        media_error_since = now
                        logger.warning(
                            "Windows media session unavailable: %s",
                            type(error).__name__,
                        )

            if snapshot is not None:
                provisional = bridge_state_from_snapshot(snapshot)
                semantic = (
                    provisional.playing,
                    provisional.identity,
                    provisional.text,
                    provisional.progress,
                )
                if semantic != last_semantic:
                    revision += 1
                    last_semantic = semantic
                active = bridge_state_from_snapshot(snapshot, revision=revision)
                state_store.update(active)
                idle_since = None
                if active.identity != last_identity:
                    logger.info(
                        "New playing media detected from %s",
                        snapshot.source or "unknown app",
                    )
                    last_identity = active.identity
            else:
                grace_elapsed = False
                if poll_failed:
                    if media_error_since is not None:
                        grace_elapsed = now - media_error_since >= MEDIA_ERROR_GRACE_SECONDS
                else:
                    if idle_since is None:
                        idle_since = now
                    grace_elapsed = now - idle_since >= PAUSE_GRACE_SECONDS

                if grace_elapsed and last_semantic[0]:
                    revision += 1
                    last_semantic = (False, "", "", -1)
                    last_identity = ""
                    state_store.update(BridgeState(revision=revision))
                    logger.info("Playback stopped; device renderer will leave rotation")

            await asyncio.sleep(max(0.01, float(poll_seconds)))
    finally:
        state_store.clear()
        if server is not None:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
        if server_thread is not None:
            server_thread.join(timeout=2.0)
        logger.info("Windows media state bridge stopped")


async def run() -> None:
    logger = configure_logging()
    logger.info("Starting Now Playing sender for %s", PUSH_URL)

    try:
        playing_icon = build_record_icon_gif(paused=False)
        static_icon = _encode_record_gif([render_record_icon_frame(0)], 1_000)
    except (RuntimeError, ValueError) as error:
        logger.error("Could not build the record icon: %s", error)
        return
    logger.info(
        "Record icon ready: %d-byte animated GIF, %d-byte safety fallback",
        len(playing_icon),
        len(static_icon),
    )
    last_panel_uptime = await asyncio.to_thread(read_panel_uptime)
    last_panel_probe_at = time.monotonic() if last_panel_uptime is not None else None

    manager: Any | None = None
    current: MediaSnapshot | None = None
    activation = TrackActivation()
    progress_sweep = ProgressSweep()
    idle_since: float | None = None
    media_error_since: float | None = None
    next_media_poll = 0.0
    next_manager_retry = 0.0
    next_network_try = 0.0
    next_activation_try = 0.0
    last_push_at = 0.0
    last_pushed_key: PayloadKey | None = None
    last_pushed_identity: tuple[str, str, str] | None = None
    last_pushed_scroll_revision = 0
    remote_visible: bool | None = None
    failures = 0
    activation_failures = 0
    safe_mode = False

    while True:
        now = time.monotonic()

        if now >= next_media_poll:
            next_media_poll = now + MEDIA_POLL_SECONDS
            # A failed manager request gets a real retry window. Merely waiting
            # inside that window must not slide the deadline forward forever.
            if manager is None and now < next_manager_retry:
                if (
                    media_error_since is not None
                    and now - media_error_since >= MEDIA_ERROR_GRACE_SECONDS
                    and idle_since is None
                ):
                    idle_since = now
            else:
                try:
                    if manager is None:
                        manager = await asyncio.wait_for(request_media_manager(), timeout=5.0)
                    snapshot = await read_snapshot(manager)
                    media_error_since = None
                except (OSError, RuntimeError, asyncio.TimeoutError) as error:
                    manager = None
                    next_manager_retry = now + 2.0
                    if media_error_since is None:
                        media_error_since = now
                        logger.warning(
                            "Windows media session unavailable: %s",
                            type(error).__name__,
                        )
                    if now - media_error_since >= MEDIA_ERROR_GRACE_SECONDS and idle_since is None:
                        idle_since = now
                else:
                    if snapshot is None:
                        if idle_since is None:
                            idle_since = now
                    else:
                        if activation.observe(snapshot):
                            progress_sweep.clear()
                            next_activation_try = now
                            activation_failures = 0
                            logger.info(
                                "New playing media detected from %s",
                                snapshot.source or "unknown app",
                            )
                        current = snapshot
                        idle_since = None

        if idle_since is not None and now - idle_since >= PAUSE_GRACE_SECONDS:
            if current is not None:
                current = None
                activation.clear()
                progress_sweep.clear()

        if current is None:
            if remote_visible is not False and now >= next_network_try:
                error = await asyncio.to_thread(delete_app)
                if error is None:
                    if remote_visible is True:
                        logger.info("Playback stopped; removed Now Playing from the panel")
                    remote_visible = False
                    failures = 0
                    last_pushed_key = None
                    last_pushed_identity = None
                    last_pushed_scroll_revision = 0
                    activation.clear()
                    progress_sweep.clear()
                else:
                    failures += 1
                    if failures >= SAFE_MODE_FAILURES and not safe_mode:
                        safe_mode = True
                        logger.error(
                            "Repeated panel failures; using a static record until this sender restarts"
                        )
                    next_network_try = now + network_retry_delay(failures)
                    if should_log_failure(failures):
                        logger.warning("%s (%d in a row)", error, failures)
        else:
            # Establish a known-clean base app once per process before publishing.
            if remote_visible is None and now >= next_network_try:
                error = await asyncio.to_thread(delete_app)
                if error is None:
                    remote_visible = False
                    failures = 0
                else:
                    failures += 1
                    if failures >= SAFE_MODE_FAILURES and not safe_mode:
                        safe_mode = True
                        logger.error(
                            "Repeated panel failures; using a static record until this sender restarts"
                        )
                    next_network_try = now + network_retry_delay(failures)
                    if should_log_failure(failures):
                        logger.warning("%s (%d in a row)", error, failures)

            paused = idle_since is not None
            # A single missed Windows media poll should not reload the icon.
            # Keep the last truthful playing page during the short grace, then
            # remove it once the pause/stop is confirmed.
            if paused:
                progress_sweep.clear()
                await asyncio.sleep(0.05)
                continue

            desired_key = (
                display_text(current),
                progress_bucket(current.progress_ratio),
                False,
            )
            desired_scroll_revision = scroll_revision_for(
                current.identity,
                last_pushed_identity,
                last_pushed_scroll_revision,
            )
            push_due = payload_push_due(
                desired_key,
                last_pushed_key,
                current.identity,
                last_pushed_identity,
                now - last_push_at,
            )

            if remote_visible is not None and now >= next_network_try and push_due:
                if not safe_mode:
                    probed_uptime = await asyncio.to_thread(read_panel_uptime)
                    probed_at = time.monotonic()
                    if probed_uptime is not None:
                        if panel_rebooted_since(
                            last_panel_uptime,
                            last_panel_probe_at,
                            probed_uptime,
                            probed_at,
                        ):
                            reason = await asyncio.to_thread(read_panel_reset_reason)
                            if reboot_was_planned(reason):
                                logger.info("Panel restarted on request (%s); keeping animation", reason)
                            else:
                                safe_mode = True
                                logger.error(
                                    "Panel reboot detected after animated use (%s); latching the static record",
                                    reason,
                                )
                        last_panel_uptime = probed_uptime
                        last_panel_probe_at = probed_at
                start_progress_sweep = (
                    not safe_mode
                    and desired_key[1] is not None
                    and desired_key[1] > 0
                    and (
                        last_pushed_key is None
                        or current.identity != last_pushed_identity
                        or desired_key[1] != last_pushed_key[1]
                    )
                )
                try:
                    payload = now_playing_payload(
                        desired_key[0],
                        desired_key[1],
                        static_icon if safe_mode else playing_icon,
                        paused=False,
                        scroll_revision=desired_scroll_revision,
                        progress_phase=(
                            0 if start_progress_sweep else PROGRESS_SWEEP_FINAL_PHASE
                        ),
                    )
                except (RuntimeError, ValueError) as error:
                    failures += 1
                    if failures >= SAFE_MODE_FAILURES and not safe_mode:
                        safe_mode = True
                        logger.error(
                            "Animated icon validation failed repeatedly; using the static record"
                        )
                    next_network_try = now + network_retry_delay(failures)
                    if should_log_failure(failures):
                        logger.warning("payload build failed: %s", error)
                    payload = None

                error = None if payload is None else await asyncio.to_thread(push_payload, payload)
                if error is None:
                    if payload is not None:
                        remote_visible = True
                        last_pushed_key = desired_key
                        last_pushed_identity = current.identity
                        last_pushed_scroll_revision = desired_scroll_revision
                        last_push_at = now
                        failures = 0
                        next_network_try = now + 0.25
                        if start_progress_sweep:
                            progress_sweep.start(
                                desired_key,
                                current.identity,
                                desired_scroll_revision,
                                now,
                            )
                        else:
                            progress_sweep.clear()
                else:
                    failures += 1
                    if failures >= SAFE_MODE_FAILURES and not safe_mode:
                        safe_mode = True
                        logger.error(
                            "Repeated panel failures; using a static record until this sender restarts"
                        )
                    next_network_try = now + network_retry_delay(failures)
                    if should_log_failure(failures):
                        logger.warning("%s (%d in a row)", error, failures)

            # A true progress change gets two lightweight follow-up frames:
            # the cream cap travels right through the already-filled cells and
            # settles on the exact endpoint. These phase-only PUTs preserve the
            # record GIF and title scroll because every other field is stable.
            if (
                not safe_mode
                and remote_visible is True
                and now >= next_network_try
                and progress_sweep.due(desired_key, current.identity, now)
            ):
                phase = progress_sweep.next_phase
                assert phase is not None
                try:
                    payload = now_playing_payload(
                        desired_key[0],
                        desired_key[1],
                        playing_icon,
                        paused=False,
                        scroll_revision=progress_sweep.scroll_revision,
                        progress_phase=phase,
                    )
                except (RuntimeError, ValueError) as error:
                    failures += 1
                    next_network_try = now + network_retry_delay(failures)
                    progress_sweep.settle_after_failure(now)
                    if failures >= SAFE_MODE_FAILURES:
                        safe_mode = True
                        progress_sweep.clear()
                        last_pushed_key = None
                        logger.error(
                            "Progress-sweep validation failed repeatedly; latching the static record and rail"
                        )
                    if should_log_failure(failures):
                        logger.warning("progress sweep build failed: %s", error)
                    payload = None

                error = None if payload is None else await asyncio.to_thread(push_payload, payload)
                if error is None:
                    if payload is not None:
                        last_push_at = now
                        failures = 0
                        next_network_try = now + PROGRESS_SWEEP_FRAME_SECONDS
                        progress_sweep.advance(now)
                else:
                    failures += 1
                    progress_sweep.settle_after_failure(now)
                    next_network_try = now + network_retry_delay(failures)
                    if failures >= SAFE_MODE_FAILURES:
                        safe_mode = True
                        progress_sweep.clear()
                        last_pushed_key = None
                        logger.error(
                            "Repeated progress-sweep failures; latching the static record and rail"
                        )
                    if should_log_failure(failures):
                        logger.warning("%s (%d in a row)", error, failures)

            # Activate only after this exact media identity has reached the panel.
            if (
                activation.pending
                and remote_visible is True
                and last_pushed_identity == current.identity
                and now >= next_activation_try
            ):
                error = await asyncio.to_thread(activate_app)
                if error is None:
                    activation.complete()
                    activation_failures = 0
                    logger.info("New track shown immediately; normal app rotation will resume")
                else:
                    activation_failures += 1
                    if activation_failures >= MAX_ACTIVATION_ATTEMPTS:
                        activation.complete()
                        logger.warning(
                            "%s; giving up immediate activation for this track; normal rotation remains",
                            error,
                        )
                    else:
                        next_activation_try = now + 2.0
                    if (
                        activation_failures < MAX_ACTIVATION_ATTEMPTS
                        and should_log_failure(activation_failures)
                    ):
                        logger.warning(
                            "%s (%d activation attempts)",
                            error,
                            activation_failures,
                        )

        await asyncio.sleep(0.05)


def main() -> None:
    if MediaManager is None or (not USE_DEVICE_SCRIPT_RENDERER and Image is None):
        message = f"Missing Windows media dependency. Run: {WINRT_INSTALL_COMMAND}"
        configure_logging().error(message)
        raise SystemExit(message)
    try:
        asyncio.run(run_script_bridge() if USE_DEVICE_SCRIPT_RENDERER else run())
    except KeyboardInterrupt:
        logger = configure_logging()
        if USE_DEVICE_SCRIPT_RENDERER:
            logger.info("Stopped; the device renderer will hide when state goes stale")
        else:
            logger.info("Stopped; the panel app will expire within 45 seconds")


if __name__ == "__main__":
    main()
