"""Pytest configuration for the repo-root test collection.

smoke_test.py is a standalone CLI probe (run via `python smoke_test.py`) that hits
the LIVE Polymarket/Kalshi APIs. Its functions are named test_polymarket/test_kalshi,
which match pytest's default `*_test.py` pattern, so without this it would be
collected and execute live network calls during the (otherwise hermetic) unit
suite — making it slow and flaky. Exclude it from collection; it stays runnable
directly as a manual smoke check (#18).
"""

collect_ignore = ["smoke_test.py"]
