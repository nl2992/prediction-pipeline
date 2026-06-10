#!/usr/bin/env python3
"""Debug specific failing pairs to understand why they don't match."""

from matcher import _tokens, _jaccard, _numeric_threshold, _named_entities

# Test pairs that are failing
failing_pairs = [
    ("Will JD Vance be the 2028 Republican presidential nominee?",
     "Vance to win the 2028 GOP nomination?", "PAIR-004"),
    ("Will a US Supreme Court justice retire or die in 2026?",
     "SCOTUS vacancy in 2026?", "PAIR-008"),
    ("Will Bitcoin be above $150,000 on December 31, 2026?",
     "BTC above $150k at year-end 2026?", "PAIR-011"),
    ("Will Bitcoin hit $175,000 at any point in 2026?",
     "BTC to touch $175k in 2026?", "PAIR-014"),
    ("Will a US spot Solana ETF have over $5B AUM by Dec 31, 2026?",
     "SOL ETFs above $5B AUM by end of 2026?", "PAIR-016"),
    ("Will Bitcoin dip below $80,000 at any point in 2026?",
     "BTC to stay above $80k for all of 2026?", "PAIR-017"),
    ("Will the Fed cut rates at the July 2026 FOMC meeting?",
     "Fed rate cut in July 2026?", "PAIR-019"),
    ("Will US CPI YoY for June 2026 print above 3.0%?",
     "June CPI above 3.0% (released July)?", "PAIR-021"),
    ("Will the S&P 500 close above 7,000 on Dec 31, 2026?",
     "S&P 500 above 7000 at year-end?", "PAIR-024"),
    ("Will the Indiana Pacers win the 2026 NBA Finals?",
     "Pacers to win 2026 NBA Championship?", "PAIR-028"),
    ("Will OpenAI release GPT-6 before Dec 31, 2026?",
     "GPT-6 released in 2026?", "PAIR-035"),
    ("Will GTA VI release before December 31, 2026?",
     "GTA 6 released in calendar 2026?", "PAIR-044"),
    ("Will the next James Bond actor be announced in 2026?",
     "New Bond actor named in 2026?", "PAIR-046"),
]

for pm_q, k_t, pair_id in failing_pairs[:5]:
    print(f"\n{'='*80}")
    print(f"{pair_id}")
    print(f"{'='*80}")
    print(f"PM: {pm_q}")
    print(f"K:  {k_t}")

    pm_toks = _tokens(pm_q)
    k_toks = _tokens(k_t)

    print(f"\nPM tokens: {sorted(pm_toks)}")
    print(f"K tokens:  {sorted(k_toks)}")

    similarity = _jaccard(pm_toks, k_toks)
    print(f"\nJaccard similarity: {similarity:.3f}")

    # Check thresholds
    pm_thresh = _numeric_threshold(pm_q)
    k_thresh = _numeric_threshold(k_t)
    print(f"\nPM threshold: {pm_thresh}")
    print(f"K threshold:  {k_thresh}")

    # Check entities
    pm_entities = _named_entities(pm_q)
    k_entities = _named_entities(k_t)
    print(f"\nPM entities: {pm_entities}")
    print(f"K entities:  {k_entities}")

    # Show overlap
    if pm_toks and k_toks:
        overlap = pm_toks & k_toks
        print(f"\nToken overlap: {sorted(overlap)}")
        print(f"Overlap count: {len(overlap)} / min({len(pm_toks)}, {len(k_toks)}) = {len(overlap)} / {min(len(pm_toks), len(k_toks))}")
