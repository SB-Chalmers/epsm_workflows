"""Two-point physical sensitivity gates against actual annual simulations."""

from pathlib import Path

from epsm_core import ScheduleSpec
from epsm_core.constructions import solve_insulation


def cases(base):
    for role, index, lo, hi in (
        ("wall", 1, 0.18, 1),
        ("top", 0, 0.1, 1.05),
        ("ground", 1, 0.2, 1.25),
    ):
        yield (
            role,
            1,
            tuple(
                base.updated(
                    envelope=base.envelope.updated(
                        **{role: solve_insulation(getattr(base.envelope, role), index, u)}
                    )
                )
                for u in (lo, hi)
            ),
        )
    yield (
        "window_u",
        1,
        tuple(
            base.updated(
                facade=base.facade.updated(glazing=base.facade.glazing.updated(u_value_w_m2k=u))
            )
            for u in (1.1, 3)
        ),
    )
    for name, lo, hi, direction in (
        ("envelope_infiltration_ach", 0.1, 0.5, 1),
        ("mechanical_vent_l_s_m2", 0.25, 0.5, 1),
        ("sensible_recovery_effectiveness", 0.45, 0.8, -1),
    ):
        yield (
            name,
            direction,
            tuple(
                base.updated(airflow=base.airflow.updated(**{name: value})) for value in (lo, hi)
            ),
        )
    yield (
        "heating_setpoint",
        1,
        tuple(
            base.updated(use=base.use.updated(heating_setpoint=ScheduleSpec.constant(v)))
            for v in (19.5, 22.5)
        ),
    )
    yield (
        "thermal_bridges",
        1,
        tuple(
            base.updated(envelope=base.envelope.updated(thermal_bridge_uplift_fraction=v))
            for v in (0.1, 0.35)
        ),
    )
    yield (
        "solar_shading_factor",
        -1,
        tuple(base.updated(facade=base.facade.updated(solar_shading_factor=v)) for v in (0.4, 0.7)),
    )
    for name, lo, hi, direction in (
        ("occupant_density_per_m2", 0.012, 0.022, -1),
        ("equipment_gain_w_m2", 1.2, 2.2, -1),
        ("dhw_kwh_m2_year", 18, 25, 1),
    ):
        yield (
            name,
            direction,
            tuple(base.updated(use=base.use.updated(**{name: v})) for v in (lo, hi)),
        )
    yield (
        "natural_exchange",
        1,
        tuple(
            base.updated(
                airflow=base.airflow.updated(
                    topology="S",
                    mechanical_vent_l_s_m2=0,
                    sensible_recovery_effectiveness=0,
                    natural_exchange_ach=v,
                )
            )
            for v in (0.2, 0.7)
        ),
    )


def run_oat(base, runner, directory):
    report = {}
    for name, direction, variants in cases(base):
        values = []
        for i, spec in enumerate(variants):
            path = Path(directory) / f"{name}_{i}"
            if path.exists():
                import uuid

                path = path.with_name(path.name + "_" + uuid.uuid4().hex[:8])
            result = runner.run(spec, path)
            values.append(
                result.scalars[
                    "dhw_load_kwh" if name == "dhw_kwh_m2_year" else "space_heating_demand_kwh"
                ]
            )
        report[name] = dict(
            low=values[0],
            high=values[1],
            expected_direction=direction,
            passed=(values[1] - values[0]) * direction > 0,
        )
    return report
