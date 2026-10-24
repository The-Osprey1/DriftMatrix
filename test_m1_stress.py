"""Empirical stress test suite for Milestone 1 in visual_system.py.

Stress-tests:
1. Typography fuzzing (measure_text, draw_text, measure_text_3x3, draw_text_3x3)
   with all printable ASCII and unicode symbols, checking zero exceptions, zero
   off-panel writes, and correct 3x5/3x3 bounding boxes.
2. Sparklines stress-testing (render_sparkline) across sequences of length 0, 1, 23, 100,
   NaN, inf, negatives, >1.0, with and without labels, verifying row 4 preservation.
3. Split Lane stress-testing (render_split_lane) across random strings, extreme numbers,
   empty strings, meter values, verifying zero off-panel writes and row 3 100% black separator.
4. Gutter invariant verification: column 8 (GUTTER_X=8) is strictly (0, 0, 0) across all frames.
"""

from __future__ import annotations

import math
import random
import string
from typing import Any, Callable, Sequence
import pytest

import visual_system as vs
from visual_system import (
    WIDTH,
    HEIGHT,
    GUTTER_X,
    LANE_X0,
    LANE_WIDTH,
    BLACK,
    TRACK,
    MIST,
    PEARL,
    ICE,
    LEVEL_COLORS,
    FONT_3X5,
    FONT_3X3,
    Frame,
    RGB,
    new_frame,
    measure_text,
    draw_text,
    measure_text_3x3,
    draw_text_3x3,
    render_sparkline,
    render_split_lane,
    render_glance_lane,
    render_dual_lane,
    render_weather_silhouette,
)


# -----------------------------------------------------------------------------
# Fixtures & Helpers
# -----------------------------------------------------------------------------

class PixelTrap:
    """Hooks vs.put_pixel to detect any attempts to write outside the 32x8 panel."""
    def __init__(self, monkeypatch: pytest.MonkeyPatch):
        self.off_panel: list[tuple[int, int]] = []
        self.all_writes: list[tuple[int, int, RGB]] = []
        self.orig_put = vs.put_pixel

        def trapping_put(frame: Frame, x: int, y: int, color: RGB) -> None:
            self.all_writes.append((x, y, color))
            if not (0 <= x < WIDTH and 0 <= y < HEIGHT):
                self.off_panel.append((x, y))
            self.orig_put(frame, x, y, color)

        monkeypatch.setattr(vs, "put_pixel", trapping_put)

    def reset(self) -> None:
        self.off_panel.clear()
        self.all_writes.clear()


@pytest.fixture
def pixel_trap(monkeypatch: pytest.MonkeyPatch) -> PixelTrap:
    return PixelTrap(monkeypatch)


UNICODE_SYMBOLS = [
    # Degree, math, fractions
    "°", "±", "µ", "½", "¼", "¾", "√", "∞", "≈", "≠", "≤", "≥",
    # Currency
    "€", "£", "¥", "¢", "$", "₹", "₿",
    # Arrows & shapes
    "←", "→", "↑", "↓", "▲", "▼", "◄", "►", "★", "☆", "•", "·", "…",
    # Extended punctuation
    "¿", "¡", "“", "”", "‘", "’", "–", "—", "«", "»",
    # Accented Latin
    "é", "è", "ê", "ë", "à", "â", "ä", "ö", "ü", "ß", "ç", "ñ", "ı", "İ", "Ø", "ø", "Å", "å",
    # Emojis & Misc
    "😀", "🔥", "⚡", "💧", "🚀", "❤️", "🎉", "💀", "🤖", "🎮", "☀️", "🌧️",
    # CJK
    "日", "本", "語", "中", "文", "字",
    # Control / edge-case chars
    "\x00", "\t", "\n", "\r", "\b",
]


# -----------------------------------------------------------------------------
# 1. Typography Fuzzing & Bounding Box Checks
# -----------------------------------------------------------------------------

def test_font_3x5_bounding_box_and_consistency():
    """Verify all glyphs in FONT_3X5 have exact height 5, non-ragged rows, and valid widths."""
    for char, glyph in FONT_3X5.items():
        assert len(glyph) == 5, f"Glyph {char!r} height {len(glyph)} != 5"
        w0 = len(glyph[0])
        assert 1 <= w0 <= 5, f"Glyph {char!r} width {w0} out of expected [1, 5] range"
        for row_idx, row in enumerate(glyph):
            assert len(row) == w0, f"Glyph {char!r} row {row_idx} length {len(row)} != {w0}"
            assert all(bit in ("0", "1") for bit in row), f"Glyph {char!r} row {row_idx} contains non-bit chars: {row!r}"


