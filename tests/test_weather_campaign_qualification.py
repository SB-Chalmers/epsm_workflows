"""Bind campaign outcomes to local diagnostics without invoking simulation engines."""

import gzip
import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pandas as pd
import pytest

from examples import qualify_weather_campaign_results as Q

SITE = "fixture"
YEAR = 2023
BUILDING = "small"
CANDIDATE = "outage-panel-donor_assessed"
ALIAS = "outage-panel-automatic"


def sha(data):
    """Fingerprint fixture bytes independently of the qualification helper implementation."""
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    """Persist deterministic fixture evidence with readable actual JSON newlines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def epw_bytes(comment):
    """Supply eight EPW headers and one operative weather observation."""
    return (
        "LOCATION,Fixture,Fixture,SWE,Fixture,000000,57.0,12.0,1.0,10.0\n"
        "DESIGN CONDITIONS,0\n"
        "TYPICAL/EXTREME PERIODS,0\n"
        "GROUND TEMPERATURES,0\n"
        "HOLIDAYS/DAYLIGHT SAVINGS,No,0,0,0\n"
        f"COMMENTS 1,{comment}\n"
        f"COMMENTS 2,{comment}\n"
        "DATA PERIODS,1,1,Data,Sunday,1/1,12/31\n"
        "2023,1,1,1,60,?9?9?9?9E0?9?9?9*9*9*9*9*9*9*9*9*9*9*9*9*9,"
        "5.0,1.0,75,101325,0,0,300,0,0,0,0,0,0,0,180,2,5,5,"
        "9999,77777,0,999999999,10,0.1,0,0,0.2,0,0\n"
    ).encode()


def make_campaign(
    base, *, baseline_warning=False, candidate_warning=False, imported=False
):
    """Create native or authenticated imported simulation evidence for two runs."""
    root = (base / "campaign-v5").resolve()
    origin = (base / "campaign-v4").resolve()
    site_year = root / "datasets" / SITE / str(YEAR)
    records = []
    folders = {}
    rows = []
    for case, method, warning in (
        ("reference", "reference", baseline_warning),
        (CANDIDATE, "donor_assessed", candidate_warning),
    ):
        folder = site_year / "simulations" / BUILDING / case
        historical = origin / folder.relative_to(root) if imported else folder
        folder.mkdir(parents=True)
        weather = site_year / "cases" / case / "weather.epw"
        weather.parent.mkdir(parents=True)
        weather.write_bytes(epw_bytes(case))
        historical_weather = historical.parents[2] / "cases" / case / "weather.epw"
        model = json.dumps({"fixture_model": BUILDING}, sort_keys=True).encode()
        (folder / "model.epJSON").write_bytes(model)
        diagnostics = {
            "policy_version": "fixture-policy",
            "engine_version": "24.2",
            "messages": [],
            "accepted": True,
            "warning_count": int(warning),
            "severe_count": 0,
            "fatal_count": 0,
            "err_path": str(historical / "eplusout.err"),
            "warning_review_pending": False,
            "context_cap_review_pending": False,
        }
        raw_warning_count = 2 if warning else 0
        if warning:
            diagnostics.update(
                disposition="accepted_with_warnings",
                messages=[
                    {
                        "severity": "warning",
                        "summary": "Temperature out of range during psychrometric iteration",
                        "approved": True,
                        "disposition": "accepted_with_warning",
                        "exception_rule": {"scope": "annual_space_heating_demand"},
                    }
                ],
            )
        err = (
            "   ** Warning ** First psychrometric warning\n"
            "   ** Warning ** Recurring summary of that same warning\n"
            if warning
            else ""
        )
        err += (
            "EnergyPlus Completed Successfully-- "
            f"{raw_warning_count} Warning; 0 Severe Errors\n"
        )
        (folder / "eplusout.err").write_text(err)
        building = {
            "identifier": BUILDING,
            "load_reference_area_m2": 100.0,
            "climate": {
                "epw_path": str(historical_weather),
                "weather_sha256": sha(weather.read_bytes()),
                "calendar_year": YEAR,
            },
        }
        run_id = sha(f"fixture-run-{case}".encode())
        receipt = {
            "year": YEAR,
            "weather_sha256": sha(weather.read_bytes()),
            "weather_path": str(historical_weather),
            "model_sha256": sha(model),
            "core_run_id": run_id,
            "core_diagnostics_accepted": True,
            "core_result_path": str(historical / "result.json"),
            "core_building_spec_path": str(historical / "building.json"),
            "warning_count": raw_warning_count,
            "severe_errors": 0,
            "fatal_errors": 0,
        }
        for filename, document in (
            ("receipt.json", receipt),
            ("diagnostics.json", diagnostics),
            ("building.json", building),
            ("result.json", {"run_id": run_id, "diagnostics": deepcopy(diagnostics)}),
        ):
            write_json(folder / filename, document)
        folders[case] = folder
        records.append(
            {
                "original_folder": str(historical),
                "copied_folder": str(folder),
                "receipt_sha256": sha((folder / "receipt.json").read_bytes()),
                "weather_sha256": receipt["weather_sha256"],
                "model_sha256": receipt["model_sha256"],
                "core_run_id": run_id,
            }
        )
        rows.append(
            {
                "site_id": SITE,
                "year": YEAR,
                "building_id": BUILDING,
                "case_id": case,
                "method": method,
                "simulation_path": str(folder),
                "compiled_model_sha256": sha(model),
            }
        )
    alias_weather = site_year / "cases" / ALIAS / "weather.epw"
    alias_weather.parent.mkdir(parents=True)
    alias_weather.write_bytes(
        epw_bytes("different comments identify the automatic case")
    )
    rows.append(rows[-1] | {"case_id": ALIAS, "method": "automatic"})
    if imported:
        write_json(
            root / "execution-environment/imported-simulations.json",
            {
                "origin_campaign": str(origin),
                "accepted_runs_imported": len(records),
                "records": records,
            },
        )
    return root, pd.DataFrame(rows), folders


def revise_json(path, change):
    """Change one evidence document without silently altering its related artifacts."""
    document = json.loads(path.read_text())
    change(document)
    write_json(path, document)


def revise_diagnostics(folder, change):
    """Keep result diagnostics synchronized when testing legitimate engine dispositions."""
    path = folder / "diagnostics.json"
    document = json.loads(path.read_text())
    change(document)
    write_json(path, document)
    revise_json(
        folder / "result.json", lambda result: result.update(diagnostics=document)
    )


def test_clean_local_evidence_qualifies_aliases_and_captures_consumed_bytes(tmp_path):
    """Clean native diagnostics omit disposition and aliases may change only comments."""
    root, rows, folders = make_campaign(tmp_path)
    original = rows.copy(deep=True)
    before = {
        str(path): sha(path.read_bytes()) for path in root.rglob("*") if path.is_file()
    }
    inputs = {}
    annotated, excluded, receipt = Q.qualify_results(root, rows, inputs)
    pd.testing.assert_frame_equal(rows, original)
    assert annotated.engine_run_qualified.all()
    assert annotated.engine_outcome_qualified.all()
    assert annotated.engine_core_warning_count.eq(0).all()
    assert annotated.engine_raw_warning_count.eq(0).all()
    assert annotated.engine_exclusion_reasons.map(json.loads).tolist() == [[], [], []]
    assert excluded.empty
    assert receipt["raw_building_rows"] == receipt["qualified_building_rows"] == 3
    assert receipt["unique_simulations"] == receipt["qualified_simulations"] == 2
    assert receipt["excluded_building_rows"] == receipt["excluded_simulations"] == 0
    qualifier_path = str(Path(Q.__file__).resolve())
    qualifier_sha = sha(Path(Q.__file__).read_bytes())
    assert receipt["qualifier_script_sha256"] == qualifier_sha
    assert receipt["inputs"] == inputs == before | {qualifier_path: qualifier_sha}
    assert {
        str(path): sha(path.read_bytes()) for path in root.rglob("*") if path.is_file()
    } == before
    automatic = annotated.loc[annotated.method.eq("automatic")].iloc[0]
    assert automatic.engine_canonical_simulation_path == str(folders[CANDIDATE])
    assert automatic.engine_baseline_simulation_path == str(folders["reference"])


def test_scoped_warning_excludes_every_alias_and_all_outcome_metrics(tmp_path):
    """One Core warning and two raw markers exclude the shared canonical run."""
    root, rows, _ = make_campaign(tmp_path, candidate_warning=True)
    annotated, excluded, receipt = Q.qualify_results(root, rows)
    assert set(excluded.case_id) == {CANDIDATE, ALIAS}
    assert annotated.loc[
        annotated.method.eq("reference"), "engine_outcome_qualified"
    ].all()
    assert excluded.engine_core_warning_count.eq(1).all()
    assert excluded.engine_raw_warning_count.eq(2).all()
    assert not excluded.engine_run_qualified.any()
    assert not excluded.engine_outcome_qualified.any()
    assert receipt["excluded_building_rows"] == 2
    assert receipt["excluded_simulations"] == 1
    warning = next(record for record in receipt["records"] if not record["qualified"])
    assert warning["warning_exception_scopes"] == ["annual_space_heating_demand"]
    assert all(
        "engine_diagnostic_warning_or_error" in json.loads(value)
        for value in excluded.engine_exclusion_reasons
    )


def test_unqualified_reference_excludes_clean_related_outcomes_without_relabeling_runs(
    tmp_path,
):
    """Clean candidates retain own qualification but cannot compare against warned baselines."""
    root, rows, _ = make_campaign(tmp_path, baseline_warning=True)
    annotated, excluded, receipt = Q.qualify_results(root, rows)
    assert len(excluded) == len(rows) == 3
    assert not annotated.engine_outcome_qualified.any()
    candidates = annotated.loc[annotated.method.ne("reference")]
    assert candidates.engine_run_qualified.all()
    assert candidates.engine_raw_warning_count.eq(0).all()
    assert (
        candidates.engine_exclusion_reasons.map(json.loads).tolist()
        == [["reference_engine_unqualified"]] * 2
    )
    assert receipt["qualified_simulations"] == 1
    assert receipt["qualified_building_rows"] == 0
    assert receipt["exclusion_reason_counts"]["reference_engine_unqualified"] == 2


def test_pending_rejected_or_unsuccessful_runs_never_enter_building_evidence(tmp_path):
    """Acceptance requires completed execution and no pending or rejected disposition."""
    for defect in (
        "warning_review_pending",
        "context_cap_review_pending",
        "rejected",
        "not_accepted",
        "incomplete",
    ):
        root, rows, folders = make_campaign(tmp_path / defect)
        folder = folders[CANDIDATE]
        if defect in ("warning_review_pending", "context_cap_review_pending"):
            revise_diagnostics(
                folder,
                lambda document, field=defect: document.update({field: True}),
            )
        elif defect == "rejected":
            revise_diagnostics(
                folder, lambda document: document.update(disposition="rejected")
            )
        elif defect == "not_accepted":
            revise_diagnostics(folder, lambda document: document.update(accepted=False))
            revise_json(
                folder / "receipt.json",
                lambda document: document.update(core_diagnostics_accepted=False),
            )
        else:
            (folder / "eplusout.err").write_text(
                "EnergyPlus execution stopped before completion\n"
            )
        annotated, excluded, _ = Q.qualify_results(root, rows)
        assert set(excluded.case_id) == {CANDIDATE, ALIAS}, defect
        assert not annotated.loc[
            annotated.method.ne("reference"), "engine_outcome_qualified"
        ].any(), defect


def test_missing_local_artifacts_fail_instead_of_claiming_clean_execution(tmp_path):
    """Every receipt, model, diagnostic, result, specification and weather is required."""
    for filename in (
        "receipt.json",
        "diagnostics.json",
        "result.json",
        "eplusout.err",
        "model.epJSON",
        "building.json",
        "canonical-weather",
        "alias-weather",
    ):
        root, rows, folders = make_campaign(tmp_path / filename)
        folder = folders[CANDIDATE]
        if filename in ("canonical-weather", "alias-weather"):
            case = CANDIDATE if filename == "canonical-weather" else ALIAS
            path = folder.parents[2] / "cases" / case / "weather.epw"
        else:
            path = folder / filename
        path.unlink()
        with pytest.raises(ValueError, match="evidence is absent"):
            Q.qualify_results(root, rows)


def test_local_model_run_and_diagnostic_identity_tampering_is_fatal(tmp_path):
    """Summary identity, compiled bytes and separate Core artifacts must agree exactly."""
    for defect in ("model", "summary_model", "run_id", "diagnostics"):
        root, rows, folders = make_campaign(tmp_path / defect)
        folder = folders[CANDIDATE]
        if defect == "model":
            (folder / "model.epJSON").write_bytes(b'{"changed_model":true}\n')
        elif defect == "summary_model":
            rows.loc[rows.method.eq("automatic"), "compiled_model_sha256"] = "0" * 64
        elif defect == "run_id":
            revise_json(
                folder / "result.json",
                lambda document: document.update(run_id="another-core-run"),
            )
        else:
            revise_json(
                folder / "result.json",
                lambda document: document["diagnostics"].update(
                    policy_version="another-policy"
                ),
            )
        with pytest.raises(ValueError):
            Q.qualify_results(root, rows)


def test_candidate_model_must_match_reference_even_when_own_evidence_agrees(tmp_path):
    """Weather comparisons cannot silently change the physical building model itself."""
    root, rows, folders = make_campaign(tmp_path)
    model = folders[CANDIDATE] / "model.epJSON"
    model.write_bytes(b'{"fixture_model":"different-physical-building"}\n')
    forged_sha = sha(model.read_bytes())
    revise_json(
        folders[CANDIDATE] / "receipt.json",
        lambda document: document.update(model_sha256=forged_sha),
    )
    rows.loc[rows.method.ne("reference"), "compiled_model_sha256"] = forged_sha
    receipt = json.loads((folders[CANDIDATE] / "receipt.json").read_text())
    assert receipt["model_sha256"] == forged_sha
    assert (
        rows.loc[rows.method.ne("reference"), "compiled_model_sha256"]
        .eq(forged_sha)
        .all()
    )
    assert sha((folders["reference"] / "model.epJSON").read_bytes()) != forged_sha
    with pytest.raises(ValueError):
        Q.qualify_results(root, rows)


def test_weather_bytes_and_serialized_artifact_paths_require_canonical_binding(
    tmp_path,
):
    """Changed operative aliases and redirected weather or diagnostic paths are refused."""
    for defect in (
        "canonical_weather",
        "alias_weather",
        "receipt_result_path",
        "diagnostic_err_path",
        "receipt_weather_path",
        "building_weather_path",
        "building_weather_sha",
    ):
        root, rows, folders = make_campaign(tmp_path / defect)
        folder = folders[CANDIDATE]
        other = tmp_path / "unrelated"
        if defect in ("canonical_weather", "alias_weather"):
            case = CANDIDATE if defect == "canonical_weather" else ALIAS
            weather = folder.parents[2] / "cases" / case / "weather.epw"
            weather.write_bytes(
                weather.read_bytes().replace(b"5.0,1.0,75", b"55.0,1.0,75")
            )
        elif defect == "receipt_result_path":
            revise_json(
                folder / "receipt.json",
                lambda document, destination=other: document.update(
                    core_result_path=str(destination / "result.json")
                ),
            )
        elif defect == "diagnostic_err_path":
            revise_diagnostics(
                folder,
                lambda document, destination=other: document.update(
                    err_path=str(destination / "eplusout.err")
                ),
            )
        elif defect == "receipt_weather_path":
            revise_json(
                folder / "receipt.json",
                lambda document, destination=other: document.update(
                    weather_path=str(destination / "weather.epw")
                ),
            )
        elif defect == "building_weather_path":
            revise_json(
                folder / "building.json",
                lambda document, destination=other: document["climate"].update(
                    epw_path=str(destination / "weather.epw")
                ),
            )
        else:
            revise_json(
                folder / "building.json",
                lambda document: document["climate"].update(weather_sha256="0" * 64),
            )
        with pytest.raises(ValueError):
            Q.qualify_results(root, rows)


def test_raw_core_and_receipt_diagnostic_counts_cannot_conceal_warnings(tmp_path):
    """Severity evidence must agree on presence while legitimate recurring counts differ."""
    for defect in (
        "core_zero",
        "raw_zero",
        "receipt_zero",
        "missing_message",
        "boolean_count",
    ):
        root, rows, folders = make_campaign(tmp_path / defect, candidate_warning=True)
        folder = folders[CANDIDATE]
        if defect == "core_zero":
            revise_diagnostics(
                folder, lambda document: document.update(warning_count=0)
            )
        elif defect == "raw_zero":
            (folder / "eplusout.err").write_text(
                "EnergyPlus Completed Successfully-- 0 Warning; 0 Severe Errors\n"
            )
        elif defect == "receipt_zero":
            revise_json(
                folder / "receipt.json",
                lambda document: document.update(warning_count=0),
            )
        elif defect == "missing_message":
            revise_diagnostics(folder, lambda document: document.update(messages=[]))
        else:
            revise_diagnostics(
                folder, lambda document: document.update(warning_count=True)
            )
        with pytest.raises(ValueError):
            Q.qualify_results(root, rows)


def test_authenticated_imports_use_local_bytes_without_reading_historical_outputs(
    tmp_path,
):
    """An exact import journal binds old serialized paths to copied local evidence."""
    root, rows, _ = make_campaign(tmp_path, imported=True, candidate_warning=True)
    assert not (tmp_path / "campaign-v4").exists()
    inputs = {}
    annotated, excluded, receipt = Q.qualify_results(root, rows, inputs)
    assert annotated.loc[
        annotated.method.eq("reference"), "engine_outcome_qualified"
    ].all()
    assert set(excluded.case_id) == {CANDIDATE, ALIAS}
    assert str(root / "execution-environment/imported-simulations.json") in inputs
    assert all(
        Path(record["canonical_simulation_path"]).is_relative_to(root)
        for record in receipt["records"]
    )
    assert all(
        not Path(record["historical_simulation_path"]).exists()
        for record in receipt["records"]
    )
    assert all(
        Path(path).is_relative_to(root) or Path(path) == Path(Q.__file__).resolve()
        for path in inputs
    )


@pytest.mark.parametrize("imported", [False, True], ids=["native", "imported"])
def test_recorded_repository_paths_resolve_from_an_unrelated_working_directory(
    tmp_path, monkeypatch, imported
):
    """Relative run and import records retain their repository identity outside it."""
    repository = tmp_path / "fixture-repository"
    root, rows, folders = make_campaign(repository, imported=imported)
    rows["simulation_path"] = rows.simulation_path.map(
        lambda value: str(Path(value).relative_to(repository))
    )
    if imported:
        journal_path = root / "execution-environment/imported-simulations.json"
        journal = json.loads(journal_path.read_text())
        journal["origin_campaign"] = str(
            Path(journal["origin_campaign"]).relative_to(repository)
        )
        for record in journal["records"]:
            for field in ("original_folder", "copied_folder"):
                record[field] = str(Path(record[field]).relative_to(repository))
        write_json(journal_path, journal)
    assert not any(Path(value).is_absolute() for value in rows.simulation_path)
    weather_root = folders[CANDIDATE].parents[2] / "cases"
    assert sha((weather_root / ALIAS / "weather.epw").read_bytes()) != sha(
        (weather_root / CANDIDATE / "weather.epw").read_bytes()
    )
    unrelated = tmp_path / "unrelated-empty-directory"
    unrelated.mkdir()
    monkeypatch.setattr(Q, "REPOSITORY", repository.resolve())
    monkeypatch.chdir(unrelated)
    annotated, excluded, receipt = Q.qualify_results(root.resolve(), rows)
    assert annotated.engine_run_qualified.all()
    assert annotated.engine_outcome_qualified.all()
    assert excluded.empty
    assert receipt["qualified_building_rows"] == 3
    assert receipt["qualified_simulations"] == 2
    assert (
        annotated.loc[
            annotated.method.eq("automatic"), "engine_canonical_simulation_path"
        ]
        .eq(str(folders[CANDIDATE]))
        .all()
    )
    assert not list(unrelated.iterdir())


def test_import_journal_cannot_authorize_unbound_or_ambiguous_historical_runs(tmp_path):
    """Imported receipts, weather, model, run identity and analogous paths stay bound."""
    for defect in (
        "missing_journal",
        "receipt_sha256",
        "weather_sha256",
        "model_sha256",
        "core_run_id",
        "original_folder",
        "copied_folder",
        "duplicate_record",
        "wrong_count",
    ):
        root, rows, _ = make_campaign(tmp_path / defect, imported=True)
        path = root / "execution-environment/imported-simulations.json"
        if defect == "missing_journal":
            path.unlink()
        else:
            journal = json.loads(path.read_text())
            if defect in (
                "receipt_sha256",
                "weather_sha256",
                "model_sha256",
                "core_run_id",
            ):
                journal["records"][1][defect] = "0" * 64
            elif defect in ("original_folder", "copied_folder"):
                journal["records"][1][defect] = str(tmp_path / "outside" / CANDIDATE)
            elif defect == "duplicate_record":
                journal["records"].append(journal["records"][1].copy())
                journal["accepted_runs_imported"] += 1
            else:
                journal["accepted_runs_imported"] += 1
            write_json(path, journal)
        with pytest.raises(ValueError):
            Q.qualify_results(root, rows)


def test_gzip_weather_is_verified_without_restoration_or_losing_alias_identity(
    tmp_path,
):
    """Lossless compressed EPWs qualify while their original receipts remain immutable."""
    root, rows, _ = make_campaign(tmp_path)
    for path in root.rglob("weather.epw"):
        Path(str(path) + ".gz").write_bytes(gzip.compress(path.read_bytes(), mtime=0))
        path.unlink()
    inputs = {}
    annotated, excluded, _ = Q.qualify_results(root, rows, inputs)
    assert annotated.engine_outcome_qualified.all()
    assert excluded.empty
    assert sum(path.endswith(".epw.gz") for path in inputs) == 3
    assert not list(root.rglob("weather.epw"))


def test_duplicate_missing_or_redirected_row_identities_are_rejected(tmp_path):
    """Outcome rows require unique identities and a local matching reference baseline."""
    root, rows, _ = make_campaign(tmp_path)
    variants = [
        pd.concat([rows, rows.iloc[[0]]], ignore_index=True),
        rows.loc[rows.method.ne("reference")].copy(),
        rows.drop(columns="compiled_model_sha256"),
        rows.assign(simulation_path=str(tmp_path / "unrelated-simulation")),
        rows.assign(building_id="unrelated-profile"),
    ]
    for altered in variants:
        with pytest.raises(ValueError):
            Q.qualify_results(root, altered)


def test_empty_qualification_has_schema_and_never_invents_clean_simulation_rows(
    tmp_path,
):
    """An empty provisional table reports zero evidence and preserves qualification columns."""
    root, rows, _ = make_campaign(tmp_path)
    annotated, excluded, receipt = Q.qualify_results(root, rows.iloc[:0])
    assert annotated.empty and excluded.empty
    assert set(Q.QUALIFICATION_COLUMNS).issubset(annotated.columns)
    assert receipt["raw_building_rows"] == receipt["unique_simulations"] == 0
    assert receipt["qualified_building_rows"] == receipt["excluded_building_rows"] == 0
    assert receipt["records"] == []
    qualifier_sha = sha(Path(Q.__file__).read_bytes())
    assert receipt["qualifier_script_sha256"] == qualifier_sha
    assert receipt["inputs"] == {str(Path(Q.__file__).resolve()): qualifier_sha}
