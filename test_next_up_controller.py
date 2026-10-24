from __future__ import annotations

from datetime import datetime, time as wall_time, timedelta
import logging
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from zoneinfo import ZoneInfo

from calendar_source import CalendarLoadResult, CalendarStatus
from next_up import Event, EventStage
import next_up_controller as controller
from panel_client import (
    ActivationOutcome,
    MutationPriority,
    PanelPolicy,
    PanelTransportError,
)


PACIFIC = ZoneInfo("America/Los_Angeles")
BASE = datetime(2026, 8, 21, 12, 0, tzinfo=PACIFIC)


class FakeClock:
    def __init__(self, wall: datetime = BASE) -> None:
        self.mono = 0.0
        self.wall = wall

    def monotonic(self) -> float:
        return self.mono

    def wall_now(self) -> datetime:
        return self.wall

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += timedelta(seconds=seconds)


def load_result(
    body: bytes | None,
    *,
    ready: bool = True,
    validated_at: float | None = None,
) -> CalendarLoadResult:
    status = CalendarStatus.READY if ready else CalendarStatus.UNAVAILABLE
    metadata = (
        None
        if validated_at is None
        else SimpleNamespace(validated_at=float(validated_at))
    )
    return CalendarLoadResult(status=status, body=body, metadata=metadata)


class FakeLoader:
    def __init__(self, *results: CalendarLoadResult) -> None:
        self.results = list(results)
        self.calls = 0
        self.last = results[-1] if results else load_result(None, ready=False)

    def load(self, *, force: bool = False) -> CalendarLoadResult:
        self.calls += 1
        if self.results:
            self.last = self.results.pop(0)
        return self.last


class RaisingLoader:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def load(self, *, force: bool = False) -> CalendarLoadResult:
        self.calls += 1
        raise self.error


class FakeParser:
    def __init__(self, values: dict[bytes, tuple[Event, ...] | Exception]) -> None:
        self.values = values
        self.calls: list[tuple[bytes, datetime, str]] = []

    def __call__(
        self,
        data: bytes,
        now: datetime,
        *,
        timezone: ZoneInfo,
        include_all_day: bool,
        source_id: str,
    ) -> tuple[Event, ...]:
        self.calls.append((data, now, source_id))
        value = self.values[data]
        if isinstance(value, Exception):
            raise value
        return value


class FakePanel:
    def __init__(self, *, policy: PanelPolicy | None = None) -> None:
        self.policy = policy or PanelPolicy(
            minimum_mutation_interval_seconds=0,
            activation_cooldown_seconds=0,
            quiet_start=None,
            quiet_end=None,
        )
        self.pushes: list[tuple[str, dict, dict | None]] = []
        self.activations: list[tuple[str, MutationPriority, bool, datetime | None]] = []
        self.deletes: list[str] = []
        self.push_effects: list[Exception | None] = []
        self.activation_effects: list[ActivationOutcome | Exception] = []
        self.delete_effects: list[bool | Exception] = []

    def push_app(self, name: str, payload: dict, *, static_payload: dict | None = None):
        if self.push_effects:
            effect = self.push_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
        self.pushes.append((name, payload, static_payload))
        return object()

    def activate_app(
        self,
        name: str,
        *,
        priority: MutationPriority,
        fast: bool,
        at: datetime | None = None,
    ) -> ActivationOutcome:
        self.activations.append((name, priority, fast, at))
        if self.activation_effects:
            effect = self.activation_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
            return effect
        return ActivationOutcome(True, "sent", priority)

    def delete_pushed_app(self, name: str) -> bool:
        self.deletes.append(name)
        if self.delete_effects:
            effect = self.delete_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
            return effect
        return True


def event_at(
    minutes: float,
    *,
    identity: str = "event-1",
    title: str = "Team stand-up",
    base: datetime = BASE,
) -> Event:
    start = base + timedelta(minutes=minutes)
    return Event(
        identity=identity,
        uid=f"uid-{identity}",
        title=title,
        start=start,
        end=start + timedelta(minutes=30),
        source_id="test-source",
    )


def fake_payload_builder(event=None, **kwargs):
    return {
        "animated": kwargs["animated"],
        "title": kwargs["title"],
        "minutes": kwargs["minutes_until"],
        "urgency": kwargs["urgency"].value,
        "durationMs": kwargs["duration_ms"],
        "lifetimeMs": kwargs["lifetime_ms"],
    }


