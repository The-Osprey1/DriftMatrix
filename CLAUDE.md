# AWTRIX NG panel project

One physical **32 x 8 RGB LED matrix** driven by **AWTRIX NG 1.1.2** on an ESP32.
Panel apps are Berry scripts (`.ax`); the PC side is Python senders plus pytest
suites that model the on-device logic.

This file states the current rules. The dated measurements behind them, and
every claim that has been superseded, are in `PANEL_HISTORY.md`.

## Hardware and device facts

| Fact | Value |
| --- | --- |
| Panel URL | `http://192.168.1.90` (mDNS `pixelclock.local` has advertised a stale second address — prefer the raw IP) |
| Firmware / board | AWTRIX NG `1.1.2`, `awtrixng`, ESP32-D0WD-V3 4 MB, uid `680947376cfc` — a **replacement board since 2026-10-10** (the first, uid `680947376e9c`, died; see `PANEL_HISTORY.md` §14). Ulanzi TC001 pin map, CP210x USB-serial (COM4 when plugged into the PC) |
| Network | **static** `192.168.1.90`/24, gateway and DNS `192.168.1.1` (`netStatic: true` in `/api/v1/system`; the first board got .90 by DHCP), hostname `pixelclock`, Wi-Fi `<YOUR_WIFI_SSID>` |
| Geometry | `panelWidth 32`, `panels 1`, `panelStart bottomRight`, `panelWiring rows`, serpentine off, mirror off, rotate off |
| Coordinates | x = 0..31, y = 0..7, origin top-left in script space |
| Script heap | `scriptHeapBudgetBytes 98304`, internal pool — shared by every installed script |
| Auth | `authEnabled false` — no credentials needed on the LAN |
| Timezone | `PST8PDT,M3.2.0,M11.1.0` (America/Los_Angeles), NTP `pool.ntp.org` |
| Location (weather apps) | lat `33.87529`, lon `-117.56644` |

## Panel memory — the rules that bite

Free heap is not the scarce resource. The **largest contiguous free block**
(`largestFreeBlockBytes`) is: it decides whether a script installs, whether a
TLS handshake fits, and whether an animated push survives. Read numbers from
the panel rather than guessing. At boot, `GET /api/v1/logs` logs
`vm heap +N (shared N)`, `max block N` and `install low-water N` for each
script.

- **What is installed** (`GET /apps` and the boot log, 2026-10-10, replacement board). The repo is not the device:

  | Script | Repo source | Bytes (repo-derived for moon/ocean) | `meta.version` | VM heap at boot |
  | --- | --- | --- | --- | --- |
  | `moon` | `weather/moon-panel.ax` (v2.1), minified | 10,918 | 2.1 | 21,907 |
  | `nightkeeper` | `nightkeeper.ax`, minified | 2,685 | 3.0 | 4,081 |
  | `nowplaying` | `now_playing.ax`, minified + PC LAN URL | 7,372 | 2.1 | 14,304 |
  | `ocean` | `ocean.ax` (v2), as-is | 10,434 | *(no header)* | 26,169 |

  Every script was installed by hash from the repo on 2026-10-10;
  `nightkeeper` and `nowplaying` read back byte-exact, and `moon` v2.1 read
  back matching its pin in `script_swap.py --check-only`. `ocean` has not
  been read back on this board. The previous moon, v2.0, is kept as
  `weather/moon-panel-v2.0.ax` (minified sha256 `f6b17ba2…`) for rollback.

  This `moon` is the old tracker (nebula, eclipses, glide to an info screen),
  restored at the user's request in place of `sunset`, which has been off
  since 2026-10-04. Root `moon.ax`, `weather/moon.ax` and every other
  `weather/*.ax` (`wx`, `wxfeed`, `wxnow`, …) are kept but not installed (404).
  `panel_snapshot.REPOSITORY_SOURCES` maps installed scripts to sources. Live
  weather is the *pushed* `weatherbrief` and `aqibrief`. Pushed apps live in
  RAM and vanish on reboot; their senders re-push within minutes. Always
  check `GET /apps`.
