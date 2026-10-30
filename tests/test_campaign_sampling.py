"""The stock sampler reproduces its strata, balance and identity across machines."""

import hashlib
import json

import pytest

pytest.importorskip("scipy")
from shapely.geometry import Polygon

from epsm_workflows.campaign.sampling import (
    allocation,
    check_balance,
    check_strata,
    stock_designs,
    stratum,
)
from epsm_workflows.campaign.study import (
    FLOOR_CHOICES,
    SCHEDULE_FAMILIES,
    SHIFT_CHOICES,
    TOPOLOGIES,
    StudySpec,
)

CITIES = [{"slug": s, "city": s, "tmy": f"{s}.epw"} for s in ("malmo", "gothenburg", "uddevalla", "umea", "ritsem")]
# Digest of (design_id, requested inputs, pilot) for the 2026-09 Vera configuration with ten
# real footprints standing in for its ten templates; verified bit-stable on SciPy 1.10-1.18.
VERA_60K_DESIGN_DIGEST = "27970aa24dceb9667660ca34596e91c8663b9e8087c90107d338a16430200c9e"


def footprints(count):
    return [
        {"footprint_id": f"fp{i:02d}", "polygon": Polygon([(0, 0), (30, 0), (30, 20), (0, 20)]), "area_m2": 600.0, "storeys": None}
        for i in range(count)
    ]


def vera_study(**overrides):
    definition = {
        "kind": "stock_diversity",
        "name": "vera",
        "seed": 42,
        "cities": CITIES,
        "geometry": {"families": ["rectangle", "L", "U", "courtyard"], "real_footprints": "x.json", "real_fraction": 0.2, "anchor_footprints": 10},
        "stock": {"diversity_designs": 9000, "anchor_designs": 200},
    }
    return StudySpec(**{**definition, **overrides})


@pytest.mark.slow
def test_vera_60k_design_identity_is_pinned():
    designs = list(stock_designs(vera_study(), footprints(10)))
    assert len(designs) == 9200
    assert sum(d["pilot"] for d in designs) == 14
    blob = json.dumps([[d["design_id"], d["requested"], d["pilot"]] for d in designs], sort_keys=True).encode()
    assert hashlib.sha256(blob).hexdigest() == VERA_60K_DESIGN_DIGEST


def test_small_study_keeps_strata_balance_and_bounds():
    study = vera_study(stock={"diversity_designs": 200, "anchor_designs": 40}, geometry={
        "families": ["rectangle", "L", "U", "courtyard"], "real_footprints": "x.json", "real_fraction": 0.2, "anchor_footprints": 3})
    designs = list(stock_designs(study, footprints(5)))
    assert len(designs) == 240
    by_seed = {}
    for d in designs:
        by_seed.setdefault(d["stratum_seed"], ([], []))
        by_seed[d["stratum_seed"]][0].append(d["lhs_unit"])
        by_seed[d["stratum_seed"]][1].append(d["requested"])
        for key, (low, high) in study.stock.bounds.items():
            if d["geometry_source"] == "real" and key == "footprint_area_m2":
                continue
            assert low <= d["requested"][key] <= high
    assert len(by_seed) == 5 + 25
    for units, inputs in by_seed.values():
        check_strata(units)
        check_balance(inputs, {"num_floors": FLOOR_CHOICES, "topology": TOPOLOGIES,
                               "schedule_family": SCHEDULE_FAMILIES, "schedule_shift": SHIFT_CHOICES})
    real = [d for d in designs if d["geometry_source"] == "real"]
    assert len(real) == 8 + 40 and all(d["requested"]["footprint_area_m2"] == 600.0 for d in real)
    anchors = [d for d in designs if d["cohort"] == "anchors"]
    assert {d["footprint_id"] for d in anchors if d["footprint_id"]} == {"fp00", "fp01", "fp02"}
    assert sum(d["pilot"] for d in anchors) == 4 + 3


def test_designs_are_deterministic_and_independent_per_stratum():
    one = list(stock_designs(vera_study(stock={"diversity_designs": 100, "anchor_designs": 0}), footprints(2)))
    two = list(stock_designs(vera_study(stock={"diversity_designs": 100, "anchor_designs": 0}), footprints(2)))
    assert [d["requested"] for d in one] == [d["requested"] for d in two]
    more = vera_study(stock={"diversity_designs": 100, "anchor_designs": 20}, geometry={
        "families": ["rectangle", "L", "U", "courtyard"], "real_footprints": "x.json", "real_fraction": 0.2, "anchor_footprints": 2})
    with_anchors = {d["design_id"]: d["requested"] for d in stock_designs(more, footprints(2))}
    for d in one:
        assert with_anchors[d["design_id"]] == d["requested"]


def test_allocation_and_feasibility_errors_are_explicit():
    assert allocation(40, ("rectangle", "L", "U", "courtyard"), footprints(3), 0.2) == {
        "rectangle": 8, "L": 8, "U": 8, "courtyard": 8, "real": 8}
    assert allocation(40, ("rectangle",), [], 0.5) == {"rectangle": 40}
    with pytest.raises(ValueError, match="divide evenly"):
        allocation(41, ("rectangle", "L"), [], 0)
    keys, unit, values, _ = stratum(7, {"a": [0, 1], "b": [1, 10]}, 12)
    assert keys == ["a", "b"] and unit.shape == values.shape == (12, 2)
    check_strata(unit)
    with pytest.raises(ValueError, match="divide evenly"):
        list(stock_designs(vera_study(stock={"diversity_designs": 9001, "anchor_designs": 0}), footprints(10)))
