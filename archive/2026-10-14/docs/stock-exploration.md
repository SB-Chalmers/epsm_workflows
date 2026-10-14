# Building-stock exploration

## What this workflow does

The stock workflow rebuilds national building information, predicts measured delivered space heat, tests whether stable physical subdivisions help, chooses actual representative buildings, and conditionally calibrates Core models. Buildings on the same property stay together. Test energy is unavailable to modelling stages and is opened only after choices are frozen.

Neighbourhood variables are diagnostic context. They never define physical archetypes. No catalogue is modified by this workflow.

## Run

Install the project's `stock` and `epc` optional dependencies alongside the pinned Core/Data sources. The installed command is `epsm-stock`; the equivalent source command is:

```bash
python -m epsm_workflows.stock prepare --config configs/stock-national.json --output output/my-stock-study
python -m epsm_workflows.stock regress --study output/my-stock-study
python -m epsm_workflows.stock cluster --study output/my-stock-study
python -m epsm_workflows.stock calibrate --study output/my-stock-study
python -m epsm_workflows.stock evaluate --study output/my-stock-study
python -m epsm_workflows.stock report --study output/my-stock-study
```

Preparation refuses an existing directory. Modelling stages refuse an existing stage or an opened holdout. Evaluation is idempotent for unchanged frozen inputs; changing choices or workflow code after opening test energy is rejected. Failed preparation attempts retain their diagnostics and are not complete studies.

## Data and eligibility

Versioned sources are configured in JSON: EPC linkage 2.0.0, national age registry, height harmonisation 1.0.0, newer raw EPCs, the supplied field dictionary, archived studies and pinned SCB population grid. Registry query contents, read source files and geometry partitions are hashed. Raw footprint geometry is preserved in metric EPSG:3006. No footprint is scaled to EPC Atemp.

The national inventory counts accepted links, non-single-building links and older/unread records. Predictive records require verified newer forms, measured positive heating, positive Atemp, twelve calendar months and unambiguous one-building linkage. Conflicting source rows or certificates sharing the latest measurement period are excluded. Earlier certificates are superseded by measurement period, never by certificate number or energy value. Certificate issue dates are absent in this export; that limitation is explicit.

Carrier energy quantities remain delivered inputs. Heat-pump electricity stays in separate families. Mixed carriers are preserved as their own named combinations. Solar-thermal boundaries are currently unresolved and excluded from predictive targets. The current schema evidence classifies heating inputs as normal-use-adjusted and before weather correction.

Usage proportions distinguish residential, office, school, hotel, retail, healthcare and other uses where reported. Blank completed renovation flags remain unknown. Only explicitly completed measures dated by measurement year become predictors; recommended or later measures do not. Design ventilation flow is already L/s/m² and is never converted into measured infiltration.

`archive-audit.json` records reused mappings, omitted legacy assumptions and verified BETSI population means. The passive-tracer survey means describe total air exchange (0.40 h⁻¹ in single-family, 0.52 h⁻¹ in multifamily homes); they cannot be assigned as additional infiltration. These are historical population context, not measurements of current EPC buildings. Archived façade estimates remain unused without dated source imagery and quality evidence.

Population is joined to the pinned 2025 SCB 1 km grid. Income/RegSO coverage comes only from unique coordinate matches to audited archived joins (income 2024, boundaries 2025); there is no guessed nationwide income coverage. These are retrospective context for older measurement periods.

National measured-year weather descriptors are not bundled. County and measurement year are geographic/time proxies, not BBR climate zones or audited weather. An optional `climate_descriptors` Parquet can provide unique municipality/year heating-degree days with source hashes, base temperature and correction status. Physical models separately require pinned local AMY files.

## Models and grouping

Each use/heating family needs at least 50 independent training properties and 10 validation properties. Others are recorded as unsupported. Archived and recent study properties are forced into training; property-name matches conservatively quarantine legacy IDs that cannot be resolved through current links. Remaining properties receive deterministic approximately 60/20/20 splits. Small strata and quarantined history affect final fractions.

