from __future__ import annotations

from contract_spec import explain
from matcher import context_veto
from pipeline import MarketSnapshot, OrderBook, PriceLevel


def snap(title: str, event: str) -> MarketSnapshot:
    return MarketSnapshot('x', title[:12], '', title, 'open', '2026-12-31', 'x',
                          OrderBook(bids=[PriceLevel(.4, 10)], asks=[PriceLevel(.5, 10)]),
                          extra={'event_title': event})


def test_relative_standings_vs_champion_rejected_in_both_engines():
    a = snap('F1: Will Carlos Sainz finish ahead of Fernando Alonso in the 2026 Drivers Championship?',
             'F1 standings')
    b = snap('Will Fernando Alonso win the F1 Drivers Championship?', 'F1 Drivers Champion')
    assert context_veto(a, b) == 'relative standings vs championship winner'
    assert not explain(a, b).match


def test_equivalent_relative_standings_is_not_vetoed():
    a = snap('Will Carlos Sainz finish ahead of Fernando Alonso?', 'F1 standings')
    b = snap('Carlos Sainz ahead of Fernando Alonso in standings?', 'F1 standings')
    assert context_veto(a, b) is None


def test_brazilian_gubernatorial_regions_rejected_but_same_region_kept():
    a = snap('Renan Filho', 'Alagoas Governor Election Winner')
    b = snap('Will Renan win the 2026 Pernambuco gubernatorial election?', 'Pernambuco gubernatorial election winner?')
    assert context_veto(a, b) == 'Brazilian gubernatorial jurisdiction mismatch'
    assert not explain(a, b).match
    c = snap('Renan Filho', 'Alagoas Governor Election Winner')
    d = snap('Will Renan Filho win the 2026 Alagoas gubernatorial election?', 'Alagoas gubernatorial election winner?')
    assert context_veto(c, d) is None
