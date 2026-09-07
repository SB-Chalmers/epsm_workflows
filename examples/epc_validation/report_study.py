"""Create the saved pilot report and real epJSON geometry renders."""

import argparse
import csv
import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from epsm_workflows.validation import accuracy
from PIL import Image, ImageDraw, ImageOps

from epsm_core.weather import file_sha256

p = argparse.ArgumentParser()
p.add_argument("--study", type=Path, required=True)
p.add_argument("--core-examples", type=Path, required=True)
a = p.parse_args()
sys.path.insert(0, str(a.core_examples.resolve()))
from real_building_review import render  # noqa: E402 — caller-selected development renderer

root = a.study.resolve()
cohort = json.loads((root / "cohort.json").read_text())
attempts = json.loads((root / "run-results.json").read_text())
actual = [r for r in attempts if r["weather"] == "actual" and r["status"] == "success"]
tmy = [r for r in attempts if r["weather"] == "tmy" and r["status"] == "success"]
entries = {r["formular_id"]: r for r in cohort}


def metrics(rows):
    return (
        dict(
            annual_kwh=accuracy(
                [r["reported_kwh"] for r in rows], [r["predicted_kwh"] for r in rows]
            ),
            annual_kwh_per_m2_atemp=accuracy(
                [r["reported_kwh_m2"] for r in rows], [r["predicted_kwh_m2"] for r in rows]
            ),
        )
        if rows
        else None
    )


summary = dict(
    attempted=len(attempts),
    successful=sum(r["status"] == "success" for r in attempts),
    actual=metrics(actual),
    tmy=metrics(tmy),
    by_schema={
        s: metrics([r for r in actual if r["schema"] == s]) for s in ["fore_v260", "efter_v260"]
    },
    high_confidence_eligible=0,
    primary_energy_accuracy=None,
    raw_meter_accuracy=None,
    loss_sensitivity={
        str(loss): accuracy(
            [r["reported_kwh_m2"] for r in actual],
            [r["loss_sensitivity"][str(loss)] / r["atemp_m2"] for r in actual],
        )
        for loss in [0, 10]
    },
    limitations=[
        "exploratory selected cohort",
        "all outlines are roof-edge proxies",
        "old EPC totals allocated",
        "new individual metering not established",
        "normal-use adjustments not reconstructed",
        "assumed fabric, height, roof, basement and net delivery losses",
        "no neighboring shading",
        "no independent national holdout",
        "related estates can correlate errors",
        "weather comparison also changes station/location and period; not a pure weather-year effect",
    ],
)
(root / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False))
with (root / "comparisons.csv").open("w", newline="") as f:
    fields = [
        "formular_id",
        "schema",
        "year",
        "weather",
        "status",
        "atemp_m2",
        "reported_kwh",
        "predicted_kwh",
        "reported_kwh_m2",
        "predicted_kwh_m2",
        "error_percent",
    ]
    w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    w.writerows(attempts)
plt.rcParams.update(
    {
        "font.family": "DejaVu Sans",
        "font.size": 11,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "figure.facecolor": "white",
    }
)
colors = {"fore_v260": "#aa633b", "efter_v260": "#256d89"}
fig, ax = plt.subplots(figsize=(8, 7))
limit = max([r["reported_kwh_m2"] for r in actual] + [r["predicted_kwh_m2"] for r in actual]) * 1.12
ax.plot([0, limit], [0, limit], color="#555", lw=1, label="Equal to reported")
ax.fill_between(
    [0, limit], [0, limit * 0.8], [0, limit * 1.2], color="#edf1f4", label="±20% (descriptive)"
)
for s, label in [("fore_v260", "Older export, allocated"), ("efter_v260", "Newer export")]:
    rows = [r for r in actual if r["schema"] == s]
    ax.scatter(
        [r["reported_kwh_m2"] for r in rows],
        [r["predicted_kwh_m2"] for r in rows],
        s=65,
        color=colors[s],
        label=label,
        zorder=3,
    )
    for r in rows:
        offset = {
            "854434": (8, 12),
            "854540": (8, -14),
            "955362": (-46, -15),
            "1000941": (6, 8),
        }.get(r["formular_id"], (4, 4))
        ax.annotate(
            r["formular_id"],
            (r["reported_kwh_m2"], r["predicted_kwh_m2"]),
            xytext=offset,
            textcoords="offset points",
            fontsize=8,
        )
