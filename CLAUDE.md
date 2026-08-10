# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Project: Mortgage vs Deposit Efficiency Calculator

A locally-run web application that answers one question: **is it more profitable to put your savings on a deposit account, or use them for a partial mortgage repayment?**

The app takes one form (mortgage + repayment strategy + deposit terms), then shows the strategies side by side with a clear winner.

Working language of the repo is Russian: docstrings, comments, UI strings, `CHANGELOG.md` and `ROADMAP.md` are written in Russian. This file stays in English — it is the agent-facing contract.

---

## Repository Layout

```
mortgage-vs-deposit/
├── tgapp_legacy/                 # Original Telegram bot (legacy, do not modify)
│   ├── bot.py
│   ├── mortgage.py
│   ├── mortgage_registry.py
│   ├── mortgage_count.py
│   ├── telegram_user.py
│   ├── Dockerfile
│   └── requirements.txt
├── web/                          # Flask web application (active)
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py               # Flask app factory + entry point
│   │   ├── database.py           # SQLite connection, schema, additive migrations
│   │   ├── engine.py             # Event-driven payment-schedule engine (the only loop)
│   │   ├── calculator.py         # Thin wrappers over the engine + deposit + comparison
│   │   └── routes/
│   │       ├── __init__.py
│   │       ├── mortgage.py       # /api/mortgage — mortgage + repayment_strategy
│   │       ├── deposit.py        # /api/deposit
│   │       └── comparison.py     # /api/comparison
│   ├── static/
│   │   ├── css/style.css
│   │   ├── js/app.js             # Form logic + result rendering
│   │   └── favicon.ico, favicon-32.png
│   └── templates/index.html      # Single-page app shell
├── tests/                        # stdlib unittest (no pytest, never add one)
│   ├── matrix.py                 # Single source of input cases for snapshot AND tests
│   ├── test_golden.py            # Schedules vs committed golden files
│   ├── test_invariant.py         # Unconditional invariants over the whole matrix
│   ├── test_engine.py            # engine.py contracts
│   ├── test_snowball.py          # calc_repayment_schedule / simulate_snowball
│   ├── test_cash_parity.py       # cash-flow parity contract (decision 7)
│   ├── test_early_repayment_allocation.py   # principal_only vs interest_first (W5)
│   ├── test_migration.py         # schema migrations keep saved history
│   ├── test_regressions.py       # one class per code-review finding, with the input it broke on
│   └── golden/
│       ├── build_amortization.json          #    92 cases
│       ├── simulate_lump_repayment.json     #  2964 cases
│       ├── calc_repayment_schedule.json     #  2205 cases
│       ├── known_bugs.json                  # registry of accepted defects (currently 0)
│       └── comparison_before_v4.json        # snapshot of 5 saved comparisons (for И5)
├── scripts/
│   ├── snapshot_golden.py        # take / check golden snapshots
│   ├── snapshot_comparison.py    # read-only snapshot of saved comparisons via the API
│   └── bench_engine.py           # cost of one simulate_strategy run
├── db/
│   ├── morst_bot.db              # Legacy Telegram bot database
│   ├── morst_bot_old.db          # Legacy backup
│   └── mortgage_web.db           # Web app database — LIVE USER HISTORY, never edit
├── Dockerfile                    # Web app image (build context: repo root)
├── docker-compose.yml
├── requirements.txt              # Flask, python-dateutil
├── run.sh                        # legacy bot script, hard-coded foreign path — do not run
├── README.md                     # human-facing: how to run, how to test
├── ROADMAP.md                    # plan: iterations И0–И9, decisions, open questions
├── CHANGELOG.md                  # what changed and why, with measured numbers
└── CLAUDE.md
```

---

## Running the Web App

**With Docker (recommended):**
```bash
cd /path/to/mortgage-vs-deposit
docker compose up --build
```
Then open http://localhost:5000

**Directly (development):**
```bash
# Create a virtualenv once
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run (from repo root)
cd web
DB_PATH=../db/mortgage_web.db PYTHONPATH=.. flask --app app/main.py run
```

