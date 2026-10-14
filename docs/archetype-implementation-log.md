# Archetype application research and implementation log

Started 3 October 2026. Objective: make the EPSM Data archetype matrix easier to
apply through EPSM Core, diagnose building-level heating errors, and validate
shared improvements against independent properties. Annual delivered space heat
is the first target; thermal demand, total building energy and EPC primary-energy
ratings are separate quantities.

## Repository inspection

- All three repositories contain existing uncommitted work. No reset, cleanup or
  catalogue replacement is authorized by this implementation. Changes are first
  developed in `/private/tmp/epsm-archetype-implementation-20261003`, with original
  file hashes retained to detect concurrent edits before applying them.
- EPSM Data v1.3 has 90 canonical construction states and 1,080 classification
  slots. Climate/ventilation classifications are not 1,080 independent fabrics.
  Existing source evidence distinguishes nominal layers from effective ground U.
- Core supports generic gains, F/FT/FTX recovery, equivalent thermal bridges,
  Kiva and common-area controls. Confirmed gaps: repeating daily schedules,
  continuously enabled mechanical outdoor air, general zone assignments and
  published office/school operations.
- MF SE.02 existing still exports whole-window U=2.8, SHGC=0.7, VT=0.75.
  The inspected eight-building U=2.2 experiment is development evidence. It does
  not justify changing the frozen catalogue or identifying individual windows.
- Existing joint sensitivity identifies ventilation and basement temperature as
  influential over its bounds; this is not proof that those parameters are wrong.
  The 125 m2 Atemp / 237 m2 conditioned-area case requires building evidence.

## Source review

The user confirmed **Sveby**, rather than Sweco, as the operational source.

- Office: Sveby *Brukarindata kontor* 1.0, 2012-10-10,
  https://www.sveby.org/wp-content/uploads/2012/10/Sveby_Brukarindata_kontor_version_1.0.pdf
  SHA256 `471f143f7e33f9ba8a4d934906c0a8c656a4bffe744097e79a09fcb5e499793d`.
- School: Sveby *Brukarindata undervisningsbyggnader* 1.0, 2016-05-16,
  https://www.sveby.org/wp-content/uploads/2016/05/Sveby-Brukarindata-undervisning-1.0-160525.pdf
  SHA256 `6d2b8648613c4d06cf7a7ef63437263999d9f3ee8d40e2ae581172d9c889677f`.
- The official site advertises office 2.0 as a consultation document. Use the
  published 1.0 inputs with explicit versioning; do not silently promote a draft.
- PDFs were downloaded, extracted with Poppler and rendered for table review.
  Office's extracted text has a broken font encoding: values must be read visually.
- Office Table 2.2: 21/23 C, outdoor air 1.5 L/s/m2 Atemp, DHW 2 kWh/m2/year,
  business electricity 50 kWh/m2/year with 70% available as heat, occupancy
  20 m2/person, nine hours/day, 108 W/person.
- School Table 2.2 distinguishes classrooms, kitchen/dining, sport/showers and
  service/storage. Classroom and associated areas: 22 C continuously, 3 L/s/m2,
  ventilation/lighting/equipment 10 hours/day, 5 days/week, 44 weeks/year;
  density 0.067 person/m2 for 6 hours/day, DHW 10 kWh/m2/year,
  lights/equipment 5 W/m2 each. Actual closure dates are not prescribed by this
  table. A project calendar must remain explicit.

## Scientific constraints

- Preserve source material properties and catalogue files while developing
  application support. Candidate corrections remain separate from accepted data.
- Select component states from independent evidence, never minimum energy error.
- Do not infer infiltration from total air exchange or n50 without a declared model.
- Keep gain reference area separate from geometry. Atemp normalization must not
  resize footprints or heated spaces.
- Use normal-use operations only for compatible normalized targets; raw measured
  consumption requires actual-use inputs and a matching weather/energy boundary.
- Retain geometry/numerical failures and report them against attempted buildings.
- No new building-accuracy claim until frozen choices are evaluated on previously
  uninspected, property-separated buildings.

## Experiments and implementation

Entries below are appended as work progresses, including unsuccessful experiments.

### Source reconciliation and implementation choices

- Visual review of office printed pp.12/18/25 established the revised 1.3 L/s/m²
  flow, 07–19 ventilation, 08–17 occupancy, 70% presence and 225 active days.
  Table 2.2's retained 1.5 is recorded as a source discrepancy, not silently
  averaged. Office electricity is normalized to 50 kWh/m²/year; 70% zone heat
  gives 35. The .2 standby fraction is an explicit completion scenario.
- School printed pp.9/22 established room-function ventilation/loads and
  80 W/person. Constant heating persists over holidays. Caller dates are required;
  07–17 operation, 09–15 occupancy and base/boost clock order are completion
  assumptions. School airing 4 kWh/m² remains a separate postprocessing input;
  it is not represented by increased infiltration.
- Residential monthly source totals are rounded. Their timing is normalized
  separately to the declared annual 14.7 kWh/m² available household heat and
  21 kWh/m² DHW, with actual Gregorian month lengths. Old daily APIs remain usable.
- EPSM Data now resolves one canonical category/era parent and independently
  selected component states. Selection requires nonempty independent sources;
  windows do not automatically change walls or airtightness. All four requested
  categories and six eras are covered by resolver tests.
- The read-only matrix audit exports all 90 canonical states and 1,080 slots,
  retaining source evidence, caveats and nominal/effective distinctions. Its first
  attempt exposed `INT_WALL_NONLOADBEARING`; the role mapping was corrected.
  Every row remains marked pending annual building validation. No numerical
  catalogue, baseline artifact or source construction was recalibrated.
- Core now resolves daily/weekly/dated/annual calendars, rejects mismatched years,
  separates scheduled mechanical outdoor air from heating/cooling availability,
  assigns zone operations, and supports native unconditioned exchange. Physical
  geometry remains separate from Atemp-based people/gains/DHW/mechanical flow.
  Zone allocations must conserve the declared building load area. Mixed thermal
  zones are not labelled as whichever physical cell happened to be visited last.
- Core carrier accounting distinguishes ideal space heat, net distribution loss,
  purchased carrier input and optional scheduled fan electricity. Efficiency/COP,
  losses and SFP require building evidence. Positive effective COP curves may
  represent degraded operation; no COP is inferred from the heating residual.
  Fan heat and recoverable distribution losses require coupled models and are not
  silently inserted. FVP retains its existing uniform-exhaust restrictions.
- The legacy Data attic adapter remains available and now rejects double-counting
  an exchange already emitted natively. Script comments/docstrings and versioned
  resources carry source references and explain the engineering assumptions.
- Workflows prepares complete physical scenarios, freezes source/runtime/weather
  hashes, preserves property splits and previously-inspected training status, and
  selects one shared scenario per family using training energy alone. Failures do
  not improve a candidate by dropping difficult buildings. Test receipts are
  frozen before opening held-out energy. Evaluation reports strict per-building
  <20%/<10% coverage with paired errors and property bootstrap uncertainty.

### Verification and unsuccessful attempts

- Initial calendar/compiler/residential tests: 26 passed. Initial component
  resolver tests: 26 passed. The first combined Core/Data invocation could not
  import Data example fixtures; tests were separated and fixture assets copied
  into staging. A requested `test_delivery.py` did not exist; independent carrier
  conservation tests were added under the actual test layout.
- Existing Core annual regressions: 10 passed, including nine geometry variants
  and leap-year weather. New office/school annual runs both passed with strict
  diagnostics, DHW conservation on a 900 m² load reference area distinct from
  1,800 m² physical conditioned area, and explicit thermal-plus-loss accounting.
  These synthetic runs are integration verification, not accuracy measurements.
- Expanded calendar/operation/delivery plus annual regression checks: 26 passed
  before the final custom-profile test was added. Source energy integrals,
  overrides, zone matching, basement/attic exchange and area allocation are tested.
- Core's broad non-EnergyPlus regression suite: 593 passed, 9 skipped,
  13 deselected and 5 failures. All five also fail in the original checkout:
  logical-window/shading zoning consistency, viewer opening geometry, and shared
  vertex object identity at 0/37/90 degrees. These existing failures remain visible;
  their code was not overwritten or their assertions relaxed.
- The initial full Data invocation yielded 230 passes, five failures and five
  fixture errors: missing staged tools, subprocess PYTHONPATH, eager-import runpy
  warnings and sandbox-blocked local HTTP fixtures. The application export was
  made lazy, fixture/tools paths corrected, and the local-only server suite run
  with the needed sandbox permission. Full staged Data suite then passed all
  240 tests. The added native/legacy double-counting test is checked separately.
- Workflows new guardrail tests: 8 passed. Combined new workflow, existing diagnosis,
  pilot, weather and validation tests: 42 passed (existing Shapely warnings retained).
  Tests cover poisoned held-out values, shared-family selection, failed panels,
  attempted-building denominators, target-area consistency, property leakage,
  source mutation and replacement of already-opened test receipts.
