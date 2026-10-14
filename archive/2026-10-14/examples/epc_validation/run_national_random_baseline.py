"""Random baseline check of the entire audited MF SE.02/F/district-heat cohort.

Draw before preparation, retain failed cases without replacement, compile local
actual-year weather, and evaluate the frozen baseline without fitting parameters.
Existing observations remain retrospective; this is not new independent evidence.
"""

from __future__ import annotations

import argparse
import copy
import html
import json
import logging
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
from pyproj import Transformer

from epsm_workflows.archetype_pilot import _geometry, _model_footprint
from epsm_workflows.campaign import atomic_json, file_sha256
from epsm_workflows.cohort_transfer.common import cleaned, read


def progress(root, stage, **values):
    record = {
        "stage": stage,
        "updated_utc": datetime.now(timezone.utc).isoformat(),
        **values,
    }
    atomic_json(root / "execution.json", record)
    print(json.dumps(record), flush=True)


def select(root, inventory, membership, membership_source, *, size=200, seed=20261005):
    """Uniform building draw, with sorted IDs and no target-dependent eligibility."""
    if root.exists():
        raise FileExistsError("Never redraw an existing sample")
    features = pd.read_parquet(inventory)
    audit = pd.read_parquet(membership)
    assert not features.building_id.duplicated().any()
    assert not audit.building_id.duplicated().any()
    eligible_ids = set(audit.loc[audit.eligible, "building_id"])
    eligible = (
        features[
            features.building_id.isin(eligible_ids)
            & (features.use_group == "multi_family")
            & (features.era == "SE.02")
            & (features.ventilation == "F")
            & (features.heating_family == "district_heat")
            & (
                features.correction_status
                == "normal_use_adjusted_pre_weather_correction"
            )
        ]
        .sort_values("building_id")
        .reset_index(drop=True)
    )
    if not 0 < size <= len(eligible):
        raise ValueError("Sample size outside population")
    # NumPy PCG64; sorting the frame makes the draw independent of parquet order.
    chosen = eligible.iloc[
        np.random.default_rng(seed).choice(len(eligible), size, replace=False)
    ]
    chosen = chosen.sort_values("building_id").reset_index(drop=True)
    source = pd.read_parquet(membership_source).set_index("building_id")
    rows = cleaned(chosen.to_dict("records"))
    for row in rows:
        origin = source.loc[row["building_id"]]
        for key in ("purpose", "purpose2", "purpose3", "purpose4", "purpose5"):
            row[key] = cleaned(origin[key])
    root.mkdir(parents=True)
    atomic_json(root / "selected-inputs.json", rows)
    # This inventory contains IDs/inputs only. No heating targets are read here.
    atomic_json(
        root / "population-ids.json",
        eligible[["building_id", "property_key", "municipality"]].to_dict("records"),
    )
    record = {
        "schema_version": 1,
        "design": "simple random sample without replacement of buildings",
        "rng": "numpy.default_rng PCG64 on sorted building IDs",
        "seed": seed,
        "population_buildings": len(eligible),
        "population_properties": eligible.property_key.nunique(),
        "population_municipalities": eligible.municipality.nunique(),
        "sample_buildings": len(chosen),
        "sample_properties": chosen.property_key.nunique(),
        "sample_municipalities": chosen.municipality.nunique(),
        "inclusion_probability": size / len(eligible),
        "scope": "MF SE.02 / F / district heat / audited membership / normal-use-adjusted space heat",
        "sampling_precedes_physics_preparation": True,
        "failed_cases_replaced": False,
        "previously_studied_buildings_excluded": False,
        "prior_study_flag_counts": Counter(
            bool(r["prior_study_property"]) for r in rows
        ),
        "measurement_years": dict(Counter(r["measurement_year"] for r in rows)),
        "municipalities": dict(Counter(r["municipality"] for r in rows)),
        "source_hashes": {
            str(p.resolve()): file_sha256(p)
            for p in (inventory, membership, membership_source)
        },
        "selected_inputs_sha256": file_sha256(root / "selected-inputs.json"),
        "population_ids_sha256": file_sha256(root / "population-ids.json"),
        "accuracy_target_coverage20": 0.90,
        "fitted_parameters": {},
        "limitations": [
            "Existing national observations already used in earlier statistical studies; retrospective baseline check.",
            "Building sampling weights repeated properties by their building counts; uncertainty resamples properties.",
            "No preparation, weather or numerical failure may be silently replaced or removed from all-sample coverage.",
        ],
    }
    atomic_json(root / "sampling-manifest.json", cleaned(record))
    progress(
        root,
        "sample_frozen",
        buildings=len(rows),
        properties=chosen.property_key.nunique(),
        municipalities=chosen.municipality.nunique(),
    )


