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

import hashlib
import json
import os
import pathlib
import sys
import time
import urllib.request


def resolve_api_key() -> str | None:
    """Return the DeepSeek key from the env, with a Windows User-registry fallback.

    A freshly spawned process (e.g. each Task Scheduler run of the alerter) does
    NOT inherit a setx'd User env var into os.environ, so reading os.environ alone
    left the verifier silently inert in production (#8). setx persists the var to
    HKCU\\Environment, so fall back to reading it there. This is still just the env
    var — nothing is written to the repo.
    """
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                val, _ = winreg.QueryValueEx(k, "DEEPSEEK_API_KEY")
            return val or None
        except Exception:
            return None
    return None

_ENDPOINT = "https://api.deepseek.com/chat/completions"
_MODEL = "deepseek-chat"
# Bump when _SYSTEM changes so cached verdicts from the old prompt are invalidated.
_PROMPT_VERSION = "v3-2026-06-22"
_CACHE_FILE = pathlib.Path(__file__).resolve().parent / "ai_verify_cache.json"
_CACHE_TTL_S = 14 * 24 * 3600  # backstop refresh; verdicts are otherwise stable
_disk: dict | None = None  # lazy-loaded persistent verdict cache
_SYSTEM = (
    "You are a meticulous prediction-market analyst. You are given two markets "
    "from different exchanges that an automated system believes are the same "
    "contract for a cross-exchange arbitrage. Judge strictly from each market's "
    "rules/description. Determine TWO things:\n"
    "1. same_event: do they resolve YES/NO on the EXACTLY SAME underlying event — "
    "same resolution criteria/source, same numeric threshold/strike, same scope? "
    "Anything that shifts what makes it resolve YES (a sub-event vs the whole, an "
    "emergency/qualified variant, a different person/team/company, a different "
    "threshold or scope) means same_event=false.\n"
    "2. settlement: do they resolve on the SAME real-world occurrence / deadline "
    "AS STATED IN THE MARKET RULES? IMPORTANT: the two exchanges often list "
    "DIFFERENT contractual expiry/close dates for the SAME event (one uses the "
    "event date, the other a far-future placeholder, or they differ by a day) — "
    "that alone does NOT make them different, so settlement_same must still be "
    "true. A gap of roughly a year between the two contractual expiry dates is "
    "almost always this placeholder pattern (e.g. one lists 2026-11-03 and the "
    "other 2027-11-03 for the SAME election/race) — do NOT treat that as a "
    "different window. Base settlement_same on what the QUESTION TEXT actually "
    "describes (same named race/contest/threshold/occurrence = same settlement), "
    "NOT on the expiry dates. Only set settlement_same=false when the RULES or "
    "question text describe genuinely different occurrences — a different deadline "
    "stated in the rules ('by June 30' vs 'by Dec 31'), or an explicitly different "
    "cycle/year in the question itself (e.g. a 2026 race vs a 2028 race). Give the "
    "realistic resolution date — when the outcome is actually decided — not the "
    "contractual placeholder.\n"
    "'same' (safe to arbitrage as identical) is true ONLY when same_event AND "
    "settlement_same are both true. Respond with ONLY a JSON object: "
    '{"same_event": bool, "poly_settlement": "YYYY-MM-DD"|null, '
    '"kalshi_settlement": "YYYY-MM-DD"|null, "settlement_same": bool, '
    '"same": bool, "reason": "<one concise sentence>"}.'
)

_cache: dict[tuple[str, str], dict] = {}


def _disk_key(text_a: str, text_b: str) -> str:
    return hashlib.sha256(f"{_PROMPT_VERSION}|{text_a}|{text_b}".encode("utf-8")).hexdigest()[:24]


def _disk_load() -> dict:
    global _disk
    if _disk is None:
        try:
            _disk = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            _disk = {}
    return _disk


def _disk_get(dk: str) -> dict | None:
    e = _disk_load().get(dk)
    if e and (time.time() - e.get("ts", 0)) < _CACHE_TTL_S:
        return e.get("v")
    return None


def _disk_put(dk: str, verdict: dict) -> None:
    global _disk
    try:
        d = _disk_load()
        d[dk] = {"v": verdict, "ts": time.time()}
        # Prune entries past the TTL so the file doesn't grow unbounded with dead
        # keys — vanished pairs, and the whole old key set after a _PROMPT_VERSION
        # bump (#49). The just-added entry (ts=now) always survives.
        cutoff = time.time() - _CACHE_TTL_S
        d = {k: e for k, e in d.items() if e.get("ts", 0) >= cutoff}
        _disk = d  # keep the in-memory cache consistent with what we write
        _CACHE_FILE.write_text(json.dumps(d), encoding="utf-8")
    except Exception:
        pass  # cache is best-effort; never break verification


def verify(text_a: str, text_b: str, api_key: str | None,
           model: str = _MODEL, timeout: float = 20.0) -> dict | None:
    """Return the verdict dict or None on any failure / no key. Verdicts are cached
    in-process AND on disk (keyed on prompt-version + the two market texts) so the
    every-20-min scan does not re-call DeepSeek for unchanged pairs."""
    if not api_key:
        return None
    ck = (text_a, text_b)
    if ck in _cache:
        return _cache[ck]
    dk = _disk_key(text_a, text_b)
    cached = _disk_get(dk)
    if cached is not None:
        _cache[ck] = cached
        return cached
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
        # Fail-open on a malformed-but-parseable verdict: a missing or non-boolean
        # 'same' is a degenerate model response, not a "different" verdict — treat it
        # as no-opinion (keep) rather than letting enforce drop a real arb (#44).
        if not isinstance(v, dict) or not isinstance(v.get("same"), bool):
            return None
        out = {
            "same": bool(v.get("same")),
            "same_event": bool(v.get("same_event")),
            "settlement_same": bool(v.get("settlement_same")),
            "poly_settlement": (v.get("poly_settlement") or None),
            "kalshi_settlement": (v.get("kalshi_settlement") or None),
            "reason": str(v.get("reason", ""))[:240],
        }
        _cache[ck] = out
        _disk_put(dk, out)
        return out
    except Exception:
        return None  # fail-open


def verify_signal(signal: dict, api_key: str | None, **kw) -> dict | None:
    """Verify one alerter signal, composing the richest available context text —
    including each market's contractual close date so the model can anchor the year
    and return an accurate implied settlement date."""
    def _txt(title_key, ev_key, close_key):
        ev = signal.get(ev_key) or ""
        t = signal.get(title_key) or ""
        base = f"{ev} — {t}".strip(" —") if ev and ev != t else t
        c = signal.get(close_key)
        return f"{base} (contractual expiry, may be a far-future placeholder: {c})" if c else base
    return verify(_txt("poly_title", "poly_event_title", "poly_close"),
                  _txt("kalshi_title", "kalshi_event_title", "kalshi_close"), api_key, **kw)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print('usage: python ai_verify.py "market A text" "market B text"')
        raise SystemExit(2)
    _key = resolve_api_key()
    if not _key:
        print("Set the DEEPSEEK_API_KEY environment variable to use the verifier.")
        raise SystemExit(1)
    print(json.dumps(verify(sys.argv[1], sys.argv[2], _key), indent=2))
