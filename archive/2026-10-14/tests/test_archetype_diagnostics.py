"""Meaningful boundary and experiment-isolation checks for physics diagnostics."""

import copy
import json
from pathlib import Path

import pytest

from epsm_workflows import archetype_diagnostics as diag


def raw(**changes):
    return {
        "Version": "2.8",
        "EgiVersion": "2020",
        "EgenByggnadsKat": "Flerbostadshus",
        "EgenNybyggAr": "1970",
        "EgenAtemp": "1000",
        "EgenAntalPlan": "3",
        "EgenAntalKallarplan": "1",
        "EgiBeraknatVarde": "Nej",
        "EgiForstaArManad": "1801",
        "EgiSistaArManad": "1812",
        "EgiFjarrvarmeUPPV": "100000",
        "EgiFjarrvarmeVV": "25000",
        "VentTypF": "Ja",
    } | changes


@pytest.mark.parametrize("version", ["2.6", "2.7", "2.8", "3.0"])
def test_epc_version_energy_weather_and_basement_semantics(version):
    fields = diag.parse_fields(raw(Version=version))
    assert fields["normal_use_adjusted"]
    assert not fields["normal_year_corrected"]
    assert fields["heated_basement_floors"] == 1
    assert fields["storeys"] == 3  # excludes the reported basement
    assert fields["measurement_year"] == 2018
    assert fields["basement_definition"] == "heated_above_10C"


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"Version": "9.0"}, "unverified_form_version"),
        ({"EgenAntalKallarplan": ""}, "unknown_heated_basement_count"),
        ({"EgenAntalKallarplan": "2"}, "multiple_basement"),
        ({"EgiBeraknatVarde": "Ja"}, "calculated_or_unknown"),
        ({"EgiForstaArManad": "1901"}, "invalid_measurement_period"),
    ],
)
def test_unverified_or_conflicting_inputs_fail_closed(changes, reason):
    with pytest.raises(ValueError, match=reason):
        diag.parse_fields(raw(**changes))


def test_diagnostic_sampling_is_deterministic_and_energy_blind():
    rows = [
        {
            "building_id": str(i),
            "roof_form": "flat" if i % 2 else "pitched_ridge",
            "heated_basement_floors": i % 3 == 0,
            "measurement_year": 2018 + i % 4,
            "atemp_m2": 1000 + i * 100,
            "representative_height_m": 9 + i,
            "heat_kwh": i * 1e8,
        }
        for i in range(30)
    ]
    selected = [r["building_id"] for r in diag.select_diagnostics(rows)]
    assert len(set(selected)) == 12
    assert selected == [
        r["building_id"]
        for r in diag.select_diagnostics(
            [{**r, "heat_kwh": -1e9} for r in reversed(rows)]
        )
    ]


def model():
    return {
        "BuildingSurface:Detailed": {
            "floor": {
                "surface_type": "Floor",
                "zone_name": "basement",
                "outside_boundary_condition": "Ground",
            },
            "wall": {
                "surface_type": "Wall",
                "zone_name": "basement",
                "outside_boundary_condition": "Ground",
            },
            "upper_wall": {
                "surface_type": "Wall",
                "zone_name": "upper",
                "outside_boundary_condition": "Outdoors",
            },
        },
        "Site:GroundTemperature:BuildingSurface": {"ground": {}},
        "Material": {"fixed": {"conductivity": 1}},
        "ZoneInfiltration:DesignFlowRate": {"fixed": {"air_changes_per_hour": 0.4}},
        "Schedule:Constant": {"on": {"hourly_value": 1}},
        "ThermostatSetpoint:DualSetpoint": {
            "main_basement_-1_setpoints": {
                "heating_setpoint_temperature_schedule_name": "heating_setpoint"
            }
        },
    }


def test_workflow_does_not_patch_foundations():
    original = model()
    changed = diag.patch_model(copy.deepcopy(original), {"zones": {}}, "combined_kiva")
    assert changed["BuildingSurface:Detailed"] == original["BuildingSurface:Detailed"]
    assert changed["Material"] == original["Material"]
    assert "Foundation:Kiva" not in changed  # emitted by Core, never patched here


def test_attic_exchange_is_separate_from_catalogue_leakage():
    original = model()
    changed = diag.patch_model(
        copy.deepcopy(original),
        {"zones": {"main_attic_3": {}, "upper": {}}},
        "attic_1ach",
    )
    assert len(changed["ZoneVentilation:DesignFlowRate"]) == 1
    assert (
        changed["ZoneVentilation:DesignFlowRate"]["main_attic_3_diagnostic_exchange"][
            "air_changes_per_hour"
        ]
        == 1
    )
    assert (
        changed["ZoneInfiltration:DesignFlowRate"]
        == original["ZoneInfiltration:DesignFlowRate"]
    )
    assert changed["BuildingSurface:Detailed"] == original["BuildingSurface:Detailed"]


def test_basement_temperature_does_not_change_above_ground_setpoint():
    changed = diag.patch_model(model(), {"zones": {}}, "basement_15c")
    assert changed["Schedule:Constant"]["diagnostic_basement_15c"]["hourly_value"] == 15
    assert (
        changed["ThermostatSetpoint:DualSetpoint"]["main_basement_-1_setpoints"][
            "heating_setpoint_temperature_schedule_name"
        ]
        == "diagnostic_basement_15c"
    )
    assert "Foundation:Kiva" not in changed


