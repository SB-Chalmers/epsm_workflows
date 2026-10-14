"""Render independent residual diagnostics as plots and an offline HTML report."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.colors import TwoSlopeNorm
from matplotlib.lines import Line2D
import numpy as np
import pandas as pd
from scipy.spatial.distance import cdist
from scipy.stats import spearmanr

from analyze_archetype_residuals import SEED, dump, read, sha

COLORS = {'inside 20%': '#177b70', 'overpredicted ≥20%': '#bf513c',
          'underpredicted ≥20%': '#456aab', 'simulation failed': '#292b35'}


def supplemental(df, root):
    rng = np.random.default_rng(SEED + 1)
    good = df[df.accepted]
    prop = good.groupby('property_key').agg(
        residual=('residual', 'median'), easting=('easting', 'mean'),
        northing=('northing', 'mean'), measurement_year=('measurement_year', 'median'))
    distances = cdist(prop[['easting', 'northing']], prop[['easting', 'northing']])
    np.fill_diagonal(distances, np.inf)
    weights = np.zeros_like(distances)
    np.put_along_axis(weights, distances.argsort(axis=1)[:, :4], 1, axis=1)
    weights = np.maximum(weights, weights.T)
    weights /= weights.sum(axis=1, keepdims=True)
    z = prop.residual.to_numpy(copy=True)
    z -= z.mean()
    actual = z @ weights @ z / (z @ z)
    year_groups = [np.flatnonzero(prop.measurement_year.to_numpy() == y)
                   for y in prop.measurement_year.unique()]
    simulated = []
    for _ in range(9999):
        permuted = z.copy()
        for index in year_groups:
            permuted[index] = rng.permutation(z[index])
        simulated.append(permuted @ weights @ permuted / (permuted @ permuted))
    conditional = {'graph': 'symmetric row-standardised knn4', 'properties': len(prop),
        'moran_i': actual, 'permutations': 9999, 'conditioning': 'shuffle only within property median measurement year',
        'p': (1 + (np.asarray(simulated) >= actual).sum()) / 10000,
        'null_mean': np.mean(simulated)}
    checks = []
    for label, mask in [('2018 only', df.measurement_year == 2018),
                        ('excluding Biskopsgården', df.area_name != 'BISKOPSGÅRDEN')]:
        selected = df[mask & df.accepted].groupby('property_key').median(numeric_only=True)
        for field in ['height_m', 'atemp_m2', 'gross_to_atemp_ratio', 'wall_area_per_atemp']:
            rho, p = spearmanr(selected[field], selected.residual)
            checks.append({'subset': label, 'field': field, 'properties': len(selected),
                           'rho': rho, 'p_unadjusted_exploratory': p})
    # Property bootstrap of building-level coverage differences. A resampled
    # property brings all its buildings, including failed runs, into the sample.
    keys = df.property_key.unique()
    indices = {key: np.flatnonzero(df.property_key.to_numpy() == key) for key in keys}
    deltas = {'6+ vs ≤5 storeys': [], 'no heated basement vs heated basement': []}
    for _ in range(3000):
        sample = df.iloc[np.concatenate([indices[key] for key in rng.choice(keys, len(keys), replace=True)])]
        for label, indicator in [('6+ vs ≤5 storeys', sample.storeys >= 6),
                                 ('no heated basement vs heated basement', sample.heated_basement == 'no')]:
            if indicator.any() and (~indicator).any():
                deltas[label].append(sample.loc[indicator, 'inside20'].mean() -
                                     sample.loc[~indicator, 'inside20'].mean())
    bootstrap = {label: {'ci95': np.quantile(values, [.025, .975]), 'resamples': len(values)}
                 for label, values in deltas.items()}
    pair = df.dropna(subset=['context_change']).copy()
    paired = {'n': len(pair), 'no_context_inside20': int((abs(pair.no_context_prediction/pair.observed-1) < .2).sum()),
              'context_inside20': int(pair.inside20.sum()), 'mean_heating_increase': pair.context_change.mean(),
              'max_heating_increase': pair.context_change.max(),
              'minimum_heating_increase': pair.context_change.min(),
              'excluded_mismatched_source_flow_building': '535eb1ea-3cee-4022-b23b-6088e8fddecc',
              'interpretation': 'Archive comparison matches 82 current predictions; one source-flow correction prevents its pairing; two failures excluded from pair.'}
    failure_records = []
    campaign = Path(read(root/'manifest.json')['campaign'])
    digest = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True,
                                      separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    identity = digest(read(campaign/'manifest.json'))
    for _, row in df[~df.accepted].iterrows():
        path = campaign/'simulations'/digest((identity, row.building_id, {}))/'receipt.json'
        receipt = read(path)
        failure_records.append({'building_id': row.building_id, 'property_key': row.property_key,
            'receipt': str(path.resolve()), 'sha256': sha(path), 'reason': receipt['reason'],
            'diagnostic_type': 'very small context shading surfaces' if 'Very small surface' in receipt['reason']
                               else 'out-of-range psychrometric calculation'})
    medians = {field: {('inside20' if k else 'outside20'): value for k, value in
                       good.groupby('inside20')[field].median().items()}
               for field in ['observed', 'predicted', 'atemp_m2', 'height_m', 'storeys',
                             'gross_to_atemp_ratio', 'wall_area_per_atemp', 'ground_area_per_atemp',
                             'floor_height_m', 'neighbours']}
    result = {'medians': medians, 'year_conditional_spatial_test': conditional,
              'subset_associations': checks, 'coverage_gap_bootstrap': bootstrap,
              'matched_context_comparison': paired, 'failures': failure_records,
              'property_balanced_coverage20': df.groupby('property_key').inside20.mean().mean(),
              'building_status_counts': df.status20.value_counts().to_dict(),
              'retrospective_exploration': True}
    dump(root/'supplement.json', result)
    return result


def style():
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10,
        'axes.spines.top': False, 'axes.spines.right': False, 'axes.titleweight': 'bold',
        'figure.facecolor': 'white', 'axes.facecolor': '#fafbf9', 'grid.alpha': .17,
        'savefig.facecolor': 'white'})


def save(fig, root, name):
    fig.savefig(root/f'{name}.png', dpi=180, bbox_inches='tight')
    fig.savefig(root/f'{name}.svg', bbox_inches='tight')
    plt.close(fig)


def plots(df, summary, root):
    style()
    good = df[df.accepted]
    prop = pd.DataFrame(read(root/'properties.json'))
    fig, axes = plt.subplots(1, 2, figsize=(13, 6.5), constrained_layout=True)
    for ax in axes:
        ax.set_aspect('equal'); ax.grid(True)
        ax.set(xlabel='SWEREF 99 TM easting (km)', ylabel='Northing (km)')
    for status, data in df.groupby('status20'):
        axes[0].scatter(data.easting/1000, data.northing/1000, s=38,
            color=COLORS[status], marker='x' if status == 'simulation failed' else 'o',
            edgecolors='white' if status != 'simulation failed' else None,
            linewidths=.6, alpha=.85, label=f'{status} · {len(data)}')
    axes[0].legend(loc='lower left', fontsize=9, framealpha=.95)
    accepted = prop[prop.residual.notna()]
    points = axes[1].scatter(accepted.easting/1000, accepted.northing/1000,
        c=accepted.residual, s=55, cmap='RdBu_r', norm=TwoSlopeNorm(0, -65, 65),
        edgecolors='#334', linewidths=.4)
    fig.colorbar(points, ax=axes[1], shrink=.63, label='Median prediction − observation (kWh/m²/year)')
    failed = prop[prop.residual.isna()]
    axes[1].scatter(failed.easting/1000, failed.northing/1000, marker='x', c='#292b35', s=55)
    labels = df.groupby('area_name')[['easting', 'northing']].mean()
    offsets = {'BISKOPSGÅRDEN': (-60, 12), 'BACKA': (8, -5), 'HJÄLLBO': (12, -12),
               'GÅRDSTEN': (10, -10), 'KÄRRA': (10, 8), 'ANGERED': (8, 9), 'JÄRNBROTT': (-65, -17),
               'KOBBEGÅRDEN': (8, 8), 'TYNNERED': (-65, 10), 'ASKIM': (8, -12),
               'LANDALA': (7, 8), 'MAJORNA': (-60, 8)}
    for ax in axes:
        for name, row in labels.iterrows():
            ax.annotate(name.title(), (row.easting/1000, row.northing/1000),
                xytext=offsets.get(name, (7, 7)), textcoords='offset points', fontsize=8,
                bbox=dict(facecolor='white', alpha=.8, edgecolor='none', pad=.9))
    axes[0].set_title('Building errors · 85 attempted buildings')
    axes[1].set_title('Property errors · 54 distinct properties')
    fig.suptitle('Gothenburg SE.02 multifamily archetype · frozen unfitted baseline', fontsize=15)
    save(fig, root, 'spatial-errors')

    fig, axes = plt.subplots(2, 3, figsize=(14, 8.2), constrained_layout=True)
    settings = [('observed', 'Observed space heat (kWh/m²/year)'),
                ('atemp_m2', 'EPC Atemp (m²)'), ('height_m', 'Model eave/flat-roof height (m)'),
                ('gross_to_atemp_ratio', 'Model gross conditioned area / Atemp'),
                ('wall_area_per_atemp', 'Exposed wall area / Atemp'),
                ('ground_area_per_atemp', 'Ground contact area / Atemp')]
    for ax, (field, title) in zip(axes.flat, settings, strict=True):
        if field == 'observed':
            ax.fill_between([good.observed.min(), good.observed.max()],
                -.2*np.array([good.observed.min(), good.observed.max()]),
                .2*np.array([good.observed.min(), good.observed.max()]), color='#d4e9e5', alpha=.6)
        for basement, data in good.groupby('heated_basement'):
            ax.scatter(data[field], data.residual, s=28,
                c=[COLORS[s] for s in data.status20], marker='^' if basement == 'yes' else 'o', alpha=.85)
        ax.axhline(0, color='#777', lw=.8); ax.grid(True)
        if field == 'atemp_m2': ax.set_xscale('log')
        ax.set(xlabel=title, ylabel='Prediction − observation (kWh/m²/year)')
    fig.suptitle('The mismatch follows size and surface exposure', fontsize=15)
    fig.legend(handles=[Line2D([], [], marker='o', ls='', color=COLORS['inside 20%'], label='Inside 20%'),
                        Line2D([], [], marker='o', ls='', color=COLORS['overpredicted ≥20%'], label='Overpredicted ≥20%'),
                        Line2D([], [], marker='^', ls='', color='#555', label='Reported heated basement')],
               loc='outside lower center', ncol=3, frameon=False)
    save(fig, root, 'error-descriptors')

    selected = [('storey_band', '2–3'), ('storey_band', '4–5'), ('storey_band', '6+'),
                ('heated_basement', 'no'), ('heated_basement', 'yes'),
                ('certificate_class', 'D'), ('certificate_class', 'E'), ('certificate_class', 'F'),
                ('measurement_year', '2018'), ('measurement_year', '2019'), ('measurement_year', '2024')]
    label = ['2–3 storeys', '4–5 storeys', '6+ storeys', 'No reported heated basement',
             'Reported heated basement', 'Certificate D', 'Certificate E', 'Certificate F',
             'Measured 2018', 'Measured 2019', 'Measured 2024']
    rows = [next(r for r in summary['groups'] if r['field'] == f and r['value'] == v) for f, v in selected]
    fig, ax = plt.subplots(figsize=(11, 6.7), constrained_layout=True)
    y = np.arange(len(rows))
    inside = np.array([r['inside20']/r['n']*100 for r in rows])
    failed = np.array([r['failed']/r['n']*100 for r in rows])
    outside = 100-inside-failed
    ax.barh(y, inside, color=COLORS['inside 20%'], label='Inside 20%')
    ax.barh(y, outside, left=inside, color=COLORS['overpredicted ≥20%'], label='Outside 20%')
    ax.barh(y, failed, left=inside+outside, color=COLORS['simulation failed'], label='Failed')
    for i, r in enumerate(rows):
        ax.text(102, i, f"{r['inside20']}/{r['n']} inside · {r['properties']} properties", va='center', fontsize=9)
    ax.set(yticks=y, yticklabels=label, xlim=(0, 153), xlabel='Share of attempted buildings (%)',
           title='Which groups match the archetype?')
    ax.set_xticks([0, 20, 40, 60, 80, 100]); ax.invert_yaxis()
    ax.legend(loc='lower center', bbox_to_anchor=(.47, -.19), ncol=3, frameon=False)
    save(fig, root, 'group-coverage')


def table(rows, columns):
    def value(x):
        return f'{x:.3f}' if isinstance(x, float) else str(x)
    return '<table><thead><tr>'+''.join(f'<th>{html.escape(label)}</th>' for _, label in columns)+\
           '</tr></thead><tbody>'+''.join('<tr>'+''.join(f'<td>{html.escape(value(row.get(key, "")))}</td>'
                                                      for key, _ in columns)+'</tr>' for row in rows)+'</tbody></table>'


def report(df, summary, supplement, root):
    # The offline map uses true metric locations and property-area labels. No tile
    # requests, external libraries, geocoding, tracking or network access.
    xmin, xmax = df.easting.min()-900, df.easting.max()+900
    ymin, ymax = df.northing.min()-900, df.northing.max()+900
    scale = min(960/(xmax-xmin), 690/(ymax-ymin))
    map_rows = []
    for _, row in df.iterrows():
        map_rows.append({k: row[k] for k in ['building_id', 'property_key', 'area_name', 'address',
            'status20', 'observed', 'predicted', 'ape', 'residual', 'height_m', 'atemp_m2',
            'gross_to_atemp_ratio', 'heated_basement', 'certificate_class', 'measurement_year',
            'longitude', 'latitude']} | {'x': 30+(row.easting-xmin)*scale,
                                        'y': 710-(row.northing-ymin)*scale})
    encoded = json.dumps(map_rows, ensure_ascii=False, default=float, allow_nan=True).replace('NaN', 'null')
    encoded = encoded.replace('<', '\\u003c')
    labels = ''.join(f'<text x="{30+(r.easting-xmin)*scale:.1f}" y="{702-(r.northing-ymin)*scale:.1f}" class="map-label">{html.escape(name.title())}</text>'
                     for name, r in df.groupby('area_name')[['easting', 'northing']].mean().iterrows())
    area_rows = sorted([r for r in summary['groups'] if r['field'] == 'area_name'], key=lambda r: -r['bias'] if r['bias'] is not None else -999)
    spatial_rows = [r for r in summary['spatial'] if r['outcome'] in ['residual', 'unexplained']]
    correlation_rows = [r for r in summary['associations'] if r['outcome'] == 'residual']
    cv_rows = [dict(model=name, **values) for name, values in summary['residual_cv'].items()]
    fail_list = ''.join(f'<li><b>{html.escape(r["property_key"])}</b>: {html.escape(r["diagnostic_type"])}. <a href="{html.escape(r["receipt"])}">Retained receipt</a>.</li>'
                        for r in supplement['failures'])
    body = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>EPSM archetype residual diagnosis</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f3f4ef;color:#24302d;font:16px/1.65 system-ui,sans-serif}}main{{max-width:1200px;margin:auto;padding:36px 30px}}h1{{font-size:36px;line-height:1.2;max-width:850px}}h2{{font-size:24px;margin-top:40px}}.lead{{font-size:21px;max-width:950px}}.eyebrow{{font-size:12px;letter-spacing:2px;text-transform:uppercase;color:#527168}}.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}}.card{{background:white;padding:18px;border:1px solid #d7ddd6;border-radius:10px}}.card strong{{display:block;font-size:30px}}.card span{{font-size:13px;color:#57675f}}.panel{{background:white;border:1px solid #d7ddd6;padding:20px;border-radius:12px;margin:24px 0}}img{{width:100%;height:auto}}table{{border-collapse:collapse;width:100%;font-size:13px;margin:20px 0}}td,th{{border-bottom:1px solid #dce1da;padding:9px;text-align:left}}th{{background:#eef2ec}}.scroll{{overflow:auto}}a{{color:#126c62}}.note{{font-size:14px;color:#5a675d}}.warning{{border-left:4px solid #bf513c;padding:12px 18px;background:#fff6ee}}select,input,button{{font:inherit;padding:7px 12px;border:1px solid #c4cfc6;border-radius:6px;background:white}}svg{{width:100%;height:auto;max-height:720px;background:#f8faf6;touch-action:none}}.map-label{{font-size:12px;fill:#394b40;paint-order:stroke;stroke:#f8faf6;stroke-width:4px;pointer-events:none}}circle{{cursor:pointer}}#details{{min-height:92px;background:#eef4ee;padding:14px;border-radius:7px;font-size:14px}}.controls{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:12px}}.foot{{font-size:13px;color:#5a675d}}@media(max-width:700px){{main{{padding:20px 15px}}.cards{{grid-template-columns:repeat(2,1fr)}}h1{{font-size:29px}}}}
</style><main><div class="eyebrow">Retrospective diagnosis · Gothenburg · 4 October 2026</div>
<h1>The archetype misses smaller buildings and particular estates</h1>
<p class="lead">The errors are structured. Taller, larger buildings match much better; the largest overpredictions cluster around Biskopsgården, Backa and Kärra. Geometry, measurement year and shared property evidence overlap with location.</p>
<div class="cards"><div class="card"><strong>29 / 85</strong><span>Below 20% error · 34.1%</span></div><div class="card"><strong>54</strong><span>Accepted predictions outside 20% · all overpredictions</span></div><div class="card"><strong>+27.8</strong><span>Mean error in kWh/m²/year</span></div><div class="card"><strong>54</strong><span>Properties · 52 with accepted predictions</span></div></div>
<p class="warning">Scope: frozen <b>unfitted parent-v10 baseline</b>, not a completed calibrated validation. All 85 buildings are GIS/EPC-consistent SE.02 multifamily, F ventilation, district heat, Gothenburg. The two failed simulations remain in coverage denominators. All observations have already been inspected; these are exploratory findings.</p>
<h2>Inside versus outside 20%</h2>
{table([dict(descriptor=k,**v) for k,v in supplement['medians'].items()], [('descriptor','Descriptor · median'),('inside20','Inside 20% · n=29'),('outside20','Outside 20% · n=54')])}
<p>Size and exposure are the strongest physical descriptors. Height has property-level Spearman ρ=−0.47 with absolute-unit signed error; Atemp ρ=−0.56; wall area/Atemp ρ=+0.50. The associations survive exploratory multiple-comparison adjustment. Height and size still associate with error in the 2018-only subset and after excluding Biskopsgården. Footprint compactness alone provides little separation.</p>
<img src="error-descriptors.png" alt="Six scatter plots relating signed heat prediction errors to measured demand, size, height and surface exposure">
<h2>Groups that do and do not match</h2><img src="group-coverage.png" alt="Inside versus outside 20 percent error by height, basement, class and measurement year">
<p>Six or more storeys: <b>21/30</b> inside. Two–three storeys: <b>8/38</b>. Four–five storeys: <b>0/17</b>, heavily confounded with the 2024 Biskopsgården estate. The property bootstrap 95% interval for the 6+ versus ≤5-storey coverage gap is {supplement['coverage_gap_bootstrap']['6+ vs ≤5 storeys']['ci95'][0]*100:.1f}–{supplement['coverage_gap_bootstrap']['6+ vs ≤5 storeys']['ci95'][1]*100:.1f} percentage points.</p>
<p>Reported heated basements: <b>8/29</b> inside; no reported heated basement: <b>21/56</b>. Bias is similar (+29.3 vs +27.1 kWh/m²), so basement presence alone cannot explain the broad mismatch. All five models with gross conditioned area/Atemp &gt;1.40 are outside or failed, but this is only four properties. The heated-area representation remains worth auditing.</p>
<p>Class D: <b>0/14</b> inside; E: <b>17/51</b>; F: <b>12/20</b>. Twelve D buildings are three Biskopsgården properties measured in 2024. Their reported intensities are identical within each property (62.3–63.0 kWh/m²), while predictions are 113.0–122.8. Confirm renovation state, metering and any energy allocation before changing physics. Class is derived from energy performance and is not an independent explanation or an assignment feature.</p>
<p>The lowest measured-demand quartile has only <b>2/22</b> inside; the highest has <b>10/20</b>. Lower demand makes percentage error more sensitive, but the absolute bias also increases (47.6 vs 14.3 kWh/m²). This is more than a denominator effect. Six buildings with dated window-replacement evidence are all outside (five properties); the records do not provide a measured U-value.</p>
<h2>Explore the spatial errors</h2><div class="panel"><div class="controls"><label>Show <select id="filter"><option value="all">All 85 buildings</option><option value="inside">Inside 20%</option><option value="outside">Outside 20%</option><option value="failed">Simulation failures</option></select></label><label>Colour <select id="metric"><option value="status">20% classification</option><option value="residual">Error · kWh/m²/year</option><option value="ape">Absolute error · %</option><option value="basement">Heated basement</option></select></label><button id="reset">Reset map</button></div><p class="note">Scroll to zoom, drag to pan, hover or click a point for the building record. Metric locations use SWEREF 99 TM; labels are property-area names, not official neighbourhood boundaries. No online basemap.</p><svg id="map" viewBox="0 0 1020 740" role="img" aria-label="Interactive georeferenced map of the 85 building errors"><g id="scene">{labels}<g id="points"></g></g></svg><div id="details">Select a building to inspect its source and model descriptors.</div></div>
<img src="spatial-errors.png" alt="Spatial map of building error categories and median property signed residuals">
<p>Property-level spatial clustering is strong: k=4 Moran’s I=<b>0.415</b>, one-sided permutation p=<b>0.0001</b>. Error magnitude and poor coverage also cluster. Results persist with k=8 and 500/1,000/2,000 m radius graphs; radius tests explicitly exclude isolated properties.</p>
<p>When shuffled only within measurement year, the same k=4 test has p=<b>{supplement['year_conditional_spatial_test']['p']:.4f}</b>. This addresses one confound, not all of them. After an input-only residual model accounts for geometry, basement, roof, position, neighbours and year, remaining clustering is weaker and depends on neighbourhood definition (k=4 p=.0196; k=8 p=.6097). Geographic coordinates add little to held-out residual prediction.</p>
{table(area_rows, [('value','Property-area name'),('n','Buildings'),('properties','Properties'),('inside20','Inside 20%'),('failed','Failed'),('bias','Bias · kWh/m²'),('median_ape','Median error %')])}
<h2>How much do input descriptors explain?</h2>
{table(cv_rows, [('model','Residual model'),('building_mae_residual','Building MAE · kWh/m²'),('property_mae_residual','Property MAE · kWh/m²'),('property_r2_residual','Property held-out R²')])}
<p>Fixed Ridge regularisation, five folds with whole properties held out, and equal property training influence. The physical-input model reduces building residual MAE from 16.6 to 10.5 kWh/m² versus a training-mean bias model; property R² is 0.35. These are predictive associations within previously inspected data, not a physical correction, a validated new archetype or a national transfer result. Neither observed energy nor certificate class enters the model.</p>
<h2>What this means for EPSM Core</h2>
<ol><li><b>Expose and audit component heat losses first.</b> The residual follows wall/ground exposure and size more clearly than footprint shape. Check measured leakage against prescribed ACH, exhaust-induced infiltration, floor heights, exposed walls and Kiva perimeter assumptions using heat-balance outputs and matched models. The data do not identify which mechanism causes the pattern.</li>
<li><b>Improve heated-space representation where evidence requires it.</b> Partial basements, partial conditioning and occupied attics are known representation limits. Keep geometry, Atemp for loads and heated extent separate. Their contribution to this cohort's errors is not established; basement presence alone is a weak discriminator. Do not resize footprints to force energy agreement.</li>
<li><b>Use existing component-state controls before changing envelope defaults.</b> Better-performing class D buildings and dated window replacements point to heterogeneous actual states. Current Core already supports glazing overrides, component renovation and zone programmes. Spatial opaque construction assignments would help when actual mixed fabric is evidenced.</li>
<li><b>Keep context physics, fix context robustness.</b> Among 82 truly matched archived pairs, measured shading raises heat by {supplement['matched_context_comparison']['mean_heating_increase']:.2f} kWh/m² on average; inside-20 coverage falls from 36/82 to 28/82. Shading exposes rather than solves the existing overprediction. Check party-wall evidence separately.</li>
<li><b>Distinguish failures and expose component outputs.</b> One model fails on tiny context shading surfaces; another on an out-of-range psychrometric calculation. Harden geometry emission against the engine’s surface-size constraints and retain provenance; investigate the psychrometric case separately. Do not waive warnings or treat either as an annual prediction error. Structured wall/roof/ground/ventilation/infiltration/solar diagnostics would make future stock diagnosis easier.</li></ol>
<ul>{fail_list}</ul>
<p><b>Assessment:</b> targeted systemic improvements are justified. The findings do not establish that a replacement engine, universal basement-temperature adjustment or more detailed apartment zoning would fix these errors. The next discriminating study should compare physics components for small versus tall buildings and independently verify the largest estate outliers.</p>
<h2>Methods and audit trail</h2><p>The strict criterion is |prediction−observation|/observation &lt;0.20. Errors are delivered district space heat per EPC Atemp; DHW is excluded. Global statistics use 52 properties with accepted predictions; all 54 properties remain in coverage. Property medians reduce repeated-building influence. Spearman tests use 9,999 property permutations, with Benjamini–Hochberg adjustment across the numeric association screen. Spatial tests use 9,999 permutations and adjustment across all 25 graphs/outcomes. Signed error, absolute-unit magnitude and percentage error are examined separately. Post-hoc strata and confidence intervals are descriptive, not independently validated causal results.</p>
<details><summary>Numeric property associations</summary>{table(correlation_rows,[('field','Descriptor'),('properties','Properties'),('rho','Spearman ρ'),('permutation_p_two_sided','Permutation p'),('fdr_q','FDR q')])}</details>
<details><summary>Spatial tests before and after input adjustment</summary>{table(spatial_rows,[('graph','Graph'),('outcome','Outcome'),('properties','Properties'),('islands_excluded','Excluded islands'),('moran_i','Moran I'),('permutation_p_positive_clustering','Permutation p'),('fdr_q','FDR q')])}</details>
<details><summary>All buildings · full table</summary><div class="scroll">{table(sorted(df.to_dict('records'),key=lambda r: -(r['ape'] if pd.notna(r['ape']) else 999)),[('building_id','Building ID'),('property_key','Property'),('status20','Status'),('observed','Observed'),('predicted','Predicted'),('ape','Error %'),('atemp_m2','Atemp'),('height_m','Height'),('gross_to_atemp_ratio','Area ratio'),('heated_basement','Basement'),('certificate_class','Class'),('measurement_year','Year')])}</div></details>
<p class="foot">Machine-readable evidence: <a href="analysis.json">analysis.json</a> · <a href="supplement.json">supplement.json</a> · <a href="buildings.json">building records</a> · <a href="properties.json">property records</a> · <a href="manifest.json">input checksums</a>. Frozen source snapshots are retained locally. No campaign, catalogue or Core input was changed.</p></main>
<script>
const data={encoded}; const colours={json.dumps(COLORS)};const points=document.getElementById('points'),scene=document.getElementById('scene'),svg=document.getElementById('map');
let selected=null,tx=0,ty=0,zoom=1,drag=null;
function format(v){{return v===null?'failed':Number(v).toFixed(1)}}
function details(d){{const box=document.getElementById('details');box.replaceChildren();const title=document.createElement('strong');title.textContent=d.property_key+' · '+d.address;box.append(title,document.createElement('br'));const text=document.createElement('span');text.textContent=d.status20+' | observed '+format(d.observed)+' | predicted '+format(d.predicted)+' kWh/m²/year | error '+format(d.ape)+'% | Atemp '+format(d.atemp_m2)+' m² | height '+format(d.height_m)+' m | basement '+d.heated_basement+' | class '+d.certificate_class+' | measured '+d.measurement_year;box.append(text,document.createElement('br'));const id=document.createElement('small');id.textContent=d.building_id+' · '+d.latitude.toFixed(5)+', '+d.longitude.toFixed(5);box.append(id)}}
function color(d,m){{if(m==='status')return colours[d.status20];if(m==='basement')return d.heated_basement==='yes'?'#6c5ca6':'#559382';if(d.predicted===null)return '#292b35';const v=m==='ape'?d.ape:d.residual;const f=Math.min(1,Math.abs(v)/(m==='ape'?120:65));return v<0?'rgb('+Math.round(200*(1-f))+','+Math.round(210-100*f)+','+Math.round(220-55*f)+')':'rgb('+Math.round(220-35*f)+','+Math.round(210-150*f)+','+Math.round(200-155*f)+')'}}
function draw(){{points.replaceChildren();const filter=document.getElementById('filter').value,metric=document.getElementById('metric').value;for(const d of data){{if(filter==='inside'&&d.status20!=='inside 20%')continue;if(filter==='outside'&&(d.predicted===null||d.ape<20))continue;if(filter==='failed'&&d.predicted!==null)continue;const c=document.createElementNS('http://www.w3.org/2000/svg','circle');c.setAttribute('cx',d.x);c.setAttribute('cy',d.y);c.setAttribute('r',d.predicted===null?6:4.5);c.setAttribute('fill',color(d,metric));c.setAttribute('stroke','#fff');c.setAttribute('stroke-width','1');c.setAttribute('tabindex','0');c.setAttribute('aria-label',d.property_key+' '+d.status20);c.addEventListener('mouseenter',()=>details(d));c.addEventListener('focus',()=>details(d));c.addEventListener('click',()=>details(d));points.append(c)}}}}
function move(){{scene.setAttribute('transform',`translate(${{tx}} ${{ty}}) scale(${{zoom}})`)}}
function position(e){{const p=svg.createSVGPoint();p.x=e.clientX;p.y=e.clientY;return p.matrixTransform(svg.getScreenCTM().inverse())}}
svg.addEventListener('wheel',e=>{{e.preventDefault();const p=position(e),next=Math.max(.7,Math.min(12,zoom*(e.deltaY<0?1.15:1/1.15)));tx=p.x-(p.x-tx)*next/zoom;ty=p.y-(p.y-ty)*next/zoom;zoom=next;move()}},{{passive:false}});
svg.addEventListener('pointerdown',e=>{{if(e.target.tagName==='circle')return;const p=position(e);drag={{x:p.x,y:p.y,tx,ty}};svg.setPointerCapture(e.pointerId)}});svg.addEventListener('pointermove',e=>{{if(!drag)return;const p=position(e);tx=drag.tx+p.x-drag.x;ty=drag.ty+p.y-drag.y;move()}});svg.addEventListener('pointerup',()=>drag=null);svg.addEventListener('pointercancel',()=>drag=null);document.getElementById('reset').onclick=()=>{{tx=ty=0;zoom=1;move()}};document.getElementById('filter').onchange=draw;document.getElementById('metric').onchange=draw;draw();
</script></html>'''
    (root/'report.html').write_text(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('analysis', type=Path)
    args = parser.parse_args()
    root = args.analysis
    df = pd.DataFrame(read(root/'buildings.json'))
    summary = read(root/'analysis.json')
    supplement = supplemental(df, root)
    plots(df, summary, root)
    report(df, summary, supplement, root)
    manifest = read(root/'manifest.json')
    manifest['render_script_sha256'] = sha(__file__)
    manifest['outputs'] = {p.name: sha(p) for p in root.iterdir() if p.is_file() and p.name != 'manifest.json'}
    dump(root/'manifest.json', manifest)
    print(json.dumps({'conditional_spatial': supplement['year_conditional_spatial_test'],
                      'bootstrap': supplement['coverage_gap_bootstrap'], 'report': str(root/'report.html')},
                     default=lambda x: x.tolist() if isinstance(x,np.ndarray) else float(x), indent=2))


if __name__ == '__main__':
    main()
