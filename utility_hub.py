r"""PC-side coordinator for transient, useful AWTRIX apps.

Network Sentinel is the first service hosted here.  Next Up and Focus Sprint
join the same process in later roadmap phases so the panel sees one shared
write-rate limit, activation cooldown, reboot detector, and quiet-hours rule.

The optional runtime configuration lives outside the repository at
``%LOCALAPPDATA%\AWTRIX\utility_hub.json``.  See ``utility_hub.example.json``.
"""

from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import threading

from service_runtime import DriftMatrixConfig, run_owned, worker_adapter, wait
from dataclasses import dataclass, field
from datetime import time as wall_time, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import network_sentinel as sentinel
import network_sentinel_preview as sentinel_art
import commute_radar
import desk_companion
import desk_reminder
import hub_status
import focus_controller
import github_sentinel
import meeting_sentinel
import miku_sentinel
import game_mode
import iss_watch
import notify_mirror
import panel_down_alert
import panel_maintenance
import quake_watch
import sky_watch
import next_up_controller as nextup_controller
from panel_client import (
    MutationPriority,
    PanelClient,
    PanelClientError,
    PanelPolicy,
    SystemClock,
)


DEFAULT_PANEL_URL = "http://192.168.1.90"
DEFAULT_QUIET_START = "21:30"
DEFAULT_QUIET_END = "07:00"
NETWORK_APP_NAME = "networksentinel"
DEFAULT_GATEWAY_FALLBACK = "192.168.1.1"
PANEL_HEALTH_INTERVAL_SECONDS = 30.0
PANEL_FAILURE_BACKOFF_SECONDS = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)


def default_config_path() -> Path:
    from runtime_paths import current_data_dir
    if current_data_dir() is not None:
        return current_data_dir() / "utility_hub.json"
    local = os.environ.get("LOCALAPPDATA")
    root = Path(local) if local else Path.home() / "AppData" / "Local"
    return root / "AWTRIX" / "utility_hub.json"


@dataclass(frozen=True)
class RawTarget:
    host: str
    port: int


@dataclass(frozen=True)
class NetworkConfig:
    enabled: bool = True
    gateway: str | None = None
    raw_targets: tuple[RawTarget, ...] = (
        RawTarget("1.1.1.1", 443),
        RawTarget("8.8.8.8", 53),
    )
    dns_host: str = "www.msftconnecttest.com"
    connectivity_url: str = "http://www.msftconnecttest.com/connecttest.txt"
    connectivity_expected: str = "Microsoft Connect Test"
    probe_interval_seconds: float = 5.0
    dns_interval_seconds: float = 15.0
    connectivity_interval_seconds: float = 30.0
    alert_refresh_seconds: float = 15.0
    alert_lifetime_seconds: float = 45.0
    recovery_seconds: float = 12.0


@dataclass(frozen=True)
class NextUpConfig:
    enabled: bool = False
    sources: tuple[str, ...] = field(default=(), repr=False)
    timezone_name: str = "America/Los_Angeles"
    include_all_day: bool = False
    aliases: tuple[tuple[str, str], ...] = ()
    refresh_seconds: float = 300.0
    cache_max_age_seconds: float = 6 * 60 * 60


@dataclass(frozen=True)
class FocusConfig:
    enabled: bool = True
    preset: str = "classic"
    motion: str = "calm"
    refresh_seconds: float = 5.0
    static_refresh_seconds: float = 15.0
    lifetime_seconds: float = 20.0
    paused_visibility_seconds: float = 30 * 60.0


@dataclass(frozen=True)
class MeetingConfig:
    enabled: bool = True
    refresh_seconds: float = 3.0
    lifetime_seconds: float = 30.0


@dataclass(frozen=True)
class GitHubHubConfig:
    enabled: bool = False
    repos: tuple[str, ...] = ()
    token: str | None = None
    poll_interval_seconds: float = 60.0
    pr_radar_enabled: bool = True


@dataclass(frozen=True)
class CompanionHubConfig:
    enabled: bool = False
    refresh_seconds: float = 240.0


@dataclass(frozen=True)
class CommuteHubConfig:
    enabled: bool = False
    refresh_seconds: float = 60.0


@dataclass(frozen=True)
class MikuHubConfig:
    enabled: bool = False
    refresh_seconds: float = 30.0


@dataclass(frozen=True)
class PlanesHubConfig:
    enabled: bool = False
    radius_nm: float = 2.5
    max_altitude_ft: int = 12_000
    include_light_aircraft: bool = False
    burst_poll_seconds: float = 20.0
    relaxed_poll_seconds: float = 60.0


@dataclass(frozen=True)
class QuakesHubConfig:
    enabled: bool = False
    radius_km: float = 150.0
    min_magnitude: float = 2.5
    latitude: float = quake_watch.HOME_LAT
    longitude: float = quake_watch.HOME_LON


@dataclass(frozen=True)
class GameModeHubConfig:
    enabled: bool = False
    extra_games: tuple[str, ...] = ()


@dataclass(frozen=True)
class IssHubConfig:
    enabled: bool = False


@dataclass(frozen=True)
class NotificationsHubConfig:
    enabled: bool = False
    max_age_minutes: float = 15.0
    rules: tuple[notify_mirror.AppRule, ...] = notify_mirror.DEFAULT_RULES


@dataclass(frozen=True)
class MaintenanceHubConfig:
    enabled: bool = False
    window_start_minute: int = 21 * 60 + 35
    window_end_minute: int = 23 * 60
    min_uptime_hours: float = 20.0
    # On with maintenance itself: the 2026-10-07 outage started an hour
    # before the nightly window and its warning sign was visible 46 minutes
    # earlier (panel_maintenance's docstring has the numbers).
    low_memory_reboot: bool = True
    # 0 = off: on the logged days the block signal only moved the dark nightly
    # reboot to a lit evening one (panel_maintenance's docstring).
    low_memory_block_bytes: int = 0
    low_memory_min_free_bytes: int = 8192


@dataclass(frozen=True)
class PanelDownAlertHubConfig:
    # On by default: on 2026-10-09 the panel was unreachable for hours and
    # nothing on the PC said so (panel_down_alert's docstring).
    enabled: bool = True
    after_minutes: float = panel_down_alert.DEFAULT_AFTER_MINUTES
    min_gap_minutes: float = panel_down_alert.DEFAULT_MIN_GAP_MINUTES


