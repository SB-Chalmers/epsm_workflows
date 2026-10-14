# National stock source and import contract

This is a proposed, reviewable contract for loading the national stock into PostgreSQL/PostGIS. It does not establish that an importer, database service, national import, or EPSM simulation pipeline has been deployed. Load observed evidence first; resolve archetypes, imputations and renovation scenarios in separately versioned model snapshots.

## Pinned starting sources

The paths below identify the current local deliveries. Register their manifests, checksums, schema versions, retrieval dates and source reference dates before importing. Keep immutable raw files externally and retain their URI, SHA-256, size and row counts in the database; normalized typed values must point back to those assets.

| Source | Pin and import inputs | Expected scope |
|---|---|---|
| Canonical footprint and EPC linkage | `/Users/ssanjay/GitHub/epc_join/outputs/releases/2.0.0/`; `owner_inventory.parquet`, 290 `building_results/kommunkod=*/part.parquet`, catalogue tables and release manifests | 9,055,408 physical building IDs; 290 municipalities |
| Building register | `/Users/ssanjay/GitHub/building_age/outputs/national/registry.sqlite`; `registry_buildings`, metadata and `canonical_footprints` | 9,100,153 register IDs; 9,022,685 matches to the 9,055,408-footprint inventory |
| Height extraction | `/Users/ssanjay/GitHub/footprint_height_extraction/outputs/sweden/harmonized/1.0.0/`; manifest and 50,637 `results/buildings/owner_tile_id=*/part.parquet` | 8,979,706 released result rows; harmonization retained 3,547 corrected rows |
| Height publication summaries | `/Users/ssanjay/GitHub/footprint_height_extraction/outputs/publication/`; provenance and supplement CSVs | 8,702,770 included buildings in 276 fully completed municipalities; 8,218,191 determined heights |
| EPC declarations | `/Users/ssanjay/GitHub/epc_join/data/epc/`; original annual text exports, form-version dictionaries and source-row hashes | Import all supported declaration records, including older forms and conflicting records; the linkage release represents 694,261 distinct certificates and 732,355 subjects |
| Population grid | `/Users/ssanjay/GitHub/epsm_core/ARCHIVE/EDA/befolkning_1km_2025.gpkg`; layer `befolkning_1km_2025` | Population counts by SCB grid cell and reference period, not building occupancy |
| Administrative and income data | Independently pin municipality/county and RegSO/DeSO boundaries, boundary editions, SCB queries and returned metadata | A national income/boundary delivery is not established by the archived city feature CSVs |

SHA-256 pins already checked for this handoff:

| Asset | SHA-256 |
|---|---|
| EPC linkage `manifest.json` | `e1e5c0db1998e56cb14618dfb98d85ce1f3f52530fd522a28c0c8958d9a4033d` |
| EPC linkage `release_manifest.json` | `9c650054f48716a14652a9cce8eaf5afa2ada108d647116b98725e97c2651dc8` |
| Building register SQLite | `33137d6e1aaa4090c2655c6377610424a3003abc72cedc69fec769b817228e88` |
| Harmonized height `manifest.json` | `c0f52ffb4bed93d8bbaff209c6db8a9cacefedbd0b4c057403d0cd2e6238a38f` |
| Height publication `provenance.json` | `445a093a8217f68c5dcbd0c86456b04a40abc5ff342341a1e2e41f2eddb41daf` |
| Population GeoPackage | `7f77beaa70503dc3000d3700997b1c9d5ef7baf01a3545939e75b6f7ceee266c` |

The [completeness manifest](../../output/national-completeness-paper-2026-10-05/data/manifest.json) additionally pins all 290 canonical footprint partitions and the existing selected-feature and membership-audit files. The height release manifest lists its result-partition checksums. Verify these pins on the import host; a changed file constitutes a new source release.

## Identity and geometry revision

`building_id` is the Lantmäteriet `objektidentitet`: the EPC footprint loader, height loader and register XML parser copy that identifier directly. The EPC owner inventory has been checked: 9,055,408 rows, 9,055,408 distinct IDs, zero null IDs and zero failed UUID casts. UUID is therefore suitable for this inventory's normalized building key. Preserve the original identifier as source evidence.

This does **not** prove every source selected the same geometry revision. The EPC owner inventory selects `versiongiltigfran DESC, fid DESC`; the register's canonical footprint index selects `objektversion DESC`, valid-from date, geometry-position preference (`Fasad`, `Takkant`, `Illustrativt läge`), then `fid DESC`. Aggregate municipality/type counts have reconciled, but a complete cross-release comparison of selected `source_fid`, revisions and geometry has not been performed.

Keep `source_fid`, object version, valid-from timestamp, outline kind, original WKB hash, CRS, repair flag and footprint source release. Bind each height observation to its extraction release and original footprint delivery. Joining the UUID locates a building; it does not authorize applying old height/roof geometry to a changed footprint. Record geometry compatibility explicitly, with the comparison method and tolerance. Quarantine incompatible combinations from native model assembly while retaining their observations.

