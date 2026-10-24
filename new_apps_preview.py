"""Offline 32x8 preview sheet for the prototype apps.

Never touches the panel.  Each row is one state of one app: every icon frame
composited with its lane at true 32x8, enlarged with nearest-neighbour
sampling, captioned with the full scrolling text.  Ticker lanes are drawn in
the house 3x5 font at scroll start -- the firmware's "small" font is close but
not identical -- so judge layout and colour here, and exact glyphs on the panel.

Where the network is up, the Sky Watch and Quake Watch rows use the real feed
as well as synthetic examples.

    python new_apps_preview.py --out %TEMP%\\new_apps
"""

from __future__ import annotations

import argparse
import base64
from io import BytesIO
from pathlib import Path
import time

from PIL import Image, ImageDraw

import game_mode as gm
import iss_watch as iw
import notify_mirror as nm
import quake_watch as qw
import sky_watch as sw
from panel_scene import composite
from visual_system import HEIGHT, LANE_WIDTH, LANE_X0, WIDTH, Frame, draw_text, new_frame, put_pixel

SCALE = 12
GAP = 6
CAPTION_W = 470
ONE_BIT = 174


def icon_frames_from(payload: dict) -> list[Frame]:
    image = Image.open(BytesIO(base64.b64decode(payload["icon"])))
    frames = []
    for i in range(getattr(image, "n_frames", 1)):
        image.seek(i)
        rgb = image.convert("RGB")
        frame = new_frame()
        for y in range(8):
            for x in range(8):
                frame[y][x] = rgb.getpixel((x, y))
        frames.append(frame)
    return frames


