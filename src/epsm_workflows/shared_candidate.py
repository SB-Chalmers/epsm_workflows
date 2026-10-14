"""Training-selected shared diagnostic candidates with infiltration held fixed."""

from __future__ import annotations

import argparse
import json
import statistics
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from epsm_core import EnergyPlusRunner

from . import archetype_pilot as pilot
from . import parameter_sensitivity as sensitivity
from .campaign import atomic_json, file_sha256
from .wwr_sensitivity import build_spec as frozen_spec

PRIMARY = (
    "wall_insulation_multiplier",
    "roof_insulation_multiplier",
    "window_u_w_m2k",
    "ventilation_l_s_m2_atemp",
)


def select(rows, results):
    """Read targets only from original training properties; no holdout residuals."""
    training = {r["building_id"]: r for r in rows if r["split"] == "train"}
    selected = {}
    trace = []
    for state in pilot.STATES:
        baseline = {
            r["building_id"]: r
            for r in results
            if r["state"] == state
            and r["parameter"] == "baseline"
            and r["status"] == "complete"
            and r["building_id"] in training
        }
        if not baseline:
            raise ValueError("No successful training buildings")

        def score(case, baseline=baseline):
            return statistics.mean(
                abs(
                    case[k]["demand_kwh_m2"]
                    + 5
                    - training[k]["heat_kwh"] / training[k]["atemp_m2"]
                )
                for k in baseline
            )

        base_score = score(baseline)
        choices = {}
        for parameter in PRIMARY:
            best_score = base_score
            best_value = None
            values = sorted(
                {
                    r["value"]
                    for r in results
                    if r["parameter"] == parameter and r["state"] == state
                }
            )
            for value in values:
                cases = {
                    r["building_id"]: r
                    for r in results
                    if r["state"] == state
                    and r["parameter"] == parameter
                    and r["value"] == value
                    and r["status"] == "complete"
                    and r["building_id"] in baseline
                }
                if set(cases) != set(baseline):
                    continue
                cost = score(cases)
                trace.append(
                    {
                        "state": state,
                        "parameter": parameter,
                        "value": value,
                        "training_mae_kwh_m2": cost,
                        "training_ids": sorted(baseline),
                    }
                )
                if cost < best_score - 1e-7:
                    best_score, best_value = cost, value
            choices[parameter] = best_value
        selected[state] = choices
    return {
        "settings": selected,
        "selection_trace": trace,
        "target_loss_kwh_m2": 5,
        "selection_uses_validation_energy": False,
        "selection_uses_test_energy": False,
        "infiltration_fixed": True,
        "claim": "Exploratory shared combination; no fresh holdout.",
    }


def build_spec(row, state, weather, choices):
    spec = frozen_spec(row, state, weather, 0.2)
    wall = choices.get("wall_insulation_multiplier")
    roof = choices.get("roof_insulation_multiplier")
    window = choices.get("window_u_w_m2k")
    flow = choices.get("ventilation_l_s_m2_atemp")
    if wall is not None:
        spec = spec.updated(
            envelope=spec.envelope.updated(
                wall=pilot._insulation_scaled(spec.envelope.wall, wall)
            )
        )
    if roof is not None:
        spec = spec.updated(
            envelope=spec.envelope.updated(
                top=pilot._insulation_scaled(spec.envelope.top, roof)
            )
        )
    if window is not None:
        spec = spec.updated(
            facade=spec.facade.updated(
                glazing=spec.facade.glazing.updated(u_value_w_m2k=window)
            )
        )
    if flow is not None:
        spec = spec.updated(
            airflow=spec.airflow.updated(
                mechanical_vent_l_s_m2=flow
                * row["atemp_m2"]
                / spec.geometry.conditioned_floor_area_m2
            )
        )
    return spec


