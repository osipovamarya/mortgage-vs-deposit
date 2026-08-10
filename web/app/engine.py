"""
Событийный движок графика платежей.

Итерация 1 роадмапа: вместо двух независимых реализаций досрочки в
``calculator.py`` появляется один помесячный цикл, а ``build_amortization()``
и ``simulate_lump_repayment()`` становятся его тонкими обёртками. Итерация 3
добавила ежемесячные события (``kind='recurring'``), и обёрткой стал ещё и
снежный ком ``calc_repayment_schedule()`` — независимых движков не осталось.

Главная идея — **начисление процентов списком сегментов**, а не тремя
спецслучаями. Период между двумя плановыми платежами разрезается событиями на
отрезки ``(начало, конец, остаток)``; проценты за период считаются по этому
списку. Отсюда структурная защита инварианта коммита ``3ca4b3e``: событие
только закрывает текущий сегмент начисления и открывает следующий — положить
процент в строку досрочки физически некуда. Единственное исключение сделано
явно и по требованию пользователя (режим ``interest_first``, Итерация 2): там
строка досрочки получает проценты, а курсор сегмента сдвигается вперёд — это и
есть защита от двойного начисления.

**База начисления фиксируется один раз** на всё сравнение (решение 4 роадмапа):

* ``BASIS_MONTHLY`` — период стоит ровно ``balance * annual/12``, сколько бы
  дней в нём ни было. При дроблении событием ЭТА ЖЕ сумма распределяется между
  отрезками пропорционально дням;
* ``BASIS_DAILY``  — ``balance * annual/365 * фактические дни``.

Смешивать базы запрещено: дневной остаток, умноженный на месячную ставку, — это
ровно та ошибка, из-за которой в роадмапе появилось неверное число 8 366,21.

Модуль не импортирует ``calculator`` (обратной зависимости нет) и ничего не
знает ни про Flask, ни про БД.
"""
from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from dateutil.relativedelta import relativedelta
from dateutil.rrule import rrule, MONTHLY

# --- База начисления процентов ------------------------------------------------
BASIS_MONTHLY = 'monthly'   # проценты = balance * rate/12 за целый период
BASIS_DAILY = 'daily'       # проценты = balance * rate/365 * дни

# --- Правило распределения досрочного платежа (W5, Итерация 2) ----------------
ALLOC_PRINCIPAL_ONLY = 'principal_only'   # вся сумма в тело — ИНВАРИАНТ, дефолт
ALLOC_INTEREST_FIRST = 'interest_first'   # сначала проценты периода, остаток в тело

# --- Вид строки графика -------------------------------------------------------
ROW_ANNUITY = 'annuity'
ROW_EARLY = 'early'

# --- Режим досрочки (живёт НА СОБЫТИИ) ---------------------------------------
MODE_PAYMENT = 'reduce_payment'   # тот же срок, аннуитет пересчитывается
MODE_TERM = 'reduce_term'         # тот же платёж, кредит закрывается раньше

# --- Вид события (Итерация 3) -------------------------------------------------
KIND_LUMP = 'lump'                # разовая досрочка
KIND_RECURRING = 'recurring'      # ежемесячная доплата (снежный ком)

# --- Как читать `amount` события ----------------------------------------------
AMOUNT_FIXED = 'fixed'    # сумма как есть
AMOUNT_BUDGET = 'budget'  # «всего готов платить в месяц»: досрочка = amount − платёж

# --- Статус сценария ----------------------------------------------------------
STATUS_OK = 'ok'
STATUS_NOT_APPLICABLE = 'not_applicable'

_CENT = Decimal('0.01')
_ZERO = Decimal('0')

# Остаток, при котором кредит считается закрытым. Значение унаследовано от
# calculator.py и менять его нельзя, не переснимая голдены.
CLOSED_BALANCE = Decimal('0.01')

# Порядок ключей строки графика. Фиксирован: snapshot_golden.ROW_COLUMNS
# упаковывает строку в массив именно в этом порядке.
ROW_KEYS = ('payment_num', 'date', 'payment', 'principal', 'interest',
            'balance', 'early', 'row_kind', 'early_interest')


