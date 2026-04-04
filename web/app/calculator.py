"""
Core financial calculation logic.

All mortgage calculations use the annuity (равные платежи) method.
Deposit calculations support both compound (капитализация) and simple interest.
"""
from datetime import datetime
from dateutil.rrule import rrule, MONTHLY
from dateutil.relativedelta import relativedelta


# ---------------------------------------------------------------------------
# Mortgage amortization
# ---------------------------------------------------------------------------

def build_amortization(loan_amount, annual_rate, first_payment_date, last_payment_date, monthly_payment=None):
    """
    Generate a full month-by-month amortization schedule.

    Returns:
        schedule        — list of dicts, one per payment
        monthly_payment — the fixed monthly payment amount
        total_interest  — sum of all interest portions
    """
    if isinstance(first_payment_date, str):
        first_payment_date = datetime.fromisoformat(first_payment_date)
    if isinstance(last_payment_date, str):
        last_payment_date = datetime.fromisoformat(last_payment_date)

    monthly_rate = annual_rate / 100 / 12
    dates = list(rrule(MONTHLY, dtstart=first_payment_date, until=last_payment_date))
    n = len(dates)

    if not monthly_payment:
        # Annuity formula: M = P * r / (1 - (1+r)^-n)
        monthly_payment = loan_amount * (monthly_rate / (1 - (1 + monthly_rate) ** -n))
        monthly_payment = round(monthly_payment, 2)

    schedule = []
    balance = loan_amount
    total_interest = 0.0

    for i, date in enumerate(dates):
        interest = round(balance * monthly_rate, 2)

        if i == n - 1:
            # Last payment clears any remaining balance
            principal = round(balance, 2)
            payment = round(principal + interest, 2)
        else:
            principal = round(monthly_payment - interest, 2)
            payment = monthly_payment

        balance = round(max(balance - principal, 0), 2)
        total_interest += interest

        schedule.append({
            'payment_num': i + 1,
            'date': date.strftime('%d.%m.%Y'),
            'payment': payment,
            'principal': principal,
            'interest': interest,
            'balance': balance,
        })

    return schedule, monthly_payment, round(total_interest, 2)


def _count_payments_until_zero(new_loan, monthly_rate, monthly_payment):
    """
    Simulate how many monthly payments it takes to pay off new_loan
    at the given fixed monthly_payment. Returns the number of payments.
    """
    balance = new_loan
    count = 0
    while balance > 0.01 and count < 10000:
        interest = balance * monthly_rate
        principal = monthly_payment - interest
        if principal <= 0:
            # Payment can't cover interest — shouldn't happen with valid data
            break
        balance = max(balance - principal, 0)
        count += 1
    return count


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
    Compare three strategies:
      A  — put savings on a deposit (keep paying mortgage as normal)
      B1 — partial mortgage repayment → reduce remaining term
      B2 — partial mortgage repayment → reduce monthly payment

    Args:
        mortgage: dict from DB row (loan_amount, annual_rate, first_payment_date,
                                    last_payment_date, monthly_payment)
        deposit:  dict from DB row (amount, annual_rate, term_months, capitalization)

    Returns a dict with all results needed by the comparison route and the frontend.
    """
    monthly_rate = mortgage['annual_rate'] / 100 / 12

    first_dt = datetime.fromisoformat(mortgage['first_payment_date'])
    last_dt = datetime.fromisoformat(mortgage['last_payment_date'])

    # Baseline: original mortgage schedule, no changes
    _, monthly_payment, baseline_total_interest = build_amortization(
        mortgage['loan_amount'],
        mortgage['annual_rate'],
        first_dt,
        last_dt,
        mortgage['monthly_payment'],
    )

    original_dates = list(rrule(MONTHLY, dtstart=first_dt, until=last_dt))
    original_n = len(original_dates)

    # New principal after the lump-sum repayment
    new_loan = max(mortgage['loan_amount'] - deposit['amount'], 0)

    # --- Strategy A: Deposit ---
    deposit_income, deposit_final = calc_deposit(
        deposit['amount'],
        deposit['annual_rate'],
        deposit['term_months'],
        deposit['capitalization'],
    )

    # --- Strategy B1: Reduce term (keep same monthly payment) ---
    new_n_b1 = _count_payments_until_zero(new_loan, monthly_rate, monthly_payment)
    new_last_date_b1 = first_dt + relativedelta(months=new_n_b1 - 1)

    _, _, b1_total_interest = build_amortization(
        new_loan, mortgage['annual_rate'], first_dt, new_last_date_b1, monthly_payment
    )
    reduce_term_interest_saved = round(baseline_total_interest - b1_total_interest, 2)
    months_saved = original_n - new_n_b1

    # --- Strategy B2: Reduce payment (keep same remaining term) ---
    new_monthly_b2 = new_loan * (monthly_rate / (1 - (1 + monthly_rate) ** -original_n))
    new_monthly_b2 = round(new_monthly_b2, 2)

    _, _, b2_total_interest = build_amortization(
        new_loan, mortgage['annual_rate'], first_dt, last_dt, new_monthly_b2
    )
    reduce_payment_interest_saved = round(baseline_total_interest - b2_total_interest, 2)

    # --- Winner ---
    options = {
        'deposit': deposit_income,
        'reduce_term': reduce_term_interest_saved,
        'reduce_payment': reduce_payment_interest_saved,
    }
    winner = max(options, key=options.get)

    return {
        'baseline_total_interest': baseline_total_interest,
        'monthly_payment': monthly_payment,
        # Strategy A
        'deposit_income': deposit_income,
        'deposit_final': deposit_final,
        # Strategy B1
        'reduce_term_new_last_date': new_last_date_b1.strftime('%Y-%m-%d'),
        'reduce_term_months_saved': months_saved,
        'reduce_term_interest_saved': reduce_term_interest_saved,
        # Strategy B2
        'reduce_payment_new_monthly': new_monthly_b2,
        'reduce_payment_interest_saved': reduce_payment_interest_saved,
        # Summary
        'winner': winner,
        'options': options,
    }
