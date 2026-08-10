#!/usr/bin/env python3
"""
Golden-снимок текущего поведения расчётных функций (Итерация 0 роадмапа).

Скрипт прогоняет матрицу входов из ``tests/matrix.py`` через ТЕКУЩИЕ функции
``web/app/calculator.py`` и складывает результат в ``tests/golden/<функция>.json``.
Снимок фиксирует поведение КАК ЕСТЬ, вместе с известными дефектами: его задача —
показать, что именно изменилось, а не утверждать, что оно правильное.

Графики сохраняются построчно и целиком. Ради размера строка графика пишется
не объектом, а массивом значений в порядке ключа ``columns``.

Формат файла::

    {
    "function": "simulate_lump_repayment",
    "columns": ["payment_num", "date", "payment", "principal", "interest", "balance", "early"],
    "cases": {
    "<id кейса>": {"kwargs": {...}, "result": {"total_interest": ..., "schedule": [[...], ...]}},
    ...
    }
    }

Каждый кейс печатается одной строкой — так git показывает построчный diff вместо
одного мегабайтного изменения. Порядок кейсов — сортировка по id, ключи внутри
кейса сериализуются с ``sort_keys=True``, даты в ``kwargs`` — ISO-строки. Поэтому
повторный прогон на тех же входах даёт побайтово тот же файл.

Режимы (их намеренно ровно столько; массовой перезаписи всех голденов одной
командой не предусмотрено — И1, И3 и И4 меняют числа выборочно, и движение
«прогнал скрипт, закоммитил» обязано быть невозможным)::

    --check                             сравнить с закоммиченными JSON, ничего не писать
    --accept <функция> --reason "..."   переснять голден РОВНО ОДНОЙ функции + строка в CHANGELOG
    --init                              первичное создание голденов (падает, если они уже есть)
    --list                              список функций, число кейсов, состояние файлов

Запуск::

    .venv/bin/python scripts/snapshot_golden.py --check
    .venv/bin/python scripts/snapshot_golden.py --accept calc_repayment_schedule --reason "..."
"""
import argparse
import json
import os
import sys
from datetime import date, datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN_DIR = os.path.join(REPO_ROOT, 'tests', 'golden')
CHANGELOG_PATH = os.path.join(REPO_ROOT, 'CHANGELOG.md')
KNOWN_BUGS_PATH = os.path.join(GOLDEN_DIR, 'known_bugs.json')

