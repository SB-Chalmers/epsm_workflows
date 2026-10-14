"""Scientific guardrails for source freezing and common physical scenarios."""

import json
import os
from pathlib import Path

import pytest
from epsm_core import (
    AirflowSpec,
    BuildingSpec,
    ConstructionSpec,
    DeliverySpec,
    EnvelopeSpec,
    FacadeSpec,
    Footprint,
    GeometrySpec,
    GlazingSpec,
    HeatingDeliverySpec,
    LayerSpec,
    MaterialSpec,
    ScheduleSpec,
    UseSpec,
)
from epsm_core.weather import climate_from_epw

from epsm_workflows import archetype_application as app


@pytest.fixture
def prepared(tmp_path):
    weather = tmp_path / "weather.epw"
    weather.write_text("LOCATION,Test,Test,SWE,TEST,0,57,12,1,20\n")
    material = MaterialSpec(
        name="test", conductivity_w_mk=0.04, density_kg_m3=40, specific_heat_j_kgk=840
    )
    construction = ConstructionSpec(
        layers=(LayerSpec(material=material, thickness_m=0.15),)
    )
    spec = BuildingSpec(
        identifier="synthetic",
        geometry=GeometrySpec.from_footprint(
            Footprint.rectangle(10, 10), storeys=1, storey_height_m=3
        ),
        facade=FacadeSpec(
            default_wwr=0.2,
            glazing=GlazingSpec(u_value_w_m2k=1.8, solar_heat_gain_coefficient=0.6),
        ),
        climate=climate_from_epw(weather),
        envelope=EnvelopeSpec(
            **dict.fromkeys(
                (
                    "wall",
                    "top",
                    "ground",
                    "intermediate_floor",
                    "weather_shell",
                    "basement_wall",
                ),
                construction,
            )
        ),
        airflow=AirflowSpec(
            topology="F", envelope_infiltration_ach=0.2, mechanical_vent_l_s_m2=0.35
        ),
        use=UseSpec(
            heating_setpoint=ScheduleSpec.constant(21),
            occupant_density_per_m2=0.02,
            equipment_gain_w_m2=2,
            dhw_kwh_m2_year=21,
        ),
        delivery=DeliverySpec(
            heating=HeatingDeliverySpec(carrier="district_heat", source="test")
        ),
    )
    (tmp_path / "spec.json").write_text(spec.model_dump_json())
    buildings = [
        {
            "building_id": bid,
            "property_key": bid,
            "family": "MF_SE02",
            "split": split,
            "atemp_m2": 100,
            "measurement_year": spec.climate.calendar_year,
            "operation_basis": "actual_use",
            "evidence": dict.fromkeys(app.REQUIRED_EVIDENCE, "synthetic fixture"),
            "specs": {"baseline": "spec.json", "candidate": "spec.json"},
            "scenario_sources": {
                "baseline": "frozen matrix",
                "candidate": "shared physical hypothesis",
            },
        }
        for bid, split in (
            ("train1", "train"),
            ("train2", "train"),
            ("val", "validation"),
            ("test", "test"),
        )
    ]
    # Poisoned held-out values would raise if development accidentally parsed them.
    (tmp_path / "targets.csv").write_text(
        "building_id,delivered_space_heat_kwh\ntrain1,1000\ntrain2,1000\nval,1000\ntest,POISON\n"
    )
    config = {
        "schema_version": 1,
        "target_boundary": "delivered_space_heat",
        "target_normalization": "actual_use",
        "weather_boundary": "measurement_year_before_weather_correction",
        "targets_csv": "targets.csv",
        "buildings": buildings,
        "bootstrap_resamples": 50,
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    root = tmp_path / "study"
    app.prepare(path, root)
    return root, config, path


def receipt(root, bid, scenario, prediction=1000, status="accepted"):
    split = (
        "train" if bid.startswith("train") else "validation" if bid == "val" else "test"
    )
    app.atomic_json(
        root / "runs" / f"{bid}-{scenario}" / "receipt.json",
        {
            "building_id": bid,
            "scenario": scenario,
            "split": split,
            "status": status,
            "scalars": {"space_heating_carrier_input_kwh": prediction},
        },
    )


def development(root, *, failed=False):
    for bid in ("train1", "train2", "val"):
        receipt(root, bid, "baseline", 1300)
        receipt(
            root,
            bid,
            "candidate",
            900 if bid != "train2" else 1100,
            "failed" if failed and bid == "train2" else "accepted",
        )


def test_selection_is_shared_and_never_parses_held_out_energy(prepared):
    root, _, _ = prepared
    development(root)
    chosen = app.select(root)
    assert chosen["choices"]["MF_SE02"]["scenario"] == "candidate"
    assert chosen["test_energy_read"] is False
    result = app.evaluate(root)
    assert result["selected"]["below_20_percent"] == 1
    assert result["selected"]["below_10_percent"] == 0  # exactly 10 is not <10
    assert result["property_bootstrap"]["independent_properties"] == 1


def test_failed_training_candidate_cannot_win_by_dropping_buildings(prepared):
    root, _, _ = prepared
    development(root, failed=True)
    assert app.select(root)["choices"]["MF_SE02"]["scenario"] == "baseline"


def test_failed_evaluation_is_counted_and_frozen_receipts_cannot_change(prepared):
    root, _, _ = prepared
    development(root)
    app.select(root)
    receipt(root, "val", "candidate", status="failed")
    with pytest.raises(ValueError, match="receipts changed"):
        app.evaluate(root)


def test_property_leakage_and_source_changes_rejected(prepared):
    root, config, path = prepared
    config["buildings"][-1]["property_key"] = "train1"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="cross splits"):
        app.prepare(path, root.parent / "other-study")
    with pytest.raises(ValueError, match="source changed"):
        app.select(root)


