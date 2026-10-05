"""Exercise the outside-frozen scheduler without starting scientific engines."""

import csv
import hashlib
import json
import signal
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from examples import archive_weather_campaign_files as A
from examples import resume_weather_imputation_campaign as S
from examples import weather_imputation_sensitivity as package


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def digest(value):
    return hashlib.sha256(value).hexdigest()


def campaign(root, sites=("south", "north"), years=(2018, 2021)):
    root.mkdir(parents=True, exist_ok=True)
    matrix = {
        "sites": [{"site_id": s} for s in sites],
        "years": list(years),
        "buildings": {f"building-{i}": {} for i in range(6)},
    }
    write_json(root / "matrix.json", matrix)
    write_json(root / "runtime/manifest.json", {"frozen": True})
    for site in sites:
        for year in years:
            write_json(
                root / "datasets" / site / str(year) / "sources/receipt.json",
                {"status": "ready"},
            )
    return matrix


def complete_year(root, site="south", year=2018):
    """Supply the real archival preflight's full six-profile comparison boundary."""
    folder = root / "datasets" / site / str(year)
    records = []
    for case_id in ("reference", "outage-era5"):
        case = folder / "cases" / case_id
        case.mkdir(parents=True)
        epw = b"Illustrative exact weather bytes\r\n" * 100
        (case / "weather.epw").write_bytes(epw)
        write_json(case / "weather-receipt.json", {"epw_sha256": digest(epw)})
        records.append(
            {
                "case_id": case_id,
                "method": "reference" if case_id == "reference" else "era5",
                "path": str(case / "weather.epw"),
            }
        )
    write_json(folder / "case-catalogue.json", {"cases": records, "unavailable": []})
    summary = folder / "summary.csv"
    with summary.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["building_id", "case_id"])
        for i in range(6):
            for record in records:
                writer.writerow([f"building-{i}", record["case_id"]])
    write_json(
        folder / "summary.csv.receipt.json",
        {"sha256": digest(summary.read_bytes()), "rows": 12},
    )
    write_json(
        folder / "audit.json",
        {
            "full_matrix_site_year_complete": True,
            "comparisons": 12,
            "diagnostics_accepted": True,
            "warnings": 0,
            "severe": 0,
            "fatal": 0,
        },
    )
    return folder


