"""Post-hoc diagnosis of frozen archetype predictions; never fits Core parameters.

Run with the existing Core study environment. Output is an independent snapshot;
campaign files, splits, catalogues and simulation inputs are never modified.
"""

from __future__ import annotations

import argparse
import codecs
import csv
import hashlib
import html
import json
import re
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
from pyproj import Transformer
from scipy.spatial.distance import cdist
from scipy.stats import spearmanr
from shapely import from_wkb
from sklearn.cluster import DBSCAN
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import GroupKFold
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

ROOT = Path(__file__).resolve().parents[2]
SEED = 20261004


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dump(path, value):
    def clean(x):
        if isinstance(x, dict):
            return {str(k): clean(v) for k, v in x.items()}
        if isinstance(x, (tuple, list, np.ndarray)):
            return [clean(v) for v in x]
        if isinstance(x, (float, np.floating)):
            return float(x) if np.isfinite(x) else None
        if isinstance(x, np.integer):
            return int(x)
        return x

    Path(path).write_text(json.dumps(clean(value), indent=2, ensure_ascii=False))


def raw_sources(cohort):
    wanted = defaultdict(dict)
    for row in cohort:
        wanted[row['source_file']][row['source_line']] = row
    found, hashes = {}, {}
    for filename, lines in wanted.items():
        path = Path(filename)
        hashes[str(path)] = sha(path)
        with path.open('rb') as stream:
            encoding = 'utf-16' if stream.read(4).startswith(
                (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else 'latin-1'
        remaining = set(lines)
        with path.open(encoding=encoding, newline='') as stream:
            for line, raw in enumerate(csv.DictReader(stream, delimiter='|'), 2):
                if line not in lines:
                    continue
                row = lines[line]
                signature = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
                assert signature == row['source_row_sha256'], 'Source row changed'
                assert int(raw['FormularId']) == row['formular_id'], 'Certificate changed'
                found[row['building_id']] = raw
                remaining.remove(line)
                if not remaining:
                    break
    assert len(found) == len(cohort)
    return found, hashes


def bh(pvalues):
    """Benjamini-Hochberg exploratory FDR adjustment."""
    p = np.asarray(pvalues)
    order = np.argsort(p)
    adjusted = np.minimum.accumulate((p[order] * len(p) /
                                     np.arange(1, len(p) + 1))[::-1])[::-1]
    q = np.empty_like(p)
    q[order] = np.minimum(adjusted, 1)
    return q


def load_data(campaign, cohort_path, output, prediction_mode='baseline'):
    cohort = read(cohort_path)
    source, source_hashes = raw_sources(cohort)
    features = read(campaign / 'features.json')
    predictions = read(campaign / ('predictions.json' if prediction_mode == 'calibrated'
                                  else 'baseline-predictions-before-targets.json'))
    frozen = read(campaign / 'baseline-frozen.json')
    assert frozen['fitted'] is False
    assert frozen['campaign_manifest_sha256'] == sha(campaign / 'manifest.json')
    targets = {}
    target_hashes = {}
    for path in sorted((campaign / 'targets').glob('*.json')):
        targets.update(read(path))
        target_hashes[str(path.resolve())] = sha(path)
    byid = {row['building_id']: row for row in cohort}
    pred = {row['building_id']: row['deployed_prediction' if prediction_mode == 'calibrated' else 'prediction']
            for row in predictions}
    prediction_records = {row['building_id']: row for row in predictions}
    assert len(pred) == len(predictions), 'Duplicate prediction identities'
    assert set(pred) == {r['building_id'] for r in features}
    transform = Transformer.from_crs(3006, 4326, always_xy=True)
    records = []
    snapshot = output / 'snapshot'
    snapshot.mkdir()
    inputs = {str(cohort_path.resolve()): sha(cohort_path), **source_hashes, **target_hashes}
    for name in ['features.json', 'baseline-predictions-before-targets.json',
                 'baseline-evaluation.json', 'baseline-frozen.json', 'manifest.json',
                 'membership-audit.json', 'execution.json', 'predictions.json', 'evaluation.json']:
        path = campaign / name
        if path.exists():
            inputs[str(path.resolve())] = sha(path)
            (snapshot / name).write_bytes(path.read_bytes())
    dump(snapshot / 'cohort.json', cohort)
    context_comparison = ROOT / 'output/archetype-parent-validation-2026-10-04-v1/baseline-context-comparison.json'
    context = {r['building_id']: r for r in read(context_comparison)['rows']}
    inputs[str(context_comparison)] = sha(context_comparison)
    for feature in features:
        bid = feature['building_id']
        row, raw = byid[bid], source[bid]
        geometry = from_wkb(bytes.fromhex(row['geometry_wkb_hex']))
        rectangle = geometry.minimum_rotated_rectangle
        sides = np.linalg.norm(np.diff(np.asarray(rectangle.exterior.coords), axis=0), axis=1)
        x, y = geometry.centroid.x, geometry.centroid.y
        lon, lat = transform.transform(x, y)
        path = campaign / feature['spec_path']
        spec = read(path)
        inputs[str(path.resolve())] = sha(path)
        elevations = spec['geometry']['parts'][0]['storey_elevations_m']
        if prediction_mode == 'calibrated':
            assert prediction_records[bid]['target'] == targets[bid], 'Prediction target changed'
            assert prediction_records[bid]['property_key'] == feature['property_key'], 'Prediction property changed'
        record = row | feature | {
            'observed': targets[bid], 'predicted': pred[bid],
            'easting': x, 'northing': y, 'longitude': lon, 'latitude': lat,
            'height_m': elevations[-1], 'floor_height_m': elevations[-1] / feature['storeys'],
            'footprint_compactness': 4 * np.pi * geometry.area / geometry.length**2,
            'footprint_aspect_ratio': sides.max() / sides.min(),
            'footprint_rectangularity': geometry.area / rectangle.area,
            'footprint_vertices': len(geometry.exterior.coords) - 1 if geometry.geom_type == 'Polygon' else None,
            'neighbours': feature['context_audit']['neighbours'],
            'context_unsupported_count': len(feature['context_audit']['unsupported']),
            'verified_party_walls': feature['context_audit']['verified_party_walls'],
            'contact_candidate_count': len(feature['context_audit']['contact_candidates']),
            'overlap_neighbours_excluded': sum(r.get('reason') == 'target_overlap'
                for r in spec['context'].get('generation', {}).get('source_exclusions', [])),
            'certificate_class': raw.get('EgiEnergiklass') or 'unknown',
            'certificate_performance': raw.get('EgiEnergiPrestanda') or None,
            'certificate_reference_version': raw.get('EgiVersion') or 'unknown',
            'address': raw.get('IdAdr') or '', 'postcode': raw.get('IdPostnr') or '',
            'area_name': re.sub(r'\s+\d.*$', '', row['property_key'].split('|')[-1]),
            'no_context_prediction': context.get(bid, {}).get('no_context_prediction'),
            'context_change': context.get(bid, {}).get('change_kwh_m2'),
            'model_ventilation_l_s_m2': spec['airflow']['mechanical_vent_l_s_m2'],
            'model_infiltration_ach': spec['airflow']['envelope_infiltration_ach'],
            'model_infiltration_l_s_m2': spec['airflow'].get('envelope_infiltration_l_s_m2'),
            'model_heating_setpoint_c': spec['use']['heating_setpoint']['values'][0],
            'baseline_gross_conditioned_area_m2': feature['gross_to_atemp_ratio'] * feature['atemp_m2'],
            'selected_method': prediction_records[bid].get('selected_method', 'baseline'),
            'assignment': prediction_records[bid].get('assignment', 'baseline'),
            'baseline_prediction': prediction_records[bid].get('predictions', {}).get('baseline', pred[bid]),
        }
        # Context comparison is admissible only when its measured-context prediction
        # exactly matches this frozen baseline; never align by row position.
        record['context_comparison_matches_current_baseline'] = (
            bid in context and pred[bid] is not None and
            abs(pred[bid] - context[bid]['measured_context_prediction']) < 1e-9)
        if not record['context_comparison_matches_current_baseline']:
            record['no_context_prediction'] = None
            record['context_change'] = None
        records.append(record)
    df = pd.DataFrame(records)
    df['residual'] = df.predicted - df.observed
    df['signed_percent'] = df.residual / df.observed * 100
    df['ape'] = df.signed_percent.abs()
    df['accepted'] = df.predicted.notna()
    df['inside20'] = df.accepted & (df.ape < 20)
    df['status20'] = np.select([~df.accepted, df.inside20, df.signed_percent >= 20],
                              ['simulation failed', 'inside 20%', 'overpredicted ≥20%'],
                              default='underpredicted ≥20%')
    df['observed_deviation_from_median'] = df.observed - df.observed.median()
    df['gross_above_ground_to_atemp'] = df.upper_gross_area_m2 / df.atemp_m2
    df['log_atemp'] = np.log(df.atemp_m2)
    df['basement_indicator'] = (df.heated_basement == 'yes').astype(float)
    df['area_ratio_band'] = pd.cut(df.gross_to_atemp_ratio, [-np.inf, 1.2, 1.4, np.inf],
                                  labels=['≤1.20', '1.20–1.40', '>1.40']).astype(str)
    df['storey_band'] = pd.cut(df.storeys, [-np.inf, 3, 5, np.inf],
                              labels=['2–3', '4–5', '6+']).astype(str)
    df['observed_quartile'] = pd.qcut(df.observed, 4, labels=['Q1 low', 'Q2', 'Q3', 'Q4 high']).astype(str)
    dump(output / 'manifest.json', {
        'analysis': f'post-hoc frozen {prediction_mode} predictions; no Core fitting or new holdout claims',
        'prediction_mode': prediction_mode,
        'campaign': str(campaign.resolve()), 'source_hashes': inputs,
        'script_sha256': sha(__file__), 'seed': SEED,
        'threshold': 'abs(predicted-observed)/observed < 0.20; failures stay in coverage denominator',
        'observed_boundary': 'normal-use-adjusted delivered district space heat, excluding DHW; actual-year weather',
        'limitations': ['85 GIS/EPC-consistent SE.02 MF F district-heat buildings in Gothenburg',
                        'All observations already inspected; exploratory associations, not causes',
                        'Certificate class and observed energy are outcome-derived descriptors',
                        'Targets opened only after verifying saved frozen baseline identity',
                        'Core specifications and input-only assignments unchanged'],
    })
    return df


def group_summary(df, column):
    result = []
    for value, group in df.groupby(column, dropna=False, observed=True):
        good = group[group.accepted]
        result.append({'field': column, 'value': str(value), 'n': len(group),
                       'properties': group.property_key.nunique(), 'accepted': len(good),
                       'inside20': int(group.inside20.sum()), 'failed': int((~group.accepted).sum()),
                       'coverage20': group.inside20.mean(), 'bias': good.residual.mean(),
                       'mae': good.residual.abs().mean(), 'median_ape': good.ape.median(),
                       'median_observed': group.observed.median(),
                       'median_predicted': good.predicted.median(),
                       'property_balanced_coverage20': group.groupby('property_key').inside20.mean().mean()})
    return result


NUMERIC = ['height_m', 'storeys', 'floor_height_m', 'log_atemp', 'atemp_per_apartment',
           'footprint_compactness', 'footprint_vertices', 'footprint_aspect_ratio',
           'footprint_rectangularity', 'gross_to_atemp_ratio',
           'gross_above_ground_to_atemp', 'wall_area_per_atemp', 'ground_area_per_atemp',
           'volume_per_atemp', 'air_exchange_w_k_per_atemp', 'transmission_w_k_per_atemp',
           'neighbours', 'measurement_year', 'construction_year', 'designed_flow_l_s_m2']


def associations(df, permutations=9999):
    properties = df[df.accepted].groupby('property_key')[NUMERIC + ['residual', 'ape']].median()
    rng = np.random.default_rng(SEED)
    result = []
    for field in NUMERIC:
        for outcome in ['residual', 'ape']:
            data = properties[[field, outcome]].dropna()
            if len(data) < 10 or data[field].nunique() < 3:
                continue
            x, y = data[field].rank().to_numpy(), data[outcome].rank().to_numpy()
            x, y = x - x.mean(), y - y.mean()
            norm = np.linalg.norm(x) * np.linalg.norm(y)
            rho = x @ y / norm
            simulated = np.array([x @ rng.permutation(y) / norm for _ in range(permutations)])
            p = (1 + (abs(simulated) >= abs(rho)).sum()) / (1 + permutations)
            result.append({'field': field, 'outcome': outcome, 'properties': len(data),
                           'rho': rho, 'permutation_p_two_sided': p})
    for row, q in zip(result, bh([r['permutation_p_two_sided'] for r in result]), strict=True):
        row['fdr_q'] = q
    return sorted(result, key=lambda r: r['permutation_p_two_sided'])


def residual_cv(df):
    """Fixed regularisation, property-held-out exploratory residual prediction.

    Outcome-derived fields (observed energy, class, residual membership) never
    enter these predictors. Each property's training influence is equalised.
    """
    good = df[df.accepted].copy()
    numeric = ['log_atemp', 'height_m', 'floor_height_m', 'gross_to_atemp_ratio',
               'wall_area_per_atemp', 'ground_area_per_atemp', 'footprint_compactness',
               'basement_indicator', 'neighbours', 'measurement_year']
    categorical = ['roof_form', 'building_position']
    result, predictions = {}, {}
    folds = list(GroupKFold(5).split(good, groups=good.property_key))
    for label in ['intercept', 'physical_inputs', 'physical_inputs_plus_coordinates']:
        oof = np.zeros(len(good))
        fields = numeric + (['easting', 'northing'] if label.endswith('coordinates') else [])
        for train, test in folds:
            training = good.iloc[train]
            weights = 1 / training.property_key.map(training.property_key.value_counts()).to_numpy()
            weights *= len(weights) / weights.sum()
            if label == 'intercept':
                oof[test] = np.average(training.residual, weights=weights)
                continue
            preprocessing = ColumnTransformer([
                ('numeric', make_pipeline(SimpleImputer(strategy='median'), StandardScaler()), fields),
                ('categorical', OneHotEncoder(handle_unknown='ignore', sparse_output=False), categorical)])
            model = make_pipeline(preprocessing, Ridge(alpha=10))
            model.fit(training, training.residual, ridge__sample_weight=weights)
            oof[test] = model.predict(good.iloc[test])
        predictions[label] = oof
        property_results = pd.DataFrame({'property': good.property_key,
                                         'actual': good.residual, 'predicted': oof}).groupby('property').mean()
        result[label] = {'building_mae_residual': mean_absolute_error(good.residual, oof),
                         'building_r2_residual': r2_score(good.residual, oof),
                         'property_mae_residual': mean_absolute_error(property_results.actual, property_results.predicted),
                         'property_r2_residual': r2_score(property_results.actual, property_results.predicted)}
    df.loc[good.index, 'oof_unexplained_residual'] = good.residual - predictions['physical_inputs']
    return result


def spatial(df, permutations=9999):
    prop = df.groupby('property_key').agg(easting=('easting', 'mean'), northing=('northing', 'mean'),
        residual=('residual', 'median'), absolute_residual=('residual', lambda x: x.abs().median()),
        ape=('ape', 'median'), poor_rate=('inside20', lambda x: 1-x.mean()),
        unexplained=('oof_unexplained_residual', 'median'), buildings=('building_id', 'count'),
        area_name=('area_name', 'first'))
    prop['neighbourhood_group'] = DBSCAN(eps=750, min_samples=2).fit_predict(prop[['easting', 'northing']])
    good = prop.dropna(subset=['residual']).copy()
    distances = cdist(good[['easting', 'northing']], good[['easting', 'northing']])
    np.fill_diagonal(distances, np.inf)
    rng = np.random.default_rng(SEED)
    result = []
    for graph in ['knn4', 'knn8', 'radius500', 'radius1000', 'radius2000']:
        if graph.startswith('knn'):
            k = int(graph[3:]); weights = np.zeros_like(distances)
            np.put_along_axis(weights, distances.argsort(axis=1)[:, :k], 1, axis=1)
            weights = np.maximum(weights, weights.T)
        else:
            weights = (distances <= int(graph[6:])).astype(float)
        keep = weights.sum(axis=1) > 0
        weights = weights[np.ix_(keep, keep)]
        if len(weights) < 5:
            continue
        weights /= weights.sum(axis=1, keepdims=True)
        for outcome in ['residual', 'absolute_residual', 'ape', 'poor_rate', 'unexplained']:
            z = good.loc[keep, outcome].to_numpy(copy=True)
            z -= z.mean()
            denom = z @ z
            stat = z @ weights @ z / denom
            simulated = np.empty(permutations)
            for start in range(0, permutations, 500):
                batch = np.array([rng.permutation(z) for _ in range(min(500, permutations-start))])
                simulated[start:start+len(batch)] = np.einsum('bi,ij,bj->b', batch, weights, batch) / denom
            p = (1 + (simulated >= stat).sum()) / (1 + permutations)
            result.append({'graph': graph, 'outcome': outcome, 'properties': len(z),
                           'islands_excluded': int((~keep).sum()), 'moran_i': stat,
                           'permutation_p_positive_clustering': p,
                           'null_expected_i': -1 / (len(z)-1)})
    for row, q in zip(result, bh([r['permutation_p_positive_clustering'] for r in result]), strict=True):
        row['fdr_q'] = q
    return prop.reset_index(), result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--campaign', type=Path, required=True)
    parser.add_argument('--cohort', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--prediction-mode', choices=['baseline', 'calibrated'], default='baseline')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    df = load_data(args.campaign, args.cohort, args.output, args.prediction_mode)
    cv = residual_cv(df)
    properties, spatial_results = spatial(df)
    correlations = associations(df)
    groups = sum([group_summary(df, field) for field in ['status20', 'heated_basement',
                 'roof_form', 'storey_band', 'area_ratio_band', 'building_position',
                 'certificate_class', 'certificate_reference_version', 'measurement_year',
                 'completed_window_replacement', 'area_name', 'observed_quartile']], [])
    simple = df.drop(columns=[c for c in df if c in ['geometry_wkb_hex', 'context_audit',
                    'membership', 'semantic_caveats', 'unknowns', 'reported_completed_measures',
                    'locked_parameters']])
    dump(args.output / 'buildings.json', simple.to_dict('records'))
    dump(args.output / 'properties.json', properties.to_dict('records'))
    summary = {'n': len(df), 'accepted': int(df.accepted.sum()), 'properties': df.property_key.nunique(),
               'inside20': int(df.inside20.sum()), 'failed': int((~df.accepted).sum()),
               'bias': df.residual.mean(), 'mae': df.residual.abs().mean(),
               'observed_median': df.observed.median(), 'predicted_median': df.predicted.median(),
               'groups': groups, 'associations': correlations, 'residual_cv': cv,
               'spatial': spatial_results}
    dump(args.output / 'analysis.json', summary)
    print(json.dumps({k: v for k, v in summary.items() if k not in ['groups', 'associations', 'spatial']}, indent=2))
    print('STRATA', json.dumps([r for r in groups if r['field'] in ['heated_basement', 'roof_form', 'area_ratio_band', 'certificate_class']], indent=2))
    print('ASSOCIATIONS', json.dumps(correlations[:12], indent=2))
    print('SPATIAL', json.dumps(spatial_results, indent=2))


if __name__ == '__main__':
    main()
