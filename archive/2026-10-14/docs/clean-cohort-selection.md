# Cleaner cohort selection — 2 October 2026

## Recommendation

Use the five prepared Gothenburg multifamily buildings from 1996–2005 (`mf_se05`) for a small diagnostic baseline. This is a cleaner comparison of residential operation and above-ground geometry, not evidence of better heating predictions. It changes the archetype family, so it cannot establish that `mf_se02` was corrected.

All five have one-to-one accepted newer EPC links, measured positive district-heating space heat, a complete calendar-year period, exclusively F ventilation, 100% reported residential use, no reported heated basement floors, no positive heated garage area, and a detached building label. Height and supported pitched-ridge roof observations pass the existing geometry screen. Registry construction year, when available, must agree with the EPC era. Gross external footprint area times above-ground storeys is 6–11% above EPC Atemp. Geometry is never rescaled to match EPC area.

The properties are absent from all 126 buildings in the earlier study. Selection used building attributes and data eligibility, never heating intensity or residuals. Heating must be positive to establish a usable measured target; valid positive tails were not trimmed.

| Building ID prefix | Construction year | Measurement year | EPC Atemp, m² | Gross area / Atemp |
|---|---:|---:|---:|---:|
| c5ffafda | 1998 | 2018 | 442 | 1.114 |
| d80cea53 | 1998 | 2018 | 441 | 1.112 |
| 521c4e24 | 2002 | 2020 | 1,196 | 1.114 |
| d47f13dc | 2004 | 2023 | 676 | 1.064 |
| 5d6232cd | 2004 | 2023 | 449 | 1.079 |

## What was checked

The screen started from 7,244 accepted Gothenburg multifamily certificates. The strict flat-roof, reported-flow, existing-weather screen produced only nine eligible older buildings, all already examined. Broadening to supported pitched roofs, retaining unknown designed flow explicitly, and allowing other measurement years produced seven eligible fresh `mf_se05` buildings on three properties. Five passed Core geometry preparation; two failed with near-coincident consecutive surface vertices. They remain recorded failures, without heating conclusions.

The selected buildings are diagnostic data. There is no train/validation/test allocation because five buildings on three properties cannot support a credible calibration experiment and fail the original 120-building preparation gate. The selection workflow reports this explicitly.

Existing parser and sampling tests passed, including explicit later-era parsing without changing the original pilot's default year bounds. Geometry preflight checks scene preparation using the historical pilot builder's three state constructions; it does not compile or simulate `mf_se05` models. Actual new models must use `core_inputs("mf_se05", state)` and its corresponding airflow catalogue values.

## Remaining work before a baseline simulation

- Only one building reports designed ventilation flow (0.35 L/s/m²). Use a declared common assumption and sensitivity for the other four; do not infer flows from energy errors.
- Pitched roofs have unreported attic heating status. Document the attic assumption and test its uncertainty.
- Zero *heated* basement floors does not rule out an unheated basement or establish the foundation geometry.
- Independently check the EPC detached label against neighbouring footprints before assuming fully exposed walls.
- Pin and audit 2020 measurement-year AMY weather for `521c4e24`; the existing weather set covers 2018 and 2023.
- Compile all three frozen `mf_se05` states with Core Kiva, run a smoke model, then report each building's errors separately. No calibration has been run or authorised by this selection.

## Artifacts

Final selection: `output/clean-cohort-gothenburg-mf-2026-10-02-v3/` contains cohort and eligible rows, exclusions, source hashes, geometry preflight and summary. Earlier empty/screening attempts are retained in the unversioned and `-v2` directories. `src/epsm_workflows/clean_cohort.py` reproduces the final screen and refuses to overwrite an existing output directory.
