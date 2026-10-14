# Focused archetype diagnosis

This workflow checks one multifamily era/system family using existing data. It never calls a newly reshuffled split an untouched evaluation. The previous national study remains unchanged.

```bash
python -m epsm_workflows.archetype_diagnosis run --config configs/archetype-diagnosis-mf-se02.json
```

Individual stages are `prepare`, `regress`, `physics` and `report`. Completed stages are restartable with identical configuration, prepared data, original EPC sources and implementation/package hashes. Failed stages retain diagnostics and can resume cached simulation receipts. The output directory is configured in JSON.

Preparation re-reads EPC rows and dictionaries, checks target consistency and reconstructs metric geometry indicators. Above-ground storeys and basement floors heated above 10°C remain separate. Missing/ambiguous basement counts remain unknown; zero never establishes absence of an unheated basement. Future/undated renovations do not become measured-period predictors. No energy tails are trimmed.

Regression uses five outer property folds and five inner property folds, five-knot additive splines and ridge penalties 0.1/1/10/100. Candidate subdivisions use only the eight declared physical attributes and at most one split. Continuous thresholds are training quartiles. Each branch must have 30 independent training properties in every inner fold. Missing split inputs retain the pooled prediction. A subdivision needs 5% development MAE improvement, preserved 20% coverage, a paired property interval supporting lower MAE and median ARI at least 0.7 over 20 property subsamples. Full subgroup reselection is repeated in the stability test. Otherwise keep the parent pooled. Outer-fold results evaluate this selection procedure; all remain exploratory because earlier studies inspected these data.

Representatives are selected without energy access, as central actual development buildings plus independent-property physical contrasts. The 12-building Gothenburg diagnostic cohort is separately selected using inputs alone and spans physical and measurement-year variation. Original AMY files and provenance are copied and calendar-validated; no weather is selected by heating error.

Physics compiles all 96 cases and runs three input-diverse existing-state smoke cases first. A failed smoke stops larger simulations. Strict geometry and EnergyPlus checks remain active. Failed cases are retained without replacement, fallback or warning waiver. The cases are three frozen states (36), all states at WWR 0.3 (36), existing-state U=2.2 (12), and existing U=2.2 with WWR 0.3 (12). The declared changes preserve geometry, envelope outside glazing, infiltration, ventilation and operating assumptions. Kiva and attic/basement uncertainties are recorded explicitly. Net delivery losses 0/5/10 kWh/m² are postprocessing sensitivities, never per-building fitted values.

The report includes nested-fold errors and assignments, representative buildings, per-building physical errors and geometry, paired comparisons, failures, delivery sensitivities and missing evidence. Calibration remains blocked unless a subsequent study establishes the original 120-building, independent-property, boundary and building-evidence gates. No catalogue is modified.
