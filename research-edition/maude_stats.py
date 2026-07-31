"""
maude_stats.py — statistical engine for MaudeDash
=================================================

All functions return either a NamedTuple or a plain dict so they are easy to
consume from Streamlit, log, and serialize.

Implementations follow standard pharmacovigilance and biostatistics
references; scipy is used for exact tests where appropriate but everything
falls back gracefully if scipy is missing.

Corrections in this revision (v5) vs the v4 module used for the published
analysis. Each is verified against scipy/statsmodels by `test_maude.py`, and
each can change which problem codes cross the EMA-2008 signal threshold, so
any number carried over from an earlier run should be regenerated:

  * Yates χ² is now computed on the OBSERVED counts with the continuity
    correction clamped at zero. v4 fed the 0.5-corrected cells into the χ²
    and did not clamp, which overstated χ² by 39-49% on sparse tables and
    scored perfectly null tables (5,5,5,5) as 0.18 instead of 0.
  * PRR/ROR confidence bounds use the exact 95% z (1.959963984540054)
    instead of a hardcoded 1.96, matching what wilson_ci() already used.
  * Mann-Kendall applies the standard tie correction to Var(S). v4 documented
    one but implemented the no-ties form, overstating significance whenever a
    yearly series repeated a value.
  * chi2_sf() provides chi-square tail probabilities without SciPy, so
    p-values no longer silently become None on a minimal install.
  * min_expected_cell() exposes Cochran's rule so callers can flag χ² results
    that rest on expected counts below 5.

This module is mirrored by web/assets/stats.js for the browser tier; the two
are cross-validated to 1e-9 or better on every function.

References
----------
  Wilson EB. Probable inference, the law of succession, and statistical
  inference. J Am Stat Assoc 1927;22:209-12.
  Evans SJ, Waller PC, Davis S. Use of proportional reporting ratios (PRRs)
  for signal generation. Pharmacoepidemiol Drug Saf 2001;10(6):483-6.
  Rothman KJ, Lanes S, Sacks ST. The reporting odds ratio and its advantages.
  Pharmacoepidemiol Drug Saf 2004;13(8):519-23.
  Agresti A, Coull BA. Approximate is better than "exact" for interval
  estimation of binomial proportions. Am Stat 1998;52(2):119-26.
  Mantel N, Haenszel W. Statistical aspects of the analysis of data from
  retrospective studies. J Natl Cancer Inst 1959;22:719-48.
  Mann HB. Nonparametric tests against trend. Econometrica 1945;13(3):245-59.
  Kendall MG. Rank Correlation Methods. London: Griffin; 1948.
"""

from __future__ import annotations

import math
from typing import NamedTuple, Optional, Sequence

try:
    from scipy import stats as _scipy_stats
    _HAS_SCIPY = True
except Exception:  # pragma: no cover
    _HAS_SCIPY = False


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

class WilsonCI(NamedTuple):
    p: float
    lo: float
    hi: float
    n: int

    def as_str(self, pct: bool = True, decimals: int = 1) -> str:
        mul = 100.0 if pct else 1.0
        unit = "%" if pct else ""
        return f"{self.p*mul:.{decimals}f}{unit} ({self.lo*mul:.{decimals}f}-{self.hi*mul:.{decimals}f}{unit})"


class RatioCI(NamedTuple):
    point: float
    lo: float
    hi: float

    def as_str(self, decimals: int = 2) -> str:
        return f"{self.point:.{decimals}f} ({self.lo:.{decimals}f}-{self.hi:.{decimals}f})"


class TwoByTwo(NamedTuple):
    a: float
    b: float
    c: float
    d: float

    @property
    def n(self) -> float:
        return self.a + self.b + self.c + self.d


class TwoByTwoResult(NamedTuple):
    a: int
    b: int
    c: int
    d: int
    prr: RatioCI
    ror: RatioCI
    chi2_yates: float
    fisher_p: Optional[float]
    risk_diff: RatioCI


