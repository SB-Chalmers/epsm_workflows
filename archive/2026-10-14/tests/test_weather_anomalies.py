"""Synthetic checks for interval labels and baseline-pinned weather events."""

from datetime import timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from examples.weather_imputation_sensitivity.anomalies import (
    compare_events,
    detect_events,
)

LST = timezone(timedelta(hours=1))


def weather_for_days(maxima, *, start="2023-07-01", minima=None):
    """Create complete EPW-label days with their maximum in hour 24."""
    maxima = np.asarray(maxima, dtype=float)
    endpoints = pd.date_range(
        pd.Timestamp(start, tz=LST) + pd.Timedelta(hours=1),
        periods=len(maxima) * 24,
        freq="h",
    )
    if minima is None:
        minima = maxima - 1
    minima = np.asarray(minima, dtype=float)
    temperature = np.repeat(minima, 24)
    temperature[23::24] = maxima
    weather = pd.DataFrame(
        {
            "dry_bulb": temperature,
            "wind_speed": np.ones(len(endpoints)),
            "ghi": np.zeros(len(endpoints)),
        },
        index=endpoints,
    )
    weather.attrs["location"] = {"latitude": 57.7, "longitude": 11.97}
    return weather


def events_of_type(report, event_type):
    return [event for event in report["events"] if event["type"] == event_type]


def test_three_heatwave_days_include_hour_24_in_the_previous_label_day():
    weather = weather_for_days([25, 25, 25], minima=[20, 20, 20])
    report = detect_events(weather)

    heatwaves = events_of_type(report, "heatwave")
    assert len(heatwaves) == 1
    event = heatwaves[0]
    assert event["start_date"] == "2023-07-01"
    assert event["end_date"] == "2023-07-03"
    assert event["duration_days"] == 3
    assert event["duration_hours"] == 72
    assert pd.Timestamp(event["start_lst"]) == pd.Timestamp("2023-07-01", tz=LST)
    assert pd.Timestamp(event["end_lst"]) == pd.Timestamp("2023-07-04", tz=LST)
    assert event["peak_value"] == 25
    assert report["summary"]["heatwave"] == {"count": 1, "hours": 72, "days": 3}


def test_two_hot_days_do_not_form_a_heatwave():
    report = detect_events(weather_for_days([25, 25], minima=[20, 20]))

    assert events_of_type(report, "heatwave") == []
    assert report["summary"]["heatwave"] == {"count": 0, "hours": 0, "days": 0}


def test_lost_and_false_warm_spells_use_the_same_baseline_percentile():
    reference = weather_for_days([30] * 3 + [20] * 9)
    candidate = weather_for_days([20] * 9 + [30] * 3)
    comparison = compare_events(reference, candidate)

    assert comparison["thresholds"]["relative_warm_daily_tmax_c"] == 30
    assert (
        comparison["candidate"]["thresholds"] == comparison["reference"]["thresholds"]
    )
    counts = comparison["by_type"]["warm_spell"]
    assert counts["reference_count"] == 1
    assert counts["candidate_count"] == 1
    assert counts["matched_reference_count"] == 0
    assert counts["matched_candidate_count"] == 0
    assert counts["lost_reference_count"] == 1
    assert counts["false_candidate_count"] == 1
    assert len(counts["lost_reference_events"]) == 1
    assert len(counts["false_candidate_events"]) == 1
    assert counts["matches"] == []
    assert counts["overlap_hours"] == 0
    assert counts["reference_hours"] == 72
    assert counts["candidate_hours"] == 72


def test_constant_mild_weather_has_explicit_zero_event_counts():
    weather = weather_for_days([20] * 4)
    comparison = compare_events(weather, weather.copy(deep=True))

    assert comparison["reference"]["events"] == []
    assert comparison["candidate"]["events"] == []
    assert comparison["thresholds"]["relative_warm_daily_tmax_c"] is None
    for event_type, counts in comparison["by_type"].items():
        assert counts["reference_count"] == 0, event_type
        assert counts["candidate_count"] == 0, event_type
        assert counts["lost_reference_count"] == 0, event_type
        assert counts["false_candidate_count"] == 0, event_type
        assert counts["overlap_hours"] == 0, event_type
        assert counts["split_reference_count"] == 0, event_type
        assert counts["merged_candidate_count"] == 0, event_type


def test_high_wind_at_the_threshold_forms_contiguous_hourly_episodes():
    weather = weather_for_days([20])
    weather.loc[weather.index[3:6], "wind_speed"] = [10, 11, 10]
    weather.loc[weather.index[7], "wind_speed"] = 10
    report = detect_events(weather)

    winds = events_of_type(report, "high_wind")
    assert len(winds) == 2
    assert [event["duration_hours"] for event in winds] == [3, 1]
    assert pd.Timestamp(winds[0]["start_lst"]) == (
        weather.index[3] - pd.Timedelta(hours=1)
    )
    assert pd.Timestamp(winds[0]["end_lst"]) == weather.index[5]
    assert winds[0]["peak_value"] == 11
    assert report["summary"]["high_wind"]["hours"] == 4


