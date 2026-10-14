"""Meaningful pairing and denominator checks for campaign summaries."""

import pandas as pd
import pytest

from examples.weather_imputation_sensitivity.campaign_analysis import (
    paired_hierarchy,
    summarize,
)


def row(method="automatic", evidence="panel-a", site="north", value=2.0):
    result = dict(
        site_id=site,
        year=2023,
        building_id="house",
        evidence_id=evidence,
        scope="meteorology",
        sampling="random_seasonal",
        gap_hours=48,
        method=method,
    )
    for mode in ("heating", "cooling"):
        result[f"delta_annual_{mode}_pct"] = value
        result[f"delta_peak_{mode}_pct"] = value
        result[f"same_{mode}_peak_hours"] = True
        result[f"baseline_{mode}_peak_hidden"] = False
    return result


def test_hierarchy_pairs_do_not_compare_different_missing_evidence():
    records = [
        row(),
        row("automatic_legacy", value=-5),
        row("automatic_legacy", "panel-b"),
    ]
    result = paired_hierarchy(pd.DataFrame(records))
    assert len(result) == 1
    assert result.iloc[0].delta_peak_heating_pct_absolute_error_change_pp == -3
    assert result.iloc[0].delta_peak_heating_pct_improved


def test_duplicate_pair_is_rejected_instead_of_inflating_sample_size():
    with pytest.raises(ValueError, match="Duplicate"):
        paired_hierarchy(pd.DataFrame([row(), row(), row("automatic_legacy")]))


def test_quantiles_keep_site_year_counts_and_missing_zero_demand_denominator():
    missing = row(evidence="panel-c", value=None)
    missing["same_cooling_peak_hours"] = None
    table = summarize(pd.DataFrame([row(), row(evidence="panel-b", value=-6), missing]))
    result = table.iloc[0]
    assert result.comparisons == 3 and result.site_years == 1
    assert result.evidence_panels == 3
    assert result.delta_annual_heating_pct_valid_n == 2
    assert result.delta_annual_heating_pct_abs_max == 6
    assert result.same_cooling_peak_valid_n == 2


def test_site_year_summary_does_not_give_more_weight_to_more_masks():
    records = [row(evidence=f"panel-{i}", value=1) for i in range(3)]
    records.append(row(site="south", evidence="panel-other", value=9))
    result = summarize(pd.DataFrame(records)).iloc[0]
    assert result.delta_annual_heating_pct_median == 1
    assert result.delta_annual_heating_pct_site_year_median == 5
    assert result.site_years == 2