class TrendResult(NamedTuple):
    test: str
    statistic: float
    p_value: Optional[float]
    direction: str  # 'increasing' | 'decreasing' | 'no trend'
    note: str


# ---------------------------------------------------------------------------
# Wilson CI for binomial proportion
# ---------------------------------------------------------------------------

def wilson_ci(successes: int, n: int, alpha: float = 0.05) -> WilsonCI:
    """Wilson score interval. Vastly better than the textbook normal-approx
    interval for small n or extreme p. No external deps.

    Edge cases:
      n = 0       → CI (0, 1) with p=0
      successes=0 → lo = 0 exactly
      successes=n → hi = 1 exactly
    """
    if n <= 0:
        return WilsonCI(p=0.0, lo=0.0, hi=1.0, n=0)
    if successes < 0 or successes > n:
        raise ValueError(f"successes={successes} not in [0, {n}]")

    z = _z_for_alpha(alpha)
    p = successes / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    halfw = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return WilsonCI(
        p=p,
        lo=max(0.0, centre - halfw),
        hi=min(1.0, centre + halfw),
        n=n,
    )


def _z_for_alpha(alpha: float) -> float:
    """Two-sided z for confidence level 1-alpha. Hand-coded to avoid scipy
    dependency for this hot path."""
    # Common cases first
    if abs(alpha - 0.05) < 1e-12:
        return 1.959963984540054
    if abs(alpha - 0.01) < 1e-12:
        return 2.5758293035489004
    if abs(alpha - 0.10) < 1e-12:
        return 1.6448536269514722
    if _HAS_SCIPY:
        return float(_scipy_stats.norm.ppf(1 - alpha / 2))
    # Abramowitz-Stegun-style approximation as a last resort
    p = 1 - alpha / 2
    t = math.sqrt(-2 * math.log(1 - p))
    c0, c1, c2 = 2.515517, 0.802853, 0.010328
    d1, d2, d3 = 1.432788, 0.189269, 0.001308
    return t - (c0 + c1 * t + c2 * t * t) / (1 + d1 * t + d2 * t * t + d3 * t * t * t)


# ---------------------------------------------------------------------------
# 2x2 contingency table analysis
# ---------------------------------------------------------------------------

