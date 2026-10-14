"""Publication figures for Swedish building-stock availability and evidence support.

Run prepare_national_completeness.py first. This renderer reads aggregate data
only; no source records are modified. Vector PDF/SVG, 600-dpi PNG, captions,
methods, an atlas and a portable archive are written into --output.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import zipfile

os.environ.setdefault("MPLCONFIGDIR", "/private/tmp/epsm-national-completeness-mpl")
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.colors import Normalize
from matplotlib.patches import Patch
import geopandas as gpd
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
USES = ["multi_family", "single_family", "school", "office"]
NATIONAL_USES = ["multi_family", "single_family", "school", "business_proxy"]
LABELS = {"multi_family": "Multifamily", "single_family": "Single family", "school": "School",
          "office": "Office", "business_proxy": "Business proxy"}
COLORS = {"year": "#2369A0", "epc": "#D16A28", "good": "#398C83", "warning": "#E9AE50",
          "unresolved": "#BA7085", "scope": "#E2E5E8", "agree": "#367D78",
          "same_era": "#E7B550", "cross_era": "#B24D69", "single": "#D3D9DF"}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def number(value):
    return f"{int(value):,}"


def setup_style():
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 9, "axes.titlesize": 10,
        "axes.labelsize": 9, "xtick.labelsize": 8, "ytick.labelsize": 9,
        "pdf.fonttype": 42, "ps.fonttype": 42, "svg.fonttype": "none",
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.spines.left": False, "axes.spines.bottom": False,
        "savefig.facecolor": "white", "figure.facecolor": "white",
        "hatch.linewidth": 0.45, "axes.axisbelow": True,
    })


def read_data(data):
    paths = list(data.glob("*.csv"))
    tables = {p.stem: pd.read_csv(p, dtype={"municipality_code": str}) for p in paths}
    boundaries = gpd.read_file(data / "municipalities.gpkg", layer="municipalities")
    assert boundaries.crs.to_epsg() == 3006
    assert len(boundaries) == 290 and boundaries.municipality_code.is_unique
    assert boundaries.geometry.is_valid.all()
    for frame in tables.values():
        if "municipality_code" in frame:
            frame["municipality_code"] = frame.municipality_code.str.zfill(4)
            assert set(frame.municipality_code).issubset(set(boundaries.municipality_code))
    return tables, boundaries


def header(fig, title, subtitle):
    fig.text(0.055, 0.975, title, ha="left", va="top", fontsize=12, weight="bold")
    fig.text(0.055, 0.941, subtitle, ha="left", va="top", fontsize=8.5, color="#4E555C")


def footer(fig, text):
    fig.text(0.055, 0.015, text, ha="left", va="bottom", fontsize=7.3, color="#41484F", linespacing=1.45)


def draw_map(ax, boundaries, frame, value, title, vmin=0, vmax=100):
    assert frame.municipality_code.is_unique
    merged = boundaries.merge(frame[["municipality_code", value]], on="municipality_code",
                              how="left", validate="one_to_one")
    boundaries.plot(ax=ax, facecolor="#EDF0F2", edgecolor="#8F989F", linewidth=0.14)
    known = merged[merged[value].notna()]
    known.plot(ax=ax, column=value, cmap="cividis", vmin=vmin, vmax=vmax,
               edgecolor="#FFFFFF", linewidth=0.14)
    missing = merged[merged[value].isna()]
    if len(missing):
        missing.plot(ax=ax, facecolor="#EDF0F2", edgecolor="#737D85", hatch="////", linewidth=0.25)
    bounds = boundaries.total_bounds
    ax.set_xlim(bounds[0] - 12000, bounds[2] + 12000)
    ax.set_ylim(bounds[1] - 18000, bounds[3] + 18000)
    ax.set_aspect("equal")
    ax.set_axis_off()
    ax.set_title(title, loc="left", pad=8, fontsize=9.2)
    return merged


def map_colorbar(fig, position, label, vmin=0, vmax=100, ticks=None):
    ax = fig.add_axes(position)
    bar = fig.colorbar(plt.cm.ScalarMappable(norm=Normalize(vmin, vmax), cmap="cividis"),
                      cax=ax, orientation="horizontal", ticks=ticks)
    bar.ax.tick_params(labelsize=8, length=2)
    bar.set_label(label, fontsize=8.5, labelpad=3)
    bar.outline.set_visible(False)


def figure1(tables, boundaries):
    fig = plt.figure(figsize=(7.48, 8.9))
    header(fig, "National availability of building-stock sources",
           "Primary GIS use | Municipality percentages of all classified footprints")
    grid = fig.add_gridspec(2, 3, left=0.055, right=0.975, bottom=0.145, top=0.885,
                           hspace=0.17, wspace=0.16)
    national = tables["national_use_totals"].set_index("use_group")
    municipality = tables["national_municipality_use"]
    metrics = [("registry_year_fraction", "Registry year"), ("accepted_epc_linked_fraction", "EPC link")]
    for row, (metric, label) in enumerate(metrics):
        for col, use in enumerate(NATIONAL_USES[:3]):
            values = municipality[municipality.use_group.eq(use)].copy()
            values["value"] = values[metric] * 100
            ax = fig.add_subplot(grid[row, col])
            draw_map(ax, boundaries, values, "value", f"({chr(97 + row * 3 + col)}) {LABELS[use]}: {label}")
            ax.text(0.03, 0.015, f"National {national.loc[use, metric]*100:.1f}%\nN = {number(national.loc[use, 'footprint_buildings'])}",
                    transform=ax.transAxes, fontsize=8, ha="left", va="bottom",
                    bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.94, "pad": 2.5})
    map_colorbar(fig, [0.25, 0.088, 0.5, 0.015], "Share of footprints (%)", ticks=[0, 20, 40, 60, 80, 100])
    footer(fig, "Year = registry year present. EPC link = at least one accepted spatial certificate link.\n"
                "Values measure availability; source accuracy and certificate scope are not established. Source: Statistics Sweden (boundaries).")
    return fig


def percent_axes(ax):
    ax.set_xlim(0, 100)
    ax.set_xticks(np.arange(0, 101, 20))
    ax.set_xlabel("Share of buildings (%)")
    ax.grid(axis="x", color="#E2E6EA", linewidth=0.6)
    ax.tick_params(axis="both", length=0)


def figure2(tables, boundaries):
    fig = plt.figure(figsize=(7.48, 7.65))
    header(fig, "Completeness by building use and selected EPC cohort",
           "National footprint coverage and EPC-cohort completeness have separate denominators")
    ax = fig.add_axes([0.19, 0.595, 0.78, 0.22])
    national = tables["national_use_totals"].set_index("use_group").loc[NATIONAL_USES]
    y = np.arange(4)
    for metric, label, color, shift in [("registry_year_fraction", "Registry year present", COLORS["year"], -0.12),
                                         ("accepted_epc_linked_fraction", "Accepted EPC link", COLORS["epc"], 0.12)]:
        values = national[metric].to_numpy() * 100
        ax.scatter(values, y + shift, color=color, s=32, zorder=3, label=label)
        for x, yy in zip(values, y + shift):
            ax.text(x + 1.7, yy, f"{x:.1f}", va="center", ha="left", fontsize=8, color=color)
    ax.set_yticks(y, [f"{LABELS[u]}\nN = {number(national.loc[u, 'footprint_buildings'])}" for u in NATIONAL_USES], fontsize=8)
    ax.set_ylim(3.55, -0.55)
    percent_axes(ax)
    fig.text(0.19, 0.885, "(a) National classified footprint inventory", fontsize=10, va="top")
    fig.legend(*ax.get_legend_handles_labels(), loc="center", bbox_to_anchor=(0.58, 0.846),
               ncol=2, frameon=False, fontsize=8, handletextpad=0.4, columnspacing=1.5)

    ax = fig.add_axes([0.19, 0.23, 0.78, 0.235])
    selected = tables["epc_selected_use_totals"].set_index("use_group").loc[USES]
    metrics = ["construction_year_present", "height_m_present", "storeys_present", "atemp_m2_present",
               "heated_basement_floors_present", "positive_designed_flow", "completed_renovation_year_present",
               "year_height_storeys_atemp_present"]
    names = ["Year", "Height", "Storeys", "Atemp", "Heated\nbasement", "Positive\ndesign flow", "Renovation\nyear", "Four-input\navailability"]
    matrix = selected[[m + "_fraction" for m in metrics]].to_numpy() * 100
    ax.imshow(matrix, cmap="cividis", vmin=0, vmax=100, aspect="auto")
    for row in range(4):
        for col in range(8):
            value = matrix[row, col]
            label = ">99.9" if 99.95 <= value < 100 else f"{value:.1f}"
            ax.text(col, row, label, ha="center", va="center", fontsize=8,
                    color="white" if value < 58 else "#152131")
    ax.set_xticks(np.arange(8), names, fontsize=8)
    ax.set_yticks(np.arange(4), [f"{LABELS[u]}\nN = {number(selected.loc[u, 'epc_selected_buildings'])}" for u in USES], fontsize=8)
    ax.tick_params(length=0, pad=7)
    ax.set_xticks(np.arange(-0.5, 8, 1), minor=True)
    ax.set_yticks(np.arange(-0.5, 4, 1), minor=True)
    ax.grid(which="minor", color="white", linewidth=1.4)
    ax.tick_params(which="minor", length=0)
    ax.set_title("(b) Field availability in selected EPC buildings (%)", loc="left", pad=16)
    map_colorbar(fig, [0.31, 0.118, 0.44, 0.012], "Field availability (%)", ticks=[0, 25, 50, 75, 100])
    footer(fig, "Business proxy = broad GIS Verksamhet category; national office completeness is unresolved.\n"
                "EPC rows use reported programme and study selection. Atemp availability is imposed by selection.\n"
                "Four inputs = year, height, storeys and Atemp in the same building; presence does not establish validity.")
    return fig


def figure3(tables, boundaries):
    fig = plt.figure(figsize=(7.48, 7.4))
    header(fig, "Evidence agreement within the selected EPC cohort",
           "Internal consistency diagnostics | Accuracy and imputation confidence have not been calibrated")
    selected = tables["epc_selected_use_totals"].set_index("use_group").loc[USES]
    totals = selected.epc_selected_buildings.to_numpy()
    counts = [selected.count_year_same.to_numpy(),
              (selected.count_year_conflict-selected.count_year_cross_era_conflict).to_numpy(),
              selected.count_year_cross_era_conflict.to_numpy(),
              (selected.total-selected.count_year_both_sources).to_numpy()]
    assert (np.asarray(counts) >= 0).all()
    assert np.array_equal(sum(counts), totals)
    ax = fig.add_axes([0.18, 0.58, 0.785, 0.205])
    left = np.zeros(4)
    for values, color, label in zip(counts, [COLORS["agree"], COLORS["same_era"], COLORS["cross_era"], COLORS["single"]],
                                     ["Identical years", "Differ within era", "Cross-era difference", "Fewer than two sources"]):
        width = values / totals * 100
        ax.barh(np.arange(4), width, left=left, height=0.62, color=color, edgecolor="white", linewidth=0.7, label=label)
        for x, w, yy in zip(left, width, range(4)):
            if w >= 6:
                ax.text(x+w/2, yy, f"{w:.1f}", ha="center", va="center", fontsize=8,
                        color="white" if color in [COLORS["agree"], COLORS["cross_era"]] else "#172431")
        left += width
    ax.set_yticks(np.arange(4), [LABELS[u] for u in USES])
    ax.invert_yaxis()
    percent_axes(ax)
    fig.text(0.18, 0.883, "(a) Registry and EPC construction-year agreement", fontsize=10, va="top")
    fig.legend(*ax.get_legend_handles_labels(), loc="center", bbox_to_anchor=(0.57, 0.832),
               ncol=2, frameon=False, fontsize=7.8, handlelength=1.2, columnspacing=1.1)

    ax = fig.add_axes([0.18, 0.18, 0.785, 0.21])
    fraction = selected.membership_eligible.to_numpy() / totals * 100
    ax.barh(np.arange(4), fraction, height=0.58, color=COLORS["year"], label="Eligible")
    ax.barh(np.arange(4), 100-fraction, left=fraction, height=0.58, color="#DCE2E7", label="Excluded")
    for yy, value in enumerate(fraction):
        ax.text(value/2, yy, f"{value:.1f}%", color="white", ha="center", va="center", fontsize=8)
        ax.text(value+(100-value)/2, yy, f"{100-value:.1f}%", color="#273B4A", ha="center", va="center", fontsize=8)
    ax.set_yticks(np.arange(4), [f"{LABELS[u]}\nN = {number(totals[i])}" for i,u in enumerate(USES)], fontsize=8)
    ax.invert_yaxis()
    percent_axes(ax)
    fig.text(0.18, 0.469, "(b) Eligibility under the GIS-led pure-programme audit", fontsize=10, va="top")
    fig.legend(*ax.get_legend_handles_labels(), loc="center", bbox_to_anchor=(0.57, 0.426),
               ncol=2, frameon=False, fontsize=8)
    footer(fig, "All percentages use the complete selected cohort for the reported use. Era boundaries follow EPSM archetypes.\n"
                "Programme eligibility combines study rules; exclusions include mixed or conflicting use and other conditions.\n"
                "Agreement and eligibility describe evidence support. They are not probabilities of correct building attributes.")
    return fig


def supplement1(tables, boundaries):
    fig = plt.figure(figsize=(7.48, 6.45))
    header(fig, "Broad business-stock source availability",
           "Supplement S1 | Primary GIS Verksamhet category | 290 municipalities")
    grid = fig.add_gridspec(1, 2, left=0.085, right=0.93, bottom=0.23, top=0.87, wspace=0.2)
    values = tables["national_municipality_use"].query("use_group == 'business_proxy'")
    national = tables["national_use_totals"].set_index("use_group").loc["business_proxy"]
    for i, (metric,label) in enumerate([("registry_year_fraction", "Registry year present"),
                                       ("accepted_epc_linked_fraction", "Accepted EPC link")]):
        frame = values.copy()
        frame["value"] = frame[metric] * 100
        ax = fig.add_subplot(grid[0, i])
        draw_map(ax, boundaries, frame, "value", f"({chr(97+i)}) {label}")
        ax.text(0.02, 0.02, f"National {national[metric]*100:.1f}%\nN = {number(national.footprint_buildings)}", transform=ax.transAxes,
                fontsize=8, va="bottom", bbox={"facecolor":"white", "edgecolor":"none", "alpha":0.94, "pad":3})
    map_colorbar(fig, [0.25, 0.145, 0.5, 0.018], "Share of business-proxy footprints (%)", ticks=[0, 20, 40, 60, 80, 100])
    footer(fig, "Verksamhet includes business uses beyond offices; this is not a national office denominator.\n"
                "Office field availability is reported only for the selected EPC office cohort in Figure 2.\n"
                "EPC linkage is spatial acceptance, not verified measurement allocation. Source: Statistics Sweden (boundaries).")
    return fig


def supplement2(tables, boundaries):
    fig = plt.figure(figsize=(7.48, 8.8))
    header(fig, "LiDAR processing scope and internal extraction evidence",
           "Supplement S2 | Municipality maps include all uses in the completed publication scope")
    grid = fig.add_gridspec(1, 2, left=0.085, right=0.94, bottom=0.515, top=0.875, wspace=0.22)
    municipality = tables["height_municipality"].copy()
    municipality["yield"] = municipality.determined_fraction * 100
    municipality["warning"] = municipality.warning_fraction_of_determined * 100
    assert len(municipality) == 276
    for i, (metric, label, minimum, maximum) in enumerate([
        ("yield", "(a) Height extraction yield", 80, 100),
        ("warning", "(b) Extraction-warning share", 0, 100)]):
        ax = fig.add_subplot(grid[0, i])
        draw_map(ax, boundaries, municipality, metric, label, minimum, maximum)
    map_colorbar(fig, [0.15, 0.467, 0.28, 0.012], "Determined / included (%)", 80, 100, ticks=[80, 85, 90, 95, 100])
    map_colorbar(fig, [0.58, 0.467, 0.28, 0.012], "Warning / determined (%)", ticks=[0, 25, 50, 75, 100])
    fig.legend(handles=[Patch(facecolor="#EDF0F2", edgecolor="#737D85", hatch="////", label="Outside completed scope (14 municipalities)")],
               loc="center", bbox_to_anchor=(0.52, 0.419), fontsize=8, frameon=False)
    ax = fig.add_axes([0.19, 0.18, 0.78, 0.145])
    heights = tables["height_national_target_use"].set_index("use_group").loc[NATIONAL_USES]
    totals = heights.footprint_buildings.to_numpy()
    counts = [(heights.determined_buildings-heights.warning_buildings).to_numpy(), heights.warning_buildings.to_numpy(),
              heights.undetermined_buildings.to_numpy(), heights.outside_completed_scope_buildings.to_numpy()]
    assert np.array_equal(sum(counts), totals)
    left = np.zeros(4)
    for values,color,label in zip(counts, [COLORS["good"], COLORS["warning"], COLORS["unresolved"], COLORS["scope"]],
                                     ["Determined: no status warning", "Determined: status warning", "Undetermined", "Outside analysis scope"]):
        width = values / totals * 100
        ax.barh(np.arange(4), width, left=left, color=color, height=0.62, edgecolor="white", linewidth=0.6, label=label)
        for x,w,yy in zip(left,width,range(4)):
            if w >= 5:
                ax.text(x+w/2,yy,f"{w:.1f}",ha="center",va="center",fontsize=8,
                        color="white" if color in [COLORS["good"],COLORS["unresolved"]] else "#1A2934")
        left += width
    ax.set_yticks(np.arange(4), [LABELS[u] for u in NATIONAL_USES], fontsize=8)
    ax.invert_yaxis()
    percent_axes(ax)
    fig.text(0.19, 0.392, "(c) Target uses: full national footprint inventory", fontsize=10, va="top")
    fig.legend(*ax.get_legend_handles_labels(), loc="center", bbox_to_anchor=(0.57, 0.353),
               ncol=2, frameon=False, fontsize=7.5, columnspacing=1.4, handlelength=1.25)
    footer(fig, "Completed scope: 276/290 municipalities, 8,702,770 included buildings; 94.43% have determined height.\n"
                "Warning means excluded negative height points or an unusually high representative height. Other flags can coexist.\n"
                "No warning does not establish accuracy. Yield colour scale starts at 80%. Source: Statistics Sweden (boundaries).")
    return fig


CAPTIONS = """Suggested figure captions (edit for the target journal)

