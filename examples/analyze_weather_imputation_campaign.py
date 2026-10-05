"""Analyze paired campaign evidence without changing its immutable runtime.

Run this after the campaign finishes, or use ``--provisional`` for explicitly
labelled checkpoints. Reads never invoke the runner's publication recovery.
Physical outage duplicates receive one weight within each sampling stratum.
Empirical percentiles describe these selected scenarios, not a Swedish climate
or building population. Six building responses are not six climate replicates.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from examples import qualify_weather_campaign_results as Q
except ModuleNotFoundError:
    import qualify_weather_campaign_results as Q

IDENTITY = [
    "site_id",
    "year",
    "building_id",
    "scope",
    "sampling",
    "gap_hours",
    "physical_signature",
]
STRATA = ["building_id", "scope", "sampling", "gap_hours"]
EVENTS = (
    "heatwave",
    "cold_spell",
    "warm_spell",
    "relative_cold_spell",
    "high_wind",
    "temperature_jump",
)
SOLAR_EVENTS = ("nighttime_solar", "solar_out_of_bounds")
TOLERANCES = {"annual": 1e-8, "peak": 1e-6, "hourly": 1e-6}


def fingerprint(data):
    """Fingerprint one captured immutable byte sequence."""
    return hashlib.sha256(data).hexdigest()


def physical_signature(panel):
    """Identify selected inputs independently of anchor labels or random seeds."""
    blocks = []
    for block in panel["blocks"]:
        start, end = (pd.Timestamp(block[key]) for key in ("start_utc", "end_utc"))
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("Physical outage blocks require aware timestamps")
        hours = int(block["hours"])
        if end - start != pd.Timedelta(hours=hours - 1):
            raise ValueError("Physical outage block duration differs from its hours")
        blocks.append(
            (
                start.tz_convert("UTC").isoformat(),
                end.tz_convert("UTC").isoformat(),
                hours,
            )
        )
    body = {
        "scope": panel["scope"],
        "columns": sorted(panel["columns"]),
        "duration_hours": int(panel["duration_hours"]),
        "blocks": sorted(blocks),
    }
    if panel["status"] != "ready":
        # Unsupported requests have no physical truth mask to deduplicate.
        body["unsupported_request"] = panel["evidence_id"]
    return fingerprint(json.dumps(body, sort_keys=True).encode())


def deduplicate(rows):
    """Retain one identical physical response and preserve its evidence labels."""
    if rows.empty:
        return rows.copy()
    keys = IDENTITY + ["method"]
    records = []
    checked = [
        c
        for c in rows.columns
        if c.startswith(
            ("annual_", "delta_", "peak_", "weather_", "hourly_", "maximum_")
        )
        and pd.api.types.is_numeric_dtype(rows[c])
    ]
    for _, group in rows.sort_values("case_id").groupby(keys, dropna=False, sort=True):
        for column in checked:
            values = pd.to_numeric(group[column], errors="coerce").to_numpy(dtype=float)
            if not np.allclose(values, values[0], rtol=0, atol=1e-9, equal_nan=True):
                raise ValueError(
                    f"Identical physical outage responses differ: {column}"
                )
        record = group.iloc[0].to_dict()
        record["evidence_ids"] = json.dumps(sorted(set(group.evidence_id)))
        record["anchor_names"] = json.dumps(sorted(set(group.anchor_name.dropna())))
        record["duplicate_evidence_count"] = group.evidence_id.nunique()
        records.append(record)
    return pd.DataFrame(records)


def _number(value):
    """Represent missing descriptive statistics as JSON-compatible null values."""
    return float(value) if pd.notna(value) and np.isfinite(value) else None


def _stats(values, prefix):
    """Describe observed finite values without population confidence claims."""
    values = (
        pd.to_numeric(values, errors="coerce")
        .replace([np.inf, -np.inf], np.nan)
        .dropna()
    )
    result = {prefix + "_n": len(values)}
    for label, value in {
        "mean": values.mean(),
        "median": values.median(),
        "p05": values.quantile(0.05),
        "p95": values.quantile(0.95),
        "max": values.max(),
    }.items():
        result[prefix + "_" + label] = _number(value)
    return result


def _metrics(rows):
    """Derive absolute area-normalized errors using each building's reference."""
    if rows.empty:
        return rows.copy(), []
    baselines = rows.loc[rows.method.eq("reference")]
    base_keys = ["site_id", "year", "building_id"]
    if baselines.duplicated(base_keys).any():
        raise ValueError("Repeated reference building response")
    columns = base_keys + [
        f"{quantity}_{mode}_{unit}"
        for mode in ("heating", "cooling")
        for quantity, unit in (("annual", "kwh_m2"), ("peak", "w_m2"))
    ]
    baseline = baselines[columns].rename(
        columns={c: "baseline_" + c for c in columns if c not in base_keys}
    )
    selected = (
        rows.loc[rows.method.ne("reference")]
        .merge(baseline, on=base_keys, validate="many_to_one")
        .copy()
    )
    metrics = []
    for mode in ("heating", "cooling"):
        annual = f"absolute_annual_{mode}_error_kwh_m2"
        peak = f"absolute_peak_{mode}_error_w_m2"
        hourly = f"hourly_{mode}_load_mae_w_m2"
        maximum = f"maximum_absolute_{mode}_load_change_w_m2"
        selected[annual] = selected[f"delta_annual_{mode}_kwh_m2"].abs()
        selected[peak] = (
            selected[f"peak_{mode}_w_m2"] - selected[f"baseline_peak_{mode}_w_m2"]
        ).abs()
        selected[hourly] = (
            selected[f"hourly_{mode}_load_mae_kw"] * 1000 / selected.floor_area_m2
        )
        selected[maximum] = (
            selected[f"maximum_absolute_{mode}_load_change_kw"]
            * 1000
            / selected.floor_area_m2
        )
        metrics.extend([annual, peak, hourly, maximum])
    selected["baseline_cooling_below_1_kwh_m2"] = (
        selected.baseline_annual_cooling_kwh_m2.lt(1)
    )
    return selected, metrics


