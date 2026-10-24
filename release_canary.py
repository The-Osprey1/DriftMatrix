r"""Controlled live release canaries for Network Sentinel and Next Up.

The default mode is offline.  ``--live`` is deliberately required before any
panel mutation.  A live run refuses to touch a same-name app that is already
present, uses short self-removing pushed-app lifetimes, captures evidence under
``%LOCALAPPDATA%\AWTRIX\canaries``, and safely deletes only confirmed pushed
apps during cleanup.

Live release gate:
    C:/Users/devyn/anaconda3/python.exe release_canary.py --live
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import requests

import network_sentinel as sentinel
import network_sentinel_preview as sentinel_art
import next_up_controller as nextup_controller
import next_up_preview as nextup_art
import utility_hub as hub
from next_up import EventStage, events_from_ics
from panel_client import MutationPriority, PanelClient, PanelPolicy, is_quiet_time
from rotation_policy import TARGET_SETTINGS, TARGET_STATE, verify_panel_stable


AWTRIX = "http://192.168.1.90"
EXPECTED_UID = "680947376e9c"
NETWORK_APP = hub.NETWORK_APP_NAME
NEXT_UP_APP = nextup_controller.APP_NAME
REQUEST_TIMEOUT_SECONDS = 5.0
PACIFIC = ZoneInfo("America/Los_Angeles")


class CanaryError(RuntimeError):
    """A canary precondition, lifecycle step, or verification failed."""


class SequenceProbe:
    def __init__(self, samples: Sequence[sentinel.ProbeSample]) -> None:
        self.samples = list(samples)

    def sample(self) -> sentinel.ProbeSample:
        if not self.samples:
            raise CanaryError("synthetic network probe sequence was exhausted")
        return self.samples.pop(0)


@dataclass
class CanaryClock:
    wall: datetime
    mono: float = 0.0

    def monotonic(self) -> float:
        return self.mono

    def wall_now(self) -> datetime:
        return self.wall

    def time(self) -> float:
        return self.wall.timestamp()

    def advance(self, seconds: float) -> None:
        self.mono += float(seconds)
        self.wall += timedelta(seconds=float(seconds))


def default_output_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise CanaryError("LOCALAPPDATA is unavailable; pass --output-root")
    return Path(local) / "AWTRIX" / "canaries"


def _atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def build_synthetic_ics(now: datetime, *, minutes_until: int = 14) -> bytes:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    start = (now + timedelta(minutes=minutes_until)).astimezone(timezone.utc)
    end = start + timedelta(minutes=30)
    stamp = now.astimezone(timezone.utc)
    values = {
        "stamp": stamp.strftime("%Y%m%dT%H%M%SZ"),
        "start": start.strftime("%Y%m%dT%H%M%SZ"),
        "end": end.strftime("%Y%m%dT%H%M%SZ"),
    }
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//AWTRIX//Release Canary//EN\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:awtrix-release-canary@example.invalid\r\n"
        f"DTSTAMP:{values['stamp']}\r\n"
        f"DTSTART:{values['start']}\r\n"
        f"DTEND:{values['end']}\r\n"
        "SUMMARY:AWTRIX synthetic release canary\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    ).encode("ascii")


def _inventory(session: requests.Session, root: str) -> list[Mapping[str, Any]]:
    response = session.get(f"{root}/api/v1/apps", timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    body = response.json()
    if isinstance(body, Mapping):
        body = body.get("apps", body.get("value"))
    if not isinstance(body, list):
        raise CanaryError("app inventory has an unexpected shape")
    return [item for item in body if isinstance(item, Mapping)]


def _app(session: requests.Session, root: str, name: str) -> Mapping[str, Any] | None:
    matches = [item for item in _inventory(session, root) if item.get("name") == name]
    if len(matches) > 1:
        raise CanaryError(f"app inventory contains duplicate {name!r} entries")
    return matches[0] if matches else None


def _require_absent(session: requests.Session, root: str, name: str) -> None:
    item = _app(session, root, name)
    if item is not None and item.get("present") is True:
        raise CanaryError(f"refusing to replace already-present app {name!r}")


def _require_pushed(session: requests.Session, root: str, name: str) -> Mapping[str, Any]:
    item = _app(session, root, name)
    if item is None or item.get("present") is not True or item.get("origin") != "pushed":
        raise CanaryError(f"{name!r} is not a confirmed pushed app: {item!r}")
    return item


def _require_absent_after_delete(session: requests.Session, root: str, name: str) -> None:
    item = _app(session, root, name)
    if item is not None and item.get("present") is True:
        raise CanaryError(f"{name!r} remained present after safe deletion")


def _wait_current_app(
    session: requests.Session,
    root: str,
    name: str,
    *,
    timeout_seconds: float = 12.0,
) -> Mapping[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: Mapping[str, Any] = {}
    while time.monotonic() < deadline:
        response = session.get(f"{root}/api/v1/device", timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        body = response.json()
        if isinstance(body, Mapping):
            last = body
            if body.get("currentApp") == name:
                return body
        time.sleep(0.5)
    raise CanaryError(f"panel did not visibly activate {name!r}; last device state {dict(last)!r}")


def _screen_hash(session: requests.Session, root: str) -> str:
    response = session.get(
        f"{root}/api/v1/display/screen",
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, Mapping) or not isinstance(body.get("pixels"), list):
        raise CanaryError("framebuffer response has an unexpected shape")
    encoded = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _live_panel_client(root: str, session: requests.Session) -> PanelClient:
    return PanelClient(
        root,
        session=session,
        source="canary",
        policy=PanelPolicy(
            request_timeout_seconds=REQUEST_TIMEOUT_SECONDS,
            minimum_mutation_interval_seconds=2.0,
            activation_cooldown_seconds=10.0,
        ),
    )


def _network_sample(latency_ms: float) -> sentinel.ProbeSample:
    return sentinel.ProbeSample(
        router_ok=True,
        wan_ok=True,
        dns_ok=True,
        login_required=False,
        latency_ms=latency_ms,
    )


def run_network_canary(
    *,
    panel: PanelClient,
    session: requests.Session,
    root: str,
    clock: CanaryClock,
) -> dict[str, Any]:
    _require_absent(session, root, NETWORK_APP)
    slow = _network_sample(200.0)
    healthy = _network_sample(30.0)
    probe = SequenceProbe([slow] * 6 + [healthy] * 7)
    config = hub.NetworkConfig(
        alert_refresh_seconds=60.0,
        alert_lifetime_seconds=60.0,
        recovery_seconds=8.0,
    )
    controller = hub.NetworkSentinelController(
        config,
        probe,
        panel,
        clock=clock,
        logger=logging.getLogger("awtrix.release_canary.network"),
    )

    entered = None
    for _ in range(6):
        entered = controller.tick()
        clock.advance(5)
    if entered is None or entered.decision.state is not sentinel.NetworkState.SLOW:
        raise CanaryError(f"network canary did not enter SLOW: {entered!r}")
    if not entered.pushed or not entered.activated:
        raise CanaryError(f"network SLOW was not pushed and activated: {entered!r}")
    pushed = dict(_require_pushed(session, root, NETWORK_APP))
    visible = _wait_current_app(session, root, NETWORK_APP)
    slow_frame = _screen_hash(session, root)

    for _ in range(5):
        quiet = controller.tick()
        if quiet.decision.is_recovery:
            raise CanaryError("network recovered before the sixth healthy observation")
        clock.advance(5)

    cooldown = panel.seconds_until_activation()
    if cooldown > 0:
        time.sleep(cooldown + 0.2)
    recovered = controller.tick()
    clock.advance(8)
    if not recovered.decision.is_recovery or not recovered.pushed or not recovered.activated:
        raise CanaryError(f"network BACK was not pushed and activated: {recovered!r}")
    _require_pushed(session, root, NETWORK_APP)
    _wait_current_app(session, root, NETWORK_APP)
    back_frame = _screen_hash(session, root)
    if back_frame == slow_frame:
        raise CanaryError("SLOW and BACK framebuffer evidence is identical")

    deleted = controller.tick()
    if not deleted.deleted:
        raise CanaryError(f"network canary was not safely deleted: {deleted!r}")
    _require_absent_after_delete(session, root, NETWORK_APP)
    return {
        "status": "pass",
        "state": entered.decision.state.value,
        "activationCurrentApp": visible.get("currentApp"),
        "inventory": pushed,
        "slowFrameSha256": slow_frame,
        "recoveryFrameSha256": back_frame,
        "recoveryState": recovered.decision.state.value,
        "safeDelete": True,
    }


def run_next_up_canary(
    *,
    panel: PanelClient,
    session: requests.Session,
    root: str,
    clock: CanaryClock,
    evidence_dir: Path,
    allow_animation: bool = False,
) -> dict[str, Any]:
    _require_absent(session, root, NEXT_UP_APP)
    source_path = evidence_dir / "synthetic-release-canary.ics"
    source_path.write_bytes(build_synthetic_ics(clock.wall, minutes_until=14))
    runtime = evidence_dir / "next-up-runtime"
    bindings = nextup_controller.build_calendar_sources(
        (source_path,),
        runtime_dir=runtime,
        source_clock=clock,
    )
    def payload_builder(event: Any = None, **kwargs: Any) -> dict[str, Any]:
        requested_animation = bool(kwargs.pop("animated"))
        return nextup_art.build_payload(
            event,
            animated=allow_animation and requested_animation,
            **kwargs,
        )

    controller = nextup_controller.NextUpController(
        bindings,
        panel,
        timezone=PACIFIC,
        aliases={"AWTRIX synthetic release canary": "CANARY"},
        policy=nextup_controller.NextUpPolicy(
            refresh_seconds=300.0,
            keepalive_seconds=30.0,
            lifetime_seconds=60.0,
            page_duration_ms=8_000,
        ),
        clock=clock,
        payload_builder=payload_builder,
        logger=logging.getLogger("awtrix.release_canary.next_up"),
    )

    cooldown = panel.seconds_until_activation()
    if cooldown > 0:
        time.sleep(cooldown + 0.2)
    shown = controller.tick()
    if shown.stage is not EventStage.FIFTEEN or not shown.pushed or not shown.activated:
        raise CanaryError(f"Next Up 15-minute stage did not push and activate: {shown!r}")
    pushed = dict(_require_pushed(session, root, NEXT_UP_APP))
    visible = _wait_current_app(session, root, NEXT_UP_APP, timeout_seconds=5.0)
    frame = _screen_hash(session, root)

    clock.advance(25 * 60)
    removed = controller.tick()
    if removed.stage is not EventStage.ABSENT or not removed.deleted:
        raise CanaryError(f"Next Up did not expire and delete safely: {removed!r}")
    _require_absent_after_delete(session, root, NEXT_UP_APP)
    return {
        "status": "pass",
        "stage": shown.stage.value,
        "minutes": shown.minutes_to_start,
        "activationCurrentApp": visible.get("currentApp"),
        "inventory": pushed,
        "frameSha256": frame,
        "expiredStage": removed.stage.value,
        "safeDelete": True,
        "source": "synthetic local ICS",
        "animationMode": "full-width animated" if allow_animation else "static safety",
    }


def _verify_release_state(session: requests.Session, root: str) -> dict[str, Any]:
    order_response = session.get(f"{root}/apploop.json", timeout=REQUEST_TIMEOUT_SECONDS)
    order_response.raise_for_status()
    if order_response.json() != TARGET_STATE.payload():
        raise CanaryError("rotation changed during live canary")
    settings_response = session.get(
        f"{root}/api/v1/settings", timeout=REQUEST_TIMEOUT_SECONDS
    )
    settings_response.raise_for_status()
    settings = settings_response.json()
    actual = {key: settings.get(key) for key in TARGET_SETTINGS}
    if actual != TARGET_SETTINGS:
        raise CanaryError(f"rotation settings changed during live canary: {actual!r}")
    device_response = session.get(f"{root}/api/v1/device", timeout=REQUEST_TIMEOUT_SECONDS)
    device_response.raise_for_status()
    device = device_response.json()
    if not isinstance(device, Mapping) or device.get("uid") != EXPECTED_UID:
        raise CanaryError("live canary reached the wrong panel UID")
    if device.get("scriptingRunning") is not True:
        raise CanaryError("panel scripting is not running")
    errors = [
        {"name": item.get("name"), "error": item.get("error")}
        for item in _inventory(session, root)
        if item.get("error")
    ]
    if errors:
        raise CanaryError(f"panel app errors after canary: {errors!r}")
    return {
        key: device.get(key)
        for key in (
            "uid",
            "version",
            "uptimeSeconds",
            "freeHeapBytes",
            "minFreeHeapBytes",
            "largestFreeBlockBytes",
            "scriptingRunning",
            "fps",
        )
    }


def offline_canary(now: datetime | None = None) -> dict[str, Any]:
    wall = now or datetime.now().astimezone()
    network_animated = sentinel_art.build_payload(
        sentinel.NetworkState.SLOW,
        latency_ms=200,
        loss_percent=0,
        animated=True,
        lifetime_ms=60_000,
    )
    network_static = sentinel_art.build_payload(
        sentinel.NetworkState.BACK,
        latency_ms=30,
        loss_percent=0,
        animated=False,
        lifetime_ms=8_000,
    )
    calendar = build_synthetic_ics(wall, minutes_until=14)
    events = events_from_ics(
        calendar,
        wall,
        timezone=PACIFIC,
        source_id="release-canary",
    )
    next_payload = nextup_art.build_payload(
        title="CANARY",
        minutes_until=14,
        urgency=nextup_art.Urgency.SOON,
        animated=True,
        lifetime_ms=60_000,
    )
    if len(events) != 1:
        raise CanaryError("synthetic ICS did not produce exactly one event")
    return {
        "status": "pass",
        "mode": "offline",
        "networkAnimatedBytes": len(json.dumps(network_animated).encode("utf-8")),
        "networkStaticBytes": len(json.dumps(network_static).encode("utf-8")),
        "nextUpBytes": len(json.dumps(next_payload).encode("utf-8")),
        "syntheticEvents": len(events),
    }


# A resident full-width animated GIF costs contiguous block far out of
# proportion to its file size, because the panel holds a decode buffer for it.
# Measured on this panel:
#
#     373 B / 3 frames  ->  -3,072 block   (8.2x)
#     771 B / 8 frames  ->  -4,096 block   (5.3x)
#   1,246 B / 8 frames  -> -10,240 block   (8.2x)
#
# The gate here was a flat 18,000 bytes, which is not a safety margin at all: on
# 2026-08-23 a 7,010-byte animation was pushed with 22,516 bytes free and the
# panel came back with resetReason "panic" -- a hard crash mid-push, taking the
# rotation and every pushed app with it. Sizing the requirement to the artwork
# is the only version of this check that means anything.
#
# The worst observed ratio is 10,240 / 1,246 = 8.22, and the sample is three
# points on one firmware. Since being wrong here panics the panel rather than
# returning an error, the constant is rounded well clear of the data rather
# than fitted to it.
ANIMATION_BLOCK_RATIO = 10.0


def required_block_for_animation(payload_bytes: int) -> int:
    """Contiguous block a full-width animation of this size needs."""
    return int(max(0, payload_bytes) * ANIMATION_BLOCK_RATIO)


def next_up_animated_bytes() -> int:
    """Size of the full-width Next Up payload the live canary would push."""
    payload = nextup_art.build_payload(
        title="CANARY",
        minutes_until=14,
        urgency=nextup_art.Urgency.SOON,
        animated=True,
        lifetime_ms=60_000,
    )
    return len(json.dumps(payload).encode("utf-8"))


def run_live(
    *,
    base_url: str,
    output_root: Path,
    run_network: bool = True,
    run_next_up: bool = True,
    allow_next_up_animation: bool = False,
) -> Path:
    root = base_url.rstrip("/")
    wall = datetime.now().astimezone()
    if is_quiet_time(wall):
        raise CanaryError("live activations are suppressed during 21:30-07:00 quiet hours")
    output_root.mkdir(parents=True, exist_ok=True)
    destination = output_root / f"canary-{wall.strftime('%Y%m%d-%H%M%S')}"
    destination.mkdir()
    result: dict[str, Any] = {
        "schemaVersion": 1,
        "mode": "live",
        "status": "running",
        "startedAt": wall.isoformat(),
        "panel": root,
    }
    result_path = destination / "result.json"
    _atomic_json(result_path, result)
    session = requests.Session()
    panel = _live_panel_client(root, session)
    clock = CanaryClock(wall=wall)
    touched: list[str] = []
    try:
        uptimes = verify_panel_stable(session, root, samples=3, interval_seconds=2.0)
        result["preflightUptimes"] = list(uptimes)
        result["before"] = _verify_release_state(session, root)
        if allow_next_up_animation:
            block = result["before"].get("largestFreeBlockBytes")
            required = required_block_for_animation(next_up_animated_bytes())
            if not isinstance(block, int) or block < required:
                raise CanaryError(
                    "full-width Next Up animation needs about "
                    f"{required} bytes in the largest free block; panel reported "
                    f"{block!r}. A flat 18000-byte gate was tried and is unsafe: "
                    "on 2026-08-23 a 7,010-byte animation was pushed with 22,516 "
                    "bytes free and the panel answered with an ESP32 panic, not a "
                    "refusal."
                )
        if run_network:
            touched.append(NETWORK_APP)
            result["networkSentinel"] = run_network_canary(
                panel=panel,
                session=session,
                root=root,
                clock=clock,
            )
        if run_next_up:
            touched.append(NEXT_UP_APP)
            result["nextUp"] = run_next_up_canary(
                panel=panel,
                session=session,
                root=root,
                clock=clock,
                evidence_dir=destination,
                allow_animation=allow_next_up_animation,
            )
        post = verify_panel_stable(
            session,
            root,
            samples=3,
            interval_seconds=2.0,
            minimum_uptime=uptimes[-1],
        )
        result["postflightUptimes"] = list(post)
        result["after"] = _verify_release_state(session, root)
        result["status"] = "pass"
    except Exception as error:
        result["status"] = "fail"
        result["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        cleanup: dict[str, str] = {}
        for name in reversed(touched):
            try:
                item = _app(session, root, name)
                if item is not None and item.get("present") is True:
                    if item.get("origin") != "pushed":
                        cleanup[name] = f"refused origin {item.get('origin')!r}"
                    else:
                        cleanup[name] = "deleted" if panel.delete_pushed_app(name) else "already absent"
                else:
                    cleanup[name] = "already absent"
            except Exception as cleanup_error:
                cleanup[name] = f"cleanup failed: {type(cleanup_error).__name__}"
        result["cleanup"] = cleanup
        result["endedAt"] = datetime.now().astimezone().isoformat()
        _atomic_json(result_path, result)
    return destination


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--awtrix", default=AWTRIX)
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--skip-network", action="store_true")
    parser.add_argument("--skip-next-up", action="store_true")
    parser.add_argument(
        "--allow-next-up-animation",
        action="store_true",
        help="exercise the full-width GIF only when contiguous heap is at least 18KB",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.live:
        print(json.dumps(offline_canary(), indent=2))
        return
    if args.skip_network and args.skip_next_up:
        raise SystemExit("Nothing selected: both live canaries were skipped")
    destination = run_live(
        base_url=args.awtrix,
        output_root=args.output_root or default_output_root(),
        run_network=not args.skip_network,
        run_next_up=not args.skip_next_up,
        allow_next_up_animation=args.allow_next_up_animation,
    )
    print(destination)


if __name__ == "__main__":
    main()
