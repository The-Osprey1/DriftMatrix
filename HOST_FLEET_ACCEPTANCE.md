# First-image reliability and actual-worker acceptance

At startup, Companion could publish before the Utility Hub's periodic health
reporter identified the panel firmware. The first embedded image then used the
legacy bare-base64 format, even on AWTRIX NG 1.2.2. A later probe corrected later
pushes, so renderer-only checks and process `RUNNING` status missed the defect.

## Behavior change

`PanelClient.push_app` now obtains health before a cold client's first bare inline
GIF/JPEG push. The existing compatibility encoder then formats an independent
wire payload using the observed firmware. A prior successful health observation
avoids the additional lookup. Text-only payloads, filenames, malformed image
identifiers and already formatted data URLs retain their existing request path.

A failed or malformed health read defers the panel write through the existing
typed error/backoff path. It does not count as a failed mutation or latch static
mode. Local Focus commands still commit their state and receipt, and the timer
continues while delivery is deferred. Retrying after recovery publishes the
elapsed timer, rather than starting it again. The documented legacy encoding
policy for unknown/unrecognized firmware metadata is retained.

The fix changes transport startup behavior; it does not change the artwork,
resident source selection, mutation gate, script installation or reboot policy.

## Combined process acceptance

`test_real_host_fleet.py` starts the real supervisor and five actual production
workers in separate processes: Utility Hub, media, PC stats, Weather and AQI.
Drift-Link remains disabled, matching the staged host's enabled fleet.

The tests verify:

- Five distinct worker identities and module paths in the selected runtime,
  actual service/supervisor ownership locks, CLI `status`, and authenticated
  cooperative `stop`.
- Actual feed HTTP parsing and publishing, metric/Companion rendering, and the
  first image prefix and 2 KiB limit for every captured payload.
- The real media HTTP server, resident-source comparison in verification-only
  mode, playing state and both ordinary/reduced-motion responses.
- Real Focus command files, durable start/pause receipts, queue removal, and
  Companion's transition to the Focus mood through shared saved state.
- One successful gate journal entry for every recorded panel mutation; no
  resident-source writes, resident deletion or reboot requests.
- Zero worker restarts or shutdown escalations, all child processes gone,
  released ownership, closed media port, and unchanged configuration/source.

The fixture controls only the native OS providers and external feed transport.
WinRT metadata, CPU/GPU counters and microphone registry results are synthetic;
Open-Meteo requests are redirected to the owned loopback server. Other outgoing
HTTP is forbidden. Real adapters, renderers, parsers, local persistence, panel
HTTP, gate, polling loops, media server and shutdown remain production code.

Test workers are isolated support files and are not installed as production
dispatch. Their transport guard refuses non-loopback panel configurations.
The installed variant copies only those fixture files into a temporary directory
and runs child CLIs outside the checkout with the installed package first on
`PYTHONPATH`. Runtime module paths and source equality are checked explicitly.
Every CI matrix job now repeats this installed acceptance after the full suite.

## Results and limits

The full local suite passed **3,176 tests in 86.27 seconds**, including **13 new
cases**, with no failures, errors or skips. Eleven cases exercise cold-image
formatting, legacy compatibility, failed reads, unchanged non-image behavior and
offline Focus persistence. Two complete fleet scenarios cover ordinary and
reduced-motion media.

The installed wheel passed both fleet scenarios outside the checkout in
**23.82 seconds**. The observed stop-to-process-exit durations were approximately
**1.97 and 2.06 seconds**, with no restart or forced termination. These figures
describe the controlled local runs, not a guaranteed production deadline.

This acceptance does not exercise live WinRT sessions, GPU hardware, microphone
registry state, upstream TLS/network availability, Windows Task Scheduler launch,
Win32 hotkeys, Berry execution, ESP32 memory/FPS or physical LEDs. Existing
focused tests cover several of those boundaries, but actual device and physical
acceptance remain distinct. `RUNNING` alone still does not establish delivery.

No live clock requests, writes, reboots, configuration changes, service restarts
or task changes were performed. The user approved the revised visual direction
in `ART_MOTION_REVISION.md` on 2026-10-28. That approval permits the next adoption
checks; it does not establish snapshot fidelity, memory/FPS or physical acceptance.

## Reproduction

From the checkout:

```powershell
& 'C:/Users/devyn/anaconda3/python.exe' -X utf8 -m pytest -q -s -p no:cacheprovider test_real_host_fleet.py
```

After building/installing a wheel in an isolated target directory, select that
directory for the same test. The test fails if its production sources differ
from the reviewed checkout:

```powershell
$env:DRIFTMATRIX_ACCEPTANCE_RUNTIME = 'C:/path/to/isolated/install'
& 'C:/Users/devyn/anaconda3/python.exe' -X utf8 -m pytest -q -s -p no:cacheprovider test_real_host_fleet.py
Remove-Item Env:DRIFTMATRIX_ACCEPTANCE_RUNTIME
```
