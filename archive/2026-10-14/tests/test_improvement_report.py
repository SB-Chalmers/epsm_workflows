"""Conditional uncertainty preserves failures in building-coverage denominators."""

import json

import numpy as np
import pandas as pd

from epsm_workflows.improvement_report import report


def test_failed_property_stays_in_bootstrap_coverage(tmp_path):
    for name, value in [
        ("complete.json", {"status": "complete"}),
        ("report.json", {"selected_scenario": "baseline"}),
        ("manifest.json", {}),
        ("selection.json", {}),
    ]:
        (tmp_path / name).write_text(json.dumps(value))
    (tmp_path / "report.md").write_text("Diagnostic report\n")
    pd.DataFrame(
        [
            {
                "building_id": str(i),
                "property_key": str(i),
                "scenario": "baseline",
                "status": "accepted" if i == 0 else "failed",
                "signed_error": 1.0 if i == 0 else np.nan,
                "absolute_error": 1.0 if i == 0 else np.nan,
                "ape": 10.0 if i == 0 else np.nan,
            }
            for i in range(2)
        ]
    ).to_parquet(tmp_path / "per-building.parquet")
    cohort = tmp_path / "cohort.parquet"
    pd.DataFrame(
        [
            {
                "building_id": str(i),
                "geometry_proxy_area_error": 0.1,
                "gross_to_atemp_ratio": 1.1,
                "eave_height_m": 6,
                "ridge_height_m": 8,
                "height_status": "reported",
                "outline": "Fasad",
                "apartments": 4,
                "source_row_sha256": "fixture",
            }
            for i in range(2)
        ]
    ).to_parquet(cohort)
    result = report(tmp_path, cohort)
    assert result["conditional_property_bootstrap_95_percent"]["baseline"][
        "coverage20"
    ] == [0.0, 1.0]
    assert result["conditional_property_bootstrap_95_percent"]["baseline"][
        "mae_kwh_m2"
    ] == [1.0, 1.0]
    assert result["error_direction_counts"] == {"over_predict": 1, "failed": 1}
    assert not result["selection_uncertainty_included"]
    assert not result["population_representativeness_established"]
