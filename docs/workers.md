# Worker adapters and cooperative shutdown (Step 4B)

The six host services expose the same blocking entry point from the repository:

```python
def run_worker(config: DriftMatrixConfig, stop_event: threading.Event) -> int:
    ...
```

`DriftMatrixConfig` is the public alias for the immutable `CanonicalConfig` returned by `config_loader.load_config`. Each adapter reads its service's enable flag and options, uses the shared panel endpoint and data directory, and returns when stopped. A disabled service or an already-set stop event returns 0 before acquiring ownership or creating network clients.

| Service key | Module | Shutdown behavior |
| --- | --- | --- |
| `hub` | `utility_hub` | Checks stop between controller ticks, wakes the scheduler, closes the panel session |
| `media` | `now_playing_push` | Wakes async retry/poll waits, clears local bridge state, shuts down and closes the HTTP server, joins its thread |
| `pc_stats` | `pc_stats_push` | Wakes sampling and retry waits; keeps metric gates, refresh confirmation, and recovery state local |
| `weather` | `weather.weather_brief_push` | Wakes refresh/retry waits and closes its session in `finally` |
| `aqi` | `weather.aqi_brief_push` | Wakes refresh/retry waits and closes its session in `finally` |
| `drift_link` | `drift_ecosystem` | Creates the hotkey owner's message queue before posting `WM_QUIT`, unregisters hotkeys, joins listener and event-bus work, closes its session |

Workers retain the existing guarded transport, backoff, static fallback, and maintenance verification. Canonical PC stats timing must leave retry headroom within the existing 45-second keepalive budget, which already preserves a margin below the 60-second page lifetime: `2 * (interval_seconds + request_timeout_seconds) < 45`. The default remains 5 seconds for each value.

## Ownership

Every adapter and the updated standalone entry points acquire `service_ownership` through `run_owned`. Ownership uses a nonblocking operating-system file lock:

```text
<data_dir>/service_locks/<normalized-panel-url-sha256-prefix>/service_<name>.lock
```

The scope is the service name, normalized panel endpoint, and selected data directory. Equivalent endpoint spellings share a lock. Different services may run together. Two instances of the same service in the same scope cannot both start; the second returns 3 before entering its service loop. Lock failures fail closed. The kernel releases ownership when a process exits, including an unexpected exit. The persistent lock file is intentionally retained: its presence alone does not mean a service is running.

For a later supervisor migration, keep `runtime.data_dir` at the existing AWTRIX directory and use the same panel endpoint as the standalone sender. A different data directory creates a separate ownership scope. Existing deployed versions must be updated to participate in this locking protocol; these locks cannot exclude an older process that never acquires them.

## Configuration and paths

Adapters translate canonical options into the services' existing typed configuration or explicit arguments. They do not overwrite module constants or process environment variables. The shared timezone and quiet hours reach the Hub, PC stats, and AQI relevance/activation decisions. Weather remains governed by its existing forecast relevance rules.

Data paths are scoped with a context variable for status, local commands, logging, and panel-gate journals. Drift-Link propagates that context into its listener and event-bus callback threads. The scope is restored on return or failure. Existing explicit panel-gate overrides continue to take precedence.

The process boundary remains one worker per process. Adapters run under the [Phase 4C supervisor](supervisor.md) or a controlled embedding; they do not create or supervise other service processes. The installed package now includes the worker runtime and resources, and the CLI provides `run`, `status`, and `stop` alongside offline `validate` and `migrate`.

## Stop handling and results

While running on the main Python thread, `run_owned` temporarily maps SIGINT, SIGTERM, and Windows SIGBREAK/Ctrl+Break to the supplied stop event and restores the previous handlers afterward. An embedding that calls an adapter from a secondary thread must set the supplied event itself. Standalone Drift-Link keeps its Windows message loop on a listener thread so the main thread can handle console signals.

Idle and retry waits wake when the event is set. An in-flight network request, controller tick, or operating-system media call still has to return before cleanup completes; cooperative shutdown does not forcibly terminate those calls. Phase 4C owns the process-level shutdown deadline and escalation policy. Forced process termination does not run Python cleanup, though it releases the kernel ownership lock.

| Result | Meaning |
| --- | --- |
| `0` | Normal stop, disabled worker, or stop requested before startup |
| `2` | Required platform support is unavailable, such as WinRT media or Windows global hotkeys |
| `3` | Ownership could not be acquired |
| Exception | Startup or runtime failure for the caller/supervisor to record |

Existing standalone script names, arguments, and configuration selection remain available. Running those scripts uses the new ownership and stop handling, but does not automatically adopt `driftmatrix.json` or replace scheduled tasks. The supervisor's lifecycle, restart policy, and shutdown deadline are described in [Phase 4C](supervisor.md). Deployment and physical-panel acceptance remain separate steps.