Figure 1. Municipality-level availability of registry construction year (a-c) and accepted EPC linkage (d-f) for multifamily, single-family and school buildings in Sweden. Denominators are all canonical physical footprint building IDs classified by primary GIS purpose in the EPC linkage release 2.0.0, covering 290 municipalities. Multifamily includes Bostad;Flerfamiljshus; single family includes detached, chained and terraced small houses; school includes Samhällsfunktion;Skola. Ambiguous small houses with several dwellings and unspecified residential uses are excluded from single family. Registry-year availability counts a parsed construction-year value; future-year values are retained in this presence measure and separately tabulated in the supplied data. An EPC link means at least one accepted spatial certificate relationship, without imposing certificate freshness, unique footprint scope or semantic eligibility. National percentages are pooled counts across buildings, not unweighted municipality averages. Boundary source: Statistics Sweden, generalized municipality boundaries, 2026-02-25 archive, CC0. Map CRS: SWEREF 99 TM (EPSG:3006). Extraction date: 2026-10-05. Percentages describe the records in these releases; uncertainty from source quality and selection is not quantified.

Figure 2. National source availability and conditional field completeness by use. (a) Registry-year presence and accepted EPC-link coverage in the complete national primary-GIS footprint inventory. Verksamhet is a broad business proxy; a reliable national office footprint denominator is not established by this classification. (b) Field availability among selected EPC buildings classified by reported EPC programme, using stock-national-2026-10-02-v2 (measurement years 2016-2024). Selection requires the study's newer-form, usable measurement and single-footprint conditions; therefore availability of Atemp is imposed by cohort selection and cannot be generalized to all national buildings. Height is a usable LiDAR-derived value. Storeys and heated-basement storeys are EPC fields; zero basement storeys count as reported. Positive design flow requires a reported value greater than zero. Renovation year is the reported completed-renovation year; missing does not mean no renovation. Four-input availability directly counts buildings with construction year, height, above-ground storeys and Atemp all present. Presence does not establish correctness, representativeness or successful simulation assembly. Values are percentages within the explicitly stated denominator; source and selection uncertainty are unquantified.

