"""Prepared evaluations with local execution, immutable inputs and attempt records.

The same on-disk plan is read by local workers and Slurm array workers. Scientific
evaluation is a named public callable receiving one JSON-compatible payload.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import logging
import platform
import shutil
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

from ._events import event
from ._inspection import inspection
from .records import atomic_json, digest, exclusive_lock, json_value, read_json

logger = logging.getLogger(__name__)


def _runtime_identity(cases, handler):
    """Fingerprint the worker code and selected engine used by prepared cases."""
    packages = {}
    for name in (
        "epsm_workflows",
        "epsm",
        "epsm_core",
        "epsm_data",
        "epsm_db",
        "epsm_lca",
        "epsm_rc",
        "epsm_studies",
        "smhi2epw",
    ):
        spec = importlib.util.find_spec(name)
        if spec is None or spec.origin is None:
            continue
        root = Path(spec.origin).parent
        fingerprint = hashlib.sha256()
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix in {".py", ".json"}:
                fingerprint.update(str(path.relative_to(root)).encode())
                fingerprint.update(hashlib.sha256(path.read_bytes()).digest())
        try:
            version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            version = None
        packages[name] = {"version": version, "source_sha256": fingerprint.hexdigest()}
    module = handler.split(":", 1)[0]
    spec = importlib.util.find_spec(module)
    if spec is None:
        raise ImportError(f"Install the prepared handler module: {module}")
    handler_hash = (
        hashlib.sha256(Path(spec.origin).read_bytes()).hexdigest()
        if spec.origin and Path(spec.origin).is_file()
        else None
    )
    dependencies = {}
    for name in ("numpy", "pandas", "scipy", "pydantic", "shapely", "pvlib", "hplib"):
        try:
            dependencies[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            dependencies[name] = None
    engines, case_engines, inspected = {}, {}, {}
    if handler == "epsm_workflows.integration:evaluate":
        from .integration import _simulation_options

        for case in cases:
            simulation = _simulation_options(case["payload"].get("simulation", {}))
            engine = simulation.get("engine", "energyplus")
            if engine == "energyplus":
                from epsm_core.compiler import EnergyPlusCompiler

                selection = simulation.get("energyplus")
                if selection not in inspected:
                    compiler = EnergyPlusCompiler(
                        selection, settings=simulation.get("settings")
                    )
                    inspected[selection] = {
                        "engine": engine,
                        "executable_sha256": compiler.engine_hash,
                        "schema_sha256": compiler.schema_hash,
                        "version": compiler.version,
                    }
                fingerprint = inspected[selection]
            elif engine == "epsm_rc":
                if "epsm_rc" not in packages:
                    raise ImportError("Install epsm_rc before preparing RC evaluations")
                fingerprint = {"engine": engine, "implementation": packages["epsm_rc"]}
            else:
                raise ValueError(f"Unsupported prepared engine: {engine}")
            identity = digest(fingerprint)
            engines[identity] = fingerprint
            case_engines[case["id"]] = identity
    return {
        "python": platform.python_version(),
        "packages": packages,
        "dependencies": dependencies,
        "handler_sha256": handler_hash,
        "engines": dict(sorted(engines.items())),
        "case_engines": dict(sorted(case_engines.items())),
    }


def _verify_runtime(manifest, cases):
    expected = manifest.get("runtime")
    if expected is None:
        raise ValueError(
            "Prepared plan has no worker runtime identity; prepare a new plan"
        )
    actual = _runtime_identity(cases, manifest["handler"])
    if actual != expected:
        raise ValueError("Worker runtime differs from the prepared plan")
    return digest(actual)


@dataclass(frozen=True)
class PreparedEvaluation:
    payload: dict
    inputs: dict = field(default_factory=dict)
    name: str = ""
    files: dict = field(default_factory=dict)

    def __repr__(self):
        return inspection(
            type(self).__name__,
            name=self.name,
            inputs=len(self.inputs),
            files=len(self.files),
        )

    __str__ = __repr__

    def summary(self):
        """Describe declared inputs without reading or hashing asset files."""
        return {
            "name": self.name,
            "payload_fields": tuple(self.payload),
            "input_names": tuple(self.inputs),
            "file_names": tuple(self.files),
        }

    @property
    def id(self):
        return self.to_dict()["id"]

    def to_dict(self):
        import hashlib

        assets = {}
        for name, filename in self.files.items():
            path = Path(filename)
            checksum = hashlib.sha256(path.read_bytes()).hexdigest()
            assets[name] = {
                "sha256": checksum,
                "path": f"assets/{checksum}/{path.name}",
            }
        value = {
            "payload": json_value(self.payload),
            "inputs": json_value(self.inputs),
            "name": self.name,
            "assets": assets,
        }
        return {"id": digest(value), **value}


def prepare(
    directory, cases, *, handler="epsm_workflows.integration:evaluate", environment=None
):
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
        raise ValueError(
            "Prepared cases need nonempty, unique identities; name repeated design rows"
        )
    manifest = {
        "schema": 3,
        "handler": handler,
        "environment": json_value(environment or {}),
        "case_ids": [case["id"] for case in cases],
        "runtime": _runtime_identity(cases, handler),
    }
    manifest["id"] = digest(manifest)
    with exclusive_lock(root / ".prepare.lock"):
        path = root / "manifest.json"
        if path.exists():
            if _manifest(root) != manifest:
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
    event(
        logger,
        "study.preparation.completed",
        "Prepared immutable study cases",
        context={"study_id": manifest["id"]},
        details={"cases": len(cases), "directory": str(root)},
    )
    return manifest


def _manifest(root):
    value = read_json(Path(root) / "manifest.json")
    if value.get("schema") != 3:
        raise ValueError("Unsupported prepared plan schema; prepare a new plan")
    if value["id"] != digest({key: data for key, data in value.items() if key != "id"}):
        raise ValueError("Prepared manifest has changed")
    runtime = value.get("runtime", {})
    engines, bindings = runtime.get("engines"), runtime.get("case_engines")
    if not isinstance(engines, dict) or not isinstance(bindings, dict):
        raise ValueError("Prepared engine identities are missing; prepare a new plan")
    expected = (
        set(value["case_ids"])
        if value["handler"] == "epsm_workflows.integration:evaluate"
        else set()
    )
    if (
        set(bindings) != expected
        or set(bindings.values()) != set(engines)
        or any(identity != digest(record) for identity, record in engines.items())
    ):
        raise ValueError("Prepared engine bindings are invalid; prepare a new plan")
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
    return manifest, _read_case(root, index, manifest)


def _read_case(root, index, manifest):
    """Check one case against the captured plan without rereading its manifest."""
    if not 0 <= index < len(manifest["case_ids"]):
        raise IndexError(index)
    case = read_json(root / "cases" / f"{index:06d}.json")
    identity = digest(
        {key: case[key] for key in ("payload", "inputs", "name", "assets")}
    )
    if case["id"] != identity or identity != manifest["case_ids"][index]:
        raise ValueError(f"Prepared case {index} has changed")
    return case


def _payload(root, case, attempt_id):
    assets = {
        name: str(_verify_asset(root, asset)) for name, asset in case["assets"].items()
    }

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
    value["_execution"] = {
        "directory": str(Path(root).resolve()),
        "case_id": case["id"],
        "attempt_id": attempt_id,
    }
    return value


def _handler(reference):
    module, name = reference.split(":", 1)
    value = getattr(import_module(module), name)
    if not callable(value):
        raise TypeError(f"Evaluation handler is not callable: {reference}")
    return value


def _write_record(path, value):
    atomic_json(path, {**value, "record_sha256": digest(value)})


def _read_attempt(path, manifest, index, *, completed=False, runtime_sha256=None):
    """Authenticate a retained attempt against its prepared inputs."""
    value = read_json(path)
    checksum = value.pop("record_sha256", None)
    if checksum != digest(value):
        raise ValueError("Stored result content has changed or is incomplete")
    if (
        value.get("plan_id") != manifest["id"]
        or value.get("case_id") != manifest["case_ids"][index]
        or value.get("index") != index
        or value.get("runtime_sha256")
        != (runtime_sha256 or digest(manifest["runtime"]))
    ):
        raise ValueError("Stored result belongs to different prepared inputs")
    allowed = (
        {"succeeded", "failed"} if completed else {"running", "succeeded", "failed"}
    )
    if value.get("status") not in allowed:
        raise ValueError("Stored result is not a completed attempt")
    if value["status"] == "succeeded" and "result" not in value:
        raise ValueError("Successful attempt has no result")
    return value


def _read_result(path, manifest, index, *, runtime_sha256=None):
    return _read_attempt(
        path, manifest, index, completed=True, runtime_sha256=runtime_sha256
    )


def validate_plan(directory):
    """Verify prepared identities and file contents before moving/executing a plan."""
    return _plan_inputs(Path(directory))[0]


def _plan_inputs(root):
    manifest = _manifest(root)
    cases, checked_assets = [], set()
    for index in range(len(manifest["case_ids"])):
        case = _read_case(root, index, manifest)
        cases.append(case)
        for asset in case["assets"].values():
            identity = (asset["path"], asset["sha256"])
            if identity not in checked_assets:
                _verify_asset(root, asset)
                checked_assets.add(identity)
    return manifest, cases


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(data) for key, data in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(data) for data in value)
    return value


def _manifest_stamp(root):
    stat = (root / "manifest.json").stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


@dataclass(frozen=True)
class _VerifiedPlan:
    manifest: object
    runtime_sha256: str
    stamp: tuple


def _verified_plan(root):
    stamp = _manifest_stamp(root)
    manifest, cases = _plan_inputs(root)
    runtime = _verify_runtime(manifest, cases)
    if _manifest_stamp(root) != stamp:
        raise ValueError("Prepared manifest changed during verification")
    return _VerifiedPlan(_freeze(manifest), runtime, stamp)


def _unchanged_manifest(root, verified):
    # This detects file changes within a batch; fresh calls always verify the
    # complete manifest and runtime. The captured plan itself is deeply frozen.
    if _manifest_stamp(root) != verified.stamp:
        raise ValueError("Prepared manifest changed during the local batch")


def read_results(directory, *, require_success=True):
    """Read verified results in prepared row order; incomplete matrices are rejected."""
    root = Path(directory)
    manifest = _manifest(root)
    runtime_sha256 = digest(manifest["runtime"])
    rows = []
    for index in range(len(manifest["case_ids"])):
        path = root / "results" / f"{index:06d}.json"
        row = (
            _read_result(path, manifest, index, runtime_sha256=runtime_sha256)
            if path.exists()
            else None
        )
        if require_success and (row is None or row["status"] != "succeeded"):
            raise ValueError(f"Prepared case {index} has no successful result")
        rows.append(row)
    return tuple(rows)


def inspect_plan(directory):
    """Inspect verified cases/assets without requiring the execution environment.

    This public collection boundary is usable after transport or package updates;
    running/resuming still verifies the exact observed worker runtime separately.
    """
    root = Path(directory).resolve()
    manifest, cases = _plan_inputs(root)
    return {"manifest": manifest, "cases": tuple(cases)}


def read_attempts(directory):
    """Return all verified attempts, including interrupted running attempts.

    Order is case index then start time and attempt identity. Collection does
    not import engines or require the producer's packages to be installed.
    """
    root = Path(directory).resolve()
    manifest = _manifest(root)
    rows = []
    for index in range(len(manifest["case_ids"])):
        for path in sorted((root / "attempts" / f"{index:06d}").glob("*.json")):
            if not path.resolve().is_relative_to(root):
                raise ValueError("Attempt artifact must stay inside the prepared directory")
            rows.append(_read_attempt(path, manifest, index))
    return tuple(sorted(rows, key=lambda row: (row["index"], row.get("started_at", ""), row["id"])))


def cancel(directory):
    """Request graceful cancellation: active cases finish; no new cases start."""
    root = Path(directory)
    manifest = _manifest(root)
    atomic_json(root / "control.json", {"plan_id": manifest["id"], "cancel_requested": True})
    event(logger, "study.cancel.requested", "Requested graceful study cancellation",
          context={"study_id": manifest["id"]})
    return status(directory)


def resume(directory):
    """Explicitly clear cancellation while preserving all cases and attempts."""
    root = Path(directory)
    manifest = _manifest(root)
    (root / "control.json").unlink(missing_ok=True)
    event(logger, "study.resume.requested", "Enabled study dispatch",
          context={"study_id": manifest["id"]})
    return status(directory)


def _cancel_requested(root, manifest):
    path = root / "control.json"
    if not path.exists():
        return False
    control = read_json(path)
    if control.get("plan_id") != manifest["id"]:
        raise ValueError("Cancellation belongs to a different prepared plan")
    return bool(control.get("cancel_requested"))


def run_case(directory, index, *, retry_failed=False):
    """Run one immutable case at most once successfully; retain every attempt."""
    root = Path(directory)
    return _run_case(root, index, _verified_plan(root), retry_failed=retry_failed)


def _run_case(root, index, verified, *, retry_failed):
    manifest, runtime_sha256 = verified.manifest, verified.runtime_sha256
    result_path = root / "results" / f"{index:06d}.json"
    with exclusive_lock(root / "locks" / f"{index:06d}.lock"):
        _unchanged_manifest(root, verified)
        if _cancel_requested(root, manifest):
            return None
        case = _read_case(root, index, manifest)
        for asset in case["assets"].values():
            _verify_asset(root, asset)
        if result_path.exists():
            previous = _read_result(
                path=result_path,
                manifest=manifest,
                index=index,
                runtime_sha256=runtime_sha256,
            )
            if previous["status"] == "succeeded" or not retry_failed:
                event(
                    logger,
                    "study.case.reused",
                    "Retained a completed case attempt",
                    context={
                        "study_id": manifest["id"],
                        "case_id": case["id"],
                        "run_id": previous["id"],
                    },
                    details={"status": previous["status"]},
                    level=logging.DEBUG,
                )
                return previous
        attempt_id = uuid4().hex
        attempt = {
            "id": attempt_id,
            "plan_id": manifest["id"],
            "case_id": case["id"],
            "index": index,
            "status": "running",
            "runtime_sha256": runtime_sha256,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        attempt_path = root / "attempts" / f"{index:06d}" / f"{attempt_id}.json"
        _write_record(attempt_path, attempt)
        context = {
            "study_id": manifest["id"],
            "case_id": case["id"],
            "run_id": attempt_id,
        }
        event(
            logger,
            "study.case.started",
            "Started a prepared case",
            context=context,
            details={"index": index},
        )
        try:
            result = _handler(manifest["handler"])(_payload(root, case, attempt_id))
            attempt.update(status="succeeded", result=json_value(result))
        except Exception as error:
            attempt.update(
                status="failed",
                error={
                    "type": type(error).__name__,
                    "message": str(error),
                    "traceback": traceback.format_exc(),
                },
            )
            event(
                logger,
                "study.case.failed",
                "Prepared case failed",
                context=context,
                details={"index": index, "error_type": type(error).__name__},
                level=logging.ERROR,
                exc_info=True,
            )
        attempt["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_record(attempt_path, attempt)
        _write_record(result_path, attempt)
        if attempt["status"] == "succeeded":
            event(
                logger,
                "study.case.completed",
                "Completed a prepared case",
                context=context,
                details={"index": index, "record": str(result_path)},
            )
        return attempt


def pending(directory, *, retry_failed=False):
    """Return unfinished, currently unlocked case indices."""
    root = Path(directory)
    return _pending(root, _verified_plan(root), retry_failed=retry_failed)


def _pending(root, verified, *, retry_failed, indices=None):
    manifest = verified.manifest
    selected = _selected_indices(indices, len(manifest["case_ids"]))
    unfinished = []
    for index in selected:
        path = root / "results" / f"{index:06d}.json"
        if path.exists():
            stored = _read_result(
                path, manifest, index, runtime_sha256=verified.runtime_sha256
            )
            if stored["status"] == "succeeded" or not retry_failed:
                continue
        try:
            with exclusive_lock(root / "locks" / f"{index:06d}.lock"):
                unfinished.append(index)
        except RuntimeError:
            continue
    _unchanged_manifest(root, verified)
    return unfinished


def _selected_indices(indices, count):
    if indices is None:
        return range(count)
    try:
        selected = tuple(indices)
    except TypeError as error:
        raise ValueError("indices must contain unique integer case indices") from error
    if any(type(index) is not int or not 0 <= index < count for index in selected):
        raise ValueError("indices must contain integer case indices inside the prepared plan")
    if len(set(selected)) != len(selected):
        raise ValueError("indices must contain unique case indices")
    return selected


def status(directory):
    root = Path(directory)
    manifest = _manifest(root)
    runtime_sha256 = digest(manifest["runtime"])
    counts = {
        "plan_id": manifest["id"],
        "total": len(manifest["case_ids"]),
        "succeeded": 0,
        "failed": 0,
        "unfinished": 0,
    }
    for index in range(counts["total"]):
        path = root / "results" / f"{index:06d}.json"
        if path.exists():
            stored = _read_result(path, manifest, index, runtime_sha256=runtime_sha256)
            counts[stored["status"]] += 1
        else:
            counts["unfinished"] += 1
    if _cancel_requested(root, manifest):
        counts["cancel_requested"] = True
    return counts


def run_local(directory, *, workers=1, retry_failed=False, indices=None):
    """Resume pending cases, optionally restricting dispatch to selected indices.

    ``indices`` is an iterable of unique zero-based case indices. Its order is
    retained when using one worker. The complete prepared plan and runtime are
    verified once per invocation; each dispatched case and asset is rechecked.
    Existing successes and failures follow the ordinary explicit retry rules.
    The returned counts always describe the complete plan, including cases
    outside the selected batch. Omitting ``indices`` retains the existing route.

    For example, ``run_local("study", indices=range(0, 32))`` resumes only the
    first 32 prepared cases without changing their identities or saved inputs.
    """
    if not isinstance(workers, int) or workers < 1:
        raise ValueError("workers must be a positive integer")
    root = Path(directory)
    verified = _verified_plan(root)
    indices = _pending(root, verified, retry_failed=retry_failed, indices=indices)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(
            pool.map(
                lambda index: _run_case(
                    root, index, verified, retry_failed=retry_failed
                ),
                indices,
            )
        )
    outcome = status(directory)
    event(
        logger,
        "study.execution.completed",
        "Finished the local execution batch",
        context={"study_id": outcome["plan_id"]},
        details={key: value for key, value in outcome.items() if key != "plan_id"},
    )
    return outcome


def main(argv=None):
    import sys
    from .authoring_cli import COMMANDS

    selected = list(sys.argv[1:] if argv is None else argv)
    if selected and selected[0] in COMMANDS:
        from .authoring_cli import main as authoring_main

        return authoring_main(selected)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "status", "pending", "cancel", "resume", "verify"))
    parser.add_argument("directory")
    parser.add_argument("--index", type=int)
    parser.add_argument("--start", type=int, help="First case in a bounded run (inclusive)")
    parser.add_argument("--stop", type=int, help="Last case boundary in a bounded run (exclusive)")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args(argv)
    indices = None
    if args.start is not None or args.stop is not None:
        if args.command != "run" or args.index is not None:
            parser.error("--start/--stop require run and cannot be combined with --index")
        if args.start is None or args.stop is None:
            parser.error("A bounded run needs both --start and --stop")
        if not 0 <= args.start <= args.stop <= len(_manifest(Path(args.directory))["case_ids"]):
            parser.error("The --start/--stop range must stay inside the prepared plan")
        indices = range(args.start, args.stop)
    if args.command == "cancel":
        value = cancel(args.directory)
    elif args.command == "resume":
        value = resume(args.directory)
    elif args.command == "verify":
        checked = inspect_plan(args.directory)
        value = {"plan_id": checked["manifest"]["id"], "cases": len(checked["cases"]), "valid": True}
    elif args.command == "pending":
        value = pending(args.directory, retry_failed=args.retry_failed)
    elif args.command == "status":
        value = status(args.directory)
    elif args.index is not None:
        value = run_case(args.directory, args.index, retry_failed=args.retry_failed)
    else:
        value = run_local(
            args.directory, workers=args.workers, retry_failed=args.retry_failed,
            indices=indices,
        )
    print(json.dumps(value))
    return (
        1
        if isinstance(value, dict)
        and (value.get("status") == "failed" or value.get("failed", 0))
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
