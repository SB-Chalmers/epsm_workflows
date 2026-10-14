"""Physical invariants and opt-in semantics for shared MF calibration controls."""

from types import SimpleNamespace

import pytest
from epsm_core import (
    AirflowSpec,
    BuildingSpec,
    ClimateSpec,
    ConstructionSpec,
    FacadeSpec,
    GeometrySpec,
    LayerSpec,
    MaterialSpec,
    load_sveby_spec,
)
from epsm_data import resolve_building_archetype
from epsm_data.cohorts import apply_cohort_profile
from shapely.geometry import box

from epsm_workflows.campaign import atomic_json
from epsm_workflows.cohort_transfer.parameters import applicable
from epsm_workflows.cohort_transfer.simulation import Evaluator
from epsm_workflows.mf_physics import (
    TOTAL_ASSEMBLY_R,
    add_insulation_overlay,
    apply_physical_profile,
    exact_resistance,
    independent_controls,
    prior_distance,
    select_candidate,
)


def material(name, conductivity, density):
    return MaterialSpec(
        name=name,
        conductivity_w_mk=conductivity,
        density_kg_m3=density,
        specific_heat_j_kgk=1000,
    )


def assembly(insulated=True):
    layers = (LayerSpec(material=material("concrete", 1.7, 2300), thickness_m=0.2),)
    if insulated:
        layers += (LayerSpec(material=material("wool", 0.04, 35), thickness_m=0.1),)
    return ConstructionSpec(layers=layers, surface_resistance_m2k_w=0.23)


@pytest.fixture
def spec():
    resolved = resolve_building_archetype(
        category="multi_family", construction_year=1965,
        climate_zone="SE.Zone3", ventilation="F",
    )
    return BuildingSpec(
        identifier="physics-test",
        geometry=GeometrySpec.from_footprint(box(0, 0, 20, 10), storey_elevations_m=(0, 3, 6)),
        envelope=resolved["envelope"],
        facade=FacadeSpec(default_wwr=0.2, glazing=resolved["glazing"]),
        use=load_sveby_spec().use_spec(calendar_year=2023),
        airflow=AirflowSpec(
            topology="F", mechanical_vent_l_s_m2=0.35, **resolved["core_airflow_inputs"]
        ),
        climate=ClimateSpec(
            epw_path="unused.epw", weather_sha256="0" * 64, latitude_deg=57,
            longitude_deg=12, elevation_m=0, time_zone_hours=1, calendar_year=2023,
        ),
        load_reference_area_m2=350,
    )


def profile(parameters):
    return {
        "infiltration_basis": "atemp_area_rate",
        "parameters": parameters,
        "bounds": {name: [0.5, 2] for name in parameters},
        "source": "Frozen development scenario, not observed building condition",
    }


@pytest.mark.parametrize("factor", [0.8, 1, 1.4])
def test_total_r_including_films_preserves_structural_layers_and_materials(factor):
    original = assembly()
    changed, audit = exact_resistance(original, factor)
    assert 1 / changed.u_value_w_m2k == pytest.approx(factor / original.u_value_w_m2k)
    assert changed.layers[0] == original.layers[0]
    assert [layer.material for layer in changed.layers] == [layer.material for layer in original.layers]
    assert changed.surface_resistance_m2k_w == original.surface_resistance_m2k_w
    assert audit["resistance_basis"] == TOTAL_ASSEMBLY_R


def test_impossible_decrease_and_uninsulated_wall_never_scale_structure():
    original = assembly()
    with pytest.raises(ValueError, match="retained non-insulation"):
        exact_resistance(original, 0.01)
    bare = assembly(insulated=False)
    with pytest.raises(ValueError, match="No insulation layer"):
        exact_resistance(bare, 1.4)
    assert bare.layers[0].thickness_m == 0.2


@pytest.mark.parametrize("factor", [0, -1, float("nan"), float("inf"), True])
def test_resistance_factor_rejects_invalid_values(factor):
    with pytest.raises(ValueError, match="finite and positive"):
        exact_resistance(assembly(), factor)


