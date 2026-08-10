"""
Приёмка Итерации 1 роадмапа — событийный движок ``web/app/engine.py``.

Тесты написаны ПО ЗАФИКСИРОВАННОМУ КОНТРАКТУ и не заглядывают внутрь движка:
импортируются только публичные имена (``MortgageState`` / ``RepaymentEvent`` /
``SimOptions`` / ``StrategyResult`` / ``payment_grid`` / ``simulate_strategy``)
и константы базы начисления. Приватные помощники (``_accrue`` и прочие)
намеренно не трогаются — иначе тест начнёт защищать реализацию, а не поведение.

Что проверяется (по разделу «Итерация 1 → Готово, когда»):

* контрольный договор без событий — 3 916 570.47 при ``basis='monthly'``
  и 3 930 963.06 при ``basis='daily'``;
* Σ ``interest`` по строкам графика == ``total_interest`` на всей матрице И0;
* ``total_interest > 0`` для входов, закрывающихся раньше ``dates[-1]``,
  и проценты последнего (обрезанного досрочкой) периода не теряются;
* лечебный кейс: досрочка в дату платежа не уменьшает проценты этого месяца,
  строка досрочки несёт ``interest == 0``;
* сетка дат единственная: ``payment_grid`` == ``StrategyResult.dates`` == даты
  строк графика, в том числе на T, выпадающих на выходные;
* неприменённое событие дренируется в ``lump_unused`` и даёт
  ``status == 'not_applicable'``, а не теряется молча;
* строковый ``prev_payment_date`` не роняет расчёт (сегодня в обёртке
  ``AttributeError`` при ``adjust_business_days=True``).

Пока ``web/app/engine.py`` не существует, весь файл уходит в skip: движок,
обёртки и тесты пишутся одновременно, и ``unittest discover`` обязан оставаться
зелёным независимо от того, кто финишировал первым.

Запуск::

    PYTHONPATH=web python -m unittest discover -s tests
    ENGINE_MATRIX_STRIDE=7 PYTHONPATH=web python -m unittest discover -s tests
"""
import json
import os
import unittest
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from matrix import (
    CONTROL_FIRST,
    CONTROL_LOAN,
    CONTROL_PAYMENT,
    CONTROL_RATE,
    FIRST_DATES,
    LONG_TERM,
    SHORT_TERM,
    amortization_cases,
    annuity,
    grid,
    lump_cases,
)

# ---------------------------------------------------------------------------
# Мягкий импорт движка: его может ещё не быть
# ---------------------------------------------------------------------------

try:
    import app.engine as ENGINE
except ImportError:                      # pragma: no cover — ветка «И1 ещё не сдана»
    ENGINE = None

SKIP_REASON = 'engine.py ещё не готов (И1)'

CONTRACT_NAMES = (
    'BASIS_MONTHLY', 'BASIS_DAILY',
    'ALLOC_PRINCIPAL_ONLY', 'ALLOC_INTEREST_FIRST',
    'ROW_ANNUITY', 'ROW_EARLY',
    'MortgageState', 'RepaymentEvent', 'SimOptions', 'StrategyResult',
    'payment_grid', 'simulate_strategy',
)

MISSING_NAMES = [] if ENGINE is None else [n for n in CONTRACT_NAMES if not hasattr(ENGINE, n)]


def _sym(name, fallback=None):
    """Имя из движка либо заглушка — чтобы модуль импортировался и без engine.py."""
    return fallback if ENGINE is None else getattr(ENGINE, name, fallback)


# Значения-заглушки равны контрактным литералам: они нужны только для того,
# чтобы модуль собрался, когда движка нет. Соответствие настоящих констант
# контракту проверяет EngineContractTest.
BASIS_MONTHLY = _sym('BASIS_MONTHLY', 'monthly')
BASIS_DAILY = _sym('BASIS_DAILY', 'daily')
ALLOC_PRINCIPAL_ONLY = _sym('ALLOC_PRINCIPAL_ONLY', 'principal_only')
ALLOC_INTEREST_FIRST = _sym('ALLOC_INTEREST_FIRST', 'interest_first')
ROW_ANNUITY = _sym('ROW_ANNUITY', 'annuity')
ROW_EARLY = _sym('ROW_EARLY', 'early')

