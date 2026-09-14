# Recall expansion proposal — more matched pairs, more arb surface

Measured live on 2026-09-14 against both full open catalogs. Scripts used for the
numbers below were one-off probes (not committed); every number is reproducible by
re-running the ingest described in §1.

## TL;DR

The matcher is not the main bottleneck — **ingestion and matching strategy are**.
Production sees ~13% of Kalshi events and ~10% of Polymarket events, and its
text-similarity core structurally cannot pair the two largest overlap categories
(sports games and House races), whose titles share almost no tokens.

| | Production today | Measured opportunity |
|---|---|---|
| Kalshi events scanned | 1,503 (cap) of 11,718 | all 11,718 (107,790 markets) in **5 s** |
| Polymarket events scanned | first ~2,100 (silent 422 past offset 2100), keyword-filtered → 1,212 | all 20,782 (225,818 markets) in **19 s** |
| Matched pairs (baseline pools) | 50, of which 23 v2-endorsed | — |
| Sports game events joined | 0 | **287** by (teams, date) key alone |
| House districts joined | 0 | **350 / 350** by district code |

## 1. Ingestion fixes (small, high-leverage — do first)

**1a. Polymarket: silent truncation bug.** Gamma `/events` now returns
`422 offset too large, use /events/keyset` past offset ~2100. `search_events`
treats the error as end-of-catalog, so every scan has been missing ~90% of
Polymarket. Fix: paginate `/events/keyset?limit=500&closed=false` with
`after_cursor=<next_cursor>`. Full catalog in ~19 s, no keyword filter needed.
Also filter stale events (`endDate` in the past but `closed=false` still occurs).

**1b. Kalshi: one call per page, not one call per event.**
`/events?status=open&with_nested_markets=true&limit=200` returns events with
markets embedded: the whole catalog in ~60 requests / 5 s, versus 1,500
per-event requests that hit 429 backoff (observed ~50 events/min live). This
removes the need for `max_events_to_search`, `_ALWAYS_INCLUDE_SERIES`, and the
alerter's `CAP_LADDER`.

**1c. Snapshot the catalogs once per cycle, re-price only candidates.** Catalog
prices (Kalshi `yes_bid/ask_dollars`, Gamma `bestBid/bestAsk`) are good enough to
*screen*; fetch CLOB books only for pairs whose catalog edge is within a few
cents of positive.

## 2. Structured matchers ahead of the text matcher

The Jaccard matcher does not scale to full catalogs (95k × 166k markets runs for
many minutes in pure Python) and cannot match differently-worded equivalents.
Add deterministic *key joins* that run first; the text matcher handles the long
tail of what's left.

**2a. Sports games (largest surface).**
Kalshi `KXNFLGAME-26SEP14DENKC-DEN` ↔ Polymarket slug `nfl-den-kc-2026-09-15`
(moneyline outcomes `["Broncos","Chiefs"]`). Key = (league, team-code set, start
time). Titles ("Denver wins" vs "Broncos vs. Chiefs") share zero tokens today.
Measured 287 joined events with an exact-code join; MLB 35/40, NFL 15/17, plus
~40 soccer/esports/cricket leagues. Required safeguards (each observed in the probe):
- **League namespace** — `hou/cin` exists in both MLS and NFL; a league-agnostic
  join paired an MLS match with an NFL game.
- **Start time, not date** — MLB doubleheaders produce two events with the same
  teams and date.
- **Outcome by code, not name** — PM 2-way moneylines are one market whose token
  order follows the slug; soccer is 3 YES/NO markets (home/draw/away) ↔ Kalshi
  `-HOME/-AWAY/-TIE`. Name heuristics mis-mapped "Al Hilal"/"Al Gharafa".
- **Settlement rules** — verify regulation-time vs incl. OT per league (Kalshi
  soccer "Regulation Time Moneyline" ↔ PM 90-minute markets), and postponement
  handling; keep `ai_verify` enforce on this class until proven.
- Team-code alias table per league for the ~75% of Kalshi game events that miss
  an exact join (e.g. NCAAF 14/239, CS2 7/43).
- Extend beyond moneyline later: PM `spreads`/`totals` ↔ Kalshi `*SPREAD`/`*TOTAL`
  series join on the same game key + line value.

**2b. US House races.** Kalshi `KXHOUSERACE` "WY-AL House winner?" ↔ PM
"CA-22 House Election Winner"; key = district code, outcome = party *and*
candidate surname (15 of 675 outcomes had the same party but a different name —
must be rejected). All 350 districts join; ~53 small (≈1c) catalog edges
settling 2026-11-03.

**2c. Ladder/bucket synthesis (phase 3).** Kalshi threshold ladders ("Republicans
26+ pts", "At least 62%") vs PM range buckets ("Republican 30–35%"): P(≥X) on
Polymarket = sum of buckets ≥ X. This is a multi-leg arb (buy the bucket set vs
the Kalshi NO) and needs `book_arb` support for N legs. Currently excluded by
`_STATS_ONLY_RE` ("margin of victory", "vote share") on the stale assumption that
Polymarket has no equivalent — it now lists 563 Midterm-MOV events.

## 3. Accuracy fixes found while auditing current pairs

Of the 50 baseline pairs, 27 were v1 false positives (caught by v2 shadow, but
they still consume the 1-to-1 slot a correct pair could have taken):
- The individual fallback matches **bare outcome labels** ("Juan Soto" [MLB RBIs
  season leader] ↔ "Juan Soto: 1+ RBIs?" [tonight's game]; "Kenya" [Ebola case] ↔
  "Kenya wins" [cricket]). Fallback should score `event_title + outcome`, not the
  label alone.
- **v2 false positive:** PM "OpenAI **$1t+** IPO before 2027?" ↔ Kalshi "Who will
  IPO before 2027? OpenAI" was endorsed; a valuation-conditional IPO is a
  strictly narrower contract. Add a monetary-qualifier check to `contract_spec`.
- Event-group matching is 1-to-1 per event; allow one Kalshi event to match
  several PM events (e.g. Kalshi "Which companies will IPO" ↔ PM "IPOs before
  2027?" *and* standalone "Ripple IPO?" events), deduping at market level.
- Promote v2 from shadow to gate (the alerter already requires `v2_match`); the
  fallback false positives above are exactly what it rejects.

## Suggested order

1. 1a + 1b (hours; biggest single jump, removes caps and the 429 storms).
2. 3 (fallback context + $-qualifier) so the larger pools don't add noise.
3. 2a sports join for top leagues (MLB/NFL/NBA/NHL/EPL/MLS) with the safeguards,
   behind `ai_verify` enforce; then 2b House.
4. 2c ladder synthesis once N-leg execution math exists.

Caveat: all edges above are from **catalog** prices, not executable books; they
indicate where arb surface exists, not confirmed profit.
