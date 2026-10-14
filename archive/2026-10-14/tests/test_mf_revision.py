"""Scientific safeguards for the MF revision and quantitative evidence path."""

from copy import deepcopy

import pytest
from epsm_core import BuildingSpec, ClimateSpec, GeometrySpec, ScheduleSpec
from epsm_data import resolve_building_archetype

from epsm_workflows.mf_evidence import (
    apply_quantitative_evidence,
    evidence_locks,
    reviewed_evidence,
)
from epsm_workflows.mf_revision import (
    audit,
    assign_splits,
    exact_resistance,
    paired_comparison,
    promotion_gate,
    sample_properties,
    scenarios,
    zone_for_municipality,
)
from epsm_workflows.campaign import atomic_json


def spec():
    from epsm_core import FacadeSpec, AirflowSpec, load_sveby_spec
    from shapely.geometry import box

    native = resolve_building_archetype(
        category="multi_family",
        construction_year=1965,
        climate_zone="SE.Zone3",
        ventilation="F",
    )
    return BuildingSpec(
        identifier="b",
        geometry=GeometrySpec.from_footprint(
            box(0, 0, 20, 10), storey_elevations_m=(0, 3, 6)
        ),
        envelope=native["envelope"],
        facade=FacadeSpec(default_wwr=0.2, glazing=native["glazing"]),
        use=load_sveby_spec().use_spec(calendar_year=2023),
        airflow=AirflowSpec(
            topology="F", mechanical_vent_l_s_m2=0.35, **native["core_airflow_inputs"]
        ),
        climate=ClimateSpec(
            epw_path="unused.epw",
            weather_sha256="0" * 64,
            latitude_deg=57,
            longitude_deg=12,
            elevation_m=0,
            time_zone_hours=1,
            calendar_year=2023,
        ),
        load_reference_area_m2=350,
    )


def observation(role="window", **updates):
    item = {
        "source": "Independent component survey",
        "classification": "measured",
        "valid_from_year": 2020,
        "valid_through_year": 2024,
    }
    if role == "window":
        item.update(definition="whole_window_including_frame", u_value_w_m2k=1.5)
    elif role == "operating_flow":
        item.update(quantity="operating_outdoor_air", area_basis="Atemp", l_s_m2=0.4)
    else:
        item.update(
            boundary="nominal_assembly_including_surface_films", u_value_w_m2k=0.2
        )
    item.update(updates)
    return {"measurement_year": 2023, "ventilation": "F", "mf_evidence": {role: item}}


def test_window_evidence_changes_only_window_and_locks_parameters():
    original = spec()
    changed, audit = apply_quantitative_evidence(original, observation())
    assert changed.facade.glazing.u_value_w_m2k == 1.5
    assert changed.envelope == original.envelope and changed.airflow == original.airflow
    assert changed.geometry == original.geometry and changed.use == original.use
    assert audit[0]["before"]["u_value_w_m2k"] == 2.2
    assert evidence_locks(observation()) == {"window_u_multiplier", "shgc_multiplier"}


@pytest.mark.parametrize(
    "updates",
    [
        {"valid_from_year": 2023},
        {"definition": "glass_only"},
        {"source": ""},
        {"u_value_w_m2k": float("nan")},
        {"classification": "checkbox"},
    ],
)
def test_insufficient_window_evidence_is_rejected(updates):
    with pytest.raises(ValueError):
        reviewed_evidence(observation(**updates))


def test_checkboxes_do_not_assign_component_values():
    original = spec()
    changed, audit = apply_quantitative_evidence(
        original, {"measurement_year": 2023, "reno_window": True}
    )
    assert changed == original and audit == []


def test_opaque_u_preserves_materials_and_non_insulation_layers():
    original = spec()
    changed, _ = apply_quantitative_evidence(original, observation("wall"))
    assert changed.envelope.wall.u_value_w_m2k == pytest.approx(0.2)
    assert changed.envelope.top == original.envelope.top
    assert [x.material for x in changed.envelope.wall.layers] == [
        x.material for x in original.envelope.wall.layers
    ]
    assert changed.load_reference_area_m2 == original.load_reference_area_m2


def test_operating_flow_units_are_explicit_and_natural_flow_is_not_mechanical():
    changed, _ = apply_quantitative_evidence(spec(), observation("operating_flow"))
    assert changed.airflow.mechanical_vent_l_s_m2 == 0.4
    observed_zero, _ = apply_quantitative_evidence(
        spec(), observation("operating_flow", l_s_m2=0)
    )
    assert observed_zero.airflow.mechanical_vent_l_s_m2 == 0
    with pytest.raises(ValueError):
        reviewed_evidence(observation("operating_flow", area_basis="floor_gross"))
    row = observation("operating_flow")
    row["ventilation"] = "S"
    with pytest.raises(ValueError):
        reviewed_evidence(row)


