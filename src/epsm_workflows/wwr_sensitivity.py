"""Paired WWR sensitivity on the frozen Gothenburg diagnostic cohort.

Both cases use Core Kiva, reported heated basements and frozen catalogue states.
Only default WWR differs. Previously inspected properties remain diagnostic data.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from . import archetype_diagnostics as diagnostics
from . import archetype_pilot as pilot
from .campaign import atomic_json, file_sha256

WWRS = (0.2, 0.3)


def build_spec(row, state, weather, wwr):
    if wwr not in WWRS:
        raise ValueError("This paired experiment supports only WWR 0.2 and 0.3")
    baseline = diagnostics.build_spec(row, state, weather, "combined_kiva")
    return baseline.updated(facade=baseline.facade.updated(default_wwr=wwr))


def execute(job):
    row, state, wwr, weather, executable, output, audit_hash = job
    spec = build_spec(row, state, weather, wwr)
    identity = diagnostics.digest((spec.model_dump(mode="json"), audit_hash))
    directory = Path(output) / "runs" / identity[:24]
    receipt = directory / "receipt.json"
    if receipt.exists():
        return pilot._read(receipt)
    directory.mkdir(parents=True, exist_ok=True)
    result = {
        "building_id": row["building_id"],
        "property_key": row["property_key"],
        "state": state,
        "wwr": wwr,
        "status": "failed",
        "compiled": False,
        "spec_sha256": spec.physical_hash(),
        "weather_sha256": spec.climate.weather_sha256,
    }
    try:
        runner = diagnostics.make_runner(executable, "combined_kiva")
        model, report = runner.compiler.compile(spec)
        result.update(
            compiled=True,
            epjson_sha256=diagnostics.digest(model),
            foundation=report.data["foundation"],
            conditioned_area_m2=report.data["conditioned_floor_area_m2"],
            conditioned_volume_m3=report.data["conditioned_volume_m3"],
        )
        run = runner.run(spec, directory / "energyplus")
        result.update(
            status="complete",
            run_id=run.run_id,
            diagnostics=run.diagnostics,
            heating_kwh=run.scalars["space_heating_demand_kwh"],
            demand_kwh_m2=run.scalars["space_heating_demand_kwh"] / row["atemp_m2"],
        )
    except (ValueError, RuntimeError, OSError) as error:
        result["error"] = f"{type(error).__name__}: {error}"
    atomic_json(receipt, result)
    return result


def summarize(rows, results, root):
    complete = defaultdict(dict)
    for result in results:
        if result["status"] == "complete":
            complete[(result["building_id"], result["wwr"])][result["state"]] = (
                result["demand_kwh_m2"] + 5
            )
    paired = [
        r for r in rows if all(len(complete[(r["building_id"], w)]) == 3 for w in WWRS)
    ]
    if not paired:
        raise ValueError("No buildings passed all three states at both WWR values")
    predictions = {
        w: {
            r["building_id"]: [complete[(r["building_id"], w)][s] for s in pilot.STATES]
            for r in paired
        }
        for w in WWRS
    }

    def metrics(group):
        return {
            "wwr_0.2": pilot._metrics(group, predictions[0.2], (1 / 3,) * 3),
            "wwr_0.3": pilot._metrics(group, predictions[0.3], (1 / 3,) * 3),
            "property_bootstrap_95": pilot._bootstrap_intervals(
                group, predictions[0.2], predictions[0.3], (1 / 3,) * 3
            ),
        }

    groups = {}
    for field in (
        "roof_form",
        "heated_basement_floors",
        "measurement_year",
        "atemp_m2",
        "representative_height_m",
    ):
        cut = (
            statistics.median(r[field] for r in rows)
            if field in {"atemp_m2", "representative_height_m"}
            else None
        )
        subsets = defaultdict(list)
        for row in paired:
            value = (
                ("above_median" if row[field] > cut else "at_or_below_median")
                if cut is not None
                else str(row[field])
            )
            subsets[value].append(row)
        groups[field] = {key: metrics(group) for key, group in subsets.items()}
    individual = []
    for row in paired:
        key = row["building_id"]
        observed = row["heat_kwh"] / row["atemp_m2"]
        before, after = [statistics.mean(predictions[w][key]) for w in WWRS]
        individual.append(
            {
                "building_id": key,
                "property_key": row["property_key"],
                "observed_kwh_m2": observed,
                "wwr_02_prediction_kwh_m2": before,
                "wwr_03_prediction_kwh_m2": after,
                "prediction_change_kwh_m2": after - before,
                "signed_error_before": before - observed,
                "signed_error_after": after - observed,
                "absolute_error_before": abs(before - observed),
                "absolute_error_after": abs(after - observed),
                "improved": abs(after - observed) < abs(before - observed) - 1e-9,
                "worsened": abs(after - observed) > abs(before - observed) + 1e-9,
                "state_predictions": {
                    str(w): dict(zip(pilot.STATES, predictions[w][key])) for w in WWRS
                },
            }
        )
    final = metrics(paired) | {
        "selected": len(rows),
        "paired": len(paired),
        "groups": groups,
        "per_building": individual,
        "improved": sum(r["improved"] for r in individual),
        "worsened": sum(r["worsened"] for r in individual),
        "failed_cases": [r for r in results if r["status"] != "complete"],
        "sensitivities": {
            str(loss): {
                str(w): pilot._metrics(paired, predictions[w], (1 / 3,) * 3, loss=loss)
                for w in WWRS
            }
            for loss in (0, 5, 10)
        },
        "state_metrics": {
            s: {
                str(w): pilot._metrics(
                    paired, predictions[w], tuple(float(t == s) for t in pilot.STATES)
                )
                for w in WWRS
            }
            for s in pilot.STATES
        },
        "calibration": False,
        "untouched_test": False,
        "catalogue_default_changed": False,
    }
    atomic_json(root / "report.json", final)
    a, b = final["wwr_0.2"], final["wwr_0.3"]
    lines = [
        "# WWR 0.2 versus 0.3",
        "",
        f"{len(paired)} of {len(rows)} buildings passed all three states at both WWR values.",
        "",
        "Both cases use Core Kiva, reported heated basements, the same AMY weather, and equal weights across frozen renovation states. Only WWR changes. No calibration or catalogue update.",
        "",
        "| Metric | WWR 0.2 | WWR 0.3 |",
        "|---|---:|---:|",
        f"| Mean absolute error (kWh/m²) | {a['mae_kwh_m2']:.2f} | {b['mae_kwh_m2']:.2f} |",
        f"| Median absolute percentage error | {a['median_ape_percent']:.1f}% | {b['median_ape_percent']:.1f}% |",
        f"| Signed bias (kWh/m²) | {a['bias_kwh_m2']:.2f} | {b['bias_kwh_m2']:.2f} |",
        "",
        f"**{final['improved']} buildings improved; {final['worsened']} worsened.** A positive bias means overprediction.",
        "",
        "Net delivery-loss assumption above: 5 kWh/m². Sensitivities:",
        "",
        "| Net loss | MAE at 0.2 | MAE at 0.3 |",
        "|---:|---:|---:|",
    ]
    for loss, v in final["sensitivities"].items():
        lines.append(
            f"| {loss} | {v['0.2']['mae_kwh_m2']:.2f} | {v['0.3']['mae_kwh_m2']:.2f} |"
        )
    interval = final["property_bootstrap_95"]["mae_improvement_kwh_m2"]
    lines += [
        "",
        f"Property-bootstrap 95% interval for MAE improvement: [{interval[0]:.2f}, {interval[1]:.2f}] kWh/m².",
        "",
        "Individual and state-specific predictions, grouped results, quantiles, intervals and failed cases are in report.json. Source hashes and assumptions are in audit.json; engine artifacts are in runs/.",
        "",
        "These properties were already examined. This experiment does not establish out-of-sample improvement or the buildings' actual WWR. Basement heated area, foundation inputs and delivered-energy losses remain uncertain.",
        "",
    ]
    (root / "report.md").write_text("\n".join(lines))
    print(
        json.dumps(
            {
                k: final[k]
                for k in ("paired", "wwr_0.2", "wwr_0.3", "improved", "worsened")
            },
            indent=2,
        ),
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--energyplus", required=True)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    source = Path(args.source).resolve()
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    rows = pilot._read(source / "cohort.json")
    previous = pilot._read(source / "audit.json")
    weather = previous["source_weather"]
    audit = {
        "cohort_path": str(source / "cohort.json"),
        "cohort_sha256": file_sha256(source / "cohort.json"),
        "source_audit_sha256": file_sha256(source / "audit.json"),
        "source_weather": weather,
        "engine_path": str(Path(args.energyplus).resolve()),
        "engine_sha256": file_sha256(args.energyplus),
        "package_sources": diagnostics.package_sources(),
        "workflow_sha256": file_sha256(Path(__file__)),
        "diagnostic_workflow_sha256": file_sha256(Path(diagnostics.__file__)),
        "pilot_workflow_sha256": file_sha256(Path(pilot.__file__)),
        "wwr": WWRS,
        "states": pilot.STATES,
        "weights": (1 / 3,) * 3,
        "assumptions": "Core Kiva; reported heated basements at 21 C and 2.5 m depth; AMY; losses 0/5/10; no calibration",
        "weather_hashes": {
            str(y): file_sha256(Path(weather) / f"weather-{y}.epw")
            for y in sorted({r["measurement_year"] for r in rows})
        },
    }
    if (root / "audit.json").exists() and pilot._read(
        root / "audit.json"
    ) != json.loads(json.dumps(audit)):
        raise ValueError("Inputs changed: use a fresh output directory")
    atomic_json(root / "audit.json", audit)
    atomic_json(root / "cohort.json", rows)
    audit_hash = diagnostics.digest(audit)
    representatives = {}
    for row in rows:
        if row["building_id"] in previous["diagnostic_ids"]:
            representatives.setdefault(
                (row["roof_form"], row["heated_basement_floors"]), row
            )

    def jobs(selected):
        return [
            (r, s, w, weather, args.energyplus, str(root), audit_hash)
            for r in selected
            for s in pilot.STATES
            for w in WWRS
        ]

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        smoke = list(pool.map(execute, jobs(list(representatives.values()))))
        atomic_json(root / "smoke-results.json", smoke)
        print(
            f"Smoke: {sum(r['status'] == 'complete' for r in smoke)}/{len(smoke)} accepted",
            flush=True,
        )
        if any(r["status"] != "complete" for r in smoke):
            raise ValueError(
                "Smoke gate failed; inspect retained receipts before continuing"
            )
        results = []
        for value in pool.map(execute, jobs(rows)):
            results.append(value)
            if len(results) % 60 == 0:
                print(f"Completed {len(results)}/{len(rows) * 6} cases", flush=True)
    atomic_json(root / "results.json", results)
    summarize(rows, results, root)


if __name__ == "__main__":
    main()