def complete_warned_year_with_proof(root):
    """Create six profiles with two labels for each heating-scoped warning run.

    These small artifacts exercise the real immutable-evidence qualifier. They
    contain no engine outputs to recreate and require no engine/compiler call.
    """
    folder = complete_year(root)
    cases = ["reference", "outage-era5", "outage-alias"]
    catalogue = []
    for case_id in cases:
        case = folder / "cases" / case_id
        case.mkdir(exist_ok=True)
        dry_bulb = "5.0" if case_id == "reference" else "4.3"
        epw = (
            "LOCATION,Fixture,SE,SWE,Fixture,999999,57.0,12.0,1.0,10.0\n"
            "DESIGN CONDITIONS,0\n"
            "TYPICAL/EXTREME PERIODS,0\n"
            "GROUND TEMPERATURES,0\n"
            "HOLIDAYS/DAYLIGHT SAVINGS,No,0,0,0\n"
            f"COMMENTS 1,{case_id}\n"
            f"COMMENTS 2,{case_id}\n"
            "DATA PERIODS,1,1,Data,Monday,1/1,12/31\n"
            f"2018,1,1,1,60,?9,{dry_bulb},1.0,75,101325,0,0,300,"
            "0,0,0,0,0,0,0,180,2,5,5,9999,77777,0,999999999,10,0.1,0,0,0.2,0,0\n"
        ).encode()
        weather = case / "weather.epw"
        weather.write_bytes(epw)
        write_json(case / "weather-receipt.json", {"epw_sha256": digest(epw)})
        catalogue.append(
            {
                "case_id": case_id,
                "method": "reference" if case_id == "reference" else "era5",
                "path": str(weather),
            }
        )
    rows = []
    for index in range(6):
        building_id = f"building-{index}"
        model = json.dumps({"illustrative_profile": building_id}).encode()
        for case_id in cases[:2]:
            run = folder / "simulations" / building_id / case_id
            run.mkdir(parents=True)
            warning = case_id != "reference"
            weather = folder / "cases" / case_id / "weather.epw"
            diagnostics = {
                "accepted": True,
                "warning_count": int(warning),
                "severe_count": 0,
                "fatal_count": 0,
                "err_path": str(run / "eplusout.err"),
                "disposition": "accepted_with_warnings" if warning else "accepted",
                "messages": (
                    [
                        {
                            "severity": "warning",
                            "approved": True,
                            "exception_rule": {"scope": "annual_space_heating_demand"},
                        }
                    ]
                    if warning
                    else []
                ),
            }
            receipt = {
                "year": 2018,
                "weather_path": str(weather),
                "weather_sha256": digest(weather.read_bytes()),
                "model_sha256": digest(model),
                "core_run_id": f"{building_id}-{case_id}",
                "core_diagnostics_accepted": True,
                "core_result_path": str(run / "result.json"),
                "core_building_spec_path": str(run / "building.json"),
                "warning_count": 2 if warning else 0,
                "severe_errors": 0,
                "fatal_errors": 0,
            }
            write_json(run / "receipt.json", receipt)
            write_json(run / "diagnostics.json", diagnostics)
            write_json(
                run / "result.json",
                {"run_id": receipt["core_run_id"], "diagnostics": diagnostics},
            )
            write_json(
                run / "building.json",
                {
                    "climate": {
                        "epw_path": str(weather),
                        "weather_sha256": receipt["weather_sha256"],
                        "calendar_year": 2018,
                    }
                },
            )
            (run / "model.epJSON").write_bytes(model)
            err = (
                " ** Warning ** Internal wetbulb iteration\n"
                " ** Warning ** Recurring summary of that same warning\n"
                if warning
                else ""
            )
            (run / "eplusout.err").write_text(
                err + "EnergyPlus Completed Successfully\n"
            )
        for case_id in cases:
            canonical_case = "reference" if case_id == "reference" else "outage-era5"
            rows.append(
                {
                    "site_id": "south",
                    "year": 2018,
                    "building_id": building_id,
                    "case_id": case_id,
                    "method": "reference" if case_id == "reference" else "era5",
                    "simulation_path": str(
                        folder / "simulations" / building_id / canonical_case
                    ),
                    "compiled_model_sha256": digest(model),
                }
            )
    write_json(folder / "case-catalogue.json", {"cases": catalogue, "unavailable": []})
    pd.DataFrame(rows).to_csv(folder / "summary.csv", index=False)
    write_json(
        folder / "summary.csv.receipt.json",
        {"sha256": digest((folder / "summary.csv").read_bytes()), "rows": len(rows)},
    )
    write_json(
        folder / "audit.json",
        {
            "full_matrix_site_year_complete": True,
            "comparisons": len(rows),
            "diagnostics_accepted": True,
            "warnings": 12,
            "severe": 0,
            "fatal": 0,
        },
    )
    return folder


def test_shared_matrix_dump_guards_only_exact_resolved_path(tmp_path):
    root = tmp_path / "campaign"
    root.mkdir()
    matrix = {"sites": ["south"], "years": [2018], "path": str(root)}
    write_json(root / "matrix.json", matrix)
    original_bytes = (root / "matrix.json").read_bytes()
    calls = []
    guard = S.frozen_matrix_dump(
        lambda *args: calls.append(args), root, digest(original_bytes)
    )
    # A tuple/Path proposal normalizes to the immutable JSON representation.
    guard(
        root / "nested" / ".." / "matrix.json",
        {"sites": ("south",), "years": (2018,), "path": root},
    )
    assert (root / "matrix.json").read_bytes() == original_bytes
    assert calls == []
    other = root / "unrelated" / "matrix.json"
    guard(other, {"different": True})
    assert calls == [(other, {"different": True})]


