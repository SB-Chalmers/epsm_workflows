"""Nested property-separated selection of transferable physical profiles.

References: docs/cohort-transfer.md; source-pinned parameter register. Sensitivity
uses simulated responses only. Representatives use descriptors only. Annual
targets select at most three common controls; one-building fitting uses one.
Confounding is reported explicitly, rather than labelled causal identification.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.stats import qmc

from ..campaign import atomic_json
from .common import (
    PARENT,
    SEED,
    bootstrap,
    cleaned,
    digest,
    guard,
    metrics,
    parent_key,
    property_folds,
    read,
)
from .grouping import assign, fit_group, representatives, stability
from .parameters import morris_design
from .preparation import TargetStore
from .simulation import Evaluator, quadratic_emulator


def shared_register(rows, registers):
    """Share assumptions where applicable; preserve absent roles and input locks."""
    result = {}
    names = sorted({name for row in rows for name in registers[row["building_id"]]})
    for name in names:
        members = [registers[r["building_id"]][name] for r in rows]
        applicable = [
            entry
            for entry in members
            if entry["status"] == "screen_and_cohort_calibration"
        ]
        if not applicable:
            continue
        lo = max(entry["bounds"][0] for entry in applicable)
        hi = min(entry["bounds"][1] for entry in applicable)
        if lo < hi:
            first = next(
                (entry for entry in applicable if lo <= entry["baseline"] <= hi), None
            )
            if first is None:
                continue  # No common evidenced prior; retain actual row baselines.
            result[name] = first | {
                "bounds": [lo, hi],
                "applicable_buildings": len(applicable),
                "locked_or_inapplicable_buildings": len(members) - len(applicable),
                "scope": "shared assumption applied only to unlocked applicable physical roles",
            }
    return result


def screen(rows, evaluator, config, output):
    """Morris mean/signed/spread effects, per-building response and confounding."""
    model = fit_group(rows, 1)
    reps = representatives(rows, model, 0)
    register = shared_register(rows, evaluator.registers)
    designs, moves = morris_design(
        register, config["morris_trajectories"], config["seed"]
    )
    if not register:
        report = {
            "active": [],
            "effects": {},
            "representatives": [],
            "register": register,
        }
        atomic_json(output, report)
        return report
    values = evaluator.batch(reps, designs)
    effects = defaultdict(list)
    by_building = defaultdict(lambda: defaultdict(list))
    for row in reps:
        for move in moves:
            before, after = (
                values[row["building_id"], move["before"]],
                values[row["building_id"], move["after"]],
            )
            if before is None or after is None:
                continue
            change = (after - before) / move["normalized_step"]
            effects[move["parameter"]].append(change)
            by_building[row["building_id"]][move["parameter"]].append(change)
    summary = {
        name: {
            "mu": float(np.mean(values)),
            "mu_star": float(np.mean(np.abs(values))),
            "sigma": float(np.std(values)),
            "responses": len(values),
        }
        for name, values in effects.items()
    }
    # EPC normal-use-adjusted targets do not justify changing the standard-use
    # operating inputs to absorb envelope error. Still report their sensitivity.
    # References: archetype_diagnostics.FORM_EVIDENCE; Core Sveby resources.
    diagnostic_only = set(config.get("calibration_excluded_parameters", ()))
    names = sorted(
        (name for name in summary if name not in diagnostic_only),
        key=lambda n: (-summary[n]["mu_star"], n),
    )
    active, confounding = [], []
    for name in names:
        if summary[name]["mu_star"] < 1e-3:
            continue
        vector = np.array(
            [
                np.mean(by_building[r["building_id"]][name])
                if by_building[r["building_id"]][name]
                else np.nan
                for r in reps
            ]
        )
        correlated = None
        vectors = [vector]
        for selected in active:
            vectors.append(
                np.array(
                    [
                        np.mean(by_building[r["building_id"]][selected])
                        if by_building[r["building_id"]][selected]
                        else np.nan
                        for r in reps
                    ]
                )
            )
        matrix = np.column_stack(vectors)
        complete = np.isfinite(matrix).all(axis=1)
        # Model differences have a fixed physical baseline: DeltaY = S DeltaTheta.
        # Centre-free rank distinguishes true collinearity from affine responses
        # (e.g. a basement effect is zero in buildings without that physical role).
        if len(vectors) > 1 and complete.sum() >= max(3, len(vectors)):
            response = matrix[complete]
            norms = np.linalg.norm(response, axis=0)
            if np.all(norms > 1e-9):
                singular = np.linalg.svd(response / norms, compute_uv=False)
                ratio = float(singular[-1] / singular[0])
                if ratio <= 0.05:
                    correlated = {
                        "parameter": name,
                        "with": active.copy(),
                        "relative_min_singular_value": ratio,
                        "criterion": "uncentred response condition number >=20",
                    }
        if correlated:
            confounding.append(correlated)
        elif len(active) < 3:
            active.append(name)
    emulator = {}
    for row in reps:
        predicted = {name: values[row["building_id"], name] for name in designs}
        emulator[row["building_id"]] = quadratic_emulator(designs, predicted)
    report = {
        "active": active,
        "effects": summary,
        "confounding": confounding,
        "representatives": [r["building_id"] for r in reps],
        "register": register,
        "designs": designs,
        "moves": moves,
        "emulator": emulator,
        "per_building_effects": {
            bid: {p: float(np.mean(v)) for p, v in columns.items()}
            for bid, columns in by_building.items()
        },
        "simulations_attempted": len(values),
        "failed": sum(v is None for v in values.values()),
        "observed_heating_used": False,
        "causal_identification": False,
        "diagnostic_only_parameters": sorted(diagnostic_only & set(summary)),
    }
    atomic_json(output, cleaned(report))
    return report


def candidate_designs(active, register, count=32, seed=SEED):
    """Bounded sparse joint settings; baseline stays a candidate at every stage."""
    designs = {"baseline": {}}
    for name in active:
        lo, hi = register[name]["bounds"]
        center = register[name]["baseline"]
        for value in sorted(set(np.linspace(lo, hi, 7).tolist() + [center])):
            settings = {
                name: float(round(value))
                if name == "schedule_shift_hours"
                else float(value)
            }
            designs[digest(settings)[:16]] = settings
    if len(active) > 1:
        for point in qmc.LatinHypercube(d=len(active), seed=seed).random(count):
            settings = {}
            for name, coordinate in zip(active, point, strict=True):
                lo, hi = register[name]["bounds"]
                value = lo + (hi - lo) * coordinate
                # Five levels bound the panel, improve cache reuse and avoid false precision.
                value = lo + round((value - lo) / (hi - lo) * 4) / 4 * (hi - lo)
                settings[name] = (
                    float(round(value))
                    if name == "schedule_shift_hours"
                    else float(value)
                )
            designs[digest(settings)[:16]] = settings
    return designs


def objective(rows, prediction, settings, register):
    """Prior-penalized robust fit; each independent property has equal influence."""
    properties = sorted({r["property_key"] for r in rows})
    losses = []
    for prop in properties:
        errors = []
        for row, pred in zip(rows, prediction, strict=True):
            if row["property_key"] == prop:
                errors.append(
                    2.0
                    if pred is None
                    else min(abs(pred - row["target"]) / row["target"], 2.0)
                )
        losses.append(float(np.mean(errors)))
    penalty = sum(
        ((value - register[name]["baseline"]) / np.ptp(register[name]["bounds"])) ** 2
        for name, value in settings.items()
    )
    # A 2-percentage-point penalty at a full-bound departure regularizes weakly
    # constrained annual-data fits without pretending to identify actual materials.
    return float(np.mean(losses)) + 0.02 * penalty


def fit_parameters(
    rows, evaluator, active, register, *, mode, designs_count=32, training_values=None
):
    """Fit supplied training targets only; one representative has one free control."""
    if mode not in ("single", "shared"):
        raise ValueError("Unknown physical calibration mode")
    selected = active[:1] if mode == "single" else active[:3]
    designs = candidate_designs(selected, register, designs_count)
    search = {"status": "direct_core", "deployed": False}
    if training_values is not None:
        panel = training_values
    elif len(selected) > 1:
        panel, search = surrogate_shortlist(
            rows, evaluator, designs, selected, register
        )
    else:
        panel = evaluator.batch(rows, designs)
    candidates = []
    for name, settings in designs.items():
        if not all((r["building_id"], name) in panel for r in rows):
            continue
        prediction = [panel.get((r["building_id"], name)) for r in rows]
        candidates.append(
            (objective(rows, prediction, settings, register), len(settings), name)
        )
    _, _, best = min(candidates)
    return {
        "parameters": designs[best],
        "candidate": best,
        "mode": mode,
        "training_properties": sorted({r["property_key"] for r in rows}),
        "free_parameters": len(designs[best]),
        "active": selected,
        "search": search,
        "fit_criterion": "property-balanced_APE_plus_0.02_prior_departure",
        "classification": "inferred_effective_assumptions",
    }


def surrogate_shortlist(rows, evaluator, designs, active, register):
    """Shortlist with verified per-building quadratics; deploy only native results.

    References: docs/cohort-transfer.md. Four joint settings, excluded from the
    quadratic fit, must have <=2% relative error for each accelerated building.
    An anchor/check failure or excessive error sends that building through the
    complete native panel. It must not invalidate verified models for unrelated
    buildings. Failed native predictions remain None and retain the fit penalty.
    The baseline and eight best candidates are evaluated natively for every row.
    """
    axis = {key: value for key, value in designs.items() if len(value) <= 1}
    joint = [(key, value) for key, value in designs.items() if len(value) > 1]
    if len(joint) < 4:
        return evaluator.batch(rows, designs), {
            "status": "direct_core",
            "deployed": False,
        }
    centers = np.array([register[name]["baseline"] for name in active])
    scales = np.array([np.ptp(register[name]["bounds"]) for name in active])

    def vector(settings):
        z = (
            np.array(
                [settings.get(name, register[name]["baseline"]) for name in active]
            )
            - centers
        ) / scales
        return np.concatenate(([1.0], z, z * z))

    panel = evaluator.batch(rows, axis)
    x = np.array([vector(settings) for settings in axis.values()])
    coefficients, rejected = {}, {}
    for row in rows:
        bid = row["building_id"]
        y = [panel[bid, name] for name in axis]
        if any(value is None for value in y):
            rejected[bid] = "anchor_failure"
        else:
            coefficients[bid] = np.linalg.lstsq(x, y, rcond=None)[0]
    checks = dict(joint[:: max(1, len(joint) // 4)][:4])
    verified_rows = [r for r in rows if r["building_id"] in coefficients]
    verified = evaluator.batch(verified_rows, checks)
    errors = []
    for row in verified_rows:
        bid = row["building_id"]
        for name, setting in checks.items():
            actual = verified[bid, name]
            if actual is None:
                rejected[bid] = "joint_check_failure"
                break
            error = abs(float(vector(setting) @ coefficients[bid]) - actual) / max(
                actual, 1e-9
            )
            errors.append(error)
            if error > 0.02:
                rejected[bid] = "joint_check_error_above_2_percent"
    panel.update(verified)
    fallback_rows = [r for r in rows if r["building_id"] in rejected]
    if fallback_rows:
        panel.update(evaluator.batch(fallback_rows, designs))
    accepted = [r for r in rows if r["building_id"] not in rejected]
    if not accepted:
        return panel, {
            "status": "direct_core_all_buildings_rejected",
            "native_fallback_buildings": rejected,
            "deployed": False,
        }
    ranked = []
    for name, settings in designs.items():
        predicted = [
            panel[r["building_id"], name]
            if r["building_id"] in rejected
            else max(0.0, float(vector(settings) @ coefficients[r["building_id"]]))
            for r in rows
        ]
        ranked.append((objective(rows, predicted, settings, register), name))
    shortlisted = dict(checks)
    shortlisted.update({name: designs[name] for _, name in sorted(ranked)[:8]})
    shortlisted["baseline"] = {}
    panel.update(evaluator.batch(rows, shortlisted))
    accepted_errors = [
        abs(
            float(vector(setting) @ coefficients[r["building_id"]])
            - verified[r["building_id"], name]
        )
        / max(verified[r["building_id"], name], 1e-9)
        for r in accepted
        for name, setting in checks.items()
    ]
    return panel, {
        "status": "validated_search_emulator",
        "maximum_relative_error": max(accepted_errors),
        "held_out_settings": len(checks),
        "ranked_settings": len(designs),
        "physically_checked_shortlist": len(shortlisted),
        "accelerated_buildings": len(accepted),
        "native_fallback_buildings": rejected,
        "maximum_screened_relative_error": max(errors) if errors else None,
        "deployed": False,
    }


def choose_method(scores):
    """Select on inner coverage, property-balanced loss, then model simplicity.

    The capped loss includes an explicit failed-case penalty, matching the fitting
    objective. It is a selection loss, not a fabricated percentage measurement.
    Legacy callers without this metric fall back to their reported MAE.
    """
    complexity = {"baseline": 0, "single": 1, "shared": 2}

    def rank(mode):
        score = scores[mode]
        loss = score.get("property_balanced_capped_ape_loss", score["mae_kwh_m2"])
        return (
            -(score["coverage20"] or 0),
            loss if loss is not None else float("inf"),
            complexity[mode],
        )

    return min(complexity, key=rank)


def inner_validate(rows, store, evaluator, config, outer_forbidden, folder):
    """Refit transforms, medoids and parameters inside every inner training fold."""
    folds = property_folds(rows, 3, seed=config["seed"] + 1)
    max_k = min(
        config["max_clusters"],
        len(folds) * 2 // 3 // config["minimum_cluster_training_properties"],
    )
    candidates = []
    for k in range(1, max(1, max_k) + 1):
        actual, predictions = (
            [],
            {mode: [] for mode in ("baseline", "single", "shared")},
        )
        trials, rejected = [], None
        for fold in range(3):
            train = [r for r in rows if folds[r["property_key"]] != fold]
            val = [r for r in rows if folds[r["property_key"]] == fold]
            if not train or not val:
                rejected = "empty_inner_fold"
                break
            try:
                model = fit_group(
                    train, k, minimum=config["minimum_cluster_training_properties"]
                )
                stable = stability(
                    train,
                    model,
                    resamples=config["cluster_resamples"],
                    minimum=config["minimum_cluster_training_properties"],
                )
                if k > 1 and (
                    stable["median_ari"] < config["minimum_cluster_ari"]
                    or model["missingness_ari"] >= 0.7
                ):
                    raise ValueError("Unstable or missingness-defined subdivision")
            except ValueError as exc:
                rejected = str(exc)
                break
            val_labels, val_distances = assign(val, model)
            fitted = {}
            for label in range(k):
                members = [
                    r for r, c in zip(train, model["labels"], strict=True) if c == label
                ]
                profile_dir = folder / f"k{k}/inner{fold}/cluster{label}"
                response = screen(
                    members, evaluator, config, profile_dir / "sensitivity.json"
                )
                forbidden = set(outer_forbidden) | {r["property_key"] for r in val}
                observed = store.training(members, forbidden)
                central = representatives(members, fit_group(members, 1), 0, limit=1)[0]
                one = [
                    r for r in observed if r["building_id"] == central["building_id"]
                ]
                fitted[label] = {
                    "baseline": {"parameters": {}},
                    "single": fit_parameters(
                        one,
                        evaluator,
                        response["active"],
                        response["register"],
                        mode="single",
                        designs_count=config["joint_designs"],
                    ),
                    "shared": fit_parameters(
                        observed,
                        evaluator,
                        response["active"],
                        response["register"],
                        mode="shared",
                        designs_count=config["joint_designs"],
                    ),
                }
            frozen = folder / f"k{k}/inner{fold}/frozen.json"
            atomic_json(
                frozen,
                {
                    "grouping": model,
                    "fits": fitted,
                    "stability": stable,
                    "training_properties": sorted({r["property_key"] for r in train}),
                    "evaluation_properties": sorted({r["property_key"] for r in val}),
                },
            )
            # Predictions precede access to the inner validation observations.
            fold_predictions = {mode: [] for mode in predictions}
            for row, label, distance in zip(
                val, val_labels, val_distances, strict=True
            ):
                for mode in predictions:
                    settings = (
                        {}
                        if distance > model["distance_limits"][label]
                        else fitted[label][mode]["parameters"]
                    )
                    fold_predictions[mode].append(evaluator.evaluate(row, settings))
            actual.extend(store.evaluation(val, frozen))
            for mode, values in predictions.items():
                values.extend(fold_predictions[mode])
            trials.append({"fold": fold, "stability": stable})
        if rejected:
            candidates.append({"k": k, "rejected": rejected})
            continue
        scores = {mode: metrics(actual, p) for mode, p in predictions.items()}
        candidates.append(
            {"k": k, "scores": scores, "method": choose_method(scores), "folds": trials}
        )
    eligible = [c for c in candidates if "rejected" not in c]
    if not eligible:
        return {
            "k": 1,
            "method": "baseline",
            "trials": candidates,
            "reason": "insufficient_inner_validation",
        }
    pooled = next(c for c in eligible if c["k"] == 1)
    base = pooled["scores"][pooled["method"]]
    qualifying = [pooled]
    for candidate in eligible:
        score = candidate["scores"][candidate["method"]]
        if (
            candidate["k"] > 1
            and score["mae_kwh_m2"] is not None
            and base["mae_kwh_m2"] is not None
            and score["mae_kwh_m2"]
            <= base["mae_kwh_m2"] * (1 - config["minimum_subdivision_mae_gain"])
            and score["coverage20"] >= base["coverage20"]
        ):
            qualifying.append(candidate)
    selected = min(
        qualifying,
        key=lambda c: (
            -c["scores"][c["method"]]["coverage20"],
            c["scores"][c["method"]]["mae_kwh_m2"] or float("inf"),
            c["k"],
        ),
    )
    return {"k": selected["k"], "method": selected["method"], "trials": candidates}


def run(root, *, evaluator=None):
    """Run all parents; isolate every outer property's complete selection path."""
    root = Path(root)
    guard(root)
    config = read(root / "config.json")
    rows = read(root / "features.json")
    evaluator = evaluator or Evaluator(root)
    store = TargetStore(root)
    folds = property_folds(rows, 5, seed=config["seed"])
    atomic_json(root / "outer-folds.json", folds)
    by_parent = defaultdict(list)
    for row in rows:
        by_parent[parent_key(row)].append(row)
    outputs, summaries = [], []
    for parent, members in sorted(by_parent.items()):
        parent_dir = root / "folds" / digest(parent)[:16]
        print("Transfer parent", parent, "buildings", len(members), flush=True)
        for fold in range(5):
            test = [r for r in members if folds[r["property_key"]] == fold]
            train = [
                r
                for r in members
                if folds[r["property_key"]] != fold and r["status"] == "prepared"
            ]
            if not test:
                continue
            folder = parent_dir / f"outer{fold}"
            forbidden = {r["property_key"] for r in test}
            if len({r["property_key"] for r in train}) >= 5:
                choice = inner_validate(
                    train, store, evaluator, config, forbidden, folder
                )
                model = fit_group(
                    train,
                    choice["k"],
                    minimum=config["minimum_cluster_training_properties"],
                )
                fits = {}
                for label in range(choice["k"]):
                    cluster = [
                        r
                        for r, c in zip(train, model["labels"], strict=True)
                        if c == label
                    ]
                    response = screen(
                        cluster,
                        evaluator,
                        config,
                        folder / f"cluster{label}/sensitivity.json",
                    )
                    observed = store.training(cluster, forbidden)
                    central = representatives(
                        cluster, fit_group(cluster, 1), 0, limit=1
                    )[0]
                    one = [
                        r
                        for r in observed
                        if r["building_id"] == central["building_id"]
                    ]
                    fits[label] = {
                        "baseline": {"parameters": {}},
                        "single": fit_parameters(
                            one,
                            evaluator,
                            response["active"],
                            response["register"],
                            mode="single",
                            designs_count=config["joint_designs"],
                        ),
                        "shared": fit_parameters(
                            observed,
                            evaluator,
                            response["active"],
                            response["register"],
                            mode="shared",
                            designs_count=config["joint_designs"],
                        ),
                    }
            else:
                choice, model, fits = (
                    {
                        "k": 1,
                        "method": "baseline",
                        "reason": "insufficient_training_properties",
                    },
                    None,
                    {},
                )
            frozen = folder / "frozen.json"
            atomic_json(
                frozen,
                {
                    "choice": choice,
                    "grouping": model,
                    "fits": fits,
                    "training_properties": sorted({r["property_key"] for r in train}),
                    "evaluation_properties": sorted(forbidden),
                    "test_targets_read": False,
                },
            )
            predictions = []
            for row in test:
                label, distance = (
                    assign([row], model)
                    if model and row["status"] == "prepared"
                    else ([0], [float("inf")])
                )
                supported = (
                    bool(model) and distance[0] <= model["distance_limits"][label[0]]
                )
                methods = {}
                for mode in ("baseline", "single", "shared"):
                    settings = fits[label[0]][mode]["parameters"] if supported else {}
                    methods[mode] = evaluator.evaluate(row, settings)
                predictions.append(
                    {
                        "building_id": row["building_id"],
                        "property_key": row["property_key"],
                        "parent": parent,
                        "fold": fold,
                        "cluster": label[0],
                        "assignment": "supported_descriptor_domain"
                        if supported
                        else "parent_fallback",
                        "distance": distance[0] if np.isfinite(distance[0]) else None,
                        "selected_method": choice["method"],
                        "predictions": methods,
                        "deployed_prediction": methods[choice["method"]],
                        "parameters": fits[label[0]][choice["method"]]["parameters"]
                        if supported
                        else {},
                    }
                )
            atomic_json(folder / "predictions-before-targets.json", predictions)
            targets = store.evaluation(test, frozen)
            for observation, prediction in zip(targets, predictions, strict=True):
                outputs.append(
                    prediction
                    | {
                        "target": observation["target"],
                        "status": observation["status"],
                        "use_group": observation["use_group"],
                        "era": observation["era"],
                    }
                )
            summaries.append(
                {
                    "parent": parent,
                    "fold": fold,
                    "choice": choice,
                    "properties": len(forbidden),
                    "train_properties": len({r["property_key"] for r in train}),
                }
            )
            atomic_json(
                root / "progress.json",
                {
                    "completed_parent_folds": len(summaries),
                    "evaluated_buildings": len(outputs),
                    "retrospective": True,
                },
            )
            print(
                "Outer fold",
                fold,
                "method",
                choice["method"],
                "k",
                choice["k"],
                "evaluated",
                len(test),
                flush=True,
            )
    if hasattr(evaluator, "close"):
        evaluator.close()
    atomic_json(root / "predictions.json", outputs)
    atomic_json(root / "selection.json", summaries)
    guard(root)
    return evaluate(root)


