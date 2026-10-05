"""Six illustrative building responses constructed entirely through EPSM Core.

These are synthetic sensitivity cases, not calibrated Swedish stock archetypes.
Old/new labels describe the stated envelope and airflow assumptions; they are not
construction-date classifications. Geometry, mass, gains, schedules, glazing and
air exchange vary together to sample different responses to reconstructed weather.
Consequently, between-profile differences cannot isolate a single building factor.

All cases use unlimited ideal loads. Cooling is a diagnostic thermal requirement,
including for dwellings and schools which may have no installed cooling. Results
are sensible heating/cooling coil demand, including mechanical outdoor-air loads,
not purchased electricity/fuel or humidity control. DHW is excluded. The campaign
holds Core's prescribed conditioned-floor boundary at18 C in all twelve months,
locations, years, building profiles and weather alternatives. This uncalibrated
boundary does not represent measured deep-ground temperatures or soil/snow physics;
it limits interpretation of absolute loads and geographical differences.

Daily values are hour-start intervals in EPW local standard time. Weekends are
explicit; holidays, school terms, daylight saving and adaptive window opening are
not inferred. Weather-responsive leakage uses Core's supported EnergyPlus design-
flow coefficients, not a pressure-network model or measured airtightness.
"""

from __future__ import annotations

import csv
import hashlib
import json
from copy import deepcopy
from pathlib import Path

from epsm_core import (
    AirflowSpec,
    BuildingPart,
    BuildingSpec,
    ConstructionSpec,
    EnvelopeSpec,
    FacadeSpec,
    Footprint,
    GeometrySpec,
    GlazingSpec,
    GroundTemperatureSpec,
    LayerSpec,
    MaterialSpec,
    RoofSpec,
    ScheduleSpec,
    UseSpec,
    ZoningSpec,
    climate_from_epw,
)
from epsm_core.weather import validate_epw_calendar

ASSUMPTION_VERSION = "synthetic-weather-sensitivity-v1"
COMMON_ASSUMPTIONS = {
    "version": ASSUMPTION_VERSION,
    "basis": "Illustrative analyst-selected sensitivity inputs; no measured-building calibration",
    "constructor": "EPSM Core BuildingSpec and native EnergyPlus 24.2 compiler",
    "ground_temperature_c": 18.0,
    "ground_basis": "Core default prescribed conditioned-floor boundary, constant18 C for every month/location/year/profile/method; uncalibrated, no measured deep-ground temperatures or soil/snow physics",
    "roof": "Compact flat roof, no attic or basement",
    "orientation_deg": 0.0,
    "zoning": "One conditioned zone per storey; uniform programme within each building",
    "shading": "No surrounding buildings, external shades or adaptive blinds",
    "thermal_bridge_uplift_fraction": 0.0,
    "hvac": "Unlimited ideal heating/cooling loads; no humidity control",
    "cooling_interpretation": "Diagnostic thermal cooling demand; installed cooling is not assumed",
    "heating_cooling_availability": "Always available; prescribed setpoints govern operation",
    "dhw_kwh_m2_year": 0.0,
    "schedule_time_basis": "Hour-start intervals, EPW local standard time, Gregorian weekdays, no DST",
    "calendar_exclusions": "No holidays, school vacations or exceptional closures inferred",
    "load_reference_area": "Geometric conditioned floor area; no assumed Atemp mapping",
    "airflow_interpretation": "Prescribed outdoor-air thermal equivalents with empirical wind/temperature-responsive design-flow leakage in older profiles; no pressure-network coupling between leakage and mechanical fans",
    "materials": {
        "concrete": {
            "conductivity_w_mk": 1.4,
            "density_kg_m3": 2200,
            "specific_heat_j_kgk": 880,
        },
        "insulation": {
            "conductivity_w_mk": 0.035,
            "density_kg_m3": 30,
            "specific_heat_j_kgk": 1400,
        },
        "plaster": {
            "conductivity_w_mk": 0.16,
            "density_kg_m3": 800,
            "specific_heat_j_kgk": 1090,
        },
        "timber": {
            "conductivity_w_mk": 0.13,
            "density_kg_m3": 500,
            "specific_heat_j_kgk": 1600,
        },
    },
    "material_surface_assumptions": "Thermal absorptance0.9, solar/visible absorptance0.5; smooth plaster, medium-rough others",
    "limitations": [
        "Synthetic models do not establish national stock representativeness or calibrated absolute demand",
        "Several building inputs vary together; results compare profiles rather than causal single-factor effects",
        "Constant18 C conditioned-floor boundary is uncalibrated and omits geographical soil-temperature differences, soil/snow physics and ground-boundary uncertainty",
        "Per-storey zones omit apartment, classroom and perimeter/core diversity",
        "Ideal sensible coil loads omit equipment efficiency, capacity limits, latent control and delivered fuel/electricity",
        "Prescribed weekly schedules omit holidays, school terms and occupant adaptation to weather",
        "Weather-dependent infiltration coefficients are scenario assumptions, not measured leakage or pressure-network results",
    ],
}

