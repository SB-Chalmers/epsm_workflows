"""Losslessly archive weather files after a campaign site/year fully finishes.

Run ``python -m examples.archive_weather_campaign_files CAMPAIGN_ROOT`` while
other site/years run. Only an audited complete site/year with a verified summary
checkpoint and every building/case comparison is eligible. Do not concurrently
resume an already completed site/year while archiving it. No frozen scientific
driver, weather/Core receipt, baseline EPW, model, or summary is rewritten.

Candidate ``weather.epw.gz`` files are standard deterministic gzip, level6.
Restore exact EPW bytes with ``gzip -dk weather.epw.gz``; retain the archive and
its separate ``weather-archive.json`` receipt. A frozen campaign resume may also
rerender EPWs from pinned sources. Existing archives are verified against that
rerender before any plain file is removed again. This changes storage only.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import platform
import uuid
import zlib
from datetime import datetime, timezone
from pathlib import Path

BLOCK_BYTES = 1024 * 1024


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(BLOCK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _valid_sha(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and set(value) <= set("0123456789abcdef")
    )


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Unreadable storage prerequisite: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Expected object receipt: {path}")
    return value


def _dump(path: Path, value: dict) -> None:
    """Replace only a utility-owned receipt, with no fixed temporary-file race."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _local(path: Path, root: Path) -> Path:
    """Reject links and traversal before opening or mutating campaign files."""
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Storage path escapes campaign root: {path}") from exc
    current = root
    for part in relative.parts:
        if part in {".", ".."}:
            raise ValueError(f"Unsafe storage path: {path}")
        current /= part
        if current.is_symlink():
            raise ValueError(f"Storage path contains a symlink: {path}")
    return path


def _archive_identity(saved: dict, case: dict) -> None:
    expected = {
        "schema_version": 1,
        "format": "gzip",
        "compression_level": 6,
        "archive_file": "weather.epw.gz",
        "uncompressed_file": "weather.epw",
        "logical_weather_path": case["logical_path"],
        "case_id": case["case_id"],
        "epw_sha256": case["epw_sha256"],
        "weather_receipt_sha256": case["receipt_sha256"],
    }
    if any(saved.get(key) != value for key, value in expected.items()):
        raise ValueError(
            "Weather archive receipt differs from its original weather receipt"
        )
    if not _valid_sha(saved.get("gzip_sha256")):
        raise ValueError("Weather archive receipt has an invalid gzip hash")
    if any(
        type(saved.get(key)) is not int or saved[key] < 0
        for key in ("compressed_bytes", "uncompressed_bytes")
    ):
        raise ValueError("Weather archive receipt has invalid byte counts")


def _verify_plain(case: dict, saved: dict | None = None) -> None:
    plain = case["folder"] / "weather.epw"
    if plain.exists() and (not plain.is_file() or _sha(plain) != case["epw_sha256"]):
        raise ValueError(f"Plain EPW differs from its weather receipt: {plain}")
    if (
        saved is not None
        and plain.exists()
        and (plain.stat().st_size != saved["uncompressed_bytes"])
    ):
        raise ValueError(
            "Plain EPW byte count differs from its weather archive receipt"
        )


def _verify_archive(archive: Path, saved: dict, case: dict) -> None:
    _archive_identity(saved, case)
    if (
        not archive.is_file()
        or _sha(archive) != saved["gzip_sha256"]
        or archive.stat().st_size != saved["compressed_bytes"]
    ):
        raise ValueError("Weather gzip archive differs from its receipt")
    digest = hashlib.sha256()
    count = 0
    try:
        with gzip.open(archive, "rb") as stream:
            for block in iter(lambda: stream.read(BLOCK_BYTES), b""):
                digest.update(block)
                count += len(block)
    except (OSError, EOFError, zlib.error) as exc:
        raise ValueError("Weather gzip archive cannot be decoded losslessly") from exc
    if digest.hexdigest() != case["epw_sha256"] or count != saved["uncompressed_bytes"]:
        raise ValueError("Decoded EPW differs from its original weather receipt")
    _verify_plain(case, saved)


