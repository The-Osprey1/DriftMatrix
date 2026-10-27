"""Source-executed Moon comparison; illustrative phase states, no device access.

Text uses the captured glyphs where available and the project's 3x5 font for
other characters. It is a font model, not a calibrated firmware framebuffer.
"""
from __future__ import annotations

import argparse
from functools import lru_cache
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from visual_system import FONT_3X5
from weather.berry_lite import Program
from weather.minify_ax import minify
from weather.moon_panel_sim import Clock, Font, R, Screen

ROOT = Path(__file__).resolve().parent
REFERENCE = ROOT / "weather" / "moon-panel.ax"
CANDIDATE = ROOT / "weather" / "moon-panel-v2.2.ax"


def preview_font():
    glyphs = {key: [''.join('#' if c == '1' else '.' for c in row) for row in rows]
              for key, rows in FONT_3X5.items()}
    glyphs.update(json.loads((ROOT / 'weather' / 'moon_panel_captures.json').read_text(encoding='utf-8'))['glyphs'])
    return Font(glyphs)


@lru_cache(maxsize=8)
def compile_page(source, fill):
    return Program(source, real_type=R, resize_fill=fill)


class MoonPreview:
    def __init__(self, source=None, *, font=None, year=2026, epoch_ms=1793066400000,
                 fill=0xA5):
        source = CANDIDATE.read_text(encoding='utf-8') if source is None else source
        self.clock = Clock(0, epoch_ms, year, 22 if year >= 2020 else -1, 0)
        self.next_loop = 1000
        self.fixed_phase = None
        self.screen = Screen(font or preview_font())
        self.page = compile_page(source, fill).run({
            'now_ms': lambda: self.clock.now_ms, 'epoch_ms': lambda: self.clock.epoch_ms,
            'year': lambda: self.clock.year, 'hour': lambda: self.clock.hour,
            'minute': lambda: self.clock.minute, 'clear': self.screen.clear,
            'pixel': self.screen.pixel, 'line': self.screen.line,
            'text': self.screen.text, 'text_ink_width': self.screen.text_ink_width,
        })
        self.page.on_show()

    def phase(self, illumination, *, waxing=True, eclipse=0):
        """Select a clearly labeled illustrative phase, not an ephemeris result."""
        self.fixed_phase = (illumination, waxing, eclipse)
        self.page.illum, self.page.wax, self.page.ecl = illumination, waxing, eclipse
        self.page.valid = True
        self.page.name_phase(); self.page.build_disc(); self.page.compute_position()

    def step(self, milliseconds):
        self.clock.now_ms += milliseconds
        if self.clock.now_ms >= self.next_loop:
            self.page.loop()
            self.next_loop = self.clock.now_ms + 1000
            if self.fixed_phase is not None:
                illumination, waxing, eclipse = self.fixed_phase
                self.phase(illumination, waxing=waxing, eclipse=eclipse)
        self.page.draw()
        return self.screen.frame()

    def image(self):
        image = Image.new('RGB', (32, 8))
        image.putdata([((c >> 16) & 255, (c >> 8) & 255, c & 255) for c in self.screen.px])
        return image


def export(output):
    output.mkdir(parents=True, exist_ok=True)
    states = [('Full moon',100,True,0), ('Waxing crescent',12,True,0),
              ('Waning gibbous',76,False,0), ('Eclipse treatment',100,True,75),
              ('Unsynced clock / forced preview only',None,True,0)]
    pairs = []
    for title, illumination, waxing, eclipse in states:
        year=1970 if illumination is None else 2026
        old = MoonPreview(REFERENCE.read_text(encoding='utf-8'),year=year)
        new = MoonPreview(year=year)
        if illumination is not None:
            for runner in (old,new):
                runner.phase(illumination, waxing=waxing, eclipse=eclipse)
        pairs.append((title,old,new))
    font_path = Path('C:/Windows/Fonts/segoeui.ttf')
    font = ImageFont.truetype(str(font_path),18) if font_path.exists() else ImageFont.load_default()
    frames = []
    # 11 seconds, sampled every 100 ms with all intermediate 20 ms draws.
    for tick in range(110):
        height=82+len(pairs)*144+52
        board = Image.new('RGB',(840,height),(12,16,21));draw = ImageDraw.Draw(board)
        draw.text((20,12),'MOON / SCENE AND INFORMATION CARD',font=font,fill=(235,240,247))
        draw.text((20,42),'Current 2.1',font=font,fill=(171,187,205))
        draw.text((430,42),'Proposed 2.2',font=font,fill=(165,231,207))
        for row,(title,old,new) in enumerate(pairs):
            for _ in range(5):
                old.step(20);new.step(20)
            y=82+row*144
            draw.text((20,y),title,font=font,fill=(171,187,205))
            board.paste(old.image().resize((384,96),Image.Resampling.NEAREST),(20,y+29))
            board.paste(new.image().resize((384,96),Image.Resampling.NEAREST),(430,y+29))
        draw.text((20,height-38),'Illustrative phases · source execution · modeled font · not deployed',font=font,fill=(171,187,205))
        frames.append(board)
    frames[64].save(output/'moon-review.png')
    frames[0].save(output/'moon-review.gif',save_all=True,append_images=frames[1:],duration=100,loop=0)
    text=CANDIDATE.read_text(encoding='utf-8')
    report={'candidate_upload_bytes':len(minify(text).encode()),
            'candidate_upload_sha256':hashlib.sha256(minify(text).encode()).hexdigest(),
            'reference_upload_sha256':hashlib.sha256(minify(REFERENCE.read_text(encoding='utf-8')).encode()).hexdigest(),
            'sample_frames':len(frames),'phase_inputs':'illustrative, deliberately selected',
            'font':'captured glyphs plus project font fallback',
            'physical_deployment':False,'device_heap_fps':'unmeasured'}
    (output/'moon-review.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'figures'/'moon-overhaul')
    args=parser.parse_args(argv)
    print(json.dumps(export(args.output),indent=2))


if __name__=='__main__':
    main()
