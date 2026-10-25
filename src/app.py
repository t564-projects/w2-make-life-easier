"""Local dashboard for the Fresh restock assistant.

Usage: .venv/bin/python src/app.py   then open http://127.0.0.1:5000
"""
from flask import Flask, jsonify, request, send_from_directory

import patterns
from db import connect

app = Flask(__name__, static_folder="static", static_url_path="/static")

EDITABLE = {"interval_days": float, "quantity": int, "paused": int, "tracked": int}


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/items")
def items():
    conn = connect()
    last_sync = conn.execute("SELECT MAX(synced_at) FROM orders").fetchone()[0]
    return jsonify({"items": patterns.item_rows(conn), "last_sync": last_sync})


@app.post("/api/recompute")
def recompute():
    return jsonify({"items": patterns.compute_stats(connect())})


@app.patch("/api/items/<path:item_type>")
def edit(item_type):
    """Set override fields. A null value clears that field, so the computed value applies again."""
    body = request.get_json(force=True)
    unknown = set(body) - set(EDITABLE)
    if unknown:
        return jsonify(error=f"unknown fields: {sorted(unknown)}"), 400
    conn = connect()
    with conn:
        conn.execute("INSERT OR IGNORE INTO overrides (item_type) VALUES (?)", (item_type,))
        for field, value in body.items():
            if value is not None:
                try:
                    value = EDITABLE[field](value)
                except (TypeError, ValueError):
                    return jsonify(error=f"bad value for {field}"), 400
                if field in ("interval_days", "quantity") and value <= 0:
                    return jsonify(error=f"{field} must be positive"), 400
            conn.execute(f"UPDATE overrides SET {field} = ? WHERE item_type = ?", (value, item_type))
    return jsonify(ok=True)


@app.delete("/api/items/<path:item_type>/overrides")
def reset(item_type):
    """Drop my edits for an item. Hand-added items are removed entirely."""
    conn = connect()
    with conn:
        conn.execute("DELETE FROM overrides WHERE item_type = ?", (item_type,))
    return jsonify(ok=True)


@app.post("/api/items")
def add():
    body = request.get_json(force=True)
    item_type = (body.get("item_type") or "").strip()
    try:
        interval, qty = float(body["interval_days"]), int(body.get("quantity") or 1)
    except (KeyError, TypeError, ValueError):
        return jsonify(error="interval_days and quantity must be numbers"), 400
    if not item_type or interval <= 0 or qty <= 0:
        return jsonify(error="a name, a positive interval and a positive quantity are required"), 400
    conn = connect()
    if conn.execute("SELECT 1 FROM item_stats WHERE item_type = ? COLLATE NOCASE", (item_type,)).fetchone():
        return jsonify(error=f"'{item_type}' is already in your history; edit it there instead"), 400
    with conn:
        conn.execute("INSERT OR REPLACE INTO overrides (item_type, manual, interval_days, quantity, tracked)"
                     " VALUES (?,1,?,?,1)", (item_type, interval, qty))
    return jsonify(item_type=item_type)


@app.patch("/api/products/<asin>")
def edit_product(asin):
    """Move a product to another item type (or rename its label). The next recompute regroups the history."""
    body = request.get_json(force=True)
    item_type, label = (body.get("item_type") or "").strip(), (body.get("label") or "").strip()
    conn = connect()
    if not conn.execute("SELECT 1 FROM products WHERE asin = ?", (asin,)).fetchone():
        return jsonify(error="unknown product"), 404
    if not item_type and not label:
        return jsonify(error="give an item_type or a label"), 400
    with conn:
        if item_type:
            conn.execute("UPDATE products SET item_type = ?, source = 'manual' WHERE asin = ?", (item_type, asin))
        if label:
            conn.execute("UPDATE products SET label = ?, source = 'manual' WHERE asin = ?", (label, asin))
    patterns.compute_stats(conn)
    return jsonify(ok=True)


if __name__ == "__main__":
    patterns.compute_stats(connect())
    app.run(host="127.0.0.1", port=5000, debug=False)
