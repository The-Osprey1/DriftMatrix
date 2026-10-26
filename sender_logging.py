"""Give the pythonw senders somewhere for their output to go.

Every sender here runs from Task Scheduler under ``pythonw.exe``, which has no
console. In that configuration ``sys.stdout`` and ``sys.stderr`` are both
``None`` and ``print()`` is a **silent no-op** -- it does not even raise.
Measured on this machine via a scheduled task rather than assumed::

    sys.stdout is None
    sys.stderr is None
    print() -> OK (no exception)

So every diagnostic these senders emit has been discarded since the day they
were installed: the animation guard announcing that the panel refused a page,
the retry counters, the "panel unavailable" warnings. Worse, an unhandled
exception writes its traceback to ``sys.stderr``, so a sender that crashed did
so without leaving anything behind at all.

Rather than rewrite thirty-odd call sites, this replaces the missing streams
with writers that forward into a rotating log file. Existing ``print()`` calls
start being recorded unchanged, and a crash leaves a traceback on disk.

Only *missing* streams are replaced. Running a sender by hand with ``--once``
from a terminal still prints to that terminal, which is what you want when you
are watching it.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

MAX_BYTES = 512 * 1024
BACKUP_COUNT = 2


def log_folder() -> Path:
    """Where the senders keep their logs, outside the synced repository."""
    from runtime_paths import current_data_dir
    if current_data_dir() is not None:
        return current_data_dir()
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "AWTRIX"


class _StreamToLog:
    """A file-like object that turns writes into log records, one per line.

    ``print()`` calls ``write`` twice -- once for the text and once for the
    newline -- so text is buffered until a newline arrives. Without that every
    line would be split across two records.
    """

    def __init__(self, logger: logging.Logger, level: int) -> None:
        self._logger = logger
        self._level = level
        self._buffer = ""

    def write(self, text: str) -> int:
        if not text:
            return 0
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip()
            if line:
                self._logger.log(self._level, line)
        return len(text)

    def flush(self) -> None:
        if self._buffer.strip():
            self._logger.log(self._level, self._buffer.strip())
        self._buffer = ""

    def isatty(self) -> bool:
        return False

    # Nothing here reads from these, but a stream that cannot answer these
    # honestly breaks anything that inspects it (argparse, traceback, pdb).
    def readable(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False


def configure(name: str, level: int = logging.INFO) -> logging.Logger:
    """Install a rotating log for ``name`` and rescue print() if it is lost.

    Safe to call more than once; the second call returns the same logger
    without stacking another handler on it.
    """
    logger = logging.getLogger(f"awtrix.{name}")
    if logger.handlers:
        return logger

    logger.setLevel(level)
    folder = log_folder()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            folder / f"{name}.log",
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError:
        # A sender that cannot open its log must still push pixels. This is
        # the one place where carrying on quietly is right: the alternative is
        # a panel that goes dark because a log file was locked.
        return logger
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)

    # Only fill in what the OS did not give us. A real console stays a console.
    if sys.stdout is None:
        sys.stdout = _StreamToLog(logger, logging.INFO)
    if sys.stderr is None:
        sys.stderr = _StreamToLog(logger, logging.ERROR)
    return logger
