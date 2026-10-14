"""Prevent hidden observations entering predictions through frozen native fills."""

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("smhi2epw")

from examples.weather_imputation_sensitivity import campaign_sources as S
from examples.weather_imputation_sensitivity import weather as W


def original_weather():
    """Create complete annual observations and physically closed hourly radiation."""
    index = pd.date_range("2022-12-31 22:00", "2024-01-01 01:00", freq="h", tz="UTC")
    _, cosine = W.automatic.solar_zenith(index - pd.Timedelta(minutes=30), W.LAT, W.LON)
    dni = np.where(cosine > W.C.COS_ZENITH_FLOOR, 500.0, 0.0)
    ghi = np.maximum(cosine, 0) * dni
    raw = pd.DataFrame(
        {
            "dry_bulb": 12.0,
            "relative_humidity": 60.0,
            "pressure": 1010.0,
            "wind_speed": 3.0,
            "wind_direction": 180.0,
            "cloud_cover": np.nan,
            "ghi": ghi,
            "dni": dni,
            "dirh": ghi,
            "ghi_measured": ghi,
        },
        index=index,
    )
    raw.attrs["year"] = W.YEAR
    return raw


def reference(raw):
    """Construct native fills before the synthetic mask is selected."""
    station = W.I.StationMeta(W.PRIMARY, "Reference", W.LAT, W.LON, 154.0)
    assert S._eligibility(raw)["eligible"]
    return S._reference(raw, station)[0]


def comparison_sources(raw):
    """Create independent complete donor and ERA5 values without hidden truth."""
    donor = original_weather()[W.MET].copy()
    donor["dry_bulb"] = 14.0
    model = original_weather()
    model["dry_bulb"] = 15.0
    model["reanalysis_surface_pressure"] = 99500.0
    return donor, model


@pytest.mark.parametrize(
    "method", ["temporal", "automatic_legacy", "automatic", "donor_assessed"]
)
def test_native_temperature_profile_cannot_reintroduce_hidden_truth(method):
    raw = original_weather()
    raw.loc[raw.index[120:126], "dry_bulb"] = np.nan
    mask = pd.Series(False, index=raw.index)
    mask.iloc[100:112] = True
    altered = raw.copy(deep=True)
    altered.loc[mask, "dry_bulb"] = 30.0
    pd.testing.assert_frame_equal(raw.loc[~mask], altered.loc[~mask])
    baseline, altered_baseline = reference(raw), reference(altered)
    # Establish the adversarial condition: the frozen native fills themselves
    # contain a signal from what will later become hidden observations.
    assert not baseline.dry_bulb.iloc[120:126].equals(
        altered_baseline.dry_bulb.iloc[120:126]
    )
    donor, model = comparison_sources(raw)
    # A partially available donor leaves an eight-hour hole requiring profiles.
    donor.loc[raw.index[100:108], "dry_bulb"] = np.nan
    predicted, decisions = W.reconstruct(
        baseline, raw, donor, model, mask, method, columns=["dry_bulb"]
    )
    changed_prediction, _ = W.reconstruct(
        altered_baseline, altered, donor, model, mask, method, columns=["dry_bulb"]
    )
    pd.testing.assert_series_equal(
        predicted.loc[mask, "dry_bulb"], changed_prediction.loc[mask, "dry_bulb"]
    )
    assert predicted.loc[mask, "dry_bulb"].notna().all()
    pd.testing.assert_frame_equal(predicted.loc[~mask], baseline.loc[~mask])
    pd.testing.assert_frame_equal(
        changed_prediction.loc[~mask], altered_baseline.loc[~mask]
    )
    if method in ("automatic", "donor_assessed"):
        assert decisions["dry_bulb"]["assessments"][0]["transferred_hours"] == 4


@pytest.mark.parametrize("method", ["temporal", "automatic_legacy", "automatic"])
def test_native_solar_profile_cannot_reintroduce_hidden_hybrid_truth(method):
    raw = original_weather()
    start = raw.index.get_loc(pd.Timestamp("2023-06-15 06:00", tz="UTC"))
    mask = pd.Series(False, index=raw.index)
    mask.iloc[start : start + 12] = True
    native = raw.index[start + 24 : start + 30]
    radiation = W.SOLAR + [W.C.METOBS_RADIATION_COLUMN]
    raw.loc[native, radiation] = np.nan
    altered = raw.copy(deep=True)
    altered.loc[mask, radiation] *= 1.8
    pd.testing.assert_frame_equal(raw.loc[~mask], altered.loc[~mask])
    baseline, altered_baseline = reference(raw), reference(altered)
    assert not baseline.loc[native, "ghi"].equals(altered_baseline.loc[native, "ghi"])
    donor, model = comparison_sources(raw)
    predicted, _ = W.reconstruct(
        baseline, raw, donor, model, mask, method, scope="solar"
    )
    changed_prediction, _ = W.reconstruct(
        altered_baseline, altered, donor, model, mask, method, scope="solar"
    )
    pd.testing.assert_frame_equal(
        predicted.loc[mask, W.SOLAR], changed_prediction.loc[mask, W.SOLAR]
    )
    assert predicted.loc[mask, W.SOLAR].notna().all().all()
    pd.testing.assert_frame_equal(predicted.loc[~mask], baseline.loc[~mask])
    pd.testing.assert_frame_equal(
        changed_prediction.loc[~mask], altered_baseline.loc[~mask]
    )
