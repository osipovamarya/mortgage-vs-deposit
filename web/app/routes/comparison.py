from datetime import datetime
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

    cursor = db.execute(
        """INSERT INTO comparison (
            mortgage_id, deposit_id,
            deposit_income, deposit_final,
            reduce_term_new_last_date, reduce_term_months_saved, reduce_term_interest_saved,
            reduce_payment_new_monthly, reduce_payment_interest_saved,
            baseline_total_interest, winner
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data['mortgage_id'], data['deposit_id'],
            result['deposit_income'], result['deposit_final'],
            result['reduce_term_new_last_date'],
            result['reduce_term_months_saved'],
            result['reduce_term_interest_saved'],
            result['reduce_payment_new_monthly'],
            result['reduce_payment_interest_saved'],
            result['baseline_total_interest'],
            result['winner'],
        ),
    )
    db.commit()

    # Build full schedules to send to the frontend (not stored in DB — can be recalculated)
    first_dt = datetime.fromisoformat(mortgage['first_payment_date'])
    last_dt = datetime.fromisoformat(mortgage['last_payment_date'])
    new_loan = mortgage['loan_amount'] - deposit['amount']
    new_last_b1 = datetime.fromisoformat(result['reduce_term_new_last_date'])

    base_schedule, _, _ = build_amortization(
        mortgage['loan_amount'], mortgage['annual_rate'],
        first_dt, last_dt, mortgage['monthly_payment']
    )
    rt_schedule, _, _ = build_amortization(
        new_loan, mortgage['annual_rate'],
        first_dt, new_last_b1, mortgage['monthly_payment']
    )
    rp_schedule, _, _ = build_amortization(
        new_loan, mortgage['annual_rate'],
        first_dt, last_dt, result['reduce_payment_new_monthly']
    )

    return jsonify({
        'id': cursor.lastrowid,
        **result,
        'schedules': {
            'baseline': base_schedule,
            'reduce_term': rt_schedule,
            'reduce_payment': rp_schedule,
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
