# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Project: Mortgage vs Deposit Efficiency Calculator

A locally-run web application that answers one question: **is it more profitable to put your savings on a deposit account, or use them for a partial mortgage repayment?**

The app walks the user through two forms (mortgage → deposit), then shows a side-by-side comparison of both strategies with a clear winner.

---

## Repository Layout

```
mortgage_calc/
├── tgapp_legacy/                 # Original Telegram bot (legacy, do not modify)
│   ├── bot.py
│   ├── mortgage.py               # ← Reused by the web app for calculations
│   ├── mortgage_registry.py
│   ├── mortgage_count.py
│   ├── telegram_user.py
│   ├── Dockerfile
│   └── requirements.txt
├── web/                          # New Flask web application (active)
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py               # Flask app factory + entry point
│   │   ├── database.py           # SQLite connection, schema migrations
│   │   ├── calculator.py         # Deposit + comparison calculation logic
│   │   └── routes/
│   │       ├── __init__.py
│   │       ├── mortgage.py       # /api/mortgage endpoints
│   │       ├── deposit.py        # /api/deposit endpoints
│   │       └── comparison.py     # /api/comparison endpoints
│   ├── static/
│   │   ├── css/
│   │   │   └── style.css         # Modern clean design
│   │   └── js/
│   │       └── app.js            # Step-by-step form logic + result rendering
│   └── templates/
│       └── index.html            # Single-page app shell
├── db/
│   ├── morst_bot.db              # Legacy Telegram bot database
│   └── mortgage_web.db           # Web app database (created on first run)
├── Dockerfile                    # Web app Docker image (build context: repo root)
├── docker-compose.yml
├── requirements.txt              # Web app dependencies (Flask, python-dateutil)
└── CLAUDE.md
```

---

## Running the Web App