- Ruff checks pass on changed scripts and tests. Formatting is limited to new
  modules/tests; existing scripts retain their documentation and surrounding work.

### Rechecking the previously explored real buildings

Results are kept in `output/archetype-implementation-2026-10-03/` separately from
the original studies. No old experiment or target was replaced.

- Eight stored existing/frozen specs were available: all eight compiled under the
  new Core implementation. This check excludes the four cases without stored
  specs because compilation had previously failed; it is not an acceptance rate.
- Reconstructing all 12 rows directly from Parquet yielded seven compiles and five
  failures. Four are the known short-edge geometry failures. One is a NaN flow:
  Parquet converted an unknown value to NaN, while the old builder expects None.
- A separately recorded normalization restored that NaN to unknown and retained
  the original diagnostic's explicitly assumed 0.35 L/s/m² fallback. Then eight
  compiled and four retained their strict short-edge failures. Raw and normalized
  attempts are both saved; the restored unknown was not called a measurement.
- Existing Core footprint cleanup did not solve those four geometries. Larger
  simplification, warning waivers and replacement buildings were not introduced.
  These cases need a subsequent evidence-preserving geometry investigation.

### Scientific disposition

The implementation removes operational/application obstacles and makes hypotheses
testable. It does not establish <20% or <10% error, or justify changing released
window/material values. A confirmation study still needs independently reviewed
building inputs, matched energy/weather/normal-use boundaries, representative
properties, frozen candidates, the original minimum 120-building gate and
predeclared family/split coverage. Validation and test observations already seen
in prior studies cannot become untouched observations through reshuffling.

Known limits: exact school room calendars/programmes, operating flows, heated
extent, leakage, recovery and delivery evidence remain building inputs. Kiva and
unconditioned exchange are scenario models, not measured soil/attic/basement
conditions. Full HVAC/fan-heat coupling, dynamic blinds, recoverable loss coupling,
preschool/university profiles and the four failing geometries remain unresolved.

### Applied repository verification

- Thirty reviewed files were applied to Core/Data after checking original and
  staged hashes. A subsequent check verified every applied file. Existing edits
  were preserved; no commit, tag or publication was made.
- Applied Core checks: 21 passed, including annual office/school simulation.
  Applied Data checks: 44 passed across component application, v1.3 and library
  tests. One subprocess import initially lacked the source PYTHONPATH; the corrected
  invocation passed. Sandbox cache-write warnings did not affect test assertions.
- Applied Workflows checks: 9 passed, including a real eight-run annual EnergyPlus
  prepare/development-simulate/shared-select/validation/test-simulate/test-evaluate
  sequence. It used a constructed building and invented positive targets purely
  to verify execution and baseline ties. Its errors are not stock accuracy evidence.
  Exact input snapshots, manifests, receipts and evaluations are retained under
  `output/archetype-implementation-2026-10-03/synthetic-protocol/`.
- Runtime freezing includes Python and relevant dependency versions as well as
  source/resource hashes. Failed evaluation families retain attempted counts even
  when they have no accepted pair. Per-building outputs include target kWh and Atemp.
- Packaged EPSM Data dataset/source files were compared to their initial hashes
  and remain unchanged. Applied-file manifests, review diff, source PDF/table
  snapshots and test logs are retained alongside `implementation-summary.json`.

### Existing literature-extraction evidence located after user clarification

- The user identified a prior evidence collection stored in an extraction JSON.
  A default tracked-file search found only the intermediate-floor review; an
  explicit ignored/archive search located the full input. The authoritative
  reviewed snapshot is `../EPSM_data/ARCHIVE/freeze-2026-09-30/extraction results.json`:
  valid JSON, 296 records, SHA256
  `4e118c3667b25ad342fd4a2f39738a299ec046649e1589eb0ea5706ad8e9b6e1`,
  exactly matching the LoD1 review manifest and intermediate-floor review.
- `ARCHIVE/extraction results copy 2.json` is valid but contains 138 records and
  differs from the full snapshot. `ARCHIVE/extraction results copy.json` is not
  valid JSON: it contains comma-separated objects without an enclosing array and
  Markdown artefacts. Neither copy replaces the hash-pinned full input.
- Full-snapshot case classifications: 168 MFH, 50 SFH, 35 Office, 19 School and
  24 Other records. These are extraction records, not independent measured
  buildings. Root fields contain metadata/case information, LoD1 U-values,
  LoD2 layer composition and renovation measures; no annual measured-energy
  target field is present in the extraction schema.
- The archived LoD1 evidence exercise already generated 630 comparisons, reviewed
  dependence groups, case/state classifications, contextual exclusions and
  renovation endpoints. Its initial current-stock interpretation was superseded
  by applied original-construction calibration. Current v1.3 documentation
  describes the resulting construction library as evidence-qualified, with annual
  building performance still unvalidated. Do not apply old proposals a second time.
- Revised next step: reconcile the full extraction, normalized review records,
  applied calibration register and current matrix by case family, component,
  era, boundary and construction/renovation state before choosing new candidates.
  Literature can bound representative construction alternatives and strengthen
  the existing-data-only investigation. It does not resolve which alternative,
  historical operation or heated extent applies to each current EPC building.

### Multifamily improvement implementation (2026-10-03)

- Reproduced the four failures from the original twelve source geometries. They
  arose in attic gable walls with generated eave heights 0.003–0.197 mm, rather
  than short source footprint edges. Core now resolves analytic gable/mono-pitch
  heights below its existing 0.01001 m generated-edge resolution to the eave base
  consistently across roof triangles and walls. Source x/y, projected coverage,
  roof choice and strict surface acceptance are preserved. The bounded modelling
  approximation is declared in graph reports. All twelve compile; the first
  previously failing building also passed an annual strict EnergyPlus run.
- Cyclic ring cleaning now retains pooled tuple identity. This resolves the three
  previously failing shared-vertex generation tests. Staged full Core verification:
  611 passed, 9 skipped, two pre-existing failures remain (logical window identity
  across zoning; viewer opening/allowlist). These are unchanged from the original
  comparison, and are not waived by this study.
- Data `extraction_audit.py` reconciles the authoritative hash-pinned 296-record
  extraction and 630 archived comparisons with all 630 current v1.3 calibration
  entries. It preserves historical proposals, contextual evidence, case dependence,
  state/era uncertainty and physical boundaries. No released numeric values changed.
- Workflows selection accepts `coverage20_then_mae`; default MAE remains compatible.
  It prioritizes the fraction of training buildings strictly below 20% absolute
  delivered-heat error, then MAE and baseline ties. An incomplete paired training
  panel retains baseline. A regression test verifies that higher coverage can beat
  a lower-MAE candidate; held-out targets remain inaccessible during selection.
- `multifamily_improvement.py` uses native monthly Sveby calendars, separate Atemp
  load reference area and delivered-space-heat accounting. Legacy area-scaled
  densities/flows are removed before applying Atemp once. Apartment counts are
  available but room mix is not: occupancy remains a labelled fallback, rather
  than inventing a Sveby category from average floor area. DHW remains separate.
- The family panel contains baseline, window-only 2.2 W/(m²K), and 40 seed-pinned
  joint wall/roof thickness, window and ventilation hypotheses. Material IDs and
  conditioned infiltration are fixed. Native soil, attic exchange and net delivery
  loss scenarios are sensitivity-only and cannot win family selection. No epJSON
  postprocessing patches or per-building best-fit choices are used.
- All twelve original properties have `split=train` and have already been examined.
  The run therefore remains retrospective and in-sample. No contrast properties
  exist here; their reported count is zero. The original 120-building and 30/10/20
  independent-property confirmation gates remain unmet. An additional read-only
  inventory of the 2,664 national simulation candidates retains the original
  configuration, opened-outcome receipt and missing weather/evidence blockers.
  Broad family counts alone do not declare a new prospective calibration cohort.
- Applied verification: 30 passed, 1 skipped across shared selection, geometry
  contracts and extraction guards. Applied files were hash-guarded; original user
  changes were preserved. Source/test manifests and staged logs are retained in
  `output/multifamily-improvement-2026-10-03/implementation/`.
- Simulation receipts, full native specs, source/runtime/weather hashes, evidence
  unknowns, fixed candidate definitions, paired outcomes and subgroup errors are
  saved under `output/multifamily-improvement-2026-10-03/experiment/`.

### Completed multifamily experiment and interpretation

- All 566 planned annual cases passed strict Core/EnergyPlus acceptance: 504
  baseline/window/joint cases and 62 applicable native sensitivity cases. Every
  original building remained in the denominator. No warnings were waived.
