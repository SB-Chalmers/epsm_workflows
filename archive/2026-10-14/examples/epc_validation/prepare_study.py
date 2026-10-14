"""Freeze a bounded, untuned EPC baseline before executing any simulations."""

import argparse
import hashlib
import json
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from epsm_workflows.fabric import fabric_from_u
from epsm_workflows.validation import district_heat_observation
from pyproj import Transformer
from shapely.geometry import shape

from epsm_core import (
    AirflowSpec,
    BuildingSpec,
    FacadeSpec,
    GeometrySpec,
    GlazingSpec,
    RoofSpec,
    ScheduleSpec,
    UseSpec,
    climate_from_epw,
)
from epsm_core.specs import ProvenanceSpec, SourceRecord
from epsm_core.weather import file_sha256

parser = argparse.ArgumentParser()
parser.add_argument("--inputs", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--tmy", type=Path, required=True)
parser.add_argument("--epc-root", type=Path, required=True)
args = parser.parse_args()
out = args.output.resolve()
if out.exists():
    raise FileExistsError("Use a fresh study directory")
out.mkdir(parents=True)
rows = json.loads((args.inputs / "eligible.json").read_text())
selected = []
seen = set()
year_counts = Counter((r["schema"], r["year"]) for r in rows)
for schema in ["fore_v260", "efter_v260"]:
    year = min(
        (y for s, y in year_counts if s == schema), key=lambda y: (-year_counts[schema, y], y)
    )
    options = sorted(
        (r for r in rows if r["schema"] == schema and r["year"] == year),
        key=lambda r: hashlib.sha256(
            ("epsm-epc-baseline-1:" + r["formular_id"]).encode()
        ).hexdigest(),
    )
    count = 0
    for r in options:
        if r["building_id"] in seen:
            continue
        selected.append(r)
        seen.add(r["building_id"])
        count += 1
        if count == 6:
            break
protocol = dict(
    study="Gothenburg 1955–1975 apartment buildings; exploratory untuned baseline v1",
    frozen_at_utc=datetime.now(timezone.utc).isoformat(),
    fitted_parameters=0,
    selection=dict(
        municipality="1480",
        construction_years=[1955, 1975],
        residential_percent=100,
        ventilation="F only",
        heated_basement_floors=0,
        minimum_above_ground_floors=2,
        geometry_area_to_atemp_range=[0.75, 1.25],
        single_building_certificate=True,
        measurement_period="complete calendar year",
        quota_per_schema=6,
        year_rule="most qualifying records per schema, earlier year breaks ties",
        ordering="ascending SHA256(epsm-epc-baseline-1:FormularId)",
        repeated_buildings="excluded across both strata",
        allocated_old_records="exploratory only",
        roof_edge_outlines="unadjusted proxy; exploratory only",
    ),
    target="reported delivered district heat for heating plus DHW, before automatic normal-year correction",
    primary_energy_comparison="not performed; normal-year correction, property electricity and regime-specific accounting unresolved",
    raw_meter_comparison="not established: old values allocated; newer values can include normal-use adjustments",
    assumptions=dict(
        wall_u_w_m2k=0.41,
        top_u_w_m2k=0.20,
        ground_u_w_m2k=0.26,
        window_u_w_m2k=2.22,
        envelope_source="ARCHIVE/FINDINGS/FINDINGS.md §4.1 TABULA reference; literature-informed assumptions, not surveyed buildings",
        thermal_bridge_uplift_fraction=0.25,
        wwr=0.20,
        effective_shgc=0.30,
        roof="compact, assumed for all cases",
        basement="none modelled; no heated basement declared does not prove no unheated basement",
        storey_height_m=2.8,
        heating_setpoint_c=21,
        infiltration_ach=0.10,
        mechanical_ventilation_l_s_m2_atemp=0.35,
        household_internal_gain_w_m2_atemp=30 * 0.7 / 8760,
        occupant_density_per_m2_atemp=0.025,
        occupant_activity_w=120,
        occupancy_fraction=14 / 24,
        dhw_kwh_m2_atemp=25,
        use_note="fixed historical BEN-era DHW baseline and conventional household/person assumptions, not observed occupant behaviour",
        ground_temperature_monthly_c=[18] * 12,
        ground_source="EnergyPlus simple Ground building-surface boundary, uncalibrated",
        space_efficiency=1,
        dhw_efficiency=1,
        airing_kwh_m2=4,
        net_distribution_loss_kwh_m2=5,
        delivery_note="simplified building-boundary district heat; 5 kWh/m² is assumed net unrecovered distribution/VVC loss, not gross VVC loss",
        context_shading="omitted; self-shading retained",
        renovation_history="not inferred from EPC recommendations or value year",
        area="footprints unchanged; gains, airflow and DHW use reported Atemp via density conversion",
    ),
    sensitivity=dict(
        net_distribution_loss_kwh_m2=[0, 10],
        weather="same frozen models with actual 2017/2018 Gothenburg and Landvetter TMYx 2004–2018; spatial and year differences confounded",
    ),
    metrics=[
        "MAE",
        "RMSE",
        "MAPE",
        "median APE",
        "signed aggregate bias",
        "R²",
        "within ±20% descriptive count",
    ],
    metric_units=["annual kWh/building", "annual kWh/m² reported Atemp"],
    inference="small selected engineering pilot, not independent national holdout; no ASHRAE monthly acceptance claim",
)


def dump(p, v):
    p.write_text(json.dumps(v, indent=2, ensure_ascii=False, allow_nan=False))


dump(out / "protocol.json", protocol)
(out / "protocol.sha256").write_text(file_sha256(out / "protocol.json") + "\n")
(out / "weather").mkdir()
for year in {r["year"] for r in selected}:
    src = args.inputs / "weather" / str(year)
    shutil.copy2(src / f"weather-{year}.epw", out / "weather" / f"weather-{year}.epw")
    shutil.copy2(
        src / "weather-provenance.json", out / "weather" / f"weather-{year}-provenance.json"
    )
shutil.copy2(args.tmy, out / "weather" / "landvetter-tmy.epw")
for schema in ["fore_v260", "efter_v260"]:
    shutil.copy2(args.inputs / f"{schema}-mapping.json", out / f"{schema}-mapping.json")
shutil.copy2(args.inputs / "exclusions.json", out / "screening-exclusions.json")
cohort = []
for r in selected:
    fid = r["formular_id"]
    directory = out / "cases" / fid
    directory.mkdir(parents=True)
    v = r["values"]
    observation = district_heat_observation(v, schema=r["schema"])
    n = int(v["EgenAntalPlan"])
    geometry = GeometrySpec.from_footprint(
        shape(r["preparation"]["cleaned_geometry"]),
        storey_elevations_m=tuple(2.8 * i for i in range(n + 1)),
        source_crs="EPSG:3006",
        roof=RoofSpec(topology="compact"),
    )
    kind = "facade" if r["outline_kind"] == "Fasad" else "roof_edge_proxy"
    geometry = geometry.updated(
        parts=tuple(p.updated(outline_kind=kind, outline_reviewed=False) for p in geometry.parts)
    )
    area = observation["atemp_m2"]
    ratio = area / geometry.conditioned_floor_area_m2
    spec = BuildingSpec(
        identifier=fid,
        geometry=geometry,
        envelope=fabric_from_u(0.41, 0.20, 0.26, "compact", 0.25),
        facade=FacadeSpec(
            default_wwr=0.2,
            glazing=GlazingSpec(u_value_w_m2k=2.22, solar_heat_gain_coefficient=0.30),
        ),
        airflow=AirflowSpec(
            topology="F", envelope_infiltration_ach=0.10, mechanical_vent_l_s_m2=0.35 * ratio
        ),
        use=UseSpec(
            heating_setpoint=ScheduleSpec.constant(21),
            occupant_density_per_m2=0.025 * ratio,
            occupant_activity_w=120,
            occupancy_fraction=ScheduleSpec.constant(14 / 24),
            equipment_gain_w_m2=30 * 0.7 / 8760 * ratio,
            dhw_kwh_m2_year=25 * ratio,
        ),
        climate=climate_from_epw(
            out / "weather" / f"weather-{r['year']}.epw", calendar_year=r["year"]
        ),
        provenance=ProvenanceSpec(
            records=(
                SourceRecord(
                    field_path="geometry.parts.footprint",
                    status="inferred",
                    source="epc_join 1.0.0 footprint; bounded geometry preparation",
                ),
                SourceRecord(
                    field_path="geometry.parts.storey_elevations_m",
                    status="inferred",
                    source="EgenAntalPlan times assumed 2.8 m; equal height across components",
                ),
                SourceRecord(field_path="envelope", status="assumed", source="protocol.json"),
                SourceRecord(field_path="facade", status="assumed", source="protocol.json"),
                SourceRecord(
                    field_path="use",
                    status="assumed",
                    source="protocol.json; density converted using EPC Atemp",
                ),
                SourceRecord(
                    field_path="airflow",
                    status="assumed",
                    source="EPC F topology; protocol.json rates",
                ),
            )
        ),
    )
    (directory / "building.json").write_text(spec.model_dump_json(indent=2))
    dump(directory / "observation.json", observation)
    # Retain only fields relevant to measurements, physical description and dates, plus source row locators.
    relevant = {
        k: value
        for k, value in v.items()
        if k.startswith(("Egi", "Egen", "VentTyp", "ExpertGodk", "FormularId"))
    }
    dump(
        directory / "source.json",
        dict(values=relevant, source_rows=r["sources"], global_links=r["global_links"]),
    )
    dump(directory / "preparation.json", r["preparation"])
    center = shape(r["preparation"]["original_source_geometry"]).centroid
    lon, lat = Transformer.from_crs(3006, 4326, always_xy=True).transform(center.x, center.y)
    entry = dict(
        formular_id=fid,
        schema=r["schema"],
        year=r["year"],
        building_id=r["building_id"],
        outline_kind=r["outline_kind"],
        measurement_kind=r["measurement_kind"],
        geometric_floor_area_m2=geometry.conditioned_floor_area_m2,
        atemp_m2=area,
        area_ratio=geometry.conditioned_floor_area_m2 / area,
        latitude=lat,
        longitude=lon,
        building_spec_sha256=file_sha256(directory / "building.json"),
        high_confidence_eligible=False,
        limitations=[
            "unsurveyed_fabric_and_height",
            "unreviewed_outline",
            "assumed_delivery_losses",
            "normal_use_adjustment_not_reconstructed",
        ]
        + (["allocated_district_heat"] if r["measurement_kind"] == "Fördelat" else []),
    )
    cohort.append(entry)
dump(out / "cohort.json", cohort)
dump(
    out / "screening.json",
    dict(
        raw_unique_certificates=len(json.loads((args.inputs / "exclusions.json").read_text()))
        + len(rows),
        exploratory_candidates=len(rows),
        candidate_years={str(k): v for k, v in year_counts.items()},
        selected=len(cohort),
        high_confidence_eligible=0,
    ),
)
mapping_sources = []
for schema in ["fore_v260", "efter_v260"]:
    p = args.epc_root / schema / f"Mappningsfil_{schema}.xlsx"
    mapping_sources.append(dict(file=str(p), sha256=file_sha256(p)))
dump(
    out / "mapping-verification.json",
    dict(
        status="verified_for_selected_fields",
        sources=mapping_sources,
        reader="@oai/artifact-tool SpreadsheetFile.importXlsx; all mapping rows extracted",
        correction="efter EgenAtemp description mistakenly says Byggnadstyp. Supplied form 2.8 p3 explicitly identifies Atemp in m², and EgenAtempBostad as percent.",
        form=str(args.epc_root / "efter_v260" / "Formul„r version 2.8.pdf"),
        form_sha256=file_sha256(args.epc_root / "efter_v260" / "Formul„r version 2.8.pdf"),
        verified_fields=[
            "FormularId",
            "EgenAtemp",
            "EgenAtempBostad",
            "EgenAntalPlan",
            "EgenAntalKallarplan",
            "EgiForstaArManad",
            "EgiSistaArManad",
            "EgiBeraknatVarde",
            "EgiFjarrvarme",
            "EgiVVBered",
            "EgiFjarrvarmeUPPV",
            "EgiFjarrvarmeVV",
            "VentTypF",
            "ExpertGodkannande",
        ],
        normal_use_source="https://www.boverket.se/sv/energideklaration/for-energiexperter/berakning-av-byggnadens-energianvandning/",
    ),
)
print(out)
print([(r["formular_id"], r["schema"], r["year"], round(r["area_ratio"], 3)) for r in cohort])
