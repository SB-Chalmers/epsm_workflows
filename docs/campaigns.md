# Vera training-data campaigns

`epsm_workflows.campaign` turns one study file into a self-contained package that
runs on Vera with a small, fixed set of released wheels, and delivers one DuckDB
database per study with the same shape every time. It replaces the archived
2026-09 campaign scripts; its sampler is that campaign's sampler, verified row for
row, so the stock study keeps the old parameter diversity on new geometry.

Install the authoring extra locally: `pip install "epsm_workflows[campaign]"`.
Vera needs only `epsm_core`, `epsm_data`, `duckdb`, `numpy` and EnergyPlus 24.2;
`epsm_rc` is optional and adds hourly RC results next to EnergyPlus.

## Study kinds

| kind | what varies | geometry | weather |
| --- | --- | --- | --- |
| `stock_diversity` | 28 bounded inputs, floors 2–8, ventilation S/F/FT/FTX, schedule family and shift; every design in 3 synthetic contexts | procedural rectangle/L/U/courtyard families and frozen real footprints, in the share you choose | one reviewed TMY and one measured AMY per city |
| `weather_sweep` | a fixed set of sampled buildings | as above | every EPW you list, crossed with every building |
| `before_after` | one catalogue measure at a time on existing-state archetype buildings (`mf_se01`–`mf_se07`) | as above | TMY (or AMY) per city |
| `ventilation_sweep` | airflow inputs only, on fixed buildings and weather | as above | TMY (or AMY) per city |

All four write the same tables: `runs` (inputs as typed `input_*` columns plus
`study_kind`, `cohort`, `site`, `context`, `weather_kind`, `footprint_family`,
`geometry_source`, `scenario_id`, `measure_id`), `scalars`, `hourly_results`
(EnergyPlus `heating_kwh`, `cooling_kwh`, `dhw_kwh`, and `rc_heating_kwh`,
`rc_cooling_kwh` when RC ran), `weather`, `buildings` (the Core Building of each
scene), `task_runtimes`, and the views `training_rows` and `hourly`. Columns that
do not apply to a kind are NULL, so a model can train on the studies in sequence.

## Writing a study

A study is JSON validated by `StudySpec` (`src/epsm_workflows/campaign/study.py`).
Paths are relative to the file. The templates in `campaigns/studies/` are the
four studies agreed with the training-data consumer; copy one and edit counts,
bounds, cities or measures. `epsm-campaign validate study.json` prints the
identity; the identity is the digest of the file, so an edited study is a new
campaign. The sampled designs themselves depend on the authoring platform and
SciPy build (the `random-cd` LHS optimisation is not bit-identical across
macOS/arm64, Linux/x86_64 or SciPy minor versions), so pack once and ship the
package: `plan.json` records `plan_sha256` and `manifest.json` the versions that
authored it, and Vera only ever runs the frozen `plan.duckdb`.

```json
{
  "kind": "stock_diversity", "name": "stock-2027", "seed": 42,
  "cities": [{"slug": "malmo", "city": "Malmö", "tmy": "weather/malmo/tmy.epw",
              "amy": "weather/malmo/amy-2018.epw"}],
  "geometry": {"families": ["rectangle", "L", "U", "courtyard"],
               "real_footprints": "footprints/stock.json", "real_fraction": 0.5},
  "stock": {"diversity_designs": 9000, "anchor_designs": 200}
}
```

Real footprints come from portable EPSM records: export them with `epsm_db`
(`record.to_dict()`), then `epsm-campaign footprints records/*.json --output
footprints/stock.json`. Registry geometry arrives as MultiPolygon; the largest
part is kept and `source_parts` says how many there were. Geometry is cleaned
with Core's `prepare_geometry`, localised and frozen; rejected records (area
below 250 m², slender limbs, preparation failures, any two vertices closer
than 0.05 m, more than 60 vertices) are listed, not dropped silently. The
vertex-spacing rule exists because Core tessellates upper floors between
footprint vertices and rejects surface edges under 0.010001 m, so a digitised
pinch fails every case of that footprint at preflight. The vertex cap exists
because EnergyPlus run time grows steeply with footprint vertices and its
shadow-overlap capacity was exhausted by 64-, 143- and 177-vertex registry
footprints in dense contexts (about 1 % of the registry pool is over 60).
Vertices under 1 cm from the chord between their ring neighbours are removed
before those checks and counted in `collinear_vertices_removed`: they are the
tips of zero-width spikes that Core's long-chord simplification keeps, and
EnergyPlus deletes such a vertex and then reports the zone as not enclosed.
After the sampled orientation is baked in, each part
outline is passed through Core's own footprint cleanup (1 cm simplification)
and the prepared outline is stored as the authored one; the transform metadata
lists the parts that changed (`footprint_cleanup_changed`). Without this, Core
refuses a Building whose prepared outline differs from the authored one, and a
rotated registry outline can cross that tolerance where the frozen one did not.
Every placed case is checked with `readiness(engine="energyplus")` at pack time. Records with `facts.storeys` keep their storeys; without it the
sampler draws floors as for procedural footprints.
Footprints with a courtyard hole use EnergyPlus's prescribed 18 °C ground
temperature because Kiva cannot model holed floors; `input_ground_method` records
which method each run used.

