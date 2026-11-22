import math

import pytest
from epsm_workflows.validation import (
    accepted_on_both,
    accuracy,
    classify_building,
    compare_cohorts,
    comparison_terms,
    delivered_heating_estimate,
    district_heat_estimate,
    district_heat_observation,
    measurement_period,
    score_cohorts,
    summarise_cohorts,
)


def row():
    return dict(
        FormularId="17",
        EgenAtemp="1000,5",
        EgiBeraknatVarde="Nej",
        EgiForstaArManad="1701",
        EgiSistaArManad="1712",
        ExpertGodkannande="2018-02-01 12:00:00",
        EgiFjarrvarme="100000",
        EgiVVBered="25000",
        EgiGruppFjarrvarme="Fördelat",
    )


def test_export_schemas_have_different_dhw_accounting():
    old = district_heat_observation(row(), schema="fore_v260")
    newer = row() | dict(EgiFjarrvarmeUPPV="75000", EgiFjarrvarmeVV="25000")
    new = district_heat_observation(newer, schema="efter_v260")
    assert (
        old["reported_district_heat_kwh"] == new["reported_district_heat_kwh"] == 100000
    )
    assert old["atemp_m2"] == 1000.5
    assert old["measurement_kind"] == "Fördelat"
    assert new["reporting_regime"] == "specific_energy_pre_2019"


@pytest.mark.parametrize(
    "patch",
    [
        {"EgiFjarrvarme": ""},
        {"EgiFjarrvarme": "NaN"},
        {"EgenAtemp": "0"},
        {"EgiVVBered": "100001"},
        {"EgiPumpMark": "1"},
        {"EgiBeraknatVarde": "Ja"},
        {"EgiGruppFjarrvarme": ""},
    ],
)
def test_incomparable_targets_fail(patch):
    with pytest.raises(ValueError):
        district_heat_observation(row() | patch, schema="fore_v260")


def test_missing_new_dhw_is_not_zero():
    with pytest.raises(ValueError, match="Missing EgiFjarrvarmeVV"):
        district_heat_observation(
            row() | {"EgiFjarrvarmeUPPV": "100000"}, schema="efter_v260"
        )


def test_period_preserves_leap_and_cross_year_dates():
    a, b = measurement_period("1903", "2002")
    assert (str(a), str(b)) == ("2019-03-01", "2020-02-29")
    with pytest.raises(ValueError):
        measurement_period("2001", "2011")


def test_delivery_accounts_for_loss_and_efficiency_once():
    estimate = district_heat_estimate(
        8000,
        2000,
        100,
        space_efficiency=0.8,
        dhw_efficiency=1,
        airing_kwh_m2=4,
        net_distribution_loss_kwh_m2=5,
    )
    assert estimate["delivered_district_heat_kwh"] == 13000


def test_metrics_are_paired_and_bias_does_not_hide_scatter():
    metrics = accuracy([100, 200], [150, 150])
    assert metrics["bias_percent"] == 0
    assert metrics["rmse"] == 50
    assert metrics["mape_percent"] == 37.5
    assert metrics["r2"] == 0
    assert metrics["within_20_percent_count"] == 0


@pytest.mark.parametrize(
    "observed,predicted",
    [
        ([], []),
        ([1], []),
        ([0], [1]),
        ([math.nan], [1]),
        ([1], [math.inf]),
        ([1], [-1]),
    ],
)
def test_metrics_reject_invalid_pairs(observed, predicted):
    with pytest.raises(ValueError):
        accuracy(observed, predicted)


def test_terms_are_added_before_the_seasonal_cop_and_scaled_to_the_period():
    estimate = delivered_heating_estimate(
        5500, 100, carrier="heat_pump", seasonal_cop=2.75, period_days=730,
        terms_kwh_m2_year={"window_airing": 4},
    )
    assert estimate["term_heat_kwh"] == {"window_airing": 800}
    assert estimate["delivered_heating_kwh"] == pytest.approx(6300 / 2.75)


@pytest.mark.parametrize(
    "options,delivered",
    [
        ({"carrier": "district_heat"}, 9000),
        ({"carrier": "electric_direct"}, 9000),
        ({"carrier": "fuel", "efficiency": 0.9}, 10000),
        ({"carrier": "heat_pump", "seasonal_cop": 3}, 3000),
    ],
)
def test_each_carrier_has_its_own_divisor(options, delivered):
    estimate = delivered_heating_estimate(9000, 100, **options)
    assert estimate["delivered_heating_kwh"] == pytest.approx(delivered)


@pytest.mark.parametrize(
    "options",
    [
        {"carrier": "heat_pump"},
        {"carrier": "heat_pump", "seasonal_cop": 0},
        {"carrier": "heat_pump", "seasonal_cop": 3, "efficiency": 0.9},
        {"carrier": "fuel"},
        {"carrier": "fuel", "efficiency": 1.1},
        {"carrier": "district_heat", "efficiency": 0.9},
        {"carrier": "coal"},
        {"carrier": "district_heat", "period_days": 0},
        {"carrier": "district_heat", "terms_kwh_m2_year": {"a": -1}},
    ],
)
def test_delivery_rejects_inputs_that_do_not_fit_the_carrier(options):
    with pytest.raises(ValueError):
        delivered_heating_estimate(9000, 100, **options)


