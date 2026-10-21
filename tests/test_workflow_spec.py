"""User-facing authoring, deterministic expansion and unchanged model evidence."""

import json

import pytest

from epsm_workflows import WorkflowSpec
from epsm_workflows.distributions import categorical, normal


def test_templates_schema_and_roundtrip():
    import jsonschema

    for method in ("lhs", "monte_carlo", "comparative"):
        spec = WorkflowSpec.template(method=method, samples=8, seed=2)
        data = spec.to_dict()
        jsonschema.validate(data, spec.schema())
        assert WorkflowSpec.from_dict(json.loads(json.dumps(data))).to_dict() == data
    with pytest.raises(ValueError):
        WorkflowSpec.from_dict({"format": "epsm-workflow/999"})


def test_lhs_defaults_uniform_and_balances_categories():
    spec = WorkflowSpec.template(samples=12, seed=3)
    spec.vary("facade.wwr", min=0, max=1)
    spec.vary("systems.ventilation.topology", choices=["F", "FTX"])
    rows = spec.samples()
    assert rows == spec.samples()
    assert sorted(int(row["facade.wwr"] * 12) for row in rows) == list(range(12))
    assert sum(row["systems.ventilation.topology"] == "F" for row in rows) == 6


def test_monte_carlo_requires_explicit_distribution_and_validates_bounds():
    spec = WorkflowSpec.template(method="monte_carlo", samples=10)
    with pytest.raises(ValueError, match="distribution"):
        spec.vary("facade.wwr", min=0.1, max=0.5)
    spec.vary("facade.wwr", min=0.1, max=0.5, distribution=normal(mean=0.3, std=0.1))
    assert all(0.1 <= row["facade.wwr"] <= 0.5 for row in spec.samples())
    with pytest.raises(ValueError):
        spec.vary("facade.wwr", min=0.5, max=0.1, distribution=normal())


def test_choice_weights_and_configuration_are_not_live_mutable_views():
    spec = WorkflowSpec.template(method="monte_carlo", samples=20)
    spec.vary(
        "systems.ventilation.topology",
        choices=["F", "FTX"],
        distribution=categorical(probabilities=[0, 1]),
    )
    assert {row["systems.ventilation.topology"] for row in spec.samples()} == {"FTX"}
    value = spec.to_dict()
    value["sampling"]["seed"] = 999
    assert spec.to_dict()["sampling"]["seed"] == 0


def test_full_option_discovery_and_unknown_binding_rejection():
    options = WorkflowSpec.capabilities()
    assert options["geometry.heated_attic"]["status"] == "unsupported"
    assert options["operation.window_opening.behavior"]["status"] == "unsupported"
    assert options["facade.wwr"]["status"] == "supported"
    with pytest.raises(ValueError, match="binding"):
        WorkflowSpec.template().set("imaginary.private.attribute", 2)


def test_discovered_input_schemas_can_validate_values_independently():
    import jsonschema
    from epsm import Building

    facade = Building(preset="illustrative-reference").facade
    options = WorkflowSpec.capabilities()
    jsonschema.validate(
        facade.glazing.model_dump(mode="json"), options["facade.glazing"]["schema"]
    )


def test_preview_emits_bounded_lifecycle_event(caplog):
    with caplog.at_level("INFO", logger="epsm_workflows.workflow"):
        report = WorkflowSpec.template().preview()
    record = next(
        row
        for row in caplog.records
        if getattr(row, "epsm_event", None) == "study.preview.completed"
    )
    assert record.epsm_context["workflow_id"] == report["workflow_id"]
    assert record.epsm_details == {"case_count": 0, "ready": False}


