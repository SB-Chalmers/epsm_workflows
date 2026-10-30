# SPDX-License-Identifier: MIT
"""Stratified continuous LHS with feasibility reassignment and balanced categories.

This is the sampler of the 2026-09 Vera 60k campaign. Each stratum (cohort, city,
footprint family) draws its own ``random-cd`` Latin hypercube from a hash-derived
seed, so adding a city or family never changes another stratum's rows. Area and
aspect ratio are log-uniform; footprint areas are permuted within the stratum so
every design can realise its cut geometry; categorical inputs are balanced.
"""
from __future__ import annotations

import math

import numpy as np
from scipy.optimize import linear_sum_assignment
from scipy.stats.qmc import LatinHypercube

from ..records import digest
from .geometry import minimum_area
from .study import FLOOR_CHOICES, SCHEDULE_FAMILIES, SHIFT_CHOICES, TOPOLOGIES

LOG_KEYS = ("footprint_area_m2", "aspect_ratio")


def seed_for(root, *keys):
    return int(digest([root, *keys])[:16], 16)


def balanced(values, n, rng):
    return rng.permutation(np.resize(np.array(values), n))


def cut_fractions(family, unit_l, unit_w):
    """Map unit cuts onto the family's feasible fraction window."""
    if family == "courtyard":
        return 0.25 + 0.20 * unit_l, 0.25 + 0.20 * unit_w
    return 0.20 + 0.25 * unit_l, 0.20 + 0.25 * unit_w


def stratum(seed, bounds, n, *, family=None, log_keys=LOG_KEYS):
    """Draw one stratum: sorted keys, unit hypercube, scaled values and its RNG.

    ``family`` enables the area reassignment that keeps every cut footprint
    buildable; ``None`` skips it. The hypercube's marginal strata are checked.
    """
    keys = sorted(bounds)
    rng = np.random.default_rng(seed)
    unit = LatinHypercube(d=len(keys), seed=seed, optimization="random-cd").random(n)
    values = np.column_stack(
        [
            lo * (hi / lo) ** unit[:, i] if key in log_keys else lo + (hi - lo) * unit[:, i]
            for i, (key, (lo, hi)) in enumerate((key, bounds[key]) for key in keys)
        ]
    )

    def column(key):
        return values[:, keys.index(key)]

    if family is not None and family != "real" and "footprint_area_m2" in bounds:
        cls, cws = cut_fractions(family, column("cut_l_unit"), column("cut_w_unit"))
        minima = np.array(
            [minimum_area(family, aspect, cl, cw) for aspect, cl, cw in zip(column("aspect_ratio"), cls, cws)]
        )
        costs = rng.random((n, n))
        costs[column("footprint_area_m2")[None, :] < minima[:, None]] = 1e9
        ii, jj = linear_sum_assignment(costs)
        if np.any(costs[ii, jj] >= 1e9):
            raise ValueError(f"Infeasible LHS area assignment for family {family}")
        index = keys.index("footprint_area_m2")
        unit[:, index] = unit[jj, index]
        values[:, index] = values[jj, index]
    if any(len(set(np.floor(unit[:, i] * n).astype(int))) != n for i in range(len(keys))):
        raise ValueError("Lost an LHS marginal stratum")
    return keys, unit, values, rng


