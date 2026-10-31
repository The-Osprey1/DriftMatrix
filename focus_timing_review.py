"""Offline before/after review of actual Focus wire payloads and timing."""
from __future__ import annotations

import argparse
from pathlib import Path
import json
from PIL import Image, ImageDraw, ImageFont

import firmware_compat
import focus_sprint_preview as focus
from information_preview import decode

BASELINE = Path(__file__).with_name('docs') / 'focus-timing' / 'before.json'


def capture():
    baseline = json.loads(BASELINE.read_text(encoding='utf-8'))
    endpoint = 'offline://focus-timing-review'
    firmware_compat.observe_device(endpoint, {'version': '1.2.2'})
    result = []
    for case in baseline['cases']:
        inputs = {k: case[k] for k in ('state', 'animated', 'remaining', 'total', 'cycle')}
        payload = firmware_compat.prepare_payload(endpoint, focus.build_payload(**inputs))
        result.append(dict(inputs=inputs, before=decode(case['payload']),
                           after=decode(payload), payload=payload))
    return result


def frame_at(entry, milliseconds):
    if len(entry['frames']) == 1:
        return entry['frames'][0]
    total = sum(entry['durations'])
    elapsed = milliseconds % total if entry['loop'] else min(milliseconds, total - 1)
    for frame, duration in zip(entry['frames'], entry['durations']):
        if elapsed < duration:
            return frame
        elapsed -= duration
    raise AssertionError('Invalid review timeline')


def canvas(cases, milliseconds, title):
    image = Image.new('RGB', (760, 65 + len(cases) * 130), '#10151d')
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=18)
    draw.text((24, 10), title, font=font, fill='#fff7e8')
    draw.text((24, 36), 'ORIGINAL', font=font, fill='#becfe2')
    draw.text((404, 36), 'PROPOSED', font=font, fill='#52ffb0')
    labels = {'short_break_running': 'Short break', 'long_break_running': 'Long break',
              'focus_done_cue': 'Completion cue', 'focus_running': 'Work',
              'focus_paused': 'Paused work', 'break_paused': 'Paused break',
              'ready_next': 'Ready'}
    for row, case in enumerate(cases):
        y = 67 + row * 130
        for side, x in (('before', 24), ('after', 404)):
            decoded = case[side]
            raw = bytes.fromhex(''.join(frame_at(decoded, milliseconds)))
            panel = Image.frombytes('RGB', (32, 8), raw).resize((320, 80), Image.Resampling.NEAREST)
            draw.text((x, y), labels[case['inputs']['state']], font=font, fill='#fff7e8')
            image.paste(panel, (x, y + 26))
            duration = decoded['durations'][0] if len(decoded['frames']) > 1 else 0
            note = f'{len(decoded["frames"])} poses / {duration} ms' if duration else 'Still / unchanged'
            draw.text((x, y + 108), note, font=font, fill='#becfe2')
    return image


def export(destination):
    destination.mkdir(parents=True, exist_ok=True)
    cases = capture()
    active = {case['inputs']['state']: case for case in cases if case['inputs']['animated']}
    steam = [active['short_break_running'], active['long_break_running']]
    # 60 ms is a REVIEW-only canvas clock: it represents both old and new delays
    # exactly. These large comparison GIFs are never panel payloads.
    frames = [canvas(steam, t, 'Focus steam / same art, calmer timing') for t in range(0, 10800, 60)]
    frames[0].save(destination / 'steam.gif', save_all=True, append_images=frames[1:],
                   duration=60, loop=0, disposal=1)
    completion = [active['focus_done_cue']]
    frames = [canvas(completion, t, 'Completion / same art, longer sparkle') for t in range(0, 2400, 60)]
    frames[0].save(destination / 'completion.gif', save_all=True, append_images=frames[1:],
                   duration=60, disposal=1)  # Like the actual cue, this preview does not loop.
    stable = [active[key] for key in ('focus_running', 'focus_paused', 'break_paused', 'ready_next')]
    canvas(stable, 0, 'Work, paused and ready / unchanged').save(destination / 'unchanged.png')
    wire_cases = [dict(inputs=c['inputs'], payload=c['payload']) for c in cases]
    (destination / 'after.json').write_text(json.dumps(wire_cases, indent=2) + '\n', encoding='utf-8')
    report = dict(cases=len(cases), max_wire_bytes=max(c['after']['bytes'] for c in cases),
                  max_icon_bytes=max(c['after']['iconBytes'] for c in cases),
                  all_rgb_frames_preserved=all(c['before']['frames'] == c['after']['frames'] for c in cases),
                  all_loop_policies_preserved=all(c['before']['loop'] == c['after']['loop'] for c in cases),
                  scope='Offline synthetic wire-payload review. No HTTP or device access.')
    (destination / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=BASELINE.parent)
    print(json.dumps(export(parser.parse_args().out), indent=2))
