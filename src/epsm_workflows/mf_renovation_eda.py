"""Rebuild MF certificate evidence without inventing renovation ground truth.

The archive is opened read-only. One certificate owns one energy observation;
addresses and repeated property relations never receive separate statistical
weight. Conflicting payloads remain in the audit and cannot enter comparisons.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from pathlib import Path

import duckdb
import pandas as pd

from .campaign import atomic_json, file_sha256
from .validation import district_heat_observation, measurement_period, number

SCHEMA_VERSION = 1
MF_CATEGORY = "Flerbostadshus"
VERIFIED_NORMAL_USE_FORMS = {"2.6", "2.7", "2.8", "3.0"}
HEAT_CARRIERS = (
    "Fjarrvarme", "Olja", "Gas", "Ved", "Flis", "OvrBiobransle",
    "ElVatten", "ElDirekt", "ElLuft", "PumpMark", "PumpFranluft",
    "PumpLuftLuft", "PumpLuftVatten",
)
VENT_FIELDS = {
    "VentTypSjalvdrag": "S", "VentTypF": "F", "VentTypFT": "FT",
    "VentTypFTX": "FTX", "VentTypFmed": "FVP",
}
PAIR_COLUMNS = (
    "scope_key", "property_key", "first_certificate_id", "last_certificate_id",
    "certificate_count", "first_measurement_year", "last_measurement_year",
    "first_atemp_m2", "last_atemp_m2", "atemp_relative_change",
    "first_space_heat_kwh_m2", "last_space_heat_kwh_m2", "delta_space_heat_kwh_m2",
    "year_gap", "eligible", "reject_reasons", "era", "ventilation",
)


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _clean(value):
    if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
        return None
    return value.item() if hasattr(value, "item") else value


def _optional(raw, field):
    try:
        return number(raw, field, required=False)
    except ValueError:
        return None


def declared_heat_sum(raw, *, suffix=""):
    """Sum declared quantities; all missing remains missing, explicit zero is zero.

    This helper identifies missingness, not a harmonised heating target. The
    unsuffixed old fields include DHW and cannot substitute for modern UPPV.
    """
    values = [_optional(raw, "Egi" + carrier + suffix) for carrier in HEAT_CARRIERS]
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def zone_for_county(county):
    """Catalogue's historical BBR18 grouping, not historical SBN climate zones."""
    if county is None:
        return "unknown"
    value = int(county)
    if value in {23, 24, 25}:
        return "SE.Zone1"
    if value in {17, 20, 21, 22}:
        return "SE.Zone2"
    if value in {1, 3, 4, 5, 6, 7, 8, 9, 10, 12, 13, 14, 18, 19}:
        return "SE.Zone3"
    return "unknown"


def era_for_year(year):
    if year is None or not 1000 <= year <= 2098:
        return "unknown"
    for index, upper in enumerate((1960, 1975, 1985, 1995, 2005, 2098), 1):
        if year <= upper:
            return f"SE.{index:02d}"
    return "unknown"


