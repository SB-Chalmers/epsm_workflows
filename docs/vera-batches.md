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
