"""Protect current-only descriptive cohorts and exposed-peak denominators."""

import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from examples import plot_weather_campaign_summary as S


@pytest.fixture
def outcomes():
    base = {
        "site_id": "fixture",
        "year": 2023,
        "scope": "meteorology",
        "sampling": "weather_extremes",
        "gap_hours": 48,
        "physical_signature": "shared",
        "method": "automatic",
        "engine_outcome_qualified": True,
        **{metric: 1.0 for metric in S.METRICS},
    }
    for mode in ("heating", "cooling"):
        base[f"baseline_peak_{mode}_w_m2"] = 10.0
        base[f"baseline_{mode}_peak_hidden"] = True
        base[f"same_{mode}_peak_hours"] = False
    return pd.DataFrame([base | {"building_id": building} for building in ("a", "b")])


def test_only_automatic_responses_enter_paper_summary(outcomes):
    legacy = outcomes.copy()
    legacy["method"] = "automatic_legacy"
    legacy[S.METRICS] = 1000
    selected = S.automatic_rows(pd.concat([outcomes, legacy]))
    summary = S.summarize(selected)
    assert len(selected) == 2
    assert summary["median"].eq(1.0).all()
    assert summary.building_responses.eq(2).all()
    assert summary.physical_masks.eq(1).all()
    assert summary.site_years.eq(1).all()


@pytest.mark.parametrize(
    "problem",
    [
        "unqualified",
        "duplicate",
        "negative",
        "nonfinite",
        "unknown_scope",
        "unknown_hours",
        "unknown_exposure",
    ],
)
def test_invalid_or_unqualified_outcomes_are_rejected(outcomes, problem):
    if problem == "unqualified":
        outcomes.loc[0, "engine_outcome_qualified"] = False
    elif problem == "duplicate":
        outcomes = pd.concat([outcomes, outcomes.iloc[[0]]])
    elif problem in ("negative", "nonfinite"):
        outcomes.loc[0, S.METRICS[0]] = -1 if problem == "negative" else np.nan
    elif problem == "unknown_scope":
        outcomes.loc[0, "scope"] = "other"
    elif problem == "unknown_hours":
        outcomes.loc[0, "gap_hours"] = 5
    else:
        outcomes["baseline_heating_peak_hidden"] = [True, None]
    with pytest.raises(ValueError):
        S.automatic_rows(outcomes)


def test_peak_timing_excludes_zero_and_unexposed_reference_peaks(outcomes):
    zero, unexposed = outcomes.iloc[[0]].copy(), outcomes.iloc[[0]].copy()
    zero["building_id"], unexposed["building_id"] = "zero", "unexposed"
    zero["baseline_peak_heating_w_m2"] = 0
    zero["same_heating_peak_hours"] = True
    unexposed["baseline_heating_peak_hidden"] = False
    unexposed["same_heating_peak_hours"] = True
    outcomes.loc[0, "same_heating_peak_hours"] = True
    row = (
        S.timing_summary(pd.concat([outcomes, zero, unexposed]))
        .set_index("mode")
        .loc["heating"]
    )
    assert row.exposed_positive_peaks == 2
    assert row.preserved_peak_hour_sets == 1
    assert row.preserved_fraction == 0.5
    assert row.unexposed_positive_peaks == 1
    assert row.zero_or_numerical_peaks == 1


def test_no_exposed_peak_is_missing_instead_of_zero_preservation(outcomes):
    outcomes["baseline_heating_peak_hidden"] = False
    row = S.timing_summary(outcomes).set_index("mode").loc["heating"]
    assert row.exposed_positive_peaks == 0
    assert np.isnan(row.preserved_fraction)


