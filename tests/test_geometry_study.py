import json

import pytest
from epsm_workflows.geometry_study import make_manifest, report_results, run_case, variants

from epsm_core import (
    AirflowSpec,
    BuildingSpec,
    ConstructionSpec,
    EnvelopeSpec,
    FacadeSpec,
    Footprint,
    GeometrySpec,
    GlazingSpec,
    LayerSpec,
    MaterialSpec,
    ScheduleSpec,
    UseSpec,
    climate_from_epw,
)


@pytest.fixture
def study_base(tmp_path):
    weather = tmp_path / "weather.epw"
    weather.write_text("LOCATION,Test,Test,SWE,TEST,000000,57,12,1,20\n")
    material = MaterialSpec(
        name="fixture", conductivity_w_mk=1, density_kg_m3=1000, specific_heat_j_kgk=1000
    )
    slab = ConstructionSpec(layers=(LayerSpec(material=material, thickness_m=0.2),))
    b = BuildingSpec(
        identifier="fixture",
        geometry=GeometrySpec.from_footprint(
            Footprint.rectangle(30, 20), storey_elevations_m=(0, 3, 6, 9)
        ),
        envelope=EnvelopeSpec(
            **{
                k: slab
                for k in (
                    "wall",
                    "top",
                    "ground",
                    "intermediate_floor",
                    "weather_shell",
                    "basement_wall",
                    "dwelling_partition",
                    "common_partition",
                )
            },
        ),
        facade=FacadeSpec(
            default_wwr=0.3, glazing=GlazingSpec(u_value_w_m2k=1.5, solar_heat_gain_coefficient=0.6)
        ),
        climate=climate_from_epw(weather),
        airflow=AirflowSpec(
            topology="F", envelope_infiltration_ach=0.2, mechanical_vent_l_s_m2=0.35
        ),
        use=UseSpec(
            occupancy_category="residential",
            heating_setpoint=ScheduleSpec.constant(21),
            occupant_density_per_m2=0.02,
            equipment_gain_w_m2=2,
            dhw_kwh_m2_year=20,
        ),
    )
    return b, weather


def test_balanced_manifest_and_full_grid(study_base):
    base, epw = study_base
    weather = {name: epw for name in ("Gothenburg", "Malmo", "Kiruna")}
    screen = make_manifest(base, weather)
    full = make_manifest(base, weather, full_environment_grid=True)
    assert len(screen["cases"]) == 303
    assert len(full["cases"]) == 924
    identifiers = {c["case_id"] for c in screen["cases"]}
    assert all(c["reference"] is None or c["reference"] in identifiers for c in screen["cases"])
    assert {c["orientation"] for c in screen["cases"]} == {0, 45, 90, 135}
    assert {c["footprint"] for c in screen["cases"]} == {"rectangle", "l", "u", "courtyard"}
    assert all(
        BuildingSpec.model_validate(c["spec"]).geometry.conditioned_floor_area_m2
        == pytest.approx(1800)
        for c in screen["cases"]
    )
    assert make_manifest(base, weather) == screen


def test_operating_controls(study_base):
    base, _ = study_base
    schedules = ScheduleSpec(values=(20,) * 12 + (22,) * 12)
    base = base.updated(use=base.use.updated(heating_setpoint=schedules))
    cases = variants(base)
    assert cases["apartments_equal"][0].common_use.heating_setpoint == schedules
    assert cases["apartments_lower"][0].common_use.heating_setpoint.values == (18,) * 24
    assert cases["hip"][1] == "flat_attic"
    assert all(b.facade.solar_shading_factor == 1 for b, _ in cases.values())
    with pytest.raises(ValueError, match="residential"):
        variants(base.updated(use=base.use.updated(occupancy_category="non_residential")))


