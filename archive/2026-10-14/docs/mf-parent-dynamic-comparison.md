# MF parent calibration and component histories

The workflow compares prediction for properties whose heating observations are
unavailable at assignment time. All current observations support retrospective
development evaluation. Nested property folds do not constitute independent
confirmation. Existing EPSM Data construction references are preserved.

The executable is `epsm-mf-parent-dynamic`, or
`python -m epsm_workflows.mf_parent_dynamic`. It reuses the
`cohort_transfer` native evaluator, physical descriptors, property folds,
candidate designs, source guards and content-addressed receipts. The original
scientific configuration declares eight spawned workers. The user-authorised
continuation uses the existing `Evaluator(root, workers=12)` constructor through
an output-only wrapper, retaining that frozen configuration and the same physics,
targets, property folds and receipt identities. Strict Core/EnergyPlus diagnostics
and interrupted outputs remain recorded. Target observations live in separate
per-property files.

## Reproducible commands

Use the existing Core environment with EPSM Workflows, Data and smhi2epw sources
on `PYTHONPATH`. Weather compilation also needs pvlib and its numerical runtime.

```sh
python -m epsm_workflows.mf_renovation_eda \
  --database /path/to/epc_sweden.duckdb \
  --output output/NEW-mf-renovation-eda
python -m epsm_workflows.mf_parent_dynamic prepare \
  --selection output/mf-revision-national-2026-10-08-v6 \
  --existing output/mf-revision-national-prepared-2026-10-08-v4 \
  --weather output/mf-parent-dynamic-weather-2026-10-08-v5-latest-merged/latest-weather-manifest.json \
  --scope-evidence output/mf-parent-dynamic-expanded-2026-10-09-v8/scope-audit-selected-1200.json \
  --output output/NEW-mf-parent-dynamic
python -m epsm_workflows.mf_parent_dynamic run \
  output/NEW-mf-parent-dynamic --stages baseline parents dynamic report
```

To expand the panel, complete `mf_weather_recovery` first, pass its completed
`weather-manifest.json` through `prepare --weather`, and create a new study
directory. Freeze that panel before comparing models. Weather failures,
unavailable cached sources and geometry failures remain in the denominator of
preparation reporting. Analysis does not silently substitute typical-year
weather for measurement-year weather. Network recovery uses strict SMHI-only
compilation; offline recovery forbids all HTTP and can reuse previously cached
sources with their provenance.

## Frozen development panel and comparison versions

The replacement comparison is prepared in
`output/mf-parent-dynamic-expanded-2026-10-09-v9`. V8 was stopped before fitting
when the independent scope audit found one certificate covering two building
subjects but only one accepted footprint. All 1,200 selected certificates have
matching release, complete archive and repeated-source-row scope evidence:
1,199 single-subject certificates and one quarantined two-subject certificate.
The scope evidence is checksum-pinned and checked before weather or preparation.
Missing, disagreeing or multi-subject scope never enters training or evaluation.

The superseded v8 panel had 1,067 prepared properties from 1,200 selected.
All selected properties retain a preparation outcome. Sixty-nine weather jobs
fail hard quality checks and leave 127 properties without accepted
measurement-year weather; six further properties fail geometry preparation.
Weather availability covers 1,073 properties. Simulation failures remain outside
coverage within each frozen prediction panel. V9 freezes
1,066 properties: 233, 262, 180, 178, 96 and 117 across SE.01–SE.06. The 1,200
selected properties retain 127 weather failures, six preparation failures and
one certificate-scope quarantine. Current and shading-only panels each import
1,065 accepted exact-input native predictions. No changed-gain parent cases are
reused. The native run retains any additional failures in coverage denominators.

