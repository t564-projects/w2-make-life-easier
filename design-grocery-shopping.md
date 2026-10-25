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
| Data source | Scraper that logs into my Amazon account (Fresh orders show item names) |
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

- `orders`: date, item name, ASIN, quantity, price.
- `item_stats`: computed interval, usual quantity, confidence, next due date.
- `overrides`: my edits (frequency, quantity, paused, manually added items). Overrides always win over computed values and must survive refreshes.

### Pattern engine

- Interval = median gap between purchases (robust to one odd gap).
- Usual quantity = most common quantity per purchase.
- Confidence depends on the number of purchases and how consistent the gaps are.
- Items with low confidence go to a separate candidates list I can promote to tracked.

### Dashboard

- Table: item, last bought, interval, usual quantity, next due, status (tracked, candidate, paused).
- Expanding a row shows the purchase dates and the math, e.g. "Mar 1, 9, 17 -> every 8 days -> high confidence".
- Controls: edit frequency, edit quantity, pause or remove, add an item manually.

## Risks

1. **Scraper login.** Amazon may require 2FA; we may need a saved session or a one-time code step. Credentials live only in a local `.env` that is gitignored.
2. **Fresh line items.** Untested whether the scraper (e.g. [amazon-orders](https://github.com/alexdlaird/amazon-orders)) sees Fresh orders. Spike this first.
3. **Alexa and Fresh.** The [Alexa Shopping Actions API](https://developer.amazon.com/en-US/docs/alexa/alexa-shopping/alexa-shopping-actions-for-alexa-skills-api-reference.html) `AMAZON.AddToShoppingCart` needs ASINs and may not cover Fresh items. Needs a developer account and extra setup, so it is a stretch goal.
4. **Scraping terms.** Scraping my own account may violate Amazon's terms and can break when the site changes. Acceptable for a personal prototype; decided deliberately.
5. **Laptop scheduling.** The daily job only runs when the laptop is awake.

## Build order

Each layer is useful on its own if the next one fails.

1. **Get the data:** spike the scraper and confirm Fresh line items come through.
2. **Analyze:** compute intervals and quantities into SQLite.
3. **Dashboard:** view the logic and edit overrides.
4. **Notify:** daily email of due items.
5. **Cart (stretch):** Alexa skill test, then Playwright fallback.
