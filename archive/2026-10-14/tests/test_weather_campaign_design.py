"""Independent checks for reproducible, observed-truth weather outage panels."""

import json
from itertools import pairwise

import numpy as np
import pandas as pd
import pytest

from examples.weather_imputation_sensitivity import campaign_design as design


@pytest.fixture
def originals():
    """A complete fixed-LST year with observed buffer hours and native cloud gaps."""
    index = pd.date_range("2022-12-30", "2024-01-03", freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "dry_bulb": 12.0,
            "relative_humidity": 60.0,
            "pressure": 1010.0,
            "wind_speed": 3.0,
            "wind_direction": 180.0,
            "ghi": 100.0,
            "dni": 200.0,
            "dirh": 50.0,
            "cloud_cover": np.nan,
        },
        index=index,
    )


def panels(originals, **kwargs):
    """Keep each check small while retaining the complete four-season design."""
    return design.generate_panels(
        originals,
        site_id="72420",
        year=2023,
        scopes=kwargs.pop("scopes", ("meteorology",)),
        durations=kwargs.pop("durations", (3,)),
        seeds=kwargs.pop("seeds", (11,)),
        anchors=kwargs.pop("anchors", {}),
        **kwargs,
    )


def selected_runs(mask):
    edges = np.diff(np.r_[False, mask.to_numpy(dtype=bool), False].astype(int))
    return list(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)))


def test_repeated_design_has_identical_masks_and_json_receipts(originals):
    first = panels(originals, durations=(3, 48), seeds=(11, 29))
    repeated = panels(originals, durations=(3, 48), seeds=(11, 29))

    assert len(first) == len(repeated) == 4
    assert len({panel.evidence_id for panel in first}) == 4
    for left, right in zip(first, repeated):
        assert left.status == right.status == "ready"
        assert left.evidence_id == right.evidence_id
        pd.testing.assert_series_equal(left.mask, right.mask)
        assert json.dumps(left.to_record(), sort_keys=True, allow_nan=False) == (
            json.dumps(right.to_record(), sort_keys=True, allow_nan=False)
        )


def test_changed_random_seed_changes_selected_weather_hours(originals):
    first = panels(originals, seeds=(11,))[0]
    second = panels(originals, seeds=(29,))[0]

    assert first.sampling == second.sampling == "random_seasonal"
    assert first.seed == 11 and second.seed == 29
    assert not first.mask.equals(second.mask)
    assert first.evidence_id != second.evidence_id


def test_random_panel_contains_four_complete_separated_seasonal_blocks(originals):
    panel = panels(originals, durations=(12,))[0]

    assert panel.status == "ready"
    assert panel.mask.dtype == bool
    pd.testing.assert_index_equal(panel.mask.index, originals.index)
    assert {block.season for block in panel.blocks} == {"DJF", "MAM", "JJA", "SON"}
    assert len(panel.blocks) == 4
    assert all(block.hours == 12 for block in panel.blocks)
    assert panel.mask.sum() == 48
    runs = selected_runs(panel.mask)
    assert len(runs) == 4
    assert all(end - start == 12 for start, end in runs)
    assert all(next_start > end for (_, end), (next_start, _) in pairwise(runs))
    label_days = originals.index[panel.mask].tz_convert("Etc/GMT-1") - pd.Timedelta(
        hours=1
    )
    assert np.all(label_days.year == 2023)
    for start, end in runs:
        target_and_neighbors = originals.iloc[start - 1 : end + 1][list(panel.columns)]
        assert np.isfinite(target_and_neighbors.to_numpy(dtype=float)).all()


def test_two_blocks_per_season_give_eight_separated_observed_blocks(originals):
    panel = panels(originals, durations=(12,), blocks_per_season=2)[0]
    runs = selected_runs(panel.mask)

    assert panel.status == "ready"
    assert len(panel.blocks) == len(runs) == 8
    assert panel.mask.sum() == 96
    assert all(end - start == 12 for start, end in runs)
    assert all(next_start > end for (_, end), (next_start, _) in pairwise(runs))
    for season in ("DJF", "MAM", "JJA", "SON"):
        assert sum(block.season == season for block in panel.blocks) == 2


def test_single_variable_scope_keeps_unrelated_meteorology_untouched(originals):
    originals["pressure"] = np.nan
    before = originals.copy(deep=True)
    panel = panels(originals, scopes=("dry_bulb",))[0]
    fitting_inputs = panel.withheld(originals)

    assert panel.status == "ready"
    assert panel.columns == ("dry_bulb",)
    assert fitting_inputs.loc[panel.mask, "dry_bulb"].isna().all()
    pd.testing.assert_frame_equal(
        fitting_inputs.drop(columns="dry_bulb"), before.drop(columns="dry_bulb")
    )
    pd.testing.assert_frame_equal(originals, before)


