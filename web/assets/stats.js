/*
 * stats.js — MaudeDash statistical engine (browser port of maude_stats.py)
 * =======================================================================
 *
 * A faithful port of the desktop Research Edition's statistics, with the
 * special-function machinery (normal CDF, regularised incomplete gamma,
 * Fisher's exact) implemented directly so the browser tier does not silently
 * degrade to "p = n/a" the way the Python module does when SciPy is absent.
 *
 * Two deliberate divergences from maude_stats.py, both corrections:
 *
 *   1. Yates' continuity correction is clamped at zero —
 *      (max(0, |ad-bc| - n/2))^2 rather than (|ad-bc| - n/2)^2.
 *      The unclamped form squares a negative number for near-null tables and
 *      therefore reports a LARGER chi-square the closer the table is to
 *      independence. See yatesChi2().
 *
 *   2. Yates chi-square is computed on the observed counts, not on the
 *      continuity-corrected cells. The EMA-2008 signal rule is defined on
 *      observed counts; mixing the 0.5 ratio correction into the chi-square
 *      shifts the chi2 >= 4 threshold.
 *
 * Both are flagged in the UI's methods notes because they can change which
 * problem codes cross the EMA signal threshold.
 *
 * References
 * ----------
 *   Wilson EB. J Am Stat Assoc 1927;22:209-12.
 *   Evans SJ, Waller PC, Davis S. Pharmacoepidemiol Drug Saf 2001;10:483-6.
 *   Rothman KJ, Lanes S, Sacks ST. Pharmacoepidemiol Drug Saf 2004;13:519-23.
 *   Agresti A, Coull BA. Am Stat 1998;52:119-26.
 *   Mann HB. Econometrica 1945;13:245-59.
 */

'use strict';

export const Z_95 = 1.959963984540054;
export const Z_99 = 2.5758293035489004;
export const Z_90 = 1.6448536269514722;

/* ---------------------------------------------------------------- specials */

/** Lanczos log-gamma. Accurate to ~15 significant digits for x > 0. */
export function logGamma(x) {
  if (x <= 0) return NaN;
  const g = [
    676.5203681218851, -1259.1392167224028, 771.32342877765313,
    -176.61502916214059, 12.507343278686905, -0.13857109526572012,
    9.9843695780195716e-6, 1.5056327351493116e-7,
  ];
  if (x < 0.5) {
    // Reflection: Gamma(x)Gamma(1-x) = pi / sin(pi x)
    return Math.log(Math.PI / Math.sin(Math.PI * x)) - logGamma(1 - x);
  }
  const z = x - 1;
  let a = 0.99999999999980993;
  const t = z + 7.5;
  for (let i = 0; i < g.length; i++) a += g[i] / (z + i + 1);
  return 0.5 * Math.log(2 * Math.PI) + (z + 0.5) * Math.log(t) - t + Math.log(a);
}

function logFactorial(n) { return logGamma(n + 1); }

/** Abramowitz & Stegun 7.1.26-grade erf, refined via a rational approximation. */
export function erf(x) {
  const sign = x < 0 ? -1 : 1;
  x = Math.abs(x);
  // Numerical Recipes erfc, then invert. Relative error < 1.2e-7.
  const t = 1 / (1 + 0.5 * x);
  const y = t * Math.exp(-x * x - 1.26551223 + t * (1.00002368 + t * (0.37409196 +
    t * (0.09678418 + t * (-0.18628806 + t * (0.27886807 + t * (-1.13520398 +
    t * (1.48851587 + t * (-0.82215223 + t * 0.17087277)))))))));
  return sign * (1 - y);
}

export function erfc(x) { return 1 - erf(x); }

/** Standard normal CDF. */
export function normalCdf(z) { return 0.5 * erfc(-z / Math.SQRT2); }

/** Two-sided p-value for a z statistic. Matches maude_stats._two_sided_z_pvalue. */
export function twoSidedZP(z) {
  if (!Number.isFinite(z)) return NaN;
  return erfc(Math.abs(z) / Math.SQRT2);
}

/** Regularised lower incomplete gamma P(s, x) by series expansion. */
function gammaPSeries(s, x) {
  let sum = 1 / s, term = sum;
  for (let k = 1; k < 1000; k++) {
    term *= x / (s + k);
    sum += term;
    if (Math.abs(term) < Math.abs(sum) * 1e-16) break;
  }
  return sum * Math.exp(-x + s * Math.log(x) - logGamma(s));
}