def prepare_geometry(rows):
    """Audit bounded footprint cleanup, then resolve geometry in native Core.

    Original outlines and preparation receipts are retained; only accepted cleanup
    is used. Unusual heights are review flags; unclear roofs use a declared flat
    fallback at their measured representative height, preserving the source label.
    """
    from epsm_core import FacadeSpec, GlazingSpec
    from epsm_core.pipeline import resolve_geometry
    from epsm_workflows.cohort_transfer.preparation import (
        build_geometry,
        prepare_footprint,
        roof_model_inputs,
    )

    results = copy.deepcopy(rows)
    for row in results:
        try:
            storeys = row["storeys"]
            if (
                isinstance(storeys, bool)
                or storeys is None
                or not np.isfinite(storeys)
                or storeys != int(storeys)
                or not 1 <= storeys <= 100
            ):
                raise ValueError("invalid_or_missing_storeys")
            if row.get("heated_basement_floors") not in (0, 1):
                raise ValueError("unknown_or_multiple_heated_basements")
            row.update(roof_model_inputs(row))
            height_inputs = (
                {**row, "roof_form": "flat"} if row["roof_fallback_applied"] else row
            )
            roof, floor_height = _geometry(height_inputs, int(storeys))
            original = row.get("source_geometry_wkb_hex", row["geometry_wkb_hex"])
            proxy, error = _model_footprint(bytes.fromhex(original), roof)
            row.update(
                source_geometry_wkb_hex=original,
                geometry_wkb_hex=proxy,
                geometry_proxy_area_error=error,
                floor_height_m=floor_height,
                geometry_preparation_policy="core_bounded_preparation_flat_fallback_v4",
                geometry_review_flags=(
                    ["floor_height_outside_typical_range"]
                    if not 2.4 <= floor_height <= 3.8
                    else []
                ),
            )
            prepared, receipt = prepare_footprint(row)
            row["footprint_preparation"] = receipt
            if prepared.status != "accepted":
                raise ValueError(
                    f"footprint_preparation_{prepared.status}: {'; '.join(prepared.reasons)}"
                )
            row["geometry_wkb_hex"] = prepared.cleaned_geometry.wkb.hex()
            native = SimpleNamespace(
                geometry=build_geometry(row, {}),
                facade=FacadeSpec(
                    default_wwr=0.2,
                    # Geometry-only facade; these optical values are not simulated.
                    glazing=GlazingSpec(
                        u_value_w_m2k=2.2, solar_heat_gain_coefficient=0.7
                    ),
                ),
            )
            resolve_geometry(native, panelize=True)
            row["native_geometry_preflight"] = "accepted"
        except Exception as exc:
            row["input_preparation_failure"] = str(exc)
    return results


