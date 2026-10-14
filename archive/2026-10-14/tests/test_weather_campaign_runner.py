"""Protect resumed campaign provenance before compiling or consuming results."""

import gzip
import hashlib
import importlib
import json
import lzma
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from examples.weather_imputation_sensitivity import run_campaign as R


@pytest.fixture
def pinned_runtime(monkeypatch, tmp_path):
    """Create an existing pin without copying Core or running shell commands."""
    live = tmp_path / "live-driver"
    root = tmp_path / "campaign"
    runtime = root / "runtime"
    driver = runtime / "driver"
    packages = runtime / "packages"
    for folder in (live, driver, packages / "fixture_package"):
        folder.mkdir(parents=True)
    for folder in (live, driver):
        (folder / "run_campaign.py").write_text("VALUE = 1\n")
    (packages / "fixture_package/model.py").write_text("MODEL_VERSION = 1\n")
    monkeypatch.setattr(R, "__file__", str(live / "run_campaign.py"))
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(
        R.shutil,
        "copytree",
        Mock(side_effect=AssertionError("Existing pins must not copy packages")),
    )
    monkeypatch.setattr(
        R.subprocess,
        "check_output",
        Mock(side_effect=AssertionError("Existing pins must not inspect live Git")),
    )
    receipt = {
        "driver_files": R.tree_hashes(live, code_only=True),
        "package_files": R.tree_hashes(packages),
        "interpreter": {
            "python": R.sys.version,
            "numpy": R.np.__version__,
            "pandas": R.pd.__version__,
        },
    }
    R.dump(runtime / "manifest.json", receipt)
    return root, live, runtime, receipt


def test_unchanged_runtime_resumes_without_copying_packages(pinned_runtime):
    root, _, runtime, receipt = pinned_runtime
    assert R.pin_runtime(root) == receipt
    assert sys.path[0] == str((runtime / "packages").resolve())
    R.shutil.copytree.assert_not_called()
    R.subprocess.check_output.assert_not_called()


@pytest.mark.parametrize("changed", ["live_driver", "copied_driver", "package"])
def test_runtime_rejects_changed_source_before_import_path_changes(
    pinned_runtime, changed
):
    root, live, runtime, _ = pinned_runtime
    paths = {
        "live_driver": live / "run_campaign.py",
        "copied_driver": runtime / "driver/run_campaign.py",
        "package": runtime / "packages/fixture_package/model.py",
    }
    paths[changed].write_text("VALUE = 'modified after pinning'\n")
    before = list(sys.path)
    with pytest.raises(ValueError, match="Pinned runtime changed"):
        R.pin_runtime(root)
    assert sys.path == before
    R.shutil.copytree.assert_not_called()
    R.subprocess.check_output.assert_not_called()


@pytest.mark.parametrize("dependency", ["python", "numpy", "pandas"])
def test_runtime_rejects_interpreter_or_dependency_mismatch(pinned_runtime, dependency):
    root, _, runtime, receipt = pinned_runtime
    receipt["interpreter"][dependency] += "-different-version"
    R.dump(runtime / "manifest.json", receipt)
    before = list(sys.path)
    with pytest.raises(ValueError, match="Pinned runtime changed"):
        R.pin_runtime(root)
    assert sys.path == before


@pytest.fixture
def simulation_dependencies(monkeypatch):
    """Stub lazy Core-facing imports while retaining the real archive checks."""
    monkeypatch.setattr(
        R.shutil, "disk_usage", lambda _: SimpleNamespace(free=10 * 1024**3)
    )
    package = importlib.import_module(R.__package__)
    modules = {}
    for name, functions in {
        "archetypes": ("make_archetype_spec", "describe_archetype"),
        "building": ("simulate",),
        "metrics": ("extract",),
        "artifact_storage": ("compact_artifacts",),
    }.items():
        module = ModuleType(f"{R.__package__}.{name}")
        for function in functions:
            setattr(module, function, Mock(name=f"{name}.{function}"))
        monkeypatch.setitem(sys.modules, module.__name__, module)
        monkeypatch.setattr(package, name, module, raising=False)
        modules[name] = module
    return modules


