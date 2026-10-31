"""Offline actual-provider-code weather review; no live requests or changes."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import json
import math
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import firmware_compat
import led_redesign_preview as gallery
from weather import weather_brief_push as weather

ROOT = Path(__file__).resolve().parent
BASELINE = ROOT / "docs/weather-conditions/before.json"
NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
NAMES = {0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 51: "Drizzle",
         56: "Freezing drizzle", 61: "Rain", 66: "Freezing rain", 71: "Snow", 95: "Thunderstorm", 999: "Unassigned code"}
FEATURED = (3, 51, 66, 999)


def snapshot(code, day, forecast=False):
    return weather.WeatherSnapshot(NOW, "UTC", 64.0, 0 if forecast else code, day, 54.0, 72.0,
                                   (NOW + timedelta(minutes=18, seconds=20),) if forecast else (),
                                   (25,) if forecast else (), (0.0,) if forecast else (), (code,) if forecast else ())


def rgb_image(pixels, low_light=False):
    image = Image.new("RGB", (32, 8))
    colors = [tuple(bytes.fromhex(value)) for value in pixels]
    if low_light:
        colors = [tuple(255 if channel >= 174 else 0 for channel in color) for color in colors]
    image.putdata(colors)
    return image.resize((384, 96), Image.Resampling.NEAREST)


def frame_at(record, elapsed, static):
    data = record["static"] if static else record
    t, phase = elapsed % sum(data["durations"]), 0
    while phase < len(data["frames"]) - 1 and t >= data["durations"][phase]:
        t -= data["durations"][phase]
        phase += 1
    return data["frames"][phase]


def board(before, after, codes, *, elapsed=0, static=True, low_light=False):
    image = Image.new("RGB", (832, 92 + 128 * len(codes)), (12, 17, 24))
    draw = ImageDraw.Draw(image); draw.fontmode = "1"; font = ImageFont.load_default(size=16)
    draw.text((16, 12), "WEATHER CONDITION ROUTING / illustrative readings", font=font, fill=(238, 242, 252))
    draw.text((16, 43), "BEFORE", font=font, fill=(169, 185, 207))
    draw.text((432, 43), "PROPOSED", font=font, fill=(165, 231, 207))
    for row, code in enumerate(codes):
        y = 76 + row * 128
        draw.text((16, y), f"{code}: {NAMES[code]}", font=font, fill=(238, 242, 252))
        for x, data in ((16, before[code]), (432, after[code])):
            image.paste(rgb_image(frame_at(data, elapsed, static), low_light), (x, y + 24))
    return image


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "figures/weather-conditions")
    args = parser.parse_args(argv); args.output.mkdir(parents=True, exist_ok=True)
    frozen = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert frozen["head"] == "18c4ae6708fe71815ac22c25d4e21a35ca48b50a"
    endpoint = "offline://weather-condition-review"; firmware_compat.observe_device(endpoint, {"version": "1.2.2"})
    cases, before, after, manifest = [], [], [], []
    for mode, source in (("Current", frozen["cases"]), ("Forecast", frozen["forecast_cases"])):
        for old in source:
            scene = weather.choose_scene(snapshot(old["code"], old["day"], mode == "Forecast"), now=NOW)
            payload = firmware_compat.prepare_payload(endpoint, weather.build_payload(scene, old["animated"]))
            a, b = gallery.decode(old["payload"]), gallery.decode(payload)
            assert b["bytes"] < 2048
            assert {k: v for k, v in old["payload"].items() if k not in ("icon", "draw")} == {k: v for k, v in payload.items() if k not in ("icon", "draw")}
            cases.append({"mode": mode, "code": old["code"], "day": old["day"], "animated": old["animated"], "before": a, "after": b})
            wire_hash = lambda body: hashlib.sha256(json.dumps(body, separators=(",", ":")).encode()).hexdigest()
            manifest.append({"mode": mode, "code": old["code"], "day": old["day"], "animated": old["animated"],
                             "before_kind": old["scene"]["icon_kind"], "after_kind": scene.icon_kind,
                             "before_text": old["scene"]["text"], "after_text": scene.text,
                             "before_wire_sha256": wire_hash(old["payload"]), "after_payload": payload,
                             "after_wire_sha256": wire_hash(payload), "bytes": b["bytes"], "durations_ms": b["durations"]})
        representatives = NAMES if mode == "Current" else {code: NAMES[code] for code in (51, 56, 61, 66, 71, 95)}
        for code, title in representatives.items():
            for day in (False, True):
                pair = {animated: next(c for c in cases if c["mode"] == mode and c["code"] == code and c["day"] == day and c["animated"] == animated) for animated in (False, True)}
                common = {"id": f"{mode}-{code}-{day}", "title": f"{code}: {title}", "group": mode + (" / day" if day else " / night")}
                before.append({**common, **pair[True]["before"], "static": pair[False]["before"]})
                after.append({**common, **pair[True]["after"], "static": pair[False]["after"]})
    still_before = {code: next(c for c in before if c["id"] == f"Current-{code}-True") for code in NAMES}
    still_after = {code: next(c for c in after if c["id"] == f"Current-{code}-True") for code in NAMES}
    board(still_before, still_after, (3, 51, 56, 66, 999)).save(args.output / "condition-comparison.png")
    board(still_before, still_after, (3, 51, 56, 66, 999), low_light=True).save(args.output / "condition-low-light.png")
    period = math.lcm(*(sum(data[code]["durations"]) for data in (still_before, still_after) for code in FEATURED))
    frames = [board(still_before, still_after, FEATURED, elapsed=t, static=False) for t in range(0, period, 120)]
    colors = sorted({rgb for frame in frames for rgb in frame.getdata()}); assert len(colors) <= 256
    indices = {rgb: i for i, rgb in enumerate(colors)}; palette = [channel for rgb in colors for channel in rgb]; indexed = []
    for frame in frames:
        image = Image.new("P", frame.size); image.putpalette(palette); image.putdata([indices[p] for p in frame.getdata()]); indexed.append(image)
    indexed[0].save(args.output / "condition-comparison.gif", save_all=True, append_images=indexed[1:], duration=120, loop=0, disposal=1, optimize=False)
    elapsed = 0
    with Image.open(args.output / "condition-comparison.gif") as movie:
        for index in range(movie.n_frames):
            movie.seek(index)
            assert list(movie.convert("RGB").getdata()) == list(frames[elapsed // 120].getdata())
            elapsed += movie.info["duration"]
    assert elapsed == period
    data = {"before": before, "after": after}
    html = gallery.HTML.replace("App visual review", "Weather condition review")
    html = html.replace("Values stay still while the icon moves.", "Provider-code routing selects the condition. Ice-shaped drops denote freezing rain/drizzle; a question mark denotes an unassigned code. Forecast cases retain their precise countdown.")
    html = html.replace("The deployed weather and air quality services currently use static mode. Animated", "The weather service still uses static mode during this review. Animated")
    (args.output / "index.html").write_text(html.replace("__DATA__", json.dumps(data).replace("</", "<\\/")), encoding="utf-8")
    result = {"baseline_head": frozen["head"], "provider_source": frozen["provider_source"],
              "baseline_sha256": hashlib.sha256(BASELINE.read_bytes()).hexdigest(),
              "source_hashes": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in ("weather/weather_brief_push.py", "weather/weather_icons.py", "weather_condition_review.py")},
              "prepared_cases": len(cases), "gallery_cases": len(after), "maximum_payload_bytes": max(c["bytes"] for c in manifest),
              "gif_period_ms": period, "gif_palette_colors": len(colors), "cases": manifest,
              "limits": "Synthetic provider codes and values; actual decoded GIF/RGB888 wire payloads. Low-light is a per-channel 174 threshold, not gamma/diffusion. No live changes."}
    (args.output / "review.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("prepared_cases", "gallery_cases", "maximum_payload_bytes", "gif_period_ms", "gif_palette_colors")}, indent=2))


if __name__ == "__main__":
    main()