**Environment variables:**
- `DB_PATH` — path to SQLite database (default: `../db/mortgage_web.db` relative to `web/`)
- `FLASK_DEBUG` — set to `1` for auto-reload during development

**Important:** always run Flask from the `web/` directory so Python's import resolution finds `web/app/` as the `app` package.

**`db/mortgage_web.db` holds the user's real history.** Never start the app against it while
experimenting, never write to it from a script. Copy it to a scratch directory and point
`DB_PATH` at the copy. `scripts/snapshot_comparison.py` is the model to follow: it opens the
original as `file:...?mode=ro` and serves the app from a copy.

---

## Tests and the Golden Snapshot

There is no pytest in the environment and none is to be added — stdlib `unittest` only.

```bash
# whole suite (~25 s; 137 tests at the time of writing, the set keeps growing)
cd /path/to/mortgage-vs-deposit
PYTHONPATH=web .venv/bin/python -m unittest discover -s tests

# calculations vs the committed golden snapshot; writes nothing, exit code 1 on any drift
PYTHONPATH=web:tests .venv/bin/python scripts/snapshot_golden.py --check

# what is snapshotted and how big it is
PYTHONPATH=web:tests .venv/bin/python scripts/snapshot_golden.py --list
```

The snapshot stores **full schedules row by row** (`ROW_KEYS` order), not just totals, over the
matrix in `tests/matrix.py`: balance × rate × lump sum × lump date × mode × business-day shift ×
monthly budget × extra-payment day × day-of-month of the first payment.

**Re-snapshotting rule.** `--accept` takes exactly one function and requires `--reason`; the
reason is appended to `CHANGELOG.md` automatically.

```bash
PYTHONPATH=web:tests .venv/bin/python scripts/snapshot_golden.py \
    --accept simulate_lump_repayment --reason "why the numbers changed"
```

There is deliberately **no command that rewrites every golden at once**: "run the script and
commit" must stay an impossible move, otherwise the snapshot proves nothing. `--init` only works
when no golden exists at all.

`tests/golden/known_bugs.json` is the registry of defects recorded as-is so that they neither
break the build nor get lost. It is currently **empty and must stay empty** — the underpayment
bug it used to hold (262 cases) was fixed in И3.

Definition of done for any change here: the whole suite green **and** `--check` reporting 0 diffs,
or an explicit `--accept` with a reason.

---

## Architecture

**Backend:** Python + Flask (intentionally simple — no async, no ORM, plain sqlite3).

**Frontend:** Single HTML page with vanilla JS. No build step, no framework. All inputs on one
form; results section shown/hidden by JS. Charts via Chart.js (CDN).

**Database:** SQLite. Single-user, no authentication. All records belong to one local user.

**Calculation layer:**

| Module | Role |
|---|---|
| `web/app/engine.py` | `simulate_strategy(state, events, opts)` — **the only** month-by-month loop in the project |
| `web/app/calculator.py` | thin wrappers over the engine, deposit math, `run_comparison()` |
| `tgapp_legacy/mortgage.py` | legacy annuity code, **no longer imported** by the web app |

---

## User Flow

### Step 1 — one form
**Mortgage** (current state, not the original loan):
- Remaining principal balance
- Annual interest rate (%)
- Amount and date of the **last payment already made** (`monthly_payment`, `first_payment_date`) — the schedule starts one month later
- Contract end date (`last_payment_date`)
- «Переносить платёж на следующий рабочий день» (`adjust_business_days`) — also selects the interest basis, see below

**Repayment strategy:**
- `repayment_mode`: reduce payment / reduce term
- `early_repayment_allocation`: principal only / interest first
- `lump_sum` + `lump_sum_date` — the savings, and when they go into the mortgage
- `monthly_budget` + `monthly_start_date` + `monthly_extra_day` — snowball ("what I am ready to pay per month in total")

