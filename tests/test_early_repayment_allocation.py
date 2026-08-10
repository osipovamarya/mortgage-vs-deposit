"""
Приёмка Итерации 2 роадмапа — W5, правило распределения досрочки
(``early_repayment_allocation``).

Проверяются свойства режима, а не его реализация:

* дефолт — ``principal_only``: вызов без ``allocation`` побайтово равен явному
  (инвариант коммита 3ca4b3e стал настройкой, дефолт не менялся);
* ``interest_first`` с досрочкой в дату планового платежа или без даты
  побайтово равен ``principal_only`` — в эту дату проценты периода уже закрыты
  аннуитетом (решение по открытому вопросу «Блокируют И2», вариант (а));
* на матрице при ``mode='reduce_term'``:
  ``total_interest(interest_first) >= total_interest(principal_only)``,
  0 нарушений. Для ``reduce_payment`` неравенство НЕ проверяется — там другой
  денежный поток (платёж пересчитан от большего остатка), фиксируется
  ``sum(payment)``;
* анти-двойное-начисление: проценты разрезанного досрочкой периода
  сравниваются с ВЫЧИСЛЕННЫМ ожиданием, а не с литералом. Проверяется и то,
  что режимы расходятся ровно на «проценты на удержанные проценты»;
* контрольный пример роадмапа (3 000 000 ₽, 8 %, досрочка 500 000 на
  17.04.2026) в обеих базах;
* досрочка меньше начисленных процентов: тело не уходит в минус, остаток
  процентов доезжает до ближайшего аннуитета.

Общий инструментарий (мягкий импорт движка, конвертация кейсов матрицы,
отпечаток результата) живёт в ``tests/test_engine.py`` — здесь он только
переиспользуется. Пока ``web/app/engine.py`` не существует, файл уходит в skip.

Запуск::

    PYTHONPATH=web python -m unittest discover -s tests
"""
import unittest
from datetime import datetime
from decimal import Decimal

from matrix import CONTROL_FIRST, LONG_TERM, annuity, grid, lump_cases

from test_engine import (
    ALLOC_INTEREST_FIRST,
    ALLOC_PRINCIPAL_ONLY,
    BASIS_DAILY,
    BASIS_MONTHLY,
    ENGINE,
    ROW_EARLY,
    SKIP_REASON,
    MoneyMixin,
    accrue,
    basis_of,
    d,
    early_rows,
    fingerprint,
    is_annuity_row,
    payment_grid,
    period_interest,
    r2,
    run_case,
    sample,
    state_of,
)

# Виды дат досрочки, при которых режимы обязаны совпадать побайтово:
# «без даты» и «до первого платежа» — досрочка применяется сразу, начисленных
# процентов ещё нет; «в дату платежа» — они уже закрыты аннуитетом.
SAME_BY_CONSTRUCTION = ('none', 'before_first', 'on_payment', 'after_end')

# Контрольный пример роадмапа (раздел «Итерация 2 → Готово, когда»).
CONTROL = {
    'loan_amount': 3_000_000.0,
    'annual_rate': 8.0,
    'monthly_payment': 23_124.77,
    'lump_sum': 500_000.0,
    'lump_date': datetime(2026, 4, 17),
}


def control_kwargs(basis, **over):
    next_dt, last_dt, _dates = grid(CONTROL_FIRST, LONG_TERM)
    kwargs = {
        'loan_amount': CONTROL['loan_amount'],
        'annual_rate': CONTROL['annual_rate'],
        'first_payment_date': next_dt,
        'last_payment_date': last_dt,
        'monthly_payment': CONTROL['monthly_payment'],
        'lump_sum': CONTROL['lump_sum'],
        'lump_date': CONTROL['lump_date'],
        'mode': 'reduce_term',
        'adjust_business_days': basis == BASIS_DAILY,
        'prev_payment_date': CONTROL_FIRST,
    }
    kwargs.update(over)
    return kwargs


def split_days(kwargs, basis):
    """
    Разбиение первого периода досрочкой по СОБСТВЕННОЙ сетке движка:
    (дни до досрочки, дни после, длина периода). Дни берутся из payment_grid,
    поэтому при basis='daily' учитывается сдвиг на рабочий день.
    """
    dates, anchor = payment_grid(state_of(kwargs), basis)
    lump_at = kwargs['lump_date']
    days1 = (lump_at - anchor).days
    days2 = (dates[0] - lump_at).days
    period_days = (dates[0] - anchor).days
    return days1, days2, period_days, dates[0]


