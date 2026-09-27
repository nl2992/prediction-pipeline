"""Tests for scan_jobs.py (Phase 2c background scan jobs).

Exercises the module's public start()/status()/cancel() through fake
discover() implementations that print progress and sleep briefly — no real
network, no TestClient/httpx (route functions aren't even involved here;
scan_jobs calls server._execute_scan directly, same as the real worker
would). Every wait uses a short deadline-bounded poll so a bug can never
hang the suite.
"""

from __future__ import annotations

import sys
import time
import unittest
from unittest.mock import patch

import scan_jobs
import store


def _poll_until(predicate, timeout=5.0, interval=0.02):
    deadline = time.time() + timeout
    job = scan_jobs.status()
    while time.time() < deadline:
        job = scan_jobs.status()
        if predicate(job):
            return job
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for condition; last job={job!r}")


def _slow_fake_discover(ticks=40, delay=0.03):
    """Prints "[1/4] a" then "[2/4] b" followed by many more "[2/4] ..."
    progress lines with a short sleep between each — gives tests a wide
    window to observe 'running' state, parse stage, or cancel mid-flight."""
    def fake(**kwargs):
        print("[1/4] a", flush=True)
        time.sleep(delay)
        print("[2/4] b", flush=True)
        for i in range(ticks):
            time.sleep(delay)
            print(f"[2/4] b tick {i}", flush=True)
        return [{"arb_net_profit": 0.01}]
    return fake


def _fast_fake_discover(pairs=None):
    def fake(**kwargs):
        print("[1/4] a", flush=True)
        print("[2/4] b", flush=True)
        print("[3/4] c", flush=True)
        print("[4/4] d", flush=True)
        return pairs if pairs is not None else [{"arb_net_profit": 0.02}]
    return fake


class ScanJobsTestCase(unittest.TestCase):
    """Resets scan_jobs' module-global state (job/thread/cancel flag/stdout
    proxy) before and after every test — it's process-wide singleton state,
    not per-instance, so tests would otherwise bleed into each other (and,
    left installed, the stdout proxy would keep wrapping a stale reference
    to a stdout object pytest has since swapped out from under it)."""

    def setUp(self):
        self._real_stdout = sys.stdout
        self._reset_scan_jobs_state()
        store.init_db()

    def tearDown(self):
        # Wait out any still-running worker thread from a failed assertion
        # before resetting state / restoring stdout, so it can't write to a
        # stdout object we're about to discard.
        thread = scan_jobs._thread
        if thread is not None and thread.is_alive():
            if scan_jobs._cancel_event is not None:
                scan_jobs._cancel_event.set()
            thread.join(timeout=5.0)
        sys.stdout = self._real_stdout
        self._reset_scan_jobs_state()

    def _reset_scan_jobs_state(self):
        scan_jobs._job = None
        scan_jobs._thread = None
        scan_jobs._cancel_event = None
        scan_jobs._worker_ident = None
        scan_jobs._orig_stdout = None
        scan_jobs._proxy_installed = False


class StartAndComplete(ScanJobsTestCase):
    def test_start_runs_to_completion_and_persists(self):
        # The patch must stay active until the worker thread has actually
        # called discover.discover() and returned — the thread runs
        # concurrently with this `with` block, so unpatching too early (e.g.
        # right after start() returns) would let the worker call the REAL
        # discover() once the mock is torn down.
        with patch("discover.discover", side_effect=_fast_fake_discover()):
            result = scan_jobs.start("fast", {"category": "all"})
            self.assertTrue(result["started"])
            scan_id = result["job"]["scan_id"]
            self.assertIsNotNone(scan_id)

            job = _poll_until(lambda j: j["status"] != "running")
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["pair_count"], 1)
        self.assertIsNone(job["error"])
        self.assertIsNotNone(job["finished_at"])

        stored = store.get_scan(scan_id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored["status"], "completed")


