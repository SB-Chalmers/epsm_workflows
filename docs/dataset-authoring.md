# Author varied building datasets

Use ordinary study designs for varied datasets. Several starting Buildings,
shared/per-building/per-group parameters and weather assignments use the same
WorkflowSpec as other studies. Training, splitting and statistical interpretation
remain outside Workflows and Studio.

## Physical operations

`geometry.footprint_area_m2` scales one explicit building part using Core's
`Footprint.scaled_to_area`. It preserves footprint shape, including holes;
multipart shortcuts are rejected. Storeys, height and orientation retain their
existing owner meanings. Orientation rotates the complete scene. Footprint area
is distinct from the reference area used for loads.

```python
from epsm_workflows import WorkflowSpec
from epsm_workflows.distributions import log_uniform

workflow = WorkflowSpec.template(samples=20, seed=42)
workflow.vary("geometry.footprint_area_m2", min=250, max=2500,
              distribution=log_uniform())
workflow.vary("geometry.storeys", choices=[2, 4, 6], kind="integer")
workflow.set("context.synthetic", {"preset": "rural", "seed": 7, "radius_m": 60})
```

Add complete subjects and weather using the existing public operations before
previewing. Synthetic context runs after the final physical geometry, including
sampled changes and reference overrides. Its explicit request accepts Core's
generator arguments. The default replaces current context; select
`retain_existing=True` to pass existing neighbours to Core. Generator limits and
overlap errors remain Core diagnostics. No context is generated while editing.

`envelope.element_u_value` changes an explicit physical insulation layer through
Core's public `Building.assign_element_u_value`. For example, after selecting a
physical wall from an authored Building:

```python
workflow.vary("envelope.element_u_value", min=0.15, max=0.3, selector={
    "elements": [wall.identifier],
    "component": wall.construction.components[0].name,
    "layer": 0,
})
```

The selector can include positive `min_thickness_m` / `max_thickness_m` bounds.
The target is nominal opaque U-value in W/(m2 K), using Core's 0.17 m2 K/W films;
it excludes thermal bridges and ground transfer. Core preserves materials,
fractions and quantities. This is assumption authoring, not a renovation event.
Windows, unknown elements/layers and infeasible targets fail explicitly. A
portfolio must select actual physical element IDs for the intended subjects.
Capability discovery marks this binding unavailable with older Core versions.

## Sampling and review

`log_uniform()` is optional and requires positive increasing bounds. Equal
multiplicative intervals have equal probability; bounds 100 to 10,000 have
median 1,000. It reuses existing probability streams. Existing distributions,
seeds, row order, specification serialization and identities are unchanged.

`workflow.describe_design()` expands through the same `preview()` implementation
and returns a compact `epsm-design-preview/1` report: case/design counts,
building/group/weather/engine counts, realized parameter ranges or choice counts,
missing/inapplicable values, fixed binding names, bounded examples and diagnostics.
It does not run engines. `check_ready=False` skips engine preflight and is
explicitly labelled `readiness_checked=False`; membership does not prove readiness.
If diagnostics prevent expansion, the returned count is not a complete ready plan.

```sh
epsm-evaluations describe workflow.json
epsm-evaluations describe workflow.json --structural
```

The CLI's compact report includes `source_sha256`, the digest of the exact
workflow file bytes it read. An application can match an imported report to its
exported draft without reproducing scientific identities or validation rules.
The report does not authorize execution or attach results to an edited draft.
This transport field leaves the existing full `preview` response unchanged.

These additions introduce no serialized defaults or new workflow format. Existing
saved definitions and prepared plans keep their identities. Newly authored
bindings/distribution kinds require a reader with these capabilities; older
readers reject them rather than guessing. Keep the captured runtime to resume a
prepared plan, or prepare a new plan after changing package code.
