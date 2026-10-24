"""Checks for the rescue of print() in the console-less senders.

The defect these guard against was invisible by construction: under Task
Scheduler ``sys.stdout`` is None and ``print()`` fails silently rather than
raising, so nothing in the running system could report that its own diagnostics
were being thrown away.
"""

from __future__ import annotations

import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sender_logging


class StreamToLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.logger = logging.getLogger("awtrix.test.stream")
        self.logger.handlers.clear()
        self.records: list[str] = []

        class Collector(logging.Handler):
            def emit(inner, record):  # noqa: N805
                self.records.append(record.getMessage())

        self.logger.addHandler(Collector())
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False

    def test_one_print_becomes_one_record(self) -> None:
        """print() writes the text and the newline separately.

        Logging each write as it arrives would split every line across two
        records, which is worse to read than no log at all.
        """
        stream = sender_logging._StreamToLog(self.logger, logging.INFO)
        stream.write("panel refused the page")
        stream.write("\n")
        self.assertEqual(self.records, ["panel refused the page"])

    def test_a_multiline_write_becomes_one_record_per_line(self) -> None:
        stream = sender_logging._StreamToLog(self.logger, logging.ERROR)
        stream.write("first\nsecond\nthird\n")
        self.assertEqual(self.records, ["first", "second", "third"])

    def test_text_without_a_newline_is_held_until_flush(self) -> None:
        stream = sender_logging._StreamToLog(self.logger, logging.INFO)
        stream.write("half a line")
        self.assertEqual(self.records, [])
        stream.flush()
        self.assertEqual(self.records, ["half a line"])

    def test_blank_lines_are_dropped(self) -> None:
        stream = sender_logging._StreamToLog(self.logger, logging.INFO)
        stream.write("\n\n  \n")
        self.assertEqual(self.records, [])

    def test_it_answers_the_questions_a_stream_is_asked(self) -> None:
        """traceback and argparse both inspect the stream they are handed."""
        stream = sender_logging._StreamToLog(self.logger, logging.INFO)
        self.assertFalse(stream.isatty())
        self.assertTrue(stream.writable())
        self.assertFalse(stream.readable())
        self.assertFalse(stream.seekable())


class ConfigureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = (sys.stdout, sys.stderr)
        # Write into a temporary directory, not the real one. A test suite that
        # drops files next to the senders' own logs makes those logs harder to
        # trust, which is the opposite of the point.
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = patch.object(
            sender_logging, "log_folder", lambda: Path(self._tmp.name)
        )
        self._patch.start()
        for name in ("awtrix.test_cfg", "awtrix.test_real"):
            logging.getLogger(name).handlers.clear()

    def tearDown(self) -> None:
        sys.stdout, sys.stderr = self.saved
        for name in ("awtrix.test_cfg", "awtrix.test_real"):
            for handler in logging.getLogger(name).handlers:
                handler.close()
            logging.getLogger(name).handlers.clear()
        self._patch.stop()
        self._tmp.cleanup()

    def test_a_missing_stdout_is_replaced(self) -> None:
        sys.stdout = None
        sys.stderr = None
        sender_logging.configure("test_cfg")
        self.assertIsInstance(sys.stdout, sender_logging._StreamToLog)
        self.assertIsInstance(sys.stderr, sender_logging._StreamToLog)

    def test_a_real_console_is_left_alone(self) -> None:
        """Running a sender by hand must still print to the terminal."""
        marker = object()
        sys.stdout = marker
        sys.stderr = marker
        sender_logging.configure("test_real")
        self.assertIs(sys.stdout, marker)
        self.assertIs(sys.stderr, marker)

    def test_configuring_twice_does_not_stack_handlers(self) -> None:
        first = sender_logging.configure("test_cfg")
        before = len(first.handlers)
        second = sender_logging.configure("test_cfg")
        self.assertIs(first, second)
        self.assertEqual(len(second.handlers), before)


class LogLocationTests(unittest.TestCase):
    """Asserts about the real folder, so it must not run under the patch above."""

    def test_the_log_lives_outside_the_repository(self) -> None:
        """Logs are device state, not source; the repo does not track them."""
        folder = sender_logging.log_folder()
        self.assertEqual(folder.name, "AWTRIX")
        repo = Path(__file__).resolve().parent
        self.assertNotIn(repo, folder.parents)
        self.assertNotEqual(folder, repo)


if __name__ == "__main__":
    unittest.main(verbosity=2)
