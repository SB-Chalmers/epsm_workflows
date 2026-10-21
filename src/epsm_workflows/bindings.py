# SPDX-License-Identifier: MIT
"""Discover and apply physical inputs through the owning public Core APIs.

Bindings are named fields, not executable expressions or positional JSON paths.
Core supplies type schemas and physical validation. Changes apply to independent
Building copies and do not manufacture renovation material events.
"""

from __future__ import annotations

import math
import json
from copy import deepcopy

ALIASES = {
    "facade.wwr": "facade.default_wwr",
    "envelope.infiltration_ach": "hvac.airflow.envelope_infiltration_ach",
    "systems.ventilation.topology": "hvac.airflow.topology",
    "systems.ventilation.heat_recovery": "hvac.airflow.sensible_recovery_effectiveness",
    "geometry.floor_height_m": "geometry.storey_height_m",
    "reference_area_m2": "load_reference_area_m2",
}

PLANNED = {
    "geometry.heated_attic": "Conditioned attic authoring requires a validated Core thermal topology.",
    "geometry.detailed_zoning": "Arbitrary drawn thermal-zone authoring is not available in the current public Core model.",
    "facade.facade_offset": "Facade-offset semantics must be implemented and validated by Core.",
    "facade.wall_offset": "Wall-offset semantics must be implemented and validated by Core.",
    "facade.actual_windows": "Measured arbitrary opening polygons need a public Core authoring operation.",
    "operation.window_opening.behavior": "Behavioral opening is a separate method; scheduled natural exchange remains available.",
    "weather.tmy_generation": "Supply a documented TMY EPW; TMY generation is a separate weather method.",
    "envelope.element_u_value": "A U-value target needs an explicit construction recipe and adjustable layer; supply a resolved construction set until this binding is available.",
}


def canonical_binding(binding):
    """Return one physical identity for a supported spelling or alias."""
    return ALIASES.get(binding, binding)


def serialize_binding(binding, value):
    """Use owner lossless serializers for catalogue objects, not summary views."""
    from collections.abc import Mapping
    from .records import json_value

    if not isinstance(value, Mapping):
        if canonical_binding(binding) == "envelope.archetype":
            from epsm_data import archetype_to_dict

            return archetype_to_dict(value)
        if canonical_binding(binding) == "envelope.construction_set":
            from epsm_data import construction_set_to_dict

            return construction_set_to_dict(value)
    return json_value(value)


def native_unit(binding):
    """Describe owner-named units; this adapter never converts scientific values."""
    path = canonical_binding(binding)
    if path in {"operation.heating_setpoint", "operation.cooling_setpoint"}:
        return "degC"
    if any(
        path.startswith(prefix + ".")
        and path.rsplit(".", 1)[-1] in {"values", "weekday_values", "annual_values"}
        for prefix in ("operation.heating_setpoint", "operation.cooling_setpoint")
    ):
        return "degC"
    for suffix, unit in (
        ("_w_m2k", "W/(m2 K)"),
        ("_w_mk", "W/(m K)"),
        ("_j_kgk", "J/(kg K)"),
        ("_kg_m3", "kg/m3"),
        ("_l_s_m2", "L/(s m2)"),
        ("_w_per_l_s", "W/(L/s)"),
        ("_kwh_m2_year", "kWh/(m2 year)"),
        ("_w_m2", "W/m2"),
        ("_per_m2", "1/m2"),
        ("_ach", "1/h"),
        ("_deg", "deg"),
        ("_m2", "m2"),
        ("_kw", "kW"),
        ("_w", "W"),
        ("_c", "degC"),
        ("_m", "m"),
    ):
        if path.endswith(suffix):
            return unit
    if path in {
        "facade.default_wwr",
        "geometry.storeys",
        "thermal_bridge_uplift_fraction",
    } or any(
        marker in path
        for marker in ("fraction", "effectiveness", "efficiency", "coefficient", "shgc")
    ):
        return "1"
    return "native"


