# AMY completion sensitivity experiment

## Read the uncertainty results

The [current reader report](../../output/weather-imputation-campaign-2026-10-05-v5/analysis-reviewed-warnings/paper.html)
shows the size of the error introduced by automatic completion, with plain-language
figures for annual demand, exposed peak loads, peak-hour differences and weather
events. Tested 3- and 12-hour gaps have annual-demand 95th-percentile errors below
1%; week-long sunshine gaps produce larger cooling uncertainty. Near-zero cooling
references are excluded from percentage summaries and retained in absolute errors.
See the [campaign note](../../docs/weather-imputation-campaign.md) for denominators,
limitations and reproducible postprocessing. Earlier presentation artifacts remain
archived; the weather and building simulations have not been changed.

## Current expanded analysis, 6 October 2026

The revised [paper and four current-only summary plots](../../output/weather-imputation-campaign-2026-10-05-v5/analysis-reviewed-warnings/paper.html)
retain accepted transient psychrometric warnings. It includes **4,146 paired
building comparisons / 691 outage masks / 17 reference weather-years / six
regions**, with all 102 reference combinations available. Kiruna 2018 remains
source-unavailable. The [campaign note](../../docs/weather-imputation-campaign.md)
explains the bounded warning policy, Core's original annual-heating scope,
additional hourly sensible-load reconciliation and current reproduction commands.
No simulations or frozen inputs were changed. **427 weather-study tests pass.**

The historical experiment and initial expanded warning-exclusion analysis below
are preserved for provenance. Their 12 weather-year / 2,916 automatic-comparison
counts are superseded for the current paper.

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

The historical command requires the original saved reproduction directory and
sources to be present. Restore them from the historical archive first if they
have been moved out of the working output directory.

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

Fast synthetic checks for the **current generalized harness**, using the
current sibling smhi2epw checkout rather than the historical package snapshot:

```sh
PYTHONPATH="$PWD/../smhi2epw/src" \
  ../epsm_core/.venv/bin/python -m pytest \
  tests/test_weather_sensitivity.py tests/test_weather_anomalies.py -q
```

The study demonstrates sensitivity for one illustrative building/year and four
selected windows. It does not establish local weather accuracy or calibrate the
building. The urban donor and airport reference have different exposure and
elevation; see the study note before interpreting small annual differences.

## Expanded Swedish campaign

The **v5 execution and numerical analysis are complete**. The
[campaign methods, results and reproduction note](../../docs/weather-imputation-campaign.md)
documents the six-strata × three-year × six-profile design, source selection,
qualification and retained evidence. Seventeen source-ready weather years are
completed; Kiruna 2018 remains unsupported. Engine-warning screening leaves
building results for **12 site-years in five strata**, with **72 qualified
references**. Weather/events retain 17 site-years across all six strata. The
[final synthesis](../../output/weather-imputation-campaign-2026-10-05-v5/synthesis.html)
and [publication receipt](../../output/weather-imputation-campaign-2026-10-05-v5/synthesis-receipt.json)
passed provenance verification. The
[current-only paper](../../output/weather-imputation-campaign-2026-10-05-v5/paper-summary.html)
presents automatic completion against reference with four summary figures.

Raw output contains **18,366 labelled building rows from 9,450 unique canonical
engine executions**. Whole-run screening retains 13,104 rows and excludes 5,262;
it removes all outcomes of warned runs, despite Core allowing annual heating
alone for the observed wet-bulb warnings. Unqualified baselines cascade to their
associated comparisons. All building years for Östersund 2021/2023, Luleå 2021
and Kiruna 2021/2023 are excluded. Original diagnostics and valid weather/event
evidence remain. The raw campaign is not warning-free, and no Kiruna building
conclusion is supported.

Current automatic versus reference analysis contains **2,916 qualified building
responses for 486 distinct physical masks**: 1,452 meteorological and 1,464 solar
responses. Weather-only automatic evidence contains 691 masks (343 meteorology
and 348 solar). All 882 truth-ready named panels reconstruct successfully;
label aliases are not independent evidence. For random meteorological panels,
pooled median/p95 absolute annual heating errors rise from 0.00105/0.00845
kWh/m² at 3-hour blocks to 0.0816/0.886 kWh/m² at 168-hour blocks. These are
descriptive scenario quantiles, not confidence intervals or guaranteed bounds.
The main note provides cooling, solar, exposure and event denominators.

