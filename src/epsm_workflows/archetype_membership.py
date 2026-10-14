"""Energy-blind agreement checks before comparing observed archetype cohorts.

Lantmäteriet's Handbok Byggnad defines Flerfamiljshus as at least three
dwellings and separates use from detached/attached placement. Its explicit
"Småhus med flera lägenheter" grey zone is not silently mapped to MF:
https://www.lantmateriet.se/globalassets/om-lantmateriet/var-samverkan-med-andra/byggnad-adress-lagenhet/handbok_byggnad.pdf
EPC use shares describe the heated programme, not just the main cadastral use:
https://www.boverket.se/sv/energideklaration/energideklaration/energideklarationens-innehall
Pure-programme eligibility below is a conservative study policy, not a legal
definition of building type. Conflicts remain in inventory and are quarantined
from pure archetype calibration; annual energy is never consulted.
"""

from __future__ import annotations

import codecs
import csv
import hashlib
import json
import math
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

POLICY = "gis-led-epc-validated-pure-programme-v2"
SHARES = {
    "residential_percent": "EgenAtempBostad",
    "office_percent": "EgenAtempKontor",
    "school_percent": "EgenAtempSkolor",
    "hotel_percent": "EgenAtempHotell",
    "restaurant_percent": "EgenAtempRestaurang",
    "grocery_percent": "EgenAtempLivsmedel",
    "retail_percent": "EgenAtempButik",
    "shopping_percent": "EgenAtempKopcentrum",
    "healthcare_percent": "EgenAtempVard",
    "daytime_service_percent": "EgenAtempVardDag",
    "bath_percent": "EgenAtempBad",
    "theatre_percent": "EgenAtempTeater",
    "other_use_percent": "EgenAtempOvrig",
}


def normalized(value):
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    return re.sub(r"[^a-z0-9]", "", text)


def numeric(value):
    try:
        result = float(str(value).replace(",", "."))
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def residential_category(value):
    """Read the EPC category; 'Friliggande' never establishes a use category."""
    key = normalized(value)
    if key == "flerbostadshus":
        return "multi_family"
    if key in {
        "enochtvabostadshus",
        "enellertvabostadshus",
        "enbostadshus",
        "tvabostadshus",
        "smahus",
    }:
        return "single_family"
    return None


def gis_use(value):
    """Interpret a purpose field without treating broad business as an office."""
    text = normalized(value)
    if not text or text in {"none", "nan", "unknown"}:
        return "unknown"
    if "smahusmedfleralagenheter" in text:
        return "ambiguous_small_multi"
    if "flerfamiljshus" in text or "flerbostadshus" in text:
        return "multi_family"
    if "smahus" in text:
        return "single_family"
    if "komplementbyggnad" in text:
        return "ancillary"
    if "universitet" in text or "hogskola" in text:
        return "higher_education"
    if "skola" in text:
        return "school"
    if "kontor" in text or "kommunhus" in text:
        return "office"
    if "bostad" in text:
        return "housing_unspecified"
    if "verksamhet" in text or text == "samhallsfunktionospecificerad":
        return "nonresidential_unspecified"
    return "other_nonresidential"


