# Matching Safety Notes

The pipeline should prefer missing a questionable pair over emitting a false
positive. A false positive can create a fake arbitrage signal; a false negative
only means the opportunity is not considered.

## Matching Stages

1. **Catalog discovery** gathers Kalshi events and Polymarket events.
2. **Event-group matching** compares parent event titles.
3. **Outcome matching** compares markets/outcomes inside matched event groups.
4. **Fallback matching** compares remaining individual markets.
5. **Arbitrage detection** runs only after a pair has passed matching.
6. **CLOB verification** rechecks live prices before a signal is trusted.

## Deterministic Vetoes

`matcher.is_compatible_match` rejects pairs when structured facts conflict:

- **Domain mismatch**: sports vs election, election vs economic, etc.
- **Office mismatch**: president vs Senate vs governor vs mayor.
- **Party mismatch**: Democratic vs Republican vs Conservative.
- **Jurisdiction mismatch**: country or U.S. state disagreement.
- **Named-person mismatch**: different candidates or people.
- **Generic winner mismatch**: named candidate rows against generic "who wins"
  markets.
- **Party-vs-person mismatch**: a candidate contract against a party contract.
- **Predicate mismatch**: win vs run/declare vs ticket vs occur vs finish
  position vs location.
- **Year mismatch**: explicit years disagree.
- **Rate mismatch**: explicit numeric percentage/rate levels disagree.

These vetoes are deliberately conservative. If a future true match is blocked,
add a regression test that proves why it should pass, then narrow the veto.

## Group Outcome Rule

Inside a matched event group, price similarity is not enough. Outcome labels must
share lexical evidence before price proximity can help. This prevents cases like
`"Andy Beshear"` matching `"Who will win the next presidential election?"`
because both rows happen to have similar catalog prices.

## Regression Samples

The test suite covers known false-positive patterns:

- French presidential candidate vs Turkish presidential winner.
- South Carolina governor vs South Dakota Senate.
- Named candidate vs generic presidential winner.
- Win presidency vs first-to-declare.
- Win election vs run for office.
- Named candidate vs party contract.
- Generic outcome group rows that share price but no label overlap.

## Operational Guidance

- Use `--show-prices` only after pair matching; live CLOB calls are expensive.
- Treat unverified signals as audit information, not executable signals.
- Keep `--min-match-sim` conservative when scanning broad categories.
- Add manual overrides only for pairs with matching resolution criteria, not just
  similar titles.
- Review `pairs.json` samples after matcher changes and convert bad examples
  into tests.

