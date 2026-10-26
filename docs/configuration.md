# Canonical configuration and offline migration (Step 4A)

Step 4A provides a schema, immutable configuration loader, and offline commands. These commands do not start workers, register hotkeys, open network connections, inspect scheduled tasks, or contact the panel. [Step 4B worker adapters](workers.md) now accept this canonical configuration through Python APIs. Existing standalone senders retain their existing configuration selection; adoption is explicit.

## Commands

From the repository, use `python -m driftmatrix`. Installing the project also provides the `driftmatrix` console command and packages the schema so validation works outside the checkout:

```text
python -m pip install -r requirements.txt
python -m pip install --no-deps .
driftmatrix validate --config driftmatrix_config/driftmatrix.example.json
```

On the original Windows installation, replace `python` with `& C:/Users/devyn/anaconda3/python.exe` in PowerShell when the bare command opens the Microsoft Store.

The complete [Draft-07 schema](../driftmatrix_config/driftmatrix.schema.json) and a [disabled example](../driftmatrix_config/driftmatrix.example.json) are shipped together. The example uses a documentation address, not a discovered panel address.

Without `--config`, validation selects `%LOCALAPPDATA%/AWTRIX/driftmatrix.json`. Missing or invalid configuration fails explicitly; it is not created or replaced. The default data directory remains `%LOCALAPPDATA%/AWTRIX`, preserving the planned location of existing locks, journals, command queues, and maintenance history.

```text
driftmatrix validate --config C:/path/to/driftmatrix.local.json
```

Exit code 0 means validation succeeded. Exit code 2 means an invalid configuration, migration conflict, or file-publication error. Validation reports the schema version, snapshot hash, and service enable flags without printing private option values or the panel endpoint.

## Configuration model

The required top-level fields are `schema_version`, `panel`, `runtime`, and `services`:

| Field | Purpose |
| --- | --- |
| `schema_version` | Version 1; unsupported versions are rejected |
| `panel` | One HTTP(S) endpoint, IANA timezone, and `HH:MM` quiet-hours defaults |
| `runtime` | Data directory, shutdown timeout, bounded restart policy, and optional rotating-log settings |
| `services` | Explicit enable flags and service-specific options for `hub`, `media`, `pc_stats`, `weather`, `aqi`, and `drift_link` |

Hub options retain the existing scene sections, including calendars, focus, reminders, notifications, maintenance, and panel-down alerts. The former `panel_url` and `quiet_hours` become shared `panel` fields. The calendar timezone also moves to `panel.timezone`.

Media options cover bridge port, poll/staleness intervals, a script path, and optional `allow_script_install`. Set the latter false for [guarded host adoption](host-adoption.md) so a missing or different resident script cannot be replaced automatically. PC stats options cover sampling/request intervals, refresh delta, and confirmation count. Weather and AQI options cover coordinates, intervals, and static artwork selection; enabling either requires valid latitude and longitude. Drift-Link options retain matrix enablement, candidate device discovery, macro mappings, app rotation, and polling interval.

Unknown fields, string-valued enable flags, duplicate JSON keys, non-finite numbers, unsupported URL schemes, invalid hotkeys, and invalid field combinations are rejected. Windows UTF-8 BOM files are accepted. Panel URLs cannot contain credentials, queries, or fragments. Calendar URLs may retain private feed parameters, but they are not included in diagnostics.

Relative canonical paths resolve against the selected configuration's directory, never the process's current working directory. `runtime.data_dir: null` resolves to the existing AWTRIX data directory. Configuration objects expose frozen dataclasses, immutable nested options, a normalized snapshot hash, and a detached `to_mapping()` copy. The loader creates no directories.

The instance's optional `$schema` value is editor metadata. Validation always uses the trusted packaged schema and never downloads a schema from that value. There are no hidden environment overrides for the panel endpoint or service flags.

## Migration preview

Select legacy inputs explicitly. A migration without `--output` prints a redacted report and writes no files:

