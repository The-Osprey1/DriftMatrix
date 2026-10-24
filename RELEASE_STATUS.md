# AWTRIX release-hardening status

Last updated: 2026-09-07 17:53 America/Los_Angeles

## Outcome so far

The current live configuration is the Ocean/Moon profile: Ocean has replaced
Meridian, Sunset is disabled to stay within the ESP32 heap budget, and Now
Playing is in the rotation only when media is active. The panel is responding
normally and has been up for roughly six days since its last reset. The
firmware still reports that last reset as `panic`; there is no current panic
line in the panel log, so this remains an investigation item rather than a
confirmed recurring crash.

The release-hardening rollout is live at the immediate gate:

- Exact saved rotation: `Time, weatherbrief, nowplaying, Time, ocean, moon,
  aqibrief, pcstats0, networksentinel, nextup, focus, nightkeeper`.
- Disabled pages: `Battery, Date, timepiece, pcstats1, meridian, sunset`.
- Rotation timing: 8,000 ms, `Dim`, 360 ms.
- Live readback: scripting on, 42 FPS, brightness 45, matrix power on, and no
  installed-script errors.
- Latest soak baseline: 50,080 bytes free, 32,756-byte largest block.
- Full offline suite after the latest monitor change: 925 tests passed.
- A fresh 24-hour soak is running from
  `%LOCALAPPDATA%\AWTRIX\soaks\soak-20260907-175255`.
- Its first sample has zero violations and zero warnings; it is the first run
  using the sender-restart correction below.

## Current script deployment

The live Berry pages use the following deliberate policy:

- Now Playing stays in the configured loop but is skipped while no media is
  active, so the panel does not show stale or misleading playback.
- Ocean is the calm animated daylight page after the second Time anchor.
- Sunset is disabled because its larger animation was not safe for the
  current ESP32 heap budget.
- Moon remains available whenever the clock is valid.
- Nightkeeper remains enabled, headless, and outside the visual loop.

The PC-side senders are running under Task Scheduler. Weather is updating,
Now Playing is reacting to Chrome media changes, PC Stats stays quiet until
CPU or RAM crosses its elevated-value threshold, and AQI stays absent when air
quality is good. Utility Hub is currently running Network Sentinel; Next Up is
idle because no calendar source is configured.

A pushed app named `mikuwave` was observed in an earlier readback, but it was
not part of the saved rotation and had expired by the 17:52 soak start.

## Rollback and source control

The private local Git baseline contains these checkpoints:

- `0f3bbbb` — original source baseline.
- `e1e681e` — byte-exact snapshot tool and hardened rotation rollout.
- `e7d0beb` — 24-hour panel/Nightkeeper soak monitor.
- `068064c` — guarded live Network Sentinel and Next Up canaries.

OneDrive/Controlled Folder Access makes the workspace's old `.git` placeholder
read-only. The functional Git database therefore lives at:

```text
%LOCALAPPDATA%\AWTRIX\git\awtrix.git
```

Use the repository wrapper for ordinary commands:

```powershell
.\awtrix-git.ps1 status
.\awtrix-git.ps1 log --oneline
```

This is a private local checkpoint, not a public-release repository. Exact
weather coordinates and machine-specific paths still need parameterization
before any remote is added.

The exact pre-write rotation values are stored under:

```text
%LOCALAPPDATA%\AWTRIX\backups\rotation\20260823-195919
```

Both the app-loop JSON and rotation-settings JSON were written before the first
panel mutation.

## Snapshot status

`panel_snapshot.py` now captures all JSON control surfaces and byte-exact Berry
sources, rejects zero-byte scripts, hashes each source with SHA-256, and rejects
an uptime rollback during capture.

The live JSON snapshot succeeded, but a complete source snapshot did not. With
the panel's largest contiguous block around 13–14 KB, the firmware closed or
returned zero bytes for the larger Now Playing, Moon, and Sunset source reads.
Nightkeeper remained readable. Incomplete attempts have no `manifest.json` and
must not be treated as rollback sets.

The current settings/order are independently backed up. A complete exact
source snapshot remains a maintenance-window gate; the deployment reboot was
used for compilation rather than for forcing large source reads.

## Watchdog correction — 2026-08-30

`panel_soak.py` now retries the device health endpoint twice for a transient
sleep-state timeout and marks a completed run `incomplete` whenever any
endpoint error occurred. Previously a one-shot check with a missing device
payload could be labelled `pass` if no other violation had accumulated. The
regression is covered by the soak tests and the full suite now passes 842
tests.

The corrected one-shot evidence is stored under:

```text
%LOCALAPPDATA%\AWTRIX\watchdog-check-20260830\soak-20260830-102401
```

It passed with one expected heap warning: the largest contiguous block was
17,396 bytes, below the 18 KB caution line but above the 12 KB failure line.