def execute(job):
    row, state, weather, settings, executable, root = job
    directory = (
        Path(root)
        / "runs"
        / sensitivity.diagnostics.digest((row["building_id"], state, settings))[:24]
    )
    directory.mkdir(parents=True)
    result = {
        "building_id": row["building_id"],
        "property_key": row["property_key"],
        "split": row["split"],
        "state": state,
        "status": "failed",
        "settings": settings,
    }
    try:
        spec = build_spec(row, state, weather, settings)
        baseline = frozen_spec(row, state, weather, 0.2)
        assert (
            spec.airflow.envelope_infiltration_ach
            == baseline.airflow.envelope_infiltration_ach
        )
        runner = EnergyPlusRunner(executable)
        model, _report = runner.compiler.compile(spec)
        result.update(
            spec_sha256=spec.physical_hash(),
            compiled=True,
            epjson_sha256=sensitivity.diagnostics.digest(model),
            weather_sha256=spec.climate.weather_sha256,
        )
        run = runner.run(spec, directory / "energyplus")
        result.update(
            status="complete",
            demand_kwh_m2=run.scalars["space_heating_demand_kwh"] / row["atemp_m2"],
            diagnostics=run.diagnostics,
        )
    except (ValueError, RuntimeError, OSError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    atomic_json(directory / "receipt.json", result)
    return result


def summarize(rows, results, baseline, root):
    by_id = {r["building_id"]: r for r in rows}
    old = {(r["building_id"], r["state"]): r for r in baseline}
    paired = []
    for r in results:
        b = old[(r["building_id"], r["state"])]
        if r["status"] != "complete" or b["status"] != "complete":
            continue
        row = by_id[r["building_id"]]
        observed = row["heat_kwh"] / row["atemp_m2"]
        for loss in [0, 5, 10]:
            paired.append(
                {
                    "building_id": row["building_id"],
                    "property_key": row["property_key"],
                    "split": row["split"],
                    "state": r["state"],
                    "loss_kwh_m2": loss,
                    "observed_kwh_m2": observed,
                    "baseline_error": b["demand_kwh_m2"] + loss - observed,
                    "candidate_error": r["demand_kwh_m2"] + loss - observed,
                }
            )
    summary = []
    for state in pilot.STATES:
        for loss in [0, 5, 10]:
            for split in ["all", "train", "validation", "test"]:
                sample = [
                    r
                    for r in paired
                    if r["state"] == state
                    and r["loss_kwh_m2"] == loss
                    and (split == "all" or r["split"] == split)
                ]
                observed = [r["observed_kwh_m2"] for r in sample]
                summary.append(
                    {
                        "state": state,
                        "loss_kwh_m2": loss,
                        "split": split,
                        "baseline": sensitivity.metrics(
                            [r["baseline_error"] for r in sample], observed
                        ),
                        "candidate": sensitivity.metrics(
                            [r["candidate_error"] for r in sample], observed
                        ),
                        "improved": sum(
                            abs(r["candidate_error"]) < abs(r["baseline_error"]) - 1e-7
                            for r in sample
                        ),
                        "worsened": sum(
                            abs(r["candidate_error"]) > abs(r["baseline_error"]) + 1e-7
                            for r in sample
                        ),
                    }
                )
    atomic_json(root / "per-building.json", paired)
    atomic_json(root / "results.json", results)
    atomic_json(
        root / "summary.json",
        {
            "metrics": summary,
            "failed": [r for r in results if r["status"] != "complete"],
            "fresh_holdout": False,
        },
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sensitivity", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--energyplus", required=True)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    source, base, root = (
        Path(args.sensitivity).resolve(),
        Path(args.baseline).resolve(),
        Path(args.output).resolve(),
    )
    selection = select(
        pilot._read(source / "cohort.json"), pilot._read(source / "results.json")
    )
    root.mkdir(parents=True, exist_ok=False)
    atomic_json(root / "selection.json", selection)
    rows = pilot._read(base / "cohort.json")
    weather = pilot._read(base / "audit.json")["source_weather"]
    audit = {
        "workflow_sha256": file_sha256(__file__),
        "sensitivity_workflow_sha256": file_sha256(sensitivity.__file__),
        "source_sensitivity_results_sha256": file_sha256(source / "results.json"),
        "source_cohort_sha256": file_sha256(base / "cohort.json"),
        "package_sources": sensitivity.diagnostics.package_sources(),
        "settings_frozen_before_full_cohort": True,
        "weather_hashes": {
            str(y): file_sha256(Path(weather) / f"weather-{y}.epw")
            for y in {r["measurement_year"] for r in rows}
        },
        "engine_sha256": file_sha256(args.energyplus),
        "not_a_fresh_holdout": True,
    }
    atomic_json(root / "audit.json", audit)
    baseline = [
        pilot._read(p)
        for p in base.glob("runs/*/receipt.json")
        if pilot._read(p)["wwr"] == 0.2
    ]
    jobs = [
        (r, s, weather, selection["settings"][s], args.energyplus, str(root))
        for r in rows
        for s in pilot.STATES
    ]
    # Compile one model for every state/roof/basement type before running.
    seen = set()
    for job in jobs:
        row, state, *_ = job
        key = (state, row["roof_form"], bool(row["heated_basement_floors"]))
        if key not in seen:
            seen.add(key)
            EnergyPlusRunner(args.energyplus).compiler.compile(
                build_spec(row, state, weather, selection["settings"][state])
            )
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        results = []
        for i, result in enumerate(pool.map(execute, jobs), 1):
            results.append(result)
            if i % 50 == 0:
                print(f"{i}/{len(jobs)} shared candidate cases processed", flush=True)
    summary = summarize(rows, results, baseline, root)
    text = "# Shared candidates following the separate parameter tests\n\n"
    text += "Settings were chosen using only the six original-training buildings in the diagnostic sample. One setting per state is applied to all buildings. Wall, roof, window and ventilation settings are combined and actually simulated; gains, infiltration, WWR, soil, basements and attics stay at baseline. No building-specific fitting.\n\n"
    text += "The former validation and test properties have already been inspected in earlier diagnostics. Their results are diagnostic confirmation, **not fresh out-of-sample evidence**.\n\n"
    text += (
        "## Shared settings\n\n```json\n"
        + json.dumps(selection["settings"], indent=2)
        + "\n```\n\n"
    )
    text += "## Entire paired cohort, 5 kWh/m² loss allowance\n\n| State | Buildings | MAE before → after | Within 20% before → after | Improved / worsened |\n|---|---:|---:|---:|---:|\n"
    for r in summary:
        if r["split"] == "all" and r["loss_kwh_m2"] == 5:
            b, a = r["baseline"], r["candidate"]
            text += f"| {r['state']} | {a['n']} | {b['mae_kwh_m2']:.1f} → {a['mae_kwh_m2']:.1f} | {b['within_20pct']} → {a['within_20pct']} | {r['improved']} / {r['worsened']} |\n"
    text += "\nThese small training-selected candidates are exploratory, not accepted catalogue replacements. Choosing each setting from baseline sensitivity does not account for interactions; the joint run exposes those. Individual errors, worst errors, bias, split-specific results and 0/5/10 loss sensitivities are preserved. If substantial errors remain, do not tune infiltration to erase them. Resolve building evidence and obtain a fresh property-separated holdout before claiming a calibrated representative archetype.\n"
    (root / "report.md").write_text(text)
    print(json.dumps(selection["settings"], indent=2))


if __name__ == "__main__":
    main()
