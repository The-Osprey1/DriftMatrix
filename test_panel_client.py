from __future__ import annotations

from datetime import datetime
import unittest
from unittest.mock import Mock, call

import requests

import panel_client as transport


class FakeClock:
    def __init__(self, monotonic: float = 100.0, wall: datetime | None = None) -> None:
        self.value = float(monotonic)
        self.wall = wall or datetime(2026, 8, 21, 12, 0)
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        seconds = float(seconds)
        self.sleeps.append(seconds)
        self.value += seconds

    def wall_now(self) -> datetime:
        return self.wall

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        json_data: object | None = None,
        text: str = "",
        *,
        json_error: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self._json_error = json_error

    def json(self) -> object:
        if self._json_error is not None:
            raise self._json_error
        return self._json_data


def make_client(
    *,
    clock: FakeClock | None = None,
    policy: transport.PanelPolicy | None = None,
) -> tuple[transport.PanelClient, Mock, FakeClock]:
    clock = clock or FakeClock()
    session = Mock()
    session.put.return_value = FakeResponse(200)
    session.delete.return_value = FakeResponse(200)
    session.get.return_value = FakeResponse(200, [])
    client = transport.PanelClient(
        "http://panel.local/",
        session=session,
        clock=clock,
        policy=policy,
    )
    return client, session, clock


class EndpointAndMutationTests(unittest.TestCase):
    def test_public_endpoint_constants_match_awtrix_http_api(self) -> None:
        self.assertEqual(transport.APPS_PATH, "/api/v1/apps")
        self.assertEqual(transport.DEVICE_PATH, "/api/v1/device")
        self.assertEqual(transport.PUSHED_APP_PATH, "/api/v1/apps/pushed/{name}")
        self.assertEqual(transport.DELETE_APP_PATH, "/api/v1/apps/{name}")
        self.assertEqual(transport.ACTIVE_APP_PATH, "/api/v1/apps/active")

    def test_push_app_uses_exact_endpoint_json_and_timeout(self) -> None:
        client, session, _ = make_client()
        payload = {"text": "WAN OK", "lifetimeMs": 45_000}

        response = client.push_app("networksentinel", payload)

        self.assertEqual(response.status_code, 200)
        session.put.assert_called_once_with(
            "http://panel.local/api/v1/apps/pushed/networksentinel",
            timeout=5.0,
            json=payload,
        )
        session.get.assert_not_called()
        session.delete.assert_not_called()

    def test_app_name_is_one_encoded_path_segment(self) -> None:
        client, session, _ = make_client()
        client.push_app("future/app", {"text": "safe"})
        self.assertEqual(
            session.put.call_args.args[0],
            "http://panel.local/api/v1/apps/pushed/future%2Fapp",
        )

    def test_all_mutation_attempts_are_at_least_two_seconds_apart(self) -> None:
        client, session, clock = make_client()

        client.push_app("networksentinel", {"text": "one"})
        clock.advance(0.25)
        client.push_app("nextup", {"text": "two"})
        clock.advance(1.0)
        client.push_app("focussprint", {"text": "three"})

        self.assertEqual(clock.sleeps, [1.75, 1.0])
        self.assertEqual(session.put.call_count, 3)

    def test_failed_attempt_also_consumes_mutation_slot(self) -> None:
        client, session, clock = make_client()
        session.put.side_effect = [
            requests.ConnectionError("offline"),
            FakeResponse(200),
        ]

        with self.assertRaises(transport.PanelTransportError):
            client.push_app("networksentinel", {"text": "down"})
        client.push_app("networksentinel", {"text": "retry"})

        self.assertEqual(clock.sleeps, [2.0])
        self.assertEqual(session.put.call_count, 2)

    def test_http_error_has_bounded_detail_and_retryability(self) -> None:
        client, session, _ = make_client()
        session.put.return_value = FakeResponse(503, text="x" * 300)

        with self.assertRaises(transport.PanelHTTPError) as caught:
            client.push_app("networksentinel", {})

        self.assertTrue(caught.exception.retryable)
        self.assertEqual(len(caught.exception.detail), 200)

    def test_empty_app_name_is_rejected_without_http(self) -> None:
        client, session, _ = make_client()
        with self.assertRaises(ValueError):
            client.push_app("  ", {})
        session.put.assert_not_called()