def test_portfolio_expansion_baselines_and_engines_preserve_original():
    from epsm import Building, Weather
    from importlib.resources import files

    building = Building(preset="illustrative-reference")
    before = building.snapshot().sha256
    spec = WorkflowSpec.template(samples=2, seed=7)
    spec.add_subject(building, id="one", group="A")
    spec.add_subject(building, id="two", group="A")
    spec.add_weather(
        Weather.from_epw(files("epsm_rc") / "data/epw/goteborg.epw"), id="gbg"
    )
    spec.set("engines", ["epsm_rc", "energyplus"])
    spec.vary("facade.wwr", min=0.1, max=0.4, baseline=0.2, scope="shared")
    preview = spec.preview(check_ready=False)
    assert preview["case_count"] == 12
    assert len({case["axes"]["pair_id"] for case in preview["cases"]}) == 6
    assert all(case["axes"].get("baseline_key") for case in preview["cases"])
    assert building.snapshot().sha256 == before
    assert spec.preview(check_ready=False) == preview


def test_fixed_physical_binding_and_geometry_selector_are_validated():
    from epsm import Building
    from epsm_workflows.bindings import apply_bindings

    building = Building(preset="illustrative-reference")
    changed = apply_bindings(
        building, {"operation.heating_setpoint": 19, "facade.wwr": 0.3}
    )
    assert changed.operation.heating_setpoint.values == (19,) * 24
    assert changed.facade.default_wwr == 0.3
    assert building.facade.default_wwr != changed.facade.default_wwr
    with pytest.raises(ValueError):
        apply_bindings(building, {"facade.wwr": 2})


def test_fixed_study_inputs_apply_to_material_alternatives_too():
    from epsm import Building, Weather
    from importlib.resources import files

    building = Building(preset="illustrative-reference")
    original = building.to_dict()
    workflow = WorkflowSpec.template("comparative")
    workflow.add_subject(building, id="one")
    workflow.add_scenario("alternative", building, subject="one")
    workflow.add_weather(Weather.from_epw(files("epsm_rc") / "data/epw/goteborg.epw"))
    workflow.set("operation.heating_setpoint", 18)
    preview = workflow.preview(check_ready=False)
    assert preview["case_count"] == 2
    assert all(
        Building.from_dict(row["building"]).operation.heating_setpoint.values
        == (18,) * 24
        for row in preview["cases"]
    )
    assert building.to_dict() == original


def test_comparative_requires_a_declared_candidate_design():
    report = WorkflowSpec.template("comparative").validate()
    assert "missing_candidates" in {issue["code"] for issue in report["diagnostics"]}


def test_source_and_weather_request_schemas_roundtrip_and_reject_unknown_choices():
    import jsonschema

    workflow = WorkflowSpec.template()
    workflow.add_subject(
        id="one",
        source={
            "kind": "synthetic",
            "shape": "rectangle",
            "dimensions": {"length_m": 10, "width_m": 8},
            "geometry": {"storeys": 2, "storey_height_m": 3},
        },
    )
    workflow.add_weather(
        id="amy",
        request={
            "kind": "amy_batch",
            "years": [2023],
            "locations": [{"name": "gbg", "station_id": 71420}],
        },
    )
    jsonschema.validate(workflow.to_dict(), workflow.schema())
    assert "AMYBatchRequest" in workflow.schema()["$defs"]
    with pytest.raises(ValueError, match="kind|tag"):
        WorkflowSpec.template().add_subject(id="invalid", source={"kind": "mystery"})
    with pytest.raises(ValueError, match="kind|tag"):
        WorkflowSpec.template().add_weather(id="invalid", request={"kind": "mystery"})


def test_correlated_request_is_represented_but_explicitly_unavailable():
    workflow = WorkflowSpec.template()
    workflow.set("sampling.correlation", {"method": "gaussian_copula"})
    report = workflow.validate()
    assert "unsupported_correlation" in {
        issue["code"] for issue in report["diagnostics"]
    }


