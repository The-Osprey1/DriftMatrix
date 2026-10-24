"""Privacy-safe, fail-closed iCalendar source and last-good cache.

This module deliberately stops at acquiring a complete VCALENDAR document.  It
does not interpret events, recurrence rules, time zones, or display policy.
Callers receive bytes only when the source was validated recently enough to be
trusted.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import base64
import binascii
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Protocol
from urllib.parse import urlsplit

import requests


DEFAULT_REFRESH_SECONDS = 5 * 60
DEFAULT_MAX_STALE_SECONDS = 6 * 60 * 60
DEFAULT_MAX_BODY_BYTES = 2 * 1024 * 1024
DEFAULT_CONNECT_TIMEOUT_SECONDS = 3.05
DEFAULT_READ_TIMEOUT_SECONDS = 8.0
CACHE_VERSION = 1
CACHE_FILENAME = "nextup-calendar-last-good.json"
_CACHE_OVERHEAD_BYTES = 64 * 1024
_CLOCK_FUTURE_TOLERANCE_SECONDS = 5 * 60
_HTTP_CHUNK_BYTES = 16 * 1024


class Clock(Protocol):
    def time(self) -> float: ...


class SystemClock:
    def time(self) -> float:
        return time.time()


class FileSystem(Protocol):
    """The small filesystem surface used by :class:`CalendarSource`."""

    def read_limited(self, path: Path, maximum_bytes: int) -> bytes: ...

    def atomic_write(self, path: Path, body: bytes) -> None: ...


class BodyTooLarge(ValueError):
    """A source or cache exceeded its configured hard size limit."""


class LocalFileSystem:
    def read_limited(self, path: Path, maximum_bytes: int) -> bytes:
        with path.open("rb") as handle:
            body = handle.read(maximum_bytes + 1)
        if len(body) > maximum_bytes:
            raise BodyTooLarge("file exceeds the configured size limit")
        return body

    def atomic_write(self, path: Path, body: bytes) -> None:
        """Write one complete cache envelope, then atomically replace the old one."""

        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                # Windows applies ACLs rather than POSIX mode bits.
                pass
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except OSError:
                pass


def default_runtime_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    return Path(root) / "AWTRIX" / "runtime" if root else Path(".awtrix-runtime")


def _is_remote(source: str | Path) -> bool:
    if isinstance(source, Path):
        return False
    return str(source).strip().lower().startswith(("http://", "https://"))


def redact_source(source: str | Path | None) -> str:
    """Return a diagnostic label that never contains URL userinfo/path/query data."""

    if source is None or not str(source).strip():
        return "not configured"
    if not _is_remote(source):
        # Even a local filename may contain a person's or organization's name.
        return "local .ics file"
    parsed = urlsplit(str(source).strip())
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname or "private-host"
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    authority = f"{hostname}:{port}" if port is not None else hostname
    return f"{scheme}://{authority}/\u2026"


def _source_identity(source: str | Path) -> str:
    if _is_remote(source):
        identity = str(source).strip()
    else:
        identity = str(Path(source).expanduser().resolve(strict=False))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _source_kind(source: str | Path) -> str:
    return "http" if _is_remote(source) else "file"


@dataclass(frozen=True, repr=False)
class CalendarSourceConfig:
    source: str | Path | None = None
    runtime_dir: Path = field(default_factory=default_runtime_dir)
    refresh_interval_seconds: float = DEFAULT_REFRESH_SECONDS
    max_stale_seconds: float = DEFAULT_MAX_STALE_SECONDS
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        source = self.source
        if source is not None and not str(source).strip():
            object.__setattr__(self, "source", None)
            source = None
        object.__setattr__(self, "runtime_dir", Path(self.runtime_dir).expanduser())
        if self.refresh_interval_seconds <= 0:
            raise ValueError("refresh_interval_seconds must be positive")
        if self.max_stale_seconds <= 0:
            raise ValueError("max_stale_seconds must be positive")
        if self.max_stale_seconds < self.refresh_interval_seconds:
            raise ValueError("max_stale_seconds cannot be shorter than refresh interval")
        if (
            not isinstance(self.max_body_bytes, int)
            or isinstance(self.max_body_bytes, bool)
            or not 1 <= self.max_body_bytes <= 32 * 1024 * 1024
        ):
            raise ValueError("max_body_bytes must be between 1 byte and 32 MiB")
        if self.connect_timeout_seconds <= 0 or self.read_timeout_seconds <= 0:
            raise ValueError("HTTP timeouts must be positive")
        if source is None:
            return
        if _is_remote(source):
            parsed = urlsplit(str(source).strip())
            if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
                raise ValueError("calendar URL must be a valid HTTP(S) URL")
            try:
                parsed.port
            except ValueError as error:
                raise ValueError("calendar URL must be a valid HTTP(S) URL") from error
        else:
            if isinstance(source, str) and "://" in source:
                raise ValueError("calendar source must be HTTP(S) or a local .ics file")
            local = Path(source).expanduser()
            if local.suffix.lower() != ".ics":
                raise ValueError("local calendar source must be an .ics file")

    @property
    def cache_path(self) -> Path:
        return self.runtime_dir / CACHE_FILENAME

    @property
    def source_label(self) -> str:
        return redact_source(self.source)

    def __repr__(self) -> str:
        return (
            "CalendarSourceConfig("
            f"source={self.source_label!r}, runtime_dir='<runtime-dir>', "
            f"refresh_interval_seconds={self.refresh_interval_seconds!r}, "
            f"max_stale_seconds={self.max_stale_seconds!r}, "
            f"max_body_bytes={self.max_body_bytes!r})"
        )


class CalendarStatus(str, Enum):
    NOT_CONFIGURED = "not_configured"
    READY = "ready"
    NOT_MODIFIED = "not_modified"
    CACHE_FALLBACK = "cache_fallback"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, repr=False)
class CacheMetadata:
    source_fingerprint: str
    source_kind: str
    source_label: str
    fetched_at: float
    validated_at: float
    checked_at: float
    byte_count: int
    sha256: str
    etag: str | None = None
    last_modified: str | None = None
    last_error: str | None = None

    def __repr__(self) -> str:
        return (
            "CacheMetadata("
            f"source_kind={self.source_kind!r}, source_label={self.source_label!r}, "
            f"fetched_at={self.fetched_at!r}, validated_at={self.validated_at!r}, "
            f"checked_at={self.checked_at!r}, byte_count={self.byte_count!r}, "
            f"has_etag={self.etag is not None!r}, "
            f"has_last_modified={self.last_modified is not None!r}, "
            f"degraded={self.last_error is not None!r})"
        )


@dataclass(frozen=True)
class CalendarLoadResult:
    """One safe source outcome; calendar contents are intentionally absent from repr."""

    status: CalendarStatus
    body: bytes | None = field(default=None, repr=False)
    metadata: CacheMetadata | None = None
    source_label: str = "not configured"
    message: str = ""
    action: str | None = None
    from_cache: bool = False
    quiet: bool = False

    @property
    def ready(self) -> bool:
        return self.body is not None and self.status in (
            CalendarStatus.READY,
            CalendarStatus.NOT_MODIFIED,
            CalendarStatus.CACHE_FALLBACK,
        )

    @property
    def text(self) -> str | None:
        return None if self.body is None else self.body.decode("utf-8-sig")


class _RefreshFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class _CachedDocument:
    body: bytes = field(repr=False)
    metadata: CacheMetadata


def _validate_ics(body: bytes, maximum_bytes: int) -> bytes:
    if not body:
        raise _RefreshFailure("empty-document")
    if len(body) > maximum_bytes:
        raise BodyTooLarge("calendar body exceeds the configured size limit")
    stripped = body.lstrip(b"\xef\xbb\xbf \t\r\n")
    lines = [line.strip().upper() for line in stripped.replace(b"\r\n", b"\n").split(b"\n")]
    lines = [line for line in lines if line]
    if not lines or lines[0] != b"BEGIN:VCALENDAR" or lines[-1] != b"END:VCALENDAR":
        raise _RefreshFailure("invalid-calendar")
    try:
        body.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise _RefreshFailure("invalid-encoding") from error
    return body


def _safe_header(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    if not result or len(result) > 1024 or "\r" in result or "\n" in result:
        return None
    return result


class CalendarSource:
    """Load one configured calendar with conditional refresh and last-good fallback."""

    def __init__(
        self,
        config: CalendarSourceConfig,
        *,
        session: Any | None = None,
        clock: Clock | None = None,
        filesystem: FileSystem | None = None,
    ) -> None:
        self.config = config
        self.session = session if session is not None else requests.Session()
        self.clock = clock or SystemClock()
        self.filesystem = filesystem or LocalFileSystem()
        self._last_attempt_at: float | None = None

    def __repr__(self) -> str:
        return f"CalendarSource(source={self.config.source_label!r})"

    def _cache_limit(self) -> int:
        encoded = ((int(self.config.max_body_bytes) + 2) // 3) * 4
        return encoded + _CACHE_OVERHEAD_BYTES

    def _read_cache(self, now: float) -> _CachedDocument | None:
        source = self.config.source
        if source is None:
            return None
        try:
            raw = self.filesystem.read_limited(self.config.cache_path, self._cache_limit())
            envelope = json.loads(raw.decode("utf-8"))
            if not isinstance(envelope, dict) or envelope.get("version") != CACHE_VERSION:
                return None
            raw_metadata = envelope.get("metadata")
            encoded_body = envelope.get("body_base64")
            if not isinstance(raw_metadata, dict) or not isinstance(encoded_body, str):
                return None
            body = base64.b64decode(encoded_body, validate=True)
            body = _validate_ics(body, int(self.config.max_body_bytes))
            fingerprint = str(raw_metadata["source_fingerprint"])
            if fingerprint != _source_identity(source):
                return None
            kind = str(raw_metadata["source_kind"])
            if kind != _source_kind(source):
                return None
            fetched_at = float(raw_metadata["fetched_at"])
            validated_at = float(raw_metadata["validated_at"])
            checked_at = float(raw_metadata["checked_at"])
            byte_count = int(raw_metadata["byte_count"])
            digest = str(raw_metadata["sha256"])
            if not all(value >= 0 for value in (fetched_at, validated_at, checked_at)):
                return None
            if not all(math.isfinite(value) for value in (fetched_at, validated_at, checked_at)):
                return None
            if fetched_at > validated_at or validated_at > checked_at:
                return None
            if checked_at > now + _CLOCK_FUTURE_TOLERANCE_SECONDS:
                return None
            if byte_count != len(body) or digest != hashlib.sha256(body).hexdigest():
                return None
            metadata = CacheMetadata(
                source_fingerprint=fingerprint,
                source_kind=kind,
                # Never trust a label read from disk; derive the redacted label anew.
                source_label=self.config.source_label,
                fetched_at=fetched_at,
                validated_at=validated_at,
                checked_at=checked_at,
                byte_count=byte_count,
                sha256=digest,
                etag=_safe_header(raw_metadata.get("etag")),
                last_modified=_safe_header(raw_metadata.get("last_modified")),
                last_error=(
                    "refresh-failed" if raw_metadata.get("last_error") is not None else None
                ),
            )
            return _CachedDocument(body, metadata)
        except (
            OSError,
            BodyTooLarge,
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
            binascii.Error,
            _RefreshFailure,
        ):
            # Cache corruption is intentionally indistinguishable from no cache.
            return None

    def _write_cache(self, document: _CachedDocument) -> None:
        metadata = document.metadata
        envelope = {
            "version": CACHE_VERSION,
            "metadata": {
                "source_fingerprint": metadata.source_fingerprint,
                "source_kind": metadata.source_kind,
                "source_label": metadata.source_label,
                "fetched_at": metadata.fetched_at,
                "validated_at": metadata.validated_at,
                "checked_at": metadata.checked_at,
                "byte_count": metadata.byte_count,
                "sha256": metadata.sha256,
                "etag": metadata.etag,
                "last_modified": metadata.last_modified,
                "last_error": metadata.last_error,
            },
            "body_base64": base64.b64encode(document.body).decode("ascii"),
        }
        serialized = json.dumps(
            envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        if len(serialized) > self._cache_limit():
            raise BodyTooLarge("encoded cache exceeds the configured size limit")
        self.filesystem.atomic_write(self.config.cache_path, serialized)

    def _new_document(
        self,
        body: bytes,
        now: float,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> _CachedDocument:
        source = self.config.source
        if source is None:
            raise AssertionError("cannot cache an unconfigured source")
        body = _validate_ics(body, int(self.config.max_body_bytes))
        metadata = CacheMetadata(
            source_fingerprint=_source_identity(source),
            source_kind=_source_kind(source),
            source_label=self.config.source_label,
            fetched_at=now,
            validated_at=now,
            checked_at=now,
            byte_count=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
            etag=_safe_header(etag),
            last_modified=_safe_header(last_modified),
        )
        return _CachedDocument(body, metadata)

    def _read_http_body(self, response: Any) -> bytes:
        raw_length = getattr(response, "headers", {}).get("Content-Length")
        try:
            content_length = int(raw_length) if raw_length is not None else None
        except (TypeError, ValueError):
            content_length = None
        if content_length is not None and content_length > self.config.max_body_bytes:
            raise BodyTooLarge("HTTP body exceeds the configured size limit")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=_HTTP_CHUNK_BYTES):
            if not chunk:
                continue
            if not isinstance(chunk, bytes):
                raise _RefreshFailure("invalid-http-body")
            total += len(chunk)
            if total > self.config.max_body_bytes:
                raise BodyTooLarge("HTTP body exceeds the configured size limit")
            chunks.append(chunk)
        return b"".join(chunks)

    def _refresh_http(
        self, cached: _CachedDocument | None, now: float
    ) -> tuple[_CachedDocument, CalendarStatus, bool]:
        source = self.config.source
        if source is None or not _is_remote(source):
            raise AssertionError("HTTP refresh requires an HTTP source")
        headers = {"Accept": "text/calendar, text/plain;q=0.9, */*;q=0.1"}
        if cached is not None and cached.metadata.etag:
            headers["If-None-Match"] = cached.metadata.etag
        if cached is not None and cached.metadata.last_modified:
            headers["If-Modified-Since"] = cached.metadata.last_modified
        response = self.session.get(
            str(source).strip(),
            headers=headers,
            timeout=(
                self.config.connect_timeout_seconds,
                self.config.read_timeout_seconds,
            ),
            stream=True,
            allow_redirects=True,
        )
        try:
            try:
                status = int(response.status_code)
            except (TypeError, ValueError, OverflowError) as error:
                raise _RefreshFailure("invalid-http-status") from error
            if status == 304:
                if cached is None:
                    raise _RefreshFailure("unexpected-not-modified")
                metadata = replace(
                    cached.metadata,
                    validated_at=now,
                    checked_at=now,
                    last_error=None,
                )
                return _CachedDocument(cached.body, metadata), CalendarStatus.NOT_MODIFIED, True
            if status != 200:
                raise _RefreshFailure("http-status")
            body = self._read_http_body(response)
            response_headers = getattr(response, "headers", {})
            document = self._new_document(
                body,
                now,
                etag=response_headers.get("ETag"),
                last_modified=response_headers.get("Last-Modified"),
            )
            return document, CalendarStatus.READY, False
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

    def _refresh_file(
        self, now: float
    ) -> tuple[_CachedDocument, CalendarStatus, bool]:
        source = self.config.source
        if source is None or _is_remote(source):
            raise AssertionError("file refresh requires a local source")
        path = Path(source).expanduser()
        body = self.filesystem.read_limited(path, int(self.config.max_body_bytes))
        return self._new_document(body, now), CalendarStatus.READY, False

    def _cache_is_usable(self, cached: _CachedDocument | None, now: float) -> bool:
        if cached is None:
            return False
        age = now - cached.metadata.validated_at
        return 0 <= age <= self.config.max_stale_seconds

    def _result(
        self,
        status: CalendarStatus,
        document: _CachedDocument | None,
        *,
        message: str,
        from_cache: bool,
        action: str | None = None,
        quiet: bool = False,
    ) -> CalendarLoadResult:
        return CalendarLoadResult(
            status=status,
            body=None if document is None else document.body,
            metadata=None if document is None else document.metadata,
            source_label=self.config.source_label,
            message=message,
            action=action,
            from_cache=from_cache,
            quiet=quiet,
        )

    def load(self, *, force: bool = False) -> CalendarLoadResult:
        """Return current bytes, refreshing at most once per five-minute interval."""

        source = self.config.source
        if source is None:
            return self._result(
                CalendarStatus.NOT_CONFIGURED,
                None,
                message="Next Up is idle because no calendar source is configured.",
                action="Configure an HTTP(S) calendar URL or a local .ics file.",
                from_cache=False,
                quiet=True,
            )

        now = float(self.clock.time())
        if not math.isfinite(now) or now < 0:
            return self._result(
                CalendarStatus.UNAVAILABLE,
                None,
                message="Calendar data is unavailable because the system clock is invalid.",
                action="Correct the PC clock before refreshing the calendar.",
                from_cache=False,
                quiet=True,
            )
        cached = self._read_cache(now)
        last_checked = (
            cached.metadata.checked_at if cached is not None else self._last_attempt_at
        )
        due = force or last_checked is None
        if last_checked is not None:
            elapsed = now - last_checked
            if elapsed < 0:
                due = True
            elif elapsed >= self.config.refresh_interval_seconds:
                due = True

        if not due:
            if self._cache_is_usable(cached, now):
                degraded = cached is not None and cached.metadata.last_error is not None
                return self._result(
                    CalendarStatus.CACHE_FALLBACK if degraded else CalendarStatus.READY,
                    cached,
                    message=(
                        "Calendar refresh is temporarily unavailable; using recent last-good data."
                        if degraded
                        else "Calendar data is ready."
                    ),
                    from_cache=True,
                )
            return self._result(
                CalendarStatus.UNAVAILABLE,
                None,
                message="Calendar data is unavailable and no recent last-good copy is safe to use.",
                action="Check the calendar source and wait for the next refresh.",
                from_cache=False,
                quiet=True,
            )

        self._last_attempt_at = now
        try:
            if _is_remote(source):
                document, status, from_cache = self._refresh_http(cached, now)
            else:
                document, status, from_cache = self._refresh_file(now)
            try:
                self._write_cache(document)
            except (OSError, ValueError):
                # The just-fetched document is still current and validated.  A
                # disk problem only removes fallback coverage for a later call.
                return self._result(
                    status,
                    document,
                    message="Calendar data is ready, but its last-good cache could not be updated.",
                    action="Check that the calendar runtime directory is writable.",
                    from_cache=from_cache,
                )
            return self._result(
                status,
                document,
                message="Calendar data is ready.",
                from_cache=from_cache,
            )
        except (
            requests.RequestException,
            OSError,
            BodyTooLarge,
            _RefreshFailure,
        ) as error:
            code = error.code if isinstance(error, _RefreshFailure) else type(error).__name__
            if cached is not None:
                degraded_metadata = replace(
                    cached.metadata,
                    checked_at=now,
                    last_error=_safe_header(code) or "refresh-failed",
                )
                degraded = _CachedDocument(cached.body, degraded_metadata)
                try:
                    self._write_cache(degraded)
                except (OSError, ValueError):
                    # Failure to persist retry timing must not make valid in-memory
                    # last-good data unsafe for this call.
                    pass
                if self._cache_is_usable(degraded, now):
                    return self._result(
                        CalendarStatus.CACHE_FALLBACK,
                        degraded,
                        message=(
                            "Calendar refresh is temporarily unavailable; using recent last-good data."
                        ),
                        from_cache=True,
                    )
            return self._result(
                CalendarStatus.UNAVAILABLE,
                None,
                message="Calendar data is unavailable and no recent last-good copy is safe to use.",
                action="Check the calendar source and wait for the next refresh.",
                from_cache=False,
                quiet=True,
            )

    read = load


__all__ = [
    "BodyTooLarge",
    "CACHE_FILENAME",
    "CacheMetadata",
    "CalendarLoadResult",
    "CalendarSource",
    "CalendarSourceConfig",
    "CalendarStatus",
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_MAX_STALE_SECONDS",
    "DEFAULT_READ_TIMEOUT_SECONDS",
    "DEFAULT_REFRESH_SECONDS",
    "FileSystem",
    "LocalFileSystem",
    "SystemClock",
    "default_runtime_dir",
    "redact_source",
]
