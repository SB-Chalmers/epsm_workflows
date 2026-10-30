# SPDX-License-Identifier: MIT
"""Slurm array submission with live array-size, quota and file-count gates."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import duckdb

from ..records import atomic_json, exclusive_lock
from .store import plan_info, prepare_production_tasks, status

STORAGE_ENTRY_LIMIT = 50000
FILE_HEADROOM = 5000
BYTE_HEADROOM = 500 * 1024**3


def walltime(seconds):
    minutes = (int(seconds) + 59) // 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}:00"


def array_spec(indices):
    """Compact sorted task indices into Slurm ranges to avoid command-size limits."""
    if not indices:
        raise ValueError("An array submission requires at least one task index")
    values = sorted(set(indices))
    ranges, start, previous = [], values[0], values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = value
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def _gib(value, unit):
    factors = {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}
    if unit not in factors:
        raise ValueError(f"Unsupported C3SE quota unit: {unit}")
    return int(float(value) * factors[unit])


def parse_c3se_quota(output, root):
    sections = re.split(r"(?=^(?:Home|Path):)", output, flags=re.MULTILINE)
    matches = []
    for section in sections:
        path_match = re.search(r"^(?:Home|Path):\s*(\S+)", section, re.MULTILINE)
        space = re.search(r"Space used:\s*([0-9.]+)([KMGT]iB)\s+Quota:\s*([0-9.]+)([KMGT]iB)", section)
        files = re.search(r"Files used:\s*(\d+)\s+Quota:\s*(\d+)", section)
        if not (path_match and space and files):
            continue
        quota_path = Path(path_match.group(1)).resolve()
        try:
            Path(root).resolve().relative_to(quota_path)
        except ValueError:
            continue
        used_files, quota_files = map(int, files.groups())
        matches.append(
            {
                "quota_path": str(quota_path),
                "free_bytes": _gib(space.group(3), space.group(4)) - _gib(space.group(1), space.group(2)),
                "free_files": quota_files - used_files,
                "source": "C3SE_quota",
            }
        )
    if not matches:
        raise ValueError(f"C3SE_quota did not report an allocation containing {root}")
    return max(matches, key=lambda item: len(item["quota_path"]))


def quota_capacity(root):
    command = shutil.which("C3SE_quota")
    if command:
        result = subprocess.run(
            [command], capture_output=True, text=True, check=False, timeout=20, env={**os.environ, "TERM": "xterm"}
        )
        if result.returncode:
            raise ValueError(f"C3SE_quota failed: {result.stderr.strip()}")
        return parse_c3se_quota(result.stdout, root)
    stat = os.statvfs(root)
    return {
        "quota_path": str(root),
        "free_bytes": stat.f_bavail * stat.f_frsize,
        "free_files": stat.f_favail,
        "source": "statvfs",
    }


def max_array_tasks():
    if not shutil.which("scontrol"):
        return 5000
    result = subprocess.run(["scontrol", "show", "config"], capture_output=True, text=True, check=False)
    match = re.search(r"^MaxArraySize\s*=\s*(\d+)", result.stdout, re.MULTILINE)
    return max(1, int(match.group(1)) - 1) if match else 5000


def job_active(job_id):
    if not job_id or not shutil.which("squeue"):
        return False
    result = subprocess.run(["squeue", "-h", "-j", str(job_id)], capture_output=True, text=True, check=False)
    return bool(result.stdout.strip())


def finished_task(root, task_id):
    receipt = Path(root) / "receipts" / f"{task_id}.json"
    if not receipt.exists():
        return False
    data = json.loads(receipt.read_text())
    return data["complete"] >= data["assigned"]


def worker_command(root, phase, energyplus, *, python=None):
    """The array element command; the index comes from Slurm at run time."""
    return (
        "OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 "
        f"{python or sys.executable} -m epsm_workflows.campaign worker {root} --phase {phase} "
        f'--index "$SLURM_ARRAY_TASK_ID" --energyplus {energyplus}'
    )


def submission(root, study, phase, energyplus, *, account, concurrency=None, python=None, max_tasks=None):
    """Build the sbatch command and receipt for every unfinished task of a phase.

    Pure apart from reading the plan: callers decide whether to execute it.
    """
    root = Path(root).resolve()
    execution = study.execution
    db = duckdb.connect(str(root / "plan.duckdb"), read_only=True)
    try:
        rows = db.execute(
            "SELECT task_index,task_id,walltime_seconds FROM tasks WHERE phase=? ORDER BY task_index", [phase]
        ).fetchall()
        expected_runs = db.execute("SELECT count(*) FROM runs").fetchone()[0]
    finally:
        db.close()
    missing = [row for row in rows if not finished_task(root, row[1])]
    if not missing:
        return None
    limit = max_tasks or max_array_tasks()
    if len(rows) > limit:
        raise ValueError(f"{len(rows)} tasks exceed the array capacity {limit}")
    # Shard, lock, receipt and log per task, plus WAL and an atomic receipt temporary.
    projected_files = 6 * len(rows) + 50
    storage_base = Path(os.environ.get("EPSM_STORAGE_BASE", str(root))).resolve()
    root.relative_to(storage_base)
    existing_entries = sum(len(d) + len(f) for _, d, f in os.walk(storage_base))
    hourly_bytes = expected_runs * 5 * 8760 * 8
    projected_bytes = 2 * hourly_bytes + (root / "plan.duckdb").stat().st_size + 5 * 1024**3
    capacity = quota_capacity(root)
    files_ok = capacity["free_files"] < 0 or capacity["free_files"] >= projected_files + FILE_HEADROOM
    quota = {
        **capacity,
        "projected_files": projected_files,
        "conservative_projected_bytes": projected_bytes,
        "storage_entries": existing_entries,
        "storage_entry_limit": STORAGE_ENTRY_LIMIT,
        "max_array_tasks": limit,
    }
    if not (capacity["free_bytes"] >= projected_bytes + BYTE_HEADROOM and files_ok) or (
        existing_entries + projected_files + FILE_HEADROOM > STORAGE_ENTRY_LIMIT
    ):
        raise ValueError(f"Submission would violate the storage headroom: {quota}")
    cap = min(
        concurrency or (execution.pilot_concurrency if phase == "pilot" else execution.production_concurrency),
        64 if phase == "pilot" else 512,
    )
    command = [
        "sbatch",
        "--parsable",
        f"--account={account}",
        f"--job-name=epsm-{study.name}-{phase}",
        "--nodes=1",
        "--ntasks=1",
        "--cpus-per-task=1",
        "--mem-per-cpu=4G",
        "--hint=nomultithread",
        "--signal=B:USR1@120",
        f"--time={walltime(max(row[2] for row in missing))}",
        f"--array={array_spec([row[0] for row in missing])}%{cap}",
        "--export=ALL",
        f"--chdir={root}",
        f"--output={root}/logs/{phase}-%A_%a.log",
    ]
    if execution.notification_email:
        command[8:8] = [f"--mail-user={execution.notification_email}", "--mail-type=END,FAIL"]
    if execution.excluded_nodes:
        command.insert(command.index("--export=ALL"), f"--exclude={','.join(execution.excluded_nodes)}")
    command += ["--wrap", worker_command(root, phase, energyplus, python=python)]
    receipt = {
        "phase": phase,
        "submitted": len(missing),
        "concurrency": cap,
        "walltime_seconds": max(row[2] for row in missing),
        "task_indices": [row[0] for row in missing],
        "excluded_nodes": list(execution.excluded_nodes),
        "quota": quota,
    }
    return command, receipt


def submit(root, study, phase, energyplus, *, account, concurrency=None, python=None, run=subprocess.run):
    """Submit the unfinished tasks of ``phase``; production is sized from the pilot first."""
    root = Path(root).resolve()
    with exclusive_lock(root / "locks" / f"submit-{phase}.lock"):
        if (root / "PAUSE").exists():
            raise ValueError("Campaign is paused")
        if phase == "production":
            prepare_production_tasks(root, study, max_tasks=max_array_tasks())
        record_path = root / "submissions" / f"{phase}.json"
        if record_path.exists() and job_active(json.loads(record_path.read_text()).get("job_id")):
            raise ValueError(f"An active {phase} submission already exists")
        built = submission(root, study, phase, energyplus, account=account, concurrency=concurrency, python=python)
        if built is None:
            return {"phase": phase, "submitted": 0, "message": "already complete"}
        command, receipt = built
        (root / "logs").mkdir(exist_ok=True)
        result = run(command, capture_output=True, text=True, check=True)
        receipt["job_id"] = result.stdout.strip().split(";")[0]
        atomic_json(record_path, receipt)
        return receipt


def pause(root):
    root = Path(root)
    atomic_json(root / "PAUSE", {"reason": "operator request"})
    held = []
    submissions = root / "submissions"
    for record in submissions.glob("*.json") if submissions.exists() else ():
        job = json.loads(record.read_text()).get("job_id")
        if job and job_active(job):
            subprocess.run(["scontrol", "hold", str(job)], check=False)
            held.append(job)
    return {"paused": True, "held_jobs": held, "status": status(root)}


def plan_summary(root):
    return {**plan_info(root), **status(root)}
