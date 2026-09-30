"""One command line for preparing, planning and operating EPSM campaigns."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import duckdb

from epsm_core import EnergyPlusRunner

from .campaign import (
    CampaignCase,
    atomic_json,
    create_plan,
    digest,
    exclusive_lock,
    file_sha256,
    finalize,
    preflight,
    prepare_production_tasks,
    require_preflight,
    run_task,
    status,
    task_rows,
)
from .surrogate.design import BOUNDS, design_cases, load_climates, rows
from .surrogate.geometry import select_templates
from .surrogate.weather import prepare_weather

CORE_COMMIT = "3878a30b63fc997a0d65cd0325675acef35851b3"


def _read(path):
    return json.loads(Path(path).read_text())


def _git(repo: Path, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _sources(root: Path, package: str):
    source = root / "src" / package
    hashes = {
        str(path.relative_to(root)): file_sha256(path)
        for path in sorted(source.rglob("*.py"))
    }
    if package == "epsm_workflows":
        for relative in (
            "campaigns/vera.sh",
            "campaigns/progress_mail.py",
            "campaigns/vera_60000.json",
        ):
            artifact = root / relative
            if artifact.is_file():
                hashes[relative] = file_sha256(artifact)
    return hashes


def load_config(path) -> dict:
    path = Path(path).resolve()
    config = _read(path)
    if config.get("schema_version") != 2 or set(config["bounds"]) != set(BOUNDS):
        raise ValueError("Unsupported configuration schema or parameter set")
    if config.get("seed") != 42 or len(config.get("cities", [])) != 5:
        raise ValueError("The Vera study requires seed 42 and exactly five sites")
    if config.get("core_commit") != CORE_COMMIT:
        raise ValueError(f"The campaign must freeze epsm_core commit {CORE_COMMIT}")
    notification_email = config.get("notification_email", "")
    if "@" not in notification_email or notification_email.startswith("@"):
        raise ValueError("A campaign notification_email is required")
    repo = path.parent.parent
    for key in ("core_repository", "smhi2epw_repository", "templates"):
        config[key] = str((repo / config[key]).resolve())
    for city in config["cities"]:
        for key in ("tmy", "amy", "amy_provenance"):
            if city.get(key):
                city[key] = str((repo / city[key]).resolve())
    config["config_source"] = str(path)
    return config


def prepare(config_path, root) -> dict:
    root = Path(root).resolve()
    config = load_config(config_path)
    if root.exists() and any(root.iterdir()):
        existing = root / "config.json"
        if not existing.exists() or _read(existing) != config:
            raise ValueError(
                "Preparation target is non-empty or has a different configuration"
            )
    root.mkdir(parents=True, exist_ok=True)
    core = Path(config["core_repository"])
    if not (core / "src" / "epsm_core").is_dir():
        raise ValueError(f"epsm_core source is missing from {core}")
    if (core / ".git").exists():
        if _git(core, "rev-parse", "HEAD") != CORE_COMMIT:
            raise ValueError(f"epsm_core must be frozen at commit {CORE_COMMIT}")
        clean = subprocess.run(
            [
                "git",
                "-C",
                str(core),
                "diff",
                "--quiet",
                "HEAD",
                "--",
                "src",
                "pyproject.toml",
            ],
            check=False,
        ).returncode
        if clean:
            raise ValueError("epsm_core source checkout is dirty")
    atomic_json(root / "config.json", config)
    templates_source = Path(config["templates"])
    if templates_source.is_file():
        templates = _read(templates_source)
    else:
        templates = select_templates(templates_source)
    if len(templates) != 10:
        raise ValueError(f"Expected ten footprint templates, found {len(templates)}")
    atomic_json(root / "templates.json", templates)
    weather = prepare_weather(config, root)
    receipt = {
        "state": "prepared",
        "sites": sorted(weather),
        "weather_files": sum(len(kinds) for kinds in weather.values()),
        "templates": len(templates),
    }
    atomic_json(root / "prepare.json", receipt)
    return receipt


def _manifest(root: Path, energyplus: str):
    config = _read(root / "config.json")
    workflow = Path(__file__).resolve().parents[2]
    core = Path(config["core_repository"])
    runner = EnergyPlusRunner(energyplus)
    workflow_sources = _sources(workflow, "epsm_workflows")
    packages = {}
    for name in ("epsm_core", "epsm_workflows", "duckdb", "numpy", "scipy"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = "source-checkout"
    core_commit = config["core_commit"]
    return {
        "release_id": f"{core_commit[:7]}-{digest(workflow_sources)[:12]}",
        "campaign_name": config["campaign_name"],
        "configuration": config,
        "configuration_hash": digest(config),
        "core_commit": core_commit,
        "core_sources": _sources(core, "epsm_core"),
        "workflow_sources": workflow_sources,
        "weather": _read(root / "weather.json"),
        "templates_sha256": file_sha256(root / "templates.json"),
        "engine_sha256": runner.compiler.engine_hash,
        "schema_sha256": runner.compiler.schema_hash,
        "compiler_sha256": runner.compiler.compiler_hash,
        "dependencies": packages,
    }


def _expand_design(payload):
    return design_cases(*payload)


def plan(root, energyplus: str, workers: int = 1) -> dict:
    root = Path(root).resolve()
    config = _read(root / "config.json")
    templates = _read(root / "templates.json")
    weather = _read(root / "weather.json")

    climates = load_climates(config, weather)

    def selected_designs():
        yield from rows(config, templates)

    def expanded():
        payloads = ((config, design, climates) for design in selected_designs())
        if workers == 1:
            for payload in payloads:
                yield _expand_design(payload)
            return
        with ProcessPoolExecutor(max_workers=workers) as pool:
            yield from pool.map(_expand_design, payloads, chunksize=4)

    def campaign_cases():
        ordinal = 0
        for batch in expanded():
            for spec, metadata in batch:
                yield CampaignCase(
                    ordinal=ordinal,
                    sample_id=spec.identifier,
                    group_id=metadata["group_id"],
                    spec=spec,
                    inputs=metadata["requested"],
                    metadata=metadata,
                )
                ordinal += 1

    return create_plan(
        root, config, campaign_cases(), weather, _manifest(root, energyplus)
    )


def _task_id(root: Path, phase: str, index: int):
    db = duckdb.connect(str(root / "plan.duckdb"), read_only=True)
    row = db.execute(
        "SELECT task_id FROM tasks WHERE phase=? AND task_index=?", [phase, index]
    ).fetchone()
    db.close()
    if not row:
        raise ValueError(f"Unknown {phase} task index {index}")
    return row[0]


def _finished_task(root: Path, task_id: str) -> bool:
    db = duckdb.connect(str(root / "plan.duckdb"), read_only=True)
    assigned = db.execute(
        "SELECT count(*) FROM task_members WHERE task_id=?", [task_id]
    ).fetchone()[0]
    db.close()
    shard = root / "shards" / f"{task_id}.duckdb"
    if not shard.exists():
        return False
    db = duckdb.connect(str(shard), read_only=True)
    complete = db.execute("SELECT count(*) FROM results").fetchone()[0]
    db.close()
    return complete == assigned


def _active(job_id: str) -> bool:
    if not job_id or not shutil.which("squeue"):
        return False
    result = subprocess.run(
        ["squeue", "-h", "-j", job_id], capture_output=True, text=True, check=False
    )
    return bool(result.stdout.strip())


def _walltime(seconds: int):
    minutes = (seconds + 59) // 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}:00"


def _array_spec(indices: list[int]) -> str:
    """Compact sorted task indices into Slurm ranges to avoid command-size limits."""
    if not indices:
        raise ValueError("An array submission requires at least one task index")
    values = sorted(set(indices))
    ranges = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = value
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def _quota_headroom_ok(
    free_bytes: int,
    free_files: int,
    projected_bytes: int,
    projected_files: int,
) -> bool:
    byte_headroom = 500 * 1024**3
    files_ok = free_files < 0 or free_files >= projected_files + 5000
    return free_bytes >= projected_bytes + byte_headroom and files_ok


def _gib(value: str, unit: str) -> int:
    factors = {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}
    if unit not in factors:
        raise ValueError(f"Unsupported C3SE quota unit: {unit}")
    return int(float(value) * factors[unit])


def _parse_c3se_quota(output: str, root: Path) -> dict:
    sections = re.split(r"(?=^(?:Home|Path):)", output, flags=re.MULTILINE)
    matches = []
    for section in sections:
        path_match = re.search(r"^(?:Home|Path):\s*(\S+)", section, re.MULTILINE)
        space_match = re.search(
            r"Space used:\s*([0-9.]+)([KMGT]iB)\s+Quota:\s*([0-9.]+)([KMGT]iB)",
            section,
        )
        files_match = re.search(
            r"Files used:\s*(\d+)\s+Quota:\s*(\d+)", section
        )
        if not (path_match and space_match and files_match):
            continue
        quota_path = Path(path_match.group(1)).resolve()
        try:
            root.relative_to(quota_path)
        except ValueError:
            continue
        used_bytes = _gib(space_match.group(1), space_match.group(2))
        quota_bytes = _gib(space_match.group(3), space_match.group(4))
        used_files, quota_files = map(int, files_match.groups())
        matches.append(
            {
                "quota_path": str(quota_path),
                "free_bytes": quota_bytes - used_bytes,
                "free_files": quota_files - used_files,
                "used_bytes": used_bytes,
                "quota_bytes": quota_bytes,
                "used_files": used_files,
                "quota_files": quota_files,
                "source": "C3SE_quota",
            }
        )
    if not matches:
        raise ValueError(f"C3SE_quota did not report an allocation containing {root}")
    return max(matches, key=lambda item: len(item["quota_path"]))


def _quota_capacity(root: Path) -> dict:
    command = shutil.which("C3SE_quota")
    if command:
        environment = os.environ.copy()
        environment["TERM"] = "xterm"
        result = subprocess.run(
            [command],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
            env=environment,
        )
        if result.returncode:
            raise ValueError(f"C3SE_quota failed: {result.stderr.strip()}")
        return _parse_c3se_quota(result.stdout, root)
    stat = os.statvfs(root)
    return {
        "quota_path": str(root),
        "free_bytes": stat.f_bavail * stat.f_frsize,
        "free_files": stat.f_favail,
        "source": "statvfs",
    }


def _max_array_tasks() -> int:
    if not shutil.which("scontrol"):
        return 5000
    result = subprocess.run(
        ["scontrol", "show", "config"], capture_output=True, text=True, check=False
    )
    match = re.search(r"^MaxArraySize\s*=\s*(\d+)", result.stdout, re.MULTILINE)
    return max(1, int(match.group(1)) - 1) if match else 5000


def submit(
    root,
    phase: str,
    energyplus: str,
    account: str,
    concurrency: int | None,
    excluded_nodes: list[str] | None = None,
) -> dict:
    root = Path(root).resolve()
    config = _read(root / "config.json")
    notification_email = config["notification_email"]
    execution = config.get("execution", {})
    exclusions = list(dict.fromkeys(
        [*execution.get("excluded_nodes", []), *(excluded_nodes or [])]
    ))
    with exclusive_lock(root / "locks" / f"submit-{phase}.lock"):
        if (root / "PAUSE").exists():
            raise ValueError("Campaign is paused")
        require_preflight(root, energyplus)
        if phase == "production":
            prepare_production_tasks(root, max_tasks=_max_array_tasks())
        record_path = root / "submissions" / f"{phase}.json"
        if record_path.exists() and _active(_read(record_path).get("job_id", "")):
            raise ValueError(f"An active {phase} submission already exists")
        db = duckdb.connect(str(root / "plan.duckdb"), read_only=True)
        rows = db.execute(
            "SELECT task_index,task_id,walltime_seconds FROM tasks WHERE phase=? ORDER BY task_index",
            [phase],
        ).fetchall()
        expected_runs = db.execute("SELECT count(*) FROM runs").fetchone()[0]
        db.close()
        missing = [
            (index, task_id, walltime)
            for index, task_id, walltime in rows
            if not _finished_task(root, task_id)
        ]
        if not missing:
            return {"phase": phase, "submitted": 0, "message": "already complete"}
        max_array_tasks = _max_array_tasks()
        if len(rows) > max_array_tasks:
            raise ValueError(
                f"{len(rows)} tasks exceed Vera MaxArraySize capacity {max_array_tasks}"
            )
        # Shard, lock, receipt and log, plus WAL and an atomic receipt temporary.
        projected_files = 6 * len(rows) + 50
        storage_base = Path(os.environ.get("EPSM_STORAGE_BASE", str(root))).resolve()
        root.relative_to(storage_base)
        existing_entries = sum(
            len(directories) + len(files)
            for _, directories, files in os.walk(storage_base)
        )
        hourly_bytes = expected_runs * 3 * 8760 * 8
        projected_bytes = (
            2 * hourly_bytes + (root / "plan.duckdb").stat().st_size + 5 * 1024**3
        )
        capacity = _quota_capacity(root)
        free_bytes = capacity["free_bytes"]
        free_files = capacity["free_files"]
        quota = {
            **capacity,
            "free_bytes": free_bytes,
            "free_files": free_files,
            "file_availability_reported": free_files >= 0,
            "conservative_projected_bytes": projected_bytes,
            "projected_files": projected_files,
            "required_byte_headroom": 500 * 1024**3,
            "required_file_headroom": 5000,
            "max_array_tasks": max_array_tasks,
            "storage_base": str(storage_base),
            "storage_entries": existing_entries,
            "storage_entry_limit": 50000,
            "hourly_array_bytes_uncompressed": hourly_bytes,
            "size_estimate_kind": "peak allowance for shards plus final database",
        }
        atomic_json(root / "quota-check.json", quota)
        if not _quota_headroom_ok(
            free_bytes, free_files, projected_bytes, projected_files
        ) or existing_entries + projected_files + 5000 > 50000:
            raise ValueError("Submission would violate 500 GiB / 5,000-file headroom")
        indices = _array_spec([row[0] for row in missing])
        cap = min(
            concurrency or (64 if phase == "pilot" else 128),
            64 if phase == "pilot" else 512,
        )
        walltime = max(row[2] for row in missing)
        command = [
            "sbatch",
            "--parsable",
            f"--account={account}",
            f"--job-name=epsm-{phase}",
            "--nodes=1",
            "--ntasks=1",
            "--cpus-per-task=1",
            "--mem-per-cpu=4G",
            "--hint=nomultithread",
            "--signal=B:USR1@120",
            f"--mail-user={notification_email}",
            "--mail-type=END,FAIL",
            f"--time={_walltime(walltime)}",
            f"--array={indices}%{cap}",
            "--export=ALL",
            f"--chdir={Path(__file__).resolve().parents[2]}",
            f"--output={root}/logs/{phase}-%A_%a.log",
            "--wrap",
            "OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 "
            f"{sys.executable} -m epsm_workflows.cli worker {root} --phase {phase} "
            f"--index $SLURM_ARRAY_TASK_ID --energyplus {energyplus}",
        ]
        if exclusions:
            command.insert(command.index("--export=ALL"), f"--exclude={','.join(exclusions)}")
        (root / "logs").mkdir(exist_ok=True)
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        job_id = result.stdout.strip().split(";")[0]
        record = {
            "phase": phase,
            "job_id": job_id,
            "submitted": len(missing),
            "concurrency": cap,
            "walltime_seconds": walltime,
            "task_indices": [row[0] for row in missing],
            "notification_email": notification_email,
            "mail_type": "END,FAIL",
            "excluded_nodes": exclusions,
            "checkpoint_runs": int(execution.get("checkpoint_runs", 8)),
            "checkpoint_seconds": float(execution.get("checkpoint_seconds", 300)),
        }
        atomic_json(record_path, record)
        return record


def pause(root) -> dict:
    root = Path(root)
    receipt = {"reason": "operator request"}
    atomic_json(root / "PAUSE", receipt)
    held = []
    for record in (
        (root / "submissions").glob("*.json") if (root / "submissions").exists() else ()
    ):
        job = _read(record).get("job_id")
        if job and _active(job):
            subprocess.run(["scontrol", "hold", job], check=False)
            held.append(job)
    return {**receipt, "held_jobs": held}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare")
    p.add_argument("config", type=Path)
    p.add_argument("campaign", type=Path)
    p = commands.add_parser("plan")
    p.add_argument("campaign", type=Path)
    p.add_argument("--energyplus", required=True)
    p.add_argument("--workers", type=int, default=min(os.cpu_count() or 1, 8))
    p = commands.add_parser("preflight")
    p.add_argument("campaign", type=Path)
    p.add_argument("--energyplus", required=True)
    p.add_argument("--workers", type=int, default=min(os.cpu_count() or 1, 8))
    p = commands.add_parser("submit")
    p.add_argument("campaign", type=Path)
    p.add_argument("--phase", choices=("pilot", "production"), required=True)
    p.add_argument("--energyplus", required=True)
    p.add_argument("--account", default="c3se2026-1-13")
    p.add_argument("--concurrency", type=int)
    p.add_argument("--exclude-node", action="append", default=[])
    p = commands.add_parser("worker", help=argparse.SUPPRESS)
    p.add_argument("campaign", type=Path)
    p.add_argument("--phase", required=True)
    p.add_argument("--index", required=True, type=int)
    p.add_argument("--energyplus", required=True)
    p.add_argument("--timeout", type=float, default=1800)
    p = commands.add_parser("status")
    p.add_argument("campaign", type=Path)
    p = commands.add_parser("pause")
    p.add_argument("campaign", type=Path)
    p = commands.add_parser("finalize")
    p.add_argument("campaign", type=Path)
    p.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        result = prepare(args.config, args.campaign)
    elif args.command == "plan":
        result = plan(args.campaign, args.energyplus, args.workers)
    elif args.command == "preflight":
        result = preflight(args.campaign, args.energyplus, args.workers)
    elif args.command == "submit":
        result = submit(
            args.campaign,
            args.phase,
            args.energyplus,
            args.account,
            args.concurrency,
            args.exclude_node,
        )
    elif args.command == "worker":
        config = _read(Path(args.campaign) / "config.json")
        jitter = float(config.get("execution", {}).get("startup_jitter_seconds", 0))
        if os.environ.get("SLURM_JOB_ID") and jitter > 0:
            # Deterministic spreading keeps plan reads and initial shard copies off one instant.
            import time

            time.sleep(((args.index * 2654435761) % 1000) / 1000 * jitter)
        task, rows = task_rows(args.campaign, args.phase, args.index)
        result = run_task(
            args.campaign, task, args.energyplus, args.timeout, rows=rows
        )
    elif args.command == "status":
        result = status(args.campaign)
    elif args.command == "pause":
        result = pause(args.campaign)
    else:
        result = finalize(args.campaign, args.destination)
    print(json.dumps(result, indent=2, default=str))
    if args.command == "preflight" and result["state"] != "passed":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
