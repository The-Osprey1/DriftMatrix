"""Tests for script_swap.py against a fake panel that reboots, restores and refuses.

Nothing here reaches the real panel: every request goes to :class:`FakePanel`,
which asserts its base URL, and LOCALAPPDATA points into ``tmp_path`` for the
whole module, so even a default path cannot land in the live data directory.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
import re

import pytest
import requests

import panel_gate
import script_swap as ss
from weather.minify_ax import minify


BASE = "http://panel.test"
WALL = datetime(2026, 10, 9, 21, 50)
OTHERS = ("nightkeeper", "nowplaying", "ocean")


@pytest.fixture(autouse=True)
def _isolated_localappdata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))


def berry(version: str, pad: int, name: str = "Moon") -> str:
    lines = [f"# @name {name}", "# @desc test page", f"# @version {version}", "",
             "# a whole-line comment that minify drops", f"class {name}"]
    lines += [f"  var v{i} = {i}  # kept: inline" for i in range(pad)]
    lines += ["  def draw()", "    clear()", "  end", "end", f"return {name}()"]
    return "\n".join(lines) + "\n"


OLD = berry("2.0", 420)
NEW = berry("2.1", 440)


def header_version(text: str) -> str | None:
    match = re.search(r"^# @version (\S+)", text, re.M)
    return match[1] if match else None


class FakeClock:
    """Monotonic and wall time that only move when slept; fires scheduled events."""

    def __init__(self, wall: datetime = WALL) -> None:
        self.t = 1000.0
        self.wall0 = wall
        self.events: list[tuple[float, object]] = []

    def monotonic(self) -> float:
        return self.t

    def wall_now(self) -> datetime:
        return self.wall0 + timedelta(seconds=self.t - 1000.0)

    def sleep(self, seconds: float) -> None:
        self.advance(max(0.0, float(seconds)))

    def advance(self, seconds: float) -> None:
        target = self.t + seconds
        while True:
            due = [event for event in self.events if event[0] <= target]
            if not due:
                break
            event = min(due, key=lambda item: item[0])
            self.events.remove(event)
            self.t = max(self.t, event[0])
            event[1]()
        self.t = target

    def at_wall(self, moment: datetime, action) -> None:
        self.events.append((1000.0 + (moment - self.wall0).total_seconds(), action))


class FakeResponse:
    def __init__(self, status: int = 200, body=None, content: bytes | None = None) -> None:
        self.status_code = status
        self._body = body
        if content is None:
            content = json.dumps(body).encode("utf-8") if body is not None else b""
        self.content = content
        self.text = content.decode("utf-8", "replace")

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body

    def raise_for_status(self) -> None:
        if not 200 <= self.status_code < 300:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def moon_frame(clock: FakeClock) -> list[int]:
    """A plausible moon frame: nebula speckle, the disc at x12-19, a twinkle."""
    pixels = [0] * 256
    for y in range(8):
        for x in range(12, 20):
            pixels[y * 32 + x] = 0x717988
        pixels[y * 32 + 3] = 0x0A1436
    pixels[int(clock.t) % 32] = 0xFFFFFF
    return pixels


class FakePanel:
    """A requests-like session playing an AWTRIX NG 1.1.2 panel.

    Models: scripts stored and compiled at boot in alphabetical order, a boot
    that takes ``boot_seconds`` and a restore ``restore_seconds`` after it
    (after the log's ``ntp:`` line; with ``ntp`` False neither comes), the
    largest free block with and without the target loaded, the PUT ``error``
    field (null / out of memory / any message) and 507, matrix power, the app
    rotation, the framebuffer, and a script that crashes the panel at restore
    (``boot_panics``: a boot loop with HTTP up only until the restore).
    """

    def __init__(self, clock: FakeClock, *, moon: str | None = OLD, uptime: int = 3 * 3600,
                 power: bool = False, block_with: int = 32_756, block_without: int = 52_000,
                 pushed_moon: bool = False) -> None:
        self.clock = clock
        self.stored: dict[str, str] = {name: berry("1.0", 40, name.title()) for name in OTHERS}
        self.errors: dict[str, str | None] = {name: None for name in OTHERS}
        if moon is not None:
            self.stored["moon"] = minify(moon)
            self.errors["moon"] = None
        self.pushed_moon = pushed_moon
        self.power = power
        self.block_with = block_with
        self.block_without = block_without
        self.boot_seconds = 10
        self.restore_seconds = 5
        self.boot_at = clock.t - uptime
        self.reset_reason = "software"
        self.down_until: float | None = None
        self.pending_reason = "software"
        self.restored = False
        self.block = 0
        self.log: list[str] = []
        self.put_results: list[object] = []
        self.boot_errors: dict[tuple[str, str | None], str] = {}
        self.writes: list[tuple[str, str, str | None, datetime]] = []
        self.current_app = lambda panel: "Time"
        self.screen = lambda panel: moon_frame(panel.clock)
        self.min_free = 20_000
        self.refuse_delete = False
        self.loop = {"order": ["Time", "ocean", "moon", "nightkeeper"], "disabled": ["Date"]}
        self.ntp = True
        self.boot_panics: set[tuple[str, str | None]] = set()
        self.boots = 0
        self._boot_log()
        self._restore()

    # -- boot model --------------------------------------------------------

    def _boot_log(self) -> None:
        self.log = ["04:35:25 boot: AWTRIX NG 1.1.2 on ESP32", "04:35:29 wifi: connected"]
        if self.ntp:
            self.log.append("21:35:29 ntp: syncing via pool.ntp.org (tz PST8PDT,M3.2.0,M11.1.0)")

    def _restore(self) -> None:
        if not self.ntp:
            return  # the restore follows NTP; without it the scripts never load
        self.restored = True
        loaded = 0
        for name in sorted(self.stored):
            if (name, header_version(self.stored[name])) in self.boot_panics:
                self.reboot_now("panic")  # crashes the panel while it compiles
                return
            message = self.boot_errors.get((name, header_version(self.stored[name])))
            if message is None and self.errors.get(name) and "out of memory" not in self.errors[name]:
                message = self.errors[name]  # a compile error stays one
            self.errors[name] = message
            if message is None:
                loaded += 1
                self.log.append(f"21:40:01 [script:{name}] vm heap +{len(self.stored[name]) * 2} bytes "
                                f"(shared {40000 + loaded}), free heap 9 -> 8, max block 50000")
        self.log.append(f"21:40:01 scripts: {loaded} restored")
        loaded_moon = "moon" in self.stored and self.errors.get("moon") is None
        self.block = self.block_with if loaded_moon else self.block_without

    def reboot_now(self, reason: str = "software") -> None:
        self.down_until = self.clock.t + self.boot_seconds
        self.pending_reason = reason

    def _tick(self) -> None:
        now = self.clock.t
        if self.down_until is not None:
            if now < self.down_until:
                raise requests.ConnectionError("panel is restarting")
            self.boot_at = self.down_until
            self.reset_reason = self.pending_reason
            self.down_until = None
            self.restored = False
            self.boots += 1
            self._boot_log()
        if not self.restored and now >= self.boot_at + self.restore_seconds:
            self._restore()
            if self.down_until is not None:
                raise requests.ConnectionError("panel crashed while restoring its scripts")

    # -- HTTP --------------------------------------------------------------

    def _path(self, url: str) -> str:
        assert url.startswith(BASE), f"request left the fake panel: {url}"
        return url[len(BASE):]

    def _record(self, method: str, path: str, version: str | None = None) -> None:
        self.writes.append((method, path, version, self.clock.wall_now()))

    def device(self) -> dict:
        return {
            "version": "1.1.2",
            "uptimeSeconds": int(self.clock.t - self.boot_at),
            "freeHeapBytes": 50_000,
            "minFreeHeapBytes": self.min_free,
            "largestFreeBlockBytes": self.block if self.restored else 100_000,
            "scriptingRunning": True,
            "fps": 42,
            "matrixPower": self.power,
            "currentApp": self.current_app(self),
            "resetReason": self.reset_reason,
        }

    def apps(self) -> list[dict]:
        apps = [{"name": "Time", "origin": "builtin", "present": True},
                {"name": "weatherbrief", "origin": "pushed", "present": True}]
        if self.pushed_moon:
            apps.append({"name": "moon", "origin": "pushed", "present": True})
        for slot, name in enumerate(sorted(self.stored)):
            error = self.errors.get(name) if self.restored else None
            apps.append({
                "name": name, "origin": "script", "present": True, "enabled": True,
                "inLoop": name != "nightkeeper", "headless": name == "nightkeeper",
                "slot": slot, "error": {"message": error} if error else None,
                "meta": {"name": name.title(), "version": header_version(self.stored[name])},
            })
        return apps

    def get(self, url: str, timeout=None, **kwargs) -> FakeResponse:
        path = self._path(url)
        self._tick()
        if path == "/api/v1/device":
            return FakeResponse(body=self.device())
        if path == "/api/v1/apps":
            return FakeResponse(body=self.apps())
        if path == "/api/v1/logs":
            return FakeResponse(body={"next": len(self.log), "lines": list(self.log)})
        if path == "/api/v1/display/screen":
            return FakeResponse(body={"width": 32, "height": 8, "pixels": self.screen(self)})
        if path == "/api/v1/display":
            return FakeResponse(body={"power": self.power})
        if path in ("/api/v1/settings", "/api/v1/system"):
            return FakeResponse(body={"surface": path})
        if path == "/apploop.json":
            return FakeResponse(body=self.loop)
        if path.startswith("/api/v1/apps/script/"):
            name = path.rsplit("/", 1)[-1]
            if name in self.stored and self.block >= 29_684:
                return FakeResponse(content=self.stored[name].encode("utf-8"))
            return FakeResponse(content=b"")
        raise AssertionError(f"unexpected GET {path}")

    def put(self, url: str, data=None, headers=None, timeout=None, **kwargs) -> FakeResponse:
        path = self._path(url)
        self._tick()
        assert path.startswith("/api/v1/apps/script/"), path
        name = path.rsplit("/", 1)[-1]
        text = bytes(data).decode("utf-8")
        version = header_version(text)
        self._record("PUT", path, version)
        outcome = self.put_results.pop(0) if self.put_results else "auto"
        if outcome == "auto":
            if self.block >= 3.5 * len(data):
                outcome = "ok"
            elif self.block >= 2 * len(data):
                outcome = "oom"
            else:
                outcome = "507"
        if outcome == "interrupt":
            raise KeyboardInterrupt
        if outcome == "507":
            return FakeResponse(507, body={"error": "insufficientStorage"})
        self.stored[name] = text
        if outcome == "panic":
            self.errors[name] = None
            self.reboot_now("panic")
            raise requests.ConnectionError("connection reset")
        if outcome in ("ok", "lost"):
            self.errors[name] = None
            self.log.append(f"21:41:00 [script:{name}] vm heap +{len(text) * 2} bytes (shared 80000), "
                            "free heap 9 -> 8, max block 30000")
            self.block -= 20_000
            if outcome == "lost":
                raise requests.ReadTimeout("reply lost")
            return FakeResponse(body={"name": name, "error": None})
        message = "out of memory" if outcome == "oom" else outcome[1]
        self.errors[name] = message
        return FakeResponse(body={"name": name, "error": {"message": message}})

    def delete(self, url: str, timeout=None, **kwargs) -> FakeResponse:
        path = self._path(url)
        self._tick()
        name = path.rsplit("/", 1)[-1]
        self._record("DELETE", path)
        if self.refuse_delete:
            return FakeResponse(500, body={"error": "busy"})
        if name in self.stored:
            del self.stored[name]
            self.errors.pop(name, None)
            return FakeResponse(body={})
        return FakeResponse(404, body={"error": "notFound"})

    def post(self, url: str, timeout=None, **kwargs) -> FakeResponse:
        path = self._path(url)
        self._tick()
        assert path == "/api/v1/device/reboot", path
        self._record("POST", path)
        self.reboot_now("software")
        return FakeResponse(body={})

    def patch(self, url: str, **kwargs):  # pragma: no cover - nothing may patch
        raise AssertionError("PATCH must never be sent")

    # -- assertions ----------------------------------------------------------

    def sequence(self) -> list[tuple[str, str | None]]:
        return [(method, version) for method, _path, version, _at in self.writes]

    def moon_version(self) -> str | None:
        return header_version(self.stored["moon"]) if "moon" in self.stored else None


def source_files(tmp_path: Path, new: str = NEW, old: str = OLD) -> tuple[Path, Path]:
    new_path, old_path = tmp_path / "moon-next.ax", tmp_path / "moon-old.ax"
    new_path.write_text(new, encoding="utf-8")
    old_path.write_text(old, encoding="utf-8")
    return new_path, old_path


def make_swap(tmp_path: Path, panel: FakePanel, clock: FakeClock, *, hub: ss.HubMaintenance | None = None,
              new: str = NEW, old: str = OLD, **overrides) -> ss.ScriptSwap:
    new_path, old_path = source_files(tmp_path, new, old)
    options = ss.SwapOptions(
        target="moon",
        new_path=new_path,
        expect_version="2.1",
        rollback_path=old_path,
        rollback_version="2.0",
        report_path=tmp_path / "data" / "script_installs.json",
        snapshot_root=tmp_path / "data" / "backups",
        maintenance_report=tmp_path / "data" / "maintenance.json",
        base_url=BASE,
        screen_seconds=40,
        argv=["moon", "moon-next.ax", "--expect-version", "2.1"],
    )
    for key, value in overrides.items():
        setattr(options, key, value)
    return ss.ScriptSwap(options, session=panel, clock=clock, hub_maintenance=hub or ss.HubMaintenance(),
                         logger=logging.getLogger("test.script_swap"))


def report(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "data" / "script_installs.json").read_text(encoding="utf-8"))


def last_entry(tmp_path: Path) -> dict:
    return report(tmp_path)["last"]


def step_names(entry: dict) -> list[str]:
    return [step["step"] for step in entry["steps"]]


def write_maintenance(tmp_path: Path, *entries: dict) -> None:
    path = tmp_path / "data" / "maintenance.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last": entries[-1] if entries else None, "history": list(entries)}),
                    encoding="utf-8")


def sizes() -> tuple[ss.SourceInfo, ss.SourceInfo]:
    """The test sources' minified sizes and block requirements."""
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        new_path, old_path = source_files(Path(folder))
        return ss.prepare_source(new_path, "2.1", "new"), ss.prepare_source(old_path, "2.0", "rollback")


# --------------------------------------------------------------- happy path and no-ops


def test_happy_path_deletes_reboots_installs_reboots_and_verifies(tmp_path: Path, panel_gate_dir: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    swap = make_swap(tmp_path, panel, clock)

    assert swap.run() == ss.EXIT_OK

    assert panel.sequence() == [("DELETE", None), ("POST", None), ("PUT", "2.1"), ("POST", None)]
    assert panel.moon_version() == "2.1" and panel.errors["moon"] is None
    assert panel.stored["moon"] == minify(NEW)
    entry = last_entry(tmp_path)
    assert entry["outcome"] == ss.OK and entry["exit_code"] == 0
    assert entry["installed_version"] == "2.1"
    assert entry["panel_state"] == "moon 2.1 installed and verified"
    names = step_names(entry)
    assert names.index("snapshot") < names.index("delete") < names.index("put") < names.index("verify")
    assert entry["verifications"][-1]["problems"] == []
    assert entry["verifications"][-1]["vm_heap"] == len(minify(NEW)) * 2
    assert entry["expected_scripts"] == ["moon", *OTHERS]
    # The snapshot came first and holds the exact old bytes read back from the panel.
    snapshot = Path(entry["snapshot"]["path"])
    assert (snapshot / "moon.ax").read_text(encoding="utf-8") == minify(OLD)
    assert entry["snapshot"]["target"]["provenance"] == "panel"
    # Fidelity: readback matched while the block allowed it.
    assert entry["fidelity"]["readback"]["status"] == "match"
    # Every write went through the gate as "install".
    lines = [json.loads(line) for line in (panel_gate_dir / panel_gate.EVENTS_NAME).read_text().splitlines()]
    assert [(line["src"], line["op"], line["app"]) for line in lines] == [
        ("install", "delete", "moon"), ("install", "reboot", None),
        ("install", "install", "moon"), ("install", "reboot", None)]
    assert lines[2]["bytes"] == len(minify(NEW).encode("utf-8"))


def test_already_installed_exits_zero_without_writing(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock, moon=NEW)
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_OK
    assert panel.writes == []
    entry = last_entry(tmp_path)
    assert entry["outcome"] == ss.ALREADY_INSTALLED
    assert entry["panel_state"].startswith("untouched")
    assert not (tmp_path / "data" / "backups").exists()


def test_dry_run_makes_zero_writes_and_takes_no_snapshot(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    swap = make_swap(tmp_path, panel, clock, dry_run=True)
    assert isinstance(swap.session, ss.ReadOnlySession)

    assert swap.run() == ss.EXIT_OK

    assert panel.writes == []
    assert not (tmp_path / "data" / "backups").exists()
    entry = last_entry(tmp_path)
    assert entry["outcome"] == ss.DRY_RUN and entry["mode"] == "dry-run"
    plan = "\n".join(entry["plan"])
    new, old = sizes()
    assert f"needs {new.required_block:,} B" in plan and f"sent with >= {old.rollback_block:,} B" in plan
    assert "DELETE moon" in plan and entry["blockers"] == []


def test_dry_run_reports_blockers_with_a_distinct_exit_code(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock, power=True)
    assert make_swap(tmp_path, panel, clock, dry_run=True, require_dark=True).run() == ss.EXIT_ABORTED
    assert panel.writes == []
    assert "not dark" in last_entry(tmp_path)["blockers"][0]


def test_read_only_session_refuses_writes() -> None:
    session = ss.ReadOnlySession(object())
    for method in ("put", "post", "delete", "patch"):
        with pytest.raises(ss.WriteBlocked):
            getattr(session, method)("http://panel.test/x")


# --------------------------------------------------------------- refusals before any write


@pytest.mark.parametrize("panel_kwargs, options, expected", [
    ({"power": True}, {"require_dark": True}, "not dark"),
    ({"moon": None, "pushed_moon": True}, {}, "origin 'pushed'"),
    ({"moon": None}, {}, "--install-if-absent"),
    ({"moon": berry("1.9", 10)}, {}, "neither"),
])
def test_refusals_send_nothing(tmp_path: Path, panel_kwargs: dict, options: dict, expected: str) -> None:
    clock = FakeClock()
    panel = FakePanel(clock, **panel_kwargs)
    assert make_swap(tmp_path, panel, clock, **options).run() == ss.EXIT_ABORTED
    assert panel.writes == []
    entry = last_entry(tmp_path)
    assert entry["outcome"] == ss.ABORTED and expected in entry["error"]
    assert entry["panel_state"].startswith("untouched")


def test_version_header_must_match_the_expected_version(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    swap = make_swap(tmp_path, panel, clock, new=berry("2.2", 10))
    assert swap.run() == ss.EXIT_ABORTED
    assert "declares '# @version 2.2'" in last_entry(tmp_path)["error"]
    assert panel.writes == []


def test_old_version_with_an_error_needs_replace_broken(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    panel.errors["moon"] = "runtime error"
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_ABORTED
    assert panel.writes == []
    assert make_swap(tmp_path, panel, clock, replace_broken=True).run() == ss.EXIT_OK
    assert panel.moon_version() == "2.1"


def test_install_if_absent_installs_a_missing_target(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock, moon=None, block_without=52_000)
    assert make_swap(tmp_path, panel, clock, install_if_absent=True).run() == ss.EXIT_OK
    assert panel.sequence() == [("POST", None), ("PUT", "2.1"), ("POST", None)]


# --------------------------------------------------------------- the Hub's maintenance


def test_refuses_while_the_nightly_reboot_is_due_without_wait_flag(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 10, 9, 21, 40))
    panel = FakePanel(clock, uptime=23 * 3600)
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_ABORTED
    assert panel.writes == []
    assert "--wait-for-nightly" in last_entry(tmp_path)["error"]


def test_refuses_while_maintenance_is_in_progress(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 10, 9, 21, 40))
    write_maintenance(tmp_path, {"at": "2026-10-09T21:39:00", "kind": "low-memory", "outcome": "in-progress"})
    panel = FakePanel(clock, uptime=600)
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_ABORTED
    assert "in progress" in last_entry(tmp_path)["error"]
    assert panel.writes == []


def test_a_stale_in_progress_entry_only_warns(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 10, 9, 21, 40))
    write_maintenance(tmp_path, {"at": "2026-10-09T20:00:00", "kind": "low-memory", "outcome": "in-progress"})
    panel = FakePanel(clock, uptime=600)
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_OK
    assert any("in-progress entry" in warning for warning in last_entry(tmp_path)["warnings"])


def test_wait_for_nightly_waits_for_the_hub_reboot_then_swaps(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 10, 9, 21, 36))
    panel = FakePanel(clock, uptime=23 * 3600)
    started = {"at": "2026-10-09T21:38:00", "kind": "nightly", "outcome": "in-progress"}

    def hub_starts() -> None:
        write_maintenance(tmp_path, started)
        panel.reboot_now("software")

    clock.at_wall(datetime(2026, 10, 9, 21, 38), hub_starts)
    clock.at_wall(datetime(2026, 10, 9, 21, 40),
                  lambda: write_maintenance(tmp_path, {**started, "outcome": "ok"}))

    assert make_swap(tmp_path, panel, clock, wait_for_nightly=True).run() == ss.EXIT_OK

    first_write = panel.writes[0][3]
    assert first_write >= datetime(2026, 10, 9, 21, 40)
    assert panel.sequence()[0] == ("DELETE", None)
    entry = last_entry(tmp_path)
    assert "wait-for-maintenance" in step_names(entry) and "maintenance-clear" in step_names(entry)
    assert entry["maintenance"]["nightly_done"]["outcome"] == "ok"


def test_wait_for_nightly_is_bounded(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 10, 9, 21, 36))
    panel = FakePanel(clock, uptime=23 * 3600)
    swap = make_swap(tmp_path, panel, clock, wait_for_nightly=True, max_wait_minutes=10)
    assert swap.run() == ss.EXIT_ABORTED
    assert panel.writes == []
    assert clock.wall_now() <= datetime(2026, 10, 9, 21, 47)
    assert "gave up after 10 min" in last_entry(tmp_path)["error"]


