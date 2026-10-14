"""Frozen-state diagnostic baseline for the independently selected mf_se05 cohort."""
from __future__ import annotations

import json
import math
import shutil
import statistics
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

from . import archetype_pilot as pilot, archetype_diagnostics as diagnostics
from .campaign import atomic_json, file_sha256

ROOT = Path('output/clean-cohort-mf-se05-baseline-2026-10-02-v2').resolve()
COHORT = Path('output/clean-cohort-gothenburg-mf-2026-10-02-v3/cohort.json').resolve()
OLD_WEATHER = Path('output/archetype-pilot-mf-se02-f-2026-09-30/weather').resolve()
ENGINE = '/Applications/OpenStudio-3.9.0/EnergyPlus/energyplus'
STATES = ('existing', 'standard', 'advanced')
ASSUMPTIONS = {
    'archetype': 'mf_se05', 'region': 'SE.Zone3', 'ventilation': 'F',
    'wwr': 0.2, 'unknown_designed_flow_l_s_m2_atemp': 0.35,
    'attic': 'unheated sealed cold attic; gable pitch 30 degrees assumed',
    'foundation': 'Core Kiva default; no basement when none reported heated; unheated basement unknown',
    'normal_use': 'bundled Sveby residential; total gains and ventilation normalized to EPC Atemp',
    'weather': 'measurement-year AMY; regenerated with current smhi2epw from pinned raw cache and original configuration',
    'target': 'reported normal-use-adjusted, pre-weather-correction district-heating space heat / EPC Atemp',
    'prediction': 'Ideal Loads Supply Air Total Heating Energy / EPC Atemp + fixed net delivery loss',
    'delivery_efficiency': 1.0, 'net_loss_kwh_m2': [0, 5, 10],
    'calibration': False, 'context_geometry': False,
}


def build_spec(row, state, weather_dir, *, archetype_id="mf_se05", ventilation_system="F", recovery_effectiveness=0):
    from epsm_core import AirflowSpec, BuildingSpec, FacadeSpec, FoundationSpec, GeometrySpec, RoofSpec, load_sveby_spec
    from epsm_data import ArchetypeKey, load_parametric_catalogue
    from epsm_data.release import core_inputs
    from shapely import from_wkb

    if row['archetype_id'] != archetype_id or row['heated_basement_floors'] != 0:

        raise ValueError('Outside the frozen new-cohort protocol')
    inputs = core_inputs(archetype_id, state)
    era={'mf_1940':'SE.01','mf_1950':'SE.01',**{f'mf_se0{i}':f'SE.0{i}' for i in range(2,7)}}[archetype_id]
    infiltration = load_parametric_catalogue().get_slot(
        ArchetypeKey('multi_family', era, 'SE.Zone3', state, ventilation_system)).infiltration.value
    geometry = GeometrySpec.from_footprint(
        from_wkb(bytes.fromhex(row['geometry_wkb_hex'])),
        storey_elevations_m=tuple(row['floor_height_m'] * i for i in range(row['storeys'] + 1)),
        source_crs='EPSG:3006',
        roof=RoofSpec(shape='flat',topology='compact') if row['roof_form']=='flat' else RoofSpec(shape='gable', topology='cold_attic', pitch_deg=30),
    )
    geometry = geometry.updated(parts=tuple(
        part.updated(outline_kind='facade' if row['outline'] == 'Fasad' else 'roof_edge_proxy', outline_reviewed=False)
        for part in geometry.parts))
    ratio = row['atemp_m2'] / geometry.conditioned_floor_area_m2
    use = load_sveby_spec().use_spec()
    use = use.updated(**{field: getattr(use, field) * ratio for field in
                        ('occupant_density_per_m2', 'equipment_gain_w_m2', 'lighting_gain_w_m2', 'dhw_kwh_m2_year')})
    flow = row['designed_ventilation_l_s_m2']
    if flow is None:
        flow = ASSUMPTIONS['unknown_designed_flow_l_s_m2_atemp']
    return BuildingSpec(
        identifier=row['building_id'], geometry=geometry,
        envelope=inputs['envelope'].updated(foundation=FoundationSpec(), ground_temperature=None),
        facade=FacadeSpec(default_wwr=ASSUMPTIONS['wwr'], glazing=inputs['glazing']),
        airflow=AirflowSpec(topology=ventilation_system, envelope_infiltration_ach=infiltration,
                            sensible_recovery_effectiveness=recovery_effectiveness,
                            mechanical_vent_l_s_m2=flow * ratio),
        use=use, climate=pilot._climate(str(Path(weather_dir) / f"weather-{row['measurement_year']}.epw"), row['measurement_year']),
    )


def prepare_weather(rows, root=ROOT):
    from smhi2epw import EPWConfig, compile_epw
    from epsm_core.weather import validate_epw_calendar
    weather = root / 'weather'
    weather.mkdir()
    audits = {}
    for year in sorted({r['measurement_year'] for r in rows}):
        old = pilot._read(OLD_WEATHER / f'weather-{year}-provenance.json')
        source = OLD_WEATHER / 'sources' / str(year) / 'raw-cache'
        cache = weather / 'sources' / str(year) / 'raw-cache'
        shutil.copytree(source, cache)
        cfg = old['config'] | {'output_path': str(weather / f'weather-{year}.epw'), 'cache_dir': str(cache)}
        print('Preparing AMY', year, flush=True)
        result = compile_epw(EPWConfig(**cfg))
        audits[str(year)] = dict(config=cfg, result=asdict(result),
            calendar=validate_epw_calendar(result.output_path, year), sha256=file_sha256(result.output_path),
            original_provenance=old, raw_cache_hashes={str(p.relative_to(cache)):file_sha256(p) for p in cache.rglob('*') if p.is_file()})
        atomic_json(weather / f'weather-{year}-provenance.json', audits[str(year)])
    return weather, audits


