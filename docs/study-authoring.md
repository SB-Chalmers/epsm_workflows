# Author a reusable study

`WorkflowSpec` is a versioned, JSON-round-trippable draft. Its typed definitions
generate templates and JSON Schema; `capabilities()` composes Core's public type
schemas and reports planned methods explicitly. Studies embeds this workflow in
its outer study document and owns permanent result archives.

```python
from epsm_workflows import WorkflowSpec
from epsm_workflows.distributions import normal

workflow = WorkflowSpec.template("monte_carlo", samples=20, seed=7)
workflow.vary("facade.wwr", min=0.1, max=0.4,
              distribution=normal(mean=0.25, std=0.05), baseline=0.2)
saved = workflow.to_dict()
assert WorkflowSpec.from_dict(saved).id == workflow.id
```

Supply explicitly authored EPSM Buildings with `add_subject(building, id=...)`
and inspected Core weather with `add_weather(weather, id=...)`. Use `set` for
fixed physical inputs, `vary` for registered marginal domains and `add_scenario`
for independent material-backed alternatives made by `Building.renovate`.
Changing WWR or a thermal property is physical sensitivity authoring; it does not
establish an installed renovation event or its embodied impact.

`lhs` templates default to independent uniform marginals. Monte Carlo requires
explicit distributions. Comparative templates enable a baseline and start with
finite-grid choices; change `sampling.method` to `lhs` or `monte_carlo` when
appropriate. Categories use finite choices and optional probabilities; they have
no numerical distance. Integer domains use discrete integer choices. No parameter
is silently rounded, clipped or resampled. Correlated designs are not supported.

Scope is `shared`, `per_building`, or `per_group`. Per-group sampling requires
group identities. Separate seed streams make subject order independent. Baseline
values either inherit each Building's inputs or explicitly override them. Output
cases retain building/scenario/weather/engine axes and baseline/engine pair IDs.
Fixed study inputs apply to baseline, sampled cases and named alternatives.
Named alternatives retain their independent material histories. A comparative
study requires an explicit candidate design or at least one named alternative.

`validate()` checks authoring requirements without I/O. `resolve_inputs()` is an
explicit acquisition/assembly step for declared sources. It uses public EPSM,
Core and weather-provider operations, never serialized credentials or callbacks.
Weather batch/morphing requires the version and extras exposing those public
methods; supplied EPWs remain usable independently. A hypothetical building can
use real weather. TMY generation and behavioral window-opening remain separate
unavailable methods, not substitutions for supplied EPW or scheduled airflow.
Source requests select `synthetic`, `drawn` or portable `record` assembly; weather
requests select supplied `epw`/`tmy` or `amy_batch`. Generated schema composes
Core footprint and installed weather-provider location/morphing definitions.
Provider availability is explicit when optional weather dependencies are absent.
Source calendar years remain distinct from future climate periods. An authored
`sampling.correlation` request is retained and explicitly blocks preparation.

`preview()` resolves candidate physical changes and uses EPSM's public preflight;
it does not run engines. `preview(check_ready=False)` inspects membership only and
does not establish readiness. `prepare(directory)` requires a ready preview and
freezes schema-3 cases through the existing executor. All defined physical values,
weather bytes, settings, requested outputs and pairing metadata are retained.
Changing a draft never rewrites a prepared plan.

Configure both engines with `set("engines", ["epsm_rc", "energyplus"])` and set
their explicit settings with `set("engine_settings", {...})`. Supported common
energy coverage is heating and fans; optional whole-building outputs must not be
inferred. Each `outputs` descriptor declares name, resolution, level, required
status and optionally unit. Required defaults are hourly heating and annual
delivered energy. Add `lca` with assessment resolution for required impacts.
Energy acceptance, output coverage and LCA completeness remain separate.

The built-in evaluator retains portable evaluation bundles for this new harness;
older `PreparedEvaluation` handlers retain their existing inline results. Public
`inspect_plan`, `read_results(require_success=False)` and `read_attempts` verify
inputs and evidence for Studies collection without the original worker runtime.
Existing `validate_plan` still returns the verified manifest.

The existing `epsm-evaluations` CLI accepts `template`, `schema`, `capabilities`,
`preview`, `resolve` and `prepare`, in addition to `run`, `status` and `pending`.
`cancel` requests graceful cancellation: active cases finish and no new cases
start; `resume` explicitly permits dispatch again. Interrupted attempts remain
recorded. Remote execution continues through public `VeraExecutor` operations;
offline transport checks do not establish a live Slurm deployment.
