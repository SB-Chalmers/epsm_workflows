"""Checks for controlled masking, isolated recovery and demand comparisons."""

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("smhi2epw")

from examples.weather_imputation_sensitivity import weather as W
from examples.weather_imputation_sensitivity.anomalies import detect_events
from examples.weather_imputation_sensitivity.run_study import load_comparison


def inputs(index):
    """Construct bounded primary inputs without a network or a building engine."""
    return pd.DataFrame(
        {
            "dry_bulb": 12.0,
            "relative_humidity": 60.0,
            "pressure": 1010.0,
            "wind_speed": 3.0,
            "wind_direction": 180.0,
            "ghi": 0.0,
            "dni": 0.0,
            "dirh": 0.0,
            "cloud_cover": np.nan,
        },
        index=index,
    )


def test_masks_keep_anchors_and_exclude_native_missing_cells():
    index = pd.date_range("2023-01-01", periods=200, freq="h", tz="UTC")
    raw = inputs(index)
    raw.loc[index[19], "dry_bulb"] = np.nan
    anchors = {str(i): index[i] for i in (20, 70, 120, 170)}
    mask, blocks = W.outage_mask(index, raw, anchors, 3)
    assert mask.sum() == 12 and len(blocks) == 4
    assert all(mask.loc[x] for x in anchors.values())
    assert not mask.loc[index[19]]


def test_donor_fit_excludes_all_hidden_and_native_missing_truth(monkeypatch):
    index = pd.date_range("2023-06-01", periods=100, freq="h", tz="UTC")
    reference = inputs(index)
    raw = reference.copy()
    raw.loc[index[12], "dry_bulb"] = np.nan
    donor = reference.copy()
    donor["dry_bulb"] = 14.0
    mask = pd.Series(False, index=index)
    mask.iloc[40:43] = True
    seen = []

    def assess(primary, candidate, column, start, end):
        assert primary[mask].isna().all()
        if column == "dry_bulb":
            assert pd.isna(primary.iloc[12])
        seen.append(column)
        return {"status": "accepted", "offset": -2 if column == "dry_bulb" else 0}

    monkeypatch.setattr(W.G, "_assessment", assess)
    completed, _ = W.reconstruct(
        reference, raw, donor, pd.DataFrame(), mask, "donor_assessed"
    )
    assert set(seen) == set(W.MET)
    pd.testing.assert_frame_equal(reference.loc[~mask], completed.loc[~mask])
    assert completed.loc[mask, "dry_bulb"].eq(12.0).all()


def test_new_and_legacy_automatic_hierarchies_use_the_same_hidden_evidence(monkeypatch):
    index = pd.date_range("2023-06-01", periods=720, freq="h", tz="UTC")
    reference = inputs(index)
    donor = reference.copy()
    donor["dry_bulb"] = 14.0
    donor.loc[index[300:348], "dry_bulb"] = 25.0
    mask = pd.Series(False, index=index)
    mask.iloc[300:348] = True

    def assess(primary, candidate, column, start, end):
        assert primary[mask].isna().all()
        return {"status": "accepted", "offset": -2 if column == "dry_bulb" else 0}

    monkeypatch.setattr(W.G, "_assessment", assess)
    new, _ = W.reconstruct(
        reference, reference, donor, pd.DataFrame(), mask, "automatic"
    )
    old, _ = W.reconstruct(
        reference, reference, donor, pd.DataFrame(), mask, "automatic_legacy"
    )
    assert new.loc[mask, "dry_bulb"].eq(23.0).all()
    assert old.loc[mask, "dry_bulb"].eq(12.0).all()
    pd.testing.assert_frame_equal(new.loc[~mask], old.loc[~mask])


