"""Verify one-at-a-time isolation and fair paired error comparisons."""

import copy
import json
from pathlib import Path

import pytest

from epsm_workflows import parameter_sensitivity as experiment


@pytest.fixture
def case():
    root = Path(__file__).parents[1] / "output/heat-balance-mf-se02-2026-10-01-reviewed"
    if not root.exists():
        pytest.skip("Local diagnostic inputs unavailable")
    row = next(
        r
        for r in json.loads((root / "cohort.json").read_text())
        if r["heated_basement_floors"] and r["roof_form"] == "pitched_ridge"
    )
    return row, json.loads((root / "audit.json").read_text())["source_weather"]


def leaves_changed(before, after, path=()):
    if isinstance(before, dict):
        return [
            p for k in before for p in leaves_changed(before[k], after[k], path + (k,))
        ]
    if isinstance(before, (list, tuple)):
        assert len(before) == len(after)
        return [
            p
            for i, (a, b) in enumerate(zip(before, after))
            for p in leaves_changed(a, b, path + (i,))
        ]
    return [] if before == after else [path]


@pytest.mark.parametrize("state", ["existing", "standard", "advanced"])
def test_each_spec_variant_changes_only_one_leaf(case, state):
    row, weather = case
    baseline = experiment.build_spec(row, state, weather, "baseline", None)
    for parameter, value in experiment.choices(row, state):
        spec = experiment.build_spec(row, state, weather, parameter, value)
        changes = leaves_changed(baseline.model_dump(), spec.model_dump())
        if parameter in ("baseline", "basement_temperature_c", "attic_exchange_ach"):
            assert not changes
        else:
            assert len(changes) == 1, (parameter, changes)
        if parameter != "infiltration_ach":
            assert (
                spec.airflow.envelope_infiltration_ach
                == baseline.airflow.envelope_infiltration_ach
            )
        if parameter not in (
            "wall_insulation_multiplier",
            "roof_insulation_multiplier",
        ):
            assert spec.envelope.wall == baseline.envelope.wall
            assert spec.envelope.top == baseline.envelope.top
        assert spec.climate == baseline.climate
        assert spec.envelope.ground == baseline.envelope.ground
        assert (
            spec.envelope.wall.layers[0].material
            == baseline.envelope.wall.layers[0].material
        )


def test_compiler_patches_only_declared_boundary(case):
    row, weather = case
    from epsm_core import EnergyPlusCompiler

    spec = experiment.build_spec(row, "existing", weather, "baseline", None)
    model, report = EnergyPlusCompiler(
        "/Applications/OpenStudio-3.9.0/EnergyPlus/energyplus"
    ).compile(spec)
    attic = experiment.patch_model(
        copy.deepcopy(model), report.data, "attic_exchange_ach", 1
    )
    assert {
        k: v for k, v in attic.items() if k != "ZoneVentilation:DesignFlowRate"
    } == model
    assert all(
        "_attic_" in v["zone_or_zonelist_or_space_or_spacelist_name"]
        for v in attic["ZoneVentilation:DesignFlowRate"].values()
    )
    basement = experiment.patch_model(
        copy.deepcopy(model), report.data, "basement_temperature_c", 15
    )
    assert {
        k: v
        for k, v in basement.items()
        if k not in ("Schedule:Constant", "ThermostatSetpoint:DualSetpoint")
    } == {
        k: v
        for k, v in model.items()
        if k not in ("Schedule:Constant", "ThermostatSetpoint:DualSetpoint")
    }
    for name, obj in basement["ThermostatSetpoint:DualSetpoint"].items():
        if "_basement_" not in name:
            assert obj == model["ThermostatSetpoint:DualSetpoint"][name]


def test_comparisons_use_identical_buildings_and_keep_worsening(tmp_path):
    rows = [
        {"building_id": "a", "property_key": "p1", "heat_kwh": 10000, "atemp_m2": 100},
        {"building_id": "b", "property_key": "p2", "heat_kwh": 10000, "atemp_m2": 100},
    ]
    results = [
        {
            "building_id": "a",
            "state": "existing",
            "parameter": "baseline",
            "value": None,
            "status": "complete",
            "demand_kwh_m2": 105,
            "compiled": True,
        },
        {
            "building_id": "b",
            "state": "existing",
            "parameter": "baseline",
            "value": None,
            "status": "complete",
            "demand_kwh_m2": 80,
            "compiled": True,
        },
        {
            "building_id": "a",
            "state": "existing",
            "parameter": "window_u_w_m2k",
            "value": 2.22,
            "status": "complete",
            "demand_kwh_m2": 100,
            "compiled": True,
        },
        {
            "building_id": "b",
            "state": "existing",
            "parameter": "window_u_w_m2k",
            "value": 2.22,
            "status": "complete",
            "demand_kwh_m2": 70,
            "compiled": True,
        },
    ]
    for result in results:
        result["property_key"] = "p1" if result["building_id"] == "a" else "p2"
    summary = experiment.summarize(rows, results, tmp_path)
    comparison = next(
        r
        for r in summary
        if r["parameter"] == "window_u_w_m2k" and r["loss_kwh_m2"] == 5
    )
    assert comparison["before"]["n"] == comparison["after"]["n"] == 2
    assert comparison["improved_buildings"] == 1
    assert comparison["worsened_buildings"] == 1
    assert comparison["mae_improvement_kwh_m2"] == -2.5
