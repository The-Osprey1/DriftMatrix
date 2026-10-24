"""Calendar-domain logic for the AWTRIX ``Next Up`` app.

This module deliberately has no panel, HTTP, cache, filesystem, or artwork
code.  It turns an iCalendar document into deterministic :class:`Event`
objects and answers the product question: *which event is useful right now?*

Recurring events are expanded by :mod:`recurring_ical_events`; RRULE handling
is not reimplemented here.  That library folds EXDATEs, RECURRENCE-ID
overrides, and recurrence cancellations into occurrence components before this
module applies the display filters.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
import hashlib
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import TypeAlias
import unicodedata
from zoneinfo import ZoneInfo

from icalendar import Calendar
import recurring_ical_events


DEFAULT_TIMEZONE_NAME = "America/Los_Angeles"
DEFAULT_TIMEZONE = ZoneInfo(DEFAULT_TIMEZONE_NAME)
UTC_TIMEZONE = ZoneInfo("UTC")
DEFAULT_HORIZON = timedelta(hours=24)
DISPLAY_HORIZON = timedelta(minutes=120)
FIFTEEN_MINUTES = timedelta(minutes=15)
FIVE_MINUTES = timedelta(minutes=5)
CURRENT_GRACE = timedelta(minutes=10)


class CalendarParseError(ValueError):
    """The supplied bytes are not a calendar that can be expanded safely."""


class EventStage(str, Enum):
    """Visibility/countdown state consumed by the future renderer."""

    ABSENT = "absent"
    NORMAL = "normal"
    FIFTEEN = "15m"
    FIVE = "5m"
    NOW = "now"


NOTIFICATION_STAGES = frozenset(
    {EventStage.FIFTEEN, EventStage.FIVE, EventStage.NOW}
)


AliasPairs: TypeAlias = Mapping[str, str] | Iterable[tuple[str, str]]


@dataclass(frozen=True, slots=True)
class Event:
    """One expanded calendar occurrence in the configured local timezone.

    ``identity`` is based on source, UID, and the original recurrence slot.  An
    override can therefore move or rename an occurrence without looking like a
    brand-new event and replaying alerts.
    """

    identity: str
    uid: str
    title: str
    start: datetime
    end: datetime
    location: str = ""
    all_day: bool = False
    source_id: str = ""

    def __post_init__(self) -> None:
        if not self.identity:
            raise ValueError("event identity must not be empty")
        if self.start.tzinfo is None or self.start.utcoffset() is None:
            raise ValueError("event start must be timezone-aware")
        if self.end.tzinfo is None or self.end.utcoffset() is None:
            raise ValueError("event end must be timezone-aware")
        if self.end.timestamp() < self.start.timestamp():
            raise ValueError("event end must not precede its start")

    def compact_label(
        self,
        aliases: AliasPairs = (),
        *,
        max_chars: int = 22,
        uppercase: bool = True,
    ) -> str:
        """Return a panel-safe label without changing event identity."""

        return safe_compact_label(
            self.title,
            aliases,
            max_chars=max_chars,
            uppercase=uppercase,
        )


@dataclass(frozen=True, slots=True)
class NextUpDecision:
    """A complete domain decision, independent of how it is displayed."""

    event: Event | None
    stage: EventStage
    minutes_to_start: int | None
    notification: EventStage | None = None
    identity_changed: bool = False

    @property
    def visible(self) -> bool:
        return self.stage is not EventStage.ABSENT

    @property
    def should_activate(self) -> bool:
        return self.notification is not None


def parse_ics(data: bytes) -> Calendar:
    """Parse raw iCalendar bytes and raise a stable domain error on failure."""

    if not isinstance(data, bytes):
        raise TypeError("iCalendar input must be bytes")
    if not data:
        raise CalendarParseError("iCalendar input is empty")
    try:
        calendar = Calendar.from_ical(data)
    except Exception as exc:
        raise CalendarParseError("invalid iCalendar data") from exc
    if not isinstance(calendar, Calendar):
        raise CalendarParseError("iCalendar data has no VCALENDAR root")
    return calendar


def _as_local_datetime(value: date | datetime, timezone: ZoneInfo) -> datetime:
    """Normalize aware values and interpret floating/date values locally."""

    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            value = value.replace(tzinfo=timezone)
        # The UTC round-trip is intentional.  Some calendar libraries produce
        # an imaginary local DTEND while adding a short DURATION across a DST
        # spring gap (for example 02:20).  A direct astimezone call is a no-op
        # when the ZoneInfo object is already identical; the round-trip
        # canonicalizes that instant to the real post-gap wall time (03:20).
        return value.astimezone(UTC_TIMEZONE).astimezone(timezone)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone)
    raise TypeError(f"unsupported calendar time {value!r}")


def _elapsed_delta(later: datetime, earlier: datetime) -> timedelta:
    """Subtract instants correctly even across a local DST fold or gap."""

    return later.astimezone(UTC_TIMEZONE) - earlier.astimezone(UTC_TIMEZONE)


def _shift_elapsed(value: datetime, delta: timedelta) -> datetime:
    """Move by elapsed time while returning to the original local timezone."""

    return (value.astimezone(UTC_TIMEZONE) + delta).astimezone(value.tzinfo)


def _decoded(component: object, name: str, default: object = None) -> object:
    property_value = component.get(name)  # type: ignore[attr-defined]
    if property_value is None:
        return default
    try:
        return component.decoded(name)  # type: ignore[attr-defined]
    except (KeyError, ValueError, TypeError):
        return getattr(property_value, "dt", property_value)


def _text(component: object, name: str, default: str = "") -> str:
    value = component.get(name)  # type: ignore[attr-defined]
    if value is None:
        return default
    return str(value).strip()


def _occurrence_key(component: object, start: datetime, timezone: ZoneInfo) -> str:
    recurrence = _decoded(component, "RECURRENCE-ID")
    value = recurrence if isinstance(recurrence, (date, datetime)) else start
    local = _as_local_datetime(value, timezone)
    return local.astimezone(UTC_TIMEZONE).strftime("%Y%m%dT%H%M%SZ")


def _identity(
    *,
    source_id: str,
    uid: str,
    occurrence_key: str,
    fallback_title: str,
) -> str:
    # Missing UIDs are invalid per RFC 5545 but occur in real exported feeds.
    # The fallback remains deterministic without exposing calendar text in logs.
    stable_uid = uid or f"missing:{fallback_title}"
    material = "\x1f".join((source_id, stable_uid, occurrence_key)).encode(
        "utf-8", "surrogatepass"
    )
    return hashlib.sha256(material).hexdigest()[:24]


def _component_to_event(
    component: object,
    *,
    timezone: ZoneInfo,
    source_id: str,
) -> Event | None:
    status = _text(component, "STATUS").casefold()
    transparency = _text(component, "TRANSP").casefold()
    if status == "cancelled" or transparency == "transparent":
        return None

    raw_start = _decoded(component, "DTSTART")
    if not isinstance(raw_start, (date, datetime)):
        raise CalendarParseError("VEVENT has no valid DTSTART")
    all_day = isinstance(raw_start, date) and not isinstance(raw_start, datetime)
    start = _as_local_datetime(raw_start, timezone)

    raw_end = _decoded(component, "DTEND")
    if isinstance(raw_end, (date, datetime)):
        end = _as_local_datetime(raw_end, timezone)
    else:
        duration = _decoded(component, "DURATION")
        if isinstance(duration, timedelta):
            end = _shift_elapsed(start, duration)
        elif all_day:
            end = start + timedelta(days=1)
        else:
            # RFC 5545 date-time events without DTEND/DURATION are instantaneous.
            end = start
    if end.timestamp() < start.timestamp():
        raise CalendarParseError("VEVENT ends before it starts")

    title = _text(component, "SUMMARY", "EVENT") or "EVENT"
    uid = _text(component, "UID")
    occurrence_key = _occurrence_key(component, start, timezone)
    return Event(
        identity=_identity(
            source_id=source_id,
            uid=uid,
            occurrence_key=occurrence_key,
            fallback_title=title,
        ),
        uid=uid,
        title=title,
        start=start,
        end=end,
        location=_text(component, "LOCATION"),
        all_day=all_day,
        source_id=source_id,
    )


def _calendar_for_expansion(calendar: Calendar) -> Calendar:
    """Adapt terse cancellation overrides without mutating the caller's tree.

    RFC 5545 permits a recurrence cancellation to identify its slot with UID
    and RECURRENCE-ID.  Some providers omit DTSTART on that CANCELLED override.
    ``recurring_ical_events`` needs DTSTART while constructing its series, so a
    private clone receives a DTSTART equal to RECURRENCE-ID.  The occurrence is
    still emitted as CANCELLED and filtered; its only purpose is suppressing the
    matching master occurrence.
    """

    needs_repair = any(
        _text(component, "STATUS").casefold() == "cancelled"
        and component.get("RECURRENCE-ID") is not None
        and component.get("DTSTART") is None
        for component in calendar.walk("VEVENT")
    )
    if not needs_repair:
        return calendar
    try:
        clone = Calendar.from_ical(calendar.to_ical())
        for component in clone.walk("VEVENT"):
            if (
                _text(component, "STATUS").casefold() == "cancelled"
                and component.get("RECURRENCE-ID") is not None
                and component.get("DTSTART") is None
            ):
                recurrence = _decoded(component, "RECURRENCE-ID")
                if not isinstance(recurrence, (date, datetime)):
                    raise CalendarParseError(
                        "cancelled recurrence has no valid RECURRENCE-ID"
                    )
                component.add("DTSTART", recurrence)
        return clone
    except CalendarParseError:
        raise
    except Exception as exc:
        raise CalendarParseError("could not normalize recurrence cancellation") from exc


def expand_occurrences(
    calendar: Calendar,
    now: datetime,
    *,
    timezone: ZoneInfo = DEFAULT_TIMEZONE,
    horizon: timedelta = DEFAULT_HORIZON,
    current_grace: timedelta = CURRENT_GRACE,
    include_all_day: bool = False,
    source_id: str = "",
) -> tuple[Event, ...]:
    """Expand VEVENT occurrences around ``now`` using component occurrences.

    The normal query window looks 24 hours forward.  A ten-minute look-back is
    added solely so an occurrence that just started can occupy the ``NOW``
    grace state.  Events that began before that look-back are discarded even
    when they have a long duration.
    """

    if not isinstance(calendar, Calendar):
        raise TypeError("calendar must be an icalendar.Calendar")
    if not isinstance(now, datetime):
        raise TypeError("now must be a datetime")
    if horizon <= timedelta(0):
        raise ValueError("horizon must be positive")
    if current_grace < timedelta(0):
        raise ValueError("current_grace must not be negative")

    local_now = _as_local_datetime(now, timezone)
    query_start = _shift_elapsed(local_now, -current_grace)
    query_stop = _shift_elapsed(local_now, horizon)
    try:
        # ``between`` returns occurrence components.  In particular, DTSTART is
        # the concrete occurrence and RECURRENCE-ID remains the stable slot key.
        components = recurring_ical_events.of(_calendar_for_expansion(calendar)).between(
            query_start, query_stop
        )
    except Exception as exc:
        raise CalendarParseError("could not expand calendar occurrences") from exc

    by_identity: dict[str, Event] = {}
    for component in components:
        event = _component_to_event(
            component, timezone=timezone, source_id=source_id
        )
        if event is None or (event.all_day and not include_all_day):
            continue
        if (
            event.start.timestamp() < query_start.timestamp()
            or event.start.timestamp() > query_stop.timestamp()
        ):
            # The recurrence engine also returns long events that overlap the
            # window.  Next Up is start-oriented, so old overlaps are not useful.
            continue
        existing = by_identity.get(event.identity)
        if existing is None:
            by_identity[event.identity] = event
        else:
            # Broken feeds occasionally duplicate a component.  Choose a stable
            # winner rather than allowing source order to affect the result.
            by_identity[event.identity] = min(
                existing,
                event,
                key=lambda item: (
                    item.start.timestamp(),
                    item.end.timestamp(),
                    item.title.casefold(),
                    item.location.casefold(),
                ),
            )
    return tuple(
        sorted(
            by_identity.values(),
            key=lambda item: (
                item.start.timestamp(),
                item.end.timestamp(),
                item.identity,
            ),
        )
    )


def events_from_ics(
    data: bytes,
    now: datetime,
    *,
    timezone: ZoneInfo = DEFAULT_TIMEZONE,
    horizon: timedelta = DEFAULT_HORIZON,
    current_grace: timedelta = CURRENT_GRACE,
    include_all_day: bool = False,
    source_id: str = "",
) -> tuple[Event, ...]:
    """Convenience boundary combining :func:`parse_ics` and expansion."""

    return expand_occurrences(
        parse_ics(data),
        now,
        timezone=timezone,
        horizon=horizon,
        current_grace=current_grace,
        include_all_day=include_all_day,
        source_id=source_id,
    )


# An explicit alias reads naturally in source adapters and preserves a small,
# discoverable public API for callers that think in parsing rather than feeds.
parse_ics_events = events_from_ics


def select_event(
    events: Sequence[Event],
    now: datetime,
    *,
    current_grace: timedelta = CURRENT_GRACE,
) -> Event | None:
    """Choose a current occurrence or the chronologically next occurrence.

    Rules are deterministic:

    * a just-started occurrence beats every future occurrence;
    * overlapping current occurrences prefer the most recently started, then
      the one ending first, then identity;
    * future ties prefer the one ending first, then identity;
    * occurrences more than ``current_grace`` past their start are expired.
    """

    if not isinstance(now, datetime):
        raise TypeError("now must be a datetime")
    if current_grace < timedelta(0):
        raise ValueError("current_grace must not be negative")
    if now.tzinfo is None or now.utcoffset() is None:
        now = now.replace(tzinfo=DEFAULT_TIMEZONE)

    current = [
        event
        for event in events
        if event.start.timestamp() <= now.timestamp()
        and _elapsed_delta(now, event.start) <= current_grace
    ]
    if current:
        return min(
            current,
            key=lambda event: (
                -event.start.timestamp(),
                event.end.timestamp(),
                event.identity,
            ),
        )

    future = [event for event in events if event.start.timestamp() > now.timestamp()]
    if not future:
        return None
    return min(
        future,
        key=lambda event: (
            event.start.timestamp(),
            event.end.timestamp(),
            event.identity,
        ),
    )


# Slightly longer spelling for callers that prefer domain-explicit names.
select_next_event = select_event


def classify_event(
    event: Event | None,
    now: datetime,
    *,
    display_horizon: timedelta = DISPLAY_HORIZON,
    current_grace: timedelta = CURRENT_GRACE,
) -> tuple[EventStage, int | None]:
    """Return the display stage and truthful, ceiling-rounded ETA minutes."""

    if event is None:
        return EventStage.ABSENT, None
    if display_horizon <= timedelta(0):
        raise ValueError("display_horizon must be positive")
    if current_grace < timedelta(0):
        raise ValueError("current_grace must not be negative")
    if now.tzinfo is None or now.utcoffset() is None:
        now = now.replace(tzinfo=DEFAULT_TIMEZONE)

    remaining = _elapsed_delta(event.start, now)
    seconds = remaining.total_seconds()
    minutes = max(0, math.ceil(seconds / 60.0))
    if remaining > display_horizon:
        return EventStage.ABSENT, minutes
    if remaining > FIFTEEN_MINUTES:
        return EventStage.NORMAL, minutes
    if remaining > FIVE_MINUTES:
        return EventStage.FIFTEEN, minutes
    if remaining > timedelta(0):
        return EventStage.FIVE, minutes
    if -remaining <= current_grace:
        return EventStage.NOW, 0
    return EventStage.ABSENT, 0


class NotificationTracker:
    """Suppress repeated activations while preserving threshold transitions.

    Feed refreshes can return a newly allocated :class:`Event` every time.  The
    stable event identity—not object equality or title—is therefore the reset
    boundary.  A temporary empty refresh does not erase notification memory.
    """

    def __init__(self) -> None:
        self._identity: str | None = None
        self._emitted: set[EventStage] = set()
        self._last_stage = EventStage.ABSENT

    @property
    def identity(self) -> str | None:
        return self._identity

    @property
    def last_stage(self) -> EventStage:
        return self._last_stage

    def reset(self) -> None:
        self._identity = None
        self._emitted.clear()
        self._last_stage = EventStage.ABSENT

    def update(self, event: Event | None, now: datetime) -> NextUpDecision:
        changed = event is not None and event.identity != self._identity
        if changed:
            self._identity = event.identity
            self._emitted.clear()
            self._last_stage = EventStage.ABSENT

        stage, minutes = classify_event(event, now)
        notification: EventStage | None = None
        if stage in NOTIFICATION_STAGES and stage not in self._emitted:
            notification = stage
            self._emitted.add(stage)
        self._last_stage = stage
        return NextUpDecision(
            event=event,
            stage=stage,
            minutes_to_start=minutes,
            notification=notification,
            identity_changed=changed,
        )

    def evaluate(self, events: Sequence[Event], now: datetime) -> NextUpDecision:
        return self.update(select_event(events, now), now)


def _alias_key(value: str) -> str:
    return " ".join(str(value).split()).casefold()


def normalize_aliases(aliases: AliasPairs = ()) -> dict[str, str]:
    """Return a case/whitespace-insensitive exact-title alias map."""

    items = aliases.items() if isinstance(aliases, Mapping) else aliases
    normalized: dict[str, str] = {}
    for pair in items:
        try:
            source, replacement = pair
        except (TypeError, ValueError) as exc:
            raise ValueError("aliases must contain (source, replacement) pairs") from exc
        key = _alias_key(source)
        value = " ".join(str(replacement).split())
        if not key:
            raise ValueError("alias source must not be empty")
        if not value:
            raise ValueError("alias replacement must not be empty")
        existing = normalized.get(key)
        if existing is not None and existing != value:
            raise ValueError(f"conflicting aliases for {source!r}")
        normalized[key] = value
    return normalized


def apply_alias(title: str, aliases: AliasPairs = ()) -> str:
    """Apply an exact title alias, ignoring case and repeated whitespace."""

    cleaned = " ".join(str(title).split())
    return normalize_aliases(aliases).get(_alias_key(cleaned), cleaned)


_PUNCTUATION_TRANSLATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2026": "...",
        "\u00a0": " ",
    }
)
_UNSAFE_LABEL = re.compile(r"[^A-Za-z0-9 &+:/.'\"#@!?_\-]+")


def safe_compact_label(
    title: str,
    aliases: AliasPairs = (),
    *,
    max_chars: int = 22,
    uppercase: bool = True,
    fallback: str = "EVENT",
) -> str:
    """Produce bounded ASCII text suitable for AWTRIX's small pixel font.

    Calendar control characters and unsupported glyphs are removed, aliases are
    applied before truncation, and ``...`` is used instead of a font-dependent
    ellipsis glyph.  Callers choose the character budget for their layout.
    """

    if isinstance(max_chars, bool) or not isinstance(max_chars, int):
        raise TypeError("max_chars must be an integer")
    if max_chars < 1:
        raise ValueError("max_chars must be at least 1")

    value = apply_alias(title, aliases).translate(_PUNCTUATION_TRANSLATION)
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = _UNSAFE_LABEL.sub(" ", value)
    value = " ".join(value.split()).strip(" .")
    if uppercase:
        value = value.upper()
    if not value:
        value = safe_compact_label(
            fallback,
            (),
            max_chars=max_chars,
            uppercase=uppercase,
            fallback="EVENT" if fallback != "EVENT" else "E",
        )

    if len(value) <= max_chars:
        return value
    if max_chars <= 3:
        return value[:max_chars]
    budget = max_chars - 3
    head = value[:budget].rstrip()
    word_head = head.rsplit(" ", 1)[0] if " " in head else ""
    if len(word_head) >= max(3, budget // 2):
        head = word_head
    return head.rstrip(" .") + "..."


__all__ = [
    "AliasPairs",
    "CalendarParseError",
    "CURRENT_GRACE",
    "DEFAULT_HORIZON",
    "DEFAULT_TIMEZONE",
    "DEFAULT_TIMEZONE_NAME",
    "DISPLAY_HORIZON",
    "Event",
    "EventStage",
    "NextUpDecision",
    "NotificationTracker",
    "apply_alias",
    "classify_event",
    "events_from_ics",
    "expand_occurrences",
    "normalize_aliases",
    "parse_ics",
    "parse_ics_events",
    "safe_compact_label",
    "select_event",
    "select_next_event",
]