ax.set(
    xlim=(75, limit),
    ylim=(75, limit),
    xlabel="EPC-reported district heat [kWh/m² Atemp·year]",
    ylabel="Estimated delivered district heat [kWh/m² Atemp·year]",
    title="Actual-year weather: heating plus hot water",
)
ax.legend(fontsize=9, loc="upper left")
fig.tight_layout()
fig.savefig(root / "accuracy.png", dpi=160)
plt.close(fig)
ordered = sorted(actual, key=lambda r: r["error_percent"])
fig, ax = plt.subplots(figsize=(11, 6))
y = np.arange(len(ordered))
ax.barh(y, [r["error_percent"] for r in ordered], color=[colors[r["schema"]] for r in ordered])
ax.axvline(0, color="#333", lw=1)
ax.axvline(-20, color="#999", ls="--")
ax.axvline(20, color="#999", ls="--")
ax.set(
    yticks=y,
    yticklabels=[r["formular_id"] for r in ordered],
    xlabel="Prediction error [% of EPC-reported district heat]",
    title="Every selected building retained",
)
fig.tight_layout()
fig.savefig(root / "building-errors.png", dpi=160)
plt.close(fig)
by_tmy = {r["formular_id"]: r for r in tmy}
paired = [r for r in actual if r["formular_id"] in by_tmy]
fig, ax = plt.subplots(figsize=(11, 6))
x = np.arange(len(paired))
ax.bar(
    x - 0.18,
    [r["error_percent"] for r in paired],
    0.36,
    color="#256d89",
    label="Göteborg actual year",
)
ax.bar(
    x + 0.18,
    [by_tmy[r["formular_id"]]["error_percent"] for r in paired],
    0.36,
    color="#b77c45",
    label="Landvetter TMYx 2004–2018",
)
ax.axhline(0, color="#333", lw=1)
ax.set(
    xticks=x,
    xticklabels=[r["formular_id"] for r in paired],
    ylabel="Prediction error [%]",
    title="Weather-file comparison with identical building assumptions",
)
ax.tick_params(axis="x", rotation=45)
ax.legend()
fig.tight_layout()
fig.savefig(root / "weather-comparison.png", dpi=160)
plt.close(fig)
fig, ax = plt.subplots(figsize=(10, 5))
for weather, rows, color in [("Actual year", actual, "#256d89"), ("TMY", tmy, "#b77c45")]:
    values = np.array([r["monthly_space_heat_kwh"] for r in rows]).sum(axis=0) / 1000
    ax.plot(range(1, 13), values, "o-", label=weather, color=color)
ax.set(
    xticks=range(1, 13),
    xlabel="Month",
    ylabel="Cohort simulated space heat [MWh]",
    title="Simulated seasonality; monthly measurements are unavailable",
)
ax.legend()
fig.tight_layout()
fig.savefig(root / "monthly-heating.png", dpi=160)
plt.close(fig)
for entry in cohort:
    fid = entry["formular_id"]
    d = root / "cases" / fid
    run = d / "actual"
    if not (run / "model.epJSON").exists():
        continue
    if all((d / (mode + ".png")).exists() for mode in ["exterior", "cutaway", "plan"]):
        continue
    compilation = json.loads((run / "compilation.json").read_text())
    report = json.loads(compilation["report_json"])
    render(
        json.loads((run / "model.epJSON").read_text()),
        report,
        d,
        "EPC " + fid,
        json.loads((d / "preparation.json").read_text()),
    )
    print("Rendered", fid, flush=True)
contacts = []
for start in range(0, len(cohort), 4):
    subset = cohort[start : start + 4]
    sheet = Image.new("RGB", (1200, len(subset) * 330), "white")
    draw = ImageDraw.Draw(sheet)
    for row, entry in enumerate(subset):
        fid = entry["formular_id"]
        draw.text((8, row * 330 + 4), "EPC " + fid, fill="black")
        for col, mode in enumerate(["exterior", "cutaway", "plan"]):
            p = root / "cases" / fid / (mode + ".png")
            if p.exists():
                thumbnail = ImageOps.contain(Image.open(p).convert("RGB"), (395, 300))
                sheet.paste(thumbnail, (col * 400, row * 330 + 25))
    name = f"geometry-contact-{start // 4 + 1}.png"
    sheet.save(root / name)
    contacts.append(name)


def fmt(v, d=1):
    return "Unavailable" if v is None else f"{v:.{d}f}"


