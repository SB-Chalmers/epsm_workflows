"""Integration and information-boundary checks for MF parent/history fitting."""

from itertools import product
from types import SimpleNamespace

import numpy as np
import pytest
from epsm_core import (
    AirflowSpec,
    BuildingSpec,
    ClimateSpec,
    FacadeSpec,
    GeometrySpec,
    load_sveby_spec,
)
from epsm_data import resolve_building_archetype
from shapely.geometry import box

from epsm_workflows import mf_parent_dynamic as study
from epsm_workflows.campaign import atomic_json, file_sha256
from epsm_workflows.cohort_transfer.common import digest, read


def row(index=0, **updates):
    return {
        "building_id": f"building-{index}",
        "property_key": f"property-{index}",
        "era": "SE.02",
        "ventilation": "F" if index % 2 else "FTX",
        "construction_year": 1965,
        "measurement_year": 2023,
        "catalogue_climate_zone": "SE.Zone3",
        "municipality": "1480",
        "status": "prepared",
        "base_heat": 120.0,
        "use_group": "multi_family",
        "heating_family": "district_heat",
        "correction_status": "normal_use_adjusted_pre_weather_correction",
        "atemp_m2": 350.0,
        "storeys": 2,
    } | updates


def register():
    return {
        "infiltration_multiplier": {
            "bounds": [0.5, 2.0],
            "baseline": 1.0,
            "status": "screen_and_cohort_calibration",
        },
        "window_u_multiplier": {
            "bounds": [0.4, 1.2],
            "baseline": 1.0,
            "status": "screen_and_cohort_calibration",
        },
        "wall_resistance": {
            "bounds": [0.8, 1.6],
            "baseline": 1.0,
            "status": "screen_and_cohort_calibration",
            "resistance_basis": "total_assembly_r",
        },
    }


class FakeEvaluator:
    def __init__(self, rows, *, interaction=0.0, anchor_failure=()):
        self.registers = {r["building_id"]: register() for r in rows}
        self.calls = []
        self.interaction = interaction
        self.anchor_failure = set(anchor_failure)

    def case_key(self, source, settings):
        return digest((source["building_id"], settings))

    def prediction(self, source, settings):
        if source["building_id"] in self.anchor_failure and not settings:
            return None
        leakage = settings.get("infiltration_multiplier", 1.0) - 1.0
        window = settings.get("window_u_multiplier", 1.0) - 1.0
        relative_u = 1 / settings.get("wall_resistance", 1.0) - 1.0
        return (
            source["base_heat"]
            + 20 * leakage
            + 40 * window
            + 30 * relative_u
            + self.interaction * leakage * relative_u
        )

    def batch(self, rows, designs):
        self.calls.append(([r["building_id"] for r in rows], dict(designs)))
        return {
            (r["building_id"], name): self.prediction(r, settings)
            for r in rows
            for name, settings in designs.items()
        }


def sensitivity():
    return {
        "operating": {"active": ["infiltration_multiplier"]},
        "fabric_by_era": {
            "SE.02": {"active": ["window_u_multiplier", "wall_resistance"]}
        },
    }


def test_separable_emulators_match_native_without_using_targets(tmp_path):
    rows = [row(i) for i in range(4)]
    atomic_json(tmp_path / "features.json", rows)
    evaluator = FakeEvaluator(rows)
    model = study.emulators(tmp_path, evaluator, sensitivity())["SE.02"]
    assert not model["native_fallback"]
    assert model["maximum_validated_relative_error"] < 1e-12
    assert len(model["active"]) <= 3
    for source in rows:
        for parameters in model["designs"].values():
            assert study._predict_estimate(
                source, parameters, {"SE.02": model}
            ) == pytest.approx(evaluator.prediction(source, parameters))
    assert all(
        "target" not in settings
        for _, designs in evaluator.calls
        for settings in designs.values()
    )


def test_emulator_interactions_trigger_complete_native_fallback(tmp_path):
    rows = [row(i) for i in range(2)]
    atomic_json(tmp_path / "features.json", rows)
    evaluator = FakeEvaluator(rows, interaction=100.0)
    model = study.emulators(tmp_path, evaluator, sensitivity())["SE.02"]
    assert set(model["native_fallback"]) == {r["building_id"] for r in rows}
    assert not model["coefficients"]
    assert model["maximum_screened_relative_error"] > 0.02
    for source in rows:
        for name, parameters in model["designs"].items():
            assert model["estimates"][source["building_id"]][
                name
            ] == evaluator.prediction(source, parameters)


