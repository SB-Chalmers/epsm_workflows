"""Fixed-input heat-balance diagnostics, with one isolated window-U sensitivity."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from epsm_core import EnergyPlusRunner
from epsm_core.compiler import EnergyPlusCompiler
from epsm_core.settings import CompilerSettings, HourlyOutputRequest

from . import archetype_diagnostics as diagnostics
from . import archetype_pilot as pilot
from .campaign import atomic_json, file_sha256
from .wwr_sensitivity import build_spec as wwr_spec

AIR_TERMS = {
    "internal_convective": "Zone Air Heat Balance Internal Convective Heat Gain Rate",
    "surface_convective": "Zone Air Heat Balance Surface Convection Rate",
    "interzone_air": "Zone Air Heat Balance Interzone Air Transfer Rate",
    "outdoor_air": "Zone Air Heat Balance Outdoor Air Transfer Rate",
    "system_air": "Zone Air Heat Balance System Air Transfer Rate",
    "system_convective": "Zone Air Heat Balance System Convective Heat Gain Rate",
    "air_storage": "Zone Air Heat Balance Air Energy Storage Rate",
    "engine_deviation": "Zone Air Heat Balance Deviation Rate",
}
SUPPLY = "Zone Ideal Loads Supply Air Total Heating Energy"
EXTRA = (
    "Surface Inside Face Conduction Heat Transfer Rate",
    "Surface Inside Face Convection Heat Gain Rate",
    "Surface Window Heat Gain Energy",
    "Surface Window Heat Loss Energy",
    "Surface Window Transmitted Solar Radiation Energy",
    "Zone People Total Heating Energy",
    "Zone Electric Equipment Total Heating Energy",
    "Zone Lights Total Heating Energy",
    "Zone Ideal Loads Outdoor Air Total Heating Energy",
    "Zone Ideal Loads Zone Total Heating Energy",
    "Zone Ideal Loads Supply Air Sensible Heating Energy",
)


def settings():
    return CompilerSettings(
        hourly_outputs=tuple(
            HourlyOutputRequest(name=name) for name in (*AIR_TERMS.values(), *EXTRA)
        )
    )


class HeatBalanceCompiler(EnergyPlusCompiler):
    """Expose the advanced imbalance output without changing physical inputs."""

    def compile(self, spec):
        model, report = super().compile(spec)
        model["Output:Diagnostics"] = {
            "heat_balance": {"diagnostics": [{"key": "DisplayAdvancedReportVariables"}]}
        }
        errors = list(self.validator.iter_errors(model))
        if errors:
            raise ValueError(str(errors[0]))
        data = report.data | {"heat_balance_advanced_outputs": True}
        return model, report.model_copy(
            update={
                "run_id": diagnostics.digest((report.run_id, model)),
                "report_json": json.dumps(data, sort_keys=True),
            }
        )


def build_spec(row, state, weather, variant):
    spec = wwr_spec(row, state, weather, 0.2)
    if variant == "baseline":
        return spec
    if variant != "window_2.22" or state != "existing":
        raise ValueError("Only existing-state window U=2.22 sensitivity is supported")
    return spec.updated(
        facade=spec.facade.updated(
            glazing=spec.facade.glazing.updated(u_value_w_m2k=2.22)
        )
    )


def sql_signals(path):
    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) as db:
        dictionary = {
            i: (name, key, units)
            for i, name, key, units in db.execute(
                "SELECT ReportDataDictionaryIndex,Name,KeyValue,Units FROM ReportDataDictionary WHERE IsMeter=0 AND ReportingFrequency='Hourly'"
            )
        }
        grouped = defaultdict(list)
        for i, time, value, interval in db.execute("""
            SELECT d.ReportDataDictionaryIndex,t.TimeIndex,d.Value,t.Interval
            FROM ReportData d JOIN Time t ON t.TimeIndex=d.TimeIndex
            JOIN EnvironmentPeriods e ON e.EnvironmentPeriodIndex=t.EnvironmentPeriodIndex
            WHERE COALESCE(t.WarmupFlag,0)=0 AND e.EnvironmentType=3
            ORDER BY d.ReportDataDictionaryIndex,t.TimeIndex"""):
            if i in dictionary:
                grouped[i].append((time, value, interval))
    signals = {}
    for i, points in grouped.items():
        name, key, units = dictionary[i]
        times, values, intervals = np.array(points).T
        if np.any(intervals != 60):
            raise ValueError("Diagnostic requires hourly intervals")
        signals[(name.upper(), key.upper())] = (units, values, times)
    return signals


def integrate(units, values):
    if units == "W":
        return float(np.sum(values) / 1000)
    if units == "J":
        return float(np.sum(values) / 3.6e6)
    raise ValueError(f"Unsupported heat-balance units: {units}")


def classify_surfaces(model):
    conditioned = {
        z.upper()
        for z, v in model["Zone"].items()
        if v["part_of_total_floor_area"] == "Yes"
    }
    categories = {}
    for name, obj in model["BuildingSurface:Detailed"].items():
        if obj["zone_name"].upper() not in conditioned:
            continue
        boundary, kind = obj["outside_boundary_condition"], obj["surface_type"]
        if boundary in ("Foundation", "Ground", "GroundFCfactorMethod"):
            category = "ground"
        elif boundary == "Outdoors" and kind == "Wall":
            category = "external_wall"
        elif kind in ("Roof", "Ceiling") and (
            boundary == "Outdoors"
            or "attic" in obj.get("outside_boundary_condition_object", "").lower()
        ):
            category = "roof_or_attic_ceiling"
        else:
            category = "internal_surfaces"
        categories[name.upper()] = category
    for name, obj in model.get("FenestrationSurface:Detailed", {}).items():
        if obj["building_surface_name"].upper() in categories:
            categories[name.upper()] = "windows"
    for name, obj in model.get("InternalMass", {}).items():
        if obj["zone_or_zonelist_name"].upper() in conditioned:
            categories[name.upper()] = "internal_surfaces"
    return conditioned, categories


def extract(path, model):
    signals = sql_signals(path)
    conditioned, surfaces = classify_surfaces(model)

    def vectors(name, keys):
        result = [
            v
            for (n, k), (u, v, t) in signals.items()
            if n == name.upper() and k in keys
        ]
        if not result:
            raise ValueError(f"Missing required output {name}")
        lengths = {len(v) for v in result}
        if len(lengths) != 1 or next(iter(lengths)) not in (8760, 8784):
            raise ValueError("Incomplete actual-year heat-balance outputs")
        return np.sum(result, axis=0)

    air = {key: vectors(name, conditioned) for key, name in AIR_TERMS.items()}
    ideal_keys = {z + "_IDEAL" for z in conditioned}
    supply = vectors(SUPPLY, ideal_keys) / 3.6e6
    active = supply > 1e-8
    terms = {k: float(v.sum() / 1000) for k, v in air.items()}
    residual = (
        sum(v for k, v in air.items() if k not in ("air_storage", "engine_deviation"))
        - air["air_storage"]
    )
    conduction, convection, windows, gains = (
        defaultdict(float),
        defaultdict(float),
        defaultdict(float),
        defaultdict(float),
    )
    heating_conduction, heating_convection = defaultdict(float), defaultdict(float)
    for (name, key), (units, values, times) in signals.items():
        if key in surfaces:
            category = surfaces[key]
            if name == EXTRA[0].upper():
                conduction[category] += integrate(units, values)
                heating_conduction[category] += integrate(units, values[active])
            elif name == EXTRA[1].upper():
                # Engine surface convection is positive INTO the face. Reverse it
                # to match the zone-air convention (positive warms zone air).
                convection[category] -= integrate(units, values)
                heating_convection[category] -= integrate(units, values[active])
            elif name in {n.upper() for n in EXTRA[2:5]}:
                windows[name] += integrate(units, values)
        if key in (conditioned | ideal_keys) and name in {n.upper() for n in EXTRA[5:]}:
            gains[name] += integrate(units, values)
    return {
        "air_balance_signed_kwh": terms,
        "heating_hours_air_balance_signed_kwh": {
            k: float(v[active].sum() / 1000) for k, v in air.items()
        },
        "air_balance_residual_signed_kwh": float(residual.sum() / 1000),
        "air_balance_residual_absolute_kwh": float(np.abs(residual).sum() / 1000),
        "air_balance_residual_max_hour_kwh": float(np.max(np.abs(residual)) / 1000),
        "residual_minus_engine_deviation_kwh": float(
            (residual - air["engine_deviation"]).sum() / 1000
        ),
        "opaque_inside_conduction_signed_kwh": dict(conduction),
        "surface_convection_signed_kwh": dict(convection),
        "heating_hours_opaque_inside_conduction_signed_kwh": dict(heating_conduction),
        "heating_hours_surface_convection_signed_kwh": dict(heating_convection),
        "window_outputs_kwh": dict(windows),
        "zone_outputs_kwh": dict(gains),
        "supply_heating_kwh": float(supply.sum()),
        "heating_hours": int(active.sum()),
        "surface_convection_reconciliation_kwh": sum(convection.values())
        - terms["surface_convective"],
    }


def execute(job):
    row, state, variant, weather, executable, output = job
    spec = build_spec(row, state, weather, variant)
    runner = EnergyPlusRunner(executable, settings=settings())
    runner.compiler = HeatBalanceCompiler(executable, settings=settings())
    identity = diagnostics.digest(
        (spec.model_dump(mode="json"), settings().model_dump(mode="json"))
    )
    directory = Path(output) / "runs" / identity[:24]
    receipt = directory / "receipt.json"
    if receipt.exists():
        return pilot._read(receipt)
    directory.mkdir(parents=True)
    result = {
        "building_id": row["building_id"],
        "property_key": row["property_key"],
        "state": state,
        "variant": variant,
        "status": "failed",
        "spec_sha256": spec.physical_hash(),
        "weather_sha256": spec.climate.weather_sha256,
        "wwr": spec.facade.default_wwr,
        "window_u_w_m2k": spec.facade.glazing.u_value_w_m2k,
        "run_directory": str(directory),
    }
    try:
        run = runner.run(spec, directory / "energyplus")
        model = pilot._read(directory / "energyplus/model.epJSON")
        heat = extract(directory / "energyplus/eplusout.sql", model)
        if not math.isclose(
            heat["supply_heating_kwh"],
            run.scalars["space_heating_demand_kwh"],
            abs_tol=1e-5,
        ):
            raise ValueError("Heating scalar does not match independent SQL extraction")
        result.update(
            status="complete",
            diagnostics=run.diagnostics,
            heat_balance=heat,
            demand_kwh_m2=heat["supply_heating_kwh"] / row["atemp_m2"],
        )
        atomic_json(directory / "heat-balance.json", heat)
    except (ValueError, RuntimeError, OSError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    atomic_json(receipt, result)
    return result


def full_errors(rows, source):
    receipts = defaultdict(dict)
    hashes = {}
    for path in sorted(Path(source).glob("runs/*/receipt.json")):
        r = pilot._read(path)
        if r["wwr"] == 0.2:
            receipts[r["building_id"]][r["state"]] = r
            hashes[str(path)] = file_sha256(path)
    result = []
    for row in rows:
        for state in pilot.STATES:
            r = receipts[row["building_id"]][state]
            record = {
                k: row[k]
                for k in (
                    "building_id",
                    "property_key",
                    "atemp_m2",
                    "roof_form",
                    "heated_basement_floors",
                )
            }
            record.update(
                state=state,
                status=r["status"],
                observed_kwh_m2=row["heat_kwh"] / row["atemp_m2"],
            )
            if r["status"] == "complete":
                record["ideal_loads_kwh_m2"] = r["demand_kwh_m2"]
                record["loss_sensitivities"] = {
                    str(loss): {
                        "signed_error_kwh_m2": r["demand_kwh_m2"]
                        + loss
                        - record["observed_kwh_m2"],
                        "absolute_error_kwh_m2": abs(
                            r["demand_kwh_m2"] + loss - record["observed_kwh_m2"]
                        ),
                    }
                    for loss in (0, 5, 10)
                }
            result.append(record)
    return result, hashes


def summarize(rows, results, all_errors, output, original):
    comparisons, reconciliations = [], []
    for result in results:
        if result["status"] != "complete":
            continue
        row = next(r for r in rows if r["building_id"] == result["building_id"])
        observed = row["heat_kwh"] / row["atemp_m2"]
        result["observed_kwh_m2"] = observed
        result["signed_error_at_5_kwh_m2"] = result["demand_kwh_m2"] + 5 - observed
        if result["variant"] == "baseline":
            previous = next(
                r
                for r in all_errors
                if r["building_id"] == row["building_id"]
                and r["state"] == result["state"]
            )
            if previous["status"] == "complete":
                delta = result["demand_kwh_m2"] - previous["ideal_loads_kwh_m2"]
                reconciliations.append(delta)
                if abs(delta) > 1e-5:
                    raise ValueError("Output-only baseline changed heating result")
        else:
            baseline = next(
                r
                for r in results
                if r["building_id"] == row["building_id"]
                and r["state"] == "existing"
                and r["variant"] == "baseline"
            )
            if baseline["status"] == "complete":
                comparisons.append(
                    {
                        "building_id": row["building_id"],
                        "prediction_change_kwh_m2": result["demand_kwh_m2"]
                        - baseline["demand_kwh_m2"],
                        "baseline_error_kwh_m2": baseline["demand_kwh_m2"]
                        + 5
                        - observed,
                        "window_2.22_error_kwh_m2": result["signed_error_at_5_kwh_m2"],
                        "improved": abs(result["signed_error_at_5_kwh_m2"])
                        < abs(baseline["demand_kwh_m2"] + 5 - observed),
                    }
                )
    metrics = {}
    for state in pilot.STATES:
        sample = [
            r for r in all_errors if r["state"] == state and r["status"] == "complete"
        ]
        metrics[state] = {
            str(loss): {
                "count": len(sample),
                "mae_kwh_m2": statistics.mean(
                    r["loss_sensitivities"][str(loss)]["absolute_error_kwh_m2"]
                    for r in sample
                ),
                "bias_kwh_m2": statistics.mean(
                    r["loss_sensitivities"][str(loss)]["signed_error_kwh_m2"]
                    for r in sample
                ),
                "overpredicted": sum(
                    r["loss_sensitivities"][str(loss)]["signed_error_kwh_m2"] > 0
                    for r in sample
                ),
            }
            for loss in (0, 5, 10)
        }
    summary = {
        "full_cohort_state_metrics": metrics,
        "diagnostic_cases": len(results),
        "complete": sum(r["status"] == "complete" for r in results),
        "failed": [r for r in results if r["status"] != "complete"],
        "window_comparisons": comparisons,
        "max_output_only_heating_change_kwh_m2": max(
            map(abs, reconciliations), default=None
        ),
    }
    atomic_json(Path(output) / "results.json", results)
    atomic_json(Path(output) / "summary.json", summary)
    text = "# Individual errors and heat-balance diagnostics\n\n"
    text += "These are diagnostic properties, not a fresh holdout. WWR stays 0.2, with Core Kiva, reported heated basements, measurement-year AMY, frozen materials and infiltration.\n\n"
    text += "## Each state separately: full cohort\n\n| State | MAE at 5 kWh/m² losses | Signed bias | Overpredicted buildings |\n|---|---:|---:|---:|\n"
    for state in pilot.STATES:
        m = metrics[state]["5"]
        text += f"| {state} | {m['mae_kwh_m2']:.2f} | {m['bias_kwh_m2']:+.2f} | {m['overpredicted']} / {m['count']} |\n"
    text += f"\n## Controlled diagnostic runs\n\n{summary['complete']}/{len(results)} cases passed strict simulation and output checks. "
    text += "The 12 buildings were selected using roof, basement, area, height and year, without observing heating energy. "
    text += f"Output-only heating change from the frozen baseline: {summary['max_output_only_heating_change_kwh_m2']} kWh/m² maximum.\n\n"
    text += "## Isolated window test\n\nOnly the existing-state window U-value changes, from 2.8 to the catalogue TABULA reference 2.22 W/m²K. Solar properties, WWR, constructions, infiltration, operating inputs and weather stay fixed. This is a sensitivity, not proof of measured glazing.\n\n"
    if comparisons:
        text += f"Mean prediction change: {statistics.mean(r['prediction_change_kwh_m2'] for r in comparisons):+.2f} kWh/m². {sum(r['improved'] for r in comparisons)}/{len(comparisons)} buildings improve at the 5 kWh/m² boundary.\n\n"
    text += "## Read the heat balance correctly\n\nPositive signed air-balance terms warm zone air; negative terms cool it. Storage is subtracted when calculating closure. Surface convection is split into wall, roof/attic ceiling, ground, windows and internal surfaces; its engine sign is reversed to give the same positive-to-zone-air convention as the air balance. Separate surface and zone integrations can differ slightly because of solver averaging; the discrepancy is preserved. These are convective exchanges at the room boundary, not isolated U-value losses. Opaque inside-face conduction is reported separately and includes storage and absorbed radiation. Window heat gain/loss and transmitted solar are overlapping diagnostics, never summed into the air balance.\n\n"
    text += "Mechanical outdoor-air coil heating is reported separately from infiltration/outdoor-air transfer into zone air. Supply-air heating is the benchmark; do not add outdoor-air heating to it again. Internal convective gain is part of the closed air balance; total people/equipment gains include radiant and latent portions and cannot be added again. Annual solar input is not necessarily useful winter heating.\n\n"
    text += "Hourly air-balance residuals and the engine deviation are preserved; positive heating-only components are not asserted to sum to delivered district heat. The 0/5/10 net-loss assumptions remain explicit sensitivities.\n\n"
    text += "## Per-building files\n\n`full-cohort-errors.json` contains every state’s signed and absolute error at all three delivery losses. `results.json` contains per-building component totals and heating-hour budgets. Each run retains hourly SQL, model, diagnostics, heat balance and receipt. `summary.json` includes failures and each window-test improvement/worsening. No calibration or clustering is performed.\n\n"
    text += "Output definitions: [EnergyPlus 24.2 reference](https://bigladdersoftware.com/epx/docs/24-2/input-output-reference/group-thermal-zone-description-geometry.html).\n"
    (Path(output) / "report.md").write_text(text)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--energyplus", required=True)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    source, output = Path(args.source).resolve(), Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    rows = pilot._read(source / "cohort.json")
    weather = pilot._read(source / "audit.json")["source_weather"]
    selected = diagnostics.select_diagnostics(rows)
    all_errors, receipts = full_errors(rows, source)
    audit = {
        "source": str(source),
        "source_cohort_sha256": file_sha256(source / "cohort.json"),
        "source_audit_sha256": file_sha256(source / "audit.json"),
        "source_receipts": receipts,
        "package_sources": diagnostics.package_sources(),
        "workflow_sha256": file_sha256(__file__),
        "pilot_sha256": file_sha256(pilot.__file__),
        "diagnostics_sha256": file_sha256(diagnostics.__file__),
        "source_weather": weather,
        "weather_hashes": {
            str(y): file_sha256(Path(weather) / f"weather-{y}.epw")
            for y in {r["measurement_year"] for r in rows}
        },
        "selected_ids": [r["building_id"] for r in selected],
        "selection_uses_energy": False,
        "energyplus_sha256": file_sha256(args.energyplus),
        "settings": settings().model_dump(mode="json"),
        "wwr": 0.2,
        "loss_sensitivities": [0, 5, 10],
        "new_holdout": False,
    }
    atomic_json(output / "audit.json", audit)
    atomic_json(output / "cohort.json", selected)
    atomic_json(output / "full-cohort-errors.json", all_errors)
    smoke = execute(
        (selected[0], "existing", "baseline", weather, args.energyplus, str(output))
    )
    atomic_json(output / "smoke.json", smoke)
    if smoke["status"] != "complete":
        raise RuntimeError("Heat-balance smoke failed; review retained diagnostics")
    jobs = [
        (r, s, "baseline", weather, args.energyplus, str(output))
        for r in selected
        for s in pilot.STATES
    ]
    jobs += [
        (r, "existing", "window_2.22", weather, args.energyplus, str(output))
        for r in selected
    ]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(execute, jobs))
    summary = summarize(rows, results, all_errors, output, source)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
