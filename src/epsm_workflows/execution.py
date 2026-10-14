"""Prepared evaluations with local execution, immutable inputs and attempt records.

The same on-disk plan is read by local workers and Slurm array workers. Scientific
evaluation is a named public callable receiving one JSON-compatible payload.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import import_module
import json
from pathlib import Path
import shutil
import traceback
from uuid import uuid4

from .records import atomic_json, digest, exclusive_lock, json_value, read_json


@dataclass(frozen=True)
class PreparedEvaluation:
    payload: dict
    inputs: dict = field(default_factory=dict)
    name: str = ""
    files: dict = field(default_factory=dict)

    @property
    def id(self):
        return self.to_dict()["id"]

    def to_dict(self):
        import hashlib
        assets = {}
        for name, filename in self.files.items():
            path = Path(filename)
            checksum = hashlib.sha256(path.read_bytes()).hexdigest()
            assets[name] = {"sha256": checksum, "path": f"assets/{checksum}/{path.name}"}
        value = {"payload": json_value(self.payload), "inputs": json_value(self.inputs),
                 "name": self.name, "assets": assets}
        return {"id": digest(value), **value}


def prepare(directory, cases, *, handler="epsm_workflows.integration:evaluate", environment=None):
    """Freeze cases and evaluator/environment identity without running simulations.

    Supply package/source revisions in environment to bind a plan to its runtime.
    Existing plans are immutable; repeated identical preparation is harmless.
    """
    root = Path(directory)
    if ":" not in handler:
        raise ValueError("handler must be an importable module:function")
    supplied = list(cases)
    cases = [case.to_dict() for case in supplied]
    if not cases or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Prepared cases need nonempty, unique identities; name repeated design rows")
    manifest = {"schema": 1, "handler": handler, "environment": json_value(environment or {}),
                "case_ids": [case["id"] for case in cases]}
    manifest["id"] = digest(manifest)
    with exclusive_lock(root / ".prepare.lock"):
        path = root / "manifest.json"
        if path.exists():
            if read_json(path) != manifest:
                raise ValueError("An existing prepared plan is immutable")
            return manifest
        for name in ("assets", "results", "attempts", "artifacts"):
            (root / name).mkdir(parents=True, exist_ok=True)
        for index, (case, original) in enumerate(zip(cases, supplied)):
            for name, asset in case["assets"].items():
                target = root / asset["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original.files[name], target)
                _verify_asset(root, asset)
            atomic_json(root / "cases" / f"{index:06d}.json", case)
        atomic_json(path, manifest)
    return manifest


def _manifest(root):
    value = read_json(Path(root) / "manifest.json")
    if value["id"] != digest({key: data for key, data in value.items() if key != "id"}):
        raise ValueError("Prepared manifest has changed")
    return value


def _verify_asset(root, asset):
    import hashlib
    root = Path(root).resolve()
    path = (root / asset["path"]).resolve()
    if not path.is_relative_to(root / "assets"):
        raise ValueError("Asset path must stay inside the prepared assets directory")
    if hashlib.sha256(path.read_bytes()).hexdigest() != asset["sha256"]:
        raise ValueError(f"Prepared asset changed: {path.name}")
    return path


def _case(root, index):
    root = Path(root)
    manifest = _manifest(root)
    if not 0 <= index < len(manifest["case_ids"]):
        raise IndexError(index)
    case = read_json(root / "cases" / f"{index:06d}.json")
    identity = digest({key: case[key] for key in ("payload", "inputs", "name", "assets")})
    if case["id"] != identity or identity != manifest["case_ids"][index]:
        raise ValueError(f"Prepared case {index} has changed")
    return manifest, case


def _payload(root, case, attempt_id):
    assets = {name: str(_verify_asset(root, asset)) for name, asset in case["assets"].items()}
    def resolve(value):
        if isinstance(value, dict):
            if set(value) == {"$asset"}:
                return assets[value["$asset"]]
            return {key: resolve(data) for key, data in value.items()}
        if isinstance(value, list):
            return [resolve(data) for data in value]
        return value
    value = resolve(case["payload"])
    if "_execution" in value:
        raise ValueError("_execution is reserved for executor context")
    value["_execution"] = {"directory": str(Path(root).resolve()), "case_id": case["id"],
                            "attempt_id": attempt_id}
    return value


def _handler(reference):
    module, name = reference.split(":", 1)
    value = getattr(import_module(module), name)
    if not callable(value):
        raise TypeError(f"Evaluation handler is not callable: {reference}")
    return value


def _write_record(path, value):
    atomic_json(path, {**value, "record_sha256": digest(value)})


def _read_result(path, manifest, index):
    value = read_json(path)
    checksum = value.pop("record_sha256", None)
    if checksum != digest(value):
        raise ValueError("Stored result content has changed or is incomplete")
    if (value.get("plan_id") != manifest["id"]
            or value.get("case_id") != manifest["case_ids"][index]
            or value.get("index") != index):
        raise ValueError("Stored result belongs to different prepared inputs")
    if value.get("status") not in {"succeeded", "failed"}:
        raise ValueError("Stored result is not a completed attempt")
    if value["status"] == "succeeded" and "result" not in value:
        raise ValueError("Successful attempt has no result")
    return value


def validate_plan(directory):
    """Verify prepared identities and file contents before moving/executing a plan."""
    manifest = _manifest(directory)
    for index in range(len(manifest["case_ids"])):
        _, case = _case(directory, index)
        for asset in case["assets"].values():
            _verify_asset(directory, asset)
    return manifest


def read_results(directory, *, require_success=True):
    """Read verified results in prepared row order; incomplete matrices are rejected."""
    root = Path(directory)
    manifest = _manifest(root)
    rows = []
    for index in range(len(manifest["case_ids"])):
        path = root / "results" / f"{index:06d}.json"
        row = _read_result(path, manifest, index) if path.exists() else None
        if require_success and (row is None or row["status"] != "succeeded"):
            raise ValueError(f"Prepared case {index} has no successful result")
        rows.append(row)
    return tuple(rows)


def run_case(directory, index, *, retry_failed=False):
    """Run one immutable case at most once successfully; retain every attempt."""
    root = Path(directory)
    manifest, case = _case(root, index)
    result_path = root / "results" / f"{index:06d}.json"
    with exclusive_lock(root / "locks" / f"{index:06d}.lock"):
        if result_path.exists():
            previous = _read_result(result_path, manifest, index)
            if previous["status"] == "succeeded" or not retry_failed:
                return previous
        attempt_id = uuid4().hex
        attempt = {"id": attempt_id, "plan_id": manifest["id"], "case_id": case["id"],
                   "index": index, "status": "running", "started_at": datetime.now(timezone.utc).isoformat()}
        attempt_path = root / "attempts" / f"{index:06d}" / f"{attempt_id}.json"
        _write_record(attempt_path, attempt)
        try:
            result = _handler(manifest["handler"])(_payload(root, case, attempt_id))
            attempt.update(status="succeeded", result=json_value(result))
        except Exception as error:
            attempt.update(status="failed", error={"type": type(error).__name__, "message": str(error),
                                                   "traceback": traceback.format_exc()})
        attempt["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_record(attempt_path, attempt)
        _write_record(result_path, attempt)
        return attempt


def pending(directory, *, retry_failed=False):
    """Return unfinished, currently unlocked case indices."""
    root = Path(directory)
    manifest = _manifest(root)
    indices = []
    for index in range(len(manifest["case_ids"])):
        path = root / "results" / f"{index:06d}.json"
        if path.exists():
            stored = _read_result(path, manifest, index)
            if stored["status"] == "succeeded" or not retry_failed:
                continue
        try:
            with exclusive_lock(root / "locks" / f"{index:06d}.lock"):
                indices.append(index)
        except RuntimeError:
            continue
    return indices


def status(directory):
    root = Path(directory)
    manifest = _manifest(root)
    counts = {"plan_id": manifest["id"], "total": len(manifest["case_ids"]),
              "succeeded": 0, "failed": 0, "unfinished": 0}
    for index in range(counts["total"]):
        path = root / "results" / f"{index:06d}.json"
        if path.exists():
            stored = _read_result(path, manifest, index)
            counts[stored["status"]] += 1
        else:
            counts["unfinished"] += 1
    return counts


def run_local(directory, *, workers=1, retry_failed=False):
    """Resume pending cases locally; external simulators may run concurrently."""
    if not isinstance(workers, int) or workers < 1:
        raise ValueError("workers must be a positive integer")
    indices = pending(directory, retry_failed=retry_failed)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda index: run_case(directory, index, retry_failed=retry_failed), indices))
    return status(directory)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "status", "pending"))
    parser.add_argument("directory")
    parser.add_argument("--index", type=int)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "pending":
        value = pending(args.directory, retry_failed=args.retry_failed)
    elif args.command == "status":
        value = status(args.directory)
    elif args.index is not None:
        value = run_case(args.directory, args.index, retry_failed=args.retry_failed)
    else:
        value = run_local(args.directory, workers=args.workers, retry_failed=args.retry_failed)
    print(json.dumps(value))
    return 1 if isinstance(value, dict) and (value.get("status") == "failed" or value.get("failed", 0)) else 0


if __name__ == "__main__":
    raise SystemExit(main())
