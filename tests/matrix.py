"""
Единая матрица входов для golden-снимка (И0) и проверок инвариантов.

Модуль НЕ импортирует расчётные функции — только строит наборы аргументов,
поэтому пригоден и для scripts/snapshot_golden.py, и для tests/*.py.

Детерминизм обязателен: никакого datetime.now(), никакого random.
Все даты выводятся из констант, поэтому снимок воспроизводим на любой машине.

Использование:

    from matrix import amortization_cases, lump_cases, snowball_cases

    for case in lump_cases():
        schedule, payment, total, months = simulate_lump_repayment(**case['kwargs'])

Каждый кейс — dict:
    id      — стабильный человекочитаемый ключ (сортируемый, без дат в свободной форме)
    kwargs  — точные именованные аргументы целевой функции
    meta    — справочные поля (term, lump_date_kind, ...), в вызов не идут
"""
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from dateutil.relativedelta import relativedelta
from dateutil.rrule import rrule, MONTHLY

# ---------------------------------------------------------------------------
# Оси матрицы
# ---------------------------------------------------------------------------

LOANS = (1_000_000.0, 3_000_000.0, 8_000_000.0)
RATES = (5.0, 7.99, 16.0)
LUMPS = (0.0, 100_000.0, 500_000.0, 2_000_000.0)
MODES = ('reduce_payment', 'reduce_term')
ADJUST = (False, True)
BUDGETS = (None, 40_000.0, 60_000.0)
EXTRA_DAYS = (None, 1, 15, 28, 31)
LUMP_DATE_KINDS = ('none', 'before_first', 'between', 'on_payment', 'after_end')

# Дата ПЕРВОГО (уже прошедшего) платежа — от неё производится сетка:
#   next_dt = first_dt + 1 месяц, дальше rrule(MONTHLY) до last_dt.
# Ось из роадмапа «день first_payment_date {02, 28, 30, 31}» плюс кейс,
# в котором сетка начинается 31-го числа и rrule пропускает февраль целиком.
FIRST_DATES = (
    datetime(2026, 1, 2),    # обычный ранний день
    datetime(2026, 1, 28),   # 28-е — существует в любом месяце
    datetime(2026, 1, 30),   # 30-е
    datetime(2025, 12, 31),  # сетка с 31.01.2026 → февраль пропускается rrule
)

SHORT_TERM = 24   # месяцев в основной (широкой) сетке
LONG_TERM = 299   # контрольный длинный договор: 02.05.2026 → 02.03.2051

# Контрольный пример из роадмапа и CHANGELOG.
CONTROL_LOAN = 2_995_218.84
CONTROL_RATE = 7.99
CONTROL_PAYMENT = 23_124.77
CONTROL_FIRST = datetime(2026, 4, 2)

_CENT = Decimal('0.01')


# ---------------------------------------------------------------------------
# Вспомогательные вычисления (повторяют production-формулы, без импорта app)
# ---------------------------------------------------------------------------

def _r2(x):
    return x.quantize(_CENT, rounding=ROUND_HALF_UP)


def annuity(loan_amount, annual_rate, periods):
    """Аннуитет, закрывающий loan_amount за periods месяцев. Как в calculator.py."""
    rate = Decimal(str(annual_rate)) / Decimal(100) / Decimal(12)
    bal = Decimal(str(loan_amount))
    if periods <= 0 or bal <= 0:
        return 0.0
    factor = (1 + rate) ** periods
    return float(_r2(bal * rate * factor / (factor - 1)))


def grid(first_dt, term_months):
    """
    Сетка платежей так, как её строит run_comparison():
    next_dt = first_dt + 1 месяц, last_dt подобран так, чтобы вышло term_months дат.

    Возвращает (next_dt, last_dt, dates).
    """
    next_dt = first_dt + relativedelta(months=1)
    last_dt = next_dt + relativedelta(months=term_months - 1)
    dates = list(rrule(MONTHLY, dtstart=next_dt, until=last_dt))
    return next_dt, last_dt, dates


def lump_date_for(kind, first_dt, dates):
    """Дата разовой досрочки по виду. None — «сразу / без даты»."""
    if kind == 'none':
        return None
    if kind == 'before_first':
        return first_dt - timedelta(days=3)
    if kind == 'between':
        # строго внутри первого периода сетки
        return dates[0] + (dates[1] - dates[0]) // 2
    if kind == 'on_payment':
        # ровно в дату планового платежа (не первого — чтобы задеть ветку
        # «досрочка после аннуитета»)
        return dates[1]
    if kind == 'after_end':
        return dates[-1] + relativedelta(months=1)
    raise ValueError(f'unknown lump date kind: {kind}')


def _tag(dt):
    return dt.strftime('%Y%m%d')


# ---------------------------------------------------------------------------
# build_amortization
# ---------------------------------------------------------------------------

