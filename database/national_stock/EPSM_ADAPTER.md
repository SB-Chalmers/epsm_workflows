# Proposed EPSM adapter for the national stock

PostgreSQL/PostGIS holds versioned stock evidence, catalogue releases, model snapshots, scenario definitions and result indexes. An external Python worker assembles and validates `epsm_core.BuildingSpec`, applies renovations and runs EnergyPlus. Executable EPSM code belongs in a pinned worker environment. This document proposes an adapter contract; it does not establish a working importer, deployed service or completed national simulation.

The source layer follows [SOURCE_CONTRACT.md](SOURCE_CONTRACT.md). Importing EPSM Data into queryable database tables must retain the original immutable JSON assets and their checksums. Current Data loaders accept packaged or caller-owned JSON, and the detailed loader explicitly performs no SQL reads: [parametric loader](/Users/ssanjay/GitHub/epsm_data/src/epsm_data/parametric.py:639), [detailed loader](/Users/ssanjay/GitHub/epsm_data/src/epsm_data/detailed.py:888). A database catalogue adapter therefore needs implementation and equivalence validation before replacing these loaders.

## Assembly contract and gates

Proposed entry point: `assemble_building(building_id, snapshot_id, scenario_id=None)`. `snapshot_id` fixes the selected observations, geometry revision, catalogue, completion policy, weather and operation assumptions. Its output is a complete validated `BuildingSpec` plus an assembly receipt, or a retained rejection with unresolved requirements. Core requires geometry, facade, climate, envelope, airflow and use: [BuildingSpec](/Users/ssanjay/GitHub/epsm_core/src/epsm_core/specs/building.py:127).

| Input group | Worker contract |
|---|---|
| Identity and geometry | Building UUID and observation/link references; metric footprint parts and holes; declared CRS and geometry revision; audited cleanup; storey elevations or resolved storeys/heights; outline kind, roof and basement geometry. Property/EPC links establish evidence scope and grouping. |
| Catalogue assignment | Explicit use category, selected construction year, climate zone, ventilation topology and independently sourced component states. Data returns fabric/glazing/infiltration, assignment evidence and requirements; it does not supply a complete building or infer location/HVAC. |
| Area and operation | Scope-compatible Atemp, occupancy/use profile, calendar year, active dates, schedules, ventilation flow basis, recovery and delivery assumptions. Keep measured energy targets outside model-input assembly. |
| Weather and boundaries | EPW URI and SHA-256, calendar year and site metadata; roof/attic exchange, foundation/soil/ground boundary, exposed walls and heating-carrier accounting. |
| Provenance | Each selected or completed field points to source release/asset/row, rule version and measured/inferred/assumed status; preserve independent component evidence and scenario assumptions. |

Set `load_reference_area_m2` to scope-compatible EPC Atemp: loads and area-based airflow use it while the physical footprint, heights and heat-transfer surfaces retain their geometry. Do not rescale a footprint to make its area equal Atemp. Missing Atemp requires a declared, versioned area assumption. The former cohort-transfer composition is part of the [retired study archive](../../archive/README.md); it is no longer an active adapter implementation.

Gate unresolved geometry compatibility, year/use assignment, storeys/heights, roof topology, basement condition, operation calendar, weather checksum/year and carrier accounting before queueing a run. A pinned completion scenario may resolve missing evidence; it must retain its assumptions. Historical study examples included unclear-to-flat roofs, assumed gable pitch/basement depth and normal-use calendars. Those choices were assumptions and their implementations are now [archived](../../archive/README.md). Cold-attic exchange and ground boundaries remain caller requirements: [Data handoff](/Users/ssanjay/GitHub/epsm_data/src/epsm_data/release.py:12). Use the native area/volume-aware airflow handoff; source-reference ACH cannot be transferred to an arbitrary model volume: [resolver contract](/Users/ssanjay/GitHub/epsm_data/src/epsm_data/application.py:27).

## Scenarios create new models

Catalogue `existing`, `standard` and `advanced` are alternative component states, not a sequence of renovation commands. `existing` is an original-era representative assumption, not proof of an unrenovated building. Store scenario definitions separately from observations and baseline snapshots.

For an intervention sequence, store typed measure parameters, effective date/order, affected-element assertions, dataset references and cost/carbon factor references. Core `apply_renovation` returns a new validated building with dated before/after physical hashes and quantity changes; observed year, EPC values and component evidence remain unchanged. Its targets assert the complete affected set rather than selecting a partial subset: [renovation API](/Users/ssanjay/GitHub/epsm_core/src/epsm_core/renovation.py:259), [audit record](/Users/ssanjay/GitHub/epsm_core/src/epsm_core/renovation.py:379). Cost/carbon calculations need separately pinned external factors.

