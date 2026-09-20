"""Live coverage report: how much of each venue is ingested and matched.

Answers "are we at 100%?" with numbers, per dimension:

  * ingestion   — open events/markets fetched from each venue (vs the whole catalog)
  * sports      — recall of the structured game join against an INDEPENDENT
                  oracle (a Kalshi game "has a counterpart" when a Polymarket game
                  in the same time window shares a 4+-letter word with EVERY Kalshi
                  team name), plus precision via cross-venue price agreement
  * text        — (--text, ~4 min) pairs found / endorsed by the v2 referee, and
                  recall against an independent title/outcome-label oracle

The oracle is deliberately looser than the join and occasionally hits the wrong
sport, so treat its misses as a review list, not ground truth.

Usage:
    python -m tools.coverage_report            # ingestion + sports (~1 min)
    python -m tools.coverage_report --text     # + full text matching
    python -m tools.coverage_report --json
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone


def _pools():
    import discover as D
    from kalshi.client import KalshiClient
    from polymarket.client import PolymarketClient

    fa = datetime.now(timezone.utc).isoformat()
    t = time.time()
    kev = KalshiClient().get_all_events(status="open", with_nested_markets=True)
    k_s = time.time() - t
    t = time.time()
    pc = PolymarketClient()
    pev = pc.get_all_events()
    p_s = time.time() - t
    k = [D._k_snap(m, fa, ev.get("title", ""), ev.get("series_ticker", ""))
         for ev in kev for m in ev.get("markets") or []
         if m.get("status") in (None, "", "active", "open")]
    p = [D._p_snap_from_event(m, ev.get("title", ""), ev.get("slug") or ev.get("id", ""), fa)
         for ev in pev for m in ev.get("markets") or []
         if m.get("active", True) and not m.get("closed")]
    ingest = {
        "kalshi_events": len(kev), "kalshi_markets": len(k), "kalshi_seconds": round(k_s, 1),
        "poly_events": len(pev), "poly_markets": len(p), "poly_seconds": round(p_s, 1),
        "poly_catalog_complete": getattr(pc, "last_scan_complete", True),
    }
    return k, p, ingest


def _words(name: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", (name or "").lower().replace("reg time:", ""))
            if len(w) >= 4}


def sports_coverage(k, p) -> dict:
    from sports_match import _ET, _k_games, _p_games, match_sports_games

    now = datetime.now(timezone.utc)
    kg = [g for g in _k_games(k)
          if (g.start or datetime.combine(g.day, datetime.min.time(), tzinfo=timezone.utc))
          > now - timedelta(hours=3)]
    by_day = collections.defaultdict(list)
    for g in _p_games(p):
        by_day[g.start.astimezone(_ET).date()].append(g)

    def pm_names(g):
        if g.two_way is not None:
            return g.two_way.extra.get("outcome_labels") or []
        return [s.title for c, s in g.three_way.items() if c != "tie"]

    pairs = match_sports_games(k, p)
    joined = {x.kalshi.event_id for x in pairs}
    has, misses = [], []
    for g in kg:
        day = g.start.date() if g.start else g.day
        cands = [q for d in (day - timedelta(1), day, day + timedelta(1))
                 for q in by_day.get(d, ()) if g.time_ok(q.start)]
        ok = [q for q in cands
              if all(_words(g.name(c)) & set().union(*[_words(n) for n in pm_names(q)])
                     for c in g.teams if _words(g.name(c)))]
        if ok:
            has.append(g)
            if g.event_ticker not in joined:
                misses.append({"kalshi": g.event_ticker, "series": g.series,
                               "teams": sorted(g.name(c) for c in g.teams),
                               "oracle_pm": ok[0].slug, "pm_teams": pm_names(ok[0])})
    def _gaps(ps):
        return [abs(x.kalshi.orderbook.mid - x.poly.orderbook.best_bid) for x in ps
                if x.kalshi.orderbook.mid and x.poly.orderbook.best_bid
                and 0.02 < x.kalshi.orderbook.mid < 0.98 and x.poly.orderbook.best_bid != 0.5]

    line_pairs = [x for x in pairs if "line key" in (x.match_reason or "")]
    ml_pairs = [x for x in pairs if x not in line_pairs]
    gaps = _gaps(ml_pairs)
    line_gaps = _gaps(line_pairs)
    hit = sum(1 for g in has if g.event_ticker in joined)
    return {
        "kalshi_upcoming_games": len(kg),
        "games_with_pm_counterpart": len(has),
        "games_joined": len(joined),
        "recall_vs_oracle": round(hit / max(1, len(has)), 4),
        "contract_pairs": len(pairs),
        "moneyline_pairs": len(ml_pairs),
        "spread_total_pairs": len(line_pairs),
        "spread_total_gap_median": round(statistics.median(line_gaps), 4) if line_gaps else None,
        "price_gap_median": round(statistics.median(gaps), 4) if gaps else None,
        "price_gap_p90": round(sorted(gaps)[int(0.9 * len(gaps))], 4) if gaps else None,
        "price_gap_over_30c": sum(1 for x in gaps if x > 0.30),
        "misses_by_series": collections.Counter(m["series"] for m in misses).most_common(15),
        "misses": misses[:40],
    }


_STOP = set("the a an of in on at to for by will be is are what who which when how than more "
            "less and or with before after this next".split())


def _fold(t: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFKD", (t or "").lower()).encode("ascii", "ignore").decode()


def _wset(t: str) -> frozenset:
    return frozenset(w for w in re.split(r"[^a-z0-9]+", _fold(t)) if w and w not in _STOP)


def _jac(a, b) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def text_oracle(k, p) -> set[tuple[str, str]]:
    """Independent ground truth for text pairs (own tokenizer, not the matcher's):
    same outcome label (PM label == Kalshi yes_sub_title after normalisation),
    event titles with word Jaccard >= 0.5 and equal non-year numbers, not a
    single game; binary events need near-identical questions (Jaccard >= 0.7).
    Only unambiguous (single-candidate) hits count."""
    def sq(t):
        return re.sub(r"[^a-z0-9]", "", re.sub(r"\(.*?\)", "", _fold(t)))

    def nums(t):
        return set(re.findall(r"\d+", _fold(t))) - {str(y) for y in range(2020, 2040)}

    vs = re.compile(r"\bvs\.?\b")
    kidx = collections.defaultdict(list)
    for s in k:
        sub = s.extra.get("yes_sub_title") or ""
        if len(sq(sub)) >= 4:
            kidx[sq(sub)].append(s)
    truth = set()
    for ps in p:
        lab, pet = sq(ps.title), ps.extra.get("event_title") or ""
        if len(lab) < 4 or vs.search(pet.lower()) or re.search(r"\([+-]\d", ps.title):
            continue
        pe = _wset(pet)
        c = [ks for ks in kidx.get(lab, ())
             if _jac(pe, _wset(ks.extra.get("event_title"))) >= 0.5
             and nums(pet) == nums(ks.extra.get("event_title") or "")
             and not vs.search((ks.extra.get("event_title") or "").lower())]
        if len(c) == 1:
            truth.add((ps.market_id, c[0].market_id))
    df = collections.Counter(t for s in k for t in _wset(s.title))
    kw = collections.defaultdict(set)
    for i, s in enumerate(k):
        for t in _wset(s.title):
            kw[t].add(i)
    for ps in p:
        q = ps.extra.get("full_question") or ps.title
        if (ps.extra.get("event_title") or "").strip().lower() != q.strip().lower():
            continue
        w = _wset(q)
        if len(w) < 4:
            continue
        rare = sorted(w, key=lambda t: df.get(t, 0))[:2]
        cand = set.intersection(*[kw.get(t, set()) for t in rare])
        best = [k[i] for i in cand if _jac(w, _wset(k[i].title)) >= 0.7]
        if len(best) == 1:
            truth.add((ps.market_id, best[0].market_id))
    return truth


def text_coverage(k, p) -> dict:
    import discover as D
    from contract_spec import explain
    from sports_match import match_sports_games

    sp = match_sports_games(k, p)
    uk = {x.kalshi.market_id for x in sp}
    up = {x.poly.market_id for x in sp}
    t = time.time()
    tp = D._match_groups_then_individual([s for s in k if s.market_id not in uk],
                                         [s for s in p if s.market_id not in up], 0.30)
    paired = {(x.poly.market_id, x.kalshi.market_id) for x in tp}
    endorsed = {(x.poly.market_id, x.kalshi.market_id) for x in tp if explain(x.poly, x.kalshi).match}
    truth = text_oracle(k, p)
    n = max(1, len(truth))
    return {"text_pairs": len(tp), "text_v2_endorsed": len(endorsed),
            "text_seconds": round(time.time() - t, 1),
            "text_oracle_pairs": len(truth),
            # MATCHED = the matcher found the pair; ENDORSED = the v2 referee also
            # confirmed it (the alerter requires v2, so the gap costs alerts).
            "text_recall_matched": round(len(truth & paired) / n, 4),
            "text_recall_vs_oracle": round(len(truth & endorsed) / n, 4),
            "text_paired_but_v2_rejected": len((truth & paired) - endorsed)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--text", action="store_true", help="also run full text matching (~3 min)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    import logging
    logging.disable(logging.WARNING)

    k, p, ingest = _pools()
    out = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "ingestion": ingest, "sports": sports_coverage(k, p)}
    if a.text:
        out["text"] = text_coverage(k, p)
    if a.json:
        print(json.dumps(out, indent=2, default=str))
        return 0
    i, s = out["ingestion"], out["sports"]
    print(f"Coverage report {out['at']}")
    print(f"  Ingestion  Kalshi {i['kalshi_events']:,} events / {i['kalshi_markets']:,} markets ({i['kalshi_seconds']}s)"
          f"   Polymarket {i['poly_events']:,} events / {i['poly_markets']:,} markets ({i['poly_seconds']}s)"
          f"{'' if i['poly_catalog_complete'] else '   PARTIAL!'}")
    print(f"  Sports     {s['games_joined']} games joined ({s['contract_pairs']} contract pairs: "
          f"{s['moneyline_pairs']} moneyline + {s['spread_total_pairs']} spread/total); "
          f"recall {s['recall_vs_oracle']:.1%} of {s['games_with_pm_counterpart']} games with a PM counterpart "
          f"(of {s['kalshi_upcoming_games']} upcoming Kalshi games)")
    print(f"             price gap median {s['price_gap_median']}, p90 {s['price_gap_p90']}, >30c: {s['price_gap_over_30c']}"
          f"   (spread/total median {s['spread_total_gap_median']})")
    print(f"             misses by series: {s['misses_by_series']}")
    if "text" in out:
        t = out["text"]
        print(f"  Text       {t['text_pairs']:,} pairs, {t['text_v2_endorsed']:,} v2-endorsed ({t['text_seconds']}s)")
        print(f"             recall of {t['text_oracle_pairs']:,} oracle pairs: matched {t['text_recall_matched']:.1%}, "
              f"v2-endorsed {t['text_recall_vs_oracle']:.1%} ({t['text_paired_but_v2_rejected']} matched but v2-rejected)")
        print("             NOTE: a hand-labelled sample of 60 misses was 63% real / 37% oracle error,")
        print("                   so the reachable ceiling is ~91%, not 100%.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
