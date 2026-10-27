"""Game Mode for AWTRIX -- the GPU at a glance while a game is running.

Present only while a game process is running; gone the moment it exits.

    icon   a gamepad whose face buttons light in turn
    lane   GPU temperature and game-process age in two held rows
    row 7  one temperature rail; a still fallback keeps both readings

A "game" is a process whose executable lives under a known game library
folder (Steam, Epic, Xbox, GOG, EA, Ubisoft, Riot, Battle.net), minus the
launchers and helpers that live there too.  Extra executables can be named in
``extra_games``.

FPS is deliberately absent: Windows exposes it only through a frame-capture
tool (PresentMon / RTSS), and this prototype does not install one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import math
from pathlib import PureWindowsPath
import subprocess
import time
from typing import Any, Callable, Iterable

from panel_scene import PresenceScene, encode_icon_gif, lane_payload, pattern_frame, run_scene
from visual_system import (
    AMBER, CORAL, GOLD, ICE, LANE_WIDTH, MINT, PEARL, RGB,
    VIOLET, Frame, measure_text_3x3, new_frame,
    render_split_lane, draw_level_rail, MIST,
)

APP_NAME = "game0"
MINUTES_PER_RAIL_PIXEL = 5
MIN_GAME_RSS_BYTES = 300 * 1024 * 1024  # helpers and crash reporters are small

LIBRARY_MARKERS = (
    "\\steamapps\\common\\",
    "\\epic games\\",
    "\\xboxgames\\",
    "\\gog galaxy\\games\\",
    "\\gog games\\",
    "\\ea games\\",
    "\\ubisoft game launcher\\games\\",
    "\\riot games\\",
    "\\battle.net\\",
)
NOT_GAMES = {
    "steam.exe", "steamwebhelper.exe", "steamservice.exe", "epicgameslauncher.exe",
    "epicwebhelper.exe", "unrealcefsubprocess.exe", "crashreportclient.exe",
    "riotclientservices.exe", "riotclientux.exe", "riotclientuxrender.exe",
    "battle.net.exe", "agent.exe", "eadesktop.exe", "eabackgroundservice.exe",
    "upc.exe", "uplaywebcore.exe", "galaxyclient.exe", "unitycrashhandler64.exe",
    "easyanticheat.exe", "easyanticheat_eos.exe", "beservice.exe", "vgc.exe",
}


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    exe: str
    create_time: float
    rss_bytes: int


@dataclass(frozen=True)
class GameSession:
    name: str
    pid: int
    started_at: float


@dataclass(frozen=True)
class GpuReading:
    temperature_c: int
    utilization_pct: int


def game_name(exe: str) -> str:
    """The library folder name ("Baldurs Gate 3"), falling back to the exe stem."""
    lowered = exe.lower()
    for marker in LIBRARY_MARKERS:
        at = lowered.find(marker)
        if at >= 0:
            rest = exe[at + len(marker):]
            folder = rest.replace("/", "\\").split("\\", 1)[0]
            if folder:
                return folder.upper()
    stem = PureWindowsPath(exe).stem
    for suffix in ("-Win64-Shipping", "-WinGDK-Shipping", "_x64", "Win64"):
        stem = stem.replace(suffix, "")
    return stem.upper()


def detect_game(processes: Iterable[ProcessInfo], extra_games: Iterable[str] = ()) -> GameSession | None:
    extras = {name.lower() for name in extra_games}
    candidates = []
    for proc in processes:
        if not proc.exe:
            continue
        base = PureWindowsPath(proc.exe).name.lower()
        if base in NOT_GAMES:
            continue
        in_library = any(marker in proc.exe.lower() for marker in LIBRARY_MARKERS)
        if base in extras or (in_library and proc.rss_bytes >= MIN_GAME_RSS_BYTES):
            candidates.append(proc)
    if not candidates:
        return None
    # Several matches is usually a game plus its own server; the biggest is the game.
    proc = max(candidates, key=lambda p: p.rss_bytes)
    return GameSession(game_name(proc.exe), proc.pid, proc.create_time)


def list_processes() -> list[ProcessInfo]:
    import psutil

    found = []
    for proc in psutil.process_iter(["pid", "exe", "create_time", "memory_info"]):
        info = proc.info
        mem = info.get("memory_info")
        found.append(ProcessInfo(
            pid=int(info["pid"]), exe=info.get("exe") or "",
            create_time=float(info.get("create_time") or 0.0),
            rss_bytes=int(mem.rss) if mem else 0,
        ))
    return found


def read_gpu() -> GpuReading | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=temperature.gpu,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip().splitlines()[0]
        temp, util = (int(v.strip()) for v in out.split(","))
        return GpuReading(temp, util)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


# ---------------------------------------------------------------- Art

def temperature_color(temp_c: int) -> RGB:
    if temp_c < 70:
        return ICE
    if temp_c < 80:
        return GOLD
    if temp_c < 85:
        return AMBER
    return CORAL


def load_color(pct: int) -> RGB:
    return MINT if pct < 90 else GOLD


GAMEPAD = (
    "........",
    ".BBBBBB.",
    "BB.BB1BB",
    "B...2.3B",
    "BB.BB4BB",
    "BBB..BBB",
    ".B....B.",
    "........",
)
# Face buttons cycle 1 -> 2 -> 3 -> 4, one lit per beat, like a demo loop.
BUTTON_COLORS = {"1": MINT, "2": CORAL, "3": GOLD, "4": ICE}


def icon_frames() -> list[Frame]:
    try:
        import icon_library
        return icon_library.gamepad_icon()
    except (ImportError, AttributeError):
        frames = []
        for lit in "1324":
            palette = {"B": VIOLET, **{k: (PEARL if k == lit else (0, 0, 0)) for k in BUTTON_COLORS}}
            palette[lit] = BUTTON_COLORS[lit]
            frames.append(pattern_frame(GAMEPAD, palette))
        return frames



TEMP_SCALE_C = (30, 95)  # the meter runs from an idle GPU to a throttling one


def temperature_level(temp_c: int) -> str:
    if temp_c < 70:
        return "good"
    if temp_c < 80:
        return "fair"
    if temp_c < 85:
        return "poor"
    return "bad"


def elapsed_label(minutes: int | float | None) -> str:
    if (isinstance(minutes, bool) or not isinstance(minutes, (int, float))
            or not math.isfinite(minutes) or minutes < 0):
        return 'AGE?'
    whole = math.floor(minutes)
    if whole < 60:
        return f'{whole}M'
    hours, remainder = divmod(whole, 60)
    return f'{hours}H{remainder:02d}' if hours < 100 else '99H+'


def render_lane(gpu: GpuReading | None, session_minutes: int | None = 0) -> Frame:
    """Temperature and process age together; only temperature owns the rail."""
    temp = getattr(gpu, 'temperature_c', None)
    valid = (not isinstance(temp, bool) and isinstance(temp, (int, float))
             and math.isfinite(temp) and measure_text_3x3(f'{round(temp)}C') <= LANE_WIDTH)
    value = f'{round(temp)}C' if valid else 'GPU?'
    frame = render_split_lane(value, elapsed_label(session_minutes), bottom_color=MIST)
    lo, hi = TEMP_SCALE_C
    draw_level_rail(frame, (temp - lo) / (hi - lo) if valid else None,
                    temperature_level(temp) if valid else 'info')
    return frame


def build_payload(gpu: GpuReading | None, session_minutes: int | None, *, animated=True) -> dict[str, Any]:
    frames = icon_frames()
    if not animated:
        frames = frames[:1]
    return lane_payload(render_lane(gpu, session_minutes), encode_icon_gif(frames),
                        duration_ms=8_000, lifetime_ms=30_000)


# ---------------------------------------------------------------- Controller

@dataclass(frozen=True)
class GameModeConfig:
    poll_seconds: float = 5.0
    refresh_seconds: float = 10.0
    extra_games: tuple[str, ...] = field(default_factory=tuple)


class GameModeController:
    def __init__(self, panel: Any, config: GameModeConfig | None = None, *,
                 processes: Callable[[], list[ProcessInfo]] | None = None,
                 gpu: Callable[[], GpuReading | None] | None = None,
                 clock: Any = time, logger: logging.Logger | None = None) -> None:
        self.config = config or GameModeConfig()
        self.processes = processes or list_processes
        self.gpu = gpu or read_gpu
        self.clock = clock
        self.logger = logger or logging.getLogger("awtrix.game_mode")
        self.scene = PresenceScene(panel, APP_NAME, self.config.refresh_seconds, clock, self.logger)
        self.next_poll_at = 0.0
        self._session_key: tuple[int, float | None, str] | None = None
        self._age_seconds: float | None = None
        self._age_mono: float | None = None

    def _process_age(self, session: GameSession, now: float) -> int | None:
        """Calibrate once from process creation, then advance with monotonic time."""
        start = session.started_at
        valid_start = (not isinstance(start, bool) and isinstance(start, (int, float))
                       and math.isfinite(start) and start > 0)
        key = (session.pid, start if valid_start else None, session.name)
        if key != self._session_key:
            self._session_key, self._age_seconds = key, None
        if self._age_seconds is None:
            wall = self.clock.time()
            if not valid_start or not math.isfinite(wall) or start > wall:
                self._age_mono = now
                return None
            self._age_seconds = wall - start
        else:
            delta = now - self._age_mono
            if not math.isfinite(delta) or delta < 0:
                self._age_seconds, self._age_mono = None, now
                return None
            self._age_seconds += delta
        self._age_mono = now
        return math.floor(self._age_seconds / 60)

    def tick(self) -> GameSession | None:
        now = self.clock.monotonic()
        if now < self.next_poll_at:
            return None
        self.next_poll_at = now + self.config.poll_seconds
        try:
            session = detect_game(self.processes(), self.config.extra_games)
        except Exception as err:  # noqa: BLE001
            self.logger.warning("Process scan failed: %s", err)
            return None
        if session is None:
            self.scene.hide()
            return None
        minutes = self._process_age(session, self.clock.monotonic())
        try:
            reading = self.gpu()
        except Exception as error:
            self.logger.warning('GPU reading failed: %s', type(error).__name__)
            reading = None
        self.scene.show(self._session_key, lambda _rev: build_payload(reading, minutes),
                        static_build=lambda _rev: build_payload(reading, minutes, animated=False))
        return session


if __name__ == "__main__":
    run_scene("Game Mode: GPU while gaming", lambda p: GameModeController(p), GameModeConfig().poll_seconds)
