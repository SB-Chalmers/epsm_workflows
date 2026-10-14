"""Audit existing stock-study eligibility without opening energy target files.

Reference: stock/calibration.py physics gates and the existing study's immutable
config/evaluation-opened/prior-study-registry receipts. An opened study cannot
supply a fresh holdout by reshuffling its previously examined properties.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from .campaign import atomic_json, file_sha256


def audit(study):
    """Return conservative prospective blockers and original gate requirements."""
    root = Path(study).resolve()
    config = json.loads((root / "config.json").read_text())
    path = root / "simulation-candidates.parquet"
    frame = pd.read_parquet(
        path,
        columns=[
            "building_id",
            "property_key",
            "split",
            "archetype_id",
            "ventilation",
            "climate_region",
            "heating_family",
            "prior_study_property",
        ],
    )
    groups = []
    for key, g in frame.groupby(
        ["archetype_id", "ventilation", "climate_region", "heating_family"],
        dropna=False,
    ):
        counts = g.groupby("split").property_key.nunique().to_dict()
        groups.append(
            {
                "family": [None if pd.isna(k) else str(k) for k in key],
                "buildings": len(g),
                "independent_properties": g.property_key.nunique(),
                "split_properties": counts,
                "original_count_gates_passed": len(g) >= config["physics_min_buildings"]
                and all(
                    counts.get(s, 0) >= n
                    for s, n in config["physics_min_properties"].items()
                ),
            }
        )
    opened = (root / "evaluation-opened.json").exists()
    sources = [path, root / "config.json", root / "prior-study-registry.json"]
    if opened:
        sources.append(root / "evaluation-opened.json")
    return {
        "schema_version": 1,
        "source_hashes": {str(p): file_sha256(p) for p in sources},
        "target_files_read": False,
        "candidate_buildings": len(frame),
        "families": groups,
        "minimum_buildings": config["physics_min_buildings"],
        "minimum_independent_properties": config["physics_min_properties"],
        "study_outcomes_already_opened": opened,
        "reshuffle_creates_fresh_holdout": False,
        "evidence_approved": config["physics_evidence_approved"],
        "weather_manifest": config["physics_weather_manifest"],
        "prospective_confirmation_ready": False,
        "blockers": [
            "Existing study outcomes already inspected"
            if opened
            else "Untouched-property lineage requires review",
            "Reviewed building boundary evidence and local weather required",
        ],
        "interpretation": "Inventory only; groups are not a newly declared calibration cohort",
    }


def main(argv=None):
    """Write a new audit without modifying the prior stock study."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise FileExistsError(args.output)
    atomic_json(args.output, audit(args.study))


if __name__ == "__main__":
    main()
