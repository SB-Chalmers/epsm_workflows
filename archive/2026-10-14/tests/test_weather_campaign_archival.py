"""Archive only finished campaign weather, with exact bytes and safe recovery."""

import csv
import gzip
import hashlib
import json
import sys
from pathlib import Path

import pytest

from examples import archive_weather_campaign_files as A

BUILDINGS = {f"building-{i}": {} for i in range(6)}
EPW = b"LOCATION,Illustrative,Sweden,SMHI,1,57,12,1,150\r\n" + (
    b"2018,7,27,14,60,reference,29.1,18.0,52,100100,0,0,0,700,650,80\r\n" * 100
)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def make_year(root, site="south", year=2018, *, complete=True, candidates=2):
    """Model the actual six-profile audit/checkpoint/catalogue storage boundary."""
    root.mkdir(parents=True, exist_ok=True)
    write_json(root / "matrix.json", {"buildings": BUILDINGS})
    folder = root / "datasets" / site / str(year)
    folder.mkdir(parents=True)
    records = []
    for case_id in ["reference", *[f"outage-{i}-era5" for i in range(candidates)]]:
        case = folder / "cases" / case_id
        case.mkdir(parents=True)
        (case / "weather.epw").write_bytes(EPW)
        write_json(
            case / "weather-receipt.json", {"epw_sha256": digest(EPW), "rows": 8760}
        )
        # Scientific and Core provenance must be unaffected by archival.
        (case / "events.json").write_text('{"events": []}\n')
        records.append(
            {
                "case_id": case_id,
                "method": "reference" if case_id == "reference" else "era5",
                "path": str(case / "weather.epw"),
            }
        )
    write_json(
        folder / "case-catalogue.json",
        {
            "cases": records,
            "unavailable": [{"case_id": "unsupported-donor", "status": "unsupported"}],
        },
    )
    summary = folder / "summary.csv"
    with summary.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["building_id", "case_id", "comment"])
        for building in BUILDINGS:
            for record in records:
                writer.writerow(
                    [building, record["case_id"], "quoted\nmultiline metadata"]
                )
    write_json(
        folder / "summary.csv.receipt.json",
        {"sha256": digest(summary.read_bytes()), "rows": len(BUILDINGS) * len(records)},
    )
    write_json(
        folder / "audit.json",
        {
            "full_matrix_site_year_complete": complete,
            "comparisons": len(BUILDINGS) * len(records),
        },
    )
    core = folder / "simulations" / "building-0" / "reference"
    core.mkdir(parents=True)
    (core / "receipt.json").write_text('{"core_run_id": "unchanged"}\n')
    (core / "model.epJSON").write_text('{"unchanged": true}\n')
    return folder


def candidate(folder, i=0):
    return folder / "cases" / f"outage-{i}-era5"


def snapshot(folder):
    return {
        str(p.relative_to(folder)): p.read_bytes()
        for p in folder.rglob("*")
        if p.is_file()
    }


def test_complete_year_roundtrips_active_year_and_baseline_are_untouched(
    tmp_path, monkeypatch
):
    root = tmp_path / "arbitrary-campaign-v5"
    finished = make_year(root)
    active = make_year(root, "north", complete=False)
    # An unfinished checkpoint is deliberately not recovered or even parsed.
    (active / "summary.csv.receipt.json.pending.json").write_text("not JSON")
    before_active, before_complete = snapshot(active), snapshot(finished)
    orphan = finished / "cases" / "unlisted"
    orphan.mkdir()
    (orphan / "weather.epw").write_bytes(b"never catalogue-owned")
    monkeypatch.setitem(sys.modules, "epsm_core", None)
    result = A.archive_completed_weather(root)
    assert result["completed_site_years"] == ["south/2018"]
    assert result["skipped_site_years"] == ["north/2018"]
    assert result["candidate_weather_files"] == result["new_archives"] == 2
    assert result["plain_files_removed"] == 2
    assert result["uncompressed_bytes"] == len(EPW) * 2
    assert (
        result["bytes_saved"]
        == result["uncompressed_bytes"] - result["compressed_bytes"]
        > 0
    )
    assert snapshot(active) == before_active
    assert (finished / "cases/reference/weather.epw").read_bytes() == EPW
    assert not (finished / "cases/reference/weather.epw.gz").exists()
    assert (orphan / "weather.epw").read_bytes() == b"never catalogue-owned"
    for relative, value in before_complete.items():
        if relative.endswith("weather.epw") and "/reference/" not in relative:
            continue
        assert (finished / relative).read_bytes() == value
    for i in range(2):
        case = candidate(finished, i)
        archive = case / "weather.epw.gz"
        assert gzip.decompress(archive.read_bytes()) == EPW
        assert archive.read_bytes()[4:8] == b"\0\0\0\0"  # fixed gzip mtime
        assert not (case / "weather.epw").exists()
        saved = json.loads((case / "weather-archive.json").read_text())
        assert saved["epw_sha256"] == digest(EPW)
        assert saved["gzip_sha256"] == digest(archive.read_bytes())
        assert saved["logical_weather_path"] == str(case / "weather.epw")
    execution = json.loads(Path(result["execution_receipt"]).read_text())
    assert execution["utility_sha256"] == digest(Path(A.__file__).read_bytes())
    assert execution["matrix_sha256"] == digest((root / "matrix.json").read_bytes())
    assert execution["result"]["bytes_saved"] == result["bytes_saved"]