def test_selected_truth_and_neighbor_anchors_exclude_nan_and_infinity(originals):
    originals.iloc[::24, originals.columns.get_loc("dry_bulb")] = np.nan
    originals.iloc[12::24, originals.columns.get_loc("wind_speed")] = np.inf
    panel = panels(originals)[0]

    assert panel.status == "ready"
    assert np.isfinite(originals.loc[panel.mask, list(panel.columns)]).all().all()
    for start, end in selected_runs(panel.mask):
        boundary_truth = originals.iloc[[start - 1, end]][list(panel.columns)]
        assert np.isfinite(boundary_truth.to_numpy(dtype=float)).all()


def test_missing_season_is_explicitly_unsupported_without_partial_mask(originals):
    spring = originals.index.month.isin([3, 4, 5]) & (originals.index.year == 2023)
    originals.loc[spring, "dry_bulb"] = np.nan
    panel = panels(originals)[0]

    assert panel.status == "unsupported"
    assert panel.reason and "MAM" in panel.reason
    assert not panel.mask.any()
    assert not panel.blocks


def test_missing_target_column_is_unsupported_without_synthetic_truth(originals):
    panel = panels(originals.drop(columns="pressure"))[0]

    assert panel.status == "unsupported"
    assert panel.reason and "pressure" in panel.reason
    assert not panel.mask.any()
    assert not panel.blocks


@pytest.mark.parametrize("invalid_calendar", ["missing_final_interval", "naive_index"])
def test_invalid_original_calendar_fails_before_design(originals, invalid_calendar):
    if invalid_calendar == "missing_final_interval":
        originals = originals.drop(index=pd.Timestamp("2023-12-31 23:00", tz="UTC"))
    else:
        originals.index = originals.index.tz_localize(None)

    with pytest.raises(ValueError):
        panels(originals)


def test_native_gap_at_weather_anchor_is_explicitly_unsupported(originals):
    anchor = pd.Timestamp("2023-07-15 12:00", tz="UTC")
    originals.loc[anchor, "dry_bulb"] = np.nan
    all_panels = panels(originals, anchors={"summer_peak": anchor})
    extreme = [panel for panel in all_panels if panel.sampling == "weather_extremes"]

    assert len(extreme) == 1
    assert extreme[0].status == "unsupported"
    assert extreme[0].reason
    assert not extreme[0].mask.any()
    assert not extreme[0].blocks


def test_last_december_31_interval_remains_inside_the_requested_lst_year(originals):
    anchor = pd.Timestamp("2023-12-31 23:00", tz="UTC")
    all_panels = panels(originals, anchors={"year_end_peak": anchor})
    panel = next(panel for panel in all_panels if panel.sampling == "weather_extremes")

    assert panel.status == "ready"
    assert len(panel.blocks) == 1
    assert panel.mask.loc[anchor]
    assert panel.mask.sum() == 3
    assert originals.index[panel.mask].equals(
        pd.date_range("2023-12-31 21:00", periods=3, freq="h", tz="UTC")
    )
    assert not panel.mask.loc[pd.Timestamp("2024-01-01", tz="UTC")]
    assert panel.blocks[0].anchor_name == "year_end_peak"
    assert panel.blocks[0].anchor_utc == anchor


def test_overlapping_weather_anchors_get_independent_panels(originals):
    anchors = {
        "temperature_peak": pd.Timestamp("2023-07-15 12:00", tz="UTC"),
        "cooling_peak": pd.Timestamp("2023-07-15 13:00", tz="UTC"),
    }
    selected = panels(originals, durations=(12,), anchors=anchors)
    extreme = [panel for panel in selected if panel.sampling == "weather_extremes"]

    assert len(extreme) == 2
    assert all(panel.status == "ready" for panel in extreme)
    assert all(len(panel.blocks) == 1 and panel.mask.sum() == 12 for panel in extreme)
    assert {panel.blocks[0].anchor_name for panel in extreme} == set(anchors)
    assert (extreme[0].mask & extreme[1].mask).any()
    assert extreme[0].evidence_id != extreme[1].evidence_id


@pytest.mark.parametrize(
    "anchor,season,expected_start",
    [
        ("2023-02-28 23:00", "DJF", "2023-02-28 22:00"),
        ("2023-03-01 00:00", "MAM", "2023-02-28 23:00"),
    ],
)
def test_weather_anchor_windows_can_center_across_a_season_boundary(
    originals, anchor, season, expected_start
):
    stamp = pd.Timestamp(anchor, tz="UTC")
    selected = panels(originals, anchors={"season_edge": stamp})
    panel = next(panel for panel in selected if panel.sampling == "weather_extremes")

    assert panel.status == "ready"
    assert panel.blocks[0].season == season
    assert panel.blocks[0].start_utc == pd.Timestamp(expected_start, tz="UTC")
    assert panel.mask.loc[stamp]
    label_hours = originals.index[panel.mask].tz_convert("Etc/GMT-1") - pd.Timedelta(
        hours=1
    )
    assert np.all(label_hours.year == 2023)