for _p in (os.path.join(REPO_ROOT, 'web'), os.path.join(REPO_ROOT, 'tests')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from matrix import ALL_SUITES  # noqa: E402
from app.calculator import (  # noqa: E402
    build_amortization,
    calc_repayment_schedule,
    simulate_lump_repayment,
)

# Порядок полей строки графика. Менять нельзя, не переснимая все голдены.
ROW_COLUMNS = ('payment_num', 'date', 'payment', 'principal', 'interest', 'balance', 'early')

DEFAULT_DIFF_LIMIT = 10


# ---------------------------------------------------------------------------
# Прогон матрицы
# ---------------------------------------------------------------------------

def _pack_row(row):
    """Строка графика → массив значений в порядке ROW_COLUMNS."""
    return [row[column] for column in ROW_COLUMNS]


def _pack_schedule(schedule):
    return [_pack_row(row) for row in schedule]


def _run_build_amortization(kwargs):
    schedule, first_payment, total_interest = build_amortization(**kwargs)
    return {
        'first_payment': first_payment,
        'total_interest': total_interest,
        'schedule': _pack_schedule(schedule),
    }


def _run_simulate_lump_repayment(kwargs):
    schedule, monthly_payment, total_interest, annuity_months = simulate_lump_repayment(**kwargs)
    return {
        'monthly_payment': monthly_payment,
        'total_interest': total_interest,
        'annuity_months': annuity_months,
        'schedule': _pack_schedule(schedule),
    }


def _run_calc_repayment_schedule(kwargs):
    total_interest, months_to_payoff, schedule = calc_repayment_schedule(**kwargs)
    return {
        'total_interest': total_interest,
        'months_to_payoff': months_to_payoff,
        'schedule': _pack_schedule(schedule),
    }


RUNNERS = {
    'build_amortization': _run_build_amortization,
    'simulate_lump_repayment': _run_simulate_lump_repayment,
    'calc_repayment_schedule': _run_calc_repayment_schedule,
}

FUNCTIONS = tuple(sorted(RUNNERS))


def serialize_kwargs(kwargs):
    """Аргументы вызова → JSON-совместимый dict (даты как ISO-строки)."""
    out = {}
    for key, value in kwargs.items():
        if isinstance(value, datetime):
            out[key] = value.isoformat()
        elif isinstance(value, date):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out


def run_suite(function):
    """
    Прогнать матрицу одной функции.

    Возвращает dict ``{id кейса: {'kwargs': ..., 'result': ...}}``.
    """
    runner = RUNNERS[function]
    cases = {}
    for case in ALL_SUITES[function]():
        cases[case['id']] = {
            'kwargs': serialize_kwargs(case['kwargs']),
            'result': runner(case['kwargs']),
        }
    return cases


def golden_path(function):
    return os.path.join(GOLDEN_DIR, f'{function}.json')


# ---------------------------------------------------------------------------
# Сериализация / чтение
# ---------------------------------------------------------------------------

def render_golden(function, cases):
    """Собрать текст golden-файла. Детерминирован при одинаковых входах."""
    lines = [
        '{',
        '"function": %s,' % json.dumps(function, ensure_ascii=False),
        '"columns": %s,' % json.dumps(list(ROW_COLUMNS), ensure_ascii=False),
        '"cases": {',
    ]
    items = sorted(cases.items())
    for i, (case_id, payload) in enumerate(items):
        tail = ',' if i < len(items) - 1 else ''
        lines.append('%s: %s%s' % (
            json.dumps(case_id, ensure_ascii=False),
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')),
            tail,
        ))
    lines.append('}')
    lines.append('}')
    return '\n'.join(lines) + '\n'


def write_golden(function, cases):
    os.makedirs(GOLDEN_DIR, exist_ok=True)
    path = golden_path(function)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(render_golden(function, cases))
    return path


def load_golden(function):
    """Прочитать закоммиченный голден. Бросает FileNotFoundError, если его нет."""
    with open(golden_path(function), encoding='utf-8') as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Сравнение
# ---------------------------------------------------------------------------

def _walk_diff(path, expected, actual, out, limit):
    """Рекурсивно собрать расхождения в виде (путь, ожидалось, получено)."""
    if len(out) >= limit:
        return
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            if key not in expected:
                out.append((f'{path}.{key}', '<нет в голдене>', actual[key]))
            elif key not in actual:
                out.append((f'{path}.{key}', expected[key], '<нет в прогоне>'))
            else:
                _walk_diff(f'{path}.{key}', expected[key], actual[key], out, limit)
            if len(out) >= limit:
                return
        return
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            out.append((f'{path} (длина)', len(expected), len(actual)))
            return
        for i, (exp_item, act_item) in enumerate(zip(expected, actual)):
            _walk_diff(f'{path}[{i}]', exp_item, act_item, out, limit)
            if len(out) >= limit:
                return
        return
    if expected != actual:
        out.append((path, expected, actual))


def diff_case(expected_case, actual_case, limit=3):
    """Расхождения одного кейса: сначала входы, потом результат."""
    out = []
    _walk_diff('kwargs', expected_case.get('kwargs'), actual_case.get('kwargs'), out, limit)
    if len(out) < limit:
        _walk_diff('result', expected_case.get('result'), actual_case.get('result'), out, limit)
    return out


def compare_function(function, limit=DEFAULT_DIFF_LIMIT):
    """
    Сравнить свежий прогон с закоммиченным голденом.

    Возвращает (число разошедшихся кейсов, список отчётов о первых расхождениях).
    Отчёт — dict с ключами function / case_id / kwargs / diffs.
    """
    golden = load_golden(function)
    stored = golden.get('cases', {})
    columns = tuple(golden.get('columns', ()))
    if columns != ROW_COLUMNS:
        raise ValueError(
            f'{function}: порядок колонок в голдене {columns} не совпадает с текущим {ROW_COLUMNS}'
        )

    fresh = run_suite(function)
    mismatched = 0
    reports = []

    for case_id in sorted(set(stored) | set(fresh)):
        if case_id not in fresh:
            mismatched += 1
            if len(reports) < limit:
                reports.append({
                    'function': function,
                    'case_id': case_id,
                    'kwargs': stored[case_id].get('kwargs'),
                    'diffs': [('<кейс>', 'есть в голдене', 'матрица его больше не строит')],
                })
            continue
        if case_id not in stored:
            mismatched += 1
            if len(reports) < limit:
                reports.append({
                    'function': function,
                    'case_id': case_id,
                    'kwargs': fresh[case_id].get('kwargs'),
                    'diffs': [('<кейс>', 'нет в голдене', 'появился в матрице')],
                })
            continue
        diffs = diff_case(stored[case_id], fresh[case_id])
        if diffs:
            mismatched += 1
            if len(reports) < limit:
                reports.append({
                    'function': function,
                    'case_id': case_id,
                    'kwargs': fresh[case_id].get('kwargs'),
                    'diffs': diffs,
                })

    return mismatched, reports


# ---------------------------------------------------------------------------
# Реестр известных багов снежного кома
# ---------------------------------------------------------------------------

KNOWN_BUG_COMMENT = 'известный баг min(annuity, budget), чинится на И3'

KNOWN_BUG_CRITERION = (
    'график закончился, а остаток не закрыт: последняя строка имеет balance > 0.01 '
    '(эквивалентно Σ principal < loan_amount). Признак вычисляется по факту прогона, '
    'а не по угадыванию конфигурации.'
)

# Контрольный замер роадмапа (раздел «Итерация 0»).
ROADMAP_CONTROL_KWARGS = {
    'loan_amount': 8_000_000.0,
    'annual_rate': 16.0,
    'first_payment_date': '02.04.2026',
    'last_payment_date': '02.03.2051',
    'lump_sum': 0,
    'lump_idx': 0,
    'monthly_budget': 40000,
    'monthly_idx': 0,
    'monthly_extra_day': 15,
}

ROADMAP_CONTROL_EXPECTED = {
    'rows': 299,
    'sum_principal': 0.0,
    'final_balance': 8_000_000.0,
    'sum_principal_with_extra_day_none': 8_000_000.0,
}


def _measure_snowball(kwargs):
    """Замер одного прогона снежка: чем закончился график."""
    total_interest, months_to_payoff, schedule = calc_repayment_schedule(**kwargs)
    sum_principal = round(sum(row['principal'] for row in schedule), 2)
    final_balance = schedule[-1]['balance'] if schedule else float(kwargs['loan_amount'])
    return {
        'rows': len(schedule),
        'months_to_payoff': months_to_payoff,
        'total_interest': total_interest,
        'sum_principal': sum_principal,
        'final_balance': final_balance,
        'shortfall': round(float(kwargs['loan_amount']) - sum_principal, 2),
    }


def _is_underpaying(measured):
    """Кредит не закрылся к последней дате графика."""
    return measured['final_balance'] > 0.01


def build_known_bugs():
    """
    Собрать реестр «недоплаточных» входов снежка.

    Кейсы берутся из той же матрицы и остаются в основном голдене снежка —
    known_bugs.json их не заменяет, а помечает.
    """
    cases = {}
    for case in ALL_SUITES['calc_repayment_schedule']():
        measured = _measure_snowball(case['kwargs'])
        if not _is_underpaying(measured):
            continue
        cases[case['id']] = {
            'kwargs': serialize_kwargs(case['kwargs']),
            'measured': measured,
            'comment': KNOWN_BUG_COMMENT,
        }

    control_bug = _measure_snowball(dict(ROADMAP_CONTROL_KWARGS))
    control_ok = _measure_snowball(dict(ROADMAP_CONTROL_KWARGS, monthly_extra_day=None))
    control_actual = {
        'rows': control_bug['rows'],
        'sum_principal': control_bug['sum_principal'],
        'final_balance': control_bug['final_balance'],
        'sum_principal_with_extra_day_none': control_ok['sum_principal'],
    }

    # Распределение отобранных кейсов по осям — замеряется, а не предполагается.
    summary = {
        'by_monthly_budget': {},
        'by_monthly_extra_day': {},
        'by_annual_rate': {},
        'by_loan_amount': {},
    }
    for case in cases.values():
        kwargs = case['kwargs']
        for field, key in (('by_monthly_budget', 'monthly_budget'),
                           ('by_monthly_extra_day', 'monthly_extra_day'),
                           ('by_annual_rate', 'annual_rate'),
                           ('by_loan_amount', 'loan_amount')):
            bucket = str(kwargs[key])
            summary[field][bucket] = summary[field].get(bucket, 0) + 1

    return {
        'function': 'calc_repayment_schedule',
        'comment': KNOWN_BUG_COMMENT,
        'criterion': KNOWN_BUG_CRITERION,
        'summary': summary,
        'control': {
            'source': 'ROADMAP.md, «Итерация 0 — Golden-снимок текущего поведения»',
            'kwargs': ROADMAP_CONTROL_KWARGS,
            'roadmap_expected': ROADMAP_CONTROL_EXPECTED,
            'measured': control_actual,
            'matches_roadmap': control_actual == ROADMAP_CONTROL_EXPECTED,
            'measured_with_extra_day_none': control_ok,
            'measured_with_extra_day_15': control_bug,
        },
        'case_count': len(cases),
        'cases': cases,
    }


def render_known_bugs(payload):
    """Текст known_bugs.json. Кейсы — по строке на кейс, порядок по id."""
    head = {k: v for k, v in payload.items() if k != 'cases'}
    lines = []
    for key in ('function', 'comment', 'criterion', 'case_count', 'summary', 'control'):
        lines.append('%s: %s,' % (
            json.dumps(key, ensure_ascii=False),
            json.dumps(head[key], ensure_ascii=False, sort_keys=True),
        ))
    lines.append('"cases": {')
    items = sorted(payload['cases'].items())
    for i, (case_id, case) in enumerate(items):
        tail = ',' if i < len(items) - 1 else ''
        lines.append('%s: %s%s' % (
            json.dumps(case_id, ensure_ascii=False),
            json.dumps(case, ensure_ascii=False, sort_keys=True, separators=(',', ':')),
            tail,
        ))
    lines.append('}')
    return '{\n' + '\n'.join(lines) + '\n}\n'


def write_known_bugs():
    os.makedirs(GOLDEN_DIR, exist_ok=True)
    payload = build_known_bugs()
    with open(KNOWN_BUGS_PATH, 'w', encoding='utf-8') as fh:
        fh.write(render_known_bugs(payload))
    return payload


# ---------------------------------------------------------------------------
# CHANGELOG
# ---------------------------------------------------------------------------

def append_changelog_entry(function, reason):
    """
    Дописать строку о переснятом голдене в раздел «Unreleased» CHANGELOG.md.

    Если раздела нет — он создаётся перед первой версией. Подраздел «Changed»
    создаётся при необходимости; строка добавляется в его конец.
    """
    entry = (
        f'- Golden-снимок `{function}` переснят '
        f'(`scripts/snapshot_golden.py --accept {function}`): {reason}'
    )

    if not os.path.exists(CHANGELOG_PATH):
        text = '# Changelog\n\n## [Unreleased]\n\n### Changed\n\n' + entry + '\n'
        with open(CHANGELOG_PATH, 'w', encoding='utf-8') as fh:
            fh.write(text)
        return entry

    with open(CHANGELOG_PATH, encoding='utf-8') as fh:
        lines = fh.read().split('\n')

    def _first_index(predicate, start=0):
        for i in range(start, len(lines)):
            if predicate(lines[i]):
                return i
        return None

    unreleased = _first_index(lambda ln: ln.strip().lower().startswith('## [unreleased]'))
    if unreleased is None:
        first_release = _first_index(lambda ln: ln.startswith('## ['))
        insert_at = first_release if first_release is not None else len(lines)
        lines[insert_at:insert_at] = ['## [Unreleased]', '', '### Changed', '', entry, '']
        with open(CHANGELOG_PATH, 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(lines))
        return entry

    # Границы раздела Unreleased.
    section_end = _first_index(lambda ln: ln.startswith('## '), unreleased + 1)
    if section_end is None:
        section_end = len(lines)

    changed = None
    for i in range(unreleased + 1, section_end):
        if lines[i].strip().lower().startswith('### changed'):
            changed = i
            break

    if changed is None:
        lines[section_end:section_end] = ['### Changed', '', entry, '']
    else:
        block_end = section_end
        for i in range(changed + 1, section_end):
            if lines[i].startswith('### '):
                block_end = i
                break
        insert_at = block_end
        while insert_at > changed + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines[insert_at:insert_at] = [entry]

    with open(CHANGELOG_PATH, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines))
    return entry


