"""ReplayFix milestone 2: a deliberately buggy webhook handler.

Run on Windows:
    py replayfix_bug_demo.py

Expected BEFORE repair: 3 tests run, with 2 failures and 1 pass.
The failures show that receiving the same event twice creates duplicate orders,
even after reopening the database. Distinct events must still create two orders.

This is a constructed teaching example, not a reported production incident.
It uses only Python's standard library. It makes no network requests, needs no
API key, and stores each test database in an automatically removed temp folder.

Repair target: handle_webhook(). Keep the tests and their assertions unchanged.
For this exercise, a unique event_id identifies one order-creation event.
"""

from pathlib import Path
import sqlite3
import tempfile
import unittest


def open_database(path):
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE IF NOT EXISTS orders ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "event_id TEXT NOT NULL, customer_id TEXT NOT NULL)"
    )
    db.commit()
    return db


def handle_webhook(db, event):
    """Create an order for an event; repeated delivery should create nothing."""
    # BUG: every delivery creates another order, including repeated events.
    db.execute(
        "INSERT INTO orders (event_id, customer_id) VALUES (?, ?)",
        (event["event_id"], event["customer_id"]),
    )
    db.commit()


class WebhookTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="replayfix_case_")
        self.addCleanup(folder.cleanup)
        self.db_path = Path(folder.name) / "orders.sqlite"
        self.db = open_database(self.db_path)
        self.addCleanup(lambda: self.db.close())

    def order_count(self):
        return self.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]

    def test_duplicate_event_creates_one_order(self):
        event = {"event_id": "evt_101", "customer_id": "customer_7"}
        handle_webhook(self.db, event)
        handle_webhook(self.db, event)
        self.assertEqual(self.order_count(), 1,
                         "Repeated delivery must not create a second order.")

    def test_distinct_events_for_same_customer_create_two_orders(self):
        handle_webhook(self.db, {"event_id": "evt_101", "customer_id": "customer_7"})
        handle_webhook(self.db, {"event_id": "evt_102", "customer_id": "customer_7"})
        self.assertEqual(self.order_count(), 2,
                         "Two legitimate events must create two orders.")

    def test_duplicate_after_database_reopen_creates_one_order(self):
        event = {"event_id": "evt_101", "customer_id": "customer_7"}
        handle_webhook(self.db, event)
        self.db.close()
        self.db = open_database(self.db_path)
        handle_webhook(self.db, event)
        self.assertEqual(self.order_count(), 1,
                         "Duplicate protection must survive reopening the database.")


if __name__ == "__main__":
    print("ReplayFix deliberately buggy example: expect 2 failures before repair.",
          flush=True)
    unittest.main(verbosity=2)
