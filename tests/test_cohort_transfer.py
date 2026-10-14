"""Scientific transfer tests: true shared parameters, grouping and exclusion.

Synthetic heating is generated from known physical response functions; it is
never a runtime input to assignment or the evaluator. Real Core integration is
exercised separately using pinned existing specs/weather.
"""

import json
from pathlib import Path

import pytest
from epsm_data.cohorts import assign_cohort

from epsm_workflows.campaign import atomic_json, file_sha256
from epsm_workflows.cohort_transfer import calibration
from epsm_workflows.cohort_transfer.common import (
    DEFAULTS,
    descriptors,
    digest,
    metrics,
    property_folds,
    read,
    source_identity,
)
from epsm_workflows.cohort_transfer.grouping import fit_group
from epsm_workflows.cohort_transfer.preparation import TargetStore, normal_use_dates


def features(n=90):
    rows = []
    for i in range(n):
        # Two coherent geometry cohorts; two buildings per property.
        group = i % 2
        for building in range(2):
            rows.append(
                {
                    "building_id": f"b{i:03}-{building}",
                    "property_key": f"p{i:03}",
                    "use_group": "multi_family",
                    "era": "SE.02",
                    "ventilation": "F",
                    "heating_family": "district_heat",
                    "correction_status": "normal_use_adjusted_pre_weather_correction",
                    "compactness": 0.3 + 0.5 * group + 0.001 * (i % 7),
                    "wall_area_per_atemp": 0.8 if group else 0.4,
                    "roof_area_per_atemp": 0.2,
                    "volume_per_atemp": 3.0,
                    "storeys": 3,
                    "roof_form": "flat",
                    "building_position": "detached",
                    "heated_basement": "no",
                    "renovation_timing": "unknown",
                    "status": "prepared",
                    "atemp_m2": 1000.0,
                }
            )
    return rows


class PhysicalResponse:
    """Independent deterministic simulator with one known effective parameter."""

    def __init__(self, rows):
        self.registers = {
            r["building_id"]: {
                "window_u_multiplier": {
                    "bounds": [0.4, 1.2],
                    "baseline": 1.0,
                    "source": "synthetic known model",
                    "status": "screen_and_cohort_calibration",
                }
            }
            for r in rows
        }

    def evaluate(self, row, parameters):
        assert "target" not in parameters
        return 100.0 + 70.0 * (parameters.get("window_u_multiplier", 1.0) - 1.0)

    def batch(self, rows, designs):
        return {
            (r["building_id"], k): self.evaluate(r, v)
            for r in rows
            for k, v in designs.items()
        }


def study(path, n=90):
    rows = features(n)
    atomic_json(path / "features.json", rows)
    cfg = DEFAULTS | {
        "cluster_resamples": 2,
        "morris_trajectories": 1,
        "joint_designs": 4,
        "bootstrap_resamples": 20,
        "max_clusters": 2,
    }
    atomic_json(path / "config.json", cfg)
    for row in rows:
        known = 0.4 if row["compactness"] < 0.5 else 1.2
        p = path / "targets" / f"{digest(row['property_key'])}.json"
        values = json.loads(p.read_text()) if p.exists() else {}
        values[row["building_id"]] = 100.0 + 70.0 * (known - 1.0)
        atomic_json(p, values)
    prepared = {
        str(p.relative_to(path)): file_sha256(p) for p in path.rglob("*") if p.is_file()
    }
    atomic_json(
        path / "manifest.json",
        {"sources": source_identity(), "inputs": {}, "prepared": prepared},
    )
    return rows