def analyze_2x2(
    a: int, b: int, c: int, d: int,
    *,
    eps: float = 0.5,
    alpha: float = 0.05,
    exact_threshold: int = 10,
) -> TwoByTwoResult:
    """Compute PRR, ROR, Yates χ², Fisher's exact (when small cells),
    and risk-difference Wilson CIs for a 2×2 contingency table.

    Layout convention:
      |               | Exposed (cohort) | Unexposed |
      | Has outcome   |        a         |     c     |
      | No outcome    |        b         |     d     |

    eps is the continuity correction added to each cell when computing
    ratio statistics, so zero cells don't blow up. The "raw" counts
    (a/b/c/d) returned in the result are untouched.
    """
    raw_a, raw_b, raw_c, raw_d = int(a), int(b), int(c), int(d)
    A, B, C, D = a + eps, b + eps, c + eps, d + eps
    z = _z_for_alpha(alpha)

    # PRR
    if (A + B) > 0 and (C + D) > 0:
        prr = (A / (A + B)) / (C / (C + D))
        se_ln_prr = math.sqrt((1 / A) - (1 / (A + B)) + (1 / C) - (1 / (C + D)))
        ln_prr = math.log(prr) if prr > 0 else float("nan")
        prr_lo = math.exp(ln_prr - z * se_ln_prr) if not math.isnan(ln_prr) else float("nan")
        prr_hi = math.exp(ln_prr + z * se_ln_prr) if not math.isnan(ln_prr) else float("nan")
    else:
        prr = prr_lo = prr_hi = float("nan")

    # ROR
    if B > 0 and C > 0:
        ror = (A * D) / (B * C)
        se_ln_ror = math.sqrt((1 / A) + (1 / B) + (1 / C) + (1 / D))
        ln_ror = math.log(ror) if ror > 0 else float("nan")
        ror_lo = math.exp(ln_ror - z * se_ln_ror) if not math.isnan(ln_ror) else float("nan")
        ror_hi = math.exp(ln_ror + z * se_ln_ror) if not math.isnan(ln_ror) else float("nan")
    else:
        ror = ror_lo = ror_hi = float("nan")

    # Yates χ² on the OBSERVED counts, with the correction clamped at zero.
    #
    # Two corrections vs MaudeDash v4 and earlier, both of which inflated χ²
    # and therefore over-produced EMA signals:
    #   1. v4 fed the eps-corrected cells (A/B/C/D) into the χ². The EMA-2008
    #      rule is defined on observed counts; the 0.5 ratio correction belongs
    #      only to PRR/ROR. On sparse tables this overstated χ² by 39-49%.
    #   2. v4 squared (|ad-bc| - n/2) without clamping, so a table CLOSER to
    #      independence than n/2 produced a LARGER χ² instead of ~0. A perfectly
    #      null table such as (5,5,5,5) scored 0.18 rather than 0.
    # Verified against scipy.stats.chi2_contingency(correction=True).
    n_obs = raw_a + raw_b + raw_c + raw_d
    n1 = raw_a + raw_b       # exposed total
    n2 = raw_c + raw_d       # unexposed total
    m1 = raw_a + raw_c       # has-outcome total
    m2 = raw_b + raw_d       # no-outcome total
    if n_obs > 0 and n1 > 0 and n2 > 0 and m1 > 0 and m2 > 0:
        corrected = max(0.0, abs(raw_a * raw_d - raw_b * raw_c) - n_obs / 2.0)
        chi2_yates = (corrected ** 2 * n_obs) / (n1 * n2 * m1 * m2)
    else:
        chi2_yates = float("nan")

    # Fisher's exact for small cells
    fisher_p: Optional[float] = None
    if _HAS_SCIPY and min(raw_a, raw_b, raw_c, raw_d) < exact_threshold:
        try:
            _, fisher_p = _scipy_stats.fisher_exact([[raw_a, raw_b], [raw_c, raw_d]])
        except Exception:
            fisher_p = None

    # Risk difference with Wilson CIs on each proportion
    r1 = wilson_ci(raw_a, raw_a + raw_b, alpha=alpha)
    r2 = wilson_ci(raw_c, raw_c + raw_d, alpha=alpha)
    rd = r1.p - r2.p
    rd_lo = r1.lo - r2.hi
    rd_hi = r1.hi - r2.lo

    return TwoByTwoResult(
        a=raw_a, b=raw_b, c=raw_c, d=raw_d,
        prr=RatioCI(point=prr, lo=prr_lo, hi=prr_hi),
        ror=RatioCI(point=ror, lo=ror_lo, hi=ror_hi),
        chi2_yates=chi2_yates,
        fisher_p=fisher_p,
        risk_diff=RatioCI(point=rd, lo=rd_lo, hi=rd_hi),
    )


class ICResult(NamedTuple):
    """Bayesian Information Component (BCPNN), the WHO-UMC signal measure."""
    observed: float
    expected: float
    ic: float
    ic025: float
    ic975: float

    @property
    def signal(self) -> bool:
        """WHO-UMC criterion: the lower credibility bound exceeds zero."""
        return self.ic025 > 0

    def as_str(self, decimals: int = 2) -> str:
        return f"{self.ic:.{decimals}f} ({self.ic025:.{decimals}f} to {self.ic975:.{decimals}f})"


