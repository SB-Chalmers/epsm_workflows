"""One-at-a-time parameter diagnostics; never choose per-building fitted models."""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from epsm_core import EnergyPlusRunner
from epsm_core.compiler import EnergyPlusCompiler

from . import archetype_diagnostics as diagnostics
from . import archetype_pilot as pilot
from .campaign import atomic_json, file_sha256
from .wwr_sensitivity import build_spec as frozen_spec

GRIDS = {
    "wall_insulation_multiplier": [0.8, 0.9, 1.1, 1.2],
    "roof_insulation_multiplier": [0.8, 0.9, 1.1, 1.2],
    "ventilation_l_s_m2_atemp": [0.30, 0.40],
    "infiltration_ach": [0.2, 0.3, 0.4],
    "wwr": [0.3],
    "soil_conductivity_w_mk": [1.0, 2.5],
    "thermal_bridge_uplift": [0.1, 0.2],
    "people_gain_multiplier": [0.8, 1.2],
    "equipment_gain_multiplier": [0.8, 1.2],
    "basement_temperature_c": [15.0],
    "basement_depth_m": [3.0],
    "attic_exchange_ach": [1.0, 2.0],
}
WINDOW_U = {"existing": 2.22, "standard": 0.90, "advanced": 0.76}


def choices(row, state):
    result = [("baseline", None), ("window_u_w_m2k", WINDOW_U[state])]
    for parameter, values in GRIDS.items():
        if parameter.startswith("basement_") and not row["heated_basement_floors"]:
            continue
        if parameter == "attic_exchange_ach" and row["roof_form"] != "pitched_ridge":
            continue
        for value in values:
            if parameter == "infiltration_ach" and value == (
                0.4 if state == "existing" else 0.2
            ):
                continue
            result.append((parameter, value))
    return result


def build_spec(row, state, weather, parameter, value):
    spec = frozen_spec(row, state, weather, 0.2)
    if (parameter, value) not in choices(row, state):
        raise ValueError("Unsupported parameter setting or not applicable to building")
    if parameter in ("baseline", "basement_temperature_c", "attic_exchange_ach"):
        return spec
    if parameter == "wall_insulation_multiplier":
        return spec.updated(
            envelope=spec.envelope.updated(
                wall=pilot._insulation_scaled(spec.envelope.wall, value)
            )
        )
    if parameter == "roof_insulation_multiplier":
        return spec.updated(
            envelope=spec.envelope.updated(
                top=pilot._insulation_scaled(spec.envelope.top, value)
            )
        )
    if parameter == "ventilation_l_s_m2_atemp":
        return spec.updated(
            airflow=spec.airflow.updated(
                mechanical_vent_l_s_m2=value
                * row["atemp_m2"]
                / spec.geometry.conditioned_floor_area_m2
            )
        )
    if parameter == "infiltration_ach":
        return spec.updated(
            airflow=spec.airflow.updated(envelope_infiltration_ach=value)
        )
    if parameter == "wwr":
        return spec.updated(facade=spec.facade.updated(default_wwr=value))
    if parameter == "window_u_w_m2k":
        return spec.updated(
            facade=spec.facade.updated(
                glazing=spec.facade.glazing.updated(u_value_w_m2k=value)
            )
        )
    if parameter == "soil_conductivity_w_mk":
        return spec.updated(
            envelope=spec.envelope.updated(
                foundation=spec.envelope.foundation.updated(
                    soil_conductivity_w_mk=value
                )
            )
        )
    if parameter == "thermal_bridge_uplift":
        return spec.updated(
            envelope=spec.envelope.updated(thermal_bridge_uplift_fraction=value)
        )
    if parameter == "people_gain_multiplier":
        return spec.updated(
            use=spec.use.updated(
                occupant_density_per_m2=spec.use.occupant_density_per_m2 * value
            )
        )
    if parameter == "equipment_gain_multiplier":
        return spec.updated(
            use=spec.use.updated(
                equipment_gain_w_m2=spec.use.equipment_gain_w_m2 * value
            )
        )
    if parameter == "basement_depth_m":
        return spec.updated(geometry=spec.geometry.updated(basement_depth_m=value))
    raise ValueError(parameter)