def test_property_folds_and_assignment_do_not_read_targets():
    rows = features(30)
    assert property_folds(rows) == property_folds(list(reversed(rows)))
    changed = [r | {"target": 999999, "residual": -100} for r in rows]
    assert property_folds(rows) == property_folds(changed)
    a, b = fit_group(rows, 2), fit_group(changed, 2)
    assert a == b
    bundle = {
        "schema_version": 1,
        "parent": {"use_group": "multi_family"},
        "transform": a["transform"],
        "profiles": [
            {
                "id": "c0",
                "medoid": a["medoids"][0],
                "distance_limit": 1.0,
                "status": "provisional",
                "parameters": {},
            }
        ],
    }
    assert (
        assign_cohort(descriptors(rows[0]), bundle)["reason"] == "unvalidated_profile"
    )
    assert assign_cohort(descriptors(rows[0]), bundle, allow_provisional=True)[
        "profile"
    ]
    with pytest.raises(ValueError, match="Heating"):
        assign_cohort(descriptors(rows[0]) | {"target": 100}, bundle)


def test_target_store_rejects_sibling_property_leakage(tmp_path):
    rows = study(tmp_path, 5)
    store = TargetStore(tmp_path)
    with pytest.raises(ValueError, match="Evaluated property"):
        store.training([rows[0]], [rows[1]["property_key"]])
    with pytest.raises(ValueError, match="frozen"):
        store.evaluation([rows[0]], tmp_path / "absent.json")


def test_failed_predictions_stay_in_denominator_and_threshold_is_strict():
    rows = [{"property_key": str(i), "target": 100.0} for i in range(4)]
    result = metrics(rows, [100.0, 110.0, 120.0, None])
    assert result["attempted"] == 4 and result["failed"] == 1
    assert result["coverage20"] == 0.5
    assert result["coverage10"] == 0.25


def test_calendar_scenarios_conserve_source_days():
    for year in (2023, 2024):
        for days in (220, 225):
            dates = normal_use_dates(year, days, summer_closure=days == 220)
            assert len(dates) == len(set(dates)) == days
            from datetime import date

            assert all(date.fromisoformat(d).weekday() < 5 for d in dates)


def test_known_cohort_parameters_transfer_and_all_properties_are_excluded(
    tmp_path, monkeypatch
):
    rows = study(tmp_path)
    evaluator = PhysicalResponse(rows)
    original = TargetStore.training
    observed = []

    def check(self, selected, forbidden_properties=()):
        assert not {r["property_key"] for r in selected} & set(forbidden_properties)
        observed.append((len(selected), len(forbidden_properties)))
        return original(self, selected, forbidden_properties)

    monkeypatch.setattr(TargetStore, "training", check)
    report = calibration.run(tmp_path, evaluator=evaluator)
    assert observed and all(forbidden for _, forbidden in observed)
    assert len(json.loads((tmp_path / "predictions.json").read_text())) == len(rows)
    result = next(iter(report.values()))
    assert (
        result["selected"]["coverage20"] > result["methods"]["baseline"]["coverage20"]
    )
    for frozen in (tmp_path / "folds").glob("*/outer*/frozen.json"):
        model = json.loads(frozen.read_text())
        assert set(model["training_properties"]).isdisjoint(
            model["evaluation_properties"]
        )
        for cluster in model["fits"].values():
            assert cluster["single"]["free_parameters"] <= 1
            assert cluster["shared"]["free_parameters"] <= 3


def test_confounded_parameters_are_not_both_fitted(tmp_path):
    rows = features(10)
    simulator = PhysicalResponse(rows)
    for entry in simulator.registers.values():
        entry["infiltration_multiplier"] = dict(entry["window_u_multiplier"])

    # Response columns are proportional across representative geometries.
    def evaluate(row, p):
        weight = row["wall_area_per_atemp"]
        return 100 + weight * (
            p.get("window_u_multiplier", 1) + 2 * p.get("infiltration_multiplier", 1)
        )

    simulator.evaluate = evaluate
    result = calibration.screen(
        rows, simulator, DEFAULTS | {"morris_trajectories": 2}, tmp_path / "screen.json"
    )
    assert len(result["active"]) == 1
    assert result["confounding"]


