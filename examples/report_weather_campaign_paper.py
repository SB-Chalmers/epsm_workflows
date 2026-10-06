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


def plain_paragraphs(markup):
    """Keep manuscript paragraphs readable when copied without HTML formatting."""
    paragraphs = re.findall(r"<p>(.*?)</p>", markup, flags=re.DOTALL)
    return "\n\n".join(
        " ".join(html.unescape(re.sub("<[^>]+>", "", p)).split()) for p in paragraphs
    )


def publish(root):
    """Bind a reader-facing uncertainty report to completed automatic-only evidence."""
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
    events = S.read_table(figures, plots, "weather-event-summary.csv")
    relative = S.read_table(figures, plots, "relative-annual-error-summary.csv")
    temperature = S.read_table(figures, plots, "event-temperature-uncertainty.csv")
    peak_timing = S.read_table(figures, plots, "exposed-peak-timing-uncertainty.csv")
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
    profiles = rows.building_id.nunique()
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
    short_pct = relative.loc[
        relative.gap_hours.le(12), "empirical_p95_absolute_error_pct"
    ].max()
    max48 = summary.loc[
        summary.gap_hours.le(48) & summary.metric.str.startswith("absolute_annual"),
        "empirical_p95",
    ].max()
    peak = peak_timing.loc[
        peak_timing.scope.eq("meteorology")
        & peak_timing.sampling.eq("weather_extremes")
        & peak_timing.gap_hours.eq(48)
    ].set_index("mode")
    heat = peak.loc["heating"]
    cool = peak.loc["cooling"]
    shifted_cool_pct = 100 * cool.beyond24h_fraction
    solar_week_peak = peak_timing.loc[
        peak_timing.scope.eq("solar")
        & peak_timing.sampling.eq("weather_extremes")
        & peak_timing.gap_hours.eq(168)
        & peak_timing["mode"].eq("cooling")
    ]
    if len(solar_week_peak) != 1:
        raise ValueError("Paper cooling peak-time stratum is ambiguous")
    solar_week_peak = solar_week_peak.iloc[0]

    def tail(scope, mode, *, percent=False):
        table = relative if percent else summary
        selected = table.loc[
            table.scope.eq(scope)
            & table.sampling.eq("random_seasonal")
            & table.gap_hours.eq(168)
            & (
                table["mode"].eq(mode)
                if percent
                else table.metric.eq(f"absolute_annual_{mode}_error_kwh_m2")
            )
        ]
        if len(selected) != 1:
            raise ValueError("Paper annual error stratum is ambiguous")
        field = "empirical_p95_absolute_error_pct" if percent else "empirical_p95"
        return float(selected[field].iloc[0])

    def same_dates(event, hours):
        selected = events.loc[events.event_type.eq(event) & events.gap_hours.eq(hours)]
        if len(selected) != 1:
            raise ValueError("Paper weather-event stratum is ambiguous")
        r = selected.iloc[0]
        return int(r.exposed_events_with_exact_bounds), int(r.reference_exposed_events)

    wave_same, wave_total = same_dates("heatwave", 12)
    wave_temperature = temperature.loc[
        temperature.event_type.eq("heatwave")
        & temperature.sampling.eq("weather_extremes")
        & temperature.gap_hours.eq(12)
    ]
    if len(wave_temperature) != 1:
        raise ValueError("Paper heatwave-temperature stratum is ambiguous")
    wave_temperature = wave_temperature.iloc[0]
    cold_temperature = temperature.loc[
        temperature.event_type.eq("cold_spell")
        & temperature.sampling.eq("weather_extremes")
        & temperature.gap_hours.eq(168)
    ]
    if len(cold_temperature) != 1:
        raise ValueError("Paper cold-spell-temperature stratum is ambiguous")
    cold_temperature = cold_temperature.iloc[0]
    percent_note = "Percentage cooling errors exclude reference demand below 1 kWh/m²; those cases remain in the absolute-error results."
    percentile_note = "The higher-end error shown here is the 95th percentile: 95% of tested comparisons in a group fall at or below it; some are larger. This is an empirical summary, not a confidence interval or a guaranteed maximum."
    method = f"""
    <p>Weather inputs were selected to match the purpose of the building simulation. Typical Meteorological Year (TMY) files were obtained for the closest available location in the <a href="https://climate.onebuilding.org/sources/default.html">OneBuilding climate database</a>. Year-specific Actual Meteorological Year (AMY) files were assembled using SMHI2EPW from SMHI observations, available radiation measurements and modelled radiation from <a href="https://strang.smhi.se/">SMHI STRÅNG</a>. The library automatically completed missing observations using temporal reconstruction, assessed records from other stations and ERA5 reanalysis, with physical consistency checks and warnings identifying reconstructed data.</p>
    <p>The additional uncertainty introduced by weather completion was quantified by withholding available data and comparing reconstructed files with their reference AMYs. The design targeted six Swedish regions in 2018, 2021 and 2023; {years} reference weather-years were eligible. Small pre-existing gaps were completed once and held fixed. Six synthetic building variants were generated using EPSM Core: older and newer detached and multifamily buildings, an office and a school. Meteorological and solar data were withheld separately for 3, 12, 48 and 168 hours. Seasonal cases contained four gaps per year, one in each season; event cases contained one gap centred on a temperature extreme, heatwave or cold spell.</p>
    <p>Each reconstructed AMY and its reference were simulated with the same building in EnergyPlus 24.2, isolating the effect of gap completion. The {n:,} paired building comparisons, spanning {panels:,} distinct outage scenarios, assessed annual sensible heating and cooling demand, hourly peak magnitude and timing, and weather-event dates and temperature extremes. Demand and peak magnitude were normalised by conditioned floor area. Absolute errors and empirical 95th percentiles were evaluated separately by missing weather component, outage length and placement, pooling the selected locations and building profiles within each group. Percentage annual errors were also reported, excluding cooling references below 1 kWh/m² to avoid unstable percentages. This experiment quantifies completion uncertainty, rather than total weather or building-model uncertainty.</p>
    """
    results = f"""
    <p>Automatic weather completion preserved annual demand closely for the tested short outages. For 3–12-hour gaps, the largest group-specific 95th-percentile annual heating or cooling error was {short_pct:.2f}%, using the cooling threshold defined above. Across gaps up to 48 hours per block, the largest corresponding absolute error was {max48:.3f} kWh/m². These results support the use of reconstructed AMYs for annual-demand comparisons within the tested conditions, while providing an explicit error scale rather than assuming that filled observations are equivalent to measured data.</p>
    <p>Long gaps in solar data introduced substantially more uncertainty into cooling demand. With a week-long gap in each season, the 95th-percentile annual cooling error was {tail("solar", "cooling"):.1f} kWh/m² ({tail("solar", "cooling", percent=True):.1f}%) for solar completion, compared with {tail("meteorology", "cooling"):.2f} kWh/m² ({tail("meteorology", "cooling", percent=True):.1f}%) for meteorological completion. Heating errors for these cases were {tail("solar", "heating"):.2f} and {tail("meteorology", "heating"):.2f} kWh/m², respectively. Thus, the missing weather component matters alongside the duration of the gap: a file suitable for annual heating analysis can still carry appreciable uncertainty for cooling.</p>
    <p>Annual agreement did not establish preservation of demand peaks. When a two-day meteorological gap covered a reference peak, the annual maximum moved by more than a day in {int(heat.beyond24h_peaks)}/{int(heat.exposed_positive_peaks)} heating comparisons and {int(cool.beyond24h_peaks)}/{int(cool.exposed_positive_peaks)} cooling comparisons. With an event-centred week-long solar gap, this occurred in {int(solar_week_peak.beyond24h_peaks)}/{int(solar_week_peak.exposed_positive_peaks)} cooling comparisons. The winning annual maximum can switch between nearly equal episodes; a time shift does not imply an equally large magnitude error or a displaced weather event. Peak magnitude and timing therefore need to be assessed together.</p>
    <p>Preserving a heatwave’s dates also did not guarantee preservation of its intensity. All {wave_same}/{wave_total} exposed heatwave comparisons retained their start and end dates following 12-hour meteorological gaps, while the absolute change in maximum temperature had a median of {wave_temperature.median_abs_peak_temperature_error_c:.1f}°C and a 95th percentile of {wave_temperature.empirical_p95_abs_peak_temperature_error_c:.1f}°C. Week-long gaps also affected cold extremes: the 95th-percentile minimum-temperature error was {cold_temperature.empirical_p95_abs_peak_temperature_error_c:.1f}°C among {int(cold_temperature.matched_exposed_events)}/{int(cold_temperature.exposed_reference_events)} exposed cold spells with a unique reconstructed match; unmatched episodes were excluded from that temperature-error calculation.</p>
    <p>The automatic completion thus maintained annual demand closely for short interruptions, with identifiable uncertainty in cooling, extreme temperatures and peak timing when the relevant observations were missing. This provides a basis for qualifying the completed weather file according to its intended use. The reported percentiles describe selected records and synthetic buildings; they are not national uncertainty bounds or universal acceptance thresholds.</p>
    """
    body = f"""
    <header><p class="eyebrow">Weather completion · uncertainty quantified</p>
    <h1>How much do filled weather gaps change a building’s results?</h1>
    <p class="lead">Short gaps had a small effect on annual demand in this experiment. Longer gaps in sunlight data, and gaps covering heating or cooling peaks, need closer attention.</p>
    <p class="context">{n:,} paired comparisons · {panels:,} outage scenarios · {years} weather-years · {regions} Swedish regions · {profiles} synthetic building profiles</p></header>
    <section class="cards" aria-label="Main findings">
      <article><strong>{short_pct:.2f}%</strong><h2>Short gaps: small annual errors</h2><p>Largest 95th-percentile annual-demand error for 3–12-hour gaps, across the tested groups.</p></article>
      <article><strong>{tail("solar", "cooling", percent=True):.1f}%</strong><h2>Long solar gaps: cooling is more uncertain</h2><p>95th-percentile annual cooling error with a week of sunlight data missing in each season.</p></article>
      <article><strong>{shifted_cool_pct:.0f}%</strong><h2>Peak timing needs its own check</h2><p>Cooling comparisons where the annual peak moved by more than a day after a two-day meteorological gap covering the peak.</p></article>
    </section>
    <p class="reading-note">{percentile_note} {percent_note} <a href="#scope">Read the study scope.</a></p>
    <aside class="implication"><h2>What this means for using the weather file</h2><p>The automatic completion performed well for annual demand after short gaps in this experiment. For peak-load or extreme-weather questions, the temperature and peak checks matter too. Qualify a completed file by the missing component, gap length and gap placement, then judge the measured error against the needs of the application.</p></aside>
    <h2 class="section-heading">Four questions the experiment answers</h2>
    """
    story = (
        (
            "automatic-annual-errors",
            "1. How much does filling gaps change annual heating and cooling?",
            "Annual demand changed little for short gaps. Longer gaps in sunlight data raised cooling uncertainty.",
            "Seasonal cases: four gaps per year, one in each season. Errors are percentages of each building’s reference annual demand. The dot shows the middle case; the line ends at the 95th-percentile error. Cooling references below 1 kWh/m² are excluded from percentages and retained in the absolute-error supplement. Heating and cooling panels use their own scales. Each comparison uses the same building with reference and reconstructed weather.",
        ),
        (
            "automatic-peak-errors",
            "2. How wrong can the heating or cooling peak become?",
            "A gap covering the demand peak deserves separate scrutiny, even when the annual total changes little.",
            "Event-centred cases: one gap per year. Only comparisons with a positive reference peak covered by the gap are shown. Errors compare the annual hourly-mean peak magnitude with its reference; gaps away from the peak do not dilute this result. The dot shows the middle case; the line ends at the 95th percentile.",
        ),
        (
            "automatic-peak-timing",
            "3. Can the annual peak move by more than a day?",
            "Filling an extreme-hour gap can change which episode produces the year’s highest demand.",
            "Event-centred cases with a positive reference peak covered by the gap. The chart counts comparisons where the closest reconstructed annual peak is more than 24 hours from a reference peak. One day is a readable descriptive scale, not an acceptance threshold. Nearly equal demand episodes can exchange the annual maximum; this does not mean the weather event itself moved. Interpret timing alongside peak magnitude.",
        ),
        (
            "automatic-weather-events",
            "4. Are heatwaves and cold spells preserved in timing and intensity?",
            "An episode can remain identifiable on the same dates while its temperature extreme changes.",
            "Meteorological gaps centred on temperature events. The first panel counts exposed reference episodes retaining both start and end dates. Temperature errors in the second panel include only episodes with a unique reconstructed match; missing or split episodes are excluded from those errors and remain represented in the date comparison. The dot shows the middle temperature error; the line ends at the 95th percentile. The same episode can be tested by several outage scenarios. Heatwaves require at least three days with daily maximum ≥25°C; cold spells require three days with daily minimum ≤−10°C. These are study definitions.",
        ),
    )
    for name, question, answer, caption in story:
        absolute_links = (
            "<span class='downloads'>Absolute annual errors, including low-cooling cases: <a href='summary-figures/automatic-annual-errors-absolute.png'>PNG</a> · <a href='summary-figures/automatic-annual-errors-absolute.svg'>SVG</a> · <a href='summary-figures/automatic-annual-errors-absolute.pdf'>PDF</a></span>"
            if name == "automatic-annual-errors"
            else ""
        )
        body += f"""<section class="story"><h2>{question}</h2><p class="answer">{answer}</p><figure><img src="summary-figures/{name}.png" alt="{html.escape(question)}"><figcaption>{caption} <span class="downloads"><a href="summary-figures/{name}.svg">Download SVG</a> · <a href="summary-figures/{name}.pdf">PDF</a></span>{absolute_links}</figcaption></figure></section>"""
    body += f"""
    <section class="manuscript"><h2>Manuscript text</h2><p><a href="paper-methods-results.txt">Download the methods and results as plain text</a>.</p><h3>Weather data and uncertainty assessment</h3>{method}<h3>Results and interpretation</h3>{results}</section>
    <details id="scope"><summary>Study scope: what the evidence establishes</summary>
    <p>{percentile_note} {percent_note}</p>
    <p>The experiment isolates the additional uncertainty caused by filling weather gaps by keeping each building fixed. It does not quantify station representativeness, climate variability, EPC reporting errors or the total uncertainty of the building model. Seasonal cases hide four blocks per year; event cases hide one. These designs have different numbers of missing hours and should not be interpreted as a controlled comparison of placement alone.</p>
    <p>The six synthetic profiles represent selected combinations of geometry, fabric, glazing, thermal mass and schedules; they are not calibrated buildings. The models use unlimited ideal sensible heating and cooling, no humidity control, and a fixed 18°C conditioned-floor boundary. Reported loads exclude latent energy, system efficiency and purchased energy. Hourly-mean peaks are not design sizing loads; overheating temperatures and building peak duration were not evaluated.</p>
    <p>The reference AMYs are observationally based, with bounded pre-existing gaps fixed before synthetic outages. Solar component references combine measured global radiation with modelled STRÅNG components, or use STRÅNG-only proxies. This experiment evaluates completion against those reference files; it does not independently validate all solar components. Region names refer to selected stations, not city centres. One of the 18 planned location-years was unavailable. Six buildings share each weather case, and repeated event exposures are not independent climate events.</p>
    <p>The timing figure uses the absolute nearest annual-peak separation; tied peak hours can give a zero separation even when the complete set of peak hours changes. The original exact-hour-set results are retained in the downloadable data. Zero-load peaks are excluded. A suitable error tolerance depends on the intended application; none is imposed here.</p></details>
    <details><summary>Data, simulation checks and reproducibility</summary>
    <p>{warned:,} unique annual runs retain bounded transient psychrometric warnings already reviewed by EPSM Core. Archived hourly sensible loads were checked for a complete time grid, finite values, and agreement with recorded annual totals, peak magnitudes and peak-hour sets. Severe or fatal errors, unreviewed warnings, pending reviews and incomplete runs remain excluded. Original diagnostic receipts are preserved.</p>
    <p>Core's original warning exception is scoped to annual space heating. Retaining sensible cooling and peak results is the study's descriptive analysis policy, supported by additional hourly reconciliation; it does not extend Core's accuracy qualification to cooling, humidity control or latent loads. Numeric reconciliation is not a counterfactual estimate of warning effects.</p>
    <p>The wider campaign contains {qualification["raw_building_rows"]:,} named outcome rows from {qualification["unique_simulations"]:,} unique annual runs, including controlled alternative methods and aliases. This report uses only the {n:,} automatic-completion comparisons.</p>
    <p>Download data: <a href="summary-figures/load-error-summary.csv">annual and peak errors</a> · <a href="summary-figures/relative-annual-error-summary.csv">percentage annual errors</a> · <a href="summary-figures/building-error-summary.csv">building profiles</a> · <a href="summary-figures/exposed-peak-error-summary.csv">covered-peak errors</a> · <a href="summary-figures/exposed-peak-timing-uncertainty.csv">peak-time separation</a> · <a href="summary-figures/peak-timing-summary.csv">exact peak hours</a> · <a href="summary-figures/weather-event-summary.csv">event dates</a> · <a href="summary-figures/event-temperature-uncertainty.csv">event temperature errors</a> · <a href="manifest.json">analysis provenance</a> · <a href="diagnostics-qualification.json">warning review evidence</a>.</p>
    <p>Reanalysis: <a href="https://doi.org/10.1002/qj.3803">ERA5</a>, accessed through <a href="https://open-meteo.com/en/docs/historical-weather-api">Open-Meteo</a>. The tested SMHI2EPW snapshot is commit <code>5930c07c987bdbdd19c01be8d86a1fc0710c361e</code>; cite the published name and version if renamed epsm_weather.</p></details>
    """
    style = """
    :root{--ink:#182a38;--muted:#526270;--blue:#006e99;--line:#dce4e8;--soft:#eef5f7}
    *{box-sizing:border-box}body{max-width:1120px;margin:0 auto;padding:52px 28px 70px;color:var(--ink);font:17px/1.65 system-ui,sans-serif;background:#fff}a{color:var(--blue)}header{max-width:920px}.eyebrow{font-size:13px;letter-spacing:.08em;text-transform:uppercase;font-weight:700;color:var(--blue)}h1{font-size:clamp(32px,4vw,46px);line-height:1.13;letter-spacing:-.025em;margin:16px 0 24px;max-width:850px}.lead{font-size:22px;line-height:1.5;max-width:920px}.context{font-size:14px;color:var(--muted);margin:24px 0 34px}.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:18px;margin:24px 0 16px}.cards article{padding:24px;border:1px solid var(--line);border-top:5px solid var(--blue);border-radius:5px}.cards article:nth-child(2){border-top-color:#b37a10}.cards article:nth-child(3){border-top-color:#826787}.cards strong{display:block;font-size:43px;line-height:1.1;margin-bottom:18px}.cards h2{font-size:18px;line-height:1.3;margin:0 0 12px}.cards p{font-size:15px;line-height:1.5;margin:0}.reading-note,figcaption{font-size:14px;line-height:1.6;color:var(--muted)}.implication{margin:32px 0 54px;padding:24px 28px;background:var(--soft);border-radius:5px}.implication h2{font-size:21px;margin:0 0 10px}.implication p{margin:0}.section-heading{font-size:27px;margin:0 0 36px}.story{margin:0 0 58px}.story h2{font-size:25px;line-height:1.3;margin:0 0 12px}.answer{font-size:19px;line-height:1.5;margin:0 0 20px;max-width:900px}figure{margin:0}img{width:100%;height:auto}figcaption{padding:12px 4px 0;max-width:980px}.downloads{display:block;margin-top:8px}.manuscript{border-top:1px solid var(--line);padding-top:28px;margin-top:50px}.manuscript h2{font-size:27px}.manuscript h3{font-size:22px;margin-top:34px}.manuscript p{max-width:920px}details{border-top:1px solid var(--line);padding:20px 0;margin-top:20px;font-size:15px;color:var(--muted)}summary{cursor:pointer;color:var(--ink);font-size:18px;font-weight:650}details p{max-width:950px}code{overflow-wrap:anywhere;font-size:13px}@media(max-width:720px){body{padding:28px 18px 45px}.cards{grid-template-columns:1fr}.cards article{padding:20px}.cards strong{font-size:34px}.lead{font-size:20px}.story h2{font-size:22px}.implication{padding:22px}.context{line-height:1.8}}
    """
    page = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Weather completion: quantified uncertainty</title><style>"
        + style
        + "</style></head><body>"
        + body
        + "</body></html>\n"
    )
    path = root / "paper.html"
    path.write_text(page)
    text_path = root / "paper-methods-results.txt"
    text_path.write_text(
        "Weather data and uncertainty assessment\n\n"
        + plain_paragraphs(method)
        + "\n\nResults and interpretation\n\n"
        + plain_paragraphs(results)
        + "\n"
    )
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
        "outputs": {p.name: S.sha(p.read_bytes()) for p in (path, text_path)},
    }
    (root / "paper-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("analysis", type=Path)
    args = parser.parse_args()
    print(json.dumps(publish(args.analysis), indent=2))


if __name__ == "__main__":
    main()
