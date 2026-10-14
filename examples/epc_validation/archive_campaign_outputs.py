"""Losslessly compress completed native outputs; active runs are never touched.

Receipts and scalar results remain unchanged. Every compressed output is checked
by decompression and has a restoration command and original SHA-256 recorded.
"""

import argparse
import gzip
import hashlib
import os
import time
from pathlib import Path

from epsm_workflows.campaign import atomic_json, file_sha256
from epsm_workflows.cohort_transfer.common import read


def archive_file(path):
    before = path.stat()
    destination = path.with_suffix(path.suffix + ".gz")
    temporary = destination.with_suffix(".gz.tmp")
    original = hashlib.sha256()
    with (
        path.open("rb") as source,
        gzip.open(temporary, "wb", compresslevel=1) as target,
    ):
        for block in iter(lambda: source.read(1024 * 1024), b""):
            original.update(block)
            target.write(block)
    verified = hashlib.sha256()
    with gzip.open(temporary, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            verified.update(block)
    after = path.stat()
    if verified.digest() != original.digest() or (
        after.st_size,
        after.st_mtime_ns,
        after.st_ino,
    ) != (before.st_size, before.st_mtime_ns, before.st_ino):
        raise ValueError(f"Native archive verification failed: {path}")
    os.replace(temporary, destination)
    evidence = {
        "original_file": path.name,
        "original_bytes": before.st_size,
        "original_sha256": original.hexdigest(),
        "archive_bytes": destination.stat().st_size,
        "archive_sha256": file_sha256(destination),
        "lossless_roundtrip_verified": True,
        "restore": f"gzip -dk {destination.name}",
    }
    record = (
        path.with_name("sql-archive.json")
        if path.suffix == ".sql"
        else path.with_name(path.name + "-archive.json")
    )
    atomic_json(record, evidence)
    path.unlink()
    return before.st_size - destination.stat().st_size


def watch(root):
    root = Path(root)
    count, saved = 0, 0
    while True:
        for receipt in (root / "simulations").glob("*/receipt.json"):
            # The receipt is written only after the runner and readers finish.
            for path in (receipt.parent / "energyplus").glob("eplusout.*"):
                if (
                    path.suffix in {".sql", ".eso", ".mtr", ".csv", ".audit"}
                    and path.stat().st_size >= 1024 * 1024
                ):
                    saved += archive_file(path)
                    count += 1
        atomic_json(
            root / "artifacts/native-archival.json",
            {"archived_files": count, "bytes_saved": saved, "lossless_verified": True},
        )
        execution = root / "execution.json"
        if execution.exists() and read(execution)["stage"] in {"completed", "failed"}:
            break
        time.sleep(30)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", required=True)
    watch(parser.parse_args().campaign)
