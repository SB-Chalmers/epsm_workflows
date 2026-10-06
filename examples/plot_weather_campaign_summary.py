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
from matplotlib.ticker import MaxNLocator, PercentFormatter

from examples import summarize_weather_campaign_uncertainty as U

HOURS = (3, 12, 48, 168)
SCOPES = (("meteorology", "Temperature, wind & other readings"), ("solar", "Sunshine"))
WEATHER_COLORS = {"meteorology": "#0072B2", "solar": "#CC8A00"}
GAP_LABELS = ("3 hours", "12 hours", "2 days", "1 week")
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


def exposed_peak_summary(rows):
    """Magnitude uncertainty where a positive reference peak intersects the outage.

    Pooled peaks outside an outage often have zero change. They cannot establish
    how well completion preserves an extreme when its weather data are missing.
    Keep each mode's exposure cohort separate and retain its exact denominator.
    """
    records = []
    keys = ["scope", "sampling", "gap_hours"]
    for mode in ("heating", "cooling"):
        exposed = rows.loc[
            rows[f"baseline_peak_{mode}_w_m2"].gt(1e-6)
            & rows[f"baseline_{mode}_peak_hidden"].eq(True)
        ]
        metric = f"absolute_peak_{mode}_error_w_m2"
        for labels, group in exposed.groupby(keys, sort=True):
            records.append(
                dict(zip(keys, labels))
                | {
                    "metric": metric,
                    "building_responses": len(group),
                    "exposed_positive_peaks": len(group),
                    "physical_masks": len(group[PHYSICAL].drop_duplicates()),
                    "site_years": len(group[["site_id", "year"]].drop_duplicates()),
                    "locations": group.site_id.nunique(),
                    "median": float(group[metric].median()),
                    "empirical_p95": float(group[metric].quantile(0.95)),
                    "maximum": float(group[metric].max()),
                }
            )
    return pd.DataFrame(
        records,
        columns=keys
        + [
            "metric",
            "building_responses",
            "exposed_positive_peaks",
            "physical_masks",
            "site_years",
            "locations",
            "median",
            "empirical_p95",
            "maximum",
        ],
    )


def _base_figure(title, subtitle, *, width=11.2):
    """One shared visual language: two readable panels and a plain-language key."""
    fig, axes = plt.subplots(1, 2, figsize=(width, 7.2), sharey=True)
    fig.suptitle(title, x=0.055, y=0.98, ha="left", fontsize=19, fontweight="bold")
    fig.text(0.055, 0.905, subtitle, fontsize=12, color="#45566a")
    for ax, mode in zip(axes, ("Heating", "Cooling")):
        ax.set_title(mode, loc="left", fontsize=16, fontweight="bold", pad=14)
        ax.set_yticks(range(4), GAP_LABELS)
        ax.set_ylim(3.55, -0.55)
        ax.tick_params(axis="both", labelsize=12, length=0, pad=9)
        ax.spines[["top", "right", "left"]].set_visible(False)
        ax.spines["bottom"].set_color("#b5bec7")
        ax.grid(axis="x", color="#e8edf1", linewidth=0.8)
        ax.set_axisbelow(True)
        for y in (0.5, 1.5, 2.5):
            ax.axhline(y, color="#eef1f4", linewidth=0.8)
    axes[0].set_ylabel("Length of each gap", fontsize=12, labelpad=12)
    fig.subplots_adjust(top=0.74, bottom=0.29, left=0.13, right=0.96, wspace=0.17)
    return fig, axes


def _weather_legend(fig, *, median_key=False):
    handles = [
        Line2D(
            [],
            [],
            color=WEATHER_COLORS[scope],
            marker="o",
            linewidth=2,
            markersize=7,
            label=label,
        )
        for scope, label in SCOPES
    ]
    fig.legend(
        handles=handles,
        loc="upper left",
        bbox_to_anchor=(0.045, 0.88),
        ncol=2,
        frameon=False,
        fontsize=12,
        columnspacing=2.5,
    )
    if median_key:
        fig.text(
            0.055,
            0.145,
            "Dot = middle case (median). Line ends at the higher-end error (95th percentile).",
            fontsize=11,
            color="#45566a",
        )