def validate_selector(selector):
    """Reject ambiguous/unsupported selectors before any physical assignment."""
    if not isinstance(selector, dict) or set(selector) - {
        "subjects",
        "groups",
        "facades",
        "storeys",
    }:
        raise ValueError(
            "Selectors support explicit subjects, groups, facades and storeys only"
        )
    for name, values in selector.items():
        if not isinstance(values, (list, tuple)) or not values:
            raise ValueError(f"Selector {name} requires a nonempty list")
        if name == "storeys":
            if any(type(value) is not int or value < 0 for value in values):
                raise ValueError("Storey selectors require nonnegative integer indices")
        elif any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError(f"Selector {name} requires nonempty identifiers")
        if len(set(values)) != len(values):
            raise ValueError(f"Selector {name} contains duplicate identifiers")


def _facade_override_field(path):
    if not path.startswith("facade."):
        return None
    field = path.split(".")[1]
    return (
        "wwr"
        if field == "default_wwr"
        else field
        if field in {"glazing", "layout", "reveal_depth_m", "overhang"}
        else None
    )


def _models():
    from epsm_core import (
        AirflowSpec,
        ContextSpec,
        DeliverySpec,
        FacadeSpec,
        FoundationSpec,
        GeometrySpec,
        GroundTemperatureSpec,
        OperationProfile,
        RenewablesSpec,
    )

    return {
        "geometry": GeometrySpec,
        "facade": FacadeSpec,
        "context": ContextSpec,
        "operation": OperationProfile,
        "hvac.airflow": AirflowSpec,
        "hvac.delivery": DeliverySpec,
        "hvac.renewables": RenewablesSpec,
        "foundation": FoundationSpec,
        "ground_temperature": GroundTemperatureSpec,
    }


def capabilities():
    """Return detached owner schemas, useful descriptions and supported bindings.

    This reads no files or services and starts no engine. Per-case engine
    applicability is checked separately using the selected model and settings.
    """
    result = {}
    for prefix, model in _models().items():
        schema = model.model_json_schema()
        definitions = schema.get("$defs", {})

        def walk(path, node, depth=0, definitions=definitions):
            if depth > 6:
                return
            value_schema = deepcopy(node)
            if (
                definitions
                and "$defs" not in value_schema
                and "#/$defs/" in json.dumps(node)
            ):
                value_schema["$defs"] = deepcopy(definitions)
            result[path] = {
                "status": "supported",
                "owner": "epsm_core",
                "schema": value_schema,
                "title": node.get("title", path),
            }
            if "$ref" in node:
                node = definitions.get(node["$ref"].rsplit("/", 1)[-1], {})
            for child in node.get("anyOf", []):
                if "$ref" in child:
                    node = definitions.get(child["$ref"].rsplit("/", 1)[-1], {})
                    break
            for name, child in node.get("properties", {}).items():
                walk(path + "." + name, child, depth + 1)

        walk(prefix, schema)
    for name in (
        "geometry.storeys",
        "geometry.storey_height_m",
        "geometry.total_height_m",
    ):
        result[name] = {
            "status": "supported",
            "owner": "epsm_core",
            "schema": {
                "type": "integer" if name.endswith("storeys") else "number",
                "exclusiveMinimum": 0,
            },
        }
    for name in (
        "thermal_bridge_uplift_fraction",
        "load_reference_area_m2",
        "load_reference_area_policy",
    ):
        result[name] = {"status": "supported", "owner": "epsm_core", "schema": {}}
    from epsm_core import UnconditionedExchangeSpec, ZoneUseSpec
    from pydantic import TypeAdapter

    for name, model in (
        ("zone_uses", ZoneUseSpec),
        ("unconditioned_exchange", UnconditionedExchangeSpec),
    ):
        result[name] = {
            "status": "supported",
            "owner": "epsm_core",
            "schema": TypeAdapter(list[model]).json_schema(),
        }
    for alias, target in ALIASES.items():
        result[alias] = {**deepcopy(result[target]), "binding": target}
    for name, reason in PLANNED.items():
        result[name] = {
            "status": "unsupported",
            "owner": "epsm_core" if not name.startswith("weather") else "smhi2epw",
            "reason": reason,
        }
    result["envelope.archetype"] = {
        "status": "supported",
        "owner": "epsm",
        "schema": {"type": "object"},
    }
    result["envelope.construction_set"] = {
        "status": "supported",
        "owner": "epsm_data",
        "schema": {"type": "object"},
    }
    for name, entry in result.items():
        if entry["status"] == "supported":
            entry["selectors"] = ["subjects", "groups"] + (
                ["facades", "storeys"]
                if _facade_override_field(canonical_binding(name))
                else []
            )
            entry["unit"] = native_unit(name)
    return result


