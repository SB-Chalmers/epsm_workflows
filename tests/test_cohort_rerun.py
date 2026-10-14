"""Rerun comparisons preserve failed cases and independent-property pairing."""

import pytest

from epsm_workflows.cohort_transfer.rerun import indexed, paired_change


def test_pairing_keeps_failure_transitions_and_counts_worsened_buildings():
    rows = [{"property_key": p, "target": 100} for p in ("a", "a", "b", "c")]
    result = paired_change(rows, [130, 100, None, 100], [110, 120, 100, None], 100)
    assert result["complete_pairs"] == 2
    assert result["improved"] == result["worsened"] == 1
    assert result["paired_mae_change_kwh_m2"] == 0
    assert result["new_failures"] == result["recovered_failures"] == 1
    assert result["resampling_unit"] == "property"
    assert len(result["coverage20_change_95_interval"]) == 2


def test_bootstrap_resamples_sibling_buildings_together():
    rows = [{"property_key": "a", "target": 100}] * 2
    result = paired_change(rows, [130, 100], [100, 130], 100)
    assert result["paired_mae_change_95_interval"] == [0, 0]
    assert result["coverage20_change_95_interval"] == [0, 0]


def test_duplicate_predictions_cannot_hide_missing_attempts():
    with pytest.raises(ValueError, match="Duplicate"):
        indexed([{"building_id": "a"}, {"building_id": "a"}])