@pytest.mark.parametrize(
    "category", ["single_family", "multi_family", "office", "school"]
)
@pytest.mark.parametrize("year", [1920, 1950, 1970, 1985, 1998, 2015])
def test_native_matrix_baselines_and_bounded_profile_contract(tmp_path, category, year):
    """Every use/era resolves to native physics; source inputs stay immutable."""
    from epsm_data.cohorts import apply_cohort_profile
    from shapely.geometry import box

    from epsm_workflows.cohort_transfer.preparation import build_baseline

    weather = tmp_path / "weather-2023.epw"
    weather.write_text("LOCATION,Unit test,Test,SWE,TEST,000000,57,12,1,20\n")
    row = {
        "building_id": "synthetic",
        "use_group": category,
        "municipality": "1480",
        "construction_year": year,
        "measurement_year": 2023,
        "atemp_m2": 1200.0,
        "heated_basement_floors": 0,
        "storeys": 2,
        "floor_height_m": 3,
        "roof_form": "flat",
        "geometry_wkb_hex": box(320000, 6400000, 320030, 6400020).wkb_hex,
        "ventilation": "F",
        "heating_family": "district_heat",
        "outline": "Fasad",
    }
    spec = build_baseline(
        row, {"catalogue_climate_zone": "SE.Zone3", "weather_directory": str(tmp_path)}
    )
    before = spec.model_dump(mode="json")
    changed = apply_cohort_profile(
        spec,
        {
            "infiltration_basis": "atemp_area_rate",
            "source": "declared synthetic bounded scenario",
            "parameters": {
                "window_u_multiplier": 0.8,
                "infiltration_multiplier": 1.2,
                "wall_resistance": 1.1,
                "people_multiplier": 0.9,
            },
            "bounds": {
                "window_u_multiplier": [0.5, 1.2],
                "infiltration_multiplier": [0.5, 2],
                "wall_resistance": [0.8, 1.2],
                "people_multiplier": [0.8, 1.2],
            },
        },
        locked_parameters=["infiltration_multiplier"],
    )
    assert spec.model_dump(mode="json") == before
    assert changed.geometry == spec.geometry and changed.climate == spec.climate
    assert changed.airflow == spec.airflow
    assert changed.facade.glazing.u_value_w_m2k == pytest.approx(
        0.8 * spec.facade.glazing.u_value_w_m2k
    )
    assert changed.use.occupant_density_per_m2 == pytest.approx(
        0.9 * spec.use.occupant_density_per_m2
    )
    assert [l.material for l in changed.envelope.wall.layers] == [
        l.material for l in spec.envelope.wall.layers
    ]
    with pytest.raises(ValueError, match="outside frozen bounds"):
        apply_cohort_profile(
            spec,
            {
                "infiltration_basis": "atemp_area_rate",
            "source": "test",
                "parameters": {"wwr": 0.9},
                "bounds": {"wwr": [0.1, 0.4]},
            },
        )


def _native_cache_fixture(tmp_path):
    """Native inputs with a deliberately incomplete weather file for failure tests."""
    from shapely.geometry import box

    from epsm_workflows.cohort_transfer.preparation import build_baseline

    (tmp_path / "weather-2023.epw").write_text(
        "LOCATION,Unit test,Test,SWE,TEST,000000,57,12,1,20\n"
    )
    row = {
        "building_id": "synthetic",
        "use_group": "multi_family",
        "municipality": "1480",
        "construction_year": 1950,
        "measurement_year": 2023,
        "atemp_m2": 1200.0,
        "heated_basement_floors": 0,
        "storeys": 2,
        "floor_height_m": 3,
        "roof_form": "flat",
        "geometry_wkb_hex": box(320000, 6400000, 320030, 6400020).wkb_hex,
        "ventilation": "F",
        "heating_family": "district_heat",
        "outline": "Fasad",
    }
    return build_baseline(
        row, {"catalogue_climate_zone": "SE.Zone3", "weather_directory": str(tmp_path)}
    )