def test_anchor_failure_remains_a_failed_baseline_in_native_fallback(tmp_path):
    source = row()
    atomic_json(tmp_path / "features.json", [source])
    model = study.emulators(
        tmp_path,
        FakeEvaluator([source], anchor_failure={source["building_id"]}),
        sensitivity(),
    )["SE.02"]
    assert model["native_fallback"][source["building_id"]] == "anchor_failure"
    assert model["estimates"][source["building_id"]]["baseline"] is None


def test_native_fallback_supports_off_grid_prior_cartesian_combinations(tmp_path):
    rows = [row()]
    atomic_json(tmp_path / "features.json", rows)
    model = study.emulators(
        tmp_path, FakeEvaluator(rows, interaction=100.0), sensitivity()
    )
    # Infiltration's actual prior 1.0 is absent from linspace(.5,2,5).
    parameters = {
        "infiltration_multiplier": 1.0,
        "window_u_multiplier": 0.6,
        "wall_resistance": 1.0,
    }
    assert study._predict_estimate(rows[0], parameters, model) == pytest.approx(
        FakeEvaluator(rows).prediction(rows[0], parameters)
    )


@pytest.mark.parametrize(
    "field", ["target", "residual", "heat_kwh", "measured_heat", "energy_performance"]
)
def test_forward_assignment_rejects_target_information(field):
    profile = {
        "pooled": {},
        "by_era": {"SE.02": {"window_u_multiplier": 0.8}},
        "support_by_era": {"SE.02": 40},
    }
    with pytest.raises(ValueError):
        study.assignment(row(**{field: 100}), profile)


def test_forward_assignment_is_shared_across_systems_and_optional_flags():
    profile = {
        "pooled": {},
        "by_era": {"SE.02": {"window_u_multiplier": 0.8}},
        "support_by_era": {"SE.02": 40},
    }
    first = study.assignment(row(ventilation="F", reno_window=0), profile)
    second = study.assignment(row(ventilation="FTX", reno_window=1), profile)
    assert first == second
    assert first["renovation_status"] == "unidentified"
    assert first["optional_renovation_fields_required"] is False


def manual_models(rows, evaluator):
    registers = register()
    active = ["infiltration_multiplier", "window_u_multiplier", "wall_resistance"]
    designs = {"baseline": {}}
    grids = [
        sorted({registers[n]["baseline"], *np.linspace(*registers[n]["bounds"], 5)})
        for n in active
    ]
    for point in product(*grids):
        parameters = dict(zip(active, point, strict=True))
        designs[digest(parameters)] = parameters
    return {
        era: {
            "active": active,
            "register": registers,
            "designs": designs,
            "coefficients": {},
            "estimates": {
                r["building_id"]: {
                    n: evaluator.prediction(r, p) for n, p in designs.items()
                }
                for r in rows
                if r["era"] == era
            },
        }
        for era in {r["era"] for r in rows}
    }


def test_parent_fit_shares_operating_correction_and_preserves_standard_inputs(tmp_path):
    rows = [row(i, era="SE.01" if i < 35 else "SE.02") for i in range(70)]
    evaluator = FakeEvaluator(rows)
    for source in rows:
        source["target"] = evaluator.prediction(
            source,
            {
                "infiltration_multiplier": 0.5,
                "window_u_multiplier": 0.8,
                "wall_resistance": 1.6,
            },
        )
    result = study.fit_parents(
        rows,
        (),
        evaluator,
        manual_models(rows, evaluator),
        scope="era",
        output=tmp_path / "fit.json",
    )
    assert result["training_metrics"]["mae_kwh_m2"] < 10
    operative = result["operating_shared"]["infiltration_multiplier"]
    assert all(
        p.get("infiltration_multiplier", 1.0) == operative
        for p in result["by_era"].values()
    )
    assert all(
        set(p) <= set(register())
        for _, designs in evaluator.calls
        for p in designs.values()
    )
    assert not ({"people_multiplier", "setpoint_offset_c", "wwr"} & set(register()))