Figure 3. Evidence-consistency diagnostics in the selected EPC cohort (measurement years 2016-2024). (a) A mutually exclusive partition into identical registry and EPC construction years, differing years within the same EPSM archetype era, differing years crossing an archetype-era boundary, and fewer than two reported source years. The six era boundaries are 1960, 1975, 1985, 1995 and 2005. All shares use the full selected-use cohort as denominator, including buildings lacking a second year. A disagreement is not proof that either source is wrong. (b) Eligibility under the existing GIS-led pure-programme membership audit, archetype-membership-gis-led-2026-10-04-v2. This is a study-specific composite gate, including programme conflicts, mixed use and other assembly conditions; it is not a classification accuracy test. These diagnostics describe evidence agreement and support; they are not calibrated confidence probabilities. No representative independent attribute or linkage accuracy validation, or imputation-error study, is supplied by this audit.

Supplementary Figure S1. Municipality-level source availability in the broad primary-GIS Verksamhet business category. Denominator: 70,137 physical footprint building IDs nationally. Both maps use the same 0-100% scale and the national-building definitions in Figure 1. This category contains business uses beyond offices; its coverage does not estimate national office completeness. EPC-selected office field availability is reported in Figure 2. Boundaries: Statistics Sweden, CC0; EPSG:3006.

