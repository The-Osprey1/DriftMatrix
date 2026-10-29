"""Optional hash-pinned upstream Berry profile; no downloads or panel transport.

Natives are explicit clock/no-op fixtures. Upstream allocation and precision differ
from the ESP32; results cannot establish admission under the device memory policy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

from ocean_memory_profile import RUNTIME_SHA256, RUNTIME_URL, RUNNER
from weather.minify_ax import minify

ROOT = Path(__file__).resolve().parent
SETUP = ('var ms=0 var yr=2026 var p=nil '
         'def now_ms() return ms end def epoch_ms() return 1793066400000+ms end '
         'def year() return yr end def hour() return 22 end def minute() return 0 end '
         'def clear() end def pixel(x,y,c) end def line(x0,y0,x1,y1,c) end '
         'def text(x,y,s,c) end def text_ink_width(s) return size(s)*4 end import gc')


def profile(runtime: Path, output: Path, *, node: str = 'node') -> dict:
    runtime, output = runtime.resolve(), output.resolve()
    if hashlib.sha256(runtime.read_bytes()).hexdigest() != RUNTIME_SHA256:
        raise ValueError('Runtime does not match the reviewed official upstream build')
    output.parent.mkdir(parents=True, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(prefix='moon-profile-', dir=output.parent) as directory:
        scratch = Path(directory).resolve()
        assert scratch.parent == output.parent and scratch.is_relative_to(output.parent)
        runner = scratch / 'run.cjs'
        runner.write_text(RUNNER, encoding='utf-8')
        for version, filename in [('2.1', 'moon-panel.ax'), ('2.3', 'moon-panel-v2.3.ax'), ('2.4', 'moon-panel-v2.4.ax')]:
            source = minify((ROOT / 'weather' / filename).read_text(encoding='utf-8'))
            inputs = [SETUP, 'gc.collect() var baseline=gc.allocated() print("BASELINE",baseline)',
                      'var code=""', *['code=code+'+json.dumps(source[i:i+120]) for i in range(0, len(source), 120)],
                      'p=compile(code)() code=nil gc.collect() print("RETAINED",gc.allocated()-baseline)',
                      'p.on_show() var i=0 while i<600 ms+=40 p.draw() if i%25==0 p.loop() end i+=1 end gc.collect() print("WARM",gc.allocated()-baseline)',
                      'yr=1970 ms+=61001 p.loop() p.draw() yr=2026 ms+=61001 p.loop() p.draw() gc.collect() print("RECOVERED",gc.allocated()-baseline)']
            input_path = scratch / 'input.json'
            input_path.write_text(json.dumps(inputs), encoding='utf-8')
            run = subprocess.run([node, str(runner), str(runtime), str(input_path)], cwd=scratch,
                                 capture_output=True, text=True, timeout=30)
            transcript = run.stdout + run.stderr
            (output.parent / f'upstream-moon-{version}.log').write_text(transcript, encoding='utf-8')
            matches = {key: re.search(r'\b'+key+r' (\d+)', transcript) for key in ('RETAINED', 'WARM', 'RECOVERED')}
            if run.returncode or not all(matches.values()) or re.search(r'\b[a-z_]+_error:', transcript):
                raise RuntimeError(f'Upstream Berry rejected {filename}; inspect its saved transcript')
            results.append({'version': version, 'source': 'weather/'+filename,
                            'minified_bytes': len(source.encode('utf-8')),
                            'minified_sha256': hashlib.sha256(source.encode('utf-8')).hexdigest(),
                            **{key.lower()+'_bytes': int(match[1]) for key, match in matches.items()}})
    report = {'runtime_source': RUNTIME_URL, 'runtime_sha256': RUNTIME_SHA256,
              'runtime': 'Official emcc Berry 1.1.0, Sep 26 2025 build, 64-bit integers',
              'node_version': subprocess.check_output([node, '--version'], text=True).strip(),
              'panel_requests': 0, 'not_device_budget_evidence': True, 'results': results,
              'retained_reduction_from_approved_bytes': results[1]['retained_bytes']-results[2]['retained_bytes'],
              'retained_reduction_from_original_bytes': results[0]['retained_bytes']-results[2]['retained_bytes'],
              'boundaries': 'Explicit no-op graphics/text, clock fixtures. Different allocator, precision and IRAM policy; actual ESP32 budget, pixels and FPS require a guarded device trial.'}
    output.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--node', default='node')
    args = parser.parse_args()
    print(json.dumps(profile(args.runtime, args.output, node=args.node), indent=2))
