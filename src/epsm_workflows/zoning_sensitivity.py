"""Isolate footprint cleanup and perimeter zoning on frozen diagnostic buildings."""
from __future__ import annotations
import json
import statistics
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from epsm_core import FootprintCleanupSpec, ZoningSpec
from . import archetype_diagnostics as d, archetype_pilot as p
from .wwr_sensitivity import build_spec as frozen_spec
from .campaign import atomic_json, file_sha256

ROOT = Path('output/zoning-cleanup-mf-se02-2026-10-02-v3').resolve()
SOURCE = Path('output/parameter-sensitivity-mf-se02-2026-10-01').resolve()
ENGINE = '/Applications/OpenStudio-3.9.0/EnergyPlus/energyplus'
MODES = ('original_per_storey', 'per_storey', 'perimeter_3', 'perimeter_4.5', 'perimeter_6')


def build_spec(row, state, weather, mode):
    if mode not in MODES:
        raise ValueError(mode)
    spec = frozen_spec(row, state, weather, 0.2)
    if mode == 'original_per_storey':
        geometry = spec.geometry.updated(footprint_cleanup=FootprintCleanupSpec(enabled=False))
        factor = spec.geometry.conditioned_floor_area_m2 / geometry.conditioned_floor_area_m2
        return spec.updated(geometry=geometry,
            use=spec.use.updated(**{key:getattr(spec.use,key)*factor for key in (
                'occupant_density_per_m2','equipment_gain_w_m2','lighting_gain_w_m2','dhw_kwh_m2_year')}),
            airflow=spec.airflow.updated(mechanical_vent_l_s_m2=spec.airflow.mechanical_vent_l_s_m2*factor))
    zoning = ZoningSpec() if mode == 'per_storey' else ZoningSpec(
        mode='perimeter_core', depth_m=float(mode.split('_')[1]), on_failure='error')
    updated = spec.updated(geometry=spec.geometry.updated(zoning=zoning))
    a,b = spec.model_dump(mode='json'),updated.model_dump(mode='json')
    a['geometry'].pop('zoning'); b['geometry'].pop('zoning')
    assert a == b
    return updated


def execute(job):
    row,state,weather,mode,audit_hash = job
    directory = ROOT/'runs'/d.digest((row['building_id'],state,mode,audit_hash))[:24]
    receipt = directory/'receipt.json'
    if receipt.exists(): return p._read(receipt)
    directory.mkdir(parents=True,exist_ok=True)
    result = dict(building_id=row['building_id'],property_key=row['property_key'],state=state,
                  zoning=mode,epc_atemp_m2=row['atemp_m2'],status='failed',compiled=False)
    try:
        spec = build_spec(row,state,weather,mode)
        runner = d.make_runner(ENGINE,'combined_kiva')
        model,report = runner.compiler.compile(spec)
        data = report.data
        result.update(compiled=True,spec_sha256=spec.physical_hash(),weather_sha256=spec.climate.weather_sha256,
            epjson_sha256=d.digest(model),conditioned_area_m2=data['conditioned_floor_area_m2'],
            conditioned_volume_m3=data['conditioned_volume_m3'],zone_count=len(model['Zone']),
            zoning_report=data['zoning'],foundation=data['foundation'],footprint_cleanup=data['footprint_cleanup'])
        atomic_json(directory/'compilation-report.json',data)
        run = runner.run(spec,directory/'energyplus')
        result.update(status='complete',heating_kwh=run.scalars['space_heating_demand_kwh'],
            demand_kwh_m2=run.scalars['space_heating_demand_kwh']/row['atemp_m2'],diagnostics=run.diagnostics,run_id=run.run_id)
    except (ValueError,RuntimeError,OSError) as error:
        result['error'] = f'{type(error).__name__}: {error}'
    atomic_json(receipt,result)
    return result