- **Budget: 80,788 of 98,304 B (82%) after boot.** That is the boot log's
  `shared` total: the four scripts' 65,972 B plus 14,816 B the VM holds
  before any script loads. Compare `shared` with the budget, not the
  per-script sum. Treat ~75% (73,728 B) as the ceiling. The set is already
  over it, so anything added needs something removed. The panel panicked on
  2026-10-04 at 14:51 with five scripts at 85%, during a companion push; the
  root cause is not established.
- **Cost is not proportional to source.** VM heap has run from 0.4× to
  2.5× the source size, because tables built in `init()` dominate. Only a
  post-reboot boot log is honest; install-time figures include compile
  overhead.
- **The block decays fast and only a reboot restores it.** What counts is
  the block left after the last script loads at boot; it can rise between
  scripts (2026-10-06: 65,524 → 77,812 → 55,284 → 32,756). So an install that
  fits a bare panel can fail on a populated one. It then fell to 18,420
  within 26 minutes, and to ~6,600–8,700 after two days. Deleting a script
  does not give the block back.
- **An install needs ≥ 3.5 × the minified source as one contiguous block.**
  3.33× has failed with OOM; 3.75× has worked. The ~2× in `install
  low-water` is the heap dip, not the contiguous need. Upload minified
  (`weather/minify_ax.py`) right after a reboot. A ~10 KB script (`moon`,
  `ocean`) needs ~37 KB, more than the post-boot block, so replacing one is:
  delete, reboot, PUT, reboot. With five scripts loaded, a 10 KB PUT was
  refused `insufficientStorage`, nothing stored. **A sender that installs its
  own script must minify too** (`now_playing_bridge.install_source()` does);
  a raw install once cost the media bridge 1h51m of retries.
- **A failed install still writes the source; the error is from compiling
  it.** The PUT returns 200 with a non-null `error`, and `GET /apps` shows
  `error: {'message': 'out of memory'}`. Reboot (the stored source then
  compiles) and re-run the install; never assume the old code is still there.
- **Readback of a ~10 KB script depends on the block, not on uptime.**
  `GET /apps/script/{name}` worked with a block of 29,684 or more (including
  at 40 h of uptime) and failed with 22,516 or less (as early as 15 minutes).
  With the current four scripts the block first dropped below ~30 KB about
  10 minutes after the 2026-10-06 boot. `panel_snapshot.py` rebuilds the
  larger sources from the repo (`minify()` is deterministic and matched every
  installed SHA-256 it could check).
- **Identity check: `meta.version` from `GET /apps`, not source comparison.**
  Known gap: `ocean.ax` has no `# @name`/`# @version` header, so its meta is
  empty and the check cannot work for it. Add the header the next time ocean
  is reinstalled; adding it now would make the repo differ from the panel.
- **HTTPS does not fit** with the scripts resident (`script http: heap too
  tight for TLS`). Use `http://` for public read-only APIs; TLS needs a
  script removed first.
- **Scene scheduling.** `sunset` (when installed) publishes `shared` key
  `sunset.phase` (`day` / `sunset` / `night`). `ocean` shows only by day and
  falls back to fixed hours (07–17) without a fresh phase, which is the case
  while `sunset` is off. `moon` stays in the loop all day: night-only was
  tried, and the user wanted the phase visible at any hour.
  `test_scene_schedule.py` keeps the key and values in agreement.

## Visual system: the Now Playing standard (2026-10-03)

The layout is unchanged from calm glance — icon x0–7, black gutter x8, one
value in PEARL on rows 1–5, row 7 a meter — but the user set Now Playing as
the quality bar for every page ("a weird face thing" and flat icons were
rejected). What that means in practice:

- **Shaded objects, not flat silhouettes.** Three or more tones per thing
  (lit edge, body, shadow), like the record's rim/body/label.
- **One palette per page,** carried from icon to rail. An empty rail is a
  dark tint of its own colour (`TRACK_TINT`), never grey.
- **Motion with a purpose:** comets along a path (`draw_comet`, the record's
  groove highlight), rotating glow, breathing, falling drops — never wrap-
  around jumps or blinking on/off.
- **Rails** come from `draw_level_rail` / `draw_rich_rail`: a fill rising
  into a PEARL leader over a tinted track. Read them in tests with
  `read_meter`, `meter_fill`, `rail_survives_brightness_one`.
