"""Scientific boundaries and reproducibility of the target-free stock pilot."""

import math
from copy import deepcopy
from pathlib import Path

import pytest

import epsm_workflows.mf_stock_history as stock_history
from epsm_workflows.mf_stock_history import (
    engineering_prior,
    generate_histories,
    library_window_profiles,
    original_component_claims,
    original_window_profile,
    validate_controls,
    weibull_scale,
    weighted_predictions,
)


def row(**updates):
    return {
        "building_id": "building-a",
        "construction_year": 1950,
        "measurement_year": 2023,
    } | updates


def windows():
    return [
        {
            "profile_id": "old-window",
            "era": "old",
            "valid_from_year": 1,
            "valid_through_year": 1975,
            "u_value_w_m2k": 2.5,
            "definition": "whole_window_including_frame",
            "source": "test source",
            "classification": "engineering_proxy",
            "installation_period_basis": "Test construction-era installation proxy",
        },
        {
            "profile_id": "new-window",
            "era": "new",
            "valid_from_year": 1976,
            "u_value_w_m2k": 1.2,
            "definition": "whole_window_including_frame",
            "source": "test source",
            "classification": "engineering_proxy",
            "installation_period_basis": "Test construction-era installation proxy",
        },
    ]


def profiles():
    return {
        "roof": [
            {
                "profile_id": "roof-1977",
                "valid_from_year": 1977,
                "source": "Reviewed roof eligibility",
                "classification": "engineering_assumption",
            }
        ],
        "facade": [
            {
                "profile_id": "wall-1990",
                "valid_from_year": 1990,
                "source": "Reviewed facade eligibility",
                "classification": "engineering_assumption",
            }
        ],
    }


def generate(values=None, controls=None, **kwargs):
    return generate_histories(
        values or row(), controls, window_profiles=windows(), **kwargs
    )


def test_prior_is_experimental_and_mean_not_scale():
    prior = engineering_prior()
    assert prior["classification"] == "engineering_sensitivity_prior"
    assert prior["controls"]["roof_upgrade_probability"] == 0.5
    assert prior["thermal_material_degradation"] == 0
    assert prior["lighting_changes"] == "inactive"
    assert weibull_scale(35) * math.gamma(1 + 1 / 3) == pytest.approx(35)
    assert prior["components"]["roof"]["mean_years"] == 40
    assert prior["components"]["facade"]["mean_years"] == 50


def test_identity_seed_and_row_order_are_reproducible():
    first = generate()
    reordered = dict(reversed(list(row().items())))
    assert generate(reordered) == first
    assert len(first["histories"]) == 100
    assert sum(x["weight"] for x in first["states"]) == pytest.approx(1)
    assert generate(row(building_id="building-b"))["histories"] != first["histories"]
    assert generate(seed=1)["histories"] != first["histories"]


def test_common_random_numbers_survive_control_changes():
    short = generate(row(construction_year=1850), {"lifetime_multiplier": 0.75})
    long = generate(row(construction_year=1850), {"lifetime_multiplier": 1.25})
    for left, right in zip(short["histories"], long["histories"], strict=True):
        for component in ("window", "roof", "facade"):
            left_first = left["events"][component][0]["year"] - 1850
            right_first = right["events"][component][0]["year"] - 1850
            assert left_first / right_first == pytest.approx(0.75 / 1.25)


def test_service_cycles_repeat_and_never_pass_measurement_year():
    ensemble = generate(row(construction_year=1850), upgrade_profiles=profiles())
    assert all(len(h["events"]["window"]) > 1 for h in ensemble["histories"])
    for history in ensemble["histories"]:
        for events in history["events"].values():
            dates = [event["year"] for event in events]
            assert dates == sorted(dates)
            assert all(1850 < date <= 2023 for date in dates)
            for event in events:
                if event.get("profile_id") == "roof-1977":
                    assert event["year"] >= 1977
                if event.get("profile_id") == "wall-1990":
                    assert event["year"] >= 1990


