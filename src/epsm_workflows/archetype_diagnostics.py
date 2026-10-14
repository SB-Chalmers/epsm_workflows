"""Evidence-led MF SE.02 diagnostics; never fit parameters to observed energy.

The compiler adapter is an experimental Kiva/attic prototype, deliberately local
to this workflow. Modified epJSON is schema checked and receives its own identity.
"""

from __future__ import annotations

import argparse
import codecs
import csv
import hashlib
import json
import math
import shutil
import statistics
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import archetype_pilot as pilot
from .campaign import atomic_json, file_sha256

SEED = "mf-se02-physics-diagnostics-v1"
FORM_EVIDENCE = {
    "2.6": "https://notar-media.b-cdn.net/objects/526QPFH1F52R0POT/files/S20435/CFIL526S5H5G752VTNH3.pdf",
    "2.7": "https://notar-media.b-cdn.net/objects/56ER946I009NHM8Q/files/S20391/CFIL5LOAR243QKQTD6G1.pdf",
    "2.8": "local:Boverket supplied Formulär version 2.8.pdf",
    "3.0": "https://www.bovida.se/mspecs_cache/sites/wwwbovidase/cache/files/0708/files/d233c420-175d-11ef-b4ea-9f2c54cdb005.pdf",
}
ASSUMPTIONS = {
    "basement_depth_m": 2.5,
    "basement_temperatures_c": [15.0, 21.0],
    "attic_exchange_ach": [0.0, 1.0, 2.0],
    "loss_kwh_m2": [0.0, 5.0, 10.0],
    "soil_conductivity_w_m_k": [1.0, 1.73, 2.5],
    "soil_density_kg_m3": 1842.0,
    "soil_specific_heat_j_kg_k": 419.0,
    "deep_ground_boundary": "ZeroFlux at 40 m; groundwater level unknown",
    "note": "Study sensitivities; depth, attic exchange and soil are not measured site inputs.",
    "primary_ground": "Kiva prototype with explicit EnergyPlus 24.2 default soil settings",
    "state_weights": [1 / 3] * 3,
    "calibration_allowed": False,
    "clustering_allowed": False,
    "old_test_status": "diagnostic; all original properties prohibited in future holdout",
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def package_sources():
    import epsm_core
    import epsm_data
    import smhi2epw

    return {
        str(path): file_sha256(path)
        for module in (epsm_core, epsm_data, smhi2epw)
        for path in Path(module.__file__).parent.rglob("*")
        if path.is_file() and path.suffix in {".py", ".json", ".yaml", ".yml", ".csv"}
    }


def parse_fields(raw, *, year_bounds=(1961, 1975), ventilation_system="F"):
    version = raw.get("Version")
    if version not in FORM_EVIDENCE:
        raise ValueError("unverified_form_version")
    observation = pilot._observation(raw, year_bounds=year_bounds, ventilation_system=ventilation_system)
    try:
        basements = int(raw["EgenAntalKallarplan"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("unknown_heated_basement_count") from exc
    if basements not in (0, 1):
        raise ValueError("multiple_basement_floors_not_supported")
    return dict(
        **observation,
        form_version=version,
        energy_method_version=raw.get("EgiVersion"),
        heated_basement_floors=basements,
        measured=True,
        normal_use_adjusted=True,
        normal_year_corrected=False,
        energy_field="EgiFjarrvarmeUPPV",
        field_evidence=FORM_EVIDENCE[version],
        storey_definition="above_ground_only",
        basement_definition="heated_above_10C",
        measurement_start=raw["EgiForstaArManad"],
        measurement_end=raw["EgiSistaArManad"],
        attic_heating_status="not_reported",
        internal_area_status="unverified",
    )


def select_diagnostics(rows, count=12):
    """Stratified round robin on inputs; no observed heat or residual is read."""
    strata = defaultdict(list)
    areas = sorted(r["atemp_m2"] for r in rows)
    heights = sorted(r["representative_height_m"] for r in rows)
    for r in rows:
        key = (
            r["roof_form"],
            r["heated_basement_floors"],
            r["measurement_year"],
            r["atemp_m2"] > statistics.median(areas),
            r["representative_height_m"] > statistics.median(heights),
        )
        strata[key].append(r)
    for group in strata.values():
        group.sort(key=lambda r: digest((SEED, r["building_id"])))
    keys = sorted(strata, key=lambda k: digest((SEED, k)))
    selected = []
    while len(selected) < min(count, len(rows)):
        for k in keys:
            if strata[k]:
                selected.append(strata[k].pop(0))
                if len(selected) == min(count, len(rows)):
                    break
    return selected


def prepare(args):
    source = Path(args.study).resolve()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "audit.json").exists():
        raise FileExistsError("Use a fresh diagnostic directory")
    rows = pilot._read(source / "cohort.json")
    manifest = pilot._read(source / "manifest.json")
    wanted = defaultdict(dict)
    for row in rows:
        wanted[row["source_file"]][row["source_line"]] = row
    enriched, exclusions = [], []
    for filename, targets in sorted(wanted.items()):
        path = Path(filename)
        with path.open("rb") as f:
            encoding = (
                "utf-16"
                if f.read(4).startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE))
                else "latin-1"
            )
        with path.open(encoding=encoding, newline="") as stream:
            for line, raw in enumerate(csv.DictReader(stream, delimiter="|"), 2):
                if line > max(targets):
                    break
                if line not in targets:
                    continue
                row = targets[line]
                try:
                    if int(raw["FormularId"]) != row["formular_id"]:
                        raise ValueError("source_row_certificate_mismatch")
                    fields = parse_fields(raw) | {"source_row_sha256": digest(raw)}
                    if any(
                        fields[k] != row[k]
                        for k in ("storeys", "heat_kwh", "atemp_m2", "measurement_year")
                    ):
                        raise ValueError("source_observation_changed")
                    from shapely import from_wkb

                    gross = from_wkb(bytes.fromhex(row["geometry_wkb_hex"])).area
                    upper_area = gross * row["storeys"]
                    total = upper_area + gross * fields["heated_basement_floors"]
                    enriched.append(
                        row
                        | fields
                        | {
                            "diagnostic_status": "eligible",
                            "upper_gross_area_m2": upper_area,
                            "gross_area_with_heated_basement_m2": total,
                            "original_area_ratio": upper_area / row["atemp_m2"],
                            "basement_area_ratio": total / row["atemp_m2"],
                            "area_note": "Gross external proxy; not measured internal Atemp. No footprint rescaling.",
                        }
                    )
                except (ValueError, KeyError, TypeError) as exc:
                    exclusions.append(
                        {"building_id": row["building_id"], "reason": str(exc)}
                    )
    if len(enriched) + len(exclusions) != len(rows):
        raise ValueError("Missing source certificate rows")
    weather = Path(args.weather_dir).resolve()
    weather_audit = {}
    from epsm_core.weather import validate_epw_calendar

    for year in sorted({r["measurement_year"] for r in enriched}):
        epw = weather / f"weather-{year}.epw"
        p = pilot._read(weather / f"weather-{year}-provenance.json")
        if p["weather_sha256"] != file_sha256(epw):
            raise ValueError("Weather provenance hash mismatch")
        calendar = validate_epw_calendar(epw, year)
        with epw.open() as f:
            for _ in range(8):
                next(f)
            years = Counter(int(line.split(",", 1)[0]) for line in f)
        if set(years) != {year}:
            raise ValueError("AMY records do not match measurement year")
        weather_audit[str(year)] = {
            "path": str(epw),
            "sha256": file_sha256(epw),
            "calendar": calendar,
            "source_years": dict(years),
            "provenance": p,
            "normal_year_correction_applied": False,
        }
    root = Path(args.epc_root)
    evidence_files = [
        root / "efter_v260" / "Mappningsfil_efter_v260.xlsx",
        *list((root / "efter_v260").glob("*.pdf")),
    ]
    audit = {
        "status": "prepared",
        "original_cohort": len(rows),
        "eligible": len(enriched),
        "exclusions": exclusions,
        "form_counts": dict(Counter(r["form_version"] for r in enriched)),
        "heated_basement_count": sum(r["heated_basement_floors"] for r in enriched),
        "diagnostic_ids": [r["building_id"] for r in select_diagnostics(enriched)],
        "forbidden_future_holdout_properties": sorted(
            {r["property_key"] for r in rows}
        ),
        "source_study": str(source),
        "source_weather": str(weather),
        "source_manifest": manifest,
        "assumptions": ASSUMPTIONS,
        "evidence": FORM_EVIDENCE,
        "evidence_files": {str(p): file_sha256(p) for p in evidence_files},
        "weather": weather_audit,
        "workflow_sha256": file_sha256(Path(__file__)),
        "package_source_hashes": package_sources(),
        "comparison": {
            "target": "reported delivered district-heating space heat / EPC Atemp",
            "weather": "AMY measurement year",
            "use": "bundled residential normal use",
            "simulated": "Zone Ideal Loads Supply Air Total Heating Energy / EPC Atemp",
            "vvc": "BEN assigns hot-water circulation losses to heating; no measured VVC inputs",
            "losses": "0/5/10 net loss sensitivities; internal recovered heat must not be counted twice",
        },
    }
    atomic_json(output / "cohort.json", enriched)
    atomic_json(output / "audit.json", audit)
    (output / "comparison.md").write_text(
        "# Comparison specification\n\n"
        "Use EgiFjarrvarmeUPPV divided by EPC Atemp. Form versions 2.6/2.7/2.8/3.0 "
        "request normal-use-adjusted inputs before normal-year correction. Use AMY for "
        "the measurement year and normal-use schedules. TMY is a separate climate experiment.\n\n"
        "Ideal supply-air heating includes outdoor-air conditioning. Do not add ventilation "
        "heating again. Real distribution and VVC losses are not explicit. BEN assigns VVC "
        "losses to heating; gains from indoor pipes offset room demand. The 0/5/10 additions "
        "represent net delivered-energy uncertainty, not measured losses or prescribed factors.\n\n"
        "EgenAntalPlan counts above-ground floors. EgenAntalKallarplan counts basement floors "
        "heated above 10 C: zero is not evidence that no unheated basement exists. Follow the "
        "reported count; unknown unheated basements remain a limitation. Atemp is internal "
        "heated area, whereas our footprint is an external proxy. Heated attics are not identified.\n\n"
        "See audit.json for source hashes, version evidence, assumptions and weather filling. "
        "All prior splits are diagnostic; no renewed calibration or clustering is allowed.\n"
    )
    return audit


