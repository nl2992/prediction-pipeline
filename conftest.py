"""Pytest configuration for the repo-root test collection.

tools/smoke_test.py is a standalone CLI probe (run via `python -m tools.smoke_test`) that hits
the LIVE Polymarket/Kalshi APIs. Its functions are named test_polymarket/test_kalshi,
which match pytest's default `*_test.py` pattern, so without this it would be
collected and execute live network calls during the (otherwise hermetic) unit
suite — making it slow and flaky. Exclude it from collection; it stays runnable
directly as a manual smoke check (#18).
"""

collect_ignore = ["tools/smoke_test.py"]

import pytest


@pytest.fixture(autouse=True)
def _isolate_dashboard_db(tmp_path, monkeypatch):
    """store.py defaults to data/dashboard.db; every test gets its own tmp
    path via PRED_DASHBOARD_DB so the unit suite never reads or writes the
    real dashboard database."""
    monkeypatch.setenv("PRED_DASHBOARD_DB", str(tmp_path / "test-dashboard.db"))