def test_profile_default_is_legacy_and_opt_in_reaches_total_r(spec):
    declared = profile({"wall_resistance": 1.4, "infiltration_multiplier": 0.8})
    legacy = apply_physical_profile(spec, declared)
    assert legacy == apply_cohort_profile(spec, declared)
    exact = apply_physical_profile(spec, declared, resistance_basis=TOTAL_ASSEMBLY_R)
    assert 1 / exact.envelope.wall.u_value_w_m2k == pytest.approx(1.4 / spec.envelope.wall.u_value_w_m2k)
    assert 1 / legacy.envelope.wall.u_value_w_m2k != pytest.approx(1.4 / spec.envelope.wall.u_value_w_m2k)
    assert exact.geometry == spec.geometry and exact.use == spec.use and exact.climate == spec.climate
    assert exact.airflow.envelope_infiltration_l_s_m2 == pytest.approx(
        spec.airflow.envelope_infiltration_l_s_m2 * 0.8
    )
    assert exact.airflow.envelope_infiltration_ach == spec.airflow.envelope_infiltration_ach


def test_evidence_locks_and_frozen_bounds_apply_before_exact_adapter(spec):
    locked = apply_physical_profile(
        spec, profile({"wall_resistance": 1.4}),
        locked_parameters=["wall_resistance"], resistance_basis=TOTAL_ASSEMBLY_R,
    )
    assert locked == spec
    with pytest.raises(ValueError, match="outside frozen bounds"):
        apply_physical_profile(spec, profile({"wall_resistance": 4}), resistance_basis=TOTAL_ASSEMBLY_R)
    with pytest.raises(ValueError, match="Conflicting resistance"):
        apply_physical_profile(
            spec, dict(profile({}), resistance_basis="legacy"), resistance_basis=TOTAL_ASSEMBLY_R
        )


def test_absent_opaque_insulation_is_inapplicable_only_for_opt_in(spec):
    bare = spec.updated(envelope=spec.envelope.updated(wall=assembly(False), top=assembly(False)))
    assert applicable(bare)["wall_resistance"]["status"] == "screen_and_cohort_calibration"
    register = applicable(bare, resistance_basis=TOTAL_ASSEMBLY_R)
    for name in ("wall_resistance", "roof_resistance"):
        assert register[name]["status"] == "not_applicable"
        assert register[name]["reason"] == "no_reviewed_insulation_layer_sourced_overlay_required"
    assert apply_physical_profile(bare, profile({"wall_resistance": 1}), resistance_basis=TOTAL_ASSEMBLY_R) == bare


def test_sourced_overlay_preserves_original_assembly_and_adds_declared_r():
    original = assembly(False)
    wool = material("sourced mineral wool", 0.04, 35)
    changed, audit = add_insulation_overlay(
        original, material=wool, thickness_m=0.08,
        source="Reviewed assembly source page 42", installation_year=1995,
    )
    assert changed.layers[1:] == original.layers
    assert changed.surface_resistance_m2k_w == original.surface_resistance_m2k_w
    assert 1 / changed.u_value_w_m2k == pytest.approx(1 / original.u_value_w_m2k + 2)
    assert audit["installation_year"] == 1995 and audit["original_layers_preserved"]
    with pytest.raises(ValueError, match="source provenance"):
        add_insulation_overlay(original, material=wool, thickness_m=0.08, source="")
    with pytest.raises(ValueError, match="low-density insulation"):
        add_insulation_overlay(original, material=material("timber", 0.12, 500), thickness_m=0.08, source="test")


def candidate(name, coverage, mae, bias=0, distance=0):
    return {
        "id": name,
        "metrics": {"coverage20": coverage, "mae_kwh_m2": mae, "bias_kwh_m2": bias},
        "prior_distance": distance,
    }


