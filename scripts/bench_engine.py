#!/usr/bin/env python3
"""
Замер стоимости ОДНОЙ ``simulate_strategy`` на контрольном договоре из роадмапа
(2 995 218.84 ₽, 7.99 %, 299 платежей, 02.05.2026 → 02.03.2051).

Число нужно двум местам: строке в CHANGELOG по итогам И1 и бюджету
производительности И8 (там движок гоняется десятками прогонов на свип).

Меряется медиана: среднее на ноутбуке с турбобустом и фоновыми процессами
показывает не стоимость кода, а везение прогона. Печатаются также min и max —
если разброс кратный, число из CHANGELOG никому не поможет.

Замеряются четыре конфигурации, потому что стоят они по-разному:

    events=[]           basis=monthly   базовый график, целые периоды
    events=[]           basis=daily     то же плюс сдвиг дат и счёт дней
    events=[lump]       basis=monthly   с разрезанием периода досрочкой
    events=[lump]       basis=daily     то же в дневной базе

Запуск::

    PYTHONPATH=web .venv/bin/python scripts/bench_engine.py
    .venv/bin/python scripts/bench_engine.py --repeat 101 --warmup 10

Скрипт обязан работать и тогда, когда движка ещё нет: печатает внятное
сообщение и возвращает код 1, а не трейсбек.
"""
import argparse
import os
import statistics
import sys
import time
from datetime import datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(REPO_ROOT, 'web'), os.path.join(REPO_ROOT, 'tests')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from app.engine import (  # noqa: E402
        BASIS_DAILY,
        BASIS_MONTHLY,
        MortgageState,
        RepaymentEvent,
        SimOptions,
        simulate_strategy,
    )
except ImportError as exc:
    print('web/app/engine.py ещё не готов — мерить нечего.', file=sys.stderr)
    print(f'  причина: {exc}', file=sys.stderr)
    print('  движок появляется на Итерации 1 роадмапа; после этого запустить снова.',
          file=sys.stderr)
    sys.exit(1)

from matrix import (  # noqa: E402
    CONTROL_FIRST,
    CONTROL_LOAN,
    CONTROL_PAYMENT,
    CONTROL_RATE,
    LONG_TERM,
    grid,
)

LUMP_AMOUNT = 500_000.0
LUMP_DATE = datetime(2026, 4, 17)   # внутри первого периода — с разрезанием начисления


def control_state():
    next_dt, last_dt, _dates = grid(CONTROL_FIRST, LONG_TERM)
    return MortgageState(
        loan_amount=CONTROL_LOAN,
        annual_rate=CONTROL_RATE,
        first_payment_date=next_dt,
        last_payment_date=last_dt,
        prev_payment_date=CONTROL_FIRST,
        contract_payment=CONTROL_PAYMENT,
    )


def configurations():
    """(подпись, события, база) — по одной строке замера на каждую."""
    return (
        ('events=[]        ', [], BASIS_MONTHLY),
        ('events=[]        ', [], BASIS_DAILY),
        ('events=[lump]    ', [RepaymentEvent(amount=LUMP_AMOUNT, at=LUMP_DATE,
                                              mode='reduce_term')], BASIS_MONTHLY),
        ('events=[lump]    ', [RepaymentEvent(amount=LUMP_AMOUNT, at=LUMP_DATE,
                                              mode='reduce_term')], BASIS_DAILY),
    )


def measure(events, basis, repeat, warmup):
    """Возвращает (список времён в мс, результат последнего прогона)."""
    state = control_state()
    opts = SimOptions(basis=basis)
    result = None
    for _ in range(warmup):
        result = simulate_strategy(state, events, opts)

    samples = []
    for _ in range(repeat):
        started = time.perf_counter()
        result = simulate_strategy(state, events, opts)
        samples.append((time.perf_counter() - started) * 1000.0)
    return samples, result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Стоимость одной simulate_strategy на 299 периодах, медиана из N прогонов.')
    parser.add_argument('--repeat', type=int, default=25,
                        help='число замеряемых прогонов каждой конфигурации (по умолчанию 25)')
    parser.add_argument('--warmup', type=int, default=3,
                        help='прогревочных прогонов перед замером (по умолчанию 3)')
    args = parser.parse_args(argv)

    if args.repeat < 1:
        parser.error('--repeat должен быть не меньше 1')
    if args.warmup < 0:
        parser.error('--warmup не может быть отрицательным')

    loan = f'{CONTROL_LOAN:,.2f}'.replace(',', ' ')
    payment = f'{CONTROL_PAYMENT:,.2f}'.replace(',', ' ')
    print(f'Контрольный договор: {loan} ₽ @ {CONTROL_RATE} %, '
          f'{LONG_TERM} платежей, платёж {payment} ₽')
    print(f'Прогонов: {args.repeat} (+{args.warmup} прогревочных), '
          f'python {sys.version.split()[0]}')
    print()
    print(f'{"конфигурация":<18} {"база":<8} {"медиана, мс":>13} {"min, мс":>10} '
          f'{"max, мс":>10}   итог')
    print('-' * 92)

    medians = {}
    for label, events, basis in configurations():
        samples, result = measure(events, basis, args.repeat, args.warmup)
        median = statistics.median(samples)
        medians[(label.strip(), basis)] = median
        tail = (f'проценты {result.total_interest:,.2f} ₽, '
                f'строк {len(result.schedule)}').replace(',', ' ')
        print(f'{label:<18} {basis:<8} {median:>13.3f} {min(samples):>10.3f} '
              f'{max(samples):>10.3f}   {tail}')

    print()
    baseline = medians[('events=[]', BASIS_MONTHLY)]
    heaviest = max(medians.values())
    print('Строка для CHANGELOG: одна simulate_strategy на 299 периодах — '
          f'{baseline:.2f} мс (медиана из {args.repeat}, basis=monthly, без событий); '
          f'самая дорогая конфигурация — {heaviest:.2f} мс.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
