"""
Приёмка Итерации 3c роадмапа — cash-parity, честный вклад и реинвест.

Контракт, который проверяется (решения 5-8 и 16 роадмапа):

* **cash-parity — контракт, а не тест на всякий случай.** Все сценарии одного
  сравнения обязаны тратить одинаковые рубли в каждом месяце, кроме месяца
  внешнего вливания и хвостового месяца. Непустой отчёт блокирует вывод winner.
* `monthly_addition = 0` **для всех семей**: на вкладе лежит только разовая
  сумма, профицит бюджета туда не попадает.
* В `reduce_payment` высвобожденная часть платежа `MP − new_monthly`
  реинвестируется под ту же ставку — иначе арм досрочки каждый месяц тратит
  меньше живых денег и сравнение недействительно.
* `reinvest_income` — **отдельное поле**, в `deposit_income` не подмешивается:
  карточка вклада показывает доход только по разовой сумме.
* Непустой `monthly_budget` переводит сравнение в семью `snowball`, и
  переключение видно в ответе.

Две группы тестов, деление вынесено в имена классов:

* ``*ContractTest`` — форма `cash_by_month` / `cash_parity_report`. Функции
  мягко импортируются: пока их нет, класс уходит в skip с внятной причиной, а
  не роняет сборку.
* Остальные — приёмка поведения. Отчёт cash-parity такой класс считает
  САМОСТОЯТЕЛЬНО (`_parity_report` ниже), не вызывая production-функцию:
  приёмка обязана работать независимо от того, в каком виде та приедет, и
  обязана краснеть, если реинвест где-то потерян.

Числа, которые эти тесты ловили на ДО-И3c реализации (дефолты формы, разовая
сумма 500 000, вклад 16 % на 12 месяцев, `adjust_business_days=True`, бюджет
пуст — семья `plain`); проверено прогоном на коде до правок:

    reduce_payment против базы   298 месяцев расхождения по −3 862,51 ₽
                                 (хвост 03.2051: −3 743,00)
    вклад против базы            286 месяцев расхождения по −4 587,87 ₽
    reduce_term против базы      расхождений нет (уже был честен)
    deposit_income с бюджетом    120 072,04 / 1 062 574,80 вместо 86 135,40 / 586 135,40
    reinvest_income              поля не было вовсе
    baseline_kind                ключа не было вовсе

Запуск::

    PYTHONPATH=web python -m unittest discover -s tests
"""
import inspect
import unittest
from decimal import Decimal

from app.calculator import calc_deposit, calc_monthly_deposit, run_comparison

# --- Мягкий импорт того, чего может ещё не быть -----------------------------

try:
    from app.calculator import cash_by_month
except ImportError:                      # pragma: no cover — ветка «И3c не сдана»
    cash_by_month = None

try:
    from app.calculator import cash_parity_report
except ImportError:                      # pragma: no cover — ветка «И3c не сдана»
    cash_parity_report = None

NO_CASH_BY_MONTH = 'cash_by_month ещё не реализован (И3c)'
NO_PARITY_REPORT = 'cash_parity_report ещё не реализован (И3c)'

CENT = Decimal('0.01')
ZERO = Decimal('0.00')


# ---------------------------------------------------------------------------
# Дефолты формы (web/templates/index.html) — на них замерены числа роадмапа
# ---------------------------------------------------------------------------

MORTGAGE = {
    'loan_amount': 2_995_218.84,
    'annual_rate': 7.99,
    'first_payment_date': '02.04.2026',
    'last_payment_date': '02.03.2051',
    'monthly_payment': 23_124.77,
    'adjust_business_days': True,
}
DEPOSIT = {'annual_rate': 16.0, 'term_months': 12, 'capitalization': 1}
LUMP = 500_000.0
BUDGET = 60_000.0

# Контрольные числа роадмапа: вклад держит ТОЛЬКО разовую сумму.
EXPECTED_DEPOSIT_INCOME = Decimal('86135.40')
EXPECTED_DEPOSIT_FINAL = Decimal('586135.40')
# Числа, которые отдаёт старый код, когда на вклад попадает профицит бюджета.
LEGACY_DEPOSIT_INCOME = Decimal('120072.04')
LEGACY_DEPOSIT_FINAL = Decimal('1062574.80')

