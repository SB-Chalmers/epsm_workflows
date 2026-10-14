"""Audit actual compiled area and gains without selecting against heating errors."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

import epsm_core
from epsm_core import load_sveby_residential_v2

from .campaign import atomic_json, file_sha256


def schedule_values(model, name):
    if name in model.get("Schedule:Constant", {}):
        return [model["Schedule:Constant"][name]["hourly_value"]] * 24
    data = model["Schedule:Compact"][name]["data"]
    values = [d["field"] for d in data if isinstance(d["field"], (float, int))]
    if len(values) != 24:
        raise ValueError(f"Expected hourly daily schedule: {name}")
    return values


def inspect_model(model, row):
    profile = load_sveby_residential_v2().use_spec()
    conditioned = {
        z: v for z, v in model["Zone"].items() if v["part_of_total_floor_area"] == "Yes"
    }
    area = sum(z["floor_area"] for z in conditioned.values())
    people = sum(p["number_of_people"] for p in model.get("People", {}).values())
    equipment = sum(
        p["design_level"] for p in model.get("ElectricEquipment", {}).values()
    )
    lighting = sum(p["lighting_level"] for p in model.get("Lights", {}).values())
    flow = sum(
        p["outdoor_air_flow_per_zone"]
        for p in model.get("DesignSpecification:OutdoorAir", {}).values()
    )
    checks = {
        "area_matches_reported_geometry_proxy": (
            area,
            row["gross_area_with_heated_basement_m2"],
        ),
        "people_total_matches_epc_atemp": (
            people,
            profile.occupant_density_per_m2 * row["atemp_m2"],
        ),
        "equipment_total_matches_epc_atemp": (
            equipment,
            profile.equipment_gain_w_m2 * row["atemp_m2"],
        ),
        "lighting_total_matches_profile": (
            lighting,
            profile.lighting_gain_w_m2 * row["atemp_m2"],
        ),
        "mechanical_flow_matches_epc_atemp": (flow, 0.35 * row["atemp_m2"] / 1000),
    }
    failures = [
        name
        for name, (actual, expected) in checks.items()
        if not math.isclose(actual, expected, rel_tol=1e-7, abs_tol=1e-7)
    ]
    for kind in ("People", "ElectricEquipment", "Lights"):
        for obj in model.get(kind, {}).values():
            if obj["zone_or_zonelist_or_space_or_spacelist_name"] not in conditioned:
                failures.append("gains_in_unconditioned_zone")
    if schedule_values(model, "occupancy_fraction") != list(
        profile.occupancy_fraction.values
    ):
        failures.append("occupancy_schedule_mismatch")
    if schedule_values(model, "activity") != [profile.occupant_activity_w] * 24:
        failures.append("activity_schedule_mismatch")
    if schedule_values(model, "equipment_fraction") != list(
        profile.equipment_fraction.values
    ):
        failures.append("equipment_schedule_mismatch")
    basement = sum(
        z["floor_area"] for name, z in conditioned.items() if "basement" in name
    )
    return {
        "compiled_area_m2": area,
        "epc_atemp_m2": row["atemp_m2"],
        "compiled_to_epc_ratio": area / row["atemp_m2"],
        "difference_m2": area - row["atemp_m2"],
        "compiled_basement_area_m2": basement,
        "peak_people": people,
        "equipment_gain_w": equipment,
        "separate_lighting_gain_w": lighting,
        "mechanical_flow_m3_s": flow,
        "failures": failures,
        "equipment_gain_kwh_m2_common_year": equipment * 8760 / 1000 / row["atemp_m2"],
        "people_total_gain_kwh_m2_common_year": people
        * profile.occupant_activity_w
        * sum(profile.occupancy_fraction.values)
        * 365
        / 1000
        / row["atemp_m2"],
        "basement_gain_allocation_fraction": basement / area,
    }


def run(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    rows = json.loads((source / "cohort.json").read_text())
    by_id = {r["building_id"]: r for r in rows}
    records, hashes, missing = [], {}, []
    for receipt_path in sorted(source.glob("runs/*/receipt.json")):
        receipt = json.loads(receipt_path.read_text())
        if receipt["wwr"] != 0.2:
            continue
        model_path = receipt_path.parent / "energyplus/model.epJSON"
        if not model_path.is_file():
            missing.append(str(model_path))
            continue
        row = by_id[receipt["building_id"]]
        records.append(
            {
                "building_id": row["building_id"],
                "property_key": row["property_key"],
                "state": receipt["state"],
                "simulation_status": receipt["status"],
                "roof_form": row["roof_form"],
                "heated_basement_floors": row["heated_basement_floors"],
                "outline": row["outline"],
                "model_path": str(model_path),
                **inspect_model(json.loads(model_path.read_text()), row),
            }
        )
        hashes[str(model_path)] = file_sha256(model_path)
    unique = {r["building_id"]: r for r in records}
    ratios = sorted(r["compiled_to_epc_ratio"] for r in unique.values())
    failures = [r for r in records if r["failures"]]
    groups = defaultdict(list)
    for r in unique.values():
        groups[
            "heated_basement"
            if r["heated_basement_floors"]
            else "no_reported_heated_basement"
        ].append(r["compiled_to_epc_ratio"])
    summary = {
        "buildings": len(unique),
        "model_count": len(records),
        "failed_checks": len(failures),
        "missing_models": missing,
        "median_compiled_to_epc_ratio": statistics.median(ratios),
        "min_ratio": min(ratios),
        "max_ratio": max(ratios),
        "compiled_area_below_epc": sum(v < 1 for v in ratios),
        "compiled_area_more_than_25pct_above_epc": sum(v > 1.25 for v in ratios),
        "groups": {
            k: {"count": len(v), "median_ratio": statistics.median(v)}
            for k, v in groups.items()
        },
        "flags_note": "Area flags are diagnostic only; no exclusions or fitted geometry corrections.",
    }
    atomic_json(
        output / "audit.json",
        {
            "source_cohort_sha256": file_sha256(source / "cohort.json"),
            "source_audit_sha256": file_sha256(source / "audit.json"),
            "audit_code_sha256": file_sha256(__file__),
            "operating_source_hashes": {
                str(path): file_sha256(path)
                for path in (
                    Path(epsm_core.__file__).parent / "standards.py",
                    Path(epsm_core.__file__).parent
                    / "standards_data/sveby_residential_v2.json",
                )
            },
            "models": hashes,
            "source_profile": load_sveby_residential_v2()
            .use_spec()
            .model_dump(mode="json"),
            "heating_energy_used": False,
            "models_modified": False,
        },
    )
    atomic_json(output / "per-building.json", records)
    atomic_json(output / "summary.json", summary)
    text = f"""# Heated area and internal gains audit