MortgageState = _sym('MortgageState')
RepaymentEvent = _sym('RepaymentEvent')
SimOptions = _sym('SimOptions')
StrategyResult = _sym('StrategyResult')
payment_grid = _sym('payment_grid')
simulate_strategy = _sym('simulate_strategy')

# Ключи строки графика до И2 (порядок фиксирован контрактом) и добавленные на И2.
LEGACY_ROW_KEYS = ('payment_num', 'date', 'payment', 'principal', 'interest', 'balance', 'early')
I2_ROW_KEYS = ('row_kind', 'early_interest')

# Прореживание матрицы для быстрых локальных прогонов.
STRIDE = max(int(os.environ.get('ENGINE_MATRIX_STRIDE', '1')), 1)

_CENT = Decimal('0.01')


# ---------------------------------------------------------------------------
# Общие помощники (используются и tests/test_early_repayment_allocation.py)
# ---------------------------------------------------------------------------

def d(value):
    return Decimal(str(value))


def r2(value):
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def sample(cases):
    """Детерминированное прореживание матрицы через ENGINE_MATRIX_STRIDE."""
    return cases if STRIDE == 1 else cases[::STRIDE]


def basis_of(kwargs):
    """Решение 4 роадмапа: база фиксируется из adjust_business_days, и только из него."""
    return BASIS_DAILY if kwargs.get('adjust_business_days') else BASIS_MONTHLY


def state_of(kwargs):
    """MortgageState из kwargs матрицы (build_amortization или simulate_lump_repayment)."""
    contract_payment = kwargs.get('fixed_payment')
    if contract_payment is None:
        contract_payment = kwargs.get('monthly_payment')
    return MortgageState(
        loan_amount=kwargs['loan_amount'],
        annual_rate=kwargs['annual_rate'],
        first_payment_date=kwargs['first_payment_date'],
        last_payment_date=kwargs['last_payment_date'],
        prev_payment_date=kwargs.get('prev_payment_date'),
        contract_payment=contract_payment,
    )


def events_of(kwargs, allocation=None):
    """Список событий из kwargs матрицы: пусто либо одна разовая досрочка."""
    amount = kwargs.get('lump_sum') or 0
    if not amount:
        return []
    return [RepaymentEvent(
        amount=amount,
        at=kwargs.get('lump_date'),
        mode=kwargs.get('mode') or 'reduce_payment',
        allocation=allocation,
    )]


def opts_of(kwargs, allocation=None):
    """SimOptions из kwargs; allocation=None — дефолт движка (обязан быть principal_only)."""
    if allocation is None:
        return SimOptions(basis=basis_of(kwargs))
    return SimOptions(basis=basis_of(kwargs), allocation=allocation)


def run_case(kwargs, allocation=None, event_allocation=None):
    """Прогон кейса матрицы через движок."""
    return simulate_strategy(
        state_of(kwargs),
        events_of(kwargs, allocation=event_allocation),
        opts_of(kwargs, allocation=allocation),
    )


def fingerprint(result):
    """
    Побайтовый отпечаток результата: график целиком (с порядком ключей) плюс
    скаляры. Именно его сравнивают проверки «побайтово равно».
    """
    return json.dumps({
        'schedule': result.schedule,
        'total_interest': result.total_interest,
        'monthly_payment': result.monthly_payment,
        'annuity_months': result.annuity_months,
        'months_to_payoff': result.months_to_payoff,
        'dates': [dt.strftime('%d.%m.%Y') for dt in result.dates],
        'lump_unused': getattr(result, 'lump_unused', 0.0),
        'status': getattr(result, 'status', 'ok'),
        'carried_interest': getattr(result, 'carried_interest', 0.0),
    }, ensure_ascii=False, sort_keys=False)


def is_early_row(row):
    """Строка досрочки: по row_kind, если он уже есть, иначе по ненулевому early."""
    kind = row.get('row_kind')
    if kind is not None:
        return kind == ROW_EARLY
    return float(row.get('early') or 0) > 0


def is_annuity_row(row):
    return not is_early_row(row)


def early_rows(result):
    return [r for r in result.schedule if is_early_row(r)]


def row_by_date(result, date_str):
    """Первая аннуитетная строка с заданной датой."""
    for row in result.schedule:
        if is_annuity_row(row) and row['date'] == date_str:
            return row
    return None