@dataclass(frozen=True)
class HubConfig:
    panel_url: str = DEFAULT_PANEL_URL
    quiet_start_minute: int = 21 * 60 + 30
    quiet_end_minute: int = 7 * 60
    network: NetworkConfig = NetworkConfig()
    focus: FocusConfig = FocusConfig()
    next_up: NextUpConfig = NextUpConfig()
    meeting: MeetingConfig = MeetingConfig()
    github: GitHubHubConfig = GitHubHubConfig()
    companion: CompanionHubConfig = CompanionHubConfig()
    commute: CommuteHubConfig = CommuteHubConfig()
    miku: MikuHubConfig = MikuHubConfig()
    planes: PlanesHubConfig = PlanesHubConfig()
    quakes: QuakesHubConfig = QuakesHubConfig()
    game_mode: GameModeHubConfig = GameModeHubConfig()
    iss: IssHubConfig = IssHubConfig()
    maintenance: MaintenanceHubConfig = MaintenanceHubConfig()
    notifications: NotificationsHubConfig = NotificationsHubConfig()
    panel_down_alert: PanelDownAlertHubConfig = PanelDownAlertHubConfig()
    reminders_enabled: bool = False


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _positive(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be a positive number") from error
    if not 0 < result <= 86_400:
        raise ValueError(f"{name} must be between 0 and 86400 seconds")
    return result


def parse_clock(value: Any, name: str) -> int:
    if not isinstance(value, str):
        raise ValueError(f"{name} must use HH:MM")
    pieces = value.split(":")
    if len(pieces) != 2 or any(not piece.isdigit() for piece in pieces):
        raise ValueError(f"{name} must use HH:MM")
    hour, minute = (int(piece) for piece in pieces)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"{name} must use a valid 24-hour time")
    return hour * 60 + minute