def test_exact_five_degree_jump_is_excluded_and_six_degree_jump_is_detected():
    weather = weather_for_days([15])
    weather["dry_bulb"] = 15.0
    weather.loc[weather.index[5:], "dry_bulb"] = 20.0
    weather.loc[weather.index[6:], "dry_bulb"] = 26.0
    report = detect_events(weather)

    jumps = events_of_type(report, "temperature_jump")
    assert len(jumps) == 1
    assert jumps[0]["duration_hours"] == 1
    assert jumps[0]["peak_value"] == 6
    assert pd.Timestamp(jumps[0]["start_lst"]) == weather.index[5]
    assert pd.Timestamp(jumps[0]["end_lst"]) == weather.index[6]


def test_candidate_warming_keeps_the_reference_percentile_threshold():
    reference = weather_for_days(np.arange(20, 32))
    candidate = reference.copy(deep=True)
    candidate["dry_bulb"] += 20
    comparison = compare_events(reference, candidate)
    independently_calibrated_candidate = detect_events(candidate)

    threshold = comparison["reference"]["thresholds"]["relative_warm_daily_tmax_c"]
    assert threshold == pytest.approx(30.45)
    assert (
        comparison["candidate"]["thresholds"]["relative_warm_daily_tmax_c"] == threshold
    )
    assert independently_calibrated_candidate["thresholds"][
        "relative_warm_daily_tmax_c"
    ] == pytest.approx(threshold + 20)
    assert comparison["by_type"]["warm_spell"]["reference_count"] == 0
    assert comparison["by_type"]["warm_spell"]["candidate_count"] == 1
    assert (
        events_of_type(comparison["candidate"], "warm_spell")[0]["duration_days"] == 12
    )


def test_comparison_rejects_shifted_hourly_alignment():
    reference = weather_for_days([20, 21, 22])
    candidate = reference.copy(deep=True)
    candidate.index = candidate.index + pd.Timedelta(hours=1)

    with pytest.raises(ValueError):
        compare_events(reference, candidate)


def test_epw_calendar_hour_24_uses_location_time_zone_as_fixed_lst():
    weather = weather_for_days([25] * 3, minima=[20] * 3)
    labels = weather.index - pd.Timedelta(hours=1)
    weather["year"] = labels.year
    weather["month"] = labels.month
    weather["day"] = labels.day
    weather["hour"] = labels.hour + 1
    weather = weather.reset_index(drop=True)
    weather.attrs["location"]["time_zone"] = 1
    report = detect_events(weather)

    heatwave = events_of_type(report, "heatwave")[0]
    assert pd.Timestamp(heatwave["start_lst"]) == pd.Timestamp("2023-07-01", tz=LST)
    assert pd.Timestamp(heatwave["end_lst"]) == pd.Timestamp("2023-07-04", tz=LST)
    assert heatwave["duration_days"] == 3
    assert report["diagnostics"]["complete_days"] == 3


def test_three_days_at_minus_ten_form_a_cold_spell():
    weather = weather_for_days([-5] * 3, start="2023-01-01", minima=[-10] * 3)
    report = detect_events(weather)

    cold = events_of_type(report, "cold_spell")
    assert len(cold) == 1
    assert cold[0]["start_date"] == "2023-01-01"
    assert cold[0]["end_date"] == "2023-01-03"
    assert cold[0]["duration_days"] == 3
    assert cold[0]["duration_hours"] == 72
    assert cold[0]["peak_value"] == -10


def test_one_missing_temperature_hour_breaks_an_otherwise_cold_spell():
    weather = weather_for_days([-5] * 5, start="2023-01-01", minima=[-10] * 5)
    weather.loc[weather.index[2 * 24 + 10], "dry_bulb"] = np.nan
    report = detect_events(weather)

    assert events_of_type(report, "cold_spell") == []
    assert report["diagnostics"]["complete_days"] == 4
    assert report["diagnostics"]["incomplete_days"] == ["2023-01-03"]
    assert report["diagnostics"]["missing_temperature_hours"] == 1