Weather is copied as given. Leaving `amy` out but naming `amy_station_id`
compiles the measured year with `smhi2epw` during packing (install the `weather`
extra). TMY and AMY of a city share the TMY's site coordinates. A weather-sweep
file of a measured leap year (8,784 rows) is packed without 29 February under the
next non-leap calendar year that starts on the same weekday (2016 runs as 2021,
2020 as 2025), because every handoff array is 8,760 hours and Core simulates a
leap year in full; `weather.json` records `source_year` and `leap_day_dropped`
and the weather id stays as declared.

## Packing

```sh
epsm-campaign pack campaigns/studies/stock_diversity.json out/ --workers 8
```

This samples, builds every case as a Core `Building`, solves the envelope for the
sampled U-values (`Building.assign_element_u_value`), generates contexts, freezes
weather and writes `out/<name>-<id>/`: `plan.duckdb`, `plan.json`, `study.json`,
`weather/`, `weather.json`, `footprints.json`, `manifest.json`, `vera.sh` and a
`README.md` with the exact Vera commands, plus the same folder zipped. Buildings
are stored once per scene; runs reference them. `plan.json` records the sampling
validation (LHS strata, categorical balance, bounds) and group sizes that
`finalize` later re-checks.

Nothing in the package depends on the authoring machine. Vera records its own
EnergyPlus and package identities in every shard (`task_runtimes` in the handoff).

## Running on Vera

The package README is the operator's checklist. In short: unzip under the EPSM
base, export `EPSM_PYTHON`, `EPSM_ENERGYPLUS`, `EPSM_ACCOUNT`, and
`sbatch vera.sh "$PWD"`. The stages chain through Slurm dependencies:

1. `preflight` compiles every case (no EnergyPlus run) into `preflight.duckdb`
   and refuses to continue on any failure.
2. `pilot` runs the pilot cases, one per array element, at most 64 at once.
3. `production` sizes batches from the pilot's p95 runtime (target 45 minutes,
   `production_min_batch_size`–`production_max_batch_size` cases per task),
   balances them by geometry complexity and submits one array.
4. `finalize` merges shards, runs the acceptance checks, reopens the database
   read-only, writes `README.md`, `.sha256`, `.verification.json`,
   `.cleanup.json`, and removes shards, receipts, locks, weather copies and the
   plan database.

Each task writes one shard, one receipt and one log. A strict EnergyPlus failure
writes `PAUSE`, saves a compact diagnostic tarball under `diagnostics/` and holds
the array; an RC failure is recorded on the row. After review, either fix the
cause (a longer `execution.timeout_seconds` in the package's `study.json`, say)
or leave the case out on record: `epsm-campaign exclude ROOT --ordinal N --reason
"..."` needs that case's diagnostic bundle and writes `exclusions.json`; workers
skip the case, and `finalize` puts its plan row in `runs_excluded` with the
reason, failure code and bundle path, lists it in the handoff manifest and README,
and checks group sizes against the plan minus the exclusions. Then
`epsm-campaign resume ROOT` clears `PAUSE` and cancels the held arrays, and
`sbatch vera.sh ROOT production` resubmits exactly the unfinished tasks and
chains `finalize`. Submission checks `MaxArraySize`,
`C3SE_quota` headroom (500 GiB, 5,000 entries) and the 50,000-entry budget of the
EPSM base.

Run the studies one after another and copy each `handoff/` folder off the cluster
before starting the next; a finalized campaign leaves `handoff/`, `logs/`,
`plan.json` and `study.json` behind.