def test_optional_completed_measure_flags_are_not_required_or_truth():
    baseline = generate()
    with_flags = generate(row(reno_window=True, reno_roof=True, reno_wall=False))
    assert baseline == with_flags
    assert not baseline["assignment"]["observed_renovation_history"]
    assert not baseline["provenance"]["optional_renovation_flags_used"]


def test_roof_covering_replacement_does_not_automatically_insulate():
    ensemble = generate(
        row(construction_year=1850),
        {"roof_upgrade_probability": 1, "facade_upgrade_probability": 1},
    )
    assert ensemble["provenance"]["unidentifiable_components"] == ["roof", "facade"]
    for history in ensemble["histories"]:
        assert history["state"]["roof_profile_id"] == "original"
        assert history["state"]["wall_profile_id"] == "original"
        assert all(
            e["thermal_effect"] == "replacement_like_for_like"
            for e in history["events"]["roof"]
        )
        assert all(
            e["thermal_effect"] == "maintenance" for e in history["events"]["facade"]
        )


def test_reviewed_upgrade_mappings_are_applied_only_after_eligibility():
    ensemble = generate(
        row(construction_year=1850),
        {"roof_upgrade_probability": 1, "facade_upgrade_probability": 1},
        upgrade_profiles=profiles(),
    )
    assert any(
        h["state"]["roof_profile_id"] == "roof-1977" for h in ensemble["histories"]
    )
    assert any(
        h["state"]["wall_profile_id"] == "wall-1990" for h in ensemble["histories"]
    )
    earlier = generate(row(measurement_year=1976), upgrade_profiles=profiles())
    assert all(
        h["state"]["roof_profile_id"] == "original" for h in earlier["histories"]
    )


def test_like_for_like_replacements_preserve_existing_insulation():
    ensemble = generate(row(construction_year=1850), upgrade_profiles=profiles())
    for history in ensemble["histories"]:
        upgraded = False
        for event in history["events"]["roof"]:
            upgraded |= event["thermal_effect"] == "energy_upgrade"
        if upgraded:
            assert history["state"]["roof_profile_id"] == "roof-1977"


def test_future_component_evidence_is_ignored_without_changing_histories():
    evidence = {
        "window": {
            "installation_year": 2024,
            "source": "Future survey",
            "classification": "measured",
        }
    }
    future = generate(row(component_evidence=evidence))
    assert future["histories"] == generate()["histories"]
    assert future["provenance"]["ignored_evidence"][0]["reason"] == "future_evidence"


def test_sourced_component_dates_anchor_cycles_and_quantitative_window_state():
    evidence = {
        "window": {
            "installation_year": 2022,
            "source": "Independent window inspection",
            "classification": "measured",
            "definition": "whole_window_including_frame",
            "u_value_w_m2k": 1.7,
        }
    }
    ensemble = generate(row(component_evidence=evidence))
    for history in ensemble["histories"]:
        assert any(
            e["year"] == 2022 and e["basis"] == "independent_component_evidence"
            for e in history["events"]["window"]
        )
    # At least one previously better proxy is superseded by the measured value;
    # quantitative evidence is not forced to improve the fitted prediction.
    assert any(h["state"]["window_u_value_w_m2k"] == 1.7 for h in ensemble["histories"])


def test_checkbox_dated_evidence_does_not_identify_a_component():
    checkbox = {
        "roof": {
            "installation_year": 2019,
            "classification": "checkbox",
            "source": "Optional EPC flag",
        }
    }
    ensemble = generate(row(component_evidence=checkbox))
    assert ensemble["histories"] == generate()["histories"]
    assert (
        ensemble["provenance"]["ignored_evidence"][0]["reason"]
        == "insufficient_independent_evidence"
    )


def test_original_reference_basis_is_audited_and_effective_parents_rejected():
    original = generate(
        row(
            source_start_year=1940,
            source_start_year_basis="Catalogue representative, not observed installation",
        )
    )
    assert original["assignment"]["construction_year"] == 1950
    assert original["assignment"]["source_start_year"] == 1940
    with pytest.raises(ValueError, match="original construction"):
        generate(row(parent_basis="calibrated_surviving_stock"))