def build_spec(row, state, weather_dir, variant, tmy=None, alternate_weather=None):
    climate = (
        pilot._climate(str(Path(tmy).resolve()), 2023) if variant == "tmy" else None
    )
    if variant == "weather_station":
        climate = pilot._climate(
            str(Path(alternate_weather).resolve()), row["measurement_year"]
        )
    spec = pilot.build_spec(row, state, weather_dir, climate_override=climate)
    if variant in {
        "lighting",
        "combined",
        "combined_kiva",
        "combined_kiva_low_soil",
        "combined_kiva_high_soil",
    }:
        spec = spec.updated(
            use=spec.use.updated(
                lighting_gain_w_m2=spec.use.lighting_gain_w_m2
                * row["atemp_m2"]
                / spec.geometry.conditioned_floor_area_m2
            )
        )
    if variant.startswith(("basement", "combined")) and row["heated_basement_floors"]:
        depth = (
            3.0 if variant == "basement_depth_3" else ASSUMPTIONS["basement_depth_m"]
        )
        geometry = spec.geometry.updated(
            basement_depth_m=depth, basement_conditioned=True
        )
        factor = (
            spec.geometry.conditioned_floor_area_m2 / geometry.conditioned_floor_area_m2
        )
        spec = spec.updated(
            geometry=geometry,
            use=spec.use.updated(
                occupant_density_per_m2=spec.use.occupant_density_per_m2 * factor,
                equipment_gain_w_m2=spec.use.equipment_gain_w_m2 * factor,
                lighting_gain_w_m2=spec.use.lighting_gain_w_m2 * factor,
                dhw_kwh_m2_year=spec.use.dhw_kwh_m2_year * factor,
            ),
            airflow=spec.airflow.updated(
                mechanical_vent_l_s_m2=spec.airflow.mechanical_vent_l_s_m2 * factor
            ),
        )
    original = pilot.build_spec(row, state, weather_dir)
    assert spec.envelope == original.envelope, "Catalogue materials must stay fixed"
    assert (
        spec.airflow.envelope_infiltration_ach
        == original.airflow.envelope_infiltration_ach
    )
    if "kiva" in variant:
        from epsm_core import FoundationSpec

        conductivity = (
            1.0
            if variant.endswith("low_soil")
            else 2.5
            if variant.endswith("high_soil")
            else 1.73
        )
        spec = spec.updated(
            envelope=spec.envelope.updated(
                foundation=FoundationSpec(soil_conductivity_w_mk=conductivity),
                ground_temperature=None,
            )
        )
    return spec


