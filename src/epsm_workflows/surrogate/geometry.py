"""Metric templates and one shared affine transform for target and neighbours."""

import math
from pathlib import Path

import numpy as np
from shapely.affinity import affine_transform, scale, translate
from shapely.geometry import Polygon, mapping, shape

from epsm_core import ContextSpec, Footprint
from epsm_core.preparation import prepare_geometry

from .common import digest, read_json


def footprint(polygon):
    return Footprint(
        exterior=tuple(polygon.exterior.coords)[:-1],
        holes=tuple(tuple(r.coords)[:-1] for r in polygon.interiors),
    )


def localized(polygon):
    x, y, _, _ = polygon.bounds
    return translate(polygon, -x, -y), (x, y)


def normalize_scene(geometry, context, angle):
    """Bake clockwise rotation, then rebase every object at target bbox bottom-left."""
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
        b.updated(
            footprints=tuple(
                footprint(affine_transform(p.polygon, forward)) for p in b.footprints
            )
        )
        for b in context.buildings
    )
    result = geometry.updated(parts=parts, orientation_deg=0, source_crs="local_metres")
    assert_normalized(result)
    return (
        result,
        ContextSpec(buildings=buildings, generation=context.generation),
        dict(
            sampled_orientation_deg=angle,
            forward_affine=forward,
            inverse_affine=inverse,
            origin="target_bounding_box_bottom_left",
            units="m",
        ),
    )


def assert_normalized(geometry):
    minima = [
        min(p.footprint.polygon.bounds[k] for p in geometry.parts) for k in (0, 1)
    ]
    if not np.allclose(minima, 0, atol=1e-8, rtol=0) or geometry.orientation_deg != 0:
        raise ValueError(
            "Simulation geometry must be normalized with baked orientation"
        )


def procedural(family, area, aspect, cut_l, cut_w):
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


def minimum_area(family, aspect, cl, cw):
    if family == "rectangle":
        return 0
    if family == "L":
        outer = max(16 / (aspect * (1 - cl) ** 2), 16 * aspect / (1 - cw) ** 2)
        return outer * (1 - cl * cw)
    if family == "U":
        outer = max(64 / (aspect * (1 - cl) ** 2), 36 * aspect / (1 - cw) ** 2)
        return outer * (1 - cl * cw)
    outer = max(16 / (aspect * cl**2), 16 * aspect / cw**2)
    return outer * (1 - (1 - 2 * cl) * (1 - 2 * cw))


def select_templates(corpus, count=10):
    candidates = []
    for path in sorted(Path(corpus).glob("*.json")):
        data = read_json(path)
        if data.get("status") != "accepted" or "cleaned_geometry" not in data:
            continue
        p = shape(data["cleaned_geometry"])
        if p.geom_type != "Polygon" or p.area < 250 or len(p.exterior.coords) < 9:
            continue
        prepared = prepare_geometry(p)
        if prepared.status != "accepted":
            continue
        p = prepared.cleaned_geometry
        x, y, xx, yy = p.bounds
        aspect = max(xx - x, yy - y) / min(xx - x, yy - y)
        if aspect > 4 or p.buffer(-2).is_empty:
            continue
        candidates.append(
            (
                path,
                data,
                p,
                [
                    p.length / math.sqrt(p.area),
                    aspect,
                    1 - p.area / p.convex_hull.area,
                    len(p.exterior.coords) - 1,
                    len(p.interiors),
                ],
            )
        )
    if len(candidates) < count:
        raise ValueError(
            f"Only {len(candidates)} eligible complex templates; need {count}"
        )
    features = np.array([c[3] for c in candidates], dtype=float)
    features = (features - features.min(axis=0)) / np.maximum(
        np.ptp(features, axis=0), 1e-12
    )
    selected = [
        min(range(len(candidates)), key=lambda i: digest(candidates[i][0].name))
    ]
    while len(selected) < count:
        distances = np.min(
            np.sum((features[:, None] - features[selected]) ** 2, axis=2), axis=1
        )
        distances[selected] = -1
        selected.append(int(np.argmax(distances)))
    records = []
    for i in selected:
        path, data, p, descriptors = candidates[i]
        local, origin = localized(p)
        records.append(
            dict(
                template_id=path.stem,
                source=data,
                source_file=path.name,
                source_origin_m=list(origin),
                template=mapping(local),
                descriptors=descriptors,
                template_sha256=digest(mapping(local)),
                usage="source-derived geometry with synthetic building physics",
            )
        )
    return sorted(records, key=lambda r: r["template_id"])


def scaled_template(record, area):
    p = shape(record["template"])
    factor = math.sqrt(area / p.area)
    return scale(p, xfact=factor, yfact=factor, origin=(0, 0)), factor
