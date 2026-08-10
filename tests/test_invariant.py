"""
Машинная фиксация инварианта коммита `3ca4b3e`: досрочное погашение гасит
только тело и никогда не уменьшает уже начисленные проценты периода.

Тесты не зависят от golden-файлов — они гоняют матрицу входов (`tests/matrix.py`)
через текущие `build_amortization` / `simulate_lump_repayment` /
`calc_repayment_schedule` и проверяют свойства графиков напрямую.

Что проверяется:

* безусловно — досрочная часть платежа целиком уходит в тело (`early <= principal`);
* безусловно — выделенная строка досрочки (платёж состоит только из досрочки)
  имеет `interest == 0`;
* безусловно — построчно `principal + interest == payment` (в копейках, не во float);
* безусловно — Σ `interest` по строкам графика равна возвращённому `total_interest`;
* контрфактически — досрочка не уменьшает проценты того периода, в который сделана:
  тот же вход с `lump_sum = 0` даёт ровно те же проценты в этой строке;
* условно — Σ `principal == loan_amount`, только для входов, где кредит фактически
  закрывается (последняя строка графика имеет `balance <= 0.01`).

Известные расхождения текущего кода зафиксированы числами в отдельных тестах
(`test_known_*`). Их нельзя чинить на И0: снимок фиксирует поведение как есть.
Когда И1/И3 их исправят, эти тесты покраснеют — это и есть напоминание внести
запись в CHANGELOG.

Запуск: PYTHONPATH=web python -m unittest discover -s tests
"""
import unittest
from decimal import Decimal

from app.calculator import (
    build_amortization,
    calc_repayment_schedule,
    simulate_lump_repayment,
)
from matrix import amortization_cases, lump_cases, snowball_cases

CENT = Decimal('0.01')


def d(value):
    """Число из графика — в Decimal. Сравнения идут в копейках, не во float."""
    return Decimal(str(value))


# ---------------------------------------------------------------------------
# Прогон матрицы
# ---------------------------------------------------------------------------

def _run_amortization(kwargs):
    schedule, _first_payment, total_interest = build_amortization(**kwargs)
    return schedule, total_interest


def _run_lump(kwargs):
    schedule, _payment, total_interest, _months = simulate_lump_repayment(**kwargs)
    return schedule, total_interest


def _run_snowball(kwargs):
    total_interest, _months, schedule = calc_repayment_schedule(**kwargs)
    return schedule, total_interest


SUITES = (
    ('build_amortization', amortization_cases, _run_amortization),
    ('simulate_lump_repayment', lump_cases, _run_lump),
    ('calc_repayment_schedule', snowball_cases, _run_snowball),
)


def iter_runs():
    """Прогоняет всю матрицу: (имя функции, кейс, график, total_interest)."""
    for name, cases, run in SUITES:
        for case in cases():
            schedule, total_interest = run(case['kwargs'])
            yield name, case, schedule, total_interest


def is_early_only_row(row):
    """
    Строка состоит только из досрочки: весь платёж — тело, планового аннуитета в
    ней нет. Именно такие строки эмитируют `_early_row()` в
    `simulate_lump_repayment` и «Row 2» в `calc_repayment_schedule`.
    """
    return d(row['early']) > 0 and d(row['payment']) == d(row['early'])


def where(name, case, row):
    """Человекочитаемый адрес строки для сообщения об ошибке."""
    return f"{name} :: {case['id']} :: строка {row['payment_num']} ({row['date']})"


# ---------------------------------------------------------------------------
# Известные расхождения текущего кода (И0 фиксирует, не чинит)
# ---------------------------------------------------------------------------

# ИСПРАВЛЕНО на И1. Раньше при досрочке 2 000 000 ₽ внутри периода проценты
# периода оказывались больше пересчитанного аннуитета, тело зажималось в ноль
# (`calculator.py:310-311`), и строка переставала сходиться:
# `principal + interest != payment` — при этом неоплаченные проценты всё равно
# попадали в `total_interest`. Движок клэмп не ставит: тело выходит
# отрицательным, остаток честно растёт, строка сходится. Замер на матрице —
# ровно две такие строки:
#   lump/long/2000000/between/reduce_payment/adj0, строка 3:
#       payment 7 666.34 = principal −5 169.45 + interest 12 835.79
#   lump/long/2000000/between/reduce_payment/adj1, строка 3:
#       payment 7 674.36 = principal −4 104.02 + interest 11 778.38
KNOWN_UNBALANCED_ROWS = {}

