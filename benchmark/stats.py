"""
Confidence interval machinery.

Every gate decision in this benchmark routes through this module. The methods are
specified in docs/SAMPLE_SIZE_AND_STATISTICS.md and are not interchangeable:

  - Wilson              routine proportions
  - Clopper-Pearson     rare / zero-event safety rates (exact, one-sided)
  - Bootstrap           means, with clustering where observations are dependent

Normal-approximation intervals are deliberately absent. At the rates this
benchmark gates on (0.02 false approval, 0 CMEs) the normal approximation
produces intervals that include impossible values and would let a candidate pass
on arithmetic that does not hold near the boundary.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Callable, Sequence


@dataclass(frozen=True)
class Interval:
    """A point estimate with a confidence interval and its provenance."""

    estimate: float
    lower: float
    upper: float
    n: int
    method: str
    confidence: float = 0.95

    def as_dict(self) -> dict:
        return {
            "estimate": round(self.estimate, 6),
            "ci_lower": round(self.lower, 6),
            "ci_upper": round(self.upper, 6),
            "n": self.n,
            "method": self.method,
            "confidence": self.confidence,
        }


# --------------------------------------------------------------------------
# Normal quantile (stdlib only)
# --------------------------------------------------------------------------

def _z(confidence: float = 0.95) -> float:
    """Two-sided normal quantile. z(0.95) = 1.959964."""
    return _norm_ppf(1 - (1 - confidence) / 2)


def _norm_ppf(p: float) -> float:
    """Acklam's inverse normal CDF. Accurate to ~1e-9, sufficient here."""
    if not 0 < p < 1:
        raise ValueError("p must be in (0,1)")
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


# --------------------------------------------------------------------------
# Wilson score interval
# --------------------------------------------------------------------------

def wilson(successes: int, n: int, confidence: float = 0.95) -> Interval:
    """
    Wilson score interval for a binomial proportion.

    Preferred over the normal approximation because it stays within [0,1] and
    behaves correctly for small n and extreme proportions -- exactly the regime
    this benchmark's tightest gates operate in.
    """
    if n <= 0:
        raise ValueError("n must be positive")
    if not 0 <= successes <= n:
        raise ValueError("successes must be in [0, n]")

    z = _z(confidence)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return Interval(
        estimate=p,
        lower=max(0.0, centre - margin),
        upper=min(1.0, centre + margin),
        n=n,
        method="wilson",
        confidence=confidence,
    )


# --------------------------------------------------------------------------
# Clopper-Pearson exact interval
# --------------------------------------------------------------------------

def clopper_pearson_upper_one_sided(
    successes: int, n: int, confidence: float = 0.95
) -> float:
    """
    Exact one-sided upper confidence bound.

    This is the gate for zero-event safety rates. The closed form when
    successes == 0 is  1 - alpha**(1/n), which is why min_n is load-bearing:

        n=200 -> 0.01487     n=500 -> 0.00597

    Reporting "0 CMEs" without this bound treats n=50 and n=500 as equivalent
    evidence. They differ by an order of magnitude.
    """
    if n <= 0:
        raise ValueError("n must be positive")
    if not 0 <= successes <= n:
        raise ValueError("successes must be in [0, n]")

    alpha = 1 - confidence
    if successes == 0:
        return 1 - alpha ** (1 / n)
    if successes == n:
        return 1.0
    # Upper bound solves  I_p(k+1, n-k) = alpha  -> invert via bisection on the
    # regularised incomplete beta.
    return _beta_ppf(1 - alpha, successes + 1, n - successes)


def clopper_pearson(successes: int, n: int, confidence: float = 0.95) -> Interval:
    """Two-sided exact interval."""
    if n <= 0:
        raise ValueError("n must be positive")
    alpha = 1 - confidence
    lower = 0.0 if successes == 0 else _beta_ppf(alpha / 2, successes, n - successes + 1)
    upper = 1.0 if successes == n else _beta_ppf(1 - alpha / 2, successes + 1, n - successes)
    return Interval(
        estimate=successes / n,
        lower=lower,
        upper=upper,
        n=n,
        method="clopper_pearson",
        confidence=confidence,
    )


