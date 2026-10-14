"""Subdivision support, target isolation and construction-only fitting checks."""

from copy import deepcopy

import pytest

from epsm_workflows import mf_parent_dynamic as parent
from epsm_workflows import mf_parent_subdivision as children
from epsm_workflows.campaign import atomic_json
from epsm_workflows.cohort_transfer.common import digest, property_folds, read


def row(index, **changes):
    return {
        "building_id": f"building-{index}",
        "property_key": f"property-{index}",
        "era": "SE.02",
        "compactness": 0.3 if index % 2 else 0.9,
        "wall_area_per_atemp": 0.8 if index % 2 else 1.8,
        "ventilation": "F" if index % 3 else "FTX",
        "status": "prepared",
        "use_group": "multi_family",
        "heating_family": "district_heat",
        "correction_status": "normal_use_adjusted_pre_weather_correction",
        "target": 60.0 if index % 2 else 100.0,
    } | changes


class Evaluator:
    def __init__(self):
        self.calls = []

    def prediction(self, source, parameters):
        return (
            100.0
            + 50.0 * (parameters.get("window_u_multiplier", 1.0) - 1.0)
            + 20.0 * (parameters.get("infiltration_multiplier", 1.0) - 1.0)
        )

    def batch(self, rows, designs):
        self.calls.append((deepcopy(rows), deepcopy(designs)))
        return {
            (source["building_id"], name): self.prediction(source, parameters)
            for source in rows
            for name, parameters in designs.items()
        }


def models():
    return {
        "SE.02": {
            "active": ["infiltration_multiplier", "window_u_multiplier"],
            "fit_fabric_controls": ["window_u_multiplier"],
            "register": {
                "window_u_multiplier": {"baseline": 1.0, "bounds": [0.4, 1.2]},
                "infiltration_multiplier": {"baseline": 1.0, "bounds": [0.5, 2.0]},
            },
            "designs": {
                "original": {},
                "low": {"window_u_multiplier": 0.4, "infiltration_multiplier": 2.0},
                "high": {"window_u_multiplier": 1.2, "infiltration_multiplier": 2.0},
            },
        }
    }


def setup_prediction(monkeypatch, evaluator):
    monkeypatch.setattr(
        parent,
        "_predict_estimate",
        lambda source, parameters, model: evaluator.prediction(source, parameters),
    )


def test_physical_assignment_removes_targets_renovation_and_operating_fields():
    source = row(
        0,
        reno_window=1,
        completed_window_replacement=True,
        designed_flow_l_s_m2=0.8,
        air_exchange_w_k_per_atemp=4.0,
        transmission_w_k_per_atemp=2.0,
        heating_kwh=10000,
    )
    descriptor = children.physical_rows([source])[0]
    assert "compactness" in descriptor
    assert not {
        "target",
        "heating_kwh",
        "reno_window",
        "completed_window_replacement",
        "ventilation",
        "designed_flow_l_s_m2",
        "transmission_w_k_per_atemp",
        "air_exchange_w_k_per_atemp",
    } & set(descriptor)


def test_child_fitting_preserves_parent_operation_and_confirms_native(
    tmp_path, monkeypatch
):
    evaluator = Evaluator()
    setup_prediction(monkeypatch, evaluator)
    rows = [row(index, target=60.0) for index in range(30)]
    fit = children.fit_child(
        rows,
        (),
        {"infiltration_multiplier": 0.5, "window_u_multiplier": 1.0},
        evaluator,
        models(),
        output=tmp_path / "child.json",
    )
    assert fit["parameters"] == {
        "infiltration_multiplier": 0.5,
        "window_u_multiplier": 0.4,
    }
    assert fit["metrics"]["mae_kwh_m2"] == pytest.approx(0.0)
    assert all(
        parameters["infiltration_multiplier"] == 0.5
        for _, designs in evaluator.calls
        for parameters in designs.values()
    )
    assert len(fit["training_properties"]) == 30
    assert fit["operating_parameters_preserved"] is True


def test_child_support_counts_properties_instead_of_rows(tmp_path, monkeypatch):
    evaluator = Evaluator()
    setup_prediction(monkeypatch, evaluator)
    sources = [row(index, property_key="one-property") for index in range(40)]
    with pytest.raises(ValueError, match="30 training properties"):
        children.fit_child(
            sources, (), {}, evaluator, models(), output=tmp_path / "child.json"
        )
    assert not evaluator.calls


