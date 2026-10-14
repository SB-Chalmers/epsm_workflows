"""Derive interpretable uncertainty measures from authenticated campaign evidence.

These postprocessing tables describe automatic weather completion in the tested
cases. Percentiles are empirical scenario summaries, not confidence intervals.
Annual percentages omit zero heating and cooling below 1 kWh/m², where division
by a small demand would obscure the absolute error. Peak timing is the distance
to the nearest reference annual peak hour; 24 hours is an explanatory tolerance,
not an engineering acceptance criterion. No simulations or source archives change.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import numpy as np
import pandas as pd

PHYSICAL = ["site_id", "year", "scope", "sampling", "gap_hours", "physical_signature"]
STRATA = ["scope", "sampling", "gap_hours"]


def sha(data):
    """Fingerprint consumed evidence without rewriting it."""
    return hashlib.sha256(data).hexdigest()


def _number(values, *, nonnegative=True):
    numbers = pd.to_numeric(values, errors="coerce")
    if not np.isfinite(numbers).all() or (nonnegative and numbers.lt(0).any()):
        raise ValueError("Uncertainty inputs must be finite with valid signs")
    return numbers


def relative_annual_summary(rows):
    """Express absolute demand differences relative to meaningful reference loads."""
    records = []
    for labels, group in rows.groupby(STRATA, sort=True):
        for mode in ("heating", "cooling"):
            baseline = _number(group[f"baseline_annual_{mode}_kwh_m2"])
            error = _number(group[f"absolute_annual_{mode}_error_kwh_m2"])
            eligible = baseline.gt(0) if mode == "heating" else baseline.ge(1)
            percentages = 100 * error[eligible] / baseline[eligible]
            records.append(
                dict(zip(STRATA, labels))
                | {
                    "mode": mode,
                    "building_responses": len(group),
                    "eligible_responses": int(eligible.sum()),
                    "excluded_small_reference_demands": int((~eligible).sum()),
                    "minimum_reference_kwh_m2": 0 if mode == "heating" else 1,
                    "median_absolute_error_pct": percentages.median(),
                    "empirical_p95_absolute_error_pct": percentages.quantile(0.95),
                    "maximum_absolute_error_pct": percentages.max(),
                }
            )
    return pd.DataFrame(records)


def exposed_timing_summary(rows):
    """Separate unchanged peak hours, changes within one day and larger changes.

    Exposure means at least one positive reference peak hour intersects the outage.
    The annual winning peak can change to another event; this is not evidence that
    the original weather event itself moved. All tied reference peaks participate
    in the existing nearest-hour distance supplied by the campaign analysis.
    """
    records = []
    for labels, group in rows.groupby(STRATA, sort=True):
        for mode in ("heating", "cooling"):
            baseline = _number(group[f"baseline_peak_{mode}_w_m2"])
            hidden = group[f"baseline_{mode}_peak_hidden"]
            exact = group[f"same_{mode}_peak_hours"]
            if (
                not hidden.isin([True, False]).all()
                or not exact.isin([True, False]).all()
            ):
                raise ValueError("Peak exposure and preservation require booleans")
            exposed = baseline.gt(1e-6) & hidden.eq(True)
            shifts = _number(
                group.loc[exposed, f"nearest_{mode}_peak_shift_h"], nonnegative=False
            ).abs()
            if shifts.mod(1).ne(0).any():
                raise ValueError("Annual peaks must refer to whole hourly intervals")
            count = len(shifts)
            same, within = int(shifts.eq(0).sum()), int(shifts.le(24).sum())
            beyond = int(shifts.gt(24).sum())
            records.append(
                dict(zip(STRATA, labels))
                | {
                    "mode": mode,
                    "exposed_positive_peaks": count,
                    "same_hour_peaks": same,
                    "exact_hour_set_peaks": int(exact[exposed].sum()),
                    "within24h_peaks": within,
                    "changed_within24h_peaks": within - same,
                    "beyond24h_peaks": beyond,
                    "beyond24h_fraction": beyond / count if count else np.nan,
                    "median_abs_shift_h": shifts.median(),
                    "p95_abs_shift_h": shifts.quantile(0.95),
                    "maximum_abs_shift_h": shifts.max(),
                }
            )
    return pd.DataFrame(records)


def _archive_json(path, manifest):
    """Authenticate every event and outage record against the analysis receipt."""
    raw = path.read_bytes()
    expected = manifest.get("inputs", {}).get(str(path.resolve()))
    if expected is None or sha(raw) != expected:
        raise ValueError(f"Archived weather evidence differs from its receipt: {path}")
    return json.loads(raw)


def event_temperature_tables(analysis_path, manifest, rows):
    """Quantify exposed heatwave/cold-spell temperature changes, without exact-equality claims.

    Each physical weather outage appears once, regardless of building count. Lost
    or split events retain their own counts and have no invented intensity error.
    Intensity quantiles describe exposed reference events with a single overlapping
    candidate; unmatched and ambiguous events are explicitly reported separately.
    Exposure follows the frozen campaign convention: EPW end-of-hour timestamps
    identify the preceding hourly interval, intersected with event start/end dates.
    """
    campaign = Path(analysis_path).resolve().parent
    selected = rows.loc[rows.scope.eq("meteorology")].drop_duplicates(PHYSICAL)
    records, plans = [], {}
    for row in selected.itertuples(index=False):
        folder = campaign / "datasets" / row.site_id / str(row.year)
        key = (row.site_id, row.year)
        if key not in plans:
            plan = _archive_json(folder / "panel-plan.json", manifest)
            panels = plan["all_panels"]
            if len({p["evidence_id"] for p in panels}) != len(panels):
                raise ValueError("Archived weather outage identities must be unique")
            plans[key] = {p["evidence_id"]: p for p in panels}
        panel = plans[key][row.evidence_id]
        intervals = pd.DatetimeIndex(pd.to_datetime(panel["masked_utc"], utc=True))
        intervals -= pd.Timedelta(hours=1)
        comparison = _archive_json(
            folder / "cases" / row.case_id / "events.json", manifest
        )
        for kind in ("heatwave", "cold_spell"):
            reference = [
                e for e in comparison["reference"]["events"] if e["type"] == kind
            ]
            for index, event in enumerate(reference):
                start, end = (
                    pd.Timestamp(event["start_lst"]),
                    pd.Timestamp(event["end_lst"]),
                )
                if not ((intervals >= start) & (intervals < end)).any():
                    continue
                matches = [
                    m
                    for m in comparison["by_type"][kind]["matches"]
                    if m["reference_index"] == index
                ]
                item = {name: getattr(row, name) for name in PHYSICAL} | {
                    "case_id": row.case_id,
                    "event_type": kind,
                    "reference_event_index": index,
                    "reference_start_lst": event["start_lst"],
                    "reference_end_lst": event["end_lst"],
                    "overlapping_candidate_events": len(matches),
                    "single_match": len(matches) == 1,
                    "same_dates": False,
                    "absolute_peak_temperature_error_c": np.nan,
                    "absolute_duration_change_h": np.nan,
                }
                if len(matches) == 1:
                    match = matches[0]
                    values = _number(
                        pd.Series(
                            [match["peak_value_change"], match["duration_change_hours"]]
                        ),
                        nonnegative=False,
                    )
                    item.update(
                        same_dates=(
                            match["start_shift_hours"] == 0
                            and match["end_shift_hours"] == 0
                        ),
                        absolute_peak_temperature_error_c=abs(values.iloc[0]),
                        absolute_duration_change_h=abs(values.iloc[1]),
                    )
                records.append(item)
    comparisons = pd.DataFrame(records)
    summaries = []
    if not comparisons.empty:
        for labels, group in comparisons.groupby(STRATA + ["event_type"], sort=True):
            matched = group.loc[group.single_match]
            temperature = matched.absolute_peak_temperature_error_c
            summaries.append(
                dict(zip(STRATA + ["event_type"], labels))
                | {
                    "exposed_reference_events": len(group),
                    "matched_exposed_events": len(matched),
                    "lost_events": int(group.overlapping_candidate_events.eq(0).sum()),
                    "split_events": int(group.overlapping_candidate_events.gt(1).sum()),
                    "unchanged_date_events": int(group.same_dates.sum()),
                    "median_abs_peak_temperature_error_c": temperature.median(),
                    "empirical_p95_abs_peak_temperature_error_c": temperature.quantile(
                        0.95
                    ),
                    "maximum_abs_peak_temperature_error_c": temperature.max(),
                }
            )
    return comparisons, pd.DataFrame(summaries)


def build_uncertainty_tables(analysis_path):
    """Return hash-authenticated derived tables for the publication figure receipt."""
    analysis = Path(analysis_path).resolve()
    manifest = json.loads((analysis / "manifest.json").read_text())
    if manifest.get("status") != "complete":
        raise ValueError("Uncertainty summaries require a complete analysis")
    name = "deduplicated-comparisons.csv"
    raw = (analysis / name).read_bytes()
    if sha(raw) != manifest.get("outputs", {}).get(name):
        raise ValueError("Automatic comparison table differs from its receipt")
    all_rows = pd.read_csv(io.BytesIO(raw))
    rows = all_rows.loc[all_rows.method.eq("automatic")].copy()
    identity = PHYSICAL + ["building_id"]
    if (
        rows.empty
        or rows[identity].isna().any().any()
        or rows.duplicated(identity).any()
    ):
        raise ValueError(
            "Automatic building responses require unique physical identities"
        )
    if not rows.engine_outcome_qualified.eq(True).all():
        raise ValueError("Unqualified outcomes cannot establish uncertainty")
    events, intensity = event_temperature_tables(analysis, manifest, rows)
    return {
        "relative-annual-error-summary.csv": relative_annual_summary(rows),
        "exposed-peak-timing-uncertainty.csv": exposed_timing_summary(rows),
        "event-temperature-comparisons.csv": events,
        "event-temperature-uncertainty.csv": intensity,
    }
