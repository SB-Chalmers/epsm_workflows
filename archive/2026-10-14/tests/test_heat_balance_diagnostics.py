"""Guard against confounded sensitivities and incorrect heat accounting."""

import json
from pathlib import Path

import numpy as np
import pytest

from epsm_workflows import archetype_diagnostics as diagnostics
from epsm_workflows import heat_balance_diagnostics as experiment


@pytest.fixture
def inputs():
    root = Path(__file__).parents[1] / "output/wwr-sensitivity-mf-se02-2026-10-01"
    if not root.exists():
        pytest.skip("Pinned local cohort not available")
    rows = json.loads((root / "cohort.json").read_text())
    return rows, json.loads((root / "audit.json").read_text())["source_weather"]


def test_window_changes_only_u_and_compiles(inputs):
    rows, weather = inputs
    baseline = experiment.build_spec(rows[0], "existing", weather, "baseline")
    variant = experiment.build_spec(rows[0], "existing", weather, "window_2.22")
    expected = baseline.model_dump(mode="json")
    expected["facade"]["glazing"]["u_value_w_m2k"] = 2.22
    assert variant.model_dump(mode="json") == expected
    compiler = experiment.HeatBalanceCompiler(
        "/Applications/OpenStudio-3.9.0/EnergyPlus/energyplus",
        settings=experiment.settings(),
    )
    a, _ = compiler.compile(baseline)
    b, _ = compiler.compile(variant)
    assert a["Material"] == b["Material"]
    assert a["ZoneInfiltration:DesignFlowRate"] == b["ZoneInfiltration:DesignFlowRate"]
    assert a["Foundation:Kiva:Settings"] == b["Foundation:Kiva:Settings"]
    assert a["Zone"] == b["Zone"]
    assert (
        a["Output:Diagnostics"]["heat_balance"]["diagnostics"][0]["key"]
        == "DisplayAdvancedReportVariables"
    )


def test_selection_does_not_use_heat(inputs):
    rows, _ = inputs
    changed = [dict(r, heat_kwh=i * 10000000) for i, r in enumerate(rows)]
    assert [r["building_id"] for r in diagnostics.select_diagnostics(rows)] == [
        r["building_id"] for r in diagnostics.select_diagnostics(changed)
    ]


def test_output_only_compiler_preserves_physics(inputs):
    from epsm_core.compiler import EnergyPlusCompiler

    rows, weather = inputs
    spec = experiment.build_spec(rows[0], "standard", weather, "baseline")
    executable = "/Applications/OpenStudio-3.9.0/EnergyPlus/energyplus"
    old, _ = EnergyPlusCompiler(executable).compile(spec)
    new, _ = experiment.HeatBalanceCompiler(
        executable, settings=experiment.settings()
    ).compile(spec)
    assert {
        k: v
        for k, v in old.items()
        if k not in ("Output:Variable", "Output:Diagnostics")
    } == {
        k: v
        for k, v in new.items()
        if k not in ("Output:Variable", "Output:Diagnostics")
    }


def test_rates_and_energy_units_are_not_confused():
    assert experiment.integrate("W", np.array([1000, -500])) == 0.5
    assert experiment.integrate("J", np.array([3600000, -1800000])) == 0.5
    with pytest.raises(ValueError):
        experiment.integrate("C", np.array([20]))


def test_sql_balance_closes_and_surface_sign_matches_zone_air():
    root = Path(__file__).parents[1] / "output/heat-balance-mf-se02-2026-10-01-reviewed"
    if not (root / "smoke.json").exists():
        pytest.skip("Local heat-balance smoke not available")
    receipt = json.loads((root / "smoke.json").read_text())
    directory = Path(receipt["run_directory"]) / "energyplus"
    model = json.loads((directory / "model.epJSON").read_text())
    result = experiment.extract(directory / "eplusout.sql", model)
    # Check closure against actual supply heating, rather than requiring annual
    # positive-only components to sum to the delivered-energy target.
    assert (
        result["air_balance_residual_absolute_kwh"] / result["supply_heating_kwh"]
        < 0.001
    )
    assert (
        abs(result["surface_convection_reconciliation_kwh"])
        / result["supply_heating_kwh"]
        < 0.001
    )
    assert result["surface_convection_signed_kwh"]["ground"] < 0
    assert result["heating_hours_surface_convection_signed_kwh"]["ground"] < 0