def test_comparison_terms_apply_only_what_the_study_chose():
    assert comparison_terms("multi_family", municipality_code="1485") == {}
    terms = comparison_terms(
        "multi_family", municipality_code="1485",
        apply=("window_airing", "hot_water_circulation"),
    )
    assert terms["window_airing"] == pytest.approx(3.6)
    assert terms["hot_water_circulation"] == pytest.approx(25 / 3)
    with pytest.raises(ValueError, match="no sourced value"):
        comparison_terms("office", apply=("hot_water_circulation",))
    with pytest.raises(ValueError, match="Unknown"):
        comparison_terms("office", apply=("cooling",))


def states(existing, standard, advanced):
    return {"existing": existing, "standard": standard, "advanced": advanced}


@pytest.mark.parametrize(
    "ratios,position,covered,selected",
    [
        (states(1.5, 1.1, 0.7), "inside", True, "standard"),
        (states(0.7, 0.6, 0.5), "above_range", False, None),
        (states(2.0, 1.9, 1.15), "below_range", False, "advanced"),
        ({"single": 1.1}, "below_range", None, "single"),
        ({"single": 0.5}, "above_range", None, None),
    ],
)
def test_classification_names_where_the_measurement_lies(ratios, position, covered, selected):
    result = classify_building(ratios)
    assert (result["position"], result["covered"], result["selected_state"]) == (
        position, covered, selected,
    )


@pytest.mark.parametrize(
    "ratios", [{"existing": 1}, states(1, 1, 0), states(1, 1, math.nan), {"single": 1, "existing": 1}]
)
def test_classification_rejects_incomplete_or_invalid_ratios(ratios):
    with pytest.raises(ValueError):
        classify_building(ratios)


def cohort(rows, *, category="multi_family", era="SE.03"):
    return [{"category": category, "era": era, "ratios": r} for r in rows]


def test_cohort_shortfall_sums_the_three_missed_targets():
    inside = states(1.5, 1.0, 0.7)
    unmatched = states(1.5, 1.3, 0.7)
    above = states(0.5, 0.4, 0.3)
    below = states(3, 2, 1.5)
    rows = [inside] * 4 + [unmatched] * 2 + [above] * 2 + [below] * 2
    result = score_cohorts(cohort(rows))["multi_family", "SE.03"]
    assert (result["above_existing"], result["below_advanced"]) == (0.2, 0.2)
    assert (result["match"], result["coverage"]) == (0.4, 0.6)
    assert result["shortfall"] == pytest.approx(0.05 + 0.05 + 0.1)
    assert result["selected_states"] == {"standard": 4, None: 6}
    assert result["enough_buildings"] and not result["meets_targets"]


def test_a_single_state_era_has_a_top_target_and_no_bottom_one():
    rows = [{"single": 1.0}] * 5 + [{"single": 0.5}] * 5
    result = score_cohorts(cohort(rows, era="SE.07"))["multi_family", "SE.07"]
    assert result["above_existing"] == 0.5 and result["below_advanced"] is None
    assert result["coverage"] is None
    assert result["shortfall"] == pytest.approx(0.35)
    rows = [{"single": 1.0}] * 5 + [{"single": 2.0}] * 5
    assert score_cohorts(cohort(rows, era="SE.07"))["multi_family", "SE.07"]["meets_targets"]


def test_small_cohorts_are_flagged_and_mixed_cohorts_refused():
    assert not score_cohorts(cohort([{"single": 1}]))["multi_family", "SE.03"]["enough_buildings"]
    with pytest.raises(ValueError, match="mixes"):
        score_cohorts(cohort([{"single": 1}, states(1, 1, 1)]))


def test_summary_weights_by_building_and_leaves_single_state_out_of_below():
    cohorts = score_cohorts(
        cohort([states(3, 2, 1.5)] * 3) + cohort([{"single": 1.0}], era="SE.07")
    )
    summary = summarise_cohorts(cohorts)
    assert summary["n"] == 4
    assert summary["below_advanced"] == 1
    assert summary["match"] == 0.25


def test_acceptance_needs_every_condition_on_both_samples():
    worse = score_cohorts(cohort([states(1.5, 1.3, 0.7)] * 10))
    better = score_cohorts(cohort([states(1.5, 1.0, 0.7)] * 10))
    passed = compare_cohorts(worse, better)
    assert passed["multi_family"]["accepted"]
    failed = compare_cohorts(better, worse)
    assert failed["multi_family"]["materially_worse"] == ["SE.03"]
    assert not failed["multi_family"]["accepted"]
    assert accepted_on_both(passed, passed) == ["multi_family"]
    assert accepted_on_both(passed, failed) == []
    with pytest.raises(ValueError, match="same cohorts"):
        compare_cohorts(worse, score_cohorts(cohort([states(1, 1, 1)], era="SE.04")))
