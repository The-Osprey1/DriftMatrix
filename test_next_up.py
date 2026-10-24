from __future__ import annotations

from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from icalendar import Calendar

import next_up


LA = ZoneInfo("America/Los_Angeles")
UTC = ZoneInfo("UTC")


def calendar_bytes(*events: str) -> bytes:
    body = "\r\n".join(event.strip().replace("\n", "\r\n") for event in events)
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//AWTRIX Tests//Next Up//EN\r\n"
        f"{body}\r\n"
        "END:VCALENDAR\r\n"
    ).encode("utf-8")


def vevent(
    uid: str,
    start: str,
    end: str | None,
    summary: str = "Design review",
    *extra: str,
) -> str:
    fields = ["BEGIN:VEVENT"]
    if uid:
        fields.append(f"UID:{uid}")
    fields.append(start)
    if end:
        fields.append(end)
    fields.append(f"SUMMARY:{summary}")
    fields.extend(extra)
    fields.append("END:VEVENT")
    return "\n".join(fields)


def event_at(
    identity: str,
    start: datetime,
    *,
    minutes: int = 30,
    title: str | None = None,
) -> next_up.Event:
    end = (start.astimezone(UTC) + timedelta(minutes=minutes)).astimezone(
        start.tzinfo
    )
    return next_up.Event(
        identity=identity,
        uid=f"uid-{identity}",
        title=title or identity,
        start=start,
        end=end,
    )