def lane_from(payload: dict) -> Frame:
    lane = new_frame()
    for op in payload.get("draw", []):
        _, x0, y0, w, h, data = op
        raw = base64.b64decode(data)
        for i in range(w * h):
            put_pixel(lane, x0 + i % w, y0 + i // w, tuple(raw[i * 3:i * 3 + 3]))
    if "text" in payload:
        hexc = payload["textColor"].lstrip("#")
        color = tuple(int(hexc[i:i + 2], 16) for i in (0, 2, 4))
        text = payload["text"]
        clipped = new_frame()
        draw_text(clipped, text, LANE_X0 + 1, 1, color)
        for y in range(1, 6):
            for x in range(LANE_X0, WIDTH):
                if clipped[y][x] != (0, 0, 0):
                    lane[y][x] = clipped[y][x]
    return lane


def frame_image(frame: Frame, one_bit: bool = False) -> Image.Image:
    img = Image.new("RGB", (WIDTH * SCALE, HEIGHT * SCALE), (18, 18, 18))
    d = ImageDraw.Draw(img)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            px = frame[y][x]
            if one_bit:
                px = tuple(255 if c >= ONE_BIT else 0 for c in px)
            d.rectangle([x * SCALE + 1, y * SCALE + 1, (x + 1) * SCALE - 2, (y + 1) * SCALE - 2], fill=px)
    return img


def row_image(title: str, payload: dict, one_bit: bool = False) -> Image.Image:
    frames = [composite(icon, lane_from(payload)) for icon in icon_frames_from(payload)]
    shown = frames[:4]
    w = CAPTION_W + len(shown) * (WIDTH * SCALE + GAP)
    img = Image.new("RGB", (w, HEIGHT * SCALE + 30), (0, 0, 0))
    d = ImageDraw.Draw(img)
    d.text((6, 8), title, fill=(230, 230, 230))
    text = payload.get("text", "(static bitmap lane)")
    d.text((6, 26), text[:70], fill=(150, 200, 255))
    d.text((6, 44), f"{len(frames)} icon frame(s)", fill=(120, 120, 120))
    for i, f in enumerate(shown):
        img.paste(frame_image(f, one_bit=one_bit), (CAPTION_W + i * (WIDTH * SCALE + GAP), 0))
    return img



def stack(rows: list[Image.Image]) -> Image.Image:
    w = max(r.width for r in rows)
    h = sum(r.height for r in rows) + GAP * len(rows)
    out = Image.new("RGB", (w, h), (0, 0, 0))
    y = 0
    for r in rows:
        out.paste(r, (0, y))
        y += r.height + GAP
    return out


def sky_rows() -> list[tuple[str, dict]]:
    def plane(cs, t, cat, alt, track, dist):
        return sw.Aircraft("x", cs, t, cat, alt, track, dist, 0.0)
    rows = [
        ("Sky: airliner heading W, close", sw.build_payload(plane("SWA1234", "B738", "A3", 4200, 265, 0.4))),
        ("Sky: airliner heading NE, edge", sw.build_payload(plane("MXY1847", "BCS3", "A3", 5100, 45, 2.2))),
        ("Sky: light plane heading S", sw.build_payload(plane("N52740", "C172", "A1", 1800, 180, 1.1))),
        ("Sky: helicopter", sw.build_payload(plane("N341GW", "R44", "A7", 1100, 180, 0.8))),
    ]
    try:
        live = sw.pick_overhead(sw.fetch_feed(sw.SkyWatchConfig(radius_nm=10)), 10, 15_000)
        if live:
            rows.append((f"Sky LIVE: nearest within 10 nm ({live.distance_nm:.1f} nm)", sw.build_payload(live)))
    except Exception as err:  # noqa: BLE001
        print("sky feed unavailable:", err)
    return rows


def quake_rows() -> list[tuple[str, dict]]:
    now = int(time.time() * 1000)
    def quake(mag, lat, lon, place, mins):
        return qw.Quake("x", mag, place, now - mins * 60_000, lat, lon, 8.0)
    rows = [
        ("Quake: M2.6 small", qw.build_payload(quake(2.6, 33.95, -117.40, "10 km NE of Corona, CA", 4))),
        ("Quake: M3.8 moderate", qw.build_payload(quake(3.8, 34.20, -116.90, "8 km S of Big Bear City, CA", 25))),
        ("Quake: M5.2 strong", qw.build_payload(quake(5.2, 33.50, -116.50, "15 km N of Anza, CA", 70))),
    ]
    try:
        feed = qw.fetch_feed(qw.QuakeWatchConfig(min_magnitude=1.5))
        if feed:
            latest = max(feed, key=lambda q: q.time_ms)
            rows.append((f"Quake LIVE: latest in 150 km (M{latest.magnitude:.1f})", qw.build_payload(latest)))
    except Exception as err:  # noqa: BLE001
        print("quake feed unavailable:", err)
    return rows


def game_rows() -> list[tuple[str, dict]]:
    rows = [
        ("Game: cool, light load, 10 min", gm.build_payload(gm.GpuReading(58, 35), 10)),
        ("Game: warm, full load, 55 min", gm.build_payload(gm.GpuReading(76, 99), 55)),
        ("Game: hot, 2h+", gm.build_payload(gm.GpuReading(87, 97), 130)),
    ]
    live = gm.read_gpu()
    if live:
        rows.append((f"Game LIVE GPU now ({live.temperature_c}C, {live.utilization_pct}%)", gm.build_payload(live, 0)))
    return rows


def iss_rows() -> list[tuple[str, dict]]:
    return [
        ("ISS: just risen in the SW, 12 deg", iw.build_payload(iw.LookAngle(225, 12))),
        ("ISS: climbing W, 47 deg", iw.build_payload(iw.LookAngle(280, 47))),
        ("ISS: nearly overhead, 86 deg", iw.build_payload(iw.LookAngle(10, 86))),
    ]


def notify_rows() -> list[tuple[str, dict]]:
    rules = {r.label: r for r in nm.DEFAULT_RULES}
    return [
        ("Notify: one new mail", nm.build_payload(rules["MAIL"], 1)),
        ("Notify: three Teams messages", nm.build_payload(rules["TEAMS"], 3)),
        ("Notify: a chat, five or more waiting", nm.build_payload(rules["CHAT"], 7)),
        ("Notify: other app (bell)", nm.build_payload(nm.AppRule("x", "NEW", "bell"), 1)),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    groups = {"sky_watch": sky_rows(), "quake_watch": quake_rows(),
              "game_mode": game_rows(), "iss_watch": iss_rows(),
              "notify_mirror": notify_rows()}
    for name, rows in groups.items():
        sheet = stack([row_image(title, payload) for title, payload in rows])
        path = args.out / f"{name}.png"
        sheet.save(path)
        print(path)
        sheet_1bit = stack([row_image(title, payload, one_bit=True) for title, payload in rows])
        path_1bit = args.out / f"{name}_1bit.png"
        sheet_1bit.save(path_1bit)
        print(path_1bit)



if __name__ == "__main__":
    main()