def harmonise_certificate(raw):
    """Return explicit nullable targets and boundary/status, never a renovation label."""
    raw = {key: _clean(value) for key, value in raw.items()}
    source_schema = raw.get("_schema_version")
    version = raw.get("Version")
    version = str(float(version)) if version is not None else None
    area = _optional(raw, "EgenAtemp")
    construction = _optional(raw, "EgenNybyggAr")
    start = end = None
    reasons = []
    try:
        start, end = measurement_period(
            str(int(raw["EgiForstaArManad"])).zfill(4),
            str(int(raw["EgiSistaArManad"])).zfill(4),
        )
    except (ValueError, TypeError, KeyError):
        reasons.append("invalid_or_nonannual_measurement_period")
    modern = source_schema == "efter_v260"
    suffix = "UPPV" if modern else ""
    positive = [
        carrier for carrier in HEAT_CARRIERS
        if (_optional(raw, "Egi" + carrier + suffix) or 0) > 0
    ]
    carrier = (
        "district_heat" if positive == ["Fjarrvarme"]
        else positive[0] if len(positive) == 1
        else "mixed" if len(positive) > 1 else "undeclared_or_zero"
    )
    amount = declared_heat_sum(raw, suffix=suffix)
    space_heat = None
    boundary = "unresolved"
    normal_use = "unverified"
    measurement_kind = "unverified"
    if source_schema not in {"fore_v260", "efter_v260"}:
        reasons.append("unverified_source_schema")
    if carrier != "district_heat":
        reasons.append("not_single_district_heat_carrier")
    if modern:
        boundary = "normal_use_adjusted_pre_weather_delivered_space_heat"
        if version not in VERIFIED_NORMAL_USE_FORMS:
            reasons.append("unverified_normal_use_form")
        else:
            normal_use = "form_requests_normal_use_adjusted_inputs"
        space_heat = _optional(raw, "EgiFjarrvarmeUPPV")
    else:
        boundary = "reported_delivered_district_heat_including_dhw"
        reasons.append("old_form_normal_use_boundary_unverified")
    if carrier == "district_heat" and start is not None:
        values = {
            key: "" if value is None else str(value)
            for key, value in raw.items()
        }
        values["EgiForstaArManad"] = str(int(raw["EgiForstaArManad"])).zfill(4)
        values["EgiSistaArManad"] = str(int(raw["EgiSistaArManad"])).zfill(4)
        try:
            observation = district_heat_observation(values, schema=source_schema)
            measurement_kind = observation["measurement_kind"]
            if not modern and observation["reported_dhw_subset_kwh"] is not None:
                space_heat = (
                    observation["reported_district_heat_kwh"]
                    - observation["reported_dhw_subset_kwh"]
                )
                boundary = "reported_pre_weather_delivered_space_heat_old_form"
        except ValueError as error:
            reasons.append("boundary_validation:" + str(error))
    if raw.get("EgiBeraknatVarde") != "Nej":
        reasons.append("calculated_or_unknown_energy")
    if area is None or area <= 0:
        reasons.append("invalid_atemp")
    if space_heat is None:
        reasons.append("missing_space_heat")
    elif space_heat <= 0:
        reasons.append("nonpositive_space_heat")
    if construction is None or construction > (end.year if end else 2098):
        reasons.append("invalid_construction_year")
    systems = [value for field, value in VENT_FIELDS.items() if raw.get(field) == "Ja"]
    ventilation = systems[0] if len(systems) == 1 else "mixed" if systems else "unknown"
    measures = sorted(
        field for field, value in raw.items()
        if field.startswith("AtgUtforda") and field != "AtgUtfordaUtfortAr"
        and value == "Ja"
    )
    answered = any(
        value in {"Ja", "Nej"} for field, value in raw.items()
        if field.startswith("AtgUtforda") and field != "AtgUtfordaUtfortAr"
    )
    renovation_year = _optional(raw, "AtgUtfordaUtfortAr")
    interval_status = (
        "positive_optional_interval_evidence" if measures
        else "no_positive_optional_interval_evidence" if answered
        else "unanswered_optional_interval_fields"
    )
    return {
        "source_schema": source_schema,
        "form_version": version,
        "energy_method_version": _optional(raw, "EgiVersion"),
        "certificate_date": str(raw.get("ExpertGodkannande") or "")[:10] or None,
        "measurement_start": start.isoformat() if start else None,
        "measurement_end": end.isoformat() if end else None,
        "measurement_year": end.year if end else None,
        "construction_year": int(construction) if construction is not None else None,
        "age_at_measurement": end.year - int(construction) if end and construction else None,
        "era": era_for_year(construction),
        "county": _optional(raw, "IdLankod"),
        "municipality": _optional(raw, "IdKommunkod"),
        "zone": zone_for_county(_optional(raw, "IdLankod")),
        "ventilation": ventilation,
        "atemp_m2": area,
        "above_ground_storeys": _optional(raw, "EgenAntalPlan"),
        "heated_basement_storeys": _optional(raw, "EgenAntalKallarplan"),
        "apartments": _optional(raw, "EgenAntalBolgh"),
        "residential_share_percent": _optional(raw, "EgenAtempBostad"),
        "designed_flow_l_s_m2": _optional(raw, "EgenProjVentFlode"),
        "declared_heating_kwh": amount,
        "legacy_unsuffixed_heat_missing": declared_heat_sum(raw) is None,
        "space_heat_kwh": space_heat,
        "space_heat_kwh_m2": space_heat / area if space_heat is not None and area and area > 0 else None,
        "heating_carrier": carrier,
        "heating_boundary": boundary,
        "normal_use_status": normal_use,
        "measurement_kind": measurement_kind,
        "normal_year_correction_applied": False,
        "reported_performance_kwh_m2": _optional(raw, "EgiEnergiPrestanda"),
        "renovation_evidence_status": interval_status,
        "optional_measure_count": len(measures),
        "optional_measures": measures,
        "optional_measure_year": int(renovation_year) if renovation_year else None,
        "optional_measure_timing": (
            "after_measurement" if renovation_year and end and renovation_year > end.year
            else "during_measurement" if renovation_year and start and end and start.year <= renovation_year <= end.year
            else "before_measurement" if renovation_year and start and renovation_year < start.year
            else "unknown"
        ),
        "eligible": not reasons,
        "reject_reasons": sorted(set(reasons)),
        "geometry_link_status": "unverified_archive_links_not_used",
    }


def rebuild_certificates(connection):
    """Collapse duplicate rows and seal the complete reported certificate scope."""
    columns = [row[0] for row in connection.execute("DESCRIBE epc").fetchall()]
    required = {"FormularId", "EgenByggnadsKat", "IdKommunkod", "IdFastBet", "IdHusnr"}
    if not required <= set(columns):
        raise ValueError("Archive is missing required certificate/scope fields")
    payload = [
        column for column in columns
        if column.startswith(("Egen", "Egi", "Vent", "AtgUtforda"))
        or column in {"Version", "ExpertGodkannande", "_schema_version", "IdLankod", "IdKommunkod"}
    ]
    projection = ",".join('"' + column + '"' for column in payload)
    packed = ",".join('"' + column + '":="' + column + '"' for column in payload)
    # The scientific payload hash excludes addresses and relational fan-out.
    query = f"""
        WITH mf_ids AS (
            SELECT DISTINCT FormularId FROM epc WHERE EgenByggnadsKat = ?
        ), raw AS (
            SELECT FormularId, IdFastBet, IdHusnr, {projection},
                   sha256(to_json(struct_pack({packed}))) AS payload_sha256
            FROM epc JOIN mf_ids USING (FormularId)
        ), grouped AS (
            SELECT FormularId, count(*) AS source_row_count,
                   count(DISTINCT payload_sha256) AS payload_variant_count,
                   count(DISTINCT (IdKommunkod, IdFastBet, IdHusnr)) AS scope_building_count,
                   count(*) FILTER (WHERE IdKommunkod IS NULL OR IdFastBet IS NULL
                                      OR trim(IdFastBet) = '' OR IdHusnr IS NULL) AS missing_identity_rows,
                   sha256(to_json(list_sort(list(DISTINCT
                       concat_ws('|', IdKommunkod, IdFastBet, IdHusnr))))) AS scope_key,
                   sha256(to_json(list_sort(list(DISTINCT
                       concat_ws('|', IdKommunkod, IdFastBet))))) AS property_key
            FROM raw GROUP BY FormularId
        ), representative AS (
            SELECT * EXCLUDE (IdFastBet,IdHusnr),
                   row_number() OVER (PARTITION BY FormularId ORDER BY payload_sha256) AS rn
            FROM raw
        )
        SELECT r.*, g.* EXCLUDE (FormularId)
        FROM representative r JOIN grouped g USING (FormularId)
        WHERE rn = 1 ORDER BY r.FormularId
    """
    cursor = connection.execute(query, [MF_CATEGORY])
    names = [column[0] for column in cursor.description]
    records = []
    while batch := cursor.fetchmany(4096):
        for values in batch:
            raw = dict(zip(names, values))
            item = harmonise_certificate(raw)
            item.update(
                certificate_id=_digest(["certificate", raw["FormularId"]]),
                property_key=raw["property_key"],
                scope_key=raw["scope_key"],
                source_payload_sha256=raw["payload_sha256"],
                source_row_count=int(raw["source_row_count"]),
                payload_variant_count=int(raw["payload_variant_count"]),
                scope_building_count=int(raw["scope_building_count"]),
            )
            if raw["payload_variant_count"] != 1:
                item["reject_reasons"].append("conflicting_certificate_payloads")
                # A deterministic representative is retained for audit only.
                item["space_heat_kwh"] = item["space_heat_kwh_m2"] = None
            if raw["missing_identity_rows"]:
                item["reject_reasons"].append("incomplete_certificate_identity")
            item["eligible"] = not item["reject_reasons"]
            records.append(item)
    return pd.DataFrame(records)