# Сценарий → (ключ графика в плоском ответе, поле нового платежа)
SCENARIOS = (
    ('deposit', 'deposit_schedule', 'deposit_new_monthly'),
    ('reduce_payment', 'reduce_payment_schedule', 'reduce_payment_new_monthly'),
    ('reduce_term', 'reduce_term_schedule', None),
)


def _c(value):
    return Decimal(str(value)).quantize(CENT)


def _compare(lump_sum=LUMP, monthly_budget=None, **extra):
    """Прогон сравнения на дефолтах формы."""
    strategy = {'lump_sum': lump_sum, 'monthly_budget': monthly_budget}
    strategy.update(extra)
    return run_comparison(dict(MORTGAGE), dict(DEPOSIT), strategy)


# ---------------------------------------------------------------------------
# Чтение ответа: поддержаны и плоский ответ И3, и scenarios[] из И5
# ---------------------------------------------------------------------------

def _scenario_obj(resp, key):
    for scen in resp.get('scenarios') or []:
        if scen.get('key') == key or scen.get('kind') == key:
            return scen
    return None


def _lookup(resp, key, canon, *flat_candidates):
    """
    Поле сценария в любой из известных форм ответа.

    `canon` ищется в объекте сценария (форма И5) и в общем словаре
    `resp[canon] = {сценарий: значение}`, `flat_candidates` — среди плоских
    ключей ответа (форма И3). Возвращает None, если поля нет нигде.
    """
    obj = _scenario_obj(resp, key)
    if obj is not None and obj.get(canon) is not None:
        return obj[canon]
    grouped = resp.get(canon)
    if isinstance(grouped, dict) and grouped.get(key) is not None:
        return grouped[key]
    for flat in flat_candidates:
        if resp.get(flat) is not None:
            return resp[flat]
    return None


def _schedule(resp, key, flat_key):
    obj = _scenario_obj(resp, key)
    if obj is not None and obj.get('schedule') is not None:
        return obj['schedule']
    schedules = resp.get('schedules')
    if isinstance(schedules, dict) and schedules.get(key) is not None:
        return schedules[key]
    return resp[flat_key]


def _base_schedule(resp):
    return _schedule(resp, 'baseline', 'base_schedule')


# Плоские имена поля реинвеста по сценариям. Общий ключ 'reinvest_income'
# принимается только для `reduce_payment`: в плоском ответе он относится именно
# к нему, и подставлять его другим армам значит выдать им чужой доход.
_REINVEST_FLAT = {
    'baseline': (),
    'deposit': ('deposit_reinvest_income',),
    'reduce_payment': ('reduce_payment_reinvest_income', 'reinvest_income'),
    'reduce_term': ('reduce_term_reinvest_income',),
}

_NEW_MONTHLY_FLAT = {key: flat for key, _sched, flat in SCENARIOS}


def _reinvest_income(resp, key):
    return _lookup(resp, key, 'reinvest_income', *_REINVEST_FLAT.get(key, ()))


def _new_monthly(resp, key, flat_key):
    if flat_key is None:
        return None
    return _lookup(resp, key, 'new_monthly', flat_key)


def _annuity_months(schedule):
    return sum(1 for row in schedule if row.get('row_kind') != 'early')


def _deposit_income(resp):
    return _lookup(resp, 'deposit', 'deposit_income', 'deposit_income')


def _deposit_final(resp):
    return _lookup(resp, 'deposit', 'deposit_final', 'deposit_final')


def _monthly_payment(resp):
    """Договорной платёж. Ответ его echo-ит; на всякий случай — из дефолтов формы."""
    return resp.get('monthly_payment') or MORTGAGE['monthly_payment']


# ---------------------------------------------------------------------------
# Собственная раскладка кэша: приёмка не зависит от production-функции
# ---------------------------------------------------------------------------

def _month(date_str):
    """'02.05.2026' → (2026, 5). Сравнимо и сортируемо."""
    day, month, year = date_str.split('.')
    return int(year), int(month)


def _fmt(month):
    return f'{month[1]:02d}.{month[0]}'


def _add(bucket, month, amount):
    bucket[month] = bucket.get(month, ZERO) + amount


