"""Derive native shading from existing footprints, LiDAR heights and ground levels.

References: context_diagnostics.py measured-neighbour workflow; Core ContextSpec.
Unknown heights and overlapping roof outlines are reported, never repaired by
changing the target footprint. Neighbour function alone does not establish a
heated shared wall. Previously reviewed full-height contacts may be retained.
"""

import math
from pathlib import Path

import duckdb
from epsm_core import ContextSpec
from epsm_core.context import resolve_context
from shapely import STRtree, from_wkb

from ..campaign import file_sha256
from ..context_diagnostics import contact_evidence, shading_groups


def derive_context(rows, settings):
    """Read local geometry releases only; cache measured neighbour records by ID."""
    publication = Path(settings["publication"])
    height = Path(settings["height_release"])
    selection = Path(settings["height_selection"])
    radius = settings.get("radius_m", 100)
    sources = {str(selection.resolve()): file_sha256(selection)}
    result = {}
    with duckdb.connect() as db:
        for municipality in sorted({str(r["municipality"]) for r in rows}):
            path = (
                publication / f"building_results/kommunkod={municipality}/part.parquet"
            )
            source = db.execute(
                "SELECT building_id,geometry,insamlingslage,andamal1 FROM read_parquet(?)",
                [str(path)],
            ).fetchall()
            sources[str(path.resolve())] = file_sha256(path)
            polygons = [from_wkb(bytes(r[1])) for r in source]
            tree = STRtree(polygons)
            index = {r[0]: i for i, r in enumerate(source)}
            targets = [r for r in rows if str(r["municipality"]) == municipality]
            neighbours, ids = {}, set()
            for row in targets:
                if row["building_id"] not in index:
                    continue
                polygon = polygons[index[row["building_id"]]]
                indices = [
                    int(i)
                    for i in tree.query(polygon.buffer(radius))
                    if source[int(i)][0] != row["building_id"]
                    and polygon.distance(polygons[int(i)]) <= radius
                ]
                neighbours[row["building_id"]] = indices
                ids.add(row["building_id"])
                ids.update(source[i][0] for i in indices)
            db.execute("CREATE OR REPLACE TEMP TABLE context_ids(building_id VARCHAR)")
            if not ids:
                for row in targets:
                    result[row["building_id"]] = {
                        "records": [],
                        "excluded": [{"reason": "target_footprint_missing"}],
                    }
                continue
            db.executemany(
                "INSERT INTO context_ids VALUES (?)", [(i,) for i in sorted(ids)]
            )
            tiles = db.execute(
                "SELECT DISTINCT owner_tile_id FROM read_parquet(?) JOIN context_ids USING(building_id)",
                [str(selection)],
            ).fetchall()
            paths = [
                height / f"results/buildings/owner_tile_id={tile}/part.parquet"
                for (tile,) in tiles
            ]
            paths = [p for p in paths if p.is_file()]
            sources.update({str(p.resolve()): file_sha256(p) for p in paths})
            heights = (
                db.execute(
                    "SELECT building_id,height_status,representative_height_m,height_p90_m,eave_height_m,ridge_height_m,ground_elevation_m,roof_form FROM read_parquet(?,union_by_name=true,hive_partitioning=false) JOIN context_ids USING(building_id)",
                    [[str(p) for p in paths]],
                ).fetchall()
                if paths
                else []
            )
            fields = (
                "status",
                "representative",
                "p90",
                "eave",
                "ridge",
                "ground",
                "roof",
            )
            observed = {r[0]: dict(zip(fields, r[1:], strict=True)) for r in heights}
            for row in targets:
                bid = row["building_id"]
                ground = observed.get(bid, {}).get("ground")
                records, excluded = [], []
                if bid not in index or ground is None or not math.isfinite(ground):
                    result[bid] = {
                        "records": [],
                        "excluded": [{"reason": "target_ground_missing"}],
                    }
                    continue
                target = polygons[index[bid]]
                for i in neighbours.get(bid, []):
                    neighbour, _, outline, function = source[i]
                    h = observed.get(neighbour, {})
                    valid = [
                        h[k]
                        for k in ("ridge", "p90", "representative")
                        if h.get(k) is not None and math.isfinite(h[k]) and h[k] > 0
                    ]
                    reason = (
                        "height_missing"
                        if not valid or h.get("status") != "determined"
                        else "ground_missing"
                        if h.get("ground") is None or not math.isfinite(h["ground"])
                        else "target_overlap"
                        if target.intersection(polygons[i]).area > 1e-8
                        else None
                    )
                    if reason:
                        excluded.append({"building_id": neighbour, "reason": reason})
                        continue
                    records.append(
                        {
                            "building_id": neighbour,
                            "geometry_wkb_hex": polygons[i].wkb_hex,
                            "outline": outline,
                            "function": function,
                            "height_m": max(valid),
                            "relative_base_m": h["ground"] - ground,
                            "height_source": h,
                            "relative_eave_top_m": h["ground"]
                            - ground
                            + (h.get("eave") or max(valid)),
                            "heated_contact_reviewed": False,
                        }
                    )
                result[bid] = {
                    "records": records,
                    "excluded": excluded,
                    "target_ground_elevation_m": ground,
                }
    return result, sources


def apply_context(spec, row, prepared, radius=100):
    """Use native validated context; preserve unsupported-neighbour diagnostics."""
    records = prepared["records"]
    buildings, groups = shading_groups(records)
    accepted, rejected, contacts = [], [], []
    for building in buildings:
        try:
            resolve_context(spec.geometry, ContextSpec(buildings=(building,)))
            accepted.append(building)
        except ValueError as exc:
            rejected.append({"identifier": building.identifier, "reason": str(exc)})
    for record in records:
        contacts.extend(contact_evidence(spec, record, row.get("outline", "unknown")))
    context = ContextSpec(
        buildings=tuple(accepted),
        generation={
            "source": "existing measured footprints/heights; conservative prisms, unknown heated contacts",
            "radius_m": radius,
            "groups": groups,
            "unsupported": rejected,
            "source_exclusions": prepared["excluded"],
            "contact_candidates": contacts,
        },
    )
    return spec.updated(context=context), {
        "neighbours": len(accepted),
        "unsupported": rejected,
        "contact_candidates": contacts,
        "verified_party_walls": sum(c["verified_eligible"] for c in contacts),
    }