def test_explicit_resolution_uses_public_geometry_and_supplied_weather(tmp_path):
    from importlib.resources import files
    from epsm import Building

    workflow = WorkflowSpec.template()
    workflow.add_subject(
        id="one",
        source={
            "kind": "synthetic",
            "dimensions": {"length_m": 10, "width_m": 8},
            "geometry": {"storeys": 2, "storey_height_m": 3},
        },
    )
    workflow.add_weather(
        id="annual",
        request={
            "kind": "epw",
            "calendar_year": 2023,
            "path": str(files("epsm_rc") / "data/epw/goteborg.epw"),
        },
    )
    workflow.resolve_inputs(directory=tmp_path)
    data = workflow.to_dict()
    building = Building.from_dict(data["subjects"][0]["building"])
    assert building.geometry.conditioned_floor_area_m2 == 160
    assert data["weather"][0]["weather"]["calendar_year"] == 2023
    assert not workflow.preview()[
        "ready"
    ]  # assembly never silently selects fabric/systems


def test_owner_definitions_and_explicit_overrides_are_order_independent():
    from epsm import Building
    from epsm_data import Archetype, OperatingProfile, archetype_to_dict
    from epsm_workflows.bindings import apply_bindings

    building = Building(preset="illustrative-reference")
    profile = OperatingProfile(
        "research:use",
        {
            "building_assumptions": {
                "use": building.operation.with_changes(
                    equipment_gain_w_m2=8
                ).model_dump(mode="json")
            }
        },
    )
    archetype = Archetype("research:archetype", "Example", operating_profile=profile)
    first = WorkflowSpec.template().set("envelope.archetype", archetype)
    assert first.to_dict()["model_inputs"]["envelope.archetype"][
        "value"
    ] == archetype_to_dict(archetype)
    values = {
        "operation.equipment_gain_w_m2": 12,
        "envelope.archetype": archetype_to_dict(archetype),
    }
    a = apply_bindings(building, values)
    b = apply_bindings(building, dict(reversed(list(values.items()))))
    assert a.operation == b.operation and a.operation.equipment_gain_w_m2 == 12


def test_named_scenarios_cannot_impersonate_generated_case_axes():
    from epsm import Building

    workflow = WorkflowSpec.template("comparative")
    workflow.add_subject(Building(preset="illustrative-reference"), id="one")
    for name in ("baseline", "sample-000000"):
        with pytest.raises(ValueError, match="reserved"):
            workflow.add_scenario(name, Building(preset="illustrative-reference"))


def test_weather_acquisition_identifier_cannot_escape_artifact_directory(
    tmp_path, monkeypatch
):
    import smhi2epw
    from epsm import Building

    observed = []

    def stop(locations, years, *, output_dir, **options):
        observed.append(output_dir)
        raise RuntimeError("No acquisition requested by this path check")

    monkeypatch.setattr(smhi2epw, "compile_amy_batch", stop)
    workflow = WorkflowSpec.template()
    workflow.add_subject(Building(preset="illustrative-reference"), id="one")
    workflow.add_weather(
        id="../../escape",
        request={
            "kind": "amy_batch",
            "years": [2023],
            "locations": [{"name": "gbg", "station_id": 71420}],
        },
    )
    with pytest.raises(RuntimeError, match="No acquisition"):
        workflow.resolve_inputs(directory=tmp_path / "allowed")
    assert observed[0].resolve().is_relative_to((tmp_path / "allowed").resolve())


def test_composed_owner_schema_keeps_workflow_container_constraints():
    import jsonschema

    workflow = WorkflowSpec.template()
    workflow.add_weather(
        id="amy",
        request={
            "kind": "amy_batch",
            "years": [2023],
            "locations": [{"name": "gbg", "station_id": 71420}],
        },
    )
    data = workflow.to_dict()
    data["weather"][0]["request"]["locations"] = []
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(data, workflow.schema())


@pytest.mark.parametrize(
    "choices", [[20.5, 21.5], [True, 20], [19, 22], [20, float("nan")]]
)
def test_explicit_integer_choices_are_integral_finite_and_inside_bounds(choices):
    with pytest.raises(ValueError, match="integer|finite"):
        WorkflowSpec.template().vary(
            "operation.heating_setpoint",
            kind="integer",
            min=20,
            max=21,
            choices=choices,
        )