At the 2026-10-09 09:08 UTC reporting checkpoint, the twelve-worker continuation
is prepared but **has not been dispatched**. The [root cache audit](../output/mf-parent-dynamic-expanded-2026-10-09-v9/native-twelve-worker-cache-audit-v2.json)
and [independent cache audit](../output/mf-parent-dynamic-expanded-2026-10-09-v9/native-twelve-worker-independent-cache-audit.json)
exhaustively reconstruct all 10,402 existing principal receipt keys with eight,
twelve and the wrapper's partial twelve-worker factory. Every key is identical;
neither audit reads targets or launches a worker pool. The
[independent wrapper review](../output/mf-parent-dynamic-expanded-2026-10-09-v9/native-twelve-worker-independent-review.json)
binds those audits and the exact wrapper. Restart remains gated on the original
pool being closed, an explicit root dispatch permit, at least 35 GiB actually
free and no more than two compression workers within the user's fourteen-worker
budget. A running or completed claim requires the actual
`native-twelve-worker-run.json` record. Cache identity establishes execution
compatibility, not native accuracy, emulator agreement or adoption.

Completed resource evidence includes the
[historic SQL capacity update](../output/mf-parent-dynamic-expanded-2026-10-09-v9/resource-forecast/archive-capacity-update.md)
and [transparent native JSON batch 1](../output/mf-parent-dynamic-expanded-2026-10-09-v9/native-transparent-json-batch1-summary.json).
The latter records 1,250 cases, 5,000 byte-preserved files, zero failures and
1.333 GiB of measured allocated-block saving. Those savings are already reflected
in current availability; pending archive/patch batches receive no credit.
Resource maintenance preserves frozen inputs and does not establish prediction
improvement. Final comparisons, benchmark accuracy and adoption remain pending.

