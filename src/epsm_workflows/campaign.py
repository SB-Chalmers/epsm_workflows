"""Minimal, restartable campaign execution and self-contained DuckDB delivery."""

from __future__ import annotations

import csv
import fcntl
import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import traceback
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import duckdb
import numpy as np
from epsm_core import BuildingSpec, EnergyPlusCompiler, EnergyPlusRunner

TARGETS = (
    "space_heating_demand_kwh",
    "cooling_demand_kwh",
    "dhw_load_kwh",
)
WEATHER_FIELDS = (
    "dry_bulb_c",
    "dew_point_c",
    "relative_humidity_pct",
    "atmospheric_pressure_pa",
    "horizontal_infrared_wh_m2",
    "global_horizontal_wh_m2",
    "direct_normal_wh_m2",
    "diffuse_horizontal_wh_m2",
    "wind_direction_deg",
    "wind_speed_m_s",
)
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

_PREFLIGHT_COMPILER = None


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_sha256(path) -> str:
    checksum = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def atomic_json(path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.{os.getpid()}.pending")
    pending.write_text(canonical(value))
    with pending.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(pending, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@contextmanager
def exclusive_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"Already owned: {path}") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


@dataclass(frozen=True)
class CampaignCase:
    """Study-independent unit handed to the execution and storage layers."""

    ordinal: int
    sample_id: str
    group_id: str
    spec: BuildingSpec
    inputs: dict
    metadata: dict

    @property
    def case_id(self) -> str:
        return digest(self.spec.model_dump(mode="json"))


def _complexity(case: CampaignCase) -> float:
    vertices = sum(len(part.footprint.exterior) for part in case.spec.geometry.parts)
    floors = sum(part.floors for part in case.spec.geometry.parts)
    context = len(case.spec.context.buildings)
    return float(vertices * floors + context / 4)


def _epw_rows(weather_id: str, path: Path):
    with path.open(newline="", errors="replace") as stream:
        rows = list(csv.reader(stream))
    data = rows[8:]
    if len(data) != 8760:
        raise ValueError(f"{path}: expected 8760 EPW rows, found {len(data)}")
    for t, row in enumerate(data):
        if len(row) < 22:
            raise ValueError(f"{path}: incomplete EPW row {t + 1}")
        timestamp = (
            f"2018-{int(row[1]):02d}-{int(row[2]):02d} {int(row[3]) - 1:02d}:00:00"
        )
        indices = (6, 7, 8, 9, 12, 13, 14, 15, 20, 21)
        yield (weather_id, t, timestamp, *(float(row[i]) for i in indices))


def _schema(db) -> None:
    db.execute("CREATE TABLE manifest(key VARCHAR PRIMARY KEY, value JSON NOT NULL)")
    db.execute(
        """CREATE TABLE runs(
        ordinal INTEGER PRIMARY KEY, case_id VARCHAR UNIQUE NOT NULL,
        sample_id VARCHAR UNIQUE NOT NULL, group_id VARCHAR NOT NULL,
        cohort VARCHAR NOT NULL, site VARCHAR NOT NULL, context VARCHAR NOT NULL,
        weather_kind VARCHAR NOT NULL, weather_id VARCHAR NOT NULL,
        footprint_family VARCHAR NOT NULL, pilot BOOLEAN NOT NULL,
        complexity DOUBLE NOT NULL, spec_json JSON NOT NULL,
        inputs_json JSON NOT NULL, metadata_json JSON NOT NULL)"""
    )
    db.execute(
        """CREATE TABLE weather(
        weather_id VARCHAR NOT NULL, t SMALLINT NOT NULL, timestamp TIMESTAMP NOT NULL,
        dry_bulb_c DOUBLE, dew_point_c DOUBLE, relative_humidity_pct DOUBLE,
        atmospheric_pressure_pa DOUBLE, horizontal_infrared_wh_m2 DOUBLE,
        global_horizontal_wh_m2 DOUBLE, direct_normal_wh_m2 DOUBLE,
        diffuse_horizontal_wh_m2 DOUBLE, wind_direction_deg DOUBLE,
        wind_speed_m_s DOUBLE, PRIMARY KEY(weather_id,t))"""
    )
    db.execute(
        "CREATE TABLE tasks(phase VARCHAR, task_index INTEGER, task_id VARCHAR PRIMARY KEY, "
        "walltime_seconds INTEGER)"
    )
    db.execute(
        "CREATE TABLE task_members(task_id VARCHAR, ordinal INTEGER, position INTEGER, "
        "PRIMARY KEY(task_id,ordinal))"
    )


def create_plan(
    root,
    config: dict,
    cases: Iterable[CampaignCase],
    weather: dict,
    manifest: dict,
) -> dict:
    """Freeze cases, inputs and pilot assignments into one immutable planning database."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    final = root / "plan.duckdb"
    if final.exists():
        raise FileExistsError(final)
    pending = root / ".plan.pending.duckdb"
    pending.unlink(missing_ok=True)
    Path(str(pending) + ".wal").unlink(missing_ok=True)
    db = duckdb.connect(str(pending))
    try:
        _schema(db)
        db.execute("BEGIN")
        identity = digest(manifest)
        db.execute("INSERT INTO manifest VALUES ('campaign', ?)", [canonical(manifest)])
        count = pilot_count = 0
        group_counts: dict[str, int] = {}
        sampled_groups = set()
        lhs_groups: dict[int, tuple[list[str], list[list[float]], list[dict]]] = {}
        for case in cases:
            if case.ordinal != count:
                raise ValueError("Case ordinals must be contiguous and zero-based")
            meta = case.metadata
            for key, (low, high) in config.get("bounds", {}).items():
                value = case.inputs[key]
                if not math.isfinite(value) or not low <= value <= high:
                    raise ValueError(f"Sample outside bounds: {key}={value}")
            if case.inputs.get("topology") not in {"S", "F", "FT", "FTX"}:
                raise ValueError("Invalid ventilation topology")
            if case.inputs.get("schedule_family") not in {"constant", "daily"}:
                raise ValueError("Invalid schedule family")
            if case.group_id not in sampled_groups:
                sampled_groups.add(case.group_id)
                seed = int(meta["stratum_seed"])
                keys = list(meta["lhs_keys"])
                lhs_groups.setdefault(seed, (keys, [], []))[1].append(meta["lhs_unit"])
                lhs_groups[seed][2].append(case.inputs)
            pilot = bool(meta.get("pilot", False))
            db.execute(
                "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    case.ordinal,
                    case.case_id,
                    case.sample_id,
                    case.group_id,
                    meta["cohort"],
                    meta["city"],
                    meta["context"],
                    meta["weather_kind"],
                    meta["weather_id"],
                    meta["family"],
                    pilot,
                    _complexity(case),
                    case.spec.model_dump_json(),
                    canonical(case.inputs),
                    canonical(meta),
                ],
            )
            group_counts[case.group_id] = group_counts.get(case.group_id, 0) + 1
            if pilot:
                task_id = f"pilot-{pilot_count:04d}"
                db.execute(
                    "INSERT INTO tasks VALUES ('pilot',?,?,7200)",
                    [pilot_count, task_id],
                )
                db.execute(
                    "INSERT INTO task_members VALUES (?,?,0)", [task_id, case.ordinal]
                )
                pilot_count += 1
            count += 1
        expected = int(config.get("expected_runs", 60000))
        if count != expected:
            raise ValueError(f"Expected {expected} cases, generated {count}")
        expected_groups = int(config.get("expected_groups", 9200))
        if len(group_counts) != expected_groups:
            raise ValueError(
                f"Expected {expected_groups} groups, generated {len(group_counts)}"
            )
        expected_pilot = int(config.get("expected_pilot_runs", 420))
        if pilot_count != expected_pilot:
            raise ValueError(
                f"Expected {expected_pilot} pilot runs, generated {pilot_count}"
            )
        categories = {
            "num_floors": list(range(2, 9)),
            "topology": ["S", "F", "FT", "FTX"],
            "schedule_family": ["constant", "daily"],
            "schedule_shift": list(range(-2, 3)),
        }
        for seed, (keys, unit, inputs) in lhs_groups.items():
            matrix = np.asarray(unit)
            n = len(matrix)
            if matrix.shape != (n, len(keys)) or any(
                len(set(np.floor(matrix[:, column] * n).astype(int))) != n
                for column in range(len(keys))
            ):
                raise ValueError(
                    f"LHS marginal-stratum invariant failed for seed {seed}"
                )
            for key, allowed in categories.items():
                counts = [sum(row[key] == value for row in inputs) for value in allowed]
                if max(counts) - min(counts) > 1 or sum(counts) != n:
                    raise ValueError(
                        f"Categorical balance failed for {key}, seed {seed}"
                    )
        for site, kinds in sorted(weather.items()):
            for kind, item in sorted(kinds.items()):
                weather_id = f"{site}_{kind}"
                db.executemany(
                    "INSERT INTO weather VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    list(_epw_rows(weather_id, Path(item["path"]))),
                )
        weather_rows = db.execute("SELECT count(*) FROM weather").fetchone()[0]
        if weather_rows != len(weather) * 2 * 8760:
            raise ValueError("Weather bundle is incomplete")
        db.execute("COMMIT")
        db.execute("CHECKPOINT")
    finally:
        db.close()
    os.replace(pending, final)
    receipt = {
        "campaign_id": identity,
        "runs": count,
        "groups": len(group_counts),
        "pilot_runs": pilot_count,
        "weather_rows": weather_rows,
        "sampling_validation": {
            "bounds": "passed",
            "categories": "passed",
            "lhs_strata": "passed",
            "strata": len(lhs_groups),
        },
        "plan_sha256": file_sha256(final),
    }
    atomic_json(root / "plan.json", receipt)
    return receipt


def _preflight_identity(root: Path, energyplus: str) -> dict:
    """Return the immutable campaign and compiler identity used by preflight."""
    plan_info = json.loads((root / "plan.json").read_text())
    plan = duckdb.connect(str(root / "plan.duckdb"), read_only=True)
    try:
        manifest = plan.execute(
            "SELECT value FROM manifest WHERE key='campaign'"
        ).fetchone()
        expected = plan.execute("SELECT count(*) FROM runs").fetchone()[0]
    finally:
        plan.close()
    if not manifest:
        raise ValueError("Plan has no campaign manifest")
    manifest = manifest[0]
    if isinstance(manifest, str):
        manifest = json.loads(manifest)
    compiler = EnergyPlusCompiler(energyplus)
    identity = {
        "campaign_id": plan_info["campaign_id"],
        "expected": expected,
        "engine_sha256": compiler.engine_hash,
        "schema_sha256": compiler.schema_hash,
        "compiler_sha256": compiler.compiler_hash,
    }
    for key in ("engine_sha256", "schema_sha256", "compiler_sha256"):
        if manifest.get(key) != identity[key]:
            raise ValueError(
                f"Preflight {key} does not match the frozen campaign manifest"
            )
    return identity


def _preflight_schema(db) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS identity(
        campaign_id VARCHAR PRIMARY KEY, expected INTEGER NOT NULL,
        engine_sha256 VARCHAR NOT NULL, schema_sha256 VARCHAR NOT NULL,
        compiler_sha256 VARCHAR NOT NULL)"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS results(
        ordinal INTEGER PRIMARY KEY, case_id VARCHAR NOT NULL,
        status VARCHAR NOT NULL, failure_code VARCHAR, message VARCHAR,
        compile_seconds DOUBLE NOT NULL)"""
    )


