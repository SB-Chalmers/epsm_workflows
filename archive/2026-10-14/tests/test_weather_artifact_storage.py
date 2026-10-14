"""Keep completed model and hourly artifacts exact through storage/recovery."""

import gzip
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from examples.weather_imputation_sensitivity import artifact_storage as A
from examples.weather_imputation_sensitivity import run_campaign as R

MODEL = b'{"Version":{"Version 1":{"version_identifier":"24.2"}}}\n'
CSV = b"lst_hour_ending,heating_kw,cooling_kw\r\n2023-01-01T01:00:00+01:00,1.125,0\r\n"


def digest(value):
    return hashlib.sha256(value).hexdigest()


def completed(folder):
    """Create a successful runner receipt without importing or invoking Core."""
    folder.mkdir(parents=True)
    (folder / "model.epJSON").write_bytes(MODEL)
    (folder / "hourly-loads.csv").write_bytes(CSV)
    receipt = {
        "core_diagnostics_accepted": True,
        "severe_errors": 0,
        "fatal_errors": 0,
        "core_run_id": "completed-simulation-identity",
        "model_sha256": digest(MODEL),
        "metrics": {"annual_heating_kwh": 1.125, "annual_cooling_kwh": 0},
        "hourly_loads_path": str(folder / "hourly-loads.csv"),
    }
    (folder / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def test_roundtrip_and_hardlinks_preserve_bytes_and_original_receipts(
    tmp_path, monkeypatch
):
    folder = tmp_path / "first"
    completed(folder)
    original_receipt = (folder / "receipt.json").read_bytes()
    # Core is not a storage dependency, including during lazy publication imports.
    monkeypatch.setitem(sys.modules, "epsm_core", None)
    metadata = A.compact_artifacts(folder)
    canonical = Path(metadata["model"]["canonical_path"])
    assert os.path.samefile(canonical, folder / "model.epJSON")
    assert (folder / "model.epJSON").read_bytes() == MODEL
    archive = folder / "hourly-loads.csv.gz"
    assert gzip.decompress(archive.read_bytes()) == CSV
    assert metadata["hourly"]["gzip_sha256"] == digest(archive.read_bytes())
    assert metadata["hourly"]["uncompressed_sha256"] == digest(CSV)
    assert not (folder / "hourly-loads.csv").exists()
    assert (folder / "receipt.json").read_bytes() == original_receipt
    assert metadata["hourly"]["logical_hourly_loads_path"] == str(
        folder / "hourly-loads.csv"
    )


def test_identical_models_share_one_inode_and_compaction_is_idempotent(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    completed(first)
    completed(second)
    initial = A.compact_artifacts(first)
    A.compact_artifacts(second)
    assert os.path.samefile(first / "model.epJSON", second / "model.epJSON")
    archive = (first / "hourly-loads.csv.gz").read_bytes()
    assert A.compact_artifacts(first) == initial
    assert (first / "hourly-loads.csv.gz").read_bytes() == archive
    assert len(list((tmp_path / ".models").glob("*.epJSON"))) == 1
    assert not list(tmp_path.rglob("*.tmp"))


def test_current_model_tampering_stops_before_any_mutation(tmp_path):
    folder = tmp_path / "first"
    completed(folder)
    (folder / "model.epJSON").write_bytes(MODEL + b"changed")
    with pytest.raises(ValueError, match="Current compiled model"):
        A.compact_artifacts(folder)
    assert (folder / "hourly-loads.csv").read_bytes() == CSV
    assert not (tmp_path / ".models").exists()


def test_existing_canonical_model_is_verified_before_replacing_current_model(tmp_path):
    folder = tmp_path / "first"
    completed(folder)
    canonical = tmp_path / ".models" / f"{digest(MODEL)}.epJSON"
    canonical.parent.mkdir()
    canonical.write_bytes(MODEL + b"wrong canonical bytes")
    inode = (folder / "model.epJSON").stat().st_ino
    with pytest.raises(ValueError, match="Canonical compiled model"):
        A.compact_artifacts(folder)
    assert (folder / "model.epJSON").stat().st_ino == inode
    assert (folder / "hourly-loads.csv").read_bytes() == CSV
    assert not (folder / "hourly-loads.csv.gz").exists()


@pytest.mark.parametrize(
    "changed",
    [
        "gzip",
        "uncompressed_sha256",
        "gzip_sha256",
        "core_run_id",
        "uncompressed_bytes",
        "csv",
    ],
)
def test_existing_archive_and_receipt_tampering_never_deletes_current_csv(
    tmp_path, changed
):
    folder = tmp_path / "first"
    completed(folder)
    A.compact_artifacts(folder)
    csv_path = folder / "hourly-loads.csv"
    csv_path.write_bytes(CSV)
    sidecar = folder / "hourly-archive.json"
    saved = json.loads(sidecar.read_text())
    if changed == "gzip":
        archive = folder / "hourly-loads.csv.gz"
        archive.write_bytes(archive.read_bytes() + b"changed")
    elif changed == "csv":
        csv_path.write_bytes(CSV + b"changed")
    else:
        saved[changed] = (
            "0" * 64
            if "sha256" in changed
            else 999
            if changed == "uncompressed_bytes"
            else "other run"
        )
        sidecar.write_text(json.dumps(saved))
    preserved = csv_path.read_bytes()
    with pytest.raises(ValueError, match="Hourly|hourly|Decoded"):
        A.compact_artifacts(folder)
    assert csv_path.read_bytes() == preserved


def test_matching_compressed_hash_still_requires_successful_exact_decoding(tmp_path):
    folder = tmp_path / "first"
    completed(folder)
    A.compact_artifacts(folder)
    (folder / "hourly-loads.csv").write_bytes(CSV)
    archive = folder / "hourly-loads.csv.gz"
    archive.write_bytes(b"not valid gzip data")
    sidecar = folder / "hourly-archive.json"
    saved = json.loads(sidecar.read_text())
    saved["gzip_sha256"] = digest(archive.read_bytes())
    saved["compressed_bytes"] = archive.stat().st_size
    sidecar.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="decoded losslessly"):
        A.compact_artifacts(folder)
    assert (folder / "hourly-loads.csv").read_bytes() == CSV


@pytest.mark.parametrize("missing", ["archive", "sidecar"])
def test_archive_without_both_publication_parts_is_not_trusted(tmp_path, missing):
    folder = tmp_path / "first"
    completed(folder)
    A.compact_artifacts(folder)
    (folder / "hourly-loads.csv").write_bytes(CSV)
    (
        folder
        / ("hourly-loads.csv.gz" if missing == "archive" else "hourly-archive.json")
    ).unlink()
    with pytest.raises(ValueError, match="must both exist"):
        A.compact_artifacts(folder)
    assert (folder / "hourly-loads.csv").exists()


@pytest.mark.parametrize(
    "key,value",
    [
        ("core_diagnostics_accepted", False),
        ("core_diagnostics_accepted", None),
        ("severe_errors", 1),
        ("fatal_errors", 1),
        ("core_run_id", ""),
        ("metrics", {}),
        ("model_sha256", "../unsafe"),
        ("hourly_loads_path", "other.csv"),
    ],
)
def test_incomplete_core_receipt_cannot_trigger_compaction(tmp_path, key, value):
    folder = tmp_path / "first"
    receipt = completed(folder)
    receipt[key] = value
    (folder / "receipt.json").write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="complete accepted Core"):
        A.compact_artifacts(folder)
    assert (folder / "model.epJSON").read_bytes() == MODEL
    assert (folder / "hourly-loads.csv").read_bytes() == CSV
    assert not (tmp_path / ".models").exists()


