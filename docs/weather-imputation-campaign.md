# Swedish weather completion sensitivity campaign

## Revised analysis: retained transient warnings, 6 October 2026

The current [paper, methods and four summary plots](../output/weather-imputation-campaign-2026-10-05-v5/analysis-reviewed-warnings/paper.html)
retain the bounded transient psychrometric warnings already reviewed by EPSM
Core. The earlier blanket exclusion was unnecessarily conservative for this
study. No EnergyPlus simulations, weather inputs, building models or original
Core diagnostics were changed.

The revised analysis retains all **18,366 labelled outcomes from 9,450 unique
annual runs**, including **2,742 runs with accepted transient warnings**. There
are **102 reference building combinations across 17 weather-years in all six
regions**. Kiruna 2018 remains unavailable because its source weather fails the
original coverage criteria. The automatic-versus-reference subset contains
**4,146 building comparisons across 691 physical outage masks**: 2,058
meteorological and 2,088 solar responses. Six profiles share each weather mask.

Retention is limited to approved EnergyPlus 24.2 psychrometric exceptions with
Core's recorded review evidence, bounded occurrence counts and no humidity
control. Additional checks authenticate each retained hourly archive, require
complete finite nonnegative sensible loads, and reconcile annual totals, peaks
and all tied peak hours with the original sensible-load receipts. Other warnings,
severe/fatal errors, pending reviews and incomplete runs remain excluded. All
2,742 reviewed-warning runs pass these checks; no additional runs are excluded.

Core's original exception remains scoped to annual heating. Retaining sensible
cooling and peak results is a **descriptive study policy**, following the user's
prior assessment and these consistency checks; it does not rewrite Core receipts
or independently establish zero warning impact on every metric. Core total
cooling includes latent load, so the checks use the study's sensible cooling
receipt rather than compare different physical quantities.

The [revised analysis manifest](../output/weather-imputation-campaign-2026-10-05-v5/analysis-reviewed-warnings/manifest.json),
[diagnostic review](../output/weather-imputation-campaign-2026-10-05-v5/analysis-reviewed-warnings/diagnostics-qualification.json),
[figure receipt](../output/weather-imputation-campaign-2026-10-05-v5/analysis-reviewed-warnings/summary-figures/manifest.json)
and [paper receipt](../output/weather-imputation-campaign-2026-10-05-v5/analysis-reviewed-warnings/paper-receipt.json)
record the revised evidence. Copyable [methods and results](../output/weather-imputation-campaign-2026-10-05-v5/analysis-reviewed-warnings/paper-methods-results.txt)
and PNG/SVG/PDF exports accompany the HTML.

Reproduce this postprocessing without running EnergyPlus:

```sh
TZ=UTC0 PYTHONDONTWRITEBYTECODE=1 ../epsm_core/.venv/bin/python -m examples.analyze_weather_imputation_campaign \
  output/weather-imputation-campaign-2026-10-05-v5 \
  --output output/weather-imputation-campaign-2026-10-05-v5/analysis-reviewed-warnings
TZ=UTC0 MPLCONFIGDIR=/private/tmp/weather-campaign-mpl ../epsm_core/.venv/bin/python -m examples.plot_weather_campaign_summary \
  output/weather-imputation-campaign-2026-10-05-v5/analysis-reviewed-warnings
TZ=UTC0 MPLCONFIGDIR=/private/tmp/weather-campaign-mpl ../epsm_core/.venv/bin/python -m examples.report_weather_campaign_paper \
  output/weather-imputation-campaign-2026-10-05-v5/analysis-reviewed-warnings
```

Validation: **406 weather-study tests passed**; changed Python files pass Ruff.
The analysis policy changes outside the frozen simulation runtime. The original
14 drivers and 77 pinned package files remain unchanged.

## Archived initial warning-exclusion analysis

The following note and its linked original outputs record the earlier analysis
at commit `851b184`. Its 12 weather-years and 2,916 automatic comparisons are
**superseded by the revised analysis above**. Its original receipts and qualifier
source snapshot are preserved for provenance; replay that archived publication
with its original code version, rather than the updated qualification helper.

The **v5 campaign execution and numerical analysis are complete**. The design
requested six Swedish geographical strata × three actual years × six synthetic
building profiles. Seventeen of the 18 requested site-years supplied eligible
reference weather; Kiruna 2018 remains explicitly unavailable. Conservative
engine-warning screening leaves building results for **12 site-years in five
strata**, while weather and event evidence retain **17 site-years in all six**.
The [final synthesis](../output/weather-imputation-campaign-2026-10-05-v5/synthesis.html)
and [publication receipt](../output/weather-imputation-campaign-2026-10-05-v5/synthesis-receipt.json)
passed final provenance verification. The concise
[current automatic-versus-reference paper](../output/weather-imputation-campaign-2026-10-05-v5/paper-summary.html)
presents methods, results and four summary figures.

This controlled experiment measures how automatic weather completion changes
ideal sensible heating/cooling demand, hourly load peaks, peak timing and weather
events relative to a common reference. It does not validate real building energy
use. Six synthetic profiles provide different thermal responses to the same
weather evidence, rather than six independent climate observations.

