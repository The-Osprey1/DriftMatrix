"""Offline, payload-exact comparison gallery for the colorful LED redesign.

Capture a baseline before editing with ``--capture before.json``. Then pass
that capture to ``--before before.json --out figures/led-redesign``. The
gallery decodes the same GIF and RGB888 bitmap that the panel receives; no
network, device writes, or substitute artwork is involved.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import math
from pathlib import Path
import sys

from PIL import Image, ImageDraw, ImageFont

# weather_canvas also supports direct sender execution and uses a sibling import.
sys.path.insert(0, str(Path(__file__).resolve().parent / "weather"))

import desk_companion as companion
import pc_stats_push as pc
from weather import aqi_brief_push as aqi
from weather import weather_brief_push as weather


def examples() -> list[tuple[str, str, str, dict]]:
    """Fixed illustrative readings, identical for both versions."""
    result = []
    for key, title, condition, is_day, temp in (
        ("sun", "Sunshine", "clear", True, 78),
        ("night", "Clear night", "clear", False, 64),
        ("partly", "Partly cloudy", "part", True, 72),
        ("cloudy", "Cloudy", "cloudy", True, 68),
        ("overcast", "Overcast", "overcast", True, 62),
        ("rain", "Rain", "rain", True, 58),
        ("drizzle", "Drizzle", "drizzle", True, 60),
        ("snow", "Snow", "snow", True, 28),
        ("sleet", "Sleet", "sleet", True, 32),
        ("storm", "Thunderstorm", "storm", True, 66),
        ("fog", "Fog", "fog", True, 54),
    ):
        scene = weather.WeatherScene("current", f"{temp}°", condition, is_day,
                                     temp, temp - 8, temp + 10, None)
        result.append((key, title, "Weather", weather.build_payload(scene)))
    when = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
    for value, title in ((28, "Good air"), (72, "Moderate air"), (125, "Sensitive groups"),
                         (175, "Unhealthy air"), (250, "Very unhealthy air"), (350, "Hazardous air")):
        sample = aqi.AQISnapshot(when, "UTC", value, 12.0,
                                tuple(when + timedelta(hours=i) for i in range(1, 6)),
                                (value,) * 5)
        result.append((f"aqi-{value}", title, "Air quality", aqi.build_payload(sample)))
    for mood in companion.CompanionMood:
        result.append((f"companion-{mood.value}", mood.value.replace("_", " ").title(),
                       "Companion", companion.build_companion_payload(mood)))
    for label, value in (("CPU", 82), ("RAM", 91), ("GPU", 96)):
        result.append((label.lower(), f"{label} activity", "PC alerts", pc.metric_payload(label, value)))
    result.append(("combined", "CPU + RAM", "PC alerts", pc.combined_payload(82, 91)))
    return result


def decode(payload: dict) -> dict:
    """Composite opaque GIF frames and static bitmaps in firmware draw order."""
    icon_bytes = base64.b64decode(payload["icon"])
    frames, durations = [], []
    with Image.open(BytesIO(icon_bytes)) as gif:
        looping = gif.info.get("loop") == 0
        for phase in range(gif.n_frames):
            gif.seek(phase)
            panel = Image.new("RGB", (32, 8))
            panel.paste(gif.convert("RGB"), (payload.get("iconOffsetX", 0), 0))
            for op in payload["draw"]:
                if op[0] != "bitmap":
                    raise ValueError(f"Preview does not implement {op[0]}")
                _, x, y, width, height, data = op
                panel.paste(Image.frombytes("RGB", (width, height), base64.b64decode(data)), (x, y))
            pixels = list(panel.getdata())
            if any(pixels[y * 32 + 8] != (0, 0, 0) for y in range(8)):
                raise ValueError("The icon/value separator is not black")
            frames.append(["%02x%02x%02x" % color for color in pixels])
            durations.append(gif.info.get("duration", 120))
    return {"frames": frames, "durations": durations, "loop": looping,
            "bytes": len(json.dumps(payload, separators=(",", ":")).encode()),
            "iconBytes": len(icon_bytes)}


def capture() -> list[dict]:
    return [{"id": key, "title": title, "group": group, **decode(payload)}
            for key, title, group, payload in examples()]


def led_image(pixels: list[str], cell: int = 16) -> Image.Image:
    img = Image.new("RGB", (32 * cell, 8 * cell), (3, 5, 10))
    draw = ImageDraw.Draw(img)
    for index, value in enumerate(pixels):
        x, y = (index % 32) * cell, (index // 32) * cell
        color = tuple(bytes.fromhex(value)) if value != "000000" else (10, 14, 22)
        draw.rectangle((x + 2, y + 2, x + cell - 3, y + cell - 3), fill=color)
    return img


def exports(after: list[dict], out: Path) -> None:
    """Small looping gallery GIF and a labeled contact sheet of real frames."""
    featured = [next(x for x in after if x["id"] == key)
                for key in ("sun", "rain", "storm", "aqi-72", "companion-idle", "cpu")]
    font_path = Path("C:/Windows/Fonts/segoeui.ttf")
    font = ImageFont.truetype(str(font_path), 21) if font_path.exists() else ImageFont.load_default()
    title_font = ImageFont.truetype(str(font_path), 30) if font_path.exists() else font

    def board(tick: int) -> Image.Image:
        img = Image.new("RGB", (1120, 650), (12, 17, 28))
        draw = ImageDraw.Draw(img)
        draw.text((32, 20), "AWTRIX / COLOR IN MOTION", font=title_font, fill=(238, 242, 252))
        draw.text((32, 61), "Actual 32 × 8 artwork · illustrative readings · offline preview", font=font, fill=(153, 166, 190))
        for index, entry in enumerate(featured):
            x, y = 32 + (index % 2) * 552, 112 + (index // 2) * 176
            elapsed = tick * 120
            total = sum(entry["durations"])
            elapsed = elapsed % total if entry["loop"] else min(elapsed, total - 1)
            phase = 0
            while elapsed >= entry["durations"][phase] and phase < len(entry["frames"]) - 1:
                elapsed -= entry["durations"][phase]
                phase += 1
            draw.text((x, y), entry["title"], font=font, fill=(195, 209, 231))
            img.paste(led_image(entry["frames"][phase]), (x, y + 32))
        return img

    # Close every featured cycle exactly at the montage loop seam, too.
    ticks = math.lcm(*(sum(entry["durations"]) for entry in featured)) // 120
    board(0).save(out / "redesign.png")
    frames = [board(tick).convert("P", palette=Image.Palette.ADAPTIVE, colors=256)
              for tick in range(ticks)]
    frames[0].save(out / "redesign.gif", save_all=True, append_images=frames[1:],
                   duration=120, loop=0, disposal=1, optimize=True)


HTML = r'''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AWTRIX — Color in motion</title>
<style>
:root{color-scheme:dark;font-family:Segoe UI,system-ui,sans-serif;color:#e9edf7;background:#0a0e17}*{box-sizing:border-box}body{margin:0}button,select{font:inherit}button{cursor:pointer}main{max-width:1320px;margin:auto;padding:44px 32px 64px}.eyebrow{font:600 12px/1.5 monospace;color:#8c9cc1;letter-spacing:3px}h1{font-size:clamp(36px,5vw,64px);font-weight:600;letter-spacing:-2px;margin:14px 0 16px}h1 span{background:linear-gradient(90deg,#7ddcff,#a99cff,#ff9ec8);color:transparent;background-clip:text}p{line-height:1.65;color:#a4b0ca;max-width:740px;margin:0}.toolbar{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin:28px 0 24px}.controls{display:flex;gap:8px;align-items:center;margin-left:auto}button,select{border:1px solid #303b55;background:#151d2e;color:#d6e0f5;padding:10px 14px;border-radius:9px}button[aria-pressed=true]{color:#bfefff;border-color:#67cee9;background:#13313c}button:focus-visible,select:focus-visible{outline:2px solid #b4a2ff;outline-offset:4px}.intro{display:flex;flex-wrap:wrap;gap:12px;margin:24px 0}.tag{font-size:13px;color:#b1bfda;background:#121a2b;border:1px solid #222d45;padding:7px 10px;border-radius:6px}.card{border:1px solid #263049;background:linear-gradient(145deg,#141c2b,#101623);border-radius:16px;padding:22px;margin:18px 0}.head{display:flex;justify-content:space-between;align-items:center;margin:0 0 18px}.head h2{font-size:20px;font-weight:550;margin:0}.head span{color:#90a3c5;font-size:12px;letter-spacing:1px;text-transform:uppercase}.pair{display:grid;grid-template-columns:1fr 1fr;gap:24px}.caption{font:600 11px monospace;letter-spacing:2px;color:#9bacc9;margin-bottom:10px}.caption.new{color:#8de5c1}canvas{display:block;width:100%;height:auto;background:#02040a;border:9px solid #03060d;border-radius:10px;box-shadow:inset 0 0 0 1px #182235,0 10px 20px #0004;image-rendering:pixelated}.empty{color:#b4bdd1;padding:40px}.foot{border-top:1px solid #25304a;margin-top:32px;padding-top:20px;font-size:13px}.count{font-size:13px;color:#8d9ab3}.hide{display:none}@media(max-width:760px){main{padding:28px 16px}.pair{grid-template-columns:1fr}.controls{margin-left:0;width:100%}.head span{font-size:10px}.card{padding:16px}.toolbar{gap:8px}.toolbar button{padding:9px 10px}h1{letter-spacing:-1px}}
</style><main>
<div class="eyebrow">AWTRIX · 32 × 8</div><h1>A little more <span>life on your desk.</span></h1>
<p>Richer colors. Expressive characters. Weather that moves. These previews use the exact artwork and timing prepared for your LED panel, with the same sample readings on both sides.</p>
<div class="intro"><span class="tag">Colorful pixel art</span><span class="tag">Steady, readable values</span><span class="tag">Smooth looping motion</span></div>
<div class="toolbar" role="group" aria-label="Preview controls"><div id="filters"></div><div class="controls"><button id="play" aria-pressed="true">Pause motion</button><label for="mode" class="caption" style="margin:0 0 0 8px">View</label><select id="mode"><option value="color">Full color</option><option value="onebit">Low-light silhouette</option></select></div></div>
<div class="count" id="count"></div><div id="gallery"></div>
<p class="foot">Offline design preview · Sample readings, not live measurements. Full color shows source pixels; low-light view models the panel’s one-bit channel threshold. Physical brightness, diffusion, and color will differ. The live panel has not been updated.</p>
</main><script>
const DATA=__DATA__;let playing=!matchMedia('(prefers-reduced-motion: reduce)').matches,elapsed=0,last=performance.now(),mode='color';const views=[];
const groups=['All screens',...new Set(DATA.after.map(x=>x.group))];
function filter(group){let n=0;document.querySelectorAll('.card').forEach(card=>{const show=group==='All screens'||card.dataset.group===group;card.classList.toggle('hide',!show);if(show)n++});document.querySelectorAll('#filters button').forEach(b=>b.setAttribute('aria-pressed',b.textContent===group));document.getElementById('count').textContent=n+' screens · before and after'}
groups.forEach(group=>{const b=document.createElement('button');b.textContent=group;b.style.margin='0 6px 6px 0';b.onclick=()=>filter(group);document.getElementById('filters').append(b)});
DATA.after.forEach(entry=>{const card=document.createElement('article');card.className='card';card.dataset.group=entry.group;const head=document.createElement('div');head.className='head';const h=document.createElement('h2');h.textContent=entry.title;const type=document.createElement('span');type.textContent=entry.group;head.append(h,type);card.append(head);const pair=document.createElement('div');pair.className='pair';const before=DATA.before.find(x=>x.id===entry.id);[[before,'BEFORE',false],[entry,'REDESIGNED',true]].forEach(([data,label,isNew])=>{const wrap=document.createElement('div');const caption=document.createElement('div');caption.className='caption'+(isNew?' new':'');caption.textContent=label;const c=document.createElement('canvas');c.width=640;c.height=160;c.setAttribute('role','img');c.setAttribute('aria-label',entry.title+' '+label.toLowerCase()+' LED animation');wrap.append(caption,c);pair.append(wrap);if(data)views.push({data,canvas:c,card})});card.append(pair);document.getElementById('gallery').append(card)});
function draw(view){const d=view.data,total=d.durations.reduce((a,b)=>a+b,0);let t=d.loop?elapsed%total:Math.min(elapsed,total-1),i=0;while(t>=d.durations[i]&&i<d.frames.length-1){t-=d.durations[i];i++}const key=i+':'+mode;if(view.lastFrame===key)return;view.lastFrame=key;const ctx=view.canvas.getContext('2d');ctx.fillStyle='#02040a';ctx.fillRect(0,0,640,160);d.frames[i].forEach((hex,j)=>{let rgb=[0,2,4].map(k=>parseInt(hex.slice(k,k+2),16));if(mode==='onebit')rgb=rgb.map(v=>v>=174?255:0);const lit=rgb.some(v=>v>0);ctx.fillStyle=lit?'rgb('+rgb.join(',')+')':'#0d121d';ctx.fillRect((j%32)*20+2,Math.floor(j/32)*20+2,16,16)})}
function animate(now){if(playing)elapsed+=Math.min(now-last,100);last=now;views.forEach(v=>{if(!v.card.classList.contains('hide'))draw(v)});requestAnimationFrame(animate)}
const play=document.getElementById('play');function updatePlay(){play.textContent=playing?'Pause motion':'Play motion';play.setAttribute('aria-pressed',playing)}play.onclick=()=>{playing=!playing;updatePlay()};document.getElementById('mode').onchange=e=>{mode=e.target.value};filter('All screens');updatePlay();requestAnimationFrame(animate);
</script></html>'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--before", type=Path)
    parser.add_argument("--out", type=Path, default=Path("figures/led-redesign"))
    args = parser.parse_args()
    after = capture()
    if args.capture:
        args.capture.parent.mkdir(parents=True, exist_ok=True)
        args.capture.write_text(json.dumps(after), encoding="utf-8")
        print(f"Captured {len(after)} scenes to {args.capture}")
        return
    if args.before is None:
        parser.error("--before is required for a comparison gallery")
    before = json.loads(args.before.read_text(encoding="utf-8"))
    args.out.mkdir(parents=True, exist_ok=True)
    data = json.dumps({"before": before, "after": after}).replace("</", "<\\/")
    (args.out / "index.html").write_text(HTML.replace("__DATA__", data), encoding="utf-8")
    (args.out / "after.json").write_text(json.dumps(after), encoding="utf-8")
    exports(after, args.out)
    print(f"Gallery: {(args.out / 'index.html').resolve()}")
    print(f"Scenes: {len(after)}; maximum payload: {max(x['bytes'] for x in after)} B")


if __name__ == "__main__":
    main()