class SafeDeletionTests(unittest.TestCase):
    @staticmethod
    def inventory(entry: dict[str, object]) -> FakeResponse:
        return FakeResponse(200, [entry])

    def test_confirmed_present_pushed_app_is_deleted_by_exact_endpoint(self) -> None:
        client, session, _ = make_client()
        session.get.return_value = self.inventory(
            {"name": "networksentinel", "present": True, "origin": "pushed"}
        )

        self.assertTrue(client.delete_pushed_app("networksentinel"))

        session.get.assert_called_once_with(
            "http://panel.local/api/v1/apps",
            timeout=5.0,
        )
        session.delete.assert_called_once_with(
            "http://panel.local/api/v1/apps/networksentinel",
            timeout=5.0,
        )

    def test_not_found_after_confirmed_ownership_is_safe_success(self) -> None:
        client, session, _ = make_client()
        session.get.return_value = self.inventory(
            {"name": "nextup", "present": True, "origin": "pushed"}
        )
        session.delete.return_value = FakeResponse(404)
        self.assertTrue(client.delete_pushed_app("nextup"))

    def test_missing_app_does_not_mutate(self) -> None:
        client, session, _ = make_client()
        session.get.return_value = FakeResponse(200, [])
        self.assertFalse(client.delete_pushed_app("nextup"))
        session.delete.assert_not_called()

    def test_saved_order_placeholder_does_not_mutate(self) -> None:
        client, session, _ = make_client()
        session.get.return_value = self.inventory(
            {"name": "nextup", "present": False, "origin": None}
        )
        self.assertFalse(client.delete_pushed_app("nextup"))
        session.delete.assert_not_called()

    def test_script_builtin_and_unknown_origins_are_never_deleted(self) -> None:
        for origin in ("script", "builtin", None):
            with self.subTest(origin=origin):
                client, session, _ = make_client()
                session.get.return_value = self.inventory(
                    {"name": "nextup", "present": True, "origin": origin}
                )
                with self.assertRaises(transport.UnsafeAppDeletion):
                    client.delete_pushed_app("nextup")
                session.delete.assert_not_called()

    def test_missing_presence_confirmation_is_refused(self) -> None:
        client, session, _ = make_client()
        session.get.return_value = self.inventory(
            {"name": "nextup", "origin": "pushed"}
        )
        with self.assertRaises(transport.UnsafeAppDeletion):
            client.delete_pushed_app("nextup")
        session.delete.assert_not_called()

    def test_duplicate_inventory_entries_are_refused(self) -> None:
        client, session, _ = make_client()
        entry = {"name": "nextup", "present": True, "origin": "pushed"}
        session.get.return_value = FakeResponse(200, [entry, dict(entry)])
        with self.assertRaises(transport.PanelProtocolError):
            client.delete_pushed_app("nextup")
        session.delete.assert_not_called()

    def test_wrapped_apps_inventory_shape_is_supported(self) -> None:
        client, session, _ = make_client()
        session.get.return_value = FakeResponse(
            200,
            {"apps": [{"name": "nextup", "present": True, "origin": "pushed"}]},
        )
        self.assertTrue(client.delete_pushed_app("nextup"))

    def test_unexpected_inventory_shape_never_reaches_delete(self) -> None:
        client, session, _ = make_client()
        session.get.return_value = FakeResponse(200, {"unexpected": []})
        with self.assertRaises(transport.PanelProtocolError):
            client.delete_pushed_app("nextup")
        session.delete.assert_not_called()