def test_all_evaluation_attempts_required_before_target_opening(prepared):
    root, _, _ = prepared
    development(root)
    app.select(root)
    receipt(root, "test", "baseline")
    with pytest.raises(ValueError, match="all evaluation"):
        app.evaluate(root, split="test")
    assert not (root / "evaluation-opened.json").exists()


def test_reference_area_and_prior_inspection_are_enforced(prepared):
    root, config, path = prepared
    config["buildings"][0]["atemp_m2"] = 101
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="reference area"):
        app.prepare(path, root.parent / "bad-area")
    config["buildings"][0]["atemp_m2"] = 100
    config["buildings"][-1]["previously_examined"] = True
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="Previously examined"):
        app.prepare(path, root.parent / "bad-holdout")


def test_failed_validation_building_stays_in_attempted_denominator(prepared):
    root, _, _ = prepared
    development(root)
    receipt(root, "val", "candidate", status="failed")
    app.select(root)
    result = app.evaluate(root)
    assert result["attempted_buildings"] == result["failed_pairs"] == 1
    assert result["accepted_pairs"] == 0
    assert result["selected"]["below_20_percent"] == 0
    assert result["selected"]["below_20_fraction_attempted"] == 0
    assert result["by_family"]["MF_SE02"]["attempted"] == 1


def test_opened_test_receipts_cannot_be_replaced(prepared):
    old_root, _, path = prepared
    # Create a new study with a numeric held-out observation before any simulation.
    (old_root.parent / "targets.csv").write_text(
        "building_id,delivered_space_heat_kwh\ntrain1,1000\ntrain2,1000\nval,1000\ntest,1000\n"
    )
    root = old_root.parent / "numeric-study"
    app.prepare(path, root)
    development(root)
    app.select(root)
    receipt(root, "test", "baseline", 1300)
    receipt(root, "test", "candidate", 1000)
    assert app.evaluate(root, split="test")["selected"]["below_10_percent"] == 1
    receipt(root, "test", "candidate", 999)
    with pytest.raises(ValueError, match="Test receipts changed"):
        app.evaluate(root, split="test")


def test_full_protocol_with_energyplus_when_weather_is_supplied(prepared):
    """Actual runner integration on synthetic targets; no stock-accuracy evidence.

    An explicit complete EPW enables this optional test; regular unit runs do not
    require a local engine/weather installation. Identical candidates test the
    baseline tie rule and freeze/receipt plumbing through real annual simulation.
    """
    weather = os.environ.get("EPSM_TEST_EPW")
    engine = os.environ.get("ENERGYPLUS_EXE")
    if (
        not weather
        or not engine
        or not Path(weather).is_file()
        or not Path(engine).is_file()
    ):
        pytest.skip("Explicit integration EPW and EnergyPlus executable required")
    original, config, path = prepared
    spec_path = original.parent / "spec.json"
    spec = BuildingSpec.model_validate_json(spec_path.read_text())
    year = int(os.environ.get("EPSM_TEST_YEAR", "2023"))
    spec = spec.updated(climate=climate_from_epw(weather, calendar_year=year))
    spec_path.write_text(spec.model_dump_json())
    for row in config["buildings"]:
        row["measurement_year"] = year
    path.write_text(json.dumps(config))
    (original.parent / "targets.csv").write_text(
        "building_id,delivered_space_heat_kwh\ntrain1,1000\ntrain2,1000\nval,1000\ntest,1000\n"
    )
    root = original.parent / "engine-study"
    app.prepare(path, root)
    records = app.simulate(root, energyplus=engine)
    assert len(records) == 6
    assert all(r["status"] == "accepted" for r in records)
    assert app.select(root)["choices"]["MF_SE02"]["scenario"] == "baseline"
    assert app.evaluate(root)["accepted_pairs"] == 1
    assert len(app.simulate(root, energyplus=engine, splits=("test",))) == 8
    assert app.evaluate(root, split="test")["accepted_pairs"] == 1
    assert (root / "evaluation-opened.json").exists()


def test_coverage_objective_can_prefer_higher_mae(prepared):
    """Two tolerable errors beat one perfect and one >20% error on coverage."""
    root, config, path = prepared
    config["selection_objective"] = "coverage20_then_mae"
    path.write_text(json.dumps(config))
    other = root.parent / "coverage-study"
    app.prepare(path, other)
    for bid in ("train1", "train2", "val"):
        receipt(other, bid, "baseline", 1190)
        receipt(other, bid, "candidate", 1000 if bid != "train2" else 1250)
    choice = app.select(other)["choices"]["MF_SE02"]
    assert choice["scenario"] == "baseline"
    assert choice["training_coverage20"] == {"baseline": 1.0, "candidate": 0.5}
    assert (
        choice["training_scores_mae_kwh_m2"]["candidate"]
        < choice["training_scores_mae_kwh_m2"]["baseline"]
    )