The frozen EPSM harness is `6ce7f10`, following `93d5959`. Smhi2epw is `5930c07`,
including the recovery hierarchy introduced in `3bb5a87` and subsequent
cloud-source/unit corrections. Core and EnergyPlus 24.2 are unchanged. The
[runtime manifest](../output/weather-imputation-campaign-2026-10-05-v5/runtime/manifest.json)
records exact identities. The final consolidated weather-study suite passed
**390 tests**; Ruff was clean on the 17 owned Python files at that checkpoint.
Tests support implementation checks; the execution audits establish coverage.

## Completed coverage and qualification

[Raw coverage](../output/weather-imputation-campaign-2026-10-05-v5/coverage.json),
[analysis manifest](../output/weather-imputation-campaign-2026-10-05-v5/analysis/manifest.json)
and [diagnostic qualification](../output/weather-imputation-campaign-2026-10-05-v5/analysis/diagnostics-qualification.json)
use different, explicit denominators:

| Evidence | Final count | Interpretation |
| --- | ---: | --- |
| Requested / source-ready / source-unavailable site-years | 18 / 17 / 1 | Kiruna 2018 has no eligible full-meteorology reference |
| Completed raw site-year case matrices | 17 | Unavailable/unsupported methods remain recorded |
| Raw reference building rows | 102 | Six profiles × 17 weather references |
| Raw building rows / unique canonical engine executions | 18,366 / 9,450 | Labelled cases can share one identical annual simulation |
| Qualified / excluded building rows | 13,104 / 5,262 | Includes reference and non-reference rows |
| Qualified reference building rows / site-years / strata | 72 / 12 / 5 | Engine-qualified climate coverage is smaller than weather coverage |
| Qualified deduplicated non-reference building responses | 10,248 | One physical mask per building/method/sampling stratum |
| Qualified current-automatic responses / physical masks | 2,916 / 486 | 1,452 meteorology and 1,464 solar building responses |
| Current-automatic weather-only physical masks | 691 | 343 meteorology + 348 solar, across 17 site-years |
| Qualified meteorology hierarchy pairs / masks | 1,452 / 242 | Current and previous automatic priority share evidence |
| Qualified forced common-method responses | 7,650 | 127 meteorology and 244 solar masks, all six profiles |

The raw weather catalogue contains 3,616 labelled panel/method comparisons:
3,044 available and evaluated, 321 unsupported and 251 unavailable. The 904
planned panels include 882 truth-ready and 22 unsupported panels. Current
automatic completion succeeds for **882/882 truth-ready named panels** across
both scopes, including **343/343 distinct meteorological masks**. These are
observed study availability results, not a guarantee that external providers
always return usable data for every request.

Whole-run qualification authenticates original diagnostics, receipts, logs,
models and canonical/alias weather. Any engine warning excludes **all** building
outcomes from that run, even when Core permits its annual-heating result alone.
An unqualified reference also excludes associated building/site-year
comparisons; aliases inherit the canonical run's qualification. Valid weather
errors and event diagnostics remain available independently.

There are 2,742 warned canonical runs and 5,496 raw actual/recurring warning
markers. Five complete building-reference years are excluded: Östersund
2021/2023, Luleå 2021 and Kiruna 2021/2023. Additional warned candidates are
excluded within otherwise qualified years. Original warnings and receipts are
retained. The warnings concern EnergyPlus wet-bulb iteration and do not establish
that the input weather was physically unrealistic: one Gothenburg example uses
approximately 4.3°C, 39% RH and 98,640 Pa; its reported −123.39°C was an internal
solver iteration, not exported air temperature. The full raw campaign is not
warning-free.

The outcome screen deliberately sacrifices coverage for a consistent,
warning-free building cohort. It is not a random loss of climates. No building
conclusion is supported for Kiruna, and the weather evidence in all six strata
must not be presented as building evidence in all six.

## Sites, years and reference weather

The six **geographical strata** span southern Sweden to the far north. They do
not represent six identical physical coordinates repeated every year. The
[frozen coverage-selection plan](../output/weather-imputation-campaign-2026-10-05-v5/source-selection/plan.json)
uses original quality-filtered observations and the predeclared gap criteria,
without building outcomes. A single compatible eligible station across the
three years is preferred where available; otherwise the closest eligible
fallback among screened candidates within 100 km is selected for that year.
The prespecified primary is retained when eligible, and Stockholm uses the
predeclared Arlanda alternative consistently. Identities are then fixed across
methods. This is a rule within the screened candidates, not a claim that every
possible Swedish archive has been searched.

