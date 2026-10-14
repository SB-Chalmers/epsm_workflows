"""Frozen, property-separated physical archetype comparison across building uses.

References: docs/archetype-implementation-log.md; EPSM Data application resolver;
Core's versioned Sveby profiles and delivered-space-heat accounting. This workflow
accepts explicit complete BuildingSpec files: ingestion and project observations
must be reviewed before preparation. It never infers missing school calendars,
heated extent or delivery losses from heating residuals. Development selection is
one common scenario per declared family, not each building's best-fitting state.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import platform
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import numpy as np

from .campaign import atomic_json, file_sha256

REQUIRED_EVIDENCE = (
    "energy_boundary",
    "heated_extent",
    "exposed_walls",
    "foundation",
    "attic",
    "operating_assumptions",
    "measurement_year_systems",
)


def _read(path):
    """Read a local JSON artifact; source text is data, not workflow instructions."""
    return json.loads(Path(path).read_text())


def _hash(value):
    """Hash canonical JSON for immutable design/selection receipts."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _implementations():
    """Freeze runtime Python and packaged physical/operational resources."""
    import epsm_core
    import epsm_data

    paths = [Path(__file__), Path(__file__).with_name("campaign.py")]
    for module in (epsm_core, epsm_data):
        root = Path(module.__file__).parent
        paths += sorted(root.rglob("*.py")) + sorted(root.rglob("*.json"))
    identities = {str(p): file_sha256(p) for p in paths}
    identities["runtime:python"] = platform.python_version()
    for package in (
        "epsm_core",
        "epsm_data",
        "numpy",
        "pydantic",
        "shapely",
        "pyproj",
        "duckdb",
        "scipy",
    ):
        try:
            identities[f"distribution:{package}"] = version(package)
        except PackageNotFoundError:
            identities[f"distribution:{package}"] = "not_installed"
    return identities


def _guard(study):
    """Reject source/design edits after preparation, including opened holdouts."""
    root = Path(study)
    manifest = _read(root / "manifest.json")
    if _hash(_read(root / "design.json")) != manifest["design_sha256"]:
        raise ValueError("Prepared design changed; start a new study")
    if _implementations() != manifest["implementations"]:
        raise ValueError("Runtime changed since preparation; start a new study")
    for path, expected in manifest["sources"].items():
        if file_sha256(path) != expected:
            raise ValueError(f"Prepared source changed: {path}")
    if (root / "selection.json").exists():
        selection = _read(root / "selection.json")
        if (
            _hash(selection)
            != _read(root / "selection-complete.json")["selection_sha256"]
        ):
            raise ValueError("Frozen shared selection changed; start a new study")
        development = [v for v in _receipts(root).values() if v["split"] != "test"]
        if _hash(development) != selection["development_receipts_sha256"]:
            raise ValueError("Development simulation receipts changed after selection")
    if (root / "evaluation-opened.json").exists():
        frozen = _read(root / "evaluation-opened.json")
        receipts = [v for v in _receipts(root).values() if v["split"] == "test"]
        if _hash(receipts) != frozen["test_receipts_sha256"]:
            raise ValueError("Test receipts changed after opening held-out energy")
    return root, _read(root / "design.json")


