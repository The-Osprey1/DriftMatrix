"""ISS Watch for AWTRIX -- look up, the space station is going over.

Alert-only.  The page exists only while the International Space Station can
actually be seen from the house: it is above the horizon, it is in sunlight,
and the sky here is dark enough to pick it out.  That happens a few minutes at
a time, in streaks of a few evenings, and never at all for weeks in between.

    icon   the station's silhouette, a glint running down its solar arrays
    lane   where to look: "NW 42" -- compass point and degrees above horizon
    meter  the same elevation on a 0..90 scale, so it grows as it climbs

The position comes from wheretheiss.at (keyless, open, ~1 request/second
allowed), which also says whether the station is sunlit.  Whether the sky is
dark is computed here from the sun's altitude, so outside twilight and night
nothing is fetched at all.  The panel's own blackout suppresses it too: a pass
at 03:00 would be shown to an empty, dark room.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
import math
import time
from typing import Any, Callable

from meridian_preview import solar_altitude
from panel_client import is_quiet_time
from panel_scene import PresenceScene, encode_icon_gif, https_get, lane_payload, run_scene
from quake_watch import compass, haversine_km
from visual_system import BLACK, ICE, PEARL, RGB, Frame, draw_comet, new_frame, put_pixel, render_glance_lane, scale_color

APP_NAME = "iss0"
HOME_LAT = 33.87529
HOME_LON = -117.56644
HOME_ALT_KM = 0.3
EARTH_RADIUS_KM = 6371.0
FEED_URL = "https://api.wheretheiss.at/v1/satellites/25544"
FEED_TIMEOUT_SECONDS = 8

# The sky has to be properly dark: below -6 deg (end of civil twilight) the
# station is the brightest thing up there after the moon and Venus.
DARK_SUN_ALTITUDE = -6.0
# Low passes are lost in haze and rooftops.
MIN_ELEVATION_DEG = 10.0
# At 7.7 km/s the station covers ~460 km a minute.  The 10 deg circle is about
# 1,400 km of ground radius, so burst-polling from 2,500 km out catches every
# pass at least two minutes before it rises.
APPROACH_KM = 2_500.0


@dataclass(frozen=True)
class StationFix:
    lat: float
    lon: float
    alt_km: float
    sunlit: bool


@dataclass(frozen=True)
class LookAngle:
    azimuth_deg: float
    elevation_deg: float


def parse_fix(document: dict[str, Any]) -> StationFix:
    return StationFix(
        lat=float(document["latitude"]),
        lon=float(document["longitude"]),
        alt_km=float(document["altitude"]),
        # "daylight" means the station is in sunlight; "eclipsed" means it is
        # in Earth's shadow and invisible however dark the sky.
        sunlit=document.get("visibility") == "daylight",
    )


def _ecef(lat: float, lon: float, alt_km: float) -> tuple[float, float, float]:
    r = EARTH_RADIUS_KM + alt_km
    p, l = math.radians(lat), math.radians(lon)
    return (r * math.cos(p) * math.cos(l), r * math.cos(p) * math.sin(l), r * math.sin(p))


def look_angle(fix: StationFix, home: tuple[float, float] = (HOME_LAT, HOME_LON)) -> LookAngle:
    """Azimuth (0 = north, clockwise) and elevation of the station from home.

    A spherical Earth is good to well under a degree at these distances, which
    is finer than "look NW, about halfway up" needs.
    """
    ox, oy, oz = _ecef(home[0], home[1], HOME_ALT_KM)
    sx, sy, sz = _ecef(fix.lat, fix.lon, fix.alt_km)
    dx, dy, dz = sx - ox, sy - oy, sz - oz
    p, l = math.radians(home[0]), math.radians(home[1])
    east = -math.sin(l) * dx + math.cos(l) * dy
    north = -math.sin(p) * math.cos(l) * dx - math.sin(p) * math.sin(l) * dy + math.cos(p) * dz
    up = math.cos(p) * math.cos(l) * dx + math.cos(p) * math.sin(l) * dy + math.sin(p) * dz
    distance = math.sqrt(dx * dx + dy * dy + dz * dz)
    return LookAngle(
        azimuth_deg=math.degrees(math.atan2(east, north)) % 360,
        elevation_deg=math.degrees(math.asin(max(-1.0, min(1.0, up / distance)))),
    )


def ground_distance_km(fix: StationFix, home: tuple[float, float] = (HOME_LAT, HOME_LON)) -> float:
    return haversine_km(home[0], home[1], fix.lat, fix.lon)


def sky_is_dark(moment: datetime, home: tuple[float, float] = (HOME_LAT, HOME_LON)) -> bool:
    minute = moment.hour * 60 + moment.minute + moment.second / 60
    return solar_altitude(moment.date(), minute, home[0], home[1]) <= DARK_SUN_ALTITUDE


def is_visible(fix: StationFix, moment: datetime, home: tuple[float, float] = (HOME_LAT, HOME_LON)) -> bool:
    return fix.sunlit and look_angle(fix, home).elevation_deg >= MIN_ELEVATION_DEG and sky_is_dark(moment, home)


def look_label(angle: LookAngle) -> str:
    return f"{compass(angle.azimuth_deg)} {round(angle.elevation_deg)}"


def describe(angle: LookAngle, rising: bool | None) -> str:
    trend = "" if rising is None else (" rising" if rising else " setting")
    return f"ISS {compass(angle.azimuth_deg)} {round(angle.elevation_deg)} deg{trend}"


# ---------------------------------------------------------------- Icon

# The station (2026-10-03, to the Now Playing standard): solar arrays with
# a cell texture in two golds, a pearl truss, ice-blue modules, a glint
# sweeping the panels with a fading trail, and stars twinkling in the empty
# sky around it.  The silhouette survives brightness 1; the faintest star
# levels may not.
STATION = (
    "AA....AA",
    "AA....AA",
    "AA.MM.AA",
    "TTTTTTTT",
    "AA.MM.AA",
    "AA....AA",
    "AA....AA",
    "........",
)
CELL_LIGHT: RGB = (255, 208, 70)
CELL_DARK: RGB = (214, 160, 40)
GLINT_PATH = ((0, 0), (1, 2), (0, 5), (6, 1), (7, 4), (6, 6))
GLINT_REST = 2  # 8 beats in all, under the 1 KB icon budget
STARS = ((3, 0, 0), (4, 6, 4), (3, 7, 2))
TWINKLE = (1.0, 0.55, 0.2, 0.0, 0.0, 0.2, 0.55, 1.0)
STAR: RGB = (210, 220, 255)


def icon_frames() -> list[Frame]:
    frames = []
    for t in range(len(GLINT_PATH) + GLINT_REST):
        frame = new_frame()
        for y, row in enumerate(STATION):
            for x, ch in enumerate(row):
                if ch == "A":
                    put_pixel(frame, x, y, CELL_LIGHT if (x + y) % 2 == 0 else CELL_DARK)
                elif ch == "T":
                    put_pixel(frame, x, y, PEARL)
                elif ch == "M":
                    put_pixel(frame, x, y, ICE)
        base = [frame[y][x] for x, y in GLINT_PATH]
        draw_comet(frame, GLINT_PATH, t, base, (1.0, 0.45))
        for x, y, phase in STARS:
            level = TWINKLE[(t + phase) % len(TWINKLE)]
            if level and frame[y][x] == BLACK:
                put_pixel(frame, x, y, scale_color(STAR, level))
        frames.append(frame)
    return frames


def render_lane(angle: LookAngle) -> Frame:
    return render_glance_lane(look_label(angle), meter=max(0.0, angle.elevation_deg) / 90.0)


def build_payload(angle: LookAngle) -> dict[str, Any]:
    # Lifetime covers two missed burst polls; a pass that ends removes itself.
    return lane_payload(render_lane(angle), encode_icon_gif(icon_frames()),
                        duration_ms=10_000, lifetime_ms=45_000)


# ---------------------------------------------------------------- Controller

@dataclass(frozen=True)
class IssWatchConfig:
    burst_poll_seconds: float = 10.0
    relaxed_poll_seconds: float = 60.0
    # Outside dark hours there is nothing to see; look again this often.
    daylight_check_seconds: float = 300.0
    max_backoff_seconds: float = 600.0
    quiet_start_minute: int = 21 * 60 + 30
    quiet_end_minute: int = 7 * 60
    lat: float = HOME_LAT
    lon: float = HOME_LON


def fetch_fix(http_get: Callable[..., Any] = https_get) -> StationFix:
    response = http_get(FEED_URL, timeout=FEED_TIMEOUT_SECONDS)
    response.raise_for_status()
    return parse_fix(response.json())


def _clock_minute(minute: int) -> Any:
    from datetime import time as wall_time
    return wall_time(minute // 60, minute % 60)


class IssWatchController:
    def __init__(self, panel: Any, config: IssWatchConfig | None = None, *,
                 fetcher: Callable[[], StationFix] | None = None,
                 clock: Any = time, wall_now: Callable[[], datetime] | None = None,
                 logger: logging.Logger | None = None) -> None:
        self.panel = panel
        self.config = config or IssWatchConfig()
        self.fetcher = fetcher or fetch_fix
        self.clock = clock
        self.wall_now = wall_now or datetime.now
        self.logger = logger or logging.getLogger("awtrix.iss_watch")
        self.scene = PresenceScene(panel, APP_NAME, self.config.burst_poll_seconds, clock, self.logger)
        self.next_poll_at = 0.0
        self.backoff_seconds = 0.0
        self.last_elevation: float | None = None
        self.pass_announced = False

    @property
    def home(self) -> tuple[float, float]:
        return (self.config.lat, self.config.lon)

    def _end_pass(self) -> None:
        self.scene.hide()
        self.last_elevation = None
        self.pass_announced = False

    def _announce(self) -> None:
        # Once per pass: a pass lasts a few minutes, so waiting for the loop to
        # come round could miss half of it.  The shared client applies quiet
        # hours and the global activation cooldown.
        if self.pass_announced:
            return
        self.pass_announced = True
        activate = getattr(self.panel, "activate_app", None)
        if activate is None:
            return
        try:
            activate(APP_NAME)
        except Exception as err:  # noqa: BLE001
            self.logger.warning("Could not bring %s forward: %s", APP_NAME, err)

    def tick(self) -> LookAngle | None:
        now = self.clock.monotonic()
        if now < self.next_poll_at:
            return None
        moment = self.wall_now()
        quiet = is_quiet_time(moment, _clock_minute(self.config.quiet_start_minute),
                              _clock_minute(self.config.quiet_end_minute))
        if quiet or not sky_is_dark(moment, self.home):
            self._end_pass()
            self.next_poll_at = now + self.config.daylight_check_seconds
            return None

        try:
            fix = self.fetcher()
        except Exception as err:  # noqa: BLE001
            self.backoff_seconds = min(self.config.max_backoff_seconds,
                                       max(self.config.relaxed_poll_seconds, self.backoff_seconds * 2))
            self.next_poll_at = now + self.backoff_seconds
            self.logger.warning("ISS position feed failed (%s); retrying in %.0f s", err, self.backoff_seconds)
            return None
        self.backoff_seconds = 0.0

        near = ground_distance_km(fix, self.home) <= APPROACH_KM
        self.next_poll_at = now + (self.config.burst_poll_seconds if near else self.config.relaxed_poll_seconds)
        if not is_visible(fix, moment, self.home):
            self._end_pass()
            return None

        angle = look_angle(fix, self.home)
        rising = None if self.last_elevation is None else angle.elevation_deg >= self.last_elevation
        self.last_elevation = angle.elevation_deg
        # The key changes whenever the label does, so the page follows the
        # station across the sky rather than waiting for the refresh interval.
        if self.scene.show((look_label(angle), describe(angle, rising)), lambda _rev: build_payload(angle)):
            self._announce()
        return angle


if __name__ == "__main__":
    run_scene("ISS Watch: visible space station passes", lambda p: IssWatchController(p),
              IssWatchConfig().burst_poll_seconds)