| Stratum | Year | Primary SMHI station | Distance from city, km | Fixed donor | Measured-GHI station |
| --- | ---: | --- | ---: | ---: | ---: |
| Malmö | 2018 | 62040 Helsingborg A | 49.64 | 52240 | 53445 |
| Malmö | 2021 | 54290 Skillinge A | 83.45 | 53530* | — |
| Malmö | 2023 | 52240 Falsterbo A | 27.29 | 52350* | 53445 |
| Gothenburg | 2018 | 72420 Göteborg-Landvetter Flygplats | 19.20 | 71420 | 71415 |
| Gothenburg | 2021 | 72420 Göteborg-Landvetter Flygplats | 19.20 | 71420 | 71415 |
| Gothenburg | 2023 | 72420 Göteborg-Landvetter Flygplats | 19.20 | 71420 | 71415 |
| Stockholm | 2018 | 97400 Stockholm-Arlanda Flygplats | 33.71 | 97200 | 98735 |
| Stockholm | 2021 | 97400 Stockholm-Arlanda Flygplats | 33.71 | 97200 | 98735 |
| Stockholm | 2023 | 97400 Stockholm-Arlanda Flygplats | 33.71 | 97200 | 98735 |
| Östersund | 2018 | 124300 Klövsjöhöjden A | 79.88 | 124030 | — |
| Östersund | 2021 | 124300 Klövsjöhöjden A | 79.88 | 124030 | — |
| Östersund | 2023 | 136090 Krångede A | 77.07 | 136410 | — |
| Luleå | 2018 | 162860 Luleå-Kallax Flygplats | 4.89 | 161940 | 162015 |
| Luleå | 2021 | 162860 Luleå-Kallax Flygplats | 4.89 | 161940 | 162015 |
| Luleå | 2023 | 162860 Luleå-Kallax Flygplats | 4.89 | 161940 | 162015 |
| Kiruna | 2018 | 180940 Kiruna Flygplats: unsupported† | 5.73 | 179960† | 180025† |
| Kiruna | 2021 | 179960 Nikkaluokta A | 50.47 | 180940 | 178985 |
| Kiruna | 2023 | 180760 Gällivare A | 81.46 | 180770* | — |

\* These partial donors lack hourly QFF pressure metadata. Their missing
parameter remains explicit and does not authorize pressure transfer.
† Kiruna 2018 retains the original candidate's metadata only for its unsupported
receipt. No screened station with all five required variables within 100 km
satisfies the unchanged ≤1% missing-cell and ≤48-hour gap limits. Its candidate
donor/GHI entries are not a consumed reference or accepted experiment.

City distances reach 83.45 km. Weather geometry, building location and ERA5
extraction use each primary station's actual coordinates and elevation, recorded
in the plan and year-specific receipts. These include airport, coastal and
mountain exposures; they are not city-centre measurements. For example,
Klövsjöhöjden is about 803 m above sea level, rather than an Östersund urban
reference. Changing station coordinates/exposure between years prevents
attributing differences solely to climate or year. Method effects remain paired
within the exact same station/year/mask/building.

