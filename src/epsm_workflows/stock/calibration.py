"""Evidence/sample-gated physics calibration with a shared frozen predictive mixture."""
from __future__ import annotations
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json,pickle
import numpy as np,pandas as pd
from scipy.optimize import minimize
from .common import stage_guard,features,development,error_metrics,read,digest
from ..campaign import atomic_json,file_sha256
from .. import archetype_pilot as pilot,clean_cohort_test as baseline
STATES=('existing','standard','advanced')
BOUNDS={'wall':[.8,.9,1.,1.1,1.2],'roof':[.8,.9,1.,1.1,1.2],'flow':[.8,1.,1.2]}
REQUIRED_EVIDENCE=('energy_boundary','heated_extent','measurement_year_systems','exposed_walls','foundation','attic','operating_assumptions')


def fit_state_weights(predictions,targets):
 result=minimize(lambda w:np.mean(np.abs(np.asarray(predictions)@w-np.asarray(targets))),np.full(3,1/3),method='SLSQP',bounds=[(0,1)]*3,constraints=[{'type':'eq','fun':lambda w:w.sum()-1}],options={'ftol':1e-9,'maxiter':1000})
 if not result.success:raise ValueError('Predictive mixture optimization failed: '+result.message)
 w=np.maximum(0,result.x);return w/w.sum()


def counts_pass(frame,cfg):
 counts=frame.groupby('split').property_key.nunique().to_dict()
 return len(frame)>=cfg['physics_min_buildings'] and all(counts.get(k,0)>=v for k,v in cfg['physics_min_properties'].items()),counts


def bounded_spec(spec,settings):
 if set(settings)!=set(BOUNDS) or any(settings[key] not in BOUNDS[key] for key in BOUNDS):raise ValueError('Settings outside declared calibration bounds')
 original=spec
 envelope=spec.envelope.updated(wall=pilot._insulation_scaled(spec.envelope.wall,settings['wall']),top=pilot._insulation_scaled(spec.envelope.top,settings['roof']))
 airflow=spec.airflow.updated(mechanical_vent_l_s_m2=spec.airflow.mechanical_vent_l_s_m2*settings['flow'])
 spec=spec.updated(envelope=envelope,airflow=airflow)
 if spec.airflow.envelope_infiltration_ach!=original.airflow.envelope_infiltration_ach:raise AssertionError('Infiltration changed')
 for role in ['wall','top']:
  if [layer.material for layer in getattr(original.envelope,role).layers]!=[layer.material for layer in getattr(spec.envelope,role).layers]:raise AssertionError('Material identities changed')
 if spec.facade!=original.facade or spec.envelope.ground!=original.envelope.ground:raise AssertionError('Uncalibrated components changed')
 return spec


def build_spec(root,row,state,evidence,settings):
 from ..archetype_membership import assess_membership
 if not assess_membership(row)['eligible']:raise ValueError('Archetype membership conflict: require a source-consistent pure cohort')
 from epsm_core import AirflowSpec
 from epsm_data import ArchetypeKey,load_parametric_catalogue
 weatherkey=f"{row['municipality']}:{int(row['measurement_year'])}";weather=evidence['weather'][weatherkey]
 path=Path(weather['path'])
 if not path.is_file() or file_sha256(path)!=weather['sha256']:raise ValueError('Unpinned measurement-year weather')
 from epsm_core.weather import validate_epw_calendar
 validate_epw_calendar(path,int(row['measurement_year']))
 # Public builder expects a year-named file; per-building folders prevent accidental
 # sharing of a different municipality's weather for the same measurement year.
 directory=Path(root)/'calibration/weather'/row['building_id'];directory.mkdir(parents=True,exist_ok=True)
 target=directory/f"weather-{int(row['measurement_year'])}.epw"
 if not target.exists():target.symlink_to(path.resolve())
 x=dict(row);x['storeys']=int(x['storeys']);x['heated_basement_floors']=int(x['heated_basement_floors'])
 recovery=evidence.get('ftx_sensible_recovery',.7) if row['ventilation']=='FTX' else 0
 spec=baseline.build_spec(x,state,directory,archetype_id=row['archetype_id'],ventilation_system=row['ventilation'],recovery_effectiveness=recovery)
 zone=evidence['catalogue_climate_zone']
 era={'mf_1940':'SE.01','mf_1950':'SE.01',**{f'mf_se0{i}':f'SE.0{i}' for i in range(2,7)}}[row['archetype_id']]
 inf=load_parametric_catalogue().get_slot(ArchetypeKey('multi_family',era,zone,state,row['ventilation'])).infiltration.value
 spec=spec.updated(airflow=spec.airflow.updated(envelope_infiltration_ach=inf))
 # Independently evidenced component states override the whole-state candidate.
 from epsm_data.release import core_inputs
 for role,component_state in evidence.get('component_states',{}).get(row['building_id'],{}).items():
  source=core_inputs(row['archetype_id'],component_state)
  if role=='window':spec=spec.updated(facade=spec.facade.updated(glazing=source['glazing']))
  elif role in ('wall','roof'):spec=spec.updated(envelope=spec.envelope.updated(**{'wall' if role=='wall' else 'top':getattr(source['envelope'],'wall' if role=='wall' else 'top')}))
  else:raise ValueError('Unsupported component evidence: '+role)
 return bounded_spec(spec,settings)