def error_figure(summary, quantity, *, relative=False):
    """Annual seasonal errors or exposed event-peak errors, never mixed budgets."""
    if quantity not in ("annual", "peak"):
        raise ValueError("Summary figure quantity must be annual or peak")
    annual = quantity == "annual"
    if relative:
        if not annual:
            raise ValueError("Relative-demand figures apply only to annual demand")
        summary = summary.copy()
        summary["metric"] = "absolute_annual_" + summary["mode"] + "_error_kwh_m2"
        summary["median"] = summary.median_absolute_error_pct
        summary["empirical_p95"] = summary.empirical_p95_absolute_error_pct
    sampling = "random_seasonal" if annual else "weather_extremes"
    unit, label = ("kwh_m2", "kWh/m²") if annual else ("w_m2", "W/m²")
    title = (
        "How much does filling gaps change annual demand?"
        if annual
        else "How much does filling gaps change the peak load?"
    )
    subtitle = (
        "Seasonal outages: four gaps per year, one in each season."
        if annual
        else "Outages around extremes: only peaks whose weather data were missing."
    )
    fig, axes = _base_figure(title, subtitle)
    selected = summary.loc[summary.sampling.eq(sampling)]
    relevant = selected.loc[selected.metric.str.startswith(f"absolute_{quantity}_")]
    maximum = float(relevant.empirical_p95.max()) if len(relevant) else 0.0
    # Both modes and weather groups use one scale; room for direct endpoint labels.
    limit = max(maximum * 1.28, 0.01)
    for ax, mode in zip(axes, ("heating", "cooling")):
        metric = f"absolute_{quantity}_{mode}_error_{unit}"
        for index, (scope, _) in enumerate(SCOPES):
            group = (
                selected.loc[selected.scope.eq(scope) & selected.metric.eq(metric)]
                .set_index("gap_hours")
                .reindex(HOURS)
            )
            color = WEATHER_COLORS[scope]
            for row, (_, record) in enumerate(group.iterrows()):
                y = row + (-0.13 if index == 0 else 0.13)
                if not np.isfinite(record.get("empirical_p95", np.nan)):
                    ax.text(
                        0.02 * limit, y, "No exposed peaks", va="center", fontsize=10
                    )
                    continue
                median, tail = float(record["median"]), float(record.empirical_p95)
                ax.plot(
                    [median, tail],
                    [y, y],
                    color=color,
                    linewidth=2.7,
                    solid_capstyle="round",
                )
                ax.plot(
                    tail, y, marker="|", markersize=11, color=color, markeredgewidth=2
                )
                ax.plot(median, y, marker="o", markersize=7, color=color)
                precision = (2 if tail < 1 else 1) if relative else (3 if annual else 2)
                endpoint = f"{tail:.{precision}f}" + ("%" if relative else "")
                ax.text(
                    tail + limit * 0.025,
                    y,
                    endpoint,
                    color=color,
                    va="center",
                    fontsize=11,
                    fontweight="bold",
                )
        ax.set_xlim(-limit * 0.015, limit)
        if relative:
            ax.xaxis.set_major_locator(
                MaxNLocator(nbins=4, steps=[1, 2, 5, 10], integer=True)
            )
            ax.xaxis.set_major_formatter(PercentFormatter(100, decimals=0))
            ax.set_xlabel(
                "Absolute change from reference (%)", fontsize=12, labelpad=12
            )
        else:
            ax.set_xlabel(f"Absolute change ({label})", fontsize=12, labelpad=12)
    _weather_legend(fig, median_key=True)
    fig.text(
        0.055,
        0.085,
        "About 95% of tested errors fall below the line end; individual cases can exceed it.",
        fontsize=11,
        color="#45566a",
    )
    if relative:
        fig.text(
            0.055,
            0.035,
            "Cooling below 1 kWh/m² is omitted to avoid inflated percentages.",
            fontsize=11,
            color="#45566a",
        )
    return fig