def test_font_3x3_bounding_box_and_consistency():
    """Verify all glyphs in FONT_3X3 have exact height 3, non-ragged rows, and width 3."""
    for char, glyph in FONT_3X3.items():
        assert len(glyph) == 3, f"Glyph {char!r} height {len(glyph)} != 3"
        for row_idx, row in enumerate(glyph):
            assert len(row) == 3, f"Glyph {char!r} row {row_idx} length {len(row)} != 3"
            assert all(bit in ("0", "1") for bit in row), f"Glyph {char!r} row {row_idx} contains non-bit chars: {row!r}"


def test_fuzz_ascii_printable_measure_and_draw(pixel_trap: PixelTrap):
    """Fuzz all ASCII printable characters (32..126) through measure_text() and draw_text()."""
    ascii_printable = [chr(i) for i in range(32, 127)]
    for char in ascii_printable:
        pixel_trap.reset()
        w = measure_text(char)
        assert isinstance(w, int) and w >= 1, f"Character {char!r} measured {w} < 1"

        frame = new_frame()
        # Draw at standard lane start
        rendered_w = draw_text(frame, char, LANE_X0, 1, PEARL)
        assert rendered_w == w, f"Character {char!r}: draw_text returned {rendered_w} != measured {w}"
        assert pixel_trap.off_panel == [], f"Off-panel writes for ASCII {char!r}: {pixel_trap.off_panel}"

        # Gutter invariant: column 8 must remain black
        for y in range(HEIGHT):
            assert frame[y][GUTTER_X] == BLACK, f"Character {char!r} corrupted gutter at row {y}"


def test_fuzz_unicode_symbols_measure_and_draw(pixel_trap: PixelTrap):
    """Fuzz Unicode symbols, emojis, and CJK characters through measure_text() and draw_text()."""
    for sym in UNICODE_SYMBOLS:
        pixel_trap.reset()
        w = measure_text(sym)
        assert isinstance(w, int) and w >= 0, f"Symbol {sym!r} measured negative width: {w}"

        frame = new_frame()
        rendered_w = draw_text(frame, sym, LANE_X0, 0, PEARL)
        assert rendered_w == w, f"Symbol {sym!r}: rendered width {rendered_w} != measured {w}"
        assert pixel_trap.off_panel == [], f"Off-panel writes for symbol {sym!r}: {pixel_trap.off_panel}"
        for y in range(HEIGHT):
            assert frame[y][GUTTER_X] == BLACK


def test_fuzz_micro_3x3_typography(pixel_trap: PixelTrap):
    """Fuzz ASCII printable and Unicode symbols through measure_text_3x3() and draw_text_3x3()."""
    all_chars = [chr(i) for i in range(32, 127)] + UNICODE_SYMBOLS
    for char in all_chars:
        pixel_trap.reset()
        w = measure_text_3x3(char)
        assert isinstance(w, int) and w >= 0, f"3x3 char {char!r} measured {w} < 0"

        frame = new_frame()
        rendered_w = draw_text_3x3(frame, char, LANE_X0, 0, PEARL)
        assert rendered_w == w
        assert pixel_trap.off_panel == [], f"Off-panel writes in 3x3 for {char!r}: {pixel_trap.off_panel}"


# -----------------------------------------------------------------------------
# 2. Sparklines Stress-Testing & Row 4 Preservation
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("length", [0, 1, 5, 22, 23, 24, 100, 500])
def test_render_sparkline_sequence_lengths(pixel_trap: PixelTrap, length: int):
    """Test render_sparkline across various sequence lengths."""
    pixel_trap.reset()
    rng = random.Random(42 + length)
    values = [rng.uniform(-0.5, 1.5) for _ in range(length)]
    frame = render_sparkline(values)

    assert pixel_trap.off_panel == [], f"Off-panel writes for length {length}: {pixel_trap.off_panel}"
    assert len(frame) == HEIGHT
    assert len(frame[0]) == WIDTH
    for y in range(HEIGHT):
        assert frame[y][GUTTER_X] == BLACK


def test_render_sparkline_degenerate_values(pixel_trap: PixelTrap):
    """Stress-test render_sparkline with NaN, inf, negatives, >1.0, extreme magnitudes."""
    degenerate_sequences = [
        [float("nan")],
        [float("inf")],
        [float("-inf")],
        [-100.0, -1.0, -0.001],
        [1.001, 10.0, 1e9],
        [float("nan")] * 23,
        [float("inf")] * 23,
        [float("-inf")] * 23,
        [-1.0] * 23,
        [10.0] * 23,
        [float("nan"), -10.0, 0.5, float("inf"), 2.0, -float("inf"), 0.0, 1.0],
    ]
    for seq in degenerate_sequences:
        pixel_trap.reset()
        frame = render_sparkline(seq, label="CPU")
        assert pixel_trap.off_panel == [], f"Off-panel writes for {seq}: {pixel_trap.off_panel}"
        for y in range(HEIGHT):
            assert frame[y][GUTTER_X] == BLACK


