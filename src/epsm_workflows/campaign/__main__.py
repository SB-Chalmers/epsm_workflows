# SPDX-License-Identifier: MIT
"""Command line for authoring packages locally and running them on Vera."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..records import json_value
from .study import StudySpec, load_study


def _study(root):
    return StudySpec.from_dict(json.loads((Path(root) / "study.json").read_text()))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="epsm-campaign", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    p = commands.add_parser("pack", help="freeze a study into a Vera package and zip it")
    p.add_argument("study")
    p.add_argument("destination")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--release-tag", default="v0.5.1")
    p.add_argument("--no-archive", action="store_true")

    p = commands.add_parser("footprints", help="freeze real footprints from portable EPSM records")
    p.add_argument("records", nargs="+", help="epsm-record-1 JSON files, each one record or a list")
    p.add_argument("--output", required=True)

    p = commands.add_parser("validate", help="check a study file and print its identity")
    p.add_argument("study")

    p = commands.add_parser("preflight", help="compile every planned case (Vera)")
    p.add_argument("root")
    p.add_argument("--energyplus", required=True)
    p.add_argument("--workers", type=int, default=1)

    p = commands.add_parser("submit", help="submit unfinished tasks of a phase (Vera)")
    p.add_argument("root")
    p.add_argument("--phase", choices=("pilot", "production"), required=True)
    p.add_argument("--energyplus", required=True)
    p.add_argument("--account", required=True)
    p.add_argument("--concurrency", type=int)
    p.add_argument("--python")

    p = commands.add_parser("worker", help=argparse.SUPPRESS)
    p.add_argument("root")
    p.add_argument("--phase", required=True)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--energyplus", required=True)
    p.add_argument("--no-rc", action="store_true")

    p = commands.add_parser("status")
    p.add_argument("root")
    p = commands.add_parser("pause")
    p.add_argument("root")
    p = commands.add_parser("finalize")
    p.add_argument("root")
    p.add_argument("--destination")

    args = parser.parse_args(argv)
    if args.command == "pack":
        from .pack import pack

        result = pack(args.study, args.destination, workers=args.workers, release_tag=args.release_tag, archive=not args.no_archive)
    elif args.command == "footprints":
        from .geometry import freeze_footprints

        records = []
        for path in args.records:
            data = json.loads(Path(path).read_text())
            records.extend(data if isinstance(data, list) else [data])
        result = freeze_footprints(records, args.output)
    elif args.command == "validate":
        result = load_study(args.study).summary()
    elif args.command == "preflight":
        from .preflight import preflight

        result = preflight(args.root, args.energyplus, workers=args.workers)
    elif args.command == "submit":
        from .preflight import require_preflight
        from .slurm import submit

        require_preflight(args.root, args.energyplus)
        result = submit(
            args.root, _study(args.root), args.phase, args.energyplus,
            account=args.account, concurrency=args.concurrency, python=args.python,
        )
    elif args.command == "worker":
        from .worker import run_task

        study = _study(args.root)
        width = 4 if args.phase == "pilot" else 5
        result = run_task(
            args.root, f"{args.phase}-{args.index:0{width}d}", args.energyplus,
            timeout=study.execution.timeout_seconds, checkpoint_runs=study.execution.checkpoint_runs,
            checkpoint_seconds=study.execution.checkpoint_seconds, rc=False if args.no_rc else None,
        )
    elif args.command == "status":
        from .slurm import plan_summary

        result = plan_summary(args.root)
    elif args.command == "pause":
        from .slurm import pause

        result = pause(args.root)
    else:
        from .store import finalize

        study = _study(args.root)
        destination = args.destination or str(Path(args.root) / "handoff" / f"{study.name}.duckdb")
        result = finalize(args.root, destination, study_name=study.name)
    json.dump(json_value(result), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
