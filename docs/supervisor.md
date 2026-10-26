# Process supervisor and lifecycle commands (Phase 4C)

`driftmatrix run` starts the enabled host services as separate child processes in a foreground supervisor. Each child uses the existing [worker adapter](workers.md), its guarded transport, and its own service lock. Utility Hub, the WinRT media loop, and the Win32 hotkey loop keep their process boundaries.

## Commands

Select and validate a canonical configuration before running its enabled services:

```text
driftmatrix validate --config C:/path/to/driftmatrix.local.json
driftmatrix run --config C:/path/to/driftmatrix.local.json
```

From another console:

```text
driftmatrix status --config C:/path/to/driftmatrix.local.json
driftmatrix stop --config C:/path/to/driftmatrix.local.json
```

From the repository, `python -m driftmatrix` provides the same commands. When bare Python opens the Microsoft Store on the original Windows host, use `& C:/Users/devyn/anaconda3/python.exe -m driftmatrix` in PowerShell. A normal package installation now includes the worker modules, their supporting modules, and the default media script, so the installed command also works outside the checkout.

`run` remains attached to the console. It does not install a Windows service, create startup tasks, replace Task Scheduler jobs, migrate private configuration, or deploy the repository. Enabling a worker is an explicit decision in the canonical file: starting an enabled worker can perform its usual panel operations. The supplied example disables every worker and uses a documentation endpoint.

`status` reads local records and checks process IDs together with process creation times. It opens no control connection and makes no panel or feed requests. Its JSON includes supervisor liveness, heartbeat age, reported uptime, configuration hash match, and each child's state, PID, exit code, restart count, retry delay, circuit status, and escalation flag. `healthy` requires a live supervisor, a heartbeat no older than five seconds, and no failed service. `degraded` identifies a failed service even while the supervisor is available.

Before a run, status reports `NOT_RUNNING` without creating files. A nonterminal record whose process identity no longer exists reports `STALE`. A completed run retains its terminal status. Uptime and update time are the last published values; the heartbeat age shows their freshness. Disabled services have no child process.

`stop` sends an authenticated, loopback-only IPC request to the running supervisor and returns `STOP_REQUESTED` after acknowledgment. This acknowledges the request, not completed shutdown; use `status` to observe completion. A missing, invalid, or stale control identity returns an error. The command never signals a PID from an unverified status file. This IPC path works on Windows without using SIGTERM, which can forcibly terminate a Windows process instead of running Python cleanup.

## Ownership and files

All supervisor records are scoped to the normalized panel endpoint and data directory:

```text
<data_dir>/service_locks/<endpoint_hash>/
    supervisor.lock
    supervisor_status.json
    supervisor_control.json
    supervisor.log
    logs/service_<name>.log
    service_<name>.lock
```

`supervisor_ownership(panel_url, data_dir)` acquires `supervisor.lock`. Each child acquires only its own `service_<name>.lock`. The parent does not hold a child's service lock, so it does not block the worker it starts. Duplicate supervisors return 3 before replacing the running instance's records.

The same endpoint/data-directory scope must be selected for `run`, `status`, and `stop`. Different endpoints or data directories permit independent supervisors. Use separate data directories for separate physical panels because legacy companion state files still have shared names within a data directory. Updated standalone senders participate in the same per-service locking protocol; older deployed copies need updating before they can participate.

The status record contains no control secret or private endpoint/options. `supervisor_control.json` contains a random instance token and loopback port; it belongs with private local runtime state. Stop requires agreement between status/control instance identities and the supervisor PID's creation time, followed by an authenticated acknowledgment from that instance. A new run publishes new credentials. Control credentials are removed during normal cleanup; stale credentials after a crash cannot bypass the identity checks.

Records use complete temporary files and atomic replacement. Short Windows sharing conflicts receive bounded retries; persistent publication failure stops the owned fleet rather than silently leaving it unreported. Shutdown still stops children if status publication fails. Files are created with restrictive modes, using the existing local directory's access rules on Windows. Lock files remain in place after exit; their existence does not prove ownership.

