# SPDX-License-Identifier: MIT
"""Vera batching delegates to the existing prepared executor and Slurm array."""

import json
import os
import shlex
import shutil
import subprocess
import sys

import pytest

from epsm_workflows import PreparedEvaluation, VeraExecutor, prepare, run_local
from epsm_workflows import execution


def prepared(tmp_path, count=10):
    local, remote = tmp_path / "local", tmp_path / "remote study"
    manifest = prepare(
        local, [PreparedEvaluation({"answer": i}) for i in range(count)],
        handler="builtins:dict",
    )
    shutil.copytree(local, remote)
    return local, remote, manifest


def transport(remote, manifest, *, indices=None, remote_id=None):
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        parts = shlex.split(args[-1])
        if "status" in parts:
            output = json.dumps({"plan_id": remote_id or manifest["id"]})
        elif "pending" in parts:
            output = json.dumps(indices if indices is not None else execution.pending(remote))
        elif "cancel" in parts:
            output = json.dumps(execution.cancel(remote))
        elif "resume" in parts:
            output = json.dumps(execution.resume(remote))
        elif "sbatch" in parts:
            output = "12345;test-cluster\n"
        else:
            raise AssertionError(parts)
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    return command, calls


def submission(calls):
    return next(shlex.split(args[-1]) for args in calls if "sbatch" in shlex.split(args[-1]))


def test_default_submit_keeps_case_array_command_and_receipt_shape(tmp_path):
    local, remote, manifest = prepared(tmp_path)
    command, calls = transport(remote, manifest, indices=[0, 2, 3])
    vera = VeraExecutor("test-host", str(remote), python=sys.executable, command=command)
    receipt = vera.submit(local, account="research", max_concurrent=2, retry_failed=True)
    assert receipt == {
        "plan_id": manifest["id"], "job_id": "12345", "submitted": 3,
        "indices": [0, 2, 3], "host": "test-host", "remote_directory": str(remote),
    }
    args = submission(calls)
    assert "--array=0,2-3%2" in args
    assert args[-1] == (
        shlex.join([sys.executable, "-m", "epsm_workflows.execution", "run", str(remote)])
        + ' --index "$SLURM_ARRAY_TASK_ID" --retry-failed'
    )
    assert json.loads((local / "submissions/12345.json").read_text()) == receipt


def test_sparse_batches_execute_original_rows_and_short_final_range(tmp_path):
    local, remote, manifest = prepared(tmp_path)
    run_local(remote, indices=[0, 2, 3, 4, 5, 6, 7])
    retained = execution.read_results(remote, require_success=False)[0]["id"]
    command, calls = transport(remote, manifest)
    vera = VeraExecutor("test-host", str(remote), python=sys.executable, command=command)
    receipt = vera.submit(local, account="research", cases_per_task=4, max_concurrent=2)
    assert receipt["submitted"] == 3 and receipt["indices"] == [1, 8, 9]
    assert receipt["task_indices"] == [0, 2] and receipt["task_count"] == 2
    assert receipt["cases_per_task"] == 4
    args = submission(calls)
    assert "--array=0,2%2" in args
    for task in receipt["task_indices"]:
        result = subprocess.run(
            ["sh", "-c", args[-1]], check=True, text=True, capture_output=True,
            env={**os.environ, "SLURM_ARRAY_TASK_ID": str(task)},
        )
        assert json.loads(result.stdout)["plan_id"] == manifest["id"]
    rows = execution.read_results(remote)
    assert [row["result"]["answer"] for row in rows] == list(range(10))
    assert rows[0]["id"] == retained
    assert len(list((remote / "attempts").glob("*/*.json"))) == 10
    assert json.loads((local / "manifest.json").read_text()) == manifest


@pytest.mark.parametrize("options", [
    {"cases_per_task": 0}, {"cases_per_task": -1}, {"cases_per_task": 1.5},
    {"cases_per_task": True}, {"array_size_limit": 0}, {"array_size_limit": True},
    {"array_size_limit": 1.5},
])
def test_invalid_batch_settings_do_not_contact_remote(tmp_path, options):
    local, remote, manifest = prepared(tmp_path)
    command, calls = transport(remote, manifest)
    vera = VeraExecutor("test-host", str(remote), command=command)
    with pytest.raises(ValueError):
        vera.submit(local, account="research", **options)
    assert calls == []