def test_native_cache_requires_physical_weather_and_engine_identity(tmp_path):
    from epsm_data.cohorts import apply_cohort_profile

    from epsm_workflows.cohort_transfer.cache import import_native_receipts

    spec = _native_cache_fixture(tmp_path)
    old, new = tmp_path / "old", tmp_path / "new"
    engine = tmp_path / "engine"
    engine.write_text("declared test engine identity")
    manifest = {
        "sources": source_identity(),
        "inputs": {str(engine): file_sha256(engine)},
    }
    register = {"window_u_multiplier": {"bounds": [0.5, 1.2]}}
    atomic_json(old / "manifest.json", manifest | {"campaign": "old"})
    atomic_json(new / "manifest.json", manifest | {"campaign": "new"})
    atomic_json(new / "config.json", {"engine": str(engine)})
    atomic_json(new / "parameter-register.json", {"synthetic": register})
    atomic_json(
        new / "features.json",
        [
            {
                "building_id": "synthetic",
                "status": "prepared",
                "spec_path": "baseline.json",
            }
        ],
    )
    atomic_json(new / "baseline.json", spec.model_dump(mode="json"))
    parameters = {"window_u_multiplier": 0.8}
    physical = apply_cohort_profile(
        spec,
        {
            "infiltration_basis": "atemp_area_rate",
            "source": "test",
            "parameters": parameters,
            "bounds": {"window_u_multiplier": [0.5, 1.2]},
        },
    )
    case = old / "simulations/oldkey"
    scalars = {"space_heating_carrier_input_kwh": 123}
    atomic_json(case / "spec.json", physical.model_dump(mode="json"))
    atomic_json(case / "energyplus/result.json", {"scalars": scalars})
    atomic_json(
        case / "receipt.json",
        {
            "identity": "oldkey",
            "building_id": "synthetic",
            "parameters": parameters,
            "status": "accepted",
            "spec_sha256": physical.physical_hash(),
            "weather_sha256": physical.climate.weather_sha256,
            "scalars": scalars,
            "prediction": 0.1025,
            "diagnostics": {"err_path": str(case / "energyplus/eplusout.err")},
        },
    )
    assert import_native_receipts(new, [old])["imported"] == 1
    altered = spec.updated(facade=spec.facade.updated(default_wwr=0.3))
    atomic_json(new / "baseline.json", altered.model_dump(mode="json"))
    assert import_native_receipts(new, [old])["skipped"] == 1
    changed = read(new / "manifest.json")
    changed["inputs"][str(engine)] = "changed engine"
    atomic_json(new / "manifest.json", changed)
    with pytest.raises(ValueError, match="identical Core sources and engine"):
        import_native_receipts(new, [old])


def test_spawned_native_batches_deduplicate_and_preserve_failures(tmp_path):
    from epsm_workflows.cohort_transfer.simulation import Evaluator

    engine = Path("/Applications/OpenStudio-3.9.0/EnergyPlus/energyplus")
    if not engine.exists():
        pytest.skip("Native integration needs EnergyPlus 24.2")
    spec = _native_cache_fixture(tmp_path)
    row = {
        "building_id": "synthetic",
        "status": "prepared",
        "spec_path": "baseline.json",
        "atemp_m2": 1200.0,
    }
    atomic_json(tmp_path / "baseline.json", spec.model_dump(mode="json"))
    atomic_json(tmp_path / "manifest.json", {"sources": source_identity()})
    atomic_json(
        tmp_path / "config.json",
        {"engine": str(engine), "workers": 2, "parallel_backend": "processes"},
    )
    atomic_json(
        tmp_path / "parameter-register.json",
        {"synthetic": {"window_u_multiplier": {"bounds": [0.5, 1.2]}}},
    )
    evaluator = Evaluator(tmp_path)
    # Simulate an interrupted previous run with no authoritative receipt.
    key = digest((evaluator.identity, "synthetic", {}))
    interrupted = tmp_path / "simulations" / key / "energyplus"
    interrupted.mkdir(parents=True)
    (interrupted / "interrupted-marker.txt").write_text("preserve partial artifacts")
    try:
        predictions = evaluator.batch(
            [row],
            {
                "baseline": {},
                "same_baseline": {},
                "changed": {"window_u_multiplier": 0.8},
            },
        )
    finally:
        evaluator.close()
    assert len(predictions) == 3 and all(p is None for p in predictions.values())
    receipts = list(tmp_path.glob("simulations/*/receipt.json"))
    assert len(receipts) == 2
    assert all(read(p)["status"] == "failed" for p in receipts)
    assert (
        interrupted.parent / "interrupted-run-0/interrupted-marker.txt"
    ).read_text() == "preserve partial artifacts"
    assert "FileExistsError" not in read(interrupted.parent / "receipt.json")["reason"]


def test_basement_threshold_scenario_preserves_main_use_and_evidenced_zones(tmp_path):
    from epsm_core import ZoneUseSpec
    from epsm_data.cohorts import apply_cohort_profile

    from epsm_workflows.cohort_transfer.parameters import applicable
    from epsm_workflows.cohort_transfer.preparation import programme_context

    spec = _native_cache_fixture(tmp_path)
    spec = spec.updated(
        geometry=spec.geometry.updated(basement_depth_m=2.5, basement_conditioned=True)
    )
    profile = {
        "infiltration_basis": "atemp_area_rate",
            "source": "EPC >10C threshold; effective basement scenario, not measured temperature",
        "parameters": {"basement_setpoint_c": 15, "people_multiplier": 1.1},
        "bounds": {"basement_setpoint_c": [11, 21], "people_multiplier": [0.8, 1.2]},
    }
    changed = apply_cohort_profile(spec, profile)
    assert changed.geometry == spec.geometry and not spec.zone_uses
    assert changed.use.heating_setpoint == spec.use.heating_setpoint
    assert changed.zone_uses[0].zone_kind == "basement"
    assert set(changed.zone_uses[0].use.heating_setpoint.all_values) == {15}
    assert (
        changed.zone_uses[0].use.occupant_density_per_m2
        == changed.use.occupant_density_per_m2
    )
    assert (
        applicable(spec)["basement_setpoint_c"]["status"]
        == "screen_and_cohort_calibration"
    )
    evidenced = spec.updated(
        zone_uses=(
            ZoneUseSpec(
                zone_kind="basement",
                use=spec.use,
                source="independent basement operation",
            ),
        )
    )
    assert applicable(evidenced)["basement_setpoint_c"]["status"] == "not_applicable"
    with pytest.raises(ValueError, match="evidenced zone overrides"):
        apply_cohort_profile(evidenced, profile)
    engine = Path("/Applications/OpenStudio-3.9.0/EnergyPlus/energyplus")
    if engine.exists():
        from epsm_core.compiler import EnergyPlusCompiler

        model, report = EnergyPlusCompiler(engine).compile(changed)
        assert any("basement" in zone for zone in report.data["zones"])
        setpoints = model["ThermostatSetpoint:DualSetpoint"]
        assert (
            len(
                {
                    o["heating_setpoint_temperature_schedule_name"]
                    for o in setpoints.values()
                }
            )
            == 2
        )
    assert (
        programme_context({"use_group": "multi_family", "purpose": "Verksamhet;"})
        == "non_residential_purpose_in_residential_epc"
    )
    assert (
        programme_context(
            {"use_group": "multi_family", "purpose": "Bostad;Flerfamiljshus"}
        )
        == "housing"
    )


