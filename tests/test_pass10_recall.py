"""Pass 10 (2026-09-20) recall + precision fixes: one regression test per
diagnosed class, from live-catalog bucket analysis (see docs/EXPANSION_PROPOSAL.md
pass 10). Hermetic — no network.

Recall classes:
  1. close-time 400-day cap over-firing on identical contracts
  2. esports/short-handle outcome labels
  3. diacritics / party suffixes / hyphenation
  4. election-domain one-sided veto + paraphrased references
  5. v2 residual over-fires (parenthetical disambiguators, label-vs-question sim)
Precision classes:
  7a. earnings-call topics joining across companies
  7b. college vs pro basketball
  7c. one-sided finishing position vs a win question
"""
from __future__ import annotations

import unittest

from contract_spec import explain
from discover import _match_outcomes_within_group
from matcher import (
    _same_outcome_label,
    _tokens,
    context_veto,
    is_close_time_compatible,
    is_compatible_match,
    match_markets,
)
from pipeline import MarketSnapshot, OrderBook


def pm(label, event, close=None):
    return MarketSnapshot("polymarket", f"p:{label}:{event}", "pe", label, "open",
                          close, "", OrderBook(bids=[], asks=[]),
                          extra={"event_title": event})


def ks(title, event, sub="", close=None):
    return MarketSnapshot("kalshi", f"k:{title}", "ke", title, "active", close, "",
                          OrderBook(bids=[], asks=[]),
                          extra={"event_title": event, "yes_sub_title": sub})


class CloseTimeCap(unittest.TestCase):
    """Fix 1: the 400-day non-sports cap rejects identical contracts whose
    far-out close is bookkeeping (formal expiry), not scope."""

    def test_identical_titles_far_closes_accepted(self):
        # Live Mamdani pair: verbatim titles, PM closes 2031 vs Kalshi 2027.
        p = pm("Will Mamdani raise the minimum wage to $30",
               "Will Mamdani raise the minimum wage to $30", close="2031-01-01T04:59:00Z")
        k = ks("Will Mamdani raise the minimum wage to $30 before 2027?",
               "Will Mamdani raise the minimum wage to $30", sub="Yes", close="2027-01-01T04:59:00Z")
        self.assertTrue(is_close_time_compatible(p, k))

    def test_word_order_variant_far_closes_accepted(self):
        # Live pair: same question, reversed name order, Kalshi formal 2040 expiry.
        p = pm("Will Anthropic or OpenAI IPO first?", "Will Anthropic or OpenAI IPO first?",
               close="2028-01-01T04:59:00Z")
        k = ks("Will OpenAI or Anthropic IPO first?", "Will OpenAI or Anthropic IPO first?",
               sub="OpenAI", close="2040-01-01T04:59:00Z")
        self.assertTrue(is_close_time_compatible(p, k))

    def test_identical_titles_pair_end_to_end(self):
        p = pm("Will Mamdani raise the minimum wage to $30",
               "Will Mamdani raise the minimum wage to $30", close="2031-01-01T04:59:00Z")
        k = ks("Will Mamdani raise the minimum wage to $30 before 2027?",
               "Will Mamdani raise the minimum wage to $30", sub="Yes", close="2027-01-01T04:59:00Z")
        pairs = match_markets([p], [k], min_title_similarity=0.45, max_close_delta_hours=100_000)
        self.assertEqual(len(pairs), 1)

    def test_different_titles_far_closes_still_rejected(self):
        p = pm("Recession in 2026?", "US recession in 2026?", close="2027-01-01T00:00:00Z")
        k = ks("Will the UK enter a recession before 2030?", "UK recession?", close="2030-01-01T00:00:00Z")
        self.assertFalse(is_close_time_compatible(p, k))


