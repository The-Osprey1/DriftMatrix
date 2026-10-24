"""Inspect and apply the product's persistent AWTRIX app-loop policy.

The order call is intentionally a one-time/reconciliation operation, not a
background polling service. App relevance remains owned by each app: scripts use
``should_show()`` and pushed senders delete their page when it is no longer useful.

Check only:
    C:/Users/devyn/anaconda3/python.exe rotation_policy.py

Apply after reviewing the diff:
    C:/Users/devyn/anaconda3/python.exe rotation_policy.py --apply
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import requests

import panel_gate


AWTRIX = "http://192.168.1.90"
GATE_SOURCE = "rotation"
ORDER_PATH = "/api/v1/apps/order"
SAVED_ORDER_PATH = "/apploop.json"
SETTINGS_PATH = "/api/v1/settings"
REQUEST_TIMEOUT_SECONDS = 5
DEVICE_PATH = "/api/v1/device"
APPS_PATH = "/api/v1/apps"
LOGS_PATH = "/api/v1/logs"
DEFAULT_HEALTH_SAMPLES = 3
DEFAULT_HEALTH_INTERVAL_SECONDS = 2.0
MIN_MUTATION_SPACING_SECONDS = 2.0

# Time appears twice so the clock remains the visual anchor even after the
# contextual pages and future apps fill out the product.
TARGET_ORDER = (
    "Time",
    "weatherbrief",
    "nowplaying",
    "Time",
    "ocean",
    "moon",
    "aqibrief",
    "pcstats0",
    "networksentinel",
    "nextup",
    "focus",
    "nightkeeper",
)

# Date and Battery were deliberately removed earlier. timepiece is the retired
# custom clock, and pcstats1 is the retired second page from the old array push.
#
# meridian was removed on request -- the day-ribbon design did not land. Ocean
# takes its place as the calm page after the second Time anchor.
#
# sunset is back (2026-10-03). It had been removed because scripts compile in
# alphabetical order and, on firmware 1.1.0 with Nightkeeper 2.x, only 26,612
# bytes of block remained when sunset (last) was reached; it needs ~35,000.
# On 1.1.2 with Nightkeeper 3.0, 47,092 remain at that point and all five
# scripts restored cleanly: 82,496 of 98,304 bytes of script heap (84%), block
# 36,852 after boot. That is above the ~75% comfort line, so the nightly
# maintenance report's script_errors is the thing to watch.
#
# The three scenes take turns rather than all cycling all day: sunset
# publishes shared "sunset.phase" from real sun times and ocean (day), sunset
# (the hour around sunset) and moon (night) each show only in their phase.
# 2026-10-04: sunset came off again so the old moon tracker (weather/moon-
# panel.ax: nebula, eclipses, the glide to an info screen) could return at the
# user's request. Four scripts at boot: 80,788 of 98,304 B (82%).
TARGET_DISABLED = ("Battery", "Date", "timepiece", "pcstats1", "meridian", "sunset")

# Two shorter Time appearances preserve roughly the same total clock time as
# the old single 15-second visit, but break up the contextual pages. A fixed,
# shorter transition makes the product rhythm predictable.
TARGET_SETTINGS = {
    "appDurationMs": 8_000,
    "transitionEffect": "Dim",
    "transitionDurationMs": 360,
}


@dataclass(frozen=True)
class LoopState:
    order: tuple[str, ...]
    disabled: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "LoopState":
        return cls(
            order=_name_list(value.get("order"), "order"),
            disabled=_name_list(value.get("disabled"), "disabled"),
        )

    def payload(self) -> dict[str, list[str]]:
        return {"order": list(self.order), "disabled": list(self.disabled)}


TARGET_STATE = LoopState(TARGET_ORDER, TARGET_DISABLED)


def verify_panel_stable(
    session: requests.Session,
    base_url: str = AWTRIX,
    samples: int = DEFAULT_HEALTH_SAMPLES,
    interval_seconds: float = DEFAULT_HEALTH_INTERVAL_SECONDS,
    minimum_uptime: int | None = None,
) -> tuple[int, ...]:
    """Require repeated healthy reads and monotonically increasing uptime.

    A previous panic remains visible as ``resetReason`` for the entire boot, so
    the useful deployment signal is not that string alone.  We instead require
    every read-only surface used by the rollout to answer on every sample and
    reject an uptime rollback, which is direct evidence of a reboot during the
    preflight window.
    """

    if samples < 2:
        raise ValueError("panel stability requires at least two samples")
    if interval_seconds < 0:
        raise ValueError("panel stability interval cannot be negative")
    if minimum_uptime is not None and minimum_uptime < 0:
        raise ValueError("minimum_uptime cannot be negative")

    base = base_url.rstrip("/")
    uptimes: list[int] = []
    for sample in range(samples):
        device = session.get(f"{base}{DEVICE_PATH}", timeout=REQUEST_TIMEOUT_SECONDS)
        device.raise_for_status()
        body = device.json()
        if not isinstance(body, Mapping):
            raise ValueError("AWTRIX device response is not an object")
        try:
            uptime = int(body["uptimeSeconds"])
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise ValueError("AWTRIX device response has no valid uptimeSeconds") from error
        if uptime < 0:
            raise ValueError("AWTRIX uptimeSeconds cannot be negative")
        previous_uptime = uptimes[-1] if uptimes else minimum_uptime
        if previous_uptime is not None and uptime < previous_uptime:
            raise RuntimeError(
                "AWTRIX rebooted during deployment preflight: "
                f"{previous_uptime} -> {uptime}"
            )
        uptimes.append(uptime)

        for path in (APPS_PATH, LOGS_PATH, SAVED_ORDER_PATH):
            response = session.get(f"{base}{path}", timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()

        if sample + 1 < samples and interval_seconds:
            time.sleep(interval_seconds)
    return tuple(uptimes)


def _name_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{field} must be a list of app names")
    result: list[str] = []
    for name in value:
        if not isinstance(name, str) or not name:
            raise ValueError(f"{field} contains an invalid app name")
        result.append(name)
    return tuple(result)


def fetch_state(session: requests.Session, base_url: str = AWTRIX) -> LoopState:
    response = session.get(
        f"{base_url.rstrip('/')}{SAVED_ORDER_PATH}",
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, Mapping):
        raise ValueError("AWTRIX app-loop response is not an object")
    return LoopState.from_mapping(body)


def fetch_rotation_settings(
    session: requests.Session, base_url: str = AWTRIX
) -> dict[str, Any]:
    response = session.get(
        f"{base_url.rstrip('/')}{SETTINGS_PATH}",
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, Mapping):
        raise ValueError("AWTRIX settings response is not an object")
    missing = [name for name in TARGET_SETTINGS if name not in body]
    if missing:
        raise ValueError(f"AWTRIX settings response is missing {missing}")
    return {name: body[name] for name in TARGET_SETTINGS}


def apply_rotation_settings(
    session: requests.Session,
    settings: Mapping[str, Any] = TARGET_SETTINGS,
    base_url: str = AWTRIX,
) -> None:
    body = dict(settings)
    with panel_gate.mutation(GATE_SOURCE, "settings", body=body) as gate:
        response = session.patch(
            f"{base_url.rstrip('/')}{SETTINGS_PATH}",
            json=body,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        gate.observe(response)
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"AWTRIX HTTP {response.status_code}: {response.text[:200]}")


def apply_state(
    session: requests.Session,
    state: LoopState = TARGET_STATE,
    base_url: str = AWTRIX,
) -> None:
    body = state.payload()
    with panel_gate.mutation(GATE_SOURCE, "other", body=body) as gate:
        response = session.put(
            f"{base_url.rstrip('/')}{ORDER_PATH}",
            json=body,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        gate.observe(response)
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"AWTRIX HTTP {response.status_code}: {response.text[:200]}")


def save_backup(state: LoopState, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    path = directory / f"apploop-before-relevance-{stamp}.json"
    path.write_text(json.dumps(state.payload(), indent=2) + "\n", encoding="utf-8")
    return path


def save_settings_backup(settings: Mapping[str, Any], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    path = directory / f"settings-before-relevance-{stamp}.json"
    path.write_text(json.dumps(dict(settings), indent=2) + "\n", encoding="utf-8")
    return path


def describe_change(current: LoopState, target: LoopState = TARGET_STATE) -> str:
    if current == target:
        return "App loop already matches the product policy."
    lines = ["App-loop change:"]
    lines.append(f"  order:    {list(current.order)}")
    lines.append(f"         -> {list(target.order)}")
    lines.append(f"  disabled: {list(current.disabled)}")
    lines.append(f"         -> {list(target.disabled)}")
    return "\n".join(lines)


def describe_settings_change(current: Mapping[str, Any]) -> str:
    if dict(current) == TARGET_SETTINGS:
        return "Rotation timing already matches the product policy."
    return (
        "Rotation-timing change:\n"
        f"  current: {dict(current)}\n"
        f"  target:  {TARGET_SETTINGS}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--awtrix", default=AWTRIX)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path, default=Path("backups/rotation"))
    parser.add_argument("--health-samples", type=int, default=DEFAULT_HEALTH_SAMPLES)
    parser.add_argument(
        "--health-interval",
        type=float,
        default=DEFAULT_HEALTH_INTERVAL_SECONDS,
        help="seconds between read-only preflight samples",
    )
    return parser.parse_args()


@dataclass(frozen=True)
class ApplyResult:
    order_changed: bool
    settings_changed: bool
    order_backup: Path | None = None
    settings_backup: Path | None = None
    preflight_uptimes: tuple[int, ...] = ()
    postflight_uptimes: tuple[int, ...] = ()


def apply_policy(
    session: requests.Session,
    *,
    base_url: str = AWTRIX,
    backup_dir: Path = Path("backups/rotation"),
    health_samples: int = DEFAULT_HEALTH_SAMPLES,
    health_interval_seconds: float = DEFAULT_HEALTH_INTERVAL_SECONDS,
    mutation_spacing_seconds: float = MIN_MUTATION_SPACING_SECONDS,
) -> ApplyResult:
    """Apply the exact policy with pre-write backups and post-write stability.

    Both mutable surfaces are captured before the first write, even when only
    one differs.  That keeps a coherent pre-release rollback pair if a small
    settings mutation exposes an unrelated panel problem.
    """

    if mutation_spacing_seconds < 0:
        raise ValueError("mutation spacing cannot be negative")

    preflight = verify_panel_stable(
        session,
        base_url,
        samples=health_samples,
        interval_seconds=health_interval_seconds,
    )

    # Read the exact pre-write state after the stability gate; the earlier
    # check-only display may be several seconds old by now.
    current = fetch_state(session, base_url)
    current_settings = fetch_rotation_settings(session, base_url)
    order_changed = current != TARGET_STATE
    settings_changed = current_settings != TARGET_SETTINGS
    if not order_changed and not settings_changed:
        return ApplyResult(
            order_changed=False,
            settings_changed=False,
            preflight_uptimes=preflight,
        )

    settings_backup = save_settings_backup(current_settings, backup_dir)
    order_backup = save_backup(current, backup_dir)
    print(f"Settings backup: {settings_backup.resolve()}")
    print(f"Order backup: {order_backup.resolve()}")

    if settings_changed:
        apply_rotation_settings(session, TARGET_SETTINGS, base_url)
        verified_settings = fetch_rotation_settings(session, base_url)
        if verified_settings != TARGET_SETTINGS:
            raise RuntimeError(
                f"AWTRIX rotation-timing verification failed: {verified_settings!r}"
            )

    if settings_changed and order_changed and mutation_spacing_seconds:
        time.sleep(mutation_spacing_seconds)

    if order_changed:
        apply_state(session, TARGET_STATE, base_url)
        verified_state = fetch_state(session, base_url)
        if verified_state != TARGET_STATE:
            raise RuntimeError(
                f"AWTRIX app-loop verification failed: {verified_state!r}"
            )

    postflight = verify_panel_stable(
        session,
        base_url,
        samples=health_samples,
        interval_seconds=health_interval_seconds,
        minimum_uptime=preflight[-1],
    )
    return ApplyResult(
        order_changed=order_changed,
        settings_changed=settings_changed,
        order_backup=order_backup,
        settings_backup=settings_backup,
        preflight_uptimes=preflight,
        postflight_uptimes=postflight,
    )


def main() -> None:
    args = parse_args()
    session = requests.Session()
    current = fetch_state(session, args.awtrix)
    current_settings = fetch_rotation_settings(session, args.awtrix)
    print(describe_change(current))
    print(describe_settings_change(current_settings))
    order_changed = current != TARGET_STATE
    settings_changed = current_settings != TARGET_SETTINGS
    if not args.apply or (not order_changed and not settings_changed):
        return
    result = apply_policy(
        session,
        base_url=args.awtrix,
        backup_dir=args.backup_dir,
        health_samples=args.health_samples,
        health_interval_seconds=args.health_interval,
    )
    print(f"Panel stability preflight passed: uptime {list(result.preflight_uptimes)}")
    if not result.order_changed and not result.settings_changed:
        print("Policy became current during preflight; no mutation was required.")
        return
    print(f"Panel post-apply stability passed: uptime {list(result.postflight_uptimes)}")
    print("Applied, read back, and stability-verified.")


if __name__ == "__main__":
    main()