def test_missing_completed_receipt_or_hourly_data_preserves_model(tmp_path):
    folder = tmp_path / "first"
    completed(folder)
    receipt = (folder / "receipt.json").read_bytes()
    (folder / "receipt.json").unlink()
    with pytest.raises(ValueError, match="Unreadable artifact receipt"):
        A.compact_artifacts(folder)
    (folder / "receipt.json").write_bytes(receipt)
    (folder / "hourly-loads.csv").unlink()
    with pytest.raises(ValueError, match="neither hourly CSV"):
        A.compact_artifacts(folder)
    assert (folder / "model.epJSON").read_bytes() == MODEL
    assert not (tmp_path / ".models").exists()


@pytest.mark.parametrize("corrupt_after_interruption", [False, True])
def test_data_published_without_final_receipt_recovers_only_exact_prepared_bytes(
    tmp_path, monkeypatch, corrupt_after_interruption
):
    folder = tmp_path / "first"
    completed(folder)
    sidecar = folder / "hourly-archive.json"
    dump = R.dump

    def interrupt_final_receipt(path, value):
        if Path(path) == sidecar:
            raise OSError("Interrupted hourly receipt publication")
        return dump(path, value)

    monkeypatch.setattr(R, "dump", interrupt_final_receipt)
    with pytest.raises(OSError, match="Interrupted hourly"):
        A.compact_artifacts(folder)
    assert (folder / "hourly-loads.csv").read_bytes() == CSV
    assert (folder / "hourly-loads.csv.gz").exists()
    assert Path(str(sidecar) + ".pending.json").exists()
    assert not sidecar.exists()
    monkeypatch.setattr(R, "dump", dump)
    if corrupt_after_interruption:
        archive = folder / "hourly-loads.csv.gz"
        archive.write_bytes(archive.read_bytes() + b"changed")
        with pytest.raises(ValueError, match="publication bytes changed"):
            A.compact_artifacts(folder)
        assert (folder / "hourly-loads.csv").read_bytes() == CSV
        assert not sidecar.exists()
    else:
        metadata = A.compact_artifacts(folder)
        assert metadata["hourly"]["uncompressed_sha256"] == digest(CSV)
        assert not (folder / "hourly-loads.csv").exists()
        assert not Path(str(sidecar) + ".pending.json").exists()
        assert gzip.decompress((folder / "hourly-loads.csv.gz").read_bytes()) == CSV