- Baseline: 8/12 below 20%, 4/12 below 10%, MAE 18.138 kWh/(m² year), signed
  bias +16.835. Shared `joint_30`: 10/12 below 20%, 5/12 below 10%, MAE 14.529,
  bias +7.627. It improved nine buildings and worsened three; paired MAE change
  -3.609 kWh/m², conditional property-bootstrap 95% interval [-6.287, -0.724].
- Shared hypothesis: wall insulation thickness ×1.112864, roof thickness ×0.920598,
  window U 2.2 W/(m²K), operating-flow sensitivity 0.35 L/(s m² Atemp). These are
  in-sample exploratory settings, not newly accepted construction definitions or
  measured operating conditions. Infiltration and material identities stayed fixed.
- Selected <20% coverage is 83.3%; conditional bootstrap interval [58.3%, 100%].
  The interval excludes candidate-selection uncertainty and does not establish
  population representativeness. The requested 90% coverage remains unachieved.
  There are no fresh validation/test observations in this 12-property experiment.
- The remaining large errors are overpredictions: `3261c00e…` predicts 110.937
  versus 62.537 kWh/m² (+77.4%); `8369bd2a…` predicts 104.161 versus 62.434
  (+66.8%). Both are reported detached buildings; generic party-wall removal is
  therefore unsupported by the available descriptor. No dated fabric-retrofit
  record resolves their present construction state. Heating control/water-saving
  measures are reported for one, but do not establish fabric U-values or leakage.
  More blanket changes to this family would risk the three already worsened cases.
- Flat-roof subgroup: 5/5 below 20%; pitched-roof subgroup: 5/7. This small split
  motivates review of geometry/state/system evidence, not a new causal roof rule.
  Existing ridge/eave descriptors are retained beside residuals; pitch remains the
  explicitly assumed 30 degrees. Net-loss +5/+10 cases worsened aggregate baseline
  coverage, so adding losses to fix the family overprediction is unsupported.
- Geometry correction audit against the hash-verified original source: maximum
  actual vertex-height change 0.0001972 m; maximum roof area change 0.000349%,
  maximum attic volume change 0.000931%; all source x/y retained. This resolves
  numerical eligibility without materially changing the roof's thermal geometry.
- Exact executed source/resource snapshots (102 files, plus the diagnostic script
  and imported helpers) are retained in `implementation/frozen-*-src/`. The helper
  files were archived after execution; per-case serialized BuildingSpecs/hashes
  remain the authoritative executed inputs. Future preparations now hash these
  three workflow helpers explicitly as well. No previous result was relabelled.
- After completion, a tested formatting-only cleanup changed the Data audit reader
  from lambda to def and formatted two tests. The executed audit source is preserved
  in the frozen snapshot; all current packaged numeric JSON hashes still match the
  experiment. The final manifest records this post-execution source cleanup.
- `interpretation.json` adds conditional bootstrap intervals and limitations;
  `building-evidence-errors.csv` joins selected errors to source lineage and available
  geometric descriptors. `paired.csv`, all candidate training scores and all receipts
  remain available for review. No numerical catalogue update, commit or publication
  was made.


### User-authorized Core commit and EPSM Data v1.4 adoption (2026-10-03)

- The subsequent user instruction explicitly authorized committing reliable Core
  improvements and adopting the better-performing window value in EPSM Data.
  Core commit `fddbcc17dd7a5bdc688efa375154d6ef72b6bba7` includes source-pinned
  Sveby residential/office/school use profiles, calendar and zone schedules,
  Atemp load references, unconditioned exchange, delivered heating/monthly
  results, Kiva foundations and bounded geometry preparation. Its working tree
  is clean. Full verification: 615 passed, 9 skipped; no push was requested.
- Full-suite investigation found that default GEOS precision rounding could
  reorder a perimeter-core polygon ring and change logical facade identities.
  Pointwise precision rounding now preserves the source winding/start while
  retaining the existing precision grid. The zoning-independent window/shading
  regression passes. A viewer failure arose from sandbox socket restrictions;
  the existing test passed with authorized localhost access.
- Database release is now `epsm-lod2-v1.4` / `1.4.0`; package version is
  `0.3.0.dev1`. Only multifamily SE.02 existing-state window U changes from
  2.8 to 2.2 W/(m²K). An explicit override and isolated material/Honeybee/assembly
  clone preserve the generic and historical window definition at 2.8. All other
  resolved construction and infiltration values are equal to the prior catalogue;
  SHGC remains 0.7, visible transmittance 0.75, and carbon references are retained.
- The window-only diagnostic improves below-20% coverage from 8/12 to 9/12 and
  MAE from 18.138 to 15.011 kWh/(m² year). The joint candidate's 10/12 result
  does not describe this release. The new default is recorded as a provisional
  engineering assumption on previously examined properties with unknown retrofit
  state; independent building-level validation remains pending.
- Evidence source locators, claim basis signatures, calibration content hashes,
  seven-role release compatibility, and the packaged dataset checksum manifest
  were updated together. The diagnostic proof ships in the Data wheel. Applied
  checks verify all 23 reviewed release files against staging hashes and exercise
  export to Core. Comments/docstrings and repository release documentation cite
  the diagnostic and its limitations directly.
- Data changes remain uncommitted, preserving the user's other ongoing Data
  work. The pre-existing explorer/application/audit changes were not reset.
  Release source snapshots, update scripts and test receipts are retained under
  `output/multifamily-improvement-2026-10-03/release-v1.4/`.
- Final applied Data verification: all 244 tests passed in 326.85 seconds;
  Core's full suite passed 615 tests with 9 skips. The Data wheel contains the
  v1.4 catalogue and diagnostic evidence. The archived receipt pins the source
  snapshots, wheel, test logs and commit message by SHA-256.

## 2026-10-04 — Fixed-data cohort transfer implementation

Implemented a separate reproducible cohort-transfer protocol, preserving historical explorations and fixed observations. Property-level nested validation compares baseline, one-representative and shared physical calibration. Target-free medoid assignment, support fallbacks, missingness/stability checks, sensitivity screening, response-confounding reports, bounded controls and actual Core simulation receipts are retained. Failed buildings remain in accuracy denominators. Data profiles are separate effective surviving-stock assumptions and do not rewrite the reference matrix. Synthetic tests exercise parameter recovery and prevent property leakage.

Core gained independent infiltration/natural exchange fraction schedules and explicitly sourced weather coefficients; FVP rejects weather-dependent leakage. smhi2epw gained actual-consumed-response provenance, source and EPW hashes. Regenerated all eight Gothenburg AMY years (2017–2024) from the existing raw caches; no new building data. All weather requests succeeded. Office/school weekday scenarios conserve the Sveby reference activity-day counts, with actual dates labelled unknown. Existing local GIS can provide measured shading without automatically declaring party walls.

The first 12-building preparation exposed a missing per-part roof fallback in applicability screening. Preserved that failed v1 audit and corrected applicability to use the building roof. No successful predictions or accuracy claim were obtained from that failed preparation. A fresh campaign is required after source changes. Numerical transfer results and test counts are appended after execution below. Protocol and limitations: `docs/cohort-transfer.md`.

### Broad baseline and runtime findings

All 126 SE.02 buildings prepared (82 independent properties). The unfitted v1.4 native baseline with regenerated AMY weather produced 124 accepted predictions and two numerical failures. Coverage was 50/126 strictly below20% (39.68%) and 21/126 below10% (16.67%); accepted-case MAE29.830 kWh/m²/year, bias+26.381. Property bootstrap below20% interval31.01–49.54%. This broader existing cohort contradicts extrapolation from the earlier 12-building development subset. Results are in `output/cohort-transfer-mf-se02-full-2026-10-04-v1/baseline-evaluation.json`; failed cases remain in coverage denominators. The reporting script initially used the wrong bootstrap keyword; fixed its reporting invocation and regenerated the report without rerunning or changing predictions.

Threaded sensitivity batches suffered severe SQLite/GIL contention during Core result extraction. Profiled only owned processes and preserved that evidence. Stopped the slow pilot, introduced spawned process batches with submission deduplication and verified native-case import, and froze new campaigns (pilot v3; full v2). Imported135 accepted pilot and124 accepted full baseline cases after exact physical spec, weather and engine/Core-source checks. Old receipts/specs/native completion artifacts are hash-pinned; changed physical models cannot reuse them. Process/failure/cache tests pass. No Core physical algorithm or diagnostic policy was relaxed to improve runtime.

Core618passed/9skipped; Data244passed in its own environment; weather88passed/4skipped/4network-deselected offline;32 transfer tests including24 use/era baseline contracts. Four Data failures in the shared Core environment were subprocess import-path failures; they disappeared in the Data repository's configured environment. Final weather provenance/documentation checks17passed. Existing GIS context was read for12 buildings from11 source partitions; the checked target compiled14 native shading neighbours, no unsupported outlines or verified party walls. Core changes committed as `e06d956` under the standing request to commit reliable Core improvements.

