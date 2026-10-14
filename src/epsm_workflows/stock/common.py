from __future__ import annotations
import hashlib,json
from pathlib import Path
import numpy as np
import pandas as pd
from ..campaign import atomic_json,file_sha256
SEED=20261002
NUMERIC=['construction_year','measurement_year','heating_degree_days','atemp_m2','residential_percent','office_percent','school_percent','footprint_area_m2','footprint_perimeter_m','compactness','height_m','gross_to_atemp_ratio','volume_proxy_m3','storeys','apartments','atemp_per_apartment','heated_basement_floors','garage_percent','designed_flow_l_s_m2','reno_window','reno_wall','reno_roof','reno_ventilation']
CATEGORICAL=['era','climate_region','ventilation','roof_form','building_position','age_conflict','renovation_timing']
USE_PERCENTAGES=['residential_percent','office_percent','school_percent','hotel_percent','restaurant_percent','grocery_percent','retail_percent','shopping_percent','healthcare_percent','daytime_service_percent','bath_percent','theatre_percent','other_use_percent']
NUMERIC+= [c for c in USE_PERCENTAGES if c not in NUMERIC]
CONTEXT=['pop_density_1km','income_median']
BLOCKS={'basic':(['construction_year','measurement_year','heating_degree_days']+USE_PERCENTAGES,['era','climate_region']),
 'geometry':([c for c in NUMERIC if c not in ['designed_flow_l_s_m2','reno_window','reno_wall','reno_roof','reno_ventilation']],CATEGORICAL[:2]+['roof_form','building_position','age_conflict']),
 'physical':(NUMERIC,CATEGORICAL),'context':(NUMERIC+CONTEXT,CATEGORICAL)}
PHYSICAL=['atemp_m2','footprint_area_m2','compactness','height_m','storeys','atemp_per_apartment','gross_to_atemp_ratio','heated_basement_floors','designed_flow_l_s_m2']
FORBIDDEN={'target','heating_intensity','heat_kwh','ep','energy_class','dhw_share','fastighetsel_share'}

def digest(x):return hashlib.sha256(json.dumps(x,sort_keys=True,default=str).encode()).hexdigest()
def read(path):return json.loads(Path(path).read_text())
def load_config(path):
 p=Path(path).resolve();c=read(p)
 if c.get('schema_version')!=1:raise ValueError('Unsupported stock configuration')
 for k in ['publication','epc','age_registry','height_release','archive','dictionary','recent_studies','context_population','climate_descriptors','betsi_report']:
  if c.get(k):c[k]=str((p.parent/Path(c[k])).resolve())
 c['config_sha256']=file_sha256(p);return c

def features(root):
 root=Path(root);m=read(root/'manifest.json')
 if file_sha256(root/'features.parquet')!=m['features_sha256']:raise ValueError('Prepared features changed')
 f=pd.read_parquet(root/'features.parquet')
 if set(f)&FORBIDDEN:raise ValueError('Target-derived predictor in feature table')
 return f

def development(root):
 f=features(root);f=f[f.split!='test'].copy()
 manifest=read(Path(root)/'manifest.json')
 if file_sha256(Path(root)/'targets-development.parquet')!=manifest['development_targets_sha256']:raise ValueError('Development targets changed')
 t=pd.read_parquet(Path(root)/'targets-development.parquet')
 if 'test' in set(t.split):raise ValueError('Holdout energy in development targets')
 return f.merge(t[['building_id','target']],on='building_id',validate='one_to_one')

