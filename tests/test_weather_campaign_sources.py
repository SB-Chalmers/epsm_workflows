"""Verify source eligibility, original truth isolation and immutable resumption."""

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("smhi2epw")

from examples.weather_imputation_sensitivity import campaign_sources as S


@pytest.fixture
def sources(monkeypatch, tmp_path):
    """Supply complete annual observations with deterministic comparison sources."""
    year = 2023
    index = pd.date_range("2022-12-31 22:00", "2024-01-01 01:00", freq="h", tz="UTC")
    raw = pd.DataFrame(
        {
            "dry_bulb": 12.0,
            "relative_humidity": 60.0,
            "pressure": 1010.0,
            "wind_speed": 3.0,
            "wind_direction": 180.0,
            "cloud_cover": np.nan,
            "ghi": 0.0,
            "dni": 0.0,
            "dirh": 0.0,
            "ghi_measured": 0.0,
        },
        index=index,
    )
    site = {
        "site_id": "test",
        "name": "Test city",
        "latitude": 59.0,
        "longitude": 15.0,
        "station_id": 1,
        "donor_station_id": 2,
        "radiation_station_id": 3,
    }
    station = S.I.StationMeta(1, "Test airport", 60.0, 15.0, 100.0)
    catalogs = {
        p: {1: (60.0, 15.0, 0, 9999999999999), 2: (60.1, 15.0, 0, 9999999999999)}
        for p in S.C.METOBS_REQUIRED_PARAMETERS
    }
    catalogs[11] = {3: (60.0, 15.0, 0, 9999999999999)}
    calls = []

    def metadata(sid, client, year=None, *, parameter=1):
        """Resolve historical metadata independently of city query coordinates."""
        return (
            station
            if sid == 1
            else S.I.StationMeta(
                sid, "Comparison", 60.1 if sid == 2 else 60.0, 15.0, 100.0
            )
        )

    def ingest(meta, received_year, client, **kwargs):
        """Record acquisition coordinates and preserve raw native missing cells."""
        calls.append(("ingest", meta, received_year, kwargs))
        return raw.copy(deep=True)

    def fetch_donor(sid, parameter, column, window, client, **kwargs):
        """Return original donor observations without filling missing hours."""
        calls.append(("donor", parameter, kwargs))
        return pd.Series(14.0 if column == "dry_bulb" else raw[column], index=index)

    def fetch_model(lat, lon, elevation, received_index, client):
        """Return independent surface-pressure units and record requested location."""
        calls.append(("model", lat, lon, elevation))
        model = raw.copy()
        model["dry_bulb"] = 25.0
        model["reanalysis_surface_pressure"] = 99900.0
        return model, {"model": "ERA5", "elevation_m": elevation}

    monkeypatch.setattr(
        S.I, "_stations_for_parameter", lambda parameter, client: catalogs[parameter]
    )
    monkeypatch.setattr(S.I, "get_station_metadata", metadata)
    monkeypatch.setattr(S.I, "ingest", ingest)
    monkeypatch.setattr(S.I, "fetch_metobs_parameter", fetch_donor)
    monkeypatch.setattr(S.reanalysis, "fetch", fetch_model)
    monkeypatch.setattr(
        S.G,
        "recover",
        lambda *a, **kw: pytest.fail("Donors must never construct reference truth"),
    )
    return raw, site, year, catalogs, calls, tmp_path


def test_primary_coordinates_and_native_missing_truth_are_preserved(sources):
    raw, site, year, _, calls, root = sources
    raw.iloc[100:106, raw.columns.get_loc("dry_bulb")] = np.nan
    original, reference, donor, model, receipt = S.prepare_location_year(
        root / "pair", site, year, root / "shared"
    )
    assert original.dry_bulb.iloc[100:106].isna().all()
    assert reference.dry_bulb.iloc[100:106].eq(12).all()
    assert donor.dry_bulb.eq(14).all()
    assert model.reanalysis_surface_pressure.eq(99900).all()
    assert receipt["latitude"] == 60.0 != site["latitude"]
    assert receipt["city_query"]["latitude"] == site["latitude"]
    assert calls[0][3]["solar_latitude"] == 60.0
    assert ("model", 60.0, 15.0, 100.0) in calls
    assert receipt["baseline_processing"]["diurnal_filled_hours"]["dry_bulb"] == 6
    assert not receipt["baseline_processing"]["cross_station_filled_hours"]
    assert not receipt["baseline_processing"]["reanalysis_filled_hours"]
    assert receipt["original_missing_hours"]["dry_bulb"] == 6


