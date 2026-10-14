# One-at-a-time parameter comparisons

This extends the window-only check to a fixed matrix of separate experiments.
Use the same 12 buildings selected by roof, basement, size, height and measurement
year; run existing, standard and advanced independently. WWR 0.2, AMY, Kiva,
normal-use schedules, reported heated-basement presence and EPC energy boundary
remain the common starting point.

| Parameter | Separate settings |
|---|---|
| Wall insulation thickness | 0.8 / 0.9 / 1.1 / 1.2 × catalogue layer |
| Roof/top-floor insulation thickness | 0.8 / 0.9 / 1.1 / 1.2 × catalogue layer |
| Mechanical F ventilation | 0.30 / 0.40 L/s/m² EPC Atemp (baseline 0.35) |
| Background infiltration | 0.2 / 0.3 / 0.4 ACH; omit each state's unchanged setting |
| Window U | State-specific TABULA references: 2.22 / 0.90 / 0.76 W/m²K |
| WWR | 0.30 (baseline 0.20) |
| Soil conductivity | 1.0 / 2.5 W/mK (baseline 1.73) |
| Thermal-bridge uplift | 10% / 20% (baseline zero) |
| Occupant gains | Density × 0.8 / 1.2, activity and schedule fixed |
| Household equipment gain | W/m² × 0.8 / 1.2 |
| Heated basement temperature | 15°C (baseline 21°C), basement-only thermostat |
| Basement depth | 3 m (baseline 2.5 m) |
| Cold-attic exchange | 1 / 2 ACH (baseline sealed), attic only |

Material identities stay fixed. Insulation tests modify one layer thickness;
thermal-bridge tests use Core's equivalent-construction implementation. The
basement/attic experiments use schema-checked, explicitly recorded compiler
patches. Core's ground model is used directly. Basement depth also changes volume
and derived ACH flow, while total gains and mechanical flow remain unchanged.

Unmeasured assumptions are uncertainty scenarios. Infiltration is tested to
understand its influence and is excluded from residual-driven calibration. Do not
promote lower-error basement, soil, gains or thermal-bridge assumptions without
evidence. Do not resize footprints or invent heated attics/unheated basements to
fit EPC Atemp. Weather selection and operating schedules remain fixed.

```sh
PYTHONPATH=src:../EPSM_data/src:../smhi2epw/src \
  ../epsm_core/.venv/bin/python -m epsm_workflows.parameter_sensitivity \
  --source output/wwr-sensitivity-mf-se02-2026-10-01 \
  --output output/parameter-sensitivity-new-study \
  --energyplus /Applications/OpenStudio-3.9.0/EnergyPlus/energyplus \
  --workers 8
```

The study pins package/workflow/weather hashes and grids, verifies identical
physical baseline specifications before reusing receipts, precompiles
representatives of every parameter/state/geometry type, and runs a smoke building
before the matrix. Every case retains compilation, diagnostics, engine output and
failures. The 12 buildings remain diagnostic; the previously known two warning
buildings are not silently replaced.

Compare each setting with baseline on exactly the same successfully paired
buildings. Report signed/absolute error, MAE, median percentage error, maximum
absolute error, counts within 10%/20%, and how many buildings improve/worsen.
Repeat at delivery-loss allowances 0/5/10 kWh/m². Basement/attic subsets cannot be
ranked against whole-sample results using their raw MAE.

`per-building-comparisons.json` is the complete per-building matrix;
`summary.json` and `report.md` expose changes that help averages but hurt buildings.
No building-specific best-model selection is used. A shared combination has to be
simulated jointly; improvements cannot simply be added. Any exploratory combined
candidate remains diagnostic and needs future verification on fresh properties.

## Combined shared candidate check

After the separate matrix completes, `shared_candidate` selects wall/roof
multipliers, window U and mechanical flow using **only the six original-training
buildings** in the diagnostic sample, at the fixed 5 kWh/m² loss allowance. Ties
retain the previous setting. Selection applies the same settings to every building
within each frozen state. No residual-based infiltration selection is permitted;
WWR, gains, thermal bridges, soil and basement/attic assumptions also stay fixed.

```sh
PYTHONPATH=src:../EPSM_data/src:../smhi2epw/src \
  ../epsm_core/.venv/bin/python -m epsm_workflows.shared_candidate \
  --sensitivity output/parameter-sensitivity-mf-se02-2026-10-01 \
  --baseline output/wwr-sensitivity-mf-se02-2026-10-01 \
  --output output/shared-candidate-new-study \
  --energyplus /Applications/OpenStudio-3.9.0/EnergyPlus/energyplus \
  --workers 8
```

The shared settings are frozen before running all 126 buildings. Every candidate
is actually simulated jointly, rather than adding separate sensitivity deltas.
Report paired per-building errors and overall/split-specific MAE, bias, percentage
errors, maximum error and 10%/20% coverage at 0/5/10 loss allowances. The old
validation/test properties have already been inspected; they provide diagnostic
confirmation, not untouched validation. This is an exploratory combination test,
not acceptance of a calibrated catalogue or evidence of national transfer.