Audited {len(records)} compiled models for {len(unique)} buildings, using all three
frozen states at WWR 0.2. No models were changed and no heating errors were used.

## What we found

- The median simulated gross heated area is {statistics.median(ratios):.3f} times EPC Atemp.
- {summary["compiled_area_below_epc"]} buildings have simulated area below EPC Atemp;
  {summary["compiled_area_more_than_25pct_above_epc"]} exceed EPC Atemp by more than 25%.
- Area ratios range from {min(ratios):.3f} to {max(ratios):.3f}.
- {len(failures)} models failed area, gains, ventilation or daily schedule checks.

People and household electricity gains ARE included. Separate lighting is zero
because household electricity already supplies the residential electrical gain.
Adding another lighting allowance would risk double counting. The source profile
uses 1.68 W/m² Atemp of household gains and 80 W per person during 14 occupied
hours per day. These are operating assumptions, not measured use.

The workflow scales people, equipment and ventilation to EPC Atemp; basement
addition preserves whole-building totals. Compiled areas equal the existing gross
geometry proxy. Pitched cold attics receive no residential gains. Reported heated
basements receive the same gains per geometric m² as upper floors, which remains
an assumption. Their actual use and temperature are unknown.

## What we fixed and what remains

The earlier statement that all internal gains were zero was incorrect: it applied
only to separate lighting. The diagnostic report generator now says this explicitly. We verified the emitted models rather than adding gains.
No current numerical area/gain implementation correction was justified by this audit.

External footprint area is not internal heated Atemp. A discrepancy alone cannot
identify wall thickness, garages, partially heated basements, heated attics or an
incorrect floor count. Do not shrink footprints or select basement fractions from
heating residuals. Acquire floor plans or area breakdowns for a small set of flagged
buildings before replacing geometry. Zero reported heated basement floors also does
not establish absence of an unheated basement.

The energy denominator remains EPC Atemp, consistently for observed and simulated
heating. Geometry affects heat-transfer area and volume; changing the denominator
alone would not resolve that representation issue. Clustering/calibration remain
premature while area and delivered-heat uncertainties are unresolved.

## Traceability

`per-building.json` contains every compiled area, difference, gain total, basement
allocation and check result. `summary.json` includes basement group comparisons.
`audit.json` pins model files, cohort, audit code and operating inputs. Failed EnergyPlus
runs are included for input audit and retain their failure status; they are not new
accepted energy results.
"""
    (output / "report.md").write_text(text)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.source, args.output), indent=2))
