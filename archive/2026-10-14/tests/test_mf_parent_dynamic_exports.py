"""Portable physical exports and failure-safe final MF report rendering."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from epsm_workflows import mf_parent_dynamic as study
from epsm_workflows.campaign import atomic_json
from epsm_workflows.cohort_transfer.common import read
from epsm_workflows.mf_stock_history import engineering_prior


def profiles():
    parameters = {
        "recovery": 0.8,
        "basement_setpoint_c": 18.0,
        "window_u_multiplier": 0.8,
        "infiltration_multiplier": 0.75,
    }
    return {
        "pooled": parameters,
        "by_era": {},
        "support_by_era": {"SE.02": 40},
        "pooled_bounds": {
            "recovery": [0.5, 0.85],
            "basement_setpoint_c": [11, 21],
            "window_u_multiplier": [0.4, 1.2],
            "infiltration_multiplier": [0.5, 2.0],
        },
        "measurement_years": [2023],
        "adoption_status": {"adopt": False},
    }


@pytest.mark.parametrize("ventilation", ["S", "F", "FT"])
def test_portable_assignment_deactivates_recovery_outside_ftx(ventilation):
    result = study.assignment(
        {
            "era": "SE.02",
            "ventilation": ventilation,
            "heated_basement": "yes",
            "heated_basement_floors": 1,
        },
        profiles(),
    )
    assert "recovery" not in result["parameters"]
    assert "recovery" not in result["profile"]["parameters"]
    assert result["parameters"]["window_u_multiplier"] == 0.8
    assert result["parameters"]["infiltration_multiplier"] == 0.75


def test_portable_assignment_retains_applicable_ftx_and_basement_controls():
    result = study.assignment(
        {
            "era": "SE.02",
            "ventilation": "FTX",
            "heated_basement": "yes",
            "heated_basement_floors": 1,
        },
        profiles(),
    )
    assert result["parameters"]["recovery"] == 0.8
    assert result["parameters"]["basement_setpoint_c"] == 18
    assert result["profile"]["parameters"] == result["parameters"]
    assert result["profile"]["resistance_basis"] == "total_assembly_r"
    assert result["profile"]["infiltration_basis"] == "atemp_area_rate"


@pytest.mark.parametrize(
    "basement",
    [
        {"heated_basement": "no", "heated_basement_floors": 0},
        {"heated_basement": "no"},
        {"heated_basement_floors": 0},
        {},
    ],
)
def test_portable_assignment_drops_unconfirmed_conditioned_basement_control(basement):
    result = study.assignment(
        {"era": "SE.02", "ventilation": "FTX"} | basement,
        profiles(),
    )
    assert "basement_setpoint_c" not in result["parameters"]
    assert result["parameters"]["recovery"] == 0.8


def test_forward_filter_does_not_mutate_frozen_shared_profiles():
    shared = profiles()
    before = deepcopy(shared)
    study.assignment({"era": "SE.02", "ventilation": "F"}, shared)
    assert shared == before
    with pytest.raises(ValueError):
        study.assignment({"era": "SE.02", "target": 100}, shared)


def report_fixture(tmp_path, monkeypatch, *, current_failed=False):
    rows = [
        {
            "building_id": f"b{i}",
            "property_key": f"p{i}",
            "era": "SE.02",
            "ventilation": "F" if i % 2 else "FTX",
            "height_m": 12,
            "heated_basement": "no",
            "heated_basement_floors": 0,
            "construction_year": 1965,
            "measurement_year": 2023,
            "catalogue_climate_zone": "SE.Zone3",
        }
        for i in range(10)
    ]
    rows[0].update(heated_basement="yes", heated_basement_floors=1)
    values = lambda number: {row["building_id"]: number for row in rows}
    default = engineering_prior()
    parent_metric = {
        "attempted": len(rows),
        "coverage20": 1.0,
        "mae_kwh_m2": 10.0,
        "bias_kwh_m2": 10.0,
    }
    for filename, payload in (
        ("features.json", rows),
        ("current-panel.json", values(None if current_failed else 130)),
        ("shading-panel.json", values(120)),
        ("corrected-native-predictions.json", values(120)),
        (
            "parent-oof.json",
            {
                "predictions": values(110),
                "folds": [
                    {"inner_scores": {"pooled": parent_metric, "era": parent_metric}}
                ],
            },
        ),
        ("dynamic-oof.json", {"predictions": values(105)}),
        (
            "dynamic-panel.json",
            {
                "predictions": {"life-1.000": values(110), "life-0.750": values(105)},
                "controls": {
                    "life-1.000": {"lifetime_multiplier": 1.0},
                    "life-0.750": {"lifetime_multiplier": 0.75},
                },
                "prior": default,
            },
        ),
        ("subdivision-oof.json", {"predictions": values(110), "status": "candidate"}),
        ("preparation-summary.json", {"selected": len(rows), "prepared": len(rows)}),
        (
            "emulators.json",
            {
                "SE.02": {
                    "register": {
                        name: {"bounds": bounds}
                        for name, bounds in profiles()["pooled_bounds"].items()
                    }
                }
            },
        ),
    ):
        atomic_json(tmp_path / filename, payload)

    class TargetStore:
        def __init__(self, root):
            pass

        def evaluation(self, records, marker):
            return [dict(record) | {"target": 100.0} for record in records]

    monkeypatch.setattr(study, "TargetStore", TargetStore)
    monkeypatch.setattr(study, "subdivision_support", lambda rows: {})
    monkeypatch.setattr(
        study,
        "fit_parents",
        lambda *args, **kwargs: {
            "pooled": profiles()["pooled"],
            "by_era": {},
            "support_by_era": {"SE.02": len(rows)},
        },
    )
    monkeypatch.setattr(
        study,
        "_shared_register",
        lambda *args: {
            name: {"bounds": bounds}
            for name, bounds in profiles()["pooled_bounds"].items()
        },
    )
    from epsm_workflows import mf_comparison

    monkeypatch.setattr(
        mf_comparison,
        "empirical_benchmarks",
        lambda *args, **kwargs: {
            "predictions": {
                "empirical_parent": values(115),
                "reliable_features": values(108),
            },
        },
    )

    def assignment_export(root, records, parent_profiles, history_priors):
        # Reference integrity is covered by test_mf_selected_assignments. This
        # fixture isolates report rendering from native source/spec artifacts.
        selected = {
            row["building_id"]: study.selected_assignment(
                study._assignment_descriptors(row), parent_profiles, history_priors
            )
            for row in records
        }
        atomic_json(root / "assignments.json", selected)
        return {"selected": selected}

    monkeypatch.setattr(study, "export_assignments", assignment_export, raising=False)
    evaluator = SimpleNamespace(config={"wall_overlay": None})
    return rows, evaluator


def test_report_exports_complete_dynamic_controls_and_all_comparison_intervals(
    tmp_path, monkeypatch
):
    rows, evaluator = report_fixture(tmp_path, monkeypatch)
    result = study.report(tmp_path, evaluator)
    exported = read(tmp_path / "dynamic-priors.json")
    assert exported["controls"] == {
        "lifetime_multiplier": 0.75,
        "roof_upgrade_probability": 0.5,
        "facade_upgrade_probability": 0.5,
    }
    assert exported["inactive_upgrade_mappings"] == ["roof", "facade"]
    assert exported["construction_basis"] == "original_construction"
    assert not exported["identified_renovation_histories"]
    for method in (
        "physical_subdivision_candidate",
        "empirical_parent",
        "reliable_features",
    ):
        assert len(result["metrics"][method]["coverage20_95_interval"]) == 2
        assert result["metrics"][method]["attempted"] == len(rows)
    assert not result["confirmation"]
    assignments = read(tmp_path / "assignments.json")
    assert assignments["b0"]["parameters"]["recovery"] == 0.8
    assert assignments["b0"]["parameters"]["basement_setpoint_c"] == 18
    assert "recovery" not in assignments["b1"]["parameters"]
    assert "basement_setpoint_c" not in assignments["b1"]["parameters"]


def test_report_renders_all_failed_native_method_without_formatting_none(
    tmp_path, monkeypatch
):
    _, evaluator = report_fixture(tmp_path, monkeypatch, current_failed=True)
    result = study.report(tmp_path, evaluator)
    assert result["metrics"]["current"]["failed"] == 10
    assert result["metrics"]["current"]["mae_kwh_m2"] is None
    markdown = (tmp_path / "report.md").read_text()
    assert "| current |" in markdown
    assert "physical_subdivision_candidate" in markdown
    assert "reliable_features" in markdown