- **Brightness 1:** the silhouette must survive; shading may fade, as Now
  Playing's dark rim does. Shaded icons are tested at ≥60% of lit pixels
  surviving the 174 threshold.
- **Budgets still bind:** pushed pages < 2,048 B, most icon GIFs ≤ 1 KB,
  delays multiples of 120 ms, no identical consecutive frames (the GIF
  encoder merges them and frame-count validators then fail).
- **Preview before shipping.** Every redesign was rendered as an animated
  GIF and approved by the user before going live; keep doing that.

Per-page designs and the history of rejected attempts are in
`scene-design-qa.md`.

Planes (`sky_watch.py`: adsb.lol, falling back to adsb.fi open data), Quakes
(`quake_watch.py`, USGS) and Game Mode (`game_mode.py`) run inside the Utility
Hub, switched by the `planes`, `quakes` and `game_mode` sections of
`%LOCALAPPDATA%\AWTRIX\utility_hub.json`. All three are alert-only and pace
themselves (Planes 20 s while a plane is overhead and 60 s when clear;
Quakes 60 s; Game Mode 5 s), because the hub ticks every service every few
seconds. adsb.lol still answers 429 now and then at that cadence. Planes hides light
aircraft by default: Corona Municipal is ~2 nm away. The two feed apps
verify HTTPS against the Windows store via `panel_scene.https_get` (certifi
fails here).

## Pushed apps

These facts were verified in the firmware source *and* on the panel:

- Body cap is **8192 bytes** (`413 payloadTooLarge` above it). There is no
  icon byte cap and no GIF frame cap. Realistic art runs ~100 B/frame.
- **Only two icon widths are safe: ≤8px or exactly 32.** Anything in between
  is blitted *last* (painting over the text) and only indents text by 9px.
- **Every `draw` primitive is static**: `bitmap`, `barChart`, `lineChart`,
  `progress`. There is no time parameter in `DrawOp`, so anything that must
  move belongs in the GIF.
- **120 ms is the only clean frame delay.** The render tick is 24 ms and GIF
  delays quantise to 10 ms; they first coincide at 120. Anything else beats
  2-tick/3-tick and visibly judders. `weather_canvas.to_gif` refuses
  non-multiples.
- **A full-width 32×8 GIF is legal and becomes the background,** with text
  and `draw` ops composited on top. It is the only way to animate the whole
  panel, and usually the wrong trade: a resident one costs block roughly in
  proportion to its decoded size (−3 KB to −10 KB measured). **Getting it
  wrong panics the panel; it does not refuse.** Budget **10 × the payload's
  bytes** in contiguous block (`release_canary.ANIMATION_BLOCK_RATIO`; worst
  measured 8.2, rounded clear of it because the failure is a crash). With
  32,756 the best post-boot block, a 7 KB full-width animation cannot fit:
  that path is closed, not merely gated.
- **The cost is the GIF decoder, not the width.** A static full-bleed `draw`
  bitmap costs 0 block (`meridian_push.py` holds the panel for 1,149 B).
  Refresh it by pushing a new static frame. Prefer an 8×8 icon GIF plus a
  static `draw` lane unless the animation genuinely carries information, and
  re-measure before animating any full-width page.
- `overlay` gives six firmware-rendered precipitation animations for ~18
  bytes, drawn on top of everything.
- At **brightness 1 the panel is 1 bit per channel**, threshold source value
  174. Colour-only distinctions vanish in the dark, so icon meaning must live
  in silhouette. (Mostly moot here — nightkeeper blacks the panel out at night.)
- **Pushes are not free.** Re-push only on change plus a keep-alive (the
  companion uses 240 s), and give any new pushed page the same treatment.

## Frame rate

- Target ~40 fps. Keep `draw()` tiny and deterministic; do parsing, HTTP, and
  JSON work in `loop()` only.
- Each native `pixel()` call costs ~50–60 µs and Berry ops are slow, so FPS
  tracks drawing calls per frame. The firmware's own per-frame work is
  ~14 ms, so 42 FPS needs `draw()` ≲10 ms. Profile with a temporary build
  that sums `now_ms()` per section and `log()`s it.
