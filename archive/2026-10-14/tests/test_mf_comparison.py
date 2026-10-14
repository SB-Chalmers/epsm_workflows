"""Evaluation denominators, property isolation and deployable benchmark inputs."""

from copy import deepcopy

import pytest

from epsm_workflows.mf_comparison import (
    adoption_gate,
    benchmark_features,
    empirical_benchmarks,
    fit_empirical_parent,
    metrics,
    paired_change,
    physical_subdivision,
    predict_empirical_parent,
)


def rows(n=100):
    return [
        {
            "building_id": f"b{i:03d}",
            "property_key": f"p{i:03d}",
            "target": 100.0,
            "era": "SE.02",
            "catalogue_climate_zone": "SE.Zone3",
            "ventilation": "F",
            "heated_basement": "no",
            "height_m": 12,
            "construction_year": 1961 + i % 15,
            "measurement_year": 2019,
            "atemp_m2": 500 + i * 10,
            "apartments": 8 + i % 5,
            "compactness": 4 + i / max(n, 1),
        }
        for i in range(n)
    ]


def test_coverage_is_inclusive_and_failures_are_retained():
    values = rows(6)
    result = metrics(values, [80, 120, 120.01, None, float("nan"), -1])
    assert result["attempted"] == 6
    assert result["accepted"] == 3
    assert result["failed"] == 3
    assert result["coverage20"] == pytest.approx(2 / 6)
    assert result["mae_kwh_m2"] == pytest.approx((20 + 20 + 20.01) / 3)
    assert result["mae_conditional_on_accepted"]


def test_missing_target_is_not_converted_to_zero():
    for invalid in (None, 0, -1, float("nan"), True):
        panel = rows(1)
        panel[0]["target"] = invalid
        with pytest.raises(ValueError, match="strictly positive"):
            metrics(panel, [100])


def test_property_bootstrap_and_complete_pair_error_statistics():
    panel = rows(3)
    result = paired_change(panel, [110, 110, 150], [100, None, 125], resamples=100)
    assert result["baseline"]["coverage20"] == pytest.approx(2 / 3)
    assert result["candidate"]["coverage20"] == pytest.approx(1 / 3)
    assert result["paired_buildings"] == 2
    assert result["paired_baseline"]["mae_kwh_m2"] == 30
    assert result["paired_candidate"]["mae_kwh_m2"] == 12.5
    assert result["paired_mae_change_kwh_m2"] == -17.5
    assert result["bootstrap"]["properties"] == 3
    assert len(result["paired_error_std_change_95_interval"]) == 2


def test_sibling_buildings_travel_together_in_bootstrap():
    panel = rows(4)
    for item in panel[:3]:
        item["property_key"] = "one-property"
    result = paired_change(
        panel, [150, 150, 150, 100], [100, 100, 100, 150], resamples=1000
    )
    assert result["bootstrap"]["properties"] == 2
    assert result["coverage_change_95_interval"] == [-1, 1]


def test_both_parent_adoption_paths_are_supported():
    panel = rows()
    coverage = adoption_gate(panel, [150] * 100, [110] * 100, resamples=100)
    assert coverage["adopt"]
    assert coverage["comparison"]["coverage_change_95_interval"] == [1, 1]
    mae = adoption_gate(panel, [115] * 100, [110] * 100, resamples=100)
    assert mae["adopt"]
    assert mae["comparison"]["coverage_change"] == 0
    assert not mae["independent_confirmation"]


@pytest.mark.parametrize("kind", ["parent", "dynamic"])
def test_equal_failure_counts_cannot_hide_worse_reported_mae(kind):
    panel = rows()
    baseline = [150] * 30 + [100] * 69 + [None]
    candidate = [110] * 30 + [None] + [100] * 68 + [10000]
    gate = adoption_gate(panel, baseline, candidate, kind=kind, resamples=100)
    comparison = gate["comparison"]
    assert comparison["coverage_change_95_interval"][0] > 0
    assert comparison["baseline"]["failed"] == comparison["candidate"]["failed"] == 1
    assert (
        comparison["paired_candidate"]["mae_kwh_m2"]
        < comparison["paired_baseline"]["mae_kwh_m2"]
    )
    assert comparison["candidate"]["mae_kwh_m2"] > comparison["baseline"]["mae_kwh_m2"]
    assert gate["mae_guards"]["paired_mae_not_worse"]
    assert not gate["mae_guards"]["reported_mae_not_worse"]
    assert not gate["adopt"]
    assert "overall_improvement_threshold_not_met" in gate["reasons"]


