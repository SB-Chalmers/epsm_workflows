"""Protect meaningful uncertainty denominators and authenticated event evidence."""

import json

import numpy as np
import pandas as pd
import pytest

from examples import summarize_weather_campaign_uncertainty as U


def responses():
    """Several buildings share weather evidence; they are not new weather events."""
    rows = []
    for building in ("a", "b"):
        row = {
            "site_id": "fixture",
            "year": 2023,
            "scope": "meteorology",
            "sampling": "weather_extremes",
            "gap_hours": 12,
            "physical_signature": "physical-mask",
            "evidence_id": "outage",
            "case_id": "outage-automatic",
            "building_id": building,
            "method": "automatic",
            "engine_outcome_qualified": True,
        }
        for mode in ("heating", "cooling"):
            row.update(
                {
                    f"baseline_annual_{mode}_kwh_m2": 100.0
                    if mode == "heating"
                    else 10.0,
                    f"absolute_annual_{mode}_error_kwh_m2": 1.0
                    if mode == "heating"
                    else 0.2,
                    f"baseline_peak_{mode}_w_m2": 10.0,
                    f"baseline_{mode}_peak_hidden": True,
                    f"same_{mode}_peak_hours": False,
                    f"nearest_{mode}_peak_shift_h": -25,
                }
            )
        rows.append(row)
    frame = pd.DataFrame(rows)
    for mode in ("heating", "cooling"):
        frame[f"absolute_annual_{mode}_error_kwh_m2"] = frame[
            f"absolute_annual_{mode}_error_kwh_m2"
        ].astype(float)
    return frame


@pytest.fixture
def analysis(tmp_path):
    """Create actual hash-bound archives so source tampering is observable."""
    root = tmp_path / "campaign"
    folder = root / "datasets" / "fixture" / "2023"
    case = folder / "cases" / "outage-automatic"
    case.mkdir(parents=True)
    out = root / "analysis"
    out.mkdir()

    def event(kind, day):
        return {
            "type": kind,
            "start_lst": f"2023-01-{day:02}T00:00:00+01:00",
            "end_lst": f"2023-01-{day + 3:02}T00:00:00+01:00",
        }

    def match(index, change, *, duration=0):
        return {
            "reference_index": index,
            "peak_value_change": change,
            "duration_change_hours": duration,
            "start_shift_hours": 0,
            "end_shift_hours": duration,
        }

    plan = {
        "all_panels": [
            {
                "evidence_id": "outage",
                "masked_utc": [f"2023-01-{day:02}T01:00:00Z" for day in (1, 5, 9, 17)],
            }
        ]
    }
    comparison = {
        "reference": {
            "events": [event("heatwave", day) for day in (1, 5, 9, 13)]
            + [event("cold_spell", 17)]
        },
        "by_type": {
            "heatwave": {
                # The second exposed heatwave is lost; the third splits.
                "matches": [match(0, -0.8), match(2, 5), match(2, 6), match(3, 3)]
            },
            "cold_spell": {"matches": [match(0, -2.9, duration=24)]},
        },
    }
    (folder / "panel-plan.json").write_text(json.dumps(plan))
    (case / "events.json").write_text(json.dumps(comparison))
    responses().to_csv(out / "deduplicated-comparisons.csv", index=False)
    manifest = {
        "status": "complete",
        "inputs": {
            str(path.resolve()): U.sha(path.read_bytes())
            for path in (folder / "panel-plan.json", case / "events.json")
        },
        "outputs": {
            "deduplicated-comparisons.csv": U.sha(
                (out / "deduplicated-comparisons.csv").read_bytes()
            )
        },
    }
    (out / "manifest.json").write_text(json.dumps(manifest))
    return out


def test_relative_errors_exclude_tiny_cooling_without_inventing_certainty():
    rows = responses()
    rows.loc[0, "baseline_annual_cooling_kwh_m2"] = 0.001
    summary = U.relative_annual_summary(rows).set_index("mode")
    assert summary.loc["cooling", "eligible_responses"] == 1
    assert summary.loc["cooling", "excluded_small_reference_demands"] == 1
    assert summary.loc["cooling", "empirical_p95_absolute_error_pct"] == 2
    assert summary.loc["heating", "eligible_responses"] == 2
    assert summary.loc["heating", "empirical_p95_absolute_error_pct"] == 1


def test_zero_annual_baselines_are_reported_as_excluded_not_zero_error():
    rows = responses()
    rows["baseline_annual_heating_kwh_m2"] = 0
    summary = U.relative_annual_summary(rows).set_index("mode")
    assert summary.loc["heating", "eligible_responses"] == 0
    assert summary.loc["heating", "excluded_small_reference_demands"] == 2
    assert np.isnan(summary.loc["heating", "empirical_p95_absolute_error_pct"])


