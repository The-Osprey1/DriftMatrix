"""Offline review of the real information/alert payloads; never contacts a panel."""
from __future__ import annotations

import argparse
import base64
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import desk_reminder as reminder
import firmware_compat
import focus_sprint_preview as focus
import game_mode
from led_redesign_preview import led_image
import miku_sentinel as miku
import next_up_preview as calendar
import pc_stats_push as pc
import quake_watch
import sky_watch


def samples():
    """Fixed illustrative samples, with only real sender-provided safety variants."""
    cases = []
    def add(key, title, group, builder, safety=True):
        cases.append((key, title, group, builder(True), builder(False) if safety else None))
    for key, title, state, remaining, total in (
        ('focus-long', 'Focus · two hours left', 'focus_running', 7200, 7200),
        ('focus-paused', 'Focus · paused long session', 'focus_paused', 7200, 9000),
        ('focus-normal', 'Focus · usual countdown', 'focus_running', 731, 1500),
        ('focus-rest', 'Focus · short break', 'short_break_running', 240, 300),
        ('focus-ready', 'Focus · ready for next session', 'ready_next', 0, 1500),
    ):
        add(key, title, 'Focus', lambda animated, s=state, r=remaining, t=total:
            focus.build_payload(s, remaining=r, total=t, cycle=1, animated=animated))
    for key, title, minutes, elapsed in (
        ('reminder-long', 'Reminder · 2h 05m left', 125, 0),
        ('reminder-rounded', 'Reminder · just under two hours', 120, 1),
        ('reminder-normal', 'Reminder · five minutes left', 5, 0),
        ('reminder-due', 'Reminder · due now', 5, 300),
    ):
        saved = reminder.Reminder('offline-reminder', 'TEA', 1000, 1000 + minutes * 60)
        add(key, title, 'Reminders', lambda animated, s=saved, e=elapsed:
            reminder.build_payload(s, 1000 + e))
    for key, title, name, minutes, urgency in (
        ('calendar-dentist', 'Calendar · Dentist appointment in 45m', 'Dentist appointment', 45, 'routine'),
        ('calendar-review', 'Calendar · Design review now', 'Design review', 0, 'now'),
        ('calendar-standup', 'Calendar · Standup in 5m', 'Standup', 5, 'imminent'),
        ('calendar-alias', 'Calendar · short configured alias', 'LAB', 14, 'soon'),
    ):
        add(key, title, 'Calendar', lambda animated, n=name, m=minutes, u=urgency:
            calendar.build_payload(title=n, minutes_until=m, urgency=u, animated=animated))
    for label, value in (('CPU', 91), ('RAM', 91), ('GPU', 91), ('NET', 8.4)):
        add(label.lower(), label + (' · 8.4 MB/s' if label == 'NET' else ' · high activity'), 'PC alerts',
            lambda animated, label=label, value=value: pc.metric_payload(label, value, animated=animated))
    add('combined', 'CPU + RAM · combined reading', 'PC alerts',
        lambda animated: pc.combined_payload(82, 91, animated=animated))
    for key, aircraft in (
        ('airliner', sky_watch.Aircraft('sample', 'SAMPLE', 'B738', 'A3', 4000, 45, 1.5, 90)),
        ('helicopter', sky_watch.Aircraft('sample', 'SAMPLE', 'R44', 'A7', 1200, 180, 1.0, 180)),
    ):
        add(key, 'Aircraft · ' + key, 'Aircraft', lambda animated, a=aircraft:
            sky_watch.build_payload(a), safety=False)
    for magnitude in (2.5, 5.5):
        quake = quake_watch.Quake('sample', magnitude, 'Illustrative event', 1000000,
                                 33.95, -117.8, 10)
        add('quake-' + str(magnitude), 'Earthquake · M' + str(magnitude), 'Earthquakes',
            lambda animated, q=quake: quake_watch.build_payload(q), safety=False)
    for key, reading in (('gaming', game_mode.GpuReading(78, 92)), ('gaming-missing', None)):
        add(key, 'Gaming · ' + ('GPU temperature' if reading else 'GPU unavailable'), 'Gaming',
            lambda animated, r=reading: game_mode.build_payload(r, 30), safety=False)
    for state in miku.MikuState:
        status = miku.MikuStatus(state, turns_today=12, activity=miku.MikuActivity.THINKING)
        add('miku-' + state.value, 'Miku · ' + state.value, 'Miku',
            lambda animated, status=status: miku.build_miku_payload(status, animated=animated))
    for activity in (miku.MikuActivity.IDLE, miku.MikuActivity.LISTENING, miku.MikuActivity.SPEAKING):
        status = miku.MikuStatus(miku.MikuState.LIVE, turns_today=12, activity=activity)
        add('miku-live-' + activity.value, 'Miku · ' + activity.value, 'Miku',
            lambda animated, status=status: miku.build_miku_payload(status, animated=animated))
    return cases


