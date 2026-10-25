"""Pattern engine: turn Fresh purchase history into per-item-type buying intervals, quantities and due dates.

An "item type" groups every product that fills the same need ("Cottage Cheese" across Daisy, Breakstone's, ...),
so switching brands doesn't look like one item stopping and another starting.

Usage: .venv/bin/python src/patterns.py   (recomputes item_stats, prints what is due and the candidates)
"""
import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from db import connect

LAPSED_AFTER = 3  # stop nudging once an item is this many intervals overdue...
LAPSED_MIN_DAYS = 45  # ...and at least this many days have passed since the last purchase

SEED_TYPES = json.loads((Path(__file__).parent / "item_types.json").read_text())  # asin -> [item_type, label]


def analyze(purchases):
    """purchases: [(date, qty)] oldest first, one per delivery day. Returns the computed stats for one item."""
    gaps = [(b[0] - a[0]).days for a, b in zip(purchases, purchases[1:])]
    qtys = [q for _, q in purchases]
    # Most common quantity; on a tie take the smaller, so we under-order rather than over-order.
    usual_qty = min(q for q, n in Counter(qtys).items() if n == max(Counter(qtys).values()))
    if not gaps:
        return {"interval_days": None, "usual_qty": usual_qty, "spread": None, "confidence": "low"}
    interval = statistics.median(gaps)
    spread = statistics.median(abs(g - interval) for g in gaps) / interval if interval else None
    n = len(purchases)
    if n >= 6 and spread is not None and spread <= 0.35:
        confidence = "high"
    elif n >= 4 and spread is not None and spread <= 0.6:
        confidence = "medium"
    else:
        confidence = "low"
    return {"interval_days": interval, "usual_qty": usual_qty, "spread": spread, "confidence": confidence}


def guess_type(title):
    """Fallback for products missing from item_types.json: the title up to the first comma, minus parentheses."""
    return re.sub(r"\s*\(.*?\)", "", title).split(",")[0].strip() or title


def sync_products(conn):
    """Make sure every purchased product has a type and label. Manual edits are never overwritten."""
    known = {r[0] for r in conn.execute("SELECT asin FROM products")}
    with conn:
        for asin, title in conn.execute("SELECT asin, MAX(title) FROM order_items GROUP BY asin").fetchall():
            if asin in known:
                continue
            if asin in SEED_TYPES:
                item_type, label, source = *SEED_TYPES[asin], "seed"
            else:
                item_type, label, source = guess_type(title), title, "auto"
            conn.execute("INSERT INTO products VALUES (?,?,?,?)", (asin, item_type, label, source))


def load_purchases(conn):
    """One entry per item type per delivery day. Cancelled, refunded and out-of-stock units don't count."""
    rows = conn.execute(
        "SELECT p.item_type, o.date, SUM(i.received_qty) FROM order_items i JOIN orders o USING (order_number)"
        " JOIN products p USING (asin) WHERE i.received_qty > 0 GROUP BY p.item_type, o.date ORDER BY o.date")
    items = defaultdict(list)
    for item_type, day, qty in rows:
        items[item_type].append((date.fromisoformat(day), qty))
    return items


def compute_stats(conn):
    sync_products(conn)
    now = datetime.now().isoformat()
    items = load_purchases(conn)
    with conn:
        conn.execute("DELETE FROM item_stats")
        for item_type, p in items.items():
            stats = analyze(p)
            conn.execute(
                "INSERT INTO item_stats VALUES (?,?,?,?,?,?,?,?,?,?)",
                (item_type, len(p), str(p[0][0]), str(p[-1][0]), stats["interval_days"], stats["usual_qty"],
                 stats["spread"], stats["confidence"], json.dumps([[str(d), q] for d, q in p]), now))
    return len(items)


def products_for(conn, item_type):
    """The products bought under this type, most bought first; the first is the default, the second the backup."""
    rows = conn.execute(
        "SELECT p.asin, p.label, p.source, COUNT(DISTINCT o.date) AS times, SUM(i.received_qty) AS units,"
        " MAX(o.date) AS last_bought, AVG(i.price * 1.0 / i.quantity) AS avg_price"
        " FROM products p JOIN order_items i USING (asin) JOIN orders o USING (order_number)"
        " WHERE p.item_type = ? AND i.received_qty > 0 GROUP BY p.asin ORDER BY times DESC, last_bought DESC",
        (item_type,)).fetchall()
    out = []
    for n, r in enumerate(rows):
        last_price = conn.execute(
            "SELECT i.price * 1.0 / i.quantity FROM order_items i JOIN orders o USING (order_number)"
            " WHERE i.asin = ? AND i.received_qty > 0 ORDER BY o.date DESC LIMIT 1", (r["asin"],)).fetchone()[0]
        out.append({"asin": r["asin"], "label": r["label"], "role": "default" if n == 0 else "backup" if n == 1 else "other",
                    "times": r["times"], "units": r["units"], "last_bought": r["last_bought"],
                    "avg_price": round(r["avg_price"], 2) if r["avg_price"] is not None else None,
                    "last_price": round(last_price, 2) if last_price is not None else None,
                    "guessed": r["source"] == "auto"})
    return out


def _d(day):
    return datetime.strptime(day, "%Y-%m-%d").strftime("%b %-d")


