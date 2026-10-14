"""Observable contracts for engine-independent renovation and study execution."""
from dataclasses import dataclass
from types import SimpleNamespace

import pytest


@dataclass(frozen=True)
class Measure:
    measure_id: str

    def to_dict(self):
        return {"measure_id": self.measure_id}


class Building:
    def __init__(self, actions=()):
        self.actions = tuple(actions)

    def to_dict(self):
        return {"actions": list(self.actions)}

    @classmethod
    def from_dict(cls, value):
        return cls(value["actions"])

    def renovate(self, *measures):
        return type(self)(self.actions + tuple(m.measure_id for m in measures))


def result(building, energy, carbon, complete=True):
    path = "/".join(building.actions) or "baseline"
    state = "/".join(sorted(building.actions)) or "baseline"
    return SimpleNamespace(
        id="evaluation:" + path, energy_id="energy:" + state,
        lca_id="lca:" + path, state_id=state, path_id=path,
        energy=SimpleNamespace(annual_delivered_energy_kwh=energy, complete=True),
        lca=SimpleNamespace(total_gwp_kgco2e=carbon, complete=complete),
        complete=complete, diagnostics={},
    )


def test_tree_keeps_dominated_prefix_and_intermediate_candidates():
    from epsm_workflows.renovation import optimise_renovations
    a, b = Measure("window"), Measure("ftx")
    values = {(): (100, 100), ("window",): (110, 120), ("ftx",): (80, 130),
              ("window", "ftx"): (40, 60), ("ftx", "window"): (40, 70)}
    tree = optimise_renovations(
        Building(), bundles=[(a,), (b,)], max_depth=2, budget=10,
        evaluator=lambda building: result(building, *values[building.actions]),
    )
    assert len(tree.nodes) == 5
    assert {n.depth for n in tree.nodes} == {0, 1, 2}
    assert [n.action_ids for n in tree.pareto] == [("window", "ftx")]
    assert tree.nodes[3].energy_id == tree.nodes[4].energy_id
    assert tree.nodes[3].lca_id != tree.nodes[4].lca_id


def test_incomplete_objectives_are_excluded_but_children_are_explored():
    from epsm_workflows.renovation import optimise_renovations
    a, b = Measure("a"), Measure("b")
    def evaluate(building):
        return result(building, 100 - len(building.actions),
                      None if building.actions == ("a",) else 20, building.actions != ("a",))
    tree = optimise_renovations(Building(), bundles=[(a,), (b,)], evaluator=evaluate,
                                max_depth=2, budget=10)
    assert len(tree.nodes) == 5
    assert not next(n for n in tree.nodes if n.action_ids == ("a",)).feasible
    assert all(n.carbon_kgco2e is not None for n in tree.pareto)


def test_budget_resume_preserves_ids_without_repeating_evaluations(tmp_path):
    from epsm_workflows.renovation import optimise_renovations
    calls = []
    def evaluate(building):
        calls.append(building.actions)
        return result(building, 100 - len(building.actions), 20 + len(building.actions))
    kwargs = dict(bundles=[(Measure("a"),), (Measure("b"),)], evaluator=evaluate,
                  evaluator_id="deterministic-test-v1", directory=tmp_path, max_depth=2)
    first = optimise_renovations(Building(), budget=2, **kwargs)
    second = optimise_renovations(Building(), budget=10, **kwargs)
    assert len(first.nodes) == 2 and len(second.nodes) == 5
    assert len(calls) == 5
    assert first.nodes[1].id == second.nodes[1].id
    assert second.nodes[1].evaluation_id == first.nodes[1].evaluation_id
    with pytest.raises(ValueError, match="configuration"):
        optimise_renovations(Building(), budget=10, **{**kwargs, "evaluator_id": "different"})


def test_bundle_atomicity_and_invalid_candidate_reporting():
    from epsm_workflows.renovation import optimise_renovations
    class Selective(Building):
        def renovate(self, *measures):
            if len(measures) > 1:
                raise ValueError("incompatible products")
            return super().renovate(*measures)
    tree = optimise_renovations(Selective(), bundles=[(Measure("a"), Measure("b"))],
                                evaluator=lambda b: result(b, 100, 50), max_depth=1)
    assert len(tree.nodes) == 1
    assert tree.rejected[0]["reason"] == "incompatible products"