def period_interest(result, end_date_str):
    """
    Σ процентов ВСЕХ строк периода, закрывающегося аннуитетом в end_date_str
    (строка досрочки внутри периода входит в сумму).
    """
    bucket = Decimal('0')
    for row in result.schedule:
        bucket += d(row['interest'])
        if is_annuity_row(row):
            if row['date'] == end_date_str:
                return float(r2(bucket))
            bucket = Decimal('0')
    raise AssertionError(f'в графике нет аннуитетной строки на {end_date_str}')


def accrue(balance, annual_rate, basis, days, period_days):
    """
    Проценты за отрезок в ``days`` дней внутри периода длиной ``period_days``.

    ``daily``   — balance * rate/365 * days;
    ``monthly`` — доля месячных процентов по дням (решения 3 и 4: смешивать базы
                  нельзя, месячная ставка не умножается на дневной остаток).
    """
    bal, rate = d(balance), d(annual_rate) / d(100)
    if basis == BASIS_DAILY:
        return float(r2(bal * rate / d(365) * d(days)))
    return float(r2(bal * rate / d(12) * d(days) / d(period_days)))


def control_state(loan=None, rate=None, payment=None):
    """Контрольный договор роадмапа: 299 платежей, 02.05.2026 → 02.03.2051."""
    next_dt, last_dt, _dates = grid(CONTROL_FIRST, LONG_TERM)
    return MortgageState(
        loan_amount=CONTROL_LOAN if loan is None else loan,
        annual_rate=CONTROL_RATE if rate is None else rate,
        first_payment_date=next_dt,
        last_payment_date=last_dt,
        prev_payment_date=CONTROL_FIRST,
        contract_payment=CONTROL_PAYMENT if payment is None else payment,
    )


class MoneyMixin:
    """Сравнения в рублях: копейка — значимая величина, tolerance не резиновый."""

    def assertMoney(self, actual, expected, msg='', delta=0.005):
        self.assertAlmostEqual(
            float(actual), float(expected), delta=delta,
            msg=f'{msg} (ожидалось {expected:.2f}, получено {float(actual):.2f})',
        )


# ---------------------------------------------------------------------------
# Контракт модуля
# ---------------------------------------------------------------------------

@unittest.skipIf(ENGINE is None, SKIP_REASON)
class EngineContractTest(unittest.TestCase):
    """Имена, значения констант и дефолты датаклассов — до того, как считать деньги."""

    def test_contract_names_present(self):
        self.assertEqual(
            MISSING_NAMES, [],
            'web/app/engine.py не отдаёт имена зафиксированного контракта: '
            + ', '.join(MISSING_NAMES),
        )

    def test_constant_values(self):
        self.assertEqual(ENGINE.BASIS_MONTHLY, 'monthly')
        self.assertEqual(ENGINE.BASIS_DAILY, 'daily')
        self.assertEqual(ENGINE.ALLOC_PRINCIPAL_ONLY, 'principal_only')
        self.assertEqual(ENGINE.ALLOC_INTEREST_FIRST, 'interest_first')
        self.assertEqual(ENGINE.ROW_ANNUITY, 'annuity')
        self.assertEqual(ENGINE.ROW_EARLY, 'early')

    def test_sim_options_defaults(self):
        opts = SimOptions()
        self.assertEqual(opts.basis, BASIS_MONTHLY, 'дефолтная база — monthly')
        self.assertEqual(opts.allocation, ALLOC_PRINCIPAL_ONLY,
                         'дефолт аллокации — principal_only, инвариант 3ca4b3e')

    def test_repayment_event_defaults(self):
        ev = RepaymentEvent(amount=100_000.0)
        self.assertIsNone(ev.at)
        self.assertEqual(ev.kind, 'lump')
        self.assertEqual(ev.mode, 'reduce_payment', 'режим живёт на событии')
        self.assertIsNone(ev.allocation, 'None на событии → берётся из SimOptions')

    def test_mortgage_state_optional_fields(self):
        state = MortgageState(
            loan_amount=1_000_000.0,
            annual_rate=8.0,
            first_payment_date=datetime(2026, 5, 2),
            last_payment_date=datetime(2028, 4, 2),
        )
        self.assertIsNone(state.prev_payment_date)
        self.assertIsNone(state.contract_payment)

    def test_row_key_order(self):
        """Первые семь ключей строки — легаси-порядок; лишние — только ключи И2."""
        result = simulate_strategy(control_state(), [], SimOptions(basis=BASIS_MONTHLY))
        keys = tuple(result.schedule[0].keys())
        self.assertEqual(keys[:len(LEGACY_ROW_KEYS)], LEGACY_ROW_KEYS,
                         'порядок ключей строки графика зафиксирован контрактом')
        extra = keys[len(LEGACY_ROW_KEYS):]
        self.assertEqual(
            extra, tuple(k for k in I2_ROW_KEYS if k in extra),
            'после легаси-ключей допустимы только row_kind и early_interest, '
            f'в этом порядке; получено {extra}',
        )