class ActivationPolicyTests(unittest.TestCase):
    def test_daytime_activation_uses_exact_body(self) -> None:
        clock = FakeClock(wall=datetime(2026, 8, 21, 12, 0))
        client, session, _ = make_client(clock=clock)

        outcome = client.activate_app(
            "networksentinel",
            priority=transport.MutationPriority.OUTAGE,
        )

        self.assertTrue(outcome.sent)
        self.assertEqual(outcome.reason, "sent")
        self.assertEqual(outcome.priority, transport.MutationPriority.OUTAGE)
        session.put.assert_called_once_with(
            "http://panel.local/api/v1/apps/active",
            timeout=5.0,
            json={"name": "networksentinel", "fast": True},
        )

    def test_quiet_hours_wrap_midnight_and_suppress_without_mutation(self) -> None:
        for hour, minute in ((21, 30), (23, 59), (0, 0), (6, 59)):
            with self.subTest(hour=hour, minute=minute):
                clock = FakeClock(wall=datetime(2026, 8, 21, hour, minute))
                client, session, _ = make_client(clock=clock)
                outcome = client.activate_app("networksentinel")
                self.assertFalse(outcome.sent)
                self.assertEqual(outcome.reason, "quiet-hours")
                session.put.assert_not_called()

    def test_quiet_end_is_exclusive(self) -> None:
        clock = FakeClock(wall=datetime(2026, 8, 21, 7, 0))
        client, session, _ = make_client(clock=clock)
        self.assertTrue(client.activate_app("nextup").sent)
        session.put.assert_called_once()

    def test_activation_cooldown_is_global_across_apps(self) -> None:
        client, session, clock = make_client()
        self.assertTrue(client.activate_app("networksentinel").sent)

        clock.advance(9.5)
        held = client.activate_app("focussprint")
        self.assertFalse(held.sent)
        self.assertEqual(held.reason, "cooldown")
        self.assertAlmostEqual(held.retry_after_seconds, 0.5)

        clock.advance(0.5)
        self.assertTrue(client.activate_app("nextup").sent)
        self.assertEqual(session.put.call_count, 2)

    def test_failed_activation_also_consumes_global_cooldown(self) -> None:
        client, session, clock = make_client()
        session.put.side_effect = requests.ConnectionError("offline")
        with self.assertRaises(transport.PanelTransportError):
            client.activate_app("networksentinel")

        session.put.reset_mock(side_effect=True)
        session.put.return_value = FakeResponse(200)
        clock.advance(1.0)
        outcome = client.activate_app("nextup")
        self.assertFalse(outcome.sent)
        self.assertEqual(outcome.reason, "cooldown")
        session.put.assert_not_called()

    def test_quiet_hours_can_be_disabled_without_other_policy_changes(self) -> None:
        policy = transport.PanelPolicy(quiet_start=None, quiet_end=None)
        clock = FakeClock(wall=datetime(2026, 8, 21, 23, 0))
        client, session, _ = make_client(clock=clock, policy=policy)
        self.assertTrue(client.activate_app("nextup").sent)
        session.put.assert_called_once()


