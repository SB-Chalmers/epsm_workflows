"""Reproduce national stock-completeness figure data without changing source data.

The national inventory is classified by its primary GIS purpose. The separate
EPC-selected inventory is classified by reported EPC use, with its existing
GIS-led membership audit attached. These denominators are never interchanged.

Run with the EPSM runtime, for example:
    /Users/ssanjay/GitHub/epsm_core/.venv/bin/python \
      examples/epc_validation/prepare_national_completeness.py
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

import duckdb
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
USES = ("multi_family", "single_family", "school", "office")
NATIONAL_USES = ("multi_family", "single_family", "school", "business_proxy")
PRIMARY_PURPOSES = {
    "multi_family": ["Bostad;Flerfamiljshus"],
    "single_family": [
        "Bostad;Småhus friliggande",
        "Bostad;Småhus kedjehus",
        "Bostad;Småhus radhus",
    ],
    "school": ["Samhällsfunktion;Skola"],
    "business_proxy": ["Verksamhet"],
}
PURPOSE_TO_USE = {p: use for use, purposes in PRIMARY_PURPOSES.items() for p in purposes}
COUNT_FIELDS = (
    "footprint_buildings", "accepted_epc_linked_buildings", "valid_footprint_buildings",
    "registry_matched_buildings", "registry_year_buildings", "registry_year_nonfuture_buildings",
)
FIELD_LABELS = {
    "construction_year_present": "Construction year (registry or EPC)",
    "registry_year_present": "Registry construction year",
    "epc_year_present": "EPC construction year",
    "footprint_area_m2_present": "Footprint area",
    "height_m_present": "Usable LiDAR height",
    "eave_height_m_present": "Eave height",
    "ridge_height_m_present": "Ridge height",
    "storeys_present": "Above-ground storeys",
    "atemp_m2_present": "Reported Atemp",
    "apartments_present": "Dwelling count",
    "heated_basement_floors_present": "Heated basement storeys",
    "designed_flow_l_s_m2_present": "Reported design ventilation flow",
    "reno_wall_present": "Dated wall-renovation answer",
    "reno_window_present": "Dated window-renovation answer",
    "reno_roof_present": "Dated roof-renovation answer",
    "reno_ventilation_present": "Dated ventilation-renovation answer",
    "completed_renovation_year_present": "Reported completed-renovation year",
    "ventilation_category_present": "Known ventilation category/categories",
    "ventilation_single_known": "Single known ventilation category",
    "ventilation_single_native_supported": "Single native-supported ventilation category",
    "epc_heating_carriers_present": "EPC heating-energy carrier combination",
    "positive_designed_flow": "Positive reported design ventilation flow",
    "zero_designed_flow": "Zero reported design ventilation flow",
    "valid_footprint": "Valid footprint geometry",
    "roof_form_classified": "Flat or pitched-ridge roof classified",
    "year_height_storeys_atemp_present": "Year + height + storeys + Atemp jointly present",
    "both_years_present": "Both registry and EPC years present",
    "year_sources_agree": "Registry and EPC years identical",
    "year_sources_disagree": "Registry and EPC years differ",
    "year_sources_cross_eras": "Registry and EPC years cross archetype eras",
    "membership_eligible": "Eligible under pure-programme GIS-led membership audit",
}

EPC_SQL = """
SELECT municipality_code, andamal1 AS primary_gis_purpose,
       count(*) AS footprint_buildings,
       count(*) FILTER (WHERE distinct_certificate_count > 0) AS accepted_epc_linked_buildings,
       count(*) FILTER (WHERE geometry_valid AND building_area_m2 > 0) AS valid_footprint_buildings
FROM read_parquet(?, union_by_name=true, hive_partitioning=false)
GROUP BY municipality_code, andamal1
"""

AGE_SQL = """
SELECT f.municipality_code, rtrim(f.detailed_purpose, ';') AS primary_gis_purpose,
       count(*) AS age_inventory_footprint_buildings,
       count(r.building_id) AS registry_matched_buildings,
       count(r.construction_year) AS registry_year_buildings,
       count(CASE WHEN r.construction_year BETWEEN 1 AND ? THEN 1 END)
         AS registry_year_nonfuture_buildings