def test_year_station_choice_preserves_declared_plan_and_sampling_identity():
    """Selecting one declared year leaves other years and the input plan intact."""
    plan = {
        "site_id": "region",
        "latitude": 59.0,
        "longitude": 15.0,
        "station_id": 1,
        "donor_station_id": 2,
        "radiation_station_id": 3,
        "year_station_overrides": {
            "2023": {
                "station_id": 2,
                "donor_station_id": 1,
                "radiation_station_id": None,
            }
        },
    }
    original = json.loads(json.dumps(plan))
    selected = S.station_choice(plan, 2023)
    assert selected == {
        "site_id": "region",
        "latitude": 59.0,
        "longitude": 15.0,
        "station_id": 2,
        "donor_station_id": 1,
        "radiation_station_id": None,
    }
    selected["station_id"] = 99
    assert S.station_choice(plan, 2021)["station_id"] == 1
    assert plan == original


def test_effective_year_station_controls_acquisition_and_immutable_resume(sources):
    """Only the chosen year's effective source configuration controls its receipt."""
    _, site, year, _, calls, root = sources
    plan = site | {
        "year_station_overrides": {str(year): {"station_id": 2, "donor_station_id": 1}}
    }
    original = json.loads(json.dumps(plan))
    first = S.prepare_location_year(root / "pair", plan, year, root / "shared")
    receipt = first[-1]
    assert receipt["station"]["station_id"] == 2
    assert receipt["request"]["location"]["station_id"] == 2
    assert "year_station_overrides" not in receipt["request"]["location"]
    assert receipt["city_query"]["latitude"] == site["latitude"]
    assert calls[0][1].station_id == 2
    assert plan == original
    acquired = list(calls)
    unrelated_year = plan | {
        "year_station_overrides": plan["year_station_overrides"]
        | {"2018": {"station_id": 99}}
    }
    resumed = S.prepare_location_year(
        root / "pair", unrelated_year, year, root / "shared"
    )
    assert resumed[-1] == receipt
    assert calls == acquired
    changed_effective = plan | {
        "year_station_overrides": {str(year): {"station_id": 1}}
    }
    with pytest.raises(ValueError, match="configuration"):
        S.prepare_location_year(root / "pair", changed_effective, year, root / "shared")


@pytest.mark.parametrize("invalid", [np.inf, 100.0])
def test_invalid_primary_samples_are_missing_before_reference_construction(
    sources, invalid
):
    raw, site, year, _, _, root = sources
    raw.loc[raw.index[100], "dry_bulb"] = invalid
    original, reference, *_ = S.prepare_location_year(
        root / "pair", site, year, root / "shared"
    )
    assert pd.isna(original.dry_bulb.iloc[100])
    assert reference.dry_bulb.iloc[100] == 12.0


@pytest.mark.parametrize("violation", ["long_gap", "fraction"])
def test_unsupported_reference_is_recorded_without_donor_or_model_truth(
    sources, violation
):
    raw, site, year, _, calls, root = sources
    if violation == "long_gap":
        raw.iloc[100:149, raw.columns.get_loc("dry_bulb")] = np.nan
    else:
        for start in range(100, 8500, 50):
            raw.loc[raw.index[start : start + 2], S.MET] = np.nan
    with pytest.raises(S.SourceUnavailable):
        S.prepare_location_year(root / "pair", site, year, root / "shared")
    receipt = json.loads((root / "pair/sources/receipt.json").read_text())
    assert receipt["status"] == "unavailable" and receipt["reason"]
    assert not receipt["eligible"]
    assert "primary" in receipt["frame_sha256"]
    assert all(call[0] == "ingest" for call in calls)
    previous = list(calls)
    with pytest.raises(S.SourceUnavailable):
        S.prepare_location_year(root / "pair", site, year, root / "shared")
    assert calls == previous


def test_partial_donor_parameters_remain_missing_and_explicit(sources):
    _, site, year, catalogs, _, root = sources
    catalogs[9].pop(2)
    _, _, donor, _, receipt = S.prepare_location_year(
        root / "pair", site, year, root / "shared"
    )
    assert donor.pressure.isna().all()
    assert donor.dry_bulb.notna().all()
    assert receipt["donor"]["pressure"]["status"] == "unavailable"
    assert "metadata" in receipt["donor"]["pressure"]["reason"]
    assert "exclude synthetic masks" in receipt["donor"]["dry_bulb"]["assessment"]


def test_primary_station_cannot_leak_hidden_truth_as_its_own_donor(sources):
    _, site, year, _, calls, root = sources
    _, _, donor, _, receipt = S.prepare_location_year(
        root / "pair", site | {"donor_station_id": 1}, year, root / "shared"
    )
    assert donor.isna().all().all()
    assert not any(call[0] == "donor" for call in calls)
    assert all("independent" in value["reason"] for value in receipt["donor"].values())