def test_coverage_selection_rejects_mae_deterioration_and_preserves_baseline_ties():
    baseline = candidate("baseline", 0.5, 20, 5)
    wrong = candidate("high-coverage-worse-mae", 0.8, 21)
    good = candidate("improvement", 0.6, 19)
    assert select_candidate([wrong, good], baseline) is good
    assert select_candidate([wrong], baseline) is baseline
    duplicate = candidate("aaa", 0.5, 20, 5)
    assert select_candidate([duplicate], baseline) is baseline
    near_prior = candidate("near-prior", 0.6, 19, 1, 0.1)
    far_prior = candidate("far-prior", 0.6, 19, 1, 0.5)
    assert select_candidate([far_prior, near_prior], baseline) is near_prior


def test_prior_distance_uses_frozen_bounds_not_physical_units():
    register = {
        "wall_resistance": {"baseline": 1, "bounds": [1, 1.4]},
        "basement_setpoint_c": {"baseline": 21, "bounds": [11, 21]},
    }
    assert prior_distance({"wall_resistance": 1.2, "basement_setpoint_c": 16}, register) == pytest.approx(0.5)


def test_uncentred_rank_retains_conditional_control_and_records_confounded_one():
    response = {
        "flow": [2, 2, 1, 1, 2, 1],
        "basement": [1, 1, 0, 0, 1, 0],
        "duplicate_flow": [4, 4, 2, 2, 4, 2],
        "zero": [0] * 6,
    }
    result = independent_controls(response, effects={"flow": 10, "basement": 5, "duplicate_flow": 1})
    assert result["active"] == ["flow", "basement"]
    assert result["confounding"][0]["parameter"] == "duplicate_flow"
    assert result["confounding"][0]["relative_min_singular_value"] <= 0.05
    assert result["excluded"][0]["parameter"] == "zero"


def test_evaluator_runs_opt_in_adapter_and_uses_inapplicable_receipt_identity(tmp_path, spec, monkeypatch):
    config = {"workers": 1, "engine": "test", "resistance_basis": TOTAL_ASSEMBLY_R}
    register = applicable(spec, resistance_basis=TOTAL_ASSEMBLY_R)
    atomic_json(tmp_path / "config.json", config)
    atomic_json(tmp_path / "manifest.json", {"identity": "test"})
    atomic_json(tmp_path / "parameter-register.json", {spec.identifier: register})
    atomic_json(tmp_path / "source.json", spec.model_dump(mode="json"))
    seen = []

    class Runner:
        def __init__(self, **kwargs):
            pass

        def run(self, updated, output, **kwargs):
            seen.append(updated)
            return SimpleNamespace(
                scalars={"space_heating_carrier_input_kwh": 3500, "space_heating_demand_kwh": 3500},
                diagnostics=[],
            )

    monkeypatch.setattr("epsm_workflows.cohort_transfer.simulation.EnergyPlusRunner", Runner)
    evaluator = Evaluator(tmp_path)
    row = {"building_id": spec.identifier, "status": "prepared", "spec_path": "source.json", "atemp_m2": 350}
    assert evaluator.evaluate(row, {"wall_resistance": 1.2}) == 10
    assert 1 / seen[0].envelope.wall.u_value_w_m2k == pytest.approx(1.2 / spec.envelope.wall.u_value_w_m2k)
    assert evaluator.case_key(row, {}) == evaluator.case_key(row, {"basement_setpoint_c": 15})