def summarize(rows, metrics, extra=()):
    """Keep empirical tails and equal site-year/location means separately."""
    records = []
    if rows.empty:
        return pd.DataFrame()
    keys = STRATA + list(extra)
    for labels, group in rows.groupby(keys, dropna=False, sort=True):
        record = dict(zip(keys, labels)) | {
            "comparisons": len(group),
            "physical_outages": len(
                group[["site_id", "year", "physical_signature"]].drop_duplicates()
            ),
            "site_years": len(group[["site_id", "year"]].drop_duplicates()),
            "locations": group.site_id.nunique(),
            "years": group.year.nunique(),
        }
        for metric in metrics:
            record |= _stats(group[metric], metric)
            finite = (
                group.assign(_value=pd.to_numeric(group[metric], errors="coerce"))
                .replace([np.inf, -np.inf], np.nan)
                .dropna(subset=["_value"])
            )
            means = finite.groupby(["site_id", "year"])._value.mean()
            record[metric + "_equal_site_year_mean"] = _number(means.mean())
            record[metric + "_equal_location_mean"] = _number(
                means.groupby(level=0).mean().mean()
            )
            record[metric + "_site_year_mean_p95"] = _number(means.quantile(0.95))
        for mode in ("heating", "cooling"):
            eligible = group[f"baseline_peak_{mode}_w_m2"].gt(1e-6)
            exposed = group[f"baseline_{mode}_peak_hidden"].eq(True) & eligible
            same = group[f"same_{mode}_peak_hours"].eq(True)
            record[f"{mode}_peak_exposed_n"] = int(exposed.sum())
            record[f"{mode}_peak_unexposed_n"] = int((eligible & ~exposed).sum())
            record[f"{mode}_zero_or_numerical_baseline_peak_n"] = int((~eligible).sum())
            record[f"{mode}_peak_preserved_exposed_n"] = int((exposed & same).sum())
            record[f"{mode}_peak_preserved_exposed_fraction"] = _number(
                same[exposed].mean()
            )
        record["tiny_baseline_cooling_n"] = int(
            group.baseline_cooling_below_1_kwh_m2.sum()
        )
        records.append(record)
    return pd.DataFrame(records)


