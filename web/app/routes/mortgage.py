from datetime import datetime
from dateutil.relativedelta import relativedelta
from flask import Blueprint, request, jsonify
from ..database import get_db
from ..calculator import build_amortization

mortgage_bp = Blueprint('mortgage', __name__, url_prefix='/api/mortgage')


@mortgage_bp.route('', methods=['POST'])
def create_mortgage():
    data = request.get_json()

    for field in ('loan_amount', 'annual_rate', 'first_payment_date', 'last_payment_date', 'monthly_payment'):
        if not data.get(field):
            return jsonify({'error': f'Поле обязательно: {field}'}), 400

    try:
        first_dt = datetime.strptime(data['first_payment_date'], '%d.%m.%Y')
        last_dt = datetime.strptime(data['last_payment_date'], '%d.%m.%Y')
    except ValueError as e:
        return jsonify({'error': f'Неверный формат даты (ожидается ДД.ММ.ГГГГ): {e}'}), 400

    if first_dt >= last_dt:
        return jsonify({'error': 'Дата последнего платежа должна быть позже первого'}), 400

    loan_amount = float(data['loan_amount'])
    annual_rate = float(data['annual_rate'])
    monthly_payment = float(data['monthly_payment'])
    adjust_business_days = 1 if data.get('adjust_business_days') else 0

    next_dt = first_dt + relativedelta(months=1)
    schedule, _, total_interest = build_amortization(
        loan_amount, annual_rate, next_dt, last_dt,
        adjust_business_days=bool(adjust_business_days),
        prev_payment_date=first_dt,
        fixed_payment=monthly_payment,
    )

    def _float_or_none(key):
        v = data.get(key)
        return float(v) if v else None

    def _date_iso_or_none(key):
        v = data.get(key)
        if not v:
            return None
        try:
            return datetime.strptime(v, '%d.%m.%Y').strftime('%Y-%m-%d')
        except ValueError:
            return v  # already ISO

    lump_sum = _float_or_none('lump_sum')
    lump_sum_date = _date_iso_or_none('lump_sum_date')
    monthly_budget = _float_or_none('monthly_budget')
    monthly_start_date = _date_iso_or_none('monthly_start_date')
    monthly_extra_day = int(data['monthly_extra_day']) if data.get('monthly_extra_day') else None
    repayment_mode = data.get('repayment_mode', 'reduce_payment')

    db = get_db()
    cursor = db.execute(
        """INSERT INTO mortgage (name, loan_amount, annual_rate, first_payment_date, last_payment_date, monthly_payment, adjust_business_days)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            data.get('name', 'Моя ипотека'),
            loan_amount,
            annual_rate,
            first_dt.strftime('%Y-%m-%d'),
            last_dt.strftime('%Y-%m-%d'),
            monthly_payment,
            adjust_business_days,
        ),
    )
    mortgage_id = cursor.lastrowid

    strategy_cursor = db.execute(
        """INSERT INTO repayment_strategy
           (mortgage_id, lump_sum, lump_sum_date, monthly_budget, monthly_start_date, monthly_extra_day, repayment_mode)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (mortgage_id, lump_sum, lump_sum_date, monthly_budget, monthly_start_date, monthly_extra_day, repayment_mode),
    )
    db.commit()

    return jsonify({
        'id': mortgage_id,
        'strategy_id': strategy_cursor.lastrowid,
        'monthly_payment': monthly_payment,
        'total_interest': total_interest,
        'payment_count': len(schedule),
    })


@mortgage_bp.route('/<int:mortgage_id>', methods=['GET'])
def get_mortgage(mortgage_id):
    row = get_db().execute('SELECT * FROM mortgage WHERE id = ?', (mortgage_id,)).fetchone()
    if not row:
        return jsonify({'error': 'Не найдено'}), 404
    return jsonify(dict(row))


@mortgage_bp.route('', methods=['GET'])
def list_mortgages():
    rows = get_db().execute('SELECT * FROM mortgage ORDER BY created_at DESC').fetchall()
    return jsonify([dict(r) for r in rows])