def information_component(a: float, b: float, c: float, d: float) -> ICResult:
    """Information Component with 95% credibility interval.

    IC contrasts the observed count against what independence would predict,
    on a log2 scale, with Bayesian shrinkage toward zero. Unlike PRR and ROR it
    is stable for rare codes — a single report cannot produce IC025 > 0 — which
    is exactly the regime where MAUDE disproportionality is most fragile.

    Uses the shrinkage form and the closed-form credibility approximation from
    Norén GN, Bate A, Orre R, Edwards IR. Extending the methods used to screen
    the WHO drug safety database. Stat Med 2006;25(21):3740-57. The +0.5 priors
    are the standard BCPNN choice.

    Layout matches analyze_2x2:
        a = exposed with outcome    b = exposed without
        c = unexposed with outcome  d = unexposed without

    Signal when ic025 > 0. This is a stricter, better-behaved criterion than
    the EMA PRR rule and is what the WHO Uppsala Monitoring Centre uses.
    """
    a, b, c, d = float(a), float(b), float(c), float(d)
    n = a + b + c + d
    if n <= 0:
        nan = float("nan")
        return ICResult(a, nan, nan, nan, nan)

    # Expected count under independence of exposure and outcome.
    expected = (a + b) * (a + c) / n
    obs_s = a + 0.5
    exp_s = expected + 0.5

    ic = math.log2(obs_s / exp_s) if exp_s > 0 else float("nan")
    # Norén 2006 closed-form 95% credibility bounds.
    ic025 = ic - 3.3 * obs_s ** -0.5 - 2.0 * obs_s ** -1.5
    ic975 = ic + 2.4 * obs_s ** -0.5 - 0.5 * obs_s ** -1.5
    return ICResult(observed=a, expected=expected, ic=ic, ic025=ic025, ic975=ic975)


def benjamini_hochberg(p_values: Sequence[Optional[float]]) -> list[Optional[float]]:
    """Benjamini-Hochberg FDR-adjusted q-values, in the input order.

    Disproportionality screens hundreds of problem codes at once, so raw
    p-values are badly inflated: at alpha=0.05 across 700 codes you expect ~35
    spurious hits. Earlier MaudeDash versions applied no multiplicity control
    at all. Entries that are None or NaN pass through as None and are excluded
    from the ranking, so a missing Fisher p does not shift everyone else's q.

    Verified against statsmodels.stats.multitest.multipletests(method='fdr_bh').
    """
    indexed = [(i, p) for i, p in enumerate(p_values)
               if p is not None and p == p]
    out: list[Optional[float]] = [None] * len(p_values)
    m = len(indexed)
    if m == 0:
        return out

    indexed.sort(key=lambda t: t[1])
    # Walk from the largest p downward, enforcing monotonicity.
    prev = 1.0
    for rank in range(m, 0, -1):
        idx, p = indexed[rank - 1]
        q = min(prev, p * m / rank)
        out[idx] = max(0.0, min(1.0, q))
        prev = q
    return out


def newcombe_diff_ci(a1: int, n1: int, a2: int, n2: int,
                     alpha: float = 0.05) -> RatioCI:
    """Newcombe hybrid-score interval for a difference of two proportions.

    Newcombe RG. Interval estimation for the difference between independent
    proportions: comparison of eleven methods. Stat Med 1998;17(8):873-90
    (method 10).

    The naive interval (lo1 - hi2, hi1 - lo2) used previously simply subtracts
    the two Wilson bounds, which over-covers badly and can extend outside
    [-1, 1]. The hybrid-score form combines them in quadrature and keeps
    nominal coverage near 95% even with small or extreme cells.
    """
    z = _z_for_alpha(alpha)
    w1 = wilson_ci(a1, n1, alpha=alpha)
    w2 = wilson_ci(a2, n2, alpha=alpha)
    if n1 <= 0 or n2 <= 0:
        return RatioCI(point=float("nan"), lo=float("nan"), hi=float("nan"))

    diff = w1.p - w2.p
    lo = diff - math.sqrt((w1.p - w1.lo) ** 2 + (w2.hi - w2.p) ** 2)
    hi = diff + math.sqrt((w1.hi - w1.p) ** 2 + (w2.p - w2.lo) ** 2)
    return RatioCI(point=diff, lo=max(-1.0, lo), hi=min(1.0, hi))


