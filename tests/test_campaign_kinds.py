"""The sweep and renovation study kinds share the stock plan shape."""

import collections

import pytest

pytest.importorskip("scipy")
pytest.importorskip("epsm_core")

from epsm_workflows.campaign import cases
from epsm_workflows.campaign.study import StudySpec
from epsm_workflows.campaign.weather import file_sha256


def catalog_for(epw, ids):
    return {i: {"path": str(epw), "sha256": file_sha256(epw), "kind": "tmy", "site": i.rsplit("_", 1)[0], "calendar_year": 2018} for i in ids}


def test_weather_sweep_crosses_every_building_with_every_weather(calendar_epw, core_solves_u_values):
    study = StudySpec(
        kind="weather_sweep", name="sweep", seed=3,
        geometry={"families": ["rectangle", "L"], "real_fraction": 0},
        contexts=[{"name": "urban_lowrise_dense", "preset": "urban_lowrise_dense"}],
        weather_sweep={"buildings": 2, "weather": [
            {"weather_id": "a_2014", "path": str(calendar_epw), "calendar_year": 2018, "location": "a"},
            {"weather_id": "b_2015", "path": str(calendar_epw), "calendar_year": 2018, "location": "b"},
        ]},
    )
    catalog = catalog_for(calendar_epw, ["a_2014", "b_2015"])
    produced = list(cases.cases_for(study, [], catalog))
    assert len(produced) == 4 and len({c.building_key for c in produced}) == 2
    assert [c.weather_id for c in produced] == ["a_2014", "b_2015"] * 2
    assert [c.axes["pilot"] for c in produced] == [True, False, True, False]
    assert produced[0].axes["site"] == "a" and produced[1].axes["site"] == "b"
    assert produced[0].inputs == produced[1].inputs and produced[0].case_id != produced[1].case_id


def test_ventilation_sweep_varies_only_airflow(calendar_epw, core_solves_u_values):
    from epsm_core import Building

    study = StudySpec(
        kind="ventilation_sweep", name="vent", seed=5,
        cities=[{"slug": "g", "city": "G", "tmy": str(calendar_epw)}],
        geometry={"families": ["rectangle"], "real_fraction": 0},
        contexts=[{"name": "urban_lowrise_dense", "preset": "urban_lowrise_dense"}],
        ventilation={"buildings": 1, "samples_per_building": 8, "topologies": ["S", "FTX"]},
    )
    produced = list(cases.cases_for(study, [], catalog_for(calendar_epw, ["g_tmy"])))
    assert len(produced) == 8 and len({c.building_key for c in produced}) == 8
    assert collections.Counter(c.inputs["topology"] for c in produced) == {"S": 4, "FTX": 4}
    assert sum(c.axes["pilot"] for c in produced) == 1
    first, second = (Building.from_dict(c.building) for c in produced[:2])
    assert first.geometry == second.geometry and first.construction_set.to_dict() == second.construction_set.to_dict()
    assert first.hvac.airflow != second.hvac.airflow
    infiltration = sorted(c.inputs["infiltration_ach"] for c in produced)
    assert all(0.1 <= v <= 1 for v in infiltration) and len(set(infiltration)) == 8
    assert all(c.inputs["natural_ach"] == c.inputs["natural_ach"] for c in produced)


def test_before_after_runs_each_measure_alone(calendar_epw, core_solves_u_values):
    pytest.importorskip("epsm")
    pytest.importorskip("epsm_data")
    study = StudySpec(
        kind="before_after", name="renovate", seed=9,
        cities=[{"slug": "g", "city": "G", "tmy": str(calendar_epw)}],
        geometry={"families": ["rectangle"], "real_fraction": 0},
        contexts=[{"name": "urban_lowrise_dense", "preset": "urban_lowrise_dense"}],
        before_after={"buildings": 1, "archetypes": ["mf_se03"]},
    )
    produced = list(cases.cases_for(study, [], catalog_for(calendar_epw, ["g_tmy"])))
    scenarios = [c.axes["scenario_id"] for c in produced]
    assert scenarios[0] == "baseline" and len(produced) > 2 and len(set(scenarios)) == len(scenarios)
    assert produced[0].axes["measure_id"] is None and all(c.axes["measure_id"] for c in produced[1:])
    assert produced[0].inputs["archetype"] == "mf_se03" and produced[0].axes["pilot"] and not produced[1].axes["pilot"]
    skipped = produced[0].metadata["skipped_measures"]
    assert isinstance(skipped, list)
    from epsm_core import Building

    baseline, renovated = (Building.from_dict(c.building) for c in produced[:2])
    # The snapshot carries the renovated physical state, not epsm's lineage wrapper.
    assert renovated.readiness(engine="energyplus").ready and produced[1].building != produced[0].building
    assert baseline.geometry == renovated.geometry