@pytest.mark.parametrize("label", ["CPU", "GPU?", "500+", "RAM", "NET", "96°", "A"])
def test_render_sparkline_preserves_row_4_with_labels(label: str):
    """Verify row 4 is never corrupted by sparkline bars or baseline when label is present."""
    lw = measure_text(label)
    # Generate full-range values to stress maximum sparkline height
    for val in [0.0, 0.5, 1.0, 10.0, -5.0, float("nan"), float("inf")]:
        values = [val] * LANE_WIDTH
        frame = render_sparkline(values, label=label)

        # Baseline check: gutter must be black
        for y in range(HEIGHT):
            assert frame[y][GUTTER_X] == BLACK

        lx = LANE_X0 + (LANE_WIDTH - lw) // 2
        expected_label_pixels: set[tuple[int, int]] = set()
        x = lx
        for idx, ch in enumerate(label):
            glyph = FONT_3X5.get(ch.upper(), FONT_3X5[" "])
            for gy, row in enumerate(glyph):
                for gx, bit in enumerate(row):
                    if bit == "1":
                        expected_label_pixels.add((x + gx, gy))
            x += len(glyph[0])
            if idx < len(label) - 1:
                x += 1

        # Check row 4 specifically:
        # Every lit pixel on row 4 must be MIST
        # Every unlit pixel on row 4 in the lane must be strictly BLACK (never TRACK or ICE)
        row_4_label_pixels = {x for (x, y) in expected_label_pixels if y == 4}
        for x in range(LANE_X0, LANE_X0 + LANE_WIDTH):
            if x in row_4_label_pixels:
                assert frame[4][x] == MIST, f"Label {label!r} pixel at ({x}, 4) corrupted: {frame[4][x]}"
            else:
                assert frame[4][x] == BLACK, f"Row 4 at x={x} should be BLACK for label {label!r}, got {frame[4][x]}"

        # When label fits budget, sparkline must ONLY occupy rows 5..7
        # Rows 5..7 must be either ICE or TRACK
        for y in (5, 6, 7):
            for x in range(LANE_X0, LANE_X0 + LANE_WIDTH):
                assert frame[y][x] in (ICE, TRACK), f"Unexpected color at ({x}, {y}): {frame[y][x]}"


@pytest.mark.parametrize("empty_or_none_label", [None, "", "OVERBUDGET_LABEL_EXCEEDING_LANE_WIDTH"])
def test_render_sparkline_unlabelled_or_empty_occupies_rows_4_to_7(empty_or_none_label: str | None):
    """Unlabelled, empty-label, or over-budget label sparklines occupy rows 4-7 (height 4)."""
    frame = render_sparkline([1.0] * LANE_WIDTH, label=empty_or_none_label)
    for x in range(LANE_X0, LANE_X0 + LANE_WIDTH):
        for y in range(4):
            assert frame[y][x] == BLACK
        for y in range(4, 8):
            assert frame[y][x] == ICE


# -----------------------------------------------------------------------------
# 3. Split Lane Stress-Testing & Row 3 Separator Isolation
# -----------------------------------------------------------------------------

SPLIT_LANE_PAIRS = [
    ("", ""),
    ("CPU", ""),
    ("", "GPU"),
    ("0", "0"),
    ("100", "200"),
    ("99.9", "0.1"),
    ("-5", "-20"),
    ("GPU?", "500+"),
    ("96°", "45%"),
    ("A/B", "C-D"),
    ("12345", "67890"),   # 5 chars = 19px, fits easily
    ("123456", "ABCDEF"), # 6 chars = 23px, exact maximum fit
]


@pytest.mark.parametrize("top,bot", SPLIT_LANE_PAIRS)
@pytest.mark.parametrize("meter", [None, 0.0, 0.5, 1.0, -10.0, 10.0, float("nan"), float("inf")])
@pytest.mark.parametrize("level", ["good", "fair", "poor", "bad", "info"])
def test_render_split_lane_stress(pixel_trap: PixelTrap, top: str, bot: str, meter: Any, level: str):
    """Stress-test render_split_lane across strings, meters, and levels."""
    pixel_trap.reset()
    frame = render_split_lane(top, bot, meter=meter, level=level)

    # 1. Zero off-panel writes
    assert pixel_trap.off_panel == [], f"Off-panel writes: {pixel_trap.off_panel}"

    # 2. Safety gutter strictly black
    for y in range(HEIGHT):
        assert frame[y][GUTTER_X] == BLACK

    # 3. Row 3 is 100% black across the entire display (0..31)
    for x in range(WIDTH):
        assert frame[3][x] == BLACK, f"Row 3 separator violated at x={x}: {frame[3][x]}"

    # 4. Row 7 is dedicated to meter/rail, never text
    expected_meter_color = LEVEL_COLORS[level]
    if meter is None:
        for x in range(LANE_X0, LANE_X0 + LANE_WIDTH):
            assert frame[7][x] == TRACK
    else:
        # Clamped meter fill
        clamped_m = 1.0 if (math.isnan(meter) or meter > 1.0) else (0.0 if (math.isinf(meter) and meter < 0 or meter < 0.0) else float(meter))
        fill = max(1, min(LANE_WIDTH, round(clamped_m * LANE_WIDTH)))
        for i in range(LANE_WIDTH):
            exp = expected_meter_color if i < fill else TRACK
            assert frame[7][LANE_X0 + i] == exp