def _archive_state(case: dict) -> tuple[dict | None, bool]:
    """Inspect pending publication read-only, including before any year mutation."""
    folder = case["folder"]
    archive = folder / "weather.epw.gz"
    sidecar = folder / "weather-archive.json"
    pending = folder / "weather-archive.json.pending.json"
    saved = _json(sidecar) if sidecar.exists() else None
    prepared = _json(pending) if pending.exists() else None
    for receipt in (saved, prepared):
        if receipt is not None:
            _archive_identity(receipt, case)
    if prepared is not None:
        if saved is not None and saved != prepared:
            raise ValueError("Conflicting final and pending weather archive receipts")
        if archive.exists():
            _verify_archive(archive, prepared, case)
            return prepared, True
        if saved is not None:
            raise ValueError("Weather archive and final receipt must both exist")
        # A pre-publication interruption retained the original; re-compress it.
        if not (folder / "weather.epw").is_file():
            raise ValueError("Pending weather publication has no recoverable EPW")
        _verify_plain(case, prepared)
        return None, True
    if archive.exists() or saved is not None:
        if not archive.is_file() or saved is None:
            raise ValueError("Weather archive and its receipt must both exist")
        _verify_archive(archive, saved, case)
        return saved, False
    if not (folder / "weather.epw").is_file():
        raise ValueError(
            "Available weather case has neither plain EPW nor verified archive"
        )
    _verify_plain(case)
    return None, False


def _preflight_year(folder: Path, root: Path, buildings: set[str]) -> dict:
    """Verify the complete six-profile catalogue without recovery or writes."""
    paths = [
        folder / "audit.json",
        folder / "summary.csv",
        folder / "summary.csv.receipt.json",
        folder / "case-catalogue.json",
    ]
    for path in paths:
        _local(path, root)
    audit, checkpoint, catalogue = (_json(paths[i]) for i in (0, 2, 3))
    if audit.get("full_matrix_site_year_complete") is not True:
        raise ValueError("Site/year stopped being fully complete during archival")
    if Path(str(paths[2]) + ".pending.json").exists():
        raise ValueError("Completed site/year has an unfinished summary checkpoint")
    if (
        not _valid_sha(checkpoint.get("sha256"))
        or type(checkpoint.get("rows")) is not int
        or checkpoint["rows"] < 1
        or _sha(paths[1]) != checkpoint["sha256"]
    ):
        raise ValueError("Completed summary checkpoint hash or receipt changed")
    pairs = []
    with paths[1].open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames or []
        if not {"building_id", "case_id"}.issubset(fields) or len(set(fields)) != len(
            fields
        ):
            raise ValueError(
                "Completed summary lacks unambiguous building/case columns"
            )
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("Malformed completed summary CSV row")
            pairs.append((row["building_id"], row["case_id"]))
    if len(pairs) != checkpoint["rows"] or _sha(paths[1]) != checkpoint["sha256"]:
        raise ValueError("Completed summary checkpoint row count or bytes changed")
    records = catalogue.get("cases")
    if not isinstance(records, list) or not records:
        raise ValueError("Completed year needs an available weather case catalogue")
    cases = []
    ids = set()
    for record in records:
        case_id = record.get("case_id") if isinstance(record, dict) else None
        if (
            not isinstance(case_id, str)
            or not case_id
            or case_id in {".", ".."}
            or "/" in case_id
            or "\\" in case_id
            or case_id in ids
            or record.get("status") in {"unsupported", "unavailable"}
        ):
            raise ValueError(
                "Unsafe, duplicate, or unavailable case in available catalogue"
            )
        ids.add(case_id)
        case_folder = _local(folder / "cases" / case_id, root)
        for name in (
            "weather.epw",
            "weather.epw.gz",
            "weather-receipt.json",
            "weather-archive.json",
            "weather-archive.json.pending.json",
        ):
            _local(case_folder / name, root)
        logical = record.get("path")
        if (
            not isinstance(logical, str)
            or Path(logical).resolve() != case_folder / "weather.epw"
        ):
            raise ValueError(
                "Catalogue weather path differs from its local case directory"
            )
        receipt_path = case_folder / "weather-receipt.json"
        receipt = _json(receipt_path)
        if not _valid_sha(receipt.get("epw_sha256")):
            raise ValueError("Weather receipt lacks its original EPW SHA-256")
        case = {
            "case_id": case_id,
            "folder": case_folder,
            "logical_path": logical,
            "epw_sha256": receipt["epw_sha256"],
            "receipt_sha256": _sha(receipt_path),
        }
        if case_id == "reference":
            if (
                record.get("method") != "reference"
                or not (case_folder / "weather.epw").is_file()
            ):
                raise ValueError("Reference baseline EPW must remain plain")
            _verify_plain(case)
        else:
            if record.get("method") == "reference":
                raise ValueError("A candidate case cannot masquerade as the reference")
            saved, pending = _archive_state(case)
            case.update(saved=saved, pending=pending)
        cases.append(case)
    if "reference" not in ids:
        raise ValueError("Completed catalogue lacks its reference baseline")
    expected = {(building, case_id) for building in buildings for case_id in ids}
    if len(set(pairs)) != len(pairs) or set(pairs) != expected:
        raise ValueError("Completed summary lacks all six building/weather comparisons")
    if audit.get("comparisons") != len(pairs):
        raise ValueError("Completed audit and summary comparison counts differ")
    return {
        "root": root,
        "folder": folder,
        "cases": cases,
        "guards": {p: _sha(p) for p in paths},
    }


