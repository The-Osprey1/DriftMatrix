"""Per-worker paths, scoped to an execution context rather than process globals."""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_directory = ContextVar("driftmatrix_data_dir", default=None)


def current_data_dir():
    return _directory.get()


@contextmanager
def data_directory(path):
    token = _directory.set(Path(path).resolve())
    try:
        yield
    finally:
        _directory.reset(token)
