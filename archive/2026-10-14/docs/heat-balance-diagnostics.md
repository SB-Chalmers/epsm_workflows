# Individual errors and heat-balance diagnostics

Run after the paired WWR study, using the frozen WWR 0.2 receipts:

```sh
PYTHONPATH=src:../EPSM_data/src:../smhi2epw/src \
  ../epsm_core/.venv/bin/python -m epsm_workflows.heat_balance_diagnostics \
  --source output/wwr-sensitivity-mf-se02-2026-10-01 \
  --output output/heat-balance-new-study \
  --energyplus /Applications/OpenStudio-3.9.0/EnergyPlus/energyplus \
  --workers 8
```

The workflow records each state separately across the full cohort, including
signed and absolute errors at delivery-loss allowances 0/5/10 kWh/m². Twelve
buildings are selected deterministically by known attributes, without inspecting
heating observations. Run all three states and one existing-state window-U
sensitivity (2.8 → 2.22 W/m²K). Preserve solar gain coefficients, WWR 0.2,
materials, infiltration, geometry, normal-use inputs and AMY weather.

Core's compiler settings request hourly outputs. An output-only compiler extension
exposes the advanced zone-air imbalance variable and validates the resulting
schema. No ground adapter or warning waiver is used. The first case is a smoke
gate. Additional outputs must reproduce the frozen baseline heating exactly.

The closed zone-air budget includes internal convection, surface convection,
outdoor-air transfer, interzone transfer, system air, system convection and air
storage. Integrate hourly watts as Wh; integrate joules as joules. Exclude warmup
and sizing periods. Positive zone-air terms warm the air. Reverse EnergyPlus's
inside-face convection sign, which is positive into the surface, before grouping
by component. Record residuals and differences between independently integrated
surface and zone outputs.

Opaque inside-face conduction, window gain/loss, transmitted solar, total people
and equipment gains, and ventilation-coil heating are separate diagnostics. They
overlap and are not summed into an invented delivered-heat balance. Mechanical
outdoor-air heating is already included in supply heating; adding it again is
incorrect. Annual heat gain is not equivalent to useful winter heating.

All existing properties remain diagnostic. A sensitivity improvement does not
establish measured inputs or authorize catalogue changes, calibration or clustering.
Failed cases remain visible. The two known psychrometric-warning buildings were
selected and retained as failed cases in this study, not replaced using their errors.

The reviewed October 1 output preserves the exact simulation source and a
postprocessing record: final analysis reverses surface convection signs and adds
heating-hour surface totals without rerunning or changing simulation results.
The initially rejected smoke remains in a separate output directory; the missing
advanced-output flag was corrected before the accepted smoke and full diagnostic run.
