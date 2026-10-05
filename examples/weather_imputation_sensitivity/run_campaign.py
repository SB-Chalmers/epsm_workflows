"""Run the staged Swedish, paired weather-completion sensitivity campaign.

Production packages, hourly sources, outage evidence, buildings and engine are
pinned. Resume filters select subsets of the same matrix; they do not change
its statistical definition. Unsupported sources/methods stay in the catalogue.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

DEFAULT_OUTPUT = Path("output/weather-imputation-campaign-2026-10-05-v5")
PREVIOUS = Path("output/weather-imputation-sensitivity-2026-10-05-v1")
MET_METHODS = ("temporal", "donor_assessed", "era5", "automatic", "automatic_legacy")
SOLAR_METHODS = ("temporal", "era5", "automatic")
DURATIONS = (3, 12, 48, 168)
SEEDS = (11, 29, 47)
ZSTD = Path(shutil.which("zstd") or "/opt/homebrew/Caskroom/miniconda/base/bin/zstd")


def dump(path, value):
    """Write a strict receipt atomically so interrupted campaigns can resume."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, default=str, allow_nan=False) + "\n"
    )
    temporary.replace(path)


def fingerprint(path):
    """Hash exact files with bounded memory, including large raw observations."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_hashes(root, *, code_only=False):
    """Pin source packages without incidental interpreter bytecode files."""
    return {
        str(path.relative_to(root)): fingerprint(path)
        for path in sorted(Path(root).rglob("*"))
        if path.is_file()
        and "__pycache__" not in path.parts
        and (not code_only or path.suffix == ".py")
    }


def pin_runtime(root):
    """Freeze Core and committed weather sources before importing either package."""
    runtime = root / "runtime"
    packages = runtime / "packages"
    marker = runtime / "manifest.json"
    driver_hashes = tree_hashes(Path(__file__).parent, code_only=True)
    interpreter = {
        "python": sys.version,
        "numpy": np.__version__,
        "pandas": pd.__version__,
    }
    if marker.exists():
        receipt = json.loads(marker.read_text())
        if (
            receipt["driver_files"] != driver_hashes
            or receipt["driver_files"]
            != tree_hashes(runtime / "driver", code_only=True)
            or receipt["package_files"] != tree_hashes(packages)
            or receipt["interpreter"] != interpreter
        ):
            raise ValueError(
                "Pinned runtime changed; use a fresh campaign output directory"
            )
    else:
        packages.mkdir(parents=True, exist_ok=True)
        core = PREVIOUS / "reproduction/pinned-packages/epsm_core"
        weather = Path(__file__).resolve().parents[2].parent / "smhi2epw/src/smhi2epw"
        if not core.is_dir() or not weather.is_dir():
            raise ValueError(
                "Provide the original pinned Core and sibling smhi2epw checkout"
            )
        weather_repository = weather.parents[1]
        weather_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=weather_repository, text=True
        ).strip()
        if subprocess.check_output(
            ["git", "status", "--porcelain", "--", "src"],
            cwd=weather_repository,
            text=True,
        ).strip():
            raise ValueError(
                "Commit weather source changes before pinning the campaign"
            )
        for source in (core, weather):
            destination = packages / source.name
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(
                source, destination, ignore=shutil.ignore_patterns("__pycache__")
            )
        driver = runtime / "driver"
        shutil.copytree(
            Path(__file__).parent,
            driver,
            ignore=shutil.ignore_patterns("__pycache__"),
            dirs_exist_ok=True,
        )
        dump(
            marker,
            {
                "driver_files": driver_hashes,
                "package_files": tree_hashes(packages),
                "weather_commit": weather_commit,
                "core_basis": str(core.resolve()),
                "interpreter": interpreter,
            },
        )
    sys.path.insert(0, str(packages.resolve()))
    return json.loads(marker.read_text())


def selection(value, allowed, kind, cast=str):
    """Resolve optional subset filters without silently expanding the campaign."""
    selected = (
        tuple(allowed) if value is None else tuple(cast(x) for x in value.split(","))
    )
    if (
        not selected
        or len(set(selected)) != len(selected)
        or not set(selected).issubset(allowed)
    ):
        raise ValueError(f"Invalid {kind} selection: {selected}")
    return selected


def publish_artifact(
    temporary, destination, receipt_path, receipt, *, hash_key="sha256"
):
    """Prepare a verifiable transaction before publishing data and its receipt."""
    if fingerprint(temporary) != receipt[hash_key]:
        raise ValueError("Prepared artifact differs from its publication receipt")
    pending = Path(str(receipt_path) + ".pending.json")
    dump(pending, receipt)
    Path(temporary).replace(destination)
    dump(receipt_path, receipt)
    pending.unlink()


def recover_publication(destination, receipt_path, *, hash_key="sha256"):
    """Finish an interrupted publication only when its exact bytes are verified."""
    pending = Path(str(receipt_path) + ".pending.json")
    if not pending.exists():
        return
    prepared = json.loads(pending.read_text())
    if not Path(destination).exists():
        # Publication stopped before any data became visible.
        pending.unlink()
        return
    actual = fingerprint(destination)
    if actual == prepared[hash_key]:
        dump(receipt_path, prepared)
    elif (
        Path(receipt_path).exists()
        and actual == json.loads(Path(receipt_path).read_text())[hash_key]
    ):
        # The old generation remains intact; abandon the unpublished write.
        pass
    else:
        raise ValueError("Interrupted artifact publication bytes changed")
    pending.unlink()


def save_table(path, table):
    """Replace comparison catalogues atomically, preserving crash-safe resume."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(path).with_suffix(".csv.tmp")
    table.to_csv(temporary, index=False)
    publish_artifact(
        temporary,
        path,
        Path(str(path) + ".receipt.json"),
        {"sha256": fingerprint(temporary), "rows": len(table)},
    )


def load_table(path):
    """Reject altered comparison checkpoints instead of accepting stale metrics."""
    path = Path(path)
    receipt_path = Path(str(path) + ".receipt.json")
    recover_publication(path, receipt_path)
    if not receipt_path.exists():
        raise ValueError(f"Missing comparison checkpoint receipt: {path}")
    receipt = json.loads(receipt_path.read_text())
    if fingerprint(path) != receipt["sha256"]:
        raise ValueError(f"Comparison checkpoint changed: {path}")
    try:
        table = pd.read_csv(path)
    except pd.errors.EmptyDataError:
        table = pd.DataFrame()
    if len(table) != receipt["rows"]:
        raise ValueError(f"Comparison checkpoint row count changed: {path}")
    return table


def merge_panel_plan(path, records, selected_ids):
    """Keep immutable evidence and accumulate selected batches across resumes."""
    selected = set(selected_ids)
    if Path(path).exists():
        previous = json.loads(Path(path).read_text())
        if previous["all_panels"] != records:
            raise ValueError("Outage evidence changed across campaign batches")
        selected.update(previous["selected_evidence_ids"])
    dump(path, {"all_panels": records, "selected_evidence_ids": sorted(selected)})


def merge_catalogue(path, cases, unavailable, **metadata):
    """Retain prior methods and replace a case atomically when its status changes."""
    records = {}
    if Path(path).exists():
        previous = json.loads(Path(path).read_text())
        for key, value in metadata.items():
            if previous.get(key) != value:
                raise ValueError(f"Fixed campaign catalogue metadata changed: {key}")
        for record in previous["cases"] + previous["unavailable"]:
            records[record["case_id"]] = record
    for record in cases + unavailable:
        records[record["case_id"]] = record
    dump(
        path,
        metadata
        | {
            "cases": [
                r
                for r in records.values()
                if r.get("status") not in {"unsupported", "unavailable"}
            ],
            "unavailable": [
                r
                for r in records.values()
                if r.get("status") in {"unsupported", "unavailable"}
            ],
        },
    )


def weather_anchors(raw, context, reference):
    """Target annual extremes and detected spells without simulated load selection."""
    from .event_anchors import weather_anchors as select_anchors

    return select_anchors(raw, reference, context)


def compact_simulation(folder):
    """Keep validated SQL losslessly compressed and remove bulky auxiliary traces."""
    from .sql_archive import compact_sql

    compact_sql(folder, ZSTD)
    for name in (
        "eplusout.eso",
        "eplusout.mtr",
        "eplusout.eio",
        "eplusout.bnd",
        "eplusout.rdd",
        "eplusout.mdd",
        "eplustbl.htm",
    ):
        (folder / name).unlink(missing_ok=True)


