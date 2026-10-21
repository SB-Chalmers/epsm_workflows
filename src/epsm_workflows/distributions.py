# SPDX-License-Identifier: MIT
"""Portable independent distributions for study input domains.

Constructors perform no sampling or I/O. Quantiles use a caller's probability,
so LHS and Monte Carlo share the same marginal definitions and local RNGs.
Numeric bounds condition a distribution when ``bounds_policy='truncate'``;
``validate`` instead reports values outside the declared domain. Unknown or
invalid values are never silently clipped or replaced.
"""
from __future__ import annotations

import math
from statistics import NormalDist
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Distribution(BaseModel):
    """Serializable marginal definition; parameter units follow its input."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    kind: str


class UniformDistribution(Distribution):
    kind: Literal["uniform"] = "uniform"


class NormalDistribution(Distribution):
    kind: Literal["normal"] = "normal"
    mean: float = 0
    std: float = Field(default=1, gt=0)


class WeibullDistribution(Distribution):
    kind: Literal["weibull"] = "weibull"
    shape: float = Field(default=1, gt=0)
    scale: float = Field(default=1, gt=0)


class TriangularDistribution(Distribution):
    kind: Literal["triangular"] = "triangular"
    mode: float


class LognormalDistribution(Distribution):
    kind: Literal["lognormal"] = "lognormal"
    mean_log: float = 0
    std_log: float = Field(default=1, gt=0)


class DiscreteDistribution(Distribution):
    kind: Literal["discrete"] = "discrete"
    probabilities: tuple[float, ...] | None = None

    @model_validator(mode="after")
    def _weights(self):
        if self.probabilities is not None:
            if (not self.probabilities or any(not math.isfinite(x) or x < 0 for x in self.probabilities)
                    or not math.isclose(sum(self.probabilities), 1, abs_tol=1e-10)):
                raise ValueError("probabilities must be finite, nonnegative and sum to one")
        return self


class CategoricalDistribution(DiscreteDistribution):
    kind: Literal["categorical"] = "categorical"


Marginal = Annotated[Union[UniformDistribution, NormalDistribution, WeibullDistribution,
                          TriangularDistribution, LognormalDistribution,
                          DiscreteDistribution, CategoricalDistribution], Field(discriminator="kind")]

_DISTRIBUTIONS = {cls.model_fields["kind"].default: cls for cls in (
    UniformDistribution, NormalDistribution, WeibullDistribution, TriangularDistribution,
    LognormalDistribution, DiscreteDistribution, CategoricalDistribution)}


def uniform() -> Distribution:
    """Uniform probability over the input's finite bounds or choices."""
    return UniformDistribution()


def normal(*, mean: float = 0, std: float = 1) -> Distribution:
    """Normal marginal, with mean and positive standard deviation in input units."""
    return NormalDistribution(mean=mean, std=std)


def weibull(*, shape: float = 1, scale: float = 1) -> Distribution:
    """Weibull marginal with positive dimensionless shape and input-unit scale."""
    return WeibullDistribution(shape=shape, scale=scale)


def triangular(*, mode: float) -> Distribution:
    """Triangular marginal with its mode between the input's declared bounds."""
    return TriangularDistribution(mode=mode)


def lognormal(*, mean_log: float = 0, std_log: float = 1) -> Distribution:
    """Lognormal marginal parameterized by the mean/std of its natural logarithm."""
    return LognormalDistribution(mean_log=mean_log, std_log=std_log)


def discrete(*, probabilities=None) -> Distribution:
    """Discrete integer/choice distribution; omitted weights mean equal probability."""
    return DiscreteDistribution(probabilities=probabilities)


def categorical(*, probabilities=None) -> Distribution:
    """Distribution over named choices; categories have no numerical distance."""
    return CategoricalDistribution(probabilities=probabilities)


def distribution_from_dict(value) -> Distribution:
    """Validate a saved marginal without sampling it."""
    if isinstance(value, Distribution) and type(value) in _DISTRIBUTIONS.values():
        return value
    if not isinstance(value, dict) or value.get("kind") not in _DISTRIBUTIONS:
        raise ValueError("Unsupported distribution kind")
    return _DISTRIBUTIONS[value["kind"]].model_validate(value)


