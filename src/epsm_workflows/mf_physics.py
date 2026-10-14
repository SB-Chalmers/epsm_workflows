"""Shared MF physical controls and conservative cohort candidate selection.

Resistance factors in this module refer to nominal total assembly resistance,
including declared surface films. They change an existing reviewed insulation
layer only. This is deliberately opt-in: historical cohort campaigns retain
EPSM Data's original layer-thickness multiplier semantics.
"""

from __future__ import annotations

import math

import numpy as np

from .mf_evidence import insulation_candidates

TOTAL_ASSEMBLY_R = "total_assembly_r"
RESISTANCE_ROLES = {
    "wall_resistance": "wall",
    "roof_resistance": "top",
    "ground_resistance": "ground",
    "basement_wall_resistance": "basement_wall",
}


def exact_resistance(construction, factor):
    """Return an insulation-only change achieving exactly ``factor * total R``.

    Values below one are allowed if a positive insulation thickness remains.
    Timber, masonry, concrete, other retained layers and films stay unchanged.
    Uninsulated assemblies need an explicitly sourced overlay, not a multiplier.
    """
    if isinstance(factor, bool) or not isinstance(factor, (int, float)):
        raise ValueError("Resistance factor must be finite and positive")  # noqa: TRY004
    if not math.isfinite(factor) or factor <= 0:
        raise ValueError("Resistance factor must be finite and positive")
    layers = list(construction.layers)
    candidates = insulation_candidates(construction)
    if not candidates:
        raise ValueError("No insulation layer; cannot change R by scaling structure")
    index = max(
        candidates,
        key=lambda i: layers[i].thickness_m / layers[i].material.conductivity_w_mk,
    )
    layer = layers[index]
    before_r = 1 / construction.u_value_w_m2k
    change_r = (factor - 1) * before_r
    after_depth = layer.thickness_m + change_r * layer.material.conductivity_w_mk
    if not math.isfinite(after_depth) or after_depth <= 0:
        raise ValueError("Requested total R incompatible with retained non-insulation layers")
    layers[index] = layer.updated(thickness_m=after_depth)
    replacement = construction.updated(layers=tuple(layers))
    if not math.isclose(1 / replacement.u_value_w_m2k, factor * before_r, rel_tol=1e-10):
        raise ValueError("Total assembly resistance target was not preserved")
    return replacement, {
        "factor": float(factor),
        "resistance_basis": TOTAL_ASSEMBLY_R,
        "before_r_m2k_w": before_r,
        "after_r_m2k_w": 1 / replacement.u_value_w_m2k,
        "surface_resistance_m2k_w": construction.surface_resistance_m2k_w,
        "before_u": construction.u_value_w_m2k,
        "after_u": replacement.u_value_w_m2k,
        "layer": index,
        "before_depth_m": layer.thickness_m,
        "after_depth_m": after_depth,
        "added_depth_mm": (after_depth - layer.thickness_m) * 1000,
        "material": layer.material.model_dump(mode="json"),
        "classification": "engineering_scenario_not_observed_assembly",
    }


