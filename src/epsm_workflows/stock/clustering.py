"""Optional physical subdivisions selected on development data only."""
from __future__ import annotations
import pickle
from pathlib import Path
from collections import defaultdict
import numpy as np,pandas as pd
from sklearn.pipeline import Pipeline,FeatureUnion
from sklearn.base import clone
from sklearn.impute import SimpleImputer,MissingIndicator
from sklearn.preprocessing import StandardScaler
from sklearn.cluster import KMeans
from sklearn.metrics import adjusted_rand_score
from .common import PHYSICAL,SEED,development,features,error_metrics,stage_guard,read
from .regression import fit_additive,predict,BLOCKS
from ..campaign import atomic_json
PARENT=['use_group','heating_family','era','climate_region','ventilation']

def cluster(root):
 root=Path(root);out=stage_guard(root,'clustering');cfg=read(root/'config.json');dev=development(root);all_features=features(root)
 bundles={};summary=[];assignments=[];representatives=[];rng=np.random.default_rng(SEED)
 full_groups={key:g for key,g in all_features.groupby(PARENT,sort=True)}
 for key,group in dev.groupby(PARENT,sort=True):
  train=group[group.split=='train'];val=group[group.split=='validation'];parent='__'.join(str(x) for x in key)
  cols=[c for c in PHYSICAL if train[c].nunique()>1 and train[c].notna().mean()>=.5]
  if not cols or len(train)==0:summary.append({'parent':parent,'status':'insufficient_physical_information','k':1});continue
  prep=FeatureUnion([('values',Pipeline([('imputer',SimpleImputer(strategy='median',keep_empty_features=True)),('scale',StandardScaler())])),('missing',MissingIndicator(features='all'))])
  z=prep.fit_transform(train[cols]);km=KMeans(n_clusters=1,n_init=20,random_state=SEED).fit(z)
  chosen={'k':1,'prep':prep,'columns':cols,'kmeans':km,'regressions':{},'confidence_distance':float(np.quantile(km.transform(z).min(axis=1),.95))}
  trprops=train.property_key.nunique();vp=val.property_key.nunique()
  trials=[]
  if trprops>=2*cfg['cluster_minimum_properties'] and vp>=cfg['minimum_validation_properties']:
   try:
    pooled=fit_additive(train,*BLOCKS['physical'],alpha=10);pooled['kind']='additive';parent_pred=predict(pooled,val);parent_metric=error_metrics(val.target,parent_pred)
    zv=prep.transform(val[cols]);candidates=[]
    for k in range(2,min(5,trprops//cfg['cluster_minimum_properties'])+1):
     fitted=KMeans(n_clusters=k,n_init=20,random_state=SEED).fit(z);labels=fitted.labels_;vl=fitted.predict(zv)
     counts=[train.iloc[np.flatnonzero(labels==i)].property_key.nunique() for i in range(k)]
     if min(counts)<cfg['cluster_minimum_properties']:trials.append({'k':k,'rejected':'minimum_independent_properties','properties':counts});continue
     regressions={};pred=np.zeros(len(val))
     for i in range(k):
      b=fit_additive(train.iloc[np.flatnonzero(labels==i)],*BLOCKS['physical'],alpha=10);b['kind']='additive';regressions[i]=b
      ix=np.flatnonzero(vl==i)
      if len(ix):pred[ix]=predict(b,val.iloc[ix])
     m=error_metrics(val.target,pred);gain=1-m['mae']/parent_metric['mae']
     if gain<.05 or m['below_20']<parent_metric['below_20']:
      trials.append({'k':k,'validation':m,'gain':gain,'rejected':'no_validation_gain'});continue
     # Resample properties; each preprocessing fit uses only resampled training.
     unique=train.property_key.unique();aris=[]
     for resample in range(cfg['cluster_resamples']):
      ids=rng.choice(unique,size=max(k,int(.8*len(unique))),replace=False);boot=train[train.property_key.isin(ids)]
      pp=clone(prep)
      zz=pp.fit_transform(boot[cols]);kb=KMeans(n_clusters=k,n_init=20,random_state=SEED+resample).fit(zz)
      # Both labels refer to the same original training buildings.
      aris.append(adjusted_rand_score(labels,kb.predict(pp.transform(train[cols]))))
     stability=float(np.median(aris));patterns=pd.factorize(train[cols].isna().astype(str).agg('|'.join,axis=1))[0]
     missing_ari=adjusted_rand_score(labels,patterns)
     trial={'k':k,'validation':m,'gain':gain,'median_ari':stability,'missingness_ari':float(missing_ari),'resample_ari':aris}
     if missing_ari>=.7:trial['rejected']='clusters_reproduce_missing_information'
     elif stability<.7:trial['rejected']='unstable'
     else:candidates.append((m['mae'],k,fitted,regressions,stability))
     trials.append(trial)
    if candidates:
     _,k,fitted,regressions,stability=min(candidates,key=lambda x:(x[0],x[1]));chosen.update(k=k,kmeans=fitted,regressions=regressions,confidence_distance=float(np.quantile(fitted.transform(z).min(axis=1),.95)),stability=stability)
    else:chosen['regressions']={0:pooled}
   except ValueError as exc:trials.append({'rejected':'unsupported_regression','reason':str(exc)})
  bundle=chosen;bundles[key]=bundle
  full=full_groups[key].copy()
  zz=prep.transform(full[cols]);labels=bundle['kmeans'].predict(zz);dist=bundle['kmeans'].transform(zz).min(axis=1)
  missing=full[cols].isna().mean(axis=1).to_numpy()
  assignments.extend({'building_id':bid,'parent':parent,'cluster':int(label),'physical_distance':float(distance),'assignment_confidence':'low' if trprops<30 or distance>bundle['confidence_distance'] or miss>.5 else 'supported'} for bid,label,distance,miss in zip(full.building_id,labels,dist,missing))
  # Representatives read only physical features and split/property identifiers.
  for label in range(bundle['k']):
   subset=full[(labels==label)&(full.split!='test')].copy()
   if subset.empty:continue
   subset['distance']=dist[(labels==label)&(full.split!='test').to_numpy()]
   subset=subset.sort_values(['distance','building_id'])
   first=subset.iloc[0];selected=[first];used={first.property_key}
   # Input-contrast companions: farthest-to-centre, bounded by the training 95% envelope.
   candidates=subset[subset.distance<=bundle['confidence_distance']].sort_values(['distance','building_id'],ascending=[False,True])
   for _,row in candidates.iterrows():
    if row.property_key in used:continue
    selected.append(row);used.add(row.property_key)
    if len(selected)==5:break
   representatives.extend({'parent':parent,'cluster':label,'building_id':row.building_id,'property_key':row.property_key,'role':'central' if i==0 else 'contrast','distance':float(row.distance)} for i,row in enumerate(selected))
  summary.append({'parent':parent,'k':bundle['k'],'training_properties':trprops,'validation_properties':vp,'trials':trials,'status':'subdivided' if bundle['k']>1 else 'pooled'})
  if len(summary)%50==0:print('Physical parent groups',len(summary),flush=True)
 assigned_ids={r['building_id'] for r in assignments}
 for row in all_features[~all_features.building_id.isin(assigned_ids)].itertuples():assignments.append({'building_id':row.building_id,'parent':None,'cluster':None,'physical_distance':None,'assignment_confidence':'unseen_parent'})
 with (out/'models.pkl').open('wb') as f:pickle.dump(bundles,f)
 pd.DataFrame(assignments).to_parquet(out/'assignments.parquet',index=False)
 pd.DataFrame(representatives).to_parquet(out/'representatives.parquet',index=False)
 atomic_json(out/'selection.json',summary);atomic_json(out/'complete.json',{'physical_groups':len(bundles),'subdivided':sum(x['k']>1 for x in summary),'holdout_energy_read':False,'income_and_population_excluded':True})
 return summary
