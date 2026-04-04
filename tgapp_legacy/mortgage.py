from datetime import datetime, timedelta
from dateutil import relativedelta
from dateutil.rrule import rrule, MONTHLY
import logbook


class Mortgage:
    def __init__(self, chat_id: int, name: str, main_debt_sum: float, interest: float,
                 mortgage_start: datetime, first_payment_date: datetime, last_payment_date: datetime,
                 month_payment: float = None, id: int = None):
        self.id = id
        self.chat_id = chat_id
        self.name = name
        self.main_debt_sum = main_debt_sum
        self.interest = interest
        self.mortgage_start = mortgage_start
        self.first_payment_date = first_payment_date
        self.last_payment_date = last_payment_date
        self.month_payment = month_payment

    @property
    def mortgage_start(self):
        return self.__mortgage_start

    @mortgage_start.setter
    def mortgage_start(self, value):
        try:
            self.__mortgage_start = datetime.strptime(value, '%d.%m.%Y')
        except ValueError:
            self.__mortgage_start = datetime.fromisoformat(value)

    @property
    def first_payment_date(self):
        return self.__first_payment_date

    @first_payment_date.setter
    def first_payment_date(self, value):
        try:
            self.__first_payment_date = datetime.strptime(value, '%d.%m.%Y')
        except ValueError:
            self.__first_payment_date = datetime.fromisoformat(value)

    @property
    def last_payment_date(self):
        return self.__last_payment_date

    @last_payment_date.setter
    def last_payment_date(self, value):
        try:
            self.__last_payment_date = datetime.strptime(value, '%d.%m.%Y')
        except ValueError:
            self.__last_payment_date = datetime.fromisoformat(value)

    @property
    def month_payment(self):
        return self.__month_payment

    @month_payment.setter
    def month_payment(self, month_payment):
        if month_payment:
            logbook.info(f'зашли в if')
            self.__month_payment = month_payment
        else:
            logbook.info(f'зашли в else')
            period_interest = float(self.interest) / (100 * 12)
            payment = float(self.main_debt_sum) * (period_interest / (1 - (1 + period_interest) ** -self.__payment_period_num()))
            self.__month_payment = round(payment, 2)

    def __payment_period_num(self, count_from=None):
        count_from = datetime.fromisoformat(count_from) if count_from else self.__mortgage_start
        logbook.info(f'count_from {count_from}')
        if count_from.day < self.__first_payment_date.day:
            previous_payment_date = datetime(count_from.year,
                                             count_from.month - 1,
                                             self.__first_payment_date.day)
        else:
            previous_payment_date = datetime(count_from.year,
                                             count_from.month,
                                             self.__first_payment_date.day)
        all_mortgage_time = self.__last_payment_date - previous_payment_date
        self.__first_payment_date.strftime('%d.%m.%Y')
        period_num = round((all_mortgage_time.days / 365) * 12)
        logbook.info(f'period_num = {period_num}')
        # считаем другие варианты расчетов
        s1, e1 = previous_payment_date, self.__last_payment_date + timedelta(days=1)
        s360 = (s1.year * 12 + s1.month) * 30 + s1.day
        e360 = (e1.year * 12 + e1.month) * 30 + e1.day
        dates_360 = divmod(e360 - s360, 30)[0]
        logbook.info(f'dates_360 = {dates_360}')
        rd = relativedelta.relativedelta(self.__last_payment_date, previous_payment_date)
        rd_period = rd.months + (12*rd.years)
        logbook.info(f'rd_period = {rd_period}')
        # продолжаем
        return period_num

    def payment_schedule(self):
        schedule = {dt.strftime('%d.%m.%Y'):self.__month_payment for dt in rrule(MONTHLY, bymonthday=2, dtstart=self.__first_payment_date, until=self.__last_payment_date)}
        # schedule = {dt.strftime('%d.%m.%Y'):self.__month_payment for dt in rrule(MONTHLY, bymonthday=2, dtstart=self.__first_payment_date.replace(day=1), until=self.__last_payment_date.replace(day=1))}
        return schedule
