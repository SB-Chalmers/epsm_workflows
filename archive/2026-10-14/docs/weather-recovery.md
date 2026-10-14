# Measurement-year weather recovery

`compile_measurement_weather` explicitly enables smhi2epw's bounded observation
fallback for newly compiled weather. Library callers retain opt-in behavior.
The changes apply when compiling fresh artifacts; they do not upgrade weather
files or frozen runtime snapshots from existing campaigns.

## Configuration

The workflow accepts these keyword arguments:

| Argument | Default | Meaning |
| --- | --- | --- |
| `metobs_gap_fallback` | `True` | Recover otherwise unfillable meteorological gaps from recorded observations. |
| `gap_fallback_max_distance_km` | `75.0` | Maximum donor distance from the requested weather point. |
| `gap_fallback_max_stations` | `3` | Maximum total candidate station attempts. |

For strict station-only compilation, pass `metobs_gap_fallback=False`. Weather
still uses the improved temporal reconstruction, with linear filling through
three hours, daily profiles through 48 hours and complete reference days searched
within seven days. Larger gaps require recorded donor observations; the temporal
limit is unchanged. Valid primary observations are preserved. Solar data is
queried at the requested point. Pressure is derived from sea-level QFF at the
resolved target elevation, which defaults to the primary station elevation.

This integration requires smhi2epw commit
`4861f9ecdb0963cb564ab8ae67ff0137810d9d8a` or a descendant containing the recovery
API. Install/use the updated checkout when running this workflow. Earlier
package releases do not expose these configuration fields. For adjacent local
repositories, install the sibling package in the workflow's Python environment:

```sh
python -m pip install -e ../smhi2epw
```

The complete algorithm, API/CLI examples and diagnostic definitions are in
[smhi2epw's recovery guide](../../smhi2epw/docs/weather_recovery.rst).

## Receipts and qualification

`weather-provenance.json` stores the configuration and dataclass result, including
primary missing/invalid counts, donor identities and coordinates, distances,
replaced UTC intervals, source overlap differences and temporal filling counts.
`required_reconstructed_fraction` measures pre-recovery missing/invalid cells
across five required meteorological variables on the ingestion grid, including
calendar boundary hours. This field is populated by the enabled recovery path.
The legacy `interpolated_fraction` describes subsequent temporal filling and
excludes cells already supplied by donors. Inspect both.

The library's optional `provenance_path` also produces consumed-response and
source-code hashes. The replay enabled this richer receipt; the workflow's
existing coordinator receipt is a separate format and is not itself a complete
raw-response fingerprint manifest.

A complete, range-valid EPW does not establish local weather accuracy. Review
substantial replacements, entire missing variables, site exposure, donor distance
and overlap differences before archetype qualification. The 5% reconstruction
highlight used in the replay is a reporting aid, not an acceptance threshold.
Changing weather must create a fresh campaign weather identity; prior annual
predictions and calibrated profiles require renewed validation against it.

## Replay evidence: 5 October 2026

The replay covered the **53 previously failed municipality/year weather jobs**
from `national-mf-se02-random-baseline-2026-10-05-v1`. It did not draw replacement
buildings, expand the original weather-job set, rerun annual building simulations
or run calibration.

- **52 jobs recovered**, passing smhi2epw export and EPSM calendar validation.
- Nine recovered without donors; 43 used quality-filtered same-hour observations.
- Nineteen reconstructed more than 5% of required meteorological cells. Some
  substitute an entire variable; one substitutes all primary meteorology.
- All **32 original successful EPWs** retained their recorded SHA-256 values.
- Job **2101:2018** remains rejected: an **86-hour pressure gap**, 15–19 October
  UTC, is also missing at its sole eligible pressure donor within 75 km. Partial
  donor recovery shortened the original 741-hour gap but cannot complete it.

Evidence is local under `output/weather-robustness-2026-10-05-v1`: `report.html`,
`summary.json`, `status.json`, `manifest.json`, `runtime/`, per-job source receipts,
`2101-2018/failure-diagnostics.json` and `replay.py`. Old EPWs were preserved;
raw caches were reused without refresh, and new responses were stored separately.
Large weather outputs and caches remain outside Git. Replay source fingerprints
precede the documentation/formatting edits made for the commit.

Validation: **97 smhi2epw tests passed**, four skipped and four live-network tests
deselected; **236 workflow tests passed**, one skipped. The offline suite verifies
an entire 8,760-hour recovered EPW, bounded donor search, circular wind means,
invalid configuration before network access, preservation of observations and
the 48-hour temporal ceiling. The replay separately exercised live SMHI retrieval.
