"""DeepSeek settlement-equivalence verifier — an optional AI second-opinion on
whether two cross-exchange markets really resolve on the IDENTICAL outcome.

The rule engine (matcher v1 + contract_spec v2) is strong but can't judge some
semantic cases (same subject, different predicate/scope/date). This asks an LLM:
"do these two markets settle YES/NO on the exact same underlying event — same
threshold, resolution source, and time window?" and extracts the resolution date
(useful for the annualised-return horizon).

Design:
  * OpenAI-compatible chat-completions call via stdlib urllib (no new deps).
  * FAIL-OPEN: any network/parse error returns None so a flaky API never drops a
    real arb or blocks the email — the caller treats None as "no opinion, keep".
  * In-process cache keyed on the two texts (one verdict per pair per run).
  * Strict-JSON response: {"same": bool, "settlement_date": "YYYY-MM-DD"|null,
    "reason": str}.

Config (alert_config.json): "deepseek_api_key". CLI: python ai_verify.py "A" "B".
"""
from __future__ import annotations

import json
import sys
import urllib.request

_ENDPOINT = "https://api.deepseek.com/chat/completions"
_MODEL = "deepseek-chat"
_SYSTEM = (
    "You are a meticulous prediction-market analyst. You are given two markets "
    "from different exchanges that an automated system believes are the same "
    "contract for a cross-exchange arbitrage. Decide whether they resolve on the "
    "EXACTLY SAME underlying outcome: same event, same numeric threshold/strike, "
    "same resolution source/criteria, and the SAME settlement timing/window. "
    "Markets that look similar but differ in any of these (different date or "
    "window, different threshold, narrower/broader scope, a sub-event vs the "
    "whole, an emergency/qualified variant, a different person/team/company) are "
    "NOT the same — answer false. Also give the realistic settlement (resolution) "
    "date if determinable. Respond with ONLY a JSON object: "
    '{"same": true|false, "settlement_date": "YYYY-MM-DD" or null, '
    '"reason": "<one concise sentence>"}.'
)

_cache: dict[tuple[str, str], dict] = {}


def verify(text_a: str, text_b: str, api_key: str | None,
           model: str = _MODEL, timeout: float = 20.0) -> dict | None:
    """Return {"same","settlement_date","reason"} or None on any failure / no key."""
    if not api_key:
        return None
    ck = (text_a, text_b)
    if ck in _cache:
        return _cache[ck]
    body = json.dumps({
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": f"Market A (Polymarket): {text_a}\n"
                                        f"Market B (Kalshi): {text_b}"},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        _ENDPOINT, data=body,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        v = json.loads(content)
        out = {
            "same": bool(v.get("same")),
            "settlement_date": (v.get("settlement_date") or None),
            "reason": str(v.get("reason", ""))[:240],
        }
        _cache[ck] = out
        return out
    except Exception:
        return None  # fail-open


def verify_signal(signal: dict, api_key: str | None, **kw) -> dict | None:
    """Verify one alerter signal, composing the richest available context text."""
    def _txt(title_key, ev_key):
        ev = signal.get(ev_key) or ""
        t = signal.get(title_key) or ""
        return f"{ev} — {t}".strip(" —") if ev and ev != t else t
    return verify(_txt("poly_title", "poly_event_title"),
                  _txt("kalshi_title", "kalshi_event_title"), api_key, **kw)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print('usage: python ai_verify.py "market A text" "market B text"')
        raise SystemExit(2)
    try:
        from alerter import load_config
        _key = load_config().get("deepseek_api_key")
    except Exception:
        _key = None
    if not _key:
        print("No deepseek_api_key in alert_config.json — set it to use the verifier.")
        raise SystemExit(1)
    print(json.dumps(verify(sys.argv[1], sys.argv[2], _key), indent=2))
