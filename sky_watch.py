"""Sky Watch for AWTRIX -- what is that plane flying over the house?

Alert-only.  The page exists only while an airborne aircraft is inside a small
circle around home and low enough to see; the rest of the time it is absent.

    icon   the aircraft's silhouette pointing where it is heading, beacon blinking
    text   what it is, as a word that fits without scrolling: 737, A320, CESSNA

Positions come from adsb.lol's free, keyless community ADS-B feed over plain
HTTPS from this PC, with adsb.fi's open data feed as the fallback.  Nothing about the house is sent except the circle centre,
which is the same lat/lon the weather senders already use.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
import time
from typing import Any, Callable, Iterable

from panel_scene import (
    PresenceScene,
    encode_icon_gif,
    https_get,
    lane_payload,
    pattern_frame,
    run_scene,
)
from visual_system import (
    GOLD, ICE, LANE_WIDTH, LANE_X0, MINT, PEARL, RED_HOT, RGB, Frame,
    gamma_lerp, measure_text, new_frame, render_glance_lane, scale_color,
)

APP_NAME = "planes0"
HOME_LAT = 33.87529
HOME_LON = -117.56644
PRIMARY_FEED_URL = "https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{nm}"
# airplanes.live was the secondary, but it has answered 403 with a request to
# email them for access since at least 2026-10-02. adsb.fi serves the same readsb
# "ac" document, keyless, from the same lat/lon/dist path shape.
SECONDARY_FEED_URL = "https://opendata.adsb.fi/api/v3/lat/{lat}/lon/{lon}/dist/{nm}"
FEED_URL = PRIMARY_FEED_URL
FEED_TIMEOUT_SECONDS = 8
DEFAULT_MAX_ALTITUDE_FT = 12_000
DEFAULT_BURST_POLL_SECONDS = 20.0
DEFAULT_RELAXED_POLL_SECONDS = 60.0
DEFAULT_PRIMARY_BACKOFF_SECONDS = 60.0

# Airline ICAO prefix -> the name people actually say.  Unknown prefixes fall
# back to the raw callsign, which is still better than nothing.
AIRLINES = {
    "AAL": "AMERICAN", "AAY": "ALLEGIANT", "ACA": "AIR CANADA", "AMX": "AEROMEXICO",
    "ASA": "ALASKA", "CPZ": "COMPASS", "DAL": "DELTA", "ENY": "ENVOY", "FDX": "FEDEX",
    "FFT": "FRONTIER", "GTI": "ATLAS", "HAL": "HAWAIIAN", "JBU": "JETBLUE",
    "MXY": "BREEZE", "NKS": "SPIRIT", "QXE": "HORIZON", "RPA": "REPUBLIC",
    "SKW": "SKYWEST", "SWA": "SOUTHWEST", "UAL": "UNITED", "UPS": "UPS",
    "VOI": "VOLARIS", "VXP": "AVELO", "WJA": "WESTJET", "ABX": "ABX", "SCX": "SUN COUNTRY",
}

# ICAO type designator prefix -> the model name people say.  Every label must
# fit the 23px lane with no scrolling (test_every_label_fits_the_lane); the
# first matching prefix wins, so longer prefixes come first where they overlap.
MODEL_PREFIXES = (
    ("B37M", "737"), ("B38M", "737"), ("B39M", "737"), ("B3XM", "737"), ("B73", "737"),
    ("B74", "747"), ("B75", "757"), ("B76", "767"), ("B77", "777"), ("B78", "787"),
    ("BCS", "A220"), ("A318", "A318"), ("A319", "A319"), ("A19N", "A319"),
    ("A320", "A320"), ("A20N", "A320"), ("A321", "A321"), ("A21N", "A321"),
    ("A33", "A330"), ("A34", "A340"), ("A35", "A350"), ("A38", "A380"),
    ("E17", "E175"), ("E75", "E175"), ("E19", "E190"), ("E29", "E190"), ("CRJ", "CRJ"),
    ("DH8", "DASH8"), ("AT7", "ATR"), ("AT4", "ATR"), ("MD8", "MD80"), ("C130", "C130"),
    ("C1", "CESSNA"), ("C2", "CESSNA"), ("PA", "PIPER"), ("P28", "PIPER"), ("P32", "PIPER"),
    ("SR2", "CIRRUS"), ("DA4", "DA40"), ("BE", "BEECH"), ("M20", "MOONY"),
    ("GLF", "JET"), ("GL", "JET"), ("CL", "JET"), ("C68", "JET"), ("C56", "JET"),
    ("E55", "JET"), ("LJ", "JET"), ("F2T", "JET"), ("FA", "JET"), ("H25", "JET"),
    ("PC12", "PC12"),
)


@dataclass(frozen=True)
class Aircraft:
    hex_id: str
    callsign: str
    type_code: str
    category: str
    altitude_ft: int
    track_deg: float | None
    distance_nm: float
    bearing_deg: float | None


def parse_aircraft(document: dict[str, Any]) -> list[Aircraft]:
    """Airborne aircraft with a known altitude and distance; ground traffic dropped."""
    found = []
    for ac in document.get("ac", []) or []:
        alt = ac.get("alt_baro")
        dist = ac.get("dst")
        if not isinstance(alt, (int, float)) or not isinstance(dist, (int, float)):
            continue  # "ground", or no position
        found.append(Aircraft(
            hex_id=str(ac.get("hex", "")),
            callsign=str(ac.get("flight") or ac.get("r") or "").strip().upper(),
            type_code=str(ac.get("t") or "").strip().upper(),
            category=str(ac.get("category") or ""),
            altitude_ft=int(alt),
            track_deg=float(ac["track"]) if isinstance(ac.get("track"), (int, float)) else None,
            distance_nm=float(dist),
            bearing_deg=float(ac["dir"]) if isinstance(ac.get("dir"), (int, float)) else None,
        ))
    return found


def pick_overhead(aircraft: Iterable[Aircraft], radius_nm: float, max_altitude_ft: int) -> Aircraft | None:
    near = [a for a in aircraft
            if a.distance_nm <= radius_nm and 0 < a.altitude_ft <= max_altitude_ft]
    return min(near, key=lambda a: a.distance_nm, default=None)


def operator_name(callsign: str) -> str:
    """"SWA1234" -> "SOUTHWEST"; N-numbers and unknown prefixes stay as they are."""
    if len(callsign) >= 4 and callsign[:3].isalpha() and callsign[3].isdigit():
        return AIRLINES.get(callsign[:3], callsign)
    return callsign


def is_rotorcraft(a: Aircraft) -> bool:
    return a.category == "A7"


def is_light(a: Aircraft) -> bool:
    return a.category in ("A1", "B1", "B4")  # light aircraft, glider, ultralight


def model_label(a: Aircraft) -> str:
    if is_rotorcraft(a):
        return "HELI"
    for prefix, label in MODEL_PREFIXES:
        if a.type_code.startswith(prefix):
            return label
    if is_light(a):
        return "SMALL"
    if a.type_code and measure_text(a.type_code) <= LANE_WIDTH:
        return a.type_code
    return "PLANE"


def describe(a: Aircraft) -> str:
    """For the log: the full story the panel no longer tries to tell."""
    who = operator_name(a.callsign) or "UNKNOWN"
    return f"{who} {a.type_code or '?'} {a.altitude_ft:,} FT {a.distance_nm:.1f} NM"


def body_color(a: Aircraft) -> RGB:
    if is_rotorcraft(a):
        return GOLD
    if is_light(a):
        return MINT
    return ICE


# ---------------------------------------------------------------- Icon

# One silhouette per kind of aircraft, each drawn nose-up (N) and nose to the
# upper right (NE); the other six headings are quarter turns of those two.
# "B" is the body, "L" the tail beacon that blinks.  Drawn in the 7x7 box at
# the top left so a quarter turn keeps row and column 7 empty.
#
# The diagonals are drawn by hand, not rotated: a 45-degree turn of a 7px
# sprite smears into a blob, which is what the first NE airliner was.  Each
# diagonal keeps a one-pixel fuselage on the true diagonal and runs the wings
# along the grid, so the shape stays crisp.
SILHOUETTES = {
    # Swept wings with a visible sweep, wide tailplane.
    "airliner": ((
        "...B...",
        "...B...",
        "..BBB..",
        ".BBBBB.",
        "BB.B.BB",
        "...B...",
        "..BLB..",
    ), (
        # Diagonals mirror exactly across the fuselage (x, y) <-> (6-y, 6-x):
        # at 7px that symmetry, not geometric accuracy, is what reads as a
        # plane.  The 2026-10-03 redraw replaced a one-pixel "7" shape.
        "......B",
        "....BB.",
        "..BBBB.",
        "BB.BB..",
        "B.B.B..",
        ".B.B...",
        "L.BB...",
    )),
    # Straight wing right behind the nose, long thin tail: a Cessna or Piper.
    "light": ((
        "...B...",
        "BBBBBBB",
        "...B...",
        "...B...",
        "...B...",
        ".BBLBB.",
        ".......",
    ), (
        "..B....",
        "...B.B.",
        "....B..",
        "...B.B.",
        "B.B...B",
        ".L.....",
        "..B....",
    )),
    # Small swept wing set far back, like a dart: a business jet.
    "jet": ((
        "...B...",
        "...B...",
        "...B...",
        "..BBB..",
        ".BBBBB.",
        "...B...",
        "..BLB..",
    ), (
        "......B",
        ".....B.",
        "....B..",
        ".BBB...",
        "..BB...",
        "BB.B...",
        "LB.....",
    )),
}
# Side view with the rotor on top; a helicopter's heading matters less than
# telling it apart from a plane at a glance, so it does not rotate.
HELI = (
    "BBBBBBB.",
    "...B....",
    ".BBBB...",
    "BBBBBBBL",
    ".BBBB...",
    "..B.B...",
    ".BBBBB..",
    "........",
)
LIGHT_LABELS = {"CESSNA", "PIPER", "CIRRUS", "DA40", "BEECH", "MOONY", "SMALL"}


def plane_class(a: Aircraft) -> str:
    if is_rotorcraft(a):
        return "heli"
    label = model_label(a)
    if label == "JET":
        return "jet"
    if label in LIGHT_LABELS or is_light(a):
        return "light"
    return "airliner"


def _rotate(pattern: tuple[str, ...], quarter_turns: int) -> tuple[str, ...]:
    """Rotate clockwise inside the 7x7 box at the top-left, keeping row/col 7 empty."""
    grid = [row[:7].ljust(7, ".") for row in pattern[:7]]
    for _ in range(quarter_turns % 4):
        grid = ["".join(grid[6 - c][r] for c in range(7)) for r in range(7)]
    return tuple(row + "." for row in grid) + ("........",)


def heading_pattern(track_deg: float | None, kind: str = "airliner") -> tuple[str, ...]:
    octant = 0 if track_deg is None else int(((track_deg % 360) + 22.5) // 45) % 8
    north, northeast = SILHOUETTES[kind]
    return _rotate(north if octant % 2 == 0 else northeast, octant // 2)


# Real aircraft lighting (2026-10-03, to the Now Playing standard): a red
# light on the left wingtip, a green one on the right, a white strobe that
# double-flashes at the tail, and a brighter spine along the fuselage for
# shading.  The lights also say which way the plane faces.  A helicopter has
# no wingtips; its rotor flickers like a spinning disc instead.
NAV_PORT: RGB = (255, 52, 60)
NAV_STARBOARD: RGB = (60, 255, 130)
STROBE: RGB = (255, 255, 255)
STROBE_BEATS = (0, 2)  # flash, dark, flash, then dark for the rest of the cycle
ICON_BEATS = 8
OCTANT_VECTORS = {0: (0, -1), 1: (1, -1), 2: (1, 0), 3: (1, 1), 4: (0, 1), 5: (-1, 1), 6: (-1, 0), 7: (-1, -1)}


def _octant(track_deg: float | None) -> int:
    return 0 if track_deg is None else int(((track_deg % 360) + 22.5) // 45) % 8


def wingtips(pattern: tuple[str, ...], track_deg: float | None) -> tuple[tuple[int, int], tuple[int, int]]:
    """(port, starboard): the body pixels furthest left and right of the heading."""
    hx, hy = OCTANT_VECTORS[_octant(track_deg)]
    norm = math.hypot(hx, hy)
    hx, hy = hx / norm, hy / norm
    lx, ly = hy, -hx  # left of the heading, in screen coordinates (y grows down)
    cells = [(x, y) for y, row in enumerate(pattern) for x, ch in enumerate(row) if ch in "BL"]
    lateral = {p: (p[0] - 3) * lx + (p[1] - 3) * ly for p in cells}
    along = {p: (p[0] - 3) * hx + (p[1] - 3) * hy for p in cells}
    port = max(cells, key=lambda p: (round(lateral[p], 2), along[p]))
    starboard = min(cells, key=lambda p: (round(lateral[p], 2), -along[p]))
    return port, starboard


def icon_frames(a: Aircraft) -> list[Frame]:
    kind = plane_class(a)
    pattern = HELI if kind == "heli" else heading_pattern(a.track_deg, kind)
    body = body_color(a)
    hx, hy = OCTANT_VECTORS[_octant(a.track_deg)]
    norm = math.hypot(hx, hy)
    lx, ly = hy / norm, -hx / norm
    cells = [(x, y) for y, row in enumerate(pattern) for x, ch in enumerate(row) if ch == "B"]
    beacon = [(x, y) for y, row in enumerate(pattern) for x, ch in enumerate(row) if ch == "L"]
    spine_colour = gamma_lerp(body, PEARL, 0.45)
    port, starboard = wingtips(pattern, a.track_deg)
    frames = []
    for beat in range(ICON_BEATS):
        frame = new_frame()
        for x, y in cells:
            on_spine = kind != "heli" and abs((x - 3) * lx + (y - 3) * ly) < 0.75
            frame[y][x] = spine_colour if on_spine else body
        if kind == "heli":
            if beat % 2:
                for x in range(4, 7):
                    if pattern[0][x] == "B":
                        frame[0][x] = scale_color(body, 0.35)
        else:
            frame[port[1]][port[0]] = NAV_PORT
            frame[starboard[1]][starboard[0]] = NAV_STARBOARD
        for x, y in beacon:
            frame[y][x] = STROBE if beat in STROBE_BEATS else scale_color(NAV_PORT, 0.55)
        frames.append(frame)
    return frames


def render_lane(a: Aircraft, max_altitude_ft: int = DEFAULT_MAX_ALTITUDE_FT) -> Frame:
    # Calm glance: the icon says it is a plane and which way it is going; the
    # lane says what it is.  The meter is its altitude against the watch
    # ceiling -- a short stub is low and loud, a long bar is passing high.
    return render_glance_lane(model_label(a), meter=a.altitude_ft / max_altitude_ft)


def build_payload(a: Aircraft, max_altitude_ft: int = DEFAULT_MAX_ALTITUDE_FT) -> dict[str, Any]:
    return lane_payload(render_lane(a, max_altitude_ft), encode_icon_gif(icon_frames(a)),
                        duration_ms=8_000, lifetime_ms=60_000)


# ---------------------------------------------------------------- Controller

@dataclass(frozen=True)
class SkyWatchConfig:
    radius_nm: float = 2.5
    max_altitude_ft: int = DEFAULT_MAX_ALTITUDE_FT
    # adsb.lol answered 429 Too Many Requests at a 10 s poll.
    poll_seconds: float = DEFAULT_BURST_POLL_SECONDS
    burst_poll_seconds: float = DEFAULT_BURST_POLL_SECONDS
    relaxed_poll_seconds: float = DEFAULT_RELAXED_POLL_SECONDS
    # Corona Municipal is ~2 nm away, so small planes in its pattern are
    # overhead almost continuously; off by default so the page stays rare.
    include_light_aircraft: bool = False
    max_backoff_seconds: float = 300.0
    primary_feed_url: str = PRIMARY_FEED_URL
    secondary_feed_url: str = SECONDARY_FEED_URL
    primary_backoff_seconds: float = DEFAULT_PRIMARY_BACKOFF_SECONDS
    lat: float = HOME_LAT
    lon: float = HOME_LON

    def __post_init__(self) -> None:
        if self.poll_seconds != DEFAULT_BURST_POLL_SECONDS and self.burst_poll_seconds == DEFAULT_BURST_POLL_SECONDS:
            object.__setattr__(self, "burst_poll_seconds", self.poll_seconds)
        elif self.burst_poll_seconds != DEFAULT_BURST_POLL_SECONDS and self.poll_seconds == DEFAULT_BURST_POLL_SECONDS:
            object.__setattr__(self, "poll_seconds", self.burst_poll_seconds)


def fetch_feed(
    config: SkyWatchConfig,
    url_template: str | None = None,
    http_get: Callable[..., Any] = https_get,
) -> list[Aircraft]:
    # Ask for a little more than the circle so the nearest plane is never cut off.
    template = url_template or config.primary_feed_url
    url = template.format(lat=config.lat, lon=config.lon, nm=max(1, math.ceil(config.radius_nm + 1)))
    response = http_get(url, timeout=FEED_TIMEOUT_SECONDS)
    response.raise_for_status()
    return parse_aircraft(response.json())


class SkyWatchController:
    def __init__(
        self,
        panel: Any,
        config: SkyWatchConfig | None = None,
        *,
        fetcher: Callable[[SkyWatchConfig], list[Aircraft]] | None = None,
        primary_fetcher: Callable[[SkyWatchConfig], list[Aircraft]] | None = None,
        secondary_fetcher: Callable[[SkyWatchConfig], list[Aircraft]] | None = None,
        clock: Any = time,
        logger: logging.Logger | None = None,
        http_get: Callable[..., Any] = https_get,
    ) -> None:
        self.config = config or SkyWatchConfig()
        self.clock = clock
        self.logger = logger or logging.getLogger("awtrix.sky_watch")
        self.http_get = http_get

        if fetcher is not None:
            self.primary_fetcher = fetcher
            self.secondary_fetcher = secondary_fetcher
        else:
            self.primary_fetcher = primary_fetcher or (
                lambda cfg: fetch_feed(cfg, cfg.primary_feed_url, http_get=self.http_get)
            )
            self.secondary_fetcher = secondary_fetcher or (
                lambda cfg: fetch_feed(cfg, cfg.secondary_feed_url, http_get=self.http_get)
            )
        self.fetcher = self.primary_fetcher

        self.backoff_seconds = 0.0
        self.resume_at = 0.0
        self.primary_backoff_seconds = 0.0
        self.primary_resume_at = 0.0
        self.active_source = "primary"
        # Refresh every burst poll: the rail and heading move as the plane does.
        self.scene = PresenceScene(panel, APP_NAME, self.config.burst_poll_seconds, clock, self.logger)

    def _fetch_with_failover(self) -> list[Aircraft]:
        now = self.clock.monotonic()
        if self.secondary_fetcher is not None and now < self.primary_resume_at:
            return self.secondary_fetcher(self.config)

        try:
            aircraft = self.primary_fetcher(self.config)
            if self.active_source != "primary":
                self.logger.info("Primary ADS-B feed restored; resuming primary")
                self.active_source = "primary"
            self.primary_backoff_seconds = 0.0
            self.primary_resume_at = 0.0
            return aircraft
        except Exception as primary_err:  # noqa: BLE001
            if self.secondary_fetcher is None:
                raise primary_err
            self.primary_backoff_seconds = min(
                self.config.max_backoff_seconds,
                max(self.config.primary_backoff_seconds, self.primary_backoff_seconds * 2),
            )
            self.primary_resume_at = now + self.primary_backoff_seconds
            if self.active_source != "secondary":
                self.logger.info(
                    "Primary ADS-B feed failed (%s); switching to secondary feed for %.0f s",
                    primary_err, self.primary_backoff_seconds,
                )
                self.active_source = "secondary"

            try:
                return self.secondary_fetcher(self.config)
            except Exception as secondary_err:
                raise RuntimeError(
                    f"All ADS-B feeds failed: primary ({primary_err}), secondary ({secondary_err})"
                ) from secondary_err

    def tick(self) -> Aircraft | None:
        now = self.clock.monotonic()
        if now < self.resume_at:
            return None
        try:
            aircraft = self._fetch_with_failover()
        except Exception as err:  # noqa: BLE001
            # Back off, doubling to a ceiling: a community feed that says 429
            # is asking us to slow down.  Leave any current page alone; its
            # 60 s lifetime clears it if the feed stays down.
            self.backoff_seconds = min(
                self.config.max_backoff_seconds,
                max(self.config.burst_poll_seconds, self.backoff_seconds * 2),
            )
            self.resume_at = now + self.backoff_seconds
            self.logger.warning("ADS-B feed failed (%s); retrying in %.0f s", err, self.backoff_seconds)
            return None

        self.backoff_seconds = 0.0

        if not self.config.include_light_aircraft:
            aircraft = [a for a in aircraft if plane_class(a) != "light"]
        plane = pick_overhead(aircraft, self.config.radius_nm, self.config.max_altitude_ft)
        if plane is None:
            self.scene.hide()
            self.resume_at = now + self.config.relaxed_poll_seconds
            return None

        self.resume_at = now + self.config.burst_poll_seconds
        # Refresh interval equals the poll, so the heading updates every poll.
        self.scene.show((plane.hex_id, describe(plane)), lambda _rev: build_payload(plane, self.config.max_altitude_ft))
        return plane


if __name__ == "__main__":
    run_scene("Sky Watch: planes overhead", lambda p: SkyWatchController(p), SkyWatchConfig().burst_poll_seconds)