def test_weather_events_are_not_multiplied_by_building_responses():
    row = {
        "site_id": "fixture",
        "year": 2023,
        "scope": "meteorology",
        "sampling": "weather_extremes",
        "gap_hours": 48,
        "physical_signature": "shared",
        "method": "automatic",
        "event_type": "heatwave",
        "reference_exposed_events": 2,
        "exposed_events_with_any_overlap": 2,
        "exposed_events_with_exact_bounds": 1,
        "exposed_events_with_exact_bounds_and_peak": 0,
    }
    other = row | {"method": "automatic_legacy", "reference_exposed_events": 100}
    result = S.event_summary(pd.DataFrame([row, other]))
    assert result.iloc[0].reference_exposed_events == 2
    assert result.iloc[0].physical_masks == 1
    with pytest.raises(ValueError, match="unique"):
        S.event_summary(pd.DataFrame([row, row]))
    with pytest.raises(ValueError, match="Stricter"):
        S.event_summary(pd.DataFrame([row | {"exposed_events_with_exact_bounds": 3}]))


def test_summary_refuses_partial_analysis_before_writing(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"status": "incomplete"}))
    with pytest.raises(ValueError, match="complete"):
        S.plot(tmp_path)
    assert not (tmp_path / "summary-figures").exists()


def test_summary_refuses_changed_csv_bytes(tmp_path, outcomes):
    path = tmp_path / "deduplicated-comparisons.csv"
    outcomes.to_csv(path, index=False)
    manifest = {"outputs": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()}}
    with path.open("ab") as stream:
        stream.write(b"\n")
    with pytest.raises(ValueError, match="differs"):
        S.read_table(tmp_path, manifest, path.name)


@pytest.mark.parametrize("quantity", ["annual", "peak"])
def test_shared_axes_show_the_larger_solar_error_tail(quantity):
    """A common scale cannot silently crop the second weather group's values."""
    records = []
    for scope, _ in S.SCOPES:
        for sampling, _, _ in S.SAMPLINGS:
            for hours in S.HOURS:
                for metric in S.METRICS:
                    records.append(
                        {
                            "scope": scope,
                            "sampling": sampling,
                            "gap_hours": hours,
                            "metric": metric,
                            "building_responses": 6,
                            "median": 1.0,
                            "empirical_p95": 10.0 if scope == "solar" else 2.0,
                        }
                    )
    fig = S.error_figure(pd.DataFrame(records), quantity)
    try:
        assert all(ax.get_xlim()[1] > 10 for ax in fig.axes)
    finally:
        S.plt.close(fig)


def test_exposed_peak_magnitudes_exclude_zero_and_unexposed_peaks(outcomes):
    """Preserving a peak outside the missing weather is not evidence of recovery."""
    zero, unexposed = outcomes.iloc[[0]].copy(), outcomes.iloc[[0]].copy()
    zero["building_id"], unexposed["building_id"] = "zero", "unexposed"
    zero["baseline_peak_heating_w_m2"] = 0
    zero["absolute_peak_heating_error_w_m2"] = 0
    unexposed["baseline_heating_peak_hidden"] = False
    unexposed["absolute_peak_heating_error_w_m2"] = 0
    result = S.exposed_peak_summary(pd.concat([outcomes, zero, unexposed]))
    heating = result.set_index("metric").loc["absolute_peak_heating_error_w_m2"]
    assert heating.exposed_positive_peaks == 2
    assert heating.building_responses == 2
    assert heating.physical_masks == 1
    assert heating["median"] == 1.0
    assert heating.empirical_p95 == 1.0


def test_exposed_peak_summary_omits_empty_exposure_cohort(outcomes):
    outcomes["baseline_heating_peak_hidden"] = False
    result = S.exposed_peak_summary(outcomes)
    assert not result.metric.eq("absolute_peak_heating_error_w_m2").any()
    assert result.metric.eq("absolute_peak_cooling_error_w_m2").all()


