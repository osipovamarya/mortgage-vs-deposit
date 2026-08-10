"""
Core financial calculation logic.

All mortgage calculations use the annuity (равные платежи) method.
Deposit calculations support both compound (капитализация) and simple interest.

`build_amortization()`, `simulate_lump_repayment()` и `calc_repayment_schedule()` —
тонкие обёртки над событийным движком `app/engine.py`: сам помесячный цикл,
начисление процентов сегментами и правило распределения досрочки живут там.
Второго движка в этом файле больше нет (Итерация 3).
"""
from decimal import Decimal
from dateutil.rrule import rrule, MONTHLY
from dateutil.relativedelta import relativedelta

from .engine import (
    ALLOC_PRINCIPAL_ONLY,
    AMOUNT_BUDGET,
    KIND_LUMP,
    KIND_RECURRING,
    MODE_PAYMENT,
    ROW_ANNUITY,
    ROW_EARLY,
    STATUS_NOT_APPLICABLE,
    STATUS_OK,
    MortgageState,
    RepaymentEvent,
    SimOptions,
    basis_for,
    payment_grid,
    simulate_strategy,
    _d,
    _next_business_day,
    _parse_date,
    _r2,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _date_to_idx(target_date, scheduled_dates):
    """
    Return the first index in scheduled_dates where date >= target_date.
    Returns 0 if target_date is None or before schedule start.
    Returns len(scheduled_dates) if past the end (strategy never kicks in).
    """
    if target_date is None:
        return 0
    for i, d in enumerate(scheduled_dates):
        if d >= target_date:
            return i
    return len(scheduled_dates)  # past end → strategy never kicks in


# ---------------------------------------------------------------------------
# Mortgage amortization
# ---------------------------------------------------------------------------

def build_amortization(loan_amount, annual_rate, first_payment_date, last_payment_date,
                       adjust_business_days=False, prev_payment_date=None, fixed_payment=None):
    """
    Generate a full month-by-month amortization schedule.

    Тонкая обёртка над движком: график без досрочек — это `simulate_strategy()`
    с пустым списком событий. Сигнатура и возвращаемый кортеж не менялись.

    Returns:
        schedule        — list of dicts, one per payment
        first_payment   — payment amount of the first period
        total_interest  — sum of all interest portions
    """
    if fixed_payment is None:
        # Мёртвая ветка: все вызывающие передают fixed_payment. Оставлена здесь
        # как есть и в движок не переезжает (роадмап, Итерация 1).
        return _amortization_without_fixed_payment(
            loan_amount, annual_rate, first_payment_date, last_payment_date,
            adjust_business_days, prev_payment_date,
        )

    state = MortgageState(
        loan_amount=loan_amount,
        annual_rate=annual_rate,
        first_payment_date=_parse_date(first_payment_date),
        last_payment_date=_parse_date(last_payment_date),
        prev_payment_date=_parse_date(prev_payment_date),
        contract_payment=fixed_payment,
    )
    result = simulate_strategy(state, [], SimOptions(basis=basis_for(adjust_business_days)))
    # Пустая сетка (договор короче месяца: 02.02.2026 → 15.02.2026) больше не
    # роняет расчёт по `schedule[0]`. Ввод такого договора отсекается в
    # routes/mortgage.py, здесь остаётся честный ответ «предстоящих платежей нет».
    first_payment = result.schedule[0]['payment'] if result.schedule else 0.0
    return result.schedule, first_payment, result.total_interest


def _amortization_without_fixed_payment(loan_amount, annual_rate,
                                        first_payment_date, last_payment_date,
                                        adjust_business_days, prev_payment_date):
    """
    Старая ветка `build_amortization()` без договорного платежа.

    Платёж пересчитывается каждый месяц, а остаток срока берётся не из сетки, а
    из `days_left / 365 * 12`. Ветка мёртвая — ни один вызывающий сюда не
    попадает; сохранена без изменений, чтобы не потерять поведение молча.
    """
    if isinstance(first_payment_date, str):
        first_payment_date = _parse_date(first_payment_date)
    if isinstance(last_payment_date, str):
        last_payment_date = _parse_date(last_payment_date)

    annual_rate_d = _d(annual_rate)
    rate = annual_rate_d / _d(100) / _d(12)

    scheduled = list(rrule(MONTHLY, dtstart=first_payment_date, until=last_payment_date))
    n = len(scheduled)

    if adjust_business_days:
        dates = [_next_business_day(d) for d in scheduled]
        _prev = prev_payment_date if prev_payment_date is not None else (first_payment_date - relativedelta(months=1))
        prev_date = _next_business_day(_parse_date(_prev))
    else:
        dates = scheduled
        prev_date = None

    schedule = []
    balance = _d(loan_amount)
    total_interest = Decimal('0')
    first_payment = None

    for i, date in enumerate(dates):
        if adjust_business_days:
            days = (date - prev_date).days
            interest = _r2(balance * annual_rate_d / _d(100) / _d(365) * _d(days))
            prev_date = date
        else:
            interest = _r2(balance * rate)

        if i == n - 1:
            principal = _r2(balance)
            payment = _r2(principal + interest)
        else:
            days_left = (last_payment_date - date).days
            remaining_n = max(round(days_left / 365 * 12), 1)
            factor = (1 + rate) ** remaining_n
            payment = _r2(balance * rate * factor / (factor - 1))
            principal = _r2(payment - interest)

        balance = balance - principal
        balance = max(balance, Decimal('0'))
        total_interest += interest

        if first_payment is None:
            first_payment = payment

        schedule.append({
            'payment_num': i + 1,
            'date': date.strftime('%d.%m.%Y'),
            'payment': float(payment),
            'principal': float(principal),
            'interest': float(interest),
            'balance': float(_r2(balance)),
            'early': 0.0,
            'row_kind': ROW_ANNUITY,
            'early_interest': 0.0,
        })

        if balance <= Decimal('0.01'):
            break

    return schedule, float(first_payment), float(_r2(total_interest))


# ---------------------------------------------------------------------------
# Deposit
# ---------------------------------------------------------------------------

def calc_deposit(amount, annual_rate, term_months, capitalization):
    """Calculate deposit income for a single lump sum."""
    if capitalization:
        monthly_rate = annual_rate / 100 / 12
        final_amount = amount * (1 + monthly_rate) ** term_months
    else:
        final_amount = amount * (1 + (annual_rate / 100) * (term_months / 12))
    income = final_amount - amount
    return round(income, 2), round(final_amount, 2)


def calc_monthly_deposit(initial, monthly_addition, annual_rate, capitalization, months):
    """
    Accumulate lump sum + monthly additions on a deposit.
    Returns (income, final_amount).
    """
    monthly_rate = annual_rate / 100 / 12
    if capitalization:
        balance = float(initial)
        for _ in range(months):
            balance = balance * (1 + monthly_rate) + monthly_addition
        income = balance - float(initial) - float(monthly_addition) * months
    else:
        income = float(initial) * monthly_rate * months
        for k in range(1, months + 1):
            income += float(monthly_addition) * monthly_rate * (months - k)
        balance = float(initial) + float(monthly_addition) * months + income
    return round(income, 2), round(balance, 2)


# ---------------------------------------------------------------------------
# Single lump-sum early repayment
# ---------------------------------------------------------------------------

def simulate_lump_repayment(loan_amount, annual_rate, first_payment_date, last_payment_date,
                            monthly_payment, lump_sum, lump_date, mode='reduce_payment',
                            adjust_business_days=False, prev_payment_date=None,
                            allocation=None):
    """
    Simulate one lump-sum early repayment on top of the regular annuity.

    Тонкая обёртка над движком: одна разовая досрочка — это одно событие.

    Правило распределения (`allocation`, None → `principal_only`):

      * `principal_only` (ИНВАРИАНТ, дефолт) — досрочка уходит на 100 % в тело и
        никогда не оплачивает проценты. Проценты за уже прошедший отрезок
        начислены на добытийный остаток и предъявляются ближайшим аннуитетом;
      * `interest_first` — из досрочки сначала удерживаются проценты, начисленные
        с прошлого платежа по дату досрочки, остаток идёт в тело. Курсор
        сегмента начисления сдвигается на дату досрочки, поэтому ближайший
        аннуитет считает проценты только за оставшиеся дни.

    Три точки применения:

      * досрочка **в дату платежа** — после аннуитета этой даты, поэтому её
        проценты равны baseline-процентам того же месяца, а режимы распределения
        тождественны (проценты периода уже закрыты);
      * досрочка **между платежами** — период делится на два отрезка;
      * досрочка **до первого предстоящего платежа** или без даты — применяется
        сразу, накопленного периода ещё нет, режимы снова тождественны.

    mode:
      'reduce_payment' — same end date, annuity recalculated after the lump
      'reduce_term'    — same monthly payment, loan closes earlier

    Returns: (schedule, monthly_payment_after_lump, total_interest, annuity_months)
    `annuity_months` counts real payments only, early-repayment rows excluded.

    Кортеж намеренно остался четырёхэлементным: его форма зафиксирована
    голденами (`tests/golden/simulate_lump_repayment.json`). `status` и
    `lump_unused` живут на `StrategyResult`, и `run_comparison()` берёт полный
    результат движка напрямую — через эту обёртку они и не должны ходить.
    """
    state = MortgageState(
        loan_amount=loan_amount,
        annual_rate=annual_rate,
        first_payment_date=_parse_date(first_payment_date),
        last_payment_date=_parse_date(last_payment_date),
        prev_payment_date=_parse_date(prev_payment_date),
        contract_payment=monthly_payment,
    )
    opts = SimOptions(
        basis=basis_for(adjust_business_days),
        allocation=allocation or ALLOC_PRINCIPAL_ONLY,
    )

    events = []
    if _d(lump_sum or 0) > Decimal('0'):
        events.append(RepaymentEvent(
            amount=lump_sum,
            at=_parse_date(lump_date),
            mode=mode,
        ))

    result = simulate_strategy(state, events, opts)
    return (result.schedule, result.monthly_payment,
            result.total_interest, result.annuity_months)


# ---------------------------------------------------------------------------
# Snowball + one-time repayment simulation
# ---------------------------------------------------------------------------

def simulate_snowball(loan_amount, annual_rate, first_payment_date, last_payment_date,
                      lump_sum, lump_idx, monthly_budget, monthly_idx,
                      monthly_extra_day=None, mode=MODE_PAYMENT, contract_payment=None,
                      adjust_business_days=False, allocation=None):
    """
    Снежный ком целиком: ежемесячная доплата из бюджета плюс, если есть, разовая
    досрочка. Возвращает `StrategyResult` движка — со `schedule`, `lump_unused`,
    `annuity_months` и `status`.

    `calc_repayment_schedule()` — трёхэлементная обёртка над этой функцией;
    `run_comparison()` берёт полный результат отсюда, иначе `lump_unused` пришлось
    бы молча выбрасывать (Итерация 3, пункт 3c).

    Стратегия выражается двумя событиями движка:

    * `recurring` с `amount_kind='budget'` — «всего готов платить в месяц»:
      доплата резолвится в момент применения как `бюджет − уплаченный в этом
      периоде аннуитет`, поэтому в `reduce_payment` она растёт вслед за падающим
      аннуитетом, в `reduce_term` остаётся постоянной, а месячный расход
      заёмщика в любом режиме равен бюджету ровно;
    * `lump` в дату платежа №`lump_idx` — применяется ПОСЛЕ аннуитета той даты,
      поэтому проценты того месяца равны baseline-процентам (инвариант 3ca4b3e).

    Что изменилось против прежней собственной реализации (Итерация 3):

    * `contract_payment` — введённый платёж вместо пересчёта аннуитета каждый
      месяц (3a-1);
    * `adjust_business_days` — снежок наконец знает про перенос на рабочий день,
      и график считается по той же сетке дат, что и все прочие сценарии (3a-2);
    * `monthly_extra_day` больше НЕ переключает базу начисления: база приходит
      только из `adjust_business_days` (3a-3, решение 4 роадмапа);
    * аннуитет платится полностью, бюджет идёт сверху — снято `min(annuity,
      budget)`, из-за которого при бюджете меньше платежа тело не гасилось вовсе
      (3b);
    * `months_to_payoff` считает плановые месяцы, а не строки графика (3b).
    """
    first_dt = _parse_date(first_payment_date)
    last_dt = _parse_date(last_payment_date)
    next_dt = first_dt + relativedelta(months=1)

    state = MortgageState(
        loan_amount=loan_amount,
        annual_rate=annual_rate,
        first_payment_date=next_dt,
        last_payment_date=last_dt,
        prev_payment_date=first_dt,
        contract_payment=contract_payment,
    )
    opts = SimOptions(
        basis=basis_for(adjust_business_days),
        allocation=allocation or ALLOC_PRINCIPAL_ONLY,
    )
    dates, _anchor = payment_grid(state, opts.basis)

    events = []
    if dates:
        budget = float(monthly_budget or 0)
        start_idx = int(monthly_idx or 0)
        if budget > 0 and 0 <= start_idx < len(dates):
            # Событие бюджета идёт ПЕРВЫМ: при совпадении дат оно применяется до
            # разовой досрочки, как в прежнем снежке, и пересчёт платежа от
            # досрочки достаётся уже следующему периоду.
            events.append(RepaymentEvent(
                amount=budget,
                kind=KIND_RECURRING,
                amount_kind=AMOUNT_BUDGET,
                mode=mode,
                start_date=dates[start_idx],
                day_of_month=int(monthly_extra_day) if monthly_extra_day else None,
            ))

        lump = float(lump_sum or 0)
        if lump > 0:
            idx = int(lump_idx or 0)
            # Индекс за концом графика: событие не состоится, движок покажет это
            # через `lump_unused` и `status='not_applicable'`, а не молча съест.
            at = dates[idx] if 0 <= idx < len(dates) else dates[-1] + relativedelta(months=1)
            events.append(RepaymentEvent(amount=lump, at=at, kind=KIND_LUMP, mode=mode))

    return simulate_strategy(state, events, opts)


def calc_repayment_schedule(loan_amount, annual_rate, first_payment_date, last_payment_date,
                            lump_sum, lump_idx,
                            monthly_budget, monthly_idx,
                            monthly_extra_day=None, mode=MODE_PAYMENT,
                            contract_payment=None, adjust_business_days=False,
                            allocation=None):
    """
    Снежный ком: ежемесячная доплата из бюджета плюс разовая досрочка.

    `monthly_budget` — ВЕСЬ месячный платёж («всего готов платить в месяц»), а не
    доплата сверх аннуитета. Аннуитет из него платится полностью, остаток уходит
    в тело; если бюджет меньше аннуитета, доплаты просто нет.

    `monthly_extra_day` — день доплаты. Если он позже дня платежа, доплата уходит
    отдельной строкой и период начисления делится по дням; иначе платится в дату
    аннуитета, сразу после него. На БАЗУ начисления процентов не влияет никак.

    Необязательные `mode` / `contract_payment` / `adjust_business_days` /
    `allocation` добавлены на Итерации 3; дефолты сохраняют прежний внешний
    контракт вызова.

    Returns: (total_interest, months_to_payoff, schedule)
    """
    result = simulate_snowball(
        loan_amount, annual_rate, first_payment_date, last_payment_date,
        lump_sum, lump_idx, monthly_budget, monthly_idx,
        monthly_extra_day=monthly_extra_day, mode=mode,
        contract_payment=contract_payment,
        adjust_business_days=adjust_business_days, allocation=allocation,
    )
    return result.total_interest, result.months_to_payoff, result.schedule


# ---------------------------------------------------------------------------
# Честность денежных потоков (решение 7 роадмапа)
# ---------------------------------------------------------------------------

def _month_key(stamp):
    """'DD.MM.YYYY' → 'MM.YYYY'."""
    return stamp[3:]


def _month_order(key):
    """Ключ сортировки месяцев: 'MM.YYYY' → (год, месяц)."""
    month, year = key.split('.')
    return int(year), int(month)


def cash_by_month(schedule, extra=None):
    """
    'MM.YYYY' → сколько рублей заёмщик отдал в этом месяце.

    Считаются ВСЕ строки графика (плановые и досрочные), поэтому месяц с
    досрочкой честно выходит дороже. `extra` — движение денег мимо ипотеки:
    взнос на вклад со знаком «+», снятие со вклада со знаком «−». Снятые со
    вклада деньги не являются новым оттоком: без вычитания арм «сначала вклад»
    выглядел бы так, будто заплатил ту же сумму дважды.
    """
    out = {}
    for row in schedule:
        key = _month_key(row['date'])
        out[key] = round(out.get(key, 0.0) + row['payment'], 2)
    for key, amount in (extra or {}).items():
        out[key] = round(out.get(key, 0.0) + amount, 2)
    return out


def cash_parity_report(base_cash, arm_cash, exclude_months=()):
    """
    Месяцы, в которых арм и база тратят разные деньги.

    Исключаются месяц внешнего вливания (`exclude_months`) и хвост: с месяца, в
    котором закрывается более короткий из двух графиков, сравнивать уже нечего.

    Пустой отчёт == сравнение честное. Для `reduce_payment` без реинвеста
    высвобожденного платежа отчёт непуст в КАЖДОМ месяце после досрочки — это и
    есть машинная проверка того, что реинвест не забыт (решение 7).
    """
    if not base_cash or not arm_cash:
        return []
    exclude = set(exclude_months or ())
    tail = min(max(base_cash, key=_month_order), max(arm_cash, key=_month_order),
               key=_month_order)
    report = []
    for key in sorted(set(base_cash) | set(arm_cash), key=_month_order):
        if _month_order(key) >= _month_order(tail):
            break
        if key in exclude:
            continue
        base_amount = base_cash.get(key, 0.0)
        arm_amount = arm_cash.get(key, 0.0)
        diff = round(arm_amount - base_amount, 2)
        if abs(diff) > 0.01:
            report.append({
                'month': key,
                'baseline': base_amount,
                'scenario': arm_amount,
                'diff': diff,
            })
    return report


def _freed_by_month(schedule, contract_payment):
    """
    'MM.YYYY' → высвобожденная часть платежа: сколько заёмщик НЕ отдал банку
    против договорного платежа. Считается по ВСЕМ строкам месяца, поэтому месяц
    досрочки высвобожденных денег не даёт.
    """
    spent = {}
    for row in schedule:
        key = _month_key(row['date'])
        spent[key] = round(spent.get(key, 0.0) + row['payment'], 2)
    freed = {}
    for key, amount in spent.items():
        rest = round(float(contract_payment) - amount, 2)
        if rest > 0:
            freed[key] = rest
    return freed


# Сколько месяцев высвобожденный платёж реально приносит доход.
#
# Ноль: на вклад его положить некуда. Вклады начинаются от 50 000 ₽ и не
# пополняются, а высвобождается несколько тысяч в месяц — собирать разницу
# вручную и раз в полгода открывать новый вклад пользователь не будет, это
# ровно то решение, которое калькулятор должен избавить от повторения.
# Вкладывается только РАЗОВАЯ сумма: она уже лежит готовой.
#
# Замер, ради которого правило введено: ипотека 2 983 243 ₽ под 7,99 % на
# 294 месяца, вклад 16 %, высвобожденные 2 635 ₽/мес. При начислении на весь
# горизонт арма reinvest_income = 8 739 709 ₽ на кредит в три миллиона,
# own_cost уходит в минус (−5 322 774 ₽), и «уменьшить платёж» побеждает
# из-за допущения о ставке на двадцать пять лет вперёд, а не из-за экономики.
REINVEST_EARNING_MONTHS = 0


def calc_reinvest_income(contributions, annual_rate, capitalization,
                         earning_months=None):
    """
    Доход по потоку высвобожденных платежей (решения 7 и 16 роадмапа).

    `contributions` — взносы по месяцам жизни арма в хронологическом порядке.

    `earning_months` — сколько первых месяцев деньги действительно приносят
    доход; дальше высвобожденный платёж просто копится без процентов. По
    умолчанию это срок вклада пользователя. Ставка вклада действует свой срок
    (обычно 6-12 месяцев), а горизонт арма — весь остаток ипотеки, и применять
    к нему сегодняшнюю ставку значит выдумывать доходность на двадцать лет
    вперёд. Замер, ради которого правило и введено: ипотека 2 983 243 ₽ под
    7,99 % на 294 месяца, вклад 16 %, высвобожденные 2 635 ₽/мес — при
    начислении на весь горизонт `reinvest_income` = 8 739 709 ₽ на кредит в
    три миллиона, `own_cost` уходит в минус, и «уменьшить платёж» побеждает
    из-за допущения, а не из-за экономики.

    Сами взносы в доход не входят: они не тратятся, но и не исчезают —
    общий отток заёмщика равен телу плюс проценты в любом арме, поэтому
    высвобожденные деньги влияют только доходом, который на них заработан.

    Соглашение о начислении то же, что в `calc_monthly_deposit`.
    """
    months = len(contributions)
    if not months or not annual_rate:
        return 0.0
    horizon = months if earning_months is None else min(int(earning_months), months)
    if horizon <= 0:
        return 0.0
    monthly_rate = float(annual_rate) / 100 / 12
    if capitalization:
        balance = 0.0
        for amount in contributions[:horizon]:
            balance = balance * (1 + monthly_rate) + amount
        income = balance - sum(contributions[:horizon])
    else:
        income = 0.0
        for k, amount in enumerate(contributions[:horizon], start=1):
            income += amount * monthly_rate * (horizon - k)
    return round(income, 2)


def _reinvest_of(schedule, contract_payment, annual_rate, capitalization,
                 earning_months=None):
    """
    Доход по высвобожденному платежу арма и сами взносы по месяцам.

    Применяется только к семье `plain`: там обязательство заёмщика равно
    договорному платежу, и всё, что арм не отдал банку, уходит на вклад под ту
    же ставку. В снежном коме обязательство — месячный бюджет, и высвобожденные
    деньги тратятся на ипотеку в том же месяце.
    """
    freed = _freed_by_month(schedule, contract_payment)
    if not freed or not annual_rate:
        return 0.0, freed
    months = []
    for row in schedule:
        key = _month_key(row['date'])
        if key not in months:
            months.append(key)
    contributions = [freed.get(key, 0.0) for key in months]
    income = calc_reinvest_income(contributions, annual_rate, capitalization,
                                  earning_months=earning_months)
    return income, freed


def _withdrawals_of(schedule):
    """
    'MM.YYYY' → сколько денег со вклада ушло в ипотеку (со знаком «−»).

    Нужно армy «сначала вклад»: сумма закрытого вклада — не новый отток,
    заёмщик отдаёт банку те же деньги, что положил на вклад месяцем раньше.
    """
    out = {}
    for row in schedule:
        if row['row_kind'] != ROW_EARLY:
            continue
        key = _month_key(row['date'])
        out[key] = round(out.get(key, 0.0) - row['payment'], 2)
    return out


def _early_months(schedule):
    """Месяцы, в которых арм внёс досрочку (месяцы внешнего вливания)."""
    return {_month_key(row['date']) for row in schedule if row['row_kind'] == ROW_EARLY}


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def run_comparison(mortgage, deposit, strategy=None):
    """
    Compare strategies:
      A  — put lump_sum on deposit for T months, then repay → reduce payment
      B  — lump_sum early repayment at lump_sum_date → reduce monthly payment
      C  — snowball: lump_sum at lump_sum_date + monthly extra from monthly_start_date

    strategy fields used:
        lump_sum, lump_sum_date, monthly_budget, monthly_start_date,
        repayment_mode, early_repayment_allocation

    Метрика сравнения (решение 5 роадмапа):

        own_cost = total_interest − deposit_income − reinvest_income
        interest_saved = own_cost базы − own_cost сценария
        winner = argmax(interest_saved) по СОСТОЯВШИМСЯ сценариям

    Сценарий, который не сделал ничего и ничего не дал (`status='not_applicable'`
    при нулевой выгоде — например, дата досрочки за концом графика), из конкурса
    исключается: его экономия равна ровно 0,00 ₽, что больше любой
    отрицательной, и он побеждал бы, ничего не сделав (решение 6 роадмапа).
    Статусы приезжают в ответе полем `option_statuses`, пул конкурса — полем
    `options_applicable`.

    `lump_unused` в метрику НЕ входит: излишек уже сидит внутри `deposit_income`
    (F = S + income), вычесть его второй раз значило бы завысить выгоду.

    Честность денежных потоков (решение 7). Все армы обязаны тратить одинаковые
    рубли в каждом месяце, иначе сравнение недействительно:

    * на вкладе лежит ТОЛЬКО разовая сумма — профицит бюджета туда не кладётся;
    * в `reduce_payment` высвобожденная часть платежа реинвестируется под ту же
      ставку до закрытия своего арма (решение 16);
    * непустой `monthly_budget` переводит сравнение в семью `snowball`
      (`baseline_kind` в ответе): в семье `plain` бюджет не тратит ни один
      сценарий, и оставить его там значило бы молча выбросить деньги.

    Проверка — `cash_parity_report()`: в ответе поле `cash_parity`, пустое, если
    сравнение честное.
    """
    strategy = strategy or {}
    adj = bool(mortgage.get('adjust_business_days'))
    basis = basis_for(adj)

    first_dt = _parse_date(mortgage['first_payment_date'])
    last_dt = _parse_date(mortgage['last_payment_date'])
    next_dt = first_dt + relativedelta(months=1)

    # Strategy parameters
    lump_sum = float(strategy.get('lump_sum') or 0)
    lump_sum_date = _parse_date(strategy.get('lump_sum_date'))
    monthly_budget = float(strategy.get('monthly_budget') or 0) or None
    monthly_start_date = _parse_date(strategy.get('monthly_start_date'))
    monthly_extra_day = strategy.get('monthly_extra_day') or None
    if monthly_extra_day:
        monthly_extra_day = int(monthly_extra_day)
    repayment_mode = strategy.get('repayment_mode') or 'reduce_payment'
    allocation = strategy.get('early_repayment_allocation') or ALLOC_PRINCIPAL_ONLY

    base_state = MortgageState(
        loan_amount=mortgage['loan_amount'],
        annual_rate=mortgage['annual_rate'],
        first_payment_date=next_dt,
        last_payment_date=last_dt,
        prev_payment_date=first_dt,
        contract_payment=mortgage['monthly_payment'],
    )
    opts = SimOptions(basis=basis, allocation=allocation)

    # Baseline: original mortgage schedule, no changes.
    baseline = simulate_strategy(base_state, [], opts)
    base_schedule = baseline.schedule
    monthly_payment = base_schedule[0]['payment']
    baseline_total_interest = baseline.total_interest

    # ЕДИНСТВЕННАЯ сетка дат: та же, по которой построен график. Раньше дата
    # вливания вклада бралась из необработанного rrule, а график шёл по датам,
    # сдвинутым на рабочий день, — две сетки расходились на выходных.
    payment_dates = baseline.dates
    # Длина СЕТКИ дат и число РЕАЛЬНЫХ платежей — разные числа: введённый платёж
    # больше аннуитета закрывает кредит раньше последней даты договора. Всё, что
    # сравнивается с `months_to_payoff` сценария или индексирует график, обязано
    # считаться отсюда, а не из `len(payment_dates)`.
    baseline_months = baseline.months_to_payoff

    # Индексы снежка считаются по ТОЙ ЖЕ сетке: с Итерации 3 снежок знает про
    # перенос на рабочий день, и сырой rrule больше не нужен — иначе индекс из
    # одной сетки указывал бы на дату из другой.
    lump_idx = _date_to_idx(lump_sum_date, payment_dates)
    monthly_idx = _date_to_idx(monthly_start_date, payment_dates)

    # Семья сравнения (решение 7): непустой месячный бюджет автоматически
    # переводит расчёт в «снежный ком». Переключение обязано быть видимым,
    # поэтому причина уезжает в ответ и печатается в карточке параметров.
    baseline_kind = 'snowball' if monthly_budget else 'plain'
    baseline_kind_reason = (
        'задан месячный бюджет: в семье «чистая ипотека» его не тратит ни один сценарий'
        if monthly_budget else None
    )

    deposit_rate = float(deposit['annual_rate']) if deposit else 0.0
    deposit_cap = bool(deposit['capitalization']) if deposit else True
    # Горизонт начисления на высвобожденный платёж: ставка вклада действует
    # свой срок, дальше деньги копятся без процентов (см. calc_reinvest_income).
    deposit_term_months = int(deposit['term_months']) if deposit else 0

    def _lump_result(amount, at_date, mode):
        """Полный результат движка по одной разовой досрочке."""
        state = MortgageState(
            loan_amount=mortgage['loan_amount'],
            annual_rate=mortgage['annual_rate'],
            first_payment_date=next_dt,
            last_payment_date=last_dt,
            prev_payment_date=first_dt,
            contract_payment=mortgage['monthly_payment'],
        )
        events = []
        if float(amount or 0) > 0:
            events.append(RepaymentEvent(amount=amount, at=at_date, kind=KIND_LUMP, mode=mode))
        return simulate_strategy(state, events, opts)

    def _reinvest(result):
        """
        Доход по высвобожденному платежу арма (решения 7 и 16, исправленные).

        `REINVEST_EARNING_MONTHS = 0`: высвобожденные несколько тысяч рублей в
        месяц положить на вклад физически некуда — вклады начинаются от 50 000 ₽
        и не пополняются. Деньги остаются в кармане и учитываются паритетом
        как непотраченные, но дохода не приносят.
        """
        return _reinvest_of(result.schedule, mortgage['monthly_payment'],
                            deposit_rate, deposit_cap,
                            earning_months=REINVEST_EARNING_MONTHS)

    # --- Strategy A: keep lump_sum on deposit for T months, then repay ---
    deposit_income = 0.0
    deposit_final = 0.0
    deposit_net_saving = 0.0
    deposit_new_monthly = 0.0
    deposit_reinvest_income = 0.0
    deposit_lump_unused = 0.0
    deposit_total_interest = baseline_total_interest
    deposit_status = STATUS_OK
    deposit_schedule = list(base_schedule)
    deposit_cash_extra = {}
    deposit_exclude_months = set()
    maturity_idx = None

    if lump_sum > 0 and deposit:
        # Срок вклада зажимается длиной РЕАЛЬНОГО графика, а не сетки дат:
        # введённый платёж больше аннуитета закрывает кредит раньше последней
        # даты договора, и график тогда короче сетки. Зажим по длине СЕТКИ ронял
        # расчёт (IndexError → 500) на входе «остаток 1 000 000 ₽, 10 %,
        # 02.02.2026 → 02.01.2031 (60 дат), платёж 50 000 ₽»: график там
        # 22 строки, а вклад на 36 месяцев просил `base_schedule[35]`.
        term_months = min(int(deposit['term_months']), len(base_schedule))
        # ОДИН индекс закрытия вклада на всё сравнение: и здесь, и в снежке.
        # Раньше снежок брал min(term, n − 1) и гасил месяцем позже, чем арм A.
        maturity_idx = max(term_months - 1, 0)

        # На вкладе лежит ТОЛЬКО разовая сумма (решение 7): профицит бюджета
        # никакой арм на вклад не кладёт, иначе арм вклада каждый месяц получает
        # чужие деньги и сравнение недействительно.
        deposit_income, deposit_final = calc_deposit(
            lump_sum, deposit['annual_rate'], term_months, deposit['capitalization'],
        )

        # Money becomes available right after the payment that closes the deposit term.
        # Дата берётся из сетки движка, а не из сырого rrule.
        deposit_lump_date = payment_dates[maturity_idx] if term_months > 0 else None
        result_a = _lump_result(deposit_final, deposit_lump_date, repayment_mode)
        deposit_schedule = result_a.schedule
        deposit_new_monthly = result_a.monthly_payment
        deposit_total_interest = result_a.total_interest
        deposit_lump_unused = result_a.lump_unused
        deposit_status = result_a.status
        deposit_reinvest_income, deposit_freed = _reinvest(result_a)

        # Кэш арма: взнос на вклад «сейчас» — внешнее вливание (месяц исключается
        # из паритета), снятие со вклада в дату погашения — не новый отток,
        # реинвест высвобожденного платежа — отток наравне с платежом банку.
        open_month = _month_key(payment_dates[0].strftime('%d.%m.%Y'))
        deposit_cash_extra = dict(_withdrawals_of(deposit_schedule))
        deposit_cash_extra[open_month] = round(
            deposit_cash_extra.get(open_month, 0.0) + lump_sum, 2)
        for month, amount in deposit_freed.items():
            deposit_cash_extra[month] = round(deposit_cash_extra.get(month, 0.0) + amount, 2)
        deposit_exclude_months = {open_month}

        # Роадмап (3c) записывает эту строку как
        #   baseline − interest_A + deposit_income,
        # то есть без реинвеста: пункт про реинвест в том же списке идёт ниже.
        # Решение 5 требует единой метрики `interest_saved = own_cost базы −
        # own_cost сценария`, а она равна формуле роадмапа ПЛЮС доход по
        # высвобожденному платежу. В `reduce_term` высвобожденного платежа нет,
        # и обе формулы совпадают ровно.
        deposit_net_saving = round(
            baseline_total_interest - deposit_total_interest
            + deposit_income + deposit_reinvest_income, 2)

    # --- Strategy B1: lump_sum → reduce payment (lower annuity, same term) ---
    # --- Strategy B2: lump_sum → reduce term  (same payment, shorter term) ---
    reduce_payment_interest_saved = 0.0
    new_monthly_b = mortgage['monthly_payment']
    reduce_term_interest_saved = 0.0
    reduce_term_months_to_payoff = baseline_months
    reduce_term_months_saved = 0
    reduce_payment_schedule = list(base_schedule)
    reduce_term_schedule = list(base_schedule)
    reduce_payment_total_interest = baseline_total_interest
    reduce_term_total_interest = baseline_total_interest
    reduce_payment_reinvest_income = 0.0
    reduce_term_reinvest_income = 0.0
    reduce_payment_lump_unused = 0.0
    reduce_term_lump_unused = 0.0
    reduce_payment_status = STATUS_OK
    reduce_term_status = STATUS_OK
    reduce_payment_cash_extra = {}
    reduce_term_cash_extra = {}

    if lump_sum > 0:
        result_b1 = _lump_result(lump_sum, lump_sum_date, 'reduce_payment')
        reduce_payment_schedule = result_b1.schedule
        new_monthly_b = result_b1.monthly_payment
        reduce_payment_total_interest = result_b1.total_interest
        reduce_payment_lump_unused = result_b1.lump_unused
        reduce_payment_status = result_b1.status
        reduce_payment_reinvest_income, reduce_payment_cash_extra = _reinvest(result_b1)
        reduce_payment_interest_saved = round(
            baseline_total_interest - reduce_payment_total_interest
            + reduce_payment_reinvest_income, 2)

        result_b2 = _lump_result(lump_sum, lump_sum_date, 'reduce_term')
        reduce_term_schedule = result_b2.schedule
        reduce_term_total_interest = result_b2.total_interest
        reduce_term_lump_unused = result_b2.lump_unused
        reduce_term_status = result_b2.status
        reduce_term_reinvest_income, reduce_term_cash_extra = _reinvest(result_b2)
        reduce_term_interest_saved = round(
            baseline_total_interest - reduce_term_total_interest
            + reduce_term_reinvest_income, 2)
        reduce_term_months_to_payoff = result_b2.months_to_payoff
        # Обе величины на карточке считаются от РЕАЛЬНОГО базового графика.
        # Раньше здесь стояла длина сетки дат, и числа на одной карточке
        # противоречили друг другу: «кредит закрыт за 20 месяцев» рядом с
        # «сэкономлено 39 месяцев» на базе в 22 платежа.
        reduce_term_months_saved = max(baseline_months - result_b2.months_to_payoff, 0)

    # --- Strategy C: snowball ---
    snowball_fields = {}

    if monthly_budget:
        # When lump_sum has no explicit date but a deposit term is set, delay the lump
        # in the snowball until the deposit matures (monthly extras still run from month 1).
        if lump_sum > 0 and not lump_sum_date and deposit and maturity_idx is not None:
            snowball_lump_idx = maturity_idx
        else:
            snowball_lump_idx = lump_idx
        snow = simulate_snowball(
            mortgage['loan_amount'],
            mortgage['annual_rate'],
            mortgage['first_payment_date'],
            mortgage['last_payment_date'],
            lump_sum,
            snowball_lump_idx,
            monthly_budget,
            monthly_idx,
            monthly_extra_day=monthly_extra_day,
            mode=repayment_mode,
            contract_payment=mortgage['monthly_payment'],
            adjust_business_days=adj,
            allocation=allocation,
        )
        snow_interest = snow.total_interest
        snow_months = snow.months_to_payoff
        snow_schedule = snow.schedule
        snow_interest_saved = round(baseline_total_interest - snow_interest, 2)

        # Deposit alternative with same money over deposit term
        # Deposit alternative: put (budget − original_annuity) on deposit each month,
        # starting with lump_sum. Simulate the full baseline term; record crossover month.
        snow_dep_income, snow_dep_final, snow_dep_months_to_match = 0.0, 0.0, None
        snow_dep_series = []
        if True:  # always calculate; use fixed CB RF average rate
            monthly_surplus = max((monthly_budget or 0) - mortgage['monthly_payment'], 0)
            dep_rate_m = 8.0 / 100 / 12  # avg CB RF rate over 20 years
            dep_balance = float(lump_sum)
            initial_dep = dep_balance
            total_added = 0.0
            for idx, row in enumerate(base_schedule):
                dep_balance = dep_balance * (1 + dep_rate_m) + monthly_surplus
                total_added += monthly_surplus
                snow_dep_series.append({'date': row['date'], 'balance': round(dep_balance, 2)})
                if dep_balance >= row['balance'] and snow_dep_months_to_match is None:
                    snow_dep_months_to_match = idx + 1
                    snow_dep_final = round(dep_balance, 2)
                    snow_dep_income = round(dep_balance - initial_dep - total_added, 2)
            if snow_dep_months_to_match is None:
                snow_dep_final = round(dep_balance, 2)
                snow_dep_income = round(dep_balance - initial_dep - total_added, 2)
                snow_dep_months_to_match = len(base_schedule)

        snowball_fields = {
            'snowball_total_interest': snow_interest,
            'snowball_interest_saved': snow_interest_saved,
            'snowball_months_to_payoff': snow_months,
            'snowball_schedule': snow_schedule,
            'snowball_lump_unused': snow.lump_unused,
            'snowball_status': snow.status,
            'snowball_own_cost': round(snow_interest, 2),
            'snowball_deposit_income': snow_dep_income,
            'snowball_deposit_final': snow_dep_final,
            'snowball_deposit_months_to_match': snow_dep_months_to_match,
            'snowball_deposit_series': snow_dep_series,
            'monthly_surplus': round(max((monthly_budget or 0) - mortgage['monthly_payment'], 0), 2),
            'monthly_budget': monthly_budget,
        }

    options = {
        'deposit': deposit_net_saving,
        'reduce_payment': reduce_payment_interest_saved,
        'reduce_term': reduce_term_interest_saved,
    }
    if snowball_fields:
        options['snowball'] = snowball_fields['snowball_interest_saved']

    # Статус сценария рядом с его выгодой (решение 6 роадмапа). Несостоявшееся
    # событие — дата досрочки за концом графика — даёт график, побайтово равный
    # базовому, и экономию ровно 0,00 ₽. Ноль больше любой отрицательной
    # величины, поэтому в конкурсе по `max` такой сценарий выигрывал у реального
    # вклада и приложение рекомендовало досрочку, которой не будет.
    option_statuses = {
        'deposit': deposit_status,
        'reduce_payment': reduce_payment_status,
        'reduce_term': reduce_term_status,
    }
    if snowball_fields:
        option_statuses['snowball'] = snowball_fields['snowball_status']

    # Из конкурса вылетает сценарий, который НИЧЕГО НЕ СДЕЛАЛ И НИЧЕГО НЕ ДАЛ:
    # событие не состоялось И выгода равна нулю. Обоих условий сразу, потому что
    # `not_applicable` приходит от движка и говорит только про ипотечную часть.
    # У арма вклада ипотечное событие тоже может не состояться (кредит закрылся
    # раньше, чем созрел вклад), но проценты по вкладу при этом реально
    # заработаны — такой арм остаётся в конкурсе.
    #
    # Если не состоялся ни один сценарий, выбор идёт по всем: выбирать иначе
    # было бы не из чего.
    def _is_dead(name, value):
        return (option_statuses.get(name) == STATUS_NOT_APPLICABLE
                and abs(value) < 0.005)

    applicable = {name: value for name, value in options.items()
                  if not _is_dead(name, value)}
    pool = applicable or options
    winner = max(pool, key=pool.get)

    # own_cost = проценты − доход по вкладу − доход по реинвесту (решение 5).
    # `lump_unused` в метрику не входит: излишек уже внутри `deposit_income`.
    own_cost = {
        'baseline': round(baseline_total_interest, 2),
        'deposit': round(deposit_total_interest - deposit_income - deposit_reinvest_income, 2),
        'reduce_payment': round(reduce_payment_total_interest
                                - reduce_payment_reinvest_income, 2),
        'reduce_term': round(reduce_term_total_interest - reduce_term_reinvest_income, 2),
    }
    if snowball_fields:
        own_cost['snowball'] = snowball_fields['snowball_own_cost']

    # Машинная проверка честности потоков (решение 7). Непустой отчёт означает,
    # что армы тратят разные деньги и сравнение недействительно.
    base_cash = cash_by_month(base_schedule)
    cash_parity = {
        'deposit': cash_parity_report(
            base_cash, cash_by_month(deposit_schedule, deposit_cash_extra),
            deposit_exclude_months),
        'reduce_payment': cash_parity_report(
            base_cash, cash_by_month(reduce_payment_schedule, reduce_payment_cash_extra),
            _early_months(reduce_payment_schedule)),
        'reduce_term': cash_parity_report(
            base_cash, cash_by_month(reduce_term_schedule, reduce_term_cash_extra),
            _early_months(reduce_term_schedule)),
    }
    cash_parity_notes = {}
    if snowball_fields:
        cash_parity_notes['snowball'] = (
            'сценарий тратит месячный бюджет, база «чистая ипотека» — только договорной '
            'платёж; паритет проверяется внутри семьи «снежный ком» (И7)'
        )

    return {
        'baseline_total_interest': baseline_total_interest,
        'monthly_payment': monthly_payment,
        'entered_monthly_payment': mortgage['monthly_payment'],
        'early_repayment_allocation': allocation,
        'base_schedule': base_schedule,
        # Поля `balance_after_deposit` в ответе нет: его никто не читал —
        # роут выбрасывал его до записи в БД, в app.js и в шаблонах его не было.
        # База сравнения (решение 7): переключение обязано быть видимым
        'baseline_kind': baseline_kind,
        'baseline_kind_reason': baseline_kind_reason,
        # Full schedules (popped by the route, never stored in the DB)
        'deposit_schedule': deposit_schedule,
        'reduce_payment_schedule': reduce_payment_schedule,
        'reduce_term_schedule': reduce_term_schedule,
        # Strategy A
        'deposit_income': deposit_income,
        'deposit_final': deposit_final,
        'deposit_net_saving': deposit_net_saving,
        'deposit_new_monthly': deposit_new_monthly,
        'deposit_term_months': (deposit or {}).get('term_months', 0),
        'deposit_total_interest': deposit_total_interest,
        'deposit_reinvest_income': deposit_reinvest_income,
        'deposit_lump_unused': deposit_lump_unused,
        'deposit_status': deposit_status,
        # Strategy B1: reduce payment
        'reduce_payment_new_monthly': new_monthly_b,
        'reduce_payment_interest_saved': reduce_payment_interest_saved,
        'reduce_payment_total_interest': reduce_payment_total_interest,
        'reduce_payment_reinvest_income': reduce_payment_reinvest_income,
        'reduce_payment_lump_unused': reduce_payment_lump_unused,
        'reduce_payment_status': reduce_payment_status,
        # Strategy B2: reduce term
        'reduce_term_months_to_payoff': reduce_term_months_to_payoff,
        'reduce_term_months_saved': reduce_term_months_saved,
        'reduce_term_interest_saved': reduce_term_interest_saved,
        'reduce_term_total_interest': reduce_term_total_interest,
        'reduce_term_reinvest_income': reduce_term_reinvest_income,
        'reduce_term_lump_unused': reduce_term_lump_unused,
        'reduce_term_status': reduce_term_status,
        # Strategy C
        **snowball_fields,
        # Summary
        'winner': winner,
        'options': options,
        # Статус каждого варианта и пул, из которого выбран победитель:
        # сценарий со `status='not_applicable'` в конкурсе не участвует.
        'option_statuses': option_statuses,
        'options_applicable': applicable,
        'own_cost': own_cost,
        'cash_parity': cash_parity,
        'cash_parity_notes': cash_parity_notes,
        'cash_parity_ok': not any(cash_parity.values()),
    }
