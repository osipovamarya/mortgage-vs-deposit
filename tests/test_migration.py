"""
Аддитивная миграция И2 (W5): `repayment_strategy.early_repayment_allocation`.

Проверяем, что `init_db()` на СТАРОЙ базе (без новой колонки) добавляет колонку
через `ALTER TABLE`, а не пересоздаёт таблицы: сохранённые строки
`repayment_strategy` и `comparison` обязаны уцелеть вместе со своими значениями.

Реальная `db/mortgage_web.db` не трогается: старая база собирается заново
во временном каталоге, а если файл пользовательской базы существует — с ним
работаем на копии.

Запуск: PYTHONPATH=web python -m unittest discover -s tests
"""
import os
import shutil
import sqlite3
import tempfile
import unittest

from app.database import init_db

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USER_DB = os.path.join(REPO_ROOT, 'db', 'mortgage_web.db')

# Схема ДО И2 — ровно та, что лежала в database.py до появления колонки
# early_repayment_allocation. Держим её здесь литералом: тест обязан проверять
# миграцию со старого состояния, а не с текущего SCHEMA.
OLD_SCHEMA = """
CREATE TABLE mortgage (
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

CREATE TABLE repayment_strategy (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    mortgage_id         INTEGER NOT NULL REFERENCES mortgage(id),
    lump_sum            REAL,
    lump_sum_date       TEXT,
    monthly_budget      REAL,
    monthly_start_date  TEXT,
    monthly_extra_day   INTEGER,
    repayment_mode      TEXT NOT NULL DEFAULT 'reduce_payment',
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE deposit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL DEFAULT 'Мой вклад',
    annual_rate     REAL NOT NULL,
    term_months     INTEGER NOT NULL,
    capitalization  INTEGER DEFAULT 1,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE comparison (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    repayment_strategy_id           INTEGER NOT NULL REFERENCES repayment_strategy(id),
    deposit_id                      INTEGER NOT NULL REFERENCES deposit(id),
    deposit_income                  REAL,
    deposit_final                   REAL,
    deposit_net_saving              REAL,
    deposit_new_monthly             REAL,
    reduce_payment_new_monthly      REAL,
    reduce_payment_interest_saved   REAL,
    reduce_term_interest_saved      REAL,
    reduce_term_months_saved        INTEGER,
    reduce_term_months_to_payoff    INTEGER,
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


def columns(conn, table):
    return [row[1] for row in conn.execute(f'PRAGMA table_info({table})')]


def build_old_db(path):
    """Собрать базу в состоянии «до И2» и положить в неё по одной строке."""
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        """INSERT INTO mortgage (id, name, loan_amount, annual_rate,
                                 first_payment_date, last_payment_date,
                                 monthly_payment, adjust_business_days)
           VALUES (1, 'Старая ипотека', 2995218.84, 7.99,
                   '2026-04-02', '2051-03-02', 23124.77, 1)"""
    )
    conn.execute(
        """INSERT INTO repayment_strategy (id, mortgage_id, lump_sum, lump_sum_date,
                                           monthly_budget, monthly_start_date,
                                           monthly_extra_day, repayment_mode)
           VALUES (1, 1, 500000.0, '2026-04-17', 40000.0, '2026-05-02', 15, 'reduce_term')"""
    )
    conn.execute(
        """INSERT INTO deposit (id, name, annual_rate, term_months, capitalization)
           VALUES (1, 'Старый вклад', 16.0, 12, 1)"""
    )
    conn.execute(
        """INSERT INTO comparison (id, repayment_strategy_id, deposit_id,
                                   deposit_income, baseline_total_interest, winner)
           VALUES (1, 1, 1, 86135.4, 3916570.47, 'reduce_term')"""
    )
    conn.commit()
    conn.close()


class OldDatabaseMigrationTest(unittest.TestCase):
    """init_db() на старой базе: колонка добавляется, данные остаются."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='mortgage-migration-')
        self.db_path = os.path.join(self.tmpdir, 'mortgage_web.db')
        build_old_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_старая_база_без_колонки(self):
        """Фикстура действительно старая — иначе тест ничего не проверяет."""
        conn = sqlite3.connect(self.db_path)
        self.assertNotIn('early_repayment_allocation', columns(conn, 'repayment_strategy'))
        conn.close()

    def test_колонка_добавляется_с_дефолтом(self):
        init_db(self.db_path)

        conn = sqlite3.connect(self.db_path)
        self.assertIn('early_repayment_allocation', columns(conn, 'repayment_strategy'))
        value = conn.execute(
            'SELECT early_repayment_allocation FROM repayment_strategy WHERE id = 1'
        ).fetchone()[0]
        conn.close()
        self.assertEqual(value, 'principal_only')

    def test_строки_уцелели(self):
        init_db(self.db_path)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        strategy = conn.execute('SELECT * FROM repayment_strategy WHERE id = 1').fetchone()
        self.assertIsNotNone(strategy, 'строка repayment_strategy пропала — таблицу пересоздали')
        self.assertEqual(strategy['lump_sum'], 500000.0)
        self.assertEqual(strategy['lump_sum_date'], '2026-04-17')
        self.assertEqual(strategy['monthly_extra_day'], 15)
        self.assertEqual(strategy['repayment_mode'], 'reduce_term')

        comparison = conn.execute('SELECT * FROM comparison WHERE id = 1').fetchone()
        self.assertIsNotNone(comparison, 'строка comparison пропала — таблицу пересоздали')
        self.assertEqual(comparison['winner'], 'reduce_term')
        self.assertEqual(comparison['baseline_total_interest'], 3916570.47)

        self.assertEqual(
            conn.execute('SELECT COUNT(*) FROM mortgage').fetchone()[0], 1)
        self.assertEqual(
            conn.execute('SELECT COUNT(*) FROM deposit').fetchone()[0], 1)
        conn.close()

    def test_повторный_запуск_идемпотентен(self):
        init_db(self.db_path)

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE repayment_strategy SET early_repayment_allocation = 'interest_first' WHERE id = 1")
        conn.commit()
        conn.close()

        init_db(self.db_path)

        conn = sqlite3.connect(self.db_path)
        cols = columns(conn, 'repayment_strategy')
        self.assertEqual(cols.count('early_repayment_allocation'), 1)
        value = conn.execute(
            'SELECT early_repayment_allocation FROM repayment_strategy WHERE id = 1'
        ).fetchone()[0]
        rows = conn.execute('SELECT COUNT(*) FROM comparison').fetchone()[0]
        conn.close()
        self.assertEqual(value, 'interest_first', 'повторный init_db затёр сохранённое значение')
        self.assertEqual(rows, 1)

    def test_оба_значения_записываются(self):
        init_db(self.db_path)

        conn = sqlite3.connect(self.db_path)
        for allocation in ('principal_only', 'interest_first'):
            conn.execute(
                """INSERT INTO repayment_strategy (mortgage_id, repayment_mode,
                                                   early_repayment_allocation)
                   VALUES (1, 'reduce_payment', ?)""",
                (allocation,),
            )
        conn.commit()
        saved = [r[0] for r in conn.execute(
            'SELECT early_repayment_allocation FROM repayment_strategy ORDER BY id')]
        conn.close()
        self.assertEqual(saved, ['principal_only', 'principal_only', 'interest_first'])


