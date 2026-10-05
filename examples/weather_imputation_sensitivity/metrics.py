"""Extract hourly delivered sensible thermal loads and unambiguous peak times.

EnergyPlus reports hourly energies in joules. Summing and dividing by 3.6e6
gives annual kWh; the same conversion gives hourly mean kW for sixty-minute
intervals. These outputs contain neither HVAC electricity nor fuel input.
"""

from __future__ import annotations

import calendar
import sqlite3
from datetime import timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

HEATING_VARIABLE = "Zone Ideal Loads Supply Air Sensible Heating Energy"
COOLING_VARIABLE = "Zone Ideal Loads Supply Air Sensible Cooling Energy"


def extract(sql_path: str | Path, *, year: int = 2023, utc_offset: float = 1.0):
    """Return hourly load data and annual/peak metrics after calendar validation.

    Peak ties use np.isclose(rtol=1e-9, atol=1e-9 kW), a numerical tolerance
    only. ISO timestamps are normalized hour-ending endpoints in fixed local
    standard time, so EPW hour 24 is midnight of the following date. Separate
    date/hour columns retain the original EPW interval label.
    """
    if not np.isfinite(utc_offset) or not -12 <= utc_offset <= 14:
        raise ValueError("Expected a finite fixed local standard time offset")
    uri = Path(sql_path).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        frame = pd.read_sql_query(
            """SELECT t.TimeIndex, t.Year, t.Month, t.Day, t.Hour, t.Minute,
                      t.Interval, t.DST AS DST, d.Name, d.KeyValue AS SystemKey, d.Units, r.Value
                 FROM ReportData r
                 JOIN ReportDataDictionary d ON d.ReportDataDictionaryIndex=r.ReportDataDictionaryIndex
                 JOIN Time t ON t.TimeIndex=r.TimeIndex
                 JOIN EnvironmentPeriods e ON e.EnvironmentPeriodIndex=t.EnvironmentPeriodIndex
                WHERE d.IsMeter=0 AND d.ReportingFrequency='Hourly'
                  AND d.Name IN (?, ?)
                  AND COALESCE(t.WarmupFlag,0)=0 AND e.EnvironmentType=3
                ORDER BY t.TimeIndex, d.Name""",
            connection,
            params=(HEATING_VARIABLE, COOLING_VARIABLE),
        )
    expected_hours = 8784 if calendar.isleap(year) else 8760
    systems = set(frame.SystemKey)
    if (
        not systems
        or len(frame) != expected_hours * 2 * len(systems)
        or set(frame.Name)
        != {
            HEATING_VARIABLE,
            COOLING_VARIABLE,
        }
    ):
        raise ValueError("Missing or duplicated annual sensible-load output")
    counts = frame.groupby(["SystemKey", "Name"]).size()
    if len(counts) != len(systems) * 2 or not counts.eq(expected_hours).all():
        raise ValueError(
            "Each ideal-loads system must report both complete hourly outputs"
        )
    if frame.duplicated(["TimeIndex", "SystemKey", "Name"]).any():
        raise ValueError("Duplicate sensible-load outputs for one system/hour")
    raw_values = frame.Value.to_numpy(dtype=float)
    if not np.isfinite(raw_values).all() or (raw_values < -1e-6).any():
        raise ValueError("Nonfinite or negative delivered sensible system load")
    if not frame.Units.eq("J").all() or not frame.Interval.eq(60).all():
        raise ValueError("Load metrics require joules over complete hourly intervals")
    if not frame.DST.eq(0).all() or not frame.Year.eq(year).all():
        raise ValueError("Simulation calendar year/DST differs from requested AMY")
    times = frame.drop_duplicates("TimeIndex").sort_values("TimeIndex")
    if len(times) != expected_hours:
        raise ValueError("Unexpected count of distinct annual reporting hours")
    expected_start = pd.date_range(f"{year}-01-01", periods=expected_hours, freq="h")
    if (
        not np.array_equal(times.Month.to_numpy(), expected_start.month.to_numpy())
        or not np.array_equal(times.Day.to_numpy(), expected_start.day.to_numpy())
        or not np.array_equal(times.Hour.to_numpy(), expected_start.hour.to_numpy() + 1)
        or not times.Minute.isin([0, 60]).all()
    ):
        raise ValueError(
            "Annual output does not match the complete ordered hour-ending calendar"
        )
    loads = frame.pivot_table(
        index="TimeIndex", columns="Name", values="Value", aggfunc="sum"
    ).reindex(times.TimeIndex)
    values = loads.to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values < -1e-6).any():
        raise ValueError("Nonfinite or negative delivered sensible load")
    fixed_zone = timezone(timedelta(hours=utc_offset))
    endpoints = (expected_start + pd.Timedelta(hours=1)).tz_localize(fixed_zone)
    hourly = pd.DataFrame(
        {
            "lst_hour_ending": [timestamp.isoformat() for timestamp in endpoints],
            "epw_date": expected_start.strftime("%Y-%m-%d"),
            "epw_hour": expected_start.hour + 1,
            "heating_kw": np.maximum(loads[HEATING_VARIABLE].to_numpy(dtype=float), 0)
            / 3.6e6,
            "cooling_kw": np.maximum(loads[COOLING_VARIABLE].to_numpy(dtype=float), 0)
            / 3.6e6,
        }
    )
    summary = {
        "hours": expected_hours,
        "ideal_load_system_count": len(systems),
        "ideal_load_system_keys": sorted(systems),
        "quantity": "delivered sensible thermal loads, excluding HVAC electricity/fuel",
        "peak_basis": "hourly mean kW, not subhourly design capacity",
        "time_basis": f"hour-ending fixed LST UTC{utc_offset:+g}; no daylight-saving shift",
        "peak_tie_tolerance": {"relative": 1e-9, "absolute_kw": 1e-9},
    }
    for mode in ("heating", "cooling"):
        values = hourly[f"{mode}_kw"].to_numpy()
        peak = float(values.max())
        mask = np.isclose(values, peak, rtol=1e-9, atol=1e-9)
        summary[f"annual_{mode}_kwh"] = float(values.sum())
        summary[f"peak_{mode}_kw"] = peak
        summary[f"peak_{mode}_hours_lst"] = hourly.loc[mask, "lst_hour_ending"].tolist()
        summary[f"peak_{mode}_epw_labels"] = [
            f"{row.epw_date} HE{int(row.epw_hour):02d} LST UTC{utc_offset:+g}"
            for row in hourly.loc[mask].itertuples()
        ]
    return hourly, summary
