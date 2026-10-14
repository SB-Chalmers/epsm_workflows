# MF SE.02 building-model diagnostics

This workflow checks the energy comparison and physical assumptions before any
further calibration or clustering. It never fits parameters or labels buildings
using heating energy. The old train, validation and test groups are all diagnostic.
Every original property is excluded from any future evaluation holdout.

## Run

Use EnergyPlus 24.2 and the sibling Core virtual environment, with the Data and
smhi2epw source packages available. Set paths to a completed pilot and a **fresh**
diagnostic directory:

```bash
export PYTHONPATH=src:../EPSM_data/src:../smhi2epw/src
PILOT_PYTHON=../epsm_core/.venv/bin/python
STUDY=output/archetype-pilot-mf-se02-f-2026-09-30/study
WEATHER=output/archetype-pilot-mf-se02-f-2026-09-30/weather
DIAGNOSTICS=output/archetype-diagnostics-mf-se02-2026-10-01-reviewed
ENERGYPLUS=/Applications/OpenStudio-3.9.0/EnergyPlus/energyplus-24.2.0

"$PILOT_PYTHON" -m epsm_workflows.archetype_diagnostics prepare \
  --study "$STUDY" --weather-dir "$WEATHER" \
  --epc-root ../epc_join/data/epc --output "$DIAGNOSTICS"
"$PILOT_PYTHON" -m epsm_workflows.archetype_diagnostics simulate \
  --output "$DIAGNOSTICS" --energyplus "$ENERGYPLUS" --stage smoke
"$PILOT_PYTHON" -m epsm_workflows.archetype_diagnostics simulate \
  --output "$DIAGNOSTICS" --energyplus "$ENERGYPLUS" --stage experiments
"$PILOT_PYTHON" -m epsm_workflows.archetype_diagnostics simulate \
  --output "$DIAGNOSTICS" --energyplus "$ENERGYPLUS" --stage full
"$PILOT_PYTHON" -m epsm_workflows.archetype_diagnostics report --output "$DIAGNOSTICS"
```

Optional weather sensitivities on smoke/experiments:

- `--tmy PATH`: a separately labelled typical-year run using a non-leap reference
  calendar (2023). It is not a replacement for measurement-year AMY and its error
  is not a weather-selection criterion.
- `--alternate-weather YEAR:PATH`: an independently compiled AMY file with adjacent
  `weather-provenance.json`. Its hash, calendar and distance (at most 50 km) are
  checked; station/filling evidence remains in the provenance file. The actual
  diagnostic run compared Göteborg A with Landvetter for 2018, retaining the same
  radiation station and target coordinates. Both files include disclosed gap fills.

Receipts are reusable only for identical specifications, workflow source and
variant. Do not overwrite failed runs; use a new diagnostic directory after a
prototype or source change. Smoke and experiment gates reject compilation or Kiva
failures. At the full-cohort gate, buildings failing all three original states
remain explicit failed cases; they do not count as failures introduced by Kiva.
Any Kiva failure on an otherwise usable baseline still blocks that gate.

`--reuse-runs DIRECTORY` can import earlier diagnostic runs after recompilation
verifies the same specification, epJSON, Core compiler, engine and schema hashes.
Original artifacts and import checksums remain recorded. This avoids rerunning
identical physics after correcting reporting metadata or gate bookkeeping.

For faster SQL validation in large studies, the optional process adapter requires
`cloudpickle` and accepts the same CLI arguments:

```bash
"$PILOT_PYTHON" examples/epc_validation/run_diagnostics_processes.py simulate \
  --output "$DIAGNOSTICS" --energyplus "$ENERGYPLUS" --stage full --workers 8
```

The adapter records its source hash and execution arguments. It changes execution
parallelism only; model inputs and strict result checks are identical.

## Evidence and experiment boundaries

- `EgiFjarrvarmeUPPV` is delivered district heat for heating, entered after normal-use
  adjustment and before automatic normal-year correction. Version evidence is
  recorded for 2.6, 2.7, 2.8 and 3.0; unknown versions are excluded. `EgiVersion`
  is preserved separately. Final EPC total energy and primary energy are different
  quantities and are not substituted for heating.