def _initialize_preflight_worker(energyplus: str) -> None:
    global _PREFLIGHT_COMPILER
    _PREFLIGHT_COMPILER = EnergyPlusCompiler(energyplus)


def _compile_preflight_case(row):
    """Compile one frozen case without launching EnergyPlus."""
    ordinal, case_id, spec_json = row
    started = time.monotonic()
    try:
        spec = BuildingSpec.model_validate_json(spec_json)
        _PREFLIGHT_COMPILER.compile(spec)
        return ordinal, case_id, "passed", None, None, time.monotonic() - started
    except (OSError, MemoryError, TimeoutError):
        # These describe the compute environment, not a rejected campaign case.
        # Let the stage stop so its already committed batches can be resumed.
        raise
    except Exception as exc:
        return (
            ordinal,
            case_id,
            "failed",
            getattr(exc, "code", "UNCLASSIFIED_COMPILATION_FAILURE"),
            str(exc),
            time.monotonic() - started,
        )


def preflight(root, energyplus: str, workers: int = 1) -> dict:
    """Compile every planned case and persist a restartable acceptance gate."""
    root = Path(root).resolve()
    workers = max(1, int(workers))
    identity = _preflight_identity(root, energyplus)
    database = root / "preflight.duckdb"
    started = time.monotonic()
    with exclusive_lock(root / "locks" / "preflight.lock"):
        checks = duckdb.connect(str(database))
        _preflight_schema(checks)
        stored_identity = checks.execute("SELECT * FROM identity").fetchall()
        expected_identity = [
            (
                identity["campaign_id"],
                identity["expected"],
                identity["engine_sha256"],
                identity["schema_sha256"],
                identity["compiler_sha256"],
            )
        ]
        if stored_identity and stored_identity != expected_identity:
            checks.close()
            raise ValueError(
                "Existing preflight database belongs to another campaign or compiler"
            )
        if not stored_identity:
            checks.execute("INSERT INTO identity VALUES (?,?,?,?,?)", expected_identity[0])
        completed = {
            row[0] for row in checks.execute("SELECT ordinal FROM results").fetchall()
        }
        plan = duckdb.connect(str(root / "plan.duckdb"), read_only=True)
        cursor = plan.execute(
            "SELECT ordinal,case_id,spec_json FROM runs ORDER BY ordinal"
        )
        added = 0
        executor = None
        try:
            if workers == 1:
                _initialize_preflight_worker(energyplus)
            else:
                executor = ProcessPoolExecutor(
                    max_workers=workers,
                    initializer=_initialize_preflight_worker,
                    initargs=(energyplus,),
                )
            while True:
                rows = cursor.fetchmany(128)
                if not rows:
                    break
                rows = [row for row in rows if row[0] not in completed]
                if not rows:
                    continue
                outcomes = (
                    list(executor.map(_compile_preflight_case, rows, chunksize=4))
                    if executor is not None
                    else [_compile_preflight_case(row) for row in rows]
                )
                checks.execute("BEGIN")
                try:
                    checks.executemany("INSERT INTO results VALUES (?,?,?,?,?,?)", outcomes)
                    checks.execute("COMMIT")
                except BaseException:
                    checks.execute("ROLLBACK")
                    raise
                added += len(outcomes)
                if added % 1024 < len(outcomes):
                    total = len(completed) + added
                    print(
                        f"preflight: {total:,}/{identity['expected']:,} compiled",
                        file=sys.stderr,
                        flush=True,
                    )
                    checks.execute("CHECKPOINT")
        finally:
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=True)
            plan.close()
            checks.execute("CHECKPOINT")
            checks.close()

        checks = duckdb.connect(str(database), read_only=True)
        try:
            checked, passed, failed, compile_seconds = checks.execute(
                """SELECT count(*),count(*) FILTER (WHERE status='passed'),
                count(*) FILTER (WHERE status='failed'),coalesce(sum(compile_seconds),0)
                FROM results"""
            ).fetchone()
            failures = [
                {
                    "ordinal": row[0],
                    "case_id": row[1],
                    "failure_code": row[2],
                    "message": row[3],
                }
                for row in checks.execute(
                    """SELECT ordinal,case_id,failure_code,message FROM results
                    WHERE status='failed' ORDER BY ordinal LIMIT 20"""
                ).fetchall()
            ]
        finally:
            checks.close()
        state = (
            "passed"
            if checked == identity["expected"] and failed == 0
            else "failed"
        )
        receipt = {
            "state": state,
            **identity,
            "checked": checked,
            "passed": passed,
            "failed": failed,
            "elapsed_seconds": compile_seconds,
            "invocation_seconds": time.monotonic() - started,
            "database": str(database),
            "failures": failures,
            "failure_summary_truncated": failed > len(failures),
        }
        atomic_json(root / "preflight.json", receipt)
        return receipt


