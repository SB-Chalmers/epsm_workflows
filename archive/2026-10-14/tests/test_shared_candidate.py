"""Shared candidate selection may not read diagnostic holdout energy."""

import json
from pathlib import Path

import pytest

from epsm_workflows import parameter_sensitivity as sensitivity
from epsm_workflows import shared_candidate as candidate


def sample():
    rows = [
        {
            "building_id": "train",
            "property_key": "p1",
            "split": "train",
            "heat_kwh": 10000,
            "atemp_m2": 100,
        },
        {
            "building_id": "validation",
            "property_key": "p2",
            "split": "validation",
            "heat_kwh": 20000,
            "atemp_m2": 100,
        },
        {
            "building_id": "test",
            "property_key": "p3",
            "split": "test",
            "heat_kwh": 30000,
            "atemp_m2": 100,
        },
    ]
    results = []
    for row in rows:
        for state in sensitivity.pilot.STATES:
            for parameter, value, prediction in [
                ("baseline", None, 110),
                ("wall_insulation_multiplier", 1.2, 100),
            ]:
                results.append(
                    {
                        "building_id": row["building_id"],
                        "state": state,
                        "parameter": parameter,
                        "value": value,
                        "status": "complete",
                        "demand_kwh_m2": prediction,
                    }
                )
    return rows, results


def test_holdout_energy_cannot_change_shared_choices():
    rows, results = sample()
    before = candidate.select(rows, results)
    changed = [dict(r, heat_kwh=99999999) if r["split"] != "train" else r for r in rows]
    assert candidate.select(changed, results) == before
    assert all(
        v["wall_insulation_multiplier"] == 1.2 for v in before["settings"].values()
    )
    assert all("infiltration_ach" not in v for v in before["settings"].values())


def test_tie_retains_previous_setting():
    rows, results = sample()
    for r in results:
        r["demand_kwh_m2"] = 110
    selection = candidate.select(rows, results)
    assert all(
        all(v is None for v in values.values())
        for values in selection["settings"].values()
    )


def test_joint_candidate_preserves_infiltration_and_all_other_assumptions():
    root = Path(__file__).parents[1] / "output/heat-balance-mf-se02-2026-10-01-reviewed"
    if not root.exists():
        pytest.skip("Local diagnostic inputs unavailable")
    row = json.loads((root / "cohort.json").read_text())[0]
    weather = json.loads((root / "audit.json").read_text())["source_weather"]
    settings = {
        "wall_insulation_multiplier": 1.2,
        "roof_insulation_multiplier": 1.1,
        "window_u_w_m2k": 2.22,
        "ventilation_l_s_m2_atemp": 0.3,
    }
    baseline = candidate.build_spec(row, "existing", weather, {})
    spec = candidate.build_spec(row, "existing", weather, settings)
    assert (
        spec.airflow.envelope_infiltration_ach
        == baseline.airflow.envelope_infiltration_ach
    )
    assert spec.geometry == baseline.geometry
    assert spec.use == baseline.use
    assert spec.climate == baseline.climate
    assert spec.envelope.foundation == baseline.envelope.foundation
    assert spec.envelope.ground == baseline.envelope.ground
    assert (
        spec.envelope.thermal_bridge_uplift_fraction
        == baseline.envelope.thermal_bridge_uplift_fraction
    )
    assert spec.facade.default_wwr == baseline.facade.default_wwr
    from epsm_core import EnergyPlusCompiler

    EnergyPlusCompiler("/Applications/OpenStudio-3.9.0/EnergyPlus/energyplus").compile(
        spec
    )
