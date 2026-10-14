# epsm_workflows

Reusable parametric designs, global sensitivity, renovation-tree optimisation and
local/Vera execution for EPSM. Workflows prepares inputs, explores alternatives and
records execution. Scientific energy and LCA calculations remain in their packages.

## Renovation alternatives

```python
from epsm_workflows import optimise_renovations

tree = optimise_renovations(
    building, catalogue=renovation_catalogue,
    max_depth=2, max_bundle_size=2, budget=100,
    engine="epsm_rc", weather=weather, assessment=assessment,
    directory="outputs/renovation-search",
)
for node in tree.pareto:
    print(node.action_ids, node.energy_kwh, node.carbon_kgco2e)
```

Supply an EPSM Building, weather, explicit assessment settings and executable Data
renovation choices. Each child applies a bundle to its parent; there are no dates or
stage slots. Intermediate states remain candidates, and dominated prefixes are still
expanded. Incomplete energy/carbon objectives are retained but excluded from selection.

## Designs and local execution

```python
from epsm_workflows import Parameter, latin_hypercube, parametric_cases, prepare, run_local

samples = latin_hypercube([Parameter("window_fraction", .15, .35)], 20, seed=42)
cases = parametric_cases(building, samples, apply_parameters,
                         engine="epsm_rc", weather=weather, assessment=assessment)
prepare("outputs/study", cases, environment={"revision": "your-reviewed-revision"})
run_local("outputs/study", workers=2)
```

`apply_parameters(copy, parameters)` is a caller-supplied physical transformation.
Use `grid` for finite combinations and `sensitivity_design`/`analyse_sensitivity` for
independent uniform variance-based global sensitivity.

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

See [the reusable workflow guide](docs/reusable-workflows.md) for identities, resumption,
assessment interpretation and sensitivity assumptions.

## Historical studies

Paper-specific implementations now live in `epsm_studies.legacy_workflows`. Their
pre-harmonisation code, tests, examples, configurations and documentation are preserved
with checksums in Studies. Thin compatibility imports retain old Workflows module names
when Studies is installed. New studies use the reusable interfaces above.

Install `.[epsm]` for the facade, `.[studies]` for historical imports, and the existing
campaign/stock/epc/weather extras for historical dependencies. Transferring old code does
not revalidate its scientific conclusions against new package versions.