def test_explicit_overlay_control_is_conditional_sourced_and_receipted(tmp_path, spec, monkeypatch):
    bare = spec.updated(envelope=spec.envelope.updated(wall=assembly(False)))
    definition = {
        "material": material("reviewed mineral wool", 0.04, 35).model_dump(mode="json"),
        "source": "EPSM Data reviewed construction material; engineering overlay scenario",
    }
    assert applicable(bare)["wall_insulation_overlay_m"]["status"] == "not_applicable"
    assert applicable(spec, wall_overlay=definition)["wall_insulation_overlay_m"]["status"] == "not_applicable"
    register = applicable(bare, resistance_basis=TOTAL_ASSEMBLY_R, wall_overlay=definition)
    assert register["wall_insulation_overlay_m"]["status"] == "screen_and_cohort_calibration"
    assert applicable(bare, wall_overlay=definition, locked=["wall_resistance"])["wall_insulation_overlay_m"]["status"] == "independent_input_locked"
    atomic_json(tmp_path / "config.json", {
        "workers": 1, "engine": "test", "resistance_basis": TOTAL_ASSEMBLY_R,
        "wall_overlay": definition,
    })
    atomic_json(tmp_path / "manifest.json", {"identity": "overlay-test"})
    atomic_json(tmp_path / "parameter-register.json", {bare.identifier: register})
    atomic_json(tmp_path / "source.json", bare.model_dump(mode="json"))
    seen = []

    class Runner:
        def __init__(self, **kwargs):
            pass

        def run(self, updated, output, **kwargs):
            seen.append(updated)
            return SimpleNamespace(
                scalars={"space_heating_carrier_input_kwh": 3500, "space_heating_demand_kwh": 3500},
                diagnostics=[],
            )

    monkeypatch.setattr("epsm_workflows.cohort_transfer.simulation.EnergyPlusRunner", Runner)
    evaluator = Evaluator(tmp_path)
    row = {"building_id": bare.identifier, "status": "prepared", "spec_path": "source.json", "atemp_m2": 350}
    settings = {"wall_insulation_overlay_m": 0.08}
    assert evaluator.evaluate(row, settings) == 10
    assert seen[0].envelope.wall.layers[1:] == bare.envelope.wall.layers
    assert seen[0].envelope.wall.layers[0].thickness_m == 0.08
    receipt = evaluator.results[evaluator.case_key(row, settings)]
    assert receipt["wall_overlay_audit"]["installation_year"] is None
    locked = row | {"locked_parameters": ["wall_resistance"]}
    assert evaluator.effective_parameters(locked, settings) == {}


def test_exported_overlay_profile_is_self_contained_and_respects_wall_evidence(spec):
    bare = spec.updated(envelope=spec.envelope.updated(wall=assembly(False)))
    exported = {
        "source": "Shared surviving-stock parent; engineering assumption",
        "resistance_basis": TOTAL_ASSEMBLY_R,
        "infiltration_basis": "atemp_area_rate",
        "parameters": {"wall_insulation_overlay_m": 0.08, "infiltration_multiplier": 0.8},
        "bounds": {"wall_insulation_overlay_m": [0, 0.15], "infiltration_multiplier": [0.5, 2]},
        "wall_overlay": {
            "source": "Reviewed EPSM Data original library mineral wool",
            "material": material("reviewed mineral wool", 0.04, 35).model_dump(mode="json"),
        },
    }
    changed = apply_physical_profile(bare, exported)
    assert changed.envelope.wall.layers[1:] == bare.envelope.wall.layers
    assert changed.envelope.wall.layers[0].thickness_m == 0.08
    assert 1 / changed.envelope.wall.u_value_w_m2k == pytest.approx(1 / bare.envelope.wall.u_value_w_m2k + 2)
    assert changed.airflow.envelope_infiltration_l_s_m2 == pytest.approx(bare.airflow.envelope_infiltration_l_s_m2 * 0.8)
    locked = apply_physical_profile(bare, exported, locked_parameters=["wall_resistance"])
    assert locked.envelope.wall == bare.envelope.wall
    assert locked.airflow == changed.airflow
    with pytest.raises(ValueError, match="sourced material definition"):
        apply_physical_profile(bare, exported | {"wall_overlay": {"source": "missing material"}})
    with pytest.raises(ValueError, match="outside frozen bounds"):
        apply_physical_profile(bare, exported | {"parameters": {"wall_insulation_overlay_m": 0.2}})