def patch_model(model, report_data, variant):
    """Apply attic and thermostat experiments; foundations are compiled by Core."""
    if variant in {"attic_1ach", "attic_2ach"}:
        for zone in report_data["zones"]:
            if "_attic_" not in zone:
                continue
            model.setdefault("ZoneVentilation:DesignFlowRate", {})[
                zone + "_diagnostic_exchange"
            ] = {
                "zone_or_zonelist_or_space_or_spacelist_name": zone,
                "schedule_name": "on",
                "design_flow_rate_calculation_method": "AirChanges/Hour",
                "air_changes_per_hour": 1.0 if variant == "attic_1ach" else 2.0,
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
    if variant == "basement_15c":
        model["Schedule:Constant"]["diagnostic_basement_15c"] = {
            "hourly_value": 15.0,
            "schedule_type_limits_name": "any_number",
        }
        for name, obj in model["ThermostatSetpoint:DualSetpoint"].items():
            if "_basement_" in name:
                obj["heating_setpoint_temperature_schedule_name"] = (
                    "diagnostic_basement_15c"
                )
    for variable in (
        "Zone Ideal Loads Zone Total Heating Energy",
        "Zone Ideal Loads Outdoor Air Total Heating Energy",
        "Zone Ideal Loads Heat Recovery Total Heating Energy",
    ):
        model.setdefault("Output:Variable", {})[
            "diagnostic_" + digest(variable)[:8]
        ] = {
            "key_value": "*",
            "variable_name": variable,
            "reporting_frequency": "Hourly",
        }
    return model


def make_runner(executable, variant):
    from epsm_core import EnergyPlusRunner
    from epsm_core.compiler import EnergyPlusCompiler

    class DiagnosticCompiler(EnergyPlusCompiler):
        def compile(self, spec):
            model, report = super().compile(spec)
            data = report.data
            model = patch_model(model, data, variant)
            errors = list(self.validator.iter_errors(model))
            if errors:
                raise ValueError("Diagnostic epJSON schema: " + str(errors[0]))
            data = data | {
                "diagnostic_variant": variant,
                "diagnostic_assumptions": ASSUMPTIONS,
                "prototype": True,
            }
            return model, report.model_copy(
                update={
                    "run_id": digest((report.run_id, variant, model)),
                    "report_json": json.dumps(data, sort_keys=True),
                }
            )

    runner = EnergyPlusRunner(energyplus=executable)
    runner.compiler = DiagnosticCompiler(executable)
    return runner


def variants(row, tmy=None, alternate_years=()):
    result = [
        "original",
        "lighting",
        "ground_kiva",
        "combined",
        "combined_kiva",
        "combined_kiva_low_soil",
        "combined_kiva_high_soil",
    ]
    if row["heated_basement_floors"]:
        result += ["basement_21c", "basement_15c", "basement_depth_3"]
    if row["roof_form"] == "pitched_ridge":
        result += ["attic_1ach", "attic_2ach"]
    if tmy:
        result.append("tmy")
    if row["measurement_year"] in alternate_years:
        result.append("weather_station")
    return result


def foundation_specific_failures(summary):
    """Distinguish prototype failures from buildings failing every original state."""
    baseline = defaultdict(set)
    for failure in summary["failures"]:
        if failure["variant"] == "original":
            baseline[failure["building_id"]].add(failure["state"])
    failed_baselines = {
        identifier
        for identifier, states in baseline.items()
        if states == set(pilot.STATES)
    }
    return [
        f
        for f in summary["failures"]
        if "kiva" in f["variant"] and f["building_id"] not in failed_baselines
    ]


def verified_reuse(paths, current, model, report):
    """Reuse only matching specs, epJSON and engine/compiler with strict diagnostics."""
    for path in paths:
        old = pilot._read(path)
        if any(old.get(k) != current[k] for k in ("spec_sha256", "epjson_sha256")):
            continue
        directory = path.parent / "energyplus"
        compilation = pilot._read(directory / "compilation.json")
        if (
            compilation["compiler_sha256"] != report.compiler_sha256
            or compilation["engine_sha256"] != report.engine_sha256
            or compilation["schema_sha256"] != report.schema_sha256
            or digest(pilot._read(directory / "model.epJSON")) != digest(model)
        ):
            continue
        diagnostics = pilot._read(directory / "diagnostics.json")
        if old["status"] == "complete" and diagnostics.get("accepted"):
            return old, path
        if (
            old["status"] == "failed"
            and not diagnostics.get("accepted")
            and diagnostics.get("failure_code") == "ENERGYPLUS_NUMERICAL_FAILURE"
        ):
            return old, path
    return None


def simulate(args):
    root = Path(args.output).resolve()
    audit = pilot._read(root / "audit.json")
    if audit["workflow_sha256"] != file_sha256(Path(__file__)):
        raise ValueError(
            "Diagnostic code changed since preparation; start a fresh study"
        )
    if audit["package_source_hashes"] != package_sources():
        raise ValueError("Core/Data/weather source changed since preparation")
    rows = pilot._read(root / "cohort.json")
    reuse = defaultdict(list)
    if args.reuse_runs:
        for path in sorted(Path(args.reuse_runs).glob("runs/*/receipt.json")):
            old = pilot._read(path)
            if (
                old.get("compiled")
                and (path.parent / "energyplus" / "diagnostics.json").exists()
            ):
                reuse[(old["building_id"], old["state"], old["variant"])].append(path)
    alternate_weather = {}
    for item in args.alternate_weather:
        year, filename = item.split(":", 1)
        year = int(year)
        from epsm_core.weather import validate_epw_calendar

        validate_epw_calendar(filename, year)
        provenance = pilot._read(Path(filename).with_name("weather-provenance.json"))
        if provenance["weather_sha256"] != file_sha256(filename):
            raise ValueError("Alternate station provenance hash mismatch")
        if provenance["result"]["coordinate_distance_km"] > 50:
            raise ValueError("Alternate station is outside the 50 km diagnostic radius")
        alternate_weather[year] = str(Path(filename).resolve())
    atomic_json(
        root / f"{args.stage}-weather.json",
        {
            "alternate": {
                str(y): {"path": p, "sha256": file_sha256(p)}
                for y, p in alternate_weather.items()
            },
            "tmy": {
                "path": str(Path(args.tmy).resolve()),
                "sha256": file_sha256(args.tmy),
            }
            if args.tmy
            else None,
        },
    )
    selected = [r for r in rows if r["building_id"] in audit["diagnostic_ids"]]
    if args.stage == "smoke":
        representatives = {}
        for r in selected:
            representatives.setdefault((r["roof_form"], r["heated_basement_floors"]), r)
        selected = list(representatives.values())
    if args.stage == "full":
        marker = pilot._read(root / "experiments-summary.json")
        if foundation_specific_failures(marker) or marker["compile_failures"]:
            raise ValueError("Full cohort blocked: resolve prototype failures first")
        selected = rows
    if args.stage == "experiments":
        smoke = pilot._read(root / "smoke-summary.json")
        if smoke["kiva_failures"] or smoke["compile_failures"]:
            raise ValueError(
                "Smoke gate failed; retain diagnostics and resolve failures"
            )
    jobs = []
    for r in selected:
        choices = (
            ["original", "combined", "combined_kiva"]
            if args.stage == "full"
            else variants(r, args.tmy, alternate_weather)
        )
        jobs += [(r, state, variant) for state in pilot.STATES for variant in choices]

    # Compile all proposed types before simulation; each job has a fresh runner.
    def execute(job):
        row, state, variant = job
        spec = build_spec(
            row,
            state,
            audit["source_weather"],
            variant,
            args.tmy,
            alternate_weather.get(row["measurement_year"]),
        )
        identity = digest(
            (spec.model_dump(mode="json"), variant, audit["workflow_sha256"])
        )
        receipt = root / "runs" / identity[:24] / "receipt.json"
        if receipt.exists():
            return pilot._read(receipt)
        receipt.parent.mkdir(parents=True, exist_ok=True)
        value = {
            "building_id": row["building_id"],
            "property_key": row["property_key"],
            "state": state,
            "variant": variant,
            "identity": identity,
            "status": "failed",
            "compiled": False,
            "spec_sha256": digest(spec.model_dump(mode="json")),
            "observed_kwh_m2": row["heat_kwh"] / row["atemp_m2"],
        }
        try:
            runner = make_runner(args.energyplus, variant)
            model, report = runner.compiler.compile(spec)
            value.update(
                compiled=True,
                epjson_sha256=digest(model),
                conditioned_area_m2=report.data["conditioned_floor_area_m2"],
                infiltration_ach=spec.airflow.envelope_infiltration_ach,
                weather_sha256=spec.climate.weather_sha256,
                actual_ground_boundary=report.data["foundation"],
            )
            imported = verified_reuse(
                reuse[(row["building_id"], state, variant)], value, model, report
            )
            if imported:
                old, source = imported
                value = (
                    old
                    | value
                    | {
                        "status": old["status"],
                        "reuse_source_receipt": str(source.resolve()),
                        "reuse_source_receipt_sha256": file_sha256(source),
                    }
                )
                shutil.copytree(
                    source.parent / "energyplus", receipt.parent / "energyplus"
                )
                atomic_json(
                    receipt.parent / "verified-compilation.json",
                    report.model_dump(mode="json"),
                )
                atomic_json(receipt, value)
                return value
            result = runner.run(spec, receipt.parent / "energyplus")
            demand = float(result.scalars["space_heating_demand_kwh"])
            if not math.isfinite(demand) or demand < 0:
                raise ValueError("Invalid simulated demand")
            value.update(
                status="complete",
                demand_kwh_m2=demand / row["atemp_m2"],
                run_id=result.run_id,
                diagnostics=result.diagnostics,
                thermal_balance_kwh={
                    variable: sum(
                        float(values.sum()) / 3.6e6
                        for name, values in result.hourly.items()
                        if name.startswith(variable + "::")
                    )
                    for variable in (
                        "Zone Ideal Loads Supply Air Total Heating Energy",
                        "Zone Ideal Loads Zone Total Heating Energy",
                        "Zone Ideal Loads Outdoor Air Total Heating Energy",
                        "Zone Ideal Loads Heat Recovery Total Heating Energy",
                    )
                },
                scalars=result.scalars,
            )
        except Exception as exc:  # noqa: BLE001 -- preserve every failed engine case
            value["error"] = f"{type(exc).__name__}: {exc}"
        atomic_json(receipt, value)
        return value

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(execute, jobs))
    summary = {
        "stage": args.stage,
        "buildings": len(selected),
        "jobs": len(results),
        "completed": sum(r["status"] == "complete" for r in results),
        "compile_failures": sum(not r["compiled"] for r in results),
        "kiva_failures": sum(
            r["status"] != "complete" and "kiva" in r["variant"] for r in results
        ),
        "failures": [r for r in results if r["status"] != "complete"],
    }
    atomic_json(root / f"{args.stage}-results.json", results)
    atomic_json(root / f"{args.stage}-summary.json", summary)
    return summary