def simulate_matrix(root,rows,evidence,settings):
 from epsm_core import EnergyPlusRunner
 cfg=read(Path(root)/'config.json');directory=Path(root)/'calibration';records=[]
 def run(item):
  row,state=item;key=digest((row['building_id'],state,settings,evidence))[:24];path=directory/'runs'/key
  receipt=path/'receipt.json'
  if receipt.exists():return read(receipt)
  path.mkdir(parents=True)
  try:
   spec=build_spec(root,row,state,evidence,settings)
   result=EnergyPlusRunner(energyplus=cfg['physics_energyplus']).run(spec,path/'energyplus')
   record=dict(building_id=row['building_id'],state=state,settings=settings,status='accepted',prediction=float(result.scalars['space_heating_demand_kwh'])/row['atemp_m2']+5,spec_sha256=digest(spec.model_dump(mode='json')),diagnostics=result.diagnostics)
  except Exception as exc:record=dict(building_id=row['building_id'],state=state,settings=settings,status='failed',reason=str(exc))
  atomic_json(receipt,record);return record
 with ThreadPoolExecutor(max_workers=3) as pool:records=list(pool.map(run,[(row,state) for row in rows.to_dict('records') for state in STATES]))
 by={(r['building_id'],r['state']):r for r in records}
 matrix=np.array([[by[row['building_id'],s].get('prediction',np.nan) for s in STATES] for row in rows.to_dict('records')])
 return matrix,records


