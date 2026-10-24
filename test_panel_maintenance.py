from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import panel_client as transport
import panel_maintenance as pm
import utility_hub as hub
from test_panel_client import FakeResponse, make_client

DAY_HOURS = 3600


class FakeClock:
    def __init__(self, t: float = 1_000.0) -> None:
        self.t = t

    def monotonic(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakePanel:
    """Plays a panel that restarts: uptime resets once reboot() is called."""

    def __init__(self, *, uptime: int = 7 * 24 * DAY_HOURS, power: bool = False,
                 errors: dict[str, str] | None = None, comes_back: bool = True,
                 dark_after_reads: int | None = 3) -> None:
        self.uptime = uptime
        self.power = power
        self.errors = errors or {}
        self.comes_back = comes_back
        # After a restart the matrix boots lit; Nightkeeper switches it off a
        # few device reads later.  None plays a Nightkeeper that never does.
        self.dark_after_reads = dark_after_reads
        self.reads_since_boot = 0
        self.reboots = 0
        self.down = False
        self.reads: list[str] = []

    def reboot(self) -> None:
        self.reboots += 1
        self.down = True

    def read_json(self, path: str, *, operation: str | None = None):
        self.reads.append(path)
        if self.down:
            if not self.comes_back:
                raise transport.PanelTransportError("unreachable")
            self.down = False
            self.uptime = 12
            self.power = True  # boots lit until Nightkeeper sees the clock
            self.reads_since_boot = 0
        if path == pm.DEVICE_PATH:
            self.reads_since_boot += 1
            if self.uptime < 100 and self.dark_after_reads is not None and self.reads_since_boot > self.dark_after_reads:
                self.power = False
            return {"uptimeSeconds": self.uptime, "largestFreeBlockBytes": 12_788 if self.uptime > 100 else 29_684,
                    "freeHeapBytes": 36_040, "matrixPower": self.power, "fps": 40,
                    "resetReason": "software" if self.reboots else "poweron"}
        if path == pm.DISPLAY_PATH:
            return {"power": self.power}
        if path == transport.APPS_PATH:
            return [{"name": n, "origin": "script", "present": True, "error": {"message": m}}
                    for n, m in self.errors.items()] + [
                {"name": "moon", "origin": "script", "present": True, "error": None}]
        if path == pm.LOGS_PATH:
            return {"lines": [
                "14:19:39 [script:ocean] vm heap +20299 bytes (shared 57610), free heap 69944 -> 55900, max block 49140",
                "14:19:39 [script:ocean] install low-water 47848 bytes",
                "14:20:14 [script:moon] vm heap +4136 bytes (shared 57938), free heap 56220 -> 55816, max block 32756",
            ]}
        raise AssertionError(path)


def run(controller: pm.MaintenanceController, clock: FakeClock, seconds: float, step: float = 5.0) -> None:
    end = clock.t + seconds
    while clock.t < end:
        controller.tick()
        clock.advance(step)


class Fixture(unittest.TestCase):
    def make(self, panel: FakePanel, wall: datetime = datetime(2026, 10, 4, 21, 45), *,
             clock: FakeClock | None = None, **config):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.report = Path(self.tmp.name) / "maintenance.json"
        self.clock = clock or FakeClock()
        self.wall = [wall]
        controller = pm.MaintenanceController(
            panel, pm.MaintenanceConfig(enabled=True, **config), clock=self.clock,
            wall_now=lambda: self.wall[0], report_path=self.report,
        )
        return controller

    def report_last(self) -> dict:
        return json.loads(self.report.read_text(encoding="utf-8"))["last"]


class DecisionTests(unittest.TestCase):
    def test_reboots_only_a_dark_panel_that_has_been_up_long_enough(self) -> None:
        cfg = pm.MaintenanceConfig(enabled=True)
        self.assertTrue(pm.should_reboot({"uptimeSeconds": 21 * DAY_HOURS}, {"power": False}, cfg)[0])
        self.assertFalse(pm.should_reboot({"uptimeSeconds": 5 * DAY_HOURS}, {"power": False}, cfg)[0])
        go, why = pm.should_reboot({"uptimeSeconds": 99 * DAY_HOURS}, {"power": True}, cfg)
        self.assertFalse(go)
        self.assertIn("lit", why)

    def test_a_missing_power_field_is_not_taken_as_dark(self) -> None:
        cfg = pm.MaintenanceConfig(enabled=True)
        self.assertFalse(pm.should_reboot({"uptimeSeconds": 99 * DAY_HOURS}, {}, cfg)[0])

    def test_window_wraps_midnight(self) -> None:
        self.assertTrue(pm.in_window(datetime(2026, 1, 1, 23, 30), 23 * 60, 60))
        self.assertTrue(pm.in_window(datetime(2026, 1, 1, 0, 30), 23 * 60, 60))
        self.assertFalse(pm.in_window(datetime(2026, 1, 1, 1, 0), 23 * 60, 60))

    def test_boot_log_gives_per_script_heap(self) -> None:
        heap = pm.parse_boot_heap(FakePanel().read_json(pm.LOGS_PATH)["lines"])
        self.assertEqual(heap["ocean"], {"vm_heap": 20_299, "max_block_after": 49_140})
        self.assertEqual(heap["moon"]["vm_heap"], 4_136)

    def test_empty_window_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            pm.MaintenanceConfig(window_start_minute=200, window_end_minute=200)


class ControllerTests(Fixture):
    def test_disabled_does_nothing(self) -> None:
        panel = FakePanel()
        controller = pm.MaintenanceController(panel, pm.MaintenanceConfig(enabled=False))
        self.assertEqual(controller.tick(), "disabled")
        self.assertEqual(panel.reads, [])

    def test_outside_the_window_it_never_touches_the_panel(self) -> None:
        panel = FakePanel()
        controller = self.make(panel, wall=datetime(2026, 10, 4, 14, 0))
        run(controller, self.clock, 3600)
        self.assertEqual((panel.reads, panel.reboots), ([], 0))

    def test_full_night_reboots_once_and_reports(self) -> None:
        panel = FakePanel()
        controller = self.make(panel)
        run(controller, self.clock, 600)
        self.assertEqual(panel.reboots, 1)
        self.assertEqual(controller.state, "idle")
        last = self.report_last()
        self.assertEqual(last["outcome"], "ok")
        self.assertEqual(last["before"]["largestFreeBlockBytes"], 12_788)
        self.assertEqual(last["after"]["largestFreeBlockBytes"], 29_684)
        self.assertEqual(last["script_heap"]["ocean"]["vm_heap"], 20_299)
        self.assertEqual(last["script_errors"], {})
        self.assertEqual(last["dark_after_boot_seconds"], 30)

    def test_panel_left_lit_after_boot_needs_attention(self) -> None:
        controller = self.make(FakePanel(dark_after_reads=None))
        run(controller, self.clock, 600)
        last = self.report_last()
        self.assertEqual(last["outcome"], "attention")
        self.assertNotIn("dark_after_boot_seconds", last)

    def test_only_one_attempt_per_night(self) -> None:
        panel = FakePanel()
        controller = self.make(panel)
        run(controller, self.clock, 3 * 3600)
        self.assertEqual(panel.reboots, 1)
        self.wall[0] = datetime(2026, 10, 5, 21, 45)
        panel.uptime = 30 * DAY_HOURS
        run(controller, self.clock, 900)
        self.assertEqual(panel.reboots, 2)

    def test_lit_panel_is_skipped_for_the_night_and_recorded(self) -> None:
        panel = FakePanel(power=True)
        controller = self.make(panel)
        run(controller, self.clock, 3 * 3600)
        self.assertEqual(panel.reboots, 0)
        self.assertEqual(self.report_last()["outcome"], "skipped")
        self.assertEqual(panel.reads.count(pm.DISPLAY_PATH), 1)

    def test_script_errors_after_boot_are_reported(self) -> None:
        panel = FakePanel(errors={"nowplaying": "out of memory"})
        controller = self.make(panel)
        run(controller, self.clock, 600)
        last = self.report_last()
        self.assertEqual(last["outcome"], "attention")
        self.assertEqual(last["script_errors"], {"nowplaying": "out of memory"})

    def test_panel_that_never_returns_is_logged_and_left_alone(self) -> None:
        panel = FakePanel(comes_back=False)
        controller = self.make(panel)
        with self.assertLogs("awtrix.maintenance", "ERROR"):
            run(controller, self.clock, 900)
        self.assertEqual(panel.reboots, 1)
        self.assertEqual(self.report_last()["outcome"], "no-return")
        self.assertEqual(controller.state, "idle")

    def test_unreadable_panel_skips_without_using_the_night(self) -> None:
        panel = FakePanel()
        calls = {"n": 0}
        original = panel.read_json

        def flaky(path, operation=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise transport.PanelTransportError("blip")
            return original(path, operation=operation)
        panel.read_json = flaky
        controller = self.make(panel)
        run(controller, self.clock, 900)
        self.assertEqual(panel.reboots, 1)

    def test_history_is_bounded(self) -> None:
        controller = self.make(FakePanel(power=True))
        for _ in range(pm.HISTORY_LIMIT + 5):
            controller._record({"outcome": "skipped"})
        data = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(len(data["history"]), pm.HISTORY_LIMIT)

    def test_panic_or_unknown_reset_is_attention_and_keeps_the_reported_reason(self) -> None:
        for reason in ("panic", "watchdog", None, ""):
            with self.subTest(reason=reason):
                panel = FakePanel()
                original = panel.read_json

                def read(path, operation=None):
                    body = original(path, operation=operation)
                    if path == pm.DEVICE_PATH and panel.reboots:
                        body["resetReason"] = reason
                    return body

                panel.read_json = read
                controller = self.make(panel)
                run(controller, self.clock, 600)
                last = self.report_last()
                self.assertEqual(last["outcome"], "attention")
                self.assertEqual(last["boot_reset_reason"], reason)
                self.assertEqual(last["after"]["resetReason"], reason)

    def test_missing_script_after_boot_cannot_pass_maintenance(self) -> None:
        for inventory in ([], [{"name": "moon", "origin": "script", "present": False, "error": None}]):
            with self.subTest(inventory=inventory):
                panel = FakePanel()
                original = panel.read_json

                def read(path, operation=None):
                    body = original(path, operation=operation)
                    return inventory if path == transport.APPS_PATH and panel.reboots else body

                panel.read_json = read
                controller = self.make(panel)
                run(controller, self.clock, 600)
                last = self.report_last()
                self.assertEqual(last["outcome"], "attention")
                self.assertEqual(last["expected_scripts"], ["moon"])
                self.assertEqual(last["missing_scripts"], ["moon"])

    def test_malformed_post_boot_readback_cannot_pass_maintenance(self) -> None:
        cases = [
            (pm.DEVICE_PATH, lambda body: {k: v for k, v in body.items() if k != "matrixPower"}),
            (pm.DEVICE_PATH, lambda body: dict(body, largestFreeBlockBytes=None)),
            (pm.DEVICE_PATH, lambda body: dict(body, fps=float("nan"))),
            (transport.APPS_PATH, lambda body: {}),
            (transport.APPS_PATH, lambda body: [None]),
            (transport.APPS_PATH, lambda body: [dict(body[0], present=None)]),
            (transport.APPS_PATH, lambda body: [dict(body[0], present="true")]),
            (pm.LOGS_PATH, lambda body: {}),
            (pm.LOGS_PATH, lambda body: {"lines": [None]}),
        ]
        for endpoint, mutate in cases:
            with self.subTest(endpoint=endpoint, mutate=mutate):
                panel = FakePanel()
                original = panel.read_json

                def read(path, operation=None):
                    body = original(path, operation=operation)
                    return mutate(body) if path == endpoint and panel.reboots else body

                panel.read_json = read
                controller = self.make(panel)
                run(controller, self.clock, 600)
                last = self.report_last()
                self.assertEqual(last["outcome"], "restarted-unverified")
                self.assertTrue(last["verification_error"])
                self.assertEqual(panel.reboots, 1)
                self.assertEqual(controller.state, "idle")

    def test_unreadable_pre_reboot_inventory_does_not_authorize_a_restart(self) -> None:
        panel = FakePanel()
        original = panel.read_json

        def read(path, operation=None):
            return {} if path == transport.APPS_PATH else original(path, operation=operation)

        panel.read_json = read
        controller = self.make(panel)
        run(controller, self.clock, 600)
        self.assertEqual(panel.reboots, 0)
        self.assertIsNone(controller.attempted_on)

    def test_invalid_history_is_replaced_without_stalling_maintenance(self) -> None:
        panel = FakePanel()
        controller = self.make(panel)
        self.report.write_text('{"history": {"bad": "shape"}}', encoding="utf-8")
        run(controller, self.clock, 600)
        self.assertEqual(self.report_last()["outcome"], "ok")
        self.assertEqual(controller.state, "idle")


class PanelClientRebootTests(unittest.TestCase):
    def test_reboot_posts_and_a_planned_restart_does_not_latch_static(self) -> None:
        client, session, _ = make_client()
        session.post = Mock(return_value=FakeResponse(200))
        session.get.return_value = FakeResponse(200, {"uptimeSeconds": 900})
        client.probe_device()
        client.reboot()
        session.post.assert_called_once_with("http://panel.local/api/v1/device/reboot", timeout=5.0)
        session.get.return_value = FakeResponse(200, {"uptimeSeconds": 4, "resetReason": "software"})
        client.probe_device()
        self.assertFalse(client.static_latched)

    def test_read_json_is_a_plain_get(self) -> None:
        client, session, _ = make_client()
        session.get.return_value = FakeResponse(200, {"power": False})
        self.assertEqual(client.read_json("/api/v1/display"), {"power": False})


class HubConfigTests(unittest.TestCase):
    def test_off_unless_enabled(self) -> None:
        config = hub.config_from_mapping({})
        self.assertFalse(config.maintenance.enabled or config.iss.enabled)

    def test_window_and_uptime_parse(self) -> None:
        config = hub.config_from_mapping({"maintenance": {
            "enabled": True, "window_start": "02:15", "window_end": "04:00", "min_uptime_hours": 36}})
        self.assertEqual((config.maintenance.window_start_minute, config.maintenance.window_end_minute), (135, 240))
        self.assertEqual(config.maintenance.min_uptime_hours, 36.0)

    def test_config_file_written_with_a_bom_still_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "utility_hub.json"
            path.write_text(json.dumps({"maintenance": {"enabled": True}}), encoding="utf-8-sig")
            self.assertTrue(hub.load_config(path).maintenance.enabled)

    def test_bad_values_are_refused(self) -> None:
        for bad in ({"window_start": "4am"}, {"min_uptime_hours": -1}, {"min_uptime_hours": True},
                    {"window_start": "03:00", "window_end": "03:00"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                hub.config_from_mapping({"maintenance": bad})


class LowMemoryPanel(FakePanel):
    """A panel whose memory is short until it restarts.

    ``blocks`` and ``min_frees`` are the readings before the restart, one per
    device read (the last one repeats); after it the panel reads healthy.
    """

    def __init__(self, *, blocks=(30_000,), min_frees=(20_000,), uptime: int = 22 * DAY_HOURS,
                 power: bool = True, **kwargs) -> None:
        super().__init__(uptime=uptime, power=power, **kwargs)
        self.blocks = list(blocks)
        self.min_frees = list(min_frees)
        self.device_reads = 0

    def read_json(self, path: str, *, operation: str | None = None):
        body = super().read_json(path, operation=operation)
        if path == pm.DEVICE_PATH:
            if self.reboots:
                body.update(largestFreeBlockBytes=32_756, minFreeHeapBytes=30_000)
            else:
                index = self.device_reads
                body.update(largestFreeBlockBytes=self.blocks[min(index, len(self.blocks) - 1)],
                            minFreeHeapBytes=self.min_frees[min(index, len(self.min_frees) - 1)])
                self.device_reads += 1
        return body


class ClockedPanel(FakePanel):
    """A panel whose uptime follows the test clock.

    ``memory(restarts, uptime)`` gives ``(largest block, minimum free heap)``,
    where ``restarts`` counts the restarts asked for; ``jitter(n)`` adds to the
    uptime of the n-th device read.  A restart takes ten seconds and leaves the
    matrix as it was.
    """

    def __init__(self, clock: FakeClock, *, uptime: int = 22 * DAY_HOURS, memory=None, jitter=None,
                 power: bool = True, **kwargs) -> None:
        super().__init__(uptime=uptime, power=power, **kwargs)
        self.clock = clock
        self.boot_at = clock.t - uptime
        self.memory = memory or (lambda restarts, up: (12_000, 6_000))
        self.jitter = jitter or (lambda n: 0)
        self.device_reads = 0
        self.reachable = True

    def reboot(self) -> None:
        self.reboots += 1
        self.boot_at = self.clock.t + 10

    def read_json(self, path: str, *, operation: str | None = None):
        if not self.reachable or self.clock.t < self.boot_at:
            self.reads.append(path)
            raise transport.PanelTransportError("unreachable")
        if path != pm.DEVICE_PATH:
            return super().read_json(path, operation=operation)
        self.reads.append(path)
        self.device_reads += 1
        uptime = int(self.clock.t - self.boot_at) + self.jitter(self.device_reads)
        block, min_free = self.memory(self.reboots, uptime)
        return {"uptimeSeconds": uptime, "largestFreeBlockBytes": block, "minFreeHeapBytes": min_free,
                "freeHeapBytes": 30_000, "matrixPower": self.power, "fps": 40,
                "resetReason": "software" if self.reboots else "poweron"}


AFTERNOON = datetime(2026, 10, 8, 14, 0)


class LowMemoryTests(Fixture):
    def make_low(self, panel: FakePanel, wall: datetime = AFTERNOON, **config):
        # Most tests read from the first tick; the start delay has its own test.
        config.setdefault("low_memory_start_delay_seconds", 0.0)
        return self.make(panel, wall=wall, low_memory_enabled=True, **config)

    def history(self) -> list:
        return json.loads(self.report.read_text(encoding="utf-8"))["history"]

    def test_off_unless_asked_for(self) -> None:
        self.assertFalse(pm.MaintenanceConfig().low_memory_enabled)

    def test_low_minimum_free_heap_restarts_a_lit_panel_in_the_afternoon(self) -> None:
        # The 2026-10-07 shape: block still ~13 KB, minimum free heap 6,740.
        panel = LowMemoryPanel(blocks=(12_788,), min_frees=(6_740,), dark_after_reads=None)
        controller = self.make_low(panel)
        run(controller, self.clock, 600)
        self.assertEqual(panel.reboots, 1)
        last = self.report_last()
        self.assertEqual((last["kind"], last["outcome"]), ("low-memory", "ok"))
        self.assertEqual(last["trigger"], {"signal": "min_free", "bytes": 6_740, "threshold": 8_192})
        self.assertIn("minimum free heap 6740 < 8192", last["reason"])
        self.assertEqual(last["before"]["minFreeHeapBytes"], 6_740)
        self.assertTrue(last["after"]["matrixPower"])

    def test_a_daytime_restart_that_comes_back_dark_needs_attention(self) -> None:
        panel = LowMemoryPanel(min_frees=(6_000,), dark_after_reads=3)
        controller = self.make_low(panel)
        run(controller, self.clock, 600)
        self.assertEqual(self.report_last()["outcome"], "attention")

    def test_a_dark_panel_restarted_for_memory_should_come_back_dark(self) -> None:
        panel = LowMemoryPanel(min_frees=(6_000,), power=False, dark_after_reads=3)
        controller = self.make_low(panel, wall=datetime(2026, 10, 8, 23, 30))
        run(controller, self.clock, 600)
        self.assertEqual((self.report_last()["kind"], self.report_last()["outcome"]), ("low-memory", "ok"))

    def test_a_low_block_needs_three_readings_in_a_row(self) -> None:
        panel = LowMemoryPanel(blocks=(7_000,), dark_after_reads=None)
        controller = self.make_low(panel, low_memory_block_bytes=8192)
        run(controller, self.clock, 115)  # readings at 0 s and 60 s
        self.assertEqual(panel.reboots, 0)
        run(controller, self.clock, 10)  # the third reading, at 120 s
        self.assertEqual(panel.reboots, 1)
        run(controller, self.clock, 600)
        last = self.report_last()
        self.assertEqual(last["trigger"], {"signal": "block", "bytes": 7_000, "threshold": 8_192, "readings": 3})

    def test_a_block_that_recovers_between_readings_starts_the_count_again(self) -> None:
        panel = LowMemoryPanel(blocks=(7_000, 7_000, 9_000, 7_000, 7_000, 30_000), dark_after_reads=None)
        controller = self.make_low(panel)
        run(controller, self.clock, 900)
        self.assertEqual(panel.reboots, 0)
        self.assertEqual(controller.low_block_readings, 0)

    def test_nothing_happens_in_the_first_half_hour_after_a_boot(self) -> None:
        panel = LowMemoryPanel(blocks=(4_000,), min_frees=(4_000,), uptime=20 * 60)
        controller = self.make_low(panel)
        run(controller, self.clock, 300)
        self.assertEqual(panel.reboots, 0)

    def test_healthy_memory_is_read_once_a_minute_and_left_alone(self) -> None:
        panel = LowMemoryPanel()
        controller = self.make_low(panel)
        run(controller, self.clock, 600)
        self.assertEqual(panel.reboots, 0)
        self.assertEqual(panel.reads.count(pm.DEVICE_PATH), 10)
        self.assertFalse(self.report.exists())

    def test_an_unreadable_or_malformed_panel_never_triggers(self) -> None:
        for body in (transport.PanelTransportError("down"), {"uptimeSeconds": "x"},
                     {"uptimeSeconds": 90_000, "largestFreeBlockBytes": True, "minFreeHeapBytes": -5},
                     {"uptimeSeconds": 90_000, "largestFreeBlockBytes": 7.5, "minFreeHeapBytes": None}):
            with self.subTest(body=body):
                panel = FakePanel()
                def read(path, *, operation=None, body=body):
                    panel.reads.append(path)
                    if isinstance(body, Exception):
                        raise body
                    return dict(body)
                panel.read_json = read
                controller = self.make_low(panel)
                run(controller, self.clock, 600)
                self.assertEqual(panel.reboots, 0)

    def seed_history(self, *entries: dict) -> None:
        self.report.write_text(json.dumps({"last": entries[-1], "history": list(entries)}), encoding="utf-8")

    def test_at_most_three_low_memory_restarts_a_day_even_across_hub_restarts(self) -> None:
        panel = LowMemoryPanel(min_frees=(6_000,), dark_after_reads=None)
        controller = self.make_low(panel)
        self.seed_history(*({"at": f"2026-10-08T{h:02d}:00:00", "kind": "low-memory", "outcome": "ok"}
                            for h in (2, 6, 10)))
        run(controller, self.clock, 1800)
        self.assertEqual(panel.reboots, 0)
        held = [e for e in self.history() if e.get("outcome") == "skipped"]
        self.assertEqual(len(held), 1, "the cap is noted once per boot, not once a minute")
        self.assertIn("3 low-memory restarts in 24 h", held[0]["reason"])

    def test_old_held_back_and_nightly_entries_do_not_count_toward_the_cap(self) -> None:
        panel = LowMemoryPanel(min_frees=(6_000,), dark_after_reads=None)
        controller = self.make_low(panel)
        self.seed_history(
            {"at": "2026-10-07T09:00:00", "kind": "low-memory", "outcome": "ok"},  # over 24 h ago
            {"at": "2026-10-08T09:00:00", "kind": "low-memory", "outcome": "skipped"},
            {"at": "2026-10-08T10:00:00", "kind": "low-memory", "outcome": "ok"},
            {"at": "2026-10-07T21:37:00", "outcome": "ok"},  # nightly, from before "kind" existed
            {"at": "not a time", "kind": "low-memory", "outcome": "ok"},
        )
        run(controller, self.clock, 600)
        self.assertEqual(panel.reboots, 1)

    def test_the_nightly_run_still_happens_with_low_memory_on(self) -> None:
        panel = LowMemoryPanel(blocks=(20_000,), min_frees=(15_000,), power=False)
        controller = self.make_low(panel, wall=datetime(2026, 10, 8, 21, 45))
        run(controller, self.clock, 600)
        self.assertEqual(panel.reboots, 1)
        last = self.report_last()
        self.assertEqual((last["kind"], last["outcome"]), ("nightly", "ok"))
        self.assertNotIn("trigger", last)

    def test_nightly_checks_keep_their_five_minute_pace(self) -> None:
        # A lit panel inside the window: the nightly path reads the display
        # once, decides "skipped" and is done for the night; only the
        # minute-by-minute memory reads continue.
        panel = LowMemoryPanel(power=True)
        controller = self.make_low(panel, wall=datetime(2026, 10, 8, 21, 45))
        run(controller, self.clock, 900)
        self.assertEqual(panel.reads.count(pm.DISPLAY_PATH), 1)
        self.assertEqual(panel.reboots, 0)

    def test_bad_low_memory_settings_are_refused(self) -> None:
        for bad in ({"low_memory_block_bytes": -1}, {"low_memory_block_bytes": 8.5},
                    {"low_memory_min_free_bytes": True}, {"low_memory_block_checks": 0},
                    {"low_memory_max_per_day": 0}, {"low_memory_check_seconds": 0},
                    {"low_memory_min_uptime_minutes": -1},
                    {"low_memory_min_free_min_uptime_minutes": -1}, {"low_memory_start_delay_seconds": -1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pm.MaintenanceConfig(**bad)

    # -------------------------------------------------- review, 2026-10-08

    def make_clocked(self, wall: datetime = AFTERNOON, panel: dict | None = None, **config):
        """A ClockedPanel, and a controller whose wall clock moves with the test clock."""
        clock = FakeClock()
        clocked = ClockedPanel(clock, **(panel or {}))
        controller = self.make_low(clocked, wall=wall, clock=clock, **config)
        self.follow_clock(controller, wall)
        return clocked, controller

    def follow_clock(self, controller: pm.MaintenanceController, start: datetime) -> None:
        t0 = self.clock.t
        controller.wall_now = lambda: start + timedelta(seconds=self.clock.t - t0)

    def low_memory_entries(self) -> list:
        return [e for e in self.history() if e.get("kind") == "low-memory"]

    def seed_restarts(self, *ats: str) -> None:
        self.seed_history(*({"at": at, "kind": "low-memory", "outcome": "ok"} for at in ats))

    def test_a_held_back_restart_is_noted_once_however_the_boot_estimate_wobbles(self) -> None:
        # Wall clock minus whole-second uptime, read with latency, wobbles by a
        # second or two.  A boot at hh:mm:30 flipped a per-minute key on every
        # other reading, and the flood of notes pushed the counted restarts out
        # of the 30-entry history until the cap stopped holding.
        panel, controller = self.make_clocked(panel={"uptime": 22 * DAY_HOURS - 31,
                                                     "jitter": lambda n: 2 * (n % 2)})
        self.seed_restarts("2026-10-08T08:00:00", "2026-10-08T10:00:00", "2026-10-08T12:00:00")
        run(controller, self.clock, 2 * 3600)
        self.assertEqual(panel.reboots, 0)
        held = [e for e in self.history() if e.get("outcome") == "skipped"]
        self.assertEqual(len(held), 1)

    def test_skipped_entries_never_push_counted_restarts_out_of_the_history(self) -> None:
        controller = self.make_low(LowMemoryPanel())
        self.seed_restarts("2026-10-08T08:00:00", "2026-10-08T10:00:00", "2026-10-08T12:00:00")
        for _ in range(2 * pm.HISTORY_LIMIT):
            controller._record({"at": "2026-10-08T13:00:00", "outcome": "skipped", "reason": "uptime 0.3 h"})
        self.assertEqual(len(self.history()), pm.HISTORY_LIMIT)
        self.assertEqual(controller._recent_low_memory_reboots(AFTERNOON), 3)

    def test_a_restart_cut_short_by_a_hub_restart_still_counts(self) -> None:
        panel = LowMemoryPanel(min_frees=(6_000,), dark_after_reads=None)
        controller = self.make_low(panel)
        controller.tick()
        self.assertEqual((controller.state, panel.reboots), ("rebooting", 1))
        successor = pm.MaintenanceController(panel, controller.config, clock=self.clock,
                                             wall_now=lambda: AFTERNOON, report_path=self.report)
        self.assertEqual(successor._recent_low_memory_reboots(AFTERNOON), 1)
        run(controller, self.clock, 600)
        self.assertEqual([e["outcome"] for e in self.low_memory_entries()], ["ok"],
                         "the outcome replaces the in-progress entry")

    def test_a_timestamp_with_an_offset_neither_crashes_the_check_nor_escapes_the_cap(self) -> None:
        panel = LowMemoryPanel(min_frees=(6_000,), dark_after_reads=None)
        controller = self.make_low(panel)
        self.seed_restarts((AFTERNOON - timedelta(hours=2)).astimezone().isoformat(),
                           (AFTERNOON - timedelta(hours=4)).astimezone(timezone.utc).isoformat(),
                           (AFTERNOON - timedelta(hours=6)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        run(controller, self.clock, 600)
        self.assertEqual(panel.reboots, 0)

    def test_restarts_that_look_later_than_now_still_count(self) -> None:
        # The PC clock stepped back ten minutes after three restarts.
        panel = LowMemoryPanel(min_frees=(6_000,), dark_after_reads=None)
        controller = self.make_low(panel)
        self.seed_restarts("2026-10-08T14:01:00", "2026-10-08T14:05:00", "2026-10-08T14:09:00")
        run(controller, self.clock, 600)
        self.assertEqual(panel.reboots, 0)
        self.assertEqual(controller._recent_low_memory_reboots(AFTERNOON - timedelta(days=2)), 0,
                         "more than a day ahead is not a real restart")
        # The repeated hour when daylight time ends: 01:25 and 01:45 PDT are
        # earlier than 01:10 PST.
        self.seed_restarts("2026-11-01T01:05:00", "2026-11-01T01:25:00", "2026-11-01T01:45:00")
        self.assertEqual(controller._recent_low_memory_reboots(datetime(2026, 11, 1, 1, 10, fold=1)), 3)

    def test_a_low_memory_restart_goes_ahead_without_the_script_inventory(self) -> None:
        panel = LowMemoryPanel(min_frees=(6_000,), dark_after_reads=None)
        original = panel.read_json

        def read(path, *, operation=None):
            if path == transport.APPS_PATH and not panel.reboots:
                raise transport.PanelTransportError("inspect apps unreachable: ReadTimeout")
            return original(path, operation=operation)

        panel.read_json = read
        controller = self.make_low(panel)
        run(controller, self.clock, 600)
        self.assertEqual(panel.reboots, 1)
        last = self.report_last()
        self.assertEqual((last["kind"], last["outcome"]), ("low-memory", "ok"))
        self.assertIsNone(last["expected_scripts"])
        self.assertIsNone(last["missing_scripts"])
        self.assertIn("ReadTimeout", last["inventory_error"])

    def test_low_block_readings_either_side_of_a_gap_are_not_in_a_row(self) -> None:
        for gap in ("panel restarted on its own", "PC asleep"):
            with self.subTest(gap=gap):
                panel, controller = self.make_clocked(panel={"memory": lambda restarts, up: (7_000, 20_000)},
                                                      low_memory_block_bytes=8192)
                run(controller, self.clock, 65)  # readings at 0 s and 60 s
                self.assertEqual(controller.low_block_readings, 2)
                if gap == "panel restarted on its own":
                    panel.reachable = False
                    run(controller, self.clock, 50 * 60)
                    panel.reachable = True
                    panel.boot_at = self.clock.t - 40 * 60
                else:
                    self.clock.advance(8 * 3600)  # the Hub does not tick while the PC sleeps
                run(controller, self.clock, 60)  # one more low reading
                self.assertEqual(panel.reboots, 0)
                self.assertEqual(controller.low_block_readings, 1)

    def test_the_first_memory_reading_waits_for_the_logon_burst(self) -> None:
        panel = LowMemoryPanel(min_frees=(6_000,), dark_after_reads=None)
        controller = self.make(panel, wall=AFTERNOON, low_memory_enabled=True)  # default delay
        run(controller, self.clock, 175)
        self.assertEqual((panel.reads, panel.reboots), ([], 0))
        run(controller, self.clock, 10)
        self.assertEqual(panel.reboots, 1)

    def test_the_report_shows_minimum_free_heap_after_the_restart(self) -> None:
        panel = LowMemoryPanel(min_frees=(6_000,), dark_after_reads=None)
        controller = self.make_low(panel)
        run(controller, self.clock, 600)
        self.assertEqual(self.report_last()["after"]["minFreeHeapBytes"], 30_000)

    def test_a_restart_across_a_nightkeeper_transition_is_not_flagged(self) -> None:
        for start, before, dark_after_reads in ((datetime(2026, 10, 8, 21, 27, 50), True, 3),
                                                (datetime(2026, 10, 8, 6, 57, 50), False, None)):
            with self.subTest(start=start):
                panel = LowMemoryPanel(min_frees=(6_000,), power=before, dark_after_reads=dark_after_reads)
                controller = self.make_low(panel, wall=start)
                self.follow_clock(controller, start)
                run(controller, self.clock, 300)
                entry = self.low_memory_entries()[-1]
                self.assertNotEqual(entry["after"]["matrixPower"], before)
                self.assertEqual(entry["outcome"], "ok")

    def test_an_unknown_pre_reboot_power_falls_back_to_the_schedule(self) -> None:
        panel = LowMemoryPanel(min_frees=(6_000,), power=False, dark_after_reads=None)
        original = panel.read_json

        def read(path, *, operation=None):
            body = original(path, operation=operation)
            if path == pm.DEVICE_PATH and not panel.reboots:
                body.pop("matrixPower")
            return body

        panel.read_json = read
        controller = self.make_low(panel, wall=datetime(2026, 10, 8, 23, 30))
        run(controller, self.clock, 600)
        last = self.report_last()
        self.assertTrue(last["after"]["matrixPower"])  # lit at 23:30
        self.assertEqual(last["outcome"], "attention")

    def test_a_matrix_held_off_its_schedule_holds_the_restart_back(self) -> None:
        # Lit in the blackout, or dark by day: a person is holding it, and a
        # restart would hand it back to Nightkeeper's schedule.
        for wall, held in ((datetime(2026, 10, 8, 23, 30), True), (AFTERNOON, False)):
            with self.subTest(wall=wall):
                panel = LowMemoryPanel(min_frees=(6_000,), power=held, dark_after_reads=None)
                controller = self.make_low(panel, wall=wall)
                run(controller, self.clock, 600)
                self.assertEqual(panel.reboots, 0)
                notes = [e for e in self.history() if e.get("outcome") == "skipped"]
                self.assertEqual(len(notes), 1, "noted once per boot, not once a minute")
                self.assertIn("person is holding it", notes[0]["reason"])
                panel.power = not held  # they let go; the matrix follows the schedule again
                run(controller, self.clock, 120)
                self.assertEqual(panel.reboots, 1)

    def test_a_restart_waits_out_the_two_minutes_around_a_switch(self) -> None:
        # Lit at 21:29: Nightkeeper is about to switch it off, so this is not a
        # person; nothing is noted and the restart waits for the next reading.
        panel = LowMemoryPanel(min_frees=(6_000,), power=True, dark_after_reads=None)
        controller = self.make_low(panel, wall=datetime(2026, 10, 8, 21, 29))
        run(controller, self.clock, 300)
        self.assertEqual(panel.reboots, 0)
        self.assertFalse(self.report.exists())

    def test_held_by_a_person_follows_nightkeepers_schedule(self) -> None:
        cases = [((14, 0), True, False), ((14, 0), False, True), ((23, 30), False, False),
                 ((23, 30), True, True), ((21, 29), True, False), ((21, 31), True, False),
                 ((7, 1), False, False), ((14, 0), None, False)]
        for (hour, minute), power, held in cases:
            with self.subTest(at=(hour, minute), power=power):
                moment = datetime(2026, 10, 8, hour, minute)
                self.assertIs(pm.held_by_a_person(moment, power), held)

    def test_minimum_free_heap_is_not_believed_in_the_first_two_hours(self) -> None:
        panel = LowMemoryPanel(min_frees=(6_000,), uptime=90 * 60, dark_after_reads=None)
        controller = self.make_low(panel)
        run(controller, self.clock, 600)
        self.assertEqual(panel.reboots, 0)
        panel = LowMemoryPanel(min_frees=(6_000,), uptime=2 * 3600 + 60, dark_after_reads=None)
        controller = self.make_low(panel)
        run(controller, self.clock, 600)
        self.assertEqual(panel.reboots, 1)

    def test_a_low_block_alone_waits_for_the_nightly_run_by_default(self) -> None:
        panel = LowMemoryPanel(blocks=(4_000,), min_frees=(20_000,), dark_after_reads=None)
        controller = self.make_low(panel)
        run(controller, self.clock, 1800)
        self.assertEqual(panel.reboots, 0)
        self.assertEqual(pm.MaintenanceConfig().low_memory_block_bytes, 0)

    def test_the_cap_holds_when_the_report_cannot_be_read_or_written(self) -> None:
        panel, controller = self.make_clocked()  # memory stays short after every restart
        self.report.mkdir()
        with self.assertLogs("awtrix.maintenance", "WARNING") as logs:
            run(controller, self.clock, 6 * 3600, step=10)
        self.assertEqual(panel.reboots, 3)
        self.assertTrue(any("Could not read maintenance report" in line for line in logs.output))

    def test_each_restart_says_how_many_the_day_has_had(self) -> None:
        panel = LowMemoryPanel(min_frees=(6_000,), dark_after_reads=None)
        controller = self.make_low(panel)
        self.seed_restarts("2026-10-08T10:00:00")
        run(controller, self.clock, 600)
        self.assertIn("low-memory restart 2 of at most 3 in 24 h", self.report_last()["reason"])

    def test_the_docstrings_describe_both_kinds_of_restart(self) -> None:
        self.assertIn("What the nightly run will not do", pm.__doc__)
        self.assertIn("low-memory", " ".join(pm.MaintenanceController.__doc__.split()).lower())


class LowMemoryHubConfigTests(unittest.TestCase):
    def test_on_with_maintenance_and_reaches_the_controller(self) -> None:
        config = hub.config_from_mapping({"maintenance": {"enabled": True}})
        built = hub.build_maintenance_config(config.maintenance)
        self.assertTrue(built.enabled and built.low_memory_enabled)
        self.assertEqual((built.low_memory_block_bytes, built.low_memory_min_free_bytes), (0, 8_192))
        self.assertEqual((built.window_start_minute, built.min_uptime_hours), (21 * 60 + 35, 20.0))

    def test_floors_can_be_set_or_the_feature_turned_off(self) -> None:
        config = hub.config_from_mapping({"maintenance": {
            "enabled": True, "low_memory_reboot": False,
            "low_memory_block_bytes": 6000, "low_memory_min_free_bytes": 7000}})
        built = hub.build_maintenance_config(config.maintenance)
        self.assertFalse(built.low_memory_enabled)
        self.assertEqual((built.low_memory_block_bytes, built.low_memory_min_free_bytes), (6_000, 7_000))

    def test_bad_values_are_refused(self) -> None:
        for bad in ({"low_memory_reboot": "yes"}, {"low_memory_block_bytes": True},
                    {"low_memory_block_bytes": -1}, {"low_memory_min_free_bytes": 70_000},
                    {"low_memory_min_free_bytes": 8192.0}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                hub.config_from_mapping({"maintenance": bad})


if __name__ == "__main__":
    unittest.main()


class WindowDefaultTests(unittest.TestCase):
    def test_default_window_is_just_after_the_blackout_while_the_pc_is_awake(self) -> None:
        # 03:30 never ran: the PC sleeps overnight.  21:35 is five minutes into
        # nightkeeper's 21:30 blackout.
        for config in (pm.MaintenanceConfig(), hub.config_from_mapping({}).maintenance):
            self.assertEqual((config.window_start_minute, config.window_end_minute), (21 * 60 + 35, 23 * 60))