def test_timing_uses_absolute_distances_and_keeps_zero_and_unexposed_peaks_out():
    rows = pd.concat([responses()] * 3, ignore_index=True)
    for mode in ("heating", "cooling"):
        rows[f"nearest_{mode}_peak_shift_h"] = [0, -25, 24, 25, 999, 999]
        rows.loc[4, f"baseline_peak_{mode}_w_m2"] = 1e-7
        rows.loc[5, f"baseline_{mode}_peak_hidden"] = False
    summary = U.exposed_timing_summary(rows).set_index("mode")
    assert summary.loc["heating", "exposed_positive_peaks"] == 4
    assert summary.loc["heating", "same_hour_peaks"] == 1
    assert summary.loc["heating", "changed_within24h_peaks"] == 1
    assert summary.loc["heating", "beyond24h_peaks"] == 2
    assert summary.loc["heating", "beyond24h_fraction"] == 0.5


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1])
def test_invalid_annual_values_do_not_enter_percentiles(bad):
    rows = responses()
    rows.loc[0, "absolute_annual_heating_error_kwh_m2"] = bad
    with pytest.raises(ValueError, match="finite with valid signs"):
        U.relative_annual_summary(rows)


def test_events_count_weather_once_and_do_not_assign_zero_error_to_lost_or_split_events(
    analysis,
):
    tables = U.build_uncertainty_tables(analysis)
    events = tables["event-temperature-uncertainty.csv"].set_index("event_type")
    heat = events.loc["heatwave"]
    assert heat.exposed_reference_events == 3
    assert heat.matched_exposed_events == 1
    assert heat.lost_events == 1
    assert heat.split_events == 1
    assert heat.median_abs_peak_temperature_error_c == 0.8
    assert heat.unchanged_date_events == 1
    assert events.loc["cold_spell", "median_abs_peak_temperature_error_c"] == 2.9
    assert events.loc["cold_spell", "unchanged_date_events"] == 0
    detail = tables["event-temperature-comparisons.csv"]
    assert (
        detail.loc[~detail.single_match, "absolute_peak_temperature_error_c"]
        .isna()
        .all()
    )


def test_changed_archived_event_evidence_is_rejected(analysis):
    path = analysis.parent / "datasets/fixture/2023/cases/outage-automatic/events.json"
    path.write_text(path.read_text() + " ")
    with pytest.raises(ValueError, match="Archived weather evidence differs"):
        U.build_uncertainty_tables(analysis)


def test_unrecorded_archived_event_evidence_is_rejected(analysis):
    path = analysis / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["inputs"] = {}
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Archived weather evidence differs"):
        U.build_uncertainty_tables(analysis)


def test_changed_comparison_table_is_rejected(analysis):
    path = analysis / "deduplicated-comparisons.csv"
    path.write_text(path.read_text() + " ")
    with pytest.raises(ValueError, match="comparison table differs"):
        U.build_uncertainty_tables(analysis)


def test_forced_fallback_rows_cannot_dilute_automatic_uncertainty(analysis):
    path = analysis / "deduplicated-comparisons.csv"
    rows = pd.read_csv(path)
    forced = rows.iloc[[0]].copy()
    forced["method"] = "era5"
    forced["absolute_annual_heating_error_kwh_m2"] = 500
    pd.concat([rows, forced], ignore_index=True).to_csv(path, index=False)
    receipt = analysis / "manifest.json"
    manifest = json.loads(receipt.read_text())
    manifest["outputs"][path.name] = U.sha(path.read_bytes())
    receipt.write_text(json.dumps(manifest))
    table = U.build_uncertainty_tables(analysis)["relative-annual-error-summary.csv"]
    heating = table.loc[table["mode"].eq("heating")].iloc[0]
    assert heating.eligible_responses == 2
    assert heating.empirical_p95_absolute_error_pct == 1


def test_unqualified_automatic_runs_cannot_establish_uncertainty(analysis):
    path = analysis / "deduplicated-comparisons.csv"
    rows = pd.read_csv(path)
    rows.loc[0, "engine_outcome_qualified"] = False
    rows.to_csv(path, index=False)
    receipt = analysis / "manifest.json"
    manifest = json.loads(receipt.read_text())
    manifest["outputs"][path.name] = U.sha(path.read_bytes())
    receipt.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Unqualified outcomes"):
        U.build_uncertainty_tables(analysis)
