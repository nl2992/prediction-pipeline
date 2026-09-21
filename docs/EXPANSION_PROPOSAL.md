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

---

## Progress log

Status key: ✅ done · 🟡 partial · ⬜ open. Newest first.

### 2026-09-21 (pass 15) — the coverage ledger: what happens to every ingested market

**Why.** "Is it 100%?" has been answered so far with recall against a reference
set. That hides the blunter question: we ingest ~104k Kalshi and ~148k
Polymarket markets — what happens to each one? New tool:
`python -m tools.coverage_ledger` (~4 min, no order books).

**Measured (live, 2026-09-21).**

| | Kalshi | Polymarket |
|---|---|---|
| Ingested markets | 103,566 | 148,051 |
| Classes (series / sportsMarketType) | 3,848 | 153 |
| **Matched** | **8,309** | **8,309** |
| Held out on purpose (stats-only, parlay, ended) | ~1,100 | 19,895 |
| Classes that never match anything | 3,040 | 139 |
| Markets in those classes | **78,746 (76%)** | **76,055 (51%)** |

**The answer to "100%":** ingestion is 100% and every tradeable market is
considered, but **~8,300 pairs is close to the real size of the overlap** — not
100% of either catalog, because each venue lists tens of thousands of contracts
the other simply does not have.

| Never matches — Kalshi | Markets | Never matches — Polymarket | Markets |
|---|---|---|---|
| `KXVOTEGENERAL` (vote-percent ladders) | 8,991 | `soccer_exact_score` | 10,214 |
| `KXMIDTERMMOV` (margin buckets) | 4,552 | corner markets (4 classes) | 23,272 |
| `KXNASDAQ100U` (hourly index ranges) | 2,800 | `soccer_team_totals` (+ halves) | 13,512 |
| `KXMIDTERMVOTETURN` | 2,776 | `first/second_half_totals` (soccer) | 4,986 |
| NCAAF rank polls / seeds / conf matchups | ~4,400 | — | — |

**Three honest categories in that list:**

1. **No counterpart at all** — corners and exact score (Kalshi lists neither);
   hourly NASDAQ ranges and AP poll rankings (Polymarket lists neither). Nothing
   to fix; these are the shape of the two product catalogs.
2. **Counterpart exists but only for other sports** — Polymarket's soccer team
   totals and soccer halves (13.5k + 5.0k markets) have Kalshi equivalents for
   NFL/WNBA only. Matched the moment Kalshi lists soccer versions; the class
   table already handles them.
3. **A genuine, sizeable gap: `KXMIDTERMMOV` (4,552 markets).** Polymarket DOES
   list midterm margin-of-victory markets. They do not pair 1-to-1 because
   Kalshi is cumulative ("Republicans 26+ pts") and Polymarket is bucketed
   ("Republican 25-30%"). Pairing them needs multi-leg synthesis — P(≥26) is the
   SUM of the buckets above 26 — which means N-leg support in `book_arb` and the
   alerter, not a matcher rule. This is the §2c item from pass 1, still the
   largest matchable class left.

**Also visible:** recall gaps inside classes that do match are concentrated in
college football (`KXNCAAFSPREAD` 22/1,651, `KXNCAAFGAME` 1/468) — Polymarket
lists far fewer college games, and at this hour (Sunday night) next Saturday's
slate is not up yet, so most have no counterpart to find.

### 2026-09-21 (pass 14) — funnel audit: what gets dropped between "pull all" and "match all"

**Why.** Every pass measured recall of the matcher. None measured the FUNNEL —
how many markets never reach the matcher at all, and why.

| Stage | Kalshi | Polymarket |
|---|---|---|
| Catalog (nested) | 10,882 events / 105,544 markets | 17,975 events / 214,725 markets |
| Dropped: not tradeable | 2,120 (finalized/inactive/closed/initialized) | 66,530 (inactive or closed) |
| Kept | 103,424 (98.0%) | 148,195 (69.0%) |
| **Of the kept: end date already PAST** | — | **23,748 (16%)** |
| Of the kept: beyond a 730-day horizon | 3,384 | 256 |

**Two findings.**

1. **Ended-but-open Polymarket markets (23,748).** Resolution is pending, so the
   catalog still says open, but the event has happened — they cannot be a live
   counterpart and their stale last-trade price manufactures edges. A live scan
   carried a **+5.7c "arb" on an Andy Burnham speech market that ended five days
   earlier**, and it had passed the pass-13 band review as genuine. Now tagged
   `match_excluded="ended"`: still ingested (coverage accounting stays 100%,
   which `health.py` enforces), never matched. Sports are exempt while
   `gameStartTime` is ahead, since a rescheduled game keeps a stale end date.
2. **The 730-day horizon** used to drop 3,384 long-dated Kalshi markets —
   including the **2028 general election**, which closes just past that window.
   Verified already fixed in parallel work (`days=None` in discover and the
   alerter); recorded here because the funnel is where it shows up.

**Measured (live):** signals on an already-ended Polymarket leg **4 → 0**; pairs
47 → 26 (the remainder end within a day, or are Kalshi-side). Top of the list
after: CMA vocalist, Venezuela leadership ×2, French primary, Trump UN words,
recession definition, two Oscar supporting-actor contracts.

### 2026-09-21 (pass 13) — audited the 3–5c band, where the alerter's threshold sits

**Why this band.** `MIN_NET_EMAIL` is 3c, so 3–5c is the zone that decides what
actually reaches the operator's inbox — a mismatch here is not academic.

**Method.** Hand-classified all 25 live signals in the band: **19 genuine, 6
mismatches** across four classes.

| Edge | Polymarket | Kalshi | Fault |
|---|---|---|---|
| +5.0c | how many **leave** the cabinet | Trump **fire** 3 members | dismissal vs departure |
| +4.7c | Tua **Comeback** Player of the Year | **Offensive** Player of the Year | award category |
| +4.4c, +3.5c, +3.4c | best **AI model** | best **coding** model | qualified domain vs general |
| +4.2c | Ocampos most **assists** | lead Liga MX in **goals** | stat category (assists was missing) |

The award-category rule also separates Best Actor / Best Actress / supporting
vs lead, and the sports awards (MVP, Rookie, Coach, Comeback, OPOTY, DPOY).

**Measured (live, same catalogs):**

| | before | after |
|---|---|---|
| 3–5c band | 25 | **18** |
| Above 3c | 58 | 51 |
| Positive-net | 497 | 485 |

All 18 survivors read as genuine: fantasy WR leader, Truth Social post buckets,
Ecuador LigaPro, Google/Spotify rank markets, Greek PM, Best Actress, DPOY, a
Senate vote, a French runoff qualifier, a NASCAR title.

**Cumulative over passes 12–13:** the live arb list went from 557 positive-net
signals with 13 of the top 16 wrong, to 485 with a top-of-book and 3–5c band
that both survive hand review — 16 mismatch classes removed in two passes.

### 2026-09-21 (pass 12) — measured the ARBS, not the pairs; cleaned the top of the list

**Why.** Coverage has been the metric for ten passes, but the project exists to
find tradable edges. This pass ran the full pipeline with LIVE order books,
computed the alerter's signals, and hand-checked the largest edges — the place
where a mismatch actually costs money (adverse selection: a wrong pair looks
like a big edge).

**Baseline (live, 2026-09-21):** 8,099 pairs → 557 positive-net signals
(265 >1c, 88 >3c). **13 of the top 16 edges were mismatches.**

