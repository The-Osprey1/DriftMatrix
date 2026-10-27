# App overhaul

The active objective is to revise weak apps, improve functionality and add everyday
quality-of-life features. The user's first priority is **animations, layout and
visual polish**. This is broader than the first visual pass below.

## Acceptance requirements

- Inspect every currently enabled app family and preserve useful existing behavior.
- Make weak artwork readable at 32×8, with distinct silhouettes, intentional motion,
  clear values and no text/meter collisions. Keep static and low-light variants useful.
- Verify the actual exported payloads and frame timing, including current firmware
  encoding, rather than relying only on simulated source art.
- Improve weak functionality and everyday controls, including configuration awareness,
  command feedback/recovery and the installed launch path.
- Keep changes reviewable in GitHub. Review visual previews before live adoption.
- Preserve guarded transport, source/config/task rollback, Nightkeeper policy and
  the panel's constrained memory budget. Any physical deployment requires a fresh
  snapshot and post-deployment verification, with physical appearance kept separate.

## Evidence and progress

The October 26 read-only check found the pinned 1.2.2 host healthy with all five
workers running and no automatic worker restarts. The clock had about 100 minutes
uptime, no resident script errors and a 34,804-byte largest free block. All six CI
jobs for firmware compatibility passed. These are observations, not long-term proof.

| Workstream | Current evidence | Remaining work |
| --- | --- | --- |
| Weather | Rounded cloud edges and layered overcast; rain advances without white drop flashes. Animated/static previews measured. | Review the full condition family; adopt a suitable motion policy after staged panel verification. |
| PC stats | Six distinct GPU rotor poses with a consistent loop seam; existing numeric lane preserved. | Review CPU/RAM/combined/NET identity and readability in their actual alert states. |
| Desk Companion | Cleaner round silhouette, 120 ms blink, unchanged 1.92 s cycle and five mood props. | User visual review, then staged host adoption and physical appearance check. |
| AQI | Existing six category designs retained as reference. Live service is explicitly static. | Review category legibility, severity pacing and whether animation should be enabled. |
| Moon / Ocean | Existing installed sources preserved. Ocean 3.0 candidate adds visible-time lifecycle, a clearer first visitor and compact exact-color caches. See `OCEAN_OVERHAUL.md`. | Moon review; exact-identity legacy Ocean rollback support; then guarded physical memory/FPS and appearance checks. |
| Now Playing / information apps | Existing Now Playing behavior is the visual quality reference. | Review title/progress behavior, Focus, reminders, calendar and alert layouts against that standard. |
| Everyday controls | Audit found missing installed Desk Controls modules, legacy-only launch/config assumptions and weak pending-command feedback. | Complete the unified controls/QoL pass. The early local draft is preserved separately and is not part of this visual change. |

## First visual pass

13 updated scenes within a 26-scene comparison set: seven weather states, five
companion moods and GPU activity. Other scenes are explicit reference designs.
Readings, text, numeric rails and scheduling are unchanged in this pass. Live
weather and AQI still use `static_only: true`; the animation preview does not claim
that the deployed configuration already animates these feeds.

![Proposed artwork](figures/app-overhaul/redesign.gif)

Download/open `figures/app-overhaul/index.html` for the self-contained comparison.
It includes category filters, updated-scenes filtering, pause/play, 120 ms stepping,
real static fallback payloads, low-light threshold view and current firmware wire sizes.
The baseline was captured before source edits; lane hashes separately protect values
and layout. The gallery requires no network or device access.

Regenerate from the repository root:

```powershell
& 'C:/Users/devyn/anaconda3/python.exe' -X utf8 led_redesign_preview.py --before figures/app-overhaul/before.json --out figures/app-overhaul
```

Validation: **2,757 tests passed in 54.42 seconds**, including 31 new checks. Maximum
animated wire size is **1,912 bytes** against the 2,048-byte limit. Browser interaction
checks covered filtering, pause, stepping, static fallback and low-light selection.
Desktop gallery rendering was inspected. Narrow viewport, screen-reader behavior,
physical LEDs and post-deployment stability have not been verified for this pass.

No live scripts, configuration or scheduled tasks were changed by this visual pass.
The overall overhaul remains active; this first batch does not complete it.

## Ocean candidate follow-up

`ocean-v3.ax` is a separate candidate; the installed-source reference `ocean.ax`
is unchanged. The source-executed 30-second preview and lifecycle tests are in
`OCEAN_OVERHAUL.md`. This follow-up improves encounter visibility and hidden-time
behavior without adding more scene elements. Its actual total VM heap use,
real device heap/FPS and physical acceptance still require verification.