Keep all canonical footprints through left joins, including the **32,723 buildings without a register match**. A register mismatch and a matched register record without a year are distinct states. Register-only IDs may remain in a source-observation table until a footprint becomes available.

## Import order and scale-up

1. Register immutable source releases/assets, source dictionaries, normalization rules and an import run. Load county/municipality code dimensions as text with leading zeros preserved.
2. Load the canonical building inventory and geometry versions from the EPC footprint release. Preserve primary and secondary purpose attributes; do not silently replace its canonical-version selection policy.
3. Load register observations by building UUID, including object status, raw/parsed construction and extension years, version and validity dates. Resolve selected years later.
4. Load released height observations in tile batches. Preserve determined/undetermined rows, methods, point counts, quality flags, failures, representative/eave/ridge height and roof geometry evidence. The publication summaries are QA references, not replacements for building observations.
5. Load property inventory and building/property spatial relations; then EPC certificates, certificate subjects, raw declaration attributes and accepted/candidate/rejected links. Preserve certificate, subject and physical-building scope separately.
6. Load independently versioned administrative boundaries, population cells and verified income metrics. Materialize building-to-region/grid assignments against a pinned geometry revision and assignment rule.
7. Recompute observed coverage from imported tables. Only then create a model snapshot that pins source releases, selection rules, imputation policy, `EPSM_data` catalogue and `epsm_core` version.

Exercise the same path on **20–100 buildings**, including MF, SF, school, office evidence, no EPC, missing year, undetermined height and conflicting scope. Next import one municipality and reconcile its counts. Proceed nationally only after those acceptance gates pass. An importer should resume by `(source_release_id, source_asset_id, batch/partition)` and must not duplicate previously accepted source rows.

For bulk import, stream bounded batches into typed staging tables with `COPY`; convert geometry from the declared source CRS to PostGIS without discarding polygon holes or disconnected parts. Build primary/foreign-key and query indexes after the initial load, then analyze the tables. Keep B-tree indexes for building UUID, certificate/subject IDs, municipality/use and release IDs; GiST indexes for footprints, administrative polygons and grid cells. Do not require a new municipal schema or table for each municipality. Add partitioning only for a demonstrated workload or historical-data volume.

## Field contract and provenance

Each normalized observation needs a source release, asset URI/hash, stable source-row key, source-field name, parser/rule version, value, unit, observation/reference period, validity interval when supplied, scope, and quality/status. Use `(asset_id, source_row_key)` to identify a source record: Parquet row group/row offset or a declared natural key, SQLite table/key, or text line number. Preserve a row-content hash and raw payload/reference so reparsing can be audited. Several source records may support the same physical fact.

Separate `observed`, `inferred`, `assumed`, `conflicting`, `invalid`, `not_applicable` and `missing` states. Null means unknown; it does not mean zero, false or no renovation. A selected fact points to its supporting observation and selection rule. Predictions belong to a model/imputation release and must not overwrite observed source values.

| Evidence | Normalized interpretation |
|---|---|
| Footprint geometry | EPSG:3006 planar geometry and m² area; retain outline kind (`Fasad`/`Takkant`/illustrative), repair evidence and geometry revision. Published geometric validity does not establish positional accuracy. |
| LiDAR height | Metres relative to the inferred local ground; preserve representative, eave and ridge heights separately. Do not substitute ground elevation for building height. Record the vertical datum if verified; horizontal EPSG:3006 alone does not identify it. Source acquisition dates can be missing. |
| Height quality | `determined` is extraction completeness. `warning_count` in publication tables means determined rows with `quality_status='warning'`; the extraction code sets warning for excluded negative-height points and/or unusually high representative height. Other diagnostic flags can occur without warning status. It is not an externally validated confidence category. |
| Construction year | Keep register and EPC years separately, with source object status and source/measurement dates. Distinguish year present, year valid as of a named cutoff, and current register object. The register publication's `current_nonfuture_year` requires `object_status='Gällande'` and year between 1000 and its reference year; it is stricter than a nonfuture-year check alone. |
| Atemp | Reported heated floor area in m² at certificate scope; retain basement and attic evidence separately from geometric floor area. Do not equate Atemp to footprint × storeys. |
| EPC use fractions | Office/school and related `EgenAtemp*` use-share fields are percentages despite their names; retain form-version definitions and mixed-use shares. `EgenAvarmgarage` is an area observation; a garage percentage is derived. |
| Storeys | `EgenAntalPlan`: above-ground storeys; `EgenAntalKallarplan`: basement storeys heated above 10 °C. Preserve raw values and flag implausible counts rather than treating presence as validity. |
| Design ventilation flow | `EgenProjVentFlode`: **L/s/m²** in the existing verified form mapping. It is not ACH and not infiltration. Retain zero, missing, multiple ventilation categories and operational-date uncertainty. ACH conversion requires a stated flow basis and conditioned volume. |
| Heating | EPC positive heating-energy fields identify reported energy carriers/combinations. They do not establish equipment capacities, COP, distribution or installed efficiencies. Keep measured/calculated flags, period and energy boundary. |
| Energy performance | Delivered/end-use energy, specific energy and primary-energy indicators need separate metric codes, units, correction status and regulation/form version. Do not compare primary-energy indicators directly to unweighted delivered energy. |
| Renovation | Keep recommendations separate from completed measures. Completed answers need component, yes/no/unknown, completion year and certificate/measurement scope. Missing evidence does not establish an original component. |
| Population | SCB `beftotalt` is people per grid cell; retain `rutid_scb`, `rutstorl`, `referenstid` and grid geometry. Derive people/km² from the stated cell-area basis. Broadcasting cell population to buildings does not estimate occupants per building. |
| Income | Existing `income_median` is a **legacy label whose exact statistical metric must be verified**. Archived code queries `HE0110I/Tab1InkDesoRegso`, `ContentsCode=0000089U`, `InkomstTyp=SaFörInk`, `Kon=1+2`, year 2024, RegSO 2025, and multiplies returned tkr by 1000. The SCB table contains both mean and median. Preserve contents code, statistic, population definition, price basis, income year, boundary edition and currency conversion; do not assign a canonical median/mean name from the archived code comment. |

