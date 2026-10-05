"""Qualify campaign building outcomes from immutable simulation diagnostics.

Every warning excludes every building outcome for that run, even when Core
accepts a warning for annual heating alone. Weather comparisons and event
receipts remain usable independently of engine qualification. This helper only
reads evidence; it never restores archives, edits receipts, or invokes engines.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

import pandas as pd

POLICY = "whole-run-warning-exclusion-1"
REPOSITORY = Path(__file__).resolve().parents[1]
BASE_KEYS = ["site_id", "year", "building_id"]
QUALIFICATION_COLUMNS = [
    "engine_run_qualified",
    "engine_outcome_qualified",
    "engine_core_warning_count",
    "engine_raw_warning_count",
    "engine_exclusion_reasons",
    "engine_canonical_simulation_path",
    "engine_baseline_simulation_path",
]


def fingerprint(data):
    """Fingerprint exact evidence bytes without trusting serialized acceptance flags."""
    return hashlib.sha256(data).hexdigest()


def _recorded_path(value):
    """Resolve recorded repository-relative paths independently of the caller's directory."""
    path = Path(value)
    return (path if path.is_absolute() else REPOSITORY / path).resolve()


def _read(path, inputs):
    """Capture existing evidence and bind every consumed file to its hash."""
    path = Path(path)
    if not path.is_file():
        raise ValueError(
            f"Required simulation qualification evidence is absent: {path}"
        )
    data = path.read_bytes()
    inputs[str(path.resolve())] = fingerprint(data)
    return data


def _json(path, inputs):
    """Parse a captured evidence document with an explicit provenance fingerprint."""
    return json.loads(_read(path, inputs))


def _weather(path, inputs, cache):
    """Bind full weather bytes and operative headers while excluding EPW comments."""
    path = Path(path)
    if path in cache:
        return cache[path]
    if path.is_file():
        data = _read(path, inputs)
    else:
        archive = Path(str(path) + ".gz")
        data = gzip.decompress(_read(archive, inputs))
    lines = data.decode("utf-8-sig").splitlines()
    if len(lines) < 9:
        raise ValueError(f"Canonical simulation weather is not an EPW: {path}")
    operative = [lines[i] for i in (0, 1, 2, 3, 4, 7)] + lines[8:]
    result = (fingerprint(data), fingerprint("\n".join(operative).encode()))
    cache[path] = result
    return result


def _imports(root, inputs):
    """Authenticate copied runs without following historical artifact paths."""
    path = root / "execution-environment/imported-simulations.json"
    if not path.exists():
        return {}
    journal = _json(path, inputs)
    records = journal["records"]
    if len(records) != journal["accepted_runs_imported"]:
        raise ValueError("Imported simulation journal count differs from records")
    origin = _recorded_path(journal["origin_campaign"])
    result = {}
    for record in records:
        copied = _recorded_path(record["copied_folder"])
        original = _recorded_path(record["original_folder"])
        if (
            copied in result
            or not copied.is_relative_to(root)
            or not original.is_relative_to(origin)
            or copied.relative_to(root) != original.relative_to(origin)
        ):
            raise ValueError("Imported simulation journal contains ambiguous paths")
        result[copied] = record | {"original_folder": str(original)}
    return result


