# SPDX-License-Identifier: MIT
"""Expand sampled designs into portable Core Buildings for each study kind.

Every case is one ``epsm_core.Building`` snapshot plus a weather id. Vera
rebuilds the Building with ``Building.from_dict`` and runs EnergyPlus and, when
installed, the RC engine on the same physical object and weather.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from epsm_core import (
    AirflowSpec,
    Building,
    FacadeOverride,
    FacadeSpec,
    FoundationSpec,
    GlazingSpec,
    GroundTemperatureSpec,
    HVACSystem,
    RoofSpec,
    ScheduleSpec,
    UseSpec,
    WindowLayoutSpec,
    ZoningSpec,
    synthetic_context,
)

from ..records import digest
from .fabric import construction_set, solve_envelope
from .geometry import normalize_scene, procedural
from .sampling import seed_for
from .study import STOCK_BOUNDS

# EnergyPlus 24.2 rejects detailed-surface edges below 0.01 m. A scaled window on a
# centimetre-scale remnant has no physical meaning; leave that facade opaque.
MIN_SCALED_OPENING_EDGE_M = 0.01001

# Core readiness wants every ground assumption stated. Kiva takes its uncalibrated
# defaults; a floor with a courtyard hole cannot use Kiva and gets EnergyPlus's
# default 18 C prescribed ground temperature instead. The choice is a recorded input.
KIVA = FoundationSpec.model_validate(
    {**FoundationSpec(method="kiva").model_dump(), "source": "epsm_campaign: uncalibrated Kiva defaults"}
)
PRESCRIBED = FoundationSpec(method="prescribed_temperature", source="epsm_campaign: holed floor, prescribed ground")
GROUND_18C = GroundTemperatureSpec(
    monthly_temperatures_c=(18.0,) * 12, source="epsm_campaign: EnergyPlus 24.2 default 18C, uncalibrated"
)


def ground_method(polygon):
    return "prescribed_temperature" if polygon.interiors else "kiva"

PROFILES = {
    "occupancy": [1, 0.8, 0.5, 1],
    "equipment": [0.3, 0.7, 0.4, 1],
    "lighting": [0.1, 0.6, 0.2, 1],
    "dhw": [0.1, 1, 0.3, 0.8],
}


@dataclass(frozen=True)
class Case:
    """Study-independent unit handed to the plan, worker and finalizer."""

    building: dict
    weather_id: str
    group_id: str
    inputs: dict
    axes: dict
    metadata: dict = field(default_factory=dict)

    @property
    def building_key(self):
        return digest(self.building)

    @property
    def case_id(self):
        return digest([self.building_key, self.weather_id, self.axes["sample_id"]])

    @property
    def complexity(self):
        geometry = self.building["geometry"]
        vertices = sum(len(part["footprint"]["exterior"]) for part in geometry["parts"])
        floors = sum(len(part["storey_elevations_m"]) - 1 for part in geometry["parts"])
        return float(vertices * floors + len(self.building.get("context", {}).get("buildings", [])) / 4)


def effective_scaled_wwr(length_m, requested_wwr):
    opening = float(length_m) * math.sqrt(float(requested_wwr))
    return requested_wwr if opening >= MIN_SCALED_OPENING_EDGE_M else 0.0


def schedules(family, shift):
    return {
        key + "_fraction": ScheduleSpec(
            values=tuple(
                float(x)
                for x in (np.ones(24) if family == "constant" else np.roll(np.repeat(values, [6, 3, 8, 7]), shift))
            )
        )
        for key, values in PROFILES.items()
    }


def airflow(p):
    return AirflowSpec(
        topology=p["topology"],
        envelope_infiltration_ach=p["infiltration_ach"],
        natural_exchange_ach=p["natural_ach"] if p["topology"] == "S" else 0,
        mechanical_vent_l_s_m2=p["mechanical_l_s_m2"] if p["topology"] != "S" else 0,
        sensible_recovery_effectiveness=p["recovery"] if p["topology"] == "FTX" else 0,
    )


def use(p):
    return UseSpec(
        occupancy_category="residential",
        heating_setpoint=ScheduleSpec.constant(p["heating_setpoint_c"]),
        occupant_density_per_m2=p["occupants_per_m2"],
        equipment_gain_w_m2=p["equipment_w_m2"],
        lighting_gain_w_m2=p["lighting_w_m2"],
        dhw_kwh_m2_year=p["dhw_kwh_m2_year"],
        **schedules(p["schedule_family"], p["schedule_shift"]),
    )


def design_polygon(design, footprints):
    if design["geometry_source"] == "real":
        record = next(f for f in footprints if f["footprint_id"] == design["footprint_id"])
        return record["polygon"]
    p = design["requested"]
    return procedural(design["family"], p["footprint_area_m2"], p["aspect_ratio"], design["cut_l"], design["cut_w"])


def physical_building(design, polygon):
    """Geometry, fabric, systems and use of one design before context and orientation."""
    p = design["requested"]
    p["ground_method"] = ground_method(polygon)
    prescribed = p["ground_method"] == "prescribed_temperature"
    building = Building.from_footprint(
        polygon,
        identifier=design["design_id"],
        storeys=p["num_floors"],
        storey_height_m=p["floor_height_m"],
        construction_set=construction_set(p),
        hvac=HVACSystem(airflow=airflow(p)),
        operation=use(p),
        thermal_bridge_uplift_fraction=p["bridge_uplift"],
        foundation=PRESCRIBED if prescribed else KIVA,
        ground_temperature=GROUND_18C if prescribed else None,
        load_reference_area_policy="conditioned_floor_area",
        zoning=ZoningSpec(mode="per_storey"),
        roof=RoofSpec(shape="flat", topology="compact"),
    )
    return solve_envelope(building, p)


def scene(study, design, geometry, preset):
    """Generate neighbours for one context preset and bake the sampled orientation."""
    seed = seed_for(study.seed, design["design_id"], "context")
    x, y, xx, yy = geometry.parts[0].footprint.polygon.bounds
    radius = math.hypot(xx - x, yy - y) / 2 + preset.radius_margin_m
    overrides = {k: tuple(v) if isinstance(v, list) else v for k, v in preset.overrides.items()}
    context = synthetic_context(
        geometry, preset=preset.preset, seed=seed, radius_m=radius, max_buildings=preset.max_buildings, **overrides
    )
    normalized, context, transform = normalize_scene(geometry, context, design["requested"]["orientation_deg"])
    return normalized, context, {"context": preset.name, "context_seed": seed, "transform": transform}


def facade_for(p, geometry):
    """Scaled windows with the sampled WWR of each cardinal direction."""
    values = tuple(
        (item, p["wwr_" + ("north", "east", "south", "west")[int((item.azimuth_deg + 45) // 90) % 4]])
        for item in geometry.facades()
    )
    facade = FacadeSpec(
        layout=WindowLayoutSpec(mode="scaled"),
        default_wwr=0.2,
        glazing=GlazingSpec(u_value_w_m2k=p["window_u"], solar_heat_gain_coefficient=p["shgc"]),
        overrides=tuple(
            FacadeOverride(facade_id=item.facade_id, wwr=effective_scaled_wwr(item.length_m, wwr)) for item, wwr in values
        ),
    )
    opaque = [item.facade_id for item, wwr in values if wwr and not effective_scaled_wwr(item.length_m, wwr)]
    return facade, opaque


def placed(building, geometry, context, facade, identifier, *, hvac=None):
    """A snapshot dictionary of ``building`` in one normalized scene."""
    copy = Building.from_dict(building.to_dict())
    copy.identifier = identifier
    copy.geometry = geometry
    copy.context = context
    copy.facade = facade
    if hvac is not None:
        copy.hvac = hvac
    return copy.to_dict()


def physical_cases(study, design, polygon, weather_by_site, *, cohort_axes):
    """Cross one design with every context preset and its sites' weather."""
    p = design["requested"]
    building = physical_building(design, polygon)
    sites = [design["city"]] if design["city"] else [c.slug for c in study.cities]
    for preset in study.contexts:
        normalized, context, context_meta = scene(study, design, building.geometry, preset)
        facade, opaque = facade_for(p, normalized)
        # One snapshot per scene; the worker renames it to the run's sample id.
        snapshot = placed(building, normalized, context, facade, f"{design['design_id']}_{preset.name}")
        for site in sites:
            for kind, weather_id in weather_by_site[site]:
                identifier = f"{design['design_id']}_{preset.name}_{site}_{kind}"
                yield Case(
                    building=snapshot,
                    weather_id=weather_id,
                    group_id=design["design_id"],
                    inputs=dict(p),
                    axes={
                        **cohort_axes,
                        "sample_id": identifier,
                        "cohort": design["cohort"],
                        "site": site,
                        "context": preset.name,
                        "weather_kind": kind,
                        "footprint_family": design["family"],
                        "geometry_source": design["geometry_source"],
                        "scenario_id": None,
                        "measure_id": None,
                        "pilot": bool(design["pilot"]),
                    },
                    metadata={
                        **{k: v for k, v in design.items() if k != "requested"},
                        **context_meta,
                        "opaque_short_facades": opaque,
                    },
                )