def apply_physical_profile(spec, profile, *, locked_parameters=(), resistance_basis=None):
    """Apply a bounded physical profile while preserving legacy campaign meaning.

    ``total_assembly_r`` must be declared either here or on the profile. Other
    controls, including Atemp-based infiltration, retain EPSM Data semantics.
    Source and bounds are validated before independently locked inputs are
    removed, matching the underlying profile contract.
    """
    from epsm_data.cohorts import apply_cohort_profile

    declared = profile.get("resistance_basis")
    if resistance_basis is not None and declared is not None and declared != resistance_basis:
        raise ValueError("Conflicting resistance basis declarations")
    basis = resistance_basis if resistance_basis is not None else declared
    if basis is None and "wall_insulation_overlay_m" not in profile.get("parameters", {}):
        return apply_cohort_profile(spec, profile, locked_parameters=locked_parameters)
    if basis not in (None, TOTAL_ASSEMBLY_R):
        raise ValueError(f"Unsupported resistance basis: {basis}")
    if not str(profile.get("source", "")).strip():
        raise ValueError("Effective cohort assumptions require source provenance")
    parameters = dict(profile.get("parameters", {}))
    bounds = profile.get("bounds", {})
    for name, value in parameters.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or name not in bounds
            or len(bounds[name]) != 2
            or not bounds[name][0] <= value <= bounds[name][1]
        ):
            raise ValueError(f"Cohort parameter outside frozen bounds: {name}")
    locked = set(locked_parameters)
    if "wall_resistance" in locked:
        locked.add("wall_insulation_overlay_m")
    envelope = spec.envelope
    overlay_depth = parameters.pop("wall_insulation_overlay_m", None)
    if overlay_depth is not None and "wall_insulation_overlay_m" not in locked:
        from epsm_core import MaterialSpec

        definition = profile.get("wall_overlay")
        if (
            not isinstance(definition, dict)
            or "material" not in definition
            or not str(definition.get("source", "")).strip()
        ):
            raise ValueError("Wall overlay profile requires sourced material definition")
        material = MaterialSpec.model_validate(definition["material"])
        if material.conductivity_w_mk > 0.1 or material.density_kg_m3 > 200:
            raise ValueError("Wall overlay requires declared low-density insulation")
        if overlay_depth < 0:
            raise ValueError("Wall overlay thickness cannot be negative")
        if overlay_depth > 0:
            wall, _ = add_insulation_overlay(
                envelope.wall, material=material, thickness_m=overlay_depth,
                source=definition["source"],
            )
            envelope = envelope.updated(wall=wall)
    roles = RESISTANCE_ROLES if basis == TOTAL_ASSEMBLY_R else {}
    for name, role in roles.items():
        if name not in parameters:
            continue
        value = parameters.pop(name)
        if name in locked or value == 1:
            continue
        construction = getattr(envelope, role)
        if construction is None:
            raise ValueError(f"Missing construction for {name}")
        changed, _ = exact_resistance(construction, value)
        envelope = envelope.updated(**{role: changed})
    remaining = dict(profile, parameters=parameters)
    return apply_cohort_profile(
        spec.updated(envelope=envelope), remaining, locked_parameters=locked
    )


def add_insulation_overlay(
    construction, *, material, thickness_m, source, installation_year=None, side="exterior"
):
    """Add a declared insulation layer without changing the original assembly.

    The caller supplies a sourced native MaterialSpec. There is no automatic
    insulation material, installation period, or thermal-bridge correction.
    Core constructions use outside-to-inside ordering, so exterior additions
    precede the retained layers and interior additions follow them.
    """
    from epsm_core import LayerSpec

    if not isinstance(source, str) or not source.strip():
        raise ValueError("Insulation overlay requires source provenance")
    if isinstance(thickness_m, bool) or not isinstance(thickness_m, (int, float)):
        raise ValueError("Overlay thickness must be finite and positive")  # noqa: TRY004
    if not math.isfinite(thickness_m) or thickness_m <= 0:
        raise ValueError("Overlay thickness must be finite and positive")
    if material.conductivity_w_mk > 0.1 or material.density_kg_m3 > 200:
        raise ValueError("Overlay must use a declared low-density insulation material")
    if side not in ("exterior", "interior"):
        raise ValueError("Overlay side must be exterior or interior")
    if installation_year is not None and (
        isinstance(installation_year, bool) or not isinstance(installation_year, int)
    ):
        raise ValueError("Overlay installation year must be an integer")
    layer = LayerSpec(material=material, thickness_m=thickness_m)
    layers = (
        (layer,) + construction.layers
        if side == "exterior"
        else construction.layers + (layer,)
    )
    changed = construction.updated(layers=layers)
    return changed, {
        "source": source,
        "classification": "sourced_assembly_scenario_not_observed_installation",
        "installation_year": installation_year,
        "side": side,
        "thickness_m": float(thickness_m),
        "added_r_m2k_w": thickness_m / material.conductivity_w_mk,
        "material": material.model_dump(mode="json"),
        "before_u": construction.u_value_w_m2k,
        "after_u": changed.u_value_w_m2k,
        "original_layers_preserved": True,
    }


