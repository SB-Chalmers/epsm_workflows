import json
from pathlib import Path

import duckdb
import numpy as np
import pytest

import epsm_workflows.campaign as campaign
from epsm_workflows.cli import (
    _array_spec,
    _parse_c3se_quota,
    _quota_headroom_ok,
    main,
    submit,
)
from epsm_workflows.campaign import (
    SCALAR_FIELDS,
    _preflight_schema,
    _schema,
    _shard_schema,
    atomic_json,
    finalize,
    preflight,
    prepare_production_tasks,
    require_preflight,
)
from epsm_workflows.surrogate.design import effective_scaled_wwr


def _production_fixture(root):
    root.mkdir()
    plan = duckdb.connect(str(root / "plan.duckdb"))
    _schema(plan)
    plan.execute("INSERT INTO manifest VALUES ('campaign','{}')")
    rows = []
    for ordinal in range(130):
        rows.append(
            (
                ordinal,
                f"case-{ordinal}",
                f"sample-{ordinal}",
                f"group-{ordinal}",
                "anchors",
                "site",
                "sparse",
                "tmy",
                "site_tmy",
                "rectangle",
                ordinal == 0,
                float(ordinal + 1),
                "{}",
                "{}",
                "{}",
            )
        )
    plan.executemany("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    plan.execute("INSERT INTO tasks VALUES ('pilot',0,'pilot-0000',100)")
    plan.execute("INSERT INTO task_members VALUES ('pilot-0000',0,0)")
    plan.close()
    atomic_json(root / "plan.json", {"campaign_id": "identity"})
    (root / "shards").mkdir()
    shard = duckdb.connect(str(root / "shards/pilot-0000.duckdb"))
    shard.execute("CREATE TABLE results(elapsed_seconds DOUBLE)")
    shard.execute("INSERT INTO results VALUES (10.0)")
    shard.close()


def _preflight_fixture(root, count=3):
    root.mkdir()
    plan = duckdb.connect(str(root / "plan.duckdb"))
    _schema(plan)
    manifest = {
        "engine_sha256": "engine",
        "schema_sha256": "schema",
        "compiler_sha256": "compiler",
    }
    plan.execute("INSERT INTO manifest VALUES ('campaign', ?)", [json.dumps(manifest)])
    for ordinal in range(count):
        plan.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ordinal,
                f"case-{ordinal}",
                f"sample-{ordinal}",
                f"group-{ordinal}",
                "anchors",
                "site",
                "sparse",
                "tmy",
                "site_tmy",
                "rectangle",
                ordinal == 0,
                1.0,
                json.dumps({"ordinal": ordinal}),
                "{}",
                "{}",
            ],
        )
    plan.close()
    atomic_json(root / "plan.json", {"campaign_id": "identity"})


def _passing_preflight(root):
    plan = duckdb.connect(str(root / "plan.duckdb"), read_only=True)
    rows = plan.execute("SELECT ordinal,case_id FROM runs ORDER BY ordinal").fetchall()
    plan.close()
    checks = duckdb.connect(str(root / "preflight.duckdb"))
    _preflight_schema(checks)
    checks.execute(
        "INSERT INTO identity VALUES ('identity',?,'engine','schema','compiler')",
        [len(rows)],
    )
    checks.executemany(
        "INSERT INTO results VALUES (?,?,'passed',NULL,NULL,0.1)", rows
    )
    checks.close()
    atomic_json(
        root / "preflight.json",
        {
            "state": "passed",
            "campaign_id": "identity",
            "expected": len(rows),
            "checked": len(rows),
            "passed": len(rows),
            "failed": 0,
        },
    )


class _PreflightSpec:
    @staticmethod
    def model_validate_json(value):
        return json.loads(value)


class _PreflightCompiler:
    engine_hash = "engine"
    schema_hash = "schema"
    compiler_hash = "compiler"
    calls = []
    failing = set()

    def __init__(self, _energyplus):
        pass

    def compile(self, spec):
        self.calls.append(spec["ordinal"])
        if spec["ordinal"] in self.failing:
            error = ValueError("invalid generated geometry")
            error.code = "GEOMETRY_INVARIANT_FAILURE"
            raise error
        return {}, {}


