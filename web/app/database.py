import os
import sqlite3
from flask import g, current_app

_MIGRATIONS = [
    "ALTER TABLE comparison ADD COLUMN deposit_net_saving REAL",
    "ALTER TABLE comparison ADD COLUMN deposit_months_saved INTEGER",
    "ALTER TABLE comparison ADD COLUMN deposit_new_last_date TEXT",
    "ALTER TABLE mortgage ADD COLUMN adjust_business_days INTEGER DEFAULT 0",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS mortgage (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL DEFAULT 'My Mortgage',
    loan_amount         REAL NOT NULL,
    annual_rate         REAL NOT NULL,
    first_payment_date  TEXT NOT NULL,
    last_payment_date   TEXT NOT NULL,
    monthly_payment     REAL,
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS deposit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL DEFAULT 'My Deposit',
    amount          REAL NOT NULL,
    annual_rate     REAL NOT NULL,
    term_months     INTEGER NOT NULL,
    capitalization  INTEGER DEFAULT 1,
    start_date      TEXT NOT NULL,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS comparison (
    id                            INTEGER PRIMARY KEY AUTOINCREMENT,
    mortgage_id                   INTEGER NOT NULL,
    deposit_id                    INTEGER NOT NULL,
    deposit_income                REAL,
    deposit_final                 REAL,
    deposit_net_saving            REAL,
    deposit_months_saved          INTEGER,
    deposit_new_last_date         TEXT,
    reduce_term_new_last_date     TEXT,
    reduce_term_months_saved      INTEGER,
    reduce_term_interest_saved    REAL,
    reduce_payment_new_monthly    REAL,
    reduce_payment_interest_saved REAL,
    baseline_total_interest       REAL,
    winner                        TEXT,
    created_at                    DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (mortgage_id) REFERENCES mortgage(id),
    FOREIGN KEY (deposit_id)  REFERENCES deposit(id)
);
"""


def init_db(db_path):
    """Create tables if they don't exist and apply schema migrations."""
    db_dir = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    for sql in _MIGRATIONS:
        try:
            conn.execute(sql)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.close()


def get_db():
    """Return a per-request database connection (stored in Flask's g)."""
    if 'db' not in g:
        g.db = sqlite3.connect(current_app.config['DB_PATH'])
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(e=None):
    """Close the database connection at the end of the request."""
    db = g.pop('db', None)
    if db is not None:
        db.close()
