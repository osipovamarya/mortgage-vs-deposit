import logbook

from app.mortgage import Mortgage
from app.mortgage_count import MortgageCount
from app.telegram_user import TelegramUser
import sqlite3
import json
from collections import defaultdict
from datetime import datetime, timedelta


class MortgageRegistry:
    def __init__(self):
        self.db_connection = None
        self.cursor = None

    def init_db(self, db_path: str):
        db_connection = sqlite3.connect(db_path, check_same_thread=False)
        self.db_connection = db_connection
        self.db_connection.row_factory = sqlite3.Row
        self.run_migrations()

    def run_migrations(self):
        self.db_connection.execute(
            """
                CREATE TABLE IF NOT EXISTS mortgage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    main_debt_sum REAL,
                    interest REAL,
                    mortgage_start DATE,
                    first_payment_date DATE,
                    last_payment_date DATE,
                    month_payment REAL
                )
            """
        )

        self.db_connection.execute(
            """
                CREATE TABLE IF NOT EXISTS mortgage_count (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    mortgage_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    main_debt_sum REAL,
                    interest REAL,
                    mortgage_start DATE,
                    first_payment_date DATE,
                    last_payment_date DATE,
                    month_payment REAL,
                    interest_sum_left REAL
                )
            """
        )

        self.db_connection.execute(
            """
                CREATE TABLE IF NOT EXISTS partial_repayment (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    mortgage_id INTEGER NOT NULL,
                    date DATE NOT NULL,
                    repayment_sum REAL NOT NULL
                )
            """
        )
        self.db_connection.execute(
            """
                CREATE UNIQUE INDEX IF NOT EXISTS mortgage_count_id_partial_repayment_id_idx 
                ON partial_repayment (id, mortgage_id);
            """
        )

    def save_mortgage(self, mortgage: Mortgage):
        self.db_connection.execute(
            """
                INSERT INTO mortgage
                (
                    name,
                    chat_id,
                    main_debt_sum,
                    interest,
                    mortgage_start,
                    first_payment_date,
                    last_payment_date,
                    month_payment
                ) VALUES (
                    :name,
                    :chat_id,
                    :main_debt_sum,
                    :interest,
                    :mortgage_start,
                    :first_payment_date,
                    :last_payment_date,
                    :month_payment
                )
            """,
            {
                "name": mortgage.name,
                "chat_id": mortgage.chat_id,
                "main_debt_sum": mortgage.main_debt_sum,
                "interest": mortgage.interest,
                "mortgage_start": mortgage.mortgage_start,
                "first_payment_date": mortgage.first_payment_date,
                "last_payment_date": mortgage.last_payment_date,
                "month_payment": mortgage.month_payment
            }
        )
        self.db_connection.commit()
        cursor = self.db_connection.execute(
            """
                SELECT
                    id,
                    chat_id,
                    name
                FROM mortgage
                WHERE chat_id = :chat_id
                AND name = :name
                ORDER BY id DESC
                LIMIT 1
            """,
            {
                "chat_id": mortgage.chat_id,
                "name": mortgage.name
            }
        )
        row = cursor.fetchone()
        mortgage.id = row["id"]
        return mortgage

    def save_count(self, mortgage_count: MortgageCount):
        self.db_connection.execute(
            """
                INSERT INTO mortgage_count
                (
                    name,
                    chat_id,
                    mortgage_id,
                    main_debt_sum,
                    interest,
                    mortgage_start,
                    first_payment_date,
                    last_payment_date,
                    month_payment
                ) VALUES (
                    :name,
                    :chat_id,
                    :mortgage_id,
                    :main_debt_sum,
                    :interest,
                    :mortgage_start,
                    :first_payment_date,
                    :last_payment_date,
                    :month_payment
                )
            """,
            {
                "name": mortgage_count.name,
                "chat_id": mortgage_count.chat_id,
                "mortgage_id": mortgage_count.mortgage.id,
                "main_debt_sum": mortgage_count.mortgage.main_debt_sum,
                "interest": mortgage_count.mortgage.interest,
                "mortgage_start": mortgage_count.mortgage.mortgage_start,
                "first_payment_date": mortgage_count.mortgage.first_payment_date,
                "last_payment_date": mortgage_count.mortgage.last_payment_date,
                "month_payment": mortgage_count.month_payment
            }
        )
        self.db_connection.commit()
        cursor = self.db_connection.execute(
            """
                SELECT
                    id,
                    chat_id,
                    name
                FROM mortgage_count
                WHERE chat_id = :chat_id
                AND name = :name
                ORDER BY id DESC
                LIMIT 1
            """,
            {
                "chat_id": mortgage_count.chat_id,
                "name": mortgage_count.name
            }
        )
        row = cursor.fetchone()
        mortgage_count.id = row["id"]
        # mortgage_count.interest_sum_left = row["interest_sum_left"]
        return mortgage_count

    def update_count(self, count: MortgageCount):
        self.db_connection.execute(
            """
                UPDATE mortgage_count
                SET main_debt_sum = :main_debt_sum,
                    month_payment = :month_payment
                WHERE chat_id = :chat_id
                AND id = :id
            """,
            {
                "main_debt_sum": count.main_debt_sum,
                "month_payment": count.month_payment,
                "chat_id": count.chat_id,
                "id": count.id,
            }
        )
        self.db_connection.commit()

    def find_mortgage(self, chat_id: int, mortgage_name: str) -> Mortgage:
        cursor = self.db_connection.execute(
            """
                SELECT *
                FROM mortgage
                WHERE chat_id = :chat_id
                AND name = :name
                ORDER BY id DESC
                LIMIT 1
            """,
            {
                "chat_id": chat_id,
                "name": mortgage_name,
            }
        )
        row = cursor.fetchone()
        if not row:
            return None
        fields = [column[0] for column in cursor.description]
        my_dict = {key: value for key, value in zip(fields, row)}
        mortgage = Mortgage(**my_dict)
        return mortgage

    def find_count(self, chat_id: int, mortgage_id: int, count_name: str) -> MortgageCount:
        cursor = self.db_connection.execute(
            """
                SELECT *
                FROM mortgage_count
                WHERE chat_id = :chat_id
                AND name = :name
                ORDER BY id DESC
                LIMIT 1
            """,
            {
                "chat_id": chat_id,
                "name": count_name,
            }
        )
        row = cursor.fetchone()
        if not row:
            return None
        fields = [column[0] for column in cursor.description]
        my_dict = {key: value for key, value in zip(fields, row)}
        count = MortgageCount(**my_dict)
        return count

    def save_payment(self, repayment_sum: float, mortgage: Mortgage, date: datetime):
        self.db_connection.execute(
            """
                INSERT INTO partial_repayment
                (
                    chat_id,
                    mortgage_id,
                    date,
                    repayment_sum
                ) VALUES (
                    :chat_id,
                    :mortgage_id,
                    :date,
                    :repayment_sum
                )
            """,
            {
                "chat_id": mortgage.chat_id,
                "mortgage_id": mortgage.id,
                "date": date,
                "repayment_sum": repayment_sum
            }
        )
        self.db_connection.commit()

    def find_payments(self, count: MortgageCount):
        cursor = self.db_connection.execute(
            """
                SELECT *
                FROM partial_repayment
                WHERE chat_id = :chat_id
                AND mortgage_count_id = :mortgage_count_id
                ORDER BY id DESC
                LIMIT 1
            """,
            {
                "chat_id": count.chat_id,
                "mortgage_count_id": count.id,
            }
        )
        row = cursor.fetchone()
        if not row:
            return None
        fields = [column[0] for column in cursor.description]
        my_dict = {key: value for key, value in zip(fields, row)}
        return my_dict


    #
    #
    # async def update_game_session(self, game_session: MortgageCount):
    #     await self.db_connection.execute(
    #         """
    #             UPDATE game_session
    #             SET phase = :phase,
    #                 json_data = :json_data,
    #                 updated_at = datetime('now')
    #             WHERE chat_id = :chat_id
    #             AND system_message_id = :system_message_id
    #         """,
    #         {
    #             "phase": game_session.phase,
    #             "json_data": json.dumps(game_session.to_dict()),
    #             "chat_id": game_session.chat_id,
    #             "system_message_id": game_session.system_message_id,
    #         }
    #     )
    #     await self.db_connection.commit()
    #
    # async def get_game_statistics(self, game: Mortgage):
    #     query = """
    #         SELECT
    #             COUNT(DISTINCT facilitator_message_id) AS estimated_game_sessions_count
    #         FROM game_session
    #         WHERE game_id = :game_id
    #         AND phase = :game_session_phase
    #     """
    #     parameters = {
    #         "game_id": game.id,
    #         "game_session_phase": MortgageCount.PHASE_RESOLUTION,
    #     }
    #     async with self.db_connection.execute(query, parameters) as cursor:
    #         row = await cursor.fetchone()
    #
    #         if not row:
    #             return None
    #
    #         return {
    #             "estimated_game_sessions_count": row["estimated_game_sessions_count"],
    #         }
