"""Deterministic bounded corpus generation from epc_join partitions and raw GeoPackage features."""

import hashlib
import heapq
import json
import sqlite3
from pathlib import Path

import pyarrow.parquet as pq
from shapely import from_wkb
from shapely.geometry import shape

from epsm_core.preparation import prepare_geometry


def select_corpus(root, out):
    manifest = []
    for municipality in ("0181", "1480", "2584"):
        path = root / f"building_results/kommunkod={municipality}/part.parquet"
        heaps = {name: [] for name in ("holes", "complex", "short", "roof_edge", "general")}
        for batch in pq.ParquetFile(path).iter_batches(
            batch_size=2048,
            columns=["building_id", "geometry", "insamlingslage", "geometry_repaired"],
        ):
            for row in batch.to_pylist():
                if not row["geometry"]:
                    continue
                p = from_wkb(row["geometry"])
                parts = (
                    list(p.geoms)
                    if p.geom_type == "MultiPolygon"
                    else [p]
                    if p.geom_type == "Polygon"
                    else []
                )
                if not parts:
                    continue
                rings = [r for p in parts for r in [p.exterior, *p.interiors]]
                coords = [list(r.coords) for r in rings]
                from math import dist

                short = any(dist(a, b) < 0.15 for r in coords for a, b in zip(r, r[1:]))
                groups = ["general"]
                if any(p.interiors for p in parts):
                    groups.append("holes")
                if sum(len(r) - 1 for r in coords) > 20:
                    groups.append("complex")
                if short:
                    groups.append("short")
                if row["insamlingslage"] == "Takkant":
                    groups.append("roof_edge")
                score = int(
                    hashlib.sha256(("epsm-geometry-1:" + row["building_id"]).encode()).hexdigest(),
                    16,
                )
                for group in groups:
                    heap = heaps[group]
                    item = (-score, row["building_id"], row)
                    if len(heap) < 100:
                        heapq.heappush(heap, item)
                    elif item > heap[0]:
                        heapq.heapreplace(heap, item)
        selected = {}
        for group in ("holes", "complex", "short", "roof_edge", "general"):
            count = 0
            for _, identifier, row in sorted(heaps[group], reverse=True):
                if identifier in selected:
                    continue
                selected[identifier] = (group, row)
                count += 1
                if len(selected) == 100 or (group != "general" and count == 20):
                    break
            if len(selected) == 100:
                break
        for identifier, (group, row) in sorted(selected.items()):
            result = prepare_geometry(from_wkb(row["geometry"]))
            artifact = result.artifact(
                source_crs="EPSG:3006",
                target_crs="EPSG:3006",
                transformation="identity; publication geometry, upstream repair flag retained",
            )
            artifact.update(
                building_id=identifier,
                municipality=municipality,
                selection_stratum=group,
                insamlingslage=row["insamlingslage"],
                upstream_geometry_repaired=row["geometry_repaired"],
                source_partition=str(path),
                visual_plausibility="pending",
                physical_accuracy="unverified",
            )
            (out / f"{municipality}-{identifier}.json").write_text(
                json.dumps(artifact, allow_nan=False)
            )
            manifest.append(
                {
                    k: artifact[k]
                    for k in (
                        "building_id",
                        "municipality",
                        "selection_stratum",
                        "insamlingslage",
                        "status",
                        "reasons",
                        "upstream_geometry_repaired",
                    )
                }
            )
        print(municipality, len(selected), flush=True)
    (out / "manifest.json").write_text(
        json.dumps(
            dict(profile="conservative-metric-1", seed="epsm-geometry-1", cases=manifest), indent=2
        )
    )


