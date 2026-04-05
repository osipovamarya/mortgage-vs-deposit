/* ============================================================
   Mortgage vs Deposit — frontend logic
   Flow: Step 1 (mortgage) → Step 2 (deposit) → Step 3 (results)
   All API calls go to Flask backend via fetch().
   ============================================================ */

// ── State ─────────────────────────────────────────────────────
const state = {
  currentStep: 1,
  mortgageId: null,
  strategyId: null,
  depositId: null,
  mortgageResult: null,   // {monthly_payment, total_interest, payment_count}
  comparisonData: null,   // full response from /api/comparison
  hasLumpSum: false,      // whether user entered a lump_sum
  activeTab: 'baseline',  // schedule table tab
  chartBalance: null,     // Chart.js instance
  chartGain: null,        // Chart.js instance
};

// ── DOM references ─────────────────────────────────────────────
const sections = {
  mortgage: document.getElementById('section-mortgage'),
  results:  document.getElementById('section-results'),
};

// ── Helpers ───────────────────────────────────────────────────

/** Format a number as "1 500 000 ₽" */
function rub(amount) {
  return new Intl.NumberFormat('ru-RU', {
    style: 'currency', currency: 'RUB',
    minimumFractionDigits: 0, maximumFractionDigits: 0,
  }).format(Math.round(amount));
}

/** Format months as "25 лет 3 мес." */
function fmtMonths(n) {
  const years  = Math.floor(n / 12);
  const months = n % 12;
  const parts  = [];
  if (years  > 0) parts.push(`${years} ${pluralRu(years,  ['год','года','лет'])}`);
  if (months > 0) parts.push(`${months} мес.`);
  return parts.join(' ') || '0 мес.';
}

/** Format "DD.MM.YYYY" as "2 марта 2045" */
function fmtDate(ddmmyyyy) {
  const [d, m, y] = ddmmyyyy.split('.');
  const months = ['января','февраля','марта','апреля','мая','июня',
                  'июля','августа','сентября','октября','ноября','декабря'];
  return `${parseInt(d)} ${months[parseInt(m) - 1]} ${y}`;
}

/** Last payment date from a schedule array */
function scheduleEndDate(schedule) {
  if (!schedule || !schedule.length) return null;
  return schedule[schedule.length - 1].date;
}

/** Months between two "DD.MM.YYYY" dates (B - A) */
function monthsBetween(ddmmyyyyA, ddmmyyyyB) {
  const [da, ma, ya] = ddmmyyyyA.split('.').map(Number);
  const [db, mb, yb] = ddmmyyyyB.split('.').map(Number);
  return (yb - ya) * 12 + (mb - ma);
}

function pluralRu(n, [one, few, many]) {
  const mod10  = n % 10;
  const mod100 = n % 100;
  if (mod100 >= 11 && mod100 <= 19) return many;
  if (mod10 === 1) return one;
  if (mod10 >= 2 && mod10 <= 4) return few;
  return many;
}

