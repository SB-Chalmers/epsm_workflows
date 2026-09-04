from dataclasses import replace
import json
from pathlib import Path

import numpy as np
import pytest

from epsm_core import SimulationResult
from epsm_core.compiler import CompilationReport
from epsm_workflows.store import CampaignStore


def test_conflict_and_incomplete(tmp_path):
    store = CampaignStore(tmp_path / "campaign", {"seed": 42})
    store.db.close()
    with pytest.raises(ValueError):
        CampaignStore(tmp_path / "campaign", {"seed": 43})
    other = tmp_path / "other"
    other.mkdir()
    (other / "unrelated.txt").write_text("keep")
    with pytest.raises(ValueError):
        CampaignStore(other, {"seed": 42})


def test_atomic_completion_and_corruption(tmp_path):
    store = CampaignStore(tmp_path / "campaign", {"seed": 42})
    rid = "a" * 64
    store.db.execute("INSERT INTO runs(run_id,ordinal,status) VALUES (?,0,'pending')", [rid])
    scalars = {
        "space_heating_demand_kwh": 8760.0,
        "dhw_load_kwh": 8760.0,
        "ideal_thermal_load_kwh": 17520.0,
    }
    hourly = {k: np.full(8760, v / 8760) for k, v in scalars.items()}
    result = SimulationResult(rid, scalars, hourly, {k: "kWh" for k in hourly}, {}, {})
    report = CompilationReport(
        run_id=rid,
        compiler_version="test",
        engine_version="test",
        engine_sha256="a",
        schema_sha256="b",
        spec_sha256="c",
        report_json="{}",
    )
    broken = replace(result, hourly={k: v[:-1] for k, v in hourly.items()})
    with pytest.raises(ValueError):
        store.complete(broken, report)
    assert store.db.execute("SELECT status FROM runs").fetchone()[0] == "pending"
    store.complete(result, report)
    assert store.validate() == dict(completed=1, errors=[], orphan_partitions=[])
    (store.path / "partitions" / rid / "hourly.parquet").write_bytes(b"corrupt")
    assert store.validate()["errors"]


def test_orphan_recovery(tmp_path):
    store = CampaignStore(tmp_path / "campaign", {"seed": 42})
    orphan = store.path / "partitions" / "orphan"
    orphan.mkdir(parents=True)
    (orphan / "pending").write_text("recoverable")
    assert store.validate()["orphan_partitions"] == ["orphan"]
    store.recover()
    assert not store.validate()["orphan_partitions"]
    assert list((store.path / "quarantine").iterdir())