def prepare(config_path, study):
    """Freeze explicitly evidenced specs and property splits without reading targets.

    Each building has baseline/candidate complete specs, an independent property
    key, a declared family, an EPC Atemp denominator and measurement year. Target
    CSV is hashed without interpreting its values. Source calendars, observations,
    weather and delivery must already be reflected in the supplied specs.
    """
    from epsm_core import BuildingSpec

    root = Path(study).resolve()
    if root.exists():
        raise FileExistsError(root)
    config_path = Path(config_path).resolve()
    config = _read(config_path)
    if (
        config.get("schema_version") != 1
        or config.get("target_boundary") != "delivered_space_heat"
    ):
        raise ValueError("Schema 1 currently compares delivered space heating only")
    if config.get("target_normalization") not in ("normal_use", "actual_use"):
        raise ValueError("Declare normal_use or actual_use target normalization")
    if config.get("weather_boundary") != "measurement_year_before_weather_correction":
        raise ValueError(
            "This protocol requires measurement-year, pre-weather-correction targets"
        )
    if config.get("study_kind", "diagnostic") not in ("diagnostic", "confirmation"):
        raise ValueError("Unknown study_kind")
    if config.get("selection_objective", "mae") not in ("mae", "coverage20_then_mae"):
        raise ValueError("Unsupported selection objective")
    buildings = config["buildings"]
    resamples = config.get("bootstrap_resamples", 1000)
    if not isinstance(resamples, int) or isinstance(resamples, bool) or resamples < 1:
        raise ValueError("bootstrap_resamples must be a positive integer")
    if not buildings or len({r["building_id"] for r in buildings}) != len(buildings):
        raise ValueError("Building IDs must be nonempty and unique")
    properties, frozen, sources, blocked = {}, [], {}, []
    for row in buildings:
        if (
            not str(row.get("family", "")).strip()
            or not str(row["building_id"]).strip()
        ):
            raise ValueError("Explicit nonempty building ID and family required")
        if (
            row["split"] not in ("train", "validation", "test")
            or not row["property_key"]
        ):
            raise ValueError("Explicit property and split required")
        if (
            row["property_key"] in properties
            and properties[row["property_key"]] != row["split"]
        ):
            raise ValueError("Buildings on one property cannot cross splits")
        properties[row["property_key"]] = row["split"]
        if row.get("previously_examined", False) and row["split"] != "train":
            raise ValueError(
                "Previously examined properties belong to development training"
            )
        area = float(row["atemp_m2"])
        if not np.isfinite(area) or area <= 0:
            raise ValueError("Atemp must be finite and positive")
        if row.get("operation_basis") != config["target_normalization"]:
            raise ValueError("Operational basis must match target normalization")
        evidence = row.get("evidence", {})
        missing = [
            key for key in REQUIRED_EVIDENCE if not str(evidence.get(key, "")).strip()
        ]
        if missing:
            blocked.append(
                {"building_id": row["building_id"], "missing_evidence": missing}
            )
        cases = []
        for scenario, name in sorted(row["specs"].items()):
            path = (config_path.parent / name).resolve()
            spec = BuildingSpec.model_validate_json(path.read_text())
            if spec.climate.calendar_year != row["measurement_year"]:
                raise ValueError(
                    "Simulation year must match the target measurement year"
                )
            if spec.delivery.heating is None:
                raise ValueError(
                    "Delivered-heat comparison needs an explicit Core heating delivery model"
                )
            reference_area = (
                spec.load_reference_area_m2 or spec.geometry.conditioned_floor_area_m2
            )
            if not np.isclose(reference_area, area, rtol=1e-9, atol=1e-6):
                raise ValueError(
                    "Spec load reference area must match the declared EPC Atemp"
                )
            if not scenario or not row.get("scenario_sources", {}).get(scenario):
                raise ValueError(
                    "Every scenario requires a source or declared hypothesis"
                )
            weather = Path(spec.climate.epw_path).resolve()
            if file_sha256(weather) != spec.climate.weather_sha256:
                raise ValueError("Weather checksum mismatch")
            sources[str(path)] = file_sha256(path)
            sources[str(weather)] = file_sha256(weather)
            cases.append(
                {
                    "scenario": scenario,
                    "spec_path": str(path),
                    "physical_hash": spec.physical_hash(),
                    "source": row["scenario_sources"][scenario],
                }
            )
        if not any(c["scenario"] == "baseline" for c in cases):
            raise ValueError("Every building needs a frozen baseline")
        frozen.append({**row, "cases": cases})
    for family in {r["family"] for r in frozen}:
        panels = [
            {c["scenario"] for c in r["cases"]} for r in frozen if r["family"] == family
        ]
        if any(x != panels[0] for x in panels):
            raise ValueError(
                "A family must have the same declared scenarios on every building"
            )
    if config.get("study_kind", "diagnostic") == "confirmation" and blocked:
        raise ValueError(f"Confirmation evidence missing: {blocked}")
    if config.get("study_kind", "diagnostic") == "confirmation":
        if len(frozen) < 120:
            raise ValueError(
                "Confirmation retains the original minimum of 120 building observations"
            )
        # Sample sufficiency must be predeclared by the study, not inferred from
        # favourable errors. These minima do not constitute statistical power.
        minima = config.get("minimum_independent_properties", {})
        for family in {r["family"] for r in frozen}:
            for split in ("train", "validation", "test"):
                minimum = minima.get(split)
                if (
                    not isinstance(minimum, int)
                    or isinstance(minimum, bool)
                    or minimum < 2
                ):
                    raise ValueError(
                        "Confirmation requires predeclared property minima >=2 per split"
                    )
                observed = {
                    r["property_key"]
                    for r in frozen
                    if r["family"] == family and r["split"] == split
                }
                if len(observed) < minimum:
                    raise ValueError(
                        f"Insufficient independent properties: {family}/{split}"
                    )
    target = (config_path.parent / config["targets_csv"]).resolve()
    sources[str(target)] = file_sha256(target)
    sources[str(config_path)] = file_sha256(config_path)
    design = {
        **config,
        "buildings": frozen,
        "targets_csv": str(target),
        "evidence_blockers": blocked,
        "test_energy_read": False,
    }
    root.mkdir(parents=True)
    atomic_json(root / "design.json", design)
    atomic_json(
        root / "manifest.json",
        {
            "design_sha256": _hash(design),
            "sources": sources,
            "implementations": _implementations(),
            "catalogue_changed": False,
        },
    )
    return design


