"""Export the rejected proposal against revised actual payloads/Berry sources.

Offline only. Reuses the documented float32 subset models and their explicit
font/HTTP/JSON boundaries; this is not ESP32 or physical-framebuffer evidence.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import led_redesign_preview as host
from moon_review import MoonPreview
from now_playing_review import MediaPreview
from ocean_preview import OceanPreview
from weather.minify_ax import minify

ROOT = Path(__file__).resolve().parent
SOURCES = {
    'moon': ('weather/moon-panel-v2.2.ax', 'weather/moon-panel-v2.3.ax'),
    'ocean': ('ocean-v3.ax', 'ocean-v3.1.ax'),
    'media': ('now_playing-v2.2.ax', 'now_playing-v2.3.ax'),
}


def board(titles, pairs, stamp, subtitle):
    canvas = Image.new('RGB', (1120, 100 + len(pairs) * 174), (12, 17, 27))
    path = Path('C:/Windows/Fonts/segoeui.ttf')
    font = ImageFont.truetype(str(path), 19) if path.exists() else ImageFont.load_default()
    draw = ImageDraw.Draw(canvas)
    draw.text((24, 12), subtitle, font=font, fill='#ecf1fa')
    draw.text((24, 44), 'Previous proposal / revisions requested', font=font, fill='#aab8ca')
    draw.text((580, 44), f'Revised / offline preview / {stamp / 1000:.2f}s', font=font, fill='#a5e6ce')
    for index, (title, pair) in enumerate(zip(titles, pairs)):
        y = 82 + index * 174
        draw.text((24, y), title, font=font, fill='#cbd7e7')
        for x, frame in zip((24, 580), pair):
            if frame is None:
                draw.text((x + 110, y + 74), 'Visit completed', font=font, fill='#aab8ca')
                continue
            pixels = ['%02x%02x%02x' % color for color in frame.getdata()]
            canvas.paste(host.led_image(pixels), (x, y + 32))
    return canvas


def save(frames, folder, name, delay, *, still=0):
    frames[still].save(folder / f'{name}.png')
    frames[0].save(folder / f'{name}.gif', save_all=True, append_images=frames[1:],
                   duration=delay, loop=0, disposal=1)


def payload_image(scene, stamp):
    position = stamp % sum(scene['durations'])
    for frame, delay in zip(scene['frames'], scene['durations']):
        if position < delay:
            image = Image.new('RGB', (32, 8))
            image.putdata([tuple(bytes.fromhex(value)) for value in frame])
            return image
        position -= delay
    raise AssertionError('Invalid captured frame timing')


def export(folder, baseline):
    folder.mkdir(parents=True, exist_ok=True)
    before = json.loads(baseline.read_text(encoding='utf-8'))
    after = host.capture()
    old, new = ({scene['id']: scene for scene in group} for group in (before, after))
    keys = ['overcast', 'rain', 'companion-idle', 'companion-rest', 'gpu']
    frames = []
    for stamp in range(0, 5760, 120):
        pairs = [(payload_image(old[key], stamp), payload_image(new[key], stamp)) for key in keys]
        frames.append(board([new[key]['title'] for key in keys], pairs, stamp, 'ART / SHADED OBJECTS AND CHARACTER MOTION'))
    save(frames, folder, 'host', 120, still=12)
    (folder / 'after.json').write_text(json.dumps(after), encoding='utf-8')

    text = {key: [(ROOT / path).read_text(encoding='utf-8') for path in paths] for key, paths in SOURCES.items()}
    oceans = [OceanPreview(source) for source in text['ocean']]
    for runner in oceans:
        runner.show()
    frames = []
    for stamp in range(120, 30001, 120):
        for runner in oceans:
            runner.advance(120)
        frames.append(board(['Layered reef / first visible manta visit'],
                            [[runner.image() for runner in oceans]], stamp, 'OCEAN / 30-SECOND SOURCE-EXECUTED VISIT'))
    save(frames, folder, 'ocean', 120, still=166)

    moons = []
    for illumination, waxing in ((100, True), (12, True), (76, False)):
        pair = [MoonPreview(source) for source in text['moon']]
        for runner in pair:
            runner.clock.hour = 12
            runner.page.shade()
            runner.page.update_hour_tint()
            runner.phase(illumination, waxing=waxing)
        moons.append(pair)
    frames = []
    for tick in range(110):
        for pair in moons:
            for runner in pair:
                for _ in range(5):
                    runner.step(20)
        frames.append(board(['Full moon / stylized surface', 'Waxing crescent / unchanged terminator',
                             'Waning gibbous / surface follows light'],
                            [[runner.image() for runner in pair] for pair in moons],
                            (tick + 1) * 100, 'MOON / ILLUSTRATIVE PHASES / DAYLIGHT PALETTE'))
    save(frames, folder, 'moon', 100, still=19)

    media = []
    for calm in (False, True):
        pair = [MediaPreview(source) for source in text['media']]
        for runner in pair:
            runner.receive(reduced_motion=calm)
            runner.page.on_show()
        media.append(pair)
    frames = []
    for tick in range(433):
        pairs = [[None if runner.rotation.advances else runner.draw_at(1000 + tick * 25)
                  for runner in pair] for pair in media]
        if tick % 6 == 0:
            frames.append(board(['Ordinary title motion / shaded vinyl', 'Reduced motion / still record and held cards'],
                                pairs, tick * 25, 'NOW PLAYING / SOURCE-EXECUTED COMPLETE VISITS'))
    save(frames, folder, 'media', 150)

    manifest = {'baseline': '64c586b3376f7247ed940155ec43d33e0bc550c2',
                'host_baseline_sha256': sha256(baseline.read_bytes()).hexdigest(),
                'comparison': 'previous rejected proposals versus revised candidates; neither deployed',
                'model': 'existing float32 subset compiler; explicit font and HTTP/JSON adapters',
                'physical_heap_fps': 'unmeasured', 'sources': {},
                'host_max_wire_bytes': max(scene['bytes'] for scene in after),
                'changed_host_scenes': [key for key in old if old[key]['frames'] != new[key]['frames']]}
    for key, paths in SOURCES.items():
        manifest['sources'][key] = [dict(path=path, sha256=sha256(source.encode()).hexdigest(),
                                        minified_bytes=len(minify(source).encode()),
                                        upload_sha256=sha256(minify(source).encode()).hexdigest())
                                   for path, source in zip(paths, text[key])]
    (folder / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, default=ROOT / 'docs/art-motion/before.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'figures/art-motion/revision')
    args = parser.parse_args()
    print(json.dumps(export(args.output, args.baseline), indent=2))
