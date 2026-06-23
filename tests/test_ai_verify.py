"""Tests for the DeepSeek settlement-equivalence verifier + the alerter gate.
HTTP is mocked — no network and no API key needed."""
from __future__ import annotations

import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import ai_verify


def _fake_resp(content: dict):
    body = json.dumps({"choices": [{"message": {"content": json.dumps(content)}}]}).encode()
    class _Ctx:
        def __enter__(self):
            return io.BytesIO(body)
        def __exit__(self, *a):
            return False
    return _Ctx()


class Verify(unittest.TestCase):
    def setUp(self):
        ai_verify._cache.clear()
        # Isolate the disk cache: empty in-memory map + a throwaway file path.
        ai_verify._disk = {}
        self._orig_cache_file = ai_verify._CACHE_FILE
        fd, path = tempfile.mkstemp(suffix=".json"); os.close(fd); os.unlink(path)
        ai_verify._CACHE_FILE = pathlib.Path(path)

    def tearDown(self):
        ai_verify._CACHE_FILE = self._orig_cache_file
        ai_verify._disk = None

    def test_disk_cache_avoids_second_api_call(self):
        resp = {"same_event": True, "settlement_same": True, "poly_settlement": None,
                "kalshi_settlement": None, "same": True, "reason": "x"}
        with patch("urllib.request.urlopen", return_value=_fake_resp(resp)) as m:
            ai_verify.verify("a", "b", api_key="k")
            ai_verify._cache.clear()              # drop in-process cache; force disk path
            ai_verify.verify("a", "b", api_key="k")
        self.assertEqual(m.call_count, 1)         # 2nd call served from disk, no API

    def test_prompt_version_change_invalidates(self):
        resp = {"same_event": True, "settlement_same": True, "poly_settlement": None,
                "kalshi_settlement": None, "same": True, "reason": "x"}
        with patch("urllib.request.urlopen", return_value=_fake_resp(resp)) as m:
            ai_verify.verify("a", "b", api_key="k")
            ai_verify._cache.clear()
            with patch.object(ai_verify, "_PROMPT_VERSION", "different"):
                ai_verify.verify("a", "b", api_key="k")  # new key -> miss -> API again
        self.assertEqual(m.call_count, 2)

    def test_no_key_returns_none(self):
        self.assertIsNone(ai_verify.verify("a", "b", api_key=None))

    def test_parses_verdict(self):
        with patch("urllib.request.urlopen",
                   return_value=_fake_resp({"same_event": True, "settlement_same": True,
                                            "poly_settlement": "2026-11-03",
                                            "kalshi_settlement": "2026-11-03",
                                            "same": True, "reason": "same race"})):
            v = ai_verify.verify("Dem KS-03", "Will Democratic win KS-03?", api_key="k")
        self.assertEqual(v["same"], True)
        self.assertEqual(v["poly_settlement"], "2026-11-03")
        self.assertTrue(v["same_event"] and v["settlement_same"])

    def test_different_settlement_date_not_same(self):
        with patch("urllib.request.urlopen",
                   return_value=_fake_resp({"same_event": True, "settlement_same": False,
                                            "poly_settlement": "2026-06-30",
                                            "kalshi_settlement": "2026-12-31",
                                            "same": False, "reason": "different windows"})):
            v = ai_verify.verify("X by Jun 30", "X by Dec 31", api_key="k")
        self.assertFalse(v["same"])  # same event, different settlement -> not arbable as identical

    def test_malformed_verdict_missing_same_fails_open(self):
        with patch("urllib.request.urlopen",
                   return_value=_fake_resp({"reason": "no same field"})):
            self.assertIsNone(ai_verify.verify("a", "b", api_key="k"))

    def test_non_bool_same_fails_open(self):
        with patch("urllib.request.urlopen",
                   return_value=_fake_resp({"same": "false", "reason": "stringified"})):
            self.assertIsNone(ai_verify.verify("a", "b", api_key="k"))

    def test_api_error_fails_open_none(self):
        with patch("urllib.request.urlopen", side_effect=OSError("boom")):
            self.assertIsNone(ai_verify.verify("a", "b", api_key="k"))

    def test_caches_by_text_pair(self):
        with patch("urllib.request.urlopen",
                   return_value=_fake_resp({"same": False, "settlement_date": None, "reason": "x"})) as m:
            ai_verify.verify("a", "b", api_key="k")
            ai_verify.verify("a", "b", api_key="k")  # cached -> no 2nd call
        self.assertEqual(m.call_count, 1)