## Sender-restart correction — 2026-09-07

The soak monitor no longer treats a Windows PID change as a release failure.
Task Scheduler can restart a healthy sender, and the September 1 runs showed
that PID churn was making otherwise useful evidence fail immediately. The
monitor now records PID changes as a warning, fails only after a sender remains
missing for three detailed samples, and keeps a sender that was absent at the
first sample as warning-only until it has proven that it can run.

The change is committed as `35adb5f` and covered by the full 881-test suite.
The new soak started at 2026-09-07 17:52 America/Los_Angeles and is expected
to run through the 9:30 PM blackout and 7:00 AM wake transitions.

## Live canaries

Evidence is stored under:

```text
%LOCALAPPDATA%\AWTRIX\canaries\canary-20260823-200817\result.json
```

Network Sentinel passed the complete controlled lifecycle:

- Six synthetic high-latency observations produced `SLOW`.
- The real animated payload was pushed and visibly activated.
- Six healthy observations produced `BACK` with different framebuffer evidence.
- The app was deleted only after pushed-app ownership was confirmed.

Next Up passed the privacy-safe static lifecycle:

- A temporary local synthetic `.ics` file produced a real event 14 minutes out.
- The real calendar source, parser, controller, art, and panel transport reached
  the `15m` stage.
- The page was visibly activated.
- Advancing the injected clock past the grace window made it absent and safely
  deleted it.
- No private calendar URL or event was used.

The Next Up canary deliberately selected static safety art. Its full-width GIF
is refused below an 18 KB contiguous-heap gate; the panel reported 14,324 bytes.
This is a remaining live-art gate, not a functional calendar blocker.

## Previous 24-hour soak (ended by requested deployment reboot)

Windows task: `AWTRIX Release Soak 2026-08-23`

Evidence directory:

```text
%LOCALAPPDATA%\AWTRIX\soaks\soak-20260823-200358
```

Planned run: 2026-08-23 20:03:58 through 2026-08-24 20:03:58 local time.
It will sample every 60 seconds, increase to every 15 seconds around the 9:00 PM
fade-down/9:30 PM blackout and 7:00 AM wake/7:30 AM day boundaries, and capture
detailed app/log/process state every five minutes.

The live start check reported:

- Correct panel UID and exact rotation.
- Zero reboot or endpoint failures.
- No app errors; scripting running at 42 FPS.
- Nightkeeper exactly matched power on / brightness 45.
- All six PC-side services were present.
- One expected warning: largest contiguous block was 13,300 bytes, below the
  18 KB caution line but above the 12 KB failure line.

That run cannot be used as final acceptance because the requested deployment
reboot interrupted it. Start a fresh soak after the always-on pages have been
visually accepted; it must again cover the 9:30 PM blackout and 7:00 AM wake.

## Gate corrections — 2026-08-23 21:00

Two gates above were written on wrong assumptions. Both are now settled.

**Gate 2 (byte-exact snapshot) is done, and heap was never the cause.**
Retested with 22,516 bytes in the largest free block:

| script | installed | readback |
| --- | --- | --- |
| nightkeeper | 4,185 B | OK |
| nowplaying | 7,348 B | OK |
| sunset | 9,967 B | `ChunkedEncodingError` |
| moon | 10,574 B | `ChunkedEncodingError` |

The block did not move during the successful reads, so this is a response-size
ceiling between 7.4 and 10.0 KB, and no maintenance window lifts it. Waiting
for one would have been waiting for nothing. Now Playing reads fine now only
because minifying it took the install from 10.7 KB to 7.3 KB, under the ceiling.

`panel_snapshot.py` now rebuilds unreadable sources from the repository.
`minify()` is deterministic and was verified to reproduce the installed bytes
with an exact SHA-256 match for both readable scripts, so this is a genuine
byte-exact rollback set, not an approximation. Each manifest entry records its
provenance, and a script with no known repository origin still fails loudly.
A complete snapshot of all four scripts is captured.

**Gate 3 (full-width Next Up animation) is closed, not pending — and the
18 KB gate was unsafe.** Running it with 22,516 bytes free produced
`resetReason: panic`: a hard ESP32 crash mid-push that took the rotation and
every pushed app down with it. There was no HTTP error to catch. The panel and
all senders recovered on their own within two minutes.

A resident full-width animation costs contiguous block roughly 8.2x its file
size (worst of three measurements). The 7,010-byte payload therefore needs
about 70 KB with the safety factor now applied, and the best block ever
observed with the full script set is 29,684. The gate is now sized from the
artwork rather than being a flat number, and correctly refuses. **Do not raise
the ratio to make this pass.**

## Remaining gates

None. All remaining gates and future plans were scrapped on 2026-09-26 (see
`ROADMAP.md`). The 2026-09-07 soak stopped logging at 22:48 that night and was
never completed; it is abandoned, not pending.