def compile_weather(root, rows, workers):
    from epsm_workflows.weather import compile_measurement_weather
    from smhi2epw import compile_epw

    transform = Transformer.from_crs(3006, 4326, always_xy=True)
    groups = {}
    for row in rows:
        if row.get("input_preparation_failure"):
            continue
        groups.setdefault((row["municipality"], row["measurement_year"]), []).append(
            row
        )
    jobs = []
    for (municipality, year), group in sorted(groups.items()):
        # Local point is the mean sampled building centroid in municipality/year.
        easting = np.mean([r["centroid_e"] for r in group])
        northing = np.mean([r["centroid_n"] for r in group])
        lon, lat = transform.transform(easting, northing)
        jobs.append(
            {
                "key": f"{municipality}:{year}",
                "municipality": municipality,
                "year": year,
                "latitude": lat,
                "longitude": lon,
                "maximum_building_to_query_point_km": max(
                    np.hypot(r["centroid_e"] - easting, r["centroid_n"] - northing)
                    / 1000
                    for r in group
                ),
            }
        )
    atomic_json(root / "weather-jobs.json", cleaned(jobs))
    shared_cache = root / "weather/raw-cache"
    shared_cache.mkdir(parents=True, exist_ok=True)

    def cached_compiler(config):
        return compile_epw(
            replace(
                config,
                cache_dir=str(shared_cache),
                provenance_path=str(
                    Path(config.output_path).with_name("source-provenance.json")
                ),
            )
        )

    def one(job):
        folder = root / "weather" / job["key"].replace(":", "-")
        output = folder / f"weather-{job['year']}.epw"
        receipt = folder / "weather-provenance.json"
        if output.exists() and receipt.exists():
            prior = read(receipt)
            if file_sha256(output) != prior["weather_sha256"]:
                raise ValueError("Existing weather checksum changed")
            return job | {
                "status": "completed",
                "path": str(output),
                "sha256": file_sha256(output),
                "provenance": str(receipt),
            }
        # Partial files from interrupted/failed compilation are retained separately.
        if folder.exists():
            index = 0
            while (folder.parent / f"{folder.name}-interrupted-{index}").exists():
                index += 1
            folder.rename(folder.parent / f"{folder.name}-interrupted-{index}")
        try:
            climate, report = compile_measurement_weather(
                f"{job['year']}-01-01",
                f"{job['year']}-12-31",
                folder,
                latitude=job["latitude"],
                longitude=job["longitude"],
                compiler=cached_compiler,
            )
            result = report["result"]
            report["config"]["cache_dir"] = str(shared_cache)
            report["cache_note"] = (
                "Shared raw cache; scientific compiler settings unchanged"
            )
            atomic_json(receipt, report)
            record = job | {
                "status": "completed",
                "path": climate.epw_path,
                "sha256": climate.weather_sha256,
                "provenance": str(receipt),
                "station": result["station"],
                "interpolated_fraction": result["interpolated_fraction"],
                "required_reconstructed_fraction": result["report"].get(
                    "required_reconstructed_fraction", 0.0
                ),
                "cross_station_filled_hours": result["report"].get(
                    "cross_station_filled_hours", {}
                ),
                "gap_fallback_sources": result["report"].get(
                    "gap_fallback_sources", []
                ),
                "source_provenance": str(folder / "source-provenance.json"),
                "station_distance_km": result.get("coordinate_distance_km"),
                "radiation_station_distance_km": result.get(
                    "radiation_station_distance_km"
                ),
            }
        except Exception as exc:
            record = job | {"status": "failed", "reason": str(exc)}
            atomic_json(folder / "failure.json", record)
        atomic_json(folder / "receipt.json", record)
        return record

    records = []
    progress(
        root,
        "compiling_local_actual_year_weather",
        jobs=len(jobs),
        completed=0,
        failed=0,
    )
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, job): job for job in jobs}
        for future in as_completed(futures):
            records.append(future.result())
            atomic_json(
                root / "weather-status.json", sorted(records, key=lambda r: r["key"])
            )
            progress(
                root,
                "compiling_local_actual_year_weather",
                jobs=len(jobs),
                completed=sum(r["status"] == "completed" for r in records),
                failed=sum(r["status"] == "failed" for r in records),
            )
    manifest = {r["key"]: r for r in records if r["status"] == "completed"}
    atomic_json(root / "weather-manifest.json", manifest)
    for row in rows:
        if (
            not row.get("input_preparation_failure")
            and f"{row['municipality']}:{row['measurement_year']}" not in manifest
        ):
            row["input_preparation_failure"] = "measurement_year_weather_failed"
    return manifest


def height_selection(root, municipalities, directory):
    """Combine existing national height inventory selections without new inference."""
    import duckdb

    paths = []
    sources = {}
    found = set()
    for path in sorted(directory.glob("*.json")):
        record = read(path)
        code = str(record.get("selection", {}).get("value"))
        if code not in municipalities:
            continue
        selected = Path(record["building_membership"]["path"])
        paths.append(str(selected))
        found.add(code)
        sources[str(path)] = file_sha256(path)
        sources[str(selected)] = file_sha256(selected)
    if found != municipalities:
        raise ValueError(
            f"Missing existing height selection: {sorted(municipalities - found)}"
        )
    output = root / "context-height-selection.parquet"
    if not output.exists():
        with duckdb.connect() as db:
            df = db.execute(
                "SELECT DISTINCT building_id,owner_tile_id FROM read_parquet(?,union_by_name=true,hive_partitioning=false)",
                [paths],
            ).fetchdf()
            df.to_parquet(output, index=False)
    atomic_json(root / "context-height-selection-sources.json", sources)
    return output


