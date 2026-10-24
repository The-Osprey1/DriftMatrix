# AWTRIX desk controls and reminders

The desktop window provides Focus Sprint preset buttons, pause/resume, five
extra minutes, finish and stop. It also creates one short desk reminder with a
deadline, five-minute snooze, dismissal, and an exact source-pixel preview.
Use the **Keep on top** checkbox for a small always-available desk controller.

The **Tea · 5m**, **Water · 30m**, and **Break · 60m** shortcuts fill the form
and preview the label. They never start or replace a reminder until you click
**Set reminder** (or press Enter while editing either reminder field). When a
saved reminder exists, the button reads **Replace reminder**. Snooze is disabled
when there is no valid reminder, and Dismiss is disabled when no file exists.

The desktop reminder status shows minutes and seconds remaining plus the local
due time. The physical panel keeps its calmer minute/hour display. Your last
successfully scheduled label and duration, plus **Keep on top**, are remembered
in `%LOCALAPPDATA%\AWTRIX\desk_preferences.json`. Demo preferences stay in its
temporary folder. Invalid preferences fall back to defaults without overwriting
the original file. The window scrolls to keep controls reachable on smaller
screens and at larger Windows text sizes.

## Open the controls

From the AWTRIX project folder:

```powershell
.\'Desk Controls.ps1'
```

Or run `C:/Users/devyn/anaconda3/python.exe desk_controls.py`.
Opening the window only reads local state. It does not start a second Utility
Hub, restart tasks, contact the panel, or change brightness or power.

To explore without controlling the live timer:

```powershell
.\'Desk Controls.ps1' -Demo
```

Demo mode uses temporary reminder files and a sample focus display. Focus
buttons do not send commands. Temporary reminders are deleted on a normal exit.

## Focus controls and status

The preset buttons use the existing timer schedules: Sprint is 15 minutes,
Classic 25 minutes, and Deep 50 minutes. Work/rest behavior remains owned by
Focus Sprint. The displayed time is its last saved checkpoint, refreshed once
a second by the window; it may lag the controller's next tick.

The window disables focus buttons if the saved checkpoint is missing, invalid,
in the future, or older than 90 seconds. This is evidence that the **local
focus controller is updating**, not proof that the physical display is online.
After a button press, the window waits for that exact command ID to appear in
the controller's saved consumed-command list. A consumed command may be a
no-op when it does not apply to the current timer phase. It does not claim a
successful panel refresh. Pending commands keep the buttons disabled; the
existing queue remains available for diagnosis if the controller stops.

The window now separates three indicators:

- **Timer service** uses the fresh focus checkpoint.
- **Panel** reports the Hub's latest read-only device check, including its age.
  The Hub checks at most every 30 seconds; missing or stale reports never claim
  the panel is reachable.
- **Reminders** reports whether the running Hub has reminders enabled. While
  a reminder is active, it distinguishes waiting for delivery from a successful
  panel write for that exact reminder. This receipt expires after 30 seconds
  and does not claim that the page is currently visible or physically legible.

The Hub writes this small local heartbeat to
`%LOCALAPPDATA%\AWTRIX\hub_status.json`. The desktop window still makes no
network requests and does not expose private configuration in the status file.

## Reminder behavior

- One reminder at a time; **Set / replace** replaces the previous schedule.
- Choose 1–1440 whole minutes and a short label such as TEA, WATER or BREAK.
  Labels are converted to uppercase and must fit the actual 23-pixel lane.
  Letters, digits, spaces and dashes are supported. The window rejects labels
  that would be clipped rather than silently truncating them.
- The countdown alternates its label and remaining minutes/hours every ten
  seconds. Values round upward; `1M` means at most one minute remains.
- A draining rail represents time remaining; a different bell silhouette marks
  the due state. State remains recognizable when colors collapse at low light.
- Deadlines continue through sleep. Waking within ten minutes of the deadline
  still shows the due reminder; older reminders expire without a late alert.
- A backward clock earlier than creation hides the reminder. Clock changes
  otherwise affect deadlines because these are wall-clock reminders.
- Snooze moves the same label to five minutes from now. Dismiss removes the
  schedule, then the Hub removes its own pushed app on the next refresh.
- A due reminder attempts forced display once per service run. Shared quiet
  hours and activation cooldown can suppress that attempt; it is not retried.
  Normal rotation can still show the reminder until its ten-minute expiry.

The schedule lives at `%LOCALAPPDATA%\AWTRIX\desk_reminder.json`. The label
remains local until the enabled Hub sends its rendered pixels to the LAN panel.
Closing the control window does not cancel an existing reminder.

## Enable panel delivery separately

This feature is **disabled by default** in the example configuration. It was
enabled on this desk's existing Utility Hub on 2026-09-19. Saving a reminder
does not enable a disabled Hub automatically.
Merge this section into the existing private
`%LOCALAPPDATA%\AWTRIX\utility_hub.json`; preserve all other configuration:

```json
"desk_reminder": {"enabled": true}
```

The Utility Hub reads configuration at startup, so a controlled restart of
the existing **AWTRIX Utility Hub** task is required. Do not launch a second
Hub alongside it. Verify that `deskreminder` joins the intended app rotation
and completes a countdown → due → dismiss lifecycle on the physical panel.
The deployment on 2026-09-19 preserved the other private configuration sections
and restarted only that existing task. Local rollback evidence is under
`%LOCALAPPDATA%\AWTRIX\backups\desk-reminder-deploy-20260919-142239`.

The page uses a static RGB888 bitmap, under 2 KB, without a GIF decoder or a
new Berry script. It refreshes every five seconds and expires on the panel
after 30 seconds without refresh. Outages use bounded retry backoff. Every
write, activation and deletion goes through the existing shared PanelClient;
safe deletion verifies the page is a pushed app.

## Artwork verification

```powershell
C:/Users/devyn/anaconda3/python.exe desk_reminder_preview.py --out "$env:LOCALAPPDATA/AWTRIX/previews/desk-reminder"
```

This exports native 32×8 images and a contact sheet showing source pixels next
to a brightness-1 visibility mask. The mask amplifies surviving channels for
inspection and is not a simulation of perceived physical brightness.

Automated checks cover local controls, exact command acknowledgment, reminder
persistence and validation, deadline/sleep/expiry boundaries, due activation,
replacement, cancellation, outage retry, payload size and native GUI creation.
Live delivery was verified on 2026-09-19 through the running Hub: the reminder
joined normal rotation, reached the due/bell page, returned to a five-minute
countdown after snooze, and disappeared after dismissal. The panel stayed up;
brightness remained 45, the saved rotation stayed unchanged, and no script
errors were reported. Countdown, due and snooze framebuffer captures plus
`live-summary.json` are in the deployment evidence directory above.

These are panel framebuffer captures, not camera photographs. Physical diffuser
appearance and perceived brightness in the room have not been measured remotely.
The full suite passed 1,037 tests before this deployment.
