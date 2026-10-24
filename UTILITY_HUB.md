# AWTRIX Utility Hub

For the desktop focus controls and optional countdown/reminder page, see
[Desk controls and reminders](DESK_CONTROLS.md). Reminders are disabled by
default and share this Hub's existing panel transport when enabled.

The Utility Hub is the one PC process that will host Network Sentinel, Next Up,
and Focus Sprint. Keeping them together gives every app the same panel
write-rate limit, activation cooldown, quiet hours, reboot detector, and static
safety fallback.

## Network Sentinel

Network Sentinel is absent while the connection is healthy. It appears only
after a problem is confirmed:

- `ROUTER`: the PC cannot reach the local default gateway.
- `WAN`: the gateway works but both independent raw-IP targets fail.
- `DNS`: raw IP works but name resolution fails.
- `LOGIN`: Microsoft NCSI is intercepted or returns unexpected content, which
  commonly means a captive-portal login.
- `L 30%`: the rolling 12-sample path has at least 25% loss and three failures.
- `180MS`: at least four of six recent samples are 150 ms or slower.
- `BACK`: a twelve-second recovery confirmation, after which the page removes
  itself.

Hard failures require three consecutive observations. Recovery requires six
healthy observations. A single missed packet never creates an app.

The probe follows Windows' own network model: the active default gateway comes
from `Get-NetRoute`; the captive-portal check uses Microsoft's documented
`http://www.msftconnecttest.com/connecttest.txt` response. The two raw-IP probes
use different providers so one remote service cannot create a false WAN alert.

## Next Up

Next Up reads one or more private iCalendar feeds or local `.ics` files on the
PC. It stays absent until the next event is within two hours, then shows a
truthful minute countdown beside a compact calendar. The connected time bead
moves around the calendar as the event approaches, changes pace and color at
15 and 5 minutes, and lands on the calendar mark at `NOW`.

The calendar is refreshed at most every five minutes. Recurrences, exclusions,
moved instances, cancellations, time zones, daylight-saving transitions, and a
ten-minute just-started grace window are handled before anything reaches the
panel. A recent last-good copy may be used for up to six hours during a source
outage; stale, malformed, oversized, or unconfigured sources fail closed and
leave the page absent.

Forced display happens once at 15 minutes, 5 minutes, and `NOW`; ordinary
minute refreshes never steal the screen. Long labels use a proportional 3x5
font and move exactly one physical column every 120 ms with a gamma-aware edge
fade. Optional exact-title aliases keep recurring calendar labels useful on a
32x8 display.

## ISS Watch

Absent unless the International Space Station can actually be seen from the
house: above 10 degrees, in sunlight, and the sun here more than 6 degrees
below the horizon. The lane says where to look, `NW 42` (compass point and
degrees above the horizon), and the row-7 meter grows as it climbs. The page
is brought forward once per pass, because a pass lasts only a few minutes.

