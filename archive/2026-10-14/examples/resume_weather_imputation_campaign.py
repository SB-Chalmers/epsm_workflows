"""Resume immutable campaign site/years in at most three isolated processes.

Each child executes the original frozen runner with workers=1 and every
building, outage duration, seed and scope. Shared matrix bytes are read-only;
global reporting waits until all children have exited. Scientific calculations
and the three-engine concurrency ceiling remain fixed. Run natively with the
pinned interpreter, for example ``TZ=UTC0 python -m
examples.resume_weather_imputation_campaign --output output/...-v5``.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = Path("output/weather-imputation-campaign-2026-10-05-v5")


def sha(path):
    """Fingerprint large evidence without holding it in memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def dump(path, value):
    """Publish scheduler-owned metadata using a unique temporary name."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def frozen_matrix_dump(original, root, expected_sha):
    """Skip only a verified identical shared matrix publication in one child."""
    matrix = (Path(root) / "matrix.json").resolve()

    def write(path, value):
        if Path(path).resolve() == matrix:
            normalized = json.loads(json.dumps(value, default=str, allow_nan=False))
            if (
                sha(matrix) != expected_sha
                or json.loads(matrix.read_text()) != normalized
            ):
                raise ValueError("Shared immutable campaign matrix changed")
            return
        original(path, value)

    return write


def jobs(root):
    """Dispatch each eligible unfinished reference once, verifying finished years."""
    from examples.archive_weather_campaign_files import _preflight_year

    matrix = json.loads((root / "matrix.json").read_text())
    result, unavailable, complete = [], [], []
    seen = set()
    for site in matrix["sites"]:
        for year in matrix["years"]:
            key = (site["site_id"], year)
            if key in seen:
                raise ValueError("Duplicate site/year in immutable campaign matrix")
            seen.add(key)
            folder = root / "datasets" / key[0] / str(key[1])
            source = json.loads((folder / "sources/receipt.json").read_text())
            if source.get("status") not in {"ready", "unavailable"}:
                raise ValueError("Unknown reference source status")
            if source.get("status") != "ready":
                unavailable.append(key)
                continue
            audit_path = folder / "audit.json"
            audit = json.loads(audit_path.read_text()) if audit_path.exists() else {}
            if audit.get("full_matrix_site_year_complete"):
                _preflight_year(folder, root, set(matrix["buildings"]))
                if not audit.get("diagnostics_accepted") or any(
                    audit.get(k) != 0 for k in ("severe", "fatal")
                ):
                    raise ValueError("Completed site/year has unacceptable diagnostics")
                warnings = audit.get("warnings")
                if type(warnings) is not int or warnings < 0:
                    raise ValueError("Completed site/year has invalid warning counts")
                if warnings:
                    import pandas as pd

                    from examples import qualify_weather_campaign_results as Q

                    rows = pd.read_csv(
                        folder / "summary.csv", float_precision="round_trip"
                    )
                    _, _, qualification = Q.qualify_results(root, rows)
                    records = qualification["records"]
                    if any(
                        any(
                            record[k] != 0
                            for k in (
                                "core_severe_count",
                                "core_fatal_count",
                                "raw_severe_count",
                                "raw_fatal_count",
                            )
                        )
                        or "core_diagnostics_not_accepted" in record["reasons"]
                        for record in records
                    ):
                        raise ValueError(
                            "Completed site/year has rejected diagnostic evidence"
                        )
                    if (
                        sum(record["raw_warning_count"] for record in records)
                        != warnings
                    ):
                        raise ValueError(
                            "Completed warning audit differs from qualified evidence"
                        )
                complete.append(key)
            else:
                result.append(key)
    return result, unavailable, complete


def worker(output, site, year, matrix_sha):
    """Use the original runner and local site/year writers with shared reads."""
    from examples.weather_imputation_sensitivity import run_campaign as runner

    original_dump, original_report, original_argv = runner.dump, runner.report, sys.argv
    runner.dump = frozen_matrix_dump(original_dump, output, matrix_sha)
    runner.report = lambda _: None
    sys.argv = [
        str(Path(runner.__file__)),
        "--phase",
        "run",
        "--output",
        str(output),
        "--select-sites",
        site,
        "--select-years",
        str(year),
        "--workers",
        "1",
    ]
    try:
        runner.main()
    finally:
        runner.dump, runner.report, sys.argv = (
            original_dump,
            original_report,
            original_argv,
        )


def resume(output, *, processes=3):
    """Schedule bounded jobs, stop after failures, and report only joined results."""
    if not 1 <= processes <= 3:
        raise ValueError("Use one to three isolated processes")
    root = Path(output).resolve()
    os.chdir(REPOSITORY)
    try:
        logical_output = root.relative_to(REPOSITORY)
    except ValueError:
        logical_output = root
    environment = root / "execution-environment"
    environment.mkdir(exist_ok=True)
    lock_path = environment / "process-scheduler.lock"
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "Another campaign process scheduler owns this output"
            ) from exc
        from examples.weather_imputation_sensitivity import run_campaign as runner

        runner.pin_runtime(root)
        matrix_sha = sha(root / "matrix.json")
        selected, unavailable, complete = jobs(root)
        generation = (
            datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        )
        receipt_path = environment / f"process-resume-{generation}.json"
        receipt = {
            "status": "running",
            "created_utc": datetime.now(UTC).isoformat(),
            "scheduler_sha256": sha(Path(__file__)),
            "matrix_sha256": matrix_sha,
            "runtime_manifest_sha256": sha(root / "runtime/manifest.json"),
            "python": sys.version,
            "executable": sys.executable,
            "timezone": "UTC0",
            "maximum_concurrent_engines": processes,
            "output_argument": str(logical_output),
            "working_directory": str(REPOSITORY),
            "planned_jobs": selected,
            "source_unavailable": unavailable,
            "verified_previously_complete": complete,
            "results": [],
            "interpretation": "Execution scheduling only: unchanged frozen runner, packages, weather, evidence, buildings and calculations; three-engine ceiling; shared matrix read-only; original global report after all processes join",
        }
        dump(receipt_path, receipt)
        active, active_lock = {}, threading.Lock()
        cancelled = threading.Event()

        def cancel(signum, _frame):
            cancelled.set()
            with active_lock:
                for process in active.values():
                    if process.poll() is None:
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass

        previous_handlers = {
            sig: signal.signal(sig, cancel) for sig in (signal.SIGINT, signal.SIGTERM)
        }

        def execute(job):
            site, year = job
            path = environment / f"process-{site}-{year}-{generation}.log"
            command = [
                sys.executable,
                "-u",
                "-m",
                "examples.resume_weather_imputation_campaign",
                "--output",
                str(logical_output),
                "--worker",
                site,
                str(year),
                "--matrix-sha",
                matrix_sha,
            ]
            env = os.environ | {"TZ": "UTC0", "PYTHONDONTWRITEBYTECODE": "1"}
            with path.open("wb") as log:
                with active_lock:
                    if cancelled.is_set():
                        return {
                            "site_id": site,
                            "year": year,
                            "returncode": None,
                            "status": "cancelled_before_start",
                            "log": str(path),
                        }
                    process = subprocess.Popen(
                        command,
                        cwd=REPOSITORY,
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    active[job] = process
                print(f"START {site}/{year} pid={process.pid} log={path}", flush=True)
                code = process.wait()
                if code != 0:
                    # A failed Python child may have left an engine/compressor.
                    # Kill only the private process group owned by this job.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                with active_lock:
                    active.pop(job, None)
            return {
                "site_id": site,
                "year": year,
                "returncode": code,
                "status": "accepted" if code == 0 else "failed",
                "log": str(path),
                "log_sha256": sha(path),
            }

        failed = False
        try:
            remaining = iter(selected)
            with ThreadPoolExecutor(max_workers=processes) as pool:
                pending = {}
                for job in list(selected)[:processes]:
                    next(remaining)
                    pending[pool.submit(execute, job)] = job
                while pending:
                    done, _ = wait(pending, timeout=30, return_when=FIRST_COMPLETED)
                    for future in done:
                        job = pending.pop(future)
                        result = future.result()
                        receipt["results"].append(result)
                        failed |= result["returncode"] != 0
                        print(
                            f"END {job[0]}/{job[1]} returncode={result['returncode']}",
                            flush=True,
                        )
                    if not failed and not cancelled.is_set():
                        while len(pending) < processes:
                            job = next(remaining, None)
                            if job is None:
                                break
                            pending[pool.submit(execute, job)] = job
                    dump(receipt_path, receipt)
            if failed or cancelled.is_set():
                receipt["status"] = "interrupted"
                raise RuntimeError(
                    "Campaign batch failed or was stopped; accepted results retained"
                )
            if sha(root / "matrix.json") != matrix_sha:
                raise ValueError("Immutable matrix bytes changed during execution")
            unfinished, unavailable, complete = jobs(root)
            if unfinished:
                raise ValueError(
                    "Successful workers left incomplete site/year evidence"
                )
            runner.report(root)
            receipt["status"] = "complete"
            receipt["verified_completed_site_years"] = complete
            receipt["source_unavailable"] = unavailable
            receipt["coverage_sha256"] = sha(root / "coverage.json")
            print(root / "report.html", flush=True)
        except BaseException:
            if receipt["status"] == "running":
                receipt["status"] = "failed"
            raise
        finally:
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
            receipt["updated_utc"] = datetime.now(UTC).isoformat()
            dump(receipt_path, receipt)
    return receipt


def main():
    """Resume an existing frozen campaign, never create a different matrix."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--processes", type=int, default=3)
    parser.add_argument("--worker", nargs=2, metavar=("SITE", "YEAR"))
    parser.add_argument("--matrix-sha")
    args = parser.parse_args()
    if args.worker:
        if args.matrix_sha is None:
            parser.error("Internal worker requires --matrix-sha")
        worker(args.output, args.worker[0], int(args.worker[1]), args.matrix_sha)
    else:
        resume(args.output, processes=args.processes)


if __name__ == "__main__":
    main()