def prior_distance(parameters, register):
    """Squared distance from sourced baselines in frozen bound coordinates."""
    return float(
        sum(
            ((value - register[name]["baseline"]) / np.ptp(register[name]["bounds"])) ** 2
            for name, value in parameters.items()
            if register[name].get("baseline") is not None
            and np.ptp(register[name]["bounds"]) > 0
        )
    )


def select_candidate(candidates, baseline, *, tolerance=1e-9):
    """Choose coverage first subject to no MAE deterioration versus baseline.

    Objects have ``id``, ``metrics`` (coverage20, mae_kwh_m2, bias_kwh_m2),
    and optional ``prior_distance``. The supplied baseline always remains an
    option. Incomplete/non-finite candidate metrics cannot win. Ties preserve
    the baseline when bias and prior distance are identical.
    """
    score = baseline["metrics"]
    baseline_mae = score.get("mae_kwh_m2")
    if baseline_mae is None or not math.isfinite(baseline_mae):
        raise ValueError("Selection requires a finite baseline MAE")

    def eligible(candidate):
        metrics = candidate["metrics"]
        values = [metrics.get(k) for k in ("coverage20", "mae_kwh_m2", "bias_kwh_m2")]
        return all(v is not None and math.isfinite(v) for v in values) and (
            metrics["mae_kwh_m2"] <= baseline_mae + tolerance
        )

    choices = [baseline] + [candidate for candidate in candidates if eligible(candidate)]

    def rank(candidate):
        metrics = candidate["metrics"]
        return (
            -metrics["coverage20"],
            abs(metrics["bias_kwh_m2"]),
            candidate.get("prior_distance", 0.0),
            candidate is not baseline,
            str(candidate.get("id", "")),
        )

    return min(choices, key=rank)


def independent_controls(response_vectors, *, effects=None, max_controls=3, condition_limit=20):
    """Select at most three uncentred, informative physical response directions.

    Vectors contain target-free simulated changes across the same buildings.
    Zero effects, missing common responses and condition number >=20 are
    retained as excluded/confounded diagnostics. Affine conditional responses
    are not spuriously removed by centring the vectors.
    """
    if not 1 <= max_controls <= 3 or condition_limit <= 1:
        raise ValueError("Require one to three controls and condition_limit >1")
    vectors = {name: np.asarray(values, dtype=float) for name, values in response_vectors.items()}
    if any(v.ndim != 1 for v in vectors.values()) or len({len(v) for v in vectors.values()}) > 1:
        raise ValueError("Response vectors must have the same one-dimensional shape")
    effects = effects or {}

    def strength(name):
        effect = effects.get(name)
        if isinstance(effect, dict):
            effect = effect.get("mu_star")
        if effect is not None:
            return float(effect)
        values = vectors[name][np.isfinite(vectors[name])]
        return float(np.mean(np.abs(values))) if len(values) else 0.0

    active, confounding, excluded = [], [], []
    for name in sorted(vectors, key=lambda n: (-strength(n), n)):
        if strength(name) < 1e-3:
            excluded.append({"parameter": name, "reason": "negligible_response"})
            continue
        if len(active) >= max_controls:
            excluded.append({"parameter": name, "reason": "maximum_three_controls"})
            continue
        matrix = np.column_stack([vectors[n] for n in active + [name]])
        complete = np.isfinite(matrix).all(axis=1)
        if complete.sum() < max(3, matrix.shape[1]):
            excluded.append({"parameter": name, "reason": "insufficient_complete_responses"})
            continue
        response = matrix[complete]
        norms = np.linalg.norm(response, axis=0)
        if np.any(norms <= 1e-9):
            excluded.append({"parameter": name, "reason": "zero_response"})
            continue
        singular = np.linalg.svd(response / norms, compute_uv=False)
        ratio = float(singular[-1] / singular[0])
        if ratio <= 1 / condition_limit:
            confounding.append(
                {
                    "parameter": name,
                    "with": active.copy(),
                    "relative_min_singular_value": ratio,
                    "condition_number": float(1 / ratio) if ratio else None,
                    "criterion": f"uncentred response condition number >={condition_limit:g}",
                }
            )
        else:
            active.append(name)
    return {"active": active, "confounding": confounding, "excluded": excluded}
