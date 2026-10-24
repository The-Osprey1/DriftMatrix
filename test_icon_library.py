import pytest
from visual_system import (
    BLACK, PEARL, ICE, MINT, GOLD, CORAL, VIOLET, EMERALD, AMBER, RED_HOT
)
import icon_library


def test_icons():
    weather_sets = [icon_library.gamepad_icon()] + [
        icon_library.companion_icon(mood) for mood in ("idle", "focus", "sleep", "on_air", "rest")
    ] + [
        icon_library.weather_icon(kind, is_day=day)
        for kind, day in (("clear", True), ("clear", False), ("rain", True), ("drizzle", True),
                          ("snow", True), ("sleet", True), ("storm", True), ("fog", True),
                          ("cloudy", True))
    ]
    icons = [
        icon_library.cpu_icon(),
        icon_library.gpu_icon(),
        icon_library.thermometer_icon(0.5),
        icon_library.network_icon(),
        icon_library.sun_icon(),
        icon_library.rain_icon(),
        icon_library.cloud_icon(),
        icon_library.calendar_icon(),
        icon_library.bell_icon(),
        icon_library.hourglass_icon(),
        icon_library.warning_icon(),
        icon_library.heart_icon(),
        icon_library.lock_icon(),
        icon_library.controller_icon(),
        weather_sets[0],
        icon_library.earthquake_icon(0.5),
        icon_library.earthquake_icon(magnitude=5.2),
        icon_library.weather_icon("clear", is_day=True),
        icon_library.weather_icon("clear", is_day=False),
        icon_library.weather_icon("rain"),
        icon_library.weather_icon("drizzle"),
        icon_library.weather_icon("snow"),
        icon_library.weather_icon("sleet"),
        icon_library.weather_icon("storm"),
        icon_library.weather_icon("fog"),
        icon_library.weather_icon("cloudy"),
        icon_library.lungs_icon(level=0),
        icon_library.lungs_icon(aqi_or_level=125),
        icon_library.aircraft_icon(45.0),
        weather_sets[1],
        weather_sets[2],
        weather_sets[3],
        weather_sets[4],
        weather_sets[5],
        icon_library.miku_icon("live"),
        icon_library.miku_icon("echo"),
        icon_library.miku_icon("off"),
        icon_library.pc_icon("cpu"),
        icon_library.pc_icon("ram"),
        icon_library.pc_icon("gpu"),
        icon_library.pc_icon("net"),
        icon_library.pc_icon("both"),
    ]

    shaded = [frames for frames in icons if frames in weather_sets]
    for frames in icons:
        assert isinstance(frames, list)
        assert len(frames) > 0
        for frame in frames:
            assert len(frame) == 8
            assert len(frame[0]) == 32

            # Check range and colors
            for y in range(8):
                for x in range(32):
                    color = frame[y][x]
                    if x > 7:
                        assert color == BLACK
                    elif color != BLACK and frames not in shaded:
                        assert max(color) >= 174, f"Color {color} at ({x}, {y}) too dark"
            if frames in shaded:
                # Rich weather icons (2026-10-03): the silhouette must survive
                # brightness 1; shading tones (terminator, cloud underside,
                # rain tails, fading stars) may drop out, as Now Playing's
                # dark rim does.
                lit = [frame[y][x] for y in range(8) for x in range(8) if frame[y][x] != BLACK]
                bright = [c for c in lit if max(c) >= 174]
                assert bright, "weather icon vanishes at brightness 1"
                assert len(bright) >= 0.6 * len(lit), (
                    f"only {len(bright)} of {len(lit)} weather pixels survive brightness 1")


def test_frame_counts():
    assert len(icon_library.cpu_icon()) == 4
    assert len(icon_library.gpu_icon()) == 4
    assert len(icon_library.thermometer_icon(0.5)) == 1
    assert len(icon_library.network_icon()) == 4
    assert len(icon_library.sun_icon()) == 4
    assert len(icon_library.rain_icon()) == 4
    assert len(icon_library.cloud_icon()) == 2
    assert len(icon_library.calendar_icon()) == 1
    assert len(icon_library.bell_icon()) == 4
    assert len(icon_library.hourglass_icon()) == 6
    assert len(icon_library.warning_icon()) == 2
    assert len(icon_library.heart_icon()) == 2
    assert len(icon_library.lock_icon()) == 1
    assert len(icon_library.controller_icon()) == 4
    assert len(icon_library.gamepad_icon()) == 4
    assert len(icon_library.earthquake_icon(0.5)) == 8
    assert len(icon_library.aircraft_icon()) == 4
    assert len(icon_library.companion_icon()) == icon_library.COMPANION_FRAMES
    assert len(icon_library.lungs_icon(level=2)) == 4
    assert len(icon_library.weather_icon("storm")) == 8
    assert len(icon_library.weather_icon("rain")) == 6
    assert len(icon_library.weather_icon("fog")) == 8
    assert len(icon_library.miku_icon()) == 4
    assert len(icon_library.pc_icon("cpu")) == 4