@pytest.mark.parametrize("state", ["existing", "standard", "advanced"])
@pytest.mark.parametrize(
    "variant",
    ["combined", "combined_kiva", "combined_kiva_low_soil", "combined_kiva_high_soil"],
)
def test_reported_basement_area_and_fixed_physical_inputs(state, variant):
    # Reuse locally prepared evidence when present; portable unit checks run above.
    base = Path(__file__).parents[1] / "output/archetype-diagnostics-mf-se02-2026-10-01"
    if not (base / "cohort.json").exists():
        pytest.skip("Local cohort artifact not available")
    rows = json.loads((base / "cohort.json").read_text())
    row = next(r for r in rows if r["heated_basement_floors"])
    weather = json.loads((base / "audit.json").read_text())["source_weather"]
    original = diag.build_spec(row, state, weather, "original")
    changed = diag.build_spec(row, state, weather, variant)
    assert changed.geometry.basement_conditioned
    assert changed.geometry.basement_depth_m == 2.5
    assert changed.geometry.conditioned_floor_area_m2 == pytest.approx(
        original.geometry.conditioned_floor_area_m2
        + original.geometry.footprint_area_m2
    )
    assert changed.envelope.model_dump(
        exclude={"foundation", "ground_temperature"}
    ) == original.envelope.model_dump(exclude={"foundation", "ground_temperature"})
    assert original.envelope.foundation.method == "prescribed_temperature"
    if "kiva" in variant:
        assert changed.envelope.foundation.method == "kiva"
        expected = (
            1.0
            if variant.endswith("low_soil")
            else 2.5
            if variant.endswith("high_soil")
            else 1.73
        )
        assert changed.envelope.foundation.soil_conductivity_w_mk == expected
        assert changed.envelope.ground_temperature is None
    assert (
        changed.airflow.envelope_infiltration_ach
        == original.airflow.envelope_infiltration_ach
    )
    assert (
        changed.airflow.mechanical_vent_l_s_m2
        * changed.geometry.conditioned_floor_area_m2
        == pytest.approx(
            original.airflow.mechanical_vent_l_s_m2
            * original.geometry.conditioned_floor_area_m2
        )
    )
    assert (
        changed.use.lighting_gain_w_m2 * changed.geometry.conditioned_floor_area_m2
        == pytest.approx(original.use.lighting_gain_w_m2 * row["atemp_m2"])
    )


def test_no_calibration_or_clustering_and_old_test_consumed():
    assert diag.ASSUMPTIONS["calibration_allowed"] is False
    assert diag.ASSUMPTIONS["clustering_allowed"] is False
    assert "prohibited" in diag.ASSUMPTIONS["old_test_status"]


def test_foundation_gate_keeps_common_baseline_failures_without_hiding_new_failures():
    failures = [
        {"building_id": "bad_weather", "variant": "original", "state": s}
        for s in diag.pilot.STATES
    ]
    failures += [
        {"building_id": "bad_weather", "variant": "combined_kiva", "state": "existing"},
        {
            "building_id": "good_baseline",
            "variant": "combined_kiva",
            "state": "existing",
        },
    ]
    assert [
        f["building_id"]
        for f in diag.foundation_specific_failures({"failures": failures})
    ] == ["good_baseline"]


def test_cache_reuse_rejects_changed_physics_or_engine(tmp_path):
    from types import SimpleNamespace

    directory = tmp_path / "case"
    engine = directory / "energyplus"
    engine.mkdir(parents=True)
    current = {"spec_sha256": "spec", "epjson_sha256": "model"}
    old = current | {"status": "complete"}
    receipt = directory / "receipt.json"
    receipt.write_text(json.dumps(old))
    model = {"Version": {"version": {"version_identifier": "24.2"}}}
    (engine / "model.epJSON").write_text(json.dumps(model))
    compilation = {
        "compiler_sha256": "compiler",
        "engine_sha256": "engine",
        "schema_sha256": "schema",
    }
    (engine / "compilation.json").write_text(json.dumps(compilation))
    (engine / "diagnostics.json").write_text(json.dumps({"accepted": True}))
    report = SimpleNamespace(**compilation)
    assert diag.verified_reuse([receipt], current, model, report)
    assert not diag.verified_reuse(
        [receipt], current | {"spec_sha256": "changed"}, model, report
    )
    assert not diag.verified_reuse([receipt], current, {}, report)
    report.engine_sha256 = "changed_engine"
    assert not diag.verified_reuse([receipt], current, model, report)


def test_explicit_later_era_does_not_change_original_pilot_scope():
    later = raw(EgenNybyggAr="2004", EgenAntalKallarplan="0")
    with pytest.raises(ValueError, match="year_outside_se02"):
        diag.parse_fields(later)
    fields = diag.parse_fields(later, year_bounds=(1996, 2005))
    assert fields["epc_year"] == 2004
    assert fields["heated_basement_floors"] == 0
    with pytest.raises(ValueError, match="year_outside_se02"):
        diag.parse_fields(later, year_bounds=(1976, 1985))
    with pytest.raises(ValueError, match="ventilation_not_single_f"):
        diag.parse_fields(later | {"VentTypFTX": "Ja"}, year_bounds=(1996, 2005))


@pytest.mark.parametrize('ventilation', ['FT', 'FTX'])
def test_other_ventilation_requires_explicit_protocol_and_single_system(ventilation):
    row = raw(VentTypF='Nej', **{'VentTyp' + ventilation: 'Ja'})
    with pytest.raises(ValueError, match='ventilation_not_single_f'):
        diag.parse_fields(row)
    fields = diag.parse_fields(row, ventilation_system=ventilation)
    assert fields['measured']
    with pytest.raises(ValueError, match='ventilation_not_single_'):
        diag.parse_fields(row | {'VentTypF':'Ja'}, ventilation_system=ventilation)