@pytest.mark.parametrize(
    "key",
    [
        "target_kwh_m2",
        "residual",
        "measured_heat",
        "energy_use",
        "demand_kwh",
        "performance",
    ],
)
def test_assignment_rejects_target_and_performance_inputs(key):
    with pytest.raises(ValueError, match="energy or residual"):
        generate(row(**{key: 1}))


@pytest.mark.parametrize(
    "controls",
    [
        {"lifetime_multiplier": 0.5},
        {"lifetime_multiplier": 1.3},
        {"roof_upgrade_probability": -0.1},
        {"facade_upgrade_probability": 1.1},
        {"roof_upgrade_probability": True},
        {"other": 1},
    ],
)
def test_invalid_history_controls_are_rejected(controls):
    with pytest.raises(ValueError):
        validate_controls(controls)


def test_window_proxy_requires_source_period_and_whole_window_definition():
    for field in ("source", "installation_period_basis", "definition"):
        invalid = windows()
        invalid[0].pop(field)
        with pytest.raises(ValueError):
            generate_histories(row(), window_profiles=invalid)


def test_installation_periods_have_no_fractional_year_gap():
    evidence = {
        "window": {
            "installation_year": 1975,
            "source": "Inspection",
            "classification": "measured",
        }
    }
    ensemble = generate(row(measurement_year=1975, component_evidence=evidence))
    assert all(
        h["state"]["window_profile_id"] == "old-window" for h in ensemble["histories"]
    )


def test_library_window_profiles_declare_the_engineering_proxy_boundary():
    profiles_from_data = library_window_profiles()
    assert len(profiles_from_data) == 6
    assert all(p["classification"] == "engineering_proxy" for p in profiles_from_data)
    assert all(
        p["definition"] == "whole_window_including_frame" for p in profiles_from_data
    )
    assert profiles_from_data[1]["u_value_w_m2k"] == pytest.approx(2.8)
    assert profiles_from_data[1]["excluded_stock_profile"]["u_value_w_m2k"] == 2.2
    profiles_from_data[0]["u_value_w_m2k"] = 999
    assert library_window_profiles()[0]["u_value_w_m2k"] != 999


def test_historical_window_reference_preserves_current_stock_library():
    from epsm_data import resolve_building_archetype

    original = original_window_profile(1970)
    assert original["era"] == "SE.02"
    assert original["u_value_w_m2k"] == pytest.approx(2.8)
    assert original["solar_heat_gain_coefficient"] == 0.7
    assert original["visible_transmittance"] == 0.75
    assert (
        original["construction_reference_basis"] == "historical_original_construction"
    )
    assert "v1_mf_se02_existing_window" in original["source"]
    assert len(original["source_content_sha256"]) == 4
    assert original["source_hashes"]
    for path, checksum in original["source_hashes"].items():
        assert Path(path).is_absolute() and Path(path).is_file()
        assert len(checksum) == 64
    stock = resolve_building_archetype(
        category="multi_family",
        construction_year=1970,
        climate_zone="SE.Zone1",
        ventilation="FTX",
        default_state="existing",
    )
    assert stock["glazing"].u_value_w_m2k == 2.2


def test_unreplaced_se02_histories_start_at_historical_window_not_stock_fit():
    ensemble = generate_histories(row(construction_year=1970, measurement_year=1970))
    assert len(ensemble["states"]) == 1
    assert ensemble["states"][0]["state"]["window_u_value_w_m2k"] == 2.8
    assert all(not history["events"]["window"] for history in ensemble["histories"])
    assert (
        ensemble["provenance"]["window_profiles"][1]["excluded_stock_profile"][
            "u_value_w_m2k"
        ]
        == 2.2
    )


@pytest.mark.parametrize(
    "basis_field,basis",
    [
        ("condition", "representative_surviving_stock_window"),
        ("construction_reference_basis", "effective_surviving_stock"),
    ],
)
def test_explicitly_surviving_stock_window_profile_cannot_start_histories(
    basis_field, basis
):
    stock_profile = original_window_profile(1970)
    stock_profile["u_value_w_m2k"] = 2.2
    stock_profile[basis_field] = basis
    with pytest.raises(ValueError, match="not surviving-stock"):
        generate_histories(row(construction_year=1970), window_profiles=[stock_profile])
    with pytest.raises(ValueError, match="not calibrated surviving-stock"):
        generate_histories(
            row(construction_year=1970, parent_basis="calibrated_surviving_stock")
        )


