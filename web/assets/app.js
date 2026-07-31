/*
 * app.js — MaudeDash browser application
 * ======================================
 *
 * Cohort builder + 22 analysis panels over the FDA MAUDE corpus, all computed
 * client-side via DuckDB-WASM.
 *
 * Design decisions that differ from the desktop Streamlit edition, each fixing
 * a defect that edition has:
 *
 *   Lazy panels      Streamlit executes every one of its 22 tab bodies on every
 *                    interaction. Here only the ACTIVE panel queries, and its
 *                    result is memoised against the cohort signature, so
 *                    switching back is free.
 *   Bounded results  No panel issues an unbounded SELECT *. Row caps are
 *                    explicit and surfaced to the user when they bite.
 *   Honest axes      Rate charts scale to the data. The desktop edition pins
 *                    the outcome axis to 0-100%, rendering sub-1% harms — which
 *                    is most of them — as invisible slivers.
 *   Correct comparator
 *                    Disproportionality divides by reports that can actually
 *                    carry a problem code, not the whole corpus. See db.js.
 */

'use strict';

import * as DB from './db.js';
import * as S from './stats.js';

/* ============================== constants ============================== */

const EVENT_LABELS = { D: 'Death', IN: 'Injury', M: 'Malfunction', '*': 'Other' };

const OUTCOMES = [
  ['outcome_death', 'Death', 'var(--harm-death)'],
  ['outcome_life_threatening', 'Life-threatening', 'var(--harm-life)'],
  ['outcome_hospitalization', 'Hospitalization', 'var(--harm-hosp)'],
  ['outcome_disability', 'Disability', 'var(--harm-disability)'],
  ['outcome_congenital_anomaly', 'Congenital anomaly', 'var(--harm-congenital)'],
  ['outcome_required_intervention', 'Required intervention', 'var(--harm-intervention)'],
  ['outcome_other', 'Other', 'var(--harm-other)'],
  ['any_serious_outcome', 'Any serious (D/L/H/S/C/R)', 'var(--harm-serious)'],
];

const SOURCE_LABELS = {
  M: 'Manufacturer', U: 'User facility', D: 'Distributor', I: 'Importer',
  V: 'Voluntary', P: 'Patient', C: 'Consumer',
};

const OCCUPATION = {
  '001': 'Physician', '002': 'Nurse', '003': 'Non-healthcare professional',
  '0HP': 'Health professional', '0LP': 'Lay user / patient',
  '100': 'Other healthcare professional', '101': 'Audiologist',
  '102': 'Dental hygienist', '103': 'Dietician', '104': 'EMT',
  '105': 'Medical technologist', '106': 'Nuclear medicine technologist',
  '107': 'Occupational therapist', '108': 'Paramedic', '109': 'Pharmacist',
  '110': 'Phlebotomist', '111': 'Physical therapist',
  '112': 'Physician assistant', '113': 'Radiologic technologist',
  '114': 'Respiratory therapist', '115': 'Speech therapist',
  '116': 'Dentist', '117': 'Nurse practitioner',
};

const AGE_BAND = `CASE
  WHEN age_years_avg IS NULL THEN 'Unknown'
  WHEN age_years_avg < 1 THEN '<1 year'
  WHEN age_years_avg < 18 THEN '1-17'
  WHEN age_years_avg < 35 THEN '18-34'
  WHEN age_years_avg < 50 THEN '35-49'
  WHEN age_years_avg < 65 THEN '50-64'
  WHEN age_years_avg < 80 THEN '65-79'
  ELSE '80+' END`;

const PASSIVE_CAVEAT =
  'MAUDE is passive surveillance with no denominator of exposed devices. ' +
  'These are proportions of <em>reports</em>, not population incidence rates.';

/* Field labels so no raw snake_case or FDA code reaches the user. */
const COLUMN_LABELS = {
  MDR_REPORT_KEY: 'MDR key', REPORT_NUMBER: 'Report no.',
  EVENT_TYPE: 'Event type', DATE_PREF: 'Date', DATE_RECEIVED_D: 'Date received',
  DATE_OF_EVENT_D: 'Date of event', report_year: 'Year', lag_days: 'Lag (days)',
  manufacturer: 'Manufacturer', BRAND_NAME: 'Brand', GENERIC_NAME: 'Generic name',
  MODEL_NUMBER: 'Model', product_code: 'Product code',
  device_count: 'Devices', patient_count: 'Patients',
  any_serious_outcome: 'Serious outcome', outcome_codes_raw: 'Outcome codes',
  implant_flag: 'Implant', device_age_days: 'Device age (days)',
  age_years_avg: 'Mean age (y)', sex_list: 'Sex',
  narrative_desc: 'Event description', narrative_mfg: 'Manufacturer narrative',
  reporter_country_code: 'Country', SOURCE_TYPE: 'Source',
  REPORTER_OCCUPATION_CODE: 'Reporter', TEXT_TYPE_CODE: 'Text type',
  FOI_TEXT: 'Narrative text',
};

/* ============================== app state ============================== */

const state = {
  filters: null,        // applied cohort (null = nothing run yet)
  draft: {},            // sidebar contents
  activeTab: 'preview',
  cache: new Map(),     // `${tabId}::${signature}` -> rendered marker
  years: { min: 1991, max: 2024 },
  summary: null,
};

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
};
const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* ============================== cohort SQL ============================= */

/**
 * Build the WHERE clause and bound parameters for the current cohort.
 * Every user-supplied value is bound, never interpolated.
 */
function buildWhere(f, extra = {}) {
  const ex = {
    excludeForwarded: f.excludeForwarded, excludeRwd: f.excludeRwd,
    initialOnly: f.initialOnly, implantOnly: f.implantOnly,
    seriousOnly: f.seriousOnly, ...extra,
  };

  if (f.mdrKey) return { sql: 'MDR_REPORT_KEY = ?', params: [f.mdrKey] };

  const parts = ['report_year BETWEEN ? AND ?'];
  const params = [f.yearLo, f.yearHi];

  if (f.productCode) { parts.push('product_code = ?'); params.push(f.productCode.toUpperCase()); }
  if (f.manufacturer) { parts.push('lower(manufacturer) LIKE ?'); params.push(`%${f.manufacturer.toLowerCase()}%`); }
  if (f.deviceTerms.length) {
    const sub = f.deviceTerms.map(() =>
      '(lower(BRAND_NAME) LIKE ? OR lower(GENERIC_NAME) LIKE ? OR lower(MODEL_NUMBER) LIKE ?)');
    for (const t of f.deviceTerms) { const p = `%${t.toLowerCase()}%`; params.push(p, p, p); }
    parts.push(`(${sub.join(' OR ')})`);
  }
  if (f.narrative) { parts.push('lower(narrative_desc) LIKE ?'); params.push(`%${f.narrative.toLowerCase()}%`); }
  if (f.eventTypes.length && f.eventTypes.length < 3) {
    parts.push(`EVENT_TYPE IN (${f.eventTypes.map(() => '?').join(',')})`);
    params.push(...f.eventTypes);
  }
  if (ex.excludeForwarded) parts.push('COALESCE(IS_FORWARDED_803_22_B2, FALSE) = FALSE');
  if (ex.excludeRwd) parts.push('COALESCE(IS_RWD_SOURCED, FALSE) = FALSE');
  if (ex.initialOnly) parts.push('COALESCE(initial_report, TRUE) = TRUE');
  if (ex.implantOnly) parts.push('COALESCE(implant_flag, FALSE) = TRUE');
  if (ex.seriousOnly) parts.push('COALESCE(any_serious_outcome, FALSE) = TRUE');

  return { sql: parts.join(' AND '), params };
}

/** The read_parquet() source, pruned to the cohort's years. */
function src(f) {
  return f.mdrKey ? 'mdr_all' : DB.mdrSource(f.yearLo, f.yearHi);
}

/** Stable key for memoising panel renders. */
function signature(f) { return JSON.stringify(f); }

/* ============================== UI plumbing =========================== */

/*
 * Progress reporting.
 *
 * Most panels answer in well under a second, but a few genuinely take longer —
 * a narrative scan, or a disproportionality screen on a cohort with no product
 * code to prune on. A bare spinner for 30 seconds is indistinguishable from a
 * hung page, which is the single worst thing this interface can do. So the
 * indicator states what is happening and, once work passes a couple of
 * seconds, how long it has been running.
 */
let busyDepth = 0;
let busyTimer = null;
let busyStart = 0;
let busyLabel = '';

function setBusyText() {
  const secs = (performance.now() - busyStart) / 1000;
  $('#busy-msg').textContent = secs > 2
    ? `${busyLabel} · ${secs.toFixed(0)}s`
    : busyLabel;
}

function busy(on, msg = 'Working…') {
  busyDepth = Math.max(0, busyDepth + (on ? 1 : -1));
  const active = busyDepth > 0;
  $('#busy').classList.toggle('on', active);
  if (on) {
    busyLabel = msg;
    if (busyDepth === 1) busyStart = performance.now();
    setBusyText();
    if (!busyTimer) busyTimer = setInterval(setBusyText, 500);
  }
  if (!active && busyTimer) {
    clearInterval(busyTimer);
    busyTimer = null;
  }
}

async function withBusy(msg, fn) {
  busy(true, msg);
  try { return await fn(); } finally { busy(false); }
}

/** Update the in-panel status line without disturbing the global indicator. */
function stage(host, text, detail = '') {
  const n = host.querySelector('.loading');
  if (!n) return;
  n.innerHTML = `<span class="spinner"></span><div><div>${esc(text)}</div>` +
    (detail ? `<div class="loading-detail">${detail}</div>` : '') + '</div>';
}

function note(kind, icon, html) {
  return `<div class="note note-${kind}"><span class="note-icon">${icon}</span><div>${html}</div></div>`;
}

function emptyState(title, body, icon = '◎') {
  return `<div class="empty"><div class="empty-icon">${icon}</div>
    <h3>${esc(title)}</h3><p>${body}</p></div>`;
}

/* ------------------------------- tables ------------------------------- */

const NARRATIVE_COLS = new Set(['narrative_desc', 'narrative_mfg', 'FOI_TEXT']);

function renderTable(rows, { columns = null, maxHeight = 560 } = {}) {
  if (!rows || !rows.length) return emptyState('No rows', 'This view has no data for the current cohort.');
  const cols = columns || Object.keys(rows[0]);
  const head = cols.map((c) => {
    const numeric = typeof rows[0][c] === 'number';
    return `<th class="${numeric ? 'right' : ''}">${esc(COLUMN_LABELS[c] || humanize(c))}</th>`;
  }).join('');
  const body = rows.map((r) => '<tr>' + cols.map((c) => {
    let v = r[c];
    let cls = '';
    if (NARRATIVE_COLS.has(c)) cls = 'narrative';
    else if (typeof v === 'number') { cls = 'right'; v = formatCell(c, v); }
    else if (c === 'MDR_REPORT_KEY' || c === 'REPORT_NUMBER') cls = 'mono';
    else v = formatCell(c, v);
    return `<td class="${cls}">${esc(v ?? '')}</td>`;
  }).join('') + '</tr>').join('');
  return `<div class="table-wrap" style="max-height:${maxHeight}px">
    <table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function humanize(c) {
  return c.replace(/_/g, ' ').replace(/\b\w/g, (m) => m.toUpperCase());
}

/**
 * Render a table whose columns can be re-sorted by clicking the header.
 *
 * `columns` entries are {key, label, get(row)->html, sort(row)->comparable,
 * right?, cls?}. Sorting is done on the raw comparable rather than the rendered
 * string, so "PRR (95% CI)" sorts by the point estimate and not alphabetically
 * — the trap that makes most naive sortable tables useless for statistics.
 * Nulls and NaNs always sink to the bottom regardless of direction.
 */
function sortableTable(host, rows, columns, initial, maxHeight = 520) {
  let sortKey = initial?.key ?? columns[0].key;
  let sortDir = initial?.dir ?? 1;

  const draw = () => {
    const col = columns.find((c) => c.key === sortKey) || columns[0];
    const sorted = [...rows].sort((x, y) => {
      const a = col.sort(x), b = col.sort(y);
      const aBad = a === null || a === undefined || (typeof a === 'number' && !Number.isFinite(a));
      const bBad = b === null || b === undefined || (typeof b === 'number' && !Number.isFinite(b));
      if (aBad && bBad) return 0;
      if (aBad) return 1;          // missing values sink, both directions
      if (bBad) return -1;
      if (typeof a === 'string' || typeof b === 'string') {
        return String(a).localeCompare(String(b)) * sortDir;
      }
      return (a - b) * sortDir;
    });

    const head = columns.map((c) => {
      const active = c.key === sortKey;
      const arrow = active ? (sortDir === 1 ? ' ▲' : ' ▼') : '';
      return `<th class="${c.right ? 'right' : ''} sortable${active ? ' sorted' : ''}"
                  data-sort="${c.key}" role="button" tabindex="0"
                  aria-sort="${active ? (sortDir === 1 ? 'ascending' : 'descending') : 'none'}"
                  title="Sort by ${esc(c.label)}">${esc(c.label)}${arrow}</th>`;
    }).join('');

    const body = sorted.map((r) => '<tr>' + columns.map((c) =>
      `<td class="${c.right ? 'right' : ''} ${c.cls || ''}">${c.get(r)}</td>`).join('') + '</tr>').join('');

    host.innerHTML = `<div class="table-wrap" style="max-height:${maxHeight}px">
      <table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;

    host.querySelectorAll('th.sortable').forEach((th) => {
      const activate = () => {
        const k = th.dataset.sort;
        if (k === sortKey) sortDir = -sortDir;
        else { sortKey = k; sortDir = (columns.find((c) => c.key === k)?.right) ? -1 : 1; }
        draw();
      };
      th.onclick = activate;
      th.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate(); } };
    });
  };
  draw();
}

function formatCell(col, v) {
  if (v === null || v === undefined) return '';
  if (col === 'EVENT_TYPE') return EVENT_LABELS[v] || v;
  if (col === 'REPORTER_OCCUPATION_CODE') return OCCUPATION[String(v).toUpperCase()] || v;
  if (col === 'SOURCE_TYPE') return SOURCE_LABELS[v] || v;
  if (typeof v === 'boolean') return v ? 'Yes' : 'No';
  if (typeof v === 'number') {
    return Number.isInteger(v) ? v.toLocaleString('en-US')
      : v.toLocaleString('en-US', { maximumFractionDigits: 2 });
  }
  return v;
}

/* ------------------------------- charts ------------------------------- */

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** Plotly layout in MaudeDash's visual language, theme-aware. */
function layout(title, opts = {}) {
  const text = cssVar('--text-2') || '#4A5C6C';
  const grid = cssVar('--border') || '#DCE3EA';
  return Object.assign({
    title: title ? { text: title, font: { size: 13.5, weight: 600 }, x: 0, xanchor: 'left' } : undefined,
    paper_bgcolor: 'rgba(0,0,0,0)',
    plot_bgcolor: 'rgba(0,0,0,0)',
    font: { family: cssVar('--sans') || 'system-ui', size: 12, color: text },
    margin: { l: 60, r: 20, t: title ? 40 : 14, b: 44 },
    height: 360,
    xaxis: { gridcolor: grid, zerolinecolor: grid, automargin: true },
    yaxis: { gridcolor: grid, zerolinecolor: grid, automargin: true },
    legend: { orientation: 'h', y: -0.18, font: { size: 11 } },
    hoverlabel: { font: { size: 12 } },
    colorway: [cssVar('--brand-600'), cssVar('--accent-500'), cssVar('--harm-hosp'),
               cssVar('--harm-disability'), cssVar('--harm-life'),
               cssVar('--harm-congenital'), cssVar('--harm-other')],
  }, opts);
}

const PLOT_CONFIG = {
  displayModeBar: true,
  displaylogo: false,
  responsive: true,
  modeBarButtonsToRemove: ['lasso2d', 'select2d', 'autoScale2d'],
  toImageButtonOptions: { format: 'png', filename: 'maudedash', scale: 2 },
};

function plot(node, traces, lay) {
  Plotly.newPlot(node, traces, lay, PLOT_CONFIG);
}

/* ------------------------------- exports ------------------------------ */

