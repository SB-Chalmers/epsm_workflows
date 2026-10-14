"""Retrospective MF parent/history comparison using cohort_transfer native receipts.

Assignment is metadata-only. Targets are stored separately and only requested by
property-isolated fitting/evaluation stages. Original references are immutable;
effective stock parents and probabilistic original-reference histories are rivals.
"""

from __future__ import annotations

import argparse
import math
from collections import Counter, defaultdict
from functools import lru_cache
from itertools import product
from pathlib import Path

import numpy as np

from .campaign import atomic_json, file_sha256
from .cohort_transfer.calibration import candidate_designs
from .cohort_transfer.common import (
    cleaned,
    digest,
    property_folds,
    read,
    source_identity,
)
from .cohort_transfer.grouping import fit_group, representatives
from .cohort_transfer.parameters import applicable
from .cohort_transfer.preparation import (
    TargetStore,
    build_baseline,
    physical_descriptors,
)
from .cohort_transfer.simulation import Evaluator
from .mf_emulator import anchors, design_matrix, physical_coordinates
from .mf_normal_use import (
    BEN_HOUSEHOLD_BASIS,
    SVEBY_HOUSEHOLD_BASIS,
    ben_household_reference,
    current_sveby_reference,
    household_reference_provenance,
)
from .mf_physics import independent_controls, prior_distance, select_candidate

SEED = 20261008
EXCLUDED = (
    "wwr",
    "shgc_multiplier",
    "solar_shading_factor",
    "natural_exchange_multiplier",
    "people_multiplier",
    "equipment_multiplier",
    "lighting_multiplier",
    "setpoint_offset_c",
    "schedule_shift_hours",
    "thermal_bridge_uplift",
    "soil_conductivity",
    "attic_exchange_ach",
    "net_loss_kwh_m2",
    "delivery_efficiency",
    "ground_resistance",
    "basement_wall_resistance",
)
OPERATING = (
    "infiltration_multiplier",
    "flow_multiplier",
    "recovery",
    "basement_setpoint_c",
)
FABRIC = (
    "window_u_multiplier",
    "wall_resistance",
    "roof_resistance",
    "wall_insulation_overlay_m",
)


def _targets(rows):
    """Read original selected, hash-verified EPC locators; never reinterpret nulls."""
    import codecs
    import csv
    import hashlib
    import json

    from .stock.prepare import parse_record

    wanted = defaultdict(lambda: defaultdict(list))
    for row in rows:
        wanted[row["source_file"]][int(row["source_line"])].append(row)
    result = {}
    for name, lines in wanted.items():
        path = Path(name)
        with path.open("rb") as stream:
            encoding = (
                "utf-16"
                if stream.read(4).startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE))
                else "latin-1"
            )
        with path.open(encoding=encoding, newline="") as stream:
            for line, raw in enumerate(csv.DictReader(stream, delimiter="|"), 2):
                if line not in lines:
                    continue
                signature = hashlib.sha256(
                    json.dumps(raw, sort_keys=True).encode()
                ).hexdigest()
                for row in lines[line]:
                    if signature != row["source_row_sha256"] or int(
                        raw["FormularId"]
                    ) != int(row["formular_id"]):
                        raise ValueError("Selected EPC observation changed")
                    parsed = parse_record(raw)
                    if (
                        parsed["heating_family"] != "district_heat"
                        or parsed["measurement_year"] != row["measurement_year"]
                    ):
                        raise ValueError("Selected EPC carrier or chronology changed")
                    result[row["building_id"]] = parsed["target"]
    if len(result) != len(rows):
        raise ValueError("Selected EPC target locators were not all recovered")
    return result


def _pin_receipt_chain(record, sources, seen=None):
    """Verify and retain every declared ancestor of an imported native receipt."""
    seen = set() if seen is None else seen
    for name in ("native_transfer", "reused_strict_receipt"):
        link = record.get(name)
        if not link:
            continue
        path = str(Path(link["source"]).resolve())
        if path in seen:
            raise ValueError("Native receipt provenance cycle")
        if file_sha256(path) != link["sha256"]:
            raise ValueError("Ancestral native receipt source changed")
        sources[path] = link["sha256"]
        _pin_receipt_chain(read(path), sources, seen | {path})


@lru_cache(maxsize=512)
def _validated_template_climate(path, checksum, year):
    """Validate each pinned municipality/year file once during preparation."""
    import csv

    from epsm_core.weather import climate_from_epw, validate_epw_calendar

    validate_epw_calendar(path, year)
    with Path(path).open(encoding="utf-8-sig") as stream:
        reader = csv.reader(stream)
        for _ in range(8):
            next(reader)
        if {int(record[0]) for record in reader} != {year}:
            raise ValueError(
                "Replacement EPW source years differ from measurement year"
            )
    climate = climate_from_epw(path, calendar_year=year)
    if climate.weather_sha256 != checksum:
        raise ValueError("Replacement EPW checksum changed while reading climate")
    return climate


def refresh_template_climate(spec, weather_record, measurement_year):
    """Replace only climate when validated building descriptors still match."""
    if (
        isinstance(measurement_year, bool)
        or not isinstance(measurement_year, (int, float))
        or not math.isfinite(measurement_year)
        or int(measurement_year) != measurement_year
    ):
        raise ValueError("Replacement weather requires an integer measurement year")
    year = int(measurement_year)
    for declared in (
        weather_record.get("year"),
        weather_record.get("calendar_validation", {}).get("calendar_year"),
    ):
        if declared is not None and declared != year:
            raise ValueError(
                "Declared replacement weather year differs from measurement year"
            )
    path = str(Path(weather_record["path"]).resolve())
    checksum = weather_record["sha256"]
    if file_sha256(path) != checksum:
        raise ValueError("Declared replacement EPW checksum differs from file")
    climate = _validated_template_climate(path, checksum, year)
    if climate.weather_sha256 != checksum or file_sha256(path) != checksum:
        raise ValueError("Replacement EPW checksum changed while reading climate")
    return spec.updated(climate=climate)


def original_construction_spec(spec, row):
    """Separate historical components from the catalogue's surviving-stock defaults."""
    from .mf_stock_history import original_component_claims, original_window_profile

    profile = original_window_profile(row["construction_year"])
    if profile["era"] != row["era"]:
        raise ValueError("Historical window period disagrees with construction era")
    glazing = spec.facade.glazing.updated(
        u_value_w_m2k=profile["u_value_w_m2k"],
        solar_heat_gain_coefficient=profile["solar_heat_gain_coefficient"],
        visible_transmittance=profile["visible_transmittance"],
    )
    original = spec.updated(facade=spec.facade.updated(glazing=glazing))
    return original, {
        "window": profile,
        "component_claims": original_component_claims(row["era"]),
        "catalogue_parent_window_u_w_m2k": spec.facade.glazing.u_value_w_m2k,
        "historical_window_override_applied": glazing != spec.facade.glazing,
        "other_assemblies": "Data original-construction representatives; ground interfaces retain their declared effective boundary",
        "geometry_weather_use_and_airflow_preserved": True,
    }


def frozen_original_spec(root, row, *, references=None, manifest=None):
    """Require the separately frozen historical source used by every history state."""
    from epsm_core import BuildingSpec

    root = Path(root)
    references = (
        references
        if references is not None
        else read(root / "original-construction-references.json")
    )
    reference = references[row["building_id"]]
    if not reference["basis"].startswith("original_construction"):
        raise ValueError("Dynamic source requires an original construction reference")
    if (
        row.get("original_reference_spec_path", reference["spec_path"])
        != reference["spec_path"]
    ):
        raise ValueError("Original registry disagrees with the dynamic source spec")
    path = root / reference["spec_path"]
    manifest = manifest if manifest is not None else read(root / "manifest.json")
    if file_sha256(path) != manifest["artifacts"][reference["spec_path"]]:
        raise ValueError("Frozen original-reference spec changed")
    original = BuildingSpec.model_validate(read(path))
    if original.physical_hash() != reference["physical_hash"]:
        raise ValueError("Original-reference physical hash disagrees with registry")
    return original


def parent_transfer_runtime_checks(runtime):
    """Limit version exceptions to history/selection code outside native execution."""
    exceptions = {}
    selection_only_modules = {
        Path(__file__).resolve(),
        Path(__file__).with_name("mf_stock_history.py").resolve(),
        Path(__file__).with_name("mf_parent_subdivision.py").resolve(),
        Path(__file__).with_name("mf_comparison.py").resolve(),
    }
    for name, checksum in runtime.items():
        physical_runtime = (
            "/epsm_core/" in name
            or "/epsm_data/" in name
            or (
                "/epsm_workflows/src/" in name
                and Path(name).resolve() not in selection_only_modules
            )
        )
        selection_only = Path(name).resolve() in selection_only_modules
        if not physical_runtime and not selection_only:
            continue
        current = file_sha256(name)
        if physical_runtime and current != checksum:
            raise ValueError(
                "Native transfer physical/diagnostic implementation changed"
            )
        if selection_only and current != checksum:
            exceptions[name] = {
                "previous_sha256": checksum,
                "current_sha256": current,
                "basis": "Parent/current only; identical reconstructed native spec, carrier, Atemp, engine and unchanged execution modules; no dynamic-bank transfer",
            }
    return exceptions


def parent_transfer_receipts(prior, manifest):
    """Include pinned ancestral current receipts under the same exact guards.

    Current predictions may have no local receipt after an earlier verified
    import. Only checksum-pinned current-native receipts are eligible here;
    fitting outputs and dynamic-history banks never enter this transfer list.
    """
    prior = Path(prior)
    candidates = {
        path.resolve(): current
        for bank, current in ((prior, False), (prior / "current-native", True))
        for path in (bank / "simulations").glob("*/receipt.json")
    }
    for name, checksum in manifest.get("sources", {}).items():
        path = Path(name)
        if (
            path.name == "receipt.json"
            and path.parent.parent.name == "simulations"
            and path.parent.parent.parent.name == "current-native"
        ):
            if file_sha256(path) != checksum:
                raise ValueError("Pinned ancestral current native receipt changed")
            candidates[path.resolve()] = True
    return sorted(candidates.items(), key=lambda item: str(item[0]))


def certificate_scope_check(row, records):
    """Require a complete single-subject certificate, independently of linkage.

    A single linked footprint does not establish that its EPC energy and Atemp
    cover only that building. Missing or disagreeing scope evidence is retained
    as a preparation quarantine, never inferred from geometry or energy use.
    """
    record = records.get(row["building_id"])
    reasons = []
    if not isinstance(record, dict):
        return {"eligible": False, "reasons": ["missing_certificate_scope_evidence"]}
    for name in ("building_id", "formular_id", "property_key", "source_row_sha256"):
        if row.get(name) is None or record.get(name) != row.get(name):
            reasons.append(f"scope_identity_mismatch:{name}")
    for name in ("release_subject_count", "archive_subject_count"):
        value = record.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            reasons.append(f"missing_or_invalid_scope:{name}")
        elif value != 1:
            reasons.append(f"multiple_certificate_subjects:{name}")
    return {
        "eligible": not reasons,
        "reasons": reasons,
        "release_subject_count": record.get("release_subject_count"),
        "archive_subject_count": record.get("archive_subject_count"),
    }


