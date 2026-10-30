"""Plan, shard and handoff databases keep identities and acceptance rules."""

import json
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("duckdb")
import duckdb

from epsm_workflows.campaign import store
from epsm_workflows.campaign.cases import Case
from epsm_workflows.campaign.study import StudySpec
from epsm_workflows.campaign.weather import file_sha256

SCALARS = {name: float(i + 1) for i, name in enumerate(store.SCALAR_FIELDS)}


def tiny_building(identifier, floors=2, context=4):
    return {
        "identifier": identifier,
        "geometry": {"parts": [{"footprint": {"exterior": [[0, 0], [10, 0], [10, 8], [0, 8]]},
                                "storey_elevations_m": [3 * i for i in range(floors + 1)]}]},
        "context": {"buildings": [{"id": i} for i in range(context)]},
    }


def study_for(epw, tmp_path):
    return StudySpec(
        kind="ventilation_sweep", name="store", seed=1,
        cities=[{"slug": "a", "city": "A", "tmy": str(epw)}],
        ventilation={"buildings": 1, "samples_per_building": 2},
    )


def stub_cases(count, weather_ids):
    for i in range(count):
        group = f"g{i // 2:02d}"
        yield Case(
            building=tiny_building(f"{group}_scene"),
            weather_id=weather_ids[i % len(weather_ids)],
            group_id=group,
            inputs={"infiltration_ach": 0.1 + 0.01 * i, "topology": "S", "num_floors": 2},
            axes={"study_kind": "ventilation_sweep", "sample_id": f"s{i:03d}", "cohort": "fixed", "site": "a",
                  "context": "urban_lowrise_dense", "weather_kind": "tmy", "footprint_family": "rectangle",
                  "geometry_source": "synthetic", "scenario_id": None, "measure_id": None, "pilot": i < 2},
            metadata={"stratum_seed": 1, "lhs_unit": [i / count]},
        )


def frozen(tmp_path, epw):
    root = tmp_path / "pkg"
    (root / "weather").mkdir(parents=True)
    target = root / "weather" / "a_tmy.epw"
    target.write_bytes(epw.read_bytes())
    catalog = {"a_tmy": {"path": "weather/a_tmy.epw", "sha256": file_sha256(target), "site": "a", "kind": "tmy", "calendar_year": 2018}}
    (root / "weather.json").write_text(json.dumps(catalog))
    return root, catalog


def fake_result(ordinal):
    hours = np.full(8760, 0.5 + ordinal)
    hourly = {"space_heating_demand_kwh": hours, "cooling_demand_kwh": np.zeros(8760), "dhw_load_kwh": hours / 2,
              "ideal_thermal_load_kwh": hours + hours / 2}
    scalars = {**SCALARS, "space_heating_demand_kwh": float(hours.sum()), "cooling_demand_kwh": 0.0,
               "dhw_load_kwh": float((hours / 2).sum()), "ideal_thermal_load_kwh": float(hours.sum() * 1.5)}
    return SimpleNamespace(run_id=f"run-{ordinal}", scalars=scalars, hourly=hourly, diagnostics={"accepted": True})


def write_shard(root, task_id, rows, campaign_id, *, rc=False):
    shard = store.open_shard(root / "shards" / f"{task_id}.duckdb", task_id, campaign_id, {"packages": {}})
    for ordinal, case_id in rows:
        result = fake_result(ordinal)
        heating, cooling, dhw = store.validated_arrays(result)
        shard.execute(
            "INSERT INTO results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,current_timestamp)",
            [ordinal, case_id, result.run_id, json.dumps(result.scalars), "{}", heating.tolist(), cooling.tolist(),
             dhw.tolist(), (heating * 0.9).tolist() if rc else None, cooling.tolist() if rc else None,
             json.dumps({"space_heating_demand_kwh": float(heating.sum() * 0.9), "cooling_demand_kwh": 0.0}) if rc else None,
             None, 12.5, 0.4 if rc else None],
        )
    shard.close()


