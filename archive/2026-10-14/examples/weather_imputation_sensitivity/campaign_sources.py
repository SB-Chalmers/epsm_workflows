"""Pin observation-supported weather sources for the six-location campaign.

City coordinates identify the geographical sampling stratum. Weather and
building coordinates follow the primary station's year-specific metadata, so
airport observations are not presented as observations at a city centre.
Reference weather uses only bounded native reconstruction. Donor assessment
belongs to the subsequent masked experiment, where hidden truth is excluded.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import asdict
from pathlib import Path
from threading import Lock

import numpy as np
import pandas as pd
from smhi2epw import automatic, export, reanalysis
from smhi2epw import constants as C
from smhi2epw import gap_recovery as G
from smhi2epw import ingestion as I
from smhi2epw import processing as P
from smhi2epw.errors import DataGapError, IngestionError

YEARS = (2018, 2021, 2023)
OFFSET = 1
MAX_NATIVE_FRACTION = 0.01
MAX_DONOR_DISTANCE_KM = 75.0
MAX_RADIATION_DISTANCE_KM = 50.0
MET = list(C.METOBS_REQUIRED_PARAMETERS.values())
FRAME_NAMES = ("primary", "reference", "donor", "era5")
_CACHE_LOCK = Lock()
_RESPONSE_LOCKS = {}
SITES = (
    {
        "site_id": "malmo",
        "name": "Malmö",
        "latitude": 55.605,
        "longitude": 13.0038,
        "station_id": 53300,
        "donor_station_id": 52240,
        "radiation_station_id": 53445,
        "year_station_overrides": {
            "2018": {
                "station_id": 62040,
                "donor_station_id": 52240,
                "radiation_station_id": 53445,
            },
            "2021": {
                "station_id": 54290,
                "donor_station_id": 53530,
                "radiation_station_id": None,
            },
            "2023": {
                "station_id": 52240,
                "donor_station_id": 52350,
                "radiation_station_id": 53445,
            },
        },
    },
    {
        "site_id": "gothenburg",
        "name": "Gothenburg",
        "latitude": 57.7089,
        "longitude": 11.9746,
        "station_id": 72420,
        "donor_station_id": 71420,
        "radiation_station_id": 71415,
        "year_station_overrides": {
            "2018": {
                "station_id": 72420,
                "donor_station_id": 71420,
                "radiation_station_id": 71415,
            },
            "2021": {
                "station_id": 72420,
                "donor_station_id": 71420,
                "radiation_station_id": 71415,
            },
            "2023": {
                "station_id": 72420,
                "donor_station_id": 71420,
                "radiation_station_id": 71415,
            },
        },
    },
    {
        "site_id": "stockholm",
        "name": "Stockholm",
        "latitude": 59.3293,
        "longitude": 18.0686,
        "station_id": 97200,
        "donor_station_id": 97400,
        "radiation_station_id": 98735,
        "year_station_overrides": {
            "2018": {
                "station_id": 97400,
                "donor_station_id": 97200,
                "radiation_station_id": 98735,
            },
            "2021": {
                "station_id": 97400,
                "donor_station_id": 97200,
                "radiation_station_id": 98735,
            },
            "2023": {
                "station_id": 97400,
                "donor_station_id": 97200,
                "radiation_station_id": 98735,
            },
        },
    },
    {
        "site_id": "ostersund",
        "name": "Östersund",
        "latitude": 63.1792,
        "longitude": 14.6357,
        "station_id": 134110,
        "donor_station_id": 134410,
        "radiation_station_id": 134615,
        "year_station_overrides": {
            "2018": {
                "station_id": 124300,
                "donor_station_id": 124030,
                "radiation_station_id": None,
            },
            "2021": {
                "station_id": 124300,
                "donor_station_id": 124030,
                "radiation_station_id": None,
            },
            "2023": {
                "station_id": 136090,
                "donor_station_id": 136410,
                "radiation_station_id": None,
            },
        },
    },
    {
        "site_id": "lulea",
        "name": "Luleå",
        "latitude": 65.5848,
        "longitude": 22.1567,
        "station_id": 162860,
        "donor_station_id": 161940,
        "radiation_station_id": 162015,
        "year_station_overrides": {
            "2018": {
                "station_id": 162860,
                "donor_station_id": 161940,
                "radiation_station_id": 162015,
            },
            "2021": {
                "station_id": 162860,
                "donor_station_id": 161940,
                "radiation_station_id": 162015,
            },
            "2023": {
                "station_id": 162860,
                "donor_station_id": 161940,
                "radiation_station_id": 162015,
            },
        },
    },
    {
        "site_id": "kiruna",
        "name": "Kiruna",
        "latitude": 67.8558,
        "longitude": 20.2253,
        "station_id": 180940,
        "donor_station_id": 179960,
        "radiation_station_id": 180025,
        "year_station_overrides": {
            "2018": {
                "station_id": 180940,
                "donor_station_id": 179960,
                "radiation_station_id": 180025,
            },
            "2021": {
                "station_id": 179960,
                "donor_station_id": 180940,
                "radiation_station_id": 178985,
            },
            "2023": {
                "station_id": 180760,
                "donor_station_id": 180770,
                "radiation_station_id": None,
            },
        },
    },
)


class SourceUnavailable(ValueError):
    """Describe an explicitly recorded unavailable campaign source combination."""


class ImmutableClient(I.CachedClient):
    """Reuse cached responses without repairing or replacing malformed JSON."""

    def get_text(self, url, suffix="txt"):
        """Serialize identical cache requests across this campaign's worker threads."""
        key = self._cache_path(url, suffix) or (url, suffix)
        with _CACHE_LOCK:
            lock = _RESPONSE_LOCKS.setdefault(key, Lock())
        with lock:
            return super().get_text(url, suffix=suffix)

    def get_json(self, url):
        """Decode exact cached parser input or report its invalidity."""
        try:
            return json.loads(self.get_text(url, suffix="json"))
        except json.JSONDecodeError as exc:
            raise IngestionError(f"Invalid immutable JSON response: {url}") from exc


