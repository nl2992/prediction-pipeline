#!/usr/bin/env python3
"""Debug the false positive PAIR-020 (date mismatch)."""

from matcher import _time_scopes, _tokens, _jaccard

pm_q = "Will the Fed cut rates at the September 2026 FOMC meeting?"
k_t = "Fed rate cut in July 2026?"

print(f"PM: {pm_q}")
print(f"K:  {k_t}")

# Check time scopes
pm_scopes = _time_scopes(pm_q)
k_scopes = _time_scopes(k_t)

print(f"\nPM time scopes: {pm_scopes}")
print(f"K time scopes:  {k_scopes}")

# Check tokens
pm_toks = _tokens(pm_q)
k_toks = _tokens(k_t)
sim = _jaccard(pm_toks, k_toks)

print(f"\nPM tokens: {sorted(pm_toks)}")
print(f"K tokens:  {sorted(k_toks)}")
print(f"Jaccard similarity: {sim:.3f}")

print(f"\nTime scopes match: {pm_scopes == k_scopes}")
print(f"Shared scopes: {pm_scopes & k_scopes}")
