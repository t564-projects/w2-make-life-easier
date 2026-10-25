"""Fill my Amazon Fresh cart with the items that are due. I still check out myself.

Items are added through Fresh's "Past Purchases" page, which only lists Fresh products, so nothing can land in the
regular Amazon cart or the Whole Foods cart (that one is my sister's). Every add is checked against the Fresh cart page.

Usage: .venv/bin/python src/cart.py            add every high-confidence overdue item that isn't in the cart yet
       .venv/bin/python src/cart.py --refresh  just read the cart
       .venv/bin/python src/cart.py --dry-run  show what would be added
"""
import argparse
import re
import threading
from datetime import date, datetime, timedelta
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

import patterns
from db import connect

FRESH_BRAND = "QW1hem9uIEZyZXNo"  # base64 of "Amazon Fresh"
CART_URL = f"https://www.amazon.com/cart/localmarket?almBrandId={FRESH_BRAND}"
PAST_URL = f"https://www.amazon.com/afx/lists/pastpurchases?almBrandId={FRESH_BRAND}"
MAX_QTY = 10  # never add more than this of one item in one go
BACKUPS = 2  # try the default product, then the backup
RETRY_FAILED_AFTER = timedelta(hours=24)  # auto mode waits this long before retrying an item that failed


class CartError(Exception):
    pass


class Blocked(CartError):
    """Amazon is asking for a verification step; stop and try again later instead of hammering it."""


def parse_cart(html):
    """Lines in the Fresh cart: [{asin, title, quantity, price}], price per unit."""
    out = []
    for row in BeautifulSoup(html, "html.parser").select("div.sc-list-item[data-asin][data-itemtype='active']"):
        price = row.get("data-price")
        out.append({"asin": row["data-asin"], "title": row.get("data-producttitle", ""),
                    "quantity": int(row.get("data-quantity") or 1), "price": float(price) if price else None})
    return out


def search_terms(label, item_type):
    """Label without its size ("20 oz", "1 lb"), plus the item type when the label doesn't say what it is."""
    terms = re.sub(r"\s+", " ", re.sub(r"\b\d[\d.]*\s*(fl\s*)?(oz|ounce|lb|pound|ct|count|pint|gallon|g|kg|ml|l)\b", "", label, flags=re.I)).strip(" ,")
    return terms if item_type.lower() in terms.lower() else f"{terms} {item_type}"