- **`ocean` is now the slow page** (28–40 FPS, 2026-10-10). `moon` v2.1
  holds 40–42 since 2026-10-10; v2.0 averaged 25 (minimum 22, below 40 in all
  120 on-screen samples, 2026-10-07/08). `nowplaying` ran ~36. The v2.1
  method — a float32-faithful emulator (`weather/berry_lite.py`,
  `weather/moon_panel_sim.py`) proving pixel identity, then caching per-phase
  work out of `draw()` — is the template for ocean.

## REST API (all under `http://192.168.1.90/api/v1`)

- `GET /device`, `GET|PATCH /settings`, `GET|PATCH /display`, `GET /display/screen`, `GET /logs`
- `GET|PUT /system` (Wi-Fi, network, timezone, hostname, panel geometry, pins). **PUT, not PATCH (405)**, and it merges: send only the keys to change, as the web UI does; Wi-Fi is untouched when omitted. `GET /system?secrets=1` includes secrets. Geometry and network keys need a reboot.
- `PUT /apps/order` `{"order": [...], "disabled": [...]}` (saved as `/apploop.json`); `GET /system/wifi-scan`
- `GET /apps`, `GET /apps/active`, `PUT /apps/active` `{"name": …, "fast": …}`, `DELETE /apps/{name}`
- `GET|PUT /apps/script/{name}`: script source, `text/plain; charset=utf-8`.
  PUT returns JSON with an `error` field; a non-null `error` means the device
  rejected the Berry source even though the HTTP status is 200. **Always check it.**
- `PUT /apps/pushed/{name}`: push or refresh a pushed app (what every sender uses)
- `POST /device/reboot`: about 9 s
- `PATCH /display` `{"power":true|false}`; `PATCH /settings` `{"brightness":N,"autoBrightness":false}`

`DELETE /apps/{name}` removes *scripts* too, so inspect `origin` first before
deleting anything (see `remove_legacy_pushed_app` in `now_playing_bridge.py`).

## Panel writes go through one gate (2026-10-06)

- **Every panel-mutating request goes through `panel_gate.mutation(source,
  op, app, body=…)`**: the Hub (`PanelClient._mutate`, source `hub`,
  maintenance reboots included), Now Playing and its bridge, PC stats, the
  weather and AQI briefs, `meridian_push`, `rotation_policy`,
  `release_canary` and `panel_scene --live`. It takes an OS file lock shared
  by all processes (`panel_mutation.lock`), waits until 0.4 s after the
  previous write ended (`panel_mutation.last`), and before unlocking appends
  one line to `panel_events.jsonl` (`t src op app bytes status ms wait_ms err
  gate`; rotates at 1 MB).
- **It never costs a sender its write.** A lock not free within 25 s (renewed
  while the queue moves, 60 s cap) or an unusable lock sends the write
  ungated, journaled as `timeout` or `unavailable` when the directory is
  writable; if the directory itself is unusable, the write still goes out and
  nothing is journaled. Nested calls journal `nested`.
- **Turning it off.** Creating `panel_gate.off` in the gate directory turns
  off the locking and the gap in every gated sender at its next write, with
  no restart, and keeps journaling (gate `off`). `AWTRIX_PANEL_GATE=0` turns
  it off with no journal, but it is a per-process environment variable, so it
  only reaches a sender started with it. `AWTRIX_PANEL_GATE_DIR` and
  `AWTRIX_PANEL_GATE_GAP` (≤ 2 s) move it and change the gap.
- **Adding a panel write:** keep the plain `requests.put(...)` /
  `session.delete(...)` call inside `with panel_gate.mutation(...) as gate:`
  and call `gate.observe(response)`. `weather/test_aqi_brief_push.py` counts
  `.delete(` calls in the source, so a method-as-string helper would hide one.
- **Ungated and unjournaled:** `nightkeeper_recover.ps1` (the panic button)
  and Miku-JARVIS (separate project, scheduled task disabled, PUTs a
  waveform up to ~6×/s). While Miku runs, a heap drop next to a journaled
  write is not evidence against that sender.
- **Live since the 2026-10-07 logon.** In its first 26 hours it journaled
  1,026 writes (~39/h): PC stats 475, Hub 403, weather 104, AQI 44.