Supplementary Figure S2. LiDAR publication scope and internal extraction diagnostics. (a) Municipality-level determined-height yield among included buildings in the 276 fully completed municipalities. The colour scale is 80-100%; hatched polygons identify the 14 municipalities outside the completed publication scope. The publication includes 8,702,770 buildings with unambiguous broad purpose; 8,218,191 have determined representative heights (94.43%). (b) Extraction-warning share among determined heights, using a 0-100% scale. Pipeline warnings identify negative height-point exclusion and/or representative height above the configured unusual-height threshold; other diagnostic flags may occur independently. (c) Exact detailed-GIS target-use counts partitioned into determined height without warning, determined height with warning, undetermined height inside completed scope, and outside completed scope. Panel (c) denominators are the full national footprint inventory for each target use, unlike the conditional map denominators. The residual outside analysis scope includes incomplete municipalities and any differences between national target inventories and publication inclusion. Seven missing result IDs exist inside the completed municipality scope across all uses; they are absent from the publication aggregates and therefore included in this residual if they belong to a displayed target use. The residual is not a claim that heights were never extracted there. No-warning status is not external validation. This release has no representative independent reference-height error estimate. Boundary source: Statistics Sweden, CC0; EPSG:3006.
"""

METHODS = """Reproducible national building-stock completeness figures

