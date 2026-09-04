"""Explicit Swedish EPC/Lantmäteriet ingestion outside the simulation library."""

import duckdb
from pyproj import Transformer
from shapely import from_wkb, from_wkt
from shapely.ops import transform

from epsm_core import BuildingBuilder, Footprint


def footprints(geometry, *, source_crs, target_crs="EPSG:3006"):
    """Preserve every polygon and interior ring; never silently select the first part."""
    if isinstance(geometry, (bytes, bytearray, memoryview)):
        geometry = from_wkb(bytes(geometry))
    elif isinstance(geometry, str):
        geometry = from_wkt(geometry)
    tx = Transformer.from_crs(source_crs, target_crs, always_xy=True)
    geometry = transform(tx.transform, geometry)
    if geometry.geom_type not in ("Polygon", "MultiPolygon") or not geometry.is_valid:
        raise ValueError("Input must be a valid Polygon or MultiPolygon")
    parts = geometry.geoms if geometry.geom_type == "MultiPolygon" else (geometry,)
    return tuple(
        Footprint(
            exterior=tuple(p.exterior.coords)[:-1],
            holes=tuple(tuple(r.coords)[:-1] for r in p.interiors),
        )
        for p in parts
    )


def load_records(
    db_path, *, municipality=None, building_category=None, year_min=None, year_max=None, limit=100
):
    filters = []
    params = []
    for column, value in (("IdKommun", municipality), ("EgenByggnadsKat", building_category)):
        if value is not None:
            filters.append("e." + column + "=?")
            params.append(value)
    if year_min is not None:
        filters.append("e.EgenNybyggAr>=?")
        params.append(year_min)
    if year_max is not None:
        filters.append("e.EgenNybyggAr<=?")
        params.append(year_max)
    query = """SELECT f.objektidentitet, f.geom AS footprint_wkb, e.* FROM footprints f
        LEFT JOIN (SELECT * FROM epc QUALIFY row_number() OVER
        (PARTITION BY FormularId ORDER BY IdHuvudadress DESC)=1) e ON f.FormularId=e.FormularId"""
    if filters:
        query += " WHERE " + " AND ".join(filters)
    if limit is not None:
        query += " LIMIT ?"
        params.append(int(limit))
    with duckdb.connect(str(db_path), read_only=True) as db:
        cursor = db.execute(query, params)
        columns = [c[0] for c in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def building_from_record(record, *, source_crs, assumption_profile, assumptions):
    """EPC Atemp remains an observation, not a silent rescaling of measured geometry."""
    fp = footprints(record["footprint_wkb"], source_crs=source_crs)
    known = dict(
        identifier=str(record["objektidentitet"]),
        geometry={"footprints": [f.model_dump(mode="json") for f in fp], "source_crs": "EPSG:3006"},
    )
    floors = record.get("EgenAntalPlan")
    if floors is not None and int(floors) > 0:
        known["geometry"]["floors"] = int(floors)
    builder = BuildingBuilder(assumption_profile=assumption_profile, assumptions=assumptions)
    building = builder.build(source=f"EPC:{record.get('FormularId')}", **known)
    observed = record.get("EgenAtemp")
    report = dict(
        observed_atemp_m2=observed,
        geometry_atemp_m2=building.geometry.conditioned_floor_area_m2,
        source_crs=source_crs,
        target_crs="EPSG:3006",
    )
    if observed and observed > 0:
        report["atemp_relative_difference"] = (
            building.geometry.conditioned_floor_area_m2 - observed
        ) / observed
    return building, report


def nearby(records, target, *, source_crs, radius_m=500):
    """Exact distance in a projected metre CRS, including nearby polygon edges."""
    from shapely.geometry import MultiPolygon

    origin = MultiPolygon(
        [p.polygon for p in footprints(target["footprint_wkb"], source_crs=source_crs)]
    )
    return [
        r
        for r in records
        if r["objektidentitet"] != target["objektidentitet"]
        and MultiPolygon(
            [p.polygon for p in footprints(r["footprint_wkb"], source_crs=source_crs)]
        ).distance(origin)
        <= radius_m
    ]