# Layers are listed outside to inside, with thicknesses in metres. All values are
# explicit scenario inputs, avoiding material/stock databases or implicit defaults.
ARCHETYPES = {
    "detached_old_light": {
        "label": "Detached, light envelope and leaky",
        "programme": "residential",
        "footprint_m": [12.0, 10.0],
        "storeys": 1,
        "storey_height_m": 2.7,
        "floor_area_m2": 120.0,
        "mass_description": "Light timber/plaster walls and roof; concrete floor",
        "wall_layers": [["timber", 0.018], ["insulation", 0.08], ["plaster", 0.013]],
        "roof_layers": [["timber", 0.018], ["insulation", 0.12], ["plaster", 0.013]],
        "floor_layers": [["insulation", 0.05], ["concrete", 0.10]],
        "intermediate_floor_layers": [["timber", 0.10], ["plaster", 0.013]],
        "wwr": 0.18,
        "window_u_w_m2k": 2.8,
        "shgc": 0.65,
        "visible_transmittance": 0.70,
        "infiltration_ach": 0.7,
        "infiltration_coefficients": [0.6, 0.01, 0.08, 0.0],
        "airflow_topology": "S",
        "natural_exchange_ach": 0.15,
        "mechanical_vent_l_s_m2": 0.0,
        "sensible_recovery": 0.0,
        "occupants_per_m2": 0.025,
        "activity_w_person": 120.0,
        "equipment_w_m2": 3.0,
        "equipment_heat_fraction": 1.0,
        "lighting_w_m2": 3.0,
        "schedule_kind": "residential",
        "heating_occupied_c": 20.0,
        "heating_unoccupied_c": 20.0,
        "cooling_occupied_c": 26.0,
        "cooling_unoccupied_c": 26.0,
    },
    "detached_new_heavy": {
        "label": "Detached, heavy envelope and insulated",
        "programme": "residential",
        "footprint_m": [12.0, 10.0],
        "storeys": 2,
        "storey_height_m": 2.8,
        "floor_area_m2": 240.0,
        "mass_description": "Insulation outside concrete inner walls, roof and floor",
        "wall_layers": [
            ["timber", 0.018],
            ["insulation", 0.20],
            ["concrete", 0.15],
            ["plaster", 0.013],
        ],
        "roof_layers": [["insulation", 0.30], ["concrete", 0.15], ["plaster", 0.013]],
        "floor_layers": [["insulation", 0.20], ["concrete", 0.15]],
        "intermediate_floor_layers": [["concrete", 0.15]],
        "wwr": 0.24,
        "window_u_w_m2k": 1.0,
        "shgc": 0.50,
        "visible_transmittance": 0.65,
        "infiltration_ach": 0.12,
        "infiltration_coefficients": [1.0, 0.0, 0.0, 0.0],
        "airflow_topology": "FTX",
        "natural_exchange_ach": 0.0,
        "mechanical_vent_l_s_m2": 0.35,
        "sensible_recovery": 0.8,
        "occupants_per_m2": 0.025,
        "activity_w_person": 120.0,
        "equipment_w_m2": 3.0,
        "equipment_heat_fraction": 1.0,
        "lighting_w_m2": 3.0,
        "schedule_kind": "residential",
        "heating_occupied_c": 20.0,
        "heating_unoccupied_c": 20.0,
        "cooling_occupied_c": 26.0,
        "cooling_unoccupied_c": 26.0,
    },
    "multifamily_old": {
        "label": "Multifamily, heavy envelope and exhaust ventilation",
        "programme": "residential",
        "footprint_m": [30.0, 12.0],
        "storeys": 4,
        "storey_height_m": 2.8,
        "floor_area_m2": 1440.0,
        "mass_description": "Concrete inner/outer walls and slabs; modest insulation",
        "wall_layers": [
            ["concrete", 0.10],
            ["insulation", 0.06],
            ["concrete", 0.15],
            ["plaster", 0.013],
        ],
        "roof_layers": [["insulation", 0.12], ["concrete", 0.15], ["plaster", 0.013]],
        "floor_layers": [["insulation", 0.08], ["concrete", 0.15]],
        "intermediate_floor_layers": [["concrete", 0.15]],
        "wwr": 0.22,
        "window_u_w_m2k": 2.6,
        "shgc": 0.60,
        "visible_transmittance": 0.70,
        "infiltration_ach": 0.35,
        "infiltration_coefficients": [0.7, 0.005, 0.08, 0.0],
        "airflow_topology": "F",
        "natural_exchange_ach": 0.0,
        "mechanical_vent_l_s_m2": 0.35,
        "sensible_recovery": 0.0,
        "occupants_per_m2": 0.030,
        "activity_w_person": 120.0,
        "equipment_w_m2": 3.0,
        "equipment_heat_fraction": 1.0,
        "lighting_w_m2": 3.0,
        "schedule_kind": "residential",
        "heating_occupied_c": 21.0,
        "heating_unoccupied_c": 21.0,
        "cooling_occupied_c": 26.0,
        "cooling_unoccupied_c": 26.0,
    },
    "multifamily_new": {
        "label": "Multifamily, heavy insulated envelope and heat recovery",
        "programme": "residential",
        "footprint_m": [30.0, 12.0],
        "storeys": 4,
        "storey_height_m": 2.8,
        "floor_area_m2": 1440.0,
        "mass_description": "Insulation outside concrete inner walls and slabs",
        "wall_layers": [
            ["timber", 0.018],
            ["insulation", 0.22],
            ["concrete", 0.18],
            ["plaster", 0.013],
        ],
        "roof_layers": [["insulation", 0.32], ["concrete", 0.18], ["plaster", 0.013]],
        "floor_layers": [["insulation", 0.20], ["concrete", 0.18]],
        "intermediate_floor_layers": [["concrete", 0.18]],
        "wwr": 0.25,
        "window_u_w_m2k": 0.9,
        "shgc": 0.48,
        "visible_transmittance": 0.65,
        "infiltration_ach": 0.10,
        "infiltration_coefficients": [1.0, 0.0, 0.0, 0.0],
        "airflow_topology": "FTX",
        "natural_exchange_ach": 0.0,
        "mechanical_vent_l_s_m2": 0.35,
        "sensible_recovery": 0.8,
        "occupants_per_m2": 0.030,
        "activity_w_person": 120.0,
        "equipment_w_m2": 3.0,
        "equipment_heat_fraction": 1.0,
        "lighting_w_m2": 3.0,
        "schedule_kind": "residential",
        "heating_occupied_c": 21.0,
        "heating_unoccupied_c": 21.0,
        "cooling_occupied_c": 26.0,
        "cooling_unoccupied_c": 26.0,
    },
    "office_glazed": {
        "label": "Office, glazed and high internal gains",
        "programme": "non_residential",
        "footprint_m": [30.0, 20.0],
        "storeys": 3,
        "storey_height_m": 3.3,
        "floor_area_m2": 1800.0,
        "mass_description": "Concrete walls and slabs behind insulated glazed envelope",
        "wall_layers": [
            ["concrete", 0.10],
            ["insulation", 0.16],
            ["concrete", 0.10],
            ["plaster", 0.013],
        ],
        "roof_layers": [["insulation", 0.20], ["concrete", 0.15], ["plaster", 0.013]],
        "floor_layers": [["insulation", 0.15], ["concrete", 0.15]],
        "intermediate_floor_layers": [["concrete", 0.15]],
        "wwr": 0.60,
        "window_u_w_m2k": 1.4,
        "shgc": 0.45,
        "visible_transmittance": 0.65,
        "infiltration_ach": 0.15,
        "infiltration_coefficients": [1.0, 0.0, 0.0, 0.0],
        "airflow_topology": "FTX",
        "natural_exchange_ach": 0.0,
        "mechanical_vent_l_s_m2": 1.2,
        "sensible_recovery": 0.7,
        "occupants_per_m2": 0.08,
        "activity_w_person": 120.0,
        "equipment_w_m2": 12.0,
        "equipment_heat_fraction": 0.70,
        "lighting_w_m2": 8.0,
        "schedule_kind": "office",
        "heating_occupied_c": 21.0,
        "heating_unoccupied_c": 17.0,
        "cooling_occupied_c": 25.0,
        "cooling_unoccupied_c": 29.0,
    },
    "school_daytime": {
        "label": "School, daytime gains and outdoor-air demand",
        "programme": "non_residential",
        "footprint_m": [40.0, 20.0],
        "storeys": 2,
        "storey_height_m": 3.3,
        "floor_area_m2": 1600.0,
        "mass_description": "Concrete walls and slabs; uniform classroom-like programme",
        "wall_layers": [
            ["concrete", 0.10],
            ["insulation", 0.16],
            ["concrete", 0.10],
            ["plaster", 0.013],
        ],
        "roof_layers": [["insulation", 0.20], ["concrete", 0.15], ["plaster", 0.013]],
        "floor_layers": [["insulation", 0.15], ["concrete", 0.15]],
        "intermediate_floor_layers": [["concrete", 0.15]],
        "wwr": 0.30,
        "window_u_w_m2k": 1.5,
        "shgc": 0.55,
        "visible_transmittance": 0.65,
        "infiltration_ach": 0.20,
        "infiltration_coefficients": [1.0, 0.0, 0.0, 0.0],
        "airflow_topology": "FTX",
        "natural_exchange_ach": 0.0,
        "mechanical_vent_l_s_m2": 2.5,
        "sensible_recovery": 0.7,
        "occupants_per_m2": 0.25,
        "activity_w_person": 110.0,
        "equipment_w_m2": 3.0,
        "equipment_heat_fraction": 1.0,
        "lighting_w_m2": 8.0,
        "schedule_kind": "school",
        "heating_occupied_c": 21.0,
        "heating_unoccupied_c": 17.0,
        "cooling_occupied_c": 26.0,
        "cooling_unoccupied_c": 30.0,
    },
}