def prepare_campaign(root, rows, inventory, *, recover_context_shadows=False):
    from epsm_workflows.cohort_transfer.preparation import prepare

    campaign = root / "campaign"
    if campaign.exists():
        return campaign
    # The national parquet serialises this field; raw EPC hydration uses a dict.
    # Decode its representation without changing any declared measure or date.
    rows = copy.deepcopy(rows)
    for row in rows:
        if isinstance(row.get("reported_completed_measures"), str):
            row["reported_completed_measures"] = json.loads(
                row["reported_completed_measures"]
            )
    stock = inventory.parent
    # Selection and all simulation assumptions are frozen before loading energy.
    identifiers = {r["building_id"] for r in rows}
    targets = pd.concat(
        [
            pd.read_parquet(stock / "targets-development.parquet"),
            pd.read_parquet(stock / "targets-test-sealed.parquet"),
        ],
        ignore_index=True,
    )
    targets = targets[targets.building_id.isin(identifiers)]
    assert not targets.building_id.duplicated().any()
    assert set(targets.building_id) == identifiers
    targets.to_parquet(root / "selected-targets.parquet", index=False)
    atomic_json(root / "cohort-inputs.json", cleaned(rows))
    selection = height_selection(
        root,
        {str(r["municipality"]) for r in rows},
        Path(
            "/Users/ssanjay/GitHub/footprint_height_extraction/outputs/sweden/inventory/selections"
        ),
    )
    config = {
        "schema_version": 1,
        "cohort_file": str(root / "cohort-inputs.json"),
        "targets_file": str(root / "selected-targets.parquet"),
        "membership_publication": "/Users/ssanjay/GitHub/epc_join/outputs/releases/2.0.0",
        "weather_manifest": str(root / "weather-manifest.json"),
        "weather_directory": str(root / "weather/unavailable"),
        "catalogue_climate_zone": "SE.Zone3",
        "ventilation": "F",
        "heating_family": "district_heat",
        "study_kind": "random_national_baseline_check_no_calibration",
        "workers": 8,
        "parallel_backend": "processes",
        "recover_context_shadows": recover_context_shadows,
        "bootstrap_resamples": 1000,
        "context_sources": {
            "publication": "/Users/ssanjay/GitHub/epc_join/outputs/releases/2.0.0",
            "height_release": "/Users/ssanjay/GitHub/footprint_height_extraction/outputs/sweden/harmonized/1.0.0",
            "height_selection": str(selection),
            "radius_m": 100,
        },
        "note": "Uniform draw from all audited eligible buildings before preparation; no fit, no replacement of failures. Same Data 1.5 physical recipes and assumptions as v11; local actual-year weather. Catalogue climate labels do not change these construction values.",
    }
    atomic_json(root / "config.json", config)
    progress(root, "preparing_native_specs_and_measured_context", selected=len(rows))
    return prepare(root / "config.json", campaign)


