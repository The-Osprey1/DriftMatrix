"""Notification Mirror for AWTRIX -- something is waiting for you on the PC.

Alert-only.  The page exists while an allowlisted app (Outlook, Teams,
Discord, ...) has a notification sitting in the Windows action centre that is
less than ``max_age_minutes`` old.  Dismiss it in Windows and it leaves the
panel; ignore it and it ages out.  A new one brings the page forward once.

    icon   what kind of thing: an envelope, a chat bubble, or a bell
    lane   the app, as a short word that fits: MAIL, TEAMS, CHAT
    meter  how many are waiting, on a scale of five

Private by construction.  Only the app's name and the notification's id and
creation time are read -- never the title, the text or the sender, which stay
on the PC.  The source test enforces that.  Apps that are not on the allowlist
are invisible, and browsers are left off because a browser notification says
nothing about which site sent it without reading its text.

Windows must allow notification access for apps (Settings > Privacy &
security > Notifications).  The listener is polled; its change event needs a
packaged app, which a Python sender is not.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import time
from typing import Any, Callable, Iterable, Sequence

from panel_scene import PresenceScene, encode_icon_gif, lane_payload
from visual_system import AMBER, GOLD, MINT, PEARL, RGB, Frame, draw_comet, new_frame, put_pixel, render_glance_lane

APP_NAME = "notify0"
METER_FULL_AT = 5


@dataclass(frozen=True)
class AppRule:
    match: str  # case-insensitive substring of the app's display name
    label: str  # what the lane says; must fit the lane on its own
    icon: str  # "mail", "chat" or "bell"


DEFAULT_RULES: tuple[AppRule, ...] = (
    AppRule("outlook", "MAIL", "mail"),
    AppRule("mail", "MAIL", "mail"),
    AppRule("teams", "TEAMS", "chat"),
    AppRule("discord", "CHAT", "chat"),
    AppRule("slack", "SLACK", "chat"),
    AppRule("whatsapp", "CHAT", "chat"),
    AppRule("messenger", "CHAT", "chat"),
    AppRule("phone link", "TEXT", "chat"),
)


@dataclass(frozen=True)
class Toast:
    toast_id: int
    app: str
    created: datetime


def match_rule(app: str, rules: Sequence[AppRule]) -> AppRule | None:
    name = app.casefold()
    for rule in rules:
        if rule.match.casefold() in name:
            return rule
    return None


def read_toasts() -> list[Toast]:
    """Ids, app names and times of the toasts in the action centre -- nothing else."""
    from winrt.windows.ui.notifications import NotificationKinds
    from winrt.windows.ui.notifications.management import (
        UserNotificationListener,
        UserNotificationListenerAccessStatus,
    )

    async def fetch() -> list[Toast]:
        listener = UserNotificationListener.current
        if listener.get_access_status() != UserNotificationListenerAccessStatus.ALLOWED:
            raise PermissionError("Windows has not allowed notification access for apps")
        found = []
        for item in await listener.get_notifications_async(NotificationKinds.TOAST):
            info = item.app_info
            app = info.display_info.display_name if info is not None else ""
            found.append(Toast(int(item.id), str(app), item.creation_time))
        return found

    return asyncio.run(fetch())


# ---------------------------------------------------------------- Icons

# Shaded icons (2026-10-03, to the Now Playing standard).  The silhouette
# survives brightness 1; the shadow tones may fade, as Now Playing's rim does.
ENVELOPE = (
    "........",
    "TTTTTTTT",
    "BF....FB",
    "B.F..F.B",
    "B..FF..B",
    "B......B",
    "SSSSSSSS",
    "........",
)
ENVELOPE_TONES: dict[str, RGB] = {"T": (255, 232, 140), "B": GOLD, "F": (255, 240, 190), "S": (170, 130, 40)}
# A comet traces the envelope's edge: something has just arrived.
ENVELOPE_EDGE = (
    tuple((x, 1) for x in range(8)) + tuple((7, y) for y in range(2, 6))
    + tuple((x, 6) for x in range(7, -1, -1)) + tuple((0, y) for y in range(5, 1, -1))
)

BUBBLE = (
    ".TTTTTT.",
    "TBBBBBBT",
    "BB.B.B.B",
    "BBBBBBBB",
    ".SSSSSS.",
    "..SS....",
    ".S......",
    "........",
)
BUBBLE_TONES: dict[str, RGB] = {"T": (150, 255, 200), "B": MINT, "S": (40, 150, 95)}
TYPING_DOTS = ((2, 2), (4, 2), (6, 2))
DOT_UNLIT: RGB = (24, 60, 40)

BELL = (
    "...G....",
    "..GHG...",
    ".GHGGG..",
    ".GHGGG..",
    ".GGGGG..",
    "GGGGGGG.",
    "........",
    "...C....",
)
BELL_TONES: dict[str, RGB] = {"G": GOLD, "H": (255, 240, 180), "C": AMBER}
BELL_SWAY = (0, 1, 0, -1)


def _draw(frame: Frame, art: tuple[str, ...], tones: dict[str, RGB], dx_from_row: int | None = None,
          dx: int = 0) -> None:
    for y, row in enumerate(art):
        for x, ch in enumerate(row):
            if ch in tones:
                shift = dx if dx_from_row is not None and y >= dx_from_row else 0
                if 0 <= x + shift < 8:
                    put_pixel(frame, x + shift, y, tones[ch])


def mail_frames() -> list[Frame]:
    frames = []
    cycle = len(ENVELOPE_EDGE) + 4
    for beat in range(cycle // 2):
        frame = new_frame()
        _draw(frame, ENVELOPE, ENVELOPE_TONES)
        base = [frame[y][x] for x, y in ENVELOPE_EDGE]
        draw_comet(frame, ENVELOPE_EDGE, beat * 2, base, (1.0, 0.55, 0.25))
        frames.append(frame)
    return frames


def chat_frames() -> list[Frame]:
    # Typing: the dots light one after another, hold, then clear.
    frames = []
    for beat in range(10):
        frame = new_frame()
        _draw(frame, BUBBLE, BUBBLE_TONES)
        lit = (beat // 2) % 5
        for i, (x, y) in enumerate(TYPING_DOTS):
            put_pixel(frame, x, y, PEARL if i < lit <= 3 else DOT_UNLIT)
        frames.append(frame)
    return frames


def bell_frames() -> list[Frame]:
    frames = []
    for sway in BELL_SWAY:
        frame = new_frame()
        _draw(frame, BELL, BELL_TONES, dx_from_row=4, dx=sway)
        frames.append(frame)
    return frames


def icon_frames(kind: str) -> list[Frame]:
    if kind == "mail":
        return mail_frames()
    if kind == "chat":
        return chat_frames()
    return bell_frames()


def render_lane(label: str, count: int) -> Frame:
    return render_glance_lane(label, meter=min(count, METER_FULL_AT) / METER_FULL_AT)


def build_payload(rule: AppRule, count: int) -> dict[str, Any]:
    return lane_payload(render_lane(rule.label, count), encode_icon_gif(icon_frames(rule.icon)),
                        duration_ms=8_000, lifetime_ms=60_000)


# ---------------------------------------------------------------- Controller

@dataclass(frozen=True)
class NotifyMirrorConfig:
    rules: tuple[AppRule, ...] = DEFAULT_RULES
    max_age_minutes: float = 15.0
    # 5 s matches the hub tick; a shorter poll would speed up every service.
    poll_seconds: float = 5.0
    # Re-push before the 60 s lifetime lapses while something is still waiting.
    refresh_seconds: float = 30.0


def pending(toasts: Iterable[Toast], rules: Sequence[AppRule], now: datetime,
            max_age_minutes: float) -> list[tuple[Toast, AppRule]]:
    """Allowlisted toasts still young enough to mention, newest first."""
    found = []
    for toast in toasts:
        rule = match_rule(toast.app, rules)
        if rule is None:
            continue
        age = (now - toast.created).total_seconds()
        if 0 <= age <= max_age_minutes * 60 or -60 < age < 0:  # tolerate small clock skew
            found.append((toast, rule))
    found.sort(key=lambda pair: pair[0].created, reverse=True)
    return found


class NotifyMirrorController:
    def __init__(self, panel: Any, config: NotifyMirrorConfig | None = None, *,
                 reader: Callable[[], list[Toast]] | None = None, clock: Any = time,
                 utc_now: Callable[[], datetime] | None = None,
                 logger: logging.Logger | None = None) -> None:
        self.panel = panel
        self.config = config or NotifyMirrorConfig()
        self.reader = reader or read_toasts
        self.clock = clock
        self.utc_now = utc_now or (lambda: datetime.now(timezone.utc))
        self.logger = logger or logging.getLogger("awtrix.notify_mirror")
        self.scene = PresenceScene(panel, APP_NAME, self.config.refresh_seconds, clock, self.logger)
        self.seen: set[int] | None = None
        self.next_poll_at = 0.0
        self.failing = False

    def _announce(self) -> None:
        activate = getattr(self.panel, "activate_app", None)
        if activate is None:
            return
        try:
            activate(APP_NAME)
        except Exception as err:  # noqa: BLE001
            self.logger.warning("Could not bring %s forward: %s", APP_NAME, err)

    def tick(self) -> int | None:
        now = self.clock.monotonic()
        if now < self.next_poll_at:
            return None
        self.next_poll_at = now + self.config.poll_seconds
        try:
            toasts = self.reader()
        except Exception as err:  # noqa: BLE001
            if not self.failing:
                self.logger.warning("Cannot read Windows notifications: %s", err)
                self.failing = True
            return None
        if self.failing:
            self.logger.info("Windows notifications readable again")
            self.failing = False

        waiting = pending(toasts, self.config.rules, self.utc_now(), self.config.max_age_minutes)
        ids = {toast.toast_id for toast, _ in waiting}
        # The first look only learns what is already there: a hub restart
        # must not announce yesterday's mail as new.
        fresh = set() if self.seen is None else ids - self.seen
        self.seen = ids if self.seen is None else (self.seen | ids)

        if not waiting:
            self.scene.hide()
            return 0
        newest_rule = waiting[0][1]
        count = sum(1 for _, rule in waiting if rule.label == newest_rule.label)
        # The key carries only the label and a count, so the log line does too.
        if self.scene.show((newest_rule.label, count), lambda _rev: build_payload(newest_rule, count)) and fresh:
            self._announce()
        return len(waiting)


def rules_from_config(value: Any) -> tuple[AppRule, ...]:
    """``[{"match": "Outlook", "label": "MAIL", "icon": "mail"}, ...]`` -> rules."""
    if not isinstance(value, list) or not value:
        raise ValueError("notifications.apps must be a non-empty list")
    rules = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("each notifications.apps entry must be an object")
        match, label = entry.get("match"), entry.get("label")
        icon = entry.get("icon", "bell")
        if not isinstance(match, str) or not match.strip():
            raise ValueError("notifications.apps[].match must be a non-empty string")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("notifications.apps[].label must be a non-empty string")
        if icon not in ("mail", "chat", "bell"):
            raise ValueError("notifications.apps[].icon must be mail, chat or bell")
        label = label.strip().upper()
        render_lane(label, 1)  # raises if the label does not fit the lane
        rules.append(AppRule(match.strip(), label, icon))
    return tuple(rules)


if __name__ == "__main__":
    toasts = read_toasts()
    waiting = pending(toasts, DEFAULT_RULES, datetime.now(timezone.utc), 15.0)
    print(f"{len(toasts)} toast(s) in the action centre; {len(waiting)} allowlisted and recent")
    for toast, rule in waiting:
        print(f"  {rule.label:<6} from {toast.app}")
