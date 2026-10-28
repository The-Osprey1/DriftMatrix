"""Read-only selection of the Hub's declared maintenance policy and state paths."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

import config_loader
import panel_maintenance as pm
import sender_logging


class MaintenancePolicyError(ValueError):
    """A selected policy cannot be established; no document values are echoed."""


@dataclass(frozen=True)
class HubMaintenance:
    enabled: bool = True
    window_start_minute: int = 21 * 60 + 35
    window_end_minute: int = 23 * 60
    min_uptime_hours: float = 20.0
    low_memory_reboot: bool = True
    low_memory_min_free_bytes: int = 8192
    low_memory_min_free_min_uptime_minutes: float = pm.MaintenanceConfig.low_memory_min_free_min_uptime_minutes
    low_memory_max_per_day: int = pm.MaintenanceConfig.low_memory_max_per_day
    source: str = 'defaults'
    timezone_name: str | None = None


def _exists(path: Path) -> bool:
    try:
        path.lstat()  # A broken selected symlink must fail validation, not fall back.
    except FileNotFoundError:
        return False
    except OSError:
        raise MaintenancePolicyError('Cannot inspect maintenance configuration location') from None
    return True


@dataclass(frozen=True)
class MaintenanceContext:
    policy: HubMaintenance
    kind: str
    path: Path | None
    data_dir: Path
    panel_url: str | None
    fingerprint: str | None = None
    watch_absent: tuple[Path, ...] = ()
    explicit: bool = False
    canonical: config_loader.CanonicalConfig | None = field(default=None, repr=False, compare=False)
    resolved_source: Path | None = None

    def summary(self):
        return dict(kind=self.kind, config=None if self.path is None else str(self.path),
                    resolved_config=None if self.resolved_source is None else str(self.resolved_source),
                    sha256=self.fingerprint, data_dir=str(self.data_dir), panel_url=self.panel_url,
                    timezone=self.policy.timezone_name or 'host-local', enabled=self.policy.enabled,
                    runtime_config_hash=None if self.canonical is None else self.canonical.config_hash,
                    explicit=self.explicit)

    def assert_unchanged(self):
        if self.path is not None:
            try:
                if self.resolved_source is not None and self.path.resolve() != self.resolved_source:
                    raise MaintenancePolicyError('Selected maintenance configuration resolves to a different source')
                current = sha256(self.path.read_bytes()).hexdigest()
            except (OSError, RuntimeError):
                raise MaintenancePolicyError('Selected maintenance configuration is no longer readable') from None
            if current != self.fingerprint:
                raise MaintenancePolicyError('Selected maintenance configuration changed; re-run to review its new policy')
        if any(_exists(path) for path in self.watch_absent):
            raise MaintenancePolicyError('A higher-priority maintenance configuration appeared; re-run to select it')
        if self.canonical is not None:
            import supervisor
            try:
                observed = supervisor.status(self.canonical)
            except (config_loader.ConfigError, OSError):
                raise MaintenancePolicyError('Cannot establish the local supervisor configuration state') from None
            if observed.get('live') and observed.get('config_matches') is not True:
                raise MaintenancePolicyError('Running supervisor uses different configuration; reconcile it before swapping scripts')
            if observed.get('live') and observed.get('state') != 'RUNNING':
                raise MaintenancePolicyError('Running supervisor is in a lifecycle transition; wait for a settled state')
            return {key: observed.get(key) for key in ('state', 'live', 'config_matches')}
        return None


def resolve_maintenance_context(*, config: Path | None = None, hub_config: Path | None = None,
                                panel_url: str | None = None) -> MaintenanceContext:
    """Explicit canonical / explicit legacy / default canonical / legacy / defaults."""
    import utility_hub as hub
    if config is not None and hub_config is not None:
        raise MaintenancePolicyError('Choose --config or --hub-config, not both')
    canonical, legacy = config_loader.default_config_path(), hub.default_config_path()
    explicit, absent = config is not None or hub_config is not None, ()
    if config is not None:
        kind, path = 'canonical', Path(config)
    elif hub_config is not None:
        kind, path = 'legacy', Path(hub_config)
    elif _exists(canonical):
        kind, path = 'canonical', canonical
    elif _exists(legacy):
        kind, path, absent = 'legacy', legacy, (canonical,)
    else:
        return MaintenanceContext(HubMaintenance(), 'defaults', None, sender_logging.log_folder().resolve(),
                                  None, watch_absent=(canonical, legacy))
    path = path.expanduser().absolute()  # Keep a selected symlink observable if it is retargeted.
    try:
        raw = path.read_bytes()
        document = config_loader.decode_json(raw)
        if kind == 'canonical':
            selected = config_loader.config_from_mapping(document, source_path=path)
            settings = hub.hub_config_from_canonical(selected)
            service_enabled = selected.services['hub'].enabled
            data_dir, endpoint, zone = selected.runtime.data_dir, selected.panel.url, selected.panel.timezone
        else:
            if not isinstance(document, dict):
                raise MaintenancePolicyError('Selected legacy maintenance configuration must be an object')
            if {'schema_version', 'services', 'panel'} & document.keys():
                raise MaintenancePolicyError('Canonical configuration requires --config, not --hub-config')
            settings = hub.config_from_mapping(document)
            service_enabled = True
            data_dir, endpoint, zone = sender_logging.log_folder().resolve(), config_loader.normalize_panel_url(settings.panel_url), None
        runtime = hub.build_maintenance_config(settings.maintenance)
        policy = HubMaintenance(enabled=service_enabled and runtime.enabled,
            window_start_minute=runtime.window_start_minute, window_end_minute=runtime.window_end_minute,
            min_uptime_hours=runtime.min_uptime_hours, low_memory_reboot=runtime.low_memory_enabled,
            low_memory_min_free_bytes=runtime.low_memory_min_free_bytes,
            low_memory_min_free_min_uptime_minutes=runtime.low_memory_min_free_min_uptime_minutes,
            low_memory_max_per_day=runtime.low_memory_max_per_day,
            source=str(path), timezone_name=zone)
        if panel_url is not None and config_loader.normalize_panel_url(panel_url) != endpoint:
            raise MaintenancePolicyError('--panel disagrees with the selected maintenance configuration')
        return MaintenanceContext(policy, kind, path, data_dir, endpoint, sha256(raw).hexdigest(), absent, explicit,
                                  selected if kind == 'canonical' else None,
                                  selected.source_path if kind == 'canonical' else path.resolve())
    except MaintenancePolicyError:
        raise
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise MaintenancePolicyError(f'Cannot validate selected {kind} maintenance configuration') from None


def maintenance_local_time(value, timezone_name=None):
    """Compare aware history in the selected zone; old naive stamps are host-local."""
    try:
        if isinstance(value, datetime):
            moment = value
        else:
            text = str(value)
            moment = datetime.fromisoformat(text[:-1] + '+00:00' if text.endswith('Z') else text)
        if timezone_name is not None:
            return moment.astimezone(ZoneInfo(timezone_name)).replace(tzinfo=None)
        return moment if moment.tzinfo is None else moment.astimezone().replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def maintenance_elapsed_seconds(later: datetime, earlier):
    """Elapsed age uses offsets, not subtraction of DST-shifted wall clocks.

    Old naive timestamps are host-local. If a fall-back time is ambiguous,
    use its youngest possible age so an active entry is not declared stale early.
    """
    try:
        text = str(earlier)
        at = earlier if isinstance(earlier, datetime) else datetime.fromisoformat(
            text[:-1] + '+00:00' if text.endswith('Z') else text)
        possible = [at.timestamp()] if at.tzinfo is not None else [at.replace(fold=n).timestamp() for n in (0, 1)]
        return min(later.timestamp() - stamp for stamp in possible)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
