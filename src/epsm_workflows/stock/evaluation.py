"""Open the holdout once after every preceding stage is immutable."""
from __future__ import annotations
import pickle
from pathlib import Path
import numpy as np,pandas as pd
from .common import features,read,freeze_files,digest,error_metrics,bootstrap
from .regression import predict
from .clustering import PARENT
from ..campaign import atomic_json,file_sha256


def evaluate(root):
 root=Path(root);cfg=read(root/'config.json');manifest=read(root/'manifest.json')
 for stage in ['regression','clustering','calibration']:
  if not (root/stage/'complete.json').is_file():raise ValueError('Stage incomplete: '+stage)
 frozen=freeze_files(root);marker=root/'evaluation-opened.json'
 if marker.exists():
  if read(marker)['frozen_sha256']!=digest(frozen):raise ValueError('Frozen choices changed after opening holdout')
  if (root/'evaluation/complete.json').exists():return read(root/'evaluation/metrics.json')
 else:atomic_json(marker,{'frozen_sha256':digest(frozen),'frozen_files':frozen,'test_energy_opened':True})
 out=root/'evaluation';out.mkdir(exist_ok=True)
 if file_sha256(root/'targets-test-sealed.parquet')!=manifest['test_targets_sha256']:raise ValueError('Sealed test target hash mismatch')
 frame=features(root);test=frame[frame.split=='test'].merge(pd.read_parquet(root/'targets-test-sealed.parquet')[['building_id','target']],on='building_id',validate='one_to_one')
 with (root/'regression/models.pkl').open('rb') as f:models=pickle.load(f)
 with (root/'clustering/models.pkl').open('rb') as f:clusters=pickle.load(f)
 records=[];outputs=[];unsupported=[]
 for key,g in test.groupby(['use_group','heating_family','correction_status'],sort=True):
  name='__'.join(key)
  if key not in models:unsupported.append({'group':name,'buildings':len(g),'reason':'No supported development model'});continue
  mm=models[key].copy();predictions={label:predict(bundle,g) for label,bundle in mm.items()}
  if 'physical' in mm:
   pred=predictions['physical'].copy()
   for parent,part in g.groupby(PARENT,sort=True):
    bundle=clusters.get(parent)
    if not bundle or bundle['k']==1 or not bundle['regressions']:continue
    labels=bundle['kmeans'].predict(bundle['prep'].transform(part[bundle['columns']]))
    local=np.zeros(len(part))
    for label,b in bundle['regressions'].items():
     ix=np.flatnonzero(labels==label)
     if len(ix):local[ix]=predict(b,part.iloc[ix])
    indexes=g.index.get_indexer(part.index);pred[indexes]=local
   predictions['physical_with_optional_clusters']=pred
  for model,pred in predictions.items():
   records.append({'group':name,'model':model,**error_metrics(g.target,pred),'bootstrap':bootstrap(g.target,pred,g.property_key,cfg['bootstrap_resamples'])})
   outputs.extend({'building_id':bid,'property_key':prop,'group':name,'model':model,'target':float(y),'prediction':float(pr),'signed_error':float(pr-y),'absolute_percentage_error':100*abs(float(pr-y))/float(y)} for bid,prop,y,pr in zip(g.building_id,g.property_key,g.target,pred))
 prediction_frame=pd.DataFrame(outputs);prediction_frame.to_parquet(out/'per-building.parquet',index=False)
 aggregate=[]
 for model,g in prediction_frame.groupby('model',sort=True):
  base=prediction_frame[prediction_frame.model=='baseline'][['building_id','prediction']].rename(columns={'prediction':'baseline'})
  paired=g.merge(base,on='building_id',validate='one_to_one')
  aggregate.append({'model':model,**error_metrics(g.target,g.prediction),'bootstrap':bootstrap(paired.target,paired.prediction,paired.property_key,cfg['bootstrap_resamples'],baseline=paired.baseline)})
 atomic_json(out/'aggregate.json',aggregate)
 # Subgroups are descriptive; no models are retuned after this point.
 subgroup=[]
 if len(prediction_frame):
  e=prediction_frame.merge(test[['building_id','era','climate_region','roof_form','heated_basement_floors','atemp_m2','height_m','measurement_year']],on='building_id',validate='many_to_one')
  e['atemp_band']=pd.cut(e.atemp_m2,[0,500,2000,10000,np.inf]).astype(str)
  e['height_band']=pd.cut(e.height_m,[0,6,12,24,np.inf]).astype(str)
  for column in ['era','climate_region','roof_form','heated_basement_floors','atemp_band','height_band','measurement_year']:
   for key,g in e.groupby(['group','model',column],dropna=False):subgroup.append({'group':key[0],'model':key[1],'attribute':column,'value':str(key[2]),'properties':g.property_key.nunique(),**error_metrics(g.target,g.prediction)})
 atomic_json(out/'subgroups.json',subgroup)
 cal=read(root/'calibration/complete.json');physical=None
 if cal['status']=='frozen':
  from .calibration import simulate_matrix
  frozen_cal=read(root/'calibration/frozen.json');evidence=read(root/'calibration/evidence.json');cohort=pd.read_parquet(root/'calibration/cohort.parquet');rows=cohort[cohort.split=='test']
  baseline,baseline_runs=simulate_matrix(root,rows,evidence,{'wall':1.,'roof':1.,'flow':1.})
  final,final_runs=simulate_matrix(root,rows,evidence,frozen_cal['settings'])
  paired=np.isfinite(baseline).all(axis=1)&np.isfinite(final).all(axis=1)
  data=rows[paired].merge(test[['building_id','target']],on='building_id',validate='one_to_one')
  frozen_pred=baseline[paired].mean(axis=1);final_pred=final[paired]@np.asarray(frozen_cal['weights']);phys=[];individual=[]
  if len(data):
   for loss in [0,5,10]:
    bp=frozen_pred+(loss-5);fp=final_pred+(loss-5);bm=error_metrics(data.target,bp);fm=error_metrics(data.target,fp);ci=bootstrap(data.target,fp,data.property_key,cfg['bootstrap_resamples'],baseline=bp)
    improved=fm['mae']<bm['mae'] and fm['below_20']>bm['below_20'] and ci['intervals']['mae_difference'][1]<0
    phys.append({'loss_kwh_m2':loss,'baseline':bm,'final':fm,'paired_bootstrap':ci,'improvement_supported':improved})
    individual.extend({'building_id':bid,'loss_kwh_m2':loss,'target':float(y),'frozen_baseline':float(b),'final':float(f)} for bid,y,b,f in zip(data.building_id,data.target,bp,fp))
  state_metrics=[{'state':state,**error_metrics(data.target,baseline[paired,i]),'bootstrap':bootstrap(data.target,baseline[paired,i],data.property_key,cfg['bootstrap_resamples'])} for i,state in enumerate(['existing','standard','advanced'])] if len(data) else []
  physical={'attempted_test_buildings':len(rows),'paired_buildings':int(paired.sum()),'failed_buildings':int((~paired).sum()),'metrics':phys,'frozen_state_metrics_at_5_kwh_m2':state_metrics,'within_20_fraction_all_attempted':float(np.sum(np.abs(final_pred-data.target.to_numpy())/data.target.to_numpy()<.2)/len(rows)) if len(rows) else 0}
  atomic_json(out/'physics-per-building.json',individual)
  atomic_json(out/'physics-runs.json',baseline_runs+final_runs)
  # Test runs are immutable evidence, not new parameter choices. Retain an updated
  # audit of their paths without rewriting the pre-test choice hashes.
 atomic_json(out/'physics.json',physical or cal)
 atomic_json(out/'metrics.json',records);atomic_json(out/'unsupported-groups.json',unsupported)
 atomic_json(out/'complete.json',{'evaluated_buildings':test.building_id.nunique(),'modelled_buildings':prediction_frame.building_id.nunique() if len(prediction_frame) else 0,'physics_status':cal['status'],'holdout_opened_once':True})
 return records