@pytest.mark.parametrize("split_reference", [True, False])
def test_high_wind_split_and_merge_preserve_overlap_associations(split_reference):
    unbroken = weather_for_days([20])
    unbroken.loc[unbroken.index[4:12], "wind_speed"] = 10
    broken = unbroken.copy(deep=True)
    broken.loc[broken.index[7:9], "wind_speed"] = 1
    reference, candidate = (unbroken, broken) if split_reference else (broken, unbroken)
    counts = compare_events(reference, candidate)["by_type"]["high_wind"]

    assert counts["reference_count"] == (1 if split_reference else 2)
    assert counts["candidate_count"] == (2 if split_reference else 1)
    assert counts["matched_reference_count"] == counts["reference_count"]
    assert counts["matched_candidate_count"] == counts["candidate_count"]
    assert counts["lost_reference_count"] == 0
    assert counts["false_candidate_count"] == 0
    assert counts["split_reference_count"] == int(split_reference)
    assert counts["merged_candidate_count"] == int(not split_reference)
    assert counts["overlap_hours"] == 6
    assert sorted(match["overlap_hours"] for match in counts["matches"]) == [3, 3]
    assert counts["reference_hours"] == (8 if split_reference else 6)
    assert counts["candidate_hours"] == (6 if split_reference else 8)


def test_nighttime_solar_uses_fixed_lst_interval_geometry():
    weather = weather_for_days([20], start="2023-01-01")
    weather.loc[weather.index[0], "ghi"] = 10
    weather.loc[weather.index[11], "ghi"] = 100
    report = detect_events(weather)

    assert report["diagnostics"]["nighttime_solar_available"] is True
    nighttime = events_of_type(report, "nighttime_solar")
    assert len(nighttime) == 1
    assert nighttime[0]["duration_hours"] == 1
    assert pd.Timestamp(nighttime[0]["start_lst"]) == pd.Timestamp("2023-01-01", tz=LST)
    assert pd.Timestamp(nighttime[0]["end_lst"]) == weather.index[0]


def test_solar_geometry_without_coordinates_is_explicitly_unavailable():
    weather = weather_for_days([20], start="2023-01-01")
    weather.attrs.clear()
    weather.loc[weather.index[0], "ghi"] = 10
    report = detect_events(weather)

    assert report["diagnostics"]["nighttime_solar_available"] is False
    assert events_of_type(report, "nighttime_solar") == []


def test_solar_deviation_requires_more_than_two_hundred_watts():
    reference = weather_for_days([20])
    candidate = reference.copy(deep=True)
    candidate.loc[candidate.index[11:13], "ghi"] = [200, 201]
    report = compare_events(reference, candidate)["candidate"]

    deviations = events_of_type(report, "solar_deviation")
    assert len(deviations) == 1
    assert deviations[0]["duration_hours"] == 1
    assert deviations[0]["peak_value"] == 201
    assert pd.Timestamp(deviations[0]["start_lst"]) == candidate.index[11]
    assert pd.Timestamp(deviations[0]["end_lst"]) == candidate.index[12]
    assert events_of_type(report, "solar_out_of_bounds") == []


def test_solar_bound_guard_accepts_endpoints_and_flags_invalid_values():
    weather = weather_for_days([20])
    weather.loc[weather.index[10:15], "ghi"] = [0, 2000, 2001, -1, np.nan]
    report = detect_events(weather)

    invalid = events_of_type(report, "solar_out_of_bounds")
    assert len(invalid) == 1
    assert invalid[0]["duration_hours"] == 3
    assert invalid[0]["peak_value"] == 2001
    assert pd.Timestamp(invalid[0]["start_lst"]) == weather.index[11]
    assert pd.Timestamp(invalid[0]["end_lst"]) == weather.index[14]


@pytest.mark.parametrize("column", ["source", "dry_bulb_source"])
def test_source_labels_expose_actual_transition_intervals(column):
    weather = weather_for_days([20])
    weather[column] = ["observed"] * 8 + ["reconstructed"] * 8 + ["observed"] * 8
    report = detect_events(weather)

    assert report["diagnostics"]["source_boundary_available"] is True
    boundaries = events_of_type(report, "source_boundary")
    assert len(boundaries) == 2
    assert [event["duration_hours"] for event in boundaries] == [1, 1]
    assert [pd.Timestamp(event["end_lst"]) for event in boundaries] == [
        weather.index[8],
        weather.index[16],
    ]


def test_detection_and_comparison_do_not_mutate_temperature_or_source_inputs():
    reference = weather_for_days([20] * 3)
    reference.loc[reference.index[4], "dry_bulb"] = np.inf
    reference["source"] = ["observed"] * 30 + ["reconstructed"] * 42
    reference.attrs["source_tags"] = reference[["source"]].copy(deep=True)
    candidate = reference.copy(deep=True)
    candidate.loc[candidate.index[30], "dry_bulb"] = np.nan
    reference_before = reference.copy(deep=True)
    candidate_before = candidate.copy(deep=True)
    source_before = reference.attrs["source_tags"].copy(deep=True)

    detect_events(reference)
    compare_events(reference, candidate)

    pd.testing.assert_frame_equal(reference, reference_before)
    pd.testing.assert_frame_equal(candidate, candidate_before)
    pd.testing.assert_frame_equal(reference.attrs["source_tags"], source_before)
