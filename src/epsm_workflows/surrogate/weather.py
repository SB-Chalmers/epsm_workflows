"""Freeze ten reviewed EPWs; generate only explicitly requested AMYs."""

from __future__ import annotations

import shutil
import sys
from dataclasses import asdict
from pathlib import Path

from epsm_core.weather import climate_from_epw, validate_epw_calendar

from ..campaign import atomic_json, digest, file_sha256


def _source_hashes(root: Path):
    return {
        str(path.relative_to(root)): file_sha256(path)
        for path in sorted(root.rglob("*.py"))
    }


def prepare_weather(config: dict, root) -> dict:
    """Copy reviewed pairs and reproducibly generate any missing explicit AMY."""
    root = Path(root)
    checkout = Path(config["smhi2epw_repository"])
    source = checkout / "src"
    sources = _source_hashes(source / "smhi2epw")
    catalog = {}
    for site in config["cities"]:
        folder = root / "weather" / site["slug"]
        folder.mkdir(parents=True, exist_ok=True)
        tmy_source = Path(site["tmy"])
        tmy = folder / "tmy.epw"
        if not tmy.exists():
            shutil.copyfile(tmy_source, tmy)
        if file_sha256(tmy) != file_sha256(tmy_source):
            raise ValueError(f"TMY copy mismatch: {site['slug']}")
        amy = folder / "amy-2018.epw"
        provenance = folder / "amy-provenance.json"
        if site.get("amy"):
            source_amy = Path(site["amy"])
            if not amy.exists():
                shutil.copyfile(source_amy, amy)
            if file_sha256(amy) != file_sha256(source_amy):
                raise ValueError(f"AMY copy mismatch: {site['slug']}")
            source_provenance = Path(site["amy_provenance"])
            if not provenance.exists():
                shutil.copyfile(source_provenance, provenance)
        elif not provenance.exists():
            sys.path.insert(0, str(source))
            import smhi2epw.compiler as compiler

            climate = climate_from_epw(tmy, calendar_year=2018)
            stage = folder / "amy-2018.pending.epw"
            cfg = compiler.EPWConfig(
                year=2018,
                output_path=str(stage),
                city=site["city"],
                latitude=climate.latitude_deg,
                longitude=climate.longitude_deg,
                utc_offset=climate.time_zone_hours,
                target_elevation_m=climate.elevation_m,
                station_id=site["amy_station_id"],
                cache_dir=str(folder / "raw-cache"),
            )
            result = compiler.compile_epw(cfg)
            validate_epw_calendar(stage, 2018)
            stage.replace(amy)
            atomic_json(
                provenance,
                {
                    "config": asdict(cfg),
                    "result": asdict(result),
                    "policies": {
                        "maximum_gap_hours": 48,
                        "pressure": getattr(compiler.C, "PRESSURE_METHOD", None),
                    },
                    "smhi2epw_source_hash": digest(sources),
                    "weather_sha256": file_sha256(amy),
                },
            )
        for path in (tmy, amy):
            validate_epw_calendar(path, 2018)
            climate_from_epw(path, calendar_year=2018)
        catalog[site["slug"]] = {
            "tmy": {"path": str(tmy.resolve()), "sha256": file_sha256(tmy)},
            "amy": {
                "path": str(amy.resolve()),
                "sha256": file_sha256(amy),
                "provenance_sha256": file_sha256(provenance),
            },
        }
    atomic_json(root / "weather.json", catalog)
    return catalog
