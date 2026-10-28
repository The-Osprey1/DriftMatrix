"""Centralized 8x8 animated icon library for AWTRIX panel apps.

Every icon is a list of frames (list[Frame]).  A single-frame list is
static; multi-frame lists animate at the 120 ms panel beat.  Icons use
the visual_system palette and are designed to be legible as silhouettes
at brightness 1 (dominant channel >= 174).
"""

from visual_system import (
    Frame, new_frame, put_pixel, RGB, gamma_lerp,
    PEARL, ICE, MINT, GOLD, AMBER, CORAL, RED_HOT, VIOLET, EMERALD, BLACK
)

try:
    from weather import weather_icons
except ImportError:
    try:
        import weather_icons
    except ImportError:
        weather_icons = None  # type: ignore[assignment]


DARK_MINT = (51, 174, 114)
DARK_GOLD = (174, 145, 53)
DARK_CORAL = (174, 51, 62)

def _build_frame(pattern: tuple[str, ...], color_map: dict[str, tuple[int, int, int]]) -> Frame:
    frame = new_frame()
    for y, row in enumerate(pattern[:8]):
        for x, char in enumerate(row[:8]):
            if char in color_map:
                put_pixel(frame, x, y, color_map[char])
    return frame

def cpu_icon() -> list[Frame]:
    base = (
        ".I.II.I.",
        "IIIIIIII",
        "I......I",
        "I.MMMM.I",
        "I.MMMM.I",
        "I......I",
        "IIIIIIII",
        ".I.II.I.",
    )
    frames = []
    for i in range(4):
        mint_color = MINT if i % 2 == 0 else DARK_MINT
        frames.append(_build_frame(base, {"I": ICE, "M": mint_color}))
    return frames

def gpu_icon() -> list[Frame]:
    base = (
        "........",
        "IIIIIIII",
        "I......I",
        "I......I",
        "I......I",
        "I......I",
        "IIIIIIII",
        "..II.II.",
    )
    frames = []
    for i in range(4):
        frame = _build_frame(base, {"I": ICE})
        if i % 2 == 0:
            for x, y in [(3, 2), (4, 2), (3, 5), (4, 5), (2, 3), (2, 4), (5, 3), (5, 4)]:
                put_pixel(frame, x, y, EMERALD)
        else:
            for x, y in [(2, 2), (3, 3), (4, 4), (5, 5), (5, 2), (4, 3), (3, 4), (2, 5)]:
                put_pixel(frame, x, y, EMERALD)
        frames.append(frame)
    return frames

def thermometer_icon(level: float) -> list[Frame]:
    level = max(0.0, min(1.0, level))
    base = (
        "...II...",
        "..I..I..",
        "..I..I..",
        "..I..I..",
        ".I....I.",
        ".I....I.",
        "..IIII..",
        "........",
    )
    frame = _build_frame(base, {"I": ICE})
    
    fill_rows = int(round(level * 4))
    
    if level < 0.5:
        color = MINT
    elif level < 0.8:
        color = GOLD
    else:
        color = CORAL
        
    put_pixel(frame, 3, 4, color)
    put_pixel(frame, 4, 4, color)
    put_pixel(frame, 2, 5, color)
    put_pixel(frame, 3, 5, color)
    put_pixel(frame, 4, 5, color)
    put_pixel(frame, 5, 5, color)
    
    for i in range(fill_rows):
        y = 3 - i
        put_pixel(frame, 3, y, color)
        put_pixel(frame, 4, y, color)
        
    return [frame]

def network_icon() -> list[Frame]:
    up_arrow = (
        "...I....",
        "..III...",
        ".IIIII..",
        "...I....",
        "...I....",
        "...I....",
        "........",
        "........",
    )
    down_arrow = (
        "........",
        "........",
        "....I...",
        "....I...",
        "....I...",
        "..IIIII.",
        "...III..",
        "....I...",
    )
    frames = []
    for i in range(4):
        f = new_frame()
        up_color = MINT if i in (0, 2) else DARK_MINT
        down_color = CORAL if i in (1, 2) else DARK_CORAL
        for y, row in enumerate(up_arrow):
            for x, char in enumerate(row):
                if char == "I": put_pixel(f, x, y, up_color)
        for y, row in enumerate(down_arrow):
            for x, char in enumerate(row):
                if char == "I": put_pixel(f, x, y, down_color)
        frames.append(f)
    return frames

def sun_icon() -> list[Frame]:
    frames = []
    base_center = (
        "........",
        "........",
        "..PPPP..",
        "..PPPP..",
        "..PPPP..",
        "..PPPP..",
        "........",
        "........",
    )
    for i in range(4):
        f = _build_frame(base_center, {"P": PEARL})
        if i % 2 == 0:
            put_pixel(f, 3, 0, GOLD); put_pixel(f, 4, 0, GOLD)
            put_pixel(f, 3, 7, GOLD); put_pixel(f, 4, 7, GOLD)
            put_pixel(f, 0, 3, GOLD); put_pixel(f, 0, 4, GOLD)
            put_pixel(f, 7, 3, GOLD); put_pixel(f, 7, 4, GOLD)
        else:
            put_pixel(f, 1, 1, GOLD); put_pixel(f, 6, 1, GOLD)
            put_pixel(f, 1, 6, GOLD); put_pixel(f, 6, 6, GOLD)
            put_pixel(f, 2, 1, GOLD); put_pixel(f, 5, 1, GOLD)
            put_pixel(f, 2, 6, GOLD); put_pixel(f, 5, 6, GOLD)
        frames.append(f)
    return frames

