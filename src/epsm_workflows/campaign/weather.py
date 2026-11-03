# SPDX-License-Identifier: MIT
"""Freeze reviewed EPWs into the package and expose them as portable climates."""
from __future__ import annotations

import csv
import hashlib
import shutil
from dataclasses import asdict
from pathlib import Path

from epsm_core.weather import climate_from_epw, validate_epw_calendar

from ..records import atomic_json, digest

WEATHER_FIELDS = (
    "dry_bulb_c",
    "dew_point_c",
    "relative_humidity_pct",
    "atmospheric_pressure_pa",
    "horizontal_infrared_wh_m2",
    "global_horizontal_wh_m2",
    "direct_normal_wh_m2",
    "diffuse_horizontal_wh_m2",
    "wind_direction_deg",
    "wind_speed_m_s",
)
_EPW_COLUMNS = (6, 7, 8, 9, 12, 13, 14, 15, 20, 21)


def file_sha256(path):
    checksum = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def epw_rows(weather_id, path, year):
    """Yield the hourly weather-table rows of one EPW for the handoff database."""
    with Path(path).open(newline="", errors="replace") as stream:
        rows = list(csv.reader(stream))
    data = rows[8:]
    if len(data) != 8760:
        raise ValueError(f"{path}: expected 8760 EPW rows, found {len(data)}")
    for t, row in enumerate(data):
        if len(row) < 22:
            raise ValueError(f"{path}: incomplete EPW row {t + 1}")
        timestamp = f"{year}-{int(row[1]):02d}-{int(row[2]):02d} {int(row[3]) - 1:02d}:00:00"
        yield (weather_id, t, timestamp, *(float(row[i]) for i in _EPW_COLUMNS))


def _copy(source, destination):
    if not destination.exists():
        shutil.copyfile(source, destination)
    if file_sha256(destination) != file_sha256(source):
        raise ValueError(f"Weather copy mismatch: {destination}")


def _generate_amy(city, tmy, destination, provenance, year):
    """Compile a measured-year EPW with smhi2epw when the study names a station."""
    from smhi2epw import compiler

    climate = climate_from_epw(tmy, calendar_year=year)
    stage = destination.with_suffix(".pending.epw")
    cfg = compiler.EPWConfig(
        year=year,
        output_path=str(stage),
        city=city.city,
        latitude=climate.latitude_deg,
        longitude=climate.longitude_deg,
        utc_offset=climate.time_zone_hours,
        target_elevation_m=climate.elevation_m,
        station_id=city.amy_station_id,
        cache_dir=str(destination.parent / "raw-cache"),
    )
    result = compiler.compile_epw(cfg)
    validate_epw_calendar(stage, year)
    stage.replace(destination)
    atomic_json(
        provenance,
        {
            "config": asdict(cfg),
            "result": asdict(result),
            "policies": {"maximum_gap_hours": 48, "pressure": getattr(compiler.C, "PRESSURE_METHOD", None)},
            "weather_sha256": file_sha256(destination),
        },
    )


def freeze_weather(study, root):
    """Copy every study EPW under ``root/weather`` and return the frozen catalog.

    Catalog entries carry package-relative paths so the plan stays portable.
    Stock-style studies get ``<slug>_tmy`` and ``<slug>_amy`` ids; weather sweeps
    use their declared ids.
    """
    root = Path(root)
    year = study.calendar_year
    catalog = {}

    def entry(weather_id, destination, *, site, kind, calendar_year):
        validate_epw_calendar(destination, calendar_year)
        climate_from_epw(destination, calendar_year=calendar_year)
        catalog[weather_id] = {
            "path": str(destination.relative_to(root)),
            "sha256": file_sha256(destination),
            "site": site,
            "kind": kind,
            "calendar_year": calendar_year,
        }

    for city in study.cities:
        folder = root / "weather" / city.slug
        folder.mkdir(parents=True, exist_ok=True)
        tmy = folder / "tmy.epw"
        _copy(Path(city.tmy), tmy)
        entry(f"{city.slug}_tmy", tmy, site=city.slug, kind="tmy", calendar_year=year)
        amy = folder / f"amy-{year}.epw"
        provenance = folder / "amy-provenance.json"
        if city.amy:
            _copy(Path(city.amy), amy)
            if city.amy_provenance and not provenance.exists():
                shutil.copyfile(city.amy_provenance, provenance)
        elif city.amy_station_id is not None:
            if not amy.exists():
                _generate_amy(city, tmy, amy, provenance, year)
        else:
            continue
        entry(f"{city.slug}_amy", amy, site=city.slug, kind="amy", calendar_year=year)
    if study.weather_sweep is not None:
        folder = root / "weather" / "sweep"
        folder.mkdir(parents=True, exist_ok=True)
        for item in study.weather_sweep.weather:
            destination = folder / f"{item.weather_id}.epw"
            calendar_year = item.calendar_year
            if _epw_row_count(item.path) == 8784:
                # Every handoff array is 8760 hours and Core simulates a leap year in
                # full, so a measured leap year is run without 29 February under the
                # next non-leap calendar that starts on the same weekday.
                calendar_year = nonleap_stand_in(item.calendar_year)
                if not destination.exists():
                    drop_leap_day(item.path, destination, calendar_year)
            else:
                _copy(Path(item.path), destination)
            entry(item.weather_id, destination, site=item.location, kind=item.kind, calendar_year=calendar_year)
            if calendar_year != item.calendar_year:
                catalog[item.weather_id].update(source_year=item.calendar_year, leap_day_dropped=True)
    atomic_json(root / "weather.json", catalog)
    return catalog


