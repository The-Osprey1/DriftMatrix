"""One durable desk countdown, with static 32x8 artwork and an opt-in hub service.

The desktop writes a local schedule; only ReminderController contacts the panel.
Deadlines use wall time, continue through PC sleep, and expire ten minutes after
they are due. Nothing wakes the display or changes its brightness.
"""
from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
import json
import logging
import math
import os
from pathlib import Path
import tempfile
import time
import uuid

from focus_control import default_data_dir
from panel_client import MutationPriority
import visual_system as visual

APP_NAME = "deskreminder"
STATE_NAME = "desk_reminder.json"
REFRESH_SECONDS = 5
LIFETIME_MS = 30_000
DUE_WINDOW_SECONDS = 600


@dataclass(frozen=True)
class Reminder:
    id: str
    label: str
    created_at: float
    due_at: float

    def phase(self, now: float) -> str:
        if now < self.created_at:
            return "clock_changed"
        if now >= self.due_at + DUE_WINDOW_SECONDS:
            return "expired"
        return "due" if now >= self.due_at else "countdown"


def validate_label(label: str) -> str:
    if not isinstance(label, str):
        raise ValueError("Use a short label containing letters, numbers or spaces.")
    label = label.strip().upper()
    if not label or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -" for c in label):
        raise ValueError("Use a short label containing letters, numbers, spaces or a dash.")
    if visual.measure_text(label) > visual.LANE_WIDTH:
        raise ValueError("Label is too wide for the panel. Try TEA, WATER or BREAK.")
    return label


def atomic_json(path: Path, value: object) -> None:
    body = json.dumps(value, allow_nan=False, separators=(",", ":"))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".reminder-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def schedule(path: Path, label: str, minutes: int, *, now: float | None = None) -> Reminder:
    label = validate_label(label)
    if type(minutes) is not int or not 1 <= minutes <= 1440:
        raise ValueError("Choose a whole number of minutes from 1 to 1440.")
    moment = time.time() if now is None else now
    if not math.isfinite(moment) or moment < 0:
        raise ValueError("The system clock is unavailable.")
    reminder = Reminder(str(uuid.uuid4()), label, moment, moment + minutes * 60)
    atomic_json(path, {"schema_version": 1, **asdict(reminder)})
    return reminder


def read_reminder(path: Path) -> Reminder | None:
    try:
        with path.open("rb") as handle:
            body = handle.read(4097)
    except FileNotFoundError:
        return None
    if len(body) > 4096:
        raise ValueError("Reminder file is too large.")
    try:
        value = json.loads(body)
        if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
            raise ValueError()
        identifier = value["id"]
        if str(uuid.UUID(identifier)) != identifier:
            raise ValueError()
        label = validate_label(value["label"])
        created, due = value["created_at"], value["due_at"]
        if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in (created, due)):
            raise ValueError()
        if not 60 <= due - created <= 86400:
            raise ValueError()
        return Reminder(identifier, label, created, due)
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError) as error:
        raise ValueError("Saved reminder is invalid. Replace it or dismiss it.") from error


