from datetime import datetime
from dateutil.relativedelta import relativedelta
from flask import Blueprint, request, jsonify
from ..database import get_db
from ..calculator import run_comparison, build_amortization

comparison_bp = Blueprint('comparison', __name__, url_prefix='/api/comparison')


@comparison_bp.route('', methods=['POST'])
def create_comparison():
    data = request.get_json()

    if not data.get('mortgage_id') or not data.get('deposit_id'):
        return jsonify({'error': 'mortgage_id и deposit_id обязательны'}), 400

    db = get_db()

    m_row = db.execute('SELECT * FROM mortgage WHERE id = ?', (data['mortgage_id'],)).fetchone()
    if not m_row:
        return jsonify({'error': 'Ипотека не найдена'}), 404

    d_row = db.execute('SELECT * FROM deposit WHERE id = ?', (data['deposit_id'],)).fetchone()
    if not d_row:
        return jsonify({'error': 'Вклад не найден'}), 404

    mortgage = dict(m_row)
    deposit = dict(d_row)

    result = run_comparison(mortgage, deposit)

    # Extract base_schedule before storing/returning result (not stored in DB)
    base_schedule = result.pop('base_schedule')
    balance_after_deposit = result.pop('balance_after_deposit')

    cursor = db.execute(
        """INSERT INTO comparison (
            mortgage_id, deposit_id,
            deposit_income, deposit_final,
            deposit_net_saving,
            reduce_payment_new_monthly, reduce_payment_interest_saved,
            baseline_total_interest, winner
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data['mortgage_id'], data['deposit_id'],
            result['deposit_income'], result['deposit_final'],
            result['deposit_net_saving'],
            result['reduce_payment_new_monthly'],
            result['reduce_payment_interest_saved'],
            result['baseline_total_interest'],
            result['winner'],
        ),
    )
    db.commit()

    # Build full schedules to send to the frontend (not stored in DB)
    first_dt = datetime.fromisoformat(mortgage['first_payment_date'])
    last_dt = datetime.fromisoformat(mortgage['last_payment_date'])
    next_dt = first_dt + relativedelta(months=1)
    adj = bool(mortgage.get('adjust_business_days'))
    new_loan = max(mortgage['loan_amount'] - deposit['amount'], 0)

    rp_schedule, _, _ = build_amortization(
        new_loan, mortgage['annual_rate'],
        next_dt, last_dt,
        adjust_business_days=adj,
        prev_payment_date=first_dt,
    )

    # Deposit schedule: N months normal + remaining months after lump-sum repayment.
    # base_schedule[0] = May; base_schedule[term_months-1] = first_dt + term_months.
    # Next payment after deposit matures = first_dt + term_months + 1.
    term_months = result['deposit_term_months']
    deposit_schedule_part1 = base_schedule[:term_months]
    new_loan_A = max(balance_after_deposit - result['deposit_final'], 0)
    if new_loan_A > 0.01:
        repayment_dt = first_dt + relativedelta(months=term_months + 1)
        deposit_part2, _, _ = build_amortization(
            new_loan_A, mortgage['annual_rate'], repayment_dt, last_dt,
            adjust_business_days=adj,
        )
        offset = len(deposit_schedule_part1)
        for row in deposit_part2:
            row['payment_num'] += offset
        deposit_schedule = deposit_schedule_part1 + deposit_part2
    else:
        deposit_schedule = deposit_schedule_part1

    # Prepend a static row for the last-made payment (April) so row 1 always
    # shows the entered balance without any recalculation.
    static_row = {
        'payment_num': 1,
        'date': first_dt.strftime('%d.%m.%Y'),
        'payment': mortgage['monthly_payment'],
        'principal': 0.0,
        'interest': 0.0,
        'balance': mortgage['loan_amount'],
    }

    def with_static(sched):
        return [static_row] + [dict(r, payment_num=r['payment_num'] + 1) for r in sched]

    return jsonify({
        'id': cursor.lastrowid,
        **result,
        'schedules': {
            'baseline': with_static(base_schedule),
            'deposit': with_static(deposit_schedule),
            'reduce_payment': with_static(rp_schedule),
        },
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