def rain_icon() -> list[Frame]:
    cloud = (
        "........",
        "..CCC...",
        ".CCCCCC.",
        "CCCCCCCC",
        "........",
        "........",
        "........",
        "........",
    )
    frames = []
    drops = [
        [(1, 5), (4, 4), (6, 6)],
        [(1, 6), (4, 5), (6, 7)],
        [(1, 7), (4, 6), (6, 4)],
        [(1, 4), (4, 7), (6, 5)],
    ]
    for i in range(4):
        f = _build_frame(cloud, {"C": ICE})
        for drop_x, drop_y in drops[i]:
            put_pixel(f, drop_x, drop_y, ICE)
        frames.append(f)
    return frames

def cloud_icon() -> list[Frame]:
    c1 = (
        "........",
        "........",
        "...CCC..",
        "..CCCCCC",
        ".CCCCCCC",
        "........",
        "........",
        "........",
    )
    c2 = (
        "........",
        "........",
        "..CCC...",
        ".CCCCCC.",
        "CCCCCCC.",
        "........",
        "........",
        "........",
    )
    return [
        _build_frame(c1, {"C": ICE}),
        _build_frame(c2, {"C": ICE})
    ]

def calendar_icon() -> list[Frame]:
    cal = (
        ".IIIIII.",
        "I......I",
        "IIIIIIII",
        "I.I..I.I",
        "I......I",
        "I...C..I",
        "I......I",
        ".IIIIII.",
    )
    return [_build_frame(cal, {"I": ICE, "C": CORAL})]

def bell_icon() -> list[Frame]:
    b_center = (
        "...GG...",
        "..GGGG..",
        "..GGGG..",
        ".GGGGGG.",
        ".GGGGGG.",
        "GGGGGGGG",
        "........",
        "...GG...",
    )
    b_left = (
        "...GG...",
        "..GGGG..",
        ".GGGG...",
        ".GGGGGG.",
        "GGGGGG..",
        "GGGGGGGG",
        "........",
        "..GG....",
    )
    b_right = (
        "...GG...",
        "..GGGG..",
        "...GGGG.",
        ".GGGGGG.",
        "..GGGGGG",
        "GGGGGGGG",
        "........",
        "....GG..",
    )
    return [
        _build_frame(b_left, {"G": GOLD}),
        _build_frame(b_right, {"G": GOLD}),
        _build_frame(b_center, {"G": GOLD}),
        _build_frame(b_center, {"G": GOLD}),
    ]

def hourglass_icon() -> list[Frame]:
    base = (
        "IIIIIIII",
        ".IIIIII.",
        "..IIII..",
        "...II...",
        "...II...",
        "..IIII..",
        ".IIIIII.",
        "IIIIIIII",
    )
    frames = []
    sand_states = [
        [(3, 2), (4, 2), (3, 3), (4, 3)],
        [(3, 2), (4, 2), (4, 3), (4, 4)],
        [(3, 2), (4, 2), (4, 4), (4, 5)],
        [(3, 2), (4, 5), (3, 6), (4, 6)],
        [(4, 2), (3, 6), (4, 6), (3, 5)],
        [(3, 6), (4, 6), (3, 5), (4, 5)]
    ]
    for state in sand_states:
        f = _build_frame(base, {"I": ICE})
        put_pixel(f, 3, 2, BLACK); put_pixel(f, 4, 2, BLACK)
        put_pixel(f, 3, 3, BLACK); put_pixel(f, 4, 3, BLACK)
        put_pixel(f, 3, 4, BLACK); put_pixel(f, 4, 4, BLACK)
        put_pixel(f, 3, 5, BLACK); put_pixel(f, 4, 5, BLACK)
        put_pixel(f, 3, 6, BLACK); put_pixel(f, 4, 6, BLACK)
        for sx, sy in state:
            put_pixel(f, sx, sy, GOLD)
        frames.append(f)
    return frames

def warning_icon() -> list[Frame]:
    tri_filled = (
        "...GG...",
        "..G..G..",
        "..GIIG..",
        ".G.II.G.",
        ".G.II.G.",
        "G......G",
        "G..II..G",
        "GGGGGGGG",
    )
    return [
        _build_frame(tri_filled, {"G": GOLD, "I": ICE}),
        _build_frame(tri_filled, {"G": DARK_GOLD, "I": ICE}),
    ]

def heart_icon() -> list[Frame]:
    h_small = (
        "........",
        "..R..R..",
        ".RRRRRR.",
        ".RRRRRR.",
        "..RRRR..",
        "...RR...",
        "........",
        "........",
    )
    h_large = (
        ".RR..RR.",
        "RRRRRRRR",
        "RRRRRRRR",
        "RRRRRRRR",
        ".RRRRRR.",
        "..RRRR..",
        "...RR...",
        "........",
    )
    return [
        _build_frame(h_small, {"R": CORAL}),
        _build_frame(h_large, {"R": CORAL}),
    ]

def lock_icon() -> list[Frame]:
    lock = (
        "...EE...",
        "..E..E..",
        "..E..E..",
        ".EEEEEE.",
        ".E....E.",
        ".E.EE.E.",
        ".E....E.",
        ".EEEEEE.",
    )
    return [_build_frame(lock, {"E": EMERALD})]

def controller_icon() -> list[Frame]:
    frames = []
    base = (
        "........",
        "........",
        ".IIIIII.",
        "IIIIIIII",
        "IIIIIIII",
        "IIIIIIII",
        ".II..II.",
        "........",
    )
    for i in range(4):
        f = _build_frame(base, {"I": ICE})
        put_pixel(f, 1, 4, BLACK)
        put_pixel(f, 2, 3, BLACK)
        put_pixel(f, 2, 5, BLACK)
        put_pixel(f, 3, 4, BLACK)
        put_pixel(f, 2, 4, BLACK)
        
        put_pixel(f, 5, 4, BLACK)
        put_pixel(f, 6, 3, BLACK)
        
        if i == 0: put_pixel(f, 2, 3, MINT)
        elif i == 1: put_pixel(f, 3, 4, MINT)
        elif i == 2: put_pixel(f, 2, 5, MINT)
        elif i == 3: put_pixel(f, 1, 4, MINT)
        
        frames.append(f)
    return frames

