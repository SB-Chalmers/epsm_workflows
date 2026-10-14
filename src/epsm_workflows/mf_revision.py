"""Evidence-led MF revision: inventory, target-free sampling, strict physics and gates.

The historical national stock has already been evaluated. Repartitioning it does
not manufacture a fresh holdout. Defaults remain provisional until independently
justified changes pass frozen validation and final-test comparisons.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

from .campaign import atomic_json, file_sha256
from .cohort_transfer.common import cleaned, digest, metrics, read
from .mf_evidence import insulation_candidates
from .mf_physics import exact_resistance

ERAS = tuple(f"SE.{i:02}" for i in range(1, 7))
ZONES = ("SE.Zone1", "SE.Zone2", "SE.Zone3")
SYSTEMS = ("S", "F", "FT", "FTX")
STATES = ("existing", "standard", "advanced")
SEED = 20261008


def zone_for_municipality(municipality):
    """Historical BBR18 grouping, independent of weather year and Fgeo."""
    county = str(municipality).zfill(4)[:2]
    if county in {"23", "24", "25"}:
        return "SE.Zone1"
    if county in {"17", "20", "21", "22"}:
        return "SE.Zone2"
    return "SE.Zone3"


def _path(config_path, value):
    p = Path(value)
    return p.resolve() if p.is_absolute() else (Path(config_path).parent / p).resolve()


def _new(root):
    root = Path(root).resolve()
    if root.exists():
        raise FileExistsError(f"Refusing to overwrite study: {root}")
    return root


def _identity():
    from .archetype_application import _implementations

    identity = _implementations()
    for p in sorted(Path(__file__).parent.rglob("*.py")):
        identity[str(p.resolve())] = file_sha256(p)
    return identity


def _guard(root):
    root = Path(root).resolve()
    manifest = read(root / "manifest.json")
    for p, expected in manifest["sources"].items():
        if file_sha256(p) != expected:
            raise ValueError(f"Frozen source changed: {p}")
    for p, expected in manifest.get("artifacts", {}).items():
        if file_sha256(root / p) != expected:
            raise ValueError(f"Frozen design changed: {p}")
    if manifest["runtime"] != _identity():
        raise ValueError("Runtime changed; create a new MF study")
    return root, manifest


def _stratum(row):
    return (
        row["era"],
        row["catalogue_climate_zone"],
        row["ventilation"],
        "heated" if row.get("heated_basement_floors") == 1 else "other",
        "low"
        if row.get("storeys", 0) <= 2
        else "tall"
        if row.get("storeys", 0) >= 5
        else "mid",
        row.get("renovation_timing", "unknown"),
        row.get("roof_form", "unknown"),
        row.get("completed_window_replacement", "unknown"),
    )


def sample_properties(rows, count=1200, seed=SEED):
    """Round-robin parent cells, then physical strata; no target participates.

    Fine strata must not fill a small panel entirely from the first climate zone.
    Parent cells get an equal first turn before selecting a second property.
    """
    from collections import deque

    groups = {}
    for row in rows:
        groups.setdefault(_stratum(row), []).append(row)
    parents = {}
    for key in sorted(groups, key=lambda k: digest((seed, k))):
        parents.setdefault(key[:3], []).append(
            deque(
                sorted(
                    groups[key],
                    key=lambda r: digest((seed, r["property_key"], r["building_id"])),
                )
            )
        )
    queues = []
    for parent in sorted(parents, key=lambda k: digest((seed, k))):
        strata = parents[parent]
        ordered = deque()
        while any(strata):
            for group in strata:
                if group:
                    ordered.append(group.popleft())
        queues.append(ordered)
    selected, used = [], set()
    while len(selected) < count and any(queues):
        for queue in queues:
            while queue and queue[0]["property_key"] in used:
                queue.popleft()
            if queue and len(selected) < count:
                row = queue.popleft()
                selected.append(row)
                used.add(row["property_key"])
    return selected


def assign_splits(rows, inspected, seed=SEED):
    """Property-atomic splits; inspected properties are always development."""
    assignments = {}
    fresh_groups = {}
    for row in rows:
        if row["property_key"] in inspected:
            assignments[row["property_key"]] = "train"
        else:
            fresh_groups.setdefault((row["era"], row["ventilation"]), set()).add(
                row["property_key"]
            )
    # A property can contain different era/system buildings: assign once globally.
    for key in sorted(fresh_groups):
        props = sorted(
            fresh_groups[key] - set(assignments), key=lambda p: digest((seed, p))
        )
        for i, prop in enumerate(props):
            assignments[prop] = (
                "train"
                if i < int(0.6 * len(props))
                else "validation"
                if i < int(0.8 * len(props))
                else "test"
            )
    return [
        r
        | {
            "split": assignments[r["property_key"]],
            "previously_examined": r["property_key"] in inspected,
        }
        for r in rows
    ]


def audit(config_path, output):
    """Inventory all MF slots and select national properties without energy access."""
    from epsm_data.matrix_audit import audit_matrix
    from epsm_data.parametric import era_for_year

    config_path = Path(config_path).resolve()
    config = read(config_path)
    root = _new(output)
    sources = {str(config_path): file_sha256(config_path)}

    def source(name):
        p = _path(config_path, config[name])
        sources[str(p)] = file_sha256(p)
        return p

    stock = pd.read_parquet(source("stock_features"))
    membership = pd.read_parquet(
        source("membership_audit"),
        columns=["building_id", "formular_id", "eligible", "assigned_archetype_use"],
    )
    frame = stock.merge(
        membership, on=["building_id", "formular_id"], validate="one_to_one", how="left"
    )
    if frame.eligible.isna().any():
        raise ValueError("Membership audit does not cover source inventory")
    mf = frame[frame.assigned_archetype_use == "multi_family"].copy()
    mf["era"] = mf.construction_year.map(
        lambda y: era_for_year(int(y)) if pd.notna(y) and y > 0 else "unknown"
    )
    mf["catalogue_climate_zone"] = mf.municipality.map(zone_for_municipality)
    affected = pd.read_parquet(source("height_quarantine"))
    quarantined = set(affected.building_id.astype(str))
    inspected = set()
    for value in config.get("previously_examined_features", []):
        p = _path(config_path, value)
        sources[str(p)] = file_sha256(p)
        if p.suffix == ".parquet":
            inspected.update(
                pd.read_parquet(p, columns=["property_key"])
                .property_key.dropna()
                .astype(str)
            )
        else:
            inspected.update(r["property_key"] for r in read(p))
    matrix = audit_matrix()
    canonical = {
        (s["key"]["era"], s["state"]): s
        for s in matrix["states"]
        if s["key"]["category"] == "multi_family"
    }
    rows, exclusions = [], []
    for raw in mf.to_dict("records"):
        row = cleaned(raw)
        reasons = []
        if not row["eligible"]:
            reasons.append("membership")
        if row["building_id"] in quarantined:
            reasons.append("known_extraction_mismatch")
        if row["era"] not in ERAS:
            reasons.append("unknown_era")
        if row["ventilation"] not in SYSTEMS:
            reasons.append("unsupported_ventilation")
        if not row.get("property_key"):
            reasons.append("missing_property_identity")
        if row.get("height_status") != "determined":
            reasons.append("missing_height")
        if row.get("geometry_status") not in {"valid", "accepted"}:
            reasons.append("geometry_source_review")
        if (
            not row.get("storeys")
            or row["storeys"] < 1
            or int(row["storeys"]) != row["storeys"]
        ):
            reasons.append("invalid_storeys")
        if row.get("heated_basement_floors") not in (0, 1):
            reasons.append("unresolved_heated_basement_count")
        if row.get("heating_family") != "district_heat":
            reasons.append("delivery_evidence_pending")
        if row.get("correction_status") != "normal_use_adjusted_pre_weather_correction":
            reasons.append("target_boundary")
        if reasons:
            exclusions.append(
                {
                    "building_id": row["building_id"],
                    "property_key": row["property_key"],
                    "reasons": reasons,
                }
            )
        else:
            row["mf_evidence"] = {}
            rows.append(row)
    selected = assign_splits(
        sample_properties(rows, config.get("initial_properties", 1200)), inspected
    )
    slots = []
    for era, zone, system, state in product(ERAS, ZONES, SYSTEMS, STATES):
        members = mf[
            (mf.era == era)
            & (mf.catalogue_climate_zone == zone)
            & (mf.ventilation == system)
        ]
        selected_members = [
            r
            for r in selected
            if (r["era"], r["catalogue_climate_zone"], r["ventilation"])
            == (era, zone, system)
        ]
        slots.append(
            {
                "key": {
                    "category": "multi_family",
                    "era": era,
                    "climate_zone": zone,
                    "ventilation_system": system,
                    "renovation_state": state,
                },
                "construction_parent": canonical[era, state]["archetype_id"],
                "inventory_buildings": len(members),
                "inventory_properties": int(members.property_key.nunique()),
                "component_state_observed_properties": None,
                "counts_are_era_zone_system_not_observed_renovation_state": True,
                "selected_properties": len(selected_members),
                "split_counts": dict(Counter(r["split"] for r in selected_members)),
                "disposition": "inherit" if len(selected_members) < 30 else "retain",
                "validation_status": "provisional",
                "reason": "Source construction retained; no independently validated replacement",
                "default_scope": "reference_era_fabric_not_observed_surviving_stock",
            }
        )
    findings = {}
    for name, value in config.get("historical_evidence", {}).items():
        p = _path(config_path, value)
        sources[str(p)] = file_sha256(p)
        findings[name] = read(p)
    counts = Counter(reason for item in exclusions for reason in item["reasons"])
    betsi = findings.get("betsi_source_audit", {})
    limitations = []
    metadata = betsi.get("raw_metadata", {})
    if not metadata.get("construction_year_rows_by_type", {}).get("F"):
        limitations.append(
            "Archived BETSI MF observations lack an audited construction-year join; do not infer era defaults from pooled MF U-values"
        )
    if not metadata.get("weight_rows_by_type", {}).get("F"):
        limitations.append(
            "Archived BETSI MF survey weights unavailable; distributions are descriptive and unweighted"
        )
    hypotheses = [
        {
            "id": "regional_fabric",
            "status": "engineering_sensitivity",
            "decision": "retain until sourced assemblies and validation",
            "controls": ["wall_resistance", "roof_resistance"],
            "bounds": [1, 1.4],
        },
        {
            "id": "component_renovation",
            "status": "quantitative_evidence_application",
            "decision": "apply dated whole-component observations; checkboxes remain uncertainty",
        },
        {
            "id": "basement_operation",
            "status": "engineering_sensitivity",
            "controls": ["setpoint", "gains", "mechanical_flow"],
            "extent": "independent evidence required",
        },
        {"id": "attic_exchange", "status": "engineering_sensitivity", "ach": [0, 1, 2]},
        {
            "id": "operating_flow",
            "status": "engineering_sensitivity",
            "multipliers": [0.8, 1, 1.2],
            "reported_design_is_operating_evidence": False,
        },
        {
            "id": "height_and_stack",
            "status": "physical_model_required",
            "decision": "no residual-driven geometry or automatic height leakage multiplier",
        },
        {
            "id": "window_optics_wwr_bridges_ground_gains",
            "status": "review_and_existing_sensitivity_workflows",
            "decision": "require source applicability and double-counting audit",
        },
        {
            "id": "synthetic_reconstruction",
            "status": "secondary_robustness_check",
            "decision": "retain prior findings; reopen only on changed input boundaries",
        },
    ]
    result = {
        "schema_version": 1,
        "slots": slots,
        "canonical_states": list(canonical.values()),
        "inventory_mf_buildings": len(mf),
        "eligible_physics_buildings": len(rows),
        "selected_properties": len(selected),
        "split_counts": dict(Counter(r["split"] for r in selected)),
        "exclusion_counts_overlapping": dict(counts),
        "energy_targets_read": False,
        "catalogue_changed": False,
        "confirmation_ready": False,
        "untouched_test_properties": sum(r["split"] == "test" for r in selected),
        "confirmation_limitation": "Previously evaluated national properties remain development; missing evidence and simulation support require separate gates",
        "historical_evidence": findings,
        "source_limitations": limitations,
        "hypothesis_register": hypotheses,
    }
    root.mkdir(parents=True)
    atomic_json(root / "decision-register.json", result)
    atomic_json(root / "cohort.json", selected)
    atomic_json(root / "exclusions.json", exclusions)
    atomic_json(root / "config.json", config)
    atomic_json(
        root / "manifest.json",
        {
            "sources": sources,
            "runtime": _identity(),
            "artifacts": {
                p: file_sha256(root / p)
                for p in [
                    "config.json",
                    "cohort.json",
                    "decision-register.json",
                    "exclusions.json",
                ]
            },
        },
    )
    report(root)
    return {
        k: result[k]
        for k in (
            "inventory_mf_buildings",
            "eligible_physics_buildings",
            "selected_properties",
            "split_counts",
            "confirmation_ready",
            "exclusion_counts_overlapping",
        )
    }


def scenarios(spec, row):
    """Frozen separate hypotheses; no geometry/area resizing or residual inputs."""
    from epsm_core import ScheduleSpec, UnconditionedExchangeSpec, ZoneUseSpec
    from .mf_evidence import evidence_locks

    cases = {"baseline": (spec, {})}
    locked = set(row.get("locked_parameters", ())) | evidence_locks(row)
    for role, field, control in (
        ("wall", "wall", "wall_resistance"),
        ("top", "top", "roof_resistance"),
    ):
        if control in locked:
            continue
        if not insulation_candidates(getattr(spec.envelope, field)):
            # An uninsulated assembly needs an independently sourced retrofit;
            # its structure cannot stand in for a missing insulation layer.
            continue
        for factor in (1.2, 1.4):
            new, audit = exact_resistance(getattr(spec.envelope, field), factor)
            cases[f"{role}_r{round((factor - 1) * 100)}"] = (
                spec.updated(envelope=spec.envelope.updated(**{field: new})),
                audit,
            )
    if "flow_multiplier" not in locked and spec.airflow.topology != "S":
        for factor in (0.8, 1.2):
            cases[f"flow_{factor}"] = (
                spec.updated(
                    airflow=spec.airflow.updated(
                        mechanical_vent_l_s_m2=spec.airflow.mechanical_vent_l_s_m2
                        * factor
                    )
                ),
                {
                    "operating_flow_multiplier": factor,
                    "design_flow_is_not_operating_evidence": True,
                },
            )
    if spec.geometry.roof.topology == "cold_attic":
        for ach in (1.0, 2.0):
            rest = tuple(
                x for x in spec.unconditioned_exchange if x.zone_kind != "attic"
            )
            cases[f"attic_{ach}"] = (
                spec.updated(
                    unconditioned_exchange=rest
                    + (
                        UnconditionedExchangeSpec(
                            zone_kind="attic",
                            outdoor_air_ach=ach,
                            source="MF fixed engineering attic sensitivity",
                        ),
                    )
                ),
                {"attic_exchange_ach": ach},
            )
    if (
        spec.geometry.basement_conditioned
        and not spec.zone_uses
        and not spec.delivery.fvp
    ):
        for temperature in (15.0, 18.0):
            use = spec.use.updated(heating_setpoint=ScheduleSpec.constant(temperature))
            cases[f"basement_{temperature}"] = (
                spec.updated(
                    zone_uses=(
                        ZoneUseSpec(
                            zone_kind="basement",
                            use=use,
                            source="MF basement thermostat sensitivity; extent/gains/flow retained",
                        ),
                    )
                ),
                {"setpoint_c": temperature, "heated_extent_changed": False},
            )
        for role in ("gains", "flow"):
            assignment = ZoneUseSpec(
                zone_kind="basement",
                use=spec.use.updated(
                    occupant_density_per_m2=0,
                    equipment_gain_w_m2=0,
                    lighting_gain_w_m2=0,
                )
                if role == "gains"
                else None,
                airflow=spec.airflow.updated(mechanical_vent_l_s_m2=0)
                if role == "flow"
                else None,
                source="Separate common-area completion sensitivity; no observed basement operation",
            )
            cases[f"basement_{role}_zero"] = (
                spec.updated(zone_uses=(assignment,)),
                {
                    "classification": "engineering_scenario",
                    "heated_extent_changed": False,
                    "note": "Diagnostic zero bound, not a proposed universal basement programme",
                },
            )
    return cases


def prepare_national(study, weather_campaign, output):
    """Prepare selected national baselines and retain every missing-input outcome.

    Reused municipality/year weather is an explicit completion assumption. This
    stage does not claim building-specific weather verification or new geometry.
    """
    from .cohort_transfer.preparation import build_baseline, physical_descriptors
    from .mf_evidence import evidence_locks
    from epsm_core.weather import validate_epw_calendar

    study, frozen = _guard(study)
    study_config = read(study / "config.json")
    root = _new(output)
    weather_path = Path(weather_campaign).resolve() / "config.json"
    weather_config = read(weather_path)
    weather = weather_config["weather_manifest"]
    sources = frozen["sources"] | {str(weather_path): file_sha256(weather_path)}
    cases, features, specs, outcomes = [], [], {}, []
    for row in read(study / "cohort.json"):
        key = f"{row['municipality']}:{row['measurement_year']}"
        entry = weather.get(key)
        if not entry:
            outcomes.append(
                {
                    "building_id": row["building_id"],
                    "property_key": row["property_key"],
                    "era": row["era"],
                    "ventilation": row["ventilation"],
                    "status": "needs_local_measurement_year_weather",
                }
            )
            continue
        try:
            weather_file = Path(entry["path"]).resolve()
            if file_sha256(weather_file) != entry["sha256"]:
                raise ValueError("Reused weather checksum mismatch")
            validate_epw_calendar(weather_file, row["measurement_year"])
            sources[str(weather_file)] = entry["sha256"]
            config = {
                "catalogue_climate_zone": row["catalogue_climate_zone"],
                "weather_manifest": {key: entry},
                "mf_reported_zero_flow_policy": study_config.get(
                    "mf_reported_zero_flow_policy", "retain_reported"
                ),
            }
            spec = build_baseline(row, config)
            descriptors = physical_descriptors(row, spec)
            path = f"specs/{row['building_id']}/baseline.json"
            specs[path] = spec.model_dump(mode="json")
            features.append(
                row
                | descriptors
                | {
                    "status": "prepared",
                    "spec_path": path,
                    "membership": {"eligible": True},
                    "heated_basement": "yes" if row["heated_basement_floors"] else "no",
                    "locked_parameters": sorted(
                        set(row.get("locked_parameters", [])) | evidence_locks(row)
                    ),
                    "mf_reported_zero_flow_policy": config[
                        "mf_reported_zero_flow_policy"
                    ],
                    "reported_design_flow_l_s_m2": row.get("designed_flow_l_s_m2"),
                    "compiled_mechanical_flow_l_s_m2": spec.airflow.mechanical_vent_l_s_m2,
                    "weather_reuse_assumption": "Existing accepted municipality/year EPW; target-site representativeness unresolved",
                }
            )
            cases.append(
                {
                    "building_id": row["building_id"],
                    "property_key": row["property_key"],
                    "scenario": "baseline",
                    "spec_path": path,
                    "physical_hash": spec.physical_hash(),
                    "atemp_m2": row["atemp_m2"],
                    "era": row["era"],
                    "ventilation": row["ventilation"],
                    "catalogue_climate_zone": row["catalogue_climate_zone"],
                    "split": row["split"],
                    "previously_examined": row["previously_examined"],
                    "weather_representativeness": "municipality_year_completion_assumption",
                    "locked_parameters": sorted(
                        set(row.get("locked_parameters", [])) | evidence_locks(row)
                    ),
                }
            )
            outcomes.append(
                {
                    "building_id": row["building_id"],
                    "property_key": row["property_key"],
                    "era": row["era"],
                    "ventilation": row["ventilation"],
                    "status": "prepared",
                }
            )
        except Exception as exc:
            outcomes.append(
                {
                    "building_id": row["building_id"],
                    "property_key": row["property_key"],
                    "era": row["era"],
                    "ventilation": row["ventilation"],
                    "status": "preparation_failed",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
    root.mkdir(parents=True)
    for path, value in specs.items():
        atomic_json(root / path, value)
    config = read(study / "config.json")
    atomic_json(
        root / "config.json",
        {
            "workers": config.get("workers", 8),
            "engine": config["engine"],
            "recover_context_shadows": False,
            "study_kind": "retrospective_national_baseline",
            "weather_manifest": weather,
        },
    )
    atomic_json(root / "case-plan.json", cases)
    atomic_json(root / "features.json", features)
    atomic_json(root / "preparation-outcomes.json", outcomes)
    result = {
        "attempted_properties": len(outcomes),
        "outcomes": dict(Counter(r["status"] for r in outcomes)),
        "prepared_by_era_system": dict(
            Counter(f"{r['era']}:{r['ventilation']}" for r in features)
        ),
        "energy_targets_read": False,
        "confirmation": False,
    }
    atomic_json(root / "preparation-summary.json", result)
    artifacts = {
        p: file_sha256(root / p)
        for p in [
            "config.json",
            "case-plan.json",
            "features.json",
            "preparation-outcomes.json",
            "preparation-summary.json",
            *specs,
        ]
    }
    atomic_json(
        root / "manifest.json",
        {
            "sources": sources,
            "runtime": _identity(),
            "artifacts": artifacts,
            "confirmation": False,
        },
    )
    return result


def prepare_screen(study, source_campaign, output, count=12):
    """Select historical input-diverse buildings; freeze complete case specs."""
    from epsm_core import BuildingSpec
    from .mf_evidence import apply_quantitative_evidence, evidence_locks

    study, manifest = _guard(study)
    source_campaign = Path(source_campaign).resolve()
    root = _new(output)
    sources = {str(study / "manifest.json"): file_sha256(study / "manifest.json")}
    for p, value in manifest["sources"].items():
        sources[p] = value
    inputs_path = source_campaign / "features.json"
    sources[str(inputs_path)] = file_sha256(inputs_path)
    source = read(inputs_path)
    config = read(study / "config.json")
    quarantine = pd.read_parquet(
        next(
            Path(p)
            for p in sources
            if p.endswith(Path(config["height_quarantine"]).name)
        )
    )
    excluded = set(quarantine.building_id.astype(str))
    eligible = []
    for row in source:
        if (
            row.get("use_group") != "multi_family"
            or row.get("status") != "prepared"
            or row["building_id"] in excluded
        ):
            continue
        if not row.get("membership", {}).get("eligible", False):
            continue
        eligible.append(
            row
            | {
                "catalogue_climate_zone": zone_for_municipality(row["municipality"]),
                "heated_basement_floors": 1
                if row.get("heated_basement") == "yes"
                else 0,
                "mf_evidence": row.get("mf_evidence", {}),
                "locked_parameters": sorted(
                    set(row.get("locked_parameters", [])) | evidence_locks(row)
                ),
            }
        )
    families = {}
    for row in eligible:
        families.setdefault((row["era"], row["ventilation"]), []).append(row)
    selected = [
        row
        for family in sorted(families)
        for row in sample_properties(families[family], count)
    ]
    if not selected:
        raise ValueError("No eligible historical screen properties")
    plans, specs, input_audit = [], {}, []
    for row in selected:
        path = Path(row["spec_path"])
        if not path.is_absolute():
            path = source_campaign / path
        sources[str(path)] = file_sha256(path)
        spec = BuildingSpec.model_validate(read(path))
        if (
            spec.identifier != row["building_id"]
            or spec.load_reference_area_m2 != row["atemp_m2"]
        ):
            raise ValueError("Spec identity/Atemp mismatch")
        weather = Path(spec.climate.epw_path).resolve()
        if file_sha256(weather) != spec.climate.weather_sha256:
            raise ValueError("Weather checksum mismatch")
        sources[str(weather)] = file_sha256(weather)
        corrected, changes = apply_quantitative_evidence(spec, row)
        generated = scenarios(corrected, row)
        generated["baseline"] = (spec, {})
        generated["evidence_applied"] = (corrected, {"components": changes})
        for name, (case, audit_row) in generated.items():
            relative = f"specs/{row['building_id']}/{name}.json"
            specs[relative] = case.model_dump(mode="json")
            plans.append(
                {
                    "building_id": row["building_id"],
                    "property_key": row["property_key"],
                    "scenario": name,
                    "spec_path": relative,
                    "physical_hash": case.physical_hash(),
                    "atemp_m2": row["atemp_m2"],
                    "changes": audit_row,
                    "era": row["era"],
                    "ventilation": row["ventilation"],
                    "catalogue_climate_zone": row["catalogue_climate_zone"],
                    "storeys": row["storeys"],
                    "roof_form": row.get("roof_form"),
                    "heated_basement": spec.geometry.basement_conditioned,
                    "split": "train",
                    "previously_examined": True,
                    "locked_parameters": row.get("locked_parameters", []),
                }
            )
        input_audit.append(
            {
                "building_id": row["building_id"],
                "reported_completed_window": row.get("completed_window_replacement"),
                "design_flow_evidence": row.get("design_flow_evidence_status"),
                "applied_flow_l_s_m2": spec.airflow.mechanical_vent_l_s_m2,
                "applied_window_u": spec.facade.glazing.u_value_w_m2k,
                "basement_conditioned": spec.geometry.basement_conditioned,
                "basement_depth_m": spec.geometry.basement_depth_m,
                "basement_presence_if_zero_heated_floors": "unknown",
                "quantitative_evidence_changes": changes,
                "fabric_scenario_dispositions": {
                    field: "locked_by_evidence"
                    if control in row.get("locked_parameters", [])
                    else "screened"
                    if insulation_candidates(getattr(spec.envelope, field))
                    else "requires_sourced_insulation_assembly"
                    for field, control in (
                        ("wall", "wall_resistance"),
                        ("top", "roof_resistance"),
                    )
                },
            }
        )
    root.mkdir(parents=True)
    for path, value in specs.items():
        atomic_json(root / path, value)
    atomic_json(root / "case-plan.json", plans)
    atomic_json(root / "input-audit.json", input_audit)
    source_config = source_campaign / "config.json"
    sources[str(source_config)] = file_sha256(source_config)
    original = read(source_config)
    atomic_json(
        root / "config.json",
        {
            "workers": config.get("workers", 8),
            "engine": config["engine"],
            "recover_context_shadows": original.get("recover_context_shadows", False),
            "study_kind": "retrospective_separate_sensitivity_not_confirmation",
        },
    )
    artifacts = {
        p: file_sha256(root / p)
        for p in ["case-plan.json", "input-audit.json", "config.json", *specs]
    }
    atomic_json(
        root / "manifest.json",
        {
            "sources": sources,
            "runtime": _identity(),
            "artifacts": artifacts,
            "confirmation": False,
            "screen_properties": len(selected),
        },
    )
    return {
        "screen_properties": len(selected),
        "cases": len(plans),
        "confirmation": False,
    }


def _run_case(item):
    """Worker has no access to target observations; write explicit strict receipt."""
    from epsm_core import BuildingSpec, EnergyPlusRunner

    root, case, config = item
    root = Path(root)
    folder = root / "runs" / digest((case["building_id"], case["scenario"]))
    path = folder / "receipt.json"
    if path.exists():
        record = read(path)
        if any(
            record.get(k) != case.get(k)
            for k in ("building_id", "scenario", "physical_hash")
        ):
            raise ValueError("Cached case identity changed")
        return record
    spec = BuildingSpec.model_validate(read(root / case["spec_path"]))
    record = case | {"status": "failed", "prediction_kwh_m2": None}
    try:
        output = folder / "energyplus"
        if output.exists():
            index = 0
            while (folder / f"interrupted-run-{index}").exists():
                index += 1
            output.rename(folder / f"interrupted-run-{index}")
        result = EnergyPlusRunner(energyplus=config["engine"]).run(
            spec,
            output,
            recover_context_shadows=config["recover_context_shadows"],
        )
        heat = result.scalars.get("space_heating_carrier_input_kwh")
        if heat is None or not np.isfinite(heat) or heat < 0:
            raise ValueError("Invalid delivered-space-heat result")
        record.update(
            status="accepted",
            prediction_kwh_m2=heat / case["atemp_m2"],
            diagnostics=result.diagnostics,
            scalars=result.scalars,
        )
    except Exception as exc:  # retain scientific failure, never waive warnings
        record["reason"] = f"{type(exc).__name__}: {exc}"
    atomic_json(path, record)
    return record


def run_screen(study):
    """Three input-diverse baseline smokes precede all candidate runs."""
    from epsm_core import EnergyPlusRunner
    from multiprocessing import get_context

    root, _ = _guard(study)
    config, cases = read(root / "config.json"), read(root / "case-plan.json")
    runner = EnergyPlusRunner(energyplus=config["engine"])
    engine = {
        "engine_hash": runner.compiler.engine_hash,
        "schema_hash": runner.compiler.schema_hash,
    }
    if (root / "engine.json").exists() and read(root / "engine.json") != engine:
        raise ValueError("Engine changed; use a new study")
    atomic_json(root / "engine.json", engine)
    baselines = [c for c in cases if c["scenario"] == "baseline"]
    if not baselines:
        raise ValueError("No prepared baseline cases")
    records = []
    with ProcessPoolExecutor(
        max_workers=config["workers"], mp_context=get_context("spawn")
    ) as pool:
        smoke = list(
            pool.map(_run_case, [(str(root), c, config) for c in baselines[:3]])
        )
        records.extend(smoke)
        if any(r["status"] != "accepted" for r in smoke):
            result = {
                "status": "stopped_after_failed_smoke",
                "smoke": smoke,
                "planned": len(cases),
            }
            atomic_json(root / "execution.json", result)
            return {
                "status": result["status"],
                "planned": len(cases),
                "smoke_failed": sum(r["status"] != "accepted" for r in smoke),
            }
        smoke_ids = {r["building_id"] for r in smoke}
        remaining = [c for c in baselines if c["building_id"] not in smoke_ids]
        records.extend(pool.map(_run_case, [(str(root), c, config) for c in remaining]))
        accepted = {r["building_id"] for r in records if r["status"] == "accepted"}
        candidates = [c for c in cases if c["scenario"] != "baseline"]
        pending = []
        for c in candidates:
            if c["building_id"] not in accepted:
                skipped = c | {
                    "status": "skipped_after_baseline_failure",
                    "prediction_kwh_m2": None,
                }
                atomic_json(
                    root
                    / "runs"
                    / digest((c["building_id"], c["scenario"]))
                    / "receipt.json",
                    skipped,
                )
                records.append(skipped)
            else:
                pending.append(pool.submit(_run_case, (str(root), c, config)))
        for future in as_completed(pending):
            records.append(future.result())
            atomic_json(
                root / "progress.json",
                {
                    "completed": len(records),
                    "planned": len(cases),
                    "accepted": sum(r["status"] == "accepted" for r in records),
                },
            )
    result = {
        "status": "completed",
        "planned": len(cases),
        "accepted": sum(r["status"] == "accepted" for r in records),
        "failed_or_skipped": sum(r["status"] != "accepted" for r in records),
    }
    atomic_json(root / "execution.json", result)
    _guard(root)
    return result


def paired_comparison(rows, baseline, candidate, *, resamples=2000, seed=SEED):
    """Failures remain coverage misses; MAE intervals use the complete paired panel."""
    if not rows:
        raise ValueError("Comparison requires an attempted panel")
    b, c = metrics(rows, baseline), metrics(rows, candidate)
    paired = [
        i
        for i, (x, y) in enumerate(zip(baseline, candidate))
        if x is not None and y is not None
    ]
    groups = {}
    for i, row in enumerate(rows):
        groups.setdefault(row["property_key"], []).append(i)
    props = sorted(groups)
    draws, mae_draws = [], []
    rng = np.random.default_rng(seed)
    for _ in range(resamples):
        indices = [
            i for p in rng.choice(props, len(props), replace=True) for i in groups[p]
        ]
        sample = [rows[i] for i in indices]
        sb, sc = (
            metrics(sample, [baseline[i] for i in indices]),
            metrics(sample, [candidate[i] for i in indices]),
        )
        draws.append(sc["coverage20"] - sb["coverage20"])
        complete = [i for i in indices if i in paired]
        if complete:
            mae_draws.append(
                np.mean(
                    [
                        abs(candidate[i] - rows[i]["target"])
                        - abs(baseline[i] - rows[i]["target"])
                        for i in complete
                    ]
                )
            )
    transitions = {"miss_to_pass": [], "pass_to_miss": []}
    for row, x, y in zip(rows, baseline, candidate):
        bp = x is not None and abs(x / row["target"] - 1) < 0.2
        cp = y is not None and abs(y / row["target"] - 1) < 0.2
        if not bp and cp:
            transitions["miss_to_pass"].append(row["building_id"])
        if bp and not cp:
            transitions["pass_to_miss"].append(row["building_id"])
    return {
        "baseline": b,
        "candidate": c,
        "paired_buildings": len(paired),
        "paired_properties": len({rows[i]["property_key"] for i in paired}),
        "coverage_change_95_interval": np.quantile(draws, [0.025, 0.975]).tolist(),
        "paired_mae_change_95_interval": np.quantile(mae_draws, [0.025, 0.975]).tolist()
        if mae_draws
        else None,
        "mae_intervals_conditional_on_accepted_pairs": True,
        **transitions,
    }


def prepare_joint(study, output, controls):
    """Freeze 32 Latin-hypercube designs after a completed separate screen.

    Explicit controls require an engineering/source justification supplied by
    the investigator. This stage never selects a control using held-out energy.
    """
    from epsm_core import BuildingSpec
    from epsm_data.cohorts import apply_cohort_profile
    from scipy.stats import qmc

    study, frozen = _guard(study)
    if not (study / "evaluation.json").exists():
        raise ValueError("Evaluate separate development screen first")
    controls_path = Path(controls).resolve()
    declared = read(controls_path)
    allowed = {
        "wall_resistance": (1.0, 1.4),
        "roof_resistance": (1.0, 1.4),
        "flow_multiplier": (0.8, 1.2),
        "basement_setpoint_c": (15.0, 21.0),
        "attic_exchange_ach": (0.0, 2.0),
    }
    if not 1 <= len(declared) <= 4:
        raise ValueError("Declare one to four plausible controls")
    for name, item in declared.items():
        if name not in allowed or not str(item.get("source", "")).strip():
            raise ValueError("Unsupported or unsourced joint control")
        lo, hi = item["bounds"]
        if not allowed[name][0] <= lo < hi <= allowed[name][1]:
            raise ValueError("Joint control exceeds screened bounds")
    root = _new(output)
    values = qmc.LatinHypercube(d=len(declared), seed=SEED).random(32)
    baselines = [
        c for c in read(study / "case-plan.json") if c["scenario"] == "evidence_applied"
    ]
    if not baselines:
        baselines = [
            c for c in read(study / "case-plan.json") if c["scenario"] == "baseline"
        ]
    from .cohort_transfer.parameters import applicable

    plans, specs = [], {}
    for base in baselines:
        spec = BuildingSpec.model_validate(read(study / base["spec_path"]))
        register = applicable(spec, locked=base.get("locked_parameters", ()))
        for index, point in enumerate(values):
            updated = spec
            parameters = {}
            for (name, item), fraction in zip(declared.items(), point):
                if register[name]["status"] != "screen_and_cohort_calibration":
                    continue
                lo, hi = item["bounds"]
                value = lo + fraction * (hi - lo)
                if name in {"wall_resistance", "roof_resistance"}:
                    field = "wall" if name == "wall_resistance" else "top"
                    new, _ = exact_resistance(getattr(updated.envelope, field), value)
                    updated = updated.updated(
                        envelope=updated.envelope.updated(**{field: new})
                    )
                else:
                    parameters[name] = float(value)
            if parameters:
                updated = apply_cohort_profile(
                    updated,
                    {
                        "parameters": parameters,
                        "bounds": {k: v["bounds"] for k, v in declared.items()},
                        "infiltration_basis": "atemp_area_rate",
                        "source": "Frozen sourced MF joint sensitivity",
                    },
                )
            name = f"joint_{index:02}"
            path = f"specs/{base['building_id']}/{name}.json"
            specs[path] = updated.model_dump(mode="json")
            plans.append(
                base
                | {
                    "scenario": name,
                    "spec_path": path,
                    "physical_hash": updated.physical_hash(),
                    "joint_design_index": index,
                }
            )
        path = f"specs/{base['building_id']}/baseline.json"
        specs[path] = spec.model_dump(mode="json")
        plans.append(
            base
            | {
                "scenario": "baseline",
                "spec_path": path,
                "physical_hash": spec.physical_hash(),
            }
        )
    root.mkdir(parents=True)
    for path, value in specs.items():
        atomic_json(root / path, value)
    atomic_json(root / "case-plan.json", plans)
    atomic_json(root / "config.json", read(study / "config.json"))
    atomic_json(root / "joint-controls.json", declared)
    sources = frozen["sources"] | {
        str(study / "manifest.json"): file_sha256(study / "manifest.json"),
        str(controls_path): file_sha256(controls_path),
    }
    artifacts = {
        p: file_sha256(root / p)
        for p in ["case-plan.json", "config.json", "joint-controls.json", *specs]
    }
    atomic_json(
        root / "manifest.json",
        {
            "sources": sources,
            "runtime": _identity(),
            "artifacts": artifacts,
            "confirmation": False,
        },
    )
    return {"joint_designs": 32, "cases": len(plans), "confirmation": False}


def promotion_gate(
    comparison,
    *,
    independent_physical_evidence,
    untouched,
    split_counts,
    subgroup_comparisons=(),
):
    """Fail closed on sparse, retrospective, failed or physically unsupported panels."""
    reasons = []
    if not independent_physical_evidence:
        reasons.append("missing_independent_physical_evidence")
    if not untouched:
        reasons.append("retrospective_or_previously_opened_targets")
    if any(
        split_counts.get(k, 0) < n
        for k, n in {"train": 90, "validation": 30, "test": 30}.items()
    ):
        reasons.append("insufficient_property_support")
    b, c = comparison["baseline"], comparison["candidate"]
    if b["failed"] or c["failed"]:
        reasons.append("incomplete_paired_simulation_panel")
    ci = comparison["coverage_change_95_interval"]
    mae_ci = comparison["paired_mae_change_95_interval"]
    coverage_gain = ci[0] > 0
    mae_gain = (
        b["mae_kwh_m2"] is not None
        and c["mae_kwh_m2"] is not None
        and c["mae_kwh_m2"] <= 0.95 * b["mae_kwh_m2"]
        and mae_ci is not None
        and mae_ci[1] < 0
        and ci[0] >= -0.02
    )
    if not (coverage_gain or mae_gain):
        reasons.append("no_supported_performance_gain")
    for item in subgroup_comparisons:
        if item["paired_properties"] < 30:
            continue
        sb, sc = item["baseline"], item["candidate"]
        if (
            sc["coverage20"] < sb["coverage20"] - 0.05
            or sc["mae_kwh_m2"] > 1.1 * sb["mae_kwh_m2"]
        ):
            reasons.append("material_subgroup_regression")
            break
    return {
        "promote": not reasons,
        "reasons": reasons,
        "disposition": "change" if not reasons else "retain",
    }


def evaluate_screen(study, targets_path):
    """Read development targets only after all attempts; never promote a screen."""
    root, _ = _guard(study)
    if (
        not (root / "execution.json").exists()
        or read(root / "execution.json")["status"] != "completed"
    ):
        raise ValueError("Complete all screen attempts before opening targets")
    cases = read(root / "case-plan.json")
    receipts = [
        read(root / "runs" / digest((c["building_id"], c["scenario"])) / "receipt.json")
        for c in cases
    ]
    target_path = Path(targets_path).resolve()
    signature = {
        "targets_sha256": file_sha256(target_path),
        "receipts_sha256": digest(receipts),
    }
    opened = root / "evaluation-opened.json"
    if opened.exists() and read(opened) != signature:
        raise ValueError("Opened targets or receipts changed")
    if target_path.suffix == ".parquet":
        targets = pd.read_parquet(target_path).to_dict("records")
    else:
        targets = read(target_path)
    allowed = {r["building_id"] for r in receipts}
    target = {}
    for r in targets:
        if r["building_id"] not in allowed:
            continue
        value = r.get("target", r.get("reported_kwh_m2"))
        if value is None and r.get("heat_kwh") is not None:
            value = r["heat_kwh"] / r["atemp_m2"]
        if value is None or not np.isfinite(value) or value <= 0:
            raise ValueError("Missing positive target")
        if r["building_id"] in target:
            raise ValueError("Duplicate target identity")
        target[r["building_id"]] = float(value)
    if set(target) != allowed:
        raise ValueError("Targets do not cover screen")
    atomic_json(opened, signature)
    baselines = {r["building_id"]: r for r in receipts if r["scenario"] == "baseline"}
    baseline_rows = [
        r | {"target": target[r["building_id"]]} for r in baselines.values()
    ]
    baseline_metrics = metrics(
        baseline_rows, [r["prediction_kwh_m2"] for r in baseline_rows]
    )
    baseline_groups = {}
    for row in baseline_rows:
        baseline_groups.setdefault(f"{row['era']}:{row['ventilation']}", []).append(row)
    baseline_by_era_system = {
        name: metrics(panel, [r["prediction_kwh_m2"] for r in panel])
        for name, panel in sorted(baseline_groups.items())
    }
    comparisons = {}
    for name in sorted({r["scenario"] for r in receipts} - {"baseline"}):
        panel = [r for r in receipts if r["scenario"] == name]
        rows = [r | {"target": target[r["building_id"]]} for r in panel]
        baseline = [baselines[r["building_id"]]["prediction_kwh_m2"] for r in panel]
        predictions = [r["prediction_kwh_m2"] for r in panel]
        result = paired_comparison(rows, baseline, predictions)
        result["promotion"] = promotion_gate(
            result,
            independent_physical_evidence=False,
            untouched=False,
            split_counts={"train": len(rows)},
        )
        result["applicable_panel_only"] = True
        comparisons[name] = result
    atomic_json(
        root / "evaluation.json",
        {
            "kind": "retrospective_development_sensitivity",
            "comparisons": comparisons,
            "baseline": baseline_metrics,
            "baseline_by_era_system": baseline_by_era_system,
            "catalogue_changed": False,
        },
    )
    lines = [
        "# MF separate physics screen",
        "",
        "Retrospective development results; no catalogue changes promoted.",
        f"Baseline: {baseline_metrics['accepted']}/{baseline_metrics['attempted']} accepted; "
        f"coverage within 20% {baseline_metrics['coverage20']:.1%}; "
        f"MAE {baseline_metrics['mae_kwh_m2']} kWh/m²; "
        f"bias {baseline_metrics['bias_kwh_m2']} kWh/m².",
        "",
        "| Scenario | Buildings | Coverage baseline → candidate | MAE baseline → candidate | Fixed / new misses |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, result in comparisons.items():
        b, c = result["baseline"], result["candidate"]
        lines.append(
            f"| {name} | {b['attempted']} | {b['coverage20']:.1%} → {c['coverage20']:.1%} | {b['mae_kwh_m2'] if b['mae_kwh_m2'] is not None else 'failed'} → {c['mae_kwh_m2'] if c['mae_kwh_m2'] is not None else 'failed'} | {len(result['miss_to_pass'])} / {len(result['pass_to_miss'])} |"
        )
    lines.extend(
        [
            "",
            "## Baselines by era/system",
            "",
            "| Era/system | Properties | Coverage within 20% | MAE kWh/m² | Bias kWh/m² |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for name, result in baseline_by_era_system.items():
        lines.append(
            f"| {name} | {result['independent_properties']} | {result['coverage20']:.1%} | "
            f"{result['mae_kwh_m2']} | {result['bias_kwh_m2']} |"
        )
    (root / "report.md").write_text("\n".join(lines) + "\n")
    return {
        "scenarios": len(comparisons),
        "promoted": 0,
        "report": str(root / "report.md"),
    }


def report(study):
    root = Path(study)
    data = read(root / "decision-register.json")
    lines = [
        "# MF revision decision register",
        "",
        f"Reviewed **{len(data['slots'])} slots** backed by **{len(data['canonical_states'])} construction states**.",
        "",
        f"MF inventory: {data['inventory_mf_buildings']}; eligible physical candidates: {data['eligible_physics_buildings']}; selected independent properties: {data['selected_properties']}.",
        "",
        f"Splits: {data['split_counts']}. Targets were not read for sampling.",
        "",
        "All current constructions remain provisional. No heating-fit correction was applied to catalogue defaults.",
        "",
        "## Source defaults by era and renovation state",
        "",
        "| Era | State | Wall U | Top U | Window U |",
        "|---|---|---:|---:|---:|",
    ]
    for state in sorted(
        data["canonical_states"], key=lambda s: (s["key"]["era"], s["state"])
    ):
        p = state["properties"]
        lines.append(
            f"| {state['key']['era']} | {state['state']} | {p['external_wall']['exported_nominal_u_w_m2k']:.3f} | {p['roof']['exported_nominal_u_w_m2k']:.3f} | {state['window']['u_value_w_m2k']:.2f} |"
        )
    lines += [
        "",
        "## Confirmation and remaining evidence",
        "",
        data["confirmation_limitation"],
        "",
        "- Dated renovation checkboxes do not identify U-values, optics or insulation depth.",
        "- No reported heated basement does not establish slab-on-ground.",
        "- Reported design outdoor flow is not measured operating flow.",
        "- Regional R+20/+40% scenarios need assembly evidence and independent validation.",
        "- Source and state performance are separate from inherited classification counts.",
        "",
        "## Quarantine and preparation exclusions",
        "",
    ]
    lines += [f"- {item}" for item in data.get("source_limitations", [])]
    lines += [
        f"- {k}: {v}" for k, v in sorted(data["exclusion_counts_overlapping"].items())
    ]
    (root / "report.md").write_text("\n".join(lines) + "\n")
    return str(root / "report.md")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="stage", required=True)
    p = sub.add_parser("audit")
    p.add_argument("--config", required=True)
    p.add_argument("--output", required=True)
    p = sub.add_parser("prepare-screen")
    p.add_argument("--study", required=True)
    p.add_argument("--source-campaign", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--properties", type=int, default=12)
    p = sub.add_parser("prepare-national")
    p.add_argument("--study", required=True)
    p.add_argument("--weather-campaign", required=True)
    p.add_argument("--output", required=True)
    p = sub.add_parser("prepare-joint")
    p.add_argument("--study", required=True)
    p.add_argument("--controls", required=True)
    p.add_argument("--output", required=True)
    p = sub.add_parser("run-screen")
    p.add_argument("--study", required=True)
    p = sub.add_parser("evaluate-screen")
    p.add_argument("--study", required=True)
    p.add_argument("--targets", required=True)
    p = sub.add_parser("report")
    p.add_argument("--study", required=True)
    args = parser.parse_args(argv)
    if args.stage == "audit":
        result = audit(args.config, args.output)
    elif args.stage == "prepare-screen":
        result = prepare_screen(
            args.study, args.source_campaign, args.output, args.properties
        )
    elif args.stage == "run-screen":
        result = run_screen(args.study)
    elif args.stage == "prepare-national":
        result = prepare_national(args.study, args.weather_campaign, args.output)
    elif args.stage == "prepare-joint":
        result = prepare_joint(args.study, args.output, args.controls)
    elif args.stage == "evaluate-screen":
        result = evaluate_screen(args.study, args.targets)
    else:
        result = report(args.study)
    import json

    print(json.dumps(cleaned(result), indent=2))


if __name__ == "__main__":
    main()
