"""Independent parent/pooled routing without silently dropped physical controls."""

from copy import deepcopy

import numpy as np
import pytest

from epsm_workflows.mf_emulator import anchors, fit_quadratic
from epsm_workflows.mf_parent_emulator_blocks import (
    UnsupportedSettingsError,
    emulator_blocks,
    predict_estimate,
    select_block,
    settings_supported,
)

OP, WINDOW, WALL, ROOF = (
    "infiltration_multiplier", "window_u_multiplier", "wall_resistance", "roof_resistance",
)
ROW = {"building_id": "building-1"}


def register():
    return {
        OP: {"baseline": 1., "bounds": [.5, 2.]},
        WINDOW: {"baseline": 1., "bounds": [.4, 1.2]},
        WALL: {"baseline": 1., "bounds": [.8, 1.6], "resistance_basis": "total_assembly_r"},
        ROOF: {"baseline": 1., "bounds": [.8, 1.6], "resistance_basis": "total_assembly_r"},
        "recovery": {"baseline": .85, "bounds": [.6, .95]},
    }


def native(settings):
    return (
        100 + 20 * (settings.get(OP, 1.) - 1.)
        + 40 * (settings.get(WINDOW, 1.) - 1.)
        + 30 * (1 / settings.get(WALL, 1.) - 1.)
        + 10 * (1 / settings.get(ROOF, 1.) - 1.)
    )


def block(active):
    reg = register()
    designs = anchors(active, reg)
    fitted = fit_quadratic(designs, [native(p) for p in designs.values()], active, reg)
    return {
        "active": active, "register": reg, "designs": designs,
        "coefficients": {ROW["building_id"]: fitted["coefficients"]},
        "estimates": {ROW["building_id"]: {key: native(p) for key, p in designs.items()}},
        "native_fallback": {},
    }


def models():
    model = block([OP, WALL, ROOF])
    model["alternate_blocks"] = {"pooled": block([OP, WINDOW])}
    return model


def test_separate_window_block_does_not_add_fourth_primary_axis():
    defs = emulator_blocks([OP, WALL, ROOF], [OP, WINDOW], register())
    assert defs == {"primary": [OP, WALL, ROOF], "pooled": [OP, WINDOW]}
    assert emulator_blocks([OP, WINDOW, WALL], [OP, WINDOW], register()) == {
        "primary": [OP, WINDOW, WALL],
    }
    assert emulator_blocks([], [], {}) == {"primary": []}


@pytest.mark.parametrize("primary,pooled", [([OP, WINDOW, WALL, ROOF], []), ([OP, OP], []), ([OP], [OP, WINDOW, WALL]), ([OP], [WINDOW, WINDOW])])
def test_definition_rejects_excess_or_duplicate_axes(primary, pooled):
    with pytest.raises(ValueError):
        emulator_blocks(primary, pooled, register())


def test_definition_requires_physical_total_r_semantics():
    reg = register()
    del reg[WALL]["resistance_basis"]
    with pytest.raises(ValueError, match="total assembly R"):
        emulator_blocks([OP, WALL], [OP, WINDOW], reg)


def test_window_request_uses_pooled_prediction_instead_of_ignored_axis():
    model = models()
    setting = {OP: 1.25, WINDOW: .7}
    assert select_block(setting, model) == "pooled"
    assert predict_estimate(ROW, setting, model) == pytest.approx(native(setting))
    assert predict_estimate(ROW, setting, model) != pytest.approx(native({OP: 1.25}))
    primary = {OP: 1.25, WALL: 1.4, ROOF: 1.1}
    assert select_block(primary, model) == "primary"
    assert predict_estimate(ROW, primary, model) == pytest.approx(native(primary))


def test_cross_block_combination_is_rejected_not_added_or_ignored():
    setting = {WINDOW: .7, WALL: 1.4}
    assert not settings_supported(setting, models())
    with pytest.raises(UnsupportedSettingsError, match="non-prior controls"):
        predict_estimate(ROW, setting, models())


def test_legacy_model_rejects_unsupported_nonprior_window():
    model = block([OP, WALL, ROOF])
    with pytest.raises(UnsupportedSettingsError):
        predict_estimate(ROW, {WINDOW: .7}, model)
    assert predict_estimate(ROW, {WINDOW: 1.}, model) == pytest.approx(100)


def test_actual_priors_not_one_define_inactive_and_default_coordinates():
    model = {"active": [OP], "register": register(), "coefficients": {ROW["building_id"]: [120, 10, 0]}}
    model["register"][OP]["baseline"] = .9
    model["register"][WINDOW]["baseline"] = 1.1
    assert predict_estimate(ROW, {}, model) == 120
    assert predict_estimate(ROW, {WINDOW: 1.1, OP: .9}, model) == 120
    with pytest.raises(UnsupportedSettingsError):
        predict_estimate(ROW, {WINDOW: 1.}, model)
    assert select_block({}, model) == "primary"


