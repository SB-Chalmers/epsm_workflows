"""Stratified continuous LHS and balanced categorical building designs."""

import math

import numpy as np
from scipy.optimize import linear_sum_assignment
from scipy.stats.qmc import LatinHypercube

from epsm_core import (
    AirflowSpec,
    BuildingSpec,
    ConstructionSpec,
    FacadeOverride,
    FacadeSpec,
    GeometrySpec,
    GlazingSpec,
    RoofSpec,
    ScheduleSpec,
    UseSpec,
    WindowLayoutSpec,
    ZoningSpec,
    synthetic_context,
)
from epsm_core.constructions import solve_insulation
from epsm_core.weather import climate_from_epw

from ..fabric import fabric
from .common import digest
from .geometry import (
    footprint,
    minimum_area,
    normalize_scene,
    procedural,
    scaled_template,
)

BOUNDS = {
    "footprint_area_m2": [250, 2500],
    "floor_height_m": [2.6, 3.1],
    "aspect_ratio": [1, 4],
    "orientation_deg": [0, 360],
    "wall_u": [0.17, 1.50],
    "roof_u": [0.10, 0.85],
    "ground_u": [0.20, 1.70],
    "window_u": [1.1, 3],
    "shgc": [0.40, 0.75],
    "wwr_north": [0.060, 0.290],
    "wwr_south": [0.080, 0.330],
    "wwr_east": [0.070, 0.320],
    "wwr_west": [0.080, 0.340],
    "infiltration_ach": [0.10, 1],
    "natural_ach": [0.20, 0.70],
    "mechanical_l_s_m2": [0.25, 0.50],
    "recovery": [0, 0.80],
    "heating_setpoint_c": [19.5, 22.5],
    "occupants_per_m2": [0.012, 0.022],
    "equipment_w_m2": [1.2, 2.2],
    "lighting_w_m2": [0, 1.5],
    "dhw_kwh_m2_year": [18, 30],
    "bridge_uplift": [0, 0.35],
    "wall_concrete_m": [0.10, 0.25],
    "roof_concrete_m": [0.12, 0.25],
    "floor_concrete_m": [0.12, 0.25],
    "cut_l_unit": [0, 1],
    "cut_w_unit": [0, 1],
}

# EnergyPlus 24.2 rejects detailed-surface edges below 0.01 m. Scaled glazing on
# a centimetre-scale footprint remnant has no physical meaning, so leave that
# facade opaque with a small numerical margin instead of emitting an invalid pane.
MIN_SCALED_OPENING_EDGE_M = 0.01001


def effective_scaled_wwr(length_m: float, requested_wwr: float) -> float:
    """Return zero when a scaled window would be shorter than the engine limit."""
    opening_width = float(length_m) * math.sqrt(float(requested_wwr))
    return requested_wwr if opening_width >= MIN_SCALED_OPENING_EDGE_M else 0.0


FAMILIES = ("rectangle", "L", "U", "courtyard", "complex")
CONTEXTS = ("urban_lowrise_sparse", "urban_lowrise_dense", "urban_highrise_dense")
PROFILES = {
    "occupancy": [1, 0.8, 0.5, 1],
    "equipment": [0.3, 0.7, 0.4, 1],
    "lighting": [0.1, 0.6, 0.2, 1],
    "dhw": [0.1, 1, 0.3, 0.8],
}


def seed_for(root, *keys):
    return int(digest([root, *keys])[:16], 16)


def balanced(values, n, rng):
    return rng.permutation(np.resize(np.array(values), n))


def schedules(family, shift):
    return {
        key + "_fraction": ScheduleSpec(
            values=tuple(
                float(x)
                for x in (
                    np.ones(24)
                    if family == "constant"
                    else np.roll(np.repeat(values, [6, 3, 8, 7]), shift)
                )
            )
        )
        for key, values in PROFILES.items()
    }


def envelope(p):
    b = fabric(uplift=p["bridge_uplift"])
    wall = list(b.wall.layers)
    wall[2] = wall[2].updated(thickness_m=p["wall_concrete_m"])
    top = ConstructionSpec(
        layers=(
            b.weather_shell.layers[0],
            b.top.layers[0],
            b.weather_shell.layers[1].updated(thickness_m=p["roof_concrete_m"]),
        ),
        surface_resistance_m2k_w=0.14,
    )
    return b.updated(
        wall=solve_insulation(b.wall.updated(layers=tuple(wall)), 1, p["wall_u"]),
        top=solve_insulation(top, 1, p["roof_u"]),
        ground=solve_insulation(b.ground, 1, p["ground_u"]),
        intermediate_floor=b.intermediate_floor.updated(
            layers=(
                b.intermediate_floor.layers[0].updated(
                    thickness_m=p["floor_concrete_m"]
                ),
            )
        ),
    )


