# SPDX-License-Identifier: MIT
"""Pinned materials and the explicit campaign construction set.

The assemblies are the reviewed concrete-and-mineral-wool fabric of the 2026-09
campaign, expressed as an ``epsm_data`` construction set so a Core ``Building``
owns them and both engines read the same physical elements. Insulation depths
are placeholders until ``Building.assign_element_u_value`` solves them for the
sampled U-values; concrete depths are the sampled thermal mass.
"""
from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from uuid import NAMESPACE_URL, uuid5

from epsm_data import (
    BuildingElement,
    Component,
    ConstructionSet,
    Element,
    Layer,
    LayerMaterial,
    LayerSlot,
    OpaqueMaterial,
    Roughness,
    ShearingLayer,
    WindowElement,
)

ARCHETYPE = "epsm_campaign"
# Role -> (insulation layer index to solve, parts). ``None`` depths are sampled.
INSULATED = {
    BuildingElement.EXT_WALL: 1,
    BuildingElement.TOP_FLOOR: 1,
    BuildingElement.GROUND_FLOOR: 1,
}


@lru_cache(maxsize=1)
def materials():
    data = json.loads(files("epsm_workflows.campaign").joinpath("data/materials.json").read_text())
    return {
        key: OpaqueMaterial(
            name=value["name"],
            json_id=f"{ARCHETYPE}.{key}",
            boverket_id=None,
            material_type="campaign_pinned",
            roughness=Roughness(value["roughness"]),
            density=value["density_kg_m3"],
            specific_heat=value["specific_heat_j_kgk"],
            thermal_absorptance=value["thermal_absorptance"],
            solar_absorptance=value["solar_absorptance"],
            visible_absorptance=value["visible_absorptance"],
            conductivity=value["conductivity_w_mk"],
            uuid=uuid5(NAMESPACE_URL, f"epsm:campaign:material:{key}"),
        )
        for key, value in data.items()
    }


def component_name(role):
    return f"{ARCHETYPE}.{role.value}"


def _element(role, parts):
    m = materials()
    layers = []
    for position, (key, thickness) in enumerate(parts):
        name = f"{ARCHETYPE}.{role.value}.{position}.{key}"
        layer = Layer(name, name, thickness, (LayerMaterial(m[key], 1, thickness),))
        layers.append(LayerSlot(layer, position))
    component = Component(component_name(role), ARCHETYPE, role, tuple(layers), ShearingLayer.STRUCTURE)
    return Element(f"{ARCHETYPE}.{role.value}", ARCHETYPE, None, (component,), role)


def construction_set(p):
    """Assemblies with the sampled concrete depths and glazing; insulation unsolved."""
    elements = {
        BuildingElement.EXT_WALL: _element(
            BuildingElement.EXT_WALL,
            [("concrete", 0.08), ("mw_old", 0.1), ("concrete", p["wall_concrete_m"]), ("gypsum", 0.013)],
        ),
        # Flat compact roof: weather shell outside, insulation, then the roof slab.
        BuildingElement.TOP_FLOOR: _element(
            BuildingElement.TOP_FLOOR,
            [("roof_felt", 0.006), ("mw_old", 0.2), ("conc_roof", p["roof_concrete_m"])],
        ),
        BuildingElement.ROOF: _element(BuildingElement.ROOF, [("roof_felt", 0.006), ("conc_roof", 0.15)]),
        BuildingElement.GROUND_FLOOR: _element(
            BuildingElement.GROUND_FLOOR,
            [("conc_floor", 0.16), ("eps", 0.08), ("dry_sand", 0.05), ("conc_floor", 0.05)],
        ),
        BuildingElement.INTERMEDIATE_FLOOR: _element(
            BuildingElement.INTERMEDIATE_FLOOR, [("conc_floor", p["floor_concrete_m"])]
        ),
        BuildingElement.BASEMENT_WALL: _element(
            BuildingElement.BASEMENT_WALL, [("conc_bsmt", 0.2), ("mw_old", 0.085)]
        ),
        BuildingElement.WINDOW: WindowElement(
            f"{ARCHETYPE}.WINDOW", ARCHETYPE, u_value=p["window_u"], shgc=p["shgc"]
        ),
    }
    return ConstructionSet(
        version_id=uuid5(NAMESPACE_URL, "epsm:campaign:construction_set"), elements=elements
    )


def solve_envelope(building, p):
    """Size each insulated role's mineral wool or EPS for the sampled nominal U."""
    targets = {
        BuildingElement.EXT_WALL: p["wall_u"],
        BuildingElement.TOP_FLOOR: p["roof_u"],
        BuildingElement.GROUND_FLOOR: p["ground_u"],
    }
    for role, target in targets.items():
        ids = [e.identifier for e in building.elements if e.role == role]
        if not ids:
            continue
        building.assign_element_u_value(
            target, component_name=component_name(role), layer_index=INSULATED[role], element_ids=ids
        )
    return building