/** POST JSON to url, return parsed JSON or throw {error: string} */
async function postJSON(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function showError(elementId, message) {
  const el = document.getElementById(elementId);
  el.textContent = message;
  el.classList.remove('hidden');
}

function clearError(elementId) {
  const el = document.getElementById(elementId);
  el.textContent = '';
  el.classList.add('hidden');
}

// ── Step navigation ───────────────────────────────────────────

function goToStep(n) {
  state.currentStep = n;

  // Show/hide sections (2 steps: 1=input, 2=results)
  sections.mortgage.classList.toggle('hidden', n !== 1);
  sections.results.classList.toggle('hidden',  n !== 2);

  // Update step indicators
  [1, 2].forEach(i => {
    const dot = document.getElementById(`step-dot-${i}`);
    dot.classList.remove('active', 'done');
    if (i < n)  dot.classList.add('done');
    if (i === n) dot.classList.add('active');
  });

  window.scrollTo({ top: 0, behavior: 'smooth' });
}

// ── Step 1: Mortgage form ─────────────────────────────────────

document.getElementById('form-mortgage').addEventListener('submit', async (e) => {
  e.preventDefault();
  clearError('mortgage-error');

  const payload = {
    loan_amount:          document.getElementById('loan_amount').value,
    annual_rate:          document.getElementById('annual_rate').value,
    monthly_payment:      document.getElementById('monthly_payment').value || null,
    first_payment_date:   document.getElementById('first_payment_date').value,
    last_payment_date:    document.getElementById('last_payment_date').value,
    adjust_business_days: document.getElementById('adjust_business_days').checked ? 1 : 0,
    lump_sum:             document.getElementById('lump_sum').value || null,
    lump_sum_date:        document.getElementById('lump_sum_date').value || null,
    monthly_budget:       document.getElementById('monthly_budget').value || null,
    monthly_start_date:   document.getElementById('monthly_start_date').value || null,
    monthly_extra_day:    document.getElementById('monthly_extra_day').value || null,
    repayment_mode:       document.querySelector('input[name="repayment_mode"]:checked').value,
  };

  // Validate lump_sum_date: must be strictly after last actual payment (first_payment_date)
  if (payload.lump_sum && payload.lump_sum_date) {
    const parseDate = s => { const [d,m,y] = s.split('.'); return new Date(+y, +m-1, +d); };
    const lumpDt  = parseDate(payload.lump_sum_date);
    const firstDt = parseDate(payload.first_payment_date);
    if (isNaN(lumpDt)) {
      showError('mortgage-error', 'Дата разового погашения: неверный формат (ожидается ДД.ММ.ГГГГ)');
      return;
    }
    if (lumpDt <= firstDt) {
      showError('mortgage-error', `Дата разового погашения должна быть позже последнего платежа (${payload.first_payment_date})`);
      return;
    }
  }

  const btn = e.target.querySelector('button[type=submit]');
  btn.disabled = true;
  btn.textContent = 'Считаем…';

  const depositPayload = {
    annual_rate:    document.getElementById('deposit_rate').value,
    term_months:    document.getElementById('deposit_term').value,
    capitalization: document.getElementById('capitalization').checked ? 1 : 0,
  };

  try {
    const result = await postJSON('/api/mortgage', payload);
    state.mortgageId     = result.id;
    state.strategyId     = result.strategy_id;
    state.mortgageResult = result;
    state.hasLumpSum     = !!(payload.lump_sum && parseFloat(payload.lump_sum) > 0);
    state.mortgageParams = payload;
    state.depositParams  = depositPayload;

    const depositResult = await postJSON('/api/deposit', depositPayload);
    state.depositId = depositResult.id;

    const comparison = await postJSON('/api/comparison', {
      strategy_id: state.strategyId,
      deposit_id:  state.depositId,
    });
    state.comparisonData = comparison;
    renderResults(comparison);
    goToStep(2);
  } catch (err) {
    showError('mortgage-error', err.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = 'Сравнить <span class="arrow">→</span>';
  }
});


// ── Step 3: Results ───────────────────────────────────────────

/** Sum of interest column across a schedule */
function totalInterest(schedule) {
  if (!schedule) return 0;
  return Math.round(schedule.reduce((s, r) => s + (r.interest || 0), 0));
}

function renderResults(d) {
  const mode = d.repayment_mode || 'reduce_payment';

  // Absolute total interest per scenario (lower = better)
  const baseTotal = totalInterest(d.schedules.baseline);
  d._totals = {
    baseline:       baseTotal,
    deposit:        state.hasLumpSum ? totalInterest(d.schedules.deposit) - Math.round(d.deposit_income) : null,
    reduce_payment: mode === 'reduce_payment' ? totalInterest(d.schedules.reduce_payment) : null,
    reduce_term:    mode === 'reduce_term'    ? totalInterest(d.schedules.reduce_payment) : null,
    snowball:       d.snowball_interest_saved != null ? totalInterest(d.schedules.snowball) : null,
  };

  // Winner = scenario with minimum effective cost (lower total interest)
  const candidates = {};
  if (d._totals.deposit        != null) candidates.deposit        = d._totals.deposit;
  if (d._totals.reduce_payment != null) candidates.reduce_payment = d._totals.reduce_payment;
  if (d._totals.reduce_term    != null) candidates.reduce_term    = d._totals.reduce_term;
  if (d._totals.snowball       != null) candidates.snowball       = d._totals.snowball;
  d.effective_winner = Object.keys(candidates).length
    ? Object.entries(candidates).reduce((a, b) => a[1] <= b[1] ? a : b)[0]
    : null;

  const hasEarly = (mode === 'reduce_payment' && d.reduce_payment_interest_saved > 0)
                || (mode === 'reduce_term'    && d.reduce_term_interest_saved    > 0);

  renderParamsCard(d);
  renderWinnerBanner(d);
  renderCards(d);
  renderChartBalance(d.schedules, mode, hasEarly, d.snowball_deposit_series);
  renderChartGain(d, hasEarly);
  renderScheduleTable('baseline', d.schedules);
  initTableTabs(d.schedules);
}

function renderParamsCard(d) {
  const m = state.mortgageParams || {};
  const dep = state.depositParams || {};
  const hasSnow = !!(m.monthly_budget && parseFloat(m.monthly_budget) > 0);
  const capLabel = dep.capitalization ? 'с капитализацией' : 'без капитализации';

  let html = `
    <div class="metric metric--compact">
      <div class="metric-label">Остаток долга</div>
      <div class="metric-value">${rub(m.loan_amount)}</div>
    </div>
    <div class="metric metric--compact">
      <div class="metric-label">Ставка</div>
      <div class="metric-value">${m.annual_rate}% год.</div>
    </div>
    <div class="metric metric--compact">
      <div class="metric-label">Платёж</div>
      <div class="metric-value">${rub(m.monthly_payment)}</div>
    </div>
    <div class="metric metric--compact">
      <div class="metric-label">Конец договора</div>
      <div class="metric-value">${m.last_payment_date}</div>
    </div>
    <div class="metric metric--compact">
      <div class="metric-label">Проценты без изменений</div>
      <div class="metric-value">${d && d._totals ? rub(d._totals.baseline) : '—'}</div>
    </div>`;

  if (state.hasLumpSum) {
    html += `
    <div class="params-section-label" style="margin-top:.8rem">Разовое погашение</div>
    <div class="metric metric--compact">
      <div class="metric-label">Сумма</div>
      <div class="metric-value">${rub(m.lump_sum)}</div>
    </div>
    <div class="metric metric--compact">
      <div class="metric-label">Дата</div>
      <div class="metric-value">${m.lump_sum_date}</div>
    </div>`;
  }

  document.getElementById('card-params-body').innerHTML = html;
}

function renderWinnerBanner(d) {
  const labels = {
    deposit:        'Вклад → затем погасить',
    reduce_payment: 'Досрочно → уменьшить платёж',
    reduce_term:    'Досрочно → уменьшить срок',
    snowball:       'Снежный ком',
  };
  const w = d.effective_winner || d.winner;
  if (!w) return;
  const cssClass = w === 'deposit' ? 'deposit' : w === 'snowball' ? 'snowball' : w === 'reduce_term' ? 'term' : 'repay';
  const winTotal   = d._totals[w];
  const baseTotal  = d._totals.baseline;
  const saved      = baseTotal - winTotal;
  const banner = document.getElementById('winner-banner');
  banner.className = `winner-banner ${cssClass}`;
  banner.innerHTML = `
    🏆 ${labels[w]} — меньше всего процентов: <strong>${rub(winTotal)}</strong>
    <span class="banner-sub">на ${rub(saved)} меньше чем ничего не делать</span>
  `;
}

function renderCards(d) {
  const mode = d.repayment_mode || 'reduce_payment';

  // Has any early repayment been specified?
  const hasEarly = (mode === 'reduce_payment' && d.reduce_payment_interest_saved > 0)
                || (mode === 'reduce_term'    && d.reduce_term_interest_saved    > 0);

  // Show/hide deposit card and tab (only when lump_sum was entered)
  document.getElementById('card-deposit').classList.toggle('hidden', !state.hasLumpSum);
  document.querySelector('.tab-btn[data-tab="deposit"]').classList.toggle('hidden', !state.hasLumpSum);

  // Show/hide repayment mode cards
  document.getElementById('card-reduce-payment').classList.toggle('hidden', mode !== 'reduce_payment' || !hasEarly);
  document.getElementById('card-reduce-term').classList.toggle('hidden',    mode !== 'reduce_term'    || !hasEarly);

  // Update table tab labels and visibility
  const tabRp = document.querySelector('.tab-btn[data-tab="reduce_payment"]');
  const tabRt = document.querySelector('.tab-btn[data-tab="reduce_term"]');
  tabRt.classList.add('hidden');
  if (!hasEarly) {
    tabRp.classList.add('hidden');
  } else {
    tabRp.classList.remove('hidden');
    tabRp.textContent = mode === 'reduce_term' ? 'Погасить досрочно (срок)' : 'Погасить досрочно (платёж)';
  }

  // Deposit card (lump_sum scenario)
  if (state.hasLumpSum) {
    const dep = state.depositParams || {};
    const capLabel = dep.capitalization ? 'с капитализацией' : 'без капитализации';
    const depMortgageInterest = totalInterest(d.schedules.deposit);
    const depSaved = d._totals.baseline - d._totals.deposit;
    const depositPaymentRow = mode === 'reduce_payment'
      ? `<div class="metric">
          <div class="metric-label">Новый платёж после погашения</div>
          <div class="metric-value purple">${rub(d.deposit_new_monthly)} / мес.</div>
        </div>`
      : '';
    document.getElementById('card-deposit-body').innerHTML = `
      <div class="metric metric--mini">
        <div class="metric-label">Ставка вклада</div>
        <div class="metric-value">${dep.annual_rate}% год. · ${dep.term_months} мес. · ${capLabel}</div>
      </div>
      <div class="metric">
        <div class="metric-label">Проценты по ипотеке</div>
        <div class="metric-value">${rub(depMortgageInterest)}</div>
      </div>
      <div class="metric">
        <div class="metric-label">Доход по вкладу</div>
        <div class="metric-value positive">−${rub(Math.round(d.deposit_income))}</div>
      </div>
      <div class="metric">
        <div class="metric-label">Итого выплат</div>
        <div class="metric-value accent">${rub(d._totals.deposit)}</div>
      </div>
      ${depositPaymentRow}
      <div class="metric--vs">на ${rub(depSaved)} меньше чем ничего не делать</div>
    `;
  }

  // Reduce payment card
  const rpTotal = d._totals.reduce_payment;
  const rpSaved = d._totals.baseline - rpTotal;
  document.getElementById('card-reduce-payment-body').innerHTML = `
    <div class="metric">
      <div class="metric-label">Проценты по ипотеке</div>
      <div class="metric-value">${rub(rpTotal)}</div>
    </div>
    <div class="metric">
      <div class="metric-label">Новый платёж</div>
      <div class="metric-value purple">${rub(d.reduce_payment_new_monthly)} / мес.</div>
    </div>
    <div class="metric--vs">на ${rub(rpSaved)} меньше чем ничего не делать</div>
  `;

  // Reduce term card
  const rtEndDate = scheduleEndDate(d.schedules.reduce_payment);
  const rtTotal = d._totals.reduce_term;
  const rtSaved = d._totals.baseline - rtTotal;
  document.getElementById('card-reduce-term-body').innerHTML = `
    <div class="metric">
      <div class="metric-label">Проценты по ипотеке</div>
      <div class="metric-value">${rub(rtTotal)}</div>
    </div>
    <div class="metric">
      <div class="metric-label">Ипотека закроется</div>
      <div class="metric-value purple">${rtEndDate ? fmtDate(rtEndDate) : fmtMonths(d.reduce_term_months_to_payoff)}</div>
    </div>
    <div class="metric">
      <div class="metric-label">Срок сокращается на</div>
      <div class="metric-value accent">${fmtMonths(d.reduce_term_months_saved)}</div>
    </div>
    <div class="metric--vs">на ${rub(rtSaved)} меньше чем ничего не делать</div>
  `;

  // Snowball card — shown only when monthly_budget was provided
  const snowballCard = document.getElementById('card-snowball');
  const snowballDepCard = document.getElementById('card-snowball-deposit');
  if (d.snowball_interest_saved != null) {
    snowballCard.classList.remove('hidden');
    document.getElementById('tab-snowball').classList.remove('hidden');

    const baseEndDate  = scheduleEndDate(d.schedules.baseline);
    const swEndDate    = scheduleEndDate(d.schedules.snowball);
    const savedMonths  = (baseEndDate && swEndDate) ? monthsBetween(swEndDate, baseEndDate) : 0;

    const mp = state.mortgageParams || {};
    let snowParamsLine = `бюджет ${rub(mp.monthly_budget)}/мес.`;
    if (mp.monthly_start_date) snowParamsLine += ` · с ${mp.monthly_start_date}`;
    if (mp.monthly_extra_day)  snowParamsLine += ` · досрочка ${mp.monthly_extra_day}-го числа`;
    const swTotal = d._totals.snowball;
    const swSaved = d._totals.baseline - swTotal;
    document.getElementById('card-snowball-body').innerHTML = `
      <div class="metric metric--mini">
        <div class="metric-label">Параметры</div>
        <div class="metric-value">${snowParamsLine}</div>
      </div>
      <div class="metric">
        <div class="metric-label">Проценты по ипотеке</div>
        <div class="metric-value">${rub(swTotal)}</div>
      </div>
      <div class="metric">
        <div class="metric-label">Ипотека закроется</div>
        <div class="metric-value purple">${swEndDate ? fmtDate(swEndDate) : fmtMonths(d.snowball_months_to_payoff)}</div>
      </div>
      <div class="metric">
        <div class="metric-label">Срок сокращается на</div>
        <div class="metric-value accent">${fmtMonths(savedMonths)}</div>
      </div>
      <div class="metric--vs">на ${rub(swSaved)} меньше чем ничего не делать</div>
    `;

    // Snowball deposit alternative card
    if (d.snowball_deposit_months_to_match != null) {
      snowballDepCard.classList.remove('hidden');
      const surplus = d.monthly_surplus != null ? d.monthly_surplus : null;
      const surplusRow = surplus != null
        ? `<div class="metric">
            <div class="metric-label">Ежемесячный взнос</div>
            <div class="metric-value accent">${rub(surplus)} / мес.</div>
          </div>`
        : '';
      const matchN = d.snowball_deposit_months_to_match; // 1-based month number
      const matchIdx = matchN - 1;
      const matchDate = d.snowball_deposit_series && d.snowball_deposit_series[matchIdx]
        ? fmtDate(d.snowball_deposit_series[matchIdx].date)
        : fmtMonths(matchN);
      // baseline[matchN] = month matchN in with_static array (index 0 is the static row)
      const baselineRow = d.schedules.baseline[matchN];
      const mortgageBalanceAtMatch = baselineRow ? baselineRow.balance : 0;
      const remainder = d.snowball_deposit_final - mortgageBalanceAtMatch;
      document.getElementById('card-snowball-deposit-body').innerHTML = `
        ${surplusRow}
        <div class="metric metric--mini">
          <div class="metric-label">Ставка</div>
          <div class="metric-value">8% год. с капитализацией — среднее ЦБ РФ за 20 лет</div>
        </div>
        <div class="metric">
          <div class="metric-label">Накопится на погашение</div>
          <div class="metric-value purple">${matchDate}</div>
        </div>
        <div class="metric">
          <div class="metric-label">Итого на вкладе</div>
          <div class="metric-value accent">${rub(d.snowball_deposit_final)}</div>
        </div>
        <div class="metric">
          <div class="metric-label">Остаток после погашения</div>
          <div class="metric-value positive">+${rub(remainder)}</div>
        </div>
      `;
    } else {
      snowballDepCard.classList.add('hidden');
    }
  } else {
    snowballCard.classList.add('hidden');
    snowballDepCard.classList.add('hidden');
    document.getElementById('tab-snowball').classList.add('hidden');
  }

  // Mark winner
  const cardMap = {
    deposit:        'card-deposit',
    reduce_payment: 'card-reduce-payment',
    reduce_term:    'card-reduce-term',
    snowball:       'card-snowball',
  };
  const w2 = d.effective_winner || d.winner;
  if (w2 && cardMap[w2]) {
    document.getElementById(cardMap[w2]).classList.add('is-winner');
  }
}

// ── Charts ────────────────────────────────────────────────────

/**
 * Build arrays of [date_label, cumulative_interest] for Chart.js.
 * To keep charts readable we sample every 12 months (yearly points).
 */
/** One point per calendar month (last balance seen that month). Returns {labels, balance}. */
function buildBalanceSeries(schedule) {
  const byMonth = {};
  const order = [];
  schedule.forEach(row => {
    const key = row.date.slice(3); // "MM.YYYY"
    if (!byMonth[key]) order.push(key);
    byMonth[key] = row.balance;
  });
  return { labels: order, balance: order.map(k => byMonth[k]) };
}

/** Align a schedule's monthly balances to a given label array; missing months → null. */
function alignSeries(schedule, labels) {
  const byMonth = {};
  schedule.forEach(row => { byMonth[row.date.slice(3)] = row.balance; });
  return labels.map(k => byMonth[k] != null ? byMonth[k] : null);
}

function renderChartBalance(schedules, mode, hasEarly, snowballDepositSeries) {
  if (state.chartBalance) state.chartBalance.destroy();

  const base       = buildBalanceSeries(schedules.baseline);
  const baseLabels = base.labels;

  const rpLabel = mode === 'reduce_term' ? 'Погасить досрочно (срок)' : 'Погасить досрочно (платёж)';
  const rpColor = mode === 'reduce_term' ? '#EA580C' : '#7C3AED';
  const rpBg    = mode === 'reduce_term' ? 'rgba(234,88,12,.08)' : 'rgba(124,58,237,.08)';

  const datasets = [
    {
      label: 'Без изменений',
      data: base.balance,
      spanGaps: false,
      borderColor: '#94A3B8',
      backgroundColor: 'rgba(148,163,184,.08)',
      borderWidth: 2,
      pointRadius: 0,
      fill: false,
    },
  ];

  if (state.hasLumpSum) {
    datasets.push({
      label: 'Погасить после вклада',
      data: alignSeries(schedules.deposit, baseLabels),
      borderColor: '#2563EB',
      backgroundColor: 'rgba(37,99,235,.08)',
      borderWidth: 2.5,
      pointRadius: 0,
      fill: false,
      spanGaps: false,
    });
  }

  if (hasEarly) {
    datasets.push({
      label: rpLabel,
      data: alignSeries(schedules.reduce_payment, baseLabels),
      borderColor: rpColor,
      backgroundColor: rpBg,
      borderWidth: 2.5,
      pointRadius: 0,
      fill: false,
      spanGaps: false,
    });
  }

  if (schedules.snowball) {
    datasets.push({
      label: 'Снежный ком',
      data: alignSeries(schedules.snowball, baseLabels),
      borderColor: '#059669',
      backgroundColor: 'rgba(5,150,105,.08)',
      borderWidth: 2.5,
      pointRadius: 0,
      fill: false,
      spanGaps: false,
    });
  }

  if (snowballDepositSeries && snowballDepositSeries.length) {
    let depSeries = snowballDepositSeries;
    const snowEndDate = scheduleEndDate(schedules.snowball);
    if (snowEndDate) {
      const toYMD = s => s.slice(6) + s.slice(3, 5) + s.slice(0, 2);
      const limit = toYMD(snowEndDate);
      depSeries = snowballDepositSeries.filter(r => toYMD(r.date) <= limit);
    }
    datasets.push({
      label: 'Вклад вместо досрочек',
      data: alignSeries(depSeries, baseLabels),
      borderColor: '#F59E0B',
      backgroundColor: 'rgba(245,158,11,.06)',
      borderWidth: 2,
      borderDash: [6, 4],
      pointRadius: 0,
      fill: false,
      spanGaps: false,
    });
  }

  const ctx = document.getElementById('balance-chart').getContext('2d');
  state.chartBalance = new Chart(ctx, {
    type: 'line',
    data: { labels: baseLabels, datasets },
    options: {
      responsive: true,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { position: 'bottom' },
        tooltip: {
          callbacks: {
            label: ctx => ` ${ctx.dataset.label}: ${rub(ctx.parsed.y)}`,
            title: items => {
              // "MM.YYYY" → "месяц YYYY"
              const key = items[0].label;
              const [mm, yyyy] = key.split('.');
              const months = ['январь','февраль','март','апрель','май','июнь',
                              'июль','август','сентябрь','октябрь','ноябрь','декабрь'];
              return `${months[parseInt(mm) - 1]} ${yyyy}`;
            },
          },
        },
      },
      scales: {
        x: {
          ticks: {
            maxRotation: 45,
            minRotation: 45,
            autoSkip: false,
            callback: (val, idx) => {
              const key = baseLabels[idx];
              return key && key.startsWith('01.') ? key.slice(3) : '';
            },
          },
          grid: {
            color: (ctx) => {
              const key = baseLabels[ctx.index];
              return key && key.startsWith('01.') ? 'rgba(0,0,0,0.08)' : 'transparent';
            },
          },
        },
        y: {
          ticks: {
            callback: v => rub(v),
            maxTicksLimit: 6,
          },
        },
      },
    },
  });
}

function renderChartGain(d, hasEarly) {
  if (state.chartGain) state.chartGain.destroy();

  const mode = d.repayment_mode || 'reduce_payment';
  const rpLabel  = mode === 'reduce_term' ? 'Погасить досрочно (срок)' : 'Погасить досрочно (платёж)';
  const rpValue  = mode === 'reduce_term' ? d.reduce_term_interest_saved : d.reduce_payment_interest_saved;
  const rpColor  = mode === 'reduce_term' ? 'rgba(234,88,12,.75)' : 'rgba(124,58,237,.75)';

  const labels = [];
  const values = [];
  const colors = [];
  if (state.hasLumpSum) { labels.push('Погасить после вклада'); values.push(d.deposit_net_saving); colors.push('rgba(37,99,235,.75)'); }
  if (hasEarly)         { labels.push(rpLabel);                 values.push(rpValue);               colors.push(rpColor); }

  if (d.snowball_interest_saved != null) {
    labels.push('Снежный ком');
    values.push(d.snowball_interest_saved);
    colors.push('rgba(5,150,105,.75)');
  }

  const ctx = document.getElementById('gain-chart').getContext('2d');
  state.chartGain = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        label: 'Экономия на процентах (₽)',
        data: values,
        backgroundColor: colors,
        borderRadius: 6,
        borderSkipped: false,
      }],
    },
    options: {
      responsive: true,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: ctx => ` ${rub(ctx.parsed.y)}`,
          },
        },
      },
      scales: {
        y: {
          ticks: {
            callback: v => rub(v),
            maxTicksLimit: 6,
          },
          beginAtZero: true,
        },
      },
    },
  });
}