def simulate(study, *, energyplus, splits=("train", "validation")):
    """Run strict Core cases, cache accepted/failed receipts and retain all attempts.

    Test simulations require a frozen common-family selection. Targets are never
    read here. A smoke failure stops further cases for that building; it does not
    exclude the building or authorize relaxed numerical/geometry warning rules.
    """
    from epsm_core import BuildingSpec, EnergyPlusRunner

    root, design = _guard(study)
    if "test" in splits and not (root / "selection.json").exists():
        raise ValueError("Freeze shared scenario choices before test simulations")
    runner = EnergyPlusRunner(energyplus=energyplus)
    engine = {
        "engine_hash": runner.compiler.engine_hash,
        "schema_hash": runner.compiler.schema_hash,
    }
    engine_path = root / "engine.json"
    if engine_path.exists() and _read(engine_path) != engine:
        raise ValueError("Engine changed since study simulation began")
    if not engine_path.exists():
        atomic_json(engine_path, engine)
    for row in design["buildings"]:
        if row["split"] not in splits:
            continue
        stopped = False
        for case in sorted(
            row["cases"], key=lambda c: (c["scenario"] != "baseline", c["scenario"])
        ):
            spec = BuildingSpec.model_validate_json(Path(case["spec_path"]).read_text())
            key = _hash(
                (row["building_id"], case["scenario"], case["physical_hash"], engine)
            )
            directory = root / "runs" / key
            receipt = directory / "receipt.json"
            if receipt.exists():
                record = _read(receipt)
            else:
                directory.mkdir(parents=True, exist_ok=True)
                record = {
                    "building_id": row["building_id"],
                    "scenario": case["scenario"],
                    "split": row["split"],
                    "physical_hash": case["physical_hash"],
                }
                if stopped:
                    record.update(status="not_attempted_after_smoke_failure")
                else:
                    try:
                        result = runner.run(spec, directory / "energyplus")
                        record.update(
                            status="accepted",
                            engine_run_id=result.run_id,
                            scalars=result.scalars,
                            monthly=result.diagnostics["monthly_energy_kwh"],
                        )
                    except Exception as exc:  # noqa: BLE001 -- retain every failed physical attempt
                        record.update(status="failed", reason=str(exc))
                atomic_json(receipt, record)
            if case["scenario"] == "baseline" and record["status"] != "accepted":
                stopped = True
    return [_read(p) for p in sorted((root / "runs").glob("*/receipt.json"))]