def build_longitudinal(certificates, *, maximum_area_change=0.10):
    """First/last observed certificates; invalid endpoints stay visible as rejects.

    The pair never skips an invalid first/last observation to find a pleasing
    energy reduction. Scope changes are reported in a separate property audit.
    """
    records = []
    chronology = certificates[certificates.measurement_end.notna()].sort_values(
        ["scope_key", "measurement_end", "certificate_date", "certificate_id"],
        na_position="last",
    )
    for _, group in chronology.groupby("scope_key", sort=True):
        if len(group) < 2:
            continue
        first, last = group.iloc[0], group.iloc[-1]
        reasons = []
        if not first.eligible or not last.eligible:
            reasons.append("invalid_endpoint_certificate")
        for label, row in (("first", first), ("last", last)):
            if pd.isna(row.space_heat_kwh_m2):
                reasons.append(label + "_missing_space_heat")
            elif row.space_heat_kwh_m2 <= 0:
                reasons.append(label + "_nonpositive_space_heat")
        if first.property_key != last.property_key:
            reasons.append("property_identity_changed")
        if first.heating_carrier != last.heating_carrier:
            reasons.append("heating_carrier_changed")
        if first.heating_boundary != last.heating_boundary or first.normal_use_status != last.normal_use_status:
            reasons.append("heating_boundary_changed")
        if last.measurement_start <= first.measurement_end:
            reasons.append("overlapping_or_repeated_measurement_period")
        area_change = (
            abs(last.atemp_m2 / first.atemp_m2 - 1)
            if pd.notna(first.atemp_m2) and first.atemp_m2 > 0 and pd.notna(last.atemp_m2)
            else None
        )
        if area_change is None:
            reasons.append("missing_atemp")
        elif area_change > maximum_area_change + 1e-12:
            reasons.append("atemp_change_exceeds_10_percent")
        values = tuple(
            float(row.space_heat_kwh_m2) if pd.notna(row.space_heat_kwh_m2) else math.nan
            for row in (first, last)
        )
        records.append({
            "scope_key": first.scope_key, "property_key": first.property_key,
            "first_certificate_id": first.certificate_id,
            "last_certificate_id": last.certificate_id,
            "certificate_count": len(group),
            "first_measurement_year": int(first.measurement_year),
            "last_measurement_year": int(last.measurement_year),
            "first_atemp_m2": first.atemp_m2, "last_atemp_m2": last.atemp_m2,
            "atemp_relative_change": area_change,
            "first_space_heat_kwh_m2": values[0], "last_space_heat_kwh_m2": values[1],
            "delta_space_heat_kwh_m2": values[1] - values[0],
            "year_gap": int(last.measurement_year - first.measurement_year),
            "eligible": not reasons, "reject_reasons": sorted(set(reasons)),
            "era": last.era, "ventilation": last.ventilation,
        })
    return pd.DataFrame(records, columns=PAIR_COLUMNS)


