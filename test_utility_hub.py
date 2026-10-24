"""Configuration contract tests for the shared AWTRIX Utility Hub."""

from __future__ import annotations

from datetime import datetime
import json
import os
from types import SimpleNamespace
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import desk_companion
import network_sentinel as sentinel
import utility_hub as hub


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class SequenceProbe:
    def __init__(self, samples: list[sentinel.ProbeSample]) -> None:
        self.samples = samples

    def sample(self) -> sentinel.ProbeSample:
        if not self.samples:
            raise AssertionError("test probe ran out of samples")
        return self.samples.pop(0)


class FakePanel:
    def __init__(self) -> None:
        self.pushes: list[tuple[str, dict, dict | None]] = []
        self.activations: list[str] = []
        self.deletes: list[str] = []
        self.health_probes = 0
        self.fail_pushes = 0

    def push_app(self, name: str, payload: dict, *, static_payload: dict | None = None) -> None:
        if self.fail_pushes:
            self.fail_pushes -= 1
            raise RuntimeError("panel offline")
        self.pushes.append((name, payload, static_payload))

    def activate_app(self, name: str, **_kwargs: object) -> SimpleNamespace:
        self.activations.append(name)
        return SimpleNamespace(sent=True)

    def delete_pushed_app(self, name: str) -> bool:
        self.deletes.append(name)
        return True

    def probe_device(self) -> SimpleNamespace:
        self.health_probes += 1
        return SimpleNamespace(uptime_seconds=100)


def sample(
    *,
    router: bool = True,
    wan: bool | None = True,
    dns: bool | None = True,
    login: bool | None = False,
    latency: float | None = 40.0,
) -> sentinel.ProbeSample:
    return sentinel.ProbeSample(router, wan, dns, login, latency, 0.0)


class UtilityHubConfigTests(unittest.TestCase):
    def test_default_config_matches_the_current_panel_and_nightkeeper(self) -> None:
        value = hub.load_config(Path("does-not-exist-utility-hub.json"))
        self.assertEqual(value.panel_url, "http://192.168.1.90")
        self.assertEqual((value.quiet_start_minute, value.quiet_end_minute), (1290, 420))
        self.assertEqual(len(value.network.raw_targets), 2)
        self.assertEqual(value.network.alert_refresh_seconds, 15)
        self.assertEqual(value.network.alert_lifetime_seconds, 45)
        self.assertTrue(value.focus.enabled)
        self.assertEqual(value.focus.preset, "classic")
        self.assertEqual(value.focus.motion, "calm")

    def test_example_config_is_valid_and_round_trips_all_network_fields(self) -> None:
        path = Path(__file__).with_name("utility_hub.example.json")
        value = hub.load_config(path)
        self.assertEqual(value.network.gateway, None)
        self.assertEqual(
            [(item.host, item.port) for item in value.network.raw_targets],
            [("1.1.1.1", 443), ("8.8.8.8", 53)],
        )
        self.assertEqual(value.network.connectivity_expected, "Microsoft Connect Test")
        self.assertEqual(value.focus.refresh_seconds, 5)
        self.assertEqual(value.focus.static_refresh_seconds, 15)
        self.assertEqual(value.focus.lifetime_seconds, 20)
        self.assertEqual(value.focus.paused_visibility_seconds, 1800)
        self.assertFalse(value.next_up.enabled)
        self.assertEqual(value.next_up.sources, ())
        self.assertEqual(value.next_up.timezone_name, "America/Los_Angeles")
        self.assertEqual(
            dict(value.next_up.aliases),
            {"Dentist appointment": "DENTIST", "Team stand-up": "STANDUP"},
        )

    def test_empty_mapping_uses_the_same_valid_defaults(self) -> None:
        self.assertEqual(hub.config_from_mapping({}), hub.HubConfig())

    def test_config_path_lives_under_local_app_data(self) -> None:
        with patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Temp\Profile"}):
            self.assertEqual(
                hub.default_config_path(),
                Path(r"C:\Temp\Profile") / "AWTRIX" / "utility_hub.json",
            )

    def test_invalid_times_targets_and_lifetimes_fail_closed(self) -> None:
        cases = (
            {"quiet_hours": {"start": "25:00"}},
            {"network_sentinel": {"raw_targets": [{"host": "1.1.1.1", "port": 443}]}},
            {
                "network_sentinel": {
                    "alert_refresh_seconds": 30,
                    "alert_lifetime_seconds": 45,
                }
            },
            {"next_up": {"sources": "private.ics"}},
            {"next_up": {"timezone": "Mars/Olympus_Mons"}},
            {"next_up": {"aliases": {"Meeting": ""}}},
            {"focus_sprint": {"preset": "magic"}},
            {"focus_sprint": {"motion": "frenetic"}},
            {
                "focus_sprint": {
                    "static_refresh_seconds": 20,
                    "lifetime_seconds": 20,
                }
            },
        )
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    hub.config_from_mapping(value)

    def test_malformed_json_names_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "bad.json"):
                hub.load_config(path)

    def test_example_has_no_private_calendar_or_authentication_material(self) -> None:
        body = json.loads(Path("utility_hub.example.json").read_text(encoding="utf-8"))
        serialized = json.dumps(body).lower()
        for secret_word in ("password", "token", "secret", "calendar_url"):
            self.assertNotIn(secret_word, serialized)

    def test_webcal_links_are_read_as_https(self) -> None:
        config = hub.config_from_mapping(
            {"next_up": {"sources": ["webcal://p01-caldav.icloud.com/published/2/abc"]}}
        )
        self.assertEqual(config.next_up.sources, ("https://p01-caldav.icloud.com/published/2/abc",))
        with self.assertRaises(ValueError):
            hub.config_from_mapping({"next_up": {"sources": ["ftp://example.invalid/cal.ics"]}})

    def test_next_up_sources_are_hidden_from_config_repr(self) -> None:
        config = hub.config_from_mapping(
            {"next_up": {"sources": ["https://calendar.invalid/private-token.ics"]}}
        )
        self.assertNotIn("private-token", repr(config))


