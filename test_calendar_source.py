from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import requests

import calendar_source as source


CALENDAR_A = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"
CALENDAR_B = (
    b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Test//EN\r\nEND:VCALENDAR\r\n"
)


class FakeClock:
    def __init__(self, value: float = 1_000_000.0) -> None:
        self.value = value

    def time(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        body: bytes = CALENDAR_A,
        *,
        headers: dict[str, str] | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self.body = body
        self.headers = headers or {}
        self.text = text
        self.closed = False
        self.iterated = False

    def iter_content(self, chunk_size: int) -> list[bytes]:
        self.iterated = True
        return [
            self.body[index : index + chunk_size]
            for index in range(0, len(self.body), chunk_size)
        ]

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((url, kwargs))
        if not self.outcomes:
            raise AssertionError("unexpected HTTP request")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, FakeResponse)
        return outcome


class MemoryFileSystem:
    def __init__(self) -> None:
        self.files: dict[Path, bytes] = {}
        self.atomic_writes: list[Path] = []

    def read_limited(self, path: Path, maximum_bytes: int) -> bytes:
        try:
            body = self.files[path]
        except KeyError as error:
            raise FileNotFoundError(path) from error
        if len(body) > maximum_bytes:
            raise source.BodyTooLarge("too large")
        return body

    def atomic_write(self, path: Path, body: bytes) -> None:
        self.atomic_writes.append(path)
        self.files[path] = body


class FailingWriteFileSystem(MemoryFileSystem):
    def atomic_write(self, path: Path, body: bytes) -> None:
        raise PermissionError("runtime directory is read only")


def make_config(runtime: Path, calendar: str | Path | None, **kwargs: object):
    return source.CalendarSourceConfig(
        source=calendar,
        runtime_dir=runtime,
        **kwargs,
    )