def test_annual_chart_does_not_mix_outage_budgets():
    """One seasonal file loses four blocks; one event file loses one block."""
    records = []
    for scope, _ in S.SCOPES:
        for sampling, _, _ in S.SAMPLINGS:
            for hours in S.HOURS:
                for metric in S.METRICS:
                    records.append(
                        {
                            "scope": scope,
                            "sampling": sampling,
                            "gap_hours": hours,
                            "metric": metric,
                            "median": 1.0,
                            "empirical_p95": 2.0
                            if sampling == "random_seasonal"
                            else 200.0,
                        }
                    )
    fig = S.error_figure(pd.DataFrame(records), "annual")
    try:
        assert all(2 < ax.get_xlim()[1] < 200 for ax in fig.axes)
    finally:
        S.plt.close(fig)


def test_peak_timing_chart_uses_day_scale_changes_instead_of_exact_equality():
    """A changed tied-hour set need not move the annual winning peak by a day."""
    records = []
    for scope, _ in S.SCOPES:
        for hours in S.HOURS:
            for mode in ("heating", "cooling"):
                records.append(
                    {
                        "scope": scope,
                        "sampling": "weather_extremes",
                        "gap_hours": hours,
                        "mode": mode,
                        "exposed_positive_peaks": 10,
                        "preserved_fraction": 0.0,
                        "beyond24h_fraction": 0.2,
                    }
                )
    fig = S.timing_figure(pd.DataFrame(records))
    try:
        points = [
            line for ax in fig.axes for line in ax.lines if line.get_marker() == "o"
        ]
        assert len(points) == 16
        assert all(float(line.get_xdata()[0]) == 20 for line in points)
    finally:
        S.plt.close(fig)


def test_event_chart_does_not_invent_temperature_error_for_lost_episodes():
    summary = pd.DataFrame(
        [
            {
                "event_type": event,
                "gap_hours": hours,
                "reference_exposed_events": 2,
                "exposed_events_with_exact_bounds": 0,
            }
            for event, _ in S.EVENTS[:2]
            for hours in S.HOURS
        ]
    )
    temperature = pd.DataFrame(
        [
            {
                "event_type": event,
                "sampling": "weather_extremes",
                "gap_hours": hours,
                "matched_exposed_events": 0,
                "median_abs_peak_temperature_error_c": np.nan,
                "empirical_p95_abs_peak_temperature_error_c": np.nan,
            }
            for event, _ in S.EVENTS[:2]
            for hours in S.HOURS
        ]
    )
    fig = S.event_figure(summary, temperature)
    try:
        points = [line for line in fig.axes[1].lines if line.get_marker() == "o"]
        assert not points
        assert (
            sum(text.get_text() == "No matched episodes" for text in fig.axes[1].texts)
            == 8
        )
    finally:
        S.plt.close(fig)


def test_relative_annual_chart_uses_percentages_without_hiding_large_cooling_tail():
    records = []
    for scope, _ in S.SCOPES:
        for hours in S.HOURS:
            for mode in ("heating", "cooling"):
                records.append(
                    {
                        "scope": scope,
                        "sampling": "random_seasonal",
                        "gap_hours": hours,
                        "mode": mode,
                        "median_absolute_error_pct": 0.2,
                        "empirical_p95_absolute_error_pct": 12.7
                        if mode == "cooling"
                        else 3.8,
                    }
                )
    fig = S.error_figure(pd.DataFrame(records), "annual", relative=True)
    try:
        assert all(ax.get_xlim()[1] > 12.7 for ax in fig.axes)
        assert any(text.get_text() == "12.7%" for text in fig.axes[1].texts)
        assert any("Cooling below 1 kWh/m²" in text.get_text() for text in fig.texts)
    finally:
        S.plt.close(fig)


def test_peak_chart_handles_no_exposed_peaks_in_either_mode(outcomes):
    """A valid outage away from both peaks must show missing evidence, not fail."""
    for mode in ("heating", "cooling"):
        outcomes[f"baseline_{mode}_peak_hidden"] = False
    summary = S.exposed_peak_summary(outcomes)
    assert summary.empty
    fig = S.error_figure(summary, "peak")
    try:
        assert any(
            "No exposed peaks" in text.get_text()
            for ax in fig.axes
            for text in ax.texts
        )
    finally:
        S.plt.close(fig)
