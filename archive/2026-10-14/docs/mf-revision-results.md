# MF revision implementation and first results

The evidence workflow is implemented. A calibrated replacement default catalogue
has not been released: the available national stock has already been evaluated,
and independent component and operating evidence remains incomplete.

## Implemented changes

- All 216 MF classification slots are registered against 18 canonical construction
  states, with source assumptions, support counts and provisional dispositions.
- A target-free national sample selects independent properties across physical
  parent groups before repeating finer strata. Known extraction mismatches are
  quarantined; energy residuals do not determine inclusion.
- Independent, dated quantitative window, wall, roof and operating-flow evidence
  now changes the corresponding compiled input and locks that control. Completed
  EPC work alone does not identify the resulting U-value or flow.
- Separate physical scenarios preserve geometry, Atemp and structural materials.
  Their exact insulation additions are exported. The runner retains failed and
  skipped attempts, checks immutable sources and begins with baseline smoke runs.
- Property bootstrap comparisons report coverage within 20%, MAE, bias and
  miss/pass transitions. Promotion requires physical evidence, complete paired
  runs, independent validation/test support and subgroup protection.
- A sourced, bounded 32-design joint sensitivity stage is available after separate
  investigation. Candidate selection uses coverage, then MAE, then absolute bias.

## Balanced SE.02/F screen

Twelve properties were selected with four in each BBR18 zone. All 132 simulations
passed strict acceptance. These are retrospective development results, with no
quantitative component observations supplied.

| Separate change | Applicable properties | Coverage before → after | MAE before → after, kWh/m² |
|---|---:|---:|---:|
| Wall total R +40% | 12 | 58.3% → 75.0% | 20.09 → 16.84 |
| Operating flow ×0.8 | 12 | 58.3% → 66.7% | 20.09 → 16.56 |
| Roof/top total R +40% | 12 | 58.3% → 58.3% | 20.09 → 18.60 |
| Conditioned basement 18°C | 6 | 50.0% → 66.7% | 20.06 → 16.18 |
| Conditioned basement 15°C | 6 | 50.0% → 83.3% | 20.06 → 15.51 |
| Basement mechanical flow zero | 6 | 50.0% → 66.7% | 20.06 → 16.33 |
| Basement residential gains zero | 6 | 50.0% → 50.0% | 20.06 → 24.46 |
| Cold-attic outdoor exchange 2 ACH | 6 | 50.0% → 50.0% | 24.58 → 24.50 |

Lower flow fixed three threshold misses and created two. Thus it is not a clean
universal correction. Removing gains worsened average error; this distinguishes
basement temperature/ventilation from the assumed residential use. Zero flow and
zero gains are diagnostic bounds. Neither establishes a storage-room default.
Attic exchange was weak on this panel. No whole-family inference follows from six
attics, six basements or twelve properties.

Property-bootstrap 95% intervals for the MAE change were −5.99 to −0.16 kWh/m²
for wall R+40%, −8.32 to +1.68 for flow ×0.8 and −8.29 to +1.08 for basement
18°C. Even the wall result is retrospective and does not identify the assembly.
The accompanying plot is saved in
`output/mf-revision-screen-2026-10-08-v2/sensitivity-intervals.png`.

An earlier screen accidentally sampled only Zone 1 because fine strata were
sorted ahead of parent-zone coverage. It is preserved as a northern diagnostic;
the corrected sampler has a regression test, and the balanced study supersedes
it for this comparison. Its 138 accepted cases are not pooled with the balanced
panel to inflate support.

## National preparation

The reviewed inventory contains 50,791 MF buildings; 23,800 pass the campaign's
physical and accounting checks. Of 1,200 independently selected properties,
391 prepared successfully, 807 need local weather for their measurement year and
two failed native geometry preparation. Prepared inputs span all six eras and
22 era/ventilation groups. Preparation read no energy targets.

All 391 baseline simulations were accepted. Retrospective coverage within 20% is
35.3%, MAE 40.08 kWh/m² and signed bias +14.97 kWh/m². This is performance on
the weather-available selected panel, not national population performance.

The earliest era overpredicts in every available ventilation family: average
signed errors range from +76 to +130 kWh/m². Later FTX groups generally
underpredict; SE.04/FTX is −24.19 and SE.05/FTX is −30.55 kWh/m². A blanket
increase in insulation or reduction in ventilation would therefore move some
already underpredicted groups further in the wrong direction. Prioritize actual
surviving fabric and renovation assignment in SE.01, and operating/heat-recovery,
system timing and heat-accounting evidence for later FTX. The residual pattern
does not uniquely identify those causes.

Seventy-two prepared SE.01 walls have no insulation layer. Their source structure
is retained. Their wall-resistance sensitivity is explicitly unavailable pending
a sourced insulation assembly; the other controls remain testable.

