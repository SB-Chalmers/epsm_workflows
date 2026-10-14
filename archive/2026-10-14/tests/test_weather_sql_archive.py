"""Verify lossless scenario SQL compression and resumable integrity checks."""

import gzip
import hashlib
import json
import random
import shutil
from pathlib import Path
from unittest.mock import Mock

import pytest

from examples.weather_imputation_sensitivity import run_campaign as R
from examples.weather_imputation_sensitivity import sql_archive as S


@pytest.fixture
def zstd():
    """Locate an installed executable without downloading campaign dependencies."""
    found = shutil.which("zstd")
    bundled = Path("/opt/homebrew/Caskroom/miniconda/base/bin/zstd")
    if found:
        return Path(found)
    if bundled.exists():
        return bundled
    pytest.skip("Zstandard CLI is unavailable")


@pytest.fixture
def campaign_sql(tmp_path, zstd):
    """Create differing byte lengths with realistic shared binary database content."""
    reference = tmp_path / "building" / "reference"
    candidate = reference.parent / "candidate"
    reference.mkdir(parents=True)
    candidate.mkdir()
    baseline = b"SQLite format 3\x00" + random.Random(9).randbytes(256 * 1024)
    changed = baseline[:90000] + b"different output row" * 200 + baseline[90003:]
    (reference / "eplusout.sql").write_bytes(baseline)
    (candidate / "eplusout.sql").write_bytes(changed)
    S.compact_sql(reference, zstd)
    S.compact_sql(candidate, zstd)
    return reference, candidate, baseline, changed


def test_reference_and_changed_length_scenario_roundtrip(campaign_sql, zstd):
    reference, candidate, baseline, changed = campaign_sql
    assert len(baseline) != len(changed)
    assert not (reference / "eplusout.sql").exists()
    assert not (candidate / "eplusout.sql").exists()
    assert (reference / "eplusout.sql.xz").exists()
    delta = candidate / "eplusout.sql.zst"
    assert delta.stat().st_size < len(changed) // 2
    receipt = json.loads((candidate / "sql-archive.json").read_text())
    assert receipt["dictionary_path"] == "../reference/sql-dictionary.sql"
    assert receipt["dictionary_sha256"] == hashlib.sha256(baseline).hexdigest()
    assert receipt["zstd_version"]
    assert receipt["zstd_executable_sha256"] == R.fingerprint(zstd)
    assert S.restore_sql(reference, zstd).read_bytes() == baseline
    assert S.restore_sql(candidate, zstd).read_bytes() == changed


def test_existing_valid_archive_is_not_recompressed(campaign_sql, zstd, monkeypatch):
    _, candidate, _, changed = campaign_sql
    archive = candidate / "eplusout.sql.zst"
    archive_before = archive.read_bytes()
    receipt_before = (candidate / "sql-archive.json").read_bytes()
    assert S.restore_sql(candidate, zstd).read_bytes() == changed
    monkeypatch.setattr(
        S.subprocess,
        "run",
        Mock(side_effect=AssertionError("Validated archives must not recompress")),
    )
    S.compact_sql(candidate, zstd)
    assert not (candidate / "eplusout.sql").exists()
    assert archive.read_bytes() == archive_before
    assert (candidate / "sql-archive.json").read_bytes() == receipt_before


@pytest.mark.parametrize("plain_exists", [False, True])
def test_altered_dictionary_is_rejected_before_decoding(
    campaign_sql, zstd, monkeypatch, plain_exists
):
    reference, candidate, _, _ = campaign_sql
    if plain_exists:
        S.restore_sql(candidate, zstd)
    (reference / "sql-dictionary.sql").write_bytes(b"modified dictionary")
    monkeypatch.setattr(
        S.subprocess,
        "run",
        Mock(side_effect=AssertionError("Changed dictionaries must not be decoded")),
    )
    with pytest.raises(ValueError, match="dictionary differs"):
        S.restore_sql(candidate, zstd)


def test_absent_dictionary_recovers_only_exact_verified_baseline(campaign_sql, zstd):
    reference, candidate, baseline, changed = campaign_sql
    dictionary = reference / "sql-dictionary.sql"
    dictionary.unlink()
    assert S.restore_sql(candidate, zstd).read_bytes() == changed
    assert dictionary.read_bytes() == baseline
    assert not (reference / "eplusout.sql").exists()


@pytest.mark.parametrize("dictionary_exists", [False, True])
def test_altered_reference_archive_is_rejected(campaign_sql, zstd, dictionary_exists):
    reference, candidate, _, _ = campaign_sql
    if not dictionary_exists:
        (reference / "sql-dictionary.sql").unlink()
    (reference / "eplusout.sql.xz").write_bytes(b"altered reference")
    with pytest.raises(ValueError, match="Archived EnergyPlus SQL changed"):
        S.restore_sql(candidate, zstd)
    assert not (candidate / "eplusout.sql").exists()


def test_missing_baseline_does_not_rebuild_from_an_unverified_dictionary(
    campaign_sql, zstd
):
    reference, candidate, _, _ = campaign_sql
    (reference / "eplusout.sql.xz").unlink()
    with pytest.raises(ValueError, match="Archived EnergyPlus SQL changed"):
        S.restore_sql(candidate, zstd)
    assert not (candidate / "eplusout.sql").exists()