def test_grid_lhs_and_global_sensitivity():
    from epsm_workflows.design import Parameter, grid, latin_hypercube, sensitivity_design, analyse_sensitivity
    parameters = [Parameter("x", 0, 1), Parameter("y", 0, 1)]
    assert len(grid({"x": [0, 1], "y": [2, 3]})) == 4
    samples = latin_hypercube(parameters, 10, seed=4)
    assert samples == latin_hypercube(parameters, 10, seed=4)
    assert sorted(int(s["x"] * 10) for s in samples) == list(range(10))
    design = sensitivity_design(parameters, 8192, seed=4)
    values = [s["x"] + 2 * s["y"] for s in design.samples]
    indices = analyse_sensitivity(design, values)
    assert indices["x"]["first_order"] == pytest.approx(.2, abs=.04)
    assert indices["x"]["total_order"] == pytest.approx(.2, abs=.04)
    assert indices["y"]["total_order"] == pytest.approx(.8, abs=.04)
    with pytest.raises(ValueError, match="variance"):
        analyse_sensitivity(design, [1] * len(values))


def evaluate_payload(payload):
    if payload.get("fail"):
        raise ValueError("intentional failed case")
    return {"answer": payload["x"] * 2}


def test_prepared_cases_resume_and_record_attempts(tmp_path):
    from epsm_workflows.execution import PreparedEvaluation, prepare, run_local, status
    cases = [PreparedEvaluation({"x": 2}), PreparedEvaluation({"x": 3, "fail": True})]
    prepare(tmp_path, cases, handler="test_harmonised_workflows:evaluate_payload")
    first = run_local(tmp_path)
    assert first["succeeded"] == 1 and first["failed"] == 1
    run_local(tmp_path)
    assert len(list((tmp_path / "attempts").glob("*/*.json"))) == 2
    run_local(tmp_path, retry_failed=True)
    assert len(list((tmp_path / "attempts").glob("*/*.json"))) == 3
    assert status(tmp_path)["succeeded"] == 1
    with pytest.raises(ValueError, match="immutable"):
        prepare(tmp_path, [PreparedEvaluation({"x": 4})], handler="test_harmonised_workflows:evaluate_payload")


def test_vera_stages_only_inputs_and_builds_quoted_submission(tmp_path):
    import subprocess
    from epsm_workflows.execution import PreparedEvaluation, prepare
    from epsm_workflows.vera import VeraExecutor
    manifest = prepare(tmp_path, [PreparedEvaluation({"x": 2})], handler="test_harmonised_workflows:evaluate_payload")
    calls = []
    def command(args, **kwargs):
        calls.append((args, kwargs))
        import json
        output = (json.dumps({"plan_id": manifest["id"]}) if " status " in " ".join(args)
                  else '[0]\n' if "pending" in " ".join(args)
                  else '12345;vera\n' if "sbatch" in " ".join(args) else '')
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")
    vera = VeraExecutor("vera1.c3se.chalmers.se", "/scratch/my study", python="/env/bin/python", command=command)
    vera.stage(tmp_path)
    receipt = vera.submit(tmp_path, account="research", time_limit="01:00:00")
    assert receipt["job_id"] == "12345"
    transfers = [args for args, _ in calls if args[0] == "rsync"]
    assert all("results" not in " ".join(args) for args in transfers)
    submission = next(args for args, _ in calls if "sbatch" in " ".join(args))
    assert "$SLURM_ARRAY_TASK_ID" in submission[-1]
    assert "--array=0" in submission[-1]
    assert "'/scratch/my study'" in submission[-1]


def test_prepared_assets_are_portable_and_checksum_verified(tmp_path):
    from epsm_workflows.execution import PreparedEvaluation, prepare, run_local
    import shutil
    source = tmp_path / "source.epw"
    source.write_text("weather content")
    case = PreparedEvaluation({"x": 2, "weather": {"$asset": "weather"}}, files={"weather": source})
    local = tmp_path / "local"
    prepare(local, [case], handler="test_harmonised_workflows:evaluate_payload")
    remote = tmp_path / "relocated"
    shutil.copytree(local, remote)
    source.unlink()
    assert run_local(remote)["succeeded"] == 1
    # A different run detects a corrupted copied resource before its evaluator.
    copied = next((local / "assets").glob("*/*.epw"))
    copied.write_text("changed")
    assert run_local(local)["failed"] == 1


def test_resume_rejects_tampered_results_and_tree(tmp_path):
    import json
    from epsm_workflows import PreparedEvaluation, prepare, run_local, optimise_renovations
    prepared = tmp_path / "prepared"
    prepare(prepared, [PreparedEvaluation({"x": 2})], handler="test_harmonised_workflows:evaluate_payload")
    run_local(prepared)
    path = prepared / "results/000000.json"
    data = json.loads(path.read_text())
    del data["result"]
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="changed or is incomplete"):
        run_local(prepared)
    options = dict(bundles=[], max_depth=0, evaluator=lambda b: result(b, 20, 30),
                   evaluator_id="tamper-test", directory=tmp_path / "tree")
    optimise_renovations(Building(), **options)
    path = tmp_path / "tree/tree.json"
    data = json.loads(path.read_text())
    data["nodes"][0]["carbon_kgco2e"] = 0
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="changed or is incomplete"):
        optimise_renovations(Building(), **options)


