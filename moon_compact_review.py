"""Compare approved Moon 2.3 and compact 2.4 source execution; no device access."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from moon_review import MoonPreview
from weather.minify_ax import minify

ROOT = Path(__file__).resolve().parent


def export(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    reference = (ROOT / 'weather/moon-panel-v2.3.ax').read_text(encoding='utf-8')
    candidate = (ROOT / 'weather/moon-panel-v2.4.ax').read_text(encoding='utf-8')
    states = [('Full moon', 100, True, 0), ('Waxing crescent', 12, True, 0),
              ('Waning gibbous', 76, False, 0), ('Eclipse treatment', 100, True, 75),
              ('Unsynced manual preview', None, True, 0)]
    pairs = []
    for title, illumination, waxing, eclipse in states:
        year = 1970 if illumination is None else 2026
        old, new = MoonPreview(reference, year=year), MoonPreview(candidate, year=year)
        if illumination is not None:
            for runner in (old, new):
                runner.phase(illumination, waxing=waxing, eclipse=eclipse)
        pairs.append((title, old, new))
    font_path = Path('C:/Windows/Fonts/segoeui.ttf')
    font = ImageFont.truetype(str(font_path), 18) if font_path.exists() else ImageFont.load_default()
    frames, checks = [], []
    for tick in range(110):
        board = Image.new('RGB', (840, 850), (12, 16, 21))
        draw = ImageDraw.Draw(board)
        draw.text((20, 12), 'MOON / SAME ART, COMPACT RESIDENT DATA', font=font, fill=(235, 240, 247))
        draw.text((20, 42), 'Approved 2.3', font=font, fill=(171, 187, 205))
        draw.text((430, 42), 'Compact 2.4', font=font, fill=(165, 231, 207))
        for row, (title, old, new) in enumerate(pairs):
            for _ in range(5):
                old.step(20); new.step(20)
                assert old.screen.px == new.screen.px and old.screen.texts == new.screen.texts
            a, b = old.image(), new.image()
            checks.append({'state': title, 'milliseconds': (tick+1)*100,
                           'sha256': hashlib.sha256(a.tobytes()).hexdigest(), 'identical': a.tobytes() == b.tobytes()})
            y = 82 + row * 144
            draw.text((20, y), title, font=font, fill=(171, 187, 205))
            board.paste(a.resize((384, 96), Image.Resampling.NEAREST), (20, y+29))
            board.paste(b.resize((384, 96), Image.Resampling.NEAREST), (430, y+29))
        draw.text((20, 812), 'Actual sources; selected phases; modeled font; not device frames', font=font, fill=(171, 187, 205))
        frames.append(board)
    frames[64].save(output / 'comparison.png')
    frames[0].save(output / 'comparison.gif', save_all=True, append_images=frames[1:], duration=100, loop=0)
    upload = minify(candidate).encode('utf-8')
    report = {'identical_frame_pairs': len(checks), 'intermediate_draws_compared': 110*5*5,
              'candidate_minified_bytes': len(upload), 'candidate_sha256': hashlib.sha256(upload).hexdigest(),
              'actual_source_execution': True, 'panel_requests': 0, 'physical_capture': False,
              'font': 'captured glyphs plus project font model', 'checks': checks}
    (output / 'frame-parity.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    return {k: v for k, v in report.items() if k != 'checks'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export(args.output), indent=2))
