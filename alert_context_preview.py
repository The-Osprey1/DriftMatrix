"""Offline actual-payload comparison for earthquake and gaming context."""
import argparse
from hashlib import sha256
import inspect
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import firmware_compat
import game_mode as game
from information_preview import decode, frame_at
from led_redesign_preview import led_image
import quake_watch as quake


def cases():
    output = []
    origin = (quake.HOME_LAT, quake.HOME_LON)
    for key, title, magnitude, point, home in (
        ('quake-near', 'Earthquake · magnitude and nearby distance', 3.2, (33.95, -117.4), origin),
        ('quake-home', 'Earthquake · event at configured origin', 2.5, origin, origin),
        ('quake-edge', 'Earthquake · near the watch boundary', 5.8, (35.2, -117.56644), origin),
        ('quake-custom', 'Earthquake · custom origin (0, 0)', 3.2, (0, .2), (0, 0)),
        ('quake-far', 'Earthquake · global distance formatting diagnostic', 8.3, (0, 180), (0, 0)),
        ('quake-magnitude', 'Earthquake · large magnitude formatting diagnostic', 10.2, (33.95, -117.4), origin),
    ):
        q = quake.Quake('illustrative', magnitude, 'Illustrative event', 1790000000000,
                        point[0], point[1], 8)
        output.append((key, title, 'Earthquakes', q, home))
    for key, title, reading, minutes in (
        ('game-short', 'Gaming · 78C and 35m process age', game.GpuReading(78, 80), 35),
        ('game-long', 'Gaming · 65C and 2h 05m process age', game.GpuReading(65, 70), 125),
        ('game-missing', 'Gaming · unavailable GPU, known process age', None, 35),
        ('game-age', 'Gaming · unknown process start time', game.GpuReading(78, 80), None),
        ('game-hot', 'Gaming · hot GPU, long-running process', game.GpuReading(90, 99), 250),
        ('game-new', 'Gaming · newly observed process', game.GpuReading(40, 20), 0),
        ('game-extended', 'Gaming · extended process-age diagnostic', game.GpuReading(70, 70), 7205),
    ):
        output.append((key, title, 'Gaming', reading, minutes))
    return output


def capture():
    endpoint = 'offline://alert-context-review'
    firmware_compat.observe_device(endpoint, {'version': '1.2.2'})
    entries = []
    for key, title, group, value, argument in cases():
        builder = quake.build_payload if group == 'Earthquakes' else game.build_payload
        parameters = inspect.signature(builder).parameters
        kwargs = {'home': argument} if group == 'Earthquakes' and 'home' in parameters else {}
        args = (value,) if group == 'Earthquakes' else (value, argument)
        animated = builder(*args, **kwargs)
        safety = builder(*args, **kwargs, animated=False) if 'animated' in parameters else None
        entries.append(dict(id=key, title=title, group=group,
            **decode(firmware_compat.prepare_payload(endpoint, animated)),
            static=None if safety is None else decode(firmware_compat.prepare_payload(endpoint, safety))))
    return entries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', type=Path)
    parser.add_argument('--before', type=Path)
    parser.add_argument('--baseline-revision')
    parser.add_argument('--out', type=Path, default=Path('figures/alert-context'))
    args = parser.parse_args()
    after = capture()
    if args.capture:
        args.capture.write_text(json.dumps(after), encoding='utf-8')
        print('Captured', len(after), 'actual-payload states')
        return
    if args.before is None:
        parser.error('--before is required')
    before = json.loads(args.before.read_text(encoding='utf-8'))
    args.out.mkdir(parents=True, exist_ok=True)
    for name, entries in (('before', before), ('after', after)):
        (args.out / (name + '.json')).write_text(json.dumps(entries), encoding='utf-8')
    old = {e['id']: e for e in before}
    sources = ('quake_watch.py', 'game_mode.py', 'panel_scene.py', 'utility_hub.py')
    manifest = dict(baseline_revision=args.baseline_revision,
        baseline_sha256=sha256(args.before.read_bytes()).hexdigest(), cases=len(after),
        max_wire_bytes=max(e['bytes'] for e in after), firmware='1.2.2',
        changed=[e['id'] for e in after if e['frames'] != old[e['id']]['frames']],
        source_sha256_utf8_lf={name:sha256(Path(__file__).with_name(name).read_text(
            encoding='utf-8').encode('utf-8')).hexdigest() for name in sources})
    (args.out / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    template = Path(__file__).with_name('app_visual_gallery.html').read_text(encoding='utf-8')
    template = template.replace('App visual review', 'Earthquake and gaming context')
    template = template.replace('Values stay still while the icon moves.',
        'Both readings stay visible together while the icon moves.')
    template = template.replace('The deployed weather and air quality services currently use static mode. Animated\nviews below show what each design can do; they do not claim that motion is enabled on the clock.',
        'Earthquake distance uses the configured origin. Gaming time is process age, '
        'not active play time. Safety view uses the new static payload; the baseline '
        'had no separate fallback and is frozen at its first frame for inspection.')
    template = template.replace('actual static fallback', 'safety / baseline first-frame inspection')
    (args.out / 'index.html').write_text(template.replace('__DATA__', json.dumps(
        dict(before=before, after=after)).replace('</', '<\\/')), encoding='utf-8')
    new = {e['id']: e for e in after}
    selected = ['quake-near', 'quake-custom', 'quake-magnitude', 'game-short', 'game-long', 'game-missing']
    font = ImageFont.truetype('C:/Windows/Fonts/segoeui.ttf', 21) if Path('C:/Windows/Fonts/segoeui.ttf').exists() else ImageFont.load_default()
    def board(tick):
        image = Image.new('RGB', (1120, 1170), (12, 17, 28))
        draw = ImageDraw.Draw(image)
        draw.text((28, 16), 'DRIFTMATRIX / EARTHQUAKE & GAMING CONTEXT', font=font, fill=(235, 242, 252))
        draw.text((28, 50), 'Current', font=font, fill=(160, 178, 198))
        draw.text((580, 50), 'Proposed · actual decoded payloads', font=font, fill=(163, 223, 199))
        for index, key in enumerate(selected):
            y = 88 + index * 178
            draw.text((28, y), new[key]['title'], font=font, fill=(214, 224, 239))
            for x, entry in ((28, old[key]), (580, new[key])):
                image.paste(led_image(frame_at(entry, tick * 120)), (x, y + 33))
        return image
    board(0).save(args.out / 'review.png')
    frames = [board(tick).convert('P', palette=Image.Palette.ADAPTIVE, colors=256) for tick in range(32)]
    frames[0].save(args.out / 'review.gif', save_all=True, append_images=frames[1:],
                   duration=120, loop=0, disposal=1, optimize=True)
    print('Exported', len(after), 'states; maximum wire bytes', manifest['max_wire_bytes'])


if __name__ == '__main__':
    main()
