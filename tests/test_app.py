import sys
from datetime import date, timedelta
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import db  # noqa: E402


class AppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.tmp.name) / "test.db"
        import app
        import patterns
        conn = db.connect()
        with conn:
            for k in range(6):  # weekly, most recent 3 days ago
                day = str(date.today() - timedelta(days=3 + 7 * k))
                conn.execute("INSERT INTO orders VALUES (?,?,?)", (f"o{k}", day, ""))
                conn.execute("INSERT INTO order_items (order_number, asin, title, quantity, received_qty)"
                             " VALUES (?,?,?,?,?)", (f"o{k}", "MILK", "Milk", 1, 1))
        patterns.compute_stats(conn)
        self.client = app.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def items(self):
        return {i["asin"]: i for i in self.client.get("/api/items").get_json()["items"]}

    def test_lists_computed_items(self):
        milk = self.items()["MILK"]
        self.assertEqual((milk["interval_days"], milk["status"], milk["overridden"]), (7, "tracked", False))

    def test_edit_then_clear_override(self):
        self.assertEqual(self.client.patch("/api/items/MILK", json={"interval_days": 14, "quantity": 2}).status_code, 200)
        milk = self.items()["MILK"]
        self.assertEqual((milk["interval_days"], milk["usual_qty"], milk["overridden"]), (14, 2, True))
        self.client.patch("/api/items/MILK", json={"interval_days": None, "quantity": None})
        self.assertEqual(self.items()["MILK"]["interval_days"], 7)

    def test_pause_and_reset(self):
        self.client.patch("/api/items/MILK", json={"paused": 1})
        self.assertEqual(self.items()["MILK"]["status"], "paused")
        self.client.delete("/api/items/MILK/overrides")
        self.assertEqual(self.items()["MILK"]["status"], "tracked")

    def test_rejects_bad_input(self):
        self.assertEqual(self.client.patch("/api/items/MILK", json={"interval_days": -1}).status_code, 400)
        self.assertEqual(self.client.patch("/api/items/MILK", json={"colour": 1}).status_code, 400)
        self.assertEqual(self.client.post("/api/items", json={"title": "", "interval_days": 7}).status_code, 400)

    def test_manual_item_lifecycle_and_survives_recompute(self):
        asin = self.client.post("/api/items", json={"title": "Dog Food", "interval_days": 30, "quantity": 2}).get_json()["asin"]
        self.assertEqual(asin, "manual-dog-food")
        dog = self.items()[asin]
        self.assertTrue(dog["manual"] and dog["due"])
        self.assertEqual(dog["usual_qty"], 2)
        self.client.post("/api/recompute")
        self.assertIn(asin, self.items())
        self.client.delete(f"/api/items/{asin}/overrides")
        self.assertNotIn(asin, self.items())


if __name__ == "__main__":
    unittest.main()