def _targets(value: Any) -> tuple[RawTarget, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("network_sentinel.raw_targets must be a list")
    targets: list[RawTarget] = []
    for index, item in enumerate(value):
        if isinstance(item, RawTarget):
            targets.append(item)
            continue
        body = _mapping(item, f"raw_targets[{index}]")
        host = body.get("host")
        port = body.get("port")
        if not isinstance(host, str) or not host.strip():
            raise ValueError(f"raw_targets[{index}].host must be non-empty")
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65_535:
            raise ValueError(f"raw_targets[{index}].port must be 1..65535")
        targets.append(RawTarget(host.strip(), port))
    if len(targets) < 2:
        raise ValueError("at least two independent raw_targets are required")
    return tuple(targets)


def config_from_mapping(value: Mapping[str, Any]) -> HubConfig:
    panel_url = value.get("panel_url", DEFAULT_PANEL_URL)
    if not isinstance(panel_url, str) or not panel_url.startswith(("http://", "https://")):
        raise ValueError("panel_url must be an http(s) URL")

    quiet = _mapping(value.get("quiet_hours", {}), "quiet_hours")
    quiet_start = parse_clock(quiet.get("start", DEFAULT_QUIET_START), "quiet_hours.start")
    quiet_end = parse_clock(quiet.get("end", DEFAULT_QUIET_END), "quiet_hours.end")

    defaults = NetworkConfig()
    raw = _mapping(value.get("network_sentinel", {}), "network_sentinel")
    enabled = raw.get("enabled", defaults.enabled)
    if not isinstance(enabled, bool):
        raise ValueError("network_sentinel.enabled must be true or false")
    gateway = raw.get("gateway", defaults.gateway)
    if gateway is not None and (not isinstance(gateway, str) or not gateway.strip()):
        raise ValueError("network_sentinel.gateway must be null or a non-empty address")

    def string(name: str, default: str) -> str:
        result = raw.get(name, default)
        if not isinstance(result, str) or not result.strip():
            raise ValueError(f"network_sentinel.{name} must be non-empty")
        return result.strip()

    network = NetworkConfig(
        enabled=enabled,
        gateway=gateway.strip() if isinstance(gateway, str) else None,
        raw_targets=_targets(raw.get("raw_targets", defaults.raw_targets)),
        dns_host=string("dns_host", defaults.dns_host),
        connectivity_url=string("connectivity_url", defaults.connectivity_url),
        connectivity_expected=string("connectivity_expected", defaults.connectivity_expected),
        probe_interval_seconds=_positive(
            raw.get("probe_interval_seconds", defaults.probe_interval_seconds),
            "network_sentinel.probe_interval_seconds",
        ),
        dns_interval_seconds=_positive(
            raw.get("dns_interval_seconds", defaults.dns_interval_seconds),
            "network_sentinel.dns_interval_seconds",
        ),
        connectivity_interval_seconds=_positive(
            raw.get(
                "connectivity_interval_seconds", defaults.connectivity_interval_seconds
            ),
            "network_sentinel.connectivity_interval_seconds",
        ),
        alert_refresh_seconds=_positive(
            raw.get("alert_refresh_seconds", defaults.alert_refresh_seconds),
            "network_sentinel.alert_refresh_seconds",
        ),
        alert_lifetime_seconds=_positive(
            raw.get("alert_lifetime_seconds", defaults.alert_lifetime_seconds),
            "network_sentinel.alert_lifetime_seconds",
        ),
        recovery_seconds=_positive(
            raw.get("recovery_seconds", defaults.recovery_seconds),
            "network_sentinel.recovery_seconds",
        ),
    )
    if network.alert_lifetime_seconds < network.alert_refresh_seconds * 2:
        raise ValueError("alert_lifetime_seconds must cover at least two refresh intervals")

    next_defaults = NextUpConfig()
    next_raw = _mapping(value.get("next_up", {}), "next_up")
    next_enabled = next_raw.get("enabled", next_defaults.enabled)
    if not isinstance(next_enabled, bool):
        raise ValueError("next_up.enabled must be true or false")
    source_value = next_raw.get("sources", next_defaults.sources)
    if not isinstance(source_value, Sequence) or isinstance(source_value, (str, bytes)):
        raise ValueError("next_up.sources must be a list")
    sources: list[str] = []
    for index, source in enumerate(source_value):
        if not isinstance(source, str) or not source.strip():
            raise ValueError(f"next_up.sources[{index}] must be non-empty")
        cleaned = source.strip()
        # Apple Calendar and Outlook hand out subscription links as webcal://,
        # which is HTTPS under another name.
        if cleaned.lower().startswith("webcal://"):
            cleaned = "https://" + cleaned[len("webcal://"):]
        if "://" in cleaned and not cleaned.startswith(("http://", "https://")):
            raise ValueError(f"next_up.sources[{index}] must be a local path or http(s) URL")
        sources.append(cleaned)
    if len(sources) > 16:
        raise ValueError("next_up.sources supports at most 16 calendars")

    timezone_name = next_raw.get("timezone", next_defaults.timezone_name)
    if not isinstance(timezone_name, str) or not timezone_name:
        raise ValueError("next_up.timezone must be a non-empty IANA name")
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"next_up.timezone is unknown: {timezone_name}") from error

    include_all_day = next_raw.get("include_all_day", next_defaults.include_all_day)
    if not isinstance(include_all_day, bool):
        raise ValueError("next_up.include_all_day must be true or false")
    alias_value = _mapping(next_raw.get("aliases", {}), "next_up.aliases")
    aliases: list[tuple[str, str]] = []
    for title, alias in alias_value.items():
        if not isinstance(title, str) or not title.strip():
            raise ValueError("next_up alias titles must be non-empty strings")
        if not isinstance(alias, str) or not alias.strip():
            raise ValueError(f"next_up alias for {title!r} must be non-empty")
        aliases.append((title.strip(), alias.strip().upper()))
    next_up = NextUpConfig(
        enabled=next_enabled,
        sources=tuple(sources),
        timezone_name=timezone_name,
        include_all_day=include_all_day,
        aliases=tuple(sorted(aliases)),
        refresh_seconds=_positive(
            next_raw.get("refresh_seconds", next_defaults.refresh_seconds),
            "next_up.refresh_seconds",
        ),
        cache_max_age_seconds=_positive(
            next_raw.get("cache_max_age_seconds", next_defaults.cache_max_age_seconds),
            "next_up.cache_max_age_seconds",
        ),
    )

    focus_defaults = FocusConfig()
    focus_raw = _mapping(value.get("focus_sprint", {}), "focus_sprint")
    focus_enabled = focus_raw.get("enabled", focus_defaults.enabled)
    if not isinstance(focus_enabled, bool):
        raise ValueError("focus_sprint.enabled must be true or false")
    preset = focus_raw.get("preset", focus_defaults.preset)
    if not isinstance(preset, str) or preset.casefold() not in {
        "classic",
        "deep",
        "sprint",
    }:
        raise ValueError("focus_sprint.preset must be classic, deep, or sprint")
    motion = focus_raw.get("motion", focus_defaults.motion)
    if not isinstance(motion, str) or motion.casefold() not in {"calm", "static"}:
        raise ValueError("focus_sprint.motion must be calm or static")
    focus = FocusConfig(
        enabled=focus_enabled,
        preset=preset.casefold(),
        motion=motion.casefold(),
        refresh_seconds=_positive(
            focus_raw.get("refresh_seconds", focus_defaults.refresh_seconds),
            "focus_sprint.refresh_seconds",
        ),
        static_refresh_seconds=_positive(
            focus_raw.get(
                "static_refresh_seconds", focus_defaults.static_refresh_seconds
            ),
            "focus_sprint.static_refresh_seconds",
        ),
        lifetime_seconds=_positive(
            focus_raw.get("lifetime_seconds", focus_defaults.lifetime_seconds),
            "focus_sprint.lifetime_seconds",
        ),
        paused_visibility_seconds=_positive(
            focus_raw.get(
                "paused_visibility_seconds",
                focus_defaults.paused_visibility_seconds,
            ),
            "focus_sprint.paused_visibility_seconds",
        ),
    )
    if focus.lifetime_seconds <= max(
        focus.refresh_seconds, focus.static_refresh_seconds
    ):
        raise ValueError(
            "focus_sprint.lifetime_seconds must exceed both refresh intervals"
        )
    meeting_defaults = MeetingConfig()
    meeting_raw = _mapping(value.get("meeting", {}), "meeting")
    meeting_enabled = meeting_raw.get("enabled", meeting_defaults.enabled)
    if not isinstance(meeting_enabled, bool):
        raise ValueError("meeting.enabled must be true or false")
    meeting = MeetingConfig(
        enabled=meeting_enabled,
        refresh_seconds=_positive(
            meeting_raw.get("refresh_seconds", meeting_defaults.refresh_seconds),
            "meeting.refresh_seconds",
        ),
        lifetime_seconds=_positive(
            meeting_raw.get("lifetime_seconds", meeting_defaults.lifetime_seconds),
            "meeting.lifetime_seconds",
        ),
    )

    github_defaults = GitHubHubConfig()
    github_raw = _mapping(value.get("github", {}), "github")
    github_enabled = github_raw.get("enabled", github_defaults.enabled)
    if not isinstance(github_enabled, bool):
        raise ValueError("github.enabled must be true or false")
    repos_raw = github_raw.get("repos", github_defaults.repos)
    repos = tuple(str(r) for r in repos_raw) if isinstance(repos_raw, (list, tuple)) else ()
    token = github_raw.get("token", github_defaults.token)
    if token is not None and not isinstance(token, str):
        token = None
    github = GitHubHubConfig(
        enabled=github_enabled,
        repos=repos,
        token=token,
        poll_interval_seconds=_positive(
            github_raw.get("poll_interval_seconds", github_defaults.poll_interval_seconds),
            "github.poll_interval_seconds",
        ),
        pr_radar_enabled=bool(github_raw.get("pr_radar_enabled", github_defaults.pr_radar_enabled)),
    )

    companion_defaults = CompanionHubConfig()
    companion_raw = _mapping(value.get("companion", {}), "companion")
    companion = CompanionHubConfig(
        enabled=bool(companion_raw.get("enabled", companion_defaults.enabled)),
        refresh_seconds=_positive(
            companion_raw.get("refresh_seconds", companion_defaults.refresh_seconds),
            "companion.refresh_seconds",
        ),
    )
    # The keep-alive must land well before the page's own lifetime runs out.
    if companion.refresh_seconds * 1000 * 2 > desk_companion.LIFETIME_MS:
        raise ValueError(
            f"companion.refresh_seconds must be at most {desk_companion.LIFETIME_MS // 2000} "
            "so the page is refreshed before it expires"
        )

    commute_defaults = CommuteHubConfig()
    commute_raw = _mapping(value.get("commute", {}), "commute")
    commute = CommuteHubConfig(
        enabled=bool(commute_raw.get("enabled", commute_defaults.enabled)),
        refresh_seconds=_positive(
            commute_raw.get("refresh_seconds", commute_defaults.refresh_seconds),
            "commute.refresh_seconds",
        ),
    )

    miku_defaults = MikuHubConfig()
    miku_raw = _mapping(value.get("miku", {}), "miku")
    miku = MikuHubConfig(
        enabled=bool(miku_raw.get("enabled", miku_defaults.enabled)),
        refresh_seconds=_positive(
            miku_raw.get("refresh_seconds", miku_defaults.refresh_seconds),
            "miku.refresh_seconds",
        ),
    )

    planes_raw = _mapping(value.get("planes", {}), "planes")
    planes_defaults = PlanesHubConfig()
    planes = PlanesHubConfig(
        enabled=bool(planes_raw.get("enabled", planes_defaults.enabled)),
        radius_nm=_positive(planes_raw.get("radius_nm", planes_defaults.radius_nm), "planes.radius_nm"),
        max_altitude_ft=int(_positive(
            planes_raw.get("max_altitude_ft", planes_defaults.max_altitude_ft), "planes.max_altitude_ft")),
        include_light_aircraft=bool(
            planes_raw.get("include_light_aircraft", planes_defaults.include_light_aircraft)),
        burst_poll_seconds=_positive(
            planes_raw.get("burst_poll_seconds", planes_defaults.burst_poll_seconds),
            "planes.burst_poll_seconds",
        ),
        relaxed_poll_seconds=_positive(
            planes_raw.get("relaxed_poll_seconds", planes_defaults.relaxed_poll_seconds),
            "planes.relaxed_poll_seconds",
        ),
    )
    quakes_raw = _mapping(value.get("quakes", {}), "quakes")
    quakes_defaults = QuakesHubConfig()
    if ("latitude" in quakes_raw) != ("longitude" in quakes_raw):
        raise ValueError("quakes.latitude and quakes.longitude must be supplied together")
    quake_home = quake_watch.validate_home((
        quakes_raw.get("latitude", quakes_defaults.latitude),
        quakes_raw.get("longitude", quakes_defaults.longitude),
    ))
    quakes = QuakesHubConfig(
        enabled=bool(quakes_raw.get("enabled", quakes_defaults.enabled)),
        radius_km=_positive(quakes_raw.get("radius_km", quakes_defaults.radius_km), "quakes.radius_km"),
        min_magnitude=_positive(
            quakes_raw.get("min_magnitude", quakes_defaults.min_magnitude), "quakes.min_magnitude"),
        latitude=quake_home[0], longitude=quake_home[1],
    )
    game_raw = _mapping(value.get("game_mode", {}), "game_mode")
    extra = game_raw.get("extra_games", [])
    if not isinstance(extra, list) or not all(isinstance(x, str) for x in extra):
        raise ValueError("game_mode.extra_games must be a list of executable names")
    game = GameModeHubConfig(enabled=bool(game_raw.get("enabled", False)), extra_games=tuple(extra))

    iss_raw = _mapping(value.get("iss", {}), "iss")
    iss = IssHubConfig(enabled=bool(iss_raw.get("enabled", False)))

    maint_raw = _mapping(value.get("maintenance", {}), "maintenance")
    maint_defaults = MaintenanceHubConfig()
    min_uptime = maint_raw.get("min_uptime_hours", maint_defaults.min_uptime_hours)
    if isinstance(min_uptime, bool) or not isinstance(min_uptime, (int, float)) or not 0 <= min_uptime <= 24 * 30:
        raise ValueError("maintenance.min_uptime_hours must be between 0 and 720")
    low_memory_bytes = {}
    for key in ("low_memory_block_bytes", "low_memory_min_free_bytes"):
        amount = maint_raw.get(key, getattr(maint_defaults, key))
        if isinstance(amount, bool) or not isinstance(amount, int) or not 0 <= amount <= 65_536:
            raise ValueError(f"maintenance.{key} must be a whole number of bytes from 0 to 65536")
        low_memory_bytes[key] = amount
    low_memory_reboot = maint_raw.get("low_memory_reboot", maint_defaults.low_memory_reboot)
    if not isinstance(low_memory_reboot, bool):
        raise ValueError("maintenance.low_memory_reboot must be true or false")
    maintenance = MaintenanceHubConfig(
        enabled=bool(maint_raw.get("enabled", False)),
        window_start_minute=parse_clock(maint_raw.get("window_start", "21:35"), "maintenance.window_start"),
        window_end_minute=parse_clock(maint_raw.get("window_end", "23:00"), "maintenance.window_end"),
        min_uptime_hours=float(min_uptime),
        low_memory_reboot=low_memory_reboot,
        **low_memory_bytes,
    )
    if maintenance.window_start_minute == maintenance.window_end_minute:
        raise ValueError("maintenance window cannot be empty")

    notes_raw = _mapping(value.get("notifications", {}), "notifications")
    notes_defaults = NotificationsHubConfig()
    notifications = NotificationsHubConfig(
        enabled=bool(notes_raw.get("enabled", False)),
        max_age_minutes=_positive(
            notes_raw.get("max_age_minutes", notes_defaults.max_age_minutes),
            "notifications.max_age_minutes",
        ),
        rules=(notify_mirror.rules_from_config(notes_raw["apps"])
               if "apps" in notes_raw else notes_defaults.rules),
    )

    down_raw = _mapping(value.get("panel_down_alert", {}), "panel_down_alert")
    down_defaults = PanelDownAlertHubConfig()
    down_enabled = down_raw.get("enabled", down_defaults.enabled)
    if not isinstance(down_enabled, bool):
        raise ValueError("panel_down_alert.enabled must be true or false")
    down_minutes = {}
    for key, low, high in (("after_minutes", 1, 720), ("min_gap_minutes", 0, 1440)):
        amount = down_raw.get(key, getattr(down_defaults, key))
        # NaN fails the range test too, so a JSON NaN cannot slip through.
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not low <= amount <= high:
            raise ValueError(f"panel_down_alert.{key} must be a number of minutes from {low} to {high}")
        down_minutes[key] = float(amount)
    panel_down = PanelDownAlertHubConfig(enabled=down_enabled, **down_minutes)

    reminders_raw = _mapping(value.get("desk_reminder", {}), "desk_reminder")
    reminders_enabled = reminders_raw.get("enabled", False)
    if not isinstance(reminders_enabled, bool):
        raise ValueError("desk_reminder.enabled must be true or false")

    return HubConfig(
        panel_url=panel_url.rstrip("/"),
        quiet_start_minute=quiet_start,
        quiet_end_minute=quiet_end,
        network=network,
        focus=focus,
        next_up=next_up,
        meeting=meeting,
        github=github,
        companion=companion,
        commute=commute,
        miku=miku,
        planes=planes,
        quakes=quakes,
        game_mode=game,
        iss=iss,
        maintenance=maintenance,
        notifications=notifications,
        panel_down_alert=panel_down,
        reminders_enabled=reminders_enabled,
    )