class CalendarSourceTests(unittest.TestCase):
    def test_unconfigured_source_is_quiet_actionable_and_does_no_io(self) -> None:
        session = FakeSession()
        result = source.CalendarSource(
            source.CalendarSourceConfig(source=None), session=session
        ).load()

        self.assertEqual(result.status, source.CalendarStatus.NOT_CONFIGURED)
        self.assertFalse(result.ready)
        self.assertIsNone(result.body)
        self.assertTrue(result.quiet)
        self.assertIn("Configure", result.action or "")
        self.assertEqual(session.calls, [])

    def test_http_fetch_uses_strict_streaming_limits_and_atomic_cache(self) -> None:
        clock = FakeClock()
        filesystem = MemoryFileSystem()
        response = FakeResponse(
            headers={
                "ETag": '"revision-1"',
                "Last-Modified": "Fri, 21 Aug 2026 12:00:00 GMT",
                "Content-Length": str(len(CALENDAR_A)),
            }
        )
        session = FakeSession(response)
        config = make_config(Path("runtime"), "https://example.test/private.ics")
        loader = source.CalendarSource(
            config, session=session, clock=clock, filesystem=filesystem
        )

        result = loader.load()

        self.assertTrue(result.ready)
        self.assertEqual(result.body, CALENDAR_A)
        self.assertFalse(result.from_cache)
        self.assertTrue(response.closed)
        self.assertTrue(response.iterated)
        self.assertEqual(filesystem.atomic_writes, [config.cache_path])
        _, options = session.calls[0]
        self.assertEqual(
            options["timeout"],
            (
                source.DEFAULT_CONNECT_TIMEOUT_SECONDS,
                source.DEFAULT_READ_TIMEOUT_SECONDS,
            ),
        )
        self.assertIs(options["stream"], True)
        self.assertIs(options["allow_redirects"], True)

    def test_refresh_is_not_attempted_before_five_minutes(self) -> None:
        clock = FakeClock()
        filesystem = MemoryFileSystem()
        session = FakeSession(FakeResponse(), FakeResponse(body=CALENDAR_B))
        config = make_config(Path("runtime"), "http://example.test/calendar.ics")
        loader = source.CalendarSource(
            config, session=session, clock=clock, filesystem=filesystem
        )

        first = loader.load()
        clock.advance(299)
        second = loader.load()

        self.assertEqual(first.body, CALENDAR_A)
        self.assertEqual(second.body, CALENDAR_A)
        self.assertTrue(second.from_cache)
        self.assertEqual(len(session.calls), 1)

        clock.advance(1)
        third = loader.load()
        self.assertEqual(third.body, CALENDAR_B)
        self.assertEqual(len(session.calls), 2)

    def test_etag_and_last_modified_drive_304_validation(self) -> None:
        clock = FakeClock()
        filesystem = MemoryFileSystem()
        first_response = FakeResponse(
            headers={
                "ETag": '"abc"',
                "Last-Modified": "Fri, 21 Aug 2026 12:00:00 GMT",
            }
        )
        not_modified = FakeResponse(304, b"")
        session = FakeSession(first_response, not_modified)
        config = make_config(Path("runtime"), "https://example.test/cal.ics")
        loader = source.CalendarSource(
            config, session=session, clock=clock, filesystem=filesystem
        )

        first = loader.load()
        first_validated = first.metadata.validated_at if first.metadata else 0
        clock.advance(300)
        second = loader.load()

        self.assertEqual(second.status, source.CalendarStatus.NOT_MODIFIED)
        self.assertEqual(second.body, CALENDAR_A)
        self.assertTrue(second.from_cache)
        self.assertGreater(second.metadata.validated_at, first_validated)  # type: ignore[union-attr]
        headers = session.calls[1][1]["headers"]
        self.assertEqual(headers["If-None-Match"], '"abc"')
        self.assertEqual(
            headers["If-Modified-Since"], "Fri, 21 Aug 2026 12:00:00 GMT"
        )

    def test_recent_last_good_is_used_on_refresh_failure(self) -> None:
        clock = FakeClock()
        filesystem = MemoryFileSystem()
        session = FakeSession(FakeResponse(), requests.Timeout("private details"))
        loader = source.CalendarSource(
            make_config(Path("runtime"), "https://example.test/cal.ics"),
            session=session,
            clock=clock,
            filesystem=filesystem,
        )
        loader.load()
        clock.advance(300)

        result = loader.load()

        self.assertEqual(result.status, source.CalendarStatus.CACHE_FALLBACK)
        self.assertTrue(result.ready)
        self.assertEqual(result.body, CALENDAR_A)
        self.assertTrue(result.from_cache)
        self.assertNotIn("private details", repr(result))

        # Persisted attempt time prevents retry storms after a service restart.
        restarted = source.CalendarSource(
            loader.config,
            session=FakeSession(),
            clock=clock,
            filesystem=filesystem,
        )
        repeated = restarted.load()
        self.assertEqual(repeated.status, source.CalendarStatus.CACHE_FALLBACK)

    def test_cache_older_than_six_hours_fails_closed(self) -> None:
        clock = FakeClock()
        filesystem = MemoryFileSystem()
        session = FakeSession(FakeResponse(), requests.Timeout("offline"))
        loader = source.CalendarSource(
            make_config(Path("runtime"), "https://example.test/cal.ics"),
            session=session,
            clock=clock,
            filesystem=filesystem,
        )
        loader.load()
        clock.advance(source.DEFAULT_MAX_STALE_SECONDS + 1)

        result = loader.load()

        self.assertEqual(result.status, source.CalendarStatus.UNAVAILABLE)
        self.assertFalse(result.ready)
        self.assertIsNone(result.body)
        self.assertTrue(result.quiet)

    def test_corrupt_or_unreadable_cache_fails_closed(self) -> None:
        clock = FakeClock()
        filesystem = MemoryFileSystem()
        config = make_config(Path("runtime"), "https://example.test/cal.ics")
        filesystem.files[config.cache_path] = b'{"body_base64":"not valid base64"}'
        loader = source.CalendarSource(
            config,
            session=FakeSession(requests.ConnectionError("offline")),
            clock=clock,
            filesystem=filesystem,
        )

        result = loader.load()

        self.assertEqual(result.status, source.CalendarStatus.UNAVAILABLE)
        self.assertIsNone(result.body)

    def test_oversized_http_body_is_rejected_without_caching(self) -> None:
        clock = FakeClock()
        filesystem = MemoryFileSystem()
        response = FakeResponse(
            body=CALENDAR_A,
            headers={"Content-Length": "9999"},
        )
        config = make_config(
            Path("runtime"), "https://example.test/cal.ics", max_body_bytes=64
        )
        result = source.CalendarSource(
            config,
            session=FakeSession(response),
            clock=clock,
            filesystem=filesystem,
        ).load()

        self.assertEqual(result.status, source.CalendarStatus.UNAVAILABLE)
        self.assertFalse(response.iterated)
        self.assertNotIn(config.cache_path, filesystem.files)

    def test_local_ics_file_obeys_refresh_interval(self) -> None:
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            calendar = root / "personal-secret-name.ics"
            calendar.write_bytes(CALENDAR_A)
            config = make_config(root / "runtime", calendar)
            loader = source.CalendarSource(config, clock=clock)

            first = loader.load()
            calendar.write_bytes(CALENDAR_B)
            clock.advance(299)
            before_due = loader.load()
            clock.advance(1)
            after_due = loader.load()

        self.assertEqual(first.body, CALENDAR_A)
        self.assertEqual(before_due.body, CALENDAR_A)
        self.assertEqual(after_due.body, CALENDAR_B)
        self.assertNotIn("personal-secret-name", repr(config))
        self.assertEqual(first.source_label, "local .ics file")

    def test_private_url_components_never_reach_outputs_or_cache_metadata(self) -> None:
        private_url = (
            "https://person:password@example.test/private/team-secret/basic.ics"
            "?token=query-secret#fragment-secret"
        )
        forbidden = (
            "person",
            "password",
            "private",
            "team-secret",
            "query-secret",
            "fragment-secret",
        )
        clock = FakeClock()
        filesystem = MemoryFileSystem()
        config = make_config(Path("runtime"), private_url)
        result = source.CalendarSource(
            config,
            session=FakeSession(FakeResponse()),
            clock=clock,
            filesystem=filesystem,
        ).load()
        cache_text = filesystem.files[config.cache_path].decode("utf-8")
        exposed = " ".join(
            (
                repr(config),
                repr(result),
                result.message,
                result.action or "",
                result.source_label,
                cache_text,
            )
        )

        self.assertEqual(result.source_label, "https://example.test/\u2026")
        for secret in forbidden:
            self.assertNotIn(secret, exposed)
        # The raw URL is used only for the actual request.
        self.assertEqual(result.metadata.source_kind, "http")  # type: ignore[union-attr]

    def test_cache_cannot_be_reused_for_a_different_source(self) -> None:
        clock = FakeClock()
        filesystem = MemoryFileSystem()
        first = source.CalendarSource(
            make_config(Path("runtime"), "https://example.test/one.ics"),
            session=FakeSession(FakeResponse()),
            clock=clock,
            filesystem=filesystem,
        )
        self.assertTrue(first.load().ready)

        second = source.CalendarSource(
            make_config(Path("runtime"), "https://example.test/two.ics"),
            session=FakeSession(requests.ConnectionError("offline")),
            clock=clock,
            filesystem=filesystem,
        )
        result = second.load()

        self.assertEqual(result.status, source.CalendarStatus.UNAVAILABLE)
        self.assertIsNone(result.body)

    def test_http_error_body_and_exception_details_are_not_exposed(self) -> None:
        private_url = "https://example.test/secret/path.ics?token=abc123"
        response = FakeResponse(
            500,
            b"",
            text=f"failure fetching {private_url}",
        )
        result = source.CalendarSource(
            make_config(Path("runtime"), private_url),
            session=FakeSession(response),
            clock=FakeClock(),
            filesystem=MemoryFileSystem(),
        ).load()

        exposed = f"{result!r} {result.message} {result.action}"
        self.assertNotIn("secret/path", exposed)
        self.assertNotIn("abc123", exposed)

    def test_cache_envelope_contains_required_metadata_without_raw_source(self) -> None:
        clock = FakeClock()
        filesystem = MemoryFileSystem()
        raw_url = "https://example.test/private/cal.ics?key=hidden"
        response = FakeResponse(headers={"ETag": '"v1"'})
        config = make_config(Path("runtime"), raw_url)
        source.CalendarSource(
            config,
            session=FakeSession(response),
            clock=clock,
            filesystem=filesystem,
        ).load()

        envelope = json.loads(filesystem.files[config.cache_path])
        metadata = envelope["metadata"]
        self.assertEqual(envelope["version"], 1)
        self.assertEqual(metadata["etag"], '"v1"')
        self.assertEqual(metadata["validated_at"], clock.time())
        self.assertEqual(metadata["checked_at"], clock.time())
        self.assertEqual(metadata["byte_count"], len(CALENDAR_A))
        self.assertEqual(len(metadata["source_fingerprint"]), 64)
        self.assertNotIn(raw_url, filesystem.files[config.cache_path].decode("utf-8"))

    def test_non_calendar_response_is_never_saved_as_last_good(self) -> None:
        filesystem = MemoryFileSystem()
        result = source.CalendarSource(
            make_config(Path("runtime"), "https://example.test/cal.ics"),
            session=FakeSession(FakeResponse(body=b"<html>sign in</html>")),
            clock=FakeClock(),
            filesystem=filesystem,
        ).load()

        self.assertEqual(result.status, source.CalendarStatus.UNAVAILABLE)
        self.assertEqual(filesystem.atomic_writes, [])

    def test_fresh_document_remains_usable_when_cache_directory_is_unwritable(self) -> None:
        result = source.CalendarSource(
            make_config(Path("runtime"), "https://example.test/cal.ics"),
            session=FakeSession(FakeResponse()),
            clock=FakeClock(),
            filesystem=FailingWriteFileSystem(),
        ).load()

        self.assertTrue(result.ready)
        self.assertEqual(result.body, CALENDAR_A)
        self.assertIn("cache could not be updated", result.message)
        self.assertIn("writable", result.action or "")

    def test_metadata_repr_hides_validators_and_fingerprint(self) -> None:
        filesystem = MemoryFileSystem()
        result = source.CalendarSource(
            make_config(Path("runtime"), "https://example.test/cal.ics"),
            session=FakeSession(
                FakeResponse(
                    headers={
                        "ETag": '"private-validator"',
                        "Last-Modified": "private-modification-value",
                    }
                )
            ),
            clock=FakeClock(),
            filesystem=filesystem,
        ).load()

        shown = repr(result.metadata)
        self.assertNotIn("private-validator", shown)
        self.assertNotIn("private-modification-value", shown)
        self.assertNotIn(result.metadata.source_fingerprint, shown)  # type: ignore[union-attr]

    def test_invalid_system_clock_fails_closed_without_io(self) -> None:
        session = FakeSession()
        clock = FakeClock(float("nan"))
        result = source.CalendarSource(
            make_config(Path("runtime"), "https://example.test/cal.ics"),
            session=session,
            clock=clock,
            filesystem=MemoryFileSystem(),
        ).load()

        self.assertEqual(result.status, source.CalendarStatus.UNAVAILABLE)
        self.assertIsNone(result.body)
        self.assertEqual(session.calls, [])


class ConfigurationTests(unittest.TestCase):
    def test_both_http_schemes_and_case_insensitive_local_suffix_are_supported(self) -> None:
        for value in (
            "http://example.test/calendar",
            "https://example.test/calendar",
            Path("calendar.ICS"),
        ):
            with self.subTest(value=value):
                config = source.CalendarSourceConfig(source=value)
                self.assertIsNotNone(config.source)

    def test_invalid_local_extension_and_timeout_are_rejected_without_echoing_path(self) -> None:
        private_path = "very-private-calendar.txt"
        with self.assertRaisesRegex(ValueError, "must be an .ics file") as raised:
            source.CalendarSourceConfig(source=private_path)
        self.assertNotIn(private_path, str(raised.exception))
        with self.assertRaisesRegex(ValueError, "timeouts must be positive"):
            source.CalendarSourceConfig(
                source="https://example.test/cal.ics", read_timeout_seconds=0
            )

    def test_non_http_url_schemes_are_rejected(self) -> None:
        for value in (
            "ftp://example.test/calendar.ics",
            "webcal://example.test/calendar.ics",
            "file://private/calendar.ics",
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "HTTP") as raised:
                    source.CalendarSourceConfig(source=value)
                self.assertNotIn(value, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