- **Tests** never touch `%LOCALAPPDATA%\AWTRIX`: the root `conftest.py`
  points every test at a session temp directory with gap 0 (`panel_gate_dir`
  gives a test its own), and a test file run without conftest gets the gate
  switched off.

## Panel logs (under `%LOCALAPPDATA%\AWTRIX`)

- **`panel_health.jsonl`**: one line per ~30 s Hub probe (`t uptime reason
  free min_free block fps app power rssi wconn watt wstate`, plus `werr`
  only when set; `app` is `currentApp`, `power` is `matrixPower`, `rssi` is
  `wifiRssi` in dBm (0 is logged as null: "no AP info"), `wconn`/`watt` the
  panel's Wi-Fi connect/attempt counters — `wconn` rising within one boot
  means it reconnected without rebooting). The SSID and endpoint are never
  logged. Failed probes leave `reachable: false` and an exception class.
  ~200 B a line, so 1 MB holds ~41 h of continuous probing (longer in
  practice: the PC sleeps); one rotated file is kept.
  After a crash, compare successful samples and find where `uptime` goes
  backwards: the lines before are the run-up; `reason: panic` is a crash.
- **`panel_events.jsonl`**: the gate's journal. **`maintenance.json`**: each
  nightly run, with block before/after and per-script boot heap.
- **`panel_heap_report.py [--data-dir DIR] [--since-hours H] [--json]`**
  (Anaconda python) joins them read-only: boots (block at
  +0/+10m/+30m/+1h/+6h/+24h), biggest drops with the mutations in each
  interval, mutation attribution (correlation, not proof), FPS by app and
  power, mutation rate, gaps, and Wi-Fi: per-boot RSSI, signal bands, and
  for each outage the probe before and after it with a verdict (Wi-Fi drop:
  `connects +N` in the same boot; no reconnect; rebooted; still down).
- **Panel-down popup** (`panel_down_alert.py` + `panel_down_popup.py`,
  2026-10-09, `panel_down_alert` config section, on by default): after 10
  minutes of failed Hub probes the Hub starts a small always-on-top window
  (its own `pythonw` process) saying how long the panel has been silent and
  to power-cycle it. Once per outage, at most one per 30 min; held while Game
  Mode sees a game (and hidden if one starts while it is up); not on waking
  from PC sleep; closes itself on the first good probe, after 12 h, or when
  the Hub has been silent 10 min. Windows toasts are off on this PC
  (`ToastEnabled = 0`), which is why it is a window.

## Berry dialect notes (as used by this firmware)

Drawing: `clear()`, `pixel(x, y, color)`, `line(x0,y0,x1,y1,color)`,
`text(x, y, str, color) -> advance`, `text_ink_width(str)`, `rgb(r,g,b)`.
Colors are also plain ints (`0xEEE3CE`).

Runtime: `now_ms()`, `epoch_ms()`, `year()`, `hour()`, `minute()`, `log(str)`,
`settings.get(k)` / `settings.set(k, v)`, `rotation.next()`,
`shared.get/set/age(key)` (cross-script key-value store with millisecond age),
`http.get(url, /b,st -> cb)` and `http.patch(url, body, cb, {"headers":{...}})`
(async, callback-based), `module("name")` for shared libraries, plus
`import json`, `import string`, `import math`. Firmware 1.1.2 adds `timer.*`,
`display.power()` / `display.is_on()`, `on_button_event()` and `should_show()`.

App header comments are metadata: `# @name`, `# @desc`, `# @author`,
`# @version`, `# @headless true` (runs without a rotation slot), `# @module <n>`
for a library. A script file ends by returning an instance: `return Moon()`.
Instances implement `init()`, `loop()` (slow tick), and `draw()` (per frame).

There is **no `wx` module** — scripts from before 2026-08 that open with
`import wx` will not load. `math`, `rgb()`, `month()` and `day()` all do exist;
verified 2026-09-01 with a throwaway `# @headless true` probe that logs each in
its own `try`. That probe is the cheap way to settle any "does this firmware
have X" question — **the panel reports only the first error**, so one missing
import hides everything behind it and invites wrong inferences about the rest.