def assess_membership(row):
    """Return source-only reasons; never reclassify from size or heating error."""
    use = row.get("use_group", "unknown")
    assignment = gis_assignment(row)
    reasons, caveats = [], []
    if assignment["use_group"] and assignment["use_group"] != use:
        reasons.append("gis_assignment_disagrees_with_reported_use")
    if use in {"single_family", "multi_family"} and not assignment["use_group"]:
        reasons.append("residential_gis_type_unresolved")

    category = (
        row.get("epc_building_category")
        or row.get("epc_category")
        or row.get("EgenByggnadsKat")
    )
    epc = residential_category(category)
    if use not in {"single_family", "multi_family", "office", "school"}:
        reasons.append("outside_pure_matrix_programmes")
    if use in {"single_family", "multi_family"} and epc != use:
        reasons.append("epc_category_missing_or_disagrees")
    if use in {"office", "school"} and epc is not None:
        reasons.append("residential_epc_category_for_nonresidential_programme")
    linked = row.get("linked_epc_category")
    if linked and residential_category(linked) != epc:
        reasons.append("raw_and_linked_epc_category_disagree")

    primary = (
        "residential_percent"
        if use in {"single_family", "multi_family"}
        else f"{use}_percent"
    )
    shares = {
        key: numeric(row.get(key, row.get(source))) for key, source in SHARES.items()
    }
    if any(value is not None and not 0 <= value <= 100 for value in shares.values()):
        reasons.append("invalid_use_share")
    if shares.get(primary) is None:
        reasons.append("primary_use_share_unknown")
    elif shares[primary] != 100:
        reasons.append("mixed_programme_requires_separate_native_model")
    if any(
        value is not None and value > 0
        for key, value in shares.items()
        if key != primary
    ):
        reasons.append("other_programme_share_positive")
    if (numeric(row.get("garage_percent")) or 0) > 0:
        reasons.append("heated_garage_requires_separate_native_programme")

    apartments = numeric(row.get("apartments", row.get("EgenAntalBolgh")))
    if apartments is not None:
        if apartments < 0 or not apartments.is_integer():
            reasons.append("invalid_dwelling_count")
        elif use == "multi_family" and apartments < 3:
            reasons.append("multifamily_with_fewer_than_three_dwellings")
        elif use == "single_family" and apartments not in {1, 2}:
            reasons.append("single_family_dwelling_count_conflict")
    else:
        caveats.append("dwelling_count_unreported")

    purposes = [row.get("purpose") or row.get("andamal1")]
    purposes += [row.get(f"purpose{i}") or row.get(f"andamal{i}") for i in range(2, 6)]
    classes = sorted({gis_use(p) for p in purposes} - {"unknown"})
    if "ambiguous_small_multi" in classes:
        reasons.append("gis_small_multi_definition_ambiguous")
    compatible = {use}
    if use in {"single_family", "multi_family"}:
        compatible.add("housing_unspecified")
    else:
        compatible.add("nonresidential_unspecified")
    if set(classes) - compatible:
        reasons.append("gis_purpose_disagrees_with_pure_programme")
    if not classes:
        caveats.append("gis_purpose_unavailable")
    if row.get("semantic_compatibility") == "incompatible":
        reasons.append("link_semantic_incompatibility")
    count = numeric(row.get("certificate_to_footprint_count"))
    if count is not None and count != 1:
        reasons.append("certificate_does_not_identify_one_footprint")
    if (
        row.get("observed_ventilation")
        and row.get("ventilation") != row["observed_ventilation"]
    ):
        reasons.append("assigned_ventilation_disagrees_with_source")

    from epsm_data.parametric import era_for_year

    years = {
        key: numeric(row.get(key))
        for key in ("construction_year", "epc_year", "registry_year")
    }
    eras = {
        era_for_year(int(value))
        for value in years.values()
        if value is not None and value > 0
    }
    if len(eras) > 1:
        reasons.append("construction_year_sources_cross_archetype_eras")
    assigned = row.get("era")
    if assigned and str(assigned).startswith("SE.") and eras and assigned not in eras:
        reasons.append("assigned_era_disagrees_with_source_year")
    return {
        "policy": POLICY,
        "eligible": not reasons,
        "status": "quarantined"
        if reasons
        else "consistent_with_caveats"
        if caveats
        else "consistent",
        "reasons": sorted(set(reasons)),
        "caveats": caveats,
        "reported_use_group": use,
        "epc_category": category,
        "gis_classes": classes,
        "gis_assignment": assignment,
        "assigned_archetype_use": assignment["use_group"]
        or (use if use in {"office", "school"} and not reasons else None),
        "primary_use_share": shares.get(primary),
        "apartments": apartments,
        "energy_used": False,
    }


