"""Publish concise current-only paper text from hash-bound analysis and figures.

Usage: python -m examples.report_weather_campaign_paper CAMPAIGN/ANALYSIS
No simulations are invoked. Counts and numeric findings come from the verified
tables rather than a manually maintained manuscript. Earlier reports stay intact.
"""

from __future__ import annotations

import argparse
import html
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from examples import plot_weather_campaign_summary as S


def publish(root):
    """Bind text and all four figures to one completed automatic-only analysis."""
    root = Path(root).resolve()
    raw = (root / "manifest.json").read_bytes()
    manifest = json.loads(raw)
    if manifest["status"] != "complete" or manifest.get("incomplete"):
        raise ValueError("Paper requires complete analysis")
    figures = root / "summary-figures"
    figure_raw = (figures / "manifest.json").read_bytes()
    plots = json.loads(figure_raw)
    if (
        plots["status"] != "complete"
        or plots["method"] != "automatic"
        or plots["analysis_manifest_sha256"] != S.sha(raw)
    ):
        raise ValueError("Paper figures do not belong to this automatic-only analysis")
    for name, expected in plots["outputs"].items():
        if S.sha((figures / name).read_bytes()) != expected:
            raise ValueError("Paper figure or summary differs from its receipt")
    rows = S.automatic_rows(
        S.read_table(root, manifest, "deduplicated-comparisons.csv")
    )
    summary = S.read_table(figures, plots, "load-error-summary.csv")
    timing = S.read_table(figures, plots, "peak-timing-summary.csv")
    events = S.read_table(figures, plots, "weather-event-summary.csv")
    qualification = json.loads((root / "diagnostics-qualification.json").read_bytes())
    if (
        S.sha((root / "diagnostics-qualification.json").read_bytes())
        != manifest["outputs"]["diagnostics-qualification.json"]
    ):
        raise ValueError("Paper diagnostic policy differs from the analysis receipt")
    n = len(rows)
    panels = len(rows[S.PHYSICAL].drop_duplicates())
    years = len(rows[["site_id", "year"]].drop_duplicates())
    regions = rows.site_id.nunique()
    if (
        n != plots["building_responses"]
        or panels != plots["physical_weather_masks"]
        or years != plots["site_years"]
    ):
        raise ValueError("Paper counts differ from the figure cohort")
    warned = sum(
        r.get("reviewed_psychrometric_warning_retained", False)
        for r in qualification["records"]
    )
    peak = timing.loc[
        timing.scope.eq("meteorology")
        & timing.sampling.eq("weather_extremes")
        & timing.gap_hours.eq(48)
    ].set_index("mode")
    heat = peak.loc["heating"]
    cool = peak.loc["cooling"]
    max48 = summary.loc[
        summary.gap_hours.le(48) & summary.metric.str.startswith("absolute_annual"),
        "empirical_p95",
    ].max()

    def tail(scope, mode):
        selected = summary.loc[
            summary.scope.eq(scope)
            & summary.sampling.eq("random_seasonal")
            & summary.gap_hours.eq(168)
            & summary.metric.eq(f"absolute_annual_{mode}_error_kwh_m2")
        ]
        if len(selected) != 1:
            raise ValueError("Paper annual error stratum is ambiguous")
        return float(selected.empirical_p95.iloc[0])

    wave = events.loc[events.event_type.eq("heatwave") & events.gap_hours.eq(12)].iloc[
        0
    ]
    method = f"""
    <p>Weather inputs are supplied in EnergyPlus Weather (EPW) format. Typical Meteorological Year (TMY) files are obtained for the closest available location in the <a href="https://climate.onebuilding.org/sources/default.html">OneBuilding climate database</a>. For simulations requiring year-specific conditions, including comparisons with reported EPC space-heating demand, Actual Meteorological Year (AMY) files are assembled using SMHI2EPW. These combine SMHI meteorological observations with available radiation measurements and modelled radiation from <a href="https://strang.smhi.se/">SMHI STRÅNG</a>. Missing values are completed automatically using temporal reconstruction, assessed observations from other stations and ERA5 reanalysis, subject to physical consistency checks and documented warnings.</p>
    <p>The sensitivity of simulation results to weather completion was assessed using controlled synthetic outages. The design targeted six Swedish regions over 2018, 2021 and 2023, yielding 17 eligible reference AMYs. Small pre-existing gaps were completed once and held fixed. Six synthetic building variants were generated using EPSM Core, representing older and newer detached and multifamily buildings, an office and a school. Meteorological and solar data were withheld separately in blocks of 3, 12, 48 and 168 hours. Outages were placed randomly across the four seasons or around observed temperature extremes and heatwaves or cold spells. Random cases contained four blocks per weather file; event cases contained one.</p>
    <p>Each reconstructed AMY was simulated alongside its reference using EnergyPlus 24.2, keeping the building model fixed. Comparisons assessed annual sensible heating and cooling demand, hourly peak magnitude and timing, and weather-event preservation. Demand and peak magnitudes were normalised by conditioned floor area. Known, previously reviewed transient psychrometric warnings were retained, with archived hourly sensible loads checked for completeness, finite values and agreement with recorded annual totals, peak magnitudes and peak-hour sets. The analysis comprises <strong>{n:,} paired building comparisons across {panels:,} outage scenarios and {years} reference weather-years in {regions} regions</strong>. Medians and empirical 95th percentiles describe sensitivity within these tested cases.</p>
    """
    results = f"""
    <p>Annual demand errors generally increased with outage length. Across the meteorological and solar groups and both outage designs, the largest stratum-specific 95th-percentile absolute annual heating or cooling error for gaps up to 48 hours per block was <strong>{max48:.3f} kWh/m²</strong>. For seasonal random 168-hour gaps, the 95th-percentile heating/cooling errors were <strong>{tail("meteorology", "heating"):.3f}/{tail("meteorology", "cooling"):.3f} kWh/m² for meteorological gaps</strong> and <strong>{tail("solar", "heating"):.3f}/{tail("solar", "cooling"):.3f} kWh/m² for solar gaps</strong>. Individual cases exceeded these percentiles.</p>
    <p>Peak timing was more sensitive than annual totals. For event-centred, 48-hour meteorological outages covering a positive reference peak, exact peak-hour sets were retained in <strong>{int(heat.preserved_peak_hour_sets)}/{int(heat.exposed_positive_peaks)} heating comparisons</strong> and <strong>{int(cool.preserved_peak_hour_sets)}/{int(cool.exposed_positive_peaks)} cooling comparisons</strong>. These fractions concern exposed peaks; outages outside the reference peak are excluded from the timing denominator.</p>
    <p>Detecting a weather event did not guarantee unchanged intensity. Following 12-hour event-centred meteorological outages, <strong>{int(wave.exposed_events_with_exact_bounds)}/{int(wave.reference_exposed_events)} exposed heatwave comparisons</strong> retained their boundaries, while <strong>{int(wave.exposed_events_with_exact_bounds_and_peak)}/{int(wave.reference_exposed_events)}</strong> retained both exact boundaries and peak temperature. Weather checks use all 17 eligible source-years. The results describe selected records, outages and six synthetic ideal-load profiles, rather than general uncertainty bounds for Swedish buildings. Solar component references include model-based proxies.</p>
    """
    body = "<h1>Weather completion sensitivity — revised analysis</h1><p class='note'>Known, reviewed transient warnings are retained. The earlier blanket warning-exclusion analysis remains archived.</p>"
    body += (
        "<h2>Weather data</h2>"
        + method
        + "<h2>Results: sensitivity to weather completion</h2>"
        + results
    )
    for name, title in (
        ("automatic-annual-errors", "Annual heating and cooling demand"),
        ("automatic-peak-errors", "Hourly peak magnitude"),
        ("automatic-peak-timing", "Peak timing where the reference peak is exposed"),
        ("automatic-weather-events", "Weather-event preservation"),
    ):
        body += f"<h2>{title}</h2><figure><img src='summary-figures/{name}.png' alt='{title}'><figcaption><a href='summary-figures/{name}.svg'>SVG</a> · <a href='summary-figures/{name}.pdf'>PDF</a></figcaption></figure>"
    body += f"<h2>Diagnostic scope and reproducibility</h2><p>{warned:,} unique annual runs retain the bounded warnings already reviewed by Core. Severe/fatal errors, unreviewed warnings, pending reviews and incomplete engines remain excluded. The original diagnostics and Core receipts are unchanged.</p>"
    body += "<p>Core's original warning exception is scoped to annual space heating. Retention of sensible cooling and peak results is the study's descriptive analysis policy, supported by the user's prior assessment and additional hourly reconciliation; it does not extend Core's accuracy qualification to cooling, humidity control or latent loads. Reconciliation checks numeric consistency, not a counterfactual estimate of the warnings' effects. The models have no humidity control.</p>"
    body += "<p>The study models use unlimited ideal sensible heating/cooling and a fixed 18°C conditioned-floor boundary. Solar component references are hybrid measured-GHI/STRÅNG or STRÅNG-only proxies. Region labels refer to selected stations, not city centres. Building peak duration and weather-event peak-hour timing were not evaluated. Peak timing requires all tied positive peak hours to match; zero-load peaks are excluded.</p>"
    body += "<p>Heatwaves require at least three complete days with daily Tmax ≥25°C; cold spells require three days with Tmin ≤−10°C; high wind means hourly mean ≥10 m/s. These are study definitions. Event episodes can recur across outage scenarios; six building responses share each weather record.</p>"
    body += f"<p>The full campaign has {qualification['raw_building_rows']:,} named outcome rows from {qualification['unique_simulations']:,} unique annual runs, including other controlled methods and aliases. The paper reports the automatic-only comparisons above.</p>"
    body += "<p>Data: <a href='summary-figures/load-error-summary.csv'>load errors</a> · <a href='summary-figures/building-error-summary.csv'>building profiles</a> · <a href='summary-figures/peak-timing-summary.csv'>peak timing</a> · <a href='summary-figures/weather-event-summary.csv'>weather events</a> · <a href='manifest.json'>analysis provenance</a> · <a href='diagnostics-qualification.json'>warning review evidence</a>.</p>"
    body += "<p>Reanalysis: <a href='https://doi.org/10.1002/qj.3803'>ERA5</a>, accessed through <a href='https://open-meteo.com/en/docs/historical-weather-api'>Open-Meteo</a>. The tested SMHI2EPW snapshot is commit <code>5930c07c987bdbdd19c01be8d86a1fc0710c361e</code>; cite the published name/version if renamed epsm_weather.</p>"
    page = (
        "<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Weather completion — revised paper</title><style>body{max-width:1050px;margin:40px auto;padding:0 24px;color:#182534;font:17px/1.65 system-ui,sans-serif}h1{font-size:30px;line-height:1.25}h2{font-size:24px;margin-top:36px}a{color:#0072b2}figure{margin:30px 0}img{width:100%;height:auto}figcaption,.note{font-size:14px;color:#45566a}.note{padding:16px;background:#f2f6f9;border-left:4px solid #0072b2}</style>"
        + body
        + "</html>\n"
    )
    path = root / "paper.html"
    path.write_text(page)
    receipt = {
        "status": "complete",
        "created_utc": datetime.now(UTC).isoformat(),
        "analysis_manifest_sha256": S.sha(raw),
        "figure_manifest_sha256": S.sha(figure_raw),
        "publisher_sha256": S.sha(Path(__file__).read_bytes()),
        "policy": qualification["policy"],
        "retained_reviewed_warning_runs": warned,
        "automatic_building_responses": n,
        "physical_masks": panels,
        "reference_site_years": years,
        "regions": int(regions),
        "outputs": {"paper.html": S.sha(path.read_bytes())},
    }
    text_path = root / "paper-methods-results.txt"
    text_path.write_text(
        "Weather data\n\n"
        + html.unescape(re.sub("<[^>]+>", "", method)).strip()
        + "\n\nResults\n\n"
        + html.unescape(re.sub("<[^>]+>", "", results)).strip()
        + "\n"
    )
    receipt["outputs"][text_path.name] = S.sha(text_path.read_bytes())
    (root / "paper-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("analysis", type=Path)
    args = parser.parse_args()
    print(json.dumps(publish(args.analysis), indent=2))


if __name__ == "__main__":
    main()