def _unchanged(plan: dict, case: dict) -> None:
    for path, expected in plan["guards"].items():
        _local(path, plan["root"])
        if _sha(path) != expected:
            raise ValueError("Completed site/year changed while archiving weather")
    for name in (
        "weather.epw",
        "weather.epw.gz",
        "weather-receipt.json",
        "weather-archive.json",
        "weather-archive.json.pending.json",
    ):
        _local(case["folder"] / name, plan["root"])
    if Path(
        str(plan["folder"] / "summary.csv.receipt.json") + ".pending.json"
    ).exists():
        raise ValueError("Completed site/year resumed a summary checkpoint")
    if _sha(case["folder"] / "weather-receipt.json") != case["receipt_sha256"]:
        raise ValueError("Original weather receipt changed while archiving")


def _archive_case(case: dict, plan: dict) -> tuple[dict, bool, int]:
    """Publish verified gzip and its receipt before deleting an exact plain EPW."""
    folder = case["folder"]
    archive, sidecar = folder / "weather.epw.gz", folder / "weather-archive.json"
    pending = folder / "weather-archive.json.pending.json"
    plain = folder / "weather.epw"
    _unchanged(plan, case)
    saved, has_pending = _archive_state(case)
    newly_created = saved is None
    if has_pending:
        if saved is not None:
            _dump(sidecar, saved)
        pending.unlink()
    if saved is None:
        temporary = folder / f".weather.epw.gz.{uuid.uuid4().hex}.tmp"
        try:
            with plain.open("rb") as source, temporary.open("wb") as destination:
                with gzip.GzipFile(
                    filename="",
                    fileobj=destination,
                    mode="wb",
                    compresslevel=6,
                    mtime=0,
                ) as compressed:
                    for block in iter(lambda: source.read(BLOCK_BYTES), b""):
                        compressed.write(block)
            saved = {
                "schema_version": 1,
                "format": "gzip",
                "compression_level": 6,
                "archive_file": archive.name,
                "uncompressed_file": plain.name,
                "logical_weather_path": case["logical_path"],
                "case_id": case["case_id"],
                "epw_sha256": case["epw_sha256"],
                "weather_receipt_sha256": case["receipt_sha256"],
                "gzip_sha256": _sha(temporary),
                "compressed_bytes": temporary.stat().st_size,
                "uncompressed_bytes": plain.stat().st_size,
                "interpretation": "Lossless storage only; original weather/Core receipts and scientific results unchanged. Restore exact weather.epw using gzip -dk weather.epw.gz.",
            }
            _verify_archive(temporary, saved, case)
            _unchanged(plan, case)
            _dump(pending, saved)
            temporary.replace(archive)
            _dump(sidecar, saved)
            pending.unlink()
        finally:
            temporary.unlink(missing_ok=True)
    _verify_archive(archive, _json(sidecar), case)
    _unchanged(plan, case)
    removed = plain.stat().st_size if plain.exists() else 0
    plain.unlink(missing_ok=True)
    return saved, newly_created, removed