class CalendarBindingTests(unittest.TestCase):
    def test_builder_gives_even_duplicate_sources_distinct_private_caches(self) -> None:
        secret = "https://calendar.invalid/private-token/basic.ics?key=secret"
        with tempfile.TemporaryDirectory() as folder:
            bindings = controller.build_calendar_sources(
                [secret, secret], runtime_dir=Path(folder)
            )
            cache_paths = [binding.loader.config.cache_path for binding in bindings]
            self.assertEqual(len(cache_paths), len(set(cache_paths)))
            self.assertNotEqual(bindings[0].source_id, bindings[1].source_id)
            self.assertTrue(all(Path(folder) in path.parents for path in cache_paths))
            self.assertNotIn("private-token", repr(bindings))
            self.assertNotIn("key=secret", repr(bindings))

    def test_builder_validates_count_and_empty_sources(self) -> None:
        with self.assertRaises(ValueError):
            controller.build_calendar_sources([""])
        with self.assertRaises(ValueError):
            controller.build_calendar_sources([Path(f"{index}.ics") for index in range(17)])

    def test_source_identity_and_cache_are_stable_across_list_reordering(self) -> None:
        sources = ["https://one.invalid/calendar.ics", "https://two.invalid/calendar.ics"]
        with tempfile.TemporaryDirectory() as folder:
            forward = controller.build_calendar_sources(sources, runtime_dir=Path(folder))
            reverse = controller.build_calendar_sources(
                list(reversed(sources)), runtime_dir=Path(folder)
            )
            forward_by_source = {
                binding.loader.config.source: (
                    binding.source_id,
                    binding.loader.config.cache_path,
                )
                for binding in forward
            }
            reverse_by_source = {
                binding.loader.config.source: (
                    binding.source_id,
                    binding.loader.config.cache_path,
                )
                for binding in reverse
            }
            self.assertEqual(forward_by_source, reverse_by_source)

    def test_binding_repr_hides_loader(self) -> None:
        loader = FakeLoader(load_result(b"secret"))
        binding = controller.CalendarBinding(loader, "calendar-deadbeef")
        self.assertNotIn("secret", repr(binding))
        self.assertIn("<redacted>", repr(binding))

    def test_controller_rejects_manual_source_cache_collisions(self) -> None:
        first = FakeLoader(load_result(None, ready=False))
        second = FakeLoader(load_result(None, ready=False))
        first.config = SimpleNamespace(cache_path=Path("shared-cache.json"))
        second.config = SimpleNamespace(cache_path=Path("shared-cache.json"))
        with self.assertRaisesRegex(ValueError, "cache paths must be unique"):
            controller.NextUpController(
                [
                    controller.CalendarBinding(first, "calendar-one"),
                    controller.CalendarBinding(second, "calendar-two"),
                ],
                FakePanel(),
            )


