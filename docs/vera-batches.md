# Bounded execution

Prepared plans keep their existing schema, case identities and attempt records.
Use the same plan for local and remote execution. Keep installed package code and
engine files fixed while any batch is running.

## Run part of a prepared plan

The public executor accepts an optional selection of zero-based case indices:

```python
from epsm_workflows import run_local

outcome = run_local("outputs/study", indices=range(0, 32))
```

This example needs an already prepared plan with at least 32 cases, its captured
worker environment and any engine prerequisites. It executes the selected cases;
it does not change the scientific specification. Omitting `indices` resumes all
pending cases as before. Lists and generators are also accepted. Indices must be
unique integers inside the plan. An empty selection runs no cases.

The complete plan, assets and runtime are verified once per invocation. A change
to an unselected input still blocks dispatch. Each selected case and its assets
are checked again immediately before execution. Completed successful cases are
reused. Failed cases are retained unless `retry_failed=True` is explicitly set.
Case locks also protect overlapping batches from duplicate execution.

The return value retains the existing status format and describes the **whole
plan**, including cases outside the selected batch. A single worker dispatches
selected cases in the supplied order; multiple workers may finish in a different
order. Graceful cancellation stops new case attempts and preserves completed
outcomes. Call the existing `resume` operation before dispatching again.

The command-line equivalent uses an inclusive start and exclusive stop:

```sh
epsm-evaluations run outputs/study --start 0 --stop 32
```

Supply both boundaries, inside the plan. They cannot be combined with `--index`.
The command prints whole-plan status using the existing exit-code rules, including
failure counts from cases outside the selected batch. No new archive or prepared
format is introduced.

## Submit bounded Vera tasks

Use an already staged plan and the same pinned remote Python environment:

```python
from epsm_workflows import VeraExecutor

vera = VeraExecutor("vera1.c3se.chalmers.se", "/absolute/scratch/study")
receipt = vera.submit(
    "outputs/study", account="your-project", cases_per_task=64,
    max_concurrent=20, array_size_limit=1000,
)
```

The resource account and array limit above are examples; supply the values for
your project and cluster. This call submits a real job. It still uses one Slurm
array and does not create a separate scheduler. An array task runs its contiguous
range sequentially using the bounded local executor. Only task groups containing
pending cases are submitted. Existing successes and retained failures inside a
group are skipped using the ordinary retry rules. The final group stops at the
last prepared case. `max_concurrent` limits active **tasks**, not individual case
indices. Time and memory requests apply to an entire task, so choose enough time
for its sequential cases. Increasing `cpus` does not turn the batch into parallel
case execution.

For 60,000 prepared cases, batches of 64 require 938 task indices. Each batch
still verifies the complete plan at startup; it then checks individual cases and
assets before their execution. This reduces repeated verification rather than
weakening integrity checks. It does not establish 60,000-case engine performance.

`array_size_limit` means the largest permitted task index plus one. For a limit
of 1000, indices must be in `0..999`, including when only a few high indices are
pending. An oversized request fails before `sbatch`; increase `cases_per_task`
or supply the correct limit. The library does not discover cluster policy or
automatically submit multiple jobs. Existing array and job quotas still apply.

The default `cases_per_task=1` retains the existing command and receipt fields.
Opting into larger batches adds `cases_per_task`, `task_count` and `task_indices`
to the receipt. `submitted` and `indices` continue to describe pending **cases**,
while `job_id` continues to identify one Slurm job. Receipts are retained under
`submissions/` as before. Scientific inputs, plan identities and attempt formats
do not change.

## Gracefully pause remote dispatch

```python
outcome = vera.cancel("outputs/study")
# Active evaluations finish; no new case attempts start.
outcome = vera.resume("outputs/study")
```

These operations check the remote plan identity before calling the same
`cancel`/`resume` controls used locally. They do not kill Slurm jobs, remove
attempts or submit new work. Already exited workers need a subsequent explicit
`submit`. Resume only after any earlier submission has settled when you intend
to keep dispatch paused until a new submission. Repeated or overlapping batches
remain protected by case locks and completed-result reuse.

Offline tests execute generated worker commands against tiny prepared plans and
exercise quoting, range boundaries, retry, cancellation and retained identities.
They do not connect to Vera or establish live Slurm behavior.

## Measure local dispatch overhead

The repository includes a reusable offline benchmark harness:

```sh
PYTHONPATH=src python tests/test_execution_benchmark.py \
  --output /tmp/epsm-dispatch-check --sizes 128 512 --batch-size 32
```

Use a new output directory and a Python environment with the Workflows
dependencies. Keep installed source files fixed until it finishes. The harness
prepares unique tiny payloads with the built-in Python `dict` handler, copies
that immutable plan, and runs the same case identities by both dispatch routes.
It retains complete results, attempts, timings, runtime fingerprints and
`benchmark.json`. It uses real filesystem reads and real package/runtime checks;
observational wrappers count full-plan verifications and case-file reads without
replacing those checks. The test suite also runs an eight-case version to check
the harness and retained outcomes. It sets no machine-dependent timing threshold.

This measures local preparation and dispatch overhead. It does not measure
building preparation, numerical engines, weather, remote filesystems or Slurm
scheduling, and must not be presented as a 60,000-simulation performance test.
