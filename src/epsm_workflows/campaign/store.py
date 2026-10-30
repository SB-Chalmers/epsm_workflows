# SPDX-License-Identifier: MIT
"""Plan database, per-task result shards and the verified handoff database.

The plan is one immutable DuckDB file written when the package is built. Each
Slurm task publishes one shard; ``finalize`` merges them, checks acceptance and
removes the transient files so a campaign leaves a handful of entries behind.
"""
from __future__ import annotations

import json
import math
import os
import shutil
from pathlib import Path

import duckdb
import numpy as np

from ..records import atomic_json, canonical, digest, json_value
from .weather import WEATHER_FIELDS, epw_rows, file_sha256

TARGETS = ("space_heating_demand_kwh", "cooling_demand_kwh", "dhw_load_kwh")
SCALAR_FIELDS = (
    "space_heating_demand_kwh",
    "space_heating_demand_eui_kwh_m2",
    "peak_hourly_average_space_heating_demand_kw",
    "cooling_demand_kwh",
    "cooling_demand_eui_kwh_m2",
    "peak_hourly_average_cooling_demand_kw",
    "dhw_load_kwh",
    "dhw_load_eui_kwh_m2",
    "ideal_thermal_load_kwh",
    "ideal_thermal_load_eui_kwh_m2",
    "maximum_hourly_average_zone_temperature_c",
    "heating_unmet_zone_hours",
    "cooling_unmet_zone_hours",
    "zone_count",
    "surface_count",
    "shading_surface_count",
    "logical_window_count",
    "thermal_pane_count",
    "compile_seconds",
    "simulation_seconds",
)
REQUIRED_SCALARS = SCALAR_FIELDS[:10]
AXES = (
    "study_kind",
    "cohort",
    "site",
    "context",
    "weather_kind",
    "footprint_family",
    "geometry_source",
    "scenario_id",
    "measure_id",
)
CATEGORICAL_INPUTS = ("num_floors", "topology", "schedule_family", "schedule_shift")


def _plan_schema(db):
    db.execute("CREATE TABLE manifest(key VARCHAR PRIMARY KEY, value JSON NOT NULL)")
    axes = ",".join(f"{name} VARCHAR" for name in AXES)
    db.execute("CREATE TABLE buildings(building_key VARCHAR PRIMARY KEY, building_json JSON NOT NULL)")
    db.execute(
        f"""CREATE TABLE runs(
        ordinal INTEGER PRIMARY KEY, case_id VARCHAR UNIQUE NOT NULL,
        sample_id VARCHAR NOT NULL, group_id VARCHAR NOT NULL, weather_id VARCHAR NOT NULL,
        building_key VARCHAR NOT NULL, {axes}, pilot BOOLEAN NOT NULL, complexity DOUBLE NOT NULL,
        inputs_json JSON NOT NULL, metadata_json JSON NOT NULL)"""
    )
    fields = ",".join(f"{name} DOUBLE" for name in WEATHER_FIELDS)
    db.execute(
        f"CREATE TABLE weather(weather_id VARCHAR, t INTEGER, timestamp TIMESTAMP, {fields}, PRIMARY KEY(weather_id,t))"
    )
    db.execute(
        "CREATE TABLE tasks(phase VARCHAR, task_index INTEGER, task_id VARCHAR PRIMARY KEY, walltime_seconds INTEGER)"
    )
    db.execute(
        "CREATE TABLE task_members(task_id VARCHAR, ordinal INTEGER, position INTEGER, PRIMARY KEY(task_id,ordinal))"
    )


