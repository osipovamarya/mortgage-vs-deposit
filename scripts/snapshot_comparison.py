#!/usr/bin/env python3
"""
Снимок сохранённых строк таблицы ``comparison`` из ``db/mortgage_web.db``.

Зачем. На Итерации 5 таблица ``comparison`` пересоздаётся через DROP_ALL, и
реальная история пользователя исчезнет. Приёмка И5 «сверить winner до/после на
сохранённых строках» требует, чтобы эти строки были зафиксированы ЗАРАНЕЕ —
вместе со связанными строками ``repayment_strategy`` / ``mortgage`` / ``deposit``,
то есть вместе со всеми входами, по которым строку можно пересчитать заново.

Гарантия read-only. Скрипт никогда не пишет в базу пользователя:

* исходная база открывается только по URI ``file:...?mode=ro`` — SQLite
  физически запрещает запись в такое соединение;
* в режиме ``--source api`` база КОПИРУЕТСЯ во временный каталог, и приложение
  поднимается на копии: ``create_app()`` вызывает ``init_db()``, который умеет
  и ``ALTER TABLE``, и ``DROP TABLE``, — оригинал он не увидит;
* к API ходят только методы GET.

Режимы снятия:

* ``--source api`` (по умолчанию) — как написано в роадмапе: ``GET /api/comparison``
  и ``GET /api/comparison/<id>``, плюс ``GET /api/mortgage/<id>`` и
  ``GET /api/deposit/<id>`` для связанных строк. Без ``--base-url`` приложение
  поднимается внутри процесса на копии базы (тестовый клиент Flask), с
  ``--base-url`` запросы уходят по HTTP на уже поднятый сервер.
* ``--source sqlite`` — прямое чтение SELECT'ами, если API поднять не выходит.

У таблицы ``repayment_strategy`` GET-эндпоинта нет, поэтому её строки в любом
режиме читаются SELECT'ом; в JSON это отмечено в ``related_source``.

Примеры:

    # снять через локально поднятый сервер (порт 5099 — на КОПИИ базы)
    cd web && DB_PATH=/tmp/copy.db PYTHONPATH=.. ../.venv/bin/python \\
        -m flask --app app/main.py run --port 5099
    .venv/bin/python scripts/snapshot_comparison.py --base-url http://127.0.0.1:5099

    # снять без сервера, приложение поднимается внутри процесса
    .venv/bin/python scripts/snapshot_comparison.py

    # снять прямым чтением sqlite
    .venv/bin/python scripts/snapshot_comparison.py --source sqlite
"""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import date

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(REPO_ROOT, 'db', 'mortgage_web.db')
DEFAULT_OUT = os.path.join(REPO_ROOT, 'tests', 'golden', 'comparison_before_v4.json')

TABLES = ('comparison', 'repayment_strategy', 'mortgage', 'deposit')


# ---------------------------------------------------------------------------
# Чтение базы (только SELECT, соединение открыто в режиме read-only)
# ---------------------------------------------------------------------------

def _connect_ro(db_path):
    uri = 'file:{}?mode=ro'.format(os.path.abspath(db_path).replace('?', '%3f').replace('#', '%23'))
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn, table):
    return [row[1] for row in conn.execute('PRAGMA table_info({})'.format(table))]


def _select_all(conn, table):
    return [dict(r) for r in conn.execute('SELECT * FROM {} ORDER BY id'.format(table))]


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Снятие через API
# ---------------------------------------------------------------------------

class _HttpClient:
    """GET по HTTP на уже поднятый сервер."""

    def __init__(self, base_url):
        self.base_url = base_url.rstrip('/')

    def get(self, path):
        from urllib.request import urlopen
        with urlopen(self.base_url + path, timeout=10) as resp:
            if resp.status != 200:
                raise RuntimeError('GET {} → HTTP {}'.format(path, resp.status))
            return json.loads(resp.read().decode('utf-8'))

    def describe(self):
        return 'GET по HTTP на {}'.format(self.base_url)


