# SPDX-License-Identifier: MIT
# Copyright (c) 2026 EPSM Development contributors
# License: See LICENSE at the project root.
"""Engine content is bound to cases, with strict checks before execution/reuse."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from epsm_workflows import execution
from epsm_workflows.records import digest


@pytest.fixture
def engines(tmp_path, monkeypatch):
    compiler = pytest.importorskip("epsm_core.compiler")
    first, second = tmp_path / "engine-a", tmp_path / "engine-b"
    first.write_bytes(b"first binary")
    second.write_bytes(b"second binary")
    inspected = []

    def inspect(path, *, settings=None):
        from pathlib import Path

        inspected.append(str(path))
        return SimpleNamespace(
            engine_hash=hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            schema_hash="b" * 64,
            version="Version 24.2.0",
        )

    monkeypatch.setattr(compiler, "EnergyPlusCompiler", inspect)
    return first, second, inspected


def case(path, name):
    return execution.PreparedEvaluation(
        {"simulation": {"engine": "energyplus", "energyplus": str(path)}},
        name=name,
    )


def evaluate(payload):
    if payload.get("fail"):
        raise ValueError("Intentional case failure")
    return {"answer": payload["answer"]}


def transient_failure(payload):
    from pathlib import Path

    marker = Path(payload["_execution"]["directory"]) / "transient-failure-recorded"
    if payload.get("fail_once") and not marker.exists():
        marker.write_text("The test's one transient failure was observed")
        raise ValueError("Transient failure")
    return {"answer": payload["answer"]}


def test_runtime_binds_each_case_to_its_engine_content(tmp_path, engines):
    first, second, inspected = engines
    plan = execution.prepare(
        tmp_path / "plan", [case(first, "a"), case(second, "b"), case(first, "repeat")]
    )
    runtime = plan["runtime"]
    assert plan["schema"] == 3
    assert len(inspected) == 2
    assert len(runtime["engines"]) == 2
    assert list(runtime["engines"]) == sorted(runtime["engines"])
    assert set(runtime["case_engines"]) == set(plan["case_ids"])
    assert (
        runtime["case_engines"][plan["case_ids"][0]]
        == runtime["case_engines"][plan["case_ids"][2]]
    )
    assert (
        runtime["case_engines"][plan["case_ids"][0]]
        != runtime["case_engines"][plan["case_ids"][1]]
    )
    assert all(key == digest(value) for key, value in runtime["engines"].items())
    assert str(tmp_path) not in json.dumps(runtime)


def test_identical_executable_contents_share_inventory_identity(tmp_path, engines):
    first, second, _ = engines
    second.write_bytes(first.read_bytes())
    plan = execution.prepare(tmp_path / "plan", [case(first, "a"), case(second, "b")])
    assert len(plan["runtime"]["engines"]) == 1
    assert len(set(plan["runtime"]["case_engines"].values())) == 1


def test_swapped_executables_are_detected_even_when_inventory_is_unchanged(
    tmp_path, engines
):
    first, second, _ = engines
    directory = tmp_path / "plan"
    plan = execution.prepare(directory, [case(first, "a"), case(second, "b")])
    left, right = first.read_bytes(), second.read_bytes()
    first.write_bytes(right)
    second.write_bytes(left)
    actual = execution._runtime_identity(
        [execution._case(directory, i)[1] for i in range(2)],
        plan["handler"],
    )
    assert actual["engines"] == plan["runtime"]["engines"]
    assert actual["case_engines"] != plan["runtime"]["case_engines"]
    with pytest.raises(ValueError, match="runtime differs"):
        execution.run_case(directory, 0)
    assert not list((directory / "attempts").glob("*/*.json"))


def test_mixed_energyplus_and_rc_cases_keep_both_engine_kinds(tmp_path, engines):
    pytest.importorskip("epsm_rc")
    first, _, _ = engines
    rc = execution.PreparedEvaluation({"simulation": {"engine": "epsm_rc"}}, name="rc")
    plan = execution.prepare(tmp_path / "plan", [case(first, "eplus"), rc])
    assert {value["engine"] for value in plan["runtime"]["engines"].values()} == {
        "energyplus",
        "epsm_rc",
    }


def test_missing_selected_rc_implementation_is_not_a_null_fingerprint(
    tmp_path, monkeypatch
):
    original = execution.importlib.util.find_spec
    monkeypatch.setattr(
        execution.importlib.util,
        "find_spec",
        lambda name: None if name == "epsm_rc" else original(name),
    )
    rc = execution.PreparedEvaluation({"simulation": {"engine": "epsm_rc"}}, name="rc")
    with pytest.raises(ImportError, match="epsm_rc"):
        execution.prepare(tmp_path / "plan", [rc])
    assert not (tmp_path / "plan/manifest.json").exists()


@pytest.mark.parametrize(
    "reader",
    [
        execution.validate_plan,
        execution.read_results,
        execution.status,
        execution.pending,
        execution.run_local,
        lambda path: execution.run_case(path, 0),
    ],
)
def test_schema_two_requires_a_new_plan(tmp_path, reader):
    plan = execution.prepare(
        tmp_path,
        [execution.PreparedEvaluation({"answer": 1})],
        handler="test_prepared_runtime:evaluate",
    )
    plan["schema"] = 2
    plan["id"] = digest({key: value for key, value in plan.items() if key != "id"})
    (tmp_path / "manifest.json").write_text(json.dumps(plan))
    with pytest.raises(ValueError, match="prepare a new plan"):
        reader(tmp_path)


def test_existing_schema_two_is_not_rewritten_by_prepare(tmp_path):
    cases = [execution.PreparedEvaluation({"answer": 1})]
    plan = execution.prepare(tmp_path, cases, handler="test_prepared_runtime:evaluate")
    plan["schema"] = 2
    plan["id"] = digest({key: value for key, value in plan.items() if key != "id"})
    original = json.dumps(plan)
    (tmp_path / "manifest.json").write_text(original)
    with pytest.raises(ValueError, match="prepare a new plan"):
        execution.prepare(tmp_path, cases, handler="test_prepared_runtime:evaluate")
    assert (tmp_path / "manifest.json").read_text() == original


def test_local_batch_verifies_runtime_once_but_each_invocation_is_fresh(
    tmp_path, monkeypatch
):
    cases = [execution.PreparedEvaluation({"answer": i}) for i in range(5)]
    execution.prepare(tmp_path, cases, handler="test_prepared_runtime:evaluate")
    original = execution._runtime_identity
    calls = []

    def inspect(cases, handler):
        calls.append(len(cases))
        return original(cases, handler)

    monkeypatch.setattr(execution, "_runtime_identity", inspect)
    assert execution.run_local(tmp_path, workers=2)["succeeded"] == 5
    assert calls == [5]
    assert execution.run_local(tmp_path)["succeeded"] == 5
    assert calls == [5, 5]
    execution.run_case(tmp_path, 0)
    assert calls == [5, 5, 5]


def test_standalone_reuse_rejects_changed_asset_without_creating_attempt(tmp_path):
    source = tmp_path / "input.txt"
    source.write_text("original")
    root = tmp_path / "plan"
    execution.prepare(
        root,
        [execution.PreparedEvaluation({"answer": 1}, files={"input": source})],
        handler="test_prepared_runtime:evaluate",
    )
    execution.run_case(root, 0)
    asset = next((root / "assets").glob("*/*"))
    asset.write_text("changed")
    with pytest.raises(ValueError, match="asset changed"):
        execution.run_case(root, 0)
    assert len(list((root / "attempts").glob("*/*.json"))) == 1


@pytest.mark.parametrize(
    "resume",
    [execution.run_local, execution.pending, lambda path: execution.run_case(path, 0)],
)
def test_completed_cases_cannot_bypass_changed_runtime_checks(
    tmp_path, monkeypatch, resume
):
    execution.prepare(
        tmp_path,
        [execution.PreparedEvaluation({"answer": 1})],
        handler="test_prepared_runtime:evaluate",
    )
    execution.run_local(tmp_path)
    original = execution._runtime_identity
    monkeypatch.setattr(
        execution,
        "_runtime_identity",
        lambda cases, handler: {
            **original(cases, handler),
            "python": "changed-worker",
        },
    )
    with pytest.raises(ValueError, match="runtime differs"):
        resume(tmp_path)
    assert len(list((tmp_path / "attempts").glob("*/*.json"))) == 1


def test_verified_plan_is_deeply_immutable_and_handler_receives_detached_values(
    tmp_path, monkeypatch
):
    execution.prepare(
        tmp_path,
        [execution.PreparedEvaluation({"answer": 1, "nested": [1, 2]})],
        handler="test_prepared_runtime:evaluate",
        environment={"nested": {"version": "fixed"}},
    )
    verified = execution._verified_plan(tmp_path)
    with pytest.raises(TypeError):
        verified.manifest["environment"]["nested"]["version"] = "changed"
    with pytest.raises(TypeError):
        verified.manifest["case_ids"][0] = "changed"

    def mutate(payload):
        payload["nested"].append(99)
        payload["_execution"]["case_id"] = "forged"
        return {"answer": payload["answer"]}

    monkeypatch.setattr(execution, "_handler", lambda _: mutate)
    assert execution.run_local(tmp_path)["succeeded"] == 1
    assert (
        execution.read_results(tmp_path)[0]["case_id"]
        == verified.manifest["case_ids"][0]
    )
    assert execution._case(tmp_path, 0)[1]["payload"]["nested"] == [1, 2]


@pytest.mark.parametrize("target", ["case", "manifest"])
def test_local_batch_rejects_inputs_changed_after_preflight(
    tmp_path, monkeypatch, target
):
    execution.prepare(
        tmp_path,
        [execution.PreparedEvaluation({"answer": i}) for i in range(2)],
        handler="test_prepared_runtime:evaluate",
    )

    def mutate(payload):
        if payload["answer"] == 0:
            path = tmp_path / (
                "cases/000001.json" if target == "case" else "manifest.json"
            )
            data = json.loads(path.read_text())
            if target == "case":
                data["payload"]["answer"] = 99
            else:
                data["environment"] = {"changed": True}
                data["id"] = digest(
                    {key: value for key, value in data.items() if key != "id"}
                )
            path.write_text(json.dumps(data))
        return {"answer": payload["answer"]}

    monkeypatch.setattr(execution, "_handler", lambda _: mutate)
    with pytest.raises(ValueError, match="changed"):
        execution.run_local(tmp_path, workers=1)
    assert not (tmp_path / "results/000001.json").exists()


def test_failed_jansen_row_stays_ordered_and_explicit_retry_completes_design(tmp_path):
    from epsm_workflows import Parameter, analyse_sensitivity, sensitivity_design

    design = sensitivity_design([Parameter("x", 0, 1)], 2, seed=42)
    cases = [
        execution.PreparedEvaluation(
            {"answer": row["x"], "fail_once": i == 0},
            inputs=row,
            name=f"row-{i}",
        )
        for i, row in enumerate(design.samples)
    ]
    execution.prepare(
        tmp_path, cases, handler="test_prepared_runtime:transient_failure"
    )
    first = execution.run_local(tmp_path)
    assert first["failed"] == 1 and first["succeeded"] == 5
    with pytest.raises(ValueError, match="no successful result"):
        execution.read_results(tmp_path)
    rows = execution.read_results(tmp_path, require_success=False)
    assert rows[0]["status"] == "failed"
    with pytest.raises(ValueError, match="finite result"):
        analyse_sensitivity(
            design,
            [
                float("nan") if row["status"] == "failed" else row["result"]["answer"]
                for row in rows
            ],
        )
    retained = rows[1]["id"]
    execution.run_local(tmp_path)
    assert len(list((tmp_path / "attempts").glob("*/*.json"))) == 6
    assert execution.run_local(tmp_path, retry_failed=True)["succeeded"] == 6
    rows = execution.read_results(tmp_path)
    assert rows[1]["id"] == retained
    values = [row["result"]["answer"] for row in rows]
    assert values == [sample["x"] for sample in design.samples]
    assert analyse_sensitivity(design, values)["x"]["n"] == 2
    assert len(list((tmp_path / "attempts").glob("*/*.json"))) == 7
