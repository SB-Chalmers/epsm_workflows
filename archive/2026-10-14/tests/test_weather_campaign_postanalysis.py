"""Protect equal physical-outage weights in the standalone campaign analysis."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pandas as pd
import pytest

from examples import analyze_weather_imputation_campaign as A


@pytest.fixture
def panel():
    """Represent one observed outage with independently named event anchors."""
    return {
        "evidence_id": "annual-extreme",
        "scope": "meteorology",
        "columns": ["dry_bulb", "relative_humidity"],
        "duration_hours": 3,
        "status": "ready",
        "anchor_name": "warmest_original_hour",
        "blocks": [
            {
                "start_utc": "2023-06-12T10:00:00+00:00",
                "end_utc": "2023-06-12T12:00:00+00:00",
                "hours": 3,
                "anchor_name": "warmest_original_hour",
            }
        ],
    }


def test_identical_physical_masks_ignore_anchor_labels_and_column_order(panel):
    duplicate = deepcopy(panel)
    duplicate["evidence_id"] = "heatwave-anchor"
    duplicate["anchor_name"] = "heatwave_hottest_hour"
    duplicate["blocks"][0]["anchor_name"] = "heatwave_hottest_hour"
    duplicate["columns"].reverse()
    assert A.physical_signature(panel) == A.physical_signature(duplicate)


@pytest.mark.parametrize("change", ["columns", "scope", "window"])
def test_distinct_input_masks_keep_distinct_weights(panel, change):
    changed = deepcopy(panel)
    if change == "columns":
        changed["columns"].append("wind_speed")
    elif change == "scope":
        changed["scope"] = "different_input_scope"
    else:
        changed["blocks"][0]["start_utc"] = "2023-06-12T11:00:00+00:00"
        changed["blocks"][0]["end_utc"] = "2023-06-12T13:00:00+00:00"
    assert A.physical_signature(panel) != A.physical_signature(changed)


def test_unsupported_requests_without_truth_do_not_collapse(panel):
    panel["status"], panel["blocks"] = "unsupported", []
    other = panel | {"evidence_id": "different-unsupported-anchor"}
    assert A.physical_signature(panel) != A.physical_signature(other)


def test_real_ready_catalogue_status_counts_as_available_weather(panel):
    decision = panel | {"case_id": "annual-extreme-automatic", "method": "automatic"}
    assert A.availability_status(panel, "automatic", decision) == "available"
    assert A.availability_status(panel, "automatic", {}) == "not_prepared"
    assert (
        A.availability_status(panel, "automatic", {"status": "unavailable"})
        == "unavailable"
    )
    assert (
        A.availability_status(panel | {"duration_hours": 168}, "temporal", {})
        == "unsupported_method"
    )


def test_deduplication_keeps_anchor_mapping_and_rejects_inconsistent_loads(panel):
    row = {
        "site_id": "fixture",
        "year": 2023,
        "building_id": "building",
        "scope": "meteorology",
        "sampling": "weather_extremes",
        "gap_hours": 3,
        "physical_signature": A.physical_signature(panel),
        "method": "automatic",
        "case_id": "annual-extreme-automatic",
        "evidence_id": "annual-extreme",
        "anchor_name": "warmest_original_hour",
        "annual_heating_kwh": 100,
    }
    duplicate = row | {
        "case_id": "heatwave-anchor-automatic",
        "evidence_id": "heatwave-anchor",
        "anchor_name": "heatwave_hottest_hour",
    }
    result = A.deduplicate(pd.DataFrame([row, duplicate]))
    assert len(result) == 1
    assert result.iloc[0].duplicate_evidence_count == 2
    assert "heatwave_hottest_hour" in result.iloc[0].anchor_names
    duplicate["annual_heating_kwh"] = 120
    with pytest.raises(ValueError, match="physical outage responses differ"):
        A.deduplicate(pd.DataFrame([row, duplicate]))


def test_event_exposure_uses_interval_starts_and_separates_overlap_from_exact():
    panel = {"masked_utc": ["2023-01-01T00:00:00+00:00"]}
    comparison = {
        "reference": {
            "events": [
                {
                    "type": "heatwave",
                    "start_lst": "2022-12-31T23:00:00+00:00",
                    "end_lst": "2023-01-01T00:00:00+00:00",
                },
                {
                    "type": "heatwave",
                    "start_lst": "2023-01-01T00:00:00+00:00",
                    "end_lst": "2023-01-01T01:00:00+00:00",
                },
            ]
        },
        "by_type": {
            "heatwave": {
                "matches": [
                    {
                        "reference_index": 0,
                        "start_shift_hours": 0,
                        "end_shift_hours": 1,
                        "peak_value_change": 0,
                    }
                ],
                "lost_reference_count": 1,
                "false_candidate_count": 0,
                "reference_hours": 2,
                "candidate_hours": 2,
            }
        },
    }
    result = A.event_exposure(panel, comparison, "heatwave")
    assert result["reference_exposed_events"] == 1
    assert result["reference_hours_hidden"] == 1
    assert result["exposed_events_with_any_overlap"] == 1
    assert result["exposed_events_with_exact_bounds"] == 0
    assert result["lost_unexposed_reference_events"] == 1


def test_common_methods_require_every_profile_to_qualify_each_scheduled_method():
    """One unqualified method/profile removes the whole physical comparison cohort."""
    methods = ["temporal", "donor_assessed", "era5", "automatic", "automatic_legacy"]
    matrix = {"buildings": {"a": {}, "b": {}}, "meteorology_methods": methods}
    rows = pd.DataFrame(
        [
            {
                "site_id": "fixture",
                "year": 2023,
                "building_id": building,
                "scope": "meteorology",
                "sampling": "weather_extremes",
                "gap_hours": 48,
                "physical_signature": "shared",
                "method": method,
            }
            for building in matrix["buildings"]
            for method in methods
        ]
    )
    assert len(A.common_method_cohort(rows, matrix)) == 10
    partial = rows.loc[~(rows.building_id.eq("b") & rows.method.eq("era5"))]
    assert A.common_method_cohort(partial, matrix).empty
    long = rows.loc[rows.method.ne("temporal")].copy()
    long["gap_hours"] = 168
    assert len(A.common_method_cohort(long, matrix)) == 8


def test_paired_analysis_retains_qualified_case_ids_for_publication():
    """The report must trace both paired responses back to screened raw rows."""
    shared = {
        "site_id": "fixture",
        "year": 2023,
        "building_id": "building",
        "scope": "meteorology",
        "sampling": "weather_extremes",
        "gap_hours": 48,
        "physical_signature": "shared-mask",
        "donor_all_five_series_available": True,
        "anchor_names": "[]",
    }
    for mode in ("heating", "cooling"):
        shared[f"baseline_{mode}_peak_hidden"] = True
        shared[f"same_{mode}_peak_hours"] = False
        shared[f"baseline_peak_{mode}_w_m2"] = 10.0
    rows = pd.DataFrame(
        [
            shared | {"method": method, "case_id": "outage-" + method}
            for method in ("automatic", "automatic_legacy")
        ]
    )
    pairs, _ = A.paired_hierarchy(rows, [])
    assert pairs.case_id_new.tolist() == ["outage-automatic"]
    assert pairs.case_id_old.tolist() == ["outage-automatic_legacy"]


@pytest.mark.parametrize("baseline_warning", [False, True])
def test_qualification_excludes_building_outcomes_but_retains_weather_and_events(
    tmp_path,
    monkeypatch,
    baseline_warning,
):
    """Explicit injected diagnostics isolate qualification from available weather evidence."""
    root, output = tmp_path / "campaign", tmp_path / "analysis"
    folder = root / "datasets/fixture/2023"
    methods = ["temporal", "donor_assessed", "era5", "automatic", "automatic_legacy"]
    matrix = {
        "sites": [{"site_id": "fixture"}],
        "years": [2023],
        "buildings": {"a": {}, "b": {}},
        "meteorology_methods": methods,
        "solar_methods": ["temporal", "era5", "automatic"],
    }

    def write(path, value):
        """Persist isolated immutable fixture documents with explicit JSON bytes."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    write(root / "matrix.json", matrix)
    write(
        folder / "sources/receipt.json",
        {
            "status": "ready",
            "donor": {str(i): {"status": "available"} for i in range(5)},
        },
    )
    panel = {
        "evidence_id": "outage",
        "scope": "meteorology",
        "status": "ready",
        "sampling": "weather_extremes",
        "duration_hours": 3,
        "columns": ["dry_bulb"],
        "masked_utc": ["2023-07-01T10:00:00+00:00"],
        "blocks": [
            {
                "start_utc": "2023-07-01T10:00:00+00:00",
                "end_utc": "2023-07-01T12:00:00+00:00",
                "hours": 3,
            }
        ],
    }
    write(folder / "panel-plan.json", {"all_panels": [panel]})
    cases = [{"case_id": "reference", "method": "reference"}] + [
        {"case_id": "outage-" + method, "method": method, "status": "ready"}
        for method in methods
    ]
    write(folder / "case-catalogue.json", {"cases": cases, "unavailable": []})
    write(
        folder / "audit.json",
        {"full_matrix_site_year_complete": True, "comparisons": 12},
    )
    events = {
        "reference": {"events": []},
        "by_type": {
            kind: {
                "matches": [],
                "lost_reference_count": 0,
                "false_candidate_count": 0,
                "reference_hours": 0,
                "candidate_hours": 0,
            }
            for kind in A.EVENTS
        },
    }
    for method in methods:
        write(folder / "cases" / ("outage-" + method) / "events.json", events)
    rows = []
    for case in cases:
        for building in matrix["buildings"]:
            row = {
                "site_id": "fixture",
                "year": 2023,
                "building_id": building,
                "method": case["method"],
                "case_id": case["case_id"],
                "evidence_id": "reference"
                if case["method"] == "reference"
                else "outage",
                "scope": "reference"
                if case["method"] == "reference"
                else "meteorology",
                "sampling": "reference"
                if case["method"] == "reference"
                else "weather_extremes",
                "gap_hours": 0 if case["method"] == "reference" else 3,
                "floor_area_m2": 100.0,
                "weather_dry_bulb_mae": 0.5,
            }
            for mode in ("heating", "cooling"):
                row.update(
                    {
                        f"annual_{mode}_kwh_m2": 10.0,
                        f"peak_{mode}_w_m2": 20.0,
                        f"delta_annual_{mode}_kwh_m2": 0.1,
                        f"hourly_{mode}_load_mae_kw": 0.01,
                        f"maximum_absolute_{mode}_load_change_kw": 0.2,
                        f"baseline_{mode}_peak_hidden": False,
                        f"same_{mode}_peak_hours": True,
                    }
                )
            rows.append(row)
    path = folder / "summary.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    write(
        folder / "summary.csv.receipt.json",
        {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "rows": len(rows)},
    )

    def qualification_fixture(campaign, raw, inputs):
        """Inject explicitly synthetic qualification without pretending to authenticate Core."""
        result = raw.copy()
        bad = result.method.eq("era5") & result.building_id.eq("b")
        if baseline_warning:
            bad = pd.Series(True, index=result.index)
        result["engine_run_qualified"] = ~bad
        result["engine_outcome_qualified"] = ~bad
        result["engine_exclusion_reasons"] = [
            '["warning_fixture"]' if value else "[]" for value in bad
        ]
        return result, result.loc[bad].copy(), {"policy": "fixture", "inputs": {}}

    monkeypatch.chdir(tmp_path)
    manifest = A.analyze(
        root.relative_to(tmp_path),
        output.relative_to(tmp_path),
        qualifier=qualification_fixture,
    )
    assert manifest["captured_building_rows"] == 12
    assert manifest["qualified_building_rows"] == (0 if baseline_warning else 11)
    assert manifest["excluded_building_rows"] == (12 if baseline_warning else 1)
    assert manifest["qualified_reference_building_rows"] == (
        0 if baseline_warning else 2
    )
    assert manifest["qualified_reference_site_years"] == (0 if baseline_warning else 1)
    assert manifest["hierarchy_pairs"] == (0 if baseline_warning else 2)
    assert manifest["common_method_comparisons"] == 0
    assert manifest["campaign_root"] == str(root.resolve())
    assert str(path.resolve()) in manifest["inputs"]
    assert all(Path(name).is_absolute() for name in manifest["inputs"])
    weather = pd.read_csv(output / "weather-comparisons.csv")
    assert len(weather) == 5 and "era5" in set(weather.method)
    event_rows = pd.read_csv(output / "event-exposure-comparisons.csv")
    assert len(event_rows.loc[event_rows.method.eq("era5")]) == len(A.EVENTS)
    availability = pd.read_csv(output / "method-availability-panels.csv")
    era = availability.loc[availability.method.eq("era5")].iloc[0]
    assert era.status == "available" and era.evaluated_buildings == 2
    assert era.qualified_buildings == (0 if baseline_warning else 1)
    assert era.engine_unqualified_buildings == (2 if baseline_warning else 1)
    receipt = json.loads((output / "diagnostics-qualification.json").read_text())
    assert receipt["evidence_mode"] == "injected_fixture"
