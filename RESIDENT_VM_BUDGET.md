# Resident shared VM acceptance

`script_swap.py` now enforces the repository's 75% shared VM ceiling for forward
installation and acceptance. This is separate from the existing 3.5x contiguous
block requirement. A minified source that fits the install block may still build
tables and caches that leave the resident set over its VM limit after reboot.

The limit is `scriptHeapBudgetBytes * 3 // 4`, using a positive integer reported
by the device. A 98,304-byte budget therefore has a 73,728-byte ceiling; equality
passes. The measured input is the final native `vm heap ... (shared N)` total,
which includes VM overhead. Per-script sums and source-size ratios are not used.

The evidence must reach back to the current boot, include exactly one complete
restore marker and valid native VM-load lines for every present resident. Missing
or malformed measurements, partial logs, a compile after the restore, or a budget
that changes across the procedure do not establish forward readiness. Unrelated
script messages containing `(shared N)` cannot replace the native measurement.

## Lifecycle behavior

1. Preflight records the current VM view and reports the acceptance ceiling in
   the dry-run plan. An over-ceiling starting set may enter a replacement trial;
   this allows a smaller compiled replacement to reclaim memory. An invalid
   reported budget blocks a fresh forward operation before its first write.
2. After deleting the target and rebooting, the remaining residents must have a
   complete measured VM total within the ceiling, in addition to the existing
   restore, identity, error and contiguous-block checks. Otherwise the new source
   is never uploaded and recovery restores the old source.
3. After uploading and rebooting, verification requires the full new resident set
   to be within the same reported budget and ceiling. Excess or unverified use
   triggers the existing rollback procedure before installation can be accepted.
4. `--check-only` and the already-installed path perform fresh, read-only memory
   checks. They return exit 7 when the budget is over limit or unverified, rather
   than returning success solely because the metadata version matches. Their
   evidence reads are bracketed by device reads to detect a reboot or budget change.
5. Recovery can restore the previous source even when the original resident set
   is already over budget or its memory log cannot be read. The recovery
   verification records its measured/unverified VM view, labels the policy
   `recovery`, and warns that this is **not adoption readiness**. This exception
   does not permit forward installation or accept a new resident set.

`--no-fidelity` controls the existing source/framebuffer evidence only. It does
not bypass the VM budget. Interrupted runs use the same guard when resumed;
versionless exact-identity checks and pinned rollback representations are retained.
No user override for this memory ceiling was added. A boot measurement is not
a measurement of peak runtime allocation or long-term heap fragmentation.

## Validation and deployment boundary

The fake-panel suite exercises measured totals at and around the ceiling,
non-integer/missing budgets, partial and malformed boot evidence, warm compile
measurements, recovery from an over-ceiling baseline, no-fidelity behavior,
changed reported budgets, interrupted verification, already-installed/check-only
behavior and exact-identity rollback of a versionless source.

The existing unreadable-log test now expects recovery through the inventory
fallback, with no new-source PUT and an explicitly unverified VM view. Inventory
can establish that the old source is restored; it cannot measure VM allocation.

Local validation: **3,216 tests passed in 84.63 seconds**, with zero failures,
errors or skips, including **40 new cases**. All 230 focused swap tests passed.
An identical fake-panel comparison of the exact previous `d9a90cd` tool accepted
an 80,000-byte new boot total; this revision rolled back the same source and boot
sequence to the previous version. The comparison made no live requests.

A read-only check on October 28 confirmed 81,652 / 98,304 bytes shared VM use on
the actual clock, which the new guard reports as over limit. The five-worker
host remains healthy on `d9a90cd798a2`, its private configuration is unchanged,
and the resident versions remain unchanged. Contiguous block at this point was
19,444 bytes, below the three staged candidates' upload floors. These are dated
observations, not permanent device characteristics.

This safeguard does not establish the new Ocean/Moon/Now Playing allocation cost,
FPS or physical appearance. Fresh snapshots, source pins, maintenance coordination,
measured post-deletion block, a controlled trial and post-boot/physical verification
remain necessary. It does not change the running host, private configuration or
resident scripts, and does not issue a live reboot.
