"""Measurement-year weather coordination; EPC normalization remains a separate operation."""

import hashlib
import json
from dataclasses import asdict
from datetime import date
from pathlib import Path

from epsm_core.weather import climate_from_epw, validate_epw_calendar


def measurement_year(start, end):
    start = date.fromisoformat(start) if isinstance(start, str) else start
    end = date.fromisoformat(end) if isinstance(end, str) else end
    if start.year != end.year:
        raise ValueError("Cross-year measurement-window simulations are deferred")
    if (start.month, start.day, end.month, end.day) != (1, 1, 12, 31):
        raise ValueError(
            "Weather coordination requires a complete calendar-year measurement period"
        )
    return start.year


def compile_measurement_weather(
    start,
    end,
    directory,
    *,
    latitude,
    longitude,
    station_id=None,
    radiation_station_id=None,
    compiler=None,
):
    year = measurement_year(start, end)
    import smhi2epw.compiler as compiler_module
    from smhi2epw import compile_epw
    from smhi2epw.compiler import EPWConfig

    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / f"weather-{year}.epw"
    if output.exists():
        raise FileExistsError("Use a fresh weather artifact directory")
    config = EPWConfig(
        year=year,
        output_path=str(output),
        latitude=latitude,
        longitude=longitude,
        station_id=station_id,
        radiation_station_id=radiation_station_id,
        utc_offset=1,
        cache_dir=str(directory / "raw-cache"),
    )
    result = (compiler or compile_epw)(config)
    calendar = validate_epw_calendar(result.output_path, year)
    climate = climate_from_epw(result.output_path, calendar_year=year)
    report = dict(
        measurement_start=str(start),
        measurement_end=str(end),
        calendar=calendar,
        config=asdict(config),
        result=asdict(result),
        weather_sha256=climate.weather_sha256,
        compiler_source_sha256=hashlib.sha256(
            Path(compiler_module.__file__).read_bytes()
        ).hexdigest(),
        comparison_note="Actual-year weather does not reproduce EPC normal-year or normal-use corrections",
    )
    (directory / "weather-provenance.json").write_text(
        json.dumps(report, indent=2, default=str, allow_nan=False)
    )
    return climate, report
