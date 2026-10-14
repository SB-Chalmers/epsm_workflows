"""Retain complete EnergyPlus SQL with verified lossless baseline compression.

Reference databases use self-contained XZ. Scenario databases use the matching
reference database as a Zstandard dictionary. Every restored byte is checked
before a cached simulation can be consumed. The dictionary remains immutable
and can be recovered only from its independently verified reference archive.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import lzma
import shutil
import subprocess
from pathlib import Path

_ARCHIVES = {
    "xz_sha256": "eplusout.sql.xz",
    "gzip_sha256": "eplusout.sql.gz",
    "zstd_sha256": "eplusout.sql.zst",
}
_DICTIONARY = "../reference/sql-dictionary.sql"


def _fingerprint(path):
    """Hash complete file contents without loading them into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _hash_key(receipt):
    """Require precisely one supported compressed artifact hash field."""
    keys = set(receipt) & set(_ARCHIVES)
    if len(keys) != 1 or not receipt.get("uncompressed_sha256"):
        raise ValueError("Invalid EnergyPlus SQL archive receipt")
    return keys.pop()


def _state(folder):
    """Recover pending publications and verify the selected archive bytes."""
    from . import run_campaign as runner

    folder = Path(folder)
    receipt_path = folder / "sql-archive.json"
    pending = Path(str(receipt_path) + ".pending.json")
    if pending.exists():
        prepared = json.loads(pending.read_text())
        key = _hash_key(prepared)
        runner.recover_publication(folder / _ARCHIVES[key], receipt_path, hash_key=key)
    if not receipt_path.exists():
        if any((folder / filename).exists() for filename in _ARCHIVES.values()):
            raise ValueError("EnergyPlus SQL archive receipt missing")
        return None
    receipt = json.loads(receipt_path.read_text())
    key = _hash_key(receipt)
    archive = folder / _ARCHIVES[key]
    if not archive.exists() or _fingerprint(archive) != receipt[key]:
        raise ValueError("Archived EnergyPlus SQL changed")
    if any(
        (folder / filename).exists()
        for other, filename in _ARCHIVES.items()
        if other != key
    ):
        raise ValueError("Ambiguous EnergyPlus SQL archives")
    sql = folder / "eplusout.sql"
    if sql.exists() and _fingerprint(sql) != receipt["uncompressed_sha256"]:
        raise ValueError("Existing EnergyPlus SQL differs from its receipt")
    return archive, receipt, key


def _zstd_identity(zstd_path):
    """Record the exact external executable used for lossless compression."""
    executable = Path(zstd_path)
    return {
        "zstd_executable_sha256": _fingerprint(executable),
        "zstd_version": subprocess.check_output(
            [str(executable), "--version"], text=True
        ).strip(),
    }


def _dictionary(folder, expected=None):
    """Validate or atomically recover an immutable matching reference dictionary."""
    reference = Path(folder).parent / "reference"
    state = _state(reference)
    if state is None or state[2] not in {"xz_sha256", "gzip_sha256"}:
        raise ValueError("Self-contained reference SQL archive missing")
    archive, receipt, key = state
    baseline_hash = receipt["uncompressed_sha256"]
    if expected is not None and expected != baseline_hash:
        raise ValueError("Reference SQL dictionary differs from its receipt")
    dictionary = reference / "sql-dictionary.sql"
    if dictionary.exists():
        if _fingerprint(dictionary) != baseline_hash:
            raise ValueError("Reference SQL dictionary differs from its receipt")
        return dictionary, baseline_hash
    temporary = reference / "sql-dictionary.sql.restore.tmp"
    opener = lzma.open if key == "xz_sha256" else gzip.open
    try:
        with opener(archive, "rb") as source, temporary.open("wb") as dest:
            shutil.copyfileobj(source, dest)
        if _fingerprint(temporary) != baseline_hash:
            raise ValueError("Restored reference SQL differs from its receipt")
        temporary.replace(dictionary)
    finally:
        temporary.unlink(missing_ok=True)
    return dictionary, baseline_hash


