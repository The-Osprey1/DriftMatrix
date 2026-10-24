"""Safe orchestration for the PC-hosted AWTRIX ``Next Up`` app.

The source, calendar-domain, artwork, and panel transport modules deliberately
have narrow responsibilities.  This module joins them without weakening their
safety boundaries:

* every configured calendar owns a distinct last-good cache directory;
* only a currently ready source body is parsed, and a failed source contributes
  no events;
* wall time is re-evaluated every tick even though calendars refresh slowly;
* panel mutations use non-blocking, bounded retry backoff;
* notification thresholds are delivered once, with cooldown failures retried
  and quiet-hour notifications intentionally discarded; and
* deletion is attempted only after this controller successfully pushed the
  exact pushed-app name during the current process lifetime.

Calendar titles and source URLs are intentionally absent from controller,
binding, state, and tick-result reprs and from every log message in this module.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import logging
import math
from pathlib import Path
import time
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from calendar_source import (
    CalendarLoadResult,
    CalendarSource,
    CalendarSourceConfig,
    DEFAULT_MAX_BODY_BYTES,
    default_runtime_dir,
)
from next_up import (
    Event,
    EventStage,
    NextUpDecision,
    classify_event,
    events_from_ics,
    normalize_aliases,
    select_event,
)
from next_up_preview import Urgency, build_payload
from panel_client import (
    ActivationOutcome,
    MutationPriority,
    PanelClient,
    PanelClientError,
    is_quiet_time,
)


APP_NAME = "nextup"
DISPLAY_HORIZON_MINUTES = 120
CURRENT_GRACE_MINUTES = 10
DEFAULT_REFRESH_SECONDS = 5 * 60.0
DEFAULT_MAX_STALE_SECONDS = 6 * 60 * 60.0
DEFAULT_KEEPALIVE_SECONDS = 60.0
DEFAULT_LIFETIME_SECONDS = 180.0
DEFAULT_PAGE_DURATION_MS = 8_000
DEFAULT_PANEL_BACKOFF_SECONDS = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
NOTIFICATION_STAGES = frozenset(
    (EventStage.FIFTEEN, EventStage.FIVE, EventStage.NOW)
)


class ControllerClock(Protocol):
    """Both clocks needed by the controller, injectable for deterministic tests."""

    def monotonic(self) -> float: ...

    def wall_now(self) -> datetime: ...


class SystemControllerClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def wall_now(self) -> datetime:
        return datetime.now().astimezone()


class CalendarLoader(Protocol):
    def load(self, *, force: bool = False) -> CalendarLoadResult: ...


class EventParser(Protocol):
    def __call__(
        self,
        data: bytes,
        now: datetime,
        *,
        timezone: ZoneInfo,
        include_all_day: bool,
        source_id: str,
    ) -> tuple[Event, ...]: ...


class PayloadBuilder(Protocol):
    def __call__(self, event: Any = None, **kwargs: Any) -> dict[str, Any]: ...


@dataclass(frozen=True, repr=False)
class CalendarBinding:
    """One privacy-safe loader binding.

    ``source_id`` is a non-secret digest token used only for stable event
    identity.  The loader itself is hidden from repr because it may retain a
    private calendar URL.
    """

    loader: CalendarLoader = field(repr=False, compare=False)
    source_id: str
    max_stale_seconds: float = DEFAULT_MAX_STALE_SECONDS

    def __post_init__(self) -> None:
        if not self.source_id or any(character.isspace() for character in self.source_id):
            raise ValueError("source_id must be a non-empty opaque token")
        if not math.isfinite(float(self.max_stale_seconds)) or self.max_stale_seconds <= 0:
            raise ValueError("max_stale_seconds must be positive")

    def __repr__(self) -> str:
        return (
            "CalendarBinding("
            f"source_id={self.source_id!r}, loader='<redacted>', "
            f"max_stale_seconds={float(self.max_stale_seconds)!r})"
        )


@dataclass(slots=True, repr=False)
class _SourceState:
    binding: CalendarBinding = field(repr=False)
    events: tuple[Event, ...] = field(default=(), repr=False)
    next_refresh_at: float = 0.0
    expires_at_wall: float | None = None
    ready: bool = False


@dataclass(frozen=True)
class NextUpTick:
    """Observable outcome of one controller tick, with private event text hidden."""

    stage: EventStage
    minutes_to_start: int | None
    visible: bool
    event_changed: bool = False
    refreshed_sources: int = 0
    ready_sources: int = 0
    pushed: bool = False
    activated: bool = False
    activation_discarded: bool = False
    activation_pending: bool = False
    delete_checked: bool = False
    deleted: bool = False
    panel_deferred: bool = False
    decision: NextUpDecision = field(repr=False, compare=False, default_factory=lambda: NextUpDecision(None, EventStage.ABSENT, None))


@dataclass(frozen=True)
class NextUpPolicy:
    refresh_seconds: float = DEFAULT_REFRESH_SECONDS
    keepalive_seconds: float = DEFAULT_KEEPALIVE_SECONDS
    lifetime_seconds: float = DEFAULT_LIFETIME_SECONDS
    page_duration_ms: int = DEFAULT_PAGE_DURATION_MS
    panel_backoff_seconds: tuple[float, ...] = DEFAULT_PANEL_BACKOFF_SECONDS

    def __post_init__(self) -> None:
        for name, value in (
            ("refresh_seconds", self.refresh_seconds),
            ("keepalive_seconds", self.keepalive_seconds),
            ("lifetime_seconds", self.lifetime_seconds),
        ):
            if not math.isfinite(float(value)) or value <= 0:
                raise ValueError(f"{name} must be positive")
        if isinstance(self.page_duration_ms, bool) or self.page_duration_ms <= 0:
            raise ValueError("page_duration_ms must be positive")
        if not self.panel_backoff_seconds:
            raise ValueError("panel_backoff_seconds must not be empty")
        if any(
            not math.isfinite(float(value)) or value < 0
            for value in self.panel_backoff_seconds
        ):
            raise ValueError("panel backoff values must be finite and non-negative")


def _source_digest(source: str | Path) -> str:
    """Return an opaque token stable across harmless source-list reordering."""

    value = str(source).strip()
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def build_calendar_sources(
    sources: Sequence[str | Path],
    *,
    runtime_dir: Path | None = None,
    refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
    max_stale_seconds: float = DEFAULT_MAX_STALE_SECONDS,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    source_clock: Any | None = None,
    session_factory: Callable[[], Any] | None = None,
) -> tuple[CalendarBinding, ...]:
    """Build independent source loaders whose cache paths cannot collide.

    The source digest is stable across harmless list reordering.  A private
    duplicate-occurrence suffix ensures repeated configured sources still
    receive separate caches.  No returned repr embeds the URL or local path.
    """

    if len(sources) > 16:
        raise ValueError("Next Up supports at most 16 calendar sources")
    root = Path(runtime_dir or default_runtime_dir()).expanduser()
    bindings: list[CalendarBinding] = []
    seen_cache_paths: set[Path] = set()
    digest_occurrences: dict[str, int] = {}
    for ordinal, source in enumerate(sources):
        if not str(source).strip():
            raise ValueError("calendar sources must be non-empty")
        digest = _source_digest(source)
        occurrence = digest_occurrences.get(digest, 0)
        digest_occurrences[digest] = occurrence + 1
        token = digest if occurrence == 0 else f"{digest}-{occurrence:02d}"
        source_id = f"calendar-{token}"
        source_runtime = root / f"nextup-{token}"
        config = CalendarSourceConfig(
            source=source,
            runtime_dir=source_runtime,
            refresh_interval_seconds=float(refresh_seconds),
            max_stale_seconds=float(max_stale_seconds),
            max_body_bytes=max_body_bytes,
        )
        cache_path = config.cache_path.resolve(strict=False)
        if cache_path in seen_cache_paths:
            raise AssertionError("calendar cache path collision")
        seen_cache_paths.add(cache_path)
        loader = CalendarSource(
            config,
            clock=source_clock,
            session=None if session_factory is None else session_factory(),
        )
        bindings.append(
            CalendarBinding(
                loader=loader,
                source_id=source_id,
                max_stale_seconds=float(max_stale_seconds),
            )
        )
    return tuple(bindings)


def _as_local_wall(moment: datetime, timezone: ZoneInfo) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        return moment.replace(tzinfo=timezone)
    return moment.astimezone(timezone)


def _urgency(stage: EventStage) -> Urgency:
    return {
        EventStage.NORMAL: Urgency.ROUTINE,
        EventStage.FIFTEEN: Urgency.SOON,
        EventStage.FIVE: Urgency.IMMINENT,
        EventStage.NOW: Urgency.NOW,
    }.get(stage, Urgency.ROUTINE)


class NextUpController:
    """Coordinate multiple calendars and the shared :class:`PanelClient`."""

    def __init__(
        self,
        sources: Sequence[CalendarBinding | CalendarLoader],
        panel: PanelClient,
        *,
        timezone: ZoneInfo | str = ZoneInfo("America/Los_Angeles"),
        include_all_day: bool = False,
        aliases: Mapping[str, str] | Sequence[tuple[str, str]] = (),
        policy: NextUpPolicy | None = None,
        clock: ControllerClock | None = None,
        parser: EventParser = events_from_ics,
        payload_builder: PayloadBuilder = build_payload,
        logger: logging.Logger | None = None,
    ) -> None:
        if not sources:
            raise ValueError("Next Up needs at least one calendar source")
        if len(sources) > 16:
            raise ValueError("Next Up supports at most 16 calendar sources")
        self.panel = panel
        self.timezone = timezone if isinstance(timezone, ZoneInfo) else ZoneInfo(str(timezone))
        self.include_all_day = bool(include_all_day)
        self.aliases = normalize_aliases(aliases)
        self.policy = policy or NextUpPolicy()
        self.clock = clock or SystemControllerClock()
        self.parser = parser
        self.payload_builder = payload_builder
        self.logger = logger or logging.getLogger("awtrix.utility_hub.next_up")

        bindings: list[CalendarBinding] = []
        cache_paths: set[Path] = set()
        for ordinal, source in enumerate(sources):
            if isinstance(source, CalendarBinding):
                binding = source
            else:
                maximum = getattr(getattr(source, "config", None), "max_stale_seconds", DEFAULT_MAX_STALE_SECONDS)
                binding = CalendarBinding(
                    loader=source,
                    source_id=f"calendar-loader-{ordinal:02d}",
                    max_stale_seconds=float(maximum),
                )
            bindings.append(binding)
            configured_cache = getattr(
                getattr(binding.loader, "config", None), "cache_path", None
            )
            if configured_cache is not None:
                cache_path = Path(configured_cache).expanduser().resolve(strict=False)
                if cache_path in cache_paths:
                    raise ValueError("calendar source cache paths must be unique")
                cache_paths.add(cache_path)
        source_ids = [binding.source_id for binding in bindings]
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("calendar source_id values must be unique")
        self._sources = [_SourceState(binding) for binding in bindings]

        self._selected_identity: str | None = None
        self._last_stage = EventStage.ABSENT
        self._last_pushed_signature: tuple[str, EventStage] | None = None
        self._last_push_at: float | None = None
        self._remote_present = False

        # Notification delivery memory deliberately survives a temporary empty
        # refresh, matching the domain tracker's no-replay behavior.
        self._notification_identity: str | None = None
        self._delivered_notifications: set[EventStage] = set()
        self._pending_notification: EventStage | None = None
        self._next_activation_attempt_at = 0.0

        self._panel_failures = 0
        self._next_panel_attempt_at = 0.0

    def __repr__(self) -> str:
        return (
            "NextUpController("
            f"sources={len(self._sources)}, timezone={self.timezone.key!r}, "
            f"include_all_day={self.include_all_day!r}, "
            f"refresh_seconds={self.policy.refresh_seconds!r})"
        )

    @property
    def remote_present(self) -> bool:
        return self._remote_present

    @property
    def panel_failures(self) -> int:
        return self._panel_failures

    def _panel_available(self, now: float) -> bool:
        return now >= self._next_panel_attempt_at

    def _panel_failed(self, now: float, operation: str, error: BaseException) -> None:
        self._panel_failures += 1
        index = min(self._panel_failures, len(self.policy.panel_backoff_seconds)) - 1
        delay = float(self.policy.panel_backoff_seconds[index])
        self._next_panel_attempt_at = now + delay
        if self._panel_failures in (1, 3, 5) or self._panel_failures % 12 == 0:
            # The exception type is enough to diagnose a transport class while
            # ensuring a private URL or event title can never enter the log.
            self.logger.warning(
                "Next Up panel %s failed (%s); retry in %.0fs",
                operation,
                type(error).__name__,
                delay,
            )

    def _panel_succeeded(self, now: float) -> None:
        self._panel_failures = 0
        self._next_panel_attempt_at = now

    def _source_expired(self, source: _SourceState, wall_epoch: float) -> bool:
        expires = source.expires_at_wall
        return expires is not None and wall_epoch > expires

    def _refresh_source(
        self,
        source: _SourceState,
        *,
        now_mono: float,
        wall: datetime,
        ordinal: int,
    ) -> bool:
        source.next_refresh_at = now_mono + self.policy.refresh_seconds
        try:
            result = source.binding.loader.load()
        except Exception as error:  # Source adapters are an isolation boundary.
            source.events = ()
            source.expires_at_wall = None
            source.ready = False
            self.logger.warning(
                "Next Up calendar source %d load failed (%s); source suppressed",
                ordinal + 1,
                type(error).__name__,
            )
            return False

        body = result.body if bool(getattr(result, "ready", False)) else None
        if not isinstance(body, bytes):
            source.events = ()
            source.expires_at_wall = None
            source.ready = False
            # Say so. A loader that raises was already logged above, but a
            # loader that fails the way it is *designed* to -- returning a
            # result whose `ready` is False -- fell through here in complete
            # silence. That is the wrong way round: the graceful path was the
            # invisible one, so an expired link or a calendar the server
            # stopped serving looked exactly like a day with nothing on it.
            #
            # `quiet` is honoured, because some outcomes really are routine:
            # a source that is simply not configured has nothing to report.
            if not bool(getattr(result, "quiet", False)):
                self.logger.warning(
                    "Next Up calendar source %d unavailable (%s%s); showing an empty day",
                    ordinal + 1,
                    getattr(getattr(result, "status", None), "value", "unknown"),
                    f": {result.message}" if getattr(result, "message", "") else "",
                )
            return False

        metadata = getattr(result, "metadata", None)
        validated_at = getattr(metadata, "validated_at", None)
        try:
            validated = float(validated_at)
        except (TypeError, ValueError, OverflowError):
            validated = wall.timestamp()
        expires_at = validated + float(source.binding.max_stale_seconds)
        if (
            not math.isfinite(validated)
            or not math.isfinite(expires_at)
            or wall.timestamp() > expires_at
        ):
            source.events = ()
            source.expires_at_wall = None
            source.ready = False
            return False

        try:
            events = self.parser(
                body,
                wall,
                timezone=self.timezone,
                include_all_day=self.include_all_day,
                source_id=source.binding.source_id,
            )
            if not isinstance(events, tuple) or any(not isinstance(event, Event) for event in events):
                raise TypeError("calendar parser returned an invalid event collection")
        except Exception as error:  # Parse failure must fail closed for this source.
            source.events = ()
            source.expires_at_wall = None
            source.ready = False
            self.logger.warning(
                "Next Up calendar source %d parse failed (%s); source suppressed",
                ordinal + 1,
                type(error).__name__,
            )
            return False

        source.events = events
        source.expires_at_wall = expires_at
        source.ready = True
        return True

    def _refresh_sources(self, now_mono: float, wall: datetime) -> tuple[int, int]:
        refreshed = 0
        wall_epoch = wall.timestamp()
        for ordinal, source in enumerate(self._sources):
            if now_mono >= source.next_refresh_at:
                refreshed += 1
                self._refresh_source(
                    source,
                    now_mono=now_mono,
                    wall=wall,
                    ordinal=ordinal,
                )
            elif source.ready and self._source_expired(source, wall_epoch):
                # A forward wall-clock jump or very short stale policy may make
                # data expire before the next acquisition cadence.
                source.events = ()
                source.expires_at_wall = None
                source.ready = False
        return refreshed, sum(source.ready for source in self._sources)

    def _combined_events(self) -> tuple[Event, ...]:
        return tuple(event for source in self._sources if source.ready for event in source.events)

    def _payloads(
        self, event: Event, stage: EventStage, minutes: int | None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        title = event.compact_label(self.aliases, max_chars=22)
        common = {
            "title": title,
            "minutes_until": minutes,
            "urgency": _urgency(stage),
            "duration_ms": self.policy.page_duration_ms,
            "lifetime_ms": round(self.policy.lifetime_seconds * 1000),
        }
        return (
            self.payload_builder(animated=True, **common),
            self.payload_builder(animated=False, **common),
        )

    def _push(
        self,
        event: Event,
        stage: EventStage,
        minutes: int | None,
        now: float,
    ) -> bool:
        if not self._panel_available(now):
            return False
        try:
            animated, static = self._payloads(event, stage, minutes)
            self.panel.push_app(APP_NAME, animated, static_payload=static)
        except (PanelClientError, OSError, RuntimeError) as error:
            self._panel_failed(self.clock.monotonic(), "push", error)
            return False
        completed = self.clock.monotonic()
        self._panel_succeeded(completed)
        self._last_push_at = completed
        self._last_pushed_signature = (event.identity, stage)
        self._remote_present = True
        return True

    def _delete(self, now: float) -> tuple[bool, bool]:
        if not self._panel_available(now):
            return False, False
        try:
            removed = bool(self.panel.delete_pushed_app(APP_NAME))
        except (PanelClientError, OSError, RuntimeError) as error:
            self._panel_failed(self.clock.monotonic(), "delete", error)
            return False, False
        completed = self.clock.monotonic()
        self._panel_succeeded(completed)
        self._remote_present = False
        self._last_push_at = None
        self._last_pushed_signature = None
        return True, removed

    def _quiet_now(self, wall: datetime) -> bool:
        policy = getattr(self.panel, "policy", None)
        if policy is None:
            return False
        return is_quiet_time(
            wall,
            getattr(policy, "quiet_start", None),
            getattr(policy, "quiet_end", None),
        )

    def _prepare_notification(
        self,
        event: Event | None,
        stage: EventStage,
        *,
        now: float,
        wall: datetime,
    ) -> tuple[bool, bool]:
        """Return ``(discarded, newly_queued)`` after updating threshold memory."""

        if event is not None and event.identity != self._notification_identity:
            self._notification_identity = event.identity
            self._delivered_notifications.clear()
            self._pending_notification = None
            self._next_activation_attempt_at = now

        if event is None or stage not in NOTIFICATION_STAGES:
            self._pending_notification = None
            return False, False
        if stage in self._delivered_notifications:
            if self._pending_notification == stage:
                self._pending_notification = None
            return False, False
        if self._quiet_now(wall):
            self._delivered_notifications.add(stage)
            self._pending_notification = None
            return True, False
        newly_queued = self._pending_notification != stage
        if self._pending_notification != stage:
            self._pending_notification = stage
            self._next_activation_attempt_at = now
        return False, newly_queued

    def _activate_pending(self, now: float, wall: datetime) -> tuple[bool, bool]:
        """Return ``(activated, discarded)`` for the current pending threshold."""

        stage = self._pending_notification
        if stage is None or now < self._next_activation_attempt_at:
            return False, False
        if not self._panel_available(now):
            return False, False
        if self._quiet_now(wall):
            self._delivered_notifications.add(stage)
            self._pending_notification = None
            return False, True
        try:
            outcome: ActivationOutcome = self.panel.activate_app(
                APP_NAME,
                priority=MutationPriority.NEXT_UP,
                fast=True,
                at=wall,
            )
        except (PanelClientError, OSError, RuntimeError) as error:
            failed_at = self.clock.monotonic()
            self._panel_failed(failed_at, "activation", error)
            self._next_activation_attempt_at = self._next_panel_attempt_at
            return False, False

        if outcome.sent:
            completed = self.clock.monotonic()
            self._panel_succeeded(completed)
            self._delivered_notifications.add(stage)
            self._pending_notification = None
            return True, False
        if outcome.reason == "quiet-hours":
            self._delivered_notifications.add(stage)
            self._pending_notification = None
            return False, True
        # Cooldown is expected and must not count as a panel failure.  Any other
        # non-sent transport outcome is also retried gently rather than spun.
        delay = max(
            float(getattr(outcome, "retry_after_seconds", 0.0) or 0.0),
            float(self.policy.panel_backoff_seconds[0]),
        )
        self._next_activation_attempt_at = self.clock.monotonic() + delay
        return False, False

    def tick(self) -> NextUpTick:
        now = self.clock.monotonic()
        wall = _as_local_wall(self.clock.wall_now(), self.timezone)
        refreshed, ready = self._refresh_sources(now, wall)

        event = select_event(self._combined_events(), wall)
        stage, minutes = classify_event(event, wall)
        selected_identity = None if event is None else event.identity
        event_changed = selected_identity != self._selected_identity
        previous_stage = self._last_stage
        self._selected_identity = selected_identity
        self._last_stage = stage

        discarded, notification_queued = self._prepare_notification(
            event,
            stage,
            now=now,
            wall=wall,
        )
        new_notification = (
            stage if notification_queued else None
        )
        decision = NextUpDecision(
            event=event,
            stage=stage,
            minutes_to_start=minutes,
            notification=new_notification,
            identity_changed=event_changed and event is not None,
        )

        if not decision.visible:
            self._pending_notification = None
            delete_checked = False
            deleted = False
            deferred = False
            if self._remote_present:
                if self._panel_available(now):
                    delete_checked, deleted = self._delete(now)
                else:
                    deferred = True
            return NextUpTick(
                stage,
                minutes,
                False,
                event_changed=event_changed,
                refreshed_sources=refreshed,
                ready_sources=ready,
                activation_discarded=discarded,
                activation_pending=False,
                delete_checked=delete_checked,
                deleted=deleted,
                panel_deferred=deferred,
                decision=decision,
            )

        if event is None:  # Defensive: classify_event cannot make None visible.
            raise AssertionError("visible Next Up decision requires an event")

        signature = (event.identity, stage)
        keepalive_due = (
            self._last_push_at is None
            or now - self._last_push_at >= self.policy.keepalive_seconds
        )
        push_due = (
            not self._remote_present
            or self._last_pushed_signature != signature
            or keepalive_due
            or previous_stage != stage
        )
        pushed = False
        deferred = False
        if push_due:
            if self._panel_available(now):
                pushed = self._push(event, stage, minutes, now)
            else:
                deferred = True

        # Never activate a stale prior event/stage: the matching page must have
        # reached the panel successfully first.
        activated = False
        activation_discarded = discarded
        if self._last_pushed_signature == signature:
            activated, discarded_now = self._activate_pending(
                self.clock.monotonic(), wall
            )
            activation_discarded = activation_discarded or discarded_now
        elif self._pending_notification is not None:
            deferred = deferred or not self._panel_available(self.clock.monotonic())

        return NextUpTick(
            stage,
            minutes,
            True,
            event_changed=event_changed,
            refreshed_sources=refreshed,
            ready_sources=ready,
            pushed=pushed,
            activated=activated,
            activation_discarded=activation_discarded,
            activation_pending=self._pending_notification is not None,
            panel_deferred=deferred,
            decision=decision,
        )


__all__ = [
    "APP_NAME",
    "CalendarBinding",
    "CalendarLoader",
    "ControllerClock",
    "DEFAULT_KEEPALIVE_SECONDS",
    "DEFAULT_LIFETIME_SECONDS",
    "DEFAULT_MAX_STALE_SECONDS",
    "DEFAULT_PANEL_BACKOFF_SECONDS",
    "DEFAULT_REFRESH_SECONDS",
    "NextUpController",
    "NextUpPolicy",
    "NextUpTick",
    "SystemControllerClock",
    "build_calendar_sources",
]
