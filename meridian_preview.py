"""Solar geometry and artwork for the AWTRIX ``Meridian`` app.

Meridian renders one whole day across the panel's 32 columns. The panel is
4:1, which makes it a timeline before it is anything else: 32 columns over 24
hours is 45 minutes per column, and the eye reads position-in-day instantly
once the frame of reference is fixed.

Two channels share the same ribbon:

* **Sky colour** comes from the sun's computed altitude at that column's time,
  so the gradient is the real sky rather than a hand-drawn one. Night, the
  three twilights, low sun and high sun each get their own stop.
* **A bright pixel per column** sits at the height of the sun's altitude,
  tracing the actual arc of the day. In June the arc is tall and wide; in
  December it is low and short, and the panel shows you that without a word.

A full-height light shaft marks *now*, and the bottom row carries pips for
sunrise, solar noon, sunset, and the start of the Nightkeeper blackout. How
much daylight is left is the distance from the shaft to the sunset pip.

This module has no network, panel, or HTTP code. Sun times are computed here
with the NOAA solar equations rather than fetched, so the app keeps working
with the internet down and the tests stay deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import math
from zoneinfo import ZoneInfo

RGB = tuple[int, int, int]
Frame = list[list[RGB]]

WIDTH = 32
HEIGHT = 8
MINUTES_PER_DAY = 24 * 60
MINUTES_PER_COLUMN = MINUTES_PER_DAY // WIDTH  # 45

LATITUDE = 33.87529
LONGITUDE = -117.56644
TIMEZONE = "America/Los_Angeles"

# Nightkeeper's blackout, mirrored here only to draw its pip. Meridian never
# writes brightness or power; it just shows where the evening ends.
BLACKOUT_MINUTE = 21 * 60 + 30

BLACK: RGB = (0, 0, 0)

# Sky stops, keyed by solar altitude in degrees. Between two stops the colour
# is interpolated, so the ribbon is continuous rather than banded.
#
# The boundaries are the real ones: -18 ends astronomical twilight, -12 nautical,
# -6 civil, and 0 is the horizon. Using the actual definitions means the picture
# agrees with what is outside the window instead of approximating it.
#
# Daylight is deliberately a DEEP blue rather than a realistic pale one. A
# literal daytime sky renders as a bright slab across two thirds of the panel,
# which glares in a dark room and leaves the sun arc -- the thing this app is
# actually about -- with nothing to stand out against. Keeping the backdrop
# dark spends the panel's small dynamic range on the sun and on twilight, which
# is where the information is. Dawn and dusk keep their saturation and become
# the colour event of the day.
# The panel's transfer function is the real constraint here. At brightness 45
# with gamma 1.8 the output is about 45*(source/255)**1.8, so a source value of
# 80 lands on 5 and anything under about 60 is simply black. A palette picked
# to look right on a monitor turns to mud on the panel; these values are chosen
# by target OUTPUT and converted back, which is why they look oddly bright as
# literals. Day sits near output 15, twilight 25-30, and the sun takes the
# ceiling at 45 so it always wins.
SKY_STOPS: tuple[tuple[float, RGB], ...] = (
    (-90.0, (10, 14, 50)),     # deep night: barely lit, but not dead
    (-18.0, (12, 18, 62)),     # astronomical twilight begins
    (-12.0, (20, 28, 84)),     # nautical
    (-6.0, (56, 44, 112)),     # civil: the sky starts to bruise
    (-2.0, (150, 70, 110)),    # the horizon reddens
    (0.0, (205, 96, 64)),      # sunrise / sunset proper
    (4.0, (190, 122, 72)),     # the fire fades as the sun clears the horizon
    (14.0, (80, 120, 150)),    # settled into day
    (40.0, (78, 128, 162)),
    (90.0, (80, 132, 168)),
)

SUN_CORE: RGB = (255, 250, 228)
SUN_LOW: RGB = (255, 150, 50)
# A dimmer echo one row below the sun, so a 7-row arc reads as a curve instead
# of a staircase. Without it the quantisation is the most visible thing.
SUN_TRAIL_STRENGTH = 0.42

SHAFT: RGB = (198, 232, 255)
GROUND: RGB = (10, 10, 16)
PIP_SUNRISE: RGB = (255, 158, 54)
PIP_NOON: RGB = (250, 246, 232)
PIP_SUNSET: RGB = (255, 84, 74)
PIP_BLACKOUT: RGB = (70, 84, 170)

# The horizon refraction NOAA uses for sunrise and sunset.
SUNRISE_ALTITUDE = -0.833

# The bottom row is ground: it carries the pips and never carries sky, so a pip
# is always readable no matter what the sun is doing above it.
SKY_ROWS = HEIGHT - 1
# Altitude mapped to the top of the arc. Fixed rather than per-day, so a
# December arc genuinely sits low and a June arc fills the panel. The local
# summer peak is 79.6 degrees, so 80 puts the year's highest sun on row 0.
ALTITUDE_CEILING = 80.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _mix(a: RGB, b: RGB, t: float) -> RGB:
    t = _clamp(t, 0.0, 1.0)
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))  # type: ignore[return-value]


def new_frame() -> Frame:
    return [[BLACK for _ in range(WIDTH)] for _ in range(HEIGHT)]


# --------------------------------------------------------------- solar model


@dataclass(frozen=True)
class SolarDay:
    """The sun's shape for one local calendar day."""

    day: date
    sunrise_minute: float | None
    sunset_minute: float | None
    noon_minute: float
    peak_altitude: float

    @property
    def daylight_minutes(self) -> float:
        if self.sunrise_minute is None or self.sunset_minute is None:
            return 0.0
        return max(0.0, self.sunset_minute - self.sunrise_minute)