class NewAppConfigTests(unittest.TestCase):
    def test_new_apps_are_off_unless_enabled(self) -> None:
        config = hub.config_from_mapping({})
        self.assertFalse(config.planes.enabled or config.quakes.enabled or config.game_mode.enabled)

    def test_new_app_settings_parse(self) -> None:
        config = hub.config_from_mapping({
            "planes": {"enabled": True, "radius_nm": 4, "include_light_aircraft": True},
            "quakes": {"enabled": True, "min_magnitude": 3.0},
            "game_mode": {"enabled": True, "extra_games": ["osu!.exe"]},
        })
        self.assertEqual((config.planes.radius_nm, config.planes.include_light_aircraft), (4, True))
        self.assertEqual(config.quakes.min_magnitude, 3.0)
        self.assertEqual(config.game_mode.extra_games, ("osu!.exe",))

    def test_extra_games_must_be_names(self) -> None:
        with self.assertRaises(ValueError):
            hub.config_from_mapping({"game_mode": {"extra_games": "osu!.exe"}})


class NetworkSentinelControllerTests(unittest.TestCase):
    def controller(
        self,
        samples: list[sentinel.ProbeSample],
        *,
        config: hub.NetworkConfig | None = None,
        panel: FakePanel | None = None,
    ) -> tuple[hub.NetworkSentinelController, FakeClock, FakePanel]:
        clock = FakeClock()
        fake_panel = panel or FakePanel()
        controller = hub.NetworkSentinelController(
            config or hub.NetworkConfig(),
            SequenceProbe(samples),
            fake_panel,  # type: ignore[arg-type]
            clock=clock,
            logger=Mock(),
        )
        return controller, clock, fake_panel

    def test_healthy_startup_performs_no_panel_mutation(self) -> None:
        controller, _, panel = self.controller([sample()])
        result = controller.tick()
        self.assertEqual(result.decision.state, sentinel.NetworkState.HEALTHY)
        self.assertFalse(result.pushed)
        self.assertEqual(panel.pushes, [])
        self.assertEqual(panel.activations, [])
        self.assertEqual(panel.deletes, [])

    def test_confirmed_outage_pushes_and_activates_exactly_once(self) -> None:
        down = sample(router=False, wan=None, dns=None, login=None, latency=None)
        controller, clock, panel = self.controller([down, down, down, down, down])
        self.assertFalse(controller.tick().pushed)
        clock.now = 5
        self.assertFalse(controller.tick().pushed)
        clock.now = 10
        entered = controller.tick()
        self.assertTrue(entered.pushed)
        self.assertTrue(entered.activated)
        self.assertEqual(len(panel.pushes), 1)
        self.assertEqual(panel.activations, [hub.NETWORK_APP_NAME])

        clock.now = 20
        self.assertFalse(controller.tick().pushed)
        clock.now = 25
        refreshed = controller.tick()
        self.assertTrue(refreshed.pushed)
        self.assertFalse(refreshed.activated)
        self.assertEqual(len(panel.activations), 1)

    def test_push_supplies_animated_and_static_safe_payloads(self) -> None:
        down = sample(router=False, wan=None, dns=None, login=None, latency=None)
        controller, clock, panel = self.controller([down, down, down])
        controller.tick()
        clock.now = 5
        controller.tick()
        clock.now = 10
        controller.tick()
        _, animated, static = panel.pushes[0]
        self.assertIsNotNone(static)
        self.assertNotEqual(animated["icon"], static["icon"])
        self.assertLess(len(json.dumps(animated).encode("utf-8")), 2048)
        self.assertLess(len(json.dumps(static).encode("utf-8")), 2048)

    def test_recovery_shows_back_then_deletes_after_twelve_seconds(self) -> None:
        down = sample(router=False, wan=None, dns=None, login=None, latency=None)
        values = [down] * 3 + [sample()] * 8
        config = hub.NetworkConfig(
            alert_refresh_seconds=100,
            alert_lifetime_seconds=300,
            recovery_seconds=12,
        )
        controller, clock, panel = self.controller(values, config=config)
        for index in range(3):
            clock.now = index * 5
            controller.tick()
        for index in range(5):
            clock.now = 15 + index * 5
            self.assertFalse(controller.tick().decision.is_recovery)
        clock.now = 40
        recovered = controller.tick()
        self.assertTrue(recovered.decision.is_recovery)
        self.assertTrue(recovered.pushed)
        self.assertTrue(recovered.activated)

        clock.now = 45
        self.assertFalse(controller.tick().deleted)
        clock.now = 52
        deleted = controller.tick()
        self.assertTrue(deleted.deleted)
        self.assertEqual(panel.deletes, [hub.NETWORK_APP_NAME])

    def test_panel_failure_backoff_prevents_a_retry_storm(self) -> None:
        down = sample(router=False, wan=None, dns=None, login=None, latency=None)
        panel = FakePanel()
        panel.fail_pushes = 2
        controller, clock, panel = self.controller([down] * 5, panel=panel)
        controller.tick()
        clock.now = 5
        controller.tick()
        clock.now = 10
        self.assertFalse(controller.tick().pushed)
        self.assertEqual(controller.panel_failures, 1)
        clock.now = 10.5
        self.assertFalse(controller.tick().pushed)
        self.assertEqual(controller.panel_failures, 1)
        clock.now = 11
        self.assertFalse(controller.tick().pushed)
        self.assertEqual(controller.panel_failures, 2)
        self.assertEqual(controller.next_panel_attempt_at, 13)

    def test_pc_offline_is_a_confirmed_router_fault_only(self) -> None:
        router_down = sample(router=False, wan=None, dns=None, login=None, latency=None)
        wan_down = sample(wan=False, dns=None, login=None, latency=None)
        controller, clock, _ = self.controller([router_down] * 3 + [sample()] * 6 + [wan_down] * 3)
        states = []
        for index in range(12):
            clock.now = index * 5
            controller.tick()
            states.append(controller.pc_offline())
        # Three samples to confirm, six healthy ones to clear; WAN down is not "no network".
        self.assertEqual(states, [False, False, True, True, True, True, True, True, False,
                                  False, False, False])

    def test_panel_health_probe_runs_at_most_every_thirty_seconds(self) -> None:
        controller, clock, panel = self.controller([sample(), sample(), sample()])
        controller.tick()
        clock.now = 29.9
        controller.tick()
        clock.now = 30
        controller.tick()
        self.assertEqual(panel.health_probes, 2)

    def test_build_probe_uses_discovery_and_both_raw_targets(self) -> None:
        with patch.object(sentinel, "discover_default_gateway", return_value="192.168.1.254"):
            gateway, probe = hub.build_probe(hub.NetworkConfig())
        self.assertEqual(gateway, "192.168.1.254")
        self.assertEqual(
            probe.config.effective_wan_targets,
            (("1.1.1.1", 443), ("8.8.8.8", 53)),
        )

    def test_network_builder_reuses_the_supplied_shared_panel(self) -> None:
        panel = FakePanel()
        clock = FakeClock()
        config = hub.HubConfig(
            network=hub.NetworkConfig(gateway="192.168.1.1")
        )
        controller = hub.build_controller(
            config,
            clock=clock,  # type: ignore[arg-type]
            panel=panel,  # type: ignore[arg-type]
            logger=Mock(),
        )
        self.assertIs(controller.panel, panel)
        self.assertIs(controller.clock, clock)