def weather_by_site(study, catalog):
    """``{site: [(kind, weather_id), ...]}`` in TMY, AMY order from the frozen catalog."""
    grouped = {}
    for city in study.cities:
        grouped[city.slug] = [
            (kind, f"{city.slug}_{kind}") for kind in ("tmy", "amy") if f"{city.slug}_{kind}" in catalog
        ]
    return grouped


def stock_cases(study, footprints, catalog):
    """All stock-diversity cases in deterministic plan order."""
    from .sampling import stock_designs

    grouped = weather_by_site(study, catalog)
    for design in stock_designs(study, footprints):
        polygon = design_polygon(design, footprints)
        yield from physical_cases(study, design, polygon, grouped, cohort_axes={"study_kind": study.kind})


def _design_axes(study, design):
    return {
        "study_kind": study.kind,
        "cohort": design["cohort"],
        "footprint_family": design["family"],
        "geometry_source": design["geometry_source"],
        "scenario_id": None,
        "measure_id": None,
    }


def _preset(study, name):
    return next(c for c in study.contexts if c.name == name)


def _city_weathers(study, catalog, kind):
    """``[(site, kind, weather_id)]`` for one weather kind of every city."""
    pairs = []
    for city in study.cities:
        weather_id = f"{city.slug}_{kind}"
        if weather_id not in catalog:
            raise ValueError(f"{city.slug} has no frozen {kind} weather")
        pairs.append((city.slug, kind, weather_id))
    return pairs


