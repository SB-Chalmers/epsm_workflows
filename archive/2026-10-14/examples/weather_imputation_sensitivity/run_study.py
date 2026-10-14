"""Run a reproducible AMY completion experiment with an EPSM Core building.

Pinned hourly inputs allow subsequent runs without any weather API calls.
Each alternative shares its outage mask and all unmasked EPW weather with the
reference. Demand differences include the building's thermal carry-over.
"""

from __future__ import annotations

import argparse
import importlib
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd

if __package__:
    from . import weather as W
else:
    import weather as W


def load_comparison(
    reference,
    candidate,
    baseline,
    summary,
    mask,
    *,
    year=W.YEAR,
    offset=W.OFFSET,
    recovery_hours=24,
):
    """Compare annual loads, tied peaks, reference-peak load and outage energy."""
    result = {}
    changed = W.export.shift_to_lst(pd.DataFrame({"mask": mask}), year, offset)[
        "mask"
    ].to_numpy()
    # Unmodified weather does not reset the building's thermal mass.
    affected = changed.copy()
    for _, end in W.P._gap_runs(changed):
        affected[end : min(end + recovery_hours, len(affected))] = True
    for mode in ("heating", "cooling"):
        energy_key, peak_key = f"annual_{mode}_kwh", f"peak_{mode}_kw"
        energy, peak = summary[energy_key], summary[peak_key]
        result[energy_key] = energy
        result[f"delta_{energy_key}"] = energy - baseline[energy_key]
        result[f"delta_annual_{mode}_pct"] = (
            (energy / baseline[energy_key] - 1) * 100
            if baseline[energy_key] > 0
            else None
        )
        result[peak_key] = peak
        result[f"delta_peak_{mode}_pct"] = (
            (peak / baseline[peak_key] - 1) * 100 if baseline[peak_key] > 0 else None
        )
        times = summary[f"peak_{mode}_hours_lst"] if peak > 0 else []
        baseline_times = (
            baseline[f"peak_{mode}_hours_lst"] if baseline[peak_key] > 0 else []
        )
        result[f"peak_{mode}_hours_lst"] = times
        result[f"same_{mode}_peak_hours"] = (
            set(times) == set(baseline_times) if baseline_times else None
        )
        shifts = [
            (pd.Timestamp(t) - pd.Timestamp(b)).total_seconds() / 3600
            for t in times
            for b in baseline_times
        ]
        result[f"nearest_{mode}_peak_shift_h"] = (
            min(shifts, key=lambda x: (abs(x), x)) if shifts else None
        )
        at_baseline = reference.lst_hour_ending.isin(baseline_times)
        result[f"baseline_{mode}_peak_hidden"] = (
            bool(changed[at_baseline].any()) if baseline_times else None
        )
        result[f"load_at_baseline_{mode}_peak_kw"] = (
            float(candidate.loc[at_baseline, f"{mode}_kw"].max())
            if at_baseline.any()
            else None
        )
        result[f"delta_affected_window_{mode}_kwh"] = float(
            (
                candidate.loc[affected, f"{mode}_kw"]
                - reference.loc[affected, f"{mode}_kw"]
            ).sum()
        )
        difference = candidate[f"{mode}_kw"] - reference[f"{mode}_kw"]
        result[f"hourly_{mode}_load_mae_kw"] = float(difference.abs().mean())
        result[f"maximum_absolute_{mode}_load_change_kw"] = float(
            difference.abs().max()
        )
    return result


def simulation_weather_hash(path):
    """Ignore comments for reuse, while preserving every operative EPW header."""
    lines = Path(path).read_text().splitlines()
    operative = [lines[i] for i in (0, 1, 2, 3, 4, 7)] + lines[8:]
    return W.hashlib.sha256("\n".join(operative).encode()).hexdigest()


