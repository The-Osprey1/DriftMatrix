"""Bounded worker feed activity and local-only, age-aware explanations."""
from __future__ import annotations

import math
import time

STATES = frozenset({"fetching", "delivered", "hidden_good_air", "data_unavailable", "panel_unavailable"})


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


def describe(value, *, now=None):
    record = valid_record(value)
    if record is None:
        return "Feed activity has not been reported by this host."
    age = (time.time() if now is None else now) - record["reported_at"]
    if not math.isfinite(age) or age < -5 or age > max(30, record["next_check_seconds"] + 30):
        return "Feed activity is out of date; waiting for a new report."
    state = record["state"]
    if state == "fetching":
        return "Checking the feed."
    if state == "delivered":
        mode = "animation enabled" if record["animated"] else "static icon"
        return f"Last page sent to clock · {mode}."
    if state == "hidden_good_air":
        return f"Page hidden · good air (AQI {record['aqi']})."
    delay = max(0, math.ceil(record["next_check_seconds"] - max(0, age)))
    retry = f"retrying in {delay} s" if delay else "retry due"
    reason = "Feed data unavailable" if state == "data_unavailable" else "Clock delivery failed"
    return f"{reason} · {retry}."