**Berry floats on this ESP32 are single precision.** Resolution is ~0.25 at a
magnitude of 2.4 million, so subtracting two Julian Day numbers as reals loses
a quarter of a day. Keep large subtractions in integers and call `real()` only
on the small remainder:

```berry
# wrong: float32 eats the difference
var raw = (real(jdn(year(), month(), day())) - 2451550.1) / 29.530588853
# right: subtract as ints, convert the remainder
var raw = (real(jdn(year(), month(), day()) - 2451550) - 0.1) / 29.530588853
```

## Layout of this folder

```
utility_hub.py / panel_client.py           Hub process + shared, rate-limited panel client
panel_gate.py / panel_heap_report.py       cross-process write gate + journal; read-only log report
panel_maintenance.py / hub_status.py       nightly reboot; 30 s health probe -> panel_health.jsonl
nightkeeper.ax / nightkeeper_logic.py      bedtime fade, blackout, morning fade (v3.0: display.power())
now_playing.ax / now_playing_push.py       Windows GSMTC media -> panel (record + rail)
now_playing_bridge.py                      device/script install + state-bridge helpers
ocean.ax, weather/moon-panel.ax            installed scenes (ocean as-is, moon minified)
panel_snapshot.py / panel_soak.py          read-only rollback capture + day/night gate
release_canary.py                          guarded Network Sentinel + Next Up live canaries
awtrix-git.ps1                             Git wrapper for separate local metadata
pc_stats_push.py                           PC telemetry sender
weather/weather_brief_push.py, aqi_brief_push.py   live weather: pushed weatherbrief + aqibrief
meridian_preview.py / meridian_push.py     day ribbon: real solar arc (out of the loop on request)
preview_*.py                               offline renderers for design review
weather/wx.ax, wxfeed.ax, {wxnow,wxrange,wxsky,wxsun,aqi,soon}.ax   script weather stack, not installed
weather/sunset.ax                          animated sunset descent + quality (off since 2026-10-04)
moon.ax, weather/moon.ax                   older moon programs, not installed
backups/  weather/backups/                 dated panel-state + exact .ax.b64 baselines
design-qa.md                               visual QA record for the progress rail
PANEL_HISTORY.md                           dated measurements and superseded claims
```

In the script weather stack, `wxfeed` (headless Open-Meteo fetcher) writes
one atomic JSON snapshot into `shared` (`wxfeed.data`) and each page reads
it via `wx.snapshot()` in `loop()`. None of it is on the panel; reinstalling
it needs something removed first.

## Working conventions

- **Test command** (2,349 tests, ~30 s as of 2026-10-09), run from the
  awtrix root. It collects `weather/` too:
  `C:/Users/devyn/anaconda3/Scripts/pytest.exe -q -p no:cacheprovider`
  (`-p no:cacheprovider` avoids the `.pytest_cache` write that Controlled
  Folder Access blocks).
- Python is Anaconda by full path (`C:/Users/devyn/anaconda3/python.exe`); see
  the global `~/.claude/CLAUDE.md`.
- On-device logic is mirrored in a `*_logic.py` module and unit-tested there.
  Keep the Berry and the Python in sync when changing thresholds or fades.
- Before touching an installed app, snapshot the panel into a dated folder under
  `backups/` (device/settings/display/apps/apploop JSON plus an exact `.ax.b64`
  of every script), the way `backups/2026-08-10-nightkeeper-baseline/` does.
- `nightkeeper_recover.ps1` is the panic button: restores power on, brightness 45,
  autoBrightness off, and can remove `nightkeeper`.
- Design changes get a normalized 32x8 before/after comparison; `design-qa.md`
  is the template for that record.

## Windows Controlled Folder Access blocks writes here

`EnableControlledFolderAccess` is **on**, and `Documents` is protected. Writes
into this folder from a freshly spawned `git.exe`, `python.exe` or `bash.exe`
fail with a misleading `No such file or directory` / `Bad file descriptor`;
reads work fine. The existing scheduled-task senders are allow-listed, so their
logs still write.

Practical effects: **`git commit` must be run from an already-allowed shell**,
`pytest` cannot write `.pytest_cache` (harmless warning), and any script that
writes output here needs an `--out` pointing elsewhere. Allow `git.exe` and
Python via Windows Security → Ransomware protection → Allow an app.

