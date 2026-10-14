"""Reusable finite grids, Latin hypercubes and variance-based global sensitivity.

The sensitivity design uses independent uniform Monte Carlo base matrices and
Jansen first/total-order estimators. It does not claim a Sobol sequence or
support correlated parameters. No building-specific ranges are supplied.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass
from itertools import product

from ._inspection import inspection


@dataclass(frozen=True)
class Parameter:
    """One independent uniform parameter with explicit finite bounds and unit."""

    name: str
    lower: float
    upper: float
    unit: str = "1"

    def __repr__(self):
        return inspection(
            type(self).__name__,
            name=self.name,
            lower=self.lower,
            upper=self.upper,
            unit=self.unit,
        )

    __str__ = __repr__

    def summary(self):
        return {
            "name": self.name,
            "lower": self.lower,
            "upper": self.upper,
            "unit": self.unit,
        }

    def __post_init__(self):
        if not self.name or not self.unit:
            raise ValueError("Parameter name and unit are required")
        if (
            not math.isfinite(self.lower)
            or not math.isfinite(self.upper)
            or self.lower >= self.upper
        ):
            raise ValueError("Parameter bounds must be finite and increasing")

    def scale(self, value):
        return self.lower + value * (self.upper - self.lower)


def _parameters(parameters):
    values = tuple(parameters)
    if not values or len({p.name for p in values}) != len(values):
        raise ValueError("Supply parameters with unique names")
    return values


def grid(values):
    """Return the Cartesian product of explicitly supplied parameter choices."""
    names = tuple(values)
    choices = [tuple(values[name]) for name in names]
    if not names or any(not choice for choice in choices):
        raise ValueError("Every grid parameter needs at least one value")
    return [dict(zip(names, row)) for row in product(*choices)]


def latin_hypercube(parameters, n, *, seed=0):
    """Sample each equal-probability stratum once per independent parameter."""
    parameters = _parameters(parameters)
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ValueError("n must be a positive integer")
    rng = random.Random(seed)
    columns = []
    for parameter in parameters:
        values = [parameter.scale((index + rng.random()) / n) for index in range(n)]
        rng.shuffle(values)
        columns.append(values)
    return [dict(zip((p.name for p in parameters), row)) for row in zip(*columns)]


@dataclass(frozen=True)
class SensitivityDesign:
    parameters: tuple[Parameter, ...]
    n: int
    seed: int
    samples: tuple[dict, ...]

    def __repr__(self):
        return inspection(
            type(self).__name__,
            parameters=len(self.parameters),
            n=self.n,
            samples=len(self.samples),
            seed=self.seed,
        )

    __str__ = __repr__

    def summary(self):
        return {
            "method": "independent_uniform_jansen",
            "parameters": tuple(parameter.summary() for parameter in self.parameters),
            "base_samples": self.n,
            "samples": len(self.samples),
            "seed": self.seed,
        }

    def to_dict(self):
        from dataclasses import asdict

        return {"method": "independent_uniform_jansen", **asdict(self)}


def sensitivity_design(parameters, n, *, seed=0):
    """Prepare A, B and A-with-column-from-B blocks, in that fixed row order."""
    parameters = _parameters(parameters)
    if isinstance(n, bool) or not isinstance(n, int) or n < 2:
        raise ValueError("Sensitivity base size n must be at least two")
    rng = random.Random(seed)
    a = [{p.name: p.scale(rng.random()) for p in parameters} for _ in range(n)]
    b = [{p.name: p.scale(rng.random()) for p in parameters} for _ in range(n)]
    samples = a + b
    for parameter in parameters:
        samples.extend(
            {**left, parameter.name: right[parameter.name]} for left, right in zip(a, b)
        )
    return SensitivityDesign(parameters, n, seed, tuple(samples))


def analyse_sensitivity(design, values):
    """Estimate first/total-order indices; reject missing or constant responses.

    Estimates may fall outside [0, 1] through Monte Carlo error and are not
    clipped. Failed cases cannot be silently dropped because paired rows matter.
    """
    values = tuple(float(v) for v in values)
    if len(values) != len(design.samples) or not all(math.isfinite(v) for v in values):
        raise ValueError("Sensitivity needs one finite result per prepared row")
    n = design.n
    a, b = values[:n], values[n : 2 * n]
    variance = statistics.variance(a + b)
    if variance <= 0:
        raise ValueError("Sensitivity is undefined for zero response variance")
    output = {}
    for index, parameter in enumerate(design.parameters):
        mixed = values[(2 + index) * n : (3 + index) * n]
        total = sum((left - value) ** 2 for left, value in zip(a, mixed)) / (
            2 * n * variance
        )
        first = 1 - sum((right - value) ** 2 for right, value in zip(b, mixed)) / (
            2 * n * variance
        )
        output[parameter.name] = {
            "first_order": first,
            "total_order": total,
            "n": n,
            "response_variance": variance,
        }
    return output


def parametric_cases(
    building,
    samples,
    apply,
    *,
    engine="energyplus",
    weather=None,
    assessment=None,
    simulation_options=None,
):
    """Freeze each independently edited copy as an executable EPSM case.

    ``apply(copy, parameters)`` may mutate the copy and return None, or return a
    replacement building. Explicit transformations prevent an accidental change
    of a thermal coefficient being treated as a material renovation for LCA.
    """
    from .execution import PreparedEvaluation
    from .records import json_value

    cases = []
    for index, parameters in enumerate(samples):
        candidate = building.copy()
        replacement = apply(candidate, dict(parameters))
        if replacement is not None:
            candidate = replacement
        from pathlib import Path

        weather_data = json_value(weather)
        files = {}
        if isinstance(weather_data, dict) and "epw_path" in weather_data:
            files["weather"] = weather_data["epw_path"]
            weather_data = {**weather_data, "epw_path": {"$asset": "weather"}}
        elif isinstance(weather, (str, Path)):
            files["weather"] = str(weather)
            weather_data = {"$asset": "weather"}
        simulation = {
            "engine": engine,
            "weather": weather_data,
            **dict(simulation_options or {}),
        }
        if assessment is not None:
            simulation["assessment"] = json_value(assessment)
        cases.append(
            PreparedEvaluation(
                {"building": json_value(candidate), "simulation": simulation},
                inputs=dict(parameters),
                name=f"case-{index:06d}",
                files=files,
            )
        )
    return tuple(cases)