def create_cases(root, raw, reference, donor, model, receipt, anchors):
    """Create complete EPWs, recording unsupported or rejected alternatives."""
    baseline_path, baseline_weather = W.render(
        root,
        "reference",
        reference,
        receipt["station"]["elevation"],
        {"scope": "reference", "method": "complete_reference"},
    )
    cases = [
        {
            "case_id": "reference",
            "method": "reference",
            "scope": "reference",
            "gap_hours": 0,
            "hidden_hours": 0,
            "path": str(baseline_path),
            "status": "ready",
        }
    ]
    masks = {}
    failures = []
    for hours in (3, 48, 168):
        mask, blocks = W.outage_mask(reference.index, raw, anchors, hours)
        masks[hours] = mask
        W.dump(
            Path(root) / f"mask-{hours}h.json",
            {
                "blocks": blocks,
                "columns": W.MET,
                "timestamps_utc": reference.index[mask].astype(str).tolist(),
            },
        )
        methods = ["temporal", "donor_raw", "donor_assessed", "era5", "automatic"]
        for method in methods:
            if method == "temporal" and hours > W.C.MAX_GAP_HOURS:
                failures.append(
                    {
                        "case_id": f"met-{hours}h-temporal",
                        "status": "unsupported",
                        "reason": "Production temporal ceiling is 48 hours; no unlimited interpolation comparison",
                    }
                )
                continue
            case = {
                "case_id": f"met-{hours}h-{method}",
                "scope": "meteorology",
                "gap_hours": hours,
                "hidden_hours": int(mask.sum()),
                "method": method,
                "forced_alternative": method in ("donor_raw", "donor_assessed", "era5"),
                "raw_donor_is_unassessed_diagnostic": method == "donor_raw",
                "blocks": blocks,
            }
            try:
                filled, decisions = W.reconstruct(
                    reference, raw, donor, model, mask, method
                )
                path, completed = W.render(
                    root,
                    case["case_id"],
                    filled,
                    receipt["station"]["elevation"],
                    case | {"decisions": decisions},
                )
                errors = W.weather_errors(
                    baseline_weather, completed, mask, "meteorology"
                )
                case.update(
                    path=str(path),
                    status="ready",
                    weather_errors=errors,
                    decisions=decisions,
                )
                cases.append(case)
            except (ValueError, W.P.DataGapError) as exc:
                failures.append(case | {"status": "unavailable", "reason": str(exc)})
    # Separate solar failures: preserve meteorology. Donors cannot supply site
    # solar radiation; compare bounded profiles, ERA5 and GHI-only Erbs paths.
    for hours, methods in ((48, ("temporal", "era5", "erbs")), (168, ("era5", "erbs"))):
        mask = masks[hours]
        for method in methods:
            case = {
                "case_id": f"solar-{hours}h-{method}",
                "scope": "solar",
                "gap_hours": hours,
                "hidden_hours": int(mask.sum()),
                "method": method,
                "ghi_retained": method == "erbs",
            }
            try:
                filled, decisions = W.reconstruct(
                    reference, raw, donor, model, mask, method, "solar"
                )
                path, completed = W.render(
                    root, case["case_id"], filled, receipt["station"]["elevation"], case
                )
                errors = W.weather_errors(baseline_weather, completed, mask, "solar")
                cases.append(
                    case
                    | {"path": str(path), "status": "ready", "weather_errors": errors}
                )
            except (ValueError, W.P.DataGapError) as exc:
                failures.append(case | {"status": "unavailable", "reason": str(exc)})
    return cases, failures, masks, baseline_weather


