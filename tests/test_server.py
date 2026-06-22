"""Tests for the dashboard's bounded signal reader (server._load_signals, #31)."""
from __future__ import annotations

import json
import unittest

import server


class LoadSignals(unittest.TestCase):
    def setUp(self):
        self._orig = server.SIGNALS_FILE

    def tearDown(self):
        server.SIGNALS_FILE = self._orig

    def _write(self, rows):
        import os, pathlib, tempfile
        fd, p = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        server.SIGNALS_FILE = pathlib.Path(p)
        return pathlib.Path(p)

    def test_absent_file_returns_empty(self):
        import pathlib
        server.SIGNALS_FILE = pathlib.Path("does-not-exist.jsonl")
        self.assertEqual(server._load_signals(), [])

    def test_returns_last_n_newest_first(self):
        p = self._write([{"i": i} for i in range(20)])
        try:
            out = server._load_signals(n=3)
            self.assertEqual([r["i"] for r in out], [19, 18, 17])
        finally:
            p.unlink()

    def test_reads_only_tail_block(self):
        p = self._write([{"i": i} for i in range(3000)])
        try:
            out = server._load_signals(n=2, _block=2000)  # tiny block -> partial-line path
            self.assertEqual([r["i"] for r in out], [2999, 2998])
        finally:
            p.unlink()


if __name__ == "__main__":
    unittest.main()
