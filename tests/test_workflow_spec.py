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
    spec.vary("systems.ventilation.topology", choices=["F", "FTX"],
              distribution=categorical(probabilities=[0, 1]))
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
    jsonschema.validate(facade.glazing.model_dump(mode="json"),
                        options["facade.glazing"]["schema"])


def test_preview_emits_bounded_lifecycle_event(caplog):
    with caplog.at_level("INFO", logger="epsm_workflows.workflow"):
        report = WorkflowSpec.template().preview()
    record = next(row for row in caplog.records
                  if getattr(row, "epsm_event", None) == "study.preview.completed")
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
    spec.add_weather(Weather.from_epw(files("epsm_rc") / "data/epw/goteborg.epw"), id="gbg")
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
    changed = apply_bindings(building, {"operation.heating_setpoint": 19,
                                       "facade.wwr": 0.3})
    assert changed.operation.heating_setpoint.values == (19,) * 24
    assert changed.facade.default_wwr == 0.3
    assert building.facade.default_wwr != changed.facade.default_wwr
    with pytest.raises(ValueError):
        apply_bindings(building, {"facade.wwr": 2})