def write_report(root, table, audit, events):
    """Save a concise human-readable catalogue alongside full machine data."""
    columns = [
        "case_id",
        "annual_heating_kwh",
        "delta_annual_heating_pct",
        "annual_cooling_kwh",
        "delta_annual_cooling_pct",
        "peak_heating_kw",
        "same_heating_peak_hours",
        "nearest_heating_peak_shift_h",
        "peak_cooling_kw",
        "same_cooling_peak_hours",
        "nearest_cooling_peak_shift_h",
    ]
    nice = table[columns].copy()
    for column in nice.select_dtypes(include="number"):
        nice[column] = nice[column].round(3)
    # HTML needs no optional tabulate package and preserves full precision CSV.
    body = nice.to_html(index=False, border=0, escape=True)
    html = (
        """<!doctype html><html lang="en"><meta charset="utf-8"><title>AMY completion sensitivity</title>
<style>body{font:15px system-ui;max-width:1500px;margin:40px auto;padding:0 20px;color:#17252c}table{border-collapse:collapse;font-size:13px}td,th{padding:8px;border-bottom:1px solid #cbd5db;text-align:right}th{background:#edf3f5}td:first-child,th:first-child{text-align:left}img{max-width:100%}p{max-width:1000px}</style>
<h1>Gothenburg 2023: weather-completion sensitivity</h1>
<p>One complete reference AMY, one building created by EPSM Core, and identical controlled seasonal outages.
Annual values are delivered sensible heating/cooling kWh. Peaks are hourly-average kW; shifts are hours in fixed UTC+1 LST.</p>
<p>Each gap size includes the reference heating and cooling peak hours plus spring/autumn windows.
Unmasked weather is identical. Donor fitting excludes all synthetically hidden and naturally missing observations.
Raw donor is an unassessed diagnostic; short-gap donor/ERA5 are forced comparisons. Automatic uses the bounded production hierarchy with the fixed donor.</p>
<p>Solar cases remove the entire solar group, except Erbs cases retain GHI and remove only direct components.
Meteorology is unchanged in solar cases. ERA5 cloud cover is excluded everywhere to prevent unrelated annual changes.</p>
"""
        + body
    )
    html += "<h2>Annual demand and peaks</h2><img src='demand-comparison.png' alt='Annual demand differences and peak differences'>"
    html += "<h2>Weather events</h2><p>Heatwave: at least three consecutive days with daily maximum ≥25°C. Cold spell: at least three days with daily minimum ≤−10°C. Relative spell thresholds are pinned to the reference seasonal 95th/5th percentiles. These are sensitivity diagnostics, not official SMHI classifications. Durations describe complete event days.</p>"
    html += events.to_html(index=False, border=0, escape=True)
    html += "<img src='event-temperatures.png' alt='Daily maxima during the heatwave and daily minima during the cold spell'>"
    html += "<p>Detailed lost/new events, overlap, timing and threshold values are in anomalies.json. Solar mismatch events are reconstruction differences, not proof of unphysical weather. Physical checks cover fully dark intervals and the broad 2,000 W/m² limit.</p>"
    html += "<h2>Weather and load traces around the reference peaks</h2><img src='peak-traces.png' alt='Weather and loads around heating and cooling peaks'>"
    html += "<p>Full data: summary.csv, catalogue.json, anomalies.json, audit.json, masks, pinned sources, individual EPWs, hourly loads and EnergyPlus receipts. One year/building and selected outages do not establish general imputation accuracy or calibrated energy predictions.</p></html>"
    (Path(root) / "report.html").write_text(html, encoding="utf-8")


