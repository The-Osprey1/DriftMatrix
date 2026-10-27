"""Execute both Now Playing sources offline with explicit JSON/HTTP adapters.

berry_lite does not implement imports, JSON exception blocks or HTTP lambdas.
Only those three boundaries are adapted; state handling, glyph construction,
draws, timing and rotation execute from the selected .ax source. Natives and
float32 arithmetic are models, not ESP32 firmware or a live framebuffer capture.
"""
from __future__ import annotations

import argparse
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
import re

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from led_redesign_preview import led_image
from now_playing_bridge import install_source
from weather.berry_lite import BerryObject, Program
from weather.minify_ax import minify
from weather.moon_panel_sim import Screen

ROOT = Path(__file__).resolve().parent
REFERENCE = ROOT / 'now_playing.ax'
CANDIDATE = ROOT / 'now_playing-v2.2.ax'


def adapted_source(source):
    source, imports = re.subn(r'(?m)^\s*import (?:json|string)\s*$', '', source)
    source, decoder = re.subn(
        r'var data\s*=\s*nil\s+try\s+data\s*=\s*json\.load\(body\)\s+except\s*\.\.\s+return\s+end',
        'var data = preview_json(body)', source)
    source, callback = re.subn(
        r'http\.get\(self\.url,\s*/\s*b,\s*st\s*->\s*self\.on_state\(b,\s*st\)\)',
        'preview_http_get(self)', source)
    if (imports, decoder, callback) != (2, 1, 1):
        raise ValueError('Unrecognized Now Playing native boundaries; refusing a partial source model')
    return source


class StateMap(BerryObject):
    def __init__(self, document):
        self.document = document
    def find(self, name):
        value = self.document.get(name)
        return np.float32(value) if type(value) is float else value


class StringModule(BerryObject):
    def find(self, text, part):
        return text.find(part)


class Rotation(BerryObject):
    def __init__(self):
        self.shows, self.advances = 0, 0
    def show(self):
        self.shows += 1
    def next(self):
        self.advances += 1


def decode_state(body):
    try:
        document = json.loads(body)
        return StateMap(document) if isinstance(document, dict) else None
    except (TypeError, ValueError):
        return None


def berry_type(value):
    if value is None:
        return 'nil'
    if isinstance(value, bool):
        return 'bool'
    if isinstance(value, int):
        return 'int'
    if isinstance(value, (float, np.floating)):
        return 'real'
    if isinstance(value, str):
        return 'string'
    return 'instance'


@lru_cache(maxsize=8)
def compile_page(source):
    return Program(adapted_source(source), real_type=np.float32)


class MediaPreview:
    def __init__(self, source=None):
        source = CANDIDATE.read_text(encoding='utf-8') if source is None else source
        self.now = 1000
        self.next_loop = 2000
        self.screen = Screen()
        self.rotation = Rotation()
        self.body, self.status, self.requests = None, 500, 0
        def rect(x, y, width, height, color):
            for row in range(y, y + height):
                self.screen.line(x, row, x + width - 1, row, color)
        def get(page):
            self.requests += 1
            page.on_state(self.body, self.status)
        self.page = compile_page(source).run({
            'now_ms': lambda: self.now, 'clear': self.screen.clear,
            'pixel': self.screen.pixel, 'line': self.screen.line, 'rect_fill': rect,
            'rgb': lambda r, g, b: (int(r) << 16) | (int(g) << 8) | int(b),
            'size': len, 'min': min, 'max': max,
            'clamp': lambda n, lo, hi: max(lo, min(hi, n)),
            'type': berry_type, 'map': StateMap, 'isinstance': isinstance,
            'preview_json': decode_state, 'preview_http_get': get,
            'string': StringModule(), 'rotation': self.rotation,
        })

    def receive(self, title='MIDNIGHT CITY - M83', progress=12, *,
                reduced_motion=False, identity='sample', playing=True):
        data = dict(p=playing, i=identity, t=title, g=progress, r=0)
        if reduced_motion:
            data['m'] = True
        self.body, self.status = json.dumps(data), 200
        self.page.on_state(self.body, self.status)

    def draw_at(self, stamp):
        if stamp < self.now:
            raise ValueError('Preview time must move forward')
        while self.next_loop <= stamp:
            self.now = self.next_loop
            self.page.loop()
            self.next_loop += 1000
        self.now = stamp
        self.page.draw()
        image = Image.new('RGB', (32, 8))
        image.putdata([((c >> 16) & 255, (c >> 8) & 255, c & 255) for c in self.screen.px])
        return image


