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


# ---------------------------------------------------------------------------
# Mortgage amortization
# ---------------------------------------------------------------------------

def build_amortization(loan_amount, annual_rate, first_payment_date, last_payment_date,
                       adjust_business_days=False, prev_payment_date=None, fixed_payment=None):
    """
    Generate a full month-by-month amortization schedule starting from
    first_payment_date, with loan_amount as the opening balance.

    Payment per period is recalculated each month using the actual number of
    remaining days to last_payment_date, matching the bank formula:
        payment = PMT(rate/12, ROUND(days_left/365*12, 0), balance)

    Returns:
        schedule        — list of dicts, one per payment
        first_payment   — payment amount of the first period
        total_interest  — sum of all interest portions
    """
    if isinstance(first_payment_date, str):
        first_payment_date = datetime.fromisoformat(first_payment_date)
    if isinstance(last_payment_date, str):
        last_payment_date = datetime.fromisoformat(last_payment_date)

    annual_rate_d = _d(annual_rate)
    rate = annual_rate_d / _d(100) / _d(12)

    scheduled = list(rrule(MONTHLY, dtstart=first_payment_date, until=last_payment_date))
    n = len(scheduled)

    if adjust_business_days:
        dates = [_next_business_day(d) for d in scheduled]
        # Previous payment date for day-count of the first period:
        # use explicitly supplied date, or fall back to one month before start.
        _prev = prev_payment_date if prev_payment_date is not None else (first_payment_date - relativedelta(months=1))
        prev_date = _next_business_day(_prev)
    else:
        dates = scheduled
        prev_date = None  # unused

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
            # Last scheduled date: clear whatever remains
            principal = _r2(balance)
            payment = _r2(principal + interest)
        elif fixed_payment is not None:
            # Known fixed payment — derive principal from it
            payment = _d(fixed_payment)
            principal = _r2(payment - interest)
        else:
            # Dynamic: PMT(rate, remaining_n, balance) — recalculate each month
            days_left = (last_payment_date - date).days
            remaining_n = max(round(days_left / 365 * 12), 1)
            factor = (1 + rate) ** remaining_n
            payment = _r2(balance * rate * factor / (factor - 1))
            principal = _r2(payment - interest)

        balance = balance - principal  # exact Decimal subtraction, no rounding
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
        })

    return schedule, float(first_payment), float(_r2(total_interest))


# ---------------------------------------------------------------------------
# Deposit
# ---------------------------------------------------------------------------

def calc_deposit(amount, annual_rate, term_months, capitalization):
    """
    Calculate deposit income.

    capitalization=True  → compound interest, capitalised monthly
    capitalization=False → simple interest
    """
    if capitalization:
        monthly_rate = annual_rate / 100 / 12
        final_amount = amount * (1 + monthly_rate) ** term_months
    else:
        final_amount = amount * (1 + (annual_rate / 100) * (term_months / 12))

    income = final_amount - amount
    return round(income, 2), round(final_amount, 2)


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def run_comparison(mortgage, deposit):
    """
    Compare two strategies:
      A  — put savings on a deposit (keep paying mortgage as normal),
           then repay deposit_final → reduce payment
      B  — partial mortgage repayment now → reduce monthly payment

    Args:
        mortgage: dict from DB row (loan_amount, annual_rate, first_payment_date,
                                    last_payment_date, monthly_payment,
                                    adjust_business_days)
        deposit:  dict from DB row (amount, annual_rate, term_months, capitalization)

    Returns a dict with all results needed by the comparison route and the frontend.
    """
    monthly_rate = mortgage['annual_rate'] / 100 / 12
    adj = bool(mortgage.get('adjust_business_days'))

    first_dt = datetime.fromisoformat(mortgage['first_payment_date'])
    last_dt = datetime.fromisoformat(mortgage['last_payment_date'])
    # first_dt is the last payment already made; future schedule starts next month
    next_dt = first_dt + relativedelta(months=1)

    # Baseline: original mortgage schedule, no changes.
    # Starts from next_dt (May); first_dt is passed as prev_payment_date
    # so day-accurate interest for the first future period is correct.
    base_schedule, monthly_payment, baseline_total_interest = build_amortization(
        mortgage['loan_amount'],
        mortgage['annual_rate'],
        next_dt,
        last_dt,
        adjust_business_days=adj,
        prev_payment_date=first_dt,
        fixed_payment=mortgage['monthly_payment'],
    )

    original_n = len(base_schedule)

    # New principal after immediate lump-sum repayment
    new_loan = max(mortgage['loan_amount'] - deposit['amount'], 0)

    # --- Strategy A: Deposit for N months, then repay deposit_final → reduce payment ---
    deposit_income, deposit_final = calc_deposit(
        deposit['amount'],
        deposit['annual_rate'],
        deposit['term_months'],
        deposit['capitalization'],
    )

    term_months = min(deposit['term_months'], original_n)
    interest_during_deposit = round(sum(row['interest'] for row in base_schedule[:term_months]), 2)
    balance_after_deposit = base_schedule[term_months - 1]['balance'] if term_months > 0 else mortgage['loan_amount']

    # base_schedule[0] = May 2026, base_schedule[term_months-1] = first_dt + term_months months.
    # The NEXT payment after deposit matures is one month later.
    repayment_dt = first_dt + relativedelta(months=term_months + 1)
    new_loan_A = max(balance_after_deposit - deposit_final, 0)
    remaining_n = original_n - term_months

    if new_loan_A <= 0.01 or remaining_n <= 0:
        interest_after_repayment_A = 0.0
        deposit_new_monthly = 0.0
    else:
        # First month's payment after repayment (for display)
        deposit_new_monthly = round(new_loan_A * (monthly_rate / (1 - (1 + monthly_rate) ** -remaining_n)), 2)
        # Full schedule uses dynamic recalculation
        _, _, interest_after_repayment_A = build_amortization(
            new_loan_A, mortgage['annual_rate'], repayment_dt, last_dt,
            adjust_business_days=adj,
        )

    deposit_net_saving = round(baseline_total_interest - (interest_during_deposit + interest_after_repayment_A), 2)

    # --- Strategy B: Reduce payment (keep same remaining term) ---
    new_monthly_b = round(new_loan * (monthly_rate / (1 - (1 + monthly_rate) ** -original_n)), 2)

    _, _, b_total_interest = build_amortization(
        new_loan, mortgage['annual_rate'], next_dt, last_dt,
        adjust_business_days=adj,
        prev_payment_date=first_dt,
    )
    reduce_payment_interest_saved = round(baseline_total_interest - b_total_interest, 2)

    # --- Winner ---
    options = {
        'deposit': deposit_net_saving,
        'reduce_payment': reduce_payment_interest_saved,
    }
    winner = max(options, key=options.get)

    return {
        'baseline_total_interest': baseline_total_interest,
        'monthly_payment': monthly_payment,
        'entered_monthly_payment': mortgage['monthly_payment'],
        'base_schedule': base_schedule,
        # Strategy A
        'deposit_income': deposit_income,
        'deposit_final': deposit_final,
        'deposit_net_saving': deposit_net_saving,
        'deposit_new_monthly': deposit_new_monthly,
        'deposit_term_months': deposit['term_months'],
        'balance_after_deposit': balance_after_deposit,
        # Strategy B
        'reduce_payment_new_monthly': new_monthly_b,
        'reduce_payment_interest_saved': reduce_payment_interest_saved,
        # Summary
        'winner': winner,
        'options': options,
    }