class RebootAndStaticLatchTests(unittest.TestCase):
    def test_device_probe_is_read_only_and_returns_typed_health(self) -> None:
        client, session, _ = make_client()
        session.get.return_value = FakeResponse(
            200, {"uptimeSeconds": 1234, "resetReason": "poweron"}
        )

        health = client.probe_device()

        self.assertEqual(health, transport.DeviceHealth(1234, "poweron"))
        session.get.assert_called_once_with(
            "http://panel.local/api/v1/device",
            timeout=5.0,
        )

    def test_device_probe_reports_current_app_and_matrix_power(self) -> None:
        client, session, _ = make_client()
        session.get.return_value = FakeResponse(200, {
            "uptimeSeconds": 60, "resetReason": "software", "fps": 41,
            "currentApp": "nowplaying" + "x" * 40, "matrixPower": False,
        })

        health = client.probe_device()

        self.assertEqual(health.current_app, ("nowplaying" + "x" * 40)[:32])
        self.assertIs(health.matrix_power, False)

    def test_device_probe_ignores_malformed_app_and_power(self) -> None:
        client, session, _ = make_client()
        session.get.return_value = FakeResponse(200, {
            "uptimeSeconds": 60, "currentApp": 7, "matrixPower": "yes",
        })

        health = client.probe_device()

        self.assertIsNone(health.current_app)
        self.assertIsNone(health.matrix_power)
        session.put.assert_not_called()
        session.delete.assert_not_called()

    def test_reset_reason_alone_does_not_latch_static(self) -> None:
        client, session, _ = make_client()
        session.get.side_effect = [
            FakeResponse(200, {"uptimeSeconds": 100, "resetReason": "panic"}),
            FakeResponse(200, {"uptimeSeconds": 101, "resetReason": "panic"}),
        ]
        client.probe_device()
        client.probe_device()
        self.assertFalse(client.static_latched)

    def test_direct_uptime_rollback_latches_static(self) -> None:
        # Was named "permanently_latches_static", which is what it used to do
        # and was the defect. See StaticLatchRecoveryTests below.
        client, session, _ = make_client()
        session.get.side_effect = [
            FakeResponse(200, {"uptimeSeconds": 900}),
            FakeResponse(200, {"uptimeSeconds": 4}),
        ]
        client.probe_device()
        client.probe_device()

        self.assertTrue(client.static_latched)
        self.assertEqual(client.last_uptime, 4)
        self.assertEqual(client.static_reason, "uptime rollback 900 -> 4")

    def test_software_rollback_preserves_animation_and_signals_lost_pushed_apps(self) -> None:
        client, session, _ = make_client()
        session.get.side_effect = [
            FakeResponse(200, {"uptimeSeconds": 900, "resetReason": "poweron"}),
            FakeResponse(200, {"uptimeSeconds": 4, "resetReason": " SOFTWARE "}),
            FakeResponse(200, {"uptimeSeconds": 5, "resetReason": "software"}),
            FakeResponse(200, {"uptimeSeconds": 1, "resetReason": "panic"}),
        ]
        self.assertEqual(client.reboot_generation, 0)
        client.probe_device()
        client.probe_device()
        self.assertFalse(client.static_latched)
        self.assertEqual(client.reboot_generation, 1)
        client.probe_device()
        self.assertEqual(client.reboot_generation, 1)
        client.probe_device()
        self.assertTrue(client.static_latched)
        self.assertEqual(client.reboot_generation, 2)

    def test_pre_reset_read_does_not_make_a_requested_software_reboot_a_crash(self) -> None:
        client, session, _ = make_client()
        session.post = Mock(return_value=FakeResponse(200))
        session.get.side_effect = [
            FakeResponse(200, {"uptimeSeconds": 900, "resetReason": "poweron"}),
            FakeResponse(200, {"uptimeSeconds": 901, "resetReason": "poweron"}),
            FakeResponse(200, {"uptimeSeconds": 4, "resetReason": "software"}),
        ]
        client.probe_device()
        client.reboot()
        client.probe_device()  # A heartbeat can land before the panel resets.
        self.assertEqual(client.reboot_generation, 0)
        client.probe_device()
        self.assertFalse(client.static_latched)
        self.assertEqual(client.reboot_generation, 1)

    def test_requested_reboot_does_not_hide_a_panic_or_unknown_reset(self) -> None:
        for reason in ("panic", "watchdog", None, ""):
            with self.subTest(reason=reason):
                client, session, _ = make_client()
                session.post = Mock(return_value=FakeResponse(200))
                session.get.side_effect = [
                    FakeResponse(200, {"uptimeSeconds": 900}),
                    FakeResponse(200, {"uptimeSeconds": 4, "resetReason": reason}),
                ]
                client.probe_device()
                client.reboot()
                client.probe_device()
                self.assertTrue(client.static_latched)
                self.assertEqual(client.reboot_generation, 1)

    def test_five_consecutive_failed_mutations_latch_static_variant(self) -> None:
        client, session, _ = make_client()
        session.put.side_effect = [requests.ConnectionError("offline")] * 5 + [FakeResponse(200)]

        for _ in range(5):
            with self.assertRaises(transport.PanelTransportError):
                client.push_app("networksentinel", {"icon": "animated"})

        self.assertTrue(client.static_latched)
        self.assertEqual(client.consecutive_mutation_failures, 5)
        client.push_app(
            "networksentinel",
            {"icon": "animated"},
            static_payload={"icon": "static"},
        )

        self.assertEqual(session.put.call_args.kwargs["json"], {"icon": "static"})
        self.assertEqual(client.consecutive_mutation_failures, 0)
        self.assertTrue(client.static_latched)

    def test_success_breaks_failure_streak_before_threshold(self) -> None:
        client, session, _ = make_client()
        session.put.side_effect = [
            requests.ConnectionError("one"),
            requests.ConnectionError("two"),
            FakeResponse(200),
            requests.ConnectionError("one again"),
            requests.ConnectionError("two again"),
        ]
        for index in range(5):
            if index == 2:
                client.push_app("nextup", {})
            else:
                with self.assertRaises(transport.PanelTransportError):
                    client.push_app("nextup", {})
        self.assertFalse(client.static_latched)
        self.assertEqual(client.consecutive_mutation_failures, 2)

    def test_bad_device_shape_does_not_guess_about_reboot(self) -> None:
        client, session, _ = make_client()
        session.get.return_value = FakeResponse(200, {"resetReason": "panic"})
        with self.assertRaises(transport.PanelProtocolError):
            client.probe_device()
        self.assertFalse(client.static_latched)