def test_preflight_resumes_and_requires_exhaustive_identity(monkeypatch, tmp_path):
    root = tmp_path / "campaign"
    _preflight_fixture(root)
    monkeypatch.setattr(campaign, "BuildingSpec", _PreflightSpec)
    monkeypatch.setattr(campaign, "EnergyPlusCompiler", _PreflightCompiler)
    _PreflightCompiler.calls = []
    _PreflightCompiler.failing = set()

    checks = duckdb.connect(str(root / "preflight.duckdb"))
    _preflight_schema(checks)
    checks.execute(
        "INSERT INTO identity VALUES ('identity',3,'engine','schema','compiler')"
    )
    checks.execute(
        "INSERT INTO results VALUES (0,'case-0','passed',NULL,NULL,0.1)"
    )
    checks.close()

    receipt = preflight(root, "energyplus", workers=1)
    assert receipt["state"] == "passed"
    assert receipt["checked"] == receipt["passed"] == 3
    assert _PreflightCompiler.calls == [1, 2]
    assert require_preflight(root, "energyplus")["state"] == "passed"

    assert preflight(root, "energyplus", workers=1)["state"] == "passed"
    assert _PreflightCompiler.calls == [1, 2]

    _PreflightCompiler.engine_hash = "other-engine"
    with pytest.raises(ValueError, match="does not match preflight"):
        require_preflight(root, "energyplus")
    _PreflightCompiler.engine_hash = "engine"

    checks = duckdb.connect(str(root / "preflight.duckdb"))
    checks.execute("UPDATE identity SET compiler_sha256='other-compiler'")
    checks.close()
    with pytest.raises(ValueError, match="another campaign or compiler"):
        preflight(root, "energyplus", workers=1)


def test_preflight_collects_nonpilot_failure_and_blocks_gate(monkeypatch, tmp_path):
    root = tmp_path / "campaign"
    _preflight_fixture(root)
    monkeypatch.setattr(campaign, "BuildingSpec", _PreflightSpec)
    monkeypatch.setattr(campaign, "EnergyPlusCompiler", _PreflightCompiler)
    _PreflightCompiler.calls = []
    _PreflightCompiler.failing = {1, 2}

    receipt = preflight(root, "energyplus", workers=1)
    assert receipt["state"] == "failed"
    assert receipt["checked"] == 3
    assert receipt["failed"] == 2
    assert [failure["ordinal"] for failure in receipt["failures"]] == [1, 2]
    assert {
        failure["failure_code"] for failure in receipt["failures"]
    } == {"GEOMETRY_INVARIANT_FAILURE"}
    assert not (root / "PAUSE").exists()
    with pytest.raises(ValueError, match="incomplete, failed"):
        require_preflight(root, "energyplus")
    assert (
        main(
            [
                "preflight",
                str(root),
                "--energyplus",
                "energyplus",
                "--workers",
                "1",
            ]
        )
        == 1
    )


def test_submit_rejects_missing_preflight_before_scheduler(tmp_path):
    root = tmp_path / "campaign"
    root.mkdir()
    atomic_json(root / "config.json", {"notification_email": "test@example.com"})
    with pytest.raises(ValueError, match="preflight is missing"):
        submit(root, "pilot", "energyplus", "account", 1)


def test_vera_launcher_gates_pilot_behind_preflight_stage():
    # Historical source now lives in epsm_studies; the launcher remains here.
    launcher = Path(__file__).parents[1] / "campaigns/vera.sh"
    source = launcher.read_text()
    start_stage = source.split("    preflight)", 1)[0]
    preflight_stage = source.split("    preflight)", 1)[1].split("    production)", 1)[0]
    assert "schedule_stage preflight" in start_stage
    assert "advance pilot" not in start_stage
    assert preflight_stage.index('cli preflight "$campaign"') < preflight_stage.index(
        "advance pilot production 64"
    )


def test_production_plan_is_atomic_and_idempotent(tmp_path):
    root = tmp_path / "campaign"
    _production_fixture(root)

    first = prepare_production_tasks(root, max_tasks=5000)
    second = prepare_production_tasks(root, max_tasks=5000)

    assert first == second
    assert first["batch_size"] == 128
    assert first["production_tasks"] == 2
    plan = duckdb.connect(str(root / "plan.duckdb"), read_only=True)
    assert plan.execute(
        "SELECT count(*) FROM tasks WHERE phase='production'"
    ).fetchone()[0] == 2
    assert plan.execute(
        "SELECT count(*) FROM task_members tm JOIN tasks t USING(task_id) "
        "WHERE t.phase='production'"
    ).fetchone()[0] == 129
    assert plan.execute(
        "SELECT count(*) FROM manifest WHERE key='production_plan'"
    ).fetchone()[0] == 1
    plan.close()


