# Joint Latin hypercube screen

Run a deterministic, stratified design of 40 joint settings on the 12 buildings selected previously using physical attributes, with all three frozen renovation states. Use eight simulation workers. Recent diagnostic timings suggest about 10–15 minutes; the actual runtime is recorded.

```sh
PYTHONPATH=src:../EPSM_data/src:../smhi2epw/src ../epsm_core/.venv/bin/python -m epsm_workflows.lhs_sensitivity \
  --source output/parameter-sensitivity-mf-se02-2026-10-01 \
  --output output/lhs-sensitivity-mf-se02-2026-10-01 \
  --energyplus /Applications/OpenStudio-3.9.0/EnergyPlus/energyplus
```

`BOUNDS` in the implementation declares all continuous ranges. Window reference fraction interpolates between the current state's window U value and the TABULA comparison value. Basement and attic coordinates apply only where those spaces exist. Catalogue infiltration and material identities stay fixed. Soil, basement and attic inputs remain uncertainty scenarios requiring physical evidence before adoption.

The same vectors apply to all buildings and states. Compile and simulate the first vector across all buildings/states before continuing. Retain strict simulation diagnostics and failed cases. Receipts allow deterministic restart. The frozen workflow, dependency source hashes, weather hashes, EPC cohort, design and model hashes preserve provenance.

Choose one shared design per state, and one for the equal-state predictive mixture, using only original training energy. Retain the baseline when no sampled design improves its training MAE. Freeze those choices at delivery loss 5 kWh/m² and assess loss 0/10 without reselection. Original validation/test buildings are diagnostics because their errors have previously been inspected.

Also show the minimum individual absolute error among sampled settings as an optimistic retrospective bound. It uses each building's energy and must never be described as predictive validation or reliable identification of its physical parameters. Keep the same complete building panel for every comparison; failed buildings remain explicitly listed.

Sensitivity uses conditional linear coefficients and marginal Spearman correlations, estimated separately by building/state. LHS itself does not yield Sobol indices. A small postprocessor checks selected pair interactions with leave-one-design-out regression diagnostics and produces plots and a plain-language report. Forty vectors cannot exhaustively establish all interactions or prove a threshold unattainable. No calibration or catalogue promotion follows automatically from this screen.