class StaticLatchRecoveryTests(unittest.TestCase):
    """Degrading to static art must be recoverable.

    It was not. Five consecutive mutation failures -- roughly twenty-five
    seconds of a Wi-Fi dropout on this -75 dBm link -- silently swapped every
    animated payload for its static variant for the rest of the process's
    life, with nothing logged and no way back. The identical mistake had
    already been found and fixed once in pc_stats_push's safe_mode latch; this
    was the second copy of it.
    """

    def _latch(self, client, session):
        session.put.side_effect = [requests.ConnectionError("offline")] * 5
        for _ in range(5):
            with self.assertRaises(transport.PanelTransportError):
                client.push_app("networksentinel", {"icon": "animated"})
        self.assertTrue(client.static_latched)

    def test_enough_clean_mutations_earn_animation_back(self) -> None:
        client, session, _ = make_client()
        self._latch(client, session)
        needed = client.policy.animate_after_successes

        session.put.side_effect = [FakeResponse(200)] * needed
        for _ in range(needed):
            client.push_app("networksentinel", {"icon": "animated"},
                            static_payload={"icon": "static"})

        self.assertFalse(client.static_latched, "the latch never cleared")
        self.assertIsNone(client.static_reason)

    def test_animation_is_not_returned_early(self) -> None:
        """One good push after an outage is not evidence the panel recovered."""
        client, session, _ = make_client()
        self._latch(client, session)
        needed = client.policy.animate_after_successes

        session.put.side_effect = [FakeResponse(200)] * (needed - 1)
        for _ in range(needed - 1):
            client.push_app("networksentinel", {"icon": "animated"},
                            static_payload={"icon": "static"})
        self.assertTrue(client.static_latched)

    def test_an_intermittent_link_never_accumulates_its_way_out(self) -> None:
        """The successes have to be consecutive, or a flapping link recovers."""
        client, session, _ = make_client()
        self._latch(client, session)
        needed = client.policy.animate_after_successes

        pattern = []
        for _ in range(needed * 3):
            pattern.extend([FakeResponse(200)] * (needed - 1))
            pattern.append(requests.ConnectionError("flap"))
        session.put.side_effect = pattern

        for outcome in pattern:
            try:
                client.push_app("networksentinel", {"icon": "animated"},
                                static_payload={"icon": "static"})
            except transport.PanelTransportError:
                pass
        self.assertTrue(client.static_latched, "a flapping link talked its way out")

    def test_recovery_restores_the_animated_payload(self) -> None:
        """The point of clearing the latch, asserted on the wire."""
        client, session, _ = make_client()
        self._latch(client, session)
        needed = client.policy.animate_after_successes

        session.put.side_effect = [FakeResponse(200)] * (needed + 1)
        for _ in range(needed):
            client.push_app("networksentinel", {"icon": "animated"},
                            static_payload={"icon": "static"})
        client.push_app("networksentinel", {"icon": "animated"},
                        static_payload={"icon": "static"})
        self.assertEqual(session.put.call_args.kwargs["json"], {"icon": "animated"})

    def test_a_reboot_latch_recovers_the_same_way(self) -> None:
        client, session, _ = make_client()
        session.get.side_effect = [
            FakeResponse(200, {"uptimeSeconds": 900}),
            FakeResponse(200, {"uptimeSeconds": 4}),
        ]
        client.probe_device()
        client.probe_device()
        self.assertTrue(client.static_latched)

        needed = client.policy.animate_after_successes
        session.put.side_effect = [FakeResponse(200)] * needed
        for _ in range(needed):
            client.push_app("moon", {"icon": "animated"}, static_payload={"icon": "static"})
        self.assertFalse(client.static_latched)

    def test_recovery_is_slower_than_degradation(self) -> None:
        """Deliberate: a panel that cannot cope should not be retried eagerly."""
        policy = transport.PanelPolicy()
        self.assertGreater(policy.animate_after_successes, policy.static_after_failures)

    def test_a_nonsense_recovery_threshold_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            transport.PanelPolicy(animate_after_successes=0)