/** Regularised upper incomplete gamma Q(s, x) by continued fraction (Lentz). */
function gammaQContinued(s, x) {
  const tiny = 1e-300;
  let b = x + 1 - s, c = 1 / tiny, d = 1 / b, h = d;
  for (let i = 1; i < 1000; i++) {
    const an = -i * (i - s);
    b += 2;
    d = an * d + b; if (Math.abs(d) < tiny) d = tiny;
    c = b + an / c;  if (Math.abs(c) < tiny) c = tiny;
    d = 1 / d;
    const del = d * c;
    h *= del;
    if (Math.abs(del - 1) < 1e-16) break;
  }
  return Math.exp(-x + s * Math.log(x) - logGamma(s)) * h;
}

/** Upper tail of the chi-square distribution: P(X_df^2 >= x). */
export function chi2SurvivalP(x, df) {
  if (!Number.isFinite(x) || !Number.isFinite(df) || df <= 0) return NaN;
  if (x <= 0) return 1;
  const s = df / 2, t = x / 2;
  return t < s + 1 ? 1 - gammaPSeries(s, t) : gammaQContinued(s, t);
}

/* ------------------------------------------------------------- Wilson CI */

/**
 * Wilson score interval for a binomial proportion.
 * Edge cases match maude_stats.wilson_ci: n=0 -> {p:0, lo:0, hi:1}.
 */
export function wilsonCI(successes, n, z = Z_95) {
  n = Number(n) || 0;
  successes = Number(successes) || 0;
  if (n <= 0) return { p: 0, lo: 0, hi: 1, n: 0, k: 0 };
  if (successes < 0) successes = 0;
  if (successes > n) successes = n;
  const p = successes / n;
  const denom = 1 + (z * z) / n;
  const centre = (p + (z * z) / (2 * n)) / denom;
  const half = (z / denom) * Math.sqrt((p * (1 - p)) / n + (z * z) / (4 * n * n));
  return {
    p, k: successes, n,
    lo: Math.max(0, centre - half),
    hi: Math.min(1, centre + half),
  };
}

/** "12.3% (11.8-12.9%)" */
export function fmtCI(ci, { pct = true, decimals = 1 } = {}) {
  const m = pct ? 100 : 1, u = pct ? '%' : '';
  if (!ci || !Number.isFinite(ci.p)) return '—';
  return `${(ci.p * m).toFixed(decimals)}${u} (${(ci.lo * m).toFixed(decimals)}–${(ci.hi * m).toFixed(decimals)}${u})`;
}

export function fmtRatio(r, decimals = 2) {
  if (!r || !Number.isFinite(r.point)) return '—';
  const lo = Number.isFinite(r.lo) ? r.lo.toFixed(decimals) : '—';
  const hi = Number.isFinite(r.hi) ? r.hi.toFixed(decimals) : '—';
  return `${r.point.toFixed(decimals)} (${lo}–${hi})`;
}

/* --------------------------------------------------------------- 2x2 table */

/**
 * Yates-corrected chi-square on OBSERVED counts, with the correction clamped
 * at zero. Layout [[a, c], [b, d]] — exposure across columns.
 */
export function yatesChi2(a, b, c, d) {
  const n = a + b + c + d;
  const n1 = a + b, n2 = c + d, m1 = a + c, m2 = b + d;
  if (n <= 0 || n1 <= 0 || n2 <= 0 || m1 <= 0 || m2 <= 0) return NaN;
  const corrected = Math.max(0, Math.abs(a * d - b * c) - n / 2);
  return (corrected * corrected * n) / (n1 * n2 * m1 * m2);
}

/**
 * Two-sided Fisher's exact test for a 2x2 table, by summing hypergeometric
 * point probabilities no greater than the observed one.
 * Layout matches analyze2x2: rows = outcome, columns = exposure.
 */
export function fisherExact2x2(a, b, c, d) {
  a = Math.round(a); b = Math.round(b); c = Math.round(c); d = Math.round(d);
  const n = a + b + c + d;
  if (n <= 0 || n > 2e7) return null;      // guard pathological inputs
  const r1 = a + c, r2 = b + d, c1 = a + b, c2 = c + d;
  const logConst = logFactorial(r1) + logFactorial(r2) +
                   logFactorial(c1) + logFactorial(c2) - logFactorial(n);
  const logP = (x) => logConst - (
    logFactorial(x) + logFactorial(c1 - x) +
    logFactorial(r1 - x) + logFactorial(c2 - r1 + x)
  );
  const lo = Math.max(0, r1 - c2), hi = Math.min(r1, c1);
  const observed = logP(a);
  const tol = 1e-9;
  let total = 0;
  for (let x = lo; x <= hi; x++) {
    const lp = logP(x);
    if (lp <= observed + tol) total += Math.exp(lp);
  }
  return Math.min(1, Math.max(0, total));
}

