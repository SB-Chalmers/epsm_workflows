"""Reusable physical variation, compact review and saved-study compatibility."""

import json
from importlib.resources import files
from pathlib import Path

import pytest
from epsm import Building, Weather
from epsm_data.domain import BuildingElement as Role

from epsm_workflows import WorkflowSpec
from epsm_workflows.bindings import apply_bindings


def authored(samples=4):
    building = Building(preset="illustrative-reference")
    workflow = WorkflowSpec.template(samples=samples, seed=19)
    workflow.add_subject(building, id="teaching", group="family-a")
    workflow.add_weather(Weather.from_epw(files("epsm_rc") / "data/epw/goteborg.epw"), id="weather")
    return workflow, building


def test_existing_saved_definition_and_seeded_rows_are_unchanged():
    fixture = json.loads((Path(__file__).parent / "fixtures/existing-authoring-v1.json").read_text())
    workflow = WorkflowSpec.from_dict(fixture["definition"])
    assert workflow.to_dict() == fixture["definition"]
    assert workflow.id == fixture["id"]
    assert workflow.samples() == fixture["samples"]


def test_log_uniform_is_an_opt_in_marginal_and_roundtrips():
    from epsm_workflows.distributions import log_uniform, sample_quantile

    distribution = log_uniform()
    assert sample_quantile(distribution, .5, lower=100, upper=10000) == pytest.approx(1000)
    for lower, upper in ((0, 100), (-1, 10), (1, 1)):
        with pytest.raises(ValueError):
            sample_quantile(distribution, .5, lower=lower, upper=upper)
    workflow = WorkflowSpec.template(samples=8).vary(
        "geometry.footprint_area_m2", min=100, max=10000, distribution=distribution,
    )
    assert WorkflowSpec.from_dict(workflow.to_dict()).samples() == workflow.samples()


def test_log_uniform_endpoint_roundoff_stays_inside_its_finite_bounds():
    import math
    from epsm_workflows.distributions import log_uniform, sample_quantile

    for low, high in ((5, 50), (10, 100), (1e-200, 1e200)):
        for probability in (math.nextafter(0, 1), math.nextafter(1, 0)):
            assert low <= sample_quantile(log_uniform(), probability, lower=low, upper=high) <= high


def test_area_and_context_use_owner_operations_after_sampled_geometry():
    workflow, building = authored()
    before = building.to_dict()
    workflow.set("context.synthetic", {"preset": "rural", "seed": 7, "radius_m": 40})
    workflow.vary("geometry.footprint_area_m2", min=100, max=400)
    workflow.vary("geometry.storeys", choices=[2, 4], kind="integer")
    report = workflow.preview(check_ready=False)
    assert report["ready"], report["diagnostics"]
    from epsm_core import synthetic_context

    for row in report["cases"]:
        candidate = Building.from_dict(row["building"])
        assert candidate.geometry.parts[0].footprint.area_m2 == pytest.approx(row["parameters"]["geometry.footprint_area_m2"])
        expected = synthetic_context(candidate.geometry, "rural", seed=7, radius_m=40)
        assert candidate.context.model_dump(mode="json") == expected.model_dump(mode="json")
    assert building.to_dict() == before


def test_physical_u_target_preserves_materials_and_selected_element_ids():
    workflow, building = authored()
    wall = next(e for e in building.elements if e.role == Role.EXT_WALL)
    workflow.vary("envelope.element_u_value", min=.15, max=.3, selector={
        "elements": [wall.identifier], "component": wall.construction.components[0].name, "layer": 0,
    })
    report = workflow.preview(check_ready=False)
    assert report["ready"], report["diagnostics"]
    for row in report["cases"]:
        candidate = Building.from_dict(row["building"])
        changed = next(e for e in candidate.elements if e.identifier == wall.identifier)
        assert changed.construction.u_value == pytest.approx(row["parameters"]["envelope.element_u_value"])
        assert changed.component_ids == wall.component_ids
        assert not candidate.stages


def test_new_selectors_cannot_be_used_on_unrelated_bindings():
    _, building = authored()
    with pytest.raises(ValueError, match="selector|Selector"):
        apply_bindings(building, {"facade.wwr": .2}, selectors={"facade.wwr": {"component": "insulation", "layer": 0}})


def test_compact_design_description_is_owner_generated_and_has_no_models():
    workflow, _ = authored()
    workflow.set("systems.ventilation.heat_recovery", 0)
    workflow.vary("facade.wwr", min=.1, max=.4)
    workflow.vary("systems.ventilation.topology", choices=["F", "FTX"])
    workflow.set("engines", ["epsm_rc", "energyplus"])
    report = workflow.describe_design(check_ready=False, examples=2)
    assert report["case_count"] == 8
    assert report["design_count"] == 4
    assert report["readiness_checked"] is False
    assert report["counts"]["engine"] == {"epsm_rc": 4, "energyplus": 4}
    assert report["parameters"]["systems.ventilation.topology"]["choices"] == [
        {"value": "F", "count": 4, "choice_index": 0},
        {"value": "FTX", "count": 4, "choice_index": 1},
    ]
    assert len(report["examples"]) == 2
    assert all("building" not in row and "weather" not in row for row in report["examples"])
    assert report["workflow_id"] == workflow.id