def test_wait_for_nightly_goes_ahead_once_the_window_closes_without_a_nightly(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 10, 9, 22, 50))
    panel = FakePanel(clock, uptime=23 * 3600)
    assert make_swap(tmp_path, panel, clock, wait_for_nightly=True, max_wait_minutes=60).run() == ss.EXIT_OK
    assert panel.writes[0][3] >= datetime(2026, 10, 9, 23, 0)
    assert any("window closed at 23:00" in warning for warning in last_entry(tmp_path)["warnings"])


def test_a_refused_delete_aborts_with_the_target_untouched(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    panel.refuse_delete = True
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_ABORTED
    assert panel.sequence() == [("DELETE", None)]
    assert panel.moon_version() == "2.0"
    entry = last_entry(tmp_path)
    assert entry["deleted"] is False and entry["panel_state"].startswith("unchanged: the panel refused DELETE")


def test_wait_for_nightly_does_not_wait_when_the_nightly_already_ran(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 10, 9, 21, 50))
    write_maintenance(tmp_path, {"at": "2026-10-09T21:35:31", "outcome": "ok"})
    panel = FakePanel(clock, uptime=900)
    assert make_swap(tmp_path, panel, clock, wait_for_nightly=True).run() == ss.EXIT_OK
    assert "wait-for-maintenance" not in step_names(last_entry(tmp_path))