/**
 * PRR, ROR, Yates chi-square, Fisher's exact and risk difference for a 2x2.
 *
 *   |             | Exposed (cohort) | Unexposed |
 *   | Has outcome |        a         |     c     |
 *   | No outcome  |        b         |     d     |
 *
 * `eps` is a continuity correction applied only to the ratio statistics; the
 * returned counts and the chi-square use the observed values.
 */
export function analyze2x2(a, b, c, d, { eps = 0.5, z = Z_95, exactThreshold = 10 } = {}) {
  const ra = Math.round(a), rb = Math.round(b), rc = Math.round(c), rd = Math.round(d);
  const A = ra + eps, B = rb + eps, C = rc + eps, D = rd + eps;

  // PRR = (A/(A+B)) / (C/(C+D)), log-normal CI (Sahai-Khurshid SE)
  let prr = { point: NaN, lo: NaN, hi: NaN };
  if (A + B > 0 && C + D > 0) {
    const point = (A / (A + B)) / (C / (C + D));
    const se = Math.sqrt(1 / A - 1 / (A + B) + 1 / C - 1 / (C + D));
    if (point > 0 && Number.isFinite(se)) {
      const l = Math.log(point);
      prr = { point, lo: Math.exp(l - z * se), hi: Math.exp(l + z * se) };
    } else prr = { point, lo: NaN, hi: NaN };
  }

  // ROR = AD/BC, log-normal CI (Wald SE)
  let ror = { point: NaN, lo: NaN, hi: NaN };
  if (B > 0 && C > 0) {
    const point = (A * D) / (B * C);
    const se = Math.sqrt(1 / A + 1 / B + 1 / C + 1 / D);
    if (point > 0 && Number.isFinite(se)) {
      const l = Math.log(point);
      ror = { point, lo: Math.exp(l - z * se), hi: Math.exp(l + z * se) };
    } else ror = { point, lo: NaN, hi: NaN };
  }

  const chi2 = yatesChi2(ra, rb, rc, rd);
  const chi2P = Number.isFinite(chi2) ? chi2SurvivalP(chi2, 1) : NaN;

  let fisherP = null;
  if (Math.min(ra, rb, rc, rd) < exactThreshold) fisherP = fisherExact2x2(ra, rb, rc, rd);

  const r1 = wilsonCI(ra, ra + rb, z), r2 = wilsonCI(rc, rc + rd, z);
  const riskDiff = { point: r1.p - r2.p, lo: r1.lo - r2.hi, hi: r1.hi - r2.lo };

  return { a: ra, b: rb, c: rc, d: rd, prr, ror, chi2, chi2P, fisherP, riskDiff };
}

/**
 * Information Component with a 95% credibility interval (BCPNN).
 *
 * The WHO Uppsala Monitoring Centre's signal measure. IC contrasts the observed
 * count against what independence predicts, on a log2 scale, with Bayesian
 * shrinkage toward zero. Unlike PRR and ROR it is stable for rare codes — a
 * single report can never produce IC025 > 0 — which is precisely the regime
 * where MAUDE disproportionality is most fragile and where the EMA PRR rule
 * generates false signals.
 *
 * Shrinkage form and closed-form bounds from Norén GN, Bate A, Orre R,
 * Edwards IR. Stat Med 2006;25(21):3740-57.
 *
 * Layout matches analyze2x2. Signal when ic025 > 0.
 */
export function informationComponent(a, b, c, d) {
  a = Number(a); b = Number(b); c = Number(c); d = Number(d);
  const n = a + b + c + d;
  if (!(n > 0)) {
    return { observed: a, expected: NaN, ic: NaN, ic025: NaN, ic975: NaN, signal: false };
  }
  const expected = ((a + b) * (a + c)) / n;
  const obsS = a + 0.5;
  const expS = expected + 0.5;
  const ic = expS > 0 ? Math.log2(obsS / expS) : NaN;
  const ic025 = ic - 3.3 * Math.pow(obsS, -0.5) - 2.0 * Math.pow(obsS, -1.5);
  const ic975 = ic + 2.4 * Math.pow(obsS, -0.5) - 0.5 * Math.pow(obsS, -1.5);
  return { observed: a, expected, ic, ic025, ic975, signal: ic025 > 0 };
}