class RetryAndPriorityTests(unittest.TestCase):
    def test_backoff_matches_bounded_one_to_thirty_second_schedule(self) -> None:
        backoff = transport.RetryBackoff()
        self.assertEqual(
            [backoff.delay(failure) for failure in range(1, 8)],
            [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0],
        )

    def test_retry_call_retries_transport_and_server_errors(self) -> None:
        clock = FakeClock()
        operation = Mock(
            side_effect=[
                transport.PanelTransportError("offline"),
                transport.PanelHTTPError("push", 503),
                "ok",
            ]
        )
        self.assertEqual(
            transport.retry_call(operation, attempts=3, clock=clock),
            "ok",
        )
        self.assertEqual(clock.sleeps, [1.0, 2.0])

    def test_retry_call_does_not_retry_payload_validation_error(self) -> None:
        clock = FakeClock()
        operation = Mock(side_effect=transport.PanelHTTPError("push", 422, "bad key"))
        with self.assertRaises(transport.PanelHTTPError):
            transport.retry_call(operation, attempts=3, clock=clock)
        operation.assert_called_once()
        self.assertEqual(clock.sleeps, [])

    def test_priority_queue_runs_outage_then_focus_then_nextup_and_fifo(self) -> None:
        events: list[str] = []
        queue = transport.PriorityMutationQueue()
        queue.submit(
            lambda: events.append("next-1"),
            priority=transport.MutationPriority.NEXT_UP,
        )
        queue.submit(
            lambda: events.append("focus"),
            priority=transport.MutationPriority.FOCUS,
        )
        queue.submit(
            lambda: events.append("outage"),
            priority=transport.MutationPriority.OUTAGE,
        )
        queue.submit(
            lambda: events.append("next-2"),
            priority=transport.MutationPriority.NEXT_UP,
        )

        while len(queue):
            queue.run_next()

        self.assertEqual(events, ["outage", "focus", "next-1", "next-2"])

    def test_queue_activation_is_a_deferred_hook_not_an_immediate_write(self) -> None:
        client, session, _ = make_client()
        queue = transport.PriorityMutationQueue()
        item = client.queue_activation(
            queue,
            "networksentinel",
            priority=transport.MutationPriority.OUTAGE,
        )
        session.put.assert_not_called()
        self.assertEqual(item.label, "activate:networksentinel")

        outcome = queue.run_next()

        self.assertTrue(outcome.sent)
        session.put.assert_called_once()

    def test_client_retry_uses_its_injected_clock_and_policy(self) -> None:
        clock = FakeClock()
        policy = transport.PanelPolicy(
            retry_backoff=transport.RetryBackoff(initial_seconds=0.5, maximum_seconds=1.0)
        )
        client, _, _ = make_client(clock=clock, policy=policy)
        operation = Mock(side_effect=[transport.PanelTransportError("x"), "done"])
        self.assertEqual(client.call_with_retry(operation, attempts=2), "done")
        self.assertEqual(clock.sleeps, [0.5])


