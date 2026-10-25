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

-- Computed by patterns.py; rebuilt from order_items on every run, so never edit by hand.
CREATE TABLE IF NOT EXISTS item_stats (
    asin          TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    n_purchases   INTEGER NOT NULL,
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
    asin          TEXT PRIMARY KEY,
    interval_days REAL,
    quantity      INTEGER,
    paused        INTEGER NOT NULL DEFAULT 0,
    tracked       INTEGER NOT NULL DEFAULT 0
);
"""


def connect():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn
