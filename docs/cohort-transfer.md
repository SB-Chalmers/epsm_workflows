# Fixed-data archetype calibration and transfer

Implementation: `epsm_workflows.cohort_transfer`, EPSM Data `cohorts`, Core native airflow controls, and smhi2epw provenance. Building observations are fixed. Unknown inputs remain declared bounded engineering assumptions. Native Core supplies every reported heating prediction; there is no fitted residual correction.

## Protocol

1. Verify source-only [archetype membership](archetype-membership.md), including EPC/GIS agreement and pure heated programme. Preserve membership quarantines in a separate inventory coverage denominator. Freeze eligible observations, native baseline specs, target boundary, weather, source/resource checksums, engine checksum and runtime versions. Keep each eligible failed preparation and simulation in the accuracy denominator.
2. Parent classes are use × EPSM era × ventilation × heating family × correction status. County does not force a split. Descriptor clustering uses geometry, nominal thermal/air exchange ratios and available operating/renovation evidence. Annual energy, EPC energy class and residuals cannot enter assignment. Robust scaling is fitted on training properties only.
3. Assign five outer property folds: all buildings belonging to one property stay together. Three inner property folds choose whether to subdivide, and whether baseline, one representative or shared fitting transfers best. All existing targets have previously been inspected; this is retrospective validation, not a fresh confirmatory dataset.
4. Mixed-distance medoids give deployable, target-free assignment. Subdivision needs ≥15 training properties per cluster, resampled adjusted Rand index ≥0.7, ≥5% validation MAE improvement and no decline in below20% coverage. Missingness-based subdivisions are checked separately. Buildings outside training descriptor support fall back to the parent baseline.
5. Morris screening covers all applicable, unlocked controls. Role-specific controls act only where the physical role exists: a basement temperature cannot change a building without a basement. Rank physical response using an uncentred sensitivity matrix about the frozen physical baseline; reject nearly dependent response columns by their singular-value ratio. Fit at most one parameter to one central representative; compare with up to three shared parameters fitted to a cohort. Bounds are engineering scenarios, not measurements. A quadratic search approximation is allowed only after held-out simulation checks; selected predictions always come from Core.
6. Freeze settings before opening each evaluation property's target. Report baseline, representative and shared methods, signed errors, failed simulations, worsened buildings, below20% and below10% coverage, and property bootstrap intervals.
7. A release needs ≥30 evaluated independent properties and ≥90% of attempted buildings strictly below20%. Cohort support also requires agreement between final descriptor partitions and every outer-fold partition. Final fitting repeats the same inner selection procedure on all available training properties; it does not substitute a vote over outer-fold decisions. Final coefficients are explicitly marked as not independently validated. Provisional profiles require explicit opt-in in Data; normal assignment falls back to the reference parent.

## Physical controls

`parameter-register.json` is authoritative for each building's applicable controls, baseline, frozen bounds, units, evidence and locks. Controls include wall/roof/ground/basement thermal resistance, window U-value, WWR, SHGC/shading, infiltration, natural and mechanical ventilation, heat recovery, people/equipment/lighting gains, heating setpoint, programme timing, thermal bridges, soil conductivity, attic exchange and heating delivery losses/efficiency. Independently evidenced component states and calendars are locked. Geometry and weather are not tuned to match energy.

Resistance changes retain reference material properties and vary effective layer thickness; this also affects capacitance. They are effective assumptions rather than inferred renovation histories. Office and school baselines use the existing versioned Sveby inputs, with a declared synthetic weekday calendar when actual dates are unavailable. School classroom use across the whole building is an explicit approximation; actual mixed programmes require a supplied native `spec_manifest`. Unresolved/mixed carriers and unknown geometry produce visible preparation failures. Heat pumps require an explicit COP scenario. Annual totals alone do not uniquely identify all these controls.

EPC basement definitions of “heated above 10°C” establish neither a 21°C setpoint nor fully heated floor area. Where the existing geometry contains a conditioned basement and has no independently specified zone programmes, a bounded 11–21°C basement setpoint can be tested through Core's native zone-use controls. This effective scenario does not establish the actual heated extent. Existing GIS purpose descriptions are retained as assignment descriptors; a residential EPC with a non-residential purpose is flagged rather than silently reclassified from its energy residual.