def require_preflight(root, energyplus: str | None = None) -> dict:
    """Require exhaustive passing preflight evidence for the current plan."""
    root = Path(root).resolve()
    receipt_path = root / "preflight.json"
    database = root / "preflight.duckdb"
    if not receipt_path.exists() or not database.exists():
        raise ValueError("Campaign preflight is missing; run epsm-workflows preflight")
    receipt = json.loads(receipt_path.read_text())
    plan_info = json.loads((root / "plan.json").read_text())
    checks = duckdb.connect(str(database), read_only=True)
    try:
        identity = checks.execute("SELECT * FROM identity").fetchone()
        checks.execute(f"ATTACH '{root / 'plan.duckdb'}' AS campaign_plan (READ_ONLY)")
        expected = checks.execute(
            "SELECT count(*) FROM campaign_plan.runs"
        ).fetchone()[0]
        missing = checks.execute(
            """SELECT count(*) FROM campaign_plan.runs r LEFT JOIN results p
            ON r.ordinal=p.ordinal AND r.case_id=p.case_id WHERE p.ordinal IS NULL"""
        ).fetchone()[0]
        extra = checks.execute(
            """SELECT count(*) FROM results p LEFT JOIN campaign_plan.runs r
            ON r.ordinal=p.ordinal AND r.case_id=p.case_id WHERE r.ordinal IS NULL"""
        ).fetchone()[0]
        failed = checks.execute(
            "SELECT count(*) FROM results WHERE status<>'passed'"
        ).fetchone()[0]
    finally:
        checks.close()
    if (
        not identity
        or identity[0] != plan_info["campaign_id"]
        or identity[1] != expected
        or receipt.get("state") != "passed"
        or receipt.get("checked") != expected
        or receipt.get("failed") != 0
        or missing
        or extra
        or failed
    ):
        raise ValueError(
            "Campaign preflight is incomplete, failed, or does not match the current plan"
        )
    if energyplus is not None:
        compiler = EnergyPlusCompiler(energyplus)
        actual = (compiler.engine_hash, compiler.schema_hash, compiler.compiler_hash)
        if actual != identity[2:5]:
            raise ValueError("Submission compiler or EnergyPlus does not match preflight")
    return receipt