**Two structural findings.**

1. **Sports pairs produce almost no arbitrage.** Of 1,127 priced sports pairs,
   just 5 had a positive edge, the best +2.5c, all on illiquid esports books
   with <10 contracts of depth. Cross-venue sports pricing is efficient (median
   gap 1c, under the fee). The sports work bought *coverage and monitoring*, not
   opportunities — worth saying plainly.
2. **Every edge above 3c came from text-matched pairs**, so top-of-book
   precision is what determines whether the alert list is tradable.

**Twelve new vetoes, each traced to a specific bogus edge** (edge shown as it
appeared): ordinal place vs top-N (+74c) · award nomination vs win (+18c) ·
different awards body (+19c) · different county (+19c) · division/conference vs
league title (+19c, +17c) · ordinal-best rank (+19c, +15c) · legislative chamber
(+15c) · superlative stat vs advancement (+16c) · school qualifier, Texas A&M vs
Texas (+11c) · exit poll vs election result (+21c) · matchup vs single-team
advancement (+14c) · women's vs men's competition (+9c) · period granularity,
day/week/month and same-grain-different-dates (+9c). Plus: "Victory" counts as
1st place, "finish 4th" parses without "place", and the weather settlement flag
now covers rain/snow.

**Result on the same live catalogs:**

| | before | after |
|---|---|---|
| Positive-net signals | 557 | 497 |
| Above 3c | 88 | 58 |
| Above 5c | 63 | 33 |
| Top-16 that are mismatches | 13 | ~2 of top 10 |

Fewer signals is the *point*: the removed ones were wrong. The top of the list
now reads as genuine (CMA vocalist, French socialist primary, Venezuela
leadership, Trump UN speech words, an unemployment rung).

**Residual, documented rather than patched:** combo markets pairing different
combinations (Alaska Gov/Sen), relegation vs champion, and "US recession by end
of 2027" vs an NBER-dated recession — that last one needs the settlement check,
not a title rule. 21 new tests, one per class.

### 2026-09-21 (pass 11) — three more contract classes, and one refused on purpose

**Proposal.** Pass 10 wired nine contract classes; a survey of every Polymarket
`sportsMarketType` against Kalshi's series families showed what was still
uncovered. Three classes are genuinely matchable, one looks matchable and is not.

| Class | Kalshi | Polymarket | Verdict |
|---|---|---|---|
| Half TEAM totals | `KX*1HTEAMTOTAL` (320 mkts) | `first_half_team_totals` (416) | ✅ matched |
| Soccer halftime / 2nd-half result | `KX*1H` / `KX*2H` per-team legs | `soccer_halftime_result` (1,851), `soccer_second_half_result` (1,914) | ✅ matched per team; Kalshi's TIE leg never pairs |
| Football / basketball half WINNER | `KXNCAAF1H` (702), `KXWNBA1HWINNER`, `KXNFL1H` | `first_half_moneyline` (33), `second_half_moneyline` (67) | ❌ **refused**: every Kalshi half-winner event carries a tie leg (252/252 live) while PM's half moneyline is 2-way, so a drawn half settles differently |

**Also implemented:** team subjects now resolve through the game join's already
verified mapping, so a line market may say "Denver" on one venue and "Broncos"
on the other, and the Polymarket subject parser strips trailing `1H`/`2H`
markers ("Broncos 1H O/U 6.5" → Broncos).

**Live result: 0 pairs from the new classes right now — and that is data, not a
bug.** Kalshi lists 1H team totals for Sep 27 games (which ARE joined);
Polymarket lists them for Oct 25 games (not yet joined). They pair when the two
venues' windows overlap. Unit tests pin each class so the wiring stays honest.

**Sports totals at this run:** 1,130 contract pairs (765 moneyline, 91 spread,
241 total, 33 team total); cross-venue price gap median 1c, p90 8.5c, one pair
above 30c.

### 2026-09-21 — live verification of the pass-10 line classes

Ran the join against the live catalogs at 03:06 UTC to confirm the nine
contract classes actually fire (not just pass their unit tests):

| Class | Pairs live | Note |
|---|---|---|
| moneyline | 766 | |
| spreads / totals | 87 / 241 | |
| team totals | 33 | proves the class-table path end to end |
| 1H/2H spreads & totals | 0 | **data, not a bug**: the only joined game with PM first-half totals (cfb-librty-coast) has no Kalshi `…1HTOTAL` event |
| MLB player props (HRR, HR) | 0 | no joined game carried PM `baseball_player_*` markets at that hour |

Finding worth recording: `KXNCAAF1H` (702 markets) is a first-half **WINNER**
market (3-way, with a tie leg), not a half total — college football halves are
named differently from `KXNFL1HTOTAL`. Polymarket has no football half-winner
class, so it stays unmatched by design; its soccer equivalent
(`soccer_halftime_result`) is a candidate for a future pass.

Coverage at that run: Kalshi 10,980 events / 104,908 markets; Polymarket 18,034
events / 149,068 markets; 601 games joined, 1,124 contract pairs; sports recall
95.5%; cross-venue price gap median 1c (spread/total 1.5c).

### 2026-09-21 (pass 10) — diagnosed the remaining ~8%: bookkeeping caps, handles, accents, v2 rungs

