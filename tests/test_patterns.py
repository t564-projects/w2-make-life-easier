import sqlite3
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import db  # noqa: E402
import patterns  # noqa: E402


def buys(start, gaps, qty=1):
    days = [start]
    for g in gaps:
        days.append(days[-1] + timedelta(days=g))
    return [(d, qty) for d in days]


class AnalyzeTest(unittest.TestCase):
    def test_steady_item_is_high_confidence(self):
        s = patterns.analyze(buys(date(2026, 1, 1), [8, 8, 9, 7, 8, 8]))
        self.assertEqual(s["interval_days"], 8)
        self.assertEqual(s["confidence"], "high")

    def test_one_odd_gap_does_not_move_the_median(self):
        s = patterns.analyze(buys(date(2026, 1, 1), [7, 7, 60, 7, 7, 7]))
        self.assertEqual(s["interval_days"], 7)

    def test_erratic_item_is_low_confidence(self):
        s = patterns.analyze(buys(date(2026, 1, 1), [3, 40, 5, 60, 9]))
        self.assertEqual(s["confidence"], "low")

    def test_few_purchases_are_low_confidence(self):
        self.assertEqual(patterns.analyze(buys(date(2026, 1, 1), [7, 7]))["confidence"], "low")

    def test_single_purchase_has_no_interval(self):
        s = patterns.analyze([(date(2026, 1, 1), 2)])
        self.assertIsNone(s["interval_days"])
        self.assertEqual(s["usual_qty"], 2)

    def test_usual_quantity_is_the_mode_and_ties_take_the_smaller(self):
        p = [(date(2026, 1, 1), 2), (date(2026, 1, 8), 2), (date(2026, 1, 15), 1), (date(2026, 1, 22), 3)]
        self.assertEqual(patterns.analyze(p)["usual_qty"], 2)
        p = [(date(2026, 1, 1), 1), (date(2026, 1, 8), 2)]
        self.assertEqual(patterns.analyze(p)["usual_qty"], 1)


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        self.n = 0

    def add(self, asin, day, qty=1, received=None, title=None, price=None):
        """asin doubles as the item type unless title says otherwise (unmapped products are typed by their title)."""
        self.n += 1
        self.conn.execute("INSERT INTO orders VALUES (?,?,?)", (f"o{self.n}", str(day), ""))
        self.conn.execute(
            "INSERT INTO order_items (order_number, asin, title, quantity, received_qty, price) VALUES (?,?,?,?,?,?)",
            (f"o{self.n}", asin, title or asin, qty, qty if received is None else received, price))

    def weekly(self, asin, start, count):
        for k in range(count):
            self.add(asin, start + timedelta(days=7 * k))

    def rows(self, today):
        patterns.compute_stats(self.conn)
        return {r["item_type"]: r for r in patterns.item_rows(self.conn, today)}

    def test_item_becomes_due_on_its_due_date(self):
        self.weekly("milk", date(2026, 1, 1), 8)  # last bought Feb 19, due Feb 26
        self.assertFalse(self.rows(date(2026, 2, 25))["milk"]["due"])
        r = self.rows(date(2026, 2, 27))["milk"]
        self.assertTrue(r["due"])
        self.assertEqual(r["days_overdue"], 1)

    def test_cancelled_units_do_not_count_as_purchases(self):
        self.weekly("milk", date(2026, 1, 1), 6)
        self.add("milk", date(2026, 2, 19), received=0)
        patterns.compute_stats(self.conn)
        self.assertEqual(self.conn.execute("SELECT n_purchases FROM item_stats").fetchone()[0], 6)

    def test_same_day_orders_are_one_purchase(self):
        self.add("milk", date(2026, 1, 1))
        self.add("milk", date(2026, 1, 1))
        patterns.compute_stats(self.conn)
        row = self.conn.execute("SELECT n_purchases, usual_qty FROM item_stats").fetchone()
        self.assertEqual((row[0], row[1]), (1, 2))

    def test_item_not_bought_for_a_long_time_is_lapsed_not_due(self):
        self.weekly("strawberries", date(2026, 1, 1), 8)
        r = self.rows(date(2026, 8, 1))["strawberries"]
        self.assertEqual(r["status"], "lapsed")
        self.assertFalse(r["due"])

    def test_overrides_win_and_survive_recompute(self):
        self.weekly("milk", date(2026, 1, 1), 8)
        self.conn.execute("INSERT INTO overrides (item_type, interval_days, quantity) VALUES ('milk', 14, 2)")
        r = self.rows(date(2026, 2, 27))["milk"]
        self.assertEqual((r["interval_days"], r["usual_qty"], r["due"]), (14, 2, False))
        self.assertEqual(self.rows(date(2026, 3, 5))["milk"]["due"], True)

    def test_paused_item_is_never_due(self):
        self.weekly("milk", date(2026, 1, 1), 8)
        self.conn.execute("INSERT INTO overrides (item_type, paused) VALUES ('milk', 1)")
        r = self.rows(date(2026, 3, 1))["milk"]
        self.assertEqual((r["status"], r["due"]), ("paused", False))

    def test_low_confidence_item_is_a_candidate_until_promoted(self):
        for d in (date(2026, 1, 1), date(2026, 1, 20)):
            self.add("saffron", d)
        self.assertEqual(self.rows(date(2026, 2, 1))["saffron"]["status"], "candidate")
        self.conn.execute("INSERT INTO overrides (item_type, tracked) VALUES ('saffron', 1)")
        self.assertEqual(self.rows(date(2026, 2, 1))["saffron"]["status"], "tracked")

    def test_brands_of_one_type_are_merged_into_one_item(self):
        # Alternating brands: each alone is bought every 14 days, together every 7.
        for k in range(8):
            self.add("A1" if k % 2 else "B1", date(2026, 1, 1) + timedelta(days=7 * k), title="Daisy, Cottage Cheese" if k % 2 else "Breakstone's, Cottage Cheese")
        self.conn.execute("INSERT INTO products VALUES ('A1','Cottage Cheese','Daisy 24 oz','manual')")
        self.conn.execute("INSERT INTO products VALUES ('B1','Cottage Cheese','Breakstone 24 oz','manual')")
        r = self.rows(date(2026, 2, 20))["Cottage Cheese"]
        self.assertEqual((r["interval_days"], r["n_purchases"]), (7, 8))

    def test_default_brand_is_the_one_bought_most_with_prices(self):
        self.conn.execute("INSERT INTO products VALUES ('D','Yogurt','Chobani 32 oz','manual')")
        self.conn.execute("INSERT INTO products VALUES ('S','Yogurt','Stonyfield 32 oz','manual')")
        for k, (asin, price) in enumerate([("D", 5.0), ("S", 6.0), ("D", 5.5), ("D", 6.0)]):
            self.add(asin, date(2026, 1, 1) + timedelta(days=7 * k), price=price)
        products = self.rows(date(2026, 2, 1))["Yogurt"]["products"]
        self.assertEqual([(p["label"], p["role"], p["times"]) for p in products],
                         [("Chobani 32 oz", "default", 3), ("Stonyfield 32 oz", "backup", 1)])
        self.assertEqual((products[0]["last_price"], products[0]["avg_price"]), (6.0, 5.5))

    def test_unmapped_product_is_typed_by_its_title_and_flagged_as_a_guess(self):
        self.add("X1", date(2026, 1, 1), title="Mystery Brand, Pickles (Packaging May Vary), 16 oz")
        r = self.rows(date(2026, 1, 5))["Mystery Brand"]
        self.assertTrue(r["products"][0]["guessed"])

    def test_manual_type_assignment_survives_recompute(self):
        self.add("X1", date(2026, 1, 1), title="Mystery Brand, Pickles")
        self.conn.execute("INSERT INTO products VALUES ('X1','Pickles','Mystery 16 oz','manual')")
        self.assertIn("Pickles", self.rows(date(2026, 1, 5)))
        self.assertIn("Pickles", self.rows(date(2026, 1, 6)))

    def test_rationale_explains_the_numbers(self):
        self.weekly("milk", date(2026, 1, 1), 8)  # last bought Feb 19
        steps = self.rows(date(2026, 2, 28))["milk"]["rationale"]
        text = " ".join(steps)
        self.assertIn("8 times", text)
        self.assertIn("typical wait", text)
        self.assertIn("High confidence", text)
        self.assertIn("overdue by 2 days", text)


if __name__ == "__main__":
    unittest.main()