class NextUpBuilderTests(unittest.TestCase):
    def test_next_up_builder_reuses_panel_and_maps_configuration(self) -> None:
        panel = FakePanel()
        clock = FakeClock()
        config = hub.HubConfig(
            next_up=hub.NextUpConfig(
                enabled=True,
                sources=("private-calendar.ics",),
                timezone_name="America/Los_Angeles",
                include_all_day=True,
                aliases=(("Team stand-up", "STANDUP"),),
                refresh_seconds=240,
                cache_max_age_seconds=7_200,
            )
        )
        controller = hub.build_next_up_controller(
            config,
            panel,  # type: ignore[arg-type]
            clock=clock,  # type: ignore[arg-type]
            logger=Mock(),
        )
        self.assertIs(controller.panel, panel)
        self.assertIs(controller.clock, clock)
        self.assertTrue(controller.include_all_day)
        self.assertEqual(controller.policy.refresh_seconds, 240)
        self.assertEqual(controller.policy.lifetime_seconds, 180)

    def test_next_up_builder_refuses_an_enabled_empty_source_set(self) -> None:
        with self.assertRaisesRegex(ValueError, "no calendar sources"):
            hub.build_next_up_controller(
                hub.HubConfig(next_up=hub.NextUpConfig(enabled=True)),
                FakePanel(),  # type: ignore[arg-type]
                clock=FakeClock(),  # type: ignore[arg-type]
            )


