"""CLI for fixed-data physical cohort calibration and transfer evaluation."""

import argparse
import json

from threadpoolctl import threadpool_limits


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="stage", required=True)
    for stage in ("prepare", "weather"):
        p = sub.add_parser(stage)
        p.add_argument("--config", required=True)
        p.add_argument("--output", required=True)
    for stage in ("run", "evaluate", "export"):
        p = sub.add_parser(stage)
        p.add_argument("--campaign", required=True)
    p = sub.add_parser("import-cache")
    p.add_argument("--campaign", required=True)
    p.add_argument("--previous", action="append", required=True)
    p = sub.add_parser("inventory")
    p.add_argument("--study", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--campaign", action="append", default=[])
    args = parser.parse_args(argv)
    with threadpool_limits(limits=2):
        if args.stage == "prepare":
            from .preparation import prepare

            result = str(prepare(args.config, args.output))
        elif args.stage == "weather":
            from .weather import prepare_weather

            result = prepare_weather(args.config, args.output)
        elif args.stage == "import-cache":
            from .cache import import_native_receipts

            result = import_native_receipts(args.campaign, args.previous)
        elif args.stage == "inventory":
            from .inventory import inventory

            result = inventory(args.study, args.output, args.campaign)
        else:
            from .calibration import evaluate, export, run

            result = {"run": run, "evaluate": evaluate, "export": export}[args.stage](
                args.campaign
            )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