Verify the income contents code against the exact API metadata/query response for the [SCB source table](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__HE__HE0110__HE0110I/Tab1InkDesoRegso/), which publishes both statistics and their units. Archive that metadata with the delivery before assigning a canonical metric name.

## EPC scope, selection and coverage

The linkage release contains **720,856 accepted relations**, **716,765 linked physical buildings**, **683,811 linked certificates** and **720,141 linked subjects**. Keep its 6,189,200 candidate links and rejection/semantic evidence accessible as audit data, rather than flattening every certificate onto a building. A certificate can cover several subjects/buildings. Preserve overlap fractions, house-number evidence, matching-policy version, semantic compatibility and cardinality counts.

The existing selected feature inventory has **262,280 buildings** and requires verified newer forms, measured energy, a full calendar year, positive Atemp/heating and a single-building certificate link; it selects an unambiguous latest measurement period. Its completeness percentages are conditional on this selection. It is not the national stock denominator, and `is_max_formularid_for_subject` is not evidence of chronological recency. Retain conflicting/latest-tie records in the source layer and version the resolution rule.

For an initial national coverage reconciliation, primary GIS purpose gives:

| Group | Footprint buildings | Accepted EPC-linked buildings | Register year present |
|---|---:|---:|---:|
| MF: `Bostad;Flerfamiljshus` | 170,478 | 141,116 | 137,640 |
| SF: detached, chained and terraced small houses | 2,827,806 | 462,230 | 2,356,116 |
| School: `Samhällsfunktion;Skola` | 22,674 | 16,562 | 3,195 |
| Broad business: `Verksamhet` | 70,137 | 24,388 | 37,710 |

Trim a trailing empty-detail semicolon only under an explicit normalization rule (`Verksamhet;` → `Verksamhet`) and preserve the raw purpose. Broad business is **not a national office denominator**; national office coverage remains unresolved. EPC-reported offices and mixed office shares can be queried as such. Archived income joins cover selected cities and use rounded coordinate matches; they do not establish national socioeconomic coverage.

Materialize coverage keyed by stock/source snapshot, geography edition, municipality, use-definition version and field. Store denominator, observed valid count, missing/invalid/conflict counts and imputed count separately. Aggregate from building/observation tables with left joins and explicit scope; do not copy the selected-EPC percentages into national coverage. Height coverage must distinguish full released extraction, fully completed-municipality publication scope and the full footprint inventory.

## Import acceptance gates

- Verify source checksums, declared row counts, schema/form versions, CRS and units; reject an unregistered changed asset. Stage source parsing failures with their raw reference rather than dropping them silently.
- Assert canonical building-key uniqueness, UUID validity and municipal-code formatting. Reconcile 9,055,408 footprints and the expected unmatched register count; do not delete footprints to satisfy a join.
- Assert unique `(height_release, building_id)` observations in the harmonized release. Retain and report source duplicate-resolution evidence, missing observations, orphan IDs and footprint-version compatibility. Count imported batches against the release manifest.
- Validate polygon type, SRID, emptiness, area and repair flags. Validate year chronology/status, percentage bounds, Atemp, flow units and plausible storey/height relationships. Preserve conflicts; record the rule used to resolve them.
- Verify certificate/subject/building foreign keys and link cardinalities. Reconcile accepted-link invariants and national counts; do not allocate certificate totals across buildings without an explicit versioned allocation basis.
- For spatial joins, pin geometry and boundary versions, use an explicit boundary-point policy, detect multiple matches, and retain unmatched cases. Do not turn suppressed or absent population/income cells into zero.
- Compare DB-derived municipal/type totals with the pinned QA references and retain an import report. Coverage publication must identify its denominator and selected-use definition.
- Promote a release transactionally only when its staged QA passes. Refresh current-access views and coverage materializations after promotion; keep immutable prior model snapshots and their selected-observation IDs. A source refresh or rule change creates a new snapshot and does not rewrite completed EPSM baseline/renovation runs.
