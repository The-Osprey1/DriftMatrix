"""Quake Watch for AWTRIX -- was that an earthquake?

Alert-only.  When the USGS reports a quake within range, the page appears for a
window that grows with magnitude, then removes itself.  Most days it never
appears at all.

    icon   magnitude-colored expanding rings
    lane   magnitude and distance in two held rows; one proximity rail

Distance is measured from the configured origin. The full description (place,
direction and age) goes to the log. A still fallback preserves both readings.

Data is the USGS FDSN event service over HTTPS -- public, keyless, and the
same feed the USGS "Latest Earthquakes" map uses.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
import time
from typing import Any, Callable, Iterable

from panel_scene import PresenceScene, encode_icon_gif, https_get, lane_payload, run_scene
from visual_system import (
    LANE_WIDTH, LEVEL_COLORS, PEARL, RGB, Frame,
    gamma_lerp, measure_text, measure_text_3x3, new_frame, put_pixel,
    render_split_lane, draw_level_rail, MIST, scale_color,
)

APP_NAME = "quake0"
HOME_LAT = 33.87529
HOME_LON = -117.56644
DEFAULT_RADIUS_KM = 150.0
FEED_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
FEED_TIMEOUT_SECONDS = 10
KM_PER_MILE = 1.609344
COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


@dataclass(frozen=True)
class Quake:
    event_id: str
    magnitude: float
    place: str
    time_ms: int
    lat: float
    lon: float
    depth_km: float


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(max(0.0, min(1.0, a))))


def validate_home(home):
    try:
        lat, lon = home
        if (any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                for v in (lat, lon)) or not -90 <= lat <= 90 or not -180 <= lon <= 180):
            raise ValueError()
        return float(lat), float(lon)
    except (TypeError, ValueError):
        raise ValueError('Earthquake origin requires finite latitude/longitude in range') from None


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(x, y)) % 360


def compass(deg: float) -> str:
    return COMPASS[int(((deg % 360) + 22.5) // 45) % 8]


def parse_quakes(document: dict[str, Any]) -> list[Quake]:
    if not isinstance(document, dict) or not isinstance(document.get('features'), list):
        raise ValueError('Earthquake feed must contain a features list')
    quakes = []
    for feature in document['features']:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties") or {}
        geometry = feature.get('geometry') or {}
        if not isinstance(props, dict) or not isinstance(geometry, dict):
            continue
        coords = geometry.get('coordinates') or []
        if props.get("type", "earthquake") != "earthquake":
            continue  # quarry blasts and explosions are in the same feed
        if not isinstance(coords, (list, tuple)) or len(coords) < 2:
            continue
        try:
            magnitude, stamp = props.get('mag'), props.get('time')
            depth = coords[2] if len(coords) > 2 and coords[2] is not None else 0
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                   for v in (magnitude, stamp, depth)) or stamp <= 0:
                continue
            lat, lon = validate_home((coords[1], coords[0]))
            identifier = feature.get('id')
            if not isinstance(identifier, str) or not identifier.strip():
                continue
        except (ValueError, TypeError, OverflowError):
            continue
        quakes.append(Quake(
            event_id=identifier,
            magnitude=float(magnitude),
            place=str(props.get("place") or ""),
            time_ms=int(stamp), lon=lon, lat=lat, depth_km=float(depth),
        ))
    return quakes


def window_minutes(magnitude: float) -> int:
    """How long a quake stays on the panel: a tremor for half an hour, a big one all day."""
    if magnitude < 3.0:
        return 30
    if magnitude < 4.0:
        return 120
    if magnitude < 5.0:
        return 360
    return 720


def magnitude_level(magnitude: float) -> str:
    if magnitude < 3.0:
        return "good"
    if magnitude < 4.0:
        return "fair"
    if magnitude < 5.0:
        return "poor"
    return "bad"


def magnitude_color(magnitude: float) -> RGB:
    return LEVEL_COLORS[magnitude_level(magnitude)]


def active_quake(quakes: Iterable[Quake], now_ms: int) -> Quake | None:
    """The biggest quake still inside its window; the newest breaks a tie."""
    live = [q for q in quakes if 0 <= now_ms - q.time_ms < window_minutes(q.magnitude) * 60_000]
    return max(live, key=lambda q: (q.magnitude, q.time_ms), default=None)


def near_place(place: str) -> str:
    """"22 km ESE of Julian, CA" -> "JULIAN"; offshore and odd names pass through."""
    tail = place.split(" of ", 1)[1] if " of " in place else place
    return tail.split(",")[0].strip().upper()


def ago(minutes: int) -> str:
    if minutes < 1:
        return "JUST NOW"
    if minutes < 60:
        return f"{minutes} MIN AGO"
    hours = minutes // 60
    return f"{hours} HR AGO" if hours == 1 else f"{hours} HRS AGO"


def describe(q: Quake, now_ms: int, home: tuple[float, float] = (HOME_LAT, HOME_LON)) -> str:
    miles = haversine_km(home[0], home[1], q.lat, q.lon) / KM_PER_MILE
    direction = compass(bearing_deg(home[0], home[1], q.lat, q.lon))
    parts = [f"M{q.magnitude:.1f}", f"{round(miles)} MI {direction}"]
    place = near_place(q.place)
    if place:
        parts.append(f"NEAR {place}")
    parts.append(ago(max(0, (now_ms - q.time_ms) // 60_000)))
    return "  ".join(parts)


def distance_miles(q: Quake, home: tuple[float, float] = (HOME_LAT, HOME_LON)) -> int:
    return round(haversine_km(home[0], home[1], q.lat, q.lon) / KM_PER_MILE)


def distance_label(q: Quake, home: tuple[float, float] = (HOME_LAT, HOME_LON)) -> str:
    miles = distance_miles(q, home)
    label = f"{miles} MI"
    # Three-digit distances only happen with a radius beyond ~160 km; drop the
    # space rather than scroll or clip.
    for candidate in (label, f'{miles}MI'):
        if max(measure_text(candidate), measure_text_3x3(candidate)) <= LANE_WIDTH:
            return candidate
    thousands = miles / 1000
    return f'{thousands:.1f}KMI' if thousands < 10 else f'{round(thousands)}KMI'


# ---------------------------------------------------------------- Icon

# Shockwave rings (2026-10-03, to the Now Playing standard): a bright
# epicentre with rings rippling smoothly outward and fading as they spread,
# like a stone dropped in water.  Colour is the magnitude; a quake of M3 or
# more sends a second ring half a cycle behind the first.  It replaced a
# scrolling seismograph trace that read thin and twitchy.
RING_FRAMES = 8
RING_REACH = 4.6
RING_WIDTH = 0.75
EPICENTRE = ((3, 3), (4, 3), (3, 4), (4, 4))


def ring_count(magnitude: float) -> int:
    return 1 if magnitude < 3.0 else 2


def magnitude_text(magnitude: float) -> str:
    if isinstance(magnitude, bool) or not math.isfinite(magnitude):
        raise ValueError('Magnitude must be finite')
    label = f'M{magnitude:.1f}'
    return label if measure_text_3x3(label) <= LANE_WIDTH else 'M?'


def icon_frames(magnitude: float) -> list[Frame]:
    color = magnitude_color(magnitude)
    offsets = (0.0, 0.5)[:ring_count(magnitude)]
    frames = []
    for t in range(RING_FRAMES):
        frame = new_frame()
        for y in range(8):
            for x in range(8):
                d = math.hypot(x - 3.5, y - 3.5)
                strength = 0.0
                for offset in offsets:
                    r = ((t / RING_FRAMES + offset) % 1.0) * RING_REACH + 0.8
                    fade = 1.0 - (r - 0.8) / RING_REACH * 0.75
                    strength = max(strength, max(0.0, 1.0 - abs(d - r) / RING_WIDTH) * fade)
                if strength > 0.2:
                    put_pixel(frame, x, y, scale_color(color, 0.6 + 0.4 * strength))
        for x, y in EPICENTRE:
            put_pixel(frame, x, y, gamma_lerp(color, PEARL, 0.55))
        frames.append(frame)
    return frames


def closeness(q: Quake, radius_km: float = DEFAULT_RADIUS_KM,
              home: tuple[float, float] = (HOME_LAT, HOME_LON)) -> float:
    """1.0 at home, 0.0 at the edge of the watched circle."""
    if isinstance(radius_km, bool) or not math.isfinite(radius_km) or radius_km <= 0:
        raise ValueError('Earthquake radius must be finite and positive')
    home = validate_home(home)
    km = haversine_km(home[0], home[1], q.lat, q.lon)
    return max(0.0, min(1.0, 1.0 - km / radius_km))


def render_lane(q: Quake, radius_km: float = DEFAULT_RADIUS_KM, *,
                home: tuple[float, float] = (HOME_LAT, HOME_LON)) -> Frame:
    c = closeness(q, radius_km, home)
    frame = render_split_lane(magnitude_text(q.magnitude), distance_label(q, home), bottom_color=MIST)
    # Row 6 belongs to distance typography; only row 7 carries proximity.
    draw_level_rail(frame, c, magnitude_level(q.magnitude))
    return frame


def build_payload(q: Quake, radius_km: float = DEFAULT_RADIUS_KM, *,
                  home: tuple[float, float] = (HOME_LAT, HOME_LON), animated: bool = True) -> dict[str, Any]:
    frames = icon_frames(q.magnitude)
    if not animated:
        frames = [frames[3]]
    return lane_payload(render_lane(q, radius_km, home=home), encode_icon_gif(frames),
                        duration_ms=8_000, lifetime_ms=180_000)


# ---------------------------------------------------------------- Controller

@dataclass(frozen=True)
class QuakeWatchConfig:
    radius_km: float = DEFAULT_RADIUS_KM
    min_magnitude: float = 2.5
    poll_seconds: float = 60.0
    lat: float = HOME_LAT
    lon: float = HOME_LON

    def __post_init__(self):
        validate_home((self.lat, self.lon))
        for name in ('radius_km', 'poll_seconds'):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(name + ' must be finite and positive')


def fetch_feed(config: QuakeWatchConfig) -> list[Quake]:
    # Look back as far as the longest window so a restart re-finds a big one.
    start = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 12 * 3600))
    response = https_get(FEED_URL, timeout=FEED_TIMEOUT_SECONDS, params={
        "format": "geojson", "latitude": config.lat, "longitude": config.lon,
        "maxradiuskm": config.radius_km, "minmagnitude": config.min_magnitude,
        "starttime": start, "orderby": "time",
    })
    response.raise_for_status()
    return parse_quakes(response.json())


class QuakeWatchController:
    def __init__(self, panel: Any, config: QuakeWatchConfig | None = None, *,
                 fetcher: Callable[[QuakeWatchConfig], list[Quake]] | None = None,
                 clock: Any = time, logger: logging.Logger | None = None) -> None:
        self.config = config or QuakeWatchConfig()
        self.fetcher = fetcher or fetch_feed
        self.clock = clock
        self.logger = logger or logging.getLogger("awtrix.quake_watch")
        self.scene = PresenceScene(panel, APP_NAME, self.config.poll_seconds, clock, self.logger)
        self.next_poll_at = 0.0

    def tick(self) -> Quake | None:
        # The hub ticks every few seconds; the USGS feed is polled once a minute.
        now = self.clock.monotonic()
        if now < self.next_poll_at:
            return None
        self.next_poll_at = now + self.config.poll_seconds
        try:
            quakes = self.fetcher(self.config)
        except Exception as err:  # noqa: BLE001
            self.logger.warning("USGS feed failed: %s", err)
            return None
        now_ms = int(self.clock.time() * 1000)
        quake = active_quake(quakes, now_ms)
        if quake is None:
            self.scene.hide()
            return None
        home = (self.config.lat, self.config.lon)
        self.scene.show((quake.event_id, describe(quake, now_ms, home)),
                        lambda _rev: build_payload(quake, self.config.radius_km, home=home),
                        static_build=lambda _rev: build_payload(quake, self.config.radius_km,
                                                               home=home, animated=False))
        return quake


if __name__ == "__main__":
    run_scene("Quake Watch: nearby earthquakes", lambda p: QuakeWatchController(p),
              QuakeWatchConfig().poll_seconds)
