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
describes the forthcoming **v4: planned, not yet run or complete**, across six
geographical strata × three years × six buildings. The completed v3 stage-one
pilot is retained separately: Gothenburg 2023, all six profiles, four-season
meteorological outages of 48/168 hours and seed 11. It has 60 paired rows,
36 unique annual simulations and zero warnings/severe/fatal errors.

At 48 hours, the new priority improves hourly heating-load MAE for 6/6 profiles
and cooling MAE for 4/6; absolute annual-energy error improves for only 1/6 in
each mode. At 168 hours the priorities tie. The pilot masks cover zero annual
load-peak and absolute heatwave/cold-spell hours, making unchanged peaks/spells
uninformative about preservation during those events.

The wider matrix remains planned coverage, separate from both that pilot and
the original completed Gothenburg study. Station
metadata fixes each reference's actual exposure and coordinates; 2018, 2021 and
2023 are deliberately contrasting years, not a random climate sample. Stations
can differ between years, preventing causal attribution of inter-year changes
solely to climate. The six synthetic EPSM Core
profiles include detached and multifamily homes, an office and a school, without
measured-building calibration.

V4 panels use 3/12/48/168-hour outages, seasonal seeds 11/29/47 and separate
warmest/coldest original annual-temperature anchors. Optional detected absolute
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
../epsm_core/.venv/bin/python -m examples.weather_imputation_sensitivity.run_campaign \
  --phase plan --output output/weather-imputation-campaign-2026-10-05-v4

../epsm_core/.venv/bin/python -m examples.weather_imputation_sensitivity.run_campaign \
  --phase run --select-sites gothenburg --select-years 2023 \
  --output output/weather-imputation-campaign-2026-10-05-v4 \
  --select-buildings detached_old_light,office_glazed \
  --select-durations 3,48,168 --select-seeds 11 \
  --select-scopes meteorology --sampling random_seasonal --workers 1
```

These commands describe the forthcoming v4 driver once finalized.
`--phase sources` pins inputs separately. Selective runs accumulate within the
same output matrix; expand coverage progressively. The intended final directory
is `output/weather-imputation-campaign-2026-10-05-v4`, with saved runtime/packages,
source and panel receipts, validated weather, Core models, compressed SQL,
hourly loads, comparison CSVs and coverage reporting.

The campaign reports empirical demand/peak error tails and event preservation,
without treating repeated masks or buildings as independent climates. Optional
clouds are frozen, so production's scoped ERA5 cloud effect is not measured.
The ground-facing floor uses Core's prescribed 18°C boundary throughout, an
uncalibrated conditioned-floor assumption without regional/seasonal soil or
snow physics. The frozen source plan qualifies 17/18 references under unchanged
gap limits; Kiruna 2018 remains unsupported within the screened 100 km area.
The campaign note gives year-specific primary/donor/GHI station identities and
city distances, partial-donor limitations and five STRÅNG-based solar proxies.
V4 has upper bounds of 56 panels and 210 non-reference weather comparisons per
site/year, or 22,788 paired building rows including 108 references across the
requested matrix. Event absence, source/method qualification and simulation
reuse change actual counts. Additional lossless storage improvements are under
development; no final implementation is claimed here yet.

The v1/v2 launch diagnostics are excluded from accepted comparisons. V3 remains
a completed precursor with its frozen runtime and receipts; source qualification
and pilot performance do not establish completed v4 coverage or universal method
accuracy. Use each version's saved driver/packages and coverage report for its
accepted results.
