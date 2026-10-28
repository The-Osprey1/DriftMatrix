# Exact identity for a versionless resident script

The original Ocean reference has no version header. `script_swap.py` previously
required a versioned rollback source, so it could not safely adopt Ocean 3.0.
Changing the original file's header would erase its exact installed identity.

The new explicit mode preserves the original file and requires two complete
SHA-256 pins. It keeps the existing versioned path and its optional prefix pins.
The new source must still have a real version header.

| Input | Purpose |
| --- | --- |
| `--rollback-version unversioned` | Select the explicit versionless rollback contract. It is not a claimed firmware metadata version. |
| `--rollback-installed-sha256` | Full hash of the original installed representation, bound to the local source bytes or its UTF-8 LF representation. |
| `--rollback-sha256` | Full hash of the minified body that would be uploaded during rollback. Prefixes are refused in this mode. |

Both bodies remain known identities: the original representation and a restored
minified copy. HTTP readback is always hashed byte for byte. Only the local file
can use an explicitly pinned LF representation, allowing Git newline conversion
without pretending a different HTTP body matches. A version tag, empty tag or
BOM-hidden header cannot be admitted as a versionless source.

## Guarded writes and recovery

Preflight checks fresh source bytes as well as origin, presence and metadata.
For the versioned candidate, the full local upload hash must match its readback;
a matching version label with different bytes is insufficient in this mode.

Before every DELETE, PUT and reboot, the shared transport runs a precondition
after pacing and after acquiring the actual panel mutation gate, immediately
before HTTP dispatch. It re-reads presence and source identity as appropriate:

- DELETE requires a recognized exact source. A forward deletion specifically
  requires the pinned old source.
- PUT requires the target to remain absent and rechecks the available block.
  A source that appeared after preflight is never overwritten.
- A heap-clearing reboot requires absence. A verification reboot requires the
  particular old/new source being verified.
- With `--require-dark`, darkness is rechecked after source proof too.
- Boot-loop retries make fresh checks on every attempt. A cached origin or
  earlier hash cannot authorize another DELETE.

The mode refuses off, unavailable, timed-out or nested gate outcomes and cannot
use `--allow-ungated`. These checks serialize cooperating senders that use this
gate. The panel API does not provide a server-side conditional-write transaction;
independent external writers must still be excluded during adoption.

The snapshot must be complete, read-only and for the selected panel. For a
present target it must contain panel-provenance bytes matching the observed hash;
the tool verifies the saved file itself. Repository-derived reconstruction is
insufficient for this versionless identity proof. The minified upload and the
original backup remain separately identified.

Resume matching includes the old original/upload pins and new source hash.
Changing a pin cannot resume another contract. Local newline conversion alone
does not change the contract. An already-restored minified copy is recognized,
then verified at boot without another upload. Reports retain actual metadata
version `null` for a versionless rollback and record its observed hash separately.

Failure reports include structured `recovery_argv` and a platform-quoted command
with the full pins, endpoint, report, snapshot and maintenance paths, firmware,
timing options and rollback-only flag. A regression test executes this generated
recipe after a failed restore, including paths with spaces and apostrophes.

## Current Ocean reference

These values identify this repository reference. They are not a fresh observation
of the clock; a live run must prove the current body independently.

| Representation | Bytes | SHA-256 |
| --- | ---: | --- |
| Original UTF-8 LF | 10,434 | `5000462ec6ea5f02a831331bd71e77c81e244c50a271afc33bca6de9adc50b7f` |
| Minified rollback | 10,435 | `c7dbc2034d3b668aaf8cf935fe24cfb1851cde5a25c3f9f99d7a973f9bee3e38` |
| Ocean 3.0 candidate, minified | 10,904 | `b9463a74f84262e513b9eb42976f1fa027b578d25b67ce6cf59830ec51fe2117` |

For this original Ocean, minification only adds a trailing newline. Its file is
unchanged; no artificial version header is inserted. The candidate needs 38,164
contiguous bytes under the existing 3.5× rule; the rollback PUT floor is 31,305.
These are source-derived requirements, not measured post-boot availability.

Read-only preflight example, from the repository checkout:

```powershell
& 'C:/Users/devyn/anaconda3/python.exe' -X utf8 script_swap.py ocean ocean-v3.ax `
  --config "$env:LOCALAPPDATA/AWTRIX/driftmatrix.json" `
  --expect-version 3.0 --rollback ocean.ax --rollback-version unversioned `
  --rollback-installed-sha256 5000462ec6ea5f02a831331bd71e77c81e244c50a271afc33bca6de9adc50b7f `
  --rollback-sha256 c7dbc2034d3b668aaf8cf935fe24cfb1851cde5a25c3f9f99d7a973f9bee3e38 `
  --firmware 1.2.2 --dry-run
```

`--dry-run` performs no panel writes or snapshot. Without an explicit report/data
directory, it writes no local report either. Review the selected maintenance
policy and firmware; this example does not authorize a live replacement.

Canonical policy selection, runtime-scoped state and recovery checks are now
implemented in the follow-up described in `CANONICAL_SWAP_MAINTENANCE.md`.
The full-suite result below records the original identity change; the follow-up
adds independent maintenance-policy regression coverage. A policy change after a
partial run pauses dispatch until the selected configuration has been reviewed.

## Verification and remaining limits

**3,085 local tests passed in 65.45 seconds**, including 56 new cases, with no
failures, errors or skips. Existing versioned swap and transport tests pass.
The fake-panel pipeline uses real snapshot capture, mutation gating and journal
files, and exercises both synthetic sources and the actual Ocean reference and
candidate. It covers forward success, compile failure, rollback, interrupted
forward/rollback, restored-source recognition, changed pins, mismatched bodies,
unreadable sources, corrupted snapshots, races during pacing, boot-loop retries,
darkness, memory decay and generated recovery commands. Installed transport
preconditions were checked outside the checkout with mocked HTTP.

`PanelClient`'s optional dispatch precondition is internal transport support. With
no callback, its existing behavior is unchanged. A rejected callback sends no
HTTP request, is journaled as a rejected attempt and does not increment the
transport's mutation-failure/static-latch counter. The swap CLI remains a
repository tool; this does not add an installed `driftmatrix` subcommand.

Unreadable or unrecognized source bytes remain a hard stop, including in a boot
loop or with `--no-fidelity`. Very low contiguous memory can make source readback
fail. Hash pins cannot waive that missing observation; manual recovery may then
be necessary. The existing versioned path is preserved.

No live panel request, swap, reboot, source replacement or host/config/task change
was performed for this implementation. Tests and fake-panel recovery are not
ESP32 compile, heap, FPS, physical appearance or long-term stability acceptance.
Ocean 3.0 still requires visual approval, fresh snapshot/live identity, validated
maintenance policy and rollback, guarded adoption and physical verification.