- The supplied Boverket mapping says `EgenAntalPlan` is **above-ground** storeys.
  `EgenAntalKallarplan` counts basement floors **heated above 10°C**. Thus the 56
  positive records establish heated-basement presence. A zero does not establish
  the absence of an unheated basement. This workflow follows the reported count.
- Geometric area remains gross footprint area. Internal Atemp is not inferred by
  shrinking a footprint to fit energy. Occupancy, equipment, DHW and mechanical
  flow totals remain tied to EPC Atemp; the separate lighting experiment fixes its
  previously omitted area scaling. Adding a basement preserves those whole-building
  totals. Residential gains are uniformly allocated: basement-specific use is still
  unknown and is not inferred from residuals.
- Compare fixed basement depth 2.5 m with 3 m, and temperature 21°C with 15°C.
  These are uncertainty scenarios, not measured inputs. Both remain heated.
- Pitched attics remain unconditioned. Exchange at 1 and 2 ACH is compared with
  the old sealed assumption, independently of the catalogue infiltration. Heated
  attics cannot be identified or reliably represented by this roof topology.
- Kiva variants now use Core's public `FoundationSpec`, with explicit soil inputs
  and a zero-flux boundary at 40 m. Historical baseline variants explicitly select
  `GroundTemperatureSpec()` to retain their declared 18°C boundary. The local Kiva
  adapter has been removed; attic and thermostat experiments remain local.
  Conductivity 1.0/1.73/2.5 W/m/K is a sensitivity. Soil and groundwater are unmeasured.
  Basement walls retain the catalogue construction. No footing block is added unless
  explicitly supplied; EnergyPlus supplies its default below-slab foundation wall.
  Exterior perimeter assumes isolated foundations and excludes internal zone edges.
  Coplanar floor tessellation is merged without changing area; Core validates and
  reports the resulting surfaces. Archived diagnostic artifacts retain their original
  source hashes and adapter outputs; they are not retroactively rewritten.
- Supply-air ideal heating already includes outdoor-air conditioning. Additional
  zone, outdoor-air and recovery outputs trace that accounting. Heating distribution
  and VVC are not explicitly simulated. Under BEN, VVC losses are assigned to
  heating; recovered indoor pipe heat must offset room demand. Add 0/5/10 kWh/m²
  **net** delivered losses only as uncertainty, never as per-building fitted values.
- Window fraction remains the original assumed 20%. Catalogue glazing, assembly
  materials, thicknesses, infiltration and thermal-bridge settings stay fixed.
  These assumptions are audited rather than optimized in this workflow.

## Outputs and decision

`audit.json` preserves field/version evidence, area ratios, source hashes, weather
quality, sampling and excluded future-holdout properties. `comparison.md` states
the target boundary. Run directories preserve epJSON, Core specifications,
compilation reports, SQL, diagnostics, failure records and energy totals.

`report.md` is the plain-language summary. `report.json` contains individual and
state-specific errors, paired before/after metrics, property bootstrap intervals,
loss sensitivities and breakdowns by roof, basement, measurement year, size and
height. Full-cohort results and small-cohort experiments are both retained.

Corrections supported by evidence (reported basements and lighting totals) are
distinguished from assumptions (basement temperature, attic exchange, soil,
internal area and delivered losses). Lower error does not establish physical truth.
Calibration and clustering remain blocked while the area and delivered-energy
boundary are unresolved. No old test performance is labelled out-of-sample evidence.

## Sources

- Supplied Boverket `Mappningsfil_efter_v260.xlsx` and version 2.8 form; paths and
  file hashes are retained in the study.
- [Boverket: measured energy and normalization](https://www.boverket.se/sv/energideklaration/for-energiexperter/berakning-av-byggnadens-energianvandning/energiprestanda-baserat-pa-matning/)
- [BEN, including VVC allocation](https://www.boverket.se/Resources/constitutiontextstore/BEN/xml/Konsoliderad_BEN_BFS_2016_12.pdf)
- [EnergyPlus 24.2 Kiva documentation](https://bigladdersoftware.com/epx/docs/24-2/input-output-reference/group-advanced-surface-concepts.html#foundationkiva)
- [EnergyPlus 24.2 Ideal Loads outputs](https://bigladdersoftware.com/epx/docs/24-2/input-output-reference/group-zone-forced-air-units.html#zone-ideal-loads-supply-air-total-heating-energy-j)