def amortization_cases():
    """
    Оси: loan × rate × adjust × first_date × {короткий срок}
         + длинный контрольный договор (loan × rate × adjust).

    Все вызовы идут с fixed_payment (как в run_comparison); ветка без
    fixed_payment мёртвая и в матрицу не входит.
    """
    cases = []
    for loan in LOANS:
        for rate in RATES:
            for first_dt in FIRST_DATES:
                next_dt, last_dt, dates = grid(first_dt, SHORT_TERM)
                payment = annuity(loan, rate, len(dates))
                for adj in ADJUST:
                    cases.append({
                        'id': f'amort/short/{int(loan)}/{rate}/{_tag(first_dt)}/adj{int(adj)}',
                        'kwargs': {
                            'loan_amount': loan,
                            'annual_rate': rate,
                            'first_payment_date': next_dt,
                            'last_payment_date': last_dt,
                            'adjust_business_days': adj,
                            'prev_payment_date': first_dt,
                            'fixed_payment': payment,
                        },
                        'meta': {'term': len(dates), 'kind': 'short'},
                    })

    # Длинный договор: контрольная конфигурация роадмапа.
    for loan in LOANS:
        for rate in RATES:
            next_dt, last_dt, dates = grid(CONTROL_FIRST, LONG_TERM)
            payment = annuity(loan, rate, len(dates))
            for adj in ADJUST:
                cases.append({
                    'id': f'amort/long/{int(loan)}/{rate}/adj{int(adj)}',
                    'kwargs': {
                        'loan_amount': loan,
                        'annual_rate': rate,
                        'first_payment_date': next_dt,
                        'last_payment_date': last_dt,
                        'adjust_business_days': adj,
                        'prev_payment_date': CONTROL_FIRST,
                        'fixed_payment': payment,
                    },
                    'meta': {'term': len(dates), 'kind': 'long'},
                })

    # Контрольные числа роадмапа: 3 916 570.47 (adj=False) / 3 930 963.06 (adj=True).
    next_dt, last_dt, dates = grid(CONTROL_FIRST, LONG_TERM)
    for adj in ADJUST:
        cases.append({
            'id': f'amort/control/adj{int(adj)}',
            'kwargs': {
                'loan_amount': CONTROL_LOAN,
                'annual_rate': CONTROL_RATE,
                'first_payment_date': next_dt,
                'last_payment_date': last_dt,
                'adjust_business_days': adj,
                'prev_payment_date': CONTROL_FIRST,
                'fixed_payment': CONTROL_PAYMENT,
            },
            'meta': {'term': len(dates), 'kind': 'control'},
        })
    return cases


# ---------------------------------------------------------------------------
# simulate_lump_repayment
# ---------------------------------------------------------------------------

def lump_cases():
    """
    Оси: loan × rate × lump × дата досрочки × mode × adjust × first_date
         (короткий срок) + контрольный длинный договор.
    """
    cases = []
    for loan in LOANS:
        for rate in RATES:
            for first_dt in FIRST_DATES:
                next_dt, last_dt, dates = grid(first_dt, SHORT_TERM)
                payment = annuity(loan, rate, len(dates))
                for lump in LUMPS:
                    for kind in LUMP_DATE_KINDS:
                        at = lump_date_for(kind, first_dt, dates)
                        for mode in MODES:
                            for adj in ADJUST:
                                cases.append({
                                    'id': (f'lump/short/{int(loan)}/{rate}/{_tag(first_dt)}'
                                           f'/{int(lump)}/{kind}/{mode}/adj{int(adj)}'),
                                    'kwargs': {
                                        'loan_amount': loan,
                                        'annual_rate': rate,
                                        'first_payment_date': next_dt,
                                        'last_payment_date': last_dt,
                                        'monthly_payment': payment,
                                        'lump_sum': lump,
                                        'lump_date': at,
                                        'mode': mode,
                                        'adjust_business_days': adj,
                                        'prev_payment_date': first_dt,
                                    },
                                    'meta': {'term': len(dates), 'kind': 'short',
                                             'lump_date_kind': kind},
                                })

    # Длинный контрольный договор — на нём замерены числа роадмапа
    # (досрочка 500 000 на 17.04.2026 → проценты периода 18 082.19 сегодня).
    next_dt, last_dt, dates = grid(CONTROL_FIRST, LONG_TERM)
    for lump in LUMPS:
        for kind in LUMP_DATE_KINDS:
            at = lump_date_for(kind, CONTROL_FIRST, dates)
            for mode in MODES:
                for adj in ADJUST:
                    cases.append({
                        'id': f'lump/long/{int(lump)}/{kind}/{mode}/adj{int(adj)}',
                        'kwargs': {
                            'loan_amount': CONTROL_LOAN,
                            'annual_rate': CONTROL_RATE,
                            'first_payment_date': next_dt,
                            'last_payment_date': last_dt,
                            'monthly_payment': CONTROL_PAYMENT,
                            'lump_sum': lump,
                            'lump_date': at,
                            'mode': mode,
                            'adjust_business_days': adj,
                            'prev_payment_date': CONTROL_FIRST,
                        },
                        'meta': {'term': len(dates), 'kind': 'long',
                                 'lump_date_kind': kind},
                    })

    # Точный кейс из CHANGELOG 0.2.0: 3 000 000 ₽ @ 8 %, досрочка 500 000 на 17.04.2026.
    next_dt, last_dt, dates = grid(CONTROL_FIRST, LONG_TERM)
    for mode in MODES:
        for adj in ADJUST:
            cases.append({
                'id': f'lump/changelog/{mode}/adj{int(adj)}',
                'kwargs': {
                    'loan_amount': 3_000_000.0,
                    'annual_rate': 8.0,
                    'first_payment_date': next_dt,
                    'last_payment_date': last_dt,
                    'monthly_payment': 23_124.77,
                    'lump_sum': 500_000.0,
                    'lump_date': datetime(2026, 4, 17),
                    'mode': mode,
                    'adjust_business_days': adj,
                    'prev_payment_date': CONTROL_FIRST,
                },
                'meta': {'term': len(dates), 'kind': 'control',
                         'lump_date_kind': 'between'},
            })
    return cases