def test_era5_pressure_is_pa_only_inside_outage():
    index = pd.date_range("2023-06-01", periods=100, freq="h", tz="UTC")
    reference = inputs(index)
    model = reference.copy()
    model["reanalysis_surface_pressure"] = 99500.0
    model["dry_bulb"] = 15.0
    mask = pd.Series(False, index=index)
    mask.iloc[40:43] = True
    completed, _ = W.reconstruct(reference, reference, reference, model, mask, "era5")
    assert completed.loc[mask, "pressure"].isna().all()
    assert completed.loc[mask, "reanalysis_surface_pressure"].eq(99500).all()
    assert completed.loc[~mask, "reanalysis_surface_pressure"].isna().all()
    assert completed.loc[~mask, "pressure"].eq(1010).all()


def test_solar_profile_keeps_fully_dark_intervals_zero():
    index = pd.date_range("2023-10-01", periods=14 * 24, freq="h", tz="UTC")
    reference = inputs(index)
    # Neighboring days vary in their daylight peaks. An unguarded endpoint
    # offset can otherwise add positive radiation to a repeated night profile.
    dark = W.automatic._dark_hours(index, W.LAT, W.LON)
    daylight = np.maximum(
        W.automatic.solar_zenith(index - pd.Timedelta(minutes=30), W.LAT, W.LON)[1], 0
    )
    reference["ghi"] = daylight * np.linspace(500, 900, len(index))
    reference["dni"] = np.where(daylight > 0, 300.0, 0.0)
    reference["dirh"] = reference.dni * daylight
    reference.loc[dark, W.SOLAR] = 0.0
    mask = pd.Series(False, index=index)
    mask.iloc[5 * 24 + 12 : 7 * 24 + 12] = True
    completed, _ = W.reconstruct(
        reference, reference, reference, pd.DataFrame(), mask, "temporal", "solar"
    )
    assert completed.loc[dark, "ghi"].eq(0).all()
    assert np.isfinite(completed[W.SOLAR]).all().all()


@pytest.mark.parametrize(
    "column,centre,amplitude,endpoint,limits",
    [
        ("relative_humidity", 70.0, 20.0, 95.0, (0.0, 100.0)),
        ("wind_speed", 6.0, -3.0, 0.1, (0.0, 40.0)),
    ],
)
def test_bounded_observations_can_overshoot_in_endpoint_adjusted_profiles(
    column, centre, amplitude, endpoint, limits
):
    """A humid front or calm spell can offset a valid borrowed daily cycle."""
    index = pd.date_range("2023-06-01", periods=12 * 24, freq="h", tz="UTC")
    observations = pd.Series(
        centre + amplitude * np.cos(2 * np.pi * index.hour / 24),
        index=index,
        name=column,
    )
    # A48-hour noon-to-noon outage spans two daily extrema. Eight-hour gradual
    # transitions reach highhumidity/calm endpoints while keeping every known
    # observation within its physical range. Profile offsets can still overshoot.
    start, end = 4 * 24 + 12, 6 * 24 + 12
    observations.iloc[start - 8 : start] = np.linspace(
        observations.iloc[start - 8], endpoint, 8
    )
    observations.iloc[end : end + 8] = np.linspace(
        endpoint, observations.iloc[end + 7], 8
    )
    observations.iloc[start:end] = np.nan
    missing = observations.isna()
    low, high = limits
    assert observations.dropna().between(low, high).all()

    unguarded, linear, diurnal, longest = W.P._fill_scalar_series(
        observations,
        required=False,
        solar=False,
        short_gap_hours=3,
        max_gap_hours=48,
    )
    assert (linear, diurnal, longest) == (0, 48, 48)
    assert ((unguarded.loc[missing] < low) | (unguarded.loc[missing] > high)).any()

    reconstructed = W.temporal(observations, column)
    assert reconstructed.loc[missing].between(low, high).all()
    assert reconstructed.attrs["bounded_filled_hours"] > 0
    pd.testing.assert_series_equal(
        reconstructed.loc[~missing], observations.loc[~missing]
    )
    assert observations.loc[missing].isna().all()  # Inputs remain untouched.


