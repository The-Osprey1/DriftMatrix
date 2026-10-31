"""National Weather Service: what the sky is doing, and what to worry about.

Open-Meteo is a model.  On 2026-10-31 at 11:45 it called the sky overcast
(WMO 3) while Corona Municipal (KAJO, about 2 nm from the panel), Riverside
and Ontario all observed Clear.  The page's "now" therefore comes from the
nearest station that has reported recently; the model stays in charge of the
forecast and is the fallback when no station is fresh.

Alerts are the NWS's active warnings and advisories for the panel's point --
heat, wind, red flag, flood -- which the model has no notion of at all.

Both endpoints are public and need only a descriptive User-Agent.  Nothing
here touches the panel.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

OBSERVATION_URL = "https://api.weather.gov/stations/{station}/observations/latest"
ALERTS_URL = "https://api.weather.gov/alerts/active"
USER_AGENT = "DriftMatrix-Weather/1.0 (local LED panel)"

# Corona Municipal first, then Riverside Municipal and Ontario International.
DEFAULT_STATIONS: tuple[str, ...] = ("KAJO", "KRAL", "KONT")

# Stations report hourly near :53, with specials in between.  Seventy-five
# minutes covers one missed routine report without trusting a dead station.
MAX_OBSERVATION_AGE_SECONDS = 75 * 60

SEVERITY_RANK = {"Minor": 1, "Moderate": 2, "Severe": 3, "Extreme": 4}

# METAR present-weather words (as api.weather.gov spells them) -> icon kind.
# Order matters: a thunderstorm with rain is a storm, freezing rain is not rain.
_PRECIPITATION_KINDS: tuple[tuple[str, str], ...] = (
    ("thunderstorm", "storm"),
    ("freezing", "freezing"),
    ("snow", "snow"),
    ("ice", "snow"),
    ("hail", "snow"),
    ("drizzle", "drizzle"),
    ("rain", "rain"),
    ("showers", "rain"),
)
_OBSCURATION_KINDS: tuple[tuple[str, str], ...] = (
    ("fog", "fog"),
    ("mist", "fog"),
)
# Cloud cover in oktas words -> the icon set's sky kinds.
_COVER_KIND = {
    "SKC": "clear", "CLR": "clear", "FEW": "clear",
    "SCT": "part", "BKN": "cloudy", "OVC": "overcast", "VV": "overcast",
}
_COVER_RANK = {"SKC": 0, "CLR": 0, "FEW": 1, "SCT": 2, "BKN": 3, "OVC": 4, "VV": 4}
# Fallback when a station sends text but no layers.  Checked in order.
_TEXT_KIND: tuple[tuple[str, str], ...] = (
    ("thunder", "storm"),
    ("freezing", "freezing"),
    ("snow", "snow"),
    ("drizzle", "drizzle"),
    ("rain", "rain"),
    ("shower", "rain"),
    ("fog", "fog"),
    ("mist", "fog"),
    ("overcast", "overcast"),
    ("mostly cloudy", "cloudy"),
    ("partly", "part"),
    ("mostly clear", "part"),
    ("mostly sunny", "part"),
    ("cloudy", "overcast"),
    ("clear", "clear"),
    ("sunny", "clear"),
    ("fair", "clear"),
)
_PRECIPITATING = frozenset({"storm", "freezing", "snow", "drizzle", "rain"})


@dataclass(frozen=True)
class Observation:
    station: str
    observed_at: datetime
    temperature_f: float | None
    icon_kind: str | None
    precipitating: bool
    wind_gust_mph: float | None


@dataclass(frozen=True)
class Alert:
    event: str
    severity: str
    kind: str
    label: str
    effective: datetime | None
    expires: datetime


def _value(block: Any) -> float | None:
    """A ``{"value": x, "unitCode": ...}`` quantity, or None when missing."""

    if not isinstance(block, Mapping):
        return None
    value = block.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _coerce_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)


def sky_kind(present_weather: Sequence[Any], cloud_layers: Sequence[Any], text: str) -> str | None:
    """The icon kind a station's report describes, or None if it says nothing."""

    words = " ".join(
        str(item.get("weather", "")) for item in present_weather if isinstance(item, Mapping)
    ).lower().replace("_", " ")
    for word, kind in _PRECIPITATION_KINDS + _OBSCURATION_KINDS:
        if word in words:
            return kind
    covers = [
        str(layer.get("amount", "")).upper()
        for layer in cloud_layers
        if isinstance(layer, Mapping) and str(layer.get("amount", "")).upper() in _COVER_RANK
    ]
    if covers:
        return _COVER_KIND[max(covers, key=_COVER_RANK.__getitem__)]
    lowered = text.lower()
    for word, kind in _TEXT_KIND:
        if word in lowered:
            return kind
    return None


