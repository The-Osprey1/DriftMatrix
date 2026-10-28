# Canonical maintenance policy for script swaps

`script_swap.py` now selects the same canonical configuration and Hub adapter
mapping as the supervised worker. Previously it selected `utility_hub.json` even
when the running Hub used `driftmatrix.json`, and a loading error silently selected
defaults. Matching values in two files did not establish which policy was applied.

## Selection and provenance

Selection uses this order:

1. Explicit `--config PATH` selects canonical `driftmatrix.json`.
2. Explicit `--hub-config PATH` selects legacy `utility_hub.json`.
3. The default canonical file is selected when present.
4. Otherwise the default legacy file is selected when present.
5. Only when neither exists are the documented maintenance defaults used.

The two explicit flags are mutually exclusive. A missing, malformed or invalid
selected file is refused; it never falls through to another file or defaults.
Passing a canonical document through `--hub-config` is refused. The canonical
loader retains strict BOM, duplicate-key and non-finite-number handling. Errors
do not echo configuration values.

Reports record the selected kind, absolute and resolved source paths, SHA-256 of
the exact parsed bytes, normalized panel URL, effective maintenance enablement,
timezone, runtime directory, history path and local supervisor observation.
An explicit `--panel` must agree with the selected configuration. The default
panel comes from that configuration.

Canonical maintenance is enabled only when both the Hub service and its
maintenance option are enabled. An existing active maintenance entry still blocks
a write even if the current configuration disables future maintenance. Canonical
wall-clock decisions use `panel.timezone`, as the supervised Hub does; offset
timestamps are converted to that zone. Active-entry age uses elapsed time across
DST changes. Older naive timestamps retain their host-local interpretation;
ambiguous fall-back timestamps use the youngest possible age.

## Runtime paths and dispatch checks

Canonical `runtime.data_dir` supplies the shared mutation gate and journal,
maintenance history, default report/snapshot/log locations and whole-run
`script_swap.lock`. `--data-dir` changes evidence output locations; it cannot move
the shared gate, history or procedure lock away from the selected runtime.
`--maintenance-report` remains an explicit read-only history override. Legacy
selection retains the existing host-local state directory and timezone behavior.

Before the first panel read, and again before guarded HTTP mutation dispatch,
the selected file must retain its byte hash and resolved source. Appearance of a
higher-priority default file also requires reselection. A live supervisor found
in the selected endpoint/runtime scope must report the same configuration hash
and be in `RUNNING`, rather than a startup/shutdown transition. Invalid local
status is refused.

Dispatch checks run after transport pacing and inside the mutation gate. They
reread maintenance history and recheck imminent maintenance, requested darkness
and the PUT block floor. Invalid selected history or an active entry with an
unreadable timestamp is refused; an absent history file remains an empty history.
Before any write, changed policy aborts. After a partial run, it pauses with the
current state recorded rather than using stale policy to begin rollback. Re-run
after reviewing the changed configuration and reconciling the running supervisor.
Canonical resume also binds the report to the endpoint and runtime directory.

Recovery recipes retain the canonical `--config` selection, full source pins and
evidence locations. A derived history path is reselected from the reviewed
runtime; an explicit history override is retained. The original source-identity,
fresh snapshot, firmware, memory and rollback checks remain required.

## Read-only review

From the checkout, for the separate Ocean candidate:

```powershell
& 'C:/Users/devyn/anaconda3/python.exe' -X utf8 script_swap.py ocean ocean-v3.ax `
  --config "$env:LOCALAPPDATA/AWTRIX/driftmatrix.json" `
  --expect-version 3.0 --rollback ocean.ax --rollback-version unversioned `
  --rollback-installed-sha256 5000462ec6ea5f02a831331bd71e77c81e244c50a271afc33bca6de9adc50b7f `
  --rollback-sha256 c7dbc2034d3b668aaf8cf935fe24cfb1851cde5a25c3f9f99d7a973f9bee3e38 `
  --firmware 1.2.2 --dry-run
```

This makes panel GET requests but sends no panel writes or snapshot request.
Without `--report` or `--data-dir`, dry run writes no local report or log and
acquires no procedure lock. This example is a review command, not adoption
approval. Live work still requires visual approval and fresh device evidence.

## Scope and verification limits

The local supervisor observation covers the selected endpoint/runtime directory.
`NOT_RUNNING` does not establish that there are no legacy scheduled tasks,
standalone workers or supervisors using another data directory. Before adoption,
verify actual task definitions, process count, applied configuration and shared
gate participation. A matching configuration hash is not hardware acceptance or
proof of a healthy child fleet.

The gate coordinates participating writers per request; it is not a whole-run
maintenance lease or a server-side conditional transaction. External writers
must be excluded operationally. Edits after the last check remain possible.
Unreadable source bytes, memory pressure and hardware failures retain their
existing recovery limits; hash pins and policy selection cannot waive them.

Regression coverage includes canonical/legacy/default selection, strict parsing,
same-snapshot fingerprinting, worker mapping, scoped state, timezone/DST behavior,
live supervisor mismatch/transitions, configuration changes during pacing and
after DELETE, resumed recovery, corrupted history, resolved-path changes,
endpoint/runtime report binding and generated recovery arguments. Tests use
isolated configuration paths and simulated panels. Installed-package checks block
network access. No live panel mutation or host configuration/service change is
part of this implementation.