def plots(root, table, hourly, weather_frames, anchors):
    """Generate exportable scientific figures using standard plotting tools."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {"font.size": 9, "axes.spines.top": False, "axes.spines.right": False}
    )
    selected = table[
        table.case_id.str.startswith("met-48h")
        | table.case_id.str.startswith("met-168h")
    ].set_index("case_id")
    fig, axes = plt.subplots(1, 2, figsize=(13, 5), constrained_layout=True)
    for axis, quantity, label in (
        (axes[0], "annual", "Annual thermal demand change (%)"),
        (axes[1], "peak", "Hourly peak change (%)"),
    ):
        selected[
            [f"delta_{quantity}_heating_pct", f"delta_{quantity}_cooling_pct"]
        ].rename(
            columns={
                f"delta_{quantity}_heating_pct": "Heating",
                f"delta_{quantity}_cooling_pct": "Cooling",
            }
        ).plot.barh(ax=axis, color=["#2876a5", "#d87836"])
        axis.set_xlabel(label)
        axis.set_ylabel("")
        axis.axvline(0, color="#666", lw=0.6)
        axis.grid(axis="x", alpha=0.2)
    fig.savefig(Path(root) / "demand-comparison.png", dpi=170)
    plt.close(fig)
    fig, axes = plt.subplots(2, 2, figsize=(13, 7), constrained_layout=True)
    colours = {
        "reference": "#141d24",
        "met-168h-temporal": "#888",
        "met-168h-donor_raw": "#df832e",
        "met-168h-donor_assessed": "#268561",
        "met-168h-era5": "#3d71ae",
    }
    for col, (event, mode) in enumerate(
        (("heating_peak", "heating"), ("cooling_peak", "cooling"))
    ):
        anchor = pd.Timestamp(anchors[event])
        for case_id, colour in colours.items():
            if case_id not in hourly:
                continue
            times = pd.to_datetime(hourly[case_id].lst_hour_ending, utc=True)
            show = (times >= anchor - pd.Timedelta(days=5)) & (
                times <= anchor + pd.Timedelta(days=5)
            )
            relative = (times[show] - anchor).dt.total_seconds() / 3600
            axes[0, col].plot(
                relative,
                weather_frames[case_id].loc[show.to_numpy(), "dry_bulb"],
                color=colour,
                label=case_id,
            )
            axes[1, col].plot(
                relative, hourly[case_id].loc[show, f"{mode}_kw"], color=colour
            )
        axes[0, col].set_title(
            f"Reference {mode} peak: {anchor.tz_convert('Etc/GMT-1').strftime('%Y-%m-%d %H:%M')} LST"
        )
        axes[0, col].set_ylabel("Dry bulb (°C)")
        axes[1, col].set_ylabel(f"{mode.title()} (kW)")
        axes[1, col].set_xlabel("Hours from reference peak")
        for axis in axes[:, col]:
            axis.axvline(0, color="#aaa", lw=0.6)
            axis.grid(alpha=0.2)
    axes[0, 0].legend(fontsize=7)
    fig.savefig(Path(root) / "peak-traces.png", dpi=170)
    plt.close(fig)
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.5), constrained_layout=True)
    import matplotlib.dates as mdates

    event_colours = {
        "reference": "#141d24",
        "met-48h-temporal": "#a7456c",
        "met-168h-donor_raw": "#df832e",
        "met-168h-donor_assessed": "#268561",
        "met-168h-era5": "#3d71ae",
    }
    for axis, variable, start, end, threshold, title in (
        (axes[0], "max", "2023-06-09", "2023-06-22", 25.0, "Heatwave: daily maximum"),
        (
            axes[1],
            "min",
            "2023-11-23",
            "2023-12-06",
            -10.0,
            "Cold spell: daily minimum",
        ),
    ):
        for case_id, colour in event_colours.items():
            frame = weather_frames[case_id]
            labels = pd.to_datetime(frame[["year", "month", "day"]])
            daily = (
                pd.Series(frame.dry_bulb.to_numpy(), index=pd.DatetimeIndex(labels))
                .groupby(level=0)
                .agg(variable)
            )
            segment = daily.loc[start:end]
            axis.plot(segment.index, segment, "o-", color=colour, ms=3, label=case_id)
        axis.axhline(
            threshold, color="#555", linestyle="--", label=f"{threshold:g}°C criterion"
        )
        axis.set_title(title)
        axis.set_ylabel("Temperature (°C)")
        axis.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        axis.grid(alpha=0.2)
    axes[0].legend(fontsize=7, loc="lower left")
    fig.savefig(Path(root) / "event-temperatures.png", dpi=170)
    plt.close(fig)


def main():
    """Prepare sources, build controlled cases and simulate each unique EPW."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output/weather-imputation-sensitivity-2026-10-05-v1"),
    )
    parser.add_argument(
        "--seed-cache",
        type=Path,
        default=Path(
            "output/archetype-pilot-mf-se02-f-2026-09-30/weather/sources/2023/raw-cache"
        ),
    )
    parser.add_argument(
        "--donor-cache", type=Path, default=Path.home() / "GitHub/smhi2epw/.smhi_cache"
    )
    parser.add_argument(
        "--energyplus",
        type=Path,
        default=Path("/Applications/OpenStudio-3.9.0/EnergyPlus/energyplus-24.2.0"),
    )
    parser.add_argument(
        "--pinned-sources",
        type=Path,
        help="reuse a previous study's sources directory for an offline fresh run",
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.pinned_sources and not (args.output / "sources/receipt.json").exists():
        shutil.copytree(args.pinned_sources, args.output / "sources")
    if __package__:
        from . import anomalies, building, metrics
    else:
        import anomalies
        import building
        import metrics

    raw, reference, donor, model, receipt = W.prepare_sources(
        args.output, args.seed_cache, args.donor_cache
    )
    baseline_path, baseline_weather = W.render(
        args.output,
        "reference",
        reference,
        receipt["station"]["elevation"],
        {"method": "complete_reference"},
    )
    baseline_sim = building.simulate(
        baseline_path,
        args.output / "cases/reference/simulation",
        year=W.YEAR,
        executable=args.energyplus,
    )
    baseline_hourly, baseline_metrics = metrics.extract(
        args.output / "cases/reference/simulation/eplusout.sql",
        year=W.YEAR,
        utc_offset=W.OFFSET,
    )
    anchors = {
        "heating_peak": pd.Timestamp(
            baseline_metrics["peak_heating_hours_lst"][0]
        ).tz_convert("UTC"),
        "cooling_peak": pd.Timestamp(
            baseline_metrics["peak_cooling_hours_lst"][0]
        ).tz_convert("UTC"),
        "spring": pd.Timestamp("2023-04-15 12:00", tz="UTC"),
        "autumn": pd.Timestamp("2023-10-15 12:00", tz="UTC"),
    }
    cases, failures, masks, baseline_weather = create_cases(
        args.output, raw, reference, donor, model, receipt, anchors
    )
    print(
        f"Prepared {len(cases)} cases; {len(failures)} explicitly unavailable/unsupported",
        flush=True,
    )
    hourly = {"reference": baseline_hourly}
    frames = {"reference": baseline_weather}
    event_comparisons = {}
    rows = []
    reference_data_hash = simulation_weather_hash(baseline_path)
    simulated = {reference_data_hash: (baseline_hourly, baseline_metrics, "reference")}
    baseline_events = anomalies.detect_events(baseline_weather)
    zero_mask = pd.Series(False, index=reference.index)
    for case in cases:
        case_id = case["case_id"]
        epw = Path(case["path"])
        # Ignore comment-only identity differences for simulation deduplication.
        data_hash = simulation_weather_hash(epw)
        if case_id == "reference":
            load, summary = baseline_hourly, baseline_metrics
        elif data_hash in simulated:
            load, summary, reused = simulated[data_hash]
            case["simulation_reused_from"] = reused
        else:
            building.simulate(
                epw,
                args.output / f"cases/{case_id}/simulation",
                year=W.YEAR,
                executable=args.energyplus,
            )
            load, summary = metrics.extract(
                args.output / f"cases/{case_id}/simulation/eplusout.sql",
                year=W.YEAR,
                utc_offset=W.OFFSET,
            )
        simulated[data_hash] = (load, summary, case_id)
        hourly[case_id] = load
        frames[case_id] = W.read_epw(epw)
        load.to_csv(args.output / f"cases/{case_id}/hourly-loads.csv", index=False)
        W.dump(args.output / f"cases/{case_id}/load-summary.json", summary)
        mask = zero_mask if case_id == "reference" else masks[case["gap_hours"]]
        if case_id != "reference":
            exported_mask = W.export.shift_to_lst(
                pd.DataFrame({"mask": mask}), W.YEAR, W.OFFSET
            )["mask"].to_numpy()
            frames[case_id].attrs["source_tags"] = pd.Series(
                np.where(exported_mask, case["method"], "primary")
            )
        compared = load_comparison(
            baseline_hourly, load, baseline_metrics, summary, mask
        )
        rows.append(
            {
                k: case[k]
                for k in ("case_id", "scope", "method", "gap_hours", "hidden_hours")
            }
            | compared
        )
        event_comparisons[case_id] = anomalies.compare_events(
            baseline_weather, frames[case_id]
        )
        print(
            f"{case_id}: heat {summary['annual_heating_kwh']:.1f}, cool {summary['annual_cooling_kwh']:.1f} kWh",
            flush=True,
        )
    table = pd.DataFrame(rows)
    table.to_csv(args.output / "summary.csv", index=False)
    W.dump(
        args.output / "catalogue.json",
        {"cases": cases, "unavailable": failures, "load_metrics": rows},
    )
    W.dump(
        args.output / "anomalies.json",
        {"reference": baseline_events, "comparisons": event_comparisons},
    )
    event_rows = []
    for case_id, comparison in event_comparisons.items():
        row = {"case_id": case_id}
        for kind in ("heatwave", "cold_spell", "warm_spell", "relative_cold_spell"):
            values = comparison["by_type"][kind]
            row[f"{kind}_days"] = values["candidate_hours"] / 24
            row[f"{kind}_lost"] = values["lost_reference_count"]
        for kind in ("temperature_jump", "nighttime_solar", "solar_out_of_bounds"):
            row[f"{kind}_hours"] = comparison["by_type"][kind]["candidate_hours"]
        event_rows.append(row)
    event_table = pd.DataFrame(event_rows)
    event_table.to_csv(args.output / "event-preservation.csv", index=False)
    audit = {
        "reference_sources": receipt,
        "building": building.SPEC,
        "reference_simulation": baseline_sim,
        "anchors_utc": anchors,
        "engine_sha256": W.sha(args.energyplus),
        "methods": "production bounded functions with a fixed donor; forced alternatives are diagnostics",
        "cloud_policy": "unchanged primary/missing cloud for every scenario; no ERA5 optional cloud augmentation",
        "source_code_sha256": {
            str(p): W.sha(p)
            for module in (
                W,
                building,
                metrics,
                anomalies,
                importlib.import_module("smhi2epw.automatic"),
                importlib.import_module("epsm_core.compiler"),
            )
            for p in [Path(module.__file__)]
        },
        "driver_sha256": W.sha(__file__),
        "python": sys.version,
        "all_unmasked_weather_equal": True,
        "unique_weather_data_simulations": len(simulated),
    }
    engine_receipts = [
        json.loads(path.read_text())
        for path in args.output.glob("cases/*/simulation/receipt.json")
    ]
    audit["core_compiler_identities"] = sorted(
        {r["core_compiler_sha256"] for r in engine_receipts}
    )
    audit["compiled_model_identities"] = sorted(
        {r["model_sha256"] for r in engine_receipts}
    )
    audit["simulation_diagnostics"] = {
        "runs": len(engine_receipts),
        "accepted": all(r["core_diagnostics_accepted"] for r in engine_receipts),
        "warnings": sum(r["warning_count"] for r in engine_receipts),
        "severe": sum(r["severe_errors"] for r in engine_receipts),
        "fatal": sum(r["fatal_errors"] for r in engine_receipts),
    }
    if (
        len(audit["core_compiler_identities"]) != 1
        or len(audit["compiled_model_identities"]) != 1
    ):
        raise ValueError(
            "Study changed its compiler or compiled building; rerun from pinned source packages"
        )
    W.dump(args.output / "audit.json", audit)
    runtime = args.output / "reproduction"
    runtime.mkdir(exist_ok=True)
    for source in Path(__file__).parent.glob("*.py"):
        shutil.copy2(source, runtime / source.name)
    plots(args.output, table, hourly, frames, anchors)
    write_report(args.output, table, audit, event_table)
    print(args.output / "report.html", flush=True)


if __name__ == "__main__":
    main()