def test_plan_shards_and_finalize_roundtrip(tmp_path, calendar_epw):
    root, catalog = frozen(tmp_path, calendar_epw)
    study = study_for(calendar_epw, tmp_path)
    receipt = store.create_plan(root, study, stub_cases(6, ["a_tmy"]), catalog, {"study_id": study.id})
    assert receipt["runs"] == 6 and receipt["pilot_runs"] == 2 and receipt["groups"] == 3
    assert receipt["buildings"] == 3 and receipt["group_sizes"] == {"2": 3} and receipt["weather_rows"] == 8760
    assert receipt["input_columns"] == {"infiltration_ach": "DOUBLE", "topology": "VARCHAR", "num_floors": "INTEGER"}
    with pytest.raises(FileExistsError):
        store.create_plan(root, study, stub_cases(6, ["a_tmy"]), catalog, {"study_id": study.id})
    plan = duckdb.connect(str(root / "plan.duckdb"), read_only=True)
    ordinals = plan.execute("SELECT ordinal,case_id FROM runs ORDER BY ordinal").fetchall()
    assert plan.execute("SELECT count(*) FROM tasks WHERE phase='pilot'").fetchone()[0] == 2
    assert [r[0] for r in store.task_rows(root, "pilot-0001")] == [1]
    plan.close()
    assert store.status(root)["remaining"] == 6

    write_shard(root, "pilot-0000", ordinals[:1], receipt["campaign_id"], rc=True)
    write_shard(root, "pilot-0001", ordinals[1:2], receipt["campaign_id"], rc=True)
    report = store.prepare_production_tasks(root, study, max_tasks=50)
    assert report["production_tasks"] == 1 and report["batch_size"] >= 4
    assert store.prepare_production_tasks(root, study, max_tasks=50) == report
    production = store.task_rows(root, "production-00000")
    assert sorted(r[0] for r in production) == [2, 3, 4, 5]
    with pytest.raises(ValueError, match="not exhaustive"):
        store.finalize(root, tmp_path / "out" / "partial.duckdb")
    write_shard(root, "production-00000", ordinals[2:], receipt["campaign_id"])
    assert store.status(root)["complete"] == 6

    destination = tmp_path / "handoff" / "store.duckdb"
    verification = store.finalize(root, destination, study_name="store")
    assert verification["training_rows"] == 6 and verification["rc_runs"] == 2
    assert destination.with_suffix(".sha256").read_text().split()[0] == verification["sha256"]
    assert not (root / "shards").exists() and not (root / "plan.duckdb").exists()
    out = duckdb.connect(str(destination), read_only=True)
    assert out.execute("SELECT count(*) FROM buildings").fetchone()[0] == 3
    assert out.execute("SELECT input_topology,input_num_floors FROM runs WHERE ordinal=0").fetchone() == ("S", 2)
    rc = out.execute("SELECT rc_space_heating_demand_kwh FROM scalars WHERE ordinal=0").fetchone()[0]
    assert rc == pytest.approx(0.5 * 8760 * 0.9)
    hourly = out.execute("SELECT heating_kwh, rc_heating_kwh FROM hourly WHERE ordinal=5 LIMIT 1").fetchone()
    assert hourly == (5.5, None)
    assert out.execute("SELECT count(*) FROM task_runtimes").fetchone()[0] == 3
    out.close()


def test_validated_arrays_reject_short_or_inconsistent_results():
    result = fake_result(0)
    store.validated_arrays(result)
    result.scalars["dhw_load_kwh"] += 100
    with pytest.raises(ValueError, match="disagreement"):
        store.validated_arrays(result)
    bad = fake_result(1)
    bad.hourly["cooling_demand_kwh"] = np.zeros(8759)
    with pytest.raises(ValueError, match="Invalid hourly"):
        store.validated_arrays(bad)


def test_foreign_shards_and_overlaps_are_rejected(tmp_path, calendar_epw):
    root, catalog = frozen(tmp_path, calendar_epw)
    study = study_for(calendar_epw, tmp_path)
    receipt = store.create_plan(root, study, stub_cases(2, ["a_tmy"]), catalog, {"study_id": study.id})
    write_shard(root, "pilot-0000", [(0, "c0")], "someone-else")
    with pytest.raises(ValueError, match="Foreign shard"):
        list(store.verified_shards(root, receipt["campaign_id"]))
    with pytest.raises(ValueError, match="identity mismatch"):
        store.open_shard(root / "shards" / "pilot-0000.duckdb", "pilot-0000", receipt["campaign_id"], {})