def test_evidence_identity_tracks_the_original_selected_truth(originals):
    original_panel = panels(originals)[0]
    changed = originals.copy(deep=True)
    selected_hour = originals.index[original_panel.mask][0]
    changed.loc[selected_hour, "dry_bulb"] += 1
    changed_panel = panels(changed)[0]

    pd.testing.assert_series_equal(original_panel.mask, changed_panel.mask)
    assert original_panel.evidence_id != changed_panel.evidence_id
    record = original_panel.to_record()
    assert record["masked_hours"] == 12
    assert record["masked_original_cells"] == 12 * len(original_panel.columns)
    assert [pd.Timestamp(hour) for hour in record["masked_utc"]] == list(
        originals.index[original_panel.mask]
    )


def test_withheld_hides_all_target_truth_and_preserves_raw_unmasked_cells(originals):
    originals.loc[originals.index[100], "dry_bulb"] = np.nan
    before = originals.copy(deep=True)
    panel = panels(originals, durations=(12,))[0]
    fitting_inputs = panel.withheld(originals)

    assert fitting_inputs is not originals
    assert fitting_inputs.loc[panel.mask, list(panel.columns)].isna().all().all()
    pd.testing.assert_frame_equal(
        fitting_inputs.loc[~panel.mask], before.loc[~panel.mask]
    )
    remaining = [column for column in originals if column not in panel.columns]
    pd.testing.assert_frame_equal(fitting_inputs[remaining], before[remaining])
    pd.testing.assert_frame_equal(originals, before)


def test_withheld_rejects_selected_truth_changed_after_panel_selection(originals):
    panel = panels(originals)[0]
    selected_hour = originals.index[panel.mask][0]
    changed = originals.copy(deep=True)
    changed.loc[selected_hour, "dry_bulb"] += 1

    with pytest.raises(ValueError):
        panel.withheld(changed)


@pytest.mark.parametrize(
    "duplicate_config", [{"seeds": (11, 11)}, {"durations": (3, 3)}]
)
def test_duplicate_sampling_configuration_is_rejected(originals, duplicate_config):
    with pytest.raises(ValueError):
        panels(originals, **duplicate_config)


def test_counts_keep_unsupported_temporal_and_solar_donors_out_of_simulations(
    originals,
):
    selected = panels(originals, scopes=("meteorology", "solar"), durations=(3, 168))
    receipts_before = [panel.to_record() for panel in selected]
    counts = design.estimate_counts(selected, building_variants=1)

    assert counts == {
        "panels_total": 4,
        "panels_ready": 4,
        "panels_unsupported": 0,
        "weather_cases": 14,
        "method_combinations_unsupported": 6,
        "building_variants": 1,
        "reference_simulations": 1,
        "scenario_simulations": 14,
        "total_simulations": 15,
    }
    more_buildings = design.estimate_counts(selected, building_variants=3)
    assert more_buildings["weather_cases"] == 14
    assert more_buildings["reference_simulations"] == 3
    assert more_buildings["scenario_simulations"] == 42
    assert more_buildings["total_simulations"] == 45
    assert [panel.to_record() for panel in selected] == receipts_before
    json.dumps(receipts_before, allow_nan=False)


def test_an_unsupported_panel_counts_all_methods_without_scenario_runs(originals):
    anchor = pd.Timestamp("2023-07-15 12:00", tz="UTC")
    originals.loc[anchor, "dry_bulb"] = np.nan
    selected = panels(originals, anchors={"native_gap": anchor})
    counts = design.estimate_counts(selected)

    assert counts["panels_total"] == 2
    assert counts["panels_ready"] == 1
    assert counts["panels_unsupported"] == 1
    assert counts["weather_cases"] == 5
    assert counts["method_combinations_unsupported"] == 5
    assert counts["scenario_simulations"] == 5
    assert counts["reference_simulations"] == 1


def test_reference_count_deduplicates_site_years_across_panels_and_buildings(originals):
    first_site = panels(originals, scopes=("meteorology", "solar"))
    second_site = design.generate_panels(
        originals,
        site_id="another_site",
        year=2023,
        scopes=("meteorology",),
        durations=(3,),
        seeds=(11,),
        anchors={},
    )
    counts = design.estimate_counts(
        first_site + second_site, building_variants=("baseline", "high_gain")
    )

    assert counts["panels_total"] == 3
    assert counts["weather_cases"] == 13
    assert counts["building_variants"] == 2
    assert counts["reference_simulations"] == 4
    assert counts["scenario_simulations"] == 26
    assert counts["total_simulations"] == 30


def test_count_helper_rejects_duplicate_evidence_panels(originals):
    panel = panels(originals)[0]

    with pytest.raises(ValueError):
        design.estimate_counts([panel, panel])


def test_current_and_legacy_automatic_are_paired_at_short_and_long_outages(originals):
    selected = panels(originals, durations=(3, 168))
    counts = design.estimate_counts(
        selected, methods=("automatic", "automatic_legacy"), building_variants=3
    )

    assert counts["panels_ready"] == 2
    assert counts["weather_cases"] == 4
    assert counts["method_combinations_unsupported"] == 0
    assert counts["reference_simulations"] == 3
    assert counts["scenario_simulations"] == 12
    assert counts["total_simulations"] == 15