**Deposit:** annual rate, term in months, capitalization yes/no. The **amount is not asked** —
the money on the deposit is the same `lump_sum`, otherwise the strategies would not be comparable.

### Step 2 — results
Cards (a card is hidden when its scenario is not applicable to the input):

| Card | What it shows |
|---|---|
| **Параметры расчёта** | echo of the inputs plus the comparison family (`baseline_kind`) and why it was chosen |
| **Вклад → потом погасить** | deposit income over the term, then the whole `deposit_final` goes into the mortgage |
| **Досрочно погасить → уменьшить платёж** | new monthly payment, interest saved |
| **Досрочно погасить → уменьшить срок** | new payoff date, months saved, interest saved |
| **Снежный ком** | monthly extra payments, payoff month, interest saved (only when `monthly_budget` is set) |
| **Вклад вместо досрочек** | the same money accumulated on a deposit instead, and the month it overtakes the debt |

A highlighted banner shows the winner. Below: two Chart.js charts (remaining balance per scenario,
gain per scenario) and a per-scenario payment-schedule table.

---

## Database Schema

File: `db/mortgage_web.db`. This is the actual schema, taken from the live database.

```sql
CREATE TABLE mortgage (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT NOT NULL DEFAULT 'Моя ипотека',
    loan_amount          REAL NOT NULL,      -- remaining principal balance
    annual_rate          REAL NOT NULL,      -- interest rate % per year
    first_payment_date   TEXT NOT NULL,      -- ISO date of the LAST payment already made
    last_payment_date    TEXT NOT NULL,      -- ISO date, contract end
    monthly_payment      REAL,               -- contract payment, entered by the user
    adjust_business_days INTEGER DEFAULT 0,  -- 1 → shift to next business day AND daily basis
    created_at           DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE repayment_strategy (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    mortgage_id         INTEGER NOT NULL REFERENCES mortgage(id),
    lump_sum            REAL,     -- one-off early repayment amount
    lump_sum_date       TEXT,     -- ISO date of the one-off repayment
    monthly_budget      REAL,     -- total monthly budget (snowball)
    monthly_start_date  TEXT,     -- ISO date the monthly extra starts
    monthly_extra_day   INTEGER,  -- day of month for the extra payment (e.g. payday)
    repayment_mode      TEXT NOT NULL DEFAULT 'reduce_payment',  -- 'reduce_payment' | 'reduce_term'
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
    early_repayment_allocation TEXT NOT NULL DEFAULT 'principal_only'
                                            -- 'principal_only' | 'interest_first'
);

CREATE TABLE deposit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL DEFAULT 'Мой вклад',
    annual_rate     REAL NOT NULL,
    term_months     INTEGER NOT NULL,
    capitalization  INTEGER DEFAULT 1,      -- 1 = compound monthly, 0 = simple
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
-- No `amount` column: the deposit amount is repayment_strategy.lump_sum.

CREATE TABLE comparison (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    repayment_strategy_id           INTEGER NOT NULL REFERENCES repayment_strategy(id),
    deposit_id                      INTEGER NOT NULL REFERENCES deposit(id),

    -- Strategy A: deposit the lump sum for T months, then repay
    deposit_income                  REAL,
    deposit_final                   REAL,
    deposit_net_saving              REAL,
    deposit_new_monthly             REAL,

    -- Strategy B1: lump-sum early repayment → reduce payment
    reduce_payment_new_monthly      REAL,
    reduce_payment_interest_saved   REAL,

    -- Strategy B2: lump-sum early repayment → reduce term
    reduce_term_interest_saved      REAL,
    reduce_term_months_saved        INTEGER,
    reduce_term_months_to_payoff    INTEGER,

    -- Strategy C: snowball
    snowball_total_interest         REAL,
    snowball_interest_saved         REAL,
    snowball_months_to_payoff       INTEGER,
    snowball_deposit_income         REAL,
    snowball_deposit_final          REAL,

    baseline_total_interest         REAL,
    winner                          TEXT,   -- 'deposit' | 'reduce_payment' | 'reduce_term' | 'snowball'
    created_at                      DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

Dates are stored as ISO strings (`YYYY-MM-DD`); user-facing input/display uses `DD.MM.YYYY`.
Schedules are **never** stored — they are recomputed and returned by the API.

**Migrations.** `database.py` keeps two mechanisms: `_schema_is_current()` decides whether the
whole schema is stale (drop + recreate), and `_ADDED_COLUMNS` adds new columns via `ALTER TABLE`
so saved comparisons survive an update. Anything additive goes into `_ADDED_COLUMNS` —
never into the drop path. (Iteration И5 of the roadmap replaces both with `PRAGMA user_version`
plus a single, one-time reset; do not pre-empt it.)

---

## API Endpoints

All return JSON.

```
POST   /api/mortgage      mortgage + repayment strategy in one payload →
                          {id, strategy_id, monthly_payment, total_interest, payment_count}
