# AMY completion sensitivity experiment

This standalone experiment uses **EPSM Core** to construct and compile a fixed
100 m², one-zone building, then runs EnergyPlus 24.2 against controlled variants
of a complete Gothenburg 2023 AMY. It calls the installed `smhi2epw` temporal,
donor assessment, radiation and physical derivation functions. It does not change
the production recovery policy.

The first study's results and limitations are catalogued in
[the study note](../../docs/weather-imputation-sensitivity.md). Full local
artifacts are under `output/weather-imputation-sensitivity-2026-10-05-v1`.

## Environment and run

Use an environment containing `epsm_core`, `smhi2epw`, `pandas`, `numpy`,
`matplotlib` and EnergyPlus 24.2. The existing EPSM Core environment has the
Python dependencies except the sibling smhi2epw checkout, which can be installed
editable or provided through `PYTHONPATH`.

From the repository root, reproduce the historical study with its **saved
driver and pinned packages** into a fresh output directory:

```sh
PYTHONPATH="$PWD/output/weather-imputation-sensitivity-2026-10-05-v1/reproduction/pinned-packages" \
  ../epsm_core/.venv/bin/python \
  output/weather-imputation-sensitivity-2026-10-05-v1/reproduction/run_study.py \
  --output output/weather-imputation-sensitivity-reproduction \
  --pinned-sources output/weather-imputation-sensitivity-2026-10-05-v1/sources
```

The driver is deliberately specific to this dataset. Initial retrieval defaults
to the existing 2023 campaign cache and the sibling smhi2epw donor cache. Missing
responses are fetched from SMHI and the Open-Meteo ERA5 archive. Original caches
are copied and never edited. Subsequent runs reuse pinned hourly sources.

`--pinned-sources` copies the original source directory for an offline run.
The generalized modules in the current checkout implement the expanded campaign
and must not replace the saved historical driver. Do not target the original
archive when invoking that saved driver: its final source-copy step would copy
the driver onto itself. A previously created separate reproduction directory can
be resumed with the same command and identities.

`--energyplus` selects the executable. Existing simulations are reused only after
checking weather, building specification, Core/compiler, engine/schema and
harness identities, model bytes, accepted diagnostics and recomputed SQL metrics.
A mismatch requires a fresh output directory. Cases with identical operative
EPW headers and hourly data can reuse a simulation; comment changes are ignored.

## Controlled alternatives

Each meteorological panel removes all five required variables together in four
originally observed windows: around the reference heating peak, cooling peak,
15 April and 15 October. Panel outage lengths are 3, 48 and 168 hours, giving
12, 192 and 672 hidden hours respectively.

- `temporal`: the production linear method through three hours, or guarded
  daily-profile reconstruction through 48 hours. A 168-hour temporal case is
  recorded as unsupported.
- `donor_raw`: forced same-hour Göteborg A observations, without applying the
  suggested fitted offset. This is an unassessed diagnostic comparison.
- `donor_assessed`: production gap-specific assessment, using only unmasked
  original primary observations, followed by raw transfer or an accepted median
  offset. Native donor holes receive bounded temporal filling.
- `era5`: forced same-year ERA5 at the target coordinates/elevation, inserted
  only into hidden cells. Surface pressure stays in its separate Pa path.
- `automatic`: production bounded filling followed by the fixed assessed donor,
  then ERA5 only where still unresolved. Station discovery is intentionally
  frozen; this is not a full new `compile_epw` request for each variant.

Solar panels remove GHI and direct components together for 48/168-hour windows.
They compare guarded temporal profiles where supported and coherent ERA5 solar.
Separate Erbs cases retain GHI and remove only the direct components; they have
more information and must not be ranked as equivalent complete-solar outages.

The complete reference has small documented native reconstructions. Those cells
are frozen background, excluded from donor training, and not selected as hidden
truth. Measured GHI and modelled partition data are held constant for meteorology
panels. Optional cloud data remain unchanged/missing throughout. Derived dew
point, longwave and surface pressure are regenerated consistently. Every
alternative must preserve every unmasked EPW field exactly.

## Outputs and checks

- `report.html`, three figures, `summary.csv`: annual sensible thermal demand,
  hourly peaks, all tied peak hours, exact timing matches/shifts, baseline-peak
  load and affected-window energy. These are delivered loads, not HVAC fuel or
  electricity. Times are hour-ending fixed UTC+1 LST.
- `event-preservation.csv`, `anomalies.json`: operational heatwaves/cold spells,
  reference-pinned percentile spells, wind episodes, temperature jumps, source
  boundaries, solar differences and physical solar checks, including event
  overlap, splits/merges, duration/peak changes and lost/new events.
- `catalogue.json`, `audit.json`, `sources/`: masks, method decisions, source
  fingerprints, model/engine/compiler identities and original inputs.
