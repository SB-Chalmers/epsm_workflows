# Bounded EPC validation pilot

The [saved report](../../reports/epc-validation-2026-09-06/index.html) compares twelve
Gothenburg apartment buildings with EPC-reported district heat. It retains six
records per export schema, frozen inputs, source rows, publication cardinalities,
certificate history, weather provenance, all 24 simulation results and 36 geometry
renders. This is an exploratory study; it does not establish primary-energy,
individual-meter or renovation-response accuracy.

The initial screening covers 2,090 distinct certificates for 1955–1975 apartment
buildings in municipality 1480. It finds 228 exploratory candidates. Within each
schema, the most populated qualifying measurement year is selected (2017/2018);
a fixed SHA256 ordering chooses six distinct buildings. Building identities cannot
repeat across strata. No energy-error-based selection, replacement of failed runs,
parameter optimization or largest-ID/latest-certificate heuristic is used.

The source adapter corrects the old/new DHW accounting distinction. Newer workbook
descriptions mislabel EgenAtemp; supplied form 2.8, page 3, confirms Atemp in m² and
residential use as a percentage. The form's energy table describes normal-use
delivered energy. The mapping audit is specific to the fields used here and does
not claim verification of every export field.

The reusable comparison functions live in
[src/epsm_workflows/validation.py](../../src/epsm_workflows/validation.py).
They preserve allocated versus measured-basis records, reject mixed carriers and
invalid observations, require explicit delivery assumptions and calculate paired
annual errors. High-confidence study eligibility additionally requires individual
building metering, normal-use alignment and reconciled source totals.

## Reproduce

Use the editable v3 core/workflows environment with review dependencies, a local
smhi2epw checkout (or installation), and EnergyPlus 24.2. The geometry renderer is
the core checkout's development example, not a production reporting dependency.
Run from the workflows repository. Replace the paths below with your checkouts.
Use fresh scratch, study and simulation directories.

```sh
EPC_ROOT=/path/to/epc_join/data/epc
EPC_PUBLICATION=/path/to/epc_join/outputs/releases/1.0.0
EPC_SCRATCH=/tmp/epc-validation-inputs
EPC_STUDY=/path/to/new-study
EPC_CORE=/path/to/epsm_core
EPC_PYTHON="$EPC_CORE/.venv/bin/python"
EPC_EXAMPLES=examples/epc_validation

"$EPC_PYTHON" "$EPC_EXAMPLES/scan_sources.py" --epc-root "$EPC_ROOT" --output "$EPC_SCRATCH"
"$EPC_PYTHON" "$EPC_EXAMPLES/select_cohort.py" --inputs "$EPC_SCRATCH" --publication "$EPC_PUBLICATION"
```

Read both supplied mapping workbooks using `read_mappings.mjs EPC_ROOT EPC_SCRATCH`
with the available `@oai/artifact-tool` runtime. Copy the reader into the scratch
directory and link that directory's `node_modules` to the runtime's dependencies.
The reader writes the two mapping JSONs used by preparation. Do not overwrite the
Boverket workbooks.

```sh
"$EPC_PYTHON" "$EPC_EXAMPLES/compile_weather.py" --output "$EPC_SCRATCH" --years 2017 2018 --latitude 57.7089 --longitude 11.9746
"$EPC_PYTHON" "$EPC_EXAMPLES/prepare_study.py" --inputs "$EPC_SCRATCH" --output "$EPC_STUDY" --epc-root "$EPC_ROOT" --tmy "$EPC_CORE/epw/SWE_VG_Gothenburg-Landvetter.AP.025260_TMYx.2004-2018.epw"
"$EPC_PYTHON" "$EPC_EXAMPLES/audit_sources.py" --study "$EPC_STUDY" --publication "$EPC_PUBLICATION"
"$EPC_PYTHON" "$EPC_EXAMPLES/run_study.py" --study "$EPC_STUDY" --runs /tmp/epc-validation-new-runs --energyplus /path/to/energyplus --workers 4
"$EPC_PYTHON" "$EPC_EXAMPLES/report_study.py" --study "$EPC_STUDY" --core-examples "$EPC_CORE/examples"
```

Weather compilation performs read-only SMHI API requests and saves raw responses
in scratch. The study retains the EPWs and provenance. Large EnergyPlus SQLs remain
in the selected run directory; all small results, inputs and diagnostics are copied
to the report. Reuse the saved EPWs to reproduce the published weather inputs exactly.

`protocol.sha256` and each cohort entry's building JSON checksum prevent accidental
changes to frozen inputs before a run. The core separately records model, compiler,
engine and weather hashes. Report regeneration can reuse existing geometry PNGs;
delete those report-only PNGs first if deliberately changing the renderer.

## Interpret the result

Actual-year MAPE is 9.46%, compared with 15.23% for the existing Landvetter TMY.
Total heat bias is −9.87%; energy-intensity R² is −0.22. Similar annual totals
therefore do not establish useful building-specific differentiation. Older values
are allocated, all outlines are roof-edge proxies, and thermal fabric, heights,
basements, shading and net distribution losses remain assumed.

The TMY experiment changes both weather period and location. Its improvement
cannot be attributed solely to year matching. Net-loss sensitivity is a declared
parameter exercise, not a statistical uncertainty interval. Annual cross-building
CVRMSE is not an ASHRAE hourly or monthly calibration acceptance statistic.

Next: physically review the largest residuals, obtain meter and normal-use records,
implement dated EPC accounting, and predeclare an independent estate-separated
holdout across cities, eras and heating systems. Renovation claims additionally
need measured before/after response.