@pytest.mark.parametrize("role", ["assembly", "material", "thermal", "claim"])
def test_changed_historical_source_content_requires_review(monkeypatch, role):
    real = stock_history._data_document
    if role == "claim":
        path = "layered/evidence/mf_se02_window_v1_4_diagnostic.json"
    else:
        path = stock_history._SE02_ORIGINAL_RECORDS[role][0]

    def changed_document(relative):
        value = real(relative)
        if relative == path:
            if role == "claim":
                value["claim"]["previous_original_construction_reference_u_w_m2k"] = 2.2
            else:
                _, group, key, _ = stock_history._SE02_ORIGINAL_RECORDS[role]
                value[group][key]["unexpected_source_change"] = True
        return value

    monkeypatch.setattr(stock_history, "_data_document", changed_document)
    with pytest.raises(ValueError, match="source content changed"):
        stock_history._historical_se02_window()


def test_unrelated_historical_evaluation_metrics_are_not_reference_content(monkeypatch):
    real = stock_history._data_document

    def changed_document(relative):
        value = real(relative)
        if relative.endswith("mf_se02_window_v1_4_diagnostic.json"):
            value["window_only"] = {
                "coverage20": "does not define construction physics"
            }
        return value

    monkeypatch.setattr(stock_history, "_data_document", changed_document)
    assert stock_history._historical_se02_window()["u_value_w_m2k"] == 2.8


def test_period_proxy_is_monotonic_only_after_separating_historical_stock_basis():
    profiles_from_data = library_window_profiles()
    assert [
        profile["u_value_w_m2k"] for profile in profiles_from_data
    ] == pytest.approx([2.8, 2.8, 2.5, 2.2, 1.8, 1.5])
    # An independent 1980 installation now changes a SE.02 original window to
    # the 2.5 period proxy, rather than being ignored against a fitted 2.2 state.
    evidence = {
        "window": {
            "installation_year": 1980,
            "source": "Independent installation register",
            "classification": "measured",
        }
    }
    ensemble = generate_histories(
        row(construction_year=1970, measurement_year=1980, component_evidence=evidence)
    )
    assert all(
        history["state"]["window_u_value_w_m2k"] == 2.5
        for history in ensemble["histories"]
    )


def test_original_profile_nested_provenance_is_copied():
    first = original_window_profile(1970)
    first["excluded_stock_profile"]["u_value_w_m2k"] = 999
    first["source_hashes"].clear()
    assert (
        original_window_profile(1970)["excluded_stock_profile"]["u_value_w_m2k"] == 2.2
    )
    assert original_window_profile(1970)["source_hashes"]


def test_all_original_component_claims_preserve_ground_boundary_semantics():
    for era in ("SE.01", "SE.02", "SE.03", "SE.04", "SE.05", "SE.06"):
        claims = original_component_claims(era)
        assert len(claims) == 7
        assert all(
            claim["condition"] == "original_construction" for claim in claims.values()
        )
        assert all(
            claim["source_hashes"] and claim["source_content_sha256"]
            for claim in claims.values()
        )
        for role in ("ground_floor", "basement_wall"):
            assert claims[role]["effective_ground_boundary_reference"]
            assert claims[role]["value_kind"] == "legacy_effective_ground_reference"
        assert not claims["roof"]["effective_ground_boundary_reference"]
    claims = original_component_claims("SE.02")
    assert claims["window"]["selected_value_w_m2k"] == 2.8
    assert (
        claims["window"]["source_declared_condition"]
        == "representative_surviving_stock_window"
    )
    assert claims["window"]["excluded_stock_profile"]["u_value_w_m2k"] == 2.2
    claims["roof"]["source_hashes"].clear()
    assert original_component_claims("SE.02")["roof"]["source_hashes"]


