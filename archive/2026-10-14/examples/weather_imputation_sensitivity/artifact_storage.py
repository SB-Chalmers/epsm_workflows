"""Lossless storage for completed weather-sensitivity simulation artifacts.

Compiled models retain their original paths and exact bytes through hardlinks to
a checked content-addressed pool. Hourly CSVs use standard gzip with a separate
archive receipt; the completed Core receipt is never rewritten. Its
``hourly_loads_path`` remains the logical decompressed CSV name. Consumers may
stream ``hourly-loads.csv.gz`` or restore its exact bytes to that logical name.
No Core imports or simulation/result reinterpretation are required.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import uuid
import zlib
from pathlib import Path

BLOCK_BYTES = 1024 * 1024


def _sha(path: Path) -> str:
    """Fingerprint exact file bytes using bounded memory."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(BLOCK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _valid_sha(value) -> bool:
    """Require the lowercase SHA-256 spelling emitted by the campaign."""
    return (
        isinstance(value, str)
        and len(value) == 64
        and set(value) <= set("0123456789abcdef")
    )


def _json(path: Path) -> dict:
    """Load an object receipt without accepting malformed sidecars."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Unreadable artifact receipt: {path.name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Artifact receipt must be an object: {path.name}")
    return value


def _completed_receipt(folder: Path) -> dict:
    """Require successful Core completion before touching any stored artifact."""
    receipt = _json(folder / "receipt.json")
    valid = (
        receipt.get("core_diagnostics_accepted") is True
        and type(receipt.get("severe_errors")) is int
        and receipt["severe_errors"] == 0
        and type(receipt.get("fatal_errors")) is int
        and receipt["fatal_errors"] == 0
        and isinstance(receipt.get("metrics"), dict)
        and bool(receipt["metrics"])
        and isinstance(receipt.get("core_run_id"), str)
        and bool(receipt["core_run_id"].strip())
        and _valid_sha(receipt.get("model_sha256"))
        and isinstance(receipt.get("hourly_loads_path"), str)
        and Path(receipt["hourly_loads_path"]).name == "hourly-loads.csv"
    )
    if not valid:
        raise ValueError("Artifacts require a complete accepted Core receipt")
    return receipt


def _model_paths(folder: Path, receipt: dict) -> tuple[Path, Path]:
    """Check both current and existing pooled model bytes before mutation."""
    model = folder / "model.epJSON"
    canonical = folder.parent / ".models" / f"{receipt['model_sha256']}.epJSON"
    if canonical.exists() and (
        not canonical.is_file() or _sha(canonical) != receipt["model_sha256"]
    ):
        raise ValueError("Canonical compiled model differs from its content hash")
    if not model.is_file() or _sha(model) != receipt["model_sha256"]:
        raise ValueError(
            "Current compiled model differs from its completed Core receipt"
        )
    return model, canonical


def _link_model(model: Path, canonical: Path, expected: str) -> dict:
    """Publish an atomic hardlink while preserving the original model path."""
    canonical.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(model, canonical)
    except FileExistsError:
        # Independent completed folders can reach the same pool entry together.
        if _sha(canonical) != expected:
            raise ValueError(
                "Canonical compiled model changed during hardlink publication"
            )
    if not os.path.samefile(model, canonical):
        temporary = model.with_name(f".model.epJSON.{uuid.uuid4().hex}.link.tmp")
        try:
            os.link(canonical, temporary)
            if _sha(temporary) != expected:
                raise ValueError(
                    "Prepared compiled model hardlink differs from its receipt"
                )
            temporary.replace(model)
        finally:
            temporary.unlink(missing_ok=True)
    return {"sha256": expected, "canonical_path": str(canonical), "storage": "hardlink"}


def _archive_identity(saved: dict, core_receipt: dict) -> None:
    """Bind a gzip receipt to its completed run and logical original CSV name."""
    expected = {
        "schema_version": 1,
        "format": "gzip",
        "compression_level": 6,
        "archive_file": "hourly-loads.csv.gz",
        "uncompressed_file": "hourly-loads.csv",
        "core_run_id": core_receipt["core_run_id"],
        "model_sha256": core_receipt["model_sha256"],
        "logical_hourly_loads_path": core_receipt["hourly_loads_path"],
    }
    if any(saved.get(key) != value for key, value in expected.items()):
        raise ValueError("Hourly archive receipt differs from its completed Core run")
    if not all(
        _valid_sha(saved.get(key)) for key in ("gzip_sha256", "uncompressed_sha256")
    ):
        raise ValueError("Hourly archive receipt has invalid content hashes")
    if any(
        type(saved.get(key)) is not int or saved[key] < 0
        for key in ("compressed_bytes", "uncompressed_bytes")
    ):
        raise ValueError("Hourly archive receipt has invalid byte counts")


def _verify_hourly(archive: Path, saved: dict, csv_path: Path) -> None:
    """Verify compressed and decoded bytes before permitting original CSV removal."""
    if (
        _sha(archive) != saved["gzip_sha256"]
        or archive.stat().st_size != saved["compressed_bytes"]
    ):
        raise ValueError("Hourly gzip archive differs from its receipt")
    digest = hashlib.sha256()
    count = 0
    try:
        with gzip.open(archive, "rb") as stream:
            for block in iter(lambda: stream.read(BLOCK_BYTES), b""):
                digest.update(block)
                count += len(block)
    except (OSError, EOFError, zlib.error) as exc:
        raise ValueError("Hourly gzip archive cannot be decoded losslessly") from exc
    if (
        digest.hexdigest() != saved["uncompressed_sha256"]
        or count != saved["uncompressed_bytes"]
    ):
        raise ValueError("Decoded hourly CSV differs from its archive receipt")
    if csv_path.exists() and (
        not csv_path.is_file()
        or _sha(csv_path) != saved["uncompressed_sha256"]
        or csv_path.stat().st_size != saved["uncompressed_bytes"]
    ):
        raise ValueError("Existing hourly CSV differs from its archive receipt")


def _existing_hourly(folder: Path, core_receipt: dict) -> dict | None:
    """Recover interrupted publication and reject unrecorded or altered archives."""
    # Import only when called: run_campaign also consumes this helper module.
    from .run_campaign import recover_publication

    archive = folder / "hourly-loads.csv.gz"
    sidecar = folder / "hourly-archive.json"
    pending = Path(str(sidecar) + ".pending.json")
    if pending.exists():
        _archive_identity(_json(pending), core_receipt)
    recover_publication(archive, sidecar, hash_key="gzip_sha256")
    if archive.exists() or sidecar.exists():
        if not archive.is_file() or not sidecar.is_file():
            raise ValueError("Hourly archive and its receipt must both exist")
        saved = _json(sidecar)
        _archive_identity(saved, core_receipt)
        _verify_hourly(archive, saved, folder / "hourly-loads.csv")
        return saved
    if not (folder / "hourly-loads.csv").is_file():
        raise ValueError(
            "Completed simulation has neither hourly CSV nor a verified archive"
        )
    return None


def _archive_hourly(folder: Path, core_receipt: dict) -> dict:
    """Prepare gzip bytes and publish their receipt before removing the source CSV."""
    from .run_campaign import publish_artifact

    csv_path = folder / "hourly-loads.csv"
    archive = folder / "hourly-loads.csv.gz"
    sidecar = folder / "hourly-archive.json"
    temporary = folder / f".hourly-loads.csv.gz.{uuid.uuid4().hex}.tmp"
    digest = hashlib.sha256()
    count = 0
    try:
        with csv_path.open("rb") as source, temporary.open("wb") as destination:
            with gzip.GzipFile(
                filename="", fileobj=destination, mode="wb", compresslevel=6, mtime=0
            ) as compressed:
                for block in iter(lambda: source.read(BLOCK_BYTES), b""):
                    digest.update(block)
                    count += len(block)
                    compressed.write(block)
        saved = {
            "schema_version": 1,
            "format": "gzip",
            "compression_level": 6,
            "archive_file": archive.name,
            "uncompressed_file": csv_path.name,
            "gzip_sha256": _sha(temporary),
            "uncompressed_sha256": digest.hexdigest(),
            "compressed_bytes": temporary.stat().st_size,
            "uncompressed_bytes": count,
            "core_run_id": core_receipt["core_run_id"],
            "model_sha256": core_receipt["model_sha256"],
            "logical_hourly_loads_path": core_receipt["hourly_loads_path"],
            "interpretation": "Lossless hourly CSV archive; original Core receipt retains its logical decompressed hourly_loads_path",
        }
        # Verify actual gzip decoding and source stability before publication.
        _verify_hourly(temporary, saved, csv_path)
        publish_artifact(temporary, archive, sidecar, saved, hash_key="gzip_sha256")
        _verify_hourly(archive, _json(sidecar), csv_path)
        return saved
    finally:
        temporary.unlink(missing_ok=True)


def compact_artifacts(folder: str | Path) -> dict:
    """Reduce completed artifacts losslessly, with verified idempotent recovery.

    Current/pooled models and any existing hourly archive are checked before
    model-link replacement or CSV deletion. A gzip publication interrupted after
    data replacement is recoverable from its prepared receipt; an interruption
    before replacement retains the original CSV. Original Core receipt paths and
    hashes remain unchanged. Shared hardlinked model files should be treated as
    immutable; subsequent calls verify both their current and canonical bytes.
    """
    folder = Path(folder).resolve()
    core_receipt = _completed_receipt(folder)
    model, canonical = _model_paths(folder, core_receipt)
    hourly = _existing_hourly(folder, core_receipt)
    model_storage = _link_model(model, canonical, core_receipt["model_sha256"])
    if hourly is None:
        hourly = _archive_hourly(folder, core_receipt)
    # Recheck the live source immediately before deletion, even after a resume.
    csv_path = folder / "hourly-loads.csv"
    _verify_hourly(folder / "hourly-loads.csv.gz", hourly, csv_path)
    csv_path.unlink(missing_ok=True)
    return {"model": model_storage, "hourly": hourly}
