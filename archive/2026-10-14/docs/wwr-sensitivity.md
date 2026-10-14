# Gothenburg WWR sensitivity

Compare assumed window-to-wall ratios **0.2 and 0.3** on the existing frozen
126-building cohort. Both cases use Core Kiva, reported heated basements at
2.5 m depth and 21°C, the same measurement-year AMY weather, and the existing,
standard and advanced catalogue constructions. Infiltration, glazing properties,
ventilation, operating schedules and floor area are fixed. Only WWR changes.

Use equal weights across the three states. Compare both cases on buildings whose
six state/WWR simulations all pass strict validation. Previously inspected
properties remain diagnostic data; this is not untouched-test validation. No
catalogue default or calibrated parameter is changed.

```sh
PYTHONPATH=src:../EPSM_data/src:../smhi2epw/src \
  ../epsm_core/.venv/bin/python -m epsm_workflows.wwr_sensitivity \
  --source output/archetype-diagnostics-mf-se02-2026-10-01-reviewed \
  --output output/wwr-sensitivity-mf-se02-2026-10-01 \
  --energyplus /Applications/OpenStudio-3.9.0/EnergyPlus/energyplus \
  --workers 8
```

First run both WWRs and all three states on four roof/basement combinations from
the previously selected, energy-blind diagnostic sample. All 24 cases must pass
before the full cohort starts. Every run preserves the model, compiler identity,
weather hash, engine diagnostics and failed-case receipt. The cohort and source
hashes are pinned in audit.json. Changed sources require a new output directory.

The report includes mean absolute error, median absolute percentage error,
signed bias, distribution quantiles, property bootstrap intervals, individual
and state-specific errors, and groups by roof, heated-basement presence, Atemp,
height and measurement year. Net delivery-loss sensitivities remain 0/5/10
kWh/m². Lower EPC error does not establish the actual WWR of a building.

The executed source snapshot is retained with the study so subsequent code
formatting or changes do not obscure the run's original provenance.
