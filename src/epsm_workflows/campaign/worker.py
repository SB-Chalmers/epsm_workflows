# SPDX-License-Identifier: MIT
"""One Slurm array task: run a contiguous case range and publish a result shard.

Each case rebuilds its Core Building, runs EnergyPlus in node-local scratch and,
when ``epsm_rc`` is installed, the RC engine on the same object and weather. A
strict EnergyPlus failure pauses the campaign; an RC failure is recorded on the
row and does not stop the task.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import subprocess
import tarfile
import time
import traceback
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import duckdb
import numpy as np

from ..records import atomic_json, canonical, exclusive_lock
from .store import open_shard, plan_info, publish_shard, task_rows, validated_arrays
from .weather import file_sha256

RC_POLICY = "epsm_rc_5r1c_v1"


def runtime_identity(energyplus):
    """Versions and engine hashes recorded with every shard, without requiring equality."""
    from epsm_core import EnergyPlusCompiler

    compiler = EnergyPlusCompiler(energyplus)
    packages = {}
    for name in ("epsm_core", "epsm_data", "epsm_rc", "epsm_workflows", "duckdb", "numpy"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    return {
        "python": platform.python_version(),
        "node": platform.node(),
        "packages": packages,
        "energyplus": {
            name: getattr(compiler, name, None) for name in ("engine_hash", "schema_hash", "version")
        },
        "rc_policy": RC_POLICY if packages["epsm_rc"] else None,
    }


def rc_available():
    try:
        import epsm_rc  # noqa: F401
    except ImportError:
        return False
    return True


def run_rc(building, weather, directory):
    """Hourly RC heating and cooling for the same building; None when unsupported."""
    from epsm_rc import RCSettings, simulate_building

    readiness = building.readiness(engine="epsm_rc")
    if not readiness.ready:
        return None, {"skipped": list(readiness.missing), "rc_policy": RC_POLICY}
    result = simulate_building(building, weather=weather, directory=directory, settings=RCSettings.from_policy(RC_POLICY))
    arrays = [np.asarray(result.hourly[name], dtype=float) for name in ("space_heating_demand_kwh", "cooling_demand_kwh")]
    for values in arrays:
        if len(values) != 8760 or not np.isfinite(values).all():
            raise ValueError("Invalid RC hourly result")
    scalars = {
        "space_heating_demand_kwh": float(arrays[0].sum()),
        "cooling_demand_kwh": float(arrays[1].sum()),
        **{k: float(v) for k, v in result.scalars.items() if isinstance(v, (int, float))},
    }
    return arrays, {"scalars": scalars, "diagnostics": result.diagnostics, "rc_policy": RC_POLICY}


def diagnostic_bundle(root, task_id, ordinal, scratch, exc):
    destination = Path(root) / "diagnostics" / f"{task_id}-{ordinal:05d}.tar.gz"
    destination.parent.mkdir(parents=True, exist_ok=True)
    scratch.mkdir(parents=True, exist_ok=True)
    atomic_json(scratch / "failure.json", {"error": str(exc), "traceback": traceback.format_exc(), "time": time.time()})
    keep = {
        "building.json", "model.epJSON", "model.idf", "compilation.json", "diagnostics.json",
        "eplusout.err", "eplusout.end", "process.log", "failure.json",
    }
    with tarfile.open(destination, "w:gz") as archive:
        for path in sorted(scratch.rglob("*")):
            if path.is_file() and path.name in keep:
                archive.add(path, arcname=path.relative_to(scratch))
    return destination


def hold_array():
    job = os.environ.get("SLURM_ARRAY_JOB_ID") or os.environ.get("SLURM_JOB_ID")
    if job and shutil.which("scontrol"):
        subprocess.run(["scontrol", "hold", job], check=False)


def pause_once(root, evidence):
    with exclusive_lock(Path(root) / "locks" / "pause.lock"):
        if not (Path(root) / "PAUSE").exists():
            atomic_json(Path(root) / "PAUSE", evidence)


def run_task(root, task_id, energyplus, *, timeout=1800, checkpoint_runs=8, checkpoint_seconds=300, rc=None, rows=None):
    """Run a task locally and publish durable shard checkpoints to shared storage."""
    from epsm_core import Building, Weather, run_building

    root = Path(root).resolve()
    if os.environ.get("SLURM_JOB_ID") and not os.environ.get("TMPDIR"):
        raise ValueError("Slurm worker requires node-local TMPDIR")
    info = plan_info(root)
    campaign_id = info["campaign_id"]
    catalog = json.loads((root / "weather.json").read_text())
    use_rc = rc_available() if rc is None else rc
    stop = False

    def drain(_signum, _frame):
        nonlocal stop
        stop = True

    previous = {sig: signal.signal(sig, drain) for sig in (signal.SIGTERM, signal.SIGUSR1)}
    try:
        with exclusive_lock(root / "locks" / f"{task_id}.lock"):
            rows = task_rows(root, task_id) if rows is None else rows
            if not rows:
                raise ValueError(f"Unknown or empty task: {task_id}")
            scratch_base = Path(os.environ.get("TMPDIR", "/tmp")).resolve()
            local_path = scratch_base / "epsm-shards" / f"{task_id}.duckdb"
            shard_path = root / "shards" / f"{task_id}.duckdb"
            local_path.parent.mkdir(parents=True, exist_ok=True)
            for stale in (local_path, Path(str(local_path) + ".wal")):
                stale.unlink(missing_ok=True)
            if shard_path.exists():
                shutil.copyfile(shard_path, local_path)
            try:
                runtime = runtime_identity(energyplus)
            except Exception as exc:
                pause_once(root, {"task_id": task_id, "error": str(exc), "time": time.time()})
                hold_array()
                raise
            shard = open_shard(local_path, task_id, campaign_id, runtime)
            completed = {r[0] for r in shard.execute("SELECT ordinal FROM results").fetchall()}
            weathers = {}
            added = unpublished = 0
            last_checkpoint = time.monotonic()

            def weather_for(weather_id):
                if weather_id not in weathers:
                    item = catalog[weather_id]
                    path = root / item["path"]
                    if file_sha256(path) != item["sha256"]:
                        raise ValueError(f"Frozen weather changed: {weather_id}")
                    weathers[weather_id] = Weather.from_epw(path, calendar_year=item["calendar_year"])
                return weathers[weather_id]

            def publish(drained=False):
                nonlocal shard, unpublished, last_checkpoint
                total = shard.execute("SELECT count(*) FROM results").fetchone()[0]
                if unpublished or not shard_path.exists():
                    shard.execute("CHECKPOINT")
                    shard.close()
                    publish_shard(local_path, shard_path)
                    shard = open_shard(local_path, task_id, campaign_id, runtime)
                    unpublished = 0
                    last_checkpoint = time.monotonic()
                atomic_json(
                    root / "receipts" / f"{task_id}.json",
                    {
                        "task_id": task_id, "assigned": len(rows), "complete": total, "added": added,
                        "shard": str(shard_path), "drained": drained, "rc": use_rc,
                        "checkpointed_at": time.time(),
                    },
                )
                return total

            for ordinal, case_id, sample_id, weather_id, building_json in rows:
                if stop or (root / "PAUSE").exists():
                    break
                if ordinal in completed:
                    continue
                scratch = scratch_base / f"epsm-{task_id}-{ordinal}-{os.getpid()}"
                if scratch.exists():
                    shutil.rmtree(scratch)
                started = time.monotonic()
                try:
                    building = Building.from_dict(json.loads(building_json))
                    building.identifier = sample_id
                    weather = weather_for(weather_id)
                    result = run_building(
                        building, weather=weather, directory=scratch / "energy", energyplus=energyplus,
                        timeout_seconds=timeout,
                    )
                    heating, cooling, dhw = validated_arrays(result)
                    elapsed = time.monotonic() - started
                    rc_arrays, rc_meta, rc_seconds = None, None, None
                    if use_rc:
                        rc_started = time.monotonic()
                        try:
                            rc_arrays, rc_meta = run_rc(building, weather, scratch / "rc")
                        except Exception as exc:  # noqa: BLE001 - RC evidence is optional; keep the EnergyPlus row.
                            rc_meta = {"error": str(exc), "rc_policy": RC_POLICY}
                        rc_seconds = time.monotonic() - rc_started
                    shard.execute("BEGIN")
                    shard.execute(
                        "INSERT INTO results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,current_timestamp)",
                        [
                            ordinal, case_id, result.run_id, canonical(result.scalars), canonical(result.diagnostics),
                            heating.tolist(), cooling.tolist(), dhw.tolist(),
                            rc_arrays[0].tolist() if rc_arrays else None,
                            rc_arrays[1].tolist() if rc_arrays else None,
                            canonical(rc_meta["scalars"]) if rc_arrays else None,
                            canonical({k: v for k, v in (rc_meta or {}).items() if k != "scalars"}) if rc_meta else None,
                            elapsed, rc_seconds,
                        ],
                    )
                    shard.execute("COMMIT")
                    added += 1
                    unpublished += 1
                    shutil.rmtree(scratch, ignore_errors=True)
                    if unpublished >= checkpoint_runs or time.monotonic() - last_checkpoint >= checkpoint_seconds:
                        publish()
                except Exception as exc:
                    try:
                        shard.execute("ROLLBACK")
                    except duckdb.Error:
                        pass
                    if unpublished:
                        publish()
                    bundle = diagnostic_bundle(root, task_id, ordinal, scratch, exc)
                    pause_once(
                        root,
                        {"task_id": task_id, "ordinal": ordinal, "error": str(exc), "diagnostic": str(bundle), "time": time.time()},
                    )
                    hold_array()
                    raise
            publish(drained=stop)
            shard.close()
            local_path.unlink(missing_ok=True)
            return json.loads((root / "receipts" / f"{task_id}.json").read_text())
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