def test_child_rejects_held_out_property_and_cross_era_fit(tmp_path, monkeypatch):
    evaluator = Evaluator()
    setup_prediction(monkeypatch, evaluator)
    sources = [row(index) for index in range(30)]
    with pytest.raises(ValueError, match="Held-out property"):
        children.fit_child(
            sources,
            {sources[0]["property_key"]},
            {},
            evaluator,
            models(),
            output=tmp_path / "child.json",
        )
    sources[0]["era"] = "SE.01"
    with pytest.raises(ValueError, match="within one era"):
        children.fit_child(
            sources, (), {}, evaluator, models(), output=tmp_path / "child.json"
        )
    assert not evaluator.calls


@pytest.mark.parametrize(
    "controls",
    [
        ["window_u_multiplier", "wall_resistance", "roof_resistance"],
        ["infiltration_multiplier"],
        ["setpoint_offset_c"],
    ],
)
def test_children_cannot_add_unselected_or_operating_controls(
    tmp_path, monkeypatch, controls
):
    evaluator = Evaluator()
    setup_prediction(monkeypatch, evaluator)
    bank = models()
    bank["SE.02"]["fit_fabric_controls"] = controls
    with pytest.raises(ValueError, match="two screened construction controls"):
        children.fit_child(
            [row(index) for index in range(30)],
            (),
            {},
            evaluator,
            bank,
            output=tmp_path / "child.json",
        )


def opaque_models():
    register = {
        "infiltration_multiplier": {"baseline": 1.0, "bounds": [0.5, 2.0]},
        "window_u_multiplier": {"baseline": 1.0, "bounds": [0.4, 1.2]},
        "wall_insulation_overlay_m": {
            "baseline": 0.0,
            "bounds": [0.0, 0.15],
            "baseline_wall_r_m2k_w": 0.6709432114378908,
            "overlay_conductivity_w_mk": 0.036,
        },
        "roof_resistance": {
            "baseline": 1.0,
            "bounds": [0.8, 1.6],
            "resistance_basis": "total_assembly_r",
        },
    }
    return {
        "SE.01": {
            "active": [
                "infiltration_multiplier",
                "wall_insulation_overlay_m",
                "roof_resistance",
            ],
            "fit_fabric_controls": ["wall_insulation_overlay_m", "roof_resistance"],
            "register": register,
            "designs": {
                "original": {},
                "fit": {
                    "infiltration_multiplier": 2.0,
                    "wall_insulation_overlay_m": 0.1,
                    "roof_resistance": 1.6,
                },
                "thicker": {
                    "wall_insulation_overlay_m": 0.15,
                    "roof_resistance": 1.6,
                },
            },
            "alternate_blocks": {
                "pooled": {
                    "active": ["infiltration_multiplier", "window_u_multiplier"],
                    "register": register,
                }
            },
        }
    }


class OpaqueEvaluator(Evaluator):
    def prediction(self, source, parameters):
        return (
            super().prediction(source, parameters)
            - 100 * parameters.get("wall_insulation_overlay_m", 0.0)
            - 20 * (parameters.get("roof_resistance", 1.0) - 1.0)
        )


