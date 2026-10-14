"""Host-managed structured events identify work without exposing case payloads."""

import logging

from test_harmonised_workflows import Building, result


def test_executor_events_keep_worker_context_and_report_failure_once(
    tmp_path, caplog, monkeypatch
):
    from epsm_workflows import PreparedEvaluation, prepare, run_local

    caplog.set_level(logging.INFO, logger="epsm_workflows")

    def handle(payload):
        if payload.get("fail"):
            raise ValueError("Expected software test failure")
        return {"answer": 4}

    monkeypatch.setattr("epsm_workflows.execution._handler", lambda _: handle)
    prepare(
        tmp_path,
        [
            PreparedEvaluation({"x": 2, "private": "do-not-log-this"}),
            PreparedEvaluation({"fail": True}),
        ],
        handler="builtins:dict",
    )
    run_local(tmp_path, workers=2)
    records = [
        record for record in caplog.records if record.name.startswith("epsm_workflows")
    ]
    assert records and all(hasattr(record, "epsm_event") for record in records)
    started = [r for r in records if r.epsm_event == "study.case.started"]
    failed = [r for r in records if r.epsm_event == "study.case.failed"]
    assert len(started) == 2 and len(failed) == 1
    assert failed[0].exc_info is not None
    assert len({r.epsm_context["case_id"] for r in started}) == 2
    assert failed[0].epsm_context["case_id"] in {
        r.epsm_context["case_id"] for r in started
    }
    assert all("do-not-log-this" not in repr(r.__dict__) for r in records)
    assert all(isinstance(r.epsm_details, dict) for r in records)
    assert not logging.getLogger("epsm_workflows.execution").handlers


def test_search_events_include_tree_and_candidate_identity(caplog):
    from epsm_workflows import optimise_renovations

    caplog.set_level(logging.INFO, logger="epsm_workflows")
    tree = optimise_renovations(
        Building(), bundles=[], max_depth=0, evaluator=lambda b: result(b, 100, 50)
    )
    records = [r for r in caplog.records if r.name == "epsm_workflows.renovation"]
    assert {r.epsm_event for r in records} >= {
        "study.search.started",
        "study.evaluation.completed",
        "study.search.completed",
    }
    assert all(r.epsm_context["study_id"] == tree.id for r in records)
    evaluated = next(r for r in records if r.epsm_event == "study.evaluation.completed")
    assert evaluated.epsm_context["case_id"] == tree.nodes[0].id
    assert not logging.getLogger("epsm_workflows.renovation").handlers


def test_import_does_not_configure_logging_or_create_files(tmp_path):
    import subprocess
    import sys

    code = """
import logging
def unexpected(*args, **kwargs):
    raise AssertionError('Library import configured logging')
logging.basicConfig = unexpected
logging.Logger.addHandler = unexpected
import epsm_workflows
import epsm_workflows.execution
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout == result.stderr == ""
    assert not tuple(tmp_path.iterdir())
