"""Plot weather-event preservation on paired physical meteorological outages.

Usage: python examples/plot_weather_campaign_events.py CAMPAIGN/analysis
Incomplete analyses require --provisional; every resulting title is marked.
Reference episodes are counted across outage scenarios, once per weather case,
without multiplying evidence by the six building profiles. These descriptive
fractions do not estimate national uncertainty or future-event probabilities.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import Patch

IDENTITY = [
    "site_id",
    "year",
    "scope",
    "sampling",
    "gap_hours",
    "physical_signature",
]
EVENTS = (
    ("heatwave", "Heatwave (Tmax ≥25°C for ≥3 days)"),
    ("cold_spell", "Cold spell (Tmin ≤−10°C for ≥3 days)"),
    ("high_wind", "High wind (hourly mean ≥10 m/s)"),
)
SAMPLINGS = (
    ("random_seasonal", "Seasonal random outages"),
    ("weather_extremes", "Weather event outages"),
)
SIDES = (
    ("new", "automatic", "Current hierarchy", "#0072B2"),
    ("old", "automatic_legacy", "Previous hierarchy", "#D55E00"),
)
DURATIONS = (3, 12, 48, 168)
COUNTS = (
    "reference_event_count",
    "reference_exposed_events",
    "reference_unexposed_events",
    "reference_hours_hidden",
    "reference_event_hours",
    "exposed_events_with_any_overlap",
    "exposed_events_with_exact_bounds",
    "exposed_events_with_exact_bounds_and_peak",
    "lost_exposed_reference_events",
    "lost_unexposed_reference_events",
    "lost_reference_event_count",
    "new_candidate_event_count",
)
REFERENCE_COUNTS = COUNTS[:5]


def sha(data):
    """Fingerprint exact input and output bytes for the figure receipt."""
    return hashlib.sha256(data).hexdigest()


def verified_events(root, manifest):
    """Capture the anomaly table only when its bytes match analysis provenance."""
    name = "event-exposure-comparisons.csv"
    data = (root / name).read_bytes()
    if sha(data) != manifest["outputs"].get(name):
        raise ValueError("Event comparison table differs from its analysis receipt")
    try:
        rows = pd.read_csv(io.BytesIO(data))
    except pd.errors.EmptyDataError:
        rows = pd.DataFrame()
    if rows.empty:
        raise ValueError("No weather-event comparisons are available to plot")
    required = IDENTITY + ["method", "event_type"] + list(COUNTS)
    if set(required) - set(rows):
        raise ValueError("Weather-event comparison schema is incomplete")
    return rows


def pair_events(rows):
    """Match original weather evidence across both automatic recovery hierarchies."""
    selected = rows.loc[
        rows.scope.eq("meteorology")
        & rows.method.isin([method for _, method, _, _ in SIDES])
        & rows.event_type.isin([event for event, _ in EVENTS])
    ].copy()
    keys = IDENTITY + ["event_type"]
    if selected.empty or selected[keys + ["method"]].isna().any().any():
        raise ValueError("Identifiable meteorological event comparisons are required")
    if (
        not selected.sampling.isin([sampling for sampling, _ in SAMPLINGS]).all()
        or not selected.gap_hours.isin(DURATIONS).all()
    ):
        raise ValueError(
            "Weather-event plotting strata differ from the campaign design"
        )
    if selected.duplicated(keys + ["method"]).any():
        raise ValueError("Duplicate physical weather cases would overweight events")
    expected = {event for event, _ in EVENTS}
    for _, group in selected.groupby(IDENTITY + ["method"], dropna=False):
        if set(group.event_type) != expected:
            raise ValueError("Each weather case must contain all three anomaly types")
    for column in COUNTS:
        values = pd.to_numeric(selected[column], errors="coerce")
        if (
            values.isna().any()
            or not np.isfinite(values).all()
            or values.lt(0).any()
            or values.ne(np.floor(values)).any()
        ):
            raise ValueError("Weather-event counts must be nonnegative finite integers")
        selected[column] = values.astype("int64")
    exact_peak = selected.exposed_events_with_exact_bounds_and_peak
    exact = selected.exposed_events_with_exact_bounds
    overlap = selected.exposed_events_with_any_overlap
    exposed = selected.reference_exposed_events
    if (
        exact_peak.gt(exact).any()
        or exact.gt(overlap).any()
        or overlap.gt(exposed).any()
        or (overlap + selected.lost_exposed_reference_events).ne(exposed).any()
        or (exposed + selected.reference_unexposed_events)
        .ne(selected.reference_event_count)
        .any()
        or (
            selected.lost_exposed_reference_events
            + selected.lost_unexposed_reference_events
        )
        .ne(selected.lost_reference_event_count)
        .any()
    ):
        raise ValueError("Weather-event counts or retention denominators disagree")
    before = selected.loc[selected.method.eq("automatic")].drop(columns="method")
    after = selected.loc[selected.method.eq("automatic_legacy")].drop(columns="method")
    pairs = before.merge(
        after, on=keys, suffixes=("_new", "_old"), validate="one_to_one"
    )
    if pairs.empty:
        raise ValueError("No matched automatic weather-event cases are available")
    for column in REFERENCE_COUNTS:
        if pairs[column + "_new"].ne(pairs[column + "_old"]).any():
            raise ValueError(
                "Paired weather cases have different reference event evidence"
            )
    matched = pairs[IDENTITY].drop_duplicates()
    metadata = {
        "paired_weather_cases": len(matched),
        "paired_site_years": len(matched[["site_id", "year"]].drop_duplicates()),
        "unmatched_weather_cases": {
            method: len(
                selected.loc[selected.method.eq(method), IDENTITY].drop_duplicates()
            )
            - len(matched)
            for _, method, _, _ in SIDES
        },
    }
    return pairs, metadata


def summarize(pairs):
    """Count exposed reference episodes and preservation without building replication."""
    records = []
    for event, _ in EVENTS:
        for sampling, _ in SAMPLINGS:
            for hours in DURATIONS:
                group = pairs.loc[
                    pairs.event_type.eq(event)
                    & pairs.sampling.eq(sampling)
                    & pairs.gap_hours.eq(hours)
                ]
                exposed = int(group.reference_exposed_events_new.sum())
                reference_fields = {
                    "paired_weather_cases": len(group),
                    "site_years": len(group[["site_id", "year"]].drop_duplicates()),
                    "site_years_with_exposed_events": len(
                        group.loc[
                            group.reference_exposed_events_new.gt(0),
                            ["site_id", "year"],
                        ].drop_duplicates()
                    ),
                    "reference_exposed_events": exposed,
                    "reference_hours_hidden": int(
                        group.reference_hours_hidden_new.sum()
                    ),
                }
                for side, method, _, _ in SIDES:
                    record = {
                        "event_type": event,
                        "sampling": sampling,
                        "gap_hours": hours,
                        "method": method,
                        **reference_fields,
                    }
                    for column in COUNTS[5:]:
                        record[column] = int(group[column + "_" + side].sum())
                    for measure in (
                        "any_overlap",
                        "exact_bounds",
                        "exact_bounds_and_peak",
                    ):
                        record[measure + "_retained_fraction"] = (
                            record["exposed_events_with_" + measure] / exposed
                            if exposed
                            else None
                        )
                    records.append(record)
    return pd.DataFrame(records)


def event_figure(summary, metadata, provisional):
    """Draw exposed-episode retention with visible paired counts and creation losses."""
    fig, axes = plt.subplots(3, 2, figsize=(14, 12))
    for row, (event, event_label) in enumerate(EVENTS):
        for column, (sampling, sampling_label) in enumerate(SAMPLINGS):
            ax = axes[row, column]
            group = summary.loc[
                summary.event_type.eq(event) & summary.sampling.eq(sampling)
            ]
            ticks = []
            for hours in DURATIONS:
                counts = group.loc[group.gap_hours.eq(hours)].set_index("method")
                first = counts.loc["automatic"]
                lost = [
                    int(counts.loc[m, "lost_exposed_reference_events"])
                    for _, m, _, _ in SIDES
                ]
                created = [
                    int(counts.loc[m, "new_candidate_event_count"])
                    for _, m, _, _ in SIDES
                ]
                ticks.append(
                    f"{hours}h\nP{int(first.paired_weather_cases)} · E{int(first.reference_exposed_events)} · Y{int(first.site_years_with_exposed_events)}/{int(first.site_years)}"
                    f"\nLost {lost[0]}/{lost[1]} · new {created[0]}/{created[1]}"
                )
            for offset, (_, method, _, color) in zip((-0.18, 0.18), SIDES):
                values = (
                    group.loc[group.method.eq(method)]
                    .set_index("gap_hours")
                    .reindex(DURATIONS)
                )
                fractions = (
                    values[
                        [
                            "exact_bounds_and_peak_retained_fraction",
                            "exact_bounds_retained_fraction",
                            "any_overlap_retained_fraction",
                        ]
                    ].to_numpy(dtype=float)
                    * 100
                )
                bottom = np.zeros(4)
                for level, (alpha, hatch) in enumerate(
                    ((0.95, None), (0.5, "//"), (0.2, ".."))
                ):
                    heights = fractions[:, level] - (
                        fractions[:, level - 1] if level else 0
                    )
                    ax.bar(
                        np.arange(4) + offset,
                        heights,
                        bottom=bottom,
                        width=0.32,
                        color=color,
                        edgecolor=color,
                        alpha=alpha,
                        hatch=hatch,
                        linewidth=0.7,
                    )
                    bottom = fractions[:, level]
            ax.set_xticks(range(4), ticks)
            ax.set_xlabel("Hours per block", fontsize=9)
            ax.set_xlim(-0.5, 3.5)
            ax.set_ylim(0, 105)
            ax.set_ylabel("Exposed reference episodes\nretained (%)", fontsize=10)
            ax.set_title(event_label + "\n" + sampling_label, fontsize=11)
            ax.tick_params(labelsize=8)
            ax.grid(axis="y", color="#dddddd", linewidth=0.6)
            ax.spines[["top", "right"]].set_visible(False)
            if not group.reference_exposed_events.gt(0).any():
                ax.text(
                    0.5,
                    0.5,
                    "No exposed reference episodes",
                    transform=ax.transAxes,
                    ha="center",
                    color="#666666",
                )
    fig.suptitle(
        "Weather-event preservation on paired meteorological outages"
        + (" — PROVISIONAL" if provisional else ""),
        y=0.99,
        fontsize=16,
        fontweight="bold",
    )
    handles = [Patch(facecolor=color, label=label) for _, _, label, color in SIDES]
    handles += [
        Patch(facecolor="#555555", alpha=0.95, label="Exact boundaries + peak value"),
        Patch(
            facecolor="#555555",
            alpha=0.5,
            hatch="//",
            label="Exact boundaries only",
        ),
        Patch(
            facecolor="#555555",
            alpha=0.2,
            hatch="..",
            label="Any overlap; boundaries differ",
        ),
    ]
    fig.legend(
        handles=handles,
        loc="upper center",
        bbox_to_anchor=(0.5, 0.955),
        ncol=3,
        frameon=False,
        fontsize=10,
    )
    unmatched = metadata["unmatched_weather_cases"]
    caption = "P: paired physical weather cases; E: exposed reference episodes; Y: exposed/paired site-years. Lost/new counts: current/previous; new episodes include cases with E=0.\n"
    caption += "Stacked bars: dark = exact boundaries + peak value, hatched = boundaries only, pale = overlap only. E=0 gives no rate; event peak-hour timing is not scored.\n"
    caption += f"Episodes are repeated across outage scenarios, once per weather case. No building multiplier or population confidence claim. Unmatched cases excluded: current {unmatched['automatic']}, previous {unmatched['automatic_legacy']}."
    caption += "\nSeasonal random: 4 blocks/file (4×hours); weather event: 1 block/file. Method comparisons are within each sampling stratum."
    fig.text(0.5, 0.025, caption, ha="center", va="bottom", fontsize=9)
    fig.subplots_adjust(
        top=0.86, bottom=0.16, left=0.08, right=0.98, hspace=0.58, wspace=0.23
    )
    return fig


def plot(root, *, provisional=False):
    """Verify analysis provenance and publish one paired weather-event figure."""
    root = Path(root)
    manifest_bytes = (root / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    incomplete = manifest["status"] != "complete" or bool(manifest.get("incomplete"))
    if incomplete and not provisional:
        raise ValueError("Incomplete analysis requires explicit --provisional")
    partial = provisional or incomplete
    pairs, metadata = pair_events(verified_events(root, manifest))
    summary = summarize(pairs)
    folder = root / "event-figures"
    folder.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update(
        {"font.family": "DejaVu Sans", "svg.fonttype": "none", "axes.axisbelow": True}
    )
    fig = event_figure(summary, metadata, partial)
    outputs = {}
    for extension in ("png", "svg"):
        path = folder / f"paired-weather-event-preservation.{extension}"
        fig.savefig(path, dpi=200, facecolor="white")
        outputs[path.name] = sha(path.read_bytes())
    plt.close(fig)
    table_path = folder / "figure-data.csv"
    summary.to_csv(table_path, index=False)
    outputs[table_path.name] = sha(table_path.read_bytes())
    receipt = {
        "status": "provisional" if partial else "complete",
        "created_utc": datetime.now(UTC).isoformat(),
        "plotter_sha256": sha(Path(__file__).read_bytes()),
        "analysis_manifest_sha256": sha(manifest_bytes),
        "event_table_sha256": manifest["outputs"]["event-exposure-comparisons.csv"],
        "python": sys.version,
        "matplotlib": matplotlib.__version__,
        "numpy": np.__version__,
        "pandas": pd.__version__,
        **metadata,
        "outputs": outputs,
        "interpretation": "Current and previous automatic methods share identical physical MET masks and original reference episodes. Anomalies counted once per weather case, independently of six building profiles. Rates describe exposed episode occurrences across selected outages; exact-boundary/peak-value retention does not assert peak-hour timing. Zero exposure has no rate; unmatched method cases excluded explicitly. No national-population or future-extreme certainty is established.",
    }
    (folder / "manifest.json").write_text(
        json.dumps(receipt, indent=2, allow_nan=False) + "\n"
    )
    return receipt


def main():
    """Render final paired event evidence or explicitly marked provisional diagnostics."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("analysis_root", type=Path)
    parser.add_argument("--provisional", action="store_true")
    args = parser.parse_args()
    receipt = plot(args.analysis_root, provisional=args.provisional)
    print(
        json.dumps(
            {
                k: receipt[k]
                for k in (
                    "status",
                    "paired_weather_cases",
                    "paired_site_years",
                    "unmatched_weather_cases",
                )
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