# ИСПРАВЛЕНО на И3. Раньше в `calc_repayment_schedule` досрочка не всегда
# получала собственную строку: в ветке `pay_off_all` остаток сметался в
# аннуитетную строку, а в ветке `use_split` разовая досрочка приклеивалась к
# платежу того же дня — 350 строк на матрице несли `early > 0` при ненулевых
# процентах. Денежная суть инварианта не нарушалась, расходилась форма строки.
# Снежок стал обёрткой над движком, а движок эмитирует досрочку только отдельной
# строкой, поэтому множество пусто. Тест держит эту пустоту: если слитые строки
# вернутся, он покраснеет.
KNOWN_MERGED_EARLY_ROWS_TOTAL = 0          # строк на всей матрице
KNOWN_MERGED_EARLY_ROWS_CASES = 0          # кейсов, в которых они встречаются


# ---------------------------------------------------------------------------
# Безусловные проверки
# ---------------------------------------------------------------------------

class EarlyRepaymentInvariantTest(unittest.TestCase):
    """Инвариант `3ca4b3e`: досрочка гасит тело и не трогает проценты."""

    def test_early_amount_goes_entirely_into_principal(self):
        """Досрочная часть платежа никогда не уходит в проценты: early <= principal."""
        checked = 0
        for name, case, schedule, _total in iter_runs():
            for row in schedule:
                early = d(row['early'])
                if early <= 0:
                    continue
                checked += 1
                with self.subTest(fn=name, case=case['id'], row=row['payment_num']):
                    self.assertLessEqual(
                        early, d(row['principal']) + CENT,
                        f"{where(name, case, row)}: досрочка {early} больше тела "
                        f"{row['principal']} — часть досрочки ушла в проценты "
                        f"(interest={row['interest']}, payment={row['payment']})",
                    )
        self.assertGreater(checked, 0, 'в матрице не нашлось ни одной строки с досрочкой')

    def test_dedicated_early_row_has_zero_interest(self):
        """Строка, состоящая только из досрочки, всегда имеет interest == 0."""
        checked = 0
        for name, case, schedule, _total in iter_runs():
            for row in schedule:
                if not is_early_only_row(row):
                    continue
                checked += 1
                with self.subTest(fn=name, case=case['id'], row=row['payment_num']):
                    self.assertEqual(
                        d(row['interest']), Decimal('0'),
                        f"{where(name, case, row)}: досрочка {row['early']} начислила "
                        f"проценты {row['interest']} — досрочное погашение не имеет "
                        f"права уменьшать или оплачивать проценты периода",
                    )
        self.assertGreater(checked, 0, 'в матрице не нашлось ни одной выделенной строки досрочки')


class RowBalanceTest(unittest.TestCase):
    """Построчная арифметика графика."""

    def test_payment_equals_principal_plus_interest(self):
        """principal + interest == payment построчно, с допуском в копейку."""
        for name, case, schedule, _total in iter_runs():
            with self.subTest(fn=name, case=case['id']):
                for row in schedule:
                    if (case['id'], row['payment_num']) in KNOWN_UNBALANCED_ROWS:
                        continue  # известное расхождение, зафиксировано ниже числами
                    principal = d(row['principal'])
                    interest = d(row['interest'])
                    payment = d(row['payment'])
                    self.assertLessEqual(
                        abs(principal + interest - payment), CENT,
                        f"{where(name, case, row)}: principal {principal} + interest "
                        f"{interest} = {principal + interest} != payment {payment} "
                        f"(расхождение {principal + interest - payment})",
                    )

    def test_sum_interest_equals_total_interest(self):
        """Σ interest по строкам графика == возвращённый total_interest."""
        for name, case, schedule, total_interest in iter_runs():
            with self.subTest(fn=name, case=case['id']):
                rows_sum = sum((d(row['interest']) for row in schedule), Decimal('0'))
                self.assertLessEqual(
                    abs(rows_sum - d(total_interest)), CENT,
                    f"{name} :: {case['id']}: Σ interest по {len(schedule)} строкам "
                    f"= {rows_sum}, а функция вернула {total_interest} "
                    f"(расхождение {rows_sum - d(total_interest)})",
                )