def evaluate(root):
    """Summarize out-of-fold physical predictions, including every failed case."""
    root = Path(root)
    guard(root)
    rows = read(root / "predictions.json")
    config = read(root / "config.json")
    groups = defaultdict(list)
    for row in rows:
        groups[row["parent"]].append(row)
    report = {}
    for parent, members in groups.items():
        methods = {
            mode: metrics(members, [r["predictions"][mode] for r in members])
            for mode in ("baseline", "single", "shared")
        }
        prediction = [r["deployed_prediction"] for r in members]
        score = metrics(members, prediction)
        supported = (
            score["independent_properties"] >= config["minimum_release_properties"]
            and score["coverage20"] >= config["target_coverage20"]
        )
        report[parent] = {
            "status": "supported" if supported else "provisional",
            "reason": None
            if supported
            else "insufficient_properties"
            if score["independent_properties"] < config["minimum_release_properties"]
            else "accuracy_target_not_met",
            "selected": score,
            "methods": methods,
            "uncertainty": bootstrap(
                members, prediction, config["bootstrap_resamples"]
            ),
            "improved": sum(
                p is not None
                and r["predictions"]["baseline"] is not None
                and abs(p - r["target"])
                < abs(r["predictions"]["baseline"] - r["target"])
                for r, p in zip(members, prediction, strict=True)
            ),
            "worsened": sum(
                p is not None
                and r["predictions"]["baseline"] is not None
                and abs(p - r["target"])
                > abs(r["predictions"]["baseline"] - r["target"])
                for r, p in zip(members, prediction, strict=True)
            ),
            "population_validation": False,
            "evaluation": config["study_kind"],
        }
    atomic_json(root / "evaluation.json", report)
    return report