def report(args):
    root = Path(args.output)
    rows = pilot._read(root / "cohort.json")
    audit = pilot._read(root / "audit.json")
    path = root / "full-results.json"
    if not path.exists():
        path = root / "experiments-results.json"
    results = pilot._read(path)
    experiment_path = root / "experiments-results.json"
    if path.name == "full-results.json" and experiment_path.exists():
        seen = {(r["building_id"], r["state"], r["variant"]) for r in results}
        results += [
            r
            for r in pilot._read(experiment_path)
            if (r["building_id"], r["state"], r["variant"]) not in seen
        ]
    complete = defaultdict(dict)
    for value in results:
        if value["status"] == "complete":
            complete[(value["building_id"], value["variant"])][value["state"]] = value[
                "demand_kwh_m2"
            ]
    predictions = {
        k: [v[s] + 5 for s in pilot.STATES] for k, v in complete.items() if len(v) == 3
    }
    comparisons, per_building = {}, []
    for variant in sorted({k[1] for k in predictions}):
        subset = [
            r
            for r in rows
            if (r["building_id"], variant) in predictions
            and (r["building_id"], "original") in predictions
        ]
        if not subset:
            continue
        base = {
            r["building_id"]: predictions[(r["building_id"], "original")]
            for r in subset
        }
        changed = {
            r["building_id"]: predictions[(r["building_id"], variant)] for r in subset
        }

        def metrics(group, base=base, changed=changed):
            return {
                "baseline": pilot._metrics(group, base, (1 / 3,) * 3),
                "changed": pilot._metrics(group, changed, (1 / 3,) * 3),
                "bootstrap_95": pilot._bootstrap_intervals(
                    group, base, changed, (1 / 3,) * 3
                ),
            }

        grouped = {}
        for field in (
            "roof_form",
            "heated_basement_floors",
            "measurement_year",
            "atemp_m2",
            "representative_height_m",
        ):
            groups = defaultdict(list)
            cut = (
                statistics.median(r[field] for r in rows)
                if field in ("atemp_m2", "representative_height_m")
                else None
            )
            for r in subset:
                groups[
                    ("above_median" if r[field] > cut else "at_or_below_median")
                    if cut is not None
                    else str(r[field])
                ].append(r)
            grouped[field] = {str(k): metrics(g) for k, g in groups.items()}
        comparisons[variant] = metrics(subset) | {
            "groups": grouped,
            "sensitivities": {
                str(loss): {
                    "baseline": pilot._metrics(subset, base, (1 / 3,) * 3, loss=loss),
                    "changed": pilot._metrics(subset, changed, (1 / 3,) * 3, loss=loss),
                }
                for loss in (0, 5, 10)
            },
            "climate_only": variant == "tmy",
        }
        for r in subset:
            identifier = r["building_id"]
            observed = r["heat_kwh"] / r["atemp_m2"]
            b, c = (
                statistics.mean(base[identifier]),
                statistics.mean(changed[identifier]),
            )
            per_building.append(
                {
                    "building_id": identifier,
                    "variant": variant,
                    "observed_kwh_m2": observed,
                    "baseline_kwh_m2": b,
                    "changed_kwh_m2": c,
                    "delta_kwh_m2": c - b,
                    "signed_error_kwh_m2": c - observed,
                    "absolute_error_kwh_m2": abs(c - observed),
                    "improved": abs(c - observed) < abs(b - observed),
                    "property_key": r["property_key"],
                    "state_predictions_kwh_m2": dict(
                        zip(pilot.STATES, changed[identifier])
                    ),
                    "state_signed_errors_kwh_m2": {
                        s: p - observed
                        for s, p in zip(pilot.STATES, changed[identifier])
                    },
                }
            )
    blockers = [
        "Net district-heating/VVC losses are unmeasured; retain 0/5/10 sensitivity.",
        "Internal heated area and heated attic status are unverified; gross geometry is not Atemp.",
        "Basement depth/temperature, soil properties and attic exchange are study assumptions.",
        "Kiva exposed perimeter assumes isolated foundations; shared foundation contacts are unverified.",
        "Window area is assumed 20%; thermal-bridge uplift is zero and has not been verified against the buildings.",
    ]
    final = {
        "status": "diagnostics_complete_calibration_blocked",
        "audit": audit,
        "comparisons": comparisons,
        "per_building": per_building,
        "failed_cases": [r for r in results if r["status"] != "complete"],
        "blockers": blockers,
        "renewed_calibration_allowed": False,
        "clustering_allowed": False,
        "fresh_holdout_required": True,
        "area_audit": {
            name: {
                "median_gross_to_atemp": statistics.median(r[name] for r in rows),
                "more_than_10pct_different": sum(abs(r[name] - 1) > 0.1 for r in rows),
            }
            for name in ("original_area_ratio", "basement_area_ratio")
        },
    }
    atomic_json(root / "report.json", final)
    lines = [
        "# Building-model diagnostics",
        "",
        "## What was wrong",
        "",
        (
            f"The pilot omitted {audit['heated_basement_count']} reported heated basements. Storeys count above-ground floors; "
            "basements are additional. Lighting scaling was incomplete, but the separate lighting gain is zero in this profile, so its correction has no effect. People and household electricity gains are included. "
            "Ground heat transfer used an unexplained 18 C boundary. Attics were assumed sealed."
        ),
        "",
        "## What changed",
        "",
        (
            "Materials and infiltration stayed fixed. We tested lighting scaling, "
            "reported heated basements, basement temperature/depth, attic ventilation and Kiva. "
            "No footprint was resized to force Atemp agreement. Original train/validation/test are now diagnostic."
        ),
        "",
        "## What helped",
        "",
        (
            "Equal weights across the three frozen states; net-loss assumption 5 kWh/m². "
            "All comparisons use the same successfully simulated buildings. TMY rows describe climate sensitivity only."
        ),
        "",
        "| Experiment | Buildings | Before MAE | After MAE | After bias | Improved / worsened |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for variant, c in comparisons.items():
        p = [r for r in per_building if r["variant"] == variant]
        improved = sum(r["improved"] for r in p)
        worsened = sum(
            r["absolute_error_kwh_m2"]
            > abs(r["baseline_kwh_m2"] - r["observed_kwh_m2"]) + 1e-9
            for r in p
        )
        lines.append(
            f"| {variant} | {c['changed']['n']} | {c['baseline']['mae_kwh_m2']:.2f} | "
            f"{c['changed']['mae_kwh_m2']:.2f} | {c['changed']['bias_kwh_m2']:.2f} | {improved} / {worsened} |"
        )
    lines += [
        "",
        "## What remains uncertain",
        "",
        *["- " + b for b in blockers],
        "",
        (
            f"Failed cases retained: {len(final['failed_cases'])}. Source hashes, property exclusions, "
            "weather filling and all individual errors are in report.json and run receipts."
        ),
        "",
        "## Decision",
        "",
        (
            "Do not renew calibration or start clustering yet. "
            "A lower diagnostic error does not verify an assumed basement temperature, soil or loss allowance. "
            "Resolve the area and delivered-energy boundary, review foundation inputs, then freeze choices "
            "before evaluating on new properties."
        ),
    ]
    (root / "report.md").write_text("\n".join(lines) + "\n")
    return {
        "status": final["status"],
        "comparisons": len(comparisons),
        "failures": len(final["failed_cases"]),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--study", required=True)
    prep.add_argument("--weather-dir", required=True)
    prep.add_argument("--epc-root", required=True)
    prep.add_argument("--output", required=True)
    sim = commands.add_parser("simulate")
    sim.add_argument("--output", required=True)
    sim.add_argument("--energyplus", required=True)
    sim.add_argument("--stage", choices=("smoke", "experiments", "full"), required=True)
    sim.add_argument("--workers", type=int, default=4)
    sim.add_argument("--tmy")
    sim.add_argument(
        "--reuse-runs",
        help="Earlier diagnostic directory; verify identical physical inputs before reusing",
    )
    sim.add_argument(
        "--alternate-weather", action="append", default=[], metavar="YEAR:EPW"
    )
    rep = commands.add_parser("report")
    rep.add_argument("--output", required=True)
    args = parser.parse_args()
    value = {"prepare": prepare, "simulate": simulate, "report": report}[args.command](
        args
    )
    display = {
        k: v
        for k, v in value.items()
        if k
        in (
            "status",
            "eligible",
            "heated_basement_count",
            "completed",
            "jobs",
            "kiva_failures",
            "compile_failures",
            "comparisons",
        )
    }
    if "failures" in value:
        display["failures"] = (
            len(value["failures"])
            if isinstance(value["failures"], list)
            else value["failures"]
        )
    print(json.dumps(display, default=str))


if __name__ == "__main__":
    main()