def source_operation_evidence(raw, measurement_year):
    """Retain declared design flow and dated completed measures without inventing U.

    References: reviewed Boverket EPC form definitions in
    archetype_diagnostics.FORM_EVIDENCE; stock.prepare.parse_record. Design flow
    is not a measured operating flow. AtgUtforda fields denote reported completed
    measures; blank dates and Nej do not establish an unrenovated envelope.
    A completed window replacement provides no independently measured U-value.
    """
    flow = numeric(raw.get("EgenProjVentFlode"))
    year = numeric(raw.get("AtgUtfordaUtfortAr"))
    if year is not None and (not year.is_integer() or not 1 <= year <= 9999):
        year = None
    measured = numeric(measurement_year)
    timing = (
        "during_or_before"
        if measured is not None and year is not None and year <= measured
        else "after_measurement"
        if measured is not None and year is not None
        else "unknown"
    )
    window = raw.get("AtgUtfordaByteFonster")
    return {
        "designed_flow_l_s_m2": flow if flow is not None and 0 <= flow <= 10 else None,
        "design_flow_evidence_status": "reported_design_not_operating"
        if flow is not None and 0 <= flow <= 10
        else "unknown_or_invalid",
        "completed_renovation_year": int(year) if year is not None else None,
        "renovation_timing": timing,
        "completed_window_replacement": "reported_before_or_during"
        if window == "Ja" and timing == "during_or_before"
        else "reported_after_measurement"
        if window == "Ja" and timing == "after_measurement"
        else "undated_report"
        if window == "Ja"
        else "unknown",
        "reported_completed_measures": {
            k: v for k, v in raw.items() if k.startswith("AtgUtforda") and v
        },
    }


def hydrate_source_membership(rows):
    """Recover omitted use fields from the existing, hash-verified EPC source rows.

    Historical cohort exports omitted dwelling counts and use shares. Read only
    their original field locators; never choose a certificate using energy. A
    changed hash, field value or formular ID is a hard provenance failure.
    """
    from .campaign import file_sha256

    hydrated = [dict(row) for row in rows]
    lookup = defaultdict(lambda: defaultdict(list))
    for row in hydrated:
        if row.get("source_file") and row.get("source_line"):
            lookup[str(Path(row["source_file"]).resolve())][
                int(row["source_line"])
            ].append(row)
    sources = {}
    for name, wanted in lookup.items():
        path = Path(name)
        sources[name] = file_sha256(path)
        with path.open("rb") as stream:
            encoding = (
                "utf-16"
                if stream.read(4).startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE))
                else "latin-1"
            )
        found = set()
        with path.open(encoding=encoding, newline="") as stream:
            for line, raw in enumerate(csv.DictReader(stream, delimiter="|"), 2):
                if line not in wanted:
                    continue
                found.add(line)
                signature = hashlib.sha256(
                    json.dumps(raw, sort_keys=True).encode()
                ).hexdigest()
                for row in wanted[line]:
                    if int(raw["FormularId"]) != int(row["formular_id"]):
                        raise ValueError("EPC membership source formular ID changed")
                    if (
                        row.get("source_row_sha256")
                        and row["source_row_sha256"] != signature
                    ):
                        raise ValueError("EPC membership source row checksum changed")
                    fields = {
                        key: numeric(raw.get(source)) for key, source in SHARES.items()
                    }
                    fields.update(
                        apartments=numeric(raw.get("EgenAntalBolgh")),
                        epc_building_category=raw.get("EgenByggnadsKat"),
                        building_position=raw.get("EgenByggnadsTyp"),
                    )
                    fields.update(
                        source_operation_evidence(raw, row.get("measurement_year"))
                    )
                    vent_fields = {
                        "VentTypSjalvdrag": "S",
                        "VentTypF": "F",
                        "VentTypFT": "FT",
                        "VentTypFTX": "FTX",
                        "VentTypFmed": "Fmed",
                    }
                    systems = [
                        value
                        for key, value in vent_fields.items()
                        if raw.get(key) == "Ja"
                    ]
                    if systems:
                        fields["observed_ventilation"] = (
                            systems[0] if len(systems) == 1 else "multiple"
                        )
                    year = numeric(raw.get("EgenNybyggAr"))
                    if year is not None and year > 0:
                        fields["epc_year"] = year
                    area, garage = (
                        numeric(raw.get("EgenAtemp")),
                        numeric(raw.get("EgenAvarmgarage")),
                    )
                    if area and garage is not None:
                        fields["garage_percent"] = 100 * garage / area
                    for key, value in fields.items():
                        current = row.get(key)
                        if (
                            current is not None
                            and not (isinstance(current, float) and math.isnan(current))
                            and value is not None
                            and current != value
                        ):
                            raise ValueError(
                                f"EPC membership field disagrees with source: {key}"
                            )
                        if value is not None:
                            row[key] = value
                    row["membership_source_row_sha256"] = signature
        if found != set(wanted):
            raise ValueError("EPC membership source row missing")
    return hydrated, sources


