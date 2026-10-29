# Moon 2.4: approved art with compact resident storage

The separate `weather/moon-panel-v2.4.ax` candidate preserves the approved 2.3
artwork, astronomy and lifecycle. The installed-source reference
`weather/moon-panel.ax` and approved `weather/moon-panel-v2.3.ax` are unchanged.
The measured Ocean 3.2 resident set had only 1,644 bytes remaining below its
73,728-byte shared-VM ceiling, so adding the larger Moon source requires a fresh
compiled-memory check.

## Implementation

- Eight disc half-widths retain their exact binary32 values in 32 bytes. The
  16 row-span values use 16 bytes, without changing any geometry calculation.
- Meteor, star, hour-tint and computed nebula palettes retain all 24 RGB bits
  in three-byte entries. Tint choices, mixing order and dimming are unchanged.
- Up to 64 nebula runs use three bytes each. Each packed value is below 2^17;
  no coordinate or palette bit is discarded. `nn` still admits only initialized
  entries. Unused capacity is never read, regardless of allocation fill.
- Eight packed disc rows use four bytes each. At most three nine-bit segments
  fit in each value, below 2^27. Every row is written before it is drawn.
- After creating the one page, the source retires its private constructor so
  one-time constants can be collected. Each load defines and initializes a
  fresh class. The six maria positions remain a list: native bytes do not
  provide an iterator on this firmware.

Typed accesses follow the
[firmware byte library](https://github.com/Blueforcer/awtrix-ng/blob/v1.2.2/lib/berry/src/be_byteslib.c).
The runtime hook list excludes the constructor, and the loader anchors the
returned instance; see
[ScriptApp](https://github.com/Blueforcer/awtrix-ng/blob/v1.2.2/src/core/script/ScriptApp.cpp)
and [BerryVM](https://github.com/Blueforcer/awtrix-ng/blob/v1.2.2/src/core/script/BerryVM.cpp).
No firmware option, VM ceiling, native API or astronomy approximation was changed.
Some private drawing helpers have shorter names to keep the minified source
within the existing upload cap; firmware entry-hook names are unchanged.

## Source fidelity

**3,332 local tests passed in 94.31 seconds**, with zero failures, errors or skips.
The 47 new focused cases execute the actual approved and compact sources:

- Every illumination percentage, waxing/waning direction and three eclipse
  intensities, including terminator geometry, surface texture and edge fades.
- All 64 nebula drift positions and garbage in unused capacity.
- Hour-tint and dimming boundaries, including an entire 80-second breathing cycle.
- Astronomy refresh, NTP loss/recovery, meteor boundaries, late/interrupted visits,
  complete 11-second visits and the larger-font label fallback.
- Raw/minified parity, constructor retirement, independent reloads and the 12,288-byte cap.

Native compilation/execution caught an invalid intermediate experiment that
iterated a byte buffer. That experiment is not in the candidate. The subset model
now rejects byte iteration too, instead of borrowing Python's bytearray iterator;
a regression case distinguishes supported list iteration from the rejected bytes.

The reproducible comparison contains **550 identical frame pairs** across five
illustrative states and checks **2,750 intermediate draws**. Text uses captured
glyphs plus the project font model. This is source execution, not device imagery.

```powershell
& 'C:/Users/devyn/anaconda3/python.exe' -X utf8 moon_compact_review.py --output figures/moon-compact
```

## Independent upstream runtime check

The official standalone emcc Berry 1.1.0 build compiles and executes all three
sources through creation, 600 draws and clock-loss/recovery, using explicit
no-op graphics/text and clock fixtures:

| Upstream retained allocation after collection | Original 2.1 | Approved 2.3 | Compact 2.4 |
| --- | ---: | ---: | ---: |
| After creation | 26,765 bytes | 29,518 bytes | 25,213 bytes |
| After 600 draws | 26,973 bytes | 29,862 bytes | 25,557 bytes |
| After clock loss/recovery | 26,914 bytes | 29,803 bytes | 25,498 bytes |

The reduction is **4,305 bytes relative to 2.3**, and **1,552 relative to 2.1**.
These are upstream measurements, not ESP32 budget evidence. Precision, allocation
and IRAM policy differ; the actual device must still pass the unchanged 75% rule.

`moon_memory_profile.py` requires a local copy of
[the official runtime](https://berry-lang.github.io/assets/js/berry.js), pinned to
SHA-256 `8c59a4a6cdf297c79fef2b3201d7da349d24df14d89ce266b7a2b11c37ab337a`.
It downloads nothing and has no panel transport. Node and that optional runtime
are not dependencies of the worker fleet or normal tests.

```powershell
& 'C:/Users/devyn/anaconda3/python.exe' -X utf8 moon_memory_profile.py --runtime C:/path/to/berry.js --output figures/moon-compact/upstream-runtime-profile.json
```

## Device adoption remains separate

The final minified source is **12,269 bytes**, SHA-256
`44a9b9b4901fc332be37fd968d17a4f782e15c15058736480d07cdcb85e8514c`.
Its source-derived upload floor is **42,942 contiguous bytes**. Source size and
upstream profiles do not establish available contiguous memory or compiled cost.

This implementation and its validation make no live panel requests, writes,
reboots, configuration/task changes or resident adoption. A later device trial
requires passing CI at a pinned head, fresh snapshots/source proof, the canonical
maintenance/Nightkeeper policy, explicit rollback pins, verified boot allocation,
draw/FPS checks and physical acceptance. Ocean's missed physical test remains
pending; approval of the visual direction does not substitute for observing LEDs.