def test_idempotence_and_identical_frozen_rerender(tmp_path):
    folder = make_year(tmp_path)
    first = A.archive_completed_weather(tmp_path)
    archives = {
        i: (candidate(folder, i) / "weather.epw.gz").read_bytes() for i in range(2)
    }
    second = A.archive_completed_weather(tmp_path)
    assert (
        second["new_archives"]
        == second["plain_files_removed"]
        == second["bytes_saved"]
        == 0
    )
    assert second["validated_existing_archives"] == 2
    assert second["compressed_bytes"] == first["compressed_bytes"]
    for i in range(2):
        (candidate(folder, i) / "weather.epw").write_bytes(EPW)
    resumed = A.archive_completed_weather(tmp_path)
    assert resumed["new_archives"] == 0
    assert resumed["bytes_saved"] == len(EPW) * 2
    for i in range(2):
        assert (candidate(folder, i) / "weather.epw.gz").read_bytes() == archives[i]
        assert not (candidate(folder, i) / "weather.epw").exists()


@pytest.mark.parametrize("archive_path_style", ["absolute", "relative"])
def test_equivalent_absolute_and_relative_logical_paths_preserve_archive_receipts(
    tmp_path, monkeypatch, archive_path_style
):
    """A frozen rerender may change path spelling while retaining exact EPW bytes."""
    folder = make_year(tmp_path, candidates=1)
    monkeypatch.chdir(tmp_path)
    catalogue_path = folder / "case-catalogue.json"
    catalogue = json.loads(catalogue_path.read_text())
    if archive_path_style == "relative":
        for record in catalogue["cases"]:
            record["path"] = str(Path(record["path"]).relative_to(tmp_path))
        write_json(catalogue_path, catalogue)
    A.archive_completed_weather(tmp_path)
    case = candidate(folder)
    sidecar = case / "weather-archive.json"
    original_sidecar = sidecar.read_bytes()
    original_archive = (case / "weather.epw.gz").read_bytes()
    original_weather_receipt = (case / "weather-receipt.json").read_bytes()
    # Mimic the original runner regenerating a catalogue and identical plain EPW.
    for record in catalogue["cases"]:
        actual = folder / "cases" / record["case_id"] / "weather.epw"
        record["path"] = (
            str(actual.relative_to(tmp_path))
            if archive_path_style == "absolute"
            else str(actual)
        )
    write_json(catalogue_path, catalogue)
    (case / "weather.epw").write_bytes(EPW)
    result = A.archive_completed_weather(tmp_path)
    assert result["new_archives"] == 0
    assert result["validated_existing_archives"] == 1
    assert result["plain_files_removed"] == 1
    assert sidecar.read_bytes() == original_sidecar
    assert (case / "weather.epw.gz").read_bytes() == original_archive
    assert (case / "weather-receipt.json").read_bytes() == original_weather_receipt
    assert not (case / "weather.epw").exists()


def test_logical_path_with_same_basename_but_different_actual_file_is_rejected(
    tmp_path, monkeypatch
):
    folder = make_year(tmp_path, candidates=1)
    monkeypatch.chdir(tmp_path)
    A.archive_completed_weather(tmp_path)
    case = candidate(folder)
    (case / "weather.epw").write_bytes(EPW)
    sidecar = case / "weather-archive.json"
    saved = json.loads(sidecar.read_text())
    saved["logical_weather_path"] = str(
        folder.relative_to(tmp_path) / "cases" / "different-case" / "weather.epw"
    )
    write_json(sidecar, saved)
    original_sidecar = sidecar.read_bytes()
    original_archive = (case / "weather.epw.gz").read_bytes()
    with pytest.raises(ValueError, match="archive receipt differs"):
        A.archive_completed_weather(tmp_path)
    assert (case / "weather.epw").read_bytes() == EPW
    assert sidecar.read_bytes() == original_sidecar
    assert (case / "weather.epw.gz").read_bytes() == original_archive


