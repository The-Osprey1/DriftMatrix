# Ocean 3.2: retain the art, reduce resident allocation

Ocean 3.1 compiled successfully in its October 28 hardware trial, but used
25,313 bytes of VM heap. The full resident set reached 80,604 bytes against the
73,728-byte ceiling, so the reviewed swap tool restored the previous Ocean.
This separate 3.2 candidate addresses retained state and constructor data.
The installed `ocean.ax` reference and approved `ocean-v3.1.ax` are unchanged.

## Implementation

- Six visitors share one lifecycle dispatcher and a 66-byte rule table. Their
  spawn order, LCG consumption, fractional speeds, exit bounds, timeout rules,
  deadlines and hidden-time behavior are retained. Deadlines remain integers;
  random delays are not rounded to seconds.
- The 128 sine samples use a 512-byte binary32 buffer; beam coefficients use
  90 bytes of signed 16-bit data. This is exact storage, not a fixed-point
  approximation. Palette colours retain all 24 RGB bits. Masks, seafloor and
  kelp data use compact byte buffers too.
- The source creates one page, then clears its private class constructor after
  initialization. The app retains the initialized buffers and runtime methods;
  the constructor's constants can be collected. Each reload/reboot defines a
  fresh class and constructor. This source is intentionally a one-page factory.

The official [firmware configuration](https://github.com/Blueforcer/awtrix-ng/blob/v1.2.2/lib/berry/berry_conf.h)
already moves instruction bytecode/line information into IRAM on this ESP32;
constants and object data therefore matter independently of source length.
No firmware setting or VM ceiling was changed. The
[host lifecycle](https://github.com/Blueforcer/awtrix-ng/blob/v1.2.2/src/core/script/ScriptApp.cpp)
caches draw/setup/loop and visibility hooks, not the class constructor. Its
[loader](https://github.com/Blueforcer/awtrix-ng/blob/v1.2.2/src/core/script/BerryVM.cpp)
executes the chunk, anchors its returned instance and treats top-level variables
as chunk locals. Retiring the constructor does not remove a runtime app hook.

## Source fidelity and model boundaries

**3,285 local tests passed in 95.85 seconds**, with zero failures, errors or
skips, including **69 new cases**. The new comparisons execute the approved 3.1
and compact 3.2 sources through the same float32 subset model:

- Seven visible minutes with every encounter and seed/state comparison.
- Every creature at both edges in both directions; simultaneous deadlines with
  multiple seeds; hidden intervals, large wall-clock starts and delayed frames.
- Exact palettes, float samples and signed coefficients, scheduling, complete
  state, raw/minified execution and arbitrary initial byte-buffer fill.
- Independent reloads after constructor retirement and unchanged runtime draws.

The saved 30-second comparison samples 250 frames from 40 ms completed draws;
every pair is pixel-identical. It is an offline source comparison, not a physical
capture. Regenerate with:

```powershell
& 'C:/Users/devyn/anaconda3/python.exe' -X utf8 ocean_compact_review.py --output figures/ocean-compact
```

The subset model now implements only the typed byte operations needed by this
candidate, with boundary, endian, type and binary32 regression cases based on the
[firmware byte library](https://github.com/Blueforcer/awtrix-ng/blob/v1.2.2/lib/berry/src/be_byteslib.c).
It also rejects duplicate local declarations that the native compiler rejects;
the native check caught two such errors in an intermediate draft. Empty classes
without constructors and assignment to an existing class member are modeled for
the constructor-release contract. This remains a subset, not the firmware VM.

## Independent upstream runtime check

The official standalone upstream Berry 1.1.0 emcc build also compiled and ran
both final sources, including 400 draws. With explicit no-op drawing and clock/
shared fixtures, retained allocation after collection was:

| Upstream emcc measurement | Approved 3.1 | Compact 3.2 |
| --- | ---: | ---: |
| Retained bytes after creation | 30,122 | 20,492 |
| After 400 draws and collection | 30,934 | 21,640 |

This **9,630-byte relative reduction is not an ESP32 measurement**. The upstream
build has a different precision/allocator/IRAM configuration. It cannot admit an
installation or establish physical pixels, frame rate or long-term stability.

The optional `ocean_memory_profile.py` tool takes a local copy of
[the official runtime](https://berry-lang.github.io/assets/js/berry.js), requires
its reviewed SHA-256 `8c59a4a6cdf297c79fef2b3201d7da349d24df14d89ce266b7a2b11c37ab337a`,
and runs it under Node. It downloads nothing and never connects to the panel.
Node/the runtime are not required by normal workers or the test suite.

```powershell
& 'C:/Users/devyn/anaconda3/python.exe' -X utf8 ocean_memory_profile.py --runtime C:/path/to/berry.js --output figures/ocean-compact/upstream-runtime-profile.json
```

## Adoption remains guarded

The minified candidate is **9,506 bytes**, SHA-256
`677c2bbf4b08355a184edf31252a5ba5ffb73ef2695311c42697e4d2a5a582e0`.
Its source-derived upload floor is **33,271 contiguous bytes**. Actual available
block and compiled VM use still need fresh device measurements. With the other
residents unchanged, Ocean needs to fit within 18,437 bytes of VM heap; this
revision has not yet demonstrated that on the clock.

The approved artwork is preserved by source comparison; no new visual direction
is proposed. Fresh snapshots/identity, pinned rollback, a controlled trial,
the 75% shared-VM check, actual draw FPS and physical acceptance remain before
adoption is complete. This implementation/profile work made no live panel
requests, writes, reboots, host/configuration/task changes or resident adoption.