class ParseAndExpansionTests(unittest.TestCase):
    def test_parse_requires_nonempty_bytes_and_calendar_root(self) -> None:
        with self.assertRaises(TypeError):
            next_up.parse_ics("BEGIN:VCALENDAR")  # type: ignore[arg-type]
        with self.assertRaises(next_up.CalendarParseError):
            next_up.parse_ics(b"")
        with self.assertRaises(next_up.CalendarParseError):
            next_up.parse_ics(b"definitely not iCalendar")
        self.assertIsInstance(
            next_up.parse_ics(calendar_bytes()),
            Calendar,
        )

    def test_aware_utc_times_are_normalized_to_los_angeles(self) -> None:
        raw = calendar_bytes(
            vevent(
                "utc-1",
                "DTSTART:20260821T190000Z",
                "DTEND:20260821T200000Z",
            )
        )
        events = next_up.events_from_ics(
            raw,
            datetime(2026, 8, 21, 11, 0, tzinfo=LA),
            source_id="work",
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].start, datetime(2026, 8, 21, 12, 0, tzinfo=LA))
        self.assertEqual(events[0].end, datetime(2026, 8, 21, 13, 0, tzinfo=LA))
        self.assertEqual(events[0].source_id, "work")

    def test_floating_timestamps_assume_configured_timezone(self) -> None:
        raw = calendar_bytes(
            vevent(
                "floating",
                "DTSTART:20260821T140000",
                "DTEND:20260821T143000",
            )
        )
        event = next_up.events_from_ics(
            raw, datetime(2026, 8, 21, 13, 0, tzinfo=LA)
        )[0]
        self.assertEqual(event.start, datetime(2026, 8, 21, 14, 0, tzinfo=LA))
        self.assertIs(event.start.tzinfo, LA)

    def test_naive_now_also_uses_configured_timezone(self) -> None:
        raw = calendar_bytes(
            vevent(
                "floating",
                "DTSTART:20260821T140000",
                "DTEND:20260821T143000",
            )
        )
        event = next_up.events_from_ics(raw, datetime(2026, 8, 21, 13, 0))[0]
        self.assertEqual(event.start.utcoffset(), timedelta(hours=-7))

    def test_duration_and_instantaneous_events_are_supported(self) -> None:
        raw = calendar_bytes(
            vevent(
                "duration",
                "DTSTART;TZID=America/Los_Angeles:20260821T140000",
                None,
                "Timed block",
                "DURATION:PT45M",
            ),
            vevent(
                "instant",
                "DTSTART;TZID=America/Los_Angeles:20260821T150000",
                None,
                "Reminder",
            ),
        )
        events = next_up.events_from_ics(
            raw, datetime(2026, 8, 21, 13, 0, tzinfo=LA)
        )
        self.assertEqual(events[0].end - events[0].start, timedelta(minutes=45))
        self.assertEqual(events[1].end, events[1].start)

    def test_short_duration_across_dst_gap_has_real_local_end(self) -> None:
        raw = calendar_bytes(
            vevent(
                "gap-duration",
                "DTSTART;TZID=America/Los_Angeles:20260308T015000",
                None,
                "Gap",
                "DURATION:PT30M",
            )
        )
        event = next_up.events_from_ics(
            raw, datetime(2026, 3, 8, 1, 0, tzinfo=LA)
        )[0]
        self.assertEqual(event.start, datetime(2026, 3, 8, 1, 50, tzinfo=LA))
        self.assertEqual(event.end, datetime(2026, 3, 8, 3, 20, tzinfo=LA))
        self.assertEqual(
            event.end.astimezone(UTC) - event.start.astimezone(UTC),
            timedelta(minutes=30),
        )

    def test_all_day_events_are_ignored_by_default_and_optional(self) -> None:
        raw = calendar_bytes(
            vevent(
                "holiday",
                "DTSTART;VALUE=DATE:20260822",
                "DTEND;VALUE=DATE:20260823",
                "Holiday",
            )
        )
        now = datetime(2026, 8, 21, 8, 0, tzinfo=LA)
        self.assertEqual(next_up.events_from_ics(raw, now), ())
        included = next_up.events_from_ics(raw, now, include_all_day=True)
        self.assertEqual(len(included), 1)
        self.assertTrue(included[0].all_day)
        self.assertEqual(included[0].start, datetime(2026, 8, 22, 0, 0, tzinfo=LA))

    def test_cancelled_and_transparent_events_are_ignored(self) -> None:
        raw = calendar_bytes(
            vevent(
                "cancelled",
                "DTSTART;TZID=America/Los_Angeles:20260821T140000",
                "DTEND;TZID=America/Los_Angeles:20260821T143000",
                "Cancelled",
                "STATUS:CANCELLED",
            ),
            vevent(
                "free",
                "DTSTART;TZID=America/Los_Angeles:20260821T150000",
                "DTEND;TZID=America/Los_Angeles:20260821T153000",
                "Free",
                "TRANSP:TRANSPARENT",
            ),
            vevent(
                "real",
                "DTSTART;TZID=America/Los_Angeles:20260821T160000",
                "DTEND;TZID=America/Los_Angeles:20260821T163000",
                "Real",
                "STATUS:CONFIRMED",
                "TRANSP:OPAQUE",
            ),
        )
        events = next_up.events_from_ics(
            raw, datetime(2026, 8, 21, 13, 0, tzinfo=LA)
        )
        self.assertEqual([event.uid for event in events], ["real"])

    def test_horizon_is_24_hours_by_default_and_validated(self) -> None:
        now = datetime(2026, 8, 21, 12, 0, tzinfo=LA)
        raw = calendar_bytes(
            vevent(
                "inside",
                "DTSTART;TZID=America/Los_Angeles:20260822T115900",
                "DTEND;TZID=America/Los_Angeles:20260822T120000",
            ),
            vevent(
                "outside",
                "DTSTART;TZID=America/Los_Angeles:20260822T120100",
                "DTEND;TZID=America/Los_Angeles:20260822T121000",
            ),
        )
        self.assertEqual(
            [event.uid for event in next_up.events_from_ics(raw, now)],
            ["inside"],
        )
        with self.assertRaises(ValueError):
            next_up.events_from_ics(raw, now, horizon=timedelta(0))

    def test_24_hour_horizon_is_elapsed_time_across_dst(self) -> None:
        # The spring transition makes 12:30 the next day only 23.5 elapsed
        # hours away.  It belongs inside a literal 24-hour lookahead.
        raw = calendar_bytes(
            vevent(
                "inside-after-gap",
                "DTSTART;TZID=America/Los_Angeles:20260308T123000",
                "DTEND;TZID=America/Los_Angeles:20260308T130000",
            )
        )
        events = next_up.events_from_ics(
            raw, datetime(2026, 3, 7, 12, 0, tzinfo=LA)
        )
        self.assertEqual([event.uid for event in events], ["inside-after-gap"])

    def test_events_older_than_current_grace_are_not_returned(self) -> None:
        raw = calendar_bytes(
            vevent(
                "old-long-meeting",
                "DTSTART;TZID=America/Los_Angeles:20260821T120000",
                "DTEND;TZID=America/Los_Angeles:20260821T160000",
            ),
            vevent(
                "just-started",
                "DTSTART;TZID=America/Los_Angeles:20260821T125500",
                "DTEND;TZID=America/Los_Angeles:20260821T140000",
            ),
        )
        events = next_up.events_from_ics(
            raw, datetime(2026, 8, 21, 13, 0, tzinfo=LA)
        )
        self.assertEqual([event.uid for event in events], ["just-started"])


