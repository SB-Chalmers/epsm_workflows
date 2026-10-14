"""Explicit SSH/rsync transport and Slurm submission for prepared evaluations.

This module creates no remote job until stage/submit are called. The caller
supplies the account, remote path and an environment with the pinned packages.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
import json
import re
import shlex
import subprocess

from .records import atomic_json, read_json


def _array(indices):
    ordered = sorted(set(indices))
    if not ordered:
        raise ValueError("No unfinished cases to submit")
    runs, start, end = [], ordered[0], ordered[0]
    for value in ordered[1:]:
        if value == end + 1:
            end = value
        else:
            runs.append(str(start) if start == end else f"{start}-{end}")
            start = end = value
    runs.append(str(start) if start == end else f"{start}-{end}")
    return ",".join(runs)


@dataclass
class VeraExecutor:
    host: str
    remote_directory: str
    python: str = "python3"
    command: object = field(default=subprocess.run, repr=False)

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+", self.host) or self.host.startswith("-"):
            raise ValueError("host must be an SSH hostname or user@hostname")
        if not PurePosixPath(self.remote_directory).is_absolute() or "\n" in self.remote_directory:
            raise ValueError("Use an absolute remote campaign directory")
        if "\n" in self.python:
            raise ValueError("Invalid Python executable")

    def _run(self, args):
        return self.command(args, check=True, text=True, capture_output=True)

    def _ssh(self, command):
        return self._run(["ssh", self.host, command])

    def stage(self, directory):
        """Transfer only immutable inputs; never overwrite remote result/attempt files."""
        root = Path(directory).resolve()
        manifest = read_json(root / "manifest.json")
        remote = self.remote_directory
        # Verify a previously staged plan before transferring files. The test is
        # performed remotely so a stale local result directory cannot replace it.
        check = (
            f"mkdir -p -- {shlex.quote(remote + '/cases')} {shlex.quote(remote + '/logs')}; "
            f"if test -f {shlex.quote(remote + '/manifest.json')}; then "
            f"{shlex.quote(self.python)} -c " + shlex.quote(
                "import json,sys; p=json.load(open(sys.argv[1])); "
                "assert p['id']==sys.argv[2], 'Remote plan differs'"
            ) + f" {shlex.quote(remote + '/manifest.json')} {shlex.quote(manifest['id'])}; fi"
        )
        self._ssh(check)
        self._run(["rsync", "--archive", "--protect-args", str(root / "cases") + "/",
                   f"{self.host}:{remote}/cases/"])
        self._run(["rsync", "--archive", "--protect-args", str(root / "assets") + "/",
                   f"{self.host}:{remote}/assets/"])
        self._run(["rsync", "--archive", "--protect-args", str(root / "manifest.json"),
                   f"{self.host}:{remote}/manifest.json"])
        return {"plan_id": manifest["id"], "host": self.host, "remote_directory": remote}

    def submit(self, directory, *, account, time_limit="01:00:00", cpus=1,
               memory="4G", max_concurrent=None, retry_failed=False):
        """Submit currently unfinished remote cases as a Slurm array.

        Call stage first. Concurrent duplicate submissions remain safe because
        each case acquires an execution lock; completed cases are never rerun.
        """
        if not re.fullmatch(r"[A-Za-z0-9_-]+", account):
            raise ValueError("Invalid Slurm account")
        if not re.fullmatch(r"(?:[0-9]+-)?[0-9]+:[0-9]{2}:[0-9]{2}", time_limit):
            raise ValueError("Use a Slurm time limit such as 01:00:00")
        if not re.fullmatch(r"[0-9]+[KMGTP]?", memory) or not isinstance(cpus, int) or cpus < 1:
            raise ValueError("Invalid Slurm resource request")
        if max_concurrent is not None and (not isinstance(max_concurrent, int) or max_concurrent < 1):
            raise ValueError("max_concurrent must be positive")
        root = Path(directory)
        manifest = read_json(root / "manifest.json")
        base = [self.python, "-m", "epsm_workflows.execution"]
        remote_status = json.loads(self._ssh(shlex.join(base + ["status", self.remote_directory])).stdout)
        if remote_status["plan_id"] != manifest["id"]:
            raise ValueError("Remote plan differs from local prepared evaluations")
        query = base + ["pending", self.remote_directory]
        if retry_failed:
            query.append("--retry-failed")
        indices = json.loads(self._ssh(shlex.join(query)).stdout)
        if not all(isinstance(i, int) and 0 <= i < len(manifest["case_ids"]) for i in indices):
            raise ValueError("Remote executor returned invalid case indices")
        if not indices:
            return {"plan_id": manifest["id"], "job_id": None, "submitted": 0}
        array = _array(indices) + (f"%{max_concurrent}" if max_concurrent else "")
        worker = shlex.join(base + ["run", self.remote_directory]) + ' --index "$SLURM_ARRAY_TASK_ID"'
        if retry_failed:
            worker += " --retry-failed"
        args = ["sbatch", "--parsable", f"--account={account}", f"--time={time_limit}",
                f"--cpus-per-task={cpus}", f"--mem={memory}", f"--array={array}",
                "--job-name=epsm-evaluation", f"--output={self.remote_directory}/logs/%A_%a.out",
                "--wrap", worker]
        response = self._ssh(shlex.join(args)).stdout.strip()
        job_id = response.split(";", 1)[0]
        if not job_id.isdigit():
            raise RuntimeError(f"Unexpected sbatch response: {response}")
        receipt = {"plan_id": manifest["id"], "job_id": job_id, "submitted": len(indices),
                   "indices": indices, "host": self.host, "remote_directory": self.remote_directory}
        atomic_json(root / "submissions" / f"{job_id}.json", receipt)
        return receipt

    def retrieve(self, directory):
        """Merge remote result/attempt records locally without deleting local files."""
        root = Path(directory).resolve()
        for name in ("results", "attempts", "artifacts"):
            (root / name).mkdir(parents=True, exist_ok=True)
            self._run(["rsync", "--archive", "--protect-args",
                       f"{self.host}:{self.remote_directory}/{name}/", str(root / name) + "/"])
        from .execution import status
        return status(root)

    def status(self, job_id):
        if not str(job_id).isdigit():
            raise ValueError("job_id must be numeric")
        return self._ssh(shlex.join(["squeue", "--noheader", "--jobs", str(job_id),
                                    "--format=%i %T %R"])).stdout