def patch_model(model, data, parameter, value):
    if parameter == "basement_temperature_c":
        model["Schedule:Constant"]["diagnostic_basement_temperature"] = {
            "hourly_value": value,
            "schedule_type_limits_name": "any_number",
        }
        for zone, obj in model["ThermostatSetpoint:DualSetpoint"].items():
            if "_basement_" in zone:
                obj["heating_setpoint_temperature_schedule_name"] = (
                    "diagnostic_basement_temperature"
                )
    elif parameter == "attic_exchange_ach":
        for zone in data["zones"]:
            if "_attic_" in zone:
                model.setdefault("ZoneVentilation:DesignFlowRate", {})[
                    zone + "_attic_exchange"
                ] = {
                    "zone_or_zonelist_or_space_or_spacelist_name": zone,
                    "schedule_name": "on",
                    "design_flow_rate_calculation_method": "AirChanges/Hour",
                    "air_changes_per_hour": value,
                    "ventilation_type": "Natural",
                    "minimum_indoor_temperature": -100,
                    "maximum_indoor_temperature": 100,
                    "delta_temperature": -100,
                    "maximum_wind_speed": 40,
                    "constant_term_coefficient": 1,
                    "temperature_term_coefficient": 0,
                    "velocity_term_coefficient": 0,
                    "velocity_squared_term_coefficient": 0,
                }
    return model


class SensitivityCompiler(EnergyPlusCompiler):
    """Apply only the separately declared attic or basement boundary experiment."""

    def __init__(self, executable, parameter, value):
        super().__init__(executable)
        self.parameter, self.value = parameter, value

    def compile(self, spec):
        model, report = super().compile(spec)
        model = patch_model(model, report.data, self.parameter, self.value)
        errors = list(self.validator.iter_errors(model))
        if errors:
            raise ValueError(str(errors[0]))
        data = report.data | {
            "sensitivity": {"parameter": self.parameter, "value": self.value}
        }
        return model, report.model_copy(
            update={
                "run_id": diagnostics.digest(
                    (report.run_id, self.parameter, self.value, model)
                ),
                "report_json": json.dumps(data, sort_keys=True),
            }
        )