def test_partial_dict_assessment_never_enters_pareto():
    from epsm_workflows import optimise_renovations
    tree = optimise_renovations(Building(), bundles=[], max_depth=0,
        evaluator=lambda b: {"energy": {"annual_delivered_energy_kwh": 1},
                             "lca": {"total_gwp_kgco2e": 0, "status": "incomplete"}})
    assert not tree.pareto
    assert tree.nodes[0].reasons == ("incomplete evaluation",)


def _local_transport(args, **kwargs):
    """Exercise retrieval file merging without requiring an SSH account."""
    import shutil
    import subprocess
    from pathlib import Path

    source = Path(args[-2].split(":", 1)[1])
    target = Path(args[-1])
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True)
    else:
        shutil.copyfile(source, target)
    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def test_vera_retrieval_keeps_local_success_and_every_attempt(tmp_path, monkeypatch):
    import shutil
    from epsm_workflows import PreparedEvaluation, prepare, read_results, run_local, VeraExecutor

    local, remote = tmp_path / "local", tmp_path / "remote"
    prepare(local, [PreparedEvaluation({"x": 2})], handler="builtins:dict")
    shutil.copytree(local, remote)
    monkeypatch.setattr("epsm_workflows.execution._handler", lambda _: lambda payload: {"answer": 4})
    run_local(local)
    original = read_results(local)[0]

    def fail(payload):
        raise ValueError("remote temporary failure")

    monkeypatch.setattr("epsm_workflows.execution._handler", lambda _: fail)
    run_local(remote)
    (remote / "artifacts" / "remote.log").write_text("retained remote diagnostics")
    vera = VeraExecutor("test-host", str(remote), command=_local_transport)
    assert vera.retrieve(local)["succeeded"] == 1
    assert read_results(local)[0] == original
    assert len(list((local / "attempts").glob("*/*.json"))) == 2
    assert (local / "artifacts" / "remote.log").read_text() == "retained remote diagnostics"
    # Repeated retrieval does not duplicate attempts or replace the success.
    vera.retrieve(local)
    assert read_results(local)[0] == original


def test_vera_retrieval_validates_before_changing_local_evidence(tmp_path, monkeypatch):
    import json
    import shutil
    from epsm_workflows import PreparedEvaluation, prepare, read_results, run_local, VeraExecutor

    local, remote = tmp_path / "local", tmp_path / "remote"
    prepare(local, [PreparedEvaluation({"x": 2})], handler="builtins:dict")
    shutil.copytree(local, remote)
    monkeypatch.setattr("epsm_workflows.execution._handler", lambda _: lambda payload: {"answer": 4})
    run_local(local)
    run_local(remote)
    original = (local / "results/000000.json").read_bytes()
    path = remote / "results/000000.json"
    value = json.loads(path.read_text())
    value["result"] = {"answer": 0}
    path.write_text(json.dumps(value))
    vera = VeraExecutor("test-host", str(remote), command=_local_transport)
    with pytest.raises(ValueError, match="changed or is incomplete"):
        vera.retrieve(local)
    assert (local / "results/000000.json").read_bytes() == original
    assert len(list((local / "attempts").glob("*/*.json"))) == 1
    # A valid remote plan for different inputs is also rejected before merging.
    other = tmp_path / "other"
    prepare(other, [PreparedEvaluation({"x": 3})], handler="builtins:dict")
    vera.remote_directory = str(other)
    with pytest.raises(ValueError, match="Remote plan differs"):
        vera.retrieve(local)
    assert read_results(local)[0]["result"] == {"answer": 4}


def test_vera_retrieval_accepts_remote_recovery_without_losing_failure(tmp_path, monkeypatch):
    import shutil
    from epsm_workflows import PreparedEvaluation, prepare, read_results, run_local, VeraExecutor

    local, remote = tmp_path / "local", tmp_path / "remote"
    prepare(local, [PreparedEvaluation({"x": 2})], handler="builtins:dict")
    shutil.copytree(local, remote)

    def fail(payload):
        raise ValueError("local temporary failure")

    monkeypatch.setattr("epsm_workflows.execution._handler", lambda _: fail)
    run_local(local)
    monkeypatch.setattr("epsm_workflows.execution._handler", lambda _: lambda payload: {"answer": 4})
    run_local(remote)
    vera = VeraExecutor("test-host", str(remote), command=_local_transport)
    assert vera.retrieve(local)["succeeded"] == 1
    assert read_results(local)[0]["result"] == {"answer": 4}
    assert len(list((local / "attempts").glob("*/*.json"))) == 2

