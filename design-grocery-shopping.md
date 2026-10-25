# Design: Fresh Restock Assistant

Part 2 (Focusing) idea for Week 2. See [IDEAS.md](IDEAS.md) for the original brainstorm.

## Problem

All my grocery shopping goes through Amazon Fresh, and I buy mostly the same staples on a regular rhythm. Every week I rebuild the cart from memory or by scrolling past orders, and I only notice something ran out when it's gone.

## Process before

1. Realize something is running low (usually too late).
2. Open Amazon Fresh and dig through past orders or search for each item.
3. Add each item to the cart by hand, guessing quantities.
4. Check out.

## Process after

1. A script collects my order history and works out, for each item, how often I buy it and how much.
2. A daily email says which items are due ("you usually buy milk every 8 days, it's day 9").
3. The due items are added to my Fresh cart.
4. I review the cart and press Buy myself.
5. A local dashboard shows the logic behind every prediction and lets me correct it.

## Decisions so far

| Question | Decision |
|---|---|
| Trigger | Per-item nudge when an item is due |
| Automation limit | Fill the cart only; I check out. No auto-purchase |
| Purchase regularity | Very regular (same staples most weeks) |
| Data source | `amazon-orders` for login plus our own parser for Fresh / Whole Foods orders (see Spike findings) |
| Cart filling | Try an Alexa skill first (I have an Echo); fall back to browser automation with Playwright |
| Notifications | Email |
| Hosting | My laptop |
| Language | Python |
| Storage | SQLite |
| UI | Local web app |
| Rare items | Shown separately as candidates; I build the tracked list over time |

## Architecture

```
Amazon order history -> scraper -> SQLite -> pattern engine -> dashboard
                                                  |
                                                  +-> daily email -> cart fill
```

### Data model

- `orders`: order number, date, synced_at. Fresh orders only.
- `skipped_orders`: order number, date, reason (`whole_foods`, `other`) so sync doesn't refetch them.
- `products`: ASIN -> item type ("Non-Fat Greek Yogurt") and label ("Chobani 32 oz"). Seeded from `src/item_types.json` (158 products typed by hand); products missing from it get a guessed type from their title and are flagged in the UI. My edits (`source = 'manual'`) are never overwritten.
- `order_items`: ASIN, title, quantity, `received_qty`, price, weight, status. `received_qty` is quantity minus cancelled, refunded and out-of-stock units; the pattern engine uses it, not `quantity`.
- `item_stats`: one row per item type: computed interval, usual quantity, confidence, purchase dates.
- `overrides`: my edits (frequency, quantity, paused, tracked). Overrides always win over computed values and must survive refreshes.

### Pattern engine

Implemented in `src/patterns.py`; run it with `.venv/bin/python src/patterns.py`. Tests: `.venv/bin/python -m unittest discover -s tests`.