class SingleFlightDedup(ScanJobsTestCase):
    def test_second_start_while_running_is_a_no_op(self):
        with patch("discover.discover", side_effect=_slow_fake_discover()):
            first = scan_jobs.start("full", {})
            self.assertTrue(first["started"])
            second = scan_jobs.start("full", {})
            self.assertFalse(second["started"])
            self.assertEqual(second["job"]["scan_id"], first["job"]["scan_id"])
            self.assertEqual(second["job"]["status"], "running")

            # Let it finish (still patched) so tearDown doesn't have to force
            # a cancel, and so the worker never falls through to the real
            # discover() after the mock is torn down.
            _poll_until(lambda j: j["status"] != "running", timeout=10.0)


class StageParsing(ScanJobsTestCase):
    def test_stage_and_stage_index_reflect_progress_lines(self):
        with patch("discover.discover", side_effect=_slow_fake_discover()):
            scan_jobs.start("fast", {})
            job = _poll_until(lambda j: j["stage_index"] >= 1)
            self.assertIn("1/4", job["stage"])
            self.assertIn("a", job["stage"])

            job = _poll_until(lambda j: j["stage_index"] >= 2)
            self.assertIn("2/4", job["stage"])
            self.assertTrue(any("tick" in line or line == "[2/4] b" for line in job["log_tail"]))
            self.assertLessEqual(len(job["log_tail"]), 20)

            _poll_until(lambda j: j["status"] != "running", timeout=10.0)


class Cancellation(ScanJobsTestCase):
    def test_cancel_marks_job_and_store_row_cancelled_keeps_previous_latest(self):
        # 1) A prior completed scan, same mode, so we can assert latest_scan
        #    still resolves to it after the second scan is cancelled.
        with patch("discover.discover", side_effect=_fast_fake_discover()):
            first = scan_jobs.start("fast", {})
            _poll_until(lambda j: j["status"] != "running")
        first_scan_id = first["job"]["scan_id"]
        self.assertEqual(store.get_scan(first_scan_id)["status"], "completed")

        # 2) Start a slow scan and cancel it mid-flight.
        with patch("discover.discover", side_effect=_slow_fake_discover(ticks=100, delay=0.02)):
            second = scan_jobs.start("fast", {})
            self.assertTrue(second["started"])
            second_scan_id = second["job"]["scan_id"]
            self.assertNotEqual(second_scan_id, first_scan_id)

            # Give the worker a moment to actually be mid-scan before cancelling.
            _poll_until(lambda j: j["stage_index"] >= 1)
            cancelled = scan_jobs.cancel()
            self.assertIsNotNone(cancelled)

            # 'cancelling' is a real, but non-terminal, status the watchdog
            # sets almost immediately (see CancellingTransientStatus below) —
            # wait past it to the actual terminal status, not just past
            # 'running'.
            job = _poll_until(lambda j: j["status"] not in ("running", "cancelling"), timeout=10.0)

        self.assertEqual(job["status"], "cancelled")
        stored = store.get_scan(second_scan_id)
        self.assertEqual(stored["status"], "cancelled")

        latest = store.latest_scan(mode="fast")
        self.assertEqual(latest["id"], first_scan_id)

    def test_cancel_with_nothing_running_returns_none(self):
        self.assertIsNone(scan_jobs.cancel())


class WorkerException(ScanJobsTestCase):
    def test_discover_exception_marks_job_failed_with_error(self):
        def boom(**kwargs):
            raise RuntimeError("kaboom")

        with patch("discover.discover", side_effect=boom):
            result = scan_jobs.start("fast", {})
            scan_id = result["job"]["scan_id"]
            job = _poll_until(lambda j: j["status"] != "running")
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"], "kaboom")

        stored = store.get_scan(scan_id)
        self.assertEqual(stored["status"], "failed")