def test_maintenance_view_reads_tonight_and_ignores_low_memory_entries() -> None:
    hub = ss.HubMaintenance()
    now = datetime(2026, 10, 9, 22, 0)
    entries = [{"at": "2026-10-08T21:35:31", "outcome": "ok"},
               {"at": "2026-10-09T21:40:00", "kind": "low-memory", "outcome": "ok"}]
    view = ss.maintenance_view(entries, now, hub, uptime_seconds=21 * 3600)
    assert view["inside_window"] and view["nightly_done"] is None and view["nightly_pending"]
    entries.append({"at": "2026-10-09T21:56:03", "outcome": "skipped"})
    view = ss.maintenance_view(entries, now, hub, uptime_seconds=21 * 3600)
    assert view["nightly_done"]["outcome"] == "skipped" and not view["nightly_pending"]
    # A young panel will be skipped by the Hub anyway.
    assert not ss.maintenance_view([], now, hub, uptime_seconds=3600)["nightly_pending"]


def test_nightly_window_contains_or_follows_now() -> None:
    start, end = ss.nightly_window(datetime(2026, 10, 9, 22, 0), 21 * 60 + 35, 23 * 60)
    assert (start, end) == (datetime(2026, 10, 9, 21, 35), datetime(2026, 10, 9, 23, 0))
    start, _ = ss.nightly_window(datetime(2026, 10, 9, 12, 0), 21 * 60 + 35, 23 * 60)
    assert start == datetime(2026, 10, 9, 21, 35)
    start, _ = ss.nightly_window(datetime(2026, 10, 9, 23, 30), 21 * 60 + 35, 23 * 60)
    assert start == datetime(2026, 10, 10, 21, 35)
    start, end = ss.nightly_window(datetime(2026, 10, 10, 1, 0), 23 * 60, 2 * 60)
    assert (start, end) == (datetime(2026, 10, 9, 23, 0), datetime(2026, 10, 10, 2, 0))