def _fractional_year(day: date, minute: float) -> float:
    """NOAA's gamma, in radians."""
    day_of_year = day.timetuple().tm_yday
    hours = minute / 60.0
    return 2.0 * math.pi / 365.0 * (day_of_year - 1 + (hours - 12.0) / 24.0)


def _equation_of_time(gamma: float) -> float:
    """Minutes by which true solar time leads mean solar time."""
    return 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )


def _declination(gamma: float) -> float:
    """Solar declination, in radians."""
    return (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )


def _utc_offset_minutes(day: date, zone: str) -> float:
    """The zone's offset on this date, so DST is handled by the tz database."""
    noon = datetime(day.year, day.month, day.day, 12, tzinfo=ZoneInfo(zone))
    offset = noon.utcoffset()
    return 0.0 if offset is None else offset.total_seconds() / 60.0


def solar_altitude(
    day: date,
    local_minute: float,
    latitude: float = LATITUDE,
    longitude: float = LONGITUDE,
    zone: str = TIMEZONE,
) -> float:
    """The sun's altitude in degrees at a local wall-clock minute."""
    offset = _utc_offset_minutes(day, zone)
    utc_minute = local_minute - offset
    gamma = _fractional_year(day, utc_minute)
    eqtime = _equation_of_time(gamma)
    decl = _declination(gamma)

    # True solar time, then the hour angle away from local solar noon.
    true_solar = utc_minute + eqtime + 4.0 * longitude
    hour_angle = math.radians(true_solar / 4.0 - 180.0)

    lat = math.radians(latitude)
    cos_zenith = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(
        hour_angle
    )
    return 90.0 - math.degrees(math.acos(_clamp(cos_zenith, -1.0, 1.0)))