def _targets(design, allowed):
    """Parse only permitted target values; unselected rows' values remain unread."""
    targets = {}
    with Path(design["targets_csv"]).open(newline="") as stream:
        for row in csv.DictReader(stream):
            bid = row["building_id"]
            if bid not in allowed:
                continue
            if bid in targets:
                raise ValueError("Duplicate permitted building target")
            target = float(row["delivered_space_heat_kwh"])
            if not np.isfinite(target) or target <= 0:
                raise ValueError(
                    "Delivered space-heat target must be finite and positive"
                )
            targets[bid] = target
    if set(targets) != set(allowed):
        raise ValueError("Some permitted buildings lack target observations")
    return targets


def _receipts(root):
    """Require unambiguous receipts; engine changes cannot select cached winners."""
    records = [_read(p) for p in sorted((root / "runs").glob("*/receipt.json"))]
    keys = [(x["building_id"], x["scenario"]) for x in records]
    if len(set(keys)) != len(keys):
        raise ValueError("Multiple engine receipts for one case; use a new study")
    return dict(zip(keys, records))


def select(study):
    """Choose one shared scenario per family using training-building outcomes.

    Every scenario is compared on the identical complete paired training panel.
    coverage20_then_mae prioritizes strictly <20% absolute annual building error,
    then area-normalized MAE. The historical default remains MAE.
    Ties retain baseline. Families with any missing/failed planned case retain
    baseline rather than rewarding scenarios for dropping difficult buildings.
    The returned choice is a predictive hypothesis, not an accepted catalogue.
    """
    root, design = _guard(study)
    if (root / "evaluation-opened.json").exists():
        raise ValueError("Holdout already opened; new tuning requires a new study")
    if (root / "selection.json").exists():
        return _read(root / "selection.json")
    rows = [r for r in design["buildings"] if r["split"] == "train"]
    targets = _targets(design, {r["building_id"] for r in rows})
    records = _receipts(root)
    for row in design["buildings"]:
        if row["split"] != "test" and any(
            (row["building_id"], c["scenario"]) not in records for c in row["cases"]
        ):
            raise ValueError(
                "Complete every development simulation attempt before selection"
            )
    choices = {}
    for family in sorted({r["family"] for r in design["buildings"]}):
        panel = [r for r in rows if r["family"] == family]
        scenarios = (
            sorted(
                set.intersection(*({c["scenario"] for c in r["cases"]} for r in panel))
            )
            if panel
            else []
        )
        scores = {}
        coverage = {}
        bias = {}
        complete = bool(panel) and all(
            records.get((r["building_id"], s), {}).get("status") == "accepted"
            for r in panel
            for s in scenarios
        )
        if complete:
            for scenario in scenarios:
                errors = [
                    abs(
                        records[r["building_id"], scenario]["scalars"][
                            "space_heating_carrier_input_kwh"
                        ]
                        - targets[r["building_id"]]
                    )
                    / r["atemp_m2"]
                    for r in panel
                ]
                scores[scenario] = float(np.mean(errors))
                bias[scenario] = float(np.mean([
                    (records[r["building_id"], scenario]["scalars"]["space_heating_carrier_input_kwh"]
                     - targets[r["building_id"]]) / r["atemp_m2"] for r in panel
                ]))
                coverage[scenario] = float(
                    np.mean(
                        [
                            error * r["atemp_m2"] / targets[r["building_id"]] < 0.20
                            for error, r in zip(errors, panel)
                        ]
                    )
                )
        selected = (
            min(
                scores,
                key=lambda s: (
                    -coverage[s]
                    if design.get("selection_objective", "mae") == "coverage20_then_mae"
                    else 0,
                    scores[s],
                    abs(bias[s]),
                    s != "baseline",
                    s,
                ),
            )
            if scores
            else "baseline"
        )
        choices[family] = {
            "scenario": selected,
            "training_scores_mae_kwh_m2": scores,
            "training_coverage20": coverage,
            "training_bias_kwh_m2": bias,
            "selection_objective": design.get("selection_objective", "mae"),
            "status": "frozen_shared_hypothesis"
            if complete
            else "baseline_retained_incomplete_panel",
        }
    selection = {
        "design_sha256": _hash(design),
        "choices": choices,
        "test_energy_read": False,
        "development_receipts_sha256": _hash(
            [v for v in records.values() if v["split"] != "test"]
        ),
    }
    atomic_json(root / "selection.json", selection)
    atomic_json(
        root / "selection-complete.json", {"selection_sha256": _hash(selection)}
    )
    return selection