def total_paid(result):
    """«Всего выплачено» — Σ payment по всем строкам, включая строки досрочки."""
    return float(r2(sum((d(row['payment']) for row in result.schedule), Decimal('0'))))


def total_principal(result):
    return float(r2(sum((d(row['principal']) for row in result.schedule), Decimal('0'))))


# ---------------------------------------------------------------------------
# Дефолт и тождественные конфигурации
# ---------------------------------------------------------------------------

@unittest.skipIf(ENGINE is None, SKIP_REASON)
class DefaultAllocationTest(MoneyMixin, unittest.TestCase):

    def test_default_equals_explicit_principal_only(self):
        """Вызов без allocation побайтово равен явному principal_only."""
        for case in sample(lump_cases()):
            with self.subTest(case=case['id']):
                default = fingerprint(run_case(case['kwargs']))
                explicit = fingerprint(run_case(case['kwargs'],
                                                allocation=ALLOC_PRINCIPAL_ONLY))
                self.assertEqual(default, explicit,
                                 f'{case["id"]}: дефолт разошёлся с principal_only')

    def test_event_allocation_overrides_nothing_when_same(self):
        """allocation на событии == allocation в SimOptions → тот же результат."""
        for case in sample(lump_cases()):
            if not case['kwargs'].get('lump_sum'):
                continue
            with self.subTest(case=case['id']):
                from_opts = fingerprint(run_case(case['kwargs'],
                                                 allocation=ALLOC_PRINCIPAL_ONLY))
                from_event = fingerprint(run_case(case['kwargs'],
                                                  event_allocation=ALLOC_PRINCIPAL_ONLY))
                self.assertEqual(from_opts, from_event,
                                 f'{case["id"]}: allocation на событии не подхватился')

    def test_event_allocation_wins_over_options(self):
        """
        allocation на событии перекрывает SimOptions: interest_first на событии
        поверх principal_only в опциях обязан дать то же, что interest_first
        в опциях.
        """
        kwargs = control_kwargs(BASIS_MONTHLY)
        by_opts = fingerprint(run_case(kwargs, allocation=ALLOC_INTEREST_FIRST))
        by_event = fingerprint(run_case(kwargs, allocation=ALLOC_PRINCIPAL_ONLY,
                                        event_allocation=ALLOC_INTEREST_FIRST))
        self.assertEqual(by_opts, by_event,
                         'allocation на событии обязан перекрывать SimOptions')