class RecurrenceTests(unittest.TestCase):
    def test_rrule_exdate_override_and_cancellation_are_respected(self) -> None:
        master = vevent(
            "daily",
            "DTSTART;TZID=America/Los_Angeles:20260821T100000",
            "DTEND;TZID=America/Los_Angeles:20260821T110000",
            "Daily standup",
            "RRULE:FREQ=DAILY;COUNT=5",
            "EXDATE;TZID=America/Los_Angeles:20260822T100000",
        )
        moved = vevent(
            "daily",
            "DTSTART;TZID=America/Los_Angeles:20260823T120000",
            "DTEND;TZID=America/Los_Angeles:20260823T130000",
            "Moved standup",
            "RECURRENCE-ID;TZID=America/Los_Angeles:20260823T100000",
        )
        cancelled = vevent(
            "daily",
            "DTSTART;TZID=America/Los_Angeles:20260824T100000",
            "DTEND;TZID=America/Los_Angeles:20260824T110000",
            "Daily standup",
            "RECURRENCE-ID;TZID=America/Los_Angeles:20260824T100000",
            "STATUS:CANCELLED",
        )
        events = next_up.events_from_ics(
            calendar_bytes(master, moved, cancelled),
            datetime(2026, 8, 21, 8, 0, tzinfo=LA),
            horizon=timedelta(days=5),
        )
        self.assertEqual(
            [(event.start.day, event.start.hour, event.title) for event in events],
            [
                (21, 10, "Daily standup"),
                (23, 12, "Moved standup"),
                (25, 10, "Daily standup"),
            ],
        )

    def test_override_move_keeps_original_occurrence_identity(self) -> None:
        master = vevent(
            "daily",
            "DTSTART;TZID=America/Los_Angeles:20260821T100000",
            "DTEND;TZID=America/Los_Angeles:20260821T110000",
            "Daily",
            "RRULE:FREQ=DAILY;COUNT=3",
        )
        moved = vevent(
            "daily",
            "DTSTART;TZID=America/Los_Angeles:20260822T120000",
            "DTEND;TZID=America/Los_Angeles:20260822T130000",
            "Renamed",
            "RECURRENCE-ID;TZID=America/Los_Angeles:20260822T100000",
        )
        now = datetime(2026, 8, 21, 8, 0, tzinfo=LA)
        base = next_up.events_from_ics(
            calendar_bytes(master), now, horizon=timedelta(days=3), source_id="work"
        )
        overridden = next_up.events_from_ics(
            calendar_bytes(master, moved),
            now,
            horizon=timedelta(days=3),
            source_id="work",
        )
        base_second = next(event for event in base if event.start.day == 22)
        moved_second = next(event for event in overridden if event.start.day == 22)
        self.assertEqual(moved_second.start.hour, 12)
        self.assertEqual(moved_second.title, "Renamed")
        self.assertEqual(moved_second.identity, base_second.identity)

    def test_terse_cancelled_override_suppresses_slot_without_mutation(self) -> None:
        master = vevent(
            "daily",
            "DTSTART;TZID=America/Los_Angeles:20260821T100000",
            "DTEND;TZID=America/Los_Angeles:20260821T110000",
            "Daily",
            "RRULE:FREQ=DAILY;COUNT=3",
        )
        # Several providers omit DTSTART/DTEND on a cancellation exception and
        # identify the removed occurrence only by RECURRENCE-ID.
        cancelled = "\n".join(
            (
                "BEGIN:VEVENT",
                "UID:daily",
                "RECURRENCE-ID;TZID=America/Los_Angeles:20260822T100000",
                "STATUS:CANCELLED",
                "SUMMARY:Daily",
                "END:VEVENT",
            )
        )
        calendar = next_up.parse_ics(calendar_bytes(master, cancelled))
        cancellation_component = next(
            component
            for component in calendar.walk("VEVENT")
            if component.get("STATUS") is not None
        )
        self.assertIsNone(cancellation_component.get("DTSTART"))

        events = next_up.expand_occurrences(
            calendar,
            datetime(2026, 8, 21, 8, 0, tzinfo=LA),
            horizon=timedelta(days=3),
        )
        self.assertEqual([event.start.day for event in events], [21, 23])
        self.assertIsNone(cancellation_component.get("DTSTART"))

    def test_source_id_namespaces_otherwise_identical_events(self) -> None:
        raw = calendar_bytes(
            vevent(
                "shared-uid",
                "DTSTART;TZID=America/Los_Angeles:20260821T140000",
                "DTEND;TZID=America/Los_Angeles:20260821T143000",
            )
        )
        now = datetime(2026, 8, 21, 13, 0, tzinfo=LA)
        work = next_up.events_from_ics(raw, now, source_id="work")[0]
        home = next_up.events_from_ics(raw, now, source_id="home")[0]
        self.assertNotEqual(work.identity, home.identity)

    def test_missing_uid_fallback_identity_is_deterministic(self) -> None:
        raw = calendar_bytes(
            vevent(
                "",
                "DTSTART;TZID=America/Los_Angeles:20260821T140000",
                "DTEND;TZID=America/Los_Angeles:20260821T143000",
                "No UID",
            )
        )
        now = datetime(2026, 8, 21, 13, 0, tzinfo=LA)
        first = next_up.events_from_ics(raw, now)[0]
        second = next_up.events_from_ics(raw, now)[0]
        self.assertEqual(first.identity, second.identity)
        self.assertEqual(len(first.identity), 24)

    def test_spring_dst_keeps_local_wall_clock_time(self) -> None:
        raw = calendar_bytes(
            vevent(
                "dst-spring",
                "DTSTART;TZID=America/Los_Angeles:20260307T013000",
                "DTEND;TZID=America/Los_Angeles:20260307T020000",
                "Early",
                "RRULE:FREQ=DAILY;COUNT=3",
            )
        )
        events = next_up.events_from_ics(
            raw,
            datetime(2026, 3, 7, 0, 0, tzinfo=LA),
            horizon=timedelta(days=3),
        )
        self.assertEqual([event.start.hour for event in events], [1, 1, 1])
        self.assertEqual(
            [event.start.utcoffset() for event in events],
            [timedelta(hours=-8), timedelta(hours=-8), timedelta(hours=-7)],
        )

    def test_fall_dst_keeps_local_wall_clock_and_changes_offset(self) -> None:
        raw = calendar_bytes(
            vevent(
                "dst-fall",
                "DTSTART;TZID=America/Los_Angeles:20261031T013000",
                "DTEND;TZID=America/Los_Angeles:20261031T020000",
                "Early",
                "RRULE:FREQ=DAILY;COUNT=3",
            )
        )
        events = next_up.events_from_ics(
            raw,
            datetime(2026, 10, 31, 0, 0, tzinfo=LA),
            horizon=timedelta(days=3),
        )
        self.assertEqual([event.start.hour for event in events], [1, 1, 1])
        self.assertEqual(events[0].start.utcoffset(), timedelta(hours=-7))
        self.assertEqual(events[-1].start.utcoffset(), timedelta(hours=-8))


class SelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 21, 13, 0, tzinfo=LA)

    def test_current_event_beats_upcoming_event(self) -> None:
        current = event_at("current", self.now - timedelta(minutes=4))
        upcoming = event_at("upcoming", self.now + timedelta(minutes=1))
        self.assertIs(next_up.select_event([upcoming, current], self.now), current)

    def test_overlapping_current_events_choose_most_recent_start(self) -> None:
        older = event_at("older", self.now - timedelta(minutes=8), minutes=60)
        newer = event_at("newer", self.now - timedelta(minutes=2), minutes=60)
        self.assertIs(next_up.select_event([older, newer], self.now), newer)

    def test_current_tie_uses_earliest_end_then_identity(self) -> None:
        start = self.now - timedelta(minutes=2)
        long = event_at("a", start, minutes=60)
        short_b = event_at("b", start, minutes=20)
        short_c = event_at("c", start, minutes=20)
        self.assertIs(
            next_up.select_event([long, short_c, short_b], self.now), short_b
        )

    def test_future_events_choose_start_then_end_then_identity(self) -> None:
        start = self.now + timedelta(minutes=20)
        later = event_at("later", start + timedelta(minutes=1), minutes=1)
        long = event_at("long", start, minutes=60)
        short_b = event_at("b", start, minutes=20)
        short_c = event_at("c", start, minutes=20)
        self.assertIs(
            next_up.select_event([later, long, short_c, short_b], self.now), short_b
        )

    def test_event_expires_ten_minutes_after_start(self) -> None:
        expired = event_at("expired", self.now - timedelta(minutes=10, seconds=1))
        future = event_at("future", self.now + timedelta(minutes=30))
        self.assertIs(next_up.select_event([expired, future], self.now), future)
        exact = event_at("exact", self.now - timedelta(minutes=10))
        self.assertIs(next_up.select_event([exact, future], self.now), exact)

    def test_empty_or_only_expired_returns_none(self) -> None:
        self.assertIsNone(next_up.select_event([], self.now))
        self.assertIsNone(
            next_up.select_event(
                [event_at("old", self.now - timedelta(hours=1))], self.now
            )
        )


class ClassificationAndNotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 21, 13, 0, tzinfo=LA)

    def stage_at(self, delta: timedelta) -> tuple[next_up.EventStage, int | None]:
        event = event_at("event", self.now + delta)
        return next_up.classify_event(event, self.now)

    def test_stage_boundaries_and_ceiling_minutes(self) -> None:
        self.assertEqual(
            self.stage_at(timedelta(minutes=120, seconds=1)),
            (next_up.EventStage.ABSENT, 121),
        )
        self.assertEqual(
            self.stage_at(timedelta(minutes=120)),
            (next_up.EventStage.NORMAL, 120),
        )
        self.assertEqual(
            self.stage_at(timedelta(minutes=15, seconds=1)),
            (next_up.EventStage.NORMAL, 16),
        )
        self.assertEqual(
            self.stage_at(timedelta(minutes=15)),
            (next_up.EventStage.FIFTEEN, 15),
        )
        self.assertEqual(
            self.stage_at(timedelta(minutes=5, seconds=1)),
            (next_up.EventStage.FIFTEEN, 6),
        )
        self.assertEqual(
            self.stage_at(timedelta(minutes=5)),
            (next_up.EventStage.FIVE, 5),
        )
        self.assertEqual(
            self.stage_at(timedelta(seconds=1)),
            (next_up.EventStage.FIVE, 1),
        )
        self.assertEqual(
            self.stage_at(timedelta(0)),
            (next_up.EventStage.NOW, 0),
        )
        self.assertEqual(
            self.stage_at(-timedelta(minutes=10)),
            (next_up.EventStage.NOW, 0),
        )
        self.assertEqual(
            self.stage_at(-timedelta(minutes=10, seconds=1)),
            (next_up.EventStage.ABSENT, 0),
        )

    def test_countdown_uses_elapsed_time_across_spring_dst_gap(self) -> None:
        now = datetime(2026, 3, 8, 1, 50, tzinfo=LA)
        start = datetime(2026, 3, 8, 3, 0, tzinfo=LA)
        event = event_at("after-gap", start)
        self.assertEqual(
            next_up.classify_event(event, now),
            (next_up.EventStage.FIFTEEN, 10),
        )

    def test_selection_uses_elapsed_time_across_fall_dst_fold(self) -> None:
        # 01:10 PST (fold 1) is twenty minutes after 01:50 PDT (fold 0),
        # even though its wall-clock fields look earlier.
        now = datetime(2026, 11, 1, 1, 50, tzinfo=LA, fold=0)
        start = datetime(2026, 11, 1, 1, 10, tzinfo=LA, fold=1)
        event = event_at("after-fold", start)
        self.assertIs(next_up.select_event([event], now), event)
        self.assertEqual(
            next_up.classify_event(event, now),
            (next_up.EventStage.NORMAL, 20),
        )

    def test_absent_decision_has_no_eta_and_is_not_visible(self) -> None:
        tracker = next_up.NotificationTracker()
        decision = tracker.update(None, self.now)
        self.assertEqual(decision.stage, next_up.EventStage.ABSENT)
        self.assertIsNone(decision.minutes_to_start)
        self.assertFalse(decision.visible)
        self.assertFalse(decision.should_activate)

    def test_each_threshold_notifies_only_once(self) -> None:
        tracker = next_up.NotificationTracker()
        event = event_at("meeting", self.now + timedelta(minutes=20))

        normal = tracker.update(event, self.now)
        self.assertTrue(normal.identity_changed)
        self.assertIsNone(normal.notification)

        at_fifteen = tracker.update(event, self.now + timedelta(minutes=5))
        self.assertEqual(at_fifteen.notification, next_up.EventStage.FIFTEEN)
        self.assertTrue(at_fifteen.should_activate)
        self.assertIsNone(
            tracker.update(event, self.now + timedelta(minutes=6)).notification
        )

        at_five = tracker.update(event, self.now + timedelta(minutes=15))
        self.assertEqual(at_five.notification, next_up.EventStage.FIVE)
        self.assertIsNone(
            tracker.update(event, self.now + timedelta(minutes=16)).notification
        )

        at_now = tracker.update(event, self.now + timedelta(minutes=20))
        self.assertEqual(at_now.notification, next_up.EventStage.NOW)
        self.assertIsNone(
            tracker.update(event, self.now + timedelta(minutes=21)).notification
        )

    def test_starting_inside_a_threshold_emits_only_current_threshold(self) -> None:
        tracker = next_up.NotificationTracker()
        event = event_at("soon", self.now + timedelta(minutes=4))
        first = tracker.update(event, self.now)
        self.assertEqual(first.notification, next_up.EventStage.FIVE)
        self.assertNotIn(next_up.EventStage.FIFTEEN, tracker._emitted)

    def test_new_identity_resets_notification_memory(self) -> None:
        tracker = next_up.NotificationTracker()
        first = event_at("first", self.now + timedelta(minutes=5))
        second = event_at("second", self.now + timedelta(minutes=5))
        self.assertEqual(
            tracker.update(first, self.now).notification, next_up.EventStage.FIVE
        )
        decision = tracker.update(second, self.now)
        self.assertTrue(decision.identity_changed)
        self.assertEqual(decision.notification, next_up.EventStage.FIVE)

    def test_same_identity_with_updated_title_does_not_reset(self) -> None:
        tracker = next_up.NotificationTracker()
        first = event_at(
            "stable", self.now + timedelta(minutes=5), title="Old title"
        )
        updated = event_at(
            "stable", self.now + timedelta(minutes=5), title="New title"
        )
        tracker.update(first, self.now)
        decision = tracker.update(updated, self.now)
        self.assertFalse(decision.identity_changed)
        self.assertIsNone(decision.notification)

    def test_temporary_empty_refresh_does_not_replay_notification(self) -> None:
        tracker = next_up.NotificationTracker()
        event = event_at("stable", self.now + timedelta(minutes=5))
        tracker.update(event, self.now)
        tracker.update(None, self.now)
        decision = tracker.update(event, self.now)
        self.assertFalse(decision.identity_changed)
        self.assertIsNone(decision.notification)

    def test_evaluate_combines_selection_and_tracking(self) -> None:
        tracker = next_up.NotificationTracker()
        later = event_at("later", self.now + timedelta(minutes=10))
        sooner = event_at("sooner", self.now + timedelta(minutes=5))
        decision = tracker.evaluate([later, sooner], self.now)
        self.assertIs(decision.event, sooner)
        self.assertEqual(decision.notification, next_up.EventStage.FIVE)