def parse_observation(payload: Mapping[str, Any], station: str) -> Observation:
    properties = payload.get("properties") if isinstance(payload, Mapping) else None
    if not isinstance(properties, Mapping):
        raise ValueError(f"{station}: observation has no properties")
    observed_at = _timestamp(properties.get("timestamp"))
    if observed_at is None:
        raise ValueError(f"{station}: observation has no timestamp")
    celsius = _value(properties.get("temperature"))
    gust_kmh = _value(properties.get("windGust"))
    present = properties.get("presentWeather") or ()
    layers = properties.get("cloudLayers") or ()
    text = properties.get("textDescription") or ""
    kind = sky_kind(
        present if isinstance(present, Sequence) else (),
        layers if isinstance(layers, Sequence) else (),
        text if isinstance(text, str) else "",
    )
    return Observation(
        station=station,
        observed_at=observed_at,
        temperature_f=None if celsius is None else celsius * 9.0 / 5.0 + 32.0,
        icon_kind=kind,
        precipitating=kind in _PRECIPITATING,
        wind_gust_mph=None if gust_kmh is None else gust_kmh / 1.609344,
    )


def latest_observation(
    session: Any,
    stations: Sequence[str] = DEFAULT_STATIONS,
    *,
    now: datetime | None = None,
    timeout: float = 10.0,
    max_age_seconds: float = MAX_OBSERVATION_AGE_SECONDS,
) -> Observation | None:
    """The first station, in order, with a fresh report carrying a temperature.

    Every failure is per station and quiet: the caller falls back to the model
    when this returns None, so one dead station never blanks the page.
    """

    reference = _coerce_now(now)
    for station in stations:
        try:
            response = session.get(
                OBSERVATION_URL.format(station=station),
                headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
                timeout=timeout,
            )
            response.raise_for_status()
            observation = parse_observation(response.json(), station)
        except Exception:  # noqa: BLE001 - any station fault means "try the next"
            continue
        age = (reference - observation.observed_at).total_seconds()
        if observation.temperature_f is None or not -300 <= age <= max_age_seconds:
            continue
        return observation
    return None


# ---------------------------------------------------------------- Alerts

# Event name fragment -> (icon kind, lane label).  Labels fit the 23px lane.
_ALERT_KINDS: tuple[tuple[str, str, str], ...] = (
    ("red flag", "fire", "FIRE"),
    ("fire", "fire", "FIRE"),
    ("heat", "heat", "HEAT"),
    ("wind", "wind", "WIND"),
    ("flood", "flood", "FLOOD"),
    ("thunderstorm", "storm", "STORM"),
    ("tornado", "storm", "STORM"),
    ("freeze", "snow", "COLD"),
    ("frost", "snow", "COLD"),
    ("cold", "snow", "COLD"),
    ("winter", "snow", "SNOW"),
    ("smoke", "alert", "SMOKE"),
    ("dust", "alert", "DUST"),
    ("air quality", "alert", "AIR"),
)


def alert_kind(event: str) -> tuple[str, str]:
    lowered = event.lower()
    for fragment, kind, label in _ALERT_KINDS:
        if fragment in lowered:
            return kind, label
    return "alert", "ALERT"


def parse_alerts(
    payload: Mapping[str, Any],
    *,
    min_severity: str = "Moderate",
    now: datetime | None = None,
) -> list[Alert]:
    """Active, unexpired alerts at or above ``min_severity``, worst first."""

    reference = _coerce_now(now)
    floor = SEVERITY_RANK.get(min_severity, SEVERITY_RANK["Moderate"])
    features = payload.get("features") if isinstance(payload, Mapping) else None
    found: list[Alert] = []
    for feature in features if isinstance(features, list) else ():
        properties = feature.get("properties") if isinstance(feature, Mapping) else None
        if not isinstance(properties, Mapping):
            continue
        if str(properties.get("status", "Actual")) != "Actual":
            continue
        if str(properties.get("messageType", "Alert")) == "Cancel":
            continue
        severity = str(properties.get("severity", ""))
        if SEVERITY_RANK.get(severity, 0) < floor:
            continue
        expires = _timestamp(properties.get("ends")) or _timestamp(properties.get("expires"))
        if expires is None or expires <= reference:
            continue
        effective = _timestamp(properties.get("onset")) or _timestamp(properties.get("effective"))
        if effective is not None and effective > reference:
            continue  # issued ahead of time; not in force yet
        event = str(properties.get("event", "")).strip()
        kind, label = alert_kind(event)
        found.append(Alert(event, severity, kind, label, effective, expires))
    found.sort(key=lambda alert: (-SEVERITY_RANK[alert.severity], alert.expires))
    return found


def active_alerts(
    session: Any,
    latitude: float,
    longitude: float,
    *,
    min_severity: str = "Moderate",
    now: datetime | None = None,
    timeout: float = 10.0,
) -> list[Alert]:
    # api.weather.gov redirects points with more than four decimals.
    point = f"{latitude:.4f},{longitude:.4f}"
    response = session.get(
        ALERTS_URL,
        params={"point": point, "status": "actual"},
        headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
        timeout=timeout,
    )
    response.raise_for_status()
    return parse_alerts(response.json(), min_severity=min_severity, now=now)
