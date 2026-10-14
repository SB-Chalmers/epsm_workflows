"""Protect revised manuscript counts and publication provenance boundaries."""

import json

import pandas as pd
import pytest

from examples import plot_weather_campaign_summary as S
from examples import report_weather_campaign_paper as P
from examples import summarize_weather_campaign_uncertainty as U


@pytest.fixture
def paper_analysis(tmp_path):
    """Bind explicit synthetic analysis tables; do not mock publication checks."""
    records = []
    for scope, _ in S.SCOPES:
        for sampling, _, _ in S.SAMPLINGS:
            for hours in S.HOURS:
                record = {
                    "site_id": "fixture",
                    "year": 2023,
                    "scope": scope,
                    "sampling": sampling,
                    "gap_hours": hours,
                    "physical_signature": f"{scope}-{sampling}-{hours}",
                    "building_id": "profile",
                    "method": "automatic",
                    "engine_outcome_qualified": True,
                    **{metric: hours / 100 for metric in S.METRICS},
                }
                for mode in ("heating", "cooling"):
                    record[f"baseline_annual_{mode}_kwh_m2"] = 100.0
                    record[f"baseline_peak_{mode}_w_m2"] = 10.0
                    record[f"baseline_{mode}_peak_hidden"] = True
                    record[f"same_{mode}_peak_hours"] = mode == "heating"
                    record[f"nearest_{mode}_peak_shift_h"] = (
                        0 if mode == "heating" else 48
                    )
                records.append(record)
    rows = pd.DataFrame(records)
    plots = tmp_path / "summary-figures"
    plots.mkdir()
    rows.to_csv(tmp_path / "deduplicated-comparisons.csv", index=False)
    tables = {
        "load-error-summary.csv": S.summarize(rows),
        "peak-timing-summary.csv": S.timing_summary(rows),
        "relative-annual-error-summary.csv": U.relative_annual_summary(rows),
        "exposed-peak-timing-uncertainty.csv": U.exposed_timing_summary(rows),
        "event-temperature-uncertainty.csv": pd.DataFrame(
            [
                {
                    "scope": "meteorology",
                    "sampling": "weather_extremes",
                    "event_type": "heatwave",
                    "gap_hours": 12,
                    "exposed_reference_events": 2,
                    "matched_exposed_events": 2,
                    "lost_events": 0,
                    "split_events": 0,
                    "unchanged_date_events": 2,
                    "median_abs_peak_temperature_error_c": 0.5,
                    "empirical_p95_abs_peak_temperature_error_c": 1.0,
                    "maximum_abs_peak_temperature_error_c": 1.2,
                },
                {
                    "scope": "meteorology",
                    "sampling": "weather_extremes",
                    "event_type": "cold_spell",
                    "gap_hours": 168,
                    "exposed_reference_events": 2,
                    "matched_exposed_events": 1,
                    "lost_events": 1,
                    "split_events": 0,
                    "unchanged_date_events": 0,
                    "median_abs_peak_temperature_error_c": 2.0,
                    "empirical_p95_abs_peak_temperature_error_c": 2.0,
                    "maximum_abs_peak_temperature_error_c": 2.0,
                },
            ]
        ),
        "weather-event-summary.csv": pd.DataFrame(
            [
                {
                    "event_type": "heatwave",
                    "gap_hours": 12,
                    "reference_exposed_events": 2,
                    "exposed_events_with_exact_bounds": 2,
                    "exposed_events_with_exact_bounds_and_peak": 0,
                }
            ]
        ),
    }
    for name, frame in tables.items():
        frame.to_csv(plots / name, index=False)
    for name in (
        "automatic-annual-errors",
        "automatic-annual-errors-absolute",
        "automatic-peak-errors",
        "automatic-peak-timing",
        "automatic-weather-events",
    ):
        for extension in ("png", "svg", "pdf"):
            (plots / f"{name}.{extension}").write_bytes(
                b"opaque figure-boundary fixture"
            )
    (tmp_path / "diagnostics-qualification.json").write_text(
        json.dumps(
            {
                "policy": "reviewed-psychrometric-sensible-study-1",
                "raw_building_rows": 17,
                "unique_simulations": 17,
                "records": [{"reviewed_psychrometric_warning_retained": True}],
            }
        )
    )
    manifest = {
        "status": "complete",
        "outputs": {
            p.name: S.sha(p.read_bytes()) for p in tmp_path.iterdir() if p.is_file()
        },
    }
    raw = json.dumps(manifest).encode()
    (tmp_path / "manifest.json").write_bytes(raw)
    (plots / "manifest.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "method": "automatic",
                "analysis_manifest_sha256": S.sha(raw),
                "building_responses": 16,
                "physical_weather_masks": 16,
                "site_years": 1,
                "outputs": {
                    p.name: S.sha(p.read_bytes())
                    for p in plots.iterdir()
                    if p.is_file()
                },
            }
        )
    )
    return tmp_path


def test_revised_paper_counts_and_values_come_from_current_only_bound_tables(
    paper_analysis,
):
    receipt = P.publish(paper_analysis)
    page = (paper_analysis / "paper.html").read_text()
    assert (
        "16 paired building comparisons, spanning 16 distinct outage scenarios" in page
    )
    assert "0/1 heating comparisons" in page
    assert "1/1 cooling comparisons" in page
    assert "1.7 kWh/m² (1.7%)" in page
    assert "0.12%" in page
    assert "median of 0.5°C and a 95th percentile of 1.0°C" in page
    assert "cooling references below 1 kWh/m²" in page
    assert "rather than total weather or building-model uncertainty" in page
    assert "Core's original warning exception is scoped to annual space heating" in page
    assert receipt["retained_reviewed_warning_runs"] == 1
    assert all(
        S.sha((paper_analysis / name).read_bytes()) == expected
        for name, expected in receipt["outputs"].items()
    )


@pytest.mark.parametrize(
    "defect",
    [
        "foreign_manifest",
        "changed_figure",
        "changed_diagnostics",
        "changed_counts",
        "changed_percentage_summary",
        "changed_temperature_summary",
    ],
)
def test_revised_paper_refuses_stale_or_changed_evidence(paper_analysis, defect):
    path = paper_analysis / "summary-figures/manifest.json"
    if defect == "foreign_manifest":
        doc = json.loads(path.read_text())
        doc["analysis_manifest_sha256"] = "other-analysis"
        path.write_text(json.dumps(doc))
    elif defect == "changed_counts":
        doc = json.loads(path.read_text())
        doc["building_responses"] = 100
        path.write_text(json.dumps(doc))
    elif defect in {"changed_percentage_summary", "changed_temperature_summary"}:
        name = (
            "relative-annual-error-summary.csv"
            if defect == "changed_percentage_summary"
            else "event-temperature-uncertainty.csv"
        )
        target = paper_analysis / "summary-figures" / name
        target.write_bytes(target.read_bytes() + b" ")
    else:
        target = paper_analysis / (
            "diagnostics-qualification.json"
            if defect == "changed_diagnostics"
            else "summary-figures/automatic-annual-errors.png"
        )
        target.write_bytes(target.read_bytes() + b" ")
    with pytest.raises(ValueError):
        P.publish(paper_analysis)
    assert not (paper_analysis / "paper.html").exists()