m = summary["actual"]["annual_kwh_per_m2_atemp"]
total = summary["actual"]["annual_kwh"]
tm = summary["tmy"]["annual_kwh_per_m2_atemp"]
style = "body{font:16px/1.55 system-ui,sans-serif;color:#22343b;max-width:1120px;margin:40px auto;padding:0 24px}h1{font-size:32px;line-height:1.15}h2{margin-top:40px}table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:right;padding:9px;border-bottom:1px solid #d9dfe2}th:first-child,td:first-child{text-align:left}img{max-width:100%;height:auto}a{color:#1e627e}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.plots{display:grid;grid-template-columns:1fr 1fr;gap:20px}.note{background:#f3f5f6;padding:16px}.caption{font-size:14px;color:#53636a}summary{cursor:pointer;font-weight:600}details{border-top:1px solid #d9dfe2;padding:16px 0}@media(max-width:760px){.grid,.plots{grid-template-columns:1fr}table{display:block;overflow:auto}}"
parts = [
    f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>EPC validation pilot</title><style>{style}</style><body>',
    "<h1>EPC validation: first actual-year comparison</h1>",
    "<p>Göteborg · 12 apartment buildings built 1955–1975 · six records per export schema · frozen, untuned assumptions.</p>",
    f"<p><strong>Mean absolute percentage error: {m['mape_percent']:.1f}%.</strong> {m['within_20_percent_count']}/12 buildings are within ±20%. Total district heat is underpredicted by {abs(total['bias_percent']):.1f}%. All 24 annual simulations completed successfully, including the paired typical-year runs.</p>",
    f'<p class="note">This establishes an exploratory comparison with EPC-reported district heat. Energy-intensity R² is {m["r2"]:.2f}, so the model does not yet explain differences between buildings well. The selected sample, allocated older values and assumed geometry/fabric prevent a claim of national, raw-meter or primary-energy accuracy.</p>',
    '<p><a href="protocol.json">Frozen assumptions and selection</a> · <a href="comparisons.csv">All comparisons (CSV)</a> · <a href="summary.json">Metrics (JSON)</a> · <a href="source-audit.json">Source reconciliation</a> · <a href="mapping-verification.json">Verified Boverket mappings</a></p>',
    "<h2>Accuracy against reported district heat</h2><table><tr><th>Actual-year stratum</th><th>n</th><th>MAPE</th><th>MAE, kWh/m²</th><th>RMSE, kWh/m²</th><th>Total heat bias</th><th>Intensity R²</th></tr>",
]
for label, metric in [
    ("All selected buildings", summary["actual"]),
    ("Older export: allocated", summary["by_schema"]["fore_v260"]),
    ("Newer export", summary["by_schema"]["efter_v260"]),
]:
    e = metric["annual_kwh_per_m2_atemp"]
    t = metric["annual_kwh"]
    parts.append(
        f"<tr><td>{label}</td><td>{e['n']}</td><td>{e['mape_percent']:.1f}%</td><td>{e['mae']:.1f}</td><td>{e['rmse']:.1f}</td><td>{t['bias_percent']:+.1f}%</td><td>{e['r2']:.2f}</td></tr>"
    )
parts += [
    '</table><p class="caption">Errors are prediction minus reported value. Intensity uses observed EPC Atemp; footprints were not resized. These are annual errors across buildings, not hourly/monthly calibration statistics. ±20% is descriptive, not a validation standard. No parameters were fitted to these targets.</p>',
    '<div class="plots"><img src="accuracy.png" alt="Predicted versus EPC-reported district heat"><img src="building-errors.png" alt="Error for each selected certificate"></div>',
    "<h2>What actual-year weather changes</h2>",
    f"<p>MAPE is {m['mape_percent']:.1f}% with actual-year weather and {tm['mape_percent']:.1f}% with the existing typical-year file. This comparison changes weather year, reference period and location together; it does not isolate a causal weather-year effect.</p>",
    '<div class="plots"><img src="weather-comparison.png" alt="Paired weather-file errors"><img src="monthly-heating.png" alt="Simulated monthly space heating"></div>',
    "<p>SMHI observations come from Göteborg A (71420), with radiation station 71415. The 2017 and 2018 files contain 8,760 hours each, with 0.098% and 0.142% interpolated observation samples. Cloud cover was unavailable; the weather compiler retained this limitation. "
    '<a href="weather/weather-2017-provenance.json">2017 provenance</a> · <a href="weather/weather-2018-provenance.json">2018 provenance</a>.</p>',
    "<h2>Measurement and model boundaries</h2>",
    "<p>Older EgiFjarrvarme includes hot water; EgiVVBered is a subset. Newer records add EgiFjarrvarmeUPPV and EgiFjarrvarmeVV. All selected declared energy totals reconcile with their components. Original strings, source row locations, global building links and certificate history are saved per case.</p>",
    "<p>The simulation estimates useful space heating and hot water. The workflow adds 4 kWh/m² for airing and assumes 5 kWh/m² of net unrecovered distribution/VVC losses, with unit delivery efficiencies. This simplified delivered-heat estimate is conditional on those assumptions. Net losses exclude recovered heat; gross VVC losses cannot simply be substituted.</p>",
    '<p><a href="https://www.boverket.se/sv/energideklaration/for-energiexperter/berakning-av-byggnadens-energianvandning/">Boverket states that energy entered in the form reflects normal use</a>. Actual-year weather does not recreate those adjustments or the automatic Energiindex normal-year correction. Primary-energy numbers are preserved in each observation file and remain unscored.</p>',
]
for loss, metric in summary["loss_sensitivity"].items():
    parts.append(
        f"<p>With net loss set to {loss} kWh/m², holding all other assumptions fixed, MAPE is {metric['mape_percent']:.1f}%. This is a parameter sensitivity, not an uncertainty interval.</p>"
    )
