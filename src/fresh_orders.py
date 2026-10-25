"""Parse Amazon Fresh / Whole Foods order detail pages (the /uff/ layout amazon-orders can't handle)."""
import re

from bs4 import BeautifulSoup


def _text(node):
    return " ".join(node.get_text().split()) if node else None


def _money(s):
    m = re.search(r"\$([\d,]+\.\d{2})", s or "")
    return float(m.group(1).replace(",", "")) if m else None


def retailer(html):
    """'fresh', 'whole_foods', or None when the page is neither (e.g. a regular Amazon order)."""
    text = _text(BeautifulSoup(html, "html.parser"))
    if "Continue shopping on Amazon Fresh" in text:
        return "fresh"
    if "Continue shopping on Whole Foods" in text:
        return "whole_foods"
    return None


def parse_items(html):
    """Return line items from the full item list on a /uff/ order details page."""
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for row in soup.select('div[id$="-item-grid-row"]'):
        asin = row["id"].removesuffix("-item-grid-row")
        cells = row.select('div[role="gridcell"]')
        # cells: image, title, quantity, weight (optional), total
        title = _text(row.select_one("a.a-link-normal span"))
        qty_cell = cells[2] if len(cells) > 2 else None
        weight = row.select_one(f'[id="{asin}-item-priced-by-quantity"]')
        per_unit = re.search(r"\(\$[\d.]+/\w+\)", _text(row) or "")
        items.append({
            "asin": asin,
            "title": title,
            "quantity": int(_text(qty_cell)) if qty_cell and _text(qty_cell).isdigit() else None,
            "weight": _text(weight) or None,
            "price_per_unit": per_unit.group(0).strip("()") if per_unit else None,
            "total": _money(_text(row.select_one(f'[id="{asin}-item-total-price"]'))),
            "status": _text(row.select_one(".ufpo-item-status")),
        })
    if not items:
        raise ValueError("no item rows found; Amazon may have changed the order page")
    return items