class _InProcessClient:
    """GET через тестовый клиент Flask; приложение поднято на копии базы."""

    def __init__(self, db_copy_path):
        sys.path.insert(0, os.path.join(REPO_ROOT, 'web'))
        os.environ['DB_PATH'] = db_copy_path
        from app.main import create_app  # noqa: импорт после подстановки DB_PATH
        self._app = create_app()
        self._client = self._app.test_client()
        self._db_copy_path = db_copy_path

    def get(self, path):
        resp = self._client.get(path)
        if resp.status_code != 200:
            raise RuntimeError('GET {} → HTTP {}'.format(path, resp.status_code))
        return resp.get_json()

    def describe(self):
        return 'GET через тестовый клиент Flask (create_app() на копии базы)'


def _capture_via_api(client, conn_ro):
    """Строки comparison и связанные с ними — через GET-эндпоинты."""
    listing = client.get('/api/comparison')
    rows = []
    strategies = {r['id']: r for r in _select_all(conn_ro, 'repayment_strategy')}

    for item in sorted(listing, key=lambda r: r['id']):
        comparison = client.get('/api/comparison/{}'.format(item['id']))
        strategy = strategies.get(comparison['repayment_strategy_id'])
        mortgage = client.get('/api/mortgage/{}'.format(strategy['mortgage_id'])) if strategy else None
        deposit = client.get('/api/deposit/{}'.format(comparison['deposit_id']))
        rows.append({
            'id': comparison['id'],
            'winner': comparison['winner'],
            'comparison': comparison,
            'repayment_strategy': strategy,
            'mortgage': mortgage,
            'deposit': deposit,
        })
    return rows


def _capture_via_sqlite(conn_ro):
    """То же самое прямым чтением, если API недоступен."""
    strategies = {r['id']: r for r in _select_all(conn_ro, 'repayment_strategy')}
    mortgages = {r['id']: r for r in _select_all(conn_ro, 'mortgage')}
    deposits = {r['id']: r for r in _select_all(conn_ro, 'deposit')}

    rows = []
    for comparison in _select_all(conn_ro, 'comparison'):
        strategy = strategies.get(comparison['repayment_strategy_id'])
        mortgage = mortgages.get(strategy['mortgage_id']) if strategy else None
        rows.append({
            'id': comparison['id'],
            'winner': comparison['winner'],
            'comparison': comparison,
            'repayment_strategy': strategy,
            'mortgage': mortgage,
            'deposit': deposits.get(comparison['deposit_id']),
        })
    return rows


# ---------------------------------------------------------------------------
# Справочный пересчёт текущим кодом
# ---------------------------------------------------------------------------

_RECOMPUTE_FIELDS = (
    'deposit_income', 'deposit_final', 'deposit_net_saving', 'deposit_new_monthly',
    'reduce_payment_new_monthly', 'reduce_payment_interest_saved',
    'reduce_term_interest_saved', 'reduce_term_months_saved', 'reduce_term_months_to_payoff',
    'snowball_total_interest', 'snowball_interest_saved', 'snowball_months_to_payoff',
    'snowball_deposit_income', 'snowball_deposit_final',
    'baseline_total_interest', 'winner',
)


def _recompute(rows):
    """Прогнать сохранённые входы через текущий run_comparison. Базу не трогает."""
    web_dir = os.path.join(REPO_ROOT, 'web')
    if web_dir not in sys.path:
        sys.path.insert(0, web_dir)
    from app.calculator import run_comparison

    out = []
    for row in rows:
        entry = {'id': row['id']}
        try:
            result = run_comparison(row['mortgage'], row['deposit'], row['repayment_strategy'])
            entry['values'] = {k: result.get(k) for k in _RECOMPUTE_FIELDS}
            entry['winner_matches_saved'] = result.get('winner') == row['winner']
        except Exception as exc:  # снимок важнее пересчёта — падать нельзя
            entry['error'] = '{}: {}'.format(type(exc).__name__, exc)
        out.append(entry)
    return out


# ---------------------------------------------------------------------------

