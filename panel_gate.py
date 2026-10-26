"""One queue for every process that writes to the panel, and a record of each write.

Five scheduled processes push to the same ESP32 -- the Utility Hub, Now
Playing, PC stats, the weather brief and the AQI brief -- and console tools
(``meridian_push``, ``panel_scene``, ``release_canary``) join them when run.
:class:`PanelClient`'s rate limit only serializes threads inside one of them.
Nothing stopped two senders landing a PUT on the panel in the same instant, and
the panel's largest free block, which is what decides whether a push survives,
decays from ~32 KB after boot to ~18 KB within half an hour. It has panicked
once during a push.

So every mutation goes through :func:`mutation`: an OS file lock shared by all
processes, a short gap since the previous write ended, and one journal line in
``panel_events.jsonl`` saying who wrote what, how big, and how it went. Lined up
against ``panel_health.jsonl`` (one heap sample per 30 s; a boot is where
``uptime`` goes backwards) that is what lets a heap drop be pinned on a sender.

Two writers on this PC bypass the gate and are not journaled.
``nightkeeper_recover.ps1`` is the panic button and must not queue behind
anything. Miku-JARVIS (a separate project, ``miku-jarvis/matrix/backends/
awtrix.py``; its scheduled task is disabled) PUTs a waveform up to ~6 times a
second, which a 0.4 s gap would break. While Miku runs, a heap drop next to a
journaled write is not evidence against that sender.

The gate must never cost a sender its job. Anything wrong with the lock or the
directory means the write goes ahead ungated and the journal says so; journal
failures are swallowed. A process that dies holding the lock loses it with its
file handle -- the OS releases it, so there is no stale-lock cleanup to get
wrong. ``AWTRIX_PANEL_GATE=0`` turns the whole thing off;
creating ``panel_gate.off`` in the gate directory stops the locking and the
gap in every running sender at its next write, without a restart, and keeps
the journal (gate ``off``) so the effect of turning it off can still be seen.
``AWTRIX_PANEL_GATE_DIR`` and ``AWTRIX_PANEL_GATE_GAP`` move its files and
change the gap, which is how the test suite keeps out of the live directory. A
test run that names no directory -- ``python test_x.py`` skips the conftest --
gets the gate switched off rather than the live senders' lock and journal.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
import errno
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any
from urllib.parse import unquote, urlsplit

from focus_control import default_data_dir

if os.name == "nt":
    import msvcrt

    def _try_lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


ENABLE_ENV = "AWTRIX_PANEL_GATE"
DIR_ENV = "AWTRIX_PANEL_GATE_DIR"
GAP_ENV = "AWTRIX_PANEL_GATE_GAP"
LOCK_NAME = "panel_mutation.lock"
LAST_NAME = "panel_mutation.last"
EVENTS_NAME = "panel_events.jsonl"
OFF_NAME = "panel_gate.off"
# ~140 bytes a line, so one file holds ~7,000 writes and the rotated file the
# ~7,000 before them.
EVENTS_MAX_BYTES = 1_000_000
# How long one holder may keep a waiter out. requests applies a sender's
# timeout (10 s at most) to the connect and again to each read, and the gap is
# waited inside the lock, so one write can hold the gate for connect + read +
# gap = 22 s. A panel that trickles its reply can hold it longer still; the
# waiter then goes in ungated and the journal says "timeout".
ACQUIRE_TIMEOUT_SECONDS = 25.0
# The budget above restarts whenever a write ends, so a waiter behind a moving
# queue keeps its place. This bounds the whole wait, so a waiter behind a queue
# that never empties still gets to write.
MAX_WAIT_SECONDS = 60.0
MIN_GAP_SECONDS = 0.4
MAX_GAP_SECONDS = 2.0
POLL_SECONDS = 0.025

OPS = frozenset({"push", "delete", "install", "activate", "reboot", "settings", "display", "other"})
# Windows reports a held byte-range lock as EACCES; flock says EWOULDBLOCK.
_CONTENDED = frozenset({errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK})

# Captured at import, not looked up through ``time``: sender tests patch
# time.sleep/time.monotonic globally with finite side effects, and the gate must
# neither consume those nor stall on them. Gate tests inject fakes here.
_wall = time.time
_monotonic = time.monotonic
_sleep = time.sleep

_thread_lock = threading.Lock()
_local = threading.local()


@dataclass
class MutationRecord:
    """What the caller's block reports back; the gate fills in the rest."""

    source: str
    op: str
    app: str | None
    size: int | None
    status: Any = None
    gate: str = "ok"
    wait_ms: int = 0
    started: float = 0.0
    ms: int | None = None
    err: str | None = None

    def observe(self, response: Any) -> None:
        """Note the panel's status code; tolerant of the stand-ins tests return."""
        self.status = getattr(response, "status_code", None)

    def line(self) -> dict[str, Any]:
        status = self.status
        return {
            "t": round(self.started, 1),
            "src": self.source,
            "op": self.op,
            "app": self.app,
            "bytes": self.size,
            "status": status if isinstance(status, int) and not isinstance(status, bool) else None,
            "ms": self.ms,
            "wait_ms": self.wait_ms,
            "err": self.err,
            "gate": self.gate,
        }