@pytest.mark.parametrize("kind", ["parent", "dynamic"])
def test_matching_failures_preserve_both_mae_definitions_and_real_adoption(kind):
    panel = rows()
    baseline = [150] * 30 + [100] * 69 + [None]
    candidate = [110] * 30 + [100] * 69 + [None]
    gate = adoption_gate(panel, baseline, candidate, kind=kind, resamples=100)
    comparison = gate["comparison"]
    assert (
        comparison["baseline"]["mae_kwh_m2"]
        == comparison["paired_baseline"]["mae_kwh_m2"]
    )
    assert (
        comparison["candidate"]["mae_kwh_m2"]
        == comparison["paired_candidate"]["mae_kwh_m2"]
    )
    assert gate["mae_guards"]["paired_mae_not_worse"]
    assert gate["mae_guards"]["reported_mae_not_worse"]
    assert gate["adopt"]


def test_different_failure_identities_can_pass_when_both_mae_definitions_improve():
    panel = rows()
    baseline = [150] * 30 + [100] * 69 + [None]
    candidate = [110] * 30 + [None] + [100] * 69
    gate = adoption_gate(panel, baseline, candidate, resamples=100)
    assert gate["mae_guards"]["paired_mae_not_worse"]
    assert gate["mae_guards"]["reported_mae_not_worse"]
    assert gate["adopt"]


def test_five_percent_parent_mae_path_requires_reported_and_paired_gains():
    panel = rows()
    baseline = [150] * 99 + [None]
    candidate = [None] + [145] * 98 + [600]
    gate = adoption_gate(panel, baseline, candidate, resamples=100)
    assert gate["comparison"]["coverage_change"] == 0
    assert gate["mae_guards"]["paired_mae_gain_at_least_5_percent"]
    assert gate["mae_guards"]["reported_mae_not_worse"]
    assert not gate["mae_guards"]["reported_mae_gain_at_least_5_percent"]
    assert not gate["adopt"]


def test_failure_swap_reported_group_regression_blocks_overall_improvement():
    panel = rows(200)
    for item in panel[:30]:
        item["era"] = "SE.01"
    baseline = [150] * 29 + [None] + [150] * 170
    candidate = [None] + [110] * 28 + [2100] + [110] * 170
    gate = adoption_gate(panel, baseline, candidate, resamples=100)
    assert gate["mae_guards"]["paired_mae_not_worse"]
    assert gate["mae_guards"]["reported_mae_not_worse"]
    assert gate["comparison"]["coverage_change_95_interval"][0] > 0
    group = next(
        item
        for item in gate["group_protections"]
        if item["field"] == "era" and item["group"] == "SE.01"
    )
    assert group["supported"]
    assert group["paired_mae_within_10_percent"]
    assert not group["reported_mae_within_10_percent"]
    assert not group["passed"]
    assert not gate["adopt"]
    assert "supported_group_regressed:era:SE.01" in gate["reasons"]


def test_dynamic_requires_two_points_and_positive_paired_interval():
    panel = rows()
    baseline = [110] * 80 + [150] * 20
    small = [110] * 81 + [150] * 19
    gate = adoption_gate(panel, baseline, small, kind="dynamic", resamples=100)
    assert not gate["adopt"]
    good = adoption_gate(panel, [150] * 100, [110] * 100, kind="dynamic", resamples=100)
    assert good["adopt"]


def test_supported_group_regression_blocks_overall_improvement():
    panel = rows(200)
    for item in panel[:40]:
        item["era"] = "SE.01"
    baseline = [110] * 40 + [150] * 160
    candidate = [140] * 40 + [110] * 160
    gate = adoption_gate(panel, baseline, candidate, resamples=100)
    assert gate["comparison"]["coverage_change"] == pytest.approx(0.6)
    assert not gate["adopt"]
    assert "supported_group_regressed:era:SE.01" in gate["reasons"]


def test_subgroup_support_counts_properties_not_duplicate_buildings():
    panel = rows(40)
    for item in panel[:20]:
        item["era"] = "SE.01"
        item["property_key"] = "same-property"
    gate = adoption_gate(
        panel, [110] * 20 + [150] * 20, [140] * 20 + [100] * 20, resamples=100
    )
    child = next(
        x
        for x in gate["group_protections"]
        if x["field"] == "era" and x["group"] == "SE.01"
    )
    assert child["independent_properties"] == 1
    assert not child["supported"] and child["passed"] is None


