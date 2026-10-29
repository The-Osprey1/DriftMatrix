"""Hash-pinned upstream Berry memory/native-frame check; no network or downloads.

Uses real upstream JSON/string modules, explicit clock/HTTP/rotation fixtures and
a bounded framebuffer. Upstream precision/allocation differ from ESP32 firmware;
these results cannot establish device admission or physical pixels/performance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

from now_playing_bridge import install_source
from ocean_memory_profile import RUNTIME_SHA256, RUNTIME_URL, RUNNER
from weather.minify_ax import minify

ROOT = Path(__file__).resolve().parent
STATE_URL = 'http://192.0.2.189:8765/nowplaying'
SETUP = ('var ms=1000 var p=nil var capture=false var screen=[] var z=0 while z<256 screen.push(0) z+=1 end '
         'def now_ms() return ms end def clear() if !capture return end var i=0 while i<256 screen[i]=0 i+=1 end end '
         'def pixel(x,y,c) if !capture return end x=int(x) y=int(y) if x>=0&&x<32&&y>=0&&y<8 screen[y*32+x]=c end end '
         'def line(x,y,x1,y1,c) while x<=x1 pixel(x,y,c) x+=1 end end '
         'def rect_fill(x,y,w,h,c) var j=0 while j<h line(x,y+j,x+w-1,y+j,c) j+=1 end end '
         'def rgb(r,g,b) return (r<<16)|(g<<8)|b end def clamp(n,l,h) return n<l?l:(n>h?h:n) end '
         'class Http def get(u,f) end end var http=Http() class Rotation def next() end def show() end end '
         'var rotation=Rotation() def min(a,b) return a<b?a:b end def max(a,b) return a>b?a:b end import gc import json')


def state(title, *, calm=False, progress=12):
    return json.dumps(json.dumps(dict(p=True, i='sample', t=title, g=progress, m=calm)))


def profile(runtime: Path, output: Path, *, node='node') -> dict:
    runtime, output = runtime.resolve(), output.resolve()
    if hashlib.sha256(runtime.read_bytes()).hexdigest() != RUNTIME_SHA256:
        raise ValueError('Runtime does not match the reviewed official upstream build')
    output.parent.mkdir(parents=True, exist_ok=True)
    results, frames = [], {}
    with tempfile.TemporaryDirectory(prefix='media-profile-', dir=output.parent) as directory:
        scratch = Path(directory).resolve()
        assert scratch.parent == output.parent and scratch.is_relative_to(output.parent)
        runner = scratch/'run.cjs'
        runner.write_text(RUNNER, encoding='utf-8')
        for version, name in [('2.1', 'now_playing.ax'), ('2.3', 'now_playing-v2.3.ax'), ('2.4', 'now_playing-v2.4.ax')]:
            source = minify(install_source((ROOT/name).read_text(encoding='utf-8'), STATE_URL))
            short = state('MIDNIGHT CITY - M83')
            inputs = [SETUP, 'gc.collect() var baseline=gc.allocated() print("BASELINE",baseline)',
                      'var code=""', *['code=code+'+json.dumps(source[i:i+120]) for i in range(0, len(source), 120)],
                      'p=compile(code)() code=nil gc.collect() print("RETAINED",gc.allocated()-baseline)',
                      'p.on_state('+short+',200) p.on_show() var i=0 while i<600 ms+=40 p.draw() if i%25==0 p.on_state('+short+',200) p.loop() end i+=1 end gc.collect() print("WARM",gc.allocated()-baseline)']
            for label, title, calm in [('MAX_NORMAL', 'M'*120, False), ('MAX_CALM', 'M'*120, True),
                                        ('SIGNED_CARD', ' '*120, True), ('TRIMMED', 'WAIT', False)]:
                inputs.append('ms+=40 p.on_state('+state(title, calm=calm, progress=24)+',200) p.on_show() p.draw() gc.collect() print("'+label+'",gc.allocated()-baseline)')
                inputs.append('capture=true p.draw() capture=false print("FRAME_'+label+'",json.dump(screen))')
            inputs += ['p.on_state("{\\"p\\":false}",200) p.draw() gc.collect() print("IDLE",gc.allocated()-baseline)']
            input_path = scratch/'input.json'
            input_path.write_text(json.dumps(inputs), encoding='utf-8')
            run = subprocess.run([node, str(runner), str(runtime), str(input_path)], cwd=scratch,
                                 capture_output=True, text=True, timeout=30)
            transcript = run.stdout+run.stderr
            (output.parent/f'upstream-media-{version}.log').write_text(transcript, encoding='utf-8')
            keys = ('RETAINED','WARM','MAX_NORMAL','MAX_CALM','SIGNED_CARD','TRIMMED','IDLE')
            matches = {k: re.search(r'\b'+k+r' (\d+)', transcript) for k in keys}
            if run.returncode or not all(matches.values()) or re.search(r'\b[a-z_]+_error:', transcript):
                raise RuntimeError(f'Upstream Berry rejected {name}; inspect its saved transcript')
            frames[version] = {k: json.loads(re.search(r'FRAME_'+k+r' (\[[^\n]+\])', transcript)[1])
                               for k in ('MAX_NORMAL','MAX_CALM','SIGNED_CARD','TRIMMED')}
            results.append({'version': version, 'source': name, 'rendered_fixture_bytes': len(source.encode()),
                            'rendered_fixture_sha256': hashlib.sha256(source.encode()).hexdigest(),
                            **{k.lower()+'_bytes': int(m[1]) for k, m in matches.items()}})
    assert frames['2.3'] == frames['2.4'], 'Native upstream framebuffer mismatch'
    report = {'runtime_source': RUNTIME_URL, 'runtime_sha256': RUNTIME_SHA256,
              'runtime': 'Official emcc Berry 1.1.0, Sep 26 2025 build, 64-bit integers',
              'node_version': subprocess.check_output([node,'--version'], text=True).strip(),
              'panel_requests': 0, 'not_device_budget_evidence': True, 'fixture_state_url': STATE_URL,
              'native_approved_compact_framebuffers_identical': True, 'native_frame_cases': list(frames['2.4']),
              'results': results, 'retained_reduction_from_approved_bytes': results[1]['retained_bytes']-results[2]['retained_bytes'],
              'warm_reduction_from_approved_bytes': results[1]['warm_bytes']-results[2]['warm_bytes'],
              'boundaries': 'Real upstream JSON/string modules; explicit clock/no-op HTTP/rotation fixtures. Graphics no-op during allocation sampling, bounded integer framebuffer for separate comparisons. Different precision, allocation and IRAM policy from ESP32.'}
    output.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    return report


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--node', default='node')
    args=parser.parse_args()
    print(json.dumps(profile(args.runtime,args.output,node=args.node), indent=2))