def summarize(rows,results):
    lookup={(x['building_id'],x['state'],x['zoning']):x for x in results}
    pairs,areas,summaries=[],[],[]
    comparisons=[('original_per_storey','per_storey')]+[('per_storey',m) for m in MODES[2:]]
    for state in p.STATES:
        for before,after in comparisons:
            group=[]
            for row in rows:
                a,b=(lookup[row['building_id'],state,m] for m in (before,after))
                item=dict(building_id=row['building_id'],property_key=row['property_key'],state=state,before=before,after=after,
                          epc_atemp_m2=row['atemp_m2'],observed_kwh_m2=row['heat_kwh']/row['atemp_m2'])
                if a['compiled'] and b['compiled']:
                    item.update(before_area_m2=a['conditioned_area_m2'],after_area_m2=b['conditioned_area_m2'],
                        area_change_m2=b['conditioned_area_m2']-a['conditioned_area_m2'],
                        volume_change_m3=b['conditioned_volume_m3']-a['conditioned_volume_m3'],
                        area_ratio=b['conditioned_area_m2']/row['atemp_m2'],before_zones=a['zone_count'],after_zones=b['zone_count'])
                    areas.append(item.copy())
                if a['status']!='complete' or b['status']!='complete':continue
                item.update(before_demand_kwh_m2=a['demand_kwh_m2'],after_demand_kwh_m2=b['demand_kwh_m2'],
                            heating_change_kwh_m2=b['demand_kwh_m2']-a['demand_kwh_m2'])
                item['loss_sensitivities']={}
                for loss in (0,5,10):
                    ea,eb=(x['demand_kwh_m2']+loss-item['observed_kwh_m2'] for x in (a,b))
                    item['loss_sensitivities'][str(loss)]=dict(before_signed_error=ea,after_signed_error=eb,
                        before_ape_percent=100*abs(ea)/item['observed_kwh_m2'],after_ape_percent=100*abs(eb)/item['observed_kwh_m2'])
                pairs.append(item);group.append(item)
            for loss in (0,5,10):
                if not group:continue
                values=[x['loss_sensitivities'][str(loss)] for x in group]
                summaries.append(dict(state=state,before=before,after=after,loss_kwh_m2=loss,n=len(group),
                    before_mae=statistics.mean(abs(x['before_signed_error']) for x in values),
                    after_mae=statistics.mean(abs(x['after_signed_error']) for x in values),
                    before_bias=statistics.mean(x['before_signed_error'] for x in values),
                    after_bias=statistics.mean(x['after_signed_error'] for x in values),
                    before_median_ape=statistics.median(x['before_ape_percent'] for x in values),
                    after_median_ape=statistics.median(x['after_ape_percent'] for x in values),
                    before_below20=sum(x['before_ape_percent']<20 for x in values),after_below20=sum(x['after_ape_percent']<20 for x in values),
                    before_below10=sum(x['before_ape_percent']<10 for x in values),after_below10=sum(x['after_ape_percent']<10 for x in values),
                    improved=sum(abs(x['after_signed_error'])<abs(x['before_signed_error'])-1e-8 for x in values),
                    worsened=sum(abs(x['after_signed_error'])>abs(x['before_signed_error'])+1e-8 for x in values),
                    heating_change_mean=statistics.mean(x['heating_change_kwh_m2'] for x in group)))
    atomic_json(ROOT/'per-building.json',pairs);atomic_json(ROOT/'area-audit.json',areas);atomic_json(ROOT/'summary.json',summaries)
    lines=['# Footprint cleanup and perimeter zoning', '',
           'Same 12 diagnostic buildings, three frozen renovation states, AMY, Kiva and WWR 0.2. No calibration or context shading. EPC Atemp remains the energy denominator. Original means cleanup disabled in the current Core; earlier reports remain preserved separately.', '',
           f"Accepted simulations: {sum(x['status']=='complete' for x in results)}/{len(results)}. Failed cases remain in results.json. No fallback or warning waiver.", '',
           '## Heating comparison', '',
           '5 kWh/m² net delivery loss below. Results at 0 and 10 are in summary.json. Each comparison uses only accepted paired buildings.', '',
           '| State | Comparison | n | MAE before → after | Below 20% | Below 10% | Improved / worsened |',
           '|---|---|---:|---:|---:|---:|---:|']
    for x in summaries:
        if x['loss_kwh_m2']!=5:continue
        lines.append(f"| {x['state']} | {x['before']} → {x['after']} | {x['n']} | {x['before_mae']:.2f} → {x['after_mae']:.2f} | {x['before_below20']} → {x['after_below20']} | {x['before_below10']} → {x['after_below10']} | {x['improved']} / {x['worsened']} |")
    lines += ['', '## Heated area', '']
    for before,after in comparisons:
        vals=[x for x in areas if x['before']==before and x['after']==after]
        if vals:lines.append(f"- {before} → {after}: largest absolute heated-area change {max(abs(x['area_change_m2']) for x in vals):.6f} m²; largest volume change {max(abs(x['volume_change_m3']) for x in vals):.6f} m³.")
    lines += ['', 'Footprint cleanup does not force area agreement with EPC. Zoning subdivides the same cleaned building. Per-building areas, EPC ratios and zone counts are in area-audit.json.', '',
              '## Failures', '', '| Cause | Cases |','|---|---:|']
    counts=Counter()
    for x in results:
        if x['status']=='complete':continue
        needles=('Window outside parent surface','Collinear surface vertex','Near-coincident consecutive','coincident/collinear vertices','Temperature out of range','Zero-length facade edge','Zone has open')
        counts[next((s for s in needles if s in x.get('error','')),x.get('error','')[:160])]+=1
    lines.extend(f'| {cause} | {n} |' for cause,n in counts.items())
    lines += ['', 'Previously inspected properties provide diagnostic evidence, not an untouched test. Numerical failures are excluded from all energy conclusions. Core remains per-storey by default.', '']
    (ROOT/'report.md').write_text('\n'.join(lines))
    print(json.dumps([x for x in summaries if x['loss_kwh_m2']==5],indent=2),flush=True)


