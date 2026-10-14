# Regional construction evidence for MF

These original tables establish plausible directions, not surviving-stock mean U
values. Construction permit timing and actual assemblies must precede assignment.
The present three-zone BBR18 descriptor is not the four-zone historical SBN map.

[SBN 1967](https://www.boverket.se/contentassets/c4c3f9ae57294ae889bfaf710b08b125/sbn-1967.pdf),
table 33:121, printed page 200 (PDF page 202), specifies maximum coefficients in
kcal/(m²·h·°C). Comparing its northern zone I and southern zone IV gives the
following ratios of required total resistance:

| Construction | North/south total R ratio |
|---|---:|
| Solid brick wall | 1.375 |
| Other heavy wall, above 100 kg/m² | 1.333 |
| Light wall, at most 100 kg/m² | 1.250 |
| Roof with stone bearing construction | 1.250 |
| Roof with timber bearing construction | 1.143 |

Table 33:1221 addresses glass-area coefficients, not whole-window U. Its values
cannot directly validate the catalogue's whole-window 2.2 W/(m²·K). Different
basement use categories also have different requirements. This standard took
effect in 1968, so it does not describe all SE.02 construction years.

[SBN 1975, edition 2](https://www.boverket.se/contentassets/c4c3f9ae57294ae889bfaf710b08b125/sbn-1975-utg-2.pdf),
Supplement 1, table 33:21, printed/PDF page 189, gives these main component
requirements for rooms heated above 18°C:

| Component | Historical zones I/II U | Historical zones III/IV U | North/south R ratio |
|---|---:|---:|---:|
| External wall | 0.25 | 0.30 | 1.200 |
| Roof/attic assembly | 0.17 | 0.20 | 1.176 |
| Whole window including frame | 2.00 | 2.00 | 1.000 |

U values here are W/(m²·K). Section 33:212 permits component tradeoffs within
separate limits when total envelope transmission meets the requirement. The
preface dates these thermal provisions to 1977, with permit transitions and a
separate window transition; a 1976 construction-year boundary alone is insufficient.
Section 33:222 permits different fabric for rooms heated between 10 and 18°C.
An assumed basement-temperature reduction therefore does not independently
establish the basement assembly.

Both tables were visually checked against the scanned originals. They justify
testing moderate, period-specific regional resistance differences. They do not
justify applying R+40% to every MF era, every wall type or every renovation state.
Convert resistance into added insulation using each assembly's fixed conductivity:
`added_depth = (R_factor − 1) × existing_total_R × insulation_conductivity`.
The campaign exports the exact added depth and material for review. Where no
insulation layer exists, structural thickness must not be scaled; a separate,
sourced retrofit assembly is required.