- `cases/`: validated EPWs, weather receipts, Core building/model files,
  simulation diagnostics, hourly loads and metrics. `processing` describes
  physical derivation; `hidden_hours`/`decisions` describe forced replacements.
- `reproduction/`: exact driver files and pinned source packages. Preliminary
  excluded experiments are explicitly separate under `diagnostics/`.

Fast synthetic checks:

```sh
PYTHONPATH="$PWD/output/weather-imputation-sensitivity-2026-10-05-v1/reproduction/pinned-packages" \
  ../epsm_core/.venv/bin/python -m pytest \
  tests/test_weather_sensitivity.py tests/test_weather_anomalies.py -q
```

The study demonstrates sensitivity for one illustrative building/year and four
selected windows. It does not establish local weather accuracy or calibrate the
building. The urban donor and airport reference have different exposure and
elevation; see the study note before interpreting small annual differences.

## Expanded Swedish campaign

The [campaign design and reproduction note](../../docs/weather-imputation-campaign.md)
describes the corrected **v5: interrupted at the disk guard, incomplete**, across six
geographical strata × three years × six buildings. The completed v3 stage-one
pilot is retained separately: Gothenburg 2023, all six profiles, four-season
meteorological outages of 48/168 hours and seed 11. It has 60 paired rows,
36 unique annual simulations and zero warnings/severe/fatal errors.

At 48 hours, the new priority improves hourly heating-load MAE for 6/6 profiles
and cooling MAE for 4/6; absolute annual-energy error improves for only 1/6 in
each mode. At 168 hours the priorities tie. The pilot masks cover zero annual
load-peak and absolute heatwave/cold-spell hours, making unchanged peaks/spells
uninformative about preservation during those events.

The wider matrix is in progress, separate from both that pilot and
the original completed Gothenburg study. Station
metadata fixes each reference's actual exposure and coordinates; 2018, 2021 and
2023 are deliberately contrasting years, not a random climate sample. Stations
can differ between years, preventing causal attribution of inter-year changes
solely to climate. The six synthetic EPSM Core
profiles include detached and multifamily homes, an office and a school, without
measured-building calibration.

V5 retains v4's panels: 3/12/48/168-hour outages, seasonal seeds 11/29/47 and
separate warmest/coldest original annual-temperature anchors. Optional detected absolute
`heatwave_hottest_hour`/`cold_spell_coldest_hour` anchors are added independently
of annual extrema and omitted when the corresponding event is absent. Anchors
are weather-based and shared across all buildings, not selected from building
loads. Every method/building shares its
panel's evidence. The new automatic priority (short interpolation → assessed
donor → bounded daily profiles → ERA5) is paired with the previous temporal-first
priority. Solar comparisons stay donor-free. Missing sources, rejected transfers
and unsupported temporal cases remain explicit; native fills are shared frozen
background and reconstruction contexts exclude all hidden truth.

```sh
TZ=UTC0 ../epsm_core/.venv/bin/python -m examples.weather_imputation_sensitivity.run_campaign \
  --phase plan --output output/weather-imputation-campaign-2026-10-05-v5

TZ=UTC0 ../epsm_core/.venv/bin/python -m examples.weather_imputation_sensitivity.run_campaign \
  --phase run --select-sites malmo --workers 3 \
  --output output/weather-imputation-campaign-2026-10-05-v5

TZ=UTC0 ../epsm_core/.venv/bin/python -m examples.weather_imputation_sensitivity.run_campaign \
  --phase run --select-sites gothenburg --workers 3 \
  --output output/weather-imputation-campaign-2026-10-05-v5
```

The full native session stopped at the 2 GiB free-disk guard. Lossless archival
and cache deduplication recovered roughly 3 GB of task-owned storage. Available
space continues to fluctuate; the study has no active simulation process.
Completed runs and pinned sources remain intact.
Execution needs adequate storage before resumption; freeing at least 10 GiB or
providing another output drive is the next operational step.

These commands illustrate explicit one-stratum batches after storage is available,
retaining every planned year/profile/scope/seed/duration for that stratum. Repeat
for `stockholm`, `ostersund`, `lulea` and `kiruna` as capacity allows. This avoids
queuing preparation across all qualified site/years when storage is constrained;
it does not alter the full study scope or frozen matrix. `--phase sources` pins
inputs separately. The corrected directory
is `output/weather-imputation-campaign-2026-10-05-v5`, with saved runtime/packages,
source and panel receipts, validated weather, Core models, compressed SQL,
hourly loads, comparison CSVs and coverage reporting. The study harness is frozen
at EPSM commit `6ce7f10`, following `93d5959`. Smhi2epw remains at `5930c07`, which
includes the `3bb5a87` recovery hierarchy and `5930c07` cloud-source/unit
corrections; Core and EnergyPlus are unchanged. **245 tests passed: 182
frozen-harness, one additional disk-guard test, eight separate analysis, 30
archival and 24 final-report tests**; Ruff was clean for the owned code/test
files. Exact hashes and
execution progress belong to the saved runtime and `full-campaign.log`.