Audit date: 2026-10-05. The figures separate field availability, internal evidence agreement, processing scope and diagnostic warnings. They do not assign a confidence score or claim a predicted attribute is correct. No imputation model was trained in this work. Quantitative predictive confidence requires independent validation with geographically grouped holdouts, field-specific error metrics and uncertainty calibration.

Unit and population
National inventory: distinct canonical building IDs in the EPC linkage release 2.0.0 building_results partitions, with counts reconciled exactly by municipality and primary GIS purpose against building_age's canonical_footprints inventory. Registry-year presence is obtained by an SQL left join on building_id. Complete cross-release building-ID set equality is not independently tested by this figure pipeline. All 290 municipalities are retained; missing use denominators are NA, not zero coverage. Fractions are numerator/denominator within municipality or pooled nationally. Sources use primary GIS purpose, not EPC-reported use.
Selected cohort: one record per building_id in stock-national-2026-10-02-v2/features.parquet, joined one-to-one with the existing GIS-led membership audit on building_id and formular_id. Cohort rows use reported EPC programme. Selected measurement years span 2016-2024. The four displayed groups contain 222,787 buildings; other uses in the complete selected stock are not displayed. Field presence uses not-null values; positive design flow additionally requires >0. Renovation fields represent reported information, not proof of current fabric condition. Year partitions and joint input availability are counted directly for the same building records.
Heights: publication summary tables of footprint_height_extraction, based on harmonized release 1.0.0 and the publication's fully completed municipality scope. Municipality maps include all publication uses; the detailed-purpose table supports exact MF/SF/school/business-proxy pooled counts. Broad municipal housing summaries cannot isolate MF versus SF and are not presented as such. Outside-analysis-scope target counts are full national footprint counts minus publication-included counts, including publication exclusions and any of the seven missing in-scope result IDs belonging to target uses.