@pytest.mark.parametrize("changed", ["proposal", "bytes", "data"])
def test_shared_matrix_dump_rejects_changed_bytes_or_scientific_data(tmp_path, changed):
    path = tmp_path / "matrix.json"
    expected = {"years": [2018]}
    write_json(path, expected)
    expected_sha = S.sha(path)
    proposed = expected
    if changed == "proposal":
        proposed = {"years": [2021]}
    elif changed == "bytes":
        path.write_bytes(path.read_bytes() + b"\n")
    else:
        write_json(path, {"years": [2021]})
        expected_sha = S.sha(path)  # Data comparison is independently required.
    calls = []
    guard = S.frozen_matrix_dump(
        lambda *args: calls.append(args), tmp_path, expected_sha
    )
    preserved = path.read_bytes()
    with pytest.raises(ValueError, match="immutable campaign matrix"):
        guard(path, proposed)
    assert path.read_bytes() == preserved
    assert not calls


def test_jobs_separate_unique_ready_unavailable_and_verified_archived_complete_years(
    tmp_path,
):
    campaign(tmp_path)
    complete_year(tmp_path)
    write_json(
        tmp_path / "datasets/north/2018/sources/receipt.json",
        {"status": "unavailable", "reason": "159-hour wind gap"},
    )
    # Existing completed candidates may be archived; the baseline remains plain.
    A.archive_completed_weather(tmp_path)
    result, unavailable, complete = S.jobs(tmp_path)
    assert result == [("south", 2021), ("north", 2021)]
    assert unavailable == [("north", 2018)]
    assert complete == [("south", 2018)]
    assert len(set(result + unavailable + complete)) == 4
    assert not (tmp_path / "datasets/south/2018/cases/outage-era5/weather.epw").exists()


@pytest.mark.parametrize(
    "changed", ["duplicate_site", "duplicate_year", "unknown_source", "missing_source"]
)
def test_jobs_reject_ambiguous_dispatch_or_unknown_source_state(tmp_path, changed):
    matrix = campaign(tmp_path, sites=("south",), years=(2018,))
    if changed == "duplicate_site":
        matrix["sites"].append({"site_id": "south"})
    elif changed == "duplicate_year":
        matrix["years"].append(2018)
    elif changed == "unknown_source":
        write_json(
            tmp_path / "datasets/south/2018/sources/receipt.json", {"status": "partial"}
        )
    else:
        (tmp_path / "datasets/south/2018/sources/receipt.json").unlink()
    write_json(tmp_path / "matrix.json", matrix)
    with pytest.raises((ValueError, FileNotFoundError)):
        S.jobs(tmp_path)


@pytest.mark.parametrize(
    "changed", ["warnings", "diagnostics", "checkpoint", "missing_profile"]
)
def test_complete_year_is_never_skipped_without_verified_full_evidence(
    tmp_path, changed
):
    campaign(tmp_path, sites=("south",), years=(2018,))
    folder = complete_year(tmp_path)
    audit = json.loads((folder / "audit.json").read_text())
    if changed == "warnings":
        audit["warnings"] = 1
    elif changed == "diagnostics":
        audit["diagnostics_accepted"] = False
    elif changed == "checkpoint":
        (folder / "summary.csv").write_bytes(
            (folder / "summary.csv").read_bytes() + b"changed"
        )
    else:
        summary = folder / "summary.csv"
        lines = summary.read_text().splitlines()
        summary.write_text("\n".join(lines[:-2]) + "\n")
        audit["comparisons"] = 10
        write_json(
            folder / "summary.csv.receipt.json", {"sha256": S.sha(summary), "rows": 10}
        )
    write_json(folder / "audit.json", audit)
    with pytest.raises(ValueError):
        S.jobs(tmp_path)


