"""Protect shared method cohorts and scientific counts without simulation runs."""

import hashlib
import json
from copy import deepcopy
from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest

from examples import plot_weather_campaign_methods as M

METRICS = (
    "absolute_annual_heating_error_kwh_m2",
    "absolute_annual_cooling_error_kwh_m2",
    "absolute_peak_heating_error_w_m2",
    "absolute_peak_cooling_error_w_m2",
)
SUMMARY_COLUMNS = {
    "scope",
    "sampling",
    "gap_hours",
    "method",
    "metric",
    "unit",
    "physical_panels",
    "site_years",
    "building_responses",
    "median",
    "empirical_p95",
}


@pytest.fixture
def matrix():
    """Schedule distinct MET and solar methods for two illustrative buildings."""
    return {
        "meteorology_methods": [
            "temporal",
            "donor_assessed",
            "era5",
            "automatic",
            "automatic_legacy",
        ],
        "solar_methods": ["temporal", "era5", "automatic"],
        "buildings": {
            "small": {"label": "Small profile", "floor_area_m2": 100.0},
            "large": {"label": "Large profile", "floor_area_m2": 200.0},
        },
    }


@pytest.fixture
def cohort(matrix):
    """Provide two identical-method panels with known zero and positive errors."""
    rows = []
    for panel in range(2):
        for building_position, building in enumerate(matrix["buildings"]):
            value = 4.0 * panel + 2.0 * building_position
            for method_position, method in enumerate(matrix["meteorology_methods"]):
                row = {
                    "site_id": "fixture",
                    "year": 2023,
                    "building_id": building,
                    "scope": "meteorology",
                    "sampling": "random_seasonal",
                    "gap_hours": 12,
                    "physical_signature": f"physical-panel-{panel}",
                    "method": method,
                    "floor_area_m2": matrix["buildings"][building]["floor_area_m2"],
                }
                row.update(
                    {metric: value * (method_position + 1) for metric in METRICS}
                )
                rows.append(row)
    return pd.DataFrame(rows)


def test_complete_cohort_summary_counts_physical_panels_without_method_multiplier(
    cohort, matrix
):
    """Known four-response distributions retain zeros and only two weather panels."""
    original = cohort.copy(deep=True)
    validated = M.validate_cohort(cohort, matrix)
    pd.testing.assert_frame_equal(cohort, original)
    assert len(validated) == 2 * 2 * 5
    result = M.summarize(validated, matrix)
    assert SUMMARY_COLUMNS.issubset(result.columns)
    assert len(result) == 5 * 4
    assert result.physical_panels.eq(2).all()
    assert result.building_responses.eq(4).all()
    assert result.site_years.eq(1).all()
    selected = result.loc[result.method.eq("temporal")].set_index("metric")
    for metric in METRICS:
        assert selected.loc[metric, "median"] == pytest.approx(3.0)
        assert selected.loc[metric, "empirical_p95"] == pytest.approx(5.7)
        expected_unit = "kWh/m²" if "annual" in metric else "W/m²"
        assert selected.loc[metric, "unit"] == expected_unit


def test_solar_cohort_uses_its_own_scheduled_methods(cohort, matrix):
    """Solar evidence requires three methods without importing donor or legacy rows."""
    solar = cohort.loc[
        cohort.physical_signature.eq("physical-panel-0")
        & cohort.method.isin(matrix["solar_methods"])
    ].copy()
    solar["scope"] = "solar"
    result = M.summarize(M.validate_cohort(solar, matrix), matrix)
    assert len(result) == 3 * 4
    assert set(result.method) == set(matrix["solar_methods"])
    assert result.physical_panels.eq(1).all()
    assert result.building_responses.eq(2).all()
    assert result.scope.eq("solar").all()


def test_long_gap_cohort_excludes_temporal_without_changing_shared_panels(
    cohort, matrix
):
    """Seven-day gaps share the four methods supported beyond forty-eight hours."""
    longer = cohort.loc[cohort.method.ne("temporal")].copy()
    longer["gap_hours"] = 168
    validated = M.validate_cohort(longer, matrix)
    assert len(validated) == 2 * 2 * 4
    result = M.summarize(validated, matrix)
    assert set(result.method) == set(matrix["meteorology_methods"]) - {"temporal"}
    assert result.physical_panels.eq(2).all()
    assert result.building_responses.eq(4).all()
    temporal = cohort.iloc[[0]].copy()
    temporal["gap_hours"] = 168
    with pytest.raises(ValueError):
        M.validate_cohort(pd.concat([longer, temporal], ignore_index=True), matrix)