def execute(job):
    row, state, parameter, value, weather, executable, output = job
    directory = (
        Path(output)
        / "runs"
        / diagnostics.digest((row["building_id"], state, parameter, value))[:24]
    )
    receipt = directory / "receipt.json"
    if receipt.exists():
        return pilot._read(receipt)
    directory.mkdir(parents=True)
    result = {
        "building_id": row["building_id"],
        "property_key": row["property_key"],
        "state": state,
        "parameter": parameter,
        "value": value,
        "status": "failed",
        "compiled": False,
    }
    try:
        spec = build_spec(row, state, weather, parameter, value)
        runner = EnergyPlusRunner(executable)
        runner.compiler = SensitivityCompiler(executable, parameter, value)
        model, _report = runner.compiler.compile(spec)
        result.update(
            compiled=True,
            spec_sha256=spec.physical_hash(),
            epjson_sha256=diagnostics.digest(model),
            weather_sha256=spec.climate.weather_sha256,
            conditioned_area_m2=spec.geometry.conditioned_floor_area_m2,
            conditioned_volume_m3=spec.geometry.conditioned_volume_m3,
        )
        run = runner.run(spec, directory / "energyplus")
        result.update(
            status="complete",
            demand_kwh_m2=run.scalars["space_heating_demand_kwh"] / row["atemp_m2"],
            diagnostics=run.diagnostics,
        )
    except (ValueError, RuntimeError, OSError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    atomic_json(receipt, result)
    return result


def reuse_baselines(rows, source, weather, output):
    selected = {r["building_id"]: r for r in rows}
    reused = []
    for path in sorted(Path(source).glob("runs/*/receipt.json")):
        old = pilot._read(path)
        if old["building_id"] not in selected or old["wwr"] != 0.2:
            continue
        row = selected[old["building_id"]]
        spec = build_spec(row, old["state"], weather, "baseline", None)
        if spec.physical_hash() != old["spec_sha256"]:
            raise ValueError("Frozen baseline inputs changed; baseline reuse rejected")
        result = {
            **old,
            "parameter": "baseline",
            "value": None,
            "reuse_receipt": str(path.resolve()),
            "reuse_receipt_sha256": file_sha256(path),
        }
        reused.append(result)
        directory = (
            Path(output)
            / "runs"
            / diagnostics.digest((row["building_id"], old["state"], "baseline", None))[
                :24
            ]
        )
        directory.mkdir(parents=True)
        atomic_json(directory / "receipt.json", result)
    if len(reused) != len(rows) * 3:
        raise ValueError("Incomplete baseline coverage")
    return reused


def metrics(errors, observed):
    return {
        "n": len(errors),
        "mae_kwh_m2": statistics.mean(abs(e) for e in errors),
        "bias_kwh_m2": statistics.mean(errors),
        "median_ape_pct": 100
        * statistics.median(abs(e) / o for e, o in zip(errors, observed)),
        "within_10pct": sum(abs(e) <= 0.1 * o for e, o in zip(errors, observed)),
        "within_20pct": sum(abs(e) <= 0.2 * o for e, o in zip(errors, observed)),
        "maximum_absolute_error_kwh_m2": max(map(abs, errors)),
    }


def summarize(rows, results, root):
    by_id = {r["building_id"]: r for r in rows}
    baseline = {
        (r["building_id"], r["state"]): r
        for r in results
        if r["parameter"] == "baseline"
    }
    paired = []
    for r in results:
        b = baseline[(r["building_id"], r["state"])]
        if r["status"] != "complete" or b["status"] != "complete":
            continue
        observed = (
            by_id[r["building_id"]]["heat_kwh"] / by_id[r["building_id"]]["atemp_m2"]
        )
        for loss in (0, 5, 10):
            b_error = b["demand_kwh_m2"] + loss - observed
            error = r["demand_kwh_m2"] + loss - observed
            paired.append(
                {
                    "building_id": r["building_id"],
                    "property_key": r["property_key"],
                    "state": r["state"],
                    "parameter": r["parameter"],
                    "value": r["value"],
                    "loss_kwh_m2": loss,
                    "observed_kwh_m2": observed,
                    "baseline_demand_kwh_m2": b["demand_kwh_m2"],
                    "demand_kwh_m2": r["demand_kwh_m2"],
                    "prediction_change_kwh_m2": r["demand_kwh_m2"] - b["demand_kwh_m2"],
                    "baseline_signed_error_kwh_m2": b_error,
                    "signed_error_kwh_m2": error,
                    "absolute_error_change_kwh_m2": abs(error) - abs(b_error),
                    "improved": abs(error) < abs(b_error) - 1e-7,
                    "worsened": abs(error) > abs(b_error) + 1e-7,
                }
            )
    grouped = defaultdict(list)
    for r in paired:
        grouped[(r["state"], r["parameter"], r["value"], r["loss_kwh_m2"])].append(r)
    summaries = []
    for (state, parameter, value, loss), group in grouped.items():
        observed = [r["observed_kwh_m2"] for r in group]
        before = metrics([r["baseline_signed_error_kwh_m2"] for r in group], observed)
        after = metrics([r["signed_error_kwh_m2"] for r in group], observed)
        summaries.append(
            {
                "state": state,
                "parameter": parameter,
                "value": value,
                "loss_kwh_m2": loss,
                "before": before,
                "after": after,
                "mae_improvement_kwh_m2": before["mae_kwh_m2"] - after["mae_kwh_m2"],
                "improved_buildings": sum(r["improved"] for r in group),
                "worsened_buildings": sum(r["worsened"] for r in group),
                "mean_prediction_change_kwh_m2": statistics.mean(
                    r["prediction_change_kwh_m2"] for r in group
                ),
            }
        )
    atomic_json(root / "results.json", results)
    atomic_json(root / "per-building-comparisons.json", paired)
    atomic_json(
        root / "summary.json",
        {
            "comparisons": summaries,
            "total_cases": len(results),
            "completed": sum(r["status"] == "complete" for r in results),
            "failed_cases": [r for r in results if r["status"] != "complete"],
            "compile_failures": [r for r in results if not r.get("compiled", False)],
            "calibration_performed": False,
            "new_holdout": False,
        },
    )
    text = "# One-at-a-time archetype parameter comparisons\n\n"
    text += "Every comparison changes one declared setting, from the same frozen WWR 0.2/Kiva/AMY baseline. Twelve input-selected buildings, three states. Existing properties are diagnostic. Failed buildings stay visible; no heating tails are trimmed.\n\n"
    text += "**This tests sensitivity and identifies candidates. It does not yet establish one calibrated model that predicts all buildings well.** Infiltration is varied only as a diagnostic: it is not fitted or eligible for residual-driven optimisation. Basement/attic/soil/gains/thermal bridges remain unmeasured assumptions; lower error does not establish their truth.\n\n"
    text += "## Per-state results at 5 kWh/m² delivery loss\n\n"
    for state in pilot.STATES:
        text += f"### {state.capitalize()}\n\n| One change | Value | Buildings | MAE before → after | Improved / worsened | Within 20% before → after |\n|---|---:|---:|---:|---:|---:|\n"
        for r in sorted(
            (r for r in summaries if r["state"] == state and r["loss_kwh_m2"] == 5),
            key=lambda r: (r["parameter"], r["value"] or 0),
        ):
            b, a = r["before"], r["after"]
            text += f"| {r['parameter']} | {r['value']} | {a['n']} | {b['mae_kwh_m2']:.1f} → {a['mae_kwh_m2']:.1f} | {r['improved_buildings']} / {r['worsened_buildings']} | {b['within_20pct']} → {a['within_20pct']} |\n"
        text += "\n"
    text += "## Interpretation and remaining limits\n\n"
    text += "Read per-building improvements and deteriorations, not just average MAE. Basement and attic comparisons apply only to eligible subsets; do not rank their MAE against whole-sample comparisons. For every change, use the same buildings for before/after. `per-building-comparisons.json` contains signed errors and changes under 0/5/10 delivery-loss allowances.\n\n"
    text += "There is no per-building best-state or best-parameter selection. A shared combination must be chosen on development properties, compiled and run jointly because interactions are not additive, and evaluated once on fresh properties. Original test buildings are already diagnostic. Resolve supported geometry and energy-boundary inputs before renewed bounded calibration; never absorb residuals into infiltration.\n\n"
    text += "Window values are catalogue TABULA references, not measured glazing. Wall/roof tests change only the highest-resistance insulation layer and keep material identity fixed. Soil tests keep Kiva and its other settings fixed. Basement depth changes geometry and volume, so fixed ACH produces a derived flow change; gains and mechanical-flow totals remain fixed. People and equipment are changed separately; light gains are not added on top of household electricity. Thermal-bridge uplift uses Core’s equivalent-construction method without changing the nominal catalogue.\n\n"
    text += "Weather remains measurement-year AMY. Footprint/Atemp disagreement is audited separately and not corrected by shrinking geometry. Setpoint and operating schedules remain the normal-use inputs. Unheated basements and heated attics cannot be invented without evidence.\n\n"
    text += "Source hashes, parameter grids, eligible cases, smoke outputs and failed cases are preserved. No catalogue or Core defaults are changed.\n"
    (root / "report.md").write_text(text)
    return summaries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--energyplus", required=True)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    source, root = Path(args.source).resolve(), Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    rows = diagnostics.select_diagnostics(pilot._read(source / "cohort.json"))
    weather = pilot._read(source / "audit.json")["source_weather"]
    atomic_json(root / "cohort.json", rows)
    audit = {
        "source_cohort_sha256": file_sha256(source / "cohort.json"),
        "source_audit_sha256": file_sha256(source / "audit.json"),
        "workflow_sha256": file_sha256(__file__),
        "package_sources": diagnostics.package_sources(),
        "pilot_workflow_sha256": file_sha256(pilot.__file__),
        "diagnostic_workflow_sha256": file_sha256(diagnostics.__file__),
        "grids": GRIDS,
        "window_reference_u": WINDOW_U,
        "baseline_wwr": 0.2,
        "source_weather": weather,
        "weather_hashes": {
            str(y): file_sha256(Path(weather) / f"weather-{y}.epw")
            for y in {r["measurement_year"] for r in rows}
        },
        "engine_sha256": file_sha256(args.energyplus),
        "selection_uses_heating": False,
        "calibration_performed": False,
        "infiltration_test_only": True,
        "net_losses_kwh_m2": [0, 5, 10],
        "not_tested_without_evidence": [
            "footprint rescaling",
            "invented heated attic",
            "invented unheated basement",
            "measurement-year weather selection by residual",
        ],
    }
    atomic_json(root / "audit.json", audit)
    baseline = reuse_baselines(rows, source, weather, root)
    jobs = [
        (r, s, p, v, weather, args.energyplus, str(root))
        for r in rows
        for s in pilot.STATES
        for p, v in choices(r, s)
        if p != "baseline"
    ]
    atomic_json(
        root / "cases.json",
        [
            {
                "building_id": j[0]["building_id"],
                "state": j[1],
                "parameter": j[2],
                "value": j[3],
            }
            for j in jobs
        ],
    )
    # Precompile one case of every parameter/state and both roof/basement types.
    smoke_jobs = {}
    for j in jobs:
        r, s, p, _v, *_ = j
        smoke_jobs.setdefault(
            (s, p, r["roof_form"], bool(r["heated_basement_floors"])), j
        )
    for j in smoke_jobs.values():
        spec = build_spec(j[0], j[1], weather, j[2], j[3])
        SensitivityCompiler(args.energyplus, j[2], j[3]).compile(spec)
    atomic_json(
        root / "precompile.json",
        {"representative_cases": len(smoke_jobs), "status": "passed"},
    )
    # Run one input-selected building across all parameter types as the smoke gate.
    smoke = []
    seen = set()
    for j in jobs:
        if (
            j[2] in seen
            or j[0]["building_id"] != rows[0]["building_id"]
            or j[1] != "existing"
        ):
            continue
        seen.add(j[2])
        smoke.append(j)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        smoke_results = list(pool.map(execute, smoke))
    atomic_json(root / "smoke.json", smoke_results)
    if any(r["status"] != "complete" for r in smoke_results):
        raise RuntimeError("Sensitivity smoke failed; inspect preserved failures")
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        completed = []
        for i, r in enumerate(pool.map(execute, jobs), 1):
            completed.append(r)
            if i % 50 == 0:
                print(f"{i}/{len(jobs)} sensitivity cases processed", flush=True)
    summarize(rows, baseline + completed, root)
    print(
        json.dumps(
            {
                "total": len(baseline + completed),
                "accepted": sum(
                    r["status"] == "complete" for r in baseline + completed
                ),
                "output": str(root),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
