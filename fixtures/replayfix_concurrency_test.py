"""Run ReplayFix's three original tests plus one concurrent-delivery test.

Save beside your repaired replayfix_bug_demo.py, then run on Windows:
    py replayfix_concurrency_test.py

With Nemotron's first SELECT-then-INSERT patch: 4 tests, 1 failure.
The new test uses two real SQLite connections to one temporary database. It
coordinates their first unprotected existence checks so both can observe a
missing event before either inserts. This exposes the known race reliably.
Checks performed inside an explicit transaction are not paused this way, so
a correct repair that acquires the write lock before checking can finish.

This exercises one controlled interleaving. It is not a comprehensive stress
test. No API key, network access, or extra Python packages are needed. The
test does not edit your repaired source file or any existing database.
"""

from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest


def load_app():
    path = Path(__file__).resolve().with_name("replayfix_bug_demo.py")
    if not path.is_file():
        raise SystemExit(
            "Save this test beside your repaired replayfix_bug_demo.py.\n"
            "Both files must have exactly those names."
        )
    spec = importlib.util.spec_from_file_location("replayfix_under_test", path)
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    return app


class ScheduledCursor(sqlite3.Cursor):
    """Coordinate an unprotected read; keep the database operations real."""

    def execute(self, sql, parameters=()):
        normalized = " ".join(sql.upper().split())
        self._pause_if_missing = (
            normalized.startswith("SELECT ")
            and "ORDERS" in normalized
            and not self.connection.in_transaction
            and not self.connection.read_pause_used
        )
        return super().execute(sql, parameters)

    def fetchone(self):
        row = super().fetchone()
        if self._pause_if_missing and row is None:
            self._pause_if_missing = False
            self.connection.read_pause_used = True
            gate = self.connection.read_gate
            self.close()
            gate.wait(timeout=5)
        return row


class ScheduledConnection(sqlite3.Connection):
    read_pause_used = False

    def cursor(self, factory=None):
        return super().cursor(factory or ScheduledCursor)

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)


def concurrency_tests_for(app):
    class ConcurrentDeliveryTests(unittest.TestCase):
        def test_simultaneous_duplicate_deliveries_create_one_order(self):
            with tempfile.TemporaryDirectory(prefix="replayfix_concurrent_") as folder:
                path = Path(folder) / "orders.sqlite"
                initial = app.open_database(path)
                initial.close()

                start_gate = threading.Barrier(2)
                read_gate = threading.Barrier(2)

                def deliver():
                    connection = sqlite3.connect(
                        path, timeout=10, factory=ScheduledConnection
                    )
                    connection.read_gate = read_gate
                    try:
                        start_gate.wait(timeout=5)
                        app.handle_webhook(
                            connection,
                            {"event_id": "evt_101", "customer_id": "customer_7"},
                        )
                    finally:
                        connection.close()

                with ThreadPoolExecutor(max_workers=2) as workers:
                    deliveries = [workers.submit(deliver) for _ in range(2)]
                    for delivery in deliveries:
                        # Exceptions are test errors, never treated as success.
                        delivery.result(timeout=15)

                check = sqlite3.connect(path)
                try:
                    count = check.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
                finally:
                    check.close()
                self.assertEqual(
                    count, 1,
                    "Two overlapping deliveries of evt_101 must create exactly one order.",
                )

    return ConcurrentDeliveryTests


def main():
    app = load_app()
    loader = unittest.TestLoader()
    suite = unittest.TestSuite([
        loader.loadTestsFromTestCase(app.WebhookTests),
        loader.loadTestsFromTestCase(concurrency_tests_for(app)),
    ])
    print("Running 3 original tests and 1 concurrent-delivery test.", flush=True)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