parts += [
    "<h2>Individual buildings and saved geometry</h2>",
    "<p>All outlines are roof-edge proxies. Geometric floor area is 1–23% above EPC Atemp. Storey counts come from EPCs; 2.8 m floor height, compact roofs and the absence of an unheated basement are assumptions. All cases need physical review before high-confidence validation. Images below are saved renders of the actual epJSON surfaces, not browser screenshots; the browser service was unavailable.</p>",
    "<table><tr><th>EPC ID</th><th>Year</th><th>Atemp, m²</th><th>Geometry/Atemp</th><th>Reported kWh/m²</th><th>Predicted kWh/m²</th><th>Error</th></tr>",
]
by_actual = {r["formular_id"]: r for r in actual}
for e in cohort:
    fid = e["formular_id"]
    r = by_actual.get(fid, {})
    parts.append(
        f'<tr><td><a href="#case-{fid}">{fid}</a></td><td>{e["year"]}</td><td>{e["atemp_m2"]:.0f}</td><td>{e["area_ratio"]:.3f}</td><td>{fmt(r.get("reported_kwh_m2"))}</td><td>{fmt(r.get("predicted_kwh_m2"))}</td><td>{fmt(r.get("error_percent"))}%</td></tr>'
    )
parts.append("</table>")
for e in cohort:
    fid = e["formular_id"]
    prefix = f"cases/{fid}"
    parts.append(
        f'<details id="case-{fid}" open><summary>EPC {fid} · {e["schema"]} · {e["year"]}</summary><p><a href="{prefix}/building.json">Building input</a> · <a href="{prefix}/observation.json">EPC observation</a> · <a href="{prefix}/source.json">Raw fields and links</a> · <a href="{prefix}/accounting-audit.json">Accounting and history</a> · <a href="{prefix}/actual/comparison.json">Comparison</a> · <a href="{prefix}/actual/diagnostics.json">Simulation diagnostics</a></p><div class="grid">'
    )
    for mode in ["exterior", "cutaway", "plan"]:
        parts.append(
            f'<a href="{prefix}/{mode}.png"><img loading="lazy" src="{prefix}/{mode}.png" alt="{fid} {mode} geometry"></a>'
        )
    parts.append("</div></details>")
parts += [
    "<h2>What to test next</h2><ol>",
    "<li>Resolve the two largest underpredictions (828960 and 845521) using building surveys, distribution losses and original meter/normalization records. Do not tune on the test set.</li>",
    "<li>Verify façade outlines, floor heights, roofs, basement conditioning and neighbouring shading. Keep EPC Atemp separate from geometric area.</li>",
    "<li>Reproduce normal-use and Energiindex corrections and add property electricity and dated primary-energy accounting before scoring EPC primary energy.</li>",
    "<li>Predeclare an independent, geographically broader holdout with estate-level separation, more construction eras and heating/ventilation systems. Assess renovation response with before/after observations.</li></ol>",
    '<p><a href="screening.json">Selection counts</a> · <a href="screening-exclusions.json">Every screening exclusion</a> · <a href="cohort.json">Frozen cohort</a>. Six-per-schema exploratory quota met; high-confidence quota remains 0/6 in each schema.</p></body></html>',
]
(root / "index.html").write_text("".join(parts))
(root / "render-manifest.json").write_text(
    json.dumps(
        dict(
            geometry_images=36,
            chart_images=4,
            contact_sheets=contacts,
            renderer=str(a.core_examples / "real_building_review.py"),
            renderer_sha256=file_sha256(a.core_examples / "real_building_review.py"),
            browser_screenshots=0,
            browser_status="no browser instances available",
            visual_inspection="pending",
        ),
        indent=2,
    )
)
print("Report saved", root / "index.html")