def evaluate(root, campaign, workers):
    from epsm_workflows.cohort_transfer.common import bootstrap, digest, guard, metrics
    from epsm_workflows.cohort_transfer.preparation import TargetStore
    from epsm_workflows.cohort_transfer.simulation import Evaluator

    guard(campaign)
    rows = read(campaign / "features.json")
    assert len(rows) == read(root / "sampling-manifest.json")["sample_buildings"], (
        "Keep every selected building in the denominator"
    )
    frozen = campaign / "baseline-frozen.json"
    if not frozen.exists():
        atomic_json(
            frozen,
            {
                "parameters": {},
                "fitted": False,
                "campaign_manifest_sha256": digest(read(campaign / "manifest.json")),
                "sampling_manifest_sha256": file_sha256(
                    root / "sampling-manifest.json"
                ),
            },
        )
    predictions_path = campaign / "baseline-predictions-before-targets.json"
    if not predictions_path.exists():
        evaluator = Evaluator(campaign, workers=workers)
        try:
            progress(
                root,
                "running_native_baseline",
                selected=len(rows),
                prepared=sum(r["status"] == "prepared" for r in rows),
                workers=workers,
            )
            values = evaluator.batch(rows, {"baseline": {}})
        finally:
            evaluator.close()
        predictions = [
            {
                "building_id": r["building_id"],
                "prediction": values[r["building_id"], "baseline"],
            }
            for r in rows
        ]
        atomic_json(predictions_path, predictions)
    else:
        predictions = read(predictions_path)
    assert [r["building_id"] for r in predictions] == [r["building_id"] for r in rows]
    observations = TargetStore(campaign).evaluation(rows, frozen)
    values = [r["prediction"] for r in predictions]
    prepared = [i for i, r in enumerate(rows) if r["status"] == "prepared"]
    report = {
        "sampling": read(root / "sampling-manifest.json"),
        "all_selected_metrics": metrics(observations, values),
        "all_selected_uncertainty": bootstrap(observations, values, 1000),
        "prepared_subset_metrics": metrics(
            [observations[i] for i in prepared], [values[i] for i in prepared]
        )
        if prepared
        else None,
        "preparation": read(campaign / "audit.json"),
        "calibrated": False,
        "parameter_overrides": {},
        "independent_confirmation": False,
    }
    details = []
    original = {r["building_id"]: r for r in read(root / "selected-inputs.json")}
    for row, target, pred in zip(rows, observations, values, strict=True):
        source = original[row["building_id"]]
        error = (
            None if pred is None else 100 * (pred - target["target"]) / target["target"]
        )
        details.append(
            {
                "building_id": row["building_id"],
                "property_key": row["property_key"],
                "municipality": row["municipality"],
                "measurement_year": row["measurement_year"],
                "reported_kwh_m2": target["target"],
                "predicted_kwh_m2": pred,
                "signed_error_percent": error,
                "inside20": error is not None and abs(error) < 20,
                "status": "accepted"
                if pred is not None
                else "simulation_rejected"
                if row["status"] == "prepared"
                else row["status"],
                "atemp_m2": source["atemp_m2"],
                "storeys": source["storeys"],
                "roof_form": source["roof_form"],
                "modelled_roof": row.get("modelled_roof"),
                "roof_fallback_applied": row.get("roof_fallback_applied", False),
                "roof_assumption": row.get("roof_assumption"),
                "height_m": source["height_m"],
                "heated_basement_floors": source["heated_basement_floors"],
                "prior_study_property": source["prior_study_property"],
            }
        )
    report["roof_completion_groups"] = roof_completion_metrics(details)
    atomic_json(root / "building-results.json", cleaned(details))
    atomic_json(root / "evaluation.json", cleaned(report))
    guard(campaign)
    render_report(root, report, details)
    progress(
        root,
        "completed",
        selected=len(rows),
        metrics=report["all_selected_metrics"],
        prepared_subset_metrics=report["prepared_subset_metrics"],
    )
    return report


def roof_completion_metrics(details):
    """Report declared fallback and measured-classification results separately."""
    from epsm_workflows.cohort_transfer.common import metrics

    groups = {}
    for name, fallback in (
        ("unclear_flat_fallback", True),
        ("classified_roofs", False),
    ):
        selected = [
            r
            for r in details
            if (
                r.get("roof_form") == "unclear" or r.get("roof_fallback_applied", False)
            )
            == fallback
        ]
        groups[name] = metrics(
            [
                {"target": r["reported_kwh_m2"], "property_key": r["property_key"]}
                for r in selected
            ],
            [r["predicted_kwh_m2"] for r in selected],
        )
    return groups


