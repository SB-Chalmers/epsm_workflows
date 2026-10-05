"""Anchor outages inside detected weather spells using original temperatures.

An isolated annual maximum need not fall inside a heatwave. These anchors retain
the observed annual extrema and separately target the hottest/coldest original
hour inside the reference's absolute heatwaves/cold spells. The event definitions
remain the study's transparent operational rules, not official warning criteria.
"""

from __future__ import annotations

from datetime import timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from smhi2epw import read_epw

from .anomalies import detect_events


def weather_anchors(originals, reference_epw, context) -> dict[str, pd.Timestamp]:
    """Select UTC outage anchors from original finite temperature evidence.

    ``reference_epw`` may be a parsed EPW DataFrame or a local EPW path.
    ``context`` supplies the actual ``year`` and fixed ``utc_offset``. Raw UTC
    source timestamps map to EPW hour-ending labels; subtracting one hour after
    conversion to fixed LST gives interval starts and the correct EPW label day.
    Thus hour24 at midnight remains inside the preceding day's detected event.

    Optional event anchors are omitted when that absolute event is absent or
    every original temperature inside its coverage is missing/nonfinite. Across
    multiple spells, choose the greatest/least finite original temperature.
    Equal extrema use the first chronological occurrence. Detection uses the
    complete reference; imputed reference temperatures never select an anchor.
    Inputs remain unchanged, and full detection metadata is saved by the caller.
    """
    source_index = pd.DatetimeIndex(originals.index)
    if (
        source_index.tz is None
        or source_index.hasnans
        or source_index.has_duplicates
        or not source_index.is_monotonic_increasing
    ):
        raise ValueError(
            "Original weather must have an ordered unique timezone-aware index"
        )
    offset = float(context.utc_offset)
    if not np.isfinite(offset) or not -12 <= offset <= 14 or not offset.is_integer():
        raise ValueError("Event anchors require a valid whole-hour fixed LST offset")
    fixed_lst = timezone(timedelta(hours=offset))
    interval_starts = source_index.tz_convert(fixed_lst) - pd.Timedelta(hours=1)
    temperature = pd.to_numeric(originals["dry_bulb"], errors="coerce").copy()
    temperature.index = source_index
    finite = np.isfinite(temperature.to_numpy(dtype=float))
    temperature = temperature.loc[finite & (interval_starts.year == context.year)]
    temperature.index = temperature.index.tz_convert("UTC")
    if temperature.empty:
        raise ValueError(
            "Reference has no original finite temperature evidence in the requested year"
        )
    anchors = {
        "warmest_original_hour": temperature.idxmax(),
        "coldest_original_hour": temperature.idxmin(),
    }

    reference = (
        reference_epw
        if isinstance(reference_epw, pd.DataFrame)
        else read_epw(Path(reference_epw))
    )
    location = reference.attrs.get("location", {})
    declared_offset = location.get("time_zone", location.get("utc_offset"))
    if declared_offset is not None and float(declared_offset) != offset:
        raise ValueError("Reference EPW offset differs from the source weather context")
    # Parsed EPWs have calendar fields and a RangeIndex; short offline fixtures
    # may have naive hour-ending indices. Supply explicit fixed LST to both.
    if declared_offset is None:
        reference = reference.copy()
        reference.attrs["location"] = dict(location) | {"time_zone": offset}
    if isinstance(reference.index, pd.DatetimeIndex) and reference.index.tz is not None:
        if any(
            stamp.utcoffset() != timedelta(hours=offset) for stamp in reference.index
        ):
            raise ValueError(
                "Reference event calendar differs from the fixed LST context"
            )
    events = detect_events(reference)["events"]
    valid_starts = temperature.index.tz_convert(fixed_lst) - pd.Timedelta(hours=1)
    for kind, label, cold in (
        ("heatwave", "heatwave_hottest_hour", False),
        ("cold_spell", "cold_spell_coldest_hour", True),
    ):
        inside = np.zeros(len(temperature), dtype=bool)
        for event in events:
            if event["type"] == kind:
                inside |= (valid_starts >= pd.Timestamp(event["start_lst"])) & (
                    valid_starts < pd.Timestamp(event["end_lst"])
                )
        candidates = temperature.loc[inside]
        if not candidates.empty:
            anchors[label] = candidates.idxmin() if cold else candidates.idxmax()
    return anchors