def _shard_schema(db) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS identity(task_id VARCHAR PRIMARY KEY, campaign_id VARCHAR)"
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS results(
        ordinal INTEGER PRIMARY KEY, case_id VARCHAR NOT NULL, run_id VARCHAR NOT NULL,
        scalars JSON NOT NULL, diagnostics JSON NOT NULL,
        heating DOUBLE[] NOT NULL, cooling DOUBLE[] NOT NULL, dhw DOUBLE[] NOT NULL,
        elapsed_seconds DOUBLE NOT NULL, completed_at TIMESTAMP DEFAULT current_timestamp)"""
    )


def _open_shard_path(path: Path, task_id: str, campaign_id: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = duckdb.connect(str(path))
    _shard_schema(db)
    identity = db.execute("SELECT task_id,campaign_id FROM identity").fetchall()
    if identity and identity != [(task_id, campaign_id)]:
        db.close()
        raise ValueError(f"Shard identity mismatch: {path}")
    if not identity:
        db.execute("INSERT INTO identity VALUES (?,?)", [task_id, campaign_id])
    return db, path


def _execution(root: Path) -> dict:
    config_path = root / "config.json"
    configured = json.loads(config_path.read_text()).get("execution", {}) if config_path.exists() else {}
    return {
        "checkpoint_runs": max(1, int(configured.get("checkpoint_runs", 8))),
        "checkpoint_seconds": max(1.0, float(configured.get("checkpoint_seconds", 300))),
        "production_min_batch_size": max(
            1, int(configured.get("production_min_batch_size", 32))
        ),
        "startup_jitter_seconds": max(
            0.0, float(configured.get("startup_jitter_seconds", 30))
        ),
        "excluded_nodes": list(configured.get("excluded_nodes", [])),
    }


def _publish_shard(local_path: Path, persistent_path: Path) -> None:
    """Atomically publish one closed, checkpointed node-local DuckDB shard."""
    persistent_path.parent.mkdir(parents=True, exist_ok=True)
    pending = persistent_path.with_name(
        f".{persistent_path.name}.{os.getpid()}.pending"
    )
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


def task_rows(root, phase: str, index: int):
    """Resolve a task and its members with one read of the central plan database."""
    root = Path(root).resolve()
    plan = duckdb.connect(str(root / "plan.duckdb"), read_only=True)
    try:
        task = plan.execute(
            "SELECT task_id FROM tasks WHERE phase=? AND task_index=?", [phase, index]
        ).fetchone()
        if not task:
            raise ValueError(f"Unknown {phase} task index {index}")
        rows = plan.execute(
            """SELECT r.ordinal,r.case_id,r.spec_json
            FROM task_members m JOIN runs r USING(ordinal)
            WHERE m.task_id=? ORDER BY m.position""",
            [task[0]],
        ).fetchall()
    finally:
        plan.close()
    return task[0], rows


def _validated_result(result):
    arrays = [np.asarray(result.hourly[name], dtype=float) for name in TARGETS]
    for name, values in zip(TARGETS, arrays):
        if (
            len(values) != 8760
            or not np.isfinite(values).all()
            or np.any(values < -1e-8)
        ):
            raise ValueError(f"Invalid hourly result: {name}")
        if not np.isclose(values.sum(), result.scalars[name], rtol=0.001, atol=1e-6):
            raise ValueError(f"Annual/hourly disagreement: {name}")
    ideal = arrays[0] + arrays[2]
    if not np.array_equal(ideal, np.asarray(result.hourly["ideal_thermal_load_kwh"])):
        raise ValueError("Derived ideal thermal load is not exact")
    return arrays


def _diagnostic_bundle(
    root: Path, task_id: str, ordinal: int, scratch: Path, exc: Exception
):
    destination = root / "diagnostics" / f"{task_id}-{ordinal:05d}.tar.gz"
    destination.parent.mkdir(parents=True, exist_ok=True)
    evidence = scratch / "failure.json"
    atomic_json(
        evidence,
        {"error": str(exc), "traceback": traceback.format_exc(), "time": time.time()},
    )
    keep = {
        "building.json",
        "model.idf",
        "compilation.json",
        "diagnostics.json",
        "eplusout.err",
        "eplusout.end",
        "process.log",
        "failure.json",
    }
    with tarfile.open(destination, "w:gz") as archive:
        for path in sorted(scratch.rglob("*")):
            if path.is_file() and path.name in keep:
                archive.add(path, arcname=path.relative_to(scratch))
    return destination


def _hold_array() -> None:
    job = os.environ.get("SLURM_ARRAY_JOB_ID") or os.environ.get("SLURM_JOB_ID")
    if job and shutil.which("scontrol"):
        subprocess.run(["scontrol", "hold", job], check=False)


def _pause_once(root: Path, evidence: dict) -> None:
    with exclusive_lock(root / "locks" / "pause.lock"):
        if not (root / "PAUSE").exists():
            atomic_json(root / "PAUSE", evidence)


def run_task(
    root, task_id: str, energyplus: str, timeout: float = 1800, rows=None
) -> dict:
    """Run a task locally and publish durable shard checkpoints to shared storage."""
    root = Path(root).resolve()
    plan_info = json.loads((root / "plan.json").read_text())
    if os.environ.get("SLURM_JOB_ID") and not os.environ.get("TMPDIR"):
        raise ValueError("Slurm worker requires node-local TMPDIR")
    campaign_id = plan_info["campaign_id"]
    execution = _execution(root)
    stop = False

    def drain(_signum, _frame):
        nonlocal stop
        stop = True

    previous = {
        sig: signal.signal(sig, drain) for sig in (signal.SIGTERM, signal.SIGUSR1)
    }
    try:
        with exclusive_lock(root / "locks" / f"{task_id}.lock"):
            if rows is None:
                plan = duckdb.connect(str(root / "plan.duckdb"), read_only=True)
                try:
                    rows = plan.execute(
                        """SELECT r.ordinal,r.case_id,r.spec_json
                        FROM task_members m JOIN runs r USING(ordinal)
                        WHERE m.task_id=? ORDER BY m.position""",
                        [task_id],
                    ).fetchall()
                finally:
                    plan.close()
            if not rows:
                raise ValueError(f"Unknown or empty task: {task_id}")
            scratch_base = Path(os.environ.get("TMPDIR", "/tmp")).resolve()
            local_path = scratch_base / "epsm-shards" / f"{task_id}.duckdb"
            shard_path = root / "shards" / f"{task_id}.duckdb"
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.unlink(missing_ok=True)
            Path(str(local_path) + ".wal").unlink(missing_ok=True)
            if shard_path.exists():
                shutil.copyfile(shard_path, local_path)
            shard, _ = _open_shard_path(local_path, task_id, campaign_id)
            completed = {
                row[0]
                for row in shard.execute("SELECT ordinal FROM results").fetchall()
            }
            try:
                runner = EnergyPlusRunner(energyplus)
            except Exception as exc:
                _pause_once(
                    root,
                    {"task_id": task_id, "error": str(exc), "time": time.time()},
                )
                _hold_array()
                raise
            added = 0
            unpublished = 0
            last_checkpoint = time.monotonic()

            def publish(drained=False):
                nonlocal shard, unpublished, last_checkpoint
                total = shard.execute("SELECT count(*) FROM results").fetchone()[0]
                if unpublished or not shard_path.exists():
                    shard.execute("CHECKPOINT")
                    shard.close()
                    _publish_shard(local_path, shard_path)
                    shard, _ = _open_shard_path(local_path, task_id, campaign_id)
                    unpublished = 0
                    last_checkpoint = time.monotonic()
                atomic_json(
                    root / "receipts" / f"{task_id}.json",
                    {
                        "task_id": task_id,
                        "assigned": len(rows),
                        "complete": total,
                        "added": added,
                        "shard": str(shard_path),
                        "drained": drained,
                        "checkpoint_runs": execution["checkpoint_runs"],
                        "checkpoint_seconds": execution["checkpoint_seconds"],
                        "checkpointed_at": time.time(),
                    },
                )
                return total

            for ordinal, case_id, spec_json in rows:
                if stop or (root / "PAUSE").exists():
                    break
                if ordinal in completed:
                    continue
                scratch = scratch_base / f"epsm-{task_id}-{ordinal}-{os.getpid()}"
                if scratch.exists():
                    shutil.rmtree(scratch)
                started = time.monotonic()
                try:
                    spec = BuildingSpec.model_validate_json(spec_json)
                    result = runner.run(spec, scratch, timeout_seconds=timeout)
                    heating, cooling, dhw = _validated_result(result)
                    elapsed = time.monotonic() - started
                    shard.execute("BEGIN")
                    shard.execute(
                        "INSERT INTO results VALUES (?,?,?,?,?,?,?,?,?,current_timestamp)",
                        [
                            ordinal,
                            case_id,
                            result.run_id,
                            canonical(result.scalars),
                            canonical(result.diagnostics),
                            heating.tolist(),
                            cooling.tolist(),
                            dhw.tolist(),
                            elapsed,
                        ],
                    )
                    shard.execute("COMMIT")
                    added += 1
                    unpublished += 1
                    shutil.rmtree(scratch)
                    if (
                        unpublished >= execution["checkpoint_runs"]
                        or time.monotonic() - last_checkpoint
                        >= execution["checkpoint_seconds"]
                    ):
                        publish()
                except Exception as exc:
                    try:
                        shard.execute("ROLLBACK")
                    except duckdb.Error:
                        pass
                    if unpublished:
                        publish()
                    bundle = _diagnostic_bundle(root, task_id, ordinal, scratch, exc)
                    _pause_once(
                        root,
                        {
                            "task_id": task_id,
                            "ordinal": ordinal,
                            "error": str(exc),
                            "diagnostic": str(bundle),
                            "time": time.time(),
                        },
                    )
                    _hold_array()
                    raise
            publish(drained=stop)
            shard.close()
            local_path.unlink(missing_ok=True)
            receipt = json.loads((root / "receipts" / f"{task_id}.json").read_text())
            return receipt
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def status(root) -> dict:
    root = Path(root)
    plan = duckdb.connect(str(root / "plan.duckdb"), read_only=True)
    tasks = plan.execute("SELECT phase,count(*) FROM tasks GROUP BY phase").fetchall()
    assigned = plan.execute("SELECT count(*) FROM task_members").fetchone()[0]
    expected = plan.execute("SELECT count(*) FROM runs").fetchone()[0]
    plan.close()
    complete = 0
    failures = (
        len(list((root / "diagnostics").glob("*.tar.gz")))
        if (root / "diagnostics").exists()
        else 0
    )
    for path in (
        sorted((root / "shards").glob("*.duckdb")) if (root / "shards").exists() else ()
    ):
        try:
            db = duckdb.connect(str(path), read_only=True)
            complete += db.execute("SELECT count(*) FROM results").fetchone()[0]
            db.close()
        except duckdb.IOException:
            receipt = root / "receipts" / f"{path.stem}.json"
            if receipt.exists():
                complete += json.loads(receipt.read_text())["complete"]
    return {
        "expected": expected,
        "assigned": assigned,
        "complete": complete,
        "remaining": expected - complete,
        "tasks": dict(tasks),
        "failures": failures,
        "paused": (root / "PAUSE").exists(),
    }


def prepare_production_tasks(root, max_tasks: int = 5000) -> dict:
    """Use pilot p95 and LPT complexity balancing to generate production batches once."""
    root = Path(root)
    plan = duckdb.connect(str(root / "plan.duckdb"), read_only=True)
    try:
        existing = plan.execute(
            "SELECT count(*) FROM tasks WHERE phase='production'"
        ).fetchone()[0]
        marker = plan.execute(
            "SELECT value FROM manifest WHERE key='production_plan'"
        ).fetchone()
        if existing:
            if not marker:
                raise ValueError("Production tasks exist without an atomic plan marker")
            report = marker[0]
            if isinstance(report, str):
                report = json.loads(report)
            atomic_json(root / "production-plan.json", report)
            return report
        if marker:
            raise ValueError("Production plan marker exists without tasks")
        pilot_expected = plan.execute(
            "SELECT count(*) FROM runs WHERE pilot"
        ).fetchone()[0]
        rows = plan.execute(
            "SELECT ordinal,complexity FROM runs WHERE NOT pilot "
            "ORDER BY complexity DESC,ordinal"
        ).fetchall()
    finally:
        plan.close()

    summary = status(root)
    pilot_done = 0
    elapsed = []
    for path in sorted((root / "shards").glob("pilot-*.duckdb")):
        shard = duckdb.connect(str(path), read_only=True)
        values = [
            float(row[0])
            for row in shard.execute("SELECT elapsed_seconds FROM results").fetchall()
        ]
        pilot_done += len(values)
        elapsed.extend(values)
        shard.close()
    if summary["paused"] or pilot_done != pilot_expected or summary["failures"]:
        raise ValueError(f"Pilot gate failed: {pilot_done}/{pilot_expected}; {summary}")

    p95 = float(np.percentile(elapsed, 95))
    minimum_batch = math.ceil(len(rows) / max_tasks)
    configured_minimum = _execution(root)["production_min_batch_size"]
    runtime_batch = min(128, math.floor(2700 / max(p95, 1)))
    batch_size = max(configured_minimum, minimum_batch, runtime_batch)
    if batch_size > 128:
        raise ValueError(
            "Production cannot fit MaxArraySize with batches of at most 128: "
            f"need {minimum_batch}"
        )
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
        "minimum_batch_size": configured_minimum,
        "batch_size": batch_size,
        "production_tasks": task_count,
        "max_array_tasks": max_tasks,
        "walltime_seconds": walltime,
    }

    plan = duckdb.connect(str(root / "plan.duckdb"))
    try:
        plan.execute("BEGIN")
        try:
            if plan.execute(
                "SELECT count(*) FROM tasks WHERE phase='production'"
            ).fetchone()[0]:
                raise ValueError("Production tasks appeared during planning")
            for index, item in enumerate(bins):
                task_id = f"production-{index:05d}"
                plan.execute(
                    "INSERT INTO tasks VALUES ('production',?,?,?)",
                    [index, task_id, walltime],
                )
                plan.executemany(
                    "INSERT INTO task_members VALUES (?,?,?)",
                    [
                        (task_id, ordinal, position)
                        for position, ordinal in enumerate(item["ordinals"])
                    ],
                )
            plan.execute(
                "INSERT INTO manifest VALUES ('production_plan', ?)",
                [canonical(report)],
            )
            plan.execute("COMMIT")
        except BaseException:
            plan.execute("ROLLBACK")
            raise
        plan.execute("CHECKPOINT")
    finally:
        plan.close()
    atomic_json(root / "production-plan.json", report)
    return report


def _verified_shards(root: Path, campaign_id: str):
    for path in sorted((root / "shards").glob("*.duckdb")):
        db = duckdb.connect(str(path), read_only=True)
        identity = db.execute("SELECT task_id,campaign_id FROM identity").fetchone()
        count = db.execute("SELECT count(*) FROM results").fetchone()[0]
        db.close()
        if not identity or identity[1] != campaign_id:
            raise ValueError(f"Foreign shard: {path}")
        yield path, identity[0], count


def finalize(root, destination) -> dict:
    """Materialize and validate the portable handoff database."""
    root, destination = Path(root).resolve(), Path(destination).resolve()
    preflight_receipt = require_preflight(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if (root / "PAUSE").exists():
        raise ValueError("Campaign is paused")
    plan_info = json.loads((root / "plan.json").read_text())
    pending = destination.with_suffix(destination.suffix + ".pending")
    pending.unlink(missing_ok=True)
    out = duckdb.connect(str(pending))
    # Intermediate query spills belong on the compute node, not Cephyr.
    if os.environ.get("TMPDIR"):
        out.execute("SET temp_directory = ?", [str(Path(os.environ["TMPDIR"]) / "duckdb-spill")])
    out.execute(f"ATTACH '{root / 'plan.duckdb'}' AS plan (READ_ONLY)")
    out.execute("CREATE TABLE manifest AS SELECT * FROM plan.manifest")
    out.execute(
        "INSERT INTO manifest VALUES ('preflight', ?)",
        [canonical(preflight_receipt)],
    )
    out.execute("CREATE TABLE weather AS SELECT * FROM plan.weather")
    input_keys = [
        row[0]
        for row in out.execute(
            "SELECT DISTINCT unnest(json_keys(inputs_json)) FROM plan.runs ORDER BY 1"
        ).fetchall()
    ]
    input_sql = ",".join(
        f"CAST(json_extract(inputs_json, '$.{key}') AS DOUBLE) AS input_{key}"
        for key in input_keys
        if key not in {"num_floors", "schedule_shift", "topology", "schedule_family"}
    )
    if input_sql:
        input_sql = "," + input_sql
    out.execute(f"""CREATE TABLE runs AS SELECT ordinal,case_id,sample_id,group_id,cohort,site,context,weather_kind,weather_id,footprint_family,pilot,spec_json,metadata_json,
        CAST(json_extract(inputs_json,'$.num_floors') AS INTEGER) AS input_num_floors,
        json_extract_string(inputs_json,'$.topology') AS input_topology,
        json_extract_string(inputs_json,'$.schedule_family') AS input_schedule_family,
        CAST(json_extract(inputs_json,'$.schedule_shift') AS INTEGER) AS input_schedule_shift
        {input_sql}, 'complete'::VARCHAR AS status, NULL::VARCHAR AS run_id FROM plan.runs""")
    scalar_columns = ",".join(f"{name} DOUBLE" for name in SCALAR_FIELDS)
    out.execute(
        f"CREATE TABLE scalars(ordinal INTEGER PRIMARY KEY,{scalar_columns},"
        "elapsed_seconds DOUBLE,diagnostics JSON)"
    )
    out.execute(
        """CREATE TABLE hourly_results(ordinal INTEGER PRIMARY KEY, heating_kwh DOUBLE[], cooling_kwh DOUBLE[], dhw_kwh DOUBLE[])"""
    )
    seen = set()
    for number, (path, task_id, _count) in enumerate(
        _verified_shards(root, plan_info["campaign_id"])
    ):
        alias = f"shard{number}"
        out.execute(f"ATTACH '{path}' AS {alias} (READ_ONLY)")
        ordinals = {
            row[0]
            for row in out.execute(f"SELECT ordinal FROM {alias}.results").fetchall()
        }
        if seen & ordinals:
            raise ValueError(f"Overlapping shard: {task_id}")
        seen |= ordinals
        extracts = ",".join(
            f"CAST(json_extract(scalars,'$.{name}') AS DOUBLE)"
            for name in SCALAR_FIELDS
        )
        out.execute(
            f"INSERT INTO scalars SELECT ordinal,{extracts},elapsed_seconds,diagnostics "
            f"FROM {alias}.results"
        )
        out.execute(
            f"INSERT INTO hourly_results SELECT ordinal,heating,cooling,dhw FROM {alias}.results"
        )
        out.execute(
            f"UPDATE runs SET run_id=x.run_id FROM {alias}.results x WHERE runs.ordinal=x.ordinal"
        )
        out.execute(f"DETACH {alias}")
    expected = out.execute("SELECT count(*) FROM runs").fetchone()[0]
    if len(seen) != expected:
        raise ValueError(f"Shard coverage is not exhaustive: {len(seen)}/{expected}")
    out.execute("""CREATE VIEW training_rows AS
        SELECT r.*,s.* EXCLUDE(ordinal,diagnostics)
        FROM runs r JOIN scalars s USING(ordinal)""")
    out.execute("""CREATE VIEW hourly AS SELECT r.ordinal,r.sample_id,r.weather_id,w.timestamp,
        h.heating_kwh[i] AS heating_kwh,h.cooling_kwh[i] AS cooling_kwh,
        h.dhw_kwh[i] AS dhw_kwh,h.heating_kwh[i]+h.dhw_kwh[i] AS ideal_thermal_load_kwh,
        w.dry_bulb_c,w.relative_humidity_pct,w.global_horizontal_wh_m2
        FROM runs r JOIN hourly_results h USING(ordinal), range(1,8761) q(i)
        JOIN weather w ON w.weather_id=r.weather_id AND w.t=i-1""")
    checks = {
        "runs": out.execute(
            "SELECT count(*),count(DISTINCT ordinal),count(DISTINCT sample_id) FROM runs"
        ).fetchone(),
        "groups": out.execute("SELECT count(DISTINCT group_id) FROM runs").fetchone()[
            0
        ],
        "weather_rows": out.execute("SELECT count(*) FROM weather").fetchone()[0],
        "bad_weather_calendars": out.execute(
            "SELECT count(*) FROM (SELECT weather_id,count(*) n,count(DISTINCT t) nt "
            "FROM weather GROUP BY weather_id) WHERE n<>8760 OR nt<>8760"
        ).fetchone()[0],
        "incomplete_joins": out.execute(
            "SELECT count(*) FROM runs r LEFT JOIN scalars s USING(ordinal) "
            "LEFT JOIN hourly_results h USING(ordinal) LEFT JOIN "
            "(SELECT DISTINCT weather_id FROM weather) w USING(weather_id) "
            "WHERE s.ordinal IS NULL OR h.ordinal IS NULL OR w.weather_id IS NULL"
        ).fetchone()[0],
        "bad_scalars": out.execute(
            "SELECT count(*) FROM scalars WHERE "
            + " OR ".join(
                f"{name} IS NULL OR NOT isfinite({name})" for name in SCALAR_FIELDS
            )
        ).fetchone()[0],
        "bad_arrays": out.execute(
            "SELECT count(*) FROM hourly_results WHERE len(heating_kwh)<>8760 "
            "OR len(cooling_kwh)<>8760 OR len(dhw_kwh)<>8760 "
            "OR len(list_filter(heating_kwh,x -> NOT isfinite(x) OR x<0))>0 "
            "OR len(list_filter(cooling_kwh,x -> NOT isfinite(x) OR x<0))>0 "
            "OR len(list_filter(dhw_kwh,x -> NOT isfinite(x) OR x<0))>0"
        ).fetchone()[0],
        "group_sizes": dict(
            out.execute(
                "SELECT cohort,count(*) FROM (SELECT cohort,group_id,count(*) n FROM runs GROUP BY ALL) WHERE (cohort='diversity' AND n<>6) OR (cohort='anchors' AND n<>30) GROUP BY cohort"
            ).fetchall()
        ),
    }
    if (
        checks["runs"] != (expected, expected, expected)
        or checks["bad_arrays"]
        or checks["bad_weather_calendars"]
        or checks["incomplete_joins"]
        or checks["bad_scalars"]
        or checks["group_sizes"]
        or checks["groups"] != plan_info.get("groups", checks["groups"])
        or checks["weather_rows"]
        != plan_info.get("weather_rows", checks["weather_rows"])
    ):
        raise ValueError(f"Final acceptance failed: {checks}")
    mismatch = out.execute("""SELECT count(*) FROM hourly_results h JOIN scalars s USING(ordinal)
        WHERE abs(list_sum(heating_kwh)-space_heating_demand_kwh) > greatest(1e-6,0.001*abs(space_heating_demand_kwh))
        OR abs(list_sum(cooling_kwh)-cooling_demand_kwh) > greatest(1e-6,0.001*abs(cooling_demand_kwh))
        OR abs(list_sum(dhw_kwh)-dhw_load_kwh) > greatest(1e-6,0.001*abs(dhw_load_kwh))
        OR ideal_thermal_load_kwh<>space_heating_demand_kwh+dhw_load_kwh""").fetchone()[
        0
    ]
    if mismatch:
        raise ValueError(f"Energy reconciliation failed for {mismatch} runs")
    out.execute("CHECKPOINT")
    out.close()
    os.replace(pending, destination)
    verify = duckdb.connect(
        str(destination), read_only=True, config={"enable_external_access": "false"}
    )
    reopened = verify.execute("SELECT count(*) FROM runs").fetchone()[0]
    hourly_probe = verify.execute(
        "SELECT heating_kwh,ideal_thermal_load_kwh FROM hourly LIMIT 1"
    ).fetchone()
    training_probe = verify.execute("SELECT count(*) FROM training_rows").fetchone()[0]
    verify.close()
    checksum = file_sha256(destination)
    receipt = {
        **checks,
        "reopened_read_only": reopened,
        "hourly_view_probe": hourly_probe,
        "training_rows": training_probe,
        "sha256": checksum,
        "database": str(destination),
    }
    atomic_json(destination.with_suffix(".verification.json"), receipt)
    destination.with_suffix(".sha256").write_text(f"{checksum}  {destination.name}\n")
    destination.with_name("README.md").write_text(
        "# EPSM 60,000-run handoff\n\nSelf-contained DuckDB. `training_rows` contains predictors and scalar targets; `hourly` lazily expands hourly arrays and joins weather.\n"
    )
    removed = []
    for name in ("shards", "receipts", "locks", "weather"):
        path = root / name
        if path.exists():
            shutil.rmtree(path)
            removed.append(name + "/")
    for name in ("plan.duckdb", "templates.json", "preflight.duckdb"):
        path = root / name
        if path.exists():
            path.unlink()
            removed.append(name)
    atomic_json(
        destination.with_suffix(".cleanup.json"),
        {"removed_after_verification": removed, "retained": ["handoff/", "logs/"]},
    )
    return receipt