def paired_hierarchy(rows, metrics):
    """Pair both automatic hierarchies including incomplete donor series cohorts."""
    if rows.empty:
        return pd.DataFrame(), pd.DataFrame()
    pairs = rows.loc[rows.method.eq("automatic")].merge(
        rows.loc[rows.method.eq("automatic_legacy")],
        on=IDENTITY,
        suffixes=("_new", "_old"),
        validate="one_to_one",
    )
    if pairs.empty:
        return pairs, pd.DataFrame()
    for mode in ("heating", "cooling"):
        field = f"baseline_{mode}_peak_hidden"
        if not pairs[field + "_new"].equals(pairs[field + "_old"]):
            raise ValueError("Paired hierarchies have different peak exposure")
    result = pairs[IDENTITY].copy()
    # Keep each qualified source row addressable through report publication.
    for side in ("new", "old"):
        result["case_id_" + side] = pairs["case_id_" + side]
    result["donor_all_five_series_available"] = (
        pairs.donor_all_five_series_available_new
    )
    result["anchor_names"] = pairs.anchor_names_new
    for metric in metrics:
        result[metric + "_new"] = pairs[metric + "_new"]
        result[metric + "_old"] = pairs[metric + "_old"]
        result[metric + "_change"] = pairs[metric + "_new"] - pairs[metric + "_old"]
    for mode in ("heating", "cooling"):
        for field in (f"baseline_{mode}_peak_hidden", f"same_{mode}_peak_hours"):
            result[field + "_new"] = pairs[field + "_new"]
            result[field + "_old"] = pairs[field + "_old"]
        result[f"baseline_peak_{mode}_w_m2"] = pairs[f"baseline_peak_{mode}_w_m2_new"]
    records = []
    for labels, group in result.groupby(STRATA, dropna=False, sort=True):
        record = dict(zip(STRATA, labels)) | {
            "pairs": len(group),
            "site_years": len(group[["site_id", "year"]].drop_duplicates()),
            "locations": group.site_id.nunique(),
            "partial_donor_series_pairs": int(
                (~group.donor_all_five_series_available).sum()
            ),
        }
        for metric in metrics:
            change = group[metric + "_change"]
            tolerance = TOLERANCES[
                "annual"
                if "annual" in metric
                else "peak"
                if "peak" in metric
                else "hourly"
            ]
            record |= _stats(change, metric + "_change")
            record[metric + "_improved_n"] = int(change.lt(-tolerance).sum())
            record[metric + "_worsened_n"] = int(change.gt(tolerance).sum())
            record[metric + "_tied_n"] = int(change.abs().le(tolerance).sum())
            site_year = (
                group.assign(_change=change).groupby(["site_id", "year"])._change.mean()
            )
            record[metric + "_equal_site_year_mean_change"] = _number(site_year.mean())
            record[metric + "_equal_location_mean_change"] = _number(
                site_year.groupby(level=0).mean().mean()
            )
            record[metric + "_site_year_favor_new_n"] = int(
                site_year.lt(-tolerance).sum()
            )
            record[metric + "_site_year_favor_old_n"] = int(
                site_year.gt(tolerance).sum()
            )
        for mode in ("heating", "cooling"):
            eligible = group[f"baseline_peak_{mode}_w_m2"].gt(1e-6)
            exposed = group[f"baseline_{mode}_peak_hidden_new"].eq(True) & eligible
            record[f"{mode}_peak_exposed_n"] = int(exposed.sum())
            record[f"{mode}_peak_unexposed_n"] = int((eligible & ~exposed).sum())
            record[f"{mode}_zero_or_numerical_baseline_peak_n"] = int((~eligible).sum())
            for side in ("new", "old"):
                same = group[f"same_{mode}_peak_hours_{side}"].eq(True)
                record[f"{mode}_peak_preserved_{side}_exposed_n"] = int(
                    (exposed & same).sum()
                )
                record[f"{mode}_peak_preserved_{side}_exposed_fraction"] = _number(
                    same[exposed].mean()
                )
        records.append(record)
    return result, pd.DataFrame(records)


def common_method_cohort(rows, matrix):
    """Retain full physical panels qualified for every supported method and building."""
    selected = []
    if rows.empty:
        return rows.copy()
    physical = [key for key in IDENTITY if key != "building_id"]
    buildings = set(matrix["buildings"])
    for _, group in rows.groupby(physical, dropna=False, sort=True):
        scope, hours = group.iloc[0][["scope", "gap_hours"]]
        required = set(
            matrix["meteorology_methods" if scope == "meteorology" else "solar_methods"]
        )
        if hours > 48:
            required.discard("temporal")
        if set(group.building_id) == buildings and all(
            required.issubset(set(building.method))
            for _, building in group.groupby("building_id", sort=True)
        ):
            selected.append(group.loc[group.method.isin(required)])
    return pd.concat(selected, ignore_index=True) if selected else rows.iloc[:0].copy()