class CancellingTransientStatus(ScanJobsTestCase):
    """The watchdog thread scan_jobs._worker starts must flip the job to the
    transient 'cancelling' status almost immediately after cancel() is
    called -- well before the scan itself actually stops -- so the UI has
    something to show right away instead of looking stuck on 'running'."""

    def test_status_becomes_cancelling_promptly_then_cancelled(self):
        # Prints spaced 0.5s apart -- much slower than the watchdog's ~0.15s
        # poll interval -- so the watchdog reliably flips the status to
        # 'cancelling' well before the next print gets a chance to actually
        # raise ScanCancelled and finish the job. (A tight print loop, as in
        # other tests here, can race straight past 'cancelling' to
        # 'cancelled' within a single poll tick -- that's fine for THOSE
        # tests, which don't assert on this transient state, but this test
        # needs the window to be wide enough to observe it.)
        with patch("discover.discover", side_effect=_slow_fake_discover(ticks=20, delay=0.5)):
            scan_jobs.start("fast", {})
            _poll_until(lambda j: j["stage_index"] >= 1)
            scan_jobs.cancel()

            job = _poll_until(lambda j: j["status"] == "cancelling", timeout=1.0)
            self.assertEqual(job["status"], "cancelling")

            job = _poll_until(lambda j: j["status"] == "cancelled", timeout=10.0)
        self.assertEqual(job["status"], "cancelled")


class CancelRequestedHelper(ScanJobsTestCase):
    def test_cancel_requested_reflects_flag_state(self):
        self.assertFalse(scan_jobs.cancel_requested())
        with patch("discover.discover", side_effect=_slow_fake_discover()):
            scan_jobs.start("fast", {})
            self.assertFalse(scan_jobs.cancel_requested())
            scan_jobs.cancel()
            self.assertTrue(scan_jobs.cancel_requested())
            _poll_until(lambda j: j["status"] not in ("running", "cancelling"), timeout=10.0)


class CancelSurvivesSwallowedExceptions(ScanJobsTestCase):
    """Regression test for the review requirement that ScanCancelled must be
    a BaseException, not an Exception subclass: discover.py has ~19 bare
    `except Exception:` blocks around its own internal calls. If a fake (or
    real) discover() happens to wrap a print() in one of those -- exactly
    the shape shown below -- a plain Exception raised from inside the stdout
    proxy's write() would be silently caught right there and the scan would
    just keep going as if cancel() had never been called. With
    ScanCancelled as a BaseException, it passes straight through."""

    def test_cancel_propagates_through_a_broad_except_exception_block(self):
        def fake_with_swallowing_prints(**kwargs):
            for i in range(200):
                try:
                    print(f"[2/4] guarded tick {i}", flush=True)
                except Exception:
                    # Mirrors discover.py's own defensive except-Exception
                    # blocks. Must NOT be able to catch ScanCancelled.
                    pass
                time.sleep(0.02)
            return [{"arb_net_profit": 0.0}]

        with patch("discover.discover", side_effect=fake_with_swallowing_prints):
            scan_jobs.start("fast", {})
            _poll_until(lambda j: j["stage_index"] >= 1)
            cancelled = scan_jobs.cancel()
            self.assertIsNotNone(cancelled)

            job = _poll_until(lambda j: j["status"] not in ("running", "cancelling"), timeout=10.0)

        self.assertEqual(job["status"], "cancelled")
        # It must have stopped well short of all 200 ticks -- proof the
        # `except Exception: pass` did NOT swallow the cancellation.
        tick_lines = [line for line in job["log_tail"] if "guarded tick" in line]
        self.assertTrue(tick_lines)
        last_tick = int(tick_lines[-1].rsplit(" ", 1)[-1])
        self.assertLess(last_tick, 199)


class MainThreadStdoutNotCaptured(ScanJobsTestCase):
    def test_main_thread_writes_pass_through_and_are_not_captured(self):
        import io
        buf = io.StringIO()
        sys.stdout = buf  # our proxy will wrap THIS as "original" on install

        with patch("discover.discover", side_effect=_slow_fake_discover(ticks=60, delay=0.02)):
            scan_jobs.start("fast", {})
            _poll_until(lambda j: j["stage_index"] >= 1)

            print("main thread marker line", flush=True)  # written via the proxy, but from the main thread

            job = scan_jobs.status()
            self.assertNotIn("main thread marker line", job["log_tail"])
            self.assertIn("main thread marker line", buf.getvalue())

            _poll_until(lambda j: j["status"] != "running", timeout=10.0)


if __name__ == "__main__":
    unittest.main()