def main():
    ROOT.mkdir(parents=True,exist_ok=True)
    rows=p._read(SOURCE/'cohort.json');weather=p._read(SOURCE/'audit.json')['source_weather']
    audit=dict(source_cohort_sha256=file_sha256(SOURCE/'cohort.json'),source_audit_sha256=file_sha256(SOURCE/'audit.json'),
        source_weather=weather,weather_hashes={str(y):file_sha256(Path(weather)/f'weather-{y}.epw') for y in {r['measurement_year'] for r in rows}},
        engine_sha256=file_sha256(ENGINE),package_sources=d.package_sources(),workflow_sha256=file_sha256(Path(__file__)),
        modes=MODES,strict_warning_gate=True,calibration=False,untouched_test=False)
    if (ROOT/'audit.json').exists() and p._read(ROOT/'audit.json')!=json.loads(json.dumps(audit)):
        raise ValueError('Inputs changed: choose a fresh output directory')
    atomic_json(ROOT/'audit.json',audit);atomic_json(ROOT/'cohort.json',rows)
    (ROOT/'workflow_source_frozen.py').write_text(Path(__file__).read_text())
    jobs=[(r,s,weather,m,d.digest(audit)) for r in rows for s in p.STATES for m in MODES]
    started=time.monotonic()
    compiled=[]
    runner=d.make_runner(ENGINE,'combined_kiva')
    for row,state,_,mode,_ in jobs:
        try:
            model,report=runner.compiler.compile(build_spec(row,state,weather,mode))
            compiled.append(dict(building_id=row['building_id'],state=state,zoning=mode,status='complete',epjson_sha256=d.digest(model)))
        except (ValueError,RuntimeError) as e:
            compiled.append(dict(building_id=row['building_id'],state=state,zoning=mode,status='failed',error=str(e)))
    atomic_json(ROOT/'precompile.json',compiled)
    print(f"Compiled {sum(x['status']=='complete' for x in compiled)}/{len(jobs)}",flush=True)
    if any(x['status']!='complete' for x in compiled):raise ValueError('Compilation gate failed')
    smoke_ids=('a35093f4','c6fecabb','3aa89264')
    smoke_jobs=[j for j in jobs if j[0]['building_id'].startswith(smoke_ids) and j[1]=='existing']
    with ProcessPoolExecutor(max_workers=6) as pool:
        smoke=list(pool.map(execute,smoke_jobs));atomic_json(ROOT/'smoke.json',smoke)
        print('Smoke:',[(x['building_id'][:8],x['zoning'],x['status'],x.get('error','')[:300]) for x in smoke],flush=True)
        if any(x['status']!='complete' for x in smoke):raise ValueError('Smoke gate failed; investigate retained diagnostics')
        results=[]
        for result in pool.map(execute,jobs):
            results.append(result)
            if len(results)%15==0:
                atomic_json(ROOT/'results.partial.json',results)
                print(f"Finished {len(results)}/{len(jobs)}; accepted {sum(x['status']=='complete' for x in results)}",flush=True)
    atomic_json(ROOT/'results.json',results);atomic_json(ROOT/'runtime.json',dict(seconds=time.monotonic()-started))
    summarize(rows,results)
    atomic_json(ROOT/'output-manifest.json',{str(f.relative_to(ROOT)):file_sha256(f) for f in ROOT.iterdir() if f.is_file() and f.name!='output-manifest.json'})

if __name__=='__main__':main()
