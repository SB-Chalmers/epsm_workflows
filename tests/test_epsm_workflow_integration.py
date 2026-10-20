"""Cross-package physical branches and serialized EPSM evaluation contracts."""
# Optional integrations must be skipped before importing their public objects.
# ruff: noqa: E402

import pytest

epsm = pytest.importorskip("epsm")

from epsm.results import EnergyResult, Evaluation
from epsm_data import RenovationMeasure
from epsm_data.domain import BuildingElement
from epsm_data.operations import ConstructionOperation
from epsm_lca import LCAResult

from epsm_workflows import optimise_renovations
from epsm_workflows.records import digest


def test_real_building_branch_lineage_resume_and_computed_objectives(tmp_path):
    baseline = epsm.Building(preset="illustrative-reference")
    window = baseline.construction_set.elements[BuildingElement.WINDOW]
    measures = [
        RenovationMeasure(
            measure_id=f"windows-{u}",
            category="envelope",
            name=f"Window {u}",
            description="Synthetic physical choice for contract test",
            target="window",
            operations=(
                ConstructionOperation(
                    "replace_element",
                    BuildingElement.WINDOW,
                    element=window.with_changes(u_value=u),
                ),
            ),
        )
        for u in (1.0, 1.5)
    ]
    calls = []

    def evaluate(building):
        calls.append(building)
        value = next(
            e.construction.u_value
            for e in building.elements
            if e.role == BuildingElement.WINDOW
        )
        energy = EnergyResult(
            engine="synthetic",
            scalars={},
            hourly={},
            units={},
            diagnostics={},
            artifacts={},
            delivered_by_carrier={"electricity": value * 100},
        )
        carbon = LCAResult(
            "complete", len(building.stages) * 10, 50, 50 + len(building.stages) * 10
        )
        return Evaluation(
            id=digest(building.to_dict()),
            state_id=building.snapshot().sha256,
            path_id=digest(building.material_flows),
            energy_id=digest(value),
            lca_id=digest(building.material_flows),
            energy=energy.to_dict(),
            lca=carbon.to_dict(),
        )

    options = dict(
        bundles=[(m,) for m in measures],
        evaluator=evaluate,
        evaluator_id="synthetic-contract-1",
        directory=tmp_path,
        max_depth=2,
    )
    first = optimise_renovations(baseline, budget=2, **options)
    resumed = optimise_renovations(baseline, budget=5, **options)
    assert len(calls) == 5
    assert len(baseline.stages) == 0 and len(baseline.results) == 0
    assert first.nodes[1].evaluation_id == resumed.nodes[1].evaluation_id
    assert all(node.energy_kwh is not None and node.feasible for node in resumed.nodes)
    assert sorted(len(b.stages) for b in calls) == [0, 1, 1, 2, 2]
    for node in resumed.nodes:
        restored = epsm.Building.from_dict(node.building)
        assert len(restored.stages) == node.depth
        assert len(restored.material_flows) == node.depth