### Intermediate diagnostic findings (not final transfer claims)

The first pilot inner sensitivity screen ranked infiltration multiplier, setpoint offset and window-U multiplier as active. Solar-shading/SHGC alternatives and some envelope controls had strongly correlated cross-building responses and were reported as confounded, not identified causes. Outer-fold0 selection used inner validation only: shared fitting won with an effective setpoint offset−1.333°C; the final representative fit retained baseline settings. Across its nine inner-validation buildings, baseline and shared both had8/9 below20%; shared reduced bias8.094→2.022 and MAE12.320→12.018 kWh/m²/year. These intermediate observations do not establish outer transfer improvement. The fitted setpoint is an effective shared assumption and may also absorb unobserved gains/envelope differences; it is not a measured indoor temperature.

Combined Workflows transfer/stock/context regression48passed. Archived all178 pinned source/resource identities in `output/cohort-transfer-source-evidence-2026-10-04/pinned-sources.zip`. Baseline figure `output/cohort-transfer-mf-se02-full-2026-10-04-v1/artifacts/baseline-errors.png` rendered and visually checked; its empirical coverage includes both failed buildings in the denominator. The figure script remains reproducible in `examples/epc_validation/plot_cohort_transfer.py`.

### Basement semantics, conditional controls and revised transfer protocol

Baseline subgroup checks found 34/70 buildings without heated basements below20%, compared with16/56 with heated basements. Bias was+19.136 versus+35.773 kWh/m²/year. This is an association, not proof of basement causality. The source definition `heated_above_10C` does not justify a 21°C setpoint or establish the conditioned floor extent. The geometry/Atemp discrepancy is retained as a descriptor and semantic caveat, not corrected by footprint rescaling. Native zone-use scenarios now allow an unlocked basement setpoint11–21°C while preserving above-ground use. Independently specified zone programmes and FVP prevent this inferred scenario.

The three largest baseline overpredictions belong to one property (`1480|KOBBEGÅRDEN6:739`) with residential EPC classification and GIS purpose `Verksamhet;`. Programme disagreement is now an observable descriptor, with no automatic use reclassification. The building outlines remain external proxies with unverified internal area; large gross/Atemp ratios and heated-extent uncertainty remain visible.

The completed pilot v4 (12 previously inspected properties) selected10/12 below20%, versus9/12 baseline; selected MAE14.087 versus15.011 kWh/m²/year, bias8.301 versus11.841, and5/12 below10% for both. Shared fitting alone achieved MAE13.145 and10/12 below20%; single-representative fitting achieved9/12 and MAE16.175. Two selected predictions improved and two worsened. This small retrospective result is provisional and does not establish the full-cohort target. Its original evaluation remains preserved.

Further inspection found that a mixed-cohort applicability intersection excluded basement and attic controls whenever any member lacked the physical role. The revised register takes applicable unlocked members, freezes their common bounds and retains all row baselines. Physical controls have zero effect where the role is absent, and cache keys use the actual effective parameters. This also protects independently evidenced inputs. Added response-rank tests demonstrate why centred Pearson correlation can incorrectly discard a conditional effect: dependence is now checked using the uncentred response matrix about the physical baseline (relative smallest singular value must exceed0.05). This tests local distinguishability, not causal identification.

Final export now repeats the same inner selection procedure validated by the outer experiment, rather than taking a majority vote over outer choices. Native-cache imports normalize effective parameter metadata after exact physical identity checks. All36 transfer tests passed, including role-conditional native compilation, immutable Data controls, response rank, property exclusion, final selection and process/cache recovery. Ruff passed. Fresh source-frozen campaigns are pilot v5 and full v4; old partial and completed campaigns remain distinct. Exported assumptions are limited to the recorded v1.4/Core sources and evaluated domain.

Final-source transfer regression36passed (266.48s while native campaigns were running). Source archive `cohort-transfer-source-evidence-2026-10-04/final-protocol` retains172 physical source/resource files plus runtime identities, test logs and reporting helper/configuration snapshots. Pilot v5 imported1144 and full v4 imported2303 accepted, physically verified native cases. Both revised native campaigns are running, with completion reporters that wait for the atomic evaluation, verify all attempted building IDs, execute the final refit, produce a support inventory/figure and append completed numerical outcomes below. Until `completion.json` records completion and `evaluation.json` exists, their full accuracy is pending.

The first full-v4 inner screen (outer0, k1, inner0) evaluated the conditional basement control for33 applicable and44 locked/inapplicable training buildings. It completed without failed simulations. Basement normalized Morris mean magnitude5.536 kWh/m²/year, with spread8.430, was based on20 responses; this includes zero effect on absent roles. Its response was poorly separable from infiltration and general setpoint (relative smallest singular value0.02654). The screen retained infiltration, setpoint and thermal-bridge uplift. Confounded basement response is unresolved, not evidence that basement conditioning is unimportant. No heating target entered this sensitivity screen, and no causal conclusion or full-cohort accuracy claim follows from it.

## 2026-10-04 — Archetype membership audit and apples-to-apples correction

The user's detached-building concern triggered a source-only audit of all262,280 eligible national records and original raw EPC rows for all126 SE.02 records. The existing national filters retained EPC categories but omitted cadastral purpose and accepted-link semantic compatibility; the focused cohort omitted residential shares, dwelling counts and attachment placement. Programme conflicts were merely descriptors and still entered calibration. This was insufficient to establish a pure MF comparison.

The old126 cohort has93 records reporting100% residential,28 mixed-use records and five with absent residential share/zero dwellings. Three of those five are GIS `Verksamhet;`, two `Komplementbyggnad;`. With all five GIS purpose fields, heated garage and other source checks,85 buildings/54 properties satisfy the conservative pure-programme policy and41 are quarantined. National MF labels include1124 primary GIS `Bostad;Småhus friliggande` records;631 of these report at most two dwellings. National MF records reporting at most two dwellings total4677. Counts overlap and are source conflicts, not proven replacement classifications. Raw/generic category disagreement is not resolved by heating error or building size.

Added `archetype_membership.py`, with direct Lantmäteriet/Boverket references and policy provenance. It checks requested use against EPC category, primary/other heated-use shares, reported dwellings, all GIS purposes, link semantics/one-footprint boundary, construction-era agreement and assigned ventilation. Omitted attributes are recovered from fixed raw source locators after formular-ID and row-SHA validation. Detached apartment blocks remain MF. Ambiguous `Småhus med flera lägenheter` and mixed programmes require distinct reviewed cohorts; they remain in inventory. Missing compatible evidence retains caveats. The pure100% use requirement is a study policy, not the legal definition of MF.

Applied the same gate before cohort target collection/spec compilation and to national candidate/calibration entry points. Native spec manifests cannot bypass inconsistent membership. National preparation now preserves raw category, GIS purpose and link compatibility and emits eligibility/status audits; metadata refresh recalculates them. Historical source-frozen studies are preserved and require fresh preparation. The national audit opens no heating target files and records source/reporting hashes. Reproduction and limitations: `docs/archetype-membership.md`; `examples/epc_validation/audit_archetype_membership.py`.

All12 development-pilot buildings pass membership checks. This preserves the provisional v1.4 window diagnostic's comparison cohort. Fresh preparations are pilot-v6-membership-reviewed (12/12) and full-v5-membership-reviewed (85/126 eligible, all85 prepared). Imported84 native accepted baseline cases after exact physical identity checks; one eligible numerical failure remains. The unchanged reviewed baseline gives37/85 below20% (43.529%),14/85 below10% (16.471%), MAE25.786, bias+24.246 kWh/m²/year. Bootstrap below20% interval33.655–58.464%. This is a different population, not calibration improvement. Older50/126 results describe mixed-label diagnostics and cannot serve as pure-MF accuracy evidence. No calibrated reviewed full-cohort result is claimed.

Final verification66passed:19 membership/provenance/entry-point regressions, national stock regressions and36 transfer tests. One initially failing new test lacked eave/ridge fixture keys required by the existing geometry screen; corrected the fixture and reran all66 successfully. A stdin-only baseline reporting command could not launch spawn workers; replaced it with a saved reproducible guarded helper (`run_cohort_baseline.py`) and ran with one worker, retaining all native failures. No Core/Data physical values changed during this audit.

The former pilot-v5/full-v4 processes were no longer active at inspection; their saved waiting statuses were stale. Preserved their status snapshots and marked them interrupted for membership review. Those partial campaigns and historical simulations remain archived. No running national calibration is implied by this audit; fresh source-frozen reviewed cohorts are prepared and the baseline is completed. Full reviewed calibration remains pending.

## 2026-10-04: GIS-led assignment and native mixed-programme blending

