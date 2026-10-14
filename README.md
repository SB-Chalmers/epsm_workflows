# epsm_workflows

Small study workflows built on the public `epsm_core` API. The base install only
requires Core; install `.[campaign]`, `.[epc]`, or `.[weather]` for those adapters.

The urgent workflow is the fresh Vera 60,000-run surrogate campaign:

```sh
epsm-workflows prepare campaigns/vera_60000.json /path/to/campaign
epsm-workflows plan /path/to/campaign --energyplus /path/to/energyplus
epsm-workflows preflight /path/to/campaign --energyplus /path/to/energyplus
epsm-workflows submit /path/to/campaign --phase pilot --energyplus /path/to/energyplus
epsm-workflows status /path/to/campaign
epsm-workflows submit /path/to/campaign --phase production --energyplus /path/to/energyplus
epsm-workflows finalize /path/to/campaign /path/to/handoff/epsm_surrogate_60000.duckdb
```

`prepare` freezes ten EPWs and ten real-footprint templates. `plan` records the
seed-42 60,000-case design in an immutable DuckDB. `preflight` compiles and validates
every planned specification without launching EnergyPlus. It checkpoints to
`preflight.duckdb`, so rerunning the same command after an infrastructure interruption
continues with missing ordinals. Pilot and production submission require exhaustive,
passing preflight evidence produced by the same Core compiler and EnergyPlus build.

Preflight reports up to the first 20 failures in `preflight.json`; the database retains
the complete set:

```sh
duckdb /path/to/campaign/preflight.duckdb \
  "SELECT ordinal, failure_code, message FROM results WHERE status = 'failed' ORDER BY ordinal"
```

Each Slurm task is the sole writer to one persistent shard and resumes missing
ordinals. The first unexpected simulation failure creates `PAUSE`, retains a compact
diagnostic bundle, and holds remaining array work. `finalize` creates one
self-contained DuckDB with compact hourly arrays and the preflight receipt in its
manifest.

EPC ingestion, weather coordination, fabric construction, and EPC comparison
validation remain library utilities. Retrofit, sensitivity, and real-building
campaign adapters can emit the same `CampaignCase` contract later without changing
the Slurm or DuckDB layers.

The bounded Gothenburg `mf_se02`/F archetype calibration pilot is described in
[docs/archetype-pilot.md](docs/archetype-pilot.md). It prepares a property-separated
train/validation/test cohort, runs frozen `epsm_data` states, and keeps the test
split sealed until parameters are frozen.

Before further calibration, [the building-model diagnostics](docs/archetype-diagnostics.md)
audit the EPC comparison, heated basements, Atemp, weather, attic exchange and Kiva
ground heat transfer. The original splits are diagnostic after inspection; future
evaluation requires new properties. Materials and infiltration stay fixed.

### National building-stock exploration

The evidence-led stock workflow separates building/use/heating families, protects property holdouts, and gates optional clustering and physics calibration. See [docs/stock-exploration.md](docs/stock-exploration.md).

```bash
python -m epsm_workflows.stock prepare --config configs/stock-national.json --output output/my-stock-study
python -m epsm_workflows.stock regress --study output/my-stock-study
python -m epsm_workflows.stock cluster --study output/my-stock-study
python -m epsm_workflows.stock calibrate --study output/my-stock-study
python -m epsm_workflows.stock evaluate --study output/my-stock-study
python -m epsm_workflows.stock report --study output/my-stock-study
```

### Focused archetype diagnosis

Test whether one observable physical subdivision improves a multifamily group,
then run 12 input-selected buildings through frozen-state/window/WWR scenarios:

```bash
python -m epsm_workflows.archetype_diagnosis run --config configs/archetype-diagnosis-mf-se02.json
```

This reuses previously inspected data as development evidence, preserves the
national study, retains strict geometry/simulation failures and makes no catalogue
changes. See [the focused study protocol](docs/archetype-diagnosis.md).

The [building archetype application workflow](docs/archetype-application.md) accepts
reviewed Core specs across building uses, freezes shared physical candidates and
property splits, and reports per-building errors below 20%/10% without changing
the catalogue. See the [source and implementation log](docs/archetype-implementation-log.md).

## Fixed-data cohort transfer

The [cohort-transfer protocol](docs/cohort-transfer.md) fits bounded native physics
within target-free subcohorts and tests transfer across independent properties.
It compares the unfitted matrix, a single central representative and shared-cohort
calibration using nested retrospective validation. The implementation retains
failed simulations, descriptor-support fallbacks and confounded parameter reports.
Run it with `python -m epsm_workflows.cohort_transfer --help`; configurations for
the existing 12- and 126-building SE.02 studies are under `configs/`. Reference
matrix values and effective calibrated profiles remain distinct. Per-building
20%/10% accuracy is established by completed campaign results, not by simulator
support or successful preparation.

The [membership audit](docs/archetype-membership.md) checks EPC category, dwelling
count, heated programmes and GIS/link agreement before pure-archetype calibration.
Conflicting records remain in inventory with explicit reasons. Detached placement
does not make an apartment block single-family.

### MF default revision

The [MF parent and dynamic-stock comparison](docs/mf-parent-dynamic-comparison.md)
rebuilds certificate evidence, fits shared physical parents with nested property
validation, tests metadata-only component histories, and exports adoption gates
and target-free assignments. Run `python -m epsm_workflows.mf_parent_dynamic --help`.

The [MF revision workflow](docs/mf-revision.md) audits all 216 MF slots, applies independently sourced component/operating evidence, quarantines known extraction mismatches, prepares national baselines, and runs strict separate/joint physics screens. Historical properties remain development data; release gates do not certify retrospective fits. See the [implementation and results](docs/mf-revision-results.md). Start with `python -m epsm_workflows.mf_revision audit --config configs/mf-revision-national.json --output output/NEW-mf-revision`.
