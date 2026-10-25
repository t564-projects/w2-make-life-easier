"""Pattern engine: turn Fresh purchase history into per-item buying intervals, quantities and due dates.

Usage: .venv/bin/python src/patterns.py   (recomputes item_stats, prints what is due and the candidates)
"""
import json
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from db import connect

LAPSED_AFTER = 3  # stop nudging once an item is this many intervals overdue...
LAPSED_MIN_DAYS = 45  # ...and at least this many days have passed since the last purchase


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


def load_purchases(conn):
    """One entry per delivery day per item. Cancelled, refunded and out-of-stock units don't count."""
    rows = conn.execute(
        "SELECT i.asin, i.title, o.date, SUM(i.received_qty) FROM order_items i JOIN orders o USING (order_number)"
        " WHERE i.received_qty > 0 GROUP BY i.asin, o.date ORDER BY o.date")
    items = defaultdict(lambda: {"title": None, "purchases": []})
    for asin, title, day, qty in rows:
        items[asin]["title"] = title  # rows are oldest first, so this ends up the latest title
        items[asin]["purchases"].append((date.fromisoformat(day), qty))
    return items


def compute_stats(conn):
    now = datetime.now().isoformat()
    items = load_purchases(conn)
    with conn:
        conn.execute("DELETE FROM item_stats")
        for asin, item in items.items():
            p = item["purchases"]
            stats = analyze(p)
            conn.execute(
                "INSERT INTO item_stats VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (asin, item["title"], len(p), str(p[0][0]), str(p[-1][0]), stats["interval_days"], stats["usual_qty"],
                 stats["spread"], stats["confidence"], json.dumps([[str(d), q] for d, q in p]), now))
    return len(items)


def explain(row):
    """The math behind a prediction, in a sentence."""
    p = json.loads(row["purchases"])
    dates = ", ".join(datetime.strptime(d, "%Y-%m-%d").strftime("%b %-d") for d, _ in p[-5:])
    more = f"{len(p) - 5} earlier, then " if len(p) > 5 else ""
    if row["interval_days"] is None:
        return f"Bought once ({dates}); not enough history for an interval."
    return (f"{more}{dates} -> median gap {row['interval_days']:g} days over {len(p)} purchases "
            f"-> {row['confidence']} confidence")


def item_rows(conn, today=None):
    """item_stats merged with overrides, with status and due info. Overrides always win over computed values."""
    today = today or date.today()
    out = []
    for r in conn.execute(
            "SELECT s.*, o.interval_days AS o_interval, o.quantity AS o_qty, COALESCE(o.paused, 0) AS paused,"
            " COALESCE(o.tracked, 0) AS tracked FROM item_stats s LEFT JOIN overrides o USING (asin)"):
        interval = r["o_interval"] or r["interval_days"]
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
            "asin": r["asin"], "title": r["title"], "status": status, "confidence": r["confidence"],
            "n_purchases": r["n_purchases"], "last_bought": r["last_bought"],
            "interval_days": interval, "usual_qty": r["o_qty"] or r["usual_qty"],
            "overridden": bool(r["o_interval"] or r["o_qty"]),
            "next_due": str(next_due) if next_due else None,
            "days_overdue": (today - next_due).days if next_due else None,
            "due": status == "tracked" and next_due is not None and today >= next_due,
            "explanation": explain(r),
        })
    return out


def main():
    conn = connect()
    print(f"computed stats for {compute_stats(conn)} items")
    rows = item_rows(conn)
    for title, wanted in (("Due now", [r for r in rows if r["due"]]),
                          ("Tracked, not due yet", [r for r in rows if r["status"] == "tracked" and not r["due"]])):
        print(f"\n{title} ({len(wanted)})")
        for r in sorted(wanted, key=lambda r: -r["days_overdue"]):
            print(f"  {r['usual_qty']}x {r['title'][:50]:50} every {r['interval_days']:g}d, due {r['next_due']} "
                  f"({r['days_overdue']:+d}d), {r['confidence']}")
    counts = Counter(r["status"] for r in rows)
    print(f"\nstatus counts: {dict(counts)}")


if __name__ == "__main__":
    main()