def test_low_disk_space_stops_before_archive_or_core(
    tmp_path, simulation_dependencies, monkeypatch
):
    """Storage rejection is independent of the machine running unit fixtures."""
    monkeypatch.setattr(R.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    with pytest.raises(RuntimeError, match="less than 2 GiB free"):
        R.simulate_case(
            tmp_path / "weather.epw",
            tmp_path,
            SimpleNamespace(year=2023, utc_offset=1),
            "house",
            18,
            Path("unavailable-energyplus"),
        )
    for module in simulation_dependencies.values():
        for value in vars(module).values():
            if isinstance(value, Mock):
                value.assert_not_called()


@pytest.mark.parametrize(
    "changed",
    ["gzip_bytes", "gzip_bytes_with_sql", "uncompressed_receipt", "existing_sql"],
)
def test_sql_archive_integrity_precedes_spec_and_simulation(
    tmp_path, simulation_dependencies, changed
):
    sql_bytes = b"Previously validated EnergyPlus SQL bytes"
    archive = tmp_path / "eplusout.sql.gz"
    archive.write_bytes(gzip.compress(sql_bytes))
    receipt = {
        "gzip_sha256": R.fingerprint(archive),
        "uncompressed_sha256": hashlib.sha256(sql_bytes).hexdigest(),
    }
    if changed in {"gzip_bytes", "gzip_bytes_with_sql"}:
        if changed == "gzip_bytes_with_sql":
            (tmp_path / "eplusout.sql").write_bytes(sql_bytes)
        archive.write_bytes(archive.read_bytes() + b"changed archive bytes")
        error = "Archived EnergyPlus SQL changed"
    elif changed == "uncompressed_receipt":
        receipt["uncompressed_sha256"] = "0" * 64
        error = "Restored EnergyPlus SQL differs from its receipt"
    else:
        (tmp_path / "eplusout.sql").write_bytes(
            b"Partial or altered SQL left by a prior restore"
        )
        error = "SQL"
    R.dump(tmp_path / "sql-archive.json", receipt)
    context = SimpleNamespace(year=2023, utc_offset=1)
    with pytest.raises(ValueError, match=error):
        R.simulate_case(
            tmp_path / "weather.epw",
            tmp_path,
            context,
            "house",
            8.0,
            Path("unavailable-energyplus"),
        )
    for module in simulation_dependencies.values():
        for value in vars(module).values():
            if isinstance(value, Mock):
                value.assert_not_called()
    if changed in {"gzip_bytes", "uncompressed_receipt"}:
        assert not (tmp_path / "eplusout.sql").exists()
    assert not (tmp_path / "eplusout.sql.restore.tmp").exists()


def test_catalogue_resume_preserves_other_methods_and_replaces_changed_status(tmp_path):
    """A later batch retains earlier cases and moves changed statuses only once."""
    path = tmp_path / "case-catalogue.json"
    original = {"case_id": "temporal", "method": "temporal"}
    rejected = {"case_id": "donor", "method": "donor", "status": "unsupported"}
    R.merge_catalogue(path, [original], [rejected], ground=8.0, site_id="test")
    transferred = {"case_id": "donor", "method": "donor", "status": "available"}
    reanalysis = {"case_id": "era5", "method": "era5"}
    R.merge_catalogue(path, [transferred, reanalysis], [], ground=8.0, site_id="test")
    catalogue = json.loads(path.read_text())
    assert {r["case_id"] for r in catalogue["cases"]} == {
        "temporal",
        "donor",
        "era5",
    }
    assert original in catalogue["cases"]
    assert catalogue["unavailable"] == []
    R.merge_catalogue(path, [], [rejected], ground=8.0, site_id="test")
    catalogue = json.loads(path.read_text())
    assert {r["case_id"] for r in catalogue["cases"]} == {"temporal", "era5"}
    assert catalogue["unavailable"] == [rejected]


def test_catalogue_rejects_changed_fixed_metadata_without_overwriting(tmp_path):
    """Resumed weather cases cannot silently switch their fixed exposure settings."""
    path = tmp_path / "case-catalogue.json"
    R.merge_catalogue(path, [{"case_id": "original"}], [], ground=8.0)
    original = path.read_bytes()
    with pytest.raises(ValueError, match="metadata changed: ground"):
        R.merge_catalogue(path, [{"case_id": "later"}], [], ground=9.0)
    assert path.read_bytes() == original


def test_panel_plan_resume_unions_selections_but_rejects_changed_evidence(tmp_path):
    """Previously selected masks survive later batches with the same evidence."""
    path = tmp_path / "panel-plan.json"
    records = [{"evidence_id": "one", "positions": [1, 2]}]
    R.merge_panel_plan(path, records, ["one"])
    R.merge_panel_plan(path, records, ["two"])
    plan = json.loads(path.read_text())
    assert plan["all_panels"] == records
    assert plan["selected_evidence_ids"] == ["one", "two"]
    original = path.read_bytes()
    with pytest.raises(ValueError, match="evidence changed"):
        R.merge_panel_plan(path, [{"evidence_id": "one", "positions": [2, 3]}], [])
    assert path.read_bytes() == original


@pytest.mark.parametrize("changed", ["bytes", "rows", "missing_receipt"])
def test_comparison_checkpoint_rejects_tampering(tmp_path, changed):
    """Comparison resume verifies both table bytes and the recorded row count."""
    path = tmp_path / "summary.csv"
    table = R.pd.DataFrame([{"case_id": "one", "heating_kwh": 100.0}])
    R.save_table(path, table)
    R.pd.testing.assert_frame_equal(R.load_table(path), table)
    receipt_path = Path(str(path) + ".receipt.json")
    if changed == "bytes":
        path.write_text(path.read_text().replace("100.0", "101.0"))
    elif changed == "rows":
        receipt = json.loads(receipt_path.read_text())
        receipt["rows"] = 99
        R.dump(receipt_path, receipt)
    else:
        receipt_path.unlink()
    with pytest.raises(ValueError, match="checkpoint"):
        R.load_table(path)


def test_empty_comparison_checkpoint_has_no_invented_rows(tmp_path):
    """A schema-less completed empty checkpoint remains empty when resumed."""
    path = tmp_path / "empty.csv"
    R.save_table(path, R.pd.DataFrame())
    assert R.load_table(path).empty


@pytest.mark.parametrize("corrupt_after_interruption", [False, True])
def test_checkpoint_publication_resumes_only_recorded_interrupted_bytes(
    tmp_path, monkeypatch, corrupt_after_interruption
):
    """An interrupted receipt write permits exact prepared bytes, never later edits."""
    path = tmp_path / "summary.csv"
    receipt_path = Path(str(path) + ".receipt.json")
    R.save_table(path, R.pd.DataFrame([{"case_id": "first"}]))
    committed_receipt = receipt_path.read_bytes()
    later = R.pd.DataFrame([{"case_id": "first"}, {"case_id": "second"}])
    dump = R.dump

    def interrupt_completed_receipt(target, value):
        """Interrupt only the final sidecar after the prepared data becomes visible."""
        if Path(target) == receipt_path:
            raise OSError("Interrupted completed receipt publication")
        return dump(target, value)

    monkeypatch.setattr(R, "dump", interrupt_completed_receipt)
    with pytest.raises(OSError, match="Interrupted"):
        R.save_table(path, later)
    assert receipt_path.read_bytes() == committed_receipt
    monkeypatch.setattr(R, "dump", dump)
    if corrupt_after_interruption:
        path.write_text(path.read_text().replace("second", "altered"))
        with pytest.raises(ValueError, match="changed|publication"):
            R.load_table(path)
        assert receipt_path.read_bytes() == committed_receipt
    else:
        R.pd.testing.assert_frame_equal(R.load_table(path), later)
        receipt = json.loads(receipt_path.read_text())
        assert receipt["sha256"] == R.fingerprint(path)
        assert receipt["rows"] == len(later)


def test_sql_archive_publication_resumes_after_final_receipt_interruption(
    tmp_path, monkeypatch, simulation_dependencies
):
    """A retained validated SQL file remains reusable after interrupted compression."""
    sql_bytes = b"Validated SQL before interrupted archive publication"
    sql = tmp_path / "eplusout.sql"
    sql.write_bytes(sql_bytes)
    receipt_path = tmp_path / "sql-archive.json"
    dump = R.dump

    def interrupt_completed_receipt(target, value):
        """Leave the archive transaction prepared while stopping final publication."""
        if Path(target) == receipt_path:
            raise OSError("Interrupted completed archive receipt publication")
        return dump(target, value)

    monkeypatch.setattr(R, "dump", interrupt_completed_receipt)
    with pytest.raises(OSError, match="Interrupted"):
        R.compact_simulation(tmp_path)
    assert sql.read_bytes() == sql_bytes
    assert not receipt_path.exists()
    monkeypatch.setattr(R, "dump", dump)
    simulation_dependencies["archetypes"].make_archetype_spec.return_value = object()
    simulation_dependencies["archetypes"].describe_archetype.return_value = {}

    def reuse_sql(*args, **kwargs):
        """Assert exact restored bytes before allowing the mocked Core reuse."""
        assert sql.read_bytes() == sql_bytes
        return {"reused": True}

    simulation_dependencies["building"].simulate.side_effect = reuse_sql
    simulation_dependencies["metrics"].extract.return_value = (
        R.pd.DataFrame(),
        {"annual_heating_kwh": 0.0},
    )
    _, _, result, _ = R.simulate_case(
        tmp_path / "weather.epw",
        tmp_path,
        SimpleNamespace(year=2023, utc_offset=1),
        "house",
        8.0,
        Path("unavailable-energyplus"),
    )
    assert result["reused"]
    archive = tmp_path / "eplusout.sql.xz"
    receipt = json.loads(receipt_path.read_text())
    assert receipt["xz_sha256"] == R.fingerprint(archive)
    assert receipt["uncompressed_sha256"] == hashlib.sha256(sql_bytes).hexdigest()
    assert lzma.decompress(archive.read_bytes()) == sql_bytes
    assert not sql.exists()


def test_all_source_unavailable_report_retains_zero_coverage_and_reasons(tmp_path):
    R.dump(
        tmp_path / "datasets/unsupported/2023/source-unavailable.json",
        {
            "site_id": "unsupported",
            "year": 2023,
            "status": "unavailable",
            "reason": "Original observations have a 72h gap",
        },
    )
    R.report(tmp_path)
    coverage = json.loads((tmp_path / "coverage.json").read_text())
    assert coverage["location_years_with_simulations"] == 0
    assert coverage["reference_building_location_years"] == 0
    assert not coverage["complete_full_matrix"]
    assert len(coverage["source_unavailable"]) == 1
    assert "72h" in coverage["source_unavailable"][0]["reason"]
    assert (tmp_path / "report.html").is_file()


def test_reference_only_report_does_not_invent_error_quantiles(tmp_path):
    row = {
        "site_id": "test",
        "year": 2023,
        "building_id": "house",
        "case_id": "reference",
        "evidence_id": "reference",
        "scope": "reference",
        "sampling": "reference",
        "gap_hours": 0,
        "method": "reference",
    }
    folder = tmp_path / "datasets/test/2023"
    folder.mkdir(parents=True)
    R.save_table(folder / "summary.csv", R.pd.DataFrame([row]))
    R.dump(
        tmp_path / "datasets/test/2023/audit.json",
        {"full_matrix_site_year_complete": False},
    )
    R.report(tmp_path)
    coverage = json.loads((tmp_path / "coverage.json").read_text())
    assert coverage["location_years_with_simulations"] == 1
    assert coverage["reference_building_location_years"] == 1
    assert not coverage["complete_full_matrix"]
    assert (tmp_path / "report.html").is_file()
    # Empty alternatives must not become a distribution of zero reference errors.
    errors = R.pd.read_csv(tmp_path / "error-distributions.csv")
    assert errors.empty


def test_source_only_report_counts_readiness_and_weather_availability(tmp_path):
    """Prepared sources and unsupported methods are visible before building runs."""
    folder = tmp_path / "datasets/test/2023"
    R.dump(folder / "sources/receipt.json", {"status": "ready", "year": 2023})
    R.merge_catalogue(
        folder / "case-catalogue.json",
        [{"case_id": "temporal-one", "method": "temporal"}],
        [{"case_id": "donor-one", "method": "donor", "status": "unsupported"}],
    )
    R.report(tmp_path)
    coverage = json.loads((tmp_path / "coverage.json").read_text())
    assert coverage["reference_weather_ready_location_years"] == 1
    assert coverage["location_years_with_simulations"] == 0
    assert coverage["reference_building_location_years"] == 0
    assert coverage["available_weather_comparisons"] == 1
    assert coverage["unsupported_weather_comparisons"] == 1
    assert coverage["evaluated_weather_comparisons"] == 0
    assert coverage["method_availability_rates"]["donor"]["available_fraction"] == 0
    assert (tmp_path / "report.html").is_file()


def test_compacted_sql_is_lossless_and_removes_only_auxiliary_outputs(tmp_path):
    import lzma

    payload = bytes(range(256)) * 1024
    (tmp_path / "eplusout.sql").write_bytes(payload)
    (tmp_path / "eplusout.eso").write_text("redundant trace")
    (tmp_path / "diagnostics.json").write_text('{"accepted":true}')
    R.compact_simulation(tmp_path)
    archive = tmp_path / "eplusout.sql.xz"
    receipt = json.loads((tmp_path / "sql-archive.json").read_text())
    assert lzma.decompress(archive.read_bytes()) == payload
    assert receipt["uncompressed_sha256"] == hashlib.sha256(payload).hexdigest()
    assert receipt["xz_sha256"] == R.fingerprint(archive)
    assert not (tmp_path / "eplusout.sql").exists()
    assert not (tmp_path / "eplusout.eso").exists()
    assert (tmp_path / "diagnostics.json").exists()


def test_xz_archive_tampering_is_rejected_before_core(
    tmp_path, simulation_dependencies
):
    import lzma

    archive = tmp_path / "eplusout.sql.xz"
    archive.write_bytes(lzma.compress(b"validated SQL"))
    R.dump(
        tmp_path / "sql-archive.json",
        {
            "xz_sha256": R.fingerprint(archive),
            "uncompressed_sha256": hashlib.sha256(b"validated SQL").hexdigest(),
        },
    )
    archive.write_bytes(archive.read_bytes() + b"altered")
    with pytest.raises(ValueError, match="Archived EnergyPlus SQL changed"):
        R.simulate_case(
            tmp_path / "weather.epw",
            tmp_path,
            SimpleNamespace(year=2023, utc_offset=1),
            "house",
            18,
            Path("unavailable-energyplus"),
        )
    for module in simulation_dependencies.values():
        for value in vars(module).values():
            if isinstance(value, Mock):
                value.assert_not_called()