The interrupted **PROVISIONAL** snapshot contains 1,792 raw building rows,
including 18 consumed reference rows across the three Malmö regional years;
1,558 deduplicated non-reference comparisons; 354 paired hierarchy responses;
and 336 forced-method common-cohort responses. No site/year has completed the
full case matrix. Verified tables and four PNG/SVG figures are saved in
`output/weather-imputation-campaign-2026-10-05-v5/provisional-analysis`, separately
from the eventual final analysis. They do not establish the campaign's overall
method ranking or completed geographical coverage.

V4 remains partial diagnostic evidence. Its forced ERA5/Erbs solar path omitted
the optional all-NaN measured-radiation column on STRÅNG-only originals, causing
solar cleanup to fail. Production already prepares that column. The research
harness now does the same, with regression checks for absent/all-NaN equivalence,
unchanged unmasked fields and no invented measurement. That driver change
requires fresh v5 evidence, with no smhi2epw production change. Accepted v4
simulations may be reused only after identical regenerated EPW bytes, model
hashes and SQL metrics are verified under the new Core specification/run
identity; migration provenance retains the unchanged original receipts.

Native execution with `TZ=UTC0` mitigates a separately diagnosed macOS
timezone-service stall in the EnergyPlus process; the initial `TZ=UTC` still
allowed intermittent startup waits. The EPW/SQL weather clock remains fixed UTC+1
local standard time. Recovery evidence and quarantined incomplete attempts are
retained; these are not accepted comparisons.

The campaign reports empirical demand/peak error tails and event preservation,
without treating repeated masks or buildings as independent climates. Optional
clouds are frozen, so production's scoped ERA5 cloud effect is not measured.
The ground-facing floor uses Core's prescribed 18°C boundary throughout, an
uncalibrated conditioned-floor assumption without regional/seasonal soil or
snow physics. The frozen source plan qualifies 17/18 references under unchanged
gap limits; Kiruna 2018 remains unsupported within the screened 100 km area.
The campaign note gives year-specific primary/donor/GHI station identities and
city distances, partial-donor limitations and five STRÅNG-based solar proxies.
V5 has upper bounds of 56 panels and 210 non-reference weather comparisons per
site/year, or 22,788 paired building rows including 108 references across the
requested matrix. Event absence, source/method qualification and simulation
reuse change actual counts. The launch inventory from 17 qualified references
contains **904 panels: 882 ready and 22 unsupported**, with 62 available weather
anchors. Its 191 repeated scope/duration/mask combinations retain separate panel
labels but do not provide independent weather evidence. Ready panels do not
establish completed reconstructions or accepted building runs.
A helper outside the frozen runtime deduplicates identical physical masks for
analysis, retaining anchor-label mappings and checking duplicated responses
before giving each physical outage one weight per building/method/sampling
stratum. Labelled-case summaries remain
available; the helper does not change scientific results or receipts.

V5 retains v4's tested lossless SQL/hourly storage. Reference SQL uses self-contained
XZ preset 3; candidate SQL uses standard Zstandard 1.5.6 level 12 with
`--patch-from` the same building/site/year's immutable reference SQL dictionary,
recoverable from XZ. Compressed/uncompressed/dictionary hashes are verified
before consumption, and the decoder binary/version is pinned. Hourly CSVs use
gzip level 6 and separate archive receipts, preserving their logical Core paths;
identical models use SHA-verified hardlinks. Atomic crash recovery and a 2 GiB
free-disk guard permit resumption of the immutable matrix. Auxiliary traces are
removed; the campaign note documents the tested storage scheme.
A separate utility outside the frozen runtime losslessly archives candidate
EPWs as gzip level 6 only for audited complete site/years, with checked summary
and building/case coverage. It verifies hashes, preserves baseline EPWs and
original receipts, and adds `weather-archive.json`. Restore exact bytes with
`gzip -dk weather.epw.gz`; retain the archive and sidecar. Incomplete site/years
are skipped, so storage support does not imply completed campaign coverage.
After the runner stopped, a separate verified storage operation also archived
already prepared candidate EPWs with no active simulations. Its interruption
receipt preserves exact hashes without marking the campaign complete.

Outside the frozen runtime, independent analysis and the plotter produce paired
tables and four PNG/SVG figures. Partial checkpoints require `--provisional` and
are labelled. A separate final-report publisher verifies complete coverage,
audits and analysis/figure/input hashes before writing the synthesis; it refuses
incomplete evidence. Available tools and provisional output do not establish a
completed campaign.

The v1/v2 launch diagnostics are excluded from accepted comparisons. V3 remains
a completed precursor with its frozen runtime and receipts; source qualification
and pilot performance do not establish completed v5 coverage or universal method
accuracy. Use each version's saved driver/packages and coverage report for its
accepted results.
