"""Local dashboard for the Fresh restock assistant.

Usage: .venv/bin/python src/app.py            then open http://127.0.0.1:5000
       .venv/bin/python src/app.py --no-auto  same, without filling the Fresh cart on its own (--port N to change the port)
"""
import argparse
import threading
import time

from flask import Flask, jsonify, request, send_from_directory

import cart
import patterns
from db import connect

app = Flask(__name__, static_folder="static", static_url_path="/static")

AUTO_EVERY = 6 * 3600  # while the dashboard is running, look for overdue high-confidence items this often

EDITABLE = {"interval_days": float, "quantity": int, "paused": int, "tracked": int}


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/items")
def items():
    conn = connect()
    last_sync = conn.execute("SELECT MAX(synced_at) FROM orders").fetchone()[0]
    return jsonify({"items": cart.annotate(conn, patterns.item_rows(conn)), "last_sync": last_sync})


def cart_state(conn):
    lines = cart.cart_lines(conn)
    failed = {r["item_type"]: r["error"] for r in conn.execute(
        "SELECT item_type, error FROM cart_adds WHERE id IN (SELECT MAX(id) FROM cart_adds GROUP BY item_type)"
        " AND status = 'failed'")}
    return {"lines": lines, "total": round(sum((l["price"] or 0) * l["quantity"] for l in lines), 2),
            "fetched_at": lines[0]["fetched_at"] if lines else None, "failed": failed, **cart.JOB}


@app.get("/api/cart")
def cart_get():
    return jsonify(cart_state(connect()))


@app.post("/api/cart/add")
def cart_add():
    """Add the named item types to the Fresh cart in the background; the page polls /api/cart for the result."""
    names = (request.get_json(force=True) or {}).get("item_types") or []
    if not names:
        return jsonify(error="item_types is required"), 400
    if not cart.start_job(f"Adding {len(names)} item{'s' if len(names) != 1 else ''} to your Fresh cart",
                          lambda conn: cart.add_items(conn, names, "manual")):
        return jsonify(error="the cart is busy; try again in a moment"), 409
    return jsonify(ok=True), 202


@app.post("/api/cart/refresh")
def cart_refresh():
    if not cart.start_job("Reading your Fresh cart", lambda conn: {"lines": len(cart.refresh(conn))}):
        return jsonify(error="the cart is busy; try again in a moment"), 409
    return jsonify(ok=True), 202


def auto_loop():
    while True:
        # Skips quietly when I'm already adding something by hand; the next round will catch up.
        cart.start_job("Adding overdue items to your Fresh cart", cart.run_auto)
        time.sleep(AUTO_EVERY)


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
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-auto", action="store_true", help="don't add overdue high-confidence items to the cart on my behalf")
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()
    patterns.compute_stats(connect())
    if not args.no_auto:
        threading.Thread(target=auto_loop, daemon=True).start()
    app.run(host="127.0.0.1", port=args.port, debug=False)
