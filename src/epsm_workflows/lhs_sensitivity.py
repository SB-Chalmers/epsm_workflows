"""Joint Latin hypercube diagnostics; fixed infiltration and material identities."""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from scipy.stats import qmc, spearmanr
from epsm_core import EnergyPlusRunner
from epsm_core.compiler import EnergyPlusCompiler
from . import parameter_sensitivity as oat
from .campaign import atomic_json, file_sha256

BOUNDS = {
    "wall_insulation_multiplier": (0.8, 1.2),
    "roof_insulation_multiplier": (0.8, 1.2),
    "window_reference_fraction": (0.0, 1.0),
    "ventilation_l_s_m2_atemp": (0.30, 0.40),
    "wwr": (0.20, 0.30),
    "soil_conductivity_w_mk": (1.0, 2.5),
    "thermal_bridge_uplift": (0.0, 0.20),
    "people_gain_multiplier": (0.8, 1.2),
    "equipment_gain_multiplier": (0.8, 1.2),
    "basement_temperature_c": (15.0, 21.0),
    "basement_depth_m": (2.5, 3.0),
    "attic_exchange_ach": (0.0, 2.0),
}


def design(n=40, seed=71353):
    names = list(BOUNDS)
    values = qmc.LatinHypercube(d=len(names), seed=seed).random(n)
    return [
        dict(
            zip(
                names, (lo + x * (hi - lo) for x, (lo, hi) in zip(row, BOUNDS.values()))
            )
        )
        for row in values
    ]


def build_spec(row, state, weather, p):
    s = oat.frozen_spec(row, state, weather, 0.2)
    envelope = s.envelope.updated(
        wall=oat.pilot._insulation_scaled(
            s.envelope.wall, p["wall_insulation_multiplier"]
        ),
        top=oat.pilot._insulation_scaled(
            s.envelope.top, p["roof_insulation_multiplier"]
        ),
        foundation=s.envelope.foundation.updated(
            soil_conductivity_w_mk=p["soil_conductivity_w_mk"]
        ),
        thermal_bridge_uplift_fraction=p["thermal_bridge_uplift"],
    )
    u = s.facade.glazing.u_value_w_m2k
    u += p["window_reference_fraction"] * (oat.WINDOW_U[state] - u)
    s = s.updated(
        envelope=envelope,
        facade=s.facade.updated(
            default_wwr=p["wwr"], glazing=s.facade.glazing.updated(u_value_w_m2k=u)
        ),
        airflow=s.airflow.updated(
            mechanical_vent_l_s_m2=p["ventilation_l_s_m2_atemp"]
            * row["atemp_m2"]
            / s.geometry.conditioned_floor_area_m2
        ),
        use=s.use.updated(
            occupant_density_per_m2=s.use.occupant_density_per_m2
            * p["people_gain_multiplier"],
            equipment_gain_w_m2=s.use.equipment_gain_w_m2
            * p["equipment_gain_multiplier"],
        ),
    )
    if row["heated_basement_floors"]:
        s = s.updated(
            geometry=s.geometry.updated(basement_depth_m=p["basement_depth_m"])
        )
    return s


class Compiler(EnergyPlusCompiler):
    def __init__(self, executable, row, parameters):
        super().__init__(executable)
        self.row, self.parameters = row, parameters

    def compile(self, spec):
        model, report = super().compile(spec)
        if self.row["heated_basement_floors"]:
            oat.patch_model(
                model,
                report.data,
                "basement_temperature_c",
                self.parameters["basement_temperature_c"],
            )
        if self.row["roof_form"] == "pitched_ridge":
            oat.patch_model(
                model,
                report.data,
                "attic_exchange_ach",
                self.parameters["attic_exchange_ach"],
            )
        errors = list(self.validator.iter_errors(model))
        if errors:
            raise ValueError(str(errors[0]))
        data = report.data | {"lhs_parameters": self.parameters}
        return model, report.model_copy(
            update={
                "run_id": oat.diagnostics.digest(
                    (report.run_id, model, self.parameters)
                ),
                "report_json": json.dumps(data, sort_keys=True),
            }
        )


