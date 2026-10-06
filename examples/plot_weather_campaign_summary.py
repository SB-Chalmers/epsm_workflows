"""Make paper summary figures for automatic completion against reference weather.

Usage: python -m examples.plot_weather_campaign_summary CAMPAIGN/analysis
The library's automatic completion is the only method summarized. Input bytes
must match a complete analysis receipt. Scenario percentiles describe this
selected experiment; they are neither confidence intervals nor population
uncertainty bounds. Scientific drivers and consumed evidence are never changed.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D

HOURS = (3, 12, 48, 168)
SCOPES = (("meteorology", "Meteorological gaps"), ("solar", "Solar gaps"))
SAMPLINGS = (
    ("random_seasonal", "Seasonal random", "#0072B2"),
    ("weather_extremes", "Temperature events", "#D55E00"),
)
PHYSICAL = ["site_id", "year", "scope", "sampling", "gap_hours", "physical_signature"]
METRICS = [
    f"absolute_{quantity}_{mode}_error_{unit}"
    for quantity, unit in (("annual", "kwh_m2"), ("peak", "w_m2"))
    for mode in ("heating", "cooling")
]
EVENTS = (
    ("heatwave", "Heatwaves"),
    ("cold_spell", "Cold spells"),
    ("high_wind", "High wind"),
)


def sha(data):
    """Fingerprint captured input and output bytes."""
    return hashlib.sha256(data).hexdigest()


def read_table(root, manifest, name):
    """Refuse analysis tables whose bytes differ from their receipt."""
    data = (root / name).read_bytes()
    if sha(data) != manifest["outputs"].get(name):
        raise ValueError(f"Analysis table differs from its receipt: {name}")
    return pd.read_csv(io.BytesIO(data))


def automatic_rows(rows):
    """Keep one qualified response per physical mask and building, without legacy cases."""
    selected = rows.loc[rows.method.eq("automatic")].copy()
    required = PHYSICAL + ["building_id", "engine_outcome_qualified"] + METRICS
    for mode in ("heating", "cooling"):
        required += [
            f"baseline_peak_{mode}_w_m2",
            f"baseline_{mode}_peak_hidden",
            f"same_{mode}_peak_hours",
        ]
    if selected.empty or set(required) - set(selected):
        raise ValueError("Automatic completion outcomes are missing or incomplete")
    if not selected.engine_outcome_qualified.eq(True).all():
        raise ValueError("Unqualified automatic building outcomes cannot be summarized")
    if (
        selected[PHYSICAL + ["building_id"]].isna().any().any()
        or selected.duplicated(PHYSICAL + ["building_id"]).any()
    ):
        raise ValueError("Automatic outcomes need unique, complete physical identities")
    if (
        not selected.scope.isin(dict(SCOPES)).all()
        or not selected.sampling.isin([s[0] for s in SAMPLINGS]).all()
        or not selected.gap_hours.isin(HOURS).all()
    ):
        raise ValueError("Automatic outcomes contain unknown experimental strata")
    for column in METRICS + [
        f"baseline_peak_{mode}_w_m2" for mode in ("heating", "cooling")
    ]:
        values = pd.to_numeric(selected[column], errors="coerce")
        if not np.isfinite(values).all() or values.lt(0).any():
            raise ValueError(
                "Absolute load errors and reference peaks must be finite and nonnegative"
            )
        selected[column] = values
    for mode in ("heating", "cooling"):
        for column in (f"baseline_{mode}_peak_hidden", f"same_{mode}_peak_hours"):
            if not selected[column].isin([True, False]).all():
                raise ValueError(
                    "Peak exposure and preservation must be explicit booleans"
                )
    return selected


def summarize(rows, *, by_building=False):
    """Report absolute errors and distinct climate/mask counts within each stratum."""
    keys = ["scope", "sampling", "gap_hours"] + (["building_id"] if by_building else [])
    records = []
    for labels, group in rows.groupby(keys, sort=True):
        counts = {
            "building_responses": len(group),
            "physical_masks": len(group[PHYSICAL].drop_duplicates()),
            "site_years": len(group[["site_id", "year"]].drop_duplicates()),
            "locations": group.site_id.nunique(),
        }
        for metric in METRICS:
            records.append(
                dict(zip(keys, labels))
                | counts
                | {
                    "metric": metric,
                    "median": float(group[metric].median()),
                    "empirical_p95": float(group[metric].quantile(0.95)),
                    "maximum": float(group[metric].max()),
                }
            )
    return pd.DataFrame(records)


def timing_summary(rows):
    """Evaluate timing only where a positive reference peak intersects the outage."""
    records = []
    for labels, group in rows.groupby(["scope", "sampling", "gap_hours"], sort=True):
        for mode in ("heating", "cooling"):
            positive = group[f"baseline_peak_{mode}_w_m2"].gt(1e-6)
            exposed = positive & group[f"baseline_{mode}_peak_hidden"].eq(True)
            preserved = exposed & group[f"same_{mode}_peak_hours"].eq(True)
            count, same = int(exposed.sum()), int(preserved.sum())
            records.append(
                dict(zip(("scope", "sampling", "gap_hours"), labels))
                | {
                    "mode": mode,
                    "exposed_positive_peaks": count,
                    "preserved_peak_hour_sets": same,
                    "preserved_fraction": same / count if count else np.nan,
                    "unexposed_positive_peaks": int((positive & ~exposed).sum()),
                    "zero_or_numerical_peaks": int((~positive).sum()),
                    "exposed_site_years": len(
                        group.loc[exposed, ["site_id", "year"]].drop_duplicates()
                    ),
                }
            )
    return pd.DataFrame(records)


def event_summary(rows):
    """Count exposed weather episodes once per physical mask, independent of buildings."""
    selected = rows.loc[
        rows.method.eq("automatic")
        & rows.scope.eq("meteorology")
        & rows.sampling.eq("weather_extremes")
        & rows.event_type.isin(dict(EVENTS))
    ].copy()
    if selected.empty or selected.duplicated(PHYSICAL + ["event_type"]).any():
        raise ValueError("Weather events require unique automatic physical masks")
    columns = [
        "reference_exposed_events",
        "exposed_events_with_any_overlap",
        "exposed_events_with_exact_bounds",
        "exposed_events_with_exact_bounds_and_peak",
    ]
    for column in columns:
        values = pd.to_numeric(selected[column], errors="coerce")
        if (
            not np.isfinite(values).all()
            or values.lt(0).any()
            or values.mod(1).ne(0).any()
        ):
            raise ValueError("Event counts must be finite nonnegative integers")
        selected[column] = values
    if any(selected[columns[i + 1]].gt(selected[columns[i]]).any() for i in range(3)):
        raise ValueError(
            "Stricter event matches cannot exceed exposed or overlapping events"
        )
    records = []
    for (event, hours), group in selected.groupby(
        ["event_type", "gap_hours"], sort=True
    ):
        records.append(
            {
                "event_type": event,
                "gap_hours": int(hours),
                "physical_masks": len(group),
                "site_years": len(group[["site_id", "year"]].drop_duplicates()),
                **{column: int(group[column].sum()) for column in columns},
            }
        )
    return pd.DataFrame(records)


def error_figure(summary, quantity):
    """Show typical and upper-tail absolute errors without comparing algorithms."""
    fig, axes = plt.subplots(2, 2, figsize=(11, 7.5), sharey="row")
    unit, label = ("kwh_m2", "kWh/m²") if quantity == "annual" else ("w_m2", "W/m²")
    for row, mode in enumerate(("heating", "cooling")):
        metric = f"absolute_{quantity}_{mode}_error_{unit}"
        for column, (scope, title) in enumerate(SCOPES):
            ax = axes[row, column]
            selected = summary.loc[summary.scope.eq(scope) & summary.metric.eq(metric)]
            counts = {}
            for sampling, _, color in SAMPLINGS:
                group = (
                    selected.loc[selected.sampling.eq(sampling)]
                    .set_index("gap_hours")
                    .reindex(HOURS)
                )
                counts[sampling] = group.building_responses
                ax.plot(
                    range(4),
                    group["median"],
                    "o-",
                    color=color,
                    linewidth=1.7,
                    markersize=4,
                )
                ax.plot(
                    range(4),
                    group.empirical_p95,
                    "--",
                    color=color,
                    linewidth=1.4,
                    alpha=0.8,
                )
            ticks = [
                f"{h}h\nR{int(counts['random_seasonal'].get(h, 0))}/E{int(counts['weather_extremes'].get(h, 0))}"
                for h in HOURS
            ]
            ax.set_xticks(range(4), ticks)
            ax.set_xlabel("Gap length per block", fontsize=9)
            if column == 0:
                ax.set_ylabel(f"Absolute {mode} error ({label})", fontsize=10)
            ax.set_title(title, fontsize=11)
            ax.spines[["top", "right"]].set_visible(False)
            ax.grid(axis="y", color="#dddddd", linewidth=0.6)
            ax.tick_params(labelsize=9)
        # Shared axes must encompass both weather groups. Fixing the first
        # panel's limits before plotting the second can silently clip its tail.
        maximum = summary.loc[summary.metric.eq(metric), "empirical_p95"].max()
        for ax in axes[row]:
            ax.set_ylim(0, max(float(maximum) * 1.1, 1e-8))
    handles = [
        Line2D([], [], color=color, label=title) for _, title, color in SAMPLINGS
    ]
    handles += [
        Line2D([], [], color="#444444", linestyle=style, label=title)
        for style, title in (("-", "Median"), ("--", "95th percentile"))
    ]
    fig.legend(
        handles=handles,
        loc="upper center",
        bbox_to_anchor=(0.5, 0.94),
        ncol=4,
        frameon=False,
    )
    fig.suptitle(
        f"{'Annual demand' if quantity == 'annual' else 'Hourly peak'} errors after automatic weather completion",
        y=0.985,
        fontsize=15,
        fontweight="bold",
    )
    fig.text(
        0.5,
        0.025,
        "R/E: building responses for random/event outages; 6 synthetic profiles share each weather record.\nRandom: 4 blocks/file; events: 1 block/file. Curves describe separate outage designs.\nAccepted transient warnings retained after hourly-load checks. Percentiles describe tested scenarios, not population uncertainty.",
        ha="center",
        va="bottom",
        fontsize=9,
    )
    fig.subplots_adjust(
        top=0.85, bottom=0.20, left=0.09, right=0.98, hspace=0.55, wspace=0.15
    )
    return fig


def timing_figure(summary):
    """Keep no exposed peaks visibly distinct from zero preservation."""
    fig, axes = plt.subplots(1, 2, figsize=(12, 5.5))
    groups = [(scope, sampling) for scope, _ in SCOPES for sampling, _, _ in SAMPLINGS]
    labels = [
        f"{'Meteorology' if scope == 'meteorology' else 'Solar'} · {'random' if sampling == 'random_seasonal' else 'events'}"
        for scope, sampling in groups
    ]
    for ax, mode in zip(axes, ("heating", "cooling")):
        values = np.full((4, 4), np.nan)
        texts = [["—" for _ in HOURS] for _ in groups]
        indexed = summary.loc[summary["mode"].eq(mode)].set_index(
            ["scope", "sampling", "gap_hours"]
        )
        for row, (scope, sampling) in enumerate(groups):
            for column, hours in enumerate(HOURS):
                record = indexed.loc[(scope, sampling, hours)]
                count = int(record.exposed_positive_peaks)
                if count:
                    same = int(record.preserved_peak_hour_sets)
                    values[row, column] = 100 * record.preserved_fraction
                    texts[row][column] = f"{values[row, column]:.0f}%\n{same}/{count}"
        cmap = plt.get_cmap("YlGnBu").copy()
        cmap.set_bad("#eeeeee")
        im = ax.imshow(values, vmin=0, vmax=100, cmap=cmap, aspect="auto")
        for row in range(4):
            for column in range(4):
                ax.text(
                    column,
                    row,
                    texts[row][column],
                    ha="center",
                    va="center",
                    color="white" if values[row, column] > 65 else "#222222",
                    fontsize=10,
                )
        ax.set_xticks(range(4), [f"{hours}h" for hours in HOURS])
        ax.set_yticks(range(4), labels)
        ax.set_title(mode.capitalize(), fontsize=12)
        ax.set_xlabel("Gap length per block")
    fig.suptitle(
        "Peak timing when the missing data cover the reference peak",
        y=0.98,
        fontsize=15,
        fontweight="bold",
    )
    fig.text(
        0.5,
        0.035,
        "Cells: retained / exposed positive peaks; — means no exposed positive peaks.\nUnexposed and zero-load peaks are excluded. Peaks are hourly mean sensible loads; all tied peak hours must match.\nRandom outages remove 4 blocks/file; events remove 1. These counts describe synthetic responses, not independent climates.",
        ha="center",
        fontsize=9,
    )
    # Position axes directly so the colorbar remains outside the second panel.
    axes[0].set_position([0.16, 0.27, 0.30, 0.58])
    axes[1].set_position([0.62, 0.27, 0.30, 0.58])
    colorbar = fig.add_axes([0.935, 0.27, 0.012, 0.58])
    fig.colorbar(im, cax=colorbar, label="Exact peak-hour sets retained (%)")
    return fig


def event_figure(summary):
    """Separate episode retention, unchanged duration and unchanged peak intensity."""
    fig, axes = plt.subplots(1, 3, figsize=(12, 5.5), sharey=True)
    columns = (
        "exposed_events_with_any_overlap",
        "exposed_events_with_exact_bounds",
        "exposed_events_with_exact_bounds_and_peak",
    )
    colors = ("#0072B2", "#009E73", "#E69F00")
    for ax, (event, title) in zip(axes, EVENTS):
        group = (
            summary.loc[summary.event_type.eq(event)]
            .set_index("gap_hours")
            .reindex(HOURS)
        )
        exposed = group.reference_exposed_events
        for index, (column, color) in enumerate(zip(columns, colors)):
            fraction = group[column].div(exposed.where(exposed.gt(0))).mul(100)
            ax.bar(np.arange(4) + (index - 1) * 0.24, fraction, width=0.23, color=color)
        ax.set_xticks(range(4), [f"{h}h\nE{int(exposed.get(h, 0))}" for h in HOURS])
        ax.set_title(title, fontsize=12)
        ax.set_xlabel("Gap length per block")
        ax.set_ylim(0, 105)
        ax.grid(axis="y", color="#dddddd", linewidth=0.6)
        ax.set_axisbelow(True)
        ax.spines[["top", "right"]].set_visible(False)
    axes[0].set_ylabel("Exposed reference episodes retained (%)")
    handles = [
        Line2D([], [], color=color, linewidth=7, label=title)
        for color, title in zip(
            colors,
            ("Any overlap", "Same start and end", "Same start, end and intensity"),
        )
    ]
    fig.legend(
        handles=handles,
        loc="upper center",
        bbox_to_anchor=(0.5, 0.92),
        ncol=3,
        frameon=False,
    )
    fig.suptitle(
        "Weather-event preservation after automatic completion",
        y=0.985,
        fontsize=15,
        fontweight="bold",
    )
    fig.text(
        0.5,
        0.035,
        "E: exposed reference episodes, counted once per weather mask; episodes can recur across outage scenarios.\nTemperature-event outages: 1 block/file. They do not target wind peaks. Weather evidence includes 17 source-years.\nSame start/end preserves episode duration; intensity is peak temperature or wind speed. Event peak-hour timing was not evaluated.",
        ha="center",
        fontsize=9,
    )
    fig.subplots_adjust(top=0.80, bottom=0.25, left=0.07, right=0.98, wspace=0.15)
    return fig


def plot(root):
    """Publish current-only figures and hash-bound, reusable summary tables."""
    root = Path(root).resolve()
    raw = (root / "manifest.json").read_bytes()
    manifest = json.loads(raw)
    if manifest["status"] != "complete" or manifest.get("incomplete"):
        raise ValueError("Paper figures require a complete campaign analysis")
    rows = automatic_rows(read_table(root, manifest, "deduplicated-comparisons.csv"))
    summary, timing = summarize(rows), timing_summary(rows)
    events = event_summary(read_table(root, manifest, "event-exposure-comparisons.csv"))
    output = root / "summary-figures"
    output.mkdir(exist_ok=True)
    outputs = {}
    for name, frame in (
        ("load-error-summary.csv", summary),
        ("building-error-summary.csv", summarize(rows, by_building=True)),
        ("peak-timing-summary.csv", timing),
        ("weather-event-summary.csv", events),
    ):
        path = output / name
        frame.to_csv(path, index=False)
        outputs[name] = sha(path.read_bytes())
    for name, fig in (
        ("automatic-annual-errors", error_figure(summary, "annual")),
        ("automatic-peak-errors", error_figure(summary, "peak")),
        ("automatic-peak-timing", timing_figure(timing)),
        ("automatic-weather-events", event_figure(events)),
    ):
        for extension in ("png", "svg", "pdf"):
            path = output / f"{name}.{extension}"
            fig.savefig(path, dpi=220, bbox_inches="tight")
            outputs[path.name] = sha(path.read_bytes())
        plt.close(fig)
    receipt = {
        "status": "complete",
        "created_utc": datetime.now(UTC).isoformat(),
        "analysis_manifest_sha256": sha(raw),
        "plotter_sha256": sha(Path(__file__).read_bytes()),
        "method": "automatic",
        "building_responses": len(rows),
        "physical_weather_masks": len(rows[PHYSICAL].drop_duplicates()),
        "site_years": len(rows[["site_id", "year"]].drop_duplicates()),
        "locations": rows.site_id.nunique(),
        "inputs": {
            name: manifest["outputs"][name]
            for name in (
                "deduplicated-comparisons.csv",
                "event-exposure-comparisons.csv",
            )
        },
        "interpretation": "Pooled empirical scenario medians/p95. No legacy or forced methods. Six synthetic responses share weather; no population confidence bounds. Separate random/event missingness budgets. Qualified building outcomes only; raw source-qualified weather events independent of engine qualification. Solar components are proxy reference data.",
        "outputs": outputs,
    }
    (output / "manifest.json").write_text(
        json.dumps(receipt, indent=2, allow_nan=False) + "\n"
    )
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("analysis", type=Path)
    args = parser.parse_args()
    receipt = plot(args.analysis)
    print(
        json.dumps(
            {
                key: receipt[key]
                for key in (
                    "status",
                    "building_responses",
                    "physical_weather_masks",
                    "site_years",
                    "locations",
                )
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
