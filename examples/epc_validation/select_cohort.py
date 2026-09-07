import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

import duckdb
from epsm_workflows.epc import prepare_footprints
from shapely import from_wkb

parser = argparse.ArgumentParser()
parser.add_argument("--inputs", type=Path, required=True)
parser.add_argument("--publication", type=Path, required=True)
args = parser.parse_args()
raw = json.loads((args.inputs / "candidate-raw.json").read_text())
groups = defaultdict(list)
for r in raw:
    groups[(r["schema"], r["values"]["FormularId"])].append(r)
root = str(args.publication)
db = duckdb.connect()
links = (
    db.execute(
        "select * from read_parquet(?) where formular_id in (select unnest(?))",
        [root + "/catalogue/accepted_links.parquet", [int(k[1]) for k in groups]],
    )
    .fetchdf()
    .to_dict("records")
)
byid = defaultdict(list)
for r in links:
    byid[str(r["formular_id"])].append(r)
buildings = {
    r["building_id"]: r
    for r in db.execute(
        "select * from read_parquet(?)", [root + "/building_results/kommunkod=1480/part.parquet"]
    )
    .fetchdf()
    .to_dict("records")
}
counts = Counter()
eligible = []
exclusions = []
for (schema, fid), occ in groups.items():
    v = occ[0]["values"]
    reasons = []
    # Conflicting simulation/measurement fields cannot be resolved by row order.
    fields = [k for k in v if k.startswith(("Egi", "Egen", "VentTyp", "ExpertGodk"))]
    if any(any(o["values"].get(k) != v.get(k) for k in fields) for o in occ):
        reasons.append("conflicting_source_rows")

    def n(k):
        return float(v.get(k, "").replace(",", ".") or 0)

    if v.get("EgiBeraknatVarde") != "Nej":
        reasons.append("calculated_or_unknown_energy")
    if n("EgenAtempBostad") != 100:
        reasons.append("not_100_percent_residential")
    if n("EgenAntalPlan") < 2 or n("EgenAtemp") <= 0:
        reasons.append("area_or_floors")
    if v.get("EgenAntalKallarplan") != "0":
        reasons.append("heated_basement_or_unknown")
    if n("EgenAvarmgarage") > 0:
        reasons.append("heated_garage")
    vents = [k for k in v if k.startswith("VentTyp") and v[k] == "Ja"]
    if vents != ["VentTypF"]:
        reasons.append("not_exhaust_only")
    start = v.get("EgiForstaArManad", "")
    end = v.get("EgiSistaArManad", "")
    if not (len(start) == 4 and start.isdigit() and end == start[:2] + "12" and start[2:] == "01"):
        reasons.append("not_calendar_year")
    heatkeys = [
        "Egi" + k + s
        for k in [
            "Olja",
            "Gas",
            "Ved",
            "Flis",
            "OvrBiobransle",
            "ElVatten",
            "ElDirekt",
            "ElLuft",
            "PumpMark",
            "PumpFranluft",
            "PumpLuftLuft",
            "PumpLuftVatten",
        ]
        for s in ([""] if schema == "fore_v260" else ["UPPV", "VV"])
    ]
    if any(n(k) > 0 for k in heatkeys + ["EgiElVV"]):
        reasons.append("mixed_heating_carriers")
    dh = (
        n("EgiFjarrvarme")
        if schema == "fore_v260"
        else n("EgiFjarrvarmeUPPV") + n("EgiFjarrvarmeVV")
    )
    if dh <= 0:
        reasons.append("missing_district_heat")
    if n("EgiSolvarme") > 0 or v.get("EgiGruppSolvarme") == "Ja":
        reasons.append("solar_thermal")
    if schema == "fore_v260" and v.get("EgiGruppFjarrvarme") not in ["Mätt", "Fördelat"]:
        reasons.append("district_heat_measurement_kind_unknown")
    rel = byid[fid]
    ids = {r["building_id"] for r in rel}
    if len(ids) != 1 or any(r["certificate_to_footprint_count"] != 1 for r in rel):
        reasons.append("not_single_building_certificate")
    b = buildings.get(next(iter(ids))) if len(ids) == 1 else None
    if b:
        if b["insamlingslage"] not in ["Fasad", "Takkant"]:
            reasons.append("unsupported_outline")
        geom = from_wkb(bytes(b["geometry"]))
        ratio = geom.area * n("EgenAntalPlan") / n("EgenAtemp") if n("EgenAtemp") else 0
        if not 0.75 <= ratio <= 1.25:
            reasons.append("geometry_area_differs_over_25pct")
        if not reasons:
            prep, artifact = prepare_footprints(geom, source_crs="EPSG:3006")
            if prep.status != "accepted":
                reasons.append("geometry_preparation_" + prep.status)
    else:
        reasons.append("building_geometry_unavailable")
    counts.update(reasons)
    if reasons:
        exclusions.append({"schema": schema, "formular_id": fid, "reasons": reasons})
        continue
    r = {
        "schema": schema,
        "formular_id": fid,
        "year": 2000 + int(start[:2]),
        "building_id": b["building_id"],
        "outline_kind": b["insamlingslage"],
        "measurement_kind": v.get("EgiGruppFjarrvarme")
        if schema == "fore_v260"
        else "measured_basis_normal_use_adjustment_possible",
        "area_ratio": ratio,
        "values": v,
        "sources": [
            {"file": o["source_file"], "row": o["source_row"], "encoding": o["encoding"]}
            for o in occ
        ],
        "preparation": artifact,
        "global_links": rel,
    }
    eligible.append(r)
print("unique", len(groups), "eligible", len(eligible), "exclusions", dict(counts))
print("byyear", Counter((r["schema"], r["year"]) for r in eligible))
(args.inputs / "eligible.json").write_text(json.dumps(eligible, default=str, ensure_ascii=False))
(args.inputs / "exclusions.json").write_text(json.dumps(exclusions))