# ---------------------------------------------------------------------------
# Печать
# ---------------------------------------------------------------------------

def _short(value, width=110):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True) if not isinstance(value, str) else value
    if len(text) > width:
        text = text[:width - 1] + '…'
    return text


def print_reports(reports, mismatched_total, limit):
    print(f'Расхождений: {mismatched_total} кейс(ов). Показаны первые {min(limit, len(reports))}.')
    for report in reports:
        print()
        print(f'  функция : {report["function"]}')
        print(f'  кейс    : {report["case_id"]}')
        print(f'  вход    : {_short(report["kwargs"])}')
        for path, expected, actual in report['diffs']:
            print(f'    {path}')
            print(f'      ожидалось: {_short(expected)}')
            print(f'      получено : {_short(actual)}')


# ---------------------------------------------------------------------------
# Режимы
# ---------------------------------------------------------------------------

def mode_list():
    print('Функции матрицы и состояние голденов:')
    print()
    for function in FUNCTIONS:
        cases = ALL_SUITES[function]()
        path = golden_path(function)
        if os.path.exists(path):
            size_mb = os.path.getsize(path) / 1024 / 1024
            state = f'есть, {size_mb:.2f} МБ'
        else:
            state = 'НЕТ'
        print(f'  {function:<26} кейсов: {len(cases):>5}   голден: {state}')
    print()
    if os.path.exists(KNOWN_BUGS_PATH):
        with open(KNOWN_BUGS_PATH, encoding='utf-8') as fh:
            payload = json.load(fh)
        print(f'  known_bugs.json            кейсов: {payload.get("case_count", 0):>5}   '
              f'({payload.get("comment", "")})')
    else:
        print('  known_bugs.json            НЕТ')
    return 0