def event_exposure(panel, comparison, kind):
    """Count event exposure and distinguish overlap matching from exact preservation."""
    intervals = pd.DatetimeIndex(
        pd.to_datetime(panel["masked_utc"], utc=True)
    ) - pd.Timedelta(hours=1)
    before = [e for e in comparison["reference"]["events"] if e["type"] == kind]
    values = comparison["by_type"][kind]
    exposed, covered = set(), np.zeros(len(intervals), dtype=bool)
    for i, event in enumerate(before):
        inside = (intervals >= pd.Timestamp(event["start_lst"])) & (
            intervals < pd.Timestamp(event["end_lst"])
        )
        if inside.any():
            exposed.add(i)
            covered |= inside
    matched = {m["reference_index"] for m in values["matches"]}
    exact = {
        m["reference_index"]
        for m in values["matches"]
        if m["start_shift_hours"] == 0 and m["end_shift_hours"] == 0
    }
    exact_peak = {
        m["reference_index"]
        for m in values["matches"]
        if m["start_shift_hours"] == 0
        and m["end_shift_hours"] == 0
        and m["peak_value_change"] is not None
        and abs(m["peak_value_change"]) <= 1e-8
    }
    return {
        "reference_event_count": len(before),
        "reference_exposed_events": len(exposed),
        "reference_unexposed_events": len(before) - len(exposed),
        "reference_hours_hidden": int(covered.sum()),
        "exposed_events_with_any_overlap": len(exposed & matched),
        "exposed_events_with_exact_bounds": len(exposed & exact),
        "exposed_events_with_exact_bounds_and_peak": len(exposed & exact_peak),
        "lost_exposed_reference_events": len(exposed - matched),
        "lost_unexposed_reference_events": len(
            set(range(len(before))) - exposed - matched
        ),
        "lost_reference_event_count": values["lost_reference_count"],
        "new_candidate_event_count": values["false_candidate_count"],
        "reference_event_hours": values["reference_hours"],
        "candidate_event_hours": values["candidate_hours"],
    }


def events_summary(root, rows, plans, inputs):
    """Evaluate exposed weather events once per outage and method, across buildings."""
    if rows.empty:
        return pd.DataFrame(), pd.DataFrame()
    keys = [c for c in IDENTITY if c != "building_id"] + ["method"]
    records = []
    for _, group in rows.groupby(keys, dropna=False, sort=True):
        row = group.iloc[0]
        path = (
            root
            / "datasets"
            / row.site_id
            / str(row.year)
            / "cases"
            / row.case_id
            / "events.json"
        )
        data = path.read_bytes()
        inputs[str(path)] = fingerprint(data)
        comparison = json.loads(data)
        panel = plans[(row.site_id, row.year, row.evidence_id)]
        for kind in EVENTS if row.scope == "meteorology" else SOLAR_EVENTS:
            records.append(
                {c: row[c] for c in keys}
                | {
                    "event_type": kind,
                    "anchor_names": row.anchor_names,
                    **event_exposure(panel, comparison, kind),
                }
            )
    events = pd.DataFrame(records)
    if events.empty:
        return events, pd.DataFrame()
    summaries = []
    for labels, group in events.groupby(
        ["scope", "sampling", "gap_hours", "method", "event_type"], sort=True
    ):
        record = dict(
            zip(["scope", "sampling", "gap_hours", "method", "event_type"], labels)
        ) | {
            "weather_outages": len(group),
            "site_years": len(group[["site_id", "year"]].drop_duplicates()),
            "outages_exposing_reference_events": int(
                group.reference_exposed_events.gt(0).sum()
            ),
            "outages_without_reference_event_exposure": int(
                group.reference_exposed_events.eq(0).sum()
            ),
        }
        for column in [
            c for c in events if c not in keys + ["event_type", "anchor_names"]
        ]:
            record[column] = int(group[column].sum())
        denominator = int(group.reference_exposed_events.sum())
        for label in ("any_overlap", "exact_bounds", "exact_bounds_and_peak"):
            record["exposed_events_" + label + "_fraction"] = (
                float(group["exposed_events_with_" + label].sum()) / denominator
                if denominator
                else None
            )
        summaries.append(record)
    return events, pd.DataFrame(summaries)