def _count(value, name):
    """Require explicit nonnegative integer counts from diagnostic evidence documents."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Invalid simulation diagnostic count: {name}")
    return value


def _model(path, inputs, cache):
    """Fingerprint identical hardlinked models once while binding every consumed pathname."""
    if not path.is_file():
        raise ValueError(
            f"Required simulation qualification evidence is absent: {path}"
        )
    stat = path.stat()
    identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    if identity not in cache:
        cache[identity] = fingerprint(_read(path, inputs))
    inputs[str(path.resolve())] = cache[identity]
    return cache[identity]


def _assess(folder, imported, inputs, weather_cache, model_cache):
    """Validate one canonical simulation and conservatively qualify its whole output."""
    receipt_path = folder / "receipt.json"
    receipt_bytes = _read(receipt_path, inputs)
    receipt = json.loads(receipt_bytes)
    diagnostics = _json(folder / "diagnostics.json", inputs)
    result = _json(folder / "result.json", inputs)
    building = _json(folder / "building.json", inputs)
    model_sha = _model(folder / "model.epJSON", inputs, model_cache)
    err_path = folder / "eplusout.err"
    err_bytes = _read(err_path, inputs)
    err = err_bytes.decode("utf-8-sig")
    historical = folder
    if imported is not None:
        for key, expected in (
            ("receipt_sha256", fingerprint(receipt_bytes)),
            ("weather_sha256", receipt["weather_sha256"]),
            ("model_sha256", receipt["model_sha256"]),
            ("core_run_id", receipt["core_run_id"]),
        ):
            if imported[key] != expected:
                raise ValueError(f"Imported simulation evidence differs: {key}")
        historical = Path(imported["original_folder"])
    for value, expected in (
        (receipt["core_result_path"], historical / "result.json"),
        (receipt["core_building_spec_path"], historical / "building.json"),
        (diagnostics["err_path"], historical / "eplusout.err"),
    ):
        if _recorded_path(value) != expected:
            raise ValueError("Simulation qualification artifact path is not canonical")
    historical_weather = historical.parents[2] / "cases" / folder.name / "weather.epw"
    canonical_weather = folder.parents[2] / "cases" / folder.name / "weather.epw"
    climate = building["climate"]
    if (
        _recorded_path(receipt["weather_path"]) != historical_weather
        or _recorded_path(climate["epw_path"]) != historical_weather
        or climate["weather_sha256"] != receipt["weather_sha256"]
        or climate["calendar_year"] != receipt["year"]
    ):
        raise ValueError("Simulation qualification weather path is not canonical")
    weather_sha, operative_sha = _weather(canonical_weather, inputs, weather_cache)
    if weather_sha != receipt["weather_sha256"]:
        raise ValueError("Canonical simulation weather differs from its receipt")
    if model_sha != receipt["model_sha256"]:
        raise ValueError("Canonical simulation model differs from its receipt")
    if result["run_id"] != receipt["core_run_id"]:
        raise ValueError("Core result run identity differs from its study receipt")
    if result["diagnostics"] != diagnostics:
        raise ValueError("Core result diagnostics differ from the diagnostics artifact")
    if (
        not isinstance(receipt["core_diagnostics_accepted"], bool)
        or not isinstance(diagnostics["accepted"], bool)
        or receipt["core_diagnostics_accepted"] is not diagnostics["accepted"]
    ):
        raise ValueError("Study receipt and Core diagnostics acceptance disagree")
    counts = {
        "core_warning_count": _count(diagnostics["warning_count"], "Core warnings"),
        "core_severe_count": _count(diagnostics["severe_count"], "Core severe errors"),
        "core_fatal_count": _count(diagnostics["fatal_count"], "Core fatal errors"),
        "raw_warning_count": len(re.findall(r"\*\*\s+Warning\s+\*\*", err)),
        "raw_severe_count": len(re.findall(r"\*\*\s+Severe\s+\*\*", err)),
        "raw_fatal_count": len(re.findall(r"\*\*\s+Fatal\s+\*\*", err)),
    }
    for severity, receipt_key in (
        ("warning", "warning_count"),
        ("severe", "severe_errors"),
        ("fatal", "fatal_errors"),
    ):
        raw = counts[f"raw_{severity}_count"]
        core = counts[f"core_{severity}_count"]
        if _count(receipt[receipt_key], receipt_key) != raw or bool(core) != bool(raw):
            raise ValueError("Study receipt, Core and raw diagnostic severity disagree")
    messages = diagnostics["messages"]
    if any(
        m.get("severity") in ("warning", "severe", "fatal") for m in messages
    ) != any(counts.values()):
        raise ValueError("Core diagnostic messages and severity counts disagree")
    reasons = []
    if any(counts.values()):
        reasons.append("engine_diagnostic_warning_or_error")
    if not diagnostics["accepted"]:
        reasons.append("core_diagnostics_not_accepted")
    if diagnostics.get("warning_review_pending") or diagnostics.get(
        "context_cap_review_pending"
    ):
        reasons.append("core_diagnostic_review_pending")
    if diagnostics.get("disposition") not in (None, "accepted"):
        reasons.append("core_disposition_not_unconditionally_accepted")
    if "EnergyPlus Completed Successfully" not in err:
        reasons.append("engine_success_not_recorded")
    return {
        "canonical_simulation_path": str(folder),
        "historical_simulation_path": str(historical),
        "canonical_weather_path": str(canonical_weather),
        "weather_sha256": weather_sha,
        "operative_weather_sha256": operative_sha,
        "receipt_sha256": fingerprint(receipt_bytes),
        "diagnostics_sha256": inputs[str((folder / "diagnostics.json").resolve())],
        "err_sha256": fingerprint(err_bytes),
        "model_sha256": model_sha,
        "core_run_id": receipt["core_run_id"],
        "calendar_year": receipt["year"],
        "core_disposition": diagnostics.get("disposition"),
        "warning_exception_scopes": sorted(
            {
                m["exception_rule"]["scope"]
                for m in messages
                if m.get("exception_rule", {}).get("scope")
            }
        ),
        "qualified": not reasons,
        "reasons": reasons,
        **counts,
    }


def qualify_results(root, rows, inputs=None):
    """Bind raw rows to diagnostics and exclude outcomes with unqualified baselines."""
    root = Path(root).resolve()
    enclosing_inputs = {} if inputs is None else inputs
    inputs = {}
    script_sha = fingerprint(_read(Path(__file__), inputs))
    selected = rows.copy()
    if rows.empty:
        enclosing_inputs.update(inputs)
        for name in QUALIFICATION_COLUMNS:
            selected[name] = pd.Series(dtype="object")
        return (
            selected,
            selected.copy(),
            {
                "policy": POLICY,
                "evidence_mode": "strict_immutable_files",
                "qualifier_script_sha256": script_sha,
                "raw_building_rows": 0,
                "qualified_building_rows": 0,
                "excluded_building_rows": 0,
                "unique_simulations": 0,
                "qualified_simulations": 0,
                "excluded_simulations": 0,
                "exclusion_reason_counts": {},
                "records": [],
                "inputs": dict(inputs),
            },
        )
    required = BASE_KEYS + [
        "case_id",
        "method",
        "simulation_path",
        "compiled_model_sha256",
    ]
    if set(required) - set(rows) or rows[required].isna().any().any():
        raise ValueError("Raw building rows lack simulation qualification identities")
    if rows.duplicated(BASE_KEYS + ["case_id"]).any():
        raise ValueError("Raw building comparison identities are repeated")
    imports = _imports(root, inputs)
    cache, weather_cache, model_cache, assessments = {}, {}, {}, []
    for row in rows.itertuples(index=False):
        if Path(str(row.case_id)).name != str(row.case_id):
            raise ValueError(
                "Requested case identity is not a canonical path component"
            )
        folder = _recorded_path(row.simulation_path)
        expected = (
            root
            / "datasets"
            / str(row.site_id)
            / str(row.year)
            / "simulations"
            / str(row.building_id)
        )
        if folder.parent != expected or not folder.is_relative_to(root):
            raise ValueError(
                "Simulation path is outside its campaign site-year building"
            )
        if folder not in cache:
            cache[folder] = _assess(
                folder, imports.get(folder), inputs, weather_cache, model_cache
            )
        assessment = cache[folder]
        if row.compiled_model_sha256 != assessment["model_sha256"]:
            raise ValueError("Summary model identity differs from canonical simulation")
        if row.year != assessment["calendar_year"]:
            raise ValueError("Summary year differs from canonical simulation weather")
        requested = expected.parents[1] / "cases" / str(row.case_id) / "weather.epw"
        _, operative = _weather(requested, inputs, weather_cache)
        if operative != assessment["operative_weather_sha256"]:
            raise ValueError(
                "Aliased case weather differs from the canonical simulation"
            )
        assessments.append(assessment)
    baseline = {}
    for position, (_, row) in enumerate(rows.iterrows()):
        if row.method == "reference":
            key = tuple(row[k] for k in BASE_KEYS)
            if key in baseline or row.case_id != "reference":
                raise ValueError("Reference simulation qualification is ambiguous")
            baseline[key] = assessments[position]
    reasons, references = [], []
    for position, (_, row) in enumerate(rows.iterrows()):
        key = tuple(row[k] for k in BASE_KEYS)
        if key not in baseline:
            raise ValueError("Building outcome lacks reference simulation diagnostics")
        reference = baseline[key]
        own = assessments[position]
        if own["model_sha256"] != reference["model_sha256"]:
            raise ValueError(
                "Candidate physical model differs from its reference model"
            )
        reason = own["reasons"].copy()
        if row.method != "reference" and not reference["qualified"]:
            reason.append("reference_engine_unqualified")
        reasons.append(reason)
        references.append(reference["canonical_simulation_path"])
    selected["engine_run_qualified"] = [a["qualified"] for a in assessments]
    selected["engine_outcome_qualified"] = [not r for r in reasons]
    selected["engine_core_warning_count"] = [
        a["core_warning_count"] for a in assessments
    ]
    selected["engine_raw_warning_count"] = [a["raw_warning_count"] for a in assessments]
    selected["engine_exclusion_reasons"] = [json.dumps(r) for r in reasons]
    selected["engine_canonical_simulation_path"] = [
        a["canonical_simulation_path"] for a in assessments
    ]
    selected["engine_baseline_simulation_path"] = references
    exclusions = selected.loc[~selected.engine_outcome_qualified].copy()
    qualified = int(selected.engine_outcome_qualified.sum())
    records = list(cache.values())
    metadata = {
        "policy": POLICY,
        "evidence_mode": "strict_immutable_files",
        "qualifier_script_sha256": script_sha,
        "raw_building_rows": len(rows),
        "qualified_building_rows": qualified,
        "excluded_building_rows": len(rows) - qualified,
        "unique_simulations": len(records),
        "qualified_simulations": sum(r["qualified"] for r in records),
        "excluded_simulations": sum(not r["qualified"] for r in records),
        "exclusion_reason_counts": dict(
            Counter(reason for group in reasons for reason in group)
        ),
        "records": records,
        "inputs": dict(inputs),
        "interpretation": "Every warning excludes all building outcomes, including annual heating accepted under scoped Core exceptions. Unqualified reference runs exclude every related profile/site-year comparison. Aliases inherit canonical run qualification. Weather availability and anomaly evidence remain independent.",
    }
    enclosing_inputs.update(inputs)
    return selected, exclusions, metadata