function toCsv(rows, cols = null) {
  if (!rows.length) return '';
  const c = cols || Object.keys(rows[0]);
  const cell = (v) => {
    if (v === null || v === undefined) return '';
    const s = String(v);
    return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return [c.join(','), ...rows.map((r) => c.map((k) => cell(r[k])).join(','))].join('\r\n');
}

function download(name, text, mime = 'text/csv;charset=utf-8') {
  const blob = new Blob([`﻿${text}`], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = Object.assign(document.createElement('a'), { href: url, download: name });
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function stamp() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}

/** Attach a "Download CSV" control to a card. */
function exportBtn(container, rows, base, cols = null) {
  if (!rows || !rows.length) return;
  const b = el('button', 'btn btn-secondary', 'Download CSV');
  b.style.cssText = 'width:auto;margin-top:10px;font-size:12px;padding:5px 11px';
  b.onclick = () => download(`maudedash_${base}_${stamp()}.csv`, toCsv(rows, cols));
  container.appendChild(b);
}

/* ============================== panels ================================ */

const TAB_GROUPS = [
  ['Overview', ['preview', 'yearly', 'events']],
  ['Clinical', ['outcomes', 'demographics', 'deviceage', 'deaths']],
  ['Signals', ['problems', 'dispro']],
  ['Inference', ['subgroup', 'trends', 'compare', 'sensitivity']],
  ['Context', ['reporter', 'geography', 'manufacturers', 'lag']],
  ['Narrative', ['textmining', 'rawtext']],
  ['Report', ['quality', 'methods', 'export']],
];

const PANELS = {};

/**
 * Register an analysis panel.
 *
 * `cost` is optional and returns {what, detail} describing what this panel is
 * about to do for THIS cohort, shown while it runs. It exists because the
 * expensive panels are expensive for reasons the user can act on — naming a
 * product code makes the problem-code panels prune instead of scanning — and a
 * generic spinner communicates none of that.
 */
function panel(id, label, desc, render, cost) {
  PANELS[id] = { id, label, desc, render, cost };
}

/* ----------------------------- 1. Preview ----------------------------- */

panel('preview', 'Reports', 'The individual MDR submissions matching your cohort.',
async (f, host) => {
  const w = buildWhere(f);
  const cols = ['MDR_REPORT_KEY', 'REPORT_NUMBER', 'DATE_PREF', 'EVENT_TYPE',
    'manufacturer', 'BRAND_NAME', 'GENERIC_NAME', 'MODEL_NUMBER', 'product_code',
    'device_count', 'patient_count', 'any_serious_outcome', 'outcome_codes_raw',
    'implant_flag'];
  const rows = await DB.query(
    `SELECT ${cols.join(', ')} FROM ${src(f)} WHERE ${w.sql}
     ORDER BY DATE_PREF DESC NULLS LAST, MDR_REPORT_KEY LIMIT ${f.limit}`, w.params);

  const card = el('div', 'card');
  card.innerHTML = `<div class="card-title">Matching reports
    <span class="count">showing ${S.fmtInt(rows.length)}${rows.length >= f.limit
      ? ` of ${S.fmtInt(f.total)} — raise the row cap to see more` : ''}</span></div>`
    + renderTable(rows, { columns: cols });
  exportBtn(card, rows, 'reports', cols);
  host.appendChild(card);
});

/* --------------------------- 2. Yearly volume -------------------------- */

panel('yearly', 'Yearly volume', 'Report counts per year for this cohort.',
async (f, host) => {
  const w = buildWhere(f);
  const rows = await DB.query(
    `SELECT report_year AS year, COUNT(*) AS reports
     FROM ${src(f)} WHERE ${w.sql} AND report_year IS NOT NULL
     GROUP BY 1 ORDER BY 1`, w.params);
  if (!rows.length) return host.insertAdjacentHTML('beforeend',
    emptyState('No dated reports', 'No report in this cohort carries a usable date.'));

  const card = el('div', 'card');
  card.innerHTML = '<div class="card-title">Reports per year</div>';
  const div = el('div', 'chart');
  card.appendChild(div);
  host.appendChild(card);
  plot(div, [{
    type: 'bar', x: rows.map((r) => r.year), y: rows.map((r) => r.reports),
    marker: { color: cssVar('--brand-600') },
    hovertemplate: '%{x}<br>%{y:,} reports<extra></extra>',
  }], layout('', { yaxis: { title: 'Reports', gridcolor: cssVar('--border') },
                   xaxis: { title: 'Report year', dtick: 1 } }));
  exportBtn(card, rows, 'yearly_volume');
  maybePartialYearNote(card, rows);
});

/** The most recent year is usually a partial download; say so rather than
 *  letting a reader mistake the drop-off for a real decline. */
function maybePartialYearNote(card, rows) {
  if (rows.length < 3) return;
  const last = rows[rows.length - 1], prev = rows[rows.length - 2];
  if (last.reports < prev.reports * 0.75) {
    card.insertAdjacentHTML('beforeend', note('warn', '▲',
      `<strong>${last.year} looks incomplete.</strong> The FDA files were captured
       mid-cycle, so the final year is usually partial. Exclude it from
       rate comparisons and trend tests.`));
  }
}

/* ---------------------------- 3. Event trends -------------------------- */

panel('events', 'Event types', 'Death, injury and malfunction reports over time.',
async (f, host) => {
  const w = buildWhere(f);
  const rows = await DB.query(
    `SELECT report_year AS year, EVENT_TYPE, COUNT(*) AS reports
     FROM ${src(f)} WHERE ${w.sql} AND report_year IS NOT NULL
       AND EVENT_TYPE IN ('D','IN','M')
     GROUP BY 1,2 ORDER BY 1,2`, w.params);
  if (!rows.length) return host.insertAdjacentHTML('beforeend',
    emptyState('No classified events', 'No report carries a death/injury/malfunction classification.'));

  const card = el('div', 'card');
  card.innerHTML = '<div class="card-title">Event type by year</div>';
  const div = el('div', 'chart'); card.appendChild(div); host.appendChild(card);

  const colors = { D: cssVar('--harm-death'), IN: cssVar('--harm-hosp'), M: cssVar('--brand-600') };
  const traces = ['D', 'IN', 'M'].map((t) => {
    const sub = rows.filter((r) => r.EVENT_TYPE === t);
    return {
      type: 'scatter', mode: 'lines+markers', name: EVENT_LABELS[t],
      x: sub.map((r) => r.year), y: sub.map((r) => r.reports),
      line: { color: colors[t], width: 2.2 }, marker: { size: 6 },
      hovertemplate: `${EVENT_LABELS[t]}<br>%{x}: %{y:,}<extra></extra>`,
    };
  }).filter((t) => t.x.length);
  plot(div, traces, layout('', { yaxis: { title: 'Reports' },
                                 xaxis: { title: 'Report year', dtick: 1 } }));
  exportBtn(card, rows, 'event_trends');
});

/* -------------------------- 4. Clinical outcomes ----------------------- */

panel('outcomes', 'Clinical outcomes',
  'The FDA\'s own patient-outcome categories per 21 CFR 803.3, with Wilson 95% confidence intervals. A report may carry more than one outcome.',
async (f, host) => {
  const w = buildWhere(f);
  const sums = OUTCOMES.map(([c]) => `SUM(CASE WHEN ${c} THEN 1 ELSE 0 END) AS ${c}`).join(', ');
  const r = await DB.queryRow(
    `SELECT COUNT(*) AS n, ${sums} FROM ${src(f)} WHERE ${w.sql}`, w.params);
  const n = Number(r?.n || 0);
  if (!n) return host.insertAdjacentHTML('beforeend',
    emptyState('Empty cohort', 'No reports match these filters.'));

  const data = OUTCOMES.map(([col, label, colour]) => {
    const k = Number(r[col] || 0);
    const ci = S.wilsonCI(k, n);
    return { label, colour, k, n, ci };
  });

  const card = el('div', 'card');
  card.innerHTML = `<div class="card-title">Outcome rates
    <span class="count">${S.fmtInt(n)} reports</span></div>`;
  const div = el('div', 'chart'); card.appendChild(div); host.appendChild(card);

  // Scale the axis to the data. Pinning it to 0-100% (as the desktop edition
  // does) makes sub-1% harms — most of them — invisible.
  const maxHi = Math.max(...data.map((d) => d.ci.hi * 100));
  const axisMax = Math.min(100, Math.max(maxHi * 1.15, 0.5));

  plot(div, [{
    type: 'bar', orientation: 'h',
    y: data.map((d) => d.label), x: data.map((d) => d.ci.p * 100),
    marker: { color: data.map((d) => cssVar(d.colour.slice(4, -1)) || d.colour) },
    error_x: {
      type: 'data', symmetric: false,
      array: data.map((d) => (d.ci.hi - d.ci.p) * 100),
      arrayminus: data.map((d) => (d.ci.p - d.ci.lo) * 100),
      color: cssVar('--text-3'), thickness: 1.3, width: 4,
    },
    customdata: data.map((d) => [S.fmtInt(d.k), S.fmtCI(d.ci)]),
    hovertemplate: '%{y}<br>%{customdata[0]} of ' + S.fmtInt(n) +
                   '<br>%{customdata[1]}<extra></extra>',
  }], layout('', {
    height: 400,
    xaxis: { title: 'Rate (% of reports), Wilson 95% CI', range: [0, axisMax] },
    yaxis: { automargin: true },
    margin: { l: 170, r: 24, t: 12, b: 48 },
  }));

  const tableRows = data.map((d) => ({
    Outcome: d.label, n: d.k, Total: n,
    'Rate (95% CI)': S.fmtCI(d.ci),
  }));
  card.insertAdjacentHTML('beforeend', renderTable(tableRows, { maxHeight: 340 }));
  card.insertAdjacentHTML('beforeend', note('caveat', '!',
    `${PASSIVE_CAVEAT} Outcome coding is voluntary — reports without an outcome
     code are not necessarily benign. Check the Data quality panel for coverage.`));
  exportBtn(card, tableRows, 'clinical_outcomes');
});

/* --------------------------- 5. Demographics --------------------------- */

panel('demographics', 'Demographics', 'Patient age and sex, where reported.',
async (f, host) => {
  const w = buildWhere(f);
  const [ages, sexes] = await Promise.all([
    DB.query(`SELECT age_years_avg AS age FROM ${src(f)} WHERE ${w.sql}
              AND age_years_avg IS NOT NULL AND age_years_avg BETWEEN 0 AND 110
              LIMIT 400000`, w.params),
    DB.query(`SELECT upper(left(coalesce(sex_list,'U'),1)) AS sex, COUNT(*) AS n
              FROM ${src(f)} WHERE ${w.sql} GROUP BY 1 ORDER BY n DESC`, w.params),
  ]);

  const grid = el('div', 'grid-2'); host.appendChild(grid);

  const c1 = el('div', 'card');
  c1.innerHTML = `<div class="card-title">Patient age
    <span class="count">${S.fmtInt(ages.length)} reports with age</span></div>`;
  const d1 = el('div', 'chart'); c1.appendChild(d1); grid.appendChild(c1);
  if (ages.length) {
    plot(d1, [{ type: 'histogram', x: ages.map((r) => r.age), nbinsx: 24,
      marker: { color: cssVar('--brand-600') },
      hovertemplate: 'Age %{x}<br>%{y:,} reports<extra></extra>' }],
      layout('', { xaxis: { title: 'Age at event (years)' }, yaxis: { title: 'Reports' } }));
  } else {
    d1.innerHTML = emptyState('No age data', 'Patient age is unreported for this cohort.');
  }

  const c2 = el('div', 'card');
  c2.innerHTML = '<div class="card-title">Patient sex</div>';
  const d2 = el('div', 'chart'); c2.appendChild(d2); grid.appendChild(c2);
  const named = { F: 'Female', M: 'Male' };
  const agg = new Map();
  for (const r of sexes) {
    const k = named[r.sex] || 'Unknown / not reported';
    agg.set(k, (agg.get(k) || 0) + Number(r.n));
  }
  const labels = [...agg.keys()], values = [...agg.values()];
  plot(d2, [{
    type: 'pie', labels, values, hole: 0.42,
    marker: { colors: [cssVar('--brand-600'), cssVar('--accent-500'), cssVar('--harm-other')] },
    textinfo: 'label+percent', hovertemplate: '%{label}<br>%{value:,}<extra></extra>',
  }], layout('', { showlegend: false }));

  const summary = labels.map((l, i) => ({ Sex: l, Reports: values[i] }));
  exportBtn(c2, summary, 'sex_distribution');
});

/* ---------------------------- 6. Device age ---------------------------- */

panel('deviceage', 'Device age', 'Time from manufacture to the reported failure.',
async (f, host) => {
  const w = buildWhere(f);
  const rows = await DB.query(
    `SELECT device_age_days / 365.25 AS age_years FROM ${src(f)}
     WHERE ${w.sql} AND device_age_days IS NOT NULL
       AND device_age_days BETWEEN 0 AND 36525 LIMIT 300000`, w.params);
  if (!rows.length) return host.insertAdjacentHTML('beforeend',
    emptyState('No device-age data',
      'MAUDE\'s DEVICE_AGE_TEXT field is frequently unpopulated, and it is absent entirely before 2015.'));

  const vals = rows.map((r) => r.age_years).sort((a, b) => a - b);
  const q = (p) => vals[Math.min(vals.length - 1, Math.floor(p * (vals.length - 1)))];

  const grid = el('div', 'grid-2'); host.appendChild(grid);
  const c1 = el('div', 'card');
  c1.innerHTML = `<div class="card-title">Age at failure
    <span class="count">${S.fmtInt(vals.length)} with data</span></div>`;
  const d1 = el('div', 'chart'); c1.appendChild(d1); grid.appendChild(c1);
  plot(d1, [{ type: 'histogram', x: vals, nbinsx: 40,
    marker: { color: cssVar('--brand-600') },
    hovertemplate: '%{x:.1f} y<br>%{y:,}<extra></extra>' }],
    layout('', { xaxis: { title: 'Device age (years)' }, yaxis: { title: 'Reports' } }));

  const c2 = el('div', 'card');
  c2.innerHTML = '<div class="card-title">Cumulative distribution</div>';
  const d2 = el('div', 'chart'); c2.appendChild(d2); grid.appendChild(c2);
  const step = Math.max(1, Math.floor(vals.length / 2000));
  const cx = [], cy = [];
  for (let i = 0; i < vals.length; i += step) { cx.push(vals[i]); cy.push(((i + 1) / vals.length) * 100); }
  plot(d2, [{ type: 'scatter', mode: 'lines', x: cx, y: cy,
    line: { color: cssVar('--accent-500'), width: 2.2 },
    hovertemplate: '%{x:.1f} y<br>%{y:.1f}%<extra></extra>' }],
    layout('', { xaxis: { title: 'Device age (years)' },
                 yaxis: { title: 'Cumulative % of failures', range: [0, 100] } }));

  const stats = [{
    'Reports with age': vals.length,
    'Median (y)': +q(0.5).toFixed(2),
    'IQR (y)': `${q(0.25).toFixed(2)}–${q(0.75).toFixed(2)}`,
    'Mean (y)': +(vals.reduce((a, b) => a + b, 0) / vals.length).toFixed(2),
    'P90 (y)': +q(0.9).toFixed(2),
  }];
  const c3 = el('div', 'card');
  c3.innerHTML = '<div class="card-title">Summary</div>' + renderTable(stats, { maxHeight: 140 });
  host.appendChild(c3);
});

/* ---------------------------- 7. Death deep-dive ----------------------- */

panel('deaths', 'Deaths', 'Reports classified as a death, or carrying the FDA death outcome code.',
async (f, host) => {
  const w = buildWhere(f);
  const clause = "(EVENT_TYPE = 'D' OR COALESCE(outcome_death, FALSE) = TRUE)";
  const total = Number(await DB.queryValue(
    `SELECT COUNT(*) FROM ${src(f)} WHERE ${w.sql} AND ${clause}`, w.params));
  if (!total) return host.insertAdjacentHTML('beforeend',
    emptyState('No death reports', 'No report in this cohort is classified as a death.'));

  const cols = ['MDR_REPORT_KEY', 'DATE_PREF', 'manufacturer', 'BRAND_NAME',
    'GENERIC_NAME', 'product_code', 'outcome_codes_raw', 'narrative_desc'];
  const rows = await DB.query(
    `SELECT ${cols.join(', ')} FROM ${src(f)} WHERE ${w.sql} AND ${clause}
     ORDER BY DATE_PREF DESC NULLS LAST LIMIT ${f.limit}`, w.params);

  const card = el('div', 'card');
  card.innerHTML = `<div class="card-title">Death reports
    <span class="count">${S.fmtInt(total)} total, showing ${S.fmtInt(rows.length)}</span></div>`
    + renderTable(rows, { columns: cols });
  card.insertAdjacentHTML('beforeend', note('caveat', '!',
    `A death report records that a death occurred and a device was involved. It is
     <strong>not</strong> an FDA determination that the device caused the death.`));
  exportBtn(card, rows, 'deaths', cols);
  host.appendChild(card);
});

/* --------------------------- 8. Problem codes -------------------------- */

panel('problems', 'Problem codes', 'The FDA device- and patient-problem terms most often coded in this cohort.',
async (f, host) => {
  const w = buildWhere(f);
  const grid = el('div', 'grid-2'); host.appendChild(grid);

  for (const [kind, bridge, dict, title] of [
    ['device', 'devprob', 'dict_device', 'Device problems'],
    ['patient', 'patprob', 'dict_patient', 'Patient problems'],
  ]) {
    // Same pushdown as the disproportionality panel — see the comment there.
    const push = ['b.y BETWEEN ? AND ?'];
    const pushParams = [f.yearLo, f.yearHi];
    if (f.productCode) {
      push.push('b.pc = ?');
      pushParams.push(f.productCode.toUpperCase());
    }
    const rows = await DB.query(
      `WITH keys AS (SELECT TRY_CAST(MDR_REPORT_KEY AS UINTEGER) AS k
                     FROM ${src(f)} WHERE ${w.sql})
       SELECT COALESCE(d.TERM, b.code) AS term, b.code AS code, COUNT(*) AS n
       FROM ${bridge} b JOIN keys USING (k)
       LEFT JOIN ${dict} d ON d.FDA_CODE = b.code
       WHERE ${push.join(' AND ')}
       GROUP BY 1,2 ORDER BY n DESC LIMIT 50`,
      [...w.params, ...pushParams]);

    const card = el('div', 'card');
    card.innerHTML = `<div class="card-title">${title}
      <span class="count">top ${Math.min(20, rows.length)} of ${rows.length}</span></div>`;
    grid.appendChild(card);
    if (!rows.length) {
      card.insertAdjacentHTML('beforeend',
        emptyState('No coded problems', 'No report in this cohort carries a coded problem.'));
      continue;
    }
    const div = el('div', 'chart'); card.appendChild(div);
    const top = rows.slice(0, 20).reverse();
    plot(div, [{
      type: 'bar', orientation: 'h',
      y: top.map((r) => truncate(r.term, 44)), x: top.map((r) => r.n),
      marker: { color: kind === 'device' ? cssVar('--brand-600') : cssVar('--accent-500') },
      hovertemplate: '%{y}<br>%{x:,} reports<extra></extra>',
    }], layout('', { height: 460, margin: { l: 210, r: 20, t: 10, b: 40 },
                     xaxis: { title: 'Reports' } }));
    exportBtn(card, rows, `${kind}_problems`);
  }
}, (f) => f.productCode
  ? { what: 'Counting coded device and patient problems…', detail: '' }
  : { what: 'Counting coded device and patient problems…',
      detail: 'No product code set, so the whole problem-code index for ' +
              `${f.yearLo}–${f.yearHi} is scanned.` });

function truncate(s, n) {
  s = String(s ?? '');
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

/* ------------------------ 9. Disproportionality ------------------------ */

panel('dispro', 'Disproportionality',
  'Screens each problem code for over-representation in your cohort relative to the rest of the corpus. Exploratory signal detection only.',
async (f, host) => {
  const w = buildWhere(f);
  const which = state.draft.disproKind || 'device';
  const [bridge, dict, global] = which === 'device'
    ? ['devprob', 'dict_device', 'agg_dev_problems_global']
    : ['patprob', 'dict_patient', 'agg_pat_problems_global'];

  // Toggle
  const bar = el('div', 'card');
  bar.innerHTML = `<div class="card-title">Compute against</div>
    <div class="checks" style="flex-direction:row;gap:18px">
      <label class="check"><input type="radio" name="dk" value="device"
        ${which === 'device' ? 'checked' : ''}><span>Device problems</span></label>
      <label class="check"><input type="radio" name="dk" value="patient"
        ${which === 'patient' ? 'checked' : ''}><span>Patient problems</span></label>
    </div>`;
  bar.querySelectorAll('input[name=dk]').forEach((r) => {
    r.onchange = () => {
      state.draft.disproKind = r.value;
      state.cache.delete(`dispro::${signature(state.filters)}`);
      renderActive(true);
    };
  });
  host.appendChild(bar);

  /*
   * The comparator is the set of reports that CAN carry a problem code.
   * The FDA problem-code files only cover 2015+, so ~20% of the corpus is
   * structurally code-free; including those rows in cell d inflates every
   * PRR by ~25% and manufactures EMA signals.
   */
  // Independent of one another, so issue them together rather than in series.
  const [eligible, cohortN] = await Promise.all([
    DB.codeEligibleTotal(),
    DB.queryValue(
      `SELECT COUNT(*) FROM ${src(f)} WHERE ${w.sql}
        AND (report_year IS NULL OR report_year >= ${DB.CODE_ELIGIBLE_FROM_YEAR})`,
      w.params).then(Number),
  ]);

  if (cohortN < 3) {
    return host.insertAdjacentHTML('beforeend', emptyState('Cohort too small',
      'Disproportionality needs at least three code-eligible reports (2015 onward).'));
  }

  /*
   * Pushdown. The bridge carries product_code and report_year and is sorted by
   * product_code, so naming the cohort's product code lets Parquet row-group
   * statistics skip essentially the whole file: a KWP query reads 0.31 MB of a
   * 58 MB file rather than all of it. This is what took the panel from 34
   * seconds to about a second.
   *
   * COUNT(*) rather than COUNT(DISTINCT b.k) is safe because (key, code) pairs
   * are unique in the source — verified, zero duplicates across 20,042,625
   * rows — and it avoids a distinct aggregation over 20M keys.
   *
   * Without a product code (a manufacturer- or narrative-defined cohort) only
   * the year range can be pushed down, so the scan is wider and slower; the UI
   * says so rather than leaving the user guessing.
   */
  const pushdown = ['b.y BETWEEN ? AND ?'];
  const pushParams = [f.yearLo, f.yearHi];
  if (f.productCode) {
    pushdown.push('b.pc = ?');
    pushParams.push(f.productCode.toUpperCase());
  }

  const rows = await DB.query(
    `WITH keys AS (
        SELECT TRY_CAST(MDR_REPORT_KEY AS UINTEGER) AS k
        FROM ${src(f)} WHERE ${w.sql}
         AND (report_year IS NULL OR report_year >= ${DB.CODE_ELIGIBLE_FROM_YEAR})
     ),
     inside AS (
        SELECT b.code, COUNT(*) AS a
        FROM ${bridge} b JOIN keys USING (k)
        WHERE ${pushdown.join(' AND ')}
        GROUP BY 1 HAVING a >= 3
     )
     SELECT COALESCE(d.TERM, i.code) AS term, i.code AS code,
            i.a AS a, g.n AS global_n
     FROM inside i JOIN ${global} g USING (code)
     LEFT JOIN ${dict} d ON d.FDA_CODE = i.code`,
    [...w.params, ...pushParams]);

  if (!rows.length) {
    return host.insertAdjacentHTML('beforeend', emptyState('No eligible codes',
      'No problem code reaches the minimum of three reports in this cohort.'));
  }

  const results = rows.map((r) => {
    const a = Number(r.a);
    const b = cohortN - a;                       // cohort, no code
    const c = Number(r.global_n) - a;            // comparator, has code
    const d = eligible - Number(r.global_n) - cohortN + a;  // comparator, no code
    const res = S.analyze2x2(a, b, c, d);
    const ic = S.informationComponent(a, b, c, d);
    return {
      term: r.term, code: r.code, a, b, c, d, res, ic,
      ema: S.emaSignal(a, res.prr.point, res.chi2),
    };
  });

  /*
   * Multiplicity. Screening hundreds of codes at alpha = 0.05 yields dozens of
   * spurious hits by construction; earlier versions applied no correction at
   * all. Fisher's p is preferred where computed (small cells), otherwise the
   * Yates chi-square p.
   */
  const rawP = results.map((r) =>
    (r.res.fisherP !== null && Number.isFinite(r.res.fisherP)) ? r.res.fisherP
      : (Number.isFinite(r.res.chi2P) ? r.res.chi2P : null));
  const qVals = S.benjaminiHochberg(rawP);
  results.forEach((r, i) => { r.p = rawP[i]; r.q = qVals[i]; });

  const nEma = results.filter((r) => r.ema).length;
  const nIc = results.filter((r) => r.ic.signal).length;
  const nBoth = results.filter((r) => r.ema && r.ic.signal).length;
  const nQ = results.filter((r) => r.q !== null && r.q < 0.05).length;

  const card = el('div', 'card');
  card.innerHTML = `<div class="card-title">Disproportionality by problem code
    <span class="count">${results.length} codes tested</span></div>`;

  const summary = el('div', 'kpis');
  summary.style.marginBottom = '12px';
  summary.innerHTML = [
    ['EMA-2008 signals', S.fmtInt(nEma), 'PRR ≥ 2, χ² ≥ 4, a ≥ 3'],
    ['IC025 &gt; 0 (WHO)', S.fmtInt(nIc), 'Bayesian, stable for rare codes'],
    ['Both criteria', S.fmtInt(nBoth), 'the defensible set'],
    ['FDR q &lt; 0.05', S.fmtInt(nQ), 'Benjamini-Hochberg'],
  ].map(([l, v, s]) => `<div class="kpi"><div class="kpi-label">${l}</div>
      <div class="kpi-value">${v}</div><div class="kpi-sub">${s}</div></div>`).join('');
  card.appendChild(summary);

  const COLUMNS = [
    { key: 'term', label: 'Problem term', get: (r) => r.term, sort: (r) => r.term },
    { key: 'code', label: 'Code', cls: 'mono', get: (r) => r.code, sort: (r) => r.code },
    { key: 'a', label: 'a', right: true, get: (r) => S.fmtInt(r.a), sort: (r) => r.a },
    { key: 'exp', label: 'Expected', right: true,
      get: (r) => S.fmtNum(r.ic.expected, 1), sort: (r) => r.ic.expected },
    { key: 'prr', label: 'PRR (95% CI)', right: true,
      get: (r) => S.fmtRatio(r.res.prr), sort: (r) => r.res.prr.point },
    { key: 'ror', label: 'ROR (95% CI)', right: true,
      get: (r) => S.fmtRatio(r.res.ror), sort: (r) => r.res.ror.point },
    { key: 'ic', label: 'IC (95% CrI)', right: true,
      get: (r) => `${S.fmtNum(r.ic.ic)} (${S.fmtNum(r.ic.ic025)}, ${S.fmtNum(r.ic.ic975)})`,
      sort: (r) => r.ic.ic025 },
    { key: 'chi2', label: 'χ² (Yates)', right: true,
      get: (r) => Number.isFinite(r.res.chi2) ? S.fmtNum(r.res.chi2) : '—',
      sort: (r) => r.res.chi2 },
    { key: 'q', label: 'FDR q', right: true,
      get: (r) => r.q === null ? '—' : S.fmtP(r.q), sort: (r) => r.q },
    { key: 'sig', label: 'Signal',
      get: (r) => {
        const bits = [];
        if (r.ema) bits.push('<span class="badge badge-signal">EMA</span>');
        if (r.ic.signal) bits.push('<span class="badge badge-warn">IC</span>');
        return bits.length ? bits.join(' ') : '<span class="badge badge-none">—</span>';
      },
      sort: (r) => (r.ema ? 2 : 0) + (r.ic.signal ? 1 : 0) },
  ];

  // Default ordering: IC025 descending — the most conservative measure first,
  // so the least fragile signals are what a reader sees at the top.
  const tableHost = el('div');
  card.appendChild(tableHost);
  sortableTable(tableHost, results, COLUMNS, { key: 'ic', dir: -1 }, 560);

  card.insertAdjacentHTML('beforeend', note('info', 'ⓘ',
    `<strong>Comparator:</strong> ${S.fmtInt(eligible)} reports eligible to carry a
     problem code (2015 onward), not the full ${S.fmtInt(state.summary.total_reports)}-report
     corpus. The FDA problem-code files begin in 2015, so including earlier reports
     would inflate every PRR by roughly 25% and manufacture signals at the
     EMA threshold of PRR&nbsp;≥&nbsp;2.`));
  card.insertAdjacentHTML('beforeend', note('info', 'ⓘ',
    `<strong>Reading this table.</strong> <em>a</em> is the observed report count and
     <em>Expected</em> what independence predicts. <strong>PRR</strong> and
     <strong>ROR</strong> are frequentist ratios and become unstable for rare codes.
     <strong>IC</strong> is the WHO-UMC Bayesian measure with shrinkage: a code
     signals when its lower credibility bound <strong>IC025 &gt; 0</strong>, which a
     single stray report can never achieve. <strong>FDR q</strong> is the
     Benjamini-Hochberg adjusted p-value across all ${results.length} codes tested here —
     an unadjusted p of 0.04 among ${results.length} tests is not evidence of anything.
     Codes flagged by <em>both</em> EMA and IC are the defensible set.`));
  card.insertAdjacentHTML('beforeend', note('caveat', '!',
    `A signal is <strong>hypothesis-generating, not causal</strong>. ${PASSIVE_CAVEAT}
     Disproportionality is vulnerable to stimulated reporting after publicity,
     differential reporting between manufacturers, and indication bias.`));

  const flat = results.map((r) => ({
    Term: r.term, Code: r.code, a: r.a, b: r.b, c: r.c, d: r.d,
    Expected: r.ic.expected,
    PRR: r.res.prr.point, PRR_lo: r.res.prr.lo, PRR_hi: r.res.prr.hi,
    ROR: r.res.ror.point, ROR_lo: r.res.ror.lo, ROR_hi: r.res.ror.hi,
    IC: r.ic.ic, IC025: r.ic.ic025, IC975: r.ic.ic975,
    chi2_yates: r.res.chi2, chi2_p: r.res.chi2P, fisher_p: r.res.fisherP,
    p_used: r.p, fdr_q: r.q,
    ema_signal: r.ema, ic_signal: r.ic.signal,
  }));
  exportBtn(card, flat, 'disproportionality');
  host.appendChild(card);
}, (f) => f.productCode
  ? { what: 'Screening problem codes for over-representation…',
      detail: `Reading only the ${esc(f.productCode.toUpperCase())} slice of the problem-code index` }
  : { what: 'Screening problem codes for over-representation…',
      detail: 'No product code set, so the full problem-code index for ' +
              `${f.yearLo}–${f.yearHi} must be scanned. Setting a product code ` +
              'makes this roughly thirty times faster.' });

/* --------------------------- 10. Subgroup ------------------------------ */

panel('subgroup', 'Subgroups', 'Outcome rate stratified by a covariate, with Wilson 95% intervals and a chi-square test across strata.',
async (f, host) => {
  const outcome = state.draft.subOutcome || 'any_serious_outcome';
  const by = state.draft.subBy || 'Sex';
  const STRAT = {
    Sex: "CASE upper(left(coalesce(sex_list,'U'),1)) WHEN 'F' THEN 'Female' WHEN 'M' THEN 'Male' ELSE 'Unknown' END",
    'Age band': AGE_BAND,
    Year: 'report_year::VARCHAR',
    'Source type': "upper(trim(coalesce(SOURCE_TYPE,'Unknown')))",
    Country: "upper(trim(coalesce(reporter_country_code,'Unknown')))",
    'Reporter occupation': "upper(trim(coalesce(REPORTER_OCCUPATION_CODE,'UNK')))",
    Implant: "CASE WHEN implant_flag THEN 'Implant' ELSE 'Non-implant' END",
  };

  const ctl = el('div', 'card');
  ctl.innerHTML = `<div class="grid-2">
    <div><label style="font-size:11.5px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--text-2)">Outcome</label>
      <select id="sub-outcome">${OUTCOMES.map(([c, l]) =>
        `<option value="${c}" ${c === outcome ? 'selected' : ''}>${l}</option>`).join('')}</select></div>
    <div><label style="font-size:11.5px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--text-2)">Stratify by</label>
      <select id="sub-by">${Object.keys(STRAT).map((k) =>
        `<option value="${k}" ${k === by ? 'selected' : ''}>${k}</option>`).join('')}</select></div>
  </div>`;
  host.appendChild(ctl);
  ctl.querySelector('#sub-outcome').onchange = (e) => {
    state.draft.subOutcome = e.target.value;
    state.cache.delete(`subgroup::${signature(state.filters)}`); renderActive(true);
  };
  ctl.querySelector('#sub-by').onchange = (e) => {
    state.draft.subBy = e.target.value;
    state.cache.delete(`subgroup::${signature(state.filters)}`); renderActive(true);
  };

  const w = buildWhere(f);
  const rows = await DB.query(
    `SELECT ${STRAT[by]} AS subgroup, COUNT(*) AS n,
            SUM(CASE WHEN ${outcome} THEN 1 ELSE 0 END) AS k
     FROM ${src(f)} WHERE ${w.sql}
     GROUP BY 1 HAVING n > 0 ORDER BY n DESC LIMIT 25`, w.params);
  if (!rows.length) return host.insertAdjacentHTML('beforeend',
    emptyState('No data', 'This stratification produced no groups.'));

  const data = rows.map((r) => {
    const n = Number(r.n), k = Number(r.k);
    const label = by === 'Reporter occupation'
      ? (OCCUPATION[String(r.subgroup)] || String(r.subgroup))
      : String(r.subgroup);
    return { label, n, k, ci: S.wilsonCI(k, n) };
  });
  const chi = S.chi2Independence(data.map((d) => [d.k, d.n - d.k]));
  const outcomeLabel = (OUTCOMES.find(([c]) => c === outcome) || [, outcome])[1];

  const card = el('div', 'card');
  card.innerHTML = `<div class="card-title">${esc(outcomeLabel)} by ${esc(by)}
    <span class="count">${data.length} strata</span></div>`;
  const div = el('div', 'chart'); card.appendChild(div); host.appendChild(card);

  const ordered = [...data].reverse();
  const traces = [];
  ordered.forEach((d) => {
    const tag = d.n < 10 ? `${d.label} *` : d.label;
    traces.push({
      type: 'scatter', mode: 'lines', showlegend: false, hoverinfo: 'skip',
      x: [d.ci.lo * 100, d.ci.hi * 100], y: [tag, tag],
      line: { color: cssVar('--brand-500'), width: 2 },
    });
  });
  traces.push({
    type: 'scatter', mode: 'markers', showlegend: false,
    x: ordered.map((d) => d.ci.p * 100),
    y: ordered.map((d) => (d.n < 10 ? `${d.label} *` : d.label)),
    marker: { color: cssVar('--brand-700'), size: 9 },
    customdata: ordered.map((d) => [S.fmtInt(d.k), S.fmtInt(d.n), S.fmtCI(d.ci)]),
    hovertemplate: '%{y}<br>%{customdata[0]} / %{customdata[1]}<br>%{customdata[2]}<extra></extra>',
  });
  plot(div, traces, layout('', {
    height: Math.max(340, 26 * ordered.length + 90),
    xaxis: { title: `${outcomeLabel} rate (%), Wilson 95% CI` },
    margin: { l: 190, r: 24, t: 12, b: 48 },
  }));

  const chiMsg = `χ² = ${S.fmtNum(chi.chi2)}, df = ${chi.df}, p = ${S.fmtP(chi.p)}`;
  card.insertAdjacentHTML('beforeend', note(chi.valid ? 'info' : 'warn',
    chi.valid ? 'ⓘ' : '▲',
    `<strong>Test across strata:</strong> ${chiMsg}.` +
    (chi.valid ? '' : ` Smallest expected cell is ${S.fmtNum(chi.minExpected)} —
      below 5, so this p-value is unreliable (Cochran's rule). Collapse sparse
      strata or treat it as descriptive.`) +
    ` Strata marked <strong>*</strong> have n&nbsp;&lt;&nbsp;10 and unstable intervals.`));

  const flat = data.map((d) => ({
    Subgroup: d.label, 'Outcome n': d.k, Total: d.n,
    'Rate %': +(d.ci.p * 100).toFixed(3),
    'CI lo %': +(d.ci.lo * 100).toFixed(3), 'CI hi %': +(d.ci.hi * 100).toFixed(3),
  }));
  card.insertAdjacentHTML('beforeend', renderTable(flat, { maxHeight: 360 }));
  exportBtn(card, flat, 'subgroup_analysis');
});

/* ---------------------------- 11. Trend tests -------------------------- */

panel('trends', 'Trend tests', 'Cochran-Armitage test for trend in a proportion, and the tie-corrected Mann-Kendall test on yearly counts.',
async (f, host) => {
  const target = state.draft.trendTarget || '__all__';
  const opts = [['__all__', 'Total reports'], ["EVENT_TYPE = 'D'", 'Deaths (event type)'],
    ...OUTCOMES.map(([c, l]) => [`${c} = TRUE`, `Outcome: ${l}`])];

  const ctl = el('div', 'card');
  ctl.innerHTML = `<label style="font-size:11.5px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--text-2)">Target</label>
    <select id="tr-target" style="max-width:420px">${opts.map(([v, l]) =>
      `<option value="${esc(v)}" ${v === target ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>`;
  host.appendChild(ctl);
  ctl.querySelector('#tr-target').onchange = (e) => {
    state.draft.trendTarget = e.target.value;
    state.cache.delete(`trends::${signature(state.filters)}`); renderActive(true);
  };

  const w = buildWhere(f);
  const counter = target === '__all__' ? 'COUNT(*)' : `SUM(CASE WHEN ${target} THEN 1 ELSE 0 END)`;
  const rows = await DB.query(
    `SELECT report_year AS year, COUNT(*) AS n, ${counter} AS k
     FROM ${src(f)} WHERE ${w.sql} AND report_year IS NOT NULL
     GROUP BY 1 ORDER BY 1`, w.params);
  if (rows.length < 2) return host.insertAdjacentHTML('beforeend',
    emptyState('Not enough years', 'Trend testing needs at least two years of data.'));

  const years = rows.map((r) => Number(r.year));
  const ns = rows.map((r) => Number(r.n));
  const ks = rows.map((r) => Number(r.k));
  const ca = S.cochranArmitage(ks, ns, years);
  const mk = S.mannKendall(ks);
  const label = (opts.find(([v]) => v === target) || [, target])[1];

  const cards = el('div', 'grid-2'); host.appendChild(cards);
  for (const [t, r, extra] of [
    ['Cochran-Armitage (trend in proportion)', ca, `z = ${S.fmtNum(ca.statistic)}`],
    ['Mann-Kendall (monotonic trend in counts)', mk, `S = ${S.fmtInt(mk.statistic)}`],
  ]) {
    const c = el('div', 'card');
    c.innerHTML = `<div class="card-title">${t}</div>
      <div class="kpi-value">${extra}</div>
      <div class="kpi-sub" style="margin-top:6px">
        Direction: <strong>${r.direction}</strong> · p = ${S.fmtP(r.p)}</div>
      <div class="hint" style="margin-top:8px;font-size:12px;color:var(--text-3)">${esc(r.note)}</div>`;
    cards.appendChild(c);
  }

  const card = el('div', 'card');
  card.innerHTML = `<div class="card-title">${esc(label)} per year</div>`;
  const div = el('div', 'chart'); card.appendChild(div); host.appendChild(card);
  const rates = rows.map((r) => (Number(r.n) ? (Number(r.k) / Number(r.n)) * 100 : 0));
  plot(div, [
    { type: 'bar', name: 'Count', x: years, y: ks, marker: { color: cssVar('--brand-600') },
      hovertemplate: '%{x}<br>%{y:,}<extra></extra>' },
    ...(target === '__all__' ? [] : [{
      type: 'scatter', mode: 'lines+markers', name: 'Rate (%)', x: years, y: rates,
      yaxis: 'y2', line: { color: cssVar('--harm-life'), width: 2.2 },
      hovertemplate: '%{x}<br>%{y:.2f}%<extra></extra>' }]),
  ], layout('', {
    xaxis: { title: 'Report year', dtick: 1 },
    yaxis: { title: 'Count' },
    yaxis2: { title: 'Rate (%)', overlaying: 'y', side: 'right', gridcolor: 'rgba(0,0,0,0)' },
  }));
  card.insertAdjacentHTML('beforeend', note('caveat', '!',
    `Report volume tracks reporting behaviour as much as device performance.
     Regulatory changes, product recalls and publicity all shift these curves
     independently of any change in real-world risk. ${PASSIVE_CAVEAT}`));
  const flat = rows.map((r, i) => ({ Year: years[i], Reports: ns[i], Events: ks[i],
    'Rate %': +rates[i].toFixed(4) }));
  exportBtn(card, flat, 'trend_tests');
});

/* -------------------------- 12. Cohort compare ------------------------- */

panel('compare', 'Compare cohorts', 'Split the current cohort in two and test whether the event-type mix differs.',
async (f, host) => {
  const mode = state.draft.cmpMode || 'year';
  const mid = Math.floor((f.yearLo + f.yearHi) / 2);
  const d = state.draft;
  d.cmpA = d.cmpA ?? '';
  d.cmpB = d.cmpB ?? '';

  const ctl = el('div', 'card');
  ctl.innerHTML = `<div class="card-title">Split by</div>
    <div class="checks" style="flex-direction:row;gap:18px;margin-bottom:12px">
      ${['year', 'manufacturer', 'event'].map((m) => `<label class="check">
        <input type="radio" name="cm" value="${m}" ${m === mode ? 'checked' : ''}>
        <span>${{ year: 'Year range', manufacturer: 'Manufacturer', event: 'Event type' }[m]}</span>
      </label>`).join('')}
    </div>
    <div class="grid-2" id="cmp-inputs"></div>`;
  host.appendChild(ctl);
  ctl.querySelectorAll('input[name=cm]').forEach((r) => {
    r.onchange = () => {
      state.draft.cmpMode = r.value; state.draft.cmpA = ''; state.draft.cmpB = '';
      state.cache.delete(`compare::${signature(state.filters)}`); renderActive(true);
    };
  });

  const box = ctl.querySelector('#cmp-inputs');
  let clauseA, clauseB, paramsA, paramsB, labelA, labelB;

  if (mode === 'year') {
    const aHi = d.cmpA || mid, bLo = d.cmpB || (mid + 1);
    box.innerHTML = `<div><label>Cohort A: ${f.yearLo}–<span id="ah">${aHi}</span></label>
        <input type="range" id="cmp-a" min="${f.yearLo}" max="${f.yearHi - 1}" value="${aHi}"></div>
      <div><label>Cohort B: <span id="bl">${bLo}</span>–${f.yearHi}</label>
        <input type="range" id="cmp-b" min="${f.yearLo + 1}" max="${f.yearHi}" value="${bLo}"></div>`;
    box.querySelector('#cmp-a').oninput = (e) => {
      state.draft.cmpA = +e.target.value;
      state.cache.delete(`compare::${signature(state.filters)}`); renderActive(true);
    };
    box.querySelector('#cmp-b').oninput = (e) => {
      state.draft.cmpB = +e.target.value;
      state.cache.delete(`compare::${signature(state.filters)}`); renderActive(true);
    };
    clauseA = 'report_year BETWEEN ? AND ?'; paramsA = [f.yearLo, aHi];
    clauseB = 'report_year BETWEEN ? AND ?'; paramsB = [bLo, f.yearHi];
    labelA = `${f.yearLo}–${aHi}`; labelB = `${bLo}–${f.yearHi}`;
  } else if (mode === 'manufacturer') {
    box.innerHTML = `<div><label>Cohort A manufacturer contains</label>
        <input type="text" id="cmp-a" value="${esc(d.cmpA)}"></div>
      <div><label>Cohort B manufacturer contains</label>
        <input type="text" id="cmp-b" value="${esc(d.cmpB)}"></div>`;
    const commit = () => {
      state.draft.cmpA = box.querySelector('#cmp-a').value.trim();
      state.draft.cmpB = box.querySelector('#cmp-b').value.trim();
      state.cache.delete(`compare::${signature(state.filters)}`); renderActive(true);
    };
    box.querySelector('#cmp-a').onchange = commit;
    box.querySelector('#cmp-b').onchange = commit;
    if (!d.cmpA || !d.cmpB) {
      host.insertAdjacentHTML('beforeend', emptyState('Name two manufacturers',
        'Enter a substring for each cohort to compare them.'));
      return;
    }
    clauseA = 'lower(manufacturer) LIKE ?'; paramsA = [`%${d.cmpA.toLowerCase()}%`];
    clauseB = 'lower(manufacturer) LIKE ?'; paramsB = [`%${d.cmpB.toLowerCase()}%`];
    labelA = d.cmpA; labelB = d.cmpB;
  } else {
    const a = d.cmpA || 'D', b = d.cmpB || 'IN';
    box.innerHTML = `<div><label>Cohort A event type</label>
        <select id="cmp-a">${['D', 'IN', 'M'].map((v) =>
          `<option value="${v}" ${v === a ? 'selected' : ''}>${EVENT_LABELS[v]}</option>`).join('')}</select></div>
      <div><label>Cohort B event type</label>
        <select id="cmp-b">${['D', 'IN', 'M'].map((v) =>
          `<option value="${v}" ${v === b ? 'selected' : ''}>${EVENT_LABELS[v]}</option>`).join('')}</select></div>`;
    const commit = () => {
      state.draft.cmpA = box.querySelector('#cmp-a').value;
      state.draft.cmpB = box.querySelector('#cmp-b').value;
      state.cache.delete(`compare::${signature(state.filters)}`); renderActive(true);
    };
    box.querySelector('#cmp-a').onchange = commit;
    box.querySelector('#cmp-b').onchange = commit;
    if (a === b) {
      host.insertAdjacentHTML('beforeend', emptyState('Pick two different event types',
        'Cohort A and cohort B are currently the same.'));
      return;
    }
    clauseA = 'EVENT_TYPE = ?'; paramsA = [a];
    clauseB = 'EVENT_TYPE = ?'; paramsB = [b];
    labelA = EVENT_LABELS[a]; labelB = EVENT_LABELS[b];
  }

  const w = buildWhere(f);
  const grab = async (clause, params) => DB.query(
    `SELECT EVENT_TYPE, COUNT(*) AS n FROM ${src(f)}
     WHERE ${w.sql} AND ${clause} GROUP BY 1`, [...w.params, ...params]);
  const [ra, rb] = await Promise.all([grab(clauseA, paramsA), grab(clauseB, paramsB)]);

  const types = ['D', 'IN', 'M'];
  const va = types.map((t) => Number(ra.find((r) => r.EVENT_TYPE === t)?.n || 0));
  const vb = types.map((t) => Number(rb.find((r) => r.EVENT_TYPE === t)?.n || 0));
  if (!va.some(Boolean) && !vb.some(Boolean)) {
    return host.insertAdjacentHTML('beforeend',
      emptyState('Both cohorts are empty', 'Neither split matched any report.'));
  }

  const chi = S.chi2Independence(types.map((_, i) => [va[i], vb[i]]));
  const card = el('div', 'card');
  card.innerHTML = `<div class="card-title">${esc(labelA)} vs ${esc(labelB)}</div>`;
  const div = el('div', 'chart'); card.appendChild(div); host.appendChild(card);
  plot(div, [
    { type: 'bar', name: labelA, x: types.map((t) => EVENT_LABELS[t]), y: va,
      marker: { color: cssVar('--brand-600') }, hovertemplate: '%{x}<br>%{y:,}<extra></extra>' },
    { type: 'bar', name: labelB, x: types.map((t) => EVENT_LABELS[t]), y: vb,
      marker: { color: cssVar('--accent-500') }, hovertemplate: '%{x}<br>%{y:,}<extra></extra>' },
  ], layout('', { barmode: 'group', yaxis: { title: 'Reports' } }));

  const sa = va.reduce((x, y) => x + y, 0) || 1, sb = vb.reduce((x, y) => x + y, 0) || 1;
  const flat = types.map((t, i) => ({
    'Event type': EVENT_LABELS[t],
    [labelA]: va[i], [`${labelA} %`]: +((va[i] / sa) * 100).toFixed(2),
    [labelB]: vb[i], [`${labelB} %`]: +((vb[i] / sb) * 100).toFixed(2),
  }));
  card.insertAdjacentHTML('beforeend', renderTable(flat, { maxHeight: 220 }));
  card.insertAdjacentHTML('beforeend', note(chi.valid ? 'info' : 'warn',
    chi.valid ? 'ⓘ' : '▲',
    `χ² = ${S.fmtNum(chi.chi2)}, df = ${chi.df}, <strong>p = ${S.fmtP(chi.p)}</strong>` +
    (chi.valid ? '' : ` — smallest expected cell ${S.fmtNum(chi.minExpected)} < 5, so treat as descriptive.`)));
  exportBtn(card, flat, 'cohort_comparison');
});

/* --------------------------- 13. Sensitivity --------------------------- */

panel('sensitivity', 'Sensitivity', 'Re-runs the headline figures under the standard MAUDE exclusion scenarios, so you can see whether your result depends on them.',
async (f, host) => {
  const scenarios = [
    ['Base case (current filters)', {}],
    ['Excluding forwarded reports', { excludeForwarded: true }],
    ['Excluding RWD-sourced reports', { excludeRwd: true }],
    ['Initial reports only', { initialOnly: true }],
    ['Conservative (all three)', { excludeForwarded: true, excludeRwd: true, initialOnly: true }],
  ];
  const out = [];
  for (const [label, extra] of scenarios) {
    const w = buildWhere(f, extra);
    const r = await DB.queryRow(
      `SELECT COUNT(*) AS n,
              SUM(CASE WHEN EVENT_TYPE='D' THEN 1 ELSE 0 END) AS deaths,
              SUM(CASE WHEN any_serious_outcome THEN 1 ELSE 0 END) AS serious
       FROM ${src(f)} WHERE ${w.sql}`, w.params);
    const n = Number(r?.n || 0), serious = Number(r?.serious || 0);
    out.push({
      Scenario: label, Reports: n, 'Deaths (event type)': Number(r?.deaths || 0),
      'Serious (outcome)': serious,
      '% serious (95% CI)': n ? S.fmtCI(S.wilsonCI(serious, n)) : '—',
    });
  }
  const card = el('div', 'card');
  card.innerHTML = '<div class="card-title">Exclusion scenarios</div>'
    + renderTable(out, { maxHeight: 320 });
  const base = out[0].Reports, cons = out[4].Reports;
  card.insertAdjacentHTML('beforeend', note('info', 'ⓘ',
    `The conservative scenario retains <strong>${base ? ((cons / base) * 100).toFixed(1) : '0'}%</strong>
     of the base cohort (${S.fmtInt(cons)} of ${S.fmtInt(base)}). If your headline
     estimate moves materially across these rows, report the sensitivity analysis
     alongside it rather than the base case alone.`));
  exportBtn(card, out, 'sensitivity');
  host.appendChild(card);
});

/* --------------------------- 14. Reporter ------------------------------ */

panel('reporter', 'Reporter & source', 'Who filed these reports, and under what reporting channel.',
async (f, host) => {
  const w = buildWhere(f);
  const [occ, srcRows] = await Promise.all([
    DB.query(`SELECT coalesce(nullif(trim(REPORTER_OCCUPATION_CODE),''),'UNK') AS code,
              COUNT(*) AS n FROM ${src(f)} WHERE ${w.sql} GROUP BY 1 ORDER BY n DESC`, w.params),
    DB.query(`WITH base AS (SELECT SOURCE_TYPE FROM ${src(f)} WHERE ${w.sql}
                AND SOURCE_TYPE IS NOT NULL AND trim(SOURCE_TYPE) <> '')
              SELECT upper(trim(s.value)) AS code, COUNT(*) AS n
              FROM base, unnest(string_split(SOURCE_TYPE, ',')) s(value)
              WHERE trim(s.value) <> '' GROUP BY 1 ORDER BY n DESC LIMIT 15`, w.params),
  ]);

  const grid = el('div', 'grid-2'); host.appendChild(grid);

  const c1 = el('div', 'card');
  c1.innerHTML = '<div class="card-title">Reporter occupation</div>';
  const d1 = el('div', 'chart'); c1.appendChild(d1); grid.appendChild(c1);
  const occTop = occ.slice(0, 10).map((r) => ({
    label: OCCUPATION[String(r.code).toUpperCase()] || `Other (${r.code})`, n: Number(r.n),
  }));
  if (occTop.length) {
    plot(d1, [{ type: 'pie', labels: occTop.map((r) => r.label), values: occTop.map((r) => r.n),
      hole: 0.42, textinfo: 'percent', hovertemplate: '%{label}<br>%{value:,}<extra></extra>' }],
      layout('', { height: 380, legend: { orientation: 'v', x: 1, y: 0.5, font: { size: 10.5 } },
                   margin: { l: 10, r: 10, t: 10, b: 10 } }));
  }
  exportBtn(c1, occ.map((r) => ({
    Occupation: OCCUPATION[String(r.code).toUpperCase()] || `Other (${r.code})`,
    Code: r.code, Reports: Number(r.n) })), 'reporter_occupation');

  const c2 = el('div', 'card');
  c2.innerHTML = '<div class="card-title">Report source</div>';
  const d2 = el('div', 'chart'); c2.appendChild(d2); grid.appendChild(c2);
  const sTop = srcRows.map((r) => ({ label: SOURCE_LABELS[r.code] || r.code, n: Number(r.n) })).reverse();
  if (sTop.length) {
    plot(d2, [{ type: 'bar', orientation: 'h', y: sTop.map((r) => r.label), x: sTop.map((r) => r.n),
      marker: { color: cssVar('--accent-500') }, hovertemplate: '%{y}<br>%{x:,}<extra></extra>' }],
      layout('', { height: 380, margin: { l: 130, r: 20, t: 10, b: 40 }, xaxis: { title: 'Reports' } }));
  }
  exportBtn(c2, sTop.slice().reverse().map((r) => ({ Source: r.label, Reports: r.n })), 'report_source');
});

/* --------------------------- 15. Geography ----------------------------- */

panel('geography', 'Geography', 'Where the report was filed from (REPORTER_COUNTRY_CODE).',
async (f, host) => {
  const w = buildWhere(f);
  const rows = await DB.query(
    `SELECT upper(trim(reporter_country_code)) AS country, COUNT(*) AS n
     FROM ${src(f)} WHERE ${w.sql} AND reporter_country_code IS NOT NULL
       AND trim(reporter_country_code) <> '' GROUP BY 1 ORDER BY n DESC`, w.params);
  if (!rows.length) return host.insertAdjacentHTML('beforeend',
    emptyState('No country data', 'No report in this cohort records a reporter country.'));

  const grid = el('div', 'grid-2-1'); host.appendChild(grid);
  const c1 = el('div', 'card');
  c1.innerHTML = '<div class="card-title">Reports by country</div>';
  const d1 = el('div', 'chart'); c1.appendChild(d1); grid.appendChild(c1);
  plot(d1, [{
    type: 'choropleth', locationmode: 'ISO-3',
    locations: rows.map((r) => ISO2_TO_ISO3[r.country] || r.country),
    z: rows.map((r) => Math.log10(Number(r.n) + 1)),
    text: rows.map((r) => `${COUNTRY_NAMES[r.country] || r.country}: ${S.fmtInt(r.n)}`),
    hovertemplate: '%{text}<extra></extra>',
    colorscale: [[0, cssVar('--brand-050')], [0.5, cssVar('--brand-500')], [1, cssVar('--brand-900')]],
    colorbar: { title: { text: 'log₁₀(reports)', font: { size: 10 } }, thickness: 12 },
  }], layout('', { height: 420, geo: { showframe: false, showcoastlines: true,
    coastlinecolor: cssVar('--border'), bgcolor: 'rgba(0,0,0,0)',
    landcolor: cssVar('--surface-3'), projection: { type: 'natural earth' } },
    margin: { l: 0, r: 0, t: 10, b: 0 } }));
  c1.insertAdjacentHTML('beforeend', note('info', 'ⓘ',
    'MAUDE records ISO-2 country codes; these are mapped to ISO-3 for the map. ' +
    'Codes without a mapping appear in the table but not on the map.'));

  const c2 = el('div', 'card');
  const flat = rows.map((r) => ({ Country: COUNTRY_NAMES[r.country] || r.country,
    Code: r.country, Reports: Number(r.n) }));
  c2.innerHTML = `<div class="card-title">Top countries
    <span class="count">${rows.length} total</span></div>`
    + renderTable(flat.slice(0, 30), { maxHeight: 430 });
  exportBtn(c2, flat, 'geography');
  grid.appendChild(c2);
});

/* ------------------------- 16. Manufacturer mix ------------------------ */

panel('manufacturers', 'Manufacturers', 'Concentration of reporting across manufacturers in this cohort.',
async (f, host) => {
  const w = buildWhere(f);
  const rows = await DB.query(
    `SELECT manufacturer, COUNT(*) AS n FROM ${src(f)}
     WHERE ${w.sql} AND manufacturer IS NOT NULL AND trim(manufacturer) <> ''
     GROUP BY 1 ORDER BY n DESC`, w.params);
  if (!rows.length) return host.insertAdjacentHTML('beforeend',
    emptyState('No manufacturer data',
      'Manufacturer is carried on the device record, which is absent before 2015.'));

  const total = rows.reduce((a, r) => a + Number(r.n), 0);
  const hhi = rows.reduce((a, r) => a + ((Number(r.n) / total) * 100) ** 2, 0);

  const k = el('div', 'kpis');
  k.innerHTML = `
    <div class="kpi"><div class="kpi-label">Manufacturers</div>
      <div class="kpi-value">${S.fmtInt(rows.length)}</div></div>
    <div class="kpi"><div class="kpi-label">Top-1 share</div>
      <div class="kpi-value">${((Number(rows[0].n) / total) * 100).toFixed(1)}%</div>
      <div class="kpi-sub">${esc(truncate(rows[0].manufacturer, 30))}</div></div>
    <div class="kpi accent"><div class="kpi-label">HHI</div>
      <div class="kpi-value">${S.fmtInt(hhi)}</div>
      <div class="kpi-sub">${hhi > 2500 ? 'Highly concentrated' : hhi > 1500 ? 'Moderately concentrated' : 'Unconcentrated'}</div></div>`;
  host.appendChild(k);

  const card = el('div', 'card');
  card.innerHTML = '<div class="card-title">Top 20 manufacturers</div>';
  const div = el('div', 'chart'); card.appendChild(div); host.appendChild(card);
  const top = rows.slice(0, 20).reverse();
  plot(div, [{ type: 'bar', orientation: 'h',
    y: top.map((r) => truncate(r.manufacturer, 40)), x: top.map((r) => Number(r.n)),
    marker: { color: cssVar('--brand-600') },
    hovertemplate: '%{y}<br>%{x:,} reports<extra></extra>' }],
    layout('', { height: 520, margin: { l: 230, r: 20, t: 10, b: 40 }, xaxis: { title: 'Reports' } }));
  card.insertAdjacentHTML('beforeend', note('caveat', '!',
    `Manufacturer names in MAUDE are free text and are <strong>not normalised</strong>
     — the same company may appear under several spellings, which understates
     concentration. HHI above 2,500 is conventionally "highly concentrated".`));
  exportBtn(card, rows.map((r) => ({ Manufacturer: r.manufacturer, Reports: Number(r.n),
    'Share %': +((Number(r.n) / total) * 100).toFixed(3) })), 'manufacturers');
});

/* -------------------------- 17. Reporting lag -------------------------- */

panel('lag', 'Reporting lag', 'Days from the event date to FDA receipt.',
async (f, host) => {
  const w = buildWhere(f);
  const rows = await DB.query(
    `SELECT lag_days FROM ${src(f)} WHERE ${w.sql}
       AND lag_days IS NOT NULL AND lag_days BETWEEN 0 AND 3650 LIMIT 400000`, w.params);
  const neg = Number(await DB.queryValue(
    `SELECT COUNT(*) FROM ${src(f)} WHERE ${w.sql} AND lag_days < 0`, w.params));
  if (!rows.length) return host.insertAdjacentHTML('beforeend',
    emptyState('No lag data', 'These reports lack either an event date or a received date.'));

  const v = rows.map((r) => Number(r.lag_days)).sort((a, b) => a - b);
  const q = (p) => v[Math.min(v.length - 1, Math.floor(p * (v.length - 1)))];

  const k = el('div', 'kpis');
  k.innerHTML = `
    <div class="kpi"><div class="kpi-label">Median</div><div class="kpi-value">${S.fmtInt(q(0.5))} d</div></div>
    <div class="kpi"><div class="kpi-label">IQR</div><div class="kpi-value">${S.fmtInt(q(0.25))}–${S.fmtInt(q(0.75))} d</div></div>
    <div class="kpi"><div class="kpi-label">P90</div><div class="kpi-value">${S.fmtInt(q(0.9))} d</div></div>
    <div class="kpi"><div class="kpi-label">Reports with lag</div><div class="kpi-value">${S.fmtInt(v.length)}</div></div>`;
  host.appendChild(k);

  const card = el('div', 'card');
  card.innerHTML = `<div class="card-title">Lag distribution
    <span class="count">0–3,650 days shown</span></div>`;
  const div = el('div', 'chart'); card.appendChild(div); host.appendChild(card);
  plot(div, [{ type: 'histogram', x: v, nbinsx: 60, marker: { color: cssVar('--brand-600') },
    hovertemplate: '%{x} d<br>%{y:,}<extra></extra>' }],
    layout('', { xaxis: { title: 'Days from event to FDA receipt' }, yaxis: { title: 'Reports' } }));
  if (neg > 0) {
    card.insertAdjacentHTML('beforeend', note('warn', '▲',
      `<strong>${S.fmtInt(neg)} reports have a negative lag</strong> — the recorded
       event date falls after the received date. These are data-entry artefacts
       and are excluded from the chart and the quantiles above.`));
  }
});

/* --------------------------- 18. Text mining --------------------------- */

panel('textmining', 'Text mining', 'Most frequent terms and phrases in the event descriptions.',
async (f, host) => {
  if (!DB.hasNarratives()) return host.insertAdjacentHTML('beforeend',
    emptyState('Narratives not included',
      'This deployment was built without narrative text. Rebuild the data with narratives enabled to use this panel.'));

  const w = buildWhere(f);
  const cap = DB.CONFIG.maxNarrativeScanRows;
  const rows = await DB.query(
    `SELECT narrative_desc FROM ${src(f)} WHERE ${w.sql}
       AND narrative_desc IS NOT NULL LIMIT ${cap}`, w.params);
  if (!rows.length) return host.insertAdjacentHTML('beforeend',
    emptyState('No narratives', 'No report in this cohort carries description text.'));

  const STOP = new Set(('a about after all also an and any are as at be because been before being ' +
    'between both but by can could did do does doing during each few for from further had has have ' +
    'having he her here hers him his how i if in into is it its itself just me more most my no nor ' +
    'not now of off on once only or other our out over own same she should so some such than that ' +
    'the their them then there these they this those through to too under until up very was we were ' +
    'what when where which while who whom why will with you your patient device report reported ' +
    'event manufacturer information unknown date received stated indicated reportedly per was were ' +
    'been additional will may also one two three product returned evaluation complaint customer ' +
    'user facility further follow up review time approximately due found noted na none unk')
    .split(/\s+/));

  const tok = /\b[a-z]{3,}\b/g;
  const uni = new Map(), bi = new Map(), tri = new Map();
  const bump = (m, k) => m.set(k, (m.get(k) || 0) + 1);
  for (const r of rows) {
    const words = String(r.narrative_desc).toLowerCase().match(tok);
    if (!words) continue;
    const t = words.filter((x) => !STOP.has(x));
    for (let i = 0; i < t.length; i++) {
      bump(uni, t[i]);
      if (i + 1 < t.length) bump(bi, `${t[i]} ${t[i + 1]}`);
      if (i + 2 < t.length) bump(tri, `${t[i]} ${t[i + 1]} ${t[i + 2]}`);
    }
  }

  const grid = el('div', 'grid-2-1'); host.appendChild(grid);

  const c1 = el('div', 'card');
  c1.innerHTML = `<div class="card-title">Term cloud
    <span class="count">top 120 single words</span></div>`;
  const cloud = el('div', 'cloud');
  const topUni = [...uni.entries()].sort((a, b) => b[1] - a[1]).slice(0, 120);
  if (topUni.length) {
    const max = topUni[0][1], min = topUni[topUni.length - 1][1];
    const palette = [cssVar('--brand-800'), cssVar('--brand-600'), cssVar('--brand-500'),
                     cssVar('--accent-600'), cssVar('--harm-other')];
    for (const [word, n] of topUni) {
      const t = max === min ? 1 : (n - min) / (max - min);
      const span = el('span');
      span.textContent = word;
      span.style.cssText = `font-size:${(12 + t * 30).toFixed(1)}px;` +
        `font-weight:${t > 0.55 ? 700 : t > 0.25 ? 600 : 500};` +
        `color:${palette[Math.min(palette.length - 1, Math.floor((1 - t) * palette.length))]};`;
      span.title = `${word}: ${S.fmtInt(n)} occurrences`;
      cloud.appendChild(span);
    }
  }
  c1.appendChild(cloud);
  grid.appendChild(c1);

  const c2 = el('div', 'card');
  const phrases = [
    ...[...bi.entries()].filter(([, n]) => n >= 5),
    ...[...tri.entries()].filter(([, n]) => n >= 5),
  ].sort((a, b) => b[1] - a[1]).slice(0, 200)
    .map(([Phrase, Count]) => ({ Phrase, Count }));
  c2.innerHTML = `<div class="card-title">Top phrases
    <span class="count">2–3 words, n ≥ 5</span></div>`
    + renderTable(phrases.slice(0, 80), { maxHeight: 430 });
  exportBtn(c2, phrases, 'narrative_phrases');
  grid.appendChild(c2);

  if (rows.length >= cap) {
    host.insertAdjacentHTML('beforeend', note('warn', '▲',
      `<strong>Sampled.</strong> This cohort exceeds the ${S.fmtInt(cap)}-narrative
       analysis cap, so these frequencies come from the first ${S.fmtInt(cap)}
       reports rather than all of them. Narrow the cohort for a complete count.`));
  }
}, (f) => ({
  what: 'Downloading and tokenising narrative text…',
  detail: 'Narratives are the largest column in the corpus, so this panel ' +
          'transfers more than the others. It is capped at ' +
          `${S.fmtInt(DB.CONFIG.maxNarrativeScanRows)} reports.`,
}));

/* ------------------------- 19. Raw narratives -------------------------- */

panel('rawtext', 'Narrative text', 'The event descriptions themselves.',
async (f, host) => {
  if (!DB.hasNarratives()) return host.insertAdjacentHTML('beforeend',
    emptyState('Narratives not included', 'This deployment was built without narrative text.'));

  const w = buildWhere(f);
  const useFoi = DB.hasFoi();
  let rows, cols;
  if (useFoi) {
    cols = ['MDR_REPORT_KEY', 'TEXT_TYPE_CODE', 'FOI_TEXT'];
    rows = await DB.query(
      `WITH keys AS (SELECT MDR_REPORT_KEY FROM ${src(f)} WHERE ${w.sql})
       SELECT f.MDR_REPORT_KEY, f.TEXT_TYPE_CODE, f.FOI_TEXT
       FROM foi f JOIN keys USING (MDR_REPORT_KEY)
       WHERE f.FOI_TEXT IS NOT NULL LIMIT ${f.limit}`, w.params);
  } else {
    cols = ['MDR_REPORT_KEY', 'DATE_PREF', 'EVENT_TYPE', 'narrative_desc', 'narrative_mfg'];
    rows = await DB.query(
      `SELECT ${cols.join(', ')} FROM ${src(f)} WHERE ${w.sql} AND has_narrative
       ORDER BY DATE_PREF DESC NULLS LAST LIMIT ${f.limit}`, w.params);
  }

  const card = el('div', 'card');
  card.innerHTML = `<div class="card-title">Event narratives
    <span class="count">showing ${S.fmtInt(rows.length)}</span></div>`
    + renderTable(rows, { columns: cols, maxHeight: 640 });
  if (!useFoi) {
    card.insertAdjacentHTML('beforeend', note('info', 'ⓘ',
      `Narratives are stored to a <strong>4,000-character cap</strong> per report
       (0.33% of source text segments exceed it). The complete untruncated
       multi-part text is available in the downloadable Research Edition.`));
  }
  exportBtn(card, rows, 'narratives', cols);
  host.appendChild(card);
});

/* --------------------------- 20. Data quality -------------------------- */

panel('quality', 'Data quality', 'Field completeness and flag rates for this cohort — check this before quoting any headline figure.',
async (f, host) => {
  const w = buildWhere(f);
  const fields = [
    ['REPORT_NUMBER', 'Report number', "REPORT_NUMBER IS NOT NULL AND trim(REPORT_NUMBER) <> ''"],
    ['EVENT_TYPE', 'Event type', "EVENT_TYPE IS NOT NULL AND trim(EVENT_TYPE) <> ''"],
    ['DATE_RECEIVED_D', 'Date received', 'DATE_RECEIVED_D IS NOT NULL'],
    ['DATE_OF_EVENT_D', 'Date of event', 'DATE_OF_EVENT_D IS NOT NULL'],
    ['manufacturer', 'Manufacturer', "manufacturer IS NOT NULL AND trim(manufacturer) <> ''"],
    ['product_code', 'Product code', "product_code IS NOT NULL AND trim(product_code) <> ''"],
    ['reporter', 'Reporter occupation', "REPORTER_OCCUPATION_CODE IS NOT NULL AND trim(REPORTER_OCCUPATION_CODE) <> ''"],
    ['source', 'Source type', "SOURCE_TYPE IS NOT NULL AND trim(SOURCE_TYPE) <> ''"],
    ['narrative', 'Narrative present', 'has_narrative'],
    ['outcome', 'Outcome coded', "outcome_codes_raw IS NOT NULL AND trim(outcome_codes_raw) <> ''"],
    ['age', 'Patient age', 'age_years_avg IS NOT NULL'],
    ['devage', 'Device age', 'device_age_days IS NOT NULL'],
  ];
  const flags = [
    ['supplements', 'Supplement submissions', 'is_supplement'],
    ['rwd', 'RWD-sourced', 'IS_RWD_SOURCED'],
    ['forwarded', 'Forwarded 803.22(b)(2)', 'IS_FORWARDED_803_22_B2'],
    ['redact_b4', 'Redaction (b)(4)', 'HAS_REDACTION_B4'],
    ['redact_b6', 'Redaction (b)(6)', 'HAS_REDACTION_B6'],
  ];
  const all = [...fields, ...flags];
  const sel = all.map(([k, , expr]) =>
    `AVG(CASE WHEN ${expr} THEN 1.0 ELSE 0.0 END) AS ${k}`).join(', ');
  const r = await DB.queryRow(`SELECT COUNT(*) AS n, ${sel} FROM ${src(f)} WHERE ${w.sql}`, w.params);
  const n = Number(r?.n || 0);
  if (!n) return host.insertAdjacentHTML('beforeend', emptyState('Empty cohort', 'No reports match.'));

  const mk = (list, title, colour) => {
    const data = list.map(([k, label]) => ({ label, pct: Number(r[k] || 0) * 100 }))
      .sort((a, b) => a.pct - b.pct);
    const card = el('div', 'card');
    card.innerHTML = `<div class="card-title">${title}</div>`;
    const div = el('div', 'chart'); card.appendChild(div);
    plot(div, [{ type: 'bar', orientation: 'h', y: data.map((d) => d.label), x: data.map((d) => d.pct),
      marker: { color: colour }, hovertemplate: '%{y}<br>%{x:.1f}%<extra></extra>' }],
      layout('', { height: 40 + 26 * data.length, xaxis: { title: '% of reports', range: [0, 100] },
                   margin: { l: 175, r: 20, t: 10, b: 40 } }));
    return { card, data };
  };
  const a = mk(fields, `Field completeness — ${S.fmtInt(n)} reports`, cssVar('--brand-600'));
  const b = mk(flags, 'Flag rates', cssVar('--harm-hosp'));
  host.appendChild(a.card); host.appendChild(b.card);

  if (Number(r.product_code || 0) < 0.95) {
    a.card.insertAdjacentHTML('beforeend', note('warn', '▲',
      `<strong>Product code is only ${(Number(r.product_code) * 100).toFixed(1)}% complete
       in this cohort.</strong> This is not random missingness: the FDA device
       files begin in 2015, so every earlier report lacks device data entirely.
       Restrict the year range to 2015 onward for any device-level analysis.`));
  }
  exportBtn(b.card, [...a.data, ...b.data].map((d) => ({ Field: d.label, 'Rate %': +d.pct.toFixed(2) })),
    'data_quality');
});

/* ---------------------------- 21. Methods ------------------------------ */

panel('methods', 'Methods & STROBE',
  'An auto-generated methods paragraph matching exactly the cohort on screen — paste it into a manuscript.',
async (f, host) => {
  const w = buildWhere(f);
  const r = await DB.queryRow(
    `SELECT COUNT(*) AS n, COUNT(DISTINCT REPORT_NUMBER) AS ev FROM ${src(f)} WHERE ${w.sql}`,
    w.params);
  const n = Number(r?.n || 0), ev = Number(r?.ev || 0);

  const crit = [`Reports dated ${f.yearLo} to ${f.yearHi} inclusive, by preferred report date ` +
    '(DATE_RECEIVED where present, falling back to REPORT_DATE).'];
  if (f.productCode) crit.push(`FDA product classification code '${f.productCode.toUpperCase()}'.`);
  if (f.manufacturer) crit.push(`Manufacturer name containing '${f.manufacturer}' (case-insensitive).`);
  if (f.deviceTerms.length) crit.push('Brand, generic or model name containing any of: ' +
    f.deviceTerms.map((t) => `'${t}'`).join(', ') + ' (case-insensitive).');
  if (f.narrative) crit.push(`Event description containing '${f.narrative}'.`);
  if (f.eventTypes.length && f.eventTypes.length < 3) {
    crit.push(`Event types restricted to: ${f.eventTypes.map((t) => EVENT_LABELS[t]).join(', ')}.`);
  }
  if (f.excludeForwarded) crit.push('Reports forwarded under 21 CFR 803.22(b)(2) were excluded.');
  if (f.excludeRwd) crit.push('Reports sourced from real-world data under the 21 CFR 803.19 exemption were excluded.');
  if (f.initialOnly) crit.push('Supplemental submissions were excluded, retaining initial reports only, to avoid double-counting events that received follow-up.');
  if (f.implantOnly) crit.push("Restricted to reports where IMPLANT_FLAG = 'Y'.");
  if (f.seriousOnly) crit.push('Restricted to reports recording at least one serious patient outcome (D, L, H, S, C or R per 21 CFR 803.3).');
  if (f.mdrKey) crit.push(`Single report lookup: MDR_REPORT_KEY = ${f.mdrKey}.`);

  const vintage = state.summary.data_vintage || 'the current release';
  const md = `## Methods

We conducted a retrospective analysis of the United States Food and Drug
Administration Manufacturer and User Facility Device Experience (MAUDE)
database using the publicly distributed device-experience files (data current
to ${vintage}). The MDR master, device, patient, foitext, foidevproblem and
patientproblemcode files were parsed, multi-line narrative records were
reassembled by a state-machine parser, and the data were loaded into a DuckDB
analytic database. A denormalised analytic table with one row per medical
device report (MDR_REPORT_KEY) was constructed; patient-level fields, including
SEQUENCE_NUMBER_OUTCOME, were aggregated to the report level, and device fields
were attached from the first device record per report ordered by
DEVICE_EVENT_KEY.

**Inclusion criteria.** ${crit.join(' ')}

**Outcomes.** Patient outcomes were taken from SEQUENCE_NUMBER_OUTCOME and
dichotomised into the seven categories defined at 21 CFR 803.3: death (D),
life-threatening (L), hospitalization (H), disability (S), congenital anomaly
(C), required intervention (R) and other (O). A composite "any serious outcome"
was defined as any of D, L, H, S, C or R.

**Statistical analysis.** Proportions are reported with Wilson score 95%
confidence intervals. Comparisons between independent groups used Pearson's
chi-square test of independence, with Fisher's exact test substituted where any
cell contained fewer than 10 observations; the minimum expected cell count was
inspected and results resting on expected counts below 5 were treated as
descriptive. Disproportionality analyses report the proportional reporting
ratio and reporting odds ratio with log-normal 95% confidence intervals and a
0.5 continuity correction, together with a Yates-corrected chi-square computed
on observed counts. Two signal criteria were applied in parallel: the
frequentist EMA 2008 rule (PRR >= 2, chi-square >= 4, and at least 3 reports in
the cohort), and the Bayesian Information Component of the WHO Uppsala
Monitoring Centre, where a signal requires the lower bound of the 95%
credibility interval to exceed zero (IC025 > 0), computed with the shrinkage
form and closed-form credibility bounds of Noren et al. (Stat Med
2006;25:3740-57). The Information Component is reported alongside PRR because
it is stable for rare codes, where ratio measures are volatile and the EMA rule
over-signals. Because several hundred problem codes are screened
simultaneously, p-values were adjusted for multiple comparisons using the
Benjamini-Hochberg false discovery rate procedure, and adjusted q-values are
reported. The disproportionality comparator was restricted to the
${S.fmtInt(await DB.codeEligibleTotal())} reports eligible to carry a problem
code, because the FDA problem-code files begin in 2015 and including earlier
reports inflates the PRR. Temporal trends were assessed with the
Cochran-Armitage test for trend in proportions and the tie-corrected
Mann-Kendall non-parametric trend test on annual counts. Differences between
independent proportions are reported with Newcombe hybrid-score intervals
(Stat Med 1998;17:873-90).

**Cohort size.** ${S.fmtInt(n)} reports met the inclusion criteria, corresponding to
${S.fmtInt(ev)} unique events by REPORT_NUMBER.

**Limitations.** MAUDE is a passive surveillance system without a defined
denominator of exposed devices; reported frequencies are proportions of reports
and cannot be interpreted as incidence. Reporting is subject to under-reporting,
stimulated reporting following publicity, differential reporting between
manufacturers and user facilities, and residual duplication despite supplement
exclusion. Outcome coding is voluntary and incomplete. Device-level fields are
unavailable for reports predating 2015 in this build. Disproportionality
signals are hypothesis-generating and do not establish causation.

**Software.** Analyses were performed with MaudeDash (DuckDB for storage and
query, with a statistics engine cross-validated against SciPy).`;

  const sqlText = `-- MaudeDash cohort definition\nSELECT * FROM mdr_flat\nWHERE ${w.sql};\n` +
    `-- Bound parameters, in order:\n-- ${JSON.stringify(w.params)}`;

  const card = el('div', 'card');
  card.innerHTML = `<div class="card-title">Generated methods</div>
    <div class="prose">${mdToHtml(md)}</div>
    <h3 style="font-size:12.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--text-3);margin:20px 0 7px">
      Exact filter, for reproducibility</h3>
    <div class="prose"><pre>${esc(sqlText)}</pre></div>`;
  const row = el('div', 'btn-row');
  const b1 = el('button', 'btn', 'Download methods (.md)');
  b1.onclick = () => download(`maudedash_methods_${stamp()}.md`, md, 'text/markdown;charset=utf-8');
  const b2 = el('button', 'btn btn-secondary', 'Copy to clipboard');
  b2.onclick = async () => {
    try { await navigator.clipboard.writeText(md); b2.textContent = 'Copied'; }
    catch { b2.textContent = 'Copy failed'; }
    setTimeout(() => { b2.textContent = 'Copy to clipboard'; }, 1600);
  };
  row.append(b1, b2); card.appendChild(row);
  host.appendChild(card);
});

/** Minimal markdown -> HTML for the generated methods text. */
function mdToHtml(md) {
  return md.split(/\n{2,}/).map((block) => {
    if (block.startsWith('## ')) return `<h3>${esc(block.slice(3))}</h3>`;
    const html = esc(block).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/\n/g, ' ');
    return `<p>${html}</p>`;
  }).join('');
}

/* ----------------------------- 22. Export ------------------------------ */

panel('export', 'Export', 'Download the filtered cohort for archival deposit or offline analysis.',
async (f, host) => {
  const w = buildWhere(f);
  const cap = Math.min(f.limit, DB.CONFIG.maxExportRows);
  const card = el('div', 'card');
  card.innerHTML = `<div class="card-title">Cohort export
    <span class="count">${S.fmtInt(f.total)} reports in cohort</span></div>
    <p style="font-size:13px;color:var(--text-2)">
      Exports the analytic record for each report — identifiers, dates, device
      fields, the seven FDA outcome flags, problem codes and, where included,
      narrative text. Capped at ${S.fmtInt(cap)} rows to keep the download inside
      browser memory; narrow the cohort or use the Research Edition for the
      complete population.</p>`;

  const wide = el('button', 'btn', `Download cohort CSV (up to ${S.fmtInt(cap)} rows)`);
  wide.style.cssText = 'width:auto';
  wide.onclick = async () => {
    await withBusy('Building export…', async () => {
      const rows = await DB.query(
        `SELECT * FROM ${src(f)} WHERE ${w.sql}
         ORDER BY DATE_PREF DESC NULLS LAST, MDR_REPORT_KEY LIMIT ${cap}`, w.params);
      download(`maudedash_cohort_${stamp()}.csv`, toCsv(rows));
    });
  };
  const row = el('div', 'btn-row'); row.appendChild(wide); card.appendChild(row);

  if (f.total > cap) {
    card.insertAdjacentHTML('beforeend', note('warn', '▲',
      `Your cohort holds ${S.fmtInt(f.total)} reports but the export is capped at
       ${S.fmtInt(cap)}. The download will be <strong>truncated</strong>, ordered by
       date descending.`));
  }
  card.insertAdjacentHTML('beforeend', note('info', 'ⓘ',
    `<strong>Citing this extract?</strong> Record the data vintage
     (${esc(state.summary.data_vintage || 'unknown')}) and the exact filter from the
     Methods panel. MAUDE is revised retroactively, so a query re-run later may
     return different counts.`));
  host.appendChild(card);
});

/* ============================ rendering =============================== */

function buildTabs() {
  const host = $('#tabgroups');
  host.innerHTML = '';
  const row = el('div', 'tabgroup-row');
  for (const [group, ids] of TAB_GROUPS) {
    const g = el('div', 'tabgroup');
    g.appendChild(el('span', 'tabgroup-name', esc(group)));
    for (const id of ids) {
      const p = PANELS[id];
      if (!p) continue;
      const b = el('button', 'tab', esc(p.label));
      b.type = 'button';
      b.setAttribute('role', 'tab');
      b.setAttribute('aria-selected', String(id === state.activeTab));
      b.dataset.tab = id;
      b.onclick = () => { state.activeTab = id; syncTabs(); renderActive(); };
      g.appendChild(b);
    }
    row.appendChild(g);
  }
  host.appendChild(row);
}

function syncTabs() {
  document.querySelectorAll('.tab').forEach((b) => {
    b.setAttribute('aria-selected', String(b.dataset.tab === state.activeTab));
  });
}

/** Render only the active panel. Results are memoised per cohort. */
async function renderActive(force = false) {
  const host = $('#panels');
  const f = state.filters;
  if (!f) return;
  const p = PANELS[state.activeTab];
  if (!p) return;

  const key = `${p.id}::${signature(f)}`;
  let panelEl = host.querySelector(`[data-panel="${p.id}"]`);
  if (panelEl && panelEl.dataset.key === key && !force) {
    host.querySelectorAll('.panel').forEach((n) => n.classList.remove('active'));
    panelEl.classList.add('active');
    return;
  }
  if (panelEl) panelEl.remove();

  panelEl = el('div', 'panel active');
  panelEl.dataset.panel = p.id;
  panelEl.dataset.key = key;

  /*
   * Tell the user what this particular panel is about to do. "Running query"
   * is useless when the honest answer is "reading 20 million problem-code rows
   * because you have not named a product code".
   */
  const hint = p.cost ? p.cost(f) : null;
  panelEl.innerHTML = `<div class="panel-head"><h2>${esc(p.label)}</h2>
    <div class="desc">${esc(p.desc)}</div></div>
    <div class="loading"><span class="spinner"></span><div>
      <div>${esc(hint ? hint.what : 'Querying the corpus…')}</div>
      ${hint && hint.detail ? `<div class="loading-detail">${hint.detail}</div>` : ''}
    </div></div>`;
  host.querySelectorAll('.panel').forEach((n) => n.classList.remove('active'));
  host.appendChild(panelEl);

  const t0 = performance.now();
  // Anything past this threshold gets a running elapsed counter, so a slow
  // panel visibly stays alive instead of looking frozen.
  const tick = setInterval(() => {
    const el2 = panelEl.querySelector('.loading-detail');
    const secs = ((performance.now() - t0) / 1000).toFixed(0);
    if (el2) el2.dataset.elapsed = `${secs}s elapsed`;
    if (el2 && !el2.dataset.base) el2.dataset.base = el2.textContent;
    if (el2 && performance.now() - t0 > 2500) {
      el2.textContent = `${el2.dataset.base} · ${secs}s elapsed`;
    }
  }, 500);

  try {
    const body = el('div');
    await p.render(f, body);
    clearInterval(tick);
    panelEl.querySelector('.loading')?.remove();
    panelEl.appendChild(body);
  } catch (err) {
    clearInterval(tick);
    console.error(`[MaudeDash] panel "${p.id}" failed:`, err);
    panelEl.querySelector('.loading')?.remove();
    panelEl.insertAdjacentHTML('beforeend', note('warn', '▲',
      `<strong>This panel could not be computed.</strong> ${esc(err.message || String(err))}
       <br>Other panels are unaffected — try narrowing the cohort, or reload the page.`));
  }
}

/* ------------------------------ KPI band ------------------------------ */

async function renderKpis(f) {
  const w = buildWhere(f);
  const r = await DB.queryRow(
    `SELECT COUNT(*) AS n, COUNT(DISTINCT REPORT_NUMBER) AS ev,
            SUM(CASE WHEN EVENT_TYPE='D' THEN 1 ELSE 0 END) AS d,
            SUM(CASE WHEN EVENT_TYPE='IN' THEN 1 ELSE 0 END) AS i,
            SUM(CASE WHEN EVENT_TYPE='M' THEN 1 ELSE 0 END) AS m,
            SUM(CASE WHEN outcome_death THEN 1 ELSE 0 END) AS od,
            SUM(CASE WHEN any_serious_outcome THEN 1 ELSE 0 END) AS ser
     FROM ${src(f)} WHERE ${w.sql}`, w.params);

  const n = Number(r?.n || 0);
  f.total = n;
  const ser = Number(r?.ser || 0);
  const ci = n ? S.wilsonCI(ser, n) : null;

  const kpi = (label, value, sub = '', cls = '') => `
    <div class="kpi ${cls}"><div class="kpi-label">${label}</div>
      <div class="kpi-value">${value}</div>
      ${sub ? `<div class="kpi-sub">${sub}</div>` : ''}</div>`;

  $('#kpis').innerHTML =
    kpi('Reports', S.fmtInt(n), 'incl. supplements', 'accent') +
    kpi('Unique events', S.fmtInt(r?.ev || 0), 'distinct report no.') +
    kpi('Deaths', S.fmtInt(r?.d || 0), 'event type') +
    kpi('Injuries', S.fmtInt(r?.i || 0), 'event type') +
    kpi('Malfunctions', S.fmtInt(r?.m || 0), 'event type') +
    kpi('Death outcome', S.fmtInt(r?.od || 0), 'FDA code D', 'harm') +
    kpi('Any serious', S.fmtInt(ser), 'D/L/H/S/C/R') +
    kpi('% serious', ci ? `${(ci.p * 100).toFixed(1)}%` : '—',
        ci ? `95% CI ${(ci.lo * 100).toFixed(1)}–${(ci.hi * 100).toFixed(1)}` : '');
  return n;
}

/* --------------------------- landing state ---------------------------- */

function renderLanding() {
  const s = state.summary;
  $('#kpis').innerHTML = '';
  $('#tabgroups').style.display = 'none';
  const host = $('#panels');
  host.innerHTML = '';

  const wrap = el('div', 'panel active');
  wrap.innerHTML = `
    <div class="card">
      <div class="empty" style="padding:34px 20px">
        <img src="assets/logo.svg" alt="" style="width:46px;height:46px;opacity:.9">
        <h3 style="margin-top:12px;font-size:18px;color:var(--text)">
          ${S.fmtInt(s.total_reports)} FDA adverse event reports, ready to query</h3>
        <p>Define a cohort in the sidebar — a product code, a manufacturer, a device
          name or a phrase from the narrative — and every panel recomputes against it.
          Nothing leaves your browser.</p>
        <div class="examples">
          <button class="example" data-pc="KWP" data-y0="2018" data-y1="2024">
            <b>KWP</b> · pedicle screw systems, 2018–2024</button>
          <button class="example" data-pc="MNI" data-y0="2018" data-y1="2024">
            <b>MNI</b> · spinal interbody fusion, 2018–2024</button>
          <button class="example" data-pc="DXY" data-y0="2015" data-y1="2024">
            <b>DXY</b> · infusion pumps, 2015–2024</button>
          <button class="example" data-pc="LWS" data-y0="2015" data-y1="2024">
            <b>LWS</b> · hip prostheses, 2015–2024</button>
          <button class="example" data-mfg="medtronic" data-y0="2020" data-y1="2024">
            <b>Medtronic</b> · all devices, 2020–2024</button>
        </div>
      </div>
    </div>`;

  const chart = el('div', 'card');
  chart.innerHTML = `<div class="card-title">Corpus at a glance
    <span class="count">reports received per year, ${s.year_min}–${s.year_max}</span></div>`;
  const div = el('div', 'chart'); chart.appendChild(div);
  wrap.appendChild(chart);

  const stats = el('div', 'kpis');
  stats.style.marginTop = '14px';
  stats.innerHTML = [
    ['Reports', S.fmtInt(s.total_reports)],
    ['Unique events', S.fmtInt(s.unique_events)],
    ['Product codes', S.fmtInt(s.product_codes)],
    ['Manufacturers', S.fmtInt(s.manufacturers)],
    ['Death outcomes', S.fmtInt(s.outcome_deaths)],
    ['Serious outcomes', S.fmtInt(s.serious_outcomes)],
    ['Years covered', `${s.year_min}–${s.year_max}`],
    ['Data size', `${(s.web_tier_bytes / 1024 / 1024 / 1024).toFixed(2)} GB`],
  ].map(([l, v]) => `<div class="kpi"><div class="kpi-label">${l}</div>
      <div class="kpi-value">${v}</div></div>`).join('');
  wrap.appendChild(stats);

  wrap.insertAdjacentHTML('beforeend', note('caveat', '!', PASSIVE_CAVEAT +
    ' Device-level fields and narratives are unavailable for reports before 2015 in this build.'));
  host.appendChild(wrap);

  const by = s.by_year || [];
  plot(div, [
    { type: 'bar', name: 'Malfunction', x: by.map((r) => r.year), y: by.map((r) => r.malfunctions),
      marker: { color: cssVar('--brand-600') } },
    { type: 'bar', name: 'Injury', x: by.map((r) => r.year), y: by.map((r) => r.injuries),
      marker: { color: cssVar('--harm-hosp') } },
    { type: 'bar', name: 'Death', x: by.map((r) => r.year), y: by.map((r) => r.deaths),
      marker: { color: cssVar('--harm-death') } },
  ], layout('', { barmode: 'stack', height: 330, xaxis: { title: 'Report year' },
                  yaxis: { title: 'Reports received' } }));

  wrap.querySelectorAll('.example').forEach((b) => {
    b.onclick = () => {
      $('#f-pc').value = b.dataset.pc || '';
      $('#f-mfg').value = b.dataset.mfg || '';
      $('#f-dev').value = ''; $('#f-narr').value = ''; $('#f-mdr').value = '';
      if (b.dataset.y0) { $('#f-yr-lo').value = b.dataset.y0; }
      if (b.dataset.y1) { $('#f-yr-hi').value = b.dataset.y1; }
      syncYearLabels();
      apply();
    };
  });
}

/* =========================== search pickers =========================== */

/*
 * MAUDE identifies devices by opaque three-letter codes. Without a lookup the
 * tool is only usable by someone who already knows that KWP means "pedicle
 * screw system", which is a small audience. These indexes are built from the
 * device names that actually appear under each code in the corpus, so a user
 * can type "pedicle screw" or "infusion pump" and get there.
 */

let PRODUCT_INDEX = null;
let MANUFACTURER_INDEX = null;

async function loadSearchIndexes() {
  const get = async (f) => {
    try {
      const res = await fetch(`${DB.CONFIG.dataDir}${f}`, { cache: 'force-cache' });
      return res.ok ? await res.json() : null;
    } catch { return null; }
  };
  [PRODUCT_INDEX, MANUFACTURER_INDEX] = await Promise.all([
    get('product_index.json'), get('manufacturer_index.json'),
  ]);
}

/**
 * Score a product-code entry against a query. Higher is better; 0 = no match.
 *
 * Match quality and report volume are deliberately on comparable scales. Pure
 * text ranking buries the codes people actually want: searching "pedicle screw"
 * matched a 4-report code exactly and pushed NKB — 15,942 reports — to sixth.
 * The log10 volume term lets a high-volume near-match compete with a rare exact
 * one without ever letting volume alone outrank relevance.
 */
function scoreProduct(entry, q) {
  const code = entry.code.toLowerCase();
  if (code === q) return 1e6;                    // typing the code means the code
  let best = 0;
  const esc_ = q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const wordRe = new RegExp(`\\b${esc_}`);
  for (const raw of [entry.label, ...(entry.names || []), ...(entry.brands || [])]) {
    const s = String(raw).toLowerCase();
    let sc = 0;
    if (s === q) sc = 300;
    else if (s.startsWith(q)) sc = 250;
    else if (wordRe.test(s)) sc = 200;
    else if (s.includes(q)) sc = 150;
    if (sc > best) best = sc;
  }
  if (!best && code.startsWith(q)) best = 275;
  if (!best) return 0;
  return best + Math.log10((entry.n || 0) + 1) * 25;
}

function attachProductPicker(input) {
  const box = el('div', 'ac-menu');
  box.hidden = true;
  input.parentElement.style.position = 'relative';
  input.parentElement.appendChild(box);
  input.setAttribute('autocomplete', 'off');
  input.setAttribute('role', 'combobox');
  input.setAttribute('aria-expanded', 'false');

  let items = [];
  let active = -1;

  const close = () => {
    box.hidden = true; active = -1;
    input.setAttribute('aria-expanded', 'false');
  };

  const choose = (i) => {
    if (!items[i]) return;
    input.value = items[i].code;
    close();
    apply();
  };

  const render = (q) => {
    if (!PRODUCT_INDEX || q.length < 2) return close();
    items = PRODUCT_INDEX
      .map((e) => ({ e, s: scoreProduct(e, q) }))
      .filter((x) => x.s > 0)
      .sort((a, b) => b.s - a.s)
      .slice(0, 10)
      .map((x) => x.e);
    if (!items.length) return close();
    box.innerHTML = items.map((e, i) => `
      <div class="ac-item${i === active ? ' on' : ''}" data-i="${i}" role="option">
        <div class="ac-main"><span class="ac-code">${esc(e.code)}</span>
          <span class="ac-label">${esc(truncate(e.label, 46))}</span></div>
        <div class="ac-meta">${S.fmtInt(e.n)} reports${
          e.deaths ? ` · ${S.fmtInt(e.deaths)} deaths` : ''}${
          e.brands && e.brands.length ? ` · ${esc(truncate(e.brands[0], 30))}` : ''}</div>
      </div>`).join('');
    box.hidden = false;
    input.setAttribute('aria-expanded', 'true');
    box.querySelectorAll('.ac-item').forEach((n) => {
      n.onmousedown = (ev) => { ev.preventDefault(); choose(+n.dataset.i); };
    });
  };

  input.addEventListener('input', () => render(input.value.trim().toLowerCase()));
  input.addEventListener('focus', () => render(input.value.trim().toLowerCase()));
  input.addEventListener('blur', () => setTimeout(close, 120));
  input.addEventListener('keydown', (e) => {
    if (box.hidden) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      active = Math.max(0, Math.min(items.length - 1,
        active + (e.key === 'ArrowDown' ? 1 : -1)));
      box.querySelectorAll('.ac-item').forEach((n, i) => n.classList.toggle('on', i === active));
      box.querySelector('.ac-item.on')?.scrollIntoView({ block: 'nearest' });
    } else if (e.key === 'Enter' && active >= 0) {
      e.preventDefault(); choose(active);
    } else if (e.key === 'Escape') {
      close();
    }
  });
}

function attachManufacturerPicker(input) {
  const box = el('div', 'ac-menu');
  box.hidden = true;
  input.parentElement.style.position = 'relative';
  input.parentElement.appendChild(box);
  input.setAttribute('autocomplete', 'off');

  let items = [];
  let active = -1;
  const close = () => { box.hidden = true; active = -1; };

  const choose = (i) => {
    if (!items[i]) return;
    input.value = items[i].name;
    close();
    apply();
  };

  const render = (q) => {
    if (!MANUFACTURER_INDEX || q.length < 2) return close();
    items = MANUFACTURER_INDEX
      .filter((m) => m.name.toLowerCase().includes(q))
      .sort((a, b) => {
        const ap = a.name.toLowerCase().startsWith(q) ? 1 : 0;
        const bp = b.name.toLowerCase().startsWith(q) ? 1 : 0;
        return bp - ap || b.n - a.n;
      })
      .slice(0, 10);
    if (!items.length) return close();
    box.innerHTML = items.map((m, i) => `
      <div class="ac-item${i === active ? ' on' : ''}" data-i="${i}" role="option">
        <div class="ac-main"><span class="ac-label">${esc(truncate(m.name, 40))}</span></div>
        <div class="ac-meta">${S.fmtInt(m.n)} reports${
          m.deaths ? ` · ${S.fmtInt(m.deaths)} deaths` : ''}</div>
      </div>`).join('');
    box.hidden = false;
    box.querySelectorAll('.ac-item').forEach((n) => {
      n.onmousedown = (ev) => { ev.preventDefault(); choose(+n.dataset.i); };
    });
  };

  input.addEventListener('input', () => render(input.value.trim().toLowerCase()));
  input.addEventListener('blur', () => setTimeout(close, 120));
  input.addEventListener('keydown', (e) => {
    if (box.hidden) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      active = Math.max(0, Math.min(items.length - 1,
        active + (e.key === 'ArrowDown' ? 1 : -1)));
      box.querySelectorAll('.ac-item').forEach((n, i) => n.classList.toggle('on', i === active));
    } else if (e.key === 'Enter' && active >= 0) {
      e.preventDefault(); choose(active);
    } else if (e.key === 'Escape') close();
  });
}

/** Resolve a product code to its human label, for the cohort title. */
function productLabel(code) {
  if (!PRODUCT_INDEX || !code) return null;
  const hit = PRODUCT_INDEX.find((e) => e.code === code.toUpperCase());
  return hit ? hit.label : null;
}

/* ============================== sidebar =============================== */

function readDraft() {
  const lo = +$('#f-yr-lo').value, hi = +$('#f-yr-hi').value;
  return {
    productCode: $('#f-pc').value.trim(),
    manufacturer: $('#f-mfg').value.trim(),
    deviceTerms: $('#f-dev').value.split(';').map((s) => s.trim()).filter(Boolean),
    narrative: $('#f-narr').value.trim(),
    mdrKey: $('#f-mdr').value.trim(),
    yearLo: Math.min(lo, hi), yearHi: Math.max(lo, hi),
    eventTypes: ['D', 'IN', 'M'].filter((t) => $(`#f-et-${t}`).checked),
    excludeForwarded: $('#f-ex-fwd').checked,
    excludeRwd: $('#f-ex-rwd').checked,
    initialOnly: $('#f-initial').checked,
    implantOnly: $('#f-implant').checked,
    seriousOnly: $('#f-serious').checked,
    limit: Math.max(100, Math.min(100000, +$('#f-limit').value || 2000)),
  };
}

function hasCohort(f) {
  return !!(f.productCode || f.manufacturer || f.deviceTerms.length ||
            f.narrative || f.mdrKey);
}

function cohortTitle(f) {
  const bits = [];
  if (f.mdrKey) return `Report ${f.mdrKey}`;
  if (f.productCode) {
    const label = productLabel(f.productCode);
    bits.push(label ? `${label} (${f.productCode.toUpperCase()})`
                    : `Product code ${f.productCode.toUpperCase()}`);
  }
  if (f.manufacturer) bits.push(`“${f.manufacturer}”`);
  if (f.deviceTerms.length) bits.push(f.deviceTerms.join(' / '));
  if (f.narrative) bits.push(`narrative “${f.narrative}”`);
  const base = bits.join(' · ') || 'All reports';
  return `${base} · ${f.yearLo}–${f.yearHi}`;
}

async function apply() {
  const f = readDraft();
  if (!hasCohort(f)) {
    state.filters = null;
    $('#cohort-title').textContent = 'Corpus overview';
    renderLanding();
    writeUrl(f);
    return;
  }
  state.filters = f;
  $('#tabgroups').style.display = '';
  $('#cohort-title').textContent = cohortTitle(f);
  writeUrl(f);

  await withBusy('Counting cohort…', async () => {
    const n = await renderKpis(f);
    if (!n) {
      $('#panels').innerHTML = `<div class="panel active"><div class="card">${
        emptyState('No reports match this cohort',
          'Try widening the year range, or check the product code — MAUDE codes are ' +
          'three letters, such as KWP. Device names and narratives are only ' +
          'available for reports from 2015 onward.', '⌀')}</div></div>`;
      return;
    }
    state.cache.clear();
    buildTabs();
    $('#tabgroups').style.display = '';
    await renderActive(true);
  });
}

function reset() {
  ['f-pc', 'f-mfg', 'f-dev', 'f-narr', 'f-mdr'].forEach((id) => { $(`#${id}`).value = ''; });
  ['f-et-D', 'f-et-IN', 'f-et-M'].forEach((id) => { $(`#${id}`).checked = true; });
  ['f-ex-fwd', 'f-ex-rwd', 'f-initial', 'f-implant', 'f-serious'].forEach((id) => {
    $(`#${id}`).checked = false;
  });
  $('#f-limit').value = 2000;
  $('#f-yr-lo').value = state.years.min;
  $('#f-yr-hi').value = state.years.max;
  syncYearLabels();
  state.draft = {};
  apply();
}

/* URL round-trip so a cohort is shareable and citable. */
function writeUrl(f) {
  const p = new URLSearchParams();
  if (f.productCode) p.set('pc', f.productCode);
  if (f.manufacturer) p.set('mfg', f.manufacturer);
  if (f.deviceTerms.length) p.set('dev', f.deviceTerms.join(';'));
  if (f.narrative) p.set('q', f.narrative);
  if (f.mdrKey) p.set('mdr', f.mdrKey);
  if (f.yearLo !== state.years.min) p.set('y0', f.yearLo);
  if (f.yearHi !== state.years.max) p.set('y1', f.yearHi);
  if (f.eventTypes.length < 3) p.set('et', f.eventTypes.join(','));
  if (f.excludeForwarded) p.set('xfwd', '1');
  if (f.excludeRwd) p.set('xrwd', '1');
  if (f.initialOnly) p.set('init', '1');
  if (f.implantOnly) p.set('imp', '1');
  if (f.seriousOnly) p.set('ser', '1');
  if (state.activeTab !== 'preview') p.set('tab', state.activeTab);
  const qs = p.toString();
  history.replaceState(null, '', qs ? `?${qs}` : location.pathname);
}

function readUrl() {
  const p = new URLSearchParams(location.search);
  if (![...p.keys()].length) return false;
  $('#f-pc').value = p.get('pc') || '';
  $('#f-mfg').value = p.get('mfg') || '';
  $('#f-dev').value = p.get('dev') || '';
  $('#f-narr').value = p.get('q') || '';
  $('#f-mdr').value = p.get('mdr') || '';
  if (p.get('y0')) $('#f-yr-lo').value = p.get('y0');
  if (p.get('y1')) $('#f-yr-hi').value = p.get('y1');
  if (p.get('et')) {
    const set = new Set(p.get('et').split(','));
    ['D', 'IN', 'M'].forEach((t) => { $(`#f-et-${t}`).checked = set.has(t); });
  }
  $('#f-ex-fwd').checked = p.get('xfwd') === '1';
  $('#f-ex-rwd').checked = p.get('xrwd') === '1';
  $('#f-initial').checked = p.get('init') === '1';
  $('#f-implant').checked = p.get('imp') === '1';
  $('#f-serious').checked = p.get('ser') === '1';
  if (p.get('tab') && PANELS[p.get('tab')]) state.activeTab = p.get('tab');
  syncYearLabels();
  return true;
}

function syncYearLabels() {
  const lo = +$('#f-yr-lo').value, hi = +$('#f-yr-hi').value;
  $('#yr-lo-val').textContent = Math.min(lo, hi);
  $('#yr-hi-val').textContent = Math.max(lo, hi);
  const pre2015 = Math.min(lo, hi) < 2015;
  $('#yr-hint').innerHTML = pre2015
    ? '<strong style="color:var(--warn)">Reports before 2015 carry no device data or ' +
      'narratives</strong> — device, manufacturer and narrative filters will exclude them entirely.'
    : '';
}

/* ============================== boot ================================== */

function bootError(msg, detail) {
  $('#boot-status').style.display = 'none';
  const box = $('#boot-error');
  box.hidden = false;
  $('#boot-error-msg').innerHTML = msg;
  $('#boot-error-detail').textContent = detail || '';
}

async function main() {
  DB.onProgress((m) => { $('#boot-msg').textContent = m; });

  try {
    const { summary } = await DB.init();
    state.summary = summary;
    state.years = { min: summary.year_min ?? 1991, max: summary.year_max ?? 2024 };
  } catch (err) {
    console.error('[MaudeDash] startup failed:', err);
    bootError(
      'The data files could not be loaded. This usually means the <code>data/</code> ' +
      'folder was not uploaded next to <code>index.html</code>, or the server is not ' +
      'serving <code>.parquet</code> files with HTTP range support. See ' +
      '<code>deploy/DREAMHOST.md</code> for the required <code>.htaccess</code>.',
      err && (err.stack || err.message));
    return;
  }

  // Year sliders
  for (const id of ['#f-yr-lo', '#f-yr-hi']) {
    const s = $(id);
    s.min = state.years.min; s.max = state.years.max;
  }
  $('#f-yr-lo').value = state.years.min;
  $('#f-yr-hi').value = state.years.max;
  $('#f-yr-lo').oninput = syncYearLabels;
  $('#f-yr-hi').oninput = syncYearLabels;
  syncYearLabels();

  if (!DB.hasNarratives()) {
    $('#f-narr').disabled = true;
    $('#f-narr').placeholder = 'not available in this build';
    $('#narr-hint').textContent = 'This deployment was built without narrative text.';
  }

  // Vintage + about
  $('#vintage').lastElementChild.textContent =
    `FDA data to ${state.summary.data_vintage || 'unknown'}`;
  fillAbout();

  // Type-ahead pickers. These load in the background; the inputs stay usable
  // as plain text fields if the indexes are absent, so an older data/ folder
  // still works.
  loadSearchIndexes().then(() => {
    if (PRODUCT_INDEX) {
      attachProductPicker($('#f-pc'));
      $('#f-pc').placeholder = 'e.g. KWP, or search "pedicle screw"';
      const hint = $('#f-pc').parentElement.querySelector('.hint');
      if (hint) {
        hint.innerHTML = `Search ${S.fmtInt(PRODUCT_INDEX.length)} FDA product codes ` +
          'by code or device name — the names come from what manufacturers actually ' +
          'recorded in these reports.';
      }
    }
    if (MANUFACTURER_INDEX) {
      attachManufacturerPicker($('#f-mfg'));
      $('#f-mfg').placeholder = 'e.g. Medtronic';
    }
    // A cohort restored from the URL renders before these indexes arrive, so
    // its heading would be stuck on the bare code. Refresh it once we can
    // resolve the device name.
    if (state.filters) $('#cohort-title').textContent = cohortTitle(state.filters);
  });

  // Wiring
  $('#btn-apply').onclick = apply;
  $('#btn-reset').onclick = reset;
  $('#btn-sidebar').onclick = () => $('#sidebar').classList.toggle('open');
  $('#btn-about').onclick = () => $('#about-backdrop').classList.add('on');
  $('#btn-about-close').onclick = () => $('#about-backdrop').classList.remove('on');
  $('#about-backdrop').onclick = (e) => {
    if (e.target === $('#about-backdrop')) $('#about-backdrop').classList.remove('on');
  };
  $('#btn-theme').onclick = () => {
    const cur = document.documentElement.getAttribute('data-theme');
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('maudedash-theme', next); } catch { /* private mode */ }
    if (state.filters) renderActive(true); else renderLanding();
  };
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') $('#about-backdrop').classList.remove('on');
    if (e.key === 'Enter' && e.target.closest('.sidebar')) apply();
  });

  // Reveal
  $('#boot').classList.add('hidden');
  $('#app').hidden = false;

  buildTabs();
  const restored = readUrl();
  if (restored && hasCohort(readDraft())) await apply();
  else renderLanding();
}

function fillAbout() {
  const s = state.summary;
  const gb = (b) => `${(b / 1024 / 1024 / 1024).toFixed(2)} GB`;
  $('#about-stats').innerHTML = [
    ['Reports', S.fmtInt(s.total_reports)],
    ['Unique events', S.fmtInt(s.unique_events)],
    ['Years', `${s.year_min}–${s.year_max}`],
    ['Product codes', S.fmtInt(s.product_codes)],
    ['Manufacturers', S.fmtInt(s.manufacturers)],
    ['Data vintage', s.data_vintage || 'unknown'],
    ['Payload served', gb(s.web_tier_bytes)],
    ['Source database', `${gb(s.source_db_bytes)} (${Math.round(s.source_db_bytes / s.web_tier_bytes)}× larger)`],
    ['Built', (s.generated_utc || '').replace('T', ' ').replace('+00:00', ' UTC')],
  ].map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('');

  $('#about-coverage').innerHTML =
    `The FDA device, narrative and problem-code files in this build begin in
     <strong>2015</strong>, while the MDR master file reaches back to
     ${s.year_min}. Reports before 2015 therefore carry no product code,
     manufacturer, brand name, narrative or problem code. Any cohort defined by
     those fields contains no pre-2015 reports, whatever the year slider says.`;

  const year = new Date().getFullYear();
  $('#cite-text').textContent =
    `Porwal M. MaudeDash: an open analytic platform for the FDA MAUDE medical ` +
    `device adverse event database. Surg Neurol Int. 2026. ` +
    `doi:10.25259/SNI_201_2026. Available from: https://maudedash.com ` +
    `(FDA data current to ${s.data_vintage || 'n/a'}; accessed ${year}).`;
}

/* ISO-2 to ISO-3 for the choropleth. MAUDE stores ISO-2. */
const ISO2_TO_ISO3 = {
  US: 'USA', CA: 'CAN', MX: 'MEX', GB: 'GBR', UK: 'GBR', IE: 'IRL', FR: 'FRA',
  DE: 'DEU', IT: 'ITA', ES: 'ESP', PT: 'PRT', NL: 'NLD', BE: 'BEL', CH: 'CHE',
  AT: 'AUT', SE: 'SWE', NO: 'NOR', DK: 'DNK', FI: 'FIN', IS: 'ISL', PL: 'POL',
  CZ: 'CZE', SK: 'SVK', HU: 'HUN', RO: 'ROU', BG: 'BGR', GR: 'GRC', TR: 'TUR',
  RU: 'RUS', UA: 'UKR', HR: 'HRV', SI: 'SVN', RS: 'SRB', EE: 'EST', LV: 'LVA',
  LT: 'LTU', LU: 'LUX', MT: 'MLT', CY: 'CYP', JP: 'JPN', CN: 'CHN', KR: 'KOR',
  IN: 'IND', AU: 'AUS', NZ: 'NZL', BR: 'BRA', AR: 'ARG', CL: 'CHL', CO: 'COL',
  PE: 'PER', VE: 'VEN', ZA: 'ZAF', EG: 'EGY', IL: 'ISR', SA: 'SAU', AE: 'ARE',
  SG: 'SGP', MY: 'MYS', TH: 'THA', ID: 'IDN', PH: 'PHL', VN: 'VNM', TW: 'TWN',
  HK: 'HKG', PK: 'PAK', BD: 'BGD', LK: 'LKA', NG: 'NGA', KE: 'KEN', MA: 'MAR',
  DZ: 'DZA', TN: 'TUN', JO: 'JOR', LB: 'LBN', KW: 'KWT', QA: 'QAT', BH: 'BHR',
  OM: 'OMN', IQ: 'IRQ', IR: 'IRN', CR: 'CRI', PA: 'PAN', DO: 'DOM', GT: 'GTM',
  UY: 'URY', PY: 'PRY', BO: 'BOL', EC: 'ECU', PR: 'PRI',
};

const COUNTRY_NAMES = {
  US: 'United States', CA: 'Canada', GB: 'United Kingdom', UK: 'United Kingdom',
  DE: 'Germany', FR: 'France', IT: 'Italy', ES: 'Spain', NL: 'Netherlands',
  JP: 'Japan', CN: 'China', KR: 'South Korea', IN: 'India', AU: 'Australia',
  BR: 'Brazil', MX: 'Mexico', CH: 'Switzerland', SE: 'Sweden', BE: 'Belgium',
  IE: 'Ireland', AT: 'Austria', DK: 'Denmark', NO: 'Norway', FI: 'Finland',
  PL: 'Poland', TR: 'Turkey', IL: 'Israel', SG: 'Singapore', NZ: 'New Zealand',
  ZA: 'South Africa', PT: 'Portugal', GR: 'Greece', CZ: 'Czechia',
};

main();
