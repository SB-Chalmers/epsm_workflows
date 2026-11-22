# SPDX-License-Identifier: MIT
"""Thin JSON command adapter to the public workflow authoring lifecycle."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .workflow import WorkflowSpec


COMMANDS = {"template", "schema", "capabilities", "preview", "describe", "prepare", "resolve"}


def main(argv=None):
    """Print schema/template/preview or explicitly resolve/prepare a saved draft."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("specification", nargs="?")
    parser.add_argument("--method", choices=("lhs", "monte_carlo", "comparative"), default="lhs")
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--structural", action="store_true", help="Preview membership without engine preflight")
    args = parser.parse_args(argv)
    if args.command == "template":
        result = WorkflowSpec.template(args.method, samples=args.samples, seed=args.seed).to_dict()
    elif args.command == "schema":
        result = WorkflowSpec.schema()
    elif args.command == "capabilities":
        result = WorkflowSpec.capabilities()
    else:
        if not args.specification:
            parser.error("This command needs a workflow JSON file")
        raw = Path(args.specification).read_bytes()
        spec = WorkflowSpec.from_dict(json.loads(raw.decode("utf-8")))
        if args.command == "preview":
            result = spec.preview(check_ready=not args.structural)
        elif args.command == "describe":
            result = spec.describe_design(check_ready=not args.structural)
            result["source_sha256"] = hashlib.sha256(raw).hexdigest()
        elif args.command == "prepare":
            if args.output is None:
                parser.error("prepare needs an explicit --output directory")
            result = spec.prepare(args.output)
        else:
            if args.output is None:
                parser.error("resolve needs an explicit --output artifact directory")
            result = spec.resolve_inputs(directory=args.output).to_dict()
    print(json.dumps(result, indent=2, allow_nan=False))
    return 1 if isinstance(result, dict) and result.get("ready") is False else 0