def test_primary_preference_and_baseline_keys_do_not_change_route():
    model = models()
    assert select_block({}, model) == "primary"
    assert select_block({OP: 1.2, WINDOW: 1.}, model) == "primary"
    assert select_block({OP: 1.2, WALL: 1., WINDOW: .8}, model) == "pooled"


@pytest.mark.parametrize("setting", [{"target": 70}, {WINDOW: np.nan}, {WINDOW: True}, {WINDOW: None}, {WINDOW: .1}])
def test_invalid_settings_raise_even_in_support_check(setting):
    with pytest.raises(ValueError):
        settings_supported(setting, models())


def test_pooled_register_inherits_or_must_match_primary_prior():
    model = models()
    del model["alternate_blocks"]["pooled"]["register"]
    assert select_block({WINDOW: .7}, model) == "pooled"
    model = models()
    model["alternate_blocks"]["pooled"]["register"][OP]["baseline"] = .9
    with pytest.raises(ValueError, match="physical prior"):
        select_block({WINDOW: .7}, model)


def test_equivalent_tuple_and_list_bounds_have_same_coordinates():
    model = models()
    model["alternate_blocks"]["pooled"]["register"][OP]["bounds"] = (.5, 2.)
    assert predict_estimate(ROW, {WINDOW: .7, OP: 1.2}, model) == pytest.approx(native({WINDOW: .7, OP: 1.2}))


def test_native_fallback_default_uses_actual_nonone_register_prior():
    model = {"active": [OP], "register": register(), "coefficients": {}, "designs": {"prior": {OP: .9}}, "estimates": {ROW["building_id"]: {"prior": 120.}}}
    model["register"][OP]["baseline"] = .9
    assert predict_estimate(ROW, {}, model) == 120
    assert predict_estimate(ROW, {OP: 1.}, model) is None


def test_native_fallback_routes_pooled_and_matches_actual_prior_defaults():
    model = models()
    pooled = model["alternate_blocks"]["pooled"]
    pooled["coefficients"] = {}
    pooled["native_fallback"] = {ROW["building_id"]: "joint_check_failure"}
    pooled["designs"] = {"known": {WINDOW: .7}}
    pooled["estimates"] = {ROW["building_id"]: {"known": 88.}}
    assert predict_estimate(ROW, {WINDOW: .7, OP: 1., WALL: 1.}, model) == 88
    assert predict_estimate(ROW, {WINDOW: .71}, model) is None
    assert predict_estimate(ROW, {WINDOW: .7, OP: 1.1}, model) is None


def test_rejected_validation_cannot_use_leftover_coefficients():
    model = models()
    pooled = model["alternate_blocks"]["pooled"]
    pooled["native_fallback"] = {ROW["building_id"]: "joint_check_failure"}
    pooled["designs"] = {"failed": {WINDOW: .7}}
    pooled["estimates"] = {ROW["building_id"]: {"failed": None}}
    assert predict_estimate(ROW, {WINDOW: .7}, model) is None
    del pooled["estimates"][ROW["building_id"]]
    assert predict_estimate(ROW, {WINDOW: .7}, model) is None


def test_native_design_cannot_hide_unsupported_physical_change():
    model = block([OP, WALL])
    model["coefficients"] = {}
    model["designs"] = {"wrong": {OP: 1.2, WINDOW: .7}}
    model["estimates"] = {ROW["building_id"]: {"wrong": 90.}}
    with pytest.raises(ValueError, match="fallback design"):
        predict_estimate(ROW, {OP: 1.2}, model)


@pytest.mark.parametrize("bad", [None, np.inf, -1.])
def test_failed_or_invalid_native_values_are_not_manufactured(bad):
    model = block([OP])
    model["coefficients"] = {}
    model["designs"] = {"baseline": {}}
    model["estimates"] = {ROW["building_id"]: {"baseline": bad}}
    if bad is None:
        assert predict_estimate(ROW, {}, model) is None
    else:
        with pytest.raises(ValueError):
            predict_estimate(ROW, {}, model)


def test_building_identity_alone_routes_without_target_or_row_order():
    model = models()
    before = deepcopy(model)
    setting = {WINDOW: .7}
    assert predict_estimate(ROW, setting, model) == predict_estimate(ROW | {"target": 999}, setting, model)
    assert model == before
    assert predict_estimate({"building_id": "absent"}, setting, model) is None


def test_support_check_does_not_require_fitted_artifacts():
    model = {"active": [OP, WALL], "register": register(), "alternate_blocks": {"pooled": {"active": [OP, WINDOW]}}}
    assert settings_supported({WINDOW: .7}, model)
    assert settings_supported({WALL: 1.4}, model)
    assert not settings_supported({WALL: 1.4, WINDOW: .7}, model)