def current_assumptions_spec(spec, row, *, shading=1.0):
    """Restore exact current household gains, independently of the fitting branch."""
    current = current_sveby_reference(
        spec, source_basis=row.get("normal_use_basis", SVEBY_HOUSEHOLD_BASIS)
    )
    return current.updated(facade=current.facade.updated(solar_shading_factor=shading))


def prepare(
    selection, existing, output, *, weather=None, reuse_native=None, scope_evidence=None
):
    """Freeze a new panel and failures; corrected shading is explicit and opt-in."""
    from epsm_core import BuildingSpec
    from epsm_data.application import resolve_building_archetype

    from .mf_evidence import insulation_candidates

    root, old = Path(output).resolve(), Path(existing).resolve()
    if (root / "manifest.json").exists():
        verify(root)
        return read(root / "preparation-summary.json")
    rows = read(Path(selection) / "cohort.json")
    if len({r["building_id"] for r in rows}) != len(rows):
        raise ValueError("Duplicate selected building identities")
    scope_records = {}
    if scope_evidence:
        evidence = read(scope_evidence)
        if evidence.get("schema_version") != 1 or not isinstance(
            evidence.get("records"), dict
        ):
            raise ValueError("Certificate scope evidence requires version 1 records")
        scope_records = evidence["records"]
    config = read(old / "config.json")
    old_features = {r["building_id"]: r for r in read(old / "features.json")}
    old_runtime = read(old / "manifest.json")["runtime"]
    for name, checksum in old_runtime.items():
        if ("/epsm_core/" in name or "/epsm_data/" in name) and file_sha256(
            name
        ) != checksum:
            raise ValueError("Historical Core/Data source identity changed")
    prior = Path(reuse_native).resolve() if reuse_native else None
    prior_features, prior_manifest = {}, None
    transfer_runtime_exceptions = {}
    if prior:
        prior_manifest = read(prior / "manifest.json")
        if prior_manifest["engine_sha256"] != file_sha256(config["engine"]):
            raise ValueError("Native transfer engine changed")
        # These modules select study candidates; they do not execute
        # a parent/current native model. Every transferred physical spec is still
        # reconstructed and matched below. Dynamic native banks are never reused.
        transfer_runtime_exceptions = parent_transfer_runtime_checks(
            prior_manifest["runtime"]
        )
        prior_features = {r["building_id"]: r for r in read(prior / "features.json")}
        if (
            file_sha256(prior / "features.json")
            != prior_manifest["artifacts"]["features.json"]
        ):
            raise ValueError("Transferred preparation descriptors changed")
    weather_manifest = read(weather) if weather else config["weather_manifest"]
    # Material provenance is an assembly alternative, not an observed renovation
    # or a claim that the 1990 material existed at every historical installation.
    reference = resolve_building_archetype(
        category="multi_family",
        construction_year=1990,
        climate_zone="SE.Zone3",
        ventilation="F",
    )["envelope"].wall
    material = reference.layers[insulation_candidates(reference)[0]].material
    config.update(
        {
            "schema_version": 1,
            "seed": SEED,
            "workers": 8,
            "parallel_backend": "processes",
            "resistance_basis": "total_assembly_r",
            "apply_residential_shading": True,
            "mf_reported_zero_flow_policy": "normal_use",
            "study_kind": "retrospective_mf_parent_dynamic_comparison",
            "weather_manifest": weather_manifest,
            "wall_overlay": {
                "material": material.model_dump(mode="json"),
                "source": "EPSM Data MF SE.04 original wall insulation material; 0-150mm explicitly engineering assembly alternatives, installation history unverified",
            },
            "calibration_excluded_parameters": list(EXCLUDED),
            "minimum_training_properties": 30,
            "outer_folds": 5,
            "inner_folds": 3,
            "history_draws": 100,
            "history_upgrade_profiles": {},
            "history_mapping_status": "Roof/facade temporal thermal mappings unsupported; inactive in principal comparison",
            "coverage_definition": "absolute_relative_error <= 0.20; failed predictions count outside coverage",
            "certificate_scope_policy": "Require independently agreeing single-subject release/archive evidence, linked footprint count alone is insufficient",
            "fixed_normal_use_reference": household_reference_provenance(),
        }
    )
    current_by_id = {}
    old_engine = read(old / "engine.json")
    from epsm_core import EnergyPlusRunner

    if old_engine.get("engine_hash") != file_sha256(config["engine"]):
        raise ValueError("Historical baseline used a different native engine")
    current_runner = EnergyPlusRunner(energyplus=config["engine"])
    if old_engine.get("schema_hash") != current_runner.compiler.schema_hash:
        raise ValueError("Historical EnergyPlus schema changed")
    for path in sorted((old / "runs").glob("*/receipt.json")):
        record = read(path)
        if (
            record.get("status") == "accepted"
            and record.get("scenario", "baseline") == "baseline"
        ):
            spec = BuildingSpec.model_validate(read(old / record["spec_path"]))
            if record.get("physical_hash") != spec.physical_hash():
                raise ValueError(
                    "Historical receipt does not match its pinned physical spec"
                )
            current_by_id[record["building_id"]] = (record["prediction_kwh_m2"], path)
    features, outcomes, registers, original = [], [], {}, {}
    sources = {
        str(Path(selection).resolve() / "cohort.json"): file_sha256(
            Path(selection) / "cohort.json"
        ),
        str(old / "config.json"): file_sha256(old / "config.json"),
        str(old / "engine.json"): file_sha256(old / "engine.json"),
    }
    if weather:
        sources[str(Path(weather).resolve())] = file_sha256(weather)
    if scope_evidence:
        sources[str(Path(scope_evidence).resolve())] = file_sha256(scope_evidence)
    if prior:
        sources[str(prior / "manifest.json")] = file_sha256(prior / "manifest.json")
    for row in rows:
        bid = row["building_id"]
        outcome = {
            "building_id": bid,
            "property_key": row["property_key"],
            "era": row["era"],
            "ventilation": row["ventilation"],
        }
        key = f"{row['municipality']}:{row['measurement_year']}"
        scope = certificate_scope_check(row, scope_records)
        outcome["certificate_scope"] = scope
        if not scope["eligible"]:
            outcomes.append(
                outcome
                | {
                    "status": "certificate_scope_quarantined",
                    "weather_available": key in weather_manifest,
                }
            )
            continue
        if key not in weather_manifest:
            outcomes.append(
                outcome | {"status": "needs_local_measurement_year_weather"}
            )
            continue
        try:
            prior_row = old_features.get(bid) or prior_features.get(bid)
            template_root = old if bid in old_features else prior
            reliable_keys = (
                "geometry_wkb_hex",
                "roof_form",
                "construction_year",
                "ventilation",
                "atemp_m2",
                "storeys",
                "heated_basement_floors",
                "measurement_year",
                "municipality",
                "height_m",
                "eave_height_m",
                "floor_height_m",
                "representative_height_m",
                "catalogue_climate_zone",
                "designed_flow_l_s_m2",
                "component_states",
                "component_sources",
                "mf_evidence",
            )
            if prior_row and all(prior_row.get(k) == row.get(k) for k in reliable_keys):
                template_path = template_root / prior_row["spec_path"]
                if (
                    template_root == prior
                    and file_sha256(template_path)
                    != prior_manifest["artifacts"][prior_row["spec_path"]]
                ):
                    raise ValueError("Transferred preparation spec changed")
                spec = BuildingSpec.model_validate(read(template_path))
                sources[str(template_path)] = file_sha256(template_path)
                replacement_weather = weather_manifest[key]
                if (
                    spec.climate.weather_sha256 != replacement_weather["sha256"]
                    or Path(spec.climate.epw_path).resolve()
                    != Path(replacement_weather["path"]).resolve()
                ):
                    old_climate = spec.climate
                    spec = refresh_template_climate(
                        spec, replacement_weather, row["measurement_year"]
                    )
                    outcome["template_weather_refresh"] = {
                        "old_weather_sha256": old_climate.weather_sha256,
                        "new_weather_sha256": spec.climate.weather_sha256,
                        "old_path": old_climate.epw_path,
                        "new_path": spec.climate.epw_path,
                        "measurement_year": spec.climate.calendar_year,
                        "building_physical_inputs_preserved": True,
                    }
                spec = spec.updated(
                    facade=spec.facade.updated(solar_shading_factor=0.5)
                )
                use_basis = prior_row.get("normal_use_basis", SVEBY_HOUSEHOLD_BASIS)
            else:
                spec = build_baseline(
                    row,
                    config | {"catalogue_climate_zone": row["catalogue_climate_zone"]},
                )
                use_basis = SVEBY_HOUSEHOLD_BASIS
            spec = ben_household_reference(spec, source_basis=use_basis)
            path = f"specs/{bid}/baseline.json"
            atomic_json(root / path, spec.model_dump(mode="json"))
            register = applicable(
                spec,
                locked=row.get("locked_parameters", ()),
                resistance_basis="total_assembly_r",
                wall_overlay=config["wall_overlay"],
            )
            register["wall_insulation_overlay_m"].update(
                {
                    "baseline_wall_r_m2k_w": 1 / spec.envelope.wall.u_value_w_m2k,
                    "overlay_conductivity_w_mk": material.conductivity_w_mk,
                }
            )
            for name in EXCLUDED:
                if (
                    name in register
                    and register[name]["status"] == "screen_and_cohort_calibration"
                ):
                    register[name]["status"] = "fixed_normal_use_or_unobserved_geometry"
            for name in ("wall_resistance", "roof_resistance"):
                register[name]["bounds"] = [0.8, 1.6]
                register[name]["source"] = (
                    "Declared total assembly R sensitivity, 0.8-1.6; only reviewed insulation depth changes"
                )
            registers[bid] = register
            original_path = f"original-specs/{bid}/baseline.json"
            historical, component_basis = original_construction_spec(spec, row)
            atomic_json(root / original_path, historical.model_dump(mode="json"))
            for evidence_path, checksum in (
                component_basis["window"].get("source_hashes", {}).items()
            ):
                if file_sha256(evidence_path) != checksum:
                    raise ValueError("Historical component source changed")
                sources[evidence_path] = checksum
            features.append(
                row
                | physical_descriptors(row, spec)
                | {
                    "status": "prepared",
                    "spec_path": path,
                    "membership": {"eligible": True},
                    "certificate_scope": scope,
                    "normal_use_basis": BEN_HOUSEHOLD_BASIS,
                    "height_group": "low_1_3"
                    if row["storeys"] <= 3
                    else "medium_4_6"
                    if row["storeys"] <= 6
                    else "tall_7_plus",
                    "parent_baseline_basis": "catalogue_existing_stock",
                    "original_reference_spec_path": original_path,
                    "shading_source": "Core Sveby residential solar_shading_factor=0.5",
                    "geometry_uncertainty": [
                        "window_area_assumed_20pct",
                        "adjacency_unverified",
                        "full_footprint_basement_if_present",
                    ],
                }
            )
            original[bid] = {
                "spec_path": original_path,
                "parent_baseline_spec_path": path,
                "physical_hash": historical.physical_hash(),
                "wall_u": historical.envelope.wall.u_value_w_m2k,
                "roof_u": historical.envelope.top.u_value_w_m2k,
                "window_u": historical.facade.glazing.u_value_w_m2k,
                "basis": "original_construction_with_fixed_normal_use_and_corrected_shading",
                "component_basis": component_basis,
            }
            sources[str(Path(spec.climate.epw_path).resolve())] = (
                spec.climate.weather_sha256
            )
            outcomes.append(outcome | {"status": "prepared"})
        except Exception as exc:  # noqa: BLE001 - preserve scientific preparation failures
            outcomes.append(
                outcome
                | {
                    "status": "preparation_failed",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
    target_values = _targets(features)
    for prop in sorted({row["property_key"] for row in features}):
        atomic_json(
            root / "targets" / f"{digest(prop)}.json",
            {
                r["building_id"]: target_values[r["building_id"]]
                for r in features
                if r["property_key"] == prop
            },
        )
    # Recover current native predictions only when identifiers/weather/geometry
    # and physical input specs agree. Newly prepared rows get native current specs.
    current, shading_only = {}, {}
    for row in features:
        bid = row["building_id"]
        old_row = old_features.get(bid)
        if old_row and bid in current_by_id:
            old_spec = BuildingSpec.model_validate(read(old / old_row["spec_path"]))
            new_spec = BuildingSpec.model_validate(read(root / row["spec_path"]))
            matching = current_assumptions_spec(
                new_spec, row, shading=old_spec.facade.solar_shading_factor
            )
            if matching.physical_hash() == old_spec.physical_hash():
                current[bid] = current_by_id[bid][0]
                receipt_path = current_by_id[bid][1]
                sources[str(receipt_path.resolve())] = file_sha256(receipt_path)
                _pin_receipt_chain(read(receipt_path), sources)
                sources[str(old / old_row["spec_path"])] = file_sha256(
                    old / old_row["spec_path"]
                )
                reused = read(receipt_path).get("reused_strict_receipt")
                if reused:
                    if reused["engine"] != old_engine:
                        raise ValueError(
                            "Historical reused receipt engine/schema differs"
                        )
                    if file_sha256(reused["source"]) != reused["sha256"]:
                        raise ValueError("Historical reused receipt source changed")
                    sources[reused["source"]] = reused["sha256"]
        if row["source_file"] not in sources:
            sources[row["source_file"]] = file_sha256(row["source_file"])
    transferred = []
    if prior:
        # Reuse only cases whose complete native physical inputs still agree.
        # Workflow receipts copied in an earlier version can have no local spec;
        # for those the frozen baseline provides the independently checked input.
        feature_map = {r["building_id"]: r for r in features}
        transfer_baselines = {
            bid: BuildingSpec.model_validate(read(root / row["spec_path"]))
            for bid, row in feature_map.items()
        }
        for path, current_bank in parent_transfer_receipts(prior, prior_manifest):
            receipt = read(path)
            bid = receipt.get("building_id")
            if receipt.get("status") != "accepted" or bid not in feature_map:
                continue
            row = feature_map[bid]
            from .mf_physics import apply_physical_profile

            baseline = transfer_baselines[bid]
            if current_bank:
                baseline = current_assumptions_spec(baseline, row)
            requested = receipt.get(
                "requested_parameters", receipt.get("parameters", {})
            )
            filtered = {
                n: v
                for n, v in requested.items()
                if n in registers[bid]
                and registers[bid][n]["status"] == "screen_and_cohort_calibration"
            }
            native_spec_path = path.parent / "spec.json"
            expected = apply_physical_profile(
                baseline,
                {
                    "parameters": filtered,
                    "bounds": {n: v["bounds"] for n, v in registers[bid].items()},
                    "wall_overlay": config["wall_overlay"],
                    "source": "Verified physical native cache transfer",
                    "infiltration_basis": "atemp_area_rate"
                    if baseline.airflow.envelope_infiltration_l_s_m2 is not None
                    else "core_volume_ach",
                },
                locked_parameters=row.get("locked_parameters", ()),
                resistance_basis="total_assembly_r",
            )
            if expected.physical_hash() != receipt.get("spec_sha256"):
                if not current_bank and not requested:
                    shading_spec = current_assumptions_spec(
                        transfer_baselines[bid], row, shading=0.5
                    )
                    if shading_spec.physical_hash() == receipt.get("spec_sha256"):
                        if native_spec_path.exists() and (
                            BuildingSpec.model_validate(
                                read(native_spec_path)
                            ).physical_hash()
                            != receipt["spec_sha256"]
                        ):
                            raise ValueError("Transferred shading receipt spec changed")
                        scalar = receipt["scalars"].get(
                            "space_heating_carrier_input_kwh"
                        )
                        if scalar is None or not math.isclose(
                            scalar / row["atemp_m2"],
                            receipt["prediction"],
                            rel_tol=1e-10,
                        ):
                            raise ValueError(
                                "Transferred shading carrier/area boundary differs"
                            )
                        sources[str(path)] = file_sha256(path)
                        if native_spec_path.exists():
                            sources[str(native_spec_path)] = file_sha256(
                                native_spec_path
                            )
                        _pin_receipt_chain(receipt, sources)
                        shading_only[bid] = receipt["prediction"]
                continue
            if native_spec_path.exists():
                native_spec = BuildingSpec.model_validate(read(native_spec_path))
                if native_spec.physical_hash() != receipt["spec_sha256"]:
                    raise ValueError("Transferred native receipt spec changed")
                sources[str(native_spec_path)] = file_sha256(native_spec_path)
            elif filtered:
                raise ValueError(
                    "Transferred parameterized native receipt lacks its spec"
                )
            scalar = receipt["scalars"].get("space_heating_carrier_input_kwh")
            if scalar is None or not math.isclose(
                scalar / row["atemp_m2"], receipt["prediction"], rel_tol=1e-10
            ):
                raise ValueError("Transferred native carrier/area boundary differs")
            sources[str(path)] = file_sha256(path)
            _pin_receipt_chain(receipt, sources)
            if current_bank:
                if requested:
                    continue
                current[bid] = receipt["prediction"]
            else:
                transferred.append((row, requested, receipt, path, expected))
    summary = {
        "selected": len(rows),
        "prepared": len(features),
        "outcomes": dict(Counter(r["status"] for r in outcomes)),
        "by_era": dict(Counter(r["era"] for r in features)),
        "current_native_receipts_imported": len(current),
        "shading_only_native_receipts_imported": len(shading_only),
        "fixed_normal_use_reference": household_reference_provenance(),
        "evaluation": "retrospective_development_only",
        "panel_frozen": True,
        "carrier": "district_heat",
        "other_carriers": "unvalidated",
    }
    if prior:
        summary["corrected_baselines_reused"] = len(
            {r["building_id"] for r, parameters, *_ in transferred if not parameters}
        )
        summary["native_cases_reused"] = len(transferred)
        summary["native_transfer_runtime_exceptions"] = transfer_runtime_exceptions
    for name, value in (
        ("config.json", config),
        ("features.json", features),
        ("parameter-register.json", registers),
        ("preparation-outcomes.json", outcomes),
        ("preparation-summary.json", summary),
        ("original-construction-references.json", original),
        ("current-native-predictions.json", current),
        ("shading-native-predictions.json", shading_only),
    ):
        atomic_json(root / name, cleaned(value))
    artifacts = {
        str(p.relative_to(root)): file_sha256(p)
        for p in root.rglob("*.json")
        if p.name != "manifest.json"
    }
    atomic_json(
        root / "manifest.json",
        {
            "schema_version": 1,
            "runtime": source_identity(),
            "sources": sources,
            "artifacts": artifacts,
            "engine_sha256": file_sha256(config["engine"]),
            "panel_scope": summary,
            "confirmation": False,
        },
    )
    if transferred:
        with Evaluator(root) as evaluator:
            for row, parameters, receipt, path, spec in transferred:
                key = evaluator.case_key(row, parameters)
                atomic_json(
                    root / "simulations" / key / "spec.json",
                    spec.model_dump(mode="json"),
                )
                atomic_json(
                    root / "simulations" / key / "receipt.json",
                    receipt
                    | {
                        "identity": key,
                        "building_id": row["building_id"],
                        "requested_parameters": parameters,
                        "native_transfer": {
                            "source": str(path),
                            "sha256": file_sha256(path),
                            "basis": "identical_native_physical_hash_weather_carrier_area_engine_schema_Core_Data",
                        },
                    },
                )
    return summary


def verify(root):
    root = Path(root)
    manifest = read(root / "manifest.json")
    if manifest["runtime"] != source_identity():
        raise ValueError(
            "Scientific runtime changed; prepare a new version rather than resuming stale receipts"
        )
    for group in ("sources", "artifacts"):
        for name, checksum in manifest[group].items():
            path = root / name if group == "artifacts" else Path(name)
            if file_sha256(path) != checksum:
                raise ValueError(f"Frozen {group} changed: {name}")
    if file_sha256(read(root / "config.json")["engine"]) != manifest["engine_sha256"]:
        raise ValueError("Native engine changed")


def _shared_register(rows, evaluator):
    from .cohort_transfer.calibration import shared_register

    return shared_register(rows, evaluator.registers)


def screen(root, evaluator):
    """Screen every requested control without targets, using physical contrasts."""
    root = Path(root)
    path = root / "sensitivity.json"
    if path.exists():
        return read(path)
    rows = read(root / "features.json")
    reps = []
    for era in sorted({r["era"] for r in rows}):
        members = [r for r in rows if r["era"] == era]
        reps.extend(representatives(members, fit_group(members, 1), 0, limit=5))
    register = _shared_register(rows, evaluator)
    designs = {"baseline": {}}
    for name in (*OPERATING, *FABRIC):
        if name not in register:
            continue
        lo, hi = register[name]["bounds"]
        designs[f"{name}:lo"] = {name: lo}
        designs[f"{name}:hi"] = {name: hi}
    panel = evaluator.batch(reps, designs)
    vectors, effects = {}, {}
    for name in (*OPERATING, *FABRIC):
        if name not in register:
            continue
        vector = [
            None
            if panel[r["building_id"], f"{name}:hi"] is None
            or panel[r["building_id"], f"{name}:lo"] is None
            else panel[r["building_id"], f"{name}:hi"]
            - panel[r["building_id"], f"{name}:lo"]
            for r in reps
        ]
        vectors[name] = vector
        effects[name] = float(
            np.nanmean(np.abs([np.nan if v is None else v for v in vector]))
        )
    operating = independent_controls(
        {k: vectors[k] for k in OPERATING if k in vectors},
        effects=effects,
        max_controls=1,
    )
    shared_operating = operating["active"]
    pooled_vectors = {
        n: vectors[n]
        for n in shared_operating + ["window_u_multiplier"]
        if n in vectors
    }
    priority = max(effects.values(), default=1.0) * 1e6 + 1.0
    pooled_independence = independent_controls(
        pooled_vectors,
        effects=effects | {n: priority for n in shared_operating},
        max_controls=2,
    )
    # Check every available construction axis jointly with shared operation.
    # A confounded stronger wall/window direction must not hide an independent
    # roof direction. Pooled windows remain an alternative fitting block.
    opaque = {}
    for era in sorted({r["era"] for r in rows}):
        ids = [i for i, r in enumerate(reps) if r["era"] == era]
        subset = {
            name: [vector[i] for i in ids]
            for name, vector in vectors.items()
            if name in FABRIC
        }
        local_effects = {
            k: float(np.nanmean(np.abs([np.nan if x is None else x for x in v])))
            for k, v in subset.items()
        }
        opaque_names = [n for n in subset if n != "window_u_multiplier"]
        ranked = independent_controls(subset, effects=local_effects, max_controls=2)
        joint_vectors = {
            n: [vectors[n][i] for i in ids] for n in shared_operating + list(subset)
        }
        joint_independence = independent_controls(
            joint_vectors,
            effects=local_effects | {n: priority for n in shared_operating},
            max_controls=3,
        )
        ranked["joint_independence"] = joint_independence
        ranked["active"] = [n for n in joint_independence["active"] if n in FABRIC]
        ranked["alternative_opaque_controls"] = [
            n for n in opaque_names if n not in ranked["active"]
        ]
        ranked["effects"] = local_effects
        opaque[era] = ranked
    report = {
        "representatives": [r["building_id"] for r in reps],
        "effects": effects,
        "response_vectors": vectors,
        "operating": operating,
        "pooled_fit_controls": pooled_independence["active"],
        "pooled_independence": pooled_independence,
        "fabric_by_era": opaque,
        "register": register,
        "observed_heating_used": False,
        "confounded_alternatives_retained_as_uncertainty": True,
        "fixed_inputs": list(EXCLUDED),
        "simulations": len(panel),
        "failed": sum(v is None for v in panel.values()),
    }
    atomic_json(path, cleaned(report))
    return report


def _vector(settings, active, register):
    return physical_coordinates(settings, active, register)


def _emulator_block(members, evaluator, active, register):
    """Validate one independently screened block; failures use native receipts."""
    designs = candidate_designs(active, register, count=32, seed=SEED)
    # The same five-level Cartesian pool is frozen without reading targets.
    for point in product(
        *[
            sorted({register[n]["baseline"], *np.linspace(*register[n]["bounds"], 5)})
            for n in active
        ]
    ):
        settings = dict(zip(active, map(float, point), strict=True))
        designs[digest(settings)[:16]] = settings
    axis = anchors(active, register)
    panel = evaluator.batch(members, axis)
    matrix = design_matrix(list(axis.values()), active, register)
    coefficients, rejected = {}, {}
    for row in members:
        bid = row["building_id"]
        values = [panel[bid, n] for n in axis]
        if any(v is None for v in values):
            rejected[bid] = "anchor_failure"
        else:
            coefficients[bid] = np.linalg.lstsq(matrix, values, rcond=None)[0]
    joint = [(k, p) for k, p in designs.items() if len(p) > 1]
    checks = dict(joint[:: max(1, len(joint) // 4)][:4])
    checked = evaluator.batch(
        [r for r in members if r["building_id"] in coefficients], checks
    )
    errors = {}
    for row in members:
        bid = row["building_id"]
        if bid not in coefficients:
            continue
        actual = [checked[bid, n] for n in checks]
        if any(v is None for v in actual):
            rejected[bid] = "joint_check_failure"
            continue
        errors[bid] = max(
            [
                abs(
                    float(_vector(p, active, register) @ coefficients[bid])
                    - checked[bid, n]
                )
                / max(checked[bid, n], 1e-9)
                for n, p in checks.items()
            ]
            or [0]
        )
        if errors[bid] > 0.02:
            rejected[bid] = "joint_check_error_above_2_percent"
    fallback = evaluator.batch(
        [r for r in members if r["building_id"] in rejected], designs
    )
    estimates = {}
    for row in members:
        bid = row["building_id"]
        estimates[bid] = {
            n: fallback[bid, n]
            if bid in rejected
            else max(0.0, float(_vector(p, active, register) @ coefficients[bid]))
            for n, p in designs.items()
        }
    return {
        "active": active,
        "register": register,
        "designs": designs,
        "estimates": estimates,
        "coefficients": {
            bid: c.tolist() for bid, c in coefficients.items() if bid not in rejected
        },
        "native_fallback": rejected,
        "held_out_checks": checks,
        "maximum_validated_relative_error": max(
            [v for b, v in errors.items() if b not in rejected] or [0]
        ),
        "maximum_screened_relative_error": max(errors.values()) if errors else None,
    }


def emulators(root, evaluator, sensitivity):
    """Build independent era and pooled blocks without adding a fourth control."""
    from .mf_parent_emulator_blocks import emulator_blocks

    root = Path(root)
    path = root / "emulators.json"
    if path.exists():
        return read(path)
    rows = read(root / "features.json")
    result = {}
    operating = sensitivity["operating"]["active"]
    pooled = sensitivity.get("pooled_fit_controls", operating + ["window_u_multiplier"])
    eras = sorted({r["era"] for r in rows})
    for era in eras:
        members = [r for r in rows if r["era"] == era]
        register = _shared_register(members, evaluator)
        primary = [
            n
            for n in operating + sensitivity["fabric_by_era"][era]["active"]
            if n in register
        ]
        blocks = {}
        for name, active in emulator_blocks(
            primary, [n for n in pooled if n in register], register
        ).items():
            blocks[name] = _emulator_block(members, evaluator, active, register)
            atomic_json(
                root / "emulator-progress.json",
                {
                    "completed_eras": sorted(result),
                    "total_eras": len(eras),
                    "current_era": era,
                    "completed_blocks": list(blocks),
                },
            )
        result[era] = blocks.pop("primary") | {
            "fit_fabric_controls": sensitivity["fabric_by_era"][era]["active"],
            "pooled_fit_controls": pooled,
            "alternate_blocks": blocks,
        }
        atomic_json(
            root / "emulator-progress.json",
            {
                "completed_eras": sorted(result),
                "total_eras": len(eras),
            },
        )
    atomic_json(path, cleaned(result))
    return result


def _predict_estimate(row, settings, models):
    from .mf_parent_emulator_blocks import predict_estimate

    model = models[row["era"]]
    settings = dict(settings)
    # A control absent across this era may still be shared with other eras.
    # Remove it only with explicit metadata showing physical inapplicability.
    inactive = {
        "recovery": row.get("ventilation") in ("S", "F", "FT"),
        "flow_multiplier": row.get("ventilation") == "S",
        "basement_setpoint_c": row.get("heated_basement") == "no"
        or row.get("heated_basement_floors") == 0,
    }
    for name, inapplicable in inactive.items():
        if name not in model["register"] and inapplicable:
            settings.pop(name, None)
    return predict_estimate(row, settings, model)


def _plan_settings(row, plan):
    return plan.get("by_era", {}).get(row["era"], plan["pooled"])


def fit_parents(rows, forbidden, evaluator, models, *, scope, output):
    """Shared operating axis; construction fits span ventilation types within era."""
    from .mf_comparison import metrics

    if set(forbidden) & {r["property_key"] for r in rows}:
        raise ValueError("Held-out property requested by fitting stage")
    register = _shared_register(rows, evaluator)
    operating = [n for n in OPERATING if any(n in m["active"] for m in models.values())]
    op = operating[0] if operating else None
    op_values = (
        sorted({register[op]["baseline"], *np.linspace(*register[op]["bounds"], 5)})
        if op
        else [None]
    )
    pooled_controls = next(iter(models.values())).get(
        "pooled_fit_controls", operating + ["window_u_multiplier"]
    )
    windows = (
        sorted({1.0, *np.linspace(*register["window_u_multiplier"]["bounds"], 5)})
        if "window_u_multiplier" in pooled_controls
        else [1.0]
    )
    baseline_values = [_predict_estimate(r, {}, models) for r in rows]
    baseline = {
        "id": "baseline",
        "metrics": metrics(rows, baseline_values),
        "prior_distance": 0.0,
        "plan": {"pooled": {}, "by_era": {}},
    }
    candidates = [baseline]
    for operation in op_values:
        operating_settings = {op: operation} if op else {}
        pooled_candidates = []
        for window in windows:
            settings = operating_settings | {"window_u_multiplier": window}
            m = metrics(rows, [_predict_estimate(r, settings, models) for r in rows])
            pooled_candidates.append(
                {
                    "id": digest(settings),
                    "metrics": m,
                    "prior_distance": prior_distance(settings, register),
                    "parameters": settings,
                }
            )
        best_pool = select_candidate(
            pooled_candidates,
            {
                "id": "baseline",
                "metrics": baseline["metrics"],
                "parameters": {},
                "prior_distance": 0.0,
            },
        )
        pooled = best_pool["parameters"]
        # A pool fallback retains the same operating correction as this trial.
        if not pooled and operating_settings:
            pooled = operating_settings
        plan = {
            "pooled": pooled,
            "by_era": {},
            "scope": scope,
            "operating_shared": operating_settings,
        }
        if scope == "era":
            for era in sorted({r["era"] for r in rows}):
                members = [r for r in rows if r["era"] == era]
                if len({r["property_key"] for r in members}) < 30:
                    plan["by_era"][era] = pooled
                    continue
                model = models[era]
                options = []
                for name, settings in model["designs"].items():
                    if (
                        op
                        and settings.get(
                            op, model["register"].get(op, {}).get("baseline")
                        )
                        != operation
                    ):
                        continue
                    fitted_fabric = model.get(
                        "fit_fabric_controls",
                        [n for n in model["active"] if n in FABRIC],
                    )
                    if any(
                        n in FABRIC
                        and n not in fitted_fabric
                        and not math.isclose(
                            v,
                            model["register"][n]["baseline"],
                            rel_tol=1e-10,
                            abs_tol=1e-10,
                        )
                        for n, v in settings.items()
                    ):
                        continue
                    options.append(
                        {
                            "id": name,
                            "metrics": metrics(
                                members,
                                [
                                    _predict_estimate(r, settings, models)
                                    for r in members
                                ],
                            ),
                            "prior_distance": prior_distance(
                                settings, model["register"]
                            ),
                            "parameters": settings,
                        }
                    )
                local_base = {
                    "id": "pooled",
                    "metrics": metrics(
                        members, [_predict_estimate(r, pooled, models) for r in members]
                    ),
                    "prior_distance": prior_distance(pooled, register),
                    "parameters": pooled,
                }
                plan["by_era"][era] = select_candidate(options, local_base)[
                    "parameters"
                ]
        estimates = [
            _predict_estimate(r, _plan_settings(r, plan), models) for r in rows
        ]
        candidates.append(
            {
                "id": digest(plan),
                "metrics": metrics(rows, estimates),
                "prior_distance": sum(
                    prior_distance(_plan_settings(r, plan), register) for r in rows
                )
                / len(rows),
                "plan": plan,
            }
        )
    # Native confirmation precedes final training selection. Emulator results are
    # never exported as final predictions. At least three finalists plus baseline.
    acceptable = [
        c
        for c in candidates
        if c["metrics"]["mae_kwh_m2"] is not None
        and c["metrics"]["mae_kwh_m2"] <= baseline["metrics"]["mae_kwh_m2"]
    ]
    shortlist = sorted(
        acceptable,
        key=lambda c: (
            -c["metrics"]["coverage20"],
            abs(c["metrics"]["bias_kwh_m2"]),
            c["prior_distance"],
        ),
    )[:3]
    if baseline not in shortlist:
        shortlist.append(baseline)
    native = []
    for candidate in shortlist:
        settings = {
            _plan_settings(r, candidate["plan"]).__repr__(): _plan_settings(
                r, candidate["plan"]
            )
            for r in rows
        }
        predictions = {}
        for parameters in settings.values():
            members = [
                r for r in rows if _plan_settings(r, candidate["plan"]) == parameters
            ]
            panel = evaluator.batch(members, {"selected": parameters})
            predictions.update(
                {r["building_id"]: panel[r["building_id"], "selected"] for r in members}
            )
        native.append(
            candidate
            | {"metrics": metrics(rows, [predictions[r["building_id"]] for r in rows])}
        )
    winner = select_candidate(native, next(c for c in native if c["id"] == "baseline"))
    result = winner["plan"] | {
        "training_properties": sorted({r["property_key"] for r in rows}),
        "training_metrics": winner["metrics"],
        "criterion": "coverage_first_MAE_guard_bias_prior_ties",
        "native_finalists": [{"id": c["id"], "metrics": c["metrics"]} for c in native],
        "selected_id": winner["id"],
        "support_by_era": {
            era: len({r["property_key"] for r in rows if r["era"] == era})
            for era in sorted({r["era"] for r in rows})
        },
        "profile_basis": "effective_surviving_stock",
        "classification": "retrospectively_calibrated_assumption",
    }
    atomic_json(output, cleaned(result))
    return result


def predict_parent(rows, plan, evaluator):
    predictions = {}
    groups = defaultdict(list)
    for row in rows:
        groups[digest(_plan_settings(row, plan))].append(row)
    for members in groups.values():
        panel = evaluator.batch(members, {"selected": _plan_settings(members[0], plan)})
        predictions.update(
            {r["building_id"]: panel[r["building_id"], "selected"] for r in members}
        )
    return [predictions[r["building_id"]] for r in rows]


def nested_parents(root, evaluator, models):
    from .mf_comparison import metrics

    root = Path(root)
    if (root / "parent-oof.json").exists():
        return read(root / "parent-oof.json")
    rows, store = read(root / "features.json"), TargetStore(root)
    folds = property_folds(rows, 5, seed=SEED)
    predictions, audits = {}, []
    for outer in range(5):
        train = [r for r in rows if folds[r["property_key"]] != outer]
        test = [r for r in rows if folds[r["property_key"]] == outer]
        forbidden = {r["property_key"] for r in test}
        inner = property_folds(train, 3, seed=SEED + outer + 1)
        scored = {"pooled": ([], []), "era": ([], [])}
        folder = root / "fits" / f"outer-{outer}"
        for fold in range(3):
            fitting = [r for r in train if inner[r["property_key"]] != fold]
            validation = [r for r in train if inner[r["property_key"]] == fold]
            excluded = forbidden | {r["property_key"] for r in validation}
            observed = store.training(fitting, excluded)
            for scope in scored:
                path = folder / f"inner-{fold}-{scope}.json"
                fit = fit_parents(
                    observed, excluded, evaluator, models, scope=scope, output=path
                )
                values = predict_parent(validation, fit, evaluator)
                scored[scope][0].extend(store.evaluation(validation, path))
                scored[scope][1].extend(values)
        scores = {scope: metrics(*parts) for scope, parts in scored.items()}
        base = {"id": "pooled", "metrics": scores["pooled"], "prior_distance": 0.0}
        chosen = select_candidate(
            [{"id": "era", "metrics": scores["era"], "prior_distance": 1.0}], base
        )["id"]
        path = folder / "frozen.json"
        final = fit_parents(
            store.training(train, forbidden),
            forbidden,
            evaluator,
            models,
            scope=chosen,
            output=path,
        )
        values = predict_parent(test, final, evaluator)
        store.evaluation(test, path)
        predictions.update(
            {r["building_id"]: v for r, v in zip(test, values, strict=True)}
        )
        audits.append(
            {
                "outer_fold": outer,
                "inner_scores": scores,
                "selected_scope": chosen,
                "training_properties": sorted({r["property_key"] for r in train}),
                "evaluation_properties": sorted(forbidden),
                "frozen_profile": str(path),
            }
        )
        atomic_json(
            root / "parent-progress.json",
            {"completed_folds": outer + 1, "total_folds": 5},
        )
    result = {
        "predictions": predictions,
        "folds": audits,
        "confirmation": False,
        "evaluation": "nested_property_separated_retrospective",
    }
    atomic_json(root / "parent-oof.json", cleaned(result))
    return result


def seed_identical_history_baselines(root, evaluator, native, state_rows):
    """Reuse this campaign's parent baseline only for an identical history state.

    This transfers no previous history bank and no fitted parent performance.
    Historical SE02 windows therefore cannot inherit the 2.2 stock baseline.
    Native receipt identity, full physical hash and delivered carrier/area must
    agree before the independently specified history state receives a receipt.
    """
    root = Path(root)
    source_rows = {r["building_id"]: r for r in read(root / "features.json")}
    pinned, imported = {}, 0
    for state_row in state_rows.values():
        row = source_rows[state_row["source_building_id"]]
        source_key = evaluator.case_key(row, {})
        path = root / "simulations" / source_key / "receipt.json"
        if not path.exists():
            continue
        receipt = read(path)
        if receipt.get("status") != "accepted":
            continue
        state_spec = native.spec(state_row)
        if state_spec.physical_hash() != receipt.get("spec_sha256"):
            continue
        if receipt.get("identity") != source_key:
            raise ValueError("History baseline source receipt identity changed")
        scalar = receipt["scalars"].get("space_heating_carrier_input_kwh")
        if scalar is None or not math.isclose(
            scalar / state_row["atemp_m2"], receipt["prediction"], rel_tol=1e-10
        ):
            raise ValueError("History baseline source carrier/area differs")
        checksum = file_sha256(path)
        pinned[str(path.resolve())] = checksum
        _pin_receipt_chain(receipt, pinned)
        key = native.case_key(state_row, {})
        atomic_json(
            native.root / "simulations" / key / "spec.json",
            state_spec.model_dump(mode="json"),
        )
        atomic_json(
            native.root / "simulations" / key / "receipt.json",
            receipt
            | {
                "identity": key,
                "building_id": state_row["building_id"],
                "parameters": {},
                "requested_parameters": {},
                "native_transfer": {
                    "source": str(path.resolve()),
                    "sha256": checksum,
                    "basis": "This campaign's unfitted baseline; identical independently frozen history physical spec, carrier, Atemp, weather, engine and runtime",
                },
            },
        )
        imported += 1
    return {
        "imported": imported,
        "sources": pinned,
        "previous_dynamic_bank_used": False,
    }


def dynamic_panel(root, evaluator):
    """One hundred histories per property; all positive-weight states need native heat."""
    from .mf_stock_history import (
        engineering_prior,
        generate_histories,
        weighted_predictions,
    )

    root = Path(root)
    if (root / "dynamic-panel.json").exists():
        return read(root / "dynamic-panel.json")
    rows = read(root / "features.json")
    config = read(root / "config.json")
    controls = {
        f"life-{v:.3f}": {"lifetime_multiplier": v} for v in np.linspace(0.75, 1.25, 5)
    }
    # Unsupported roof/facade period mappings do not acquire thermal effect just
    # because histories contain refurbishment/covering events.
    mapping = config.get("history_upgrade_profiles", {})
    if mapping:
        raise ValueError(
            "Native roof/facade mapping application requires a reviewed installation-profile adapter"
        )
    histories, state_rows, sources = {}, {}, {}
    references = read(root / "original-construction-references.json")
    frozen_manifest = read(root / "manifest.json")
    for row in rows:
        bid = row["building_id"]
        source = frozen_original_spec(
            root, row, references=references, manifest=frozen_manifest
        )
        metadata = {
            k: row[k]
            for k in (
                "building_id",
                "construction_year",
                "measurement_year",
                "era",
                "catalogue_climate_zone",
                "ventilation",
            )
        }
        histories[bid] = {}
        sources[bid] = source
        for name, values in controls.items():
            history = generate_histories(
                metadata, values, draws=100, seed=SEED, upgrade_profiles=mapping
            )
            histories[bid][name] = history
            for state in history["states"]:
                thermal = state["state"]
                if thermal.get("wall_profile_id") not in (
                    None,
                    "original",
                ) or thermal.get("roof_profile_id") not in (None, "original"):
                    raise ValueError(
                        "Unsupported temporal upgrade escaped inactive mapping"
                    )
                u = (
                    thermal.get("window_u_value_w_m2k")
                    or source.facade.glazing.u_value_w_m2k
                )
                shgc = thermal.get(
                    "window_solar_heat_gain_coefficient",
                    source.facade.glazing.solar_heat_gain_coefficient,
                )
                vt = thermal.get(
                    "window_visible_transmittance",
                    source.facade.glazing.visible_transmittance,
                )
                key = digest((bid, float(u), shgc, vt))
                if key not in state_rows:
                    spec = source.updated(
                        facade=source.facade.updated(
                            glazing=source.facade.glazing.updated(
                                u_value_w_m2k=u,
                                solar_heat_gain_coefficient=shgc,
                                visible_transmittance=vt,
                            )
                        )
                    )
                    path = f"dynamic-specs/{key}.json"
                    atomic_json(root / path, spec.model_dump(mode="json"))
                    state_rows[key] = row | {
                        "building_id": key,
                        "source_building_id": bid,
                        "spec_path": path,
                    }
                    evaluator.registers[key] = applicable(
                        spec, locked=EXCLUDED, resistance_basis="total_assembly_r"
                    )
                state["native_key"] = key
        atomic_json(root / "histories" / f"{bid}.json", cleaned(histories[bid]))
    # Evaluator processes reload the register; keep dynamic state bank in a
    # separate frozen cohort_transfer campaign rather than mutate parent artifacts.
    bank = root / "dynamic-native"
    bank.mkdir(exist_ok=True)
    for row in state_rows.values():
        source_path = root / row["spec_path"]
        atomic_json(bank / row["spec_path"], read(source_path))
    atomic_json(bank / "config.json", config)
    atomic_json(
        bank / "parameter-register.json",
        {
            r["building_id"]: evaluator.registers[r["building_id"]]
            for r in state_rows.values()
        },
    )
    atomic_json(
        bank / "manifest.json",
        {
            "parent_manifest": digest(read(root / "manifest.json")),
            "state_spec_hashes": {
                k: file_sha256(root / r["spec_path"]) for k, r in state_rows.items()
            },
            "prior": engineering_prior(),
            "history_controls": controls,
            "original_parent_basis": True,
            "observed_heating_used": False,
        },
    )
    with Evaluator(bank) as native:
        atomic_json(
            bank / "identical-baseline-cache.json",
            seed_identical_history_baselines(root, evaluator, native, state_rows),
        )
        panel = native.batch(list(state_rows.values()), {"baseline": {}})
    predictions = {name: {} for name in controls}
    for row in rows:
        bid = row["building_id"]
        for name, history in histories[bid].items():
            values = {
                state["state_key"]: panel[state["native_key"], "baseline"]
                for state in history["states"]
            }
            aggregate = weighted_predictions(history["states"], values)
            predictions[name][bid] = aggregate.get("prediction")
    result = {
        "predictions": predictions,
        "controls": controls,
        "prior": engineering_prior(),
        "native_states": len(state_rows),
        "failed_native_states": sum(v is None for v in panel.values()),
        "history_draws": 100,
        "roof_facade_thermal_upgrades": "inactive_unsupported_installation_period_mapping",
        "identified_histories": False,
    }
    atomic_json(root / "dynamic-panel.json", cleaned(result))
    return result


def nested_dynamic(root, panel):
    from .mf_comparison import metrics

    root = Path(root)
    rows, store = read(root / "features.json"), TargetStore(root)
    folds = property_folds(rows, 5, seed=SEED)
    predictions, audit = {}, []
    for fold in range(5):
        train = [r for r in rows if folds[r["property_key"]] != fold]
        test = [r for r in rows if folds[r["property_key"]] == fold]
        observed = store.training(train, {r["property_key"] for r in test})
        candidates = [
            {
                "id": name,
                "metrics": metrics(observed, [values[r["building_id"]] for r in train]),
                "prior_distance": (panel["controls"][name]["lifetime_multiplier"] - 1.0)
                ** 2,
            }
            for name, values in panel["predictions"].items()
        ]
        base = next(c for c in candidates if c["id"] == "life-1.000")
        selected = select_candidate(candidates, base)["id"]
        path = root / "fits" / f"dynamic-{fold}.json"
        fit = {
            "controls": panel["controls"][selected],
            "selected": selected,
            "training_properties": sorted({r["property_key"] for r in train}),
            "evaluation_properties": sorted({r["property_key"] for r in test}),
            "inactive_controls": [
                "roof_upgrade_probability",
                "facade_upgrade_probability",
            ],
        }
        atomic_json(path, fit)
        predictions.update(
            {
                r["building_id"]: panel["predictions"][selected][r["building_id"]]
                for r in test
            }
        )
        store.evaluation(test, path)
        audit.append(fit)
    result = {
        "predictions": predictions,
        "folds": audit,
        "classification": "experimental_history_priors_not_verified_renovation",
    }
    atomic_json(root / "dynamic-oof.json", result)
    return result


def assignment(row, profiles):
    """A forward assignment does not accept an energy target or residual."""
    from .mf_stock_history import _validate_target_free

    _validate_target_free(row)
    era = row.get("era")
    if not era and row.get("construction_year"):
        from epsm_data.parametric import era_for_year

        era = era_for_year(int(row["construction_year"]))
    selected = profiles.get("by_era", {}).get(era, profiles["pooled"])
    support = profiles.get("support_by_era", {}).get(era, 0)
    selected = dict(selected)
    inactive = {}
    topology = str(row.get("ventilation", "")).upper()
    floors = row.get("heated_basement_floors")
    basement = row.get("heated_basement")
    conditioned = (
        floors is not None
        and float(floors) > 0
        and str(basement).lower() not in ("no", "false", "0")
    )
    for name in list(selected):
        reason = None
        if name == "recovery" and topology != "FTX":
            reason = "Heat recovery requires FTX"
        elif name == "flow_multiplier" and topology == "S":
            reason = "Mechanical flow correction requires mechanical ventilation"
        elif name == "basement_setpoint_c" and not conditioned:
            reason = "No reliably reported conditioned basement"
        elif name in row.get("locked_parameters", ()) or (
            name == "wall_insulation_overlay_m"
            and "wall_resistance" in row.get("locked_parameters", ())
        ):
            reason = "Independently evidenced component remains locked"
        if reason:
            inactive[name] = {"value": selected.pop(name), "reason": reason}
    return {
        "parent": f"MF:{era or 'pooled'}",
        "parameters": selected,
        "inactive_parameters": inactive,
        "profile": {
            "parameters": selected,
            "reference_basis": "catalogue_existing_stock_with_fixed_normal_use_and_corrected_shading",
            "solar_shading_factor": 0.5,
            "normal_use_basis": BEN_HOUSEHOLD_BASIS,
            "fixed_normal_use_reference": profiles.get(
                "fixed_normal_use_reference", household_reference_provenance()
            ),
            "resistance_basis": "total_assembly_r",
            "bounds": profiles.get("bounds_by_era", {}).get(
                era, profiles.get("pooled_bounds", {})
            ),
            "wall_overlay": profiles.get("wall_overlay"),
            "infiltration_basis": "atemp_area_rate",
            "source": "Retrospective shared MF effective parent; inferred assumptions, not measured component condition",
        },
        "profile_status": "adopted_retrospective"
        if profiles.get("adoption_status", {}).get("adopt")
        else "candidate_not_adopted",
        "profile_basis": "effective_surviving_stock",
        "training_support": support,
        "confidence": "supported_retrospective"
        if support >= 30
        else "pooled_sparse_support",
        "measurement_year_applicability": profiles.get("measurement_years", []),
        "carrier_validation": "district_heat_only",
        "renovation_status": "unidentified",
        "optional_renovation_fields_required": False,
        "confirmation": False,
    }


def _assignment_descriptors(row):
    """Whitelist forward descriptors before constructing any component histories."""
    fields = (
        "building_id",
        "construction_year",
        "era",
        "ventilation",
        "catalogue_climate_zone",
        "measurement_year",
        "heated_basement",
        "heated_basement_floors",
        "locked_parameters",
        "heating_family",
        "component_evidence",
        "parent_basis",
    )
    return {name: row[name] for name in fields if name in row}


def _adoption_summary(status):
    return {
        "adopt": bool(status.get("adopt")),
        "reasons": list(status.get("reasons", [])),
        "independent_confirmation": False,
    }


def _catalogue_reference(row, *, current=False):
    """A future building needs no pre-existing study spec or history file."""
    from epsm_data import __version__

    era = row.get("era")
    if not era and row.get("construction_year"):
        from epsm_data.parametric import era_for_year

        era = era_for_year(int(row["construction_year"]))
    from .mf_stock_history import original_window_profile

    historical_window = (
        None
        if current or row.get("construction_year") is None
        else original_window_profile(row["construction_year"])
    )
    return {
        "kind": "catalogue_current_stock_reference"
        if current
        else "catalogue_original_construction_reference",
        "source": f"EPSM Data {__version__} current MF stock library"
        if current
        else f"EPSM Data {__version__} historical MF references with explicit component source guards",
        "historical_window_profile": historical_window,
        "construction_year": row.get("construction_year"),
        "era": era,
        "catalogue_climate_zone": row.get("catalogue_climate_zone"),
        "ventilation": row.get("ventilation"),
        "solar_shading_factor": 1.0 if current else 0.5,
        "normal_use_basis": SVEBY_HOUSEHOLD_BASIS if current else BEN_HOUSEHOLD_BASIS,
        "useful_household_gain_kwh_m2_year": 14.7 if current else 21.0,
        "requires_building_geometry_and_declared_scenario_weather": True,
        "measured_component_condition": False,
    }


def dynamic_assignment(row, priors, *, original_reference=None):
    """Apply frozen shared controls to metadata-only, identity-seeded histories."""
    from .mf_stock_history import _validate_target_free, generate_histories

    _validate_target_free(row)
    metadata = _assignment_descriptors(row)
    if not metadata.get("era") and metadata.get("construction_year"):
        from epsm_data.parametric import era_for_year

        metadata["era"] = era_for_year(int(metadata["construction_year"]))
    if (
        original_reference
        and original_reference.get("basis")
        and not original_reference["basis"].startswith("original_construction")
    ):
        raise ValueError(
            "Dynamic assignment requires an original construction reference"
        )
    missing = [
        name
        for name in ("building_id", "construction_year", "measurement_year")
        if metadata.get(name) is None
    ]
    history = (
        None
        if missing
        else generate_histories(
            metadata,
            priors["controls"],
            draws=100,
            seed=SEED,
            upgrade_profiles={},
            component_priors=priors["components"],
        )
    )
    years = priors.get("measurement_years", [])
    within_years = metadata.get("measurement_year") in years
    status = _adoption_summary(priors.get("adoption_status", {}))
    return {
        "model": "dynamic_histories",
        "parent": f"MF:{metadata.get('era') or 'original_construction'}",
        "profile_basis": "original_construction",
        "original_reference": original_reference or _catalogue_reference(metadata),
        "controls": dict(priors["controls"]),
        "selected_configuration": priors.get("selected_configuration"),
        "state_distribution": []
        if history is None
        else [
            {name: state[name] for name in ("state_key", "weight", "state")}
            for state in history["states"]
        ],
        "state_distribution_status": "metadata_incomplete"
        if missing
        else "target_free_generated",
        "required_metadata_missing": missing,
        "history_draws": 100,
        "provenance": {
            "prior": {
                name: priors[name]
                for name in (
                    "schema_version",
                    "classification",
                    "status",
                    "components",
                    "bounds",
                    "thermal_material_degradation",
                    "lighting_changes",
                    "energy_conditioning",
                )
                if name in priors
            },
            "generation": None if history is None else history["provenance"],
            "seed": SEED,
            "identity": metadata.get("building_id"),
            "component_history_file_required": False,
            "measured_heating_used_for_history_generation": False,
            "shared_controls_fit_on_training_properties": True,
        },
        "profile_status": "adopted_retrospective"
        if status["adopt"]
        else "candidate_not_adopted",
        "adoption_status": status,
        "training_support": priors.get("training_properties", 0),
        "confidence": "metadata_incomplete"
        if missing
        else "supported_retrospective_distribution"
        if within_years and status["adopt"]
        else "experimental_or_extrapolated_distribution",
        "measurement_year_applicability": years,
        "measurement_year_within_development_support": within_years,
        "carrier_validation": "district_heat_only",
        "renovation_status": "unidentified",
        "optional_renovation_fields_required": False,
        "confirmation": False,
    }


def selected_assignment(
    row, profiles, priors, *, original_reference=None, current_reference=None
):
    """Select retained current, adopted parent or adopted original-reference history."""
    from .mf_stock_history import _validate_target_free

    _validate_target_free(row)
    parent_status = _adoption_summary(profiles.get("adoption_status", {}))
    dynamic_status = _adoption_summary(priors.get("adoption_status", {}))
    parent = assignment(row, profiles)
    if not parent_status["adopt"]:
        selected = parent | {
            "model": "current_assumptions",
            "parameters": {},
            "inactive_parameters": {},
            "profile": {
                "parameters": {},
                "reference": current_reference
                or _catalogue_reference(row, current=True),
                "source": "Retained exact current comparison assumptions",
                "solar_shading_factor": 1.0,
                "normal_use_basis": SVEBY_HOUSEHOLD_BASIS,
            },
            "profile_status": "retained_current_assumptions",
            "profile_basis": "current_assumptions",
            "confidence": "current_catalogue_assumption",
        }
    elif dynamic_status["adopt"]:
        selected = dynamic_assignment(
            row, priors, original_reference=original_reference
        )
    else:
        selected = parent | {
            "model": "calibrated_parent",
            "original_reference": original_reference or _catalogue_reference(row),
        }
    selected["selection"] = {
        "parent_adoption": parent_status,
        "dynamic_adoption": dynamic_status,
        "dynamic_requires_both_adoption_gates": True,
        "full_development_fit_is_not_accuracy_evidence": True,
    }
    return selected


def _current_assignment_receipts(root, rows):
    """Index explicit current-branch native receipts, including pinned ancestors."""
    ids = {row["building_id"] for row in rows}
    paths = set((root / "current-native" / "simulations").glob("*/receipt.json"))
    manifest = read(root / "manifest.json")
    for name, checksum in manifest.get("sources", {}).items():
        path = Path(name)
        if path.name != "receipt.json":
            continue
        if file_sha256(path) != checksum:
            raise ValueError("Pinned current receipt changed before assignment export")
        paths.add(path)
    indexed = defaultdict(list)
    for path in sorted(paths):
        receipt = read(path)
        if receipt.get("building_id") not in ids or receipt.get("status") != "accepted":
            continue
        is_legacy_current = (
            "physical_hash" in receipt
            and receipt.get("scenario", "baseline") == "baseline"
        )
        is_current_bank = "current-native" in path.parts and not receipt.get(
            "requested_parameters", receipt.get("parameters", {})
        )
        if is_legacy_current or is_current_bank:
            indexed[receipt["building_id"]].append((path, receipt))
    return indexed


def export_assignments(root, rows, profiles, priors):
    """Export selected assumptions separately from independently reviewable candidates."""
    from epsm_core import BuildingSpec

    from .mf_stock_history import _validate_target_free

    root = Path(root).resolve()
    references = read(root / "original-construction-references.json")
    manifest = read(root / "manifest.json")
    current_receipts = _current_assignment_receipts(root, rows)
    selected, parent_candidates, dynamic_candidates = {}, {}, {}
    for row in rows:
        _validate_target_free(row)
        bid = row["building_id"]
        reference = references[bid]
        if (
            row.get("original_reference_spec_path", reference["spec_path"])
            != reference["spec_path"]
        ):
            raise ValueError("Original registry disagrees with the dynamic source spec")
        source_path = root / reference["spec_path"]
        if file_sha256(source_path) != manifest["artifacts"][reference["spec_path"]]:
            raise ValueError("Frozen original-reference spec changed")
        source = frozen_original_spec(
            root, row, references=references, manifest=manifest
        )
        original = {
            "kind": "frozen_original_construction_reference",
            "registry_path": str(root / "original-construction-references.json"),
            "registry_key": bid,
            "spec_path": str(source_path),
            "file_sha256": file_sha256(source_path),
            "physical_hash": source.physical_hash(),
            "basis": reference["basis"],
            "component_basis": reference.get("component_basis"),
            "matches_dynamic_panel_source_spec": True,
            "normal_use_basis": row.get("normal_use_basis", SVEBY_HOUSEHOLD_BASIS),
        }
        parent_path = root / row["spec_path"]
        if file_sha256(parent_path) != manifest["artifacts"][row["spec_path"]]:
            raise ValueError("Frozen parent-baseline spec changed")
        parent_source = BuildingSpec.model_validate(read(parent_path))
        parent_reference = {
            "kind": "frozen_catalogue_parent_baseline",
            "spec_path": str(parent_path),
            "file_sha256": file_sha256(parent_path),
            "physical_hash": parent_source.physical_hash(),
            "basis": "catalogue_existing_stock_with_fixed_normal_use_and_corrected_shading",
            "normal_use_basis": row.get("normal_use_basis", SVEBY_HOUSEHOLD_BASIS),
        }
        current_spec = current_assumptions_spec(parent_source, row)
        verified = []
        for receipt_path, receipt in current_receipts.get(bid, []):
            if (
                receipt.get("spec_sha256", receipt.get("physical_hash"))
                != current_spec.physical_hash()
            ):
                raise ValueError(
                    "Recreated current assumptions disagree with pinned current receipt"
                )
            receipt_spec = receipt_path.parent / "spec.json"
            if (
                receipt_spec.is_file()
                and BuildingSpec.model_validate(read(receipt_spec)).physical_hash()
                != current_spec.physical_hash()
            ):
                raise ValueError(
                    "Pinned current receipt spec disagrees with recreated current assumptions"
                )
            verified.append(
                {"path": str(receipt_path), "sha256": file_sha256(receipt_path)}
            )
        current_path = root / "current-assignment-specs" / f"{bid}.json"
        atomic_json(current_path, current_spec.model_dump(mode="json"))
        current = {
            "kind": "frozen_current_comparison_reference",
            "spec_path": str(current_path),
            "file_sha256": file_sha256(current_path),
            "physical_hash": current_spec.physical_hash(),
            "source_parent_baseline_reference": parent_reference,
            "declared_preparation_difference": {
                "solar_shading_factor": 1.0,
                "normal_use_basis": SVEBY_HOUSEHOLD_BASIS,
            },
            "verified_current_receipts": verified,
            "receipt_verification_status": "verified"
            if verified
            else "no_accepted_current_receipt_available",
        }
        metadata = _assignment_descriptors(row)
        parent_candidates[bid] = assignment(metadata, profiles) | {
            "model": "calibrated_parent",
            "original_reference": original,
            "parent_baseline_reference": parent_reference,
        }
        parent_candidates[bid]["profile"]["reference"] = parent_reference
        dynamic_candidates[bid] = dynamic_assignment(
            metadata, priors, original_reference=original
        )
        selected[bid] = selected_assignment(
            metadata,
            profiles,
            priors,
            original_reference=original,
            current_reference=current,
        )
        if selected[bid]["model"] == "calibrated_parent":
            selected[bid]["parent_baseline_reference"] = parent_reference
            selected[bid]["profile"]["reference"] = parent_reference
    for name, values in (
        ("assignments.json", selected),
        ("candidate-calibrated-parent-assignments.json", parent_candidates),
        ("candidate-dynamic-assignments.json", dynamic_candidates),
    ):
        atomic_json(root / name, cleaned(values))
    return {
        "selected": selected,
        "calibrated_parent_candidates": parent_candidates,
        "dynamic_candidates": dynamic_candidates,
    }


def run(root, *, stages=("baseline", "parents", "dynamic", "report")):
    """Complete restartable stages; receipts preserve every preparation/run failure."""
    root = Path(root).resolve()
    verify(root)
    rows = read(root / "features.json")
    with Evaluator(root) as evaluator:
        if "baseline" in stages:
            panel = evaluator.batch(rows, {"baseline": {}})
            atomic_json(
                root / "corrected-native-predictions.json",
                {r["building_id"]: panel[r["building_id"], "baseline"] for r in rows},
            )
            # Hold current catalogue use separate from the fixed BEN household
            # reference. The extra shading-only panel isolates the two repairs.
            for bank_name, imports, panel_name, shading in (
                (
                    "current-native",
                    "current-native-predictions.json",
                    "current-panel.json",
                    1.0,
                ),
                (
                    "shading-native",
                    "shading-native-predictions.json",
                    "shading-panel.json",
                    0.5,
                ),
            ):
                reference_values = read(root / imports)
                missing = [r for r in rows if r["building_id"] not in reference_values]
                if missing:
                    bank = root / bank_name
                    for row in missing:
                        spec = current_assumptions_spec(
                            evaluator.spec(row), row, shading=shading
                        )
                        atomic_json(
                            bank / row["spec_path"], spec.model_dump(mode="json")
                        )
                    atomic_json(bank / "config.json", evaluator.config)
                    atomic_json(
                        bank / "parameter-register.json",
                        {
                            r["building_id"]: evaluator.registers[r["building_id"]]
                            for r in missing
                        },
                    )
                    atomic_json(
                        bank / "manifest.json",
                        {
                            "parent": digest(read(root / "manifest.json")),
                            "basis": SVEBY_HOUSEHOLD_BASIS,
                            "solar_shading_factor": shading,
                            "specs": {
                                r["building_id"]: file_sha256(bank / r["spec_path"])
                                for r in missing
                            },
                        },
                    )
                    with Evaluator(bank) as native:
                        values = native.batch(missing, {"baseline": {}})
                    reference_values.update(
                        {
                            r["building_id"]: values[r["building_id"], "baseline"]
                            for r in missing
                        }
                    )
                atomic_json(root / panel_name, reference_values)
        if "parents" in stages:
            sensitivity = screen(root, evaluator)
            models = emulators(root, evaluator, sensitivity)
            nested_parents(root, evaluator, models)
            from .mf_parent_subdivision import evaluate_subdivisions

            evaluate_subdivisions(root, evaluator, models)
        if "dynamic" in stages:
            nested_dynamic(root, dynamic_panel(root, evaluator))
        if "report" in stages:
            return report(root, evaluator)
    return {"root": str(root), "stages_completed": list(stages)}


def report(root, evaluator):
    from .mf_comparison import (
        adoption_gate,
        empirical_benchmarks,
        metrics,
        paired_change,
    )

    rows = read(root / "features.json")
    store = TargetStore(root)
    marker = root / "evaluation-frozen.json"
    atomic_json(
        marker,
        {
            "parent": digest(read(root / "parent-oof.json")),
            "dynamic": digest(read(root / "dynamic-oof.json")),
        },
    )
    observed = store.evaluation(rows, marker)
    methods = {
        "current": read(root / "current-panel.json"),
        "shading_only": read(root / "shading-panel.json"),
        "corrected_preparation": read(root / "corrected-native-predictions.json"),
        "calibrated_parents": read(root / "parent-oof.json")["predictions"],
        "dynamic_frozen": read(root / "dynamic-panel.json")["predictions"][
            "life-1.000"
        ],
        "dynamic_learned": read(root / "dynamic-oof.json")["predictions"],
    }
    comparisons = {
        name: metrics(observed, [values[r["building_id"]] for r in rows])
        for name, values in methods.items()
    }
    child_results = read(root / "subdivision-oof.json")
    comparisons["physical_subdivision_candidate"] = metrics(
        observed, child_results["predictions"]
    )
    gates = {
        "parent": adoption_gate(
            observed,
            [methods["current"][r["building_id"]] for r in rows],
            [methods["calibrated_parents"][r["building_id"]] for r in rows],
            kind="parent",
        ),
        "dynamic": adoption_gate(
            observed,
            [methods["calibrated_parents"][r["building_id"]] for r in rows],
            [methods["dynamic_learned"][r["building_id"]] for r in rows],
            kind="dynamic",
        ),
    }
    benchmarks = empirical_benchmarks(
        rows, {r["building_id"]: r["target"] for r in observed}, seed=SEED
    )
    atomic_json(root / "benchmarks.json", cleaned(benchmarks))
    for name, values in benchmarks["predictions"].items():
        comparisons[name] = metrics(observed, [values[r["building_id"]] for r in rows])
        methods[name] = values
    methods["physical_subdivision_candidate"] = child_results["predictions"]
    information_comparisons = {
        label: paired_change(
            observed,
            [methods[base][r["building_id"]] for r in rows],
            [methods[candidate][r["building_id"]] for r in rows],
        )
        for label, base, candidate in (
            ("residential_shading_repair", "current", "shading_only"),
            (
                "fixed_household_reference_alignment",
                "shading_only",
                "corrected_preparation",
            ),
            (
                "preparation_and_shared_physical_assumptions",
                "current",
                "calibrated_parents",
            ),
            (
                "shared_physical_assumptions_after_preparation",
                "corrected_preparation",
                "calibrated_parents",
            ),
            (
                "reliable_features_beyond_era_region",
                "empirical_parent",
                "reliable_features",
            ),
            (
                "physical_subdivision_after_parents",
                "calibrated_parents",
                "physical_subdivision_candidate",
            ),
            (
                "probabilistic_component_histories",
                "calibrated_parents",
                "dynamic_learned",
            ),
        )
    }
    for name, values in methods.items():
        comparisons[name]["coverage20_95_interval"] = _coverage_interval(
            observed, values
        )
    subdivision = subdivision_support(rows)
    atomic_json(root / "subdivision-support.json", subdivision)
    # Full-development profiles are exports for later target-free application;
    # their training fit is never used as evidence of predictive accuracy.
    models = read(root / "emulators.json")
    frozen = root / "calibrated-parent-profiles.json"
    audit = read(root / "parent-oof.json")
    pooled_scores = {}
    for scope in ("pooled", "era"):
        scores = [fold["inner_scores"][scope] for fold in audit["folds"]]
        total = sum(s["attempted"] for s in scores)
        pooled_scores[scope] = {
            key: sum(s[key] * s["attempted"] for s in scores) / total
            for key in ("coverage20", "mae_kwh_m2", "bias_kwh_m2")
        }
    deployment_scope = select_candidate(
        [{"id": "era", "metrics": pooled_scores["era"], "prior_distance": 1.0}],
        {"id": "pooled", "metrics": pooled_scores["pooled"], "prior_distance": 0.0},
    )["id"]
    profiles = fit_parents(
        observed, (), evaluator, models, scope=deployment_scope, output=frozen
    )
    profiles["scope_selection"] = {
        "basis": "pooled_nested_inner_validation",
        "scores": pooled_scores,
    }
    profiles["measurement_years"] = sorted({r["measurement_year"] for r in rows})
    profiles["adoption_status"] = gates["parent"]
    profiles["bounds_by_era"] = {
        era: {n: v["bounds"] for n, v in model["register"].items()}
        for era, model in models.items()
    }
    profiles["pooled_bounds"] = {
        n: v["bounds"] for n, v in _shared_register(rows, evaluator).items()
    }
    profiles["wall_overlay"] = evaluator.config["wall_overlay"]
    profiles["resistance_basis"] = "total_assembly_r"
    profiles["fixed_normal_use_reference"] = household_reference_provenance()
    atomic_json(frozen, cleaned(profiles))
    dynamic_values = read(root / "dynamic-panel.json")
    candidates = [
        {
            "id": name,
            "metrics": metrics(observed, values),
            "prior_distance": (
                dynamic_values["controls"][name]["lifetime_multiplier"] - 1.0
            )
            ** 2,
        }
        for name, values in dynamic_values["predictions"].items()
    ]
    best_history = select_candidate(
        candidates, next(c for c in candidates if c["id"] == "life-1.000")
    )["id"]
    atomic_json(
        root / "dynamic-priors.json",
        dynamic_values["prior"]
        | {
            "controls": dynamic_values["prior"]["controls"]
            | dynamic_values["controls"][best_history],
            "adoption_status": gates["dynamic"],
            "training_properties": len({r["property_key"] for r in rows}),
            "measurement_years": profiles["measurement_years"],
            "construction_basis": "original_construction",
            "component_history_directory": "histories",
            "selected_configuration": best_history,
            "inactive_upgrade_mappings": ["roof", "facade"],
            "identified_renovation_histories": False,
            "fixed_normal_use_reference": household_reference_provenance(),
        },
    )
    export_assignments(root, rows, profiles, read(root / "dynamic-priors.json"))
    result = {
        "schema_version": 1,
        "evaluation": "retrospective_development_only",
        "confirmation": False,
        "fixed_normal_use_reference": household_reference_provenance(),
        "panel": read(root / "preparation-summary.json"),
        "metrics": comparisons,
        "adoption": gates,
        "benchmarks": benchmarks,
        "physical_subdivision": child_results,
        "dynamic_limit": "Window installation-era proxy only; unsupported roof/facade thermal upgrade mappings remain inactive; histories unverified",
        "additional_information": {
            "comparisons": information_comparisons,
            "interpretation": "Property-isolated retrospective predictive differences; histories are not verified renovation, statistical benchmarks are not native accuracy",
            "optional_condition_fields_required": False,
        },
        "carriers": {
            "district_heat": "development_evaluated",
            "other": "not_validated",
        },
    }
    atomic_json(root / "report.json", cleaned(result))
    lines = [
        "# MF parents and dynamic component histories",
        "",
        "Retrospective development evaluation. No independent confirmation claim.",
        "",
        f"Frozen properties: {len(rows)} of {result['panel']['selected']} selected. Preparation failures and missing weather remain recorded.",
        "",
        "| Model | ±20% coverage [95% interval] | MAE (kWh/m²) | Bias (kWh/m²) | Error SD (kWh/m²) | Failures |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, m in comparisons.items():
        lo, hi = m["coverage20_95_interval"]
        lines.append(
            f"| {name} | {100 * m['coverage20']:.1f}% [{100 * lo:.1f}, {100 * hi:.1f}] | {_metric_text(m['mae_kwh_m2'])} | {_metric_text(m['bias_kwh_m2'])} | {_metric_text(m['error_std_kwh_m2'])} | {m['failed']} |"
        )
    for name, gate in gates.items():
        change = gate["comparison"]
        lo, hi = change["coverage_change_95_interval"]
        lines.extend(
            [
                "",
                f"**{name} adoption: {'passes' if gate['adopt'] else 'does not pass'}.** Coverage change {100 * change['coverage_change']:+.1f} points, paired 95% interval [{100 * lo:+.1f}, {100 * hi:+.1f}]. Reasons: {', '.join(gate['reasons']) or 'all required gates passed'}.",
            ]
        )
    lines.extend(
        [
            "",
            "| Cohort | Properties | Current coverage | Parent coverage | Parent MAE | Parent bias | Parent error SD | Dynamic coverage | Protection |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    dynamic_groups = {
        (g["field"], g["group"]): g for g in gates["dynamic"]["group_protections"]
    }
    for group in gates["parent"]["group_protections"]:
        score = group["candidate"]
        dyn = dynamic_groups[group["field"], group["group"]]["candidate"]
        protection = (
            "passes" if group["passed"] else "fails" if group["supported"] else "sparse"
        )
        lines.append(
            f"| {group['field']}:{group['group']} | {group['independent_properties']} | {100 * group['baseline']['coverage20']:.1f}% | {100 * score['coverage20']:.1f}% | {_metric_text(score['mae_kwh_m2'])} | {_metric_text(score['bias_kwh_m2'])} | {_metric_text(score['error_std_kwh_m2'])} | {100 * dyn['coverage20']:.1f}% | {protection} |"
        )
    lines.extend(
        [
            "",
            "| Additional assumption/information | Coverage change [paired 95% interval], points | MAE change (kWh/m²) |",
            "|---|---:|---:|",
        ]
    )
    for label, change in information_comparisons.items():
        lo, hi = change["coverage_change_95_interval"]
        lines.append(
            f"| {label} | {100 * change['coverage_change']:+.1f} [{100 * lo:+.1f}, {100 * hi:+.1f}] | {_metric_text(change['paired_mae_change_kwh_m2'])} |"
        )
    lines.extend(
        [
            "",
            "Parent adoption and dynamic adoption gates, paired property confidence intervals and protected cohort results are in report.json.",
            "",
            result["dynamic_limit"],
            "",
            "Original construction references and calibrated surviving-stock parents are exported separately. No inferred history is presented as verified renovation.",
            "",
            "Normal dwelling temperatures, occupancy and gains, geometry, Atemp and measurement-year weather were fixed. The residential shading omission was repaired before fitting. SE.01 overlays preserve structural layers and are explicitly engineering alternatives.",
            "The fixed principal household reference is BEN 30 kWh/m² electricity with 70% useful heat (21 kWh/m² annual gain), retaining the declared Sveby 2.0 timing proxy. Current and shading-only baselines retain current catalogue gains (14.7 kWh/m²). This sourced reference repair is reported separately from calibration; individual EPC adjustment completeness is unverified.",
            "",
            "Repeated-certificate renovation evidence is reported separately in the rebuilt archive EDA; optional flags do not establish renovation truth.",
        ]
    )
    (root / "report.md").write_text("\n".join(lines) + "\n")
    return result


def _metric_text(value):
    return (
        "unavailable" if value is None or not math.isfinite(value) else f"{value:.2f}"
    )


def _coverage_interval(rows, predictions, resamples=1000):
    """Property bootstrap for inclusive coverage, including native failures."""
    groups = defaultdict(list)
    for row in rows:
        value = predictions.get(row["building_id"])
        inside = (
            value is not None
            and math.isfinite(value)
            and abs(value - row["target"]) <= 0.2 * row["target"]
        )
        groups[row["property_key"]].append(float(inside))
    keys = sorted(groups)
    rng = np.random.default_rng(SEED)
    draws = [
        float(
            np.mean(
                [
                    v
                    for prop in rng.choice(keys, len(keys), replace=True)
                    for v in groups[prop]
                ]
            )
        )
        for _ in range(resamples)
    ]
    return np.quantile(draws, [0.025, 0.975]).tolist()


def subdivision_support(rows):
    """A child needs 30 fitting properties in every nested training split."""
    folds = property_folds(rows, 5, seed=SEED)
    support = defaultdict(list)
    for outer in range(5):
        train = [r for r in rows if folds[r["property_key"]] != outer]
        inner = property_folds(train, 3, seed=SEED + outer + 1)
        for fold in range(3):
            for era in sorted({r["era"] for r in rows}):
                support[era].append(
                    len(
                        {
                            r["property_key"]
                            for r in train
                            if r["era"] == era and inner[r["property_key"]] != fold
                        }
                    )
                )
    return {
        "stage": "after_parent_fitting",
        "minimum_child_training_properties": 30,
        "minimum_ari": 0.7,
        "minimum_validation_mae_gain": 0.05,
        "coverage_must_be_preserved": True,
        "parents": {
            era: {
                "minimum_nested_training_properties": min(counts),
                "two_children_development_supported": min(counts) >= 60,
                "status": "requires_physical_subdivision_fit"
                if min(counts) >= 60
                else "pooled_parent_retained_insufficient_child_support",
            }
            for era, counts in support.items()
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--selection", type=Path, required=True)
    p.add_argument("--existing", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--weather", type=Path)
    p.add_argument("--reuse-native", type=Path)
    p.add_argument("--scope-evidence", type=Path, required=True)
    p = sub.add_parser("run")
    p.add_argument("study", type=Path)
    p.add_argument(
        "--stages",
        nargs="+",
        choices=("baseline", "parents", "dynamic", "report"),
        default=["baseline", "parents", "dynamic", "report"],
    )
    args = parser.parse_args(argv)
    if args.command == "prepare":
        result = prepare(
            args.selection,
            args.existing,
            args.output,
            weather=args.weather,
            reuse_native=args.reuse_native,
            scope_evidence=args.scope_evidence,
        )
    else:
        result = run(args.study, stages=args.stages)
    import json

    print(json.dumps(cleaned(result), indent=2))


if __name__ == "__main__":
    main()
