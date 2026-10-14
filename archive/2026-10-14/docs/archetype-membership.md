# Comparing like building programmes

The current policy is GIS-led v2 (follow-up below). The initial audit and baseline
results remain preserved as v1 evidence; they have not been relabelled as a new run.

The previous filters did not establish consistent membership in a pure archetype
cohort. EPC category alone admitted ancillary/business buildings and mixed heated
programmes. National ingestion also omitted GIS purpose and accepted-link semantic
compatibility from the physical screen. These omissions are now corrected through
`epsm_workflows.archetype_membership`.

## Source-only gate

The policy `epc-gis-pure-programme-v1` checks the EPC category against the requested
use, reported dwelling count, primary and other heated-use percentages, positive
heated-garage area, all five GIS purpose fields, accepted-link semantic compatibility,
certificate-to-footprint count, source construction-era agreement and reported
ventilation against the assigned topology. Heating intensity and model residuals
never choose membership. The original raw row is checked by formular ID, line and
SHA-256 before omitted fields are recovered from the fixed source files.

`Friliggande` describes placement. A detached apartment block belongs in MF when
its use evidence is consistent; it is not reclassified as SF. Reported MF with
fewer than three dwellings is quarantined. `Småhus med flera lägenheter` is an
explicit cadastral grey zone and is retained for a separate reviewed cohort.
Small size is not a classification rule. A source conflict is not proof that a
specific replacement classification is correct.

The 100% primary-use requirement is a **pure-programme study policy**, not the
legal definition of a residential building. An MF building may legitimately contain
offices, shops or schools. Its envelope can remain MF while its heating model needs
the reported mixed programme. These buildings remain in the national inventory;
they cannot inherit accuracy evidence from a whole-building Sveby residential
programme. Compatible missing fields retain explicit caveats; consistent source
labels do not establish measurement-year operating conditions or heated extent.

Definitions: [Lantmäteriet, Handbok Byggnad](https://www.lantmateriet.se/globalassets/om-lantmateriet/var-samverkan-med-andra/byggnad-adress-lagenhet/handbok_byggnad.pdf)
and the existing Boverket field mapping
`epc_join/outputs/ubem-review-2026-09-11/dictionary_extract.json`.
[Boverket's EPC contents](https://www.boverket.se/sv/energideklaration/energideklaration/energideklarationens-innehall)
distinguish dwelling/premises programmes through Atemp. Current GIS descriptions
can disagree with historical EPCs; disagreements are preserved for review.

## Applied audit, 4 October 2026

All 262,280 eligible national records were compared with their exact accepted
EPC/footprint link and existing GIS purposes. Among EPC-labelled MF records,
1,124 have primary GIS purpose `Bostad;Småhus friliggande`; 631 of those report at
most two dwellings. Across all MF-labelled records, 4,677 report at most two
dwellings. These overlapping counts describe unresolved source consistency.

All 126 original SE.02 cohort records were re-read from their hash-verified source
rows: 93 report 100% residential use, 28 have mixed use and five omit residential
share and report zero dwellings. Three of the latter have GIS purpose
`Verksamhet;` and two `Komplementbyggnad;`. Secondary purposes and the other
checks leave **85 eligible buildings on 54 properties**; 41 are quarantined from
this pure comparison. The 12-building development pilot passes all membership
checks. Older 126-building results are retained as mixed-label diagnostics.

The unchanged v1.4 baseline on the reviewed 85-building cohort yields **37/85
below20%**, **14/85 below10%**, 84 accepted predictions and one native model
failure. MAE25.786 and bias+24.246 kWh/m²/year. The failed eligible model remains
in the denominator. This population change is not calibration improvement, and
the remaining error is substantial. Membership does not verify the actual heated
basement extent, area semantics, exposed walls or operation.

## Reproduction and records

```sh
python examples/epc_validation/audit_archetype_membership.py --study output/stock-national-2026-10-02-v2 --output output/archetype-membership-audit-2026-10-04
python -m epsm_workflows.cohort_transfer prepare --config configs/cohort-transfer-mf-se02-full.json --output output/NEW-reviewed-campaign
python examples/epc_validation/run_cohort_baseline.py --campaign output/NEW-reviewed-campaign
```

Both MF campaign configs now pin the existing publication for GIS/link validation.
Each preparation writes `membership-audit.json`, membership-stage exclusions and
separate source/membership/physics coverage counts. Native spec manifests cannot
bypass the source gate. National stock preparation retains raw EPC category,
GIS purposes and link compatibility; its candidate and calibration paths apply
the same predicate. Old source-frozen studies require a fresh preparation.

Audit records, national row-level flags, suspect IDs, source hashes and summaries:
`output/archetype-membership-audit-2026-10-04/`. Reviewed baseline:
`output/cohort-transfer-mf-se02-full-2026-10-04-v5-membership-reviewed/`.
The former pilot-v5/full-v4 completion statuses are marked interrupted; there are
no active calibration jobs claimed by this audit. The full reviewed calibration
has not been completed or promoted to a supported profile.

## GIS-led assignment and mixed programmes (2026-10-04 follow-up)

Policy `gis-led-epc-validated-pure-programme-v2` explicitly assigns the residential
SF/MF archetype family from all five GIS purpose fields. The inventory retains the
reported EPC `use_group`; `gis_use_group`, `gis_assignment_status` and
`assigned_archetype_use` preserve the assignment independently. National MF simulation
candidates now select the GIS-assigned family and still require EPC consistency.
Broad/missing residential GIS labels no longer qualify for pure SF/MF calibration.
Specific source conflicts remain quarantined; the EPC is never silently relabelled.
Broad `Verksamhet` cannot distinguish offices from other business uses, so specific
nonresidential programme selection still requires the EPC heated-area shares and
compatible GIS evidence. Multiple GIS purposes never imply equal area weights.

EPSM Core now offers `ProgramComponent`/`ProgramBlend` for explicit heated-area mixtures.
This does **not** automatically readmit all mixed records: every positive programme
needs an available sourced profile, valid shares, compatible system/control inputs
and a declared effective-zone assumption, or separate native zone assignments.
The previous 85-building baseline belongs to the earlier v1 policy and source
identity; reprepare before new evaluation. No historical result has been rewritten.

The source-only v2 national audit retains 131,976 SF, 37,522 MF, 2,281 office and
7,501 school records as pure-cohort eligible; these are coverage counts, not
validated simulation accuracy. Workflows `program_blend_from_epc(row, programmes,
source=...)` builds a Core blend from existing percentages, where `programmes` maps
use names to `(UseSpec, AirflowSpec, profile_source)` tuples. No weights are inferred
from GIS ordering and no unsupported positive use is silently replaced. Resolving
the blend checks calendars and operational-system compatibility.

Core implementation/documentation commit: `940fdaa`. Full Core validation: 625 passed,
9 skipped; Workflows membership/stock/cohort/mixture checks: 76 passed.
