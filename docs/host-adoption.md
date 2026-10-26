# Guarded host adoption and physical acceptance (Phase 5)

Adopt the supervisor in stages, preserve the old launch definitions and source, and keep resident-script deployment separate from host ownership changes. Software tests do not establish physical LED output or long-term panel stability.

## Preserve recovery evidence

Before a live transition, capture a fresh panel snapshot, including exact resident sources, settings, inventory, and logs:

```text
python panel_snapshot.py --panel http://192.168.1.90 --purpose before-host-adoption
```

`--awtrix` remains supported. Verify `manifest.json` reports `complete: true` and record whether each source came from the panel or repository reconstruction. Archive every affected scheduled-task XML, the actual legacy source tree including unfinished changes, private configuration, and process identities outside Git.

Read heap metrics and the last boot log. Record `freeHeapBytes`, `minFreeHeapBytes`, `largestFreeBlockBytes`, script VM allocations, and script errors. Use `panel_heap_report.py` to interpret health history. A recent boot log describes that boot; a host-only transition does not require an extra panel reboot.

Compare normalized payload previews for representative weather, AQI, companion, and PC states. New artwork needs a concrete preview and review before it is sent. Keep physical brightness, diffusion, animation, and legibility acceptance separate from source/API tests.

## Generate and review the candidate

Inspect the real launch arguments and constants for every active sender. Prepare an inventory with explicit endpoint, timezone, quiet hours, sampling/refresh/request intervals, media port/staleness, and feed coordinates. Start with all worker enable flags false.

On Windows, Weather and AQI use the native trust store through `truststore` while retaining certificate and hostname verification. Verify a read-only source fetch before declaring a feed stage accepted; a `RUNNING` process alone does not prove it can fetch or publish fresh data. Require a successful guarded push or intentional hide/delete after that stage starts.

```text
driftmatrix migrate --from-hub C:/path/to/utility_hub.json --from-ecosystem drift_ecosystem.json --from-senders C:/path/to/sender-inventory.json --output C:/path/to/driftmatrix.json --report C:/path/to/migration_report.json
driftmatrix validate --config C:/path/to/driftmatrix.json
```

Migration never overwrites existing outputs. Archive a prior generated report before producing a replacement. Resolve conflicts explicitly rather than dropping unknown runtime settings. The legacy Hub's `_readme` string or list of text lines is documentation: migration reports its omission without printing its contents. Other settings hidden under that key are rejected.

Review provenance and warnings, reconstruct typed Hub configuration, and compare it with the old loader's effective settings. Preserve empty calendar sources and feature enablement. Leave Drift-Link disabled if it was not running. Keep `runtime.data_dir` at the existing AWTRIX directory to preserve focus, reminders, maintenance history, and journals.

For host-only media adoption, explicitly set `services.media.options.allow_script_install` to false. The default remains true for compatibility with existing standalone setup. In verification-only mode the bridge compares the rendered/minified source against the installed script. Identical source proceeds without a script write. Missing or different source, including a changed LAN URL, causes a reported retry without replacing the resident script.

Before stopping the old media service, verify its actual LAN route, port, state path, and configured template produce exactly the snapshot's bytes. Preserve its port so the firmware script does not need changing.

## Stage service ownership

1. Start with PC stats. Back up and stop its exact legacy task/process, temporarily disabling that launch to prevent overlap. Confirm exit before enabling the replacement. Older deployed copies do not acquire the new locks.
2. Enable only PC stats in the candidate. Run the supervisor, check status, process identity and guarded journal, then request stop. Verify cooperative exit, no escalation, and release of supervisor/service ownership.
3. Add media after its old bridge releases the port and resident bytes still match. Verify bridge liveness, valid state, clean stop, and absence of install/reboot mutations.
4. Add Weather/AQI, then the Hub. Verify one process per service, the expected data directory, fresh Hub status and health, panel uptime/memory, and no new script errors. Leave previously inactive workers disabled.
5. On failure, stop the new supervisor, verify child exit, restore the last known configuration, and re-enable/restart only the replaced legacy tasks. Preserve failure logs before retrying.

The supervisor snapshots configuration at startup. Stop it before editing enable flags, validate, then start a new instance. `STOP_REQUESTED` acknowledges a request; observe terminal status and child exits before starting replacements.

## Transition scheduled tasks

After staged lifecycle checks pass, create one supervisor task under the existing interactive user. WinRT media and global hotkeys require that interactive session. Preserve suitable logon triggers, restart settings, and unlimited duration. Point the action at a stable release directory, not a temporary review checkout or mutable branch.

Keep original tasks archived and disabled for rollback. Start the supervisor task and verify its actual process tree, configuration hash, enabled worker states, fresh status, and absence of legacy duplicates. Repeat verification after the real scheduled-task launch rather than relying only on a manual test.

Hidden `pythonw.exe` workers rescue absent stdout/stderr into their scoped rotating service log. Console workers retain their real streams. Verify this path after the actual hidden task launch so print-based PC/feed diagnostics are not lost.

## Resident-script and physical gates

Every resident upload uses minified source. A replacement requires a fresh snapshot and a concrete rollback source/version:

```text
python script_swap.py APP NEW.ax --expect-version NEW_VERSION --rollback OLD.ax --rollback-version OLD_VERSION
```

Use pinned hashes and the tool's memory, origin, maintenance, compilation, restoration, and post-boot checks. Do not replace a resident script merely because its host sender moved. Track the contiguous block as well as total free heap.

Finally confirm normal physical rotation, legibility, brightness, animation, and truthful media/telemetry with the user. A short successful observation is operational evidence, not long-term stability. Preserve snapshot paths, release hash, original task XML, stage results, final process identities, memory measurements, and any acceptance still pending.
