"""The cross-process panel mutation gate, and every sender's use of it.

The conftest points the gate at a fresh directory per test with no gap, so a
test that wants the gap sets it back. Sender modules are imported inside their
tests: the multiprocess test's workers import this module, and they should not
pay for psutil, Pillow and the art stacks to start.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import errno
import importlib
import inspect
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests

import focus_control
import panel_gate
from panel_client import PanelClient, PanelPolicy, PanelTransportError


ROOT = Path(__file__).resolve().parent
SCHEMA = ["t", "src", "op", "app", "bytes", "status", "ms", "wait_ms", "err", "gate"]


def events(directory: Path, name: str = panel_gate.EVENTS_NAME) -> list[dict]:
    path = directory / name
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def triples(directory: Path) -> list[tuple]:
    return [(line["src"], line["op"], line["app"]) for line in events(directory)]


class FakeTime:
    """One clock for wall and monotonic time; sleeping advances it and runs what falls due."""

    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now
        self.sleeps: list[float] = []
        self.due: list[tuple[float, Callable[[], None]]] = []

    def time(self) -> float:
        return self.now

    def at(self, when: float, action: Callable[[], None]) -> None:
        self.due.append((when, action))
        self.due.sort(key=lambda item: item[0])

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        while self.due and self.due[0][0] <= self.now + 1e-9:
            self.due.pop(0)[1]()


@pytest.fixture
def fake_time(monkeypatch: pytest.MonkeyPatch) -> FakeTime:
    clock = FakeTime()
    monkeypatch.setattr(panel_gate, "_wall", clock.time)
    monkeypatch.setattr(panel_gate, "_monotonic", clock.time)
    monkeypatch.setattr(panel_gate, "_sleep", clock.sleep)
    return clock


def sender(name: str) -> ModuleType:
    """Import a sender the way its own tests do (weather/ ones as top-level)."""
    if name in {"weather_brief_push", "aqi_brief_push"}:
        weather = str(ROOT / "weather")
        if weather not in sys.path:
            sys.path.insert(0, weather)
    return importlib.import_module(name)


def ok(status: int = 200, **attributes) -> Mock:
    return Mock(status_code=status, text="", **attributes)


def hold_lock(directory: Path) -> int:
    """Take the gate on a second handle, as another process would hold it."""
    other = os.open(directory / panel_gate.LOCK_NAME, os.O_RDWR | os.O_CREAT)
    try:
        panel_gate._try_lock(other)
    except OSError:
        os.close(other)
        raise
    return other


def let_go(other: int) -> None:
    panel_gate._unlock(other)
    os.close(other)


def lock_is_held(directory: Path) -> bool:
    """Whether another process would find the gate taken right now."""
    try:
        probe = hold_lock(directory)
    except OSError:
        return True
    let_go(probe)
    return False


def mark_handoff(directory: Path, clock: FakeTime) -> None:
    """What a holder leaves behind as it lets go: the time its write ended."""
    (directory / panel_gate.LAST_NAME).write_text(f"{clock.now:.3f}", encoding="ascii")


# ---------------------------------------------------------------- journal


def test_journal_line_has_exactly_the_documented_schema(panel_gate_dir: Path) -> None:
    before = time.time()
    with panel_gate.mutation("hub", "push", "clock", body={"text": "hi"}) as gate:
        gate.observe(ok(200))

    [line] = events(panel_gate_dir)
    assert list(line) == SCHEMA
    assert (line["src"], line["op"], line["app"]) == ("hub", "push", "clock")
    assert line["bytes"] == len('{"text":"hi"}')
    assert line["status"] == 200
    assert line["err"] is None
    assert line["gate"] == "ok"
    assert isinstance(line["ms"], int) and isinstance(line["wait_ms"], int)
    assert line["t"] == round(line["t"], 1)
    assert before - 0.1 <= line["t"] <= time.time() + 0.1


def test_status_that_is_not_an_http_code_is_journaled_as_null(panel_gate_dir: Path) -> None:
    with panel_gate.mutation("hub", "push", "clock") as gate:
        gate.observe(Mock())  # a bare Mock's status_code is another Mock
    with panel_gate.mutation("hub", "push", "clock") as gate:
        gate.observe(None)

    assert [line["status"] for line in events(panel_gate_dir)] == [None, None]


def test_unknown_op_is_journaled_as_other(panel_gate_dir: Path) -> None:
    with panel_gate.mutation("hub", "frobnicate"):
        pass

    assert events(panel_gate_dir)[0]["op"] == "other"


@pytest.mark.parametrize(
    "body, size",
    [
        (None, None),
        (b"abc", 3),
        (bytearray(b"ab"), 2),
        (memoryview(b"abcd"), 4),
        ("héllo", 6),
        ({"a": [1, 2]}, len('{"a":[1,2]}')),
        ([1, "x"], len('[1,"x"]')),
        ({"bad": {1, 2}}, None),
    ],
)
def test_body_size_counts_what_the_journal_promises(body, size) -> None:
    assert panel_gate.body_size(body) == size


def test_journal_rotates_like_the_health_log(panel_gate_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(panel_gate, "EVENTS_MAX_BYTES", 1)
    for app in ("first", "second", "third"):
        with panel_gate.mutation("hub", "push", app):
            pass

    assert [line["app"] for line in events(panel_gate_dir)] == ["third"]
    assert [line["app"] for line in events(panel_gate_dir, "panel_events.1.jsonl")] == ["second"]


def test_a_gated_write_is_journaled_before_the_lock_is_let_go(
    panel_gate_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two processes appending at once on Windows lose and splice lines; the
    # lock is what keeps gated writers' appends apart.
    held: list[bool] = []
    journal = panel_gate._journal

    def probing(directory: Path, record: panel_gate.MutationRecord) -> None:
        held.append(lock_is_held(directory))
        journal(directory, record)

    monkeypatch.setattr(panel_gate, "_journal", probing)
    with panel_gate.mutation("hub", "push", "outer"):
        with panel_gate.mutation("hub", "delete", "inner"):
            pass

    assert held == [True, True]
    assert triples(panel_gate_dir) == [("hub", "delete", "inner"), ("hub", "push", "outer")]


@pytest.mark.skipif(os.name != "nt", reason="only Windows refuses to rename a file another handle has open")
def test_a_reader_holding_the_journal_open_costs_rotation_not_lines(
    panel_gate_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(panel_gate, "EVENTS_MAX_BYTES", 1)
    with panel_gate.mutation("hub", "push", "first"):
        pass
    # panel_heap_report reads the journal with a plain open().
    with (panel_gate_dir / panel_gate.EVENTS_NAME).open(encoding="utf-8"):
        for app in ("second", "third"):
            with panel_gate.mutation("hub", "push", app):
                pass
    assert [line["app"] for line in events(panel_gate_dir)] == ["first", "second", "third"]
    assert not (panel_gate_dir / "panel_events.1.jsonl").exists()

    with panel_gate.mutation("hub", "push", "fourth"):
        pass
    assert [line["app"] for line in events(panel_gate_dir)] == ["fourth"]
    assert [line["app"] for line in events(panel_gate_dir, "panel_events.1.jsonl")] == [
        "first", "second", "third",
    ]


# ---------------------------------------------------------------- gap


def test_second_write_waits_out_the_gap_since_the_first_ended(
    panel_gate_dir: Path, fake_time: FakeTime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(panel_gate.GAP_ENV, "0.4")
    with panel_gate.mutation("hub", "push", "a"):
        fake_time.now += 0.1
    ended = fake_time.now
    fake_time.now += 0.15
    with panel_gate.mutation("aqi", "push", "b") as second:
        started = fake_time.now

    assert fake_time.sleeps == [pytest.approx(0.25)]
    assert started == pytest.approx(ended + 0.4, abs=1e-3)
    assert second.wait_ms == 250
    first_line, second_line = events(panel_gate_dir)
    assert first_line["ms"] == 100 and first_line["wait_ms"] == 0
    assert second_line["wait_ms"] == 250


def test_a_write_after_the_gap_has_passed_does_not_sleep(
    panel_gate_dir: Path, fake_time: FakeTime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(panel_gate.GAP_ENV, "0.4")
    with panel_gate.mutation("hub", "push", "a"):
        pass
    fake_time.now += 5
    with panel_gate.mutation("hub", "push", "b"):
        pass

    assert fake_time.sleeps == []


@pytest.mark.parametrize(
    "last, sleeps",
    [
        ("9999999999999", [0.4]),  # far future
        ("1800003600.0", [0.4]),  # an hour ahead: clock jumped back
        ("not a time", []),
        ("nan", []),
        ("inf", []),
        ("-inf", []),
        ("", []),
        ("12.5", []),
    ],
)
def test_garbage_or_future_last_write_costs_at_most_one_gap(
    panel_gate_dir: Path, fake_time: FakeTime, monkeypatch: pytest.MonkeyPatch, last: str, sleeps: list
) -> None:
    monkeypatch.setenv(panel_gate.GAP_ENV, "0.4")
    (panel_gate_dir / panel_gate.LAST_NAME).write_text(last, encoding="ascii")
    with panel_gate.mutation("hub", "push", "a") as gate:
        pass

    assert fake_time.sleeps == [pytest.approx(s) for s in sleeps]
    assert gate.gate == "ok"


@pytest.mark.parametrize(
    "value, gap",
    [("0", 0.0), ("0.25", 0.25), ("-1", 0.0), ("60", panel_gate.MAX_GAP_SECONDS), ("nan", 0.4), ("x", 0.4)],
)
def test_gap_setting_is_clamped(monkeypatch: pytest.MonkeyPatch, value: str, gap: float) -> None:
    monkeypatch.setenv(panel_gate.GAP_ENV, value)
    assert panel_gate.gap_seconds() == gap


def test_default_gap_is_the_documented_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(panel_gate.GAP_ENV)
    assert panel_gate.gap_seconds() == panel_gate.MIN_GAP_SECONDS == 0.4


# ---------------------------------------------------------------- failure modes


def test_lock_held_elsewhere_times_out_and_writes_ungated(
    panel_gate_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(panel_gate, "ACQUIRE_TIMEOUT_SECONDS", 0.1)
    other = hold_lock(panel_gate_dir)
    ran = False
    try:
        with panel_gate.mutation("weather", "push", "weatherbrief") as gate:
            ran = True
    finally:
        let_go(other)

    assert ran and gate.gate == "timeout"
    [line] = events(panel_gate_dir)
    assert line["gate"] == "timeout"
    assert 100 <= line["wait_ms"] < 2000
    assert (panel_gate_dir / panel_gate.LAST_NAME).exists()
    with panel_gate.mutation("weather", "push", "weatherbrief") as after:
        pass
    assert after.gate == "ok"


def test_the_write_after_an_ungated_one_still_keeps_the_gap(
    panel_gate_dir: Path, fake_time: FakeTime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(panel_gate.GAP_ENV, "0.4")
    monkeypatch.setattr(panel_gate, "ACQUIRE_TIMEOUT_SECONDS", 0.1)
    other = hold_lock(panel_gate_dir)
    try:
        with panel_gate.mutation("weather", "push", "weatherbrief") as ungated:
            fake_time.now += 0.05
        ended = fake_time.now
    finally:
        let_go(other)
    with panel_gate.mutation("aqi", "push", "aqibrief") as gated:
        started = fake_time.now

    assert (ungated.gate, gated.gate) == ("timeout", "ok")
    assert started == pytest.approx(ended + 0.4, abs=1e-3)


def test_a_waiter_keeps_its_place_while_the_queue_ahead_is_moving(
    panel_gate_dir: Path, fake_time: FakeTime, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With the budget counted from the waiter's own start, the third writer in
    # a stall went ungated while the second was still mid-request.
    monkeypatch.setattr(panel_gate, "ACQUIRE_TIMEOUT_SECONDS", 1.0)
    other = hold_lock(panel_gate_dir)
    start = fake_time.now
    fake_time.at(start + 0.8, lambda: mark_handoff(panel_gate_dir, fake_time))
    fake_time.at(start + 1.6, lambda: let_go(other))
    with panel_gate.mutation("nowplaying", "push", "nowplaying") as gate:
        pass

    assert gate.gate == "ok"
    assert 1600 <= gate.wait_ms < 1700


def test_a_holder_that_overruns_the_budget_sends_the_waiter_in_ungated(
    panel_gate_dir: Path, fake_time: FakeTime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(panel_gate, "ACQUIRE_TIMEOUT_SECONDS", 1.0)
    other = hold_lock(panel_gate_dir)
    start = fake_time.now
    fake_time.at(start + 0.8, lambda: mark_handoff(panel_gate_dir, fake_time))
    try:
        with panel_gate.mutation("nowplaying", "push", "nowplaying") as gate:
            pass
    finally:
        let_go(other)

    assert gate.gate == "timeout"
    assert 1800 <= gate.wait_ms < 1900


def test_even_a_moving_queue_releases_a_waiter_at_the_overall_cap(
    panel_gate_dir: Path, fake_time: FakeTime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(panel_gate, "ACQUIRE_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(panel_gate, "MAX_WAIT_SECONDS", 3.0)
    other = hold_lock(panel_gate_dir)
    start = fake_time.now
    for n in range(1, 20):
        fake_time.at(start + 0.5 * n, lambda: mark_handoff(panel_gate_dir, fake_time))
    try:
        with panel_gate.mutation("hub", "push", "starved") as gate:
            pass
    finally:
        let_go(other)

    assert gate.gate == "timeout"
    assert 3000 <= gate.wait_ms < 3100


def test_acquire_budget_outlasts_the_longest_hold_a_sender_can_make() -> None:
    # requests applies its timeout to the connect and again to each read, and
    # the gap is waited inside the lock: one write can hold the gate for
    # connect + read + gap. A shorter budget lets the waiter in beside it.
    bridge = sender("now_playing_bridge")
    timeouts = [
        sender("now_playing_push").REQUEST_TIMEOUT_SECONDS,
        sender("pc_stats_push").REQUEST_TIMEOUT,
        sender("meridian_push").REQUEST_TIMEOUT,
        sender("rotation_policy").REQUEST_TIMEOUT_SECONDS,
        sender("weather_brief_push").REQUEST_TIMEOUT_SECONDS,
        sender("aqi_brief_push").REQUEST_TIMEOUT_SECONDS,
        inspect.signature(bridge.ensure_script_installed).parameters["timeout"].default,
        inspect.signature(bridge.remove_legacy_pushed_app).parameters["timeout"].default,
        PanelPolicy().request_timeout_seconds,
    ]
    longest_hold = 2 * max(timeouts) + panel_gate.MAX_GAP_SECONDS
    assert panel_gate.ACQUIRE_TIMEOUT_SECONDS > longest_hold
    assert panel_gate.MAX_WAIT_SECONDS >= 2 * panel_gate.ACQUIRE_TIMEOUT_SECONDS


def test_an_interrupt_just_after_acquiring_still_gives_the_gate_back(
    panel_gate_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ctrl+C in a console --live run can land anywhere. A leaked lock made every
    # later write on the thread "nested" and every other process wait it out.
    monkeypatch.setattr(panel_gate._local, "depth", 0, raising=False)
    held_when_read: list[bool] = []

    def interrupted_once() -> float:
        held_when_read.append(lock_is_held(panel_gate_dir))
        if len(held_when_read) == 1:
            raise KeyboardInterrupt
        return time.time()

    with monkeypatch.context() as patched:
        patched.setattr(panel_gate, "_wall", interrupted_once)
        with pytest.raises(KeyboardInterrupt):
            with panel_gate.mutation("scene", "delete", "quake"):
                pytest.fail("the block must not run")

    free = panel_gate._thread_lock.acquire(blocking=False)
    panel_gate._thread_lock.release()  # ours if it was free, the leaked one if not
    assert held_when_read[0], "the interrupt must land after the lock is taken"
    assert free
    assert not lock_is_held(panel_gate_dir)
    with panel_gate.mutation("scene", "delete", "quake") as after:
        pass
    assert after.gate == "ok"


def test_a_thread_holding_the_gate_times_out_another_thread(
    panel_gate_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(panel_gate, "ACQUIRE_TIMEOUT_SECONDS", 0.1)
    inside, release = threading.Event(), threading.Event()

    def hold() -> None:
        with panel_gate.mutation("hub", "push", "holder"):
            inside.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert inside.wait(5)
        with panel_gate.mutation("aqi", "push", "waiter") as waiter:
            pass
    finally:
        release.set()
        holder.join(5)

    assert waiter.gate == "timeout"
    assert {line["app"]: line["gate"] for line in events(panel_gate_dir)} == {
        "waiter": "timeout",
        "holder": "ok",
    }


def test_missing_or_unwritable_directory_writes_ungated_without_raising(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x", encoding="ascii")
    for directory in (tmp_path / "missing", not_a_dir, not_a_dir / "below"):
        ran = False
        with panel_gate.mutation("hub", "push", "a", data_dir=directory) as gate:
            ran = True
        assert ran and gate.gate == "unavailable"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["file"]


def test_a_lock_error_that_is_not_contention_is_unavailable_and_still_journaled(
    panel_gate_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(fd: int) -> None:
        raise OSError(errno.EIO, "I/O error")

    with monkeypatch.context() as patched:
        patched.setattr(panel_gate, "_try_lock", broken)
        with panel_gate.mutation("hub", "reboot") as gate:
            pass
    assert gate.gate == "unavailable"
    assert events(panel_gate_dir)[0]["gate"] == "unavailable"
    with panel_gate.mutation("hub", "reboot") as after:
        pass
    assert after.gate == "ok"  # the in-process lock was given back


def test_caller_exception_propagates_unchanged_and_is_journaled(panel_gate_dir: Path) -> None:
    error = requests.ConnectionError("down")
    with pytest.raises(requests.ConnectionError) as caught:
        with panel_gate.mutation("weather", "push", "weatherbrief", body=b"12345"):
            raise error

    assert caught.value is error
    [line] = events(panel_gate_dir)
    assert (line["err"], line["status"], line["bytes"], line["gate"]) == ("ConnectionError", None, 5, "ok")
    assert (panel_gate_dir / panel_gate.LAST_NAME).exists()
    with panel_gate.mutation("weather", "push", "weatherbrief") as after:
        pass
    assert after.gate == "ok"


def test_nested_call_on_the_same_thread_does_not_wait_on_itself(panel_gate_dir: Path) -> None:
    started = time.monotonic()
    with panel_gate.mutation("hub", "push", "outer") as outer:
        with panel_gate.mutation("hub", "delete", "inner") as inner:
            pass

    assert time.monotonic() - started < 2
    assert (outer.gate, inner.gate) == ("ok", "nested")
    assert [(line["app"], line["gate"]) for line in events(panel_gate_dir)] == [
        ("inner", "nested"),
        ("outer", "ok"),
    ]
    with panel_gate.mutation("hub", "push", "after") as after:
        pass
    assert after.gate == "ok"


def test_nesting_depth_unwinds_after_an_exception(panel_gate_dir: Path) -> None:
    with pytest.raises(ValueError):
        with panel_gate.mutation("hub", "push", "outer"):
            with panel_gate.mutation("hub", "push", "inner"):
                raise ValueError("boom")
    with panel_gate.mutation("hub", "push", "after") as after:
        pass

    assert after.gate == "ok"
    assert [line["err"] for line in events(panel_gate_dir)] == ["ValueError", "ValueError", None]


def test_disabled_gate_neither_locks_nor_journals(panel_gate_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(panel_gate.ENABLE_ENV, "0")
    with panel_gate.mutation("hub", "push", "a") as gate:
        pass
    with pytest.raises(KeyError):
        with panel_gate.mutation("hub", "push", "a"):
            raise KeyError("still propagates")

    assert gate.gate == "off"
    assert list(panel_gate_dir.iterdir()) == []


def test_off_file_stops_locking_without_a_restart_but_keeps_the_journal(
    panel_gate_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(panel_gate, "ACQUIRE_TIMEOUT_SECONDS", 0.2)
    other = hold_lock(panel_gate_dir)
    try:
        (panel_gate_dir / panel_gate.OFF_NAME).touch()
        with panel_gate.mutation("hub", "push", "a") as gate:
            gate.observe(ok())
    finally:
        let_go(other)

    assert gate.gate == "off" and gate.wait_ms < 200
    assert triples(panel_gate_dir) == [("hub", "push", "a")]
    assert events(panel_gate_dir)[0]["gate"] == "off"

    (panel_gate_dir / panel_gate.OFF_NAME).unlink()
    with panel_gate.mutation("hub", "push", "b") as gate:
        pass
    assert gate.gate == "ok"


def test_directory_comes_from_argument_then_environment_then_awtrix_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, panel_gate_dir: Path
) -> None:
    assert panel_gate.gate_dir(tmp_path) == tmp_path
    assert panel_gate.gate_dir() == panel_gate_dir
    monkeypatch.delenv(panel_gate.DIR_ENV)
    assert panel_gate.gate_dir() == focus_control.default_data_dir()


# ---------------------------------------------------------------- the live directory


PROBE = """\
import sys
sys.path.insert(0, {root!r})
import unittest  # a live sender may load it through some library; that alone is not a test run
import panel_gate