**Diagnosis.** Passes 7–9 put text recall AT the hand-labelled ceiling (~91.9%),
so this pass classified **every** remaining oracle miss by the exact gate that
rejected it (an instrumented port of `is_compatible_match`, greedy-theft
tracing, hand-labelling of each bucket — same method as pass 7). Of 363 misses:
~88 (24%) real (down from pass 7's 63% — the big classes were already fixed),
210 correct v1 rejections, 65 oracle noise (in 63 of 65 "stolen-slot" cases the
thief pairing was the correct sibling, v2-endorsed — the oracle picked the
wrong duplicate). Bucket detail: 252 v1-vetoed / 98 never-candidate /
11 below-gate / 2 v2-rejected.

**Implemented (one regression test per class, `tests/test_pass10_recall.py`).**

| # | Fix | Where |
|---|---|---|
| 1 | Close-time 400-day cap + one-sided deadline veto no longer fire on near-identical titles (Jaccard ≥ 0.8) — Mamdani min-wage (verbatim titles, PM formal 2031 expiry vs Kalshi 2027), "OpenAI or Anthropic IPO first?" word-order pair; `settlement_risk` still flags the horizon | `matcher.py` |
| 2 | Esports handles: group-path label lift allows verbatim digit labels ("f0rsakeN"); `_same_outcome_label` floor 5→3 ("s0pp", "bang"); "VALORANT Champions"/"Tournament MVP" added to generic name terms so they stop producing phantom people | `discover.py`, `matcher.py` |
| 3 | `_tokens` NFKD-folds accents ("Vinícius Júnior" = "Vinicius Junior", "Te Pāti Māori"); group path squash-compares labels, reconciling hyphen variants ("Jung-Hwan Lee") and party suffixes ("Kelly Ayotte (R)") | `matcher.py`, `discover.py` |
| 4 | Identical event title + identical outcome label is a compatibility fast path (still subject to `context_veto`): Clarity Act paraphrases ("a crypto market structure bill (as defined in KXCRYPTOSTRUCTURE)"), Grüne 2nd-place; election-domain one-sided veto skipped when the label matches; "VP" added to the office gazetteer (Hegseth VP-vs-President precision) | `matcher.py` |
| 5 | v2: parentheticals stripped before name extraction ("Gary (Stephen Wilson Jr.)" — pass 7's 2 residual over-fires); identical DISTINCTIVE label (≥4 chars) is sufficient acceptance evidence after all hard gates; new count-rung gate rejects disjoint integer cutoffs ("Above 68" vs "Above 66" rode token similarity 0.75 to a false endorsement), with ±1 tolerance for venue convention ("20+" == "over 20", audited fixture PAIR-049) and comma/decimal-safe parsing | `contract_spec.py` |
| 6 | Sports: Swiss-hockey aliases (EHC Kloten↔Kloten Flyers, SC Langnau Tigers↔SCL Tigers), county-cricket one-day cups survive a league-vote tie when the join is exact-code (Leicestershire↔Middlesex); line join extended to first/second-half spreads & totals, team totals and baseball HRR/HR player props — each class keyed by its EXACT Kalshi event ticker, Kalshi `floor_strike`/`cap_strike` plumbed through as the line | `sports_match.py`, `discover.py` |
| 7 | Precision: earnings-call company gazetteer (Costco↔PepsiCo GLP-1 FP — the org gate was tech-only); college vs pro basketball split in `_sports_league` (Miami FP); one-sided finishing-position veto ("3rd Place" outcome vs "win the first round?" on the same contestant — `_finish_places` was also case-sensitive, "3rd Place" capital-P never matched) | `matcher.py` |

**Measured** (live A/B; before = 2026-09-20 catalog, after = 2026-09-21 — Monday
vs Sunday composition differs, so read the recall rates, not the pair counts).

| | pass 9 (9/20) | pass 10 (9/21) |
|---|---|---|
| Oracle pairs MATCHED | 91.9% | **92.6%** |
| Oracle pairs V2-ENDORSED | 91.9% | **92.6%** |
| Matched but v2-rejected | 2 | **1** |
| Sports recall vs oracle | 95.3% | **97.0%** |
| Sports price agreement | median 0.75c | median 1c, >30c: **1** |
| Text pairs / endorsed | 7,964 / 7,545 | 7,448 / 7,148 |

The 0.7pt text gain is above the pass-7 hand-labelled ceiling of ~91.9%,
consistent with the diagnosed real-miss classes now matching; the endorsed
rate tracks matched exactly again (nothing lost between matcher and alert
gate). No regression in any audited fixture (912 hermetic tests green,
including the v2 endorsed-audit precision floors).

**Caveats, recorded deliberately:** (a) the near-identical-title relaxations
(#1) trust text evidence over close-time bookkeeping — the year-differing
variants ("...in 2026?" vs "...in 2027?") stay under the cap because their
Jaccard is < 0.8; (b) the new sports line/prop classes (first-half, team
totals, HRR/HR) are joined by exact event key + equal line and inherit the
moneyline join's game verification, but their settlement-source parity is
younger than moneylines' — the alerter's AI settlement check remains the
enforcing gate on this class; (c) the rung gate is v2-side only; v1 continues
to rely on its price-led mode and stat-line vetoes for rung discipline.



**Proposal.** Text recall (91.9%) now sits AT the hand-labelled oracle ceiling
(~91%), so more wording rules would chase the oracle's own errors. The
uncovered surface left is contract **types**, not wording — and both venues
list spreads and totals for the same games, which the pipeline matched 0% of.

**Structure (both venues, confirmed live).**

| | Kalshi | Polymarket |
|---|---|---|
| Spread | own event `KXMLSSPREAD-26SEP26VANDCU`, market "Vancouver wins by more than 2.5 goals?" | market in the game event, "Spread: Rays (-1.5)", outcomes `[Rays, Yankees]` (token 0 covers) |
| Total | own event `KXMLSTOTAL-…`, "Will over 2.5 goals be scored?" | "O/U 7.5", outcomes `[Over, Under]` (token 0 = Over) |

Kalshi's spread/total events share the GAME KEY, so they attach to a game the
moneyline join has already verified and inherit its team mapping, league vote,
outcome-shape and start-time checks — no new matching risk.

**Implemented** (`sports_match.py`):

* `_k_line_events` indexes Kalshi SPREAD/TOTAL markets by (series prefix, game key).
  The prefix must match EXACTLY, which keeps first-half and team-total variants
  (`KXNFL1HTEAMTOTAL`) out — they share the key but are different contracts.
* `_match_lines` pairs a Kalshi spread with the PM spread market whose token 0
  is the same team and whose line is EQUAL, and a Kalshi total with the PM
  total on an equal line.
* The line is read from the PM market slug (`-total-4pt5`, `-spread-away-1pt5`),
  which is unambiguous; the title is a fallback because PM titles sometimes
  fall back to the whole question ("Red Wings vs. Penguins: O/U 4.5").
* 4 new tests: pairing, unequal line rejected, wrong side rejected, first-half /
  team-total series not joined.

**Measured (live).**

| | before | after |
|---|---|---|
| Sports contract pairs | 1,305 | **1,829** (1,271 moneyline + **558 spread/total**) |
| Spread/total pairs | 0 | 558 |
| Cross-venue price gap (spread/total) | — | median **0.5c** |
| Sports game recall | 95–97% | unchanged (same games, more contracts each) |

The spread/total tail is wider than moneylines (53 pairs > 15c) but inspection
showed untraded Polymarket books on obscure college games — every line on such
a game sits near 0.5 — not mis-mapping; the live order books the alerter uses
replace those catalog prices.

### 2026-09-19 (pass 8) — closed the v2 gate gap (endorsed 87.7% → 91.9%)

**Diagnosis.** Pass 7 split MATCHED from V2-ENDORSED and found 188 oracle pairs
the matcher found but the v2 referee rejected. The alerter requires v2, so those
pairs can never produce an alert — the single largest remaining lever. Every one
of the 188 was v2 over-firing on a true pair:

| n | v2 reason | Reality |
|---|---|---|
| 105 | "player-prop vs non-prop" | "QB Points Leader" vs "Season Top QB" — two leader markets (the same class fixed in v1 in pass 5) |
| 47 | "selected-name mismatch" | v2 extracted "new jersey" from the Kalshi question instead of the candidate |
| 28 | "winner-subject mismatch" | party-worded question vs candidate name — the candidate is in `yes_sub_title`, which v2 never saw |
| 7 | "different person" | fired on *identical* strings ("Yokohama DeNA BayStars") |
| 1 | similarity below gate | genuine |

**Implemented.**

* `ContractSpec` gains `outcome_label` (Kalshi `yes_sub_title` / PM market title).
* An exact label match suppresses the winner-subject, selected-name and
  first-name-collision heuristics — they exist to separate DIFFERENT contestants.
* Leader-vs-leader exemption for the prop gate, mirroring `matcher.py`.
* **New v2 guard from the same evidence:** different named outcome ("Bo Nix" vs
  "Patrick Mahomes", Lakers vs "Los Angeles C"), tolerant of short forms,
  spellings, middle names and second surnames.

**Two rejected attempts at that guard** (both caught by the live A/B before
landing): strict containment dropped 134 true pairs ("Ben"/"Benjamin
Silverman", "Cam"/"Cameron Davis", "Jaxon"/"Jaxson Smith-Njigba"); requiring an
equal last token still dropped 46 ("Justin J. Pearson", "Kendor Gregorio Macías
Martínez"). The landed version accepts a token subset sharing ≥ 2 words.

**Measured** (same pools; live in the last column):

| | pass 7 | pass 8 | live |
|---|---|---|---|
| Oracle pairs MATCHED | 91.7% | 91.7% | 91.9% |
| Oracle pairs V2-ENDORSED | 87.4% | **91.7%** | **91.9%** |
| Matched but v2-rejected | 188 | **1** | 1 |
| Endorsed pairs, total | 9,307 | **9,814** (+507) | — |
| Newly (correctly) rejected | — | 18 (Lakers vs Clippers, Chargers vs Rams, Giants vs Jets, opposite chamber-control combos) | — |

**Caveat, recorded deliberately:** v1 and v2 now share the outcome-label logic,
so v2 is less independent on those classes than it was. It still runs its own
extraction for everything else, and the alerter's AI settlement check remains a
third, genuinely independent gate.

**Where this leaves coverage:** ingestion and matching scope are 100%; matched
and endorsed recall are now the same number, so nothing is lost between the
matcher and the alert gate. The remaining ~8% of oracle misses is ~63% real
(pass-7 hand label) → true recall ≈ 95%.

### 2026-09-19 (pass 7) — hand-labelled the misses; matched recall 91.9%

**Proposal.** Stop adding rules blind. The oracle is looser than the matcher, so
an unknown share of "misses" are the oracle's own errors. Hand-label a random
sample, measure the reachable ceiling, and fix only what is real.

**Hand-label (60 random misses, classified by reading both contracts):**

| Verdict | n | Examples |
|---|---|---|
| **Real miss** | 38 (63%) | Fantasy season leaders ("highest-scoring QB of the season" / "Season Top QB") ×10; Senate + AG races (party question + candidate label vs "Democratics win … ? <name>") ×8; CFB Playoff top-4 seeds ×5; VALORANT Champions 2026 MVP / "Champions Shanghai MVP" ×6; MLB home-run leader ×2; Dune cinematography, LA-05 nominee, Clarity Act, PODEMOS, Ro Khanna, NPB champion, Putin–Zelenskyy meeting |
| **Oracle error** | 22 (37%) | group winner vs tournament champion ×5; "advance to" vs "win" conference finals ×3; AI model Sep/Oct/Nov vs Dec 31 ×3; league phase vs champion ×2; county vs statewide ×2; Game Awards different categories ×2; US vs global (Google, Netflix); Coachella perform vs headline; Cracker Barrel vs Costco; Trump–Putin vs Putin–Zelenskyy; French candidate list vs winning |

**So the ceiling is ~91% of oracle pairs, not 100%** — the remaining 37% of
misses *should* be misses.

**Also discovered:** a matched pair can still be counted as missed because the
**v2 referee** rejects it. The report now separates MATCHED from V2-ENDORSED.

**Fixed (all from the real-miss classes):**

| Fix | Note |
|---|---|
| **v1 bug:** "(2026)" after a title parsed as a point spread ("(-1.5)") | NPB/KBO champions hit the spread-vs-moneyline veto; spreads now need a sign or decimal |
| **v1 bug:** "Dune: Part **Three**" read as a "threes" stat line, valued from "99th Academy Awards" / a ticker suffix | stat numbers must sit next to the stat word |
| "Top 4 **Seeds**" (plural) never matched "top 4 seed" | every CFB seed pair was rejected as a different seed |
| Leader markets worded without "lead" ("hit the **most** home runs", "**highest-scoring** RB") | now leader markets; "yards/receptions/touchdowns" count as leader stats on BOTH wordings — a first attempt made this asymmetric and dropped 474 true NCAAF/NFL leader pairs (caught by the A/B, reverted within the pass) |
| New veto: different stat category | "Week 2 Receiving Yards Leader" vs "Leader in fantasy points for Week 2" |

**Measured** (same pools; live in the last column):

| | pass 6 | pass 7 | live |
|---|---|---|---|
| Oracle pairs MATCHED | 85.3% | **91.7%** | 91.9% |
| Oracle pairs v2-ENDORSED | 86.3%* | 87.4% | 87.7% |
| Matched but v2-rejected | — | 188 | 190 |
| Sports recall | 96.2% | 96.2% | 96.9% |

\* pass 6 measured endorsement only; the matched/endorsed split is new in pass 7.

**Adjusted for oracle error** (63% of misses real): true recall ≈ **95%** of the
pairs that actually exist in the oracle's slice.

**Next:** the 190 matched-but-v2-rejected pairs are now the largest single
lever — they are found by the matcher but the alerter drops them because it
requires v2 endorsement.

### 2026-09-19 (pass 6) — market-level assignment, 8 precision rules (85% → 86.3%)

**Diagnosis.** After pass 5 the biggest bucket was still 231 pairs that pass
every veto yet stay unpaired, plus 105 leader-vs-leader pairs. Causes:

* **Assignment.** Events were matched 1-1 (then capped at 3). Whenever a venue
  lists one race several ways — a statewide race beside its county sub-events,
  "… (by individual)" duplicates — the wrong variant claimed the Kalshi event
  and its outcomes never got another chance.
* **v1 bug.** `_stat_thresholds` read a long slug digit run
  ("…-20260727173335231") as a stat line, which silently disabled the
  leader-vs-leader exemption ("MLB: Home Runs Leader" vs "lead Pro Baseball in
  home runs").

**Implemented.**

| Fix | Where |
|---|---|
| Score outcome pairs for EVERY candidate event pairing, then assign greedily at **market** level (each market used once) — no event-level cap | `discover.py` |
| Ignore 5+-digit numbers (slug ids) and calendar years in stat lines | `matcher.py` |
| 8 new `context_veto` rules, each from a wrong pair the wider assignment surfaced: different district (CA-04 vs MO-04); different stat-line value (400+ vs 3,500+ passing yards, 0.75 tolerance so "more than 84.5 games" == "at least 85"); reach a stage vs win it; finishing scope (top-5 vs winner, "#1" vs "Top 5"); playoff seed number; vote-share threshold vs winning; week vs season scope; size-conditional contract ("$1t+ IPO" vs plain IPO — bare price levels excluded, they are the contract itself) | `matcher.py` |

**Measured** (same pools; live report in the last column):

| | pass 5 | pass 6 | live 2026-09-19 |
|---|---|---|---|
| Text recall vs oracle | 85.1% | **86.3%** | 86.5% |
| Endorsed pairs, total | 8,805 | **9,341** | — |
| Sports recall | 96.2% | 96.2% | 96.2% |
| Matching time | 206 s | 288 s | 206 s |

Wider assignment costs runtime (outcome scoring now runs for every candidate
event pair) and it surfaces wrong pairs as fast as right ones — every one of
the 8 rules above came from reviewing what the wider assignment let through.

### 2026-09-19 (pass 5) — text recall 81% → 85%, and two more v1 bugs

**Method.** Trace every oracle miss to the exact line that rejects it, rank by
count, then judge each bucket as a real miss or a correct rejection (the oracle
is looser than the matcher).

**Fixed (real misses).**

| Bucket | Cause | Fix |
|---|---|---|
| 253 passed every veto yet stayed unpaired | `is_close_time_compatible` capped non-sports pairs at 400 days, but open-ended succession markets ("next Press Secretary", "Next James Bond actor") carry a formal Kalshi expiry (end of term, 2029) against a calendar-year PM close | pair them when the outcome name matches; `settlement_risk` flags the differing deadlines so alerts skip them |
| 86 `stat_leader` | "QB Points Leader" vs "Season Top QB" | "Season Top QB/RB/WR/TE" and "Golden Boot" are leader markets |
| 51 candidacy | "Who will **announce Presidential run**" not recognised | broadened to "announce/declare … run/candidacy/bid" |
| 51 deadline asymmetry, 46 foreign jurisdiction, 39+30+19+10 name heuristics | these separate DIFFERENT contestants, but fired on pairs whose outcome name is identical | exact label identity (PM label == Kalshi `yes_sub_title`) overrides them; semantics still gated by `context_veto` |
| **v1 bug** | `_stat_thresholds` read "run before 2027" as `runs = 2027`, and a season span "2026-27" as a 27 line | years and season spans excluded from stat lines |
| **v1 bug** | a DEADLINE year compared against a TARGET year ("announce before 2027" vs "the 2028 nomination") | `_period_years` returns (targets, deadlines); only targets are compared, in both the new and the old year veto |

**Kept (correct rejections, oracle noise):** group-stage winner vs tournament
champion (67), "AI model" vs "coding model" on different months (30), Coachella
"perform" vs "headline" (14), General Mills vs Carnival earnings words (20).

**New precision rule found while measuring:** a numeric stat line on one side
only ("1250+ rushing yards" vs "Rushing Yards Leader") is not the same
contract — both word orders, basis points excluded (they have their own range rule).

**Tried and reverted:** letting a barren event pairing retry its fan-out slot.
+1% recall but ~1,000 mostly-wrong pairs (CA-04 vs MO-04 House, leader vs
"1250+ yards", "#1 artist" vs "#2 artist"), because wrong candidates keep
descending the score order once the right one is absent.

**Measured** (same 2026-09-18 pools; live report in the second column):

| | pass 4 | pass 5 |
|---|---|---|
| Text recall vs oracle | 80.8% | **85.1%** (live 85.4%) |
| Endorsed pairs, total | 8,628 | **8,805** |
| vs pass 4 | — | +220 gained, 43 removed (most correct: "15+ Sacks" vs "lead in Sacks") |
| Pairs flagged `settlement_risk` | ~100 | 459 (weather station + horizon) |

### 2026-09-18 (pass 4) — text recall measured and raised (74% → 81%)

**Proposal.** Text-matched markets had no recall number. Build an oracle
*independent of the matcher* (own tokenizer): a PM outcome and a Kalshi market
are a true pair when their event titles overlap strongly (word Jaccard ≥ 0.5,
same non-year numbers, not a single game) and the PM label equals Kalshi's
`yes_sub_title` after normalisation; binary events need near-identical
questions. 4,398 oracle pairs on the 2026-09-18 pools. Then trace every miss
through `is_compatible_match` and rank causes.

**Findings and fixes.**

| Cause (share of misses) | Fix | Where |
|---|---|---|
| Events matched strictly 1-1; PM lists one race several ways ("CA-07 … Winner" and "… (by individual)", party vs candidate), the wrong duplicate claimed the Kalshi event (the largest bucket: 614 misses passed every veto) | event fan-out ≤ 3, markets still 1-1 | `discover.py` |
| **v1 bug:** `_matchup_signature` treated " at " as a game separator — "win Best Director *at the* 99th Academy Awards" became a two-team matchup, so every awards pair (Oscars, Grammys, CMAs) was vetoed | "at" separates only when not followed by the/their/a/an/… | `matcher.py` |
| **v1 bug:** at-large districts ("WY-AL") not recognised as a seat, read as chamber control | `XX-AL` and "house race for" = seat | `matcher.py` |
| New false positives surfaced by the extra recall: runner-up vs winner; Champions/Europa League *league phase* vs overall; wrong bps rung | 3 more `context_veto` rules; bps compared as ranges (">25bps" = "50+ bps", 25bp steps) | `matcher.py` |

**Measured** (same pools):

| | pass 3 | pass 4 |
|---|---|---|
| Text recall vs oracle | 74.1% (3,259 / 4,398) | **80.8%** (3,553 / 4,398) |
| Endorsed pairs, total | 7,866 | **8,628** |
| Last veto batch | — | 93 removed, all false positives (league phase ×~40, runner-up ×~35, wrong bps rung); 25 gained, all correct |
| Matching time | 195 s | 208 s |

**Remaining text misses** (845, ranked by the tracer): v1 `stat_leader` /
`deadline` action vetoes over-firing on "next to leave the Cabinet" and
fantasy "Top WR" lists; `foreign jurisdiction` on non-US cabinets; esports
award names; plus oracle noise (e.g. "How high will *inflation* get" vs
"*unemployment*" share the phrase and the "Above 10%" label).

### 2026-09-18 (pass 3) — sports recall to ~96%, measurable coverage

**Diagnosis.** Six sports series stuck at 0 have **no Polymarket counterpart at
all** (NCAA women's volleyball, Polish TT Elite — PM's Setka tables are
Ukrainian/Czech/Moldovan leagues — AFCON qualifier games, Serie C, Davis Cup,
Ettan), so they belong outside the recall denominator. The real misses were
name normalisation in the join's fallback: "Reg Time:" prefixes, accents
(Grêmio, Bodø), dotted abbreviations (D.C.), initials (RB = Red Bulls, SL =
Sport Lisboa), extra words on one side (Hokkaido Nippon-Ham Fighters), and
local spellings (Praha, München, Utd).

**Implemented.**

| Fix | Where | Status |
|---|---|---|
| Name normalisation for the sports join (above list + small alias map) | `sports_match.py` | ✅ |
| `tools/coverage_report.py` — live ingestion counts, sports recall vs an independent oracle with a miss list, price-agreement precision, optional text matching | `tools/` | ✅ |
| Finishing-position veto ("3rd place" vs "last place") | `matcher.py` | ✅ |
| Period-year veto with normalisation ("2026-27" spans both; "before 2027" / "before Jan 2027" = 2026) | `matcher.py` | ✅ |

**Measured.**

| | before | after |
|---|---|---|
| Sports recall vs oracle (games with a PM counterpart) | 85.5% (652/763) | **95.6%** (746/780, fresh catalog) |
| Sports contract pairs (same pools) | 1,545 | 1,886 |
| Sports price agreement | median 0.5c | median 0.5c, p90 3c |
| Text pairs removed by the two new vetoes | — | 39, all false positives (Brazil state 1st vs national 4th/5th ×24, Ligue 1 3rd vs last ×15), 0 collateral |
| Total endorsed pairs (same 2026-09-18 pools) | 7,564 | **7,866** |

**Remaining sports misses** (from the report): NCAAF where the oracle hits the
wrong sport (noise), rugby where Kalshi is 2-way and PM 3-way (correctly
refused — different draw settlement), reserve teams ("S. Bratislava B" vs
first team, correctly refused), and a few names no rule reconciles
("Uniao SC Paredes" vs "USC Paredes").

**Is coverage 100%?** Ingestion and matching scope: yes. Recall: ~96% on sports
games, ~96% on House races; the text matcher has no independent oracle yet, so
its recall is unmeasured beyond those two families — that is the next gap.

### 2026-09-18 (pass 2) — precision: event context, and stop "whack-a-mole"

**Diagnosis.** Polymarket outcome snapshots are titled with the bare label
("Phil Murphy", "St. Louis Blues"). Both engines scored that label against the
full Kalshi question, so the parent-event difference was invisible — and when a
wrong pair was vetoed, greedy 1-1 assignment handed the slot to the *next*
wrong candidate (Conference Champion → Division winner → Presidents' Trophy).
Separately, inside matched event groups, labels like "Dividend" or "Denver
Broncos" were compared with Kalshi's whole question and fell under the 0.15
similarity floor, so thousands of true outcome pairs were missed.

**Implemented.**

| Fix | Where | Status |
|---|---|---|
| `context_veto` — 10 event-context rules, each from an observed class: single-game vs season scope; "run for" vs winning; county vs whole jurisdiction; division vs conference; price race ("$50k before $100k") vs level; different central banks; opposite party wave; negated contract; "exactly N" vs open-ended; different named entities sharing words (tolerates initials "J. J."/"J.J." and short first names Alex/Alexander) | `matcher.py` | ✅ |
| `settlement_risk` — weather high/low pairs flagged (venues may use different stations); alerter skips them, pairs stay visible | `matcher.py`, `alerter.py` | ✅ |
| Contextual gate (label + event title, Jaccard ≥ 0.30) on every fallback candidate **before** 1-1 assignment (`match_markets(pair_gate=…)`) | `discover.py`, `matcher.py` | ✅ |
| Label-to-label scoring against Kalshi `yes_sub_title` for named outcomes (numeric ladders excluded — they picked adjacent rungs, e.g. "below 5.21%" → "below 5.22%") | `discover.py` | ✅ |
| Tried and reverted: scoring the fallback on full contextual titles — +3,885 pairs but 51 min runtime | — | ✖ |

**Measured** (live A/B, identical 2026-09-18 pools, full catalogs):

| | before this pass | after |
|---|---|---|
| v2-endorsed pairs | 6,046 | **7,564** (+1,586 gained, 28 removed) |
| of the 28 removed | — | ~26 false positives (conference vs division ×11, leader vs single-game prop, county win vs county vote-share, Alex vs Matt Fitzpatrick, Universidad de Chile vs Católica, BoE vs Fed …); ~2 true pairs (nickname Toño/Antonio; "Stays with Golden State") |
| matching time | 211 s | 186 s |
| House races endorsed | 658 | 659 |

**Still open (next pass):** finish-position mismatch ("3rd place" vs "last
place"); year mismatch hidden in event titles ("Brazil inflation 2026" vs "Dec
2025"); "team to advance" vs "championship matchup"; "Mamdani rent freeze" vs
"congestion pricing" (shared NYC/2027 tokens); nickname aliases; recession
definitions ("by end of 2027" vs NBER-dated); sports series still at 0
(NCAA W volleyball, TT Elite, AFCON, Serie C, Davis Cup).

### 2026-09-18 — pull everything, match everything

**Proposal (this pass).** "Pull all events and match all" needed three things:
(i) full ingestion of both catalogs, (ii) a matcher that finishes on the full
cross-product, (iii) a path for markets whose titles never overlap (sports games).

**Implemented.**

| # | Fix | Where | Status |
|---|---|---|---|
| 1a | Polymarket keyset pagination; `last_scan_complete` flag + PARTIAL warning | `polymarket/client.py`, `discover.py` | ✅ |
| 1b | Kalshi `with_nested_markets` catalog (no per-event calls); event close derived from nested markets (the `/events` rows carry no `close_time`, so the horizon filter and close-time sort had been no-ops) | `kalshi/client.py`, `discover.py` | ✅ |
| 1d | Unbounded scan = whole PM catalog, unfiltered (keyword filter was O(events × keywords) and matched nearly everything anyway) | `discover.py` | ✅ |
| 1e | Alerter scans with no event cap (`CAP_LADDER = (None,)`) | `alerter.py` | ✅ |
| M1 | Exact **prefix-filter** blocking (`PrefixIndex`) for event-group and individual matching — lossless for Jaccard ≥ gate; threshold-led path keeps full-token candidates | `matcher.py`, `discover.py` | ✅ |
| M2 | Memoised pure text extractors (per-title, results frozen) | `matcher.py` | ✅ |
| M3 | Deterministic tie-break in greedy 1-1 assignment (results no longer depend on candidate iteration order) | `matcher.py`, `discover.py` | ✅ |
| 2a | Structured **sports-game join** (teams + `gameStartTime`; doubleheader, league-namespace, tie-shape guards; name fallback) | `sports_match.py` | 🟡 moneylines only; ~96% recall (pass 3) |
| 2b | House structured join | — | not needed: text matcher now finds 635/660 on full pools |
| 2c | Ladder/bucket synthesis (Kalshi thresholds vs PM ranges) | — | ⬜ |
| 3 | Precision fixes for high-edge false-positive classes (below) | `matcher.py` | 🟡 see pass 2 |

**Performance** (same machine, pure Python):

| | before | after |
|---|---|---|
| Kalshi ingest | 1,500 per-event calls, ~50 events/min under 429 backoff | whole catalog, ~60 pages, 5–22 s |
| Polymarket ingest | first ~2,100 events (silent 422) | whole catalog, ~20 s |
| Matching, baseline pools (14k × 5.7k) | 23.2 s | 1.2 s |
| Matching, full catalogs (88k × 166k) | did not finish (>40 min) | 109 s |
| Live end-to-end, full catalogs, incl. order books (141k × 214k) | — | 510 s |

### Coverage scorecard (live, 2026-09-18)

"100% coverage" has two separate meanings, and only one is reachable:

| Dimension | Result | 100%? |
|---|---|---|
| **Ingestion** — share of each venue's open catalog fetched | Kalshi 15,627 events / 141,046 markets; Polymarket 21,099 events / 213,977 markets — the whole open catalog on both | ✅ yes |
| **Matching scope** — share of ingested markets the matcher considers | every market on both sides enters matching (no cap, no keyword filter) | ✅ yes |
| **Recall** — share of *truly equivalent* pairs found | House races 635/660 (96%, 0 wrong counterparts); sports games 765/1,440 upcoming Kalshi games joined (53%) — MLB 42/45, NFL 26/31, NHL 14/20, ITF 14/14, EFL Ch/L1/NL 11/11 | ❌ no — see gaps |
| **Precision** | text pairs: 39/40 random v2-endorsed pairs correct; sports pairs: median \|price gap\| 0.5c, p90 2.5c over 1,523 priced pairs (no swapped teams) | high on random sample; **poor in the high-edge tail** |

Recall can never be 100% of *markets*: most Kalshi markets (hourly index
ranges, vote-share ladders, player props) have no Polymarket counterpart.
The target is 100% of the pairs that really exist, and the gaps are known:

- **Sports series still at 0:** KXNCAAWVMATCH (0/98), KXTTELITEMATCH (0/87),
  KXAFCONGAME (0/48), KXSERIECGAME (0/30), KXDAVISCUPMATCH (0/27),
  KXETTANGAME (0/16) — either no PM counterpart or codes/names the join can't
  reconcile yet; needs per-series inspection.
- **NCAAF 111/237** — mostly code/name mismatches for small schools.
- **Non-moneyline sports** (spreads, totals, props) not joined yet.
- **Ladder/bucket markets** (§2c) need multi-leg synthesis.

### High-edge false-positive classes (adverse selection)

On the full catalog, the largest "arbs" are mostly mismatches — a wrong pair
looks like a big edge. From the 2026-09-18 end-to-end run (281 positive-net
signals, 46 above 3c), the top of the list is dominated by:

1. **"run for" vs "nominee"** — PM "Phil Murphy [Democratic Presidential
   Nominee 2028]" ↔ Kalshi "Who will run for the Democratic presidential
   nomination? Phil Murphy" (7 of the top 25).
2. **Sub-jurisdiction vs statewide** — PM "Abdul El-Sayed (D) [Michigan Senate]"
   ↔ Kalshi "Will Abdul El-Sayed win Kent County?".
3. **Near-identical outcome labels** — PM "Reporters Without Borders" ↔ Kalshi
   "…Nobel Peace Prize? Doctors Without Borders".
4. **Season award/leader vs single-game prop** — PM "José Ramírez [leader]" ↔
   Kalshi "José Ramírez: 1+ stolen bases?".
5. **"by end of" vs "during"** — PM "US recession by end of 2027?" ↔ Kalshi
   "Will there be a recession in 2027?".

These pass both v1 and v2 today. The alerter's AI settlement check is the
last line of defence, but the matcher should reject them itself. **Next pass:**
add each class as a v2 field-level rule, with a regression test per class.

---

## Pass 16 — multi-leg ladder synthesis (`ladder_match.py`)

**Target.** The pass-15 coverage ledger named `KXMIDTERMMOV` (4,552 Kalshi
markets) as the largest class that is genuinely matchable but structurally
unreachable. Polymarket *does* list midterm margin-of-victory markets; they
never pair 1-to-1 because the venues quote different shapes:

| Kalshi | Polymarket |
|---|---|
| `Kotek, 8+ pts` — cumulative, YES iff margin ≥ 8 | `Kotek 9-12%` — disjoint buckets partitioning the race |

The counterpart of a rung is a **sum of buckets**, so no 1-to-1 matcher rule can
ever find it. Alignment measured live first: 603 Kalshi MOV events, 486 PM MOV
events, **68 races on both venues**, and the ladders only partly line up —
Rhode Island's Kalshi rungs (7, 10, 13, 16) meet PM's 5-point edges at 10 and
25, while Georgia's (1, 4, 7, 10) never meet PM's 3/6/9/12 edges at all.

**The key idea: a straddled threshold is still tradeable, one way.** The first
cut treated a threshold falling inside a bucket as an *estimate* with bounds.
That was weaker than the truth. Pick the basket per direction and both sides
become exact settlement dominance:

- **ABOVE-only** basket (buckets entirely ≥ threshold) pays 1 only when the rung
  does → it is *dominated* → **buy the rung, sell that basket**.
- **ABOVE + STRADDLING** basket pays 1 whenever the rung does → it *dominates*
  → **sell the rung, buy that basket**.

A positive edge then holds however the straddling bucket resolves — no estimate
anywhere. When the threshold lands on a bucket edge the straddle set is empty,
both baskets coincide, and the rung replicates in both directions (`exact`).

**Two bugs caught by auditing the first live run.** The first run reported 76
positive edges of 218 priced, several near 20c. That was too good, and it was:

1. *Priced off marks, not books.* `ladder_match` read `snap.orderbook.best_bid`,
   which for Polymarket is `outcomePrices` — a single mid written to **both**
   sides so the matcher's `price_sim` keeps working (`discover._catalog_bid_ask`
   documents this). The real top-of-book is `catalog_bid`/`catalog_ask` in
   `extra`. Every PM leg showing `bid == ask` to four decimals was the tell.
2. *One price for both directions.* Buying a basket pays **asks**; selling it
   receives **bids**. Using a single number booked a spread that doesn't exist.

Fixing both: **76 positive → 28**, and >3c candidates **47 → 16**. The marks
were inflating the opportunity roughly 3×.

**A soundness hole the live data nearly hid.** The dominating basket only
dominates if PM's buckets cover the *whole* upper tail. A capped top bucket
(`10-15%` with no `15%+`) pays nothing on a 20-point win, so buying it leaves a
short rung uncovered; a gap in the middle does the same. PM mixes shapes inside
one race — Kansas Senate has a full 5-point ladder for Marshall and a bare
`Hamilton Wins` for the other side — so this is not hypothetical.
`_covers_tail()` now requires contiguity and an unbounded top before quoting a
buy-side basket. Selling the ABOVE-only basket needs no such check: every bucket
in it sits entirely above the threshold regardless of what the rest looks like.
No live race currently fails the check, so the numbers didn't move — it is
insurance, and it caught three of my own test fixtures, which had capped tops.

**Live result (2026-09-21).** 235 rungs synthesised across 25 races, 63
edge-aligned; 28 positive edges, 16 above 3c. Largest verified by hand:

> Oregon Governor, `Kotek, 8+ pts`. Buy the rung at 0.41; sell PM's 9-12, 12-15,
> 15-18 and 18%+ at bids totalling 0.59. Margin ≥ 9: both pay, net 0. Margin
> 8-9: rung pays, basket doesn't, +1. Margin < 8: neither pays. Worst case
> **+0.163** after the Kalshi taker fee.

**Review-only, by design — nothing here feeds the alerter.** Three reasons, none
of which price data can resolve:

- the executor has no **N-leg** support, and legging into a 4-bucket basket one
  order at a time is not the arb that was priced;
- Gamma's catalog quotes carry **no depth**, so a fat edge may sit on a book
  with a few dollars in it (Oregon's `Kotek 6-9%` quotes 0.01/0.099 — a 10×
  spread, i.e. an empty book; the winning direction happens to touch only the
  tight legs, but nothing guarantees that);
- a rung and a bucket may define the margin differently (two-party share vs all
  votes), a **settlement** question no quote will answer.

Run it: `python -m tools.ladder_report --min-edge 0.05`.

**Coverage effect.** This is reach, not recall: it makes a 4,552-market class
*analysable* for the first time, but those markets still do not appear as pairs,
because they are not pairs. Honest statement of coverage is unchanged — see
"Where the remaining gap actually is" below.

---

## Pass 17 — team-name reconciliation, and a reverted experiment

**Where the ledger pointed.** After pass 16, the largest recall gaps in classes
that *do* match were Polymarket's `totals` (243/7,082) and `spreads`
(86/5,555) — 12,637 markets, ~330 matched. Diagnosis on live pools:

| PM spread/total markets | 12,670 |
|---|---|
| …in a PM event with ANY joined contract | 680 |
| …actually matched | 329 |
| …in events with **no joined contract at all** | **11,990** |

So 95% of the gap was not a line-matching problem. It was that **the GAME never
joined**, and lines ride on a verified game key, so one failed game costs its
whole ladder (~30 markets).

**First hypothesis, and why it was wrong.** `_k_games` only builds a game when
the Kalshi series ends in `GAME`/`MATCH`. `KXNFLSPREAD` does not, and Kalshi
lists 365 NFL spread markets against only 34 `KXNFLGAME` markets — so it looked
like Kalshi was pricing lines on games it lists no moneyline for. I implemented
`_KLineGame` to synthesise a game from its line events (deriving the date from
the ticker and the teams from the event title `LA Rams vs DEN Broncos: Spread`).

Measured live: **60 synthesised games, 0 new pairs.** The premise was wrong —
every `KXNFLSPREAD` event *is* already covered by a real `KXNFLGAME`; the 34 vs
365 comparison was markets, not games. The 60 it did find were
`KXMLBINNINGTOTAL` and `KXWTAGTOTAL`, classes with no Polymarket counterpart.
**Reverted** (~90 lines for no measured gain).

That work did surface one genuine defect: `_K_EVENT_KIND_RE`'s
`(GAME|MATCH|SPREAD|TOTAL)` alternation mis-splits compound classes, reading
`KXNCAAFTEAMTOTAL` as `KXNCAAFTEAM` + `TOTAL` — a namespace that addresses no
event. It affected only the new code, so it left with it, but the same trap
waits for anyone re-deriving a league prefix that way; strip the longest
**known** class suffix instead.

**The actual cause: team naming.** The NFL game `nfl-nyg-la-2026-09-22` failed
to join `KXNFLGAME-26SEP21NYGLAR` for two independent reasons:

* codes — Polymarket `la` vs Kalshi `lar`, so no exact match;
* names — Kalshi `"Los Angeles R"` vs Polymarket `"Rams"`, sharing no token.
  Kalshi abbreviates as **`<City> <first letter of nickname>`**, inconsistently:
  some games use the nickname outright (`"DEN Broncos"`).

Fixes, both narrow:

* `_code_prefix_bridge` — accept a prefix relation on one team's code **only**
  when the other team's code matches exactly. `la` is ambiguous across
  franchises (Lakers/Angels/Chargers/Rams); the exact match on the other team is
  what pins the game down before trusting the prefix.
* `_city_letter_agrees` — treat the trailing letter as a real discriminator
  ("New York G" is the Giants, not the Jets), checked against any token of the
  Polymarket name so `"Boston R"` reaches `"Red Sox"`.

**A false positive the live A/B caught before landing.** A single combined
matching pass LOST 2 real pairs: Polymarket `"Athletics"` matched both Kalshi
`"A's"` (strict, correct) and Kalshi `"Los Angeles A"` — the *Angels* — via the
letter rule. Two hits meant ambiguity, and `_code_map` dropped the whole
Angels/Athletics game. Fix: **strict token agreement resolves every name first;
only names with zero strict hits reach the letter rule**, and only against codes
the strict pass has not claimed.

The letter pattern was also tightened to plain city words. On the live catalog
the looser shape matched 211 distinct names including `'Cardi B'`, `'Polo G'`,
`'LL Cool J'` and `'Amendment I'`. None can reach the join today (only
GAME/MATCH series enter it), so that guard is precautionary, not a live fix.

**Result:** 1,135 → 1,158 sports contract pairs on a frozen catalog (**+23, 0
lost**); 1,190 on the live scan. The additions are the LAR/DEN spread and total
ladders and the NHL Kings/Ducks game with its totals.

**Known limitation, stated rather than papered over.** `_names_agree` remains
permissive on its own — `_names_agree("Los Angeles A", "Athletics")` is still
True — because the fix lives in `_code_map`'s assignment, not in the predicate.
`_subject_agrees` also calls it, so a LINE subject could in principle be
mis-assigned. Measured: **0 live line pairs currently use the city+letter
shape**, so this is latent, not active. Containing it needs the same two-pass
discipline inside `_match_lines`; that is the next pass's job.

**Ingestion remains 100%** on both venues (103,451/103,451 and
147,491/147,491). Matching is not 100% and cannot be — see the README's
"What 100% means" section and `tools.coverage_ledger`.

---

## Pass 18 — Polymarket's sibling line events, and closing pass 17's latent hole

Two fixes. One recovers a large, genuinely matchable set of markets; the other
hardens a risk pass 17 documented but did not contain.

### 18a — `-more-markets`: one game, several Polymarket events

Pass 17 established that 95% of the unmatched spread/total gap was games that
never joined, not lines that failed to pair. This pass found out why for the
reachable part of it.

**Polymarket splits a single game across multiple events.** The moneyline lives
in the base event; the extra lines live in a sibling whose slug carries a
suffix:

```
mls-vwh-dcu-2026-09-26                 <- base game, already joined to Kalshi
mls-vwh-dcu-2026-09-26-more-markets    <- spreads / totals / team totals
```

`_PM_GAME_SLUG_RE` requires the slug to END at the date, so **every sibling
event was rejected before a game could be built**, and its whole ladder was
invisible — even when the base game joined perfectly.

Measured on a frozen full catalog, by suffix:

| Suffix | Markets | Reachable? |
|---|---|---|
| `-more-markets` | **29,855** (545 games) | yes — classes we already handle |
| `-total-corners` | 26,337 | **no** — Kalshi lists no corner market |
| `-exact-score` | 10,301 | **no** — Kalshi lists no exact-score market |
| `-player-props` | 2,727 | partly |
| `-second-half-result` / `-halftime-result` | 1,886 / 1,846 | yes |

Of the `-more-markets` total, **5,762 markets belong to 97 games already joined
to Kalshi**. `_p_games` now groups by BASE slug and folds in siblings whose
suffix is in `_PM_SIBLING_SUFFIXES`. Corners and exact score are deliberately
excluded — unmatchable by definition, and merging them would only add noise.

Care taken: each snapshot keeps its own `event_id` (other code uses it for pair
identity); only the grouping key changes. `_PGame.slug` stays the base slug so
`match_reason` and the one-PM-game-per-Kalshi-game dedupe keep working. Markets
listed on both base and sibling are de-duped by `market_id`. A sibling with no
base event is dropped by the existing moneyline/three-way guard.

**Result: 1,158 → 1,592 sports contract pairs (+434, 0 lost).** Every addition
is a line pair; moneyline stays at 775, confirming these games were already
joined and only their ladders were missing.

**Precision check on the 434 additions** — median cross-venue price gap
**0.005**, p90 0.020. One pair exceeded 0.30 and is a *correct* match
(Central Español −2.5) against an empty Kalshi book defaulting to a 0.5 mid.
Two venues pricing within half a cent are quoting the same contract; this is
the same evidence standard passes 12–13 used.

### 18b — the latent hole from pass 17 was not hypothetical

Pass 17 closed the city+letter rule's ambiguity in `_code_map` but left
`_subject_agrees` on the permissive predicate, and recorded the residual risk
honestly. Writing the regression test showed it was **reachable, not latent**:
on unmodified `main`, a game carrying both an Angels and an Athletics team-total
at the same line value pairs the Kalshi ANGELS line to the Polymarket ATHLETICS
line, because `_names_agree("Los Angeles A", "Athletics")` is True.

```
AssertionError: 'mlb-laa-oak-2026-09-22-team-total-oak-4pt5'
             != 'mlb-laa-oak-2026-09-22-team-total-laa-4pt5'
```

A wrong line pair does not merely miss an arb — it *invents* one, pricing two
different teams' totals against each other. `_subject_agrees` gained a `strict`
flag and `_match_lines` now runs strict-first per contract class, the same
discipline `_code_map` uses. **0 live pairs changed**, because no current slate
uses that shape; the fix removes the trap rather than repairing damage.

### Coverage after this pass

Ingestion stays **100%** on both venues (103,352/103,352 Kalshi;
147,634/147,634 Polymarket). The ledger's shape is unchanged and worth
restating plainly, because it is what bounds matching:

* Kalshi: 3,036 of 3,846 classes never match — **78,474 markets**;
* Polymarket: 140 of 154 classes never match — **75,504 markets**.

Those are contracts the other venue does not list. Pass 18 did not move that
ceiling and no matcher rule can; it recovered markets that were matchable and
were being dropped by a slug pattern. That is the distinction the README's
"What 100% means" section draws, and it remains the honest answer to
"match all".