def read_binding(building, binding):
    """Inspect an inherited physical field for a declarative condition, without I/O."""
    path = canonical_binding(binding)
    if path in {
        "geometry.storeys",
        "geometry.storey_height_m",
        "geometry.total_height_m",
    }:
        if building.geometry is None or len(building.geometry.parts) != 1:
            raise ValueError("Dimension conditions require one explicit building part")
        part = building.geometry.parts[0]
        if path.endswith("storeys"):
            return part.floors
        if path.endswith("total_height_m"):
            return part.storey_elevations_m[-1]
        heights = [
            right - left
            for left, right in zip(
                part.storey_elevations_m, part.storey_elevations_m[1:]
            )
        ]
        if not all(math.isclose(value, heights[0]) for value in heights):
            raise ValueError(
                "A nonuniform building has no single inherited storey height"
            )
        return heights[0]
    root, *tail = path.split(".")
    if root == "envelope":
        raise ValueError(
            "Conditions need physical input fields, not catalogue application commands"
        )
    value = getattr(building, root, None)
    if value is None:
        return None
    if tail:
        value = (
            value.to_dict()
            if hasattr(value, "to_dict")
            else value.model_dump(mode="json")
        )
        for field in tail:
            if not isinstance(value, dict) or field not in value:
                return None
            value = value[field]
    if path in {"operation.heating_setpoint", "operation.cooling_setpoint"}:
        from epsm_core import ScheduleSpec

        schedule = ScheduleSpec.model_validate(value)
        if len(set(schedule.all_values)) == 1:
            return schedule.all_values[0]
    return deepcopy(value)


def _patch(mapping, path, value):
    if len(path) == 1:
        mapping[path[0]] = value
    else:
        child = mapping.get(path[0])
        if child is None:
            child = {}
            mapping[path[0]] = child
        if not isinstance(child, dict):
            raise ValueError("Binding traverses a non-object field")
        _patch(child, path[1:], value)