def main():
    from epsm_core import EnergyPlusRunner
    if ROOT.exists():
        raise FileExistsError(ROOT)
    ROOT.mkdir(parents=True)
    rows = pilot._read(COHORT)
    weather, weather_audit = prepare_weather(rows)
    specs = {(r['building_id'], s):build_spec(r, s, weather) for r in rows for s in STATES}
    source_hashes = diagnostics.package_sources()
    atomic_json(ROOT / 'audit.json', dict(assumptions=ASSUMPTIONS, cohort_sha256=file_sha256(COHORT),
        workflow_sha256=file_sha256(__file__), package_sources=source_hashes, weather=weather_audit,
        prior_diagnostic_properties_disjoint=True, comparison_is_diagnostic=True))
    compiler = EnergyPlusRunner(energyplus=ENGINE).compiler
    compiled = []
    for (bid,state),spec in specs.items():
        directory = ROOT / 'compiled' / bid / state
        directory.mkdir(parents=True)
        model, report = compiler.compile(spec)
        atomic_json(directory / 'spec.json', spec.model_dump(mode='json'))
        atomic_json(directory / 'model.epJSON', model)
        atomic_json(directory / 'compilation-report.json', report.data)
        compiled.append(dict(building_id=bid,state=state,spec_sha256=diagnostics.digest(spec.model_dump(mode='json')),
            heated_area_m2=spec.geometry.conditioned_floor_area_m2,volume_m3=spec.geometry.conditioned_volume_m3,
            zone_count=len(model['Zone'])))
    atomic_json(ROOT / 'compilation.json', compiled)
    print('Compiled',len(compiled),'frozen models',flush=True)

    def run(item):
        bid,state = item
        directory = ROOT / 'runs' / bid / state
        directory.mkdir(parents=True)
        try:
            result = EnergyPlusRunner(energyplus=ENGINE).run(specs[item],directory / 'energyplus')
            heat = float(result.scalars['space_heating_demand_kwh'])
            if not math.isfinite(heat) or heat < 0:
                raise ValueError('Invalid heating result')
            receipt = dict(building_id=bid,state=state,status='accepted',heat_kwh=heat,
                           scalars=result.scalars,diagnostics=result.diagnostics)
        except Exception as exc:
            receipt = dict(building_id=bid,state=state,status='failed',reason=str(exc))
        atomic_json(directory / 'receipt.json',receipt)
        print(bid[:8],state,receipt['status'],flush=True)
        return receipt

    first = next(iter(specs))
    results = [run(first)]
    if results[0]['status'] != 'accepted':
        atomic_json(ROOT / 'results.json',results)
        raise RuntimeError('Smoke simulation failed; inspect retained diagnostics')
    with ThreadPoolExecutor(max_workers=3) as pool:
        results.extend(pool.map(run,[item for item in specs if item != first]))
    atomic_json(ROOT / 'results.json',results)
    by_result = {(x['building_id'],x['state']):x for x in results}
    paired = [r for r in rows if all(by_result[r['building_id'],s]['status']=='accepted' for s in STATES)]
    individual=[]; summary=[]
    for loss in ASSUMPTIONS['net_loss_kwh_m2']:
        for state in STATES:
            entries=[]
            for row in paired:
                observed=row['heat_kwh']/row['atemp_m2']
                predicted=by_result[row['building_id'],state]['heat_kwh']/row['atemp_m2']+loss
                entry=dict(building_id=row['building_id'],property_key=row['property_key'],state=state,loss_kwh_m2=loss,
                    observed_kwh_m2=observed,predicted_kwh_m2=predicted,signed_error_kwh_m2=predicted-observed,
                    absolute_error_kwh_m2=abs(predicted-observed),absolute_percentage_error=100*abs(predicted-observed)/observed)
                entries.append(entry);individual.append(entry)
            if entries:
                summary.append(dict(state=state,loss_kwh_m2=loss,n=len(entries),
                    mae_kwh_m2=statistics.mean(x['absolute_error_kwh_m2'] for x in entries),
                    bias_kwh_m2=statistics.mean(x['signed_error_kwh_m2'] for x in entries),
                    median_ape_percent=statistics.median(x['absolute_percentage_error'] for x in entries),
                    below_20_percent=sum(x['absolute_percentage_error']<20 for x in entries),
                    below_10_percent=sum(x['absolute_percentage_error']<10 for x in entries)))
    atomic_json(ROOT / 'per-building.json',individual)
    atomic_json(ROOT / 'summary.json',dict(accepted_runs=sum(x['status']=='accepted' for x in results),total_runs=len(results),
        paired_buildings=len(paired),properties=len({r['property_key'] for r in paired}),metrics=summary,
        package_sources_unchanged=source_hashes==diagnostics.package_sources()))
    print(json.dumps(pilot._read(ROOT/'summary.json'),indent=2),flush=True)

if __name__=='__main__':
    main()