# ---------------------------------------------------------------------------
# Контрольный договор без событий
# ---------------------------------------------------------------------------

@unittest.skipIf(ENGINE is None, SKIP_REASON)
class ControlContractTest(MoneyMixin, unittest.TestCase):
    """
    2 995 218.84 ₽, 7.99 %, первый предстоящий платёж 02.05.2026, 299 платежей,
    договорной платёж 23 124.77 ₽ (прошлый платёж 02.04.2026).
    """

    EXPECTED = {'monthly': 3_916_570.47, 'daily': 3_930_963.06}

    def _run(self, basis):
        return simulate_strategy(control_state(), [], SimOptions(basis=basis))

    def test_total_interest_monthly(self):
        result = self._run(BASIS_MONTHLY)
        self.assertMoney(result.total_interest, self.EXPECTED['monthly'],
                         'контрольный договор, basis=monthly')

    def test_total_interest_daily(self):
        result = self._run(BASIS_DAILY)
        self.assertMoney(result.total_interest, self.EXPECTED['daily'],
                         'контрольный договор, basis=daily')

    def test_shape(self):
        for basis in (BASIS_MONTHLY, BASIS_DAILY):
            with self.subTest(basis=basis):
                result = self._run(basis)
                self.assertEqual(len(result.dates), LONG_TERM)
                self.assertEqual(result.annuity_months, LONG_TERM,
                                 'без досрочек все 299 платежей — плановые')
                self.assertEqual(result.months_to_payoff, LONG_TERM)
                self.assertEqual(len(result.schedule), LONG_TERM)
                self.assertMoney(result.monthly_payment, CONTROL_PAYMENT,
                                 'без событий платёж равен договорному')
                self.assertMoney(result.schedule[-1]['balance'], 0.0, 'кредит закрыт')
                self.assertEqual(getattr(result, 'status', 'ok'), 'ok')
                self.assertMoney(getattr(result, 'lump_unused', 0.0), 0.0,
                                 'событий не было — дренировать нечего')

    def test_events_empty_matches_amortization_matrix(self):
        """
        Ветка build_amortization целиком: движок с events=[] обязан давать те же
        числа, что и обёртка, — паритет по всей матрице И0 (решение 14).
        """
        from app.calculator import build_amortization

        for case in sample(amortization_cases()):
            with self.subTest(case=case['id']):
                result = run_case(case['kwargs'])
                schedule, first_payment, total = build_amortization(**case['kwargs'])
                self.assertMoney(result.total_interest, total, case['id'])
                self.assertEqual(len(result.schedule), len(schedule), case['id'])
                self.assertMoney(result.schedule[0]['payment'], first_payment, case['id'])


# ---------------------------------------------------------------------------
# Сохранение процентов: Σ по строкам и последний период
# ---------------------------------------------------------------------------

