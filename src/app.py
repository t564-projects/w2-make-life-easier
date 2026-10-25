"""Local dashboard for the Fresh restock assistant.

Usage: .venv/bin/python src/app.py   then open http://127.0.0.1:5000
"""
import re
from datetime import datetime

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


@app.patch("/api/items/<asin>")
def edit(asin):
    """Set override fields. A null value clears that field, so the computed value applies again."""
    body = request.get_json(force=True)
    unknown = set(body) - set(EDITABLE)
    if unknown:
        return jsonify(error=f"unknown fields: {sorted(unknown)}"), 400
    conn = connect()
    with conn:
        conn.execute("INSERT OR IGNORE INTO overrides (asin) VALUES (?)", (asin,))
        for field, value in body.items():
            if value is not None:
                try:
                    value = EDITABLE[field](value)
                except (TypeError, ValueError):
                    return jsonify(error=f"bad value for {field}"), 400
                if field in ("interval_days", "quantity") and value <= 0:
                    return jsonify(error=f"{field} must be positive"), 400
            conn.execute(f"UPDATE overrides SET {field} = ? WHERE asin = ?", (value, asin))
    return jsonify(ok=True)


@app.delete("/api/items/<asin>/overrides")
def reset(asin):
    """Drop my edits for an item. Hand-added items are removed entirely."""
    conn = connect()
    with conn:
        conn.execute("DELETE FROM overrides WHERE asin = ?", (asin,))
    return jsonify(ok=True)


@app.post("/api/items")
def add():
    body = request.get_json(force=True)
    title = (body.get("title") or "").strip()
    try:
        interval, qty = float(body["interval_days"]), int(body.get("quantity") or 1)
    except (KeyError, TypeError, ValueError):
        return jsonify(error="interval_days and quantity must be numbers"), 400
    if not title or interval <= 0 or qty <= 0:
        return jsonify(error="title, a positive interval and a positive quantity are required"), 400
    asin = (body.get("asin") or "").strip() or "manual-" + re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    conn = connect()
    with conn:
        conn.execute("INSERT OR REPLACE INTO overrides (asin, title, interval_days, quantity, tracked)"
                     " VALUES (?,?,?,?,1)", (asin, title, interval, qty))
    return jsonify(asin=asin)


if __name__ == "__main__":
    patterns.compute_stats(connect())
    app.run(host="127.0.0.1", port=5000, debug=False)