def test_archive_without_receipt_is_rejected(campaign_sql, zstd):
    _, candidate, _, _ = campaign_sql
    (candidate / "sql-archive.json").unlink()
    with pytest.raises(ValueError, match="archive receipt missing"):
        S.restore_sql(candidate, zstd)


@pytest.mark.parametrize("operation", [S.restore_sql, S.compact_sql])
def test_altered_scenario_archive_rejected_even_with_valid_plain_sql(
    campaign_sql, zstd, operation
):
    _, candidate, _, changed = campaign_sql
    (candidate / "eplusout.sql").write_bytes(changed)
    (candidate / "eplusout.sql.zst").write_bytes(b"altered scenario")
    with pytest.raises(ValueError, match="Archived EnergyPlus SQL changed"):
        operation(candidate, zstd)


def test_altered_existing_sql_is_rejected(campaign_sql, zstd):
    _, candidate, _, _ = campaign_sql
    (candidate / "eplusout.sql").write_bytes(b"altered plain SQL")
    with pytest.raises(ValueError, match="Existing EnergyPlus SQL differs"):
        S.restore_sql(candidate, zstd)


@pytest.mark.parametrize(
    "key,value,reason",
    [
        ("uncompressed_sha256", "0" * 64, "Restored EnergyPlus SQL differs"),
        ("dictionary_sha256", "0" * 64, "dictionary differs"),
        ("dictionary_path", "../../unrelated.sql", "Unexpected reference"),
        ("zstd_executable_sha256", "0" * 64, "executable differs"),
    ],
)
def test_altered_receipt_is_rejected(campaign_sql, zstd, key, value, reason):
    _, candidate, _, _ = campaign_sql
    receipt_path = candidate / "sql-archive.json"
    receipt = json.loads(receipt_path.read_text())
    receipt[key] = value
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match=reason):
        S.restore_sql(candidate, zstd)
    assert not (candidate / "eplusout.sql").exists()
    assert not (candidate / "eplusout.sql.restore.tmp").exists()


def test_failed_decoder_does_not_publish_partial_sql(campaign_sql, zstd, monkeypatch):
    _, candidate, _, _ = campaign_sql

    def interrupted_decoder(*args, stdout, **kwargs):
        """Write an incomplete restore before simulating a decoder failure."""
        stdout.write(b"incomplete restored SQL")
        raise OSError("Interrupted decoder")

    monkeypatch.setattr(S.subprocess, "run", interrupted_decoder)
    with pytest.raises(OSError, match="Interrupted decoder"):
        S.restore_sql(candidate, zstd)
    assert not (candidate / "eplusout.sql").exists()
    assert not (candidate / "eplusout.sql.restore.tmp").exists()


@pytest.mark.parametrize("patched", [False, True])
def test_interrupted_receipt_publication_recovers_losslessly(
    campaign_sql, tmp_path, zstd, monkeypatch, patched
):
    reference, _, baseline, changed = campaign_sql
    folder = reference.parent / "another" if patched else tmp_path / "standalone"
    folder.mkdir()
    sql = folder / "eplusout.sql"
    sql.write_bytes(changed if patched else baseline)
    receipt_path = folder / "sql-archive.json"
    original_dump = R.dump

    def interrupted_final_receipt(path, value):
        """Interrupt publication after archive rename and before final receipt."""
        if Path(path) == receipt_path:
            raise OSError("Interrupted receipt publication")
        original_dump(path, value)

    with monkeypatch.context() as patch:
        patch.setattr(R, "dump", interrupted_final_receipt)
        with pytest.raises(OSError, match="Interrupted receipt publication"):
            S.compact_sql(folder, zstd)
    assert sql.exists()
    assert Path(str(receipt_path) + ".pending.json").exists()
    assert S.restore_sql(folder, zstd).read_bytes() == sql.read_bytes()
    assert receipt_path.exists()
    assert not Path(str(receipt_path) + ".pending.json").exists()
    S.compact_sql(folder, zstd)
    assert not sql.exists()
    assert S.restore_sql(folder, zstd).read_bytes() == (
        changed if patched else baseline
    )


def test_noncampaign_folder_falls_back_to_self_contained_xz(tmp_path, zstd):
    folder = tmp_path / "case"
    folder.mkdir()
    original = b"complete self-contained SQL" * 100
    (folder / "eplusout.sql").write_bytes(original)
    S.compact_sql(folder, zstd)
    assert (folder / "eplusout.sql.xz").exists()
    assert S.restore_sql(folder, zstd).read_bytes() == original


def test_legacy_gzip_receipt_remains_readable(tmp_path, zstd):
    original = b"legacy complete SQL" * 100
    archive = tmp_path / "eplusout.sql.gz"
    archive.write_bytes(gzip.compress(original))
    R.dump(
        tmp_path / "sql-archive.json",
        {
            "gzip_sha256": R.fingerprint(archive),
            "uncompressed_sha256": hashlib.sha256(original).hexdigest(),
        },
    )
    assert S.restore_sql(tmp_path, zstd).read_bytes() == original