@pytest.mark.parametrize(
    "defect", ["duplicate", "missing_method", "extra_method", "missing_building"]
)
def test_incomplete_or_duplicated_method_cohorts_are_rejected(cohort, matrix, defect):
    """Every physical panel must contain exactly the shared scheduled building methods."""
    altered = cohort.copy(deep=True)
    if defect == "duplicate":
        altered = pd.concat([altered, altered.iloc[[0]]], ignore_index=True)
    elif defect == "missing_method":
        altered = altered.iloc[1:]
    elif defect == "extra_method":
        extra = altered.iloc[[0]].copy()
        extra["method"] = "donor_raw"
        altered = pd.concat([altered, extra], ignore_index=True)
    else:
        altered = altered.loc[
            ~(
                altered.physical_signature.eq("physical-panel-0")
                & altered.building_id.eq("large")
            )
        ]
    with pytest.raises(ValueError):
        M.validate_cohort(altered, matrix)


@pytest.mark.parametrize("value", [-0.1, np.inf])
def test_negative_and_nonfinite_physical_errors_are_rejected(cohort, matrix, value):
    """All four area-normalized load metrics require finite nonnegative values."""
    for metric in METRICS:
        altered = cohort.copy(deep=True)
        altered.loc[0, metric] = value
        with pytest.raises(ValueError):
            M.validate_cohort(altered, matrix)


def test_unsupported_physical_metadata_cannot_enter_shared_method_evidence(
    cohort, matrix
):
    """Scope, sampling, duration, building identity and positive area remain explicit."""
    for column, value in (
        ("scope", "combined_unknown"),
        ("sampling", "unplanned_sampling"),
        ("gap_hours", 72),
        ("building_id", "unplanned_building"),
        ("floor_area_m2", 0.0),
    ):
        altered = cohort.copy(deep=True)
        altered.loc[0, column] = value
        with pytest.raises(ValueError):
            M.validate_cohort(altered, matrix)


def test_empty_shared_cohort_keeps_an_honest_schema_without_zero_error_samples(
    cohort, matrix
):
    """Absent shared evidence retains columns without inventing successful method rows."""
    empty = cohort.iloc[:0].copy()
    validated = M.validate_cohort(empty, matrix)
    assert validated.empty
    assert set(empty.columns).issubset(validated.columns)
    summary = M.summarize(validated, matrix)
    assert summary.empty
    assert SUMMARY_COLUMNS.issubset(summary.columns)
    for scope in ("meteorology", "solar"):
        figure = M.render_scope(summary, scope, matrix, False)
        try:
            for axis in figure.axes:
                assert any(
                    text.get_text() == "No common eligible panels"
                    for text in axis.texts
                )
                for curve in axis.lines:
                    assert np.isnan(np.asarray(curve.get_ydata(), dtype=float)).all()
        finally:
            M.plt.close(figure)


def captured_analysis(folder, cohort, matrix):
    """Write isolated captured tables and matrix bytes for publication guard tests."""
    campaign = folder / "campaign"
    analysis = campaign / "analysis"
    analysis.mkdir(parents=True)
    matrix_path = campaign / "matrix.json"
    matrix_path.write_text(json.dumps(matrix, sort_keys=True))
    table_path = analysis / "common-method-comparisons.csv"
    cohort.to_csv(table_path, index=False)
    manifest = {
        "status": "complete",
        "incomplete": [],
        "campaign_root": str(campaign.resolve()),
        "matrix_sha256": hashlib.sha256(matrix_path.read_bytes()).hexdigest(),
        "common_method_comparisons": len(cohort),
        "outputs": {
            table_path.name: hashlib.sha256(table_path.read_bytes()).hexdigest()
        },
    }
    manifest_path = analysis / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    return analysis, matrix_path, table_path, manifest_path, manifest


def test_unfinished_or_unbound_analysis_is_rejected_before_any_figure_write(
    tmp_path, cohort, matrix, monkeypatch
):
    """Provenance and declared counts fail before rendering or creating output folders."""
    monkeypatch.setattr(
        M,
        "render_scope",
        Mock(side_effect=AssertionError("Invalid evidence must fail before rendering")),
    )
    for defect in ("provisional", "table_hash", "declared_count", "matrix_hash"):
        root, matrix_path, table_path, manifest_path, manifest = captured_analysis(
            tmp_path / defect, cohort, deepcopy(matrix)
        )
        if defect == "provisional":
            manifest["status"] = "provisional"
        elif defect == "table_hash":
            with table_path.open("ab") as stream:
                stream.write(b"\n")
        elif defect == "declared_count":
            manifest["common_method_comparisons"] += 1
        else:
            with matrix_path.open("a") as stream:
                stream.write("\n")
        manifest_path.write_text(json.dumps(manifest))
        before = sorted(path.relative_to(root) for path in root.rglob("*"))
        with pytest.raises(ValueError):
            M.plot(root)
        assert sorted(path.relative_to(root) for path in root.rglob("*")) == before