Office scope
The broad primary-GIS Verksamhet category cannot isolate a complete national office inventory. Office EPC-cohort field completeness can be estimated conditionally. National office completeness, representativeness and geographic coverage remain unresolved until an independently defined office stock denominator is assembled.

Uncertainty and interpretation
Release percentages are descriptive census-like counts and have no binomial confidence intervals. Uncertainty due to source accuracy, selection, EPC measurement allocation and unprocessed scope is not quantified. Year agreement and programme eligibility are internal diagnostics; accepted spatial linkage does not establish a valid individual-building energy denominator. Height extraction warnings describe pipeline support rather than error probabilities. Absence of warning cannot be read as high confidence. A future imputation study should distinguish observed, derived, imputed and archetype-assumed inputs, retain field-specific provenance, and validate on independent properties and geographic regions.

Reproduction
Use Python with numpy, pandas, geopandas, pyogrio, shapely and matplotlib to rerender the included aggregate data:
python plot_national_completeness.py --data data --output figures
To rederive the aggregates from original local releases, run prepare_national_completeness.py with source arguments documented by --help. Source paths and SHA-256 hashes are stored in data/manifest.json. The plotting package has no need to access original individual-building records. Boundaries in data/municipalities.gpkg contain 290 valid polygons in EPSG:3006. Full provenance, boundary licence and output hashes are included.

