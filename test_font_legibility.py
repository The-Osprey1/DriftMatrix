"""Repo-wide guard: no font may contain two glyphs a reader cannot tell apart.

Every pixel font in this repository declared the letter ``O`` and the digit
``0`` with byte-identical bitmaps, and three of them did the same for ``1``
and ``I`` -- as a bare single column, so a title reading ``1ST`` drew exactly
the pixels of one reading ``IST``.  ``preview_sunset`` also had ``5`` and
``S`` identical.

None of that could fail a test.  Each font is a private dict inside its own
module, the collisions are invisible unless two specific glyphs are compared,
and the panel renders an ambiguous string as cheerfully as an unambiguous one.
The fix for the individual glyphs is in those modules; this file is what stops
the class coming back, in a font nobody has written yet.
"""

from __future__ import annotations

import importlib
import itertools
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent
for path in (ROOT, ROOT / "weather"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

# Every module that declares glyph bitmaps.  A new one must be added here;
# test_every_font_module_is_listed fails when one is not.
FONT_MODULES = (
    "visual_system",
    "github_sentinel",
    "meeting_sentinel",
    "focus_sprint_preview",
    "next_up_preview",
    "network_sentinel_preview",
    "now_playing_push",
    "pc_alert_native_concepts",
    "pc_readouts",
    "pc_stats_preview",
    "aqi_brief_preview",
    "preview_countdown",
    "preview_moon",
    "preview_sunset",
    "weather_brief_preview",
)


def _is_font(value: object) -> bool:
    return (
        isinstance(value, dict)
        and len(value) > 5
        and all(isinstance(key, str) for key in value)
        and all(
            isinstance(glyph, (tuple, list))
            and glyph
            and all(isinstance(row, str) for row in glyph)
            for glyph in value.values()
        )
    )


def fonts():
    """Yield (module name, attribute, font) for every font in the repo."""
    seen: set[int] = set()
    for name in FONT_MODULES:
        module = importlib.import_module(name)
        for attr in dir(module):
            value = getattr(module, attr, None)
            if not _is_font(value):
                continue
            # Modules that import a shared font are the same object; checking
            # it once is enough and keeps the failure message unambiguous.
            if id(value) in seen:
                continue
            seen.add(id(value))
            yield name, attr, value


class FontLegibilityTests(unittest.TestCase):
    def test_no_font_has_two_identical_glyphs(self) -> None:
        for name, attr, font in fonts():
            printable = sorted(k for k in font if k.strip())
            for a, b in itertools.combinations(printable, 2):
                if font[a] != font[b]:
                    continue
                with self.subTest(font=f"{name}.{attr}", pair=f"{a}/{b}"):
                    self.fail(
                        f"{name}.{attr}: {a!r} and {b!r} are byte-identical, "
                        f"so the panel cannot distinguish them"
                    )

    def test_digits_are_distinct_from_the_letters_they_resemble(self) -> None:
        """The pairs that collide by convention, checked by name.

        Identity is not the only failure: a glyph one pixel from its
        look-alike is still a coin toss at this size, and these three pairs
        are the ones that actually carry different meaning in the same field.
        """
        for name, attr, font in fonts():
            for digit, letter in (("0", "O"), ("1", "I"), ("5", "S")):
                if digit not in font or letter not in font:
                    continue
                with self.subTest(font=f"{name}.{attr}", pair=f"{digit}/{letter}"):
                    self.assertNotEqual(
                        font[digit],
                        font[letter],
                        f"{name}.{attr}: {digit!r} and {letter!r} render the "
                        f"same pixels",
                    )

    def test_confusable_glyphs_stay_distinct(self) -> None:
        """Pairs that carry meaning must not collapse into the same shape.

        A 1px "1" was identical to "I" and read as a stray rule between 3px
        digits.  The 3px "W" differed from "H" by a single pixel, which is
        precisely the WORK/HOME distinction the commute lane's destination
        initial exists to carry -- both destinations rendered as "H".
        """
        # Every pair here is one that can occupy the same field position with
        # a different meaning: "1"/"I" in a timer, "W"/"H" in the commute
        # destination slot, and "H"/"M" and "M"/"N" in the github repo initial,
        # where several repos rotate through one app slot.
        import github_sentinel, meeting_sentinel, visual_system

        pairs = (
            ("1", "I"), ("W", "H"), ("H", "M"), ("M", "N"),
            ("?", "7"), ("?", "3"), ("+", "-"),
        )
        for name, font in (
            ("visual_system", visual_system.FONT_3X5),
            ("github_sentinel", github_sentinel.FONT_3X5),
            ("meeting_sentinel", meeting_sentinel.FONT_3X5),
        ):
            for left, right in pairs:
                if left not in font or right not in font:
                    continue
                with self.subTest(font=name, pair=f"{left}/{right}"):
                    self.assertNotEqual(font[left], font[right])

    def test_required_glyphs_present_in_visual_system(self) -> None:
        import visual_system
        font = visual_system.FONT_3X5
        for char in ("?", "+"):
            with self.subTest(char=char):
                self.assertIn(char, font)
                self.assertEqual(len(font[char]), 5)

    def test_every_font_module_is_listed(self) -> None:
        """A font added in a new module must not escape this file."""
        declared = set(FONT_MODULES)
        found = set()
        for source in sorted(ROOT.glob("*.py")) + sorted((ROOT / "weather").glob("*.py")):
            if source.name.startswith("test_"):
                continue
            text = source.read_text(encoding="utf-8")
            # A glyph table is a dict literal mapping a character to a tuple of
            # bit-strings; the opening of one is distinctive enough to spot.
            if '": ("1' in text or '": ("0' in text:
                found.add(source.stem)
        missing = sorted(found - declared)
        self.assertEqual(
            missing,
            [],
            f"these modules declare glyphs but are not listed in "
            f"FONT_MODULES: {missing}",
        )


if __name__ == "__main__":
    unittest.main()