The user asked GIS to lead classification and requested Honeybee-like programme
blending in Core. Inspected GIS purpose taxonomy, native UseSpec/AirflowSpec,
calendar expansion, zone assignments, load emission and result extraction. Reviewed
Honeybee's primary ProgramType.average documentation (conceptual precedent):
https://www.ladybug.tools/honeybee-energy/docs/honeybee_energy.programtype.html

Assignment v2 retains the EPC reported use but separately resolves all five GIS
purposes into gis_use_group/assignment status. National MF candidates use this GIS
family and still pass the independent EPC/link consistency gate. Broad/missing
residential GIS labels are unresolved. Broad business GIS needs EPC use percentages
to distinguish office programmes. No GIS purpose position creates an area fraction.
The source-only national rerun (262,280 records) is saved separately in
output/archetype-membership-gis-led-2026-10-04-v2. Eligible SF/MF/office/school counts
are 131,976 / 37,522 / 2,281 / 7,501 respectively. Original MF SE.02 membership stays
85/126. Earlier baseline accuracy has not been rerun or transferred to a new source
identity. Existing historical campaign files are unchanged.

Core ProgramComponent/ProgramBlend accepts explicit sourced heated-area fractions.
Design-rate-weighted schedules preserve hourly people, equipment electricity,
lighting and flow. Activity and equipment heat fraction need hourly effective
ratios because residential and office inputs differ: added optional native schedules
and compiler support. Three additive equipment objects preserve electricity and
heat; radiation retains the historical 30% input share where physical and is capped
when heat is below 30%. DHW normalises each programme's schedule independently
before blending annual demand, preserving both annual demand and hourly profile.
Dated calendars/closures/leap years remain explicit. Different topologies, recovery
efficiencies, availability or leakage coefficients require separate zone programmes.
Different setpoints require an explicit effective-control assumption. ACH blending
requires a shared height basis; separate zones remain the physical model for known
spatial uses. The programme mixture cannot guarantee thermal-demand transfer or
building accuracy by itself.

The Workflows program_blend_from_epc adapter consumes only existing EPC percentages;
it rejects incomplete totals, unsupported positive uses, invalid shares, heated
garages and incompatible/multi-footprint links. It neither collects new building
data nor automatically readmits mixed records into pure-archetype calibration.
Keep the original serialised ProgramBlend for per-component source provenance.

Validation: staged 76 Workflows tests passed; installed native-library membership/
stock/adapter subset 40 passed. Seven new Core checks pass including hourly normal/
leap-year conservation, closure, zero-load and nested blends, conflict rejection,
epJSON schema compilation and an annual EnergyPlus run checking DHW, equipment
electricity and zero severe errors. Test exploration initially requested an equipment
scalar EPSM does not expose; corrected to request native hourly electricity and sum
actual EnergyPlus outputs. Initial expanded Workflows check omitted smhi2epw from
PYTHONPATH; all checks passed with the complete existing source paths. No energy
parameter was fitted or calibrated in this change.

Final complete native Core validation: 625 passed, 9 skipped in 95.10 s. Core changes
committed as 940fdaa (Add conservative area-weighted mixed programme blending);
Core working tree clean. Source identities, exact commit patch and validation logs
are retained in the separate GIS-led v2 audit output. Workflows changes remain
reviewable alongside the existing uncommitted study implementation.

## 2026-10-04: parent-first validation implementation

Implemented the approved fixed-observation protocol for MF SE.02: 126 source
records, 85 eligible buildings, 54 independent properties, and 41 GIS/EPC membership
quarantines. The source-boundary audit checks delivered district space heating,
Atemp, separate DHW, measured versus calculated declarations, and existing normal-use
adjustment flags. It pins the existing 296-record construction extraction JSON;
that file supports construction assumptions and is not building-level validation.
The audit is saved in output/archetype-parent-validation-2026-10-04-v1/boundary-audit.json.

Fixed two empty assignment descriptors: compactness and Atemp per apartment now
use the existing native geometry and declared dwelling counts. No energy residual
enters assignment. Kept one parent cohort because 54 properties cannot support
two independently releasable groups of at least 30. Normal-use people, equipment,
lighting, heating temperature and timing remain diagnostic sensitivity controls,
but cannot be fitted against the adjusted annual target. Method selection first
maximises below20 coverage, then minimises equal-property capped absolute percentage
loss (failed simulations cost 2), then prefers the simpler method. Capped loss is
only a selection score; failed cases have no fabricated physical prediction.

Added a supervised, resumable completion runner and per-building/sensitivity
assessment exports. Final assessment includes baseline, representative, shared
and inner-selected predictions, uncertainty, worsened buildings and explicit
release status. National inventory refreshes eligibility from the GIS-led v2 audit.
Workflows verification: 80 tests passed; package, test and completion-runner lint passed.

The measured-context v7 experiment was deliberately interrupted after its baseline
exposed 29 strict failures. It is retained as rejected diagnostic evidence, not as
a completed archetype-quality assessment. Two real defects were isolated: negative
shading centroids from lower-terrain neighbours (and Core's internal capacity hint
on basement floors), and duplicate opposing context faces at touching neighbour
interfaces. Staged Core correction preserves full source shells, clips opaque
casters below every outdoor solar receiver, removes exact opaque internal interfaces,
and puts the internal capacity hint at grade where outdoor receivers are above
grade. Ground/basement physics, targets and strict warning policy are unchanged.
References are embedded in Core's helper docstrings, including EnergyPlus 24.2
SurfaceGeometry::CalcSurfaceCentroid and the Engineering Reference shading module.
Both actual failed buildings aacac6aa and ed10fdad passed strict annual simulation
with the staged correction. Ray-intersection tests verify preserved sun-up shadow
intersections. A fresh source identity is required before restarting validation.

Core correction committed as 9014824. Regression verification: 629 passed and nine
skipped in the staged complete suite; the remaining local-viewer server test passed
with local socket permission (630 total passed). Numerical strictness is unchanged.
For aacac6aa, the old rejected native SQL and corrected native SQL both contain
1,030,216.7579401155 kWh ideal-load heating (relative difference exactly zero).
The diagnosed warning correction therefore did not manufacture an energy improvement.
Fresh campaign v8 freezes the committed Core identity; v7 remains interrupted.

Fresh v8 baseline completed with measured context: 83 accepted/85 attempted;
29/85 below20% and 12/85 below10%; MAE 28.590997 and bias +27.393086 kWh/m²/year.
The remaining c5d92091 psychrometric warning is unchanged. ba6583c0 has two native
context faces of area 0.000816948 m² triggering strict small-surface warnings;
these faces have not been removed or the warnings waived to improve coverage.
Both buildings remain in every method's accuracy denominator. The physics-correct
context addition makes the reference archetype's positive bias worse; source
correction and physical accuracy improvement are distinct claims.
The national matrix inventory records known-era eligible counts SF131910/MF37503/
office2279/school7498; unknown eras are separately counted, not assigned by energy.

Search implementation review found that one failed quadratic anchor caused a
complete native grid for every building. Corrected fallback to operate per building:
a failed anchor, failed joint check or >2% held-out response error now sends only
that building through the full native grid. Other buildings must each pass all
four independent joint checks. Ranking uses actual full-grid values for rejected
buildings (including None failures), and every shortlisted/deployed prediction is
still evaluated natively. No target or release gate changed. v8 was interrupted
for this source correction; native receipts will be reused only through existing
Core/spec/weather/completion identity verification in fresh v9.

Per-building search-fallback tests: 43 transfer tests passed (18.84 s), plus
40 membership/stock/mixed-programme tests (14.12 s); lint passed. Native cache
verification imported 443 completed v8 simulations into fresh v9, skipping none.
Failed and interrupted native cases were not imported; they are rerun under the
same strict policy. Old physical specifications, results and receipts are pinned
in v9/native-cache-import.json. Final calibration predictions remain native.

Source completeness cross-check against the exact existing national certificates
found operational inputs omitted by the legacy cohort JSON. Among 70 exact
certificate matches, 49 have design flow .35 L/s/m² and one .66; two have completed
measures dated before measurement (other ventilation in2005; window replacement
in2012). These are existing observations, not new data. Restored original-source
EgenProjVentFlode and dated AtgUtforda metadata in hydration, retained formular ID
in prepared features, and added completed-window reporting to target-free descriptors.
A declared design flow is not actual operating flow; the existing bounded operating
scenario remains explicit. A qualitative window replacement does not establish
U-value or a complete matrix renovation package. No numerical state or lock is
invented from that flag. The original source files are audited for all85 eligible
buildings, rather than using a nonmatching/latest certificate as a substitute.
v9 remains interrupted diagnostic evidence; final validation uses fresh v10.