def mode_check(limit):
    missing = [f for f in FUNCTIONS if not os.path.exists(golden_path(f))]
    if missing:
        print('Голдены отсутствуют: ' + ', '.join(missing))
        print('Первичное создание: scripts/snapshot_golden.py --init')
        return 1

    total = 0
    reports = []
    for function in FUNCTIONS:
        remaining = max(limit - len(reports), 0)
        mismatched, function_reports = compare_function(function, limit=max(remaining, 1))
        total += mismatched
        reports.extend(function_reports[:remaining] if remaining else [])
        status = 'OK' if mismatched == 0 else f'РАСХОЖДЕНИЙ: {mismatched}'
        print(f'{function:<26} {status}')

    if total == 0:
        print('\n0 расхождений с закоммиченными голденами.')
        return 0

    print()
    print_reports(reports, total, limit)
    return 1


def mode_init():
    existing = [f for f in FUNCTIONS if os.path.exists(golden_path(f))]
    if os.path.exists(KNOWN_BUGS_PATH):
        existing.append('known_bugs.json')
    if existing:
        print('--init работает только когда голденов ещё нет. Уже существуют: '
              + ', '.join(existing))
        print('Переснять конкретную функцию: --accept <функция> --reason "..."')
        return 1

    for function in FUNCTIONS:
        cases = run_suite(function)
        path = write_golden(function, cases)
        size_mb = os.path.getsize(path) / 1024 / 1024
        print(f'{function:<26} кейсов: {len(cases):>5}   → {os.path.relpath(path, REPO_ROOT)} '
              f'({size_mb:.2f} МБ)')

    payload = write_known_bugs()
    print(f'{"known_bugs":<26} кейсов: {payload["case_count"]:>5}   '
          f'→ {os.path.relpath(KNOWN_BUGS_PATH, REPO_ROOT)}')
    if not payload['control']['matches_roadmap']:
        print('ВНИМАНИЕ: контрольный замер роадмапа не совпал с фактом, см. ключ "control".')
    print('\nВ CHANGELOG.md при --init намеренно ничего не пишется.')
    return 0