def test_fitting_forbidden_property_fails_before_any_simulation(tmp_path):
    rows = [row(target=100)]
    evaluator = FakeEvaluator(rows)
    with pytest.raises(ValueError, match="Held-out property"):
        study.fit_parents(
            rows,
            {rows[0]["property_key"]},
            evaluator,
            {},
            scope="era",
            output=tmp_path / "fit.json",
        )
    assert not evaluator.calls


def test_sparse_era_inherits_pooled_profile(tmp_path):
    rows = [row(i, target=80.0) for i in range(12)]
    evaluator = FakeEvaluator(rows)
    result = study.fit_parents(
        rows,
        (),
        evaluator,
        manual_models(rows, evaluator),
        scope="era",
        output=tmp_path / "fit.json",
    )
    assert result["by_era"]["SE.02"] == result["pooled"]


def test_nested_parent_training_and_evaluation_properties_do_not_overlap(
    tmp_path, monkeypatch
):
    rows = [row(i) for i in range(20)] + [row(100, property_key="property-0")]
    atomic_json(tmp_path / "features.json", rows)
    for source in rows:
        path = tmp_path / "targets" / f"{digest(source['property_key'])}.json"
        existing = read(path) if path.exists() else {}
        atomic_json(path, existing | {source["building_id"]: 100.0})
    seen = []

    def fitting(observed, forbidden, evaluator, models, *, scope, output):
        assert not set(forbidden) & {r["property_key"] for r in observed}
        seen.append((set(forbidden), {r["property_key"] for r in observed}))
        value = {"pooled": {}, "by_era": {}, "scope": scope}
        atomic_json(output, value)
        return value

    monkeypatch.setattr(study, "fit_parents", fitting)
    monkeypatch.setattr(
        study, "predict_parent", lambda members, plan, evaluator: [100.0] * len(members)
    )
    result = study.nested_parents(tmp_path, object(), {})
    assert len(result["predictions"]) == len(rows)
    assert len(result["folds"]) == 5
    assert len(seen) == 35
    for fold in result["folds"]:
        assert not set(fold["training_properties"]) & set(fold["evaluation_properties"])
    assert all(
        "property-0"
        in set(fold["training_properties"]) ^ set(fold["evaluation_properties"])
        for fold in result["folds"]
    )


@pytest.fixture
def native_spec():
    physical = resolve_building_archetype(
        category="multi_family",
        construction_year=1965,
        climate_zone="SE.Zone3",
        ventilation="F",
    )
    return BuildingSpec(
        identifier="building-0",
        geometry=GeometrySpec.from_footprint(
            box(0, 0, 20, 10), storey_elevations_m=(0, 3, 6)
        ),
        envelope=physical["envelope"],
        facade=FacadeSpec(
            default_wwr=0.2, glazing=physical["glazing"], solar_shading_factor=0.5
        ),
        use=load_sveby_spec().use_spec(calendar_year=2023),
        airflow=AirflowSpec(
            topology="F", mechanical_vent_l_s_m2=0.35, **physical["core_airflow_inputs"]
        ),
        climate=ClimateSpec(
            epw_path="unused.epw",
            weather_sha256="0" * 64,
            latitude_deg=57.0,
            longitude_deg=12.0,
            elevation_m=0.0,
            time_zone_hours=1.0,
            calendar_year=2023,
        ),
        load_reference_area_m2=350.0,
    )


def test_history_cache_requires_identical_historical_physics_and_carrier(
    tmp_path, native_spec
):
    source = row()
    evaluator = FakeEvaluator([source])
    atomic_json(tmp_path / "features.json", [source])
    receipt_path = (
        tmp_path / "simulations" / evaluator.case_key(source, {}) / "receipt.json"
    )
    atomic_json(
        receipt_path,
        {
            "status": "accepted",
            "identity": evaluator.case_key(source, {}),
            "building_id": source["building_id"],
            "spec_sha256": native_spec.physical_hash(),
            "prediction": 100.0,
            "scalars": {"space_heating_carrier_input_kwh": 35000.0},
        },
    )
    original = native_spec.updated(
        facade=native_spec.facade.updated(
            glazing=native_spec.facade.glazing.updated(u_value_w_m2k=2.8)
        )
    )
    states = {
        name: source
        | {"building_id": name, "source_building_id": source["building_id"]}
        for name in ("stock-equivalent", "historical-se02")
    }
    native = SimpleNamespace(
        root=tmp_path / "dynamic-native",
        spec=lambda row: (
            native_spec if row["building_id"] == "stock-equivalent" else original
        ),
        case_key=lambda row, settings: digest((row["building_id"], settings)),
    )
    imported = study.seed_identical_history_baselines(
        tmp_path, evaluator, native, states
    )
    assert imported["imported"] == 1
    assert imported["previous_dynamic_bank_used"] is False
    assert not (
        native.root
        / "simulations"
        / native.case_key(states["historical-se02"], {})
        / "receipt.json"
    ).exists()
    wrong_area = {
        "bad-area": states["stock-equivalent"]
        | {"building_id": "bad-area", "atemp_m2": 700.0}
    }
    native.spec = lambda row: native_spec
    with pytest.raises(ValueError, match="carrier/area differs"):
        study.seed_identical_history_baselines(tmp_path, evaluator, native, wrong_area)


