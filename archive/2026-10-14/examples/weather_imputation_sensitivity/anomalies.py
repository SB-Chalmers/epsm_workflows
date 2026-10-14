"""Describe weather events and reconstruction differences using operational rules.

These diagnostics are deliberately transparent study definitions, not official
warnings or climatologically validated extreme-event criteria. All comparisons
use the reference year's thresholds. EPW label days belong to interval starts:
an hour-ending midnight is hour 24 of the preceding date in fixed local time.
"""

from __future__ import annotations

from datetime import timedelta, timezone

import numpy as np
import pandas as pd

EVENT_TYPES = (
    "heatwave",
    "cold_spell",
    "warm_spell",
    "relative_cold_spell",
    "high_wind",
    "temperature_jump",
    "solar_out_of_bounds",
    "nighttime_solar",
    "solar_deviation",
    "source_boundary",
)
DEFAULTS = {
    "heatwave_daily_tmax_c": 25.0,
    "cold_spell_daily_tmin_c": -10.0,
    "minimum_spell_days": 3,
    "high_wind_m_s": 10.0,
    "temperature_jump_c_h": 5.0,
    "solar_max_w_m2": 2000.0,
    "nighttime_solar_w_m2": 1.0,
    "solar_deviation_w_m2": 200.0,
}
RELATIVE_KEYS = ("relative_warm_daily_tmax_c", "relative_cold_daily_tmin_c")


def _index(weather, index=None):
    """Resolve and validate continuous hour-ending fixed local timestamps."""
    if index is None:
        if isinstance(weather.index, pd.DatetimeIndex):
            index = weather.index
        elif {"year", "month", "day", "hour"}.issubset(weather.columns):
            index = pd.to_datetime(weather[["year", "month", "day"]])
            index = index + pd.to_timedelta(weather["hour"], unit="h")
        else:
            raise ValueError(
                "Provide an hour-ending DatetimeIndex or EPW calendar fields"
            )
    index = pd.DatetimeIndex(index)
    if len(index) != len(weather) or not len(index) or index.hasnans:
        raise ValueError(
            "Weather and hour-ending timestamps must have equal nonzero length"
        )
    if index.tz is None:
        location = weather.attrs.get("location", {})
        offset = location.get("time_zone", location.get("utc_offset"))
        if offset is not None:
            if not np.isfinite(offset) or not -12 <= offset <= 14:
                raise ValueError("Invalid fixed local standard time offset")
            index = index.tz_localize(timezone(timedelta(hours=float(offset))))
    if index.tz is not None and len({stamp.utcoffset() for stamp in index}) != 1:
        raise ValueError(
            "Event detection requires fixed LST without daylight-saving changes"
        )
    if not index.equals(index.floor("h")) or np.any(
        (index[1:] - index[:-1]) != pd.Timedelta(hours=1)
    ):
        raise ValueError(
            "Event detection requires ordered, consecutive complete hourly intervals"
        )
    return index


def _daily_temperature(weather, index):
    """Aggregate only complete observed temperature days using EPW interval labels."""
    temperature = pd.to_numeric(weather["dry_bulb"], errors="coerce").to_numpy(
        dtype=float, copy=True
    )
    temperature[~np.isfinite(temperature)] = np.nan
    labels = (index - pd.Timedelta(hours=1)).normalize()
    hourly = pd.DataFrame({"temperature": temperature}, index=labels)
    daily = hourly.groupby(level=0).agg(
        tmax=("temperature", "max"),
        tmin=("temperature", "min"),
        temperature_hours=("temperature", "count"),
        rows=("temperature", "size"),
    )
    daily["complete"] = daily.temperature_hours.eq(24) & daily.rows.eq(24)
    daily.loc[~daily.complete, ["tmax", "tmin"]] = np.nan
    return daily


def _percentile(values, quantile):
    """Return seasonal percentile when variation makes relative extremes meaningful."""
    values = values.dropna()
    if values.empty or float(values.max() - values.min()) <= 1e-8:
        return None
    return float(values.quantile(quantile))