def timing_figure(summary):
    """Show shifts exceeding one day for exposed positive annual winning peaks."""
    fig, axes = _base_figure(
        "Can the annual peak move by more than a day?",
        "Annual peak hours more than 24 hours apart.",
    )
    selected = summary.loc[summary.sampling.eq("weather_extremes")]
    for ax, mode in zip(axes, ("heating", "cooling")):
        indexed = selected.loc[selected["mode"].eq(mode)].set_index(
            ["scope", "gap_hours"]
        )
        for index, (scope, _) in enumerate(SCOPES):
            color = WEATHER_COLORS[scope]
            for row, hours in enumerate(HOURS):
                y = row + (-0.13 if index == 0 else 0.13)
                if (scope, hours) not in indexed.index:
                    count = 0
                else:
                    record = indexed.loc[(scope, hours)]
                    count = int(record.exposed_positive_peaks)
                if count == 0:
                    ax.text(2, y, "No exposed peaks", fontsize=10, va="center")
                    continue
                changed = 100 * float(record.beyond24h_fraction)
                ax.plot(changed, y, "o", markersize=8, color=color)
                ax.text(
                    changed + 3,
                    y,
                    f"{changed:.0f}%",
                    color=color,
                    va="center",
                    fontsize=12,
                    fontweight="bold",
                )
        ax.set_xlim(-2, 118)
        ax.set_xticks((0, 25, 50, 75, 100), ("0%", "25%", "50%", "75%", "100%"))
        ax.set_xlabel(
            "Cases with a shift larger than 24 hours", fontsize=12, labelpad=12
        )
    _weather_legend(fig)
    fig.text(
        0.055,
        0.14,
        "Only peaks whose weather data were missing are counted; the nearest reference peak is used.",
        fontsize=11,
        color="#45566a",
    )
    fig.text(
        0.055,
        0.085,
        "24 hours is a descriptive day-scale comparison, not an accuracy acceptance threshold.",
        fontsize=11,
        color="#45566a",
    )
    return fig