def create_plan(root, study, cases, catalog, manifest):
    """Freeze cases, weather and pilot tasks into one immutable planning database."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    final = root / "plan.duckdb"
    if final.exists():
        raise FileExistsError(final)
    pending = root / ".plan.pending.duckdb"
    for stale in (pending, Path(str(pending) + ".wal")):
        stale.unlink(missing_ok=True)
    bounds = getattr(study.section, "bounds", {}) or {}
    db = duckdb.connect(str(pending))
    try:
        _plan_schema(db)
        db.execute("BEGIN")
        db.execute("INSERT INTO manifest VALUES ('campaign', ?)", [canonical(manifest)])
        db.execute("INSERT INTO manifest VALUES ('study', ?)", [canonical(study.to_dict())])
        count = pilot_count = 0
        groups = {}
        buildings = set()
        input_types = {}
        for case in cases:
            axes = case.axes
            for key, (low, high) in bounds.items():
                value = case.inputs.get(key)
                if value is not None and (not math.isfinite(value) or not low <= value <= high):
                    raise ValueError(f"Sample outside bounds: {key}={value}")
            for key, value in case.inputs.items():
                kind = "VARCHAR" if isinstance(value, str) else "INTEGER" if isinstance(value, int) else "DOUBLE"
                if input_types.setdefault(key, kind) != kind:
                    raise ValueError(f"Mixed input types for {key}")
            if case.weather_id not in catalog:
                raise ValueError(f"Case names unfrozen weather {case.weather_id}")
            pilot = bool(axes.get("pilot", False))
            key = case.building_key
            if key not in buildings:
                db.execute("INSERT INTO buildings VALUES (?,?)", [key, canonical(case.building)])
                buildings.add(key)
            db.execute(
                f"INSERT INTO runs VALUES ({','.join('?' * (10 + len(AXES)))})",
                [
                    count,
                    case.case_id,
                    axes["sample_id"],
                    case.group_id,
                    case.weather_id,
                    key,
                    *(axes.get(name) for name in AXES),
                    pilot,
                    case.complexity,
                    canonical(case.inputs),
                    canonical(case.metadata),
                ],
            )
            groups[case.group_id] = groups.get(case.group_id, 0) + 1
            if pilot:
                task_id = f"pilot-{pilot_count:04d}"
                db.execute("INSERT INTO tasks VALUES ('pilot',?,?,7200)", [pilot_count, task_id])
                db.execute("INSERT INTO task_members VALUES (?,?,0)", [task_id, count])
                pilot_count += 1
            count += 1
        if not count:
            raise ValueError("The study produced no cases")
        for weather_id, item in sorted(catalog.items()):
            db.executemany(
                f"INSERT INTO weather VALUES ({','.join('?' * (3 + len(WEATHER_FIELDS)))})",
                list(epw_rows(weather_id, root / item["path"], item["calendar_year"])),
            )
        weather_rows = db.execute("SELECT count(*) FROM weather").fetchone()[0]
        db.execute("INSERT INTO manifest VALUES ('input_columns', ?)", [canonical(input_types)])
        db.execute("COMMIT")
        db.execute("CHECKPOINT")
    finally:
        db.close()
    os.replace(pending, final)
    sizes = {}
    for size in groups.values():
        sizes[str(size)] = sizes.get(str(size), 0) + 1
    receipt = {
        "campaign_id": digest(manifest),
        "runs": count,
        "buildings": len(buildings),
        "groups": len(groups),
        "group_sizes": sizes,
        "pilot_runs": pilot_count,
        "weather_rows": weather_rows,
        "weather_ids": sorted(catalog),
        "input_columns": input_types,
        "plan_sha256": file_sha256(final),
    }
    atomic_json(root / "plan.json", receipt)
    return receipt


def plan_info(root):
    return json.loads((Path(root) / "plan.json").read_text())


def validate_stock_sampling(root):
    """Re-check LHS strata and categorical balance from the frozen plan."""
    from .sampling import check_balance, check_strata
    from .study import FLOOR_CHOICES, SCHEDULE_FAMILIES, SHIFT_CHOICES, TOPOLOGIES

    db = duckdb.connect(str(Path(root) / "plan.duckdb"), read_only=True)
    try:
        rows = db.execute(
            "SELECT DISTINCT group_id, inputs_json, metadata_json FROM runs ORDER BY group_id"
        ).fetchall()
    finally:
        db.close()
    strata = {}
    for _group, inputs, metadata in rows:
        meta = json.loads(metadata)
        entry = strata.setdefault(meta["stratum_seed"], ([], [], meta.get("geometry_source")))
        entry[0].append(meta["lhs_unit"])
        entry[1].append(json.loads(inputs))
    categories = {
        "num_floors": FLOOR_CHOICES,
        "topology": TOPOLOGIES,
        "schedule_family": SCHEDULE_FAMILIES,
        "schedule_shift": SHIFT_CHOICES,
    }
    for units, inputs, source in strata.values():
        check_strata(units)
        # Real footprints keep their recorded storeys, so floors are not balanced there.
        skip = {"num_floors"} if source == "real" else set()
        check_balance(inputs, {k: v for k, v in categories.items() if k in inputs[0] and k not in skip})
    return {"strata": len(strata), "lhs_strata": "passed", "categories": "passed"}


# --- result shards -------------------------------------------------------------


def _shard_schema(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS identity(task_id VARCHAR PRIMARY KEY, campaign_id VARCHAR, runtime JSON)"
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS results(
        ordinal INTEGER PRIMARY KEY, case_id VARCHAR NOT NULL, run_id VARCHAR NOT NULL,
        scalars JSON NOT NULL, diagnostics JSON NOT NULL,
        heating DOUBLE[] NOT NULL, cooling DOUBLE[] NOT NULL, dhw DOUBLE[] NOT NULL,
        rc_heating DOUBLE[], rc_cooling DOUBLE[], rc_scalars JSON, rc_diagnostics JSON,
        elapsed_seconds DOUBLE NOT NULL, rc_seconds DOUBLE,
        completed_at TIMESTAMP DEFAULT current_timestamp)"""
    )