def assign_splits(frame,prior):
 """Property-atomic, target-blind, deterministic assignment; prior data train-only."""
 strata={};result={};counts={};total={}
 ordered=frame.sort_values('building_id')
 summary=ordered.groupby('property_key',sort=True).agg(use_group=('use_group','first'),heating_family=('heating_family','first'),era=('era','first'),climate_region=('climate_region','first'),size=('building_id','size'))
 for row in summary.itertuples():
  property_key=row.Index
  key=tuple('unknown' if pd.isna(v) else str(v) for v in (row.use_group,row.heating_family,row.era,row.climate_region))
  if property_key in prior:result[property_key]='train';continue
  strata.setdefault(key,[]).append((property_key,row.size))
 for key,groups in sorted(strata.items()):
  groups.sort(key=lambda x:digest((SEED,x[0])))
  n=sum(x[1] for x in groups);counts={s:0 for s in ('train','validation','test')}
  for prop,size in groups:
   s=max(counts,key=lambda s:({'train':.6,'validation':.2,'test':.2}[s]*n-counts[s],s))
   result[prop]=s;counts[s]+=size
 return frame.property_key.map(result)

def error_metrics(y,pred):
 y=np.asarray(y,float);pred=np.asarray(pred,float)
 if len(y)==0 or np.any(y<=0) or not np.isfinite(y).all() or not np.isfinite(pred).all():raise ValueError('Invalid metric inputs')
 e=pred-y;ape=np.abs(e)/y
 return {'n':len(y),'mae':float(np.mean(np.abs(e))),'bias':float(e.mean()),'median_ape_percent':float(np.median(ape)*100),'below_20':float(np.mean(ape<.2)),'below_10':float(np.mean(ape<.1)), 'signed_error_quantiles':np.quantile(e,[.05,.25,.5,.75,.95]).tolist()}

def bootstrap(y,pred,properties,n=1000,baseline=None):
 y=np.asarray(y);pred=np.asarray(pred);props=np.asarray(properties);unique,inv=np.unique(props,return_inverse=True)
 err=pred-y;vals=[np.abs(err),err,(np.abs(err)/y<.2).astype(float),(np.abs(err)/y<.1).astype(float)]
 names=['mae','bias','below_20','below_10'];counts=np.bincount(inv)
 sums=np.array([np.bincount(inv,weights=v) for v in vals])
 if baseline is not None:
  sums=np.vstack([sums,np.bincount(inv,weights=np.abs(err)-np.abs(np.asarray(baseline)-y))]);names.append('mae_difference')
 rng=np.random.default_rng(SEED);draws=[];ape=100*np.abs(err)/y;order=np.argsort(ape);ordered_ape=ape[order];ordered_groups=inv[order]
 for _ in range(n):
  ids=rng.integers(0,len(unique),len(unique));size=int(counts[ids].sum());values=sums[:,ids].sum(axis=1)/size
  multiplicity=np.bincount(ids,minlength=len(unique));cumulative=np.cumsum(multiplicity[ordered_groups])
  middle=ordered_ape[np.searchsorted(cumulative,[(size-1)//2+1,size//2+1])].mean()
  draws.append(np.append(values,middle))
 names.append('median_ape_percent')
 ci=np.quantile(np.array(draws),[.025,.975],axis=0)
 return {'property_count':len(unique),'resamples':n,'intervals':{name:ci[:,i].tolist() for i,name in enumerate(names)}}

def stage_guard(root,stage):
 root=Path(root)
 if (root/'evaluation-opened.json').exists():raise ValueError('Test energy already opened; changes require a fresh study/holdout')
 path=root/stage
 if path.exists():raise FileExistsError(path)
 path.mkdir();return path

def freeze_files(root):
 root=Path(root)
 paths=[p for directory in ['regression','clustering','calibration'] for p in sorted((root/directory).rglob('*')) if p.is_file() and not (directory=='calibration' and any(v in p.relative_to(root/directory).parts for v in ['runs','weather','compiled']))]
 hashes={str(p.relative_to(root)):file_sha256(p) for p in paths}
 hashes['config.json']=file_sha256(root/'config.json')
 for name in ['manifest.json','field-dictionary.json','prior-study-registry.json','context-audit.json','archive-audit.json','environment.json']:
  if (root/name).is_file():hashes[name]=file_sha256(root/name)
 hashes.update({'workflow:'+str(p.resolve()):file_sha256(p) for p in Path(__file__).parent.glob('*.py')})
 return hashes