- Items are item types, not products: every brand of cottage cheese counts as "Cottage Cheese", so a brand switch isn't mistaken for one item lapsing and another starting. 159 products became 98 item types.
- One purchase per item type per delivery day; quantities from `received_qty` only (cancelled, refunded and out-of-stock units don't count).
- Interval = median gap between purchases (robust to one odd gap).
- Usual quantity = most common quantity per purchase; ties take the smaller.
- Spread = median absolute deviation of the gaps divided by the interval.
- Confidence: high = 6+ purchases and spread <= 0.35; medium = 4+ purchases and spread <= 0.6; otherwise low.
- Status: `tracked` (medium or high confidence, or promoted by me), `candidate` (low confidence), `lapsed` (not bought for 3+ intervals and 45+ days, so no nudges), `paused` (my choice).
- Next due = last bought + interval. An item is due when it is tracked and today >= next due.
- `item_stats` is rebuilt from `order_items` on every run; my edits live in `overrides` and are merged on read, so they always win and survive recomputing.

First run on 12 months of data: 159 items, 30 tracked, 25 lapsed, 104 candidates.

### Dashboard

Implemented as a Flask app (`src/app.py`) serving one static page (`src/static/index.html`). Run `.venv/bin/python src/app.py` and open http://127.0.0.1:5000. API tests: `tests/test_app.py`.

- Tabs: Due now, Tracked, Candidates, Lapsed, Paused, each with a count. Rows show the item type, not the product name.
- Sorting: a "Sort by" menu with Most frequent (default; shortest interval first, ties go to the item bought more often), Last bought (most recent first), Item (A to Z) and Next due (most overdue first). Items with no date or interval sort last.
- Table: item type, last bought, interval, usual quantity, next due (with days overdue), confidence. Edited values are marked "edited".
- Expanding a row shows, in order:
  - **Why this prediction:** plain-language steps (how many purchases, the gaps between them, the typical wait and why the middle value is used, the confidence reason, last bought + interval = next due).
  - **Brands you buy:** every product of that type with a role (default = bought most, backup = second, others), times bought, last price and average price per unit. "change type" moves a product to another type.
  - **Purchase history:** the last 5 dates with the gap since the previous one; "Show all N dates" expands.
  - **Adjust:** frequency and quantity (blank = back to computed), Track, Pause or Resume, Reset my edits.
- Add an item by hand (name, interval, quantity). Hand-added items have no history, so they show as due right away; Remove deletes them. A name that already has history is rejected; edit that item instead.
- Recompute re-runs the pattern engine. Pulling new orders is still `src/sync.py` (needs the browser login), not a button.

## Spike findings (3 months, run Oct 25 2026)

- **Login:** works with username and password only; no OTP key needed (no 2FA on the account). Amazon serves a JavaScript sign-in challenge, so `amazon-orders[browser]` and `playwright install chromium` are required, with the Playwright auth forms registered in the session config (see `src/amazon_session.py`).
- **Library coverage:** 83 orders came back; 43 are regular Amazon orders with full items. The other 40 (Fresh / Whole Foods delivery, about 15 a month) came back with no items or total because they use a different page layout (`/uff/your-account/order-details`).
- **Our parser:** `src/fresh_orders.py` reads the item list from `/uff/your-account/order-details/item-details?orderID=...` with the logged-in session. All 40 orders parsed: 302 line items, 97 distinct products, each with ASIN, title, quantity, line total, weight and price per pound for weighed items.
- **Status notes:** 33 of 302 lines carry one (cancelled 15, weight adjusted 10, refunded 6, out of stock 2). Cancelled, refunded and out-of-stock units were never received, so they must not count as purchases.
- **Header counts units:** "8 items in this order" was 7 rows with one quantity-2 line.
- **Whole Foods orders** share the `/uff/` layout (or `/fopo/` for store receipts) but belong to my sister, so sync excludes them by the "Continue shopping on ..." text on the page.
- **12-month sync:** 88 Fresh orders, 694 line items (Nov 2025 to Oct 2026); 22 Whole Foods and other orders skipped.
- **Staples are clear:** cottage cheese (19 deliveries received), blueberries, grapes, chicken breasts, Roma tomatoes, onions.

## Risks

1. **Scraper login.** Credentials live only in a local `.env` that is gitignored. Amazon's sign-in challenge can change and break the browser step. No 2FA on this account today.
2. **Fresh line items.** Resolved by the spike above, but the `/uff/` HTML is undocumented and can change; the parser should fail loudly when a page has no item rows.
3. **Alexa and Fresh.** The [Alexa Shopping Actions API](https://developer.amazon.com/en-US/docs/alexa/alexa-shopping/alexa-shopping-actions-for-alexa-skills-api-reference.html) `AMAZON.AddToShoppingCart` needs ASINs and may not cover Fresh items. We now have the ASINs, but it needs a developer account and extra setup, so it is a stretch goal.
4. **Scraping terms.** Scraping my own account may violate Amazon's terms and can break when the site changes. Acceptable for a personal prototype; decided deliberately.
5. **Laptop scheduling.** The daily job only runs when the laptop is awake.
6. **History depth.** Only 3 months so far; longer history would improve intervals for slow-moving items.

## Build order

Each layer is useful on its own if the next one fails.

1. **Get the data:** done. `.venv/bin/python src/sync.py` logs in, lists orders for the last 12 months, and stores only Amazon Fresh orders in `data/grocery.db`. Whole Foods orders (my sister's) and regular Amazon orders are recorded in `skipped_orders` and ignored. Re-runs only fetch new orders and the last 14 days (cancellations and refunds land late); `--full` refetches everything.
2. **Analyze:** done. Intervals, quantities, confidence and due dates are computed into SQLite by `src/patterns.py`. Substitutes are grouped by item type.
3. **Dashboard:** done. See above.
4. **Notify:** daily email of due items.
5. **Cart (stretch):** Alexa skill test, then Playwright fallback.