Full original-row audit supersedes the partial national cross-check: among all85
eligible audited source rows, 54 declare design flow (53 at.35, one at.66 L/s/m²),
31 have no declared flow, 17 have completed-measure dates during/before measurement,
and six report a dated window replacement. Those dates/flags had been absent in
the legacy cohort export. The same-certificate national view covered only70 rows
and is not substituted for original target-source evidence. Unknown values retain
explicit defaults; completed-measure flags do not specify quantitative U-values,
insulation thickness or actual operating airflow. Revised boundary audit is saved
under archetype-parent-validation-2026-10-04-v2 with original row checksums.
Final source-restoration verification:84 tests passed (40.28 s), lint passed.

Fresh v10 prepared85 buildings with restored source descriptors and design flows.
Exact native cache verification imported1540 accepted cases and rejected23 with
changed physical identity (the source-design-flow correction). These cases are
rerun; no incompatible native result enters validation. Metadata changes also
refit descriptor transforms, representative selection and property-fold fits.

Physical comparability exploration: all85 remain pure housing; 29 have heated
basements and56 do not; 57 pitched and28 flat roofs. Atemp/apartment ranges69–104.33
m²; gross/Atemp1.039–1.544; conditioned-volume/Atemp2.826–4.941 m. These are available
physical distinctions, not inferred renovation classes. Restored-source v10 baseline
remains29/85 below20% and12/85 below10%, with83 accepted; bias+27.818126 and
MAE28.578823 kWh/m²/year. The one design-flow correction changes physical prediction
and slightly lowers aggregate MAE without making the cohort adequate.

First diagnostic screen before metadata restoration found strongest eligible
responses in infiltration and window U. Mechanical-flow and infiltration response
columns were nearly dependent, and excluded standard heating temperature had a
large effect. This is an identification limit, not proof of actual leakage or
window replacement performance. Data claims explicitly call.4ACH an effective
background assumption, not n50 or total ventilation. Do not substitute BETSI total
exchange or infer a pressure conversion. Review the distinction between additional
background exchange and exhaust-driven intake as a physical hypothesis, without
asserting established double counting from annual heat alone. EnergyPlus24.2
Infiltration/Ventilation Engineering Reference documents constant design flow,
weather coefficients and advanced airflow coupling:
https://bigladdersoftware.com/epx/docs/24-2/engineering-reference/infiltration-ventilation.html.
Bounds/controls were not changed in response to this diagnostic interpretation.

Rechecked the residential programme source: Core's Sveby use template explicitly
uses a fallback occupant density when apartment-size/room mix is unknown. Dwelling
count and Atemp alone do not establish that mix; no occupancy value was inferred
from heating residuals. Existing gross/Atemp and volume/Atemp descriptors also
flag the exterior-zone volume approximation. An effective infiltration fit can
absorb part of that reference-volume uncertainty and must not be presented as
measured airtightness. Geometry and reference-area inputs remain frozen.

First v10 inner training fit chose only infiltration multiplier .5 for both
representative and shared modes, despite screening three active controls. This
corresponds to nominal model-background.2ACH from the.4 reference; it is not yet
an outer-validation result or a measured physical leakage rate. Nine building
response approximations exceeded the2% joint-check tolerance and therefore used
the full native search panel. Other independently checked responses were accelerated;
all final candidate scores use native predictions. Frozen source bytes (176 files)
are archived and checksum-verified in v10/artifacts/frozen-source.zip.

Rechecked the original cohort constructor's carrier boundary: archetype_pilot._observation
explicitly rejects positive alternative heating/DHW carriers and solar thermal,
requires measured full-calendar-year district heat and validates storey count.
The restored source rows retain their original verified hashes; the parent has
not silently equated a partial district-heating component with total mixed-carrier
space heating. This check adds no observations and changes no calibration choices.

Native SQL archival during v10: completed receipts are authoritative for cached
predictions. To keep the full native sensitivity experiment within local storage,
completed `energyplus/eplusout.sql` files in v7–v10 are archived as
`eplusout.sql.gz`, with gzip level 1 and a verified SHA-256 round trip before the
uncompressed copy is removed. Each `sql-archive.json` preserves original and
archive checksums and sizes. Restore with `gzip -dk eplusout.sql.gz`. Only cases
with an atomic receipt older than 30 seconds are considered; active outputs,
receipts, model inputs, result scalars, and warning diagnostics remain intact.
This changes storage representation, not simulation physics, selection or results.

Checked observation identity during v10: all 85 eligible rows have distinct EPC
formular IDs. Their 54 property groups provide the training/evaluation separation;
no repeated certificate is counted as another building observation. All rows
retain the external-area caveat and fixed reported Atemp. Generated
archetype-parent-validation-2026-10-04-v2/baseline-errors.png from the completed
unfitted baseline. Added summarize_parent_misses.py to describe completed held-out
errors and boundary hits; its strata are diagnostic, not retroactively selected
or validated sub-archetypes. It cannot feed parameters back into the experiment.

First outer fold (15 buildings/11 properties), frozen single-representative
infiltration multiplier .5: baseline4/15 below20%,4/15 below10%, MAE26.489,
bias+25.709; selected8/15 below20%,3/15 below10%, MAE15.220,bias+2.545.
One numerical failure remains in both denominators. These partial held-out
results are not a cohort release estimate. They illustrate that removing bulk
bias can worsen some already accurate buildings; final reporting must preserve
that effect instead of reporting only the mean improvement. Bounds and search
were not changed in response to the first-fold observations.

Rechecked the original EPC extraction header for unused quantitative envelope and
conditioned-extent evidence. It exposes basement/storey/stairwell counts, total
Atemp, heated-garage area, programme fractions and qualitative completed envelope
measures. It does not expose measured window U-values or conditioned basement
area. These missing quantities must not be invented from the qualitative measure
flags. No further source or parameter changes were made during validation.

Added apply_cohort_profile.py as a concrete deployment example outside the frozen
calibration package. It uses only target-free audited features, checks source
membership policy and the Atemp/spec boundary, retains independent parameter
locks, and writes assignment and native-spec hashes. Normal provisional fallback
and explicit opt-in will be verified against the completed Data artifact. No
national driver defaults or existing calibration sources were changed.

Context temporal caveat: derive_context uses the pinned current footprint/height
release and known heights/ground levels. It does not reconstruct neighbour geometry
at each2017–2024 measurement year or claim known neighbour commissioning dates.
Actual-year weather is matched, but historical context-state fidelity is not
independently established. This is a retained model-input uncertainty, not evidence
that a particular neighbour is wrong and not a reason to remove shading to fit heat.

Application safeguards passed nine checks using an explicitly unreleased smoke
bundle: ordinary provisional fallback preserves the baseline hash; opt-in changes
the native spec; a locked infiltration parameter preserves its baseline; other
municipalities and unaudited membership fall back; mismatched Atemp, building ID,
ventilation topology and heating carrier are rejected. Verification retained in
application-boundary-verification.json with its no-accuracy-claim fixture label.
These application checks are separate from the85-building energy experiment.

After two outer folds (30 buildings): baseline9/30 below20%,6/30 below10%,
MAE29.592,bias+29.215; inner-selected17/30 below20%,9/30 below10%,
MAE17.932,bias+8.288. One numerical failure remains. Outer0 selected single;
outer1 selected shared (infiltration.5,window-U1.2,WWR.2). Thirteen existing misses
mean the85-building90% gate cannot now be reached, even if all55 remaining
buildings pass. Continue the frozen five-fold experiment to quantify the complete
error distribution and retain provisional status; do not expand bounds or use
these misses to tune later folds. Screening log includes NumPy empty-response
warnings for failed representative response vectors; non-finite rows are excluded
from rank diagnostics, while native failures remain in prediction coverage.

Target-free airflow unit audit retained as airflow-unit-audit.py/.json. Frozen
modelled conditioned volume/Atemp ranges2.826–4.941m³/m². At nominal additional
background.4ACH, this means.314–.549L/s/m², alongside mechanical design.35L/s/m²
for84 rows and.66 for one row. Multiplier.5 changes that background contribution
to.157–.274L/s/m². These are additive nominal simple-model quantities, not measured
flow, pressure-coupled intake or n50. A common ACH acts on variable exterior-volume
proxies, so inferred leakage can also absorb reference-volume error. Prioritize
an independently specified conditioned-air-volume convention and the distinction
between exhaust-driven intake, background leakage and total exchange in the next
physical input audit. No internal clear height, retrofit or causal explanation was
inferred, and the current experiment was not adjusted using this diagnostic.

Context shape convention rechecked: context.shading_groups explicitly unions
positive-area-overlapping roof-edge footprints and retains a conservative height
envelope; derive_context chooses the maximum available ridge/p90/representative
height. This is an upper-envelope prism proxy, not an observed roof mesh or a
resolved stepped union of different-height buildings. It can overstate obstruction
around low roofs or mixed-height merged groups. Core9014824 preserves that input
proxy's above-receiver shadow geometry; the successful invariance check does not
prove the proxy matches historical real roofs. Retain this distinction in model
uncertainty, and investigate stepped/roof-aware context from the existing geometry
before interpreting an effective airflow fit as measured leakage. No shading
faces, heights or archetype settings were adjusted to fit the held-out results.

