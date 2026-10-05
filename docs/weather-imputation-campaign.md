# Swedish weather completion sensitivity campaign

This is a **planned, staged expansion** of the completed
[Gothenburg 2023 experiment](weather-imputation-sensitivity.md). The planned
matrix is six geographical strata × three actual years × six synthetic
building profiles: 18 reference weather datasets and 108 reference
building/site/year combinations. These numbers describe the design, not
completed coverage or an accuracy guarantee. Actual availability and progress
are recorded in the output receipts and `coverage.json`.

The forthcoming final matrix is **v4: planned, not yet run or complete**. Its
source plan retains 17 qualified references and one unsupported combination,
Kiruna 2018. The completed v3 pilot is a stage-one precursor with its own frozen
runtime and accepted receipts, not a completed multi-site campaign. The v1/v2
launch directories remain excluded diagnostics and contribute no accepted
campaign comparisons. Source qualification does not establish completed
weather-imputation coverage.

The campaign compares how weather completion affects delivered sensible
heating/cooling demand, hourly peaks, timing and weather events. Each method
uses the same withheld original evidence and each building receives the same
weather cases. Buildings supply different thermal responses; they do not add
independent climates to the sample.

## Sites, years and reference weather

The six **geographical strata** span southern Sweden to the far north. They do
not represent six identical physical coordinates repeated every year. The
[frozen coverage-selection plan](../output/weather-imputation-campaign-2026-10-05-v3/source-selection/plan.json)
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
establish realistic ground conditions for Swedish buildings. A preliminary
ambient-annual-mean prescription triggered an EnergyPlus 24.2 warning and Core
rejection; that attempt supplies no accepted building results. The campaign
uses the native boundary without an exception to Core's diagnostics policy.

## Outages and methods

### Completed v3 pilot

The [v3 pilot report](../output/weather-imputation-campaign-2026-10-05-v3/report.html)
covers Göteborg-Landvetter 2023, all six building profiles, meteorology only,
seed 11 and four-season random panels with 48-/168-hour blocks. It contains
**60 paired building rows**: six references and 54 non-reference comparisons,
using **36 unique annual simulations**. All accepted runs have zero warnings,
severe errors and fatal errors. This is one site/year, not 60 climates.

For the 48-hour new/legacy hierarchy comparison, the new priority reduces
hourly heating-load MAE for **6/6 profiles** and cooling-load MAE for **4/6**.
Absolute annual-energy error improves for only **1/6 profiles in each mode**.
The priorities are identical at 168 hours in this pilot, so those pairs are
ties. These are reconstruction-sensitivity comparisons against the reference
simulation, not improvements against measured building demand. Full paired
values are in [paired-hierarchy.csv](../output/weather-imputation-campaign-2026-10-05-v3/paired-hierarchy.csv).

The random masks cover **zero baseline annual-peak hours and zero absolute
heatwave/cold-spell hours**. Peak matches and unchanged absolute spells therefore
provide little evidence about preservation during outages affecting those
events. This limitation motivates the additional weather-event anchors in v4.

### Planned v4 panels

Each scope has 3-, 12-, 48- and 168-hour outages. Meteorological panels withhold
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

V4 permits at most **56 panels per site/year**: three seasonal random panels
plus up to four extreme/event anchors, across two scopes and four durations.
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

## Metrics and interpretation

The catalogue records annual heating/cooling energy, absolute and percentage
differences, area-normalized demand, hourly peak magnitude, all tied peak
timestamps, exact peak-timing matches, nearest signed timing shifts and demand
at the baseline peak. It also includes hourly load error and energy differences
over masked intervals plus a fixed 168-hour recovery window. That carryover
window is a diagnostic choice, not a proven limit on thermal memory. Percentage
errors with zero reference demand are undefined and remain missing.

Weather errors use bias, MAE and RMSE, with circular wind-direction differences.
Event records include overlapping, lost/new, split/merged episodes, durations
and extrema. Operational definitions include daily Tmax ≥25°C for three complete
days, Tmin ≤−10°C for three days, baseline-pinned warm/cold daily percentiles,
wind ≥10 m/s, temperature jumps >5°C/hour, nighttime solar and the broad
2,000 W/m² solar guard. These are transparent diagnostics, not official warning
definitions. Temperature-extreme anchors do not guarantee that a complete
heatwave is masked or that reconstruction preserves an event.

Summaries stratify by building, scope, sampling group, outage length and method.
They retain valid denominators, site/year counts and evidence-panel counts.
Empirical medians, 5th/95th percentiles and absolute-error tails describe the
evaluated scenarios; site/year median summaries also avoid giving sites with
more available panels extra weight. They are **not population confidence
intervals**. Repeated masks and building responses do not create additional
independent climate samples. Paired hierarchy differences use the same
site/year/building/evidence identifiers; smaller absolute error is a comparison
within this matrix, not proof of a universally best recovery method.

## Planned v4 stages and retaining evidence

The following commands describe the forthcoming v4 matrix. Run them with the
finalized v4 driver once available; they are not evidence that v4 has executed.
From the repository root, use a Python environment with EPSM Core,
`smhi2epw` dependencies and EnergyPlus 24.2. The driver snapshots Core from the
first experiment's saved packages and smhi2epw from the sibling checkout before
importing either. Source and driver hashes, execution environment and the
EnergyPlus binary/schema identify the campaign. Changed pinned identities
require a separate output directory. `--energyplus` selects the executable and
must remain consistent across stages.

```sh
../epsm_core/.venv/bin/python -m examples.weather_imputation_sensitivity.run_campaign \
  --phase plan --output output/weather-imputation-campaign-2026-10-05-v4

../epsm_core/.venv/bin/python -m examples.weather_imputation_sensitivity.run_campaign \
  --phase sources --select-sites gothenburg --select-years 2023 --workers 1 \
  --output output/weather-imputation-campaign-2026-10-05-v4

../epsm_core/.venv/bin/python -m examples.weather_imputation_sensitivity.run_campaign \
  --phase run --select-sites gothenburg --select-years 2023 \
  --output output/weather-imputation-campaign-2026-10-05-v4 \
  --select-buildings detached_old_light,office_glazed \
  --select-durations 3,48,168 --select-seeds 11 \
  --select-scopes meteorology --sampling random_seasonal --workers 1
```

Filters select stages of the same matrix. Expand them progressively; one to
three workers bound simultaneous engine/API load. `--output` selects a campaign
directory; the intended final directory is
`output/weather-imputation-campaign-2026-10-05-v4`.
Use the campaign's saved driver/packages when resuming an archived version;
the current working tree may implement a different experiment.

Planned outputs retain matrix/runtime receipts, raw responses, source frames,
panel plans, supported/unavailable case catalogues, validated EPWs, weather
errors and per-weather event records. Building folders retain Core models,
specifications, accepted diagnostics, error logs, hourly loads and receipts.
V3 preserves SQL losslessly as gzip with compressed/uncompressed fingerprints
and removes bulky auxiliary ESO/MTR/EIO/BND/RDD/MDD/HTML traces. Additional
lossless storage improvements for the larger v4 matrix are under development
to fit the available disk; no implementation or final retention policy is
claimed here yet. Aggregate CSVs,
`paired-hierarchy.csv`, `coverage.json` and HTML describe only accumulated
available comparisons. A completed selected batch does not mean the wider
matrix is complete.

The old Gothenburg study uses its own saved `reproduction/run_study.py`, pinned
packages and original sources. Follow the [historical reproduction command](../examples/weather_imputation_sensitivity/README.md#environment-and-run)
into a fresh output directory; the current generalized modules cannot reproduce
its frozen hierarchy and model assumptions.