class EsportsHandles(unittest.TestCase):
    """Fix 2: digit-bearing esports handles and 4-char handles are outcomes."""

    def test_same_outcome_label_short_handle(self):
        p = pm("bang", "VALORANT Champions 2026 MVP")
        k = ks("Will bang win Tournament MVP at VALORANT Champions Shanghai?",
               "VALORANT Champions Shanghai MVP", sub="bang")
        self.assertTrue(_same_outcome_label(p, k))

    def test_digit_handle_group_pairing(self):
        # Live f0rsakeN pair: group path must lift the verbatim label even
        # though it contains digits.
        k = [ks("Will f0rsakeN win Tournament MVP at VALORANT Champions Shanghai?",
                "VALORANT Champions Shanghai MVP", sub="f0rsakeN")]
        p = [pm("f0rsakeN", "VALORANT Champions 2026 MVP")]
        pairs = _match_outcomes_within_group(k, p, 0.6)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].kalshi.market_id, k[0].market_id)

    def test_four_char_handle_compatible(self):
        # Live s0pp pair: the selected-names veto fired on phantom phrase
        # "names" extracted from both sides ("VALORANT Champions" /
        # "Tournament MVP"); the 4-char handle now counts as outcome identity.
        p = pm("s0pp", "VALORANT Champions 2026 MVP")
        k = ks("Will s0pp win Tournament MVP at VALORANT Champions Shanghai?",
               "VALORANT Champions Shanghai MVP", sub="s0pp")
        self.assertIsNone(context_veto(p, k))
        self.assertTrue(is_compatible_match(p, k))


class AccentsAndSuffixes(unittest.TestCase):
    """Fix 3: diacritic folding, party suffixes, hyphen variants."""

    def test_tokens_fold_diacritics(self):
        self.assertEqual(_tokens("Vinícius Júnior"), _tokens("Vinicius Junior"))
        self.assertEqual(_tokens("Fenerbahçe"), _tokens("Fenerbahce"))
        self.assertEqual(_tokens("Te Pāti Māori"), _tokens("Te Pati Maori"))

    def test_diacritic_name_pair_compatible(self):
        p = pm("Vinicius Junior", "Ballon d'Or Winner 2026")
        k = ks("Who will win the Ballon d'Or in 2026? Vinicius Junior",
               "Who will win the Ballon d'Or in 2026?", sub="Vinicius Junior")
        self.assertTrue(is_compatible_match(p, k))

    def test_hyphen_variant_label_lift(self):
        # Live Junghwan Lee pair: "Junghwan Lee" vs "Jung-Hwan Lee" must
        # squash-equal to one outcome in the group path.
        k = [ks("Junghwan Lee finishes top 20", "Bmw Pga Championship: Top 20 Finishers",
                sub="Junghwan Lee")]
        p = [pm("Jung-Hwan Lee", "DP World Tour: BMW PGA Championship Top 20")]
        pairs = _match_outcomes_within_group(k, p, 0.55)
        self.assertEqual(len(pairs), 1)

    def test_party_suffix_label_lift(self):
        # Live governor pair: the label lift strips "(R)".
        k = [ks("Will the Republican party win the governorship in New Hampshire Kelly Ayotte",
                "New Hampshire Governor winner?", sub="Kelly Ayotte")]
        p = [pm("Kelly Ayotte (R)", "New Hampshire Governor Election Winner")]
        pairs = _match_outcomes_within_group(k, p, 0.67)
        self.assertEqual(len(pairs), 1)


class ParaphrasedReferences(unittest.TestCase):
    """Fix 4: identical event + identical label is one contract even when one
    side's wording dodges the domain/action parsers."""

    def test_clarity_act_paraphrase(self):
        # Live pair: Kalshi's indirect bill reference vs PM's "Clarity Act".
        p = pm("Above 66", "How many Senators will vote for the Clarity Act?")
        k = ks("How many Senate members will vote Yea on a crypto market structure "
               "bill (as defined in KXCRYPTOSTRUCTURE)? Above 66",
               "How many Senators will vote for the Clarity Act?", sub="Above 66")
        self.assertTrue(is_compatible_match(p, k))

    def test_placement_event_fast_path(self):
        # Live Berlin pair: identical event title, identical label.
        p = pm("Grüne", "Berlin State Election: 2nd Place")
        k = ks("Will Grüne finish 2nd in the 2026 Berlin state election?",
               "Berlin State Election: 2nd Place", sub="Grüne")
        self.assertTrue(is_compatible_match(p, k))

    def test_office_mismatch_still_vetoed(self):
        # Precision guard: same person, different office must NOT ride the
        # label-identity relaxation.
        p = pm("Pete Hegseth", "Republican VP Nominee 2028")
        k = ks("Will Pete Hegseth be the nominee for the Presidency for the Republican party?",
               "2028 Republican presidential nominee", sub="Pete Hegseth")
        self.assertFalse(is_compatible_match(p, k))


