# MF default revision

This revision covers 216 MF classification slots (six eras, three historical
zones, four ventilation types and three construction states), backed by 18
canonical constructions. Existing source fabric is not a measurement of surviving
building condition. No construction change is accepted from retrospective energy
fit alone.

## Current findings and decisions

- The historical 200-building sample covers SE.02/F, not the whole MF family.
  Its completed window and roof measures were retained as descriptors but did not
  change compiled components. Quantitative independent evidence now has a native
  application path. Checkbox-only records still do not identify component U,
  glazing optics or insulation thickness.
- Eighteen degrees for all conditioned basements is not accepted as a new default.
  The prior screened panel fixed six 20% misses and created five. New separate
  cases distinguish temperature, residential gains and mechanical flow. Zero-gain
  and zero-flow cases are diagnostic bounds, not recommended storage-room inputs.
  Heated extent, unheated-basement presence and basement depth remain unresolved
  unless established independently. Geometry is never fitted to heating errors.
- Historical Zone 1 is counties 23–25; Zone 2 is 17/20/21/22; other counties are Zone 3.
  This is the BBR18 construction grouping, not a claim about current rules.
  Fgeo and actual-year weather remain separate. Regional wall/top R+20/+40% are
  hypotheses. Their added insulation depths are exported for assembly review.
- Reported design airflow is a completion assumption, not observed operating flow.
  Native quantitative operating observations require Atemp units and a source.
  Zero and unusual reported design flows remain visible; no silent clipping occurs.
- Attic cases add one native outdoor-exchange assignment, avoiding duplication.
  Unclear roofs retain their explicit flat-completion policy. Height, heated area,
  adjacency and roof evidence precede introducing height-dependent defaults.
- The archived BETSI audit includes 559 MF observations, but audited MF age joins
  and survey weights are absent. Its component distributions are unweighted
  survey-state descriptions. They cannot identify era/zone original constructions.
  The archived TABULA zone audit found 240 component comparisons with no differences;
  it supplies no numerical regional correction by itself.
- Additional leakage starting values are engineering assumptions. Wind/stack and
  exhaust pressure coupling remains unresolved. Review WWR, glazing optics,
  thermal-bridge inclusion, ground coupling, gains and target accounting using the
  existing diagnostic workflows before widening the fitted parameter set.
- Synthetic reconstruction remains a secondary robustness check, following prior
  tests; reconsider it if reconstruction or energy-boundary inputs change.

