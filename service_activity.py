"""Bounded worker activity and local-only, age-aware explanations."""
from __future__ import annotations

import math
import time

FEED_STATES = frozenset({"fetching", "delivered", "hidden_good_air", "data_unavailable", "panel_unavailable"})
MEDIA_STATES = frozenset({"media_starting", "media_setup_wait", "media_playing", "media_idle", "media_grace", "media_unavailable"})
STATES = FEED_STATES | MEDIA_STATES
SERVICE_STATES = {"weather": FEED_STATES - {"hidden_good_air"}, "aqi": FEED_STATES, "media": MEDIA_STATES}


def valid_report(value):
    if not isinstance(value, dict) or set(value) - {"state", "next_check_seconds", "aqi", "animated"}:
        return None
    state, interval = value.get("state"), value.get("next_check_seconds")
    if not isinstance(state, str) or state not in STATES:
        return None
    if type(interval) not in (int, float) or not 0 <= interval <= 86400:
        return None
    result = {"state": state, "next_check_seconds": interval}
    if state == "hidden_good_air":
        if type(value.get("aqi")) is not int or not 0 <= value["aqi"] <= 50:
            return None
        result["aqi"] = value["aqi"]
    elif "aqi" in value:
        return None
    if state == "delivered":
        if type(value.get("animated")) is not bool:
            return None
        result["animated"] = value["animated"]
    elif "animated" in value:
        return None
    return result


def valid_record(value):
    if not isinstance(value, dict):
        return None
    report = valid_report({k: v for k, v in value.items() if k != "reported_at"})
    timestamp = value.get("reported_at")
    if report is None or type(timestamp) not in (int, float) or not 0 <= timestamp <= 253402300799:
        return None
    return {**report, "reported_at": timestamp}


def describe(value, *, now=None, media=False):
    subject = "Media" if media else "Feed"
    record = valid_record(value)
    if record is None:
        return f"{subject} activity has not been reported by this host."
    age = (time.time() if now is None else now) - record["reported_at"]
    if not math.isfinite(age) or age < -5 or age > max(30, record["next_check_seconds"] + 30):
        return f"{subject} activity is out of date; waiting for a new report."
    state = record["state"]
    if state == "media_starting":
        return "Preparing the media bridge."
    if state == "media_playing":
        return "Playback detected."
    if state == "media_idle":
        return "No media playing."
    if state == "media_grace":
        return "Playback inactive · waiting briefly."
    if state == "fetching":
        return "Checking the feed."
    if state == "delivered":
        mode = "animation enabled" if record["animated"] else "static icon"
        return f"Last page sent to clock · {mode}."
    if state == "hidden_good_air":
        return f"Page hidden · good air (AQI {record['aqi']})."
    delay = max(0, math.ceil(record["next_check_seconds"] - max(0, age)))
    retry = f"retrying in {delay} s" if delay else "retry due"
    reason = {"data_unavailable": "Feed data unavailable", "panel_unavailable": "Clock delivery failed",
              "media_unavailable": "Windows media unavailable", "media_setup_wait": "Media bridge setup unavailable"}[state]
    return f"{reason} · {retry}."
