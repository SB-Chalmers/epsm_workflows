"""Matched geometry screening. Run with python -m epsm_workflows.geometry_study."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import html
import json
import shutil
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

from epsm_core import (
    BalconySpec,
    BuildingSpec,
    EnergyPlusRunner,
    Footprint,
    GeometrySpec,
    OverhangSpec,
    RoofSpec,
    ScheduleSpec,
    WindowLayoutSpec,
    ZoningSpec,
    climate_from_epw,
)


def variants(base):
    """Every candidate names its matched reference; operation changes stay separate."""
    if base.use.occupancy_category != "residential":
        raise ValueError("This study uses a residential apartment mix; supply a residential base")
    if base.envelope.dwelling_partition is None or base.envelope.common_partition is None:
        raise ValueError(
            "Supply explicit dwelling/common partition constructions in the study base"
        )
    simple = base.updated(
        geometry=base.geometry.updated(
            zoning=ZoningSpec(mode="per_storey"),
            roof=RoofSpec(),
            parts=tuple(p.updated(roof=None) for p in base.geometry.parts),
        ),
        facade=base.facade.updated(
            layout=WindowLayoutSpec(),
            reveal_depth_m=0,
            overhang=OverhangSpec(),
            balconies=(),
            solar_shading_factor=1,
            overrides=(),
        ),
    )
    repeated = simple.updated(facade=simple.facade.updated(layout=WindowLayoutSpec(mode="wwr")))
    reveals = repeated.updated(facade=repeated.facade.updated(reveal_depth_m=0.2))
    overhangs = repeated.updated(facade=repeated.facade.updated(overhang=OverhangSpec(depth_m=0.6)))
    # Place one open balcony per facade on upper occupied storeys, fitted to short edges.
    from epsm_core.geometry import facade_edges

    balconies = []
    for p in base.geometry.parts:
        for fid, a, b in facade_edges(p.footprint.polygon, p.identifier):
            length = float(np.linalg.norm(np.array(b) - a))
            if length >= 3:
                width = min(4, length - 0.4)
                balconies.append(
                    BalconySpec(
                        facade_id=fid,
                        storeys=tuple(range(1, p.floors)),
                        start_m=(length - width) / 2,
                        width_m=width,
                        depth_m=1.5,
                    )
                )
    balcony = repeated.updated(facade=repeated.facade.updated(balconies=tuple(balconies)))
    shade = repeated.updated(
        facade=repeated.facade.updated(
            reveal_depth_m=0.2, overhang=OverhangSpec(depth_m=0.6), balconies=tuple(balconies)
        )
    )
    flat = repeated.updated(
        geometry=repeated.geometry.updated(roof=RoofSpec(topology="cold_attic"))
    )
    roofs = {
        s: flat.updated(geometry=flat.geometry.updated(roof=RoofSpec(shape=s)))
        for s in ("mono_pitch", "gable", "hip")
    }
    apt = repeated.updated(
        geometry=repeated.geometry.updated(
            zoning=ZoningSpec(
                mode="apartments",
                on_failure="per_storey",
                apartments=repeated.geometry.zoning.apartments.updated(
                    common_heating_setpoint_c=18
                ),
            ),
        )
    )
    common = base.common_use or base.use.updated(
        heating_setpoint=ScheduleSpec.constant(18),
        occupant_density_per_m2=0,
        equipment_gain_w_m2=0,
        lighting_gain_w_m2=0,
        dhw_kwh_m2_year=0,
    )
    apt = apt.updated(common_use=common)
    equal = apt.updated(common_use=common.updated(heating_setpoint=base.use.heating_setpoint))
    combined = shade.updated(
        geometry=apt.geometry.updated(roof=RoofSpec(shape="hip")),
        envelope=flat.envelope,
        common_use=common,
    )
    out = {
        "simple": (simple, None),
        "whole": (
            simple.updated(
                geometry=simple.geometry.updated(zoning=ZoningSpec(mode="whole_building"))
            ),
            "simple",
        ),
        "windows": (repeated, "simple"),
        "reveals": (reveals, "windows"),
        "overhangs": (overhangs, "windows"),
        "balconies": (balcony, "windows"),
        "combined_shading": (shade, "windows"),
        "flat_attic": (flat, "windows"),
        **{s: (b, "flat_attic") for s, b in roofs.items()},
        "apartments_equal": (equal, "windows"),
        "apartments_lower": (apt, "apartments_equal"),
        "combined": (combined, "windows"),
        "without_reveals": (
            combined.updated(facade=combined.facade.updated(reveal_depth_m=0)),
            "combined",
        ),
        "without_overhangs": (
            combined.updated(facade=combined.facade.updated(overhang=OverhangSpec())),
            "combined",
        ),
        "without_balconies": (
            combined.updated(facade=combined.facade.updated(balconies=())),
            "combined",
        ),
        "without_roof": (
            combined.updated(
                geometry=combined.geometry.updated(roof=RoofSpec(topology="cold_attic"))
            ),
            "combined",
        ),
        "without_layout": (
            combined.updated(
                geometry=combined.geometry.updated(zoning=ZoningSpec(mode="per_storey"))
            ),
            "combined",
        ),
    }
    for wwr in (0.1, 0.75):
        label = f"wwr_{int(wwr * 100)}"
        out[label + "_simple"] = (
            simple.updated(facade=simple.facade.updated(default_wwr=wwr)),
            None,
        )
        out[label + "_windows"] = (
            repeated.updated(facade=repeated.facade.updated(default_wwr=wwr)),
            label + "_simple",
        )
    return out


def make_manifest(base, weather, stage="screen", full_environment_grid=False):
    shapes = {
        "rectangle": Footprint.rectangle(30, 20),
        "l": Footprint.l_shape(30, 30, 15, 15),
        "u": Footprint.u_shape(30, 30, 14, 20),
        "courtyard": Footprint(
            exterior=((0, 0), (30, 0), (30, 30), (0, 30)),
            holes=(((8, 8), (22, 8), (22, 22), (8, 22)),),
        ),
    }
    records = []
    for climate_index, (climate_name, epw) in enumerate(weather.items()):
        climate = climate_from_epw(epw, calendar_year=base.climate.calendar_year)
        for orientation_index, orientation in enumerate((0, 45, 90, 135)):
            for shape_index, (shape, footprint) in enumerate(shapes.items()):
                if stage == "smoke" and (
                    climate_name != next(iter(weather)) or orientation != 0 or shape != "rectangle"
                ):
                    continue
                b = base.updated(
                    climate=climate,
                    geometry=GeometrySpec.from_footprint(
                        footprint.scaled_to_area(600),
                        storey_elevations_m=(0, 3, 6, 9),
                        orientation_deg=orientation,
                    ),
                    facade=base.facade.updated(default_wwr=0.3),
                )
                for name, (spec, reference) in variants(b).items():
                    # Balanced detail screening: every climate sees all orientations and shapes.
                    # Baseline/window pairs still cover the complete environmental grid.
                    selected = shape_index == (climate_index + orientation_index) % len(shapes)
                    if (
                        stage == "screen"
                        and not full_environment_grid
                        and not selected
                        and name not in ("simple", "windows")
                    ):
                        continue
                    # Ablations use all climates/orientations on the rectangular shape.
                    if name.startswith("without_") and shape != "rectangle":
                        continue
                    prefix = f"{climate_name}_{orientation}_{shape}"
                    records.append(
                        dict(
                            case_id=prefix + "_" + name,
                            climate=climate_name,
                            orientation=orientation,
                            footprint=shape,
                            variant=name,
                            reference=prefix + "_" + reference if reference else None,
                            retain_sql=stage == "smoke"
                            or (climate_index == 0 and orientation == 0 and shape == "rectangle"),
                            spec=spec.model_dump(mode="json"),
                        )
                    )
    return dict(
        version=1,
        stage=stage,
        full_environment_grid=full_environment_grid,
        comparison="model-to-model; not measured-building validation",
        assumptions=dict(
            gross_floor_area_m2=1800,
            occupied_storeys=3,
            storey_height_m=3,
            WWR=0.3,
            reveal_m=0.2,
            overhang_m=0.6,
            balcony_depth_m=1.5,
            common_heating_c=18,
            apartment_mix="illustrative residential-only 40/40/15/5",
        ),
        cases=records,
    )


def _hash(data):
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def run_case(case, root, energyplus, timeout):
    path = root / "runs" / case["case_id"]
    spec = BuildingSpec.model_validate(case["spec"])
    runner = EnergyPlusRunner(energyplus)
    expected = runner.compiler.run_id(spec)
    marker = path / "study-result.json"
    if marker.exists():
        saved = json.loads(marker.read_text())
        if saved.get("run_id") == expected:
            return saved
        raise ValueError(f"Refusing stale study directory: {path}; choose a fresh output directory")
    if path.exists() and any(path.iterdir()):
        raise ValueError(f"Incomplete run directory {path}; preserve it and choose a fresh output")
    start = time.perf_counter()
    row = {k: v for k, v in case.items() if k != "spec"}
    row.update(run_id=expected, status="failed", fallback=False)
    phase = "compilation"
    try:
        t = time.perf_counter()
        _, report = runner.compiler.compile(spec)
        row["compile_seconds"] = time.perf_counter() - t
        row["layout"] = report.data.get("layout", {})
        row["zoning"] = report.data.get("zoning", {})
        row["fallback"] = bool(row["zoning"].get("substituted")) or any(
            p["status"] != "generated" for p in row["layout"].get("parts", [])
        )
        row.update(
            zone_count=len(report.data["zones"]),
            surface_count=report.data["validation"]["surface_count"],
            window_count=report.data["validation"]["opening_count"],
        )
        phase = "simulation_or_results"
        result = runner.run(spec, path, timeout_seconds=timeout)
        row.update(status="passed", **result.scalars)
        row["simulation_seconds"] = result.scalars["simulation_seconds"]
        np.savez_compressed(
            path / "study-hourly.npz",
            time_axis=result.time_axis,
            heating_kwh=result.hourly["space_heating_demand_kwh"],
            cooling_kwh=result.hourly["cooling_demand_kwh"],
            dhw_kwh=result.hourly["dhw_load_kwh"],
            transmitted_solar_kwh=np.sum(
                [
                    v / 3.6e6
                    for k, v in result.hourly.items()
                    if k.startswith("Enclosure Windows Total Transmitted Solar Radiation Energy::")
                ],
                axis=0,
            ),
        )
        for name in ("space_heating_demand", "cooling_demand"):
            hourly = result.hourly[name + "_kwh"]
            row[name + "_peak_hourly_kw"] = float(hourly.max())
            months = result.time_axis.astype("datetime64[M]")
            row[name + "_monthly_kwh"] = [
                float(hourly[months == month].sum()) for month in np.unique(months)
            ]
        solar = [
            v / 3.6e6
            for k, v in result.hourly.items()
            if k.startswith("Enclosure Windows Total Transmitted Solar Radiation Energy::")
        ]
        row["transmitted_solar_kwh"] = float(np.sum(solar)) if solar else 0.0
    except Exception as exc:
        row["error"] = str(exc)
        row["failure_stage"] = phase
    # Preserve the SQL evidence losslessly without retaining hundreds of large databases.
    sql = path / "eplusout.sql"
    if sql.exists() and (case.get("retain_sql", True) or row["status"] != "passed"):
        with (
            sql.open("rb") as source,
            gzip.open(path / "eplusout.sql.gz", "wb", compresslevel=1) as target,
        ):
            shutil.copyfileobj(source, target)
        sql.unlink()
        row["sql_artifact"] = "eplusout.sql.gz"
    elif sql.exists():
        sql.unlink()
        row["sql_artifact"] = "compact study-hourly.npz; full SQL retained for representative cases"
    # Hard-link immutable copies of viewer libraries within this report, avoiding per-case duplication.
    import os

    preview = path / "preview"
    assets = root / "assets"
    assets.mkdir(exist_ok=True)
    for asset in preview.glob("*") if preview.exists() else ():
        if asset.name in ("model.json", "index.html") or not asset.is_file():
            continue
        shared = assets / asset.name
        try:
            with shared.open("xb") as dest:
                dest.write(asset.read_bytes())
        except FileExistsError:
            pass
        asset.unlink()
        os.link(shared, asset)
    row["total_seconds"] = time.perf_counter() - start
    path.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(row, indent=2, allow_nan=False))
    return row


METRICS = (
    "space_heating_demand_kwh",
    "cooling_demand_kwh",
    "space_heating_demand_peak_hourly_kw",
    "cooling_demand_peak_hourly_kw",
    "transmitted_solar_kwh",
    "compile_seconds",
    "simulation_seconds",
    "total_seconds",
    "surface_count",
    "window_count",
    "zone_count",
)


def report_results(root, manifest, rows):
    byid = {r["case_id"]: r for r in rows}
    comparisons = []
    for row in rows:
        ref = byid.get(row.get("reference"))
        if not ref:
            continue
        comparison = dict(
            case_id=row["case_id"],
            reference=ref["case_id"],
            variant=row["variant"],
            status="passed" if row["status"] == ref["status"] == "passed" else "unavailable",
            fallback=row.get("fallback", False) or ref.get("fallback", False),
        )
        if comparison["status"] == "passed":
            for key in METRICS:
                a, b = ref[key], row[key]
                comparison[key + "_difference"] = b - a
                comparison[key + "_difference_pct"] = (
                    100 * (b - a) / a if abs(a) > 1e-9 else (0 if abs(b) < 1e-9 else None)
                )
            for signal in ("space_heating_demand", "cooling_demand"):
                key = signal + "_monthly_kwh"
                for month, (a, b) in enumerate(zip(ref[key], row[key]), 1):
                    prefix = f"{signal}_month_{month:02d}_kwh"
                    comparison[prefix + "_difference"] = b - a
                    comparison[prefix + "_difference_pct"] = (
                        100 * (b - a) / a if abs(a) > 1e-9 else (0 if abs(b) < 1e-9 else None)
                    )
        comparisons.append(comparison)
    (root / "results.json").write_text(json.dumps(rows, indent=2))
    (root / "comparisons.json").write_text(json.dumps(comparisons, indent=2))
    for filename, data in (("results.csv", rows), ("comparisons.csv", comparisons)):
        fields = sorted({k for r in data for k, v in r.items() if not isinstance(v, (dict, list))})
        with (root / filename).open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(data)
    monthly = []
    for row in rows:
        if row["status"] != "passed":
            continue
        for month, (heating, cooling) in enumerate(
            zip(row["space_heating_demand_monthly_kwh"], row["cooling_demand_monthly_kwh"]), 1
        ):
            monthly.append(
                dict(
                    case_id=row["case_id"],
                    month=month,
                    heating_kwh=heating,
                    cooling_kwh=cooling,
                    fallback=row.get("fallback", False),
                )
            )
    with (root / "monthly.csv").open("w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=("case_id", "month", "heating_kwh", "cooling_kwh", "fallback")
        )
        writer.writeheader()
        writer.writerows(monthly)
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    valid = [r for r in comparisons if r["status"] == "passed" and not r["fallback"]]
    fig, ax = plt.subplots(figsize=(11, 6))
    variants = sorted({r["variant"] for r in valid})
    for i, v in enumerate(variants):
        vals = [
            r["space_heating_demand_kwh_difference_pct"]
            for r in valid
            if r["variant"] == v and r["space_heating_demand_kwh_difference_pct"] is not None
        ]
        if vals:
            ax.scatter(vals, [i] * len(vals), s=18, alpha=0.55)
    ax.set_yticks(range(len(variants)), variants)
    ax.axvline(0, color="gray", lw=0.8)
    ax.set_xlabel("Annual heating difference from matched reference (%)")
    ax.set_title("Geometry screening — model comparisons, not measured validation")
    fig.tight_layout()
    fig.savefig(root / "heating-comparison.png", dpi=150)
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(9, 5))
    for v in variants:
        subset = [r for r in valid if r["variant"] == v]
        ax.scatter(
            [r["simulation_seconds_difference"] for r in subset],
            [r["space_heating_demand_kwh_difference_pct"] for r in subset],
            label=v,
            s=20,
        )
    ax.set_xlabel("Simulation runtime difference (seconds)")
    ax.set_ylabel("Annual heating difference (%)")
    if variants:
        ax.legend(fontsize=6, ncol=2)
    fig.tight_layout()
    fig.savefig(root / "runtime-tradeoff.png", dpi=150)
    plt.close(fig)
    counts = {status: sum(r["status"] == status for r in rows) for status in ("passed", "failed")}
    layout_cases = [r for r in rows if r.get("layout", {}).get("parts")]
    summary = dict(
        total=len(rows),
        **counts,
        fallbacks=sum(r.get("fallback", False) for r in rows),
        apartment_layout_attempts=len(layout_cases),
        apartment_layout_fallback_rate=(
            sum(r.get("fallback", False) for r in layout_cases) / len(layout_cases)
        )
        if layout_cases
        else 0,
        failure_rate=counts["failed"] / len(rows) if rows else 0,
        fallback_rate=sum(r.get("fallback", False) for r in rows) / len(rows) if rows else 0,
        manifest_sha256=_hash(manifest),
        interpretation="Exploratory model comparisons; no empirical accuracy claims",
    )
    (root / "summary.json").write_text(json.dumps(summary, indent=2))
    tr = []
    for r in sorted(rows, key=lambda x: x["case_id"]):
        name = html.escape(r["case_id"])
        preview = f"runs/{r['case_id']}/preview/index.html"
        tr.append(
            f'<tr><td><a href="{preview}">{name}</a></td><td>{r["status"]}</td><td>{r.get("fallback", False)}</td><td>{r.get("space_heating_demand_kwh", 0):.1f}</td><td>{r.get("simulation_seconds", 0):.2f}</td><td>{html.escape(r.get("error", ""))}</td></tr>'
        )
    (root / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>Geometry screening</title><style>body{font:15px system-ui;margin:32px;max-width:1400px}table{border-collapse:collapse}td,th{padding:8px;border:1px solid #ddd}img{max-width:100%}</style><h1>Geometry screening</h1><p>Matched model comparisons; not measured-building validation. All failed and fallback cases are retained. Serve this report over HTTP to use linked 3D previews.</p><pre>'
        + html.escape(json.dumps(summary, indent=2))
        + '</pre><p><a href="manifest.json">Frozen manifest</a> · <a href="results.csv">Results CSV</a> · <a href="comparisons.csv">Comparisons CSV</a> · <a href="monthly.csv">Monthly CSV</a></p><img src="heating-comparison.png"><img src="runtime-tradeoff.png"><table><tr><th>Case / geometry</th><th>Status</th><th>Layout fallback</th><th>Heating kWh</th><th>Simulation seconds</th><th>Failure</th></tr>'
        + "".join(tr)
        + "</table>"
    )
    return summary


def run_study(manifest, root, energyplus=None, workers=3, timeout=1800):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    frozen = root / "manifest.json"
    if frozen.exists() and _hash(json.loads(frozen.read_text())) != _hash(manifest):
        raise ValueError("Output directory contains a different manifest")
    frozen.write_text(json.dumps(manifest, indent=2))
    rows = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(run_case, c, root, energyplus, timeout): c for c in manifest["cases"]
        }
        for f in as_completed(futures):
            row = f.result()
            rows.append(row)
            print(f"{len(rows)}/{len(futures)} {row['case_id']}: {row['status']}", flush=True)
            (root / "progress.json").write_text(json.dumps(rows, indent=2))
    return report_results(root, manifest, rows)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", required=True, type=Path)
    p.add_argument("--weather", action="append", required=True, help="label=/path/to/weather.epw")
    p.add_argument("--stage", choices=("smoke", "screen"), default="smoke")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--energyplus")
    p.add_argument("--workers", type=int, default=3)
    p.add_argument("--manifest-only", action="store_true")
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--full-environment-grid", action="store_true")
    args = p.parse_args()
    base = BuildingSpec.model_validate_json(args.base.read_text())
    weather = dict(v.split("=", 1) for v in args.weather)
    manifest = make_manifest(base, weather, args.stage, args.full_environment_grid)
    if args.manifest_only:
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    else:
        print(
            json.dumps(
                run_study(manifest, args.output, args.energyplus, args.workers, args.timeout),
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
