# MF SE.02/F archetype calibration pilot

This workflow freezes one Gothenburg cohort before any simulated or reported
test-set outcome is inspected. It uses the promoted `epc_join` 2.0.0 links,
newer raw EPC exports, the building-register year, the promoted height release,
`epsm_data` LoD2 v1.3, and `epsm_core`/EnergyPlus 24.2. Source repositories are
read-only. Study output must be a new directory.

## Run

Install the EPC dependencies of `epsm_workflows`, `epsm_core`, and `EPSM_data` in
one Python environment. Include the local `smhi2epw` checkout when generating
weather. The following commands use sibling source checkouts explicitly:

```sh
export PYTHONPATH="$PWD/src:$PWD/../epsm_core/src:$PWD/../EPSM_data/src:$PWD/../smhi2epw/src"
PILOT_PYTHON="$PWD/../epsm_core/.venv/bin/python"
STUDY=/path/to/new-mf-se02-study
WEATHER=/path/to/new-mf-se02-weather
ENERGYPLUS=/path/to/EnergyPlus-24-2-0/energyplus

"$PILOT_PYTHON" -m epsm_workflows.archetype_pilot prepare \
  --publication ../epc_join/outputs/releases/2.0.0 \
  --epc-root ../epc_join/data/epc \
  --registry ../building_age/outputs/national/registry.sqlite \
  --height-release ../footprint_height_extraction/outputs/sweden/harmonized/1.0.0 \
  --height-selection ../footprint_height_extraction/outputs/sweden/inventory/selections/gothenburg.buildings.parquet \
  --output "$STUDY"

"$PILOT_PYTHON" -m epsm_workflows.archetype_pilot weather --study "$STUDY" --output "$WEATHER"
"$PILOT_PYTHON" -m epsm_workflows.archetype_pilot smoke --study "$STUDY" --weather-dir "$WEATHER" --energyplus "$ENERGYPLUS"
"$PILOT_PYTHON" -m epsm_workflows.archetype_pilot baseline --study "$STUDY" --weather-dir "$WEATHER" --energyplus "$ENERGYPLUS"
"$PILOT_PYTHON" -m epsm_workflows.archetype_pilot calibrate --study "$STUDY" --weather-dir "$WEATHER" --energyplus "$ENERGYPLUS"
"$PILOT_PYTHON" -m epsm_workflows.archetype_pilot evaluate --study "$STUDY" --weather-dir "$WEATHER" --energyplus "$ENERGYPLUS"
```

`weather` downloads source observations through `smhi2epw` and produces an
actual-year EPW for each selected calendar year. Review its per-year provenance
before `baseline`. `baseline` pins all weather checksums; later stages refuse
changed weather. The compiler and EnergyPlus executable must match version 24.2.
The `smoke` stage runs three non-test buildings in all three states, including
flat and pitched roofs when both have local weather. It never touches test.
If the nearest SMHI station fails the weather gap check, rerun `weather` with
`--station YEAR:ID` for a documented nearby station. The 2020 and 2024 EPW
holiday headers declare their leap days for EnergyPlus; the source EPW hash and
adjusted hash are both retained in the weather provenance.

## Cohort and safeguards

`cohort.json` has up to 200 real, one-building EPC observations. The preparer
requires one certificate per building, one accepted footprint for the
certificate, a 1961–1975 construction year, only F ventilation, measured rather
than calculated district heat, 12 January–December months, no declared mixed
carrier or solar thermal, positive Atemp and space heat, and a supported height
and roof form. It uses the register year when available and rejects a conflict
with the archetype era. It requires positive finite measured heights and rejects
footprint × storeys / Atemp outside 0.75–1.25. All reasons are recorded in
`exclusions.json`. Energy intensity does not determine the sample or split.
Core geometry is resolved for all three catalogue states before sampling.
Original polygonal outlines, including irregular pitched plans, are preserved.
The former rectangular proxy and 2.4–3.8 m rejection gates were removed after
comparison with current Core geometry support. Existing frozen studies retain
their original preparation policy. The national runner resolves native geometry
before weather compilation and flags unusual floor heights for review rather
than rejecting them. Unresolved roof forms still require source evidence.

One property stays in one split. The deterministic target is 120 train, 40
validation and 40 test buildings when 200 are selected. Fewer than 120 selected
buildings stop calibration after baseline. `test_report.json` is created once,
only after `calibration.json` freezes all parameter choices. Runs are content
addressed and leave the compiler, EnergyPlus and diagnostic artifacts intact.
`test_report.md` gives the baseline and final headline metrics and delivery-loss
sensitivity; the JSON holds subgroup metrics, quantiles, intervals and the
per-building predictions.
Baseline simulation failures are recorded and their property groups omitted
from fitting without consulting reported heating. The original selected cohort
remains in `cohort.json`; `baseline.json` lists the analysis building IDs and
all simulation failures. Calibration stops if fewer than 120 buildings remain.
If a test simulation fails, the report compares baseline and final on the same
complete property groups and gives both the planned and analyzed counts with
the failure diagnostics. Failed test buildings are never replaced.

The frozen catalogue baseline simulates Existing, Standard and Advanced for
each building. The primary no-fit comparison uses an equal-weight mean of
those states; each pure state is also reported. Training EPC heat determines
only a **group-level predictive
state mixture**. The workflow never assigns a true renovation state to an
individual building. With the mixture fixed, it selects wall insulation, top
insulation, then mechanical F flow on the validation split. Only the largest
resistance insulation layer in each role changes thickness. All material
identities, ground construction, window properties and catalogue infiltration
remain fixed. Existing uses 0.4 ACH; Standard and Advanced use 0.2 ACH.

The comparison target is EPC-reported delivered district-heating space heat per
reported Atemp. The simulated thermal demand is converted with fixed efficiency
1.0 and an assumed 5 kWh/m² net loss. Final test metrics repeat the comparison
at 0 and 10 kWh/m². These are study assumptions, not Boverket defaults for space
heating. Boverket's 1.0 default is for district-heat **hot-water production**.
[BEN Table 3:2](https://www.boverket.se/Resources/constitutiontextstore/BEN/xml/Konsoliderad_BEN_BFS_2016_12.pdf)
Actual-year weather does not reproduce every EPC normal-use adjustment.

Flat roofs use LiDAR representative height and compact topology. Supported
pitched roofs use eave height, a gable approximation and an unconditioned attic.
The attic is sealed at 0 ACH in this pilot because Core has no attic exchange
input on `BuildingSpec`; this assumption and the roof approximation are reported
limits, not calibrated parameters. The bundled Sveby residential profile supplies
normal-use schedules; its gains and hot-water intensity are scaled from EPC
Atemp to Core conditioned floor area. Ground temperature retains Core's
uncalibrated 18 °C boundary and is not fitted.

The pilot demonstrates a city-specific out-of-sample comparison. Annual heating
alone cannot identify actual material layers, renovation histories or ventilation
flows. A lower test error supports predictive usefulness under the fixed
assumptions, not physical identification of every component.

## Weather exporter calendar fix

`smhi2epw` now writes the leap-day declaration and January 1 weekday for the
actual measurement year and rejects contradictory calendar headers. The pilot
copies the compiler artifact and provenance unchanged; its local header patch
has been removed. Existing pinned weather remains unchanged. To regenerate
weather with the updated exporter, use a fresh output directory and freeze new
hashes for a new study rather than replacing historical weather artifacts.
