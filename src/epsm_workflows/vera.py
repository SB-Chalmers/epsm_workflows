"""Explicit SSH/rsync transport and Slurm submission for prepared evaluations.

This module creates no remote job until stage/submit are called. The caller
supplies the account, remote path and an environment with the pinned packages.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from ._events import event
from ._inspection import inspection
from .records import atomic_json, exclusive_lock, read_json

logger = logging.getLogger(__name__)


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

    def __repr__(self):
        return inspection(
            type(self).__name__, host=self.host, remote_directory=self.remote_directory
        )

    __str__ = __repr__

    def summary(self):
        """Describe the configured endpoint without opening an SSH connection."""
        return {
            "host": self.host,
            "remote_directory": self.remote_directory,
            "python": self.python,
        }

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+", self.host) or self.host.startswith(
            "-"
        ):
            raise ValueError("host must be an SSH hostname or user@hostname")
        if (
            not PurePosixPath(self.remote_directory).is_absolute()
            or "\n" in self.remote_directory
        ):
            raise ValueError("Use an absolute remote campaign directory")
        if "\n" in self.python:
            raise ValueError("Invalid Python executable")

    def _run(self, args):
        event(
            logger,
            "study.transport.started",
            "Started a remote transport command",
            context={"worker_id": self.host},
            details={"transport": args[0]},
            level=logging.DEBUG,
        )
        return self.command(args, check=True, text=True, capture_output=True)

    def _ssh(self, command):
        return self._run(["ssh", self.host, command])

    def stage(self, directory):
        """Transfer only immutable inputs; never overwrite remote result/attempt files."""
        root = Path(directory).resolve()
        from .execution import validate_plan

        manifest = validate_plan(root)
        remote = self.remote_directory
        # Verify a previously staged plan before transferring files. The test is
        # performed remotely so a stale local result directory cannot replace it.
        check = (
            "mkdir -p -- "
            + " ".join(
                shlex.quote(remote + "/" + name)
                for name in ("cases", "logs", "results", "attempts", "artifacts")
            )
            + "; "
            f"if test -f {shlex.quote(remote + '/manifest.json')}; then "
            f"{shlex.quote(self.python)} -c "
            + shlex.quote(
                "import json,sys; p=json.load(open(sys.argv[1])); "
                "assert p['id']==sys.argv[2], 'Remote plan differs'"
            )
            + f" {shlex.quote(remote + '/manifest.json')} {shlex.quote(manifest['id'])}; fi"
        )
        self._ssh(check)
        self._run(
            [
                "rsync",
                "--archive",
                "--protect-args",
                str(root / "cases") + "/",
                f"{self.host}:{remote}/cases/",
            ]
        )
        self._run(
            [
                "rsync",
                "--archive",
                "--protect-args",
                str(root / "assets") + "/",
                f"{self.host}:{remote}/assets/",
            ]
        )
        self._run(
            [
                "rsync",
                "--archive",
                "--protect-args",
                str(root / "manifest.json"),
                f"{self.host}:{remote}/manifest.json",
            ]
        )
        event(
            logger,
            "study.staging.completed",
            "Staged prepared inputs on the remote host",
            context={"study_id": manifest["id"], "worker_id": self.host},
            details={"remote_directory": remote},
        )
        return {
            "plan_id": manifest["id"],
            "host": self.host,
            "remote_directory": remote,
        }

    def submit(
        self,
        directory,
        *,
        account,
        time_limit="01:00:00",
        cpus=1,
        memory="4G",
        max_concurrent=None,
        retry_failed=False,
    ):
        """Submit currently unfinished remote cases as a Slurm array.

        Call stage first. Concurrent duplicate submissions remain safe because
        each case acquires an execution lock; completed cases are never rerun.
        """
        if not re.fullmatch(r"[A-Za-z0-9_-]+", account):
            raise ValueError("Invalid Slurm account")
        if not re.fullmatch(r"(?:[0-9]+-)?[0-9]+:[0-9]{2}:[0-9]{2}", time_limit):
            raise ValueError("Use a Slurm time limit such as 01:00:00")
        if (
            not re.fullmatch(r"[0-9]+[KMGTP]?", memory)
            or not isinstance(cpus, int)
            or cpus < 1
        ):
            raise ValueError("Invalid Slurm resource request")
        if max_concurrent is not None and (
            not isinstance(max_concurrent, int) or max_concurrent < 1
        ):
            raise ValueError("max_concurrent must be positive")
        root = Path(directory)
        manifest = read_json(root / "manifest.json")
        base = [self.python, "-m", "epsm_workflows.execution"]
        remote_status = json.loads(
            self._ssh(shlex.join(base + ["status", self.remote_directory])).stdout
        )
        if remote_status["plan_id"] != manifest["id"]:
            raise ValueError("Remote plan differs from local prepared evaluations")
        query = base + ["pending", self.remote_directory]
        if retry_failed:
            query.append("--retry-failed")
        indices = json.loads(self._ssh(shlex.join(query)).stdout)
        if not all(
            isinstance(i, int) and 0 <= i < len(manifest["case_ids"]) for i in indices
        ):
            raise ValueError("Remote executor returned invalid case indices")
        if not indices:
            return {"plan_id": manifest["id"], "job_id": None, "submitted": 0}
        array = _array(indices) + (f"%{max_concurrent}" if max_concurrent else "")
        worker = (
            shlex.join(base + ["run", self.remote_directory])
            + ' --index "$SLURM_ARRAY_TASK_ID"'
        )
        if retry_failed:
            worker += " --retry-failed"
        args = [
            "sbatch",
            "--parsable",
            f"--account={account}",
            f"--time={time_limit}",
            f"--cpus-per-task={cpus}",
            f"--mem={memory}",
            f"--array={array}",
            "--job-name=epsm-evaluation",
            f"--output={self.remote_directory}/logs/%A_%a.out",
            "--wrap",
            worker,
        ]
        response = self._ssh(shlex.join(args)).stdout.strip()
        job_id = response.split(";", 1)[0]
        if not job_id.isdigit():
            raise RuntimeError(f"Unexpected sbatch response: {response}")
        receipt = {
            "plan_id": manifest["id"],
            "job_id": job_id,
            "submitted": len(indices),
            "indices": indices,
            "host": self.host,
            "remote_directory": self.remote_directory,
        }
        atomic_json(root / "submissions" / f"{job_id}.json", receipt)
        event(
            logger,
            "study.submission.completed",
            "Submitted the Slurm evaluation array",
            context={
                "study_id": manifest["id"],
                "worker_id": self.host,
                "slurm_job_id": job_id,
            },
            details={"submitted": len(indices)},
        )
        return receipt

    def retrieve(self, directory):
        """Validate remote evidence, then merge while preserving successful cases.

        Inputs must match the local plan. Every remote attempt is retained, but
        an existing local success remains the selected result. A remote success
        replaces a local failure; otherwise the later failed attempt is selected.
        Conflicting immutable attempt/artifact files are rejected before merging.
        Transfers use a temporary directory, so invalid downloads cannot replace
        local evidence. Remote paths recorded inside results remain unchanged.
        """
        from .execution import (
            _manifest,
            _read_attempt,
            _read_result,
            _write_record,
            status,
        )

        root = Path(directory).resolve()
        manifest = _manifest(root)
        with (
            exclusive_lock(root / ".retrieve.lock"),
            TemporaryDirectory(prefix=".retrieve-", dir=root) as temporary,
        ):
            incoming = Path(temporary)
            self._run(
                [
                    "rsync",
                    "--archive",
                    "--protect-args",
                    f"{self.host}:{self.remote_directory}/manifest.json",
                    str(incoming / "manifest.json"),
                ]
            )
            if _manifest(incoming) != manifest:
                raise ValueError("Remote plan differs from local prepared evaluations")
            for name in ("results", "attempts", "artifacts"):
                (incoming / name).mkdir()
                self._run(
                    [
                        "rsync",
                        "--archive",
                        "--protect-args",
                        f"{self.host}:{self.remote_directory}/{name}/",
                        str(incoming / name) + "/",
                    ]
                )

            # Verify every record and collision before publishing any evidence.
            attempts = {}
            for path in (incoming / "attempts").rglob("*"):
                if path.is_dir():
                    continue
                relative = path.relative_to(incoming / "attempts")
                if (
                    len(relative.parts) != 2
                    or not re.fullmatch(r"[0-9]{6}", relative.parts[0])
                    or not re.fullmatch(r"[a-f0-9]{32}\.json", relative.parts[1])
                ):
                    raise ValueError(f"Unexpected remote attempt path: {relative}")
                index = int(relative.parts[0])
                if not 0 <= index < len(manifest["case_ids"]):
                    raise ValueError(
                        "Remote attempt index is outside the prepared plan"
                    )
                attempt = _read_attempt(path, manifest, index)
                if attempt["id"] != path.stem:
                    raise ValueError(
                        "Remote attempt filename differs from its identity"
                    )
                attempts[(index, path.stem)] = attempt
            results = {}
            for path in (incoming / "results").iterdir():
                if not re.fullmatch(r"[0-9]{6}\.json", path.name):
                    raise ValueError(f"Unexpected remote result path: {path.name}")
                index = int(path.stem)
                if not 0 <= index < len(manifest["case_ids"]):
                    raise ValueError("Remote result index is outside the prepared plan")
                result = _read_result(path, manifest, index)
                if attempts.get((index, result["id"])) != result:
                    raise ValueError("Remote result has no matching retained attempt")
                results[index] = result

            files = [
                path
                for name in ("attempts", "artifacts")
                for path in (incoming / name).rglob("*")
                if not path.is_dir()
            ]
            for path in files:
                if path.is_symlink() or not path.resolve().is_relative_to(incoming):
                    raise ValueError(
                        "Retrieved files must stay inside the transferred evidence"
                    )
                target = root / path.relative_to(incoming)
                if target.exists() and target.read_bytes() != path.read_bytes():
                    # Interrupted attempts may advance to completion remotely.
                    # Keep the original observed running record alongside it.
                    if path.parts[-3] == "attempts":
                        index = int(path.parent.name)
                        prior = _read_attempt(target, manifest, index)
                        updated = attempts[(index, path.stem)]
                        if (
                            prior["status"] == "running"
                            and updated["status"] != "running"
                            and all(
                                updated.get(key) == value
                                for key, value in prior.items()
                                if key != "status"
                            )
                        ):
                            continue
                    raise ValueError(
                        f"Conflicting retrieved evidence: {path.relative_to(incoming)}"
                    )
            for index in results:
                path = root / "results" / f"{index:06d}.json"
                if path.exists():
                    _read_result(path, manifest, index)

            for path in files:
                target = root / path.relative_to(incoming)
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    # Same-filesystem no-overwrite publication preserves any
                    # evidence concurrently written by a local worker.
                    os.link(path, target)
                except FileExistsError:
                    if target.read_bytes() == path.read_bytes():
                        continue
                    index = (
                        int(path.parent.name) if path.parts[-3] == "attempts" else None
                    )
                    if index is not None:
                        prior = _read_attempt(target, manifest, index)
                        updated = attempts[(index, path.stem)]
                        if (
                            prior["status"] == "running"
                            and updated["status"] != "running"
                        ):
                            history = (
                                root
                                / "attempt_observations"
                                / path.parent.name
                                / path.name
                            )
                            _write_record(history, prior)
                            _write_record(target, updated)
                            continue
                    raise ValueError(
                        f"Conflicting retrieved evidence: {path.relative_to(incoming)}"
                    )
            for index, result in results.items():
                with exclusive_lock(root / "locks" / f"{index:06d}.lock"):
                    path = root / "results" / f"{index:06d}.json"
                    previous = (
                        _read_result(path, manifest, index) if path.exists() else None
                    )
                    if previous is None or (
                        previous["status"] != "succeeded"
                        and (
                            result["status"] == "succeeded"
                            or result.get("finished_at", "")
                            > previous.get("finished_at", "")
                        )
                    ):
                        _write_record(path, result)
        outcome = status(root)
        event(
            logger,
            "study.retrieval.completed",
            "Retrieved and merged remote evaluation evidence",
            context={"study_id": manifest["id"], "worker_id": self.host},
            details={key: value for key, value in outcome.items() if key != "plan_id"},
        )
        return outcome

    def status(self, job_id):
        if not str(job_id).isdigit():
            raise ValueError("job_id must be numeric")
        return self._ssh(
            shlex.join(
                ["squeue", "--noheader", "--jobs", str(job_id), "--format=%i %T %R"]
            )
        ).stdout
