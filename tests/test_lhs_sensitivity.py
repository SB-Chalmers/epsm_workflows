"""LHS coverage, physical invariants and exclusion of holdout energy."""

import copy
import json
from pathlib import Path

import pytest
from epsm_workflows import lhs_sensitivity as lhs


def test_latin_hypercube_is_deterministic_and_stratified():
    samples = lhs.design()
    assert samples == lhs.design()
    for name, (lo, hi) in lhs.BOUNDS.items():
        bins = [int((p[name] - lo) / (hi - lo) * 40) for p in samples]
        assert sorted(bins) == list(range(40))


@pytest.mark.parametrize("state", ["existing", "standard", "advanced"])
def test_joint_design_preserves_infiltration_materials_area_weather(state):
    root = Path(__file__).parents[1] / "output/parameter-sensitivity-mf-se02-2026-10-01"
    if not root.exists():
        pytest.skip("Local study inputs unavailable")
    row = json.loads((root / "cohort.json").read_text())[0]
    weather = json.loads((root / "audit.json").read_text())["source_weather"]
    base = lhs.oat.frozen_spec(row, state, weather, 0.2)
    spec = lhs.build_spec(row, state, weather, lhs.design()[0])
    assert (
        spec.airflow.envelope_infiltration_ach == base.airflow.envelope_infiltration_ach
    )
    assert (
        spec.geometry.conditioned_floor_area_m2
        == base.geometry.conditioned_floor_area_m2
    )
    assert spec.climate == base.climate
    assert spec.envelope.ground == base.envelope.ground
    for before, after in [
        (base.envelope.wall, spec.envelope.wall),
        (base.envelope.top, spec.envelope.top),
    ]:
        assert [layer.material for layer in before.layers] == [
            layer.material for layer in after.layers
        ]
    lhs.Compiler(
        "/Applications/OpenStudio-3.9.0/EnergyPlus/energyplus", row, lhs.design()[0]
    ).compile(spec)


def test_selection_never_uses_holdout_energy(tmp_path):
    rows = [
        dict(
            building_id=i,
            property_key=i,
            split=split,
            heat_kwh=10000,
            atemp_m2=100,
            heated_basement_floors=0,
            roof_form="flat",
        )
        for i, split in [("a", "train"), ("b", "test")]
    ]
    designs = lhs.design(20)
    baseline = [
        dict(
            building_id=r["building_id"], state=s, status="complete", demand_kwh_m2=110
        )
        for r in rows
        for s in lhs.oat.pilot.STATES
    ]
    results = [
        dict(
            building_id=r["building_id"],
            state=s,
            design_id=i,
            status="complete",
            demand_kwh_m2=95 + i,
        )
        for r in rows
        for s in lhs.oat.pilot.STATES
        for i in range(20)
    ]
    lhs.summarize(rows, results, designs, baseline, tmp_path)
    before = json.loads((tmp_path / "selection.json").read_text())
    changed = copy.deepcopy(rows)
    changed[1]["heat_kwh"] = 9999999
    lhs.summarize(changed, results, designs, baseline, tmp_path)
    assert json.loads((tmp_path / "selection.json").read_text()) == before
    assert all(v["design_id"] == 0 for v in before.values())
