"""Construct and simulate the weather sensitivity building with EPSM Core.

BuildingSpec owns geometry, constructions, gains, airflow and controls.
EnergyPlusRunner invokes Core's compiler, validates the weather calendar and
checks simulation diagnostics/results. No EnergyPlus geometry is handwritten.
This illustrative model is not calibrated to a measured Gothenburg building.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

import epsm_core
from epsm_core import (
    AirflowSpec,
    BuildingPart,
    BuildingSpec,
    ConstructionSpec,
    EnergyPlusRunner,
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
from epsm_core.settings import CompilerSettings, HourlyOutputRequest

if __package__:
    from . import metrics
else:
    import metrics

ENGINE = Path("/Applications/OpenStudio-3.9.0/EnergyPlus/energyplus-24.2.0")
SPEC = {
    "model_constructor": "epsm_core.BuildingSpec + EnergyPlusRunner/EnergyPlusCompiler",
    "floor_area_m2": 100.0,
    "zone_height_m": 3.0,
    "volume_m3": 300.0,
    "orientation": "10 x 10 m square aligned to geographic north",
    "zoning": "one whole-building zone; compact flat roof, no basement",
    "wall_layers_outside_to_inside": "150 mm concrete / 120 mm insulation / 13 mm plaster",
    "roof_layers_outside_to_inside": "160 mm insulation / 150 mm concrete / 13 mm plaster",
    "floor_layers_outside_to_inside": "100 mm insulation / 150 mm concrete",
    "concrete_conductivity_w_mk": 1.4,
    "insulation_conductivity_w_mk": 0.035,
    "plaster_conductivity_w_mk": 0.16,
    "default_window_to_wall_ratio": 0.2,
    "window_placement": "EPSM Core WindowLayoutSpec defaults; equal facade WWR",
    "glazing_u_w_m2k": 1.8,
    "glazing_shgc": 0.55,
    "glazing_visible_transmittance": 0.65,
    "infiltration_ach": 0.3,
    "infiltration_coefficients": [1, 0, 0, 0],
    "people_per_m2": 0.02,
    "occupant_activity_w_person": 120.0,
    "lighting_w_m2": 3.0,
    "equipment_w_m2": 3.0,
    "gains_schedule": "constant 24 hours/day, all year",
    "ground_temperature_c": 18.0,
    "ground_temperature_basis": "EPSM Core prescribed-temperature default; explicit uncalibrated boundary",
    "heating_setpoint_c": 20.0,
    "cooling_setpoint_c": 26.0,
    "hvac": "Core ideal loads, NoLimit heating/cooling, no humidity control",
    "ventilation": "natural topology S, 0.3 ACH infiltration only; no additional mechanical air",
    "dhw_kwh_m2_year": 0.0,
    "timestep_minutes": 10,
    "reported_interval_minutes": 60,
    "calendar": "Core explicit requested year, exact EPW calendar validation, no DST",
    "limitations": "Illustrative box; fixed ground boundary and gains; delivered sensible loads are not HVAC electricity/fuel",
}


def make_spec(weather: str | Path, year: int = 2023) -> BuildingSpec:
    """Create the complete fixed building through EPSM Core specification classes."""
    weather = Path(weather).resolve()
    if not isinstance(year, int) or not 1900 <= year <= 2100:
        raise ValueError("Study model requires an integer actual calendar year")
    lines = weather.read_text(encoding="utf-8-sig").splitlines()
    if any(int(line.split(",")[0]) != year for line in lines[8:]):
        raise ValueError("Every study EPW row must belong to the requested AMY year")
    concrete = MaterialSpec(
        name="study concrete",
        conductivity_w_mk=1.4,
        density_kg_m3=2200,
        specific_heat_j_kgk=880,
    )
    insulation = MaterialSpec(
        name="study insulation",
        conductivity_w_mk=0.035,
        density_kg_m3=30,
        specific_heat_j_kgk=1400,
        solar_absorptance=0.5,
        visible_absorptance=0.5,
    )
    plaster = MaterialSpec(
        name="study plaster",
        conductivity_w_mk=0.16,
        density_kg_m3=800,
        specific_heat_j_kgk=1090,
        solar_absorptance=0.5,
        visible_absorptance=0.5,
        roughness="Smooth",
    )
    slab_layer = LayerSpec(material=concrete, thickness_m=0.15)
    plaster_layer = LayerSpec(material=plaster, thickness_m=0.013)
    slab = ConstructionSpec(layers=(slab_layer,))
    wall = ConstructionSpec(
        layers=(
            slab_layer,
            LayerSpec(material=insulation, thickness_m=0.12),
            plaster_layer,
        )
    )
    roof = ConstructionSpec(
        layers=(
            LayerSpec(material=insulation, thickness_m=0.16),
            slab_layer,
            plaster_layer,
        )
    )
    floor = ConstructionSpec(
        layers=(LayerSpec(material=insulation, thickness_m=0.10), slab_layer)
    )
    return BuildingSpec(
        identifier="weather-sensitivity-box-100m2",
        climate=climate_from_epw(weather, calendar_year=year),
        geometry=GeometrySpec(
            parts=(
                BuildingPart(
                    identifier="main",
                    footprint=Footprint.rectangle(10, 10),
                    storey_elevations_m=(0, 3),
                ),
            ),
            zoning=ZoningSpec(mode="whole_building"),
            roof=RoofSpec(shape="flat", topology="compact"),
        ),
        facade=FacadeSpec(
            default_wwr=0.2,
            glazing=GlazingSpec(
                u_value_w_m2k=1.8,
                solar_heat_gain_coefficient=0.55,
                visible_transmittance=0.65,
            ),
        ),
        envelope=EnvelopeSpec(
            wall=wall,
            top=roof,
            ground=floor,
            intermediate_floor=slab,
            weather_shell=slab,
            basement_wall=wall,
            ground_temperature=GroundTemperatureSpec(
                monthly_temperatures_c=(18.0,) * 12,
                source="weather_sensitivity_fixed_18C_uncalibrated_assumption",
            ),
        ),
        airflow=AirflowSpec(
            topology="S",
            envelope_infiltration_ach=0.3,
            natural_exchange_ach=0,
            mechanical_vent_l_s_m2=0,
            infiltration_coefficients=(1, 0, 0, 0),
            infiltration_source="weather_sensitivity_fixed_0.3ACH_assumption",
        ),
        use=UseSpec(
            occupancy_category="residential",
            heating_setpoint=ScheduleSpec.constant(20),
            cooling_setpoint=ScheduleSpec.constant(26),
            cooling_enabled=True,
            occupancy_fraction=ScheduleSpec.constant(1),
            equipment_fraction=ScheduleSpec.constant(1),
            lighting_fraction=ScheduleSpec.constant(1),
            occupant_density_per_m2=0.02,
            occupant_activity_w=120,
            equipment_gain_w_m2=3,
            lighting_gain_w_m2=3,
            dhw_kwh_m2_year=0,
        ),
    )


def simulate(
    weather: str | Path,
    output_dir: str | Path,
    *,
    year: int = 2023,
    executable: str | Path = ENGINE,
    spec: BuildingSpec | None = None,
    assumptions: dict | None = None,
) -> dict:
    """Run Core's compiler and validated runner in a new empty directory."""
    spec = spec if spec is not None else make_spec(weather, year)
    if (
        spec.climate.weather_sha256
        != hashlib.sha256(Path(weather).read_bytes()).hexdigest()
    ):
        raise ValueError("Building specification weather differs from supplied EPW")
    output = Path(output_dir).resolve()
    settings = CompilerSettings(
        hourly_outputs=(
            HourlyOutputRequest(name=metrics.HEATING_VARIABLE),
            HourlyOutputRequest(name=metrics.COOLING_VARIABLE),
        )
    )
    runner = EnergyPlusRunner(energyplus=executable, settings=settings)
    identity = {
        "year": year,
        "weather_sha256": spec.climate.weather_sha256,
        "building_spec_sha256": spec.physical_hash(),
        "core_compiler_sha256": runner.compiler.compiler_hash,
        "engine_sha256": runner.compiler.engine_hash,
        "engine_schema_sha256": runner.compiler.schema_hash,
        "building_constructor_sha256": hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
        "metrics_source_sha256": hashlib.sha256(
            Path(metrics.__file__).read_bytes()
        ).hexdigest(),
    }
    receipt_path = output / "receipt.json"
    if receipt_path.exists():
        saved = json.loads(receipt_path.read_text(encoding="utf-8"))
        mismatches = [key for key, value in identity.items() if saved.get(key) != value]
        if mismatches:
            raise ValueError(
                f"Existing simulation identity differs ({', '.join(mismatches)}); use a fresh output directory"
            )
        model_path = output / "model.epJSON"
        diagnostics = json.loads(
            (output / "diagnostics.json").read_text(encoding="utf-8")
        )
        if (
            not (output / "result.json").is_file()
            or not diagnostics.get("accepted")
            or diagnostics.get("severe_count")
            or diagnostics.get("fatal_count")
            or saved.get("model_sha256")
            != hashlib.sha256(model_path.read_bytes()).hexdigest()
            or saved.get("core_run_id") != runner.compiler.run_id(spec)
        ):
            raise ValueError(
                "Existing simulation is incomplete or changed; use a fresh output directory"
            )
        _, current_summary = metrics.extract(
            output / "eplusout.sql", year=year, utc_offset=spec.climate.time_zone_hours
        )
        if current_summary != saved.get("metrics"):
            raise ValueError(
                "Existing simulation metrics changed; use a fresh output directory"
            )
        return saved
    result = runner.run(spec, output, timeout_seconds=180)
    diagnostics = result.diagnostics
    if diagnostics["severe_count"] or diagnostics["fatal_count"]:
        raise RuntimeError("EPSM Core returned severe/fatal simulation diagnostics")
    model_path = output / "model.epJSON"
    model = json.loads(model_path.read_text(encoding="utf-8"))
    systems = model["ZoneHVAC:IdealLoadsAirSystem"]
    if any(
        system.get("heating_limit") != "NoLimit"
        or system.get("cooling_limit") != "NoLimit"
        for system in systems.values()
    ):
        raise ValueError("Weather study requires unlimited ideal-load heating/cooling")
    hourly, summary = metrics.extract(
        output / "eplusout.sql", year=year, utc_offset=spec.climate.time_zone_hours
    )
    hourly.to_csv(output / "hourly-loads.csv", index=False)
    errors = (output / "eplusout.err").read_text(encoding="utf-8")
    receipt = {
        **identity,
        "year": year,
        "weather_path": str(Path(weather).resolve()),
        "weather_sha256": spec.climate.weather_sha256,
        "model_sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
        "building_spec_sha256": spec.physical_hash(),
        "core_package_version": epsm_core.__version__,
        "core_package_path": str(Path(epsm_core.__file__).parent),
        "core_compiler_sha256": runner.compiler.compiler_hash,
        "core_run_id": result.run_id,
        "core_diagnostics_accepted": diagnostics["accepted"],
        "core_result_path": str(output / "result.json"),
        "core_building_spec_path": str(output / "building.json"),
        "engine_path": str(runner.compiler.executable),
        "engine_sha256": runner.compiler.engine_hash,
        "severe_errors": diagnostics["severe_count"],
        "fatal_errors": diagnostics["fatal_count"],
        "warning_count": len(
            re.findall(r"\*\*\s+Warning\s+\*\*", errors, flags=re.IGNORECASE)
        ),
        "building_assumptions": assumptions if assumptions is not None else SPEC,
        "metrics": summary,
        "hourly_loads_path": str(output / "hourly-loads.csv"),
    }
    (output / "receipt.json").write_text(
        json.dumps(receipt, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weather", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--year", type=int, default=2023)
    parser.add_argument("--engine", default=str(ENGINE))
    arguments = parser.parse_args()
    result = simulate(
        arguments.weather,
        arguments.output,
        year=arguments.year,
        executable=arguments.engine,
    )
    print(json.dumps(result["metrics"], indent=2))