def ema_signal(a: int, prr_point: float, chi2: float,
               min_a: int = 3, min_prr: float = 2.0, min_chi2: float = 4.0) -> bool:
    """EMA-2008 screening rule used by signal-detection systems.

    A "signal" is hypothesis-generating only; it does not constitute evidence
    of causation.
    """
    if a < min_a or not (prr_point == prr_point):  # NaN check
        return False
    return prr_point >= min_prr and chi2 >= min_chi2


# ---------------------------------------------------------------------------
# Trend tests
# ---------------------------------------------------------------------------

def cochran_armitage_trend(
    successes: Sequence[int],
    totals: Sequence[int],
    scores: Optional[Sequence[float]] = None,
) -> TrendResult:
    """Cochran-Armitage test for trend in proportions across ordered groups.

    successes[i] / totals[i] are the proportions; scores[i] are the ordered
    group scores (defaults to 0, 1, 2, ...). Returns z and two-sided p.
    """
    if len(successes) != len(totals):
        raise ValueError("successes and totals must be same length")
    n_groups = len(successes)
    if n_groups < 2:
        return TrendResult("cochran-armitage", 0.0, None, "no trend",
                          "Need ≥2 groups.")
    if scores is None:
        scores = list(range(n_groups))
    if len(scores) != n_groups:
        raise ValueError("scores must match group count")

    N = sum(totals)
    R = sum(successes)
    if N == 0 or R == 0 or R == N:
        return TrendResult("cochran-armitage", 0.0, None, "no trend",
                          "Degenerate (no events or all events).")

    p_bar = R / N
    s_bar = sum(s * n for s, n in zip(scores, totals)) / N

    numer = sum(s * (r - n * p_bar) for s, r, n in zip(scores, successes, totals))
    denom_var = p_bar * (1 - p_bar) * sum(n * (s - s_bar) ** 2
                                          for s, n in zip(scores, totals))
    if denom_var <= 0:
        return TrendResult("cochran-armitage", 0.0, None, "no trend",
                          "Zero variance.")
    z = numer / math.sqrt(denom_var)
    p = _two_sided_z_pvalue(z)
    direction = "increasing" if z > 0 else ("decreasing" if z < 0 else "no trend")
    return TrendResult("cochran-armitage", z, p, direction,
                      f"z={z:.3f}, p={p:.4g}")


def mann_kendall(values: Sequence[float]) -> TrendResult:
    """Mann-Kendall non-parametric trend test on a time-ordered series.

    Returns S statistic, two-sided p-value, and direction. Good for short
    yearly count series where you don't want to assume normality.
    """
    n = len(values)
    if n < 4:
        return TrendResult("mann-kendall", 0.0, None, "no trend",
                          "Need ≥4 observations.")
    S = 0
    for i in range(n - 1):
        for j in range(i + 1, n):
            d = values[j] - values[i]
            if d > 0:
                S += 1
            elif d < 0:
                S -= 1

    # Variance with the standard tie correction. v4 documented a tie
    # correction but implemented the no-ties form, which understates the
    # variance and therefore overstates significance whenever the yearly
    # series repeats a value — common in low-count cohorts.
    from collections import Counter
    tie_adj = sum(t * (t - 1) * (2 * t + 5)
                  for t in Counter(values).values() if t > 1)
    var_s = (n * (n - 1) * (2 * n + 5) - tie_adj) / 18.0
    if var_s <= 0:
        return TrendResult("mann-kendall", S, None, "no trend",
                          "Zero variance (all observations tied).")
    if S > 0:
        z = (S - 1) / math.sqrt(var_s)
    elif S < 0:
        z = (S + 1) / math.sqrt(var_s)
    else:
        z = 0.0
    p = _two_sided_z_pvalue(z)
    direction = "increasing" if z > 0 else ("decreasing" if z < 0 else "no trend")
    return TrendResult("mann-kendall", S, p, direction,
                      f"S={S}, z={z:.3f}, p={p:.4g}")