class FocusBuilderTests(unittest.TestCase):
    def test_focus_builder_reuses_panel_and_maps_configuration(self) -> None:
        panel = FakePanel()
        clock = FakeClock()
        config = hub.HubConfig(
            focus=hub.FocusConfig(
                enabled=True,
                preset="sprint",
                motion="calm",
                refresh_seconds=4.0,
                static_refresh_seconds=12.0,
                lifetime_seconds=25.0,
                paused_visibility_seconds=1800.0,
            )
        )
        controller = hub.build_focus_controller(
            config,
            panel,  # type: ignore[arg-type]
            clock=clock,  # type: ignore[arg-type]
            logger=Mock(),
        )
        self.assertIs(controller.panel, panel)
        self.assertEqual(controller.policy.motion, "calm")
        self.assertEqual(controller.policy.refresh_seconds, 4.0)
        self.assertEqual(controller.policy.static_refresh_seconds, 12.0)
        self.assertEqual(controller.policy.lifetime_seconds, 25.0)
        self.assertEqual(controller.policy.paused_visibility_seconds, 1800.0)

    def test_build_meeting_controller_and_github_controller(self) -> None:
        panel = FakePanel()
        clock = FakeClock()
        config = hub.HubConfig(
            meeting=hub.MeetingConfig(enabled=True, refresh_seconds=2.0, lifetime_seconds=20.0),
            github=hub.GitHubHubConfig(
                enabled=True,
                repos=("test/repo",),
                token="test-token",
                poll_interval_seconds=45.0,
            ),
        )
        meeting_ctrl = hub.build_meeting_controller(config, panel, clock=clock)  # type: ignore[arg-type]
        self.assertIs(meeting_ctrl.panel, panel)
        self.assertEqual(meeting_ctrl.policy.refresh_seconds, 2.0)
        self.assertEqual(meeting_ctrl.policy.lifetime_seconds, 20.0)

        github_ctrl = hub.build_github_controller(config, panel, clock=clock)  # type: ignore[arg-type]
        self.assertIs(github_ctrl.panel, panel)
        self.assertEqual(github_ctrl.config.repos, ("test/repo",))
        self.assertEqual(github_ctrl.config.poll_interval_seconds, 45.0)

        companion_ctrl = hub.build_companion_controller(config, panel, clock=clock)  # type: ignore[arg-type]
        self.assertIs(companion_ctrl.panel, panel)
        self.assertEqual(companion_ctrl.policy.refresh_seconds, 240.0)

        commute_ctrl = hub.build_commute_controller(config, panel, clock=clock)  # type: ignore[arg-type]
        self.assertIs(commute_ctrl.panel, panel)
        self.assertEqual(commute_ctrl.config.refresh_seconds, 60.0)