def test_candidate_cannot_gain_mae_by_losing_native_predictions():
    panel = rows(100)
    baseline = [150] * 100
    candidate = [100] * 99 + [None]
    gate = adoption_gate(panel, baseline, candidate, resamples=100)
    assert not gate["adopt"]
    assert "candidate_native_failures_increased" in gate["reasons"]


def test_unknown_parent_cells_have_a_frozen_pooled_fallback():
    panel = rows(4)
    model = fit_empirical_parent(panel, shrinkage=20)
    assert predict_empirical_parent(
        [{"era": "unknown", "catalogue_climate_zone": "unknown"}], model
    ) == [100]
    with pytest.raises(ValueError, match="heating observations"):
        predict_empirical_parent([{"era": "SE.02", "target": 90}], model)


def test_reliable_feature_contract_excludes_renovation_energy_and_age():
    value = rows(1)[0]
    frame = benchmark_features([value], strict=False)
    assert "target" not in frame and "age" not in frame
    assert "construction_year" in frame and "measurement_year" in frame
    altered = value | {
        "reno_window": True,
        "reno_roof": False,
        "age": 900,
        "reported_kwh_m2": 1,
    }
    assert frame.equals(benchmark_features([altered], strict=False))
    with pytest.raises(ValueError, match="heating observations"):
        benchmark_features([value])


def test_nested_empirical_benchmarks_isolate_property_targets_and_transforms():
    panel = rows(18)
    # Each property appears twice, in two separate buildings.
    for i, item in enumerate(panel):
        item["property_key"] = f"p{i // 2}"
    targets = {item["building_id"]: 70 + i * 3 for i, item in enumerate(panel)}
    benchmark = empirical_benchmarks(panel, targets, outer_folds=3, inner_folds=2)
    assert set(benchmark["predictions"]["empirical_parent"]) == set(targets)
    assert set(benchmark["predictions"]["reliable_features"]) == set(targets)
    assert not benchmark["feature_contract"]["renovation_fields_used"]
    assert not benchmark["feature_contract"]["energy_features_used"]
    for audit in benchmark["training_audit"]:
        assert not set(audit["training_properties"]) & set(
            audit["evaluation_properties"]
        )
        for inner in audit["inner_folds"]:
            assert not set(inner["training_properties"]) & set(
                inner["validation_properties"]
            )
            assert set(inner["training_properties"]) <= set(
                audit["training_properties"]
            )
            assert set(inner["validation_properties"]) <= set(
                audit["training_properties"]
            )
    # Changing targets of the evaluation fold cannot affect its fitted predictions.
    chosen = benchmark["training_audit"][0]
    changed = deepcopy(targets)
    for item in panel:
        if item["property_key"] in chosen["evaluation_properties"]:
            changed[item["building_id"]] = 1000
    repeat = empirical_benchmarks(panel, changed, outer_folds=3, inner_folds=2)
    for item in panel:
        if item["property_key"] in chosen["evaluation_properties"]:
            for kind in ("empirical_parent", "reliable_features"):
                assert (
                    repeat["predictions"][kind][item["building_id"]]
                    == benchmark["predictions"][kind][item["building_id"]]
                )


def test_subdivision_requires_separate_training_properties_and_support():
    panel = rows(10)
    with pytest.raises(ValueError, match="separate properties"):
        physical_subdivision(panel, panel, [100] * 10, [100] * 10)
    validation = rows(2)
    for item in validation:
        item["property_key"] += "-validation"
    result = physical_subdivision(panel, validation, [150, 150], [100, 100])
    assert not result["supported"]
    assert result["reasons"] == ["insufficient_physical_group_support"]
    with pytest.raises(ValueError, match="30 training properties"):
        physical_subdivision(panel, validation, [150, 150], [100, 100], minimum=10)


def test_subdivision_enforces_stability_and_independent_validation_gain(monkeypatch):
    from epsm_workflows.cohort_transfer import grouping

    training, validation = rows(60), rows(10)
    for item in validation:
        item["property_key"] += "-validation"
    fake_model = {"k": 2, "property_counts": [30, 30]}
    monkeypatch.setattr(grouping, "fit_group", lambda *args, **kwargs: fake_model)
    monkeypatch.setattr(
        grouping, "stability", lambda *args, **kwargs: {"median_ari": 0.69}
    )
    unstable = physical_subdivision(training, validation, [150] * 10, [100] * 10)
    assert not unstable["supported"]
    assert "assignment_stability_below_0.7" in unstable["reasons"]
    monkeypatch.setattr(
        grouping, "stability", lambda *args, **kwargs: {"median_ari": 0.7}
    )
    valid = physical_subdivision(training, validation, [150] * 10, [100] * 10)
    assert valid["supported"]