def test_interruption_before_data_publication_keeps_csv_and_can_retry(
    tmp_path, monkeypatch
):
    folder = tmp_path / "first"
    completed(folder)
    archive = folder / "hourly-loads.csv.gz"
    replace = Path.replace

    def interrupt_archive_replace(path, destination):
        if Path(destination) == archive:
            raise OSError("Interrupted before archive data publication")
        return replace(path, destination)

    monkeypatch.setattr(Path, "replace", interrupt_archive_replace)
    with pytest.raises(OSError, match="before archive"):
        A.compact_artifacts(folder)
    assert (folder / "hourly-loads.csv").read_bytes() == CSV
    assert not archive.exists()
    assert (folder / "hourly-archive.json.pending.json").exists()
    monkeypatch.setattr(Path, "replace", replace)
    A.compact_artifacts(folder)
    assert gzip.decompress(archive.read_bytes()) == CSV
    assert not (folder / "hourly-archive.json.pending.json").exists()


def test_interrupted_hardlink_replacement_preserves_original_model_and_csv(
    tmp_path, monkeypatch
):
    first, second = tmp_path / "first", tmp_path / "second"
    completed(first)
    completed(second)
    A.compact_artifacts(first)
    replace = Path.replace

    def interrupt_model_replace(path, destination):
        if Path(destination) == second / "model.epJSON":
            raise OSError("Interrupted hardlink replacement")
        return replace(path, destination)

    monkeypatch.setattr(Path, "replace", interrupt_model_replace)
    with pytest.raises(OSError, match="hardlink"):
        A.compact_artifacts(second)
    assert (second / "model.epJSON").read_bytes() == MODEL
    assert (second / "hourly-loads.csv").read_bytes() == CSV
    assert not list(second.glob("*.tmp"))
    monkeypatch.setattr(Path, "replace", replace)
    A.compact_artifacts(second)
    assert os.path.samefile(first / "model.epJSON", second / "model.epJSON")
