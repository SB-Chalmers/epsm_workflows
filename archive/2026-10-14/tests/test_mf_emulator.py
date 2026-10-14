"""Rank-identifiable native anchors and physical transmission coordinates."""

from copy import deepcopy

import numpy as np
import pytest

from epsm_workflows.mf_emulator import (
    anchors,
    design_matrix,
    fit_quadratic,
    physical_coordinates,
    validate_native_checks,
)


def register():
    return {
        "wall_insulation_overlay_m": {
            "baseline": 0,
            "bounds": [0, 0.15],
            "baseline_wall_r_m2k_w": 0.7,
            "overlay_conductivity_w_mk": 0.04,
        },
        "roof_resistance": {
            "baseline": 1,
            "bounds": [0.8, 1.6],
            "resistance_basis": "total_assembly_r",
        },
        "flow_multiplier": {"baseline": 1, "bounds": [0.8, 1.2]},
    }


def test_total_assembly_r_coordinates_are_normalized_u_changes():
    params = register()
    value = physical_coordinates({"roof_resistance": 1.6}, ["roof_resistance"], params)
    expected = (1 / 1.6 - 1) / (1 / 0.8 - 1 / 1.6)
    assert value.tolist() == pytest.approx([1, expected, expected**2])
    assert physical_coordinates({}, ["roof_resistance"], params).tolist() == [1, 0, 0]


def test_overlay_coordinates_use_original_total_r_and_declared_conductivity():
    params = register()
    depth = 0.05
    low_u, high_u = 1 / 0.7, 1 / (0.7 + 0.15 / 0.04)
    expected = (1 / (0.7 + depth / 0.04) - low_u) / (low_u - high_u)
    vector = physical_coordinates(
        {"wall_insulation_overlay_m": depth}, ["wall_insulation_overlay_m"], params
    )
    assert vector.tolist() == pytest.approx([1, expected, expected**2])
    assert vector[1] != pytest.approx(depth / 0.15)


def test_other_controls_keep_linear_coordinates():
    value = physical_coordinates(
        {"flow_multiplier": 1.2}, ["flow_multiplier"], register()
    )
    assert value.tolist() == pytest.approx([1, 0.5, 0.25])


def test_three_axis_anchors_remain_full_rank_with_prior_at_bound():
    params = register()
    active = list(params)
    axis = anchors(active, params)
    matrix = design_matrix(axis, active, params)
    assert np.linalg.matrix_rank(matrix) == 7
    overlay_depths = {
        item.get("wall_insulation_overlay_m", 0) for item in axis.values()
    }
    assert overlay_depths == {0, 0.075, 0.15}
    assert len(matrix) >= 7
    assert axis == anchors(active, deepcopy(params))


def test_actual_prior_is_preserved_when_not_the_parameter_midpoint():
    params = register()
    params["flow_multiplier"]["baseline"] = 0.95
    axis = anchors(["flow_multiplier"], params)
    levels = {setting.get("flow_multiplier", 0.95) for setting in axis.values()}
    assert levels == {0.8, 0.95, 1.0, 1.2}


def test_two_level_bound_prior_is_rejected_before_fitting():
    active = ["wall_insulation_overlay_m"]
    settings = [{}, {"wall_insulation_overlay_m": 0.15}]
    with pytest.raises(ValueError, match="Rank-deficient"):
        design_matrix(settings, active, register())
    with pytest.raises(ValueError, match="Rank-deficient"):
        fit_quadratic(settings, [200, 80], active, register())
    assert design_matrix(
        settings, active, register(), require_full_rank=False
    ).shape == (2, 3)


