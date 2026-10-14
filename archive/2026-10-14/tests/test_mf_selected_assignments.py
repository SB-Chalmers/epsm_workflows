"""Deployment assignment routing is separate from comparative candidate exports."""

import importlib.util
import os
import sys
from copy import deepcopy
from pathlib import Path

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

from epsm_workflows.campaign import atomic_json, file_sha256
from epsm_workflows.cohort_transfer.common import read
from epsm_workflows.mf_stock_history import engineering_prior


@pytest.fixture
def study():
    proposal = os.environ.get("MF_ASSIGNMENT_PROPOSAL_MODULE")
    if proposal:
        name = "epsm_workflows._mf_assignment_proposal_under_test"
        spec = importlib.util.spec_from_file_location(name, proposal)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    from epsm_workflows import mf_parent_dynamic

    return mf_parent_dynamic


def metadata(**updates):
    return {
        "building_id": "unseen-building-198",
        "construction_year": 1940,
        "measurement_year": 2023,
        "era": "SE.01",
        "catalogue_climate_zone": "SE.Zone3",
        "ventilation": "F",
        "heated_basement": "no",
        "heated_basement_floors": 0,
    } | updates


def profile(parent_adopted):
    return {
        "pooled": {"window_u_multiplier": 0.8},
        "by_era": {},
        "support_by_era": {"SE.01": 100},
        "measurement_years": [2023],
        "adoption_status": {
            "adopt": parent_adopted,
            "reasons": []
            if parent_adopted
            else ["overall_improvement_threshold_not_met"],
        },
    }


def priors(dynamic_adopted):
    return engineering_prior({"lifetime_multiplier": 0.75}) | {
        "selected_configuration": "life-0.750",
        "measurement_years": [2023],
        "training_properties": 200,
        "adoption_status": {
            "adopt": dynamic_adopted,
            "reasons": []
            if dynamic_adopted
            else ["overall_improvement_threshold_not_met"],
        },
    }


@pytest.mark.parametrize(
    "parent,dynamic,expected",
    [
        (False, False, "current_assumptions"),
        (False, True, "current_assumptions"),
        (True, False, "calibrated_parent"),
        (True, True, "dynamic_histories"),
    ],
)
def test_selected_model_requires_both_gates_for_dynamic(
    study, parent, dynamic, expected
):
    result = study.selected_assignment(metadata(), profile(parent), priors(dynamic))
    assert result["model"] == expected
    assert result["selection"]["parent_adoption"]["adopt"] is parent
    assert result["selection"]["dynamic_adoption"]["adopt"] is dynamic
    if not parent:
        assert result["parameters"] == {}
        assert result["profile"]["solar_shading_factor"] == 1.0
        assert result["selection"]["parent_adoption"]["reasons"]
    elif dynamic:
        assert result["profile_basis"] == "original_construction"
        assert result["controls"]["lifetime_multiplier"] == 0.75
        assert result["selected_configuration"] == "life-0.750"
        assert len(result["state_distribution"]) > 0
        assert sum(s["weight"] for s in result["state_distribution"]) == pytest.approx(
            1
        )
        assert "parameters" not in result
    else:
        assert result["parameters"]["window_u_multiplier"] == 0.8


def test_unseen_building_generates_reproducible_states_without_history_file(
    study, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    first = study.dynamic_assignment(metadata(), priors(False))
    second = study.dynamic_assignment(metadata(reno_window=True), priors(False))
    assert first == second
    assert first["history_draws"] == 100
    assert first["state_distribution_status"] == "target_free_generated"
    assert first["provenance"]["component_history_file_required"] is False
    assert first["provenance"]["measured_heating_used_for_history_generation"] is False
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "field",
    [
        "target",
        "measured_heat",
        "heat_kwh_m2",
        "prediction_error",
        "energy_performance",
    ],
)
def test_selected_and_candidate_histories_reject_heating_information(study, field):
    row = metadata(**{field: 100})
    with pytest.raises(ValueError):
        study.dynamic_assignment(row, priors(True))
    with pytest.raises(ValueError):
        study.selected_assignment(row, profile(True), priors(True))


def test_dynamic_rejects_effective_parent_basis_and_reference(study):
    with pytest.raises(ValueError, match="original construction"):
        study.dynamic_assignment(
            metadata(parent_basis="effective_surviving_stock"), priors(True)
        )
    with pytest.raises(ValueError, match="original construction"):
        study.dynamic_assignment(
            metadata(),
            priors(True),
            original_reference={"basis": "effective_surviving_stock"},
        )


