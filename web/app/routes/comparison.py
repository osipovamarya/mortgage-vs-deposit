from flask import Blueprint, request, jsonify
from ..database import get_db
from ..calculator import run_comparison, _parse_date

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
    result.pop('balance_after_deposit', None)
    deposit_schedule = result.pop('deposit_schedule')
    reduce_payment_schedule = result.pop('reduce_payment_schedule')
    reduce_term_schedule = result.pop('reduce_term_schedule')
    snowball_schedule = result.pop('snowball_schedule', None)
    snowball_deposit_series = result.pop('snowball_deposit_series', None)

    cursor = db.execute(
        """INSERT INTO comparison (
            repayment_strategy_id, deposit_id,
            deposit_income, deposit_final,
            deposit_net_saving, deposit_new_monthly,
            reduce_payment_new_monthly, reduce_payment_interest_saved,
            reduce_term_interest_saved, reduce_term_months_saved, reduce_term_months_to_payoff,
            snowball_total_interest, snowball_interest_saved,
            snowball_months_to_payoff, snowball_deposit_income, snowball_deposit_final,
            baseline_total_interest, winner
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data['strategy_id'], data['deposit_id'],
            result['deposit_income'], result['deposit_final'],
            result['deposit_net_saving'], result['deposit_new_monthly'],
            result['reduce_payment_new_monthly'], result['reduce_payment_interest_saved'],
            result['reduce_term_interest_saved'], result['reduce_term_months_saved'],
            result['reduce_term_months_to_payoff'],
            result.get('snowball_total_interest'), result.get('snowball_interest_saved'),
            result.get('snowball_months_to_payoff'), result.get('snowball_deposit_income'),
            result.get('snowball_deposit_final'),
            result['baseline_total_interest'],
            result['winner'],
        ),
    )
    db.commit()

    # Schedules are built by the calculator itself (single source of truth),
    # so the table always matches the numbers on the cards.
    first_dt = _parse_date(mortgage['first_payment_date'])
    repayment_mode = strategy.get('repayment_mode', 'reduce_payment')

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
        'reduce_payment': with_static(reduce_payment_schedule),
        'reduce_term':    with_static(reduce_term_schedule),
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
