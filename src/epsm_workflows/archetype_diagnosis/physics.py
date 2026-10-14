"""Strict frozen-state and common-window scenarios on 12 independent properties."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import json,math,shutil
from pathlib import Path
import numpy as np
import pandas as pd
from ..campaign import atomic_json,file_sha256
from ..stock.common import read,digest,error_metrics,bootstrap
from ..clean_cohort_test import build_spec as catalogue_spec
from .regression import select_diagnostic

STATES=('existing','standard','advanced')
SCENARIOS={'frozen':{'wwr':.2,'window_u':None},'wwr_03':{'wwr':.3,'window_u':None},'window_22':{'wwr':.2,'window_u':2.2},'combined':{'wwr':.3,'window_u':2.2}}


def jobs(rows):
    return [(row,state,scenario) for row in rows for scenario in SCENARIOS for state in STATES if SCENARIOS[scenario]['window_u'] is None or state=='existing']


def build_spec(row,state,weather,scenario):
    if scenario not in SCENARIOS:raise ValueError('Unsupported scenario')
    if SCENARIOS[scenario]['window_u'] is not None and state!='existing':raise ValueError('Window sensitivity applies only to existing state')
    row=dict(row);row['storeys']=int(row['storeys']);row['heated_basement_floors']=int(row['heated_basement_floors'])
    original=catalogue_spec(row,state,weather,archetype_id='mf_se02',ventilation_system='F')
    s=SCENARIOS[scenario];glazing=original.facade.glazing
    if s['window_u'] is not None:glazing=glazing.updated(u_value_w_m2k=s['window_u'])
    spec=original.updated(facade=original.facade.updated(default_wwr=s['wwr'],glazing=glazing))
    for field in ['geometry','envelope','airflow','use','climate']:
        if getattr(spec,field)!=getattr(original,field):raise AssertionError('Isolated scenario changed '+field)
    return spec


def weather_audit(rows,cfg,out):
    from epsm_core.weather import validate_epw_calendar
    audits=[];weather=out/'weather';weather.mkdir(exist_ok=True)
    for year in sorted({int(row['measurement_year']) for row in rows}):
        source=Path(cfg['weather_directory'])/f'weather-{year}.epw';provenance=source.with_name(f'weather-{year}-provenance.json')
        if not source.is_file() or not provenance.is_file():raise ValueError('Existing pinned AMY/provenance unavailable for '+str(year))
        calendar=validate_epw_calendar(source,year);sha=file_sha256(source)
        target=weather/source.name
        if target.exists() and file_sha256(target)!=sha:raise ValueError('Weather changed during diagnostic run')
        if not target.exists():shutil.copyfile(source,target)
        saved=weather/provenance.name
        if not saved.exists():shutil.copyfile(provenance,saved)
        audits.append({'measurement_year':year,'source':str(source.resolve()),'sha256':sha,'calendar':calendar,'provenance_sha256':file_sha256(provenance),'provenance':read(provenance),'selection':'previously reviewed Gothenburg AMY, never chosen by EPC error'})
    atomic_json(out/'weather-audit.json',audits)
    return weather


def compile_case(job,cfg,out,weather):
    from epsm_core import EnergyPlusRunner
    row,state,scenario=job;folder=out/'cases'/row['building_id']/state/scenario;folder.mkdir(parents=True,exist_ok=True)
    result={'building_id':row['building_id'],'property_key':row['property_key'],'state':state,'scenario':scenario,'atemp_m2':row['atemp_m2'],'status':'compile_failed','directory':str(folder.resolve())}
    try:
        spec=build_spec(row,state,weather,scenario);model,report=EnergyPlusRunner(energyplus=cfg['energyplus']).compiler.compile(spec)
        atomic_json(folder/'spec.json',spec.model_dump(mode='json'));atomic_json(folder/'model.epJSON',model);atomic_json(folder/'compilation-report.json',report.data)
        result.update(status='compiled',spec_sha256=spec.physical_hash(),weather_sha256=spec.climate.weather_sha256,epjson_sha256=digest(model),conditioned_area_m2=report.data['conditioned_floor_area_m2'],conditioned_volume_m3=report.data['conditioned_volume_m3'],zone_count=len(model['Zone']),foundation=report.data['foundation'])
    except Exception as exc:result['reason']=str(exc)
    atomic_json(folder/'compile-receipt.json',result)
    return result


def run_case(job,compiled,cfg,out,weather):
    from epsm_core import EnergyPlusRunner
    row,state,scenario=job;folder=Path(compiled['directory']);receipt=folder/'receipt.json'
    if receipt.exists():
        result=read(receipt)
        if result.get('spec_sha256')!=compiled.get('spec_sha256') or result.get('weather_sha256')!=compiled.get('weather_sha256'):raise ValueError('Simulation receipt does not match compiled inputs')
        return result
    result=compiled.copy();result['status']='failed'
    if compiled['status']=='compiled':
        try:
            spec=build_spec(row,state,weather,scenario);run=EnergyPlusRunner(energyplus=cfg['energyplus']).run(spec,folder/'energyplus')
            heat=float(run.scalars['space_heating_demand_kwh'])
            if not math.isfinite(heat) or heat<0:raise ValueError('Invalid heating result')
            result.update(status='accepted',heat_kwh=heat,demand_kwh_m2=heat/row['atemp_m2'],diagnostics=run.diagnostics,scalars=run.scalars)
        except Exception as exc:result['reason']=str(exc)
    atomic_json(receipt,result);print(row['building_id'][:8],state,scenario,result['status'],flush=True)
    return result


def summarize(rows,results,targets,cfg,out):
    by={(r['building_id'],r['state'],r['scenario']):r for r in results};entries=[];metrics=[];paired=[]
    for r in results:
        if r['status']!='accepted':continue
        y=float(targets.loc[r['building_id']])
        for loss in (0,5,10):
            pr=r['demand_kwh_m2']+loss
            entries.append({**{k:r[k] for k in ['building_id','property_key','state','scenario','atemp_m2','conditioned_area_m2','conditioned_volume_m3','zone_count']},'loss_kwh_m2':loss,'target':y,'prediction':pr,'signed_error':pr-y,'absolute_error':abs(pr-y),'absolute_percentage_error':100*abs(pr-y)/y})
    frame=pd.DataFrame(entries)
    if len(frame):
        frame.to_parquet(out/'per-building.parquet',index=False)
        for key,g in frame.groupby(['state','scenario','loss_kwh_m2']):
            attempted=sum(r['state']==key[0] and r['scenario']==key[1] for r in results)
            metrics.append({'state':key[0],'scenario':key[1],'loss_kwh_m2':int(key[2]),**error_metrics(g.target,g.prediction),'attempted':attempted,'accepted':len(g),'below_20_all_attempted':int((g.absolute_percentage_error<20).sum())/attempted,'bootstrap':bootstrap(g.target,g.prediction,g.property_key,cfg['bootstrap_resamples'])})
        for scenario in ('window_22','wwr_03','combined'):
            for state in STATES:
                for loss in (0,5,10):
                    a=frame[(frame.scenario=='frozen')&(frame.state==state)&(frame.loss_kwh_m2==loss)]
                    b=frame[(frame.scenario==scenario)&(frame.state==state)&(frame.loss_kwh_m2==loss)]
                    p=a.merge(b,on='building_id',suffixes=('_baseline','_scenario'),validate='one_to_one')
                    if p.empty:continue
                    old=p.absolute_error_baseline.to_numpy();new=p.absolute_error_scenario.to_numpy()
                    paired.append({'scenario':scenario,'state':state,'loss_kwh_m2':loss,'paired':len(p),'improved':int((new<old-1e-9).sum()),'worsened':int((new>old+1e-9).sum()),'unchanged':int((np.abs(new-old)<=1e-9).sum()),'baseline':error_metrics(p.target_baseline,p.prediction_baseline),'scenario_metrics':error_metrics(p.target_scenario,p.prediction_scenario),'paired_bootstrap':bootstrap(p.target_scenario,p.prediction_scenario,p.property_key_scenario,cfg['bootstrap_resamples'],baseline=p.prediction_baseline),'individual':[{'building_id':r.building_id,'heating_change_kwh_m2':float(r.prediction_scenario-r.prediction_baseline),'absolute_error_change':float(r.absolute_error_scenario-r.absolute_error_baseline)} for r in p.itertuples()]})
    atomic_json(out/'metrics.json',metrics);atomic_json(out/'paired-comparisons.json',paired)
    atomic_json(out/'failed-cases.json',[r for r in results if r['status']!='accepted'])
    return frame


def physics(cfg):
    root=Path(cfg['output']);out=root/'physics';out.mkdir(exist_ok=True)
    candidates=pd.read_parquet(root/'simulation-candidates.parquet');candidates=candidates[candidates.municipality.astype(str)==cfg['physics_municipality']]
    # Diagnostic selection has no energy columns or outcome-based replacements.
    selected=select_diagnostic(candidates,cfg['diagnostic_buildings'],cfg['seed']);selected.to_parquet(out/'cohort.parquet',index=False)
    rows=json.loads(selected.to_json(orient='records'));weather=weather_audit(rows,cfg,out)
    atomic_json(out/'assumptions.json',{'states':STATES,'scenarios':SCENARIOS,'WWR_and_U_are_common_uncertainty_scenarios':True,'catalogue_changed':False,'calibration':False,'fresh_holdout':False,'geometry':'Core cleanup/per-storey; original outline and conditioned area retained','foundation':'Core Kiva default soil/footing assumptions','attic':'flat compact or sealed unheated gable; pitched slope 30 degrees assumed','basement':'reported heated count zero; unheated basement existence unresolved','ventilation':'reported design or common declared 0.35 L/s/m² EPC Atemp; actual historical operation unverified','delivery':'efficiency 1.0 plus fixed 0/5/10 kWh/m² net losses','infiltration':'frozen state-specific catalogue values; never fitted'})
    case_jobs=jobs(rows);compiled=[]
    for i,job in enumerate(case_jobs):
        compiled.append(compile_case(job,cfg,out,weather))
        if (i+1)%12==0:print('Compiled cases',i+1,'/',len(case_jobs),flush=True)
    atomic_json(out/'compilation.json',compiled)
    # Three accepted compilations spanning roof/year, then additional properties.
    smoke=[];seen=set();props=set()
    for i,(row,state,scenario) in enumerate(case_jobs):
        combo=(row['roof_form'],row['measurement_year'])
        if state=='existing' and scenario=='frozen' and compiled[i]['status']=='compiled' and combo not in seen:
            smoke.append(i);seen.add(combo);props.add(row['property_key'])
        if len(smoke)==3:break
    if len(smoke)<3:
        for i,(row,state,scenario) in enumerate(case_jobs):
            if state=='existing' and scenario=='frozen' and compiled[i]['status']=='compiled' and row['property_key'] not in props:
                smoke.append(i);props.add(row['property_key'])
            if len(smoke)==3:break
    results=[run_case(case_jobs[i],compiled[i],cfg,out,weather) for i in smoke];atomic_json(out/'smoke.json',results)
    if len(smoke)!=3 or any(r['status']!='accepted' for r in results):
        smoke_set=set(smoke)
        results.extend(c|{'status':'not_attempted_after_smoke_failure'} for i,c in enumerate(compiled) if i not in smoke_set)
        status='stopped_after_smoke_failure'
    else:
        rest=[i for i in range(len(case_jobs)) if i not in set(smoke)]
        with ThreadPoolExecutor(max_workers=cfg['simulation_workers']) as pool:
            results.extend(pool.map(lambda i:run_case(case_jobs[i],compiled[i],cfg,out,weather),rest))
        status='complete_with_failures' if any(r['status']!='accepted' for r in results) else 'complete'
    atomic_json(out/'results.json',results)
    targets=pd.read_parquet(root/'targets-development.parquet').set_index('building_id').target
    summarize(rows,results,targets,cfg,out)
    atomic_json(out/'complete.json',{'status':status,'buildings':len(rows),'properties':selected.property_key.nunique(),'planned_cases':len(case_jobs),'accepted':sum(r['status']=='accepted' for r in results),'failures_or_unattempted':sum(r['status']!='accepted' for r in results),'calibration_authorized':False})