**With Docker (recommended):**
```bash
cd /path/to/mortgage_calc
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

---

## Architecture

**Backend:** Python + Flask (intentionally simple — no async, no ORM, plain sqlite3).

**Frontend:** Single HTML page with vanilla JS. No build step, no framework. Step-by-step form wizard rendered client-side; results section shown/hidden by JS. Charts via Chart.js (CDN).

**Database:** SQLite. Single-user, no authentication. All records belong to one local user.

**Calculation logic:**
- Mortgage annuity payment and schedule: reused from `tgapp_legacy/mortgage.py`
- Deposit compound interest: implemented in `web/app/calculator.py`
- Comparison logic: implemented in `web/app/calculator.py`

---

## User Flow

### Step 1 — Mortgage parameters
User enters their **current** mortgage state (not the original loan — what they have *now*):
- Remaining principal balance (сколько ещё долга)
- Annual interest rate (%)
- First upcoming payment date (DD.MM.YYYY)
- Last payment date (DD.MM.YYYY) — determines remaining term
- Monthly payment — auto-calculated from the above, but user can override

### Step 2 — Deposit / Savings parameters
User enters what they want to do with their savings:
- Amount (сумма накоплений, the money to invest or repay with)
- Annual deposit rate (%)
- Term in months (how long to keep it on deposit)
- Capitalization: yes/no (monthly compound interest or simple interest)

### Step 3 — Comparison results
Three scenarios shown as cards:

| Scenario | What it shows |
|---|---|
| **Deposit** | Total interest earned after the deposit term |
| **Repay → Reduce term** | How many months shorter, how much total interest saved |
| **Repay → Reduce payment** | New monthly payment, how much total interest saved |

A highlighted banner shows the winner (deposit income vs best repayment interest saved).

Below the cards: a toggle for a monthly payment schedule table for each scenario.

---

## Database Schema

File: `db/mortgage_web.db`

```sql
CREATE TABLE mortgage (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL DEFAULT 'My Mortgage',
    loan_amount         REAL NOT NULL,      -- remaining principal balance
    annual_rate         REAL NOT NULL,      -- interest rate % per year
    first_payment_date  DATE NOT NULL,      -- ISO date string
    last_payment_date   DATE NOT NULL,      -- ISO date string
    monthly_payment     REAL,               -- calculated; can be overridden
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE deposit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL DEFAULT 'My Deposit',
    amount          REAL NOT NULL,          -- the savings amount
    annual_rate     REAL NOT NULL,          -- deposit rate % per year
    term_months     INTEGER NOT NULL,       -- deposit duration
    capitalization  INTEGER DEFAULT 1,      -- 1 = compound monthly, 0 = simple
    start_date      DATE NOT NULL,          -- ISO date string
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE comparison (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    mortgage_id         INTEGER NOT NULL,
    deposit_id          INTEGER NOT NULL,

    -- Scenario A: keep savings on deposit
    deposit_income      REAL,               -- interest earned from deposit
    deposit_final       REAL,               -- total amount (principal + income)

    -- Scenario B1: partial repayment → reduce term
    reduce_term_new_last_date   DATE,       -- new end date
    reduce_term_months_saved    INTEGER,    -- months cut off
    reduce_term_interest_saved  REAL,       -- total interest saved vs baseline

    -- Scenario B2: partial repayment → reduce payment
    reduce_payment_new_monthly  REAL,       -- new monthly payment
    reduce_payment_interest_saved REAL,     -- total interest saved vs baseline

    -- Baseline: total interest with no changes
    baseline_total_interest     REAL,

    -- Summary
    winner              TEXT,               -- 'deposit', 'reduce_term', or 'reduce_payment'
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (mortgage_id) REFERENCES mortgage(id),
    FOREIGN KEY (deposit_id)  REFERENCES deposit(id)
);
```

Dates stored as ISO strings (`YYYY-MM-DD`). User-facing input/display uses `DD.MM.YYYY`.

---

## API Endpoints

All return JSON.

```
POST   /api/mortgage              Save mortgage, return {id, monthly_payment, schedule_preview}
GET    /api/mortgage/<id>         Get mortgage by id
GET    /api/mortgage              List all mortgages (history)

POST   /api/deposit               Save deposit params, return {id, deposit_income, deposit_final}
GET    /api/deposit/<id>          Get deposit by id

POST   /api/comparison            {mortgage_id, deposit_id} → calculate + save, return full results
GET    /api/comparison/<id>       Get comparison results with schedules
GET    /api/comparison            List all comparisons (history)
```

---

## Calculation Logic (web/app/calculator.py)

### Mortgage (reused from app/mortgage.py)
Annuity payment formula:
```
M = P * (r / (1 - (1 + r)^-n))
```
Where: `P` = principal, `r` = monthly rate (annual_rate / 12 / 100), `n` = number of payments.

The full payment schedule (principal + interest split per month) is generated month by month.

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

### Partial Repayment
After applying `deposit.amount` as a lump-sum payment to the remaining principal:

**Reduce term:** recalculate `n` keeping monthly payment the same → new `last_payment_date`.

**Reduce payment:** recalculate `M` keeping `n` the same → new smaller monthly payment.

In both cases, compare total interest paid (sum of interest portions across all payments) against the baseline schedule to get `interest_saved`.

### Comparison winner
```
winner = argmax(deposit_income, reduce_term_interest_saved, reduce_payment_interest_saved)
```

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
ENV PYTHONPATH=/app
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
- Progress indicator at the top showing current step (1 → 2 → Results).
- All currency values formatted with thousands separators (e.g. `1 500 000 ₽`).
- Charts: a simple line chart comparing cumulative interest paid over time for each scenario (Chart.js).
- No page reloads — JS posts to API, updates DOM with results.

---

## Legacy Telegram Bot (tgapp_legacy/)

The original bot is in `tgapp_legacy/`. It is **not being modified**. The file `tgapp_legacy/mortgage.py` is imported by the web app for its `Mortgage` class and annuity calculation logic. All other `tgapp_legacy/` files are legacy.

Known issues in legacy code (do not fix):
- `tgapp_legacy/mortgage_count.py` line 11: broken import `from mortgage import Mortgage`
- `tgapp_legacy/discussion_vote.py`, `tgapp_legacy/estimation_vote.py`: unused leftovers
- `run.sh`: hard-coded path for Docker volume

---

## What Is Not Implemented (out of scope)

- User authentication / multiple users
- Variable interest rates on mortgage
- Differentiated payment type (only annuity)
- Deposit taxes (e.g. Russian 13% NDFL)
- Insurance / commission fees on mortgage
- Currency selection (rubles only, ₽)
- Multiple partial repayments (only one lump sum)