gates = []
for _ in range(2):
    with panel_gate.mutation("probe", "push", "probe") as gate:
        gates.append(gate.gate)
print(" ".join(gates))
"""


def run_probe(tmp_path: Path, script_name: str, **env: str) -> tuple[str, Path]:
    """Run the probe as a script with no gate settings and a stand-in LOCALAPPDATA."""
    live = tmp_path / "LocalAppData" / "AWTRIX"
    live.mkdir(parents=True)
    script = tmp_path / script_name
    script.write_text(PROBE.format(root=str(ROOT)), encoding="utf-8")
    settings = {panel_gate.DIR_ENV, panel_gate.GAP_ENV, panel_gate.ENABLE_ENV}
    environment = {key: value for key, value in os.environ.items() if key not in settings}
    environment.update(env, LOCALAPPDATA=str(live.parent))
    done = subprocess.run(
        [sys.executable, str(script)], env=environment, capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip(), live


def test_a_test_file_run_directly_never_touches_the_live_directory(tmp_path: Path) -> None:
    # `python test_pc_stats_push.py` skips the conftest; it used to lock, wait
    # and journal fake writes in the live senders' directory.
    gates, live = run_probe(tmp_path, "test_probe.py")

    assert gates == "off off"
    assert list(live.iterdir()) == []


def test_a_live_sender_still_uses_the_live_directory(tmp_path: Path) -> None:
    gates, live = run_probe(tmp_path, "sender_probe.py", **{panel_gate.GAP_ENV: "0"})

    assert gates == "ok ok"
    assert triples(live) == [("probe", "push", "probe")] * 2


def test_a_pytest_test_that_drops_the_directory_setting_is_kept_out_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "AWTRIX"
    live.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv(panel_gate.DIR_ENV)
    with panel_gate.mutation("hub", "push", "clock") as gate:
        pass

    assert gate.gate == "off"
    assert list(live.iterdir()) == []


def _main(file: str | None = None, spec: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(__file__=file, __spec__=SimpleNamespace(name=spec) if spec else None)


@pytest.mark.parametrize(
    "modules, expected",
    [
        ({"pytest": ModuleType("pytest"), "__main__": _main("utility_hub.py")}, True),
        ({"unittest": ModuleType("unittest"), "__main__": _main(r"C:\awtrix\test_pc_stats_push.py")}, True),
        ({"unittest": ModuleType("unittest"), "__main__": _main("__main__.py", "unittest.__main__")}, True),
        ({"unittest": ModuleType("unittest"), "__main__": _main(r"C:\awtrix\utility_hub.py")}, False),
        ({"__main__": _main(r"C:\awtrix\weather\aqi_brief_push.py", "aqi_brief_push")}, False),
        ({"__main__": _main()}, False),  # python -c
        ({}, False),
    ],
)
def test_a_test_run_is_recognised_by_its_runner_not_its_imports(modules: dict, expected: bool) -> None:
    assert panel_gate._running_tests(modules) is expected


# ---------------------------------------------------------------- serialization


def test_threads_in_one_process_take_turns(panel_gate_dir: Path) -> None:
    spans: list[tuple[float, float]] = []

    def work() -> None:
        for _ in range(5):
            with panel_gate.mutation("hub", "push", threading.current_thread().name):
                entered = time.perf_counter()
                time.sleep(0.003)
                spans.append((entered, time.perf_counter()))

    threads = [threading.Thread(target=work, name=f"t{n}") for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    spans.sort()
    assert len(spans) == 20
    assert all(later[0] >= earlier[1] for earlier, later in zip(spans, spans[1:]))
    assert {line["gate"] for line in events(panel_gate_dir)} == {"ok"}


CHILD_GAP_SECONDS = 0.02


def _gate_in_child(directory: str, out: str, sections: int, barrier) -> None:
    os.environ[panel_gate.GAP_ENV] = str(CHILD_GAP_SECONDS)
    barrier.wait(30)
    spans = []
    for _ in range(sections):
        with panel_gate.mutation("proc", "push", str(os.getpid()), data_dir=directory):
            entered = time.time()
            time.sleep(0.02)
            spans.append(f"{entered!r} {time.time()!r}\n")
    Path(out).write_text("".join(spans), encoding="ascii")


def test_real_processes_never_overlap_and_keep_the_gap(panel_gate_dir: Path, tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    outputs = [tmp_path / f"worker{n}.txt" for n in range(3)]
    workers = [
        context.Process(target=_gate_in_child, args=(str(panel_gate_dir), str(out), 4, barrier))
        for out in outputs
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(30)
    assert [worker.exitcode for worker in workers] == [0, 0, 0]

    spans = sorted(
        tuple(float(value) for value in line.split())
        for out in outputs
        for line in out.read_text(encoding="ascii").splitlines()
    )
    assert len(spans) == 12
    for earlier, later in zip(spans, spans[1:]):
        assert later[0] >= earlier[1], "two processes were inside the gate at once"
        assert later[0] - earlier[1] >= CHILD_GAP_SECONDS - 0.005
    lines = events(panel_gate_dir)
    assert len(lines) == 12
    assert {line["gate"] for line in lines} == {"ok"}
    assert len({line["app"] for line in lines}) == 3


def _die_holding_the_gate(directory: str) -> None:
    with panel_gate.mutation("proc", "push", "doomed", data_dir=directory):
        os._exit(3)


def test_a_process_that_dies_holding_the_gate_does_not_wedge_the_rest(
    panel_gate_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = multiprocessing.get_context("spawn").Process(
        target=_die_holding_the_gate, args=(str(panel_gate_dir),)
    )
    child.start()
    child.join(30)
    assert child.exitcode == 3
    assert (panel_gate_dir / panel_gate.LOCK_NAME).exists()

    monkeypatch.setattr(panel_gate, "ACQUIRE_TIMEOUT_SECONDS", 5.0)
    with panel_gate.mutation("hub", "push", "survivor") as gate:
        pass

    assert gate.gate == "ok" and gate.wait_ms < 1000
    assert triples(panel_gate_dir) == [("hub", "push", "survivor")]


# ---------------------------------------------------------------- classify


@pytest.mark.parametrize(
    "method, url, body, expected",
    [
        ("put", "http://p/api/v1/apps/pushed/focus", {"text": "x"}, ("push", "focus")),
        ("post", "/api/v1/apps/pushed/next%20up", None, ("push", "next up")),
        ("put", "http://p/api/v1/apps/script/nowplaying", None, ("install", "nowplaying")),
        ("delete", "http://p/api/v1/apps/pcstats0", None, ("delete", "pcstats0")),
        ("DELETE", "http://p/api/v1/apps/pushed/old", None, ("delete", "old")),
        ("put", "http://p/api/v1/apps/active", {"name": "aqibrief", "fast": False}, ("activate", "aqibrief")),
        ("put", "http://p/api/v1/apps/active", None, ("activate", None)),
        ("post", "http://p/api/v1/device/reboot", None, ("reboot", None)),
        ("patch", "http://p/api/v1/settings", {"brightness": 4}, ("settings", None)),
        ("patch", "http://p/api/v1/display", {"power": False}, ("display", None)),
        ("put", "http://p/api/v1/apps/order", {}, ("other", None)),
    ],
)
def test_classify_reads_op_and_app_from_the_request(method, url, body, expected) -> None:
    assert panel_gate.classify(method, url, body) == expected


# ---------------------------------------------------------------- senders


def test_hub_client_journals_push_activate_delete_and_reboot(panel_gate_dir: Path) -> None:
    session = Mock()
    session.put.return_value = ok(200)
    session.post.return_value = ok(200)
    session.delete.return_value = ok(200)
    session.get.return_value = ok(200, json=Mock(return_value=[
        {"name": "focus", "present": True, "origin": "pushed"},
    ]))
    client = PanelClient(
        "http://panel.local/",
        session=session,
        policy=PanelPolicy(
            minimum_mutation_interval_seconds=0,
            activation_cooldown_seconds=0,
            quiet_start=None,
            quiet_end=None,
        ),
    )

    client.push_app("focus", {"text": "25:00"})
    client.activate_app("focus")
    client.delete_pushed_app("focus")
    client.reboot()

    assert client.source == "hub"
    assert triples(panel_gate_dir) == [
        ("hub", "push", "focus"),
        ("hub", "activate", "focus"),
        ("hub", "delete", "focus"),
        ("hub", "reboot", None),
    ]
    lines = events(panel_gate_dir)
    assert lines[0]["bytes"] == len('{"text":"25:00"}')
    assert [line["status"] for line in lines] == [200, 200, 200, 200]


def test_hub_client_transport_failure_is_journaled_and_still_mapped(panel_gate_dir: Path) -> None:
    session = Mock()
    session.put.side_effect = requests.ConnectTimeout("slow")
    client = PanelClient("http://panel.local", session=session, source="scene")

    with pytest.raises(PanelTransportError):
        client.push_app("quake", {"text": "M3"})

    [line] = events(panel_gate_dir)
    assert (line["src"], line["op"], line["app"], line["err"]) == ("scene", "push", "quake", "ConnectTimeout")


def test_release_canary_names_itself_in_the_journal() -> None:
    canary = importlib.import_module("release_canary")
    assert canary._live_panel_client("http://panel.local", Mock()).source == "canary"


def test_now_playing_push_activate_and_delete_are_journaled(panel_gate_dir: Path) -> None:
    app = sender("now_playing_push")
    with patch.object(app.requests, "put", return_value=ok(200)), \
            patch.object(app.requests, "delete", return_value=ok(404)):
        assert app.push_payload({"text": "Song"}) is None
        assert app.activate_app() is None
        assert app.delete_app() is None
    with patch.object(app.requests, "put", side_effect=requests.ConnectionError("down")):
        assert app.push_payload({"text": "Song"}) == "push unreachable: ConnectionError"

    assert triples(panel_gate_dir) == [
        ("nowplaying", "push", "nowplaying"),
        ("nowplaying", "activate", "nowplaying"),
        ("nowplaying", "delete", "nowplaying"),
        ("nowplaying", "push", "nowplaying"),
    ]
    assert [(line["status"], line["err"]) for line in events(panel_gate_dir)] == [
        (200, None), (200, None), (404, None), (None, "ConnectionError"),
    ]


def test_now_playing_bridge_install_and_legacy_delete_are_journaled(panel_gate_dir: Path) -> None:
    bridge = sender("now_playing_bridge")
    source = 'self.url = "http://192.168.1.42:8765/nowplaying"\n'
    session = Mock()
    session.get.return_value = ok(404)
    session.put.return_value = ok(200, json=Mock(return_value={"error": None}))
    assert bridge.ensure_script_installed("http://panel", "nowplaying", source, session=session)

    session = Mock()
    session.get.return_value = ok(200, json=Mock(return_value=[
        {"name": "nowplaying", "present": True, "origin": "pushed"},
    ]))
    session.delete.return_value = ok(200)
    assert bridge.remove_legacy_pushed_app("http://panel", "nowplaying", session=session)

    assert triples(panel_gate_dir) == [
        ("nowplaying", "install", "nowplaying"),
        ("nowplaying", "delete", "nowplaying"),
    ]
    assert events(panel_gate_dir)[0]["bytes"] == len(source.encode("utf-8"))


def test_pc_stats_cleanup_push_and_activation_are_journaled(panel_gate_dir: Path) -> None:
    app = sender("pc_stats_push")
    with patch.object(app.requests, "delete", return_value=ok(404)), \
            patch.object(app.requests, "put", return_value=ok(200)):
        state, error = app.sync_panel([("CPU", 92)], None)
        assert app.activate_app() is None
    assert (state, error) == (("CPU",), None)

    assert triples(panel_gate_dir) == [
        ("pcstats", "delete", "pcstats"),
        ("pcstats", "delete", "pcstats1"),
        ("pcstats", "delete", "pcstats0"),
        ("pcstats", "push", "pcstats0"),
        ("pcstats", "activate", "pcstats0"),
    ]
    assert events(panel_gate_dir)[3]["bytes"] > 0


def test_weather_brief_push_is_journaled(panel_gate_dir: Path) -> None:
    app = sender("weather_brief_push")
    session = Mock()
    session.put.return_value = ok(200)
    app.push_payload(session, "http://panel/", {"text": "72"})

    assert triples(panel_gate_dir) == [("weather", "push", "weatherbrief")]
    assert events(panel_gate_dir)[0]["bytes"] == len('{"text":"72"}')


def test_aqi_brief_push_delete_and_activation_are_journaled(panel_gate_dir: Path) -> None:
    app = sender("aqi_brief_push")
    session = Mock()
    session.put.return_value = ok(200)
    session.delete.return_value = ok(200)
    app.push_payload(session, "http://panel/", {"text": "42"})
    app.delete_app(session, "http://panel/")
    app.activate_app(session, "http://panel/")

    assert triples(panel_gate_dir) == [
        ("aqi", "push", "aqibrief"),
        ("aqi", "delete", "aqibrief"),
        ("aqi", "activate", "aqibrief"),
    ]


def test_meridian_push_and_quiet_delete_are_journaled(panel_gate_dir: Path) -> None:
    app = sender("meridian_push")
    lit = ok(200, json=Mock(return_value={"matrixPower": True}))
    dark = ok(200, json=Mock(return_value={"matrixPower": False}))
    with patch.object(app.requests, "put", return_value=ok(200)), \
            patch.object(app.requests, "delete", return_value=ok(200)):
        with patch.object(app.requests, "get", return_value=lit):
            assert app.sync_panel(datetime(2026, 8, 22, 12, 30), False) == (True, None)
        with patch.object(app.requests, "get", return_value=dark):
            assert app.sync_panel(datetime(2026, 8, 22, 22, 30), True) == (False, None)

    assert triples(panel_gate_dir) == [
        ("meridian", "push", "meridian"),
        ("meridian", "delete", "meridian"),
    ]


def test_rotation_policy_writes_are_journaled(panel_gate_dir: Path) -> None:
    policy = sender("rotation_policy")
    session = Mock()
    session.patch.return_value = ok(200)
    session.put.return_value = ok(200)
    policy.apply_rotation_settings(session, base_url="http://panel/")
    policy.apply_state(session, base_url="http://panel/")

    assert triples(panel_gate_dir) == [
        ("rotation", "settings", None),
        ("rotation", "other", None),
    ]
