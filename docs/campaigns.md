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
campaign.

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
footprints/stock.json`. Geometry is cleaned with Core's `prepare_geometry`,
localised and frozen; rejected records are listed, not dropped silently.
Footprints with a courtyard hole use EnergyPlus's prescribed 18 °C ground
temperature because Kiva cannot model holed floors; `input_ground_method` records
which method each run used.

Weather is copied as given. Leaving `amy` out but naming `amy_station_id`
compiles the measured year with `smhi2epw` during packing (install the `weather`
extra). TMY and AMY of a city share the TMY's site coordinates.

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
writes `PAUSE`, saves a compact diagnostic tarball and holds the array; an RC
failure is recorded on the row. `epsm-campaign status`, `pause` and `submit`
cover the operator's remaining needs; `epsm-campaign submit --phase production`
after review resumes a paused campaign. Submission checks `MaxArraySize`,
`C3SE_quota` headroom (500 GiB, 5,000 entries) and the 50,000-entry budget of the
EPSM base.

Run the studies one after another and copy each `handoff/` folder off the cluster
before starting the next; a finalized campaign leaves `handoff/`, `logs/`,
`plan.json` and `study.json` behind.