def test_conditional_physical_roles_are_shared_without_adjusting_absent_roles(tmp_path):
    from epsm_data.cohorts import apply_cohort_profile

    from epsm_workflows.cohort_transfer.simulation import Evaluator

    spec = _native_cache_fixture(tmp_path)
    profile = {
        "infiltration_basis": "atemp_area_rate",
            "source": "effective role-conditional assumption",
        "parameters": {"basement_setpoint_c": 15, "attic_exchange_ach": 1},
        "bounds": {"basement_setpoint_c": [11, 21], "attic_exchange_ach": [0, 2]},
    }
    assert apply_cohort_profile(spec, profile).physical_hash() == spec.physical_hash()
    register = {
        "basement_setpoint_c": {
            "bounds": [11, 21],
            "baseline": 21,
            "infiltration_basis": "atemp_area_rate",
            "source": "test",
            "status": "not_applicable",
        }
    }
    other = {
        "basement_setpoint_c": register["basement_setpoint_c"]
        | {"status": "screen_and_cohort_calibration"}
    }
    shared = calibration.shared_register(
        [{"building_id": "a"}, {"building_id": "b"}], {"a": register, "b": other}
    )
    assert shared["basement_setpoint_c"]["applicable_buildings"] == 1
    atomic_json(tmp_path / "config.json", {"workers": 1})
    atomic_json(tmp_path / "manifest.json", {"sources": source_identity()})
    atomic_json(tmp_path / "parameter-register.json", {"a": register})
    evaluator = Evaluator(tmp_path)
    row = {"building_id": "a", "status": "prepared"}
    assert evaluator.case_key(row, {}) == evaluator.case_key(
        row, {"basement_setpoint_c": 15}
    )
    assert evaluator.effective_parameters(row, {"basement_setpoint_c": 15}) == {}


def test_response_rank_retains_conditional_effect_with_affine_cross_building_response(
    tmp_path,
):
    rows = features(20)
    simulator = PhysicalResponse(rows)
    for row in rows:
        row["has_basement"] = row["wall_area_per_atemp"] > 0.6
        simulator.registers[row["building_id"]] = {
            "infiltration_multiplier": {
                "bounds": [0.5, 2],
                "baseline": 1,
                "status": "screen_and_cohort_calibration",
                "source": "synthetic",
            },
            "basement_setpoint_c": {
                "bounds": [11, 21],
                "baseline": 21,
                "status": "screen_and_cohort_calibration"
                if row["has_basement"]
                else "not_applicable",
                "source": "synthetic",
            },
        }
    simulator.evaluate = lambda r, p: (
        100
        + (p.get("infiltration_multiplier", 1) - 1) * (2 if r["has_basement"] else 1)
        + (21 - p.get("basement_setpoint_c", 21)) * 0.2 * r["has_basement"]
    )
    result = calibration.screen(
        rows, simulator, DEFAULTS | {"morris_trajectories": 2}, tmp_path / "rank.json"
    )
    assert set(result["active"]) == {"infiltration_multiplier", "basement_setpoint_c"}


def test_final_export_uses_the_same_inner_selection_as_validated_algorithm(
    tmp_path, monkeypatch
):
    rows = study(tmp_path)
    simulator = PhysicalResponse(rows)
    simulator.close = lambda: None
    calibration.run(tmp_path, evaluator=simulator)
    original = calibration.inner_validate
    selection_sizes = []

    def checked(selected, *args, **kwargs):
        selection_sizes.append(len(selected))
        return original(selected, *args, **kwargs)

    monkeypatch.setattr(calibration, "inner_validate", checked)
    monkeypatch.setattr(calibration, "Evaluator", lambda root: simulator)
    bundles = calibration.export(tmp_path)
    assert selection_sizes == [len(rows)]
    assert bundles[0]["final_selection"]["k"] == len(bundles[0]["profiles"])
    assert all(
        p["method"] == bundles[0]["final_selection"]["method"]
        for p in bundles[0]["profiles"]
    )


