"""Protect equal physical-outage weights in the standalone campaign analysis."""

from copy import deepcopy

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