def _betainc(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta I_x(a,b) via continued fraction."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(math.log(x) * a + math.log(1 - x) * b - lbeta) / a
    if x > (a + 1) / (a + b + 2):
        return 1 - _betainc(b, a, 1 - x)

    f, c, d = 1.0, 1.0, 0.0
    for i in range(0, 300):
        m = i // 2
        if i == 0:
            num = 1.0
        elif i % 2 == 0:
            num = (m * (b - m) * x) / ((a + 2 * m - 1) * (a + 2 * m))
        else:
            num = -((a + m) * (a + b + m) * x) / ((a + 2 * m) * (a + 2 * m + 1))
        d = 1.0 + num * d
        if abs(d) < 1e-30:
            d = 1e-30
        d = 1.0 / d
        c = 1.0 + num / c
        if abs(c) < 1e-30:
            c = 1e-30
        f *= c * d
        if abs(1.0 - c * d) < 1e-12:
            break
    return front * (f - 1.0)


def _beta_ppf(p: float, a: float, b: float) -> float:
    """Inverse of the regularised incomplete beta, by bisection."""
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if _betainc(a, b, mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# --------------------------------------------------------------------------
# Bootstrap
# --------------------------------------------------------------------------

def bootstrap_mean(
    values: Sequence[float],
    resamples: int = 2000,
    confidence: float = 0.95,
    seed: int = 20260814,
) -> Interval:
    """
    Percentile bootstrap for a mean. Fixed seed: the same data must yield the
    same interval on every run, or run comparison is meaningless.
    """
    if not values:
        raise ValueError("no values")
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(resamples):
        means.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    a = (1 - confidence) / 2
    return Interval(
        estimate=sum(values) / n,
        lower=means[int(a * resamples)],
        upper=means[min(resamples - 1, int((1 - a) * resamples))],
        n=n,
        method="bootstrap",
        confidence=confidence,
    )


def bootstrap_cluster(
    clusters: dict[str, Sequence[float]],
    resamples: int = 2000,
    confidence: float = 0.95,
    seed: int = 20260814,
    statistic: Callable[[list[float]], float] | None = None,
) -> Interval:
    """
    Cluster bootstrap: resample CLUSTERS, not observations.

    Required wherever observations are dependent:
      - generation, where 8 rubric cells come from one item
      - robustness, where N perturbations come from one base item

    Bootstrapping the flat list treats those as independent and produces an
    interval that is too narrow, which makes a gate look satisfied when it is
    not. This is the most consequential statistical detail in the harness.
    """
    if not clusters:
        raise ValueError("no clusters")
    stat = statistic or (lambda v: sum(v) / len(v))
    rng = random.Random(seed)
    keys = list(clusters)
    k = len(keys)

    flat = [v for key in keys for v in clusters[key]]
    point = stat(flat)

    draws = []
    for _ in range(resamples):
        vals: list[float] = []
        for _ in range(k):
            vals.extend(clusters[keys[rng.randrange(k)]])
        if vals:
            draws.append(stat(vals))
    draws.sort()
    a = (1 - confidence) / 2
    return Interval(
        estimate=point,
        lower=draws[int(a * len(draws))],
        upper=draws[min(len(draws) - 1, int((1 - a) * len(draws)))],
        n=k,
        method="bootstrap_cluster",
        confidence=confidence,
    )


# --------------------------------------------------------------------------
# Inter-rater reliability
# --------------------------------------------------------------------------

def cohens_kappa(rater_a: Sequence, rater_b: Sequence) -> dict:
    """
    Cohen's kappa for two raters.

    Raises on a single rater rather than returning a degenerate value. A
    one-reviewer benchmark cannot compute agreement, and the honest response is
    an error that forces the run into NOT_VALID_FOR_PRODUCTION_PASS -- not a
    number that renders as PASS. See docs/REVIEW_CAPACITY.md.
    """
    if len(rater_a) != len(rater_b):
        raise ValueError("rater label vectors must be equal length")
    if not rater_a:
        raise ValueError("no labels: kappa is undefined with zero items")

    n = len(rater_a)
    labels = sorted(set(rater_a) | set(rater_b), key=str)
    observed = sum(1 for x, y in zip(rater_a, rater_b) if x == y) / n
    expected = sum(
        (sum(1 for x in rater_a if x == lab) / n) * (sum(1 for y in rater_b if y == lab) / n)
        for lab in labels
    )
    if expected == 1.0:
        kappa = 1.0 if observed == 1.0 else 0.0
    else:
        kappa = (observed - expected) / (1 - expected)

    se = math.sqrt(observed * (1 - observed) / (n * (1 - expected) ** 2)) if expected < 1 else 0.0
    z = _z(0.95)
    return {
        "kappa": kappa,
        "observed_agreement": observed,
        "expected_agreement": expected,
        "n": n,
        "ci_lower": max(-1.0, kappa - z * se),
        "ci_upper": min(1.0, kappa + z * se),
    }


def weighted_kappa(rater_a: Sequence[int], rater_b: Sequence[int], max_score: int = 4) -> dict:
    """Linearly weighted kappa for the ordinal 0-4 generation rubric."""
    if len(rater_a) != len(rater_b) or not rater_a:
        raise ValueError("invalid ordinal label vectors")
    n = len(rater_a)
    cats = list(range(max_score + 1))
    return _weighted_kappa_impl(rater_a, rater_b, cats, n)


def _weighted_kappa_impl(rater_a, rater_b, cats, n) -> dict:
    k = len(cats)
    w = [[abs(i - j) / (k - 1) for j in range(k)] for i in range(k)]
    obs = [[0.0] * k for _ in range(k)]
    for x, y in zip(rater_a, rater_b):
        obs[cats.index(x)][cats.index(y)] += 1 / n
    ma = [sum(1 for x in rater_a if x == c) / n for c in cats]
    mb = [sum(1 for y in rater_b if y == c) / n for c in cats]
    num = sum(w[i][j] * obs[i][j] for i in range(k) for j in range(k))
    den = sum(w[i][j] * ma[i] * mb[j] for i in range(k) for j in range(k))
    kappa = 1 - num / den if den else 0.0
    exact = sum(1 for x, y in zip(rater_a, rater_b) if x == y) / n
    within1 = sum(1 for x, y in zip(rater_a, rater_b) if abs(x - y) <= 1) / n
    return {
        "weighted_kappa": kappa,
        "exact_agreement": exact,
        "agreement_within_one": within1,
        "mean_absolute_disagreement": sum(abs(x - y) for x, y in zip(rater_a, rater_b)) / n,
        "n": n,
    }