def archive_completed_weather(campaign_root: str | Path) -> dict:
    """Archive candidates in verified finished site/years; skip active years.

    ``bytes_saved`` measures weather data bytes removed minus newly created gzip
    bytes for this invocation; sidecar/environment-receipt overhead is excluded.
    All eligible years are preflighted before any weather file is changed.
    """
    root = Path(campaign_root).resolve()
    matrix_path = _local(root / "matrix.json", root)
    buildings = _json(matrix_path).get("buildings")
    if (
        not isinstance(buildings, dict)
        or len(buildings) != 6
        or not all(isinstance(b, str) and b for b in buildings)
    ):
        raise ValueError(
            "Weather archival requires the campaign's six building profiles"
        )
    _local(root / "datasets", root)
    environment = _local(root / "execution-environment", root)
    plans, skipped = [], []
    for folder in sorted((root / "datasets").glob("*/*")):
        _local(folder, root)
        if not folder.is_dir():
            continue
        audit_path = _local(folder / "audit.json", root)
        label = str(folder.relative_to(root / "datasets"))
        if (
            not audit_path.exists()
            or _json(audit_path).get("full_matrix_site_year_complete") is not True
        ):
            skipped.append(label)
            continue
        plans.append(_preflight_year(folder, root, set(buildings)))
    result = {
        "completed_site_years": [
            str(p["folder"].relative_to(root / "datasets")) for p in plans
        ],
        "skipped_site_years": skipped,
        "candidate_weather_files": 0,
        "new_archives": 0,
        "validated_existing_archives": 0,
        "plain_files_removed": 0,
        "uncompressed_bytes": 0,
        "compressed_bytes": 0,
        "bytes_saved": 0,
    }
    for plan in plans:
        for case in plan["cases"]:
            if case["case_id"] == "reference":
                continue
            saved, created, removed = _archive_case(case, plan)
            result["candidate_weather_files"] += 1
            result["new_archives" if created else "validated_existing_archives"] += 1
            result["plain_files_removed"] += int(removed > 0)
            result["uncompressed_bytes"] += saved["uncompressed_bytes"]
            result["compressed_bytes"] += saved["compressed_bytes"]
            result["bytes_saved"] += removed - (
                saved["compressed_bytes"] if created else 0
            )
    _local(environment, root).mkdir(parents=True, exist_ok=True)
    execution_receipt = environment / f"post-weather-storage-{uuid.uuid4().hex}.json"
    _dump(
        execution_receipt,
        {
            "schema_version": 1,
            "operation": "lossless_candidate_weather_gzip",
            "executed_at_utc": datetime.now(timezone.utc).isoformat(),
            "utility_path": str(Path(__file__).resolve()),
            "utility_sha256": _sha(Path(__file__)),
            "python_version": platform.python_version(),
            "campaign_root": str(root),
            "matrix_sha256": _sha(matrix_path),
            "result": result,
            "scientific_interpretation": "Storage only; frozen harness and scientific receipts unchanged",
        },
    )
    return result | {"execution_receipt": str(execution_receipt)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign_root", type=Path)
    args = parser.parse_args()
    print(json.dumps(archive_completed_weather(args.campaign_root), indent=2))


if __name__ == "__main__":
    main()