def _two_sided_z_pvalue(z: float) -> float:
    """Two-sided normal p-value for a z statistic.

    Uses the survival function rather than 1 - cdf. The v4 form underflowed to
    exactly 0.0 once |z| exceeded about 8.3, because 1 - cdf loses every
    significant digit there; erfc and scipy's sf stay accurate to ~1e-300.
    """
    if not (z == z):  # NaN
        return float("nan")
    if _HAS_SCIPY:
        return float(2 * _scipy_stats.norm.sf(abs(z)))
    return math.erfc(abs(z) / math.sqrt(2))


def chi2_sf(x: float, df: int) -> float:
    """Upper tail of the chi-square distribution, P(X²_df >= x).

    Pure-Python regularised incomplete gamma so that chi-square p-values
    survive a SciPy-less install instead of silently becoming None. Agrees
    with scipy.stats.chi2.sf to ~1e-12 over the range this tool produces.
    """
    if not (x == x) or not (df == df) or df <= 0:
        return float("nan")
    if x <= 0:
        return 1.0
    s, t = df / 2.0, x / 2.0
    if t < s + 1:
        # Series expansion for the lower tail, then complement.
        total = term = 1.0 / s
        for k in range(1, 1000):
            term *= t / (s + k)
            total += term
            if abs(term) < abs(total) * 1e-16:
                break
        return 1.0 - total * math.exp(-t + s * math.log(t) - math.lgamma(s))
    # Continued fraction (modified Lentz) for the upper tail.
    tiny = 1e-300
    b = t + 1 - s
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - s)
        b += 2
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-16:
            break
    return math.exp(-t + s * math.log(t) - math.lgamma(s)) * h


# ---------------------------------------------------------------------------
# χ² test for k×2 tables (used by subgroup analysis)
# ---------------------------------------------------------------------------

def chi2_independence(table: Sequence[Sequence[int]]) -> tuple[float, Optional[float], int]:
    """Pearson chi-square test of independence. Returns (chi2, p, df).

    No continuity correction (only valid for 2×2 tables anyway).

    Use `min_expected_cell(table)` alongside this: the asymptotic p-value is
    unreliable when any expected cell count falls below 5 (Cochran's rule),
    which happens readily in subgroup analyses of rare outcomes.
    """
    rows = len(table)
    if rows == 0:
        return 0.0, None, 0
    cols = len(table[0])
    if any(len(r) != cols for r in table):
        raise ValueError("All rows must have same number of columns.")
    n = sum(sum(r) for r in table)
    if n == 0:
        return 0.0, None, 0
    row_tot = [sum(r) for r in table]
    col_tot = [sum(table[i][j] for i in range(rows)) for j in range(cols)]
    chi2 = 0.0
    for i in range(rows):
        for j in range(cols):
            exp = row_tot[i] * col_tot[j] / n
            if exp > 0:
                chi2 += (table[i][j] - exp) ** 2 / exp
    df = (rows - 1) * (cols - 1)
    if df <= 0:
        return chi2, None, df
    # sf via SciPy when available, else the pure-Python implementation, so a
    # SciPy-less install still reports a p-value instead of a blank.
    if _HAS_SCIPY:
        try:
            return chi2, float(_scipy_stats.chi2.sf(chi2, df)), df
        except Exception:
            pass
    return chi2, chi2_sf(chi2, df), df


def min_expected_cell(table: Sequence[Sequence[int]]) -> float:
    """Smallest expected cell count under independence.

    Below 5, the chi-square approximation should not be trusted; report
    Fisher's exact instead (or collapse sparse strata).
    """
    rows = len(table)
    if rows == 0:
        return 0.0
    cols = len(table[0])
    n = sum(sum(r) for r in table)
    if n == 0:
        return 0.0
    row_tot = [sum(r) for r in table]
    col_tot = [sum(table[i][j] for i in range(rows)) for j in range(cols)]
    return min(row_tot[i] * col_tot[j] / n
               for i in range(rows) for j in range(cols))
