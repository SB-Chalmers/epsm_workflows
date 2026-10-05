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
        assert all(ax.get_ylim()[1] > 10 for ax in fig.axes)
    finally:
        S.plt.close(fig)