def hydrate_link_membership(rows, publication):
    """Check existing exact EPC/footprint links and all GIS purpose fields."""
    import duckdb
    import pandas as pd

    from .campaign import file_sha256

    publication = Path(publication).resolve()
    link_path = publication / "catalogue/accepted_links.parquet"
    ids = pd.DataFrame(
        [
            {"building_id": row["building_id"], "formular_id": row["formular_id"]}
            for row in rows
        ]
    )
    municipalities = {str(row["municipality"]) for row in rows}
    paths = [
        publication / f"building_results/kommunkod={m}/part.parquet"
        for m in sorted(municipalities)
    ]
    if any(not path.is_file() for path in paths):
        raise ValueError("Membership GIS municipality partition missing")
    with duckdb.connect() as db:
        db.register("chosen", ids)
        links = db.execute(
            "SELECT l.* FROM read_parquet(?) l JOIN chosen c USING(building_id,formular_id)",
            [str(link_path)],
        ).fetchdf()
        gis = db.execute(
            "SELECT b.building_id,b.andamal1,b.andamal2,b.andamal3,b.andamal4,b.andamal5 "
            "FROM read_parquet(?,union_by_name=true,hive_partitioning=false) b "
            "JOIN chosen c USING(building_id)",
            [list(map(str, paths))],
        ).fetchdf()
    if (
        links.duplicated(["building_id", "formular_id"]).any()
        or gis.building_id.duplicated().any()
    ):
        raise ValueError("Ambiguous membership source identity")
    by_link = {
        (row["building_id"], int(row["formular_id"])): row
        for row in links.to_dict("records")
    }
    by_gis = {row["building_id"]: row for row in gis.to_dict("records")}
    hydrated = []
    for original in rows:
        row = dict(original)
        link = by_link.get((row["building_id"], int(row["formular_id"])))
        if link is None or row["building_id"] not in by_gis:
            raise ValueError("Membership source link or GIS building missing")
        if link["property_key"] != row["property_key"]:
            raise ValueError(
                "Membership property identity disagrees with accepted link"
            )
        row["linked_epc_category"] = link["epc_building_category"]
        for key in (
            "semantic_compatibility",
            "certificate_to_footprint_count",
            "subject_to_footprint_count",
            "evidence_tier",
        ):
            row[key] = link[key]
        for i in range(1, 6):
            value = by_gis[row["building_id"]][f"andamal{i}"]
            key = "purpose" if i == 1 else f"purpose{i}"
            if pd.notna(value):
                if row.get(key) and row[key] != value:
                    raise ValueError("Membership GIS purpose changed")
                row[key] = value
        hydrated.append(row)
    return hydrated, {str(path): file_sha256(path) for path in [link_path, *paths]}


def gis_assignment(row):
    """Assign type from all GIS purposes; EPC shares establish operation separately.

    Lantmäteriet Handbok Byggnad (module reference) distinguishes use and placement.
    Specific GIS types lead assignment. Missing/broad labels and multiple specific
    types remain unresolved; purpose order never supplies heated-area percentages.
    An EPC/GIS disagreement is evidence to review, not grounds to silently relabel
    the certificate or put that building into an allegedly pure calibration cohort.
    """
    purposes = [row.get("purpose") or row.get("andamal1")]
    purposes += [row.get(f"purpose{i}") or row.get(f"andamal{i}") for i in range(2, 6)]
    classes = sorted({gis_use(p) for p in purposes} - {"unknown"})
    specific = set(classes) & {"single_family", "multi_family", "office", "school"}
    if "ambiguous_small_multi" in classes:
        status, use = "ambiguous", None
    elif len(specific) > 1:
        status, use = "mixed", None
    elif len(specific) == 1:
        use = next(iter(specific))
        permitted = {
            use,
            "housing_unspecified"
            if use in {"single_family", "multi_family"}
            else "nonresidential_unspecified",
        }
        status = "specific" if set(classes) <= permitted else "mixed_or_conflicting"
        if status != "specific":
            use = None
    else:
        status, use = "unspecified", None
    return {
        "use_group": use,
        "status": status,
        "source": "GIS purpose attributes 1–5",
        "classes": classes,
        "area_shares_inferred": False,
    }