# ---------------------------------------------------------------------------
# calc_repayment_schedule (снежный ком)
# ---------------------------------------------------------------------------

def snowball_cases():
    """
    Оси: loan × rate × lump × budget × monthly_extra_day × first_date
         (короткий срок, lump_idx=0, monthly_idx=0)
         + сдвинутые индексы + длинный договор, включая конфигурацию
         известного бага min(annuity, budget) из роадмапа.

    calc_repayment_schedule сама делает next_dt = first_payment_date + 1 месяц,
    поэтому сюда идёт ИСХОДНАЯ дата первого платежа, а не сдвинутая.
    """
    cases = []
    for loan in LOANS:
        for rate in RATES:
            for first_dt in FIRST_DATES:
                _next, last_dt, _dates = grid(first_dt, SHORT_TERM)
                for lump in LUMPS:
                    for budget in BUDGETS:
                        for extra_day in EXTRA_DAYS:
                            cases.append({
                                'id': (f'snow/short/{int(loan)}/{rate}/{_tag(first_dt)}'
                                       f'/{int(lump)}/{int(budget or 0)}/{extra_day}'),
                                'kwargs': {
                                    'loan_amount': loan,
                                    'annual_rate': rate,
                                    'first_payment_date': first_dt,
                                    'last_payment_date': last_dt,
                                    'lump_sum': lump,
                                    'lump_idx': 0,
                                    'monthly_budget': budget,
                                    'monthly_idx': 0,
                                    'monthly_extra_day': extra_day,
                                },
                                'meta': {'term': SHORT_TERM, 'kind': 'short'},
                            })

    # Сдвинутые индексы: досрочка не в первом месяце, снежок стартует позже.
    _next, last_dt, _dates = grid(FIRST_DATES[0], SHORT_TERM)
    for budget in BUDGETS:
        for extra_day in EXTRA_DAYS:
            cases.append({
                'id': f'snow/idx/{int(budget or 0)}/{extra_day}',
                'kwargs': {
                    'loan_amount': 3_000_000.0,
                    'annual_rate': 7.99,
                    'first_payment_date': FIRST_DATES[0],
                    'last_payment_date': last_dt,
                    'lump_sum': 500_000.0,
                    'lump_idx': 6,
                    'monthly_budget': budget,
                    'monthly_idx': 3,
                    'monthly_extra_day': extra_day,
                },
                'meta': {'term': SHORT_TERM, 'kind': 'short_shifted'},
            })

    # Длинный договор: контрольный + конфигурация известного бага
    # (8 000 000 @ 16 %, budget 40k, extra_day 15 → Σ principal = 0.00).
    _next, last_dt, _dates = grid(CONTROL_FIRST, LONG_TERM)
    for loan, rate, tag in ((CONTROL_LOAN, CONTROL_RATE, 'control'),
                            (8_000_000.0, 16.0, 'knownbug')):
        for budget in BUDGETS:
            for extra_day in EXTRA_DAYS:
                cases.append({
                    'id': f'snow/long/{tag}/{int(budget or 0)}/{extra_day}',
                    'kwargs': {
                        'loan_amount': loan,
                        'annual_rate': rate,
                        'first_payment_date': CONTROL_FIRST,
                        'last_payment_date': last_dt,
                        'lump_sum': 0.0,
                        'lump_idx': 0,
                        'monthly_budget': budget,
                        'monthly_idx': 0,
                        'monthly_extra_day': extra_day,
                    },
                    'meta': {'term': LONG_TERM, 'kind': 'long'},
                })
    return cases


ALL_SUITES = {
    'build_amortization': amortization_cases,
    'simulate_lump_repayment': lump_cases,
    'calc_repayment_schedule': snowball_cases,
}


if __name__ == '__main__':
    for name, fn in ALL_SUITES.items():
        cases = fn()
        ids = {c['id'] for c in cases}
        assert len(ids) == len(cases), f'{name}: дублирующиеся id'
        print(f'{name}: {len(cases)} кейсов')
