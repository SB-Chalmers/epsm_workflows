"""Bounded physical subdivision after shared MF parent calibration.

Children are assigned from reliable physical descriptors within an era. Existing
parent operation stays fixed; only screened construction controls may change.
Alternative construction blocks return unsupported parent construction terms to
their declared priors. The actual parent remains the native comparison baseline.
Every selection gate is inside property-separated training/validation folds.
Outer-fold child profiles are development candidates, not deployment exports.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from .campaign import atomic_json
from .cohort_transfer.common import cleaned, digest, property_folds, read
from .cohort_transfer.grouping import assign, fit_group, stability
from .cohort_transfer.preparation import TargetStore
from .mf_comparison import adoption_gate, metrics, paired_change
from .mf_physics import prior_distance, select_candidate

MINIMUM_PROPERTIES = 30
STABILITY_RESAMPLES = 20
MINIMUM_ARI = 0.7
MINIMUM_MAE_GAIN = 0.05
K = 2
PHYSICAL_FIELDS = (
    "wall_area_per_atemp",
    "roof_area_per_atemp",
    "ground_area_per_atemp",
    "volume_per_atemp",
    "compactness",
    "storeys",
    "atemp_per_apartment",
    "gross_to_atemp_ratio",
    "roof_form",
    "building_position",
    "heated_basement",
    "use_group",
    "era",
    "heating_family",
    "correction_status",
)
FABRIC_FIELDS = {
    "window_u_multiplier",
    "wall_resistance",
    "roof_resistance",
    "wall_insulation_overlay_m",
}
OPERATING_FIELDS = {
    "infiltration_multiplier",
    "flow_multiplier",
    "recovery",
    "basement_setpoint_c",
}


def physical_rows(rows):
    """Remove energy, optional renovation fields and operation before clustering."""
    return [
        {"building_id": row["building_id"], "property_key": row["property_key"]}
        | {name: row.get(name) for name in PHYSICAL_FIELDS}
        for row in rows
    ]


def _support(rows):
    return len({row["property_key"] for row in rows})


def _partition(rows):
    """Fit one target-free k=2 partition and verify support/stability."""
    if _support(rows) < K * MINIMUM_PROPERTIES:
        return None, {"passed": False, "reason": "fewer_than_60_training_properties"}
    descriptors = physical_rows(rows)
    try:
        model = fit_group(descriptors, K, minimum=MINIMUM_PROPERTIES)
        checked = stability(
            descriptors,
            model,
            resamples=STABILITY_RESAMPLES,
            minimum=MINIMUM_PROPERTIES,
        )
    except ValueError as error:
        return None, {
            "passed": False,
            "reason": "unsupported_physical_partition",
            "detail": str(error),
        }
    reasons = []
    if min(model["property_counts"]) < MINIMUM_PROPERTIES:
        reasons.append("child_training_support_below_30")
    if checked["median_ari"] < MINIMUM_ARI:
        reasons.append("assignment_stability_below_0.7")
    if model.get("missingness_ari", 0.0) >= 0.7:
        reasons.append("missingness_defined_partition")
    audit = {
        "passed": not reasons,
        "reasons": reasons,
        "training_properties": _support(rows),
        "child_training_properties": model["property_counts"],
        "stability": checked,
        "physical_fields": list(PHYSICAL_FIELDS),
    }
    return (model if not reasons else None), audit


def _parent_settings(era, parent):
    return dict(parent.get("by_era", {}).get(era, parent["pooled"]))


def _child_block(model):
    """Keep the primary construction block and its one shared operating axis."""
    controls = list(model.get("fit_fabric_controls", []))
    if (
        len(controls) > 2
        or len(controls) != len(set(controls))
        or set(controls) - FABRIC_FIELDS
    ):
        raise ValueError(
            "Subdivision may fit at most two screened construction controls"
        )
    active = list(model["active"])
    operating = [name for name in active if name in OPERATING_FIELDS]
    if (
        len(active) != len(set(active))
        or len(active) > 3
        or len(operating) > 1
        or set(active) - FABRIC_FIELDS - OPERATING_FIELDS
        or set(controls) - set(active)
    ):
        raise ValueError(
            "Subdivision requires a supported primary block of at most three controls "
            "with at most one shared operating control"
        )
    return controls, operating + controls


def _child_candidate_base(parent_parameters, model, controls):
    """Reset construction outside the selected child block, never operation."""
    parameters = dict(parent_parameters)
    resets = {}
    for name in sorted(set(parameters) & (FABRIC_FIELDS - set(controls))):
        if name not in model["register"]:
            raise ValueError(
                f"Unsupported parent construction has no declared prior: {name}"
            )
        prior = model["register"][name]["baseline"]
        if parameters[name] != prior:
            resets[name] = {"parent_value": parameters[name], "child_prior": prior}
        parameters[name] = prior
    return parameters, resets


def fit_child(rows, forbidden, parent_parameters, evaluator, models, *, output):
    """Fit construction only; confirm three emulated finalists with native Core."""
    from .mf_parent_dynamic import _predict_estimate
    from .mf_parent_emulator_blocks import settings_supported

    if set(forbidden) & {r["property_key"] for r in rows}:
        raise ValueError("Held-out property requested by subdivision fitting")
    if _support(rows) < MINIMUM_PROPERTIES:
        raise ValueError("A physical child requires at least 30 training properties")
    eras = {row["era"] for row in rows}
    if len(eras) != 1:
        raise ValueError("Construction children must remain within one era")
    era = next(iter(eras))
    model = models[era]
    controls, block = _child_block(model)
    candidate_base, resets = _child_candidate_base(parent_parameters, model, controls)
    if not settings_supported(candidate_base, model):
        raise ValueError("Child candidate base is outside its supported primary block")
    native_parent = evaluator.batch(rows, {"parent": dict(parent_parameters)})
    baseline_values = [native_parent[row["building_id"], "parent"] for row in rows]
    baseline = {
        "id": "parent",
        "parameters": dict(parent_parameters),
        "metrics": metrics(rows, baseline_values),
        "prior_distance": prior_distance(parent_parameters, model["register"]),
    }
    if not controls:
        result = baseline | {
            "reason": "no_screened_construction_controls",
            "fitting_block": block,
            "native_parent_parameters": dict(parent_parameters),
            "operating_parameters_preserved": True,
        }
        atomic_json(output, result)
        return result
    candidates = {digest(parent_parameters): baseline}
    for settings in model["designs"].values():
        parameters = dict(candidate_base)
        parameters.update(
            {
                name: settings.get(name, model["register"][name]["baseline"])
                for name in controls
            }
        )
        if digest(parameters) in candidates:
            continue
        if not settings_supported(parameters, model):
            raise ValueError("Child candidate is outside its supported primary block")
        values = [_predict_estimate(row, parameters, models) for row in rows]
        score = metrics(rows, values)
        if score["failed"] > baseline["metrics"]["failed"]:
            continue
        candidates[digest(parameters)] = {
            "id": digest(parameters),
            "parameters": parameters,
            "metrics": score,
            "prior_distance": prior_distance(parameters, model["register"]),
        }
    baseline_mae = baseline["metrics"]["mae_kwh_m2"]
    if baseline_mae is None:
        raise ValueError("Subdivision selection requires a finite parent baseline MAE")
    eligible = [
        candidate
        for candidate in candidates.values()
        if candidate["metrics"]["mae_kwh_m2"] is not None
        and candidate["metrics"]["mae_kwh_m2"] <= baseline_mae + 1e-12
    ]
    shortlist = sorted(
        eligible,
        key=lambda candidate: (
            -candidate["metrics"]["coverage20"],
            candidate["metrics"]["mae_kwh_m2"],
            abs(candidate["metrics"]["bias_kwh_m2"]),
            candidate["prior_distance"],
            candidate["id"],
        ),
    )[:3]
    if baseline not in shortlist:
        shortlist.append(baseline)
    designs = {candidate["id"]: candidate["parameters"] for candidate in shortlist}
    native = evaluator.batch(rows, designs)
    confirmed = [
        candidate
        | {
            "metrics": metrics(
                rows,
                [native[row["building_id"], candidate["id"]] for row in rows],
            )
        }
        for candidate in shortlist
    ]
    native_baseline = next(
        candidate for candidate in confirmed if candidate["id"] == "parent"
    )
    confirmed = [
        candidate
        for candidate in confirmed
        if candidate["metrics"]["failed"] <= native_baseline["metrics"]["failed"]
    ]
    winner = select_candidate(confirmed, native_baseline)
    result = winner | {
        "training_properties": sorted({row["property_key"] for row in rows}),
        "active_construction_controls": controls,
        "fitting_block": block,
        "candidate_construction_reset_to_prior": resets,
        "native_parent_parameters": dict(parent_parameters),
        "operating_parameters_preserved": True,
        "native_finalists": [
            {
                "id": item["id"],
                "parameters": item["parameters"],
                "metrics": item["metrics"],
            }
            for item in confirmed
        ],
        "classification": "inferred_effective_child_construction_not_observed_renovation",
    }
    atomic_json(output, cleaned(result))
    return result


def predict_children(rows, profile, parent, evaluator):
    """Apply frozen descriptor assignment; extrapolating rows inherit their parent."""
    from .mf_parent_dynamic import predict_parent

    labels, distances = assign(physical_rows(rows), profile["grouping"])
    by_settings = {}
    assignments = []
    for row, label, distance in zip(rows, labels, distances, strict=True):
        parameters = _parent_settings(row["era"], parent)
        fallback = distance > profile["grouping"]["distance_limits"][label]
        if not fallback:
            parameters = profile["children"][str(label)]["parameters"]
        key = digest(parameters)
        by_settings.setdefault(key, (parameters, []))[1].append(row)
        assignments.append(
            {
                "building_id": row["building_id"],
                "child": label,
                "distance": distance,
                "parent_fallback": fallback,
            }
        )
    predictions = {}
    for parameters, members in by_settings.values():
        values = predict_parent(
            members, {"pooled": parameters, "by_era": {}}, evaluator
        )
        predictions.update(
            {
                row["building_id"]: value
                for row, value in zip(members, values, strict=True)
            }
        )
    return [predictions[row["building_id"]] for row in rows], assignments


def _inner_gate(
    root, outer, era, rows, parent, forbidden, store, evaluator, models, *, inner_folds
):
    """Use existing inner parent fits, never an outer-trained validation baseline."""
    from .mf_parent_dynamic import predict_parent

    # Parent inner folds were assigned across the entire outer training panel.
    # Reassigning just this era would not match those frozen parent profiles.
    inner = inner_folds
    scored_rows, baseline_values, child_values, audits = [], [], [], []
    scope = parent.get("scope", "pooled")
    if scope not in {"pooled", "era"}:
        raise ValueError("Unknown frozen parent fitting scope")
    for fold in range(3):
        training = [row for row in rows if inner[row["property_key"]] != fold]
        validation = [row for row in rows if inner[row["property_key"]] == fold]
        if not validation:
            return {
                "passed": False,
                "reason": "empty_inner_validation_era",
                "inner_fold": fold,
            }
        if _support(training) < 2 * MINIMUM_PROPERTIES:
            return {
                "passed": False,
                "reason": "inner_training_properties_below_60",
                "inner_fold": fold,
            }
        model, audit = _partition(training)
        if model is None:
            return {
                "passed": False,
                "reason": "inner_partition_rejected",
                "inner_fold": fold,
                "audit": audit,
            }
        parent_path = root / "fits" / f"outer-{outer}" / f"inner-{fold}-{scope}.json"
        inner_parent = read(parent_path)
        fitting_properties = set(inner_parent.get("training_properties", []))
        excluded = set(forbidden) | {row["property_key"] for row in validation}
        if fitting_properties & excluded:
            raise ValueError(
                "Existing inner parent fit includes subdivision validation properties"
            )
        folder = root / "subdivisions" / f"outer-{outer}" / era / f"inner-{fold}"
        children = {}
        for label in range(K):
            members = [
                row
                for row, assigned in zip(training, model["labels"], strict=True)
                if assigned == label
            ]
            observed = store.training(members, excluded)
            children[str(label)] = fit_child(
                observed,
                excluded,
                _parent_settings(era, inner_parent),
                evaluator,
                models,
                output=folder / f"child-{label}.json",
            )
        profile = {
            "grouping": model,
            "children": children,
            "training_properties": sorted({row["property_key"] for row in training}),
            "evaluation_properties": sorted(
                {row["property_key"] for row in validation}
            ),
            "parent_profile": str(parent_path),
            "stability": audit,
        }
        frozen = folder / "frozen.json"
        atomic_json(frozen, cleaned(profile))
        baseline = predict_parent(validation, inner_parent, evaluator)
        candidate, assignments = predict_children(
            validation, profile, inner_parent, evaluator
        )
        observed = store.evaluation(validation, frozen)
        scored_rows.extend(observed)
        baseline_values.extend(baseline)
        child_values.extend(candidate)
        audits.append(
            {
                "inner_fold": fold,
                "partition": audit,
                "training_properties": profile["training_properties"],
                "validation_properties": profile["evaluation_properties"],
                "assignment_parent_fallbacks": sum(
                    item["parent_fallback"] for item in assignments
                ),
                "frozen_profile": str(frozen),
            }
        )
    before, after = (
        metrics(scored_rows, baseline_values),
        metrics(scored_rows, child_values),
    )
    reasons = []
    if (
        before["mae_kwh_m2"] is None
        or after["mae_kwh_m2"] is None
        or before["mae_kwh_m2"] <= 0
        or after["mae_kwh_m2"] > (1 - MINIMUM_MAE_GAIN) * before["mae_kwh_m2"] + 1e-12
    ):
        reasons.append("inner_validation_mae_gain_below_5_percent")
    if after["coverage20"] < before["coverage20"] - 1e-12:
        reasons.append("inner_validation_coverage_decreased")
    if after["failed"] > before["failed"]:
        reasons.append("candidate_native_failures_increased")
    return {
        "passed": not reasons,
        "reasons": reasons,
        "inner_folds": audits,
        "baseline": before,
        "candidate": after,
        "selection_uses_outer_targets": False,
    }


def evaluate_subdivisions(root, evaluator, models):
    """Evaluate k=2 era children after parent fits; unsupported eras inherit parent."""
    from .mf_parent_dynamic import SEED, predict_parent

    root = Path(root).resolve()
    destination = root / "subdivision-oof.json"
    if destination.exists():
        return read(destination)
    rows = read(root / "features.json")
    parent_oof = read(root / "parent-oof.json")
    selected_scopes = {
        fold["outer_fold"]: fold["selected_scope"]
        for fold in parent_oof.get("folds", [])
    }
    folds = property_folds(rows, 5, seed=SEED)
    store = TargetStore(root)
    predictions = dict(parent_oof["predictions"])
    audits, frozen_profiles = [], []
    for outer in range(5):
        training = [row for row in rows if folds[row["property_key"]] != outer]
        evaluation = [row for row in rows if folds[row["property_key"]] == outer]
        forbidden = {row["property_key"] for row in evaluation}
        parent = read(root / "fits" / f"outer-{outer}" / "frozen.json")
        # A baseline winner can omit plan.scope, while nested parent selection
        # still records which parent family generated that frozen fit.
        parent = parent | {
            "scope": selected_scopes.get(outer, parent.get("scope", "pooled"))
        }
        if set(parent.get("training_properties", [])) & forbidden:
            raise ValueError("Existing parent fit includes outer evaluation properties")
        inner_folds = property_folds(training, 3, seed=SEED + outer + 1)
        for era in sorted({row["era"] for row in training}):
            members = [row for row in training if row["era"] == era]
            test = [row for row in evaluation if row["era"] == era]
            audit = {
                "outer_fold": outer,
                "era": era,
                "training_properties": _support(members),
                "evaluation_properties": _support(test),
            }
            if not test or _support(members) < 90:
                audits.append(
                    audit
                    | {
                        "selected": False,
                        "reason": "insufficient_support_for_three_inner_training_folds",
                    }
                )
                continue
            inner = _inner_gate(
                root,
                outer,
                era,
                members,
                parent,
                forbidden,
                store,
                evaluator,
                models,
                inner_folds=inner_folds,
            )
            if not inner["passed"]:
                audits.append(audit | {"selected": False, "inner_gate": inner})
                continue
            model, partition = _partition(members)
            if model is None:
                audits.append(
                    audit
                    | {
                        "selected": False,
                        "inner_gate": inner,
                        "reason": "outer_training_partition_rejected",
                        "partition": partition,
                    }
                )
                continue
            folder = root / "subdivisions" / f"outer-{outer}" / era
            children = {}
            for label in range(K):
                subset = [
                    row
                    for row, assigned in zip(members, model["labels"], strict=True)
                    if assigned == label
                ]
                children[str(label)] = fit_child(
                    store.training(subset, forbidden),
                    forbidden,
                    _parent_settings(era, parent),
                    evaluator,
                    models,
                    output=folder / f"child-{label}.json",
                )
            profile = {
                "grouping": model,
                "children": children,
                "training_properties": sorted({row["property_key"] for row in members}),
                "evaluation_properties": sorted({row["property_key"] for row in test}),
                "era": era,
                "parent_profile": str(root / "fits" / f"outer-{outer}" / "frozen.json"),
                "partition": partition,
                "inner_gate": inner,
            }
            frozen = folder / "frozen.json"
            atomic_json(frozen, cleaned(profile))
            values, assignments = predict_children(test, profile, parent, evaluator)
            baseline = predict_parent(test, parent, evaluator)
            actual = store.evaluation(test, frozen)
            predictions.update(
                {
                    row["building_id"]: value
                    for row, value in zip(test, values, strict=True)
                }
            )
            frozen_profiles.append(str(frozen))
            audits.append(
                audit
                | {
                    "selected": True,
                    "inner_gate": inner,
                    "partition": partition,
                    "outer_comparison": paired_change(actual, baseline, values),
                    "assignments": assignments,
                    "frozen_profile": str(frozen),
                }
            )
        atomic_json(
            root / "subdivision-progress.json",
            {
                "completed_outer_folds": outer + 1,
                "total_outer_folds": 5,
                "selected_era_folds": sum(
                    item.get("selected", False) for item in audits
                ),
            },
        )
    frozen_marker = root / "subdivision-evaluation-frozen.json"
    atomic_json(
        frozen_marker,
        {
            "candidate_predictions_sha256": digest(predictions),
            "frozen_profiles": frozen_profiles,
            "outer_targets_used_for_selection": False,
        },
    )
    observed = store.evaluation(rows, frozen_marker)
    baseline = [parent_oof["predictions"][row["building_id"]] for row in rows]
    candidate = [predictions[row["building_id"]] for row in rows]
    result = {
        "schema_version": 1,
        "predictions": predictions,
        "comparison": paired_change(observed, baseline, candidate),
        "adoption": adoption_gate(observed, baseline, candidate, kind="parent"),
        "audits": audits,
        "support": {
            "selected_era_folds": sum(item.get("selected", False) for item in audits),
            "fold_status_counts": dict(
                Counter(
                    "selected" if item.get("selected") else "parent_inherited"
                    for item in audits
                )
            ),
        },
        "assignment": {
            "k": K,
            "scope": "within_era",
            "physical_fields": list(PHYSICAL_FIELDS),
            "optional_renovation_fields_used": False,
            "ventilation_used_for_clustering": False,
            "global_parent_operation_preserved": True,
            "extrapolation": "parent beyond child training distance95 limit",
        },
        "thresholds": {
            "training_properties_per_child": MINIMUM_PROPERTIES,
            "stability_resamples": STABILITY_RESAMPLES,
            "median_ari": MINIMUM_ARI,
            "inner_validation_mae_gain": MINIMUM_MAE_GAIN,
            "preserve_inner_coverage": True,
        },
        "classification": "retrospective_physical_subdivision_development_comparison",
        "confirmation": False,
        "deployment": {
            "status": "development_candidate_only",
            "selected_default_assignment_includes_children": False,
            "full_training_child_profiles_exported": False,
            "required_for_deployment": [
                "Refit and freeze supported child partitions on the full development panel",
                "Export child construction profiles and target-free descriptor assignment",
                "Integrate explicitly adopted children into selected default assignments",
            ],
        },
    }
    atomic_json(destination, cleaned(result))
    return result
