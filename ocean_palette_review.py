"""Reproduce the source-executed Ocean palette comparison, without a device."""
from collections import Counter
import argparse,hashlib,json
from pathlib import Path
from PIL import Image,ImageDraw,ImageFont
from ocean_preview import OceanPreview
from weather.minify_ax import minify
ROOT=Path(__file__).resolve().parent


def export(out):
    out.mkdir(parents=True,exist_ok=True)
    sources=[(ROOT/p).read_text(encoding='utf-8') for p in ('ocean-v3.2.ax','ocean-v3.3.ax')]
    a,b=[OceanPreview(s) for s in sources];a.show();b.show()
    path=Path('C:/Windows/Fonts/segoeui.ttf')
    font=ImageFont.truetype(str(path),19) if path.exists() else ImageFont.load_default()
    frames=[];hist=[Counter(),Counter()]
    for _ in range(250):
        a.advance(120);b.advance(120)
        for field in a.page.__berry_members__:
            if field not in ('w','c','B','sc'):assert getattr(a.page,field)==getattr(b.page,field),field
        board=Image.new('RGB',(840,220),(12,16,21));d=ImageDraw.Draw(board)
        d.text((20,12),'OCEAN / RESTORE THE REEF COLOURS',font=font,fill=(235,240,247))
        d.text((20,45),'Current: blue-grey seabed',font=font,fill=(171,187,205))
        d.text((430,45),'Proposed: original green reef',font=font,fill=(170,235,175))
        for j,r in enumerate((a,b)):
            image=r.image();hist[j].update(image.getdata());board.paste(image.resize((384,96),Image.Resampling.NEAREST),(20+j*410,78))
        d.text((20,186),'Same motion and packed storage. Source preview, not a device capture.',font=font,fill=(171,187,205));frames.append(board)
    frames[180].save(out/'palette-comparison.png')
    frames[0].save(out/'palette-comparison.gif',save_all=True,append_images=frames[1:],duration=120,loop=0)
    def balance(h):
        total=sum(h.values())
        return {name:sum(n for (r,g,b),n in h.items() if condition(r,g,b))/total for name,condition in [('blue',lambda r,g,b:b>r and b>g),('green',lambda r,g,b:g>r and g>b),('warm',lambda r,g,b:r>g and r>b)]}
    upload=minify(sources[1]).encode()
    report={'candidate':'Ocean 3.3','samples':250,'seconds':30,'state_parity':True,'before':balance(hist[0]),'after':balance(hist[1]),'minified_bytes':len(upload),'sha256':hashlib.sha256(upload).hexdigest(),'panel_requests':0,'physical_capture':False}
    (out/'palette-review.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    print(json.dumps(export(args.output),indent=2))