def _cdf(distribution, value):
    if distribution.kind == "normal":
        return NormalDist(distribution.mean, distribution.std).cdf(value)
    if distribution.kind == "lognormal":
        return 0.0 if value <= 0 else NormalDist(distribution.mean_log, distribution.std_log).cdf(math.log(value))
    return 0.0 if value <= 0 else -math.expm1(-(value / distribution.scale) ** distribution.shape)


def _inverse(distribution, probability):
    p = max(math.nextafter(0.0, 1.0), min(math.nextafter(1.0, 0.0), probability))
    if distribution.kind == "normal":
        return NormalDist(distribution.mean, distribution.std).inv_cdf(p)
    if distribution.kind == "lognormal":
        return math.exp(NormalDist(distribution.mean_log, distribution.std_log).inv_cdf(p))
    return distribution.scale * (-math.log1p(-p)) ** (1 / distribution.shape)


def sample_quantile(distribution, probability, *, lower=None, upper=None,
                    choices=None, bounds_policy="truncate"):
    """Map an open-unit probability into a validated domain.

    Parameters
    ----------
    distribution : Distribution or mapping
        Portable marginal definition.
    probability : float
        Probability strictly between zero and one.
    lower, upper : float, optional
        Domain bounds in the input's units; integer bounds for discrete domains.
    choices : sequence, optional
        Finite categorical or explicit discrete values.
    bounds_policy : {'truncate', 'validate'}
        Condition within bounds or validate a full-distribution draw.

    Returns
    -------
    float, int or supplied choice
        One value. Invalid domains and out-of-domain draws raise ValueError.
    """
    d = distribution_from_dict(distribution)
    if not 0 < probability < 1 or bounds_policy not in {"truncate", "validate"}:
        raise ValueError("probability must be inside (0,1) and bounds_policy must be truncate or validate")
    if (lower is not None and not math.isfinite(lower)) or (upper is not None and not math.isfinite(upper)):
        raise ValueError("bounds must be finite")
    if lower is not None and upper is not None and lower > upper:
        raise ValueError("lower bound exceeds upper bound")
    if d.kind in {"discrete", "categorical"} or choices is not None:
        if d.kind not in {"discrete", "categorical", "uniform"}:
            raise ValueError("categorical choices require a categorical, discrete or uniform distribution")
        if choices is None:
            if lower is None or upper is None or int(lower) != lower or int(upper) != upper:
                raise ValueError("discrete domains need integer bounds or choices")
            choices = range(int(lower), int(upper) + 1)
        if not len(choices):
            raise ValueError("choices must not be empty")
        weights = getattr(d, "probabilities", None) or (1 / len(choices),) * len(choices)
        if len(weights) != len(choices):
            raise ValueError("probabilities must match choices")
        cumulative = 0.0
        for value, weight in zip(choices, weights):
            cumulative += weight
            if probability < cumulative:
                return value
        return choices[-1]
    if d.kind in {"uniform", "triangular"}:
        if lower is None or upper is None or lower >= upper:
            raise ValueError("uniform and triangular domains need increasing finite bounds")
        if d.kind == "uniform":
            return lower + probability * (upper - lower)
        if d.mode is None or not lower <= d.mode <= upper:
            raise ValueError("triangular mode must be inside its bounds")
        threshold = (d.mode - lower) / (upper - lower)
        if probability < threshold:
            return lower + math.sqrt(probability * (upper - lower) * (d.mode - lower))
        return upper - math.sqrt((1 - probability) * (upper - lower) * (upper - d.mode))
    p = probability
    if bounds_policy == "truncate":
        a = 0 if lower is None else _cdf(d, lower)
        b = 1 if upper is None else _cdf(d, upper)
        if not a < b:
            raise ValueError("distribution has no resolvable probability inside bounds")
        p = a + probability * (b - a)
    value = _inverse(d, p)
    if not math.isfinite(value) or (lower is not None and value < lower) or (upper is not None and value > upper):
        raise ValueError("sample is outside the declared bounds")
    return value