def gamepad_icon() -> list[Frame]:
    """8x8 controller (2026-10-03, to the Now Playing standard).

    A shaded body (lit top edge, shadowed grips), a PEARL d-pad cross and four
    face buttons always softly lit in their colours; each beat one is pressed
    and flashes toward PEARL, in the order 1 (top) -> 3 (right) -> 2 (left) ->
    4 (bottom).  Every lit pixel keeps a dominant channel >= 174 except the
    grip shadow, which may fade at brightness 1.
    """
    frames = []
    for lit in "1324":
        f = new_frame()
        for y, row in enumerate(GAMEPAD_BODY):
            for x, ch in enumerate(row):
                if ch in GAMEPAD_TONES:
                    put_pixel(f, x, y, GAMEPAD_TONES[ch])
        for x, y in GAMEPAD_DPAD:
            put_pixel(f, x, y, GAMEPAD_DPAD_COLOR)
        for key, (x, y) in GAMEPAD_BUTTONS.items():
            colour = GAMEPAD_BUTTON_COLORS[key]
            put_pixel(f, x, y, gamma_lerp(colour, PEARL, 0.65) if key == lit else colour)
        frames.append(f)
    return frames


GAMEPAD_BODY = (
    "........",
    ".TTTTTT.",
    "TBBBBBBT",
    "BBBBBBBB",
    "BBBBBBBB",
    "BBB..BBB",
    ".S....S.",
    "........",
)
GAMEPAD_TONES = {"T": (206, 132, 255), "B": (160, 84, 230), "S": (96, 46, 150)}
GAMEPAD_DPAD = ((2, 2), (1, 3), (2, 3), (3, 3), (2, 4))
GAMEPAD_DPAD_COLOR = (232, 232, 244)
GAMEPAD_BUTTONS = {"1": (5, 2), "3": (6, 3), "2": (4, 3), "4": (5, 4)}


GAMEPAD_PATTERN = (
    "........",
    ".BBBBBB.",
    "BB.BB1BB",
    "B...2.3B",
    "BB.BB4BB",
    "BBB..BBB",
    ".B....B.",
    "........",
)
GAMEPAD_BUTTON_COLORS = {"1": MINT, "2": CORAL, "3": GOLD, "4": ICE}

# ---------------------------------------------------------------- Seismograph
# Natural 8-beat seismograph pulses: flatline resting frames + deflections scaled
# by magnitude tiers (MINT, GOLD, CORAL) with PEARL peak epicenter flash.

BASE_TRACE = [4, 4, 4, 4, 4, 4, 4, 4]

WAVE_L1 = [
    [4, 4, 3, 5, 4, 3, 4, 4],
]
WAVE_L2 = [
    [4, 4, 4, 3, 5, 2, 6, 4],
    [4, 3, 1, 6, 2, 5, 3, 4],
    [3, 5, 2, 6, 4, 4, 4, 4],
]
WAVE_L3 = [
    [4, 4, 3, 1, 6, 2, 5, 4],
    [4, 2, 0, 6, 1, 6, 3, 4],
    [3, 6, 1, 6, 0, 5, 4, 4],
    [2, 5, 2, 6, 3, 4, 4, 4],
]

SEISMIC_WAVES = {1: WAVE_L1, 2: WAVE_L2, 3: WAVE_L3}
SEISMIC_SHAKES = {
    1: [(1, 0)] + [(0, 0)] * 7,
    2: [(1, 0), (2, 0), (3, 0)] + [(0, 0)] * 5,
    3: [(1, 0), (2, 0), (3, 0), (4, 0)] + [(0, 0)] * 4,
}
SEISMIC_COLORS = {1: MINT, 2: GOLD, 3: CORAL}


def _draw_seismic_trace(
    frame: Frame,
    y_points: list[int],
    color: tuple[int, int, int],
    spark: bool = False,
) -> None:
    for x in range(len(y_points) - 1):
        y1, y2 = y_points[x], y_points[x + 1]
        step = 1 if y2 >= y1 else -1
        for y in range(y1, y2 + step, step):
            if 0 <= y < 8 and 0 <= x < 8:
                put_pixel(frame, x, y, color)
    if 0 <= y_points[-1] < 8:
        put_pixel(frame, 7, y_points[-1], color)
    if spark:
        min_y = min(y_points)
        peak_x = y_points.index(min_y)
        put_pixel(frame, peak_x, min_y, PEARL)


