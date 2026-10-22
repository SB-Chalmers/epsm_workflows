# SPDX-License-Identifier: MIT
"""Bounded dispatch preserves prepared identities, integrity and attempts."""

import json

import pytest

from epsm_workflows import execution


def evaluate(payload):
    if payload.get("fail"):
        raise ValueError("Intentional failed case")
    return {"answer": payload["answer"]}


def plan(directory, count=5, *, failing=()):
    return execution.prepare(
        directory,
        [
            execution.PreparedEvaluation({"answer": i, "fail": i in failing})
            for i in range(count)
        ],
        handler="test_execution_batches:evaluate",
    )


def test_selected_batch_runs_only_requested_rows_and_verifies_once(tmp_path, monkeypatch):
    manifest = plan(tmp_path)
    original = execution._runtime_identity
    checked, observed = [], []

    def verify(cases, handler):
        checked.append(len(cases))
        return original(cases, handler)

    def handler(payload):
        observed.append(payload["answer"])
        return evaluate(payload)

    monkeypatch.setattr(execution, "_runtime_identity", verify)
    monkeypatch.setattr(execution, "_handler", lambda _: handler)
    result = execution.run_local(tmp_path, indices=[3, 1])
    assert result == {
        "plan_id": manifest["id"], "total": 5, "succeeded": 2,
        "failed": 0, "unfinished": 3,
    }
    assert observed == [3, 1]
    assert checked == [5]
    retained = execution.read_results(tmp_path, require_success=False)[1]["id"]
    execution.run_local(tmp_path, indices=(i for i in [0, 1, 2]), workers=2)
    assert checked == [5, 5]
    assert execution.read_results(tmp_path, require_success=False)[1]["id"] == retained
    assert len(list((tmp_path / "attempts").glob("*/*.json"))) == 4


@pytest.mark.parametrize("indices", [[True], [-1], [5], [1.5], ["1"], [0, 0]])
def test_invalid_selection_creates_no_attempts(tmp_path, indices):
    plan(tmp_path)
    with pytest.raises(ValueError, match="indices"):
        execution.run_local(tmp_path, indices=indices)
    assert not list((tmp_path / "attempts").glob("*/*.json"))


def test_empty_selection_does_not_dispatch(tmp_path):
    plan(tmp_path)
    assert execution.run_local(tmp_path, indices=[])["unfinished"] == 5
    assert not list((tmp_path / "attempts").glob("*/*.json"))


@pytest.mark.parametrize("target", ["case", "asset"])
def test_batch_checks_unselected_inputs_before_creating_attempts(tmp_path, target):
    source = tmp_path / "weather.epw"
    source.write_text("original weather")
    root = tmp_path / "plan"
    execution.prepare(
        root,
        [
            execution.PreparedEvaluation({"answer": 0}),
            execution.PreparedEvaluation({"answer": 1}, files={"weather": source}),
        ],
        handler="test_execution_batches:evaluate",
    )
    if target == "case":
        path = root / "cases/000001.json"
        value = json.loads(path.read_text())
        value["payload"]["answer"] = 10
        path.write_text(json.dumps(value))
    else:
        next((root / "assets").glob("*/*")).write_text("changed weather")
    with pytest.raises(ValueError, match="changed"):
        execution.run_local(root, indices=[0])
    assert not list((root / "attempts").glob("*/*.json"))


def test_selected_case_changed_during_batch_is_rejected(tmp_path, monkeypatch):
    plan(tmp_path)

    def mutate(payload):
        path = tmp_path / "cases/000002.json"
        value = json.loads(path.read_text())
        value["payload"]["answer"] = 100
        path.write_text(json.dumps(value))
        return evaluate(payload)

    monkeypatch.setattr(execution, "_handler", lambda _: mutate)
    with pytest.raises(ValueError, match="changed"):
        execution.run_local(tmp_path, indices=[0, 2])
    assert (tmp_path / "results/000000.json").exists()
    assert not (tmp_path / "results/000002.json").exists()


def test_subset_retries_failures_only_when_requested(tmp_path, monkeypatch):
    plan(tmp_path, failing=[1])
    assert execution.run_local(tmp_path, indices=[0, 1])["failed"] == 1
    retained = execution.read_results(tmp_path, require_success=False)[0]["id"]
    monkeypatch.setattr(
        execution, "_handler", lambda _: lambda payload: {"answer": payload["answer"]}
    )
    assert execution.run_local(tmp_path, indices=[0, 1])["failed"] == 1
    result = execution.run_local(tmp_path, indices=[0, 1], retry_failed=True)
    assert result["succeeded"] == 2 and result["unfinished"] == 3
    assert execution.read_results(tmp_path, require_success=False)[0]["id"] == retained
    assert len(list((tmp_path / "attempts").glob("*/*.json"))) == 3


def test_cancel_and_resume_preserve_bounded_batch_attempts(tmp_path, monkeypatch):
    plan(tmp_path)

    def stop(payload):
        execution.cancel(tmp_path)
        return evaluate(payload)

    monkeypatch.setattr(execution, "_handler", lambda _: stop)
    result = execution.run_local(tmp_path, indices=range(4))
    assert result["succeeded"] == 1 and result["cancel_requested"]
    retained = execution.read_results(tmp_path, require_success=False)[0]["id"]
    execution.resume(tmp_path)
    monkeypatch.setattr(execution, "_handler", lambda _: evaluate)
    assert execution.run_local(tmp_path, indices=range(4))["succeeded"] == 4
    assert execution.read_results(tmp_path, require_success=False)[0]["id"] == retained
    assert len(list((tmp_path / "attempts").glob("*/*.json"))) == 4


def test_cli_bounded_range_uses_exclusive_stop(tmp_path, capsys):
    plan(tmp_path)
    assert execution.main(["run", str(tmp_path), "--start", "1", "--stop", "4"]) == 0
    assert json.loads(capsys.readouterr().out)["succeeded"] == 3
    rows = execution.read_results(tmp_path, require_success=False)
    assert [index for index, row in enumerate(rows) if row is not None] == [1, 2, 3]


@pytest.mark.parametrize("options", [
    ["--start", "1"], ["--stop", "2"], ["--start", "-1", "--stop", "2"],
    ["--start", "3", "--stop", "2"], ["--start", "0", "--stop", "6"],
    ["--index", "0", "--start", "0", "--stop", "2"],
])
def test_cli_invalid_ranges_fail_before_dispatch(tmp_path, options):
    plan(tmp_path)
    with pytest.raises((SystemExit, ValueError)):
        execution.main(["run", str(tmp_path), *options])
    assert not list((tmp_path / "attempts").glob("*/*.json"))