Durations **3/12/48/168 hours are per block**. Seasonal random panels contain
four blocks, totalling 12/48/192/672 missing hours/file, with seeds 11/29/47.
Weather-event panels contain one block, anchored independently of building
loads to original warmest/coldest temperatures and optional detected absolute
heatwave/cold-spell events. Every method/profile shares a mask's original
observed truth; reconstruction contexts exclude all hidden observations,
including through native filling. Random/event error magnitudes have different
exposure budgets. Paired method comparisons remain within the same stratum.

Automatic meteorology follows short interpolation → assessed donor → bounded
daily profiles → ERA5. Solar is donor-free. The archived full study also retains
legacy-priority and forced-method comparisons; these are distinct from the
concise automatic-versus-reference paper. Their 12-/48-hour meteorological
heating benefits do not establish universal cooling/peak improvement or
heatwave preservation. Station exposure can differ between years; the three
purposeful years and six synthetic profiles are not calibrated or independent
samples of Swedish climate/buildings. The prescribed floor boundary is 18°C
for all cases; optional clouds remain frozen across controlled methods.

The frozen scientific identities are EPSM harness `6ce7f10` and smhi2epw
`5930c07` (including hierarchy `3bb5a87`). Core and EnergyPlus 24.2 are unchanged.
All 14 scientific driver files and 77 package files remain pinned. The final
consolidated weather-study suite passed **390 tests**, and Ruff was clean on
17 owned Python files. Exact source/runtime identities, qualification,
independent audits and the seven PNG/SVG campaign figures are linked in the
[campaign note](../../docs/weather-imputation-campaign.md#figures-and-retained-evidence).

The current-only summary set is available as four PNG/SVG/PDF figures with
source CSVs: [annual errors](../../output/weather-imputation-campaign-2026-10-05-v5/analysis/summary-figures/automatic-annual-errors.png),
[peak errors](../../output/weather-imputation-campaign-2026-10-05-v5/analysis/summary-figures/automatic-peak-errors.png),
[exposed peak timing](../../output/weather-imputation-campaign-2026-10-05-v5/analysis/summary-figures/automatic-peak-timing.png)
and [weather-event preservation](../../output/weather-imputation-campaign-2026-10-05-v5/analysis/summary-figures/automatic-weather-events.png).
The campaign note links each vector/PDF format, CSV and hash-bound manifest.
Generate these summaries with
`python -m examples.plot_weather_campaign_summary CAMPAIGN/analysis` in the
EPSM environment, replacing `CAMPAIGN` with the complete campaign directory.

Resume the saved campaign natively from the repository root:

```sh
TZ=UTC0 PYTHONDONTWRITEBYTECODE=1 ../epsm_core/.venv/bin/python -u \
  -m examples.resume_weather_imputation_campaign \
  --output output/weather-imputation-campaign-2026-10-05-v5 --processes 3
```

The scheduler locks the controller, verifies completed work, runs at most three
one-site/year children with `--workers 1`, and serializes global reporting after
they join. `TZ=UTC0` prevents a macOS timezone-service startup stall; EPW/SQL
weather remains fixed UTC+1 local standard time. Completed work is skipped
only after identity checks. Manual site/year batches use the original runner's
`--phase run --select-sites malmo --select-years 2018 --workers 1`; there is no
`--phase report` option. Scientific identity changes require fresh output.

All SQL and hourly loads are retained losslessly. Reference SQL uses XZ and
candidate SQL uses SHA-verified Zstandard patches against the exact reference
dictionary. Hourly CSVs and completed candidate EPWs use lossless gzip and
archive sidecars. The 3,044 EPW archives store 5.109 GB of original bytes in
0.574 GB. Baseline weather, immutable receipts/models, error logs and source
frames remain available. Archive after all workers join and **before** final
qualification/analysis; changing plain/archive representation later requires
regenerating all postanalysis and publication receipts. The 2 GiB free-disk
guard preserves completed work without reducing the intended scope.

With workers finished and raw coverage verified, reproduce postanalysis using
the appropriate EPSM Python environment:

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

Final commands omit `--provisional` and require matching completed evidence.
The raw `report.html` is the diagnostic catalogue; the final synthesis and its
receipt passed verification. The
[latest analysis-bound audit](../../output/weather-imputation-campaign-2026-10-05-v5/analysis-audits/independent-final-analysis-bound-audit-index-20261005T163822Z.json)
records final independent provenance. V3
remains a separate completed stage-one pilot, and v1/v2/v4 retain excluded or
partial diagnostic identities. The first Gothenburg study above must use its
own saved reproduction driver and pinned sources, not these generalized
modules.
