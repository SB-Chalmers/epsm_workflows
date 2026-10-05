"""Controlled weather replacements using the installed smhi2epw algorithms.

This experiment forces alternatives for comparison. It does not alter the
production source hierarchy or raise its 48-hour temporal ceiling.
"""

import hashlib
import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from smhi2epw import automatic, export, read_epw, reanalysis
from smhi2epw import constants as C
from smhi2epw import gap_recovery as G
from smhi2epw import ingestion as I
from smhi2epw import processing as P
from smhi2epw.errors import Smhi2EpwError as Smhi2EpwError

YEAR, LAT, LON, OFFSET = 2023, 57.7089, 11.9746, 1
PRIMARY, DONOR, RADIATION = 72420, 71420, 71415
MET = list(C.METOBS_REQUIRED_PARAMETERS.values())
SOLAR = ["ghi", "dni", "dirh"]


@dataclass(frozen=True)
class StudyContext:
    """Fix one reference's actual calendar, source coordinates and provenance."""

    year: int = YEAR
    latitude: float = LAT
    longitude: float = LON
    utc_offset: int = OFFSET
    station_id: int = PRIMARY
    city: str = "Gothenburg sensitivity study"
    solar_origin: str = "measured GHI at SMHI Sol 71415 with STRÅNG beam partition; native STRÅNG gaps reconstructed identically"
    solar_source: str = "measured+strang_partition"