@pytest.mark.parametrize(
    "role",
    [
        "roof",
        "external_wall",
        "intermediate_floor",
        "internal_wall",
        "ground_floor",
        "basement_wall",
    ],
)
def test_surviving_stock_opaque_component_cannot_enter_original_reference(
    monkeypatch, role
):
    real = stock_history._data_document

    def changed_document(relative):
        value = real(relative)
        if relative == "parametric/catalogue.json":
            value["claim_metadata"][f"multi_family:SE.03:existing:{role}"][
                "condition"
            ] = "calibrated_surviving_stock"
        return value

    monkeypatch.setattr(stock_history, "_data_document", changed_document)
    with pytest.raises(ValueError, match="surviving-stock/calibrated"):
        stock_history._original_component_claims.__wrapped__("SE.03")


def test_opaque_stock_fit_hidden_under_original_condition_is_rejected(monkeypatch):
    real = stock_history._data_document

    def changed_document(relative):
        value = real(relative)
        if relative == "parametric/catalogue.json":
            value["claim_metadata"]["multi_family:SE.03:existing:external_wall"][
                "basis"
            ] = "user_adopted_retrospective_wall_hypothesis"
        return value

    monkeypatch.setattr(stock_history, "_data_document", changed_document)
    with pytest.raises(ValueError, match="surviving-stock/calibrated"):
        stock_history._original_component_claims.__wrapped__("SE.03")


def test_effective_wall_target_cannot_masquerade_as_ground_boundary(monkeypatch):
    real = stock_history._data_document

    def changed_document(relative):
        value = real(relative)
        if relative == "parametric/catalogue.json":
            value["claim_metadata"]["multi_family:SE.03:existing:external_wall"][
                "boundary"
            ] = "effective_slab_on_ground"
        return value

    monkeypatch.setattr(stock_history, "_data_document", changed_document)
    with pytest.raises(ValueError, match="only permitted for ground"):
        stock_history._original_component_claims.__wrapped__("SE.03")


def test_unknown_original_component_era_is_rejected():
    with pytest.raises(ValueError, match="Unknown MF construction era"):
        original_component_claims("unknown")


def test_compression_preserves_draw_mass_and_per_draw_installation_dates():
    ensemble = generate()
    assert len(ensemble["states"]) < len(ensemble["histories"])
    index = {h["draw"]: h for h in ensemble["histories"]}
    for state in ensemble["states"]:
        assert state["weight"] == len(state["draw_indices"]) / 100
        for position, draw in enumerate(state["draw_indices"]):
            assert state["state"] == index[draw]["state"]
            for component in ("window", "roof", "facade"):
                assert (
                    state["installation_years"][component][position]
                    == index[draw]["installation_years"][component]
                )


def test_weighted_prediction_retains_failed_native_probability_mass():
    states = [{"state_key": "a", "weight": 0.75}, {"state_key": "b", "weight": 0.25}]
    result = weighted_predictions(states, {"a": 100})
    assert result["status"] == "incomplete"
    assert result["prediction"] is None
    assert result["successful_weight"] == 0.75
    assert result["missing_weight"] == 0.25
    assert result["failed_state_keys"] == ["b"]
    complete = weighted_predictions(states, [100, 200])
    assert complete["prediction"] == 125
    assert complete["weighted_p05"] == 100
    assert complete["weighted_p95"] == 200
    zero = weighted_predictions(states, {"a": 0, "b": 0})
    assert zero["status"] == "complete" and zero["prediction"] == 0


def test_weighted_predictions_validate_weights_and_invalid_outputs():
    invalid = [{"state_key": "a", "weight": 0.5}]
    with pytest.raises(ValueError, match="sum to one"):
        weighted_predictions(invalid, {"a": 100})
    states = [{"state_key": "a", "weight": 1.0}]
    assert weighted_predictions(states, {"a": float("nan")})["missing_weight"] == 1
    assert weighted_predictions(states, {"a": -10})["prediction"] is None
    duplicate = deepcopy(states) + deepcopy(states)
    duplicate[0]["weight"] = duplicate[1]["weight"] = 0.5
    with pytest.raises(ValueError, match="Duplicate"):
        weighted_predictions(duplicate, {"a": 1})
