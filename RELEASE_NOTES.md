# Release 0.6.2 — operator case exclusions and registry footprints (3 November 2026)

A strict EnergyPlus failure that cannot be fixed by re-running can now be left out
of a campaign on record instead of holding it forever. `epsm-campaign exclude ROOT
--ordinal N --reason "..."` records the case in `exclusions.json`; it requires the
diagnostic bundle the worker saved for that failure, and stores the case id, group,
failure code, error and bundle path with the reason. Workers skip excluded cases and
count them in their receipts, `status` reports `excluded`, `finalize` moves the plan
rows to a `runs_excluded` table (inputs plus reason, failure code and diagnostic),
lists them in the handoff `manifest` (`exclusions`) and README, checks group sizes
against the plan minus the exclusions, and refuses an excluded case that has a
result. `epsm-campaign resume ROOT` clears `PAUSE` and cancels the held arrays so
the `production` stage of `vera.sh` resubmits only the unfinished tasks. First
used on the 60k stock study, where every case of one 64-vertex registry footprint
exceeded EnergyPlus's polygon-clipping capacity (1,000,000 overlap figures) in all
three synthetic contexts, and every case of a 15-vertex footprint with a vertex 4 µm
off its neighbours' chord lost that vertex to EnergyPlus's collinearity cleanup and
failed as an unenclosed zone; raising the shadow cap only made EnergyPlus exhaust 4 GB.
`vera.sh` schedules the `finalize` stage with a 12-hour walltime (the submit-only
stages keep 2 hours): merging the 60k study's 886 shards into the handoff on Cephyr
ran past two hours and was cut off.

`freeze_footprints` accepts the MultiPolygon geometry the EPSM stock lake exports
(largest part kept, `source_parts` recorded) instead of rejecting every registry
record as "not a single polygon". The case test no longer requires Core's RC
readiness verdict, whose capability list lags epsm_rc 0.11; the worker gates on
`epsm_rc.preflight`. `pack` uses only the real footprints whose area lies within
the study's sampled `footprint_area_m2` bounds and records `footprints_used` and
`footprints_excluded_by_area` in the manifest, instead of failing the bounds
check on the first oversized block. In a stock study each city now draws its
real footprints from its own interleaved share of the pool, so a national pool
yields distinct real buildings per city (the first 60k pack reused 900 of 4,813).
`freeze_footprints` rejects a footprint whose vertices lie closer than 0.05 m
(reason `vertices N mm apart`): Core tessellates upper floors between footprint
vertices and refuses edges under 0.010001 m, so a digitised pinch failed every
case of that footprint at Vera preflight (8 of 4,870 registry footprints).
Scene normalization now adopts Core's prepared (1 cm-simplified) part outline as
the authored footprint and records the parts it changed in the transform
metadata (`footprint_cleanup_changed`); a rotated registry outline could cross
that tolerance where the frozen one did not, and Core then refused the Building
("requires reviewed footprint cleanup"). Every placed case is also checked with
`readiness(engine="energyplus")` at pack time, the first step of Vera preflight.

# Release 0.6.1 — campaign fixes from the first Vera smoke run (2 November 2026)

Packing a `stock_diversity` study with more than one context no longer fails the
LHS strata check (each design was counted once per context). The campaign worker
acknowledges the approximations `epsm_rc` 0.11 requires (`airflow_policy`,
`ground_policy="conductance_multiplier"`, `self_shadowing_policy="ignore"`), runs
`epsm_rc.preflight` first and records the settings or the preflight diagnostics
in `rc_diagnostics`; previously every RC column stayed NULL. `epsm-campaign pack`
defaults to release tag `v0.6.0`. The 0.6.0 notes called the stock sampler
row-for-row reproducible; that holds per platform and SciPy minor version only
(SciPy's `random-cd` LHS differs between macOS/arm64, Linux/x86_64 and SciPy
1.17/1.18), so the design-digest test is pinned per platform and the docs say to
pack once and ship the frozen plan.

# Release 0.6.0 — dataset authoring, bounded Vera execution and training-data campaigns (31 October 2026)

Requires Core 0.10.0 (`epsm_core>=0.10.0`), which ships `Building.assign_element_u_value`,
and Data 0.7.0 (`epsm_data>=0.7.0`). Lower bounds only, as across the EPSM siblings; the
EPSM supported manifest records the tested combination.

Authoring gains `log_uniform()` marginals, the owner-backed physical bindings
`geometry.footprint_area_m2` (Core `Footprint.scaled_to_area`),
`envelope.element_u_value` (Core `Building.assign_element_u_value`; requires a
Core with that operation, otherwise the capability is reported unsupported) and
`context.synthetic` (Core `synthetic_context`, applied after final geometry;
a supplied complete context needs `retain_existing=True`), plus
`describe_design()` and the `epsm-evaluations describe` report. Applicability
conditions on generated context are rejected with a diagnostic.

Prepared execution accepts `run_local(indices=...)` and `--start/--stop`, and
`VeraExecutor.submit(cases_per_task=..., array_size_limit=...)` runs contiguous
case ranges per Slurm task; `cancel`/`resume` reach a staged remote plan. Plan
identities, attempt records and existing defaults are unchanged. See
docs/dataset-authoring.md and docs/vera-batches.md.

`epsm_workflows.campaign` (extra `campaign`, CLI `epsm-campaign`) packs one study
file into a self-contained Vera package: the 2026-09 stock sampler (row-for-row
identical for its procedural families) plus real footprints frozen from EPSM
records, every case stored once per scene as a portable Core `Building`,
frozen weather, `vera.sh` and a README of the exact cluster commands. Four study
kinds share one handoff schema: `stock_diversity`, `weather_sweep`,
`before_after`, `ventilation_sweep`. On Vera a task runs a contiguous case range,
runs EnergyPlus and, when `epsm_rc` is installed, RC on the same Building, and
publishes one DuckDB shard; `finalize` merges, checks and cleans up. Vera needs
only `epsm_core`, `epsm_data`, `duckdb`, `numpy` (and optionally `epsm_rc`).
Solving envelope U-values requires a Core with `Building.assign_element_u_value`.
Holed (courtyard) floors use prescribed ground temperatures; `input_ground_method`
records it. See docs/campaigns.md.

# Release 0.5.0 — the Core 0.9 / Data 0.7 cohort (29 October 2026)

Compatibility release. Workflows now requires Core 0.9.0 (`epsm_core>=0.9.0,<0.10`)
and Data 0.7.0 (`epsm_data>=0.7.0,<0.8`); the `epsm` extra requires EPSM 0.5.0.
Authoring, prepared execution (schema 3) and research callbacks are unchanged.

# Release 0.4.0 — reusable study authoring

Add versioned WorkflowSpec templates, generated schema and owner field discovery, independent distributions, registered physical bindings, portfolio/baseline/engine expansion and preview. Existing prepared schema-3 execution and research callbacks remain supported.

New study evaluations retain lightweight references to owner bundles. Public outcome inspection and graceful cancellation preserve attempt history; CLI commands use the same authoring API. Unsupported physical methods remain explicit capabilities rather than substitutions.
