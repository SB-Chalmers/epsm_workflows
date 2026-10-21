"""Collection validates retained evidence independently of execution runtimes."""
import pytest

from epsm_workflows.execution import (PreparedEvaluation, prepare, run_case,
                                      read_attempts, validate_plan, inspect_plan, cancel, resume,
                                      run_local, status)


def test_collection_preserves_running_failure_and_success_receipts(tmp_path):
    prepare(tmp_path, [PreparedEvaluation({"x": 1}), PreparedEvaluation({"x": 2})],
            handler="builtins:dict")
    run_case(tmp_path, 0)
    assert len(inspect_plan(tmp_path)["cases"]) == 2
    assert validate_plan(tmp_path)["case_ids"] == inspect_plan(tmp_path)["manifest"]["case_ids"]
    attempts = read_attempts(tmp_path)
    assert len(attempts) == 1 and attempts[0]["status"] == "succeeded"
    path = next((tmp_path / "attempts").glob("*/*.json"))
    path.write_text(path.read_text().replace('"index":0', '"index":9'))
    with pytest.raises(ValueError, match="changed|different"):
        read_attempts(tmp_path)


def test_cancel_stops_dispatch_and_requires_explicit_resume(tmp_path):
    prepare(tmp_path, [PreparedEvaluation({"x": 1})], handler="builtins:dict")
    cancel(tmp_path)
    assert run_local(tmp_path)["unfinished"] == 1
    assert not read_attempts(tmp_path)
    resume(tmp_path)
    assert run_local(tmp_path)["succeeded"] == 1
    assert status(tmp_path)["unfinished"] == 0


def cancel_during_evaluation(payload):
    """A live case requests cancellation, then finishes its own outcome."""
    cancel(payload["directory"])
    return {"finished": payload["value"]}


def test_active_case_finishes_and_queued_cases_stay_unattempted(tmp_path):
    prepare(tmp_path, [PreparedEvaluation({"directory": str(tmp_path), "value": i})
                       for i in range(3)],
            handler=__name__ + ":cancel_during_evaluation")
    outcome = run_local(tmp_path, workers=1)
    assert outcome["succeeded"] == 1 and outcome["unfinished"] == 2
    assert outcome["cancel_requested"]
    attempts = read_attempts(tmp_path)
    assert len(attempts) == 1 and attempts[0]["status"] == "succeeded"
