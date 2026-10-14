"""Target-blind physical coordinates and native anchors for MF emulators.

The transformation improves numerical representation of transmission response;
it neither adds fitted controls nor relaxes native validation. EnergyPlus still
decides whether an emulator is accurate enough for an individual building.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from numbers import Real

import numpy as np

RESISTANCE_CONTROLS = {
    "wall_resistance",
    "roof_resistance",
    "ground_resistance",
    "basement_wall_resistance",
}
OVERLAY_CONTROL = "wall_insulation_overlay_m"


def _number(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")  # noqa: TRY004
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        raise ValueError(f"Invalid {name}")
    return number


def _register(active, register):
    active = list(active)
    if len(active) != len(set(active)):
        raise ValueError("Emulator controls must be distinct")
    if len(active) > 3:
        raise ValueError("MF emulator fitting supports at most three shared controls")
    result = {}
    for name in active:
        if name not in register or not isinstance(register[name], Mapping):
            raise ValueError(f"Missing emulator register entry: {name}")
        entry = register[name]
        bounds = entry.get("bounds")
        if not isinstance(bounds, Sequence) or len(bounds) != 2:
            raise ValueError(f"{name} requires finite bounds")
        lo, hi = (_number(value, f"{name} bound") for value in bounds)
        baseline = _number(entry.get("baseline"), f"{name} baseline")
        if not lo < hi or not lo <= baseline <= hi:
            raise ValueError(f"Invalid bounds or prior for {name}")
        if name in RESISTANCE_CONTROLS:
            if entry.get("resistance_basis") != "total_assembly_r":
                raise ValueError(
                    "U coordinates require declared total assembly R semantics"
                )
            if lo <= 0:
                raise ValueError("Total resistance factors must be positive")
            coordinate = "normalized_change_in_relative_u"
        elif name == OVERLAY_CONTROL:
            _number(
                entry.get("baseline_wall_r_m2k_w"),
                "baseline_wall_r_m2k_w",
                positive=True,
            )
            _number(
                entry.get("overlay_conductivity_w_mk"),
                "overlay_conductivity_w_mk",
                positive=True,
            )
            if lo < 0:
                raise ValueError("Insulation overlay depth cannot be negative")
            coordinate = "normalized_change_in_wall_u_from_overlay"
        else:
            coordinate = "normalized_linear_parameter"
        result[name] = dict(entry) | {
            "bounds": [lo, hi],
            "baseline": baseline,
            "coordinate_basis": coordinate,
        }
    return result


def _physical_value(value, name, entry):
    if name in RESISTANCE_CONTROLS:
        return 1 / value
    if name == OVERLAY_CONTROL:
        return 1 / (
            entry["baseline_wall_r_m2k_w"] + value / entry["overlay_conductivity_w_mk"]
        )
    return value


def physical_coordinates(settings, active, register):
    """Return ``[1, z..., z²...]`` in physically appropriate frozen coordinates.

    Total-R factors use relative U=1/factor. Overlay depth uses the original wall
    assembly R including surface films and the declared insulation conductivity.
    Other controls remain linear. Every coordinate is centred on its actual prior
    and scaled by its physical bound span. Missing overlay physics is an error;
    linear depth is never silently substituted.
    """
    register = _register(active, register)
    z = []
    for name, entry in register.items():
        value = _number(settings.get(name, entry["baseline"]), name)
        lo, hi = entry["bounds"]
        tolerance = 1e-12 * max(1, abs(lo), abs(hi))
        if value < lo - tolerance or value > hi + tolerance:
            raise ValueError(f"Emulator setting outside frozen bounds: {name}")
        value = min(hi, max(lo, value))
        start = _physical_value(entry["baseline"], name, entry)
        span = abs(_physical_value(hi, name, entry) - _physical_value(lo, name, entry))
        if not math.isfinite(span) or span <= 0:
            raise ValueError(f"Degenerate physical bound span for {name}")
        z.append((_physical_value(value, name, entry) - start) / span)
    z = np.array(z)
    return np.r_[1.0, z, z * z]


def _key(settings):
    payload = json.dumps(
        settings, sort_keys=True, allow_nan=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def anchors(active, register):
    """Freeze native axis lo/midpoint/hi/prior anchors with at least three levels.

    The baseline already contains every actual prior. Equivalent prior anchors
    are deduplicated by their coordinates, avoiding repeated native simulations.
    This remains full rank when an actual prior is at a bound, including zero
    insulation-overlay depth.
    """
    register = _register(active, register)
    result = {"baseline": {}}
    seen = {tuple(physical_coordinates({}, active, register))}
    for name, entry in register.items():
        lo, hi = entry["bounds"]
        levels = sorted({lo, (lo + hi) / 2, hi, entry["baseline"]})
        for value in levels:
            setting = {name: value}
            coordinate = tuple(physical_coordinates(setting, active, register))
            if coordinate in seen:
                continue
            seen.add(coordinate)
            result[_key(setting)] = setting
    design_matrix(list(result.values()), active, register)
    return result


def design_matrix(settings, active, register, *, require_full_rank=True):
    """Check identifiability before fitting; ridge cannot rescue missing anchors."""
    settings = (
        list(settings.values()) if isinstance(settings, Mapping) else list(settings)
    )
    if not settings:
        raise ValueError("Emulator training requires native anchors")
    matrix = np.array(
        [physical_coordinates(item, active, register) for item in settings]
    )
    rank = int(np.linalg.matrix_rank(matrix))
    if require_full_rank and rank != matrix.shape[1]:
        raise ValueError(
            f"Rank-deficient emulator anchors: rank {rank}, required {matrix.shape[1]}"
        )
    return matrix


def fit_quadratic(settings, native_predictions, active, register):
    """Fit complete native anchors after checking their physical matrix rank."""
    settings = (
        list(settings.values()) if isinstance(settings, Mapping) else list(settings)
    )
    matrix = design_matrix(settings, active, register)
    predictions = list(native_predictions)
    if len(predictions) != len(settings):
        raise ValueError("Native anchors and predictions must align")
    values = [_number(value, "native heating prediction") for value in predictions]
    if any(value < 0 for value in values):
        raise ValueError("Native heating predictions cannot be negative")
    coefficients = np.linalg.lstsq(matrix, values, rcond=None)[0]
    return {
        "coefficients": coefficients.tolist(),
        "rank": int(np.linalg.matrix_rank(matrix)),
        "required_rank": matrix.shape[1],
        "condition_number": float(np.linalg.cond(matrix)),
        "anchor_count": len(settings),
        "coordinate_bases": {
            name: entry["coordinate_basis"]
            for name, entry in _register(active, register).items()
        },
        "energy_targets_used": False,
        "status": "requires_held_out_native_validation",
    }


def validate_native_checks(
    settings, native_predictions, coefficients, active, register
):
    """Apply the existing per-building 2% native held-out gate without relaxation."""
    settings = (
        list(settings.values()) if isinstance(settings, Mapping) else list(settings)
    )
    predictions = list(native_predictions)
    if not settings or len(settings) != len(predictions):
        raise ValueError("Held-out native checks must be nonempty and aligned")
    coefficients = np.asarray(coefficients, dtype=float)
    if (
        coefficients.ndim != 1
        or len(coefficients) != 1 + 2 * len(active)
        or not np.isfinite(coefficients).all()
    ):
        raise ValueError("Invalid native emulator coefficients")
    errors = []
    for setting, value in zip(settings, predictions, strict=True):
        if (
            value is None
            or isinstance(value, bool)
            or not isinstance(value, Real)
            or not math.isfinite(value)
            or value < 0
        ):
            return {
                "validated": False,
                "native_fallback": True,
                "reason": "held_out_native_failure",
                "maximum_relative_error": None,
            }
        estimate = float(physical_coordinates(setting, active, register) @ coefficients)
        errors.append(abs(estimate - value) / max(value, 1e-9))
    maximum = max(errors)
    return {
        "validated": maximum <= 0.02,
        "native_fallback": maximum > 0.02,
        "reason": None if maximum <= 0.02 else "held_out_native_error_above_2_percent",
        "maximum_relative_error": maximum,
        "relative_error_limit": 0.02,
        "energy_targets_used": False,
    }
