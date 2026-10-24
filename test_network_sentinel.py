"""Deterministic contract tests for the Network Sentinel decision engine."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import network_sentinel as ns


def healthy(latency: float = 40.0, timestamp: float = 0.0) -> ns.ProbeSample:
    return ns.ProbeSample(True, True, True, False, latency, timestamp)


def router_down(timestamp: float = 0.0) -> ns.ProbeSample:
    return ns.ProbeSample(False, None, None, None, None, timestamp)


def wan_down(timestamp: float = 0.0) -> ns.ProbeSample:
    return ns.ProbeSample(True, False, None, None, None, timestamp)


def dns_down(timestamp: float = 0.0) -> ns.ProbeSample:
    return ns.ProbeSample(True, True, False, None, 40.0, timestamp)


def login(timestamp: float = 0.0) -> ns.ProbeSample:
    return ns.ProbeSample(True, True, True, True, 40.0, timestamp)


class PublicModelTests(unittest.TestCase):
    def test_network_state_is_string_valued_and_complete(self) -> None:
        self.assertTrue(issubclass(ns.NetworkState, str))
        self.assertEqual(
            {state.name for state in ns.NetworkState},
            {"HEALTHY", "ROUTER", "WAN", "DNS", "LOGIN", "LOSS", "SLOW", "BACK"},
        )
        self.assertEqual(ns.NetworkState.ROUTER.value, "router")

    def test_default_policy_is_the_product_contract(self) -> None:
        policy = ns.DEFAULT_POLICY
        self.assertEqual(policy.hard_trigger_count, 3)
        self.assertEqual(policy.hard_clear_count, 6)
        self.assertEqual(
            (
                policy.loss_window,
                policy.loss_min_failures,
                policy.loss_trigger_percent,
                policy.loss_clear_percent,
            ),
            (12, 3, 25.0, 10.0),
        )
        self.assertEqual(
            (
                policy.slow_window,
                policy.slow_min_high,
                policy.slow_trigger_ms,
                policy.slow_clear_ms,
                policy.slow_clear_count,
            ),
            (6, 4, 150.0, 100.0, 6),
        )

    def test_bad_samples_are_rejected(self) -> None:
        with self.assertRaises(TypeError):
            ns.ProbeSample(1, True, True, False)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            ns.ProbeSample(True, "yes", True, False)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            ns.ProbeSample(True, True, True, False, -0.1)
        with self.assertRaises(ValueError):
            ns.ProbeSample(True, True, True, False, float("nan"))

    def test_bad_policy_relationships_are_rejected(self) -> None:
        bad = (
            {"hard_trigger_count": 0},
            {"loss_window": 2, "loss_min_failures": 3},
            {"loss_clear_percent": 25},
            {"slow_window": 3, "slow_min_high": 4},
            {"slow_clear_ms": 150},
        )
        for kwargs in bad:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ns.SentinelPolicy(**kwargs)


class HardFaultTests(unittest.TestCase):
    def test_every_hard_fault_needs_three_consecutive_samples(self) -> None:
        cases = (
            (ns.NetworkState.ROUTER, router_down()),
            (ns.NetworkState.WAN, wan_down()),
            (ns.NetworkState.DNS, dns_down()),
            (ns.NetworkState.LOGIN, login()),
        )
        for expected, sample in cases:
            with self.subTest(state=expected):
                sentinel = ns.NetworkSentinel()
                first = sentinel.update(sample)
                second = sentinel.update(sample)
                third = sentinel.update(sample)

                self.assertEqual(first.state, ns.NetworkState.HEALTHY)
                self.assertEqual(first.observed, expected)
                self.assertEqual(first.trigger_streak, 1)
                self.assertFalse(first.changed)
                self.assertEqual(second.state, ns.NetworkState.HEALTHY)
                self.assertEqual(second.trigger_streak, 2)
                self.assertEqual(third.state, expected)
                self.assertEqual(third.entered, expected)
                self.assertTrue(third.changed)
                self.assertTrue(third.is_alert)

    def test_one_isolated_failure_never_alerts(self) -> None:
        sentinel = ns.NetworkSentinel()
        self.assertEqual(sentinel.update(wan_down()).state, ns.NetworkState.HEALTHY)
        for _ in range(12):
            decision = sentinel.update(healthy())
            self.assertNotIn(decision.state, ns.FAULT_STATES)
        self.assertEqual(decision.loss_failures, 0)

    def test_candidate_type_must_itself_be_consecutive(self) -> None:
        sentinel = ns.NetworkSentinel()
        sequence = (wan_down(), dns_down(), wan_down(), wan_down())
        states = [sentinel.update(sample).state for sample in sequence]
        self.assertEqual(states, [ns.NetworkState.HEALTHY] * 4)
        entered = sentinel.update(wan_down())
        self.assertEqual(entered.state, ns.NetworkState.WAN)

    def test_priority_reports_the_earliest_broken_hop(self) -> None:
        all_broken = ns.ProbeSample(False, False, False, True)
        sentinel = ns.NetworkSentinel()
        decisions = [sentinel.update(all_broken) for _ in range(3)]
        self.assertTrue(all(item.observed is ns.NetworkState.ROUTER for item in decisions))
        self.assertEqual(decisions[-1].state, ns.NetworkState.ROUTER)

        dns_and_login = ns.ProbeSample(True, True, False, True, 30.0)
        sentinel = ns.NetworkSentinel()
        decision = None
        for _ in range(3):
            decision = sentinel.update(dns_and_login)
        assert decision is not None
        self.assertEqual(decision.state, ns.NetworkState.DNS)

    def test_confirmed_hard_fault_switches_directly_to_another(self) -> None:
        sentinel = ns.NetworkSentinel()
        for _ in range(3):
            decision = sentinel.update(wan_down())
        self.assertEqual(decision.state, ns.NetworkState.WAN)

        self.assertEqual(sentinel.update(dns_down()).state, ns.NetworkState.WAN)
        self.assertEqual(sentinel.update(dns_down()).state, ns.NetworkState.WAN)
        switched = sentinel.update(dns_down())
        self.assertEqual(switched.state, ns.NetworkState.DNS)
        self.assertEqual(switched.entered, ns.NetworkState.DNS)
        self.assertIsNone(switched.recovered_from)

    def test_hard_fault_needs_six_healthy_samples_then_emits_back_once(self) -> None:
        sentinel = ns.NetworkSentinel()
        for _ in range(3):
            sentinel.update(wan_down())

        for expected_streak in range(1, 6):
            decision = sentinel.update(healthy())
            self.assertEqual(decision.state, ns.NetworkState.WAN)
            self.assertEqual(decision.clear_streak, expected_streak)

        back = sentinel.update(healthy())
        self.assertEqual(back.state, ns.NetworkState.BACK)
        self.assertEqual(back.entered, ns.NetworkState.BACK)
        self.assertEqual(back.recovered_from, ns.NetworkState.WAN)
        self.assertTrue(back.is_recovery)
        self.assertFalse(back.is_alert)

        clear = sentinel.update(healthy())
        self.assertEqual(clear.state, ns.NetworkState.HEALTHY)
        self.assertEqual(clear.entered, ns.NetworkState.HEALTHY)
        self.assertTrue(clear.changed)
        self.assertIsNone(clear.recovered_from)

    def test_a_different_hard_fault_interrupts_the_clear_streak(self) -> None:
        sentinel = ns.NetworkSentinel()
        for _ in range(3):
            sentinel.update(wan_down())
        for _ in range(5):
            sentinel.update(healthy())
        interrupted = sentinel.update(dns_down())
        self.assertEqual(interrupted.state, ns.NetworkState.WAN)
        self.assertEqual(interrupted.clear_streak, 0)
        for _ in range(5):
            self.assertEqual(sentinel.update(healthy()).state, ns.NetworkState.WAN)
        self.assertEqual(sentinel.update(healthy()).state, ns.NetworkState.BACK)


class LossFaultTests(unittest.TestCase):
    @staticmethod
    def _loss_pattern() -> list[ns.ProbeSample]:
        # Three intermittent failures: never a hard-WAN streak, but exactly
        # 25% loss once all 12 observations are present.
        return [
            wan_down() if index in {0, 3, 6} else healthy()
            for index in range(12)
        ]

    def test_three_intermittent_failures_trigger_loss_not_wan(self) -> None:
        sentinel = ns.NetworkSentinel()
        decisions = [sentinel.update(sample) for sample in self._loss_pattern()]
        alerts = [item for item in decisions if item.is_alert]
        self.assertTrue(alerts)
        self.assertTrue(all(item.state is ns.NetworkState.LOSS for item in alerts))
        self.assertEqual(decisions[-1].state, ns.NetworkState.LOSS)
        self.assertEqual(decisions[-1].loss_failures, 3)
        self.assertEqual(decisions[-1].loss_samples, 12)
        self.assertEqual(decisions[-1].loss_percent, 25.0)

    def test_loss_holds_below_trigger_and_clears_only_below_ten_percent(self) -> None:
        sentinel = ns.NetworkSentinel()
        for sample in self._loss_pattern():
            decision = sentinel.update(sample)
        self.assertEqual(decision.state, ns.NetworkState.LOSS)

        # Removing the oldest failure leaves 2/12 = 16.7%, below the trigger
        # but above the strict clear threshold, so the alert must remain.
        decision = sentinel.update(healthy())
        self.assertEqual(decision.loss_percent, 16.7)
        self.assertEqual(decision.state, ns.NetworkState.LOSS)
        sentinel.update(healthy())
        sentinel.update(healthy())

        # The next success removes the second old failure: 1/12 = 8.3%.
        back = sentinel.update(healthy())
        self.assertEqual(back.loss_percent, 8.3)
        self.assertEqual(back.state, ns.NetworkState.BACK)
        self.assertEqual(back.recovered_from, ns.NetworkState.LOSS)

    def test_loss_has_priority_over_simultaneous_slowness(self) -> None:
        sentinel = ns.NetworkSentinel()
        samples = [
            wan_down() if index in {0, 3, 6} else healthy(200.0)
            for index in range(8)
        ]
        decisions = [sentinel.update(sample) for sample in samples]
        self.assertIn(ns.NetworkState.SLOW, [item.state for item in decisions])
        self.assertEqual(decisions[-1].state, ns.NetworkState.LOSS)

    def test_confirmed_router_fault_overtakes_loss_after_three_samples(self) -> None:
        sentinel = ns.NetworkSentinel()
        for sample in self._loss_pattern():
            sentinel.update(sample)
        self.assertEqual(sentinel.current_state, ns.NetworkState.LOSS)
        self.assertEqual(sentinel.update(router_down()).state, ns.NetworkState.LOSS)
        self.assertEqual(sentinel.update(router_down()).state, ns.NetworkState.LOSS)
        changed = sentinel.update(router_down())
        self.assertEqual(changed.state, ns.NetworkState.ROUTER)
        self.assertEqual(changed.entered, ns.NetworkState.ROUTER)


class SlowFaultTests(unittest.TestCase):
    def test_four_of_six_at_or_above_150ms_triggers_slow(self) -> None:
        sentinel = ns.NetworkSentinel()
        latencies = (150.0, 180.0, 80.0, 220.0, 90.0, 160.0)
        decisions = [sentinel.update(healthy(value)) for value in latencies]
        self.assertTrue(all(item.state is ns.NetworkState.HEALTHY for item in decisions[:-1]))
        entered = decisions[-1]
        self.assertEqual(entered.state, ns.NetworkState.SLOW)
        self.assertEqual(entered.observed, ns.NetworkState.SLOW)
        self.assertEqual(entered.entered, ns.NetworkState.SLOW)
        self.assertEqual(entered.high_latency_samples, 4)
        self.assertEqual(entered.latency_samples, 6)

    def test_slow_does_not_trigger_before_a_full_six_sample_window(self) -> None:
        sentinel = ns.NetworkSentinel()
        for _ in range(5):
            self.assertEqual(sentinel.update(healthy(500.0)).state, ns.NetworkState.HEALTHY)

    def test_slow_needs_six_consecutive_samples_strictly_below_100ms(self) -> None:
        sentinel = ns.NetworkSentinel()
        for latency in (150.0, 180.0, 80.0, 220.0, 90.0, 160.0):
            sentinel.update(healthy(latency))
        self.assertEqual(sentinel.current_state, ns.NetworkState.SLOW)

        for expected_streak in range(1, 6):
            decision = sentinel.update(healthy(99.0))
            self.assertEqual(decision.state, ns.NetworkState.SLOW)
            self.assertEqual(decision.clear_streak, expected_streak)

        # Exactly 100 is intentionally not below the clear threshold.
        held = sentinel.update(healthy(100.0))
        self.assertEqual(held.state, ns.NetworkState.SLOW)
        self.assertEqual(held.clear_streak, 0)

        for _ in range(5):
            self.assertEqual(sentinel.update(healthy(50.0)).state, ns.NetworkState.SLOW)
        back = sentinel.update(healthy(50.0))
        self.assertEqual(back.state, ns.NetworkState.BACK)
        self.assertEqual(back.recovered_from, ns.NetworkState.SLOW)

    def test_slow_clear_streak_resets_on_a_failed_attempt(self) -> None:
        sentinel = ns.NetworkSentinel()
        for _ in range(6):
            sentinel.update(healthy(200.0))
        for _ in range(5):
            sentinel.update(healthy(40.0))
        interrupted = sentinel.update(wan_down())
        self.assertEqual(interrupted.state, ns.NetworkState.SLOW)
        self.assertEqual(interrupted.clear_streak, 0)


class LifecycleTests(unittest.TestCase):
    def test_initial_healthy_decision_has_explicit_unchanged_semantics(self) -> None:
        decision = ns.NetworkSentinel().update(healthy())
        self.assertEqual(decision.state, ns.NetworkState.HEALTHY)
        self.assertEqual(decision.observed, ns.NetworkState.HEALTHY)
        self.assertFalse(decision.changed)
        self.assertIsNone(decision.entered)
        self.assertIsNone(decision.recovered_from)

    def test_reset_forgets_fault_and_metric_history(self) -> None:
        sentinel = ns.NetworkSentinel()
        for sample in LossFaultTests._loss_pattern():
            sentinel.update(sample)
        self.assertEqual(sentinel.active_fault, ns.NetworkState.LOSS)
        sentinel.reset()
        self.assertEqual(sentinel.current_state, ns.NetworkState.HEALTHY)
        self.assertIsNone(sentinel.active_fault)
        decision = sentinel.update(healthy())
        self.assertEqual((decision.loss_samples, decision.loss_failures), (1, 0))


class ProbeBoundaryTests(unittest.TestCase):
    def test_system_probe_short_circuits_after_router_failure(self) -> None:
        calls: list[str] = []

        def ping(host: str, timeout: float) -> ns.ProbeAttempt:
            calls.append(f"ping:{host}:{timeout}")
            return ns.ProbeAttempt(False)

        def forbidden(*args, **kwargs):  # type: ignore[no-untyped-def]
            self.fail("a downstream probe ran after the router failed")

        probe = ns.SystemProbe(
            ns.SystemProbeConfig("192.168.1.1", timeout_seconds=0.25),
            ping=ping,
            tcp=forbidden,
            dns=forbidden,
            portal=forbidden,
            clock=lambda: 123.0,
        )
        sample = probe.sample()
        self.assertEqual(calls, ["ping:192.168.1.1:0.25"])
        self.assertEqual(
            sample,
            ns.ProbeSample(False, None, None, None, None, 123.0),
        )

    def test_system_probe_builds_a_complete_login_sample_from_injected_reads(self) -> None:
        calls: list[str] = []

        def ping(host: str, timeout: float) -> ns.ProbeAttempt:
            calls.append("router")
            return ns.ProbeAttempt(True, 1.0)

        def tcp(host: str, port: int, timeout: float) -> ns.ProbeAttempt:
            calls.append("wan")
            return ns.ProbeAttempt(True, 37.5)

        def dns(host: str, timeout: float) -> bool:
            calls.append("dns")
            return True

        def portal(url: str, **kwargs) -> ns.PortalAttempt:  # type: ignore[no-untyped-def]
            calls.append("portal")
            self.assertEqual(kwargs["expected_status"], 200)
            self.assertEqual(kwargs["expected_body"], b"Microsoft Connect Test")
            return ns.PortalAttempt(True, True)

        probe = ns.SystemProbe(
            ns.SystemProbeConfig("192.168.1.1"),
            ping=ping,
            tcp=tcp,
            dns=dns,
            portal=portal,
            clock=lambda: 456.0,
        )
        sample = probe.sample()
        self.assertEqual(calls, ["router", "wan", "dns", "portal"])
        self.assertEqual(sample, ns.ProbeSample(True, True, True, True, 37.5, 456.0))

    def test_dns_and_portal_checks_use_their_independent_cadences(self) -> None:
        moments = iter((0.0, 5.0, 15.0, 30.0))
        dns = Mock(return_value=True)
        portal = Mock(return_value=ns.PortalAttempt(True, False))
        probe = ns.SystemProbe(
            ns.SystemProbeConfig(
                "192.168.1.1",
                dns_interval_seconds=15,
                portal_interval_seconds=30,
            ),
            ping=Mock(return_value=ns.ProbeAttempt(True, 1.0)),
            tcp=Mock(return_value=ns.ProbeAttempt(True, 40.0)),
            dns=dns,
            portal=portal,
            clock=lambda: next(moments),
        )
        samples = [probe.sample() for _ in range(4)]
        self.assertTrue(all(sample.dns_ok for sample in samples))
        self.assertEqual(dns.call_count, 3)  # t=0, 15, 30
        self.assertEqual(portal.call_count, 2)  # t=0, 30

    def test_probe_configuration_rejects_unbounded_or_invalid_inputs(self) -> None:
        with self.assertRaises(ValueError):
            ns.SystemProbeConfig("")
        with self.assertRaises(ValueError):
            ns.SystemProbeConfig("192.168.1.1", timeout_seconds=0)
        with self.assertRaises(ValueError):
            ns.SystemProbeConfig("192.168.1.1", wan_port=0)
        with self.assertRaises(ValueError):
            ns.ping_once("", 1.0)
        with self.assertRaises(ValueError):
            ns.dns_resolves("", 1.0)

    def test_two_raw_targets_require_both_to_fail(self) -> None:
        config = ns.SystemProbeConfig(
            router_host="192.168.1.1",
            wan_targets=(("1.1.1.1", 443), ("8.8.8.8", 53)),
        )
        tcp = Mock(
            side_effect=(
                ns.ProbeAttempt(False, None),
                ns.ProbeAttempt(True, 42.0),
            )
        )
        probe = ns.SystemProbe(
            config,
            ping=Mock(return_value=ns.ProbeAttempt(True, 1.0)),
            tcp=tcp,
            dns=Mock(return_value=True),
            portal=Mock(return_value=ns.PortalAttempt(True, False)),
            clock=Mock(return_value=5.0),
        )
        sample = probe.sample()
        self.assertTrue(sample.wan_ok)
        self.assertEqual(sample.latency_ms, 42.0)
        self.assertEqual(tcp.call_count, 2)

    def test_two_raw_targets_report_wan_only_when_both_fail(self) -> None:
        config = ns.SystemProbeConfig(
            router_host="192.168.1.1",
            wan_targets=(("1.1.1.1", 443), ("8.8.8.8", 53)),
        )
        dns = Mock()
        probe = ns.SystemProbe(
            config,
            ping=Mock(return_value=ns.ProbeAttempt(True, 1.0)),
            tcp=Mock(return_value=ns.ProbeAttempt(False, None)),
            dns=dns,
            portal=Mock(),
        )
        sample = probe.sample()
        self.assertFalse(sample.wan_ok)
        self.assertIsNone(sample.dns_ok)
        dns.assert_not_called()

    def test_default_gateway_discovery_is_bounded_and_validated(self) -> None:
        runner = Mock(return_value=Mock(returncode=0, stdout="192.168.1.1\n"))
        with patch.object(ns.os, "name", "nt"):
            self.assertEqual(
                ns.discover_default_gateway(1.25, runner=runner),
                "192.168.1.1",
            )
        command = runner.call_args.args[0]
        self.assertEqual(command[:3], ["powershell.exe", "-NoProfile", "-NonInteractive"])
        self.assertEqual(runner.call_args.kwargs["timeout"], 1.25)
        self.assertFalse(runner.call_args.kwargs["shell"])

    def test_default_gateway_discovery_fails_closed(self) -> None:
        with patch.object(ns.os, "name", "nt"):
            for result in (
                Mock(returncode=1, stdout="192.168.1.1"),
                Mock(returncode=0, stdout="not-an-address"),
                Mock(returncode=0, stdout="0.0.0.0"),
            ):
                with self.subTest(result=result):
                    self.assertIsNone(
                        ns.discover_default_gateway(runner=Mock(return_value=result))
                    )


if __name__ == "__main__":
    unittest.main()