@unittest.skipIf(ENGINE is None, SKIP_REASON)
class ModesCoincideTest(MoneyMixin, unittest.TestCase):
    """Конфигурации, в которых interest_first по построению равен principal_only."""

    def test_lump_on_payment_date_or_without_date(self):
        covered = {kind: 0 for kind in SAME_BY_CONSTRUCTION}
        for case in sample(lump_cases()):
            kind = case['meta']['lump_date_kind']
            kwargs = case['kwargs']
            if kind not in SAME_BY_CONSTRUCTION or not kwargs.get('lump_sum'):
                continue
            if kind == 'on_payment' and not self._lands_on_payment_date(kwargs):
                continue        # см. test_shifted_payment_date_is_not_a_payment_date
            covered[kind] += 1
            with self.subTest(case=case['id']):
                po = fingerprint(run_case(kwargs, allocation=ALLOC_PRINCIPAL_ONLY))
                first = fingerprint(run_case(kwargs, allocation=ALLOC_INTEREST_FIRST))
                self.assertEqual(
                    po, first,
                    f'{case["id"]}: в эту дату проценты периода уже закрыты аннуитетом, '
                    'режимы обязаны совпадать побайтово',
                )
        self.assertTrue(all(covered.values()),
                        f'какой-то вид даты остался непроверенным: {covered}')

    @staticmethod
    def _lands_on_payment_date(kwargs):
        """Дата досрочки лежит в СОБСТВЕННОЙ сетке движка, а не в сырой rrule-сетке."""
        dates, _anchor = payment_grid(state_of(kwargs), basis_of(kwargs))
        return kwargs['lump_date'] in dates

    def test_shifted_payment_date_is_not_a_payment_date(self):
        """
        При basis='daily' платёж, выпавший на выходной, уезжает на понедельник, и
        досрочка «в дату платежа» из формы оказывается ВНУТРИ периода. Режимы в
        таких кейсах обязаны разойтись — иначе проверка выше отбраковывала бы их
        зря, а движок игнорировал бы сдвиг сетки.

        Исключение — досрочка, которой хватает на весь остаток: кредит
        закрывается прямо на событии, и распределять уже нечего (излишек уходит
        в lump_unused). Такие кейсы совпадают законно.
        """
        shifted, differing = 0, 0
        for case in sample(lump_cases()):
            kwargs = case['kwargs']
            if case['meta']['lump_date_kind'] != 'on_payment' or not kwargs.get('lump_sum'):
                continue
            if self._lands_on_payment_date(kwargs):
                continue
            po = run_case(kwargs, allocation=ALLOC_PRINCIPAL_ONLY)
            first = run_case(kwargs, allocation=ALLOC_INTEREST_FIRST)
            if po.lump_unused > 0 or first.lump_unused > 0:
                continue                       # досрочка закрыла кредит целиком
            shifted += 1
            if abs(first.total_interest - po.total_interest) > 0.005:
                differing += 1
        self.assertGreater(shifted, 0, 'в матрице нет сдвинутых дат платежа')
        self.assertEqual(differing, shifted,
                         f'{shifted - differing} из {shifted} сдвинутых кейсов не заметили, '
                         'что досрочка попала внутрь периода')

    def test_zero_lump_is_mode_agnostic(self):
        """Без досрочки распределять нечего."""
        for case in sample(lump_cases()):
            if case['kwargs'].get('lump_sum'):
                continue
            with self.subTest(case=case['id']):
                self.assertEqual(
                    fingerprint(run_case(case['kwargs'], allocation=ALLOC_PRINCIPAL_ONLY)),
                    fingerprint(run_case(case['kwargs'], allocation=ALLOC_INTEREST_FIRST)),
                    f'{case["id"]}: пустое событие не может зависеть от режима',
                )


# ---------------------------------------------------------------------------
# Свойства режима на матрице
# ---------------------------------------------------------------------------

@unittest.skipIf(ENGINE is None, SKIP_REASON)
class MatrixPropertiesTest(MoneyMixin, unittest.TestCase):

    def test_interest_first_never_cheaper_on_reduce_term(self):
        """
        mode='reduce_term': платёж не пересчитывается, поэтому денежный поток тот
        же, и удержание процентов из досрочки может только увеличить итог.
        0 нарушений на матрице.
        """
        violations = []
        strictly_greater = 0
        for case in sample(lump_cases()):
            kwargs = case['kwargs']
            if kwargs.get('mode') != 'reduce_term' or not kwargs.get('lump_sum'):
                continue
            po = run_case(kwargs, allocation=ALLOC_PRINCIPAL_ONLY).total_interest
            first = run_case(kwargs, allocation=ALLOC_INTEREST_FIRST).total_interest
            if first < po - 0.005:
                violations.append((case['id'], po, first))
            elif first > po + 0.005:
                strictly_greater += 1

        self.assertEqual(
            violations, [],
            'interest_first дешевле principal_only при reduce_term:\n' + '\n'.join(
                f'  {cid}: {po:.2f} → {first:.2f}' for cid, po, first in violations[:10]
            ),
        )
        self.assertGreater(strictly_greater, 0,
                           'нет ни одного кейса, где режимы разошлись — проверка вырождена')

    def test_reduce_payment_records_total_paid(self):
        """
        mode='reduce_payment': неравенство по процентам не выполняется (платёж
        растёт, кредит закрывается раньше), поэтому фиксируется «Всего выплачено».

        Тождество, которое обязано держаться в обоих режимах: Σ payment ==
        loan_amount + total_interest, а значит разница «Всего выплачено» между
        режимами равна разнице процентов копейка-в-копейку.
        """
        checked = 0
        for case in sample(lump_cases()):
            kwargs = case['kwargs']
            if kwargs.get('mode') != 'reduce_payment' or not kwargs.get('lump_sum'):
                continue
            po = run_case(kwargs, allocation=ALLOC_PRINCIPAL_ONLY)
            first = run_case(kwargs, allocation=ALLOC_INTEREST_FIRST)
            if po.schedule[-1]['balance'] > 0.01 or first.schedule[-1]['balance'] > 0.01:
                continue                       # кредит не закрылся — тождество неприменимо
            checked += 1
            with self.subTest(case=case['id']):
                self.assertMoney(total_principal(po), kwargs['loan_amount'],
                                 f'{case["id"]}: Σ principal (principal_only)', delta=0.02)
                self.assertMoney(total_principal(first), kwargs['loan_amount'],
                                 f'{case["id"]}: Σ principal (interest_first)', delta=0.02)
                self.assertMoney(
                    total_paid(first) - total_paid(po),
                    first.total_interest - po.total_interest,
                    f'{case["id"]}: «Всего выплачено» разошлось с разницей процентов',
                    delta=0.03,
                )
        self.assertGreater(checked, 0, 'в матрице не нашлось закрывающихся reduce_payment')

    def test_early_row_shape_in_interest_first(self):
        """
        Строка досрочки в interest_first: платёж сходится (principal + interest ==
        payment), тело не отрицательное, row_kind == 'early'.
        """
        for case in sample(lump_cases()):
            kwargs = case['kwargs']
            if case['meta']['lump_date_kind'] != 'between' or not kwargs.get('lump_sum'):
                continue
            result = run_case(kwargs, allocation=ALLOC_INTEREST_FIRST)
            for row in early_rows(result):
                with self.subTest(case=case['id'], date=row['date']):
                    self.assertGreaterEqual(row['principal'], 0.0,
                                            'тело досрочки не может быть отрицательным')
                    self.assertMoney(row['principal'] + row['interest'], row['payment'],
                                     f'{case["id"]}: строка досрочки не сходится')
                    if 'row_kind' in row:
                        self.assertEqual(row['row_kind'], ROW_EARLY)
                        self.assertMoney(row.get('early_interest', 0.0), row['interest'],
                                         'early_interest обязан повторять проценты строки')