class V2Residual(unittest.TestCase):
    """Fix 5: v2 (contract_spec) over-fires."""

    def test_parenthetical_disambiguator_not_a_surname(self):
        # Live pair: "Gary" is a song (performed by Stephen Wilson Jr.);
        # the parenthetical must not create a phantom second person.
        p = pm("Gary (Stephen Wilson Jr.)", "CMA Song of the Year 2026")
        k = ks("Will Gary win Song of the Year at the 60th Country Music Association Awards?",
               "CMA Awards: Song of the Year", sub="Gary")
        self.assertTrue(explain(p, k).match)

    def test_identical_event_label_acceptance(self):
        # Live pair: event titles identical, but the Kalshi question is a
        # 40-word description so full-text similarity is 0.23.
        p = pm("SELF DRIVE Act", "Which bills will become law in 2026?")
        k = ks("Will legislation that preempts state and local autonomous vehicle laws "
               "by establishing a unified federal regulatory framework for the testing "
               "and deployment of vehicles equipped with automated driving systems "
               "become law before Jan 1, 2027? SELF DRIVE Act",
               "Which bills will become law in 2026?", sub="SELF DRIVE Act")
        self.assertTrue(explain(p, k).match)

    def test_different_labels_same_event_not_accepted(self):
        p = pm("Above 68", "How many Senators will vote for the Clarity Act?")
        k = ks("How many Senators will vote for the Clarity Act? Above 66",
               "How many Senators will vote for the Clarity Act?", sub="Above 66")
        self.assertFalse(explain(p, k).match)


class EarningsCompanyPrecision(unittest.TestCase):
    """Fix 7a: shared earnings-call topics must not join across companies."""

    def test_cross_company_earnings_rejected(self):
        # Live false positive: PepsiCo GLP-1 mention paired with Costco's.
        p = pm("GLP-1 / GLP 1", "What will Costco say during their next earnings call?")
        k = ks("What will PepsiCo, Inc. say during their next earnings call? GLP-1",
               "What will PepsiCo say during their next earnings call?", sub="GLP-1")
        self.assertFalse(is_compatible_match(p, k))

    def test_same_company_earnings_still_matches(self):
        p = pm("GLP-1 / GLP 1", "What will Costco say during their next earnings call?")
        k = ks("What will Costco say during their next earnings call? GLP-1",
               "What will Costco say during their next earnings call?", sub="GLP-1")
        self.assertTrue(is_compatible_match(p, k))


class CollegeVsProPrecision(unittest.TestCase):
    """Fix 7b: college and pro basketball champions are different contracts."""

    def test_college_vs_pro_basketball_rejected(self):
        # Live false positive: Miami (college champion) paired with the NBA market.
        p = pm("Miami", "2027 Men's College Basketball National Champion")
        k = ks("Will Miami win the 2027 Pro Basketball Finals?",
               "2027 Pro Basketball Champion", sub="Miami")
        self.assertFalse(is_compatible_match(p, k))

    def test_same_league_pair_unaffected(self):
        p = pm("Miami", "2027 Pro Basketball Champion")
        k = ks("Will Miami win the 2027 Pro Basketball Finals?",
               "2027 Pro Basketball Champion", sub="Miami")
        self.assertTrue(is_compatible_match(p, k))


class FinishingPositionPrecision(unittest.TestCase):
    """Fix 7c: a placement contract vs a win question on the same contestant."""

    def test_one_sided_place_vs_win_rejected(self):
        # Live false positive: Tarcísio de Freitas 3rd-Place outcome paired
        # with the "win the first round" question.
        p = pm("Tarcisio de Freitas", "Brazil Presidential Election First Round: 3rd Place")
        k = ks("Will Tarcísio de Freitas win the first round of the 2026 Brazilian "
               "presidential election?",
               "Brazil presidential election: first round winner?", sub="Tarcísio de Freitas")
        reason = context_veto(p, k)
        self.assertIsNotNone(reason)
        self.assertIn("finishing position", reason)
        self.assertFalse(is_compatible_match(p, k))

    def test_same_place_pair_unaffected(self):
        # Both sides carry the same position — a true pair, must survive.
        p = pm("Grüne", "Berlin State Election: 3rd Place")
        k = ks("Will Grüne finish 3rd in the 2026 Berlin state election?",
               "Berlin State Election: 3rd Place", sub="Grüne")
        self.assertIsNone(context_veto(p, k))
        self.assertTrue(is_compatible_match(p, k))

    def test_win_label_pair_unaffected(self):
        # No position tokens anywhere — a plain winner pair, must survive.
        p = pm("New York Yankees", "MLB: 2026 World Series Winner")
        k = ks("Will the New York Yankees win the 2026 World Series?",
               "MLB: 2026 World Series", sub="New York Yankees")
        self.assertIsNone(context_veto(p, k))


if __name__ == "__main__":
    unittest.main()
