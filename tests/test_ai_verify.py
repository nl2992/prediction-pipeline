"""Tests for the DeepSeek settlement-equivalence verifier + the alerter gate.
HTTP is mocked — no network and no API key needed."""
from __future__ import annotations

import io
import json
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

    def test_api_error_fails_open_none(self):
        with patch("urllib.request.urlopen", side_effect=OSError("boom")):
            self.assertIsNone(ai_verify.verify("a", "b", api_key="k"))

    def test_caches_by_text_pair(self):
        with patch("urllib.request.urlopen",
                   return_value=_fake_resp({"same": False, "settlement_date": None, "reason": "x"})) as m:
            ai_verify.verify("a", "b", api_key="k")
            ai_verify.verify("a", "b", api_key="k")  # cached -> no 2nd call
        self.assertEqual(m.call_count, 1)


class Gate(unittest.TestCase):
    def _sig(self, t):
        return {"poly_title": t, "kalshi_title": t + " K", "net_accurate": 0.05}

    def test_no_key_is_noop(self):
        from alerter import _ai_verify_gate
        sigs = [self._sig("A"), self._sig("B")]
        with patch.dict("os.environ", {}, clear=True):  # no DEEPSEEK_API_KEY
            self.assertEqual(_ai_verify_gate(sigs, {}), sigs)

    def test_enforce_drops_different_shadow_keeps(self):
        from alerter import _ai_verify_gate
        sigs = [self._sig("REAL"), self._sig("PHANTOM")]
        def fake_verify(s, key, **kw):
            return {"same": s["poly_title"] == "REAL", "poly_settlement": None,
                    "kalshi_settlement": None, "reason": "r"}
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "k"}), \
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
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "k"}), \
             patch("ai_verify.verify_signal",
                   return_value={"same": False, "poly_settlement": None,
                                 "kalshi_settlement": None, "reason": "glitch"}):
            kept = _ai_verify_gate(sigs, {"ai_verify_mode": "enforce"})
        self.assertEqual(len(kept), 4)  # 100% drop -> anomaly -> keep all

    def test_failopen_keeps_on_none(self):
        from alerter import _ai_verify_gate
        sigs = [self._sig("A")]
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "k"}), \
             patch("ai_verify.verify_signal", return_value=None):
            self.assertEqual(_ai_verify_gate(sigs, {"ai_verify_mode": "enforce"}), sigs)


if __name__ == "__main__":
    unittest.main()