FROM canonical_footprints AS f
LEFT JOIN registry_buildings AS r USING (building_id)
WHERE f.detailed_purpose IN (?, ?, ?, ?, ?, ?)
GROUP BY f.municipality_code, f.detailed_purpose
"""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def standardize_codes(frame: pd.DataFrame, column: str) -> pd.DataFrame:
    frame[column] = frame[column].astype(str).str.zfill(4)
    return frame


def add_national_fractions(frame: pd.DataFrame) -> pd.DataFrame:
    for field in COUNT_FIELDS[1:]:
        frame[field.removesuffix("_buildings") + "_fraction"] = (
            frame[field] / frame.footprint_buildings.where(frame.footprint_buildings > 0)
        )
    return frame


def national_inventory(release: Path, age_registry: Path, year: int):
    partitions = sorted((release / "building_results").glob("*/part.parquet"))
    assert len(partitions) == 290, f"Expected 290 footprint partitions, got {len(partitions)}"
    with duckdb.connect() as connection:
        connection.execute("SET threads=4")
        source = connection.execute(EPC_SQL, [[str(p) for p in partitions]]).fetchdf()
    source = standardize_codes(source, "municipality_code")
    source["primary_gis_purpose_raw"] = source.primary_gis_purpose
    source["primary_gis_purpose"] = source.primary_gis_purpose.str.rstrip(";")
    assert source.municipality_code.nunique() == 290
    source["use_group"] = source.primary_gis_purpose.map(PURPOSE_TO_USE)
    target = source[source.use_group.notna()].copy()
    groups = ["municipality_code", "use_group"]
    target = target.groupby(groups, as_index=False)[list(COUNT_FIELDS[:3])].sum()
    with sqlite3.connect(f"file:{age_registry}?mode=ro", uri=True) as connection:
        # Keep the read-only primary-key lookup workload from thrashing SQLite's
        # default 2 MiB page cache. This setting does not change the database.
        connection.execute("PRAGMA cache_size=-262144")
        categories = [p if p != "Verksamhet" else "Verksamhet;" for p in PURPOSE_TO_USE]
        print("Footprint aggregates complete; joining canonical building IDs to registry years", flush=True)
        age = pd.read_sql_query(AGE_SQL, connection, params=[year, *categories])
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
    age = standardize_codes(age, "municipality_code")
    age["use_group"] = age.primary_gis_purpose.map(PURPOSE_TO_USE)
    assert age.use_group.notna().all()
    age_fields = ["age_inventory_footprint_buildings", *COUNT_FIELDS[3:]]
    age = age.groupby(groups, as_index=False)[age_fields].sum()
    combined = target.merge(age, on=groups, how="outer", validate="one_to_one")
    assert combined.footprint_buildings.notna().all()
    assert combined.age_inventory_footprint_buildings.notna().all()
    assert (combined.footprint_buildings == combined.age_inventory_footprint_buildings).all(), (
        "Age registry canonical footprints differ from EPC canonical footprint inventory"
    )
    combined = combined.drop(columns="age_inventory_footprint_buildings")
    index = pd.MultiIndex.from_product(
        [sorted(source.municipality_code.unique()), NATIONAL_USES], names=groups
    )
    combined = combined.set_index(groups).reindex(index, fill_value=0).reset_index()
    combined[list(COUNT_FIELDS)] = combined[list(COUNT_FIELDS)].astype("int64")
    combined["county_code"] = combined.municipality_code.str[:2]
    combined["classification_basis"] = "primary_gis_purpose"
    combined["office_inventory"] = False
    for field in COUNT_FIELDS[1:]:
        assert combined[field].le(combined.footprint_buildings).all()
    national = combined.groupby("use_group", as_index=False)[list(COUNT_FIELDS)].sum()
    return add_national_fractions(combined), add_national_fractions(national), source, partitions, metadata


def selected_epc(stock: Path, membership_path: Path):
    fields = [
        "construction_year", "registry_year", "epc_year", "footprint_area_m2", "height_m",
        "eave_height_m", "ridge_height_m", "storeys", "atemp_m2", "apartments",
        "heated_basement_floors", "designed_flow_l_s_m2", "reno_wall", "reno_window",
        "reno_roof", "reno_ventilation", "completed_renovation_year",
    ]
    columns = [
        "building_id", "formular_id", "property_key", "municipality", "use_group", "ventilation",
        "heating_family", "geometry_status", "roof_form", "measurement_year", *fields,
    ]
    frame = pd.read_parquet(stock / "features.parquet", columns=columns)
    assert len(frame) == 262280 and frame.building_id.is_unique
    audit = pd.read_parquet(membership_path, columns=["building_id", "formular_id", "eligible", "reasons"])
    frame = frame.merge(audit, on=["building_id", "formular_id"], validate="one_to_one")
    assert frame.eligible.notna().all()
    frame = frame[frame.use_group.isin(USES)].copy()
    frame = standardize_codes(frame.rename(columns={"municipality": "municipality_code"}), "municipality_code")
    for field in fields:
        frame[field + "_present"] = frame[field].notna()
    frame["ventilation_category_present"] = frame.ventilation.ne("unknown")
    frame["ventilation_single_known"] = frame.ventilation.isin(["S", "F", "FT", "FTX", "Fmed"])
    frame["ventilation_single_native_supported"] = frame.ventilation.isin(["S", "F", "FT", "FTX"])
    frame["epc_heating_carriers_present"] = frame.heating_family.ne("unknown")
    frame["positive_designed_flow"] = frame.designed_flow_l_s_m2.gt(0)
    frame["zero_designed_flow"] = frame.designed_flow_l_s_m2.eq(0)
    frame["valid_footprint"] = frame.geometry_status.eq("valid")
    frame["roof_form_classified"] = frame.roof_form.isin(["flat", "pitched_ridge"])
    frame["year_height_storeys_atemp_present"] = frame[
        ["construction_year", "height_m", "storeys", "atemp_m2"]
    ].notna().all(axis=1)
    frame["both_years_present"] = frame.registry_year.notna() & frame.epc_year.notna()
    frame["year_sources_agree"] = frame.both_years_present & frame.registry_year.eq(frame.epc_year)
    frame["year_sources_disagree"] = frame.both_years_present & frame.registry_year.ne(frame.epc_year)
    frame["year_sources_cross_eras"] = frame.reasons.map(
        lambda reasons: "construction_year_sources_cross_archetype_eras" in reasons
    )
    frame["membership_eligible"] = frame.eligible
    metrics = [f + "_present" for f in fields] + [
        "ventilation_category_present", "ventilation_single_known", "ventilation_single_native_supported",
        "epc_heating_carriers_present", "positive_designed_flow", "zero_designed_flow", "valid_footprint",
        "roof_form_classified", "year_height_storeys_atemp_present", "both_years_present",
        "year_sources_agree", "year_sources_disagree", "year_sources_cross_eras", "membership_eligible",
    ]

    def aggregate(keys):
        grouped = frame.groupby(keys, sort=True, observed=True)
        result = grouped.agg(
            epc_selected_buildings=("building_id", "size"), independent_properties=("property_key", "nunique"),
            measurement_year_min=("measurement_year", "min"), measurement_year_max=("measurement_year", "max"),
        )
        counts = grouped[metrics].sum().astype("int64").rename(columns=lambda c: c + "_count")
        result = result.join(counts).reset_index()
        for metric in metrics:
            result[metric + "_fraction"] = result[metric + "_count"] / result.epc_selected_buildings
        aliases = {
            "total": "epc_selected_buildings", "count_known_year": "construction_year_present_count",
            "count_year_both_sources": "both_years_present_count", "count_year_same": "year_sources_agree_count",
            "count_year_conflict": "year_sources_disagree_count", "count_year_cross_era_conflict": "year_sources_cross_eras_count",
            "membership_eligible": "membership_eligible_count",
        }
        for alias, source in aliases.items():
            result[alias] = result[source]
        result["classification_basis"] = "reported_epc_use"
        return result

    municipal = aggregate(["municipality_code", "use_group"])
    national = aggregate(["use_group"])
    long_rows = []
    for row in national.to_dict("records"):
        for metric in metrics:
            long_rows.append({
                "use_group": row["use_group"], "metric": metric, "display_name": FIELD_LABELS[metric],
                "count": row[metric + "_count"], "denominator": row["epc_selected_buildings"],
                "fraction": row[metric + "_fraction"], "denominator_basis": "epc_selected_reported_use",
            })
    return municipal, national, pd.DataFrame(long_rows)


def height_tables(publication: Path, national: pd.DataFrame):
    table_dir = publication / "supplement" / "tables"
    municipal = pd.read_csv(table_dir / "summary_by_municipality.csv", dtype={"kommunkod": str, "county_code": str})
    broad = pd.read_csv(table_dir / "summary_by_municipality_building_type.csv", dtype={"kommunkod": str, "county_code": str})
    for frame in (municipal, broad):
        frame.rename(columns={"kommunkod": "municipality_code", "kommun": "municipality_name"}, inplace=True)
        standardize_codes(frame, "municipality_code")
        frame["county_code"] = frame.county_code.str.zfill(2)
        frame["denominator_basis"] = "included_buildings_in_fully_completed_municipalities"
        frame["external_accuracy_validated"] = False
        frame["determined_fraction"] = frame.determined_count / frame.building_count
        frame["warning_fraction_of_determined"] = frame.warning_count / frame.determined_count.where(frame.determined_count > 0)
    broad["classification_basis"] = "broad_primary_gis_type"
    detail = pd.read_csv(table_dir / "summary_by_detailed_purpose.csv")
    detail["primary_gis_purpose"] = detail.detailed_purpose.str.replace(" — ", ";", regex=False)
    detail["use_group"] = detail.primary_gis_purpose.map(PURPOSE_TO_USE)
    # The publication's broad Verksamhet label has no detail suffix.
    detail.loc[detail.building_type.eq("Verksamhet"), "use_group"] = "business_proxy"
    targets = detail[detail.use_group.notna()].groupby("use_group", as_index=False)[
        ["building_count", "determined_count", "warning_count", "undetermined_count"]
    ].sum()
    targets = targets.rename(columns={"building_count": "included_buildings", "determined_count": "determined_buildings", "warning_count": "warning_buildings", "undetermined_count": "undetermined_buildings"})
    targets = targets.merge(national[["use_group", "footprint_buildings"]], on="use_group", validate="one_to_one")
    targets["outside_completed_scope_buildings"] = targets.footprint_buildings - targets.included_buildings
    targets["included_fraction_national_inventory"] = targets.included_buildings / targets.footprint_buildings
    targets["determined_fraction_included_scope"] = targets.determined_buildings / targets.included_buildings
    targets["warning_fraction_of_determined"] = targets.warning_buildings / targets.determined_buildings
    for name, source in {"building_count": "included_buildings", "determined_count": "determined_buildings", "warning_count": "warning_buildings", "undetermined_count": "undetermined_buildings"}.items():
        targets[name] = targets[source]
    targets["denominator_basis"] = "included_buildings_in_fully_completed_municipalities"
    targets["classification_basis"] = "primary_gis_purpose"
    assert targets.outside_completed_scope_buildings.ge(0).all()
    assert targets.included_buildings.eq(targets.determined_buildings + targets.undetermined_buildings).all()
    provenance = json.loads((publication / "provenance.json").read_text())
    assert municipal.municipality_code.nunique() == provenance["scope"]["completed_municipality_count"]
    assert int(municipal.building_count.sum()) == provenance["scope"]["included_buildings"]
    assert int(municipal.determined_count.sum()) == provenance["scope"]["determined_buildings"]
    return municipal, broad, targets, provenance


def assert_prior(national: pd.DataFrame, selected: pd.DataFrame, height: pd.DataFrame, prior_path: Path):
    prior = json.loads(prior_path.read_text())
    for row in national.to_dict("records"):
        reference = prior["national"][row["use_group"]]
        for column, expected in (
            ("footprint_buildings", reference["footprint_buildings"]),
            ("accepted_epc_linked_buildings", reference["epc_link"]["count"]),
            ("valid_footprint_buildings", reference["valid_footprint"]["count"]),
            ("registry_year_buildings", reference["registry_year"]["count"]),
        ):
            assert row[column] == expected, (row["use_group"], column, row[column], expected)
    for row in selected.to_dict("records"):
        reference = prior["epc_selected"][row["use_group"]]
        assert row["epc_selected_buildings"] == reference["buildings"]
        for metric in reference["fields"]:
            rename = {
                "single_supported_ventilation": "ventilation_single_native_supported",
                "positive_designed_flow_l_s_m2": "positive_designed_flow",
                "year_height_storeys_atemp_present": "year_height_storeys_atemp_present",
            }
            column = rename.get(metric, metric + "_present") + "_count"
            assert row[column] == reference["fields"][metric]["count"], (row["use_group"], metric)
    for row in height.to_dict("records"):
        reference = prior["national"][row["use_group"]]["height_completed_municipality_scope"]
        assert row["included_buildings"] == reference["included"]
        assert row["determined_buildings"] == reference["determined"]["count"]
        assert row["warning_buildings"] == reference["warning_among_determined"]["count"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock", type=Path, default=ROOT / "output/stock-national-2026-10-02-v2")
    parser.add_argument("--membership", type=Path, default=ROOT / "output/archetype-membership-gis-led-2026-10-04-v2/national-membership-audit.parquet")
    parser.add_argument("--height-publication", type=Path, default=Path("/Users/ssanjay/GitHub/footprint_height_extraction/outputs/publication"))
    parser.add_argument("--prior-audit", type=Path, default=ROOT / "output/data-completeness-mf-sf-school-office-2026-10-05/audit.json")
    parser.add_argument("--output", type=Path, default=ROOT / "output/national-completeness-paper-2026-10-05/data")
    parser.add_argument("--reference-year", type=int, default=2026)
    args = parser.parse_args()
    config = json.loads((args.stock / "config.json").read_text())
    release = Path(config["publication"])
    registry = Path(config["age_registry"])
    args.output.mkdir(parents=True, exist_ok=True)
    print("Aggregating national footprint, EPC linkage and registry year data", flush=True)
    municipal, national, purpose, partitions, registry_metadata = national_inventory(release, registry, args.reference_year)
    print("Aggregating selected EPC fields and semantic membership", flush=True)
    selected_municipal, selected_national, selected_fields = selected_epc(args.stock, args.membership)
    height_municipal, height_broad, height_targets, height_provenance = height_tables(args.height_publication, national)
    assert_prior(national, selected_national, height_targets, args.prior_audit)
    all_municipalities = pd.DataFrame({"municipality_code": sorted(municipal.municipality_code.unique())})
    names = height_municipal[["municipality_code", "municipality_name", "county_code", "county"]]
    all_municipalities = all_municipalities.merge(names, how="left", on="municipality_code", validate="one_to_one")
    all_municipalities["county_code"] = all_municipalities.municipality_code.str[:2]
    all_municipalities["height_publication_completed_scope"] = all_municipalities.municipality_name.notna()
    csvs = {
        "national_municipality_use.csv": municipal,
        "national_use_totals.csv": national,
        "national_municipality_primary_purpose.csv": purpose,
        "epc_selected_municipality_use.csv": selected_municipal,
        "epc_selected_use_totals.csv": selected_national,
        "epc_selected_field_completeness.csv": selected_fields,
        "height_municipality.csv": height_municipal,
        "height_municipality_broad_type.csv": height_broad,
        "height_national_target_use.csv": height_targets,
        "municipalities.csv": all_municipalities,
    }
    for filename, frame in csvs.items():
        frame.to_csv(args.output / filename, index=False, float_format="%.10g")
    sources = [
        args.stock / "config.json", args.stock / "features.parquet", args.membership, args.prior_audit,
        release / "manifest.json", registry,
        args.height_publication / "provenance.json",
        args.height_publication / "supplement/tables/summary_by_municipality.csv",
        args.height_publication / "supplement/tables/summary_by_municipality_building_type.csv",
        args.height_publication / "supplement/tables/summary_by_detailed_purpose.csv",
        args.height_publication.parents[1] / "src/building_heights/pipeline.py",
        args.height_publication.parents[1] / "scripts/analyze_height_quality.py",
        *partitions,
    ]
    print(f"Recording SHA-256 hashes for {len(sources)} source files", flush=True)
    with ThreadPoolExecutor(max_workers=3) as pool:
        source_hashes = list(pool.map(lambda p: {"path": str(p.resolve()), "size_bytes": p.stat().st_size, "sha256": sha256(p)}, sources))
    rules = [
        "National rows use primary GIS purpose and count physical canonical footprint building IDs; EPC-selected rows use reported EPC programme and the existing selection rules.",
        "Single family includes detached, chained and terraced small houses; ambiguous small houses with several dwellings are excluded.",
        "Trailing empty-detail semicolons are removed from primary GIS purpose keys, e.g. Verksamhet; maps to the broad Verksamhet business proxy.",
        "Verksamhet is a broad business proxy, not a national office inventory. National office coverage is unknown.",
        "Accepted EPC linkage counts buildings with at least one accepted certificate; it does not establish unambiguous building-level measurement scope or EPC freshness.",
        "Registry year presence and registry year between 1 and the reference year are separate metrics.",
        "Height municipality and broad-type rows refer only to fully completed municipalities. Municipal MF/SF/school height rates cannot be derived from these broad-type aggregates.",
        "Height target-use national totals use detailed primary purpose within the completed-municipality scope. Determined height and extraction warnings express internal extraction evidence, not external accuracy.",
        "Height warning_count counts height_status='determined' AND quality_status='warning'; pipeline quality_status becomes warning when negative height points were excluded or representative height exceeds the configured unusual-height threshold. It is not a generic confidence class. Other diagnostic flags can occur without warning status, and warning mechanisms overlap.",
        "Field presence is not semantic validity, native assembly readiness or prediction accuracy.",
        "Ventilation single known includes Fmed; single native supported excludes Fmed and multiple categories.",
        "EPC heating carriers derive from positive EPC energy fields and do not establish installed HVAC capacities, efficiencies or distribution.",
        "Renovation presence counts explicit yes/no completed-component answers dated no later than measurement year; missing is unknown, not evidence of no renovation.",
        "Year-source disagreements are nonexclusive with membership exclusions; cross-era differences follow the existing membership audit.",
        "Municipality and type classifications match exactly between the age canonical-footprint inventory and EPC footprint inventory; all national totals are asserted against the preceding audit.",
    ]
    manifest = {
        "schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "reference_year": args.reference_year, "generator": str(Path(__file__).resolve()),
        "generator_sha256": sha256(Path(__file__)), "source_hashes": source_hashes,
        "queries": {"epc_inventory": EPC_SQL, "registry": AGE_SQL},
        "registry_query_parameters": [args.reference_year, *[p if p != "Verksamhet" else "Verksamhet;" for p in PURPOSE_TO_USE]],
        "primary_gis_categories": PRIMARY_PURPOSES, "height_provenance": height_provenance,
        "registry_metadata": {k: json.loads(v) for k, v in registry_metadata.items() if k.endswith("manifest")},
        "rules": rules,
        "outputs": {name: {"rows": len(frame), "columns": frame.columns.tolist(), "sha256": sha256(args.output / name)} for name, frame in csvs.items()},
        "validation": {"matched_prior_national_totals": True, "matched_prior_epc_selected_totals": True, "matched_prior_height_target_totals": True, "municipalities": 290},
    }
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"output": str(args.output), "rows": {name: len(frame) for name, frame in csvs.items()}}, indent=2), flush=True)


if __name__ == "__main__":
    main()