Core additionally supports independent infiltration and natural-exchange schedules and declared temperature/wind coefficients. The default remains constant design-flow infiltration. Reference-flow ACH is not an n50 measurement. FVP accounting rejects weather-dependent leakage. See EnergyPlus 24.2 [DesignFlowRate](https://bigladdersoftware.com/epx/docs/24-2/input-output-reference/group-airflow.html).

Existing local footprint/height/ground releases can supply native shading through `context_sources`. Unknown heights and overlaps are recorded. Adjacency does not automatically establish a heated party wall. Actual verified shared contacts need reviewed native context. No GIS acquisition is performed.

## Commands

Run with the Core virtual environment and Workflows/Data/smhi2epw sources on `PYTHONPATH`:

```sh
python -m epsm_workflows.cohort_transfer weather --config configs/cohort-transfer-gothenburg-weather.json --output output/cohort-transfer-gothenburg-weather-2026-10-04
python -m epsm_workflows.cohort_transfer prepare --config configs/cohort-transfer-mf-se02-pilot.json --output output/cohort-transfer-mf-se02-pilot-2026-10-04-v6-membership-reviewed
python -m epsm_workflows.cohort_transfer run --campaign output/cohort-transfer-mf-se02-pilot-2026-10-04-v6-membership-reviewed
python -m epsm_workflows.cohort_transfer export --campaign output/cohort-transfer-mf-se02-pilot-2026-10-04-v6-membership-reviewed
python -m epsm_workflows.cohort_transfer inventory --study output/stock-national-2026-10-02-v2 --output output/cohort-transfer-matrix-support.json --campaign output/cohort-transfer-mf-se02-pilot-2026-10-04-v6-membership-reviewed
```

Use `cohort-transfer-mf-se02-full.json` and a new campaign directory for all 126 existing buildings. The revised full campaign is `output/cohort-transfer-mf-se02-full-2026-10-04-v5-membership-reviewed`. Preparation refuses an existing output directory. Simulation batches use spawned processes to avoid concurrent SQLite-reader contention. `import-cache --campaign NEW --previous OLD` can reuse accepted native cases after identical Core-source/engine, physical-spec, weather-payload and completion-scalar checks; archived receipt/spec/result checksums remain pinned. Runs resume with content-addressed simulation receipts; changed source, specs, inputs, weather or engine cause a hard refusal rather than reusing incompatible results. Numerical results are written under the campaign directory. `progress.json` reports completed folds. `evaluation.json` is the actual transfer result, `failures.json` records preparation exclusions, and simulation receipts retain all failed cases. Exports do not mutate the reference matrix or database.

Weather regeneration uses existing raw weather caches and declared AMY reconstruction settings. smhi2epw's optional `--provenance`/`EPWConfig.provenance_path` records actual consumed-response hashes, source code and output hash. Unrelated cache files do not count as evidence. Weather alternatives must be declared independently of heating residuals.

`examples/epc_validation/finish_cohort_transfer.py --campaign CAMPAIGN --study STUDY --notes docs/archetype-implementation-log.md` exports final profiles, builds the cell support inventory, renders the accuracy figure and records the measured outcomes. `--wait --producer-log LOG` waits for a running experiment's atomic evaluation; it stops on a producer traceback or its declared timeout. It verifies complete attempted-building accounting before reporting. `completion.json` distinguishes waiting, exporting, completed and failed stages; a waiting campaign supplies no final accuracy claim. Reports and figures are saved under `artifacts/`.

## Interpretation and references

A shared physical correction is useful only if it improves held-out properties and its assignment is possible without their energy observations. Residual improvement by itself does not establish the causal reason for an archetype's bias. Cluster supports and parameter confounding determine whether to retain a parent, subdivide it or leave the cohort provisional.

The hierarchical calibration rationale is informed by [Kristensen, Hedegaard and Petersen (2018)](https://doi.org/10.1016/j.enbuild.2018.07.030). Their study used 3-hourly measurements and evaluated aggregate time-series errors; those results do not establish a per-building annual 10% guarantee for this fixed annual dataset. Operation references remain the repository's versioned [Sveby inputs](https://www.sveby.org/hur-anvander-jag-sveby/berakning/).

National support accounting includes all four uses and six matrix eras, including cells without a physical campaign. Successful preparation or synthetic validation never grants a real-building accuracy claim.

Exported profiles are bound to the recorded campaign sources, v1.4 reference matrix, operating assumptions and evaluated descriptor/geographic domain. Applying them to another matrix revision, Core revision or municipality requires a new validation campaign. Descriptor proximity alone cannot establish geographic transfer when every evaluated building is in Gothenburg.

## Parent-first execution

`configs/cohort-transfer-mf-se02-parent-validated.json` freezes the source-boundary
 audit, keeps one parent, adds measured existing context and excludes normal-use
 gain, temperature and timing adjustments from fitting. Those controls still
 appear in diagnostic screening. Selection compares below20 coverage, then
 equal-property capped percentage loss, then complexity; failures count in coverage
 and incur a declared loss penalty, without a fabricated annual error.

```sh
python -m epsm_workflows.cohort_transfer prepare --config configs/cohort-transfer-mf-se02-parent-validated.json --output output/cohort-transfer-mf-se02-parent-2026-10-04-v10
python examples/epc_validation/complete_parent_campaign.py --campaign output/cohort-transfer-mf-se02-parent-2026-10-04-v10 --study output/stock-national-2026-10-02-v2 --membership-audit output/archetype-membership-gis-led-2026-10-04-v2/national-membership-audit.parquet --report-root output/archetype-parent-validation-2026-10-04-v2
```

`execution.json` records the actual process and terminal stage. `assessment.md`,
`building-results.json` and `sensitivity-atlas.json` are generated only after nested
validation and final export complete. A source or Core correction requires a new
campaign; a rejected or interrupted run supplies no release claim.

Joint-search approximation checks and native fallback operate per building. A failed
anchor or any failed/>2% held-out joint check forces that building through the
complete native grid. Every accelerated building independently passes all four
checks. The shortlist always uses native predictions for final fitting and deployment.

Source hydration retains EgenProjVentFlode as declared design flow, plus dated
AtgUtforda completed measures. Dated replacement is an assignment descriptor,
not a measured U-value or a licence to assume a particular renovation package.
Unknown or undated measures remain explicit; qualitative evidence does not lock
a quantitative parameter whose value is still unknown.

After completion, run `examples/epc_validation/summarize_parent_misses.py` with
`--campaign` and `--destination` to retain descriptive held-out strata and fitted
boundary hits. These diagnostics do not change the frozen search or grant
sub-archetype support. Completed native SQL can be losslessly archived as
`eplusout.sql.gz`; each `sql-archive.json` records a verified original checksum
and restoration command. Receipts and failure diagnostics remain available.

`examples/epc_validation/apply_cohort_profile.py` applies a saved bundle to a
native spec and a single audited feature record. Supply `--spec`, `--descriptors`,
`--bundle` and `--output`. It verifies the GIS-led membership policy and exact
reported Atemp load reference, preserves `locked_parameters`, and writes
`spec.json` plus `assignment.json`. Provisional profiles require
`--allow-provisional`; otherwise the baseline physical spec is retained. A
source-inconsistent or out-of-scope building receives an auditable parent fallback.
The command is an application step, not an additional energy validation study.

## Data 1.5 fixed-property rerun

The Data 1.5 MF SE.02 rerun is
`output/cohort-transfer-mf-se02-parent-data15-2026-10-04-v11`, with configuration
`configs/cohort-transfer-mf-se02-data15-rerun.json`. It retains v10's 85 buildings,
54 properties, property folds, observations, parameter bounds, weather and
non-airflow physics. Existing-state additional infiltration is now 0.20 L/s/m²
Atemp, passed through Data's native `core_airflow_inputs`, with ACH zero and an
explicit Atemp load reference. Window U=2.2 W/(m²K) was already present in v10.
The old fitted infiltration multiplier is not applied to the new baseline.

`epsm_workflows.cohort_transfer.rerun --verify-only --previous OLD --campaign NEW`
checks population, target, protocol and physical identity before execution. The
completion helper's `--previous OLD` also produces baseline and final paired
comparisons, including property-bootstrap intervals, worsened buildings and
failure transitions. Coverage includes every attempt; paired MAE changes use
complete pairs. Changed population, unrelated physics or parameter bounds stop
the rerun rather than silently confounding the comparison.

The actual runtime sources, including uncommitted changes, are frozen under
`output/archetype-parent-validation-data15-2026-10-04-v3/runtime`; its sibling
`runtime-identity.json` records original Git states and file hashes. Execute or
resume with that directory's `run.sh`, after checking `launch.json` and the
campaign's `execution.json` to avoid starting concurrent producers. The script
uses the frozen runtime for baseline, nested validation, final export and reports.
It does not import old simulation caches or regenerate weather.

`artifacts/rerun-integrity.json` records the pre-run checks. After baseline,
`artifacts/rerun-comparison.md` and `.json` show the baseline change; after
calibration they also show the new calibrated procedure. `execution.json` and
`progress.json` distinguish an active campaign from completed validation. The
export remains separately versioned and requires the existing accuracy/support
gates. No calibrated result is available until those stages complete.

Completed native SQL and large text outputs are losslessly compressed with
verified original/archive checksums and restoration commands alongside each file.
Active outputs, receipts, scalar results and scientific failure policy are
unchanged. The archival process stops when the producer completes or fails.

Data 1.5's starting values used earlier development observations. This rerun is
retrospective validation; independent confirmation requires fresh properties.
National statistical exploration is unchanged by this focused campaign.

## Catalogue handover after Data 1.5 completion

The current EPSM Data profile is
`cohort_profiles/2026-10-05-v2/mf-se02-gothenburg.json`, indexed by
`cohort_profiles/current.json`. It supersedes the historical ACH-based v1 profile.
Final selection is baseline with empty overrides; Data 1.5 numerical defaults
are retained. Baseline coverage is 54/85 within 20%, whereas the fold-dependent
selection procedure achieves 55/85. These scores describe different outputs.
The packaged `parametric/mf_se02_data15_validation.json` retains source hashes,
exact metrics, fold selections and limitations. Status remains provisional for
Gothenburg existing MF SE.02/F/district heat; default assignment falls back to
the parent. Other eras, states, locations and systems have no support from this
study. This handover does not change the frozen campaign.

## Random national baseline check

`examples/epc_validation/run_national_random_baseline.py` draws a reproducible
simple random sample of buildings from the complete membership-audited
MF SE.02/F/district-heat parent, before geometry/weather preparation. The
2026-10-05 run uses seed20261005 and200 buildings from5716 eligible records:
180 properties and94 municipalities. Neither observed heating nor residuals enter
the draw; prior-studied buildings remain eligible. No failed case is replaced.
The runtime source/resources are copied and hashed before weather or simulation.
Actual-year weather is compiled locally for sampled municipality/year points.
Geometry follows the previous measured-height/rectangular-pitched-footprint
rules; source failures remain attempted cases. Native neighbour shading is
retained and unknown heated contacts are not invented. All-selected coverage,
prepared-only metrics and property-bootstrap uncertainty are separate outputs.
No calibration, component override or catalogue change is performed by this run.
Current artifacts: `output/national-mf-se02-random-baseline-2026-10-05-v1`.


## Roof and preparation correction (2026-10-05)

Future native baselines use `epsm_core.preparation.prepare_geometry` with the
versioned bounded metric policy. Only accepted results enter the shared geometry
builder; source geometry, cleaned geometry, policy, change metrics and edge
lineage are retained in the national input rows and campaign feature receipts.
Review/rejected results remain failures. Heights, EPC Atemp and roof classes are
not inferred or adjusted by footprint cleanup. This replaces the national
runner's obsolete rectangular-proxy and floor-height rejection gates; unusual
floor heights remain explicit review flags.

Core now rejects roof facets that its minimum-edge treatment wholly collapses
onto the attic floor, before creating duplicate thermal boundaries. It retains
existing treatment for facets with positive clearance. The workflow cleanup
resolves the audited thin-eave case without relaxing surface or engine checks.
Existing frozen campaigns remain unchanged and must be rerun with a newly pinned
runtime to use this correction. Geometry-only evidence is in
`output/national-mf-se02-roof-preparation-fix-2026-10-05-v1`.


## Unclear-to-flat completion scenario (2026-10-05)

For generated native baselines, source `roof_form=unclear` now selects a compact
flat roof at the determined measured `representative_height_m`. Source roof
classification remains unchanged; `modelled_roof`, `roof_fallback_applied` and
`roof_assumption` record policy `unclear_to_flat_v1`, height source and uncertainty.
Missing/invalid measured heights and other unsupported roof labels remain failures.
Known flat and pitched classifications retain their existing model choices.

National evaluation reports the source-unclear/fallback cohort separately from
classified roofs, retaining preparation/weather/simulation failures in the group
denominators. This completion scenario can change roof area, attic buffering and
conditioned volume; it is not evidence that the actual roof is flat. Geometry-only
verification of the unchanged 200-building sample passes all 200, including all
41 fallback buildings. Twenty-eight unusual floor-height review flags remain.
Evidence: `output/national-mf-se02-flat-roof-fallback-2026-10-05-v1`.
No new annual predictions or calibration results have been generated by this check.

### Missing measurement-year weather (2026-10-05)

`compile_measurement_weather` explicitly enables smhi2epw's bounded nearby-observation
fallback (`metobs_gap_fallback=True`, maximum 75 km, three candidate stations).
Callers can disable it or tighten the radius/station limit with the corresponding
keyword arguments. The library default remains disabled. Required observations
are validated before filling; donor data uses the same UTC hours and accepted
SMHI quality codes. Temporal interpolation retains its 48-hour ceiling. Solar
queries and target pressure derivation remain at the requested site.

The weather receipt retains original missing/invalid counts, donor IDs and
coordinates, distances, replaced UTC intervals and overlap differences, alongside
`required_reconstructed_fraction`. Review these diagnostics before qualifying an
archetype: complete EPW formatting does not establish weather accuracy, and
long or whole-variable substitutions remain a distinct source of uncertainty.
The replay under `output/weather-robustness-2026-10-05-v1` preserves the original
national run and weather files; it does not rerun annual building simulations.
