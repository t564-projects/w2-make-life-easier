import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import db  # noqa: E402


class FakeBrowser:
    """Stands in for FreshBrowser. `broken` ASINs raise; `ghost` ASINs claim success but never reach the cart."""
    cart = []
    broken, ghost, blocked = set(), set(), False
    attempts = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def read_cart(self):
        return list(FakeBrowser.cart)

    def add(self, asin, qty, query):
        import cart
        FakeBrowser.attempts.append((asin, qty))
        if FakeBrowser.blocked:
            raise cart.Blocked("verification")
        if asin in FakeBrowser.broken:
            raise cart.CartError("out of stock")
        if asin not in FakeBrowser.ghost:
            FakeBrowser.cart.append({"asin": asin, "title": query, "quantity": qty, "price": 3.0})


class CartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.tmp.name) / "test.db"
        import cart
        import patterns
        self.cart, self.patterns = cart, patterns
        self.conn = db.connect()
        FakeBrowser.cart, FakeBrowser.broken, FakeBrowser.ghost = [], set(), set()
        FakeBrowser.blocked, FakeBrowser.attempts = False, []
        # Eggs: weekly for six weeks, last bought 10 days ago -> overdue, high confidence. Two products, A is the default.
        self.buy("Eggs", [("A1", 5)] * 5 + [("B1", 1)], first_gap=10, weekly=True)
        # Rice: only three purchases, so low confidence; I track it by hand so it shows as due.
        self.buy("Rice", [("R1", 1)] * 3, first_gap=40, weekly=False)
        with self.conn:
            self.conn.execute("INSERT INTO overrides (item_type, tracked) VALUES ('Rice', 1)")

    def tearDown(self):
        self.tmp.cleanup()

    def buy(self, name, asins, first_gap, weekly):
        with self.conn:
            for k, (asin, _) in enumerate(asins):
                gap = first_gap + (7 * k if weekly else 20 * k)
                day = str(date.today() - timedelta(days=gap))
                order = f"{name}{k}"
                self.conn.execute("INSERT INTO orders VALUES (?,?,?)", (order, day, ""))
                self.conn.execute("INSERT INTO order_items (order_number, asin, title, quantity, received_qty, price)"
                                  " VALUES (?,?,?,?,?,?)", (order, asin, f"{name} {asin}", 2, 2, 7.0))
            self.conn.execute("INSERT OR IGNORE INTO products VALUES (?,?,?,?)", (asins[0][0], name, f"{name} label", "manual"))
            if asins[-1][0] != asins[0][0]:
                self.conn.execute("INSERT OR IGNORE INTO products VALUES (?,?,?,?)", (asins[-1][0], name, f"{name} backup", "manual"))
        self.patterns.compute_stats(self.conn)

    def rows(self):
        return {r["item_type"]: r for r in self.cart.annotate(self.conn, self.patterns.item_rows(self.conn))}

    def test_only_high_confidence_overdue_is_automatic(self):
        rows = self.rows()
        self.assertTrue(rows["Eggs"]["due"] and rows["Eggs"]["confidence"] == "high")
        self.assertTrue(rows["Eggs"]["cart"]["auto"])
        self.assertTrue(rows["Rice"]["due"] and not rows["Rice"]["cart"]["auto"])
        self.assertEqual([r["item_type"] for r in self.cart.auto_candidates(self.conn, self.patterns.item_rows(self.conn))], ["Eggs"])

    def test_price_is_unit_price_times_quantity(self):
        c = self.rows()["Eggs"]["cart"]
        self.assertEqual((c["quantity"], c["unit_price"], c["price"]), (2, 3.5, 7.0))

    def test_auto_adds_once(self):
        result = self.cart.run_auto(self.conn, FakeBrowser)
        self.assertEqual(result["added"], ["Eggs"])
        self.assertEqual(FakeBrowser.attempts, [("A1", 2)])
        self.assertTrue(self.rows()["Eggs"]["cart"]["in_cart"])
        self.assertEqual(self.cart.run_auto(self.conn, FakeBrowser), {"added": [], "failed": [], "skipped": []})

    def test_removed_from_cart_is_not_added_again(self):
        self.cart.run_auto(self.conn, FakeBrowser)
        FakeBrowser.cart = []  # I deleted it on Amazon
        self.cart.refresh(self.conn, FakeBrowser)
        self.assertFalse(self.rows()["Eggs"]["cart"]["in_cart"])
        self.assertEqual(self.cart.auto_candidates(self.conn, self.patterns.item_rows(self.conn)), [])
        # ...but the button still works.
        self.assertEqual(self.cart.add_items(self.conn, ["Eggs"], "manual", FakeBrowser)["added"], ["Eggs"])

    def test_buying_again_makes_it_eligible_again(self):
        self.cart.run_auto(self.conn, FakeBrowser)
        with self.conn:  # pretend the add happened before my last purchase
            self.conn.execute("UPDATE cart_adds SET added_at = ?", (str(date.today() - timedelta(days=30)),))
        FakeBrowser.cart = []
        self.cart.refresh(self.conn, FakeBrowser)
        self.assertEqual(len(self.cart.auto_candidates(self.conn, self.patterns.item_rows(self.conn))), 1)

    def test_backup_product_used_when_default_fails(self):
        FakeBrowser.broken = {"A1"}
        result = self.cart.add_items(self.conn, ["Eggs"], "manual", FakeBrowser)
        self.assertEqual(result["added"], ["Eggs"])
        self.assertEqual([a for a, _ in FakeBrowser.attempts], ["A1", "B1"])

    def test_failure_is_recorded_and_not_retried_straight_away(self):
        FakeBrowser.broken = {"A1", "B1"}
        self.assertEqual(self.cart.run_auto(self.conn, FakeBrowser)["failed"], ["Eggs"])
        self.assertEqual(self.conn.execute("SELECT status, error FROM cart_adds").fetchone()[:], ("failed", "out of stock"))
        self.assertEqual(self.cart.auto_candidates(self.conn, self.patterns.item_rows(self.conn)), [])

    def test_an_add_that_never_reaches_the_cart_is_a_failure(self):
        FakeBrowser.ghost = {"A1"}
        result = self.cart.add_items(self.conn, ["Eggs"], "manual", FakeBrowser)
        self.assertEqual((result["added"], result["failed"]), ([], ["Eggs"]))
        self.assertEqual(self.conn.execute("SELECT status FROM cart_adds").fetchone()[0], "failed")

    def test_already_in_cart_is_skipped(self):
        FakeBrowser.cart = [{"asin": "B1", "title": "Eggs", "quantity": 1, "price": 3.0}]  # the backup counts too
        result = self.cart.add_items(self.conn, ["Eggs"], "manual", FakeBrowser)
        self.assertEqual((result["skipped"], FakeBrowser.attempts), (["Eggs"], []))

    def test_verification_screen_stops_the_run(self):
        FakeBrowser.blocked = True
        result = self.cart.add_items(self.conn, ["Eggs", "Rice"], "manual", FakeBrowser)
        self.assertIn("blocked", result)
        self.assertEqual(len(FakeBrowser.attempts), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM cart_adds").fetchone()[0], 0)

    def test_parse_cart_and_search_terms(self):
        html = ('<div class="sc-list-item" data-asin="X1" data-itemtype="active" data-quantity="2" data-price="1.5"'
                ' data-producttitle="Milk"></div><div class="sc-list-item" data-asin="X2" data-itemtype="saved"></div>')
        self.assertEqual(self.cart.parse_cart(html), [{"asin": "X1", "title": "Milk", "quantity": 2, "price": 1.5}])
        self.assertEqual(self.cart.search_terms("Fresh 1 lb", "Strawberries"), "Fresh Strawberries")
        self.assertEqual(self.cart.search_terms("Sara Lee 100% Whole Wheat 20 oz", "Sandwich Bread"),
                         "Sara Lee 100% Whole Wheat Sandwich Bread")


if __name__ == "__main__":
    unittest.main()
