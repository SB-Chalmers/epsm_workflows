"""Publication-contract EPC ingestion. Certificate energy is never broadcast to buildings."""

import csv
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
from pyproj import CRS, Transformer
from shapely import STRtree, from_wkb, from_wkt
from shapely.ops import transform

from epsm_core import (
    BuildingBuilder,
    BuildingPart,
    Footprint,
    GeometryPolicy,
    prepare_geometry,
)

from .validation import reporting_regime as _reporting_regime


def reporting_regime(certificate_date):
    """Backward-compatible re-export of the lightweight validation helper."""
    return _reporting_regime(certificate_date)


def prepare_footprints(
    geometry, *, source_crs, target_crs="EPSG:3006", policy=GeometryPolicy()
):
    if isinstance(geometry, (bytes, bytearray, memoryview)):
        geometry = from_wkb(bytes(geometry))
    elif isinstance(geometry, str):
        geometry = from_wkt(geometry)
    crs = CRS.from_user_input(target_crs)
    if not crs.is_projected or any(
        a.unit_conversion_factor != 1 for a in crs.axis_info
    ):
        raise ValueError("Geometry preparation requires a projected metre CRS")
    tx = Transformer.from_crs(source_crs, target_crs, always_xy=True)
    projected = transform(tx.transform, geometry)
    prepared = prepare_geometry(projected, policy)
    artifact = prepared.artifact(
        source_crs=str(source_crs),
        target_crs=str(target_crs),
        transformation=tx.description,
    )
    from shapely.geometry import mapping

    artifact["original_source_geometry"] = mapping(geometry)
    artifact["original_source_wkb_hex"] = geometry.wkb_hex
    artifact["transformation_definition"] = tx.definition
    return prepared, artifact


def footprints(geometry, *, source_crs, target_crs="EPSG:3006"):
    result, _ = prepare_footprints(
        geometry, source_crs=source_crs, target_crs=target_crs
    )
    if result.status != "accepted":
        raise ValueError(f"Footprint preparation {result.status}: {result.reasons}")
    g = result.cleaned_geometry
    parts = list(g.geoms) if g.geom_type == "MultiPolygon" else [g]
    return tuple(
        Footprint(
            exterior=tuple(p.exterior.coords)[:-1],
            holes=tuple(tuple(r.coords)[:-1] for r in p.interiors),
        )
        for p in parts
    )


