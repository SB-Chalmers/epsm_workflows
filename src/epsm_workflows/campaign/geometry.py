# SPDX-License-Identifier: MIT
"""Procedural footprint families, frozen real footprints and scene normalization."""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
from epsm_core import ContextSpec, Footprint
from shapely.affinity import affine_transform, translate
from shapely.geometry import Polygon, mapping, shape

from ..records import atomic_json, digest

FOOTPRINT_FORMAT = "epsm-campaign-footprints/1"


def footprint(polygon):
    return Footprint(
        exterior=tuple(polygon.exterior.coords)[:-1],
        holes=tuple(tuple(ring.coords)[:-1] for ring in polygon.interiors),
    )


def localized(polygon):
    x, y, _, _ = polygon.bounds
    return translate(polygon, -x, -y), (x, y)


def minimum_area(family, aspect, cl, cw):
    """Smallest outer area whose remaining limbs stay at least four metres wide."""
    if family in ("rectangle", "real"):
        return 0
    if family == "L":
        outer = max(16 / (aspect * (1 - cl) ** 2), 16 * aspect / (1 - cw) ** 2)
        return outer * (1 - cl * cw)
    if family == "U":
        outer = max(64 / (aspect * (1 - cl) ** 2), 36 * aspect / (1 - cw) ** 2)
        return outer * (1 - cl * cw)
    outer = max(16 / (aspect * cl**2), 16 * aspect / cw**2)
    return outer * (1 - (1 - 2 * cl) * (1 - 2 * cw))


def procedural(family, area, aspect, cut_l, cut_w):
    """Build a rectangle, L, U or courtyard polygon of the requested net area."""
    removed = (
        cut_l * cut_w
        if family in ("L", "U")
        else ((1 - 2 * cut_l) * (1 - 2 * cut_w) if family == "courtyard" else 0)
    )
    outer = area / (1 - removed)
    length, width = math.sqrt(outer * aspect), math.sqrt(outer / aspect)
    if family == "rectangle":
        return Footprint.rectangle(length, width).polygon
    if family == "L":
        return Footprint.l_shape(length, width, length * cut_l, width * cut_w).polygon
    if family == "U":
        return Footprint.u_shape(length, width, length * cut_l, width * cut_w).polygon
    if family != "courtyard":
        raise ValueError(f"Unknown footprint family {family}")
    return Polygon(
        [(0, 0), (length, 0), (length, width), (0, width)],
        [
            [
                (length * cut_l, width * cut_w),
                (length * (1 - cut_l), width * cut_w),
                (length * (1 - cut_l), width * (1 - cut_w)),
                (length * cut_l, width * (1 - cut_w)),
            ]
        ],
    )


def normalize_scene(geometry, context, angle):
    """Bake clockwise rotation, then rebase every object at the target bbox corner."""
    if geometry.orientation_deg != 0:
        raise ValueError("Scene normalization expects unrotated input geometry")
    a = math.radians(angle)
    c, s = math.cos(a), math.sin(a)
    rotation = [c, s, -s, c, 0, 0]
    polygons = [affine_transform(p.footprint.polygon, rotation) for p in geometry.parts]
    xmin, ymin = min(p.bounds[0] for p in polygons), min(p.bounds[1] for p in polygons)
    forward = [c, s, -s, c, -xmin, -ymin]
    inverse = [c, -s, s, c, c * xmin - s * ymin, s * xmin + c * ymin]
    parts = tuple(
        p.updated(footprint=footprint(affine_transform(p.footprint.polygon, forward)))
        for p in geometry.parts
    )
    buildings = tuple(
        b.updated(footprints=tuple(footprint(affine_transform(p.polygon, forward)) for p in b.footprints))
        for b in context.buildings
    )
    result = geometry.updated(parts=parts, orientation_deg=0, source_crs="local_metres")
    minima = [min(p.footprint.polygon.bounds[k] for p in result.parts) for k in (0, 1)]
    if not np.allclose(minima, 0, atol=1e-8, rtol=0):
        raise ValueError("Simulation geometry must be normalized with baked orientation")
    return (
        result,
        ContextSpec(buildings=buildings, generation=context.generation),
        {
            "sampled_orientation_deg": angle,
            "forward_affine": forward,
            "inverse_affine": inverse,
            "origin": "target_bounding_box_bottom_left",
            "units": "m",
        },
    )


def load_footprints(path):
    """Read a frozen footprint file; every entry carries its local-metre polygon."""
    if path is None:
        return []
    data = json.loads(Path(path).read_text())
    if data.get("format") != FOOTPRINT_FORMAT:
        raise ValueError(f"Expected {FOOTPRINT_FORMAT} in {path}")
    footprints = []
    for item in data["footprints"]:
        polygon = shape(item["polygon"])
        if polygon.geom_type != "Polygon" or not polygon.is_valid:
            raise ValueError(f"Invalid frozen footprint {item['footprint_id']}")
        footprints.append({**item, "polygon": polygon, "area_m2": float(polygon.area)})
    return footprints


def freeze_footprints(records, destination, *, minimum_area_m2=250, maximum_aspect=4):
    """Clean, localise and freeze real footprints from portable EPSM records.

    Records are ``epsm-record-1`` dictionaries (``BuildingRecord.to_dict``) or any
    mapping with ``building_id``, GeoJSON ``geometry`` in metres and optional
    ``facts.storeys``. Rejected geometry is reported, not silently dropped.
    """
    from epsm_core import prepare_geometry

    accepted, rejected = [], []
    for record in records:
        data = record.get("record", record)
        identifier = str(data.get("building_id"))
        polygon = shape(data["geometry"]) if data.get("geometry") else None
        reason = None
        if polygon is None or polygon.geom_type != "Polygon":
            reason = "not a single polygon"
        else:
            prepared = prepare_geometry(polygon)
            if prepared.status != "accepted":
                reason = f"geometry preparation {prepared.status}"
            else:
                polygon = prepared.cleaned_geometry
                x, y, xx, yy = polygon.bounds
                aspect = max(xx - x, yy - y) / max(min(xx - x, yy - y), 1e-9)
                if polygon.area < minimum_area_m2:
                    reason = f"area {polygon.area:.0f} m2 below {minimum_area_m2}"
                elif aspect > maximum_aspect or polygon.buffer(-2).is_empty:
                    reason = "too slender for a four-metre limb"
        if reason:
            rejected.append({"building_id": identifier, "reason": reason})
            continue
        local, origin = localized(polygon)
        facts = data.get("facts") or {}
        storeys = facts.get("storeys") or data.get("storeys")
        accepted.append(
            {
                "footprint_id": identifier,
                "dataset_id": data.get("dataset_id"),
                "polygon": mapping(local),
                "source_origin_m": list(origin),
                "storeys": int(storeys) if storeys else None,
                "area_m2": float(local.area),
                "polygon_sha256": digest(mapping(local)),
            }
        )
    accepted.sort(key=lambda item: item["footprint_id"])
    atomic_json(
        destination,
        {"format": FOOTPRINT_FORMAT, "footprints": accepted, "rejected": rejected},
    )
    return {"accepted": len(accepted), "rejected": rejected}
