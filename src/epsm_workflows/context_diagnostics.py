"""Measured-neighbour shading and evidence-gated shared-wall diagnostics."""

from __future__ import annotations

import argparse
import math
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import duckdb
from shapely import STRtree, from_wkb, set_precision
from shapely.geometry import LineString
from shapely.ops import unary_union

from epsm_core import EnergyPlusRunner
from epsm_core.context import resolve_context, validate_context
from epsm_core.specs.context import ContextBuilding, ContextSpec
from epsm_core.specs.geometry import Footprint, PartyWall
from . import parameter_sensitivity as oat
from .campaign import atomic_json, file_sha256


def polygons(geometry):
    return list(geometry.geoms) if geometry.geom_type == "MultiPolygon" else [geometry]


def footprint(poly):
    return Footprint(
        exterior=tuple(poly.exterior.coords),
        holes=tuple(tuple(r.coords) for r in poly.interiors),
    )


def clean_context_polygon(polygon):
    """Remove sub-centimetre outline edges while retaining shape and area."""
    lengths = [
        LineString([a, b]).length
        for ring in (polygon.exterior, *polygon.interiors)
        for a, b in zip(ring.coords, list(ring.coords)[1:])
    ]
    if not lengths or min(lengths) >= 0.010001:
        return polygon, 0.0
    cleaned = polygon.simplify(0.01, preserve_topology=True)
    changed = polygon.symmetric_difference(cleaned).area
    if not cleaned.is_valid or cleaned.is_empty or changed > 0.01 * polygon.area:
        raise ValueError("Context outline cleanup exceeds 1% area bound")
    return cleaned, changed