def render_report(root, report, details):
    sample = report["sampling"]
    m = report["all_selected_metrics"]
    text = f"""# National MF SE.02 random baseline check

Uniform building draw: {sample["sample_buildings"]} buildings / {sample["sample_properties"]} properties /
{sample["sample_municipalities"]} municipalities from {sample["population_buildings"]} eligible buildings.
Seed {sample["seed"]}. Sample drawn before preparation, no failed building replaced.

All-selected results: {m["accepted"]}/{m["attempted"]} accepted; {m["coverage20"]:.1%} within 20%,
{m["coverage10"]:.1%} within 10%; accepted-case MAE {m["mae_kwh_m2"]:.3f} kWh/m²/year,
signed bias {m["bias_kwh_m2"]:+.3f}. Failures remain in coverage denominators.

Baseline Data 1.5: window U 2.2 W/(m² K), additional infiltration 0.20 L/s/m² Atemp,
native weather and neighbour context. No calibration or fitted overrides. Roof/geometry
completion uses bounded footprint preparation. Unclear roofs use a declared flat
fallback at measured representative height; the source classification remains unchanged.
Other unresolved inputs remain explicit failures.
Local weather points, selected stations, distances, source/interpolation limitations and hashes
are retained under weather/. Weather compilation does not reproduce EPC correction methods. Bounded nearby-observation
recovery is enabled, with original missing fractions and donor intervals retained in receipts.
Complete EPWs do not establish local weather accuracy; heavy substitutions need review.
The SE.Zone3 catalogue key is a construction-recipe lookup, not a geographic climate-zone claim.

This is a retrospective baseline check, not fresh independent national certification.
Building weights are uniform; property bootstrap handles within-property dependence.
Prepared-only metrics are reported separately and cannot replace all-selected coverage.
Catalogue/Core/Data assumptions remain frozen; the 90% within-20% target is retained.
"""
    text += "\nRoof completion groups (failures retained in each denominator):\n"
    for name, group in report.get("roof_completion_groups", {}).items():
        coverage = (
            f"{group['coverage20']:.1%}" if group["coverage20"] is not None else "n/a"
        )
        text += f"- {name}: {group['accepted']}/{group['attempted']} accepted; {coverage} within 20%.\n"
    (root / "report.md").write_text(text)
    rows = "".join(
        "<tr>"
        + "".join(
            f"<td>{html.escape(str(r[k]))}</td>"
            for k in [
                "municipality",
                "property_key",
                "building_id",
                "status",
                "reported_kwh_m2",
                "predicted_kwh_m2",
                "signed_error_percent",
            ]
        )
        + "</tr>"
        for r in details
    )
    (root / "report.html").write_text(
        f'<!doctype html><meta charset="utf-8"><title>National random baseline</title><style>body{{font:16px system-ui;margin:30px;line-height:1.5}}pre{{white-space:pre-wrap}}table{{border-collapse:collapse;font-size:12px}}td,th{{padding:8px;border-bottom:1px solid #ccc}}</style><pre>{html.escape(text)}</pre><table><tr><th>Municipality</th><th>Property</th><th>Building</th><th>Status</th><th>Reported kWh/m²</th><th>Predicted</th><th>Error %</th></tr>{rows}</table>'
    )


def run(root, inventory, weather_workers, workers, *, recover_context_shadows=False):
    sampling = read(root / "sampling-manifest.json")
    assert (
        file_sha256(root / "selected-inputs.json") == sampling["selected_inputs_sha256"]
    )
    for name, sha in sampling["source_hashes"].items():
        assert file_sha256(name) == sha, f"Sampling source changed: {name}"
    if read(root / "execution.json")["stage"] == "completed":
        return read(root / "evaluation.json")
    rows = prepare_geometry(read(root / "selected-inputs.json"))
    atomic_json(
        root / "geometry-preparation.json",
        [
            {
                "building_id": r["building_id"],
                "failure": r.get("input_preparation_failure"),
                "geometry_proxy_area_error": r.get("geometry_proxy_area_error"),
                "source_roof_form": r.get("roof_form"),
                "modelled_roof": r.get("modelled_roof"),
                "roof_fallback_applied": r.get("roof_fallback_applied", False),
                "roof_assumption": r.get("roof_assumption"),
            }
            for r in rows
        ],
    )
    if not (root / "campaign").exists():
        compile_weather(root, rows, weather_workers)
    campaign = prepare_campaign(
        root, rows, inventory, recover_context_shadows=recover_context_shadows
    )
    return evaluate(root, campaign, workers)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stage", choices=["select", "run"], required=True)
    parser.add_argument(
        "--inventory",
        type=Path,
        default=Path("output/stock-national-2026-10-02-v2/features.parquet"),
    )
    parser.add_argument(
        "--membership",
        type=Path,
        default=Path(
            "output/archetype-membership-gis-led-2026-10-04-v2/national-membership-audit.parquet"
        ),
    )
    parser.add_argument(
        "--membership-source",
        type=Path,
        default=Path(
            "output/archetype-membership-gis-led-2026-10-04-v2/national-membership-source.parquet"
        ),
    )
    parser.add_argument("--size", type=int, default=200)
    parser.add_argument("--seed", type=int, default=20261005)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--weather-workers", type=int, default=4)
    parser.add_argument(
        "--recover-context-shadows", action="store_true",
        help="Opt in to Core's audited 1 m then 2 m context shadow-recovery fallback",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    root = args.output.resolve()
    if args.stage == "select":
        select(
            root,
            args.inventory,
            args.membership,
            args.membership_source,
            size=args.size,
            seed=args.seed,
        )
    else:
        start = time.monotonic()
        try:
            run(
                root, args.inventory, args.weather_workers, args.workers,
                recover_context_shadows=args.recover_context_shadows,
            )
        except Exception as exc:
            progress(
                root,
                "failed",
                reason=str(exc),
                elapsed_seconds=time.monotonic() - start,
            )
            raise


if __name__ == "__main__":
    main()
