from datetime import datetime
from flask import Blueprint, request, jsonify
from ..database import get_db
from ..calculator import calc_deposit

deposit_bp = Blueprint('deposit', __name__, url_prefix='/api/deposit')


@deposit_bp.route('', methods=['POST'])
def create_deposit():
    data = request.get_json()

    for field in ('amount', 'annual_rate', 'term_months'):
        if not data.get(field):
            return jsonify({'error': f'Поле обязательно: {field}'}), 400

    start_date_str = data.get('start_date') or datetime.today().strftime('%d.%m.%Y')
    try:
        start_dt = datetime.strptime(start_date_str, '%d.%m.%Y')
    except ValueError as e:
        return jsonify({'error': f'Неверный формат даты: {e}'}), 400

    amount = float(data['amount'])
    annual_rate = float(data['annual_rate'])
    term_months = int(data['term_months'])
    capitalization = int(data.get('capitalization', 1))

    income, final_amount = calc_deposit(amount, annual_rate, term_months, capitalization)

    db = get_db()
    cursor = db.execute(
        """INSERT INTO deposit (name, amount, annual_rate, term_months, capitalization, start_date)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            data.get('name', 'Мой вклад'),
            amount,
            annual_rate,
            term_months,
            capitalization,
            start_dt.strftime('%Y-%m-%d'),
        ),
    )
    db.commit()

    return jsonify({
        'id': cursor.lastrowid,
        'income': income,
        'final_amount': final_amount,
    })


@deposit_bp.route('/<int:deposit_id>', methods=['GET'])
def get_deposit(deposit_id):
    row = get_db().execute('SELECT * FROM deposit WHERE id = ?', (deposit_id,)).fetchone()
    if not row:
        return jsonify({'error': 'Не найдено'}), 404
    return jsonify(dict(row))