def enabled() -> bool:
    return os.environ.get(ENABLE_ENV, "").strip().lower() not in {"0", "false", "off", "no"}


def gate_dir(data_dir: Path | str | None = None) -> Path:
    if data_dir is not None:
        return Path(data_dir)
    configured = os.environ.get(DIR_ENV, "").strip()
    return Path(configured) if configured else default_data_dir()


def _running_tests(modules: Mapping[str, Any] | None = None) -> bool:
    """Whether this process is a test run, judged by its runner.

    Not by ``unittest`` being imported: a library a live sender loads may
    import it, and a false positive here would switch the live gate off.
    """
    modules = sys.modules if modules is None else modules
    if "pytest" in modules:
        return True
    main = modules.get("__main__")
    if getattr(getattr(main, "__spec__", None), "name", None) == "unittest.__main__":
        return True
    return Path(getattr(main, "__file__", None) or "").name.startswith("test_")


def _defaults_to_live_dir_under_test(data_dir: Path | str | None) -> bool:
    from runtime_paths import current_data_dir
    return (data_dir is None and current_data_dir() is None
            and not os.environ.get(DIR_ENV, "").strip() and _running_tests())


def gap_seconds() -> float:
    try:
        gap = float(os.environ.get(GAP_ENV, MIN_GAP_SECONDS))
    except ValueError:
        gap = MIN_GAP_SECONDS
    if not math.isfinite(gap):
        gap = MIN_GAP_SECONDS
    return min(max(gap, 0.0), MAX_GAP_SECONDS)


def body_size(body: Any) -> int | None:
    """Request body size as the journal counts it; None when there is no body."""
    if body is None:
        return None
    if isinstance(body, (bytes, bytearray)):
        return len(body)
    if isinstance(body, memoryview):
        return body.nbytes
    if isinstance(body, str):
        return len(body.encode("utf-8"))
    try:
        return len(json.dumps(body, separators=(",", ":")))
    except (TypeError, ValueError):
        return None


def classify(method: str, url: str, body: Any = None) -> tuple[str, str | None]:
    """The journal's (op, app) for a panel request, read from its method and path."""
    path = urlsplit(str(url)).path.rstrip("/")
    leaf = unquote(path.rsplit("/", 1)[-1]) or None
    if str(method).lower() == "delete":
        return "delete", leaf if path.startswith("/api/v1/apps/") else None
    if path.startswith("/api/v1/apps/pushed/"):
        return "push", leaf
    if path.startswith("/api/v1/apps/script/"):
        return "install", leaf
    if path == "/api/v1/apps/active":
        name = body.get("name") if isinstance(body, Mapping) else None
        return "activate", name if isinstance(name, str) else None
    if path == "/api/v1/device/reboot":
        return "reboot", None
    if path == "/api/v1/settings":
        return "settings", None
    if path == "/api/v1/display":
        return "display", None
    return "other", None


def _switched_off(directory: Path) -> bool:
    try:
        return (directory / OFF_NAME).exists()
    except OSError:
        return False


def _read_last(directory: Path) -> float | None:
    try:
        value = float((directory / LAST_NAME).read_text(encoding="ascii").strip())
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return value if math.isfinite(value) else None


@dataclass
class _Budget:
    """How long a waiter has left, renewed each time some write ends.

    Counted from the waiter's own start, the budget sent the third writer of a
    stalled queue in ungated while the second was still mid-request. Every
    write that ends rewrites ``.last``, so a new value there means the queue
    moved, and the waiter goes in ungated only when one holder overruns.
    """

    directory: Path
    began: float
    seen: float | None = field(init=False)
    renewed: float = field(init=False)

    def __post_init__(self) -> None:
        self.seen = _read_last(self.directory)
        self.renewed = self.began

    def spent(self) -> bool:
        now = _monotonic()
        last = _read_last(self.directory)
        if last is not None and last != self.seen:
            self.seen, self.renewed = last, now
        deadline = min(self.renewed + ACQUIRE_TIMEOUT_SECONDS, self.began + MAX_WAIT_SECONDS)
        return now >= deadline


def _lock_file(fd: int, budget: _Budget) -> bool:
    while True:
        try:
            _try_lock(fd)
            return True
        except OSError as error:
            if error.errno not in _CONTENDED:
                raise
        if budget.spent():
            return False
        _sleep(POLL_SECONDS)


