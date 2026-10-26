"""Worker ownership and cooperative stop primitives; no process supervisor."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from functools import wraps
from hashlib import sha256
import logging
import os
from pathlib import Path
import signal
import threading
import time
from typing import Protocol

from config_loader import CanonicalConfig, SERVICE_NAMES, normalize_panel_url
from runtime_paths import data_directory

DriftMatrixConfig = CanonicalConfig


class ServiceWorker(Protocol):
    def run_worker(self, config: DriftMatrixConfig, stop_event: threading.Event) -> int: ...


class ServiceAlreadyRunning(RuntimeError):
    pass


def ownership_root(data_dir):
    return Path(data_dir) / "service_locks"


@contextmanager
def service_ownership(service: str, panel_url: str, data_dir: Path):
    if service not in SERVICE_NAMES:
        raise ValueError("Unknown service")
    identity = sha256(normalize_panel_url(panel_url).encode("utf-8")).hexdigest()[:20]
    directory = ownership_root(data_dir) / identity
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"service_{service}.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ServiceAlreadyRunning(f"Service {service} already has an owner or cannot be locked") from None
        locked = True
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
        yield path
    finally:
        try:
            if locked:
                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
        # Keep the inode: unlinking lets a new opener bypass an existing owner.


@contextmanager
def stop_signals(stop_event):
    saved = {}
    if threading.current_thread() is threading.main_thread():
        for number in dict.fromkeys(getattr(signal, name, None) for name in ("SIGINT", "SIGTERM", "SIGBREAK")):
            if number is not None:
                saved[number] = signal.signal(number, lambda _number, _frame: stop_event.set())
    try:
        yield
    finally:
        for number, previous in saved.items():
            signal.signal(number, previous)


def run_owned(service, panel_url, data_dir, stop_event, function):
    if stop_event.is_set():
        return 0
    try:
        with service_ownership(service, panel_url, data_dir), data_directory(data_dir), stop_signals(stop_event):
            if stop_event.is_set():
                return 0
            result = function()
            return 0 if result is None else result
    except ServiceAlreadyRunning as error:
        logging.getLogger("driftmatrix.worker").warning("%s", error)
        return 3


def worker_adapter(service):
    def decorate(function):
        @wraps(function)
        def run_worker(config: DriftMatrixConfig, stop_event: threading.Event) -> int:
            if not config.services[service].enabled:
                return 0
            return run_owned(service, config.panel.url, config.runtime.data_dir,
                             stop_event, lambda: function(config, stop_event))
        return run_worker
    return decorate


def wait(stop_event, seconds, *, sleep=None):
    """Keep legacy fake-clock callers; workers wake as soon as stop is set."""
    if stop_event is not None:
        return stop_event.wait(max(0.0, seconds))
    (sleep or time.sleep)(max(0.0, seconds))
    return False


async def async_wait(stop_event, seconds):
    if stop_event is None:
        await asyncio.sleep(seconds)
    else:
        try:
            await asyncio.to_thread(stop_event.wait, max(0.0, seconds))
        except asyncio.CancelledError:
            stop_event.set()  # Release the executor thread before asyncio shuts down.
            raise


def in_quiet_hours(moment, quiet_hours):
    minute = moment.hour * 60 + moment.minute
    start, end = (sum(int(part) * multiplier for part, multiplier in zip(quiet_hours[key].split(":"), (60, 1)))
                  for key in ("start", "end"))
    return start <= minute < end if start < end else minute >= start or minute < end