def build_snapshot(db_path, source, base_url, captured_at, with_recompute):
    conn_ro = _connect_ro(db_path)
    tmp_dir = None
    try:
        columns = {table: _columns(conn_ro, table) for table in TABLES}
        user_version = conn_ro.execute('PRAGMA user_version').fetchone()[0]

        if source == 'api':
            if base_url:
                client = _HttpClient(base_url)
            else:
                tmp_dir = tempfile.mkdtemp(prefix='snapshot_comparison_')
                db_copy = os.path.join(tmp_dir, os.path.basename(db_path))
                shutil.copy2(db_path, db_copy)
                client = _InProcessClient(db_copy)
            rows = _capture_via_api(client, conn_ro)
            source_detail = (
                '{}; comparison и mortgage/deposit — через GET-эндпоинты, '
                'repayment_strategy — SELECT (GET-эндпоинта нет)'.format(client.describe())
            )
            related_source = {
                'comparison': 'api: GET /api/comparison, GET /api/comparison/<id>',
                'mortgage': 'api: GET /api/mortgage/<id>',
                'deposit': 'api: GET /api/deposit/<id>',
                'repayment_strategy': 'sqlite: SELECT (GET-эндпоинта нет)',
            }
        else:
            rows = _capture_via_sqlite(conn_ro)
            source_detail = 'прямое чтение SELECT из копии схемы, соединение открыто mode=ro'
            related_source = {table: 'sqlite: SELECT' for table in TABLES}

        snapshot = {
            'schema_version': 1,
            'captured_at': captured_at,
            'captured_before': 'Итерация 5 (пересоздание таблицы comparison через DROP_ALL)',
            'source': source,
            'source_detail': source_detail,
            'related_source': related_source,
            'read_only': 'оригинал базы открыт только mode=ro; приложение (если поднималось) работало на копии',
            'db_path': os.path.relpath(os.path.abspath(db_path), REPO_ROOT),
            'db_sha256': _sha256(db_path),
            'db_user_version': user_version,
            'note': (
                'Реальная история пользователя. Каждая строка самодостаточна: '
                'вместе с comparison лежат repayment_strategy, mortgage и deposit по внешним ключам, '
                'поэтому строку можно пересчитать после пересоздания таблицы на И5 и сверить winner.'
            ),
            'columns': columns,
            'row_count': len(rows),
            'winners': {str(row['id']): row['winner'] for row in rows},
            'rows': rows,
        }

        if with_recompute:
            snapshot['recomputed_by_current_code'] = {
                '__комментарий': (
                    'Справочный слепок: как run_comparison считает те же входы на коммите Итерации 0. '
                    'Это НЕ golden — И1, И3 и И5 меняют эти числа осознанно. Нужен, чтобы на И5 '
                    'отличить «изменилось при пересоздании таблицы» от «уже расходилось с сохранённой строкой».'
                ),
                'rows': _recompute(rows),
            }

        return snapshot
    finally:
        conn_ro.close()
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Снять снимок сохранённых строк comparison до пересоздания таблицы на И5. '
                    'Только чтение: в базу пользователя скрипт не пишет никогда.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--db', default=DEFAULT_DB,
                        help='путь к базе (по умолчанию db/mortgage_web.db)')
    parser.add_argument('--out', default=DEFAULT_OUT,
                        help='куда писать JSON (по умолчанию tests/golden/comparison_before_v4.json)')
    parser.add_argument('--source', choices=('api', 'sqlite'), default='api',
                        help='как снимать: через GET-эндпоинты (api, по умолчанию) или SELECT (sqlite)')
    parser.add_argument('--base-url',
                        help='адрес уже поднятого приложения, например http://127.0.0.1:5099; '
                             'без него api-режим поднимает приложение внутри процесса на копии базы')
    parser.add_argument('--captured-at', default=date.today().isoformat(),
                        help='дата снятия в поле captured_at (по умолчанию сегодня)')
    parser.add_argument('--no-recompute', action='store_true',
                        help='не добавлять справочный пересчёт текущим кодом')
    parser.add_argument('--stdout', action='store_true',
                        help='напечатать JSON вместо записи файла')
    args = parser.parse_args(argv)

    if args.base_url and args.source != 'api':
        parser.error('--base-url имеет смысл только с --source api')
    if not os.path.exists(args.db):
        parser.error('база не найдена: {}'.format(args.db))

    snapshot = build_snapshot(
        args.db, args.source, args.base_url, args.captured_at, not args.no_recompute
    )
    text = json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=False) + '\n'

    if args.stdout:
        sys.stdout.write(text)
    else:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, 'w', encoding='utf-8') as fh:
            fh.write(text)
        print('Снято строк: {} → {}'.format(snapshot['row_count'], args.out))
        print('Победители: ' + ', '.join(
            '{} → {}'.format(k, v) for k, v in snapshot['winners'].items()
        ))
    return 0


if __name__ == '__main__':
    sys.exit(main())