def rationale(purchases, interval, spread, confidence, status, next_due, today, overridden_from=None):
    """Plain-language steps explaining why an item is (or isn't) due."""
    n = len(purchases)
    dates = [date.fromisoformat(d) for d, _ in purchases]
    gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    steps = [f"You bought this {n} time{'s' if n != 1 else ''}, from {_d(purchases[0][0])} to {_d(purchases[-1][0])}."]
    if not gaps:
        steps.append("One purchase isn't enough to see a pattern yet, so there is no interval.")
        return steps
    steps.append(
        f"The gaps between purchases were {', '.join(map(str, gaps[-6:]))}{' (latest six)' if len(gaps) > 6 else ''} "
        f"days. The middle value, {statistics.median(gaps):g} days, is the typical wait; "
        "using the middle value means one unusually long break doesn't skew it.")
    if overridden_from is not None:
        steps.append(f"You set the interval to {interval:g} days yourself; the computed value would be {overridden_from:g}.")
    mad = (spread or 0) * statistics.median(gaps)
    if confidence == "high":
        steps.append(f"High confidence: {n} purchases, and the timing is steady (usually within about {mad:g} days of the typical wait).")
    elif confidence == "medium":
        steps.append(f"Medium confidence: {n} purchases and fairly steady timing (usually within about {mad:g} days of the typical wait).")
    elif n < 4:
        steps.append(f"Low confidence: only {n} purchases, too few to trust a pattern yet.")
    else:
        steps.append(f"Low confidence: the timing varies a lot (gaps from {min(gaps)} to {max(gaps)} days).")
    since = (today - dates[-1]).days
    if next_due:
        diff = (today - next_due).days
        when = "due today" if diff == 0 else f"overdue by {diff} day{'s' if diff != 1 else ''}" if diff > 0 \
            else f"due in {-diff} day{'s' if diff != -1 else ''}"
        steps.append(f"Last bought {_d(purchases[-1][0])} ({since} days ago), plus {interval:g} days means next due "
                     f"{_d(str(next_due))}: {when}.")
    if status == "lapsed":
        steps.append(f"It has been {since} days, more than {LAPSED_AFTER}x the typical wait, so I assume you stopped "
                     "buying it and won't nudge. Track it to override.")
    elif status == "candidate":
        steps.append("Not steady enough to track automatically. Track it if you want it in the nudges.")
    return steps


def item_rows(conn, today=None):
    """item_stats merged with overrides, with status and due info. Overrides always win over computed values."""
    today = today or date.today()
    out = []
    for r in conn.execute(
            "SELECT s.*, o.interval_days AS o_interval, o.quantity AS o_qty, COALESCE(o.paused, 0) AS paused,"
            " COALESCE(o.tracked, 0) AS tracked FROM item_stats s LEFT JOIN overrides o USING (item_type)"):
        interval = r["o_interval"] or r["interval_days"]
        purchases = json.loads(r["purchases"])
        last = date.fromisoformat(r["last_bought"])
        next_due = last + timedelta(days=round(interval)) if interval else None
        days_since = (today - last).days
        lapsed = bool(interval) and days_since >= LAPSED_MIN_DAYS and days_since > LAPSED_AFTER * interval
        if r["paused"]:
            status = "paused"
        elif r["tracked"]:
            status = "tracked"
        elif lapsed:
            status = "lapsed"
        elif r["confidence"] in ("high", "medium"):
            status = "tracked"
        else:
            status = "candidate"
        out.append({
            "item_type": r["item_type"], "status": status, "confidence": r["confidence"],
            "n_purchases": r["n_purchases"], "last_bought": r["last_bought"],
            "interval_days": interval, "usual_qty": r["o_qty"] or r["usual_qty"],
            "overridden": bool(r["o_interval"] or r["o_qty"]),
            "next_due": str(next_due) if next_due else None,
            "days_overdue": (today - next_due).days if next_due else None,
            "due": status == "tracked" and next_due is not None and today >= next_due,
            "rationale": rationale(purchases, interval, r["spread"], r["confidence"], status, next_due, today,
                                   r["interval_days"] if r["o_interval"] else None),
            "purchases": purchases, "products": products_for(conn, r["item_type"]), "manual": False,
        })
    # Items added by hand that have no order history yet: due right away, since I added them on purpose.
    for r in conn.execute(
            "SELECT * FROM overrides WHERE manual = 1 AND item_type NOT IN (SELECT item_type FROM item_stats)"):
        out.append({
            "item_type": r["item_type"], "status": "paused" if r["paused"] else "tracked",
            "confidence": None, "n_purchases": 0, "last_bought": None, "interval_days": r["interval_days"],
            "usual_qty": r["quantity"] or 1, "overridden": True, "next_due": None, "days_overdue": None,
            "due": not r["paused"], "rationale": ["Added by hand; no order history yet, so it shows as due."],
            "purchases": [], "products": [], "manual": True,
        })
    return out


def main():
    conn = connect()
    print(f"computed stats for {compute_stats(conn)} item types")
    rows = item_rows(conn)
    for title, wanted in (("Due now", [r for r in rows if r["due"]]),
                          ("Tracked, not due yet", [r for r in rows if r["status"] == "tracked" and not r["due"]])):
        print(f"\n{title} ({len(wanted)})")
        for r in sorted(wanted, key=lambda r: -r["days_overdue"]):
            print(f"  {r['usual_qty']}x {r['item_type'][:36]:36} every {r['interval_days']:g}d, due {r['next_due']} "
                  f"({r['days_overdue']:+d}d), {r['confidence']}")
    print(f"\nstatus counts: {dict(Counter(r['status'] for r in rows))}")


if __name__ == "__main__":
    main()