class ResolveApiKey(unittest.TestCase):
    def test_env_takes_precedence(self):
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "from-env"}):
            self.assertEqual(ai_verify.resolve_api_key(), "from-env")

    def test_none_when_absent_everywhere(self):
        # No env var and (mock) no registry value -> None.
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(ai_verify.sys, "platform", "linux"):
            self.assertIsNone(ai_verify.resolve_api_key())


class Gate(unittest.TestCase):
    def setUp(self):
        # _ai_verify_gate appends verdicts to BASE/ai_verify.jsonl — redirect BASE
        # to a temp dir so the test suite never pollutes the real verdict log (#5).
        import alerter
        self._alerter = alerter
        self._orig_base = alerter.BASE
        self._tmp = tempfile.mkdtemp()
        alerter.BASE = pathlib.Path(self._tmp)

    def tearDown(self):
        self._alerter.BASE = self._orig_base
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _sig(self, t):
        return {"poly_title": t, "kalshi_title": t + " K", "net_accurate": 0.05}

    def test_no_key_is_noop(self):
        # resolve_api_key returns None (no env, no registry) -> gate keeps all.
        from alerter import _ai_verify_gate
        sigs = [self._sig("A"), self._sig("B")]
        with patch("ai_verify.resolve_api_key", return_value=None):
            self.assertEqual(_ai_verify_gate(sigs, {}), sigs)

    def test_enforce_drops_different_shadow_keeps(self):
        from alerter import _ai_verify_gate
        sigs = [self._sig("REAL"), self._sig("PHANTOM")]
        def fake_verify(s, key, **kw):
            return {"same": s["poly_title"] == "REAL", "poly_settlement": None,
                    "kalshi_settlement": None, "reason": "r"}
        with patch("ai_verify.resolve_api_key", return_value="k"), \
             patch("ai_verify.verify_signal", side_effect=fake_verify):
            enforced = _ai_verify_gate(sigs, {"ai_verify_mode": "enforce"})
            shadow = _ai_verify_gate(sigs, {"ai_verify_mode": "shadow"})
        self.assertEqual([s["poly_title"] for s in enforced], ["REAL"])     # phantom dropped
        self.assertEqual(len(shadow), 2)                                     # shadow keeps both

    def test_enforce_mass_drop_keeps_all(self):
        # If the AI would drop > 60% of the cycle (API/prompt anomaly), enforce
        # must fail-open and keep ALL rather than send a near-empty email (issue #1).
        from alerter import _ai_verify_gate
        sigs = [self._sig(x) for x in ("A", "B", "C", "D")]  # all judged different
        with patch("ai_verify.resolve_api_key", return_value="k"), \
             patch("ai_verify.verify_signal",
                   return_value={"same": False, "poly_settlement": None,
                                 "kalshi_settlement": None, "reason": "glitch"}):
            kept = _ai_verify_gate(sigs, {"ai_verify_mode": "enforce"})
        self.assertEqual(len(kept), 4)  # 100% drop -> anomaly -> keep all

    def test_failopen_keeps_on_none(self):
        from alerter import _ai_verify_gate
        sigs = [self._sig("A")]
        with patch("ai_verify.resolve_api_key", return_value="k"), \
             patch("ai_verify.verify_signal", return_value=None):
            self.assertEqual(_ai_verify_gate(sigs, {"ai_verify_mode": "enforce"}), sigs)


if __name__ == "__main__":
    unittest.main()