def _epw_row_count(path):
    with Path(path).open(newline="", errors="replace") as stream:
        return sum(1 for _ in stream) - 8


def nonleap_stand_in(year):
    """The next non-leap year whose 1 January falls on the same weekday as ``year``'s."""
    import calendar

    weekday = calendar.weekday(year, 1, 1)
    for candidate in range(year + 1, year + 12):
        if not calendar.isleap(candidate) and calendar.weekday(candidate, 1, 1) == weekday:
            return candidate
    raise ValueError(f"No non-leap stand-in year for {year}")


def drop_leap_day(source, destination, year):
    """Write ``source`` (8784 rows) as an 8760-row EPW: 29 February removed, every row
    dated ``year`` and the leap-day header flag cleared."""
    with Path(source).open(newline="", errors="replace") as stream:
        rows = list(csv.reader(stream))
    headers, data = rows[:8], rows[8:]
    if len(data) != 8784:
        raise ValueError(f"{source}: expected 8784 EPW rows, found {len(data)}")
    kept = [row for row in data if not (int(row[1]) == 2 and int(row[2]) == 29)]
    if len(kept) != 8760:
        raise ValueError(f"{source}: removing 29 February left {len(kept)} rows")
    source_year = kept[0][0]
    for row in kept:
        row[0] = str(year)
    flags = headers[4]
    if flags[0].upper().startswith("HOLIDAYS") and len(flags) > 1:
        flags[1] = "No"
    # DATA PERIODS dates such as 1/1/2016 name the source year; EnergyPlus reads them.
    headers[7] = [
        field[: -len(source_year)] + str(year) if field.count("/") == 2 and field.endswith(source_year) else field
        for field in headers[7]
    ]
    with Path(destination).open("w", newline="") as stream:
        csv.writer(stream, lineterminator="\n").writerows(headers + kept)


def load_climates(catalog, root):
    """Return portable ClimateSpecs keyed by weather id, paths relative to the package.

    An AMY shares its TMY's site coordinates: the target site stays fixed when
    the observation station changes.
    """
    root = Path(root)
    climates = {}
    for weather_id, item in catalog.items():
        climate = climate_from_epw(root / item["path"], calendar_year=item["calendar_year"])
        if climate.weather_sha256 != item["sha256"]:
            raise ValueError(f"Frozen weather changed: {weather_id}")
        climates[weather_id] = climate.updated(epw_path=item["path"])
    for weather_id, item in catalog.items():
        if item["kind"] != "amy" or not item["site"]:
            continue
        tmy = climates.get(f"{item['site']}_tmy")
        if tmy is None:
            continue
        amy = climates[weather_id]
        for name, tolerance in (("latitude_deg", 1e-4), ("longitude_deg", 1e-4), ("elevation_m", 0.01), ("time_zone_hours", 1e-9)):
            if abs(getattr(amy, name) - getattr(tmy, name)) > tolerance:
                raise ValueError(f"TMY/AMY site mismatch for {item['site']}: {name}")
        climates[weather_id] = amy.updated(
            latitude_deg=tmy.latitude_deg,
            longitude_deg=tmy.longitude_deg,
            elevation_m=tmy.elevation_m,
            time_zone_hours=tmy.time_zone_hours,
        )
    return climates


def catalog_digest(catalog):
    return digest({k: v["sha256"] for k, v in catalog.items()})