def _dump(path, value):
    """Publish complete human-readable receipts without nonfinite JSON numbers."""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}-",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(
                value,
                handle,
                indent=2,
                ensure_ascii=False,
                default=str,
                allow_nan=False,
            )
            handle.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _sha(path):
    """Fingerprint exact bytes of a persisted campaign artifact."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _request(location, year):
    """Identify inputs and eligibility rules required for unchanged resumption."""
    return {
        "location": dict(location),
        "year": int(year),
        "utc_offset": OFFSET,
        "accepted_quality": sorted(C.METOBS_ACCEPTED_QUALITY),
        "max_native_required_fraction": MAX_NATIVE_FRACTION,
        "max_native_gap_hours": C.MAX_GAP_HOURS,
        "max_donor_distance_km": MAX_DONOR_DISTANCE_KM,
        "max_radiation_distance_km": MAX_RADIATION_DISTANCE_KM,
        "cloud_unit_policy": "Use recorded canonical octas; never convert again in campaign",
    }


def station_choice(location, year):
    """Resolve declared yearly stations while preserving the regional sampling label.

    Choices are made from original-source coverage before simulation, never from
    building response. Exact station coordinates remain the weather location;
    changing stations between years prevents causal inter-year climate claims.
    """
    selected = dict(location)
    overrides = selected.pop("year_station_overrides", {})
    selected.update(overrides.get(str(year), {}))
    return selected


def _responses(client):
    """Record consumed response hashes and immutable shared-cache file paths."""
    result = []
    for entry in client.response_receipts.values():
        record = dict(entry)
        path = client._cache_path(record["url"], record["representation"])
        if path is not None and Path(path).exists():
            record["cache_path"] = str(Path(path).resolve())
            record["cache_file_sha256"] = _sha(path)
        result.append(record)
    return sorted(result, key=lambda item: (item["url"], item["sha256"]))


def _load(sources, request):
    """Verify pinned inputs and restore physical-processing frame attributes."""
    receipt = json.loads((sources / "receipt.json").read_text(encoding="utf-8"))
    if receipt["request"] != request:
        raise ValueError("Pinned source configuration changed; use a new source folder")
    for response in receipt.get("raw_responses", []):
        if "cache_path" in response and (
            not Path(response["cache_path"]).exists()
            or _sha(response["cache_path"]) != response["cache_file_sha256"]
        ):
            raise ValueError(f"Pinned raw response changed: {response['url']}")
    for name, expected in receipt.get("frame_sha256", {}).items():
        path = sources / f"{name}-hourly.csv"
        if not path.exists() or _sha(path) != expected:
            raise ValueError(f"Pinned source frame changed: {name}")
    if receipt["status"] != "ready":
        raise SourceUnavailable(receipt["reason"])
    frames = []
    for name in FRAME_NAMES:
        frame = pd.read_csv(
            sources / f"{name}-hourly.csv", index_col=0, float_precision="round_trip"
        )
        frame.index = pd.to_datetime(frame.index, utc=True)
        frame.index.name = None
        frames.append(frame)
    frames[0].attrs["source_failures"] = receipt["ingestion_failures"]
    frames[0].attrs["year"] = receipt["year"]
    frames[0].attrs.update(receipt.get("raw_unit_attrs", {}))
    frames[1].attrs.update(receipt["reference_attrs"])
    return *frames, receipt


def _eligibility(raw):
    """Measure native gaps on exported hours without treating filled cells as truth."""
    annual = export.shift_to_lst(raw[MET], int(raw.attrs["year"]), OFFSET)
    missing = {column: int(annual[column].isna().sum()) for column in MET}
    longest = {
        column: P._max_gap_length(raw[column].isna().to_numpy()) for column in MET
    }
    fraction = sum(missing.values()) / (len(annual) * len(MET))
    quality = {
        "original_missing_hours": missing,
        "original_max_gap_hours": longest,
        "native_required_fraction": fraction,
        "fraction_grid": "exported Local Standard Time year",
        "gap_grid": "buffered hourly UTC inputs",
        "eligible": True,
        "eligibility_reason": None,
    }
    if any(hours > C.MAX_GAP_HOURS for hours in longest.values()):
        quality.update(
            eligible=False,
            eligibility_reason=f"Original required observations contain a gap longer than {C.MAX_GAP_HOURS}h: {longest}",
        )
    elif fraction > MAX_NATIVE_FRACTION:
        quality.update(
            eligible=False,
            eligibility_reason=f"Native missing required cells are {fraction:.2%}, exceeding the {MAX_NATIVE_FRACTION:.0%} reference limit",
        )
    return quality


def _reference(raw, station):
    """Construct complete reference inputs using bounded native filling only."""
    frame = raw.copy(deep=True)
    report = P.ProcessingReport()
    report.primary_missing_hours = {
        column: int(raw[column].isna().sum()) for column in MET
    }
    report.required_reconstructed_fraction = sum(
        report.primary_missing_hours.values()
    ) / (len(raw) * len(MET))
    P.impute(frame, MET, list(C.METOBS_OPTIONAL_PARAMETERS.values()), report=report)
    if any(G.valid_observations(frame[column], column).isna().any() for column in MET):
        raise SourceUnavailable(
            "Native reconstruction produced invalid required weather"
        )
    ghi, measured, _, tags = automatic._solar_inputs(
        frame, report, station.latitude, station.longitude
    )
    if ghi.isna().any() or not ghi.between(0, automatic.MAX_SOLAR).all():
        raise SourceUnavailable(
            "Reference solar GHI requires more than bounded native filling"
        )
    automatic._finish_solar(
        frame,
        ghi,
        measured,
        pd.DataFrame({"ghi": tags}, index=frame.index),
        station.latitude,
        station.longitude,
        report,
    )
    frame.attrs["automatic_prepared"] = True
    report.solar_source = frame.attrs["automatic_solar_source"]
    report.clamped_dni_hours = int(frame.attrs["automatic_clamped_dni_hours"])
    surface = P.pressure_at_elevation(
        frame.pressure * 100, frame.dry_bulb, station.latitude, station.elevation
    )
    if (
        not np.isfinite(surface).all()
        or not pd.Series(surface).between(31000, 120000).all()
    ):
        raise SourceUnavailable("Reference QFF implies invalid target surface pressure")
    return frame, report


def _cloud_units(raw):
    """Require recorded canonical cloud units without converting twice downstream."""
    source = raw.attrs.get("cloud_source_unit")
    output = raw.attrs.get("cloud_output_unit")
    units = {"source": source, "output": output, "status": "unavailable"}
    if "cloud_cover" not in raw or raw.cloud_cover.isna().all():
        units["reason"] = "No original cloud observations in requested input grid"
        return units
    if output != "octas":
        raw["cloud_cover"] = np.nan
        units["reason"] = (
            "Unconfirmed canonical cloud output unit; optional observations excluded"
        )
        raw.attrs.setdefault("source_failures", []).append(
            {
                "code": "cloud_unit_unconfirmed",
                "message": units["reason"],
                "variable": "cloud_cover",
            }
        )
        return units
    raw["cloud_cover"] = raw.cloud_cover.where(
        np.isfinite(raw.cloud_cover) & raw.cloud_cover.between(0, 8)
    )
    units["status"] = "canonical"
    return units


def _radiation(location, station, year, catalog, client):
    """Resolve measured GHI when historical coverage and distance permit use."""
    sid = location.get("radiation_station_id")
    if sid is None:
        return None, {"status": "unavailable", "reason": "No fixed radiation station"}
    record = catalog.get(int(sid))
    if record is None or not I._covers_year(record, year):
        return None, {
            "status": "unavailable",
            "reason": "No full-year measured GHI metadata",
        }
    try:
        meta = I.get_station_metadata(int(sid), client, year=year, parameter=11)
        distance = meta.distance_km(station.latitude, station.longitude)
        if distance > MAX_RADIATION_DISTANCE_KM:
            return None, {
                "status": "unavailable",
                "reason": "Measured GHI outside 50km",
                "distance_km": distance,
            }
    except IngestionError as exc:
        return None, {"status": "unavailable", "reason": str(exc)}
    return int(sid), {
        "status": "selected",
        "station": asdict(meta),
        "distance_km": distance,
    }


def _donor(raw, location, station, year, catalogs, client):
    """Fetch a fixed comparison donor without approving transfers on hidden truth."""
    frame = pd.DataFrame(np.nan, index=raw.index, columns=MET)
    metadata = {}
    sid = location.get("donor_station_id")
    for parameter, column in C.METOBS_REQUIRED_PARAMETERS.items():
        entry = {"station_id": sid, "status": "unavailable"}
        metadata[column] = entry
        if sid == station.station_id:
            entry["reason"] = (
                "Primary observations cannot be an independent comparison donor"
            )
            continue
        record = catalogs[parameter].get(int(sid)) if sid is not None else None
        if record is None or not I._covers_year(record, year):
            entry["reason"] = "Fixed donor has no full-year parameter metadata"
            continue
        try:
            meta = I.get_station_metadata(
                int(sid), client, year=year, parameter=parameter
            )
            distance = meta.distance_km(station.latitude, station.longitude)
            entry.update(asdict(meta), distance_km=distance)
            if distance > MAX_DONOR_DISTANCE_KM:
                entry["reason"] = "Fixed donor outside 75km at year-specific position"
                continue
            observed = I.fetch_metobs_parameter(
                int(sid),
                parameter,
                column,
                (raw.index[0], raw.index[-1]),
                client,
                accepted_quality=C.METOBS_ACCEPTED_QUALITY,
            )
            frame[column] = G.hourly_observations(observed, column).reindex(raw.index)
            overlap = raw[column].notna() & frame[column].notna()
            difference = G._differences(
                frame.loc[overlap, column], raw.loc[overlap, column], column
            )
            entry.update(
                status="available" if frame[column].notna().any() else "unavailable",
                original_missing_hours=int(frame[column].isna().sum()),
                original_max_gap_hours=P._max_gap_length(
                    frame[column].isna().to_numpy()
                ),
                original_overlap_hours=int(overlap.sum()),
                original_overlap_mae=float(difference.abs().mean())
                if overlap.any()
                else None,
                original_overlap_mean_difference=float(difference.mean())
                if overlap.any()
                else None,
                assessment="descriptive only; production gap assessment must exclude synthetic masks",
            )
            if entry["status"] == "unavailable":
                entry["reason"] = "No valid quality-filtered same-year observations"
        except IngestionError as exc:
            entry["reason"] = str(exc)
    return frame, metadata


def prepare_location_year(root, location_record, year, cache_dir):
    """Pin eligible primary weather and comparison sources for one site-year.

    ``root`` identifies the site-year output folder; ``cache_dir`` is shared
    across the eighteen combinations. Existing receipts are verified and
    reused without network access. An unavailable reference is saved as an
    explicit receipt and raises ``SourceUnavailable``. Donor or ERA5 outages
    remain visible as missing comparison data rather than becoming truth.
    """
    root, cache_dir = Path(root), Path(cache_dir).resolve()
    location_record = station_choice(location_record, year)
    sources = root / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    request = _request(location_record, year)
    if (sources / "receipt.json").exists():
        return _load(sources, request)
    client = ImmutableClient(
        cache_dir=str(cache_dir), timeout=120, refresh=False, cache_ttl=None
    ).scoped()
    receipt = {
        "request": request,
        "status": "unavailable",
        "year": int(year),
        "location": dict(location_record),
    }
    frames = {}
    try:
        catalogs = {
            parameter: I._stations_for_parameter(parameter, client)
            for parameter in C.METOBS_REQUIRED_PARAMETERS
        }
        sid = int(location_record["station_id"])
        for parameter, catalog in catalogs.items():
            record = catalog.get(sid)
            if record is None or not all(
                I._covers_year(record, item) for item in YEARS
            ):
                raise SourceUnavailable(
                    f"Fixed primary {sid} lacks parameter {parameter} metadata across {YEARS}"
                )
            if not I._covers_year(record, year):
                raise SourceUnavailable(
                    f"Fixed primary {sid} lacks parameter {parameter} metadata for {year}"
                )
        station = I.get_station_metadata(sid, client, year=year)
        receipt.update(
            station=asdict(station),
            latitude=station.latitude,
            longitude=station.longitude,
            utc_offset=OFFSET,
            city_query={
                "latitude": location_record["latitude"],
                "longitude": location_record["longitude"],
                "distance_km": station.distance_km(
                    location_record["latitude"], location_record["longitude"]
                ),
            },
            coordinate_policy="Year-specific primary station coordinates; city coordinates identify the geographic stratum only",
        )
        try:
            radiation_catalog = I._stations_for_parameter(11, client)
        except IngestionError:
            radiation_catalog = {}
        radiation_id, radiation_metadata = _radiation(
            location_record, station, year, radiation_catalog, client
        )
        raw = I.ingest(
            station,
            year,
            client,
            utc_offset=OFFSET,
            accepted_quality=C.METOBS_ACCEPTED_QUALITY,
            radiation_station_id=radiation_id,
            solar_latitude=station.latitude,
            solar_longitude=station.longitude,
            radiation_required=False,
            tolerate_failures=True,
        )
        for column in MET:
            raw[column] = G.valid_observations(raw[column], column)
        receipt["cloud_units"] = _cloud_units(raw)
        receipt["raw_unit_attrs"] = {
            key: raw.attrs[key]
            for key in ("cloud_source_unit", "cloud_output_unit")
            if key in raw.attrs
        }
        raw.attrs["year"] = int(year)
        frames["primary"] = raw
        receipt["ingestion_failures"] = raw.attrs.get("source_failures", [])
        receipt["radiation"] = radiation_metadata
        if radiation_id is not None:
            measured = raw[C.METOBS_RADIATION_COLUMN]
            receipt["radiation"].update(
                original_missing_hours=int(measured.isna().sum()),
                status="available" if measured.notna().any() else "unavailable",
            )
        quality = _eligibility(raw)
        receipt.update(quality)
        if not quality["eligible"]:
            raise SourceUnavailable(quality["eligibility_reason"])
        reference, report = _reference(raw, station)
        frames["reference"] = reference
        donor, donor_metadata = _donor(
            raw, location_record, station, year, catalogs, client
        )
        frames["donor"] = donor
        model_columns = [
            "dry_bulb",
            "relative_humidity",
            "reanalysis_surface_pressure",
            "wind_speed",
            "wind_direction",
            "cloud_cover",
            "ghi",
            "dni",
            "dirh",
        ]
        try:
            model, model_metadata = reanalysis.fetch(
                station.latitude,
                station.longitude,
                station.elevation,
                raw.index,
                client,
            )
        except IngestionError as exc:
            model = pd.DataFrame(np.nan, index=raw.index, columns=model_columns)
            model_metadata = {"status": "unavailable", "reason": str(exc)}
        frames["era5"] = model
        receipt.update(
            status="ready",
            donor=donor_metadata,
            reanalysis=model_metadata,
            baseline_processing=asdict(report),
            reference_attrs={
                key: reference.attrs[key]
                for key in (
                    "automatic_prepared",
                    "automatic_solar_source",
                    "automatic_clamped_dni_hours",
                )
            },
            original_missing_cloud_hours=int(
                raw.get("cloud_cover", pd.Series(np.nan, index=raw.index)).isna().sum()
            ),
            solar_reference="Measured GHI where available, at the recorded Sol-station distance; STRÅNG at primary coordinates supplies the beam partition and alternate GHI. DNI/DHI are hybrid estimates, not independent observations.",
            truth_policy="Only quality-filtered, physically valid original MET cells may be hidden for scoring. Native fills are frozen shared background and excluded from donor assessment.",
        )
    except (SourceUnavailable, DataGapError, IngestionError) as exc:
        receipt["reason"] = str(exc)
    for name, frame in frames.items():
        frame.to_csv(sources / f"{name}-hourly.csv", index_label="utc")
    receipt["frame_sha256"] = {
        name: _sha(sources / f"{name}-hourly.csv") for name in frames
    }
    receipt["raw_responses"] = _responses(client)
    _dump(sources / "receipt.json", receipt)
    if receipt["status"] != "ready":
        raise SourceUnavailable(receipt["reason"])
    return raw, reference, donor, model, receipt