def _validated_dictionary(folder, receipt, zstd_path):
    """Check dictionary location, contents and pinned decoder identity first."""
    if receipt.get("dictionary_path") != _DICTIONARY:
        raise ValueError("Unexpected reference SQL dictionary path")
    expected = receipt.get("dictionary_sha256")
    if not expected:
        raise ValueError("Reference SQL dictionary hash missing")
    executable_hash = receipt.get("zstd_executable_sha256")
    if executable_hash and executable_hash != _fingerprint(zstd_path):
        raise ValueError("Zstandard executable differs from its archive receipt")
    return _dictionary(folder, expected)[0]


def restore_sql(folder, zstd_path):
    """Validate cached SQL or atomically restore its exact archived bytes.

    Return the SQL path when available, or ``None`` for a fresh simulation.
    Existing plain databases still require their archive and dictionary checks.
    Legacy gzip receipts remain readable without newly added metadata fields.
    """
    folder = Path(folder)
    sql = folder / "eplusout.sql"
    state = _state(folder)
    if state is None:
        return sql if sql.exists() else None
    archive, receipt, key = state
    dictionary = (
        _validated_dictionary(folder, receipt, zstd_path)
        if key == "zstd_sha256"
        else None
    )
    if sql.exists():
        return sql
    temporary = folder / "eplusout.sql.restore.tmp"
    try:
        if dictionary is not None:
            with temporary.open("wb") as dest:
                subprocess.run(
                    [
                        str(zstd_path),
                        "--quiet",
                        "--decompress",
                        f"--patch-from={dictionary}",
                        "--stdout",
                        str(archive),
                    ],
                    stdout=dest,
                    check=True,
                )
        else:
            opener = lzma.open if key == "xz_sha256" else gzip.open
            with opener(archive, "rb") as source, temporary.open("wb") as dest:
                shutil.copyfileobj(source, dest)
        if _fingerprint(temporary) != receipt["uncompressed_sha256"]:
            raise ValueError("Restored EnergyPlus SQL differs from its receipt")
        temporary.replace(sql)
    finally:
        temporary.unlink(missing_ok=True)
    return sql


def compact_sql(folder, zstd_path):
    """Publish verified lossless SQL before removing its uncompressed database.

    A noncampaign folder without a sibling reference uses self-contained XZ.
    Valid existing archives are retained unchanged when SQL is restored again.
    """
    from . import run_campaign as runner

    folder = Path(folder)
    sql = folder / "eplusout.sql"
    state = _state(folder)
    if state is not None:
        _, receipt, key = state
        if key == "zstd_sha256":
            _validated_dictionary(folder, receipt, zstd_path)
        sql.unlink(missing_ok=True)
        return
    if not sql.exists():
        return
    receipt = {"uncompressed_sha256": _fingerprint(sql)}
    use_dictionary = (
        folder.name != "reference" and (folder.parent / "reference").exists()
    )
    if use_dictionary:
        dictionary, dictionary_hash = _dictionary(folder)
        key = "zstd_sha256"
        receipt |= {
            "method": "zstd_patch_from_reference",
            "dictionary_path": _DICTIONARY,
            "dictionary_sha256": dictionary_hash,
            **_zstd_identity(zstd_path),
        }
    else:
        key = "xz_sha256"
        receipt["method"] = "xz"
    archive = folder / _ARCHIVES[key]
    temporary = Path(str(archive) + ".tmp")
    try:
        if use_dictionary:
            subprocess.run(
                [
                    str(zstd_path),
                    "--quiet",
                    "--force",
                    "-12",
                    f"--patch-from={dictionary}",
                    str(sql),
                    "-o",
                    str(temporary),
                ],
                check=True,
            )
        else:
            with sql.open("rb") as source, lzma.open(temporary, "wb", preset=3) as dest:
                shutil.copyfileobj(source, dest)
        receipt[key] = _fingerprint(temporary)
        runner.publish_artifact(
            temporary,
            archive,
            folder / "sql-archive.json",
            receipt,
            hash_key=key,
        )
        sql.unlink()
    finally:
        temporary.unlink(missing_ok=True)
