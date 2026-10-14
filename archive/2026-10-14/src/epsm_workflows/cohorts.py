"""Target-free assignment of versioned, calibrated surviving-stock profiles.

References: Kristensen et al., Energy and Buildings (2018),
https://www.sciencedirect.com/science/article/pii/S0378778818312532;
EPSM Workflows docs/cohort-transfer.md. Profiles describe inferred effective
assumptions, not observed renovations or changes to construction-reference data.
"""

import math
from copy import deepcopy

FORBIDDEN = {
    "target",
    "heat_kwh",
    "heating_intensity",
    "prediction",
    "residual",
    "energy_class",
    "ep",
    "dhw_share",
    "fastighetsel_share",
}


def descriptor_vector(descriptors, transform):
    """Apply training-fitted medians/scales; retain explicit missingness flags."""
    if set(descriptors) & FORBIDDEN:
        raise ValueError("Heating observations cannot enter cohort assignment")
    values = []
    for field in transform["numeric"]:
        raw = descriptors.get(field)
        missing = raw is None or not isinstance(raw, (float, int)) or not math.isfinite(raw)
        median = transform["medians"][field]
        value = median if missing else raw
        values.extend(((value - median) / transform["scales"][field], float(missing)))
    return values


def descriptor_distance(a, b, transform):
    """Mixed distance with equal numeric/categorical block weights.

    Missingness contributes a quarter of a numeric coordinate's weight; it is
    audited separately by the training workflow. Numeric differences are capped
    at three training IQRs so one scale cannot overwhelm all other descriptors.
    """
    av, bv = descriptor_vector(a, transform), descriptor_vector(b, transform)
    numeric = sum(
        min(abs(av[i] - bv[i]), 3) / 3 + 0.25 * abs(av[i + 1] - bv[i + 1])
        for i in range(0, len(av), 2)
    ) / max(1, len(av) // 2)
    cats = transform["categorical"]
    categorical = sum(str(a.get(c, "unknown")) != str(b.get(c, "unknown")) for c in cats) / max(
        1, len(cats)
    )
    return (numeric + categorical) / (2 if cats else 1)


def assign_cohort(descriptors, bundle, *, allow_provisional=False):
    """Resolve descriptors without energy, returning parent fallback out of domain.

    Bundle schema 1 has an exact parent mapping, descriptor transform and medoid
    profiles. Accepted profiles require retrospective transfer support; provisional
    profiles are available only by explicit opt-in. No input is mutated.
    """
    descriptor_vector(descriptors, bundle["transform"])
    if bundle.get("schema_version") != 1:
        raise ValueError("Unsupported cohort profile schema")
    if any(descriptors.get(k) != v for k, v in bundle["parent"].items()):
        return {
            "status": "parent_fallback",
            "reason": "outside_parent",
            "profile": None,
        }
    candidates = bundle["profiles"]
    if not candidates:
        return {"status": "parent_fallback", "reason": "no_profiles", "profile": None}
    distances = [
        descriptor_distance(descriptors, p["medoid"], bundle["transform"]) for p in candidates
    ]
    index = min(range(len(candidates)), key=lambda i: (distances[i], candidates[i]["id"]))
    selected = candidates[index]
    status = selected.get("status", "provisional")
    vector = descriptor_vector(descriptors, bundle["transform"])
    missing = sum(vector[1::2])
    reason = (
        "outside_descriptor_support"
        if distances[index] > selected["distance_limit"]
        else "missing_descriptors"
        if missing > len(bundle["transform"]["numeric"]) / 2
        else "unvalidated_profile"
        if status != "supported" and not allow_provisional
        else None
    )
    return {
        "status": "parent_fallback" if reason else status,
        "reason": reason,
        "distance": distances[index],
        "profile": None if reason else deepcopy(selected),
    }


def apply_cohort_profile(spec, profile, *, locked_parameters=()):
    """Apply bounded cohort assumptions to a native Core BuildingSpec.

    ``locked_parameters`` protects independently evidenced inputs. Geometry,
    climate, material conductivity/density and load area are never fitted here.
    Every change is checked against the frozen profile bounds and source record.
    """
    from epsm_core import ScheduleSpec, UnconditionedExchangeSpec, ZoneUseSpec

    if not profile.get("source"):
        raise ValueError("Effective cohort assumptions require source provenance")
    # DB 1.5.0 MF review: legacy fitted multipliers depend on the old ACH
    # baseline. Refitting/provenance is required before native-area application.
    if (spec.airflow.envelope_infiltration_l_s_m2 is not None
            and profile.get("infiltration_basis") != "atemp_area_rate"):
        raise ValueError("Cohort profile requires refit for the Atemp area-rate baseline")
    changes = dict(profile.get("parameters", {}))
    bounds = profile.get("bounds", {})
    for name, value in changes.items():
        if (
            name not in bounds
            or not math.isfinite(value)
            or not bounds[name][0] <= value <= bounds[name][1]
        ):
            raise ValueError(f"Cohort parameter outside frozen bounds: {name}")
    for name in locked_parameters:
        changes.pop(name, None)

    basement_setpoint = changes.pop("basement_setpoint_c", None)
    if not spec.geometry.basement_conditioned or not spec.geometry.basement_depth_m:
        basement_setpoint = None  # A cohort control has zero effect where its role is absent.
    if basement_setpoint is not None and (
        not spec.geometry.basement_conditioned or spec.zone_uses or spec.delivery.fvp
    ):
        raise ValueError(
            "Basement scenario requires a conditioned basement without evidenced zone overrides or FVP"
        )

    def scaled_construction(construction, factor):
        # Effective resistance variation uses layer thickness; reference materials
        # are retained. For uninsulated assemblies all layers participate explicitly.
        if factor == 1:
            return construction
        low_k = any(x.material.conductivity_w_mk <= 0.2 for x in construction.layers)
        return construction.updated(
            layers=tuple(
                layer.updated(thickness_m=layer.thickness_m * factor)
                if not low_k or layer.material.conductivity_w_mk <= 0.2
                else layer
                for layer in construction.layers
            )
        )

    def shift(schedule, offset):
        return ScheduleSpec.annual(
            (x + offset for x in schedule.hourly(spec.climate.calendar_year)),
            calendar_year=spec.climate.calendar_year,
            source=profile["source"],
        )

    envelope, facade, airflow, use, delivery = (
        spec.envelope,
        spec.facade,
        spec.airflow,
        spec.use,
        spec.delivery,
    )
    for name, value in changes.items():
        roles = {
            "wall_resistance": "wall",
            "roof_resistance": "top",
            "ground_resistance": "ground",
            "basement_wall_resistance": "basement_wall",
        }
        if name in roles:
            role = roles[name]
            construction = getattr(envelope, role)
            if construction is None:
                raise ValueError(f"Missing construction for {name}")
            envelope = envelope.updated(**{role: scaled_construction(construction, value)})
        elif name == "window_u_multiplier":
            facade = facade.updated(
                glazing=facade.glazing.updated(
                    u_value_w_m2k=spec.facade.glazing.u_value_w_m2k * value
                )
            )
        elif name == "wwr":
            if facade.overrides:
                raise ValueError(
                    "Calibrated default WWR cannot override evidenced facade-specific WWR"
                )
            facade = facade.updated(default_wwr=value)
        elif name == "shgc_multiplier":
            facade = facade.updated(
                glazing=facade.glazing.updated(
                    solar_heat_gain_coefficient=min(
                        1, spec.facade.glazing.solar_heat_gain_coefficient * value
                    )
                )
            )
        elif name == "solar_shading_factor":
            facade = facade.updated(solar_shading_factor=value)
        elif name == "infiltration_multiplier":
            if spec.airflow.envelope_infiltration_l_s_m2 is not None:
                airflow = airflow.updated(
                    envelope_infiltration_l_s_m2=spec.airflow.envelope_infiltration_l_s_m2 * value
                )
            else:
                airflow = airflow.updated(
                    envelope_infiltration_ach=spec.airflow.envelope_infiltration_ach * value
                )
        elif name == "natural_exchange_multiplier":
            airflow = airflow.updated(
                natural_exchange_ach=spec.airflow.natural_exchange_ach * value
            )
        elif name == "flow_multiplier":
            airflow = airflow.updated(
                mechanical_vent_l_s_m2=spec.airflow.mechanical_vent_l_s_m2 * value
            )
        elif name == "recovery":
            airflow = airflow.updated(sensible_recovery_effectiveness=value)
        elif name in (
            "people_multiplier",
            "equipment_multiplier",
            "lighting_multiplier",
        ):
            field = {
                "people_multiplier": "occupant_density_per_m2",
                "equipment_multiplier": "equipment_gain_w_m2",
                "lighting_multiplier": "lighting_gain_w_m2",
            }[name]
            use = use.updated(**{field: getattr(spec.use, field) * value})
        elif name == "setpoint_offset_c":
            use = use.updated(heating_setpoint=shift(spec.use.heating_setpoint, value))
        elif name == "schedule_shift_hours":
            offset = int(value)
            fields = ("occupancy_fraction", "equipment_fraction", "lighting_fraction")

            def rotated(schedule):
                raw = schedule.hourly(spec.climate.calendar_year)
                days = [raw[i : i + 24] for i in range(0, len(raw), 24)]
                return ScheduleSpec.annual(
                    tuple(
                        v
                        for day in days
                        for v in (day[-offset:] + day[:-offset] if offset else day)
                    ),
                    calendar_year=spec.climate.calendar_year,
                    source=profile["source"],
                )

            use = use.updated(**{field: rotated(getattr(spec.use, field)) for field in fields})
            airflow = airflow.updated(
                mechanical_vent_fraction=rotated(spec.airflow.mechanical_vent_fraction)
            )
        elif name == "thermal_bridge_uplift":
            envelope = envelope.updated(thermal_bridge_uplift_fraction=value)
        elif name == "soil_conductivity":
            if envelope.foundation is None:
                raise ValueError("Soil sensitivity requires a native foundation")
            envelope = envelope.updated(
                foundation=envelope.foundation.updated(soil_conductivity_w_mk=value)
            )
        elif name == "attic_exchange_ach":
            if not any(
                (part.roof or spec.geometry.roof).topology == "cold_attic"
                for part in spec.geometry.parts
            ):
                continue
            remaining = tuple(x for x in spec.unconditioned_exchange if x.zone_kind != "attic")
            spec = spec.updated(
                unconditioned_exchange=remaining
                + (
                    UnconditionedExchangeSpec(
                        zone_kind="attic",
                        outdoor_air_ach=value,
                        source=profile["source"],
                    ),
                )
            )
        elif name in ("net_loss_kwh_m2", "delivery_efficiency"):
            if delivery.heating is None:
                raise ValueError("Delivered-heat calibration needs an explicit carrier model")
            field = (
                "efficiency"
                if name == "delivery_efficiency"
                else "net_distribution_loss_kwh_m2_year"
            )
            delivery = delivery.updated(
                heating=delivery.heating.updated(**{field: value, "source": profile["source"]})
            )
        else:
            raise ValueError(f"Unknown cohort parameter: {name}")
    # The EPC field denotes >10C, not residential21C or a fully heated area.
    # Native ZoneUseSpec changes thermostat behavior while preserving geometry;
    # partial conditioned extent remains an explicit unresolved assumption.
    zone_uses = spec.zone_uses
    if basement_setpoint is not None:
        zone_uses = (
            ZoneUseSpec(
                zone_kind="basement",
                use=use.updated(heating_setpoint=ScheduleSpec.constant(basement_setpoint)),
                source=profile["source"],
            ),
        )
    return spec.updated(
        envelope=envelope,
        facade=facade,
        airflow=airflow,
        use=use,
        delivery=delivery,
        zone_uses=zone_uses,
    )
