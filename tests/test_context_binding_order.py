# SPDX-License-Identifier: MIT
"""Whole context records and generation retain explicit scientific choices."""

from importlib.resources import files

import pytest

from epsm import Building, Weather
from epsm_core import ContextBuilding, ContextSpec, Footprint, synthetic_context
from epsm_workflows import WorkflowSpec
from epsm_workflows.bindings import apply_bindings


def context(identifier="explicit-neighbour", *, simplify=False):
    return ContextSpec(
        buildings=(ContextBuilding(
            identifier=identifier,
            footprints=(Footprint(exterior=((100, 100), (105, 100), (105, 105), (100, 105))),),
            height_m=9,
        ),),
        simplify_small_caps=simplify,
        generation={"source": identifier},
    )


def workflow(samples=2):
    building = Building(preset="illustrative-reference")
    experiment = WorkflowSpec.template(samples=samples).add_subject(building, id="one")
    experiment.add_weather(Weather.from_epw(files("epsm_rc") / "data/epw/goteborg.epw"), id="weather")
    return experiment, building


def test_fixed_whole_context_cannot_be_silently_replaced_by_generated_choices():
    experiment, building = workflow()
    before = building.to_dict()
    experiment.set("context", context().model_dump(mode="json"))
    experiment.vary("context.synthetic", choices=[{"preset": "rural", "radius_m": 25}])
    report = experiment.preview(check_ready=False)
    assert not report["ready"]
    assert "retain_existing" in report["diagnostics"][0]["message"]
    assert building.to_dict() == before


def test_retained_whole_context_preserves_semantics_after_geometry_changes():
    experiment, building = workflow()
    supplied = context()
    experiment.set("context", supplied.model_dump(mode="json"))
    experiment.vary("geometry.footprint_area_m2", min=100, max=300)
    experiment.vary("context.synthetic", choices=[{
        "preset": "rural", "radius_m": 25, "retain_existing": True,
    }])
    report = experiment.preview(check_ready=False)
    assert report["ready"], report["diagnostics"]
    for row in report["cases"]:
        result = Building.from_dict(row["building"])
        expected = synthetic_context(result.geometry, "rural", radius_m=25, existing=supplied)
        assert result.context.model_dump(mode="json") == expected.model_dump(mode="json")
        assert result.context.simplify_small_caps is False
        assert result.context.generation["supplied_generation"] == supplied.generation
    assert building.context != supplied


def test_whole_context_choices_require_explicit_retention_by_fixed_generation():
    experiment, _ = workflow()
    choices = [context("first").model_dump(mode="json"), context("second", simplify=True).model_dump(mode="json")]
    experiment.vary("context", choices=choices)
    experiment.set("context.synthetic", {"preset": "rural", "radius_m": 25})
    report = experiment.preview(check_ready=False)
    assert not report["ready"]
    assert "retain_existing" in report["diagnostics"][0]["message"]
    experiment.set("context.synthetic", {"preset": "rural", "radius_m": 25, "retain_existing": True})
    report = experiment.preview(check_ready=False)
    assert report["ready"], report["diagnostics"]
    for row in report["cases"]:
        result = Building.from_dict(row["building"])
        supplied = ContextSpec.model_validate(row["parameters"]["context"])
        expected = synthetic_context(result.geometry, "rural", radius_m=25, existing=supplied)
        assert result.context.model_dump(mode="json") == expected.model_dump(mode="json")


def test_direct_generation_retains_complete_context_only_when_explicit():
    _, building = workflow()
    supplied = context()
    values = {"context": supplied.model_dump(mode="json"),
              "context.synthetic": {"preset": "rural", "radius_m": 25}}
    with pytest.raises(ValueError, match="retain_existing"):
        apply_bindings(building, values)
    values["context.synthetic"]["retain_existing"] = True
    changed = apply_bindings(building, values)
    expected = synthetic_context(changed.geometry, "rural", radius_m=25, existing=supplied)
    assert changed.context.model_dump(mode="json") == expected.model_dump(mode="json")


@pytest.mark.parametrize("dependency", ["context", "context.simplify_small_caps", "context.generation"])
def test_context_condition_cannot_read_the_state_before_generation(dependency):
    experiment, _ = workflow()
    experiment.set("context", {"simplify_small_caps": False})
    experiment.vary("context.synthetic", choices=[{
        "preset": "rural", "radius_m": 25, "retain_existing": True,
    }])
    experiment.vary("facade.wwr", min=.1, max=.2,
                    applies_when={dependency: False})
    report = experiment.preview(check_ready=False)
    assert not report["ready"]
    issue = report["diagnostics"][0]
    assert issue["code"] == "unresolved_generated_context_condition"
    assert "before generated context resolves" in issue["message"]
    assert "remove the condition or split the design" in issue["message"]


def test_fixed_context_condition_without_generation_keeps_its_existing_behavior():
    experiment, _ = workflow()
    experiment.set("context", {"simplify_small_caps": False})
    experiment.vary("facade.wwr", min=.1, max=.2,
                    applies_when={"context.simplify_small_caps": False})
    report = experiment.preview(check_ready=False)
    assert report["ready"], report["diagnostics"]
    assert all("facade.wwr" in row["parameters"] for row in report["cases"])


def test_context_condition_gate_respects_subject_selectors():
    experiment, building = workflow()
    experiment.add_subject(building, id="two")
    experiment.set("context.synthetic", {"preset": "rural", "radius_m": 25},
                   selector={"subjects": ["one"]})
    experiment.set("context", {"simplify_small_caps": False},
                   selector={"subjects": ["two"]})
    experiment.vary("facade.wwr", min=.1, max=.2, selector={"subjects": ["two"]},
                    applies_when={"context.simplify_small_caps": False})
    report = experiment.preview(check_ready=False)
    assert report["ready"], report["diagnostics"]
    assert all("facade.wwr" in row["parameters"] for row in report["cases"]
               if row["axes"]["building_id"] == "two")


def test_sampled_generation_request_cannot_be_used_as_a_context_condition():
    experiment, _ = workflow()
    request = {"preset": "rural", "radius_m": 25}
    experiment.vary("context.synthetic", choices=[request])
    experiment.vary("facade.wwr", min=.1, max=.2, applies_when={"context.synthetic": request})
    report = experiment.preview(check_ready=False)
    assert not report["ready"]
    assert report["diagnostics"][0]["code"] == "unresolved_generated_context_condition"