def test_child_alternative_block_resets_pooled_windows_and_keeps_native_parent(
    tmp_path, monkeypatch
):
    evaluator = OpaqueEvaluator()
    sources = [row(index, era="SE.01", target=68.0) for index in range(30)]
    frozen_parent = {"infiltration_multiplier": 0.5, "window_u_multiplier": 0.6}
    original_parent = deepcopy(frozen_parent)
    emulated = []

    def estimate(source, parameters, model):
        # The comparison parent is native; the primary child block cannot carry
        # a non-prior pooled window alongside operation, wall and roof.
        assert parameters["window_u_multiplier"] == 1.0
        emulated.append(deepcopy(parameters))
        return evaluator.prediction(source, parameters)

    monkeypatch.setattr(parent, "_predict_estimate", estimate)
    fit = children.fit_child(
        sources,
        (),
        frozen_parent,
        evaluator,
        opaque_models(),
        output=tmp_path / "child.json",
    )
    assert frozen_parent == original_parent
    assert emulated
    assert fit["parameters"] == {
        "infiltration_multiplier": 0.5,
        "window_u_multiplier": 1.0,
        "wall_insulation_overlay_m": 0.1,
        "roof_resistance": 1.6,
    }
    assert fit["metrics"]["mae_kwh_m2"] == pytest.approx(0.0)
    assert fit["fitting_block"] == [
        "infiltration_multiplier",
        "wall_insulation_overlay_m",
        "roof_resistance",
    ]
    assert fit["candidate_construction_reset_to_prior"] == {
        "window_u_multiplier": {"parent_value": 0.6, "child_prior": 1.0}
    }
    assert fit["native_parent_parameters"] == original_parent
    comparison = next(
        item for item in fit["native_finalists"] if item["id"] == "parent"
    )
    assert comparison["parameters"] == original_parent
    assert comparison["metrics"]["mae_kwh_m2"] == pytest.approx(2.0)
    for _, designs in evaluator.calls:
        for name, parameters in designs.items():
            assert parameters["infiltration_multiplier"] == 0.5
            assert parameters["window_u_multiplier"] == (
                0.6 if name == "parent" else 1.0
            )
            varied = {
                n
                for n, value in parameters.items()
                if value != opaque_models()["SE.01"]["register"][n]["baseline"]
            }
            assert len(varied) <= 3


@pytest.mark.parametrize(
    "active,controls",
    [
        (
            ["infiltration_multiplier", "flow_multiplier", "window_u_multiplier"],
            ["window_u_multiplier"],
        ),
        (
            [
                "infiltration_multiplier",
                "window_u_multiplier",
                "wall_resistance",
                "roof_resistance",
            ],
            ["window_u_multiplier", "roof_resistance"],
        ),
        (["infiltration_multiplier", "window_u_multiplier"], ["roof_resistance"]),
    ],
)
def test_child_rejects_unsupported_primary_or_multiple_operating_blocks(
    tmp_path, monkeypatch, active, controls
):
    evaluator = Evaluator()
    setup_prediction(monkeypatch, evaluator)
    bank = models()
    bank["SE.02"]["active"] = active
    bank["SE.02"]["fit_fabric_controls"] = controls
    with pytest.raises(ValueError, match="supported primary block"):
        children.fit_child(
            [row(index) for index in range(30)],
            (),
            {},
            evaluator,
            bank,
            output=tmp_path / "child.json",
        )
    assert not evaluator.calls


def test_child_cannot_invent_prior_for_unsupported_parent_construction(
    tmp_path, monkeypatch
):
    evaluator = Evaluator()
    setup_prediction(monkeypatch, evaluator)
    with pytest.raises(ValueError, match="no declared prior"):
        children.fit_child(
            [row(index) for index in range(30)],
            (),
            {"wall_resistance": 1.4},
            evaluator,
            models(),
            output=tmp_path / "child.json",
        )
    assert not evaluator.calls


def test_child_cannot_inherit_another_non_prior_operating_axis(tmp_path, monkeypatch):
    evaluator = Evaluator()
    setup_prediction(monkeypatch, evaluator)
    bank = models()
    bank["SE.02"]["register"]["flow_multiplier"] = {
        "baseline": 1.0,
        "bounds": [0.8, 1.2],
    }
    with pytest.raises(ValueError, match="outside its supported primary block"):
        children.fit_child(
            [row(index) for index in range(30)],
            (),
            {"infiltration_multiplier": 0.5, "flow_multiplier": 1.1},
            evaluator,
            bank,
            output=tmp_path / "child.json",
        )
    assert not evaluator.calls


def test_child_without_selected_fabric_inherits_untouched_native_parent(
    tmp_path, monkeypatch
):
    evaluator = Evaluator()
    monkeypatch.setattr(
        parent,
        "_predict_estimate",
        lambda *args: pytest.fail(
            "Inherited parent baseline must use native predictions"
        ),
    )
    bank = models()
    bank["SE.02"]["fit_fabric_controls"] = []
    frozen_parent = {"infiltration_multiplier": 0.5, "window_u_multiplier": 0.6}
    fit = children.fit_child(
        [row(index, target=70.0) for index in range(30)],
        (),
        frozen_parent,
        evaluator,
        bank,
        output=tmp_path / "child.json",
    )
    assert fit["parameters"] == frozen_parent
    assert fit["metrics"]["mae_kwh_m2"] == 0.0
    assert fit["fitting_block"] == ["infiltration_multiplier"]
    assert len(evaluator.calls) == 1