def test_physical_quadratic_exactly_recovers_nonlinear_depth_heat_loss():
    params, active = register(), ["wall_insulation_overlay_m"]
    axis = anchors(active, params)

    def native_heat(setting):
        return 20 + 100 / (0.7 + setting.get("wall_insulation_overlay_m", 0) / 0.04)

    values = [native_heat(setting) for setting in axis.values()]
    fitted = fit_quadratic(axis, values, active, params)
    check = [{"wall_insulation_overlay_m": 0.012}, {"wall_insulation_overlay_m": 0.10}]
    actual = [native_heat(setting) for setting in check]
    validation = validate_native_checks(
        check, actual, fitted["coefficients"], active, params
    )
    assert validation["validated"]
    assert validation["maximum_relative_error"] < 1e-12
    assert fitted["rank"] == fitted["required_rank"] == 3
    # Raw-depth quadratic interpolation of the same three points does not capture
    # the near-zero-depth response, despite fitting its anchors exactly.
    raw = np.array(
        [
            [
                1,
                p.get("wall_insulation_overlay_m", 0),
                p.get("wall_insulation_overlay_m", 0) ** 2,
            ]
            for p in axis.values()
        ]
    )
    raw_coefficients = np.linalg.lstsq(raw, values, rcond=None)[0]
    raw_prediction = np.array([1, 0.012, 0.012**2]) @ raw_coefficients
    assert abs(raw_prediction - actual[0]) / actual[0] > 0.02


def test_overlay_coordinate_matches_core_construction_u_without_changing_structure():
    from epsm_data import resolve_building_archetype

    from epsm_workflows.mf_evidence import insulation_candidates
    from epsm_workflows.mf_physics import add_insulation_overlay

    original = resolve_building_archetype(
        category="multi_family",
        construction_year=1940,
        climate_zone="SE.Zone3",
        ventilation="F",
    )["envelope"].wall
    later = resolve_building_archetype(
        category="multi_family",
        construction_year=1990,
        climate_zone="SE.Zone3",
        ventilation="F",
    )["envelope"].wall
    material = later.layers[insulation_candidates(later)[0]].material
    params = register()
    params["wall_insulation_overlay_m"].update(
        {
            "baseline_wall_r_m2k_w": 1 / original.u_value_w_m2k,
            "overlay_conductivity_w_mk": material.conductivity_w_mk,
        }
    )
    changed, _ = add_insulation_overlay(
        original,
        material=material,
        thickness_m=0.05,
        source="Reviewed Data assembly material",
    )
    high, _ = add_insulation_overlay(
        original,
        material=material,
        thickness_m=0.15,
        source="Reviewed Data assembly material",
    )
    expected = (changed.u_value_w_m2k - original.u_value_w_m2k) / (
        original.u_value_w_m2k - high.u_value_w_m2k
    )
    actual = physical_coordinates(
        {"wall_insulation_overlay_m": 0.05}, ["wall_insulation_overlay_m"], params
    )
    assert actual[1] == pytest.approx(expected)
    assert changed.layers[1:] == original.layers


def test_missing_overlay_physics_never_silently_uses_linear_depth():
    for field in ("baseline_wall_r_m2k_w", "overlay_conductivity_w_mk"):
        params = register()
        del params["wall_insulation_overlay_m"][field]
        with pytest.raises(ValueError, match=field):
            anchors(["wall_insulation_overlay_m"], params)


def test_legacy_layer_multiplier_cannot_claim_total_r_physics():
    params = register()
    del params["roof_resistance"]["resistance_basis"]
    with pytest.raises(ValueError, match="total assembly R"):
        physical_coordinates({}, ["roof_resistance"], params)


def test_native_two_percent_gate_is_retained_and_failures_force_fallback():
    params, active = register(), ["flow_multiplier"]
    checks = [{"flow_multiplier": 1.05}]
    good = validate_native_checks(checks, [100], [101.9, 0, 0], active, params)
    assert good["validated"] and not good["native_fallback"]
    bad = validate_native_checks(checks, [100], [102.1, 0, 0], active, params)
    assert not bad["validated"] and bad["native_fallback"]
    missing = validate_native_checks(checks, [None], [100, 0, 0], active, params)
    assert missing["reason"] == "held_out_native_failure"


def test_invalid_bounds_controls_or_nonfinite_anchors_are_rejected():
    params = register()
    with pytest.raises(ValueError, match="outside frozen bounds"):
        physical_coordinates({"flow_multiplier": 10}, ["flow_multiplier"], params)
    with pytest.raises(ValueError, match="distinct"):
        anchors(["flow_multiplier", "flow_multiplier"], params)
    with pytest.raises(ValueError, match="at most three"):
        anchors(["a", "b", "c", "d"], params)
    with pytest.raises(ValueError, match="native heating"):
        fit_quadratic(
            anchors(["flow_multiplier"], params),
            [None, 100, 100],
            ["flow_multiplier"],
            params,
        )
