"""
Golden-тесты: текущий прогон матрицы против закоммиченных снимков.

Снимки лежат в ``tests/golden/*.json`` и снимаются скриптом
``scripts/snapshot_golden.py``. Тест ничего не чинит и ничего не переснимает —
он падает на любом расхождении. Если расхождение осознанное, голден переснимается
руками: ``scripts/snapshot_golden.py --accept <функция> --reason "<текст>"``.

Запуск::

    PYTHONPATH=web python -m unittest discover -s tests
"""
import json
import os
import sys
import unittest
from decimal import Decimal, ROUND_HALF_UP

from datetime import datetime

from dateutil.relativedelta import relativedelta
from dateutil.rrule import rrule, MONTHLY

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(REPO_ROOT, 'scripts'), os.path.join(REPO_ROOT, 'web')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from snapshot_golden import (  # noqa: E402
    FUNCTIONS,
    KNOWN_BUGS_PATH,
    ROW_COLUMNS,
    build_known_bugs,
    diff_case,
    golden_path,
    render_known_bugs,
    run_suite,
)

from matrix import FIRST_DATES, SHORT_TERM, annuity, grid  # noqa: E402
from app.calculator import build_amortization  # noqa: E402

ACCEPT_HINT = 'scripts/snapshot_golden.py --accept {func} --reason "<почему числа изменились>"'


class GoldenSnapshotTest(unittest.TestCase):
    """По одному тесту на функцию; кейсы разложены через subTest."""

    maxDiff = 4000

    def _assert_matches_golden(self, function):
        path = golden_path(function)
        self.assertTrue(
            os.path.exists(path),
            f'нет golden-файла {os.path.relpath(path, REPO_ROOT)}. '
            f'Первичное создание: scripts/snapshot_golden.py --init',
        )

        with open(path, encoding='utf-8') as fh:
            golden = json.load(fh)

        self.assertEqual(
            tuple(golden.get('columns', ())), ROW_COLUMNS,
            f'{function}: порядок колонок строки графика в голдене отличается от текущего',
        )
        self.assertEqual(golden.get('function'), function,
                         f'{function}: голден снят для другой функции')

        stored = golden['cases']
        fresh = run_suite(function)

        self.assertEqual(
            sorted(stored), sorted(fresh),
            f'{function}: набор кейсов матрицы разошёлся с голденом. '
            + ACCEPT_HINT.format(func=function),
        )

        for case_id in sorted(stored):
            expected = stored[case_id]
            actual = fresh[case_id]
            if expected == actual:
                continue
            with self.subTest(case=case_id):
                diffs = diff_case(expected, actual, limit=1)
                path_to_value, want, got = diffs[0]
                self.fail(
                    f'{function} / {case_id}: расхождение в {path_to_value}\n'
                    f'  вход      : {json.dumps(actual["kwargs"], ensure_ascii=False, sort_keys=True)}\n'
                    f'  ожидалось : {want}\n'
                    f'  получено  : {got}\n'
                    f'  если изменение осознанное: ' + ACCEPT_HINT.format(func=function)
                )

    def test_build_amortization(self):
        self._assert_matches_golden('build_amortization')

    def test_simulate_lump_repayment(self):
        self._assert_matches_golden('simulate_lump_repayment')

    def test_calc_repayment_schedule(self):
        self._assert_matches_golden('calc_repayment_schedule')

    def test_all_functions_covered(self):
        """Каждая функция матрицы имеет свой golden-файл."""
        for function in FUNCTIONS:
            with self.subTest(function=function):
                self.assertTrue(
                    os.path.exists(golden_path(function)),
                    f'нет голдена для {function}: scripts/snapshot_golden.py --init',
                )


class KnownBugsRegistryTest(unittest.TestCase):
    """
    Реестр «недоплаточных» входов снежного кома.

    Эти кейсы остаются и в основном голдене `calc_repayment_schedule` — реестр не
    исключает их, а помечает: сегодня `min(annuity, budget)` обрезает аннуитет
    бюджетом, и тело долга не гасится вовсе.
    """

    def test_registry_matches_current_behaviour(self):
        self.assertTrue(
            os.path.exists(KNOWN_BUGS_PATH),
            'нет tests/golden/known_bugs.json: scripts/snapshot_golden.py --init',
        )
        with open(KNOWN_BUGS_PATH, encoding='utf-8') as fh:
            stored = json.load(fh)

        fresh = json.loads(render_known_bugs(build_known_bugs()))
        self.assertEqual(
            stored['case_count'], fresh['case_count'],
            'число недоплаточных кейсов изменилось — реестр известного бага устарел',
        )
        self.assertEqual(
            sorted(stored['cases']), sorted(fresh['cases']),
            'состав недоплаточных кейсов изменился — реестр известного бага устарел',
        )
        for case_id in sorted(stored['cases']):
            with self.subTest(case=case_id):
                self.assertEqual(
                    stored['cases'][case_id], fresh['cases'][case_id],
                    f'замеры кейса {case_id} разошлись с реестром known_bugs.json',
                )

    def test_roadmap_control_measurement(self):
        """
        Контрольный замер роадмапа: 8 000 000 ₽ @ 16 %, бюджет 40 000, extra_day 15.

        ДО И3 недоплата: 299 строк, Σ principal = 0.00, финальный баланс
        8 000 000.00 — аннуитет обрезался бюджетом (`min(annuity, budget)`),
        и тело долга не гасилось вовсе.

        ПОСЛЕ И3 (снято `min(annuity, budget)`, аннуитет платится полностью,
        бюджет идёт сверху) недоплаты не существует ни при каком бюджете:
        последний плановый платёж всегда добивает остаток. Тест закрепляет
        исправленное поведение и держит `extra_day=15` и `extra_day=None`
        неразличимыми — база начисления днём доплаты больше не переключается.
        """
        with open(KNOWN_BUGS_PATH, encoding='utf-8') as fh:
            stored = json.load(fh)
        control = stored['control']

        self.assertEqual(
            control['measured'], control['roadmap_expected'],
            'контрольный замер разошёлся с числами роадмапа',
        )
        self.assertTrue(control['matches_roadmap'])

        fixed = control['measured_with_extra_day_15']
        self.assertEqual(fixed['sum_principal'], 8_000_000.0)
        self.assertEqual(fixed['final_balance'], 0.0)

        ok = control['measured_with_extra_day_none']
        self.assertEqual(ok['sum_principal'], 8_000_000.0)
        self.assertEqual(ok['final_balance'], 0.0)
        self.assertEqual(
            fixed['total_interest'], ok['total_interest'],
            'monthly_extra_day снова переключает базу начисления (регрессия И3a-3)',
        )

    def test_every_registered_case_really_underpays(self):
        """Критерий реестра вычислен по факту: остаток не закрыт к концу графика."""
        with open(KNOWN_BUGS_PATH, encoding='utf-8') as fh:
            stored = json.load(fh)
        for case_id, case in sorted(stored['cases'].items()):
            with self.subTest(case=case_id):
                measured = case['measured']
                self.assertGreater(measured['final_balance'], 0.01)
                self.assertLess(measured['sum_principal'], case['kwargs']['loan_amount'])
                self.assertIsNotNone(case['kwargs']['monthly_budget'])


