"""
Приёмка Итерации 3 роадмапа — снежный ком на событийном движке.

Файл написан ПО СПЕКЕ («Итерация 3 → Готово, когда», подпункты 3a-1, 3a-2, 3a-3,
3b, 3c), а не по текущему коду. Классы названы по номерам коммитов роадмапа,
чтобы по выводу ``unittest`` было видно, какое именно исправление сломалось:

* ``SnowballInvariantTest`` — свойства, которые обязаны держаться и ДО, и ПОСЛЕ
  итерации (строки досрочки без процентов, снежок с нулевым бюджетом равен
  аннуитету);
* ``Snowball3aTest`` / ``Snowball3bTest`` / ``Snowball3cTest`` — приёмка трёх
  исправлений. Тесты НЕ скипаются, когда исправления ещё нет: недостающий
  параметр `calc_repayment_schedule` — это падение с перечислением
  фактической сигнатуры, а не тихий пропуск.

Числа, которые эти тесты ловили на ДО-И3 реализации снежка (проверено прогоном
на коде до правок; после И3 все они стали нулями):

    3a-1  снежок без бюджета и без досрочки   3 918 288,43 против 3 916 570,47
                                              у build_amortization  → Δ 1 717,96
    3a-2  снежок не знал adjust_business_days (параметра не было)   → Δ 14 392,59
          (снежок по месячной базе против базы по дневной: −12 674,63 на
           контрольном договоре)
    3a-3  monthly_extra_day переключал базу    3 923 725,29 против 3 918 288,43
          при недостижимом monthly_idx                              → Δ 5 436,86
    3b    budget=40k / 8 млн / 16 % / день 15  Σ principal = 0,00, остаток 8 млн
    3b    budget=60 000                        months_to_payoff = 121 при 61
                                               аннуитетной строке
    3c    lump_unused наружу не отдавался вовсе

Запуск::

    PYTHONPATH=web python -m unittest discover -s tests
    SNOWBALL_MATRIX_STRIDE=1 PYTHONPATH=web python -m unittest discover -s tests
"""
import dataclasses
import inspect
import os
import unittest
from decimal import Decimal

from dateutil.relativedelta import relativedelta

from app.calculator import build_amortization, calc_repayment_schedule

# Полный результат снежка (со `status` и `lump_unused`) — то, чем пользуется
# `run_comparison`. Трёхэлементная `calc_repayment_schedule` о неистраченных
# рублях сообщить не может в принципе, поэтому 3c проверяется здесь.
try:
    from app.calculator import simulate_snowball
except ImportError:                      # pragma: no cover — ветка «И3 не сдана»
    simulate_snowball = None
from matrix import (
    CONTROL_FIRST,
    CONTROL_LOAN,
    CONTROL_PAYMENT,
    CONTROL_RATE,
    FIRST_DATES,
    LOANS,
    LONG_TERM,
    RATES,
    SHORT_TERM,
    annuity,
    grid,
    snowball_cases,
)

# Движок нужен только одному тесту (recurring-событие). Его может не быть —
# тогда этот тест уходит в skip, а остальной файл работает.
try:
    import app.engine as ENGINE
except ImportError:                      # pragma: no cover — ветка «И1 не сдана»
    ENGINE = None

ENGINE_SKIP = 'app.engine ещё не готов (И1)'

# Каждый N-й кейс матрицы для инвариантных прогонов: полная матрица снежка —
# 2205 кейсов, в CI это лишние секунды. SNOWBALL_MATRIX_STRIDE=1 гоняет всё.
STRIDE = int(os.environ.get('SNOWBALL_MATRIX_STRIDE', '11'))

ROW_FIELDS = ('date', 'payment', 'principal', 'interest', 'balance', 'early', 'row_kind')

# Параметры, которые снежок обязан принимать после И3. Сегодня их нет —
# `_snow()` их отбрасывает и сообщает об этом тесту.
SNOW_PARAMS = frozenset(inspect.signature(calc_repayment_schedule).parameters)


# ---------------------------------------------------------------------------
# Помощники
# ---------------------------------------------------------------------------

def _c(value):
    """Число из графика — в Decimal. Сравнения идут в копейках, не во float."""
    return Decimal(str(value)).quantize(Decimal('0.01'))


