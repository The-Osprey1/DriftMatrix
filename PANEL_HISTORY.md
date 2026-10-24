# Panel history

The dated measurements, incidents and superseded claims behind the rules in
`CLAUDE.md`. CLAUDE.md states each current rule once; this file keeps the
evidence, grouped by topic, oldest first within a topic. Numbers are copied
verbatim from the record they came from. **Superseded** marks a claim that is
no longer true and names what replaced it; **corrected** marks one that was
never right.

Sources for the 2026-10-06 entries, all read-only: `GET /api/v1/device` and
`GET /api/v1/apps` at 23:29 (uptime 6,702 s), `%LOCALAPPDATA%\AWTRIX\maintenance.json`,
the boot log saved in `%LOCALAPPDATA%\AWTRIX\backups\panel-snapshot-20261006-230236\logs.json`,
and `panel_health.jsonl` read through `panel_heap_report.py`. The 2026-10-07
corrections also use the `manifest.json` and `device.json` of every snapshot
under `%LOCALAPPDATA%\AWTRIX\backups\`, and `utility_hub.log`.

The consolidated CLAUDE.md, which keeps only the current rules and points
here, was drafted on 2026-10-06 and applied on 2026-10-07 after an
independent check that no fact was lost.

## 1. The script heap (98,304-byte budget)

- **Firmware caps (1.1.0). Superseded 2026-10-03.** `scriptLimit: 16` was
  never the real limit; the 98304-byte script heap was. Five non-trivial
  scripts reached 93% and loaded only intermittently (date not recorded).
  `scriptMaxBytes: 16384` per script, with `now_playing.ax` at 10.4 KB the
  largest. Firmware 1.1.2 dropped both caps and ignores writes to them, so
  memory is the only gate. The repo's own tests still cap sizes:
  `weather/moon-panel.ax` ≤ 16,384 B raw (`weather/test_moon_panel_logic.py`;
  the file is 16,364), `now_playing.ax` ≤ 16 × 1024 raw
  (`test_now_playing_berry_preview.py`; the file is 11,874), and every
  `weather/*.ax` ≤ 12,288 B minified (`weather/test_weather_logic.py`).
- **Weather size guard. Corrected 2026-10-06.** CLAUDE.md said `weather/`
  guards itself at 10240 bytes per `.ax` in `test_weather_logic.py`, raised
  from 8192 when `sunset.ax` landed. The test has said 12,288 (minified) since
  the 2026-08-23 baseline commit. No tracked version says 10240.
- `scriptHeapBudgetBytes: 98304`, internal pool. `weather/moon.ax` was
  minified specifically to fit.
- **"Heap, not CPU, is the wall." Superseded in part 2026-10-03.** Heap, and
  above all the contiguous block, still decides installs, pushes and TLS.
  Frame rate is CPU-bound: `ocean` v2 is draw-bound at 33–35 FPS, because
  each native `pixel()` call costs ~50–60 µs (section 7).
- **Cost is not proportional to source (2026-09-01, firmware 1.1.0, boot
  log).** `nightkeeper` 8.5KB source → 7,104 bytes VM heap (0.83×),
  `nowplaying` 10.4KB → 14,502 (1.39×), `ocean` 9.6KB → 20,299 (2.11×),
  `moon` 3.7KB → 1,509 (0.41×). `moon` reported +3,932 installing and +1,509
  restoring, which is why only a post-reboot boot log gives an honest figure.
  Later figures for the same scripts: nightkeeper v3.0 on 1.1.2, 4,081; ocean
  v2 on 1.1.2, ~26,000.
- **2026-10-03 14:00, 1.1.0 boot log** (`backups\panel-snapshot-20261003-140141`):
  moon +1,693 (shared 15,705), nightkeeper +7,104 (shared 22,809), nowplaying
  +14,502 (shared 37,311), ocean +20,299 (shared 57,610), max block 49,140
  after ocean. The VM held 14,012 bytes before the first script. The 1.1.0 →
  1.1.2 comparison table is in `ROADMAP.md`, Phase 1.
- **2026-10-03, five scripts on 1.1.2** (comment in `rotation_policy.py`):
  82,496 of 98,304 bytes (84%), block 36,852 after boot. `ROADMAP.md` calls it
  85%. When `sunset` (loaded last) was reached, 47,092 bytes of block remained.
  On 1.1.0 with Nightkeeper 2.x only 26,612 had remained, against the ~35,000
  sunset needs.
- **2026-10-04, five scripts, boot log in the snapshot taken after the panic**
  (`backups\panel-snapshot-20261004-152902`, reset reason `panic`): moon (root
  port) +2,893 (shared 17,709), nightkeeper +4,081, nowplaying +14,665, ocean
  +26,158, sunset +20,468 (shared 83,081, i.e. 85%), max block 24,564 after
  sunset. The panel panicked on 2026-10-04 at 14:51, during a companion push,
  with five scripts at 85%. The timing does not prove the push caused it.
- **2026-10-04, four scripts** (old moon tracker restored, `sunset` off):
  recorded as moon 21,162, nightkeeper 4,081, nowplaying 14,368, ocean 26,361,
  "80,788 of 98,304 B (82%), block 36,852 after boot".
  **Corrected 2026-10-06.** The four figures add up to **65,972**, not 80,788
  (and not 85,972). 80,788 is the boot log's running `shared` total, which
  also counts the 14,816 bytes the VM holds before the first script loads
  (17,709 − 2,893 on 10-04; 35,978 − 21,162 on 10-06). Both numbers are right,
  and they come from boots with identical per-script figures. 65,972 is what
  the four scripts cost. 80,788 (82%) is how full the budget is, and it is the
  figure to compare with 98,304 and with the ~75% (73,728 B) comfort line.
  "Block 36,852 after boot" is the 2026-10-03 five-script figure, not a
  four-script one. The four-script set measured 32,756 on 2026-10-06.
- **2026-10-06 21:37:32, maintenance reboot boot log:** moon +21,162 (shared
  35,978, max block 65,524), nightkeeper +4,081 (shared 40,059, 77,812),
  nowplaying +14,368 (shared 54,427, 55,284), ocean +26,361 (shared 80,788,
  32,756). Install low-water: 77,576 / 85,424 / 65,968 / 42,724. VM heap per
  installed byte: moon 2.0×, nightkeeper 1.5×, nowplaying 1.95×, ocean 2.5×.
- **Source-size framing. Superseded.** "The four current scripts total 31 KB of
  source; adding the 25 KB weather stack (`wx` + `wxfeed` + `sunset`) exceeds
  the budget and apps start reporting `out of memory`. Something must come off
  to add that stack." That was written about an older set. The installed four
  happen to total 31,065 B (10,574 + 2,685 + 7,372 + 10,434), but source size
  does not predict cost. The rule now uses the boot log's `shared` total:
  80,788 of 98,304, with ~17.5 KB left under the budget and the set already
  ~7 KB over the comfort line. The weather stack still needs something removed
  first.

## 2. The largest free block: boot, decay, reboot

- **Scripts load in sequence at boot and each shrinks the block.** From one
  boot (date and firmware not recorded): moon leaves 59,380, nightkeeper
  63,476, nowplaying 40,948, sunset 27,636. On 2026-10-06: moon 65,524,
  nightkeeper 77,812, nowplaying 55,284, ocean 32,756. `rotation_policy.py`
  notes that scripts compile in alphabetical order. **Corrected 2026-10-07:**
  both boots contradict "each shrinks": the block rose after nightkeeper
  loaded (+4,096 and +12,288). The rule that holds is that the block left
  after the last script loads is what counts, which is why an install that
  fits a bare panel can fail on a populated one.
- **Deleting a script does not give the block back.** Removing `timepiece`
  (2.4 KB) left `largestFreeBlockBytes` unchanged at 11,252. Only a reboot
  reclaimed it, taking the block to 27,636.
- **2026-10-03 14:00:** 7.0 days of uptime, block 12,788 B, min free heap
  since boot 9,516 B. A manual reboot took the block to 45,044 B.
- **After the 1.1.2 upgrade (2026-10-03):** four scripts (small moon, ocean
  v1) left free heap 64,116 and block 40,948. Nightkeeper v3.0 then gave free
  62,972 and block 59,380. ocean v2 then gave free 58,424 (was 64,116) and
  block 47,092 (was 40,948).
- **2026-10-04 boot (15:30, software):** 10,228 at +30 min and +1 h. That is
  the "largest block remains 10,228 B" in `STABILITY_STATUS.md`. The block was
  8,692 at 7h28m, reached a low of 4,596 at 2d04h, and on 2026-10-06 read 6,644 from 21:22
  and 8,692 again at 21:36. Maintenance's own pre-reboot read was
  8,692 at uptime 194,821 s (54.1 h), with min free 7,344.
- **2026-10-06 boot (21:37, maintenance):** 32,756 at +0 (and at uptime 104 s
  in `maintenance.json`), 31,732 at 8 min, 28,660 at 10 min, 30,708 at 17 min,
  26,612 at 20 min, 25,588 at 21 min, then 18,420 at 25.7 min (uptime
  1,544 s), 16,372 at 52 min. Read 18,420 again at 85 min (snapshot) and at
  1h52m (live GET, uptime 6,702 s). The biggest single drops cluster 20–25
  minutes after boot: −7,168, −4,096, and −2,048 several times.
- `UTILITY_HUB.md`'s "the largest free block falls from ~30 KB after a boot
  to ~12 KB after a week" (2026-10-03, from the 1.1.0 readback of 12,788 at
  7.0 days). **Superseded 2026-10-07:** on 1.1.2 with the current four
  scripts the block passes 12 KB within hours, not a week. The 2026-10-06
  boot went 32,756 → 18,420 in 26 minutes and reached 12,276 at 10h02m
  (2026-10-07 07:40). The 2026-10-04 boot read 10,228 at 20 minutes and
  4,596 at 2d04h. `UTILITY_HUB.md` now gives ~32 KB after boot, 10–18 KB
  within half an hour and ~5–12 KB within a day or two.
- **"With the full script set resident the best block observed is 29,684"**
  (2026-08-23). **Superseded:** the best observed with the current four is
  32,756, at boot on 2026-10-06.

## 3. Installing a script

- **Undated (~2026-08-22), 6,980-byte minified `now_playing`:**

      free heap 53,284 -> 45,792, install low-water 38,424, max block 20,468

  The transient dip was 14,860 bytes (2.13 × source), of which 7,492 stayed
  resident. Outcomes for that same source:

  | largest free block | result |
  | --- | --- |
  | 11,252 | **507** |
  | 16,372 | **507** — only ~1.5 KB over the 14,860 needed |
  | 27,636 | ok |

  The conclusion then was "budget **2 × source contiguous plus real
  headroom**", with a blanket "4×" rule of thumb described as working but
  "a symptom, not the mechanism". The size rule was never the confusing part;
  the contiguity was.
- **2026-08-23, the same 7,372-byte script:** block 27,636 installed fine
  (3.75×), block 24,564 returned `Device rejected the Berry script: out of
  memory` (3.33×). The `install low-water` figure describes the *heap* dip,
  not the *contiguous* requirement.
- **Reconciled 2026-10-06.** The two rules did not conflict. 2× is the
  transient heap dip, which `install low-water` reports. The contiguous block
  an install needs is larger, and both data sets fit "≥ 3.5 × minified
  source": 6,980 B failed at 2.35× and worked at 3.96×; 7,372 B failed at
  3.33× and worked at 3.75×. The current rule budgets against 3.5×, the stricter
  figure. The old line "a Berry script install needs (~2× source)" in the
  pushed-apps section is superseded by the same rule.
- **2026-08-23, a failed install still writes the source.** After the OOM
  above, `GET /apps` reported `nowplaying` with `error: {'message': 'out of
  memory'}` while it was still present and in the loop. A reboot compiled the
  already-stored source cleanly and the error cleared. `ensure_script_installed`
  then found the panel byte-identical to the repo and correctly wrote nothing.
- **Undated:** with five scripts loaded, a PUT of a 10 KB source was refused
  (`insufficientStorage`, nothing stored).
- **2026-08-22, the media bridge lost 1h51m of startup.** `now_playing.ax` was
  ~10.7 KB, so a raw install wanted ~21 KB contiguous, more than the panel
  offered even shortly after a reboot. `now_playing_push.py` retried every
  32 s from 11:35 until 13:26 and published no media state until the install
  landed. `now_playing_bridge.install_source()` has minified since then
  (~7.0 KB, "~14 KB needed" under the 2× rule; ~25 KB under 3.5×). On
  2026-10-06 the source is 11,874 B raw and 7,349 B minified, and 7,372 B is
  installed (the PC's LAN address is substituted at install time).
- **2026-10-06:** at 3.5×, the installed 10,574-byte `moon` and 10,434-byte
  `ocean` each need ~37 KB, more than the 32,756 post-boot block. This is why
  replacing either means delete, reboot, PUT, reboot.

## 4. Reading scripts back

- **Undated (1.1.0): "`GET /apps/script/{name}` returns an empty body for
  older scripts."** It returned full source for `timepiece` (installed
  minutes earlier) but empty for `nowplaying`, `moon` and `sunset`, so
  anything comparing installed source against desired source always saw a
  mismatch and reinstalled. That is why the identity check uses
  `meta.version` from `GET /apps`.
- **2026-08-23, with 22,516 bytes free:** `nightkeeper` (4,185 B) and
  `nowplaying` (7,348 B) returned fine. `sunset` (9,967 B) and `moon`
  (10,574 B) died with `ChunkedEncodingError`. This was read as a
  response-size ceiling of roughly 8 KB, "not heap pressure", that "no
  maintenance window lifts". **Superseded** by the next entry.
- **2026-10-03:** `ocean` (9,605 B, SHA-256 `938064bc…`) read back byte-exact
  90 s after a reboot, so the ~8 KB ceiling is fragmentation, not a hard
  limit. The 1.1.1/1.1.2 release notes had also said log/scan responses use
  less temporary memory.
- **2026-10-04 15:28–15:29** (uptime 2,234–2,281 s, block 19,444 then
  18,420): `ocean` (10,434) and `sunset` (10,142) failed, first with
  `ConnectionError` and then with zero bytes. `moon` (4,171), `nowplaying`
  (7,372) and `nightkeeper` (2,685) read back. The two failed sizes are the
  repo's, not the panel's.
- **2026-10-06 23:02** (uptime 5,105 s, block 18,420): `ocean` (10,434) and
  `moon` (10,574) returned zero bytes. `nowplaying` (7,372) and `nightkeeper`
  (2,685) read back. Again the two failed sizes are the repo's.
- **Earlier ~10 KB readbacks, from the snapshot manifests.** `ocean` v1
  (9,605 B) read back on 2026-09-19 (uptime 145,430 s, about 40 h; block
  29,684), 2026-09-26 (uptime 6,377 s; block 31,732) and 2026-10-03 (uptime
  90 s; block 45,044). On 2026-08-23 a snapshot at uptime 899 s, with the
  block at 22,516, stopped after `nowplaying` (the `.partial` folder; this is
  the "22,516 bytes free" above), and at uptime 1,058 s, with the block at
  21,492, `sunset` and `moon` failed with `ConnectionError`. All five of
  those were on 1.1.0.
- **Reconciled 2026-10-06, re-keyed 2026-10-07.** It was first written as
  "readbacks above ~8 KB fail once the block has decayed, which takes under
  40 minutes after a boot". Uptime is the wrong key: a 9.6 KB readback worked
  at 40 h, and one failed at 15 minutes. The largest free block separates
  every case on record. ~10 KB readbacks worked with a block of 29,684 or
  more and failed with 22,516 or less. With the current four scripts the
  block first fell below 29,684 about 10 minutes after the 2026-10-06
  boot (28,660 at uptime 602 s) and reached 18,420 at ~26 minutes. The "older
  scripts" observation fits the same pattern: `nowplaying` failed then and
  reads back now. `panel_snapshot.py` rebuilds the larger sources from the
  repository; `minify()` is deterministic and reproduced the installed bytes
  with an exact SHA-256 match for every script small enough to check.
- **`moon` (`weather/moon-panel.ax`) and `ocean` v2 have never been read
  back.** Every snapshot that included them marks them repository-derived:
  moon 10,574 B (`f6b17ba2`) with `ConnectionError` on 2026-08-23 and zero
  bytes on 2026-10-06; ocean v2 with `ConnectionError` and then zero bytes on
  2026-10-04, and zero bytes on 2026-10-06. The only panel-provenance `ocean`
  reads are v1. A readback in the first few minutes after a maintenance
  reboot, while the block is still above ~30 KB, would settle both.
- **After ~9 days of uptime** the API refuses large bodies in both
  directions: `GET /apps/script/{name}` returns 0 bytes, so backups silently
  fail. `POST /api/v1/device/reboot` takes about 9 seconds and restores it.

## 5. Script identities

- **`moon`, three programs.** `weather/moon-panel.ax`, minified (10,574 B),
  has been installed since 2026-10-04 (meta version 2.0): the old tracker
  with the nebula, eclipses and the glide to an info screen, restored at the
  user's request. Root `moon.ax`, the
  small port, was installed from 2026-09-01 to 2026-10-04 to save heap.
  CLAUDE.md called it "the small 2.9 KB port"; 2,893 is its VM heap on 1.1.2,
  and its installed source was 4,171 B in the 2026-10-04 snapshot (version
  2.3). `weather/moon.ax` is not installed.
- **`ocean` v1:** recovered byte-exact (minified, 9,605 B, SHA-256
  `938064bcff39fe95…`) from a fresh-boot readback on 2026-10-03, and kept in
  `backups/2026-10-03-pre-firmware-1.1.2/`. CLAUDE.md called it 9.6 KB.
- **`ocean` v2 (2026-10-03, the faster one):** root `ocean.ax`, 10,434 B,
  SHA-256 `5000462ec6ea5f02…`, assumed to be installed exactly as it sits in
  the repo (`panel_snapshot.INSTALLED_AS_IS`). That identity is not verified:
  v2 has never been read back (section 4), and its empty meta leaves no
  version to compare. VM heap 26,193 (v1 on 1.1.2: 18,939; v1 on 1.1.0:
  20,299), then 26,158 in the 2026-10-04 boot log and 26,361 on 2026-10-06.
- **`moon`'s 10,574 B is also repo-derived.** `weather/moon-panel.ax` has
  never been read back either (section 4); its `meta.version` 2.0 is the
  only panel-side identity check.
- **`ocean` has no `# @name`/`# @version` header**, so `GET /apps` shows empty
  meta for it (`name`, `desc`, `author` and `version` all `""`, verified
  2026-10-06). The `meta.version` identity check cannot work for ocean. This
  is a known gap: add the header when ocean is next reinstalled. Adding it now
  would make the repo differ from the installed bytes.
- **`GET /apps` on 2026-10-06:** moon 2.0, nightkeeper 3.0, nowplaying 2.1,
  ocean empty. All present with `error: null`. `sunset`, `timepiece`,
  `meridian` and `pcstats1` are disabled and absent.
- **`nightkeeper`:** 8.5 KB source and 7,104 B VM heap (2026-09-01). v3.0
  (2026-10-03) took minified source from 4,185 to 2,685 B and VM heap from
  7,104 to 4,081. The raw file is now 6,502 B.

## 6. Pushed apps and animation

- **Full-width GIF cost**, measured on this panel. One resident full-width
  animation costs contiguous block roughly in proportion to its decoded size:

  | GIF | frames | largest-free-block delta |
  | --- | --- | --- |
  | 373 B | 3 | −3,072 |
  | 771 B | 8 | −4,096 |
  | 1,246 B | 8 | **−10,240** |
  | 489 B (8×8 icon + static lane) | 6 | ~0 |

- **2026-08-22: a static full-bleed 32×8 `draw` bitmap costs 0** of the
  largest free block, both on push and while it is the current app. No decode
  buffer is ever allocated. `meridian_push.py` was built on this and holds the
  whole panel for 1,149 bytes at zero block cost.
- **2026-08-23: a full-width animation panicked the panel.** A 7,010-byte
  full-width animated Next Up payload was pushed with 22,516 bytes free in the
  largest block. The panel came back with `resetReason: panic`, taking the
  rotation and every pushed app with it. There was no HTTP error to catch. The
  worst measured block-to-payload ratio is 8.2, so
  `release_canary.ANIMATION_BLOCK_RATIO` is 10. The assumptions this
  corrected had been baked into `weather_brief_push.py`.
- **Until 2026-10-04 the companion re-pushed every 15 s.** Since then it
  re-pushes on change plus a 240 s keep-alive. The 2026-10-04 14:51 panic
  happened during a companion push.

## 7. Frame rate

- **2026-10-03:** the readback showed FPS 32 with `ocean` current. Sampled per
  app after the upgrade, `ocean` ran at 28–29 FPS and every other page at 42,
  so low FPS is `ocean`'s `draw()`, not fragmentation. ocean v2 went from
  27–29 to 33–35 FPS (see ROADMAP Phase 2 for the changes).
- **Profiled on the device (2026-10-03):** each native `pixel()` call costs
  ~50–60 µs, Berry ops are slow, and the firmware's own per-frame work is
  ~14 ms, so 42 FPS needs `draw()` ≲10 ms.
- **2026-10-04 15:29:** FPS 30 in the post-panic snapshot.
- **Health log, 2026-10-04 15:50 → 2026-10-06 23:29:** 749 successful probes,
  mean 38.5 FPS, minimum 22. When first counted on the evening of 2026-10-06,
  181 of 589 probes (~31%) were below 40. All 181 are from the 2026-10-04
  boot: 181 of its 542 probes, 33%. None of the 207 probes after the 21:37
  reboot were below 40 (mean 41.7). Those were all taken during Nightkeeper's
  blackout, though, so they do not show a fix. Counts split the boots where
  `uptime` rolls back (last old-boot probe 21:37:22, first new one 21:37:54
  at uptime 23 s), as `panel_heap_report.py` does. Pinning slow frames on a
  page needs the `app` field (section 10).
- **2026-10-07 morning, same boot:** after the blackout (it ends at 07:00;
  probes resumed at 07:22, when the PC woke) slow frames came back: from
  07:22 to 07:54, 33 of 60 probes were below 40 (minimum 23).
- **First FPS-by-app data, 2026-10-07 19:40 → 2026-10-08 22:03**
  (`panel_heap_report.py`, 797 probes with `app`): with the matrix on,
  `moon` averaged 25.1 FPS (minimum 22) and was below 40 in all 120 of its
  probes; `nowplaying` 35.8 (6 probes); `aqibrief` 37.8 with 25% below 40;
  `Time`, `companion0`, `weatherbrief` and `pcstats0` held 41–42. With the
  matrix off every page reads ~41.7. So the slow frames on 2026-10-04/07
  were most likely `moon`, not `ocean` (which was not on screen in this
  window, as the PC slept through its 07–17 slot).

## 8. Nightly maintenance

- **Shipped 2026-10-03, disabled,** with a 03:30–05:00 window.
- **Enabled since 2026-10-04 with a 21:35–23:00 window,** because the PC
  sleeps overnight and 03:30 never ran. The window requires uptime ≥ 20 h and
  a dark matrix.
- **2026-10-04 22:57:05: skipped,** "uptime 7.4 h is under 20 h" (the panel
  had been restarted that afternoon).
- **2026-10-05: no attempt.** The PC was asleep: there are no health probes
  from 2026-10-04 23:43 to 2026-10-06 18:36.
- **2026-10-06 21:37:30: the first real run, outcome `ok`.** Reason: "uptime
  54.1 h, largest block 8692". Before the reboot: free 25,404, min free 7,344,
  block 8,692, fps 41. Boot took 12 s, reset reason `software`, dark after
  boot 0 s. After (uptime 104 s): free 52,728, block 32,756, fps 42, matrix
  off. All four expected scripts came back, `script_errors` `{}`,
  `missing_scripts` `[]`. Hub log at 21:39:15: "Nightly maintenance ok:
  largest block 8692 -> 32756, script errors none".

- **Low-memory restarts, 2026-10-09.** Replay of every successful health
  sample 2026-10-04 15:50 → 2026-10-08 22:27 (60 s cadence, 30 min minimum
  uptime, 3/day cap; reviewer's scratch `replay.py`) through
  `low_memory_trigger`:

  | Policy | Restarts | Before the 10-07 outage? |
  | --- | --- | --- |
  | block < 8 KB × 3 readings only (the original request) | 2 (10-06 19:07, 10-08 18:27) | no |
  | min free < 8 KB only | 2 (10-06 19:38, 10-07 19:43) | yes, 46 min |
  | both at 8 KB | 3 (all evenings 18:00–20:00, matrix lit) | yes |
  | both at 6 KB | 2 | no |
  | block or min free at 10 KB | 3, one at 0.5 h or 1.4 h uptime (loop-prone) | — |

  Chosen by the user: min free < 8 KB only, 2 h minimum uptime for that
  signal, hold while the matrix is off Nightkeeper's schedule. The final
  policy replays as 2 restarts (10-06 19:38 at 52.1 h, 10-07 19:43 at 22.1 h).
  Lowest min free seen 30–60 min after a boot: 14,700 B; largest single drop
  after 30 min: 7,436 B (10-06 23:02, at 1.42 h). On the 10-07 boot the
  block held at 21,492 B through an 18 h PC sleep and reached 7,668 B after
  1.2 h of PC-awake time: the drain follows the senders, not uptime.

- **First real low-memory restart, 2026-10-09 12:58:38:** minimum free heap
  7,392 < 8,192 at 15.4 h uptime (matrix lit, on schedule); outcome `ok`,
  block after 36,852, minimum free after 37,016.
- **Outage, 2026-10-09 from 13:46 (still down at 17:02):** not a memory
  event. The last good probe (13:45:21, uptime 47 min after that restart)
  read minimum free 12,520 and block 23,540; the last writes (13:43 companion,
  13:44 weather) succeeded normally. During the outage ping lost 70% (some
  replies "destination host unreachable" from this PC: ARP failing), TCP
  connects never completed, and the router answered in 3 ms. That points at
  the panel's Wi-Fi link (RSSI had been −69 to −71 dBm), not its heap. So the
  2026-10-07 outage may have been Wi-Fi too; its low minimum free heap is a
  coincidence that the data cannot separate. A panel that does not accept
  connections cannot be restarted over HTTP: it needs a power cycle.

- **2026-10-10 correction:** the Wi-Fi explanation above was wrong. The
  "only one network visible" evidence was a stale Windows scan cache
  (`netsh wlan show networks` lists the last scan); a forced `WlanScan`
  showed the Wi-Fi network broadcasting at 78% on channel 1. The outage
  was the board failing (§14).

## 9. Feeds

- **Sky Watch (Planes).** adsb.lol is the primary feed. It answered 429 Too
  Many Requests at a 10 s poll, so since 2026-10-02 polling has been 20 s
  while a plane is overhead and 60 s when the sky is clear. It still answers
  429 now and then at that cadence: `utility_hub.log` has 23 primary-feed
  429s from 2026-10-02 00:34 to 2026-10-07 07:43. airplanes.live
  (`api.airplanes.live/v2/point/{lat}/{lon}/{nm}`) became the secondary in
  the same 2026-10-02 change and has answered 403 since at least its first
  logged use, 2026-10-02 00:34, so the fallback never worked. It asks callers
  to email them for access. On 2026-10-06 the code's secondary became adsb.fi
  open data (`opendata.adsb.fi/api/v3/lat/{lat}/lon/{lon}/dist/{nm}`), which
  serves the same readsb `ac` document, keyless. The running Hub (PID 7500,
  started 2026-10-06 18:36, before `sky_watch.py` changed at 22:19) still
  calls airplanes.live (403 again at 2026-10-07 07:47) and picks up adsb.fi
  only when it is restarted.

## 10. Logs, journal and reports

- **`panel_health.jsonl`** has had one line per ~30 s Hub probe since
  2026-10-04 15:50: `t`, `uptime`, `reason`, `free`, `min_free`, `block` and
  `fps`. `reachable` and failed-probe lines (`reachable: false` plus an
  exception class) were added 2026-10-04.
- **2026-10-06:** `hub_status.append_health` also writes `app` (the panel's
  `currentApp`) and `power` (`matrixPower`). The running Hub started at 18:36
  on 2026-10-06, before that change, so no live line carried them as of 23:29.
  They appear once the Hub is restarted.
- **2026-10-06: the cross-process mutation gate and journal** (`panel_gate.py`,
  81 tests in `test_panel_gate.py`) was built and wired into 19 call sites.
  The running senders started before it existed and pick it up only when
  restarted. As of 2026-10-06 23:29, `%LOCALAPPDATA%\AWTRIX` held no
  `panel_events.jsonl` or `panel_mutation.*` files.
- **2026-10-06: `panel_heap_report.py`** (38 tests) was added. It is a
  read-only report joining the health log and the journal. First run on the
  real logs: 2d07h of data, two boots, one failed probe (22:05, 31 s,
  `PanelTransportError`), and three silences with no probes, the longest
  1d18h.

- **First journal data, 2026-10-07 19:43 → 2026-10-08 22:03** (the senders
  restarted with the gate at a logon on 2026-10-07): 1,026 writes, 661 of
  them failed (mostly `ConnectTimeout` during the outage below). Attribution
  (correlation, not proof; baseline +6 B per 30 s interval with no writes):
  intervals containing PC stats deletes averaged −1,152 B of largest block,
  PC stats pushes −826 B, a `planes0` delete −1,170 B, weather pushes
  −179 B, companion pushes −66 B. The biggest single drop, −10,240 B two
  minutes after the 2026-10-08 21:35 boot, coincided with two PC stats pushes
  (3,338 B).
- **Outage, 2026-10-07 20:29 → 21:50 (81 min):** the panel stopped answering
  HTTP without resetting (uptime kept counting). Its boot had reached a
  minimum free heap of 5,444 B and block ~12 KB at ~22 h uptime. During the
  outage PC stats retried 369 times and the companion 259 times. Nightly
  maintenance rebooted it at 21:52. 2026-10-08 maintenance at 21:37 took the
  block from 5,876 to 32,756.
- **2026-10-08:** the redesigned PC stats icons were re-encoded with one
  shared palette per GIF (pixels unchanged): the combined CPU+RAM jewel
  957 → 464 B, its page ~2,210 → ~1,525 B, single icons ~40% smaller.

## 11. REST API

- `POST /apps/pushed/{name}` was listed as the "legacy pushed-app path (being
  retired)". **Superseded:** every sender in the repo pushes with
  `PUT /api/v1/apps/pushed/{name}` (`panel_client`, `now_playing_push`,
  `pc_stats_push`, the weather and AQI briefs, `meridian_push`). POST was not
  re-tested, because testing it would mean a mutating request.

## 12. Firmware 1.1.0 → 1.1.2 and the figures that moved

- Most heap figures in the older CLAUDE.md were measured on 1.1.0. The
  consolidated rules quote 1.1.2 boot logs. The 1.1.0 → 1.1.2 table is in
  `ROADMAP.md`, Phase 1: ~8 KB more free heap and a ~8 KB smaller post-boot
  block.
- `scriptLimit`/`scriptMaxBytes` were dropped in 1.1.2 (section 1).

## 13. Test-suite size

- 1,798 on 2026-10-03; "~1,840 tests, ~9 s" in the CLAUDE.md committed
  after it.
- 1,843 → 1,858 in 9.01 s (2026-10-04, stability pass). The Hub was
  restarted with 1,858 passing.
- 1,861 passed in ~8.5 s (2026-10-06, before the gate work).
- 1,980 collected on 2026-10-06 at 23:31 (`pytest.exe -q -p no:cacheprovider`):
  1,976 passed and 4 failed in 10.79 s. The 4 failures are icon and scene art
  assertions (`test_icon_library.py::test_frame_counts`, two in
  `test_pc_stats_push.py::ArtAndAnimationTests`,
  `test_scene_layout.py::SceneFamilyTests::test_every_lane_draws_the_same_ground_line`).
  They are in files another agent was editing for the visual redesign that
  night.
- 1,995 collected on 2026-10-07 at 07:34, the same command: 1,989 passed and
  6 failed in 11.90 s. The failures are
  `test_pc_stats_push.py` (4) and `test_e2e_awtrix_visual_polish.py` (2),
  all PC stats page assertions. They come from the redesign, which was still
  editing `pc_stats_push.py` and its tests at 07:30. The earlier icon and
  scene failures were passing by then.

## 14. Board replacement, 2026-10-10

- **The first board died.** Last good probe 2026-10-09 13:45:21 (healthy:
  min free 12,520, block 23,540, 47 min after a low-memory restart). Then 70%
  ping loss and failing ARP, then nothing. On 2026-10-10 the matrix stayed
  dark on a known-good power supply. On the PC's USB its CP210x enumerated
  (COM4, the Windows chime) but the red LED stayed off and the ESP32 never
  answered — not esptool at 115,200/57,600/9,600 baud with auto-reset, not
  manual BOOT+RST, not a passive listen at 115,200/74,880. The ROM bootloader
  answers even with corrupt firmware, so this was hardware: the 3.3 V supply
  (the red LED sits on it) or the ESP32 itself. Software restarts cannot cause
  that; nothing had reached the board since 13:45.
- **The replacement board** (ESP32-D0WD-V3 rev 3.1, 4 MB, MAC
  `68:09:47:37:6c:fc`, same TC001 pin map) arrived boot-looping: an app image
  cut off at 12 KB sat in the bootloader slot (0x1000) and the partition table
  was blank, so the ROM watchdog reset it every ~300 ms
  (`RTCWDT_RTC_RESET`, `load:0x3f400020,len:69564`).
- **Flashed** after a full erase with `usb-awtrix-ng-4mb.bin` from the
  official v1.1.2 `usb-awtrix-ng.zip` (sha256 `72ac4c83…` verified against
  GitHub's digest; the 4 MB image is bootloader @0x1000, partitions @0x8000
  — nvs, otadata, app0/app1 1,728 KB, spiffs 512 KB, coredump — and the app
  @0x10000, byte-identical to `firmware-awtrix-ng.bin`, which is an
  app-only *update* image and cannot be flashed at 0x0). 99 s at 115,200
  baud, hash verified; first boot opened the `awtrixng-376cfc` setup AP.
- **Restored** from the 2026-10-06 snapshot: 5 system keys (geometry
  `bottomRight`/`rows`/no serpentine, timezone, hostname) via a partial
  `PUT /api/v1/system`, 12 display keys via `PATCH /api/v1/settings`
  (brightness left to Nightkeeper), the loop order via `PUT /apps/order`,
  static IP `192.168.1.90`, then scripts by hash (nightkeeper, ocean, moon
  v2.1); Now Playing installed itself after its sender was restarted (it had
  seen the PC on the setup AP at 192.168.4.2 and tried to reinstall for it).
- **Boot log, all four scripts:** shared 81,288 B; after the three-script
  boot the block was 47,092 (the first board's best was ~33 KB with four).
  Baseline snapshot: `backups/panel-snapshot-20261010-133525`.
