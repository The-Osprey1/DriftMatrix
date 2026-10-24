# Stability pass — 2026-10-04

The recovery fixes are active in the Utility Hub. This pass verifies software
behavior and current API/runtime health; it does not establish the root cause
of the earlier panic or long-term device stability.

## Repairs

- PanelClient keeps its health baseline during a requested restart. A software
  reset preserves animation; panic, watchdog, and unknown reset reasons select
  static fallback. Pre-reset health reads cannot turn a planned restart into a
  false crash. Every observed uptime rollback advances a reboot generation.
- Desk Companion now supplies static artwork to the shared client. After an
  observed reboot it restores the page on the next tick rather than waiting
  for its four-minute keepalive. A failed restoration remains retryable.
- Maintenance captures the resident script names before reboot and verifies
  they return. A panic, missing script, script error, or lit matrix needs
  attention. Malformed device, inventory, or log data is unverified. Reset
  reasons are retained in the report, and malformed saved history cannot trap
  the controller during report completion.
- Health logging now retains failed probes, with an exception class rather
  than an error body or URL. The Hub's tick interval is capped at 30 seconds
  even when the only enabled service has a longer keepalive.

## Verification

- Baseline: 1,843 tests passed.
- After repairs: **1,858 tests passed in 9.01 seconds**. These include real
  PanelClient/fake-HTTP regressions for pre-reset reads, software and panic
  reboots, companion recovery and failed retries, incomplete maintenance data,
  missing scripts, and outage/recovery logging.
- Independent code review found malformed script presence could falsely count
  as restored. That was repaired before the final test run: presence must be
  an actual Boolean.
- Private Git whitespace validation passed. No new release soak gate was
  created; the retired September gates remain retired.

## Runtime application

Only the `AWTRIX Utility Hub` scheduled task was restarted, at 16:09 PDT.
The old process exited before its replacement started. PID 3040 is the new
single Hub instance; the other sender processes were left running.

Immediately after application:

| Readback | Observation |
| --- | --- |
| Firmware | 1.1.2 |
| Uptime | 2,327 seconds, continuing through the Hub restart |
| Last reset reason | software |
| Largest free block | 10,228 bytes |
| Free heap | 35,012 bytes in this sample |
| Minimum free heap since boot | 15,012 bytes |
| Wi-Fi RSSI | -68 dBm |
| Scripts | nowplaying, ocean, moon, nightkeeper; all present, error null |
| Companion | companion0 present as a pushed app |
| Hub heartbeat | fresh, panel reachable, PID matches running process |
| Health log | new reachable field present in live samples |

This pass did not replace resident Berry scripts or reboot the panel.
Successful API readback does not establish physical LED appearance.

## Remaining evidence and memory work

The largest free block stayed at 10,228 bytes in the observed health samples.
The software repairs do not reclaim the resident scenes' heap. Reducing scene
memory, particularly ocean's cached runtime tables, remains a separate design
and on-device measurement task. The earlier panic occurred during a companion
push; that timing does not prove the push caused it.

Nightly maintenance is already enabled for 21:35–23:00, requires the matrix to
be off, and requires at least 20 hours of uptime. With today's afternoon
panel restart, tonight's window will skip for insufficient uptime. The next
expected eligible window is October 5, if the PC is awake and the panel is dark.
A real automatic maintenance outcome has not yet been observed in this pass.

Rollback evidence is under
`C:/Users/devyn/AppData/Local/AWTRIX/backups/stability-20261004-160707/`:
original private-HEAD source archive, pre-change configuration and scheduled
task, device/display/apps readbacks, the prior health timeline, and source
changes. Runtime snapshots remain private and outside the source repository.

## Follow-up — 2026-10-06

- **Maintenance worked.** The first real nightly run, at 21:37, came back
  `ok`. Uptime was 54.1 h, the reset reason was `software`, and the boot took
  12 s with the matrix dark throughout. The largest block went from 8,692 to
  32,756. All four scripts came back with no errors.
- **The block does not stay up.** It fell from 32,756 at boot to 18,420 at
  26 minutes and to 16,372 at 52 minutes. A read-only `GET /device` at 23:29
  (uptime 6,702 s) still showed 18,420, with FPS 41 and reset reason
  `software`. The previous boot had sat near 10,228 from its first half hour,
  then fell to 6,644–8,692 by 54 h, with a low of 4,596.
- **Frame rate.** 181 of the previous boot's 542 health samples (33%) were
  below 40 FPS. When first counted that evening, this was 181 of 589 (~31%).
  None of the 207 samples from the reboot to 23:29 were below 40, but all of
  them were taken during the blackout. On the morning of 2026-10-07, after
  the blackout, 33 of 60 samples from 07:22 to 07:54 were below 40.
- **New evidence tooling, not yet live:**
  - A cross-process write gate and journal (`panel_gate.py` →
    `panel_events.jsonl`).
  - `app`/`power` fields in `panel_health.jsonl`.
  - A read-only report, `panel_heap_report.py`, which runs today against the
    existing health log.

  The running senders and the Hub started before these code changes, so the
  gate and the new fields take effect only when each one is restarted. No
  journal exists yet. Nothing was sent to the panel, and no task was
  restarted.
- **Planes feed.** In the code, the Sky Watch fallback is now adsb.fi:
  airplanes.live has answered 403 since at least 2026-10-02. The running Hub
  picks up adsb.fi only when it is restarted. adsb.lol still answers 429 now
  and then at the 20 s / 60 s cadence.
- **Tests.** The last run, on 2026-10-07 at 07:34, collected 1,995: 1,989
  pass and 6 fail. All 6 are PC stats page assertions in files the
  in-progress visual redesign was still editing. The baseline before the
  gate work was 1,861.
- **Not committed.** This pass's changes and today's are still uncommitted
  in the private Git (`.\awtrix-git.ps1 status`; this file is untracked).
- Dated measurements now live in `PANEL_HISTORY.md`.