def _snow(**kwargs):
    """
    Вызов снежка с отбрасыванием ещё не существующих параметров.

    Возвращает ``(result, dropped)``:
      * ``result`` — нормализованный ``_SnowResult``;
      * ``dropped`` — имена параметров, которых сигнатура пока не знает.

    Тест сам решает, что делать с `dropped`: приёмочные тесты И3 обязаны
    падать с внятным сообщением, инвариантные — работать и без них.
    """
    dropped = sorted(k for k in kwargs if k not in SNOW_PARAMS)
    call = {k: v for k, v in kwargs.items() if k in SNOW_PARAMS}
    return _unpack(calc_repayment_schedule(**call)), dropped


@dataclasses.dataclass
class _SnowResult:
    total_interest: float
    months_to_payoff: int
    schedule: list
    lump_unused: float = None       # None == «наружу не отдаётся»


def _unpack(raw):
    """
    Нормализация результата снежка.

    Сегодня это кортеж ``(total_interest, months_to_payoff, schedule)``.
    И3c обязана вынести наружу ``lump_unused``; форма расширения заранее не
    зафиксирована, поэтому принимаем и 4-й элемент кортежа, и объект-результат
    с атрибутами (как ``StrategyResult``).
    """
    if isinstance(raw, tuple):
        total_interest, months, schedule = raw[0], raw[1], raw[2]
        unused = raw[3] if len(raw) > 3 else None
        return _SnowResult(total_interest, months, schedule, unused)
    return _SnowResult(
        getattr(raw, 'total_interest'),
        getattr(raw, 'months_to_payoff', getattr(raw, 'annuity_months', None)),
        getattr(raw, 'schedule'),
        getattr(raw, 'lump_unused', None),
    )


def _baseline(loan, rate, first_dt, last_dt, payment, adj=False):
    """
    База сравнения: ``build_amortization`` на той же сетке, что строит снежок.

    Снежок получает ПРОШЕДШУЮ дату первого платежа и сам делает
    ``next_dt = first + 1 месяц``; ``build_amortization`` получает уже сдвинутую
    дату плюс ``prev_payment_date`` — сетки при этом совпадают.
    """
    next_dt = first_dt + relativedelta(months=1)
    schedule, _first, total_interest = build_amortization(
        loan, rate, next_dt, last_dt,
        adjust_business_days=adj, prev_payment_date=first_dt,
        fixed_payment=payment,
    )
    return schedule, total_interest


def _row(row):
    return tuple((f, _c(row[f]) if isinstance(row[f], (int, float)) else row[f])
                 for f in ROW_FIELDS)


def _sum_principal(schedule):
    return sum((_c(r['principal']) for r in schedule), Decimal('0'))


def _control_grid():
    """Контрольный длинный договор роадмапа: 02.05.2026 → 02.03.2051, 299 дат."""
    return grid(CONTROL_FIRST, LONG_TERM)


# ---------------------------------------------------------------------------
# Инварианты: держатся и до, и после И3
# ---------------------------------------------------------------------------