def mode_accept(function, reason):
    cases = run_suite(function)
    path = write_golden(function, cases)
    size_mb = os.path.getsize(path) / 1024 / 1024
    print(f'{function}: переснято {len(cases)} кейсов → '
          f'{os.path.relpath(path, REPO_ROOT)} ({size_mb:.2f} МБ)')

    if function == 'calc_repayment_schedule':
        payload = write_known_bugs()
        print(f'known_bugs.json обновлён: {payload["case_count"]} кейс(ов) недоплаты')

    entry = append_changelog_entry(function, reason)
    print(f'CHANGELOG.md: {entry}')
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog='snapshot_golden.py',
        description=(
            'Golden-снимок расчётных функций калькулятора. '
            'Снимок фиксирует текущее поведение как есть, включая известные дефекты.'
        ),
        epilog=(
            'Массовой перезаписи всех голденов одной командой нет намеренно: '
            'каждое изменение чисел должно быть осознанным и объяснённым в CHANGELOG.\n'
            'Примеры:\n'
            '  scripts/snapshot_golden.py --list\n'
            '  scripts/snapshot_golden.py --check\n'
            '  scripts/snapshot_golden.py --check --limit 3\n'
            '  scripts/snapshot_golden.py --accept simulate_lump_repayment '
            '--reason "единая база начисления, +251.14 ₽ на контрольном примере"'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--check', action='store_true',
                       help='сравнить прогон с закоммиченными голденами; ничего не пишет, '
                            'код возврата 1 при расхождении')
    group.add_argument('--accept', metavar='ФУНКЦИЯ', choices=FUNCTIONS,
                       help='переснять голден ровно одной функции; требует --reason. '
                            'Допустимо: ' + ', '.join(FUNCTIONS))
    group.add_argument('--init', action='store_true',
                       help='первичное создание всех голденов; падает, если хоть один уже есть')
    group.add_argument('--list', action='store_true',
                       help='показать функции, число кейсов и состояние файлов')
    parser.add_argument('--reason', metavar='ТЕКСТ',
                        help='обязательное объяснение к --accept; идёт строкой в CHANGELOG.md')
    parser.add_argument('--limit', type=int, default=DEFAULT_DIFF_LIMIT, metavar='N',
                        help=f'сколько расхождений печатать в --check (по умолчанию {DEFAULT_DIFF_LIMIT})')
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.accept and not args.reason:
        parser.error('--accept требует --reason "<текст>": голден не переснимается без объяснения')
    if args.reason and not args.accept:
        parser.error('--reason имеет смысл только вместе с --accept')
    if args.limit < 1:
        parser.error('--limit должен быть положительным')

    if args.list:
        return mode_list()
    if args.check:
        return mode_check(args.limit)
    if args.init:
        return mode_init()
    return mode_accept(args.accept, args.reason)


if __name__ == '__main__':
    sys.exit(main())