def test_production_plan_rolls_back_all_assignments(monkeypatch, tmp_path):
    root = tmp_path / "campaign"
    _production_fixture(root)
    original_connect = campaign.duckdb.connect

    class FailingPlan:
        def __init__(self, connection):
            self.connection = connection
            self.member_batches = 0

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def executemany(self, statement, parameters):
            if statement.startswith("INSERT INTO task_members"):
                self.member_batches += 1
                if self.member_batches == 2:
                    raise RuntimeError("injected interruption")
            return self.connection.executemany(statement, parameters)

    def failing_connect(database, *args, **kwargs):
        connection = original_connect(database, *args, **kwargs)
        if Path(database) == root / "plan.duckdb" and not kwargs.get("read_only"):
            return FailingPlan(connection)
        return connection

    monkeypatch.setattr(campaign.duckdb, "connect", failing_connect)
    with pytest.raises(RuntimeError, match="injected interruption"):
        prepare_production_tasks(root, max_tasks=5000)

    plan = original_connect(str(root / "plan.duckdb"), read_only=True)
    assert plan.execute(
        "SELECT count(*) FROM tasks WHERE phase='production'"
    ).fetchone()[0] == 0
    assert plan.execute(
        "SELECT count(*) FROM manifest WHERE key='production_plan'"
    ).fetchone()[0] == 0
    plan.close()


def test_worker_commits_once_and_resumes(monkeypatch, tmp_path):
    root = tmp_path / "campaign"
    root.mkdir()
    plan = duckdb.connect(str(root / "plan.duckdb"))
    _schema(plan)
    plan.execute("INSERT INTO manifest VALUES ('campaign','{}')")
    plan.execute(
        "INSERT INTO runs VALUES (0,'case','sample','group','anchors','site','sparse',"
        "'tmy','site_tmy','rectangle',true,1,'{}','{}','{}')"
    )
    plan.execute("INSERT INTO tasks VALUES ('pilot',0,'pilot-0000',100)")
    plan.execute("INSERT INTO task_members VALUES ('pilot-0000',0,0)")
    plan.close()
    atomic_json(
        root / "plan.json",
        {"campaign_id": "identity", "groups": 1, "weather_rows": 8760},
    )

    class Spec:
        @staticmethod
        def model_validate_json(_value):
            return object()

    class Result:
        run_id = "run"
        hourly = {
            "space_heating_demand_kwh": np.ones(8760),
            "cooling_demand_kwh": np.ones(8760) * 2,
            "dhw_load_kwh": np.ones(8760) * 3,
            "ideal_thermal_load_kwh": np.ones(8760) * 4,
        }
        scalars = {
            "space_heating_demand_kwh": 8760,
            "cooling_demand_kwh": 17520,
            "dhw_load_kwh": 26280,
        }
        diagnostics = {"accepted": True}

    calls = []
    scratch_paths = []
    monkeypatch.setenv("TMPDIR", str(tmp_path / "node-local"))
    (tmp_path / "node-local").mkdir()

    class Runner:
        def __init__(self, _energyplus):
            pass

        def run(self, _spec, scratch, timeout_seconds):
            calls.append(timeout_seconds)
            scratch.mkdir()
            scratch_paths.append(scratch)
            (scratch / "eplusout.sql").write_text("raw output")
            return Result()

    monkeypatch.setattr(campaign, "BuildingSpec", Spec)
    monkeypatch.setattr(campaign, "EnergyPlusRunner", Runner)
    assert campaign.run_task(root, "pilot-0000", "engine")["added"] == 1
    assert campaign.run_task(root, "pilot-0000", "engine")["added"] == 0
    assert calls == [1800]
    assert scratch_paths and all(not path.exists() for path in scratch_paths)
    assert all(path.parent == tmp_path / "node-local" for path in scratch_paths)


def test_quota_guard_accepts_ceph_unreported_inode_availability():
    projected_bytes = 30 * 1024**3
    assert _quota_headroom_ok(
        free_bytes=7 * 1024**5,
        free_files=-1,
        projected_bytes=projected_bytes,
        projected_files=470,
    )
    assert not _quota_headroom_ok(
        free_bytes=projected_bytes + 500 * 1024**3 - 1,
        free_files=-1,
        projected_bytes=projected_bytes,
        projected_files=470,
    )


def test_slurm_array_indices_are_compacted():
    assert _array_spec([0, 1, 2, 5, 7, 8, 9]) == "0-2,5,7-9"


