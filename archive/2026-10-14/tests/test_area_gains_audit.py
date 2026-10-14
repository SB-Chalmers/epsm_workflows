"""Detect area and gains regressions in compiled models, independently of heat."""

import copy
import json
from pathlib import Path

import pytest

from epsm_workflows.area_gains_audit import inspect_model, schedule_values


@pytest.fixture
def compiled_case():
    root = Path(__file__).parents[1] / "output/wwr-sensitivity-mf-se02-2026-10-01"
    if not root.exists():
        pytest.skip("Pinned diagnostic models not available")
    rows = {r["building_id"]: r for r in json.loads((root / "cohort.json").read_text())}
    for path in sorted(root.glob("runs/*/receipt.json")):
        receipt = json.loads(path.read_text())
        model = path.parent / "energyplus/model.epJSON"
        row = rows[receipt["building_id"]]
        if receipt["wwr"] == 0.2 and row["heated_basement_floors"] and model.exists():
            return row, json.loads(model.read_text())
    pytest.fail("Missing compiled heated basement case")


def test_compiled_totals_match_epc_not_gross_area(compiled_case):
    row, model = compiled_case
    result = inspect_model(model, row)
    assert not result["failures"]
    assert result["compiled_basement_area_m2"] > 0
    assert result["equipment_gain_w"] > 0
    assert result["peak_people"] > 0
    assert result["separate_lighting_gain_w"] == 0
    assert result["equipment_gain_kwh_m2_common_year"] == pytest.approx(14.7168)
    assert result["people_total_gain_kwh_m2_common_year"] > 0


@pytest.mark.parametrize(
    "fault,expected",
    [
        ("area", "area_matches_reported_geometry_proxy"),
        ("equipment", "equipment_total_matches_epc_atemp"),
        ("people_schedule", "occupancy_schedule_mismatch"),
    ],
)
def test_audit_detects_compiler_regressions(compiled_case, fault, expected):
    row, original = compiled_case
    model = copy.deepcopy(original)
    if fault == "area":
        next(
            z for z in model["Zone"].values() if z["part_of_total_floor_area"] == "Yes"
        )["floor_area"] *= 1.1
    elif fault == "equipment":
        next(iter(model["ElectricEquipment"].values()))["design_level"] = 0
    else:
        for entry in model["Schedule:Compact"]["occupancy_fraction"]["data"]:
            if isinstance(entry["field"], (int, float)):
                entry["field"] = 0
    assert expected in inspect_model(model, row)["failures"]


def test_audit_does_not_use_heating_observation(compiled_case):
    row, model = compiled_case
    changed = dict(row, heat_kwh=999999999)
    assert inspect_model(model, row) == inspect_model(model, changed)
    assert sum(schedule_values(model, "occupancy_fraction")) == 14