def test_real_rc_and_automatic_lca_renovation_tree(tmp_path):
    pytest.importorskip("epsm_rc")
    from epsm_rc import RCSettings
    from datetime import datetime, timedelta

    from epsm import AssessmentSettings, EnvironmentalFactor, Weather
    from epsm_core import DeliverySpec, HeatingDeliverySpec

    path = tmp_path / "synthetic.epw"
    headers = [
        "LOCATION,Test,Test,SWE,SYNTHETIC,0,57,12,1,20",
        "DESIGN CONDITIONS,0",
        "TYPICAL/EXTREME PERIODS,0",
        "GROUND TEMPERATURES,0",
        "HOLIDAYS/DAYLIGHT SAVINGS,No,0,0,0",
        "COMMENTS 1,Software test only",
        "COMMENTS 2,Not measured weather",
        "DATA PERIODS,1,1,Data,Sunday,1/1,12/31",
    ]
    with path.open("w") as stream:
        stream.write("\n".join(headers) + "\n")
        for index in range(8760):
            stamp = datetime(2023, 1, 1) + timedelta(hours=index)
            row = ["0"] * 35
            row[:10] = [
                "2023",
                str(stamp.month),
                str(stamp.day),
                str(stamp.hour + 1),
                "60",
                "?",
                "10",
                "5",
                "70",
                "101325",
            ]
            row[12] = "300"
            row[20:22] = ["180", "2"]
            stream.write(",".join(row) + "\n")
    building = epsm.Building(preset="illustrative-reference")
    building.hvac = building.hvac.with_changes(
        delivery=DeliverySpec(
            heating=HeatingDeliverySpec(
                carrier="district_heat",
                efficiency=1,
                specific_fan_power_w_per_l_s=1,
                source="Synthetic test delivery assumption",
            )
        )
    )
    window = building.construction_set.elements[BuildingElement.WINDOW]
    measure = RenovationMeasure(
        "test-window",
        "envelope",
        "Test window",
        "Synthetic test choice",
        "window",
        operations=(
            ConstructionOperation(
                "replace_element",
                BuildingElement.WINDOW,
                element=window.with_changes(u_value=1.0),
            ),
        ),
    )
    child = building.renovate(measure)
    material_factors = {
        q.material_id: {
            "A1A3": EnvironmentalFactor(2, "m2", "A1A3", "Synthetic test factor")
        }
        for q in child.material_flows[-1].installed
    }
    assessment = AssessmentSettings(
        period_years=30,
        energy_boundary="space_heating_and_fans",
        baseline="retained",
        material_modules=("A1A3",),
        material_factors=material_factors,
        carrier_factors={
            key: EnvironmentalFactor(0.1, "kWh", "B6", "Synthetic test factor")
            for key in ("district_heat", "electricity")
        },
    )
    options = dict(
        bundles=[(measure,)],
        max_depth=1,
        budget=2,
        engine="epsm_rc",
        weather=Weather.from_epw(path),
        assessment=assessment,
        directory=tmp_path / "tree",
        evaluation_options={
            "directory": tmp_path / "engine",
            "settings": RCSettings.from_policy(
                "epsm_rc_5r1c_v1",
                airflow_policy="sensible_only_unlimited_supply",
                ground_policy="conductance_multiplier",
            ),
        },
    )
    tree = optimise_renovations(building, **options)
    assert len(tree.nodes) == 2
    assert all(node.feasible for node in tree.nodes), [
        node.reasons for node in tree.nodes
    ]
    assert tree.nodes[1].energy_kwh < tree.nodes[0].energy_kwh
    resumed = optimise_renovations(building, **options)
    assert [n.evaluation_id for n in resumed.nodes] == [
        n.evaluation_id for n in tree.nodes
    ]


def test_builtin_search_rejects_changed_weather_or_runtime(tmp_path, monkeypatch):
    """Resume validates inputs before bypassing the calculation cache."""
    from epsm import provenance

    weather = tmp_path / "weather.epw"
    weather.write_text("LOCATION,Test,Test,SWE,SYNTHETIC,0,57,12,1,20\n")
    baseline = epsm.Building()
    calls = []

    def simulate(self, **options):
        calls.append(options)
        return {
            "energy": {"annual_delivered_energy_kwh": 100},
            "lca": {"total_gwp_kgco2e": 50, "status": "complete"},
        }

    monkeypatch.setattr(epsm.Building, "simulate", simulate)
    options = dict(
        bundles=[],
        max_depth=0,
        engine="epsm_rc",
        weather=str(weather),
        directory=tmp_path / "tree",
    )
    original = optimise_renovations(baseline, **options)
    resumed = optimise_renovations(baseline, **options)
    assert original.id == resumed.id and len(calls) == 1
    weather.write_text(weather.read_text() + "COMMENTS 1,Changed evidence\n")
    with pytest.raises(ValueError, match="configuration"):
        optimise_renovations(baseline, **options)
    weather.write_text("LOCATION,Test,Test,SWE,SYNTHETIC,0,57,12,1,20\n")
    original_implementation = provenance.implementation_identity
    monkeypatch.setattr(
        provenance,
        "implementation_identity",
        lambda package: (
            "changed-assessment"
            if package == "epsm_lca"
            else original_implementation(package)
        ),
    )
    with pytest.raises(ValueError, match="configuration"):
        optimise_renovations(baseline, **options)
    assert len(calls) == 1