def attach_raw_geometry(root, out, gpkg):
    manifest = json.loads((out / "manifest.json").read_text())
    by_id = {r["building_id"]: r for r in manifest["cases"]}
    with sqlite3.connect("file:" + str(gpkg) + "?mode=ro", uri=True) as db:
        for m in ("0181", "1480", "2584"):
            ids = [k for k, v in by_id.items() if v["municipality"] == m]
            source = pq.read_table(
                root / f"building_results/kommunkod={m}/part.parquet",
                partitioning=None,
                columns=["building_id", "source_fid"],
                filters=[("building_id", "in", ids)],
            )
            lookup = {r["source_fid"]: r["building_id"] for r in source.to_pylist()}
            marks = ",".join("?" for _ in lookup)
            rows = db.execute(
                f"SELECT fid,geom,objektidentitet,insamlingslage FROM byggnad WHERE fid IN ({marks})",
                list(lookup),
            )
            for fid, blob, identifier, outline in rows:
                if identifier != lookup[fid]:
                    raise ValueError("Source feature identity mismatch")
                envelope = (blob[3] >> 1) & 7
                offset = 8 + {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}[envelope]
                p = from_wkb(blob[offset:])
                path = out / f"{m}-{identifier}.json"
                old = json.loads(path.read_text())
                result = prepare_geometry(p)
                artifact = result.artifact(
                    source_crs="EPSG:3006",
                    target_crs="EPSG:3006",
                    transformation="identity; raw GeoPackage geometry",
                )
                old.update(
                    artifact,
                    raw_source_path=str(gpkg),
                    raw_source_fid=fid,
                    raw_geometry_verified=True,
                )
                path.write_text(json.dumps(old, allow_nan=False))
                by_id[identifier].update(status=result.status, reasons=result.reasons)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print("Verified raw source geometry for", len(by_id), "cases")


def plot_corpus(out):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    items = []
    for path in sorted(out.glob("*.json")):
        if path.name in ("manifest.json", "review-selection.json", "visual-review.json"):
            continue
        data = json.loads(path.read_text())
        items.append((path, data))

        def draw(ax, data):
            for key, color, style in [
                ("source_geometry", "#d04a39", "-"),
                ("cleaned_geometry", "#168568", "--"),
            ]:
                g = shape(data[key])
                ps = list(g.geoms) if g.geom_type == "MultiPolygon" else [g]
                for p in ps:
                    if p.geom_type != "Polygon":
                        continue
                    for ring in (p.exterior, *p.interiors):
                        xs, ys = ring.xy
                        ax.plot(
                            xs,
                            ys,
                            style,
                            color=color,
                            lw=1.5,
                            label=key if ring is p.exterior else None,
                        )
            ax.set_aspect("equal")
            ax.ticklabel_format(useOffset=False, style="plain")
            ax.tick_params(labelsize=6)
            ax.set_title(
                f"{data['municipality']} · {data['selection_stratum']} · {data['status']}\n{data['building_id'][:12]} · {data['insamlingslage']}",
                fontsize=8,
            )

        fig, ax = plt.subplots(figsize=(5, 5))
        draw(ax, data)
        fig.tight_layout()
        fig.savefig(path.with_suffix(".svg"))
        plt.close(fig)
    review = [x for x in items if x[1]["status"] != "accepted"]
    # Ten per municipality, ordered to include diverse selection strata; plus every quarantine.
    selected = []
    for m in ("0181", "1480", "2584"):
        local = [x for x in items if x[1]["municipality"] == m]
        for group in ("holes", "complex", "short", "roof_edge", "general"):
            selected += [x for x in local if x[1]["selection_stratum"] == group][:2]
    seen = {x[0] for x in selected}
    selected += [x for x in review if x[0] not in seen]
    for index in range(0, len(selected), 6):
        fig, axes = plt.subplots(2, 3, figsize=(15, 10))
        for ax, (path, data) in zip(axes.flat, selected[index : index + 6]):
            draw(ax, data)
        for ax in list(axes.flat)[len(selected[index : index + 6]) :]:
            ax.axis("off")
        fig.suptitle(
            "Raw source (red) / cleaned (green dashed) — visual plausibility only", fontsize=12
        )
        fig.tight_layout()
        fig.savefig(out / f"review-{index // 6 + 1:02}.png", dpi=130)
        plt.close(fig)
    (out / "review-selection.json").write_text(
        json.dumps(
            [
                dict(file=p.name, building_id=d["building_id"], status=d["status"])
                for p, d in selected
            ],
            indent=2,
        )
    )
    print("Plots", len(items), "review selected", len(selected), "quarantine", len(review))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("publication_root", type=Path)
    parser.add_argument("raw_gpkg", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        raise FileExistsError(
            "Use a fresh corpus output directory; review records are immutable artifacts"
        )
    args.output.mkdir(parents=True, exist_ok=True)
    select_corpus(args.publication_root, args.output)
    attach_raw_geometry(args.publication_root, args.output, args.raw_gpkg)
    plot_corpus(args.output)