class SnowballInvariantTest(unittest.TestCase):
    """Свойства снежка, которые И3 обязана сохранить."""

    def test_early_rows_carry_no_interest(self):
        """
        Инвариант проекта (коммит 3ca4b3e): досрочка гасит только тело.

        Строка досрочки в снежке — это `row_kind == 'early'`; процентов на ней
        быть не может ни при каком бюджете и ни при каком дне доплаты.
        """
        cases = snowball_cases()[::STRIDE]
        self.assertGreater(len(cases), 50, 'матрица снежка неожиданно пуста')
        for case in cases:
            result, _dropped = _snow(**case['kwargs'])
            for row in result.schedule:
                if row.get('row_kind') != 'early':
                    continue
                self.assertEqual(
                    _c(row['interest']), Decimal('0.00'),
                    f"{case['id']}: строка досрочки {row['date']} несёт проценты "
                    f"{row['interest']} — инвариант 3ca4b3e нарушен",
                )
                self.assertEqual(
                    _c(row['early']), _c(row['principal']),
                    f"{case['id']}: строка досрочки {row['date']} — early != principal",
                )

    def test_early_rows_are_marked_by_row_kind(self):
        """
        Строка досрочки помечается движком, а не угадывается фронтом.

        Эвристика `early > 0 && interest == 0` перестаёт работать в режиме
        `interest_first` (И2), поэтому `row_kind` обязан приезжать из расчёта.
        """
        cases = snowball_cases()[::STRIDE]
        for case in cases:
            result, _dropped = _snow(**case['kwargs'])
            for row in result.schedule:
                self.assertIn(
                    row.get('row_kind'), ('annuity', 'early'),
                    f"{case['id']}: строка {row['date']} без row_kind",
                )

    @unittest.skipIf(ENGINE is None, ENGINE_SKIP)
    def test_recurring_with_zero_budget_equals_baseline(self):
        """
        `simulate_strategy(events=[recurring(budget=0)])` == `build_amortization`.

        Пункт «Готово, когда» И3. Нулевой бюджет не создаёт денежного потока,
        поэтому график обязан совпасть с аннуитетным построчно — включая случай,
        когда ветка 'recurring' в движке уже написана.
        """
        next_dt, last_dt, dates = _control_grid()
        base_schedule, base_total = _baseline(
            CONTROL_LOAN, CONTROL_RATE, CONTROL_FIRST, last_dt, CONTROL_PAYMENT,
        )

        state = ENGINE.MortgageState(
            loan_amount=CONTROL_LOAN,
            annual_rate=CONTROL_RATE,
            first_payment_date=next_dt,
            last_payment_date=last_dt,
            prev_payment_date=CONTROL_FIRST,
            contract_payment=CONTROL_PAYMENT,
        )
        # Бюджет — «всего готов платить в месяц» (решённый вопрос И3), поэтому
        # нулевой бюджет выражается через amount_kind='budget', если поле есть.
        event = _make_recurring(0.0, amount_kind='budget')
        result = ENGINE.simulate_strategy(
            state, [event], ENGINE.SimOptions(basis=ENGINE.BASIS_MONTHLY),
        )

        self.assertEqual(
            _c(result.total_interest), _c(base_total),
            'recurring с нулевым бюджетом изменил проценты: '
            f'{result.total_interest} против {base_total}',
        )
        self.assertEqual(len(result.schedule), len(base_schedule),
                         'recurring с нулевым бюджетом изменил число строк')
        for got, want in zip(result.schedule, base_schedule):
            self.assertEqual(_row(got), _row(want),
                             f"строка {got['date']} разошлась с аннуитетной")


def _make_recurring(amount, **extra):
    """
    Событие ``kind='recurring'`` по фактическому набору полей ``RepaymentEvent``.

    Контракт recurring дописывается на И3; лишние поля отбрасываются, чтобы тест
    не привязывался к именам, которых ещё нет.
    """
    fields = {f.name for f in dataclasses.fields(ENGINE.RepaymentEvent)}
    kwargs = {'amount': amount}
    if 'kind' in fields:
        kwargs['kind'] = 'recurring'
    kwargs.update({k: v for k, v in extra.items() if k in fields})
    return ENGINE.RepaymentEvent(**kwargs)


# ---------------------------------------------------------------------------
# 3a — обёртка снежка над движком: три исправления
# ---------------------------------------------------------------------------