// ── Schedule table ────────────────────────────────────────────

function renderScheduleTable(tab, schedules) {
  const rows  = schedules[tab] || [];
  const entered = state.comparisonData && state.comparisonData.entered_monthly_payment;
  const tbody = document.getElementById('schedule-tbody');
  tbody.innerHTML = rows.map((r, i) => {
    const isFirst = i === 0 && entered && Math.abs(entered - r.payment) > 0.05;
    const paymentCell = isFirst
      ? `${rub(r.payment)} <small class="fact-payment">(факт: ${rub(entered)})</small>`
      : rub(r.payment);
    const isEarly = r.early && r.early > 0.5;
    const earlyLabel = (isEarly && r.interest === 0)
      ? 'досрочно'
      : `+${rub(r.early)} досрочно`;
    const principalCell = isEarly
      ? `${rub(r.principal)} <span class="early-badge">${earlyLabel}</span>`
      : rub(r.principal);
    return `
    <tr${isEarly ? ' class="row-early"' : ''}>
      <td>${r.payment_num}</td>
      <td>${r.date}</td>
      <td>${paymentCell}</td>
      <td>${principalCell}</td>
      <td>${rub(r.interest)}</td>
      <td>${rub(r.balance)}</td>
    </tr>`;
  }).join('');
}

