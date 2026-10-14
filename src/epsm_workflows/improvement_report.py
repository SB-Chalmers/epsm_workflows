"""Interpret frozen multifamily diagnostic outcomes without further fitting.

Reference: multifamily_improvement.py fixed twelve-property experiment. Bootstrap
intervals are conditional on the selected hypothesis; they do not account for
adaptive candidate selection or establish population representativeness.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .campaign import atomic_json, file_sha256


def report(experiment, cohort):
    """Join observed predictors/unknowns to paired errors and resample whole properties."""
    root = Path(experiment)
    complete = json.loads((root / "complete.json").read_text())
    if complete["status"] != "complete":
        raise ValueError("Diagnostic simulations incomplete")
    result = json.loads((root / "report.json").read_text())
    frame = pd.read_parquet(root / "per-building.parquet")
    rows = pd.read_parquet(cohort)
    selected = frame[frame.scenario == result["selected_scenario"]]
    if len(selected) != len(rows) or selected.property_key.nunique() != len(rows):
        raise ValueError("Expected the complete one-building-per-property panel")
    selected = selected.merge(
        rows[
            [
                "building_id",
                "geometry_proxy_area_error",
                "gross_to_atemp_ratio",
                "eave_height_m",
                "ridge_height_m",
                "height_status",
                "outline",
                "apartments",
                "source_row_sha256",
            ]
        ],
        on="building_id",
        validate="one_to_one",
    )
    selected["direction"] = np.where(
        selected.status != "accepted",
        "failed",
        np.where(selected.signed_error > 0, "over_predict", "under_predict"),
    )
    selected.drop(columns=["scalars", "diagnostics"], errors="ignore").to_csv(
        root / "building-evidence-errors.csv", index=False
    )
    rng = np.random.default_rng(71353)
    summaries = {}
    for scenario in ("baseline", result["selected_scenario"]):
        panel = frame[frame.scenario == scenario]
        indices = rng.integers(0, len(panel), size=(2000, len(panel)))
        accepted = (panel.status == "accepted").to_numpy()
        ape = panel.ape.to_numpy()
        absolute = panel.absolute_error.to_numpy()
        signed = panel.signed_error.to_numpy()
        counts = accepted[indices].sum(axis=1)

        def accepted_mean(values, accepted=accepted, indices=indices, counts=counts):
            return np.divide(
                np.where(accepted[indices], values[indices], 0).sum(axis=1),
                counts,
                out=np.full(len(counts), np.nan),
                where=counts > 0,
            )

        values = {
            "coverage20": ((ape[indices] < 20) & accepted[indices]).mean(axis=1),
            "coverage10": ((ape[indices] < 10) & accepted[indices]).mean(axis=1),
            "mae_kwh_m2": accepted_mean(absolute),
            "bias_kwh_m2": accepted_mean(signed),
        }
        summaries[scenario] = {
            k: np.nanquantile(v, [0.025, 0.975]).tolist() for k, v in values.items()
        }
    supplemental = {
        "schema_version": 1,
        "source_hashes": {
            str(p): file_sha256(p)
            for p in (
                root / "manifest.json",
                root / "selection.json",
                root / "per-building.parquet",
                Path(cohort),
            )
        },
        "conditional_property_bootstrap_95_percent": summaries,
        "bootstrap_resamples": 2000,
        "selection_uncertainty_included": False,
        "population_representativeness_established": False,
        "fresh_validation_available": False,
        "error_direction_counts": selected.direction.value_counts().to_dict(),
        "observed_ridge_and_eave_descriptors_used_to_fit_pitch": False,
        "roof_pitch_basis": "Legacy diagnostic 30-degree assumption; descriptors retained for future geometric review",
        "operating_flow_measured": False,
        "design_flow_is_operating_measurement": False,
        "required_next_information": [
            "Independent untouched property validation",
            "Measurement-year retrofit state",
            "Exposed facade/context",
            "Verified heated and roof geometry",
            "Operating ventilation",
            "Net delivery losses",
        ],
        "adoption": "No numeric catalogue update from this in-sample search",
    }
    atomic_json(root / "interpretation.json", supplemental)
    text = (root / "report.md").read_text()
    text += "\nConditional property bootstrap intervals are in interpretation.json. They exclude selection uncertainty and cannot certify population coverage. "
    text += "building-evidence-errors.csv joins each selected error to the available geometric descriptors and source lineage. "
    text += "Recorded design ventilation is not a measured operating rate; the shared-flow cases remain hypotheses. "
    text += "Roof pitch remains the explicitly assumed 30 degrees; archived ridge/eave descriptors require geometric review before use.\n"
    if "Conditional property bootstrap" not in (root / "report.md").read_text():
        (root / "report.md").write_text(text)
    return supplemental


def main(argv=None):
    """Add interpretation artifacts without changing model inputs or selection."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--cohort", type=Path, required=True)
    args = parser.parse_args(argv)
    report(args.experiment, args.cohort)


if __name__ == "__main__":
    main()