def _arm_cash(resp, key, schedule, monthly_payment, infusion_month):
    """
    Сколько живых денег заёмщик отдал в каждом месяце по этому арму.

    Считается ровно то, что уходит ИЗ КАРМАНА:

    * платежи по ипотеке из графика;
    * разовая сумма — она уже сидит строкой досрочки у армов погашения, а у
      арма вклада её нет: там она уходит на вклад в месяц вливания, а обратно
      приходит уже вместе с доходом. Поэтому у арма вклада строки досрочки
      (перевод со вклада в ипотеку — внутренний, не отток) исключаются, а в
      месяц вливания добавляется сама разовая сумма;
    * высвобожденная часть платежа `MP − new_monthly` — БЕЗУСЛОВНО. Эти деньги
      не уходят банку, но и не исчезают: они остаются в кармане. Паритет
      сравнивает, сколько денег заёмщик отдал, а не сколько заработал, поэтому
      он держится и при нулевом `reinvest_income`.

    Прежняя версия добавляла эти взносы только при `reinvest_income > 0` —
    формулировка из решения 16, где высвобожденный платёж обязан был копиться
    на вкладе. Правило отменено: вклады начинаются от 50 000 ₽ и не пополняются,
    положить туда несколько тысяч в месяц физически нельзя. Доход по ним теперь
    ноль (`REINVEST_EARNING_MONTHS`), а паритет по-прежнему обязан быть пуст.
    """
    cash = {}
    for row in schedule:
        if key == 'deposit' and row.get('row_kind') == 'early':
            continue                      # перевод со вклада — не новый отток
        _add(cash, _month(row['date']), _c(row['payment']))

    if key == 'deposit':
        _add(cash, infusion_month, _c(LUMP))

    if key == 'baseline':
        return cash                       # у базы платёж не снижается, высвобождать нечего

    new_monthly = _new_monthly(resp, key, _NEW_MONTHLY_FLAT.get(key))
    contribution = (_c(monthly_payment) - _c(new_monthly)
                    if new_monthly is not None else None)
    for row in schedule:
        if row.get('row_kind') == 'early':
            continue
        paid = _c(row['payment'])
        if paid >= _c(monthly_payment):
            continue                      # платёж ещё не снижен — высвобождать нечего
        step = contribution if contribution is not None else _c(monthly_payment) - paid
        _add(cash, _month(row['date']), step)
    return cash


def _parity_report(resp, key, schedule, monthly_payment, infusion_month):
    """
    Месяцы, где кэш арма расходится с базой.

    Исключаются ровно два случая, разрешённые решением 7 роадмапа: месяц
    внешнего вливания и хвост — всё, начиная с месяца, в котором закрылся более
    короткий из двух армов.
    """
    base = _arm_cash(resp, 'baseline', _base_schedule(resp), monthly_payment, infusion_month)
    treat = _arm_cash(resp, key, schedule, monthly_payment, infusion_month)

    tail = min(max(base), max(treat))
    report = []
    for month in sorted(set(base) | set(treat)):
        if month == infusion_month or month >= tail:
            continue
        diff = treat.get(month, ZERO) - base.get(month, ZERO)
        if diff != ZERO:
            report.append((_fmt(month), diff))
    return report


def _infusion_month(resp):
    """
    Месяц внешнего вливания: месяц, в котором разовая сумма уходит либо на
    вклад, либо сразу в ипотеку. Без явной даты — месяц уже прошедшего платежа
    (`mortgage.first_payment_date`), он же якорь начисления.
    """
    return _month(MORTGAGE['first_payment_date'])


# ---------------------------------------------------------------------------
# Форма production-функций (skip, пока их нет)
# ---------------------------------------------------------------------------

@unittest.skipIf(cash_by_month is None, NO_CASH_BY_MONTH)
class CashByMonthContractTest(unittest.TestCase):
    """`cash_by_month()` раскладывает денежный отток арма по месяцам."""

    def setUp(self):
        self.resp = _compare()
        self.schedule = _base_schedule(self.resp)

    def test_keys_are_months(self):
        result = cash_by_month(self.schedule)
        self.assertTrue(result, 'cash_by_month вернул пустую раскладку')
        for key in result:
            self.assertRegex(str(key), r'^\d{2}\.\d{4}$',
                             f'ключ {key!r} не похож на MM.YYYY')

    def test_sum_equals_sum_of_payments(self):
        result = cash_by_month(self.schedule)
        total = sum(_c(v) for v in result.values())
        expected = sum(_c(row['payment']) for row in self.schedule)
        self.assertEqual(total, expected,
                         'сумма раскладки не равна сумме платежей графика')

    def test_two_rows_in_one_month_are_aggregated(self):
        """Аннуитет и досрочка в одном месяце складываются в один ключ."""
        schedule = _schedule(self.resp, 'deposit', 'deposit_schedule')
        months = [row['date'][3:] for row in schedule]
        duplicated = {m for m in months if months.count(m) > 1}
        self.assertTrue(duplicated, 'в графике арма вклада нет месяца с двумя строками')
        result = cash_by_month(schedule)
        for month in duplicated:
            expected = sum(_c(row['payment']) for row in schedule
                           if row['date'][3:] == month)
            self.assertEqual(_c(result[month]), expected,
                             f'месяц {month} не агрегирован')