def test_quantitative_flow_locks_separate_cases_without_precomputed_locks():
    cases = scenarios(spec(), observation("operating_flow"))
    assert not any(name.startswith("flow_") for name in cases)


def test_zero_flow_completion_is_explicit_and_preserves_other_inputs():
    from epsm_workflows.cohort_transfer.preparation import mechanical_flow_completion

    assert mechanical_flow_completion("multi_family", 0, 0.35) == (
        0,
        "reported_design_completion_assumption",
    )
    assert mechanical_flow_completion(
        "multi_family", 0, 0.35, zero_policy="normal_use"
    ) == (0.35, "reported_zero_normal_use_completion_assumption")
    assert (
        mechanical_flow_completion(
            "multi_family", 0.48, 0.35, zero_policy="normal_use"
        )[0]
        == 0.48
    )
    assert (
        mechanical_flow_completion("office", 0, 2.0, zero_policy="normal_use")[0] == 0
    )
    with pytest.raises(ValueError):
        mechanical_flow_completion("multi_family", 0, 0.35, zero_policy="silent_clip")


def test_low_conductivity_structural_layer_is_not_insulation():
    from epsm_core import MaterialSpec, LayerSpec, ConstructionSpec

    wall = ConstructionSpec(
        layers=(
            LayerSpec(
                material=MaterialSpec(
                    name="timber",
                    conductivity_w_mk=0.12,
                    density_kg_m3=500,
                    specific_heat_j_kgk=1600,
                ),
                thickness_m=0.2,
            ),
        ),
    )
    with pytest.raises(ValueError, match="No insulation layer"):
        exact_resistance(wall, 1.2)


def test_total_resistance_change_is_exact_and_materials_are_preserved():
    original = spec().envelope.wall
    changed, audit = exact_resistance(original, 1.4)
    assert 1 / changed.u_value_w_m2k == pytest.approx(1.4 / original.u_value_w_m2k)
    assert audit["added_depth_mm"] > 0
    assert [x.material for x in changed.layers] == [x.material for x in original.layers]


def rows(n=50):
    return [
        {
            "building_id": f"b{i}",
            "property_key": f"p{i // 2}",
            "era": "SE.02",
            "ventilation": "F",
            "catalogue_climate_zone": "SE.Zone3",
            "storeys": 3,
            "heated_basement_floors": 0,
        }
        for i in range(n)
    ]


def test_sampling_is_target_free_deterministic_and_property_unique():
    source = rows()
    a = sample_properties(source, 12)
    b = sample_properties(
        [r | {"target": 9999, "residual": -999} for r in reversed(source)], 12
    )
    assert [r["building_id"] for r in a] == [r["building_id"] for r in b]
    assert len({r["property_key"] for r in a}) == 12
    assigned = assign_splits(source, {"p0", "p1"})
    assert all(
        r["split"] == "train" for r in assigned if r["property_key"] in {"p0", "p1"}
    )
    assert all(
        len({r["split"] for r in assigned if r["property_key"] == p}) == 1
        for p in {r["property_key"] for r in assigned}
    )


def test_small_screen_covers_all_zones_before_repeating_a_parent():
    source = []
    for zone in ("SE.Zone1", "SE.Zone2", "SE.Zone3"):
        for i in range(20):
            source.append(
                {
                    "building_id": f"{zone}-{i}",
                    "property_key": f"{zone}-{i}",
                    "era": "SE.02",
                    "ventilation": "F",
                    "catalogue_climate_zone": zone,
                    "heated_basement_floors": i % 2,
                    "storeys": 1 + i % 8,
                    "renovation_timing": str(i),
                    "roof_form": "flat",
                }
            )
    selected = sample_properties(source, 12)
    assert all(
        sum(r["catalogue_climate_zone"] == zone for r in selected) == 4
        for zone in ("SE.Zone1", "SE.Zone2", "SE.Zone3")
    )


def test_historical_zone_is_not_northern_county_shortcut():
    assert zone_for_municipality("2281") == "SE.Zone2"
    assert zone_for_municipality("2380") == "SE.Zone1"
    assert zone_for_municipality("1480") == "SE.Zone3"


