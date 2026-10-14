"""Trace fixed EPC targets, physical assumptions and existing numerical failures.

References: archetype_diagnostics.FORM_EVIDENCE (reviewed EPC form definitions),
EPSM Data evidence/lod2_v1_3/claims.json and archived extraction review; Sveby
normal-use inputs in Core. This is a boundary audit, not parameter fitting.
"""

from __future__ import annotations

import codecs
import csv
import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from ..archetype_diagnostics import FORM_EVIDENCE
from ..archetype_membership import (
    assess_membership,
    hydrate_link_membership,
    hydrate_source_membership,
    numeric,
)
from ..campaign import atomic_json, file_sha256
from .common import read


def audit_inputs(config_path, output, *, previous=None):
    """Reconcile original target fields and source-only eligibility before freezing.

    Previously inspected annual targets may be checked for transcription errors;
    no residual informs membership, geometry, folds or bounds. Raw row hashes are
    verified before reading target fields. No correction is silently re-applied.
    """
    path = Path(config_path).resolve()
    config = read(path)
    source = Path(config["cohort_file"])
    if not source.is_absolute():
        source = (path.parent / source).resolve()
    rows, hashes = hydrate_source_membership(read(source))
    publication = Path(config["membership_publication"])
    if not publication.is_absolute():
        publication = (path.parent / publication).resolve()
    rows, linked = hydrate_link_membership(rows, publication)
    hashes.update(linked)
    locators = defaultdict(dict)
    for row in rows:
        # Mirror the preparation defaults for legacy exports that omitted labels.
        row.setdefault(
            "use_group",
            "multi_family"
            if row.get("epc_category") == "FLERBOSTADSHUS"
            else "unknown",
        )
        row.setdefault("ventilation", config.get("ventilation", "unknown"))
        row.setdefault("heating_family", config.get("heating_family", "unknown"))
        locators[row["source_file"]][int(row["source_line"])] = row
    records = []
    for name, wanted in locators.items():
        source_file = Path(name)
        with source_file.open("rb") as stream:
            encoding = (
                "utf-16"
                if stream.read(4).startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE))
                else "latin-1"
            )
        with source_file.open(encoding=encoding, newline="") as stream:
            for line, raw in enumerate(csv.DictReader(stream, delimiter="|"), 2):
                if line not in wanted:
                    continue
                row = wanted[line]
                signature = hashlib.sha256(
                    json.dumps(raw, sort_keys=True).encode()
                ).hexdigest()
                if signature != row["membership_source_row_sha256"]:
                    raise ValueError("Boundary audit source checksum changed")
                if raw["Version"] not in FORM_EVIDENCE:
                    raise ValueError("Unreviewed EPC form version")
                area, heating, dhw = [
                    numeric(raw[k])
                    for k in ("EgenAtemp", "EgiFjarrvarmeUPPV", "EgiFjarrvarmeVV")
                ]
                if (
                    area is None
                    or area <= 0
                    or heating is None
                    or heating <= 0
                    or dhw is None
                    or dhw < 0
                ):
                    raise ValueError("Invalid original annual heating boundary")
                if (
                    not math.isclose(heating, row["heat_kwh"], rel_tol=0, abs_tol=1e-7)
                    or area != row["atemp_m2"]
                ):
                    raise ValueError(
                        "Exported target or Atemp differs from original source"
                    )
                if raw.get("EgiBeraknatVarde") != "Nej":
                    raise ValueError("Annual target is calculated rather than measured")
                if not row.get("normal_use_adjusted") or row.get(
                    "normal_year_corrected"
                ):
                    raise ValueError("Unresolved normal-use/weather target boundary")
                records.append(
                    {
                        "building_id": row["building_id"],
                        "property_key": row["property_key"],
                        "membership": assess_membership(row),
                        "source_file": name,
                        "source_line": line,
                        "source_row_sha256": signature,
                        "form_version": raw["Version"],
                        "form_definition_reference": FORM_EVIDENCE[raw["Version"]],
                        "source_operation_evidence": {
                            k: row.get(k)
                            for k in (
                                "designed_flow_l_s_m2",
                                "design_flow_evidence_status",
                                "completed_renovation_year",
                                "renovation_timing",
                                "completed_window_replacement",
                                "reported_completed_measures",
                            )
                        },
                        "measurement_start": raw["EgiForstaArManad"],
                        "measurement_end": raw["EgiSistaArManad"],
                        "heating_field": "EgiFjarrvarmeUPPV",
                        "dhw_field": "EgiFjarrvarmeVV",
                        "area_field": "EgenAtemp",
                        "target_kwh_m2": heating / area,
                        "dhw_added_to_target": False,
                        "normal_year_correction_applied": False,
                        "normal_use_correction_reapplied": False,
                        "normal_use_status_basis": "Reviewed form semantics; exported fields are not independent correction flags",
                    }
                )
    if len(records) != len(rows):
        raise ValueError("Boundary audit source coverage incomplete")
    evidence = {}
    import epsm_data

    data_root = Path(epsm_data.__file__).resolve().parents[2]
    for name in (
        "ARCHIVE/freeze-2026-09-30/extraction results.json",
        "evidence/lod2_v1_3/claims.json",
    ):
        resource = data_root / name
        if resource.is_file():
            value = read(resource)
            evidence[str(resource)] = {
                "sha256": file_sha256(resource),
                "records": len(value) if isinstance(value, list) else None,
            }
    numerical = []
    if previous:
        for receipt in (Path(previous) / "simulations").glob("*/receipt.json"):
            record = read(receipt)
            if record.get("status") == "accepted":
                continue
            sql = receipt.parent / "energyplus/eplusout.sql"
            ranges = []
            if sql.is_file():
                with sqlite3.connect(
                    sql.resolve().as_uri() + "?mode=ro", uri=True
                ) as db:
                    ranges = db.execute(
                        "SELECT x.KeyValue,min(r.Value),max(r.Value) FROM ReportData r JOIN ReportDataDictionary x USING(ReportDataDictionaryIndex) JOIN Time t USING(TimeIndex) JOIN EnvironmentPeriods e USING(EnvironmentPeriodIndex) WHERE x.Name='Zone Mean Air Temperature' AND e.EnvironmentType=3 AND COALESCE(t.WarmupFlag,0)=0 GROUP BY x.KeyValue"
                    ).fetchall()
            numerical.append(
                {
                    "building_id": record["building_id"],
                    "reason": record.get("reason"),
                    "zone_temperature_ranges_c": ranges,
                    "receipt_sha256": file_sha256(receipt),
                    "decision": "Retain strict failure; no reproducible Core defect established; no warning waiver",
                }
            )
    report = {
        "source_buildings": len(records),
        "eligible_buildings": sum(r["membership"]["eligible"] for r in records),
        "eligible_properties": len(
            {r["property_key"] for r in records if r["membership"]["eligible"]}
        ),
        "form_versions": dict(Counter(r["form_version"] for r in records)),
        "records": records,
        "inputs": hashes
        | {str(source): file_sha256(source), str(path): file_sha256(path)},
        "construction_evidence": evidence,
        "historical_numerical_failures": numerical,
        "new_building_data": False,
        "parameter_fitting": False,
        "normal_use_calibration_exclusions": config.get(
            "calibration_excluded_parameters", []
        ),
        "unresolved": "Renovation state, envelope leakage, actual operating flow, basement conditioned extent, party-wall evidence and source-normalisation uncertainty",
    }
    atomic_json(output, report)
    return {
        key: report[key]
        for key in (
            "source_buildings",
            "eligible_buildings",
            "eligible_properties",
            "form_versions",
        )
    }