Roof convention check: cohort rows retain eave/ridge heights and floor_height_m
uses measured eave/storey count (e.g.20.2476/6=3.3746m). There is no demonstrated
ridge-as-heated-floor-height bug in these rows. build_baseline uses a generic30°
gable/cold-attic roof, however, without matching the available measured ridge–eave
rise. This synthetic target roof and the conservative neighbour prisms are input
approximations, distinct from envelope material assumptions. A future source-frozen
geometry scenario should reconcile roof rise and existing roof observations; the
present cross-validation and export retain their original declared roof convention.

## Completed cohort transfer: cohort-transfer-mf-se02-parent-2026-10-04-v10

Parent: `multi_family__SE.02__F__district_heat__normal_use_adjusted_pre_weather_correction`.

- baseline: 29/85 below20%; 12/85 below10%; MAE 28.579; bias 27.818 kWh/m²/year; 2 failed.
- single representative: 53/85 below20%; 33/85 below10%; MAE 16.627; bias 5.363 kWh/m²/year; 2 failed.
- shared: 53/85 below20%; 34/85 below10%; MAE 16.86; bias 6.649 kWh/m²/year; 2 failed.
- inner-selected procedure: 53/85 below20%; 34/85 below10%; MAE 16.809; bias 6.008 kWh/m²/year; 2 failed.

Status: provisional; reason: accuracy_target_not_met. 54 independent properties; 62 improved and 16 worsened predictions.
Property-bootstrap 95% below20% interval: [0.4459084084084084, 0.7954868810051736]; below10% interval: [0.2285533453887884, 0.5932272288824583].

Final refit profiles: 1; supported: 0. Final selection repeats the validated inner procedure.
Retrospective property validation on existing observations; no new building data. Effective calibrated assumptions do not identify actual retrofit, operation or heated extent. Exact all-data refits are not independently validated; geographic transfer remains untested. Other use/era cells without measured campaigns remain pending.
Evaluation SHA-256: `6fc89399c7e91ccbb9ea01ed7f98ca17f118495ed86e033adb2d06858ba99b63`. Campaign: `/Users/ssanjay/GitHub/epsm_workflows/output/cohort-transfer-mf-se02-parent-2026-10-04-v10`.

## Completed Data publication and deployment verification

v10 completed all85 attempted buildings/54 properties, five outer and three inner
property folds, and final full-data selection/export. The exported method is single,
infiltration_multiplier=.5, status provisional, coherent one-parent subdivision.
Saved EPSM Data cohort_profiles/2026-10-04-v1/mf-se02-gothenburg.json as separate
artifact_version gothenburg-mf-se02-cohort-v1, municipality1480 only; reference
matrix remainsv1.4. Source export SHA65e1cc6378caab61dfb66ca9b7484ede74590432b6236b43c0f8cbfa60c08ed0;
Data artifact SHA832a32f65a01c76366be76b38fd07c28b6fd69d57cf448a1dc1bcedd1cc63f92.
Six checks against the actual published resource verify default fallback, explicit
opt-in, input locks, other/missing municipality fallback and unaudited membership
fallback. The campaign source/input guard passes after publication. Earlier nine
application-boundary smoke checks remain separately labelled.

Completed error strata: flat22/28 versus pitched31/57 below20%; known dated measures
6/17; known dated window replacement1/6. Five out-of-domain buildings retain baseline
and all fail20%; they are not omitted. These describe unresolved heterogeneity and
possible input conventions, not verified causal explanations or released subclasses.
62 predictions improve,16 worsen,2 numerical failures remain; no automatic warning
waiver or per-building residual correction was introduced. Native producer logs,
regression logs, publication/verification scripts, scalar results and source hashes
are retained in archetype-parent-validation-2026-10-04-v2. Scientific error plots are
saved in the completed campaign. All24 matrix cells are inventoried; national
accuracy support is not inferred from this local provisional result.


2026-10-04 airflow source review: EPSM Data DB1.4.1 records the TABULA additional infiltration surcharge on A_C_ref ×2.5 m (CommonCalculationMethod equation7, pp12–13). Added explicit Data-to-Core reference-volume conversion, source/geometry ACH distinction, assumption flags and source-pointer register for296 extracted records /44 candidate statements. No raw numerical matrix targets changed. Sveby normal-use ventilation and airing are separate quantities. Unit-only85-building scenario, assuming A_C_ref=Atemp explicitly, is saved in output/airflow-source-review-2026-10-04/reference-volume-scenario.json; corrected Core ACH0.2024–0.3538 replaces0.4 in that scenario. No native simulation or accuracy claim. Previously fitted0.5 infiltration multiplier must be refitted against the new volume convention before combining them. The existing national/campaign scripts have not been silently migrated. Detailed notes are in ../epsm_data/docs/airflow-review-notes.md.

Airflow update verification: 256 EPSM Data tests passed (254.87 s); all1080 matrix slots preserve numerical targets; 92 infiltration snapshots refreshed; baseline checksums and whitespace checks pass. Full-suite CLI tests were rerun from the Data repository and localhost integration tests with the required socket permission. Results saved in output/airflow-source-review-2026-10-04/verification.json.

2026-10-04 Core airflow fix committed10d6fb0: added mutually exclusive reference-area infiltration input, compiler design-flow accounting, hourly Ideal Loads/infiltration and S natural-exchange diagnostics, explicit DCV None, and mixed-programme reference-basis safeguards. Original ACH behavior retained. Sources and scientific limits in epsm_core/docs/airflow-accounting.md and inline comments/docstrings. Full suite636passed9skipped; focused native equivalence/recovery checks plus separate native S run passed. Sensible-component tolerance0.5% follows different humidity-dependent cp in EnergyPlus source; exact area/ACH heat equivalence checked at1e-7. No pressure-network or national accuracy claim.

## Data 1.5 full-cohort rerun launched

Prepared `cohort-transfer-mf-se02-parent-data15-2026-10-04-v11` against a frozen
runtime in `archetype-parent-validation-data15-2026-10-04-v3/runtime`. Verified the
same 85 buildings, 54 properties, v10 property folds, targets, locks, parameter
search and all non-airflow physics. Native additional infiltration is .20 L/s/m²
Atemp with ACH zero. Window U=2.2 was already present in every v10 baseline spec.
No prior fitted multiplier or native simulation cache was imported.

Added fixed-property rerun integrity checks and baseline/final paired reporting to
the completion pipeline. Tests: 47 passed across cohort-transfer and rerun tests;
native-output compression roundtrip verified. The producer automatically runs
baseline, nested validation, final selection/export, release assessment and the
v10-to-Data-1.5 comparison. Completed outputs are compressed losslessly with
checksummed restoration records. `execution.json` is authoritative for current
state. A launch supplies no final calibration or independent-validation claim;
the completed outcome will be recorded separately when available.

Data 1.5 v11 full baseline completed: 83 accepted /85 attempted, two strict numerical failures; 54/85 below20%, 37/85 below10%, MAE14.302 and bias+4.079 kWh/m²/year. Identical v10 baseline:29/85 below20%,12/85 below10%, MAE28.579,bias+27.818,two failures. V10 inner-selected calibrated predictions:53/85 below20%,34/85 below10%,MAE16.809,bias+6.008. Thus the new unfitted baseline improves over the prior baseline and has lower MAE than the old calibrated procedure on these inspected properties. New nested calibration remains in progress; no new fitted profile or independent confirmation claim. Full paired report: v11/artifacts/rerun-comparison.json and .md.

2026-10-05 storage recovery: removed90.79GiB (328869 files) of native EnergyPlus/interrupted-run outputs from superseded MF SE.02 cohort-transfer campaigns. Reports, specs, scalar receipts, source snapshots and all current v11 outputs remain. Historical raw outputs are intentionally unavailable following user-authorized deletion; cleanup paths recorded in output/older-result-cleanup-2026-10-05.json. V11 failed after two outer folds because the disk filled; preserved6042 completed simulation receipts. Source/input guard and v10 comparison checks passed after deletion. Resumed the same frozen pipeline with95.38GiB free; terminal failure state archived in v11/artifacts/storage-failure-before-resume-2026-10-05.json. Final evaluation remains pending.

2026-10-05 briefly stopped v11 per request to investigate/commit Core fixes. Before any Core edits, user directed calibration to proceed unchanged. Core checkout remains clean at10d6fb0, frozen campaign sources preserved; resumed6312 retained receipts with original strict warning policy. KÄRRA45:2 tiny-context-face warnings and BISKOPSGÅRDEN86:2 internal psychrometric warnings remain failures in attempted-building coverage. Final evaluation pending.

