"""
maude_stats.py — publication-grade statistical helpers for MAUDE-Dash v4
========================================================================

All functions return either a NamedTuple or a plain dict so they are easy to
consume from Streamlit, log, and serialize.

Implementations follow standard pharmacovigilance and biostatistics
references; scipy is used for exact tests where appropriate but everything
falls back gracefully if scipy is missing.

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
    n = A + B + C + D
    n1 = A + B   # exposed total
    n2 = C + D   # unexposed total

    # PRR
    if (A + B) > 0 and (C + D) > 0:
        prr = (A / (A + B)) / (C / (C + D))
        se_ln_prr = math.sqrt((1 / A) - (1 / (A + B)) + (1 / C) - (1 / (C + D)))
        ln_prr = math.log(prr) if prr > 0 else float("nan")
        prr_lo = math.exp(ln_prr - 1.96 * se_ln_prr) if not math.isnan(ln_prr) else float("nan")
        prr_hi = math.exp(ln_prr + 1.96 * se_ln_prr) if not math.isnan(ln_prr) else float("nan")
    else:
        prr = prr_lo = prr_hi = float("nan")

    # ROR
    if B > 0 and C > 0:
        ror = (A * D) / (B * C)
        se_ln_ror = math.sqrt((1 / A) + (1 / B) + (1 / C) + (1 / D))
        ln_ror = math.log(ror) if ror > 0 else float("nan")
        ror_lo = math.exp(ln_ror - 1.96 * se_ln_ror) if not math.isnan(ln_ror) else float("nan")
        ror_hi = math.exp(ln_ror + 1.96 * se_ln_ror) if not math.isnan(ln_ror) else float("nan")
    else:
        ror = ror_lo = ror_hi = float("nan")

    # Yates χ² (Mantel-Haenszel form, with continuity correction)
    if n > 0 and n1 > 0 and n2 > 0 and (A + C) > 0 and (B + D) > 0:
        chi2_yates = ((abs(A * D - B * C) - n / 2) ** 2 * n) / (n1 * n2 * (A + C) * (B + D))
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
    # Variance (with tie correction)
    # ties: groups of equal values; we don't have many in count data, so
    # this is the no-ties form. For count series with ties, results are
    # approximate; treat S only as descriptive.
    var_s = n * (n - 1) * (2 * n + 5) / 18.0
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
    """Two-sided normal p-value for a z statistic."""
    if _HAS_SCIPY:
        return float(2 * (1 - _scipy_stats.norm.cdf(abs(z))))
    # erfc-based fallback
    return math.erfc(abs(z) / math.sqrt(2))


# ---------------------------------------------------------------------------
# χ² test for k×2 tables (used by subgroup analysis)
# ---------------------------------------------------------------------------

def chi2_independence(table: Sequence[Sequence[int]]) -> tuple[float, Optional[float], int]:
    """Pearson chi-square test of independence. Returns (chi2, p, df).

    No continuity correction (only valid for 2×2 tables anyway).
    """
    rows = len(table)
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
    p: Optional[float] = None
    if _HAS_SCIPY:
        try:
            p = float(1 - _scipy_stats.chi2.cdf(chi2, df))
        except Exception:
            p = None
    return chi2, p, df