class FreshBrowser:
    """A logged-in headless browser that is inside Amazon Fresh. Use as a context manager."""

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        from amazon_session import login
        session = login()
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        ctx = self._browser.new_context(viewport={"width": 1300, "height": 1000})
        ctx.add_cookies([{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
                         for c in session.session.cookies])
        self.page = ctx.new_page()
        return self

    def __exit__(self, *exc):
        self._browser.close()
        self._pw.stop()

    def _open(self, url):
        page = self.page
        for attempt in range(3):  # Amazon sometimes drops a navigation
            try:
                page.goto(url, wait_until="domcontentloaded")
                break
            except Exception:
                if attempt == 2:
                    raise
                page.wait_for_timeout(2000)
        page.wait_for_timeout(3000)
        if any(w in page.url for w in ("/checkpoint", "/ap/cvf", "/errors/validateCaptcha")):
            raise Blocked("Amazon asked for a verification step; try again later")
        # Fresh asks once per session to switch to the last address I used with it.
        prompt = page.locator("input[data-action-type=SET_ADDRESS]")
        if prompt.count():
            prompt.first.click()
            page.wait_for_timeout(4000)

    def read_cart(self):
        self._open(CART_URL)
        try:  # an empty cart never renders rows, so a timeout here just means empty
            self.page.wait_for_selector("div.sc-list-item[data-asin]", timeout=8000)
        except Exception:
            pass
        return parse_cart(self.page.content())

    def add(self, asin, qty, query):
        """Put qty of this product in the Fresh cart. Looks on Past Purchases first, then searches Fresh."""
        if not self._add_from_past(asin, qty):
            self._add_from_search(asin, query)

    def _add_from_past(self, asin, qty):
        page = self.page
        self._open(PAST_URL)
        card = page.locator(f"#gridElement-{asin}")
        try:
            page.wait_for_selector("[id^=gridElement-]", timeout=15000)
        except Exception:
            return False
        seen = -1
        for _ in range(10):  # the list loads as it scrolls, and stops growing at about two dozen products
            if card.count():
                break
            now = page.locator("[id^=gridElement-]").count()
            if now == seen:
                break
            seen = now
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(1200)
        if not card.count():
            return False
        card.scroll_into_view_if_needed()
        first = card.locator('button[aria-label^="Add to Cart"]')
        if not first.count():
            raise CartError("no add button (out of stock?)")
        first.first.click()
        for n in range(1, qty + 1):
            page.locator(f'#gridElement-{asin} button[aria-label="Current quantity {n}"]').wait_for(timeout=15000)
            if n < qty:
                card.locator('input[aria-label^="Add "]').first.click()
        return True

    def _add_from_search(self, asin, query):
        """Fallback for products that have dropped off Past Purchases. Adds one unit; the exact ASIN must be in the results."""
        page = self.page
        self._open(f"https://www.amazon.com/s?k={quote_plus(query)}&i=amazonfresh&almBrandId={FRESH_BRAND}")
        try:
            page.wait_for_selector("div[data-component-type='s-search-result']", timeout=15000)
        except Exception:
            raise CartError("Fresh search found nothing")
        card = page.locator(f"div[data-component-type='s-search-result'][data-asin='{asin}']")
        if not card.count():
            raise CartError("not sold on Fresh right now")
        button = card.first.get_by_text("Add to cart", exact=True)
        if not button.count():
            raise CartError("no add button (out of stock?)")
        button.first.click()
        page.wait_for_timeout(3000)


def save_cart(conn, lines):
    now = datetime.now().isoformat()
    with conn:
        conn.execute("DELETE FROM cart_items")
        conn.executemany("INSERT OR REPLACE INTO cart_items VALUES (?,?,?,?,?)",
                         [(l["asin"], l["title"], l["quantity"], l["price"], now) for l in lines])


def cart_lines(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM cart_items ORDER BY title")]


def annotate(conn, rows, today=None):
    """Add a 'cart' dict to every item row: whether it is in the cart, what would be added and roughly what it costs."""
    in_cart = {r["asin"] for r in conn.execute("SELECT asin FROM cart_items")}
    today = (today or date.today()).isoformat()
    added = {r["item_type"] for r in conn.execute("SELECT item_type FROM cart_adds WHERE status = 'added'")}
    for r in rows:
        products = r["products"][:BACKUPS]
        qty = min(r["usual_qty"], MAX_QTY)
        unit = products[0]["last_price"] if products else None
        r["cart"] = {
            "in_cart": any(p["asin"] in in_cart for p in r["products"]),
            "can_add": bool(products) and r["due"],
            "auto": bool(products) and r["due"] and r["confidence"] == "high",
            "quantity": qty, "unit_price": unit, "price": round(unit * qty, 2) if unit is not None else None,
            "label": products[0]["label"] if products else None,
        }
    return rows


def auto_candidates(conn, rows):
    """Overdue, high confidence, not in the cart, and not already handled since the last purchase."""
    out = []
    for r in annotate(conn, rows):
        c = r["cart"]
        if not c["auto"] or c["in_cart"]:
            continue
        latest = conn.execute(
            "SELECT status, added_at FROM cart_adds WHERE item_type = ? ORDER BY added_at DESC LIMIT 1",
            (r["item_type"],)).fetchone()
        if latest:
            # Added once since the last purchase (even if I then removed it): leave it alone.
            if latest["status"] == "added" and latest["added_at"][:10] >= (r["last_bought"] or ""):
                continue
            if latest["status"] == "failed" and datetime.fromisoformat(latest["added_at"]) > datetime.now() - RETRY_FAILED_AFTER:
                continue
        out.append(r)
    return out


def add_items(conn, item_types, source, browser_factory=None):
    """Add these item types to the Fresh cart. Returns {added, failed, skipped}, each a list of item type names."""
    rows = {r["item_type"]: r for r in annotate(conn, patterns.item_rows(conn))}
    result = {"added": [], "failed": [], "skipped": []}
    wanted = [rows[t] for t in item_types if t in rows and rows[t]["products"]]
    if not wanted:
        return result
    with (browser_factory or FreshBrowser)() as browser:
        save_cart(conn, browser.read_cart())
        in_cart = {l["asin"] for l in cart_lines(conn)}
        for r in wanted:
            qty = r["cart"]["quantity"]
            if any(p["asin"] in in_cart for p in r["products"]):
                result["skipped"].append(r["item_type"])
                continue
            error, done = None, None
            for p in r["products"][:BACKUPS]:
                try:
                    browser.add(p["asin"], qty, search_terms(p["label"], r["item_type"]))
                    done = p["asin"]
                    break
                except Blocked as e:
                    result["blocked"] = str(e)
                    break
                except Exception as e:  # a timeout on one product shouldn't stop the others
                    error = str(e).splitlines()[0] if str(e) else type(e).__name__
            if "blocked" in result:
                break
            with conn:
                conn.execute("INSERT INTO cart_adds (item_type, asin, quantity, status, error, source, added_at)"
                             " VALUES (?,?,?,?,?,?,?)",
                             (r["item_type"], done, qty, "added" if done else "failed", None if done else error,
                              source, datetime.now().isoformat()))
            result["added" if done else "failed"].append(r["item_type"])
        try:
            lines = browser.read_cart()
        except Blocked as e:
            result["blocked"] = str(e)
            return result
    save_cart(conn, lines)
    # Trust the cart over the click: an add that doesn't show up there is a failure.
    present = {l["asin"] for l in lines}
    for t in list(result["added"]):
        asin = conn.execute("SELECT asin FROM cart_adds WHERE item_type = ? AND status = 'added'"
                            " ORDER BY id DESC LIMIT 1", (t,)).fetchone()[0]
        if asin not in present:
            with conn:
                conn.execute("UPDATE cart_adds SET status = 'failed', error = ? WHERE item_type = ? AND asin = ?"
                             " AND id = (SELECT MAX(id) FROM cart_adds WHERE item_type = ?)",
                             ("clicked add, but it isn't in the Fresh cart", t, asin, t))
            result["added"].remove(t)
            result["failed"].append(t)
    return result


def refresh(conn, browser_factory=None):
    with (browser_factory or FreshBrowser)() as browser:
        save_cart(conn, browser.read_cart())
    return cart_lines(conn)


def run_auto(conn, browser_factory=None):
    wanted = [r["item_type"] for r in auto_candidates(conn, patterns.item_rows(conn))]
    if not wanted:
        return {"added": [], "failed": [], "skipped": []}
    return add_items(conn, wanted, "auto", browser_factory)


# One browser job at a time; the dashboard polls JOB to show progress.
JOB = {"busy": False, "message": "", "result": None}
_lock = threading.Lock()


def start_job(label, fn):
    """Run fn(conn) in the background. Returns False when another job is still running."""
    if not _lock.acquire(blocking=False):
        return False
    JOB.update(busy=True, message=label, result=None)

    def work():
        try:
            conn = connect()
            JOB["result"] = fn(conn) or {}
        except BaseException as e:  # SystemExit from a missing .env too
            JOB["result"] = {"error": str(e) or type(e).__name__}
        finally:
            JOB.update(busy=False, message="")
            _lock.release()

    threading.Thread(target=work, daemon=True).start()
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="only read the cart")
    ap.add_argument("--dry-run", action="store_true", help="list what would be added")
    args = ap.parse_args()
    conn = connect()
    patterns.compute_stats(conn)
    if args.refresh:
        lines = refresh(conn)
    elif args.dry_run:
        for r in auto_candidates(conn, patterns.item_rows(conn)):
            c = r["cart"]
            print(f"would add {c['quantity']}x {r['item_type']} ({c['label']}) ~${c['price']}")
        return
    else:
        result = run_auto(conn)
        print({k: v for k, v in result.items() if v})
        lines = cart_lines(conn)
    total = sum((l["price"] or 0) * l["quantity"] for l in lines)
    print(f"\nFresh cart: {len(lines)} items, ${total:.2f}")
    for l in lines:
        print(f"  {l['quantity']}x {l['title'][:60]}  ${(l['price'] or 0) * l['quantity']:.2f}")


if __name__ == "__main__":
    main()
