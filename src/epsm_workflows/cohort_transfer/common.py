"""Frozen protocol, descriptor transforms and property-level evaluation.

References: docs/cohort-transfer.md; Kristensen et al. (2018), hierarchical
archetype calibration, https://www.sciencedirect.com/science/article/pii/S0378778818312532.
All historical observations remain retrospective; folding does not create fresh data.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np

from ..campaign import file_sha256

SEED = 20261003
PARENT = ("use_group", "era", "ventilation", "heating_family", "correction_status")
NUMERIC = (
    "wall_area_per_atemp",
    "roof_area_per_atemp",
    "ground_area_per_atemp",
    "volume_per_atemp",
    "compactness",
    "storeys",
    "atemp_per_apartment",
    "gross_to_atemp_ratio",
    "designed_flow_l_s_m2",
    "transmission_w_k_per_atemp",
    "air_exchange_w_k_per_atemp",
    "context_shaded_fraction",
)
CATEGORICAL = (
    "roof_form",
    "building_position",
    "heated_basement",
    "renovation_timing",
    "completed_window_replacement",
    "programme_context",
)
DEFAULTS = {
    "schema_version": 1,
    "seed": SEED,
    "outer_folds": 5,
    "inner_folds": 3,
    "minimum_cluster_training_properties": 15,
    "minimum_release_properties": 30,
    "cluster_resamples": 20,
    "minimum_cluster_ari": 0.7,
    "minimum_subdivision_mae_gain": 0.05,
    "target_coverage20": 0.9,
    "max_clusters": 5,
    "morris_trajectories": 4,
    "joint_designs": 32,
    "bootstrap_resamples": 1000,
    "workers": 8,
    "parallel_backend": "processes",
    "engine": "/Applications/OpenStudio-3.9.0/EnergyPlus/energyplus",
    "study_kind": "retrospective_property_cross_validation",
}


def read(path):
    return json.loads(Path(path).read_text())


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, allow_nan=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def cleaned(value):
    """Convert source numpy/missing values to portable strict-JSON inputs."""
    if isinstance(value, dict):
        return {str(k): cleaned(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [cleaned(v) for v in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def parent_key(row):
    return "__".join(str(row.get(key, "unknown")) for key in PARENT)


def descriptors(row):
    """Whitelist only features available without the heating observation."""
    return {k: row.get(k) for k in NUMERIC + CATEGORICAL + PARENT}


def property_folds(rows, n=5, *, seed=SEED):
    """Stable property-atomic folds, independent of row order and target values."""
    properties = sorted(
        {r["property_key"] for r in rows}, key=lambda p: digest((seed, p))
    )
    if n < 2 or len(properties) < n:
        raise ValueError("At least one independent property per fold is required")
    return {p: i % n for i, p in enumerate(properties)}


def fit_transform(rows):
    """Train-only robust scales; entirely absent features are omitted explicitly."""
    columns, medians, scales = [], {}, {}
    for key in NUMERIC:
        values = [
            r[key]
            for r in rows
            if isinstance(r.get(key), (int, float)) and math.isfinite(r[key])
        ]
        if len(values) < max(2, len(rows) / 2):
            continue
        columns.append(key)
        medians[key] = float(np.median(values))
        scales[key] = max(float(np.subtract(*np.percentile(values, [75, 25]))), 1e-6)
    if not columns:
        raise ValueError("No adequately observed physical descriptors")
    return {
        "numeric": columns,
        "categorical": list(CATEGORICAL),
        "medians": medians,
        "scales": scales,
    }


def metrics(rows, predictions):
    """Retain failed predictions in coverage; do not fabricate an error magnitude."""
    errors, absolute, ape, successful = [], [], [], []
    property_losses = {}
    for row, prediction in zip(rows, predictions, strict=True):
        target = row["target"]
        if not math.isfinite(target) or target <= 0:
            raise ValueError("Measured heating targets must be finite and positive")
        okay = prediction is not None and math.isfinite(prediction) and prediction >= 0
        successful.append(okay)
        # Same explicit capped selection loss as calibration.objective; each
        # property has equal influence even when it contains several buildings.
        property_losses.setdefault(row["property_key"], []).append(
            min(abs(prediction - target) / target, 2.0) if okay else 2.0
        )
        if okay:
            errors.append(prediction - target)
            absolute.append(abs(prediction - target))
            ape.append(abs(prediction - target) / target)
    count = len(rows)
    return {
        "attempted": count,
        "accepted": sum(successful),
        "failed": count - sum(successful),
        "independent_properties": len({r["property_key"] for r in rows}),
        "coverage20": sum(a < 0.2 for a in ape) / count if count else None,
        "coverage10": sum(a < 0.1 for a in ape) / count if count else None,
        "mae_kwh_m2": float(np.mean(absolute)) if absolute else None,
        "bias_kwh_m2": float(np.mean(errors)) if errors else None,
        "median_ape_percent": float(np.median(ape) * 100) if ape else None,
        "maximum_ape_percent": max(ape) * 100 if ape else None,
        "property_balanced_capped_ape_loss": float(
            np.mean([np.mean(values) for values in property_losses.values()])
        )
        if property_losses
        else None,
    }


def bootstrap(rows, predictions, n=1000):
    """Resample independent properties, preserving failures and sibling buildings."""
    groups = {}
    for i, row in enumerate(rows):
        groups.setdefault(row["property_key"], []).append(i)
    keys = sorted(groups)
    if not keys:
        return {}
    rng = np.random.default_rng(SEED)
    draws = []
    for _ in range(n):
        ids = [i for k in rng.choice(keys, len(keys), replace=True) for i in groups[k]]
        m = metrics([rows[i] for i in ids], [predictions[i] for i in ids])
        draws.append((m["coverage20"], m["coverage10"]))
    ci = np.quantile(draws, [0.025, 0.975], axis=0)
    return {
        "properties": len(keys),
        "resamples": n,
        "coverage20_95_interval": ci[:, 0].tolist(),
        "coverage10_95_interval": ci[:, 1].tolist(),
    }


def source_identity():
    """Pin source/resources actually available to this protocol across packages."""
    import sys
    from importlib.metadata import version

    import epsm_core
    import epsm_data
    import smhi2epw

    identity = {"python": sys.version}
    for package in ("numpy", "pandas", "scikit-learn", "shapely", "pydantic"):
        identity[f"version:{package}"] = version(package)
    for module in (epsm_core, epsm_data, smhi2epw):
        directory = Path(module.__file__).parent
        for path in sorted(directory.rglob("*")):
            if (
                path.is_file()
                and path.suffix in (".py", ".json")
                and "__pycache__" not in path.parts
            ):
                identity[str(path)] = file_sha256(path)
    for path in sorted(Path(__file__).parents[1].rglob("*.py")):
        identity[str(path)] = file_sha256(path)
    return identity


def guard(root):
    """Refuse resume after any source/input/spec/weather/engine identity changes."""
    root = Path(root)
    manifest = read(root / "manifest.json")
    cache_manifest = root / "native-cache-import.json"
    if cache_manifest.exists():
        for filename, checksum in read(cache_manifest)["inputs"].items():
            if file_sha256(filename) != checksum:
                raise ValueError(f"Archived native cache artifact changed: {filename}")
    if manifest["sources"] != source_identity():
        raise ValueError(
            "Protocol or dependency sources changed; prepare a new campaign"
        )
    for name, sha in manifest["inputs"].items():
        if not Path(name).is_file() or file_sha256(name) != sha:
            raise ValueError(f"Frozen campaign input changed: {name}")
    for name, sha in manifest["prepared"].items():
        if file_sha256(root / name) != sha:
            raise ValueError(f"Prepared campaign file changed: {name}")
    return manifest