Primary source register: [BETSI database](https://www.boverket.se/sv/om-boverket/oppna-data/betsi-oppna-data/databasen-fran-betsi/),
[Swedish TABULA](https://episcope.eu/building-typology/country/se/),
[Sveby residential 2.0](https://www.sveby.org/wp-content/uploads/2024/11/Sveby-brukarindata-bostadshus-2.0-241111.pdf),
[historical BBR18](https://rinfo.boverket.se/BFS2011-6/pdf/BFS2011-6.pdf).
Source observations, nominal assembly values and effective ground references have
different boundaries; never tune their numerical differences away automatically.
The original SBN tables and permit transitions are reviewed in
[regional construction evidence](mf-regional-construction-evidence.md).

## Commands and immutable outputs

Use an environment with the stock dependencies, EPSM Core, EPSM Data and smhi2epw.
The local full runtime can be selected without installing or changing any package:

```sh
export PYTHONPATH="$PWD/src:/Users/ssanjay/GitHub/epsm_core/src:/Users/ssanjay/GitHub/epsm_data/src:/Users/ssanjay/GitHub/smhi2epw/src"
python -m epsm_workflows.mf_revision audit \
  --config configs/mf-revision-national.json --output output/NEW-mf-inventory
python -m epsm_workflows.mf_revision prepare-national \
  --study output/NEW-mf-inventory \
  --weather-campaign output/national-mf-se02-random-baseline-2026-10-05-v3-weather-shadow/campaign \
  --output output/NEW-mf-national-preparation
python -m epsm_workflows.mf_revision prepare-screen \
  --study output/NEW-mf-inventory \
  --source-campaign output/national-mf-se02-random-baseline-2026-10-05-v3-weather-shadow/campaign \
  --properties 12 --output output/NEW-mf-screen
python -m epsm_workflows.mf_revision run-screen --study output/NEW-mf-screen
python -m epsm_workflows.mf_revision evaluate-screen \
  --study output/NEW-mf-screen \
  --targets output/national-mf-se02-random-baseline-2026-10-05-v3-weather-shadow/building-results.json
```

Each directory is new. Preparation refuses overwrite; source, runtime, weather,
case specifications and design hashes are checked on resume. Accepted, failed and
skipped cases remain explicit. Interrupted simulation directories are retained.
Three baseline smoke cases precede the broader panel; a smoke failure stops it.

Reported-zero mechanical systems need an absolute-flow diagnostic: multiplying
zero cannot explore operating uncertainty. Prepare it with:

```sh
python examples/epc_validation/prepare_mf_zero_flow_screen.py \
  --inventory output/NEW-mf-inventory \
  --source-campaign output/COMPLETED-mf-national-preparation \
  --output output/NEW-mf-zero-flow
python -m epsm_workflows.mf_revision run-screen --study output/NEW-mf-zero-flow
```

The alternative uses the existing 0.35 L/s/m² Atemp normal-use completion and
retains reported zero as the baseline. BBR18 6:251 gives a design floor-area
requirement, which supplies plausibility rather than evidence of annual operation
on Atemp. Independently observed operating flow is excluded from this diagnostic.
Matching accepted baseline receipts may be reused only with identical physical
hashes, Atemp and engine/schema identity; reuse provenance is exported.

The MF campaign config now selects `mf_reported_zero_flow_policy: normal_use`.
It completes a reported-zero mechanical flow using the existing residential
normal-use rate, records both reported and compiled values, and preserves positive
design values. Independent quantitative operating evidence takes precedence,
including an explicitly observed zero. Generic/legacy preparers default to
`retain_reported`; the policy does not alter other building-use profiles or the
numerical construction catalogue. Use a new study directory when changing it.

The inventory selects 1200 independent properties by round-robin physical strata
without energy access. Known extraction mismatches are quarantined from the fixed
audited list, without removing energy tails. Property splits use 60/20/20 proportions
for fresh properties; previously examined properties always enter development.
The default config conservatively treats the entire previously evaluated national
statistical stock as inspected. A new extraction does not erase that history.

`prepare-national` records all 1200 outcomes, including unavailable measurement-year
weather and native preparation failures. Reused municipality/year weather is an
explicit representativeness assumption, not building-specific validation.
Prepared native baselines cover available era/system groups. They can be run using
`run-screen`; missing-weather records remain in preparation coverage accounting.

`prepare-screen --properties 12` selects up to 12 input-diverse properties **per
era/system family**. Separate scenarios cover wall and top R, operating flow,
cold-attic exchange, basement thermostat, basement gains and basement flow.
Independent input locks are respected. No footprint, conditioned extent or Atemp
changes. `evidence_applied` is equal to baseline if quantitative observations are
absent; this is recorded as unresolved evidence rather than an inferred renovation.

After separate development evaluation, `prepare-joint --study SCREEN --controls
CONTROL_FILE --output NEW_DIRECTORY` freezes 32 Latin-hypercube designs for one to
four explicitly justified controls. The JSON maps control names to `bounds` and
`source`. Allowed screened envelopes are wall/roof total R factors 1–1.4, operating
flow multipliers 0.8–1.2, basement setpoint 15–21°C and attic exchange 0–2 ACH.
Structural layers cannot substitute for missing insulation. Joint cases remain
development evidence; inspect their exact added depths before adoption.

## Quantitative evidence interface

`mf_evidence` on an input row maps `window`, `wall`, `roof` or `operating_flow` to
independently sourced observations. Every item needs `source`, `classification`
(`measured` or `source_derived`), `valid_from_year` strictly before the measurement
year and `valid_through_year` covering it. Same-year renovations cannot represent
the whole year without more detailed timing.

Window evidence requires `definition: whole_window_including_frame` and
`u_value_w_m2k`; SHGC/visible transmittance are optional independently sourced
values. Wall/roof evidence requires `boundary:
nominal_assembly_including_surface_films` and `u_value_w_m2k`. Its implementation
adjusts the equivalent insulation depth at unchanged material properties and
records that the exact layer stack is not observed. Operating flow requires
`quantity: operating_outdoor_air`, `area_basis: Atemp` and `l_s_m2`.
Natural-ventilation exchange requires a separate physical model.

Automatic equivalent-insulation adjustment is limited to reviewed MF stack
candidates with conductivity at most 0.1 W/(m·K) and density at most 200 kg/m³.
Conductivity alone can select timber or lightweight structural layers. This
conservative guard is not material identification; unrecognised assemblies need
review. All 208 previously simulated national fabric cases select exactly the
same layer and added depth under the tightened guard.

The shared cohort preparer applies this evidence to generated and supplied native
specs, exports compiled-value provenance and locks the corresponding parameters.
Legacy rows without `mf_evidence` keep their existing behavior.

## Confirmation, release and remaining work

Use `archetype_application` for complete, independently reviewed common-family
baseline/candidate specs and sealed targets. Choose `coverage20_then_mae`;
selection now breaks remaining ties using absolute bias, then baseline retention.
Freeze the final candidate before final-test targets are opened. Group validation
and testing by property, and run a municipality-blocked development stress check.

`promotion_gate` requires independent physical evidence, untouched evaluation,
90/30/30 development/validation/test properties and complete paired simulations.
An improvement must have a positive lower 95% coverage-change bound, or at least 5%
lower MAE with its paired interval below zero and coverage lower bound no worse
than −2 percentage points. Adequately sampled subgroups cannot lose more than 5
coverage points or increase MAE by 10%. Assess validation and final test separately.
Bootstrap sampling preserves all buildings within a resampled property. Failures
remain coverage misses; MAE intervals are explicitly conditional on accepted pairs.

The present inventory is **not confirmation-ready**. Completing a supported release
requires corrected extraction inputs, independently reviewed component/operation
evidence, measurement-year weather coverage, and genuinely untouched properties.
If these cannot be obtained, report retrospective performance and retain provisional
source defaults. Sparse slots inherit the reviewed construction parent; no subtype
is labelled independently validated merely because a model can be compiled.

Release deliverables are the 216-slot register, sourced before/after decisions,
paired validation and final-test reports, compatibility checks and the retained
previous baseline. This implementation does not automatically edit EPSM Data or
publish a new numerical default catalogue.