def simulate_case(epw, folder, context, building_id, ground, engine):
    """Run or validate one paired building/weather case using EPSM Core."""
    from . import archetypes, building, metrics

    if (
        shutil.disk_usage(
            folder.parent if folder.parent.exists() else folder.parents[2]
        ).free
        < 2 * 1024**3
    ):
        raise RuntimeError(
            "Campaign paused before simulation: less than 2 GiB free; restore disk space and resume the same immutable matrix"
        )
    sql = folder / "eplusout.sql"
    from .sql_archive import restore_sql

    restore_sql(folder, ZSTD)
    spec = archetypes.make_archetype_spec(
        epw, context.year, building_id, ground_temperatures_c=(ground,) * 12
    )
    assumptions = archetypes.describe_archetype(building_id) | {
        "ground_temperatures_c": [ground] * 12,
        "ground_basis": "EPSM Core default prescribed18 C conditioned-floor boundary, fixed across sites/years/profiles/methods; uncalibrated, no soil/snow model",
    }
    started = time.monotonic()
    receipt = building.simulate(
        epw,
        folder,
        year=context.year,
        executable=engine,
        spec=spec,
        assumptions=assumptions,
    )
    hourly, summary = metrics.extract(
        sql, year=context.year, utc_offset=context.utc_offset
    )
    compact_simulation(folder)
    from .artifact_storage import compact_artifacts

    compact_artifacts(folder)
    return hourly, summary, receipt, time.monotonic() - started


def event_metrics(comparison):
    """Catalogue event preservation once per weather case, independently of buildings."""
    record = {}
    for kind in (
        "heatwave",
        "cold_spell",
        "warm_spell",
        "relative_cold_spell",
        "high_wind",
        "temperature_jump",
        "nighttime_solar",
        "solar_out_of_bounds",
    ):
        values = comparison["by_type"].get(kind)
        if values is not None:
            record[f"{kind}_reference_hours"] = values["reference_hours"]
            record[f"{kind}_candidate_hours"] = values["candidate_hours"]
            record[f"{kind}_lost"] = values["lost_reference_count"]
            record[f"{kind}_new"] = values["false_candidate_count"]
    return record


def prepare_cases(folder, sources, args):
    """Create identical-mask comparisons and explicitly retain unsupported methods."""
    from . import anomalies
    from . import campaign_design as design
    from . import weather as W

    raw, reference, donor, model, receipt = sources
    station = receipt["station"]
    context = W.StudyContext(
        year=receipt["year"],
        latitude=receipt["latitude"],
        longitude=receipt["longitude"],
        utc_offset=receipt["utc_offset"],
        station_id=station["station_id"],
        city=station["name"],
        solar_origin=receipt.get(
            "solar_reference",
            "SMHI measured GHI where usable; STRÅNG irradiance/partition otherwise; hybrid solar reference",
        ),
        solar_source=receipt["baseline_processing"]["solar_source"],
    )
    baseline_path, baseline = W.render(
        folder,
        "reference",
        reference,
        station["elevation"],
        {"scope": "reference", "method": "reference"},
        context=context,
    )
    # BuildingSurface temperatures represent the prescribed conditioned-floor
    # boundary, not undisturbed ambient annual ground temperatures. Use Core's
    # explicit default within EnergyPlus's nominal range; omit soil/snow claims.
    ground = 18.0
    anchors = weather_anchors(raw, context, baseline)
    dump(folder / "reference-events.json", anomalies.detect_events(baseline))
    panels = design.generate_panels(
        raw,
        args.site_id,
        context.year,
        scopes=("meteorology", "solar"),
        durations=DURATIONS,
        seeds=SEEDS,
        anchors=anchors,
    )
    selected = [
        panel
        for panel in panels
        if panel.duration_hours in args.selected_durations
        and panel.scope in args.selected_scopes
        and (panel.seed is None or panel.seed in args.selected_seeds)
        and (args.sampling == "all" or panel.sampling == args.sampling)
    ]
    merge_panel_plan(
        folder / "panel-plan.json",
        [p.to_record() for p in panels],
        [p.evidence_id for p in selected],
    )
    zero = pd.Series(False, index=reference.index)
    cases = [
        {
            "case_id": "reference",
            "method": "reference",
            "scope": "reference",
            "sampling": "reference",
            "gap_hours": 0,
            "hidden_hours": 0,
            "evidence_id": "reference",
            "mask": zero,
            "path": baseline_path,
            "events": {},
            "weather_errors": {},
        }
    ]
    unavailable = []
    for panel in selected:
        methods = MET_METHODS if panel.scope == "meteorology" else SOLAR_METHODS
        for method in methods:
            case = panel.to_record() | {
                "case_id": f"{panel.evidence_id}-{method}",
                "method": method,
                "gap_hours": panel.duration_hours,
                "hidden_hours": int(panel.mask.sum()),
            }
            supported, reason = design.method_support(panel, method)
            if not supported:
                unavailable.append(case | {"status": "unsupported", "reason": reason})
                continue
            panel.withheld(
                raw
            )  # An evidence mismatch must abort, not become unavailable.
            try:
                filled, decisions = W.reconstruct(
                    reference,
                    raw,
                    donor,
                    model,
                    panel.mask,
                    method,
                    panel.scope,
                    context=context,
                    columns=panel.columns,
                )
                epw, completed = W.render(
                    folder,
                    case["case_id"],
                    filled,
                    station["elevation"],
                    case | {"decisions": decisions},
                    context=context,
                )
            except (ValueError, W.Smhi2EpwError) as exc:
                unavailable.append(case | {"status": "unavailable", "reason": str(exc)})
                continue
            errors = W.weather_errors(
                baseline, completed, panel.mask, panel.scope, context=context
            )
            exported_mask = W.export.shift_to_lst(
                pd.DataFrame({"mask": panel.mask}), context.year, context.utc_offset
            )["mask"].to_numpy()
            completed.attrs["source_tags"] = pd.Series(
                np.where(exported_mask, method, "reference")
            )
            comparison = anomalies.compare_events(baseline, completed)
            dump(epw.parent / "events.json", comparison)
            events = event_metrics(comparison)
            cases.append(
                case
                | {
                    "mask": panel.mask,
                    "path": epw,
                    "events": events,
                    "weather_errors": errors,
                }
            )
    merge_catalogue(
        folder / "case-catalogue.json",
        [{k: v for k, v in case.items() if k != "mask"} for case in cases],
        unavailable,
        ground_temperature_c=ground,
        all_unmasked_epw_fields_identical=True,
        cloud_policy="Primary/reference cloud treatment frozen for all controlled methods; production scoped ERA5 cloud augmentation is a separate operational effect",
    )
    return context, cases, ground, unavailable