class RruleShortMonthTest(unittest.TestCase):
    """
    Поведение rrule(MONTHLY) на коротких месяцах — известное и осознанно оставленное.

    dateutil выводит день месяца из dtstart и просто ПРОПУСКАЕТ месяцы, в которых
    такого дня нет. При платеже 31-го числа выпадают и февраль, и все 30-дневные
    месяцы; при платеже 30-го — февраль. Пропущенный февраль не превращается в
    два платежа: получается один период в 59 дней, за который при месячной базе
    начисления берётся ровно один обычный месячный процент.

    Тест закрепляет это поведение, а не чинит его.
    """

    def test_day31_grid_skips_february_and_30day_months(self):
        first_dt = FIRST_DATES[3]           # 31.12.2025 → сетка с 31.01.2026
        self.assertEqual(first_dt, datetime(2025, 12, 31))

        next_dt, last_dt, dates = grid(first_dt, SHORT_TERM)
        self.assertEqual(next_dt, datetime(2026, 1, 31))

        # Окно в 24 месяца, а платежей всего 14 — 10 месяцев без 31-го числа выпали.
        self.assertEqual(len(dates), 14)
        self.assertEqual(
            [d.strftime('%d.%m.%Y') for d in dates],
            ['31.01.2026', '31.03.2026', '31.05.2026', '31.07.2026', '31.08.2026',
             '31.10.2026', '31.12.2026', '31.01.2027', '31.03.2027', '31.05.2027',
             '31.07.2027', '31.08.2027', '31.10.2027', '31.12.2027'],
        )
        self.assertNotIn(2, {d.month for d in dates}, 'февраль обязан отсутствовать')

        # Февраль пропущен целиком: между январём и мартом 59 дней одним периодом.
        self.assertEqual((dates[1] - dates[0]).days, 59)

    def test_day30_grid_skips_february(self):
        dates = list(rrule(MONTHLY,
                           dtstart=datetime(2026, 1, 30),
                           until=datetime(2026, 12, 30)))
        self.assertEqual(len(dates), 11, 'из 12 месяцев выпал только февраль')
        self.assertNotIn(2, {d.month for d in dates})
        self.assertEqual((dates[1] - dates[0]).days, 59)

    def test_relativedelta_clamps_day30_to_february_end(self):
        """
        Смежный факт: сетка строится как first + 1 месяц, а relativedelta день
        не пропускает, а прижимает. Поэтому FIRST_DATES[2] (30.01) вырождается
        в 28-е число и дальше ведёт себя как FIRST_DATES[1].
        """
        self.assertEqual(FIRST_DATES[2] + relativedelta(months=1), datetime(2026, 2, 28))
        self.assertEqual(grid(FIRST_DATES[2], SHORT_TERM)[0], datetime(2026, 2, 28))
        self.assertEqual(grid(FIRST_DATES[2], SHORT_TERM)[2],
                         grid(FIRST_DATES[1], SHORT_TERM)[2])

    def test_59_day_period_is_charged_as_one_monthly_period(self):
        """
        При adjust_business_days=False проценты считаются как balance * rate/12
        независимо от длины периода: за 59 дней берётся столько же, сколько за 28.
        """
        loan, rate = 3_000_000.0, 7.99
        next_dt, last_dt, dates = grid(FIRST_DATES[3], SHORT_TERM)
        payment = annuity(loan, rate, len(dates))

        schedule, _first, _total = build_amortization(
            loan, rate, next_dt, last_dt,
            adjust_business_days=False,
            prev_payment_date=FIRST_DATES[3],
            fixed_payment=payment,
        )

        self.assertEqual(schedule[0]['date'], '31.01.2026')
        self.assertEqual(schedule[1]['date'], '31.03.2026')

        monthly_rate = Decimal(str(rate)) / Decimal(100) / Decimal(12)
        balance_after_first = Decimal(str(schedule[0]['balance']))
        expected = (balance_after_first * monthly_rate).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP)

        self.assertEqual(schedule[1]['interest'], float(expected))


if __name__ == '__main__':
    unittest.main()