def test_inner_selection_prioritises_twenty_percent_coverage_then_property_loss():
    def score(coverage, loss, mae):
        return {
            "coverage20": coverage,
            "property_balanced_capped_ape_loss": loss,
            "mae_kwh_m2": mae,
        }

    scores = {
        "baseline": score(0.7, 0.1, 10),
        "single": score(0.8, 0.2, 20),
        "shared": score(0.9, 0.3, 30),
    }
    assert calibration.choose_method(scores) == "shared"
    scores["shared"] = score(0.8, 0.3, 5)
    assert calibration.choose_method(scores) == "single"
    scores["baseline"] = score(0.8, 0.2, 20)
    assert calibration.choose_method(scores) == "baseline"


def test_property_balanced_loss_does_not_reward_many_sibling_buildings():
    rows = [{"target": 100, "property_key": "many"} for _ in range(10)]
    rows.append({"target": 100, "property_key": "one"})
    result = metrics(rows, [100] * 10 + [None])
    assert result["property_balanced_capped_ape_loss"] == 1
    assert result["failed"] == 1
    assert result["coverage20"] == pytest.approx(10 / 11)
    assert result["mae_kwh_m2"] == 0


def test_normal_use_control_can_be_screened_without_being_fitted(tmp_path):
    rows = features(10)
    response = PhysicalResponse(rows)
    config = DEFAULTS | {"calibration_excluded_parameters": ["window_u_multiplier"]}
    report = calibration.screen(rows, response, config, tmp_path / "screen.json")
    assert "window_u_multiplier" in report["effects"]
    assert report["active"] == []
    assert report["diagnostic_only_parameters"] == ["window_u_multiplier"]


def test_boundary_audit_recovers_legacy_labels_and_rejects_target_transcription(
    tmp_path, monkeypatch
):
    import csv
    import hashlib

    from epsm_workflows.cohort_transfer import audit

    raw = {
        "FormularId": "7",
        "Version": "2.8",
        "EgenAtemp": "1000",
        "EgiFjarrvarmeUPPV": "100000",
        "EgiFjarrvarmeVV": "20000",
        "EgenByggnadsKat": "Flerbostadshus",
        "EgenAtempBostad": "100",
        "EgenAntalBolgh": "20",
        "EgenNybyggAr": "1970",
        "VentTypF": "Ja",
        "EgiBeraknatVarde": "Nej",
        "EgiForstaArManad": "2001",
        "EgiSistaArManad": "2012",
    }
    source = tmp_path / "raw.csv"
    with source.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(raw), delimiter="|")
        writer.writeheader()
        writer.writerow(raw)
    row = {
        "building_id": "a",
        "property_key": "p",
        "formular_id": 7,
        "source_file": str(source),
        "source_line": 2,
        "source_row_sha256": hashlib.sha256(
            json.dumps(raw, sort_keys=True).encode()
        ).hexdigest(),
        "epc_category": "FLERBOSTADSHUS",
        "purpose": "Bostad;Flerfamiljshus",
        "atemp_m2": 1000,
        "heat_kwh": 100000,
        "normal_use_adjusted": True,
        "normal_year_corrected": False,
    }
    atomic_json(tmp_path / "cohort.json", [row])
    atomic_json(
        tmp_path / "config.json",
        {
            "cohort_file": "cohort.json",
            "membership_publication": "unused",
            "ventilation": "F",
            "heating_family": "district_heat",
        },
    )
    monkeypatch.setattr(audit, "hydrate_link_membership", lambda rows, _: (rows, {}))
    result = audit.audit_inputs(tmp_path / "config.json", tmp_path / "audit.json")
    assert result["eligible_buildings"] == 1
    record = read(tmp_path / "audit.json")["records"][0]
    assert record["target_kwh_m2"] == 100
    assert not record["dhw_added_to_target"]
    atomic_json(tmp_path / "cohort.json", [row | {"heat_kwh": 120000}])
    with pytest.raises(ValueError, match="differs from original"):
        audit.audit_inputs(tmp_path / "config.json", tmp_path / "rejected.json")


