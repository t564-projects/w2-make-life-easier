"""SQLite storage for order history and (later) the pattern engine's output."""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "grocery.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_number TEXT PRIMARY KEY,
    date         TEXT NOT NULL,
    synced_at    TEXT NOT NULL
);

-- Orders we looked at and ignore (Whole Foods, regular Amazon), so sync doesn't refetch them.
CREATE TABLE IF NOT EXISTS skipped_orders (
    order_number TEXT PRIMARY KEY,
    date         TEXT NOT NULL,
    reason       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_items (
    id             INTEGER PRIMARY KEY,
    order_number   TEXT NOT NULL REFERENCES orders(order_number),
    asin           TEXT,
    title          TEXT NOT NULL,
    quantity       INTEGER NOT NULL,
    received_qty   INTEGER NOT NULL,  -- quantity minus cancelled / refunded / out-of-stock units
    price          REAL,              -- line total as listed on the order
    weight         TEXT,
    price_per_unit TEXT,
    status         TEXT,              -- raw note from Amazon
    status_kind    TEXT,              -- cancelled | refunded | out_of_stock | weight_adjusted | NULL
    UNIQUE (order_number, asin)
);
CREATE INDEX IF NOT EXISTS idx_items_asin ON order_items(asin);

-- What kind of item each product is ("Non-Fat Greek Yogurt") and how to label it ("Chobani 32 oz").
-- Seeded from item_types.json; rows with source 'manual' are my edits and are never overwritten.
CREATE TABLE IF NOT EXISTS products (
    asin      TEXT PRIMARY KEY,
    item_type TEXT NOT NULL,
    label     TEXT NOT NULL,
    source    TEXT NOT NULL        -- seed | auto | manual
);

-- Computed by patterns.py, one row per item type; rebuilt from order_items on every run, so never edit by hand.
CREATE TABLE IF NOT EXISTS item_stats (
    item_type     TEXT PRIMARY KEY,
    n_purchases   INTEGER NOT NULL, -- delivery days on which any product of this type arrived
    first_bought  TEXT NOT NULL,
    last_bought   TEXT NOT NULL,
    interval_days REAL,             -- median gap between purchases; NULL with a single purchase
    usual_qty     INTEGER NOT NULL,
    spread        REAL,             -- median absolute deviation of gaps / interval (lower is steadier)
    confidence    TEXT NOT NULL,    -- high | medium | low
    purchases     TEXT NOT NULL,    -- JSON [[date, qty], ...] so the dashboard can show the math
    computed_at   TEXT NOT NULL
);

-- Overrides always win over computed values and must survive reloads.
CREATE TABLE IF NOT EXISTS overrides (
    item_type     TEXT PRIMARY KEY,
    manual        INTEGER NOT NULL DEFAULT 0,  -- 1 for items I add by hand that have no order history
    interval_days REAL,
    quantity      INTEGER,
    paused        INTEGER NOT NULL DEFAULT 0,
    tracked       INTEGER NOT NULL DEFAULT 0
);

-- Snapshot of my Amazon Fresh cart, replaced wholesale each time cart.py reads it.
CREATE TABLE IF NOT EXISTS cart_items (
    asin       TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    quantity   INTEGER NOT NULL,
    price      REAL,              -- per unit
    fetched_at TEXT NOT NULL
);

-- Every attempt to put an item type in the cart; keeps auto-add from re-adding something I removed.
CREATE TABLE IF NOT EXISTS cart_adds (
    id        INTEGER PRIMARY KEY,
    item_type TEXT NOT NULL,
    asin      TEXT,
    quantity  INTEGER NOT NULL,
    status    TEXT NOT NULL,      -- added | failed
    error     TEXT,
    source    TEXT NOT NULL,      -- auto | manual
    added_at  TEXT NOT NULL
);
"""


def connect():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn
