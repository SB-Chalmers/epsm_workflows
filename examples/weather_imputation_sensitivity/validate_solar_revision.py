"""Run nine fresh annual comparisons after the SMHI2EPW solar correction.

This bounded diagnostic uses three actual provider weather-years and one fixed
100 m² Core building. Corrected references are rebuilt from original inputs;
archived study references, results and caches are never overwritten. Findings
describe these cases, not national uncertainty or independently observed solar
component accuracy. Run as a module from the workflow repository.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

import numpy as np
import pandas as pd
import requests
import smhi2epw
from smhi2epw import automatic, export, reanalysis, solar
from smhi2epw import constants as C
from smhi2epw import gap_recovery as G
from smhi2epw import ingestion as I
from smhi2epw import processing as P

from . import building, metrics
from . import campaign_sources as S
from . import weather as W

ROOT = Path(__file__).resolve().parents[2]
CASES = (
    {"name": "gothenburg-2016", "year": 2016, "station": 71420, "donor": 72420},
    {"name": "lulea-2023", "year": 2023, "station": 162860, "donor": 161940},
    {"name": "gothenburg-2024", "year": 2024, "station": 72420, "donor": 71420},
)


class BudgetSession(requests.Session):
    """Use ordinary HTTP transport within one explicit network request budget."""

    def __init__(self, budget):
        """Bound cache misses across the whole experiment, including workers."""
        super().__init__()
        self.budget = budget
        self.urls = []
        self.lock = Lock()

    def get(self, url, **kwargs):
        """Count recognized provider requests before sending a real HTTP GET."""
        if not any(
            url.startswith(base)
            for base in (C.METOBS_BASE, C.STRANG_BASE, reanalysis.ENDPOINT)
        ):
            raise ValueError(f"Unexpected provider URL: {url}")
        with self.lock:
            if len(self.urls) >= self.budget:
                raise I.IngestionError(
                    "Fresh validation network request budget exhausted"
                )
            self.urls.append(url)
        return super().get(url, **kwargs)


def seed_cache(destination):
    """Seed a separate namespace without copying large files or editing sources."""
    destination.mkdir(parents=True, exist_ok=True)
    roots = (
        # Reuse raw provider responses from the discarded exploratory attempt;
        # its simulations are never reused as validation evidence.
        ROOT / "output/weather-imputation-solar-validation-2026-10-06/raw-cache",
        ROOT / "output/weather-imputation-campaign-2026-10-05-v5/raw-cache",
        ROOT
        / "output/national-mf-se02-random-baseline-2026-10-05-v3-weather-shadow/weather/raw-cache",
        ROOT.parent / "smhi2epw/.smhi_cache",
        ROOT.parent / "smhi2epw/examples/.smhi_cache",
    )
    count = 0
    for root in roots:
        if not root.is_dir():
            continue
        for source in sorted(root.iterdir()):
            target = destination / source.name
            if (
                source.is_file()
                and source.suffix in {".json", ".csv"}
                and not target.exists()
            ):
                try:
                    os.link(source, target)
                except OSError:
                    shutil.copy2(source, target)
                count += 1
    return count


def sources(folder, case, client):
    """Retrieve real raw providers and rebuild the corrected bounded reference."""
    station = I.get_station_metadata(case["station"], client, year=case["year"])
    radiation = I.find_nearest_radiation_station(
        station.latitude, station.longitude, case["year"], client
    )
    radiation_id = (
        radiation[0] if radiation is not None and radiation[1] <= 50 else None
    )
    raw = I.ingest(
        station,
        case["year"],
        client,
        max_workers=2,
        utc_offset=1,
        radiation_station_id=radiation_id,
        solar_latitude=station.latitude,
        solar_longitude=station.longitude,
        tolerate_failures=True,
    )
    for column in S.MET:
        raw[column] = G.valid_observations(raw[column], column)
    raw.attrs["year"] = case["year"]
    quality = S._eligibility(raw)
    if not quality["eligible"]:
        raise S.SourceUnavailable(quality["eligibility_reason"])
    reference, report = S._reference(raw, station)
    donor = pd.DataFrame(np.nan, index=raw.index, columns=S.MET)
    donor_metadata = {}
    for parameter, column in C.METOBS_REQUIRED_PARAMETERS.items():
        try:
            meta = I.get_station_metadata(
                case["donor"], client, year=case["year"], parameter=parameter
            )
            distance = meta.distance_km(station.latitude, station.longitude)
            if distance > 75:
                raise I.IngestionError("Fixed diagnostic donor exceeds 75 km")
            observed = I.fetch_metobs_parameter(
                case["donor"],
                parameter,
                column,
                (raw.index[0], raw.index[-1]),
                client,
            )
            donor[column] = G.hourly_observations(observed, column).reindex(raw.index)
            donor_metadata[column] = asdict(meta) | {"distance_km": distance}
        except I.IngestionError as exc:
            donor_metadata[column] = {"status": "unavailable", "reason": str(exc)}
    model, model_metadata = reanalysis.fetch(
        station.latitude,
        station.longitude,
        station.elevation,
        raw.index,
        client,
    )
    source_dir = folder / "sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    for name, frame in (
        ("primary", raw),
        ("reference", reference),
        ("donor", donor),
        ("era5", model),
    ):
        W.save_frame(source_dir / f"{name}-hourly.csv", frame)
    W.dump(
        source_dir / "receipt.json",
        {
            "station": asdict(station),
            "radiation_station_id": radiation_id,
            "quality": quality,
            "baseline_processing": asdict(report),
            "donor": donor_metadata,
            "reanalysis": model_metadata,
            "raw_responses": list(client.response_receipts.values()),
            "frame_sha256": {
                name: W.sha(source_dir / f"{name}-hourly.csv")
                for name in ("primary", "reference", "donor", "era5")
            },
        },
    )
    context = W.StudyContext(
        year=case["year"],
        latitude=station.latitude,
        longitude=station.longitude,
        station_id=station.station_id,
        city=f"Fresh solar diagnostic {case['name']}",
        solar_source=report.solar_source,
        solar_origin="Actual SMHI GHI and STRÅNG components; corrected reference includes bounded native filling; components are not independent observed truth",
    )
    return raw, reference, donor, model, station, context, quality


def compare_loads(reference, candidate, reference_metrics, candidate_metrics):
    """Report absolute energy/peak/hourly differences for one fixed building."""
    assert reference.lst_hour_ending.equals(candidate.lst_hour_ending)
    result = {}
    for mode in ("heating", "cooling"):
        before = reference[f"{mode}_kw"].to_numpy()
        after = candidate[f"{mode}_kw"].to_numpy()
        energy = (
            candidate_metrics[f"annual_{mode}_kwh"]
            - reference_metrics[f"annual_{mode}_kwh"]
        )
        base_energy = reference_metrics[f"annual_{mode}_kwh"]
        base_peak = reference_metrics[f"peak_{mode}_kw"]
        peak = candidate_metrics[f"peak_{mode}_kw"] - base_peak
        before_hours = pd.to_datetime(reference_metrics[f"peak_{mode}_hours_lst"])
        after_hours = pd.to_datetime(candidate_metrics[f"peak_{mode}_hours_lst"])
        shift = (
            min(
                abs((a - b).total_seconds()) / 3600
                for a in before_hours
                for b in after_hours
            )
            if base_peak > 1e-6
            else None
        )
        result[mode] = {
            "reference_annual_kwh_m2": base_energy / 100,
            "signed_annual_error_kwh_m2": energy / 100,
            "absolute_annual_error_kwh_m2": abs(energy) / 100,
            "signed_annual_error_percent": 100 * energy / base_energy
            if base_energy > 0
            else None,
            "reference_peak_w_m2": base_peak * 10,
            "signed_peak_error_w_m2": peak * 10,
            "hourly_load_mae_w_m2": float(np.abs(after - before).mean()) * 10,
            "maximum_hourly_load_error_w_m2": float(np.abs(after - before).max()) * 10,
            "nearest_annual_peak_separation_h": shift,
        }
    return result


def compare_weather(reference, candidate, mask, raw, scope):
    """Score original meteorology or hybrid solar values only in the fixed mask."""
    selected = export.shift_to_lst(
        pd.DataFrame({"mask": mask}), int(raw.attrs["year"]), 1
    )["mask"].to_numpy()
    result = {}
    for column in S.MET if scope == "meteorology" else ("ghi", "dni", "dhi"):
        difference = candidate[column].to_numpy() - reference[column].to_numpy()
        if column == "wind_direction":
            difference = (difference + 180) % 360 - 180
        eligible = selected.copy()
        if scope == "meteorology":
            original = (
                export.shift_to_lst(raw[[column]], int(raw.attrs["year"]), 1)[column]
                .notna()
                .to_numpy()
            )
            eligible &= original
        errors = difference[eligible]
        result[column] = {
            "scored_exported_hours": len(errors),
            "mean_error": float(errors.mean()),
            "mae": float(np.abs(errors).mean()),
            "maximum_absolute_error": float(np.abs(errors).max()),
            "reference_basis": "original-observation-supported reference"
            if scope == "meteorology"
            else "hybrid solar reference; components are not independent observations",
        }
    return result


def run_case(root, case, base_client, engine):
    """Run corrected reference plus one solar and one temperature-event outage."""
    folder = root / case["name"]
    folder.mkdir(parents=True, exist_ok=True)
    raw, reference, donor, model, station, context, quality = sources(
        folder, case, base_client.scoped()
    )
    reference_path, reference_weather = W.render(
        folder,
        "reference",
        reference,
        station.elevation,
        {"method": "corrected_reference"},
        context=context,
    )
    reference_receipt = building.simulate(
        reference_path,
        folder / "cases/reference/simulation",
        year=context.year,
        executable=engine,
    )
    reference_hourly, reference_metrics = metrics.extract(
        folder / "cases/reference/simulation/eplusout.sql", year=context.year
    )
    print(f"Completed {case['name']} corrected reference", flush=True)
    solar_anchor = pd.Timestamp(context.year, 6, 21, 12, tz="UTC")
    annual_original = export.shift_to_lst(raw[["dry_bulb"]], context.year, 1)
    # Convert the LST-naive endpoint back to UTC for the exact input-hour mask.
    temperature_anchor = (
        annual_original.dry_bulb.idxmax().tz_localize("Etc/GMT-1").tz_convert("UTC")
    )
    outcomes = []
    for name, scope, duration, anchor in (
        ("solar-168h", "solar", 168, solar_anchor),
        ("temperature-event-48h", "meteorology", 48, temperature_anchor),
    ):
        mask = pd.Series(
            (raw.index >= anchor - pd.Timedelta(hours=duration // 2))
            & (raw.index < anchor + pd.Timedelta(hours=duration // 2)),
            index=raw.index,
        )
        assert int(mask.sum()) == duration
        candidate, decisions = W.reconstruct(
            reference, raw, donor, model, mask, "automatic", scope, context=context
        )
        if scope == "solar":
            # The archived helper copies only GHI/DNI/beam back to the frame.
            # Prepared solar now bypasses a second finalization in processing,
            # so explicitly copy the DHI implied by the finalized beam group.
            candidate.loc[mask, "dhi"] = np.maximum(
                candidate.loc[mask, "ghi"] - candidate.loc[mask, "dirh"], 0.0
            )
        assert np.allclose(
            candidate.ghi, candidate.dhi + candidate.dirh, atol=1e-9, rtol=0
        ), "Prepared solar group fails GHI = DHI + mean horizontal beam"
        path, candidate_weather = W.render(
            folder,
            name,
            candidate,
            station.elevation,
            {
                "method": "automatic",
                "scope": scope,
                "hidden_hours": duration,
                "anchor_utc": anchor,
                "decisions": decisions,
            },
            context=context,
        )
        exported = export.shift_to_lst(
            pd.DataFrame({"hidden": mask}), context.year, 1
        ).hidden.to_numpy()
        before_rows = reference_path.read_text().splitlines()[8:]
        after_rows = path.read_text().splitlines()[8:]
        assert all(
            before == after
            for before, after, hidden in zip(before_rows, after_rows, exported)
            if not hidden
        ), "Unmasked EPW data changed"
        receipt = building.simulate(
            path,
            folder / f"cases/{name}/simulation",
            year=context.year,
            executable=engine,
        )
        assert receipt["model_sha256"] == reference_receipt["model_sha256"], (
            "Paired compiled models differ"
        )
        hourly, summary = metrics.extract(
            folder / f"cases/{name}/simulation/eplusout.sql", year=context.year
        )
        outcomes.append(
            {
                "case": name,
                "scope": scope,
                "duration_h": duration,
                "anchor_utc": str(anchor),
                "all_unmasked_weather_rows_identical": True,
                "compiled_model_identical_to_reference": True,
                "weather_errors": compare_weather(
                    reference_weather, candidate_weather, mask, raw, scope
                ),
                "load_errors": compare_loads(
                    reference_hourly, hourly, reference_metrics, summary
                ),
                "engine_warning_count": receipt["warning_count"],
                "core_diagnostics_accepted": receipt["core_diagnostics_accepted"],
                "weather_sha256": W.sha(path),
            }
        )
        print(f"Completed {case['name']} {name}", flush=True)
    result = {
        "weather_year": case,
        "source_station": asdict(station),
        "source_quality": quality,
        "reference_metrics": reference_metrics,
        "reference_engine_warnings": reference_receipt["warning_count"],
        "reference_epw_sha256": W.sha(reference_path),
        "outcomes": outcomes,
    }
    W.dump(folder / "comparison.json", result)
    return result


def main():
    """Execute a separate bounded experiment and preserve failures explicitly."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "output/weather-imputation-solar-validation-2026-10-06-v2",
    )
    parser.add_argument("--energyplus", type=Path, default=building.ENGINE)
    parser.add_argument("--network-request-budget", type=int, default=40)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(__file__), args.output / "executed-driver.py")
    started_utc = datetime.now(UTC).isoformat()
    W.dump(
        args.output / "invocation.json",
        {
            "started_utc": started_utc,
            "python_arguments": sys.orig_argv,
            "working_directory": str(Path.cwd()),
            "pythonpath": os.environ.get("PYTHONPATH"),
            "note": "Source-tree import paths select the corrected local package and Core versions; no code is injected into the driver.",
        },
    )
    source_paths = [
        Path(module.__file__)
        for module in (
            smhi2epw,
            automatic,
            export,
            reanalysis,
            solar,
            G,
            I,
            P,
            C,
            W,
            S,
            building,
            metrics,
        )
    ] + [Path(__file__)]
    fingerprints = {str(path): W.sha(path) for path in source_paths}
    W.dump(args.output / "source-identities.json", fingerprints)
    seeded = seed_cache(args.output / "raw-cache")
    session = BudgetSession(args.network_request_budget)
    client = I.CachedClient(
        cache_dir=str(args.output / "raw-cache"),
        session=session,
        timeout=60,
        max_retries=0,
    )
    outcomes, failures = [], []
    for case in CASES:
        try:
            outcomes.append(run_case(args.output, case, client, args.energyplus))
        except Exception as exc:  # noqa: BLE001 - retain each requested case's failure
            failures.append(
                {
                    "weather_year": case,
                    "exception": type(exc).__name__,
                    "reason": str(exc),
                }
            )
            print(
                f"Unavailable {case['name']}: {type(exc).__name__}: {exc}", flush=True
            )
    assert all(W.sha(path) == fingerprints[str(path)] for path in source_paths), (
        "Source code changed during execution"
    )
    W.dump(
        args.output / "summary.json",
        {
            "status": "complete" if not failures else "complete_with_unavailable_cases",
            "started_utc": started_utc,
            "completed_utc": datetime.now(UTC).isoformat(),
            "requested_weather_years": list(CASES),
            "completed_weather_years": len(outcomes),
            "requested_annual_runs": 9,
            "completed_annual_runs": 3 * len(outcomes),
            "source_code_sha256": fingerprints,
            "engine_sha256": W.sha(args.energyplus),
            "seeded_cache_files": seeded,
            "network_requests": len(session.urls),
            "network_request_budget": args.network_request_budget,
            "actual_network_urls": session.urls,
            "outcomes": outcomes,
            "failures": failures,
            "interpretation": [
                "Fresh corrected references and candidates; the earlier 9450-run campaign is unchanged.",
                "Baseline/candidate solar errors common to the original study can cancel. Its published sensitivity is conditional on its frozen reference; it is not absolute solar validation.",
                "One fixed 100 m² illustrative Core building, three selected weather-years and two deterministic outages each; no national confidence intervals or acceptance thresholds.",
                "Optional clouds are frozen; fixed donors use unmasked original observations for assessment. Model/source discovery is not independently validated by the paired comparisons.",
                "Native solar reference components include modelled STRANG partitioning and bounded fills; they are not independently observed DNI/DHI.",
                "Only simulations accepted by Core enter this diagnostic. Warning counts are reported explicitly; acceptance does not prove zero warning effect or generic weather accuracy.",
            ],
        },
    )
    print(args.output / "summary.json", flush=True)
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
