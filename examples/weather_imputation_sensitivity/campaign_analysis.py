"""Describe paired reconstruction errors without treating buildings as climates.

Quantiles describe the evaluated scenarios. They are not population confidence
intervals: sites, selected years, buildings and event masks are deliberately
chosen rather than an independent random sample of Swedish weather/buildings.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

GROUPS = ["building_id", "scope", "sampling", "gap_hours", "method"]


def summarize(rows: pd.DataFrame) -> pd.DataFrame:
    """Give empirical error tails and denominators for each comparable stratum."""
    selected = rows.loc[rows.method.ne("reference")]
    records = []
    for keys, group in selected.groupby(GROUPS, dropna=False, sort=True):
        record = dict(zip(GROUPS, keys))
        record.update(
            comparisons=len(group),
            site_years=len(group[["site_id", "year"]].drop_duplicates()),
            locations=group.site_id.nunique(),
            years=group.year.nunique(),
            evidence_panels=group.evidence_id.nunique(),
        )
        for mode in ("heating", "cooling"):
            for quantity in ("annual", "peak"):
                name = f"delta_{quantity}_{mode}_pct"
                values = pd.to_numeric(group[name], errors="coerce").dropna()
                record[f"{name}_valid_n"] = len(values)
                for label, number in (
                    ("median", values.median()),
                    ("p05", values.quantile(0.05)),
                    ("p95", values.quantile(0.95)),
                    ("abs_p95", values.abs().quantile(0.95)),
                    ("abs_max", values.abs().max()),
                ):
                    record[f"{name}_{label}"] = float(number) if len(values) else None
                clustered = group.assign(
                    _value=pd.to_numeric(group[name], errors="coerce")
                ).dropna(subset=["_value"])
                site_year = clustered.groupby(["site_id", "year"])["_value"]
                medians = site_year.median()
                absolute = site_year.apply(lambda numbers: numbers.abs().median())
                record[f"{name}_site_year_median"] = (
                    float(medians.median()) if len(medians) else None
                )
                record[f"{name}_site_year_abs_median_p95"] = (
                    float(absolute.quantile(0.95)) if len(absolute) else None
                )
            same = group[f"same_{mode}_peak_hours"].dropna().astype(bool)
            record[f"same_{mode}_peak_fraction"] = (
                float(same.mean()) if len(same) else None
            )
            record[f"same_{mode}_peak_valid_n"] = len(same)
            covered = group[f"baseline_{mode}_peak_hidden"].dropna().astype(bool)
            record[f"baseline_{mode}_peak_hidden_fraction"] = (
                float(covered.mean()) if len(covered) else None
            )
        records.append(record)
    return pd.DataFrame(records)


def paired_hierarchy(rows: pd.DataFrame) -> pd.DataFrame:
    """Compare the two priorities only for identical evidence and buildings."""
    keys = [
        "site_id",
        "year",
        "building_id",
        "evidence_id",
        "scope",
        "sampling",
        "gap_hours",
    ]
    left = rows.loc[rows.method.eq("automatic")]
    right = rows.loc[rows.method.eq("automatic_legacy")]
    if left.duplicated(keys).any() or right.duplicated(keys).any():
        raise ValueError("Duplicate hierarchy comparison for one paired evidence panel")
    paired = left.merge(
        right, on=keys, suffixes=("_new", "_old"), validate="one_to_one"
    )
    result = paired[keys].copy()
    for mode in ("heating", "cooling"):
        for quantity in ("annual", "peak"):
            name = f"delta_{quantity}_{mode}_pct"
            new = pd.to_numeric(paired[f"{name}_new"], errors="coerce")
            old = pd.to_numeric(paired[f"{name}_old"], errors="coerce")
            result[f"{name}_new"] = new
            result[f"{name}_old"] = old
            result[f"{name}_absolute_error_change_pp"] = new.abs() - old.abs()
            result[f"{name}_improved"] = np.where(
                new.notna() & old.notna(), new.abs() < old.abs() - 1e-9, None
            )
    return result