def evaluate(study, *, split="validation"):
    """Report paired per-building errors and property-bootstrap uncertainty.

    Test targets open only after choices and all test attempts are frozen. Failure
    counts refer to the complete attempted panel. Absolute percentage thresholds
    are calculated per building, never from average signed bias. Small diagnostic
    cohorts do not certify population coverage or promote catalogue corrections.
    """
    if split not in ("validation", "test"):
        raise ValueError("Evaluation requires validation or test split")
    root, design = _guard(study)
    selection = _read(root / "selection.json")
    if selection["design_sha256"] != _hash(design):
        raise ValueError("Selection and design differ")
    rows = [r for r in design["buildings"] if r["split"] == split]
    if not rows:
        raise ValueError("No buildings in the requested split")
    records = _receipts(root)
    for row in rows:
        for scenario in {c["scenario"] for c in row["cases"]}:
            if (row["building_id"], scenario) not in records:
                raise ValueError(
                    "Complete all evaluation case attempts before opening targets"
                )
    if split == "test":
        receipt = {
            "selection_sha256": _hash(selection),
            "design_sha256": _hash(design),
            "test_receipts_sha256": _hash(
                [v for v in records.values() if v["split"] == "test"]
            ),
        }
        opened = root / "evaluation-opened.json"
        if opened.exists() and _read(opened) != receipt:
            raise ValueError("Frozen choices changed after opening test energy")
        if not opened.exists():
            atomic_json(opened, receipt)
    targets = _targets(design, {r["building_id"] for r in rows})
    panel = []
    for row in rows:
        chosen = selection["choices"][row["family"]]["scenario"]
        baseline = records[row["building_id"], "baseline"]
        candidate = records[row["building_id"], chosen]
        item = {
            "building_id": row["building_id"],
            "property_key": row["property_key"],
            "family": row["family"],
            "atemp_m2": row["atemp_m2"],
            "target_kwh": targets[row["building_id"]],
            "scenario": chosen,
            "split": split,
            "status": "accepted"
            if baseline["status"] == candidate["status"] == "accepted"
            else "failed_pair",
        }
        if item["status"] == "accepted":
            for label, result in (("baseline", baseline), ("selected", candidate)):
                prediction = result["scalars"]["space_heating_carrier_input_kwh"]
                error = prediction - targets[row["building_id"]]
                item[label] = {
                    "prediction_kwh": prediction,
                    "signed_error_kwh_m2": error / row["atemp_m2"],
                    "absolute_error_kwh_m2": abs(error) / row["atemp_m2"],
                    "absolute_percentage_error": 100
                    * abs(error)
                    / targets[row["building_id"]],
                }
        else:
            item["failure_receipts"] = [baseline, candidate]
        panel.append(item)
    result = {
        "split": split,
        "attempted_buildings": len(panel),
        "per_building": panel,
        "accepted_pairs": sum(x["status"] == "accepted" for x in panel),
        "failed_pairs": sum(x["status"] != "accepted" for x in panel),
        "catalogue_changed": False,
        "accuracy_claim": "exploratory physical comparison",
    }
    for label in ("baseline", "selected"):
        good = [x for x in panel if x["status"] == "accepted"]
        result[label] = {
            "mae_kwh_m2": float(
                np.mean([x[label]["absolute_error_kwh_m2"] for x in good])
            )
            if good
            else None,
            "below_20_percent": sum(
                x[label]["absolute_percentage_error"] < 20 for x in good
            ),
            "below_10_percent": sum(
                x[label]["absolute_percentage_error"] < 10 for x in good
            ),
        }
    for label in ("baseline", "selected"):
        result[label]["below_20_fraction_attempted"] = result[label][
            "below_20_percent"
        ] / len(panel)
        result[label]["below_10_fraction_attempted"] = result[label][
            "below_10_percent"
        ] / len(panel)
    result["by_family"] = {}
    for family in sorted({x["family"] for x in panel}):
        attempted = [x for x in panel if x["family"] == family]
        accepted = [x for x in attempted if x["status"] == "accepted"]
        result["by_family"][family] = {
            "attempted": len(attempted),
            "accepted_pairs": len(accepted),
            "selected_mae_kwh_m2": float(
                np.mean([x["selected"]["absolute_error_kwh_m2"] for x in accepted])
            )
            if accepted
            else None,
            "buildings_below_20_percent": sum(
                x["selected"]["absolute_percentage_error"] < 20 for x in accepted
            ),
            "buildings_below_10_percent": sum(
                x["selected"]["absolute_percentage_error"] < 10 for x in accepted
            ),
        }
    good = [x for x in panel if x["status"] == "accepted"]
    if good:
        for label in ("baseline", "selected"):
            errors = np.array([x[label]["signed_error_kwh_m2"] for x in good])
            result[label]["bias_kwh_m2"] = float(errors.mean())
            result[label]["median_absolute_percentage_error"] = float(
                np.median([x[label]["absolute_percentage_error"] for x in good])
            )
            result[label]["below_20_fraction_accepted"] = result[label][
                "below_20_percent"
            ] / len(good)
            result[label]["below_10_fraction_accepted"] = result[label][
                "below_10_percent"
            ] / len(good)
            result[label]["below_20_fraction_attempted"] = result[label][
                "below_20_percent"
            ] / len(panel)
            result[label]["below_10_fraction_attempted"] = result[label][
                "below_10_percent"
            ] / len(panel)
        # Property resampling preserves all buildings of each selected property.
        props = np.array([x["property_key"] for x in good])
        unique = np.unique(props)
        rng = np.random.default_rng(20261003)
        draws = []
        for _ in range(int(design.get("bootstrap_resamples", 1000))):
            chosen = rng.choice(unique, len(unique), replace=True)
            indices = np.concatenate([np.flatnonzero(props == prop) for prop in chosen])
            selected = np.array(
                [good[i]["selected"]["absolute_error_kwh_m2"] for i in indices]
            )
            baseline = np.array(
                [good[i]["baseline"]["absolute_error_kwh_m2"] for i in indices]
            )
            percentages = np.array(
                [good[i]["selected"]["absolute_percentage_error"] for i in indices]
            )
            draws.append(
                [
                    selected.mean(),
                    (selected - baseline).mean(),
                    np.mean(percentages < 20),
                    np.mean(percentages < 10),
                ]
            )
        intervals = np.quantile(draws, [0.025, 0.975], axis=0)
        result["property_bootstrap"] = {
            "independent_properties": len(unique),
            "resamples": len(draws),
            "intervals_95_percent": dict(
                zip(
                    (
                        "selected_mae_kwh_m2",
                        "paired_mae_difference_kwh_m2",
                        "below_20",
                        "below_10",
                    ),
                    intervals.T.tolist(),
                )
            ),
            "interpretation": "Diagnostic panel; no population guarantee",
        }
    atomic_json(root / f"{split}-evaluation.json", result)
    return result


def main(argv=None):
    """Expose the prepared-design, simulation, shared-selection and evaluation stages."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="stage", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--config", required=True)
    prep.add_argument("--study", required=True)
    run = sub.add_parser("simulate")
    run.add_argument("--study", required=True)
    run.add_argument("--energyplus", required=True)
    run.add_argument("--split", choices=("development", "test"), default="development")
    choose = sub.add_parser("select")
    choose.add_argument("--study", required=True)
    score = sub.add_parser("evaluate")
    score.add_argument("--study", required=True)
    score.add_argument("--split", choices=("validation", "test"), default="validation")
    args = parser.parse_args(argv)
    if args.stage == "prepare":
        result = prepare(args.config, args.study)
    elif args.stage == "simulate":
        result = simulate(
            args.study,
            energyplus=args.energyplus,
            splits=("test",) if args.split == "test" else ("train", "validation"),
        )
    elif args.stage == "select":
        result = select(args.study)
    else:
        result = evaluate(args.study, split=args.split)
    print(
        json.dumps(
            {"stage": args.stage, "study": args.study, "complete": result is not None}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
