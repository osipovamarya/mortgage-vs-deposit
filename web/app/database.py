import os
import sqlite3
from flask import g, current_app

SCHEMA = """
CREATE TABLE IF NOT EXISTS mortgage (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT NOT NULL DEFAULT 'Моя ипотека',
    loan_amount          REAL NOT NULL,
    annual_rate          REAL NOT NULL,
    first_payment_date   TEXT NOT NULL,
    last_payment_date    TEXT NOT NULL,
    monthly_payment      REAL,
    adjust_business_days INTEGER DEFAULT 0,
    created_at           DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS repayment_strategy (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    mortgage_id         INTEGER NOT NULL REFERENCES mortgage(id),
    lump_sum            REAL,     -- единоразовая сумма досрочного погашения
    lump_sum_date       TEXT,     -- дата разового погашения (ISO)
    monthly_budget      REAL,     -- ежемесячный бюджет (для снежного кома)
    monthly_start_date  TEXT,     -- дата начала ежемесячных погашений (ISO)
    monthly_extra_day   INTEGER,  -- день месяца для досрочного платежа (напр. 15 = день зарплаты)
    repayment_mode      TEXT NOT NULL DEFAULT 'reduce_payment',  -- 'reduce_payment' или 'reduce_term'
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS deposit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL DEFAULT 'Мой вклад',
    annual_rate     REAL NOT NULL,
    term_months     INTEGER NOT NULL,
    capitalization  INTEGER DEFAULT 1,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS comparison (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    repayment_strategy_id           INTEGER NOT NULL REFERENCES repayment_strategy(id),
    deposit_id                      INTEGER NOT NULL REFERENCES deposit(id),

    -- Strategy A: deposit lump sum for T months, then repay → reduce payment
    deposit_income                  REAL,
    deposit_final                   REAL,
    deposit_net_saving              REAL,
    deposit_new_monthly             REAL,

    -- Strategy B: lump sum immediate early repayment → reduce payment
    reduce_payment_new_monthly      REAL,
    reduce_payment_interest_saved   REAL,

    -- Strategy C: snowball (monthly_budget - required_payment goes to early repayment each month)
    snowball_total_interest         REAL,
    snowball_interest_saved         REAL,
    snowball_months_to_payoff       INTEGER,
    snowball_deposit_income         REAL,
    snowball_deposit_final          REAL,

    baseline_total_interest         REAL,
    winner                          TEXT,
    created_at                      DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


def _schema_is_current(conn):
    """Return True if the DB has the current schema."""
    try:
        rs_cols = [row[1] for row in conn.execute('PRAGMA table_info(repayment_strategy)')]
        dep_cols = [row[1] for row in conn.execute('PRAGMA table_info(deposit)')]
        return ('lump_sum_date' in rs_cols and 'repayment_mode' in rs_cols
                and 'monthly_extra_day' in rs_cols and 'amount' not in dep_cols)
    except sqlite3.OperationalError:
        return False


def init_db(db_path):
    """Create or reset tables to match the current schema."""
    db_dir = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(db_path)

    if not _schema_is_current(conn):
        # Schema is outdated — drop all tables and recreate.
        conn.executescript("""
            DROP TABLE IF EXISTS comparison;
            DROP TABLE IF EXISTS repayment_strategy;
            DROP TABLE IF EXISTS deposit;
            DROP TABLE IF EXISTS mortgage;
        """)

    conn.executescript(SCHEMA)
    conn.commit()
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
