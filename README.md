# epsm_workflows

Study workflows for `epsm_core` 2.0: Swedish EPC/Lantmäteriet ingestion, the
10,000-simulation campaign, transactional storage, QA and group-safe ML diagnostics.

Install the sibling core first, then `pip install -e .`. The configuration contains
explicit local EPW paths; edit those paths before creating a campaign. A nonempty
campaign directory rejects any change to configuration, source code, engine, weather
or package versions. Create a fresh directory for a changed experiment.

```sh
epsm-campaign plan campaigns/v2_10000.json results/v2 --energyplus /path/to/energyplus
epsm-campaign pilot campaigns/v2_10000.json results/v2 --energyplus /path/to/energyplus --workers 4
epsm-campaign run campaigns/v2_10000.json results/v2 --energyplus /path/to/energyplus --workers 4
epsm-campaign qa campaigns/v2_10000.json results/v2 --energyplus /path/to/energyplus
epsm-campaign ml campaigns/v2_10000.json results/v2 --energyplus /path/to/energyplus
```

The run command runs OAT and the first 200 planned rows before the remaining rows.
Repeated execution resumes incomplete rows. A failed EnergyPlus run stops the stage.
The first 200 contain two anchors and nineteen complete pairs per city.

`store.duckdb` owns run status. Each completed run has validated `hourly.parquet`
and `scalars.parquet` files with catalogue checksums. An interrupted partition write
cannot mark a run complete. Recovery quarantines orphan partitions. Shared weather
and calendar signals are stored by EPW checksum and joined on reading.

`training_data(store, target=...)` returns versioned predictors, one target, `group_id`
and city labels. Model evaluation keeps pairs and cross-city anchors together;
leave-one-city-out also excludes the held-out anchors' groups from training.
Identifiers, output targets and arbitrary database columns cannot enter the feature
matrix. DHW is explicitly excluded as an input when predicting DHW or combined load.

## Campaign assumptions

All buildings use explicitly resolved materials from `data/materials.json`. This is
a pinned subset of the local EPSM_data opaque material catalogue, preserving source
material names and full thermal properties. No Boverket/network call is needed.
The original seed-42 samples preserve layer thicknesses; corrected anchors use the
intended insulated cold-attic ceiling and Kiva rather than interpolated U-values.

New pairs use 950 optimized 25-dimensional LHS points per city. Conditional airflow
dimensions are inactive outside their topology. WWR columns and floor-area values are permuted to satisfy
constraints while preserving every marginal stratum. Floor labels are balanced and
permuted jointly with feasibility. This constrained design is no longer claimed to
retain the optimizer's original discrepancy optimum.

L cuts condition the upper width fraction on the 30% removed-area limit; shape pairs
use the shared L/U cut interval. U wings have at least 4 m width and bases 6 m depth.
All new footprints have at least 8 m outer width. Massing alternatives differ by two
storeys. Physics is held identical within pairs, including the fixed
`energyplus_24_2_default_soil` Kiva profile. S/F/FT/FTX are independently balanced
from roof and comparison labels, avoiding categorical confounding.

The first release's annual target `ideal_thermal_load_kwh` is space-heating demand
plus DHW load. Cooling is separate. Anchor deltas explicitly compare the former
zone-load output with the new ideal supply-air thermal demand, so the difference
contains both physics and accounting changes.

## Real-building data

`epc.load_records` performs explicit read-only queries against a caller-specified
database. `epc.footprints` preserves holes and all multipolygon parts using Shapely
and PyProj. Source CRS is required; distance filtering takes place in metres.
`building_from_record` takes a caller-supplied assumption profile and returns the
canonical building plus an Atemp reconciliation report. Measured Atemp never
silently changes the footprint or thermal zoning.