def test_context_generation_conflicts_are_explicit_and_leave_template_unchanged():
    _, building = authored()
    before = building.to_dict()
    with pytest.raises(ValueError, match="Conflicting|conflict"):
        apply_bindings(building, {"context.synthetic": {"preset": "rural"}, "context": {}})
    assert building.to_dict() == before


def test_missing_insulation_recipe_remains_a_loadable_unready_draft():
    workflow, _ = authored()
    workflow.vary("envelope.element_u_value", min=.15, max=.3)
    restored = WorkflowSpec.from_dict(workflow.to_dict())
    report = restored.preview(check_ready=False)
    assert not report["ready"]
    assert "requires elements, component and layer" in report["diagnostics"][0]["message"]


def test_previous_unsupported_u_target_draft_retains_its_native_unit_and_id():
    workflow, _ = authored()
    workflow.vary("envelope.element_u_value", min=.15, max=.3)
    old = workflow.to_dict()
    old["parameters"][0]["unit"] = "native"
    from epsm_workflows.records import digest

    restored = WorkflowSpec.from_dict(old)
    assert restored.to_dict() == old
    assert restored.id == digest(old)
    assert not restored.preview(check_ready=False)["ready"]


@pytest.mark.parametrize("area", [0, -1, float("inf")])
def test_invalid_area_is_rejected_by_the_owner_without_mutating_inputs(area):
    _, building = authored()
    before = building.to_dict()
    with pytest.raises(ValueError):
        apply_bindings(building, {"geometry.footprint_area_m2": area})
    assert building.to_dict() == before


def test_fixed_context_is_regenerated_for_reference_and_named_alternative():
    workflow, building = authored()
    workflow.set("context.synthetic", {"preset": "rural", "seed": 3, "radius_m": 35})
    workflow.add_scenario("unchanged-materials", building)
    workflow.set("geometry.footprint_area_m2", 200)
    report = workflow.preview(check_ready=False)
    assert report["ready"], report["diagnostics"]
    assert {row["axes"]["scenario_id"] for row in report["cases"]} == {"baseline", "unchanged-materials"}
    assert all(Building.from_dict(row["building"]).geometry.parts[0].footprint.area_m2 == pytest.approx(200) for row in report["cases"])


def test_context_recipe_choices_have_bounded_descriptions_and_retain_typed_values():
    workflow, _ = authored()
    choices = [{"preset": "rural", "seed": 1, "radius_m": 25}, {"preset": "rural", "seed": 2, "radius_m": 25}]
    workflow.vary("context.synthetic", choices=choices)
    report = workflow.describe_design(check_ready=False)
    assert report["ready"], report["diagnostics"]
    summary = report["parameters"]["context.synthetic"]["choices"]
    assert {row["choice_index"] for row in summary} == {0, 1}
    assert sum(row["count"] for row in summary) == 4
    assert all("choice_id" in row and "value" not in row for row in summary)
    assert all(set(row["parameters"]["context.synthetic"]) == {"value_id", "value_type"} for row in report["examples"])


def test_area_only_preserves_nonuniform_storeys():
    _, building = authored()
    part = building.geometry.parts[0].updated(storey_elevations_m=(0, 3, 6.5, 9.1))
    building.geometry = building.geometry.updated(parts=(part,))
    changed = apply_bindings(building, {"geometry.footprint_area_m2": 250})
    assert changed.geometry.parts[0].storey_elevations_m == part.storey_elevations_m
    assert changed.geometry.parts[0].footprint.area_m2 == pytest.approx(250)


def test_generator_command_cannot_silently_act_as_a_physical_condition():
    workflow, _ = authored()
    workflow.vary("facade.wwr", min=.1, max=.2, applies_when={"context.synthetic": {"preset": "rural"}})
    report = workflow.preview(check_ready=False)
    assert not report["ready"]
    assert "retained physical fields" in report["diagnostics"][0]["message"]


def test_generator_does_not_silently_drop_other_explicit_context_inputs():
    workflow, _ = authored()
    workflow.set("context.buildings", [])
    workflow.vary("context.synthetic", choices=[{"preset": "rural", "radius_m": 25}])
    report = workflow.preview(check_ready=False)
    assert not report["ready"]
    assert "retain_existing" in report["diagnostics"][0]["message"]
    workflow.vary("context.synthetic", choices=[{"preset": "rural", "radius_m": 25, "retain_existing": True}])
    assert workflow.preview(check_ready=False)["ready"]
