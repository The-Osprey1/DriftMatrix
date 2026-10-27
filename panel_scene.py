"""Shared payload plumbing for the icon-plus-lane pushed scenes.

Every scene here has the same shape: an 8x8 GIF icon, the black column-8
gutter, and a 23px lane that is either a static bitmap or the firmware's own
scrolling text over a one-row rail.  Commute, Miku and friends each carry a
private copy of the GIF encoder and payload builder; the newer apps
(sky_watch, quake_watch, game_mode) share this one instead.

Pattern icons are tuples of strings, one per row, where "." is black and any
other character is looked up in a palette -- the same convention as the
weather silhouettes in visual_system.
"""

from __future__ import annotations

import base64
from io import BytesIO
import json
from typing import Any, Mapping, Sequence

from PIL import Image

from visual_system import (
    BLACK,
    FRAME_BEAT_MS,
    GUTTER_X,
    HEIGHT,
    ICON_HEIGHT,
    ICON_WIDTH,
    LANE_HEIGHT,
    LANE_WIDTH,
    LANE_X0,
    MAX_ICON_BYTES,
    MAX_PAYLOAD_BYTES,
    RGB,
    TRACK,
    WIDTH,
    Frame,
    cycle_length,
    new_frame,
    put_pixel,
)

# Native scrolling text, tuned the same as Now Playing's.
SCROLL_SPEED = 100
SCROLL_GAP = 8
SCROLL_HOLD_MS = 1_000


_HTTPS_SESSION: Any = None


def https_get(url: str, **kwargs: Any) -> Any:
    """requests.get, verified against the Windows certificate store.

    Anaconda's certifi bundle rejects the chain this PC is served for the
    public feeds (CERTIFICATE_VERIFY_FAILED) while curl and the Windows store
    accept it.  A truststore context keeps full verification and only changes
    whose roots are trusted.  It is mounted on one session rather than
    injected globally, which recursed inside urllib3.
    """
    global _HTTPS_SESSION
    if _HTTPS_SESSION is None:
        import ssl

        import requests
        from requests.adapters import HTTPAdapter

        try:
            import truststore
            context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except ImportError:
            context = None

        class _Adapter(HTTPAdapter):
            def init_poolmanager(self, *args: Any, **kw: Any) -> Any:
                if context is not None:
                    kw["ssl_context"] = context
                return super().init_poolmanager(*args, **kw)

        session = requests.Session()
        session.mount("https://", _Adapter())
        _HTTPS_SESSION = session
    return _HTTPS_SESSION.get(url, **kwargs)


def hex_color(color: RGB) -> str:
    return "#{:02X}{:02X}{:02X}".format(*color)


def pattern_frame(pattern: Sequence[str], palette: Mapping[str, RGB]) -> Frame:
    frame = new_frame()
    for y, row in enumerate(pattern[:ICON_HEIGHT]):
        for x, ch in enumerate(row[:ICON_WIDTH]):
            if ch != "." and ch in palette:
                put_pixel(frame, x, y, palette[ch])
    return frame


def encode_icon_gif(frames: Sequence[Frame], beat_ms: int = FRAME_BEAT_MS) -> bytes:
    """Encode the icon area of each frame as an 8x8 GIF on the 120 ms beat."""
    if not frames:
        raise ValueError("an icon needs at least one frame")
    if beat_ms % FRAME_BEAT_MS:
        raise ValueError(f"frame delay must be a multiple of {FRAME_BEAT_MS} ms")
    count = cycle_length(list(frames))
    images = []
    for frame in frames[:count]:
        image = Image.new("RGB", (ICON_WIDTH, ICON_HEIGHT), BLACK)
        image.putdata([frame[y][x] for y in range(ICON_HEIGHT) for x in range(ICON_WIDTH)])
        images.append(image)
    stream = BytesIO()
    if count > 1:
        images[0].save(
            stream, format="GIF", save_all=True, append_images=images[1:],
            duration=[beat_ms] * count, loop=0, disposal=1, optimize=False,
        )
    else:
        images[0].save(stream, format="GIF", optimize=True)
    data = stream.getvalue()
    if len(data) > MAX_ICON_BYTES:
        raise ValueError(f"icon is {len(data)} bytes, over the {MAX_ICON_BYTES} budget")
    return data


def rail_row(fill: int, color: RGB, track: RGB = TRACK) -> list[RGB]:
    """A 23px meter for row 7: ``fill`` pixels of colour from the left, rest track."""
    fill = max(0, min(LANE_WIDTH, int(fill)))
    return [color] * fill + [track] * (LANE_WIDTH - fill)


def _b64_pixels(pixels: Sequence[RGB]) -> str:
    return base64.b64encode(bytes(ch for px in pixels for ch in px)).decode("ascii")


def _finish(payload: dict[str, Any]) -> dict[str, Any]:
    size = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    if size >= MAX_PAYLOAD_BYTES:
        raise ValueError(f"payload is {size} bytes, over the {MAX_PAYLOAD_BYTES} budget")
    return payload


