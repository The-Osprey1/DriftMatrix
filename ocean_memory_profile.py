"""Optional upstream Berry retained-allocation comparison, without panel access.

Supply the official standalone runtime downloaded from RUNTIME_URL. Its exact
hash is checked; it is not bundled or downloaded by this tool. Node is needed
only for this optional profile, not for the normal renderer or test suite.
The upstream emcc build has a different allocator/configuration from the ESP32;
these values cannot be used to admit a resident installation.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import tempfile

from weather.minify_ax import minify

RUNTIME_URL = 'https://berry-lang.github.io/assets/js/berry.js'
RUNTIME_SHA256 = '8c59a4a6cdf297c79fef2b3201d7da349d24df14d89ce266b7a2b11c37ab337a'
ROOT = Path(__file__).resolve().parent
RUNNER = r'''
const fs=require('fs'), path=require('path'), vm=require('vm');
globalThis.require=require;
globalThis.__dirname=path.dirname(process.argv[2]);
globalThis.Module={print: s=>process.stdout.write(String(s)+'\n'),
 printErr:s=>process.stderr.write(String(s)+'\n')};
const inputs=JSON.parse(fs.readFileSync(process.argv[3],'utf8')).flatMap(s=>s.trimEnd().split(/\r?\n/));
let position=0;
globalThis.writeOutputText=async s=>process.stdout.write(String(s));
globalThis.waitLineText=async()=>{
 if(position>=inputs.length) process.exit(0);
 return inputs[position++]+'\n';
};
vm.runInThisContext(fs.readFileSync(process.argv[2],'utf8'),{filename:process.argv[2]});
'''


def profile(runtime: Path, output: Path, *, node: str = 'node') -> dict:
    runtime = runtime.resolve()
    if sha256(runtime.read_bytes()).hexdigest() != RUNTIME_SHA256:
        raise ValueError('Runtime does not match the reviewed official upstream build')
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(prefix='berry-profile-', dir=output.parent) as directory:
        scratch = Path(directory).resolve()
        # The context manager recursively cleans only this newly created child.
        assert scratch.parent == output.parent and scratch.is_relative_to(output.parent)
        runner = scratch / 'run.cjs'
        runner.write_text(RUNNER, encoding='utf-8')
        for version in ('3.1', '3.2'):
            path = ROOT / f'ocean-v{version}.ax'
            source = minify(path.read_text(encoding='utf-8'))
            inputs = [
                'var ms=0 var p=nil def now_ms() return ms end def pixel(x,y,c) end def line(x0,y0,x1,y1,c) end def hour() return 12 end class Shared def get(k) return nil end def age(k) return 0 end end var shared=Shared() import gc',
                'gc.collect() var baseline=gc.allocated() print("BASELINE",baseline)',
                'var code=""',
                *['code=code+'+json.dumps(source[i:i+120]) for i in range(0, len(source), 120)],
                'p=compile(code)() code=nil',
                'gc.collect() print("RETAINED",gc.allocated()-baseline)',
                'p.on_show() var i=0 while i<400 ms+=40 p.draw() if i%25==0 p.loop() end i+=1 end gc.collect() print("WARM",gc.allocated()-baseline)',
            ]
            input_path = scratch / 'input.json'
            input_path.write_text(json.dumps(inputs), encoding='utf-8')
            run = subprocess.run([node, str(runner), str(runtime), str(input_path)],
                                 cwd=scratch, capture_output=True, text=True, timeout=30)
            transcript = run.stdout + run.stderr
            retained, warm = re.search(r'RETAINED (\d+)', transcript), re.search(r'WARM (\d+)', transcript)
            (output.parent / f'upstream-{version}.log').write_text(transcript, encoding='utf-8')
            if run.returncode or not retained or not warm or re.search(r'\b[a-z_]+_error:', transcript):
                raise RuntimeError(f'Upstream Berry rejected {path.name}; inspect its saved transcript')
            results.append({'version': version, 'source': path.name,
                            'minified_bytes': len(source.encode('utf-8')), 'minified_sha256': sha256(source.encode('utf-8')).hexdigest(),
                            'retained_bytes': int(retained[1]), 'after_400_draws_bytes': int(warm[1])})
    report = {'runtime': 'Official emcc Berry 1.1.0, Sep 26 2025 build, 64-bit integers',
              'runtime_source': RUNTIME_URL, 'runtime_sha256': RUNTIME_SHA256,
              'node_version': subprocess.check_output([node, '--version'], text=True).strip(),
              'not_device_budget_evidence': True, 'panel_requests': 0, 'results': results,
              'retained_reduction_bytes': results[0]['retained_bytes'] - results[1]['retained_bytes'],
              'boundaries': 'Natives are explicit no-op/clock/shared fixtures. Different precision, allocation and IRAM policy from the ESP32; source-level pixel parity is verified separately with float32.'}
    output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--node', default='node')
    args = parser.parse_args()
    print(json.dumps(profile(args.runtime, args.output, node=args.node), indent=2))