def _thresholds(daily, overrides):
    """Build explicit reference thresholds and validate optional pinned replacements."""
    warm = daily.index.month.isin([5, 6, 7, 8, 9])
    thresholds = DEFAULTS | {
        "relative_warm_daily_tmax_c": _percentile(daily.loc[warm, "tmax"], 0.95),
        "relative_cold_daily_tmin_c": _percentile(daily.loc[~warm, "tmin"], 0.05),
    }
    overrides = overrides or {}
    unknown = set(overrides) - set(thresholds)
    if unknown:
        raise ValueError(f"Unknown event thresholds: {sorted(unknown)}")
    thresholds.update(overrides)
    for key, value in thresholds.items():
        if value is None and key in RELATIVE_KEYS:
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not np.isfinite(value)
        ):
            raise ValueError(f"Event threshold {key} must be finite")
    minimum = thresholds["minimum_spell_days"]
    if int(minimum) != minimum or minimum < 1:
        raise ValueError("Minimum spell duration must be a positive integer")
    for key in (
        "high_wind_m_s",
        "temperature_jump_c_h",
        "solar_max_w_m2",
        "solar_deviation_w_m2",
    ):
        if thresholds[key] <= 0:
            raise ValueError(f"Event threshold {key} must be positive")
    return thresholds


def _runs(mask):
    """Return half-open positional runs of contiguous flagged intervals."""
    edges = np.diff(np.r_[False, np.asarray(mask, dtype=bool), False].astype(int))
    return zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1))


def _spell_events(kind, daily, mask, values, threshold, minimum, cold=False):
    """Describe complete daily spells with duration and temperature extrema."""
    events = []
    for start, end in _runs(mask):
        if end - start < minimum:
            continue
        first, last = daily.index[start], daily.index[end - 1]
        events.append(
            {
                "type": kind,
                "start_lst": first.isoformat(),
                "end_lst": (last + pd.Timedelta(days=1)).isoformat(),
                "start_date": first.date().isoformat(),
                "end_date": last.date().isoformat(),
                "duration_days": int(end - start),
                "duration_hours": int((end - start) * 24),
                "peak_value": float(
                    values.iloc[start:end].min()
                    if cold
                    else values.iloc[start:end].max()
                ),
                "peak_units": "degrees Celsius",
                "threshold": threshold,
            }
        )
    return events


def _hourly_events(kind, index, mask, values, threshold, units):
    """Describe contiguous hourly flags with actual interval start and end."""
    events = []
    for start, end in _runs(mask):
        selected = np.asarray(values)[start:end]
        finite = selected[np.isfinite(selected)]
        events.append(
            {
                "type": kind,
                "start_lst": (index[start] - pd.Timedelta(hours=1)).isoformat(),
                "end_lst": index[end - 1].isoformat(),
                "duration_hours": int(end - start),
                "peak_value": float(np.max(finite)) if len(finite) else None,
                "peak_units": units,
                "threshold": threshold,
            }
        )
    return events


def _nighttime(weather, index):
    """Find geometrically dark complete intervals when location and offset exist."""
    location = weather.attrs.get("location", {})
    if index.tz is None or not {"latitude", "longitude"}.issubset(location):
        return None
    latitude, longitude = location["latitude"], location["longitude"]
    if not np.isfinite([latitude, longitude]).all():
        return None
    from smhi2epw.solar import solar_zenith

    index = index.tz_convert("UTC")
    cosines = [
        solar_zenith(index - pd.Timedelta(minutes=m), latitude, longitude)[1]
        for m in (0, 30, 60)
    ]
    return np.logical_and.reduce([cosine <= 0 for cosine in cosines])


def _boundaries(weather):
    """Identify transitions in provided source labels without inventing attribution."""
    sources = weather.attrs.get("source_tags")
    if sources is None:
        columns = [c for c in weather if c == "source" or c.endswith("_source")]
        if not columns:
            return None
        sources = weather[columns]
    if isinstance(sources, pd.Series):
        sources = sources.to_frame()
    if not isinstance(sources, pd.DataFrame) or len(sources) != len(weather):
        raise ValueError("Source tags must align with weather rows")
    sources = sources.reset_index(drop=True)
    previous = sources.shift()
    changed = (sources.ne(previous) & sources.notna() & previous.notna()).any(axis=1)
    changed.iloc[0] = False
    return changed.to_numpy()


