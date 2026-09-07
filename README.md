# epsm_workflows

Development package: **0.1.0.dev0**. Current work: [the first EPC validation report](reports/epc-validation-2026-09-06/index.html) and [reproduction instructions](examples/epc_validation/README.md). The pilot keeps source interpretation, weather retrieval, delivery assumptions and accuracy metrics outside core. It contains 12 actual-year comparisons and 12 paired TMY runs, with all assumptions and exclusions retained.

## Building campaigns

Study workflows use the current `epsm_core` API for explicit building geometry,
transactional storage, QA and group-safe ML diagnostics. Historical reports retain
their original software provenance; archived experimental stores are unsupported
by the current parser. Start a fresh output directory for the current schema.

Install the sibling core first, then `pip install -e .`. The configuration contains
explicit local EPW paths; edit those paths before creating a campaign. A nonempty
campaign directory rejects any change to configuration, source code, engine, weather
or package versions. Create a fresh directory for a changed experiment.

```sh
python -m epsm_workflows.cli plan campaigns/buildings.json output/building-campaign --energyplus /path/to/energyplus
python -m epsm_workflows.cli pilot campaigns/buildings.json output/building-campaign --energyplus /path/to/energyplus --workers 4
python -m epsm_workflows.cli run campaigns/buildings.json output/building-campaign --energyplus /path/to/energyplus --workers 4
python -m epsm_workflows.cli qa campaigns/buildings.json output/building-campaign --energyplus /path/to/energyplus
python -m epsm_workflows.cli ml campaigns/buildings.json output/building-campaign --energyplus /path/to/energyplus
```

The run command runs OAT and the first 200 planned rows before the remaining rows.
Repeated execution resumes incomplete rows. A failed EnergyPlus run stops the stage.
The first 200 contain two anchors and nineteen complete pairs per city.

`store.duckdb` owns run status. Each completed run has validated `hourly.parquet`
and `scalars.parquet` files with catalogue checksums. An interrupted partition write
cannot mark a run complete. Recovery quarantines orphan partitions. Shared weather
and calendar signals are stored by EPW checksum and joined on reading.

`training_data(store, target=...)` returns named predictors, one target, `group_id`
and city labels. Model evaluation keeps pairs and cross-city anchors together;
leave-one-city-out also excludes the held-out anchors' groups from training.
Identifiers, output targets and arbitrary database columns cannot enter the feature
matrix. DHW is explicitly excluded as an input when predicting DHW or combined load.

## Campaign assumptions

All buildings use explicitly resolved materials from `data/materials.json`. This is
a pinned subset of the local EPSM_data opaque material catalogue, preserving source
material names and full thermal properties. No Boverket/network call is needed.
The original seed-42 samples preserve layer thicknesses; corrected anchors use the
intended insulated cold-attic ceiling. Roof topology belongs to `GeometrySpec.roof`;
`fabric` returns construction assemblies only. Campaigns explicitly retain whole-building
zoning, while the core default is per-storey zoning.

New pairs use 950 optimized 25-dimensional LHS points per city. Conditional airflow
dimensions are inactive outside their topology. WWR columns and floor-area values are permuted to satisfy
constraints while preserving every marginal stratum. Floor labels are balanced and
permuted jointly with feasibility. This constrained design is no longer claimed to
retain the optimizer's original discrepancy optimum.

L cuts condition the upper width fraction on the 30% removed-area limit; shape pairs
use the shared L/U cut interval. U wings have at least 4 m width and bases 6 m depth.
All new footprints have at least 8 m outer width. Massing alternatives differ by two
storeys. Physics is held identical within pairs, including the default uncalibrated
18°C ground-surface temperature assumption. S/F/FT/FTX are independently balanced
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

## Geometry comparisons

`python -m epsm_workflows.geometry_study` creates a matched comparison from a current
`BuildingSpec`. Zoning uses `ZoningSpec`; apartment comparisons explicitly opt into
per-storey fallback and mark substituted cases for exclusion from valid comparisons.
Flat cold attics and compact roofs remain separate geometry choices. Roof, window
and attached-shading settings are independent.

The compiler fingerprints nested package sources, settings and the input schema.
Campaign manifests also record source-file hashes recursively; no package bump is
needed for an implementation change. Package development versions remain
`0.1.0.dev0`. The campaign manifest revision is independent of the package version.