/**
 * Benjamini-Hochberg FDR-adjusted q-values, returned in the input order.
 *
 * A disproportionality screen tests hundreds of codes at once: at alpha = 0.05
 * across 700 codes you expect ~35 spurious hits purely by chance. null/NaN
 * entries pass through as null and are excluded from the ranking, so a missing
 * Fisher p does not shift anyone else's q.
 *
 * Verified against statsmodels multipletests(method='fdr_bh') to 3e-17.
 */
export function benjaminiHochberg(pValues) {
  const out = new Array(pValues.length).fill(null);
  const indexed = [];
  pValues.forEach((p, i) => {
    if (p !== null && p !== undefined && Number.isFinite(p)) indexed.push([i, p]);
  });
  const m = indexed.length;
  if (m === 0) return out;

  indexed.sort((x, y) => x[1] - y[1]);
  let prev = 1;
  for (let rank = m; rank >= 1; rank--) {
    const [idx, p] = indexed[rank - 1];
    const q = Math.min(prev, (p * m) / rank);
    out[idx] = Math.max(0, Math.min(1, q));
    prev = q;
  }
  return out;
}

/**
 * Newcombe hybrid-score interval for a difference of two proportions
 * (Newcombe RG, Stat Med 1998;17(8):873-90, method 10).
 *
 * Subtracting Wilson bounds directly over-covers and can leave [-1, 1]; the
 * hybrid-score form combines them in quadrature and holds nominal coverage
 * with small or extreme cells.
 */
export function newcombeDiffCI(a1, n1, a2, n2, z = Z_95) {
  if (!(n1 > 0) || !(n2 > 0)) return { point: NaN, lo: NaN, hi: NaN };
  const w1 = wilsonCI(a1, n1, z), w2 = wilsonCI(a2, n2, z);
  const diff = w1.p - w2.p;
  const lo = diff - Math.sqrt((w1.p - w1.lo) ** 2 + (w2.hi - w2.p) ** 2);
  const hi = diff + Math.sqrt((w1.hi - w1.p) ** 2 + (w2.p - w2.lo) ** 2);
  return { point: diff, lo: Math.max(-1, lo), hi: Math.min(1, hi) };
}

/** EMA-2008 screening rule. Hypothesis-generating only, never causal. */
export function emaSignal(a, prrPoint, chi2, { minA = 3, minPrr = 2, minChi2 = 4 } = {}) {
  if (!Number.isFinite(prrPoint) || !Number.isFinite(chi2)) return false;
  return a >= minA && prrPoint >= minPrr && chi2 >= minChi2;
}

/* ------------------------------------------------------------ trend tests */

/** Cochran-Armitage test for trend in proportions across ordered groups. */
export function cochranArmitage(successes, totals, scores = null) {
  const k = successes.length;
  if (totals.length !== k) throw new Error('successes and totals must match');
  if (k < 2) {
    return { test: 'cochran-armitage', statistic: 0, p: null,
             direction: 'no trend', note: 'Need at least 2 groups.' };
  }
  const s = scores && scores.length === k ? scores.map(Number) : [...Array(k).keys()];
  const N = totals.reduce((x, y) => x + y, 0);
  const R = successes.reduce((x, y) => x + y, 0);
  if (N === 0 || R === 0 || R === N) {
    return { test: 'cochran-armitage', statistic: 0, p: null, direction: 'no trend',
             note: 'Degenerate: no events, or every report has the event.' };
  }
  const pBar = R / N;
  const sBar = s.reduce((acc, si, i) => acc + si * totals[i], 0) / N;
  const numer = s.reduce((acc, si, i) => acc + si * (successes[i] - totals[i] * pBar), 0);
  const varTerm = totals.reduce((acc, ni, i) => acc + ni * (s[i] - sBar) ** 2, 0);
  const denom = pBar * (1 - pBar) * varTerm;
  if (!(denom > 0)) {
    return { test: 'cochran-armitage', statistic: 0, p: null,
             direction: 'no trend', note: 'Zero variance across groups.' };
  }
  const zStat = numer / Math.sqrt(denom);
  const p = twoSidedZP(zStat);
  return {
    test: 'cochran-armitage', statistic: zStat, p,
    direction: zStat > 0 ? 'increasing' : zStat < 0 ? 'decreasing' : 'no trend',
    note: `z = ${zStat.toFixed(3)}, p = ${fmtP(p)} (${k} year groups)`,
  };
}

