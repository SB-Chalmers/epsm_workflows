"""Audit fixed national EPC/GIS membership without opening heating targets.

Reference: epsm_workflows.archetype_membership and docs/archetype-membership.md.
Conflicts indicate unresolved source consistency, not proof of a particular
replacement category. Preserve historical studies and write a separate audit.
"""

import argparse
import json
from collections import Counter
from pathlib import Path

import duckdb
import pandas as pd

from epsm_workflows import archetype_membership as membership_module
from epsm_workflows.archetype_membership import SHARES, assess_membership
from epsm_workflows.campaign import atomic_json, file_sha256


def audit(study, output):
    study, output = Path(study).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    config = json.loads((study / "config.json").read_text())
    feature_path = study / "features.parquet"
    frame = pd.read_parquet(feature_path)
    # Assignment evidence is whitelisted. Neither target parquet is opened.
    fields = [
        "building_id",
        "formular_id",
        "property_key",
        "use_group",
        "apartments",
        "building_position",
        "construction_year",
        "registry_year",
        "epc_year",
        "atemp_m2",
        "garage_percent",
        "ventilation",
        "source_file",
        "source_line",
        "source_row_sha256",
        *SHARES,
    ]
    frame = frame[[name for name in fields if name in frame]]
    publication = Path(config["publication"])
    gis_paths = sorted((publication / "building_results").glob("*/part.parquet"))
    link_path = publication / "catalogue/accepted_links.parquet"
    with duckdb.connect() as db:
        db.execute("SET threads=4")
        db.register("chosen", frame)
        source = db.execute(
            "SELECT c.*,b.andamal1 AS purpose,b.andamal2 AS purpose2,b.andamal3 AS purpose3,"
            "b.andamal4 AS purpose4,b.andamal5 AS purpose5 FROM chosen c "
            "LEFT JOIN read_parquet(?,union_by_name=true,hive_partitioning=false) b USING(building_id)",
            [list(map(str, gis_paths))],
        ).fetchdf()
        links = db.execute(
            "SELECT c.building_id,c.formular_id,l.epc_building_category,l.semantic_compatibility,"
            "l.certificate_to_footprint_count,l.evidence_tier FROM chosen c "
            "JOIN read_parquet(?) l USING(building_id,formular_id)",
            [str(link_path)],
        ).fetchdf()
    if (
        source.building_id.duplicated().any()
        or links.duplicated(["building_id", "formular_id"]).any()
    ):
        raise ValueError("Membership source identity is not unique")
    if len(links) != len(frame):
        raise ValueError("Membership accepted-link coverage is incomplete")
    joined = source.merge(
        links, on=["building_id", "formular_id"], validate="one_to_one"
    )
    records = [
        {
            "building_id": row["building_id"],
            "formular_id": row["formular_id"],
            "property_key": row["property_key"],
            "purpose": row["purpose"],
            **assess_membership(row),
        }
        for row in joined.to_dict("records")
    ]
    source.to_parquet(output / "national-membership-source.parquet", index=False)
    links.to_parquet(output / "national-link-source.parquet", index=False)
    pd.DataFrame(records).to_parquet(
        output / "national-membership-audit.parquet", index=False
    )
    summary = {}
    for use in sorted(frame.use_group.unique()):
        selected = [row for row in records if row["reported_use_group"] == use]
        summary[use] = {
            "source_buildings": len(selected),
            "pure_cohort_eligible": sum(row["eligible"] for row in selected),
            "quarantined": sum(not row["eligible"] for row in selected),
            "reasons": dict(
                Counter(reason for row in selected for reason in row["reasons"])
            ),
        }
    atomic_json(output / "national-summary.json", summary)
    suspects = source[
        (source.use_group == "multi_family")
        & (source.purpose == "Bostad;Småhus friliggande")
    ]
    suspects.sort_values("building_id").to_csv(
        output / "mf-labelled-detached-smallhouses.csv", index=False
    )
    atomic_json(
        output / "manifest.json",
        {
            "inputs": {
                str(path): file_sha256(path)
                for path in [feature_path, study / "config.json", link_path, *gis_paths]
            },
            "sources": {
                str(Path(__file__).resolve()): file_sha256(__file__),
                str(Path(membership_module.__file__).resolve()): file_sha256(
                    membership_module.__file__
                ),
            },
            "target_files_opened": False,
            "new_building_data": False,
            "source_buildings": len(frame),
            "counts_are_source_conflicts_not_confirmed_reclassifications": True,
        },
    )
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(audit(args.study, args.output))
