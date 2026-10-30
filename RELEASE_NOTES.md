# Unreleased

`validation` compares models with measured EPC heating for every carrier and scores
catalogue cohorts. `delivered_heating_estimate` converts simulated heat by efficiency
or seasonal COP, with accounting terms scaled to the measured period, and
`comparison_terms` reads their sourced values from Data. `classify_building`,
`score_cohorts`, `summarise_cohorts`, `compare_cohorts` and `accepted_on_both` implement
epsm_data's measured-data contract. Requires the next Data release.

# Release 0.5.0 — the Core 0.9 / Data 0.7 cohort (29 October 2026)

Compatibility release. Workflows now requires Core 0.9.0 (`epsm_core>=0.9.0,<0.10`)
and Data 0.7.0 (`epsm_data>=0.7.0,<0.8`); the `epsm` extra requires EPSM 0.5.0.
Authoring, prepared execution (schema 3) and research callbacks are unchanged.

# Release 0.4.0 — reusable study authoring

Add versioned WorkflowSpec templates, generated schema and owner field discovery, independent distributions, registered physical bindings, portfolio/baseline/engine expansion and preview. Existing prepared schema-3 execution and research callbacks remain supported.

New study evaluations retain lightweight references to owner bundles. Public outcome inspection and graceful cancellation preserve attempt history; CLI commands use the same authoring API. Unsupported physical methods remain explicit capabilities rather than substitutions.