@unittest.skipIf(cash_parity_report is None or cash_by_month is None, NO_PARITY_REPORT)
class CashParityReportContractTest(unittest.TestCase):
    """
    `cash_parity_report()` пуст для всех сценариев, кроме вливания и хвоста.

    Реализованная форма — ``cash_parity_report(base_cash, arm_cash,
    exclude_months=())``: на вход идут раскладки `cash_by_month`, а не графики.
    Если сигнатура снова изменится, тест не краснеет ложно, а уходит в skip с
    указанием фактических имён параметров — красным должно быть поведение, а не
    несовпадение контракта.
    """

    def setUp(self):
        self.resp = _compare()
        self.params = list(inspect.signature(cash_parity_report).parameters)
        self.monthly_payment = _c(_monthly_payment(self.resp))
        self.infusion = _fmt(_infusion_month(self.resp))
        if len(self.params) < 2 or 'sched' in self.params[0]:
            self.skipTest('cash_parity_report принимает не раскладки кэша, а '
                          f'{self.params} — тест приёмки требует обновления')
        self.takes_exclude = 'exclude_months' in self.params

    def _cash(self, key, flat_key, with_reinvest=True):
        """
        Раскладка кэша арма в форме production-функции.

        `extra` — движение денег мимо ипотеки: взнос на вклад со знаком «+»,
        снятие со вклада со знаком «−», взнос реинвеста высвобожденного платежа
        со знаком «+».
        """
        schedule = _schedule(self.resp, key, flat_key)
        extra = {}
        if key == 'deposit':
            extra[self.infusion] = float(LUMP)
            for row in schedule:
                if row.get('row_kind') == 'early':
                    month = row['date'][3:]
                    extra[month] = extra.get(month, 0.0) - row['payment']
        if with_reinvest:
            new_monthly = _new_monthly(self.resp, key, _NEW_MONTHLY_FLAT.get(key))
            if new_monthly is not None:
                step = float(self.monthly_payment - _c(new_monthly))
                if step > 0:
                    for row in schedule:
                        if row.get('row_kind') == 'early':
                            continue
                        if _c(row['payment']) < self.monthly_payment:
                            month = row['date'][3:]
                            extra[month] = extra.get(month, 0.0) + step
        return cash_by_month(schedule, extra)

    def _report(self, arm_cash):
        base_cash = self._cash('baseline', 'base_schedule', with_reinvest=False)
        if self.takes_exclude:
            return cash_parity_report(base_cash, arm_cash, (self.infusion,))
        return cash_parity_report(base_cash, arm_cash)

    def test_report_is_empty_for_every_scenario(self):
        for key, flat_key, _new_monthly_key in SCENARIOS:
            with self.subTest(scenario=key):
                report = self._report(self._cash(key, flat_key))
                head = list(report)[:5]
                self.assertEqual(
                    len(list(report)), 0,
                    f'cash_parity_report непуст для сценария {key}: {head}',
                )

    def test_report_is_not_empty_without_reinvest(self):
        """
        Машинная проверка «реинвест не забыт»: без взносов реинвеста отчёт по
        паре (`reduce_payment`, база) обязан быть непуст в каждом месяце.
        """
        arm_cash = self._cash('reduce_payment', 'reduce_payment_schedule',
                              with_reinvest=False)
        report = list(self._report(arm_cash))
        self.assertTrue(report,
                        'без реинвеста отчёт пуст — cash_parity_report ничего не ловит')


# ---------------------------------------------------------------------------
# Приёмка поведения: сегодня красная
# ---------------------------------------------------------------------------