def _daily(default: float, *periods: tuple[int, int, float]) -> tuple[float, ...]:
    """Declare non-overlapping hour-start intervals without implicit calendar rules."""
    values = [default] * 24
    for start, end, value in periods:
        values[start:end] = [value] * (end - start)
    return tuple(values)


def _weekly_profiles(profile: dict) -> dict[str, tuple[tuple[float, ...], ...]]:
    """Resolve explicit operational assumptions to seven Monday-first profiles."""
    kind = profile["schedule_kind"]
    if kind == "residential":
        occupancy = _daily(1.0, (8, 17, 0.35))
        weekend = _daily(1.0)
        equipment = _daily(0.25, (6, 8, 0.6), (17, 23, 1.0))
        equipment_weekend = _daily(0.25, (8, 23, 0.8))
        lighting = _daily(0.1, (6, 8, 0.8), (17, 23, 1.0))
        lighting_weekend = _daily(0.1, (8, 17, 0.4), (17, 23, 1.0))
        ventilation = ventilation_weekend = _daily(1.0)
        heating = heating_weekend = _daily(profile["heating_occupied_c"])
        cooling = cooling_weekend = _daily(profile["cooling_occupied_c"])
    else:
        start, end = (8, 18) if kind == "office" else (8, 16)
        preheat, setback = (7, 19) if kind == "office" else (6, 17)
        occupancy = _daily(0.0, (start, end, 1.0))
        weekend = _daily(0.0)
        equipment = _daily(0.15 if kind == "office" else 0.1, (start, end, 1.0))
        equipment_weekend = _daily(0.15 if kind == "office" else 0.1)
        lighting = _daily(0.05, (start - 1, end + 1, 1.0))
        lighting_weekend = _daily(0.05)
        ventilation = _daily(0.05, (start - 1, end + 1, 1.0))
        ventilation_weekend = _daily(0.05)
        heating = _daily(
            profile["heating_unoccupied_c"],
            (preheat, setback, profile["heating_occupied_c"]),
        )
        heating_weekend = _daily(profile["heating_unoccupied_c"])
        cooling = _daily(
            profile["cooling_unoccupied_c"],
            (preheat, setback, profile["cooling_occupied_c"]),
        )
        cooling_weekend = _daily(profile["cooling_unoccupied_c"])
    return {
        name: (weekday,) * 5 + (weekend_day,) * 2
        for name, weekday, weekend_day in (
            ("occupancy_fraction", occupancy, weekend),
            ("equipment_fraction", equipment, equipment_weekend),
            ("lighting_fraction", lighting, lighting_weekend),
            ("mechanical_vent_fraction", ventilation, ventilation_weekend),
            ("heating_setpoint", heating, heating_weekend),
            ("cooling_setpoint", cooling, cooling_weekend),
        )
    }