def _wait_gap(directory: Path) -> None:
    gap = gap_seconds()
    last = _read_last(directory) if gap > 0 else None
    if last is None:
        return
    # Clamped both ways: a garbage, future or clock-jumped timestamp can cost
    # at most one gap, never a stall.
    wait = min(max(last + gap - _wall(), 0.0), gap)
    if wait > 0:
        _sleep(wait)


def _acquire(directory: Path, began: float) -> tuple[int | None, str]:
    """Return (lock fd, gate state); a None fd means the write goes ahead ungated."""
    budget = _Budget(directory, began)
    while not _thread_lock.acquire(timeout=POLL_SECONDS):
        if budget.spent():
            return None, "timeout"
    fd: int | None = None
    held = False
    try:
        try:
            fd = os.open(directory / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o644)
            if not _lock_file(fd, budget):
                return None, "timeout"
            _wait_gap(directory)
            held = True
            return fd, "ok"
        except Exception:  # noqa: BLE001 - an unusable lock must not stop the write
            return None, "unavailable"
    finally:
        if not held:
            try:
                if fd is not None:
                    os.close(fd)
            except OSError:
                pass
            finally:
                _thread_lock.release()


def _mark_last(directory: Path) -> None:
    """Note when a write ended: the next writer's gap, and a waiter's sign of progress."""
    try:
        (directory / LAST_NAME).write_text(f"{_wall():.3f}", encoding="ascii")
    except OSError:
        pass


def _release(fd: int, directory: Path) -> None:
    try:
        _mark_last(directory)
        try:
            _unlock(fd)
        except OSError:
            pass  # closing the handle releases it anyway
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        finally:
            _thread_lock.release()


def _journal(directory: Path, record: MutationRecord) -> None:
    """Append one line, before the lock is let go.

    Windows appends from two processes at once lose and splice lines, so the
    lock is what keeps gated writers apart; an ungated write's line stays
    best-effort. Rotation gets its own try: a reader with the file open (a plain
    ``open()``, as panel_heap_report uses) blocks the rename, and the line is
    appended anyway -- the file runs past the cap until a later rotation works.
    """
    path = directory / EVENTS_NAME
    try:
        if path.stat().st_size >= EVENTS_MAX_BYTES:
            os.replace(path, path.with_suffix(".1.jsonl"))
    except OSError:
        pass
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.line(), separators=(",", ":")) + "\n")
    except Exception:  # noqa: BLE001 - evidence is best-effort; it must never take a sender down
        pass


@contextmanager
def mutation(
    source: str,
    op: str,
    app: str | None = None,
    *,
    body: Any = None,
    data_dir: Path | str | None = None,
) -> Iterator[MutationRecord]:
    """Hold the panel for one write; call ``record.observe(response)`` inside.

    Exceptions from the block propagate unchanged and are journaled by class
    name. A nested call on the same thread does not lock again -- it would wait
    on itself for the whole timeout -- and is journaled as ``nested``.

    Everything after the lock is taken runs inside the ``finally`` that gives
    it back: a Ctrl+C in a console run that leaked it would leave this thread's
    later writes "nested" and every other process waiting out the timeout.
    """
    record = MutationRecord(
        source=str(source)[:32],
        op=op if op in OPS else "other",
        app=None if app is None else str(app)[:64],
        size=body_size(body),
    )
    if not enabled() or _defaults_to_live_dir_under_test(data_dir):
        record.gate = "off"
        record.started = _wall()
        yield record
        return

    began = _monotonic()
    depth = getattr(_local, "depth", 0)
    try:
        directory: Path | None = gate_dir(data_dir)
    except Exception:  # noqa: BLE001 - e.g. no resolvable home directory
        directory = None
    fd: int | None = None
    _local.depth = depth + 1
    try:
        if depth:
            record.gate = "nested"
        elif directory is None:
            record.gate = "unavailable"
        elif _switched_off(directory):
            record.gate = "off"
        else:
            fd, record.gate = _acquire(directory, began)
        record.wait_ms = max(0, round((_monotonic() - began) * 1000))
        record.started = _wall()
        started = _monotonic()
        try:
            yield record
        except BaseException as error:
            record.err = type(error).__name__
            raise
        finally:
            record.ms = max(0, round((_monotonic() - started) * 1000))
            if directory is not None:
                _journal(directory, record)
                if fd is None and not depth:
                    _mark_last(directory)  # ungated, but the next gated write still keeps its gap
    finally:
        _local.depth = depth
        if fd is not None and directory is not None:
            _release(fd, directory)


__all__ = [
    "ACQUIRE_TIMEOUT_SECONDS",
    "EVENTS_NAME",
    "MAX_WAIT_SECONDS",
    "MIN_GAP_SECONDS",
    "MutationRecord",
    "OFF_NAME",
    "body_size",
    "classify",
    "enabled",
    "gap_seconds",
    "gate_dir",
    "mutation",
]