def weather_sweep_cases(study, footprints, catalog):
    """A fixed set of buildings, each run under every listed weather file."""
    from .sampling import fixed_designs

    section = study.weather_sweep
    preset = _preset(study, section.context)
    weathers = [(item.location or "sweep", item.kind, item.weather_id) for item in section.weather]
    for design in fixed_designs(study, section.buildings, "sweep", footprints, section.bounds):
        p = design["requested"]
        building = physical_building(design, design_polygon(design, footprints))
        normalized, context, context_meta = scene(study, design, building.geometry, preset)
        facade, opaque = facade_for(p, normalized)
        snapshot = placed(building, normalized, context, facade, f"{design['design_id']}_{preset.name}")
        for index, (site, kind, weather_id) in enumerate(weathers):
            identifier = f"{design['design_id']}_{weather_id}"
            yield Case(
                building=snapshot,
                weather_id=weather_id,
                group_id=design["design_id"],
                inputs=dict(p),
                axes={**_design_axes(study, design), "sample_id": identifier, "site": site, "context": preset.name,
                      "weather_kind": kind, "pilot": bool(design["pilot"]) and index == 0},
                metadata={**{k: v for k, v in design.items() if k != "requested"}, **context_meta,
                          "opaque_short_facades": opaque},
            )


def ventilation_cases(study, footprints, catalog):
    """Fixed buildings and weather; a stratified sweep over airflow inputs per building."""
    from .sampling import balanced, independent_designs, seed_for

    section = study.ventilation
    preset = _preset(study, section.context)
    weathers = _city_weathers(study, catalog, section.weather_kind)
    for design in fixed_designs_for(study, section.buildings, "ventilation", footprints, section.building_bounds):
        p = design["requested"]
        building = physical_building(design, design_polygon(design, footprints))
        normalized, context, context_meta = scene(study, design, building.geometry, preset)
        facade, opaque = facade_for(p, normalized)
        rng = np.random.default_rng(seed_for(study.seed, "ventilation", design["design_id"]))
        topologies = balanced(section.topologies, section.samples_per_building, rng)
        samples = independent_designs(study.seed, section.bounds, section.samples_per_building,
                                      label=f"ventilation:{design['design_id']}")
        for index, (values, unit) in enumerate(samples):
            inputs = {**p, **values, "topology": str(topologies[index])}
            snapshot = placed(building, normalized, context, facade, f"{design['design_id']}_v{index:03d}",
                              hvac=HVACSystem(airflow=airflow(inputs)))
            for site, kind, weather_id in weathers:
                identifier = f"{design['design_id']}_v{index:03d}_{site}_{kind}"
                yield Case(
                    building=snapshot,
                    weather_id=weather_id,
                    group_id=design["design_id"],
                    inputs=inputs,
                    axes={**_design_axes(study, design), "sample_id": identifier, "site": site,
                          "context": preset.name, "weather_kind": kind, "pilot": bool(design["pilot"]) and index == 0},
                    metadata={**{k: v for k, v in design.items() if k != "requested"}, **context_meta,
                              "opaque_short_facades": opaque, "ventilation_unit": unit, "sample_index": index},
                )