class Snowball3aTest(unittest.TestCase):
    """
    Приёмка 3a-1 / 3a-2 / 3a-3. На старом коде падает — так и задумано.

    Каждый тест сначала проверяет, что снежок вообще принимает нужный параметр
    (`contract_payment`, `adjust_business_days`, `mode`), и печатает измеренную
    дельту, чтобы по протоколу было видно не только «красный», но и «на сколько».
    """

    def _require(self, dropped, commit):
        if dropped:
            self.fail(f'{commit}: calc_repayment_schedule ещё не принимает {dropped} — '
                      f'сигнатура {sorted(SNOW_PARAMS)}')

    # -- 3a-1 -------------------------------------------------------------

    def test_3a1_no_budget_no_lump_equals_baseline_control(self):
        """
        Снежок с `lump_sum=0` и `monthly_budget=None` == baseline до копейки.

        Сегодня снежок пересчитывает аннуитет каждый месяц вместо договорного
        платежа: 3 918 288,43 против 3 916 570,47, Δ 1 717,96.
        """
        _next, last_dt, _dates = _control_grid()
        _base_schedule, base_total = _baseline(
            CONTROL_LOAN, CONTROL_RATE, CONTROL_FIRST, last_dt, CONTROL_PAYMENT,
        )
        result, dropped = _snow(
            loan_amount=CONTROL_LOAN, annual_rate=CONTROL_RATE,
            first_payment_date=CONTROL_FIRST, last_payment_date=last_dt,
            lump_sum=0.0, lump_idx=0, monthly_budget=None, monthly_idx=0,
            monthly_extra_day=None, contract_payment=CONTROL_PAYMENT,
        )
        delta = _c(result.total_interest) - _c(base_total)
        self._require(dropped, f'3a-1 (текущее расхождение {delta})')
        self.assertEqual(
            _c(result.total_interest), _c(base_total),
            f'снежок без бюджета и без досрочки разошёлся с базой на {delta} ₽',
        )

    def test_3a1_no_budget_no_lump_equals_baseline_matrix(self):
        """То же самое на сетке остаток × ставка × день первого платежа."""
        mismatches = []
        dropped = []
        for loan in LOANS:
            for rate in RATES:
                for first_dt in FIRST_DATES:
                    _next, last_dt, dates = grid(first_dt, SHORT_TERM)
                    payment = annuity(loan, rate, len(dates))
                    _base_schedule, base_total = _baseline(
                        loan, rate, first_dt, last_dt, payment,
                    )
                    result, dropped = _snow(
                        loan_amount=loan, annual_rate=rate,
                        first_payment_date=first_dt, last_payment_date=last_dt,
                        lump_sum=0.0, lump_idx=0, monthly_budget=None, monthly_idx=0,
                        monthly_extra_day=None, contract_payment=payment,
                    )
                    if _c(result.total_interest) != _c(base_total):
                        mismatches.append(
                            f'{int(loan)}/{rate}/{first_dt:%d.%m.%Y}: '
                            f'{result.total_interest} против {base_total} '
                            f'(Δ {_c(result.total_interest) - _c(base_total)})'
                        )
        self._require(dropped, f'3a-1 ({len(mismatches)} расхождений на сетке)')
        self.assertEqual(mismatches, [], 'снежок без бюджета не равен базе')

    # -- 3a-2 -------------------------------------------------------------

    def test_3a2_snowball_honours_adjust_business_days(self):
        """
        `snowball(budget=None, lump=0, adj=True)` построчно == `build_amortization(adj=True)`.

        Сегодня у снежка параметра нет вообще, поэтому при включённом чекбоксе
        (а он включён по умолчанию) даты и проценты расходятся: 3 916 570,47
        против 3 930 963,06, Δ 14 392,59.
        """
        _next, last_dt, _dates = _control_grid()
        base_schedule, base_total = _baseline(
            CONTROL_LOAN, CONTROL_RATE, CONTROL_FIRST, last_dt, CONTROL_PAYMENT,
            adj=True,
        )
        result, dropped = _snow(
            loan_amount=CONTROL_LOAN, annual_rate=CONTROL_RATE,
            first_payment_date=CONTROL_FIRST, last_payment_date=last_dt,
            lump_sum=0.0, lump_idx=0, monthly_budget=None, monthly_idx=0,
            monthly_extra_day=None, contract_payment=CONTROL_PAYMENT,
            adjust_business_days=True,
        )
        delta = _c(result.total_interest) - _c(base_total)
        self._require(dropped, f'3a-2 (текущее расхождение {delta})')

        self.assertEqual(_c(result.total_interest), _c(base_total),
                         f'проценты разошлись на {delta} ₽')
        self.assertEqual(len(result.schedule), len(base_schedule),
                         'разное число строк при adjust_business_days=True')
        for got, want in zip(result.schedule, base_schedule):
            self.assertEqual(_row(got), _row(want),
                             f"строка {got['date']} снежка разошлась с аннуитетной "
                             f"{want['date']}")

    # -- 3a-3 -------------------------------------------------------------

    def test_3a3_extra_day_does_not_switch_accrual_basis(self):
        """
        `monthly_extra_day` не переключает базу начисления.

        Конфигурация роадмапа: недостижимый `monthly_idx` — снежок не тратит ни
        рубля, денежные потоки идентичны при любом дне доплаты. Сегодня
        3 923 725,29 против 3 918 288,43, Δ +5 436,86 на пустом месте.
        """
        _next, last_dt, dates = _control_grid()
        unreachable = len(dates) + 10

        def run(extra_day):
            result, dropped = _snow(
                loan_amount=CONTROL_LOAN, annual_rate=CONTROL_RATE,
                first_payment_date=CONTROL_FIRST, last_payment_date=last_dt,
                lump_sum=0.0, lump_idx=0, monthly_budget=60_000.0,
                monthly_idx=unreachable, monthly_extra_day=extra_day,
                contract_payment=CONTROL_PAYMENT,
            )
            return result, dropped

        reference, dropped = run(None)
        deltas = {}
        for extra_day in (1, 15, 28, 31):
            probe, _dropped = run(extra_day)
            deltas[extra_day] = _c(probe.total_interest) - _c(reference.total_interest)

        self._require(dropped, f'3a-3 (текущие дельта по дню доплаты: {deltas})')
        for extra_day, delta in deltas.items():
            self.assertEqual(
                delta, Decimal('0.00'),
                f'monthly_extra_day={extra_day} при недостижимом monthly_idx '
                f'изменил проценты на {delta} ₽ — база начисления поехала от дня доплаты',
            )

    # -- бюджет == договорному платежу ------------------------------------

    def test_reduce_term_with_budget_equal_to_payment_equals_baseline(self):
        """
        `mode='reduce_term'` с `monthly_budget == contract_payment` == baseline.

        Бюджет ровно равен платежу ⇒ доплаты нет ⇒ график обязан совпасть с
        аннуитетным построчно. Сегодня у снежка нет ни `mode`, ни
        `contract_payment`.
        """
        _next, last_dt, _dates = _control_grid()
        base_schedule, base_total = _baseline(
            CONTROL_LOAN, CONTROL_RATE, CONTROL_FIRST, last_dt, CONTROL_PAYMENT,
        )
        result, dropped = _snow(
            loan_amount=CONTROL_LOAN, annual_rate=CONTROL_RATE,
            first_payment_date=CONTROL_FIRST, last_payment_date=last_dt,
            lump_sum=0.0, lump_idx=0, monthly_budget=CONTROL_PAYMENT, monthly_idx=0,
            monthly_extra_day=None, contract_payment=CONTROL_PAYMENT,
            mode='reduce_term',
        )
        delta = _c(result.total_interest) - _c(base_total)
        self._require(dropped, f'3a/mode (текущее расхождение {delta})')

        self.assertEqual(_c(result.total_interest), _c(base_total),
                         f'бюджет, равный платежу, изменил проценты на {delta} ₽')
        self.assertEqual(len(result.schedule), len(base_schedule),
                         'бюджет, равный платежу, изменил число строк')
        for got, want in zip(result.schedule, base_schedule):
            self.assertEqual(_row(got), _row(want),
                             f"строка {got['date']} разошлась с аннуитетной")


