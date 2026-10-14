"""Freeze an absolute normal-use flow diagnostic for reported-zero MF systems.

This is an explicit alternative completion assumption, never an operating
measurement or a silent replacement of the baseline. Multipliers cannot
investigate a zero baseline. Run/evaluate with epsm_workflows.mf_revision.
"""

import argparse
from pathlib import Path

from epsm_core import BuildingSpec, EnergyPlusRunner
from epsm_workflows.campaign import atomic_json, file_sha256
from epsm_workflows.cohort_transfer.common import digest, read
from epsm_workflows.mf_revision import _guard, _identity, _new


def prepare(inventory, source_campaign, output):
    inventory, frozen = _guard(inventory)
    source = Path(source_campaign).resolve()
    root = _new(output)
    config = read(inventory / "config.json")
    engine = EnergyPlusRunner(energyplus=config["engine"])
    identity = {
        "engine_hash": engine.compiler.engine_hash,
        "schema_hash": engine.compiler.schema_hash,
    }
    if read(source / "engine.json") != identity:
        raise ValueError("Cannot reuse receipts from a different engine")
    if read(source / "execution.json")["status"] != "completed":
        raise ValueError("Complete the source baseline campaign first")
    sources = frozen["sources"] | {
        str(Path(__file__).resolve()): file_sha256(__file__),
        str(source / "features.json"): file_sha256(source / "features.json"),
        str(source / "engine.json"): file_sha256(source / "engine.json"),
        str(source / "manifest.json"): file_sha256(source / "manifest.json"),
    }
    plans, specs, reuse = [], {}, []
    for row in read(source / "features.json"):
        if row["ventilation"] == "S" or "operating_flow" in row.get("mf_evidence", {}):
            continue
        path = source / row["spec_path"]
        spec = BuildingSpec.model_validate(read(path))
        if spec.airflow.mechanical_vent_l_s_m2 != 0:
            continue
        if (
            spec.identifier != row["building_id"]
            or spec.load_reference_area_m2 != row["atemp_m2"]
        ):
            raise ValueError("Spec identity/Atemp mismatch")
        sources[str(path)] = file_sha256(path)
        weather = Path(spec.climate.epw_path).resolve()
        if file_sha256(weather) != spec.climate.weather_sha256:
            raise ValueError("Weather checksum changed")
        sources[str(weather)] = file_sha256(weather)
        receipt_path = (
            source / "runs" / digest((row["building_id"], "baseline")) / "receipt.json"
        )
        receipt = read(receipt_path)
        if (
            receipt["status"] != "accepted"
            or receipt["physical_hash"] != spec.physical_hash()
        ):
            raise ValueError("Unaccepted or mismatched source baseline")
        sources[str(receipt_path)] = file_sha256(receipt_path)
        for name, native in (
            ("baseline", spec),
            (
                "normal_use_flow_0.35",
                spec.updated(airflow=spec.airflow.updated(mechanical_vent_l_s_m2=0.35)),
            ),
        ):
            relative = f"specs/{row['building_id']}/{name}.json"
            specs[relative] = native.model_dump(mode="json")
            case = receipt | {
                "scenario": name,
                "spec_path": relative,
                "physical_hash": native.physical_hash(),
                "changes": {
                    "absolute_flow_l_s_m2_atemp": 0.35,
                    "classification": "alternative_normal_use_completion_not_observation",
                }
                if name != "baseline"
                else {},
            }
            for key in (
                "status",
                "prediction_kwh_m2",
                "diagnostics",
                "scalars",
                "reason",
            ):
                case.pop(key, None)
            plans.append(case)
            if name == "baseline":
                reuse.append((case, receipt, receipt_path))
    if not plans:
        raise ValueError("No accepted reported-zero MF baselines")
    root.mkdir(parents=True)
    for path, value in specs.items():
        atomic_json(root / path, value)
    atomic_json(root / "case-plan.json", plans)
    atomic_json(
        root / "config.json",
        {
            "engine": config["engine"],
            "workers": config.get("workers", 8),
            "recover_context_shadows": False,
        },
    )
    atomic_json(
        root / "hypothesis.json",
        {
            "source": "https://www.boverket.se/contentassets/3108c5069a60495380949c906e9c6f0b/bbr-18-ovk.pdf",
            "locator": "BBR18 section 6:251",
            "qualification": "Design floor-area requirement provides plausibility only. Here 0.35 is the existing EPSM Atemp normal-use completion, not a measured annual operating rate or a claim about every historical building.",
            "reported_design_zero_retained": True,
            "catalogue_changed": False,
        },
    )
    atomic_json(
        root / "manifest.json",
        {
            "sources": sources,
            "runtime": _identity(),
            "artifacts": {
                p: file_sha256(root / p)
                for p in ["case-plan.json", "config.json", "hypothesis.json", *specs]
            },
            "confirmation": False,
        },
    )
    for case, receipt, original in reuse:
        atomic_json(
            root / "runs" / digest((case["building_id"], "baseline")) / "receipt.json",
            receipt
            | case
            | {
                "reused_strict_receipt": str(original),
                "reused_receipt_sha256": file_sha256(original),
                "reused_engine": identity,
            },
        )
    return {
        "properties": len(reuse),
        "new_simulations": len(reuse),
        "confirmation": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--source-campaign", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(prepare(args.inventory, args.source_campaign, args.output))