class LabelTests(unittest.TestCase):
    def test_aliases_are_exact_case_and_whitespace_insensitive(self) -> None:
        aliases = (("Weekly Design Review", "Design Sync"),)
        self.assertEqual(
            next_up.apply_alias("  WEEKLY   design review ", aliases),
            "Design Sync",
        )
        self.assertEqual(
            next_up.apply_alias("Weekly Design Review Extra", aliases),
            "Weekly Design Review Extra",
        )

    def test_conflicting_normalized_aliases_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            next_up.normalize_aliases(
                (("Design Review", "SYNC"), (" design   review ", "REVIEW"))
            )

    def test_compact_label_sanitizes_unicode_controls_and_punctuation(self) -> None:
        label = next_up.safe_compact_label("  Démo\n— team\x00 “A”  ", max_chars=22)
        self.assertEqual(label, 'DEMO - TEAM "A"')

    def test_alias_is_applied_before_bounded_truncation(self) -> None:
        aliases = {"Extremely Long Weekly Planning Meeting": "PLAN"}
        self.assertEqual(
            next_up.safe_compact_label(
                "Extremely Long Weekly Planning Meeting", aliases, max_chars=8
            ),
            "PLAN",
        )
        self.assertEqual(
            next_up.safe_compact_label("Project planning session", max_chars=12),
            "PROJECT...",
        )

    def test_label_length_validation_and_tiny_budgets(self) -> None:
        self.assertEqual(next_up.safe_compact_label("ABCDE", max_chars=3), "ABC")
        with self.assertRaises(ValueError):
            next_up.safe_compact_label("A", max_chars=0)
        with self.assertRaises(TypeError):
            next_up.safe_compact_label("A", max_chars=True)  # type: ignore[arg-type]

    def test_event_compact_label_convenience_method(self) -> None:
        start = datetime(2026, 8, 21, 13, 0, tzinfo=UTC)
        event = event_at("id", start, title="One on one with Devyn")
        self.assertEqual(
            event.compact_label({"One on one with Devyn": "1:1"}),
            "1:1",
        )


if __name__ == "__main__":
    unittest.main()