def test_scaled_windows_are_suppressed_on_centimetre_facade_remnants():
    assert effective_scaled_wwr(0.03272, 0.08788) == 0
    assert effective_scaled_wwr(1.0, 0.08788) == pytest.approx(0.08788)


def test_c3se_quota_selects_matching_storage_project():
    output = """Current usage and quotas for user "ssanjay":

Home: /cephyr/users/ssanjay
  Space used:     3.4GiB       Quota:      30GiB
  Files used:      41971       Quota:      60000

Path: /cephyr/NOBACKUP/groups/c3se2026-1-14
Proj: c3se2026-1-14
  Space used:  1557.8GiB       Quota:   11000GiB
  Files used:     264854       Quota:     300000
"""
    capacity = _parse_c3se_quota(
        output,
        Path("/cephyr/NOBACKUP/groups/c3se2026-1-14/ssanjay/epsm-60000"),
    )
    assert capacity["source"] == "C3SE_quota"
    assert capacity["quota_path"] == "/cephyr/NOBACKUP/groups/c3se2026-1-14"
    assert capacity["free_files"] == 35146
    assert capacity["quota_files"] == 300000
    projected_bytes = 30 * 1024**3
    assert not _quota_headroom_ok(
        free_bytes=7 * 1024**5,
        free_files=5469,
        projected_bytes=projected_bytes,
        projected_files=470,
    )


def test_finalize_is_self_contained_and_hourly_is_lazy(tmp_path):
    root = tmp_path / "campaign"
    root.mkdir()
    plan = duckdb.connect(str(root / "plan.duckdb"))
    _schema(plan)
    plan.execute("INSERT INTO manifest VALUES ('campaign','{}')")
    weather = [
        ("site_tmy", t, "2018-01-01 00:00:00", *([1.0] * 10)) for t in range(8760)
    ]
    plan.executemany("INSERT INTO weather VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", weather)
    inputs = json.dumps(
        {
            "num_floors": 3,
            "topology": "FTX",
            "schedule_family": "daily",
            "schedule_shift": 0,
            "wall_u": 0.2,
        }
    )
    for ordinal in range(30):
        plan.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ordinal,
                f"case-{ordinal}",
                f"sample-{ordinal}",
                "anchor-group",
                "anchors",
                "site",
                "sparse",
                "tmy",
                "site_tmy",
                "rectangle",
                True,
                1.0,
                "{}",
                inputs,
                "{}",
            ],
        )
    plan.close()
    atomic_json(root / "plan.json", {"campaign_id": "identity"})
    _passing_preflight(root)
    (root / "shards").mkdir()
    shard = duckdb.connect(str(root / "shards/task.duckdb"))
    _shard_schema(shard)
    shard.execute("INSERT INTO identity VALUES ('task','identity')")
    heating, cooling, dhw = [0.1] * 8760, [0.2] * 8760, [0.3] * 8760
    scalar_values = dict.fromkeys(SCALAR_FIELDS, 0.0)
    scalar_values.update(
        space_heating_demand_kwh=sum(heating),
        cooling_demand_kwh=sum(cooling),
        dhw_load_kwh=sum(dhw),
        ideal_thermal_load_kwh=sum(heating) + sum(dhw),
    )
    scalars = json.dumps(scalar_values)
    shard.executemany(
        "INSERT INTO results VALUES (?,?,?,?,?,?,?,?,?,current_timestamp)",
        [
            (
                ordinal,
                f"case-{ordinal}",
                f"run-{ordinal}",
                scalars,
                "{}",
                heating,
                cooling,
                dhw,
                1.0,
            )
            for ordinal in range(30)
        ],
    )
    shard.close()
    destination = tmp_path / "handoff.duckdb"
    receipt = finalize(root, destination)
    assert receipt["reopened_read_only"] == 30
    db = duckdb.connect(str(destination), read_only=True)
    assert db.execute("SELECT count(*) FROM training_rows").fetchone()[0] == 30
    assert json.loads(
        db.execute("SELECT value FROM manifest WHERE key='preflight'").fetchone()[0]
    )["state"] == "passed"
    assert db.execute("SELECT ideal_thermal_load_kwh FROM hourly LIMIT 1").fetchone()[
        0
    ] == pytest.approx(0.4)
    assert db.execute("SELECT input_wall_u FROM runs LIMIT 1").fetchone()[0] == 0.2
    db.close()
    assert not (root / "preflight.duckdb").exists()