def earthquake_icon(
    magnitude_or_level: float = 0.5,
    *,
    magnitude: float | None = None,
    level: float | None = None,
    beats: int = 8,
) -> list[Frame]:
    """8x8 dynamic seismograph icon with natural 8-beat cadence.

    Accepts either a normalized level (0.0..1.0) or a Richter magnitude.
    Tier 1 (<3.0 / <0.33): 7 flatline + 1 tremor frame in MINT.
    Tier 2 (3.0..4.5 / 0.33..0.66): 5 flatline + 3 wave frames in GOLD.
    Tier 3 (>=4.5 / >=0.66): 4 flatline + 4 violent wave frames in CORAL with PEARL flash.
    """
    if magnitude is not None:
        mag = float(magnitude)
        tier = 1 if mag < 3.0 else 2 if mag < 4.5 else 3
    elif level is not None:
        lvl = max(0.0, min(1.0, float(level)))
        tier = 1 if lvl < 0.33 else 2 if lvl < 0.66 else 3
    elif magnitude_or_level > 1.0 or magnitude_or_level < 0.0:
        val = float(magnitude_or_level)
        if val > 1.0:
            tier = 1 if val < 3.0 else 2 if val < 4.5 else 3
        else:
            tier = 1
    else:
        lvl = max(0.0, min(1.0, float(magnitude_or_level)))
        tier = 1 if lvl < 0.33 else 2 if lvl < 0.66 else 3

    color = SEISMIC_COLORS[tier]
    waves = SEISMIC_WAVES[tier]

    if beats != 8:
        # Legacy compact mode (1 flatline + waves)
        frames = []
        base_frame = new_frame()
        _draw_seismic_trace(base_frame, BASE_TRACE, color)
        frames.append(base_frame)
        for w in waves:
            f = new_frame()
            _draw_seismic_trace(f, w, color, spark=(tier == 3))
            frames.append(f)
        return frames

    shakes = SEISMIC_SHAKES[tier]
    frames = []
    rest_motion = (0, 0)
    move_count = 0
    for motion in shakes:
        f = new_frame()
        if motion == rest_motion:
            _draw_seismic_trace(f, BASE_TRACE, color, spark=False)
        else:
            wave = waves[move_count % len(waves)]
            move_count += 1
            _draw_seismic_trace(f, wave, color, spark=(tier == 3))
        frames.append(f)
    return frames


# ---------------------------------------------------------------- Aircraft & Rotorcraft

# One silhouette per kind of aircraft, each drawn nose-up (N) and nose to the
# upper right (NE); the other six headings are quarter turns of those two.
# "B" is the body, "L" the tail beacon that blinks RED_HOT on beat 0.
# Drawn in the 7x7 box at top left so a quarter turn keeps row and column 7 empty.
# Diagonals are hand-drawn to preserve crisp single-pixel fuselages and wings.
_AIRCRAFT_SILHOUETTES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # Swept wings with a visible sweep, wide tailplane.
    "airliner": (
        (
            "...B...",
            "...B...",
            "..BBB..",
            ".BBBBB.",
            "BB.B.BB",
            "...B...",
            "..BLB..",
        ),
        (
            "......B",
            ".....B.",
            ".BBBB..",
            "...BB..",
            "..B.B..",
            "BL..B..",
            ".B.....",
        ),
    ),
    # Straight wing right behind the nose, long thin tail: Cessna or Piper.
    "light": (
        (
            "...B...",
            "BBBBBBB",
            "...B...",
            "...B...",
            "...B...",
            ".BBLBB.",
            ".......",
        ),
        (
            "..B....",
            "...B.B.",
            "....B..",
            "...B.B.",
            "B.B...B",
            ".L.....",
            "..B....",
        ),
    ),
    # Small swept wing set far back, like a dart: business jet.
    "jet": (
        (
            "...B...",
            "...B...",
            "...B...",
            "..BBB..",
            ".BBBBB.",
            "...B...",
            "..BLB..",
        ),
        (
            "......B",
            ".....B.",
            "....B..",
            ".BBB...",
            "..BB...",
            "BL.B...",
            ".B.....",
        ),
    ),
}

# Side view with rotor on top and tail beacon at row 3 col 7.
# A helicopter's heading matters less than telling it apart at a glance, so it does not rotate.
_HELI_SILHOUETTE: tuple[str, ...] = (
    "BBBBBBB.",
    "...B....",
    ".BBBB...",
    "BBBBBBBL",
    ".BBBB...",
    "..B.B...",
    ".BBBBB..",
    "........",
)

AIRCRAFT_DEFAULT_COLORS: dict[str, RGB] = {
    "airliner": ICE,
    "light": MINT,
    "jet": ICE,
    "heli": GOLD,
}

_COMPASS_OCTANTS: dict[str, int] = {
    "N": 0, "NORTH": 0,
    "NE": 1, "NORTHEAST": 1,
    "E": 2, "EAST": 2,
    "SE": 3, "SOUTHEAST": 3,
    "S": 4, "SOUTH": 4,
    "SW": 5, "SOUTHWEST": 5,
    "W": 6, "WEST": 6,
    "NW": 7, "NORTHWEST": 7,
}


def _rotate_pattern(pattern: tuple[str, ...], quarter_turns: int) -> tuple[str, ...]:
    """Rotate clockwise inside the 7x7 box at the top-left, keeping row/col 7 empty."""
    grid = [row[:7].ljust(7, ".") for row in pattern[:7]]
    for _ in range(quarter_turns % 4):
        grid = ["".join(grid[6 - c][r] for c in range(7)) for r in range(7)]
    return tuple(row + "." for row in grid) + ("........",)