def test_array_limit_checks_largest_task_index_before_sbatch(tmp_path):
    local, remote, manifest = prepared(tmp_path)
    command, calls = transport(remote, manifest, indices=[9])
    vera = VeraExecutor("test-host", str(remote), command=command)
    with pytest.raises(ValueError, match="cases_per_task"):
        vera.submit(local, account="research", cases_per_task=4, array_size_limit=2)
    assert not any("sbatch" in shlex.split(args[-1]) for args in calls)
    receipt = vera.submit(local, account="research", cases_per_task=5, array_size_limit=2)
    assert receipt["task_indices"] == [1]


def test_empty_pending_keeps_existing_no_job_receipt(tmp_path):
    local, remote, manifest = prepared(tmp_path)
    command, calls = transport(remote, manifest, indices=[])
    vera = VeraExecutor("test-host", str(remote), command=command)
    assert vera.submit(local, account="research", cases_per_task=4) == {
        "plan_id": manifest["id"], "job_id": None, "submitted": 0,
    }
    assert not any("sbatch" in shlex.split(args[-1]) for args in calls)


def test_batched_retry_flag_is_forwarded(tmp_path):
    local, remote, manifest = prepared(tmp_path)
    command, calls = transport(remote, manifest, indices=[2])
    vera = VeraExecutor("test-host", str(remote), command=command)
    vera.submit(local, account="research", cases_per_task=4, retry_failed=True)
    assert "--retry-failed" in submission(calls)[-1]


def test_remote_graceful_controls_preserve_results_and_attempts(tmp_path):
    local, remote, manifest = prepared(tmp_path)
    run_local(remote, indices=[0])
    retained = execution.read_results(remote, require_success=False)[0]["id"]
    command, calls = transport(remote, manifest)
    vera = VeraExecutor("test-host", str(remote), command=command)
    assert vera.cancel(local)["cancel_requested"]
    assert execution.run_local(remote)["succeeded"] == 1
    assert "cancel_requested" not in vera.resume(local)
    assert execution.run_local(remote)["succeeded"] == 10
    assert execution.read_results(remote)[0]["id"] == retained
    assert len(list((remote / "attempts").glob("*/*.json"))) == 10
    assert not any("scancel" in args[-1] for args in calls)


@pytest.mark.parametrize("operation", ["cancel", "resume"])
def test_remote_control_rejects_another_plan_before_changing_it(tmp_path, operation):
    local, remote, manifest = prepared(tmp_path)
    command, calls = transport(remote, manifest, remote_id="another-plan")
    vera = VeraExecutor("test-host", str(remote), command=command)
    with pytest.raises(ValueError, match="Remote plan differs"):
        getattr(vera, operation)(local)
    assert len(calls) == 1
    assert not (remote / "control.json").exists()


def test_remote_control_verifies_its_returned_identity(tmp_path):
    local, remote, manifest = prepared(tmp_path)
    command, calls = transport(remote, manifest)

    def incorrect_receipt(args, **kwargs):
        result = command(args, **kwargs)
        if "cancel" in shlex.split(args[-1]):
            result.stdout = json.dumps({"plan_id": "incorrect-receipt"})
        return result

    vera = VeraExecutor("test-host", str(remote), command=incorrect_receipt)
    with pytest.raises(ValueError, match="Remote plan differs"):
        vera.cancel(local)
    assert len(calls) == 2


def test_remote_pending_boolean_is_not_a_case_index(tmp_path):
    local, remote, manifest = prepared(tmp_path)
    command, calls = transport(remote, manifest, indices=[True])
    vera = VeraExecutor("test-host", str(remote), command=command)
    with pytest.raises(ValueError, match="invalid case indices"):
        vera.submit(local, account="research", cases_per_task=4)
    assert not any("sbatch" in shlex.split(args[-1]) for args in calls)