def detect_events(weather: pd.DataFrame, index=None, thresholds=None):
    """Detect operational spells and hourly anomalies in fixed local weather.

    ``index`` contains hour-ending LST timestamps; EPW hour 24 belongs to the
    preceding label date. Daily spells require 24 valid temperature hours.
    Relative thresholds use this reference year's May--September daily maxima
    and October--April minima; constant or absent seasonal samples have no
    relative threshold. Pass reference thresholds to analyze a candidate.
    Solar deviations require an optional ``reference_ghi`` comparison column.
    Source transitions require source columns or ``attrs['source_tags']``.
    """
    if "dry_bulb" not in weather:
        raise ValueError("Event detection requires dry_bulb temperature")
    index = _index(weather, index)
    daily = _daily_temperature(weather, index)
    thresholds = _thresholds(daily, thresholds)
    warm = daily.index.month.isin([5, 6, 7, 8, 9])
    definitions = [
        (
            "heatwave",
            "tmax",
            thresholds["heatwave_daily_tmax_c"],
            np.ones(len(daily), bool),
            False,
        ),
        (
            "cold_spell",
            "tmin",
            thresholds["cold_spell_daily_tmin_c"],
            np.ones(len(daily), bool),
            True,
        ),
        ("warm_spell", "tmax", thresholds["relative_warm_daily_tmax_c"], warm, False),
        (
            "relative_cold_spell",
            "tmin",
            thresholds["relative_cold_daily_tmin_c"],
            ~warm,
            True,
        ),
    ]
    events = []
    for kind, column, threshold, season, cold in definitions:
        if threshold is None:
            continue
        mask = daily[column].le(threshold) if cold else daily[column].ge(threshold)
        events.extend(
            _spell_events(
                kind,
                daily,
                mask & season,
                daily[column],
                threshold,
                thresholds["minimum_spell_days"],
                cold,
            )
        )
    temperature = pd.to_numeric(weather["dry_bulb"], errors="coerce").to_numpy(float)
    jumps = np.abs(np.diff(temperature, prepend=np.nan))
    events.extend(
        _hourly_events(
            "temperature_jump",
            index,
            jumps > thresholds["temperature_jump_c_h"],
            jumps,
            thresholds["temperature_jump_c_h"],
            "degrees Celsius/hour",
        )
    )
    if "wind_speed" in weather:
        wind = pd.to_numeric(weather["wind_speed"], errors="coerce").to_numpy(float)
        events.extend(
            _hourly_events(
                "high_wind",
                index,
                np.isfinite(wind) & (wind >= thresholds["high_wind_m_s"]),
                wind,
                thresholds["high_wind_m_s"],
                "m/s hourly mean",
            )
        )
    dark = None
    if "ghi" in weather:
        ghi = pd.to_numeric(weather["ghi"], errors="coerce").to_numpy(float)
        invalid = ~np.isfinite(ghi) | (ghi < 0) | (ghi > thresholds["solar_max_w_m2"])
        events.extend(
            _hourly_events(
                "solar_out_of_bounds",
                index,
                invalid,
                np.abs(ghi),
                thresholds["solar_max_w_m2"],
                "W/m²",
            )
        )
        dark = _nighttime(weather, index)
        if dark is not None:
            events.extend(
                _hourly_events(
                    "nighttime_solar",
                    index,
                    dark
                    & np.isfinite(ghi)
                    & (ghi > thresholds["nighttime_solar_w_m2"]),
                    ghi,
                    thresholds["nighttime_solar_w_m2"],
                    "W/m²",
                )
            )
        if "reference_ghi" in weather:
            reference = pd.to_numeric(
                weather["reference_ghi"], errors="coerce"
            ).to_numpy(float)
            difference = np.abs(ghi - reference)
            events.extend(
                _hourly_events(
                    "solar_deviation",
                    index,
                    difference > thresholds["solar_deviation_w_m2"],
                    difference,
                    thresholds["solar_deviation_w_m2"],
                    "W/m² absolute reconstruction difference",
                )
            )
    boundaries = _boundaries(weather)
    if boundaries is not None:
        events.extend(
            _hourly_events(
                "source_boundary",
                index,
                boundaries,
                jumps,
                None,
                "degrees Celsius absolute boundary change",
            )
        )
    events.sort(key=lambda event: (event["start_lst"], event["type"]))
    summary = {}
    for kind in EVENT_TYPES:
        selected = [event for event in events if event["type"] == kind]
        summary[kind] = {
            "count": len(selected),
            "hours": sum(event["duration_hours"] for event in selected),
            "days": sum(event["duration_days"] for event in selected)
            if kind in EVENT_TYPES[:4]
            else None,
        }
    return {
        "definition": "Operational study diagnostics; relative thresholds are annual-reference seasonal percentiles, not climatological extremes",
        "time_basis": "Fixed LST; start_lst inclusive and end_lst exclusive; label days use interval start",
        "thresholds": thresholds,
        "events": events,
        "summary": summary,
        "daily_temperature": [
            {
                "date": day.date().isoformat(),
                "tmax_c": float(row.tmax) if np.isfinite(row.tmax) else None,
                "tmin_c": float(row.tmin) if np.isfinite(row.tmin) else None,
                "temperature_hours": int(row.temperature_hours),
                "complete": bool(row.complete),
            }
            for day, row in daily.iterrows()
        ],
        "diagnostics": {
            "complete_days": int(daily.complete.sum()),
            "incomplete_days": [
                day.date().isoformat() for day in daily.index[~daily.complete]
            ],
            "missing_temperature_hours": int((~np.isfinite(temperature)).sum()),
            "undefined_relative_thresholds": [
                key for key in RELATIVE_KEYS if thresholds[key] is None
            ],
            "nighttime_solar_available": dark is not None,
            "source_boundary_available": boundaries is not None,
        },
    }


