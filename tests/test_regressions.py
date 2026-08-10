"""
Регрессии по находкам код-ревью (ревью снято на коммите 193edc6).

По одному классу на находку. В докстринге каждого теста стоит РОВНО тот вход,
на котором находка воспроизводилась, — чтобы через полгода было видно не только
«что проверяем», но и «на чём это ломалось».

Что чинилось:

    1  `balance_after_deposit` индексировал график длиной сетки дат → IndexError
    2  аннуитет при ставке 0 % делил на ноль → decimal.InvalidOperation
    3  сценарий с несостоявшимся событием выигрывал конкурс, ничего не сделав
    4  «сэкономлено месяцев» считалось от длины СЕТКИ, а не от базового графика
    6  дренаж не различал «событие невозможно» и «событие не понадобилось»
    9  `_accrue` в базе daily начислял проценты за отрицательные дни
   10  осиротевшая константа `_CENT` в calculator.py
   11  `balance_after_deposit` никем не читался
   13  договор короче месяца ронял POST /api/mortgage с IndexError и 500-й

Находка 5 (бейдж досрочки в app.js) чинится отдельно и здесь не проверяется.
Находки 7 и 8 закрылись сами на И3 — тесты ниже это фиксируют.
Находка 12 осознанно НЕ чинилась, см. отчёт и комментарий в
``LumpUnusedIsConservedTest``.

Запуск::

    PYTHONPATH=web python -m unittest discover -s tests
"""
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal

from app import calculator, engine
from app.calculator import build_amortization, run_comparison, simulate_snowball
from app.engine import (
    AMOUNT_BUDGET,
    BASIS_DAILY,
    BASIS_MONTHLY,
    KIND_LUMP,
    KIND_RECURRING,
    MODE_TERM,
    ROW_EARLY,
    STATUS_NOT_APPLICABLE,
    STATUS_OK,
    MortgageState,
    RepaymentEvent,
    StrategyResult,
    simulate_strategy,
    _accrue,
    _d,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Вход находок 1, 3 и 4: остаток 1 000 000 ₽ под 10 %, сетка 02.02.2026 →
# 02.01.2031 (60 дат), а введённый платёж 50 000 ₽ вдвое больше аннуитета
# (~21 247 ₽), поэтому РЕАЛЬНЫЙ график — 22 строки, не 60.
# `first_payment_date` — уже сделанный платёж, сетка идёт от него плюс месяц.
FAST_MORTGAGE = {
    'loan_amount': 1_000_000.0,
    'annual_rate': 10.0,
    'first_payment_date': '2026-01-02',
    'last_payment_date': '2031-01-02',
    'monthly_payment': 50_000.0,
    'adjust_business_days': 0,
}
DEPOSIT_36M = {'annual_rate': 8.0, 'term_months': 36, 'capitalization': 1}


class MoneyMixin:
    def assertMoney(self, actual, expected, msg=None, delta=0.01):
        self.assertAlmostEqual(float(actual), float(expected), delta=delta, msg=msg)


# ---------------------------------------------------------------------------
# Находка 1 + 11 — срок вклада длиннее реального графика
# ---------------------------------------------------------------------------

class DepositTermBeyondScheduleTest(MoneyMixin, unittest.TestCase):
    """
    Срок вклада больше, чем РЕАЛЬНЫЙ график ипотеки.

    Вход находки: остаток 1 000 000 ₽, 10 %, 02.02.2026 → 02.01.2031 (60 дат),
    платёж 50 000 ₽ (аннуитет ~21 247 ₽) — график закрывается за 22 платежа;
    вклад на 36 месяцев, разовая сумма 100 000 ₽. Старый код брал
    `base_schedule[36 − 1]` из графика длиной 22 → IndexError → 500 на
    POST /api/comparison.
    """

    def test_grid_is_longer_than_the_real_schedule(self):
        """Проверка самого входа: сетка 60 дат, график 22 строки."""
        result = run_comparison(dict(FAST_MORTGAGE), dict(DEPOSIT_36M), {'lump_sum': 100_000.0})
        self.assertEqual(len(result['base_schedule']), 22)

    def test_comparison_does_not_crash(self):
        result = run_comparison(dict(FAST_MORTGAGE), dict(DEPOSIT_36M), {'lump_sum': 100_000.0})
        self.assertIn('winner', result)

    def test_deposit_term_is_clamped_to_the_real_schedule(self):
        """
        Вклад не может пережить ипотеку: срок зажимается длиной графика (22),
        а не длиной сетки дат (60) и не введёнными 36 месяцами.
        """
        result = run_comparison(dict(FAST_MORTGAGE), dict(DEPOSIT_36M), {'lump_sum': 100_000.0})
        expected, _final = calculator.calc_deposit(100_000.0, 8.0, 22, 1)
        self.assertMoney(result['deposit_income'], expected,
                         'доход вклада посчитан не по зажатому сроку')

    def test_balance_after_deposit_is_gone(self):
        """Находка 11: поле никем не читалось — ни в БД, ни в ответе, ни в app.js."""
        result = run_comparison(dict(FAST_MORTGAGE), dict(DEPOSIT_36M), {'lump_sum': 100_000.0})
        self.assertNotIn('balance_after_deposit', result)


# ---------------------------------------------------------------------------
# Находка 2 — беспроцентный договор
# ---------------------------------------------------------------------------

class ZeroRateTest(MoneyMixin, unittest.TestCase):
    """
    Ставка 0 %: `factor = (1 + 0)^n = 1`, знаменатель `factor − 1` = 0.

    Входы находки: `annual_rate=0` плюс любая досрочка в `reduce_payment`, и
    `contract_payment=None` при нулевой ставке — последний ронял даже пустой
    прогон без единого события.
    """

    STATE = dict(loan_amount=1_000_000.0, annual_rate=0.0,
                 first_payment_date='02.02.2026', last_payment_date='02.01.2031',
                 prev_payment_date='02.01.2026')

    def test_empty_run_without_contract_payment(self):
        result = simulate_strategy(MortgageState(**self.STATE), [])
        self.assertEqual(len(result.dates), 60)
        self.assertMoney(result.total_interest, 0.0, 'беспроцентный договор без процентов')
        self.assertMoney(result.monthly_payment, 1_000_000.0 / 60,
                         'аннуитет при нулевой ставке — тело, делённое на число периодов')
        self.assertMoney(result.schedule[-1]['balance'], 0.0, 'кредит обязан закрыться')

    def test_lump_in_reduce_payment(self):
        """Досрочка с пересчётом платежа: `_apply_mode` зовёт аннуитет второй раз."""
        event = RepaymentEvent(amount=100_000.0, at=datetime(2026, 5, 2))
        result = simulate_strategy(MortgageState(**self.STATE), [event])
        self.assertMoney(result.total_interest, 0.0)

        early_index = next(i for i, row in enumerate(result.schedule)
                           if row['row_kind'] == ROW_EARLY)
        left = result.schedule[early_index]['balance']
        periods = len(result.dates) - sum(1 for row in result.schedule[:early_index]
                                          if row['row_kind'] != ROW_EARLY)
        self.assertMoney(result.monthly_payment, left / periods,
                         'платёж после досрочки: остаток на остаток срока')
        self.assertMoney(result.schedule[-1]['balance'], 0.0)

    def test_comparison_with_zero_rate(self):
        mortgage = dict(FAST_MORTGAGE, annual_rate=0.0, monthly_payment=16_666.67)
        result = run_comparison(mortgage, dict(DEPOSIT_36M),
                                {'lump_sum': 100_000.0, 'lump_sum_date': '2026-05-02'})
        self.assertMoney(result['baseline_total_interest'], 0.0)
        self.assertEqual(result['winner'], 'deposit',
                         'без процентов экономить нечего — выигрывает вклад')


# ---------------------------------------------------------------------------
# Находка 3 — несостоявшийся сценарий в конкурсе победителей
# ---------------------------------------------------------------------------

class NotApplicableIsOutOfTheContestTest(unittest.TestCase):
    """
    Дата досрочки за концом графика: событие не состоится, график сценария
    побайтово равен базовому, экономия ровно 0,00 ₽. Ноль больше любой
    отрицательной величины, поэтому `max` по `options` объявлял победителем
    досрочку, которой не будет (решение 6 роадмапа).

    Вход находки: остаток 1 000 000 ₽, 10 %, платёж 50 000 ₽, вклад 12 месяцев
    под 8 %, разовая сумма 100 000 ₽, `lump_sum_date='02.05.2032'`.
    """

    def setUp(self):
        self.result = run_comparison(
            dict(FAST_MORTGAGE), dict(DEPOSIT_36M, term_months=12),
            {'lump_sum': 100_000.0, 'lump_sum_date': '2032-05-02'})

    def test_input_really_produces_a_dead_scenario(self):
        """Проверка входа: оба арма досрочки не состоялись и равны базе."""
        self.assertEqual(self.result['reduce_payment_status'], STATUS_NOT_APPLICABLE)
        self.assertEqual(self.result['reduce_term_status'], STATUS_NOT_APPLICABLE)
        self.assertEqual(self.result['reduce_payment_schedule'], self.result['base_schedule'])
        self.assertEqual(self.result['options']['reduce_payment'], 0.0)

    def test_winner_is_not_a_dead_scenario(self):
        self.assertNotIn(self.result['winner'], ('reduce_payment', 'reduce_term'),
                         'победителем объявлена досрочка, которой не будет')
        self.assertEqual(self.result['winner'], 'deposit')

    def test_statuses_and_contest_pool_are_visible(self):
        self.assertEqual(self.result['option_statuses']['deposit'], STATUS_OK)
        self.assertEqual(set(self.result['options_applicable']), {'deposit'},
                         'в конкурсе обязан остаться только состоявшийся сценарий')

    def test_deposit_stays_in_the_contest_when_its_income_is_real(self):
        """
        Граница правила: у арма вклада ипотечное событие тоже может не
        состояться — кредит закрывается раньше, чем созревает вклад (срок 36
        месяцев против графика в 22 платежа). Проценты по вкладу при этом
        реально заработаны, и выбрасывать такой арм из конкурса нельзя.
        """
        result = run_comparison(dict(FAST_MORTGAGE), dict(DEPOSIT_36M),
                                {'lump_sum': 100_000.0})
        self.assertEqual(result['deposit_status'], STATUS_NOT_APPLICABLE,
                         'вход подобран неверно: погашение из вклада обязано не состояться')
        self.assertGreater(result['options']['deposit'], 0.0)
        self.assertIn('deposit', result['options_applicable'],
                      'вклад заработал проценты — из конкурса его убирать нельзя')

    def test_live_scenario_still_competes(self):
        """Та же ипотека с достижимой датой: сценарий досрочки снова в конкурсе."""
        live = run_comparison(dict(FAST_MORTGAGE), dict(DEPOSIT_36M, term_months=12),
                              {'lump_sum': 100_000.0, 'lump_sum_date': '2026-05-02'})
        self.assertEqual(live['reduce_term_status'], STATUS_OK)
        self.assertIn('reduce_term', live['options_applicable'])
        self.assertEqual(live['winner'], 'reduce_term')


# ---------------------------------------------------------------------------
# Находка 4 — «сэкономлено месяцев» от длины сетки
# ---------------------------------------------------------------------------

class MonthsSavedCountsRealMonthsTest(unittest.TestCase):
    """
    `original_n − months_to_payoff` смешивал длину СЕТКИ дат с реальными
    месяцами графика.

    Вход находки: остаток 1 000 000 ₽, 10 %, 60 дат, платёж 50 000 ₽, досрочка
    100 000 ₽ на 02.05.2026. База — 22 строки, сценарий закрывается за 20
    месяцев, то есть экономия 2 месяца, а карточка показывала 39.
    """

    def setUp(self):
        self.result = run_comparison(dict(FAST_MORTGAGE), dict(DEPOSIT_36M),
                                     {'lump_sum': 100_000.0, 'lump_sum_date': '2026-05-02'})

    def test_months_saved(self):
        self.assertEqual(self.result['reduce_term_months_to_payoff'], 20)
        self.assertEqual(self.result['reduce_term_months_saved'], 2)

    def test_card_numbers_agree_with_each_other(self):
        """Числа одной карточки обязаны сходиться с базовым графиком."""
        baseline_months = len(self.result['base_schedule'])
        self.assertEqual(
            self.result['reduce_term_months_saved'],
            baseline_months - self.result['reduce_term_months_to_payoff'],
        )

    def test_without_lump_nothing_is_saved(self):
        """Без досрочки срок равен базовому, а не длине сетки дат."""
        result = run_comparison(dict(FAST_MORTGAGE), dict(DEPOSIT_36M), {})
        self.assertEqual(result['reduce_term_months_saved'], 0)
        self.assertEqual(result['reduce_term_months_to_payoff'],
                         len(result['base_schedule']))


# ---------------------------------------------------------------------------
# Находка 6 — «невозможно» против «не понадобилось»
# ---------------------------------------------------------------------------

class DrainTellsImpossibleFromUnneededTest(MoneyMixin, unittest.TestCase):
    """
    `status='not_applicable'` означает ровно одно: стратегия не сделала НИЧЕГО и
    её график равен базовому. Кредит, закрытый раньше даты досрочки, — штатный
    исход, а не несостоявшийся сценарий.

    Вход находки: 3 000 000 ₽ под 8 %, 60 дат, ежемесячные доплаты, `reduce_term`.
    """

    LOAN, RATE = 3_000_000.0, 8.0
    FIRST, LAST = '02.01.2026', '02.01.2031'

    def test_recurring_closing_the_loan_early_is_ok(self):
        result = simulate_snowball(self.LOAN, self.RATE, self.FIRST, self.LAST,
                                   lump_sum=0.0, lump_idx=0,
                                   monthly_budget=120_000.0, monthly_idx=0,
                                   mode=MODE_TERM, contract_payment=60_000.0)
        self.assertLess(result.months_to_payoff, len(result.dates),
                        'вход подобран неверно: кредит обязан закрыться раньше сетки')
        self.assertEqual(result.status, STATUS_OK,
                         'кредит закрыт досрочно — это не «сценарий не состоялся»')
        self.assertMoney(result.lump_unused, 0.0,
                         'неистраченный месячный бюджет не является разовой суммой')

    def test_lump_that_was_not_needed_keeps_status_ok(self):
        """Ежемесячные доплаты закрыли кредит раньше даты разовой досрочки."""
        result = simulate_snowball(self.LOAN, self.RATE, self.FIRST, self.LAST,
                                   lump_sum=500_000.0, lump_idx=55,
                                   monthly_budget=100_000.0, monthly_idx=0,
                                   mode=MODE_TERM, contract_payment=60_000.0)
        self.assertEqual(result.status, STATUS_OK, 'сценарий состоялся — доплаты работали')
        self.assertMoney(result.lump_unused, 500_000.0,
                         'разовая сумма не потрачена и обязана быть видна')

    def test_nothing_happened_is_not_applicable(self):
        """Одна разовая досрочка за концом графика: не сделано ничего."""
        result = simulate_snowball(self.LOAN, self.RATE, self.FIRST, self.LAST,
                                   lump_sum=500_000.0, lump_idx=999,
                                   monthly_budget=0.0, monthly_idx=0,
                                   mode=MODE_TERM, contract_payment=60_000.0)
        self.assertEqual(result.status, STATUS_NOT_APPLICABLE)
        self.assertMoney(result.lump_unused, 500_000.0)


# ---------------------------------------------------------------------------
# Находка 7 — вид события читается движком (закрылась на И3, фиксируем)
# ---------------------------------------------------------------------------

class RecurringEventIsNotASingleLumpTest(unittest.TestCase):
    """
    `kind='recurring'` обязан раскрываться по сетке в множество применений.
    До И3 движок его не читал, и ежемесячная доплата молча работала как одна
    разовая досрочка (ровно одна строка графика вместо десятков).
    """

    def test_recurring_produces_many_early_rows(self):
        state = MortgageState(loan_amount=1_000_000.0, annual_rate=10.0,
                              first_payment_date='02.02.2026',
                              last_payment_date='02.01.2031',
                              prev_payment_date='02.01.2026',
                              contract_payment=21_247.04)
        event = RepaymentEvent(amount=30_000.0, kind=KIND_RECURRING,
                               amount_kind=AMOUNT_BUDGET, mode=MODE_TERM)
        result = simulate_strategy(state, [event])
        early = [row for row in result.schedule if row['row_kind'] == ROW_EARLY]
        self.assertGreater(len(early), 1,
                           'ежемесячная доплата отработала как одна разовая досрочка')
        self.assertLess(result.months_to_payoff, len(result.dates))


# ---------------------------------------------------------------------------
# Находка 9 — отрицательные дни в базе daily
# ---------------------------------------------------------------------------

class AccrueIgnoresNegativeDaysTest(MoneyMixin, unittest.TestCase):
    """
    Отрезок отрицательной длины начислял ОТРИЦАТЕЛЬНЫЕ проценты в базе daily и
    ровно ноль в базе monthly.

    Вход находки: `prev_payment_date=20.02.2026` при первой дате платежа
    02.02.2026 → −4 931,51 ₽ в daily против 0,00 ₽ в monthly.
    Через API сегодня недостижимо, но И4 начнёт двигать даты.
    """

    def test_both_bases_agree_on_a_reversed_segment(self):
        start = datetime(2026, 2, 20)
        end = datetime(2026, 2, 2)
        segments = [(start, end, _d(1_000_000))]
        monthly_rate = _d(10) / _d(100) / _d(12)
        daily_rate = _d(10) / _d(100) / _d(365)

        daily = _accrue(segments, BASIS_DAILY, (start, end), monthly_rate, daily_rate)
        monthly = _accrue(segments, BASIS_MONTHLY, (start, end), monthly_rate, daily_rate)

        self.assertMoney(daily, 0.0, 'отрицательные дни начислили проценты')
        self.assertMoney(monthly, 0.0)
        self.assertEqual(daily, monthly, 'базы разошлись на пустом месте')

    def test_positive_segment_still_accrues(self):
        """Охранник не должен глушить нормальный отрезок."""
        start = datetime(2026, 2, 2)
        end = datetime(2026, 3, 2)
        segments = [(start, end, _d(1_000_000))]
        daily_rate = _d(10) / _d(100) / _d(365)
        value = _accrue(segments, BASIS_DAILY, (start, end),
                        _d(10) / _d(100) / _d(12), daily_rate)
        self.assertMoney(value, 1_000_000 * 0.10 / 365 * 28)


# ---------------------------------------------------------------------------
# Находки 8, 10, 11 — уборка мёртвого контракта
# ---------------------------------------------------------------------------

class DeadContractIsGoneTest(unittest.TestCase):
    """
    Поля и константы, которые никто не читал. Тест держит уборку: вернуть их
    обратно можно только вместе с настоящим потребителем.
    """

    def test_carried_interest_is_not_a_result_field(self):
        """
        Находка 8: поле присваивалось и обнулялось внутри одной итерации, наружу
        выходило структурным нулём. Само поведение проверяется по графику —
        tests/test_early_repayment_allocation.py.
        """
        self.assertNotIn('carried_interest', StrategyResult.__dataclass_fields__)

    def test_calculator_has_no_orphan_cent(self):
        """Находка 10: единственный потребитель `_CENT` уехал в движок."""
        self.assertFalse(hasattr(calculator, '_CENT'))
        self.assertEqual(engine._CENT, Decimal('0.01'), 'в движке константа нужна')


# ---------------------------------------------------------------------------
# Находка 12 — осознанно не чинится, фиксируем действующую семантику
# ---------------------------------------------------------------------------

class LumpUnusedIsConservedTest(MoneyMixin, unittest.TestCase):
    """
    Находка 12 признана верной, но НЕ чинится здесь: `lump_unused` — величина с
    законом сохранения «Σ early + lump_unused == сумма события», который
    проверяется на всей матрице И0 (tests/test_engine.py) и приколочен точным
    числом в tests/test_cash_parity.py. Проценты закрывающего периода —
    отдельный денежный поток, он уже сидит в `total_interest` и в строке
    графика; вычесть их из `lump_unused` значит сломать закон сохранения.

    Врёт не число, а подпись «не понадобилось +X ₽» в карточке. Тест фиксирует
    ровно ту величину, на которую подпись расходится с наличными.

    Вход: остаток 100 000 ₽ под 16 %, платёж 50 000 ₽, досрочка 200 000 ₽
    20.01.2026 (внутри периода 02.01 → 02.02). Тело закрывается досрочкой,
    закрывающая строка предъявляет 774,19 ₽ процентов, а `lump_unused`
    показывает 100 000 ₽ вместо 99 225,81 ₽ реально вернувшихся денег.
    """

    def _run(self):
        state = MortgageState(loan_amount=100_000.0, annual_rate=16.0,
                              first_payment_date='02.02.2026',
                              last_payment_date='02.01.2031',
                              prev_payment_date='02.01.2026',
                              contract_payment=50_000.0)
        event = RepaymentEvent(amount=200_000.0, at=datetime(2026, 1, 20), kind=KIND_LUMP)
        return simulate_strategy(state, [event])

    def test_conservation_law_holds(self):
        result = self._run()
        applied = sum(row['early'] for row in result.schedule)
        self.assertMoney(applied + result.lump_unused, 200_000.0,
                         'закон сохранения суммы события нарушен')

    def test_closing_interest_is_charged_separately(self):
        result = self._run()
        self.assertMoney(result.total_interest, 774.19,
                         'проценты закрывающего периода обязаны быть предъявлены')
        self.assertMoney(result.lump_unused, 100_000.0)
        self.assertMoney(result.lump_unused - result.total_interest, 99_225.81,
                         'разрыв между подписью «не понадобилось» и наличными')


# ---------------------------------------------------------------------------
# Находка 13 — договор короче месяца
# ---------------------------------------------------------------------------

class ShortContractCalculatorTest(unittest.TestCase):
    """
    Вход находки: первый платёж 02.02.2026, последний 15.02.2026. Сетка строится
    от `first + 1 месяц`, `rrule` возвращает пустой список, и `schedule[0]`
    роняет `build_amortization` с IndexError.
    """

    def test_build_amortization_survives_an_empty_grid(self):
        schedule, payment, total = build_amortization(
            1_000_000.0, 10.0, datetime(2026, 3, 2), datetime(2026, 2, 15),
            prev_payment_date=datetime(2026, 2, 2), fixed_payment=50_000.0)
        self.assertEqual(schedule, [])
        self.assertEqual(payment, 0.0)
        self.assertEqual(total, 0.0)


class MortgageRouteValidationTest(unittest.TestCase):
    """
    Те же входы через POST /api/mortgage: роут обязан отвечать 400 с внятным
    текстом, а не 500 со стек-трейсом.

    Приложение поднимается на ВРЕМЕННОЙ базе: db/mortgage_web.db содержит живую
    историю пользователя и в тестах не трогается.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp(prefix='mortgage-regressions-')
        cls._old_db_path = os.environ.get('DB_PATH')
        os.environ['DB_PATH'] = os.path.join(cls._tmpdir, 'test.db')
        if os.path.join(REPO_ROOT, 'web') not in sys.path:
            sys.path.insert(0, os.path.join(REPO_ROOT, 'web'))
        from app.main import create_app     # импорт строго после подстановки DB_PATH
        cls.client = create_app().test_client()

    @classmethod
    def tearDownClass(cls):
        if cls._old_db_path is None:
            os.environ.pop('DB_PATH', None)
        else:
            os.environ['DB_PATH'] = cls._old_db_path
        shutil.rmtree(cls._tmpdir, ignore_errors=True)

    BODY = {
        'loan_amount': 1_000_000,
        'annual_rate': 10,
        'first_payment_date': '02.02.2026',
        'last_payment_date': '02.01.2031',
        'monthly_payment': 21_247.04,
    }

    def _post(self, **over):
        return self.client.post('/api/mortgage', json={**self.BODY, **over})

    def test_valid_contract_is_accepted(self):
        response = self._post()
        self.assertEqual(response.status_code, 200, response.get_json())

    def test_contract_shorter_than_a_month_is_rejected(self):
        response = self._post(first_payment_date='02.02.2026',
                              last_payment_date='15.02.2026')
        self.assertEqual(response.status_code, 400,
                         'договор короче месяца обязан отсекаться валидацией, а не падать')
        self.assertIn('месяц', response.get_json()['error'])

    def test_exactly_one_month_is_accepted(self):
        """Граница: ровно один предстоящий платёж — договор действительный."""
        response = self._post(first_payment_date='02.02.2026',
                              last_payment_date='02.03.2026',
                              monthly_payment=1_010_000)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()['payment_count'], 1)

    def test_zero_rate_is_a_valid_input(self):
        """Находка 2: 0 % — беспроцентная рассрочка, а не «поле не заполнено»."""
        response = self._post(annual_rate=0)
        self.assertEqual(response.status_code, 200, response.get_json())

    def test_negative_rate_is_rejected(self):
        response = self._post(annual_rate=-1)
        self.assertEqual(response.status_code, 400)
        self.assertIn('ставка', response.get_json()['error'].lower())

    def test_missing_field_is_still_rejected(self):
        response = self.client.post('/api/mortgage', json={
            k: v for k, v in self.BODY.items() if k != 'annual_rate'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('annual_rate', response.get_json()['error'])

    def test_non_positive_money_is_rejected(self):
        for field in ('loan_amount', 'monthly_payment'):
            with self.subTest(field=field):
                response = self._post(**{field: 0})
                self.assertEqual(response.status_code, 400)


if __name__ == '__main__':
    unittest.main()
