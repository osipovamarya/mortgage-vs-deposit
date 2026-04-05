/* ============================================================
   Mortgage vs Deposit — frontend logic
   Flow: Step 1 (mortgage) → Step 2 (deposit) → Step 3 (results)
   All API calls go to Flask backend via fetch().
   ============================================================ */

// ── State ─────────────────────────────────────────────────────
const state = {
  currentStep: 1,
  mortgageId: null,
  depositId: null,
  mortgageResult: null,   // {monthly_payment, total_interest, payment_count}
  comparisonData: null,   // full response from /api/comparison
  activeTab: 'baseline',  // schedule table tab
  chartBalance: null,     // Chart.js instance
  chartGain: null,        // Chart.js instance
};

// ── DOM references ─────────────────────────────────────────────
const sections = {
  mortgage: document.getElementById('section-mortgage'),
  deposit:  document.getElementById('section-deposit'),
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

  // Show/hide sections
  sections.mortgage.classList.toggle('hidden', n !== 1);
  sections.deposit.classList.toggle('hidden',  n !== 2);
  sections.results.classList.toggle('hidden',  n !== 3);

  // Update step indicators
  [1, 2, 3].forEach(i => {
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
    loan_amount:           document.getElementById('loan_amount').value,
    annual_rate:           document.getElementById('annual_rate').value,
    monthly_payment:       document.getElementById('monthly_payment').value || null,
    first_payment_date:    document.getElementById('first_payment_date').value,
    last_payment_date:     document.getElementById('last_payment_date').value,
    adjust_business_days:  document.getElementById('adjust_business_days').checked ? 1 : 0,
  };

  const btn = e.target.querySelector('button[type=submit]');
  btn.disabled = true;
  btn.textContent = 'Считаем…';

  try {
    const result = await postJSON('/api/mortgage', payload);
    state.mortgageId     = result.id;
    state.mortgageResult = result;
    renderMortgageSummary(payload, result);
    goToStep(2);
  } catch (err) {
    showError('mortgage-error', err.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = 'Продолжить <span class="arrow">→</span>';
  }
});

function renderMortgageSummary(payload, result) {
  document.getElementById('mortgage-summary').innerHTML = `
    <div class="s-item">
      <span class="s-label">Остаток долга</span>
      <span class="s-value">${rub(payload.loan_amount)}</span>
    </div>
    <div class="s-item">
      <span class="s-label">Ставка</span>
      <span class="s-value">${payload.annual_rate}% год.</span>
    </div>
    <div class="s-item">
      <span class="s-label">Платёж</span>
      <span class="s-value">${rub(result.monthly_payment)} / мес.</span>
    </div>
    <div class="s-item">
      <span class="s-label">Кол-во платежей</span>
      <span class="s-value">${result.payment_count}</span>
    </div>
    <div class="s-item">
      <span class="s-label">Всего процентов</span>
      <span class="s-value">${rub(result.total_interest)}</span>
    </div>
  `;
}

// ── Step 2: Deposit form ──────────────────────────────────────

document.getElementById('btn-back').addEventListener('click', () => goToStep(1));

document.getElementById('form-deposit').addEventListener('submit', async (e) => {
  e.preventDefault();
  clearError('deposit-error');

  const depositPayload = {
    amount:         document.getElementById('deposit_amount').value,
    annual_rate:    document.getElementById('deposit_rate').value,
    term_months:    document.getElementById('deposit_term').value,
    capitalization: document.getElementById('capitalization').checked ? 1 : 0,
  };

  const btn = e.target.querySelector('button[type=submit]');
  btn.disabled = true;
  btn.textContent = 'Сравниваем…';

  try {
    const depositResult = await postJSON('/api/deposit', depositPayload);
    state.depositId = depositResult.id;

    const comparison = await postJSON('/api/comparison', {
      mortgage_id: state.mortgageId,
      deposit_id:  state.depositId,
    });
    state.comparisonData = comparison;
    renderResults(comparison);
    goToStep(3);
  } catch (err) {
    showError('deposit-error', err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Сравнить →';
  }
});

// ── Step 3: Results ───────────────────────────────────────────

function renderResults(d) {
  renderWinnerBanner(d);
  renderCards(d);
  renderChartBalance(d.schedules);
  renderChartGain(d);
  renderScheduleTable('baseline', d.schedules);
  initTableTabs(d.schedules);
}

function renderWinnerBanner(d) {
  const labels = {
    deposit:        'Вклад → затем погасить',
    reduce_payment: 'Досрочное погашение',
  };
  const amounts = {
    deposit:        d.deposit_net_saving,
    reduce_payment: d.reduce_payment_interest_saved,
  };
  const isRepay = d.winner !== 'deposit';
  const banner  = document.getElementById('winner-banner');
  banner.className = `winner-banner ${isRepay ? 'repay' : 'deposit'}`;
  banner.innerHTML = `
    🏆 ${labels[d.winner]} выгоднее на <strong>${rub(amounts[d.winner])}</strong>
    <span class="banner-sub">по сравнению с базовым сценарием (ничего не делать)</span>
  `;
}

function renderCards(d) {
  // Deposit card
  document.getElementById('card-deposit-body').innerHTML = `
    <div class="metric">
      <div class="metric-label">Доход за срок вклада</div>
      <div class="metric-value positive">+${rub(d.deposit_income)}</div>
    </div>
    <div class="metric">
      <div class="metric-label">Итого для погашения через ${d.deposit_term_months} мес.</div>
      <div class="metric-value accent">${rub(d.deposit_final)}</div>
    </div>
    <div class="metric">
      <div class="metric-label">Новый платёж после погашения</div>
      <div class="metric-value purple">${rub(d.deposit_new_monthly)} / мес.</div>
    </div>
    <div class="metric">
      <div class="metric-label">Итоговая экономия на процентах</div>
      <div class="metric-value positive">+${rub(d.deposit_net_saving)}</div>
    </div>
  `;

  // Reduce payment card
  document.getElementById('card-reduce-payment-body').innerHTML = `
    <div class="metric">
      <div class="metric-label">Сэкономлено на процентах</div>
      <div class="metric-value positive">+${rub(d.reduce_payment_interest_saved)}</div>
    </div>
    <div class="metric">
      <div class="metric-label">Новый платёж</div>
      <div class="metric-value purple">${rub(d.reduce_payment_new_monthly)} / мес.</div>
    </div>
    <div class="metric">
      <div class="metric-label">Снижение платежа</div>
      <div class="metric-value" style="font-size:.95rem">
        −${rub(d.monthly_payment - d.reduce_payment_new_monthly)} / мес.
      </div>
    </div>
    <div class="metric">
      <div class="metric-label">Срок не меняется</div>
      <div class="metric-value" style="font-size:.95rem">Закрытие в тот же день</div>
    </div>
  `;

  // Mark winner
  const cardMap = {
    deposit:        'card-deposit',
    reduce_payment: 'card-reduce-payment',
  };
  document.getElementById(cardMap[d.winner]).classList.add('is-winner');
}

// ── Charts ────────────────────────────────────────────────────

/**
 * Build arrays of [date_label, cumulative_interest] for Chart.js.
 * To keep charts readable we sample every 12 months (yearly points).
 */
function buildBalanceSeries(schedule) {
  const labels  = [];
  const balance = [];
  schedule.forEach((row, i) => {
    if (i % 12 === 0 || i === schedule.length - 1) {
      labels.push(row.date.slice(6));   // year only
      balance.push(row.balance);
    }
  });
  return { labels, balance };
}

function renderChartBalance(schedules) {
  if (state.chartBalance) state.chartBalance.destroy();

  const base = buildBalanceSeries(schedules.baseline);
  const dep  = buildBalanceSeries(schedules.deposit);
  const rp   = buildBalanceSeries(schedules.reduce_payment);

  const ctx = document.getElementById('balance-chart').getContext('2d');
  state.chartBalance = new Chart(ctx, {
    type: 'line',
    data: {
      labels: base.labels,
      datasets: [
        {
          label: 'Без изменений',
          data: base.balance,
          borderColor: '#94A3B8',
          backgroundColor: 'rgba(148,163,184,.08)',
          borderWidth: 2,
          pointRadius: 0,
          fill: false,
        },
        {
          label: 'Вклад → погасить',
          data: dep.balance,
          borderColor: '#2563EB',
          backgroundColor: 'rgba(37,99,235,.08)',
          borderWidth: 2.5,
          pointRadius: 0,
          fill: false,
        },
        {
          label: 'Досрочно погасить',
          data: rp.balance,
          borderColor: '#7C3AED',
          backgroundColor: 'rgba(124,58,237,.08)',
          borderWidth: 2.5,
          pointRadius: 0,
          fill: false,
        },
      ],
    },
    options: {
      responsive: true,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { position: 'bottom' },
        tooltip: {
          callbacks: {
            label: ctx => ` ${ctx.dataset.label}: ${rub(ctx.parsed.y)}`,
          },
        },
      },
      scales: {
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

function renderChartGain(d) {
  if (state.chartGain) state.chartGain.destroy();

  const ctx = document.getElementById('gain-chart').getContext('2d');
  state.chartGain = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: ['Вклад → погасить', 'Досрочно погасить'],
      datasets: [{
        label: 'Экономия на процентах (₽)',
        data: [
          d.deposit_net_saving,
          d.reduce_payment_interest_saved,
        ],
        backgroundColor: [
          'rgba(37,99,235,.75)',
          'rgba(124,58,237,.75)',
        ],
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
    return `
    <tr>
      <td>${r.payment_num}</td>
      <td>${r.date}</td>
      <td>${paymentCell}</td>
      <td>${rub(r.principal)}</td>
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
  state.depositId      = null;
  state.mortgageResult = null;
  state.comparisonData = null;
  // Reset winner marks
  ['card-deposit','card-reduce-payment'].forEach(id => {
    document.getElementById(id).classList.remove('is-winner');
  });
  // Reset tabs
  document.querySelectorAll('.tab-btn').forEach((b, i) => {
    b.classList.toggle('active', i === 0);
  });
  document.getElementById('schedule-table-wrap').classList.remove('hidden');
  document.getElementById('btn-toggle-table').textContent = 'Скрыть таблицу ↑';
  goToStep(1);
});