def test_completed_scoped_warnings_require_real_proof_without_engine_or_mutation(
    tmp_path, monkeypatch
):
    """Canonical warnings reconcile once; aliases remain excluded in postanalysis."""
    from examples import qualify_weather_campaign_results as Q

    campaign(tmp_path, sites=("south",), years=(2018,))
    folder = complete_warned_year_with_proof(tmp_path)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    def engine_must_not_run(*args, **kwargs):
        pytest.fail("Qualification must not launch any process or weather engine")

    monkeypatch.setattr(S.subprocess, "Popen", engine_must_not_run)
    assert S.jobs(tmp_path) == ([], [], [("south", 2018)])
    rows = pd.read_csv(folder / "summary.csv", float_precision="round_trip")
    _, exclusions, metadata = Q.qualify_results(tmp_path, rows)
    assert len(exclusions) == 12  # Six warned runs, each having two weather labels.
    assert metadata["excluded_simulations"] == 6
    assert sum(record["raw_warning_count"] for record in metadata["records"]) == 12
    assert metadata["qualified_building_rows"] == 6
    assert all(path.read_bytes() == data for path, data in before.items())
    assert {path for path in tmp_path.rglob("*") if path.is_file()} == set(before)


@pytest.mark.parametrize("audit_count", [11, 13, 24])
def test_warning_completion_rejects_unreconciled_totals_and_alias_double_counting(
    tmp_path, audit_count
):
    """The audit counts canonical raw markers rather than warning-bearing row labels."""
    campaign(tmp_path, sites=("south",), years=(2018,))
    folder = complete_warned_year_with_proof(tmp_path)
    path = folder / "audit.json"
    audit = json.loads(path.read_text())
    audit["warnings"] = audit_count
    write_json(path, audit)
    with pytest.raises(ValueError, match="warning audit"):
        S.jobs(tmp_path)


@pytest.mark.parametrize("audit_count", [-1, True, "12", 12.0])
def test_warning_completion_rejects_invalid_diagnostic_count_types(
    tmp_path, audit_count
):
    """Negative, boolean, textual and floating warning totals are not audit counts."""
    campaign(tmp_path, sites=("south",), years=(2018,))
    folder = complete_warned_year_with_proof(tmp_path)
    path = folder / "audit.json"
    audit = json.loads(path.read_text())
    audit["warnings"] = audit_count
    write_json(path, audit)
    with pytest.raises(ValueError, match="invalid warning counts"):
        S.jobs(tmp_path)


@pytest.mark.parametrize(
    "artifact",
    [
        "receipt.json",
        "diagnostics.json",
        "result.json",
        "building.json",
        "model.epJSON",
        "eplusout.err",
    ],
)
def test_warning_completion_requires_each_canonical_core_artifact(tmp_path, artifact):
    """A completed audit and accepted flag cannot replace missing diagnostic proof."""
    campaign(tmp_path, sites=("south",), years=(2018,))
    folder = complete_warned_year_with_proof(tmp_path)
    (folder / "simulations/building-0/outage-era5" / artifact).unlink()
    with pytest.raises(ValueError, match="evidence is absent"):
        S.jobs(tmp_path)


@pytest.mark.parametrize(
    "defect", ["receipt_count", "result_identity", "error_log", "weather"]
)
def test_warning_completion_rejects_changed_canonical_proof(tmp_path, defect):
    """Every warning run still authenticates its result, log and consumed weather."""
    campaign(tmp_path, sites=("south",), years=(2018,))
    folder = complete_warned_year_with_proof(tmp_path)
    run = folder / "simulations/building-0/outage-era5"
    if defect in ("receipt_count", "result_identity"):
        path = run / ("receipt.json" if defect == "receipt_count" else "result.json")
        document = json.loads(path.read_text())
        if defect == "receipt_count":
            document["warning_count"] = 0
        else:
            document["run_id"] = "different-core-run"
        write_json(path, document)
    elif defect == "error_log":
        (run / "eplusout.err").write_text("EnergyPlus Completed Successfully\n")
    else:
        weather = folder / "cases/outage-era5/weather.epw"
        weather.write_bytes(weather.read_bytes().replace(b"4.3", b"7.3"))
        # The archival boundary agrees, but the actual Core-consumed hash does not.
        write_json(
            weather.parent / "weather-receipt.json",
            {"epw_sha256": digest(weather.read_bytes())},
        )
    with pytest.raises(ValueError):
        S.jobs(tmp_path)