def export(root, *, evaluator=None):
    """Refit final physical profiles while retaining retrospective support labels.

    Exact released coefficients are full-data refits; CV evaluated the training
    procedure, not an untouched test of these final coefficients. Unstable fold
    subdivisions never inherit a pooled parent's support automatically.
    """
    root = Path(root)
    guard(root)
    config = read(root / "config.json")
    score = read(root / "evaluation.json")
    choices = read(root / "selection.json")
    features = read(root / "features.json")
    store = TargetStore(root)
    evaluator = evaluator or Evaluator(root)
    bundles = []
    for parent in score:
        rows = [
            r for r in features if parent_key(r) == parent and r["status"] == "prepared"
        ]
        if not rows:
            continue
        selections = [c["choice"] for c in choices if c["parent"] == parent]
        final_choice = inner_validate(
            rows,
            store,
            evaluator,
            config,
            set(),
            root / "export" / digest(parent)[:16] / "selection",
        )
        model = fit_group(
            rows,
            final_choice["k"],
            minimum=config["minimum_cluster_training_properties"],
        )
        profiles = []
        for label in range(model["k"]):
            members = [
                r for r, c in zip(rows, model["labels"], strict=True) if c == label
            ]
            response = screen(
                members,
                evaluator,
                config,
                root
                / "export"
                / digest(parent)[:16]
                / f"cluster{label}-sensitivity.json",
            )
            mode = final_choice["method"]
            observations = store.training(members)
            if mode == "baseline":
                fitted = {"parameters": {}, "free_parameters": 0}
            else:
                central = representatives(members, fit_group(members, 1), 0, limit=1)[0]
                train = (
                    [
                        r
                        for r in observations
                        if r["building_id"] == central["building_id"]
                    ]
                    if mode == "single"
                    else observations
                )
                fitted = fit_parameters(
                    train,
                    evaluator,
                    response["active"],
                    response["register"],
                    mode=mode,
                    designs_count=config["joint_designs"],
                )
            # Cohort-specific support needs consistent subdivision decisions and
            # its own out-of-fold members; never grant support from aggregate MAE.
            predictions = read(root / "predictions.json")
            member_ids = {r["building_id"] for r in members}
            subset = [r for r in predictions if r["building_id"] in member_ids]
            cohort_score = metrics(subset, [r["deployed_prediction"] for r in subset])
            from sklearn.metrics import adjusted_rand_score

            partition_agreement = []
            for frozen_path in (root / "folds" / digest(parent)[:16]).glob(
                "outer*/frozen.json"
            ):
                frozen_model = read(frozen_path).get("grouping")
                if frozen_model is None:
                    partition_agreement.append(0.0)
                    continue
                full_labels, _ = assign(rows, model)
                fold_labels, _ = assign(rows, frozen_model)
                partition_agreement.append(
                    float(adjusted_rand_score(full_labels, fold_labels))
                )
            coherent = (
                all(c["k"] == model["k"] for c in selections)
                and len(partition_agreement) == config["outer_folds"]
                and min(partition_agreement) >= config["minimum_cluster_ari"]
            )
            supported = (
                coherent
                and cohort_score["independent_properties"]
                >= config["minimum_release_properties"]
                and cohort_score["coverage20"] >= config["target_coverage20"]
            )
            profiles.append(
                {
                    "id": f"{digest(parent)[:12]}:{label}",
                    "status": "supported" if supported else "provisional",
                    "medoid": model["medoids"][label],
                    "distance_limit": model["distance_limits"][label],
                    "parameters": fitted["parameters"],
                    "infiltration_basis": "atemp_area_rate"
                    if hasattr(evaluator, "spec")
                    and evaluator.spec(members[0]).airflow.envelope_infiltration_l_s_m2 is not None
                    else "core_volume_ach",
                    "parameter_scope": "apply only to unlocked applicable physical roles",
                    "bounds": {k: v["bounds"] for k, v in response["register"].items()},
                    "source": "Retrospective property-CV effective surviving-stock cohort; inferred, not measured",
                    "validation": cohort_score,
                    "consistent_fold_subdivision": coherent,
                    "partition_agreement_ari": partition_agreement,
                    "classification": "inferred_effective_assumptions",
                    "method": mode,
                    "exact_refit_independently_validated": False,
                }
            )
        bundle = {
            "schema_version": 1,
            "parent": {k: rows[0][k] for k in PARENT},
            "transform": model["transform"],
            "profiles": profiles,
            "matrix_release": "epsm-lod2-v1.4",
            "final_selection": final_choice,
            "campaign_manifest_sha256": digest(read(root / "manifest.json")),
        }
        bundles.append(bundle)
    evaluator.close()
    atomic_json(root / "export/cohort-profiles.json", bundles)
    return bundles