def _horizon_crossing(
    day: date,
    latitude: float,
    longitude: float,
    rising: bool,
    seed_utc_minute: float,
) -> float | None:
    """Solve for a horizon crossing in UTC minutes, refining the seed estimate.

    Evaluating the equation of time and declination once at noon and reusing
    them for both crossings costs a couple of minutes at the far end of the
    day, because declination has moved by the time the sun sets. Re-evaluating
    them at each crossing's own estimate converges in two passes and brings
    both ends inside half a minute.
    """
    lat = math.radians(latitude)
    guess = seed_utc_minute
    for _ in range(4):
        gamma = _fractional_year(day, guess)
        eqtime = _equation_of_time(gamma)
        decl = _declination(gamma)

        numerator = math.cos(math.radians(90.0 - SUNRISE_ALTITUDE)) - math.sin(lat) * math.sin(
            decl
        )
        denominator = math.cos(lat) * math.cos(decl)
        if denominator == 0.0 or abs(numerator / denominator) > 1.0:
            # Polar day or polar night: no crossing exists. Unreachable at this
            # latitude, but the renderer must not divide by a missing sunrise.
            return None

        hour_angle = math.degrees(math.acos(numerator / denominator))
        signed = hour_angle if rising else -hour_angle
        updated = 720.0 - 4.0 * (longitude + signed) - eqtime
        if abs(updated - guess) < 0.01:
            return updated
        guess = updated
    return guess


def solar_day(
    day: date,
    latitude: float = LATITUDE,
    longitude: float = LONGITUDE,
    zone: str = TIMEZONE,
) -> SolarDay:
    """Sunrise, sunset, solar noon and peak altitude in local minutes."""
    offset = _utc_offset_minutes(day, zone)
    noon_gamma = _fractional_year(day, 12 * 60)
    noon_utc = 720.0 - 4.0 * longitude - _equation_of_time(noon_gamma)
    noon_local = noon_utc + offset

    sunrise_utc = _horizon_crossing(day, latitude, longitude, True, noon_utc - 360.0)
    sunset_utc = _horizon_crossing(day, latitude, longitude, False, noon_utc + 360.0)

    return SolarDay(
        day=day,
        sunrise_minute=None if sunrise_utc is None else sunrise_utc + offset,
        sunset_minute=None if sunset_utc is None else sunset_utc + offset,
        noon_minute=noon_local,
        peak_altitude=solar_altitude(day, noon_local, latitude, longitude, zone),
    )


# ------------------------------------------------------------------ artwork


def sky_color(altitude: float) -> RGB:
    """Interpolate the sky palette at a solar altitude."""
    stops = SKY_STOPS
    if altitude <= stops[0][0]:
        return stops[0][1]
    if altitude >= stops[-1][0]:
        return stops[-1][1]
    for (low_alt, low_rgb), (high_alt, high_rgb) in zip(stops, stops[1:]):
        if low_alt <= altitude <= high_alt:
            span = high_alt - low_alt
            return _mix(low_rgb, high_rgb, 0.0 if span == 0 else (altitude - low_alt) / span)
    return stops[-1][1]


def column_minute(column: int) -> float:
    """The local minute at the centre of a column, so pips land where they look."""
    return column * MINUTES_PER_COLUMN + MINUTES_PER_COLUMN / 2.0