def test_render_split_lane_overflow_rejection():
    """Verify render_split_lane rejects strings exceeding lane width (23px)."""
    # 7 chars in FONT_3X3 is 7*3 + 6 = 27px > 23px
    with pytest.raises(ValueError, match="exceeds lane width"):
        render_split_lane("1234567", "OK")

    with pytest.raises(ValueError, match="exceeds lane width"):
        render_split_lane("OK", "1234567")

    with pytest.raises(ValueError, match="unknown level"):
        render_split_lane("1", "2", level="invalid_level")


# -----------------------------------------------------------------------------
# 4. Gutter Invariant Across All Frames
# -----------------------------------------------------------------------------

def test_gutter_invariant_monte_carlo(pixel_trap: PixelTrap):
    """Generate 1,000+ randomized frames across all layouts and assert column 8 is strictly (0, 0, 0)."""
    rng = random.Random(1337)
    printable_chars = string.ascii_letters + string.digits + "+-?:.%°"

    for iteration in range(1200):
        pixel_trap.reset()
        layout_choice = iteration % 5

        if layout_choice == 0:
            # render_sparkline
            length = rng.randint(0, 40)
            vals = [rng.choice([rng.uniform(-2, 2), float("nan"), float("inf"), float("-inf")]) for _ in range(length)]
            lbl = None if rng.random() < 0.3 else "".join(rng.choices(printable_chars, k=rng.randint(0, 4)))
            frame = render_sparkline(vals, label=lbl)

        elif layout_choice == 1:
            # render_split_lane
            top_len = rng.randint(0, 6)
            bot_len = rng.randint(0, 6)
            top = "".join(rng.choices(printable_chars, k=top_len))
            bot = "".join(rng.choices(printable_chars, k=bot_len))
            m = None if rng.random() < 0.2 else rng.choice([rng.uniform(-2, 2), float("nan"), float("inf")])
            lvl = rng.choice(list(LEVEL_COLORS.keys()))
            frame = render_split_lane(top, bot, meter=m, level=lvl)

        elif layout_choice == 2:
            # render_glance_lane
            val_len = rng.randint(1, 4)
            val = "".join(rng.choices(printable_chars, k=val_len))
            lbl = None if rng.random() < 0.5 else "".join(rng.choices(printable_chars, k=rng.randint(0, 2)))
            m = None if rng.random() < 0.3 else rng.uniform(0, 1)
            lvl = rng.choice(list(LEVEL_COLORS.keys()))
            frame = render_glance_lane(val, label=lbl, meter=m, level=lvl)

        elif layout_choice == 3:
            # render_dual_lane
            l_val = "".join(rng.choices(printable_chars, k=rng.randint(1, 2)))
            r_val = "".join(rng.choices(printable_chars, k=rng.randint(1, 2)))
            lm = None if rng.random() < 0.3 else rng.uniform(0, 1)
            rm = None if rng.random() < 0.3 else rng.uniform(0, 1)
            llvl = rng.choice(list(LEVEL_COLORS.keys()))
            rlvl = rng.choice(list(LEVEL_COLORS.keys()))
            frame = render_dual_lane(l_val, r_val, left_meter=lm, right_meter=rm, left_level=llvl, right_level=rlvl)

        else:
            # render_weather_silhouette
            pattern = tuple("".join(rng.choices([".", "S", "H", "C", "R", "W"], k=8)) for _ in range(8))
            frame = render_weather_silhouette(pattern)

        # Check gutter invariant
        for y in range(HEIGHT):
            assert frame[y][GUTTER_X] == (0, 0, 0), (
                f"Iteration {iteration} (layout {layout_choice}): Gutter column {GUTTER_X} violated at y={y}: {frame[y][GUTTER_X]}"
            )
        assert pixel_trap.off_panel == [], f"Iteration {iteration}: Off panel writes: {pixel_trap.off_panel}"