```text
driftmatrix migrate --from-hub utility_hub.example.json --from-ecosystem drift_ecosystem.json
```

For personal configuration, pass the actual selected paths instead of the repository examples. The migration does not search private directories or assume that the example matches a running installation.

The report includes:

- `status`: `ready` or `conflicts`.
- `candidate_written`: whether an explicit candidate output was successfully created.
- `conflicts`: field paths, source labels, and reasons, without competing private values.
- `warnings`: unverified startup state and omitted reserved metadata.
- `provenance`: an origin for every candidate leaf, including agreement between multiple sources and defaults.

Alias titles and custom macro keys are redacted in report field paths. Calendar URLs, tokens, coordinates, endpoint values, and source file contents are not printed. The candidate itself contains the imported private configuration and belongs outside source control.

Hub and ecosystem files do not prove that their host processes are enabled in Task Scheduler. Their worker flags remain false while their existing options and device flags are imported. No new maintenance or optional scene is enabled by inference. Review the candidate's worker enablement during the later service migration.

Unknown legacy fields block migration for review rather than disappearing. The old `fallback_port`, `udp_discovery_port`, and `haptic_alerts` fields are documented exceptions: they were reserved metadata, are reported as omitted, and do not acquire runtime behavior during migration.

### Sender inventory

Media, PC stats, weather, and AQI currently carry settings in module constants or launch arguments. An optional, manually prepared inventory records those selected values without importing or executing the sender:

```json
{
  "pc_stats": {
    "enabled": false,
    "panel_url": "http://192.0.2.10",
    "quiet_hours": {"start": "21:30", "end": "07:00"},
    "options": {"interval_seconds": 5, "refresh_delta_points": 3}
  },
  "weather": {
    "enabled": false,
    "options": {"latitude": 0, "longitude": 0, "refresh_seconds": 300}
  }
}
```

Use `--from-senders sender-inventory.local.json` to include it. Each of the four sender records may contain `enabled`, `panel_url`, `timezone`, `quiet_hours`, and its typed `options`. Explicit inventory enable flags are imported; missing flags remain false. Media script paths resolve against the inventory directory.

Differing shared endpoints, timezones, or quiet hours block candidate creation. Equivalent default-port/trailing-slash endpoint spellings are normalized. The tool does not select one conflicting value silently. An explicit legacy endpoint is required; migration never guesses a live device from the example address.

Relative legacy calendar paths are ambiguous because the old Hub resolved them using its working directory. Supply `--legacy-working-dir` to preserve the actual selected launch directory. Without it, relative calendar paths are reported as conflicts. No calendar is opened or downloaded.

## Create new files for review

After resolving conflicts, select a new candidate path and optionally a separate report path:

```text
driftmatrix migrate --from-hub C:/path/to/utility_hub.json --from-ecosystem C:/path/to/drift_ecosystem.json --output C:/path/to/driftmatrix.local.json --report C:/path/to/migration-report.local.json
driftmatrix validate --config C:/path/to/driftmatrix.local.json
```

Parent directories must already exist. Neither file may overwrite an existing file or alias an input. Publication uses a fully written temporary file and an exclusive same-directory hard link; another process cannot win a race and have its file overwritten. Filesystems without that operation fail explicitly. Temporary files are cleaned up on ordinary I/O errors.

Candidate and report are independently published files. If report publication fails after candidate creation, the error includes `candidate_written: true`. A conflict never produces a candidate, though an explicitly requested redacted conflict report may still be written.

All original inputs remain unchanged. The commands do not replace live configuration, change startup tasks, install resident scripts, reboot the panel, or switch senders to the candidate.

## Next phases

Step 4B provides worker adapters, cooperative shutdown, and the per-service ownership primitive shared with updated standalone entry points. [Step 4C](supervisor.md) now provides process supervision, supervisor ownership, bounded restarts, shutdown escalation, and `run`/`status`/`stop`. Offline `validate` and `migrate` retain their side-effect boundary. Physical adoption, acceptance, and rollback remain separate deployment steps.