def test_year_extrapolation_and_missing_metadata_are_explicit(study):
    future = study.dynamic_assignment(metadata(measurement_year=2040), priors(True))
    assert not future["measurement_year_within_development_support"]
    assert "extrapolated" in future["confidence"]
    missing = study.dynamic_assignment({"era": "SE.01"}, priors(True))
    assert missing["state_distribution_status"] == "metadata_incomplete"
    assert missing["state_distribution"] == []
    assert set(missing["required_metadata_missing"]) == {
        "building_id",
        "construction_year",
        "measurement_year",
    }


def test_unseen_id_can_resolve_era_from_construction_year(study):
    row = metadata()
    del row["era"]
    result = study.dynamic_assignment(row, priors(True))
    assert result["parent"] == "MF:SE.01"
    assert result["original_reference"]["era"] == "SE.01"


@pytest.fixture
def native_spec():
    physical = resolve_building_archetype(
        category="multi_family",
        construction_year=1940,
        climate_zone="SE.Zone3",
        ventilation="F",
    )
    return BuildingSpec(
        identifier="unseen-building-198",
        geometry=GeometrySpec.from_footprint(
            box(0, 0, 20, 10), storey_elevations_m=(0, 3, 6)
        ),
        envelope=physical["envelope"],
        facade=FacadeSpec(
            default_wwr=0.2, glazing=physical["glazing"], solar_shading_factor=0.5
        ),
        airflow=AirflowSpec(
            topology="F", mechanical_vent_l_s_m2=0.35, **physical["core_airflow_inputs"]
        ),
        use=load_sveby_spec().use_spec(calendar_year=2023),
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


def export_fixture(root, spec, *, receipt_mismatch=False, historical=False):
    row = metadata(spec_path="specs/original.json")
    atomic_json(root / row["spec_path"], spec.model_dump(mode="json"))
    atomic_json(
        root / "original-construction-references.json",
        {
            row["building_id"]: {
                "spec_path": row["spec_path"],
                "physical_hash": spec.physical_hash(),
                "basis": "original_construction_with_fixed_normal_use_and_corrected_shading",
            }
        },
    )
    current = spec.updated(facade=spec.facade.updated(solar_shading_factor=1.0))
    sources = {}
    if historical:
        path = root / "legacy" / "runs" / "a" / "receipt.json"
        atomic_json(
            path,
            {
                "building_id": row["building_id"],
                "status": "accepted",
                "scenario": "baseline",
                "physical_hash": current.physical_hash(),
            },
        )
        sources[str(path)] = file_sha256(path)
    else:
        path = root / "current-native" / "simulations" / "a" / "receipt.json"
        atomic_json(
            path,
            {
                "building_id": row["building_id"],
                "status": "accepted",
                "parameters": {},
                "spec_sha256": "wrong" if receipt_mismatch else current.physical_hash(),
            },
        )
        atomic_json(path.parent / "spec.json", current.model_dump(mode="json"))
    atomic_json(
        root / "manifest.json",
        {
            "sources": sources,
            "artifacts": {row["spec_path"]: file_sha256(root / row["spec_path"])},
        },
    )
    return row, current


@pytest.mark.parametrize("historical", [False, True])
def test_exports_keep_candidates_and_verify_exact_current_reference(
    study, tmp_path, native_spec, historical
):
    row, expected = export_fixture(tmp_path, native_spec, historical=historical)
    original_profile, original_priors = profile(False), priors(True)
    before = deepcopy((original_profile, original_priors))
    exported = study.export_assignments(
        tmp_path, [row], original_profile, original_priors
    )
    selected = exported["selected"][row["building_id"]]
    assert selected["model"] == "current_assumptions"
    reference = selected["profile"]["reference"]
    assert (
        BuildingSpec.model_validate(read(Path(reference["spec_path"]))).physical_hash()
        == expected.physical_hash()
    )
    assert reference["receipt_verification_status"] == "verified"
    assert len(reference["verified_current_receipts"]) == 1
    assert (
        exported["calibrated_parent_candidates"][row["building_id"]]["parameters"][
            "window_u_multiplier"
        ]
        == 0.8
    )
    dynamic = exported["dynamic_candidates"][row["building_id"]]
    assert dynamic["original_reference"]["spec_path"] == str(
        tmp_path / row["spec_path"]
    )
    assert dynamic["original_reference"]["physical_hash"] == native_spec.physical_hash()
    assert (original_profile, original_priors) == before
    for name in (
        "assignments.json",
        "candidate-calibrated-parent-assignments.json",
        "candidate-dynamic-assignments.json",
    ):
        assert (tmp_path / name).exists()


def test_receipt_mismatch_cannot_silently_label_recreated_current_as_verified(
    study, tmp_path, native_spec
):
    row, _ = export_fixture(tmp_path, native_spec, receipt_mismatch=True)
    with pytest.raises(ValueError, match="current receipt"):
        study.export_assignments(tmp_path, [row], profile(False), priors(False))


def test_adopted_dynamic_export_uses_original_spec_and_keeps_parent_candidate(
    study, tmp_path, native_spec
):
    row, _ = export_fixture(tmp_path, native_spec)
    exported = study.export_assignments(tmp_path, [row], profile(True), priors(True))
    selected = exported["selected"][row["building_id"]]
    assert selected["model"] == "dynamic_histories"
    assert (
        selected["original_reference"]["physical_hash"] == native_spec.physical_hash()
    )
    assert (
        selected["state_distribution"]
        == exported["dynamic_candidates"][row["building_id"]]["state_distribution"]
    )
    assert "parameters" not in selected
    assert exported["calibrated_parent_candidates"][row["building_id"]][
        "parameters"
    ] == {"window_u_multiplier": 0.8}


def test_original_reference_mutation_and_observations_are_rejected(
    study, tmp_path, native_spec
):
    row, _ = export_fixture(tmp_path, native_spec)
    with pytest.raises(ValueError):
        study.export_assignments(
            tmp_path, [row | {"target": 100}], profile(True), priors(True)
        )
    atomic_json(
        tmp_path / row["spec_path"],
        native_spec.updated(
            facade=native_spec.facade.updated(solar_shading_factor=0.8)
        ).model_dump(mode="json"),
    )
    with pytest.raises(ValueError, match="original-reference spec"):
        study.export_assignments(tmp_path, [row], profile(True), priors(True))


def test_se02_exports_keep_historical_windows_separate_from_current_stock(
    study, tmp_path, native_spec
):
    stock = native_spec.updated(
        facade=native_spec.facade.updated(
            glazing=native_spec.facade.glazing.updated(u_value_w_m2k=2.2)
        )
    )
    row, expected_current = export_fixture(tmp_path, stock)
    row |= {"construction_year": 1965, "era": "SE.02"}
    historical, provenance = study.original_construction_spec(stock, row)
    assert stock.facade.glazing.u_value_w_m2k == 2.2
    assert historical.facade.glazing.u_value_w_m2k == 2.8
    assert historical.envelope == stock.envelope
    assert historical.geometry == stock.geometry
    assert historical.use == stock.use
    assert historical.airflow == stock.airflow
    assert historical.climate == stock.climate
    assert provenance["historical_window_override_applied"]
    path = "original-specs/historical.json"
    atomic_json(tmp_path / path, historical.model_dump(mode="json"))
    row["original_reference_spec_path"] = path
    atomic_json(
        tmp_path / "original-construction-references.json",
        {
            row["building_id"]: {
                "spec_path": path,
                "physical_hash": historical.physical_hash(),
                "basis": "original_construction",
                "component_basis": provenance,
            }
        },
    )
    manifest = read(tmp_path / "manifest.json")
    manifest["artifacts"][path] = file_sha256(tmp_path / path)
    atomic_json(tmp_path / "manifest.json", manifest)
    exports = study.export_assignments(tmp_path, [row], profile(False), priors(True))
    bid = row["building_id"]
    current = exports["selected"][bid]["profile"]["reference"]
    assert current["physical_hash"] == expected_current.physical_hash()
    parent = exports["calibrated_parent_candidates"][bid]
    assert parent["parent_baseline_reference"]["physical_hash"] == stock.physical_hash()
    dynamic = exports["dynamic_candidates"][bid]
    assert dynamic["original_reference"]["physical_hash"] == historical.physical_hash()
    assert dynamic["original_reference"]["spec_path"] == str(tmp_path / path)
    assert all(
        s["state"]["window_u_value_w_m2k"] <= 2.8 for s in dynamic["state_distribution"]
    )