def _rows(db, query, params):
    cursor = db.execute(query, params)
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def load_records(publication_root, *, municipality, limit=100):
    """Return certificate records with complete global relations and bounded geometries.

    Municipality selects a candidate cohort, never truncates a certificate's linked
    buildings. Global catalogue counts, history indicators and all subjects survive.
    No latest-certificate inference is made from formular_id or address identifiers.
    """
    root = Path(publication_root)
    if not re.fullmatch(r"\d{4}", municipality) or not 1 <= limit <= 10000:
        raise ValueError("Use a four-digit municipality and limit in 1..10000")
    partition = root / f"accepted_links/kommunkod={municipality}/part.parquet"
    catalogue = root / "catalogue/accepted_links.parquet"
    subjects = root / "catalogue/epc_subject_results.parquet"
    with duckdb.connect() as db:
        selected = (
            db.execute(
                "SELECT DISTINCT formular_id FROM read_parquet(?) ORDER BY formular_id LIMIT ?",
                [str(partition), limit],
            )
            .fetchnumpy()["formular_id"]
            .tolist()
        )
        if not selected:
            return []
        relations = _rows(
            db,
            "SELECT * FROM read_parquet(?) WHERE formular_id IN (SELECT unnest(?)) ORDER BY formular_id,epc_subject_id,building_id",
            [str(catalogue), selected],
        )
        subject_rows = _rows(
            db,
            "SELECT * FROM read_parquet(?) WHERE formular_id IN (SELECT unnest(?)) ORDER BY formular_id,epc_subject_id",
            [str(subjects), selected],
        )
    required = {
        "subject_to_footprint_count",
        "certificate_to_footprint_count",
        "building_to_certificate_count",
    }
    if relations and not required <= relations[0].keys():
        raise ValueError(
            "Requires global publication catalogue cardinalities, not partition-local counts"
        )
    ids_by_municipality = defaultdict(set)
    for row in relations:
        ids_by_municipality[str(row["building_municipality_code"]).zfill(4)].add(
            row["building_id"]
        )
    buildings = {}
    for code, ids in ids_by_municipality.items():
        path = root / f"building_results/kommunkod={code}/part.parquet"
        table = pq.read_table(
            path, partitioning=None, filters=[("building_id", "in", sorted(ids))]
        )
        for row in table.to_pylist():
            row["source_partition"] = str(path)
            buildings[row["building_id"]] = row
    by_certificate = defaultdict(list)
    by_subject = defaultdict(list)
    for row in relations:
        by_certificate[row["formular_id"]].append(row)
    for row in subject_rows:
        by_subject[row["formular_id"]].append(row)
    result = []
    for certificate_id in selected:
        links = by_certificate[certificate_id]
        ids = sorted({r["building_id"] for r in links})
        if any(r["certificate_to_footprint_count"] != len(ids) for r in links):
            raise ValueError("Global certificate cardinality does not reconcile")
        if any(i not in buildings for i in ids):
            raise ValueError("Published link lacks its building result")
        result.append(
            dict(
                formular_id=certificate_id,
                subjects=by_subject[certificate_id],
                accepted_links=links,
                buildings=[buildings[i] for i in ids],
                certificate_to_footprint_count=len(ids),
                publication_root=str(root),
                measurements=[],
                measurement_mapping_status="unverified",
            )
        )
    return result


def raw_certificate_rows(paths, certificate_ids, *, encoding, source_schema):
    """Streaming raw rows joined solely on the publication's verified FormularId key.

    Original string values and row identity are retained. Units, calculation versions
    and measurement dates are deliberately not inferred before workbook verification.
    """
    ids = {str(i) for i in certificate_ids}
    for path in map(Path, paths):
        with path.open(encoding=encoding, newline="") as stream:
            reader = csv.DictReader(stream, delimiter="|")
            if "FormularId" not in (reader.fieldnames or []):
                raise ValueError(f"Missing verified FormularId key: {path}")
            for row_number, row in enumerate(reader, 2):
                if row["FormularId"] in ids:
                    yield dict(
                        formular_id=int(row["FormularId"]),
                        source_file=str(path),
                        source_row=row_number,
                        source_schema=source_schema,
                        original_values=row,
                        units=None,
                        calculation_version=None,
                        measurement_dates=None,
                        mapping_status="unverified",
                    )


def building_from_record(record, *, source_crs, assumption_profile, assumptions):
    if record["certificate_to_footprint_count"] != 1 or len(record["buildings"]) != 1:
        raise ValueError(
            "One BuildingSpec requires a reviewed single-building certificate"
        )
    source = record["buildings"][0]
    result, artifact = prepare_footprints(source["geometry"], source_crs=source_crs)
    if result.status != "accepted":
        raise ValueError(f"Geometry requires review: {result.reasons}")
    fps = footprints(source["geometry"], source_crs=source_crs)
    part_assumptions = assumptions["geometry"]["parts"]
    if len(part_assumptions) != len(fps):
        raise ValueError(
            "Explicit part/storey assumptions required for each disconnected footprint"
        )
    outline = {
        "Fasad": "facade",
        "Takkant": "roof_edge_proxy",
        "Illustrativt läge": "illustrative",
    }.get(source["insamlingslage"])
    if outline is None:
        raise ValueError("Unknown outline collection position requires review")
    parts = [
        BuildingPart.model_validate(
            {
                **a,
                "footprint": fp.model_dump(),
                "outline_kind": outline,
                "outline_reviewed": False,
            }
        )
        for a, fp in zip(part_assumptions, fps)
    ]
    building = BuildingBuilder(
        assumption_profile=assumption_profile, assumptions=assumptions
    ).build(
        identifier=source["building_id"],
        geometry={"parts": [p.model_dump() for p in parts], "source_crs": "EPSG:3006"},
        source=f"epc_join:{record['formular_id']}",
    )
    return building, dict(
        geometry_preparation=artifact,
        observed_atemp_m2=None,
        geometric_conditioned_area_m2=building.geometry.conditioned_floor_area_m2,
        measurement_mapping_status=record["measurement_mapping_status"],
    )