# ---------------------------------------------------------------------------
# 3b — недоплата и семантика месяцев
# ---------------------------------------------------------------------------

class Snowball3bTest(unittest.TestCase):
    """Приёмка 3b: снято `min(annuity, budget)`, `months_to_payoff` в месяцах."""

    def test_underpayment_pays_annuity_in_full(self):
        """
        Бюджет меньше аннуитета не отменяет аннуитет.

        Конфигурация известного бага из `tests/golden/known_bugs.json`:
        8 000 000 ₽ @ 16 %, бюджет 40 000 (аннуитет ≈ 106 900), день доплаты 15.
        Сегодня `min(annuity, budget)` схлопывает тело в ноль: Σ principal = 0,00,
        остаток так и остаётся 8 000 000,00 через 299 строк.
        """
        _next, last_dt, _dates = _control_grid()
        result, _dropped = _snow(
            loan_amount=8_000_000.0, annual_rate=16.0,
            first_payment_date=CONTROL_FIRST, last_payment_date=last_dt,
            lump_sum=0.0, lump_idx=0, monthly_budget=40_000.0, monthly_idx=0,
            monthly_extra_day=15,
        )
        total_principal = _sum_principal(result.schedule)
        self.assertEqual(
            total_principal, Decimal('8000000.00'),
            f'Σ principal = {total_principal} (ожидалось 8 000 000,00): аннуитет '
            f'обрезан бюджетом, остаток последней строки '
            f"{result.schedule[-1]['balance']}",
        )
        self.assertLessEqual(
            _c(result.schedule[-1]['balance']), Decimal('0.01'),
            'кредит не закрылся: остаток последней строки не нулевой',
        )

    def test_months_to_payoff_counts_months_not_rows(self):
        """
        `months_to_payoff` — месяцы, а не строки графика.

        Конфигурация роадмапа: контрольный договор, бюджет 60 000, без разовой
        суммы. Сегодня возвращается `len(schedule)` = 121 при 61 аннуитетной
        строке; последняя дата 02.05.2031.
        """
        _next, last_dt, _dates = _control_grid()
        result, _dropped = _snow(
            loan_amount=CONTROL_LOAN, annual_rate=CONTROL_RATE,
            first_payment_date=CONTROL_FIRST, last_payment_date=last_dt,
            lump_sum=0.0, lump_idx=0, monthly_budget=60_000.0, monthly_idx=0,
            monthly_extra_day=None, contract_payment=CONTROL_PAYMENT,
        )
        annuity_rows = sum(1 for r in result.schedule if r.get('row_kind') == 'annuity')

        self.assertEqual(
            result.schedule[-1]['date'], '02.05.2031',
            f"последняя дата {result.schedule[-1]['date']}, ожидалась 02.05.2031",
        )
        self.assertEqual(
            annuity_rows, 61,
            f'аннуитетных строк {annuity_rows}, ожидался 61 месяц',
        )
        self.assertEqual(
            result.months_to_payoff, 61,
            f'months_to_payoff = {result.months_to_payoff} при {annuity_rows} '
            f'аннуитетных строках и {len(result.schedule)} строках графика',
        )


