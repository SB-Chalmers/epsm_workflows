"""Explicit engineering bounds and physically applicable sensitivity designs.

References: prior parameter-sensitivity/LHS diagnostic ranges; EPSM Data reviewed
claim register; Core Sveby source resources. Multipliers beyond measured inputs
are effective cohort assumptions, never measured building properties. These
screening bounds are frozen before reading calibration outcomes.
"""

from __future__ import annotations

import numpy as np

REGISTER = {
    "wall_insulation_overlay_m": (
        0.0,
        0.15,
        0.0,
        "Explicit sourced insulation assembly scenario; not an observed renovation",
    ),
    "wall_resistance": (0.8, 1.2, 1.0, "Prior opaque insulation +/-20% diagnostic"),
    "roof_resistance": (0.8, 1.2, 1.0, "Prior opaque insulation +/-20% diagnostic"),
    "ground_resistance": (
        0.8,
        1.2,
        1.0,
        "Engineering extension of opaque resistance diagnostic",
    ),
    "basement_wall_resistance": (
        0.8,
        1.2,
        1.0,
        "Engineering extension; reported heated basement only",
    ),
    "window_u_multiplier": (
        0.4,
        1.2,
        1.0,
        "Reviewed matrix existing/standard alternatives; effective surviving-stock hypothesis",
    ),
    "wwr": (
        0.15,
        0.35,
        None,
        "Prior .20/.30 tests, extended +/- .05 engineering envelope",
    ),
    "shgc_multiplier": (
        0.8,
        1.2,
        1.0,
        "Engineering uncertainty around reviewed glazing",
    ),
    "solar_shading_factor": (
        0.5,
        1.0,
        None,
        "Core incident-solar factor; geometry shading remains independent",
    ),
    "infiltration_multiplier": (
        0.5,
        2.0,
        1.0,
        "Prior .2/.3/.4 ACH diagnostic; multiplier does not identify n50",
    ),
    "natural_exchange_multiplier": (
        0.5,
        1.5,
        1.0,
        "Effective natural exchange; BETSI total ACH is not additional leakage",
    ),
    "flow_multiplier": (
        0.8,
        1.2,
        1.0,
        "Prior operating-flow +/-20% screen; reported design is not actual flow",
    ),
    "recovery": (
        0.5,
        0.85,
        None,
        "Engineering FTX operating effectiveness around .70, not rated product performance",
    ),
    "people_multiplier": (0.8, 1.2, 1.0, "Prior Sveby occupancy +/-20% diagnostic"),
    "equipment_multiplier": (
        0.8,
        1.2,
        1.0,
        "Prior normal-use electrical heat +/-20% diagnostic",
    ),
    "lighting_multiplier": (
        0.8,
        1.2,
        1.0,
        "Engineering extension; separate lighting when nonzero",
    ),
    "setpoint_offset_c": (
        -2.0,
        2.0,
        0.0,
        "Engineering uncertainty around source thermostat; target normalization caveat",
    ),
    "schedule_shift_hours": (
        -2.0,
        2.0,
        0.0,
        "Declared clock-placement scenario; source annual integrals conserved",
    ),
    "thermal_bridge_uplift": (
        0.0,
        0.2,
        None,
        "Prior equivalent-construction 0/10/20% diagnostic; check source inclusion",
    ),
    "soil_conductivity": (
        1.0,
        2.5,
        None,
        "Prior native Kiva soil sensitivity; soil unobserved",
    ),
    "basement_setpoint_c": (
        11.0,
        21.0,
        None,
        "Existing EPC basement_definition=heated_above_10C (Boverket form2.8); effective full-zone setpoint11-21C, extent unresolved",
    ),
    "attic_exchange_ach": (0.0, 2.0, None, "Prior cold-attic 0/1/2 ACH scenario"),
    "net_loss_kwh_m2": (
        0.0,
        10.0,
        None,
        "Prior 0/5/10 net losses; exclude recovered heat",
    ),
    "delivery_efficiency": (
        0.8,
        1.0,
        None,
        "Engineering combustion delivery range; district-heat interface fixed at 1",
    ),
}


