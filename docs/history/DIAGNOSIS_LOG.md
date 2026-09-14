# Pair-Match Diagnosis Log

Purpose: record every attempt to verify that **all** Kalshi↔Polymarket pairs
truly match (same event, same party/outcome, same cycle/year, comparable
timing). This is an append-only log so approaches are not repeated and we can
conclude which directions are worth digging into.

Method for auditing a pair set: extract structured evidence from BOTH sides
(year from ticker `-NN-` + close date + titles, party R/D, jurisdiction, named
candidate, close-time delta) and flag any pair whose two sides disagree.

---

## Iteration 1 — 2026-06-10

### 1a. Stale artifact `pairs.json` (the original symptom)

- **Finding:** `pairs.json` (untracked, dated May 25, 273 pairs) has
  **94 / 273 (34%) cross-cycle mismatches** — e.g. Kalshi `SENATENC-28-R`
  (2028 race, closes 2029) paired with Polymarket's 2026 NC Senate race, and
  reported as a 9.5% arb. Several other phantom arbs on mismatched pairs.
- **Root cause:** the file predates commit `b47ec8a` ("Use market identifiers
  in year compatibility"), which taught `matcher._years` to parse the 2-digit
  cycle out of the Kalshi event ticker (`-28-` → 2028). Before that, Kalshi
  Senate titles carry **no year** (the year lives only in the ticker + close
  date), so the year-disjoint veto never fired.
- **Conclusion:** `pairs.json` is a stale output, NOT evidence of a current
  bug. Should be deleted / git-ignored. Do not re-audit it.

### 1b. Live re-run of the exact failing case (NC Senate)

Ran the current matcher live (`fetch_polymarket` + `fetch_kalshi` +
`match_markets`). Kalshi lists BOTH cycles with identical titles:
`SENATENC-26-R` (closes 2027) and `SENATENC-28-R` (closes 2029).

- **Result:** current code paired Poly-2026 → `SENATENC-26-R` (correct cycle)
  and Poly-Dem-2026 → `SENATENC-26-D`. It did NOT match the 2028 tickers.
- **Conclusion:** the cycle veto works **because** the Kalshi `event_ticker`
  (which holds the year) is included in `matcher._snapshot_text`.

### 1c. Fresh broad audit — ELECTION category

`python3 discover.py --category election --max-events 60 --poly-scan 8000
--min-sim 0.30 --output diag_pairs_election.json` → 62 pairs.

Audited all 62 for year / party / close-delta disagreement:

- **62 total, 2 flagged, 0 real mismatches.**
- The 2 flagged (#40/#41) are the 2028 Presidential-party markets
  (R↔R, D↔D, both 2028) flagged only on a 366-day close-date gap — which is
  expected: Kalshi and Polymarket use different resolution dates for the same
  event (README "Horizon note"). These are correct matches.

**Conclusion so far:** with the current code, the election category matches
correctly. The original mismatch symptom is entirely explained by the stale
`pairs.json`. The year veto's robustness depends on the Kalshi ticker carrying
the cycle — a latent weakness if a future ticker lacks it (see Open Questions).

## Iteration 2 — 2026-06-10

Built a reusable structured-evidence auditor `diag_audit.py` (year-cycle /
party / rate disagreement). Ran fresh bounded scans for economic and political.

### 2a. Audit heuristic flaw found & fixed (not a matcher bug)
First economic run flagged 6 IPO "lead-left underwriter" pairs as
`YEAR 2028 vs 2027`. These are **correct matches** — the Kalshi ticker
`-28JAN01` is a *resolution date* (Jan 1 2028) and the Poly side closes
~Dec 2027; same bank, same IPO, closeΔ=0–1 day. The auditor conflated "any
year token" with "election cycle." **Fix:** `diag_audit.py` now only flags a
YEAR mismatch when the cycle gap is ≥2 years (2026-vs-2028 stays flagged;
2027-vs-2028 calendar boundary does not). After the fix: election 62 / economic
8 / political 1 → **0 structural flags**.

### 2b. REAL matcher bug found — different political events, same subject
Political scan produced 1 pair, and it was a genuine false positive:
- Poly:   "Will Trump be **impeached** before his term ends?"
- Kalshi: "Will Trump impose **martial law** before his term ends?"

Diagnosis: both titles reduce to action `{deadline}` (from "before his term
ends"), Jaccard = 0.50 (shared scaffolding "Will Trump … his term ends"), no
proper-name / year / party / rate difference. The distinguishing verb phrases
mapped to **no structured signal**, so every veto passed. This is the class
*same subject + same time scaffolding + different core predicate*.

**Approach tried (worked):** extend `matcher._contract_actions` with distinct
political-event predicates (`impeach`, `martial_law`, `govt_shutdown`,
`national_emergency`, `pardon`, `indicted`) and add a dedicated veto in
`is_compatible_match`: if both sides name a `_POLITICAL_EVENT_ACTIONS` member
and they are disjoint → reject. Adding predicates alone was insufficient (both
still share `deadline`, so the generic `isdisjoint` veto passes) — the
mutually-exclusive-subset veto is the key.

**Verification:**
- impeach↔martial law → rejected; impeach↔impeach → still matches.
- Full suite 35→**37 passing** (added 2 regression tests in
  `tests/test_core_logic.py`).
- Re-ran political discover → **0 pairs** (false positive gone, no legit
  political pairs in this catalog window).

**Conclusion:** the generic predicate-veto design has blind spots wherever a
shared template + shared deadline masks a different core verb. The fix follows
the codebase's explicit-predicate idiom. The general (riskier) alternative —
requiring overlap of distinctive non-stopword tokens after stripping shared
scaffolding — was considered and *not* taken this iteration (risk of
over-vetoing synonyms like win/victory). Noted as a possible deeper direction.

## Iteration 3 — 2026-06-10 (false-negative hunt)

Shifted from false positives (bad matches) to **false negatives** (pairs a
human sees that the engine drops). Audited sports (3 pairs) and pop (0 pairs)
— both suspiciously sparse, which motivated the hunt. Built
`diag_false_neg.py`: fetch a Kalshi event + Polymarket keyword set, run
`match_markets`, and report every unmatched cross pair with title overlap and
the veto that fired.

### 3a. Polymarket Gamma `_q` search is broken (known, reconfirmed)
`PolymarketClient.search_events("2028 presidential")` returns 8994 events with
identical irrelevant top hits for every query — the `_q` param is ignored
(README already notes this). Discovery therefore depends entirely on a
full-catalog scan + client-side token filter. `search_markets` (substring
filter) works, but multi-word keywords must match a **contiguous** substring:
keyword `"2028 presidential"` misses `"...2028 US Presidential..."`. Lesson for
the fixed/keyword path: keywords must be single tokens or true substrings.

### 3b. REAL false-negative bug — Kalshi candidate identity dropped from title
Probing `KXPRESPERSON-28` ("2028 Presidential winner") returned **0 matches**
against the obvious Polymarket twins ("Will Gavin Newsom win the 2028 US
Presidential Election?"). Root cause: every Kalshi market in a
mutually-exclusive event shares one generic `title` ("Who will win the next
presidential election?"); the candidate lives only in `yes_sub_title`
("Gavin Newsom"). Both `pipeline.fetch_kalshi` and `discover._k_snap` built the
snapshot title from `title` alone, dropping the name. The matcher scores and
vetoes on the title, so every such market looked generic → the
generic-winner veto rejected all named Polymarket contracts. An **entire class**
of Kalshi markets (candidate-winner ladders) was invisible to matching.

**Fix (worked):** added `pipeline.kalshi_market_title(mkt)` which appends
`yes_sub_title` to the title when it adds information; used it in both build
sites. Result on the live probe: **0 → 22 correct candidate matches**
(Newsom↔Newsom, AOC↔AOC, …), and different candidates (Newsom↔Shapiro) are
still correctly rejected. Full suite 37→**39 passing** (added
`test_kalshi_title_carries_candidate_subtitle` and
`test_named_poly_matches_kalshi_generic_title_with_subtitle`).

### 3c. Remaining narrower false negative — initials with periods
"J.D. Vance" (Kalshi `yes_sub_title`) vs "JD Vance" (Polymarket) do not pass
`_proper_names` overlap because of the periods, so JD Vance specifically stays
unmatched even after 3b (it appears in the near-miss list, not the 22).
Candidates without dotted initials match fine. **Not yet fixed** — recorded so
it isn't rediscovered. Likely fix: normalize initials in `_proper_names`
(strip periods, collapse "J.D."→"jd") — but verify it doesn't merge distinct
people. Deferred to a future iteration.

## Iteration 4 — 2026-06-10 (resolve 3c; deeper diagnosis)

Re-examined 3c. The "J.D. Vance" vs "JD Vance" miss was NOT mainly about
periods. Direct trace of the live snapshots showed the veto fired on
`_proper_names` disagreement: Polymarket side extracted `{presidential election}`
(the phrase is Titlecased in the Poly title) while the Kalshi side extracted
`{vance kxpresperson}` (surname + leaked event ticker). The two never overlap,
so `is_compatible_match` rejected the pair — and the production within-group
matcher (`discover._match_outcomes_within_group`) hard-gates on the same
function (line 512), so the miss occurred on BOTH paths.

Two contributing root causes:
1. `_GENERIC_NAME_TERMS` lacked election/contest words, so the generic phrase
   "Presidential Election" was mistaken for a person's name on the Poly side.
2. (latent) `event_id` (ticker `KXPRESPERSON-28`) leaks into `_snapshot_text`
   and gets glued onto the trailing surname → "vance kxpresperson".

**Fix (worked):** added `presidential, election, elections, primaries` to
`matcher._GENERIC_NAME_TERMS`. Now Poly "Will JD Vance win the 2028 US
Presidential Election?" extracts no phantom name; with no name on the Poly
side the name-veto is skipped and `match_markets`/greedy assigns JD Vance to
J.D. Vance via the shared "vance" title token (correct candidate, highest
Jaccard). Newsom and the other clean names still match.

**Verification:**
- Live `KXPRESPERSON-28` probe: candidate matches **22 → 25** (JD Vance now
  included); wrong-candidate cross pairs remain unmatched.
- Suite 39 → **40 passing** (added `test_generic_contest_phrase_is_not_a_proper_name`).

**Conclusion / direction:** matching named candidates through a
capitalization-dependent proper-name regex over a string that includes the raw
event ticker is fragile (asymmetric extraction, ticker leakage). The contained
fix resolves the observed miss without regressions, but the *robust* long-term
direction is to match candidate identity via the explicit Kalshi
`yes_sub_title` outcome label vs the Polymarket outcome label, rather than
inferring names from free text. Recorded as the deeper dig for a later pass.

## Iteration 5 — 2026-06-10 (sports false negatives)

Browsed live sports catalogs. Found genuine overlap is thin in June (many
championships resolved), but located confirmed shared markets:
`KXNBA-26` (NBA Finals) and `KXNHL-26` (Stanley Cup) ↔ Polymarket
"Will the <team> win the 2026 NBA Finals / NHL Stanley Cup?".

### 5a. REAL false negative — Kalshi city-only team + trademark avoidance
Probe of `KXNBA-26` vs Polymarket "NBA Finals":
- San Antonio matched (substring "san antonio" ⊂ "san antonio spurs").
- **New York Knicks MISSED** despite the highest title sim (0.56). Kalshi
  titles use the city ONLY and avoid the league trademark:
  "Will the New York win the 2026 **Pro Basketball** Finals?" vs Polymarket
  "Will the New York **Knicks** win the 2026 **NBA** Finals?". "New York" is in
  `_NAME_STOP` (jurisdiction), so after stripping it the Kalshi side's only
  `_proper_names` value was the phantom phrase "pro basketball finals", which
  cannot overlap "new york knicks"/"nba finals" → vetoed.

**Fix (worked):** added sports event-descriptor words
(`pro, basketball, baseball, football, soccer, finals, final`) to
`_GENERIC_NAME_TERMS` so they are not mistaken for team names. Then the Kalshi
side has no phantom name, the name-veto is skipped, and `match_markets` assigns
the correct team by title Jaccard. League acronyms (nba/wnba/nfl/nhl/mlb) were
deliberately NOT added (kept as distinguishing). Result: NBA 0→2, NHL 2→2.

### 5b. Regression the fix introduced — and its fix
City-only matching let "New York Liberty win the 2026 WNBA Finals" become
compatible with Kalshi NBA "New York" (a cross-league FALSE POSITIVE; greedy
usually picks the Knicks, but it is a latent collision). **Added** a
`_sports_league` helper + veto in `is_compatible_match`: "Pro Basketball"→nba,
"WNBA"→wnba, etc.; disjoint leagues are rejected. Verified: Knicks(NBA)↔Kalshi
NBA = compatible; Liberty(WNBA)↔Kalshi NBA = rejected.

**Verification:** suite 40 → **42 passing** (added city-only match,
cross-league veto regression tests). Net: resolves the sports false negative
without the cross-league false positive.

**Conclusion:** Kalshi's trademark avoidance ("Pro Basketball"/"Pro Football")
and city-only finalists are a distinct false-negative family from the candidate
ladders (iter 3-4). The league synonym map is the key enabler; it should be
extended (college, soccer leagues, MLS, etc.) and is the natural place to add
future league disambiguation.

## Iteration 6 — 2026-06-10 (deadline / horizon ladders; PARTIAL)

Test case: Kalshi impeachment ladder ("Will the President be impeached before
Jan 1, 2027?", …) vs Polymarket "Will Trump be impeached by end of 2026?".
The correct human match is by aligned deadline (end of 2026 == before Jan 1,
2027). Probe returned **0 matches** — THREE stacked vetoes, diagnosed in order:

1. **Domain misclassification.** Bare "president" was an `_ELECTION_TERMS`
   word, so the Kalshi title was tagged `election` while the Polymarket title
   ("Trump…") had no domain; the `election-and-not-other` guard then vetoed.
   **Fix:** removed bare "president" from `_ELECTION_TERMS` (kept
   "presidential"/"presidency"). Impeachment is an office-holder event, not an
   election.
2. **Deadline-action asymmetry.** "by end of 2026" does not match the deadline
   regex (needs `by <month>`), but "before Jan 1, 2027" does → the
   `("deadline" in p) != ("deadline" in k)` veto fired.
3. **Year-token disjointness.** "end of 2026" → {2026}, "before Jan 1, 2027" →
   {2027} → year veto, even though they are the SAME instant.

**Fix for 2 & 3 (principled):** close times are authoritative for horizon. Added
`_same_horizon = close_delta <= 72h` in `is_compatible_match`; when true, the
deadline-asymmetry and year-token vetoes are skipped. Election cycles have close
dates months/years apart, so the guard never relaxes those.

**Verified:** the correct pair (Δ39h) now passes `is_compatible_match`; a
far-apart pair (years disjoint, Δ thousands of h) is still rejected. Suite
42 → **45 passing** (3 new tests).

### 6a. REMAINING gate (NOT solved) — vocabulary/alias gap
After the veto fixes the pair is *compatible* but still **not matched by
`match_markets`**: title Jaccard is only 0.143 because Polymarket says "Trump"
and Kalshi says "the President" (shared content token is just "impeached").
The flat title-similarity matcher cannot bridge the sitting-president alias.
This is a genuine limitation, recorded so it is not re-diagnosed:
- The production `discover.py` path scores on price proximity + event grouping
  too, which may bridge this where flat `match_markets` cannot — NOT yet
  verified for impeachment; next step.
- A general fix would need a sitting-office alias map ("the President" ⇄ current
  holder) — time-sensitive and risky; deferred. Do NOT lower
  `min_title_similarity` globally (would add false positives elsewhere).

**Conclusion:** the veto layer no longer blocks deadline-ladder matches (real
progress), but title-only similarity is the next wall. The dig-deeper direction
is price/label-assisted scoring (as discover already does) rather than more
veto surgery.

## Iteration 7 — 2026-06-10 (price+horizon-led path: TRIED, REVERTED)

Resolved 6a's open question and attempted the price-assisted fix.

### 7a. Confirmed `discover.py` also misses the impeachment pair
Ran the production group matcher (`_match_groups_then_individual`) on the
impeach snapshots WITH order books. Still **0 matches**. The Polymarket impeach
markets are separate singleton events (slugs), not grouped with each other or
with the Kalshi `KXIMPEACH` event, so group matching fails and the individual
fallback hits the same title-Jaccard wall (0.143). **The strong price signal is
real** (Poly "end of 2026" mid 0.075 ≈ Kalshi "before Jan 1 2027" mid 0.054;
wrong horizons at 0.305 / 0.575) but is never reached. So 6a is a genuine
production miss on BOTH paths.

### 7b. APPROACH TRIED — price+horizon-led acceptance (REVERTED)
Added `_price_horizon_led(p,k)` + a fallback branch in `match_markets`: when
title sim < threshold, accept the pair only if it shares a SPECIFIC predicate
(not generic glue), resolves within 48h, and has catalog mids within 0.05.

- **Worked for the target:** the engine returned exactly
  "Trump impeached by end of 2026" ↔ "President impeached before Jan 1, 2027"
  (conf 0.40), and ONLY that horizon. 45 unit tests still passed.
- **But it REGRESSED** on ladder/bucket markets. A broad economic re-scan
  produced off-by-one false positives:
  - "2.5–3.0%" ↔ "GDP growth in 2026? **2.1% to 2.5%**"
  - "1.5–2.0%" ↔ "**1.1% to 1.5%**"; "2.0–2.5%" ↔ "**1.6% to 2.0%**"; etc.
  - "December 31, 2026" ↔ "When will OpenAI IPO? **Before Nov 1, 2026**"
  Adjacent ladder rungs have near-identical prices and identical horizons, and
  the rate veto does not separate them because neighbors **share a boundary
  value** (e.g. both contain "2.5%") so `_rates` is not disjoint.
- **Decision:** for an arbitrage engine a false match is costly, so the path
  was **reverted** in full. The iteration-6 veto fixes are independent and kept.

**Why recorded:** do NOT re-add an unconditional price+horizon-led path. A
viable version MUST first exclude laddered/bucketed markets — i.e. add a
numeric-range/bucket disjointness test ("2.1–2.5" vs "2.5–3.0" are adjacent,
not equal; treat shared endpoints as disjoint buckets) BEFORE allowing a
price-led match. Only then is price proximity safe as a tie-breaker. Until that
range-aware guard exists, the impeach-style alias gap stays unsolved by design.

### Useful side effect observed
The broad political re-scan now yields 12 correct pairs (Secretary-of-Labor
candidate ladder, "acquire"⇄"buy" Greenland, "recognize Somaliland") — all with
sim ≥ 0.33, i.e. surfaced by the iter 3–6 fixes, not the reverted path. Confirms
those fixes are compounding into real new true-positives.

## Iteration 8 — 2026-06-10 (crypto price thresholds: FIXED)

New family: asset price-threshold ladders. Kalshi `KXBTCMAXY-26DEC31`
("Will Bitcoin be above $149,999.99 by Dec 31, 2026 at 11:59 PM ET?") ↔
Polymarket "Will Bitcoin hit $150k by December 31, 2026?". Probe: **0 matches**.

**Root cause:** same level, different wording. "$150k"/"$150,000" vs
"$149,999.99" tokenize differently and Kalshi adds noise ("at 11:59 PM ET"), so
title Jaccard is 0.133 (< threshold) → never scored. (`is_compatible_match` was
already True; the gate was scoring.) This is the iter-7 vocabulary wall again,
but now with a CLEAN numeric key: the dollar threshold.

**Fix (worked — this is the safe version of iter-7's idea):**
1. `_dollar_threshold(text)` → `(direction, value)`, normalising `$150k`,
   `$150,000`, `$149,999.99` (with a `(?![a-z])` guard so the `[kmb]` suffix
   does not eat the "b" in "by").
2. `_threshold_equal` with 0.1% tolerance: `$150k == $149,999.99`,
   `$150k != $140k`.
3. **Veto** in `is_compatible_match`: both sides have thresholds and they are
   unequal → reject. Fixes adjacent-rung false positives ($140k vs $150k).
4. **Threshold-led acceptance** in `match_markets`: when title sim is below
   threshold, accept ONLY on EXACT threshold equality + shared named entity
   (the asset) + close times within 72h. Exact-threshold gating is what makes
   this safe where iter-7's catalog-price proximity was not (off-by-one).

**Verified:** all **7** BTC rungs now match their exact counterpart
($100k↔$99,999.99 … $200k↔$199,999.99), zero off-by-one; $150k↔$139,999.99
rejected. Suite 45 → **48 passing** (3 new tests).

### 8a. NEWLY EVIDENCED pre-existing false positives (NOT mine) — % and date ladders
The economic re-scan (discover group matcher) still emits off-by-one matches my
`$` veto does not cover, because they are PERCENT and DATE buckets:
- "2.5–3.0%" ↔ "GDP growth in 2026? **2.1% to 2.5%**" (adjacent % buckets share
  the 2.5% endpoint, so `_rates` is not disjoint)
- "December 31, 2026" ↔ "When will OpenAI IPO? **Before Nov 1, 2026**"
- "Negative GDP growth in 2026?" ↔ "GDP growth in 2026? **5.6% to 6.0%**"
These come from `_match_outcomes_within_group` (price-led, title floor 0.15),
are independent of iter-8, and are the concrete next target.

**Conclusion / direction:** the threshold-key approach is the right pattern.
Generalise it: a `_percent_range`/bucket disjointness veto (treat "2.1–2.5" and
"2.5–3.0" as adjacent, not equal) and a `_deadline_bucket` veto for date
ladders. Those would convert the iter-8 win for `$` ladders into a general
ladder-rung guard covering %, dates, and counts.

## Iteration 9 — 2026-06-10 (generalise thresholds to percent; one-sided only)

Pursued 8a. Browsing showed two distinct shapes on Kalshi:
  * one-sided thresholds: "Above 4.0%", "Above 5.0%" (KXGDP, KXCPIYOY) — like
    the iter-8 `$` ladders but in percent;
  * two-sided range buckets: "2.6% to 3.0%" (KXGDPYEAR) — a different shape.

### 9a. Found a SECOND false-negative cause in the existing `_rates` veto
"Will inflation reach more than 5% in 2026?" vs "...CPI...above 5.0%...": the
crude `_rates` string veto extracts {"5%"} vs {"5.0%"}, finds them disjoint, and
REJECTS — even though they are the same level. So one-sided percent thresholds
were doubly blocked (low title sim AND a spurious rate veto).

**Fix (worked — extends iter-8):** generalised `_dollar_threshold` →
`_numeric_threshold(text) → (direction, value, unit)` where unit is `usd|pct`
(two-sided "between X and Y" returns None). `_threshold_equal` is unit-aware:
usd within 0.1%, pct within 0.05 absolute (so 5%==5.0% but 4.9%≠5.0%). In
`is_compatible_match`, when BOTH sides parse a threshold the tolerant numeric
comparison is **authoritative and supersedes** the string `_rates` veto;
otherwise `_rates` still applies. The threshold-led acceptance in `match_markets`
now covers `%` too.

**Verified:** "more than 5%" vs "above 5.0%" → compatible; vs "above 4.9%" →
rejected. Economic re-scan now surfaces the correct "Above 4.5%" ↔ "At least
4.5%" inflation threshold pair; a diff vs iter-8 shows NO new false positives
(only a pre-existing Negative-GDP range FP shifting rungs — see below). Suite
remained **48 passing** (iter-8 tests updated to the new 3-tuple signature, plus
percent cases).

### 9b. Still deferred — two-sided range buckets
GDP/CPI range buckets remain unmatched/occasionally mis-matched:
- Cross-exchange boundaries do not align (Polymarket 2026 GDP uses 0.5-wide
  buckets at .0/.5; Kalshi KXGDPYEAR uses .1–.5 / .6–.0 and has NO 2026 event),
  so a "correct" range pair is often genuinely ambiguous or absent.
- "Negative GDP growth in 2026?" still matches a positive range (5.1–5.5%) via
  the discover group matcher's price-led mode (range returns None from
  `_numeric_threshold`, falls to `_rates`, and "Negative" has no rate token so
  nothing vetoes). This is the remaining range-family false positive (8a).

**Next:** a `_percent_interval(text) → (lo, hi)` with an interval-overlap (IoU)
veto for two-sided ranges, plus a sign/zero guard so "Negative" (lo<0) never
matches a strictly-positive bucket. Deferred this iteration to keep the change
small and because boundary misalignment makes the true-positive target thin.

## Iteration 10 — 2026-06-10 (implied-domestic jurisdiction)

Tested clean single-binary shared markets. Recession is unambiguous:
Kalshi `KXRECSSNBER-26` "Will there be a recession in 2026?" (close 2027-01-31)
↔ Polymarket "US recession by end of 2026?" (close 2027-01-31).

(Aside: Fed markets are NOT a false negative — Kalshi KXFED is rate LEVEL at a
meeting, Polymarket is COUNT of 2026 cuts; different contracts, correctly
unmatched, per the README.)

### 10a. REAL false negative (+ paired false positive) — implied US jurisdiction
The engine matched Polymarket "**UK** Recession in 2026?" to the (US) Kalshi
market and thereby STARVED the correct "US recession" match. Causes:
1. The Kalshi market names no country; on a US exchange an unmarked market is
   the domestic/US contract, but `_jurisdictions` returns empty, so the
   jurisdiction-disjoint veto cannot fire.
2. "UK"/"Japan" outscored "US" on greedy Jaccard (fewer tokens), and **Japan
   was not even in `_COUNTRY_ALIASES`** (sparse map: 11 countries, missing most
   major economies).

**Fix (worked):**
- Added major economies to `_COUNTRY_ALIASES` (japan, china, germany, canada,
  india, russia, mexico, australia, italy, spain, south korea, argentina,
  ukraine).
- Added `_FOREIGN_COUNTRIES` (= countries − united states) and a veto: a market
  naming a FOREIGN country must not match one with NO jurisdiction at all (the
  unmarked side is domestic/US). US states count as domestic, so the rule fires
  only on foreign countries.

**Verified:** engine now returns "US recession" ↔ Kalshi recession (conf 0.53);
UK and Japan are vetoed. Election re-scan rose to **86 correct pairs** (was 62
in iter-1) with 0 structural flags — no regression. Suite 48 → **50 passing**
(foreign-vs-unmarked + US-state-is-domestic regression tests).

## Iteration 11 — 2026-06-10 (award ladders; phantom-name recurrence)

Browsed popular Polymarket topics for shared markets. Nobel Peace Prize is a
clean candidate ladder: `KXNOBELPEACE-26` "Who will win the Nobel Peace Prize?
<entity>" ↔ Polymarket "Will <entity> win the Nobel Peace Prize in 2026?".

### 11a. False-negative goal MET, but a recurring phantom-name FP surfaced
The real nominees DID match (Pope Leo XIV, Trump, Musk, Zelenskyy, Navalnaya,
UNRWA, ICJ — 7 correct). But leftover Polymarket nominees were greedily
mis-paired ("Putin"↔"Dario Amodei", "Greta Thunberg"↔"Narges Mohammadi", …).
Root cause = the iter-4 bug recurring: "Nobel Peace Prize" was extracted as a
proper name on both sides, so `_names_overlap` returned True via the shared
award phrase and bypassed the nominee mismatch.

**Fix (worked):** added award/contest tokens (`nobel, peace, prize, oscar,
emmy, grammy, heisman, cup, trophy, medal`) to `_GENERIC_NAME_TERMS`. The
phantom-overlap FPs are gone (Putin↔Amodei now rejected); the 7 correct
nominees still match. Suite 50 → **51 passing** (1 new test).

### 11b. REMAINING leftover FPs — brittle name extraction (recorded, not fixed)
After 11a, 3 FPs persist: "Xi Jinping"↔"Minneapolis", "Ahmed al-Sharaa"↔
"European Union", "Mohammed bin Salman"↔"Narges Mohammadi". Cause: these names
do not extract via `_proper_names` ("Xi" < 3 chars; "al-Sharaa"/"bin Salman"
particles), so the Polymarket side yields an EMPTY name set, the
`p_names and k_names` veto is skipped, and greedy pairs them on shared
scaffolding (sim ~0.5). Same brittle-extraction family as JD Vance (iter-4).

**Proposed next (deferred — regression risk):** a distinctive-token veto for
winner/selection markets — after removing shared + generic tokens, if both
sides have non-empty distinctive token sets that do NOT overlap (substring-aware
to preserve "Zelensky"⊂"Zelenskyy"), veto. Risk: spelling/translation variants
across exchanges ("Zelensky" vs "Zelenskyy", anglicised names) could become
false negatives, so it needs the substring-aware comparison and careful
testing. Not shipped this iteration to avoid trading these FPs for new FNs.

## Iteration 12 — 2026-06-10 (sports season year convention)

Two probes:
- **US-Iran nuclear deal** (deadline ladder): the engine ALREADY matches the
  correct rungs ("deal before 2027?" ↔ "Before 2027"; "by June 30?" ↔ "Before
  July") via the iter-6 close-time horizon logic + greedy. No fix needed — good
  validation. (Latent: "Iran nuclear test" is compatible with "nuclear deal"
  rungs — predicate test≠deal not distinguished — but lost greedy here.)
- **NFL MVP** (named-player ladder): a real false negative.

### 12a. REAL false negative — sports season spans two calendar years
Poly "Will Josh Allen win the **2026** NFL MVP?" ↔ Kalshi "Will Josh Allen win
the MVP?" (`KXNFLMVP-**27**`) was rejected solely by `year:2026≠2027`.
Polymarket labels by season-start year (2026); Kalshi by award year (2027 =
season+1). Also Kalshi's title had no sport word ("MVP" was not a `_SPORT_TERM`)
so it got NO sports domain, and its close time is a far-out contractual expiry
(2028), so the iter-6 same-horizon (72h) relaxation could not apply.

**Fix (worked):**
1. Added `mvp` to `_SPORT_TERMS` so award markets get the sports domain.
2. Year veto: when a sports domain is present on either side AND the year gap is
   EXACTLY 1, treat as the same award (season-start vs award-year convention).
   Election cycles differ by >= 2, so they are never relaxed.

**Verified:** NFL MVP now matches **23 pairs**, all correct (player-name veto
still blocks Josh Allen↔Josh Jacobs); 2026-vs-2028 (gap 2) still vetoed for both
sports and elections. Sports discover rose from 3 (iter-5) to **39 pairs** with
no visible new false positives (chess, UFC titles, NBA 2K cover, MVP, golf — all
name-correct). Suite 51 → **53 passing** (2 new tests).

**Latent risk (noted):** if Polymarket lists two adjacent seasons of the same
award simultaneously (2026 and 2027 MVP for the same player), both become
gap≤1-compatible with one Kalshi rung; greedy picks one. Rare; not observed.

## Iteration 13 — 2026-06-10 (validation pass; no new FN class)

Probed several fresh families; all either WORK or have no live overlap:
- **MLB World Series** (`KXMLB-26` "Pro Baseball Championship" city-only ↔
  Polymarket "<Team> win the 2026 World Series"): 29 correct matches, even
  disambiguating same-city teams ("New York Yankees"↔"New York Y",
  "New York Mets"↔"New York M"). The iter-5 baseball/city fix generalises.
- **US-Iran nuclear deal**, **NFL MVP**: already covered (iter-12) and correct.
- **Trump resign / Putin out / leave-role**: horizons don't align across
  exchanges (Kalshi "before term ends" 2029 vs Polymarket dated 2026), so not
  clean matches — correct to not force.
- **Oscar Best Picture, single MLB games, GTA price**: no live Polymarket
  counterpart → genuine non-overlap, not a false negative.

No NEW false-negative class surfaced — signal that iters 3–12 covered the
major ones; remaining work is the documented harder items (9b ranges, 11b
brittle names).

### 13a. Broad health check — 1835 pairs (all categories)
`discover.py --category all --max-events 120 --poly-scan 9000` → 1835 pairs.
Audited: 22 flagged (1.2%), and on inspection almost all are AUDIT artifacts,
not engine errors:
- ~19 "gap≥2 year" flags are CORRECT Marvel "Avengers: Doomsday" cast roles
  ("Don Cheadle as War Machine" ↔ Kalshi same). The year delta is from differing
  CLOSE DATES; titles carry no cycle token, so the engine rightly matches.
- 2 "RATE 5.0% vs 5%" flags are correct (iter-9 threshold fix; the audit's
  string check is cruder than the engine).
- **1 genuine low-confidence FP (#1777):** "LeBron James retire before next NBA
  season" (2026) ↔ "...before the 2029-30 NBA season" (conf 0.36) — same
  subject, different horizon. The Polymarket side has no year token so the year
  veto can't fire; close times are far apart but that is only a scoring signal.

**Conclusion:** engine is healthy at scale. No code change shipped this
iteration (no clean false negative found; the lone FP is low-confidence and a
hard horizon veto would risk the deadline-ladder matches that currently work —
iter-6/12). Recorded the LeBron-style same-subject/different-horizon FP as a
candidate for a future scoring-level (not veto) horizon penalty.

## Iteration 14 — 2026-06-10 (nomination = nominee; HIGH VALUE)

Data-driven: pulled top Polymarket markets by VOLUME and checked each for a
matched Kalshi twin. The biggest (2026 FIFA World Cup winner, $40M+) has NO
Kalshi champion market (only props) — genuine non-overlap. But the next cluster,
**2028 Democratic/Republican nomination** ($40–53M each: Oprah, Bernie, Newsom,
…), exposed a big false negative.

### 14a. REAL false negative — "win the nomination" ≠ "be the nominee"
`KXPRESNOMD-28` "Will <X> be the Democratic Presidential nominee in 2028?" vs
Polymarket "Will <X> win the 2028 Democratic presidential nomination?": **0
matched**. `_contract_actions` produced `win_nomination` for "win the
nomination" but `nomination` for "be the nominee", and the action-disjoint veto
rejected every pair — even Newsom↔Newsom.

**Fix:** collapsed the two into a single `nomination` action (removed
`win_nomination` from `_contract_actions` and `_ARB_ACTIONS`). Result: 0 → **35
matches**, all correct (AOC, Newsom, Buttigieg, Shapiro …).

### 14b. Regression the fix exposed — and its fix
The action distinction had been ACCIDENTALLY masking a VP-vs-President bug:
after collapsing, "presidential nomination" matched "Vice Presidential nominee"
(False positive). Root cause: `_offices` matched the president pattern inside
"vice **presidential**". **Fixed** `_offices` to detect `vice_president`
distinctly (and drop the spurious `president`). Now pres-nom ↔ VP-nom is
rejected, while pres↔pres and VP↔VP still match.

**Verified:** suite 53 → **55 passing** (nomination-match + office-separation
tests). Election discover jumped **86 → 161 pairs** with 0 structural flags;
iter-1 VP-nominee matches (Trump Jr ↔ VP nominee) preserved. No regression.

**Why high value:** these were the highest-volume Polymarket markets on the
exchange and were 100% missed before.

## Iteration 15 — 2026-06-10 ("head of state" / leave-vs-hold)

Continued volume-driven hunt. Many top markets have no Kalshi twin (FIFA World
Cup champion, US-invade-Iran, China-invade-Taiwan — genuine non-overlap). The
2028 Presidential-winner cluster (`KXPRESPERSON-28`) still matches (25 pairs,
iter-3 intact). Venezuela leadership exposed a false negative.

### 15a. REAL false negative — "head of state" tagged as election (bare "state")
`KXVENEZUELALEADER-26DEC31` "Will <X> be the head of state of Venezuela?" vs
Polymarket "Will <X> be the leader of Venezuela?": **0 matched**. Same class as
iter-6's bare "president": "state" (in "head of state") was an `_ELECTION_TERMS`
word, so Kalshi got `election` domain, Polymarket ("leader") did not, and the
`election-and-not-other` veto rejected all pairs — even Machado↔Machado.

**Fix:** removed bare "state" from `_ELECTION_TERMS` ("Secretary of State" is
still caught via "secretary"; real election markets carry stronger signals).
Also added venezuela/iran/north korea to `_COUNTRY_ALIASES`. Result: 0 → **14
matches**, all correct (Machado, Cabello, Padrino López …).

### 15b. False positive the fix exposed — leave vs hold
With Venezuela pairs now evaluated, "Delcy Rodríguez **out as** leader"
(leaving) matched "Delcy Rodríguez **be the head of state**" (holding) — opposite
outcomes. "out as X" is a common Polymarket phrasing (Putin/Trump out), so this
would systematically mismatch complementary markets. **Fixed**
`_contract_actions`: "out as / leave / resign / step down" → `leave_role`;
"head of state / be the leader of / officially|de facto lead / in power" →
`hold_office`. The two are disjoint actions, so leave never matches hold.

**Verified:** Venezuela 14 matches, 0 out-as FPs; "out as" vs "be head" rejected;
"be leader" vs "be head" matches. Election discover steady at 161 pairs, 0 flags
(removing "state" caused no regression). Suite 55 → **57 passing** (2 new).

## Iteration 16 — 2026-06-10 (seat vs chamber; conjunctive parlay)

Volume hunt: ETH thresholds validated (5/8 match; the 3 misses have no
Polymarket rung — correct). Chamber-control markets exposed a false negative
PLUS a false positive.

### 16a. REAL FN + FP — single seat vs chamber-wide control
`CONTROLH-2026` "Will Republicans win the House in 2026?" (chamber control)
matched Polymarket "Will the Republican Party win the **IN-01 House seat**?" (one
district) — wrong scope — and the correct "control the House after the 2026
Midterm" market LOST to it in greedy. The matcher had no seat-vs-chamber notion.

**Fix:** `_legislative_scope(text)` → `seat` (district code `xx-##`, "house/
senate seat", "congressional district") vs `chamber` (control/majority/win the
House/Senate). Veto when one side is `seat` and the other `chamber`.

### 16b. Second blocker — conjunctive parlay starved the correct match
Even after 16a, an ACA combo "Will ACA credits not be extended **and will** the
GOP win the House?" outscored the real chamber market. `_is_parlay_market` did
not catch it (only "yes X, yes Y" and 3+ "win"). **Fixed:** added an
`\band will\b` signal (two separate will-clauses = parlay). Legit single markets
(nomination, BTC, recession) are not filtered.

**Verified:** the correct chamber pairs now match (Dem↔Dem, Rep↔Rep, conf 0.55);
IN-01 seat vetoed; ACA parlay filtered. Election discover steady at 161 pairs.
Suite 57 → **59 passing** (2 new tests). Note: the regression test first failed
because real Kalshi titles carry the party via `yes_sub_title`
("...? Republican Party") — confirming the engine was right and the unrealistic
test was wrong; fixed the test.

## Iteration 17 — 2026-06-10 (validation; 11b recurrence)

Probed several areas; validated working behavior, found no clean new FN:
- **Senate control** (`CONTROLS-2026`): the iter-16 seat/chamber + parlay fix
  generalises — "control the Senate" ↔ "win the U.S. Senate" matches (Dem↔Dem,
  Rep↔Rep).
- **California governor** (`KXGOVCA-26` "Who will win the governorship in
  California? <cand>" ↔ Polymarket "Will <cand> win the California Governor
  Election in 2026?"): matches correctly (Hilton↔Hilton, Becerra↔Becerra).
- **AI-model ladders** (`KXTOPAI-27` "Will <company> have a top-ranked AI model
  before 2027?"): NO clean FN. Polymarket's company markets are "end of June
  2026" horizon (correctly year-vetoed vs Kalshi's "before 2027"), and
  Polymarket's only "before 2027" AI markets are a FrontierMath benchmark and
  "a dLLM" (a model TYPE) — neither a company. So no same-horizon company twin
  exists.

### 17a. 11b brittle-name FP recurred (recorded, not fixed)
The AI probe surfaced a high-confidence false positive: "Will **a dLLM** be the
top AI model before 2027?" ↔ "Will **Z.ai** have a top-ranked AI model before
2027?" (conf 0.77). Both "dLLM" and "Z.ai" fail `_proper_names`/entity
extraction, so the shared scaffolding matches. Same family as Xi↔Minneapolis
(iter-11). This is now the clearest recurring defect.

**Why still deferred:** the natural fix (distinctive-token-overlap veto) was
re-analysed and is risky: scoping by "shared scaffolding ≥3 tokens" avoids the
recession case, and scoping out sports avoids Chiefs↔Kansas City, BUT it can
re-break dotted-initial candidate names ("JD Vance" vs "J.D. Vance", iter-4) and
needs substring-aware comparison for spelling variants. Worth doing as a
dedicated pass with the full regression suite, not inline here.

No code change this iteration (no clean FN; the lone FP is the high-risk 11b
family). Two real fixes already shipped in iters 14–16.

## Iteration 18 — 2026-06-10 (party race labelled with candidate)

(First re-confirmed 11b is too fragile to ship: tracing the JD-Vance tokens, a
distinctive-token veto would treat scaffolding variation "2028" vs "next" as an
entity mismatch and re-break iter-4. Kept deferred.)

### 18a. REAL false negative — party race row labelled with the nominee
Georgia 2026 Senate (`SENATEGA-26`): the Republican pair matched but the
Democrat pair did NOT. Kalshi labels the Republican row "Republican party" but
the Democrat row with the nominee ("Will Democratics win the Senate race in
Georgia? **Jon Ossoff**"). The appended `yes_sub_title` gave the Kalshi side a
proper name, so the "named-candidate vs party-contract" veto rejected it against
Polymarket's party-level "Will the Democrats win the Georgia Senate race?".

**Fix:** added a same-party exception to that veto — when BOTH sides assert the
SAME party, the named side is that party's one candidate for the race, so do not
reject. Office/jurisdiction/year vetoes still apply, so cross-state/cross-party
pairs stay rejected.

**Verified:** Georgia Senate now matches BOTH parties; GA-Dem vs TX-Dem still
rejected (jurisdiction); the existing named-vs-party test (Caruso vs Labour,
no shared party) still passes. Election discover **161 → 194 pairs**, 0 flags.
Suite 59 → **60 passing** (1 new test). Affects every Senate/House/Governor race
where Kalshi labels a party row with the nominee — a broad fix.

## Iteration 19 — 2026-06-10 (count-threshold ladders / win totals)

Validated: **House district races** match at scale — hundreds of districts exist
on BOTH exchanges; AL-01 matches both parties (iter-16 scope + iter-18 party +
iter-3 yes_sub_title combine). Then found a new FN in MLB win totals.

### 19a. REAL false negative — game-count threshold ladders
`KXMLBWINS-TOR-26` "Will Toronto win at least 85 games this season?" vs
Polymarket "Will the Toronto Blue Jays win more than 84.5 games?": **0 matched**.
`_numeric_threshold` only parsed $ and % — not game counts — so the ladder rung
could not be aligned, and "more than 84.5" ≠ "at least 85" by tokens.

**Fix (3 parts):**
1. Extended `_numeric_threshold` to parse INTEGER COUNT thresholds, scoped to a
   count noun (games/wins/seats/points/…) so bare numbers/years are ignored.
   Normalises to the integer CUTOFF — "more than 84.5"→85, "at least 85"→85,
   "85+ wins"→85 — so the two phrasings are recognised as the same level while
   "at least 90"→90 stays distinct. `_threshold_equal` compares counts exactly.
2. A second blocker: "win **more than** 84.5 games" got the `comparison` action
   (for head-to-head "more X than Y"), but "at least 85" did not → the
   comparison-asymmetry veto fired. Excluded "more than \<number\>" from the
   comparison action (it is a threshold, not a comparison).

**Verified:** Toronto now matches the correct rung only (84.5↔85, conf 0.49; NOT
↔90/80). Real head-to-head comparison ("win more games than the Red Sox") still
detected. Sports discover steady (~37 pairs); suite 60 → **62 passing** (2 new).
Generalises to all 30 teams and other count ladders (seats/points/goals).

## Iteration 20 — 2026-06-10 (labelled fixture integration)

User supplied a labelled fixture (`tests/fixtures/pairs_fixture.json`, 50 pairs
with ground-truth should_match + match_type: exact / paraphrase / inverted /
date_mismatch / strike_mismatch / settlement_mismatch / outcome_mismatch /
entity_mismatch). Built `diag_eval_fixture.py` to score the matcher and added
`tests/test_fixture_pairs.py` (locks fixed pairs + accuracy baseline).

**Baseline:** 8 compat false-neg, 4 compat false-pos, 11 engine-only false-neg.

### 20a. Fixes shipped
- **027/028 NBA "Finals" vs "Championship":** "NBA Finals"/"NBA Championship"
  were phantom proper names (because "nba" was not a generic NAME term), blocking
  the team-name overlap. Added league acronyms (nba/nfl/nhl/mlb/mls/wnba/ncaa) to
  `_GENERIC_NAME_TERMS` (name-extraction only; `_sports_league` regex unaffected,
  so NBA-vs-WNBA still separates). Fixed 027.
- **034 goals "number of ... over 2.5":** got the `count` action vs an
  "over 2.5 goals" phrasing → count-asymmetry veto. Excluded
  "number of X over/under \<n\>" from `count` (it is a threshold). Fixed.
- **011/013 crypto paraphrase ("BTC above $150k" vs "Bitcoin above $150,000"):**
  the iter-8 threshold-led path needs a shared entity, but "BTC" and "Bitcoin"
  did not unify. Added ticker→name synonyms (btc→bitcoin, eth→ethereum, sol, xrp,
  doge) in `_named_entities`. Both now matched via threshold-led path.

**After:** 6 compat FN / 4 compat FP / 9 engine FN. Suite 62 → **65 passing**.

### 20b. Remaining fixture gaps (recorded for next iterations)
- **Engine-only FNs (9) — the dominant theme: paraphrase/abbreviation** where
  compat passes but title Jaccard < 0.30. "year-end 2026"="Dec 31 2026",
  "S&P 500 above 7000"="S&P 500 above 7,000", GPT-6, Waymo NYC, GTA VI/GTA 6,
  "crewed lunar landing"="walk on the Moon", "SCOTUS vacancy"="justice retire or
  die". Needs a synonym/abbreviation layer or threshold/entity-led acceptance for
  non-crypto numeric markets (S&P level → numeric threshold like crypto).
- **Compat FPs (4):** 015 settlement ("above $175k ON Dec 31" vs "TOUCH $175k
  any time"), 020 date (Sept vs July FOMC — month-level deadline not vetoed),
  030 outcome ("reach the final" vs "win"), 041 entity (Anthropic/Claude vs
  OpenAI/GPT — disjoint named entities not vetoed outside token_launch).
- **Compat FNs (6):** 016 (Solana ETF $5B — "over" vs "above" + SOL/Solana),
  017 (inverted below/above $80k — by design hard), 021 (CPI above 3.0%),
  028 (Pacers — "Pacers" alone not entity-extracted on Kalshi side),
  042 (Avengers box office $1.5B), 046 ("James Bond" vs "New Bond" — "New"
  captured into the name).

**Highest-leverage next:** (a) generalise numeric-threshold-led acceptance to
S&P/index levels and an entity-disjoint veto for FP-041; (b) a month-level
deadline veto for FP-020; (c) "reach round" vs "win" predicate for FP-030.

### Open questions / not yet done
- [ ] Fixture paraphrase FNs: abbreviation/synonym layer + extend threshold-led
      acceptance to index levels (S&P 7000) and dated thresholds.
- [ ] Fixture compat FPs: entity-disjoint veto (Anthropic≠OpenAI), settlement
      (ON-date vs TOUCH), month-level FOMC date, reach-vs-win predicate.
- [ ] PRIORITISE 11b: distinctive-token-overlap veto (substring-aware,
      non-sports, shared-scaffolding≥3) for selection ladders — fixes
      Xi↔Minneapolis and dLLM↔Z.ai. Guard against the JD-Vance dotted-initial
      regression (iter-4) and spelling variants.
- [ ] Same-subject/different-horizon low-conf FPs (e.g. LeBron retire 2026 vs
      2029-30): consider a scoring penalty (not a hard veto) when close-time
      delta is very large and one side lacks a year token.
- [ ] Distinctive-token overlap veto for winner/selection ladders (11b) —
      substring-aware AND team-alias-aware (must not re-break Chiefs↔Kansas City
      from iter-5); fixes the unextractable-name leftover FPs (Xi↔Minneapolis).
- [ ] Two-sided percent-range interval veto + IoU matching (9b); add a
      negative-vs-positive sign guard (kills "Negative GDP" ↔ positive bucket).
- [ ] Date-bucket disjointness for IPO-timing ladders ("Dec 31" vs "Before
      Nov 1") — analogous to numeric thresholds but on deadlines.
- [ ] Sitting-office alias normalization ("the President" ⇄ Trump) — risky,
      time-sensitive; only if a range-aware price-led path proves insufficient.
- [ ] Extend `_sports_league` synonyms (college vs pro, MLS/EPL/UCL, tennis,
      golf) as more cross-league collisions surface.
- [ ] (latent) Stop `event_id` ticker tokens (e.g. `KXPRESPERSON`) leaking into
      `_proper_names` / `_snapshot_text`-derived names. Did not block JD Vance
      after iter-4 fix, but can cause other subtle name mis-extractions.
- [ ] Deeper direction: explicit outcome-label matching (Kalshi `yes_sub_title`
      ↔ Poly outcome) for candidate ladders instead of title-regex names.
- [ ] Re-run full discover per category to confirm iter-3/4 surface the new
      candidate-winner pairs at scale (and add no false positives).
- [ ] Audit sports more deeply (only 3 pairs found — likely more false
      negatives from team-name vs city-name and fixture-date differences).
- [ ] Audit pop category (0 pairs — investigate whether truly no overlap or
      another title/identity gap).
- [ ] Re-run economic/election with wider `--max-events` to confirm 0 mismatches
      at larger scale (iteration 1–2 used 50–60 event caps).
- [ ] Consider the general "distinctive-token overlap" veto as a backstop for
      other shared-template predicate pairs (deferred — synonym risk).
- [ ] Latent risk: year veto reads ticker text only, never `close_time`.
      If a Kalshi market lacks a year in title AND ticker while Poly has one,
      the disjoint check is skipped. Reproduced synthetically ("title only"
      case → compatible=True). Consider folding `close_time` year into the veto.
- [ ] `close_delta_days=None` when one side has no close time (Poly often does);
      both the close-time guard and any close-based year inference go blind.
- [ ] Delete / git-ignore stale `pairs.json` (34% mismatched, pre-fix output).