def test_separate_scenarios_keep_geometry_area_and_component_locks():
    original = spec().updated(
        geometry=spec().geometry.updated(
            basement_conditioned=True, basement_depth_m=2.5
        )
    )
    cases = scenarios(original, {"locked_parameters": ["wall_resistance"]})
    assert not any(name.startswith("wall_") for name in cases)
    basement = cases["basement_18.0"][0]
    assert (
        basement.geometry == original.geometry and basement.airflow == original.airflow
    )
    assert basement.use == original.use
    assert basement.zone_uses[0].use.heating_setpoint == ScheduleSpec.constant(18)
    assert all(
        s.load_reference_area_m2 == original.load_reference_area_m2
        for s, _ in cases.values()
    )


def test_uninsulated_old_wall_does_not_stop_other_physics_scenarios():
    old = resolve_building_archetype(
        category="multi_family",
        construction_year=1940,
        climate_zone="SE.Zone3",
        ventilation="F",
    )
    original = spec().updated(envelope=old["envelope"])
    assert not any(
        layer.material.conductivity_w_mk <= 0.2
        for layer in original.envelope.wall.layers
    )
    cases = scenarios(original, {})
    assert not any(name.startswith("wall_") for name in cases)
    assert "flow_0.8" in cases and "top_r20" in cases
    assert all(s.envelope.wall == original.envelope.wall for s, _ in cases.values())


def test_failures_stay_in_denominator_and_retrospective_cannot_promote():
    panel = [r | {"target": 100.0} for r in rows(6)]
    comparison = paired_comparison(
        panel, [130.0] * 6, [100.0, None, 100.0, 100.0, 100.0, 100.0], resamples=20
    )
    assert comparison["candidate"]["coverage20"] == pytest.approx(5 / 6)
    assert comparison["candidate"]["failed"] == 1
    gate = promotion_gate(
        comparison,
        independent_physical_evidence=True,
        untouched=False,
        split_counts={"train": 90, "validation": 30, "test": 30},
    )
    assert not gate["promote"]
    assert "retrospective_or_previously_opened_targets" in gate["reasons"]
    assert "incomplete_paired_simulation_panel" in gate["reasons"]


def test_supported_gain_can_promote_but_subgroup_regression_blocks_it():
    panel = [r | {"target": 100.0} for r in rows(80)]
    comparison = paired_comparison(panel, [130.0] * 80, [100.0] * 80, resamples=20)
    args = dict(
        independent_physical_evidence=True,
        untouched=True,
        split_counts={"train": 90, "validation": 30, "test": 30},
    )
    assert promotion_gate(comparison, **args)["promote"]
    subgroup = deepcopy(comparison)
    subgroup["candidate"]["coverage20"] = 0
    subgroup["baseline"]["coverage20"] = 1
    assert not promotion_gate(comparison, subgroup_comparisons=[subgroup], **args)[
        "promote"
    ]


def test_real_matrix_inventory_quarantines_geometry_and_never_reads_targets(tmp_path):
    import pandas as pd
    from epsm_workflows.mf_revision import _guard

    source = rows(12)
    for row in source:
        row.update(
            formular_id=int(row["building_id"][1:]),
            construction_year=1965,
            municipality="1480",
            height_status="determined",
            geometry_status="valid",
            heating_family="district_heat",
            correction_status="normal_use_adjusted_pre_weather_correction",
        )
    pd.DataFrame(source).to_parquet(tmp_path / "stock.parquet")
    pd.DataFrame(
        [
            {
                "building_id": r["building_id"],
                "formular_id": r["formular_id"],
                "eligible": True,
                "assigned_archetype_use": "multi_family",
            }
            for r in source
        ]
    ).to_parquet(tmp_path / "membership.parquet")
    pd.DataFrame([{"building_id": "b0"}]).to_parquet(tmp_path / "quarantine.parquet")
    atomic_json(
        tmp_path / "config.json",
        {
            "stock_features": "stock.parquet",
            "membership_audit": "membership.parquet",
            "height_quarantine": "quarantine.parquet",
            "previously_examined_features": ["stock.parquet"],
            "initial_properties": 6,
        },
    )
    output = tmp_path / "study"
    result = audit(tmp_path / "config.json", output)
    assert result["split_counts"] == {"train": 6}
    register = __import__("json").loads((output / "decision-register.json").read_text())
    assert len(register["slots"]) == 216
    assert len(register["canonical_states"]) == 18
    assert register["energy_targets_read"] is False
    assert "b0" not in {
        r["building_id"]
        for r in __import__("json").loads((output / "cohort.json").read_text())
    }
    _guard(output)
    (output / "cohort.json").write_text("[]")
    with pytest.raises(ValueError, match="Frozen design changed"):
        _guard(output)
