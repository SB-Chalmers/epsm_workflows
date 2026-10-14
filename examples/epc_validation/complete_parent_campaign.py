"""Complete a source-frozen parent campaign; a launch is not a validation result.

References: docs/cohort-transfer.md and approved parent-first protocol. The runner
records its real PID and terminal errors, executes baseline, nested validation and
final export, then produces a release assessment. All numerical predictions are
native Core outputs; the reported refit is not an independent validation dataset.
"""

from __future__ import annotations

import argparse
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from threadpoolctl import threadpool_limits

from epsm_workflows.campaign import atomic_json, file_sha256
from epsm_workflows.cohort_transfer.common import cleaned, guard, read


def assessment(root, report_root):
    """Report physical hypotheses and out-of-fold misses without refitting them."""
    features = {r["building_id"]: r for r in read(root / "features.json")}
    predictions = read(root / "predictions.json")
    scores = read(root / "evaluation.json")
    rows = []
    for record in predictions:
        target = record["target"]
        value = record["deployed_prediction"]
        error = (value - target) / target * 100 if value is not None else None
        feature = features[record["building_id"]]
        rows.append(
            {
                **record,
                "signed_error_percent": error,
                "below20": error is not None and abs(error) < 20,
                "below10": error is not None and abs(error) < 10,
                "baseline_prediction": record["predictions"]["baseline"],
                "fixed_descriptors": {
                    k: feature.get(k)
                    for k in (
                        "compactness",
                        "gross_to_atemp_ratio",
                        "atemp_per_apartment",
                        "heated_basement",
                        "building_position",
                        "roof_form",
                        "renovation_timing",
                        "completed_renovation_year",
                        "completed_window_replacement",
                        "designed_flow_l_s_m2",
                    )
                },
                "boundary_unknowns": feature.get("unknowns", []),
            }
        )
    effects = []
    for path in sorted((root / "folds").rglob("sensitivity.json")):
        report = read(path)
        effects.append(
            {
                "scope": str(path.relative_to(root)),
                "effects": report["effects"],
                "active": report["active"],
                "confounding": report.get("confounding", []),
                "diagnostic_only_parameters": report.get(
                    "diagnostic_only_parameters", []
                ),
                "simulations_attempted": report.get("simulations_attempted", 0),
                "failed": report.get("failed", 0),
                "causal_identification": False,
            }
        )
    atomic_json(report_root / "building-results.json", cleaned(rows))
    atomic_json(report_root / "sensitivity-atlas.json", cleaned(effects))
    lines = [
        "# MF SE.02 parent archetype validation",
        "",
        "Fixed observations; GIS-led membership; five outer and three inner property folds.",
        "Results are retrospective procedure validation. Exact full-data coefficients are not independently validated.",
        "",
        "| Method | Below 20% | Below 10% | MAE kWh/m²/year | Bias kWh/m²/year | Failed |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for report in scores.values():
        n = report["selected"]["attempted"]
        for method, score in list(report["methods"].items()) + [
            ("inner-selected", report["selected"])
        ]:
            mae = score["mae_kwh_m2"]
            bias = score["bias_kwh_m2"]
            lines.append(
                f"| {method} | {round(score['coverage20'] * n)}/{n} | {round(score['coverage10'] * n)}/{n} | {mae:.3f} | {bias:+.3f} | {score['failed']} |"
            )
        lines.extend(
            [
                "",
                f"Release assessment: **{report['status']}**; reason: {report['reason']}.",
                f"Independent properties: {report['selected']['independent_properties']}; release requires at least 30 and at least 90% below20%.",
                f"Improved: {report['improved']}; worsened: {report['worsened']}.",
                f"Property-bootstrap 95% below20% interval: {report['uncertainty']['coverage20_95_interval']}.",
            ]
        )
    bundles = read(root / "export/cohort-profiles.json")
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "Normal-use people/equipment/lighting, main-space temperature and clock placement were screened but excluded from fitting.",
            "Remaining calibrated changes are bounded effective assumptions; parameter response and annual agreement do not identify actual renovations or leakage.",
            "Geometry, Atemp and weather were not fitted. Existing observed context supplies shading; unknown heated contacts were not turned into party walls.",
            "All misses and numerical failures remain in building coverage. Descriptor-domain fallbacks remain in the same denominator.",
            f"Exported profile status: {[p['status'] for b in bundles for p in b['profiles']]}. Provisional profiles require explicit opt-in; normal assignment retains the reference parent.",
            "The per-building results and sensitivity atlas identify unresolved heterogeneity for follow-up; outer-fold residuals were not used to revise the search.",
            "A Gothenburg cohort cannot establish accuracy elsewhere in Sweden. Other matrix cells are inventoried separately and require their own campaigns.",
        ]
    )
    report_root.mkdir(parents=True, exist_ok=True)
    (report_root / "assessment.md").write_text("\n".join(lines) + "\n")
    return scores


def complete(args):
    from finish_cohort_transfer import finish
    from run_cohort_baseline import run as baseline

    from epsm_workflows.cohort_transfer.calibration import run
    from epsm_workflows.cohort_transfer.inventory import inventory

    root = Path(args.campaign).resolve()
    report_root = Path(args.report_root).resolve()
    started = time.monotonic()
    identity = {
        "pid": os.getpid(),
        "campaign": str(root),
        "helper_sha256": file_sha256(__file__),
        "started_utc": datetime.now(UTC).isoformat(),
    }

    def state(stage, **extra):
        atomic_json(
            root / "execution.json",
            identity
            | {
                "stage": stage,
                "updated_utc": datetime.now(UTC).isoformat(),
                "elapsed_seconds": time.monotonic() - started,
            }
            | extra,
        )
        print("Execution stage", stage, flush=True)

    try:
        guard(root)
        config = read(root / "config.json")
        if args.previous:
            from epsm_workflows.cohort_transfer.rerun import verify

            verify(args.previous, root)
        state("baseline")
        with threadpool_limits(limits=2):
            if not (root / "baseline-evaluation.json").is_file():
                baseline(root, config["workers"])
            if args.previous:
                from epsm_workflows.cohort_transfer.rerun import compare

                compare(args.previous, root, baseline_only=True)
            state("nested_property_validation")
            if not (root / "evaluation.json").is_file():
                run(root)
            state("final_selection_and_export")
            finish(
                SimpleNamespace(
                    campaign=str(root),
                    study=args.study,
                    notes=args.notes,
                    wait=False,
                    timeout=1,
                    producer_log=None,
                )
            )
            inventory(
                args.study,
                root / "artifacts/matrix-support.json",
                [root],
                membership_audit=args.membership_audit,
            )
            state("release_assessment")
            report = assessment(root, report_root)
            if args.previous:
                compare(args.previous, root)
        guard(root)
        atomic_json(
            report_root / "implementation-identity.json",
            identity
            | {
                "campaign_manifest_sha256": file_sha256(root / "manifest.json"),
                "evaluation_sha256": file_sha256(root / "evaluation.json"),
                "all_building_predictions_accounted": True,
                "normalised_targets_not_refitted": True,
            },
        )
        state("completed", results=report)
        return report
    except BaseException as exc:
        state("failed", error=f"{type(exc).__name__}: {exc}")
        raise


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--campaign", required=True)
    p.add_argument("--study", required=True)
    p.add_argument("--membership-audit", required=True)
    p.add_argument("--report-root", required=True)
    p.add_argument("--notes", default="docs/archetype-implementation-log.md")
    p.add_argument(
        "--previous",
        help="Archived campaign for fixed-property rerun checks and comparisons",
    )
    complete(p.parse_args())
