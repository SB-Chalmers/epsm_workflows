"""Describe calibrated misses using saved out-of-fold predictions and EPC inputs.

This report cannot change Core settings, assignment, folds or calibration. Energy
class and observed heat are outcome descriptors, not independent causal evidence.
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.spatial.distance import cdist
from scipy.stats import spearmanr

from analyze_archetype_residuals import SEED, dump, read, sha

COLORS = {
    "inside 20%": "#177b70",
    "overpredicted ≥20%": "#bf513c",
    "underpredicted ≥20%": "#456aab",
    "simulation failed": "#292b35",
}


def extra_checks(df, root):
    rng = np.random.default_rng(SEED + 1)
    good = df[df.accepted]
    fields = [
        "height_m",
        "atemp_m2",
        "floor_height_m",
        "gross_to_atemp_ratio",
        "wall_area_per_atemp",
        "footprint_aspect_ratio",
        "footprint_compactness",
        "construction_year",
    ]
    comparisons = []
    for name, mask in [
        ("all", df.accepted),
        ("2018 only", df.accepted & (df.measurement_year == 2018)),
        ("excluding Biskopsgården", df.accepted & (df.area_name != "BISKOPSGÅRDEN")),
    ]:
        properties = df[mask].groupby("property_key").median(numeric_only=True)
        for field in fields:
            values = properties[[field, "residual"]].dropna()
            rho, p = spearmanr(values[field], values.residual)
            comparisons.append(
                {
                    "subset": name,
                    "field": field,
                    "properties": len(values),
                    "rho": rho,
                    "p_unadjusted_exploratory": p,
                }
            )
    # Test spatial association after restricting permutations to the same year.
    properties = good.groupby("property_key").agg(
        residual=("residual", "median"),
        easting=("easting", "mean"),
        northing=("northing", "mean"),
        year=("measurement_year", "median"),
    )
    distances = cdist(
        properties[["easting", "northing"]], properties[["easting", "northing"]]
    )
    np.fill_diagonal(distances, np.inf)
    weights = np.zeros_like(distances)
    np.put_along_axis(weights, distances.argsort(axis=1)[:, :4], 1, axis=1)
    weights = np.maximum(weights, weights.T)
    weights /= weights.sum(axis=1, keepdims=True)
    z = properties.residual.to_numpy(copy=True)
    z -= z.mean()
    actual = z @ weights @ z / (z @ z)
    year_groups = [
        np.flatnonzero(properties.year.to_numpy() == year)
        for year in properties.year.unique()
    ]
    simulations = []
    for _ in range(9999):
        shuffled = z.copy()
        for ids in year_groups:
            shuffled[ids] = rng.permutation(z[ids])
        simulations.append(shuffled @ weights @ shuffled / (shuffled @ shuffled))
    spatial = {
        "graph": "symmetric row-standardised knn4",
        "properties": len(properties),
        "moran_i": actual,
        "conditioning": "shuffle within property median measurement year",
        "permutations": 9999,
        "p": (1 + (np.array(simulations) >= actual).sum()) / 10000,
    }
    property_table = df.groupby("property_key").agg(
        buildings=("building_id", "size"),
        inside20=("inside20", "sum"),
        unique_certificates=("formular_id", "nunique"),
        unique_heating_intensities=("observed", "nunique"),
        unique_heating_kwh=("heat_kwh", "nunique"),
        unique_atemp=("atemp_m2", "nunique"),
        observed_median=("observed", "median"),
        predicted_median=("predicted", "median"),
        measurement_year=("measurement_year", "median"),
    )
    result = {
        "status_counts": df.status20.value_counts().to_dict(),
        "miss_properties": df[~df.inside20].property_key.nunique(),
        "property_balanced_coverage20": df.groupby("property_key")
        .inside20.mean()
        .mean(),
        "medians_by_status": df.groupby("status20")[
            fields + ["observed", "predicted", "storeys"]
        ]
        .median()
        .to_dict("index"),
        "subset_correlations": comparisons,
        "year_conditional_spatial_test": spatial,
        "property_repeated_values": property_table.reset_index().to_dict("records"),
        "verified_party_walls": int(df.verified_party_walls.sum()),
        "context_comparisons_matching_current_predictions": int(
            df.context_comparison_matches_current_baseline.sum()
        ),
        "descriptive_only": True,
        "new_physical_simulations": False,
    }
    dump(root / "supplement.json", result)
    return result


def plots(df, root):
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.alpha": 0.16,
        }
    )
    fig, axes = plt.subplots(1, 3, figsize=(17, 5.5), constrained_layout=True)
    for status, group in df[df.accepted].groupby("status20"):
        axes[0].scatter(
            group.observed,
            group.predicted,
            color=COLORS[status],
            s=28,
            alpha=0.8,
            label=f"{status}: {len(group)}",
        )
    bounds = np.array([40, 145])
    axes[0].fill_between(bounds, 0.8 * bounds, 1.2 * bounds, color="#177b70", alpha=0.1)
    axes[0].plot(bounds, bounds, color="#555", linewidth=0.8)
    axes[0].set(
        xlabel="Reported space heat (kWh/m²/year)",
        ylabel="Held-out prediction (kWh/m²/year)",
        title="24 overpredicted · 4 underpredicted\n2 numerical rejections omitted from scatter",
    )
    axes[0].legend(fontsize=8, loc="upper left")
    # Every failed run remains a plotted attempt in the geographic map.
    for status, group in df.groupby("status20"):
        axes[1].scatter(
            group.easting / 1000,
            group.northing / 1000,
            color=COLORS[status],
            s=30,
            marker="x" if status == "simulation failed" else "o",
            alpha=0.8,
        )
    for area in ("BISKOPSGÅRDEN", "BACKA", "KÄRRA", "JÄRNBROTT", "KOBBEGÅRDEN"):
        group = df[df.area_name == area]
        axes[1].annotate(
            area.title(),
            (group.easting.mean() / 1000, group.northing.mean() / 1000),
            xytext=(4, 5),
            textcoords="offset points",
            fontsize=8,
        )
    axes[1].set(
        xlabel="SWEREF 99 TM easting (km)",
        ylabel="Northing (km)",
        title="Misses cluster by estate",
    )
    axes[1].set_aspect("equal")
    settings = [
        ("Biskopsgården", df.area_name == "BISKOPSGÅRDEN"),
        ("Backa", df.area_name == "BACKA"),
        ("4–5 storeys", df.storeys.between(4, 5)),
        ("6+ storeys", df.storeys >= 6),
        ("Pitched roof", df.roof_form == "pitched_ridge"),
        ("Flat roof", df.roof_form == "flat"),
        ("Heated basement", df.heated_basement == "yes"),
        ("No reported heated basement", df.heated_basement == "no"),
    ]
    for i, (name, mask) in enumerate(settings):
        group = df[mask]
        start = 0
        for status in COLORS:
            percentage = (group.status20 == status).mean() * 100
            axes[2].barh(i, percentage, left=start, color=COLORS[status])
            start += percentage
        axes[2].text(
            102, i, f"{int(group.inside20.sum())}/{len(group)}", va="center", fontsize=9
        )
    axes[2].set(
        yticks=np.arange(len(settings)),
        yticklabels=[x[0] for x in settings],
        xlim=(0, 120),
        xlabel="Share of attempted buildings (%)",
        title="Inside / over / under / failed",
    )
    axes[2].invert_yaxis()
    fig.suptitle(
        "Data 1.5 calibrated procedure · 85 buildings · 54 properties", fontsize=15
    )
    fig.savefig(root / "miss-overview.png", dpi=170)
    fig.savefig(root / "miss-overview.svg")
    plt.close(fig)
    fig, axes = plt.subplots(2, 3, figsize=(14, 8), constrained_layout=True)
    for ax, (field, title) in zip(
        axes.flat,
        [
            ("atemp_m2", "EPC Atemp (m²)"),
            ("height_m", "Model eave/flat-roof height (m)"),
            ("gross_to_atemp_ratio", "Gross conditioned area / Atemp"),
            ("wall_area_per_atemp", "Exposed wall area / Atemp"),
            ("footprint_aspect_ratio", "Footprint length / width"),
            ("construction_year", "Construction year"),
        ],
        strict=True,
    ):
        for status, group in df[df.accepted].groupby("status20"):
            ax.scatter(group[field], group.residual, c=COLORS[status], s=25, alpha=0.8)
        ax.axhline(0, c="#555", linewidth=0.8)
        if field == "atemp_m2":
            ax.set_xscale("log")
        ax.set(xlabel=title, ylabel="Prediction − reported heat (kWh/m²/year)")
    fig.suptitle(
        "Geometry patterns are confounded with estate, year and reported heat",
        fontsize=15,
    )
    fig.savefig(root / "miss-geometry.png", dpi=170)
    fig.savefig(root / "miss-geometry.svg")
    plt.close(fig)


def value(x):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "—"
    return f"{x:.2f}" if isinstance(x, float) else html.escape(str(x))


def report(df, summary, supplement, root):
    misses = df[~df.inside20].sort_values(
        "signed_percent", ascending=False, na_position="last"
    )
    fields = [
        "building_id",
        "property_key",
        "address",
        "status20",
        "observed",
        "predicted",
        "signed_percent",
        "construction_year",
        "measurement_year",
        "height_m",
        "storeys",
        "atemp_m2",
        "gross_to_atemp_ratio",
        "wall_area_per_atemp",
        "footprint_aspect_ratio",
        "heated_basement",
        "certificate_class",
        "building_position",
        "completed_window_replacement",
        "overlap_neighbours_excluded",
        "assignment",
    ]
    dump(root / "30-misses.json", misses[fields].to_dict("records"))
    groups = [
        r
        for r in summary["groups"]
        if r["field"]
        in [
            "area_name",
            "roof_form",
            "heated_basement",
            "storey_band",
            "certificate_class",
            "measurement_year",
            "area_ratio_band",
            "completed_window_replacement",
        ]
    ]
    group_rows = "".join(
        f"<tr><td>{value(r['field'])}</td><td>{value(r['value'])}</td><td>{r['inside20']}/{r['n']}</td><td>{r['properties']}</td><td>{value(r['bias'])}</td></tr>"
        for r in groups
    )
    columns = [
        ("property_key", "Property"),
        ("address", "Address"),
        ("status20", "Result"),
        ("observed", "Reported heat"),
        ("predicted", "Prediction"),
        ("signed_percent", "Error %"),
        ("construction_year", "Built"),
        ("measurement_year", "Measured"),
        ("height_m", "Height m"),
        ("storeys", "Storeys"),
        ("atemp_m2", "Atemp m²"),
        ("gross_to_atemp_ratio", "Gross/Atemp"),
        ("certificate_class", "EPC"),
        ("heated_basement", "Heated basement"),
        ("building_position", "Position"),
    ]
    table_rows = "".join(
        "<tr>" + "".join(f"<td>{value(row[key])}</td>" for key, _ in columns) + "</tr>"
        for row in misses.to_dict("records")
    )
    repeated = [
        r
        for r in supplement["property_repeated_values"]
        if r["inside20"] < r["buildings"]
        and r["buildings"] > 1
        and r["unique_heating_kwh"] == 1
    ]
    duplicate_rows = "".join(
        f"<tr><td>{value(r['property_key'])}</td><td>{r['buildings']}</td><td>{r['unique_certificates']}</td><td>{value(r['observed_median'])}</td></tr>"
        for r in repeated
    )
    content = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Data 1.5: the 30 misses</title><style>body{{font-family:system-ui,sans-serif;margin:32px auto;padding:0 24px;max-width:1450px;color:#22323a;line-height:1.55}}h1,h2{{line-height:1.2}}img{{width:100%;height:auto}}table{{border-collapse:collapse;width:100%;font-size:13px}}td,th{{text-align:left;padding:8px;border-bottom:1px solid #dde4e3;white-space:nowrap}}th{{background:#eef3f2;position:sticky;top:0}}.scroll{{overflow:auto}}li{{margin:12px 0}}.note{{padding:16px;background:#eef3f2}}</style>
<h1>What do the 30 misses have in common?</h1><p>Frozen out-of-fold predictions for 85 buildings / 54 properties. The 30 misses occupy 17 properties: <b>24 overpredictions, 4 underpredictions, 2 rejected simulations</b>. Fifty-five buildings are inside 20%.</p>
<img src="miss-overview.png" alt="Prediction-versus-observation scatter, geographic errors and subgroup coverage">
<h2>Strongest patterns</h2><ol>
<li><b>Långströmsgatan dominates.</b> Twelve buildings on Biskopsgården 7:1, 7:2 and 7:3 are all overpredicted. They are four-storey, pitched-roof, class-D buildings built in 1967–68 and measured in 2024. Reported space heat is 62.3–63.0 kWh/m²/year; predictions are 90.0–100.1. These are three properties, not twelve independent property observations.</li>
<li><b>Backa contains nine overpredictions plus one underprediction.</b> Five two-storey buildings on Backa 99:3/99:4 report 79.4 and predict 99.9–104.9. Sägengatan 3/58 report 69.0 and predict 96.9–97.9; both report dated window replacement. Klassikergatan 15/19 report 60.9/66.3 and predict 82.1/84.1; both also report dated window replacement. All four Sägengatan/Klassikergatan buildings were assigned the parent fallback because they were outside descriptor support. The separate Backa 69:11 tower is underpredicted.</li>
<li><b>Class and measurement year do not identify the mechanism.</b> All fourteen class-D buildings miss; twelve are the same 2024 Långströmsgatan group. Every form-3.0 and 2024 record is in that group. Class derives from energy performance, and percentage error itself divides by reported energy. These are useful audit flags, not independent predictors of a physical defect.</li>
<li><b>Geometry signals weaken after controlling for the sample.</b> Overpredicted buildings have median Atemp 1897 m² versus 3362 among matches; exposed wall/Atemp is .701 versus .615. Four–five-storey coverage is 4/17 versus 25/30 for six-plus. In property-level comparisons restricted to 2018, size, height and wall exposure associations weaken. Floor height, footprint elongation and construction year do not provide a strong general explanation.</li>
<li><b>Gross/Atemp and basement conditioning need selected-building review.</b> Both overpredicted Kärra 40:1 buildings have gross/Atemp 1.52–1.54 and reported heated basements. This can make assumed heated extent relevant. Across the cohort, however, basement coverage is 19/29 versus 36/56 without reported heated basement; there is no universal basement failure pattern.</li>
<li><b>Shared-wall evidence is unresolved.</b> No verified party walls or contact candidates were applied. Eighteen buildings have overlapping neighbour outlines excluded from shading; thirteen are overpredicted. Roof-edge overlap may indicate adjacency or outline problems, but does not prove a heated party wall. Detached EPC labels alone are insufficient evidence of full exterior exposure.</li>
<li><b>The four underpredictions require a different explanation.</b> Hammarkulletorget 13, Solarvsplan 37, Landalabergen 25 and Folke Filbyters Stig 7 have reported heat 92.5–123.4 and predictions 70.1–87.7. Three are six–eleven-storey buildings. A universal reduction in heat loss would worsen these cases.</li>
</ol>
<p>Spatial clustering also remains detectable when permutations are restricted to the same measurement year (property-level Moran I = 0.365, exploratory p = 0.0129). This supports checking estate-specific inputs, without identifying which input is responsible.</p>
<h2>Recommended evidence checks</h2><p>First audit metering/allocation, Atemp and normal-use/weather-correction boundaries for Långströmsgatan, Backa 99:3/99:4 and Kärra 40:1. Then obtain dated component renovations and actual ventilation for Långströmsgatan and Sägengatan. Check heated basement extent in Kärra and verified heated contacts wherever footprints overlap. Review the four underpredictions separately. Do not change the frozen catalogue or widen parameter bounds from these inspected residuals.</p>
<h2>Repeated EPC quantities deserve a boundary audit</h2><p>The following miss properties contain distinct certificate IDs but identical reported annual heating, Atemp and heating intensity across their selected buildings. This is consistent with several possible reporting arrangements; it does not establish actual meter independence. Recheck the certificate attachments and allocation basis before interpreting every repeated value as an independent building measurement.</p><table><tr><th>Property</th><th>Buildings</th><th>Certificates</th><th>Reported kWh/m²/year</th></tr>{duplicate_rows}</table>
<img src="miss-geometry.png" alt="Residual comparisons by area, height, area ratio, wall exposure, footprint shape and construction year">
<h2>Subgroup comparison</h2><p>Coverage retains both simulation failures. Property counts may overlap between strata.</p><table><tr><th>Descriptor</th><th>Value</th><th>Inside 20% / attempted</th><th>Properties</th><th>Mean signed error kWh/m²/year</th></tr>{group_rows}</table>
<h2>All 30 buildings</h2><p>Reported/predicted heat is delivered space heat in kWh/m²/year, excluding DHW. Height is the model eave/flat-roof height. Blank prediction denotes rejected simulation.</p><div class="scroll"><table><tr>{"".join("<th>" + label + "</th>" for _, label in columns)}</tr>{table_rows}</table></div>
<p class="note">Retrospective, descriptive diagnosis. No Core parameters or campaign inputs were fitted or changed. Building size, location, class, year and estate characteristics are confounded. All use multifamily SE.02, exhaust ventilation and district heating; there is no use/system contrast in this sample. Source hashes and property-level tests are retained in manifest.json, analysis.json and supplement.json. Correlations were explored with multiple-testing adjustment; they do not establish cause.</p></html>"""
    (root / "report.html").write_text(content)
    return {
        "misses": len(misses),
        "miss_properties": misses.property_key.nunique(),
        "repeated_value_miss_properties": len(repeated),
        "conditional_spatial_test": supplement["year_conditional_spatial_test"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis", type=Path, required=True)
    args = parser.parse_args()
    root = args.analysis
    df = pd.DataFrame(read(root / "buildings.json"))
    supplement = extra_checks(df, root)
    plots(df, root)
    result = report(df, read(root / "analysis.json"), supplement, root)
    dump(
        root / "diagnosis-verification.json",
        {
            **result,
            "script_sha256": sha(__file__),
            "source_prediction_sha256": read(root / "manifest.json")["source_hashes"][
                str(Path(read(root / "manifest.json")["campaign"]) / "predictions.json")
            ],
            "all_attempts_accounted": len(df) == 85 and int(df.inside20.sum()) == 55,
            "calibration_changed": False,
        },
    )
    print(json.dumps(result, default=float, indent=2))


if __name__ == "__main__":
    main()
