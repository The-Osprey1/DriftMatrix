"""Tests for the persistent AWTRIX rotation policy."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

import rotation_policy as policy


class RotationPolicyTests(unittest.TestCase):
    def test_target_order_is_the_exact_release_contract(self) -> None:
        self.assertEqual(
            policy.TARGET_ORDER,
            (
                "Time",
                "weatherbrief",
                "nowplaying",
                "Time",
                "ocean",
                "moon",
                "aqibrief",
                "pcstats0",
                "networksentinel",
                "nextup",
                "focus",
                "nightkeeper",
            ),
        )

    def test_time_is_the_only_duplicate_and_anchors_both_halves(self) -> None:
        self.assertEqual(policy.TARGET_ORDER.count("Time"), 2)
        self.assertEqual(
            {name for name in policy.TARGET_ORDER if policy.TARGET_ORDER.count(name) > 1},
            {"Time"},
        )
        self.assertEqual(policy.TARGET_ORDER[0], "Time")
        self.assertLess(policy.TARGET_ORDER.index("weatherbrief"), policy.TARGET_ORDER.index("moon"))
        self.assertLess(policy.TARGET_ORDER.index("ocean"), policy.TARGET_ORDER.index("moon"))

    def test_duplicate_time_is_coupled_to_short_predictable_timing(self) -> None:
        self.assertEqual(policy.TARGET_SETTINGS["appDurationMs"], 8_000)
        self.assertEqual(policy.TARGET_SETTINGS["transitionEffect"], "Dim")
        self.assertEqual(policy.TARGET_SETTINGS["transitionDurationMs"], 360)

    def test_ambient_contextual_and_future_apps_have_stable_slots(self) -> None:
        for name in (
            "ocean",
            "nowplaying",
            "aqibrief",
            "pcstats0",
            "networksentinel",
            "nextup",
            "focus",
        ):
            self.assertIn(name, policy.TARGET_ORDER)
        self.assertEqual(policy.TARGET_ORDER[-1], "nightkeeper")

    def test_the_calm_page_still_follows_the_second_time_anchor(self) -> None:
        """Ocean took meridian's place; the shape of the sequence is the point.

        The second Time anchor is followed by the calm ambient page and then
        the celestial one, so the rotation still alternates between reading
        something and just looking at something.
        """
        time_positions = [
            index for index, name in enumerate(policy.TARGET_ORDER) if name == "Time"
        ]
        self.assertEqual(len(time_positions), 2)
        self.assertEqual(
            policy.TARGET_ORDER[time_positions[1] + 1 : time_positions[1] + 3],
            ("ocean", "moon"),
        )

    def test_retired_pages_stay_disabled(self) -> None:
        self.assertEqual(
            set(policy.TARGET_DISABLED),
            {"Battery", "Date", "timepiece", "pcstats1", "meridian", "sunset"},
        )

    def test_the_old_moon_replaced_sunset(self) -> None:
        """2026-10-04: the user chose the old moon tracker over sunset.

        Both cannot fit with ocean and nowplaying: the old moon alone costs
        21,162 B of script heap on 1.1.2.
        """
        self.assertIn("moon", policy.TARGET_ORDER)
        self.assertNotIn("sunset", policy.TARGET_ORDER)
        self.assertIn("sunset", policy.TARGET_DISABLED)

    def test_fetch_state_parses_saved_json(self) -> None:
        response = Mock()
        response.json.return_value = {"order": ["Time", "moon"], "disabled": ["Date"]}
        session = Mock()
        session.get.return_value = response
        result = policy.fetch_state(session, "http://panel")
        self.assertEqual(result, policy.LoopState(("Time", "moon"), ("Date",)))
        session.get.assert_called_once_with("http://panel/apploop.json", timeout=5)

    def test_apply_uses_exact_v1_order_endpoint_and_full_object(self) -> None:
        response = Mock(status_code=200)
        session = Mock()
        session.put.return_value = response
        policy.apply_state(session, base_url="http://panel/")
        session.put.assert_called_once_with(
            "http://panel/api/v1/apps/order",
            json=policy.TARGET_STATE.payload(),
            timeout=5,
        )

    def test_settings_are_read_and_patched_through_the_v1_settings_endpoint(self) -> None:
        response = Mock()
        response.json.return_value = {
            "appDurationMs": 15_000,
            "transitionEffect": "Random",
            "transitionDurationMs": 1_000,
        }
        session = Mock()
        session.get.return_value = response
        self.assertEqual(
            policy.fetch_rotation_settings(session, "http://panel"),
            {
                "appDurationMs": 15_000,
                "transitionEffect": "Random",
                "transitionDurationMs": 1_000,
            },
        )
        patch_response = Mock(status_code=200)
        session.patch.return_value = patch_response
        policy.apply_rotation_settings(session, base_url="http://panel/")
        session.patch.assert_called_once_with(
            "http://panel/api/v1/settings",
            json=policy.TARGET_SETTINGS,
            timeout=5,
        )

    def test_stability_preflight_reads_every_surface_repeatedly(self) -> None:
        responses = []
        for uptime in (120, 122, 124):
            device = Mock()
            device.json.return_value = {"uptimeSeconds": uptime, "resetReason": "panic"}
            responses.extend((device, Mock(), Mock(), Mock()))
        session = Mock()
        session.get.side_effect = responses

        with patch.object(policy.time, "sleep") as sleep:
            self.assertEqual(
                policy.verify_panel_stable(session, "http://panel/", samples=3, interval_seconds=2),
                (120, 122, 124),
            )

        expected_round = [
            call("http://panel/api/v1/device", timeout=5),
            call("http://panel/api/v1/apps", timeout=5),
            call("http://panel/api/v1/logs", timeout=5),
            call("http://panel/apploop.json", timeout=5),
        ]
        self.assertEqual(session.get.call_args_list, expected_round * 3)
        self.assertEqual(sleep.call_args_list, [call(2), call(2)])

    def test_stability_preflight_rejects_uptime_rollback(self) -> None:
        first = Mock()
        first.json.return_value = {"uptimeSeconds": 500}
        second = Mock()
        second.json.return_value = {"uptimeSeconds": 4}
        session = Mock()
        session.get.side_effect = [first, Mock(), Mock(), Mock(), second]
        with patch.object(policy.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "rebooted during deployment preflight"):
                policy.verify_panel_stable(session, samples=2, interval_seconds=0)

    def test_stability_preflight_requires_multiple_samples(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least two"):
            policy.verify_panel_stable(Mock(), samples=1)

    def test_backup_round_trips(self) -> None:
        state = policy.LoopState(("Time", "moon"), ("Date",))
        with tempfile.TemporaryDirectory() as folder:
            path = policy.save_backup(state, Path(folder))
            self.assertEqual(policy.LoopState.from_mapping(json.loads(path.read_text())), state)

    def test_stability_preflight_compares_first_sample_to_prior_uptime(self) -> None:
        device = Mock()
        device.json.return_value = {"uptimeSeconds": 4}
        session = Mock()
        session.get.return_value = device
        with self.assertRaisesRegex(RuntimeError, "500 -> 4"):
            policy.verify_panel_stable(
                session,
                samples=2,
                interval_seconds=0,
                minimum_uptime=500,
            )

    def test_apply_policy_backs_up_both_surfaces_before_first_write(self) -> None:
        current = policy.LoopState(("Time", "moon"), ("Date",))
        current_settings = {
            "appDurationMs": 15_000,
            "transitionEffect": "Random",
            "transitionDurationMs": 1_000,
        }
        events: list[str] = []
        with (
            patch.object(policy, "verify_panel_stable", side_effect=[(100, 102), (104, 106)]),
            patch.object(policy, "fetch_state", side_effect=[current, policy.TARGET_STATE]),
            patch.object(
                policy,
                "fetch_rotation_settings",
                side_effect=[current_settings, policy.TARGET_SETTINGS],
            ),
            patch.object(
                policy,
                "save_settings_backup",
                side_effect=lambda *_: events.append("settings-backup") or Path("settings.json"),
            ),
            patch.object(
                policy,
                "save_backup",
                side_effect=lambda *_: events.append("order-backup") or Path("order.json"),
            ),
            patch.object(
                policy,
                "apply_rotation_settings",
                side_effect=lambda *_: events.append("settings-write"),
            ),
            patch.object(
                policy,
                "apply_state",
                side_effect=lambda *_: events.append("order-write"),
            ),
            patch.object(policy.time, "sleep") as sleep,
        ):
            result = policy.apply_policy(
                Mock(),
                backup_dir=Path("backup"),
                health_samples=2,
                health_interval_seconds=0,
            )

        self.assertEqual(
            events,
            ["settings-backup", "order-backup", "settings-write", "order-write"],
        )
        sleep.assert_called_once_with(policy.MIN_MUTATION_SPACING_SECONDS)
        self.assertTrue(result.order_changed)
        self.assertTrue(result.settings_changed)
        self.assertEqual(result.postflight_uptimes, (104, 106))

    def test_failed_settings_readback_prevents_order_mutation(self) -> None:
        current = policy.LoopState(("Time",), ())
        old_settings = {
            "appDurationMs": 15_000,
            "transitionEffect": "Random",
            "transitionDurationMs": 1_000,
        }
        with (
            patch.object(policy, "verify_panel_stable", return_value=(100, 102)),
            patch.object(policy, "fetch_state", return_value=current),
            patch.object(policy, "fetch_rotation_settings", side_effect=[old_settings, old_settings]),
            patch.object(policy, "save_settings_backup", return_value=Path("settings.json")),
            patch.object(policy, "save_backup", return_value=Path("order.json")),
            patch.object(policy, "apply_rotation_settings"),
            patch.object(policy, "apply_state") as apply_order,
        ):
            with self.assertRaisesRegex(RuntimeError, "rotation-timing verification"):
                policy.apply_policy(Mock(), health_samples=2, health_interval_seconds=0)
        apply_order.assert_not_called()

    def test_unchanged_policy_has_no_backup_or_mutation(self) -> None:
        with (
            patch.object(policy, "verify_panel_stable", return_value=(100, 102)),
            patch.object(policy, "fetch_state", return_value=policy.TARGET_STATE),
            patch.object(policy, "fetch_rotation_settings", return_value=policy.TARGET_SETTINGS),
            patch.object(policy, "save_settings_backup") as save_settings,
            patch.object(policy, "save_backup") as save_order,
            patch.object(policy, "apply_rotation_settings") as apply_settings,
            patch.object(policy, "apply_state") as apply_order,
        ):
            result = policy.apply_policy(Mock(), health_samples=2, health_interval_seconds=0)
        save_settings.assert_not_called()
        save_order.assert_not_called()
        apply_settings.assert_not_called()
        apply_order.assert_not_called()
        self.assertFalse(result.order_changed)
        self.assertFalse(result.settings_changed)


if __name__ == "__main__":
    unittest.main()
