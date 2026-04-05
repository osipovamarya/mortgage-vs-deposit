from datetime import datetime
from dateutil.relativedelta import relativedelta
from flask import Blueprint, request, jsonify
from ..database import get_db
from ..calculator import run_comparison, build_amortization

comparison_bp = Blueprint('comparison', __name__, url_prefix='/api/comparison')


@comparison_bp.route('', methods=['POST'])
def create_comparison():
    data = request.get_json()

    if not data.get('strategy_id') or not data.get('deposit_id'):
        return jsonify({'error': 'strategy_id и deposit_id обязательны'}), 400

    db = get_db()

    s_row = db.execute('SELECT * FROM repayment_strategy WHERE id = ?', (data['strategy_id'],)).fetchone()
    if not s_row:
        return jsonify({'error': 'Стратегия погашения не найдена'}), 404

    strategy = dict(s_row)

    m_row = db.execute('SELECT * FROM mortgage WHERE id = ?', (strategy['mortgage_id'],)).fetchone()
    if not m_row:
        return jsonify({'error': 'Ипотека не найдена'}), 404

    d_row = db.execute('SELECT * FROM deposit WHERE id = ?', (data['deposit_id'],)).fetchone()
    if not d_row:
        return jsonify({'error': 'Вклад не найден'}), 404

    mortgage = dict(m_row)
    deposit = dict(d_row)

    result = run_comparison(mortgage, deposit, strategy)

    # Pop non-DB fields before insert
    base_schedule = result.pop('base_schedule')
    balance_after_deposit = result.pop('balance_after_deposit')
    snowball_schedule = result.pop('snowball_schedule', None)
    snowball_deposit_series = result.pop('snowball_deposit_series', None)

    cursor = db.execute(
        """INSERT INTO comparison (
            repayment_strategy_id, deposit_id,
            deposit_income, deposit_final,
            deposit_net_saving, deposit_new_monthly,
            reduce_payment_new_monthly, reduce_payment_interest_saved,
            snowball_total_interest, snowball_interest_saved,
            snowball_months_to_payoff, snowball_deposit_income, snowball_deposit_final,
            baseline_total_interest, winner
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data['strategy_id'], data['deposit_id'],
            result['deposit_income'], result['deposit_final'],
            result['deposit_net_saving'], result['deposit_new_monthly'],
            result['reduce_term_months_saved'], result['reduce_payment_interest_saved'],
            result.get('snowball_total_interest'), result.get('snowball_interest_saved'),
            result.get('snowball_months_to_payoff'), result.get('snowball_deposit_income'),
            result.get('snowball_deposit_final'),
            result['baseline_total_interest'],
            result['winner'],
        ),
    )
    db.commit()

    # Build full schedules for the frontend (not stored in DB)
    from ..calculator import _parse_date, _date_to_idx
    from dateutil.rrule import rrule, MONTHLY

    first_dt = _parse_date(mortgage['first_payment_date'])
    last_dt = _parse_date(mortgage['last_payment_date'])
    next_dt = first_dt + relativedelta(months=1)
    adj = bool(mortgage.get('adjust_business_days'))

    lump_sum = float(strategy.get('lump_sum') or 0)
    lump_sum_date = _parse_date(strategy.get('lump_sum_date'))
    scheduled_dates = list(rrule(MONTHLY, dtstart=next_dt, until=last_dt))
    lump_idx = _date_to_idx(lump_sum_date, scheduled_dates)

    balance_at_lump = base_schedule[lump_idx - 1]['balance'] if lump_idx > 0 else mortgage['loan_amount']
    new_loan = max(balance_at_lump - lump_sum, 0)

    repayment_dt_b = next_dt + relativedelta(months=lump_idx)

    repayment_mode = strategy.get('repayment_mode', 'reduce_payment')

    def build_rp_schedule(fixed_pmt):
        post, _, _ = build_amortization(
            new_loan, mortgage['annual_rate'],
            repayment_dt_b, last_dt,
            adjust_business_days=adj,
            fixed_payment=fixed_pmt,
        )
        pre = [dict(r) for r in base_schedule[:lump_idx]]
        if lump_sum > 0 and lump_sum_date:
            early_row = {
                'payment_num': 0,
                'date': lump_sum_date.strftime('%d.%m.%Y'),
                'payment': lump_sum,
                'principal': lump_sum,
                'interest': 0.0,
                'balance': float(new_loan),
                'early': lump_sum,
            }
            sched = pre + [early_row] + post
        else:
            sched = pre + post
        for i, row in enumerate(sched):
            row['payment_num'] = i + 1
        return sched

    rp_fixed = result['reduce_payment_new_monthly'] if repayment_mode == 'reduce_payment' else mortgage['monthly_payment']
    rp_schedule = build_rp_schedule(rp_fixed)

    # Deposit schedule: N months normal + remaining months after lump-sum repayment
    term_months = result['deposit_term_months']
    deposit_schedule_part1 = base_schedule[:term_months]
    new_loan_A = max(balance_after_deposit - result['deposit_final'], 0)
    if new_loan_A > 0.01 and term_months < len(base_schedule) and result['deposit_final'] > 0.01:
        repayment_dt = first_dt + relativedelta(months=term_months + 1)
        dep_fixed = result['deposit_new_monthly'] if repayment_mode == 'reduce_payment' else mortgage['monthly_payment']
        if not dep_fixed or dep_fixed < 1:
            dep_fixed = mortgage['monthly_payment']
        deposit_part2, _, _ = build_amortization(
            new_loan_A, mortgage['annual_rate'], repayment_dt, last_dt,
            adjust_business_days=adj,
            fixed_payment=dep_fixed,
        )
        repayment_date_str = (first_dt + relativedelta(months=term_months + 1)).strftime('%d.%m.%Y')
        deposit_early_row = {
            'payment_num': 0,
            'date': repayment_date_str,
            'payment': result['deposit_final'],
            'principal': result['deposit_final'],
            'interest': 0.0,
            'balance': float(new_loan_A),
            'early': result['deposit_final'],
        }
        deposit_schedule_part1 = deposit_schedule_part1 + [deposit_early_row]
        offset = len(deposit_schedule_part1)
        for row in deposit_part2:
            row['payment_num'] += offset
        deposit_schedule = deposit_schedule_part1 + deposit_part2
    else:
        # No meaningful repayment (no lump sum or fully paid off) — show full baseline
        deposit_schedule = list(base_schedule)

    # Static row for the last-made payment so row 1 always shows entered balance
    static_row = {
        'payment_num': 1,
        'date': first_dt.strftime('%d.%m.%Y'),
        'payment': mortgage['monthly_payment'],
        'principal': 0.0,
        'interest': 0.0,
        'balance': mortgage['loan_amount'],
        'early': 0.0,
    }

    def with_static(sched):
        return [static_row] + [dict(r, payment_num=r['payment_num'] + 1) for r in sched]

    schedules = {
        'baseline':       with_static(base_schedule),
        'deposit':        with_static(deposit_schedule),
        'reduce_payment': with_static(rp_schedule),
    }
    if snowball_schedule:
        schedules['snowball'] = with_static(snowball_schedule)

    return jsonify({
        'id': cursor.lastrowid,
        'repayment_mode': repayment_mode,
        **result,
        'schedules': schedules,
        'snowball_deposit_series': snowball_deposit_series,
    })


@comparison_bp.route('/<int:comparison_id>', methods=['GET'])
def get_comparison(comparison_id):
    row = get_db().execute('SELECT * FROM comparison WHERE id = ?', (comparison_id,)).fetchone()
    if not row:
        return jsonify({'error': 'Не найдено'}), 404
    return jsonify(dict(row))


@comparison_bp.route('', methods=['GET'])
def list_comparisons():
    rows = get_db().execute('SELECT * FROM comparison ORDER BY created_at DESC').fetchall()
    return jsonify([dict(r) for r in rows])