class SumPrincipalTest(unittest.TestCase):
    """Условная проверка: тело сходится только у графиков, которые закрылись."""

    def test_sum_principal_equals_loan_amount(self):
        """
        Σ principal == loan_amount — только для входов, где кредит фактически
        закрывается. Незакрывшиеся графики (в том числе жертвы известного бага
        `min(annuity, budget)`, где тело не гасится вовсе) скипаются: проверка к
        ним неприменима. Признак определяется по результату — балансу последней
        строки, а не по списку id.
        """
        for name, case, schedule, _total in iter_runs():
            with self.subTest(fn=name, case=case['id']):
                self.assertTrue(schedule, f'{name} :: {case["id"]}: пустой график')

                paid = sum((d(row['principal']) for row in schedule), Decimal('0'))
                loan = d(case['kwargs']['loan_amount'])
                tail = d(schedule[-1]['balance'])

                if tail > CENT:
                    if paid == 0:
                        self.skipTest(
                            f'кредит не гасится вовсе (Σ principal = 0.00, остаток {tail}) — '
                            f'известный баг min(annuity, budget), чинится на И3'
                        )
                    self.skipTest(
                        f'кредит не закрывается за срок графика (остаток {tail}, '
                        f'погашено {paid} из {loan}) — проверка неприменима'
                    )

                self.assertLessEqual(
                    abs(paid - loan), CENT,
                    f"{name} :: {case['id']}: Σ principal по {len(schedule)} строкам "
                    f"= {paid}, а остаток долга был {loan} (расхождение {paid - loan})",
                )


# ---------------------------------------------------------------------------
# Контрфактические проверки: досрочка не удешевляет текущий период
# ---------------------------------------------------------------------------

class LumpDoesNotReducePeriodInterestTest(unittest.TestCase):
    """
    Тот же вход с `lump_sum = 0` обязан дать ровно те же проценты в периоде,
    в который сделана досрочка. Экономия появляется в следующих периодах, а не
    задним числом в текущем — это и есть содержание коммита `3ca4b3e`.
    """

    def test_lump_on_payment_date_does_not_change_that_payment_interest(self):
        """simulate_lump_repayment: досрочка в дату планового платежа."""
        checked = 0
        skipped = 0
        for case in lump_cases():
            kwargs = case['kwargs']
            if case['meta']['lump_date_kind'] != 'on_payment' or kwargs['lump_sum'] <= 0:
                continue
            with self.subTest(case=case['id']):
                schedule, _p, _t, _m = simulate_lump_repayment(**kwargs)
                baseline, _p, _t, _m = simulate_lump_repayment(**dict(kwargs, lump_sum=0.0))

                stamp = kwargs['lump_date'].strftime('%d.%m.%Y')
                annuity_rows = [r for r in schedule
                                if r['date'] == stamp and not is_early_only_row(r)]
                baseline_rows = [r for r in baseline if r['date'] == stamp]
                if not annuity_rows or not baseline_rows:
                    # adjust_business_days сдвигает плановую дату на рабочий день,
                    # и досрочка попадает уже внутрь периода, а не в его конец.
                    skipped += 1
                    self.skipTest(
                        f'в дату досрочки {stamp} нет плановой строки '
                        f'(adjust_business_days={kwargs["adjust_business_days"]})'
                    )

                checked += 1
                self.assertEqual(
                    d(annuity_rows[0]['interest']), d(baseline_rows[0]['interest']),
                    f"{case['id']}: проценты платежа {stamp} с досрочкой "
                    f"{kwargs['lump_sum']} = {annuity_rows[0]['interest']}, "
                    f"без досрочки = {baseline_rows[0]['interest']} — досрочка "
                    f"уменьшила уже начисленные проценты периода",
                )

                early_rows = [r for r in schedule if r['date'] == stamp and d(r['early']) > 0]
                for row in early_rows:
                    self.assertEqual(
                        d(row['interest']), Decimal('0'),
                        f"{case['id']}: строка досрочки {stamp} несёт проценты "
                        f"{row['interest']}",
                    )
        self.assertGreater(checked, 0, 'ни один кейс с досрочкой в дату платежа не проверился')

    def test_snowball_lump_month_interest_unchanged(self):
        """calc_repayment_schedule: месяц разовой досрочки внутри снежного кома."""
        checked = 0
        for case in snowball_cases():
            kwargs = case['kwargs']
            if kwargs['lump_sum'] <= 0:
                continue
            with self.subTest(case=case['id']):
                _t, _m, schedule = calc_repayment_schedule(**kwargs)
                _t, _m, baseline = calc_repayment_schedule(**dict(kwargs, lump_sum=0.0))

                rows = [r for r in schedule if not is_early_only_row(r)]
                base_rows = [r for r in baseline if not is_early_only_row(r)]
                idx = kwargs['lump_idx']
                if len(rows) <= idx or len(base_rows) <= idx:
                    self.skipTest(
                        f'график короче месяца досрочки: {len(rows)} и {len(base_rows)} '
                        f'аннуитетных строк при lump_idx={idx}'
                    )

                checked += 1
                self.assertEqual(
                    d(rows[idx]['interest']), d(base_rows[idx]['interest']),
                    f"{case['id']}: проценты аннуитета {rows[idx]['date']} с досрочкой "
                    f"{kwargs['lump_sum']} = {rows[idx]['interest']}, без досрочки = "
                    f"{base_rows[idx]['interest']} — досрочка уменьшила уже начисленные "
                    f"проценты периода",
                )
        self.assertGreater(checked, 0, 'ни один снежковый кейс с досрочкой не проверился')