function initTableTabs(schedules) {
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.activeTab = btn.dataset.tab;
      renderScheduleTable(state.activeTab, schedules);
    });
  });
}

// Toggle table visibility
document.getElementById('btn-toggle-table').addEventListener('click', function () {
  const wrap = document.getElementById('schedule-table-wrap');
  const hidden = wrap.classList.toggle('hidden');
  this.textContent = hidden ? 'Показать таблицу ↓' : 'Скрыть таблицу ↑';
});

// ── Recalculate ───────────────────────────────────────────────

document.getElementById('btn-recalc').addEventListener('click', () => {
  state.mortgageId     = null;
  state.strategyId     = null;
  state.depositId      = null;
  state.mortgageResult = null;
  state.comparisonData = null;
  state.hasLumpSum     = false;
  // Reset winner marks and hide dynamic cards
  ['card-deposit','card-reduce-payment','card-reduce-term','card-snowball','card-snowball-deposit'].forEach(id => {
    document.getElementById(id).classList.remove('is-winner');
  });
  document.getElementById('card-snowball').classList.add('hidden');
  document.getElementById('card-snowball-deposit').classList.add('hidden');
  document.getElementById('tab-snowball').classList.add('hidden');
  // Reset tabs
  document.querySelectorAll('.tab-btn').forEach((b, i) => {
    b.classList.toggle('active', i === 0);
  });
  document.getElementById('schedule-table-wrap').classList.remove('hidden');
  document.getElementById('btn-toggle-table').textContent = 'Скрыть таблицу ↑';
  goToStep(1);
});
