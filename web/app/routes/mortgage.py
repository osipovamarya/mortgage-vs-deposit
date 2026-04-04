from datetime import datetime
from flask import Blueprint, request, jsonify
from ..database import get_db
from ..calculator import build_amortization

mortgage_bp = Blueprint('mortgage', __name__, url_prefix='/api/mortgage')


@mortgage_bp.route('', methods=['POST'])
def create_mortgage():
    data = request.get_json()

    for field in ('loan_amount', 'annual_rate', 'first_payment_date', 'last_payment_date'):
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
    monthly_payment = float(data['monthly_payment']) if data.get('monthly_payment') else None

    schedule, monthly_payment, total_interest = build_amortization(
        loan_amount, annual_rate, first_dt, last_dt, monthly_payment
    )

    db = get_db()
    cursor = db.execute(
        """INSERT INTO mortgage (name, loan_amount, annual_rate, first_payment_date, last_payment_date, monthly_payment)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            data.get('name', 'Моя ипотека'),
            loan_amount,
            annual_rate,
            first_dt.strftime('%Y-%m-%d'),
            last_dt.strftime('%Y-%m-%d'),
            monthly_payment,
        ),
    )
    db.commit()

    return jsonify({
        'id': cursor.lastrowid,
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
