"""Publish a readable synthesis of a completed, verified weather campaign.

Run after the independent analysis and plotter. This adds a report and its
provenance receipt without changing the frozen simulation runtime or evidence.
Incomplete results are refused; use the analysis tool for provisional checks.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

METHODS = {
    "automatic": "Current hierarchy",
    "automatic_legacy": "Previous hierarchy",
    "donor_assessed": "Assessed donor",
    "temporal": "Temporal profiles",
    "era5": "ERA5",
}


def sha(data):
    """Bind a report to the exact bytes it consumed."""
    return hashlib.sha256(data).hexdigest()


def table(root, manifest, name):
    """Read only a table whose bytes match the independent analysis receipt."""
    data = (root / name).read_bytes()
    if sha(data) != manifest["outputs"].get(name):
        raise ValueError(f"Analysis table differs from its receipt: {name}")
    try:
        return pd.read_csv(io.BytesIO(data))
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def finite(values):
    """Keep undefined percentages separate from zero physical error."""
    return (
        pd.to_numeric(values, errors="coerce")
        .replace([np.inf, -np.inf], np.nan)
        .dropna()
    )


def number(value):
    """Represent absent descriptive quantities as null."""
    return float(value) if pd.notna(value) and np.isfinite(value) else None


def paired_digest(pairs):
    """Compare identical physical MET masks, separating sampling and duration."""
    records = []
    for (sampling, hours), group in pairs.groupby(["sampling", "gap_hours"]):
        for mode in ("heating", "cooling"):
            record = {
                "sampling": sampling,
                "gap_hours": int(hours),
                "mode": mode,
                "paired_building_responses": len(group),
                "site_years": len(group[["site_id", "year"]].drop_duplicates()),
                "locations": group.site_id.nunique(),
            }
            for quantity, unit, tolerance in (
                ("annual", "kwh_m2", 1e-8),
                ("peak", "w_m2", 1e-6),
                ("hourly", "w_m2", 1e-6),
            ):
                metric = (
                    f"hourly_{mode}_load_mae_w_m2"
                    if quantity == "hourly"
                    else f"absolute_{quantity}_{mode}_error_{unit}"
                )
                change = finite(group[metric + "_change"])
                record[quantity + "_improved_n"] = int(change.lt(-tolerance).sum())
                record[quantity + "_worsened_n"] = int(change.gt(tolerance).sum())
                record[quantity + "_tied_n"] = int(change.abs().le(tolerance).sum())
                for side in ("new", "old"):
                    values = finite(group[metric + "_" + side])
                    record[f"{quantity}_{side}_mean_{unit}"] = number(values.mean())
                    record[f"{quantity}_{side}_p95_{unit}"] = number(
                        values.quantile(0.95)
                    )
                    means = (
                        group.assign(_value=group[metric + "_" + side])
                        .groupby(["site_id", "year"])
                        ._value.mean()
                    )
                    record[f"{quantity}_{side}_equal_site_year_mean_{unit}"] = number(
                        means.mean()
                    )
            eligible = group[f"baseline_peak_{mode}_w_m2"].gt(1e-6)
            exposed = group[f"baseline_{mode}_peak_hidden_new"].eq(True) & eligible
            record["peak_exposed_n"] = int(exposed.sum())
            record["peak_unexposed_n"] = int((eligible & ~exposed).sum())
            record["zero_or_numerical_reference_peak_n"] = int((~eligible).sum())
            for side in ("new", "old"):
                same = group[f"same_{mode}_peak_hours_{side}"].eq(True)
                record[f"peak_hours_preserved_{side}_exposed_n"] = int(
                    (exposed & same).sum()
                )
                record[f"peak_hours_preserved_{side}_exposed_fraction"] = number(
                    same[exposed].mean()
                )
            records.append(record)
    return pd.DataFrame(records)


def display(data):
    """Escape data values and keep small quantities visible in report tables."""
    return (
        '<div class="table">'
        + data.to_html(
            index=False,
            escape=True,
            border=0,
            na_rep="—",
            float_format=lambda v: f"{v:.4g}",
        )
        + "</div>"
    )


def verify_qualification(root, analysis, manifest, comparisons, common, pairs):
    """Recheck scoped diagnostics and prevent excluded runs leaking into outcomes."""
    name = "diagnostics-qualification.json"
    expected = manifest["outputs"].get(name)
    if expected is None:
        return None
    data = (analysis / name).read_bytes()
    if (
        sha(data) != expected
        or manifest.get("diagnostics_qualification_sha256") != expected
    ):
        raise ValueError("Diagnostics qualification differs from its analysis receipt")
    saved = json.loads(data)
    if saved.get("evidence_mode") != "strict_immutable_files":
        raise ValueError("Final diagnostics require immutable simulation evidence")
    from examples import qualify_weather_campaign_results as Q

    captured = []
    for path in sorted((root / "datasets").glob("*/*/summary.csv")):
        raw = path.read_bytes()
        if manifest["inputs"].get(str(path)) != sha(raw):
            raise ValueError("Raw diagnostic input differs from captured analysis")
        captured.append(pd.read_csv(io.BytesIO(raw)))
    raw_rows = pd.concat(captured, ignore_index=True) if captured else pd.DataFrame()
    annotated, excluded, verified = Q.qualify_results(root, raw_rows)
    for key in (
        "policy",
        "evidence_mode",
        "qualifier_script_sha256",
        "raw_building_rows",
        "qualified_building_rows",
        "excluded_building_rows",
        "unique_simulations",
        "qualified_simulations",
        "excluded_simulations",
        "exclusion_reason_counts",
    ):
        if saved.get(key) != verified.get(key):
            raise ValueError(f"Diagnostics qualification mismatch: {key}")
    if (
        verified["raw_building_rows"] != manifest["captured_building_rows"]
        or verified["qualified_building_rows"]
        != manifest.get("qualified_building_rows")
        or verified["excluded_building_rows"] != manifest.get("excluded_building_rows")
    ):
        raise ValueError("Raw, qualified and excluded diagnostic counts disagree")
    references = annotated.loc[
        annotated.method.eq("reference") & annotated.engine_outcome_qualified
    ]
    if len(references) != manifest.get("qualified_reference_building_rows") or len(
        references[["site_id", "year"]].drop_duplicates()
    ) != manifest.get("qualified_reference_site_years"):
        raise ValueError("Qualified reference building and site-year counts disagree")
    for path, expected_sha in verified["inputs"].items():
        if (
            saved["inputs"].get(path) != expected_sha
            or manifest["inputs"].get(path) != expected_sha
        ):
            raise ValueError("Diagnostics qualification evidence differs")
    if saved["inputs"] != verified["inputs"]:
        raise ValueError("Diagnostics qualification evidence set differs")
    key = "canonical_simulation_path"
    if len(saved["records"]) != saved["unique_simulations"] or len(
        {r[key] for r in saved["records"]}
    ) != len(saved["records"]):
        raise ValueError("Duplicate or missing canonical simulation qualifications")
    if {r[key]: r for r in saved["records"]} != {
        r[key]: r for r in verified["records"]
    }:
        raise ValueError("Canonical simulation qualifications differ")
    identity = ["site_id", "year", "building_id", "case_id"]
    expected_exclusions = set(
        map(tuple, excluded[identity].itertuples(index=False, name=None))
    )
    captured_exclusions = table(analysis, manifest, "diagnostic-exclusions.csv")
    if (
        len(captured_exclusions) != len(excluded)
        or set(
            map(tuple, captured_exclusions[identity].itertuples(index=False, name=None))
        )
        != expected_exclusions
    ):
        raise ValueError(
            "Diagnostic exclusion catalogue differs from screened evidence"
        )
    eligible = set(
        map(
            tuple,
            annotated.loc[annotated.engine_outcome_qualified, identity].itertuples(
                index=False, name=None
            ),
        )
    )
    for outcomes in (comparisons, common):
        if outcomes.empty:
            continue
        if (
            "engine_outcome_qualified" not in outcomes
            or not outcomes.engine_outcome_qualified.eq(True).all()
            or not set(
                map(tuple, outcomes[identity].itertuples(index=False, name=None))
            ).issubset(eligible)
        ):
            raise ValueError("Unqualified engine outcomes entered building comparisons")
    for side in ("new", "old"):
        column = "case_id_" + side
        if column not in pairs:
            raise ValueError("Paired outcomes lack their qualified case identities")
        paired_ids = pairs[identity[:-1] + [column]].rename(columns={column: "case_id"})
        if not set(map(tuple, paired_ids.itertuples(index=False, name=None))).issubset(
            eligible
        ):
            raise ValueError("Unqualified engine outcomes entered hierarchy pairs")
    return saved


def publish(root):
    """Refuse partial evidence and write a provenance-bound scientific summary."""
    root = Path(root).resolve()
    analysis = root / "analysis"
    manifest_bytes = (analysis / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    coverage_bytes = (root / "coverage.json").read_bytes()
    coverage = json.loads(coverage_bytes)
    matrix_bytes = (root / "matrix.json").read_bytes()
    if (
        manifest["status"] != "complete"
        or Path(manifest["campaign_root"]).resolve() != root
        or manifest.get("incomplete")
        or not coverage.get("complete_full_matrix")
        or coverage["comparison_rows"] != manifest["captured_building_rows"]
        or manifest["matrix_sha256"] != sha(matrix_bytes)
    ):
        raise ValueError("A completed analysis matching the final campaign is required")
    for name, expected in manifest["inputs"].items():
        if sha(Path(name).read_bytes()) != expected:
            raise ValueError(f"Captured campaign evidence changed: {name}")
    pairs = table(analysis, manifest, "hierarchy-pairs.csv")
    comparisons = table(analysis, manifest, "deduplicated-comparisons.csv")
    available = table(analysis, manifest, "method-availability.csv")
    common = table(analysis, manifest, "common-method-comparisons.csv")
    events = table(analysis, manifest, "event-exposure-summary.csv")
    sources = table(analysis, manifest, "sources.csv")
    if (
        len(pairs) != manifest["hierarchy_pairs"]
        or len(comparisons) != manifest["deduplicated_comparisons"]
    ):
        raise ValueError("Analysis row counts differ from their manifest")
    audits, audit_hashes = [], {}
    for path in sorted((root / "datasets").glob("*/*/audit.json")):
        data = path.read_bytes()
        audit = json.loads(data)
        if not audit.get("full_matrix_site_year_complete"):
            raise ValueError("An incomplete site/year audit cannot enter the synthesis")
        if not audit.get("diagnostics_accepted") or any(
            audit.get(key) != 0 for key in ("severe", "fatal")
        ):
            raise ValueError("Accepted campaign diagnostics must be clean")
        audits.append(audit)
        audit_hashes[str(path.relative_to(root))] = sha(data)
    if len(audits) != coverage["location_years_completed_full_matrix"]:
        raise ValueError("Completed audit count differs from final coverage")
    if sum(a["comparisons"] for a in audits) != coverage["comparison_rows"]:
        raise ValueError("Completed audits and captured comparison counts differ")
    qualification = verify_qualification(
        root, analysis, manifest, comparisons, common, pairs
    )
    raw_warnings = sum(a["warnings"] for a in audits)
    if raw_warnings and qualification is None:
        raise ValueError(
            "Accepted campaign diagnostics must be clean or strictly screened"
        )
    plot_path = analysis / "figures/manifest.json"
    plot_bytes = plot_path.read_bytes()
    plots = json.loads(plot_bytes)
    if plots["status"] != "complete" or plots["analysis_manifest_sha256"] != sha(
        manifest_bytes
    ):
        raise ValueError("Final figures must use this completed analysis")
    for name, expected in plots["outputs"].items():
        if sha((plot_path.parent / name).read_bytes()) != expected:
            raise ValueError(f"Figure differs from its provenance receipt: {name}")
    event_plot_path = analysis / "event-figures/manifest.json"
    event_plot_sha = None
    if event_plot_path.exists():
        event_plot_bytes = event_plot_path.read_bytes()
        event_plots = json.loads(event_plot_bytes)
        if event_plots["status"] != "complete" or event_plots[
            "analysis_manifest_sha256"
        ] != sha(manifest_bytes):
            raise ValueError("Final event figure must use this completed analysis")
        for name, expected in event_plots["outputs"].items():
            if sha((event_plot_path.parent / name).read_bytes()) != expected:
                raise ValueError(
                    f"Event figure differs from its provenance receipt: {name}"
                )
        event_plot_sha = sha(event_plot_bytes)
    method_plot_path = analysis / "method-figures/manifest.json"
    method_plot_sha = None
    if method_plot_path.exists():
        method_plot_bytes = method_plot_path.read_bytes()
        method_plots = json.loads(method_plot_bytes)
        if method_plots["status"] != "complete" or method_plots[
            "analysis_manifest_sha256"
        ] != sha(manifest_bytes):
            raise ValueError("Final method figures must use this completed analysis")
        for name, expected in method_plots["outputs"].items():
            if sha((method_plot_path.parent / name).read_bytes()) != expected:
                raise ValueError(
                    f"Method figure differs from its provenance receipt: {name}"
                )
        method_plot_sha = sha(method_plot_bytes)
    meteorology_pairs = pairs.loc[pairs.scope.eq("meteorology")].copy()
    if meteorology_pairs.empty:
        raise ValueError("No matched meteorological hierarchy evidence")
    digest = paired_digest(meteorology_pairs)
    digest_path = analysis / "hierarchy-digest.csv"
    digest.to_csv(digest_path, index=False)
    unique_runs = sum(a["unique_simulations_accumulated"] for a in audits)
    title = "Swedish weather completion: building and event sensitivity"
    body = f"<h1>{title}</h1><p class='lead'>Weather references meeting source criteria: {len(audits)}/18 planned "
    body += f"location-years, {coverage['reference_building_location_years']}/108 "
    body += f"raw evaluated reference building combinations and {unique_runs:,} unique annual "
    body += "EnergyPlus simulations. Unsupported reference years remain explicit.</p>"
    if qualification is not None:
        body += f"<p>{manifest['qualified_reference_building_rows']:,} warning-free reference building combinations across "
        body += f"{manifest['qualified_reference_site_years']:,} location-years qualify for building-outcome analysis. "
        body += "These qualified reference counts differ from raw evaluations when reference runs have diagnostics.</p>"
    body += f"<p>{manifest['deduplicated_comparisons']:,} non-reference building "
    body += f"responses after duplicate physical masks are removed; {len(pairs):,} "
    body += f"paired current/previous-hierarchy responses ({len(meteorology_pairs):,} meteorological pairs). Building outcomes retained for inference have "
    body += "zero EnergyPlus warnings, severe errors and fatal errors.</p>"
    if qualification is not None:
        body += f"<p>The raw catalogue contains {qualification['raw_building_rows']:,} building responses; "
        body += f"{qualification['qualified_building_rows']:,} qualify for building-outcome analysis and "
        body += f"{qualification['excluded_building_rows']:,} are excluded. "
        body += f"{qualification['excluded_simulations']:,} canonical simulations have unqualified diagnostics. "
        body += "Every warning excludes all outcomes conservatively, including annual heating that Core accepts under a heating-only exception. "
        body += "A flagged reference also excludes related comparisons for that building/year. Physically valid weather and anomaly comparisons remain available. "
        body += "Original results and diagnostic warnings are preserved: "
        body += "<a href='analysis/diagnostic-exclusions.csv'>excluded outcomes</a>, "
        body += "<a href='analysis/diagnostics-qualification.json'>qualification evidence</a>.</p>"
    body += "<p>The current hierarchy uses observations → interpolation for gaps "
    body += "up to 3 hours → assessed same-year donors → bounded daily profiles "
    body += "up to 48 hours → same-year ERA5. Donors must pass blocked holdout "
    body += "and engineering checks against original observations. Their limited "
    body += "median-offset correction is retained only when holdout error improves.</p>"
    body += "<h2>Current versus previous hierarchy</h2><p>The comparison below "
    body += "pairs the same original evidence, weather mask and building. Both "
    body += "hierarchies use the same donor assessment and physical derivation; "
    body += "the controlled change is recovery priority. Annual "
    body += "errors use kWh/m² and peak errors W/m². The 95th percentiles describe "
    body += "the selected responses and are not population confidence limits. "
    body += "Separate seasonal random panels from deliberately selected event "
    body += "outages; three-hour and long-gap pairs may legitimately be identical.</p>"
    body += "<p>Duration means the gap length per block. Each seasonal-random "
    body += "weather file contains four blocks, one in each season, withholding "
    body += "4× that many hours in total. Each weather-event file contains one "
    body += "block, withholding 1× that many hours. Method comparisons are "
    body += "paired within each sampling stratum; differences between random "
    body += "and event error magnitudes also reflect these different exposure "
    body += "budgets, not only the severity of the weather.</p>"
    view = digest[
        [
            "sampling",
            "gap_hours",
            "mode",
            "paired_building_responses",
            "site_years",
            "annual_new_equal_site_year_mean_kwh_m2",
            "annual_old_equal_site_year_mean_kwh_m2",
            "annual_new_p95_kwh_m2",
            "annual_old_p95_kwh_m2",
            "annual_improved_n",
            "annual_worsened_n",
            "annual_tied_n",
            "peak_new_p95_w_m2",
            "peak_old_p95_w_m2",
        ]
    ].rename(
        columns={
            "sampling": "Outage selection",
            "gap_hours": "Hours",
            "mode": "Load",
            "paired_building_responses": "Pairs",
            "site_years": "Site-years",
            "annual_new_equal_site_year_mean_kwh_m2": "Annual mean current¹",
            "annual_old_equal_site_year_mean_kwh_m2": "Annual mean previous¹",
            "annual_new_p95_kwh_m2": "Annual p95 current",
            "annual_old_p95_kwh_m2": "Annual p95 previous",
            "annual_improved_n": "Annual better",
            "annual_worsened_n": "Annual worse",
            "annual_tied_n": "Annual tied",
            "peak_new_p95_w_m2": "Peak p95 current",
            "peak_old_p95_w_m2": "Peak p95 previous",
        }
    )
    body += display(view)
    body += "<p>¹Mean absolute error, first averaged within each site-year and "
    body += "then averaged with equal site-year weights. Better/worse/tied refer "
    body += "to absolute error against each reference simulation, not measured "
    body += "building demand. Full precision and hourly errors: "
    body += "<a href='analysis/hierarchy-digest.csv'>paired digest</a>, "
    body += "<a href='analysis/hierarchy-pairs.csv'>paired responses</a>.</p>"
    for name, caption in (
        ("paired-annual-errors", "Absolute annual errors and percentage errors"),
        ("paired-peak-errors", "Absolute hourly peak errors and percentage errors"),
        (
            "building-profile-changes",
            "Different building responses to identical weather changes",
        ),
        (
            "exposed-peak-timing",
            "Peak timing conditional on reference peaks actually hidden",
        ),
    ):
        body += f"<figure><img src='analysis/figures/{name}.png' alt='{caption}'>"
        body += f"<figcaption>{caption}. <a href='analysis/figures/{name}.svg'>"
        body += "Vector figure</a></figcaption></figure>"
    body += "<h2>Available fallback methods</h2><p>Availability is counted once "
    body += "per physical weather outage, independently of buildings. Source "
    body += "absence, donor rejection, unsupported truth and temporal gaps above "
    body += "48 hours remain visible. A successful automatic file can combine "
    body += "methods by parameter even when an all-variable donor reconstruction "
    body += "is unavailable. Forced methods below use only identical masks "
    body += "successfully evaluated for every scheduled method; this cohort "
    body += "can be much smaller than the full automatic comparison.</p>"
    body += display(
        available[
            [
                c
                for c in (
                    "scope",
                    "sampling",
                    "gap_hours",
                    "method",
                    "planned_distinct_panels",
                    "truth_ready_panels",
                    "available_weather_panels",
                    "unavailable_weather_panels",
                    "not_prepared_panels",
                    "unsupported_truth_panels",
                    "unsupported_method_panels",
                    "panels_with_all_buildings_evaluated",
                    "panels_with_all_buildings_qualified",
                    "engine_unqualified_panels",
                )
                if c in available
            ]
        ]
    )
    forced = []
    for keys, group in common.groupby(["scope", "sampling", "gap_hours", "method"]):
        item = dict(zip(["Scope", "Selection", "Hours", "Method"], keys))
        item["Method"] = METHODS.get(item["Method"], item["Method"])
        item["Responses"] = len(group)
        item["Site-years"] = len(group[["site_id", "year"]].drop_duplicates())
        for mode in ("heating", "cooling"):
            item[f"{mode} annual p95, kWh/m²"] = number(
                finite(group[f"absolute_annual_{mode}_error_kwh_m2"]).quantile(0.95)
            )
            item[f"{mode} peak p95, W/m²"] = number(
                finite(group[f"absolute_peak_{mode}_error_w_m2"]).quantile(0.95)
            )
        forced.append(item)
    body += display(pd.DataFrame(forced))
    if method_plot_sha is not None:
        for scope, caption in (
            ("meteorology", "Meteorological methods on common eligible masks"),
            ("solar", "Solar methods against the hybrid radiation reference"),
        ):
            name = f"common-method-{scope}-errors"
            body += f"<figure><img src='analysis/method-figures/{name}.png' alt='{caption}'>"
            body += (
                f"<figcaption>{caption}. <a href='analysis/method-figures/{name}.svg'>"
            )
            body += "Vector figure</a></figcaption></figure>"
    body += "<h2>Heatwaves and other anomalies</h2><p>Absolute heatwaves require "
    body += "at least three complete local-standard-time days with Tmax ≥25°C; "
    body += "cold spells require Tmin ≤−10°C. Relative warm/cold spells, wind "
    body += "≥10 m/s, temperature jumps above 5°C/hour and solar physics checks "
    body += "are also catalogued. Each anomaly is evaluated once per physical "
    body += "weather outage/method. Lost and new events are episode counts. "
    body += "Any overlap is a lenient match; exact boundaries and exact event "
    body += "peak values are stricter measures. Event peak-hour timing is not "
    body += "scored. Denominators below count exposed "
    body += "event occurrences across scenarios, not independent climate events.</p>"
    if event_plot_sha is not None:
        body += "<figure><img src='analysis/event-figures/paired-weather-event-preservation.png' "
        body += "alt='Paired heatwave, cold-spell and wind episode preservation'>"
        body += "<figcaption>Matched meteorological weather cases, independent of "
        body += "building responses. Exact episode boundaries and peak values "
        body += "are stricter than any-overlap matching. "
        body += (
            "<a href='analysis/event-figures/paired-weather-event-preservation.svg'>"
        )
        body += "Vector figure</a></figcaption></figure>"
    exposed = events.loc[
        events.reference_exposed_events.gt(0) | events.new_candidate_event_count.gt(0)
    ]
    body += display(
        exposed[
            [
                "event_type",
                "scope",
                "sampling",
                "gap_hours",
                "method",
                "site_years",
                "weather_outages",
                "reference_exposed_events",
                "reference_hours_hidden",
                "lost_exposed_reference_events",
                "new_candidate_event_count",
                "exposed_events_any_overlap_fraction",
                "exposed_events_exact_bounds_fraction",
                "exposed_events_exact_bounds_and_peak_fraction",
            ]
        ]
    )
    body += "<p><a href='analysis/event-exposure-summary.csv'>All anomaly "
    body += "results, including zero-exposure rows</a> and "
    body += (
        "<a href='analysis/event-exposure-comparisons.csv'>each weather case</a>.</p>"
    )
    body += "<h2>Reference weather and interpretation limits</h2>" + display(sources)
    body += "<ul><li>Six geographical strata, three selected years and six "
    body += "synthetic EPSM Core profiles define the campaign design. Building "
    body += "inference uses only the diagnostic-qualified coverage shown above; "
    body += "exclusions can remove entire years or geographical strata. "
    body += "They do not establish national population uncertainty or certainty "
    body += "about future weather. Building responses share the same climates.</li>"
    body += "<li>Reference native gaps are bounded common background; only "
    body += "finite original observations are withheld and used as observed "
    body += "meteorological truth. Kiruna 2018 fails unchanged eligibility "
    body += "criteria and is excluded with its reason retained.</li>"
    body += "<li>Some regional strata use different stations across years. "
    body += "Weather and model geometry use each station's coordinates and "
    body += "elevation. Inter-year differences include station exposure changes.</li>"
    body += "<li>Solar references combine measured GHI or STRÅNG GHI with "
    body += "estimated components. Solar completion errors are proxy comparisons, "
    body += "not validation against independent measured DNI/DHI.</li>"
    body += "<li>The six buildings use ideal sensible loads and a fixed 18°C "
    body += "conditioned-floor boundary prescribed by Core. They are uncalibrated "
    body += "sensitivity models. Purchased energy, latent loads, precipitation, "
    body += "soil/snow uncertainty and adaptive controls are outside scope.</li>"
    body += "<li>Small annual errors can coexist with larger peak errors or "
    body += "changed peak hours. Untouched reference peaks are kept separate "
    body += "from exposed peaks; all tied maximum hours are compared exactly.</li></ul>"
    body += "<p><a href='report.html'>Original frozen-runner catalogue</a> · "
    body += (
        "<a href='summary.csv'>All raw comparisons, including excluded outcomes</a> · "
    )
    body += "<a href='analysis/manifest.json'>Analysis provenance</a> · "
    body += "<a href='coverage.json'>Coverage</a> · "
    body += "<a href='source-selection/plan.json'>Source selection</a></p>"
    page = "<!doctype html><html lang='en'><meta charset='utf-8'>"
    page += "<meta name='viewport' content='width=device-width,initial-scale=1'>"
    page += f"<title>{html.escape(title)}</title><style>"
    page += "body{font:16px/1.55 system-ui;color:#172b35;max-width:1500px;margin:40px auto;padding:0 24px}"
    page += (
        "p,li{max-width:1100px}.lead{font-size:21px}.table{overflow:auto;margin:20px 0}"
    )
    page += "table{border-collapse:collapse;font-size:12px;white-space:nowrap}td,th{padding:7px;border-bottom:1px solid #ccd6dc}"
    page += "th{background:#edf3f5;text-align:left}figure{margin:35px 0}img{width:100%;height:auto}"
    page += "a{color:#0069a6}h2{margin-top:40px}</style>" + body + "</html>"
    path = root / "synthesis.html"
    path.write_text(page, encoding="utf-8")
    receipt = {
        "status": "complete",
        "created_utc": datetime.now(UTC).isoformat(),
        "report_script_sha256": sha(Path(__file__).read_bytes()),
        "analysis_manifest_sha256": sha(manifest_bytes),
        "plot_manifest_sha256": sha(plot_bytes),
        "event_plot_manifest_sha256": event_plot_sha,
        "method_plot_manifest_sha256": method_plot_sha,
        "diagnostics_qualification_sha256": manifest["outputs"].get(
            "diagnostics-qualification.json"
        ),
        "coverage_sha256": sha(coverage_bytes),
        "matrix_sha256": sha(matrix_bytes),
        "audit_sha256": audit_hashes,
        "unique_annual_simulations": unique_runs,
        "outputs": {
            "synthesis.html": sha(path.read_bytes()),
            "analysis/hierarchy-digest.csv": sha(digest_path.read_bytes()),
        },
    }
    (root / "synthesis-receipt.json").write_text(
        json.dumps(receipt, indent=2, allow_nan=False) + "\n"
    )
    return receipt


def main():
    """Render the final verified campaign only."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign_root", type=Path)
    args = parser.parse_args()
    receipt = publish(args.campaign_root)
    print(json.dumps({k: receipt[k] for k in ("status", "unique_annual_simulations")}))


if __name__ == "__main__":
    main()
