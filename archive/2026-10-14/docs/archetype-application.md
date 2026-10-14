# Frozen building archetype application

This workflow joins EPSM Data component assignments to EPSM Core's documented
operations and delivery model. It accepts reviewed complete `BuildingSpec` JSON
files rather than guessing missing building evidence. The first target is annual
delivered **space heat**, with a measurement-year weather file and a consistent
actual-use or normal-use boundary. DHW, business electricity, EPC primary energy
and total delivered building energy are different targets.

## Preparing physical inputs

1. Resolve category/era and independently observed component renovations with
   `epsm_data.resolve_building_archetype`. Retain its assignment/requirements in
   project evidence. Do not choose renovation states by minimum building error.
2. Verify heated extent, storeys, exposed walls, foundation and attic. Preserve
   metric footprints; reconcile Atemp using `load_reference_area_m2`, not footprint
   scaling. Old cases that already rescaled densities need review before reuse.
3. Apply Core's versioned residential, office or primary/secondary school profile.
   Supply the real operational calendar and source, room-function assignments,
   ventilation topology/flow/recovery and independent leakage assumptions. Match
   normal-use profiles to normalized targets and actual-use profiles to raw use.
4. Supply an explicit Core heating delivery model and source: thermal demand is
   not purchased heat. Net distribution losses are declared independently, never
   fitted per building. Fan electricity remains separately reported.
5. Write each baseline/candidate as a complete spec. Candidates are common physical
   hypotheses within a predeclared family. The same candidate names must exist
   for all buildings in that family. Spec load reference area must match EPC Atemp.

See Core `docs/archetype-operation.md` and Data `docs/building-application.md`.
The [implementation research log](archetype-implementation-log.md) records source
checks, investigations, test failures and changes. No numerical catalogue value
was changed by this implementation.

## Configuration

JSON paths are relative to the configuration file; weather paths in Core specs
must resolve to existing files. Target CSV columns are `building_id` and
`delivered_space_heat_kwh` (positive annual kWh). This shape is illustrative:

```json
{
  "schema_version": 1,
  "study_kind": "diagnostic",
  "target_boundary": "delivered_space_heat",
  "target_normalization": "actual_use",
  "weather_boundary": "measurement_year_before_weather_correction",
  "targets_csv": "targets.csv",
  "bootstrap_resamples": 1000,
  "buildings": [{
    "building_id": "reviewed-building-id",
    "property_key": "independent-property-id",
    "family": "multi_family_SE02_F",
    "split": "train",
    "previously_examined": false,
    "atemp_m2": 1000,
    "measurement_year": 2023,
    "operation_basis": "actual_use",
    "evidence": {
      "energy_boundary": "source and field interpretation",
      "heated_extent": "reviewed floor/area evidence",
      "exposed_walls": "survey or audited adjacency",
      "foundation": "reviewed boundary evidence",
      "attic": "reviewed topology evidence",
      "operating_assumptions": "profile/calendar/room functions/source",
      "measurement_year_systems": "systems operating in target year"
    },
    "specs": {"baseline": "building-baseline.json", "candidate": "building-candidate.json"},
    "scenario_sources": {"baseline": "frozen source fabric", "candidate": "independent physical hypothesis"}
  }]
}
```

One property stays in one split. Previously inspected buildings belong to training;
reshuffling them is not fresh validation. Diagnostic preparation records missing
evidence. Confirmation preparation rejects it, requires at least 120 building
observations and predeclared `minimum_independent_properties` for train, validation
and test in every family (integers >=2). Those minima are explicit study design,
not a power calculation or sufficient proof of generalization. The original
scientific confirmation gates and a suitable representative cohort still apply.

## Execute the stages

```sh
python -m epsm_workflows.archetype_application prepare --config project.json --study output/new-application-study
python -m epsm_workflows.archetype_application simulate --study output/new-application-study --energyplus /path/to/energyplus
python -m epsm_workflows.archetype_application select --study output/new-application-study
python -m epsm_workflows.archetype_application evaluate --study output/new-application-study --split validation
python -m epsm_workflows.archetype_application simulate --study output/new-application-study --energyplus /path/to/energyplus --split test
python -m epsm_workflows.archetype_application evaluate --study output/new-application-study --split test
```

Preparation hashes configuration, targets, specs, weather and runtime source/data
without interpreting target values. Simulation pins the executable/schema and
retains every accepted, failed or skipped-after-smoke case. A baseline failure
stops other cases for that building; it stays in the attempted denominator.
Strict Core geometry and EnergyPlus acceptance remain active. No warning waivers,
fallback result substitution or per-building residual correction are introduced.

Selection waits for all development attempts, then chooses one common scenario
per family on the same complete paired panel. The default uses training MAE in
kWh/m². Set `selection_objective: "coverage20_then_mae"` to prioritize the fraction
strictly below 20% absolute building error, then MAE. Any
failed training candidate retains baseline for that family. Ties retain baseline.
The choice and development receipts are hashed before testing. The test energy
field is not numerically read until every test case attempt is recorded; opened
test receipts cannot subsequently be replaced. Changes to prepared inputs,
runtime, choices or frozen receipts require a new study.

Evaluation records each building's signed/absolute error in kWh/m², absolute
percentage error, paired MAE, bias, median APE, category/family summaries and
strict counts below 20% and 10%. `below_20_percent`/`below_10_percent` are counts,
not percentages; explicit fraction fields distinguish accepted and attempted
denominators. Property bootstrap resamples all paired buildings of each property,
with 95% intervals for MAE, paired MAE difference and threshold coverage.
Its intervals are conditional on accepted pairs; failed pairs are reported against
all attempts. Small diagnostic panels cannot establish population accuracy.

Selection creates a predictive hypothesis. This workflow does not automatically
accept or publish a catalogue correction, even in confirmation mode. Evidence
and untouched evaluation must support that subsequent scientific decision.


The retrospective SE.02 diagnostic can be reproduced with
`python -m epsm_workflows.multifamily_improvement --source
output/archetype-diagnosis-mf-se02-2026-10-03 --output NEW_DIRECTORY --workers 6`.
It uses the existing 12-property training cohort and frozen AMY inputs. All outcomes
are development evidence; this command cannot certify unseen-building accuracy.
Soil/attic/net-loss cases are separate sensitivities and cannot win selection.
The read-only `confirmation_inventory` command checks existing stock-study
metadata without opening energy target files.