@unittest.skipUnless(os.path.exists(USER_DB), 'db/mortgage_web.db отсутствует')
class UserDatabaseCopyTest(unittest.TestCase):
    """Тот же прогон на копии реальной базы пользователя (оригинал не трогаем)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='mortgage-migration-real-')
        self.db_path = os.path.join(self.tmpdir, 'mortgage_web.db')
        shutil.copy2(USER_DB, self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_история_переживает_миграцию(self):
        conn = sqlite3.connect(self.db_path)
        before_comparisons = conn.execute('SELECT COUNT(*) FROM comparison').fetchone()[0]
        before_strategies = conn.execute('SELECT COUNT(*) FROM repayment_strategy').fetchone()[0]
        before_winners = [r[0] for r in conn.execute('SELECT winner FROM comparison ORDER BY id')]
        conn.close()

        init_db(self.db_path)

        conn = sqlite3.connect(self.db_path)
        self.assertIn('early_repayment_allocation', columns(conn, 'repayment_strategy'))
        self.assertEqual(
            conn.execute('SELECT COUNT(*) FROM comparison').fetchone()[0], before_comparisons)
        self.assertEqual(
            conn.execute('SELECT COUNT(*) FROM repayment_strategy').fetchone()[0], before_strategies)
        self.assertEqual(
            [r[0] for r in conn.execute('SELECT winner FROM comparison ORDER BY id')], before_winners)
        allocations = {r[0] for r in conn.execute(
            'SELECT early_repayment_allocation FROM repayment_strategy')}
        conn.close()
        self.assertTrue(allocations <= {'principal_only'},
                        f'старые стратегии получили не тот дефолт: {allocations}')


if __name__ == '__main__':
    unittest.main()