def compare_events(reference: pd.DataFrame, candidate: pd.DataFrame, index=None):
    """Compare event preservation using fixed reference thresholds and overlap.

    Match events by positive temporal overlap, preserving split and merge
    associations rather than forcing a one-to-one assignment. Report lost and
    candidate-only events explicitly. Solar deviation is a reconstruction
    difference against reference GHI, not a claim of physically impossible sun.
    """
    reference_index = _index(reference, index)
    candidate_index = _index(candidate, index)
    if not reference_index.equals(candidate_index):
        raise ValueError("Reference and candidate event calendars must align exactly")
    original = detect_events(reference, reference_index)
    compared = candidate.copy()
    if "ghi" in reference and "ghi" in candidate:
        compared["reference_ghi"] = reference["ghi"].to_numpy()
    alternative = detect_events(compared, candidate_index, original["thresholds"])
    results = {}
    for kind in EVENT_TYPES:
        before = [event for event in original["events"] if event["type"] == kind]
        after = [event for event in alternative["events"] if event["type"] == kind]
        matches = []
        for i, left in enumerate(before):
            for j, right in enumerate(after):
                start = max(
                    pd.Timestamp(left["start_lst"]), pd.Timestamp(right["start_lst"])
                )
                end = min(pd.Timestamp(left["end_lst"]), pd.Timestamp(right["end_lst"]))
                overlap = (end - start).total_seconds() / 3600
                if overlap <= 0:
                    continue
                matches.append(
                    {
                        "reference_index": i,
                        "candidate_index": j,
                        "overlap_hours": int(overlap),
                        "start_shift_hours": (
                            pd.Timestamp(right["start_lst"])
                            - pd.Timestamp(left["start_lst"])
                        ).total_seconds()
                        / 3600,
                        "end_shift_hours": (
                            pd.Timestamp(right["end_lst"])
                            - pd.Timestamp(left["end_lst"])
                        ).total_seconds()
                        / 3600,
                        "duration_change_hours": right["duration_hours"]
                        - left["duration_hours"],
                        "peak_value_change": right["peak_value"] - left["peak_value"]
                        if right["peak_value"] is not None
                        and left["peak_value"] is not None
                        else None,
                    }
                )
        matched_reference = {match["reference_index"] for match in matches}
        matched_candidate = {match["candidate_index"] for match in matches}
        lost = [event for i, event in enumerate(before) if i not in matched_reference]
        false = [event for j, event in enumerate(after) if j not in matched_candidate]
        results[kind] = {
            "reference_count": len(before),
            "candidate_count": len(after),
            "matched_reference_count": len(matched_reference),
            "matched_candidate_count": len(matched_candidate),
            "reference_hours": sum(event["duration_hours"] for event in before),
            "candidate_hours": sum(event["duration_hours"] for event in after),
            "overlap_hours": sum(match["overlap_hours"] for match in matches),
            "lost_reference_count": len(lost),
            "false_candidate_count": len(false),
            "lost_reference_events": lost,
            "false_candidate_events": false,
            "split_reference_count": sum(
                sum(match["reference_index"] == i for match in matches) > 1
                for i in matched_reference
            ),
            "merged_candidate_count": sum(
                sum(match["candidate_index"] == j for match in matches) > 1
                for j in matched_candidate
            ),
            "matches": matches,
        }
    return {
        "thresholds": original["thresholds"],
        "reference": original,
        "candidate": alternative,
        "by_type": results,
    }