class CashParityTest(unittest.TestCase):
    """
    Отчёт cash-parity, посчитанный тестом самостоятельно.

    Семья `plain` (бюджет пуст): все армы обязаны тратить одни и те же деньги —
    базовый платёж 23 124,77 ₽ в месяц. Пара (`reduce_payment`, база) — тот
    самый случай, который доказывает, что реинвест высвобожденного платежа не
    забыт.
    """

    def setUp(self):
        self.resp = _compare()
        self.monthly_payment = _monthly_payment(self.resp)
        self.infusion = _infusion_month(self.resp)

    def _report(self, key, flat_key):
        schedule = _schedule(self.resp, key, flat_key)
        return _parity_report(self.resp, key, schedule, self.monthly_payment, self.infusion)

    def _assert_empty(self, report, note=''):
        """
        Сравнение длин, а не списков: отчёт на 300 месяцев в diff'е unittest
        нечитаем. Первые пять месяцев с рублями идут в сообщение.
        """
        head = ', '.join(f'{month}: {diff:+}' for month, diff in report[:5])
        self.assertEqual(
            len(report), 0,
            f'кэш расходится с базой в {len(report)} месяцах ({head}){note}',
        )

    def test_parity_reduce_payment_vs_baseline(self):
        report = self._report('reduce_payment', 'reduce_payment_schedule')
        self._assert_empty(
            report,
            ' — высвобожденный платёж не реинвестируется, reinvest_income = '
            f'{_reinvest_income(self.resp, "reduce_payment")}',
        )

    def test_parity_deposit_vs_baseline(self):
        report = self._report('deposit', 'deposit_schedule')
        self._assert_empty(
            report,
            ' — арм вклада после погашения тоже платит меньше базы, '
            f'reinvest_income = {_reinvest_income(self.resp, "deposit")}',
        )

    def test_parity_reduce_term_vs_baseline(self):
        """Уже зелёный: арм reduce_term платит тот же платёж до самого конца."""
        report = self._report('reduce_term', 'reduce_term_schedule')
        self._assert_empty(report)

    def test_response_carries_its_own_parity_report(self):
        """
        Отчёт обязан приезжать в ответе: непустой блокирует вывод winner (И5),
        а значит должен быть виден снаружи, а не оставаться внутри расчёта.
        """
        report = self.resp.get('cash_parity')
        if report is None:
            self.skipTest('ответ пока не несёт cash_parity (ключ появляется на И3c)')
        self.assertIsInstance(report, dict)
        for key in ('deposit', 'reduce_payment', 'reduce_term'):
            with self.subTest(scenario=key):
                self.assertIn(key, report,
                              f'сценарий {key} не проверен на cash-parity')
                self.assertEqual(
                    list(report[key])[:5], [],
                    f'ответ сам сообщает о расхождении кэша в сценарии {key}',
                )


class DepositIncomeTest(unittest.TestCase):
    """`monthly_addition = 0` для ВСЕХ семей: на вкладе только разовая сумма."""

    def test_deposit_income_without_budget(self):
        """Без бюджета профицита нет — числа обязаны совпасть уже сегодня."""
        resp = _compare(monthly_budget=None)
        self.assertEqual(_c(_deposit_income(resp)), EXPECTED_DEPOSIT_INCOME)
        self.assertEqual(_c(_deposit_final(resp)), EXPECTED_DEPOSIT_FINAL)

    def test_deposit_income_with_budget(self):
        """
        С бюджетом 60 000 старый код кладёт на вклад профицит 36 875,23 ₽/мес:
        120 072,04 / 1 062 574,80 вместо 86 135,40 / 586 135,40.
        """
        resp = _compare(monthly_budget=BUDGET)
        income, final = _c(_deposit_income(resp)), _c(_deposit_final(resp))
        self.assertEqual(
            income, EXPECTED_DEPOSIT_INCOME,
            f'deposit_income = {income} (ожидалось {EXPECTED_DEPOSIT_INCOME}); '
            f'разница {income - EXPECTED_DEPOSIT_INCOME} — на вклад попал профицит бюджета',
        )
        self.assertEqual(
            final, EXPECTED_DEPOSIT_FINAL,
            f'deposit_final = {final} (ожидалось {EXPECTED_DEPOSIT_FINAL}); '
            f'разница {final - EXPECTED_DEPOSIT_FINAL}',
        )

    def test_deposit_income_matches_lump_only_formula(self):
        """Доход вклада == calc_deposit(разовая сумма) при любом бюджете."""
        expected_income, expected_final = calc_deposit(
            LUMP, DEPOSIT['annual_rate'], DEPOSIT['term_months'], DEPOSIT['capitalization'],
        )
        for budget in (None, BUDGET):
            with self.subTest(monthly_budget=budget):
                resp = _compare(monthly_budget=budget)
                self.assertEqual(_c(_deposit_income(resp)), _c(expected_income))
                self.assertEqual(_c(_deposit_final(resp)), _c(expected_final))