def shading_groups(records):
    """Union overlapping roof-edge outlines, preserving a conservative height envelope."""
    if not records:
        return [], []
    originals = [from_wkb(bytes.fromhex(r["geometry_wkb_hex"])) for r in records]
    # Cleanup can create tiny overlaps between previously touching outlines. Merge
    # those envelopes until the final shading footprints are disjoint.
    components = [[i] for i in range(len(records))]
    while True:
        geoms = [
            set_precision(unary_union([originals[i] for i in ids]), 0.02)
            for ids in components
        ]
        tree = STRtree(geoms)
        parent = list(range(len(components)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i, geom in enumerate(geoms):
            for j in tree.query(geom, predicate="intersects"):
                j = int(j)
                if i < j and geom.intersection(geoms[j]).area > 1e-8:
                    parent[find(j)] = find(i)
        merged = {}
        for i, ids in enumerate(components):
            merged.setdefault(find(i), []).extend(ids)
        if len(merged) == len(components):
            break
        components = list(merged.values())
    groups = dict(enumerate(components))
    buildings = []
    source_ids = {}
    audit = []
    for indices in groups.values():
        ids = sorted(records[i]["building_id"] for i in indices)
        measured_base = min(records[i]["relative_base_m"] for i in indices)
        # EnergyPlus 24.2 can report zero-elevation shading caps as just below grade
        # after its geometry transform. Keep context-only surfaces 2 cm above that datum.
        base = max(0.02, measured_base)
        top = max(
            records[i]["relative_base_m"] + records[i]["height_m"] for i in indices
        )
        if top <= base:
            audit.append(
                {
                    "member_ids": ids,
                    "excluded_below_target_ground": True,
                    "measured_base_m": measured_base,
                    "top_m": top,
                }
            )
            continue
        union = unary_union([originals[i] for i in indices])
        original = union
        # Use precision-snapped copies only to find groups. Preserve the measured outline
        # in EnergyPlus geometry so snapping cannot push a neighbour into the target.
        changed_area = 0.0
        precision_grid_m = 0.0
        cleanup = []
        for part_index, polygon in enumerate(polygons(union)):
            polygon, changed = clean_context_polygon(polygon)
            cleanup.append({"part": part_index, "simplification_area_m2": changed})
            identifier = (
                "context-"
                + oat.diagnostics.digest(ids)[:16]
                + "-g"
                + oat.diagnostics.digest(polygon.wkb.hex())[:8]
                + f"-p{part_index}"
            )
            buildings.append(
                ContextBuilding(
                    identifier=identifier,
                    footprints=(footprint(polygon),),
                    height_m=top - base,
                    base_elevation_m=base,
                )
            )
            source_ids[identifier.rsplit("-p", 1)[0]] = ids
        audit.append(
            {
                "member_ids": ids,
                "merged_overlap": len(ids) > 1,
                "base_m": base,
                "measured_base_m": measured_base,
                "below_target_ground_clipped": measured_base < 0.02,
                "context_minimum_base_m": 0.02,
                "top_m": top,
                "area_m2": union.area,
                "original_area_m2": original.area,
                "precision_grid_m": precision_grid_m,
                "symmetric_difference_area_m2": changed_area,
                "short_edge_cleanup": cleanup,
            }
        )
    # Constructing Footprint records can expose small overlaps introduced by coordinate
    # snapping. Merge those published polygons too, or Core's strict context validator
    # will correctly reject them. Preserve the conservative outer height envelope.
    while True:
        shapes = [unary_union([f.polygon for f in b.footprints]) for b in buildings]
        tree = STRtree(shapes)
        parent = list(range(len(shapes)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i, shape in enumerate(shapes):
            for j in tree.query(shape, predicate="intersects"):
                j = int(j)
                if i < j and shape.intersection(shapes[j]).area > 1e-8:
                    parent[find(j)] = find(i)
        merged = {}
        for i, building in enumerate(buildings):
            merged.setdefault(find(i), []).append(building)
        if all(len(group) == 1 for group in merged.values()):
            break
        rebuilt = []
        for group in merged.values():
            ids = sorted(
                identifier
                for building in group
                for identifier in source_ids[building.identifier.rsplit("-p", 1)[0]]
            )
            base = min(building.base_elevation_m for building in group)
            top = max(
                building.base_elevation_m + building.height_m for building in group
            )
            combined = unary_union(
                [
                    polygon
                    for building in group
                    for polygon in [f.polygon for f in building.footprints]
                ]
            )
            for part_index, polygon in enumerate(polygons(combined)):
                polygon, _changed = clean_context_polygon(polygon)
                identifier = (
                    "context-"
                    + oat.diagnostics.digest(ids)[:16]
                    + "-g"
                    + oat.diagnostics.digest(polygon.wkb.hex())[:8]
                    + f"-p{part_index}"
                )
                rebuilt.append(
                    ContextBuilding(
                        identifier=identifier,
                        footprints=(footprint(polygon),),
                        height_m=top - base,
                        base_elevation_m=base,
                    )
                )
                source_ids[identifier.rsplit("-p", 1)[0]] = ids
        buildings = rebuilt
    return buildings, audit


def contact_evidence(spec, record, target_outline):
    """Never treat roof-edge proximity or building function as reviewed heated contact."""
    neighbour = from_wkb(bytes.fromhex(record["geometry_wkb_hex"]))
    result = []
    for facade in spec.geometry.facades():
        line = LineString([facade.start_m, facade.end_m])
        shared = line.intersection(neighbour.boundary).length
        if shared <= 0.1:
            continue
        full = shared >= facade.length_m - 1e-5
        aboveground_top = max(facade.storey_elevations_m)
        vertical = (
            record.get("relative_base_m", math.inf) <= 0.01
            and record.get("relative_eave_top_m", -math.inf) >= aboveground_top - 0.01
        )
        reasons = []
        if target_outline != "Fasad" or record["outline"] != "Fasad":
            reasons.append("roof_edge_not_verified_wall_contact")
        if not full:
            reasons.append("partial_facade_contact_unsupported")
        if not vertical:
            reasons.append("neighbour_does_not_cover_full_height_or_height_unknown")
        if record.get("heated_contact_reviewed") is not True:
            reasons.append("heated_neighbour_contact_not_independently_reviewed")
        if spec.geometry.basement_depth_m:
            reasons.append("shared_basement_extent_unknown")
        result.append(
            {
                "facade_id": facade.facade_id,
                "neighbor_id": record["building_id"],
                "shared_line_m": shared,
                "facade_length_m": facade.length_m,
                "full_facade": full,
                "full_height": vertical,
                "verified_eligible": not reasons,
                "reasons": reasons,
                "neighbor_function": record["function"],
                "neighbor_eave_top_m": record.get("relative_eave_top_m"),
                "target_aboveground_top_m": aboveground_top,
            }
        )
    return result


def build_spec(row, state, weather, prepared, variant):
    spec = oat.frozen_spec(row, state, weather, 0.2)
    if variant.startswith("shading") or variant == "combined_verified":
        radius = 200 if variant == "shading_200" else 100
        records = [
            r
            for r in prepared["neighbours"]
            if r.get("shading_eligible") and r["distance_m"] <= radius
        ]
        buildings, groups = shading_groups(records)
        supported, unsupported = [], []
        for building in buildings:
            try:
                resolve_context(spec.geometry, ContextSpec(buildings=(building,)))
            except ValueError as exc:
                unsupported.append(
                    {"context_id": building.identifier, "reason": str(exc)}
                )
            else:
                supported.append(building)
        spec = spec.updated(
            context=ContextSpec(
                buildings=tuple(supported),
                generation={
                    "source": "measured EPSG:3006 footprints and promoted LiDAR heights",
                    "radius_m": radius,
                    "groups": groups,
                    "unsupported_outlines_excluded": unsupported,
                    "missing_or_overlapping_neighbours_recorded": True,
                },
            )
        )
        validate_context(spec.geometry, spec.context)
    if variant in ["party_verified", "combined_verified"]:
        contacts = [r for r in prepared["contacts"] if r["verified_eligible"]]
        walls = tuple(
            PartyWall(
                facade_id=r["facade_id"],
                neighbor_id=r["neighbor_id"],
                neighbor_conditioning="heated",
                contact_confirmed=True,
                assumption_source="Independent reviewed contact evidence recorded in preparation.json",
            )
            for r in contacts
        )
        spec = spec.updated(geometry=spec.geometry.updated(party_walls=walls))
    return spec


def prepare(rows, weather, publication, height_release, selection, root):
    path = publication / "building_results/kommunkod=1480/part.parquet"
    with duckdb.connect() as db:
        raw = db.execute(
            "SELECT building_id,geometry,insamlingslage,andamal1 FROM read_parquet(?)",
            [str(path)],
        ).fetchall()
        geoms = [from_wkb(bytes(r[1])) for r in raw]
        tree = STRtree(geoms)
        index = {r[0]: i for i, r in enumerate(raw)}
        neighbours = {}
        ids = set(r["building_id"] for r in rows)
        for row in rows:
            target = geoms[index[row["building_id"]]]
            candidates = sorted(
                int(i)
                for i in tree.query(target.buffer(200))
                if raw[int(i)][0] != row["building_id"]
                and target.distance(geoms[int(i)]) <= 200
            )
            neighbours[row["building_id"]] = candidates
            ids.update(raw[i][0] for i in candidates)
        db.execute("CREATE TEMP TABLE ids(building_id VARCHAR)")
        db.executemany("INSERT INTO ids VALUES (?)", [(i,) for i in sorted(ids)])
        tiles = db.execute(
            "SELECT DISTINCT owner_tile_id FROM read_parquet(?) JOIN ids USING(building_id)",
            [str(selection)],
        ).fetchall()
        files = [
            height_release / f"results/buildings/owner_tile_id={t}/part.parquet"
            for (t,) in tiles
        ]
        files = [p for p in files if p.is_file()]
        heights = db.execute(
            "SELECT building_id,height_status,representative_height_m,height_p90_m,eave_height_m,ridge_height_m,ground_elevation_m,roof_form FROM read_parquet(?,hive_partitioning=false,union_by_name=true) JOIN ids USING(building_id)",
            [[str(f) for f in files]],
        ).fetchall()
    hd = {
        r[0]: dict(
            zip(
                ["status", "representative", "p90", "eave", "ridge", "ground", "roof"],
                r[1:],
            )
        )
        for r in heights
    }
    prepared = {}
    for row in rows:
        target = geoms[index[row["building_id"]]]
        spec = oat.frozen_spec(row, "existing", weather, 0.2)
        model_target = unary_union([p.footprint.polygon for p in spec.geometry.parts])
        target_height = hd.get(row["building_id"], {})
        ground = target_height.get("ground")
        if ground is None or not math.isfinite(ground):
            raise ValueError("Target ground elevation missing")
        records = []
        contacts = []
        for i in neighbours[row["building_id"]]:
            bid, _, outline, function = raw[i]
            h = hd.get(bid, {})
            rec = {
                "building_id": bid,
                "geometry_wkb_hex": geoms[i].wkb.hex(),
                "outline": outline,
                "function": function,
                "distance_m": target.distance(geoms[i]),
                "height_source": h,
                "shading_eligible": False,
                "excluded_reasons": [],
            }
            if not geoms[i].is_valid or geoms[i].geom_type not in [
                "Polygon",
                "MultiPolygon",
            ]:
                rec["excluded_reasons"].append("invalid_footprint")
            valid = [
                float(h[k])
                for k in ["ridge", "p90", "representative"]
                if h.get(k) is not None and math.isfinite(h[k]) and h[k] > 0
            ]
            if h.get("status") != "determined" or not valid:
                rec["excluded_reasons"].append("measured_height_missing")
            if h.get("ground") is None or not math.isfinite(h["ground"]):
                rec["excluded_reasons"].append("ground_elevation_missing")
            if not rec["excluded_reasons"]:
                rec.update(height_m=max(valid), relative_base_m=h["ground"] - ground)
                eave = (
                    h.get("eave")
                    if h.get("roof") == "pitched_ridge"
                    else h.get("representative")
                )
                if eave is not None:
                    rec["relative_eave_top_m"] = rec["relative_base_m"] + eave
            overlap = geoms[i].intersection(model_target).area
            rec["target_overlap_m2"] = overlap
            if overlap > 1e-8:
                rec["excluded_reasons"].append(
                    "overlap_with_target_no_automatic_clipping"
                )
            rec["shading_eligible"] = not rec["excluded_reasons"]
            records.append(rec)
            contacts.extend(contact_evidence(spec, rec, row["outline"]))
        prepared[row["building_id"]] = {
            "target_ground_elevation_m": ground,
            "source_target_geometry_sha256": oat.diagnostics.digest(target.wkb.hex()),
            "model_target_geometry_sha256": oat.diagnostics.digest(
                model_target.wkb.hex()
            ),
            "target_outline": row["outline"],
            "neighbours": records,
            "contacts": contacts,
            "verified_party_wall_count": sum(c["verified_eligible"] for c in contacts),
        }
    audit = {
        "publication_release_sha256": file_sha256(
            publication / "release_manifest.json"
        ),
        "footprints_sha256": file_sha256(path),
        "height_manifest_sha256": file_sha256(height_release / "manifest.json"),
        "height_selection_sha256": file_sha256(selection),
        "height_partitions": {str(p.resolve()): file_sha256(p) for p in files},
        "workflow_sha256": file_sha256(__file__),
        "package_sources": oat.diagnostics.package_sources(),
        "assumptions": {
            "radius_m": [100, 200],
            "height": "flat prism to largest valid ridge/p90/representative height",
            "ground": "measured neighbour ground minus measured target ground",
            "overlapping_context_outlines": "union group with lowest base and highest top, conservative shading envelope",
            "overlap_target": "exclude and report; do not clip or modify target",
            "party_wall": "only independently reviewed full facade, full height, heated contact; basement extent must also be known",
            "fixed_infiltration": True,
            "solar_shading_factor": 1,
            "operating_assumptions_unchanged": True,
            "fresh_holdout": False,
        },
        "source_weather": weather,
    }
    atomic_json(root / "preparation.json", prepared)
    atomic_json(root / "audit.json", audit)
    return prepared


def execute(job):
    row, state, variant, prepared, weather, executable, output = job
    directory = (
        Path(output)
        / "runs"
        / oat.diagnostics.digest((row["building_id"], state, variant, prepared))[:24]
    )
    receipt = directory / "receipt.json"
    if receipt.exists():
        return oat.pilot._read(receipt)
    directory.mkdir(parents=True, exist_ok=True)
    result = {
        "building_id": row["building_id"],
        "state": state,
        "variant": variant,
        "status": "failed",
        "compiled": False,
    }
    try:
        spec = build_spec(row, state, weather, prepared, variant)
        runner = EnergyPlusRunner(executable)
        model, report = runner.compiler.compile(spec)
        result.update(
            compiled=True,
            spec_sha256=spec.physical_hash(),
            epjson_sha256=oat.diagnostics.digest(model),
            weather_sha256=spec.climate.weather_sha256,
            conditioned_area_m2=spec.geometry.conditioned_floor_area_m2,
            conditioned_volume_m3=spec.geometry.conditioned_volume_m3,
            context_building_count=len(spec.context.buildings),
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


def summarize(rows, results, baseline, root):
    base = {(r["building_id"], r["state"]): r for r in baseline}
    by_id = {r["building_id"]: r for r in rows}
    paired = []
    for r in results:
        old = base[r["building_id"], r["state"]]
        if r["status"] != "complete" or old["status"] != "complete":
            continue
        row = by_id[r["building_id"]]
        observed = row["heat_kwh"] / row["atemp_m2"]
        for loss in [0, 5, 10]:
            paired.append(
                {
                    "building_id": r["building_id"],
                    "property_key": row["property_key"],
                    "state": r["state"],
                    "variant": r["variant"],
                    "loss": loss,
                    "observed": observed,
                    "heating_change_kwh_m2": r["demand_kwh_m2"] - old["demand_kwh_m2"],
                    "baseline_error": old["demand_kwh_m2"] + loss - observed,
                    "context_error": r["demand_kwh_m2"] + loss - observed,
                }
            )
    summaries = []
    for variant in sorted({r["variant"] for r in results}):
        for state in oat.pilot.STATES:
            for loss in [0, 5, 10]:
                sample = [
                    r
                    for r in paired
                    if r["variant"] == variant
                    and r["state"] == state
                    and r["loss"] == loss
                ]
                if not sample:
                    continue
                summaries.append(
                    {
                        "variant": variant,
                        "state": state,
                        "loss": loss,
                        "baseline": oat.metrics(
                            [r["baseline_error"] for r in sample],
                            [r["observed"] for r in sample],
                        ),
                        "context": oat.metrics(
                            [r["context_error"] for r in sample],
                            [r["observed"] for r in sample],
                        ),
                        "improved": sum(
                            abs(r["context_error"]) < abs(r["baseline_error"]) - 1e-7
                            for r in sample
                        ),
                        "worsened": sum(
                            abs(r["context_error"]) > abs(r["baseline_error"]) + 1e-7
                            for r in sample
                        ),
                    }
                )
    atomic_json(root / "per-building.json", paired)
    atomic_json(root / "summary.json", summaries)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--energyplus", required=True)
    p.add_argument("--publication", default="../epc_join/outputs/releases/2.0.0")
    p.add_argument(
        "--height-release",
        default="../footprint_height_extraction/outputs/sweden/harmonized/1.0.0",
    )
    p.add_argument(
        "--height-selection",
        default="../footprint_height_extraction/outputs/sweden/inventory/selections/gothenburg.buildings.parquet",
    )
    p.add_argument("--workers", type=int, default=8)
    a = p.parse_args()
    source = Path(a.source)
    root = Path(a.output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    rows = oat.pilot._read(source / "cohort.json")
    weather = oat.pilot._read(source / "audit.json")["source_weather"]
    atomic_json(root / "cohort.json", rows)
    prepared = prepare(
        rows,
        weather,
        Path(a.publication).resolve(),
        Path(a.height_release).resolve(),
        Path(a.height_selection).resolve(),
        root,
    )
    (root / "workflow_source_frozen.py").write_text(Path(__file__).read_text())
    baseline = [
        r
        for r in oat.pilot._read(source / "results.json")
        if r["parameter"] == "baseline"
    ]
    for r in baseline:
        row = next(row for row in rows if row["building_id"] == r["building_id"])
        if (
            oat.frozen_spec(row, r["state"], weather, 0.2).physical_hash()
            != r["spec_sha256"]
        ):
            raise ValueError("Frozen baseline changed")
    atomic_json(root / "baseline.json", baseline)
    jobs = []
    context_exclusions = []
    for row in rows:
        # EnergyPlus 24.2 emits a new strict-gate centroid warning when surrounding
        # shading is combined with the current below-grade basement representation.
        # Keep the measured geometry, but do not count those simulations as accepted.
        if row["heated_basement_floors"]:
            context_exclusions.append(
                {
                    "building_id": row["building_id"],
                    "reason": "EnergyPlus 24.2 CalcSurfaceCentroid warning with context shading and below-grade basement surfaces",
                }
            )
            continue
        variants = ["shading_100", "shading_200"]
        if prepared[row["building_id"]]["verified_party_wall_count"]:
            variants += ["party_verified", "combined_verified"]
        jobs.extend(
            (row, s, v, prepared[row["building_id"]], weather, a.energyplus, str(root))
            for s in oat.pilot.STATES
            for v in variants
        )
    preflight = []
    for row, state, variant, prep, *_ in jobs:
        spec = build_spec(row, state, weather, prep, variant)
        EnergyPlusRunner(a.energyplus).compiler.compile(spec)
        preflight.append(
            {
                "building_id": row["building_id"],
                "state": state,
                "variant": variant,
                "compiled": True,
            }
        )
    atomic_json(root / "precompile.json", preflight)
    atomic_json(root / "context-exclusions.json", context_exclusions)
    smoke = next(j for j in jobs if j[1] == "existing" and j[2] == "shading_100")
    trial = execute(smoke)
    atomic_json(root / "smoke.json", trial)
    if trial["status"] != "complete":
        raise RuntimeError("Context smoke failed; inspect retained diagnostics")
    start = time.monotonic()
    with ProcessPoolExecutor(max_workers=a.workers) as pool:
        results = []
        for r in pool.map(execute, jobs):
            results.append(r)
            if len(results) % 12 == 0:
                print(f"{len(results)}/{len(jobs)} context cases", flush=True)
    atomic_json(root / "results.json", results)
    summarize(rows, results, baseline, root)
    atomic_json(
        root / "runtime.json",
        {
            "simulation_wall_seconds": time.monotonic() - start,
            "cases": len(results),
            "accepted": sum(r["status"] == "complete" for r in results),
        },
    )


if __name__ == "__main__":
    main()
