"""Sample size, error bars and minimum detectable effects for the test suite.

Formulas (see README "可比性" for derivations and sources):

* Standard error of a mean score (CLT; Miller 2024, Eq. 1):
      SE = sqrt( sum_i (s_i - mean)^2 / (n - 1) / n )
* Clustered SE when questions share a source passage/document (Miller 2024, Eq. 4):
      SE_c = sqrt( SE^2 + (1/n^2) * sum_c sum_{i != j in c} (s_i - mean)(s_j - mean) )
* Paired comparison of systems A and B on the same questions (Miller 2024, §4.2):
      d_i = s_A,i - s_B,i ;  SE_d = sd(d) / sqrt(n)
* Questions needed to detect a paired difference delta (Miller 2024, Eq. 9 with
  deterministic retrieval, i.e. no per-question resampling variance):
      n = (z_{1-alpha/2} + z_{1-beta})^2 * omega^2 / delta^2 ,  omega^2 = Var(d_i)
* Minimum detectable effect for a given n (same equation solved for delta):
      MDE = (z_{1-alpha/2} + z_{1-beta}) * omega / sqrt(n)
* Questions needed to estimate one proportion within +/- E (normal approximation):
      n = z_{1-alpha/2}^2 * p (1 - p) / E^2
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from statistics import NormalDist

Z = NormalDist()


def z(q: float) -> float:
    return Z.inv_cdf(q)


def mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def se_mean(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return float("nan")
    mu = mean(xs)
    return math.sqrt(sum((x - mu) ** 2 for x in xs) / (n - 1) / n)


def clustered_se(xs: Sequence[float], clusters: Sequence[str]) -> float:
    n = len(xs)
    if n < 2:
        return float("nan")
    mu = mean(xs)
    base = se_mean(xs) ** 2
    groups: dict[str, list[float]] = {}
    for x, c in zip(xs, clusters, strict=True):
        groups.setdefault(c, []).append(x - mu)
    cross = 0.0
    for devs in groups.values():
        total = sum(devs)
        cross += total * total - sum(d * d for d in devs)  # sum over i != j
    return math.sqrt(max(0.0, base + cross / n**2))


def ci95(xs: Sequence[float], se: float | None = None) -> tuple[float, float]:
    se = se_mean(xs) if se is None else se
    mu = mean(xs)
    return mu - 1.96 * se, mu + 1.96 * se


def paired(a: Sequence[float], b: Sequence[float]) -> dict:
    d = [x - y for x, y in zip(a, b, strict=True)]
    n = len(d)
    sd = math.sqrt(sum((x - mean(d)) ** 2 for x in d) / (n - 1)) if n > 1 else float("nan")
    se = sd / math.sqrt(n) if n > 1 else float("nan")
    return {"n": n, "delta": mean(d), "omega": sd, "se": se,
            "ci95": (mean(d) - 1.96 * se, mean(d) + 1.96 * se)}


def required_n_paired(omega: float, delta: float, alpha: float = 0.05, power: float = 0.8) -> int:
    if delta <= 0:
        raise ValueError("delta must be positive")
    return math.ceil((z(1 - alpha / 2) + z(power)) ** 2 * omega**2 / delta**2)


def mde_paired(omega: float, n: int, alpha: float = 0.05, power: float = 0.8) -> float:
    return (z(1 - alpha / 2) + z(power)) * omega / math.sqrt(n)


def required_n_proportion(margin: float, p: float = 0.5, alpha: float = 0.05) -> int:
    return math.ceil(z(1 - alpha / 2) ** 2 * p * (1 - p) / margin**2)


def design_effect(se_clustered: float, se_iid: float) -> float:
    """Kish design effect: variance inflation from clustering; n_eff = n / deff."""
    return (se_clustered / se_iid) ** 2 if se_iid else float("nan")
