# Desk reminder visual refinement — 2026-09-19

Implemented a lighter hourglass silhouette, warm sand positions that reflect
remaining time, pearl text, mint time units, and a rounded bell with a small
highlight. The black gutter, shared font, remaining-time rail, static payload,
and label width limits remain intact.

The desktop window now has styled dark fields and buttons, a centered timer,
mint primary actions, quieter secondary actions, more deliberate spacing and
a darker LED preview. The controls and their local-state behavior are unchanged.

## Evidence

Local before/after native frames and the inspected comparison are under:

`C:/Users/devyn/AppData/Local/AWTRIX/previews/desk-reminder-polish/`

- `before/`: source pixels exported before this refinement.
- `after/`: revised native frames and brightness-1 visibility mask.
- `before-after.png`: matched-size comparison with equal horizontal/vertical
  pixel pitch; countdown, label, and due states.

Inspected the 5-minute, WATER label, under-one-minute, due and 24-hour frames.
Text stays inside the 23-pixel lane. The hourglass has more negative space;
the sand visibly transfers between bulbs. Pearl lettering and the bell retain
their shape in the low-brightness mask. This mask amplifies surviving channels
and is not a prediction of perceived brightness.

Validation: 29 focused reminder/control checks pass, including native Tk window
creation and requested layout size checks. The native desktop window was not
screenshot-reviewed in this pass. Physical brightness and panel appearance
remain unverified; no live deployment or service restart was performed.

## Subsequent live verification — 2026-09-19

After user authorization, enabled reminders and restarted the existing Utility
Hub once. The normal rotation, due cue, five-minute snooze and dismissal passed
the live lifecycle check. Inspected the countdown, bell/TEA and snoozed/5M
framebuffers read from the device. Power stayed on at brightness 45, saved
rotation was identical, uptime increased and no script errors were reported.

Evidence: `%LOCALAPPDATA%/AWTRIX/backups/desk-reminder-deploy-20260919-142239/`.
`live-summary.json` records the results. Framebuffer pixels confirm firmware
rendering; they are not a camera check of the physical diffuser or room lighting.