def report(root):
 root=Path(root);out=root/'report';out.mkdir(exist_ok=True)
 coverage=read(root/'coverage.json');cal=read(root/'calibration/complete.json');clustering=read(root/'clustering/complete.json');metrics=read(root/'evaluation/metrics.json');validation=read(root/'regression/metrics.json');evaluation=read(root/'evaluation/complete.json')
 headlines=['## In plain words','']
 family='multi_family__district_heat__normal_use_adjusted_pre_weather_correction'
 for label,text in [('baseline','Using only typical heating for the era and region'),('physical','Using the recorded building attributes'),('tree','Using the fixed tree benchmark')]:
  found=next((m for m in metrics if m['group']==family and m['model']==label),None)
  if found:headlines.append(f"- {text}: **{100*found['below_20']:.1f}%** of unseen multifamily district-heated buildings were below 20% error, and **{100*found['below_10']:.1f}%** below 10%. MAE was {found['mae']:.1f} kWh/m² over {found['n']:,} buildings.")
 headlines+=['- These are statistical predictions. Physical archetype fitting remains blocked by insufficient independent simulation-ready properties and unresolved building evidence.' if cal['status']=='blocked' else '- Physical archetype results are reported separately below.',f"- Only {clustering['subdivided']} physical parent groups earned additional clusters on development data; subdivision is optional, not a requirement.",'- The largest missing inputs are dated completed renovations, actual measurement-year systems, heated extent and audited local climate. Design airflow is not measured infiltration.','']
 lines=['# Swedish building-stock exploration','',
 f"We prepared **{coverage['eligible_buildings']:,} buildings** with eligible measured EPC space-heating targets, spanning {coverage['properties']:,} properties. These are eligible newer EPCs, not a census of all Swedish buildings.",'',
 *headlines,
 '## What explains heating?', '',
 'The tables below compare unseen-property predictions. Basic uses age, use and geographic/year descriptors; geometry adds size and shape; physical adds ventilation and dated renovation evidence. Context adds retrospective neighbourhood variables and is not used to define construction archetypes. These are associations, not measured causes.','',
 '| Building use / heating family | Model | Buildings | MAE kWh/m² | Median error | Within 20% | Within 10% |','|---|---|---:|---:|---:|---:|---:|']
 for m in metrics:
  if m['model'] not in ['baseline','physical','context','tree','physical_with_optional_clusters']:continue
  group=m['group'].replace('__normal_use_adjusted_pre_weather_correction','').replace('__',' / ')
  lines.append(f"| {group} | {m['model']} | {m['n']:,} | {m['mae']:.1f} | {m['median_ape_percent']:.1f}% | {100*m['below_20']:.1f}% | {100*m['below_10']:.1f}% |")
 gains=[]
 by={}
 for m in validation:by.setdefault(m['group'],{})[m['model']]=m
 for group,models in by.items():
  for first,next_ in [('basic','geometry'),('geometry','physical'),('physical','context')]:
   if first in models and next_ in models:gains.append({'group':group,'block':next_,'validation_mae_change':models[next_]['mae']-models[first]['mae']})
 atomic_json(out/'feature-block-gains.json',gains)
 ranked=[]
 for block in ['geometry','physical','context']:
  subset=[g for g in gains if g['block']==block]
  if subset:
   weights=[by[g['group']][block]['n'] for g in subset]
   ranked.append({'block':block,'families':len(subset),'validation_buildings':sum(weights),'building_weighted_mae_change':float(np.average([g['validation_mae_change'] for g in subset],weights=weights))})
 atomic_json(out/'feature-block-summary.json',ranked)
 lines+=['','Feature-block validation gains are saved in feature-block-gains.json; additive effect plots and municipality-held-out development checks are in regression/. Test results have not been used to change models.','',
 '## What information is missing?','',
 '| Attribute | Missing |','|---|---:|']
 for name in ['construction_year','height_m','apartments','designed_flow_l_s_m2','reno_window','reno_wall','reno_roof','heated_basement_floors','income_median','pop_density_1km']:
  lines.append(f"| {name} | {100*coverage['missing_fraction'].get(name,1):.1f}% |")
 lines+=['','County/year descriptors currently provide a geographic/weather proxy; audited measurement-year climate descriptors are not available nationally. Income joins are limited to archived locations. The 2024/2025 context data are retrospective for older EPC periods. Blank renovations remain unknown; current EPC ventilation has not been established as the system operating in every measurement year.','',
 '## Were additional groups useful?','',f"{clustering['physical_groups']:,} physical parent groups were prepared; **{clustering['subdivided']:,}** passed the validation improvement, minimum-property and stability gates. Physical groups exclude income, population and heating outcomes. Pooled groups are retained elsewhere. See clustering/selection.json for every accepted or rejected subdivision, and representatives.parquet for central and contrasting buildings.",'',
 '## Are the archetypes calibrated and validated?','']
 if cal['status']=='blocked':
  lines+=['**Calibration stopped at its evidence/sample gates.** No calibrated-archetype accuracy claim is supported by this run.']
  lines.extend('- '+r for r in cal.get('reasons',[cal.get('reason','Unspecified gate')]))
  ranking=read(root/'calibration/candidate-ranking.json') if (root/'calibration/candidate-ranking.json').exists() else []
  if ranking:
   largest=max(ranking,key=lambda x:x['buildings']);counts=largest['property_counts']
   lines+=['',f"The largest screened candidate group has {largest['buildings']} buildings on {largest['properties']} properties, but only {counts.get('test',0)} untouched test and {counts.get('validation',0)} validation properties. The protocol requires 20 and 10 respectively. More buildings on the same property cannot replace independent properties."]
 else:
  physics=read(root/'evaluation/physics.json');lines+=['Frozen and final physical predictions, delivery-loss sensitivities and paired confidence intervals are in evaluation/physics.json. An improvement claim requires lower test MAE, better 20% coverage and a paired interval supporting lower MAE.','',json_pretty(physics)]
 lines+=['','## Coverage and reliability','',f"Statistical evaluation opened test energy once for {evaluation['evaluated_buildings']:,} buildings; supported models cover {evaluation['modelled_buildings']:,}. Other heating/use families remain explicit unsupported groups, not silently discarded. Every reported interval uses 1,000 property-group resamples. Errors and coverage are building-level; repeated buildings on one property are not independent evidence.",'',
 'No targets were statistically trimmed. Test properties were excluded from preparation fitting, feature selection, clustering, representative selection and calibration. Archived and recent study properties were forced into training. Multi-building certificates were not broadcast to footprints. BETSI-labelled archive templates were not accepted as measured materials for these buildings. Verified BETSI survey means (0.40 h⁻¹ for single-family and 0.52 h⁻¹ for multifamily) describe total air exchange, not infiltration; they are retained as population plausibility context in archive-audit.json. Archived façade estimates require dated source images and individual review before use.','',
 '## Saved evidence','',
 'coverage.json, exclusions.json, field-dictionary.json and assignment evidence in features.parquet; source manifests and prior-study registry; separate development and sealed test targets; regression models, effect plots and development diagnostics; physical cluster definitions and representatives; calibration gates and optional simulations; per-building test outputs, subgroup metrics and bootstrap intervals.','',
 'The stock predictions establish empirical achievable coverage. They do not establish measured materials, actual renovation histories, or a universal 20%/10% guarantee. Further model changes after these test results require a fresh holdout.','']
 (out/'report.md').write_text('\n'.join(lines))
 # National missing-information maps use current footprint positions, aggregated
 # into 10 km cells; they do not expose holdout energy.
 import matplotlib
 matplotlib.use('Agg')
 import matplotlib.pyplot as plt
 frame=features(root);loc=frame.dropna(subset=['centroid_e','centroid_n']).copy()
 loc['cell_e']=(loc.centroid_e//10000)*10000;loc['cell_n']=(loc.centroid_n//10000)*10000
 for field in ['height_m','designed_flow_l_s_m2','reno_window']:
  loc['missing']=loc[field].isna().astype(float);grid=loc.groupby(['cell_e','cell_n']).missing.mean().reset_index()
  fig,ax=plt.subplots(figsize=(5,8));plot=ax.scatter(grid.cell_e,grid.cell_n,c=grid.missing,s=8,cmap='viridis',vmin=0,vmax=1);ax.set_aspect('equal');ax.set(title=f'Missing {field}',xlabel='SWEREF99 easting',ylabel='SWEREF99 northing');fig.colorbar(plot,ax=ax,label='Fraction missing');fig.tight_layout();fig.savefig(out/f'missing-{field}.png',dpi=160);plt.close(fig)
 return out/'report.md'


def json_pretty(value):
 import json
 return '```json\n'+json.dumps(value,indent=2)+'\n```'