Output conventions
Individual PDFs and SVGs are vector figures. PNG exports are 600 dpi. PDF fonts are embedded as TrueType; SVG text is editable and uses DejaVu Sans. Colour scales use cividis; categorical diagnostics have explicit labels and legends. Figure dimensions are 190 mm wide, suitable for a two-column paper subject to journal-specific adjustments. A combined atlas repeats the five figure pages for review.
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / "output/national-completeness-paper-2026-10-05/data")
    parser.add_argument("--output", type=Path, default=ROOT / "output/pdf/national-completeness-paper-2026-10-05")
    parser.add_argument("--dpi", type=int, default=600)
    args = parser.parse_args()
    setup_style()
    args.output.mkdir(parents=True, exist_ok=True)
    tables, boundaries = read_data(args.data)
    makers = [("figure_1_national_source_maps", figure1), ("figure_2_completeness", figure2),
              ("figure_3_evidence_agreement", figure3), ("supplement_s1_business_proxy_maps", supplement1),
              ("supplement_s2_height_scope", supplement2)]
    files = []
    atlas = args.output / "national_completeness_atlas.pdf"
    metadata = {"Title": "Swedish building-stock completeness and evidence support", "Author": "EPSM national-stock workflow",
                "Subject": "Descriptive availability and internal quality diagnostics; no calibrated confidence estimate",
                "Keywords": "Sweden, UBEM, building stock, EPC, LiDAR, data completeness"}
    with PdfPages(atlas, metadata=metadata) as pages:
        for name, maker in makers:
            fig = maker(tables, boundaries)
            for extension in ["pdf", "svg", "png"]:
                path = args.output / f"{name}.{extension}"
                kwargs = {"dpi": args.dpi} if extension == "png" else {}
                if extension == "pdf":
                    kwargs["metadata"] = metadata
                fig.savefig(path, **kwargs)
                files.append(path)
            pages.savefig(fig)
            plt.close(fig)
            print(f"Saved {name}", flush=True)
    files.append(atlas)
    captions = args.output / "captions.txt"
    captions.write_text(CAPTIONS, encoding="utf-8")
    methods = args.output / "methods_and_reproduction.txt"
    methods.write_text(METHODS, encoding="utf-8")
    files.extend([captions, methods])
    record = {
        "created_at": datetime.now(timezone.utc).isoformat(), "audit_date": "2026-10-05",
        "renderer": str(Path(__file__).resolve()), "renderer_sha256": sha256(__file__),
        "aggregate_manifest_sha256": sha256(args.data / "manifest.json"),
        "geography_manifest_sha256": sha256(args.data / "geography_manifest.json"),
        "runtime": {"python": platform.python_version(), "matplotlib": matplotlib.__version__,
                    "numpy": np.__version__, "pandas": pd.__version__, "geopandas": gpd.__version__},
        "export": {"width_mm": 190, "png_dpi": args.dpi, "pdf_fonttype": 42, "svg_text_editable": True,
                   "map_crs": "EPSG:3006", "boundary_attribution": "Source: Statistics Sweden", "boundary_licence": "CC0"},
        "interpretation": "Descriptive availability and internal evidence support, not calibrated attribute confidence or imputation accuracy.",
        "files": [{"filename": p.name, "size_bytes": p.stat().st_size, "sha256": sha256(p)} for p in files],
    }
    manifest = args.output / "figure_manifest.json"
    manifest.write_text(json.dumps(record, indent=2) + "\n")
    archive = args.output / "national_completeness_paper_package.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for path in [*files, manifest]:
            package.write(path, f"figures/{path.name}")
        for path in sorted(args.data.iterdir()):
            if path.is_file():
                package.write(path, f"data/{path.name}")
        for path in [Path(__file__), Path(__file__).with_name("prepare_national_completeness.py")]:
            package.write(path, path.name)
    print(json.dumps({"atlas": str(atlas), "archive": str(archive), "figures": len(makers)}, indent=2), flush=True)


if __name__ == "__main__":
    main()