def decode(payload):
    """Decode GIF disposal and draw bitmap overlays in firmware order."""
    encoded = payload.get('icon')
    if encoded:
        raw = base64.b64decode(encoded.split(',', 1)[-1], validate=True)
        source = Image.open(BytesIO(raw))
        looping = source.info.get('loop') == 0
        count = source.n_frames
    else:
        source, looping, count, raw = None, False, 1, b''
    frames, durations = [], []
    for index in range(count):
        panel = Image.new('RGB', (32, 8))
        if source:
            source.seek(index)
            if source.size not in ((8, 8), (32, 8)):
                raise ValueError('Unexpected icon dimensions')
            panel.paste(source.convert('RGB'), (payload.get('iconOffsetX', 0), 0))
        for operation in payload.get('draw', []):
            if operation[0] != 'bitmap':
                raise ValueError('Unsupported review operation: ' + operation[0])
            _, x, y, width, height, pixels = operation
            panel.paste(Image.frombytes('RGB', (width, height), base64.b64decode(pixels)), (x, y))
        rgb = list(panel.getdata())
        if any(rgb[y * 32 + 8] != (0, 0, 0) for y in range(8)):
            raise ValueError('Gutter is not black')
        frames.append(['%02x%02x%02x' % pixel for pixel in rgb])
        durations.append(source.info.get('duration', 120) if source else 120)
    if source:
        source.close()
    # Match requests' json= serialization, including spaces and ASCII escapes.
    wire = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode('utf-8')
    return dict(frames=frames, durations=durations, loop=looping, bytes=len(wire),
                iconBytes=len(raw), payload_sha256=sha256(wire).hexdigest())


def capture():
    endpoint = 'offline://information-review'
    firmware_compat.observe_device(endpoint, {'version': '1.2.2'})
    output = []
    for key, title, group, animated, safety in samples():
        item = decode(firmware_compat.prepare_payload(endpoint, animated))
        item.update(id=key, title=title, group=group, static=None if safety is None else
                    decode(firmware_compat.prepare_payload(endpoint, safety)))
        output.append(item)
    return output


def frame_at(entry, milliseconds):
    total = sum(entry['durations'])
    t = milliseconds % total if entry['loop'] else min(milliseconds, total - 1)
    for index, duration in enumerate(entry['durations']):
        if t < duration:
            return entry['frames'][index]
        t -= duration
    raise AssertionError('Invalid timeline')


def export(before, after, destination):
    template = Path(__file__).with_name('app_visual_gallery.html').read_text(encoding='utf-8')
    template = template.replace('App visual review', 'Information and alert review')
    template = template.replace('Values stay still while the icon moves.',
        'Calendar cards hold the time and compact title in turn; each value stays still while shown.')
    template = template.replace('The deployed weather and air quality services currently use static mode. Animated\nviews below show what each design can do; they do not claim that motion is enabled on the clock.',
        'Safety view uses actual safety payloads where supplied. Aircraft, earthquake and gaming '
        'senders have no separate safety payload; that view freezes their first frame for inspection.')
    template = template.replace('actual static fallback', 'safety / first-frame inspection')
    template = template.replace('Static fallback</option>', 'Safety / still inspection</option>')
    data = json.dumps(dict(before=before, after=after)).replace('</', '<\\/')
    (destination / 'index.html').write_text(template.replace('__DATA__', data), encoding='utf-8')
    featured = ('calendar-dentist', 'calendar-review', 'focus-long', 'focus-paused',
                'reminder-long', 'reminder-rounded')
    font = ImageFont.truetype('C:/Windows/Fonts/segoeui.ttf', 21) if Path('C:/Windows/Fonts/segoeui.ttf').exists() else ImageFont.load_default()
    old, new = {x['id']: x for x in before}, {x['id']: x for x in after}
    def board(tick):
        image = Image.new('RGB', (1120, 1170), (12, 17, 28))
        draw = ImageDraw.Draw(image)
        draw.text((28, 16), 'DRIFTMATRIX / INFORMATION & ALERTS', font=font, fill=(235, 242, 252))
        draw.text((28, 50), 'Current', font=font, fill=(160, 178, 198))
        draw.text((580, 50), 'Proposed · actual decoded payloads', font=font, fill=(163, 223, 199))
        for index, key in enumerate(featured):
            y = 88 + index * 178
            draw.text((28, y), new[key]['title'], font=font, fill=(214, 224, 239))
            for x, entry in ((28, old[key]), (580, new[key])):
                offset = 2400 if key.startswith('calendar-') else 0
                image.paste(led_image(frame_at(entry, tick * 120 + offset)), (x, y + 33))
        return image
    board(0).save(destination / 'review.png')
    frames = [board(tick).convert('P', palette=Image.Palette.ADAPTIVE, colors=256) for tick in range(40)]
    frames[0].save(destination / 'review.gif', save_all=True, append_images=frames[1:],
                   duration=120, loop=0, disposal=1, optimize=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', type=Path)
    parser.add_argument('--before', type=Path)
    parser.add_argument('--baseline-revision', help='source commit of the saved baseline')
    parser.add_argument('--out', type=Path, default=Path('figures/information-overhaul'))
    args = parser.parse_args()
    after = capture()
    if args.capture:
        args.capture.parent.mkdir(parents=True, exist_ok=True)
        args.capture.write_text(json.dumps(after), encoding='utf-8')
        print(f'Captured {len(after)} real sample payloads')
        return
    if args.before is None:
        parser.error('--before is required')
    before = json.loads(args.before.read_text(encoding='utf-8'))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / 'after.json').write_text(json.dumps(after), encoding='utf-8')
    (args.out / 'before.json').write_text(json.dumps(before), encoding='utf-8')
    export(before, after, args.out)
    original = {entry['id']: entry for entry in before}
    manifest = dict(baseline_revision=args.baseline_revision,
        baseline_sha256=sha256(args.before.read_bytes()).hexdigest(),
        firmware='1.2.2', cases=len(after),
        changed=[entry['id'] for entry in after if entry['frames'] != original[entry['id']]['frames']],
        max_wire_bytes=max(entry['bytes'] for entry in after),
        sources={name:sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in
            ('visual_system.py', 'focus_sprint_preview.py', 'desk_reminder.py', 'next_up_preview.py')})
    (args.out / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(f'Review exported: {len(after)} scenes; maximum {max(x["bytes"] for x in after)} bytes')


if __name__ == '__main__':
    main()