def minute_column(minute: float) -> int:
    """Which column a local minute falls in."""
    return int(_clamp(minute // MINUTES_PER_COLUMN, 0, WIDTH - 1))


def sun_height(altitude: float) -> float | None:
    """Fractional row for the sun, or None when it is below the horizon.

    Returned unrounded so the caller can shade the neighbouring row and turn a
    seven-row staircase into something the eye reads as a curve.
    """
    if altitude <= 0.0:
        return None
    fraction = _clamp(altitude / ALTITUDE_CEILING, 0.0, 1.0)
    # Row SKY_ROWS-1 is the horizon and row 0 the top of the panel.
    return (SKY_ROWS - 1) * (1.0 - fraction)


def _vertical_shade(base: RGB, row: int) -> RGB:
    """Lift the horizon end of each column so the ribbon reads as sky, not a bar."""
    # A real sky is deepest overhead and brightest near the horizon. Kept
    # subtle: the altitude already chose the colour, and a strong gradient
    # would compete with the sun arc for the little contrast there is.
    depth = 0.74 + 0.26 * (row / max(1, SKY_ROWS - 1))
    return tuple(int(round(channel * depth)) for channel in base)  # type: ignore[return-value]


def render(
    now: datetime,
    latitude: float = LATITUDE,
    longitude: float = LONGITUDE,
    zone: str = TIMEZONE,
    blackout_minute: int | None = BLACKOUT_MINUTE,
) -> Frame:
    """One 32x8 frame of the day containing ``now``."""
    day = now.date()
    info = solar_day(day, latitude, longitude, zone)
    frame = new_frame()

    for x in range(WIDTH):
        minute = column_minute(x)
        altitude = solar_altitude(day, minute, latitude, longitude, zone)
        base = sky_color(altitude)
        for y in range(SKY_ROWS):
            frame[y][x] = _vertical_shade(base, y)
        frame[SKY_ROWS][x] = GROUND

        height = sun_height(altitude)
        if height is not None:
            # Warm the sun near the horizon and blanch it at altitude, which is
            # what it actually does and what keeps the arc legible.
            warmth = _clamp(altitude / ALTITUDE_CEILING, 0.0, 1.0)
            disc = _mix(SUN_LOW, SUN_CORE, warmth)
            # The nearest row takes the disc at full strength and the row it is
            # drifting toward takes a proportional glow. Splitting the disc
            # across both rows instead conserved ink but halved the peak, and a
            # sun that never reaches the panel's ceiling loses to its own sky.
            row = int(round(height))
            frame[row][x] = disc
            neighbour = row + (1 if height > row else -1)
            if 0 <= neighbour < SKY_ROWS:
                frame[neighbour][x] = _mix(
                    frame[neighbour][x], disc, abs(height - row) * 2.0 * SUN_TRAIL_STRENGTH
                )

    # Pips ride the ground row, which the sky never touches.
    for minute, color in (
        (info.sunrise_minute, PIP_SUNRISE),
        (info.noon_minute, PIP_NOON),
        (info.sunset_minute, PIP_SUNSET),
        (float(blackout_minute) if blackout_minute is not None else None, PIP_BLACKOUT),
    ):
        if minute is None:
            continue
        frame[HEIGHT - 1][minute_column(minute)] = color

    now_minute = now.hour * 60 + now.minute + now.second / 60.0
    apply_now_shaft(frame, now_minute)
    return frame


def apply_now_shaft(frame: Frame, now_minute: float, strength: float = 0.62) -> None:
    """Brighten the current column into a full-height shaft of light.

    Lifting the existing colour rather than overwriting it keeps the sky and
    the sun visible underneath, so the marker reads as *where you are* instead
    of punching a white hole in the picture.

    The shaft is cool rather than neutral so it cannot be mistaken for the sun.
    Everything else that is bright on this panel is warm, which leaves the cool
    end of the spectrum free for the one element that has to be unambiguous.
    """
    x = minute_column(now_minute)
    for y in range(HEIGHT):
        frame[y][x] = _mix(frame[y][x], SHAFT, strength)


def frame_bytes(frame: Frame) -> bytes:
    """Row-major RGB triples, the layout the panel's bitmap draw op expects."""
    return bytes(channel for row in frame for color in row for channel in color)


def describe(now: datetime) -> str:
    """One line for a console dry run."""
    info = solar_day(now.date())
    def hhmm(minute: float | None) -> str:
        if minute is None:
            return "--:--"
        minute = int(round(minute)) % MINUTES_PER_DAY
        return f"{minute // 60:02d}:{minute % 60:02d}"

    daylight = info.daylight_minutes
    return (
        f"{now:%Y-%m-%d %H:%M}  sunrise {hhmm(info.sunrise_minute)}  "
        f"noon {hhmm(info.noon_minute)}  sunset {hhmm(info.sunset_minute)}  "
        f"daylight {int(daylight) // 60}h{int(daylight) % 60:02d}m  "
        f"peak {info.peak_altitude:.1f}deg"
    )