# ---------------------------------------------------------------------------
# Анти-двойное-начисление
# ---------------------------------------------------------------------------

@unittest.skipIf(ENGINE is None, SKIP_REASON)
class NoDoubleChargeTest(MoneyMixin, unittest.TestCase):
    """
    Проценты разрезанного досрочкой периода сверяются с вычислением:

        I(без досрочки)      = accrue(B, весь период)
        I(principal_only)    = accrue(B, days1) + accrue(B − L, days2)
        I(interest_first)    = accrue(B, days1) + accrue(B − (L − I₁), days2)

    отсюда два свойства:
      * эффект досрочки на проценты периода равен ровно accrue(L, days2)
        — ни рубля сверху (двойное начисление), ни рубля мимо;
      * расхождение режимов равно ровно accrue(I₁, days2) — «проценты на
        удержанные проценты», и считается в СВОЕЙ базе (решения 3 и 4).

    Округление каждого сегмента до копейки даёт погрешность до 0.02 ₽.
    """

    CASES = (
        (3_000_000.0, 8.0, 500_000.0),
        (1_000_000.0, 5.0, 100_000.0),
        (8_000_000.0, 16.0, 500_000.0),
        (8_000_000.0, 16.0, 2_000_000.0),
    )

    def _period_expectations(self, kwargs, basis):
        days1, days2, period_days, end_dt = split_days(kwargs, basis)
        rate = kwargs['annual_rate']
        balance = kwargs['loan_amount']
        lump = kwargs['lump_sum']
        i1 = accrue(balance, rate, basis, days1, period_days)
        return {
            'end': end_dt.strftime('%d.%m.%Y'),
            'days': (days1, days2, period_days),
            'i1': i1,
            'no_lump': accrue(balance, rate, basis, period_days, period_days),
            'principal_only': i1 + accrue(balance - lump, rate, basis, days2, period_days),
            'interest_first': i1 + accrue(balance - (lump - i1), rate, basis, days2, period_days),
            'lump_effect': accrue(lump, rate, basis, days2, period_days),
            'mode_delta': accrue(i1, rate, basis, days2, period_days),
        }

    def test_period_interest_matches_computation(self):
        for loan, rate, lump in self.CASES:
            for basis in (BASIS_MONTHLY, BASIS_DAILY):
                kwargs = control_kwargs(basis, loan_amount=loan, annual_rate=rate,
                                        lump_sum=lump,
                                        monthly_payment=annuity(loan, rate, LONG_TERM))
                exp = self._period_expectations(kwargs, basis)
                with self.subTest(loan=loan, rate=rate, lump=lump, basis=basis):
                    self.assertGreater(exp['days'][0], 0)
                    self.assertGreater(exp['days'][1], 0)

                    po = period_interest(run_case(kwargs, allocation=ALLOC_PRINCIPAL_ONLY),
                                         exp['end'])
                    first = period_interest(run_case(kwargs, allocation=ALLOC_INTEREST_FIRST),
                                            exp['end'])
                    self.assertMoney(po, exp['principal_only'],
                                     'principal_only: проценты периода', delta=0.02)
                    self.assertMoney(first, exp['interest_first'],
                                     'interest_first: проценты периода', delta=0.02)

    def test_lump_effect_is_exactly_the_balance_reduction(self):
        """Проценты периода с досрочкой = проценты без досрочки − accrue(L, days2)."""
        for loan, rate, lump in self.CASES:
            for basis in (BASIS_MONTHLY, BASIS_DAILY):
                kwargs = control_kwargs(basis, loan_amount=loan, annual_rate=rate,
                                        lump_sum=lump,
                                        monthly_payment=annuity(loan, rate, LONG_TERM))
                exp = self._period_expectations(kwargs, basis)
                with self.subTest(loan=loan, rate=rate, lump=lump, basis=basis):
                    without = period_interest(
                        run_case({**kwargs, 'lump_sum': 0.0, 'lump_date': None}), exp['end'])
                    with_lump = period_interest(
                        run_case(kwargs, allocation=ALLOC_PRINCIPAL_ONLY), exp['end'])
                    self.assertMoney(without, exp['no_lump'],
                                     'период без досрочки', delta=0.02)
                    self.assertMoney(without - with_lump, exp['lump_effect'],
                                     'эффект досрочки на проценты периода', delta=0.02)

    def test_mode_delta_is_interest_on_withheld_interest(self):
        for loan, rate, lump in self.CASES:
            for basis in (BASIS_MONTHLY, BASIS_DAILY):
                kwargs = control_kwargs(basis, loan_amount=loan, annual_rate=rate,
                                        lump_sum=lump,
                                        monthly_payment=annuity(loan, rate, LONG_TERM))
                exp = self._period_expectations(kwargs, basis)
                with self.subTest(loan=loan, rate=rate, lump=lump, basis=basis):
                    po = period_interest(run_case(kwargs, allocation=ALLOC_PRINCIPAL_ONLY),
                                         exp['end'])
                    first = period_interest(run_case(kwargs, allocation=ALLOC_INTEREST_FIRST),
                                            exp['end'])
                    self.assertMoney(first - po, exp['mode_delta'],
                                     'расхождение режимов ≠ проценты на удержанные проценты',
                                     delta=0.02)

    def test_balance_after_lump_uses_the_same_basis(self):
        """
        Остаток после досрочки обязан быть balance − (lump − accrued) в ТОЙ ЖЕ
        базе, в которой шло начисление. Смешение баз (дневной остаток × месячная
        ставка) — ровно тот дефект, против которого написаны решения 3 и 4.
        """
        for loan, rate, lump in self.CASES:
            for basis in (BASIS_MONTHLY, BASIS_DAILY):
                kwargs = control_kwargs(basis, loan_amount=loan, annual_rate=rate,
                                        lump_sum=lump,
                                        monthly_payment=annuity(loan, rate, LONG_TERM))
                exp = self._period_expectations(kwargs, basis)
                with self.subTest(loan=loan, rate=rate, lump=lump, basis=basis):
                    result = run_case(kwargs, allocation=ALLOC_INTEREST_FIRST)
                    rows = early_rows(result)
                    self.assertEqual(len(rows), 1)
                    row = rows[0]
                    self.assertMoney(row['interest'], exp['i1'],
                                     'проценты, удержанные из досрочки', delta=0.01)
                    self.assertMoney(row['principal'], lump - exp['i1'],
                                     'тело досрочки', delta=0.01)
                    self.assertMoney(row['balance'], loan - (lump - exp['i1']),
                                     'остаток после досрочки', delta=0.01)