def fake_group(rows, k, minimum):
    assert k == 2 and minimum == 30
    assert all(
        "target" not in source and "ventilation" not in source for source in rows
    )
    labels = [int(source["compactness"] > 0.6) for source in rows]
    support = [
        len(
            {
                source["property_key"]
                for source, label in zip(rows, labels, strict=True)
                if label == value
            }
        )
        for value in (0, 1)
    ]
    if min(support) < minimum:
        raise ValueError("insufficient child support")
    return {
        "k": 2,
        "labels": labels,
        "property_counts": support,
        "distance_limits": [0.5, 0.5],
        "missingness_ari": 0.0,
    }


def fake_assign(rows, model):
    return [int(source["compactness"] > 0.6) for source in rows], [0.0] * len(rows)


def nested_fixture(tmp_path, monkeypatch, *, stability_value=1.0):
    # A second sparse era changes the global inner-fold ordering. This checks
    # that era-local gates reuse existing parent fold identities correctly.
    sources = [row(index) for index in range(180)] + [
        row(index + 180, era="SE.01") for index in range(15)
    ]
    targets = {source["building_id"]: source.pop("target") for source in sources}
    atomic_json(tmp_path / "features.json", sources)
    for source in sources:
        atomic_json(
            tmp_path / "targets" / f"{digest(source['property_key'])}.json",
            {source["building_id"]: targets[source["building_id"]]},
        )
    atomic_json(
        tmp_path / "parent-oof.json",
        {"predictions": {source["building_id"]: 90.0 for source in sources}},
    )
    outer_folds = property_folds(sources, 5, seed=parent.SEED)
    for outer in range(5):
        training = [
            source for source in sources if outer_folds[source["property_key"]] != outer
        ]
        inner_folds = property_folds(training, 3, seed=parent.SEED + outer + 1)
        base = {
            "pooled": {"infiltration_multiplier": 0.5, "window_u_multiplier": 1.0},
            "by_era": {},
            "scope": "pooled",
            "operating_shared": {"infiltration_multiplier": 0.5},
        }
        folder = tmp_path / "fits" / f"outer-{outer}"
        atomic_json(
            folder / "frozen.json",
            base
            | {
                "training_properties": sorted(
                    {source["property_key"] for source in training}
                )
            },
        )
        for inner in range(3):
            fitting = [
                source
                for source in training
                if inner_folds[source["property_key"]] != inner
            ]
            atomic_json(
                folder / f"inner-{inner}-pooled.json",
                base
                | {
                    "training_properties": sorted(
                        {source["property_key"] for source in fitting}
                    )
                },
            )
    monkeypatch.setattr(children, "fit_group", fake_group)
    monkeypatch.setattr(children, "assign", fake_assign)
    stability_calls = []

    def stable(rows, model, *, resamples, minimum):
        stability_calls.append((resamples, minimum))
        assert resamples == 20 and minimum == 30
        return {
            "median_ari": stability_value,
            "resample_ari": [stability_value] * resamples,
        }

    monkeypatch.setattr(children, "stability", stable)
    evaluator = Evaluator()
    setup_prediction(monkeypatch, evaluator)
    return sources, evaluator, stability_calls