def run_site_year(folder, sources, args, engine):
    """Run all six responses to a shared weather comparison, preserving pairing."""
    from . import archetypes, run_study

    context, cases, ground, unavailable = prepare_cases(folder, sources, args)
    baselines = {}
    cache = {}
    previous_path = folder / "summary.csv"
    previous = (
        load_table(previous_path).to_dict("records") if previous_path.exists() else []
    )
    rows_by_key = {(row["building_id"], row["case_id"]): row for row in previous}
    rows = list(rows_by_key.values())
    receipts = []
    for case in cases:
        weather_hash = run_study.simulation_weather_hash(case["path"])
        for building_id in args.selected_buildings:
            simulation = folder / "simulations" / building_id / case["case_id"]
            key = (building_id, weather_hash)
            if key in cache:
                hourly, summary, receipt, reused = cache[key]
                elapsed = 0.0
            else:
                hourly, summary, receipt, elapsed = simulate_case(
                    case["path"], simulation, context, building_id, ground, engine
                )
                reused = str(simulation)
                cache[key] = (hourly, summary, receipt, reused)
                receipts.append(receipt)
            if case["method"] == "reference":
                baselines[building_id] = (hourly, summary)
            baseline_hourly, baseline_summary = baselines[building_id]
            compared = run_study.load_comparison(
                baseline_hourly,
                hourly,
                baseline_summary,
                summary,
                case["mask"],
                year=context.year,
                offset=context.utc_offset,
                recovery_hours=168,
            )
            area = archetypes.ARCHETYPES[building_id]["floor_area_m2"]
            record = {
                "site_id": args.site_id,
                "year": context.year,
                "reference_station_id": context.station_id,
                "reference_station_name": context.city,
                "reference_latitude": context.latitude,
                "reference_longitude": context.longitude,
                "reference_elevation_m": sources[-1]["station"]["elevation"],
                "reference_native_required_fraction": sources[-1][
                    "native_required_fraction"
                ],
                "reference_solar_source": context.solar_source,
                "building_id": building_id,
                "anchor_name": case.get("anchor_name"),
                "anchor_utc": case.get("anchor_utc"),
                **{
                    key: case[key]
                    for key in (
                        "case_id",
                        "method",
                        "scope",
                        "sampling",
                        "gap_hours",
                        "hidden_hours",
                        "evidence_id",
                    )
                },
                "floor_area_m2": area,
                "simulation_path": reused,
                "wall_seconds": elapsed,
                "compiled_model_sha256": receipt["model_sha256"],
                **compared,
                **case["events"],
            }
            for mode in ("heating", "cooling"):
                record[f"annual_{mode}_kwh_m2"] = summary[f"annual_{mode}_kwh"] / area
                record[f"peak_{mode}_w_m2"] = summary[f"peak_{mode}_kw"] * 1000 / area
                record[f"delta_annual_{mode}_kwh_m2"] = (
                    compared[f"delta_annual_{mode}_kwh"] / area
                )
            for column, values in case["weather_errors"].items():
                for quantity, value in values.items():
                    record[f"weather_{column}_{quantity}"] = value
            rows_by_key[(building_id, case["case_id"])] = record
            rows = list(rows_by_key.values())
            save_table(folder / "summary.csv", pd.DataFrame(rows))
        print(
            f"{args.site_id}/{context.year}: {case['case_id']} ({len(rows)} paired building comparisons)",
            flush=True,
        )
    identities = {
        building_id: sorted(
            {
                row["compiled_model_sha256"]
                for row in rows
                if row["building_id"] == building_id
            }
        )
        for building_id in args.selected_buildings
    }
    if any(len(values) != 1 for values in identities.values()):
        raise ValueError("Compiled building changed within a site-year comparison")
    prior_audit_path = folder / "audit.json"
    prior_audit = (
        json.loads(prior_audit_path.read_text()) if prior_audit_path.exists() else {}
    )
    accumulated_receipts = [
        json.loads(path.read_text())
        for path in sorted((folder / "simulations").glob("*/*/receipt.json"))
    ]
    catalogue = json.loads((folder / "case-catalogue.json").read_text())
    dump(
        folder / "audit.json",
        {
            "status": "selected_batch_complete",
            "context": asdict(context),
            "sources": sources[-1],
            "building_assumptions": {
                b: archetypes.describe_archetype(b) for b in args.selected_buildings
            },
            "ground_temperature_c": ground,
            "compiled_model_identities": identities,
            "unique_simulations_in_selected_batch": len(cache),
            "unique_simulations_accumulated": len(accumulated_receipts),
            "comparisons": len(rows),
            "unavailable_weather_comparisons_in_selected_batch": len(unavailable),
            "unavailable_weather_comparisons": len(catalogue["unavailable"]),
            "core_compiler_identities": sorted(
                {r["core_compiler_sha256"] for r in accumulated_receipts}
            ),
            "engine_identities": sorted(
                {r["engine_sha256"] for r in accumulated_receipts}
            ),
            "diagnostics_accepted": all(
                r["core_diagnostics_accepted"] for r in accumulated_receipts
            ),
            "warnings": sum(r["warning_count"] for r in accumulated_receipts),
            "severe": sum(r["severe_errors"] for r in accumulated_receipts),
            "fatal": sum(r["fatal_errors"] for r in accumulated_receipts),
            "all_unmasked_epw_fields_identical": True,
            "full_matrix_site_year_complete": prior_audit.get(
                "full_matrix_site_year_complete", False
            )
            or (
                set(args.selected_buildings) == set(archetypes.ARCHETYPES)
                and set(args.selected_durations) == set(DURATIONS)
                and set(args.selected_seeds) == set(SEEDS)
                and set(args.selected_scopes) == {"meteorology", "solar"}
                and args.sampling == "all"
            ),
            "retention": "Lossless baseline XZ SQL and candidate Zstandard patches with SHA-verified baseline dictionaries; gzip hourly-load archives; hardlinked identical models; specs/results/diagnostics/errors/receipts; auxiliary ESO/MTR/EIO/BND/RDD/MDD/HTML traces removed",
        },
    )
    return rows