# ---------------------------------------------------------------------------
# Известные расхождения: зафиксированы числами, чинятся на И1/И3
# ---------------------------------------------------------------------------

class KnownDeviationsTest(unittest.TestCase):
    """
    Снимок И0 фиксирует текущее поведение как есть. Тесты ниже держат известные
    расхождения на числах: когда И1/И3 их починят, тесты покраснеют и потребуют
    записи в CHANGELOG. Чинить их здесь нельзя.
    """

    def test_known_unbalanced_rows_in_lump_simulation(self):
        """
        Исправлено на И1: несходящихся строк больше нет.

        Раньше при досрочке 2 000 000 ₽ внутри периода пересчитанный аннуитет
        оказывался меньше процентов периода, тело зажималось в ноль
        (`calculator.py:310-311`) и строка переставала сходиться. Движок клэмп
        снял: тело выходит отрицательным, остаток растёт, строка сходится.
        Тест держит пустоту множества — если клэмп вернётся, он покраснеет.
        """
        found = {}
        for case in lump_cases():
            schedule, _p, _t, _m = simulate_lump_repayment(**case['kwargs'])
            for row in schedule:
                principal, interest, payment = d(row['principal']), d(row['interest']), d(row['payment'])
                if abs(principal + interest - payment) > CENT:
                    found[(case['id'], row['payment_num'])] = {
                        'principal': f'{principal:.2f}',
                        'interest': f'{interest:.2f}',
                        'payment': f'{payment:.2f}',
                    }
        self.assertEqual(
            found, KNOWN_UNBALANCED_ROWS,
            'набор несходящихся строк изменился: слева факт, справа снимок И0. '
            'Если расхождение исправлено — обновить KNOWN_UNBALANCED_ROWS и '
            'дописать строку в CHANGELOG.',
        )

    def test_known_merged_early_rows_in_snowball(self):
        """
        Известное расхождение, И3: в снежном коме досрочка не всегда получает
        отдельную строку. В ветке `pay_off_all` остаток сметается в аннуитетную
        строку, а в ветке `use_split` разовая досрочка приклеивается к платежу
        того же дня. Такие строки несут `early > 0` при ненулевых процентах.
        Деньги при этом на месте: проценты начислены на добытийный остаток
        (см. test_snowball_lump_month_interest_unchanged), расходится только форма.
        """
        rows_total = 0
        cases_hit = set()
        for name, cases, run in SUITES:
            for case in cases():
                schedule, _total = run(case['kwargs'])
                for row in schedule:
                    if d(row['early']) > 0 and d(row['interest']) != 0:
                        self.assertEqual(
                            name, 'calc_repayment_schedule',
                            f'{where(name, case, row)}: слитая строка досрочки за '
                            f'пределами снежного кома — это уже настоящее нарушение '
                            f'инварианта, а не известная форма',
                        )
                        rows_total += 1
                        cases_hit.add(case['id'])

        self.assertEqual(
            (rows_total, len(cases_hit)),
            (KNOWN_MERGED_EARLY_ROWS_TOTAL, KNOWN_MERGED_EARLY_ROWS_CASES),
            'число слитых строк досрочки в снежном коме изменилось. Если форма '
            'исправлена — обновить константы и дописать строку в CHANGELOG.',
        )


if __name__ == '__main__':
    unittest.main()
