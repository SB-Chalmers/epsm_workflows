"""Matrix-cell support accounting over the fixed national inventory.

References: stock preparation's EPC target eligibility and EPSM Data era map.
Existing candidate screens never define national calibration coverage implicitly.
"""

from pathlib import Path

import pandas as pd

from ..campaign import atomic_json, file_sha256
from .common import read


def inventory(study, output, campaigns=(), *, membership_audit=None):
    """Count all four uses/six eras; attach actual campaign outcomes separately."""
    from epsm_data.parametric import era_for_year

    study = Path(study)
    path = study / "features.parquet"
    frame = pd.read_parquet(path)
    membership_source = None
    if membership_audit is not None:
        membership_source = Path(membership_audit)
        audit = pd.read_parquet(
            membership_source, columns=["building_id", "formular_id", "eligible"]
        )
        if audit.duplicated(["building_id", "formular_id"]).any():
            raise ValueError("Ambiguous membership audit identity")
        frame = frame.drop(
            columns=["archetype_membership_eligible"], errors="ignore"
        ).merge(
            audit.rename(columns={"eligible": "archetype_membership_eligible"}),
            on=["building_id", "formular_id"],
            how="left",
            validate="one_to_one",
        )
        if frame.archetype_membership_eligible.isna().any():
            raise ValueError("Membership audit does not cover the inventory")
    frame = frame[
        frame.use_group.isin(["single_family", "multi_family", "office", "school"])
    ].copy()
    frame["matrix_era"] = frame.construction_year.map(
        lambda year: era_for_year(int(year)) if pd.notna(year) else "unknown"
    )
    evaluations = []
    for campaign in campaigns:
        p = Path(campaign) / "evaluation.json"
        if p.exists():
            evaluations.append(
                {"path": str(p.resolve()), "sha256": file_sha256(p), "results": read(p)}
            )
    cells = []
    for use in ("single_family", "multi_family", "office", "school"):
        for era in ("SE.01", "SE.02", "SE.03", "SE.04", "SE.05", "SE.06"):
            members = frame[(frame.use_group == use) & (frame.matrix_era == era)]
            evidence = {
                field: int(members[field].notna().sum())
                for field in (
                    "height_m",
                    "storeys",
                    "heated_basement_floors",
                    "designed_flow_l_s_m2",
                    "reno_wall",
                    "reno_window",
                )
            }
            results = [
                result
                for evaluation in evaluations
                for parent, result in evaluation["results"].items()
                if parent.split("__")[:2] == [use, era]
            ]
            cells.append(
                {
                    "use_group": use,
                    "era": era,
                    "buildings": len(members),
                    "membership_audited": "archetype_membership_eligible" in members,
                    "membership_eligible": int(
                        members.archetype_membership_eligible.sum()
                    )
                    if "archetype_membership_eligible" in members
                    else None,
                    "independent_properties": int(members.property_key.nunique()),
                    "ventilation": members.ventilation.value_counts().to_dict(),
                    "heating_families": members.heating_family.value_counts().to_dict(),
                    "observed_input_counts": evidence,
                    "campaign_results": results,
                    "status": "partially_evaluated"
                    if results
                    else "pending_physical_campaign",
                    "missing_information_is_not_an_exclusion": True,
                }
            )
    result = {
        "schema_version": 1,
        "source_sha256": file_sha256(path),
        "cells": cells,
        "campaigns": evaluations,
        "new_building_data": False,
        "unknown_era_buildings": int((frame.matrix_era == "unknown").sum()),
        "membership_audit": str(membership_source.resolve())
        if membership_source
        else None,
        "membership_sha256": file_sha256(membership_source)
        if membership_source
        else None,
    }
    atomic_json(output, result)
    return result