def _summaries(frame, grouping):
    records = []
    valid = frame[frame.eligible & frame.space_heat_kwh_m2.notna()]
    for keys, group in valid.groupby(grouping, dropna=False, sort=True):
        keys = keys if isinstance(keys, tuple) else (keys,)
        heat = group.space_heat_kwh_m2
        item = dict(zip(grouping, [_clean(value) for value in keys]))
        item.update(
            n=len(group), properties=int(group.property_key.nunique()),
            median_kwh_m2=float(heat.median()), mean_kwh_m2=float(heat.mean()),
            p10_kwh_m2=float(heat.quantile(0.1)), p90_kwh_m2=float(heat.quantile(0.9)),
            p25_kwh_m2=float(heat.quantile(0.25)), p75_kwh_m2=float(heat.quantile(0.75)),
        )
        records.append(item)
    return records


def run_eda(database, output):
    """Build certificate/pair evidence and grouped MF heating EDA reproducibly."""
    database, output = Path(database).resolve(), Path(output).resolve()
    if (output / "audit.json").exists():
        raise FileExistsError("Use a new EDA output directory")
    output.mkdir(parents=True, exist_ok=True)
    source_hash = file_sha256(database)
    with duckdb.connect(str(database), read_only=True) as connection:
        frame = rebuild_certificates(connection)
        if frame.empty:
            raise ValueError("Archive contains no MF certificates")
        source_columns = [row[0] for row in connection.execute("DESCRIBE epc").fetchall()]
        flags = [column for column in source_columns if column.startswith("AtgUtforda") and column != "AtgUtfordaUtfortAr"]
        any_flag = " OR ".join('CAST("' + column + '" AS VARCHAR) = \'Ja\'' for column in flags) or "FALSE"
        reported = connection.execute(f"""
            SELECT count(*), count(*) FILTER (WHERE ({any_flag})),
                   count(DISTINCT FormularId),
                   count(DISTINCT FormularId) FILTER (WHERE ({any_flag}))
            FROM epc WHERE EgenByggnadsKat = ?
        """, [MF_CATEGORY]).fetchone()
        legacy = connection.execute("""
            SELECT count(*), count(DISTINCT FormularId),
                   count(DISTINCT (IdFastBet,IdHusnr))
            FROM epc WHERE EgenByggnadsKat = ?
              AND EgenNybyggAr BETWEEN 1900 AND 1999
              AND EgiEnergiPrestanda > 0 AND EgenAtemp > 50
        """, [MF_CATEGORY]).fetchone()
    pairs = build_longitudinal(frame)
    frame.to_parquet(output / "certificates.parquet", index=False)
    pairs.to_parquet(output / "longitudinal.parquet", index=False)
    valid = frame[frame.eligible]
    # Cross-sectional stock EDA gives each current scope one observation.
    latest = valid.sort_values(["measurement_end", "certificate_id"]).drop_duplicates("scope_key", keep="last")
    latest.to_parquet(output / "latest_scopes.parquet", index=False)
    reason_counts = Counter(reason for reasons in frame.reject_reasons for reason in reasons)
    pair_reasons = Counter(reason for reasons in pairs.reject_reasons for reason in reasons)
    optional_rate = reported[3] / reported[2]
    weighted_rate = reported[1] / reported[0]
    consistent = frame[frame.payload_variant_count == 1]
    consistent_rate = float(consistent.optional_measure_count.gt(0).mean())
    consistent_weighted = float(
        (consistent.optional_measure_count.gt(0) * consistent.source_row_count).sum()
        / consistent.source_row_count.sum()
    )
    accepted_pairs = pairs[pairs.eligible]
    temporal_eda = latest.assign(
        construction_decade=(latest.construction_year // 10 * 10).astype(int),
        age_decade=(latest.age_at_measurement // 10 * 10).astype(int),
    )
    audit = {
        "schema_version": SCHEMA_VERSION,
        "source_database": str(database), "source_database_sha256": source_hash,
        "workflow_sha256": file_sha256(Path(__file__)),
        "sampling_unit": "unique_certificate_energy_scope; latest_valid_scope_for_stock_eda",
        "privacy": "certificate, property and scope identifiers are SHA256 pseudonyms; raw identifiers omitted",
        "raw_mf_rows": int(reported[0]),
        "source_rows_for_complete_mf_certificate_scopes": int(frame.source_row_count.sum()),
        "unique_mf_certificates": len(frame),
        "duplicate_or_relational_rows_removed": int(frame.source_row_count.sum() - len(frame)),
        "conflicting_certificates": int((frame.payload_variant_count > 1).sum()),
        "multi_building_certificate_scopes": int((frame.scope_building_count > 1).sum()),
        "eligible_certificates": len(valid), "latest_eligible_scopes": len(latest),
        "eligible_properties": int(valid.property_key.nunique()),
        "certificate_rejection_counts_overlapping": dict(sorted(reason_counts.items())),
        "legacy_catalogue_selection": {
            "rows": int(legacy[0]), "unique_certificates": int(legacy[1]),
            "property_house_keys_without_county": int(legacy[2]),
        },
        "optional_interval_evidence_positive_rate": {
            "certificate_weighted": optional_rate, "archive_row_weighted": weighted_rate,
            "certificate_definition": "any positive flag among original rows; conflicts are not resolved into component condition",
            "consistent_payload_certificates": len(consistent),
            "consistent_payload_certificate_weighted": consistent_rate,
            "consistent_payload_row_weighted": consistent_weighted,
            "interpretation": "reporting rates, not verified lifetime renovation prevalence; consistent-payload comparison isolates duplicate/relational weighting",
        },
        "all_null_declared_heating_certificates": int(frame.declared_heating_kwh.isna().sum()),
        "explicit_zero_declared_heating_certificates": int(frame.declared_heating_kwh.eq(0).sum()),
        "legacy_unsuffixed_fields_all_missing_certificates": int(frame.legacy_unsuffixed_heat_missing.sum()),
        "form_versions": dict(Counter(frame.form_version.fillna("unknown"))),
        "longitudinal": {
            "candidate_first_last_scope_pairs": len(pairs), "accepted_pairs": len(accepted_pairs),
            "rejection_counts_overlapping": dict(sorted(pair_reasons.items())),
            "properties_with_multiple_certificate_scopes": int((frame.groupby("property_key").scope_key.nunique() > 1).sum()),
            "median_heat_change_kwh_m2": float(accepted_pairs.delta_space_heat_kwh_m2.median()) if len(accepted_pairs) else None,
            "improved_share": float(accepted_pairs.delta_space_heat_kwh_m2.lt(0).mean()) if len(accepted_pairs) else None,
            "interpretation": "observed pre-weather change; not a causal renovation effect",
        },
        "eda": {
            "by_era": _summaries(latest, ["era"]),
            "by_era_zone_ventilation": _summaries(latest, ["era", "zone", "ventilation"]),
            "by_measurement_year": _summaries(latest, ["measurement_year"]),
            "by_construction_decade_measurement_year": _summaries(temporal_eda, ["construction_decade", "measurement_year"]),
            "by_age_decade": _summaries(temporal_eda, ["age_decade"]),
            "age_period_cohort_policy": "construction period and measurement year are the modelling coordinates; age is derived and never entered alongside both in an unrestricted linear model",
        },
        "geometry": {"status": "not_joined", "reason": "archive UUID_Byggnad absent; footprint certificate/property relations lack reviewed single-building cardinality and geometry identity"},
        "prohibited_ground_truth": ["negative_or_missing_optional_measure_flags", "bottom_quartile_hidden_renovation", "proposed_package_savings"],
        "development_status": "retrospective evidence; no untouched-confirmation claim",
    }
    audit["artifacts"] = {
        name: file_sha256(output / name)
        for name in ("certificates.parquet", "longitudinal.parquet", "latest_scopes.parquet")
    }
    if file_sha256(database) != source_hash:
        raise ValueError("Source database changed during read-only EDA")
    atomic_json(output / "audit.json", audit)
    report = [
        "# Rebuilt MF certificate and renovation evidence", "",
        f"The archive contains **{audit['raw_mf_rows']:,} MF rows representing {len(frame):,} certificates**. Certificate scopes receive one observation each; {audit['conflicting_certificates']:,} conflicting certificate payloads are quarantined.", "",
        f"The original catalogue selection reproduces {legacy[0]:,} rows, {legacy[1]:,} distinct certificates and {legacy[2]:,} property/house keys. These rows are not independent buildings.", "",
        f"**{len(valid):,} certificates in {len(latest):,} latest eligible scopes** have the verified modern form, annual measured single-carrier district-heat space-heating boundary. Targets are divided by Atemp before automatic weather correction; no normal-use adjustment is applied again.", "",
        "Older fields remain distinct. Old total district heat includes DHW; subtracting a declared DHW subset does not verify its normal-use boundary. Old forms are excluded from the principal harmonised comparison. All-null carrier quantities remain missing, while an explicitly declared zero remains zero.", "",
        "## Optional renovation fields", "",
        f"Any positive optional flag appears in {100 * optional_rate:.2f}% of unique certificates versus {100 * weighted_rate:.2f}% under exact archive row weighting. Among the {len(consistent):,} consistent-payload certificates, certificate weighting gives {100 * consistent_rate:.2f}% versus {100 * consistent_weighted:.2f}% with their original repeated rows. These figures measure reporting, not lifetime renovation prevalence. Missing or negative flags do not establish original components; proposed savings and low-energy quartiles are not renovation labels.", "",
        "## Longitudinal comparisons", "",
        f"There are {len(pairs):,} first/last pairs with identical reported building scope; **{len(accepted_pairs):,} pass** valid annual targets, unchanged boundary/carrier, non-overlapping chronology and Atemp agreement within 10%. Invalid endpoints are retained rather than skipped. Property-level scope changes are counted separately in audit.json.", "",
        "The heating-change distribution is descriptive: measured-year weather, operation and reporting changes can also affect it. It does not identify a component upgrade or prove causation.", "",
        "## Current stock heating by construction era", "",
        "| Era | Latest scopes | Median kWh/m² | P10–P90 kWh/m² |", "|---|---:|---:|---:|",
    ]
    for row in audit["eda"]["by_era"]:
        report.append(f"| {row['era']} | {row['n']:,} | {row['median_kwh_m2']:.1f} | {row['p10_kwh_m2']:.1f}–{row['p90_kwh_m2']:.1f} |")
    report.extend([
        "", "The archive provides Atemp, apartments, declared storeys, location, ventilation and measurement periods. Geometry is not joined through unreviewed relations. Group summaries cover all six eras and retain tails.", "",
        "Construction period and observation year are analysed jointly. Building age is derived from them; separate age summaries are descriptive and are not interpreted as independent ageing effects.", "",
        "Artifact hashes, form counts, rejection counts, detailed era/zone/system summaries and period/cohort summaries are in audit.json. This rebuilt archive panel supplies contextual evidence; native model comparisons must use the same independently prepared properties and target boundary.", "",
    ])
    (output / "report.md").write_text("\n".join(report), encoding="utf-8")
    return audit


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    result = run_eda(args.database, args.output)
    print(json.dumps({key: result[key] for key in (
        "unique_mf_certificates", "conflicting_certificates", "eligible_certificates",
        "latest_eligible_scopes", "longitudinal",
    )}, indent=2))


if __name__ == "__main__":
    main()