def report(root):
    """Combine finished batches with explicit coverage and empirical distributions."""
    from . import campaign_analysis as analysis

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    identity_columns = [
        "site_id",
        "year",
        "building_id",
        "case_id",
        "method",
        "scope",
        "sampling",
        "gap_hours",
        "evidence_id",
    ]
    quantities = [
        f"delta_{quantity}_{mode}_pct"
        for mode in ("heating", "cooling")
        for quantity in ("annual", "peak")
    ]
    display_columns = [
        *analysis.GROUPS,
        "comparisons",
        "site_years",
        "evidence_panels",
        *[
            f"{quantity}_{suffix}"
            for quantity in quantities
            for suffix in ("valid_n", "abs_p95", "site_year_abs_median_p95")
        ],
    ]
    tables = [
        load_table(path) for path in sorted((root / "datasets").glob("*/*/summary.csv"))
    ]
    rows = (
        pd.concat(tables, ignore_index=True)
        if tables
        else pd.DataFrame(columns=identity_columns)
    )
    save_table(root / "summary.csv", rows)
    alternatives = rows.loc[rows.method.ne("reference")]
    if alternatives.empty:
        # References lack error columns and must not become zero-error samples.
        grouped = pd.DataFrame(columns=display_columns)
        paired = pd.DataFrame(
            columns=[
                "site_id",
                "year",
                "building_id",
                "evidence_id",
                "scope",
                "sampling",
                "gap_hours",
                *[
                    f"{quantity}_{suffix}"
                    for quantity in quantities
                    for suffix in ("new", "old", "absolute_error_change_pp", "improved")
                ],
            ]
        )
    else:
        grouped = analysis.summarize(rows)
        paired = analysis.paired_hierarchy(rows)
    save_table(root / "error-distributions.csv", grouped)
    save_table(root / "paired-hierarchy.csv", paired)
    reference_coverage = rows.loc[
        rows.method.eq("reference"), ["site_id", "year", "building_id"]
    ].drop_duplicates()
    simulated_sites = rows[["site_id", "year"]].drop_duplicates()

    matrix_path = root / "matrix.json"
    matrix = json.loads(matrix_path.read_text()) if matrix_path.exists() else {}
    planned_sites = len(matrix.get("sites", range(6)))
    planned_years = len(matrix.get("years", range(3)))
    planned_buildings = len(matrix.get("buildings", range(6)))
    planned_location_years = planned_sites * planned_years

    audits = [
        json.loads(path.read_text())
        for path in sorted((root / "datasets").glob("*/*/audit.json"))
    ]
    completed = sum(
        bool(audit.get("full_matrix_site_year_complete")) for audit in audits
    )
    pinned_sources = {}
    unavailable_sources = {}
    for path in sorted((root / "datasets").glob("*/*/sources/receipt.json")):
        receipt = json.loads(path.read_text())
        site_id, year = path.parents[2].name, int(path.parents[1].name)
        key = (site_id, year)
        pinned_sources[key] = receipt
        if receipt.get("status") != "ready":
            unavailable_sources[key] = {
                "site_id": site_id,
                "year": year,
                "status": "unavailable",
                "reason": receipt.get("reason", "Source reference is not ready"),
            }
    for path in sorted((root / "datasets").glob("*/*/source-unavailable.json")):
        key = (path.parents[1].name, int(path.parent.name))
        if pinned_sources.get(key, {}).get("status") != "ready":
            unavailable_sources[key] = json.loads(path.read_text())
    ready_sources = sum(r.get("status") == "ready" for r in pinned_sources.values())

    # Weather availability is counted once per panel/method, never per building.
    decisions = {}
    for path in sorted((root / "datasets").glob("*/*/case-catalogue.json")):
        catalogue = json.loads(path.read_text())
        site_year = (path.parents[1].name, int(path.parent.name))
        for label, category in (("cases", "available"), ("unavailable", "unavailable")):
            for position, case in enumerate(catalogue.get(label, [])):
                if case.get("method") == "reference":
                    continue
                case_id = case.get("case_id", f"{label}-{position}")
                key = (*site_year, case_id)
                status = (
                    "unsupported" if case.get("status") == "unsupported" else category
                )
                # A retained successful case takes precedence over an old failure.
                decisions.setdefault(
                    key,
                    {
                        "method": case.get("method", "unknown"),
                        "scope": case.get("scope", "unknown"),
                        "status": status,
                    },
                )
    rates = {}
    scope_rates = {}
    for decision in decisions.values():
        method, scope = decision["method"], decision["scope"]
        for record in (
            rates.setdefault(
                method, {"available": 0, "unsupported": 0, "unavailable": 0}
            ),
            scope_rates.setdefault(
                (scope, method), {"available": 0, "unsupported": 0, "unavailable": 0}
            ),
        ):
            record[decision["status"]] += 1
    for record in [*rates.values(), *scope_rates.values()]:
        record["evaluated"] = (
            record["available"] + record["unsupported"] + record["unavailable"]
        )
        record["available_fraction"] = record["available"] / record["evaluated"]
    availability = pd.DataFrame(
        [
            {"scope": scope, "method": method, **record}
            for (scope, method), record in sorted(scope_rates.items())
        ],
        columns=[
            "scope",
            "method",
            "available",
            "unsupported",
            "unavailable",
            "evaluated",
            "available_fraction",
        ],
    )
    save_table(root / "method-availability.csv", availability)
    resolved = completed + len(unavailable_sources)
    full_matrix = planned_location_years > 0 and resolved == planned_location_years
    coverage = {
        "location_years_with_simulations": len(simulated_sites),
        "reference_building_location_years": len(reference_coverage),
        "comparison_rows": len(rows),
        "non_reference_building_comparisons": len(alternatives),
        "planned_location_years": planned_location_years,
        "planned_reference_building_location_years": planned_location_years
        * planned_buildings,
        "location_years_completed_full_matrix": completed,
        "location_years_resolved_including_unavailable_sources": resolved,
        "location_years_with_pinned_sources": len(pinned_sources),
        "reference_weather_ready_location_years": ready_sources,
        "source_unavailable": [
            unavailable_sources[key] for key in sorted(unavailable_sources)
        ],
        "catalogued_weather_comparisons": len(decisions),
        "evaluated_weather_comparisons": len(
            alternatives[["site_id", "year", "case_id"]].drop_duplicates()
        ),
        "available_weather_comparisons": sum(
            d["status"] == "available" for d in decisions.values()
        ),
        "unsupported_weather_comparisons": sum(
            d["status"] == "unsupported" for d in decisions.values()
        ),
        "unavailable_weather_comparisons": sum(
            d["status"] == "unavailable" for d in decisions.values()
        ),
        "method_availability_rates": dict(sorted(rates.items())),
        "method_rate_denominator": "Catalogued weather panel/method combinations, once per site-year and independent of buildings; unsupported combinations included",
        "complete_full_matrix": full_matrix,
        "interpretation": "Full completion means the planned matrix was evaluated with unavailable outcomes retained. Selected batches may be complete while wider coverage remains partial. Quantiles describe evaluated scenarios, not population confidence intervals.",
    }
    dump(root / "coverage.json", coverage)
    selected = grouped.reindex(columns=display_columns).round(3)
    page = '<!doctype html><html lang="en"><meta charset="utf-8"><title>Swedish weather completion sensitivity</title><style>body{font:15px system-ui;max-width:1500px;margin:40px auto;padding:0 20px;color:#17252c}table{border-collapse:collapse;font-size:12px}td,th{padding:6px;border-bottom:1px solid #ccd6dc}th{background:#edf3f5}p{max-width:1050px}</style><h1>Swedish weather completion sensitivity</h1>'
    status = (
        "The full planned matrix has been evaluated; unavailable outcomes remain explicit."
        if full_matrix
        else "Coverage is partial. Completed selected batches do not establish completion of the full planned matrix."
    )
    page += f"<p>{status} Simulated: {len(simulated_sites)}/{planned_location_years} location-years and {len(reference_coverage)}/{planned_location_years * planned_buildings} reference building/site/year combinations. Recorded rows: {len(rows)}, including {len(alternatives)} non-reference building comparisons. Full-matrix site-years evaluated: {completed}.</p>"
    page += f"<p>Pinned source receipts: {len(pinned_sources)}; eligible reference weather ready: {ready_sources}; unavailable source combinations: {len(unavailable_sources)}. Ready sources do not imply completed simulations.</p>"
    page += f"<p>Catalogued weather comparisons: {len(decisions)}; available {coverage['available_weather_comparisons']}, unsupported {coverage['unsupported_weather_comparisons']}, unavailable {coverage['unavailable_weather_comparisons']}. Availability below is counted once per weather panel/method, independently of buildings, with unsupported combinations retained in its denominator.</p>"
    if not availability.empty:
        page += availability.round(3).to_html(index=False, escape=True, border=0)
    if unavailable_sources:
        page += pd.DataFrame(coverage["source_unavailable"]).to_html(
            index=False, escape=True, border=0
        )
    page += "<p>Synthetic EPSM Core building responses; delivered sensible thermal demand, hourly peaks and peak timing. Cooling is diagnostic, including normally uncooled dwellings/schools. Models and Core's prescribed18 C conditioned-floor ground boundary are held constant across imputation methods and sites; this boundary is uncalibrated. Random seasonal outages and weather-extreme stress cases are reported separately. Optional cloud treatment is held fixed.</p><p>Pooled abs_p95 is the empirical 95th percentile of absolute percentage error across evaluated panels. site_year_abs_median_p95 first takes each site's/year's median absolute panel error, then takes the 95th percentile across those equally weighted site-years. Both are descriptive quantities, not population confidence intervals. Valid_n excludes undefined zero-reference-demand percentages. Repeated masks and buildings are not independent climate samples. These results do not establish all weather uncertainty or certainty about future extremes. Precipitation, latent cooling, plant electricity and measured-building calibration are outside this study.</p>"
    if selected.empty:
        page += "<p>No non-reference building comparisons have been evaluated; error distributions and hierarchy pairs are empty.</p>"
    else:
        page += (
            '<div style="overflow:auto">'
            + selected.to_html(index=False, escape=True, border=0, na_rep="—")
            + "</div>"
        )
    page += '<p>Full precision: <a href="summary.csv">building comparison catalogue</a>, <a href="error-distributions.csv">error distributions</a>, <a href="paired-hierarchy.csv">paired hierarchy changes</a>, <a href="method-availability.csv">weather method availability</a>, <a href="coverage.json">coverage</a>. Dataset folders retain source receipts, exact outage masks, EPWs, events, Core models and compressed SQL.</p></html>'
    temporary = root / "report.html.tmp"
    temporary.write_text(page, encoding="utf-8")
    temporary.replace(root / "report.html")