Thirty-three mechanical systems used reported-zero design flow as operating flow.
An absolute alternative of 0.35 L/s/m² Atemp (the existing normal-use completion)
was strictly simulated for all 33. Coverage rose from 9.1% to 42.4%; MAE fell
from 40.74 to 32.38 kWh/m² and bias moved from −20.25 to +2.07 kWh/m². Thirteen
misses were fixed and two created. The paired 95% coverage-change interval is
+15.1 to +54.5 percentage points. This supports an explicit unknown-flow fallback
option, not a claim that all 33 operated at that rate. Multipliers of zero cannot
test this direction. The design benchmark in
[BBR18 6:251](https://www.boverket.se/contentassets/3108c5069a60495380949c906e9c6f0b/bbr-18-ovk.pdf)
uses floor area; the model completion uses Atemp. Their boundaries are recorded
separately, and neither substitutes for measurement.

Weather reuse is limited to previously accepted municipality/year files and
retains a site-representativeness caveat. Missing weather is a campaign gap,
not evidence that the corresponding archetype performs well or badly.

## Decisions supported now

The broad separate screen covered 58 properties across all 22 available era/system
groups. All 600 cases were accepted: 484 new simulations and 116 reused identical
strict baseline receipts. Each group has at most three properties, so these are
directional checks, not independent archetype validation.

| Separate change | Applicable properties | Coverage before → after | MAE before → after, kWh/m² |
|---|---:|---:|---:|
| Wall R +40% | 46 | 28.3% → 30.4% | 24.00 → 24.36 |
| Top R +40% | 58 | 22.4% → 24.1% | 38.91 → 38.23 |
| Flow ×0.8 | 50 | 24.0% → 30.0% | 33.44 → 33.24 |
| Flow ×1.2 | 50 | 24.0% → 36.0% | 33.44 → 34.63 |
| Basement 18°C | 24 | 8.3% → 12.5% | 53.52 → 47.71 |
| Basement 15°C | 24 | 8.3% → 20.8% | 53.52 → 43.36 |
| Basement gains zero | 24 | 8.3% → 16.7% | 53.52 → 56.23 |
| Attic 2 ACH | 40 | 22.5% → 22.5% | 35.28 → 35.30 |

The broad wall result reverses the SE.02/F average-error benefit. The higher-flow
case increases coverage while worsening MAE, and removing basement gains similarly
fixes threshold misses while worsening MAE. Keep those tradeoffs visible. Neither
threshold coverage alone nor the smaller SE.02 sample establishes a universal
correction.

The MF campaign now explicitly selects normal-use completion for reported-zero
mechanical flow, retaining the report and provenance. Independently measured flow
takes precedence. Existing generic preparations retain their prior policy unless
this option is selected. Quantitative component evidence also locks controls in
national preparation, separate screens and subsequent joint designs.

The final national preparation rebuilt all 391 available cases with this policy.
Every full physical hash matched an accepted receipt: 33 tested flow alternatives
and 358 original baselines. Engine/schema and Atemp checks passed, and receipt
reuse provenance is sealed. Coverage rises from 35.3% to 38.1% and MAE falls from
40.08 to 39.37 kWh/m². Signed bias increases from +14.97 to +16.85 kWh/m² because
some already overpredicted buildings also receive more ventilation. This remains
a modest overall improvement and does not close the MF fit problem.

Authoritative artifacts:

- [Current 216-slot inventory](../output/mf-revision-national-2026-10-08-v6/report.md)
- [Final national policy baseline](../output/mf-revision-national-prepared-2026-10-08-v4/report.md)
- [Broad separate sensitivity results](../output/mf-revision-national-screen-2026-10-08-v1/report.md)
- [Reported-zero flow comparison](../output/mf-revision-zero-flow-2026-10-08-v1/report.md)
- [Balanced SE.02/F comparison](../output/mf-revision-screen-2026-10-08-v2/report.md)

Earlier studies retain their original source hashes and assumptions. They are
archived evidence, not resumable studies under the final source version. The final
inventory and national policy baseline match the current implementation.

Retain provisional numerical catalogue defaults while collecting independent
evidence. Prioritize basement use/operation and era-specific wall assemblies,
then operational flow; keep attic exchange a smaller investigation unless new
roof evidence changes that result. Review period-specific geographic rules before
regional assignments: original SBN limits support moderate differences, with
construction-type and permit-transition qualifications, not uniform R+40%.

The reported window and roof discrepancy was a missing application of completed
work descriptors to components. There was no verified wall-specific application
failure in that sample. The new quantitative evidence path addresses components
independently. Corrected heights, floor definitions, basement extent, measured
operation and dated renovation performance remain required inputs; they must not
be inferred by fitting heating residuals.

See [workflow and release gates](mf-revision.md) and
[original regional source review](mf-regional-construction-evidence.md).

Verification: 87 focused tests passed, two optional tests skipped; the final
21-test MF safeguard suite also passed. Ruff checks passed for the changed
workflow, preparation, evidence, diagnostic and test files. The prior results and
source versions are retained; changing workflow hashes requires a new study.