@unittest.skipIf(ENGINE is None, SKIP_REASON)
class InterestConservationTest(MoneyMixin, unittest.TestCase):

    def test_sum_of_rows_equals_total_interest(self):
        """Σ interest по строкам == total_interest на всей матрице И0."""
        cases = sample(amortization_cases()) + sample(lump_cases())
        worst = None
        for case in cases:
            result = run_case(case['kwargs'])
            rows_sum = float(r2(sum((d(r['interest']) for r in result.schedule), Decimal('0'))))
            delta = abs(rows_sum - result.total_interest)
            if worst is None or delta > worst[0]:
                worst = (delta, case['id'], rows_sum, result.total_interest)
        self.assertIsNotNone(worst)
        delta, case_id, rows_sum, total = worst
        self.assertLessEqual(
            delta, 0.005,
            f'{case_id}: Σ interest по строкам {rows_sum:.2f} != total_interest {total:.2f}',
        )

    def test_total_interest_positive_when_closing_early(self):
        """
        Вход, закрывающийся раньше dates[-1], обязан унести с собой проценты
        последнего (обрезанного) периода. Исключение — закрытие ровно в дату
        якоря: там времени не прошло и нулевые проценты законны.
        """
        checked = 0
        for case in sample(lump_cases()):
            kwargs = case['kwargs']
            if not kwargs.get('lump_sum'):
                continue
            result = run_case(kwargs)
            last_date = result.schedule[-1]['date']
            if last_date == result.dates[-1].strftime('%d.%m.%Y'):
                continue                                     # дошли до конца графика
            _dates, anchor = payment_grid(state_of(kwargs), basis_of(kwargs))
            if datetime.strptime(last_date, '%d.%m.%Y') <= anchor:
                continue                                     # закрылись в дату якоря
            checked += 1
            with self.subTest(case=case['id']):
                self.assertGreater(
                    result.total_interest, 0.0,
                    f'{case["id"]}: кредит закрыт {last_date}, а процентов начислено 0 — '
                    'потерян последний период',
                )
        self.assertGreater(checked, 0, 'в матрице не нашлось досрочно закрывающихся входов')

    def test_closing_period_interest_is_not_dropped(self):
        """
        Досрочка обнуляет остаток внутри периода: проценты от предыдущего платежа
        до даты досрочки обязаны быть предъявлены. Ожидание считается, а не
        берётся литералом: baseline-проценты первого периода плюс начисление на
        остаток после первого аннуитета за дни до досрочки.

        Вход: 1 000 000 ₽ @ 16 %, досрочка 2 000 000 ₽ внутри второго периода.
        """
        first_dt = FIRST_DATES[0]
        next_dt, last_dt, dates = grid(first_dt, SHORT_TERM)
        payment = annuity(1_000_000.0, 16.0, len(dates))
        lump_at = dates[0] + (dates[1] - dates[0]) // 2

        for basis in (BASIS_MONTHLY, BASIS_DAILY):
            with self.subTest(basis=basis):
                kwargs = {
                    'loan_amount': 1_000_000.0,
                    'annual_rate': 16.0,
                    'first_payment_date': next_dt,
                    'last_payment_date': last_dt,
                    'monthly_payment': payment,
                    'lump_sum': 2_000_000.0,
                    'lump_date': lump_at,
                    'mode': 'reduce_term',
                    'adjust_business_days': basis == BASIS_DAILY,
                }
                base = run_case({**kwargs, 'lump_sum': 0.0, 'lump_date': None})
                actual = run_case(kwargs)

                grid_dates, _anchor = payment_grid(state_of(kwargs), basis)
                period_days = (grid_dates[1] - grid_dates[0]).days
                days_to_lump = (lump_at - grid_dates[0]).days
                self.assertGreater(days_to_lump, 0, 'досрочка обязана попасть внутрь периода')

                balance_after_first = base.schedule[0]['balance']
                tail_prorata = accrue(balance_after_first, 16.0, basis,
                                      days_to_lump, period_days)
                tail_full = accrue(balance_after_first, 16.0, basis,
                                   period_days, period_days)
                expected_min = base.schedule[0]['interest'] + tail_prorata
                expected_max = base.schedule[0]['interest'] + tail_full

                total = actual.total_interest
                self.assertGreaterEqual(
                    total, expected_min - 0.01,
                    f'проценты обрезанного периода потеряны: получено {total:.2f}, '
                    f'минимум {expected_min:.2f} (аннуитет {base.schedule[0]["interest"]:.2f} + '
                    f'{days_to_lump} дн. на остатке {balance_after_first:.2f})',
                )
                self.assertLessEqual(
                    total, expected_max + 0.01,
                    f'начислено больше целого периода: {total:.2f} > {expected_max:.2f}',
                )


# ---------------------------------------------------------------------------
# Лечебный кейс: досрочка в дату планового платежа
# ---------------------------------------------------------------------------

