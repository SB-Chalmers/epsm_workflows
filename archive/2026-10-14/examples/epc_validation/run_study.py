"""Run frozen pilot inputs; retain failures and paired actual/TMY weather results."""

import argparse
import json
import shutil
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from epsm_workflows.validation import district_heat_estimate

from epsm_core import BuildingSpec, EnergyPlusRunner, climate_from_epw
from epsm_core.weather import file_sha256

parser = argparse.ArgumentParser()
parser.add_argument("--study", type=Path, required=True)
parser.add_argument("--runs", type=Path, required=True)
parser.add_argument("--energyplus", required=True)
parser.add_argument("--workers", type=int, default=4)
args = parser.parse_args()
study = args.study.resolve()
runs = args.runs.resolve()
cohort = json.loads((study / "cohort.json").read_text())
protocol = json.loads((study / "protocol.json").read_text())
if file_sha256(study / "protocol.json") != (study / "protocol.sha256").read_text().strip():
    raise ValueError("Frozen protocol changed")


def execute(entry, weather):
    fid = entry["formular_id"]
    case = study / "cases" / fid
    run = runs / fid / weather
    result = dict(
        formular_id=fid,
        weather=weather,
        schema=entry["schema"],
        year=entry["year"],
        run_directory=str(run),
    )
    if file_sha256(case / "building.json") != entry["building_spec_sha256"]:
        raise ValueError("Frozen building changed")
    spec = BuildingSpec.model_validate_json((case / "building.json").read_text())
    if weather == "tmy":
        spec = spec.updated(
            climate=climate_from_epw(
                study / "weather" / "landvetter-tmy.epw", calendar_year=entry["year"]
            )
        )
    try:
        output = EnergyPlusRunner(args.energyplus).run(spec, run)
        assumptions = protocol["assumptions"]
        inputs = dict(
            space_heat_kwh=output.scalars["space_heating_demand_kwh"],
            dhw_kwh=output.scalars["dhw_load_kwh"],
            atemp_m2=entry["atemp_m2"],
            space_efficiency=assumptions["space_efficiency"],
            dhw_efficiency=assumptions["dhw_efficiency"],
            airing_kwh_m2=assumptions["airing_kwh_m2"],
        )
        delivery = district_heat_estimate(
            **inputs, net_distribution_loss_kwh_m2=assumptions["net_distribution_loss_kwh_m2"]
        )
        observation = json.loads((case / "observation.json").read_text())
        reported = observation["reported_district_heat_kwh"]
        predicted = delivery["delivered_district_heat_kwh"]
        monthly = []
        import calendar

        offset = 0
        for month in range(1, 13):
            hours = 24 * calendar.monthrange(entry["year"], month)[1]
            monthly.append(
                float(output.hourly["space_heating_demand_kwh"][offset : offset + hours].sum())
            )
            offset += hours
        result.update(
            status="success",
            scalars=output.scalars,
            delivery=delivery,
            delivery_inputs=inputs,
            reported_kwh=reported,
            predicted_kwh=predicted,
            atemp_m2=entry["atemp_m2"],
            reported_kwh_m2=reported / entry["atemp_m2"],
            predicted_kwh_m2=predicted / entry["atemp_m2"],
            error_percent=100 * (predicted / reported - 1),
            monthly_space_heat_kwh=monthly,
            loss_sensitivity={
                str(loss): district_heat_estimate(**inputs, net_distribution_loss_kwh_m2=loss)[
                    "delivered_district_heat_kwh"
                ]
                for loss in protocol["sensitivity"]["net_distribution_loss_kwh_m2"]
            },
        )
    except Exception as e:
        result.update(status="failed", error=str(e), traceback=traceback.format_exc())
    destination = case / weather
    destination.mkdir(exist_ok=True)
    for name in [
        "building.json",
        "model.epJSON",
        "compilation.json",
        "diagnostics.json",
        "calendar.json",
        "result.json",
        "eplusout.err",
        "process.log",
    ]:
        if (run / name).exists():
            shutil.copy2(run / name, destination / name)
    (destination / "comparison.json").write_text(json.dumps(result, indent=2, allow_nan=False))
    return result


results = []
with ThreadPoolExecutor(max_workers=args.workers) as pool:
    futures = [pool.submit(execute, r, w) for r in cohort for w in ["actual", "tmy"]]
    for future in as_completed(futures):
        result = future.result()
        results.append(result)
        print(
            result["formular_id"],
            result["weather"],
            result["status"],
            round(result.get("error_percent", 0), 2),
            flush=True,
        )
        (study / "run-results.json").write_text(
            json.dumps(
                sorted(results, key=lambda r: (r["formular_id"], r["weather"])),
                indent=2,
                allow_nan=False,
            )
        )
print(
    "Completed",
    len(results),
    "attempts",
    sum(r["status"] == "success" for r in results),
    "successes",
    flush=True,
)
