"""Verify and compare a fixed-property Data 1.5 rerun against its archived study.

This is retrospective comparison, not an independent confirmation. Failure
coverage uses every attempted building; paired error changes use complete pairs.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from epsm_core import BuildingSpec

from ..campaign import atomic_json, file_sha256
from .common import SEED, bootstrap, digest, metrics, property_folds, read


def indexed(records):
    result = {r["building_id"]: r for r in records}
    if len(result) != len(records):
        raise ValueError("Duplicate building identity")
    return result


def verify(previous, campaign):
    """Reject population, protocol or unrelated physical changes before running."""
    previous, campaign = Path(previous), Path(campaign)
    old = indexed(read(previous / "features.json"))
    new = indexed(read(campaign / "features.json"))
    if old.keys() != new.keys():
        raise ValueError("Rerun building population differs")
    old_config, config = read(previous / "config.json"), read(campaign / "config.json")
    for key in old_config.keys() | config.keys():
        if key not in {"note"} and old_config.get(key) != config.get(key):
            raise ValueError(f"Rerun protocol differs: {key}")
    folds = property_folds(
        list(new.values()), config["outer_folds"], seed=config["seed"]
    )
    if folds != read(previous / "outer-folds.json"):
        raise ValueError("Rerun property folds differ")
    for name in ("membership-audit.json", "preparation-failures.json"):
        if read(previous / name) != read(campaign / name):
            raise ValueError(f"Rerun eligibility differs: {name}")
    old_register = read(previous / "parameter-register.json")
    register = read(campaign / "parameter-register.json")
    old_targets = {p.name: read(p) for p in (previous / "targets").glob("*.json")}
    targets = {p.name: read(p) for p in (campaign / "targets").glob("*.json")}
    if old_targets != targets:
        raise ValueError("Rerun observations differ")
    checks = []
    for bid in sorted(new):
        a, b = old[bid], new[bid]
        for key in ("property_key", "status", "atemp_m2", "locked_parameters"):
            if a[key] != b[key]:
                raise ValueError(f"Rerun input differs: {bid}: {key}")
        if a["status"] != "prepared":
            raise ValueError("This rerun requires the original fully prepared cohort")
        before = BuildingSpec.model_validate(read(previous / a["spec_path"]))
        after = BuildingSpec.model_validate(read(campaign / b["spec_path"]))
        x, y = before.model_dump(mode="json"), after.model_dump(mode="json")
        for key in (
            "envelope_infiltration_ach",
            "envelope_infiltration_l_s_m2",
            "infiltration_source",
        ):
            x["airflow"].pop(key, None)
            y["airflow"].pop(key, None)
        x.pop("provenance", None)
        y.pop("provenance", None)
        if x != y:
            raise ValueError(f"Unrelated physical input changed: {bid}")
        if (
            before.facade.glazing.u_value_w_m2k != 2.2
            or after.facade.glazing.u_value_w_m2k != 2.2
        ):
            raise ValueError(f"Expected the already corrected window U=2.2: {bid}")
        if (
            before.airflow.envelope_infiltration_ach != 0.4
            or after.airflow.envelope_infiltration_ach != 0
            or after.airflow.envelope_infiltration_l_s_m2 != 0.20
            or after.load_reference_area_m2 != b["atemp_m2"]
        ):
            raise ValueError(f"Incorrect Data 1.5 airflow handoff: {bid}")
        if old_register[bid].keys() != register[bid].keys():
            raise ValueError(f"Parameter controls changed: {bid}")
        for name in register[bid]:
            for key in ("bounds", "status", "baseline"):
                if old_register[bid][name].get(key) != register[bid][name].get(key):
                    raise ValueError(f"Parameter search changed: {bid}: {name}: {key}")
        flow = after.airflow.infiltration_design_flow_m3_s(
            reference_area_m2=b["atemp_m2"],
            volume_m3=after.geometry.conditioned_volume_m3,
        )
        if not np.isclose(flow, 0.20 * b["atemp_m2"] / 1000, rtol=1e-12):
            raise ValueError(f"Infiltration flow conservation failed: {bid}")
        checks.append(
            {"building_id": bid, "infiltration_m3_s": flow, "window_u_w_m2k": 2.2}
        )
    report = {
        "buildings": len(new),
        "independent_properties": len(folds),
        "property_folds_sha256": digest(folds),
        "unchanged_observations": True,
        "unchanged_non_airflow_physics": True,
        "unchanged_parameter_search": True,
        "window_correction_already_in_previous": True,
        "native_cache_imported": False,
        "previous_manifest_sha256": file_sha256(previous / "manifest.json"),
        "campaign_manifest_sha256": file_sha256(campaign / "manifest.json"),
        "airflow_checks": checks,
    }
    atomic_json(campaign / "artifacts/rerun-integrity.json", report)
    return report


def paired_change(rows, before, after, resamples=1000):
    """Pair by building and resample properties, retaining missing predictions."""
    paired = [
        i
        for i, (a, b) in enumerate(zip(before, after, strict=True))
        if a is not None and b is not None
    ]
    losses = [
        abs(after[i] - rows[i]["target"]) - abs(before[i] - rows[i]["target"])
        for i in paired
    ]
    properties = {}
    for i, row in enumerate(rows):
        properties.setdefault(row["property_key"], []).append(i)
    keys = sorted(properties)
    rng = np.random.default_rng(SEED)
    draws = []
    for _ in range(resamples):
        ids = [
            i
            for key in rng.choice(keys, len(keys), replace=True)
            for i in properties[key]
        ]
        a = metrics([rows[i] for i in ids], [before[i] for i in ids])
        b = metrics([rows[i] for i in ids], [after[i] for i in ids])
        pair_ids = [i for i in ids if before[i] is not None and after[i] is not None]
        delta = (
            np.mean(
                [
                    abs(after[i] - rows[i]["target"])
                    - abs(before[i] - rows[i]["target"])
                    for i in pair_ids
                ]
            )
            if pair_ids
            else np.nan
        )
        draws.append(
            (
                b["coverage20"] - a["coverage20"],
                b["coverage10"] - a["coverage10"],
                delta,
            )
        )
    ci = np.nanquantile(draws, [0.025, 0.975], axis=0)
    return {
        "complete_pairs": len(paired),
        "improved": sum(x < 0 for x in losses),
        "worsened": sum(x > 0 for x in losses),
        "unchanged": sum(x == 0 for x in losses),
        "new_failures": sum(
            a is not None and b is None for a, b in zip(before, after, strict=True)
        ),
        "recovered_failures": sum(
            a is None and b is not None for a, b in zip(before, after, strict=True)
        ),
        "paired_mae_change_kwh_m2": float(np.mean(losses)) if losses else None,
        "coverage20_change_95_interval": ci[:, 0].tolist(),
        "coverage10_change_95_interval": ci[:, 1].tolist(),
        "paired_mae_change_95_interval": ci[:, 2].tolist() if losses else None,
        "resampling_unit": "property",
        "resamples": resamples,
    }


def compare(previous, campaign, *, baseline_only=False):
    previous, campaign = Path(previous), Path(campaign)
    verify(previous, campaign)
    old = indexed(read(previous / "predictions.json"))
    baseline = indexed(read(campaign / "baseline-predictions-before-targets.json"))
    if old.keys() != baseline.keys():
        raise ValueError("Baseline predictions omit attempted buildings")
    ids = sorted(old)
    rows = [old[bid] for bid in ids]
    predictions = {
        "v10_baseline": [r["predictions"]["baseline"] for r in rows],
        "data15_baseline": [baseline[bid]["prediction"] for bid in ids],
        "v10_calibrated": [r["deployed_prediction"] for r in rows],
    }
    if not baseline_only:
        final = indexed(read(campaign / "predictions.json"))
        if final.keys() != old.keys():
            raise ValueError("Calibrated predictions omit attempted buildings")
        for bid in ids:
            if (
                final[bid]["target"] != old[bid]["target"]
                or final[bid]["property_key"] != old[bid]["property_key"]
            ):
                raise ValueError("Final prediction observation changed")
        predictions["data15_calibrated"] = [
            final[bid]["deployed_prediction"] for bid in ids
        ]
    n = read(campaign / "config.json")["bootstrap_resamples"]
    pairs = [("v10_baseline", "data15_baseline")]
    if not baseline_only:
        pairs += [
            ("data15_baseline", "data15_calibrated"),
            ("v10_calibrated", "data15_calibrated"),
        ]
    report = {
        "retrospective": True,
        "independent_confirmation": False,
        "stage": "baseline_complete" if baseline_only else "complete",
        "methods": {
            k: {"metrics": metrics(rows, v), "uncertainty": bootstrap(rows, v, n)}
            for k, v in predictions.items()
        },
        "changes": {
            f"{a}_to_{b}": paired_change(rows, predictions[a], predictions[b], n)
            for a, b in pairs
        },
        "buildings": [
            {
                "building_id": bid,
                "property_key": old[bid]["property_key"],
                "target": old[bid]["target"],
                **{k: v[i] for k, v in predictions.items()},
            }
            for i, bid in enumerate(ids)
        ],
    }
    atomic_json(campaign / "artifacts/rerun-comparison.json", report)
    lines = [
        "# Data 1.5 MF SE.02 rerun",
        "",
        "Same 85 buildings and 54 properties; retrospective comparison. Window U=2.2 was already present in v10.",
        "",
        "| Method | Below20% | Below10% | MAE | Bias | Failed |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, result in report["methods"].items():
        m = result["metrics"]
        lines.append(
            f"| {name} | {round(m['coverage20'] * m['attempted'])}/{m['attempted']} | {round(m['coverage10'] * m['attempted'])}/{m['attempted']} | {m['mae_kwh_m2']:.3f} | {m['bias_kwh_m2']:+.3f} | {m['failed']} |"
        )
    lines += [
        "",
        "MAE/bias units: kWh/m²/year. Coverage includes all attempts; paired error changes use complete pairs. Property-bootstrap intervals, worsened buildings and failure changes are retained in rerun-comparison.json.",
        "",
        "Data 1.5 starting values used earlier development observations; fresh independent properties are required for confirmation.",
    ]
    if baseline_only:
        lines += [
            "",
            "Calibration is pending. No updated profile or calibrated accuracy claim is available.",
        ]
    else:
        evaluation = read(campaign / "evaluation.json")
        lines += [
            "",
            f"New release status: {[r['status'] for r in evaluation.values()]}. Exact full-data refits are not independently validated.",
        ]
    (campaign / "artifacts/rerun-comparison.md").write_text("\n".join(lines) + "\n")
    return {
        "stage": report["stage"],
        "methods": report["methods"],
        "changes": report["changes"],
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--previous", required=True)
    p.add_argument("--campaign", required=True)
    p.add_argument("--verify-only", action="store_true")
    p.add_argument("--baseline-only", action="store_true")
    args = p.parse_args()
    result = (
        verify(args.previous, args.campaign)
        if args.verify_only
        else compare(args.previous, args.campaign, baseline_only=args.baseline_only)
    )
    print({k: v for k, v in result.items() if k != "airflow_checks"})


if __name__ == "__main__":
    main()