def heading_pattern(
    track_deg: float | int | str | None = None,
    kind: str = "airliner",
) -> tuple[str, ...]:
    """Return 8x8 pattern strings for the given heading and aircraft kind."""
    k = kind.lower()
    if k in ("heli", "helicopter", "rotorcraft"):
        return _HELI_SILHOUETTE
    if k not in _AIRCRAFT_SILHOUETTES:
        k = "airliner"

    if track_deg is None:
        octant = 0
    elif isinstance(track_deg, str):
        cleaned = track_deg.strip().upper()
        if cleaned in _COMPASS_OCTANTS:
            octant = _COMPASS_OCTANTS[cleaned]
        else:
            try:
                deg = float(cleaned)
                octant = int(((deg % 360) + 22.5) // 45) % 8
            except ValueError:
                octant = 0
    else:
        octant = int(((float(track_deg) % 360) + 22.5) // 45) % 8

    north, northeast = _AIRCRAFT_SILHOUETTES[k]
    return _rotate_pattern(north if octant % 2 == 0 else northeast, octant // 2)


def aircraft_icon(
    track_deg: float | int | str | None = None,
    kind: str = "airliner",
    color: RGB | None = None,
) -> list[Frame]:
    """8x8 directional aircraft silhouette with blinking beacon.

    Args:
        track_deg: Heading angle in degrees (0..360, clockwise from north),
                   compass string ('N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'),
                   or None (defaults to north). Ignored for rotorcraft ('heli').
        kind: Aircraft category ('airliner', 'light', 'jet', 'heli').
        color: Optional body color override. Defaults to category canonical color:
               ICE for airliner/jet, MINT for light, GOLD for heli.

    Returns:
        4-frame list of 8x8 frames (120 ms cadence):
        Beat 0: Tail beacon is RED_HOT (flash).
        Beats 1..3: Tail beacon is body color (rest).
    """
    k = kind.lower()
    body = color if color is not None else AIRCRAFT_DEFAULT_COLORS.get(k, ICE)
    pat = heading_pattern(track_deg, k)
    return [
        _build_frame(pat, {"B": body, "L": (RED_HOT if beat == 0 else body)})
        for beat in range(4)
    ]


# ---------------------------------------------------------------- Character Avatars

COMPANION_BEAT_MS = 240  # a gentle 1.92 s cycle, on the panel's 120 ms beat
COMPANION_FRAMES = 8
# Keep the same 1.92-second cycle, with a brief natural blink and more time
# in the open-eye poses. Every delay remains on the physical 120 ms beat.
COMPANION_FRAME_MS = (360, 240, 240, 360, 240, 240, 120, 120)
# Lit edge, body, belly, cool shadow, feet. Each mood has a coherent palette
# that also colours its lane; the same character stays recognizable at one bit.
COMPANION_PALETTES: dict[str, dict[str, RGB]] = {
    "idle": {"L": (154, 255, 220), "B": (38, 214, 188), "b": (102, 237, 238),
             "S": (44, 69, 150), "F": (116, 89, 224)},
    "focus": {"L": (221, 189, 255), "B": (158, 107, 239), "b": (119, 178, 255),
              "S": (63, 54, 133), "F": (97, 114, 214)},
    "rest": {"L": (255, 222, 160), "B": (255, 169, 108), "b": (255, 196, 139),
             "S": (136, 63, 79), "F": (232, 112, 123)},
    "on_air": {"L": (255, 173, 209), "B": (245, 94, 145), "b": (248, 141, 180),
               "S": (109, 47, 124), "F": (184, 77, 204)},
    "sleep": {"L": (184, 215, 255), "B": (111, 141, 232), "b": (165, 164, 250),
              "S": (58, 58, 122), "F": (115, 99, 195)},
}
COMPANION_TONES = COMPANION_PALETTES["idle"]
COMPANION_ART = (
    "..LLLL..",
    ".LBBBBL.",
    "LSSSSSSS",
    "LSbbbbSS",
    "LSbbbbSS",
    ".LbbbbS.",
    "..SSSS..",
    "..F..F..",
)
COMPANION_EYES = ((2, 2), (5, 2))  # top-left of each 2x2 eye
COMPANION_EYE: RGB = (255, 255, 255)
COMPANION_PUPIL: RGB = (0, 0, 0)  # an unlit LED is the crispest pupil
COMPANION_BLUSH: RGB = (255, 128, 160)
COMPANION_STEAM: RGB = (236, 246, 255)


def companion_icon(mood: str = "idle") -> list[Frame]:
    """A jewel-coloured desk sprite with expressive props and a moving glow.

    Eight distinct poses over a 1.92-second cycle. A shaded shell surrounds a
    dark faceplate; breathing, glances and a small wave give the idle pose life.
    Glasses reflect left and right, a mug's steam curls, and the headset has
    a real boom mic. The dark shadow can fade at brightness 1; the lit body,
    eyes, and mood prop retain the character's silhouette.
    """
    mood_str = str(mood.value if hasattr(mood, "value") else mood).lower()
    if mood_str not in ("idle", "focus", "sleep", "on_air", "rest"):
        mood_str = "idle"
    tones = COMPANION_PALETTES[mood_str]
    frames = []
    for t in range(COMPANION_FRAMES):
        f = new_frame()

        def put(x: int, y: int, colour: RGB) -> None:
            if 0 <= x < 8 and 0 <= y < 8:
                put_pixel(f, x, y, colour)

        bob = 1 if t >= (4 if mood_str == "sleep" else 5) else 0
        # The light belongs to the object: a broad upper-left highlight and
        # gently changing body tone, rather than a white dot orbiting the face.
        breath = (0.96, 0.98, 1.0, 0.99, 0.97, 0.94, 0.92, 0.93)[t]
        for y, row in enumerate(COMPANION_ART):
            for x, ch in enumerate(row):
                if ch in tones:
                    colour = tones[ch] if ch in ('L', 'S', 'F') else tuple(round(c * breath) for c in tones[ch])
                    put(x, y + (bob if y < 7 else 0), colour)
        put(1, 1 + bob, PEARL)
        blink = t == COMPANION_FRAMES - 1
        look = (0, 1, 1, 0, -1, -1, 0, 0)[t] if mood_str == "idle" else 0

        def open_eyes() -> None:
            for ex, ey in COMPANION_EYES:
                if blink:
                    put(ex, ey + 1 + bob, COMPANION_PUPIL)
                    put(ex + 1, ey + 1 + bob, COMPANION_PUPIL)
                    continue
                for dx in (0, 1):
                    put(ex + dx, ey + bob, COMPANION_EYE)
                    put(ex + dx, ey + 1 + bob, COMPANION_EYE)
                put(ex + (1 if look >= 0 else 0), ey + 1 + bob, COMPANION_PUPIL)

        def closed_eyes(colour: RGB) -> None:
            for ex, ey in COMPANION_EYES:
                put(ex, ey + 1 + bob, colour)
                put(ex + 1, ey + 1 + bob, colour)

        if mood_str == "idle":
            open_eyes()
            put(1, 4 + bob, COMPANION_BLUSH)
            put(6, 4 + bob, COMPANION_BLUSH)
            put(3, 5 + bob, CORAL)
            put(4, 5 + bob, CORAL)
            # A little articulated hand lifts, pauses, then returns to the body.
            if t in (1, 2, 3, 4):
                hand_y = (5, 4, 4, 5)[t - 1]
                put(7, hand_y, tones['L'])
                put(7, hand_y + 1, tones['B'])
        elif mood_str == "focus":
            open_eyes()
            for x in range(1, 7):
                put(x, 2 + bob, GOLD)
            # Reflection sweeps back rather than jumping across the glasses.
            put((1, 2, 3, 4, 5, 6, 5, 4)[t], 2 + bob, PEARL)
            put(3, 5 + bob, tones["S"])
            put(4, 5 + bob, tones["S"])
        elif mood_str == "on_air":
            open_eyes()
            for x in range(2, 6):
                put(x, 1 + bob, RED_HOT)
            put(0, 3 + bob, RED_HOT)
            put(7, 3 + bob, RED_HOT)
            put(7, 4 + bob, RED_HOT)
            put(6, 5 + bob, RED_HOT)
            put(5, 5 + bob, PEARL)  # headset boom microphone
            put(3, 5 + bob, COMPANION_PUPIL)
            if t in (2, 3, 6):
                put(3, 4 + bob, COMPANION_PUPIL)
        elif mood_str == "rest":
            closed_eyes(COMPANION_PUPIL)
            put(1, 4 + bob, COMPANION_BLUSH)
            # A mint mug contrasts against the warm body; the rim and handle
            # remain distinct even when the sprite dips with its breathing.
            put(5, 4 + bob, PEARL)
            put(6, 4 + bob, PEARL)
            put(5, 5 + bob, MINT)
            put(6, 5 + bob, MINT)
            put(7, 4 + bob, AMBER)
            put(7, 5 + bob, AMBER)
            # Curl upward and back into the cup; no left-to-right wrap jump.
            steam_x, steam_y = ((6, 3), (7, 2), (7, 1), (6, 0),
                                (5, 0), (5, 1), (6, 2), (6, 3))[t]
            put(steam_x, steam_y, COMPANION_STEAM)
        else:  # sleep
            closed_eyes(COMPANION_PUPIL)
            # A little nightcap, with the tassel rocking softly as it breathes.
            put(5, 0 + bob, VIOLET)
            put(6, 0 + bob, VIOLET)
            put(7, (1 if t in (2, 3, 4, 5) else 0) + bob, PEARL)
        frames.append(f)
    return frames


def miku_icon(state: str = "live", activity: str = "idle") -> list[Frame]:
    """8x8 animated Miku Sentinel avatar.

    Features 4-frame twin-tails sway, blinking, state collars (EMERALD for LIVE,
    AMBER for ECHO, CORAL for OFF), and activity badges (LISTENING sound pips,
    THINKING traveling dot, SPEAKING animated mouth).
    """
    state_str = str(state.value if hasattr(state, "value") else state).lower()
    if state_str not in ("live", "echo", "off"):
        state_str = "live"
    activity_str = str(activity.value if hasattr(activity, "value") else activity).lower()
    if activity_str not in ("idle", "listening", "thinking", "speaking"):
        activity_str = "idle"

    accent = {
        "live": EMERALD,
        "echo": AMBER,
        "off": CORAL,
    }[state_str]

    frames = []
    for p in range(4):
        f = new_frame()
        # Fringe and crown
        for x in range(2, 6):
            put_pixel(f, x, 1, ICE)
            put_pixel(f, x, 2, ICE)

        # Face
        for y in range(3, 6):
            for x in range(2, 6):
                put_pixel(f, x, y, PEARL)

        # Eyes
        if state_str == "off":
            put_pixel(f, 2, 4, VIOLET)
            put_pixel(f, 5, 4, VIOLET)
        else:
            blinking = (state_str == "live" and p == 3)
            eye = VIOLET if blinking else accent
            put_pixel(f, 2, 4, eye)
            put_pixel(f, 5, 4, eye)

        # Collar
        for x in range(2, 6):
            put_pixel(f, x, 6, accent)

        # Twin tails
        sway = 0 if state_str == "off" else (1 if p in (1, 2) else 0)
        for y in range(1, 7):
            put_pixel(f, 1, y, ICE)
            put_pixel(f, 6, y, ICE)
        put_pixel(f, 0 + sway, 7, ICE)
        put_pixel(f, 7 - sway, 7, ICE)

        # Activity overlays
        if state_str != "off":
            if activity_str == "listening":
                y = 3 + (p % 2)
                put_pixel(f, 0, y, accent)
                put_pixel(f, 7, y, accent)
            elif activity_str == "thinking":
                put_pixel(f, 2 + p, 0, accent)
            elif activity_str == "speaking":
                put_pixel(f, 3, 5, accent)
                if p % 2:
                    put_pixel(f, 4, 5, accent)

        frames.append(f)
    return frames


# ---------------------------------------------------------------- Weather Particles
# Dynamic weather particles: precipitation bands, fog drift, sun ring, night moon/stars.

WEATHER_ALIASES: dict[str, str] = {
    "clear": "clear",
    "sunny": "clear",
    "sun": "clear",
    "night": "clear",
    "rain": "rain",
    "rainy": "rain",
    "drizzle": "drizzle",
    "snow": "snow",
    "snowy": "snow",
    "sleet": "sleet",
    "storm": "storm",
    "thunder": "storm",
    "thunderstorm": "storm",
    "lightning": "storm",
    "fog": "fog",
    "mist": "fog",
    "haze": "fog",
    "part": "part",
    "partly": "part",
    "partly_cloudy": "part",
    "partlycloudy": "part",
    "cloud": "cloudy",
    "cloudy": "cloudy",
    "overcast": "overcast",
}


def weather_icon(condition: str = "clear", is_day: bool = True) -> list[Frame]:
    """8x8 dynamic animated weather icon.

    Supports precipitation bands (rain, drizzle, snow, sleet, storm), fog drift,
    clear day pulsing sun, clear night blinking moon/stars, and cloud edge shimmer.
    Every non-black pixel maintains dominant channel >= 174.
    """
    key = condition.lower().strip().replace("-", "_").replace(" ", "_")
    kind = WEATHER_ALIASES.get(key, "clear")

    if weather_icons is not None:
        art_frames, _ = weather_icons.frames(kind, is_day=is_day)
        frames: list[Frame] = []
        for art in art_frames:
            f = new_frame()
            weather_icons.paint(f, art, 0, 0)
            frames.append(f)
        return frames

    # Fallback if weather_icons module is inaccessible
    if kind == "clear":
        return sun_icon() if is_day else [new_frame()]
    elif kind in ("rain", "drizzle", "sleet", "storm"):
        return rain_icon()
    return cloud_icon()


# ---------------------------------------------------------------- AQI Breathing Lungs
# 4-frame breathing lungs: expanding/contracting highlights across 6 EPA categories.

AQI_COLORS: tuple[tuple[int, int, int], ...] = (
    (69, 255, 157),   # Good (0..50): Green
    (255, 226, 72),   # Moderate (51..100): Yellow
    (255, 151, 47),   # USG (101..150): Orange
    (255, 77, 91),    # Unhealthy (151..200): Red
    (202, 100, 255),  # Very Unhealthy (201..300): Purple
    (255, 65, 143),   # Hazardous (301..500): Maroon
)

LEFT_LUNG = frozenset({
    (1, 2), (2, 2), (3, 2),
    (0, 3), (1, 3), (2, 3),
    (0, 4), (1, 4), (2, 4),
    (0, 5), (1, 5), (2, 5),
    (1, 6), (2, 6),
    (2, 7),
})
RIGHT_LUNG = frozenset((7 - x, y) for x, y in LEFT_LUNG)
AIRWAY = frozenset({(3, 0), (4, 0), (3, 1), (4, 1), (2, 2), (3, 2), (4, 2), (5, 2)})

LEFT_HIGHLIGHTS: tuple[frozenset[tuple[int, int]], ...] = (
    frozenset({(2, 2), (2, 3), (2, 4), (1, 4), (1, 5)}),
    frozenset({(2, 2), (1, 3), (2, 3), (1, 4), (2, 4), (1, 5), (2, 5)}),
    frozenset({(1, 2), (2, 2), (1, 3), (2, 3), (0, 4), (1, 4), (2, 4), (0, 5), (1, 5), (1, 6)}),
    frozenset({(2, 2), (1, 3), (2, 3), (1, 4), (2, 4), (1, 5), (2, 5)}),
)


def lungs_icon(
    aqi_or_level: int | float = 0,
    *,
    level: int | None = None,
) -> list[Frame]:
    """8x8 breathing lungs animation for US AQI reporting across 4 frames.

    Accepts either an explicit EPA category index (0..5) or a raw AQI reading (0..500).
    Body lung silhouette uses 58% linear gamma dimming (dominant channel >= 199).
    Internal highlight expands from 5 to 10 pixels and contracts back.
    Airway is anchored in ICE and PEARL.
    """
    if level is not None:
        idx = max(0, min(5, int(level)))
    elif isinstance(aqi_or_level, int) and 0 <= aqi_or_level <= 5:
        idx = aqi_or_level
    else:
        v = float(aqi_or_level)
        if v <= 50:
            idx = 0
        elif v <= 100:
            idx = 1
        elif v <= 150:
            idx = 2
        elif v <= 200:
            idx = 3
        elif v <= 300:
            idx = 4
        else:
            idx = 5

    color = AQI_COLORS[idx]
    # Emitted-light 58% dimming: guaranteed dominant channel >= 199 on all 6 categories
    body = gamma_lerp(BLACK, color, 0.58)

    frames: list[Frame] = []
    for phase in range(4):
        f = new_frame()
        # Draw base anatomical lung body
        for x, y in LEFT_LUNG | RIGHT_LUNG:
            put_pixel(f, x, y, body)
        # Draw breathing highlights in full category color
        hl = LEFT_HIGHLIGHTS[phase]
        for x, y in hl:
            put_pixel(f, x, y, color)
            put_pixel(f, 7 - x, y, color)
        # Draw stable Y-airway anchor
        for x, y in AIRWAY:
            put_pixel(f, x, y, ICE)
        put_pixel(f, 2, 2, PEARL)
        put_pixel(f, 5, 2, PEARL)
        frames.append(f)
    return frames


# ---------------------------------------------------------------- PC Stats Hardware

_PC_CPU_PATTERN = (
    "..P.P...",
    ".CCCCC..",
    "PC...CP.",
    ".C.C.C..",
    "PC...CP.",
    ".CCCCC..",
    "..P.P...",
    "........",
)
_PC_CPU_SPARKS = ((2, 0), (4, 0), (6, 2), (6, 4), (4, 6), (2, 6))

_PC_RAM_PATTERN = (
    "........",
    "BBBBBBBB",
    "BC.C.C.B",
    "BC.C.C.B",
    "BBBBBBBB",
    ".P.P.P.P",
    "........",
    "........",
)
_PC_RAM_SPARKS = ((1, 2), (3, 2), (5, 2), (5, 3), (3, 3), (1, 3))

_PC_GPU_PATTERN = (
    "........",
    "BBBBBBBB",
    "B.FFF..B",
    "B.F.F..B",
    "B.FFF..B",
    "BBBBBBBB",
    ".PPP.PP.",
    "........",
)
_PC_GPU_SPARKS = ((2, 2), (3, 2), (4, 2), (4, 3), (4, 4), (3, 4))

_PC_DIAL_BEZEL = (
    (2, 1), (3, 1), (4, 1), (5, 1),
    (1, 2), (6, 2),
    (1, 5), (6, 5),
    (2, 6), (3, 6), (4, 6), (5, 6),
)
_PC_CPU_TICKS = ((2, 2), (5, 2))
_PC_RAM_TICKS = ((2, 5), (5, 5))
_PC_CPU_NEEDLES = (
    ((2, 2),),
    ((2, 2), (3, 2)),
    ((2, 2), (3, 2), (4, 2)),
    ((3, 2), (4, 2), (5, 2)),
    ((4, 2), (5, 2)),
    ((5, 2),),
)
_PC_RAM_NEEDLES = (
    ((2, 5),),
    ((2, 5), (3, 5)),
    ((2, 5), (3, 5), (4, 5)),
    ((3, 5), (4, 5), (5, 5)),
    ((4, 5), (5, 5)),
    ((5, 5),),
)


def pc_icon(metric: str = "cpu", level: int = 0, other_level: int = 0) -> list[Frame]:
    """8x8 hardware icon for PC telemetry.

    Supports:
    - 'cpu': Microprocessor chip silhouette with GOLD pins, ICE die, and walking spark.
    - 'ram': Memory DIMM stick with VIOLET board, PEARL DRAM chips, GOLD contact fingers.
    - 'gpu': Graphics expansion card with EMERALD PCB, PEARL fan shroud, GOLD PCIe pins.
    - 'net' / 'network': Up/down transfer arrows with traveling PEARL packet.
    - 'both' / 'combined' / 'dial': Combined dial gauge with GOLD bezel, CORAL CPU needle, VIOLET RAM needle.
    """
    m = str(metric).lower()
    frames = []
    spark_col = CORAL if level >= 2 else (AMBER if level == 1 else ICE)

    if m == "cpu":
        colors = {"C": ICE, "P": GOLD}
        for p in range(4):
            f = _build_frame(_PC_CPU_PATTERN, colors)
            sx, sy = _PC_CPU_SPARKS[p % len(_PC_CPU_SPARKS)]
            put_pixel(f, sx, sy, spark_col)
            frames.append(f)
    elif m == "ram":
        colors = {"B": VIOLET, "C": PEARL, "P": GOLD}
        for p in range(4):
            f = _build_frame(_PC_RAM_PATTERN, colors)
            sx, sy = _PC_RAM_SPARKS[p % len(_PC_RAM_SPARKS)]
            put_pixel(f, sx, sy, spark_col)
            frames.append(f)
    elif m == "gpu":
        colors = {"B": EMERALD, "F": PEARL, "P": GOLD}
        for p in range(4):
            f = _build_frame(_PC_GPU_PATTERN, colors)
            sx, sy = _PC_GPU_SPARKS[p % len(_PC_GPU_SPARKS)]
            put_pixel(f, sx, sy, spark_col)
            frames.append(f)
    elif m in ("net", "network"):
        for p in range(4):
            f = new_frame()
            # Up arrow (MINT)
            put_pixel(f, 2, 1, MINT)
            put_pixel(f, 1, 2, MINT); put_pixel(f, 2, 2, MINT); put_pixel(f, 3, 2, MINT)
            for y in range(3, 7):
                put_pixel(f, 2, y, MINT)
            # Down arrow (ICE)
            for y in range(1, 5):
                put_pixel(f, 5, y, ICE)
            put_pixel(f, 4, 5, ICE); put_pixel(f, 5, 5, ICE); put_pixel(f, 6, 5, ICE)
            put_pixel(f, 5, 6, ICE)
            # Traveling pulse
            put_pixel(f, 2, 6 - p, PEARL)
            put_pixel(f, 5, 1 + p, PEARL)
            frames.append(f)
    elif m in ("both", "combined", "dial"):
        cpu_target = min(5, max(0, level + 3))
        ram_target = min(5, max(0, other_level + 3))
        for p in range(4):
            f = new_frame()
            for x, y in _PC_DIAL_BEZEL:
                put_pixel(f, x, y, GOLD)
            for x, y in _PC_CPU_TICKS:
                put_pixel(f, x, y, CORAL)
            for x, y in _PC_RAM_TICKS:
                put_pixel(f, x, y, VIOLET)
            for x, y in ((3, 3), (4, 3), (3, 4), (4, 4)):
                put_pixel(f, x, y, PEARL)
            c_pose = min(cpu_target, p)
            r_pose = min(ram_target, p)
            for x, y in _PC_CPU_NEEDLES[c_pose]:
                put_pixel(f, x, y, CORAL)
            for x, y in _PC_RAM_NEEDLES[r_pose]:
                put_pixel(f, x, y, VIOLET)
            frames.append(f)
    else:
        raise ValueError(f"unsupported pc metric: {metric}")
    return frames