@pytest.mark.parametrize("defect", ["core_not_accepted", "severe", "fatal"])
def test_warned_year_cannot_hide_rejected_core_evidence_behind_clean_audit(
    tmp_path, defect
):
    """Zero audit errors must not mask consistent contradictory native artifacts."""
    campaign(tmp_path, sites=("south",), years=(2018,))
    folder = complete_warned_year_with_proof(tmp_path)
    run = folder / "simulations/building-0/outage-era5"
    receipt = json.loads((run / "receipt.json").read_text())
    diagnostics = json.loads((run / "diagnostics.json").read_text())
    if defect == "core_not_accepted":
        diagnostics["accepted"] = False
        receipt["core_diagnostics_accepted"] = False
    else:
        diagnostics[defect + "_count"] = 1
        diagnostics["messages"].append({"severity": defect})
        receipt[defect + "_errors"] = 1
        with (run / "eplusout.err").open("a") as stream:
            stream.write(f" ** {defect.title()} ** Contradictory native error\n")
    write_json(run / "receipt.json", receipt)
    write_json(run / "diagnostics.json", diagnostics)
    result = json.loads((run / "result.json").read_text())
    result["diagnostics"] = diagnostics
    write_json(run / "result.json", result)
    with pytest.raises(ValueError, match="rejected diagnostic evidence"):
        S.jobs(tmp_path)


@pytest.mark.parametrize("fail", [False, True])
def test_worker_restores_runner_metadata_hooks_and_argv_with_full_defaults(
    tmp_path, monkeypatch, fail
):
    matrix = campaign(tmp_path, sites=("south",), years=(2018,))
    original_argv = sys.argv
    calls, seen = [], []

    def original_dump(*args):
        calls.append(("dump", args))

    def original_report(*args):
        calls.append(("report", args))

    runner = SimpleNamespace(
        __file__="unchanged-frozen-runner.py",
        dump=original_dump,
        report=original_report,
    )

    def main():
        seen.append(list(sys.argv))
        runner.dump(tmp_path / "matrix.json", matrix)
        runner.dump(
            tmp_path / "datasets/south/2018/audit.json", {"test": "local writer"}
        )
        runner.report(tmp_path)
        if fail:
            raise RuntimeError("worker execution failed")

    runner.main = main
    monkeypatch.setattr(package, "run_campaign", runner, raising=False)
    if fail:
        with pytest.raises(RuntimeError, match="execution failed"):
            S.worker(tmp_path, "south", 2018, S.sha(tmp_path / "matrix.json"))
    else:
        S.worker(tmp_path, "south", 2018, S.sha(tmp_path / "matrix.json"))
    assert seen == [
        [
            "unchanged-frozen-runner.py",
            "--phase",
            "run",
            "--output",
            str(tmp_path),
            "--select-sites",
            "south",
            "--select-years",
            "2018",
            "--workers",
            "1",
        ]
    ]
    assert runner.dump is original_dump and runner.report is original_report
    assert sys.argv is original_argv
    assert calls == [
        (
            "dump",
            (tmp_path / "datasets/south/2018/audit.json", {"test": "local writer"}),
        )
    ]