# ---------------------------------------------------------------- Aircraft Tests

def test_aircraft_icon_categories():
    """Verify all 4 aircraft categories render with correct canonical colors and 4 frames."""
    expected_colors = {
        "airliner": ICE,
        "light": MINT,
        "jet": ICE,
        "heli": GOLD,
    }
    for kind, body_col in expected_colors.items():
        frames = icon_library.aircraft_icon(0, kind=kind)
        assert len(frames) == 4
        # Verify 120 ms beacon timing: beat 0 has RED_HOT, beats 1..3 do not
        has_beacon_0 = any(
            frames[0][y][x] == RED_HOT for y in range(8) for x in range(8)
        )
        assert has_beacon_0, f"Kind {kind} missing RED_HOT beacon on beat 0"
        for beat in (1, 2, 3):
            has_beacon_rest = any(
                frames[beat][y][x] == RED_HOT for y in range(8) for x in range(8)
            )
            assert not has_beacon_rest, f"Kind {kind} beacon must not be RED_HOT on beat {beat}"

        # Verify optical dominant channel >= 174 on all non-black pixels
        for f in frames:
            for y in range(8):
                for x in range(8):
                    px = f[y][x]
                    if px != BLACK:
                        assert max(px) >= 174, f"Sub-threshold pixel {px} in {kind}"
                # Column 8 safety gutter must be strictly black
                assert f[y][8] == BLACK


def test_aircraft_icon_compass_headings():
    """Verify all 8 compass octants produce distinct hand-drawn silhouettes."""
    headings = [0, 45, 90, 135, 180, 225, 270, 315]
    compass_labels = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]

    for kind in ("airliner", "light", "jet"):
        patterns_from_deg = [
            icon_library.heading_pattern(deg, kind=kind) for deg in headings
        ]
        patterns_from_str = [
            icon_library.heading_pattern(lbl, kind=kind) for lbl in compass_labels
        ]
        # Degrees and string labels must match
        assert patterns_from_deg == patterns_from_str
        # All 8 headings must be unique
        assert len(set(patterns_from_deg)) == 8


def test_aircraft_icon_heli_invariance():
    """Verify helicopter maintains side-view regardless of heading."""
    base_frames = icon_library.aircraft_icon(0, kind="heli")
    for deg in (45, 90, 180, 270, "SW"):
        other_frames = icon_library.aircraft_icon(deg, kind="heli")
        for b in range(4):
            assert base_frames[b] == other_frames[b]


def test_aircraft_icon_color_override():
    """Verify custom body color override."""
    frames = icon_library.aircraft_icon(0, kind="airliner", color=PEARL)
    assert len(frames) == 4
    # Beat 1..3 should have beacon and body at PEARL
    body_pixels = [
        frames[1][y][x] for y in range(8) for x in range(8) if frames[1][y][x] != BLACK
    ]
    assert all(px == PEARL for px in body_pixels)


# ---------------------------------------------------------------- Environmental Tests

def test_earthquake_icon_natural_8beat_tiers():
    # Tier 1 (minor): MINT, 1 tremor + 7 flatlines
    t1 = icon_library.earthquake_icon(0.2)
    assert len(t1) == 8

    # Tier 2 (moderate): GOLD, 3 waves + 5 flatlines
    t2 = icon_library.earthquake_icon(0.5)
    assert len(t2) == 8

    # Tier 3 (major): CORAL, 4 violent waves with PEARL flash + 4 flatlines
    t3 = icon_library.earthquake_icon(0.8)
    assert len(t3) == 8
    has_pearl = any(t3[frame_idx][y][x] == PEARL
                    for frame_idx in range(4)
                    for y in range(8) for x in range(8))
    assert has_pearl, "Tier 3 active deflection frames must have PEARL epicenter flash"

    # Resting frames (4..7) must NOT have PEARL flash
    for f_idx in range(4, 8):
        assert not any(t3[f_idx][y][x] == PEARL for y in range(8) for x in range(8))


def test_lungs_icon_epa_categories_and_phases():
    for lvl in range(6):
        frames = icon_library.lungs_icon(level=lvl)
        assert len(frames) == 4
        for f in frames:
            for y in range(8):
                for x in range(8):
                    c = f[y][x]
                    if c != BLACK:
                        assert max(c) >= 174, f"EPA level {lvl} pixel {c} below threshold"


# ---------------------------------------------------------------- Gaming & Character Tests