def comparison(destination):
    destination.mkdir(parents=True, exist_ok=True)
    sources = [REFERENCE.read_text(encoding='utf-8'), CANDIDATE.read_text(encoding='utf-8')]
    samples = [('Short title / known progress', 'WAIT', 12, False),
               ('Long title / ordinary motion', 'MIDNIGHT CITY - M83', 18, False),
               ('Reduced motion / held whole-glyph cards', 'MIDNIGHT CITY - M83', 18, True),
               ('Unknown progress / held title', 'UNKNOWN TIMELINE', -1, True)]
    pairs = []
    for title, text, progress, calm in samples:
        row = []
        for source in sources:
            preview = MediaPreview(source)
            preview.receive(text, progress, reduced_motion=calm)
            preview.page.on_show()
            row.append(preview)
        pairs.append(row)
    font_path = Path('C:/Windows/Fonts/segoeui.ttf')
    font = ImageFont.truetype(str(font_path), 21) if font_path.exists() else ImageFont.load_default()
    frames, raw_frames = [], []
    # Draw every 25 ms, export every 150 ms; retain real intermediate draws.
    for tick in range(433):
        images = [[p.draw_at(1000 + tick * 25) if not p.rotation.advances else None
                   for p in row] for row in pairs]
        if tick % 6:
            continue
        board = Image.new('RGB', (1120, 800), (12, 17, 28))
        draw = ImageDraw.Draw(board)
        draw.text((28, 16), 'DRIFTMATRIX / NOW PLAYING 2.2 CANDIDATE', font=font, fill=(235, 242, 252))
        draw.text((28, 50), 'Current 2.1', font=font, fill=(160, 178, 198))
        draw.text((580, 50), 'Proposed 2.2 / source-executed model', font=font, fill=(163, 223, 199))
        for index, ((title, _, _, _), row) in enumerate(zip(samples, images)):
            y = 88 + index * 178
            draw.text((28, y), title, font=font, fill=(214, 224, 239))
            for x, image in zip((28, 580), row):
                if image is None:
                    draw.text((x + 100, y + 75), 'Rotation advanced', font=font, fill=(160, 178, 198))
                else:
                    board.paste(led_image(['%02x%02x%02x' % color for color in image.getdata()]), (x, y + 33))
        frames.append(board)
        raw_frames.append([[None if image is None else list(image.getdata()) for image in row] for row in images])
    frames[0].save(destination / 'review.png')
    storyboard = Image.new('RGB', (630, 800), (12, 17, 28))
    draw = ImageDraw.Draw(storyboard)
    draw.text((28, 16), 'Reduced motion / complete title across held cards', font=font, fill=(235, 242, 252))
    for row, index in enumerate((0, 17, 33, 49)):
        y = 56 + row * 180
        draw.text((28, y), f'{index * 150 / 1000:.2f}s after showing', font=font, fill=(214, 224, 239))
        pixels = raw_frames[index][2][1]
        storyboard.paste(led_image(['%02x%02x%02x' % tuple(color) for color in pixels]), (28, y + 34))
    storyboard.save(destination / 'calm-cards.png')
    paletted = [frame.convert('P', palette=Image.Palette.ADAPTIVE, colors=256) for frame in frames]
    paletted[0].save(destination / 'review.gif', save_all=True, append_images=paletted[1:],
                     duration=150, loop=0, disposal=1, optimize=True)
    report = dict(reference_sha256_utf8_lf=sha256(sources[0].encode()).hexdigest(),
                  candidate_sha256_utf8_lf=sha256(sources[1].encode()).hexdigest(),
                  baseline_revision='acd73dd0a77f383942d9c4b131eeb55ef17aad2e',
                  minified_bytes=[len(minify(s).encode()) for s in sources],
                  synthetic_state_url='http://192.0.2.40:8765/nowplaying',
                  rendered_minified_bytes=[len(install_source(s, 'http://192.0.2.40:8765/nowplaying').encode()) for s in sources],
                  model_sha256_utf8_lf={name:sha256((ROOT / name).read_text(encoding='utf-8').encode()).hexdigest()
                      for name in ('now_playing_review.py', 'weather/berry_lite.py')},
                  frame_ms=25, export_ms=150, sample_ms=10800,
                  states=[title for title, *_ in samples],
                  limitations='Float32 subset with JSON/HTTP adapters; not real Berry, device heap/FPS or physical LEDs')
    (destination / 'manifest.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    (destination / 'frames.json').write_text(json.dumps(raw_frames, separators=(',', ':')), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'figures' / 'now-playing-overhaul')
    comparison(parser.parse_args().output)
