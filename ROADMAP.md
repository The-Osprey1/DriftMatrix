# AWTRIX overhaul roadmap

Started 2026-10-03. The previous plan was scrapped on 2026-09-26 and is still
in Git history (`.\awtrix-git.ps1 log -- ROADMAP.md`). This plan starts from
what the panel was doing on 2026-10-03, not from that plan.

## Where things stand

Live readback, 2026-10-03 14:00:

| Signal | Value | Read |
| --- | --- | --- |
| Firmware | 1.1.0 | **two releases behind** (1.1.2 released 2026-09-19) |
| Uptime | 7.0 days | no reboot since 2026-09-26 |
| Largest free block | 12,788 B | was ~30 KB after boot; installs need ~2–3.5× source |
| Min free heap since boot | 9,516 B | close to the floor |
| FPS | 32 | target 40; `ocean` is current |
| Wi-Fi RSSI | −71 dBm | weak; the transport already tolerates dropouts |
| Scripts | nowplaying, ocean, moon, nightkeeper | `ocean` costs 20,299 B of VM heap and has no source in this repo |
| Tests | 1,798 pass | 37 of them new today |

The software side is mature: one visual system, a shared transport with
rate limiting and a static fallback, a Utility Hub hosting alert-only pages,
and a large test suite. Nearly every remaining limit is the ESP32 heap. So the
overhaul goes after the heap first, then spends what it frees.

## Phase 0: Foundations (done 2026-10-03)

- **Nightly maintenance reboot** (`panel_maintenance.py`). Restarts the panel
  once a night between 03:30 and 05:00, only when uptime is at least 20 h and
  the matrix is dark. Afterwards it records the largest block before and
  after, per-script boot heap, script errors, and how long the matrix stayed
  lit. Without it the panel drifts to the ~9-day point where the API refuses
  large bodies, and the per-script heap figures go stale. **Shipped disabled;
  turn it on with `"maintenance": {"enabled": true}`.**
  *2026-10-04: enabled, and the window moved to 21:35–23:00 because the PC
  sleeps overnight and 03:30 never ran. First real run 2026-10-06 21:37:
  `ok`, largest block 8,692 → 32,756, all four scripts back, no errors.*
- **`PanelClient.reboot()` / `expect_reboot()` / `read_json()`**. A planned
  restart no longer looks like a crash to the Hub's own client, so the Hub
  does not latch static art for the night.
- **ISS Watch** (`iss_watch.py`). A new alert-only page for visible
  space-station passes, using open data from wheretheiss.at. It is quiet in
  daylight and during the blackout, and costs no script heap. **Shipped
  disabled; `"iss": {"enabled": true}`.**
- CLAUDE.md reconciled with the live panel (it named two different sets of
  installed scripts).