def test_gamepad_icon():
    frames = icon_library.gamepad_icon()
    assert len(frames) == 4
    # Press order: 1 (top) -> 3 (right) -> 2 (left) -> 4 (bottom).
    expected_pressed = [
        (5, 2, icon_library.MINT),
        (6, 3, icon_library.GOLD),
        (4, 3, icon_library.CORAL),
        (5, 4, icon_library.ICE),
    ]
    buttons = {(5, 2): icon_library.MINT, (6, 3): icon_library.GOLD,
               (4, 3): icon_library.CORAL, (5, 4): icon_library.ICE}
    for i, (px, py, base) in enumerate(expected_pressed):
        f = frames[i]
        # The pressed button flashes toward PEARL: brighter than its colour.
        assert sum(f[py][px]) > sum(base), f"frame {i}: pressed button does not flash"
        # Every other button stays softly lit in its own colour.
        for (bx, by), colour in buttons.items():
            if (bx, by) != (px, py):
                assert f[by][bx] == colour, f"frame {i}: button ({bx}, {by}) lost its colour"
        # The right of the icon box stays empty; the body survives brightness 1
        # except its grip shadow, which may fade.
        lit = [f[y][x] for y in range(8) for x in range(8) if f[y][x] != BLACK]
        assert all(f[y][x] == BLACK for y in range(8) for x in range(8, 32))
        assert sum(max(c) >= 174 for c in lit) >= 0.9 * len(lit)


def test_companion_icon_mood_features():
    eyes = [(2, 2), (3, 2), (2, 3), (3, 3), (5, 2), (6, 2), (5, 3), (6, 3)]
    for mood in ("idle", "focus", "sleep", "on_air", "rest"):
        frames = icon_library.companion_icon(mood)
        assert len(frames) == icon_library.COMPANION_FRAMES
        assert len({tuple(map(tuple, f)) for f in frames}) > 1, f"{mood} does not move"
        # It breathes: the crown dips one row at some point in the cycle.
        crowns = {next(y for y in range(8) if f[y][3] != BLACK) for f in frames}
        assert len(crowns) == 2, f"{mood} never breathes"

    idle = icon_library.companion_icon("idle")
    assert idle[0][2][2] == icon_library.COMPANION_EYE  # big white eyes
    assert idle[0][3][3] == icon_library.COMPANION_PUPIL
    # Glances: the left eye's pupil sits on both sides of the eye over a cycle
    # (the eye dips a row while it breathes, so look at both rows).
    pupils = {x for f in idle for y in (3, 4) for x in (2, 3)
              if f[y][x] == icon_library.COMPANION_PUPIL and f[y - 1][x] == icon_library.COMPANION_EYE}
    assert pupils == {2, 3}
    last = idle[-1]
    assert last[2][2] != icon_library.COMPANION_EYE  # blinks

    focus = icon_library.companion_icon("focus")
    assert all(any(f[2 + b][x] == GOLD for b in (0, 1) for x in range(1, 7)) for f in focus)
    assert any(PEARL in row for f in focus for row in f)  # the glint

    on_air = icon_library.companion_icon("on_air")
    assert all(any(px == RED_HOT for row in f for px in row) for f in on_air)

    rest = icon_library.companion_icon("rest")
    assert all(any(px == AMBER for row in f for px in row) for f in rest)  # the mug

    sleep = icon_library.companion_icon("sleep")
    assert any(PEARL in row for f in sleep for row in f)  # the z


def test_miku_icon():
    for state in ("live", "echo", "off"):
        for act in ("idle", "listening", "thinking", "speaking"):
            frames = icon_library.miku_icon(state, act)
            assert len(frames) == 4
            for f in frames:
                for y in range(8):
                    for x in range(32):
                        col = f[y][x]
                        if x > 7:
                            assert col == BLACK
                        elif col != BLACK:
                            assert max(col) >= 174

    # Check collar colors
    assert icon_library.miku_icon("live")[0][6][3] == icon_library.EMERALD
    assert icon_library.miku_icon("echo")[0][6][3] == icon_library.AMBER
    assert icon_library.miku_icon("off")[0][6][3] == icon_library.CORAL

    # Check tail sway
    live = icon_library.miku_icon("live")
    assert live[0][7][0] == icon_library.ICE and live[0][7][7] == icon_library.ICE
    assert live[1][7][1] == icon_library.ICE and live[1][7][6] == icon_library.ICE
    off = icon_library.miku_icon("off")
    assert all(off[i][7][0] == icon_library.ICE and off[i][7][7] == icon_library.ICE for i in range(4))


def test_pc_icon():
    for metric in ("cpu", "ram", "gpu", "net", "both"):
        frames = icon_library.pc_icon(metric)
        assert len(frames) == 4
        for f in frames:
            for y in range(8):
                for x in range(32):
                    col = f[y][x]
                    if x > 7:
                        assert col == BLACK
                    elif col != BLACK:
                        assert max(col) >= 174

    # Alert severity scaling
    cpu_warn = icon_library.pc_icon("cpu", level=1)
    cpu_crit = icon_library.pc_icon("cpu", level=2)
    assert cpu_warn[0][0][2] == icon_library.AMBER
    assert cpu_crit[0][0][2] == icon_library.CORAL