def calibrate(root,evidence_path=None):
 root=Path(root);out=stage_guard(root,'calibration');cfg=read(root/'config.json');candidates=pd.read_parquet(root/'simulation-candidates.parquet');failures=[]
 from ..archetype_membership import assess_membership
 if not candidates.empty:
  membership=[assess_membership(row) for row in candidates.to_dict('records')]
  atomic_json(out/'membership-quarantine.json',[{'building_id':row['building_id'],**check} for row,check in zip(candidates.to_dict('records'),membership,strict=True) if not check['eligible']])
  candidates=candidates[[check['eligible'] for check in membership]]
 if candidates.empty:
  atomic_json(out/'complete.json',{'status':'blocked','reason':'No simulation candidates','test_energy_read':False});return
 assign=pd.read_parquet(root/'clustering/assignments.parquet')
 candidates=candidates.merge(assign,on='building_id',how='left',validate='one_to_one');ranking=[]
 for key,g in candidates.groupby(['parent','cluster'],dropna=True):
  eligible,counts=counts_pass(g,cfg);coverage=float(g[['designed_flow_l_s_m2','height_m','storeys']].notna().mean().mean())
  ranking.append({'parent':key[0],'cluster':int(key[1]),'buildings':len(g),'property_counts':counts,'sample_gate_before_compilation':eligible,'evidence_completeness':coverage,'properties':g.property_key.nunique()})
 ranking.sort(key=lambda x:(-x['evidence_completeness'],-x['properties'],x['parent'],x['cluster']));atomic_json(out/'candidate-ranking.json',ranking)
 possible=[r for r in ranking if r['sample_gate_before_compilation']]
 evidence=read(evidence_path) if evidence_path else None
 blockers=[]
 if not possible:blockers.append('No group meets 120-building and independent-property gates before compilation')
 if evidence is None:blockers.append('Measurement-year system, heated extent, exposure, foundation, operating and energy-boundary evidence not supplied')
 else:
  blockers.extend('Missing reviewed evidence: '+k for k in REQUIRED_EVIDENCE if not evidence.get(k))
  if not evidence.get('weather') or not evidence.get('catalogue_climate_zone'):blockers.append('Audited local AMY weather/climate mapping missing')
 if blockers:
  atomic_json(out/'complete.json',{'status':'blocked','reasons':blockers,'test_energy_read':False,'catalogue_changed':False});return
 atomic_json(out/'evidence.json',evidence)
 choice=possible[0];g=candidates[(candidates.parent==choice['parent'])&(candidates.cluster==choice['cluster'])]
 # Property groups stay atomic. Cap using deterministic feature-only ordering.
 props=sorted(g.property_key.unique(),key=lambda key:digest(('physics',key)));selected=[]
 for prop in props:
  part=g[g.property_key==prop]
  if len(selected)+len(part)<=cfg['physics_max_buildings']:selected.extend(part.to_dict('records'))
 g=pd.DataFrame(selected);good,counts=counts_pass(g,cfg)
 if not good:
  atomic_json(out/'complete.json',{'status':'blocked','reason':'Property-atomic cap fails independent-property gates','counts':counts});return
 # Compile each state and smoke each roof/ventilation combination before larger runs.
 from epsm_core import EnergyPlusRunner
 runner=EnergyPlusRunner(energyplus=cfg['physics_energyplus']);valid=[];defaults={'wall':1.,'roof':1.,'flow':1.}
 for row in g.to_dict('records'):
  try:
   for state in STATES:
    spec=build_spec(root,row,state,evidence,defaults);model,report=runner.compiler.compile(spec)
    folder=out/'compiled'/row['building_id']/state;folder.mkdir(parents=True);atomic_json(folder/'spec.json',spec.model_dump(mode='json'));atomic_json(folder/'report.json',report.data)
   valid.append(row)
  except Exception as exc:failures.append({'building_id':row['building_id'],'stage':'compile','reason':str(exc)})
 g=pd.DataFrame(valid);atomic_json(out/'failed-cases.json',failures)
 good,counts=counts_pass(g,cfg) if not g.empty else (False,{})
 if not good:atomic_json(out/'complete.json',{'status':'blocked','reason':'Accepted compilation subset fails sample gates','counts':counts});return
 dev=g[g.split!='test'];smoke=dev.drop_duplicates(['roof_form','ventilation'])
 matrix,receipts=simulate_matrix(root,smoke,evidence,defaults);atomic_json(out/'smoke.json',receipts)
 if not np.isfinite(matrix).all():atomic_json(out/'complete.json',{'status':'blocked','reason':'Annual smoke failures','test_energy_read':False});return
 values=development(root).set_index('building_id').target;train=dev.split=='train';val=dev.split=='validation'
 original,receipts=simulate_matrix(root,dev,evidence,defaults)
 accepted=np.isfinite(original).all(axis=1)
 if not accepted.all():
  dev=dev[accepted];original=original[accepted];train=dev.split=='train';val=dev.split=='validation'
 if dev[train].property_key.nunique()<30 or dev[val].property_key.nunique()<10:atomic_json(out/'complete.json',{'status':'blocked','reason':'Baseline simulation failures leave too few development properties'});return
 target=values.loc[dev.building_id].to_numpy();weights=fit_state_weights(original[train],target[train]);settings=defaults.copy();previous=float(np.mean(np.abs(original[val]@weights-target[val])));stages=[]
 for parameter,grid in BOUNDS.items():
  best=settings.copy();best_score=previous
  for value in grid:
   proposed=settings|{parameter:value}
   if proposed==settings:continue
   matrix,receipts=simulate_matrix(root,dev,evidence,proposed)
   if not np.isfinite(matrix).all():stages.append({'parameter':parameter,'value':value,'rejected':'simulation_failure'});continue
   score=float(np.mean(np.abs(matrix[val]@weights-target[val])));stages.append({'parameter':parameter,'value':value,'validation_mae':score})
   if score<best_score-1e-9:best=proposed;best_score=score
  settings=best;previous=best_score
 g.to_parquet(out/'cohort.parquet',index=False)
 atomic_json(out/'frozen.json',{'parent':choice['parent'],'cluster':choice['cluster'],'weights':weights.tolist(),'settings':settings,'stages':stages,'validation_mae':previous,'state_weights_are_predictive_not_renovation_prevalence':True})
 atomic_json(out/'complete.json',{'status':'frozen','test_energy_read':False,'catalogue_changed':False})
