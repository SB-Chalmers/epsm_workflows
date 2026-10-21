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

Printing a tree or node shows bounded identities/counts, without expanding hourly
results or building inputs. Use `summary()` for loaded metadata and objectives;
`to_dict()` explicitly exports the full record. Prepared cases, sensitivity designs
and Vera executors provide the same bounded inspection without reading files or
contacting services.

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
- Built-in evaluation pins weather contents/calendar, EPSM implementation and
  dependency identities, assessment implementation, and EnergyPlus executable/schema
  when selected. It uses the public `epsm.evaluation_identity` inspection. Changed
  inputs or runtime reject resume even when completed nodes would bypass simulation.
- A node identity includes its parent and measure bundle. Equal final physical
  states reached through different interventions remain separate nodes.
- The evaluator supplies independent energy, LCA and evaluation identities.
- Equal thermal states may reuse energy while needing different LCA calculations.

`directory` atomically checkpoints every evaluated or rejected candidate. A lock
prevents concurrent tree writers. Configuration changes reject resume. Persistent
custom callables require `evaluator_id` and `constraint_id`; change these when their
semantics change. Failed evaluations remain evidence rather than silently retrying.
The current tree configuration uses schema 2; earlier experimental checkpoints must
be kept separately and cannot be resumed as a current calculation. Inspecting the
built-in EnergyPlus identity requires its executable locally. For remote-only engine
environments, use an explicitly identified custom evaluator.

## Parametric designs and sensitivity

`grid` forms finite Cartesian products. `latin_hypercube` stratifies independent
uniform parameters with an explicit seed. No paper-specific ranges are supplied.

The versioned `WorkflowSpec` adapter delegates finite designs to `grid` and LHS
unit-probability designs to `latin_hypercube`, retaining its hash-derived marginal
and scope seeds before quantile conversion. Its reuse regression checks actual
rows and calls to these public functions. Neither design algorithm is duplicated
inside study authoring.

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
`prepare` freezes them with an importable `module:function` handler, a supplied
environment manifest and an observed worker runtime identity. The identity records
Python, installed EPSM package versions and source hashes, selected dependency
versions and handler source. For the built-in EPSM handler it also records the
selected EnergyPlus executable/schema or RC implementation. Prepare on a host
with the selected engines available. Manifest schema 3 stores a content-keyed
engine inventory and binds every case ID to its selected engine fingerprint.
Different executables remain distinct; equal contents share a fingerprint.
Swapping executables between cases changes those bindings even if the inventory
still contains the same fingerprints. Runtime records contain no executable paths.
An explicit executable path remains a constraint in its case payload; omit it and
configure `ENERGYPLUS_EXE` on each host when installation prefixes differ.

Workers compare their actual runtime before execution or resume, and each attempt
retains its runtime hash. A local batch verifies the complete plan and runtime once,
then uses a private, deeply immutable captured manifest. Each selected case, its
assets and any retained result are checked separately. A file-metadata change
detector rejects manifest changes during the batch; it is not a cryptographic
filesystem snapshot. Standalone workers and every new batch verify afresh. There
is no global fingerprint cache. Keep installed code fixed while a batch runs.
Name repeated design rows so their identities remain distinct. An existing
prepared plan cannot be changed.

Declared assets are copied and verified by digest. Payloads use
`{"$asset": "name"}`; workers resolve references at the local or remote root.
The EPSM adapter does this automatically for weather. Other prerequisites, such as
the engine executable, belong to the configured remote environment.

A handler receives one payload plus an injected `_execution` record containing root,
case ID and attempt ID. It returns a JSON-compatible result or object with `to_dict()`.
The built-in handler reconstructs Building, Weather, AssessmentSettings and public
EnergyPlus CompilerSettings, uses a shared energy/LCA cache and writes each
attempt's artifacts separately. RC accepts the explicitly serialized reduction
settings through its public API. EnergyPlus settings validation also happens during preparation.

`run_local` skips successful and previously failed cases. `retry_failed=True` retries
failures explicitly. Interrupted attempts remain recorded; unfinished cases resume.
Workers lock individual cases. Corrupted or missing frozen assets are plan
integrity failures: preflight raises before creating any attempts. Ordinary
evaluator failures retain their error and traceback as retryable attempts.

### Upgrading prepared studies to 0.3.0

Prepared schema 2 is rejected with a request to prepare a new plan. Keep old
artifacts separately; this release does not migrate, rewrite or resume them.
Prepare from reviewed physical inputs and the new installed package combination.
Changed runtime identities invalidate reuse even for previously completed cases.
Renovation trees retain their existing schema 2 and public APIs are unchanged.

`VeraExecutor.stage` transfers immutable inputs/assets and rejects an incompatible
remote plan. `submit` reads remote completion and submits unfinished Slurm indices.
`retrieve` downloads into a temporary directory and verifies the remote plan and
attempt/result checksums before merging. A local success stays selected even when
the remote attempt failed. A remote success can replace a local failure; all attempt
records are retained. Conflicting artifact files are rejected instead of overwritten.
Paths saved inside remote results continue to identify their original remote location.
The remote Python environment must match the prepared packages and engine. Offline
tests verify commands, quoting, input transport and resume; they are not live Vera
or Slurm execution evidence.

## Execution events

Search, preparation, case execution and Vera operations emit ordinary Python log
records under `epsm_workflows`. Records carry `epsm_event`, `epsm_context` and
`epsm_details`. Context includes study/case/attempt identifiers and remote worker or
Slurm identifiers when available. A handled case failure emits one traceback at the
executor and remains in its attempt record. Events contain counts and safe metadata;
they do not include case payloads or hourly arrays. The application owns handlers,
levels, formatting and log retention; importing Workflows configures none of these.