def applicable(spec, *, locked=(), overrides=None, resistance_basis=None, wall_overlay=None):
    """Remove inapplicable/observed controls, retaining a full parameter audit."""
    from ..mf_evidence import insulation_candidates
    from ..mf_physics import RESISTANCE_ROLES, TOTAL_ASSEMBLY_R

    if resistance_basis not in (None, TOTAL_ASSEMBLY_R):
        raise ValueError(f"Unsupported resistance basis: {resistance_basis}")
    if wall_overlay is not None:
        from epsm_core import MaterialSpec

        if not str(wall_overlay.get("source", "")).strip():
            raise ValueError("Wall overlay requires source provenance")
        material = MaterialSpec.model_validate(wall_overlay["material"])
        if material.conductivity_w_mk > 0.1 or material.density_kg_m3 > 200:
            raise ValueError("Wall overlay requires declared low-density insulation")
    locked = set(locked)
    if "wall_resistance" in locked:
        locked.add("wall_insulation_overlay_m")
    excluded = {
        "wall_insulation_overlay_m": wall_overlay is None or bool(insulation_candidates(spec.envelope.wall)),
        "basement_setpoint_c": not spec.geometry.basement_conditioned
        or not spec.geometry.basement_depth_m
        or bool(spec.zone_uses)
        or bool(spec.delivery.fvp)
        or len(set(spec.use.heating_setpoint.all_values)) != 1,
        "basement_wall_resistance": not spec.geometry.basement_conditioned
        or spec.envelope.basement_wall is None,
        "natural_exchange_multiplier": spec.airflow.topology != "S"
        or spec.airflow.natural_exchange_ach == 0,
        "flow_multiplier": spec.airflow.mechanical_vent_l_s_m2 == 0,
        "recovery": spec.airflow.topology != "FTX",
        "people_multiplier": spec.use.occupant_density_per_m2 == 0,
        "equipment_multiplier": spec.use.equipment_gain_w_m2 == 0,
        "lighting_multiplier": spec.use.lighting_gain_w_m2 == 0,
        "soil_conductivity": spec.envelope.foundation is None,
        "attic_exchange_ach": not any(
            (p.roof or spec.geometry.roof).topology == "cold_attic"
            for p in spec.geometry.parts
        ),
        "net_loss_kwh_m2": spec.delivery.heating is None,
        "delivery_efficiency": spec.delivery.heating is None
        or spec.delivery.heating.carrier
        in ("district_heat", "electric_direct", "heat_pump"),
        "wwr": bool(spec.facade.overrides),
    }
    if resistance_basis == TOTAL_ASSEMBLY_R:
        for name, role in RESISTANCE_ROLES.items():
            construction = getattr(spec.envelope, role)
            excluded[name] = excluded.get(name, False) or construction is None or not (
                insulation_candidates(construction)
            )
    baseline = {
        "basement_setpoint_c": spec.use.heating_setpoint.all_values[0]
        if len(set(spec.use.heating_setpoint.all_values)) == 1
        else None,
        "wwr": spec.facade.default_wwr,
        "solar_shading_factor": spec.facade.solar_shading_factor,
        "recovery": spec.airflow.sensible_recovery_effectiveness,
        "thermal_bridge_uplift": spec.envelope.thermal_bridge_uplift_fraction,
        "soil_conductivity": spec.envelope.foundation.soil_conductivity_w_mk
        if spec.envelope.foundation
        else None,
        "attic_exchange_ach": next(
            (
                x.outdoor_air_ach
                for x in spec.unconditioned_exchange
                if x.zone_kind == "attic"
            ),
            0.0,
        ),
        "net_loss_kwh_m2": spec.delivery.heating.net_distribution_loss_kwh_m2_year
        if spec.delivery.heating
        else None,
        "delivery_efficiency": spec.delivery.heating.efficiency
        if spec.delivery.heating
        else None,
    }
    result = {}
    for name, (lo, hi, center, source) in REGISTER.items():
        center = baseline.get(name) if center is None else center
        reason = (
            "independent_input_locked"
            if name in locked
            else "not_applicable"
            if excluded.get(name)
            else None
        )
        if overrides and name in overrides:
            lo, hi = overrides[name]["bounds"]
            source = overrides[name]["source"]
            if not source or not np.isfinite([lo, hi]).all() or lo >= hi:
                raise ValueError(f"Invalid declared bounds: {name}")
        if center is not None:
            # Always retain the actual baseline; never silently clamp a source value.
            lo, hi = min(lo, center), max(hi, center)
        result[name] = {
            "bounds": [float(lo), float(hi)],
            "baseline": center,
            "source": source,
            "classification": "engineering_assumption",
            "status": reason or "screen_and_cohort_calibration",
        }
        if resistance_basis == TOTAL_ASSEMBLY_R and name in RESISTANCE_ROLES:
            result[name]["resistance_basis"] = TOTAL_ASSEMBLY_R
            construction = getattr(spec.envelope, RESISTANCE_ROLES[name])
            if (
                reason == "not_applicable"
                and construction is not None
                and not insulation_candidates(construction)
            ):
                result[name]["reason"] = "no_reviewed_insulation_layer_sourced_overlay_required"
    return result


def morris_design(register, trajectories=4, seed=20261003):
    """Six-level Morris trajectories in normalized coordinates; no targets used."""
    names = [
        k for k, v in register.items() if v["status"] == "screen_and_cohort_calibration"
    ]
    rng = np.random.default_rng(seed)
    designs, moves = {}, []
    for trajectory in range(trajectories):
        point = rng.integers(0, 3, len(names)) / 5
        direction = rng.choice([-1, 1], len(names))
        point = np.where(direction < 0, point + 0.6, point)

        def settings(z):
            result = {
                name: float(
                    register[name]["bounds"][0]
                    + value * np.ptp(register[name]["bounds"])
                )
                for name, value in zip(names, z, strict=True)
            }
            if "schedule_shift_hours" in result:
                result["schedule_shift_hours"] = float(
                    round(result["schedule_shift_hours"])
                )
            return result

        previous = f"morris_{trajectory}_0"
        designs[previous] = settings(point)
        for step, index in enumerate(rng.permutation(len(names)), 1):
            updated = point.copy()
            updated[index] += 0.6 * direction[index]
            name = f"morris_{trajectory}_{step}"
            designs[name] = settings(updated)
            moves.append(
                {
                    "parameter": names[index],
                    "before": previous,
                    "after": name,
                    "normalized_step": 0.6 * int(direction[index]),
                }
            )
            previous, point = name, updated
    return designs, moves