def test_compile_failure_remains_visible(study_base, tmp_path, monkeypatch):
    base, _ = study_base
    import epsm_workflows.geometry_study as module

    class Compiler:
        def run_id(self, spec):
            return "fixture-hash"

        def compile(self, spec):
            raise ValueError("infeasible fixture window")

    class Runner:
        def __init__(self, *args):
            self.compiler = Compiler()

    monkeypatch.setattr(module, "EnergyPlusRunner", Runner)
    case = dict(
        case_id="failed", variant="windows", reference=None, spec=base.model_dump(mode="json")
    )
    row = run_case(case, tmp_path, None, 10)
    assert row["status"] == "failed" and "infeasible" in row["error"]
    assert json.loads((tmp_path / "runs/failed/study-result.json").read_text()) == row
    summary = report_results(tmp_path, {"cases": [case]}, [row])
    assert summary["failed"] == 1 and summary["passed"] == 0
    assert "infeasible fixture window" in (tmp_path / "index.html").read_text()


def test_matched_differences_and_zero_reference(tmp_path):
    from epsm_workflows.geometry_study import METRICS

    reference = dict(
        case_id="base",
        variant="simple",
        reference=None,
        status="passed",
        fallback=False,
        **{k: 10.0 for k in METRICS},
    )
    reference.update(
        space_heating_demand_monthly_kwh=[0.0, 10.0] + [0.0] * 10,
        cooling_demand_monthly_kwh=[0.0] * 12,
    )
    detailed = dict(reference, case_id="detail", variant="windows", reference="base")
    detailed.update(
        space_heating_demand_kwh=12.0, space_heating_demand_monthly_kwh=[1.0, 11.0] + [0.0] * 10
    )
    report_results(tmp_path, {"cases": []}, [reference, detailed])
    comparison = json.loads((tmp_path / "comparisons.json").read_text())[0]
    assert comparison["space_heating_demand_kwh_difference"] == 2
    assert comparison["space_heating_demand_kwh_difference_pct"] == 20
    assert comparison["space_heating_demand_month_01_kwh_difference"] == 1
    assert comparison["space_heating_demand_month_01_kwh_difference_pct"] is None
    assert comparison["space_heating_demand_month_02_kwh_difference_pct"] == 10


def test_roof_and_zoning_choices_preserve_study_intent(study_base):
    base, _ = study_base
    cases = variants(base)
    assert cases["simple"][0].geometry.roof.topology == "compact"
    assert cases["flat_attic"][0].geometry.roof.topology == "cold_attic"
    assert cases["without_roof"][0].geometry.roof.shape == "flat"
    assert cases["without_roof"][0].geometry.roof.topology == "cold_attic"
    assert cases["whole"][0].geometry.zoning.mode == "whole_building"
    assert cases["apartments_equal"][0].geometry.zoning.on_failure == "per_storey"
    assert cases["apartments_equal"][0].geometry.zoning.mode == "apartments"
    assert cases["without_layout"][0].geometry.zoning.mode == "per_storey"
    assert cases["without_layout"][0].geometry.zoning.on_failure == "error"


def test_training_geometry_for_unequal_wings(study_base):
    from types import SimpleNamespace

    from epsm_workflows.analysis import frame

    from epsm_core import BuildingPart, RoofSpec

    base, _ = study_base
    other = BuildingPart(
        identifier="wing",
        footprint=Footprint.rectangle(15, 20).updated(
            exterior=((40, 0), (55, 0), (55, 20), (40, 20))
        ),
        storey_elevations_m=(0, 3, 6),
        roof=RoofSpec(shape="gable"),
    )
    building = base.updated(geometry=base.geometry.updated(parts=(*base.geometry.parts, other)))
    row = (
        "fixture",
        building.model_dump_json(),
        json.dumps({"requested": {}}),
        json.dumps({"report_json": json.dumps({"perimeter_m": 170, "facades": []})}),
        "{}",
        "{}",
    )
    store = SimpleNamespace(
        db=SimpleNamespace(execute=lambda _: SimpleNamespace(fetchall=lambda: [row]))
    )
    result = frame(store).iloc[0]
    assert result["floors"] == pytest.approx(2400 / 900)
    assert result["floor_height_m"] == 3
    assert result["roof"] == "mixed"