@pytest.mark.parametrize(
    "column,outlier", [("relative_humidity", 105.0), ("wind_speed", 42.0)]
)
def test_temporal_bounds_do_not_silently_repair_finite_observation_errors(
    column, outlier
):
    """Invalid observed values must remain visible to upstream/export validation."""
    index = pd.date_range("2023-06-01", periods=12 * 24, freq="h", tz="UTC")
    observations = pd.Series(
        60.0 if column == "relative_humidity" else 3.0, index=index, name=column
    )
    observations.iloc[2] = outlier
    observations.iloc[120:123] = np.nan
    reconstructed = W.temporal(observations, column)
    assert reconstructed.iloc[2] == outlier
    assert reconstructed.iloc[120:123].notna().all()
    pd.testing.assert_series_equal(
        reconstructed[observations.notna()], observations.dropna()
    )


def test_zero_load_and_peak_shift_are_not_fabricated():
    index = pd.date_range("2022-12-31 22:00", periods=8764, freq="h", tz="UTC")
    hours = pd.date_range("2023-01-01 01:00", periods=8760, freq="h", tz="Etc/GMT-1")
    hourly = pd.DataFrame(
        {"lst_hour_ending": hours.astype(str), "heating_kw": 0.0, "cooling_kw": 0.0}
    )
    summary = {
        "annual_heating_kwh": 0.0,
        "annual_cooling_kwh": 0.0,
        "peak_heating_kw": 0.0,
        "peak_cooling_kw": 0.0,
        "peak_heating_hours_lst": [],
        "peak_cooling_hours_lst": [],
    }
    result = load_comparison(
        hourly, hourly, summary, summary, pd.Series(False, index=index)
    )
    assert result["delta_annual_heating_pct"] is None
    assert result["delta_peak_cooling_pct"] is None
    assert result["same_heating_peak_hours"] is None
    assert result["nearest_cooling_peak_shift_h"] is None


def test_physical_solar_events_on_exported_calendar_use_fixed_lst():
    index = pd.date_range("2023-01-01 01:00", periods=48, freq="h", tz="Etc/GMT-1")
    frame = inputs(index)
    frame.attrs["location"] = {"latitude": W.LAT, "longitude": W.LON, "time_zone": 1}
    assert detect_events(frame)["summary"]["nighttime_solar"]["hours"] == 0


@pytest.mark.parametrize("method", ["era5", "erbs"])
def test_forced_solar_without_measured_ghi_matches_empty_optional_column(method):
    index = pd.date_range("2023-06-01", periods=72, freq="h", tz="UTC")
    reference = inputs(index)
    cosine = np.maximum(
        W.automatic.solar_zenith(index - pd.Timedelta(minutes=30), W.LAT, W.LON)[1], 0
    )
    reference["ghi"] = 400 * cosine
    reference["dni"] = np.where(cosine > 0, 180.0, 0.0)
    reference["dirh"] = reference["dni"] * cosine
    model = reference.copy()
    model["ghi"] = 600 * cosine
    mask = pd.Series(False, index=index)
    mask.iloc[10:13] = True
    before = reference.copy(deep=True)
    with_optional = reference.assign(**{W.C.METOBS_RADIATION_COLUMN: np.nan})
    actual, _ = W.reconstruct(
        reference, reference, reference, model, mask, method, "solar"
    )
    expected, _ = W.reconstruct(
        with_optional, with_optional, with_optional, model, mask, method, "solar"
    )
    pd.testing.assert_frame_equal(
        actual, expected.drop(columns=W.C.METOBS_RADIATION_COLUMN)
    )
    pd.testing.assert_frame_equal(actual.loc[~mask], reference.loc[~mask])
    pd.testing.assert_frame_equal(reference, before)
    assert np.isfinite(actual.loc[mask, W.SOLAR]).all().all()
    assert W.C.METOBS_RADIATION_COLUMN not in actual
    if method == "era5":
        assert not np.allclose(actual.loc[mask, "ghi"], reference.loc[mask, "ghi"])
