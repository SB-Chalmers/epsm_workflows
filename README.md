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
