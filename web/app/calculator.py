"""
Core financial calculation logic.

All mortgage calculations use the annuity (равные платежи) method.
Deposit calculations support both compound (капитализация) and simple interest.
"""
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from dateutil.rrule import rrule, MONTHLY
from dateutil.relativedelta import relativedelta

_CENT = Decimal('0.01')

def _d(x):
    return Decimal(str(x))

def _r2(x):
    return x.quantize(_CENT, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _next_business_day(date):
    """Move Saturday → Monday, Sunday → Monday. No holiday calendar."""
    wd = date.weekday()
    if wd == 5:
        return date + timedelta(days=2)
    if wd == 6:
        return date + timedelta(days=1)
    return date


def _parse_date(value):
    """Parse DD.MM.YYYY or ISO date string to datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.strptime(value, '%d.%m.%Y')
    except ValueError:
        return datetime.fromisoformat(value)


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
    return 0  # past end → apply immediately (conservative)


# ---------------------------------------------------------------------------
# Mortgage amortization
# ---------------------------------------------------------------------------

def build_amortization(loan_amount, annual_rate, first_payment_date, last_payment_date,
                       adjust_business_days=False, prev_payment_date=None, fixed_payment=None):
    """
    Generate a full month-by-month amortization schedule.

    Returns:
        schedule        — list of dicts, one per payment
        first_payment   — payment amount of the first period
        total_interest  — sum of all interest portions
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
        prev_date = _next_business_day(_prev)
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
        elif fixed_payment is not None:
            payment = _d(fixed_payment)
            principal = _r2(payment - interest)
            if principal >= balance:
                # Loan pays off early — treat as final payment
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
# Snowball + one-time repayment simulation
# ---------------------------------------------------------------------------

def calc_repayment_schedule(loan_amount, annual_rate, first_payment_date, last_payment_date,
                             lump_sum, lump_idx,
                             monthly_budget, monthly_idx,
                             monthly_extra_day=None):
    """
    Unified repayment simulation supporting both lump-sum and snowball.

    When monthly_extra_day is set (must be > annuity day):
      - Annuity date: pay interest (accrued over FULL period from prev payment,
        split across pre/post early repayment if applicable) + principal.
      - Extra date (payday): pay principal ONLY — no interest. Early repayment
        reduces the balance immediately; the interest saving shows up as lower
        interest in the NEXT month's annuity (daily accrual on reduced balance).

    Returns: (total_interest, months_to_payoff, schedule)
    """
    import calendar as _cal

    if isinstance(first_payment_date, str):
        first_payment_date = _parse_date(first_payment_date)
    if isinstance(last_payment_date, str):
        last_payment_date = _parse_date(last_payment_date)

    next_dt = first_payment_date + relativedelta(months=1)
    scheduled_dates = list(rrule(MONTHLY, dtstart=next_dt, until=last_payment_date))
    original_n = len(scheduled_dates)

    monthly_rate_d = _d(annual_rate) / _d(100) / _d(12)
    daily_rate_d   = _d(annual_rate) / _d(100) / _d(365)
    lump_d = _d(lump_sum or 0)
    budget_d = _d(monthly_budget or 0)

    balance = _d(loan_amount)
    total_interest = Decimal('0')
    schedule = []
    lump_applied = False

    # State carried across iterations for split-day interest calculation:
    #   prev_annuity_date  — date of the last annuity row
    #   split_info         — (early_date, balance_before_early) when the previous
    #                        month had a separate early-repayment row; None otherwise
    prev_annuity_date = first_payment_date
    split_info = None  # type: tuple | None

    for i, date in enumerate(scheduled_dates):
        if balance <= Decimal('0.01'):
            break

        remaining_n = original_n - i

        # ── Interest for this annuity ──────────────────────────────────────
        if split_info is not None:
            # Previous month had an early repayment on split_info[0].
            # balance here = balance AFTER that early repayment.
            # Accrue daily interest: period-1 (prev_annuity → early) on the
            # pre-early balance; period-2 (early → today) on the post-early balance.
            early_date_prev, bal_before_early = split_info
            days1 = (early_date_prev - prev_annuity_date).days
            days2 = (date - early_date_prev).days
            interest = _r2(
                bal_before_early * daily_rate_d * _d(days1) +
                balance           * daily_rate_d * _d(days2)
            )
            split_info = None
        elif monthly_extra_day:
            # No prior early, but daily-rate mode is active for consistency.
            days = (date - prev_annuity_date).days
            interest = _r2(balance * daily_rate_d * _d(days))
        else:
            interest = _r2(balance * monthly_rate_d)

        # ── Regular annuity (recalculated from current balance) ────────────
        if remaining_n == 1:
            annuity = _r2(balance + interest)
        else:
            factor = (1 + monthly_rate_d) ** remaining_n
            annuity = _r2(balance * monthly_rate_d * factor / (factor - 1))

        snowball_active = bool(monthly_budget) and i >= monthly_idx
        lump_this_month = not lump_applied and lump_d > Decimal('0') and i == lump_idx

        # ── Decide whether to split onto different dates ───────────────────
        use_split = False
        extra_date = None
        if snowball_active and monthly_extra_day:
            extra_day_int = int(monthly_extra_day)
            last_day = _cal.monthrange(date.year, date.month)[1]
            clamped = min(extra_day_int, last_day)
            candidate = date.replace(day=clamped)
            if candidate.day > date.day:
                use_split = True
                extra_date = candidate

        if use_split:
            # ── Row 1: annuity on scheduled date ──────────────────────────
            regular_base = min(annuity, budget_d)
            principal_reg = _r2(regular_base - interest)
            if principal_reg < Decimal('0'):
                principal_reg = Decimal('0')
                regular_base = interest

            early_row1 = Decimal('0')
            if lump_this_month:
                lump_cap = min(lump_d, balance - principal_reg)
                lump_cap = max(lump_cap, Decimal('0'))
                principal_reg += lump_cap
                regular_base  += lump_cap
                early_row1 = lump_cap
                lump_applied = True

            balance -= principal_reg
            balance  = max(balance, Decimal('0'))
            total_interest += interest
            prev_annuity_date = date

            schedule.append({
                'payment_num': len(schedule) + 1,
                'date':        date.strftime('%d.%m.%Y'),
                'payment':     float(regular_base),
                'principal':   float(principal_reg),
                'interest':    float(interest),
                'balance':     float(_r2(balance)),
                'early':       float(early_row1),
            })

            if balance <= Decimal('0.01'):
                break

            # ── Row 2: early repayment — principal ONLY, no interest ───────
            # Interest for this gap period will be collected at the NEXT annuity.
            regular_base_nolump = min(annuity, budget_d)
            extra_amount = _r2(budget_d - regular_base_nolump)
            extra_amount = max(extra_amount, Decimal('0'))
            extra_amount = min(extra_amount, balance)

            if extra_amount > Decimal('0.01'):
                bal_before_early = balance          # balance after annuity, before extra
                balance -= extra_amount
                balance  = max(balance, Decimal('0'))
                # No interest added here — accrues until next annuity date.

                schedule.append({
                    'payment_num': len(schedule) + 1,
                    'date':        extra_date.strftime('%d.%m.%Y'),
                    'payment':     float(extra_amount),   # principal only
                    'principal':   float(extra_amount),
                    'interest':    0.0,
                    'balance':     float(_r2(balance)),
                    'early':       float(extra_amount),
                })
                # Carry forward for next month's split interest calculation.
                split_info = (extra_date, bal_before_early)

            if balance <= Decimal('0.01'):
                break

        else:
            # ── Row 1: annuity (interest + regular principal, no early) ────
            principal_reg = _r2(annuity - interest)
            if principal_reg < Decimal('0'):
                principal_reg = Decimal('0')

            # Nearly paid off — sweep everything into one final row
            pay_off_all = snowball_active and (balance + interest) <= budget_d
            if pay_off_all:
                early_final = max(_r2(balance - principal_reg), Decimal('0'))
                balance = Decimal('0')
                total_interest += interest
                prev_annuity_date = date
                schedule.append({
                    'payment_num': len(schedule) + 1,
                    'date':        date.strftime('%d.%m.%Y'),
                    'payment':     float(_r2(interest + principal_reg + early_final)),
                    'principal':   float(_r2(principal_reg + early_final)),
                    'interest':    float(interest),
                    'balance':     0.0,
                    'early':       float(early_final),
                })
                if lump_this_month:
                    lump_applied = True
                break

            # Normal annuity row — early = 0
            balance -= principal_reg
            balance  = max(balance, Decimal('0'))
            total_interest += interest
            prev_annuity_date = date
            schedule.append({
                'payment_num': len(schedule) + 1,
                'date':        date.strftime('%d.%m.%Y'),
                'payment':     float(annuity),
                'principal':   float(principal_reg),
                'interest':    float(interest),
                'balance':     float(_r2(balance)),
                'early':       0.0,
            })

            if balance <= Decimal('0.01'):
                break

            # ── Row 2: early repayment — principal ONLY, no interest ───────
            extra = Decimal('0')
            if snowball_active:
                extra = _r2(max(budget_d - annuity, Decimal('0')))
                extra = min(extra, balance)

            if lump_this_month:
                extra_lump = min(lump_d, balance - extra)
                extra_lump = max(extra_lump, Decimal('0'))
                extra += extra_lump
                lump_applied = True

            if extra > Decimal('0.01'):
                balance -= extra
                balance  = max(balance, Decimal('0'))
                schedule.append({
                    'payment_num': len(schedule) + 1,
                    'date':        date.strftime('%d.%m.%Y'),
                    'payment':     float(extra),
                    'principal':   float(extra),
                    'interest':    0.0,
                    'balance':     float(_r2(balance)),
                    'early':       float(extra),
                })

            if balance <= Decimal('0.01'):
                break

    return float(_r2(total_interest)), len(schedule), schedule


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
        lump_sum, lump_sum_date, monthly_budget, monthly_start_date

    The amount for both deposit and repayment is strategy.lump_sum.
    """
    strategy = strategy or {}
    monthly_rate = mortgage['annual_rate'] / 100 / 12
    adj = bool(mortgage.get('adjust_business_days'))

    first_dt = _parse_date(mortgage['first_payment_date'])
    last_dt = _parse_date(mortgage['last_payment_date'])
    next_dt = first_dt + relativedelta(months=1)

    scheduled_dates = list(rrule(MONTHLY, dtstart=next_dt, until=last_dt))
    original_n = len(scheduled_dates)

    # Strategy parameters
    lump_sum = float(strategy.get('lump_sum') or 0)
    lump_sum_date = _parse_date(strategy.get('lump_sum_date'))
    monthly_budget = float(strategy.get('monthly_budget') or 0) or None
    monthly_start_date = _parse_date(strategy.get('monthly_start_date'))
    monthly_extra_day = strategy.get('monthly_extra_day') or None
    if monthly_extra_day:
        monthly_extra_day = int(monthly_extra_day)

    lump_idx = _date_to_idx(lump_sum_date, scheduled_dates)
    monthly_idx = _date_to_idx(monthly_start_date, scheduled_dates)

    # Baseline: original mortgage schedule, no changes.
    base_schedule, monthly_payment, baseline_total_interest = build_amortization(
        mortgage['loan_amount'],
        mortgage['annual_rate'],
        next_dt,
        last_dt,
        adjust_business_days=adj,
        prev_payment_date=first_dt,
        fixed_payment=mortgage['monthly_payment'],
    )

    # --- Strategy A: deposit lump_sum for T months, then repay → reduce payment ---
    deposit_income = 0.0
    deposit_final = 0.0
    deposit_net_saving = 0.0
    deposit_new_monthly = 0.0
    balance_after_deposit = mortgage['loan_amount']

    if lump_sum > 0 and deposit:
        term_months = min(deposit['term_months'], original_n)
        monthly_surplus = max((monthly_budget or 0) - mortgage['monthly_payment'], 0)

        deposit_income, deposit_final = calc_monthly_deposit(
            lump_sum, monthly_surplus,
            deposit['annual_rate'], deposit['capitalization'],
            term_months,
        )

        interest_during_deposit = round(
            sum(row['interest'] for row in base_schedule[:term_months]), 2
        )
        balance_after_deposit = base_schedule[term_months - 1]['balance'] if term_months > 0 else mortgage['loan_amount']

        repayment_dt = first_dt + relativedelta(months=term_months + 1)
        new_loan_A = max(balance_after_deposit - deposit_final, 0)
        remaining_n = original_n - term_months

        if new_loan_A <= 0.01 or remaining_n <= 0:
            interest_after_repayment_A = 0.0
            deposit_new_monthly = 0.0
        else:
            deposit_new_monthly = round(
                new_loan_A * (monthly_rate / (1 - (1 + monthly_rate) ** -remaining_n)), 2
            )
            _, _, interest_after_repayment_A = build_amortization(
                new_loan_A, mortgage['annual_rate'], repayment_dt, last_dt,
                adjust_business_days=adj,
            )

        deposit_net_saving = round(
            baseline_total_interest - (interest_during_deposit + interest_after_repayment_A), 2
        )

    # --- Strategy B1: lump_sum → reduce payment (lower annuity, same term) ---
    # --- Strategy B2: lump_sum → reduce term  (same payment, shorter term) ---
    reduce_payment_interest_saved = 0.0
    new_monthly_b = mortgage['monthly_payment']
    reduce_term_interest_saved = 0.0
    reduce_term_months_to_payoff = original_n
    reduce_term_months_saved = 0

    if lump_sum > 0:
        interest_before = sum(row['interest'] for row in base_schedule[:lump_idx])
        balance_at_lump = base_schedule[lump_idx - 1]['balance'] if lump_idx > 0 else mortgage['loan_amount']
        new_loan_b = max(balance_at_lump - lump_sum, 0)
        remaining_b = original_n - lump_idx
        repayment_dt_b = next_dt + relativedelta(months=lump_idx)

        if new_loan_b > 0.01 and remaining_b > 0:
            # B1: reduce payment
            new_monthly_b = round(
                new_loan_b * (monthly_rate / (1 - (1 + monthly_rate) ** -remaining_b)), 2
            )
            _, _, interest_after_b1 = build_amortization(
                new_loan_b, mortgage['annual_rate'], repayment_dt_b, last_dt,
                adjust_business_days=adj,
                fixed_payment=new_monthly_b,
            )
            reduce_payment_interest_saved = round(
                baseline_total_interest - (interest_before + interest_after_b1), 2
            )

            # B2: reduce term
            sched_b2, _, interest_after_b2 = build_amortization(
                new_loan_b, mortgage['annual_rate'], repayment_dt_b, last_dt,
                adjust_business_days=adj,
                fixed_payment=mortgage['monthly_payment'],
            )
            reduce_term_months_to_payoff = lump_idx + len(sched_b2)
            reduce_term_months_saved = original_n - reduce_term_months_to_payoff
            reduce_term_interest_saved = round(
                baseline_total_interest - (interest_before + interest_after_b2), 2
            )

    # --- Strategy C: snowball ---
    snowball_fields = {}

    if monthly_budget:
        # When lump_sum has no explicit date but a deposit term is set, delay the lump
        # in the snowball until the deposit matures (monthly extras still run from month 1).
        if lump_sum > 0 and not lump_sum_date and deposit:
            snowball_lump_idx = min(deposit['term_months'], original_n - 1)
        else:
            snowball_lump_idx = lump_idx
        snow_interest, snow_months, snow_schedule = calc_repayment_schedule(
            mortgage['loan_amount'],
            mortgage['annual_rate'],
            mortgage['first_payment_date'],
            mortgage['last_payment_date'],
            lump_sum,
            snowball_lump_idx,
            monthly_budget,
            monthly_idx,
            monthly_extra_day=monthly_extra_day,
        )
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

    winner = max(options, key=options.get)

    return {
        'baseline_total_interest': baseline_total_interest,
        'monthly_payment': monthly_payment,
        'entered_monthly_payment': mortgage['monthly_payment'],
        'base_schedule': base_schedule,
        'balance_after_deposit': balance_after_deposit,
        # Strategy A
        'deposit_income': deposit_income,
        'deposit_final': deposit_final,
        'deposit_net_saving': deposit_net_saving,
        'deposit_new_monthly': deposit_new_monthly,
        'deposit_term_months': (deposit or {}).get('term_months', 0),
        # Strategy B1: reduce payment
        'reduce_payment_new_monthly': new_monthly_b,
        'reduce_payment_interest_saved': reduce_payment_interest_saved,
        # Strategy B2: reduce term
        'reduce_term_months_to_payoff': reduce_term_months_to_payoff,
        'reduce_term_months_saved': reduce_term_months_saved,
        'reduce_term_interest_saved': reduce_term_interest_saved,
        # Strategy C
        **snowball_fields,
        # Summary
        'winner': winner,
        'options': options,
    }