@unittest.skipIf(ENGINE is None, SKIP_REASON)
class LumpOnPaymentDateTest(MoneyMixin, unittest.TestCase):
    """
    Инвариант коммита 3ca4b3e: досрочка в дату платежа применяется ПОСЛЕ
    аннуитета, поэтому проценты этого месяца равны baseline-процентам того же
    месяца копейка-в-копейку, а строка досрочки несёт interest == 0.
    """

    LOAN, RATE, PAYMENT, LUMP = 3_000_000.0, 8.0, 23_124.77, 500_000.0

    def _kwargs(self, basis, lump_date, lump=None):
        next_dt, last_dt, _dates = grid(CONTROL_FIRST, LONG_TERM)
        return {
            'loan_amount': self.LOAN,
            'annual_rate': self.RATE,
            'first_payment_date': next_dt,
            'last_payment_date': last_dt,
            'monthly_payment': self.PAYMENT,
            'lump_sum': self.LUMP if lump is None else lump,
            'lump_date': lump_date,
            'mode': 'reduce_term',
            'adjust_business_days': basis == BASIS_DAILY,
        }

    def test_interest_of_that_month_equals_baseline(self):
        for basis in (BASIS_MONTHLY, BASIS_DAILY):
            with self.subTest(basis=basis):
                state = control_state(self.LOAN, self.RATE, self.PAYMENT)
                dates, _anchor = payment_grid(state, basis)
                lump_date = dates[1]

                base = run_case(self._kwargs(basis, None, lump=0.0))
                actual = run_case(self._kwargs(basis, lump_date))

                date_str = lump_date.strftime('%d.%m.%Y')
                base_row = row_by_date(base, date_str)
                row = row_by_date(actual, date_str)
                self.assertIsNotNone(base_row, f'baseline без строки на {date_str}')
                self.assertIsNotNone(row, f'график досрочки без аннуитета на {date_str}')
                self.assertMoney(
                    row['interest'], base_row['interest'],
                    f'{date_str}: проценты месяца досрочки разошлись с baseline',
                )
                # И первый месяц, и все предыдущие обязаны совпасть с baseline целиком.
                idx = actual.schedule.index(row)
                for i in range(idx + 1):
                    self.assertMoney(actual.schedule[i]['interest'],
                                     base.schedule[i]['interest'],
                                     f'строка {i + 1} до досрочки разошлась с baseline')

    def test_early_row_carries_no_interest(self):
        for basis in (BASIS_MONTHLY, BASIS_DAILY):
            with self.subTest(basis=basis):
                state = control_state(self.LOAN, self.RATE, self.PAYMENT)
                dates, _anchor = payment_grid(state, basis)
                actual = run_case(self._kwargs(basis, dates[1]))

                rows = early_rows(actual)
                self.assertEqual(len(rows), 1, 'ровно одна строка досрочки')
                row = rows[0]
                self.assertEqual(row['date'], dates[1].strftime('%d.%m.%Y'))
                self.assertMoney(row['interest'], 0.0,
                                 'principal_only: досрочка не платит проценты')
                self.assertMoney(row['principal'], self.LUMP, 'вся сумма ушла в тело')
                self.assertMoney(row['payment'], self.LUMP)
                if 'row_kind' in row:
                    self.assertEqual(row['row_kind'], ROW_EARLY)
                    self.assertMoney(row.get('early_interest', 0.0), 0.0,
                                     'early_interest в principal_only равен нулю')

    def test_early_row_goes_after_the_annuity(self):
        """Порядок строк: аннуитет этой даты, затем досрочка — не наоборот."""
        for basis in (BASIS_MONTHLY, BASIS_DAILY):
            with self.subTest(basis=basis):
                state = control_state(self.LOAN, self.RATE, self.PAYMENT)
                dates, _anchor = payment_grid(state, basis)
                actual = run_case(self._kwargs(basis, dates[1]))
                date_str = dates[1].strftime('%d.%m.%Y')
                same_date = [i for i, r in enumerate(actual.schedule) if r['date'] == date_str]
                self.assertEqual(len(same_date), 2, f'на {date_str} ожидались аннуитет и досрочка')
                self.assertTrue(is_annuity_row(actual.schedule[same_date[0]]))
                self.assertTrue(is_early_row(actual.schedule[same_date[1]]))


# ---------------------------------------------------------------------------
# Единственная сетка дат
# ---------------------------------------------------------------------------