def test_load_hub_maintenance_uses_the_hub_parser_and_falls_back_enabled(tmp_path: Path) -> None:
    config = tmp_path / "utility_hub.json"
    config.write_text(json.dumps({"maintenance": {"enabled": True, "window_start": "21:00",
                                                  "window_end": "22:30", "min_uptime_hours": 12}}),
                      encoding="utf-8")
    hub = ss.load_hub_maintenance(config)
    assert (hub.enabled, hub.window_start_minute, hub.window_end_minute, hub.min_uptime_hours) == (
        True, 21 * 60, 22 * 60 + 30, 12)
    config.write_text("{not json", encoding="utf-8")
    fallback = ss.load_hub_maintenance(config)
    assert fallback.enabled and fallback.source.startswith("defaults")


# --------------------------------------------------------------- failures and rollback


def test_insufficient_block_sends_no_new_source_and_rolls_back(tmp_path: Path) -> None:
    new, old = sizes()
    clock = FakeClock()
    panel = FakePanel(clock, block_without=(new.required_block + old.required_block) // 2)
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_ROLLED_BACK
    # The fresh block fits the old source, so it goes straight back in.
    assert panel.sequence() == [("DELETE", None), ("POST", None), ("PUT", "2.0"), ("POST", None)]
    assert panel.moon_version() == "2.0" and panel.errors["moon"] is None
    entry = last_entry(tmp_path)
    assert entry["outcome"] == ss.ROLLED_BACK and "largest free block" in entry["rollback_reason"]
    assert "2.0 reinstalled and verified" in entry["panel_state"]


def test_no_room_for_either_version_fails_loudly_and_leaves_the_rest(tmp_path: Path, caplog) -> None:
    _new, old = sizes()
    clock = FakeClock()
    panel = FakePanel(clock, block_without=old.rollback_block - 1)
    with caplog.at_level(logging.CRITICAL, logger="test.script_swap"):
        assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_FAILED
    assert [method for method, _ in panel.sequence()] == ["DELETE", "POST", "POST", "POST"]
    assert "moon" not in panel.stored and set(panel.stored) == set(OTHERS)
    entry = last_entry(tmp_path)
    assert entry["outcome"] == ss.FAILED
    assert entry["panel_state"].startswith("FAILED: moon: None")
    # The way out it prints puts 2.0 back; it never swaps the roles of the sources.
    assert "--rollback-now" in entry["next"] and "--install-if-absent" not in entry["next"]
    assert any("SCRIPT SWAP FAILED" in record.message for record in caplog.records)


def test_out_of_memory_install_compiles_after_one_reboot(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    panel.put_results = ["oom"]
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_OK
    assert panel.sequence() == [("DELETE", None), ("POST", None), ("PUT", "2.1"), ("POST", None)]
    entry = last_entry(tmp_path)
    put = [step for step in entry["steps"] if step["step"] == "put-result"][0]
    assert put["kind"] == "oom" and put["status"] == 200
    assert panel.moon_version() == "2.1" and panel.errors["moon"] is None


def test_out_of_memory_that_persists_rolls_back(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    panel.put_results = ["oom"]
    panel.boot_errors[("moon", "2.1")] = "out of memory"
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_ROLLED_BACK
    assert panel.sequence() == [("DELETE", None), ("POST", None), ("PUT", "2.1"), ("POST", None),
                                ("DELETE", None), ("POST", None), ("PUT", "2.0"), ("POST", None)]
    assert panel.moon_version() == "2.0" and panel.errors["moon"] is None


def test_insufficient_storage_stores_nothing_and_rolls_back(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    panel.put_results = ["507"]
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_ROLLED_BACK
    # Nothing was stored, so there is nothing to delete before the old one goes back.
    assert panel.sequence() == [("DELETE", None), ("POST", None), ("PUT", "2.1"),
                                ("POST", None), ("PUT", "2.0"), ("POST", None)]
    assert "insufficient-storage" in last_entry(tmp_path)["rollback_reason"]


def test_compile_error_in_the_put_reply_rolls_back(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    panel.put_results = [("error", "syntax error at line 12")]
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_ROLLED_BACK
    assert "syntax error" in last_entry(tmp_path)["rollback_reason"]
    assert panel.moon_version() == "2.0"


def test_verification_failure_after_reboot_rolls_back(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    panel.boot_errors[("moon", "2.1")] = "attribute_error: nil"
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_ROLLED_BACK
    entry = last_entry(tmp_path)
    assert "verification failed" in entry["rollback_reason"] and "attribute_error" in entry["rollback_reason"]
    assert panel.moon_version() == "2.0" and panel.errors["moon"] is None
    assert entry["verifications"][-1]["version"] == "2.0" and entry["verifications"][-1]["problems"] == []


def test_another_script_failing_at_boot_blocks_the_new_install(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    panel.boot_errors[("ocean", "1.0")] = "out of memory"
    # ocean fails at every boot from here on: the new version is never sent,
    # the old one goes back, and the run still ends loudly because ocean is
    # not healthy -- a person has to look.
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_FAILED
    assert ("PUT", "2.1") not in panel.sequence()
    assert panel.moon_version() == "2.0"
    entry = last_entry(tmp_path)
    assert "ocean" in entry["rollback_reason"] and "ocean" in entry["error"]


def test_rollback_failure_is_loud(tmp_path: Path, caplog) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    panel.boot_errors[("moon", "2.1")] = "attribute_error: nil"
    panel.put_results = ["ok", ("error", "syntax error")]
    with caplog.at_level(logging.CRITICAL, logger="test.script_swap"):
        assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_FAILED
    entry = last_entry(tmp_path)
    assert entry["outcome"] == ss.FAILED and "reinstalling 2.0 failed" in entry["error"]
    assert any(record.levelno == logging.CRITICAL for record in caplog.records)


def test_lost_put_reply_is_resolved_from_the_inventory(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    panel.put_results = ["lost"]
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_OK
    resolved = [step for step in last_entry(tmp_path)["steps"] if step["step"] == "put-resolved"]
    assert resolved[0]["kind"] == "ok"


def test_a_panic_during_the_put_rolls_back(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    panel.put_results = ["panic"]
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_ROLLED_BACK
    entry = last_entry(tmp_path)
    assert "panic" in entry["rollback_reason"]
    assert panel.moon_version() == "2.0"
    # The panicked-in 2.1 compiled at boot, so it was deleted before 2.0 went back.
    assert panel.sequence()[3:] == [("DELETE", None), ("POST", None), ("PUT", "2.0"), ("POST", None)]


# --------------------------------------------------------------- interruption and resume


def test_ctrl_c_records_the_panel_state_and_a_rerun_resumes(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    panel.put_results = ["interrupt"]
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_INTERRUPTED
    entry = last_entry(tmp_path)
    assert entry["outcome"] == ss.INTERRUPTED and entry["deleted"] is True
    assert entry["panel_state"].startswith("PUT moon 2.1 sent")
    assert "moon" not in panel.stored

    clock.advance(60)
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_OK
    resumed = last_entry(tmp_path)
    assert resumed["resumed_from"] == entry["id"]
    assert panel.moon_version() == "2.1"
    assert panel.sequence()[-3:] == [("POST", None), ("PUT", "2.1"), ("POST", None)]
    assert resumed["expected_scripts"] == ["moon", *OTHERS]


def test_ctrl_c_during_the_rollback_resumes_the_rollback(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    panel.boot_errors[("moon", "2.1")] = "attribute_error: nil"
    panel.put_results = ["ok", "interrupt"]
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_INTERRUPTED
    assert last_entry(tmp_path)["rollback_reason"].startswith("verification failed")

    clock.advance(60)
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_ROLLED_BACK
    assert panel.moon_version() == "2.0" and panel.errors["moon"] is None


def test_unexplained_new_version_error_is_refused(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock, moon=NEW)
    panel.errors["moon"] = "attribute_error"
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_ABORTED
    assert panel.writes == []


# --------------------------------------------------------------- fidelity


def rotate_moon(panel: FakePanel) -> str:
    return "moon" if int(panel.clock.t) % 30 < 11 else "Time"


def test_black_frames_while_lit_are_clearly_broken_and_roll_back(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock, power=True)
    panel.current_app = rotate_moon
    panel.screen = lambda p: [0] * 256 if p.moon_version() == "2.1" else moon_frame(p.clock)
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_ROLLED_BACK
    entry = last_entry(tmp_path)
    assert "all black" in entry["rollback_reason"]
    assert panel.moon_version() == "2.0"


def test_black_frames_while_dark_are_inconclusive_not_broken(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock, power=False)
    panel.current_app = rotate_moon
    panel.screen = lambda p: [0] * 256
    assert make_swap(tmp_path, panel, clock, require_dark=True).run() == ss.EXIT_OK
    judgement = last_entry(tmp_path)["fidelity"]["screen"]["judgement"]
    assert judgement["verdict"] == "inconclusive" and "dark" in judgement["reason"]


def test_a_healthy_lit_page_passes_the_screen_check(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock, power=True)
    panel.current_app = rotate_moon
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_OK
    fidelity = last_entry(tmp_path)["fidelity"]
    assert fidelity["screen"]["judgement"]["verdict"] == "ok"
    assert fidelity["screen"]["fps_while_current"]
    # The fake disc is one flat colour, so a simulator that is present
    # disagrees -- and that is evidence, never a failure.
    assert fidelity["simulator"]["status"] in ("skipped", "mismatch")


def simulator():
    sim = pytest.importorskip("weather.moon_panel_sim")
    if not callable(getattr(sim, "render", None)) or "2.1" not in getattr(sim, "VERSIONS", {}):
        pytest.skip("weather.moon_panel_sim has no render() for 2.1")
    return sim


def test_the_disc_is_compared_with_the_simulator(tmp_path: Path) -> None:
    sim = simulator()
    clock = FakeClock()
    panel = FakePanel(clock, power=True)
    panel.current_app = rotate_moon

    def screen(p: FakePanel) -> list[int]:
        now = p.clock.wall_now()
        drawn = sim.render("2.1", int(p.clock.t * 1000), epoch_ms=int(now.timestamp() * 1000),
                           hour=now.hour, minute=now.minute, year=now.year)
        return list(drawn.pixels)

    panel.screen = screen
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_OK
    result = last_entry(tmp_path)["fidelity"]["simulator"]
    assert result["status"] == "match"
    assert all(item["differ"] == 0 for item in result["results"] if "differ" in item)


def test_simulator_is_not_consulted_for_a_dark_framebuffer(tmp_path: Path) -> None:
    simulator()
    clock = FakeClock()
    panel = FakePanel(clock, power=False)
    panel.current_app = rotate_moon
    panel.screen = lambda p: [0] * 256
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_OK
    assert "not being drawn" in last_entry(tmp_path)["fidelity"]["simulator"]["note"]


def test_disc_mask_follows_span0() -> None:
    mask = ss.disc_mask([2, 4, 1, 6, 0, 8, 0, 8, 0, 8, 0, 8, 1, 6, 2, 4])
    assert len(mask) == 4 + 6 + 8 * 4 + 6 + 4
    assert mask[0] == 14 and mask[-1] == 7 * 32 + 17


def capture(nonblack: int, disc: int, *, power, digest: str, window: bool = False) -> dict:
    return {"power": power, "disc_window": window,
            "stats": {"nonblack": nonblack, "disc_nonblack": disc, "hash": digest}}


def test_judge_screen_is_conservative() -> None:
    assert ss.judge_screen([], "moon")["verdict"] == "inconclusive"
    dark_black = [capture(0, 0, power=False, digest="a") for _ in range(6)]
    assert ss.judge_screen(dark_black, "moon")["verdict"] == "inconclusive"
    lit_black = [capture(0, 0, power=True, digest="a") for _ in range(4)]
    assert ss.judge_screen(lit_black, "moon")["verdict"] == "broken"
    assert ss.judge_screen(lit_black[:3], "moon")["verdict"] == "inconclusive"
    no_disc = [capture(40, 0, power=True, digest=str(i), window=True) for i in range(2)]
    assert ss.judge_screen(no_disc, "moon")["verdict"] == "broken"
    frozen_dark = [capture(40, 30, power=False, digest="same") for _ in range(5)]
    assert ss.judge_screen(frozen_dark, "moon")["verdict"] == "inconclusive"
    healthy = [capture(80, 50, power=True, digest=str(i), window=i == 0) for i in range(5)]
    assert ss.judge_screen(healthy, "moon")["verdict"] == "ok"


def test_frame_stats_counts_the_disc_columns() -> None:
    pixels = [0] * 256
    pixels[3 * 32 + 12] = 0x112233
    pixels[3 * 32 + 25] = 0x445566
    stats = ss.frame_stats({"width": 32, "height": 8, "pixels": pixels})
    assert stats["nonblack"] == 2 and stats["disc_nonblack"] == 1
    assert ss.frame_stats({"width": 32, "height": 8, "pixels": [0] * 10}) is None


# --------------------------------------------------------------- check only


def test_check_only_is_read_only_and_reports_health(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock, moon=NEW, power=True)
    panel.current_app = rotate_moon
    assert make_swap(tmp_path, panel, clock, check_only=True).run() == ss.EXIT_OK
    assert panel.writes == []
    assert last_entry(tmp_path)["outcome"] == ss.CHECKED_OK

    panel.errors["moon"] = "attribute_error"
    assert make_swap(tmp_path, panel, clock, check_only=True).run() == ss.EXIT_CHECK_FAILED
    assert panel.writes == []
    assert last_entry(tmp_path)["outcome"] == ss.CHECKED_BROKEN


def test_check_only_flags_the_old_version(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    assert make_swap(tmp_path, panel, clock, check_only=True, fidelity=False).run() == ss.EXIT_CHECK_FAILED
    assert "state is 'old'" in last_entry(tmp_path)["error"]


# --------------------------------------------------------------- helpers and CLI


def test_prepare_source_minifies_and_budgets_three_point_five(tmp_path: Path) -> None:
    path = tmp_path / "x.ax"
    path.write_text(NEW, encoding="utf-8")
    info = ss.prepare_source(path, "2.1", "new")
    assert info.body == minify(NEW).encode("utf-8")
    assert info.required_block == -(-len(info.body) * 35 // 10)
    # Tonight's sources: CLAUDE.md's 3.5x for the install, max(3.0x, 30 KB) for the rollback.
    assert ss.required_block(10_918) == 38_213 and ss.required_block(10_574) == 37_009
    assert ss.rollback_block(10_574) == 31_722 and ss.rollback_block(8_000) == 30_000
    assert ss.parse_header(minify(NEW))["version"] == "2.1"


def test_boot_log_helpers() -> None:
    logs = {"lines": ["old line", "04:35:25 boot: AWTRIX NG 1.1.2 on ESP32",
                      "21:35:29 [script:moon] vm heap +21162 bytes (shared 35978), free heap 1 -> 2, max block 65524",
                      "21:35:30 scripts: 4 restored"]}
    lines = ss.boot_lines(logs)
    assert lines[0].endswith("on ESP32") and ss.restored_count(lines) == 4 and ss.shared_total(lines) == 35978
    assert ss.boot_lines({"lines": "nope"}) is None


def test_main_dry_run_prints_the_plan(tmp_path: Path, capsys) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    new_path, old_path = source_files(tmp_path)
    try:
        code = ss.main([
            "moon", str(new_path), "--expect-version", "2.1", "--rollback", str(old_path),
            "--rollback-version", "2.0", "--dry-run", "--panel", BASE, "--data-dir", str(tmp_path / "d"),
            "--maintenance-report", str(tmp_path / "none.json"), "--screen-seconds", "5",
        ], session=panel, clock=clock, hub_maintenance=ss.HubMaintenance())
    finally:
        logger = logging.getLogger("awtrix.script_swap")
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
    assert code == ss.EXIT_OK
    assert panel.writes == []
    out = capsys.readouterr().out
    assert "Plan for moon: 2.0 -> 2.1" in out and "blockers none" in out
    saved = json.loads((tmp_path / "d" / ss.REPORT_NAME).read_text(encoding="utf-8"))
    assert saved["last"]["outcome"] == ss.DRY_RUN
    assert (tmp_path / "d" / ss.LOG_NAME).exists()


# --------------------------------------------------------------- review findings, 2026-10-09
#
# One regression test (or a few) per defect the review of the moon v2.1
# installer found; each reproduced the defect before the fix.


def hand_options(tmp_path: Path, **overrides) -> ss.SwapOptions:
    """The command a person types by hand, with the given flags."""
    new_path, old_path = source_files(tmp_path)
    options = ss.SwapOptions(
        target="moon", new_path=new_path, expect_version="2.1", rollback_path=old_path,
        rollback_version="2.0", report_path=tmp_path / "data" / "script_installs.json",
        snapshot_root=tmp_path / "data" / "backups", maintenance_report=tmp_path / "data" / "maintenance.json",
        base_url=BASE, screen_seconds=40,
    )
    for key, value in overrides.items():
        setattr(options, key, value)
    return options


def run_options(options: ss.SwapOptions, panel: FakePanel, clock: FakeClock) -> int:
    return ss.ScriptSwap(options, session=panel, clock=clock, hub_maintenance=ss.HubMaintenance(),
                         logger=logging.getLogger("test.script_swap")).run()


# -- the printed recovery command (critical)


def test_after_a_failed_run_the_printed_command_puts_the_old_version_back(tmp_path: Path) -> None:
    _new, old = sizes()
    clock = FakeClock()
    panel = FakePanel(clock, block_without=old.rollback_block - 1)
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_FAILED
    assert "moon" not in panel.stored and "--rollback-now" in last_entry(tmp_path)["next"]
    panel.block_without = 52_000
    clock.advance(600)
    before = len(panel.writes)
    assert run_options(hand_options(tmp_path, rollback_now=True), panel, clock) == ss.EXIT_ROLLED_BACK
    sent = panel.sequence()[before:]
    assert ("PUT", "2.0") in sent and ("PUT", "2.1") not in sent
    assert panel.moon_version() == "2.0" and panel.errors["moon"] is None


def test_rollback_now_after_a_rejected_new_version_never_sends_it(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    panel.boot_errors[("moon", "2.1")] = "attribute_error: nil"
    panel.put_results = ["ok", "507"]
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_FAILED
    clock.advance(600)
    before = len(panel.writes)
    assert run_options(hand_options(tmp_path, rollback_now=True), panel, clock) == ss.EXIT_ROLLED_BACK
    assert ("PUT", "2.1") not in panel.sequence()[before:]
    assert panel.moon_version() == "2.0" and panel.errors["moon"] is None


def test_rollback_now_uninstalls_a_healthy_new_version_too(tmp_path: Path) -> None:
    """The morning check found 2.1 wrong: --rollback-now takes it out."""
    clock = FakeClock()
    panel = FakePanel(clock, moon=NEW)
    assert run_options(hand_options(tmp_path, rollback_now=True), panel, clock) == ss.EXIT_ROLLED_BACK
    assert panel.sequence() == [("DELETE", None), ("POST", None), ("PUT", "2.0"), ("POST", None)]
    # Nothing to do when 2.0 is already there.
    clock.advance(60)
    again = tmp_path / "again"
    again.mkdir()
    assert run_options(hand_options(again, rollback_now=True), panel, clock) == ss.EXIT_ABORTED


def test_a_run_with_swapped_sources_does_not_resume_the_others_rollback(tmp_path: Path) -> None:
    """The old printed hand command: --expect-version 2.0 with 2.1 as its rollback.
    It must install what it was asked for (2.0), not continue the other run's
    rollback with its own 'rollback' source (2.1)."""
    _new, old = sizes()
    clock = FakeClock()
    panel = FakePanel(clock, block_without=old.rollback_block - 1)
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_FAILED
    panel.block_without = 52_000
    clock.advance(600)
    new_path, old_path = source_files(tmp_path)
    swapped = hand_options(tmp_path, new_path=old_path, expect_version="2.0", rollback_path=new_path,
                           rollback_version="2.1", install_if_absent=True)
    before = len(panel.writes)
    assert run_options(swapped, panel, clock) == ss.EXIT_OK
    assert ("PUT", "2.1") not in panel.sequence()[before:]
    assert panel.moon_version() == "2.0"
    entry = last_entry(tmp_path)
    assert "resumed_from" not in entry and any("different sources" in w for w in entry["warnings"])


def test_an_unfinished_run_with_other_sources_is_named_when_refusing(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    panel.put_results = ["interrupt"]
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_INTERRUPTED
    clock.advance(60)
    edited = berry("2.1", 441)  # moon-panel-next.ax changed since: other bytes, same version
    assert make_swap(tmp_path, panel, clock, new=edited).run() == ss.EXIT_ABORTED
    error = last_entry(tmp_path)["error"]
    assert "different sources" in error and "--rollback-now" in error
    assert "moon" not in panel.stored


# -- the resume chain (major)


def test_a_refused_resume_does_not_break_the_chain(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    panel.put_results = ["interrupt"]
    assert make_swap(tmp_path, panel, clock, require_dark=True).run() == ss.EXIT_INTERRUPTED
    a = last_entry(tmp_path)
    clock.advance(600)
    panel.power = True
    assert make_swap(tmp_path, panel, clock, require_dark=True).run() == ss.EXIT_ABORTED
    b = last_entry(tmp_path)
    # B sent nothing and says the panel is as A left it, not "untouched".
    assert b["writes"] == 0
    assert b["panel_state"].startswith(f"no write was sent by this run; the panel is as run {a['id']}")
    clock.advance(600)
    panel.power = False
    assert make_swap(tmp_path, panel, clock, require_dark=True).run() == ss.EXIT_OK
    assert last_entry(tmp_path)["resumed_from"] == a["id"] and panel.moon_version() == "2.1"


def test_a_snapshot_failure_on_resume_does_not_break_the_chain(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    panel.put_results = ["interrupt"]
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_INTERRUPTED

    def broken_snapshot(**kwargs):
        raise OSError("disk full")

    clock.advance(60)
    swap = make_swap(tmp_path, panel, clock)
    swap.snapshot_fn = broken_snapshot
    assert swap.run() == ss.EXIT_ABORTED
    clock.advance(60)
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_OK
    assert panel.moon_version() == "2.1"


def test_a_refused_resume_keeps_the_rollback_decision(tmp_path: Path) -> None:
    _new, old = sizes()
    clock = FakeClock()
    panel = FakePanel(clock, block_without=old.rollback_block - 1)
    assert make_swap(tmp_path, panel, clock, require_dark=True).run() == ss.EXIT_FAILED
    clock.advance(600)
    panel.power = True
    assert make_swap(tmp_path, panel, clock, require_dark=True).run() == ss.EXIT_ABORTED
    clock.advance(600)
    panel.power = False
    panel.block_without = 52_000
    before = len(panel.writes)
    assert make_swap(tmp_path, panel, clock, require_dark=True).run() == ss.EXIT_ROLLED_BACK
    assert ("PUT", "2.1") not in panel.sequence()[before:] and panel.moon_version() == "2.0"


def test_ctrl_c_while_a_resume_waits_keeps_the_rollback_decision(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    panel.boot_errors[("moon", "2.1")] = "attribute_error: nil"
    panel.put_results = ["ok", "interrupt"]
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_INTERRUPTED
    now = clock.wall_now()
    write_maintenance(tmp_path, {"at": now.isoformat(timespec="seconds"), "kind": "low-memory",
                                 "outcome": "in-progress"})

    def ctrl_c() -> None:
        raise KeyboardInterrupt

    clock.events.append((clock.t + 5, ctrl_c))
    assert make_swap(tmp_path, panel, clock, wait_for_nightly=True).run() == ss.EXIT_INTERRUPTED
    write_maintenance(tmp_path, {"at": now.isoformat(timespec="seconds"), "kind": "low-memory", "outcome": "ok"})
    clock.advance(60)
    before = len(panel.writes)
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_ROLLED_BACK
    assert ("PUT", "2.1") not in panel.sequence()[before:] and panel.moon_version() == "2.0"


def test_dry_runs_cannot_push_the_unfinished_swap_out_of_the_history(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    panel.put_results = ["interrupt"]
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_INTERRUPTED
    for _ in range(ss.REPORT_HISTORY_LIMIT + 5):
        clock.advance(2)
        make_swap(tmp_path, panel, clock, dry_run=True).run()
    assert len(report(tmp_path)["history"]) == ss.REPORT_HISTORY_LIMIT
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_OK
    assert panel.moon_version() == "2.1"


def test_trim_history_drops_write_free_entries_first() -> None:
    swap = {"mode": "swap", "target": "moon", "writes": 3, "id": "anchor"}
    history = [swap] + [{"mode": "dry-run", "target": "moon", "writes": 0, "id": str(i)} for i in range(10)]
    kept = ss.trim_history(history, 4)
    assert len(kept) == 4 and kept[0]["id"] == "anchor" and kept[-1]["id"] == "9"


# -- rollback room (major)


def test_the_rollback_goes_in_below_the_install_ratio_and_compiles_at_boot(tmp_path: Path) -> None:
    """Room between max(3.0x, 30 KB) and 3.5x: the 2.0 PUT replies out of memory,
    the source is stored, and the verification boot compiles it."""
    new, old = sizes()
    clock = FakeClock()
    block = old.rollback_block + 100
    assert block < old.required_block < new.required_block
    panel = FakePanel(clock, block_without=block)
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_ROLLED_BACK
    assert panel.sequence() == [("DELETE", None), ("POST", None), ("PUT", "2.0"), ("POST", None)]
    assert panel.moon_version() == "2.0" and panel.errors["moon"] is None
    entry = last_entry(tmp_path)
    assert [step["kind"] for step in entry["steps"] if step["step"] == "put-result"] == ["oom"]
    # The three-script block is measured and kept.
    assert entry["block_without_target"][0]["block"] == block
    assert entry["block_without_target"][0]["scripts"] == list(OTHERS)


class DecayingPanel(FakePanel):
    """The block wears down with every read after a boot (senders pushing)."""

    def device(self) -> dict:
        info = super().device()
        if self.restored and "moon" not in self.stored:
            self.block -= 4_000
        return info


def test_the_block_is_read_again_just_before_the_put(tmp_path: Path) -> None:
    new, _old = sizes()
    clock = FakeClock()
    panel = DecayingPanel(clock, block_without=new.required_block + 6_000)
    make_swap(tmp_path, panel, clock).run()
    entry = last_entry(tmp_path)
    skipped = [step for step in entry["steps"] if step["step"] == "put-skipped"]
    assert skipped and skipped[0]["kind"] == "no-room"
    assert ("PUT", "2.1") not in panel.sequence()


# -- rotation membership (major)


class LoopLosingPanel(FakePanel):
    """DELETE drops the script from the saved loop; the re-added script is not in it."""

    def delete(self, url, timeout=None, **kwargs):
        response = super().delete(url, timeout=timeout, **kwargs)
        name = url.rsplit("/", 1)[-1]
        self.loop = {"order": [n for n in self.loop["order"] if n != name], "disabled": self.loop["disabled"]}
        self.dropped = getattr(self, "dropped", set()) | {name}
        return response

    def apps(self):
        apps = super().apps()
        for app in apps:
            if app.get("name") in getattr(self, "dropped", set()):
                app["inLoop"] = False
        return apps


def test_dropping_out_of_the_rotation_needs_a_person_and_is_not_ok(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = LoopLosingPanel(clock)
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_ATTENTION
    entry = last_entry(tmp_path)
    assert entry["outcome"] == ss.NEEDS_ATTENTION and "rollback_reason" not in entry
    assert "dropped out of the rotation" in entry["error"] and "missing from /apploop.json" in entry["error"]
    assert panel.moon_version() == "2.1"  # not rolled back: that DELETE + PUT would do the same
    assert "rotation_policy.py" in entry["next"]


def test_check_only_flags_a_script_out_of_the_rotation(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = LoopLosingPanel(clock)
    make_swap(tmp_path, panel, clock).run()
    assert make_swap(tmp_path, panel, clock, check_only=True, fidelity=False).run() == ss.EXIT_CHECK_FAILED
    assert "not in the rotation" in last_entry(tmp_path)["error"]


def test_check_only_fails_when_the_page_never_comes_on_screen_while_lit(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock, moon=NEW, power=True)  # current app stays "Time"
    assert make_swap(tmp_path, panel, clock, check_only=True).run() == ss.EXIT_CHECK_FAILED
    assert "never came on screen" in last_entry(tmp_path)["error"]
    # Dark, the same is no evidence either way.
    panel.power = False
    assert make_swap(tmp_path, panel, clock, check_only=True).run() == ss.EXIT_OK


# -- preflight (major / minor)


def test_another_script_already_in_error_stops_the_swap_before_any_write(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    panel.errors["ocean"] = "attribute_error: nil"
    panel._boot_log()
    panel._restore()
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_ABORTED
    assert panel.writes == [] and "ocean" in last_entry(tmp_path)["error"]


def test_installed_bytes_differing_from_the_rollback_source_stop_before_the_delete(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    panel.stored["moon"] = minify(OLD).replace("var v0 = 0", "var v0 = 7")  # same @version 2.0
    panel.block_with = 35_000  # the snapshot can read moon back
    panel._boot_log()
    panel._restore()
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_ABORTED
    assert panel.writes == [] and "not byte-identical" in last_entry(tmp_path)["error"]


def test_sha256_pins_refuse_other_bytes(tmp_path: Path) -> None:
    new, old = sizes()
    clock = FakeClock()
    panel = FakePanel(clock)
    assert make_swap(tmp_path, panel, clock, expect_sha256="0" * 16).run() == ss.EXIT_ABORTED
    assert "--expect-sha256" in last_entry(tmp_path)["error"] and panel.writes == []
    assert make_swap(tmp_path, panel, clock, rollback_sha256=new.sha256[:16]).run() == ss.EXIT_ABORTED
    assert make_swap(tmp_path, panel, clock, expect_sha256="abc").run() == ss.EXIT_ABORTED
    assert panel.writes == []
    assert make_swap(tmp_path, panel, clock, expect_sha256=new.sha256[:12],
                     rollback_sha256=old.sha256).run() == ss.EXIT_OK


def test_a_boot_log_without_ntp_stops_the_swap_before_any_write(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    panel.log = [line for line in panel.log if "ntp:" not in line]
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_ABORTED
    assert panel.writes == [] and "'ntp:' line" in last_entry(tmp_path)["error"]
    # A log that no longer reaches back to the boot is not taken as "no NTP".
    assert ss.ntp_state({"lines": ["10:00:00 sunset: ok"]}).startswith("unknown")


# -- restore signals (minor)


def test_an_unreadable_log_after_the_boot_falls_back_to_the_inventory(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    original_get, original_delete = panel.get, panel.delete
    state = {"deleted": False}

    def get(url, timeout=None, **kwargs):
        if state["deleted"] and url.endswith("/api/v1/logs"):
            return FakeResponse(503, body={"error": "busy"})
        return original_get(url, timeout=timeout, **kwargs)

    def delete(url, timeout=None, **kwargs):
        state["deleted"] = True
        return original_delete(url, timeout=timeout, **kwargs)

    panel.get, panel.delete = get, delete
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_OK
    assert panel.moon_version() == "2.1"
    booted = [step for step in last_entry(tmp_path)["steps"] if step["step"] == "booted"]
    assert [step["restored_via"] for step in booted] == ["apps", "apps"]
    assert all(step["uptime"] >= ss.RESTORE_FALLBACK_UPTIME for step in booted)


def test_ntp_stalling_after_the_delete_ends_without_spending_more_reboots(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    original_delete = panel.delete

    def delete(url, timeout=None, **kwargs):
        response = original_delete(url, timeout=timeout, **kwargs)
        panel.ntp = False  # the internet goes away with the reboot
        return response

    panel.delete = delete
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_FAILED
    assert panel.sequence() == [("DELETE", None), ("POST", None)]
    assert "NTP or restore stalled" in last_entry(tmp_path)["error"]
    # NTP comes back: the same command resumes the rollback.
    panel.delete = original_delete
    panel.ntp = True
    panel.reboot_now()
    clock.advance(60)
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_ROLLED_BACK
    assert panel.moon_version() == "2.0"


def test_a_read_blip_after_the_delete_does_not_start_a_rollback(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    original_get, original_delete = panel.get, panel.delete
    state = {"fail": 0}

    def delete(url, timeout=None, **kwargs):
        response = original_delete(url, timeout=timeout, **kwargs)
        state["fail"] = 3
        return response

    def get(url, timeout=None, **kwargs):
        if state["fail"] > 0:
            state["fail"] -= 1
            raise requests.ConnectionError("blip")
        return original_get(url, timeout=timeout, **kwargs)

    panel.get, panel.delete = get, delete
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_OK
    assert panel.sequence() == [("DELETE", None), ("POST", None), ("PUT", "2.1"), ("POST", None)]
    assert "rollback_reason" not in last_entry(tmp_path)


# -- gate and concurrency (minor)


def test_a_real_swap_refuses_to_write_ungated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    monkeypatch.setenv(panel_gate.ENABLE_ENV, "0")
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_ABORTED
    assert panel.writes == [] and "--allow-ungated" in last_entry(tmp_path)["error"]
    # A dry run only says so.
    assert make_swap(tmp_path, panel, clock, dry_run=True).run() == ss.EXIT_OK
    assert any("ungated" in w for w in last_entry(tmp_path)["warnings"])
    assert make_swap(tmp_path, panel, clock, allow_ungated=True).run() == ss.EXIT_OK
    assert any("--allow-ungated was given" in w for w in last_entry(tmp_path)["warnings"])


def test_a_real_swap_refuses_while_the_gate_is_switched_off(tmp_path: Path, panel_gate_dir: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    (panel_gate_dir / panel_gate.OFF_NAME).write_text("", encoding="utf-8")
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_ABORTED
    assert panel.writes == [] and panel_gate.OFF_NAME in last_entry(tmp_path)["error"]


def test_only_one_swap_runs_at_a_time(tmp_path: Path) -> None:
    import os

    clock = FakeClock()
    panel = FakePanel(clock)
    lock = tmp_path / "data" / ss.LOCK_NAME
    lock.parent.mkdir(parents=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT)
    panel_gate._try_lock(fd)
    try:
        assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_ABORTED
        assert panel.writes == []
        # The other run's report is left alone.
        assert not (tmp_path / "data" / ss.REPORT_NAME).exists()
        # A dry run beside it still plans, but does not rewrite the report under it.
        assert make_swap(tmp_path, panel, clock, dry_run=True).run() == ss.EXIT_OK
        assert not (tmp_path / "data" / ss.REPORT_NAME).exists()
    finally:
        panel_gate._unlock(fd)
        os.close(fd)
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_OK


def test_a_dry_run_without_data_dir_writes_no_file(tmp_path: Path, capsys) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    new_path, old_path = source_files(tmp_path)
    try:
        code = ss.main(["moon", str(new_path), "--expect-version", "2.1", "--rollback", str(old_path),
                        "--rollback-version", "2.0", "--dry-run", "--panel", BASE,
                        "--maintenance-report", str(tmp_path / "none.json")],
                       session=panel, clock=clock, hub_maintenance=ss.HubMaintenance())
    finally:
        logger = logging.getLogger("awtrix.script_swap")
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
    assert code == ss.EXIT_OK and panel.writes == []
    assert "Plan for moon" in capsys.readouterr().out
    live = tmp_path / "localappdata" / "AWTRIX"
    assert not live.exists() or not any(live.iterdir())


# -- the Hub's low-memory restart (minor)


def low_memory_panel(clock: FakeClock) -> FakePanel:
    panel = FakePanel(clock, uptime=15 * 3600)
    panel.min_free = 7_392
    return panel


def test_a_due_low_memory_restart_is_refused_without_the_wait_flag(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = low_memory_panel(clock)
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_ABORTED
    assert panel.writes == [] and "low-memory restart is due" in last_entry(tmp_path)["error"]


def test_the_wait_flag_waits_out_a_due_low_memory_restart(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = low_memory_panel(clock)
    started = {"at": "2026-10-09T21:51:00", "kind": "low-memory", "outcome": "in-progress"}

    def hub_restarts() -> None:
        write_maintenance(tmp_path, started)
        panel.reboot_now("software")

    clock.at_wall(datetime(2026, 10, 9, 21, 51), hub_restarts)
    clock.at_wall(datetime(2026, 10, 9, 21, 53), lambda: write_maintenance(tmp_path, {**started, "outcome": "ok"}))
    assert make_swap(tmp_path, panel, clock, wait_for_nightly=True).run() == ss.EXIT_OK
    assert panel.writes[0][3] >= datetime(2026, 10, 9, 21, 53)


def test_a_capped_low_memory_restart_does_not_hold_the_swap(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = low_memory_panel(clock)
    write_maintenance(tmp_path, *[{"at": f"2026-10-09T{h:02d}:00:00", "kind": "low-memory", "outcome": "ok"}
                                  for h in (3, 9, 12)])
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_OK
    assert any("will not restart the panel for it now" in w for w in last_entry(tmp_path)["warnings"])


def test_a_restart_between_preflight_and_the_delete_is_waited_out(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    swap = make_swap(tmp_path, panel, clock)
    snapshot = swap.snapshot_fn
    restarted = {}

    def snapshot_then_restart(**kwargs):
        destination = snapshot(**kwargs)
        panel.reboot_now("software")  # the Hub, between preflight and the first write
        restarted["boot_at"] = panel.down_until
        return destination

    swap.snapshot_fn = snapshot_then_restart
    assert swap.run() == ss.EXIT_OK
    method, _path, _version, at = panel.writes[0]
    restore_done = clock.wall0 + timedelta(seconds=restarted["boot_at"] + panel.restore_seconds - 1000.0)
    assert method == "DELETE" and at >= restore_done
    assert "first-write-guard" in step_names(last_entry(tmp_path))


# -- the screen check (major)


def lit_frame(nonblack: int, disc: int, digest: str, *, lit: bool, window: bool = False) -> dict:
    return {"lit": lit, "power": lit, "disc_window": window,
            "stats": {"nonblack": nonblack, "disc_nonblack": disc, "hash": digest}}


def test_judge_screen_counts_only_frames_taken_lit() -> None:
    # One lit frame of a healthy moon, then dark black frames (two in the disc window).
    mixed = [lit_frame(80, 40, "lit", lit=True)] + [lit_frame(0, 0, "dark", lit=False, window=i < 2)
                                                     for i in range(5)]
    assert ss.judge_screen(mixed, "moon")["verdict"] == "ok"
    # One lit black frame (the boot-lit moment) and dark black frames: too little to judge.
    boot_lit = [lit_frame(0, 0, "a", lit=True)] + [lit_frame(0, 0, "a", lit=False) for _ in range(3)]
    assert ss.judge_screen(boot_lit, "moon")["verdict"] == "inconclusive"
    # Dark frames that change, with the disc black: no evidence at all.
    changing = [lit_frame(30 + i, 0, str(i), lit=False, window=True) for i in range(4)]
    assert ss.judge_screen(changing, "moon")["verdict"] == "inconclusive"
    # Lit evidence still convicts.
    assert ss.judge_screen([lit_frame(0, 0, "b", lit=True) for _ in range(4)], "moon")["verdict"] == "broken"


def test_a_night_install_treats_the_screen_verdict_as_evidence_only(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock, power=False)
    panel.current_app = rotate_moon
    panel.screen = lambda p: [0] * 256 if p.moon_version() == "2.1" else moon_frame(p.clock)
    original_post = panel.post
    posts = {"n": 0}

    def post(url, timeout=None, **kwargs):
        response = original_post(url, timeout=timeout, **kwargs)
        posts["n"] += 1
        if posts["n"] == 2:
            panel.power = True  # lit while the watch runs
        return response

    panel.post = post
    assert make_swap(tmp_path, panel, clock, require_dark=True).run() == ss.EXIT_OK
    fidelity = last_entry(tmp_path)["fidelity"]
    assert fidelity["screen"]["judgement"]["verdict"] == "broken" and fidelity["broken"] is None
    assert "all black" in fidelity["advisory"] and panel.moon_version() == "2.1"


def test_dark_framebuffer_frames_are_sampled_before_a_night_swap(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock, power=False)
    panel.screen = lambda p: [0] * 256
    assert make_swap(tmp_path, panel, clock, dry_run=True, require_dark=True).run() == ss.EXIT_OK
    dark = last_entry(tmp_path)["before"]["dark_screen"]
    assert len(dark["samples"]) == ss.DARK_SAMPLES and dark["distinct"] == 1
    assert all(sample["power"] is False and sample["nonblack"] == 0 for sample in dark["samples"])


# -- night reboots and boot loops (minor)


def test_a_boot_loop_from_the_new_version_is_rolled_back(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock)
    panel.boot_panics.add(("moon", "2.1"))
    assert make_swap(tmp_path, panel, clock).run() == ss.EXIT_ROLLED_BACK
    assert panel.moon_version() == "2.0" and panel.errors["moon"] is None
    entry = last_entry(tmp_path)
    assert "boot loop" in entry["rollback_reason"]
    assert any(step["step"] == "boot-loop-delete" for step in entry["steps"])


def test_a_person_holding_the_matrix_lit_pauses_the_reboots(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel(clock, power=False)
    original_delete = panel.delete

    def delete(url, timeout=None, **kwargs):
        response = original_delete(url, timeout=timeout, **kwargs)
        panel.power = True  # someone switches the panel on at 21:50
        return response

    panel.delete = delete
    assert make_swap(tmp_path, panel, clock, require_dark=True).run() == ss.EXIT_INTERRUPTED
    entry = last_entry(tmp_path)
    assert entry["outcome"] == ss.PAUSED and panel.sequence() == [("DELETE", None)]
    assert "re-run the same command" in entry["error"]
    panel.delete = original_delete
    panel.power = False
    clock.advance(60)
    assert make_swap(tmp_path, panel, clock, require_dark=True).run() == ss.EXIT_OK
    assert panel.moon_version() == "2.1" and last_entry(tmp_path)["resumed_from"] == entry["id"]