The years are deliberately contrasting examples rather than a random climate
sample. SMHI describes [2018 as warm, sunny and dry](https://www.smhi.se/klimat/klimatet-da-och-nu/arets-och-arstidernas-vader/arets-och-arstidernas-vader-och-vatten/2018-12-28-aret-2018---varmt-soligt-och-torrt-ar).
[2021 had national annual temperature near the 1991–2020 normal](https://www.smhi.se/nyheter/nyheter/2022-06-14-vaderaret-2021-nu-i-presentationsformat),
following warmer years. [2023 was particularly wet in southern Sweden and ended with a cold December](https://www.smhi.se/klimat/klimatet-da-och-nu/arets-och-arstidernas-vader/arets-och-arstidernas-vader-och-vatten/2023-12-28-aret-2023---mycket-nederbordsrikt-i-sodra-sverige).
These summaries motivate contrasting actual weather sequences; precipitation
effects themselves are outside this thermal-weather experiment.

A reference must have at most **1% missing required meteorological cells** on
the exported annual grid and no native required-variable gap above **48 hours**
on the buffered source grid. Native gaps receive bounded temporal completion,
and those filled cells are frozen common background. A dataset needing donor
or ERA5 meteorology to establish its reference is unavailable for this study.
Only finite, quality-approved original meteorological observations can be
withheld and scored as observed truth.

Measured GHI is used when the fixed radiation station has usable historical
coverage within 50 km. STRÅNG supplies alternate GHI and the beam partition.
Solar scores therefore compare with a **hybrid reference**: original source
completeness does not make DNI/DHI independently observed truth. Donors within
75 km are fetched as comparison sources; their availability does not approve
transfer. Gap-specific assessment happens after withholding observations.
Unavailable donor/ERA5 inputs remain explicit missing comparison data.

Five qualified references declare no measured-GHI station within 50 km:
Malmö 2021, Östersund 2018/2021/2023 and Kiruna 2023. Their solar reference is a
STRÅNG-based proxy. The remaining qualified references declare measured GHI
plus STRÅNG partitioning; actual usable fractions are recorded in source
receipts. Nikkaluokta 2021 uses Tarfala Sol (178985), about 18.44 km away.

## Buildings and paired boundaries

EPSM Core constructs six explicitly specified profiles and compiles them for
EnergyPlus 24.2. They are sensitivity models, not calibrated Swedish stock
archetypes or construction-date classifications.

| Profile identifier | Illustrative response | Floor area, m² |
| --- | --- | ---: |
| `detached_old_light` | Light envelope, higher leakage | 120 |
| `detached_new_heavy` | Heavy insulated envelope, heat recovery | 240 |
| `multifamily_old` | Heavy envelope, exhaust ventilation | 1440 |
| `multifamily_new` | Heavy insulated envelope, heat recovery | 1440 |
| `office_glazed` | Higher glazing and internal gains | 1800 |
| `school_daytime` | Daytime occupancy and outdoor-air loads | 1600 |

Geometry, mass, gains, airflow, glazing and schedules vary together. Differences
between profiles cannot isolate one building factor. Each profile's model is
held invariant across weather methods within a site/year and checked by its
compiled-model fingerprint.

Heating and cooling use unlimited ideal loads, without humidity control.
Cooling is diagnostic even for a dwelling or school without installed cooling.
Results are sensible thermal demand, including the prescribed mechanical
outdoor-air loads; they are not purchased energy, plant electricity, latent
cooling or design-capacity measurements. Peaks are hourly means.

The ground-facing floor uses **EPSM Core's native prescribed 18°C boundary**,
constant in all twelve months, profiles, locations, years and recovery methods.
This is an **uncalibrated conditioned-floor boundary**, not a soil-temperature
estimate or a boundary derived from ambient weather. The same prescription
keeps the floor boundary invariant in every paired comparison.

It omits regional and seasonal ground-temperature variation, soil/snow physics
and uncertainty in floor-to-ground heat transfer. Consequently, absolute demand
and sensitivity results are conditional on this explicit boundary and do not
establish realistic ground conditions for Swedish buildings. The preliminary ambient-derived ground prescription supplies no accepted
campaign results. The final prescription requires no ground-warning exception.

## Outages and methods

Each scope has 3-, 12-, 48- and 168-hour outages **per block**. Meteorological panels withhold
all five required variables; solar panels withhold GHI and direct components.
Recovery methods are paired by an evidence identifier independent of buildings
and methods. Receipts store exact UTC hour-ending timestamps and selected-cell
fingerprints. The annual calendar uses fixed UTC+1 local standard time, with
hour 24 assigned to the preceding EPW label date and no daylight saving.

There are two separate sampling groups:

- **Random seasonal panels:** seeds 11, 29 and 47 each select one complete block
  in DJF, MAM, JJA and SON. Blocks are separated by an observed hour and have
  observed adjacent endpoints. Each panel thus hides 12, 48, 192 or 672 hours.
- **Weather-extreme/event panels:** separate windows containing the warmest and
  coldest original annual temperature hours, plus optional
  `heatwave_hottest_hour` and `cold_spell_coldest_hour` anchors within detected
  absolute spells. The optional anchors are determined separately from annual
  extrema and are omitted when the corresponding event is absent. Every anchor
  uses weather alone and is shared across all buildings; none is selected from
  simulated loads. An unavailable complete window containing the exact anchor
  is unsupported, and the experiment does not select a different event. Such
  windows may cross seasonal boundaries but stay within the year.

Duration labels refer to each block, not the total withheld hours in a file.
Random seasonal files have four blocks: 12/48/192/672 hours in total for
3/12/48/168-hour blocks. Event files contain one block and therefore hide only
3/12/48/168 hours. Absolute error magnitudes across these sampling groups also
reflect different exposure budgets; they cannot isolate a random-versus-event
effect. Method comparisons remain paired within the same sampling stratum and
physical mask.

Reconstruction contexts are built after removing **all** selected original
cells. Native filling in the context must also exclude hidden truth. Scored
cells and donor assessment never receive previously filled reference values as
observations. Every exported EPW field outside the synthetic masks is held
identical to the common reference, including its frozen native fills. A
reconstruction or identity failure cannot establish a successful comparison.
This stricter prediction-context rule belongs to the expanded campaign; the
first experiment's saved driver and historical results are retained unchanged.

Meteorology compares five methods:

| Method | Controlled recovery |
| --- | --- |
| `temporal` | Short linear/circular interpolation through 3 hours, then bounded daily profiles through 48 hours |
| `donor_assessed` | Same-year fixed donor, accepted local overlap assessment and optional median offset; bounded filling of donor holes |
| `era5` | Same-year ERA5 at the primary coordinates/elevation, limited to hidden cells; surface pressure remains in Pa |
| `automatic` | Short interpolation → assessed donor → bounded daily profiles → ERA5 |
| `automatic_legacy` | Bounded temporal completion first → assessed donor → ERA5 |

The paired automatic comparison changes priority while holding donor identity,
observations, assessment rules, physical derivation and buildings fixed. It
does not repeat station discovery for every case. Raw donor transfer is an
unassessed diagnostic in the first study, not a default campaign method.

Solar compares temporal, ERA5 and automatic completion with production
darkness/geometry handling; donors are excluded. The two automatic priorities
have the same solar policy, so only one solar automatic case is needed. A
168-hour forced temporal case is unsupported. Erbs cases retaining GHI test a
different information-loss scenario and are not included in this full-solar
campaign matrix.

Optional cloud treatment is **frozen** across controlled cases. Production's
scoped ERA5 cloud augmentation is a separate operational effect not measured
here. Derived dew point, longwave and surface pressure are regenerated
consistently; no extra cloud-driven change is introduced outside the masks.

V5 retains the v4 design of at most **56 panels per site/year**: three seasonal
random panels plus up to four extreme/event anchors, across two scopes and four
durations.
With all events, evidence and comparison sources available, the methods permit
at most **210 non-reference weather comparisons per site/year**: 133
meteorological and 77 solar. Across the 18 requested site/years this gives at
most **22,788 paired building rows, including 108 references**. Event absence,
source and method qualification, and reuse of identical operative EPWs change
the actual counts. These are bounds on work and rows, not promised results.

The frozen selection qualifies 17 references, so the corresponding ceiling is
**21,522 rows including 102 references**, before panel and comparison-source
availability reduce it further. The requested Kiruna 2018 combination remains
an explicit unsupported outcome and contributes no observed-truth error scores.

The inventory derived from the 17 qualified references contains **904 panels:
882 ready and 22 unsupported**, using **62 available weather anchors** across
the site/years. It includes 191 repeated scope/duration/mask combinations across
panel labels. These labels retain the intended event comparisons, but repeated
masked evidence does not supply independent observations. Panel readiness describes eligible withheld truth. Final reconstruction and
engine-outcome qualification are counted separately below.

## Metrics and observed automatic-completion errors

Each completed EPW is compared with the common reference weather and simulation
for its site/year/profile. Building metrics include absolute annual sensible
heating/cooling errors in kWh/m², hourly mean peak errors in W/m², all tied peak
hours, peak-hour-set preservation, nearest signed timing shifts, full-year
hourly-load MAE and errors over the masked interval plus a fixed 168-hour
recovery window. That recovery window is diagnostic, not a demonstrated limit
on thermal memory. Peak magnitude and timing are measured; load duration and
peak duration are not.

The following **current automatic versus reference** values are pooled median /
p95 absolute annual errors in kWh/m² for random seasonal panels. Each file
contains four blocks, so the total missing hours exceed the block duration.
Each group includes 36 physical masks and 216 building responses over 12
site-years, except meteorology/48 hours, which includes 35 masks and 210
responses. All six profiles qualify for each included mask.

| Scope | Block duration, h | Total hidden hours/file | Heating median / p95, kWh/m² | Cooling median / p95, kWh/m² |
| --- | ---: | ---: | ---: | ---: |
| Meteorology | 3 | 12 | 0.00105 / 0.00845 | 0.000705 / 0.00424 |
| Meteorology | 12 | 48 | 0.00690 / 0.0586 | 0.00584 / 0.0408 |
| Meteorology | 48 | 192 | 0.0174 / 0.114 | 0.0135 / 0.0646 |
| Meteorology | 168 | 672 | 0.0816 / 0.886 | 0.0610 / 0.261 |
| Solar | 3 | 12 | 0.00323 / 0.0344 | 0.00206 / 0.0189 |
| Solar | 12 | 48 | 0.0130 / 0.115 | 0.0118 / 0.0963 |
| Solar | 48 | 192 | 0.104 / 0.271 | 0.0306 / 0.293 |
| Solar | 168 | 672 | 0.252 / 0.996 | 0.222 / 1.063 |

These descriptive tails show increasing annual sensitivity with longer random
outages within this selected matrix. Event-panel medians may be near numerical
zero because a warm event need not cause heating; p95 and the actual exposed
peak/event counts are necessary alongside medians. Smaller median error does
not guarantee a small worst case or preserve an extreme. The underlying
[qualified deduplicated comparisons](../output/weather-imputation-campaign-2026-10-05-v5/analysis/deduplicated-comparisons.csv)
retain building, source-year, scope, sampling and duration identities.

Percentages are undefined at zero reference demand. Cooling below 1 kWh/m² is
flagged because percentages can be unstable; these cases remain in absolute
error summaries. Full-year hourly MAE averages over 8,760 hours and therefore
dilutes short outages. Empirical quantiles describe the selected scenarios,
**not population confidence intervals or guaranteed bounds**. Pooled responses,
equal-site-year means and equal-location summaries answer different weighting
questions. The six profiles, duplicate anchor labels and repeated episode
exposures do not create independent climates.

Weather metrics use bias, MAE and RMSE, with circular wind-direction errors.
Operational event definitions include daily Tmax ≥25°C for three complete LST
days, daily Tmin ≤−10°C for three days, baseline-pinned daily percentile warm/cold
spells, wind ≥10 m/s, temperature jumps >5°C/hour, nighttime solar and a broad
2,000 W/m² solar plausibility guard. They are transparent diagnostics rather
than official warning definitions. Thresholds are fixed to the reference for
candidate comparisons. Temperature anchors do not guarantee that a complete
spell is masked.

Peak-hour-set preservation is assessed only when a positive reference annual
peak was actually hidden; unexposed peaks supply no evidence of recovery during
the peak. In meteorological event panels, current automatic completion retains
the exact exposed peak-hour set in 10/22 heating and 13/28 cooling responses
with 12-hour blocks, and 11/26 heating and 14/38 cooling responses with 48-hour
blocks. The denominators are exposed building responses, not unique weather
episodes. Heatwave overlap also does not prove preservation of episode maxima:
no exposed heatwave at 3/12/48-hour blocks retains both exact bounds and exact
peak temperature under either automatic priority in this matrix. Plausible
extremes must remain visible in uncertainty and provenance reporting.

### Conditional priority and forced-method evidence

The archived paired hierarchy comparison supports donor-before-daily-profiles
as a practical meteorological default for 4–48-hour gaps, while retaining
per-parameter assessment, fallback and warnings. With 12-/48-hour blocks,
current priority usually reduces annual heating and full-year heating-load MAE.
Equal-site-year mean absolute annual heating error for random panels is
0.0153 versus 0.0233 kWh/m² at 12 hours and 0.0377 versus 0.0979 at 48 hours;
for temperature-event panels it is 0.00705 versus 0.0201 and 0.0374 versus
0.0591. Meteorological 3-/168-hour scored outputs are exact ties between
priorities. This finding is specific to the tested policy change and cohort.

Cooling and peaks remain mixed. Random 12-hour annual cooling error is slightly
higher under current priority (0.0126 versus 0.0117 kWh/m² equal-site-year mean).
Random 48-hour cooling peak error is also higher (0.0713 versus 0.0328 W/m²),
despite more individual improvements than deteriorations. One Malmö 2021 school
case increases from 0.910 to 7.43 W/m². The paired result does not establish
universal superiority, guaranteed heatwave preservation or safe sizing from a
completed file. Full values and denominator-specific summaries are retained in
[hierarchy-pairs.csv](../output/weather-imputation-campaign-2026-10-05-v5/analysis/hierarchy-pairs.csv)
and [hierarchy-summary.csv](../output/weather-imputation-campaign-2026-10-05-v5/analysis/hierarchy-summary.csv).

Forced assessed transfer of all five meteorological parameters qualifies for
139/343 distinct truth-ready masks (40.5%). The labelled inventory instead has
184/434 available donor comparisons (42.4%); it includes repeated panel labels.
Neither denominator is the production per-parameter donor acceptance rate.
Automatic completion may use an accepted partial donor and another fallback
for the remaining parameters. The 127-mask qualified common meteorological
cohort is donor-selected and cannot establish that donors universally outperform
ERA5. Solar method comparisons use hybrid measured-GHI/STRÅNG proxy truth,
not independently measured DNI/DHI. Its apparent ERA5 advantages therefore
cannot be interpreted as independent beam/diffuse-radiation validation.

## Figures and retained evidence

Four current-automatic summary figures show annual and peak error medians/p95,
actually exposed peak-hour preservation and weather-event overlap/bounds.
Their variation bands describe tested scenarios rather than population
confidence bounds. The [summary manifest](../output/weather-imputation-campaign-2026-10-05-v5/analysis/summary-figures/manifest.json)
binds the completed analysis and original numeric tables.

| Current automatic versus reference | PNG | SVG | PDF | Numeric data |
| --- | --- | --- | --- | --- |
| Annual demand errors | [PNG](../output/weather-imputation-campaign-2026-10-05-v5/analysis/summary-figures/automatic-annual-errors.png) | [SVG](../output/weather-imputation-campaign-2026-10-05-v5/analysis/summary-figures/automatic-annual-errors.svg) | [PDF](../output/weather-imputation-campaign-2026-10-05-v5/analysis/summary-figures/automatic-annual-errors.pdf) | [CSV](../output/weather-imputation-campaign-2026-10-05-v5/analysis/summary-figures/load-error-summary.csv) |
| Hourly mean peak errors | [PNG](../output/weather-imputation-campaign-2026-10-05-v5/analysis/summary-figures/automatic-peak-errors.png) | [SVG](../output/weather-imputation-campaign-2026-10-05-v5/analysis/summary-figures/automatic-peak-errors.svg) | [PDF](../output/weather-imputation-campaign-2026-10-05-v5/analysis/summary-figures/automatic-peak-errors.pdf) | [CSV](../output/weather-imputation-campaign-2026-10-05-v5/analysis/summary-figures/load-error-summary.csv) |
| Exposed annual peak-hour sets | [PNG](../output/weather-imputation-campaign-2026-10-05-v5/analysis/summary-figures/automatic-peak-timing.png) | [SVG](../output/weather-imputation-campaign-2026-10-05-v5/analysis/summary-figures/automatic-peak-timing.svg) | [PDF](../output/weather-imputation-campaign-2026-10-05-v5/analysis/summary-figures/automatic-peak-timing.pdf) | [CSV](../output/weather-imputation-campaign-2026-10-05-v5/analysis/summary-figures/peak-timing-summary.csv) |
| Weather-event preservation | [PNG](../output/weather-imputation-campaign-2026-10-05-v5/analysis/summary-figures/automatic-weather-events.png) | [SVG](../output/weather-imputation-campaign-2026-10-05-v5/analysis/summary-figures/automatic-weather-events.svg) | [PDF](../output/weather-imputation-campaign-2026-10-05-v5/analysis/summary-figures/automatic-weather-events.pdf) | [CSV](../output/weather-imputation-campaign-2026-10-05-v5/analysis/summary-figures/weather-event-summary.csv) |

The [building-specific summary](../output/weather-imputation-campaign-2026-10-05-v5/analysis/summary-figures/building-error-summary.csv)
retains differences among the bundled synthetic profiles without attributing
them to one building factor. Reproduce the current-only summaries with
`python -m examples.plot_weather_campaign_summary CAMPAIGN/analysis`, replacing
`CAMPAIGN` with the complete analysis's campaign directory.

Seven further campaign figures are generated from the qualified analysis. They document
the full method study; the separate concise paper focuses on current automatic
completion versus reference. Each figure manifest binds the exact analysis and
source-table hashes. Numeric data remain available for independent inspection.

| Figure | PNG | SVG |
| --- | --- | --- |
| Paired annual errors | [PNG](../output/weather-imputation-campaign-2026-10-05-v5/analysis/figures/paired-annual-errors.png) | [SVG](../output/weather-imputation-campaign-2026-10-05-v5/analysis/figures/paired-annual-errors.svg) |
| Paired peak errors | [PNG](../output/weather-imputation-campaign-2026-10-05-v5/analysis/figures/paired-peak-errors.png) | [SVG](../output/weather-imputation-campaign-2026-10-05-v5/analysis/figures/paired-peak-errors.svg) |
| Building-profile changes | [PNG](../output/weather-imputation-campaign-2026-10-05-v5/analysis/figures/building-profile-changes.png) | [SVG](../output/weather-imputation-campaign-2026-10-05-v5/analysis/figures/building-profile-changes.svg) |
| Exposed peak timing | [PNG](../output/weather-imputation-campaign-2026-10-05-v5/analysis/figures/exposed-peak-timing.png) | [SVG](../output/weather-imputation-campaign-2026-10-05-v5/analysis/figures/exposed-peak-timing.svg) |
| Weather-event preservation | [PNG](../output/weather-imputation-campaign-2026-10-05-v5/analysis/event-figures/paired-weather-event-preservation.png) | [SVG](../output/weather-imputation-campaign-2026-10-05-v5/analysis/event-figures/paired-weather-event-preservation.svg) |
| Common meteorological methods | [PNG](../output/weather-imputation-campaign-2026-10-05-v5/analysis/method-figures/common-method-meteorology-errors.png) | [SVG](../output/weather-imputation-campaign-2026-10-05-v5/analysis/method-figures/common-method-meteorology-errors.svg) |
| Common solar methods | [PNG](../output/weather-imputation-campaign-2026-10-05-v5/analysis/method-figures/common-method-solar-errors.png) | [SVG](../output/weather-imputation-campaign-2026-10-05-v5/analysis/method-figures/common-method-solar-errors.svg) |

Figure source data and receipts: [paired/load CSV](../output/weather-imputation-campaign-2026-10-05-v5/analysis/figures/figure-data.csv)
and [manifest](../output/weather-imputation-campaign-2026-10-05-v5/analysis/figures/manifest.json);
[event CSV](../output/weather-imputation-campaign-2026-10-05-v5/analysis/event-figures/figure-data.csv)
and [manifest](../output/weather-imputation-campaign-2026-10-05-v5/analysis/event-figures/manifest.json);
[common-method CSV](../output/weather-imputation-campaign-2026-10-05-v5/analysis/method-figures/figure-data.csv)
and [manifest](../output/weather-imputation-campaign-2026-10-05-v5/analysis/method-figures/manifest.json).

Further tables retain [physical panel identities](../output/weather-imputation-campaign-2026-10-05-v5/analysis/physical-panels.csv),
[weather errors](../output/weather-imputation-campaign-2026-10-05-v5/analysis/weather-comparisons.csv),
[event exposure](../output/weather-imputation-campaign-2026-10-05-v5/analysis/event-exposure-comparisons.csv),
[method availability](../output/weather-imputation-campaign-2026-10-05-v5/analysis/method-availability.csv),
[forced common-method outcomes](../output/weather-imputation-campaign-2026-10-05-v5/analysis/common-method-comparisons.csv)
and [diagnostic exclusions](../output/weather-imputation-campaign-2026-10-05-v5/analysis/diagnostic-exclusions.csv).
The original [raw report](../output/weather-imputation-campaign-2026-10-05-v5/report.html)
is the diagnostic catalogue. The verified
[final synthesis](../output/weather-imputation-campaign-2026-10-05-v5/synthesis.html)
and [publication receipt](../output/weather-imputation-campaign-2026-10-05-v5/synthesis-receipt.json)
separate raw coverage from qualified outcomes. The
[current-only paper](../output/weather-imputation-campaign-2026-10-05-v5/paper-summary.html)
focuses on automatic completion versus the common reference, with four summary
figures and verified local artifact links.

## Runtime, archives and reproduction

The [completed controller receipt](../output/weather-imputation-campaign-2026-10-05-v5/execution-environment/process-resume-20261005T155946-a12acd81.json)
records final verification without engine reruns. The
[independent final audit index](../output/weather-imputation-campaign-2026-10-05-v5/analysis-audits/independent-final-audit-index-20261005T161556Z.json)
checks the 14 scientific driver files, 77 package files, 69 pinned source frames,
246 cached API responses and all 9,450 canonical receipt/model identities;
compiled schedules and selected lossless SQL round trips are also verified.
The [final analysis-bound audit index](../output/weather-imputation-campaign-2026-10-05-v5/analysis-audits/independent-final-analysis-bound-audit-index-20261005T163822Z.json)
records the independent audit binding to the final regenerated analysis.
Identical operative EPWs can share an engine run while keeping their labelled
comparison receipts. This explains why 18,366 building rows are not 18,366
independent simulations.

Execution used native processes with `TZ=UTC0`. This avoids a macOS timezone
service stall during EnergyPlus startup; the EPW and SQL weather clock stays
fixed UTC+1 local standard time. The guarded scheduler runs at most three
children, each executing one original full site/year with `--workers 1`, and
serializes global metadata/reporting after children join. Controller locks,
completed-case verification and shared-matrix equality checks protect resume.
Operational scheduler changes and postanalysis do not change the frozen
scientific drivers, weather, buildings, Core or engine.

V5 retains **all SQL and hourly loads losslessly**. Reference SQL is
self-contained XZ preset 3. Candidate SQL uses standard Zstandard 1.5.6 level 12
with `--patch-from` the exact immutable reference SQL for the same
building/site/year; its matching dictionary is required for decoding. One plain
reference dictionary remains recoverable from XZ. Compressed, original and
dictionary SHA-256 checks precede consumption, and the decoder version/binary
identity is pinned. Hourly CSV uses gzip level 6 with a sidecar preserving the
logical Core path. Identical models use hash-verified hardlinks. Auxiliary
ESO/MTR/EIO/BND/RDD/MDD/HTML traces are removed; receipts, models, specifications,
error logs, schedules, SQL and hourly evidence remain.

The [completed EPW archive operation](../output/weather-imputation-campaign-2026-10-05-v5/execution-environment/post-weather-storage-ab024de0fd744769b1839223f67507e7.json)
retains 3,044 candidate EPWs as verified lossless gzip: 5.109 GB of original
bytes occupies 0.574 GB. Baseline EPWs remain plain. Compression and sidecars
preserve original weather/Core receipts and can restore exact bytes with
`gzip -dk weather.epw.gz`. Qualification fingerprints the final plain/archive
representation: archive **before** final analysis. Changing representations
later requires regenerating qualification, analysis, plots and synthesis.

A fresh execution needs enough free disk space for the full study. The 2 GiB
free-space guard preserves completed cases at interruption; it does not reduce
the scientific scope. Resume the saved campaign from the repository root using
its pinned drivers/packages and identical engine:

```sh
TZ=UTC0 PYTHONDONTWRITEBYTECODE=1 ../epsm_core/.venv/bin/python -u \
  -m examples.resume_weather_imputation_campaign \
  --output output/weather-imputation-campaign-2026-10-05-v5 --processes 3
```

Manual work selection remains one site/year at a time, retaining all frozen
profiles/scopes/seeds/durations:

```sh
TZ=UTC0 PYTHONDONTWRITEBYTECODE=1 ../epsm_core/.venv/bin/python \
  -m examples.weather_imputation_sensitivity.run_campaign \
  --phase run --select-sites malmo --select-years 2018 --workers 1 \
  --output output/weather-imputation-campaign-2026-10-05-v5
```

The frozen runner supports `--phase plan`, `sources` and `run`; it has no
`--phase report`. Changed scientific source/runtime identities require a new
output directory. After all workers have joined and raw matrix coverage has
been verified, the separate final postprocessing sequence is:

```sh
weather_study_root=output/weather-imputation-campaign-2026-10-05-v5
weather_study_python=../epsm_core/.venv/bin/python

PYTHONDONTWRITEBYTECODE=1 "$weather_study_python" -m examples.archive_weather_campaign_files \
  "$weather_study_root"
PYTHONDONTWRITEBYTECODE=1 "$weather_study_python" -m examples.analyze_weather_imputation_campaign \
  "$weather_study_root" --output "$weather_study_root/analysis"
PYTHONDONTWRITEBYTECODE=1 "$weather_study_python" -m examples.plot_weather_campaign_summary \
  "$weather_study_root/analysis"
PYTHONDONTWRITEBYTECODE=1 "$weather_study_python" -m examples.plot_weather_imputation_campaign \
  "$weather_study_root/analysis"
PYTHONDONTWRITEBYTECODE=1 "$weather_study_python" -m examples.plot_weather_campaign_events \
  "$weather_study_root/analysis"
PYTHONDONTWRITEBYTECODE=1 "$weather_study_python" -m examples.plot_weather_campaign_methods \
  "$weather_study_root/analysis"
PYTHONDONTWRITEBYTECODE=1 "$weather_study_python" -m examples.report_weather_imputation_campaign \
  "$weather_study_root"
```

Final commands omit `--provisional` and refuse incomplete or changed evidence.
The standalone common-method command is
`python -m examples.plot_weather_campaign_methods /ABS/v5/analysis`, with the
absolute analysis folder and appropriate EPSM environment substituted.

Earlier outputs retain their own identities. V1/v2 are excluded launch
diagnostics; v3 is a completed one-site/year pilot with 60 labelled building
rows, 36 unique annual runs and zero engine warnings. Its random masks hide no
annual peaks or absolute-spell hours, limiting its event conclusions. V4 is
partial diagnostic evidence. A research-only optional solar placeholder fix
required a fresh v5 harness; production already created the placeholder and
needed no corresponding code change. Imported accepted runs retain original
receipts and migration provenance after identical regenerated weather, model
and SQL checks. The original Gothenburg experiment is described in
[its historical study note](weather-imputation-sensitivity.md); reproduction
requires its saved `reproduction/run_study.py`, pinned packages and original
sources in a fresh output directory, as shown in the
[historical reproduction command](../examples/weather_imputation_sensitivity/README.md#environment-and-run).
The current generalized modules do not reproduce those earlier assumptions.
