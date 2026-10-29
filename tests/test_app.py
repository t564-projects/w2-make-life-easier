import sys
from datetime import date, timedelta
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import db  # noqa: E402


class AppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.tmp.name) / "test.db"
        import app
        import patterns
        env = mock.patch.dict(os.environ)  # a password in .env must not leak into these tests
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("DASHBOARD_PASSWORD", None)
        conn = db.connect()
        with conn:
            for k in range(6):  # weekly, most recent 3 days ago
                day = str(date.today() - timedelta(days=3 + 7 * k))
                conn.execute("INSERT INTO orders VALUES (?,?,?)", (f"o{k}", day, ""))
                conn.execute("INSERT INTO order_items (order_number, asin, title, quantity, received_qty)"
                             " VALUES (?,?,?,?,?)", (f"o{k}", "B0MILK", "Milk", 1, 1))
        patterns.compute_stats(conn)
        self.client = app.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def items(self):
        return {i["item_type"]: i for i in self.client.get("/api/items").get_json()["items"]}

    def test_lists_computed_items(self):
        milk = self.items()["Milk"]
        self.assertEqual((milk["interval_days"], milk["status"], milk["overridden"]), (7, "tracked", False))

    def test_edit_then_clear_override(self):
        self.assertEqual(self.client.patch("/api/items/Milk", json={"interval_days": 14, "quantity": 2}).status_code, 200)
        milk = self.items()["Milk"]
        self.assertEqual((milk["interval_days"], milk["usual_qty"], milk["overridden"]), (14, 2, True))
        self.client.patch("/api/items/Milk", json={"interval_days": None, "quantity": None})
        self.assertEqual(self.items()["Milk"]["interval_days"], 7)

    def test_items_carry_cart_info_and_cart_endpoint_works(self):
        milk = self.items()["Milk"]
        self.assertEqual(set(milk["cart"]), {"in_cart", "can_add", "auto", "quantity", "unit_price", "price", "label"})
        state = self.client.get("/api/cart").get_json()
        self.assertEqual((state["lines"], state["total"], state["busy"]), ([], 0, False))
        self.assertEqual(self.client.post("/api/cart/add", json={}).status_code, 400)

    def test_pause_and_reset(self):
        self.client.patch("/api/items/Milk", json={"paused": 1})
        self.assertEqual(self.items()["Milk"]["status"], "paused")
        self.client.delete("/api/items/Milk/overrides")
        self.assertEqual(self.items()["Milk"]["status"], "tracked")

    def test_rejects_bad_input(self):
        self.assertEqual(self.client.patch("/api/items/Milk", json={"interval_days": -1}).status_code, 400)
        self.assertEqual(self.client.patch("/api/items/Milk", json={"colour": 1}).status_code, 400)
        self.assertEqual(self.client.post("/api/items", json={"item_type": "", "interval_days": 7}).status_code, 400)

    def test_manual_item_lifecycle_and_survives_recompute(self):
        self.client.post("/api/items", json={"item_type": "Dog Food", "interval_days": 30, "quantity": 2})
        asin = "Dog Food"
        dog = self.items()[asin]
        self.assertTrue(dog["manual"] and dog["due"])
        self.assertEqual(dog["usual_qty"], 2)
        self.client.post("/api/recompute")
        self.assertIn(asin, self.items())
        self.client.delete("/api/items/Dog Food/overrides")
        self.assertNotIn(asin, self.items())

    def test_cannot_add_a_type_that_already_has_history(self):
        res = self.client.post("/api/items", json={"item_type": "milk", "interval_days": 7})
        self.assertEqual(res.status_code, 400)

    def test_moving_a_product_regroups_the_history(self):
        self.assertEqual(self.client.patch("/api/products/B0MILK", json={"item_type": "Dairy Milk"}).status_code, 200)
        items = self.items()
        self.assertIn("Dairy Milk", items)
        self.assertNotIn("Milk", items)
        self.assertEqual(self.client.patch("/api/products/NOPE", json={"item_type": "x"}).status_code, 404)

    def test_password_guards_writes_but_not_reads(self):
        os.environ["DASHBOARD_PASSWORD"] = "hunter2"
        self.assertEqual(self.client.get("/api/items").status_code, 200)
        for headers in ({}, {"X-Dashboard-Password": "nope"}):
            self.assertEqual(self.client.post("/api/cart/add", json={"item_types": ["Milk"]}, headers=headers).status_code, 401)
            self.assertEqual(self.client.patch("/api/items/Milk", json={"quantity": 3}, headers=headers).status_code, 401)
        self.assertEqual(self.items()["Milk"]["overridden"], False)
        ok = self.client.patch("/api/items/Milk", json={"quantity": 3}, headers={"X-Dashboard-Password": "hunter2"})
        self.assertEqual(ok.status_code, 200)


if __name__ == "__main__":
    unittest.main()
