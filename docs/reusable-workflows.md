# Reusable workflows

## Physical renovation trees

`optimise_renovations` performs deterministic breadth-first search over finite,
resolved catalogue choices. A bundle is one atomic edge. Descendants inherit the
actual parent physical state. Set `max_bundle_size` to enumerate combinations, or
pass `bundles=[(windows,), (windows, roof), (ftx,), (ftx, wall)]` explicitly.

The baseline consumes one unit of `budget`. Invalid bundles are recorded in
`tree.rejected` without launching simulations. Valid physical states with failed or
incomplete evaluations remain nodes and may have valid descendants. Measures cannot
repeat along a path unless `allow_repeated=True`. Bundle order is retained and Core
validates physical changes.

All intermediate states participate in the feasible Pareto comparison. Dominated
prefixes are expanded because later interventions may improve their outcome. The
bounded enumeration does not claim a global optimum after exhausting its budget;
`tree.exhausted_budget` identifies that condition.

### Assessment interpretation

Each reached state is assessed under the configured period and boundary. Embodied
impacts use cumulative installation/removal events. Operating energy belongs to the
reached state. Workflows invents no stage dates or operating durations for ancestors.
Other timing conventions belong to an explicit assessment configuration/evaluator.

Annual delivered energy (kWh) and life-cycle GWP (kgCO2e) are minimised separately.
Only complete, finite objectives enter `tree.pareto`. Constraints receive the full
evaluation and return True, False or a failure explanation. Constraints affect
feasibility, not expansion. Thermal demand and delivered carrier energy remain
distinct; the declared energy boundary must have complete coverage.

### Independent identities and resume

- The tree pins baseline inputs, measure definitions, evaluator and constraints.
  Depth and budget may increase when resuming.
- A node identity includes its parent and measure bundle. Equal final physical
  states reached through different interventions remain separate nodes.
- The evaluator supplies independent energy, LCA and evaluation identities.
- Equal thermal states may reuse energy while needing different LCA calculations.

`directory` atomically checkpoints every evaluated or rejected candidate. A lock
prevents concurrent tree writers. Configuration changes reject resume. Persistent
custom callables require `evaluator_id` and `constraint_id`; change these when their
semantics change. Failed evaluations remain evidence rather than silently retrying.

## Parametric designs and sensitivity

`grid` forms finite Cartesian products. `latin_hypercube` stratifies independent
uniform parameters with an explicit seed. No paper-specific ranges are supplied.

`sensitivity_design(parameters, n, seed=...)` makes independent uniform Monte Carlo
matrices A/B and one mixed matrix per parameter: `n*(d+2)` evaluations for d inputs.
`analyse_sensitivity` computes Jansen first/total-order variance indices in that row
order. This is not a Sobol sequence generator. Correlated/categorical inputs require
another explicitly implemented method. Failed rows cannot be dropped, constant
responses have undefined indices, and estimates are not clipped to [0, 1].

`parametric_cases` copies the baseline for each sample. Its callback changes physical
inputs explicitly. Changing a thermal coefficient alone does not establish an
installed material inventory for LCA.

## Prepared evaluations

`PreparedEvaluation(payload, inputs, name, files)` holds portable JSON inputs.
`prepare` freezes them with an importable `module:function` handler and a supplied
environment manifest. Name repeated design rows so their identities remain distinct.
An existing prepared plan cannot be changed.

Declared assets are copied and verified by digest. Payloads use
`{"$asset": "name"}`; workers resolve references at the local or remote root.
The EPSM adapter does this automatically for weather. Other prerequisites, such as
the engine executable, belong to the configured remote environment.

A handler receives one payload plus an injected `_execution` record containing root,
case ID and attempt ID. It returns a JSON-compatible result or object with `to_dict()`.
The built-in handler reconstructs Building, Weather and AssessmentSettings, uses a
shared energy/LCA cache and writes each attempt's artifacts separately.

`run_local` skips successful and previously failed cases. `retry_failed=True` retries
failures explicitly. Interrupted attempts remain recorded; unfinished cases resume.
Workers lock individual cases.

`VeraExecutor.stage` transfers immutable inputs/assets and rejects an incompatible
remote plan. `submit` reads remote completion and submits unfinished Slurm indices.
`retrieve` merges attempts, results and artifacts without deleting local files.
The remote Python environment must contain the pinned packages and engine. Offline
tests verify commands, quoting, input transport and resume; they are not live Vera
or Slurm execution evidence.