```python
# Proposed flow only; export/store/queue helpers are not implemented APIs.
batch = export_snapshot_to_parquet(snapshot_id, cohort_partition)
worker.load_once(batch, pinned_catalogue_json, worker_release_manifest)

def assemble_building(building_id, snapshot_id, scenario_id=None):
    evidence = worker.batch.select(building_id, snapshot_id)
    resolved = resolve_evidence(evidence, pinned_selection_and_completion_rules)
    require_ready(resolved)  # retain unresolved/invalid/conflicting requirements
    fabric = resolve_building_archetype(**resolved.catalogue_arguments)
    baseline = BuildingSpec(**compose_native_inputs(resolved, fabric))
    model = baseline
    for event in ordered_typed_measures(scenario_id):
        model = apply_renovation(model, event.measure, **event.audit_arguments)
    store_immutable_model_and_receipt(baseline, model, evidence.references)
    return model

model = assemble_building(building_id, snapshot_id, scenario_id)
result = run_with_pinned_core_and_energyplus(model, unique_attempt_directory)
validate_and_commit_result(result, retained_artifact_checksums)
```

## Required pin manifest and identities

Every promoted model/cohort release needs this manifest:

- Source snapshot ID, selected-observation/geometry references, source manifests and asset hashes; selection, cleanup, imputation, completion and adapter rule/code versions.
- Data package/baseline/database identifiers; the original `BASELINE.json` and SHA-256 of every used JSON/evidence asset; catalogue assignment and component-state audit.
- Actual Core/Data/adapter source or wheel/container hashes, Git commits and dirty-tree status; dependency lock/backend versions; Core schema and compiler source hashes; EnergyPlus binary/schema/version and compiler settings.
- Weather URI/checksum/year, use/calendar/profile version, scenario definition hash, measure/factor references and numerical units.
- Full model snapshot serialization/hash, Core physical hash, compilation report/run ID, attempt ID/status and result artifact hashes. Define the full-snapshot canonicalization explicitly.

At inspection on 2026-10-05, Core reports `0.1.0.dev0`; Data reports `0.3.0.dev1`, baseline `epsm-data-baseline-v1.5.0`, dataset `LoD2 v1.4`, database version `1.5.0`. [BASELINE.json](/Users/ssanjay/GitHub/epsm_data/BASELINE.json:1) SHA-256 is `0b463e9a86a1fb982c632b2223ff73f5c51d2f4403219128870f84a4ea4311d4`; its catalogue asset SHA-256 is `188d52f1bc40683ed0b95923955968d937595663b096639c17c99937ab9f792c`. Core HEAD is `be78938a6dc440352b0ce89eb34669c2dc46ee59`; Data HEAD is `38ff4c782bcdc17d1950bae93daa26fb4240c69f`. Both working trees contain changes, so these commits alone cannot reproduce the inspected implementation. Capture the actual executed content. The Data manifest still marks empirical validation pending and its scoped MF study provisional, without population validation.

Keep three identities distinct:

- **Full snapshot hash:** adapter-defined hash including identifier, provenance, assumptions and renovation history; identifies the auditable stored document.
- **Physical hash:** Core excludes identifier, provenance, renovation history, assessment references and local EPW path; it includes weather content through its checksum. This identifies simulation inputs, not the execution environment: [physical hash](/Users/ssanjay/GitHub/epsm_core/src/epsm_core/specs/building.py:306).
- **Run ID:** take it from the actual compilation report. Core binds the physical hash, compiler/engine/schema/backend/settings identities and serialized compiled model. `CompilationReport.spec_sha256` is the physical hash, not the full snapshot hash: [input identity](/Users/ssanjay/GitHub/epsm_core/src/epsm_core/compiler.py:440), [compiled-model identity](/Users/ssanjay/GitHub/epsm_core/src/epsm_core/compiler.py:1365).

## National execution and result storage

Export each immutable cohort snapshot once to partitioned Parquet with a schema and checksum manifest; workers read bounded batches and cache the pinned catalogue locally. Avoid millions of per-building database round trips. An external queue dispatches partitions/building scenarios to parallel workers, with bounded concurrency, unique attempt directories, retries and explicit queued/running/accepted/rejected/failed states. Validate a small representative cohort and one municipality before national execution.

Deployment configuration supplies database endpoints, TLS and least-privilege roles; credentials stay in the deployment secret store. Separate ingestion privileges from snapshot reading and model/result writing, and prevent simulation workers from rewriting source observations. Queue infrastructure and worker environments are deployed separately from the stock database.

Store annual scalars, their units/energy boundaries, diagnostics, attempt status and artifact URI/checksum indexes in PostgreSQL. Store hourly arrays/time axes, EPW, compiled models and raw EnergyPlus outputs in external immutable storage. Preserve failed attempts and reasons. Core results expose scalars, hourly signals, units, diagnostics, artifacts and local-standard-time labels: [result contract](/Users/ssanjay/GitHub/epsm_core/src/epsm_core/results.py:47). Its final `result.json` marker does not replace external result-store validation: [completion marker](/Users/ssanjay/GitHub/epsm_core/src/epsm_core/runner.py:392). Every accepted annual result must resolve to its building/scenario/snapshot, compiled run ID and exact retained artifacts.