def nearby(records, target, *, source_crs, radius_m=500):
    """Indexed neighborhood query over a caller-bounded partition/window."""
    from shapely.geometry import MultiPolygon

    geometries = [
        MultiPolygon(
            [p.polygon for p in footprints(r["geometry"], source_crs=source_crs)]
        )
        for r in records
    ]
    origin = MultiPolygon(
        [p.polygon for p in footprints(target["geometry"], source_crs=source_crs)]
    )
    tree = STRtree(geometries)
    return [
        records[i]
        for i in tree.query(origin, predicate="dwithin", distance=radius_m)
        if records[i]["building_id"] != target["building_id"]
    ]


def study_eligibility(record, review, *, mapping_verified=False):
    reasons = []
    if not mapping_verified:
        reasons.append("boverket_mapping_workbooks_unverified")
    if record["certificate_to_footprint_count"] != 1:
        reasons.append("multiple_buildings_for_certificate")
    if not review.get("reviewer") or not review.get("reviewed_at"):
        reasons.append("building_review_missing")
    for b in record["buildings"]:
        if b["insamlingslage"] != "Fasad" and not (
            b["insamlingslage"] == "Takkant" and review.get("roof_proxy_approved")
        ):
            reasons.append("outline_not_high_confidence")
    start, end = review.get("measurement_start"), review.get("measurement_end")
    if not start or not end:
        reasons.append("measurement_period_unverified")
    else:
        start, end = date.fromisoformat(start), date.fromisoformat(end)
        if start.year != end.year:
            reasons.append("cross_year_measurement_window_deferred")
        elif (start.month, start.day, end.month, end.day) != (1, 1, 12, 31):
            reasons.append("incomplete_calendar_year")
    if review.get("energy_boundary") != "delivered_district_heat_heat_and_dhw":
        reasons.append("delivery_accounting_incomparable")
    if not review.get("delivery_losses_accounted"):
        reasons.append("ideal_demand_not_delivered_energy")
    if review.get("measurement_basis") != "individual_building_meter":
        reasons.append("individual_building_measurement_unverified")
    if not review.get("normal_use_alignment_verified"):
        reasons.append("normal_use_alignment_unverified")
    if not review.get("source_totals_reconciled"):
        reasons.append("source_energy_totals_unverified")
    if not review.get("weather_provenance"):
        reasons.append("measurement_year_weather_unverified")
    return dict(
        formular_id=record["formular_id"],
        eligible=not reasons,
        reasons=sorted(set(reasons)),
        observed_atemp_m2=review.get("observed_atemp_m2"),
        source_schemas=sorted({s["source_schemas"] for s in record["subjects"]}),
    )


def study_report(records, reviews=(), *, mapping_verified=False):
    lookup = {r["formular_id"]: r for r in reviews}
    cases = [
        study_eligibility(
            r, lookup.get(r["formular_id"], {}), mapping_verified=mapping_verified
        )
        for r in records
    ]
    quotas = {
        schema: dict(
            required=6,
            eligible=sum(
                c["eligible"] and c["source_schemas"] == [schema] for c in cases
            ),
        )
        for schema in ("fore_v260", "efter_v260")
    }
    return dict(
        status="eligible_cohort"
        if all(q["eligible"] >= 6 for q in quotas.values())
        else "ineligible_cohort",
        mapping_workbook_gate="verified" if mapping_verified else "unverified",
        quotas=quotas,
        cases=cases,
        comparison_results=[],
        note="No EPC energy comparison is accepted until mapping, review, weather and delivery gates pass",
    )
