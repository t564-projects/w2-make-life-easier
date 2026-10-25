"""Sync Amazon Fresh order history into data/grocery.db. Whole Foods and regular Amazon orders are skipped.

Usage: .venv/bin/python src/sync.py [--months 12] [--full]
  --full  refetch every order in the window instead of only new and recent ones
"""
import argparse
import re
from datetime import date, datetime, timedelta

from amazonorders.orders import AmazonOrders

from amazon_session import login
from db import connect
from fresh_orders import parse_items, retailer

DETAILS_URL = "https://www.amazon.com/gp/your-account/order-details?orderID="
REFRESH_DAYS = 14  # recent orders can still be cancelled or refunded, so refetch them


def status_kind(status):
    if not status:
        return None, 0
    kind = {"Cancelled": "cancelled", "Refunded": "refunded", "Out": "out_of_stock", "Weight": "weight_adjusted"}.get(
        status.split(" ")[0])
    m = re.search(r"\((\d+)\)", status)
    return kind, int(m.group(1)) if m and kind != "weight_adjusted" else 0


def list_candidates(session, since):
    """Orders amazon-orders can't itemize (Fresh / Whole Foods); regular orders come back with items."""
    found = {}
    for year in range(since.year, date.today().year + 1):
        for o in AmazonOrders(session).get_orders(time_filter=f"year-{year}", full_details=False):
            if not o.items and o.order_placed_date >= since:
                found[o.order_number] = o.order_placed_date
    return found


def save_order(conn, number, placed, items):
    conn.execute("DELETE FROM order_items WHERE order_number = ?", (number,))
    conn.execute("INSERT OR REPLACE INTO orders VALUES (?,?,?)", (number, str(placed), datetime.now().isoformat()))
    for i in items:
        kind, lost = status_kind(i["status"])
        conn.execute(
            "INSERT INTO order_items (order_number, asin, title, quantity, received_qty, price, weight,"
            " price_per_unit, status, status_kind) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (number, i["asin"], i["title"], i["quantity"], max(i["quantity"] - lost, 0), i["total"],
             i["weight"], i["price_per_unit"], i["status"], kind))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args()

    since = date.today() - timedelta(days=30 * args.months)
    conn = connect()
    session = login()
    candidates = list_candidates(session, since)
    print(f"{len(candidates)} non-itemized orders since {since}")

    known = {r[0] for r in conn.execute("SELECT order_number FROM orders UNION SELECT order_number FROM skipped_orders")}
    recent = date.today() - timedelta(days=REFRESH_DAYS)
    todo = {n: d for n, d in candidates.items() if args.full or n not in known or d >= recent}
    print(f"fetching {len(todo)} ({len(candidates) - len(todo)} already synced)")

    added = skipped = 0
    for number, placed in sorted(todo.items(), key=lambda kv: kv[1], reverse=True):
        response = session.get(DETAILS_URL + number).response
        html = response.text
        shop = "whole_foods" if "/fopo/" in response.url else retailer(html)  # fopo = Whole Foods store receipt
        with conn:
            if shop == "fresh":
                save_order(conn, number, placed, parse_items(html))
                added += 1
            else:
                reason = shop or "other"
                conn.execute("INSERT OR REPLACE INTO skipped_orders VALUES (?,?,?)", (number, str(placed), reason))
                skipped += 1
        print(f"  {placed}  {number}  {shop or 'other (skipped)'}")

    n_orders, n_items = conn.execute("SELECT COUNT(*), (SELECT COUNT(*) FROM order_items) FROM orders").fetchone()
    print(f"\nsynced {added} Fresh orders, skipped {skipped}. Database now has {n_orders} Fresh orders, {n_items} line items.")


if __name__ == "__main__":
    main()
