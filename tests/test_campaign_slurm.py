"""Submission commands, quota gates and receipts without a scheduler."""

import json

import pytest

pytest.importorskip("duckdb")

from test_campaign_store import frozen, stub_cases

from epsm_workflows.campaign import slurm, store
from epsm_workflows.campaign.study import StudySpec

QUOTA = """Path: /cephyr/NOBACKUP/groups/c3se2026-1-14
  Space used:  120.5GiB  Quota:   10000GiB
  Files used:  18176  Quota:  1000000
Home: /cephyr/users/ssanjay/Vera
  Space used:  1.2GiB  Quota:   30GiB
  Files used:  1000  Quota:  60000
"""


def test_array_spec_walltime_and_quota_parsing(tmp_path):
    assert slurm.array_spec([5, 1, 2, 3, 9, 10]) == "1-3,5,9-10"
    assert slurm.walltime(3661) == "01:02:00"
    with pytest.raises(ValueError):
        slurm.array_spec([])
    root = tmp_path / "cephyr/NOBACKUP/groups/c3se2026-1-14/ssanjay/x"
    root.mkdir(parents=True)
    quota = slurm.parse_c3se_quota(QUOTA.replace("/cephyr", str(tmp_path / "cephyr")), root)
    assert quota["free_files"] == 1000000 - 18176 and quota["free_bytes"] == int((10000 - 120.5) * 1024**3)
    with pytest.raises(ValueError, match="did not report"):
        slurm.parse_c3se_quota(QUOTA, tmp_path)


def test_submission_command_and_receipt(tmp_path, calendar_epw, monkeypatch):
    root, catalog = frozen(tmp_path, calendar_epw)
    study = StudySpec(kind="ventilation_sweep", name="sub", seed=1,
                      cities=[{"slug": "a", "city": "A", "tmy": str(calendar_epw)}],
                      ventilation={"buildings": 1, "samples_per_building": 2},
                      execution={"notification_email": "x@example.org", "excluded_nodes": ["vera-r01-20"]})
    store.create_plan(root, study, stub_cases(4, ["a_tmy"]), catalog, {"study_id": study.id})
    monkeypatch.setattr(slurm, "quota_capacity", lambda root: {"free_bytes": 10**13, "free_files": 10**6, "source": "test"})
    monkeypatch.setattr(slurm, "max_array_tasks", lambda: 5000)
    command, receipt = slurm.submission(root, study, "pilot", "/opt/ep", account="acc", python="/venv/bin/python")
    assert command[:3] == ["sbatch", "--parsable", "--account=acc"]
    assert "--array=0-1%64" in command and "--mail-user=x@example.org" in command and "--exclude=vera-r01-20" in command
    assert command[-1].endswith('--phase pilot --index "$SLURM_ARRAY_TASK_ID" --energyplus /opt/ep')
    assert "/venv/bin/python -m epsm_workflows.campaign worker" in command[-1]
    assert receipt["submitted"] == 2 and receipt["task_indices"] == [0, 1] and receipt["concurrency"] == 64
    (root / "receipts").mkdir()
    (root / "receipts" / "pilot-0000.json").write_text(json.dumps({"complete": 1, "assigned": 1}))
    command, receipt = slurm.submission(root, study, "pilot", "/opt/ep", account="acc")
    assert "--array=1%64" in command and receipt["submitted"] == 1
    monkeypatch.setattr(slurm, "quota_capacity", lambda root: {"free_bytes": 10**9, "free_files": 10**6, "source": "test"})
    with pytest.raises(ValueError, match="headroom"):
        slurm.submission(root, study, "pilot", "/opt/ep", account="acc")


def test_submit_records_job_id_and_refuses_when_paused(tmp_path, calendar_epw, monkeypatch):
    root, catalog = frozen(tmp_path, calendar_epw)
    study = StudySpec(kind="ventilation_sweep", name="sub", seed=1,
                      cities=[{"slug": "a", "city": "A", "tmy": str(calendar_epw)}],
                      ventilation={"buildings": 1, "samples_per_building": 2})
    store.create_plan(root, study, stub_cases(2, ["a_tmy"]), catalog, {"study_id": study.id})
    monkeypatch.setattr(slurm, "quota_capacity", lambda root: {"free_bytes": 10**13, "free_files": 10**6, "source": "test"})
    monkeypatch.setattr(slurm, "max_array_tasks", lambda: 5000)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return type("R", (), {"stdout": "12345;vera\n"})()

    receipt = slurm.submit(root, study, "pilot", "/opt/ep", account="acc", run=run)
    assert receipt["job_id"] == "12345" and len(calls) == 1
    assert json.loads((root / "submissions" / "pilot.json").read_text())["job_id"] == "12345"
    (root / "PAUSE").write_text("{}")
    with pytest.raises(ValueError, match="paused"):
        slurm.submit(root, study, "pilot", "/opt/ep", account="acc", run=run)