# ---------------------------------------------------------------------------
# Примитивы
# ---------------------------------------------------------------------------

def _d(x):
    """Число в Decimal через str: 0.1 остаётся 0.1, а не 0.1000000000000000055."""
    return Decimal(str(x))


def _r2(x):
    """Округление до копейки, банковское округление не используется."""
    return x.quantize(_CENT, rounding=ROUND_HALF_UP)


def _next_business_day(date):
    """Суббота → понедельник, воскресенье → понедельник. Календаря праздников нет."""
    wd = date.weekday()
    if wd == 5:
        return date + timedelta(days=2)
    if wd == 6:
        return date + timedelta(days=1)
    return date


def _parse_date(value):
    """Строку DD.MM.YYYY или ISO — в datetime. None и datetime проходят насквозь."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.strptime(value, '%d.%m.%Y')
    except ValueError:
        return datetime.fromisoformat(value)


def basis_for(adjust_business_days):
    """
    База начисления по флагу переноса на рабочие дни.

    Решение 4 роадмапа: база берётся ТОЛЬКО отсюда и фиксируется на всё
    сравнение. Никогда — из наличия `monthly_extra_day`.
    """
    return BASIS_DAILY if adjust_business_days else BASIS_MONTHLY


# ---------------------------------------------------------------------------
# Контракт
# ---------------------------------------------------------------------------

@dataclass
class MortgageState:
    """Текущее состояние ипотеки: остаток, ставка, границы сетки платежей."""

    loan_amount: float
    annual_rate: float
    first_payment_date: datetime          # первая ПРЕДСТОЯЩАЯ дата платежа
    last_payment_date: datetime           # последняя дата по договору
    prev_payment_date: datetime = None    # якорь начисления; None → first − 1 месяц
    contract_payment: float = None        # договорной платёж; None → считаем аннуитет


@dataclass
class RepaymentEvent:
    """
    Досрочный платёж: разовый (`lump`) или ежемесячный (`recurring`).

    `mode` живёт именно на событии — это ключ к смешанным стратегиям (W2).
    `allocation=None` означает «взять из SimOptions».

    Поля `lump`
        `at` — дата платежа; None → применить сразу, до первого планового платежа.

    Поля `recurring` (Итерация 3, снежный ком)
        `start_date`    — с какой даты платежа включается доплата (None → с первой);
        `end_date`      — по какую включительно (None → до конца графика);
        `day_of_month`  — день доплаты; None → в саму дату планового платежа.
                          День прижимается к длине месяца (31 → 30/28). Если он
                          ПОЗЖЕ даты платежа, доплата уходит внутрь следующего
                          периода и период начисления делится; если не позже —
                          платится в дату платежа, сразу после аннуитета;
        `period_months` — 1 = каждый месяц, 2 = через месяц (смешанный ком, W2);
        `phase`         — сдвиг фазы для `period_months > 1`.

    `amount_kind='budget'` означает «всего готов платить в месяц»: сумма события
    резолвится В МОМЕНТ ПРИМЕНЕНИЯ как `amount − уплаченный в этом периоде
    аннуитет`, поэтому в `reduce_payment` доплата растёт по мере падения
    аннуитета сама собой, а в `reduce_term` остаётся постоянной — без единой
    ветки `if mode`.
    """

    amount: float
    at: datetime = None                   # None → применить сразу, до первого платежа
    kind: str = KIND_LUMP                 # 'lump' | 'recurring'
    mode: str = MODE_PAYMENT
    allocation: str = None
    amount_kind: str = AMOUNT_FIXED       # 'fixed' | 'budget'
    start_date: datetime = None           # recurring: первая дата платежа с доплатой
    end_date: datetime = None             # recurring: последняя дата платежа с доплатой
    day_of_month: int = None              # recurring: день доплаты внутри месяца
    period_months: int = 1                # recurring: раз в сколько месяцев
    phase: int = 0                        # recurring: сдвиг фазы


@dataclass
class SimOptions:
    """Опции симуляции, общие для всех сценариев одного сравнения."""

    basis: str = BASIS_MONTHLY
    allocation: str = ALLOC_PRINCIPAL_ONLY


@dataclass
class StrategyResult:
    """Результат одной симуляции."""

    schedule: list                        # строки графика
    total_interest: float
    monthly_payment: float                # платёж после последнего применённого события
    annuity_months: int                   # только плановые платежи, строки досрочки не в счёт
    months_to_payoff: int
    dates: list                           # ЕДИНСТВЕННАЯ сетка дат платежей
    lump_unused: float = 0.0              # справочно; в own_cost не входит
    status: str = STATUS_OK
    carried_interest: float = 0.0         # непредъявленный остаток процентов, обязан быть 0


@dataclass
class _Pending:
    """Материализованное событие: дата применения плюс отметка «применено»."""

    event: RepaymentEvent
    at: datetime
    applied: bool = False


# ---------------------------------------------------------------------------
# Сетка дат — единственный источник истины
# ---------------------------------------------------------------------------

def payment_grid(state, basis):
    """
    Сетка дат платежей и якорь начисления для первого периода.

    Единственный источник дат: и график, и дата вливания вклада в
    `run_comparison` обязаны брать даты отсюда, иначе появляются две сетки —
    сырая из `rrule` и сдвинутая на рабочий день (замер роадмапа: d* 7,8771
    против 8,4059 при T=1).

    Возвращает (dates, prev_date).
    """
    first = _parse_date(state.first_payment_date)
    last = _parse_date(state.last_payment_date)
    prev = _parse_date(state.prev_payment_date)
    if prev is None:
        prev = first - relativedelta(months=1)

    scheduled = list(rrule(MONTHLY, dtstart=first, until=last))
    if basis == BASIS_DAILY:
        return [_next_business_day(d) for d in scheduled], _next_business_day(prev)
    return scheduled, prev


# ---------------------------------------------------------------------------
# Начисление процентов
# ---------------------------------------------------------------------------

def _accrue(segments, basis, month_span, monthly_rate, daily_rate):
    """
    Проценты за период по списку сегментов.

    `segments` — список `(начало, конец, остаток на отрезке)`, покрывающий период
    целиком. `month_span` — границы периода `(якорь, дата планового платежа)`.

    ``basis='daily'``   — annual/365 × фактические дни каждого отрезка.
    ``basis='monthly'`` — период стоит ровно `bal · monthly_rate`; при дроблении
    ЭТА ЖЕ сумма распределяется между отрезками пропорционально дням.

    Свойство, ради которого функция написана: один сегмент на весь период при
    `basis='monthly'` даёт ровно `bal · monthly_rate`. Значит, включение любого
    режима без изменения денежных потоков даёт нулевую дельту процентов.
    """
    if basis == BASIS_DAILY:
        total = _ZERO
        for start, end, balance in segments:
            days = (end - start).days
            if days:
                total += balance * daily_rate * _d(days)
        return _r2(total)

    total_days = (month_span[1] - month_span[0]).days
    if total_days <= 0:
        return _ZERO
    weighted = _ZERO
    for start, end, balance in segments:
        weighted += balance * _d((end - start).days)
    return _r2(monthly_rate * (weighted / _d(total_days)))


# ---------------------------------------------------------------------------
# Материализация событий по сетке
# ---------------------------------------------------------------------------

def _resolve_amount(event, annuity_paid):
    """
    Сумма события в момент применения.

    `amount_kind='budget'` — «всего готов платить в месяц»: доплата равна
    остатку бюджета сверх уже уплаченного в этом периоде аннуитета. Резолвить
    заранее нельзя: в `reduce_payment` аннуитет падает от события к событию, и
    заранее посчитанная доплата отстала бы от реальности.

    Вычитается именно **фактически уплаченный** аннуитет, а не платёж «в силе»:
    иначе в месяце разовой досрочки, которая пересчитывает аннуитет между
    платежом и доплатой, месячный расход перестал бы равняться бюджету.
    """
    amount = _d(event.amount or 0)
    if event.amount_kind == AMOUNT_BUDGET:
        return max(amount - annuity_paid, _ZERO)
    return amount


def _recurring_dates(event, dates):
    """
    Даты ежемесячной доплаты — ПО СЕТКЕ платежей, а не по календарю.

    Раскрытие по сетке обязательно: иначе перенос платежа на рабочий день
    уводил бы доплату от аннуитета и появлялся бы дрейф.

    Для каждой подходящей даты платежа:

    * `day_of_month=None` → доплата в саму дату платежа (её обработает ветка
      «событие в дату платежа», то есть строго после аннуитета);
    * иначе день прижимается к длине месяца (31 → 30/28). Если получившаяся дата
      ПОЗЖЕ даты платежа — доплата попадает внутрь следующего периода и делит его
      начисление; если не позже — платится в дату платежа. Второе повторяет
      прежнее поведение снежка (`monthly_extra_day` работал только «позже дня
      аннуитета», см. `calc_repayment_schedule`).
    """
    start = _parse_date(event.start_date)
    end = _parse_date(event.end_date)
    period = max(int(event.period_months or 1), 1)
    phase = int(event.phase or 0)
    day = int(event.day_of_month) if event.day_of_month else None

    window = [date for date in dates
              if (start is None or date >= start) and (end is None or date <= end)]

    out = []
    for step, date in enumerate(window):
        if step < phase or (step - phase) % period:
            continue
        if day is None:
            out.append(date)
            continue
        candidate = date.replace(day=min(day, monthrange(date.year, date.month)[1]))
        out.append(candidate if candidate > date else date)
    return out


def _materialize(events, dates, anchor):
    """
    Разложить события по сетке платежей.

    Возвращает (pre, inside, on_date, late, all_pending):

    * `pre`     — без даты или не позже якоря: применяются сразу, накопленного
                  периода ещё нет (days = 0);
    * `inside`  — по одному списку на период: событие строго внутри `(начало, дата)`;
    * `on_date` — по одному списку на период: событие ровно в дату планового платежа;
    * `late`    — дата за последним платежом: событие не состоится;
    * `all_pending` — те же объекты одним списком, для дренажа неприменённых.

    Ежемесячное событие раскрывается здесь же в список одиночных применений, и
    дальше цикл симуляции ничего не знает про его периодичность.

    Границы периодов статичны, потому что сетка дат фиксирована до симуляции.
    """
    inside = [[] for _ in dates]
    on_date = [[] for _ in dates]
    pre = []
    late = []
    all_pending = []

    expanded = []
    for index, event in enumerate(events):
        if event.kind == KIND_RECURRING:
            for step, at in enumerate(_recurring_dates(event, dates)):
                expanded.append((at, (index, step + 1), event))
        else:
            expanded.append((_parse_date(event.at), (index, 0), event))

    ordered = sorted(expanded, key=lambda item: (item[0] or anchor, item[1]))

    for at, _key, event in ordered:
        pending = _Pending(event=event, at=at if at is not None else anchor)
        all_pending.append(pending)

        if at is None or at <= anchor:
            pre.append(pending)
            continue

        placed = False
        start = anchor
        for i, date in enumerate(dates):
            if at == date:
                on_date[i].append(pending)
                placed = True
                break
            if start < at < date:
                inside[i].append(pending)
                placed = True
                break
            start = date
        if not placed:
            late.append(pending)

    return pre, inside, on_date, late, all_pending


# ---------------------------------------------------------------------------
# Симуляция
# ---------------------------------------------------------------------------

def simulate_strategy(state, events, opts=None):
    """
    Прогнать стратегию (список досрочных событий) по графику ипотеки.

    Возвращает `StrategyResult`. Пустой список событий даёт обычный аннуитетный
    график — именно так работает обёртка `build_amortization()`.
    """
    opts = opts or SimOptions()
    basis = opts.basis or BASIS_MONTHLY

    annual_rate = _d(state.annual_rate)
    monthly_rate = annual_rate / _d(100) / _d(12)
    daily_rate = annual_rate / _d(100) / _d(365)

    dates, anchor = payment_grid(state, basis)
    n = len(dates)

    balance = _d(state.loan_amount)
    remaining_term = n
    lump_unused = _ZERO
    carried_interest = _ZERO
    status = STATUS_OK

    schedule = []
    total_interest = _ZERO
    annuity_months = 0

    def _annuity(bal, periods):
        """Аннуитет, закрывающий `bal` за `periods` месяцев."""
        if periods <= 0 or bal <= _ZERO:
            return _ZERO
        factor = (1 + monthly_rate) ** periods
        return _r2(bal * monthly_rate * factor / (factor - 1))

    payment_in_force = (_d(state.contract_payment) if state.contract_payment
                        else _annuity(balance, n))
    # Аннуитет, фактически уплаченный в текущем периоде: от него отсчитывается
    # доплата события с `amount_kind='budget'`. До первого планового платежа
    # равен платежу «в силе».
    annuity_paid = payment_in_force

    def _emit(date, row_kind, payment, principal, interest, early, early_interest, bal_after):
        """Строка графика. Порядок ключей — ROW_KEYS, менять нельзя."""
        schedule.append({
            'payment_num': 0,                       # проставляется сквозной нумерацией в конце
            'date': date.strftime('%d.%m.%Y'),
            'payment': float(payment),
            'principal': float(principal),
            'interest': float(interest),
            'balance': float(_r2(bal_after)),
            'early': float(early),
            'row_kind': row_kind,
            'early_interest': float(early_interest),
        })

    def _apply_mode(event):
        """
        Как событие меняет платёж и срок.

        `reduce_payment` — аннуитет пересчитывается на остаток срока;
        `reduce_term`    — платёж заморожен, срок сокращается сам собой.
        """
        nonlocal payment_in_force
        if event.mode == MODE_TERM:
            return
        payment_in_force = _annuity(balance, remaining_term)

    pre, inside, on_date, late, all_pending = _materialize(events, dates, anchor)

    # ── События до первого предстоящего платежа: накопленного периода ещё нет,
    #    поэтому days = 0 и режимы распределения тождественны ────────────────
    for pending in pre:
        amount = _resolve_amount(pending.event, annuity_paid)
        pending.applied = True
        if amount <= _ZERO:
            continue
        applied = min(amount, balance)
        if pending.event.kind == KIND_LUMP:
            lump_unused += amount - applied
        balance -= applied
        _emit(pending.at, ROW_EARLY, applied, applied, _ZERO, applied, _ZERO, balance)
        _apply_mode(pending.event)

    for i, pay_date in enumerate(dates):
        if balance <= CLOSED_BALANCE:
            break

        # ── (1) события СТРОГО ВНУТРИ периода (anchor, pay_date) ────────────
        segments = []
        cursor = anchor
        for pending in inside[i]:
            event = pending.event
            at = pending.at
            allocation = event.allocation or opts.allocation
            amount = _resolve_amount(event, annuity_paid)
            pending.applied = True
            if amount <= _ZERO:
                continue

            if allocation == ALLOC_INTEREST_FIRST:
                # Проценты, накопленные с прошлого платежа по дату досрочки,
                # гасятся из самой досрочки. Курсор сегмента сдвигается на ev.at,
                # а уже оплаченные сегменты выбрасываются — это единственная
                # защита от двойного начисления в ближайшем аннуитете.
                accrued = _accrue(segments + [(cursor, at, balance)], basis,
                                  (anchor, pay_date), monthly_rate, daily_rate)
                cash = min(amount, balance + accrued)
                paid_interest = min(accrued, cash)
                paid_principal = _r2(cash - paid_interest)
                balance -= paid_principal
                total_interest += paid_interest
                # Досрочка меньше начисленных процентов: тело не уменьшаем,
                # непокрытый остаток предъявляем ближайшим аннуитетом.
                carried_interest += accrued - paid_interest
                if event.kind == KIND_LUMP:
                    lump_unused += amount - cash
                segments = []
                _emit(at, ROW_EARLY, cash, paid_principal, paid_interest,
                      paid_principal, paid_interest, balance)
            else:
                # Проценты отрезка считаются на ДОсобытийном остатке и будут
                # предъявлены ближайшим аннуитетом — инвариант 3ca4b3e.
                segments.append((cursor, at, balance))
                applied = min(amount, balance)
                if event.kind == KIND_LUMP:
                    lump_unused += amount - applied
                balance -= applied
                _emit(at, ROW_EARLY, applied, applied, _ZERO, applied, _ZERO, balance)

            cursor = at
            _apply_mode(event)
            if balance <= CLOSED_BALANCE:
                break

        segments.append((cursor, pay_date, balance))

        # ── (2) проценты периода — СЧИТАЮТСЯ ВСЕГДА, до проверки закрытия ───
        #     Раньше проверка стояла до начисления и выбрасывала проценты от
        #     якоря до даты досрочки вместе с carried_interest.
        interest = _accrue(segments, basis, (anchor, pay_date),
                           monthly_rate, daily_rate) + carried_interest
        carried_interest = _ZERO

        if balance <= CLOSED_BALANCE:
            # Кредит закрыт досрочкой внутри периода: начисленные проценты всё
            # равно предъявляются — закрывающей строкой без тела.
            if interest > _ZERO:
                total_interest += interest
                _emit(pay_date, ROW_ANNUITY, interest, _ZERO, interest, _ZERO, _ZERO, balance)
                annuity_months += 1
            anchor = pay_date
            break

        # ── (3) регулярный аннуитет ─────────────────────────────────────────
        if i == n - 1 or payment_in_force >= balance + interest:
            principal = _r2(balance)
            payment = _r2(principal + interest)
        else:
            payment = payment_in_force
            # Тело может выйти отрицательным: если проценты периода больше
            # платежа, остаток растёт. Зажимать его в ноль нельзя — строка
            # перестала бы сходиться (principal + interest != payment), а
            # неоплаченные проценты всё равно попадали бы в total_interest.
            principal = _r2(payment - interest)

        balance = max(balance - principal, _ZERO)
        total_interest += interest
        annuity_months += 1
        remaining_term = max(remaining_term - 1, 0)
        annuity_paid = payment          # от него отсчитывается доплата из бюджета
        _emit(pay_date, ROW_ANNUITY, payment, principal, interest, _ZERO, _ZERO, balance)
        anchor = pay_date

        if balance <= CLOSED_BALANCE:
            break

        # ── (4) события В ДАТУ платежа — строго ПОСЛЕ аннуитета ─────────────
        #     Проценты периода уже закрыты аннуитетом, поэтому здесь
        #     interest_first тождествен principal_only.
        for pending in on_date[i]:
            amount = _resolve_amount(pending.event, annuity_paid)
            pending.applied = True
            if amount <= _ZERO:
                continue
            applied = min(amount, balance)
            if pending.event.kind == KIND_LUMP:
                lump_unused += amount - applied
            balance -= applied
            _emit(pay_date, ROW_EARLY, applied, applied, _ZERO, applied, _ZERO, balance)
            _apply_mode(pending.event)
            if balance <= CLOSED_BALANCE:
                break

    # ── Дренаж: события, до которых цикл не дошёл ───────────────────────────
    #    Молча терять их нельзя — сценарий с несостоявшимся событием обязан
    #    быть видимым, иначе «ничего не сделал» выигрывает у реального погашения.
    #    Ежемесячные доплаты в дренаж НЕ идут: их «неприменение» означает, что
    #    кредит закрылся раньше конца графика, и записывать неистраченный бюджет
    #    в `lump_unused` («не понадобилось +X ₽» разовой суммы) было бы враньём.
    for pending in all_pending:
        if pending.applied or pending.event.kind == KIND_RECURRING:
            continue
        amount = _resolve_amount(pending.event, payment_in_force)
        if amount > _ZERO:
            lump_unused += amount
            status = STATUS_NOT_APPLICABLE

    for index, row in enumerate(schedule):
        row['payment_num'] = index + 1

    return StrategyResult(
        schedule=schedule,
        total_interest=float(_r2(total_interest)),
        monthly_payment=float(payment_in_force),
        annuity_months=annuity_months,
        months_to_payoff=annuity_months,
        dates=dates,
        lump_unused=float(_r2(lump_unused)),
        status=status,
        carried_interest=float(_r2(carried_interest)),
    )
