"""Distribution contracts use known quantiles, not implementation replicas."""
import math

import pytest

from epsm_workflows.distributions import (
    categorical, discrete, lognormal, normal, triangular, uniform, weibull,
    distribution_from_dict, sample_quantile,
)


@pytest.mark.parametrize("distribution,median", [
    (normal(mean=3, std=2), 3),
    (weibull(shape=2, scale=4), 4 * math.sqrt(math.log(2))),
    (lognormal(mean_log=1, std_log=0.5), math.e),
    (triangular(mode=2), 2),
    (uniform(), 2),
])
def test_known_medians_and_roundtrip(distribution, median):
    limits = (0, 4) if distribution.kind in {"uniform", "triangular"} else (None, None)
    assert sample_quantile(distribution, 0.5, lower=limits[0], upper=limits[1]) == pytest.approx(median)
    assert distribution_from_dict(distribution.model_dump()) == distribution


def test_truncated_normal_is_conditioned_not_clipped():
    values = [sample_quantile(normal(mean=0, std=1), p, lower=0, upper=2)
              for p in (0.01, 0.2, 0.5, 0.8, 0.99)]
    assert all(0 < value < 2 for value in values)
    assert values == sorted(values)
    assert sample_quantile(normal(mean=0, std=1), 0.5, lower=-1, upper=1) == pytest.approx(0)
    with pytest.raises(ValueError, match="outside"):
        sample_quantile(normal(mean=0, std=1), 0.01, lower=0, upper=2, bounds_policy="validate")


def test_discrete_and_categorical_weighted_choices():
    assert [sample_quantile(categorical(probabilities=[0.25, 0.75]), p,
                            choices=["F", "FTX"]) for p in (0.1, 0.3, 0.9)] == ["F", "FTX", "FTX"]
    assert [sample_quantile(discrete(), p, lower=1, upper=3) for p in (0.1, 0.5, 0.9)] == [1, 2, 3]
    with pytest.raises(ValueError, match="probabilities"):
        categorical(probabilities=[0.2, 0.2])


@pytest.mark.parametrize("factory,kwargs", [
    (normal, {"std": 0}), (weibull, {"shape": -1}),
    (lognormal, {"std_log": 0}), (normal, {"mean": float("nan")}),
])
def test_invalid_distribution_parameters(factory, kwargs):
    with pytest.raises(ValueError):
        factory(**kwargs)


def test_invalid_domains_and_unsupported_distributions():
    with pytest.raises(ValueError):
        distribution_from_dict({"kind": "made-up"})
    with pytest.raises(ValueError):
        sample_quantile(uniform(), 0.5, lower=2, upper=1)
    with pytest.raises(ValueError):
        sample_quantile(weibull(shape=2, scale=1), 0.5, lower=-2, upper=-1)
