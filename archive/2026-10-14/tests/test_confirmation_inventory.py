"""Opened stock observations cannot be relabelled into fresh confirmation data."""

import json

import pandas as pd

from epsm_workflows.confirmation_inventory import audit


def test_opened_study_and_missing_evidence_do_not_pass_by_counts(tmp_path):
    config = {
        "physics_min_buildings": 1,
        "physics_min_properties": {"train": 1, "validation": 1, "test": 1},
        "physics_evidence_approved": False,
        "physics_weather_manifest": None,
    }
    for name, value in [
        ("config.json", config),
        ("evaluation-opened.json", {}),
        ("prior-study-registry.json", {}),
    ]:
        (tmp_path / name).write_text(json.dumps(value))
    (tmp_path / "targets-test-sealed.parquet").write_text(
        "POISON: must never open targets"
    )
    pd.DataFrame(
        [
            {
                "building_id": s,
                "property_key": s,
                "split": s,
                "archetype_id": "mf_se02",
                "ventilation": "F",
                "climate_region": "SE.Zone3",
                "heating_family": "district_heat",
                "prior_study_property": False,
            }
            for s in ("train", "validation", "test")
        ]
    ).to_parquet(tmp_path / "simulation-candidates.parquet")
    report = audit(tmp_path)
    assert report["families"][0]["original_count_gates_passed"]
    assert report["study_outcomes_already_opened"]
    assert not report["prospective_confirmation_ready"]
    assert not report["target_files_read"]
    assert not report["reshuffle_creates_fresh_holdout"]