def describe_archetype(archetype_id: str) -> dict:
    """Return complete JSON-serializable assumptions without shared mutable leaves."""
    try:
        profile = deepcopy(ARCHETYPES[archetype_id])
    except KeyError as error:
        raise ValueError(
            f"Unknown archetype {archetype_id!r}; choose from {', '.join(ARCHETYPES)}"
        ) from error
    profile["archetype_id"] = archetype_id
    profile["common_assumptions"] = deepcopy(COMMON_ASSUMPTIONS)
    profile["weekday_profiles_monday_first"] = _weekly_profiles(profile)
    profile["volume_m3"] = profile["floor_area_m2"] * profile["storey_height_m"]
    profile["infiltration_formula"] = (
        "ACH_design * schedule * (C + T*abs(indoor-outdoor_C) + V*wind_m_s + W*wind_m_s**2)"
    )
    profile["infiltration_coefficient_units"] = ["dimensionless", "1/K", "s/m", "s²/m²"]
    profile["infiltration_basis"] = (
        "Scenario design ACH on actual conditioned volume, independent of mechanical/natural exchange; no n50 inference"
    )
    return profile


def archetype_hash(archetype_id: str) -> str:
    """Identify profile assumptions independently of weather, year and host paths."""
    payload = json.dumps(
        describe_archetype(archetype_id), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def make_archetype_spec(
    weather: str | Path,
    year: int,
    archetype_id: str,
    *,
    ground_temperatures_c: tuple[float, ...] | None = None,
) -> BuildingSpec:
    """Create a validated actual-year profile using native Core constructors only.

    Core validates the full hourly calendar and required weather inputs. In
    addition, actual source-year labels are required here, so a TMY cannot silently
    enter an AMY sensitivity experiment. ``physical_hash`` includes weather content
    and calendar year; ``archetype_hash`` separately identifies the profile template.
    The campaign uses the default constant18 C conditioned-floor boundary for
    all alternatives and site/years. Optional prescribed tuples support separate
    experiments; this constructor never estimates ground from candidate weather.
    The boundary is uncalibrated and excludes soil/snow physics.
    """
    if type(year) is not int or not 1900 <= year <= 2100:
        raise ValueError(
            "Study model requires an integer actual calendar year in [1900,2100]"
        )
    profile = describe_archetype(archetype_id)
    ground = GroundTemperatureSpec(
        monthly_temperatures_c=(18.0,) * 12
        if ground_temperatures_c is None
        else ground_temperatures_c,
        source=(
            f"{ASSUMPTION_VERSION}:fixed18C_uncalibrated_ground"
            if ground_temperatures_c is None
            else f"{ASSUMPTION_VERSION}:caller_supplied_frozen_ground_proxy_uncalibrated_assumption"
        ),
    )
    weather = Path(weather).resolve()
    validate_epw_calendar(weather, year)
    with weather.open(encoding="utf-8-sig") as stream:
        rows = csv.reader(stream)
        for _ in range(8):
            next(rows)
        if any(int(row[0]) != year for row in rows):
            raise ValueError(
                "Every study EPW row must belong to the requested AMY year"
            )
    materials = {
        name: MaterialSpec(
            name=f"weather study {name}",
            **properties,
            thermal_absorptance=0.9,
            solar_absorptance=0.5,
            visible_absorptance=0.5,
            roughness="Smooth" if name == "plaster" else "MediumRough",
        )
        for name, properties in COMMON_ASSUMPTIONS["materials"].items()
    }

    def assembly(role: str) -> ConstructionSpec:
        return ConstructionSpec(
            layers=tuple(
                LayerSpec(material=materials[name], thickness_m=thickness)
                for name, thickness in profile[f"{role}_layers"]
            )
        )

    weekly = profile["weekday_profiles_monday_first"]

    def schedule(name: str) -> ScheduleSpec:
        values = weekly[name]
        return ScheduleSpec(
            values=values[0],
            weekday_values=values,
            source=f"{ASSUMPTION_VERSION}:{archetype_id}:{name}",
        )

    floor_area = profile["floor_area_m2"]
    height = profile["storey_height_m"]
    wall = assembly("wall")
    intermediate_floor = assembly("intermediate_floor")
    return BuildingSpec(
        identifier=f"weather-sensitivity-{archetype_id}",
        load_reference_area_m2=floor_area,
        climate=climate_from_epw(weather, calendar_year=year),
        geometry=GeometrySpec(
            parts=(
                BuildingPart(
                    identifier="main",
                    footprint=Footprint.rectangle(*profile["footprint_m"]),
                    storey_elevations_m=tuple(
                        height * i for i in range(profile["storeys"] + 1)
                    ),
                ),
            ),
            orientation_deg=0,
            zoning=ZoningSpec(mode="per_storey"),
            roof=RoofSpec(shape="flat", topology="compact"),
        ),
        facade=FacadeSpec(
            default_wwr=profile["wwr"],
            glazing=GlazingSpec(
                u_value_w_m2k=profile["window_u_w_m2k"],
                solar_heat_gain_coefficient=profile["shgc"],
                visible_transmittance=profile["visible_transmittance"],
            ),
        ),
        envelope=EnvelopeSpec(
            wall=wall,
            top=assembly("roof"),
            ground=assembly("floor"),
            intermediate_floor=intermediate_floor,
            weather_shell=intermediate_floor,
            basement_wall=wall,
            thermal_bridge_uplift_fraction=0,
            ground_temperature=ground,
        ),
        airflow=AirflowSpec(
            topology=profile["airflow_topology"],
            envelope_infiltration_ach=profile["infiltration_ach"],
            infiltration_coefficients=tuple(profile["infiltration_coefficients"]),
            infiltration_source=f"{ASSUMPTION_VERSION}:{archetype_id}:analyst_selected_designflow_coefficients_not_measured",
            natural_exchange_ach=profile["natural_exchange_ach"],
            mechanical_vent_l_s_m2=profile["mechanical_vent_l_s_m2"],
            mechanical_vent_fraction=schedule("mechanical_vent_fraction"),
            sensible_recovery_effectiveness=profile["sensible_recovery"],
            latent_recovery_effectiveness=0,
        ),
        use=UseSpec(
            occupancy_category=profile["programme"],
            heating_setpoint=schedule("heating_setpoint"),
            cooling_setpoint=schedule("cooling_setpoint"),
            cooling_enabled=True,
            occupancy_fraction=schedule("occupancy_fraction"),
            equipment_fraction=schedule("equipment_fraction"),
            lighting_fraction=schedule("lighting_fraction"),
            occupant_density_per_m2=profile["occupants_per_m2"],
            occupant_activity_w=profile["activity_w_person"],
            equipment_gain_w_m2=profile["equipment_w_m2"],
            equipment_heat_fraction=profile["equipment_heat_fraction"],
            lighting_gain_w_m2=profile["lighting_w_m2"],
            dhw_kwh_m2_year=0,
        ),
    )


def make_spec(
    weather: str | Path,
    year: int = 2023,
    archetype_id: str = "detached_new_heavy",
    *,
    ground_temperatures_c: tuple[float, ...] | None = None,
) -> BuildingSpec:
    """Convenience constructor matching the original study's ``make_spec`` shape."""
    return make_archetype_spec(
        weather, year, archetype_id, ground_temperatures_c=ground_temperatures_c
    )
