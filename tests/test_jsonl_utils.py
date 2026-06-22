"""Tests for the shared bounded-JSONL helper (jsonl_utils.cap_jsonl, #32)."""
from __future__ import annotations

import os
import pathlib
import tempfile
import unittest

from jsonl_utils import cap_jsonl


def _file(n: int) -> pathlib.Path:
    fd, p = tempfile.mkstemp(suffix=".jsonl"); os.close(fd)
    p = pathlib.Path(p)
    p.write_text("".join(f'{{"i":{i}}}\n' for i in range(n)), encoding="utf-8")
    return p


class CapJsonl(unittest.TestCase):
    def test_noop_under_threshold(self):
        p = _file(100)
        try:
            cap_jsonl(p, max_bytes=10_000_000, keep_rows=10)
            self.assertEqual(len(p.read_text().splitlines()), 100)
        finally:
            p.unlink()

    def test_trims_keeping_most_recent_atomically(self):
        p = _file(5000)
        try:
            cap_jsonl(p, max_bytes=1, keep_rows=50)
            lines = p.read_text().splitlines()
            self.assertEqual(len(lines), 50)
            self.assertEqual(lines[-1], '{"i":4999}')
            self.assertFalse(p.with_suffix(p.suffix + ".tmp").exists())  # no leftover
        finally:
            p.unlink()

    def test_missing_file_is_noop(self):
        cap_jsonl(pathlib.Path("does-not-exist.jsonl"), max_bytes=1, keep_rows=10)  # no raise

    def test_alerter_reexports_same_helper(self):
        import alerter
        self.assertIs(alerter._cap_jsonl, cap_jsonl)   # shared, not a copy


if __name__ == "__main__":
    unittest.main()