@unittest.skipIf(ENGINE is None, SKIP_REASON)
class PaymentGridTest(unittest.TestCase):
    """
    Дата вливания вклада берётся из той же сетки, что и график. Проверяется на
    T = {1, 2, 4, 13}: 02.05.2026 — суббота, 02.08.2026 и 02.05.2027 — воскресенья,
    то есть при basis='daily' сдвиг обязан состояться и попасть в обе стороны.
    """

    TERMS = (1, 2, 4, 13)

    def test_grid_matches_schedule_dates(self):
        for basis in (BASIS_MONTHLY, BASIS_DAILY):
            with self.subTest(basis=basis):
                state = control_state()
                dates, anchor = payment_grid(state, basis)
                result = simulate_strategy(state, [], SimOptions(basis=basis))

                self.assertEqual(len(dates), LONG_TERM)
                self.assertEqual(list(result.dates), list(dates),
                                 'StrategyResult.dates обязан быть той же сеткой')
                self.assertIsNotNone(anchor, 'payment_grid обязан вернуть якорь начисления')

                for T in self.TERMS:
                    self.assertEqual(
                        result.schedule[T - 1]['date'], dates[T - 1].strftime('%d.%m.%Y'),
                        f'T={T}: дата строки графика разошлась с сеткой',
                    )

    def test_daily_basis_shifts_weekend_dates(self):
        """Без реального сдвига проверка выше ничего не стоила бы."""
        _next_dt, _last_dt, raw_dates = grid(CONTROL_FIRST, LONG_TERM)
        dates, _anchor = payment_grid(control_state(), BASIS_DAILY)

        shifted = [T for T in self.TERMS if dates[T - 1] != raw_dates[T - 1]]
        self.assertEqual(
            sorted(shifted), [1, 4, 13],
            'при basis=daily выходные T=1 (сб), T=4 и T=13 (вс) обязаны съехать '
            'на рабочий день, а T=2 (вт) — нет',
        )
        for T in shifted:
            self.assertLess(dates[T - 1].weekday(), 5, f'T={T}: сдвинули, но не на будни')

    def test_monthly_basis_keeps_raw_dates(self):
        _next_dt, _last_dt, raw_dates = grid(CONTROL_FIRST, LONG_TERM)
        dates, _anchor = payment_grid(control_state(), BASIS_MONTHLY)
        self.assertEqual(list(dates), list(raw_dates),
                         'basis=monthly не двигает даты (решение 4)')


# ---------------------------------------------------------------------------
# Дренаж неприменённых событий
# ---------------------------------------------------------------------------

@unittest.skipIf(ENGINE is None, SKIP_REASON)
class UnusedEventTest(MoneyMixin, unittest.TestCase):

    def _short_kwargs(self, **over):
        first_dt = FIRST_DATES[0]
        next_dt, last_dt, dates = grid(first_dt, SHORT_TERM)
        kwargs = {
            'loan_amount': 3_000_000.0,
            'annual_rate': 7.99,
            'first_payment_date': next_dt,
            'last_payment_date': last_dt,
            'monthly_payment': annuity(3_000_000.0, 7.99, len(dates)),
            'lump_sum': 500_000.0,
            'lump_date': None,
            'mode': 'reduce_term',
            'adjust_business_days': False,
            'prev_payment_date': first_dt,
        }
        kwargs.update(over)
        return kwargs, dates

    def test_event_past_the_end_is_drained(self):
        """Дата события за концом графика: сумма в lump_unused, статус not_applicable."""
        for basis in (BASIS_MONTHLY, BASIS_DAILY):
            with self.subTest(basis=basis):
                kwargs, dates = self._short_kwargs(
                    adjust_business_days=basis == BASIS_DAILY)
                kwargs['lump_date'] = dates[-1] + (dates[-1] - dates[-2])
                result = run_case(kwargs)

                self.assertMoney(result.lump_unused, kwargs['lump_sum'],
                                 'несостоявшееся событие обязано уйти в lump_unused целиком')
                self.assertEqual(result.status, 'not_applicable',
                                 'сценарий с несостоявшимся событием исключается из конкурса')
                self.assertEqual(early_rows(result), [], 'строк досрочки быть не должно')

                base = run_case({**kwargs, 'lump_sum': 0.0, 'lump_date': None})
                self.assertMoney(result.total_interest, base.total_interest,
                                 'несостоявшееся событие не меняет проценты')

    def test_applied_event_keeps_status_ok(self):
        """Состоявшаяся досрочка: статус ok, дренировать нечего."""
        kwargs, dates = self._short_kwargs()
        kwargs['lump_date'] = dates[1]
        result = run_case(kwargs)
        self.assertEqual(result.status, 'ok')
        self.assertMoney(result.lump_unused, 0.0)
        self.assertEqual(len(early_rows(result)), 1)

    def test_excess_is_reported_not_swallowed(self):
        """
        Досрочка больше остатка: применённая часть плюс lump_unused обязаны
        сойтись с суммой события (решение 6 — справочное поле, не метрика).
        """
        for basis in (BASIS_MONTHLY, BASIS_DAILY):
            with self.subTest(basis=basis):
                kwargs, _dates = self._short_kwargs(
                    loan_amount=1_000_000.0,
                    monthly_payment=annuity(1_000_000.0, 7.99, SHORT_TERM),
                    lump_sum=2_000_000.0,
                    adjust_business_days=basis == BASIS_DAILY,
                )
                result = run_case(kwargs)
                applied = float(r2(sum((d(r['early']) for r in result.schedule), Decimal('0'))))
                self.assertMoney(
                    applied + result.lump_unused, kwargs['lump_sum'],
                    f'{applied:.2f} применено + {result.lump_unused:.2f} не понадобилось',
                )
                self.assertGreater(result.lump_unused, 0.0,
                                   '2 млн на остаток 1 млн — излишек обязан быть виден')

    def test_conservation_over_matrix(self):
        """Σ early + lump_unused == сумма события — на всей матрице И0."""
        for case in sample(lump_cases()):
            amount = case['kwargs'].get('lump_sum') or 0
            if not amount:
                continue
            result = run_case(case['kwargs'])
            applied = float(r2(sum((d(r['early']) for r in result.schedule), Decimal('0'))))
            with self.subTest(case=case['id']):
                self.assertMoney(
                    applied + result.lump_unused, amount,
                    f'{case["id"]}: применено {applied:.2f}, не использовано '
                    f'{result.lump_unused:.2f}',
                )


