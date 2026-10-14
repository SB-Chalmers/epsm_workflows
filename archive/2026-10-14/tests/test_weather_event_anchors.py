"""Target actual detected spells independently of isolated annual extrema."""

from datetime import timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("smhi2epw")

from examples.weather_imputation_sensitivity.event_anchors import weather_anchors


def reference_and_originals(maxima, minima, *, start="2023-06-01", offset=1):
    """Make complete EPW-label days and their original UTC source timestamps."""
    local = timezone(timedelta(hours=offset))
    index = pd.date_range(
        pd.Timestamp(start, tz=local) + pd.Timedelta(hours=1),
        periods=24 * len(maxima),
        freq="h",
    )
    temperatures = np.repeat((np.asarray(maxima) + np.asarray(minima)) / 2, 24)
    temperatures[14::24] = maxima
    temperatures[5::24] = minima
    reference = pd.DataFrame({"dry_bulb": temperatures}, index=index)
    reference.attrs["location"] = {"time_zone": offset}
    originals = reference.copy()
    originals.index = originals.index.tz_convert("UTC")
    context = SimpleNamespace(year=2023, utc_offset=offset)
    return reference, originals, context


def test_heatwave_anchor_is_inside_earlier_wave_not_isolated_annual_maximum():
    reference, originals, context = reference_and_originals(
        [26, 28, 27, 20, 20, 32, 20], [18, 18, 18, 10, 10, 15, 10]
    )
    saved_reference = reference.copy(deep=True)
    saved_originals = originals.copy(deep=True)
    anchors = weather_anchors(originals, reference, context)
    assert anchors["warmest_original_hour"] == originals.index[5 * 24 + 14]
    assert anchors["heatwave_hottest_hour"] == originals.index[24 + 14]
    assert "cold_spell_coldest_hour" not in anchors
    assert str(anchors["heatwave_hottest_hour"].tz) == "UTC"
    pd.testing.assert_frame_equal(reference, saved_reference)
    pd.testing.assert_frame_equal(originals, saved_originals)


def test_cold_spell_anchor_is_not_isolated_coldest_hour():
    reference, originals, context = reference_and_originals(
        [5, 4, 4, 5, 4, 4, 4], [0, -11, -14, -12, 0, -20, 0], start="2023-11-01"
    )
    anchors = weather_anchors(originals, reference, context)
    assert anchors["coldest_original_hour"] == originals.index[5 * 24 + 5]
    assert anchors["cold_spell_coldest_hour"] == originals.index[2 * 24 + 5]
    assert "heatwave_hottest_hour" not in anchors


def test_no_absolute_events_returns_only_original_extrema():
    reference, originals, context = reference_and_originals(
        [24, 23, 22, 21], [-9, -8, -7, -6]
    )
    assert weather_anchors(originals, reference, context) == {
        "warmest_original_hour": originals.dry_bulb.idxmax(),
        "coldest_original_hour": originals.dry_bulb.idxmin(),
    }


def test_missing_original_at_reference_wave_peak_selects_next_original_in_event():
    reference, originals, context = reference_and_originals(
        [26, 28, 27, 20, 20, 32, 20], [18, 18, 18, 10, 10, 15, 10]
    )
    originals.loc[originals.index[24 + 14], "dry_bulb"] = np.nan
    anchors = weather_anchors(originals, reference, context)
    assert anchors["heatwave_hottest_hour"] == originals.index[2 * 24 + 14]
    assert np.isfinite(originals.loc[anchors["heatwave_hottest_hour"], "dry_bulb"])
    originals.iloc[:72, originals.columns.get_loc("dry_bulb")] = np.nan
    assert "heatwave_hottest_hour" not in weather_anchors(originals, reference, context)


@pytest.mark.parametrize("offset", [0, 1, 2])
def test_hour24_on_last_wave_day_is_inside_event_and_maps_to_correct_utc(offset):
    reference, originals, context = reference_and_originals(
        [26, 26, 26, 20], [18, 18, 18, 10], offset=offset
    )
    # The maximum ends at midnight after the final heatwave labelday. It must
    # remain inside that day when selecting the original source UTC timestamp.
    reference.loc[reference.index[71], "dry_bulb"] = 30
    originals.loc[originals.index[71], "dry_bulb"] = 30
    anchors = weather_anchors(originals, reference, context)
    assert anchors["heatwave_hottest_hour"] == originals.index[71]
    assert anchors["heatwave_hottest_hour"] == pd.Timestamp(
        "2023-06-04", tz=timezone(timedelta(hours=offset))
    ).tz_convert("UTC")


def test_year_selection_uses_interval_start_not_hour_ending_year():
    reference, originals, context = reference_and_originals(
        [26, 27, 28], [18, 18, 18], start="2023-12-29"
    )
    reference.loc[reference.index[-1], "dry_bulb"] = 30
    originals.loc[originals.index[-1], "dry_bulb"] = 30
    anchors = weather_anchors(originals, reference, context)
    assert anchors["warmest_original_hour"] == originals.index[-1]
    assert anchors["heatwave_hottest_hour"] == originals.index[-1]


def test_naive_epw_calendar_gets_explicit_context_offset_without_mutation():
    reference, originals, context = reference_and_originals([26, 27, 28], [18, 18, 18])
    reference.index = reference.index.tz_localize(None)
    reference.attrs = {}
    expected = originals.index[2 * 24 + 14]
    assert (
        weather_anchors(originals, reference, context)["heatwave_hottest_hour"]
        == expected
    )
    assert reference.attrs == {} and reference.index.tz is None


def test_parsed_epw_calendar_fields_map_hour24_to_original_utc():
    reference, originals, context = reference_and_originals([26, 27, 28], [18, 18, 18])
    reference.loc[reference.index[-1], "dry_bulb"] = 30
    originals.loc[originals.index[-1], "dry_bulb"] = 30
    labels = reference.index - pd.Timedelta(hours=1)
    parsed = reference.reset_index(drop=True)
    parsed["year"] = labels.year
    parsed["month"] = labels.month
    parsed["day"] = labels.day
    parsed["hour"] = labels.hour + 1
    anchors = weather_anchors(originals, parsed, context)
    assert anchors["heatwave_hottest_hour"] == originals.index[-1]
    assert anchors["warmest_original_hour"] == originals.index[-1]


def test_no_original_temperature_evidence_and_offset_mismatch_are_rejected():
    reference, originals, context = reference_and_originals([26, 27, 28], [18, 18, 18])
    missing = originals.copy()
    missing["dry_bulb"] = np.nan
    with pytest.raises(ValueError, match="original finite temperature"):
        weather_anchors(missing, reference, context)
    with pytest.raises(ValueError, match="offset differs"):
        weather_anchors(originals, reference, SimpleNamespace(year=2023, utc_offset=2))