def allocation(designs, families, footprints, real_fraction):
    """Split one stratum's designs between real footprints and procedural families."""
    real = round(designs * real_fraction) if footprints else 0
    if not families:
        real = designs
    synthetic = designs - real
    if families and synthetic % len(families):
        raise ValueError(
            f"{synthetic} procedural designs do not divide evenly over {len(families)} families"
        )
    counts = {family: synthetic // len(families) for family in families} if families else {}
    if real:
        counts["real"] = real
    return counts


def stratum_designs(study_seed, cohort, city, family, n, pool, bounds, *, pilot_cohort):
    """One stratum's physical designs: continuous inputs, balanced categories, footprints.

    The seed depends only on the study seed, cohort, city and family, so adding a
    stratum never changes another. In the pilot cohort the first design of a
    procedural family and the first use of each real footprint are pilot designs.
    """
    seed = seed_for(study_seed, cohort, city, family)
    keys, unit, values, rng = stratum(seed, bounds, n, family=family)
    cls, cws = cut_fractions(family, values[:, keys.index("cut_l_unit")], values[:, keys.index("cut_w_unit")])
    floors = balanced(FLOOR_CHOICES, n, rng)
    systems = balanced(TOPOLOGIES, n, rng)
    profiles = balanced(SCHEDULE_FAMILIES, n, rng)
    shifts = balanced(SHIFT_CHOICES, n, rng)
    footprint_ids = balanced(range(max(1, len(pool))), n, rng)
    seen = set()
    for i in range(n):
        p = dict(zip(keys, map(float, values[i])))
        p.update(
            num_floors=int(floors[i]),
            topology=str(systems[i]),
            schedule_family=str(profiles[i]),
            schedule_shift=int(shifts[i]),
        )
        record = pool[int(footprint_ids[i])] if family == "real" else None
        first = i == 0
        if record is not None:
            p["footprint_area_m2"] = float(record["area_m2"])
            if record.get("storeys"):
                p["num_floors"] = int(record["storeys"])
            first = record["footprint_id"] not in seen
            seen.add(record["footprint_id"])
        yield {
            "design_id": f"{cohort}_{city or 'all'}_{family}_{i:05d}",
            "cohort": cohort,
            "city": city,
            "family": family,
            "geometry_source": "real" if record else "synthetic",
            "footprint_id": record["footprint_id"] if record else None,
            "requested": p,
            "cut_l": float(cls[i]),
            "cut_w": float(cws[i]),
            "lhs_unit": unit[i].tolist(),
            "lhs_keys": keys,
            "stratum_seed": seed,
            "pilot": pilot_cohort and first,
        }


def stock_designs(study, footprints):
    """Yield physical stock designs before context and weather replication.

    ``footprints`` is the frozen list of real footprints (see geometry.load_footprints);
    it may be empty. Cross-site anchors come first, then one diversity stratum per
    city and family.
    """
    section = study.stock
    cities = [c.slug for c in study.cities]
    geometry = study.geometry
    families = tuple(geometry.families)
    cohorts = []
    if section.anchor_designs:
        cohorts.append(("anchors", [None], section.anchor_designs, footprints[: geometry.anchor_footprints]))
    per_city, remainder = divmod(section.diversity_designs, len(cities))
    if remainder:
        raise ValueError("diversity_designs must divide evenly over the cities")
    cohorts.append(("diversity", cities, per_city, footprints))
    for cohort, city_labels, designs, pool in cohorts:
        counts = allocation(designs, families, pool, geometry.real_fraction)
        for city in city_labels:
            for family, n in counts.items():
                yield from stratum_designs(
                    study.seed, cohort, city, family, n, pool, section.bounds, pilot_cohort=cohort == "anchors"
                )


def fixed_designs(study, count, cohort, footprints, bounds):
    """A city-independent set of buildings for the sweep studies; its firsts are the pilot."""
    geometry = study.geometry
    pool = footprints[: geometry.anchor_footprints]
    counts = allocation(count, tuple(geometry.families), pool, geometry.real_fraction)
    for family, n in counts.items():
        yield from stratum_designs(study.seed, cohort, None, family, n, pool, bounds, pilot_cohort=True)


def independent_designs(seed, bounds, n, *, label):
    """Plain stratified rows for sweeps that vary a subset of inputs."""
    keys, unit, values, _rng = stratum(seed_for(seed, label), bounds, n)
    for i in range(n):
        yield dict(zip(keys, map(float, values[i]))), unit[i].tolist()


def check_balance(rows, categories):
    """Require every categorical column to be balanced to within one design."""
    for key, allowed in categories.items():
        counts = [sum(row[key] == value for row in rows) for value in allowed]
        if max(counts) - min(counts) > 1 or sum(counts) != len(rows):
            raise ValueError(f"Categorical balance failed for {key}")


def check_strata(unit_rows):
    matrix = np.asarray(unit_rows, dtype=float)
    n = len(matrix)
    if any(len(set(np.floor(matrix[:, c] * n).astype(int))) != n for c in range(matrix.shape[1])):
        raise ValueError("LHS marginal-stratum invariant failed")
    return math.isfinite(matrix.sum())
