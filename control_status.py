"""Plain-language desktop status from local supervisor evidence only."""
from __future__ import annotations

from dataclasses import dataclass

SERVICE_LABELS = {"hub": "Utility Hub", "media": "Now Playing", "pc_stats": "PC stats",
                  "weather": "Weather", "aqi": "Air quality", "drift_link": "Drift-Link"}


@dataclass(frozen=True)
class HostSummary:
    headline: str
    detail: str
    rows: tuple[tuple[str, str, str], ...]


def summarize(snapshot=None, *, configured=True):
    """A dead process never appears running just because a status file says so."""
    if not configured:
        return HostSummary("Legacy controls", "Choose driftmatrix.json to inspect the supervised services.", ())
    if not snapshot:
        return HostSummary("Host status unavailable", "Start DriftMatrix with this configuration, then refresh.", ())
    if not snapshot.get("live"):
        headline = "Host stopped"
    elif snapshot.get("degraded"):
        headline = "A service needs attention"
    elif not snapshot.get("healthy"):
        headline = "Host status is not fresh"
    elif not snapshot.get("config_matches"):
        headline = "Configuration changes pending"
    else:
        headline = "Host running"
    if not snapshot.get('live'):
        detail = 'Start DriftMatrix with the selected configuration, then refresh.'
    elif not snapshot.get("config_matches"):
        detail = "The running host uses a different configuration. Review changes before restarting it."
    else:
        detail = "Process status is read locally. Panel delivery is shown separately."
    rows = []
    for name, label in SERVICE_LABELS.items():
        service = snapshot.get("services", {}).get(name, {})
        state = service.get("state")
        if service.get("circuit_open"):
            shown, note = "Needs attention", "Restart limit reached; check the service log."
        elif state == "DISABLED":
            shown, note = "Disabled", "Off in the running configuration."
        elif not snapshot.get("live"):
            shown, note = "Stopped", "The supervisor is not running."
        elif state == "RUNNING" and service.get("live"):
            shown, note = "Running", "" if snapshot.get('healthy') else 'Host status needs attention.'
        elif state == "STARTING":
            shown, note = "Starting", "Waiting for the worker to report ready."
        elif state == "FAILED":
            delay = service.get("retry_in_seconds")
            shown, note = "Retrying" if delay is not None else "Failed", "Check the service log."
        elif state == "STOPPING":
            shown, note = "Stopping", "Waiting for a clean shutdown."
        else:
            shown, note = "Stopped", "No live worker is confirmed."
        restarts = service.get("restart_count", 0)
        if type(restarts) is int and restarts > 0:
            note = f"{restarts} automatic restart{'s' if restarts != 1 else ''}. " + note
        rows.append((label, shown, note.strip()))
    return HostSummary(headline, detail, tuple(rows))