def fixed_designs_for(study, count, cohort, footprints, bounds):
    from .sampling import fixed_designs

    return fixed_designs(study, count, cohort, footprints, bounds)


def _catalogue_measures(section):
    from epsm_data import Catalogue, RenovationCatalogue

    catalogue = Catalogue.load()
    renovations = RenovationCatalogue.load(catalogue)
    # Dependent companions (sealing after windows) ride along with their hosts.
    candidates = [c for c in renovations if c.kind.standalone]
    if section.measures != "all":
        wanted = set(section.measures)
        missing = wanted - {c.candidate_id for c in candidates}
        if missing:
            raise ValueError(f"Unknown renovation candidates: {sorted(missing)}")
        candidates = [c for c in candidates if c.candidate_id in wanted]
    return catalogue, candidates


def before_after_cases(study, footprints, catalog):
    """Existing-state archetype buildings, each rerun with one catalogue measure at a time.

    Needs ``epsm`` (archetype application) at packing time. Measures a building
    cannot host are recorded in its baseline metadata rather than silently dropped.
    """
    from epsm import Building as EpsmBuilding
    from epsm_core.measures import apply_candidates

    from .sampling import balanced, seed_for

    section = study.before_after
    preset = _preset(study, section.context)
    weathers = _city_weathers(study, catalog, section.weather_kind)
    catalogue, candidates = _catalogue_measures(section)
    designs = list(fixed_designs_for(study, section.buildings, "existing", footprints, STOCK_BOUNDS))
    rng = np.random.default_rng(seed_for(study.seed, "before_after", "archetypes"))
    archetypes = balanced(section.archetypes, len(designs), rng)
    for index, design in enumerate(designs):
        p = design["requested"]
        archetype_id = str(archetypes[index])
        core = physical_building(design, design_polygon(design, footprints))
        building = EpsmBuilding.from_dict(core.to_dict())
        state = None if archetype_id.endswith("se07") else "existing"  # SE.07 has one state
        building.apply_archetype(catalogue.get(archetype_id, state=state, ventilation=p["topology"]))
        normalized, context, context_meta = scene(study, design, building.geometry, preset)
        facade, opaque = facade_for(p, normalized)
        building.geometry, building.context, building.facade = normalized, context, facade
        inputs = {k: p[k] for k in ("footprint_area_m2", "num_floors", "floor_height_m", "orientation_deg", "topology")}
        inputs.update(archetype=archetype_id, ground_method=p["ground_method"])
        alternatives, skipped = [("baseline", None, building)], []
        for candidate in candidates:
            try:
                child = apply_candidates(building, candidate)
            except (ValueError, KeyError, TypeError) as error:
                skipped.append({"candidate_id": candidate.candidate_id, "reason": str(error)[:300]})
                continue
            alternatives.append((candidate.candidate_id, candidate.measure_type, child))
        for scenario_id, measure_type, alternative in alternatives:
            alternative.identifier = f"{design['design_id']}_{archetype_id}_{scenario_id}"
            # The Core snapshot, not epsm's wrapper, so Vera rebuilds it with Core alone.
            snapshot = alternative.snapshot().to_dict()
            for site, kind, weather_id in weathers:
                identifier = f"{alternative.identifier}_{site}_{kind}"
                yield Case(
                    building=snapshot,
                    weather_id=weather_id,
                    group_id=design["design_id"],
                    inputs={**inputs, "scenario": scenario_id, "measure_type": measure_type or "none"},
                    axes={**_design_axes(study, design), "sample_id": identifier, "site": site,
                          "context": preset.name, "weather_kind": kind, "scenario_id": scenario_id,
                          "measure_id": measure_type, "pilot": bool(design["pilot"]) and scenario_id == "baseline"},
                    metadata={**{k: v for k, v in design.items() if k != "requested"}, **context_meta,
                              "opaque_short_facades": opaque, "archetype": archetype_id,
                              "skipped_measures": skipped if scenario_id == "baseline" else []},
                )


def cases_for(study, footprints, catalog):
    """Dispatch to the study kind's case generator."""
    return {
        "stock_diversity": stock_cases,
        "weather_sweep": weather_sweep_cases,
        "before_after": before_after_cases,
        "ventilation_sweep": ventilation_cases,
    }[study.kind](study, footprints, catalog)
