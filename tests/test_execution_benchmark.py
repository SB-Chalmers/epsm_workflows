# SPDX-License-Identifier: MIT
"""Offline prepared-dispatch benchmark; no scientific engines or network.

Run this file directly with --output and optional --sizes/--batch-size to retain
a reproducible benchmark receipt. Only verification instrumentation uses a
private hook; preparation, dispatch and retained-result reads use public APIs.
"""

import argparse
import json
import shutil
import time
from pathlib import Path
from unittest.mock import patch

from epsm_workflows import PreparedEvaluation, prepare, read_results, run_local
from epsm_workflows import execution


def benchmark(directory, count, batch_size):
    """Compare standalone dispatch and batches on identical tiny prepared cases."""
    root = Path(directory)
    if count < 1 or batch_size < 1:
        raise ValueError("count and batch_size must be positive")
    if root.exists():
        raise ValueError("Use a new benchmark directory so no retained result is reused")
    source = root / "prepared"
    started = time.perf_counter()
    manifest = prepare(
        source,
        [PreparedEvaluation({"answer": index}, name=f"row-{index}") for index in range(count)],
        handler="builtins:dict",
    )
    preparation_seconds = time.perf_counter() - started
    records = []
    for mode in ("standalone", "bounded"):
        destination = root / mode
        shutil.copytree(source, destination)
        checked, case_reads = [], []
        original = execution._runtime_identity
        original_case = execution._read_case

        def observe(cases, handler):
            checked.append(len(cases))
            return original(cases, handler)

        def observe_case(root, index, captured):
            case_reads.append(index)
            return original_case(root, index, captured)

        with (
            patch.object(execution, "_runtime_identity", observe),
            patch.object(execution, "_read_case", observe_case),
        ):
            started = time.perf_counter()
            if mode == "standalone":
                for index in range(count):
                    execution.run_case(destination, index)
            else:
                for start in range(0, count, batch_size):
                    run_local(destination, indices=range(start, min(start + batch_size, count)))
            duration = time.perf_counter() - started
        rows = read_results(destination)
        assert [row["case_id"] for row in rows] == manifest["case_ids"]
        assert [row["result"]["answer"] for row in rows] == list(range(count))
        attempts = tuple((destination / "attempts").glob("*/*.json"))
        assert len(attempts) == count
        records.append({
            "mode": mode,
            "duration_s": duration,
            "full_plan_verifications": len(checked),
            "verified_case_rows": sum(checked),
            "case_file_reads": len(case_reads),
            "succeeded": len(rows),
            "attempts": len(attempts),
            "case_identities_match": True,
        })
    return {
        "cases": count, "batch_size": batch_size, "plan_id": manifest["id"],
        "preparation_s": preparation_seconds, "runtime": manifest["runtime"],
        "handler": "builtins:dict", "results": records,
    }


def test_benchmark_uses_real_verification_and_preserves_case_results(tmp_path):
    receipt = benchmark(tmp_path / "benchmark", count=8, batch_size=3)
    standalone, bounded = receipt["results"]
    assert standalone["full_plan_verifications"] == 8
    assert standalone["verified_case_rows"] == 64
    assert standalone["case_file_reads"] == 72
    assert bounded["full_plan_verifications"] == 3
    assert bounded["verified_case_rows"] == 24
    assert bounded["case_file_reads"] == 32
    assert standalone["succeeded"] == bounded["succeeded"] == 8
    assert standalone["attempts"] == bounded["attempts"] == 8
    assert receipt["runtime"]["packages"]["epsm_workflows"]["source_sha256"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sizes", type=int, nargs="+", default=[128, 512])
    parser.add_argument("--batch-size", type=int, default=32)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("--output must name a new directory")
    if args.batch_size < 1 or any(size < 1 for size in args.sizes) or len(set(args.sizes)) != len(args.sizes):
        parser.error("Provide distinct positive sizes and a positive batch size")
    report = {
        "format": "epsm-prepared-dispatch-benchmark/1",
        "scope": "Real local integrity checks and stub dispatch; no engines, network or Slurm",
        "measurements": [
            benchmark(args.output / f"cases-{size}", size, args.batch_size)
            for size in args.sizes
        ],
    }
    (args.output / "benchmark.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