# ---------------------------------------------------------------------------
# 3c — lump_unused наружу
# ---------------------------------------------------------------------------

class Snowball3cTest(unittest.TestCase):
    """Приёмка 3c в части снежка: излишек разовой суммы не испаряется молча."""

    def _oversized_lump_run(self):
        """
        Разовая сумма заведомо больше остатка: 5 млн на кредит 1 млн.

        Проверяется полный результат снежка (`simulate_snowball`), если он есть:
        трёхэлементная `calc_repayment_schedule` о неистраченных рублях сообщить
        не может в принципе. Пока полного результата нет — спрашиваем обёртку,
        и тест честно краснеет.
        """
        _next, last_dt, _dates = grid(FIRST_DATES[0], SHORT_TERM)
        payment = annuity(1_000_000.0, 7.99, SHORT_TERM)
        kwargs = dict(
            loan_amount=1_000_000.0, annual_rate=7.99,
            first_payment_date=FIRST_DATES[0], last_payment_date=last_dt,
            lump_sum=5_000_000.0, lump_idx=0, monthly_budget=None, monthly_idx=0,
            monthly_extra_day=None, contract_payment=payment,
        )
        if simulate_snowball is None:
            return _snow(**kwargs)
        params = frozenset(inspect.signature(simulate_snowball).parameters)
        dropped = sorted(k for k in kwargs if k not in params)
        call = {k: v for k, v in kwargs.items() if k in params}
        return _unpack(simulate_snowball(**call)), dropped

    def test_lump_unused_is_returned(self):
        """
        Излишек возвращается наружу, а не обрезается `min(lump, balance)`.

        Сегодня `calc_repayment_schedule` отдаёт 3-кортеж
        `(total_interest, months_to_payoff, schedule)` — сообщить о неистраченных
        рублях ему нечем.
        """
        result, _dropped = self._oversized_lump_run()
        self.assertIsNotNone(
            result.lump_unused,
            'lump_unused наружу не отдаётся: ожидался 4-й элемент кортежа '
            'calc_repayment_schedule или атрибут lump_unused на объекте результата',
        )
        self.assertGreater(
            _c(result.lump_unused), Decimal('0.00'),
            'разовая сумма 5 000 000 ₽ на кредите 1 000 000 ₽ обязана дать излишек',
        )

    def test_lump_unused_equals_the_part_that_did_not_fit(self):
        """`lump_unused` == внесено − применено, и излишек не попал в тело."""
        result, _dropped = self._oversized_lump_run()
        if result.lump_unused is None:
            self.fail('lump_unused наружу не отдаётся (И3c ещё не приехала)')

        applied_early = sum((_c(r['early']) for r in result.schedule), Decimal('0'))
        self.assertEqual(
            _c(result.lump_unused), Decimal('5000000.00') - applied_early,
            f'lump_unused = {result.lump_unused}, применено досрочкой {applied_early}',
        )
        total_principal = _sum_principal(result.schedule)
        self.assertEqual(
            total_principal, Decimal('1000000.00'),
            f'Σ principal = {total_principal}: излишек ушёл в тело кредита',
        )


if __name__ == '__main__':
    unittest.main()