@pytest.mark.parametrize(
    "changed",
    [
        "archive",
        "gzip_sha256",
        "epw_sha256",
        "uncompressed_bytes",
        "logical_weather_path",
        "weather_receipt_sha256",
        "weather_receipt",
        "original",
        "missing_sidecar",
        "missing_archive",
    ],
)
def test_altered_archive_receipt_or_rerender_is_rejected_before_any_epw_deletion(
    tmp_path, changed
):
    folder = make_year(tmp_path)
    A.archive_completed_weather(tmp_path)
    for i in range(2):
        (candidate(folder, i) / "weather.epw").write_bytes(EPW)
    case = candidate(folder, 1)
    archive, sidecar = case / "weather.epw.gz", case / "weather-archive.json"
    saved = json.loads(sidecar.read_text())
    if changed == "archive":
        archive.write_bytes(archive.read_bytes() + b"changed")
    elif changed == "original":
        (case / "weather.epw").write_bytes(EPW + b"changed")
    elif changed == "weather_receipt":
        write_json(case / "weather-receipt.json", {"epw_sha256": "0" * 64})
    elif changed == "missing_archive":
        archive.unlink()
    elif changed == "missing_sidecar":
        sidecar.unlink()
    else:
        saved[changed] = 999 if changed == "uncompressed_bytes" else "changed"
        write_json(sidecar, saved)
    original = (case / "weather.epw").read_bytes()
    with pytest.raises(ValueError):
        A.archive_completed_weather(tmp_path)
    assert (case / "weather.epw").read_bytes() == original
    assert (candidate(folder) / "weather.epw").read_bytes() == EPW


def test_matching_compressed_hash_cannot_hide_wrong_decoded_weather(tmp_path):
    folder = make_year(tmp_path, candidates=1)
    A.archive_completed_weather(tmp_path)
    case = candidate(folder)
    (case / "weather.epw").write_bytes(EPW)
    archive = case / "weather.epw.gz"
    archive.write_bytes(gzip.compress(EPW.replace(b"29.1", b"39.1"), mtime=0))
    sidecar = case / "weather-archive.json"
    saved = json.loads(sidecar.read_text())
    saved.update(
        gzip_sha256=digest(archive.read_bytes()),
        compressed_bytes=archive.stat().st_size,
    )
    write_json(sidecar, saved)
    with pytest.raises(ValueError, match="Decoded EPW"):
        A.archive_completed_weather(tmp_path)
    assert (case / "weather.epw").read_bytes() == EPW


@pytest.mark.parametrize(
    "stage", ["before_archive", "final_receipt", "pending_removal", "plain_removal"]
)
def test_interrupted_publication_recovers_only_exact_bytes(
    tmp_path, monkeypatch, stage
):
    folder = make_year(tmp_path, candidates=1)
    case = candidate(folder)
    pending, sidecar = (
        case / "weather-archive.json.pending.json",
        case / "weather-archive.json",
    )
    replace, unlink, dump = Path.replace, Path.unlink, A._dump
    if stage == "before_archive":

        def interrupt_replace(path, destination):
            if Path(destination) == case / "weather.epw.gz":
                raise OSError("simulated interruption")
            return replace(path, destination)

        monkeypatch.setattr(Path, "replace", interrupt_replace)
    elif stage == "final_receipt":

        def interrupt_dump(path, value):
            if path == sidecar:
                raise OSError("simulated interruption")
            return dump(path, value)

        monkeypatch.setattr(A, "_dump", interrupt_dump)
    else:

        def interrupt_unlink(path, *args, **kwargs):
            if path == (
                pending if stage == "pending_removal" else case / "weather.epw"
            ):
                raise OSError("simulated interruption")
            return unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", interrupt_unlink)
    with pytest.raises(OSError, match="simulated interruption"):
        A.archive_completed_weather(tmp_path)
    assert (case / "weather.epw").read_bytes() == EPW
    assert (folder / "cases/reference/weather.epw").read_bytes() == EPW
    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(A, "_dump", dump)
    A.archive_completed_weather(tmp_path)
    assert gzip.decompress((case / "weather.epw.gz").read_bytes()) == EPW
    assert not (case / "weather.epw").exists()
    assert not pending.exists()
    assert not list(folder.rglob("*.tmp"))


def test_interrupted_archive_tampering_cannot_be_recovered(tmp_path, monkeypatch):
    folder = make_year(tmp_path, candidates=1)
    case = candidate(folder)
    dump = A._dump

    def interrupt(path, value):
        if path == case / "weather-archive.json":
            raise OSError("simulated interruption")
        return dump(path, value)

    monkeypatch.setattr(A, "_dump", interrupt)
    with pytest.raises(OSError):
        A.archive_completed_weather(tmp_path)
    monkeypatch.setattr(A, "_dump", dump)
    archive = case / "weather.epw.gz"
    archive.write_bytes(archive.read_bytes() + b"tampered")
    before = snapshot(folder)
    with pytest.raises(ValueError, match="gzip archive"):
        A.archive_completed_weather(tmp_path)
    assert snapshot(folder) == before


