# Now Playing 2.4: compact storage for approved media behavior

The separate `now_playing-v2.4.ax` source preserves approved 2.3 record artwork,
title scrolling, reduced-motion cards, progress and lifecycle behavior. Original
`now_playing.ax`, approved `now_playing-v2.3.ax` and the default worker template
selection are unchanged. The new template is packaged as a sibling resource and
can be selected explicitly with the existing canonical `script_path` option.

## Implementation

- Cache title columns as bytes: each column needs only five bits. The accepted
  120-character limit produces at most 719 columns; no title content is truncated.
- Store each packed 15-bit glyph in two bytes. Groove and label-ring positions
  use one byte each. Replace the 0..15 identity lookup table with its index.
- Store each progress colour in three bytes, preserving all 24 RGB bits and the
  23-cell rounding, leader timing and gutter.
- Store reduced-motion card entries in signed two-byte values. Valid offsets fit
  below 23,040, while unusual space-heavy inputs can produce the reference's -1
  sentinel; signed reads preserve that behavior without wrapping it to 65,535.
- Create one page, then retire its private constructor so one-time constants can
  be collected. Each independent load initializes a fresh class and buffers.

No request timing, JSON validation, stale-media timeout, record-step timing,
rotation completion, user-navigation guard or metadata limit was changed. The
small byte append operation follows the integer widths/endian contract in the
[firmware byte library](https://github.com/Blueforcer/awtrix-ng/blob/v1.2.2/lib/berry/src/be_byteslib.c).
The subset model implements that operation with independent width, masking,
return/chaining and invalid-type cases; it remains a subset of the firmware VM.

The loader anchors the returned instance, and runtime hooks exclude the constructor;
see [BerryVM](https://github.com/Blueforcer/awtrix-ng/blob/v1.2.2/src/core/script/BerryVM.cpp)
and [ScriptApp](https://github.com/Blueforcer/awtrix-ng/blob/v1.2.2/src/core/script/ScriptApp.cpp).
No firmware settings or memory limits were changed.

## Fidelity and worker acceptance

**3,417 local tests passed in 124.97 seconds**, zero failures, errors or skips.
This adds 81 focused media/model cases, two additional real-fleet cases and two
bounded-polling regressions.

The 81 new focused cases compare the actual approved and compact sources through
the documented preview adapters. Coverage includes all accepted glyphs, randomized
titles, full maximum-length visits, every progress bucket, signed card offsets,
invalid JSON/status/type/length inputs, late and hidden draws, stale exit, track
identity changes, navigation, motion-mode changes, minification and independent loads.

The preview model substitutes only module imports, JSON decoding/exception handling
and the HTTP callback boundary. Graphics and float32 are models. It does not prove
physical pixels or firmware timing. The comparison checks 2,207 completed draws
and saves **666 identical frame pairs** over six illustrative cases. After a return
request it holds the last frame, rather than simulating a different app's rotation.

The real five-worker process/HTTP/state acceptance is additionally parameterized
for approved 2.3 and compact 2.4 in ordinary and reduced motion. It checks actual
worker loops, verify-only source selection, media JSON, command receipts, guarded
pushes and clean shutdown. WinRT/counters and upstream feeds use explicit fixtures;
the actual Berry execution is covered separately. CI also checks the installed
2.4 sibling resource outside the checkout. See `HOST_FLEET_ACCEPTANCE.md` for the
existing process boundary and fixture limits.

```powershell
& 'C:/Users/devyn/anaconda3/python.exe' -X utf8 media_compact_review.py --output figures/media-compact
```

## Independent upstream execution

The official standalone emcc Berry 1.1.0 build uses real JSON/string modules and
explicit clock, HTTP and rotation fixtures. Graphics are no-ops during allocation
sampling; a separate bounded integer framebuffer compares the actual interpreted
drawing code. Four cases match exactly between 2.3 and 2.4: maximum normal title,
maximum reduced-motion title, signed-card input and a trimmed short title.

| Upstream allocation after collection | Original 2.1 | Approved 2.3 | Compact 2.4 |
| --- | ---: | ---: | ---: |
| After creation | 16,399 | 19,322 | 16,022 |
| After 600 draws with ordinary title | 19,046 | 21,969 | 17,043 |
| Maximum-width normal title | 30,062 | 32,985 | 17,797 |
| Maximum-width reduced-motion title | 30,059 | 33,462 | 17,820 |
| Trimmed short title | 17,755 | 20,678 | 16,868 |
| Idle after trimming | 17,631 | 20,573 | 16,731 |

Creation retains **3,300 fewer bytes than 2.3**; the ordinary warm case saves 4,926,
and the maximum reduced-motion case saves 15,642. These are upstream measurements,
not ESP32 admission evidence. Precision, allocation and IRAM policy differ. All
allocation samples follow collection; they do not measure transient peaks.

The optional tool requires a local copy of
[the official runtime](https://berry-lang.github.io/assets/js/berry.js), pinned to
SHA-256 `8c59a4a6cdf297c79fef2b3201d7da349d24df14d89ce266b7a2b11c37ab337a`.
It downloads nothing, opens no network connection and does not need the real
bridge. Its TEST-NET URL is a rendered-string fixture. Node and this optional
runtime are not dependencies of workers or normal tests.

```powershell
& 'C:/Users/devyn/anaconda3/python.exe' -X utf8 media_memory_profile.py --runtime C:/path/to/berry.js --output figures/media-compact/upstream-runtime-profile.json
```

## Adoption boundary

Minified template: **9,511 bytes**, SHA-256
`f0a71c5c4c04acc029559dd5aff3b81c50ed6d9d1b80578484c7b96b717248da`.
Rendered TEST-NET fixture: **9,532 bytes**, SHA-256
`3385bdc3e057ebbb73ea0da9331830e6b7e22b33d3e50f11311301417d942252`.
These are different identities. A real trial must render the actual bridge URL,
pin those exact bytes and compute its contiguous upload floor; never upload the
unrendered placeholder or substitute this fixture's hash for the live candidate.

This implementation makes no live panel requests, writes, reboots or host/config/task
changes. A guarded trial still requires CI at a pinned head, fresh snapshots and
exact resident identities, coordinated host template selection, explicit rollback
pins, canonical maintenance/Nightkeeper policy, measured compiled cost within the
unchanged 75% ceiling, real draw/bridge/FPS checks and physical acceptance. Moon
2.4 also remains staged; Ocean's missed physical test remains pending.

The initial Windows/Python 3.12 CI run exposed a permission denial while the test
observer read worker-owned Focus state. The polling helper now retries that read
condition at its original bounded deadline. Separate regressions prove recovery
after a transient denial and timeout on permanent denial. Production persistence
and renderer behavior are unchanged.