def test_dynamic_native_states_preserve_original_envelope_and_standard_use(
    tmp_path, monkeypatch, native_spec
):
    from epsm_workflows import mf_stock_history

    source = row(ventilation="F")
    atomic_json(tmp_path / "features.json", [source])
    atomic_json(tmp_path / "config.json", {"history_upgrade_profiles": {}})
    historical = native_spec.updated(
        facade=native_spec.facade.updated(
            glazing=native_spec.facade.glazing.updated(u_value_w_m2k=2.8)
        )
    )
    original_path = "original-specs/building-0/baseline.json"
    atomic_json(tmp_path / original_path, historical.model_dump(mode="json"))
    atomic_json(
        tmp_path / "original-construction-references.json",
        {
            source["building_id"]: {
                "spec_path": original_path,
                "physical_hash": historical.physical_hash(),
                "basis": "original_construction",
            }
        },
    )
    atomic_json(
        tmp_path / "manifest.json",
        {"artifacts": {original_path: file_sha256(tmp_path / original_path)}},
    )
    evaluator = FakeEvaluator([source])
    evaluator.spec = lambda value: pytest.fail(
        "Dynamic states must use historical references"
    )

    def histories(metadata, controls, **kwargs):
        assert "target" not in metadata and kwargs["draws"] == 100
        return {
            "states": [
                {
                    "state_key": name,
                    "weight": 0.5,
                    "state": {
                        "wall_profile_id": "original",
                        "roof_profile_id": "original",
                        "window_u_value_w_m2k": u,
                    },
                }
                for name, u in (("old", 2.2), ("new", 1.4))
            ]
        }

    class Native:
        def __init__(self, root):
            self.root = root

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def batch(self, rows, designs):
            output = {}
            for state in rows:
                spec = BuildingSpec.model_validate(read(self.root / state["spec_path"]))
                assert spec.envelope == native_spec.envelope
                assert spec.use == native_spec.use
                assert spec.geometry == native_spec.geometry
                assert spec.airflow == native_spec.airflow
                output[state["building_id"], "baseline"] = (
                    100.0 if spec.facade.glazing.u_value_w_m2k == 2.2 else None
                )
            return output

    monkeypatch.setattr(mf_stock_history, "generate_histories", histories)
    monkeypatch.setattr(study, "Evaluator", Native)
    result = study.dynamic_panel(tmp_path, evaluator)
    assert result["native_states"] == 2
    assert result["failed_native_states"] == 1
    assert all(
        values[source["building_id"]] is None
        for values in result["predictions"].values()
    )
    assert result["identified_histories"] is False