def main():
    """Execute selected stages of one fixed matrix, with resumable scientific receipts."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--phase", choices=("plan", "sources", "run"), default="plan")
    parser.add_argument("--select-sites")
    parser.add_argument("--select-years")
    parser.add_argument("--select-buildings")
    parser.add_argument("--select-durations")
    parser.add_argument("--select-seeds")
    parser.add_argument("--select-scopes")
    parser.add_argument(
        "--sampling",
        choices=("all", "random_seasonal", "weather_extremes"),
        default="all",
    )
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument(
        "--energyplus",
        type=Path,
        default=Path("/Applications/OpenStudio-3.9.0/EnergyPlus/energyplus-24.2.0"),
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    runtime = pin_runtime(args.output)
    from smhi2epw.errors import IngestionError

    from . import archetypes
    from . import campaign_sources as sources

    selected_sites = selection(
        args.select_sites, [s["site_id"] for s in sources.SITES], "sites"
    )
    selected_years = selection(args.select_years, sources.YEARS, "years", int)
    args.selected_buildings = selection(
        args.select_buildings, archetypes.ARCHETYPES, "buildings"
    )
    args.selected_durations = selection(
        args.select_durations, DURATIONS, "durations", int
    )
    args.selected_seeds = selection(args.select_seeds, SEEDS, "seeds", int)
    args.selected_scopes = selection(
        args.select_scopes, ("meteorology", "solar"), "scopes"
    )
    if not 1 <= args.workers <= 3:
        raise ValueError("Use one to three workers to bound engine/API and disk load")
    matrix = {
        "sites": sources.SITES,
        "years": sources.YEARS,
        "buildings": {
            b: archetypes.describe_archetype(b) for b in archetypes.ARCHETYPES
        },
        "durations_hours": DURATIONS,
        "seasonal_seeds": SEEDS,
        "meteorology_methods": MET_METHODS,
        "solar_methods": SOLAR_METHODS,
        "weather_extreme_anchors": ["warmest_original_hour", "coldest_original_hour"],
        "optional_event_anchors": ["heatwave_hottest_hour", "cold_spell_coldest_hour"],
        "engine_sha256": fingerprint(args.energyplus),
        "sql_archiver": {
            "path": str(ZSTD.resolve()),
            "sha256": fingerprint(ZSTD),
            "version": subprocess.check_output(
                [str(ZSTD), "--version"], text=True
            ).strip(),
        },
        "runtime": runtime,
    }
    matrix_path = args.output / "matrix.json"
    serialized = json.loads(json.dumps(matrix, default=str))
    if matrix_path.exists() and json.loads(matrix_path.read_text()) != serialized:
        raise ValueError("Campaign matrix changed; choose a new output directory")
    dump(matrix_path, matrix)
    if args.phase == "plan":
        print(
            json.dumps(
                {
                    "locations": 6,
                    "years": 3,
                    "buildings": 6,
                    "reference_combinations": 108,
                    "paired_methods": MET_METHODS,
                    "output": str(args.output),
                },
                indent=2,
            )
        )
        return
    cache = args.output / "raw-cache"
    cache.mkdir(exist_ok=True)
    seed = PREVIOUS / "sources/raw-cache"
    if seed.exists():
        for path in seed.iterdir():
            if path.is_file() and not (cache / path.name).exists():
                shutil.copy2(path, cache / path.name)
    tasks = [
        (site, year)
        for site in sources.SITES
        if site["site_id"] in selected_sites
        for year in selected_years
    ]

    def execute(task):
        site, year = task
        folder = args.output / "datasets" / site["site_id"] / str(year)
        folder.mkdir(parents=True, exist_ok=True)
        try:
            data = sources.prepare_location_year(folder, site, year, cache)
        except (sources.SourceUnavailable, IngestionError) as exc:
            dump(
                folder / "source-unavailable.json",
                {
                    "site_id": site["site_id"],
                    "year": year,
                    "status": "unavailable",
                    "reason": str(exc),
                },
            )
            print(f"{site['site_id']}/{year}: source unavailable: {exc}", flush=True)
            return
        if args.phase == "sources":
            print(f"{site['site_id']}/{year}: pinned eligible sources", flush=True)
            return
        local = argparse.Namespace(**vars(args))
        local.site_id = site["site_id"]
        run_site_year(folder, data, local, args.energyplus)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(execute, tasks))
    report(args.output)
    print(args.output / "report.html", flush=True)


if __name__ == "__main__":
    main()