GET    /api/mortgage/<id> Get mortgage by id
GET    /api/mortgage      List all mortgages (history)

POST   /api/deposit       {annual_rate, term_months, capitalization} → {id}
GET    /api/deposit/<id>  Get deposit by id

POST   /api/comparison    {strategy_id, deposit_id} → calculate + save, return full results
                          incl. `schedules` per scenario, `own_cost`, `cash_parity`, `winner`,
                          `option_statuses`, `options_applicable`
GET    /api/comparison/<id>   Saved scalar results (no schedules)
GET    /api/comparison        List all comparisons (history)
```

`POST /api/mortgage` validates the input before saving: required fields (`0` is a legal value,
only `None`/`''` are missing), date format `DD.MM.YYYY`, `last > first`, at least one month
between them (otherwise the payment grid is empty), positive balance and payment, non-negative
rate, and `early_repayment_allocation` from the allowed set. Everything else returns 400 with a
readable Russian message.

`POST /api/comparison` returns `schedules.{baseline,deposit,reduce_payment,reduce_term[,snowball]}`.
Every schedule is prefixed with one static row for the last payment already made, so row 1 always
shows the balance the user entered.

---

## Calculation Logic

### The engine — `web/app/engine.py`

`simulate_strategy(state, events, opts) -> StrategyResult` is the single entry point and the only
month-by-month loop. Everything else is a wrapper:

| Wrapper (`calculator.py`) | Events passed to the engine |
|---|---|
| `build_amortization()` | `[]` — a plain annuity schedule |
| `simulate_lump_repayment()` | one `RepaymentEvent(kind='lump')` |
| `simulate_snowball()` / `calc_repayment_schedule()` | one `kind='recurring'` budget event (+ optional lump) |

Contracts: `MortgageState` (balance, rate, first/last payment dates, accrual anchor, contract
payment), `RepaymentEvent` (amount, date, `kind`, `mode`, `allocation`, `amount_kind`, plus the
recurring fields `start_date` / `end_date` / `day_of_month` / `period_months` / `phase`),
`SimOptions` (`basis`, `allocation`), `StrategyResult` (schedule, `total_interest`,
`monthly_payment`, `annuity_months`, `months_to_payoff`, `dates`, `lump_unused`, `status`).

**Interest is accrued over a list of segments**, not by three special cases. The period between
two scheduled payments is cut by events into `(start, end, balance)` segments, and the period's
interest is computed from that list. This is what makes the early-repayment invariant structural:
an event only closes the current accrual segment and opens the next one.

**The accrual basis is fixed once per comparison** and comes **only** from `adjust_business_days`
(`basis_for()`):

- `BASIS_MONTHLY` — the period costs exactly `balance * annual/12` no matter how many days it has;
  when an event splits it, that same amount is distributed between the segments pro rata by days;
- `BASIS_DAILY` — `balance * annual/365 * actual days`.

Mixing bases is forbidden (a daily balance multiplied by a monthly rate is exactly the bug that
produced the wrong 8 366.21 in the roadmap). `monthly_extra_day` does **not** switch the basis.

**One date grid.** `payment_grid()` is the only source of payment dates: the schedule, the
snowball indices and the date the deposit is poured into the mortgage all come from it. Two grids
(raw `rrule` vs business-day-shifted) is a bug class of its own.

Each schedule row carries `row_kind` (`'annuity' | 'early'`) and `early_interest`. **The frontend
must branch on `row_kind`, never guess from `interest == 0` or from `early > 0`** — in
`interest_first` an early row can legitimately have `early == 0` with all of the money going into
interest.

A zero annual rate is a legal, degenerate input (interest-free instalments): the annuity formula
divides by `factor − 1`, so `_annuity()` handles it in a separate branch — principal split evenly
over the remaining periods.

Known and deliberately kept: `rrule(MONTHLY)` skips short months, so with a payment day of 30 or
31 February drops out of the grid and one row covers 59 days. Recorded in the golden snapshot as-is.

### Early repayment: an invariant that became the default

**`early_repayment_allocation` is a user setting; `principal_only` is the default and it is the
invariant.**

`principal_only` — the early repayment goes 100% into the principal and never pays interest.
Interest already accrued for the running period is charged in full on the pre-event balance and
is presented by the next annuity. The early row has `interest = 0` and `early_interest = 0`.
Fixed by commit `3ca4b3e`, guarded by `tests/test_invariant.py`. Do not weaken it.

`interest_first` — interest accrued from the previous payment up to the repayment date is
withheld from the payment first, the remainder goes into the principal. The segment cursor moves
to the event date, so the next annuity charges interest only for the remaining days — no double
accrual. If the payment is smaller than the accrued interest, only what was paid is withheld, the
principal does not decrease, and the uncovered remainder (`carried_interest`) is added to the next
annuity's interest.

Three application points; in the first two the two allocations are **identical**:

- lump **before** the first upcoming payment (or with no date) → applied at once, there is no
  accrued period yet;
- lump **on** a payment date → applied *after* that date's annuity, so that payment's interest
  equals the baseline one;
- lump **between** payments → the period is split into segments; only here do the modes diverge.

### Deposit
With capitalization (compound monthly):
```
A = P * (1 + r/12)^n
income = A - P
```
Without capitalization (simple interest):
```
income = P * (annual_rate/100) * (term_months/12)
```

### The freed payment earns nothing: `REINVEST_EARNING_MONTHS = 0`

In `reduce_payment` the borrower stops paying `contract_payment − new_monthly` every month. That
money is accounted for as *not spent* (cash parity), but it **earns no income**:
`calc_reinvest_income()` is called with `earning_months = REINVEST_EARNING_MONTHS = 0`.

The reason is the products, not caution: deposits start at 50 000 ₽ and cannot be topped up, while
a few thousand roubles a month is what gets freed — there is nowhere to put it. Only the **one-off
sum** goes on a deposit, because it is already there. The monthly budget surplus has no
alternative either: it goes into the mortgage or it sits idle.

The measurement that killed the previous rule (roadmap decision 16, cancelled; decision 17 is the
replacement): mortgage 2 983 243 ₽ at 7.99% over 294 months, deposit 16%, 2 635 ₽/month freed →
`reinvest_income` = 8 739 709 ₽ on a three-million loan, `own_cost` going negative
(−5 322 774 ₽), and `reduce_payment` winning because of an assumption about deposit rates
twenty-five years out.

### Comparison metric — `run_comparison()`

```
own_cost        = total_interest − deposit_income − reinvest_income
interest_saved  = own_cost(baseline) − own_cost(scenario)
winner          = argmax(interest_saved) over APPLICABLE scenarios only
```

`status = 'not_applicable'` means exactly one thing: **not a single event was applied**, so the
scenario's schedule is byte-identical to the baseline (e.g. the lump date is past the end of the
schedule). Such a scenario is excluded from the contest — its saving is exactly 0.00 ₽, which
beats any negative one, so "did nothing" would win (roadmap decision 6). An unspent lump on its
own does **not** set the status: in the snowball the loan can close before the lump date, and the
lump is legitimately not needed while the scenario itself did happen. Statuses and the contest
pool come back as `option_statuses` and `options_applicable`.

`lump_unused` is **not** part of the metric (the surplus already sits inside `deposit_income`,
since `F = S + income`); it is returned for reference together with `*_status`.

**Cash-flow parity is a contract, not a nice-to-have** (roadmap decision 7): every scenario must
spend the same roubles in every month, apart from the month of the external contribution and the
tail. Checked by `cash_parity_report()`; the answer carries `cash_parity`, `cash_parity_notes` and
`cash_parity_ok`. A non-empty report means the comparison is invalid.

A non-empty `monthly_budget` switches the comparison to the snowball family; the switch is visible
through `baseline_kind` / `baseline_kind_reason` in the answer and in the parameters card.

---

## Docker

`Dockerfile` (repo root):
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV DB_PATH=/app/db/mortgage_web.db
ENV PYTHONPATH=/app/web
EXPOSE 5000
CMD ["python", "-m", "flask", "--app", "web/app/main.py", "run", "--host=0.0.0.0", "--port=5000"]
```