class NextUpControllerTests(unittest.TestCase):
    def make_controller(
        self,
        loader: FakeLoader | RaisingLoader,
        parser: FakeParser,
        *,
        clock: FakeClock | None = None,
        panel: FakePanel | None = None,
        refresh: float = 300,
        keepalive: float = 60,
        max_stale: float = 21_600,
        aliases=(),
        logger: logging.Logger | None = None,
    ) -> tuple[controller.NextUpController, FakeClock, FakePanel]:
        shared_clock = clock or FakeClock()
        shared_panel = panel or FakePanel()
        binding = controller.CalendarBinding(loader, "calendar-one", max_stale)
        service = controller.NextUpController(
            [binding],
            shared_panel,
            timezone=PACIFIC,
            aliases=aliases,
            policy=controller.NextUpPolicy(
                refresh_seconds=refresh,
                keepalive_seconds=keepalive,
                lifetime_seconds=180,
                panel_backoff_seconds=(1, 2, 4, 8, 16, 30),
            ),
            clock=shared_clock,
            parser=parser,
            payload_builder=fake_payload_builder,
            logger=logger,
        )
        return service, shared_clock, shared_panel

    def test_an_unavailable_source_says_so(self) -> None:
        """A calendar that stops loading must not look like an empty day.

        A loader that *raises* was always logged. A loader that fails the way
        it is designed to -- returning a result whose `ready` is False -- fell
        through in complete silence, so an expired link or a calendar the
        server stopped serving was indistinguishable from a free afternoon.
        The graceful path was the invisible one.
        """
        logger = logging.getLogger("test.nextup.unavailable")
        logger.handlers.clear()
        records: list[str] = []

        class Collector(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger.addHandler(Collector())
        logger.setLevel(logging.WARNING)
        logger.propagate = False

        loader = FakeLoader(load_result(None, ready=False))
        service, _, _ = self.make_controller(loader, FakeParser({}), logger=logger)
        service.tick()

        self.assertTrue(records, "an unavailable calendar was suppressed silently")
        self.assertIn("unavailable", records[0])

    def test_a_quiet_result_stays_quiet(self) -> None:
        """Some outcomes really are routine: an unconfigured source has no news."""
        logger = logging.getLogger("test.nextup.quiet")
        logger.handlers.clear()
        records: list[str] = []

        class Collector(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger.addHandler(Collector())
        logger.setLevel(logging.WARNING)
        logger.propagate = False

        quiet = CalendarLoadResult(status=CalendarStatus.NOT_CONFIGURED, quiet=True)
        service, _, _ = self.make_controller(FakeLoader(quiet), FakeParser({}), logger=logger)
        service.tick()

        self.assertEqual(records, [], "a deliberately quiet outcome was logged anyway")

    def test_only_ready_bytes_are_parsed(self) -> None:
        unavailable = load_result(b"must-not-parse", ready=False)
        loader = FakeLoader(unavailable)
        parser = FakeParser({b"must-not-parse": (event_at(30),)})
        service, _, panel = self.make_controller(loader, parser)
        result = service.tick()
        self.assertEqual(parser.calls, [])
        self.assertEqual(result.stage, EventStage.ABSENT)
        self.assertFalse(result.visible)
        self.assertEqual(panel.pushes, [])

    def test_each_source_fails_closed_without_hiding_other_sources(self) -> None:
        first = FakeLoader(load_result(b"one", validated_at=BASE.timestamp()))
        second = FakeLoader(load_result(b"bad", validated_at=BASE.timestamp()))
        parser = FakeParser(
            {
                b"one": (event_at(45, identity="good", title="GOOD"),),
                b"bad": ValueError("private broken title"),
            }
        )
        panel = FakePanel()
        service = controller.NextUpController(
            [
                controller.CalendarBinding(first, "calendar-one"),
                controller.CalendarBinding(second, "calendar-two"),
            ],
            panel,
            timezone=PACIFIC,
            clock=FakeClock(),
            parser=parser,
            payload_builder=fake_payload_builder,
        )
        with self.assertLogs("awtrix.utility_hub.next_up", logging.WARNING) as logs:
            result = service.tick()
        self.assertEqual(result.ready_sources, 1)
        self.assertEqual(panel.pushes[0][1]["title"], "GOOD")
        self.assertNotIn("private broken title", "\n".join(logs.output))

    def test_source_load_exception_is_suppressed_without_secret_text_in_log(self) -> None:
        loader = RaisingLoader(RuntimeError("https://private.invalid/token"))
        parser = FakeParser({})
        service, _, _ = self.make_controller(loader, parser)
        with self.assertLogs("awtrix.utility_hub.next_up", logging.WARNING) as logs:
            result = service.tick()
        self.assertFalse(result.visible)
        self.assertNotIn("private.invalid", "\n".join(logs.output))

    def test_wall_time_reclassifies_each_tick_without_source_refresh(self) -> None:
        item = event_at(16)
        loader = FakeLoader(load_result(b"calendar", validated_at=BASE.timestamp()))
        parser = FakeParser({b"calendar": (item,)})
        service, clock, panel = self.make_controller(loader, parser, refresh=300)

        first = service.tick()
        self.assertEqual(first.stage, EventStage.NORMAL)
        self.assertTrue(first.pushed)
        self.assertFalse(first.activated)

        clock.advance(60)
        second = service.tick()
        self.assertEqual(loader.calls, 1)
        self.assertEqual(second.stage, EventStage.FIFTEEN)
        self.assertTrue(second.pushed)
        self.assertTrue(second.activated)
        self.assertEqual(panel.activations[0][1], MutationPriority.NEXT_UP)

    def test_event_enters_at_exactly_120_minutes_without_reload(self) -> None:
        item = event_at(121)
        loader = FakeLoader(load_result(b"calendar", validated_at=BASE.timestamp()))
        parser = FakeParser({b"calendar": (item,)})
        service, clock, panel = self.make_controller(loader, parser, refresh=300)
        first = service.tick()
        self.assertFalse(first.visible)
        self.assertEqual(panel.pushes, [])

        clock.advance(60)
        second = service.tick()
        self.assertTrue(second.visible)
        self.assertEqual(second.minutes_to_start, 120)
        self.assertTrue(second.pushed)
        self.assertEqual(loader.calls, 1)

    def test_keepalive_is_sixty_seconds_and_lifetime_is_180_seconds(self) -> None:
        loader = FakeLoader(load_result(b"calendar", validated_at=BASE.timestamp()))
        parser = FakeParser({b"calendar": (event_at(30),)})
        service, clock, panel = self.make_controller(loader, parser)
        service.tick()
        clock.advance(59)
        self.assertFalse(service.tick().pushed)
        clock.advance(1)
        self.assertTrue(service.tick().pushed)
        self.assertEqual(len(panel.pushes), 2)
        animated = panel.pushes[-1][1]
        static = panel.pushes[-1][2]
        self.assertEqual(animated["lifetimeMs"], 180_000)
        self.assertEqual(static["lifetimeMs"], 180_000)
        self.assertTrue(animated["animated"])
        self.assertFalse(static["animated"])

    def test_alias_is_applied_before_payload_and_private_text_stays_out_of_repr(self) -> None:
        private_title = "Long Private Appointment"
        loader = FakeLoader(load_result(b"calendar", validated_at=BASE.timestamp()))
        parser = FakeParser({b"calendar": (event_at(30, title=private_title),)})
        service, _, panel = self.make_controller(
            loader,
            parser,
            aliases={private_title: "DENTIST"},
        )
        result = service.tick()
        self.assertEqual(panel.pushes[0][1]["title"], "DENTIST")
        self.assertNotIn(private_title, repr(service))
        self.assertNotIn(private_title, repr(result))

    def test_fifteen_five_and_now_activate_once_each(self) -> None:
        item = event_at(15)
        loader = FakeLoader(load_result(b"calendar", validated_at=BASE.timestamp()))
        parser = FakeParser({b"calendar": (item,)})
        service, clock, panel = self.make_controller(loader, parser, refresh=3600)

        first = service.tick()
        self.assertEqual(first.stage, EventStage.FIFTEEN)
        self.assertTrue(first.activated)
        self.assertFalse(service.tick().activated)

        clock.advance(10 * 60)
        five = service.tick()
        self.assertEqual(five.stage, EventStage.FIVE)
        self.assertTrue(five.activated)
        self.assertFalse(service.tick().activated)

        clock.advance(5 * 60)
        now = service.tick()
        self.assertEqual(now.stage, EventStage.NOW)
        self.assertTrue(now.activated)
        self.assertFalse(service.tick().activated)
        self.assertEqual(len(panel.activations), 3)

    def test_activation_cooldown_is_retried_without_duplicate_delivery(self) -> None:
        panel = FakePanel()
        panel.activation_effects = [
            ActivationOutcome(
                False,
                "cooldown",
                MutationPriority.NEXT_UP,
                retry_after_seconds=5,
            ),
            ActivationOutcome(True, "sent", MutationPriority.NEXT_UP),
        ]
        loader = FakeLoader(load_result(b"calendar", validated_at=BASE.timestamp()))
        parser = FakeParser({b"calendar": (event_at(15),)})
        service, clock, _ = self.make_controller(loader, parser, panel=panel)

        first = service.tick()
        self.assertFalse(first.activated)
        self.assertTrue(first.activation_pending)
        self.assertEqual(first.decision.notification, EventStage.FIFTEEN)
        clock.advance(4)
        waiting = service.tick()
        self.assertTrue(waiting.activation_pending)
        self.assertIsNone(waiting.decision.notification)
        self.assertEqual(len(panel.activations), 1)
        clock.advance(1)
        self.assertTrue(service.tick().activated)
        self.assertFalse(service.tick().activated)
        self.assertEqual(len(panel.activations), 2)

    def test_activation_transport_failure_retries_after_bounded_backoff(self) -> None:
        panel = FakePanel()
        panel.activation_effects = [
            PanelTransportError("private transport detail"),
            ActivationOutcome(True, "sent", MutationPriority.NEXT_UP),
        ]
        loader = FakeLoader(load_result(b"calendar", validated_at=BASE.timestamp()))
        parser = FakeParser({b"calendar": (event_at(15),)})
        service, clock, _ = self.make_controller(loader, parser, panel=panel)
        with self.assertLogs("awtrix.utility_hub.next_up", logging.WARNING) as logs:
            first = service.tick()
        self.assertTrue(first.activation_pending)
        self.assertNotIn("private transport detail", "\n".join(logs.output))
        clock.advance(0.9)
        self.assertFalse(service.tick().activated)
        clock.advance(0.1)
        self.assertTrue(service.tick().activated)
        self.assertEqual(len(panel.activations), 2)

    def test_quiet_hour_threshold_is_discarded_not_replayed_after_wakeup(self) -> None:
        clock = FakeClock(datetime(2026, 8, 22, 6, 59, tzinfo=PACIFIC))
        panel = FakePanel(
            policy=PanelPolicy(
                minimum_mutation_interval_seconds=0,
                activation_cooldown_seconds=0,
                quiet_start=wall_time(21, 30),
                quiet_end=wall_time(7, 0),
            )
        )
        item = event_at(5, base=clock.wall)
        loader = FakeLoader(load_result(b"calendar", validated_at=clock.wall.timestamp()))
        parser = FakeParser({b"calendar": (item,)})
        service, _, _ = self.make_controller(
            loader, parser, clock=clock, panel=panel, refresh=3600
        )
        first = service.tick()
        self.assertTrue(first.activation_discarded)
        self.assertFalse(first.activation_pending)
        self.assertEqual(panel.activations, [])

        clock.advance(60)
        second = service.tick()
        self.assertFalse(second.activation_discarded)
        self.assertFalse(second.activated)
        self.assertEqual(panel.activations, [])

    def test_source_expiration_before_next_refresh_fails_closed_and_deletes(self) -> None:
        item = event_at(30)
        loader = FakeLoader(load_result(b"calendar", validated_at=BASE.timestamp()))
        parser = FakeParser({b"calendar": (item,)})
        service, clock, panel = self.make_controller(
            loader, parser, refresh=300, max_stale=30
        )
        self.assertTrue(service.tick().pushed)
        clock.advance(31)
        expired = service.tick()
        self.assertFalse(expired.visible)
        self.assertTrue(expired.delete_checked)
        self.assertTrue(expired.deleted)
        self.assertEqual(loader.calls, 1)
        self.assertEqual(panel.deletes, [controller.APP_NAME])

    def test_unavailable_refresh_clears_prior_events_and_safely_deletes(self) -> None:
        loader = FakeLoader(
            load_result(b"calendar", validated_at=BASE.timestamp()),
            load_result(None, ready=False),
        )
        parser = FakeParser({b"calendar": (event_at(30),)})
        service, clock, panel = self.make_controller(loader, parser, refresh=10)
        service.tick()
        clock.advance(10)
        result = service.tick()
        self.assertFalse(result.visible)
        self.assertTrue(result.delete_checked)
        self.assertEqual(panel.deletes, [controller.APP_NAME])

    def test_startup_absence_never_deletes_unknown_remote_app(self) -> None:
        loader = FakeLoader(load_result(None, ready=False))
        parser = FakeParser({})
        service, _, panel = self.make_controller(loader, parser)
        result = service.tick()
        self.assertFalse(result.delete_checked)
        self.assertEqual(panel.deletes, [])

    def test_failed_push_does_not_authorize_later_delete(self) -> None:
        loader = FakeLoader(
            load_result(b"calendar", validated_at=BASE.timestamp()),
            load_result(None, ready=False),
        )
        parser = FakeParser({b"calendar": (event_at(30),)})
        panel = FakePanel()
        panel.push_effects = [PanelTransportError("offline")]
        service, clock, _ = self.make_controller(
            loader, parser, panel=panel, refresh=10
        )
        self.assertFalse(service.tick().pushed)
        clock.advance(10)
        service.tick()
        self.assertEqual(panel.deletes, [])

    def test_push_failure_uses_cross_tick_backoff_then_recovers(self) -> None:
        loader = FakeLoader(load_result(b"calendar", validated_at=BASE.timestamp()))
        parser = FakeParser({b"calendar": (event_at(30),)})
        panel = FakePanel()
        panel.push_effects = [PanelTransportError("offline"), None]
        service, clock, _ = self.make_controller(loader, parser, panel=panel)

        first = service.tick()
        self.assertFalse(first.pushed)
        self.assertEqual(service.panel_failures, 1)
        clock.advance(0.5)
        deferred = service.tick()
        self.assertTrue(deferred.panel_deferred)
        self.assertEqual(len(panel.pushes), 0)
        clock.advance(0.5)
        recovered = service.tick()
        self.assertTrue(recovered.pushed)
        self.assertEqual(service.panel_failures, 0)
        self.assertEqual(len(panel.pushes), 1)

    def test_parse_failure_replaces_prior_events_instead_of_retaining_them(self) -> None:
        loader = FakeLoader(
            load_result(b"good", validated_at=BASE.timestamp()),
            load_result(b"bad", validated_at=(BASE + timedelta(seconds=10)).timestamp()),
        )
        parser = FakeParser(
            {
                b"good": (event_at(30),),
                b"bad": ValueError("event title should remain private"),
            }
        )
        service, clock, panel = self.make_controller(loader, parser, refresh=10)
        service.tick()
        clock.advance(10)
        with self.assertLogs("awtrix.utility_hub.next_up", logging.WARNING):
            result = service.tick()
        self.assertFalse(result.visible)
        self.assertEqual(result.ready_sources, 0)
        self.assertEqual(panel.deletes, [controller.APP_NAME])

    def test_event_change_pushes_new_page_and_resets_notification_identity(self) -> None:
        first = event_at(15, identity="first", title="FIRST")
        second = event_at(14, identity="second", title="SECOND")
        loader = FakeLoader(
            load_result(b"first", validated_at=BASE.timestamp()),
            load_result(b"second", validated_at=(BASE + timedelta(seconds=10)).timestamp()),
        )
        parser = FakeParser({b"first": (first,), b"second": (second,)})
        service, clock, panel = self.make_controller(loader, parser, refresh=10)
        self.assertTrue(service.tick().activated)
        clock.advance(10)
        changed = service.tick()
        self.assertTrue(changed.event_changed)
        self.assertTrue(changed.pushed)
        self.assertTrue(changed.activated)
        self.assertEqual([entry[1]["title"] for entry in panel.pushes], ["FIRST", "SECOND"])
        self.assertEqual(len(panel.activations), 2)

    def test_temporary_empty_refresh_does_not_replay_same_event_notification(self) -> None:
        item = event_at(15)
        loader = FakeLoader(
            load_result(b"event", validated_at=BASE.timestamp()),
            load_result(None, ready=False),
            load_result(b"event", validated_at=(BASE + timedelta(seconds=20)).timestamp()),
        )
        parser = FakeParser({b"event": (item,)})
        service, clock, panel = self.make_controller(loader, parser, refresh=10)
        self.assertTrue(service.tick().activated)
        clock.advance(10)
        self.assertFalse(service.tick().visible)
        clock.advance(10)
        returned = service.tick()
        self.assertTrue(returned.visible)
        self.assertTrue(returned.pushed)
        self.assertFalse(returned.activated)
        self.assertEqual(len(panel.activations), 1)

    def test_real_art_builder_receives_animated_and_static_payloads_under_contract(self) -> None:
        item = event_at(30, title="Design Review")
        loader = FakeLoader(load_result(b"calendar", validated_at=BASE.timestamp()))
        parser = FakeParser({b"calendar": (item,)})
        panel = FakePanel()
        service = controller.NextUpController(
            [controller.CalendarBinding(loader, "calendar-one")],
            panel,
            timezone=PACIFIC,
            clock=FakeClock(),
            parser=parser,
        )
        self.assertTrue(service.tick().pushed)
        animated, static = panel.pushes[0][1], panel.pushes[0][2]
        self.assertEqual(animated["lifetimeMs"], 180_000)
        self.assertEqual(static["lifetimeMs"], 180_000)
        self.assertLess(len(str(animated).encode("utf-8")), 8192)
        self.assertLess(len(str(static).encode("utf-8")), 8192)
        self.assertNotEqual(animated["icon"], static["icon"])


if __name__ == "__main__":
    unittest.main()