class UtilityHubRunnerTests(unittest.TestCase):
    def test_companion_only_hub_keeps_thirty_second_health_checks(self) -> None:
        config = hub.HubConfig(
            network=hub.NetworkConfig(enabled=False),
            focus=hub.FocusConfig(enabled=False),
            meeting=hub.MeetingConfig(enabled=False),
            companion=hub.CompanionHubConfig(enabled=True, refresh_seconds=240),
        )
        with (
            patch.object(hub, "parse_args", return_value=SimpleNamespace(config=Path("unused"), once=False)),
            patch.object(hub, "load_config", return_value=config),
            patch.object(hub, "configure_logging", return_value=Mock()),
            patch.object(hub, "build_panel_client", return_value=Mock()),
            patch.object(hub, "build_companion_controller", return_value=Mock()),
            patch.object(hub, "UtilityHubRunner") as runner,
        ):
            hub.main()
        self.assertEqual(runner.call_args.kwargs["interval_seconds"], 30)
        services = runner.call_args.args[0]
        self.assertEqual(len(services), 3)
        self.assertIsInstance(services[-2], hub.hub_status.HubStatusReporter)
        self.assertIsInstance(services[-1], hub.panel_down_alert.PanelDownAlert)

    def _main_services(self, config: hub.HubConfig) -> list:
        with (
            patch.object(hub, "parse_args", return_value=SimpleNamespace(config=Path("unused"), once=False)),
            patch.object(hub, "load_config", return_value=config),
            patch.object(hub, "configure_logging", return_value=Mock()),
            patch.object(hub, "build_panel_client", return_value=Mock()),
            patch.object(hub, "build_controller", return_value=Mock()),
            patch.object(hub, "build_focus_controller", return_value=Mock()),
            patch.object(hub, "build_meeting_controller", return_value=Mock()),
            patch.object(hub, "UtilityHubRunner") as runner,
        ):
            hub.main()
        return list(runner.call_args.args[0])

    def test_panel_down_alert_runs_right_after_the_status_reporter_and_reads_it(self) -> None:
        config = hub.config_from_mapping({
            "panel_down_alert": {"after_minutes": 15, "min_gap_minutes": 60},
            "game_mode": {"extra_games": ["osu!.exe"]},
        })
        services = self._main_services(config)
        reporters = [s for s in services if isinstance(s, hub.hub_status.HubStatusReporter)]
        alerts = [s for s in services if isinstance(s, hub.panel_down_alert.PanelDownAlert)]
        self.assertEqual((len(reporters), len(alerts)), (1, 1))
        self.assertEqual(services.index(alerts[0]), services.index(reporters[0]) + 1)
        self.assertIs(services[-1], alerts[0])
        self.assertIs(alerts[0].reporter, reporters[0])
        self.assertEqual(alerts[0].status_path, reporters[0].path)
        self.assertEqual((alerts[0].after_seconds, alerts[0].min_gap_seconds), (900.0, 3600.0))
        self.assertEqual(alerts[0].extra_games, ("osu!.exe",))

    def test_the_status_reporter_asks_the_network_sentinel_whether_this_pc_is_offline(self) -> None:
        network = Mock()
        with patch.object(hub, "build_controller", return_value=network) as build:
            services = self._main_services_with(hub.config_from_mapping({}), build)
        [reporter] = [s for s in services if isinstance(s, hub.hub_status.HubStatusReporter)]
        self.assertIs(services[0], network)
        self.assertEqual(reporter.pc_offline_check, network.pc_offline)
        network.pc_offline.return_value = True
        self.assertIs(reporter._sample_pc_offline(), True)
        off = hub.config_from_mapping({"network_sentinel": {"enabled": False},
                                       "focus_sprint": {"enabled": True}})
        [reporter] = [s for s in self._main_services(off)
                      if isinstance(s, hub.hub_status.HubStatusReporter)]
        self.assertIsNone(reporter.pc_offline_check)

    def _main_services_with(self, config: hub.HubConfig, build: Mock) -> list:
        with (
            patch.object(hub, "parse_args", return_value=SimpleNamespace(config=Path("unused"), once=False)),
            patch.object(hub, "load_config", return_value=config),
            patch.object(hub, "configure_logging", return_value=Mock()),
            patch.object(hub, "build_panel_client", return_value=Mock()),
            patch.object(hub, "build_focus_controller", return_value=Mock()),
            patch.object(hub, "build_meeting_controller", return_value=Mock()),
            patch.object(hub, "UtilityHubRunner") as runner,
        ):
            hub.main()
        build.assert_called_once()
        return list(runner.call_args.args[0])

    def test_disabled_panel_down_alert_is_not_built(self) -> None:
        config = hub.config_from_mapping({"panel_down_alert": {"enabled": False}})
        services = self._main_services(config)
        self.assertFalse(any(isinstance(s, hub.panel_down_alert.PanelDownAlert) for s in services))
        self.assertIsInstance(services[-1], hub.hub_status.HubStatusReporter)

    def test_one_service_failure_does_not_stop_the_next_service(self) -> None:
        failing = Mock()
        failing.tick.side_effect = RuntimeError("broken")
        healthy = Mock()
        healthy.tick.return_value = "ok"
        logger = Mock()
        runner = hub.UtilityHubRunner(
            [failing, healthy],
            clock=FakeClock(),
            logger=logger,
        )
        self.assertEqual(runner.tick(), [None, "ok"])
        healthy.tick.assert_called_once()
        logger.exception.assert_called_once()

    def test_runner_rejects_an_empty_service_set(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one"):
            hub.UtilityHubRunner([])


class CompanionHubTests(unittest.TestCase):
    def test_companion_config_parsing(self) -> None:
        defaults = hub.CompanionHubConfig()
        self.assertFalse(defaults.enabled)
        self.assertEqual(defaults.refresh_seconds, 240.0)

        cfg = hub.config_from_mapping({
            "companion": {
                "enabled": True,
                "refresh_seconds": 25.0,
            }
        })
        self.assertTrue(cfg.companion.enabled)
        self.assertEqual(cfg.companion.refresh_seconds, 25.0)

    def test_build_companion_controller_wires_reactive_detector(self) -> None:
        panel = FakePanel()
        clock = Mock()
        clock.wall_now.return_value = datetime(2026, 10, 2, 23, 0)
        config = hub.HubConfig(
            companion=hub.CompanionHubConfig(enabled=True, refresh_seconds=10.0),
            quiet_start_minute=21 * 60 + 30,
            quiet_end_minute=7 * 60,
        )
        ctrl = hub.build_companion_controller(config, panel, clock=clock)  # type: ignore[arg-type]
        self.assertTrue(callable(ctrl.mood_detector))

        # During quiet hours -> SLEEP
        mood, label = ctrl.mood_detector()
        self.assertEqual(mood, desk_companion.CompanionMood.SLEEP)
        self.assertEqual(label, "SLEEP")

        # Outside quiet hours -> IDLE (when isolated from ambient system files)
        clock.wall_now.return_value = datetime(2026, 10, 2, 14, 0)
        with tempfile.TemporaryDirectory() as empty_dir:
            with patch("focus_control.default_data_dir", return_value=Path(empty_dir)):
                with patch("meeting_sentinel.scan_windows_microphone_registry", return_value=None):
                    mood2, label2 = ctrl.mood_detector()
                    self.assertEqual(mood2, desk_companion.CompanionMood.IDLE)
                    self.assertEqual(label2, "READY")

    def test_companion_controller_instantiation_with_policy(self) -> None:
        panel = FakePanel()
        config = hub.HubConfig(
            companion=hub.CompanionHubConfig(enabled=True, refresh_seconds=20.0),
        )
        ctrl = hub.build_companion_controller(config, panel)  # type: ignore[arg-type]
        self.assertIs(ctrl.panel, panel)
        self.assertTrue(ctrl.policy.enabled)
        self.assertEqual(ctrl.policy.refresh_seconds, 20.0)
        self.assertTrue(callable(ctrl.mood_detector))


if __name__ == "__main__":
    unittest.main()