## Completed cohort transfer: cohort-transfer-mf-se02-parent-data15-2026-10-04-v11

Parent: `multi_family__SE.02__F__district_heat__normal_use_adjusted_pre_weather_correction`.

- baseline: 54/85 below20%; 37/85 below10%; MAE 14.302; bias 4.079 kWh/m²/year; 2 failed.
- single representative: 38/85 below20%; 13/85 below10%; MAE 17.408; bias -7.071 kWh/m²/year; 2 failed.
- shared: 55/85 below20%; 39/85 below10%; MAE 14.522; bias 5.129 kWh/m²/year; 2 failed.
- inner-selected procedure: 55/85 below20%; 40/85 below10%; MAE 14.337; bias 4.629 kWh/m²/year; 2 failed.

Status: provisional; reason: accuracy_target_not_met. 54 independent properties; 12 improved and 9 worsened predictions.
Property-bootstrap 95% below20% interval: [0.4683122362869198, 0.8174565217391304]; below10% interval: [0.30136573692028384, 0.6504114017043785].

Final refit profiles: 1; supported: 0. Final selection repeats the validated inner procedure.
Retrospective property validation on existing observations; no new building data. Effective calibrated assumptions do not identify actual retrofit, operation or heated extent. Exact all-data refits are not independently validated; geographic transfer remains untested. Other use/era cells without measured campaigns remain pending.
Evaluation SHA-256: `f6d67439f0ce2387a26a7e8eeae5c7316d2a4c4b29c3a988143f6239e4793cad`. Campaign: `/Users/ssanjay/GitHub/epsm_workflows/output/cohort-transfer-mf-se02-parent-data15-2026-10-04-v11`.

## 2026-10-05: Data 1.5 catalogue evidence and current profile handover

Recorded the completed v11 study in EPSM Data's packaged
`parametric/mf_se02_data15_validation.json`, catalogue validation register and
MF SE.02 existing window/infiltration evidence. Installed the area-rate v2
Gothenburg profile and `cohort_profiles/current.json`; the historical ACH v1
profile is retained and documented as superseded. Final selection is baseline
with no physical overrides. Baseline achieves54/85 within20%; the fold-dependent
procedure achieves55/85. Both retain two numerical failures. Status remains
provisional; no national, new-property confirmation or other-cell support claim.
Numerical values, material recipes and database/package versions are unchanged.
Verified numerical identity across all1080 slots, current claim signatures,
export checksum, default provisional fallback, opt-in empty overrides and
outside-Gothenburg fallback. Prepared before/after snapshots and write hashes
are in `output/catalogue-update-data15-2026-10-05-v1`; refreshed packaged dataset
hashes in Data's BASELINE.json. No commit or publication was requested.

## 2026-10-05: random national baseline launched before further model changes

User authorized a random sample from the entire eligible cohort. Selected200
buildings uniformly without replacement from all5716 audited MF SE.02/F/district
heat records (1848 properties/233 municipalities), rather than the previously
prepared950. Seed20261005 on sorted building IDs;200 selected buildings span180
properties/94 municipalities. Source hashes, all population IDs, selected IDs
and prior-study flags are saved before energy evaluation. Prior exposure is not
an exclusion or a claim of independence. Frozen runtime contains213 files.
Geometry preflight retains122 candidates and78 input failures (41 unknown roof,
21 nonrectangular pitched footprints,16 implausible storey heights). No source
failure is replaced. Weather requires85 municipality/measurement-year files.
Data1.5 baseline recipes/controls unchanged; no optimisation or catalogue edits.
All200 retain denominator membership; prepared-only accuracy is reported
separately. Launched frozen runner PID14143 with local SMHI weather access and
8 simulation workers. Artifacts: national-mf-se02-random-baseline-2026-10-05-v1.
49 workflow/sampling/regression checks passed, including draw order invariance
and retention of unavailable geometry. Added explicit input preparation failure
handling to preserve failures without forcing fabricated geometry/weather.

Random national baseline completed2026-10-05T04:58:23UTC. All200 selected IDs
retained;78 geometry-input failures (41 unresolved roof form,21 nonrectangular
pitched footprints,16 implausible floor heights),69 weather-input failures and
18 native simulation rejections.32 of85 weather jobs passed unchanged quality
checks.53 native specs produced35 accepted predictions spanning31 properties/
21 municipalities.24/35 accepted predictions are below20%,14/35 below10%;
accepted-case MAE15.0678kWh/m²/year,bias+0.7773. Seven overpredict and four
underpredict by at least20%. Including numerical failures:24/53 prepared cases
below20%; all-selected successful coverage24/200. These distinct denominators
must not be confused with national predictive accuracy. Sixteen cases have
psychrometric out-of-range warnings, two have wet-bulb convergence warnings;
one case also has tiny context faces. No warning waived or failed case replaced.
Before simulation, corrected only serialized completed-measure dictionary
representation; repaired an unguarded multiprocessing resume launcher. Both
interruptions are retained and no simulation receipts existed from those failed
launches. Original sample/runtime/spec hashes verified; all53 specs retain2.2
window U and.20L/s/m² additional infiltration. No fitted overrides. Reports,
all200 outcomes, warning details and verification are saved in the study root.
Receipt-safe native archives are lossless. Qualification stays provisional;
geometry/weather coverage and numerical robustness precede further calibration.


## 2026-10-05: correct national geometry preparation gates

Removed the obsolete pitched-plan rectangular proxy and arbitrary floor-height
rejection in shared pilot helpers. The national runner now uses the baseline's
shared native geometry builder and resolves geometry before weather compilation.
Original footprints and measured heights are retained; unusual heights are
review flags, while unknown roofs and actual native errors remain explicit.
The unchanged 200-building sample passes 158 native geometry preflights: all 37
old rectangle/height exclusions are recoverable; 41 roof forms remain unresolved
and one previously admitted case has duplicate thermal surfaces. Evidence:
`output/national-mf-se02-geometry-gate-fix-2026-10-05-v1/`.
No annual simulations or calibration were launched by this correction.


## 2026-10-05: commit roof guard and integrate bounded preparation

Core commit: `be78938a6dc440352b0ce89eb34669c2dc46ee59`

Core now rejects a roof facet wholly collapsed onto the attic floor before
constructing duplicate thermal boundaries, with an explicit preparation-required
diagnostic. Facets with positive clearance retain existing minimum-edge handling.
No validation check is disabled or engine warning waived.

The commit contains only roofs.py and its regression tests. Existing Core
preparation worktree changes remain separate; verification.json records their
source hashes as well as the committed roof source.

Workflow preflight and generated baselines now call Core's bounded preparation
API and accept only accepted status. Original and cleaned geometry, metric CRS,
policy, change metrics and edge lineage are retained in national inputs and
campaign features. Explicit supplied native specs keep their existing input path.

The unchanged random sample has **159/200 passing native geometry checks** and
**41 unresolved roof types**. There are 159 preparation receipts; bounded cleanup
changes 44 outlines. The formerly duplicate building passes after cleanup.

**96 Core tests and 63 workflow tests passed**. Core lint and workflow E9/F lint
passed. Final workflow source reproduces identical geometry results for all 200.

The cleaned building completed an annual EnergyPlus regression under strict
acceptance with the committed Core code. The uncleaned outline is rejected at
compilation by the new actionable zero-clearance diagnostic.

That engine check uses a borrowed accepted 2019 EPW because this building lacks
accepted local weather, with no neighbour context. It establishes geometry/engine
behavior, not EPC accuracy. Committed smoke results are authoritative; prototype
smoke diagnostics remain in separate folders. No cohort calibration was rerun.
Existing frozen national results and catalogue are retained.


## 2026-10-05: declared flat fallback for unclear roofs

Source roof_form=unclear selects a compact flat model using determined measured
representative_height_m. Source labels and measured heights are preserved;
modelled_roof, roof_fallback_applied and roof_assumption retain policy, height
source and limitations. No roof-class evidence is fabricated.

On the same 200-building random sample:

| Group | Buildings | Native geometry accepted |
| --- | ---: | ---: |
| Classified roofs | 159 | 159 |
| Unclear roofs using flat fallback | 41 | 41 |
| Total | 200 | 200 |

Twenty-eight unusual floor-height review flags remain. This is successful geometry
preparation, not verification of actual storey heights or actual roof shapes.
The fallback omits a possible attic buffer and may misstate actual roof area and
conditioned volume. Known flat/pitched classifications retain existing choices.
Missing measured heights and unsupported roof types remain failures.

Future national evaluations write separate roof_completion_groups metrics for
source-unclear/fallback and classified roofs. Failures remain in their original
group denominators. Per-building results and campaign features preserve fallback
metadata. No new annual energy results or calibration have been generated.
Original frozen study and catalogue are retained. See verification.json and
prepared-geometry.json for source hashes and per-building assumptions.
