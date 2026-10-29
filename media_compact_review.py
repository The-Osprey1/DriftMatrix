"""Source-executed approved/compact media comparison; explicit native adapters."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from now_playing_review import MediaPreview
from now_playing_bridge import install_source
from weather.minify_ax import minify

ROOT = Path(__file__).resolve().parent


def export(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    sources=[(ROOT/name).read_text(encoding='utf-8') for name in ('now_playing-v2.3.ax','now_playing-v2.4.ax')]
    samples=[('Short title / known progress','WAIT',12,False),
             ('Normal scroll','MIDNIGHT CITY - M83',18,False),
             ('Reduced motion / whole-glyph cards','MIDNIGHT CITY - M83',18,True),
             ('Maximum-width 120-character title','M'*120,24,False),
             ('Maximum-width title / held cards','M'*120,24,True),
             ('Unknown progress','UNKNOWN TIMELINE',-1,True)]
    pairs=[]
    for _title,text,progress,calm in samples:
        runners=[MediaPreview(source) for source in sources]
        for runner in runners:
            runner.receive(text,progress,reduced_motion=calm)
            runner.page.on_show()
        pairs.append(runners)
    font_path=Path('C:/Windows/Fonts/segoeui.ttf')
    font=ImageFont.truetype(str(font_path),18) if font_path.exists() else ImageFont.load_default()
    frames, checks, previous=[],[],{}
    compared=0
    for tick in range(441):
        for row,(old,new) in enumerate(pairs):
            if not old.rotation.advances:
                a,b=old.draw_at(1000+tick*25),new.draw_at(1000+tick*25)
                assert a.tobytes()==b.tobytes()
                assert old.rotation.advances==new.rotation.advances
                previous[row]=(a,b)
                compared+=1
        if tick%4:
            continue
        board=Image.new('RGB',(840,1000),(12,16,21));draw=ImageDraw.Draw(board)
        draw.text((20,12),'NOW PLAYING / SAME ART, COMPACT STORAGE',font=font,fill=(235,240,247))
        draw.text((20,42),'Approved 2.3',font=font,fill=(171,187,205))
        draw.text((430,42),'Compact 2.4',font=font,fill=(165,231,207))
        for row,(title,_text,_progress,_calm) in enumerate(samples):
            a,b=previous[row]
            checks.append({'case':title,'milliseconds':tick*25,
                           'identical':a.tobytes()==b.tobytes(),
                           'sha256':hashlib.sha256(a.tobytes()).hexdigest(),
                           'frozen_after_exit_request':bool(pairs[row][0].rotation.advances)})
            y=82+row*144
            draw.text((20,y),title,font=font,fill=(171,187,205))
            board.paste(a.resize((384,96),Image.Resampling.NEAREST),(20,y+29))
            board.paste(b.resize((384,96),Image.Resampling.NEAREST),(430,y+29))
        draw.text((20,960),'Source execution; native adapters; last frame holds after exit; not device captures',font=font,fill=(171,187,205))
        frames.append(board)
    frames[20].save(output/'comparison.png')
    frames[0].save(output/'comparison.gif',save_all=True,append_images=frames[1:],duration=100,loop=0)
    raw=minify(sources[1]).encode()
    fixture=minify(install_source(sources[1],'http://192.0.2.189:8765/nowplaying')).encode()
    report={'identical_saved_frame_pairs':len(checks),'compared_completed_draws':compared,
            'template_minified_bytes':len(raw),'template_minified_sha256':hashlib.sha256(raw).hexdigest(),
            'fixture_minified_bytes':len(fixture),'fixture_minified_sha256':hashlib.sha256(fixture).hexdigest(),
            'panel_requests':0,'physical_capture':False,'native_boundaries':'JSON, HTTP callback and module imports use explicit preview adapters; graphics and float32 are models',
            'checks':checks}
    (output/'frame-parity.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    return {k:v for k,v in report.items() if k!='checks'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(export(args.output),indent=2))
