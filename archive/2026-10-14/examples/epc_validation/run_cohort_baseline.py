"""Evaluate a frozen, membership-reviewed native baseline before calibration.

Reference: docs/cohort-transfer.md and docs/archetype-membership.md. Source-only
membership quarantines have their own denominator; eligible native model failures
remain in accuracy coverage. Filtering changes the population, not model accuracy.
"""

import argparse
from pathlib import Path

from epsm_workflows.campaign import atomic_json, file_sha256
from epsm_workflows.cohort_transfer.common import (
    bootstrap,
    digest,
    guard,
    metrics,
    read,
)
from epsm_workflows.cohort_transfer.preparation import TargetStore
from epsm_workflows.cohort_transfer.simulation import Evaluator


def run(campaign, workers):
    root = Path(campaign).resolve()
    guard(root)
    rows = read(root / "features.json")
    if not rows:
        raise ValueError("No eligible cohort buildings")
    frozen = root / "baseline-frozen.json"
    atomic_json(
        frozen,
        {
            "parameters": {},
            "fitted": False,
            "campaign_manifest_sha256": digest(read(root / "manifest.json")),
            "helper_sha256": file_sha256(__file__),
            "runtime_workers": workers,
            "membership_eligible_buildings": len(rows),
        },
    )
    evaluator = Evaluator(root, workers=workers)
    try:
        values = evaluator.batch(rows, {"baseline": {}})
    finally:
        evaluator.close()
    records = [
        {
            "building_id": row["building_id"],
            "prediction": values[row["building_id"], "baseline"],
        }
        for row in rows
    ]
    atomic_json(root / "baseline-predictions-before-targets.json", records)
    observations = TargetStore(root).evaluation(rows, frozen)
    predictions = [row["prediction"] for row in records]
    audit = read(root / "audit.json")
    report = {
        "metrics": metrics(observations, predictions),
        "uncertainty": bootstrap(
            observations, predictions, read(root / "config.json")["bootstrap_resamples"]
        ),
        "source_buildings": audit["source_buildings"],
        "membership_eligible": audit["membership_eligible"],
        "membership_quarantined": audit["membership_quarantined"],
        "classification_is_not_calibration_improvement": True,
        "retrospective": True,
    }
    guard(root)
    atomic_json(root / "baseline-evaluation.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    print(run(args.campaign, args.workers))