## Panel hygiene

- **Reboot before any install or backup session.** After ~9 days of uptime
  the heap fragments until the API refuses large bodies in *both* directions,
  and `GET /apps/script/{name}` returns 0 bytes, so backups silently fail.
  ~10 KB readbacks have worked only with the block at ~30 KB or more, which
  with the current four scripts lasts about 10 minutes after a boot, so do
  large readbacks and installs in the first minutes after the reboot.
  `POST /api/v1/device/reboot` takes about 9 seconds.
- Scripts that read `hour()`/`minute()`/`year()` must not use a sentinel value
  the clock can actually return: `hour()` returns **-1** before NTP sync. See
  `panel-log-2026-08-20.md` for the `moon` crash this caused.

## Current release state

- **Firmware is 1.1.2** (upgraded 2026-10-03 by manual `.bin` upload). It
  adds `timer.*`, `display.power()`, `on_button_event()`, `should_show()` and
  multi-icon pushes, and drops the `scriptLimit`/`scriptMaxBytes` caps (the
  tests keep their own: `moon-panel.ax` and `now_playing.ax` ≤ 16,384 B raw,
  `weather/*.ax` ≤ 12,288 B minified). The 1.1.0 → 1.1.2 comparison is in
  `ROADMAP.md`.
- **Nightly maintenance is on** (`panel_maintenance.py`, `maintenance`
  section): 21:35–23:00 because the PC sleeps overnight, only with uptime
  ≥ 20 h and the matrix dark. First real run 2026-10-06 21:37: `ok`, block
  8,692 → 32,756, all four scripts back, no errors.
- **Low-memory restarts** (same controller, 2026-10-09; live since the
  09:49 Hub restart, first real one 12:58 `ok`): read once a minute at any hour; restart, lit or not, when
  `minFreeHeapBytes` (since boot) is under 8,192 — never in the first 2 h
  after a boot, never while the matrix is off Nightkeeper's schedule (a
  person is holding it) or within 2 min of 21:30/07:00, at most 3 per 24 h.
  The largest-block signal exists but defaults off (`low_memory_block_bytes:
  0`). Replayed over 2026-10-04..08 the policy restarts twice, the second 46
  min before the 2026-10-07 outage. Memory drains with PC-awake sender time,
  not uptime: an 18 h PC sleep left the block unchanged.
- **Moon v2.1 is installed** (2026-10-10, on the replacement board):
  `weather/moon-panel.ax`, minified sha256 `90cd90e0…`. First hardware run:
  readback match, screen match against the emulator, 40–42 FPS (v2.0: ~25).
  Its boot VM heap is 21,907 B — 745 B *more* than v2.0, not the 1.0–1.5 KB
  saving the emulation estimated. Health check (read-only):
  `python script_swap.py moon weather/moon-panel.ax --expect-version 2.1
  --rollback weather/moon-panel-v2.0.ax --rollback-version 2.0 --expect-sha256
  90cd90e057a969d3 --rollback-sha256 f6b17ba2500719b0 --check-only`; drop
  `--check-only` and add `--rollback-now` to go back to v2.0.
- **PC stats writes on change** (2026-10-09): refresh on a 3-point move or
  level change, 25 s keep-alive, 60 s page lifetime, 30 s minimum display
  showing the spike value, 5→60 s backoff for lost and refused writes.
- The companion re-pushes only on change plus a 240 s keep-alive. ISS Watch
  (`iss_watch.py`, `iss` section, alert-only) is off in the live config.
- **Recovery checks hardened 2026-10-04.** The companion supplies static
  fallback and restores on the next tick after an observed reboot.
  Maintenance verifies the reset reason, complete readbacks and the return of
  pre-reboot scripts. See `STABILITY_STATUS.md` for evidence and limits.
- **All future plans and release gates were scrapped on 2026-09-26**: no
  pending soak, calendar source, gamma work or redesign. Don't resume any of
  it unless asked.
- Git metadata lives under `%LOCALAPPDATA%\AWTRIX\git\awtrix.git`; use
  `awtrix-git.ps1`. `git commit` through it works from an ordinary shell:
  only the git directory under `%LOCALAPPDATA%` is written.
