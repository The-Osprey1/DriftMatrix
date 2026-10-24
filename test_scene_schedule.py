"""Ocean, sunset and moon share one schedule through ``shared``.

sunset publishes ``shared.set("phase", ...)``, which other scripts read as
``sunset.phase``.  These checks keep the three sources agreeing on the key and
the values, since nothing on the panel would report a typo -- a misspelt phase
just hides a page forever.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent
SUNSET = (ROOT / "weather" / "sunset.ax").read_text(encoding="utf-8")
OCEAN = (ROOT / "ocean.ax").read_text(encoding="utf-8")
MOON = (ROOT / "moon.ax").read_text(encoding="utf-8")
# The installed moon since 2026-10-04 is the old tracker, not root moon.ax.
INSTALLED_MOON = (ROOT / "weather" / "moon-panel.ax").read_text(encoding="utf-8")


class SceneScheduleTests(unittest.TestCase):
    def test_sunset_publishes_every_phase(self) -> None:
        self.assertIn('shared.set("phase", p)', SUNSET)
        for phase in ("day", "sunset", "night"):
            with self.subTest(phase=phase):
                self.assertIn(f'"{phase}"', SUNSET)

    def test_followers_read_the_published_key_and_check_its_age(self) -> None:
        for name, source, phase in (("ocean", OCEAN, "day"),):
            with self.subTest(page=name):
                self.assertIn('shared.get("sunset.phase")', source)
                self.assertIn('shared.age("sunset.phase")', source)
                self.assertRegex(source, rf'return p\s*==\s*"{phase}"')

    def test_followers_fall_back_to_the_clock_and_show_before_ntp(self) -> None:
        # hour() is -1 before NTP sync; an unknown time shows the page.
        for name, source in (("ocean", OCEAN),):
            with self.subTest(page=name):
                self.assertRegex(source, r"h\s*<\s*0\s*\|\|")

    def test_moon_stays_in_the_loop_all_day(self) -> None:
        # Tried night-only on 2026-10-03; the moon phase is wanted at any hour.
        self.assertNotIn("def should_show", MOON)
        self.assertIn("var ALWAYS_SHOW = true", INSTALLED_MOON)


if __name__ == "__main__":
    unittest.main()
