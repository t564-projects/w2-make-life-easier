# Prompt log

Each entry: the prompt as I sent it, a short description of what was built, and the commit that holds it. Newest at the bottom. "No commit" means the turn was a question or a spike with nothing worth committing on its own.

Entries before the first commit (`d874d27`, the design doc) were not logged.

## 1. What is AMAZON_OTP_SECRET_KEY?

> what is this AMAZON_OTP_SECRET_KEY on the env? I added my login credentials to .env but I am not sure about this key. Is it necessary?

Explained it is the TOTP secret for Amazon two-step verification and is optional; only username and password are required. No commit.

## 2. Spike: what can we pull from Amazon orders?

> I dont think I have 2FA, the only thing is that I have an option to login with a pass key in my laptop here. But lets try a run to see what you can and cannot pull from my amazon orders and move along with the design

Ran the first order spike. Amazon blocked the plain login with a JavaScript challenge, so I installed `amazon-orders[browser]` and Chromium and registered the Playwright auth forms. Login worked without an OTP key. Of 83 orders in 3 months, 43 had items and 40 (Fresh / Whole Foods delivery) came back empty. No commit (spike scripts were later replaced).

## 3. Try option 1: parse the empty orders ourselves

> lets try the number 1 first

Fetched the Fresh order pages (`/uff/your-account/order-details`) with the logged-in session and wrote a parser. All 40 empty orders parsed: 302 line items with ASIN, title, quantity, price, weight and status notes. Committed with the next step.

## 4. Update the design doc and start saving data locally

> lets do that and start adding this history and findings into our local data for the UI later as well

Added the spike findings to the design doc, added `playwright` to requirements, and created the SQLite schema (`orders`, `order_items`, `overrides`) with `received_qty`, which excludes cancelled, refunded and out-of-stock units. Loaded the spike data into `data/grocery.db`. Committed in `3b622a5`.

## 5. Sync only Fresh orders, 12 months, sync command

> lets sync only fresh orders, the whole foods one are from my sister. And yes, get the 12 month history and sync command

Built `src/sync.py`: logs in, lists orders for the last 12 months, keeps only Amazon Fresh orders (88 orders, 694 line items) and records Whole Foods and other orders in `skipped_orders`. Re-runs fetch only new orders and the last 14 days; `--full` refetches everything. Removed the spike scripts. Commit `3b622a5`.

## 6. Commit, then the pattern engine

> lets commit and then start with the pattern engine

Committed the sync work, then built `src/patterns.py`: median-gap interval, most common quantity, confidence (high / medium / low), status (tracked / candidate / lapsed / paused) and next-due dates, with overrides merged on read. 13 unit tests. Commit `a9f0625`.

## 7. Commit and move on to the UI

> lets commit and move as is
> l will check everything and make any changes out once is done and available on the UI

The engine was already committed, so I moved on to the local dashboard: a Flask app with a single-page UI (tabs, expandable rows showing the math, edit / pause / track / reset, add items by hand) and API tests. Commit `730fa90`.

## 8. Run the app

> cant you run the app so I can check it locally for me please?

Started the dashboard at http://127.0.0.1:5000 as a background task. No commit.

## 9. Keep a prompt log

> Can we have a doc/log where we start saving the prompts I am sending and a brief description of what you built/the commit id after it?

Created this file. Commit: not committed yet; the id will be added once it is.