def apply_bindings(building, values, *, selectors=None):
    """Return an independently authored Building using public owner validation.

    Parameters
    ----------
    building : epsm.Building or epsm_core.Building
        Source model, preserved unchanged including its earlier results.
    values : mapping
        Registered binding IDs to concrete values in their physical units.
    selectors : mapping, optional
        Per-binding facade/storey selectors. Other physical selectors are rejected
        until a corresponding public assignment operation exists.
    """
    from epsm_core import FacadeOverride, HVACSystem, OperationProfile, ScheduleSpec

    registry = capabilities()
    candidate = type(building).from_dict(building.to_dict())
    paths = [canonical_binding(binding) for binding in values]
    if len(set(paths)) != len(paths):
        raise ValueError("Duplicate physical binding aliases")
    if any(
        right.startswith(left + ".")
        for left in paths
        for right in paths
        if left != right
    ):
        raise ValueError(
            "Conflicting whole-object and leaf bindings; apply fixed inputs before variations"
        )
    roots, geometry_dimensions, scalar = {}, {}, {}
    # Declarative dictionaries have no operation order. Apply selected catalogue
    # assumptions first, then construction overrides and explicit physical fields.
    priority = {"envelope.archetype": 0, "envelope.construction_set": 1}
    for binding in sorted(
        values,
        key=lambda key: (
            priority.get(canonical_binding(key), 2),
            canonical_binding(key),
        ),
    ):
        value = values[binding]
        if binding not in registry:
            raise ValueError(f"Unknown physical binding: {binding}")
        if registry[binding]["status"] != "supported":
            raise ValueError(
                f"Unsupported binding {binding}: {registry[binding]['reason']}"
            )
        path = canonical_binding(binding)
        selection = (selectors or {}).get(binding, {})
        validate_selector(selection)
        if selection and (selection.get("facades") or selection.get("storeys")):
            field = _facade_override_field(path)
            if field is None:
                raise ValueError("Facade selectors are not supported for this binding")
            if not selection.get("facades"):
                raise ValueError(
                    "A storey override requires explicit facade identifiers"
                )
            if candidate.facade is None or candidate.geometry is None:
                raise ValueError(
                    "Facade selectors require supplied geometry and facade inputs"
                )
            catalog = {
                facade.facade_id: facade for facade in candidate.geometry.facades()
            }
            facade_data = roots.setdefault(
                "facade", candidate.facade.model_dump(mode="json")
            )
            overrides = facade_data.setdefault("overrides", [])
            for facade in selection["facades"]:
                if facade not in catalog:
                    raise ValueError(f"Unknown facade selector: {facade}")
                for storey in selection.get("storeys", [None]):
                    if storey is not None and storey not in catalog[facade].storeys:
                        raise ValueError(f"Unknown storey {storey} for facade {facade}")
                    found = next(
                        (
                            x
                            for x in overrides
                            if x["facade_id"] == facade and x.get("storey") == storey
                        ),
                        None,
                    )
                    if found is None:
                        found = {"facade_id": facade, "storey": storey}
                        overrides.append(found)
                    tail = path.split(".")[2:]
                    if tail:
                        if found.get(field) is None:
                            from epsm_core import FacadeSpec

                            inherited = FacadeSpec.model_validate(
                                facade_data
                            ).value_for(field, facade, storey)
                            found[field] = inherited.model_dump(mode="json")
                        _patch(found[field], tail, value)
                    else:
                        found[field] = value
                    FacadeOverride.model_validate(found)
            continue
        if path in {
            "geometry.storeys",
            "geometry.storey_height_m",
            "geometry.total_height_m",
        }:
            geometry_dimensions[path.split(".")[-1]] = value
            continue
        if path == "envelope.archetype":
            from epsm_data import archetype_from_dict

            candidate.apply_archetype(archetype_from_dict(value))
            continue
        if path == "envelope.construction_set":
            from epsm_data import construction_set_from_dict

            candidate.assign_inputs(construction_set=construction_set_from_dict(value))
            continue
        root, *tail = path.split(".")
        if not tail:
            scalar[root] = value
            continue
        if root not in roots:
            current = getattr(candidate, root)
            if current is None:
                raise ValueError(f"Supply {root} before applying a leaf binding")
            roots[root] = (
                current.to_dict() if root == "hvac" else current.model_dump(mode="json")
            )
        if (
            root == "operation"
            and tail in (["heating_setpoint"], ["cooling_setpoint"])
            and isinstance(value, (int, float))
        ):
            value = ScheduleSpec.constant(value).model_dump(mode="json")
        _patch(roots[root], tail, value)
    if geometry_dimensions:
        from epsm_core import GeometrySpec

        geometry = (
            GeometrySpec.model_validate(roots["geometry"])
            if "geometry" in roots
            else candidate.geometry
        )
        if geometry is None or len(geometry.parts) != 1:
            raise ValueError("Dimension shortcuts require one explicit building part")
        if (
            "total_height_m" in geometry_dimensions
            and "storey_height_m" in geometry_dimensions
        ):
            raise ValueError("Choose total height or storey height as authoritative")
        part = geometry.parts[0]
        floors = geometry_dimensions.get("storeys", part.floors)
        if isinstance(floors, bool) or int(floors) != floors or floors < 1:
            raise ValueError("storeys must be a positive integer")
        if (
            "storey_height_m" not in geometry_dimensions
            and "total_height_m" not in geometry_dimensions
        ):
            read_binding(candidate, "geometry.storey_height_m")
        height = geometry_dimensions.get(
            "storey_height_m", part.storey_elevations_m[-1] / part.floors
        )
        if "total_height_m" in geometry_dimensions:
            height = geometry_dimensions["total_height_m"] / floors
        newpart = part.updated(
            storey_elevations_m=tuple(i * height for i in range(int(floors) + 1))
        )
        roots["geometry"] = geometry.updated(parts=(newpart,)).model_dump(mode="json")
    for root in ("geometry", "facade", "context", "foundation", "ground_temperature"):
        if root in roots:
            setattr(candidate, root, roots[root])
        if root in scalar:
            setattr(candidate, root, scalar.pop(root))
    assignments = {}
    if "hvac" in roots:
        assignments["hvac"] = HVACSystem(**roots["hvac"])
    if "operation" in roots:
        assignments["operation"] = OperationProfile.model_validate(roots["operation"])
    if "operation" in scalar:
        assignments["operation"] = OperationProfile.model_validate(
            scalar.pop("operation")
        )
    if assignments:
        candidate.assign_inputs(**assignments)
    for root, value in scalar.items():
        setattr(candidate, root, value)
    return candidate