@pytest.mark.parametrize("failure", ["anchor", "nonquadratic"])
def test_search_fallback_is_per_building_and_selected_values_are_native(failure):
    """One bad case cannot invalidate independently checked physical responses."""
    register = {name: {"baseline": 1.0, "bounds": [0.5, 1.5]} for name in ("a", "b")}
    designs = calibration.candidate_designs(["a", "b"], register, count=32)
    rows = [
        {"building_id": bid, "property_key": bid, "target": 120.0}
        for bid in ("good", "bad")
    ]

    class IndependentResponse:
        def __init__(self):
            self.calls = []

        def response(self, bid, settings):
            a, b = settings.get("a", 1.0), settings.get("b", 1.0)
            if bid == "bad" and failure == "anchor":
                return None if a == 1.0 and b == 1.0 else 100 + 10 * a + 10 * b
            if bid == "bad":
                return 100 + 100 * a * b
            return 100 + 10 * a + 10 * b

        def batch(self, selected, settings):
            self.calls.append(([r["building_id"] for r in selected], set(settings)))
            return {
                (r["building_id"], k): self.response(r["building_id"], v)
                for r in selected
                for k, v in settings.items()
            }

    engine = IndependentResponse()
    panel, audit = calibration.surrogate_shortlist(
        rows, engine, designs, ["a", "b"], register
    )
    assert audit["status"] == "validated_search_emulator"
    assert audit["accelerated_buildings"] == 1
    assert audit["maximum_relative_error"] <= 0.02
    assert audit["native_fallback_buildings"] == {
        "bad": "anchor_failure"
        if failure == "anchor"
        else "joint_check_error_above_2_percent"
    }
    assert (["bad"], set(designs)) in engine.calls
    assert (["good", "bad"], set(designs)) not in engine.calls
    # Exact fallback results, including failed anchors, are retained.
    for key, setting in designs.items():
        assert panel["bad", key] == engine.response("bad", setting)
    fit = calibration.fit_parameters(
        rows, engine, ["a", "b"], register, mode="shared", training_values=panel
    )
    for row in rows:
        assert panel[row["building_id"], fit["candidate"]] == engine.response(
            row["building_id"], fit["parameters"]
        )


def test_all_rejected_search_models_keep_complete_native_failure_panel():
    register = {name: {"baseline": 1.0, "bounds": [0.5, 1.5]} for name in ("a", "b")}
    designs = calibration.candidate_designs(["a", "b"], register, count=32)
    rows = [{"building_id": "bad", "property_key": "bad", "target": 100.0}]

    class FailedResponse:
        def batch(self, selected, settings):
            return {(r["building_id"], k): None for r in selected for k in settings}

    panel, audit = calibration.surrogate_shortlist(
        rows, FailedResponse(), designs, ["a", "b"], register
    )
    assert audit["status"] == "direct_core_all_buildings_rejected"
    assert len(panel) == len(designs)
    assert all(v is None for v in panel.values())


def test_area_infiltration_profiles_require_matching_baseline():
    """DB 1.5.0: legacy fitted ACH profiles cannot silently halve new area rates."""
    from epsm_core import BuildingSpec
    from epsm_data.cohorts import apply_cohort_profile
    root = Path(__file__).parents[1] / "output/cohort-transfer-mf-se02-parent-2026-10-04-v10/specs"
    if not root.exists():
        pytest.skip("Frozen real-building specs unavailable")
    spec = BuildingSpec.model_validate_json(next(root.glob("*.json")).read_text())
    spec = spec.updated(airflow=spec.airflow.updated(envelope_infiltration_ach=0, envelope_infiltration_l_s_m2=.20))
    profile = {"parameters":{"infiltration_multiplier":.5},"bounds":{"infiltration_multiplier":[.1,2]},"source":"Synthetic test; not a fitted profile"}
    with pytest.raises(ValueError, match="refit"):
        apply_cohort_profile(spec, profile)
    changed = apply_cohort_profile(spec, profile | {"infiltration_basis":"atemp_area_rate"})
    assert changed.airflow.envelope_infiltration_l_s_m2 == .10
    assert changed.airflow.envelope_infiltration_ach == 0
