"""Property-separated MF development comparisons and empirical benchmarks.

This is retrospective development evaluation. Outer folds prevent fitting to a
building's own heating observation; they do not turn inspected stock into a fresh
holdout. Benchmarks predict heating, never assign an observed renovation history.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping

import numpy as np

from .cohort_transfer.common import property_folds

SEED = 20261008
NUMERIC_FEATURES = (
    "construction_year",
    "measurement_year",
    "atemp_m2",
    "footprint_area_m2",
    "footprint_perimeter_m",
    "compactness",
    "height_m",
    "storeys",
    "apartments",
    "atemp_per_apartment",
    "gross_to_atemp_ratio",
    "heated_basement_floors",
    "designed_flow_l_s_m2",
    "wall_area_per_atemp",
    "roof_area_per_atemp",
    "ground_area_per_atemp",
    "volume_per_atemp",
)
CATEGORICAL_FEATURES = (
    "era",
    "catalogue_climate_zone",
    "ventilation",
    "heated_basement",
    "roof_form",
    "building_position",
)
BENCHMARK_CANDIDATES = (
    {
        "kind": "hist_gradient_boosting",
        "max_leaf_nodes": 7,
        "min_samples_leaf": 20,
        "l2_regularization": 10.0,
        "max_iter": 120,
    },
    {
        "kind": "hist_gradient_boosting",
        "max_leaf_nodes": 15,
        "min_samples_leaf": 15,
        "l2_regularization": 1.0,
        "max_iter": 120,
    },
    {
        "kind": "random_forest",
        "n_estimators": 128,
        "min_samples_leaf": 5,
        "max_features": 0.8,
    },
    {
        "kind": "random_forest",
        "n_estimators": 128,
        "min_samples_leaf": 15,
        "max_features": 1.0,
    },
)


def _valid_prediction(value):
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float, np.number))
        and math.isfinite(value)
        and value >= 0
    )


def _targets(rows):
    values = []
    for row in rows:
        target = row.get("target")
        if not _valid_prediction(target) or target <= 0:
            raise ValueError("Heating targets must be finite and strictly positive")
        values.append(float(target))
    return np.array(values)


def _predictions(rows, predictions):
    if isinstance(predictions, Mapping):
        return [predictions.get(row["building_id"]) for row in rows]
    predictions = list(predictions)
    if len(predictions) != len(rows):
        raise ValueError("Predictions must align with the attempted panel")
    return predictions


def _arrays(rows, predictions):
    targets = _targets(rows)
    values = _predictions(rows, predictions)
    valid = np.array([_valid_prediction(value) for value in values], dtype=bool)
    numbers = np.array(
        [float(v) if okay else np.nan for v, okay in zip(values, valid, strict=True)]
    )
    errors = numbers - targets
    # Floating-point arithmetic must not turn an exact 20% boundary into a miss.
    passes = valid & (np.abs(errors) <= 0.2 * targets + 1e-12 * np.maximum(1, targets))
    return targets, numbers, valid, errors, passes


def _property(row):
    value = row.get("property_key")
    if value is None or not str(value).strip():
        raise ValueError("Independent property identity is required")
    return str(value)


def metrics(rows, predictions):
    """Inclusive ±20% coverage retains every attempted building in its denominator."""
    _, _, valid, errors, passes = _arrays(rows, predictions)
    successful = errors[valid]
    properties = {_property(row) for row in rows}
    return {
        "attempted": len(rows),
        "accepted": int(valid.sum()),
        "failed": len(rows) - int(valid.sum()),
        "independent_properties": len(properties),
        "coverage20": float(passes.mean()) if len(rows) else None,
        "mae_kwh_m2": float(np.abs(successful).mean()) if len(successful) else None,
        "bias_kwh_m2": float(successful.mean()) if len(successful) else None,
        "error_std_kwh_m2": float(successful.std()) if len(successful) else None,
        "error_iqr_kwh_m2": float(np.subtract(*np.percentile(successful, [75, 25])))
        if len(successful)
        else None,
        "error_p05_kwh_m2": float(np.percentile(successful, 5))
        if len(successful)
        else None,
        "error_p95_kwh_m2": float(np.percentile(successful, 95))
        if len(successful)
        else None,
        "mae_conditional_on_accepted": True,
    }


def paired_change(rows, baseline, candidate, *, seed=SEED, resamples=1000):
    """Property bootstrap with all attempts for coverage and complete pairs for MAE."""
    if not rows:
        raise ValueError("A comparison requires an attempted panel")
    if isinstance(resamples, bool) or not isinstance(resamples, int) or resamples < 1:
        raise ValueError("resamples must be a positive integer")
    baseline, candidate = _predictions(rows, baseline), _predictions(rows, candidate)
    _, _, bv, be, bp = _arrays(rows, baseline)
    _, _, cv, ce, cp = _arrays(rows, candidate)
    complete = bv & cv
    grouped = defaultdict(list)
    for index, row in enumerate(rows):
        grouped[_property(row)].append(index)
    properties = sorted(grouped)
    # Aggregate by independent property once; sibling buildings travel together.
    summaries = []
    for prop in properties:
        ids = np.array(grouped[prop], dtype=int)
        paired = ids[complete[ids]]
        summaries.append(
            [
                len(ids),
                int(bp[ids].sum()),
                int(cp[ids].sum()),
                len(paired),
                float(np.abs(be[paired]).sum()),
                float(np.abs(ce[paired]).sum()),
                float(be[paired].sum()),
                float(ce[paired].sum()),
                float(np.square(be[paired]).sum()),
                float(np.square(ce[paired]).sum()),
            ]
        )
    summaries = np.array(summaries)
    rng = np.random.default_rng(seed)
    indices = rng.integers(0, len(properties), size=(resamples, len(properties)))
    samples = summaries[indices].sum(axis=1)
    coverage_changes = (samples[:, 2] - samples[:, 1]) / samples[:, 0]
    has_pairs = samples[:, 3] > 0
    paired_samples = samples[has_pairs]
    if len(paired_samples):
        n = paired_samples[:, 3]
        mae_changes = (paired_samples[:, 5] - paired_samples[:, 4]) / n
        bias_changes = (paired_samples[:, 7] - paired_samples[:, 6]) / n
        bvar = np.maximum(0, paired_samples[:, 8] / n - (paired_samples[:, 6] / n) ** 2)
        cvar = np.maximum(0, paired_samples[:, 9] / n - (paired_samples[:, 7] / n) ** 2)
        spread_changes = np.sqrt(cvar) - np.sqrt(bvar)
    else:
        mae_changes = bias_changes = spread_changes = []
    paired_ids = np.flatnonzero(complete).tolist()
    pair_rows = [rows[i] for i in paired_ids]
    paired_baseline = metrics(pair_rows, [baseline[i] for i in paired_ids])
    paired_candidate = metrics(pair_rows, [candidate[i] for i in paired_ids])
    bmetric, cmetric = metrics(rows, baseline), metrics(rows, candidate)

    def interval(values):
        return np.quantile(values, [0.025, 0.975]).tolist() if len(values) else None

    return {
        "baseline": bmetric,
        "candidate": cmetric,
        "paired_baseline": paired_baseline,
        "paired_candidate": paired_candidate,
        "paired_buildings": len(paired_ids),
        "paired_properties": len({_property(rows[i]) for i in paired_ids}),
        "coverage_change": cmetric["coverage20"] - bmetric["coverage20"],
        "paired_mae_change_kwh_m2": float(
            np.abs(ce[complete]).mean() - np.abs(be[complete]).mean()
        )
        if complete.any()
        else None,
        "coverage_change_95_interval": interval(coverage_changes),
        "paired_mae_change_95_interval": interval(mae_changes),
        "paired_bias_change_95_interval": interval(bias_changes),
        "paired_error_std_change_95_interval": interval(spread_changes),
        "mae_intervals_conditional_on_accepted_pairs": True,
        "bootstrap": {
            "seed": seed,
            "resamples": resamples,
            "properties": len(properties),
        },
        "miss_to_pass": [rows[i]["building_id"] for i in np.flatnonzero(~bp & cp)],
        "pass_to_miss": [rows[i]["building_id"] for i in np.flatnonzero(bp & ~cp)],
    }


def _basement_group(row):
    value = row.get("heated_basement")
    if str(value).lower() in {"yes", "true", "1"}:
        return "reported_conditioned_basement"
    if str(value).lower() in {"no", "false", "0"}:
        return "no_reported_conditioned_basement"
    count = row.get("heated_basement_floors")
    if _valid_prediction(count):
        return (
            "reported_conditioned_basement"
            if count > 0
            else "no_reported_conditioned_basement"
        )
    return "unknown"


def _height_group(row):
    value = row.get("height_m", row.get("representative_height_m"))
    if not _valid_prediction(value) or value <= 0:
        return "unknown"
    return (
        "height_le_9m"
        if value <= 9
        else "height_9_to_18m"
        if value <= 18
        else "height_gt_18m"
    )


def adoption_gate(
    rows, baseline, candidate, *, kind="parent", seed=SEED, resamples=1000
):
    """Implement the accepted parent/dynamic gates and supported-group protections."""
    if kind not in {"parent", "dynamic"}:
        raise ValueError("kind must be parent or dynamic")
    baseline, candidate = _predictions(rows, baseline), _predictions(rows, candidate)
    comparison = paired_change(
        rows, baseline, candidate, seed=seed, resamples=resamples
    )
    before, after = comparison["paired_baseline"], comparison["paired_candidate"]
    reported_before, reported_after = comparison["baseline"], comparison["candidate"]
    reasons = []
    if not comparison["paired_buildings"]:
        reasons.append("no_complete_native_pairs")
        paired_mae_not_worse = paired_mae_gain = False
    else:
        paired_mae_not_worse = after["mae_kwh_m2"] <= before["mae_kwh_m2"] + 1e-12
        paired_mae_gain = (
            before["mae_kwh_m2"] > 0
            and after["mae_kwh_m2"] <= 0.95 * before["mae_kwh_m2"] + 1e-12
        )
    reported_mae_available = (
        reported_before["mae_kwh_m2"] is not None
        and reported_after["mae_kwh_m2"] is not None
    )
    reported_mae_not_worse = (
        reported_mae_available
        and reported_after["mae_kwh_m2"] <= reported_before["mae_kwh_m2"] + 1e-12
    )
    reported_mae_gain = (
        reported_mae_available
        and reported_before["mae_kwh_m2"] > 0
        and reported_after["mae_kwh_m2"] <= 0.95 * reported_before["mae_kwh_m2"] + 1e-12
    )
    # Equal failure counts can conceal different failed properties. Complete-pair
    # improvement alone must not certify worse MAE in the reported accepted panel.
    mae_not_worse = paired_mae_not_worse and reported_mae_not_worse
    mae_gain = paired_mae_gain and reported_mae_gain
    coverage_ci = comparison["coverage_change_95_interval"]
    positive_coverage_ci = coverage_ci is not None and coverage_ci[0] > 0
    if kind == "parent":
        eligible = (positive_coverage_ci and mae_not_worse) or (
            mae_gain and comparison["coverage_change"] >= -1e-12
        )
    else:
        eligible = (
            comparison["coverage_change"] >= 0.02 - 1e-12
            and positive_coverage_ci
            and mae_not_worse
        )
    if not eligible:
        reasons.append("overall_improvement_threshold_not_met")
    if comparison["candidate"]["failed"] > comparison["baseline"]["failed"]:
        reasons.append("candidate_native_failures_increased")
    fields = {
        "era": lambda r: str(r.get("era", "unknown")),
        "region": lambda r: str(r.get("catalogue_climate_zone", "unknown")),
        "ventilation": lambda r: str(r.get("ventilation", "unknown")),
        "basement": _basement_group,
        "height": _height_group,
    }
    protections = []
    for field, getter in fields.items():
        groups = defaultdict(list)
        for i, row in enumerate(rows):
            groups[getter(row)].append(i)
        for group, ids in sorted(groups.items()):
            subset = [rows[i] for i in ids]
            support = len({_property(r) for r in subset})
            supported = support >= 30
            bm = metrics(subset, [baseline[i] for i in ids])
            cm = metrics(subset, [candidate[i] for i in ids])
            complete = [
                i
                for i in ids
                if _valid_prediction(baseline[i]) and _valid_prediction(candidate[i])
            ]
            pbefore = metrics(
                [rows[i] for i in complete], [baseline[i] for i in complete]
            )
            pafter = metrics(
                [rows[i] for i in complete], [candidate[i] for i in complete]
            )
            coverage_loss = cm["coverage20"] < bm["coverage20"] - 0.05 - 1e-12
            paired_mae_within_limit = (
                bool(complete)
                and pafter["mae_kwh_m2"] <= 1.1 * pbefore["mae_kwh_m2"] + 1e-12
            )
            reported_mae_within_limit = (
                bm["mae_kwh_m2"] is not None
                and cm["mae_kwh_m2"] is not None
                and cm["mae_kwh_m2"] <= 1.1 * bm["mae_kwh_m2"] + 1e-12
            )
            passed = (
                not coverage_loss
                and paired_mae_within_limit
                and reported_mae_within_limit
            )
            protections.append(
                {
                    "field": field,
                    "group": group,
                    "supported": supported,
                    "independent_properties": support,
                    "baseline": bm,
                    "candidate": cm,
                    "paired_baseline": pbefore,
                    "paired_candidate": pafter,
                    "paired_mae_within_10_percent": paired_mae_within_limit,
                    "reported_mae_within_10_percent": reported_mae_within_limit,
                    "passed": passed if supported else None,
                }
            )
            if supported and not passed:
                reasons.append(f"supported_group_regressed:{field}:{group}")
    return {
        "kind": kind,
        "adopt": not reasons,
        "reasons": reasons,
        "comparison": comparison,
        "group_protections": protections,
        "evaluation_status": "retrospective_property_separated_development",
        "independent_confirmation": False,
        "group_support_basis": "At least 30 independent properties",
        "mae_guards": {
            "paired_mae_not_worse": paired_mae_not_worse,
            "reported_mae_not_worse": reported_mae_not_worse,
            "paired_mae_gain_at_least_5_percent": paired_mae_gain,
            "reported_mae_gain_at_least_5_percent": reported_mae_gain,
            "basis": "Require both complete native pairs and each method's accepted panel",
        },
    }


def _training_rows(rows, targets):
    targets = _predictions(rows, targets)
    if len({r["building_id"] for r in rows}) != len(rows):
        raise ValueError("Duplicate building identities in benchmark panel")
    result = [
        dict(row) | {"target": target}
        for row, target in zip(rows, targets, strict=True)
    ]
    _targets(result)
    for row in result:
        _property(row)
    return sorted(result, key=lambda row: row["building_id"])


def _weighted_mean(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[_property(row)].append(row["target"])
    return float(np.mean([np.mean(v) for v in grouped.values()]))


def fit_empirical_parent(rows, targets=None, *, shrinkage=20.0):
    """A small JSON-safe era/zone benchmark, fitted exclusively on training properties."""
    if targets is not None:
        rows = _training_rows(rows, targets)
    _targets(rows)
    if (
        not rows
        or not isinstance(shrinkage, (int, float))
        or not math.isfinite(shrinkage)
        or shrinkage < 0
    ):
        raise ValueError(
            "Parent benchmark requires training rows and nonnegative shrinkage"
        )
    global_mean = _weighted_mean(rows)
    eras, cells = defaultdict(list), defaultdict(list)
    for row in rows:
        era = str(row.get("era", "unknown"))
        zone = str(row.get("catalogue_climate_zone", "unknown"))
        eras[era].append(row)
        cells[(era, zone)].append(row)
    era_profiles, cell_profiles = {}, {}
    for era, members in sorted(eras.items()):
        n = len({_property(r) for r in members})
        era_profiles[era] = {
            "mean": (n * _weighted_mean(members) + shrinkage * global_mean)
            / (n + shrinkage),
            "properties": n,
        }
    for (era, zone), members in sorted(cells.items()):
        n = len({_property(r) for r in members})
        mean = (n * _weighted_mean(members) + shrinkage * era_profiles[era]["mean"]) / (
            n + shrinkage
        )
        cell_profiles[f"{era}|{zone}"] = {"mean": mean, "properties": n}
    return {
        "schema_version": "mf-empirical-parent/1",
        "global_mean": global_mean,
        "eras": era_profiles,
        "cells": cell_profiles,
        "shrinkage": float(shrinkage),
        "training_properties": sorted({_property(row) for row in rows}),
        "interpretation": "Heating benchmark, not an identified component state",
    }


def _assert_target_free(row):
    forbidden = [
        key
        for key in row
        if str(key).lower().startswith(("target", "residual", "measured_heat"))
        or "energy" in str(key).lower()
        or "demand" in str(key).lower()
        or str(key).lower() in {"heating_intensity", "performance", "prediction"}
        or ("kwh" in str(key).lower() and "heat" in str(key).lower())
    ]
    if forbidden:
        raise ValueError(
            f"Forward benchmark assignment cannot receive heating observations: {forbidden}"
        )


def predict_empirical_parent(rows, model, *, strict=True):
    predictions = []
    for row in rows:
        if strict:
            _assert_target_free(row)
        era, zone = (
            str(row.get("era", "unknown")),
            str(row.get("catalogue_climate_zone", "unknown")),
        )
        profile = model["cells"].get(f"{era}|{zone}") or model["eras"].get(era)
        predictions.append(profile["mean"] if profile else model["global_mean"])
    return predictions


def benchmark_features(rows, *, strict=True):
    """Whitelist observable descriptors; age/period/year are not treated as causal effects."""
    import pandas as pd

    records = []
    for row in rows:
        if strict:
            _assert_target_free(row)
        record = {}
        for field in NUMERIC_FEATURES:
            value = row.get(field)
            record[field] = (
                float(value)
                if isinstance(value, (int, float, np.number))
                and not isinstance(value, bool)
                and math.isfinite(value)
                else np.nan
            )
        for field in CATEGORICAL_FEATURES:
            value = row.get(field)
            record[field] = "unknown" if value is None else str(value)
        records.append(record)
    return pd.DataFrame(records, columns=NUMERIC_FEATURES + CATEGORICAL_FEATURES)


def _statistical_model(candidate, seed):
    from sklearn.compose import ColumnTransformer
    from sklearn.ensemble import HistGradientBoostingRegressor, RandomForestRegressor
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder

    candidate = dict(candidate)
    kind = candidate.pop("kind")
    transformer = ColumnTransformer(
        [
            (
                "numeric",
                SimpleImputer(
                    strategy="median", add_indicator=True, keep_empty_features=True
                ),
                list(NUMERIC_FEATURES),
            ),
            (
                "categorical",
                OneHotEncoder(handle_unknown="ignore", sparse_output=False),
                list(CATEGORICAL_FEATURES),
            ),
        ]
    )
    estimator = (
        HistGradientBoostingRegressor(random_state=seed, **candidate)
        if kind == "hist_gradient_boosting"
        else RandomForestRegressor(random_state=seed, n_jobs=1, **candidate)
    )
    return Pipeline([("features", transformer), ("regressor", estimator)])


def _fit_statistical(rows, candidate, seed):
    from threadpoolctl import threadpool_limits

    model = _statistical_model(candidate, seed)
    counts = defaultdict(int)
    for row in rows:
        counts[_property(row)] += 1
    weights = np.array([1 / counts[_property(row)] for row in rows])
    with threadpool_limits(limits=1):
        model.fit(
            benchmark_features(rows, strict=False),
            _targets(rows),
            regressor__sample_weight=weights,
        )
    return model


def _predict_statistical(model, rows):
    from threadpoolctl import threadpool_limits

    with threadpool_limits(limits=1):
        return np.maximum(
            0, model.predict(benchmark_features(rows, strict=False))
        ).tolist()


def empirical_benchmarks(rows, targets, outer_folds=5, *, seed=SEED, inner_folds=3):
    """Nested property validation of a parent mean and reliable-feature benchmark."""
    rows = _training_rows(rows, targets)
    folds = property_folds(rows, outer_folds, seed=seed)
    predictions = {"empirical_parent": {}, "reliable_features": {}}
    audits = []
    shrinkages = (100.0, 20.0, 5.0, 0.0)
    for fold in range(outer_folds):
        train = [row for row in rows if folds[_property(row)] != fold]
        test = [row for row in rows if folds[_property(row)] == fold]
        train_properties = {_property(row) for row in train}
        test_properties = {_property(row) for row in test}
        if train_properties & test_properties:
            raise AssertionError("Property isolation failed")
        n_inner = min(inner_folds, len(train_properties))
        inner_audit, parent_trials, statistical_trials = [], [], []
        if n_inner >= 2:
            inner = property_folds(train, n_inner, seed=seed + fold + 1)
            inner_specs = []
            for index in range(n_inner):
                a = [r for r in train if inner[_property(r)] != index]
                b = [r for r in train if inner[_property(r)] == index]
                inner_specs.append((a, b))
                inner_audit.append(
                    {
                        "fold": index,
                        "training_properties": sorted({_property(r) for r in a}),
                        "validation_properties": sorted({_property(r) for r in b}),
                    }
                )
            for shrinkage in shrinkages:
                values = {}
                for a, b in inner_specs:
                    model = fit_empirical_parent(a, shrinkage=shrinkage)
                    values.update(
                        zip(
                            [r["building_id"] for r in b],
                            predict_empirical_parent(b, model, strict=False),
                            strict=True,
                        )
                    )
                parent_trials.append(
                    {"shrinkage": shrinkage, "metrics": metrics(train, values)}
                )
            best_parent = min(
                range(len(parent_trials)),
                key=lambda i: (
                    -parent_trials[i]["metrics"]["coverage20"],
                    parent_trials[i]["metrics"]["mae_kwh_m2"],
                    abs(parent_trials[i]["metrics"]["bias_kwh_m2"]),
                    i,
                ),
            )
            selected_shrinkage = parent_trials[best_parent]["shrinkage"]
            for number, candidate in enumerate(BENCHMARK_CANDIDATES):
                values = {}
                for a, b in inner_specs:
                    model = _fit_statistical(a, candidate, seed + fold)
                    values.update(
                        zip(
                            [r["building_id"] for r in b],
                            _predict_statistical(model, b),
                            strict=True,
                        )
                    )
                statistical_trials.append(
                    {"candidate": dict(candidate), "metrics": metrics(train, values)}
                )
            best_statistical = min(
                range(len(statistical_trials)),
                key=lambda i: (
                    -statistical_trials[i]["metrics"]["coverage20"],
                    statistical_trials[i]["metrics"]["mae_kwh_m2"],
                    abs(statistical_trials[i]["metrics"]["bias_kwh_m2"]),
                    i,
                ),
            )
            selected_candidate = statistical_trials[best_statistical]["candidate"]
        else:
            selected_shrinkage, selected_candidate = (
                100.0,
                dict(BENCHMARK_CANDIDATES[0]),
            )
        parent = fit_empirical_parent(train, shrinkage=selected_shrinkage)
        parent_values = predict_empirical_parent(test, parent, strict=False)
        statistical = _fit_statistical(train, selected_candidate, seed + fold)
        statistical_values = _predict_statistical(statistical, test)
        for mode, values in (
            ("empirical_parent", parent_values),
            ("reliable_features", statistical_values),
        ):
            predictions[mode].update(
                zip([r["building_id"] for r in test], values, strict=True)
            )
        audits.append(
            {
                "outer_fold": fold,
                "training_properties": sorted(train_properties),
                "evaluation_properties": sorted(test_properties),
                "inner_folds": inner_audit,
                "selected_shrinkage": selected_shrinkage,
                "selected_statistical_candidate": selected_candidate,
                "parent_trials": parent_trials,
                "statistical_trials": statistical_trials,
                "parent_model": parent,
                "input_transform_fitted_on_training_only": True,
            }
        )
    return {
        "schema_version": "mf-empirical-comparison/1",
        "predictions": predictions,
        "folds": folds,
        "training_audit": audits,
        "metrics": {
            mode: metrics(rows, values) for mode, values in predictions.items()
        },
        "feature_contract": {
            "numeric": list(NUMERIC_FEATURES),
            "categorical": list(CATEGORICAL_FEATURES),
            "renovation_fields_used": False,
            "energy_features_used": False,
            "age_period_year": "Construction year and observation year are predictors; age is their difference and is not added as a third independent causal effect",
            "missing_values": "Train-only median and missingness indicators; categorical unknown",
        },
        "evaluation_status": "retrospective_nested_property_cross_validation",
        "independent_confirmation": False,
    }


def _physical_descriptors(rows):
    # Reuse the existing medoid implementation after removing its optional
    # renovation fields and assumed transmission/airflow indicators.
    from .cohort_transfer.common import CATEGORICAL, NUMERIC, PARENT

    allowed = set(NUMERIC_FEATURES + CATEGORICAL_FEATURES) | {
        "use_group",
        "heating_family",
        "correction_status",
    }
    return [
        {"building_id": row["building_id"], "property_key": _property(row)}
        | {
            key: row.get(key) if key in allowed else None
            for key in NUMERIC + CATEGORICAL + PARENT
        }
        for row in rows
    ]


def physical_subdivision(
    training_rows,
    validation_rows,
    baseline,
    candidate,
    *,
    k=2,
    resamples=20,
    minimum=30,
):
    """Gate an existing physical subdivision using independently held-out predictions.

    The caller fits native subgroup profiles inside the training properties and
    supplies their predictions on the separate inner validation properties. This
    helper does not label energy residuals as physical clusters.
    """
    from .cohort_transfer.grouping import fit_group, stability

    if isinstance(k, bool) or not isinstance(k, int) or k < 2:
        raise ValueError("A subdivision requires at least two physical children")
    if isinstance(resamples, bool) or not isinstance(resamples, int) or resamples < 1:
        raise ValueError("Subdivision stability requires positive resamples")
    if {_property(row) for row in training_rows} & {
        _property(row) for row in validation_rows
    }:
        raise ValueError("Subdivision validation must use separate properties")
    if minimum < 30:
        raise ValueError("Each physical child requires at least 30 training properties")
    try:
        descriptors = _physical_descriptors(training_rows)
        model = fit_group(descriptors, k, minimum=minimum)
        consistent = stability(descriptors, model, resamples=resamples, minimum=minimum)
    except ValueError as error:
        return {
            "supported": False,
            "reasons": ["insufficient_physical_group_support"],
            "detail": str(error),
        }
    before = metrics(validation_rows, baseline)
    after = metrics(validation_rows, candidate)
    reasons = []
    if consistent["median_ari"] < 0.7:
        reasons.append("assignment_stability_below_0.7")
    if (
        before["mae_kwh_m2"] is None
        or after["mae_kwh_m2"] is None
        or before["mae_kwh_m2"] <= 0
        or after["mae_kwh_m2"] > 0.95 * before["mae_kwh_m2"] + 1e-12
    ):
        reasons.append("inner_validation_mae_gain_below_5_percent")
    if after["coverage20"] < before["coverage20"] - 1e-12:
        reasons.append("inner_validation_coverage_decreased")
    if after["failed"] > before["failed"]:
        reasons.append("candidate_native_failures_increased")
    return {
        "supported": not reasons,
        "reasons": reasons,
        "model": model,
        "assignment_stability": consistent,
        "baseline": before,
        "candidate": after,
        "training_properties": sorted({_property(row) for row in training_rows}),
        "validation_properties": sorted({_property(row) for row in validation_rows}),
        "status": "retrospective_physical_subdivision_development_test",
    }