- **Proof that maintenance works, measured live.** A manual reboot took the
  largest block from 12,788 to 45,044 B. (FPS is *not* fragmentation:
  sampled per app after the upgrade, `ocean` runs at 28-29 FPS and every
  other page at 42. `ocean`'s `draw()` is the cost.)
- Pre-upgrade snapshot: `backups/2026-10-03-pre-firmware-1.1.2/`.
- **Planned reboots no longer freeze the icons.** Weather, AQI and Now
  Playing locked into static art after *any* reboot until their process
  restarted (PC Stats recovered on its own). Combined with the nightly reboot,
  they would have been static permanently. They now ignore a restart the
  panel reports as `software` (API reboot, OTA) and still latch on `panic` or
  a watchdog. An unreadable reason counts as a crash.

## Phase 1: Firmware 1.1.2 (done 2026-10-03)

Installed by manual upload (System → Maintenance): the in-UI updater only
exists from 1.1.1. All four scripts restored with `error: null`. Boot log,
same scripts, 1.1.0 → 1.1.2:

| | 1.1.0 | 1.1.2 |
| --- | --- | --- |
| moon VM heap | 1,693 | 2,893 |
| nightkeeper | 7,104 | 7,104 |
| nowplaying | 14,502 | 14,502 |
| ocean | 20,299 | 18,939 |
| free heap after all four restore | 55,900 | 64,116 |
| largest block after all four restore | 49,140 | 40,948 |

So: ~8 KB more free heap, but the largest block after boot is ~8 KB smaller,
and the block is what decides installs. The upgrade is not the big heap win the
release notes suggested; the nightly reboot remains the thing that matters.
Watch `maintenance.json` over a few nights before drawing more conclusions.

Original rationale, kept for reference:

Why: the 1.1.1 and 1.1.2 release notes target the exact problems this repo
works around:

- Berry VMs, regex and GIF decoding *retain less temporary memory*.
- Script apps release their icons when hidden. File access and log/scan
  responses use less temporary memory. This may lift the ~8 KB readback
  ceiling that keeps `ocean`, `sunset` and `moon` from being backed up.
- The fixed `scriptLimit`/`scriptMaxBytes` caps are gone; real memory is the
  only gate.
- New script API: `timer.after/every`, `display.power()`/`display.is_on()`,
  `on_button_event()`, up to four icons per pushed app, `iconGap`, and a
  `/fullscreen` live mirror page.

Breaking changes checked against this repo: the button webhook is now JSON
(not used here), and `scriptLimit`/`scriptMaxBytes` writes are now ignored
(not written anywhere here). Nothing in the repo depends on the old behaviour.

Procedure:

1. Reboot, then `panel_snapshot.py` into `backups/2026-10-xx-pre-1.1.2/`.
2. Web UI → System → **Check for updates** → **Download & install**
   (`firmware-awtrix-ng.bin`, classic ESP32). Settings and files are kept.
3. Verify: `GET /device` shows 1.1.2. `GET /apps` shows all four scripts
   with `error: null`. The next nightly maintenance report records the new
   per-script heap.
4. Rollback: upload the 1.1.0 `firmware-awtrix-ng.bin` the same way, then
   restore the snapshot.

## Phase 2: Spend the heap better (after Phase 1)

Measure first: the maintenance report gives fresh per-script heap every
morning.

- **Nightkeeper on `display.power()` (done 2026-10-03, v3.0).** The
  loopback HTTP PATCH, its in-flight/timeout/retry state and the two-minute
  re-check are gone. `display.is_on()` is read every tick, and it notices
  changes made over HTTP within a second (probed on the panel), so manual
  overrides are still held; verified live. VM heap 7,104 → 4,081 B; minified
  source 4,185 → 2,685 B. After boot with all four scripts: free heap 62,972,
  largest block 59,380.
- **Slim `ocean`.** Its source is now in the repo (`ocean.ax`, recovered
  2026-10-03). v1 (9,605 B) read back fine right after a reboot, so the
  "8 KB ceiling" was fragmentation. At 20,299 B of VM heap it is the largest
  resident cost; most of that is runtime tables built in `init()`.
  *2026-10-06: still open, and larger since the speed-up below. ocean v2
  costs 26,361 B at boot, the largest of the four. v2 has never been read
  back (only v1 has): every snapshot since it went in got a connection error
  or zero bytes, with the block at 19,444 or less, so the panel running the
  repo's `ocean.ax` (`5000462e…`) is assumed, not verified
  (`PANEL_HISTORY.md` §4–5). A readback in the first few minutes after a
  maintenance reboot, while the block is still above ~30 KB, would settle
  it.*
- **`sunset` is back, and the scenes follow the sky (done 2026-10-03).** All
  five scripts restore at boot (85% of script heap). `sunset` publishes
  `sunset.phase`; `ocean` shows by day, `sunset` in the hour around sunset,
  `moon` at night, each falling back to fixed hours without the signal.
  *2026-10-04: `sunset` is off again, to make room for the old moon tracker
  (`weather/moon-panel.ax`), which the user asked to have back. Four scripts
  now restore: 80,788 of 98,304 B (82%). `moon` stays in the loop all day,
  and `ocean` uses its fixed 07–17 hours.*
- **Timers instead of `now_ms()` polling** in `nowplaying` and `moon`, where
  that shrinks code.
- **`ocean` sped up (done 2026-10-03): 27–29 → 33–35 FPS.** Changes: a
  sine table instead of per-frame `sn()` calls; shaft colours blended inline
  and cached, 8 columns recomputed per frame; member lookups pulled out of
  loops; the seabed's bottom row drawn as one `line()`; shaft row 4 (≤8 %
  blend, ≤6/255 change) dropped. Cost: +7 KB VM heap for the tables. 42 FPS
  would mean removing scene elements, which is a design call.

## Phase 3: New pages and ideas from the community

Verdicts on ideas from AWTRIX/LED-matrix projects online:

| Idea | Source | Verdict |
| --- | --- | --- |
| ISS visible-pass alert | wheretheiss.at open API | **Done** (Phase 0) |
| Scheduled maintenance reboot | common ESP32 practice | **Done** (Phase 0) |
| AWTRIX Hub flows (awtrix.de, 313 flows) | Blueforcer | Browse and port small ones *as pushed apps*. Installing them as scripts costs heap |
| Physical-button actions (`on_button_event`) | NG 1.1.2 | Only if this build has buttons. Long-press could snooze an alert page |
| Windows notification mirror (app icon only, allowlist) | LaMetric/AWTRIX notify bridges | **Built 2026-10-03** (`notify_mirror.py`), off by default. Windows notifications are switched off on this PC (`ToastEnabled = 0`), so it has nothing to read until they are turned on |
| `/fullscreen` mirror in Desk Controls | NG 1.1.1 | Cheap: an iframe of what the panel shows |
| Home Assistant / MQTT | AWTRIX integrations | Only if HA is in the house; MQTT is disabled on the panel today |
| Tetris / Game-of-Life clock faces | LED-matrix classics | Rejected for now: a resident animated script is the most expensive thing this panel can run |

## Phase 4: Hygiene

- Show the maintenance report's last outcome in `hub_status` / Desk Controls.
- Wi-Fi at −71 dBm: moving the panel or the access point would do more for
  reliability than any code.
- Retire the duplicate/stale sections of CLAUDE.md as each phase lands.
  *2026-10-06: the dated measurements and superseded claims are now in
  `PANEL_HISTORY.md`. A consolidated CLAUDE.md that keeps only the current
  rules was applied on 2026-10-07.*
- **Done 2026-10-06:** a cross-process panel write gate with a journal
  (`panel_gate.py`, `panel_events.jsonl`), and a read-only heap/FPS report
  (`panel_heap_report.py`). The gate takes effect for each sender only when
  it is restarted, and the health log gains `app` and `power` only once the
  Hub is restarted. The report already reads today's logs.
