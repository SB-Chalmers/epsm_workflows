"""A task runs its range with stubbed engines and publishes a shard; failures pause."""

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("duckdb")
epsm_core = pytest.importorskip("epsm_core")
import duckdb
from test_campaign_store import fake_result, frozen, tiny_building

from epsm_workflows.campaign import store, worker
from epsm_workflows.campaign.cases import Case
from epsm_workflows.campaign.study import StudySpec


class StubBuilding:
    def __init__(self, data):
        self.data = data
        self.identifier = data["identifier"]

    @classmethod
    def from_dict(cls, data):
        return cls(data)


def prepared(tmp_path, calendar_epw, count=3):
    root, catalog = frozen(tmp_path, calendar_epw)
    study = StudySpec(kind="ventilation_sweep", name="worker", seed=1,
                      cities=[{"slug": "a", "city": "A", "tmy": str(calendar_epw)}],
                      ventilation={"buildings": 1, "samples_per_building": 2})

    def cases():
        for i in range(count):
            yield Case(building=tiny_building(f"b{i}"), weather_id="a_tmy", group_id=f"g{i}",
                       inputs={"x": float(i)},
                       axes={"study_kind": "ventilation_sweep", "sample_id": f"s{i}", "cohort": "fixed", "site": "a",
                             "context": "c", "weather_kind": "tmy", "footprint_family": "rectangle",
                             "geometry_source": "synthetic", "scenario_id": None, "measure_id": None, "pilot": True},
                       metadata={})

    receipt = store.create_plan(root, study, cases(), catalog, {"study_id": study.id})
    return root, receipt


def stub_engines(monkeypatch, tmp_path, fail_on=None, seen=None):
    def run_building(building, *, weather, directory, energyplus, **options):
        if seen is not None:
            seen.append((building.identifier, weather, directory, energyplus, options))
        if fail_on is not None and building.identifier == fail_on:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "eplusout.err").write_text("** Severe ** boom")
            raise RuntimeError("strict failure")
        return fake_result(int(building.identifier[1:]))

    monkeypatch.setattr(epsm_core, "Building", StubBuilding)
    monkeypatch.setattr(epsm_core, "Weather", SimpleNamespace(from_epw=lambda path, calendar_year: f"weather:{path.name}:{calendar_year}"))
    monkeypatch.setattr(epsm_core, "run_building", run_building)
    monkeypatch.setattr(worker, "runtime_identity", lambda energyplus: {"packages": {"epsm_core": "test"}, "energyplus": {"engine_hash": "e"}})
    monkeypatch.setenv("TMPDIR", str(tmp_path / "scratch"))
    (tmp_path / "scratch").mkdir(exist_ok=True)


def test_task_runs_range_resumes_and_publishes(tmp_path, calendar_epw, monkeypatch):
    root, _receipt = prepared(tmp_path, calendar_epw)
    seen = []
    stub_engines(monkeypatch, tmp_path, seen=seen)
    outcome = worker.run_task(root, "pilot-0001", "/opt/energyplus", rc=False, checkpoint_runs=1)
    assert outcome["complete"] == 1 and outcome["added"] == 1 and outcome["rc"] is False
    identifier, weather, directory, energyplus, options = seen[0]
    assert identifier == "s1" and weather == "weather:a_tmy.epw:2018" and energyplus == "/opt/energyplus"
    assert options == {"timeout_seconds": 1800} and not directory.exists()
    shard = duckdb.connect(str(root / "shards" / "pilot-0001.duckdb"), read_only=True)
    row = shard.execute("SELECT ordinal,case_id,run_id,rc_heating,rc_seconds,len(heating) FROM results").fetchone()
    assert row[0] == 1 and row[2] == "run-1" and row[3] is None and row[4] is None and row[5] == 8760
    assert json.loads(shard.execute("SELECT runtime FROM identity").fetchone()[0])["packages"]["epsm_core"] == "test"
    shard.close()
    again = worker.run_task(root, "pilot-0001", "/opt/energyplus", rc=False)
    assert again["complete"] == 1 and again["added"] == 0 and len(seen) == 1


def test_strict_failure_pauses_and_bundles_diagnostics(tmp_path, calendar_epw, monkeypatch):
    root, _receipt = prepared(tmp_path, calendar_epw)
    held = []
    stub_engines(monkeypatch, tmp_path, fail_on="s2")
    monkeypatch.setattr(worker, "hold_array", lambda: held.append(True))
    with pytest.raises(RuntimeError, match="strict failure"):
        worker.run_task(root, "pilot-0002", "/opt/energyplus", rc=False)
    pause = json.loads((root / "PAUSE").read_text())
    assert pause["task_id"] == "pilot-0002" and pause["ordinal"] == 2 and held == [True]
    assert (root / "diagnostics" / "pilot-0002-00002.tar.gz").exists()
    assert store.status(root)["paused"] and store.status(root)["failures"] == 1
    with pytest.raises(ValueError, match="paused"):
        store.finalize(root, tmp_path / "x.duckdb")


def test_rc_failure_keeps_the_energyplus_row(tmp_path, calendar_epw, monkeypatch):
    root, _receipt = prepared(tmp_path, calendar_epw)
    stub_engines(monkeypatch, tmp_path)

    def run_rc(building, weather, directory):
        raise ValueError("rc unsupported here")

    monkeypatch.setattr(worker, "run_rc", run_rc)
    worker.run_task(root, "pilot-0000", "/opt/energyplus", rc=True)
    shard = duckdb.connect(str(root / "shards" / "pilot-0000.duckdb"), read_only=True)
    rc_heating, rc_diagnostics, rc_seconds = shard.execute("SELECT rc_heating,rc_diagnostics,rc_seconds FROM results").fetchone()
    shard.close()
    assert rc_heating is None and rc_seconds >= 0
    assert json.loads(rc_diagnostics)["error"] == "rc unsupported here"


def test_changed_weather_is_refused(tmp_path, calendar_epw, monkeypatch):
    root, _receipt = prepared(tmp_path, calendar_epw)
    stub_engines(monkeypatch, tmp_path)
    monkeypatch.setattr(worker, "hold_array", lambda: None)
    with (root / "weather" / "a_tmy.epw").open("a") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match="Frozen weather changed"):
        worker.run_task(root, "pilot-0000", "/opt/energyplus", rc=False)