def weather_summary(rows):
    """Describe masked weather errors once per physical outage and method."""
    if rows.empty:
        return pd.DataFrame(), pd.DataFrame()
    keys = [c for c in IDENTITY if c != "building_id"] + ["method"]
    metrics = [c for c in rows if c.startswith("weather_")]
    weather = rows[keys + ["anchor_names"] + metrics].drop_duplicates(keys)
    records = []
    groups = ["scope", "sampling", "gap_hours", "method"]
    for labels, group in weather.groupby(groups, sort=True):
        record = dict(zip(groups, labels)) | {
            "physical_outages": len(group),
            "site_years": len(group[["site_id", "year"]].drop_duplicates()),
            "locations": group.site_id.nunique(),
        }
        for metric in metrics:
            if metric.endswith("_hours"):
                continue
            record |= _stats(group[metric], metric)
            means = group.groupby(["site_id", "year"])[metric].mean()
            record[metric + "_equal_site_year_mean"] = _number(means.mean())
            record[metric + "_equal_location_mean"] = _number(
                means.groupby(level=0).mean().mean()
            )
        records.append(record)
    return weather, pd.DataFrame(records)


def availability_status(panel, method, decision):
    """Normalize actual catalogue statuses while retaining execution preparation gaps."""
    if panel["status"] != "ready":
        return "unsupported_truth"
    if method == "temporal" and panel["duration_hours"] > 48:
        return "unsupported_method"
    status = decision.get("status", "available" if decision else "not_prepared")
    if status == "ready":
        return "available"
    if status == "unsupported":
        return "unsupported_method"
    if status not in {"available", "unavailable", "not_prepared"}:
        raise ValueError(f"Unknown catalogue availability status: {status}")
    return status