Regressions compare an era/region median baseline, five-knot additive splines with ridge alpha 0.1/1/10/100, and a fixed boosted-tree benchmark (15 leaves, 200 iterations, rate 0.05, minimum leaf 50). Alpha selection and preprocessing are fitted within five property-separated training folds. Municipality-held-out development checks diagnose geographical shortcuts. Predictors are explicitly whitelisted; consumption, EPC energy classes and outcomes are excluded.

Known use/carrier/era/county/ventilation parent groups may subdivide using k-means k=2–5, seed 20261002 and 20 initialisations. Clustering uses physical features with adequate observed coverage. Minimum cluster size is 30 training properties. A subdivision must improve validation MAE by at least 5%, retain 20% error coverage, achieve median ARI >=0.7 across 20 property subsamples, and avoid reproducing missing-data patterns. Otherwise the group remains pooled. Population and income are excluded.

Representatives are actual development buildings nearest the training-fitted centre. Up to four bounded-distance contrast buildings come from different properties. Neither choice uses heating errors. Assignment output includes distance and low-confidence flags.

## Conditional physics calibration

`simulation-candidates.parquet` is a screened candidate set, **not a promise of accepted simulation readiness**. It initially contains pure-residential, detached-labelled MF district-heating buildings with F/FT/FTX, no reported heated basement or garage, supported height/roof geometry and gross/Atemp 0.95–1.20. An unheated basement, attic heating and actual wall exposure remain unverified. Failed geometry preparation is recorded.

A pilot requires at least 120 buildings (maximum 200, property-atomic cap) and 30/10/20 independent train/validation/test properties, again after compilation. It also requires documented energy boundary, heated extent, measurement-year systems, wall exposure, foundation, attic and operating assumptions. The default study stops calibration when those are absent.

To provide established evidence before the first calibration call:

```bash
python -m epsm_workflows.stock calibrate --study output/my-stock-study --evidence reviewed-physics.json
```

The reviewed JSON contains nonempty source/assumption descriptions for `energy_boundary`, `heated_extent`, `measurement_year_systems`, `exposed_walls`, `foundation`, `attic` and `operating_assumptions`; an explicit `catalogue_climate_zone`; and `weather` entries keyed by `municipality:year`, each containing absolute `path` and `sha256`. Optional `component_states` maps building IDs to independently evidenced `wall`, `roof`, or `window` catalogue states. `ftx_sensible_recovery` is a declared common assumption, not fitted per building.

The pilot compiles the matching era catalogue and runs annual smoke cases before the larger cohort. Compilation and numerical warnings remain strict. Training energy fits one nonnegative state mixture; validation chooses sequential shared wall/roof thickness multipliers 0.8–1.2 in 0.1 steps, then flow multipliers 0.8/1/1.2. Infiltration, material identities, windows and ground construction remain fixed except independently evidenced component overrides. Numerical failures reject candidates; they do not support heating conclusions. No holdout energy enters selection.

The initial delivery conversion is efficiency 1.0 plus 5 kWh/m² net losses, with final 0/10 sensitivities. These are study assumptions. A close mixture match does not identify renovation prevalence.

## Results

The report includes stock coverage and missing-information maps; validation feature-block gains and additive effects; spatial checks; accepted/rejected subdivisions and representatives; explicit calibration blockers; and untouched-property statistical predictions. Per-building and subgroup outputs include signed error, MAE, median absolute percentage error, 20%/10% coverage and property-bootstrap intervals (1,000 resamples).

If physics calibration is frozen, evaluate it and the equal-state frozen baseline on accepted paired test simulations, reporting failures against attempted counts and 0/5/10 loss sensitivities. A calibration improvement claim requires lower test MAE, better 20% coverage and a paired bootstrap interval supporting lower MAE. Further tuning requires fresh holdout properties.

No regression success rate is labelled calibrated-archetype accuracy when the physics gate is blocked.
