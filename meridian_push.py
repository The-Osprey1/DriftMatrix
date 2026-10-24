r"""Push the AWTRIX ``Meridian`` day ribbon to the panel.

Meridian shows one whole day across the panel's 32 columns: the sun's real arc,
the sky colour its altitude implies, and a shaft of light marking now. The
artwork and the solar geometry live in :mod:`meridian_preview`; this file only
decides when to write and talks to the panel.

Everything is computed locally, so this keeps working with the internet down.

SETUP
    C:\Users\devyn\anaconda3\python.exe -m pip install requests

RUN
    C:\Users\devyn\anaconda3\python.exe meridian_push.py
    C:\Users\devyn\anaconda3\python.exe meridian_push.py --once --dry-run

RUN IT AUTOMATICALLY AT LOGIN
    Task Scheduler -> Create Task
      Actions : C:\Users\devyn\anaconda3\pythonw.exe "<full path to this file>"
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime
import json
import sys
import time

try:
    import requests
except ImportError:  # pragma: no cover - dependency guard
    sys.exit(
        r"Missing dependencies. Run:  C:\Users\devyn\anaconda3\python.exe -m pip install requests"
    )

import meridian_preview as art
import panel_gate

# ---------------------------------------------------------------- config
AWTRIX = "http://192.168.1.90"
APP_NAME = "meridian"
GATE_SOURCE = "meridian"
URL = f"{AWTRIX}/api/v1/apps/pushed/{APP_NAME}"
DELETE_URL = f"{AWTRIX}/api/v1/apps/{APP_NAME}"
DEVICE_URL = f"{AWTRIX}/api/v1/device"
REQUEST_TIMEOUT = 5

# One column is 45 minutes, so the shaft only *moves* twice an hour. Pushing
# every 30s would be pure noise; this is slow enough to be polite and fast
# enough that the shaft is never visibly stale.
PUSH_INTERVAL = 120
PAGE_DURATION_MS = 8_000
# Comfortably longer than the push interval, so a couple of failed writes in a
# row cannot make the page blink out, while a dead sender still clears within
# a few minutes rather than leaving yesterday's ribbon up forever.
LIFETIME_MS = 600_000

# The panel is dark during Nightkeeper's blackout, so there is nothing to draw
# and no reason to keep writing. Meridian never touches power or brightness.
QUIET_START_MINUTE = art.BLACKOUT_MINUTE
QUIET_END_MINUTE = 7 * 60

# The whole page is one static bitmap. Measured on the panel: a full-width 32x8
# static draw costs 0 bytes of largest-contiguous-free-block, on push and while
# rendering. That is what makes a full-bleed design affordable here -- it is
# animated full-width GIFs that are expensive, because those need a decode
# buffer. Do not turn this into an animation without re-measuring.
MAX_PAYLOAD_BYTES = 4_096
# -----------------------------------------------------------------------


def is_quiet_time(moment: datetime) -> bool:
    """Whether the clock says the panel should be dark."""
    minute = moment.hour * 60 + moment.minute
    return minute >= QUIET_START_MINUTE or minute < QUIET_END_MINUTE


def panel_is_dark(session=requests) -> bool | None:
    """Whether the panel is actually switched off, or None if it will not say.

    Gating on the clock alone was wrong. Nightkeeper honours a manual override
    until the next scheduled transition, so someone can switch the panel on at
    midnight and it stays on -- and Meridian would keep hiding from a lit panel
    for the rest of the night. What matters is whether there is anything to
    draw on, which is a question the panel can answer directly.
    """
    try:
        response = session.get(DEVICE_URL, timeout=REQUEST_TIMEOUT)
        if not 200 <= response.status_code < 300:
            return None
        power = response.json().get("matrixPower")
    except (requests.RequestException, ValueError, AttributeError):
        return None
    return None if power is None else not power


def should_stay_quiet(moment: datetime, session=requests) -> bool:
    """Quiet only when the panel is genuinely dark.

    Falls back to the clock when the panel cannot be reached, so a network
    blip during the blackout does not start pushing to a switched-off panel.
    """
    dark = panel_is_dark(session)
    if dark is None:
        return is_quiet_time(moment)
    return dark


def build_payload(now: datetime) -> dict:
    """One pushed page carrying the whole ribbon as a single bitmap."""
    frame = art.render(now)
    lane = base64.b64encode(art.frame_bytes(frame)).decode("ascii")
    payload = {
        "draw": [["bitmap", 0, 0, art.WIDTH, art.HEIGHT, lane]],
        "backgroundColor": "#000000",
        "durationMs": PAGE_DURATION_MS,
        "lifetimeMs": LIFETIME_MS,
        "lifetimeExpiry": "remove",
    }
    size = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    if size > MAX_PAYLOAD_BYTES:
        raise ValueError(f"Meridian page is {size} bytes; limit is {MAX_PAYLOAD_BYTES}")
    return payload


def sync_panel(now: datetime, published: bool) -> tuple[bool, str | None]:
    """Make the panel match the current moment; return (published, error)."""
    try:
        if should_stay_quiet(now):
            # Quiet is a real state: remove the page rather than pushing to a
            # panel that is switched off.
            if published:
                with panel_gate.mutation(GATE_SOURCE, "delete", APP_NAME) as gate:
                    response = requests.delete(DELETE_URL, timeout=REQUEST_TIMEOUT)
                    gate.observe(response)
                if not (200 <= response.status_code < 300 or response.status_code == 404):
                    return published, f"cleanup HTTP {response.status_code}"
            return False, None

        try:
            page = build_payload(now)
        except ValueError as error:
            # The size guard raises, and this loop runs under pythonw with
            # nowhere for a traceback to go. Report it as a failed cycle rather
            # than letting the sender disappear without a word.
            return published, f"page rejected locally: {error}"

        with panel_gate.mutation(GATE_SOURCE, "push", APP_NAME, body=page) as gate:
            response = requests.put(URL, json=page, timeout=REQUEST_TIMEOUT)
            gate.observe(response)
        if 200 <= response.status_code < 300:
            return True, None
        return published, f"push HTTP {response.status_code}: {response.text[:120]}"
    except requests.RequestException as error:
        return published, f"unreachable: {type(error).__name__}"


def should_log(misses: int) -> bool:
    return misses in (1, 5) or misses % 30 == 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Push the Meridian day ribbon to AWTRIX.")
    parser.add_argument("--once", action="store_true", help="one cycle, then exit")
    parser.add_argument("--dry-run", action="store_true", help="render but never touch the panel")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    published = False
    misses = 0

    if args.dry_run:
        now = datetime.now()
        payload = build_payload(now)
        size = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        print(art.describe(now))
        print(f"Dry run: {size} byte page, quiet={is_quiet_time(now)}; panel untouched")
        return

    print(f"Meridian -> {URL} every {PUSH_INTERVAL}s. Ctrl+C to stop.")
    while True:
        now = datetime.now()
        try:
            published, error = sync_panel(now, published)
        except Exception as unexpected:  # noqa: BLE001 - see below
            # A long-running sender under pythonw has no console, so anything
            # that escapes here takes the app off the panel silently and stays
            # off until someone notices days later. Surviving a bad cycle and
            # saying so is strictly better than vanishing; the throttled log
            # keeps a persistent fault visible without filling the disk.
            error = f"unexpected {type(unexpected).__name__}: {unexpected}"

        if error is None:
            misses = 0
        else:
            misses += 1
            if should_log(misses):
                print(f"  {error} ({misses} in a row)")
        if args.once:
            return
        time.sleep(PUSH_INTERVAL)


if __name__ == "__main__":
    # pythonw has no stdout under Task Scheduler; without this every print()
    # in this file is discarded. See sender_logging for the measurement.
    import sender_logging

    sender_logging.configure("meridian")
    try:
        main()
    except KeyboardInterrupt:
        print(f"\nStopped. The ribbon expires within {LIFETIME_MS // 60_000} minutes.")