def ticker_payload(
    text: str,
    color: RGB,
    icon_gif: bytes,
    *,
    rail: Sequence[RGB] | None = None,
    duration_ms: int = 7_000,
    lifetime_ms: int = 60_000,
    revision: int = 0,
) -> dict[str, Any]:
    """Icon plus firmware-rendered text that scrolls only when it does not fit."""
    payload: dict[str, Any] = {
        "text": text,
        "textCase": "asTyped",
        "font": "small",
        "textColor": hex_color(color),
        "textCenter": True,
        "icon": base64.b64encode(icon_gif).decode("ascii"),
        "iconMode": "fixed",
        "iconOffsetX": 0,
        "scroll": {
            "mode": "loop",
            "direction": "left",
            "entry": "inline",
            "whenFits": "static",
            "speed": SCROLL_SPEED,
            "gap": SCROLL_GAP,
            # One invisible millisecond per revision makes the firmware restart
            # its scroller for new text of equal width (see now_playing_push).
            "holdMs": SCROLL_HOLD_MS + (int(revision) & 1),
        },
        "backgroundColor": "#000000",
        "durationMs": duration_ms,
        "repeat": 1,
        "lifetimeMs": lifetime_ms,
        "lifetimeExpiry": "remove",
    }
    if rail is not None:
        if len(rail) != LANE_WIDTH:
            raise ValueError(f"rail must be {LANE_WIDTH} pixels")
        payload["draw"] = [["bitmap", LANE_X0, HEIGHT - 1, LANE_WIDTH, 1, _b64_pixels(rail)]]
    return _finish(payload)


def lane_payload(
    lane: Frame,
    icon_gif: bytes,
    *,
    duration_ms: int = 7_000,
    lifetime_ms: int = 60_000,
) -> dict[str, Any]:
    """Icon plus a static 23x8 bitmap lane taken from x9..31 of ``lane``."""
    pixels = [lane[y][x] for y in range(LANE_HEIGHT) for x in range(LANE_X0, LANE_X0 + LANE_WIDTH)]
    return _finish({
        "icon": base64.b64encode(icon_gif).decode("ascii"),
        "iconMode": "fixed",
        "iconOffsetX": 0,
        "draw": [["bitmap", LANE_X0, 0, LANE_WIDTH, LANE_HEIGHT, _b64_pixels(pixels)]],
        "backgroundColor": "#000000",
        "durationMs": duration_ms,
        "lifetimeMs": lifetime_ms,
        "lifetimeExpiry": "remove",
    })


class PresenceScene:
    """Push a page while something is worth showing; remove it the moment it is not.

    ``show`` pushes when the key changes (a new plane, a new quake) or when
    ``refresh_seconds`` have passed, so the page's lifetime never lapses while
    it is still true.  ``hide`` deletes it once, only if this scene put it
    there.  Neither raises: a tick must not kill the sender.
    """

    def __init__(self, panel: Any, app_name: str, refresh_seconds: float,
                 clock: Any, logger: Any) -> None:
        self.panel = panel
        self.app_name = app_name
        self.refresh_seconds = refresh_seconds
        self.clock = clock
        self.logger = logger
        self.key: Any = None
        self.showing = False
        self.last_push_at: float | None = None
        self.revision = 0

    def _now(self) -> float:
        return getattr(self.clock, "monotonic")()

    def show(self, key: Any, build: Any, *, static_build: Any = None) -> bool:
        now = self._now()
        changed = key != self.key
        due = self.last_push_at is None or now - self.last_push_at >= self.refresh_seconds
        if not (changed or due or not self.showing):
            return False
        if changed:
            self.revision += 1
        try:
            payload = build(self.revision)
            safety = {} if static_build is None else {'static_payload': static_build(self.revision)}
            self.panel.push_app(self.app_name, payload, **safety)
        except Exception as err:  # noqa: BLE001
            self.logger.warning("Failed to push %s: %s", self.app_name, err)
            return False
        if changed:
            self.logger.info("%s now showing %s", self.app_name, key)
        self.key, self.showing, self.last_push_at = key, True, now
        return True

    def hide(self) -> bool:
        if not self.showing:
            return False
        try:
            self.panel.delete_pushed_app(self.app_name)
        except Exception as err:  # noqa: BLE001
            self.logger.warning("Failed to remove %s: %s", self.app_name, err)
            return False
        self.logger.info("%s removed", self.app_name)
        self.key, self.showing, self.last_push_at = None, False, None
        return True


class PrintPanel:
    """Dry-run stand-in for PanelClient: reports what would be sent."""

    def push_app(self, name: str, payload: Mapping[str, Any], *, static_payload=None) -> None:
        size = len(json.dumps(payload, separators=(",", ":")))
        print(f"PUSH {name} ({size} B): {payload.get('text', '<bitmap lane>')}")

    def delete_pushed_app(self, name: str) -> bool:
        print(f"DELETE {name}")
        return True


def run_scene(description: str, make_controller: Any, interval_seconds: float,
              argv: Sequence[str] | None = None) -> None:
    """Prototype CLI: one dry-run tick by default, ``--live`` to drive the panel."""
    import argparse
    import logging
    import time

    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--live", action="store_true",
                        help="push to the real panel in a loop (Ctrl+C to stop)")
    parser.add_argument("--panel", default="http://192.168.1.90")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    if not args.live:
        tick = make_controller(PrintPanel()).tick()
        print(tick)
        return

    from panel_client import PanelClient
    controller = make_controller(PanelClient(args.panel, source="scene"))
    try:
        while True:
            controller.tick()
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        controller.scene.hide()


def composite(icon: Frame, lane: Frame) -> Frame:
    """Preview helper: icon owns x0..7, column 8 is black, lane owns x9..31."""
    out = [[BLACK] * WIDTH for _ in range(HEIGHT)]
    for y in range(HEIGHT):
        for x in range(ICON_WIDTH):
            out[y][x] = icon[y][x]
        for x in range(LANE_X0, WIDTH):
            out[y][x] = lane[y][x]
        out[y][GUTTER_X] = BLACK
    return out
