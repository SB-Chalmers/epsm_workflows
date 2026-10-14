"""Training-fitted mixed-distance k-medoids and stability diagnostics.

References: EPSM Data cohorts.descriptor_distance; docs/cohort-transfer.md.
Fit at most 600 independent-property exemplars to bound pairwise memory, then
assign every training/evaluation building with the frozen descriptor transform.
"""

from __future__ import annotations

import numpy as np
from epsm_data.cohorts import descriptor_distance
from sklearn.metrics import adjusted_rand_score

from .common import SEED, descriptors, digest, fit_transform


def distances(rows, medoids, transform):
    return np.array(
        [
            [
                descriptor_distance(descriptors(r), descriptors(m), transform)
                for m in medoids
            ]
            for r in rows
        ]
    )


def fit_group(rows, k, *, minimum=15):
    """PAM-style deterministic alternate medoid updates; targets are unused."""
    transform = fit_transform(rows)
    by_property = {}
    for row in sorted(rows, key=lambda r: r["building_id"]):
        by_property.setdefault(row["property_key"], row)
    sample = sorted(
        by_property.values(), key=lambda r: digest((SEED, r["property_key"]))
    )[:600]
    if k > 1 and len(sample) < minimum * k:
        raise ValueError("Too few independent training properties for subdivision")
    pairwise = distances(sample, sample, transform)
    selected = [int(np.argmin(pairwise.sum(axis=0)))]
    while len(selected) < k:
        selected.append(int(np.argmax(pairwise[:, selected].min(axis=1))))
        if len(set(selected)) != len(selected):
            raise ValueError("No distinct physical medoids")
    for _ in range(30):
        labels = pairwise[:, selected].argmin(axis=1)
        proposed = []
        for label in range(k):
            indices = np.flatnonzero(labels == label)
            if not len(indices):
                raise ValueError("Empty medoid cluster")
            proposed.append(
                int(indices[np.argmin(pairwise[np.ix_(indices, indices)].sum(axis=0))])
            )
        if proposed == selected:
            break
        selected = proposed
    medoids = [sample[i] for i in selected]
    d = distances(rows, medoids, transform)
    labels = d.argmin(axis=1)
    counts = [
        len({rows[i]["property_key"] for i in np.flatnonzero(labels == c)})
        for c in range(k)
    ]
    if k > 1 and min(counts) < minimum:
        raise ValueError("Subdivision has too few independent properties")
    missing_patterns = [
        "|".join(str(r.get(key) is None) for key in transform["numeric"]) for r in rows
    ]
    missing_ari = float(adjusted_rand_score(labels, missing_patterns))
    return {
        "k": k,
        "transform": transform,
        "medoids": [descriptors(r) for r in medoids],
        "medoid_ids": [r["building_id"] for r in medoids],
        "labels": labels.tolist(),
        "property_counts": counts,
        "missingness_ari": missing_ari,
        "distance_limits": [
            float(np.quantile(d[labels == c, c], 0.95)) for c in range(k)
        ],
    }


def assign(rows, model):
    d = distances(rows, model["medoids"], model["transform"])
    return d.argmin(axis=1).tolist(), d.min(axis=1).tolist()


def representatives(rows, model, label, limit=5):
    """Central medoid plus bounded descriptor contrasts from different properties."""
    labels, d = assign(rows, model)
    candidates = [
        (r, distance)
        for r, c, distance in zip(rows, labels, d, strict=True)
        if c == label
    ]
    central = next(
        (r for r, _ in candidates if r["building_id"] == model["medoid_ids"][label]),
        min(candidates, key=lambda p: (p[1], p[0]["building_id"]))[0],
    )
    selected = [central]
    for row, distance in sorted(candidates, key=lambda p: (-p[1], p[0]["building_id"])):
        if distance > model["distance_limits"][label] or row["property_key"] in {
            r["property_key"] for r in selected
        }:
            continue
        selected.append(row)
        if len(selected) >= limit:
            break
    return selected


def stability(rows, model, *, resamples=20, minimum=15):
    """Property subsamples refit transforms; compare labels on identical buildings."""
    if model["k"] == 1:
        return {"median_ari": 1.0, "resample_ari": [1.0] * resamples}
    properties = sorted({r["property_key"] for r in rows})
    rng = np.random.default_rng(SEED)
    scores = []
    for _ in range(resamples):
        chosen = set(
            rng.choice(
                properties, max(model["k"], int(0.8 * len(properties))), replace=False
            )
        )
        try:
            alternative = fit_group(
                [r for r in rows if r["property_key"] in chosen],
                model["k"],
                minimum=max(1, int(0.8 * minimum)),
            )
            labels, _ = assign(rows, alternative)
            scores.append(float(adjusted_rand_score(model["labels"], labels)))
        except ValueError:
            scores.append(0.0)
    return {"median_ari": float(np.median(scores)), "resample_ari": scores}
