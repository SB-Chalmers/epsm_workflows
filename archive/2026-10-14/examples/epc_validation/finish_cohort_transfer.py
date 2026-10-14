"""Finish reporting a source-frozen native cohort-transfer experiment.

Reference: docs/cohort-transfer.md. Only completed property-CV predictions grant
reported accuracy; the all-data refit is not independently validated. This helper
can wait for the existing producer's atomic evaluation, then export and document
results without changing the frozen simulation or statistical protocol.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path


def finish(args):
    from plot_cohort_transfer import render

    from epsm_workflows.campaign import atomic_json, file_sha256
    from epsm_workflows.cohort_transfer.calibration import export
    from epsm_workflows.cohort_transfer.common import guard, read
    from epsm_workflows.cohort_transfer.inventory import inventory

    root = Path(args.campaign).resolve()
    status = root / "completion.json"
    started = time.monotonic()
    identity = {
        "campaign": str(root),
        "helper_sha256": file_sha256(__file__),
        "started_utc": datetime.now(UTC).isoformat(),
    }
    try:
        while not (root / "evaluation.json").exists():
            if not args.wait:
                raise ValueError("The native transfer experiment has not completed")
            if time.monotonic() - started > args.timeout:
                raise TimeoutError("Waiting for the transfer experiment timed out")
            if args.producer_log:
                with Path(args.producer_log).open("rb") as stream:
                    stream.seek(0, 2)
                    stream.seek(max(0, stream.tell() - 8192))
                    if b"Traceback (most recent call last)" in stream.read():
                        raise RuntimeError("Producer failed; inspect its retained log")
            progress = (
                read(root / "progress.json")
                if (root / "progress.json").exists()
                else {}
            )
            atomic_json(
                status,
                identity | {"stage": "waiting_for_evaluation", "progress": progress},
            )
            time.sleep(10)
        guard(root)
        features = read(root / "features.json")
        predictions = read(root / "predictions.json")
        if {r["building_id"] for r in features} != {
            r["building_id"] for r in predictions
        } or len(features) != len(predictions):
            raise ValueError(
                "Completed predictions must retain every attempted building"
            )
        atomic_json(status, identity | {"stage": "exporting_final_profiles"})
        bundles = export(root)
        inventory(args.study, root / "artifacts/matrix-support.json", [root])
        render(root, root / "artifacts/building-errors.png")
        evaluation = read(root / "evaluation.json")
        lines = [f"## Completed cohort transfer: {root.name}", ""]
        for parent, report in evaluation.items():
            lines.extend([f"Parent: `{parent}`.", ""])
            for name, result in [
                ("baseline", report["methods"]["baseline"]),
                ("single representative", report["methods"]["single"]),
                ("shared", report["methods"]["shared"]),
                ("inner-selected procedure", report["selected"]),
            ]:
                n = result["attempted"]
                mae = result["mae_kwh_m2"]
                bias = result["bias_kwh_m2"]
                lines.append(
                    f"- {name}: {round(result['coverage20'] * n)}/{n} below20%; "
                    f"{round(result['coverage10'] * n)}/{n} below10%; "
                    f"MAE {mae if mae is None else round(mae, 3)}; "
                    f"bias {bias if bias is None else round(bias, 3)} kWh/m²/year; "
                    f"{result['failed']} failed."
                )
            lines.extend(
                [
                    "",
                    (
                        f"Status: {report['status']}; reason: {report['reason']}. "
                        f"{report['selected']['independent_properties']} independent properties; "
                        f"{report['improved']} improved and {report['worsened']} worsened predictions."
                    ),
                    (
                        f"Property-bootstrap 95% below20% interval: {report['uncertainty']['coverage20_95_interval']}; "
                        f"below10% interval: {report['uncertainty']['coverage10_95_interval']}."
                    ),
                    "",
                ]
            )
        profiles = [p for bundle in bundles for p in bundle["profiles"]]
        lines.extend(
            [
                (
                    f"Final refit profiles: {len(profiles)}; supported: "
                    f"{sum(p['status'] == 'supported' for p in profiles)}. "
                    "Final selection repeats the validated inner procedure."
                ),
                (
                    "Retrospective property validation on existing observations; no new building data. "
                    "Effective calibrated assumptions do not identify actual retrofit, operation or heated extent. "
                    "Exact all-data refits are not independently validated; geographic transfer remains untested. "
                    "Other use/era cells without measured campaigns remain pending."
                ),
                (
                    f"Evaluation SHA-256: `{file_sha256(root / 'evaluation.json')}`. "
                    f"Campaign: `{root}`."
                ),
                "",
            ]
        )
        prose = "\n".join(lines)
        (root / "artifacts/completion.md").write_text(prose)
        if args.notes:
            notes = Path(args.notes)
            existing = notes.read_text()
            if lines[0] not in existing:
                notes.write_text(existing + "\n" + prose)
        atomic_json(
            status,
            identity
            | {
                "stage": "complete",
                "completed_utc": datetime.now(UTC).isoformat(),
                "attempted_buildings": len(features),
                "evaluation_sha256": file_sha256(root / "evaluation.json"),
                "profiles_sha256": file_sha256(root / "export/cohort-profiles.json"),
                "report_sha256": hashlib.sha256(prose.encode()).hexdigest(),
            },
        )
        print(json.dumps(read(status), indent=2), flush=True)
    except Exception as exc:
        atomic_json(
            status,
            identity | {"stage": "failed", "reason": f"{type(exc).__name__}: {exc}"},
        )
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--study", required=True)
    parser.add_argument("--notes")
    parser.add_argument("--producer-log")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--timeout", type=float, default=43200)
    finish(parser.parse_args())


if __name__ == "__main__":
    main()
