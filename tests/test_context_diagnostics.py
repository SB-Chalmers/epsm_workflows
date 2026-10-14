"""Evidence gates and physical invariants for measured context."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from shapely.geometry import box
from epsm_core.specs.geometry import GeometrySpec
from epsm_workflows import context_diagnostics as study


def neighbour(identifier, poly, base=0.0, height=8.0):
    return dict(
        building_id=identifier,
        geometry_wkb_hex=poly.wkb.hex(),
        relative_base_m=base,
        height_m=height,
        outline="Fasad",
        function="Bostad;Flerfamiljshus",
        relative_eave_top_m=base + height,
    )


def test_overlapping_shades_union_without_double_area_and_conservative_height():
    records = [
        neighbour("a", box(0, 0, 10, 10), base=2, height=4),
        neighbour("b", box(8, 0, 18, 10), base=1, height=8),
    ]
    buildings, audit = study.shading_groups(records)
    assert len(buildings) == 1
    assert sum(f.area_m2 for f in buildings[0].footprints) == 180
    assert buildings[0].base_elevation_m == 1
    assert buildings[0].height_m == 8
    assert audit[0]["member_ids"] == ["a", "b"]
    assert buildings == study.shading_groups(records)[0]


def test_roof_edges_and_function_do_not_establish_heated_contact():
    geometry = GeometrySpec.from_footprint(
        study.footprint(box(0, 0, 10, 10)), storeys=2, storey_height_m=3
    )
    spec = SimpleNamespace(geometry=geometry)
    r = neighbour("b", box(10, 0, 20, 10))
    contacts = study.contact_evidence(spec, r, "Takkant")
    assert len(contacts) == 1 and contacts[0]["shared_line_m"] == 10
    assert not contacts[0]["verified_eligible"]
    assert "roof_edge_not_verified_wall_contact" in contacts[0]["reasons"]
    assert (
        "heated_neighbour_contact_not_independently_reviewed" in contacts[0]["reasons"]
    )
    r["heated_contact_reviewed"] = True
    assert study.contact_evidence(spec, r, "Fasad")[0]["verified_eligible"]


def test_partial_height_and_basement_contacts_are_not_silently_adiabatic():
    geometry = GeometrySpec.from_footprint(
        study.footprint(box(0, 0, 10, 10)), storeys=2, storey_height_m=3
    )
    spec = SimpleNamespace(
        geometry=geometry.updated(basement_depth_m=2.5, basement_conditioned=True)
    )
    r = neighbour("b", box(10, 0, 20, 10), height=3)
    r["heated_contact_reviewed"] = True
    c = study.contact_evidence(spec, r, "Fasad")[0]
    assert not c["verified_eligible"]
    assert "shared_basement_extent_unknown" in c["reasons"]
    assert "neighbour_does_not_cover_full_height_or_height_unknown" in c["reasons"]


@pytest.mark.parametrize("radius", [100, 200])
def test_actual_context_preserves_all_target_inputs(radius):
    root = Path(__file__).parents[1] / "output/context-diagnostics-mf-se02-2026-10-02"
    if not (root / "preparation.json").exists():
        pytest.skip("Local context preparation unavailable")
    rows = json.loads((root / "cohort.json").read_text())
    prep = json.loads((root / "preparation.json").read_text())
    weather = json.loads((root / "audit.json").read_text())["source_weather"]
    row = next(r for r in rows if r["building_id"].startswith("df7cdd8f"))
    baseline = study.oat.frozen_spec(row, "existing", weather, 0.2)
    spec = study.build_spec(
        row, "existing", weather, prep[row["building_id"]], f"shading_{radius}"
    )
    assert spec.context.buildings
    assert spec.geometry == baseline.geometry
    assert spec.envelope == baseline.envelope
    assert spec.facade == baseline.facade
    assert spec.airflow == baseline.airflow
    assert spec.use == baseline.use
    assert spec.climate == baseline.climate
    study.EnergyPlusRunner(
        "/Applications/OpenStudio-3.9.0/EnergyPlus/energyplus"
    ).compiler.compile(spec)