def test_nested_physical_children_pass_inner_gates_and_predict_outer_properties(
    tmp_path, monkeypatch
):
    rows, evaluator, stability_calls = nested_fixture(tmp_path, monkeypatch)
    result = children.evaluate_subdivisions(tmp_path, evaluator, models())
    assert result["support"]["selected_era_folds"] == 5
    assert (
        result["comparison"]["candidate"]["mae_kwh_m2"]
        < result["comparison"]["baseline"]["mae_kwh_m2"]
    )
    assert result["comparison"]["coverage_change"] > 0
    assert result["adoption"]["adopt"]
    assert result["deployment"]["status"] == "development_candidate_only"
    assert not result["deployment"]["selected_default_assignment_includes_children"]
    assert not result["deployment"]["full_training_child_profiles_exported"]
    assert len(result["predictions"]) == len(rows)
    assert len(stability_calls) == 20
    assert all(
        parameters.get("infiltration_multiplier", 1.0) == 0.5
        for _, designs in evaluator.calls
        for parameters in designs.values()
    )
    for audit in result["audits"]:
        if audit["era"] == "SE.01":
            assert not audit["selected"]
            continue
        frozen = read(audit["frozen_profile"])
        assert not set(frozen["training_properties"]) & set(
            frozen["evaluation_properties"]
        )
        for inner in audit["inner_gate"]["inner_folds"]:
            assert not set(inner["training_properties"]) & set(
                inner["validation_properties"]
            )
    for source in rows:
        if source["era"] == "SE.01":
            assert result["predictions"][source["building_id"]] == 90.0


def test_unstable_partitions_inherit_all_parent_predictions_without_fitting(
    tmp_path, monkeypatch
):
    sources, evaluator, _ = nested_fixture(tmp_path, monkeypatch, stability_value=0.6)
    result = children.evaluate_subdivisions(tmp_path, evaluator, models())
    assert not evaluator.calls
    assert result["support"]["selected_era_folds"] == 0
    assert result["predictions"] == {source["building_id"]: 90.0 for source in sources}


def test_distance_extrapolation_inherits_parent(tmp_path, monkeypatch):
    evaluator = Evaluator()
    monkeypatch.setattr(
        children, "assign", lambda rows, model: ([0] * len(rows), [2.0] * len(rows))
    )
    sources = [row(0)]
    profile = {
        "grouping": {"distance_limits": [0.5, 0.5]},
        "children": {"0": {"parameters": {"window_u_multiplier": 0.4}}},
    }
    parent_fit = {
        "pooled": {"window_u_multiplier": 1.0, "infiltration_multiplier": 0.5}
    }
    predictions, assignments = children.predict_children(
        sources, profile, parent_fit, evaluator
    )
    assert predictions == [90.0]
    assert assignments[0]["parent_fallback"] is True


def test_existing_inner_parent_cannot_contain_validation_targets(tmp_path, monkeypatch):
    sources, evaluator, _ = nested_fixture(tmp_path, monkeypatch)
    first = tmp_path / "fits" / "outer-0" / "inner-0-pooled.json"
    atomic_json(
        first,
        read(first)
        | {"training_properties": [source["property_key"] for source in sources]},
    )
    with pytest.raises(ValueError, match="includes subdivision validation properties"):
        children.evaluate_subdivisions(tmp_path, evaluator, models())


def test_unsupported_stock_panel_skips_clustering_and_keeps_parent(
    tmp_path, monkeypatch
):
    rows = [row(index) for index in range(25)]
    for source in rows:
        target = source.pop("target")
        atomic_json(
            tmp_path / "targets" / f"{digest(source['property_key'])}.json",
            {source["building_id"]: target},
        )
    atomic_json(tmp_path / "features.json", rows)
    atomic_json(
        tmp_path / "parent-oof.json",
        {"predictions": {source["building_id"]: 90.0 for source in rows}},
    )
    for fold in range(5):
        atomic_json(
            tmp_path / "fits" / f"outer-{fold}" / "frozen.json",
            {"pooled": {}, "training_properties": []},
        )
    monkeypatch.setattr(
        children,
        "fit_group",
        lambda *args, **kwargs: pytest.fail("Unsupported panel must not cluster"),
    )
    evaluator = Evaluator()
    result = children.evaluate_subdivisions(tmp_path, evaluator, models())
    assert not evaluator.calls
    assert not result["support"]["selected_era_folds"]
    assert result["comparison"]["coverage_change"] == 0.0


def test_actual_medoid_partition_has_supported_stable_physical_children():
    sources = [row(index, reno_window=index % 3) for index in range(100)]
    model, audit = children._partition(sources)
    assert model is not None
    assert min(model["property_counts"]) == 50
    assert audit["stability"]["median_ari"] == pytest.approx(1.0)
    assert len(audit["stability"]["resample_ari"]) == 20
    assert audit["physical_fields"] == list(children.PHYSICAL_FIELDS)
