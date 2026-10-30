# Unreleased — dataset authoring, bounded Vera execution and training-data campaigns

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