SSID = "OurHouse-5G"
ENDPOINT = "ourhouse-mesh-2"


def device_body(**overrides: object) -> dict:
    """GET /api/v1/device as firmware 1.1.2 shapes it (2026-10-09 sample)."""
    wifi = {"enabled": True, "state": "connected", "host": SSID, "endpoint": ENDPOINT,
            "attempts": 0, "retryInMs": 0, "connects": 1, "error": None, "lastError": None}
    wifi.update(overrides.pop("wifi", {}))
    body = {"uptimeSeconds": 53213, "resetReason": "software", "freeHeapBytes": 31264,
            "minFreeHeapBytes": 11524, "largestFreeBlockBytes": 19444, "fps": 32,
            "matrixPower": True, "currentApp": "ocean", "wifiRssi": -68, "wifi": wifi}
    body.update(overrides)
    return body


class WifiHealthTests(unittest.TestCase):
    def probe(self, body: object) -> transport.DeviceHealth:
        client, session, _ = make_client()
        session.get.return_value = FakeResponse(200, body)
        health = client.probe_device()
        session.put.assert_not_called()
        session.delete.assert_not_called()
        return health

    def test_probe_reads_rssi_counters_state_and_last_error(self) -> None:
        health = self.probe(device_body(
            wifiRssi=-71, wifi={"connects": 3, "attempts": 2, "lastError": "beacon timeout"}))
        self.assertEqual(
            (health.wifi_rssi, health.wifi_connects, health.wifi_attempts,
             health.wifi_state, health.wifi_last_error),
            (-71, 3, 2, "connected", "beacon timeout"))
        self.assertEqual(health.largest_block, 19444)

    def test_the_ssid_and_endpoint_never_reach_the_health_record(self) -> None:
        health = self.probe(device_body(wifi={
            "state": f"joining {SSID}",
            "lastError": f"no AP named {SSID.lower()} near {ENDPOINT.upper()}"}))
        self.assertEqual(health.wifi_state, "joining *")
        self.assertEqual(health.wifi_last_error, "no AP named * near *")
        for secret in (SSID, ENDPOINT):
            self.assertNotIn(secret.lower(), repr(health).lower())

    def test_redaction_happens_before_truncation_so_no_ssid_prefix_survives(self) -> None:
        # Cut first and "x"*40 + " OurHous" would be kept: the first 8 letters of the SSID.
        health = self.probe(device_body(wifi={"lastError": "x" * 40 + " " + SSID + " lost"}))
        self.assertEqual(health.wifi_last_error, "x" * 40 + " * lost")
        self.assertNotIn("OurH", health.wifi_last_error)
        long = self.probe(device_body(wifi={"lastError": "e" * 100}))
        self.assertEqual(long.wifi_last_error, "e" * transport.WIFI_ERROR_CHARS)

    def test_rssi_outside_the_sanity_range_or_not_a_whole_number_is_none(self) -> None:
        cases = [(-69, -69), (-120, -120), (-1, -1), (-69.0, -69), (0, None), (5, None),
                 (-121, None), (-69.5, None), (float("nan"), None), (float("inf"), None),
                 (True, None), ("-69", None), (None, None), ([], None), ({}, None)]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(self.probe(device_body(wifiRssi=value)).wifi_rssi, expected)
        missing = device_body()
        del missing["wifiRssi"], missing["wifi"]
        self.assertEqual(self.probe(missing).wifi_rssi, None)
        self.assertIsNone(self.probe({"uptimeSeconds": 5}).wifi_state)

    def test_counters_must_be_non_negative_integers(self) -> None:
        cases = [(0, 0), (7, 7), (2**32 - 1, 2**32 - 1), (2**32, None), (-1, None),
                 (True, None), (1.0, None), ("3", None), (None, None)]
        for value, expected in cases:
            with self.subTest(value=value):
                health = self.probe(device_body(wifi={"connects": value, "attempts": value}))
                self.assertEqual((health.wifi_connects, health.wifi_attempts), (expected, expected))

    def test_state_and_error_lose_control_characters_and_are_bounded(self) -> None:
        health = self.probe(device_body(wifi={
            "state": "con\x00nec\x1bted‮\x7f\x85", "lastError": "auth\nfail​\t"}))
        self.assertEqual((health.wifi_state, health.wifi_last_error), ("connected", "authfail"))
        bounded = self.probe(device_body(wifi={"state": "reconnecting-after-timeout"}))
        self.assertEqual(bounded.wifi_state, "reconnecting-aft")
        self.assertLessEqual(len(bounded.wifi_state), transport.WIFI_STATE_CHARS)
        for junk in (7, None, [], "", "   ", "\x00\x01"):
            with self.subTest(junk=junk):
                health = self.probe(device_body(wifi={"state": junk, "lastError": junk}))
                self.assertIsNone(health.wifi_state)
                self.assertIsNone(health.wifi_last_error)

    def test_malformed_wifi_object_blanks_wifi_and_keeps_the_rest(self) -> None:
        for wifi in ("connected", [], 7, None, True):
            with self.subTest(wifi=wifi):
                body = device_body()
                body["wifi"] = wifi
                health = self.probe(body)
                self.assertEqual(
                    (health.wifi_connects, health.wifi_attempts, health.wifi_state,
                     health.wifi_last_error), (None, None, None, None))
                self.assertEqual((health.wifi_rssi, health.uptime_seconds), (-68, 53213))

    def test_a_firmware_cut_prefix_of_the_ssid_drops_the_whole_text(self) -> None:
        long_ssid = "LongNetworkName-ABCDEFGHIJKLMNOP"
        for text in ("auth fail LongNetworkName-ABCDEF",  # the panel cut its own buffer
                     "auth fail LongNe",
                     "...MNOP lost"):                      # or the start of the message
            with self.subTest(text=text):
                health = self.probe(device_body(wifi={"host": long_ssid, "lastError": text}))
                self.assertEqual(health.wifi_last_error, transport.WIFI_REDACTED)
        # Under SSID_FRAGMENT_CHARS of the name in common is kept.
        kept = self.probe(device_body(wifi={"host": long_ssid, "lastError": "auth fail Lon"}))
        self.assertEqual(kept.wifi_last_error, "auth fail Lon")

    def test_unicode_forms_and_case_folding_of_the_ssid_are_redacted(self) -> None:
        cases = [
            ("Café", "lost Café at 3", "lost * at 3"),     # NFC name, NFD text
            ("Café", "lost Café at 3", "lost * at 3"),     # and the other way
            ("Straße", "no AP STRASSE", transport.WIFI_REDACTED),  # full case folding
            ("ＯＵＲ-5G", "lost OUR-5g", "lost *"),      # fullwidth letters
        ]
        for ssid, text, expected in cases:
            with self.subTest(ssid=ssid, text=text):
                health = self.probe(device_body(wifi={"host": ssid, "lastError": text, "state": text}))
                self.assertEqual(health.wifi_last_error, expected)
                self.assertEqual(health.wifi_state, expected[:transport.WIFI_STATE_CHARS].strip())

    def test_an_endpoint_holding_the_ssid_is_cut_out_whole(self) -> None:
        health = self.probe(device_body(wifi={
            "host": "OurHouse", "endpoint": "OurHouse-mesh-2", "lastError": "lost OurHouse-mesh-2"}))
        self.assertEqual(health.wifi_last_error, "lost *")

    def test_odd_host_values_do_not_break_redaction(self) -> None:
        for host in (None, 7, "", "  ", "a.b*c(", "\x00"):
            with self.subTest(host=host):
                health = self.probe(device_body(wifi={"host": host, "lastError": "a.b*c( down"}))
                expected = "* down" if host == "a.b*c(" else "a.b*c( down"
                self.assertEqual(health.wifi_last_error, expected)


if __name__ == "__main__":
    unittest.main()