`docker-compose.yml` (repo root):
```yaml
services:
  web:
    build:
      context: .
      dockerfile: Dockerfile
    ports:
      - "5000:5000"
    volumes:
      - ./db:/app/db
    environment:
      - DB_PATH=/app/db/mortgage_web.db
      - FLASK_DEBUG=0
```

---

## Design Notes

- Modern minimal style: white cards, subtle shadows, blue accent color (#2563EB).
- Mobile-friendly layout, centered single column.
- Progress indicator at the top: 1 «Параметры» → 2 «Результат».
- All currency values formatted with thousands separators (e.g. `1 500 000 ₽`).
- Charts: remaining balance per scenario over time, and a bar chart of the gain per scenario (Chart.js).
- No page reloads — JS posts to the API and updates the DOM.
- Early-repayment rows in the schedule table get `class="row-early"` and a badge; the gate is
  `row_kind === 'early'`, with a fallback for old saved answers that have no `row_kind`.

---

## Roadmap and Changelog

`ROADMAP.md` is the plan: nine iterations (И0–И9), the wish list (W1–W9), 17 numbered decisions and
the open questions. Before changing calculation behaviour, check whether a decision already covers
it — decisions 2 (the invariant), 4 (single basis), 5 (`own_cost`), 7 (cash parity) and 17
(the freed payment earns nothing) are load-bearing.

`CHANGELOG.md` follows Keep a Changelog. Every entry that changes numbers must carry the
**measured** before/after and the configuration it was measured in — estimates are not accepted.
`scripts/snapshot_golden.py --accept` appends its own line there automatically.

---

## Legacy Telegram Bot (tgapp_legacy/)

The original bot is in `tgapp_legacy/`. It is **not being modified** and is no longer imported by
the web app — `web/app/` has no dependency on it at all.

Known issues in legacy code (do not fix):
- `tgapp_legacy/mortgage_count.py` line 11: broken import `from mortgage import Mortgage`
- `tgapp_legacy/discussion_vote.py`, `tgapp_legacy/estimation_vote.py`: unused leftovers
- `run.sh`: hard-coded path from another machine; it builds the web-app Dockerfile under the bot's
  name. Not part of the web app, do not run it.

---

## What Is Not Implemented (out of scope)

- User authentication / multiple users
- Variable interest rates on mortgage
- Differentiated payment type (only annuity)
- Insurance / commission fees on mortgage
- Currency selection (rubles only, ₽)
- Time value of money in `own_cost` (a scenario that closes three years earlier frees the whole
  budget for those years; consciously accepted)
- A separate reinvestment rate (the deposit rate is used)

Planned, so **not** to be treated as out of scope: arbitrary numbers of early-repayment events
(И9), optional deposit tax / НДФЛ off by default (И8), the deposit-profitability threshold (И8),
one-page layout and unloaded cards (И4a).