def execute(job):
    row, state, index, p, weather, executable, output = job
    directory = (
        Path(output)
        / "runs"
        / oat.diagnostics.digest((row["building_id"], state, index, p))[:24]
    )
    receipt = directory / "receipt.json"
    if receipt.exists():
        return oat.pilot._read(receipt)
    directory.mkdir(parents=True, exist_ok=True)
    result = {
        "building_id": row["building_id"],
        "state": state,
        "design_id": index,
        "status": "failed",
        "compiled": False,
    }
    started = time.monotonic()
    try:
        spec = build_spec(row, state, weather, p)
        runner = EnergyPlusRunner(executable)
        runner.compiler = Compiler(executable, row, p)
        model, _ = runner.compiler.compile(spec)
        result.update(
            compiled=True,
            spec_sha256=spec.physical_hash(),
            epjson_sha256=oat.diagnostics.digest(model),
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
    result["elapsed_seconds"] = time.monotonic() - started
    atomic_json(receipt, result)
    return result


def summarize(rows, results, designs, baseline, root):
    lookup = {
        (r["building_id"], r["state"], r["design_id"]): r["demand_kwh_m2"]
        for r in results
        if r["status"] == "complete"
    }
    base = {
        (r["building_id"], r["state"]): r["demand_kwh_m2"]
        for r in baseline
        if r["status"] == "complete"
    }
    usable = [
        r
        for r in rows
        if all(
            (r["building_id"], s) in base
            and all((r["building_id"], s, i) in lookup for i in range(len(designs)))
            for s in oat.pilot.STATES
        )
    ]
    sensitivity = []
    for row in usable:
        names = [
            p
            for p in BOUNDS
            if not (p.startswith("basement_") and not row["heated_basement_floors"])
            and not (p == "attic_exchange_ach" and row["roof_form"] != "pitched_ridge")
        ]
        x = np.array([[p[n] for n in names] for p in designs])
        z = (x - x.mean(0)) / x.std(0)
        for state in oat.pilot.STATES:
            y = np.array(
                [lookup[row["building_id"], state, i] for i in range(len(designs))]
            )
            fit = np.linalg.lstsq(np.column_stack([np.ones(len(z)), z]), y, rcond=None)[
                0
            ]
            predicted = np.column_stack([np.ones(len(z)), z]) @ fit
            r2 = 1 - float(np.sum((y - predicted) ** 2) / np.sum((y - y.mean()) ** 2))
            for j, n in enumerate(names):
                sensitivity.append(
                    {
                        "building_id": row["building_id"],
                        "state": state,
                        "parameter": n,
                        "spearman_r": float(spearmanr(x[:, j], y).statistic),
                        "regression_kwh_m2_per_input_sd": float(fit[j + 1]),
                        "linear_fit_r2": r2,
                    }
                )
    atomic_json(root / "sensitivity.json", sensitivity)
    per_building = []
    summaries = []
    selections = {}
    for state in [*oat.pilot.STATES, "equal_state_mixture"]:

        def prediction(row, index):
            states = oat.pilot.STATES if state == "equal_state_mixture" else [state]
            return float(
                np.mean(
                    [
                        base[row["building_id"], s]
                        if index is None
                        else lookup[row["building_id"], s, index]
                        for s in states
                    ]
                )
            )

        train = [r for r in usable if r["split"] == "train"]
        # One setting for all buildings, selected only with training energy.
        costs = [
            float(
                np.mean(
                    [
                        abs(prediction(r, i) + 5 - r["heat_kwh"] / r["atemp_m2"])
                        for r in train
                    ]
                )
            )
            for i in [None, *range(len(designs))]
        ]
        winner = int(np.argmin(costs)) - 1
        winner = None if winner == -1 else winner
        selections[state] = {
            "design_id": winner,
            "training_mae": min(costs),
            "training_ids": [r["building_id"] for r in train],
        }
        for loss in [0, 5, 10]:
            for row in usable:
                obs = row["heat_kwh"] / row["atemp_m2"]
                errors = [prediction(row, i) + loss - obs for i in range(len(designs))]
                oracle = int(np.argmin(np.abs(errors)))
                per_building.append(
                    {
                        "building_id": row["building_id"],
                        "property_key": row["property_key"],
                        "split": row["split"],
                        "state": state,
                        "loss": loss,
                        "observed": obs,
                        "baseline_error": prediction(row, None) + loss - obs,
                        "shared_error": prediction(row, winner) + loss - obs,
                        "oracle_error": errors[oracle],
                        "oracle_design_id": oracle,
                        "minimum_prediction": min(errors) + obs,
                        "maximum_prediction": max(errors) + obs,
                    }
                )
            for split in ["all", "train", "nontrain"]:
                sample = [
                    r
                    for r in per_building
                    if r["state"] == state
                    and r["loss"] == loss
                    and (
                        split == "all" or (r["split"] == "train") == (split == "train")
                    )
                ]
                summaries.append(
                    {
                        "state": state,
                        "loss": loss,
                        "split": split,
                        **{
                            k: oat.metrics(
                                [r[k + "_error"] for r in sample],
                                [r["observed"] for r in sample],
                            )
                            for k in ["baseline", "shared", "oracle"]
                        },
                    }
                )
    atomic_json(root / "selection.json", selections)
    atomic_json(root / "per-building.json", per_building)
    atomic_json(
        root / "summary.json",
        {
            "metrics": summaries,
            "usable_buildings": len(usable),
            "excluded_incomplete_buildings": [
                r["building_id"] for r in rows if r not in usable
            ],
            "failed_cases": [r for r in results if r["status"] != "complete"],
            "fresh_holdout": False,
            "oracle_is_retrospective": True,
        },
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--energyplus", required=True)
    p.add_argument("--designs", type=int, default=40)
    p.add_argument("--workers", type=int, default=8)
    a = p.parse_args()
    root = Path(a.output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    source = Path(a.source)
    rows = oat.pilot._read(source / "cohort.json")
    weather = oat.pilot._read(source / "audit.json")["source_weather"]
    designs = design(a.designs)
    baseline = [
        r
        for r in oat.pilot._read(source / "results.json")
        if r["parameter"] == "baseline"
    ]
    atomic_json(root / "cohort.json", rows)
    atomic_json(root / "design.json", designs)
    atomic_json(
        root / "audit.json",
        {
            "bounds": BOUNDS,
            "seed": 71353,
            "infiltration_fixed": True,
            "material_identities_fixed": True,
            "source_sha256": file_sha256(__file__),
            "source_results_sha256": file_sha256(source / "results.json"),
            "package_sources": oat.diagnostics.package_sources(),
            "weather": weather,
            "engine_sha256": file_sha256(a.energyplus),
            "selection_uses_heating": False,
            "fresh_holdout": False,
            "soil_basement_attic_are_uncertainty_scenarios": True,
        },
    )
    (root / "workflow_source_frozen.py").write_text(Path(__file__).read_text())
    jobs = [
        (r, s, i, d, weather, a.energyplus, str(root))
        for i, d in enumerate(designs)
        for r in rows
        for s in oat.pilot.STATES
    ]
    # Smoke one combined design across every selected building and state first.
    started = time.monotonic()
    results = []
    with ProcessPoolExecutor(max_workers=a.workers) as pool:
        for phase in [jobs[: len(rows) * 3], jobs[len(rows) * 3 :]]:
            for result in pool.map(execute, phase):
                results.append(result)
                if len(results) % 36 == 0:
                    print(
                        f"{len(results)}/{len(jobs)} cases, elapsed {time.monotonic() - started:.0f}s",
                        flush=True,
                    )
            if any(not r["compiled"] for r in results):
                atomic_json(root / "results.json", results)
                raise RuntimeError("Compilation failure; stopping joint experiment")
    atomic_json(root / "results.json", results)
    summarize(rows, results, designs, baseline, root)
    atomic_json(
        root / "runtime.json",
        {
            "wall_seconds": time.monotonic() - started,
            "cases": len(results),
            "complete": sum(r["status"] == "complete" for r in results),
        },
    )


if __name__ == "__main__":
    main()