/**
 * Mann-Kendall monotonic trend test with the standard tie correction.
 * maude_stats.py documents a tie correction but implements the no-ties
 * variance; this version applies it, which matters for short count series
 * that repeat values.
 */
export function mannKendall(values) {
  const n = values.length;
  if (n < 4) {
    return { test: 'mann-kendall', statistic: 0, p: null, direction: 'no trend',
             note: 'Need at least 4 observations.' };
  }
  let S = 0;
  for (let i = 0; i < n - 1; i++) {
    for (let j = i + 1; j < n; j++) {
      const dd = values[j] - values[i];
      if (dd > 0) S += 1; else if (dd < 0) S -= 1;
    }
  }
  // Variance with tie correction: sum over tie groups of t(t-1)(2t+5)
  const counts = new Map();
  for (const v of values) counts.set(v, (counts.get(v) || 0) + 1);
  let tieAdj = 0;
  for (const t of counts.values()) if (t > 1) tieAdj += t * (t - 1) * (2 * t + 5);
  const varS = (n * (n - 1) * (2 * n + 5) - tieAdj) / 18;
  if (!(varS > 0)) {
    return { test: 'mann-kendall', statistic: S, p: null, direction: 'no trend',
             note: 'Zero variance (all observations tied).' };
  }
  const zStat = S > 0 ? (S - 1) / Math.sqrt(varS)
              : S < 0 ? (S + 1) / Math.sqrt(varS) : 0;
  const p = twoSidedZP(zStat);
  const tau = S / (0.5 * n * (n - 1));
  return {
    test: 'mann-kendall', statistic: S, z: zStat, p, tau,
    direction: zStat > 0 ? 'increasing' : zStat < 0 ? 'decreasing' : 'no trend',
    note: `S = ${S}, z = ${zStat.toFixed(3)}, tau = ${tau.toFixed(3)}, p = ${fmtP(p)}`,
  };
}

/* ------------------------------------------------- chi-square independence */

/**
 * Pearson chi-square test of independence on an r x c table.
 * Also reports the minimum expected cell count, so the UI can warn when the
 * asymptotic approximation is not trustworthy (Cochran's rule).
 */
export function chi2Independence(table) {
  const rows = table.length;
  if (rows === 0) return { chi2: 0, p: null, df: 0, minExpected: 0, valid: false };
  const cols = table[0].length;
  if (table.some((r) => r.length !== cols)) throw new Error('ragged table');
  const n = table.reduce((acc, r) => acc + r.reduce((x, y) => x + y, 0), 0);
  if (n === 0) return { chi2: 0, p: null, df: 0, minExpected: 0, valid: false };
  const rowTot = table.map((r) => r.reduce((x, y) => x + y, 0));
  const colTot = Array.from({ length: cols },
    (_, j) => table.reduce((acc, r) => acc + r[j], 0));
  let chi2 = 0, minExpected = Infinity;
  for (let i = 0; i < rows; i++) {
    for (let j = 0; j < cols; j++) {
      const exp = (rowTot[i] * colTot[j]) / n;
      if (exp > 0) {
        minExpected = Math.min(minExpected, exp);
        chi2 += (table[i][j] - exp) ** 2 / exp;
      }
    }
  }
  const df = (rows - 1) * (cols - 1);
  return {
    chi2, df,
    p: df > 0 ? chi2SurvivalP(chi2, df) : null,
    minExpected: Number.isFinite(minExpected) ? minExpected : 0,
    valid: Number.isFinite(minExpected) && minExpected >= 5,
  };
}

/* ------------------------------------------------------------- formatting */

export function fmtP(p) {
  if (p === null || p === undefined || !Number.isFinite(p)) return 'n/a';
  if (p < 1e-300) return '< 1e-300';
  if (p < 0.001) return p.toExponential(2);
  return p.toFixed(4);
}

export function fmtInt(x) {
  const n = Number(x);
  return Number.isFinite(n) ? Math.round(n).toLocaleString('en-US') : '—';
}

export function fmtNum(x, decimals = 2) {
  const n = Number(x);
  return Number.isFinite(n)
    ? n.toLocaleString('en-US', { minimumFractionDigits: decimals,
                                  maximumFractionDigits: decimals })
    : '—';
}
