"""Offline comparison of actual AQI wire payloads against a frozen baseline."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import firmware_compat
import led_redesign_preview as gallery
from weather import aqi_brief_push as aqi

ROOT = Path(__file__).resolve().parent
BASELINE = ROOT / "docs/aqi-reading/before.json"
FEATURED = (28, 72, 125, 175, 250, 350, 500)


def current_payload(value: int, animated: bool) -> dict:
    sample = aqi.AQISnapshot(datetime(2026, 10, 30, 12, tzinfo=timezone.utc), "UTC", value, 12.0, (), ())
    return firmware_compat.prepare_payload("offline://aqi-reading-review", aqi.build_payload(sample, animated=animated))


def panel(pixels: list[str], low_light: bool = False) -> Image.Image:
    image = Image.new("RGB", (32, 8))
    rgb = [tuple(bytes.fromhex(pixel)) for pixel in pixels]
    if low_light:
        rgb = [tuple(255 if channel >= 174 else 0 for channel in pixel) for pixel in rgb]
    image.putdata(rgb)
    return image.resize((384, 96), Image.Resampling.NEAREST)


def comparison(before: dict, after: dict, values=FEATURED, elapsed: int = 0,
               *, static: bool = True, low_light: bool = False) -> Image.Image:
    image = Image.new("RGB", (832, 90 + 128 * len(values)), (12, 17, 24))
    draw = ImageDraw.Draw(image)
    draw.fontmode = "1"
    font = ImageFont.load_default(size=16)
    draw.text((16, 12), "AQI BREATHING LUNGS / illustrative readings", font=font, fill=(238, 242, 252))
    draw.text((16, 43), "BEFORE", font=font, fill=(169, 185, 207))
    draw.text((432, 43), "PROPOSED - restored lungs, calm palette", font=font, fill=(165, 231, 207))
    for row, value in enumerate(values):
        y = 76 + row * 128
        draw.text((16, y), f"AQI {value}", font=font, fill=(238, 242, 252))
        for x, record in ((16, before[value]), (432, after[value])):
            data = record["static"] if static else record
            t, phase = elapsed % sum(data["durations"]), 0
            while phase < len(data["frames"]) - 1 and t >= data["durations"][phase]:
                t -= data["durations"][phase]
                phase += 1
            image.paste(panel(data["frames"][phase], low_light), (x, y + 24))
    return image


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "figures/aqi-reading")
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    frozen = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert frozen["head"] == "617689cea289310de672e53e78b65eebe706b22f"
    firmware_compat.observe_device("offline://aqi-reading-review", {"version": "1.2.2"})
    before, after, cases = {}, {}, []
    values = sorted({case["value"] for case in frozen["cases"]})
    for value in values:
        entries = {}
        for animated in (False, True):
            original = next(case["payload"] for case in frozen["cases"] if case["value"] == value and case["animated"] == animated)
            current = current_payload(value, animated)
            old_decoded, new_decoded = gallery.decode(original), gallery.decode(current)
            assert max(new_decoded["bytes"], old_decoded["bytes"]) < 2048
            assert {k: v for k, v in original.items() if k not in ("icon", "draw")} == {k: v for k, v in current.items() if k not in ("icon", "draw")}
            entries[animated] = (old_decoded, new_decoded)
            cases.append({"value": value, "animated": animated, "before_payload": original,
                          "after_payload": current, "before": old_decoded, "after": new_decoded})
        before[value] = {"id": f"aqi-{value}", "title": f"AQI {value}", "group": "Air quality",
                         **entries[True][0], "static": entries[False][0]}
        after[value] = {"id": f"aqi-{value}", "title": f"AQI {value}", "group": "Air quality",
                        **entries[True][1], "static": entries[False][1]}
    comparison(before, after).save(args.output / "lungs-comparison.png")
    comparison(before, after, low_light=True).save(args.output / "lungs-low-light.png")
    # One representative category per movie keeps every actual RGB colour in GIF's 256 entries.
    delay = 120
    period = sum(after[125]["durations"])
    frames = [comparison(before, after, (125,), t, static=False) for t in range(0, period, delay)]
    colors = sorted({color for image in frames for color in image.getdata()})
    assert len(colors) <= 256
    palette = [channel for color in colors for channel in color]
    indices = {color: index for index, color in enumerate(colors)}
    indexed = []
    for frame in frames:
        image = Image.new("P", frame.size)
        image.putpalette(palette)
        image.putdata([indices[pixel] for pixel in frame.getdata()])
        assert list(image.convert("RGB").getdata()) == list(frame.getdata())
        indexed.append(image)
    indexed[0].save(args.output / "lungs-comparison.gif", save_all=True, append_images=indexed[1:],
                    duration=delay, loop=0, disposal=1, optimize=False)
    encoded_delays, elapsed = [], 0
    with Image.open(args.output / "lungs-comparison.gif") as movie:
        for index in range(movie.n_frames):
            movie.seek(index)
            assert list(movie.convert("RGB").getdata()) == list(frames[elapsed // delay].getdata())
            encoded_delays.append(movie.info["duration"])
            elapsed += movie.info["duration"]
    assert elapsed == period
    data = {"before": list(before.values()), "after": list(after.values())}
    html = gallery.HTML.replace("App visual review", "AQI reading review")
    html = html.replace("Values stay still while the icon moves.", "The preserved lung poses breathe again in one mint/pearl palette. AQI labels remain explicit, with a neutral label and single-hue value rail.")
    (args.output / "index.html").write_text(html.replace("__DATA__", json.dumps(data).replace("</", "<\\/")), encoding="utf-8")
    manifest_cases = []
    for case in cases:
        payload_hash = lambda body: hashlib.sha256(json.dumps(body, separators=(",", ":")).encode()).hexdigest()
        frame_hashes = lambda decoded: [hashlib.sha256(bytes.fromhex("".join(frame))).hexdigest() for frame in decoded["frames"]]
        manifest_cases.append({"value": case["value"], "animated": case["animated"],
                               "before_payload_sha256": payload_hash(case["before_payload"]),
                               "after_payload": case["after_payload"], "after_payload_sha256": payload_hash(case["after_payload"]),
                               "before_frame_sha256": frame_hashes(case["before"]),
                               "after_frame_sha256": frame_hashes(case["after"]),
                               "durations_ms": case["after"]["durations"], "bytes": case["after"]["bytes"]})
    result = {"baseline_head": frozen["head"], "baseline_sha256": hashlib.sha256(BASELINE.read_bytes()).hexdigest(),
              "source_hashes": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in ("visual_system.py", "weather/aqi_brief_push.py", "weather/aqi_brief_preview.py", "aqi_reading_review.py")},
              "firmware": "1.2.2", "cases": manifest_cases, "maximum_payload_bytes": max(case["after"]["bytes"] for case in cases),
              "payload_size_delta_range": [min(c["after"]["bytes"] - c["before"]["bytes"] for c in cases), max(c["after"]["bytes"] - c["before"]["bytes"] for c in cases)], "gif_sample_value": 125, "gif_sample_ms": period,
              "gif_sample_step_ms": delay, "gif_encoded_durations_ms": encoded_delays, "gif_exact_palette_colors": len(colors),
              "limits": "Actual decoded GIF/RGB888 payloads; illustrative values. Low-light is the existing per-channel 174-threshold model, not LED gamma or physical diffusion. No live changes."}
    (args.output / "review.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("maximum_payload_bytes", "payload_size_delta_range", "gif_sample_ms", "gif_exact_palette_colors")}, indent=2))


if __name__ == "__main__":
    main()