# ---------------------------------------------------------------------------
# Мелочь по пути: строковый prev_payment_date
# ---------------------------------------------------------------------------

@unittest.skipIf(ENGINE is None, SKIP_REASON)
class StringPrevPaymentDateTest(MoneyMixin, unittest.TestCase):
    """
    Сегодня строковый prev_payment_date роняет расчёт: AttributeError
    ('str' object has no attribute 'weekday') при adjust_business_days=True
    и TypeError при сравнении с датой досрочки при False. И1 обязана это снять.
    """

    def _kwargs(self, prev, basis):
        first_dt = FIRST_DATES[0]
        next_dt, last_dt, dates = grid(first_dt, SHORT_TERM)
        return {
            'loan_amount': 3_000_000.0,
            'annual_rate': 7.99,
            'first_payment_date': next_dt,
            'last_payment_date': last_dt,
            'monthly_payment': annuity(3_000_000.0, 7.99, len(dates)),
            'lump_sum': 500_000.0,
            'lump_date': dates[0] + (dates[1] - dates[0]) // 2,
            'mode': 'reduce_term',
            'adjust_business_days': basis == BASIS_DAILY,
            'prev_payment_date': prev,
        }

    def test_engine_accepts_string_prev_date(self):
        first_dt = FIRST_DATES[0]
        for basis in (BASIS_MONTHLY, BASIS_DAILY):
            for text in (first_dt.strftime('%d.%m.%Y'), first_dt.strftime('%Y-%m-%d')):
                with self.subTest(basis=basis, prev=text):
                    expected = run_case(self._kwargs(first_dt, basis))
                    actual = run_case(self._kwargs(text, basis))
                    self.assertEqual(
                        fingerprint(actual), fingerprint(expected),
                        f'строковый prev_payment_date={text!r} обязан дать то же, '
                        'что и datetime',
                    )

    def test_wrapper_accepts_string_prev_date(self):
        """Та же мелочь на уровне обёртки simulate_lump_repayment (calculator.py:227)."""
        from app.calculator import simulate_lump_repayment

        first_dt = FIRST_DATES[0]
        for basis in (BASIS_MONTHLY, BASIS_DAILY):
            with self.subTest(basis=basis):
                kwargs = self._kwargs(first_dt.strftime('%d.%m.%Y'), basis)
                try:
                    _sched, _pay, total, _months = simulate_lump_repayment(**kwargs)
                except (AttributeError, TypeError) as exc:
                    self.fail(f'строковый prev_payment_date роняет обёртку: '
                              f'{type(exc).__name__}: {exc}')
                _s, _p, expected, _m = simulate_lump_repayment(
                    **{**kwargs, 'prev_payment_date': first_dt})
                self.assertMoney(total, expected, 'строка и datetime дали разные числа')


if __name__ == '__main__':
    unittest.main()
