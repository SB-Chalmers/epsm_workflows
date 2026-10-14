"""Run the same diagnostic CLI in processes to avoid SQL-reading thread contention.

Requires cloudpickle. Physics, strict validation and receipt identities are unchanged.
Usage: python examples/epc_validation/run_diagnostics_processes.py simulate [options]
"""

import json
import sys
from concurrent.futures import ProcessPoolExecutor
from itertools import repeat
from pathlib import Path

import cloudpickle

from epsm_workflows import archetype_diagnostics as diagnostics
from epsm_workflows.campaign import file_sha256


def execute_serialized(payload, job):
    return cloudpickle.loads(payload)(job)


class DiagnosticProcesses:
    def __init__(self, max_workers):
        self.pool = ProcessPoolExecutor(max_workers=max_workers)

    def __enter__(self):
        return self

    def map(self, function, jobs):
        return self.pool.map(
            execute_serialized, repeat(cloudpickle.dumps(function)), jobs
        )

    def __exit__(self, exc_type, exc, traceback):
        self.pool.shutdown(wait=True, cancel_futures=exc is not None)


if __name__ == "__main__":
    diagnostics.ThreadPoolExecutor = DiagnosticProcesses
    if "--output" in sys.argv:
        root = Path(sys.argv[sys.argv.index("--output") + 1])
        root.mkdir(parents=True, exist_ok=True)
        stage = (
            sys.argv[sys.argv.index("--stage") + 1]
            if "--stage" in sys.argv
            else "other"
        )
        (root / f"process-execution-{stage}.json").write_text(
            json.dumps(
                {
                    "adapter_sha256": file_sha256(Path(__file__)),
                    "python": sys.version,
                    "cloudpickle_version": cloudpickle.__version__,
                    "strategy": "processes; identical diagnostic functions and validation",
                    "arguments": sys.argv[1:],
                },
                indent=2,
            )
        )
    diagnostics.main()