def isolated_scheduler(
    tmp_path,
    monkeypatch,
    *,
    selected,
    failure=None,
    startup_failure=False,
    mutate_matrix=False,
):
    """Mock only external processes and scientific runner, retaining real scheduling."""
    root = tmp_path / "campaign"
    campaign(root, sites=("south",), years=(2018,))
    lock = threading.Lock()
    state = {
        "running": 0,
        "maximum_running": 0,
        "processes": [],
        "finished": [],
        "reports": [],
        "pin_calls": [],
        "killed_groups": [],
    }
    gate, failure_collected = threading.Event(), threading.Event()
    target = min(3, len(selected))
    original_dump = S.dump

    def record_dump(path, value):
        original_dump(path, value)
        if any(x.get("returncode") == 1 for x in value.get("results", [])):
            failure_collected.set()

    def report(path):
        assert state["running"] == 0
        assert len(state["finished"]) == len(selected)
        state["reports"].append(path)
        write_json(path / "coverage.json", {"complete_full_matrix": True})

    runner = SimpleNamespace(
        pin_runtime=lambda path: state["pin_calls"].append(path), report=report
    )
    monkeypatch.setattr(package, "run_campaign", runner, raising=False)
    monkeypatch.setattr(S, "dump", record_dump)
    monkeypatch.setattr(
        S,
        "jobs",
        lambda path: (
            (selected, [], []) if not state["finished"] else ([], [], selected)
        ),
    )
    monkeypatch.setattr(
        S.os, "killpg", lambda pid, sig: state["killed_groups"].append((pid, sig))
    )
    # Restore the test's cwd at teardown: resume intentionally changes to repository.
    monkeypatch.chdir(tmp_path)

    class Process:
        def __init__(self, command, **kwargs):
            if startup_failure:
                raise OSError("simulated process start failure")
            self.site = command[command.index("--worker") + 1]
            self.year = int(command[command.index("--worker") + 2])
            self.returncode = None
            self.command, self.kwargs = command, kwargs
            with lock:
                self.pid = 10000 + len(state["processes"])
                state["processes"].append(self)
                state["running"] += 1
                state["maximum_running"] = max(
                    state["maximum_running"], state["running"]
                )
                if len(state["processes"]) >= target:
                    gate.set()
            kwargs["stdout"].write(b"fake isolated process output\n")

        def poll(self):
            return self.returncode

        def wait(self):
            assert gate.wait(timeout=3)
            if failure and self.site != failure:
                assert failure_collected.wait(timeout=3)
            time.sleep(0.01)
            if mutate_matrix:
                with lock:
                    if not state["finished"]:
                        matrix = root / "matrix.json"
                        matrix.write_bytes(matrix.read_bytes() + b"\n")
            self.returncode = int(self.site == failure)
            with lock:
                state["finished"].append((self.site, self.year))
                state["running"] -= 1
            return self.returncode

    monkeypatch.setattr(S.subprocess, "Popen", Process)
    return root, state


def test_resume_never_exceeds_three_processes_and_reports_only_after_all_join(
    tmp_path, monkeypatch
):
    selected = [(f"site-{i}", 2018) for i in range(7)]
    root, state = isolated_scheduler(tmp_path, monkeypatch, selected=selected)
    receipt = S.resume(root, processes=3)
    assert state["maximum_running"] == 3
    assert state["running"] == 0
    assert sorted(state["finished"]) == sorted(selected)
    assert state["reports"] == [root]
    assert state["pin_calls"] == [root]
    assert receipt["status"] == "complete"
    assert len(receipt["results"]) == 7
    assert receipt["coverage_sha256"] == S.sha(root / "coverage.json")
    assert receipt["scheduler_sha256"] == S.sha(Path(S.__file__))
    assert not state["killed_groups"]
    for process in state["processes"]:
        assert process.command[:4] == [
            sys.executable,
            "-u",
            "-m",
            "examples.resume_weather_imputation_campaign",
        ]
        assert process.kwargs["start_new_session"] is True
        assert process.kwargs["cwd"] == S.REPOSITORY
        assert process.kwargs["env"]["TZ"] == "UTC0"
        assert process.kwargs["env"]["PYTHONDONTWRITEBYTECODE"] == "1"