def event_figure(summary, temperature):
    """Separate event-date preservation from quantitative extreme-temperature error.

    Temperature changes require one unambiguous overlapping candidate episode;
    lost and split events cannot be assigned a fabricated intensity difference.
    Their counts remain in the exported temperature summary and date denominator.
    """
    fig, axes = _base_figure(
        "Does filling gaps preserve the weather extremes?",
        "Temperature-event outages: one gap per weather file; only exposed episodes are counted.",
    )
    axes[0].set_title("Same dates", loc="left", fontsize=16, fontweight="bold", pad=14)
    axes[1].set_title(
        "Peak temperature change",
        loc="left",
        fontsize=16,
        fontweight="bold",
        pad=14,
    )
    palette = {"heatwave": "#C65102", "cold_spell": "#0072B2"}
    selected = temperature.loc[temperature.sampling.eq("weather_extremes")]
    tails = selected.empirical_p95_abs_peak_temperature_error_c
    largest = float(tails.max()) if np.isfinite(tails).any() else 0.0
    limit = max(largest * 1.3, 0.1)
    for index, (event, label) in enumerate(EVENTS[:2]):
        dates = summary.loc[summary.event_type.eq(event)].set_index("gap_hours")
        values = selected.loc[selected.event_type.eq(event)].set_index("gap_hours")
        color = palette[event]
        for row, hours in enumerate(HOURS):
            y = row + (-0.13 if index == 0 else 0.13)
            if hours in dates.index and int(dates.loc[hours].reference_exposed_events):
                record = dates.loc[hours]
                fraction = (
                    100
                    * int(record.exposed_events_with_exact_bounds)
                    / int(record.reference_exposed_events)
                )
                axes[0].plot(fraction, y, "o", color=color, markersize=7)
                axes[0].text(
                    fraction + 3,
                    y,
                    f"{fraction:.0f}%",
                    color=color,
                    fontsize=11,
                    fontweight="bold",
                    va="center",
                )
            else:
                axes[0].text(2, y, "No exposed episodes", fontsize=10, va="center")
            if hours not in values.index or not int(
                values.loc[hours].matched_exposed_events
            ):
                axes[1].text(
                    0.02 * limit, y, "No matched episodes", fontsize=10, va="center"
                )
                continue
            record = values.loc[hours]
            middle = float(record.median_abs_peak_temperature_error_c)
            tail = float(record.empirical_p95_abs_peak_temperature_error_c)
            axes[1].plot(
                [middle, tail],
                [y, y],
                color=color,
                linewidth=2.7,
                solid_capstyle="round",
            )
            axes[1].plot(tail, y, "|", markersize=11, color=color, markeredgewidth=2)
            axes[1].plot(middle, y, "o", markersize=7, color=color)
            axes[1].text(
                tail + limit * 0.025,
                y,
                f"{tail:.1f}°C",
                color=color,
                fontsize=11,
                fontweight="bold",
                va="center",
            )
    axes[0].set_xlim(-2, 118)
    axes[0].set_xticks((0, 25, 50, 75, 100), ("0%", "25%", "50%", "75%", "100%"))
    axes[0].set_xlabel(
        "Events with unchanged start and end dates", fontsize=11, labelpad=12
    )
    axes[1].set_xlim(-limit * 0.015, limit)
    axes[1].set_xlabel(
        "Absolute change in hottest / coldest temperature (°C)",
        fontsize=11,
        labelpad=12,
    )
    fig.legend(
        handles=[
            Line2D(
                [],
                [],
                color=palette[event],
                marker="o",
                markersize=7,
                linewidth=2,
                label=label,
            )
            for event, label in EVENTS[:2]
        ],
        loc="upper left",
        bbox_to_anchor=(0.045, 0.88),
        ncol=2,
        frameon=False,
        fontsize=12,
        columnspacing=3,
    )
    fig.text(
        0.055,
        0.145,
        "Temperature: dot = middle case; line end = 95th percentile, for matched episodes only.",
        fontsize=11,
        color="#45566a",
    )
    fig.text(
        0.055,
        0.085,
        "Lost or split episodes are excluded from temperature errors. Detailed counts accompany the figure.",
        fontsize=11,
        color="#45566a",
    )
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
    exposed = exposed_peak_summary(rows)
    events = event_summary(read_table(root, manifest, "event-exposure-comparisons.csv"))
    uncertainty = U.build_uncertainty_tables(root)
    output = root / "summary-figures"
    output.mkdir(exist_ok=True)
    outputs = {}
    for name, frame in (
        ("load-error-summary.csv", summary),
        ("building-error-summary.csv", summarize(rows, by_building=True)),
        ("peak-timing-summary.csv", timing),
        ("exposed-peak-error-summary.csv", exposed),
        ("weather-event-summary.csv", events),
        *uncertainty.items(),
    ):
        path = output / name
        frame.to_csv(path, index=False)
        outputs[name] = sha(path.read_bytes())
    for name, fig in (
        (
            "automatic-annual-errors",
            error_figure(
                uncertainty["relative-annual-error-summary.csv"],
                "annual",
                relative=True,
            ),
        ),
        ("automatic-annual-errors-absolute", error_figure(summary, "annual")),
        ("automatic-peak-errors", error_figure(exposed, "peak")),
        (
            "automatic-peak-timing",
            timing_figure(uncertainty["exposed-peak-timing-uncertainty.csv"]),
        ),
        (
            "automatic-weather-events",
            event_figure(events, uncertainty["event-temperature-uncertainty.csv"]),
        ),
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
        "uncertainty_summary_script_sha256": sha(Path(U.__file__).read_bytes()),
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
        "interpretation": "Annual figure: relative change from reference for seasonal random four-block outages only; positive heating and cooling at least1kWh/m². Absolute annual supplement retained. Peak magnitude/timing figures: positive exposed reference peaks in one-block event-centred outages only. Event figure: unchanged heatwave/cold-spell dates and absolute extreme-temperature errors in uniquely matched episodes only; lost/split episodes excluded from temperature summaries. Timing figure: nearest annual winning peak moves more than24h (descriptive scale, no acceptance criterion). Pooled empirical scenario medians/p95. No legacy or forced methods. Six synthetic responses share weather; no population confidence bounds. Separate random/event missingness budgets. Qualified building outcomes only; raw source-qualified weather events independent of engine qualification. Solar components are proxy reference data.",
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