def rows(config, templates):
    """Yield physical designs before weather/context replication; bounded per-stratum memory."""
    counts = config["counts"]
    cities = [c["slug"] for c in config["cities"]]
    # JSON member ordering is not part of the canonical manifest identity.
    keys = sorted(config["bounds"])
    bounds = {key: config["bounds"][key] for key in keys}
    for cohort, city_labels, per_group in (
        ("anchors", [None], counts["anchor_designs"] // 5),
        ("diversity", cities, counts["diversity_designs"] // (5 * len(cities))),
    ):
        for city in city_labels:
            for family in FAMILIES:
                n = per_group
                seed = seed_for(config["seed"], cohort, city, family)
                rng = np.random.default_rng(seed)
                unit = LatinHypercube(
                    d=len(keys), seed=seed, optimization="random-cd"
                ).random(n)
                values = np.column_stack(
                    [
                        lo * (hi / lo) ** unit[:, i]
                        if key in ("footprint_area_m2", "aspect_ratio")
                        else lo + (hi - lo) * unit[:, i]
                        for i, (key, (lo, hi)) in enumerate(bounds.items())
                    ]
                )

                def column(key):
                    return values[:, keys.index(key)]

                cls = (
                    0.25 + 0.20 * column("cut_l_unit")
                    if family == "courtyard"
                    else 0.20 + 0.25 * column("cut_l_unit")
                )
                cws = (
                    0.25 + 0.20 * column("cut_w_unit")
                    if family == "courtyard"
                    else 0.20 + 0.25 * column("cut_w_unit")
                )
                if family != "complex":
                    minima = np.array(
                        [
                            minimum_area(family, aspect, cl, cw)
                            for aspect, cl, cw in zip(column("aspect_ratio"), cls, cws)
                        ]
                    )
                    costs = rng.random((n, n))
                    costs[column("footprint_area_m2")[None, :] < minima[:, None]] = 1e9
                    ii, jj = linear_sum_assignment(costs)
                    if np.any(costs[ii, jj] >= 1e9):
                        raise ValueError(
                            f"Infeasible LHS area assignment: {cohort}/{city}/{family}"
                        )
                    index = keys.index("footprint_area_m2")
                    unit[:, index] = unit[jj, index]
                    values[:, index] = values[jj, index]
                if any(
                    len(set(np.floor(unit[:, i] * n).astype(int))) != n
                    for i in range(len(keys))
                ):
                    raise ValueError("Lost an LHS marginal stratum")
                floors = balanced(range(2, 9), n, rng)
                systems = balanced(["S", "F", "FT", "FTX"], n, rng)
                profiles = balanced(["constant", "daily"], n, rng)
                shifts = balanced(range(-2, 3), n, rng)
                template_ids = balanced(range(len(templates)), n, rng)
                for i in range(n):
                    p = dict(zip(keys, map(float, values[i])))
                    p.update(
                        num_floors=int(floors[i]),
                        topology=str(systems[i]),
                        schedule_family=str(profiles[i]),
                        schedule_shift=int(shifts[i]),
                    )
                    record = (
                        templates[int(template_ids[i])] if family == "complex" else None
                    )
                    if record:
                        polygon, factor = scaled_template(
                            record, p["footprint_area_m2"]
                        )
                    else:
                        polygon = procedural(
                            family,
                            p["footprint_area_m2"],
                            p["aspect_ratio"],
                            float(cls[i]),
                            float(cws[i]),
                        )
                        factor = 1
                    design_id = f"{cohort}_{city or 'all'}_{family}_{i:05d}"
                    yield dict(
                        design_id=design_id,
                        cohort=cohort,
                        city=city,
                        family=family,
                        requested=p,
                        lhs_unit=unit[i].tolist(),
                        lhs_keys=keys,
                        stratum_seed=seed,
                        footprint=footprint(polygon),
                        template_id=record["template_id"] if record else None,
                        source_transform=dict(
                            scale=factor,
                            source_origin_m=record["source_origin_m"]
                            if record
                            else [0, 0],
                        ),
                        # One prototype per procedural family and each real template.
                        pilot=(
                            cohort == "anchors"
                            and (
                                i == 0
                                if not record
                                else int(template_ids[i])
                                not in set(map(int, template_ids[:i]))
                            )
                        ),
                    )


def load_climates(config, weather, *, allow_missing_weather=False):
    """Load and cross-check each prepared TMY/AMY pair once."""
    climates = {}
    for city in config["cities"]:
        tmy = climate_from_epw(weather[city["slug"]]["tmy"]["path"], calendar_year=2018)
        climates[city["slug"]] = [("tmy", tmy)]
        if "amy" not in weather[city["slug"]]:
            if allow_missing_weather:
                continue
            raise ValueError(f"Missing AMY for {city['slug']}")
        amy = climate_from_epw(weather[city["slug"]]["amy"]["path"], calendar_year=2018)
        for name, tolerance in [
            ("latitude_deg", 1e-4),
            ("longitude_deg", 1e-4),
            ("elevation_m", 0.01),
            ("time_zone_hours", 1e-9),
        ]:
            if abs(getattr(amy, name) - getattr(tmy, name)) > tolerance:
                raise ValueError(
                    f"TMY/AMY target-site mismatch for {city['slug']}: {name}"
                )
        # The target site and solar query point stay fixed when an observation station changes.
        amy = amy.updated(
            latitude_deg=tmy.latitude_deg,
            longitude_deg=tmy.longitude_deg,
            elevation_m=tmy.elevation_m,
            time_zone_hours=tmy.time_zone_hours,
        )
        climates[city["slug"]].append(("amy", amy))
    return climates


def design_cases(config, design, climates):
    """Expand one physical design; safe to call in a planning process pool."""
    p = design["requested"]
    g = GeometrySpec.from_footprint(
        design["footprint"],
        storeys=p["num_floors"],
        storey_height_m=p["floor_height_m"],
        zoning=ZoningSpec(mode="per_storey"),
        roof=RoofSpec(shape="flat", topology="compact"),
    )
    env = envelope(p)
    air = AirflowSpec(
        topology=p["topology"],
        envelope_infiltration_ach=p["infiltration_ach"],
        natural_exchange_ach=p["natural_ach"] if p["topology"] == "S" else 0,
        mechanical_vent_l_s_m2=p["mechanical_l_s_m2"] if p["topology"] != "S" else 0,
        sensible_recovery_effectiveness=p["recovery"] if p["topology"] == "FTX" else 0,
    )
    use = UseSpec(
        occupancy_category="residential",
        heating_setpoint=ScheduleSpec.constant(p["heating_setpoint_c"]),
        occupant_density_per_m2=p["occupants_per_m2"],
        equipment_gain_w_m2=p["equipment_w_m2"],
        lighting_gain_w_m2=p["lighting_w_m2"],
        dhw_kwh_m2_year=p["dhw_kwh_m2_year"],
        **schedules(p["schedule_family"], p["schedule_shift"]),
    )
    seed = seed_for(config["seed"], design["design_id"], "context")
    x, y, xx, yy = design["footprint"].polygon.bounds
    radius = math.hypot(xx - x, yy - y) / 2 + 100
    generated = []
    for context_name in CONTEXTS:
        overrides = (
            dict(
                spacing_m=64,
                size_range_m=(20, 26),
                height_range_m=(6, 15),
                clearance_m=20,
            )
            if context_name == "urban_lowrise_sparse"
            else {}
        )
        context = synthetic_context(
            g,
            preset=("urban_lowrise_dense" if overrides else context_name),
            seed=seed,
            radius_m=radius,
            max_buildings=512,
            **overrides,
        )
        normalized, context, transform = normalize_scene(
            g, context, p["orientation_deg"]
        )
        facade_values = tuple(
            (
                item,
                p[
                    "wwr_"
                    + ("north", "east", "south", "west")[
                        int((item.azimuth_deg + 45) // 90) % 4
                    ]
                ],
            )
            for item in normalized.facades()
        )
        facade = FacadeSpec(
            layout=WindowLayoutSpec(mode="scaled"),
            default_wwr=0.2,
            glazing=GlazingSpec(
                u_value_w_m2k=p["window_u"], solar_heat_gain_coefficient=p["shgc"]
            ),
            overrides=tuple(
                FacadeOverride(
                    facade_id=item.facade_id,
                    wwr=effective_scaled_wwr(item.length_m, requested_wwr),
                )
                for item, requested_wwr in facade_values
            ),
        )
        metadata = {k: v for k, v in design.items() if k != "footprint"}
        metadata.update(
            context=context_name,
            context_seed=seed,
            transform=transform,
            opaque_short_facades=[
                item.facade_id
                for item, requested_wwr in facade_values
                if requested_wwr and not effective_scaled_wwr(item.length_m, requested_wwr)
            ],
        )
        for city in [design["city"]] if design["city"] else climates:
            for kind, climate in climates[city]:
                identifier = f"{design['design_id']}_{context_name}_{city}_{kind}"
                generated.append(
                    (
                        BuildingSpec(
                            identifier=identifier,
                            geometry=normalized,
                            context=context,
                            envelope=env,
                            airflow=air,
                            use=use,
                            facade=facade,
                            climate=climate,
                        ),
                        {
                            **metadata,
                            "city": city,
                            "weather_kind": kind,
                            "weather_id": f"{city}_{kind}",
                            "group_id": design["design_id"],
                        },
                    )
                )
    return generated


def cases(config, templates, weather, *, pilot_only=False, allow_missing_weather=False):
    """Generate immutable cases; an explicit partial pilot may omit unavailable EPWs."""
    climates = load_climates(
        config, weather, allow_missing_weather=allow_missing_weather
    )
    for design in rows(config, templates):
        if pilot_only and design["cohort"] != "anchors":
            break
        if pilot_only and not design["pilot"]:
            continue
        yield from design_cases(config, design, climates)