def test_failed_process_stops_new_dispatch_reaps_private_group_and_never_reports(
    tmp_path, monkeypatch
):
    selected = [
        (s, 2018) for s in ["bad", "good-1", "good-2", "never-start-3", "never-start-4"]
    ]
    root, state = isolated_scheduler(
        tmp_path, monkeypatch, selected=selected, failure="bad"
    )
    with pytest.raises(RuntimeError, match="failed or was stopped"):
        S.resume(root)
    assert len(state["processes"]) == 3
    assert state["running"] == 0
    assert state["reports"] == []
    failed_process = next(p for p in state["processes"] if p.site == "bad")
    assert state["killed_groups"] == [(failed_process.pid, signal.SIGKILL)]
    saved = json.loads(
        next((root / "execution-environment").glob("process-resume-*.json")).read_text()
    )
    assert saved["status"] == "interrupted"
    assert len(saved["results"]) == 3
    assert sum(x["returncode"] == 1 for x in saved["results"]) == 1


def test_process_start_exception_is_propagated_without_aggregate_report(
    tmp_path, monkeypatch
):
    root, state = isolated_scheduler(
        tmp_path, monkeypatch, selected=[("bad", 2018)], startup_failure=True
    )
    with pytest.raises(OSError, match="process start failure"):
        S.resume(root)
    assert not state["reports"]
    saved = json.loads(
        next((root / "execution-environment").glob("process-resume-*.json")).read_text()
    )
    assert saved["status"] == "failed"


def test_parent_detects_late_matrix_mutation_after_join_and_before_reporting(
    tmp_path, monkeypatch
):
    root, state = isolated_scheduler(
        tmp_path, monkeypatch, selected=[("one", 2018)], mutate_matrix=True
    )
    with pytest.raises(ValueError, match="matrix bytes changed"):
        S.resume(root)
    assert state["running"] == 0 and len(state["finished"]) == 1
    assert not state["reports"]


def test_successful_process_exit_still_requires_complete_site_year_evidence(
    tmp_path, monkeypatch
):
    selected = [("one", 2018)]
    root, state = isolated_scheduler(tmp_path, monkeypatch, selected=selected)
    monkeypatch.setattr(S, "jobs", lambda path: (selected, [], []))
    with pytest.raises(ValueError, match="incomplete site/year evidence"):
        S.resume(root)
    assert state["running"] == 0 and state["finished"] == selected
    assert not state["reports"]


def test_repository_relative_output_argument_preserves_existing_logical_paths(
    tmp_path, monkeypatch
):
    root, state = isolated_scheduler(tmp_path, monkeypatch, selected=[("one", 2018)])
    monkeypatch.setattr(S, "REPOSITORY", tmp_path)
    receipt = S.resume(root)
    assert receipt["output_argument"] == "campaign"
    command = state["processes"][0].command
    assert command[command.index("--output") + 1] == "campaign"
    assert state["processes"][0].kwargs["cwd"] == tmp_path


def test_verified_already_complete_campaign_reports_once_without_processes(
    tmp_path, monkeypatch
):
    root, state = isolated_scheduler(tmp_path, monkeypatch, selected=[])
    receipt = S.resume(root)
    assert receipt["status"] == "complete"
    assert not state["processes"]
    assert state["reports"] == [root]


def test_existing_scheduler_lock_prevents_a_second_controller(tmp_path, monkeypatch):
    root = tmp_path / "campaign"
    campaign(root, sites=("south",), years=(2018,))
    environment = root / "execution-environment"
    environment.mkdir()
    monkeypatch.chdir(tmp_path)
    with (environment / "process-scheduler.lock").open("a+") as lock:
        S.fcntl.flock(lock, S.fcntl.LOCK_EX | S.fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="Another campaign process scheduler"):
            S.resume(root)
    assert not list(environment.glob("process-resume-*.json"))


@pytest.mark.parametrize("processes", [0, 4, -1])
def test_invalid_process_ceiling_cannot_launch_jobs(tmp_path, processes):
    with pytest.raises(ValueError, match="one to three"):
        S.resume(tmp_path, processes=processes)
