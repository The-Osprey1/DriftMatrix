from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import unittest

import notify_mirror as nm
import utility_hub as hub
import visual_system as vs
from test_new_app_support import FakeClock, SpyPanel

NOW = datetime(2026, 10, 3, 18, 0, tzinfo=timezone.utc)


def toast(tid: int, app: str = "Outlook", minutes_ago: float = 1.0) -> nm.Toast:
    return nm.Toast(tid, app, NOW - timedelta(minutes=minutes_ago))


class ActivatingPanel(SpyPanel):
    def __init__(self):
        super().__init__()
        self.activations = []

    def activate_app(self, name):
        self.activations.append(name)


class PrivacyTests(unittest.TestCase):
    def test_source_never_reads_notification_content(self) -> None:
        source = Path(nm.__file__).read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        # UserNotification.notification.visual.get_binding(...).get_text_elements()
        # is the only road to a title or body; none of it may appear.
        for forbidden in (r"\.notification\b", r"\.visual\b", r"get_text_elements", r"get_binding"):
            with self.subTest(forbidden=forbidden):
                self.assertNotRegex(code, forbidden)

    def test_log_key_carries_only_label_and_count(self) -> None:
        panel = SpyPanel()
        c = nm.NotifyMirrorController(panel, clock=FakeClock(), utc_now=lambda: NOW,
                                      reader=lambda: [toast(1, "Microsoft Teams (work)")])
        c.tick()
        self.assertEqual(c.scene.key, ("TEAMS", 1))


class RuleTests(unittest.TestCase):
    def test_allowlist_matches_by_name_and_ignores_everything_else(self) -> None:
        rules = nm.DEFAULT_RULES
        self.assertEqual(nm.match_rule("Outlook (new)", rules).label, "MAIL")
        self.assertEqual(nm.match_rule("Microsoft Teams", rules).label, "TEAMS")
        self.assertEqual(nm.match_rule("Discord", rules).icon, "chat")
        for ignored in ("Google Chrome", "Windows Security", "OneDrive", ""):
            with self.subTest(app=ignored):
                self.assertIsNone(nm.match_rule(ignored, rules))

    def test_old_and_unlisted_toasts_are_not_pending(self) -> None:
        waiting = nm.pending([toast(1, minutes_ago=2), toast(2, minutes_ago=40), toast(3, "Google Chrome")],
                             nm.DEFAULT_RULES, NOW, 15)
        self.assertEqual([t.toast_id for t, _ in waiting], [1])

    def test_newest_first(self) -> None:
        waiting = nm.pending([toast(1, minutes_ago=9), toast(2, "Discord", minutes_ago=1)], nm.DEFAULT_RULES, NOW, 15)
        self.assertEqual(waiting[0][1].label, "CHAT")

    def test_config_rules_parse_and_reject_bad_entries(self) -> None:
        rules = nm.rules_from_config([{"match": "Signal", "label": "text", "icon": "chat"}])
        self.assertEqual(rules, (nm.AppRule("Signal", "TEXT", "chat"),))
        for bad in ([], [{"label": "X"}], [{"match": "a", "label": "X", "icon": "gif"}],
                    [{"match": "a", "label": "MUCHTOOLONGLABEL"}]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                nm.rules_from_config(bad)


class PayloadTests(unittest.TestCase):
    def test_every_default_label_fits_and_payload_is_small(self) -> None:
        for rule in nm.DEFAULT_RULES:
            for count in (1, 3, 9):
                with self.subTest(label=rule.label, count=count):
                    p = nm.build_payload(rule, count)
                    vs.validate_awtrix_payload(p)
                    self.assertLessEqual(len(json.dumps(p, separators=(",", ":"))), 2048)

    def test_icons_keep_their_silhouette_at_brightness_one_and_animate(self) -> None:
        for kind in ("mail", "chat", "bell"):
            frames = nm.icon_frames(kind)
            self.assertGreater(len({json.dumps(f) for f in frames}), 1, kind)
            for frame in frames:
                lit = [px for row in frame for px in row[:8] if px != vs.BLACK]
                safe = [px for px in lit if vs.is_safe_at_low_brightness(px)]
                # Shadow tones may fade at brightness 1; the shape may not.
                self.assertGreaterEqual(len(safe), 0.6 * len(lit), kind)
                self.assertTrue(all(px == vs.BLACK for row in frame for px in row[8:]))

    def test_meter_counts_to_five(self) -> None:
        lit = vs.meter_fill
        self.assertLess(lit(nm.render_lane("MAIL", 1)), lit(nm.render_lane("MAIL", 3)))
        self.assertEqual(lit(nm.render_lane("MAIL", 5)), lit(nm.render_lane("MAIL", 12)))


class ControllerTests(unittest.TestCase):
    def make(self, batches):
        self.clock = FakeClock()
        self.panel = ActivatingPanel()
        queue = list(batches)

        def read():
            item = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(item, Exception):
                raise item
            return item
        return nm.NotifyMirrorController(self.panel, clock=self.clock, utc_now=lambda: NOW, reader=read)

    def step(self, c):
        result = c.tick()
        self.clock.advance(c.config.poll_seconds)
        return result

    def test_existing_toasts_at_startup_show_quietly(self) -> None:
        c = self.make([[toast(1)]])
        self.step(c)
        self.assertEqual(len(self.panel.pushes), 1)
        self.assertEqual(self.panel.activations, [])

    def test_a_new_toast_shows_and_announces_once(self) -> None:
        c = self.make([[], [toast(5)], [toast(5)], [toast(5)]])
        for _ in range(4):
            self.step(c)
        self.assertEqual(len(self.panel.pushes), 1)
        self.assertEqual(self.panel.activations, ["notify0"])

    def test_dismissing_in_windows_removes_the_page(self) -> None:
        c = self.make([[], [toast(5)], []])
        for _ in range(3):
            self.step(c)
        self.assertEqual(self.panel.deletes, ["notify0"])

    def test_count_follows_the_newest_apps_label(self) -> None:
        c = self.make([[], [toast(1), toast(2, minutes_ago=0.5), toast(3, "Discord", minutes_ago=3)]])
        self.step(c)
        self.step(c)
        self.assertEqual(c.scene.key, ("MAIL", 2))

    def test_unlisted_apps_never_reach_the_panel(self) -> None:
        c = self.make([[], [toast(1, "Google Chrome"), toast(2, "Windows Security")]])
        for _ in range(3):
            self.step(c)
        self.assertEqual((self.panel.pushes, self.panel.activations), ([], []))

    def test_reader_failure_is_logged_once_and_survived(self) -> None:
        c = self.make([PermissionError("no access")])
        with self.assertLogs("awtrix.notify_mirror", "WARNING") as logs:
            for _ in range(5):
                self.assertIsNone(self.step(c))
        self.assertEqual(len(logs.output), 1)


class HubConfigTests(unittest.TestCase):
    def test_off_by_default_with_default_rules(self) -> None:
        config = hub.config_from_mapping({})
        self.assertFalse(config.notifications.enabled)
        self.assertEqual(config.notifications.rules, nm.DEFAULT_RULES)

    def test_custom_rules_and_age(self) -> None:
        config = hub.config_from_mapping({"notifications": {
            "enabled": True, "max_age_minutes": 30, "apps": [{"match": "Signal", "label": "TEXT", "icon": "chat"}]}})
        self.assertEqual(config.notifications.max_age_minutes, 30.0)
        self.assertEqual(config.notifications.rules[0].match, "Signal")


if __name__ == "__main__":
    unittest.main()
