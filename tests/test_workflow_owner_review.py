"""Independent authoring regressions for physical and experiment boundaries."""

from importlib.resources import files

import pytest
from epsm import Building, Weather

from epsm_workflows import WorkflowSpec
from epsm_workflows.bindings import apply_bindings


def workflow():
    building = Building(preset="illustrative-reference")
    spec = WorkflowSpec.template(samples=3, seed=17)
    spec.add_subject(building, id="one", group="A")
    spec.add_weather(
        Weather.from_epw(files("epsm_rc") / "data/epw/goteborg.epw"), id="weather"
    )
    return spec, building


def test_geometry_dimension_change_preserves_other_requested_geometry():
    building = Building(preset="illustrative-reference")
    changed = apply_bindings(
        building, {"geometry.orientation_deg": 45, "geometry.storeys": 4}
    )
    assert changed.geometry.orientation_deg == 45
    assert changed.geometry.parts[0].floors == 4


def test_facade_selector_and_global_glazing_patch_both_survive():
    building = Building(preset="illustrative-reference")
    facade_id = building.geometry.facades()[0].facade_id
    changed = apply_bindings(
        building,
        {"facade.glazing.solar_heat_gain_coefficient": 0.4, "facade.wwr": 0.35},
        selectors={"facade.wwr": {"facades": [facade_id]}},
    )
    assert changed.facade.glazing.solar_heat_gain_coefficient == 0.4
    assert changed.facade.wwr_for(facade_id) == 0.35


def test_condition_resolves_inherited_owner_values_and_aliases():
    spec, building = workflow()
    inherited = building.hvac.airflow.topology
    spec.vary(
        "facade.wwr",
        min=0.3,
        max=0.4,
        applies_when={"systems.ventilation.topology": inherited},
    )
    report = spec.preview(check_ready=False)
    assert report["ready"], report
    assert all("facade.wwr" in row["parameters"] for row in report["cases"])


def test_unknown_condition_and_subject_selector_are_diagnostics():
    spec, _ = workflow()
    spec.vary(
        "facade.wwr",
        min=0.3,
        max=0.4,
        applies_when={"invented.parameter": 1},
        selector={"subjects": ["missing"]},
    )
    report = spec.validate()
    assert not report["ready"]
    assert {row["code"] for row in report["diagnostics"]} >= {
        "unknown_condition",
        "unknown_subject",
    }


def test_alias_replacement_does_not_create_two_physical_variables():
    spec, _ = workflow()
    spec.vary("facade.wwr", min=0.1, max=0.2)
    spec.vary("facade.default_wwr", min=0.6, max=0.7)
    assert len(spec.to_dict()["parameters"]) == 1
    assert all(0.6 <= row["facade.default_wwr"] <= 0.7 for row in spec.samples())


def test_authoring_rejects_nonfinite_values_before_mutating_draft():
    spec = WorkflowSpec.template()
    original = spec.to_dict()
    with pytest.raises(ValueError):
        spec.set("facade.wwr", float("nan"))
    assert spec.to_dict() == original


def test_inspected_weather_can_supply_its_default_identity():
    weather = Weather.from_epw(files("epsm_rc") / "data/epw/goteborg.epw")
    spec = WorkflowSpec.template().add_weather(weather)
    assert spec.to_dict()["weather"][0]["id"] == weather.sha256


def test_explicit_unconditioned_exchange_uses_existing_core_authoring():
    building = Building(preset="illustrative-reference")
    changed = apply_bindings(
        building,
        {
            "unconditioned_exchange": [
                {
                    "zone_kind": "attic",
                    "outdoor_air_ach": 0.5,
                    "source": "Declared study scenario",
                }
            ]
        },
    )
    assert changed.unconditioned_exchange[0].outdoor_air_ach == 0.5


def test_facade_selector_cannot_silently_reference_an_unknown_wall():
    building = Building(preset="illustrative-reference")
    with pytest.raises(ValueError, match="facade"):
        apply_bindings(
            building,
            {"facade.wwr": 0.3},
            selectors={"facade.wwr": {"facades": ["invented"]}},
        )


