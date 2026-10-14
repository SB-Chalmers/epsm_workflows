"""Restartable actual-year weather preparation for a frozen MF selection.

Existing EPWs are reused only after SHA-256 and complete-calendar validation.
Raw-cache files are hard-linked into the new study's cache; atomic cache writes
replace only new-study paths. Existing studies and their weather stay unchanged.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from shutil import copy2

import numpy as np
from epsm_core.weather import validate_epw_calendar
from pyproj import Transformer

from .campaign import atomic_json, file_sha256
from .cohort_transfer.common import cleaned, digest, read
from .weather import compile_measurement_weather


def weather_jobs(rows):
    """Resolve municipality/year query points from geography, never heating."""
    groups = {}
    for row in rows:
        year = row["measurement_year"]
        if isinstance(year, bool) or int(year) != year:
            raise ValueError("Weather requires an integer measurement year")
        if not all(math.isfinite(row[key]) for key in ("centroid_e", "centroid_n")):
            raise ValueError("Weather requires finite building coordinates")
        key = (str(row["municipality"]).zfill(4), int(year))
        groups.setdefault(key, []).append(row)
    transform = Transformer.from_crs(3006, 4326, always_xy=True)
    jobs = []
    for (municipality, year), group in sorted(groups.items()):
        easting = float(np.mean([r["centroid_e"] for r in group]))
        northing = float(np.mean([r["centroid_n"] for r in group]))
        longitude, latitude = transform.transform(easting, northing)
        jobs.append({
            "key": f"{municipality}:{year}", "municipality": municipality,
            "year": year, "latitude": latitude, "longitude": longitude,
            "selected_buildings": len(group),
            "maximum_building_to_query_point_km": max(
                np.hypot(r["centroid_e"] - easting, r["centroid_n"] - northing) / 1000
                for r in group
            ),
        })
    return cleaned(jobs)


def _validated(record, year):
    path = Path(record["path"])
    if file_sha256(path) != record["sha256"]:
        raise ValueError("Existing weather checksum changed")
    calendar = validate_epw_calendar(path, year)
    return dict(record, path=str(path.resolve()), calendar_validation=calendar)


def classify_weather_failure(record):
    """Distinguish missing offline payloads from native weather-quality failures."""
    if record.get("status") == "failed":
        constrained = "Offline weather cache miss:" in record.get("reason", "")
        return dict(
            record,
            failure_classification="offline_cache_unavailable_requires_external_authorization"
            if constrained else "weather_preparation_or_validation_failure",
            external_authorization_required=constrained,
        )
    return record


def _seed_cache(destination, sources):
    """Share immutable cached payloads; subsequent replacements affect only copies."""
    destination.mkdir(parents=True, exist_ok=True)
    seeded = 0
    for source in sources:
        source = Path(source)
        if not source.exists() or source.resolve() == destination.resolve():
            continue
        for path in source.rglob("*"):
            if not path.is_file() or path.name.startswith(".smhi2epw-"):
                continue
            target = destination / path.relative_to(source)
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                target.hardlink_to(path)
            except OSError:
                copy2(path, target)
            seeded += 1
    return seeded


def recover_weather(
    selection, existing, output, workers=4, *, raw_cache=None,
    compiler=None, retry_failed=False, progress=True, cache_only=False,
):
    """Prepare and validate weather, retaining successes, failures and restarts.

    ``selection`` is a cohort JSON path or rows; ``existing`` is the prepared
    configuration JSON path, its weather_manifest, or a compatible config.
    Failed jobs are preserved and retried only with explicit ``retry_failed``.
    The return contains a completed weather manifest and all job status records.
    """
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("Weather workers must be a positive integer")
    rows = read(selection) if isinstance(selection, (str, Path)) else selection
    prior = read(existing) if isinstance(existing, (str, Path)) else existing
    prior = prior.get("weather_manifest", prior)
    root = Path(output).resolve()
    jobs = weather_jobs(rows)
    identity = digest(jobs)
    root.mkdir(parents=True, exist_ok=True)
    design_path = root / "weather-design.json"
    design = {
        "schema_version": 1, "identity": identity, "jobs": jobs,
        "selection_buildings": len(rows), "query_point_policy": "mean sampled building centroid per municipality/year",
        "failed_cases_replaced": False, "observed_heating_used": False,
        "network_requests_allowed": not cache_only,
    }
    if design_path.exists() and read(design_path) != design:
        raise ValueError("Frozen weather selection changed; use a new output directory")
    atomic_json(design_path, design)
    shared_cache = root / "weather" / "raw-cache"
    cache_sources = [Path(raw_cache)] if raw_cache is not None else sorted({
        Path(r["path"]).parent.parent / "raw-cache"
        for r in prior.values() if r.get("status") == "completed"
    })
    seeded = _seed_cache(shared_cache, cache_sources)

    def cached_compiler(config):
        from smhi2epw import compile_epw

        updated = replace(
            config, cache_dir=str(shared_cache),
            provenance_path=str(Path(config.output_path).with_name("source-provenance.json")),
            # Online authorization covers SMHI only. Automatic ERA5/Open-Meteo
            # fallback is permitted solely when every HTTP request is prohibited.
            weather_policy="automatic" if cache_only else "strict",
        )
        if compiler is not None:
            return compiler(updated)
        if cache_only:
            from smhi2epw.ingestion import CachedClient, IngestionError

            class OfflineCacheClient(CachedClient):
                def get_text(self, url, suffix="txt"):
                    path = self._cache_path(url, suffix)
                    if path is None or not self._cache_valid(path):
                        raise IngestionError(f"Offline weather cache miss: {url}")
                    return super().get_text(url, suffix=suffix)

            return compile_epw(updated, client=OfflineCacheClient(cache_dir=str(shared_cache)))
        return compile_epw(updated)

    def one(job):
        folder = root / "weather" / job["key"].replace(":", "-")
        receipt_path = folder / "receipt.json"
        if receipt_path.exists():
            receipt = read(receipt_path)
            if receipt.get("job_identity") != digest(job):
                raise ValueError("Frozen weather job changed")
            if receipt["status"] == "completed":
                return _validated(receipt, job["year"])
            if not retry_failed:
                return classify_weather_failure(receipt)
        if job["key"] in prior and prior[job["key"]].get("status") == "completed":
            previous = _validated(prior[job["key"]], job["year"])
            # Preserve the original compiled query point and its scientific identity.
            return dict(previous, job_identity=digest(job), selected_buildings=job["selected_buildings"],
                        requested_query_point={"latitude": job["latitude"], "longitude": job["longitude"]},
                        reused_existing_weather=True)
        if folder.exists():
            index = 0
            while (folder.parent / f"{folder.name}-interrupted-{index}").exists():
                index += 1
            folder.rename(folder.parent / f"{folder.name}-interrupted-{index}")
        record = dict(job, job_identity=digest(job), status="failed", reused_existing_weather=False)
        try:
            climate, report = compile_measurement_weather(
                f"{job['year']}-01-01", f"{job['year']}-12-31", folder,
                latitude=job["latitude"], longitude=job["longitude"], compiler=cached_compiler,
            )
            report["config"]["cache_dir"] = str(shared_cache)
            report["config"]["weather_policy"] = "automatic" if cache_only else "strict"
            report["raw_cache_policy"] = "new-study cache seeded with immutable prior payload hardlinks"
            provenance = folder / "weather-provenance.json"
            atomic_json(provenance, report)
            result = report["result"]
            record.update(
                status="completed", path=climate.epw_path, sha256=climate.weather_sha256,
                provenance=str(provenance), source_provenance=str(folder / "source-provenance.json"),
                station=result.get("station"),
                interpolated_fraction=result.get("interpolated_fraction"),
                required_reconstructed_fraction=result.get("report", {}).get("required_reconstructed_fraction", 0),
                station_distance_km=result.get("coordinate_distance_km"),
                radiation_station_distance_km=result.get("radiation_station_distance_km"),
            )
            record = _validated(record, job["year"])
        except Exception as exc:  # noqa: BLE001 - preserve every preparation failure
            record["reason"] = f"{type(exc).__name__}: {exc}"
            record = classify_weather_failure(record)
            atomic_json(folder / "failure.json", record)
        atomic_json(receipt_path, cleaned(record))
        return record

    records = []

    def save(stage):
        status = {
            "stage": stage, "updated_utc": datetime.now(UTC).isoformat(),
            "jobs": len(jobs), "processed": len(records),
            "completed": sum(r["status"] == "completed" for r in records),
            "failed": sum(r["status"] == "failed" for r in records),
            "cache_unavailable_requires_authorization": sum(bool(r.get("external_authorization_required")) for r in records),
            "reused": sum(bool(r.get("reused_existing_weather")) for r in records),
            "workers": workers, "seeded_cache_files_this_run": seeded,
            "network_requests_allowed": not cache_only,
        }
        atomic_json(root / "execution.json", status)
        atomic_json(root / "weather-status.json", sorted(records, key=lambda r: r["key"]))
        atomic_json(root / "weather-manifest.json", {
            r["key"]: r for r in sorted(records, key=lambda r: r["key"]) if r["status"] == "completed"
        })
        if progress:
            print(json.dumps(status), flush=True)
        return status

    save("recovering_measurement_year_weather")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, job): job for job in jobs}
        for future in as_completed(futures):
            job = futures[future]
            try:
                records.append(future.result())
            except Exception as exc:  # noqa: BLE001 - cache/calendar failures are scientific failures
                records.append(classify_weather_failure(dict(job, job_identity=digest(job), status="failed", reason=f"{type(exc).__name__}: {exc}")))
            save("recovering_measurement_year_weather")
    status = save("weather_recovery_complete")
    status["failure_reasons"] = dict(Counter(r.get("reason") for r in records if r["status"] == "failed"))
    atomic_json(root / "weather-summary.json", status)
    return {"manifest": read(root / "weather-manifest.json"), "status": sorted(records, key=lambda r: r["key"]), "summary": status}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("selection")
    parser.add_argument("existing")
    parser.add_argument("output")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--cache-only", action="store_true", help="Read existing cached payloads; prohibit all HTTP requests")
    arguments = parser.parse_args(argv)
    result = recover_weather(arguments.selection, arguments.existing, arguments.output,
                             arguments.workers, retry_failed=arguments.retry_failed,
                             cache_only=arguments.cache_only)
    print(json.dumps(result["summary"], sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
