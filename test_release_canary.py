"""Offline guards for the controlled live release canary."""

from __future__ import annotations

import unittest
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from next_up import EventStage, classify_event, events_from_ics, select_event
import release_canary as canary


PACIFIC = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 8, 23, 20, 0, tzinfo=PACIFIC)


class AnimationHeapGateTests(unittest.TestCase):
    """The gate that is supposed to stop a full-width GIF crashing the panel.

    A flat 18,000-byte gate was tried and is not a safety margin: on 2026-08-23
    a 7,010-byte animation was pushed with 22,516 bytes free and the panel
    answered with resetReason "panic" -- a hard crash mid-push that took the
    rotation and every pushed app with it.
    """

    def test_the_requirement_scales_with_the_artwork(self) -> None:
        small = canary.required_block_for_animation(1_000)
        large = canary.required_block_for_animation(7_000)
        self.assertGreater(large, small * 6)

    def test_the_case_that_actually_panicked_is_now_refused(self) -> None:
        # 7,010 bytes of artwork against the 22,516 bytes that were free.
        self.assertGreater(canary.required_block_for_animation(7_010), 22_516)

    def test_the_ratio_covers_the_worst_measurement(self) -> None:
        # 1,246 B / 8 frames cost 10,240 bytes of contiguous block.
        self.assertGreaterEqual(canary.required_block_for_animation(1_246), 10_240)

    def test_the_real_next_up_payload_is_sized_from_the_real_artwork(self) -> None:
        size = canary.next_up_animated_bytes()
        payload = canary.nextup_art.build_payload(title='CANARY', minutes_until=14,
            urgency=canary.nextup_art.Urgency.SOON, animated=True, lifetime_ms=60_000)
        self.assertEqual(size, len(json.dumps(payload).encode('utf-8')))
        self.assertEqual(
            canary.required_block_for_animation(size),
            int(size * canary.ANIMATION_BLOCK_RATIO),
        )

    def test_nothing_is_free(self) -> None:
        self.assertEqual(canary.required_block_for_animation(0), 0)
        self.assertEqual(canary.required_block_for_animation(-5), 0)


class ReleaseCanaryTests(unittest.TestCase):
    def test_default_offline_canary_builds_real_bounded_art_and_calendar(self) -> None:
        result = canary.offline_canary(NOW)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["mode"], "offline")
        self.assertEqual(result["syntheticEvents"], 1)
        self.assertLess(result["networkAnimatedBytes"], 2_048)
        self.assertLess(result["networkStaticBytes"], 2_048)
        self.assertLess(result["nextUpBytes"], 8_192)

    def test_synthetic_event_is_truthfully_in_the_fifteen_minute_stage(self) -> None:
        body = canary.build_synthetic_ics(NOW, minutes_until=14)
        events = events_from_ics(body, NOW, timezone=PACIFIC, source_id="canary")
        selected = select_event(events, NOW)
        self.assertIsNotNone(selected)
        self.assertEqual(classify_event(selected, NOW), (EventStage.FIFTEEN, 14))

    def test_synthetic_calendar_contains_no_real_domain_or_private_title(self) -> None:
        text = canary.build_synthetic_ics(NOW).decode("ascii")
        self.assertIn("example.invalid", text)
        self.assertIn("synthetic release canary", text)
        self.assertNotIn("http://", text)
        self.assertNotIn("https://", text)

    def test_canary_clock_advances_wall_epoch_and_monotonic_together(self) -> None:
        clock = canary.CanaryClock(NOW)
        clock.advance(90)
        self.assertEqual(clock.monotonic(), 90)
        self.assertEqual(clock.wall.timestamp(), NOW.timestamp() + 90)
        self.assertEqual(clock.time(), clock.wall.timestamp())

    def test_synthetic_network_sequence_uses_the_real_probe_contract(self) -> None:
        classifier = canary.sentinel.NetworkSentinel()
        decisions = [classifier.update(canary._network_sample(200.0)) for _ in range(6)]
        self.assertTrue(all(item.state is canary.sentinel.NetworkState.HEALTHY for item in decisions[:5]))
        self.assertIs(decisions[-1].state, canary.sentinel.NetworkState.SLOW)


if __name__ == "__main__":
    unittest.main()