@pytest.mark.parametrize(
    "corruption",
    [None, "receipt_physical_hash", "engine_hash", "multiple_scope", "missing_scope"],
)
def test_prepare_imports_only_current_schema_hash_matching_receipts(
    tmp_path, monkeypatch, native_spec, corruption
):
    old, selection, output = (
        tmp_path / name for name in ("old", "selection", "prepared")
    )
    source_file = tmp_path / "source.csv"
    source_file.write_text("source bytes\n")
    engine = tmp_path / "engine"
    engine.write_text("engine bytes\n")
    epw = tmp_path / "weather.epw"
    epw.write_text("weather bytes\n")
    corrected = native_spec.updated(
        climate=native_spec.climate.updated(
            epw_path=str(epw), weather_sha256=file_sha256(epw)
        )
    )
    original = corrected.updated(
        facade=corrected.facade.updated(solar_shading_factor=1.0)
    )
    source = row(
        source_file=str(source_file),
        source_line=2,
        source_row_sha256="0" * 64,
        formular_id=1,
        spec_path="specs/building-0/baseline.json",
    )
    scope_evidence = tmp_path / "scope-evidence.json"
    atomic_json(
        scope_evidence,
        {
            "schema_version": 1,
            "records": {
                source["building_id"]: {
                    **{
                        name: source[name]
                        for name in (
                            "building_id",
                            "formular_id",
                            "property_key",
                            "source_row_sha256",
                        )
                    },
                    "release_subject_count": 1,
                    "archive_subject_count": 1,
                }
            },
        },
    )
    atomic_json(selection / "cohort.json", [source])
    atomic_json(
        old / "config.json",
        {
            "engine": str(engine),
            "weather_manifest": {
                "1480:2023": {"path": str(epw), "sha256": file_sha256(epw)}
            },
        },
    )
    atomic_json(old / "features.json", [source])
    atomic_json(old / "manifest.json", {"runtime": {}})
    atomic_json(old / source["spec_path"], original.model_dump(mode="json"))
    atomic_json(
        old / "engine.json",
        {"engine_hash": file_sha256(engine), "schema_hash": "0" * 64},
    )
    atomic_json(
        old / "runs" / "case" / "receipt.json",
        {
            "building_id": source["building_id"],
            "scenario": "baseline",
            "status": "accepted",
            "prediction_kwh_m2": 100.0,
            "physical_hash": original.physical_hash(),
            "spec_path": source["spec_path"],
        },
    )
    monkeypatch.setattr(study, "build_baseline", lambda value, config: corrected)
    monkeypatch.setattr(study, "physical_descriptors", lambda value, spec: {})
    monkeypatch.setattr(
        study, "_targets", lambda values: {source["building_id"]: 100.0}
    )
    monkeypatch.setattr(study, "source_identity", lambda: {"test": "runtime"})
    monkeypatch.setattr(
        "epsm_core.EnergyPlusRunner",
        lambda *args, **kwargs: SimpleNamespace(
            compiler=SimpleNamespace(schema_hash="0" * 64)
        ),
    )
    if corruption == "receipt_physical_hash":
        receipt_path = old / "runs" / "case" / "receipt.json"
        atomic_json(receipt_path, read(receipt_path) | {"physical_hash": "0" * 64})
        with pytest.raises(ValueError, match="receipt.*physical spec"):
            study.prepare(selection, old, output, scope_evidence=scope_evidence)
        return
    if corruption == "engine_hash":
        atomic_json(
            old / "engine.json", {"engine_hash": "0" * 64, "schema_hash": "0" * 64}
        )
        with pytest.raises(ValueError, match="different native engine"):
            study.prepare(selection, old, output, scope_evidence=scope_evidence)
        return
    if corruption in {"multiple_scope", "missing_scope"}:
        evidence = read(scope_evidence)
        if corruption == "multiple_scope":
            evidence["records"][source["building_id"]].update(
                release_subject_count=2, archive_subject_count=2
            )
        else:
            evidence["records"] = {}
        atomic_json(scope_evidence, evidence)

        def forbidden_build(*args, **kwargs):
            pytest.fail("Quarantined certificate must not enter preparation")

        def no_targets(values):
            assert values == []
            return {}

        monkeypatch.setattr(study, "build_baseline", forbidden_build)
        monkeypatch.setattr(study, "_targets", no_targets)
        summary = study.prepare(selection, old, output, scope_evidence=scope_evidence)
        assert summary["prepared"] == 0
        assert summary["current_native_receipts_imported"] == 0
        assert summary["outcomes"] == {"certificate_scope_quarantined": 1}
        assert read(output / "features.json") == []
        assert not list((output / "targets").glob("*.json"))
        assert str(scope_evidence) in read(output / "manifest.json")["sources"]
        return
    summary = study.prepare(selection, old, output, scope_evidence=scope_evidence)
    assert summary["prepared"] == 1
    assert summary["current_native_receipts_imported"] == 1
    assert (
        read(output / "current-native-predictions.json")[source["building_id"]] == 100.0
    )
    assert (
        str(old / "runs" / "case" / "receipt.json")
        in read(output / "manifest.json")["sources"]
    )
    study.verify(output)
    source_file.write_text("altered source bytes\n")
    with pytest.raises(ValueError, match="Frozen sources changed"):
        study.verify(output)
