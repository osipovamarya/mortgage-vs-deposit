from flask import Blueprint, request, jsonify
from ..database import get_db

deposit_bp = Blueprint('deposit', __name__, url_prefix='/api/deposit')


@deposit_bp.route('', methods=['POST'])
def create_deposit():
    data = request.get_json()

    for field in ('annual_rate', 'term_months'):
        if not data.get(field):
            return jsonify({'error': f'Поле обязательно: {field}'}), 400

    annual_rate = float(data['annual_rate'])
    term_months = int(data['term_months'])
    capitalization = int(data.get('capitalization', 1))

    db = get_db()
    cursor = db.execute(
        "INSERT INTO deposit (name, annual_rate, term_months, capitalization) VALUES (?, ?, ?, ?)",
        (data.get('name', 'Мой вклад'), annual_rate, term_months, capitalization),
    )
    db.commit()

    return jsonify({'id': cursor.lastrowid})


@deposit_bp.route('/<int:deposit_id>', methods=['GET'])
def get_deposit(deposit_id):
    row = get_db().execute('SELECT * FROM deposit WHERE id = ?', (deposit_id,)).fetchone()
    if not row:
        return jsonify({'error': 'Не найдено'}), 404
    return jsonify(dict(row))
