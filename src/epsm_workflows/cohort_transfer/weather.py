"""Target-independent measurement-year weather preparation and reusable receipts.

References: smhi2epw EPWConfig/provenance and SMHI corrected archives/STRANG.
Alternative stations are weather uncertainties, never selected using EPC errors.
"""

import shutil
from dataclasses import asdict
from pathlib import Path

from ..campaign import atomic_json, file_sha256
from .common import digest, read


def prepare_weather(config_path, output):
    """Compile declared site/year requests, preserving gaps and alternatives."""
    from epsm_core.weather import validate_epw_calendar
    from smhi2epw import EPWConfig, compile_epw

    config = read(config_path)
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest, failures = {}, []
    for request in config["requests"]:
        site = request["site_id"]
        settings = request["configuration"]
        identity = digest(settings)
        folder = root / identity
        folder.mkdir(exist_ok=True)
        epw, provenance = folder / "weather.epw", folder / "provenance.json"
        cache = (
            folder / "raw-cache"
            if request.get("existing_cache")
            else root / "raw-cache"
        )
        if request.get("existing_cache") and not cache.exists():
            source = Path(request["existing_cache"])
            shutil.copytree(source, cache)
            atomic_json(
                folder / "copied-cache.json",
                {
                    "source": str(source.resolve()),
                    "files": {
                        str(p.relative_to(cache)): file_sha256(p)
                        for p in cache.rglob("*")
                        if p.is_file()
                    },
                },
            )
        cfg = EPWConfig(
            **(
                settings
                | {
                    "output_path": str(epw),
                    "provenance_path": str(provenance),
                    "cache_dir": str(cache),
                }
            )
        )
        key = f"{site}:{cfg.year}"
        if key in manifest:
            raise ValueError("Duplicate main site/year request")
        try:
            if epw.exists() and provenance.exists():
                receipt = read(provenance)
                if receipt["epw_sha256"] != file_sha256(epw):
                    raise ValueError("Cached EPW changed")
                expected = asdict(cfg)
                if receipt["configuration"] != expected:
                    raise ValueError("Cached weather configuration changed")
            else:
                compile_epw(cfg)
                receipt = read(provenance)
            validate_epw_calendar(epw, cfg.year)
            manifest[key] = {
                "path": str(epw),
                "sha256": file_sha256(epw),
                "provenance": str(provenance),
                "provenance_sha256": file_sha256(provenance),
                "station_distance_km": receipt["result"]["coordinate_distance_km"],
                "interpolated_fraction": receipt["result"]["interpolated_fraction"],
                "alternatives": [],
            }
            for station in request.get("alternative_station_ids", []):
                alternative = prepare_weather_alternative(settings, station, root)
                manifest[key]["alternatives"].append(alternative)
        except Exception as exc:  # noqa: BLE001 - preserve every failed scientific case
            failures.append(
                {"site_year": key, "reason": f"{type(exc).__name__}: {exc}"}
            )
        atomic_json(root / "weather-manifest.json", manifest)
        atomic_json(root / "weather-failures.json", failures)
    return manifest


def prepare_weather_alternative(settings, station, root):
    """Keep a requested alternative even when its station cannot cover the year."""
    from smhi2epw import EPWConfig, compile_epw

    cfg = settings | {"station_id": station}
    folder = Path(root) / digest(cfg)
    folder.mkdir(exist_ok=True)
    try:
        epw, provenance = folder / "weather.epw", folder / "provenance.json"
        config = EPWConfig(
            **(
                cfg
                | {
                    "output_path": str(epw),
                    "provenance_path": str(provenance),
                    "cache_dir": str(Path(root) / "raw-cache"),
                }
            )
        )
        compile_epw(config)
        return {
            "station_id": station,
            "status": "accepted",
            "path": str(epw),
            "sha256": file_sha256(epw),
            "provenance": str(provenance),
        }
    except Exception as exc:  # noqa: BLE001 - preserve every failed scientific case
        return {"station_id": station, "status": "failed", "reason": str(exc)}
