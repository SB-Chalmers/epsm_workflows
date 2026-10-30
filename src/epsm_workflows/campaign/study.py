# SPDX-License-Identifier: MIT
"""Frozen study definitions for Vera training-data campaigns.

A study is one JSON document. Its digest is the campaign identity, so the same
file always plans the same cases. Paths are resolved against the study file.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..records import digest

KINDS = ("stock_diversity", "weather_sweep", "before_after", "ventilation_sweep")
FAMILIES = ("rectangle", "L", "U", "courtyard")
TOPOLOGIES = ("S", "F", "FT", "FTX")
SCHEDULE_FAMILIES = ("constant", "daily")
FLOOR_CHOICES = tuple(range(2, 9))
SHIFT_CHOICES = tuple(range(-2, 3))

STOCK_BOUNDS = {
    "footprint_area_m2": [250, 2500],
    "floor_height_m": [2.6, 3.1],
    "aspect_ratio": [1, 4],
    "orientation_deg": [0, 360],
    "wall_u": [0.17, 1.5],
    "roof_u": [0.1, 0.85],
    "ground_u": [0.2, 1.7],
    "window_u": [1.1, 3],
    "shgc": [0.4, 0.75],
    "wwr_north": [0.06, 0.29],
    "wwr_south": [0.08, 0.33],
    "wwr_east": [0.07, 0.32],
    "wwr_west": [0.08, 0.34],
    "infiltration_ach": [0.1, 1],
    "natural_ach": [0.2, 0.7],
    "mechanical_l_s_m2": [0.25, 0.5],
    "recovery": [0, 0.8],
    "heating_setpoint_c": [19.5, 22.5],
    "occupants_per_m2": [0.012, 0.022],
    "equipment_w_m2": [1.2, 2.2],
    "lighting_w_m2": [0, 1.5],
    "dhw_kwh_m2_year": [18, 30],
    "bridge_uplift": [0, 0.35],
    "wall_concrete_m": [0.1, 0.25],
    "roof_concrete_m": [0.12, 0.25],
    "floor_concrete_m": [0.12, 0.25],
    "cut_l_unit": [0, 1],
    "cut_w_unit": [0, 1],
}
VENTILATION_BOUNDS = {
    "infiltration_ach": [0.1, 1],
    "natural_ach": [0.2, 0.7],
    "mechanical_l_s_m2": [0.25, 0.5],
    "recovery": [0, 0.8],
}


class _Definition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class CitySpec(_Definition):
    """One site with a reviewed TMY and an optional measured-year AMY."""

    slug: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    city: str
    tmy: str
    amy: str | None = None
    amy_station_id: int | None = None
    amy_provenance: str | None = None


class WeatherFile(_Definition):
    """One explicit EPW crossed with every building of a weather sweep."""

    weather_id: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    path: str
    calendar_year: int = Field(ge=1900, le=2200)
    location: str = ""
    kind: Literal["tmy", "amy"] = "amy"


class ContextPreset(_Definition):
    """A named synthetic neighbourhood built from one Core preset plus overrides."""

    name: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    preset: Literal["rural", "urban_lowrise_dense", "urban_highrise_dense"]
    overrides: dict[str, Any] = Field(default_factory=dict)
    max_buildings: int = Field(default=512, ge=1)
    radius_margin_m: float = Field(default=100, gt=0)


DEFAULT_CONTEXTS = (
    ContextPreset(
        name="urban_lowrise_sparse",
        preset="urban_lowrise_dense",
        overrides={"spacing_m": 64, "size_range_m": [20, 26], "height_range_m": [6, 15], "clearance_m": 20},
    ),
    ContextPreset(name="urban_lowrise_dense", preset="urban_lowrise_dense"),
    ContextPreset(name="urban_highrise_dense", preset="urban_highrise_dense"),
)


class GeometrySources(_Definition):
    """Procedural families plus an optional frozen set of real footprints.

    ``real_fraction`` of each stratum's designs use real footprints; the rest
    are shared equally by the procedural families. ``anchor_footprints`` limits
    how many real footprints the cross-site anchor cohort cycles through so the
    pilot stays small.
    """

    families: list[Literal["rectangle", "L", "U", "courtyard"]] = Field(default_factory=lambda: list(FAMILIES))
    real_footprints: str | None = None
    real_fraction: float = Field(default=0.5, ge=0, le=1)
    anchor_footprints: int = Field(default=10, ge=1)

    @model_validator(mode="after")
    def _some_source(self):
        if not self.families and self.real_footprints is None:
            raise ValueError("Select procedural families, real footprints or both")
        if len(set(self.families)) != len(self.families):
            raise ValueError("Procedural families must be unique")
        return self


class ExecutionSettings(_Definition):
    checkpoint_runs: int = Field(default=8, ge=1)
    checkpoint_seconds: float = Field(default=300, ge=1)
    production_min_batch_size: int = Field(default=32, ge=1)
    production_max_batch_size: int = Field(default=128, ge=1)
    pilot_concurrency: int = Field(default=64, ge=1)
    production_concurrency: int = Field(default=128, ge=1)
    timeout_seconds: float = Field(default=1800, gt=0)
    startup_jitter_seconds: float = Field(default=30, ge=0)
    excluded_nodes: list[str] = Field(default_factory=list)
    account: str | None = None
    notification_email: str | None = None


class StockSection(_Definition):
    """Seed-42 style stock design: diversity designs per city plus cross-site anchors."""

    diversity_designs: int = Field(default=9000, ge=1)
    anchor_designs: int = Field(default=200, ge=0)
    bounds: dict[str, list[float]] = Field(default_factory=lambda: {k: list(v) for k, v in STOCK_BOUNDS.items()})

    @model_validator(mode="after")
    def _bounds(self):
        if set(self.bounds) != set(STOCK_BOUNDS):
            raise ValueError("Stock bounds must name exactly the known design inputs")
        for key, (low, high) in self.bounds.items():
            if not low < high:
                raise ValueError(f"Bounds for {key} must increase")
        return self


class WeatherSweepSection(_Definition):
    """A fixed set of buildings crossed with many explicit weather files."""

    buildings: int = Field(default=40, ge=1)
    weather: list[WeatherFile] = Field(min_length=1)
    context: str = "urban_lowrise_dense"
    bounds: dict[str, list[float]] = Field(default_factory=lambda: {k: list(v) for k, v in STOCK_BOUNDS.items()})


class BeforeAfterSection(_Definition):
    """Existing-state buildings, each rerun with one catalogue measure at a time."""

    buildings: int = Field(default=40, ge=1)
    archetypes: list[str] = Field(default_factory=lambda: [f"mf_se0{i}" for i in range(1, 8)])
    measures: list[str] | Literal["all"] = "all"
    context: str = "urban_lowrise_dense"
    weather_kind: Literal["tmy", "amy"] = "tmy"


class VentilationSection(_Definition):
    """Fixed geometry, materials and weather; only airflow inputs vary."""

    buildings: int = Field(default=20, ge=1)
    samples_per_building: int = Field(default=40, ge=1)
    topologies: list[Literal["S", "F", "FT", "FTX"]] = Field(default_factory=lambda: list(TOPOLOGIES))
    bounds: dict[str, list[float]] = Field(default_factory=lambda: {k: list(v) for k, v in VENTILATION_BOUNDS.items()})
    context: str = "urban_lowrise_dense"
    weather_kind: Literal["tmy", "amy"] = "tmy"
    building_bounds: dict[str, list[float]] = Field(default_factory=lambda: {k: list(v) for k, v in STOCK_BOUNDS.items()})


class StudySpec(_Definition):
    """One self-describing training-data study; its digest is the campaign identity."""

    format: Literal["epsm-campaign/1"] = "epsm-campaign/1"
    kind: Literal["stock_diversity", "weather_sweep", "before_after", "ventilation_sweep"]
    name: str = Field(min_length=1, pattern=r"^[a-z0-9_-]+$")
    seed: int = 42
    calendar_year: int = Field(default=2018, ge=1900, le=2200)
    cities: list[CitySpec] = Field(default_factory=list)
    contexts: list[ContextPreset] = Field(default_factory=lambda: list(DEFAULT_CONTEXTS))
    geometry: GeometrySources = Field(default_factory=GeometrySources)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    stock: StockSection | None = None
    weather_sweep: WeatherSweepSection | None = None
    before_after: BeforeAfterSection | None = None
    ventilation: VentilationSection | None = None

    @model_validator(mode="after")
    def _sections(self):
        sections = {
            "stock_diversity": self.stock,
            "weather_sweep": self.weather_sweep,
            "before_after": self.before_after,
            "ventilation_sweep": self.ventilation,
        }
        if sections[self.kind] is None:
            raise ValueError(f"A {self.kind} study needs its section")
        if len({c.slug for c in self.cities}) != len(self.cities):
            raise ValueError("City slugs must be unique")
        if len({c.name for c in self.contexts}) != len(self.contexts):
            raise ValueError("Context names must be unique")
        if self.kind != "weather_sweep" and not self.cities:
            raise ValueError(f"A {self.kind} study needs at least one city")
        for section in (self.weather_sweep, self.before_after, self.ventilation):
            if section is not None and section.context not in {c.name for c in self.contexts}:
                raise ValueError(f"Unknown context {section.context}")
        return self

    @property
    def section(self):
        return {
            "stock_diversity": self.stock,
            "weather_sweep": self.weather_sweep,
            "before_after": self.before_after,
            "ventilation_sweep": self.ventilation,
        }[self.kind]

    @property
    def id(self):
        return digest(self.to_dict())

    def to_dict(self):
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, value):
        return cls.model_validate(value)

    def summary(self):
        return {
            "kind": self.kind,
            "name": self.name,
            "seed": self.seed,
            "cities": [c.slug for c in self.cities],
            "contexts": [c.name for c in self.contexts],
            "id": self.id,
        }


def load_study(path):
    """Read a study file and resolve its relative input paths against it."""
    path = Path(path).resolve()
    data = json.loads(path.read_text())
    base = path.parent

    def resolve(value):
        if value is None:
            return None
        candidate = Path(value)
        return str(candidate if candidate.is_absolute() else (base / candidate).resolve())

    for city in data.get("cities", []):
        for key in ("tmy", "amy", "amy_provenance"):
            if city.get(key):
                city[key] = resolve(city[key])
    if data.get("geometry", {}).get("real_footprints"):
        data["geometry"]["real_footprints"] = resolve(data["geometry"]["real_footprints"])
    for item in (data.get("weather_sweep") or {}).get("weather", []):
        item["path"] = resolve(item["path"])
    return StudySpec.from_dict(data)
