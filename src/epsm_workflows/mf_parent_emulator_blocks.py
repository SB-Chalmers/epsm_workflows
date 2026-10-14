"""Target-blind MF emulator blocks with complete, explicit control routing.

The selected operating/fabric model remains the top-level primary block. A
separately validated pooled operating/window block is needed only when those
controls are not contained in primary. Predictions never combine coefficients
from different blocks, and no requested non-prior control is silently ignored.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from numbers import Real

import numpy as np

from .mf_emulator import physical_coordinates


class UnsupportedSettingsError(ValueError):
    """No single validated block represents the requested physical settings."""


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")  # noqa: TRY004
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _same(left, right):
    return math.isclose(left, right, rel_tol=1e-10, abs_tol=1e-10)


def _active(names, register, *, maximum, label):
    if isinstance(names, (str, bytes)) or not isinstance(names, Sequence):
        raise ValueError(f"{label} controls must be a sequence")  # noqa: TRY004
    names = list(names)
    if any(not isinstance(name, str) or not name for name in names):
        raise ValueError(f"{label} control names must be nonempty strings")
    if len(names) != len(set(names)) or len(names) > maximum:
        raise ValueError(f"{label} requires distinct controls, at most {maximum}")
    if not isinstance(register, Mapping):
        raise ValueError("An explicit emulator register is required")  # noqa: TRY004
    physical_coordinates({}, names, register)
    return names


def emulator_blocks(primary_active, pooled_active, register):
    """Define separate native blocks without adding a diagnostic fitting axis.

    Independent control selection belongs to the sensitivity screen. This helper
    enforces three axes per primary block and two per pooled block, validates
    physical coordinates/actual priors, and preserves the selected axis order.
    The returned values are active-name lists for the existing model builder.
    """
    primary = _active(primary_active, register, maximum=3, label="Primary")
    pooled = _active(pooled_active, register, maximum=2, label="Pooled")
    result = {"primary": primary}
    if not set(pooled).issubset(primary):
        result["pooled"] = pooled
    return result


def _settings(settings, register):
    if not isinstance(settings, Mapping):
        raise ValueError("Emulator settings must be an explicit mapping")  # noqa: TRY004
    values, changed = {}, set()
    for name, raw in settings.items():
        if name not in register or not isinstance(register[name], Mapping):
            raise ValueError(f"Unregistered emulator control: {name}")
        entry = register[name]
        value = _number(raw, name)
        baseline = _number(entry.get("baseline"), f"{name} baseline")
        bounds = entry.get("bounds")
        if bounds is not None:
            if isinstance(bounds, (str, bytes)) or not isinstance(bounds, Sequence) or len(bounds) != 2:
                raise ValueError(f"Invalid emulator bounds: {name}")
            lo, hi = (_number(item, f"{name} bound") for item in bounds)
            if not lo < hi or not lo <= baseline <= hi:
                raise ValueError(f"Invalid emulator bounds/prior: {name}")
            tolerance = 1e-12 * max(1, abs(lo), abs(hi))
            if value < lo - tolerance or value > hi + tolerance:
                raise ValueError(f"Emulator setting outside frozen bounds: {name}")
        values[name] = value
        if not _same(value, baseline):
            changed.add(name)
    return values, changed


def _signature(name, register):
    entry = register[name]
    signature = {
        key: entry.get(key)
        for key in (
            "baseline", "bounds", "resistance_basis",
            "baseline_wall_r_m2k_w", "overlay_conductivity_w_mk",
        )
    }
    signature["bounds"] = tuple(signature["bounds"])
    return signature


def _blocks(model):
    if not isinstance(model, Mapping) or not isinstance(model.get("register"), Mapping):
        raise ValueError("Primary emulator requires a register")  # noqa: TRY004
    register = model["register"]
    primary = _active(model.get("active"), register, maximum=3, label="Primary")
    blocks = {"primary": (model, primary, register)}
    alternates = model.get("alternate_blocks", {})
    if not isinstance(alternates, Mapping) or set(alternates) - {"pooled"}:
        raise ValueError("Only the declared pooled alternate block is supported")
    for name, block in alternates.items():
        if not isinstance(block, Mapping):
            raise ValueError("Pooled alternate must be an explicit model")  # noqa: TRY004
        block_register = block.get("register", register)
        active = _active(block.get("active"), block_register, maximum=2, label="Pooled")
        for control in active:
            if control not in register or _signature(control, register) != _signature(control, block_register):
                raise ValueError(f"Alternate register differs from primary physical prior: {control}")
        blocks[name] = block, active, block_register
    return blocks


def _route(settings, model):
    blocks = _blocks(model)
    values, changed = _settings(settings, model["register"])
    for name, (block, active, register) in blocks.items():
        if changed.issubset(active):
            return name, block, active, register, values
    raise UnsupportedSettingsError(
        "No emulator block covers requested non-prior controls: " + ", ".join(sorted(changed))
    )


def select_block(settings, model):
    """Return ``primary`` or ``pooled``; prefer primary for overlapping support.

    Known inactive controls are allowed only at their actual registered prior.
    Unknown controls, malformed values and invalid bounds are errors. A request
    spanning separate blocks is unsupported; it cannot combine their outputs.
    """
    return _route(settings, model)[0]


def settings_supported(settings, model):
    """Test block coverage for child candidates without needing fitted models.

    Only a valid request lacking a covering block returns False. Invalid or
    unregistered values still raise, keeping configuration errors observable.
    """
    try:
        select_block(settings, model)
    except UnsupportedSettingsError:
        return False
    return True


def predict_estimate(row, settings, model):
    """Route a per-building prediction through one complete validated block.

    ``model`` retains the existing primary fields and optional
    ``alternate_blocks['pooled']`` with the same fields. Only building_id is read
    from row. A rejected native validation takes precedence over coefficients.
    Missing/failed exact native designs return None; nearest designs are never
    substituted and failed predictions are never renormalized or manufactured.
    """
    if not isinstance(row, Mapping) or not row.get("building_id"):
        raise ValueError("A building identity is required")
    bid = row["building_id"]
    _, block, active, register, values = _route(settings, model)
    coefficients = block.get("coefficients", {})
    if bid in coefficients and bid not in block.get("native_fallback", {}):
        coef = np.asarray(coefficients[bid], dtype=float)
        if coef.ndim != 1 or len(coef) != 1 + 2 * len(active) or not np.isfinite(coef).all():
            raise ValueError("Invalid emulator coefficients")
        estimate = float(physical_coordinates(values, active, register) @ coef)
        if not math.isfinite(estimate):
            raise ValueError("Invalid emulator prediction")
        return max(0.0, estimate)
    for name, raw_design in block.get("designs", {}).items():
        design, changed = _settings(raw_design, model["register"])
        if not changed.issubset(active):
            raise ValueError("Native fallback design contains an unsupported non-prior control")
        names = set(active) | set(values) | set(design)
        if all(
            _same(
                values.get(control, model["register"][control]["baseline"]),
                design.get(control, model["register"][control]["baseline"]),
            )
            for control in names
        ):
            native = block.get("estimates", {}).get(bid, {}).get(name)
            if native is None:
                return None
            native = _number(native, "native fallback prediction")
            if native < 0:
                raise ValueError("Native heating predictions cannot be negative")
            return native
    return None