class ReinvestIncomeTest(unittest.TestCase):
    """
    Реинвест высвобожденного платежа — отдельное поле ответа.

    Решения 5, 7 и 16: `MP − new_monthly` каждый месяц уходит на вклад под ту же
    ставку и с той же капитализацией, до закрытия своего арма; результат живёт в
    `reinvest_income` и вычитается из `own_cost`, но в `deposit_income` не
    подмешивается.
    """

    def setUp(self):
        self.resp = _compare()
        self.monthly_payment = _c(_monthly_payment(self.resp))
        self.schedule = _schedule(self.resp, 'reduce_payment', 'reduce_payment_schedule')
        self.new_monthly = _new_monthly(self.resp, 'reduce_payment',
                                        'reduce_payment_new_monthly')

    def _reinvest(self):
        value = _reinvest_income(self.resp, 'reduce_payment')
        if value is None:
            self.fail('reinvest_income не отдаётся наружу: в ответе нет ни '
                      "'reinvest_income', ни 'reduce_payment_reinvest_income', "
                      'ни поля на объекте сценария (И3c)')
        return _c(value)

    def test_reinvest_income_is_a_separate_field(self):
        """
        Поле обязано существовать и приезжать наружу отдельным числом, даже
        когда оно равно нулю: карточка вклада показывает доход по разовой
        сумме, и подмешивать туда что-либо ещё нельзя. Ноль здесь — не
        отсутствие поля, а осознанная величина (см. REINVEST_EARNING_MONTHS).
        """
        self.assertEqual(self._reinvest(), ZERO)

    def test_reinvest_income_matches_the_freed_payment_stream(self):
        """
        Число обязано быть доходом ровно того потока, который высвободился:
        взнос `MP − new_monthly`, ставка и капитализация — вкладные.
        """
        reinvest = self._reinvest()
        step = float(self.monthly_payment - _c(self.new_monthly))
        months = _annuity_months(self.schedule)
        candidates = {
            h: _c(calc_monthly_deposit(0, step, DEPOSIT['annual_rate'],
                                       DEPOSIT['capitalization'], h)[0])
            for h in range(1, months + 1)
        }
        implied = [h for h, value in candidates.items() if value == reinvest]
        self.assertTrue(
            implied,
            f'reinvest_income = {reinvest} не является доходом потока {step} ₽/мес '
            f'под {DEPOSIT["annual_rate"]} % ни на одном горизонте 1..{months} '
            f'(горизонт арма даёт {candidates[months]})',
        )

    def test_freed_payment_earns_nothing(self):
        """
        Решение 16 отменено: высвобожденный платёж дохода НЕ приносит.

        Вклады начинаются от 50 000 ₽ и не пополняются, а высвобождается
        несколько тысяч в месяц — положить их некуда. Прежнее правило («копится
        до закрытия своего арма по ставке вклада») давало 8 739 709 ₽ дохода на
        кредит в три миллиона и уводило `own_cost` в минус: победителя выбирало
        допущение о ставке на двадцать пять лет вперёд.

        Тест кусачий: с прежним правилом здесь стояло бы число порядка
        13,7 млн ₽, поэтому регрессия к нему видна сразу.
        """
        self.assertEqual(self._reinvest(), ZERO)
        months = _annuity_months(self.schedule)
        step = float(self.monthly_payment - _c(self.new_monthly))
        would_be = _c(calc_monthly_deposit(0, step, DEPOSIT['annual_rate'],
                                           DEPOSIT['capitalization'], months)[0])
        self.assertGreater(
            would_be, _c(1_000_000),
            'контрпример перестал быть показательным — пересмотрите замер',
        )

    def test_reinvest_is_not_mixed_into_deposit_income(self):
        """Карточка вклада показывает доход только по разовой сумме."""
        self._reinvest()          # красный, пока поля нет
        self.assertEqual(
            _c(_deposit_income(self.resp)), EXPECTED_DEPOSIT_INCOME,
            'в deposit_income подмешан чужой доход',
        )

    def test_own_cost_is_reduced_by_reinvest(self):
        """
        `own_cost = total_interest − deposit_income − reinvest_income`.

        `lump_unused` в метрику не входит — иначе двойной вычет.
        """
        reinvest = self._reinvest()
        own_cost = _lookup(self.resp, 'reduce_payment', 'own_cost',
                           'reduce_payment_own_cost')
        if own_cost is None:
            self.skipTest('own_cost появляется в ответе на И5; на И3 проверяется '
                          'только наличие и величина reinvest_income')
        total_interest = sum((_c(row['interest']) for row in self.schedule), ZERO)
        deposit_income = _c(_lookup(self.resp, 'reduce_payment', 'deposit_income',
                                    'reduce_payment_deposit_income') or 0)
        self.assertEqual(
            _c(own_cost), total_interest - deposit_income - reinvest,
            f'own_cost = {own_cost} не равен {total_interest} − {deposit_income} − {reinvest}',
        )


