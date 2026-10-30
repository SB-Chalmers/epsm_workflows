# epsm_workflows guide

Run commands from the repository root unless stated otherwise.

Reusable parametric designs, global sensitivity, renovation-tree optimisation and
local/Vera execution for EPSM. Workflows prepares inputs, explores alternatives and
records execution. Scientific energy and LCA calculations remain in their packages.

## Study authoring in 0.4.0

```python
from epsm_workflows import WorkflowSpec
from epsm_workflows.distributions import normal

workflow = WorkflowSpec.template("monte_carlo", samples=20, seed=7)
workflow.vary("facade.wwr", min=0.1, max=0.4,
              distribution=normal(mean=0.25, std=0.05), baseline=0.2)
workflow.validate()  # reports the subjects and weather still required
```

Use `add_subject`, `add_weather`, `set` and `add_scenario` to author complete
experiments. `preview` expands physical states and owner readiness without running
engines; `prepare` freezes the exact cases for the existing local/Vera executor.
The same typed definitions generate template JSON, `WorkflowSpec.schema()` and
input discovery. Studies owns the outer `Study` and permanent results archive.
Existing callback designs below remain supported.

See [study authoring](../docs/study-authoring.md),
[distributions](../docs/distributions.md), [examples](../examples/README.md) and the
[0.4.0 compatibility notes](../RELEASE_NOTES.md).

## Renovation alternatives

```python
from epsm_workflows import optimise_renovations
from epsm_rc import RCSettings

rc_settings = RCSettings.from_policy(
    "epsm_rc_5r1c_v1",
    airflow_policy="sensible_only_unlimited_supply",
    ground_policy="conductance_multiplier",
)

tree = optimise_renovations(
    building, catalogue=renovation_catalogue,
    max_depth=2, max_bundle_size=2, budget=100,
    engine="epsm_rc", weather=weather, assessment=assessment,
    directory="outputs/renovation-search",
    evaluation_options={"settings": rc_settings},
)
for node in tree.pareto:
    print(node.action_ids, node.energy_kwh, node.carbon_kgco2e)
```

Supply an authored EPSM Building, weather, explicit assessment settings and executable Data
renovation choices. Each child applies a bundle to its parent; there are no dates or
stage slots. Intermediate states remain candidates, and dominated prefixes are still
expanded. Incomplete energy/carbon objectives are retained but excluded from selection.

## Designs and local execution

```python
from epsm_workflows import Parameter, latin_hypercube, parametric_cases, prepare, run_local

samples = latin_hypercube([Parameter("window_fraction", .15, .35)], 20, seed=42)
cases = parametric_cases(building, samples, apply_parameters,
                         engine="epsm_rc", weather=weather, assessment=assessment,
                         simulation_options={"settings": rc_settings})
prepare("outputs/study", cases, environment={"revision": "your-reviewed-revision"})
run_local("outputs/study", workers=2)
```

`apply_parameters(copy, parameters)` is a caller-supplied physical transformation.
Use `grid` for finite combinations and `sensitivity_design`/`analyse_sensitivity` for
independent uniform variance-based global sensitivity.

## Comparing with measured EPC heating

`epsm_workflows.validation` puts a model and an EPC on the same terms and scores
catalogue cohorts by epsm_data's [measured-data contract](https://github.com/SB-Chalmers/epsm_data/blob/main/docs/validation.md).

```python
from epsm_workflows.validation import (
    comparison_terms, delivered_heating_estimate, score_cohorts, compare_cohorts,
)

terms = comparison_terms("multi_family", municipality_code="1485",
                         apply=("window_airing", "hot_water_circulation"))
estimate = delivered_heating_estimate(space_heat_kwh, atemp_m2, carrier="heat_pump",
                                      seasonal_cop=2.75, period_days=365,
                                      terms_kwh_m2_year=terms)
```

No term applies unless the study names it: which terms suit which building type is a
study's accepted result. `score_cohorts` takes each building's modelled/measured ratio
per renovation state, or `single` for SE.07. `compare_cohorts` applies the acceptance
rule on one sample and `accepted_on_both` combines two; the sample itself, and whether a
value stays in its sourced range, stay with the study.

## Vera

```python
from epsm_workflows import VeraExecutor

vera = VeraExecutor("vera1.c3se.chalmers.se", "/absolute/scratch/study",
                    python="/absolute/environment/bin/python")
vera.stage("outputs/study")
receipt = vera.submit("outputs/study", account="your-project", max_concurrent=20)
# Later, inspect vera.status(receipt["job_id"]), then retrieve results.
vera.retrieve("outputs/study")
```

These calls perform real SSH transfers and Slurm submission. Both execution modes
consume the same frozen cases and verified weather assets. Successful cases resume;
failed attempts retain diagnostics and retry only when requested. The remote environment
must contain the pinned EPSM packages and engine. Offline tests are not live Vera evidence.

See [the reusable workflow guide](../docs/reusable-workflows.md) for identities, resumption,
assessment interpretation and sensitivity assumptions.

## Release 0.3.1 packaging

Version 0.3.1 includes the [MIT license](../LICENSE) in its wheel and source archive
and declares it in distribution metadata. It retains the existing EPSM
Development contributors notice. The package description, README and repository
link are included in metadata as well. Scientific behaviour and public function
signatures are unchanged; prepared plans continue to use schema 3. See the
[packaging release note](../docs/release-0.3.1.md).

## Prepared study format in 0.3.0

New prepared plans use schema 3, with a content-based engine inventory and a binding
from each case to its selected engine. Multiple EnergyPlus executables are retained
correctly. EnergyPlus settings survive JSON export as the public typed Core settings.
Local batches verify their runtime once; standalone workers and later resume calls
always verify afresh. Individual case inputs, assets and result records remain checked.

Prepare a new plan when upgrading from 0.2.0. Old schema-2 prepared artifacts are
rejected without being rewritten. Corrupted or missing frozen assets now reject
preflight before any attempts; ordinary evaluator failures remain recorded and
retryable. Renovation-tree schema 2 and the public function signatures are unchanged.

## Archived studies

Existing named studies, fixed campaigns and legacy compatibility imports were
archived by maintainer decision on 14 October 2026. Their source, scripts,
configurations, tests, assumptions and documentation are preserved in
[archive/2026-10-14](../archive/2026-10-14), with original paths and a checksum
manifest described in the [archive register](../archive/README.md). Old study imports
and command-line entry points are no longer installed. Reproduction of those
studies is not an active requirement.

The reusable APIs above remain active for future studies. EPC ingestion/comparison
and measurement-year weather helpers also remain available. Install `.[epsm]` for
the facade, `.[epc]` for publication ingestion and `.[weather]` for weather
compilation. The `epsm-evaluations` command provides prepared execution. The
[national database starter](../database/national_stock/README.md) is a separate design
proposal, not a deployed study workflow.