def test_confirmed_canonical_clouds_are_never_converted_twice(sources):
    raw, site, year, _, _, root = sources
    raw["cloud_cover"] = 8.0
    raw.attrs.update(cloud_source_unit="procent", cloud_output_unit="octas")
    original, reference, _, _, receipt = S.prepare_location_year(
        root / "pair", site, year, root / "shared"
    )
    assert original.cloud_cover.eq(8).all()
    assert reference.cloud_cover.eq(8).all()
    assert receipt["cloud_units"] == {
        "source": "procent",
        "output": "octas",
        "status": "canonical",
    }


def test_unknown_cloud_units_are_explicit_optional_gaps(sources):
    raw, site, year, _, _, root = sources
    raw["cloud_cover"] = 100.0
    raw.attrs.update(cloud_source_unit="procent", cloud_output_unit="procent")
    original, reference, _, _, receipt = S.prepare_location_year(
        root / "pair", site, year, root / "shared"
    )
    assert original.cloud_cover.isna().all()
    assert reference.cloud_cover.isna().all()
    assert receipt["cloud_units"]["status"] == "unavailable"
    assert receipt["ingestion_failures"][0]["code"] == "cloud_unit_unconfirmed"


def test_comparison_model_failure_does_not_disqualify_observation_reference(
    sources, monkeypatch
):
    _, site, year, _, _, root = sources

    def unavailable(*args):
        """Expose missing comparison model data without inventing predictions."""
        raise S.IngestionError("Archive unavailable")

    monkeypatch.setattr(S.reanalysis, "fetch", unavailable)
    _, reference, _, model, receipt = S.prepare_location_year(
        root / "pair", site, year, root / "shared"
    )
    assert reference[S.MET].notna().all().all()
    assert model.isna().all().all()
    assert receipt["status"] == "ready"
    assert receipt["reanalysis"]["status"] == "unavailable"


def test_resume_is_offline_unchanged_and_detects_tampered_frames(sources, monkeypatch):
    _, site, year, _, calls, root = sources
    expected = S.prepare_location_year(root / "pair", site, year, root / "shared")
    count = len(calls)
    monkeypatch.setattr(
        S.I, "ingest", lambda *a, **kw: pytest.fail("Resume must be offline")
    )
    loaded = S.prepare_location_year(root / "pair", site, year, root / "shared")
    assert len(calls) == count
    pd.testing.assert_frame_equal(loaded[0], expected[0], check_freq=False)
    assert loaded[1].attrs["automatic_prepared"]
    assert loaded[4]["frame_sha256"] == expected[4]["frame_sha256"]
    path = root / "pair/sources/primary-hourly.csv"
    path.write_text(path.read_text().replace("12.0", "13.0", 1))
    with pytest.raises(ValueError, match="Pinned source frame changed"):
        S.prepare_location_year(root / "pair", site, year, root / "shared")


def test_resume_rejects_changed_source_configuration(sources):
    _, site, year, _, _, root = sources
    S.prepare_location_year(root / "pair", site, year, root / "shared")
    with pytest.raises(ValueError, match="configuration changed"):
        S.prepare_location_year(
            root / "pair", site | {"station_id": 99}, year, root / "shared"
        )


def test_malformed_cached_json_is_never_replaced(tmp_path):
    url = "https://example.invalid/immutable.json"
    cache = S.ImmutableClient(cache_dir=str(tmp_path), max_retries=0)
    path = Path(cache._cache_path(url, "json"))
    path.write_text("malformed cached response")
    original = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(S.IngestionError, match="immutable JSON"):
        cache.get_json(url)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original


def test_interrupted_receipt_publication_preserves_previous_complete_bytes(
    tmp_path, monkeypatch
):
    path = tmp_path / "receipt.json"
    S._dump(path, {"version": 1})
    previous = path.read_bytes()

    def interrupted(*args):
        """Simulate interruption immediately before atomic receipt publication."""
        raise OSError("Interrupted publication")

    monkeypatch.setattr(Path, "replace", interrupted)
    with pytest.raises(OSError, match="Interrupted"):
        S._dump(path, {"version": 2})
    assert path.read_bytes() == previous
    assert list(tmp_path.iterdir()) == [path]


def test_resume_detects_changed_consumed_raw_cache(sources, monkeypatch):
    _, site, year, _, _, root = sources
    cache_file = root / "cache-entry.json"
    cache_file.write_text('{"value": 1}')
    monkeypatch.setattr(
        S,
        "_responses",
        lambda client: [
            {
                "url": "https://example.invalid/source",
                "cache_path": str(cache_file),
                "cache_file_sha256": S._sha(cache_file),
            }
        ],
    )
    S.prepare_location_year(root / "pair", site, year, root / "shared")
    cache_file.write_text('{"value": 2}')
    with pytest.raises(ValueError, match="Pinned raw response changed"):
        S.prepare_location_year(root / "pair", site, year, root / "shared")
