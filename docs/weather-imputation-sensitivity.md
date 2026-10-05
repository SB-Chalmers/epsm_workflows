# Weather completion sensitivity: Gothenburg 2023

**Completed 5 October 2026:** 20 weather scenarios, 17 unique annual simulations,
using EPSM Core and EnergyPlus 24.2. All final simulations passed Core diagnostics
with zero warnings, severe errors or fatal errors. One pinned Core compiler
identity and one byte-identical compiled building were used throughout. All
35 EPW fields remain identical outside synthetic gaps.

Full local [report](../output/weather-imputation-sensitivity-2026-10-05-v1/report.html),
[demand catalogue](../output/weather-imputation-sensitivity-2026-10-05-v1/summary.csv),
[event catalogue](../output/weather-imputation-sensitivity-2026-10-05-v1/event-preservation.csv)
and [audit](../output/weather-imputation-sensitivity-2026-10-05-v1/audit.json)
are accompanied by EPWs, source receipts, hourly loads and Core building/model
files. The data directory is excluded from Git. The reproducible driver and
instructions are in [examples/weather_imputation_sensitivity](../examples/weather_imputation_sensitivity/README.md).

## Reference and controls

The reference uses quality-approved 2023 observations at Göteborg-Landvetter
(72420), measured GHI at Sol station 71415, and STRÅNG beam partitioning, at
57.7089°N, 11.9746°E with a common target elevation of 154 m. It needs only the
existing bounded temporal method for its native gaps: 23 temperature, 29 humidity,
27 pressure and 26 hours in each wind variable on the buffered grid, roughly
0.30% of required meteorological cells. Native fills are frozen for all cases.
Only originally observed, quality-approved cells are synthetically hidden;
hidden observations and native fills are excluded from donor fitting.

The fixed donor is Göteborg A (71420), an urban station around 1 km from the
requested point, at approximately 3 m elevation. Its annual raw temperature is
about 2.23°C warmer than Landvetter; the experiment reconstructs the airport
reference, not independently measured city-building weather. Both station QFF
sources use the same target-pressure conversion; ERA5 surface pressure is used
directly in Pa at the same target height.

EPSM Core `BuildingSpec` creates a 10×10×3 m insulated concrete box: one zone,
20% window-to-wall ratio, glazing U=1.8 W/(m²K), SHGC=0.55, 0.3 ACH constant
infiltration, two occupants, 3 W/m² lighting and equipment gains each. All gain
fractions are constant; heating/cooling setpoints are 20/26°C. Ideal loads have
no flow/capacity limits. The prescribed ground boundary is the explicit Core
18°C default, held constant and uncalibrated. The experiment reports **delivered
sensible thermal demand**, excluding HVAC electricity, fuel, DHW and plant/fan
energy. Peaks are hourly means, not subhourly design capacities.

Four gaps per panel cover the reference heating/cooling peak hours and spring/
autumn conditions. Outages last 3, 48 or 168 hours per event: 12, 192 or 672 hours
in total. Comparisons within each panel use identical masks. Forced donor/ERA5
alternatives at short gaps expose method sensitivity; production normally uses
temporal filling first. A 168-hour temporal alternative is explicitly unsupported.
The automatic case fixes the donor identity and uses the production hierarchy.

Solar outages are separate from meteorological outages. Their GHI/direct groups
are replaced coherently, with physical nighttime zeros. Erbs cases retain GHI
and therefore test a different information-loss scenario. Optional cloud cover
is absent and remains absent in all cases, preventing ERA5 from changing
longwave radiation outside the masks. Solar error metrics compare with the
hybrid reference, not independently observed DNI/DHI.

## Demand and peak timing

Reference annual heating is **4483.72 kWh** and cooling **1682.43 kWh**.
Heating peaks at **2.37585 kW**, 30 November 07:00 LST; cooling at **2.53519 kW**,
14 June 15:00 LST. All times use hour-ending fixed UTC+1, without DST.

Selected results below; the CSV/HTML contains all 20 scenarios and both absolute
and percentage demand/peak differences. Positive timing shifts mean a later peak.

