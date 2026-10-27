"""Offline actual-payload comparison for receive-throughput alerts."""
import argparse
import json
from hashlib import sha256
from pathlib import Path

import firmware_compat
from information_preview import decode, frame_at
from led_redesign_preview import led_image
import pc_stats_push as pc
from PIL import Image, ImageDraw, ImageFont


def cases():
    result = []
    for key, label, rate in (
        ('low', '8.4 MiB/s', 8.4), ('normal', '25.0 MiB/s', 25),
        ('fraction', '99.9 MiB/s', 99.9), ('fast', '150.0 MiB/s', 150),
        ('faster', '250.0 MiB/s', 250), ('last-mib', '1023.9 MiB/s', 1023.9),
        ('gib', '1.00 GiB/s', 1024), ('multi-gib', '2.00 GiB/s', 2048),
        ('ceiling', '10000 MiB/s (sampler gate ceiling)', 10000),
        ('zero', 'Zero receive rate', 0), ('unknown', 'Unavailable sample (API diagnostic)', None),
    ):
        result.append((key, label, [('NET', rate)]))
    result += [
        ('cpu-priority', 'CPU 88% + receive 250 MiB/s', [('CPU', 88), ('NET', 250)]),
        ('gpu-priority', 'GPU 82% + receive 95 MiB/s', [('GPU', 82), ('NET', 95)]),
        ('combined-priority', 'CPU 88% + RAM 90% + receive 250 MiB/s', [('CPU', 88), ('RAM', 90), ('NET', 250)]),
    ]
    return result


def capture():
    endpoint = 'offline://network-review'
    firmware_compat.observe_device(endpoint, {'version': '1.2.2'})
    entries = []
    for key, title, values in cases():
        animated = firmware_compat.prepare_payload(endpoint, pc.build_payload(values))
        safety = firmware_compat.prepare_payload(endpoint, pc.build_payload(values, animated=False))
        entries.append(dict(id=key, title=title, group='Receive throughput' if len(values) == 1 else
            'Alert priority', **decode(animated), static=decode(safety)))
    return entries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', type=Path)
    parser.add_argument('--before', type=Path)
    parser.add_argument('--baseline-revision')
    parser.add_argument('--out', type=Path, default=Path('figures/network-overhaul'))
    args = parser.parse_args()
    after = capture()
    if args.capture:
        args.capture.write_text(json.dumps(after), encoding='utf-8')
        print(f'Captured {len(after)} sample payloads')
        return
    if not args.before:
        parser.error('--before is required')
    before = json.loads(args.before.read_text(encoding='utf-8'))
    args.out.mkdir(parents=True, exist_ok=True)
    for name, data in [('before', before), ('after', after)]:
        (args.out / (name + '.json')).write_text(json.dumps(data), encoding='utf-8')
    original = {entry['id']: entry for entry in before}
    sources = ('pc_stats_push.py', 'network_readout.py', 'pyproject.toml')
    manifest = dict(baseline_revision=args.baseline_revision,
        baseline_sha256=sha256(args.before.read_bytes()).hexdigest(), cases=len(after),
        firmware='1.2.2', max_wire_bytes=max(entry['bytes'] for entry in after),
        changed=[entry['id'] for entry in after if entry['frames'] != original[entry['id']]['frames']],
        source_sha256_utf8_lf={name:sha256(Path(__file__).with_name(name).read_text(
            encoding='utf-8').encode('utf-8')).hexdigest() for name in sources})
    (args.out / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    template = Path(__file__).with_name('app_visual_gallery.html').read_text(encoding='utf-8')
    template = template.replace('App visual review', 'Network receive review')
    template = template.replace('Values stay still while the icon moves.',
        'Receive rate and binary units stay visible together while packets move into the receiver.')
    template = template.replace('The deployed weather and air quality services currently use static mode. Animated\nviews below show what each design can do; they do not claim that motion is enabled on the clock.',
        'MiB/s is mebibytes received per second. The rail uses a configured visual scale; '
        'it is not measured link utilization. Hardware alerts take priority over network traffic.')
    (args.out / 'index.html').write_text(template.replace('__DATA__',
        json.dumps(dict(before=before, after=after)).replace('</', '<\\/')), encoding='utf-8')
    font = ImageFont.truetype('C:/Windows/Fonts/segoeui.ttf', 21) if Path('C:/Windows/Fonts/segoeui.ttf').exists() else ImageFont.load_default()
    old, new = {x['id']: x for x in before}, {x['id']: x for x in after}
    selected = ['normal', 'fast', 'faster', 'gib', 'cpu-priority', 'gpu-priority']
    def board(tick):
        image = Image.new('RGB', (1120, 1170), (12, 17, 28))
        draw = ImageDraw.Draw(image)
        draw.text((28, 16), 'DRIFTMATRIX / RECEIVE THROUGHPUT', font=font, fill=(235, 242, 252))
        draw.text((28, 50), 'Current', font=font, fill=(160, 178, 198))
        draw.text((580, 50), 'Proposed · actual decoded payloads', font=font, fill=(163, 223, 199))
        for index, key in enumerate(selected):
            y = 88 + index * 178
            draw.text((28, y), new[key]['title'], font=font, fill=(214, 224, 239))
            for x, entry in ((28, old[key]), (580, new[key])):
                image.paste(led_image(frame_at(entry, tick * 120)), (x, y + 33))
        return image
    board(0).save(args.out / 'review.png')
    frames = [board(tick).convert('P', palette=Image.Palette.ADAPTIVE, colors=256) for tick in range(36)]
    frames[0].save(args.out / 'review.gif', save_all=True, append_images=frames[1:],
                   duration=120, loop=0, disposal=1, optimize=True)
    print('Maximum wire bytes:', max(x['bytes'] for x in after))


if __name__ == '__main__':
    main()
