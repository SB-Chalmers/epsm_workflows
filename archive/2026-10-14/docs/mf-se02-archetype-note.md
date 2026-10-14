# mf_se02 archetype review note

Recorded 2 October 2026. Scope: Gothenburg multifamily buildings from 1961–1975 with F ventilation. These are recommendations for reviewing the archetype, not approved catalogue changes or conclusions about the national stock.

The strongest candidate for a numerical correction is the existing-state window U-value: approximately **2.2 W/m²K instead of 2.8**. Before adopting it, verify the precise archetype source and that both values describe the whole window, including its frame. The TABULA Swedish decade averages are roughly 2.1–2.3 W/m²K; these broad averages do not establish every building's window performance. This recommendation does not apply to the standard or advanced renovation states.

## Other recommended changes

- **Basements:** represent basement heating separately from apartment heating. Verify heated extent and operating temperature rather than automatically assigning a full-footprint basement at 21°C. The sampled 17.6°C setting is a hypothesis, not an established default.
- **Renovation options:** support window replacement independently of wall and roof renovation. Replacing windows should not automatically imply better opaque insulation or lower infiltration.
- **Thermal bridges:** establish a documented allowance after checking what the construction inputs already include. Investigate the current zero uplift; do not adopt the sampled 15.7% from energy fit alone.
- **Ventilation:** retain 0.35 L/s/m² EPC Atemp provisionally, with mechanical ventilation separate from background infiltration. The sensitivity screen does not establish a better universal flow.
- **Geometry:** keep basement presence, heated extent and heated area explicit. Verify footprint, storeys and EPC linkage where simulated conditioned area differs substantially from EPC Atemp. Do not adjust geometry using heating residuals.

Keep wall and roof insulation, infiltration, soil properties and WWR unchanged pending independent evidence. Retain the implemented Kiva calculation method with its soil and foundation assumptions documented.

## Evidence from the joint sensitivity screen

Forty Latin hypercube combinations across twelve buildings and three states took 15.5 minutes. Ten buildings produced complete accepted results; the two previously identified numerical-warning buildings remained failed. Infiltration and material identities stayed fixed.

Ventilation and basement temperature were the most influential sampled parameters, followed by soil conductivity and windows. Insulation thickness was less influential within the tested bounds.

The fixed equal-state mixture's mean absolute error fell from 18.2 to 14.3 kWh/m², but coverage stayed at nine of ten buildings within 20% error and five within 10%. Excluding the extreme building, mean absolute error barely changed: 7.84 to 7.72 kWh/m². Standard and advanced shared settings worsened average error on the four buildings that did not select them. These inspected buildings are diagnostic data, not a fresh holdout.

The extreme building reports 125 m² EPC Atemp while the model contains about 237 m² conditioned gross floor area, including a full-footprint heated basement. Verify this representation before inferring envelope parameters; the discrepancy alone does not establish the correct geometry.

## Recommended order

1. Verify the window source and whole-window definition, then consider the approximately 2.2 W/m²K existing-state correction.
2. Resolve basement extent and temperature from building evidence.
3. Document thermal bridges and check for double counting.
4. Evaluate any frozen shared changes on new, property-separated buildings before promoting calibration results.

## References

- [TABULA appendix, Table 63](https://www.episcope.eu/downloads/public/docs/report/TABULA_FinalReport_AppendixVolume.pdf): Swedish decade averages, not an exact mf_se02 window prescription.
- [Joint sensitivity report](../output/lhs-sensitivity-mf-se02-2026-10-01/report.md).
- [Joint sensitivity protocol](lhs-sensitivity.md).

No catalogue or Core defaults were changed by this note.

## Context geometry

The pilot models the target building by itself. It does not include neighbouring-building shadows or shared-wall contacts. Core has support for explicit shading context and party-wall boundary assumptions, but these inputs are not populated in this study. A shading-only neighbour is an obstruction; it does not represent heat transfer to a neighbouring building or make a target wall adiabatic.

This is a building-specific geometry check, not a reason to add one permanent context factor to the archetype. For the diagnostic buildings, obtain nearby footprints, heights and distances, plus evidence of actual attached/shared walls. Compare no context, shading only, shared-wall treatment where verified, and both together. Keep weather, windows, WWR, materials, ventilation and all other settings fixed. Report each building's prediction and error change. Shading can reduce solar gains through windows; the direction and size of the resulting heating change depend on orientation, climate and the surrounding geometry. Shared walls can reduce exposed heat-loss area, depending on the adjacent building and wall construction.

Do not infer reduced infiltration or wind shelter from shading geometry alone. Treat those as separate inputs requiring evidence. Promote no neighbourhood assumption to a stock-wide default from this small, previously inspected cohort.

## Measured context shading result

On the four selected diagnostic buildings without heated basements, measured neighbour shading within 100 m increased existing-state simulated heat by 2.2–4.5 kWh/m² and worsened the error for all four. Standard and advanced results improved slightly for some buildings. Extending the radius to 200 m had almost no effect. Eight buildings with heated basements were not included after the basement smoke run triggered a new EnergyPlus strict-gate centroid warning; they need a safe, warning-free representation before testing context on those buildings. No shared-wall contacts met the independent evidence requirements. The small, previously inspected subset does not support a permanent archetype shading factor.

- [Measured context report](../output/context-diagnostics-mf-se02-2026-10-02/report.md)