def render(reminder: Reminder, now: float, *, show_label: bool = False):
    frame = visual.new_frame()
    phase = reminder.phase(now)
    if phase not in {"countdown", "due"}:
        return frame
    remaining = max(0, math.ceil(reminder.due_at - now))
    # A narrower glass leaves breathing room around the icon. Four sand
    # positions move from the upper bulb into the lower bulb as time passes.
    # This is actual elapsed progress, not a decorative animation.
    icon = ("...G...", "..GGG..", ".G...G.", ".G...G.", ".G...G.", "GGGGGGG", "...G...") if phase == "due" else (
        ".PPPPP.", ".I...I.", "..I.I..", "...G...", "..I.I..", ".I...I.", ".PPPPP.")
    colors = {"P": visual.PEARL, "I": visual.ICE, "G": visual.GOLD}
    for y, row in enumerate(icon):
        for x, bit in enumerate(row):
            if bit in colors:
                visual.put_pixel(frame, x, y, colors[bit])
    if phase == "countdown":
        ratio = min(1.0, remaining / (reminder.due_at - reminder.created_at))
        upper = math.ceil(4 * ratio)
        for x, y in ((3, 2), (3, 1), (2, 1), (4, 1))[:upper]:
            visual.put_pixel(frame, x, y, visual.GOLD)
        for x, y in ((3, 5), (2, 5), (4, 5), (3, 4))[:4 - upper]:
            visual.put_pixel(frame, x, y, visual.GOLD)
    else:
        visual.put_pixel(frame, 2, 1, visual.PEARL)
    timer = visual.format_remaining_time(remaining, show_seconds=False)
    # Calm glance: the time left in white with the reminder's name as a grey
    # label when both fit (TEA 5M); otherwise the page alternates between
    # them every ten seconds.  Once due, the name alone.  The meter drains
    # with the time left.
    if phase == "due":
        lane = visual.render_glance_lane(reminder.label)
    else:
        total = reminder.due_at - reminder.created_at
        fits = visual.glance_text_width(timer, reminder.label) <= visual.TEXT_BUDGET
        value, label = (timer, reminder.label) if fits else (
            (reminder.label, None) if show_label else (timer, None))
        lane = visual.render_glance_lane(value, label=label, meter=remaining / total, level="info")
    for y in range(8):
        frame[y][8] = visual.BLACK
        frame[y][9:] = lane[y][9:]
    return frame


def build_payload(reminder: Reminder, now: float) -> dict:
    frame = render(reminder, now, show_label=int((now - reminder.created_at) // 10) % 2 == 1)
    bitmap = bytes(channel for row in frame for pixel in row for channel in pixel)
    return {
        "draw": [["bitmap", 0, 0, 32, 8, base64.b64encode(bitmap).decode("ascii")]],
        "backgroundColor": "#000000", "durationMs": 8000,
        "lifetimeMs": LIFETIME_MS, "lifetimeExpiry": "remove",
    }


class ReminderController:
    def __init__(self, panel, *, path: Path | None = None, clock=None, monotonic=None):
        self.panel = panel
        self.path = path if path is not None else default_data_dir() / STATE_NAME
        self.clock = clock or time.time
        self.monotonic = monotonic or time.monotonic
        self.present = False
        self.last_push = float("-inf")
        self.last_key = None
        self.announced = None
        self.failures = 0
        self.retry_at = 0.0
        self.delivery_status = None

    def tick(self):
        if self.monotonic() < self.retry_at:
            return "retry_wait"
        try:
            result = self._sync()
        except (OSError, RuntimeError) as error:
            self.failures += 1
            self.retry_at = self.monotonic() + min(60, 5 * 2 ** min(self.failures - 1, 4))
            if self.failures in (1, 3, 5) or self.failures % 12 == 0:
                logging.getLogger("awtrix.utility_hub.reminder").warning(
                    "Reminder panel request failed (%s); retrying with backoff", type(error).__name__)
            return "panel_unavailable"
        self.failures = 0
        self.retry_at = 0.0
        return result

    def _sync(self):
        now = self.clock()
        try:
            reminder = read_reminder(self.path)
        except (ValueError, OSError):
            reminder = None  # Bad state must not leave stale content alive.
        phase = reminder.phase(now) if reminder else "absent"
        if phase not in {"countdown", "due"}:
            if self.present:
                self.panel.delete_pushed_app(APP_NAME)
                self.present = False
            self.last_key = None
            self.delivery_status = None
            return phase
        key = (reminder.id, phase)
        if key != self.last_key or not 0 <= now - self.last_push < REFRESH_SECONDS:
            payload = build_payload(reminder, now)
            self.panel.push_app(APP_NAME, payload, static_payload=payload)
            self.present, self.last_push, self.last_key = True, now, key
            self.delivery_status = {"id": reminder.id, "phase": phase, "sent_at": self.clock()}
            if phase == "due" and self.announced != reminder.id:
                # Attempt once, including when quiet hours/cooldown suppress it.
                # The reminder still joins the ordinary rotation until expiry.
                self.announced = reminder.id
                self.panel.activate_app(APP_NAME, priority=MutationPriority.FOCUS)
        return phase