def load_config(path: Path | None = None) -> HubConfig:
    target = (path or default_config_path()).expanduser()
    if not target.exists():
        return HubConfig()
    try:
        # utf-8-sig: Notepad and PowerShell 5.1 both write a BOM.
        value = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read utility hub config {target}: {error}") from error
    return config_from_mapping(_mapping(value, "configuration"))


class HubClock(Protocol):
    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class HubService(Protocol):
    def tick(self) -> object: ...


class UtilityHubRunner:
    """Run independent utility services without letting one failure stop peers."""

    def __init__(
        self,
        services: Sequence[HubService],
        *,
        interval_seconds: float = 5.0,
        clock: HubClock | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if not services:
            raise ValueError("Utility Hub needs at least one enabled service")
        if interval_seconds <= 0:
            raise ValueError("Utility Hub interval must be positive")
        self.services = tuple(services)
        self.interval_seconds = float(interval_seconds)
        self.clock = clock or SystemClock()
        self.logger = logger or logging.getLogger("awtrix.utility_hub")

    def tick(self, stop_event=None) -> list[object | None]:
        results: list[object | None] = []
        for service in self.services:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                results.append(service.tick())
            except Exception:
                self.logger.exception("Utility Hub service tick failed")
                results.append(None)
        return results

    def run_forever(self, stop_event=None) -> None:
        while stop_event is None or not stop_event.is_set():
            started = self.clock.monotonic()
            self.tick(stop_event)
            elapsed = self.clock.monotonic() - started
            wait(stop_event, self.interval_seconds - elapsed, sleep=self.clock.sleep)


@dataclass(frozen=True)
class NetworkTick:
    decision: sentinel.SentinelDecision
    pushed: bool = False
    activated: bool = False
    deleted: bool = False


class NetworkSentinelController:
    """Join probes, classification, compact art, and the shared panel client."""

    def __init__(
        self,
        config: NetworkConfig,
        probe: sentinel.ProbeSource,
        panel: PanelClient,
        *,
        classifier: sentinel.NetworkSentinel | None = None,
        clock: HubClock | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.probe = probe
        self.panel = panel
        self.classifier = classifier or sentinel.NetworkSentinel()
        self.clock = clock or SystemClock()
        self.logger = logger or logging.getLogger("awtrix.utility_hub.network")
        self.last_push_at: float | None = None
        self.last_health_at: float | None = None
        self.recovery_until: float | None = None
        self.remote_present = False
        self.panel_failures = 0
        self.next_panel_attempt_at = 0.0

    def pc_offline(self) -> bool:
        """This PC cannot reach its own router (a confirmed ROUTER fault).

        Then a failed panel probe says nothing about the panel; the status
        reporter samples this at each probe for the panel-down alert.
        """
        return self.classifier.active_fault is sentinel.NetworkState.ROUTER

    def _can_contact_panel(self, now: float) -> bool:
        return now >= self.next_panel_attempt_at

    def _panel_failed(self, now: float, error: BaseException) -> None:
        self.panel_failures += 1
        index = min(self.panel_failures, len(PANEL_FAILURE_BACKOFF_SECONDS)) - 1
        delay = PANEL_FAILURE_BACKOFF_SECONDS[index]
        self.next_panel_attempt_at = now + delay
        if self.panel_failures in (1, 3, 5) or self.panel_failures % 12 == 0:
            self.logger.warning(
                "panel unavailable (%d); retry in %.0fs: %s",
                self.panel_failures,
                delay,
                error,
            )

    def _panel_succeeded(self, now: float) -> None:
        self.panel_failures = 0
        self.next_panel_attempt_at = now

    @staticmethod
    def _art_metrics(
        decision: sentinel.SentinelDecision,
        sample: sentinel.ProbeSample,
    ) -> dict[str, float]:
        return {
            "latency_ms": sample.latency_ms if sample.latency_ms is not None else 180.0,
            "loss_percent": decision.loss_percent,
        }

    def _payloads(
        self,
        decision: sentinel.SentinelDecision,
        sample: sentinel.ProbeSample,
        *,
        lifetime_ms: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        metrics = self._art_metrics(decision, sample)
        common = {
            **metrics,
            "duration_ms": sentinel_art.PAGE_DURATION_MS,
            "lifetime_ms": lifetime_ms,
        }
        return (
            sentinel_art.build_payload(decision.state, animated=True, **common),
            sentinel_art.build_payload(decision.state, animated=False, **common),
        )

    def _push(
        self,
        decision: sentinel.SentinelDecision,
        sample: sentinel.ProbeSample,
        now: float,
        *,
        lifetime_ms: int,
    ) -> bool:
        if not self._can_contact_panel(now):
            return False
        animated, static = self._payloads(decision, sample, lifetime_ms=lifetime_ms)
        try:
            self.panel.push_app(NETWORK_APP_NAME, animated, static_payload=static)
        except (PanelClientError, OSError, RuntimeError) as error:
            self._panel_failed(self.clock.monotonic(), error)
            return False
        completed = self.clock.monotonic()
        self._panel_succeeded(completed)
        self.last_push_at = completed
        self.remote_present = True
        return True

    def _activate(self, now: float) -> bool:
        try:
            outcome = self.panel.activate_app(
                NETWORK_APP_NAME,
                priority=MutationPriority.OUTAGE,
                fast=False,
            )
        except (PanelClientError, OSError, RuntimeError) as error:
            self._panel_failed(self.clock.monotonic(), error)
            return False
        return outcome.sent

    def _maybe_probe_panel_health(self, now: float) -> None:
        if (
            self.last_health_at is not None
            and now - self.last_health_at < PANEL_HEALTH_INTERVAL_SECONDS
        ):
            return
        self.last_health_at = now
        try:
            self.panel.probe_device()
        except (PanelClientError, OSError, RuntimeError) as error:
            self.logger.debug("panel health probe failed: %s", error)

    def _delete(self, now: float) -> bool:
        if not self._can_contact_panel(now):
            return False
        try:
            self.panel.delete_pushed_app(NETWORK_APP_NAME)
        except (PanelClientError, OSError, RuntimeError) as error:
            self._panel_failed(self.clock.monotonic(), error)
            return False
        self._panel_succeeded(self.clock.monotonic())
        self.remote_present = False
        self.last_push_at = None
        return True

    def tick(self) -> NetworkTick:
        sample = self.probe.sample()
        decision = self.classifier.update(sample)
        now = self.clock.monotonic()
        self._maybe_probe_panel_health(now)

        if decision.is_alert:
            self.recovery_until = None
            refresh_due = (
                self.last_push_at is None
                or now - self.last_push_at >= self.config.alert_refresh_seconds
            )
            pushed = False
            activated = False
            if decision.changed or refresh_due:
                pushed = self._push(
                    decision,
                    sample,
                    now,
                    lifetime_ms=round(self.config.alert_lifetime_seconds * 1000),
                )
                if pushed and decision.changed:
                    activated = self._activate(now)
            return NetworkTick(decision, pushed=pushed, activated=activated)

        if decision.is_recovery:
            self.recovery_until = now + self.config.recovery_seconds
            pushed = self._push(
                decision,
                sample,
                now,
                lifetime_ms=round(self.config.recovery_seconds * 1000),
            )
            activated = self._activate(now) if pushed else False
            return NetworkTick(decision, pushed=pushed, activated=activated)

        if (
            self.remote_present
            and self.recovery_until is not None
            and now >= self.recovery_until
        ):
            deleted = self._delete(now)
            if deleted:
                self.recovery_until = None
            return NetworkTick(decision, deleted=deleted)

        # Healthy startup is intentionally silent. A stale alert from a dead
        # prior process has a 45-second lifetime and removes itself.
        return NetworkTick(decision)

    def run_forever(self) -> None:
        while True:
            started = self.clock.monotonic()
            try:
                self.tick()
            except Exception:
                self.logger.exception("Network Sentinel tick failed")
            elapsed = self.clock.monotonic() - started
            self.clock.sleep(max(0.0, self.config.probe_interval_seconds - elapsed))


def _wall_time_from_minute(value: int) -> wall_time:
    return wall_time(value // 60, value % 60)


def build_probe(config: NetworkConfig) -> tuple[str, sentinel.SystemProbe]:
    gateway = config.gateway or sentinel.discover_default_gateway()
    gateway = gateway or DEFAULT_GATEWAY_FALLBACK
    targets = tuple((item.host, item.port) for item in config.raw_targets)
    first_host, first_port = targets[0]
    probe_config = sentinel.SystemProbeConfig(
        router_host=gateway,
        wan_host=first_host,
        wan_port=first_port,
        wan_targets=targets,
        dns_name=config.dns_host,
        portal_url=config.connectivity_url,
        portal_body=config.connectivity_expected.encode("utf-8"),
        dns_interval_seconds=config.dns_interval_seconds,
        portal_interval_seconds=config.connectivity_interval_seconds,
    )
    return gateway, sentinel.SystemProbe(probe_config)


def build_panel_client(
    config: HubConfig,
    *,
    clock: SystemClock | None = None,
) -> PanelClient:
    """Build the one transport shared by every Utility Hub service."""

    shared_clock = clock or SystemClock()
    return PanelClient(
        config.panel_url,
        clock=shared_clock,
        policy=PanelPolicy(
            minimum_mutation_interval_seconds=2.0,
            activation_cooldown_seconds=10.0,
            quiet_start=_wall_time_from_minute(config.quiet_start_minute),
            quiet_end=_wall_time_from_minute(config.quiet_end_minute),
        ),
    )


def build_controller(
    config: HubConfig,
    *,
    clock: SystemClock | None = None,
    panel: PanelClient | None = None,
    logger: logging.Logger | None = None,
) -> NetworkSentinelController:
    shared_clock = clock or (panel.clock if panel is not None else SystemClock())
    _, probe = build_probe(config.network)
    shared_panel = panel or build_panel_client(config, clock=shared_clock)
    return NetworkSentinelController(
        config.network,
        probe,
        shared_panel,
        clock=shared_clock,
        logger=logger,
    )


def build_next_up_controller(
    config: HubConfig,
    panel: PanelClient,
    *,
    clock: SystemClock | None = None,
    logger: logging.Logger | None = None,
) -> nextup_controller.NextUpController:
    """Build Next Up on the same transport and clock as the other services."""

    if not config.next_up.sources:
        raise ValueError("Next Up is enabled but no calendar sources are configured")
    shared_clock = clock or panel.clock
    bindings = nextup_controller.build_calendar_sources(
        config.next_up.sources,
        refresh_seconds=config.next_up.refresh_seconds,
        max_stale_seconds=config.next_up.cache_max_age_seconds,
    )
    return nextup_controller.NextUpController(
        bindings,
        panel,
        timezone=config.next_up.timezone_name,
        include_all_day=config.next_up.include_all_day,
        aliases=config.next_up.aliases,
        policy=nextup_controller.NextUpPolicy(
            refresh_seconds=config.next_up.refresh_seconds,
            keepalive_seconds=60.0,
            lifetime_seconds=180.0,
            page_duration_ms=8_000,
        ),
        clock=shared_clock,
        logger=logger,
    )


def build_focus_controller(
    config: HubConfig,
    panel: PanelClient,
    *,
    clock: Any | None = None,
    logger: logging.Logger | None = None,
) -> focus_controller.FocusController:
    """Build Focus Sprint on the shared panel client and clock."""

    policy = focus_controller.FocusPolicy(
        motion=config.focus.motion,
        refresh_seconds=config.focus.refresh_seconds,
        static_refresh_seconds=config.focus.static_refresh_seconds,
        lifetime_seconds=config.focus.lifetime_seconds,
        paused_visibility_seconds=config.focus.paused_visibility_seconds,
    )
    return focus_controller.FocusController(
        panel,
        policy=policy,
        clock=clock,
        logger=logger,
    )


def build_maintenance_config(config: MaintenanceHubConfig) -> panel_maintenance.MaintenanceConfig:
    return panel_maintenance.MaintenanceConfig(
        enabled=config.enabled,
        window_start_minute=config.window_start_minute,
        window_end_minute=config.window_end_minute,
        min_uptime_hours=config.min_uptime_hours,
        low_memory_enabled=config.low_memory_reboot,
        low_memory_block_bytes=config.low_memory_block_bytes,
        low_memory_min_free_bytes=config.low_memory_min_free_bytes,
    )


def build_panel_down_alert(
    config: HubConfig,
    reporter: hub_status.HubStatusReporter,
    *,
    logger: logging.Logger | None = None,
) -> panel_down_alert.PanelDownAlert:
    """The popup reads the reporter's probes and Game Mode's game list."""
    return panel_down_alert.PanelDownAlert(
        reporter,
        after_minutes=config.panel_down_alert.after_minutes,
        min_gap_minutes=config.panel_down_alert.min_gap_minutes,
        extra_games=config.game_mode.extra_games,
        logger=logger,
    )


def build_meeting_controller(
    config: HubConfig,
    panel: PanelClient,
    *,
    clock: Any | None = None,
    logger: logging.Logger | None = None,
) -> meeting_sentinel.MeetingSentinelController:
    """Build Meeting Sentinel On-Air controller."""
    policy = meeting_sentinel.MeetingSentinelPolicy(
        refresh_seconds=config.meeting.refresh_seconds,
        lifetime_seconds=config.meeting.lifetime_seconds,
        enabled=config.meeting.enabled,
    )
    return meeting_sentinel.MeetingSentinelController(
        panel,
        policy=policy,
        clock=clock,
        logger=logger,
    )


def build_github_controller(
    config: HubConfig,
    panel: PanelClient,
    *,
    clock: Any | None = None,
    logger: logging.Logger | None = None,
) -> github_sentinel.GitHubSentinelController:
    """Build GitHub CI/CD and PR Radar sentinel controller."""
    gh_cfg = github_sentinel.GitHubConfig(
        enabled=config.github.enabled,
        repos=config.github.repos,
        token=config.github.token,
        poll_interval_seconds=config.github.poll_interval_seconds,
        pr_radar_enabled=config.github.pr_radar_enabled,
    )
    return github_sentinel.GitHubSentinelController(
        panel,
        gh_cfg,
        clock=clock,
        logger=logger,
    )


def build_companion_controller(
    config: HubConfig,
    panel: PanelClient,
    *,
    mood_detector: Callable[[], desk_companion.CompanionMood | tuple[desk_companion.CompanionMood, str | None]] | None = None,
    clock: Any | None = None,
    logger: logging.Logger | None = None,
) -> desk_companion.DeskCompanionController:
    """Build Desk Companion controller."""
    policy = desk_companion.CompanionPolicy(
        enabled=config.companion.enabled,
        refresh_seconds=config.companion.refresh_seconds,
    )
    if mood_detector is None:
        quiet_start = _wall_time_from_minute(config.quiet_start_minute)
        quiet_end = _wall_time_from_minute(config.quiet_end_minute)

        def _default_system_mood_detector() -> tuple[desk_companion.CompanionMood, str | None]:
            now_val = None
            if clock is not None:
                if hasattr(clock, "wall_now"):
                    now_val = clock.wall_now()
                elif hasattr(clock, "now"):
                    now_val = clock.now()
            return desk_companion.detect_system_companion_mood(
                quiet_start=quiet_start,
                quiet_end=quiet_end,
                now=now_val,
            )

        mood_detector = _default_system_mood_detector

    return desk_companion.DeskCompanionController(
        panel,
        policy=policy,
        mood_detector=mood_detector,
        clock=clock,
        logger=logger,
    )


def build_miku_controller(
    config: HubConfig,
    panel: PanelClient,
    *,
    status_reader: Callable[[], miku_sentinel.MikuStatus] | None = None,
    clock: Any | None = None,
    logger: logging.Logger | None = None,
) -> miku_sentinel.MikuSentinelController:
    """Build the Miku Sentinel controller."""
    policy = miku_sentinel.MikuPolicy(
        enabled=config.miku.enabled,
        refresh_seconds=config.miku.refresh_seconds,
    )
    return miku_sentinel.MikuSentinelController(
        panel,
        policy=policy,
        status_reader=status_reader,
        clock=clock,
        logger=logger,
    )


def build_commute_controller(
    config: HubConfig,
    panel: PanelClient,
    *,
    estimator: Callable[[str], commute_radar.CommuteEstimate] | None = None,
    clock: Any | None = None,
    logger: logging.Logger | None = None,
) -> commute_radar.CommuteRadarController:
    """Build Commute Radar controller."""
    commute_cfg = commute_radar.CommuteConfig(
        enabled=config.commute.enabled,
        refresh_seconds=config.commute.refresh_seconds,
    )
    return commute_radar.CommuteRadarController(
        panel,
        commute_cfg,
        estimator=estimator,
        clock=clock,
        logger=logger,
    )


def configure_logging() -> logging.Logger:
    logger = logging.getLogger("awtrix.utility_hub")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    folder = default_config_path().parent
    folder.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        folder / "utility_hub.log",
        maxBytes=512 * 1024,
        backupCount=2,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--once", action="store_true", help="probe once and print; do not push")
    return parser.parse_args()


def main(stop_event=None) -> int:
    args = parse_args()
    config = load_config(args.config)
    if args.once:
        gateway, probe = build_probe(config.network)
        sample = probe.sample()
        print(
            json.dumps(
                {
                    "panel_url": config.panel_url,
                    "gateway": gateway,
                    "router_ok": sample.router_ok,
                    "wan_ok": sample.wan_ok,
                    "dns_ok": sample.dns_ok,
                    "login_required": sample.login_required,
                    "latency_ms": sample.latency_ms,
                },
                indent=2,
            )
        )
        return
    return run_owned("hub", config.panel_url, default_config_path().parent,
                     stop_event or threading.Event(), lambda: run_hub(config, stop_event))


@worker_adapter("hub")
def run_worker(config: DriftMatrixConfig, stop_event: threading.Event) -> int:
    value = config.to_mapping()["services"]["hub"]["options"]
    value["panel_url"] = config.panel.url
    value["quiet_hours"] = dict(config.panel.quiet_hours)
    value.setdefault("next_up", {})["timezone"] = config.panel.timezone
    run_hub(config_from_mapping(value), stop_event, timezone_name=config.panel.timezone)
    return 0


def run_hub(config: HubConfig, stop_event=None, *, timezone_name=None) -> None:
    logger = configure_logging()
    clock = SystemClock()
    if timezone_name is not None:
        class WorkerClock(SystemClock):
            def wall_now(self):
                return datetime.now(ZoneInfo(timezone_name))
        clock = WorkerClock()
    panel = build_panel_client(config, clock=clock)
    try:
        services: list[HubService] = []
        names: list[str] = []
        # The heartbeat must keep running even when the only page has a long keepalive.
        intervals: list[float] = [hub_status.PROBE_INTERVAL_SECONDS]
        network_controller = None
        if config.network.enabled:
            network_controller = build_controller(
                config,
                clock=clock,
                panel=panel,
                logger=logger.getChild("network"),
            )
            services.append(network_controller)
            names.append("Network Sentinel")
            intervals.append(config.network.probe_interval_seconds)
        if config.next_up.enabled:
            if config.next_up.sources:
                services.append(
                    build_next_up_controller(
                        config,
                        panel,
                        clock=clock,
                        logger=logger.getChild("next_up"),
                    )
                )
                names.append("Next Up")
                intervals.append(5.0)
            else:
                logger.warning(
                    "Next Up is enabled but no calendar source is configured; service is idle"
                )
        if config.focus.enabled:
            services.append(
                build_focus_controller(
                    config,
                    panel,
                    logger=logger.getChild("focus"),
                )
            )
            names.append("Focus Sprint")
            intervals.append(config.focus.refresh_seconds)
        if config.meeting.enabled:
            services.append(
                build_meeting_controller(
                    config,
                    panel,
                    clock=clock,
                    logger=logger.getChild("meeting"),
                )
            )
            names.append("Meeting Sentinel")
            intervals.append(config.meeting.refresh_seconds)
        if config.github.enabled and config.github.repos:
            services.append(
                build_github_controller(
                    config,
                    panel,
                    clock=clock,
                    logger=logger.getChild("github"),
                )
            )
            names.append("GitHub Sentinel")
            intervals.append(config.github.poll_interval_seconds)
        if config.companion.enabled:
            services.append(
                build_companion_controller(
                    config,
                    panel,
                    clock=clock,
                    logger=logger.getChild("companion"),
                )
            )
            names.append("Desk Companion")
            intervals.append(config.companion.refresh_seconds)
        if config.commute.enabled:
            services.append(
                build_commute_controller(
                    config,
                    panel,
                    clock=clock,
                    logger=logger.getChild("commute"),
                )
            )
            names.append("Commute Radar")
            intervals.append(config.commute.refresh_seconds)
        if config.miku.enabled:
            services.append(
                build_miku_controller(
                    config,
                    panel,
                    clock=clock,
                    logger=logger.getChild("miku"),
                )
            )
            names.append("Miku Sentinel")
            intervals.append(config.miku.refresh_seconds)
        if config.planes.enabled:
            cfg = sky_watch.SkyWatchConfig(
                radius_nm=config.planes.radius_nm,
                max_altitude_ft=config.planes.max_altitude_ft,
                include_light_aircraft=config.planes.include_light_aircraft,
                burst_poll_seconds=config.planes.burst_poll_seconds,
                relaxed_poll_seconds=config.planes.relaxed_poll_seconds,
            )
            services.append(sky_watch.SkyWatchController(panel, cfg, logger=logger.getChild("planes")))
            names.append("Planes Overhead")
            intervals.append(cfg.burst_poll_seconds)
        if config.quakes.enabled:
            cfg = quake_watch.QuakeWatchConfig(
                radius_km=config.quakes.radius_km, min_magnitude=config.quakes.min_magnitude,
                lat=config.quakes.latitude, lon=config.quakes.longitude,
            )
            services.append(quake_watch.QuakeWatchController(panel, cfg, logger=logger.getChild("quakes")))
            names.append("Quake Watch")
            intervals.append(cfg.poll_seconds)
        if config.game_mode.enabled:
            cfg = game_mode.GameModeConfig(extra_games=config.game_mode.extra_games)
            services.append(game_mode.GameModeController(panel, cfg, logger=logger.getChild("game")))
            names.append("Game Mode")
            intervals.append(cfg.poll_seconds)
        if config.iss.enabled:
            cfg = iss_watch.IssWatchConfig(
                quiet_start_minute=config.quiet_start_minute, quiet_end_minute=config.quiet_end_minute,
            )
            services.append(iss_watch.IssWatchController(panel, cfg, logger=logger.getChild("iss")))
            names.append("ISS Watch")
            intervals.append(cfg.burst_poll_seconds)
        if config.maintenance.enabled:
            cfg = build_maintenance_config(config.maintenance)
            services.append(panel_maintenance.MaintenanceController(
                panel, cfg, logger=logger.getChild("maintenance")))
            names.append("Panel Maintenance")
            intervals.append(cfg.poll_seconds)
        if config.notifications.enabled:
            cfg = notify_mirror.NotifyMirrorConfig(
                rules=config.notifications.rules, max_age_minutes=config.notifications.max_age_minutes,
            )
            services.append(notify_mirror.NotifyMirrorController(
                panel, cfg, logger=logger.getChild("notifications")))
            names.append("Notification Mirror")
            intervals.append(cfg.poll_seconds)
        reminder_controller = None
        if config.reminders_enabled:
            reminder_controller = desk_reminder.ReminderController(panel)
            services.append(reminder_controller)
            names.append("Desk Reminder")
            intervals.append(desk_reminder.REFRESH_SECONDS)
        if not services:
            raise SystemExit("No Utility Hub services are enabled and configured")
        # The sentinel ticks first, so each probe reads its state for that tick.
        reporter = hub_status.HubStatusReporter(
            panel, reminders_enabled=config.reminders_enabled, reminder=reminder_controller,
            pc_offline=network_controller.pc_offline if network_controller is not None else None,
        )
        services.append(reporter)
        if config.panel_down_alert.enabled:
            # After the reporter, so each tick reads the probe it has just made.
            services.append(build_panel_down_alert(config, reporter, logger=logger.getChild("panel_down")))
            names.append("Panel-down alert")
        runner = UtilityHubRunner(
            services,
            interval_seconds=min(intervals),
            clock=clock,
            logger=logger,
        )
        logger.info("Utility Hub started; active services: %s", ", ".join(names))
        try:
            runner.run_forever(stop_event)
        except KeyboardInterrupt:
            logger.info("Utility Hub stopped")


    finally:
        panel.session.close()


if __name__ == "__main__":
    raise SystemExit(main(threading.Event()))
