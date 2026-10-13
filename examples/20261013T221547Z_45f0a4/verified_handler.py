def handle_webhook(db, event):
    db.execute("BEGIN IMMEDIATE")
    cursor = db.execute("SELECT 1 FROM orders WHERE event_id = ?", (event["event_id"],))
    if cursor.fetchone() is None:
        db.execute("INSERT INTO orders (event_id, customer_id) VALUES (?, ?)", (event["event_id"], event["customer_id"]))
    db.execute("COMMIT")