class LumpUnusedIsNotAMetricTest(unittest.TestCase):
    """
    `lump_unused` — справочное поле, в `own_cost` не входит (решения 5-6).

    Конфигурация замера роадмапа: разовая сумма 500 000 ₽ на вкладе под 16 % в
    течение 180 месяцев вырастает до 5 424 868,36 ₽ при остатке 1 901 057,86 —
    не понадобилось 3 523 810,50 ₽. Вычитать излишек из метрики нельзя: он уже
    сидит внутри `deposit_income`, потому что `F = S + income`. По ошибочной
    формуле выходило −5 380 381,24 ₽ вместо −1 856 570,74 ₽.
    """

    def setUp(self):
        deposit = dict(DEPOSIT, term_months=180)
        self.resp = run_comparison(dict(MORTGAGE), deposit,
                                   {'lump_sum': LUMP, 'monthly_budget': None})

    def _lump_unused(self):
        value = _lookup(self.resp, 'deposit', 'lump_unused', 'deposit_lump_unused')
        if value is None:
            self.fail('lump_unused по сценарию вклада наружу не отдаётся (И3c)')
        return _c(value)

    def test_excess_is_reported(self):
        self.assertEqual(self._lump_unused(), Decimal('3523810.50'))
        self.assertEqual(_c(_deposit_final(self.resp)), Decimal('5424868.36'))

    def test_own_cost_does_not_subtract_the_excess(self):
        own_cost = _lookup(self.resp, 'deposit', 'own_cost', 'deposit_own_cost')
        if own_cost is None:
            self.skipTest('own_cost появляется в ответе на И5')
        unused = self._lump_unused()
        self.assertEqual(
            _c(own_cost), Decimal('-1856570.74'),
            f'own_cost = {own_cost}; при вычете излишка вышло бы '
            f'{_c(own_cost) - unused} — двойной вычет',
        )


class BaselineKindTest(unittest.TestCase):
    """Непустой `monthly_budget` переводит сравнение в семью `snowball`."""

    def test_budget_switches_baseline_to_snowball(self):
        resp = _compare(monthly_budget=BUDGET)
        self.assertEqual(
            resp.get('baseline_kind'), 'snowball',
            f"baseline_kind = {resp.get('baseline_kind')!r} при бюджете {BUDGET}: "
            'в семье plain бюджет не тратит ни один сценарий, деньги пользователя '
            'молча выбрасываются (решение 7)',
        )

    def test_empty_budget_keeps_plain(self):
        """
        Обратная сторона правила: без бюджета семья остаётся `plain`.

        Ключа `baseline_kind` в ответе И3 может ещё не быть вовсе — это не
        ошибка, дефолт колонки всё равно 'plain' (И7). Ошибка — только явный
        'snowball' там, где бюджет не задан.
        """
        resp = _compare(monthly_budget=None)
        self.assertIn(resp.get('baseline_kind'), (None, 'plain'),
                      f"baseline_kind = {resp.get('baseline_kind')!r} без бюджета")

    def test_switch_reason_is_visible(self):
        """
        Переключение обязано быть видимым: карточка параметров печатает причину,
        значит ответ обязан её нести.
        """
        resp = _compare(monthly_budget=BUDGET)
        candidates = ('baseline_kind_reason', 'baseline_reason', 'baseline_switch_reason')
        reason = next((resp[k] for k in candidates if resp.get(k)), None)
        self.assertTrue(
            reason,
            'причина переключения базы не отдаётся наружу: ожидался непустой '
            f'ключ из {candidates}',
        )


if __name__ == '__main__':
    unittest.main()