V9 also aligns the fixed annual household reference with BEN: 30 kWh/m² of
household electricity and 70% useful heat, or 21 kWh/m² annually. The timing
proxy remains Sveby 2.0; temperatures, occupancy, lighting, DHW, geometry and
weather do not change, and household gains are never fitted. Current and the
separate shading-only baseline retain the current catalogue's 14.7 kWh/m²
useful gain. Exact physical hashes retain those baselines; parent or history
cases with different gains must be simulated again. Native attribution separates
shading repair, household-reference alignment and further parent fitting.
Individual EPC normalisation completeness cannot be verified from the exports.
BEN's reference is a fixed sourced comparison choice, not an inferred correction
for individual properties. See [BEN Tables 2:2 and chapter 3 §5](https://www.boverket.se/resources/constitutiontextstore/ben/PDF/konsoliderad_ben_bfs_2016_12.pdf).

The v6 native campaign was subsequently stopped for a confirmed dynamic
source-basis flaw: SE.02 uses a surviving-stock window default of 2.2 W/m²K in
EPSM Data, while its historical construction reference is 2.8. Avoiding fitted
parent coefficients and renovation overrides does not establish that Data
defaults are original historical assemblies. V7 supplies separate, sourced
historical specifications for histories and preserves the current/parent
physical inputs. SE.02 remains at 2.2 W/m²K in the
current/parent baseline, while the original-history reference uses the preserved
2.8 W/m²K assembly. `original_reference_spec_path` points to those separately
frozen inputs; the registry identifies their historical basis and provenance.
Original-period component claims are reviewed before preparing histories.
V6 native comparison remains superseded and its strict source guards remain
active. The statistical derivatives retain their original computation provenance
on unchanged descriptors, targets and property folds.

The panel contains district-heated MF in every construction era, all three
climate zones and all four ventilation systems. Selection balances archetype
parents and physical strata rather than weighting the national population.
Weather and geometry availability further change its composition. The final
[panel-support report](../output/mf-parent-dynamic-expanded-2026-10-09-v9/panel-support.md)
records availability by era, zone, county, ventilation, basement, storeys and
measurement year, plus the actual property counts in every nested training fold.
Preparation succeeds for 343/373 in Zone 1 (92.0%), 313/333 in Zone 2 (94.0%)
and 410/494 in Zone 3 (83.0%). Nine of 24 era/ventilation groups have fewer than
30 prepared properties; 13 fall below 30 in at least one inner training fold.
Every era has at least 30 inner training properties when sharing construction
parameters across ventilation systems. Two 30-property children are count-feasible
in every inner fold for SE.01–SE.04, while SE.05 and SE.06 lack that support in
at least one fold. Actual group balance, stability and prediction gates remain
required. Whole-stock national accuracy and independent confirmation cannot be
inferred from this panel.

The latest weather package has 391 accepted municipality/year jobs from 460
attempted jobs. Every accepted file matches the complete source hashes of the
smhi2epw v1.1.1 checkout, passes complete-calendar and actual-measurement-year
checks, and retains its source provenance. Source policy remains distinct from
compiler version: **188 jobs use strict SMHI inputs; 203 automatic jobs reuse
cached inputs, including 27 ERA5-assisted jobs**. Latest compiler use does not
imply observed-only or strict-SMHI-only weather. STRANG is modelled solar;
bounded donor/temporal recovery, direct-component decomposition and estimated
infrared remain explicit. The V9 panel uses 388 weather jobs: 187 strict jobs
serve 373 prepared properties, while 201 automatic jobs serve 693. The latter
include 27 ERA5-assisted jobs serving 116 properties. The 1,066-property
panel-support derivative reports weather quality quantities weighted to its
prepared properties rather than copying earlier panels' weights.

All 82 older weather jobs were rebuilt using the latest compiler at their
original declared query points, with cached responses and no new HTTP requests.
All succeeded; no centroid fallback was selected. Their EPW hashes changed,
affecting 391 earlier prepared properties. Original weather and native receipts
remain preserved. V7 reuses 4,210 accepted main cases, including 1,066 corrected
baselines, and 269 current receipts only where reconstructed physical inputs, weather, carrier,
Atemp, engine and execution-module identities agree. The two declared workflow
source exceptions concern historical reference generation and orchestration;
they permit parent/current cache transfer only. Dynamic-bank results do not
transfer from v6. Updating a valid template's measurement-year weather
preserves its validated geometry, Atemp, HVAC and construction inputs.

The 694-property v4 panel, its statistical benchmarks and its paired native
current/shading baseline are **superseded historical diagnostics**. The native
campaign was stopped after a confirmed solar-processing issue in its legacy
weather inputs. Those baseline figures cannot establish current model accuracy
or a calibration improvement. They remain in their original output directories
with their original input hashes. Metrics from different panel versions are not
paired improvements. V9 native comparisons and statistical benchmarks must use
the same scope-corrected properties and new exact property folds.

The [statistical continuity audit](../output/mf-parent-dynamic-expanded-2026-10-09-v7/statistical-continuity.md)
checks v6/v7 predictor, target and fold equality while preserving the original
computation hashes. The [statistical benchmark diagnostic](../output/mf-parent-dynamic-expanded-2026-10-08-v6/benchmark-diagnostic.md)
uses the existing nested empirical-parent and reliable-feature routines on that
superseded pre-scope-audit panel. Its exact out-of-fold predictions, property folds, inner
selection audits and source hashes are retained. These are statistical
prediction benchmarks, not native EnergyPlus accuracy:

| Historical statistical predictor, 1,067 properties | Coverage within ±20% | MAE, kWh/m²/year | Error SD, kWh/m²/year |
|---|---:|---:|---:|
| Empirical era/zone parent | 51.92% | 21.992 | 28.626 |
| Reliable building features | 61.20% | 17.982 | 23.789 |

Reliable features improve paired coverage by 9.28 percentage points (95% property
bootstrap interval +6.09 to +12.65) and lower MAE by 4.010 kWh/m²/year (interval
−4.920 to −3.106). The residual spread remains substantial. These comparisons
show predictive information beyond an era/zone mean; they do not identify an
observed renovation history or demonstrate a native construction correction.
The earlier 694-property benchmark reached 64.12%, but the changed denominator
prevents interpreting the lower 61.20% as a paired deterioration. Native cohort
and building-level prediction results will be reported separately for v9.
Historical statistical figures above and stopped native v6–v8 runs do not
establish acceptance on the final scope-corrected panel.

The [historical same-panel feature ablations](../output/mf-parent-dynamic-expanded-2026-10-08-v6/feature-ablation.md)
reproduce the full benchmark exactly and retain the same outer and inner folds.
Ventilation/design flow lowers MAE by 0.83 kWh/m²/year (95% interval −1.19 to
−0.46) and error SD by 0.76 (−1.16 to −0.35); basement descriptors lower MAE by
0.48 (−0.82 to −0.15) and SD by 0.64 (−0.99 to −0.30). Measurement year lowers
MAE by 0.31 (−0.54 to −0.07) and SD by 0.38 (−0.63 to −0.13). Each individual
coverage-gain interval includes zero. Geometry and apartment blocks have weaker
conditional evidence when the remaining correlated descriptors are retained.
These five exploratory intervals are unadjusted for multiple comparisons and
measure predictive substitution within this sample. Measurement year can proxy
weather or certificate-cohort variation; it does not identify renovation or aging.
None of these statistical differences is a native intervention effect.

## Evidence boundaries

The archive EDA deduplicates complete certificate scope and certificate payloads,
quarantines conflicting versions, preserves missing carrier quantities, and
distinguishes old heating-plus-DHW fields from reviewed modern space-heating
fields. Longitudinal pairs need matching scope, ordered valid measurements,
matching carrier and Atemp change of at most 10%. Optional completed measures
provide supporting interval evidence. Neither missing flags, low heating,
bottom-quartile labels nor proposed package savings establish renovation truth.

The rebuilt archive has 918,179 MF rows, 144,066 distinct certificates and 12,987
conflicting certificate payloads. There are 66,732 eligible district-heating
certificates. Only 26 of 2,067 candidate first/last pairs pass all checks; 1,933
change Atemp by more than 10%. See the generated EDA report for exact definitions,
all-era distributions and rejected comparison counts. These observations do not
justify assigning a renovation state from heating intensity.

## Physical parents

Residential shading applies the Core Sveby factor of 0.5 before fitting. Geometry,
Atemp, measurement-year weather, dwelling temperatures, occupancy and gains stay
fixed after the sourced annual household reference is established. Window area,
adjacency and full-footprint basement assumptions remain
explicit uncertainties.

Opaque controls use total assembly R, including declared films. Only reviewed
low-density insulation changes thickness. SE.01 uninsulated walls have a separate
0–150 mm insulation-overlay axis using a sourced EPSM Data insulation material;
the original wall layers stay intact. This is an engineering construction
alternative, not an observed installation or a service-history default.

Sensitivity screens wall/roof R, window U, additional infiltration, mechanical
flow, FTX recovery and conditioned-basement temperature using simulated physical
contrasts without targets. V9 screens effective full-zone basement temperatures
from 11–21°C: the EPC form 2.8 definition establishes heating above 10°C, while
the actual temperature and heated extent remain uncertain. Basement temperature
is screened but is not retained as the operating fit. The selected additional
infiltration multiplier is bounded to 0.5–2.0; it scales the declared envelope
airflow assumption and does not infer a measured airtightness or n50 value.
The principal fitting pilot retains one shared
operating axis and at most two independent fabric axes per era. Construction
corrections span ventilation types within an era; the operating correction is
shared across applicable systems. The operating and fabric axes must also pass
a joint independence check; pairwise independence alone is insufficient. The same check applies to the
pooled operating/window fit, and confounded windows stay at their prior.
All applicable fabric controls enter the joint check before the three-control
cap is applied. The v7 review found that screening only windows plus the strongest
opaque direction hid an independent roof response in SE01–SE05; v7 was stopped
before fitting and v8 verified the corrected three-control eligibility. The
[fresh V9 control audit](../output/mf-parent-dynamic-expanded-2026-10-09-v9/control-block-audit/audit.md)
reproduces 510 requested sensitivity evaluations on 30 representative properties,
with no failed evaluations. It retains infiltration and overlay/roof controls
in SE.01, infiltration and window/roof controls in SE.02–SE.05, and infiltration
and windows in SE.06. Roof enters every era's joint check; SE.06 rejects it at a
condition number of 25.98 against the limit of 20. Five representatives per era
establish control eligibility, not predictive improvement. V9 repeats native
emulator validation for its new fixed household reference. SE01 uses
separate validated operating/overlay/roof and pooled operating/window blocks.
The two blocks are alternative profiles: no fourth correction is combined.
Requests outside every validated block fail explicitly. Child construction
alternatives reset unsupported inherited fabric corrections to declared priors,
while their native comparison retains the actual fitted parent.
Confounded controls remain uncertainty. Regional numerical corrections require
additional physical evidence and support.

The [target-free thermal-information audit](../output/mf-parent-dynamic-expanded-2026-10-09-v9/thermal-information-audit/audit.md)
identifies what the frozen panel does and does not observe. Every prepared
property has Atemp, apartments, footprint, derived height and storeys, but all
1,066 assume 20% window area, unverified adjacency and full-footprint basement
when present. Heated attic extent is unverified. Of 604 reported design flows,
570 are positive and 34 are zero; 462 are missing. Reported design flow is not
measured operating flow, and operational system timing is unverified. Each
reviewed component flag is available for 233 properties; none establishes a
complete dated condition inventory or measured thermal performance.

Age dependence in leakage is already present: the frozen SE.01/SE.02 existing
profiles use 0.20 L/s/m² Atemp additional infiltration and SE.03–SE.06 use 0.10.
The Data provenance labels these engineering starting values provisional and
shares them across S/F/FT/FTX. All frozen parent specs retain constant infiltration
coefficients `[1,0,0,0]`. A shared multiplier preserves the old/new ratio; it does
not infer measured n50/q50, northern tightness or coupled wind/stack/exhaust
pressure. Regional assembly performance and reduced thermal pathways require
their own supported mappings. Model-derived transmission and air-exchange
descriptors cannot independently verify those assumptions.

A parent correction can shift a cohort's central prediction. It can reduce
building-level spread when its interaction with known geometry or systems is
informative, but it cannot determine each building's unobserved component or
operational state. Final native OOF diagnostics separate cohort bias from
within-cohort spread; identical-property statistical benchmarks and conditional
ablations test which available descriptors add predictive information. More
cluster labels alone do not supply new observations. Metadata histories express
uncertain component states without identifying observed renovations; their draw
variability is not a calibrated prediction interval. Individual normalisation
adjustments, measured component U-values, actual household gains and measured
operating airflow remain unavailable in this panel.

Five outer property folds estimate development prediction. Three inner property
folds select pooled or era-specific parents. Era fits with fewer than 30 training
properties inherit pooled assumptions. Candidates maximise inclusive ±20%
coverage subject to no worse MAE; absolute bias and prior distance break ties.
Validated simulation emulators use physical U-value coordinates for
resistance controls and include midpoint validation contrasts. They shortlist
candidates; every final prediction is native. A failed held-out 2% emulator check
invokes the complete native candidate panel for that building.

## Dynamic comparison

Histories must start from separately established original construction
profiles, never fitted surviving-stock parents. The confirmed SE.02 v6 source
correction also shows why a catalogue default cannot be assumed historical from
its era label alone. Component provenance must distinguish historical assemblies,
current stock assumptions and installation-period performance proxies. One hundred identity-seeded draws model repeated component events
through measurement year. Engineering sensitivity priors use mean lifetimes of
35 years for windows, 40 for roof covering and 50 for façade refurbishment,
Weibull shape 3, lifetime multiplier 0.75–1.25 and upgrade probabilities 0–1.
The Weibull scale is calculated from the mean using the Gamma function.

Maintenance, like-performance replacement and thermal upgrades are distinct.
Covering replacement does not automatically insulate a roof. Degradation is zero
and lighting changes are inactive in the principal comparison. The first native
pilot uses explicitly labelled whole-window installation-period proxies from the
construction-era library. Roof/façade thermal upgrades stay inactive until
installation-period mappings are independently supported; their probabilities
cannot be identified in that pilot.

Window replacement changes glazing U, solar gain and visible transmittance in
this pilot; it does not update infiltration or operating ventilation. Consequently,
the comparison cannot test a coupled window-replacement/airtightness history.

Consequently, the first native history comparison tests a **window-only thermal
history model**. Roof and façade service events can be generated, but they have
no thermal upgrade effect in this comparison. Its result will not establish
whether a fully supported dynamic stock model improves prediction across all
components.

The history engine accepts reviewed period mappings, while the principal native
adapter deliberately rejects unsupported roof/façade thermal states. Each
thermally identical state is simulated once and integrated with its draw weight.
Missing positive-weight states make the ensemble prediction unavailable; weights
are never renormalised to conceal failures. The measured heating of a predicted
building cannot choose its history. Learned lifetime controls use training
properties only; frozen priors provide a separate comparator.

Sections 2.2–2.3 of
[Österbring et al.](https://research.chalmers.se/publication/512385/file/512385_Fulltext.pdf)
use component- and type-specific Weibull distributions fitted from that paper's
references 38 and 39. Their calibration selects the closest of 100 building
realisations using observed EPC energy. Here, predictions integrate the weighted
histories without the predicted building's heating observation. This evaluates
metadata-only prediction separately from energy-conditioned calibration. The
engineering mean lifetimes above are experimental sensitivity assumptions, not
defaults established by that paper.

## Acceptance and outputs

The report compares current inputs, corrected preparation, calibrated parents,
frozen histories, histories with shared training-learned controls, an empirical
parent and a nested reliable-feature statistical benchmark on identical
properties. Coverage includes native failures. Paired property bootstrap
intervals quantify changes. MAE and bias use valid predictions; complete-pair
metrics and failures are separately visible.

Parents need a positive coverage-change confidence interval with no worse MAE,
or at least 5% lower MAE with preserved coverage. Histories need at least two
coverage points, a positive paired confidence interval and no worse MAE relative
to calibrated parents. Supported era, region, ventilation, basement and height
groups must retain coverage within five points and MAE within 10%.

Physical subdivision runs after parent fitting within each supported era, using
two physical children in the first pilot. Every child needs at least 30 fitting
properties in every inner training fold, median ARI ≥0.7 across 20 resamples, at
least 5% inner validation MAE improvement and preserved coverage against the
frozen parent. The global operating control remains fixed; children fit only
eligible era construction controls shared across ventilation systems. Emulators
shortlist candidates and native simulations confirm them. The child decision is
frozen before outer validation predictions. Unsupported, unstable or ineffective
children retain the parent, with reasons in `subdivision-oof.json`.

Primary outputs are `report.md`, `report.json`, `preparation-outcomes.json`,
`original-construction-references.json`, `calibrated-parent-profiles.json`,
`assignments.json`, per-building `histories/`, `dynamic-panel.json`, and nested
fitting audits under `fits/`. Profiles remain candidates unless adoption gates
pass. `assignments.json` selects exact current assumptions when the parent gate
fails, a calibrated parent when its gate passes, and the original-reference
component-history distribution only when both parent and dynamic gates pass.
`candidate-calibrated-parent-assignments.json` and
`candidate-dynamic-assignments.json` retain the alternatives independently of
adoption. Selected exports include adoption reasons, physical reference pointers,
applicability and confidence. Current-reference exports are checked against
accepted current native receipt hashes wherever available. Dynamic assignment
can generate 100 weighted identity-seeded states for a future building from
construction and measurement metadata and exported priors; it requires neither
that building's heating target nor an existing history file. Optional renovation
fields are not required by forward assignment.

The initial physical target is delivered district space heat. Other carriers have
separate unvalidated status; heat-pump electricity is not equated with thermal
demand. The output directory contains the sealed config, material provenance,
parameter register, current/parent specs, separate original historical specs and
native receipts needed to reproduce it.