Supervisor logs and child root-logger files use `runtime.logging.level`, `max_bytes`, and `backup_count`. Existing sender-specific handlers and console output remain available. Status errors record exception class names rather than private exception messages.

## Child startup and configuration

Only enabled services are spawned. The dispatch table is fixed to the six known adapters; there is no CLI option to load an arbitrary worker module. Child arguments and stop events travel over multiprocessing IPC instead of exposing private configuration on a command line.

The parent loads and validates configuration once. Each child receives a detached snapshot with resolved paths and reconstructs the immutable configuration from that snapshot. Restarts reuse the same snapshot even if the source file changes. `status` reports `config_matches: false` when the selected file differs from the running snapshot. Stop the fleet and start a new run to adopt an edited configuration; there is no automatic reload.

Children report `RUNNING` only after acquiring their service lock. A service's states are:

| State | Meaning |
| --- | --- |
| `DISABLED` | No process is started |
| `STARTING` | Child is starting or importing its adapter; ownership acknowledgment is pending |
| `RUNNING` | Worker holds its service lock and has entered its adapter |
| `FAILED` | Worker exited or failed startup; a retry may be scheduled or the circuit is open |
| `STOPPING` | Cooperative stop was requested |
| `STOPPED` | Worker completed cooperative shutdown with exit code 0 |

`RUNNING` is a lifecycle acknowledgment, not proof of panel reachability or physical display output. Source and network failures continue to use each sender's existing backoff and do not restart an otherwise running worker process.

## Bounded restarts

The canonical example sets:

```json
{
  "shutdown_timeout_seconds": 30,
  "restart": {"max_attempts": 5, "window_seconds": 600}
}
```

`max_attempts` counts automatic restarts after the initial launch. A value of 5 allows the initial launch plus at most five restarts in the rolling 600-second window. A value of 0 disables automatic restarts. Old restart timestamps leave the budget when the window expires, provided the circuit has not already tripped.

Retry delays are 1, 2, 4, 8, 16 seconds, capped at 60 seconds for larger budgets. Startup exceptions and unexpected exits, including an unexpected exit code 0, consume the retry budget. Exit code 2 (unsupported worker/platform) and 3 (ownership unavailable) open the circuit immediately. A tripped circuit remains open until the operator stops the supervisor and starts a new run; it does not resume a persistent crash loop by itself. Other services continue running.

## Shutdown deadline and escalation

Ctrl+C, SIGTERM, Windows Ctrl+Break, and the authenticated stop request set the supervisor's stop event. The supervisor stops scheduling launches/restarts, marks live children `STOPPING`, and sets every child's IPC stop event before waiting. Python signal handlers remain installed through cleanup and are restored afterward.

All children share one `shutdown_timeout_seconds` deadline, rather than receiving a full timeout sequentially. Cooperative exit closes sessions, unregisters hotkeys, and closes the media bridge as specified by each adapter. After the deadline the supervisor calls `terminate()` on remaining owned children, gives the group one second, then calls `kill()` on survivors and allows a final two-second group reap interval. Status records forced exits as `FAILED` with `escalated: true`. Kernel locks are released when the child exits.

A child also monitors its parent IPC connection. If the parent dies, the child requests its own cooperative stop and exits forcefully if its configured shutdown deadline expires. This prevents a supervisor crash from leaving a sender indefinitely unsupervised.

| Command/result | Exit code |
| --- | --- |
| `run`: normal cooperative stop, no failed service | `0` |
| `run`: failed service or forced worker exit recorded at shutdown | `1` |
| Configuration, control, or local publication error | `2` |
| `run`: supervisor ownership unavailable | `3` |
| `status`: valid local report, including `NOT_RUNNING` or `STALE` | `0` |
| `stop`: request acknowledged | `0` |

Phase 4C tests use temporary data roots and simulated workers. Physical rollout, startup-task adoption, panel snapshots, rollback, and visual acceptance remain Phase 5 work.