# ---------------------------------------------------------------------------
# Контрольный пример роадмапа
# ---------------------------------------------------------------------------

@unittest.skipIf(ENGINE is None, SKIP_REASON)
class RoadmapControlExampleTest(MoneyMixin, unittest.TestCase):
    """
    3 000 000 ₽, 8 %, прошлый платёж 02.04.2026, досрочка 500 000 на 17.04.2026.
    Числа — из раздела «Итерация 2 → Готово, когда».
    """

    def test_monthly_basis(self):
        """adjust_business_days=False ⇒ basis='monthly'."""
        kwargs = control_kwargs(BASIS_MONTHLY)
        result = run_case(kwargs, allocation=ALLOC_INTEREST_FIRST)
        rows = early_rows(result)
        self.assertEqual(len(rows), 1, 'ожидалась одна строка досрочки')
        row = rows[0]
        self.assertEqual(row['date'], '17.04.2026')
        self.assertMoney(row['interest'], 10_000.00, 'проценты строки досрочки')
        self.assertMoney(row['principal'], 490_000.00, 'тело строки досрочки')
        self.assertMoney(row['balance'], 2_510_000.00, 'остаток после досрочки')

        _d1, _d2, _period, end_dt = split_days(kwargs, BASIS_MONTHLY)
        end = end_dt.strftime('%d.%m.%Y')
        annuity_row = next(r for r in result.schedule
                           if is_annuity_row(r) and r['date'] == end)
        self.assertMoney(annuity_row['interest'], 8_366.67, 'следующий аннуитет')
        self.assertMoney(period_interest(result, end), 18_366.67, 'проценты периода')

        po = run_case(kwargs, allocation=ALLOC_PRINCIPAL_ONLY)
        self.assertMoney(period_interest(po, end), 18_333.33,
                         'principal_only: проценты периода')

    def test_monthly_basis_early_interest_field(self):
        """Отдельное поле строки: в interest_first оно несёт удержанные проценты."""
        result = run_case(control_kwargs(BASIS_MONTHLY), allocation=ALLOC_INTEREST_FIRST)
        row = early_rows(result)[0]
        self.assertIn('early_interest', row, 'поле early_interest добавляется на И2')
        self.assertMoney(row['early_interest'], 10_000.00)

    def test_daily_basis_computed(self):
        """
        adjust_business_days=True ⇒ basis='daily'. Строка досрочки не зависит от
        сдвига дат (якорь 02.04.2026 — четверг), поэтому её числа взяты
        литералами роадмапа; следующий аннуитет считается по СОБСТВЕННОЙ сетке
        движка, потому что при basis='daily' дата платежа 02.05.2026 (суббота)
        уезжает на понедельник и число дней второго отрезка меняется.
        """
        kwargs = control_kwargs(BASIS_DAILY)
        result = run_case(kwargs, allocation=ALLOC_INTEREST_FIRST)
        row = early_rows(result)[0]
        self.assertEqual(row['date'], '17.04.2026')
        self.assertMoney(row['interest'], 9_863.01, 'проценты строки досрочки')
        self.assertMoney(row['principal'], 490_136.99, 'тело строки досрочки')
        self.assertMoney(row['balance'], 2_509_863.01, 'остаток после досрочки')

        _d1, days2, period_days, end_dt = split_days(kwargs, BASIS_DAILY)
        end = end_dt.strftime('%d.%m.%Y')
        expected = accrue(row['balance'], CONTROL['annual_rate'], BASIS_DAILY,
                          days2, period_days)
        annuity_row = next(r for r in result.schedule
                           if is_annuity_row(r) and r['date'] == end)
        self.assertMoney(annuity_row['interest'], expected,
                         f'следующий аннуитет {end} за {days2} дн.')

        # Сравнивать надо ПЕРИОД, а не аннуитетную строку: в principal_only
        # проценты досегментные и послесегментные предъявляет один аннуитет
        # (строка досрочки несёт 0), а в interest_first первый сегмент уже
        # удержан из самой досрочки.
        po = run_case(kwargs, allocation=ALLOC_PRINCIPAL_ONLY)
        self.assertMoney(
            period_interest(result, end) - period_interest(po, end),
            accrue(row['interest'], CONTROL['annual_rate'], BASIS_DAILY, days2, period_days),
            'дельта режимов в базе daily', delta=0.02,
        )

    def test_daily_basis_roadmap_literals(self):
        """
        Литералы роадмапа для базы daily (следующий аннуитет 8 251,60 ₽) сняты
        по НЕсдвинутой сетке: 9 863,01 · 8 %/365 · 15 дн. = 32,42 ₽ дельты даёт
        второй отрезок ровно в 15 дней, то есть платёж 02.05.2026. По контракту
        payment_grid при basis='daily' обязан сдвинуть эту субботу на 04.05.2026,
        и второй отрезок становится 17-дневным. Тест не выносит вердикт, а
        показывает, какое из двух чисел выдал движок.
        """
        kwargs = control_kwargs(BASIS_DAILY)
        result = run_case(kwargs, allocation=ALLOC_INTEREST_FIRST)
        _d1, days2, period_days, end_dt = split_days(kwargs, BASIS_DAILY)
        end = end_dt.strftime('%d.%m.%Y')
        annuity_row = next(r for r in result.schedule
                           if is_annuity_row(r) and r['date'] == end)
        if days2 != 15:
            self.skipTest(
                f'сетка сдвинута на рабочий день: платёж {end}, второй отрезок '
                f'{days2} дн. вместо 15; следующий аннуитет {annuity_row["interest"]:.2f} ₽ '
                f'вместо литерала роадмапа 8 251,60 ₽ (расхождение роадмапа с решением 4, '
                f'см. отчёт по И1/И2)'
            )
        self.assertMoney(annuity_row['interest'], 8_251.60, 'литерал роадмапа')


