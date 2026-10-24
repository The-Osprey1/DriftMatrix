"""Scenes may only call methods PanelClient actually has.

`meeting_sentinel` and `commute_radar` both called `panel.delete_app(...)`.
PanelClient has `delete_pushed_app`. Every call site wraps the call in
`except Exception -> logger.warning(...)`, so the AttributeError became a log
line instead of a crash and the scene simply never cleared itself:

    WARNING Failed to clear On-Air alert: 'PanelClient' object has no attribute 'delete_app'

Twice on 11 September. The visible symptom is an ON AIR badge that stays on the
panel after the call has ended. `focus_controller` and `next_up_controller` --
the two scenes that are actually enabled -- use the correct name, which is why
this survived.

The interface is read off PanelClient rather than written down here, so
renaming a method there cannot leave this test passing.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest

import panel_client

ROOT = Path(__file__).resolve().parent

# Every module that drives the panel through a PanelClient.
SCENES = (
    "desk_reminder.py",
    "meeting_sentinel.py",
    "commute_radar.py",
    "desk_companion.py",
    "miku_sentinel.py",
    "github_sentinel.py",
    "focus_controller.py",
    "next_up_controller.py",
)


def panel_methods() -> set[str]:
    return {name for name in dir(panel_client.PanelClient)
            if not name.startswith("__")}


class PanelInterfaceTests(unittest.TestCase):
    def test_the_scan_finds_real_calls(self) -> None:
        """A scan that matched nothing would pass no matter what broke."""
        calls = re.findall(r"self\.panel\.([a-z_]+)",
                           (ROOT / "meeting_sentinel.py").read_text(encoding="utf-8"))
        self.assertIn("push_app", calls)

    def test_every_scene_calls_only_methods_that_exist(self) -> None:
        have = panel_methods()
        self.assertIn("delete_pushed_app", have)   # the name that is real
        self.assertNotIn("delete_app", have)       # the name that was used

        for scene in SCENES:
            source = (ROOT / scene).read_text(encoding="utf-8")
            for call in sorted(set(re.findall(r"self\.panel\.([a-z_]+)", source))):
                with self.subTest(scene=scene, call=call):
                    self.assertIn(
                        call, have,
                        f"{scene} calls panel.{call}(), which PanelClient does "
                        f"not have; the call site swallows AttributeError, so "
                        f"the scene fails silently instead of crashing",
                    )


if __name__ == "__main__":
    unittest.main()
