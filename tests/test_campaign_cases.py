"""Stock designs become portable, engine-ready Core Buildings."""

import json

import pytest

pytest.importorskip("scipy")
epsm_core = pytest.importorskip("epsm_core")

from epsm_workflows.campaign import cases
from epsm_workflows.campaign.sampling import stock_designs
from epsm_workflows.campaign.study import StudySpec


def study_for(epw):
    return StudySpec(
        kind="stock_diversity", name="smoke", seed=42,
        cities=[{"slug": "malmo", "city": "Malmö", "tmy": str(epw)}],
        geometry={"families": ["rectangle", "L", "U", "courtyard"], "real_fraction": 0.0},
        stock={"diversity_designs": 8, "anchor_designs": 0},
    )


def test_every_family_yields_ready_buildings_with_solved_envelope(calendar_epw, core_solves_u_values):
    from epsm_core import Building, Weather, preflight_energyplus, thermal_construction

    study = study_for(calendar_epw)
    catalog = {"malmo_tmy": {"path": str(calendar_epw), "sha256": "x", "kind": "tmy", "site": "malmo", "calendar_year": 2018}}
    grouped = cases.weather_by_site(study, catalog)
    weather = Weather.from_epw(calendar_epw, calendar_year=2018)
    seen = set()
    for design in stock_designs(study, []):
        polygon = cases.design_polygon(design, [])
        produced = list(cases.physical_cases(study, design, polygon, grouped, cohort_axes={"study_kind": study.kind}))
        assert len(produced) == len(study.contexts)
        assert len({c.building_key for c in produced}) == len(produced)
        case = produced[0]
        assert case.axes["context"] == "urban_lowrise_sparse" and case.weather_id == "malmo_tmy"
        building = Building.from_dict(json.loads(json.dumps(case.building)))
        assert building.to_dict() == case.building
        building.identifier = case.axes["sample_id"]
        assert building.readiness(engine="energyplus").ready
        assert building.readiness(engine="epsm_rc").ready
        assert preflight_energyplus(building.to_spec(weather)).ready
        wall = next(e for e in building.elements if e.role.value == "EXT_WALL")
        assert thermal_construction(wall.construction).u_value_w_m2k == pytest.approx(case.inputs["wall_u"], rel=1e-6)
        concrete = wall.construction.components[0].layers[2].layer
        assert concrete.thickness == pytest.approx(case.inputs["wall_concrete_m"])
        assert building.geometry.orientation_deg == 0 and building.context.buildings
        assert case.complexity > 0 and case.case_id != produced[1].case_id
        seen.add(design["family"])
    assert seen == {"rectangle", "L", "U", "courtyard"}


def test_facade_wwr_follows_cardinal_direction_and_tiny_facades_stay_opaque():
    from epsm_core import Footprint, GeometrySpec

    geometry = GeometrySpec.from_footprint(Footprint.rectangle(20, 10), storeys=2, storey_height_m=3)
    p = {"wwr_north": 0.1, "wwr_east": 0.2, "wwr_south": 0.3, "wwr_west": 0.4, "window_u": 1.2, "shgc": 0.5}
    facade, opaque = cases.facade_for(p, geometry)
    assert {round(o.wwr, 2) for o in facade.overrides} == {0.1, 0.2, 0.3, 0.4}
    assert opaque == []
    assert cases.effective_scaled_wwr(0.02, 0.2) == 0.0 and cases.effective_scaled_wwr(5, 0.2) == 0.2


def test_schedules_shift_daily_profiles_only():
    constant = cases.schedules("constant", 2)["occupancy_fraction"].values
    daily = cases.schedules("daily", 0)["occupancy_fraction"].values
    shifted = cases.schedules("daily", 2)["occupancy_fraction"].values
    assert set(constant) == {1.0} and len(daily) == 24
    assert shifted[2:] == daily[:22] and shifted[:2] == daily[22:]