@pytest.mark.parametrize(
    "binding,unit,bounds",
    [
        ("geometry.floor_height_m", "m", (2.5, 3.5)),
        ("operation.heating_setpoint", "degC", (19, 22)),
        ("envelope.infiltration_ach", "1/h", (0.1, 0.4)),
        ("facade.glazing.u_value_w_m2k", "W/(m2 K)", (0.8, 1.4)),
    ],
)
def test_native_units_are_retained_and_incompatible_values_rejected(
    binding, unit, bounds
):
    spec = WorkflowSpec.template().vary(binding, min=bounds[0], max=bounds[1])
    assert spec.to_dict()["parameters"][0]["unit"] == unit
    with pytest.raises(ValueError, match="unit conversion"):
        spec.vary(binding, min=bounds[0], max=bounds[1], unit="unrelated")


def test_integer_monte_carlo_needs_declared_discrete_probability():
    from epsm_workflows.distributions import normal

    with pytest.raises(ValueError, match="discrete"):
        WorkflowSpec.template("monte_carlo").vary(
            "geometry.storeys", min=1, max=5, distribution=normal(mean=3, std=1)
        )


def test_condition_cycles_are_actionable_before_expansion():
    spec, _ = workflow()
    spec.vary(
        "facade.wwr", min=0.3, max=0.4, applies_when={"operation.heating_setpoint": 21}
    )
    spec.vary(
        "operation.heating_setpoint",
        min=20,
        max=22,
        applies_when={"facade.default_wwr": 0.3},
    )
    assert any(
        row["code"] == "cyclic_condition" for row in spec.validate()["diagnostics"]
    )


def test_monthly_engine_requirement_is_diagnostic_in_preview():
    spec, _ = workflow()
    spec.set("outputs", [{"name": "space_heating_demand_kwh", "resolution": "monthly"}])
    report = spec.preview()
    assert not report["ready"]
    assert any(
        row["code"] == "invalid_evaluation_request" for row in report["diagnostics"]
    )


def test_batch_resolution_retains_owner_receipts_assignments_and_future_axes(
    tmp_path, monkeypatch
):
    import json
    import shutil
    from dataclasses import asdict

    import smhi2epw
    from smhi2epw import BatchResult, MorphResult
    from smhi2epw.compiler import CompileResult
    from smhi2epw.io import file_sha256
    from smhi2epw.processing import ProcessingReport

    baseline = tmp_path / "amy.epw"
    future = tmp_path / "future.epw"
    shutil.copyfile(files("epsm_rc") / "data/epw/goteborg.epw", baseline)
    shutil.copyfile(baseline, future)
    baseline_receipt = tmp_path / "amy.epw.json"
    baseline_receipt.write_text(
        json.dumps({"epw_sha256": file_sha256(baseline), "source": "synthetic test"})
    )
    future_receipt = tmp_path / "future.epw.json"
    future_receipt.write_text(
        json.dumps({"output_sha256": file_sha256(future), "scenario": "ssp245"})
    )
    morphed = MorphResult(
        str(baseline), str(future), "ssp245", 2050, str(future_receipt)
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "completed",
                "jobs": [
                    {
                        "source_year": 2023,
                        "amy_path": str(baseline),
                        "status": "completed",
                        "provenance_path": str(baseline_receipt),
                        "morphed": [asdict(morphed)],
                    }
                ],
            }
        )
    )
    compiled = CompileResult(str(baseline), 8760, None, 0, None, ProcessingReport())
    calls = []

    def compile_batch(locations, years, output_dir, **options):
        calls.append((locations, years, output_dir, options))
        return BatchResult([compiled], [morphed], str(manifest))

    monkeypatch.setattr(smhi2epw, "compile_amy_batch", compile_batch)
    spec, building = workflow()
    spec.add_subject(building, id="two")
    spec.set("weather", [])
    spec.add_weather(
        id="climate",
        request={
            "kind": "amy_batch",
            "locations": [{"name": "gbg", "station_id": 71420}],
            "years": [2023],
            "subjects_by_location": {"gbg": ["one"]},
        },
    )
    spec.resolve_inputs(directory=tmp_path / "resolved")
    weather = spec.to_dict()["weather"]
    assert len(calls) == 1 and len(weather) == 2
    assert all(row["subjects"] == ["one"] for row in weather)
    future_case = next(
        row for row in weather if row["metadata"]["classification"] != "amy"
    )
    assert future_case["metadata"]["climate_scenario"] == "ssp245"
    assert future_case["metadata"]["climate_period"] == 2050
    assert future_case["weather"]["calendar_year"] == 2023
    assert future_case["weather"]["provenance"]["owner_receipt"]["scenario"] == "ssp245"