# ---------------------------------------------------------------------------
# Досрочка меньше начисленных процентов
# ---------------------------------------------------------------------------

@unittest.skipIf(ENGINE is None, SKIP_REASON)
class LumpSmallerThanAccruedTest(MoneyMixin, unittest.TestCase):
    """
    Решение по открытому вопросу «что делать, если досрочка меньше начисленных
    процентов», вариант (а): удержать проценты в пределах внесённой суммы, тело
    не уменьшать, остаток (carried_interest) добавить к ближайшему аннуитету.

    Вход подобран так, чтобы начисленные проценты заведомо превышали досрочку:
    8 000 000 ₽ @ 16 % — за половину периода набегает ~53 тыс. ₽, досрочка 10 тыс.
    """

    LOAN, RATE, LUMP = 8_000_000.0, 16.0, 10_000.0

    def _kwargs(self, basis):
        return control_kwargs(basis, loan_amount=self.LOAN, annual_rate=self.RATE,
                              lump_sum=self.LUMP,
                              monthly_payment=annuity(self.LOAN, self.RATE, LONG_TERM))

    def test_principal_never_goes_negative(self):
        for basis in (BASIS_MONTHLY, BASIS_DAILY):
            with self.subTest(basis=basis):
                kwargs = self._kwargs(basis)
                days1, _days2, period_days, _end = split_days(kwargs, basis)
                accrued = accrue(self.LOAN, self.RATE, basis, days1, period_days)
                self.assertGreater(accrued, self.LUMP,
                                   'вход подобран неверно: досрочка больше процентов')

                result = run_case(kwargs, allocation=ALLOC_INTEREST_FIRST)
                row = early_rows(result)[0]
                self.assertMoney(row['payment'], self.LUMP)
                self.assertMoney(row['principal'], 0.0, 'тело не уменьшается')
                self.assertMoney(row['interest'], self.LUMP,
                                 'вся досрочка ушла в проценты')
                self.assertMoney(row['balance'], self.LOAN, 'остаток не изменился')
                for r in result.schedule:
                    self.assertGreaterEqual(r['balance'], 0.0, 'тело ушло в минус')

    def test_carried_interest_lands_on_the_next_annuity(self):
        """
        Тело не изменилось ⇒ проценты периода обязаны остаться теми же, что и без
        досрочки, а ближайший аннуитет — унести остаток (проценты периода − досрочка).
        """
        for basis in (BASIS_MONTHLY, BASIS_DAILY):
            with self.subTest(basis=basis):
                kwargs = self._kwargs(basis)
                _d1, _d2, _period, end_dt = split_days(kwargs, basis)
                end = end_dt.strftime('%d.%m.%Y')

                without = run_case({**kwargs, 'lump_sum': 0.0, 'lump_date': None})
                result = run_case(kwargs, allocation=ALLOC_INTEREST_FIRST)

                base_period = period_interest(without, end)
                self.assertMoney(period_interest(result, end), base_period,
                                 'тело не изменилось — проценты периода тоже', delta=0.02)

                annuity_row = next(r for r in result.schedule
                                   if is_annuity_row(r) and r['date'] == end)
                self.assertMoney(annuity_row['interest'], base_period - self.LUMP,
                                 'остаток процентов не доехал до ближайшего аннуитета',
                                 delta=0.02)


if __name__ == '__main__':
    unittest.main()