def open_shard(path, task_id, campaign_id, runtime):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = duckdb.connect(str(path))
    _shard_schema(db)
    identity = db.execute("SELECT task_id,campaign_id FROM identity").fetchall()
    if identity and identity != [(task_id, campaign_id)]:
        db.close()
        raise ValueError(f"Shard identity mismatch: {path}")
    if not identity:
        db.execute("INSERT INTO identity VALUES (?,?,?)", [task_id, campaign_id, canonical(runtime)])
    return db


def publish_shard(local_path, persistent_path):
    """Atomically publish one closed, checkpointed node-local DuckDB shard."""
    persistent_path = Path(persistent_path)
    persistent_path.parent.mkdir(parents=True, exist_ok=True)
    pending = persistent_path.with_name(f".{persistent_path.name}.{os.getpid()}.pending")
    pending.unlink(missing_ok=True)
    shutil.copyfile(local_path, pending)
    with pending.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(pending, persistent_path)
    directory = os.open(persistent_path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def validated_arrays(result):
    """Three finite non-negative 8760-hour kWh arrays reconciled with their annual sums."""
    arrays = [np.asarray(result.hourly[name], dtype=float) for name in TARGETS]
    for name, values in zip(TARGETS, arrays):
        if len(values) != 8760 or not np.isfinite(values).all() or np.any(values < -1e-8):
            raise ValueError(f"Invalid hourly result: {name}")
        if not np.isclose(values.sum(), result.scalars[name], rtol=0.001, atol=1e-6):
            raise ValueError(f"Annual/hourly disagreement: {name}")
    if "ideal_thermal_load_kwh" in result.hourly and not np.array_equal(
        arrays[0] + arrays[2], np.asarray(result.hourly["ideal_thermal_load_kwh"])
    ):
        raise ValueError("Derived ideal thermal load is not exact")
    return arrays


def task_rows(root, task_id):
    plan = duckdb.connect(str(Path(root) / "plan.duckdb"), read_only=True)
    try:
        return plan.execute(
            """SELECT r.ordinal, r.case_id, r.sample_id, r.weather_id, b.building_json
            FROM task_members m JOIN runs r USING(ordinal) JOIN buildings b USING(building_key)
            WHERE m.task_id=? ORDER BY m.position""",
            [task_id],
        ).fetchall()
    finally:
        plan.close()


def verified_shards(root, campaign_id):
    for path in sorted((Path(root) / "shards").glob("*.duckdb")):
        db = duckdb.connect(str(path), read_only=True)
        identity = db.execute("SELECT task_id,campaign_id FROM identity").fetchone()
        count = db.execute("SELECT count(*) FROM results").fetchone()[0]
        db.close()
        if not identity or identity[1] != campaign_id:
            raise ValueError(f"Foreign shard: {path}")
        yield path, identity[0], count


def status(root):
    root = Path(root)
    plan = duckdb.connect(str(root / "plan.duckdb"), read_only=True)
    try:
        tasks = dict(plan.execute("SELECT phase,count(*) FROM tasks GROUP BY phase").fetchall())
        assigned = plan.execute("SELECT count(*) FROM task_members").fetchone()[0]
        expected = plan.execute("SELECT count(*) FROM runs").fetchone()[0]
    finally:
        plan.close()
    complete = 0
    shards = root / "shards"
    for path in sorted(shards.glob("*.duckdb")) if shards.exists() else ():
        try:
            db = duckdb.connect(str(path), read_only=True)
            complete += db.execute("SELECT count(*) FROM results").fetchone()[0]
            db.close()
        except duckdb.IOException:
            receipt = root / "receipts" / f"{path.stem}.json"
            if receipt.exists():
                complete += json.loads(receipt.read_text())["complete"]
    diagnostics = root / "diagnostics"
    return {
        "expected": expected,
        "assigned": assigned,
        "complete": complete,
        "remaining": expected - complete,
        "tasks": tasks,
        "failures": len(list(diagnostics.glob("*.tar.gz"))) if diagnostics.exists() else 0,
        "paused": (root / "PAUSE").exists(),
    }


def prepare_production_tasks(root, study, max_tasks=5000):
    """Size production batches from pilot p95 runtime and balance them by complexity."""
    root = Path(root)
    execution = study.execution
    plan = duckdb.connect(str(root / "plan.duckdb"), read_only=True)
    try:
        existing = plan.execute("SELECT count(*) FROM tasks WHERE phase='production'").fetchone()[0]
        marker = plan.execute("SELECT value FROM manifest WHERE key='production_plan'").fetchone()
        if existing:
            if not marker:
                raise ValueError("Production tasks exist without an atomic plan marker")
            report = json.loads(marker[0]) if isinstance(marker[0], str) else marker[0]
            atomic_json(root / "production-plan.json", report)
            return report
        if marker:
            raise ValueError("Production plan marker exists without tasks")
        pilot_expected = plan.execute("SELECT count(*) FROM runs WHERE pilot").fetchone()[0]
        rows = plan.execute(
            "SELECT ordinal,complexity FROM runs WHERE NOT pilot ORDER BY complexity DESC,ordinal"
        ).fetchall()
    finally:
        plan.close()
    summary = status(root)
    elapsed = []
    for path in sorted((root / "shards").glob("pilot-*.duckdb")):
        shard = duckdb.connect(str(path), read_only=True)
        elapsed.extend(float(r[0]) for r in shard.execute("SELECT elapsed_seconds FROM results").fetchall())
        shard.close()
    if summary["paused"] or len(elapsed) != pilot_expected or summary["failures"]:
        raise ValueError(f"Pilot gate failed: {len(elapsed)}/{pilot_expected}; {summary}")
    if not rows:
        raise ValueError("No production cases to plan")
    p95 = float(np.percentile(elapsed, 95)) if elapsed else 600.0
    limit = execution.production_max_batch_size
    minimum_batch = math.ceil(len(rows) / max_tasks)
    runtime_batch = min(limit, math.floor(2700 / max(p95, 1)))
    batch_size = max(execution.production_min_batch_size, minimum_batch, runtime_batch)
    if batch_size > limit:
        raise ValueError(f"Production cannot fit the array limit with batches of at most {limit}: need {minimum_batch}")
    task_count = math.ceil(len(rows) / batch_size)
    bins = [{"load": 0.0, "ordinals": []} for _ in range(task_count)]
    for ordinal, weight in rows:
        target = min(bins, key=lambda item: (item["load"], len(item["ordinals"])))
        target["ordinals"].append(ordinal)
        target["load"] += weight
    walltime = math.ceil(2 * batch_size * p95 + 600)
    report = {
        "pilot_p95_seconds": p95,
        "runtime_batch_size": runtime_batch,
        "minimum_batch_size": execution.production_min_batch_size,
        "batch_size": batch_size,
        "production_tasks": task_count,
        "max_array_tasks": max_tasks,
        "walltime_seconds": walltime,
    }
    plan = duckdb.connect(str(root / "plan.duckdb"))
    try:
        plan.execute("BEGIN")
        try:
            if plan.execute("SELECT count(*) FROM tasks WHERE phase='production'").fetchone()[0]:
                raise ValueError("Production tasks appeared during planning")
            for index, item in enumerate(bins):
                task_id = f"production-{index:05d}"
                plan.execute("INSERT INTO tasks VALUES ('production',?,?,?)", [index, task_id, walltime])
                plan.executemany(
                    "INSERT INTO task_members VALUES (?,?,?)",
                    [(task_id, ordinal, position) for position, ordinal in enumerate(item["ordinals"])],
                )
            plan.execute("INSERT INTO manifest VALUES ('production_plan', ?)", [canonical(report)])
            plan.execute("COMMIT")
        except BaseException:
            plan.execute("ROLLBACK")
            raise
        plan.execute("CHECKPOINT")
    finally:
        plan.close()
    atomic_json(root / "production-plan.json", report)
    return report


# --- handoff -------------------------------------------------------------------


def finalize(root, destination, *, study_name="campaign"):
    """Materialize, validate and publish the handoff database; then remove shards."""
    root, destination = Path(root).resolve(), Path(destination).resolve()
    if (root / "PAUSE").exists():
        raise ValueError("Campaign is paused")
    info = plan_info(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    pending = destination.with_suffix(destination.suffix + ".pending")
    pending.unlink(missing_ok=True)
    out = duckdb.connect(str(pending))
    if os.environ.get("TMPDIR"):
        out.execute("SET temp_directory = ?", [str(Path(os.environ["TMPDIR"]) / "duckdb-spill")])
    out.execute(f"ATTACH '{root / 'plan.duckdb'}' AS plan (READ_ONLY)")
    out.execute("CREATE TABLE manifest AS SELECT * FROM plan.manifest")
    out.execute("CREATE TABLE weather AS SELECT * FROM plan.weather")
    out.execute("CREATE TABLE buildings AS SELECT * FROM plan.buildings")
    columns = ",".join(
        f"CAST(json_extract(inputs_json,'$.{key}') AS {kind}) AS input_{key}"
        if kind != "VARCHAR"
        else f"json_extract_string(inputs_json,'$.{key}') AS input_{key}"
        for key, kind in sorted(info["input_columns"].items())
    )
    axes = ",".join(AXES)
    out.execute(
        f"""CREATE TABLE runs AS SELECT ordinal,case_id,sample_id,group_id,weather_id,building_key,{axes},pilot,
        metadata_json,{columns},NULL::VARCHAR AS run_id FROM plan.runs"""
    )
    scalars = ",".join(f"{name} DOUBLE" for name in SCALAR_FIELDS)
    out.execute(
        f"CREATE TABLE scalars(ordinal INTEGER PRIMARY KEY,{scalars},elapsed_seconds DOUBLE,diagnostics JSON,"
        "rc_space_heating_demand_kwh DOUBLE,rc_cooling_demand_kwh DOUBLE,rc_seconds DOUBLE,rc_diagnostics JSON)"
    )
    out.execute(
        """CREATE TABLE hourly_results(ordinal INTEGER PRIMARY KEY, heating_kwh DOUBLE[], cooling_kwh DOUBLE[],
        dhw_kwh DOUBLE[], rc_heating_kwh DOUBLE[], rc_cooling_kwh DOUBLE[])"""
    )
    out.execute("CREATE TABLE task_runtimes(task_id VARCHAR PRIMARY KEY, runtime JSON)")
    seen = set()
    for number, (path, task_id, _count) in enumerate(verified_shards(root, info["campaign_id"])):
        alias = f"shard{number}"
        out.execute(f"ATTACH '{path}' AS {alias} (READ_ONLY)")
        ordinals = {r[0] for r in out.execute(f"SELECT ordinal FROM {alias}.results").fetchall()}
        if seen & ordinals:
            raise ValueError(f"Overlapping shard: {task_id}")
        seen |= ordinals
        extracts = ",".join(f"CAST(json_extract(scalars,'$.{name}') AS DOUBLE)" for name in SCALAR_FIELDS)
        out.execute(
            f"""INSERT INTO scalars SELECT ordinal,{extracts},elapsed_seconds,diagnostics,
            CAST(json_extract(rc_scalars,'$.space_heating_demand_kwh') AS DOUBLE),
            CAST(json_extract(rc_scalars,'$.cooling_demand_kwh') AS DOUBLE),rc_seconds,rc_diagnostics
            FROM {alias}.results"""
        )
        out.execute(
            f"INSERT INTO hourly_results SELECT ordinal,heating,cooling,dhw,rc_heating,rc_cooling FROM {alias}.results"
        )
        out.execute(f"UPDATE runs SET run_id=x.run_id FROM {alias}.results x WHERE runs.ordinal=x.ordinal")
        out.execute(f"INSERT INTO task_runtimes SELECT task_id,runtime FROM {alias}.identity")
        out.execute(f"DETACH {alias}")
    expected = out.execute("SELECT count(*) FROM runs").fetchone()[0]
    if len(seen) != expected:
        raise ValueError(f"Shard coverage is not exhaustive: {len(seen)}/{expected}")
    out.execute(
        "CREATE VIEW training_rows AS SELECT r.* EXCLUDE(metadata_json),"
        "s.* EXCLUDE(ordinal,diagnostics,rc_diagnostics) FROM runs r JOIN scalars s USING(ordinal)"
    )
    out.execute(
        """CREATE VIEW hourly AS SELECT r.ordinal,r.sample_id,r.weather_id,w.timestamp,
        h.heating_kwh[i] AS heating_kwh,h.cooling_kwh[i] AS cooling_kwh,h.dhw_kwh[i] AS dhw_kwh,
        h.heating_kwh[i]+h.dhw_kwh[i] AS ideal_thermal_load_kwh,
        h.rc_heating_kwh[i] AS rc_heating_kwh,h.rc_cooling_kwh[i] AS rc_cooling_kwh,
        w.dry_bulb_c,w.relative_humidity_pct,w.global_horizontal_wh_m2
        FROM runs r JOIN hourly_results h USING(ordinal), range(1,8761) q(i)
        JOIN weather w ON w.weather_id=r.weather_id AND w.t=i-1"""
    )
    sizes = dict(
        out.execute(
            "SELECT CAST(n AS VARCHAR),count(*) FROM (SELECT group_id,count(*) n FROM runs GROUP BY group_id) GROUP BY n"
        ).fetchall()
    )
    checks = {
        "runs": list(out.execute("SELECT count(*),count(DISTINCT ordinal),count(DISTINCT sample_id) FROM runs").fetchone()),
        "groups": out.execute("SELECT count(DISTINCT group_id) FROM runs").fetchone()[0],
        "group_sizes": sizes,
        "weather_rows": out.execute("SELECT count(*) FROM weather").fetchone()[0],
        "bad_weather_calendars": out.execute(
            "SELECT count(*) FROM (SELECT weather_id,count(*) n,count(DISTINCT t) nt FROM weather GROUP BY weather_id) "
            "WHERE n<>8760 OR nt<>8760"
        ).fetchone()[0],
        "incomplete_joins": out.execute(
            "SELECT count(*) FROM runs r LEFT JOIN scalars s USING(ordinal) LEFT JOIN hourly_results h USING(ordinal) "
            "LEFT JOIN (SELECT DISTINCT weather_id FROM weather) w USING(weather_id) "
            "WHERE s.ordinal IS NULL OR h.ordinal IS NULL OR w.weather_id IS NULL"
        ).fetchone()[0],
        "bad_scalars": out.execute(
            "SELECT count(*) FROM scalars WHERE "
            + " OR ".join(f"{name} IS NULL OR NOT isfinite({name})" for name in REQUIRED_SCALARS)
        ).fetchone()[0],
        "bad_arrays": out.execute(
            "SELECT count(*) FROM hourly_results WHERE len(heating_kwh)<>8760 OR len(cooling_kwh)<>8760 "
            "OR len(dhw_kwh)<>8760 OR len(list_filter(heating_kwh,x -> NOT isfinite(x) OR x<0))>0 "
            "OR len(list_filter(cooling_kwh,x -> NOT isfinite(x) OR x<0))>0 "
            "OR len(list_filter(dhw_kwh,x -> NOT isfinite(x) OR x<0))>0"
        ).fetchone()[0],
        "bad_rc_arrays": out.execute(
            "SELECT count(*) FROM hourly_results WHERE rc_heating_kwh IS NOT NULL AND (len(rc_heating_kwh)<>8760 "
            "OR len(rc_cooling_kwh)<>8760 OR len(list_filter(rc_heating_kwh,x -> NOT isfinite(x) OR x<0))>0)"
        ).fetchone()[0],
        "rc_runs": out.execute("SELECT count(*) FROM hourly_results WHERE rc_heating_kwh IS NOT NULL").fetchone()[0],
    }
    if (
        checks["runs"] != [expected, expected, expected]
        or checks["bad_arrays"]
        or checks["bad_rc_arrays"]
        or checks["bad_weather_calendars"]
        or checks["incomplete_joins"]
        or checks["bad_scalars"]
        or checks["groups"] != info["groups"]
        or checks["group_sizes"] != info["group_sizes"]
        or checks["weather_rows"] != info["weather_rows"]
    ):
        raise ValueError(f"Final acceptance failed: {checks}")
    mismatch = out.execute(
        """SELECT count(*) FROM hourly_results h JOIN scalars s USING(ordinal)
        WHERE abs(list_sum(heating_kwh)-space_heating_demand_kwh) > greatest(1e-6,0.001*abs(space_heating_demand_kwh))
        OR abs(list_sum(cooling_kwh)-cooling_demand_kwh) > greatest(1e-6,0.001*abs(cooling_demand_kwh))
        OR abs(list_sum(dhw_kwh)-dhw_load_kwh) > greatest(1e-6,0.001*abs(dhw_load_kwh))
        OR ideal_thermal_load_kwh<>space_heating_demand_kwh+dhw_load_kwh"""
    ).fetchone()[0]
    if mismatch:
        raise ValueError(f"Energy reconciliation failed for {mismatch} runs")
    out.execute("CHECKPOINT")
    out.close()
    os.replace(pending, destination)
    verify = duckdb.connect(str(destination), read_only=True, config={"enable_external_access": "false"})
    reopened = verify.execute("SELECT count(*) FROM runs").fetchone()[0]
    hourly_probe = verify.execute("SELECT heating_kwh,ideal_thermal_load_kwh FROM hourly LIMIT 1").fetchone()
    training_probe = verify.execute("SELECT count(*) FROM training_rows").fetchone()[0]
    verify.close()
    checksum = file_sha256(destination)
    receipt = {
        **json_value(checks),
        "reopened_read_only": reopened,
        "hourly_view_probe": json_value(hourly_probe),
        "training_rows": training_probe,
        "sha256": checksum,
        "database": str(destination),
    }
    atomic_json(destination.with_suffix(".verification.json"), receipt)
    destination.with_suffix(".sha256").write_text(f"{checksum}  {destination.name}\n")
    destination.with_name("README.md").write_text(
        f"# EPSM {study_name} handoff\n\nSelf-contained DuckDB. `training_rows` joins predictors and scalar targets; "
        "`hourly` lazily expands the EnergyPlus heating/cooling/DHW arrays, the RC heating/cooling arrays "
        "(NULL where RC was not run) and weather. `task_runtimes` records each task's engine identities.\n"
    )
    removed = []
    for name in ("shards", "receipts", "locks", "weather"):
        path = root / name
        if path.exists():
            shutil.rmtree(path)
            removed.append(name + "/")
    for name in ("plan.duckdb", "preflight.duckdb"):
        path = root / name
        if path.exists():
            path.unlink()
            removed.append(name)
    atomic_json(
        destination.with_suffix(".cleanup.json"),
        {"removed_after_verification": removed, "retained": ["handoff/", "logs/", "plan.json", "study.json"]},
    )
    return receipt