| Completion / outage per event | Heating kWh | Cooling kWh | Heating peak Δ% | Cooling peak Δ% | Heating peak shift h | Cooling peak shift h |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Reference | 4483.72 | 1682.43 | 0.00 | 0.00 | 0 | 0 |
| Daily profiles / 48 h | 4486.74 | 1673.71 | −0.89 | −2.91 | −5 | 0 |
| Raw donor / 168 h | 4427.61 | 1737.26 | −5.94 | +11.74 | +119 | +1 |
| Assessed donor / 168 h | 4481.53 | 1682.14 | +1.34 | +0.91 | +45 | +1 |
| ERA5 meteorology / 168 h | 4463.87 | 1692.36 | −0.42 | +4.46 | +73 | 0 |
| Guarded solar profiles / 48 h | 4486.00 | 1693.91 | −3.45 | +2.19 | +73 | +24 |
| ERA5 solar / 168 h | 4475.30 | 1684.13 | +2.25 | −7.39 | 0 | −47 |

For week-long meteorological outages, raw borrowing changes annual heating by
−1.25% and cooling by +3.26%; assessed borrowing reduces those to −0.049% and
−0.017%. Its heating peak still moves **45 hours**, and the cooling peak moves
one hour. ERA5 preserves the cooling peak hour in all meteorological panels,
while heating peaks move by 2–73 hours. Three-hour linear filling alone shifts
both peak hours by one hour despite annual changes below 0.01%.

Automatic matches temporal reconstruction at 3/48 hours and assessed donors at
168 hours for these pinned sources. The identical cases reuse simulations.
Annual agreement can hide event errors and altered peak timing; it is not a
sufficient validation criterion.

## Heatwaves and other events

These are transparent operational study definitions, not official SMHI warnings:
heatwaves require daily maximum ≥25°C for at least three consecutive complete
days; cold spells require daily minimum ≤−10°C for three days. Relative spells
use fixed reference-season daily percentiles: warm Tmax95=23.72°C during May–
September and cold Tmin5=−8.39°C during October–April. Hour 24 belongs to its
preceding EPW label date. Wind episodes use ≥10 m/s; temperature jumps use
>5°C/hour. Source boundaries, solar differences and physical solar limits are
reported separately.

The reference has a **four-day heatwave, 14–17 June** (maximum 25.9°C), and a
**three-day cold spell, 27–29 November** (minimum −13.0°C). It also has a
four-day relative warm spell, eight-day relative cold spell, 60 high-wind
episodes totalling 153 hours, and one original temperature jump above 5°C/hour.

| Meteorological completion | Heatwave days | Cold-spell days | Relative warm days | Relative cold days |
| --- | ---: | ---: | ---: | ---: |
| Reference | 4 | 3 | 4 | 8 |
| Daily profiles / 48 h | 0 | 4 | 4 | 8 |
| Raw donor / 168 h | 4 | 0 | 6 | 0 |
| Assessed donor / 168 h | 0 | 0 | 4 | 8 |
| ERA5 / 168 h | 4 | 0 | 4 | 3 |

Daily-profile and assessed-donor completions can cross an absolute classification
threshold while retaining the relative warm spell. Small annual demand errors
therefore do not prove preservation of an extreme event. Week-long raw-donor
and ERA5 replacements each introduce an additional >5°C/hour temperature jump.
Detailed event overlap, duration, peak changes, new/lost episodes, source
boundaries and wind differences are retained in `anomalies.json`.

All final candidates pass nighttime-solar and broad 2,000 W/m² checks. The solar
comparison uses the production darkness guard; an earlier standalone profile
prototype that missed this guard is explicitly excluded under `diagnostics/`.
Independent checks recomputed every weather-error and load-metric record and
verified every unmasked EPW data row. Twenty-six synthetic tests cover calendar/
event definitions, pinned thresholds, missing-truth exclusion, pressure units,
darkness, and zero-demand metric handling.

## Interpretation

The study supports keeping simple donor assessment/bias correction and same-year
ERA5 fallback, while assessing peak/event preservation in addition to annual
demand. It does not support selecting a universally best method from one building,
one year and four event-centred masks. Airport/urban exposure, a prescribed
uncalibrated ground boundary, constant infiltration/gains and absent cloud data
limit transfer to real buildings; native fills are shared background, not observed
truth. Precipitation anomalies are not evaluated because this AMY input does not
supply usable precipitation observations.

No production recovery algorithm or existing campaign has been changed by the
study. Raw SMHI and same-year [ERA5 via Open-Meteo](https://open-meteo.com/en/docs/historical-weather-api)
responses, source-code snapshots, model and engine fingerprints allow the results
to be traced and reproduced offline.