Nothing is fetched in daylight or during the blackout. In the dark it polls
the keyless [wheretheiss.at](https://wheretheiss.at) feed once a minute,
switching to every 10 s once the station is within 2,500 km. That is at least
two minutes before it rises. Enable with `"iss": {"enabled": true}`.

## Planes Overhead feeds

Positions come from adsb.lol's keyless community ADS-B feed. When it fails
or answers 429, Sky Watch switches to adsb.fi's open data feed for 60 s,
doubling up to 300 s while adsb.lol keeps failing. adsb.fi serves the same
readsb `ac` document from the same lat/lon/distance path. It replaced
airplanes.live in the code on 2026-10-06. That feed had answered 403, with a
request to email them for access, since at least its first logged use on
2026-10-02. The running Hub started before the change and keeps falling
back to airplanes.live until it is restarted. Sky Watch polls every 60 s
while the sky is clear and every 20 s while a plane is overhead. adsb.lol
still answers 429 now and then at that cadence, which is what the fallback
is for. Only the circle centre (the weather senders' lat/lon) is sent.

## Notification Mirror

Absent unless an allowlisted app has a notification in the Windows
notification centre that is under `max_age_minutes` old (default 15). The
icon says what kind (envelope, chat bubble with typing dots, bell), the lane
names the app in one short word (`MAIL`, `TEAMS`, `CHAT`), and the meter
counts what is waiting, full at five. Dismiss the notification in Windows and
the page leaves the panel. A new one brings the page forward once.

It reads only each notification's app name, id and time, never the title,
text or sender; `test_notify_mirror.py` fails if the code reaches for them.
Browsers are left off the default list because a browser notification does not
say which site sent it.

Needs Windows notifications on (Settings > System > Notifications) and the
`winrt-Windows.UI.Notifications.Management` package. A setup that suits the
panel: per app, turn **Show notification banners** off and keep **Show
notifications in notification center** on, so the screen stays quiet and the
panel is the signal. Enable with `"notifications": {"enabled": true}`; replace
the allowlist with `"apps": [{"match": "Signal", "label": "TEXT", "icon":
"chat"}]`. Labels must fit the lane (about five letters).

## Nightly maintenance

Heap fragmentation is what limits this panel: the largest free block falls
from ~32 KB right after a boot to 10–18 KB within half an hour and ~5–12 KB
within a day or two (firmware 1.1.2, the current four scripts). Only a
reboot gives it back.
With `"maintenance": {"enabled": true}` the Hub restarts the panel once a
night inside `window_start`-`window_end` (default 21:35-23:00, inside
Nightkeeper's blackout while the PC is still awake), but only when:

- uptime is at least `min_uptime_hours` (default 20), and
- the matrix is off. A lit panel during the maintenance window means someone turned it on, so the
  night is skipped.

Afterwards it waits for the scripts to restore, then writes
`%LOCALAPPDATA%\AWTRIX\maintenance.json` with the largest block before and
after, per-script VM heap parsed from the boot log, any script errors, and how
many seconds the matrix stayed lit after the restart before Nightkeeper
switched it off. Outcomes are `ok`, `attention`, `skipped`, `no-return` or
`restarted-unverified`. It tries once per night whatever happens. The first
real run, 2026-10-06 21:37, was `ok`: uptime 54.1 h, largest block 8,692 →
32,756, all four scripts restored without errors, matrix dark.

**Low-memory restarts** (`"low_memory_reboot": true`, the default). The same
service also reads the panel once a minute at any hour, starting 3 minutes
after the Hub starts, and restarts it, lit or not, when its minimum free heap
since boot falls under `low_memory_min_free_bytes` (default 8192). That is the
sign that preceded the 2026-10-07 outage by 46 minutes, while the largest
block still looked healthy. It does not fire in the first 2 hours after a
boot, while the matrix is off Nightkeeper's schedule (lit in the blackout or
dark by day: a person is holding it, noted once per boot), or within 2
minutes of 21:30 and 07:00, and it allows at most 3 restarts per 24 hours,
counted from `maintenance.json`. `low_memory_block_bytes` (default 0, off)
adds a largest-block trigger: the block under that floor for 3 readings in a
row. Entries carry `"kind": "low-memory"` and the `trigger` that fired.

A reboot is successful only when its reset reason is `software`, the expected
scripts restore without errors, and the readbacks confirm the matrix is dark
(for a low-memory restart: where Nightkeeper's schedule puts it).
A panic or watchdog reset needs attention. Missing or malformed verification
data is reported as unverified rather than successful.

`%LOCALAPPDATA%\AWTRIX\panel_health.jsonl` records a health probe about every
30 seconds, rotating at 1 MB to `panel_health.1.jsonl`. Successful samples
include uptime, reset reason, heap, largest free block, and FPS. Since
2026-10-06 they also carry `app` (the page on screen) and `power` (false while
Nightkeeper has the matrix dark), so FPS and heap can be pinned on a page.
Since 2026-10-09 they also carry the panel's Wi-Fi: `rssi` (dBm), `wconn` and
`watt` (connect and attempt counters), `wstate`, and `werr` when the panel
reports a last Wi-Fi error. The SSID and endpoint are never written.
`panel_heap_report.py` uses them to say whether an outage was a Wi-Fi drop
(the counter rose within the same boot) or something else. New fields appear
once the Hub is restarted with that code. Failed
probes record `reachable: false` and the exception class, so the timeline
retains outages. Compare uptime only between successful samples; an uptime
rollback with `reason: panic` identifies a crash. Log writes are best-effort
and do not stop the Hub.

`%LOCALAPPDATA%\AWTRIX\panel_events.jsonl` is the panel write journal
(`panel_gate.py`, 2026-10-06). Every mutating request from the Hub and from
the other senders (Now Playing, PC stats, the weather and AQI briefs,
Meridian, and the console tools) takes one shared file lock, keeps a 0.4 s gap
after the previous write, and leaves one line: time, source, operation, app,
body bytes, HTTP status, duration, time spent waiting, error class, and gate
state. The file rotates at 1 MB to `panel_events.1.jsonl`. A write that
cannot get the lock within its budget, or finds the lock unusable, still goes
out, journaled as `timeout` or `unavailable`. If the directory itself cannot
be used, the write still goes out but leaves no line. Creating
`panel_gate.off` in `%LOCALAPPDATA%\AWTRIX` turns off the locking and the gap
in every gated sender at its next write, without a restart, and the journal
keeps recording (gate `off`). `nightkeeper_recover.ps1` and Miku-JARVIS write
ungated and leave no line. Each sender starts journaling only when it is next
restarted.

To read the logs together:

```powershell
C:/Users/devyn/anaconda3/python.exe panel_heap_report.py --since-hours 72
```

The report is read-only. It covers boots with the largest block at fixed
uptimes, the biggest block drops with the writes made in each interval,
per-sender attribution (correlation, not proof), FPS by page, write rates,
and outages. Add `--json` for one machine-readable document.

## Panel-down popup

When the Hub's health probe has failed for `after_minutes` (default 10), the
Hub starts a small always-on-top window, in its own process, saying how long
the panel has been silent and that unplugging it for 5 seconds is the fix (an
unresponsive panel cannot be restarted over HTTP). It appears once per outage
and at most once per `min_gap_minutes` (default 30); it waits while Game Mode
sees a game and hides if one starts while it is open; it does not fire just
because the PC woke from sleep. It closes itself at the first good probe,
after 12 hours, or when the Hub itself has gone quiet, and it writes nothing.
Turn it off with `"panel_down_alert": {"enabled": false}`. Windows toast
notifications are switched off on this PC, which is why this is a window.

## Configuration

The checked-in [utility_hub.example.json](utility_hub.example.json) is safe to
copy to:

```text
%LOCALAPPDATA%\AWTRIX\utility_hub.json
```

No configuration file is required for this panel: the built-in defaults use
`http://192.168.1.90`, discover the gateway automatically, and match
Nightkeeper's 9:30 PM–7:00 AM blackout. Forced app activation is suppressed
during that blackout; the Utility Hub never changes panel power or brightness.

Next Up is deliberately disabled until a calendar is chosen. To enable it,
copy the example file and add an HTTP(S) iCalendar URL or a full path to a local
`.ics` file under `next_up.sources`, then set `next_up.enabled` to `true`.
Calendar URLs can contain private tokens, so the real configuration and cache
remain under `%LOCALAPPDATA%\AWTRIX` and are never written to this repository or
logs.

## Verify without touching the panel

```powershell
C:/Users/devyn/anaconda3/python.exe utility_hub.py --once
```

This performs one bounded, read-only network probe and prints the gateway,
router, WAN, DNS, captive-login, and latency result. It does not push, activate,
delete, wake, or configure the panel.

## Run interactively

```powershell
C:/Users/devyn/anaconda3/python.exe utility_hub.py
```

Runtime logs rotate under `%LOCALAPPDATA%\AWTRIX\utility_hub.log`. The supplied
`awtrix-utility-hub.xml` is the eventual login task definition. Do not register
it until the panel passes the rotation preflight and Network Sentinel's static
canary/soak gate.

## Transport safety

- Network Sentinel uses an opaque 8×8 GIF plus a fixed 23×8 RGB888 lane; its
  current state payloads are about 1.3–1.4 KB.
- Next Up uses a bounded opaque 32×8 local GIF so the text can advance by one
  real LED column per frame. A complete pass is capped at 64 frames and the
  serialized request stays below 8 KB.
- Panel mutations are spaced by at least two seconds.
- Forced activations have one global ten-second cooldown.
- Deletion first verifies `GET /api/v1/apps` reports the exact name as a present
  pushed app. Built-ins, scripts, placeholders, and unknown origins are refused.
- Five consecutive mutation failures or an unplanned uptime rollback select
  static artwork. A `software` reset preserves animation; panic, watchdog and
  unknown reasons select the fallback. Ten consecutive successful mutations
  allow animation again.
- Desk Companion supplies both animated and static artwork, keeps a four-minute
  keepalive with a ten-minute expiry, and restores its cached page on the next
  tick after a detected reboot. The Hub ticks at least every 30 seconds even
  when the companion is its only configured service.
- Network alerts have a 45-second self-removal lifetime. Next Up refreshes every
  60 seconds with a 180-second lifetime. Stale state therefore disappears if
  the PC sleeps or the service dies.

References: [Microsoft NCSI troubleshooting](https://learn.microsoft.com/en-us/troubleshoot/windows-server/networking/troubleshoot-ncsi-guidance),
[Microsoft Get-NetRoute](https://learn.microsoft.com/en-us/powershell/module/nettcpip/get-netroute),
and [AWTRIX pushed-app lifecycle](https://blueforcer.github.io/awtrix-ng/guides/pushed-apps/).