def analyze(root, output, *, provisional=False, qualifier=None):
    """Read verified snapshots and publish an independent, explicitly scoped analysis."""
    root, output = Path(root).resolve(), Path(output).resolve()
    matrix_bytes = (root / "matrix.json").read_bytes()
    matrix = json.loads(matrix_bytes)
    captured, panels, availability, source_records, inputs, incomplete = (
        [],
        [],
        [],
        [],
        {},
        [],
    )
    plans = {}
    for site in matrix["sites"]:
        for year in matrix["years"]:
            folder = root / "datasets" / site["site_id"] / str(year)
            source_path = folder / "sources/receipt.json"
            if not source_path.exists():
                incomplete.append(f"{site['site_id']}/{year}: sources absent")
                continue
            source_bytes = source_path.read_bytes()
            source = json.loads(source_bytes)
            inputs[str(source_path)] = fingerprint(source_bytes)
            donor_ready = (
                source.get("status") == "ready"
                and all(
                    value["status"] == "available"
                    for value in source.get("donor", {}).values()
                )
                and len(source.get("donor", {})) == 5
            )
            source_records.append(
                {
                    "site_id": site["site_id"],
                    "year": year,
                    "status": source["status"],
                    "donor_all_five_series_available": donor_ready,
                    "reference_station_id": source.get("station", {}).get("station_id"),
                    "reference_latitude": source.get("latitude"),
                    "reference_longitude": source.get("longitude"),
                    "native_required_fraction": source.get("native_required_fraction"),
                    "source_reason": source.get(
                        "eligibility_reason", source.get("reason")
                    ),
                }
            )
            if source["status"] != "ready":
                continue
            audit_path = folder / "audit.json"
            audit = json.loads(audit_path.read_text()) if audit_path.exists() else {}
            if not audit.get("full_matrix_site_year_complete"):
                incomplete.append(f"{site['site_id']}/{year}: simulations incomplete")
            plan_path, catalogue_path = (
                folder / "panel-plan.json",
                folder / "case-catalogue.json",
            )
            if not plan_path.exists():
                incomplete.append(f"{site['site_id']}/{year}: panel plan absent")
                continue
            plan_bytes = plan_path.read_bytes()
            inputs[str(plan_path)] = fingerprint(plan_bytes)
            plan = json.loads(plan_bytes)["all_panels"]
            if catalogue_path.exists():
                catalogue_bytes = catalogue_path.read_bytes()
                inputs[str(catalogue_path)] = fingerprint(catalogue_bytes)
                catalogue = json.loads(catalogue_bytes)
            else:
                catalogue = {"cases": [], "unavailable": []}
                incomplete.append(f"{site['site_id']}/{year}: catalogue absent")
            decisions = {
                c["case_id"]: c for c in catalogue["cases"] + catalogue["unavailable"]
            }
            evidence = {p["evidence_id"]: p for p in plan}
            plans.update({(site["site_id"], year, p["evidence_id"]): p for p in plan})
            signatures = {p["evidence_id"]: physical_signature(p) for p in plan}
            table_path = folder / "summary.csv"
            table = pd.DataFrame()
            if table_path.exists():
                data = table_path.read_bytes()
                sidecar_path = Path(str(table_path) + ".receipt.json")
                sidecar = (
                    json.loads(sidecar_path.read_text())
                    if sidecar_path.exists()
                    else {}
                )
                if sidecar.get("sha256") == fingerprint(data):
                    table = pd.read_csv(io.BytesIO(data))
                    if len(table) != sidecar.get("rows"):
                        raise ValueError("Snapshot row count differs from its receipt")
                    inputs[str(table_path)] = fingerprint(data)
                    if not table.empty:
                        table["physical_signature"] = table.evidence_id.map(signatures)
                        table.loc[
                            table.method.eq("reference"), "physical_signature"
                        ] = "reference"
                        if table.physical_signature.isna().any():
                            raise ValueError(
                                "Evaluated evidence is absent from the pinned panel plan"
                            )
                        table["anchor_name"] = table.evidence_id.map(
                            {k: p.get("anchor_name") for k, p in evidence.items()}
                        )
                        table["donor_all_five_series_available"] = donor_ready
                        captured.append(table)
                else:
                    incomplete.append(
                        f"{site['site_id']}/{year}: checkpoint publication in progress"
                    )
            else:
                incomplete.append(f"{site['site_id']}/{year}: checkpoint absent")
            if audit.get("full_matrix_site_year_complete"):
                expected_rows = len(catalogue["cases"]) * len(matrix["buildings"])
                if len(table) != expected_rows or len(table) != audit.get(
                    "comparisons"
                ):
                    incomplete.append(
                        f"{site['site_id']}/{year}: complete audit/checkpoint/catalogue counts disagree"
                    )
            for panel in plan:
                physical = signatures[panel["evidence_id"]]
                panels.append(panel | {"physical_signature": physical})
                methods = matrix[
                    "meteorology_methods"
                    if panel["scope"] == "meteorology"
                    else "solar_methods"
                ]
                for method in methods:
                    case_id = f"{panel['evidence_id']}-{method}"
                    decision = decisions.get(case_id, {})
                    status = availability_status(panel, method, decision)
                    evaluated = (
                        table.loc[table.case_id.eq(case_id)].building_id.nunique()
                        if not table.empty
                        else 0
                    )
                    availability.append(
                        {
                            "site_id": site["site_id"],
                            "year": year,
                            "scope": panel["scope"],
                            "sampling": panel["sampling"],
                            "gap_hours": panel["duration_hours"],
                            "method": method,
                            "case_id": case_id,
                            "physical_signature": physical,
                            "status": status,
                            "truth_ready": panel["status"] == "ready",
                            "evaluated_buildings": evaluated,
                            "reason": decision.get("reason", panel.get("reason")),
                        }
                    )
    if incomplete and not provisional:
        raise ValueError(
            "Campaign is incomplete; --provisional explicitly labels partial evidence: "
            + "; ".join(incomplete)
        )
    rows = pd.concat(captured, ignore_index=True) if captured else pd.DataFrame()
    # A supplied qualifier supports explicit isolated fixtures only; real CLI
    # analyses always authenticate every diagnostic artifact through Q.
    annotated, exclusions, qualification = (qualifier or Q.qualify_results)(
        root, rows, inputs
    )
    if qualifier is not None:
        qualification["evidence_mode"] = "injected_fixture"
    qualified = annotated.loc[annotated.engine_outcome_qualified.eq(True)].copy()
    raw_selected = (
        deduplicate(annotated.loc[annotated.method.ne("reference")])
        if not annotated.empty
        else annotated.copy()
    )
    selected, metrics = _metrics(qualified)
    selected = deduplicate(selected)
    pairs, paired_summary = paired_hierarchy(selected, metrics)
    common = common_method_cohort(selected, matrix)
    events, event_summary = events_summary(root, raw_selected, plans, inputs)
    weather, weather_errors = weather_summary(raw_selected)
    raw_common = common_method_cohort(raw_selected, matrix)
    common_weather, common_weather_errors = weather_summary(raw_common)
    availability_table = pd.DataFrame(availability)
    availability_summary = []
    if not availability_table.empty:
        qualification_counts = (
            {
                (site, year, case_id): (
                    int(group.engine_outcome_qualified.sum()),
                    int((~group.engine_outcome_qualified).sum()),
                )
                for (site, year, case_id), group in annotated.groupby(
                    ["site_id", "year", "case_id"], sort=True
                )
            }
            if not annotated.empty
            else {}
        )
        counts = [
            qualification_counts.get((r.site_id, r.year, r.case_id), (0, 0))
            for r in availability_table.itertuples(index=False)
        ]
        availability_table["qualified_buildings"] = [c[0] for c in counts]
        availability_table["engine_unqualified_buildings"] = [c[1] for c in counts]
        identity = [
            "site_id",
            "year",
            "scope",
            "sampling",
            "gap_hours",
            "method",
            "physical_signature",
        ]
        for _, group in availability_table.groupby(identity, dropna=False, sort=True):
            if group.status.nunique() != 1:
                raise ValueError(
                    "Identical physical outage has inconsistent method availability"
                )
        availability_table = availability_table.sort_values(
            "evaluated_buildings", ascending=False
        ).drop_duplicates(identity)
        for labels, group in availability_table.groupby(
            ["scope", "sampling", "gap_hours", "method"], sort=True
        ):
            counts = Counter(group.status)
            ready = int(group.truth_ready.sum())
            availability_summary.append(
                dict(zip(["scope", "sampling", "gap_hours", "method"], labels))
                | {
                    "planned_distinct_panels": len(group),
                    "truth_ready_panels": ready,
                    "available_weather_panels": counts["available"],
                    "unavailable_weather_panels": counts["unavailable"],
                    "unsupported_truth_panels": counts["unsupported_truth"],
                    "unsupported_method_panels": counts["unsupported_method"],
                    "not_prepared_panels": counts["not_prepared"],
                    "available_fraction_of_truth_ready": counts["available"] / ready
                    if ready
                    else None,
                    "panels_with_any_building_evaluated": int(
                        group.evaluated_buildings.gt(0).sum()
                    ),
                    "panels_with_all_buildings_evaluated": int(
                        group.evaluated_buildings.eq(len(matrix["buildings"])).sum()
                    ),
                    "qualified_building_comparisons": int(
                        group.qualified_buildings.sum()
                    ),
                    "engine_unqualified_building_comparisons": int(
                        group.engine_unqualified_buildings.sum()
                    ),
                    "panels_with_any_qualified_building": int(
                        group.qualified_buildings.gt(0).sum()
                    ),
                    "panels_with_all_buildings_qualified": int(
                        group.qualified_buildings.eq(len(matrix["buildings"])).sum()
                    ),
                    "engine_unqualified_panels": int(
                        group.engine_unqualified_buildings.gt(0).sum()
                    ),
                }
            )
    outputs = {
        "sources.csv": pd.DataFrame(source_records),
        "diagnostic-exclusions.csv": exclusions,
        "deduplicated-comparisons.csv": selected,
        "method-error-summary.csv": summarize(selected, metrics, ("method",)),
        "hierarchy-pairs.csv": pairs,
        "hierarchy-summary.csv": paired_summary,
        "common-method-comparisons.csv": common,
        "common-method-error-summary.csv": summarize(common, metrics, ("method",)),
        "method-availability.csv": pd.DataFrame(availability_summary),
        "method-availability-panels.csv": availability_table,
        "physical-panels.csv": pd.DataFrame(panels),
        "event-exposure-comparisons.csv": events,
        "event-exposure-summary.csv": event_summary,
        "weather-comparisons.csv": weather,
        "weather-error-summary.csv": weather_errors,
        "common-method-weather-comparisons.csv": common_weather,
        "common-method-weather-error-summary.csv": common_weather_errors,
    }
    output.mkdir(parents=True, exist_ok=True)
    output_hashes = {}
    for name, data in outputs.items():
        path = output / name
        data.to_csv(path, index=False)
        output_hashes[name] = fingerprint(path.read_bytes())
    qualification_path = output / "diagnostics-qualification.json"
    qualification_path.write_text(
        json.dumps(qualification, indent=2, allow_nan=False) + "\n"
    )
    output_hashes[qualification_path.name] = fingerprint(
        qualification_path.read_bytes()
    )
    manifest = {
        "status": "provisional" if provisional or incomplete else "complete",
        "created_utc": datetime.now(UTC).isoformat(),
        "campaign_root": str(root.resolve()),
        "matrix_sha256": fingerprint(matrix_bytes),
        "analysis_script_sha256": fingerprint(Path(__file__).read_bytes()),
        "analysis_interpreter": {
            "python": sys.version,
            "numpy": np.__version__,
            "pandas": pd.__version__,
        },
        "inputs": inputs,
        "outputs": output_hashes,
        "incomplete": incomplete,
        "captured_building_rows": len(rows),
        "qualified_building_rows": len(qualified),
        "qualified_reference_building_rows": int(qualified.method.eq("reference").sum())
        if not qualified.empty
        else 0,
        "qualified_reference_site_years": len(
            qualified.loc[
                qualified.method.eq("reference"), ["site_id", "year"]
            ].drop_duplicates()
        )
        if not qualified.empty
        else 0,
        "excluded_building_rows": len(exclusions),
        "qualified_nonreference_building_rows": int(
            qualified.method.ne("reference").sum()
        )
        if not qualified.empty
        else 0,
        "excluded_nonreference_building_rows": int(
            exclusions.method.ne("reference").sum()
        )
        if not exclusions.empty
        else 0,
        "raw_deduplicated_comparisons": len(raw_selected),
        "diagnostics_qualification_policy": qualification["policy"],
        "diagnostics_qualification_sha256": output_hashes[qualification_path.name],
        "deduplicated_comparisons": len(selected),
        "hierarchy_pairs": len(pairs),
        "common_method_comparisons": len(common),
        "methodology": {
            "primary_errors": "Absolute annual sensible-load error in kWh/m2, peak/hourly errors in W/m2; percentage errors remain secondary raw fields",
            "tiny_cooling_flag": "Reference annual cooling below1 kWh/m2 is a descriptive flag, not an exclusion; absolute errors include these rows",
            "deduplication": "One physical scope/column-set/duration/UTC-block mask per site-year/building/method/sampling stratum; anchor labels retained",
            "hierarchy_cohort": "All common new/old automatic panels, including partial donor-series references",
            "forced_method_cohort": "Only identical physical panels with every matrix building qualified for every scheduled method, excluding temporal above48h; losing one method/profile removes the entire panel from building method ranking",
            "engine_qualification": "Every warning excludes all building outcomes conservatively, including annual heating accepted under a scoped Core exception; unqualified baselines exclude related profile/site-year outcomes. Diagnostic receipts, results, logs, models and canonical/alias weather are authenticated without modifying frozen evidence",
            "availability": "Distinct weather panels, independent of six building responses; truth eligibility, source/validation failures and RAW evaluated coverage remain separate. Qualified building comparisons and engine-unqualified panels describe outcome eligibility without relabeling available weather",
            "weather_engine_independence": "Weather errors and event preservation use RAW deduplicated weather cases even when building outcomes are engine-unqualified; common-method weather tables require weather availability rather than engine-qualified outcomes",
            "weather_errors": "Masked-input errors once per physical outage/method; weather units remain C, RH percentage points, pressurePa, windm/s, circular degrees, radiationW/m2; building responses do not multiply weather evidence",
            "peak_preservation": "Preservation fractions only among baseline peaks hidden by the outage; unexposed and zero/numerical baseline peaks reported separately. Numerical floor1e-6W/m2 excludes meaningless zero-load timing",
            "event_preservation": "Event counts, not lost hours; exposed reference events matched by any overlap and exact bounds/peak reported separately, once per weather case independent of buildings. Fractions describe event-exposure scenarios, not independent unique weather events",
            "aggregation": "Empirical scenario quantiles; equal site-year means and equal-location means across available years shown separately",
            "population_inference": "No population confidence intervals or national certainty; sites/years/profiles/events are selected, and year-specific station coordinates differ",
            "solar_truth": "Hybrid measured-GHI/STRANG reference; component errors are not validation against independently observed DNI/DHI",
            "hourly_load_mae": "Mean across the full8760-hour year, with affected-window energy and maximum hourly deviations retained in comparison rows",
            "building_exposure": "Six synthetic Core profiles, constant18C conditioned-floor boundary and ideal sensible loads; not measured buildings or delivered fuel/electricity",
            "tie_tolerances": TOLERANCES,
        },
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, allow_nan=False) + "\n"
    )
    return manifest


def main():
    """Write independent descriptive tables after verifying captured campaign inputs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--provisional", action="store_true")
    args = parser.parse_args()
    manifest = analyze(
        args.root, args.output or args.root / "analysis", provisional=args.provisional
    )
    print(
        json.dumps(
            {
                k: manifest[k]
                for k in (
                    "status",
                    "captured_building_rows",
                    "deduplicated_comparisons",
                    "hierarchy_pairs",
                    "common_method_comparisons",
                )
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