def dump(path, value):
    """Write readable UTF-8 receipts without nonfinite JSON numbers."""
    Path(path).write_text(
        json.dumps(value, indent=2, default=str, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def sha(path):
    """Fingerprint an exact artifact or source file."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save_frame(path, frame):
    """Preserve hourly UTC input data at full floating-point precision."""
    frame.to_csv(path, index_label="utc")


def load_frame(path):
    """Load a previously pinned hourly frame, including original missing cells."""
    frame = pd.read_csv(path, index_col=0)
    frame.index = pd.to_datetime(frame.index, utc=True)
    return frame


def prepare_sources(root, seed_cache, donor_cache):
    """Pin original primary, donor and ERA5 data and construct a complete reference."""
    root = Path(root)
    sources = root / "sources"
    sources.mkdir(exist_ok=True)
    if (sources / "receipt.json").exists():
        return load_sources(root)
    cache = sources / "raw-cache"
    cache.mkdir(exist_ok=True)
    # Copy the known primary responses; seed only metadata and five donor
    # archives from the other cache. Original campaign caches are never edited.
    for path in Path(seed_cache).iterdir():
        if path.is_file():
            shutil.copy2(path, cache / path.name)
    names = set()
    for parameter in C.METOBS_REQUIRED_PARAMETERS:
        url = f"{C.METOBS_BASE}/parameter/{parameter}/station/{DONOR}/period/{C.METOBS_PERIOD}/data.csv"
        names.add(hashlib.sha256(url.encode()).hexdigest()[:24] + ".csv")
    for path in Path(donor_cache).iterdir():
        if path.suffix == ".json" or path.name in names:
            if not (cache / path.name).exists():
                shutil.copy2(path, cache / path.name)
    client = I.CachedClient(cache_dir=str(cache), timeout=120).scoped()
    station = I.get_station_metadata(PRIMARY, client, year=YEAR)
    raw = I.ingest(
        station,
        YEAR,
        client,
        utc_offset=OFFSET,
        radiation_station_id=RADIATION,
        solar_latitude=LAT,
        solar_longitude=LON,
        radiation_required=True,
    )
    for column in MET:
        raw[column] = G.valid_observations(raw[column], column)
    donor = pd.DataFrame(index=raw.index)
    donor_metadata = {}
    for parameter, column in C.METOBS_REQUIRED_PARAMETERS.items():
        meta = I.get_station_metadata(DONOR, client, year=YEAR, parameter=parameter)
        donor_metadata[column] = asdict(meta) | {
            "distance_km": meta.distance_km(LAT, LON)
        }
        observed = I.fetch_metobs_parameter(
            DONOR,
            parameter,
            column,
            (raw.index[0], raw.index[-1]),
            client,
            accepted_quality=C.METOBS_ACCEPTED_QUALITY,
        )
        donor[column] = G.hourly_observations(observed, column).reindex(raw.index)
    model, model_metadata = reanalysis.fetch(
        LAT, LON, station.elevation, raw.index, client
    )
    report = P.ProcessingReport()
    reference = raw.copy(deep=True)
    elevation, _ = automatic.prepare(
        reference,
        station,
        YEAR,
        LAT,
        LON,
        station.elevation,
        client,
        report,
        max_distance_km=75,
        max_stations=3,
    )
    if report.cross_station_filled_hours or any(
        report.reanalysis_filled_hours.values()
    ):
        raise ValueError(
            "Reference requires donor/reanalysis recovery; choose a better supported AMY"
        )
    assert elevation == station.elevation
    report.solar_source = reference.attrs["automatic_solar_source"]
    for name, frame in [
        ("primary", raw),
        ("reference", reference),
        ("donor", donor),
        ("era5", model),
    ]:
        save_frame(sources / f"{name}-hourly.csv", frame)
    receipt = {
        "year": YEAR,
        "latitude": LAT,
        "longitude": LON,
        "utc_offset": OFFSET,
        "station": asdict(station),
        "donor": donor_metadata,
        "reanalysis": model_metadata,
        "baseline_processing": asdict(report),
        "original_missing_hours": {c: int(raw[c].isna().sum()) for c in MET},
        "original_missing_cloud_hours": int(
            raw.get("cloud_cover", pd.Series(np.nan, index=raw.index)).isna().sum()
        ),
        "raw_responses": list(client.response_receipts.values()),
        "frame_sha256": {
            name: sha(sources / f"{name}-hourly.csv")
            for name in ("primary", "reference", "donor", "era5")
        },
    }
    dump(sources / "receipt.json", receipt)
    return raw, reference, donor, model, receipt


def load_sources(root):
    """Reuse pinned data for offline reruns."""
    sources = Path(root) / "sources"
    receipt = json.loads((sources / "receipt.json").read_text())
    for name, expected in receipt["frame_sha256"].items():
        if sha(sources / f"{name}-hourly.csv") != expected:
            raise ValueError(f"Pinned source changed: {name}")
    return (
        *[
            load_frame(sources / f"{name}-hourly.csv")
            for name in ("primary", "reference", "donor", "era5")
        ],
        receipt,
    )


def render(root, case_id, inputs, elevation, diagnostics, *, context=None):
    """Regenerate derived weather and write a fully validated annual EPW."""
    context = context or StudyContext()
    folder = Path(root) / "cases" / case_id
    folder.mkdir(parents=True, exist_ok=True)
    frame = inputs.copy(deep=True)
    frame.attrs["automatic_prepared"] = True
    report = P.process(
        frame, context.latitude, context.longitude, target_elevation_m=elevation
    )
    # The complete reference already contains documented small native fills;
    # all controlled alternatives include additional reconstruction.
    report.weather_classification = "mixed_reconstructed"
    solar_origin = context.solar_origin
    replacements = {
        "temporal": "bounded daily profiles with physical darkness zeros",
        "era5": "same-year ERA5 GHI and direct component group",
        "erbs": "retained reference GHI with Erbs direct/diffuse decomposition",
        "automatic": "bounded temporal solar completion, then same-year ERA5 for remaining missing solar components; exact decisions recorded",
        "automatic_legacy": "bounded temporal solar completion, then same-year ERA5 for remaining missing solar components; exact decisions recorded",
    }
    scope = diagnostics.get("scope")
    report.solar_source = "mixed" if scope == "solar" else context.solar_source
    diagnostics = diagnostics | {
        "processing_report_scope": "physical derivation; forced replacements are recorded by hidden_hours and decisions, not its temporal-fill counters",
        "solar_provenance": {
            "unmasked": solar_origin,
            "masked": replacements.get(diagnostics.get("method"))
            if scope == "solar"
            else solar_origin,
        },
        "solar_error_reference": "hybrid reference irradiance, not independently observed DNI/DHI",
    }
    header = export.build_header(
        city=context.city,
        region="SE",
        country="SWE",
        wmo_id="999999",
        latitude=context.latitude,
        longitude=context.longitude,
        time_zone=context.utc_offset,
        elevation=elevation,
        year=context.year,
        station_id=context.station_id,
    )
    header[5] += f" Controlled synthetic outage scenario {case_id}."
    path = folder / "weather.epw"
    export.write_epw(
        path,
        header,
        export.shift_to_lst(frame, context.year, context.utc_offset),
        context.year,
    )
    validated = read_epw(path)
    dump(
        folder / "weather-receipt.json",
        diagnostics
        | {
            "epw_sha256": sha(path),
            "processing": asdict(report),
            "rows": len(validated),
        },
    )
    return path, validated


def outage_mask(index, originals, anchors, hours):
    """Choose four nonoverlapping blocks, retaining each reference peak anchor.

    Prefer windows with original observations in every cell. If no such window
    includes an anchor, fail rather than treating native imputation as truth.
    """
    mask = pd.Series(False, index=index)
    blocks = []
    complete = originals[MET].notna().all(axis=1)
    for name, anchor in anchors.items():
        centre = index.get_indexer([pd.Timestamp(anchor).tz_convert("UTC")])[0]
        if centre < 0:
            raise ValueError(f"Anchor absent from weather: {anchor}")
        preferred = centre - hours // 2
        starts = sorted(
            range(centre - hours + 1, centre + 1), key=lambda x: (abs(x - preferred), x)
        )
        start = next(
            (
                s
                for s in starts
                if s > 0
                and s + hours < len(index)
                and complete.iloc[s : s + hours].all()
                and not mask.iloc[s : s + hours].any()
            ),
            None,
        )
        if start is None:
            raise ValueError(
                f"No originally complete {hours}-hour window around {name}"
            )
        mask.iloc[start : start + hours] = True
        blocks.append(
            {
                "event": name,
                "start_utc": index[start].isoformat(),
                "end_utc": index[start + hours - 1].isoformat(),
                "hours": hours,
            }
        )
    return mask, blocks


def temporal(series, column, *, max_gap_hours=C.MAX_GAP_HOURS):
    """Apply production temporal filling and bounds only to reconstructed cells."""
    kwargs = dict(
        required=False, short_gap_hours=C.SHORT_GAP_HOURS, max_gap_hours=max_gap_hours
    )
    if column == "wind_direction":
        return P._fill_wind_direction(series, **kwargs)[0]
    filled = P._fill_scalar_series(series, solar=column in SOLAR, **kwargs)[0]
    # P.impute applies these bounds after its private interpolator. Controlled
    # methods must reproduce that guard rather than export overshooting profiles.
    limits = {
        "relative_humidity": (0.0, 100.0),
        "wind_speed": (0.0, 40.0),
        "cloud_cover": (0.0, 8.0),
    }
    if column in limits:
        lo, hi = limits[column]
        missing = series.isna()
        bounded = missing & ((filled < lo) | (filled > hi))
        filled.loc[missing] = filled.loc[missing].clip(lo, hi)
        filled.attrs["bounded_filled_hours"] = int(bounded.sum())
    return filled


def reconstruct(
    reference,
    originals,
    donor,
    model,
    mask,
    method,
    scope="meteorology",
    *,
    context=None,
    columns=None,
):
    """Replace only hidden inputs and expose method-specific decisions.

    Donor raw is an explicitly unassessed diagnostic. Assessed variants use
    original unmasked observations exclusively for production cross-validation.
    Native donor holes get only bounded temporal reconstruction afterwards.
    Prediction contexts start from original observations with selected truth
    removed. Frozen native reference fills may have consumed that truth and
    must never become interpolation/profile predictors.
    """
    context = context or StudyContext()
    frame = reference.copy(deep=True)
    columns = list(columns or (MET if scope == "meteorology" else SOLAR))
    automatic_method = method in ("automatic", "automatic_legacy")
    frame.loc[mask, columns] = np.nan
    decisions = {}
    if scope == "solar":
        working = originals.copy(deep=True)
        # Forced ERA5/Erbs tests bypass _solar_inputs, which normally creates
        # this optional placeholder before _finish_solar removes it. Absence
        # represents STRANG-only input, never a measured zero.
        if C.METOBS_RADIATION_COLUMN not in working:
            working[C.METOBS_RADIATION_COLUMN] = np.nan
        working.loc[mask, columns] = np.nan
        if C.METOBS_RADIATION_COLUMN in working:
            working.loc[mask, C.METOBS_RADIATION_COLUMN] = np.nan
        # The automatic production path establishes dark-interval zeros before
        # interpolation. Calling the profile interpolator alone can shift a
        # daytime endpoint offset into nighttime radiation.
        solar_report = P.ProcessingReport()
        if method == "temporal" or automatic_method:
            ghi, _, _, _ = automatic._solar_inputs(
                working, solar_report, context.latitude, context.longitude
            )
            working["ghi"] = ghi
            if automatic_method:
                missing = ghi.isna()
                usable = model["ghi"].where(model["ghi"].between(0, 2000))
                selected = missing & usable.notna()
                working.loc[selected, "ghi"] = usable[selected]
                working.loc[selected, ["dni", "dirh"]] = model.loc[
                    selected, ["dni", "dirh"]
                ]
                decisions["solar_era5_hours"] = int(selected.sum())
        elif method == "era5":
            working.loc[mask, SOLAR] = model.loc[mask, SOLAR]
        elif method == "erbs":
            working.loc[mask, "ghi"] = reference.loc[mask, "ghi"]
        else:
            raise ValueError(f"Unsupported solar method: {method}")
        tags = pd.DataFrame({"ghi": "primary"}, index=working.index)
        automatic._finish_solar(
            working,
            working.ghi.copy(),
            pd.Series(False, index=working.index),
            tags,
            context.latitude,
            context.longitude,
            solar_report,
        )
        frame.loc[mask, SOLAR] = working.loc[mask, SOLAR]
        if C.METOBS_RADIATION_COLUMN in frame:
            frame.loc[mask, C.METOBS_RADIATION_COLUMN] = np.nan
        decisions["solar"] = asdict(solar_report)
        return frame, decisions
    for column in columns:
        prediction = originals[column].mask(mask)
        if method == "temporal":
            filled = temporal(prediction, column)
        elif method in ("donor_raw", "donor_assessed", "automatic", "automatic_legacy"):
            if scope != "meteorology":
                raise ValueError("Meteorological donors cannot supply solar data")
            filled = (
                temporal(
                    prediction,
                    column,
                    max_gap_hours=C.SHORT_GAP_HOURS
                    if method == "automatic"
                    else C.MAX_GAP_HOURS,
                )
                if automatic_method
                else prediction.copy()
            )
            pending = mask & filled.isna()
            training = originals[column].mask(mask)
            assessments = []
            invalid_corrected = 0
            for start, end in P._gap_runs(pending.to_numpy()):
                assessment = G._assessment(
                    training,
                    donor[column],
                    column,
                    frame.index[start],
                    frame.index[end - 1],
                )
                assessments.append(assessment)
                if method == "donor_raw":
                    offset = 0.0
                elif assessment["status"] == "accepted":
                    offset = assessment["offset"]
                elif automatic_method:
                    assessment["application"] = "rejected_donor; no transfer"
                    continue
                else:
                    raise ValueError(
                        f"Assessed donor rejected {column}: {assessment['reason']}"
                    )
                transferred = G.valid_observations(donor[column] + offset, column)
                assessment["application"] = (
                    "unassessed raw transfer; fitted offset not applied"
                    if method == "donor_raw"
                    else "accepted assessed transfer"
                )
                assessment["applied_offset"] = offset
                assessment["transferred_hours"] = int(
                    transferred.iloc[start:end].notna().sum()
                )
                invalid_corrected += int(
                    (
                        donor[column].iloc[start:end].notna()
                        & transferred.iloc[start:end].isna()
                    ).sum()
                )
                filled.iloc[start:end] = transferred.iloc[start:end]
            filled = temporal(filled, column)
            decisions[column] = {
                "assessments": assessments,
                "corrected_out_of_bounds_hours": invalid_corrected,
                "donor_temporal_filled_hours": int(
                    (pending & donor[column].isna() & filled.notna()).sum()
                ),
                "bounded_temporal_hours": filled.attrs.get("bounded_filled_hours", 0),
            }
            if automatic_method:
                filled = G.valid_observations(filled, column)
            if automatic_method and filled[mask].isna().any():
                pending = mask & filled.isna()
                if column == "pressure":
                    frame.loc[pending, "reanalysis_surface_pressure"] = model.loc[
                        pending, "reanalysis_surface_pressure"
                    ]
                else:
                    filled.loc[pending] = model.loc[pending, column]
                decisions[column]["era5_hours"] = int(pending.sum())
        elif method == "era5":
            filled = frame[column].copy()
            if column == "pressure":
                frame.loc[mask, "reanalysis_surface_pressure"] = model.loc[
                    mask, "reanalysis_surface_pressure"
                ]
            else:
                filled.loc[mask] = model.loc[mask, column]
        elif method == "erbs" and scope == "solar":
            # Retain GHI while removing only the direct component group.
            frame.loc[mask, "ghi"] = reference.loc[mask, "ghi"]
            continue
        else:
            raise ValueError(method)
        frame.loc[mask, column] = filled[mask]
    # ERA5 cloud cover is intentionally excluded: a hidden temperature sample
    # must not change optional weather or longwave across the rest of the year.
    return frame, decisions


def weather_errors(
    reference_epw, candidate_epw, mask, scope, *, context=None, columns=None
):
    """Check identical unmasked weather and quantify errors on hidden truth."""
    context = context or StudyContext()
    exported = export.shift_to_lst(
        pd.DataFrame({"mask": mask}), context.year, context.utc_offset
    )["mask"].to_numpy()
    if not reference_epw.loc[~exported].equals(candidate_epw.loc[~exported]):
        raise ValueError("Reconstruction changed unmasked weather")
    errors = {}
    for column in columns or (MET if scope == "meteorology" else ["ghi", "dni", "dhi"]):
        delta = candidate_epw.loc[exported, column].to_numpy(
            dtype=float
        ) - reference_epw.loc[exported, column].to_numpy(dtype=float)
        if column == "wind_direction":
            delta = (delta + 180) % 360 - 180
        errors[column] = {
            "hours": len(delta),
            "bias": float(delta.mean()),
            "mae": float(np.abs(delta).mean()),
            "rmse": float(np.sqrt(np.mean(delta**2))),
        }
    return errors