@pytest.mark.parametrize(
    "changed",
    [
        "summary",
        "rows",
        "missing_building",
        "duplicate_pair",
        "audit_count",
        "baseline",
        "missing_weather_receipt",
        "summary_pending",
    ],
)
def test_complete_flag_requires_checked_full_matrix_and_plain_reference(
    tmp_path, changed
):
    folder = make_year(tmp_path)
    summary = folder / "summary.csv"
    if changed == "summary":
        summary.write_bytes(summary.read_bytes() + b"tamper")
    elif changed == "rows":
        receipt = json.loads((folder / "summary.csv.receipt.json").read_text())
        receipt["rows"] += 1
        write_json(folder / "summary.csv.receipt.json", receipt)
    elif changed in {"missing_building", "duplicate_pair"}:
        with summary.open(newline="") as stream:
            rows = list(csv.reader(stream))
        rows = rows[:-1] if changed == "missing_building" else [*rows, rows[-1]]
        with summary.open("w", newline="") as stream:
            csv.writer(stream).writerows(rows)
        write_json(
            folder / "summary.csv.receipt.json",
            {"sha256": digest(summary.read_bytes()), "rows": len(rows) - 1},
        )
        write_json(
            folder / "audit.json",
            {"full_matrix_site_year_complete": True, "comparisons": len(rows) - 1},
        )
    elif changed == "audit_count":
        write_json(
            folder / "audit.json",
            {"full_matrix_site_year_complete": True, "comparisons": 999},
        )
    elif changed == "baseline":
        (folder / "cases/reference/weather.epw").unlink()
    elif changed == "missing_weather_receipt":
        (candidate(folder, 1) / "weather-receipt.json").unlink()
    else:
        (folder / "summary.csv.receipt.json.pending.json").write_text("not recovered")
    before = snapshot(folder)
    with pytest.raises(ValueError):
        A.archive_completed_weather(tmp_path)
    assert snapshot(folder) == before
    assert not (tmp_path / "execution-environment").exists()


def test_missing_audit_skips_even_an_inconsistent_active_year(tmp_path):
    folder = make_year(tmp_path)
    (folder / "audit.json").unlink()
    (folder / "summary.csv").write_text("partially written")
    before = snapshot(folder)
    result = A.archive_completed_weather(tmp_path)
    assert result["skipped_site_years"] == ["south/2018"]
    assert result["candidate_weather_files"] == 0
    assert snapshot(folder) == before


def test_symlinked_case_cannot_delete_outside_completed_case_directories(tmp_path):
    root = tmp_path / "campaign"
    folder = make_year(root, candidates=1)
    case = candidate(folder)
    outside = tmp_path / "outside"
    case.rename(outside)
    case.symlink_to(outside, target_is_directory=True)
    before = snapshot(outside)
    with pytest.raises(ValueError, match="symlink"):
        A.archive_completed_weather(root)
    assert snapshot(outside) == before
    assert (folder / "cases/reference/weather.epw").read_bytes() == EPW


def test_late_receipt_change_preserves_current_plain_weather(tmp_path, monkeypatch):
    folder = make_year(tmp_path, candidates=1)
    case = candidate(folder)
    dump = A._dump

    def mutate_after_publication(path, value):
        dump(path, value)
        if path == case / "weather-archive.json":
            write_json(
                case / "weather-receipt.json",
                {"epw_sha256": digest(EPW), "changed": True},
            )

    monkeypatch.setattr(A, "_dump", mutate_after_publication)
    with pytest.raises(ValueError, match="receipt changed"):
        A.archive_completed_weather(tmp_path)
    assert (case / "weather.epw").read_bytes() == EPW


def test_late_symlink_cannot_redirect_plain_deletion_outside_campaign(
    tmp_path, monkeypatch
):
    root = tmp_path / "campaign"
    folder = make_year(root, candidates=1)
    case = candidate(folder)
    outside = tmp_path / "outside"
    dump = A._dump

    def redirect_after_publication(path, value):
        dump(path, value)
        if path == case / "weather-archive.json":
            case.rename(outside)
            case.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(A, "_dump", redirect_after_publication)
    with pytest.raises(ValueError, match="symlink"):
        A.archive_completed_weather(root)
    assert (outside / "weather.epw").read_bytes() == EPW
    assert (folder / "cases/reference/weather.epw").read_bytes() == EPW
