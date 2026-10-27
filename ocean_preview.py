"""Execute Ocean's actual Berry source offline; never contacts the clock.

This uses the existing Berry subset compiler with float32 semantics. It is not
a firmware VM or a measurement of ESP32 heap/FPS. It provides reproducible
source-level pixel and lifecycle evidence before any physical deployment.
"""
from __future__ import annotations

import argparse
from functools import lru_cache
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from weather.berry_lite import BerryObject, Program
from weather.moon_panel_sim import R, Screen
from weather.minify_ax import minify

ROOT = Path(__file__).resolve().parent
INSTALLED = ROOT / "ocean.ax"
CANDIDATE = ROOT / "ocean-v3.ax"


class Shared(BerryObject):
    """Trusted native-module test double using the compiler's method protocol."""
    def __init__(self):
        self.phase = None
        self.age_ms = 0

    def get(self, key):
        assert key == "sunset.phase"
        return self.phase

    def age(self, key):
        assert key == "sunset.phase"
        return self.age_ms


@lru_cache(maxsize=8)
def program(source, fill):
    return Program(source, real_type=R, resize_fill=fill)


class OceanPreview:
    def __init__(self, source=None, *, wall_ms=0, fill=0xA5):
        self.source = CANDIDATE.read_text(encoding="utf-8") if source is None else source
        self.wall_ms = wall_ms
        self.hour = 12
        self.shared = Shared()
        self.screen = Screen()
        self.page = program(self.source, fill).run({
            "now_ms": lambda: self.wall_ms,
            "hour": lambda: self.hour,
            "shared": self.shared,
            "pixel": self.screen.pixel,
            "line": self.screen.line,
        })
        self.visible = False
        self.next_loop = wall_ms

    def show(self):
        self.visible = True
        if hasattr(self.page, "on_show"):
            self.page.on_show()
        self.page.loop()

    def hide(self):
        self.visible = False
        if hasattr(self.page, "on_hide"):
            self.page.on_hide()

    def advance(self, milliseconds, *, frame_ms=40):
        """Run actual draws and one-second loops; time can pass while hidden."""
        if milliseconds < 0 or frame_ms <= 0:
            raise ValueError("Use a nonnegative interval and positive frame duration")
        end = self.wall_ms + milliseconds
        while self.wall_ms < end:
            self.wall_ms = min(end, self.wall_ms + frame_ms)
            if self.visible:
                self.screen.clear()
                self.page.draw()
            if self.wall_ms >= self.next_loop:
                self.page.loop()
                self.next_loop = self.wall_ms + 1000
        return self.screen.frame()

    def image(self):
        image = Image.new("RGB", (32, 8))
        image.putdata([((c >> 16) & 255, (c >> 8) & 255, c & 255) for c in self.screen.px])
        return image

    def cache_storage(self):
        result = {}
        for name in ("sc", "sn4"):
            value = getattr(self.page, name)
            result[name] = {"kind": "bytes" if isinstance(value, bytearray) else "list",
                            "entries": len(value)}
        return result


def export(output):
    """Side-by-side real source frames from the start of a normal visit."""
    output.mkdir(parents=True, exist_ok=True)
    old = OceanPreview(INSTALLED.read_text(encoding="utf-8"))
    new = OceanPreview()
    old.show(); new.show()
    font_path = Path("C:/Windows/Fonts/segoeui.ttf")
    font = ImageFont.truetype(str(font_path), 18) if font_path.exists() else ImageFont.load_default()
    frames, hashes = [], []
    # Sample all 30 seconds every 120 ms, simulating all intermediate 40 ms
    # draws. A GIF frame is a sampled source frame, not a device FPS claim.
    for tick in range(250):
        old.advance(120); new.advance(120)
        panel = Image.new("RGB", (840, 300), (12, 16, 21))
        draw = ImageDraw.Draw(panel)
        draw.text((20, 14), "OCEAN / FIRST 30 SECONDS ON SCREEN", font=font, fill=(235, 240, 247))
        for column, (runner, title) in enumerate(((old, "Current"), (new, "Proposed 3.0"))):
            x = 20 + column * 410
            draw.text((x, 57), title, font=font, fill=(171, 187, 205))
            panel.paste(runner.image().resize((384, 96), Image.Resampling.NEAREST), (x, 90))
        draw.text((20, 207), f"Visit: {(tick + 1) * .12:.1f}s / 30s", font=font, fill=(165, 231, 207))
        draw.text((20, 242), "Actual Berry source · offline model · no device writes", font=font, fill=(171, 187, 205))
        frames.append(panel)
        hashes.append(hashlib.sha256(new.image().tobytes()).hexdigest())
    frames[166].save(output / "ocean-visit.png")  # 20-second encounter review still.
    frames[0].save(output / "ocean-visit.gif", save_all=True, append_images=frames[1:],
                   duration=120, loop=0, disposal=1)
    candidate_text = CANDIDATE.read_text(encoding="utf-8")
    reference_text = INSTALLED.read_text(encoding="utf-8")
    report = {"candidate_text_sha256": hashlib.sha256(candidate_text.encode()).hexdigest(),
              "candidate_upload_sha256": hashlib.sha256(minify(candidate_text).encode()).hexdigest(),
              "candidate_upload_bytes": len(minify(candidate_text).encode()),
              "reference_text_sha256": hashlib.sha256(reference_text.encode()).hexdigest(),
              "cache_before": old.cache_storage(), "cache_after": new.cache_storage(),
              "sampled_frames": len(frames), "frame_hashes": hashes,
              "physical_heap_and_fps": "not measured", "physical_deployment": False}
    (output / "ocean-review.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "figures" / "ocean-overhaul")
    args = parser.parse_args(argv)
    report = export(args.output)
    print(json.dumps({k: v for k, v in report.items() if k != "frame_hashes"}, indent=2))


if __name__ == "__main__":
    main()
