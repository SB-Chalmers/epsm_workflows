"""Property-separated explanatory regression; never reads sealed test targets."""
from __future__ import annotations
import json,pickle
from pathlib import Path
import numpy as np,pandas as pd
from scipy import sparse
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer,MissingIndicator
from sklearn.preprocessing import SplineTransformer,OneHotEncoder
from sklearn.pipeline import Pipeline
from sklearn.linear_model import Ridge
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.model_selection import GroupKFold
from .common import BLOCKS,FORBIDDEN,SEED,development,error_metrics,stage_guard,read
from ..campaign import atomic_json


def clean(frame,numeric,categorical):
 x=frame[numeric+categorical].copy()
 for col in numeric:x[col]=pd.to_numeric(x[col],errors='coerce').replace([np.inf,-np.inf],np.nan)
 for col in categorical:x[col]=x[col].fillna('unknown').astype(str)
 return x


def additive(train,numeric,categorical,alpha=1.):
 numeric=[c for c in numeric if pd.to_numeric(train[c],errors='coerce').nunique()>1]
 categorical=[c for c in categorical if train[c].nunique()>1]
 if set(numeric+categorical)&FORBIDDEN:raise ValueError('Forbidden predictor')
 transformers=[]
 if numeric:
  transformers.extend([('numeric',Pipeline([('impute',SimpleImputer(strategy='median')),('spline',SplineTransformer(n_knots=5,include_bias=False))]),numeric),('missing',MissingIndicator(features='all'),numeric)])
 if categorical:transformers.append(('categorical',OneHotEncoder(handle_unknown='ignore',sparse_output=True),categorical))
 if not transformers:raise ValueError('No varying predictors')
 pre=ColumnTransformer(transformers,sparse_threshold=1.)
 return dict(pre=pre,numeric=numeric,categorical=categorical,alpha=alpha)


def fit_additive(frame,numeric,categorical,alpha):
 bundle=additive(frame,numeric,categorical,alpha)
 x=clean(frame,bundle['numeric'],bundle['categorical'])
 z=bundle['pre'].fit_transform(x)
 model=Ridge(alpha=alpha,solver='lsqr');model.fit(z,frame.target.to_numpy(float))
 bundle['model']=model;return bundle


def predict(bundle,frame):
 if bundle['kind']=='baseline':
  return np.array([bundle['values'].get((str(r.era),str(r.climate_region)),bundle['fallback']) for r in frame.itertuples()])
 x=clean(frame,bundle['numeric'],bundle['categorical']);z=bundle['pre'].transform(x)
 if bundle['kind']=='tree' and sparse.issparse(z):z=z.toarray()
 return np.maximum(0,bundle['model'].predict(z))


def fit_baseline(train):
 return dict(kind='baseline',values=train.groupby(['era','climate_region']).target.median().to_dict(),fallback=float(train.target.median()))


def choose_alpha(train,numeric,categorical):
 folds=GroupKFold(n_splits=5);scores={a:[] for a in (.1,1.,10.,100.)}
 for tr,va in folds.split(train,groups=train.property_key):
  a=train.iloc[tr];b=train.iloc[va];bundle=additive(a,numeric,categorical);x=clean(a,bundle['numeric'],bundle['categorical']);xb=clean(b,bundle['numeric'],bundle['categorical'])
  z=bundle['pre'].fit_transform(x);zb=bundle['pre'].transform(xb)
  for alpha in scores:
   m=Ridge(alpha=alpha,solver='lsqr');m.fit(z,a.target.to_numpy(float));scores[alpha].append(float(np.mean(np.abs(np.maximum(0,m.predict(zb))-b.target))))
 return min(scores,key=lambda a:(np.mean(scores[a]),-a)),{str(k):v for k,v in scores.items()}


def tree(train):
 n,c=BLOCKS['physical'];b=additive(train,n,c)
 # Tree uses raw numeric values, not spline-expanded ones; missing indicators retained.
 transformers=[]
 if b['numeric']:transformers.append(('numeric',SimpleImputer(strategy='median',add_indicator=True),b['numeric']))
 if b['categorical']:transformers.append(('category',OneHotEncoder(handle_unknown='ignore',sparse_output=False),b['categorical']))
 b['pre']=ColumnTransformer(transformers,sparse_threshold=0)
 z=b['pre'].fit_transform(clean(train,b['numeric'],b['categorical']))
 m=HistGradientBoostingRegressor(max_leaf_nodes=15,max_iter=200,learning_rate=.05,min_samples_leaf=50,random_state=SEED,early_stopping=False)
 m.fit(z,train.target.to_numpy(float));b.update(model=m,kind='tree');return b


def spatial_check(train):
 # Diagnostic only: hold entire municipalities out inside training.
 if train.municipality.nunique()<5:return {'status':'insufficient_municipalities'}
 scores=[]
 for tr,va in GroupKFold(5).split(train,groups=train.municipality):
  try:
   b=fit_additive(train.iloc[tr],*BLOCKS['physical'],alpha=10);b['kind']='additive'
   scores.append(error_metrics(train.iloc[va].target,predict(b,train.iloc[va]))['mae'])
  except ValueError as exc:return {'status':'unavailable','reason':str(exc)}
 return {'status':'completed','municipality_fold_mae':scores,'mean_mae':float(np.mean(scores))}


def effect_outputs(bundle,train,path):
 import matplotlib
 matplotlib.use('Agg')
 import matplotlib.pyplot as plt
 effects=[]
 reference={c:float(pd.to_numeric(train[c],errors='coerce').median()) for c in bundle['numeric']}
 reference.update({c:str(train[c].fillna('unknown').mode().iloc[0]) for c in bundle['categorical']})
 ref=pd.DataFrame([reference]);base=predict(bundle,ref)[0]
 for col in bundle['numeric']:
  lo,hi=pd.to_numeric(train[col],errors='coerce').quantile([.05,.95])
  if not np.isfinite(lo+hi) or lo==hi:continue
  values=np.linspace(lo,hi,30);x=pd.DataFrame([reference]*30);x[col]=values;y=predict(bundle,x)-base
  effects.extend({'feature':col,'value':float(v),'relative_prediction_kwh_m2':float(e)} for v,e in zip(values,y))
  fig,ax=plt.subplots(figsize=(5,3));ax.plot(values,y);ax.set(xlabel=col,ylabel='Prediction change, kWh/m²',title='Association with other inputs held fixed');fig.tight_layout();fig.savefig(path/f'effect-{col}.png',dpi=140);plt.close(fig)
 pd.DataFrame(effects).to_csv(path/'additive-effects.csv',index=False)


def regress(root):
 root=Path(root);out=stage_guard(root,'regression');cfg=read(root/'config.json');frame=development(root)
 bundles={};records=[];failures=[]
 for key,group in frame.groupby(['use_group','heating_family','correction_status'],sort=True):
  train=group[group.split=='train'];val=group[group.split=='validation'];name='__'.join(key)
  if train.property_key.nunique()<cfg['minimum_training_properties'] or val.property_key.nunique()<cfg['minimum_validation_properties']:
   failures.append({'group':name,'reason':'insufficient_independent_properties','training_properties':train.property_key.nunique(),'validation_properties':val.property_key.nunique()});continue
  print('Regression',name,'train',len(train),'validation',len(val),flush=True)
  directory=out/name;directory.mkdir();models={'baseline':fit_baseline(train)};alphas={}
  for block,(numeric,category) in BLOCKS.items():
   if block=='context' and not any(train[c].notna().sum()>10 and train[c].nunique()>1 for c in ['pop_density_1km','income_median']):continue
   try:
    alpha,cv=choose_alpha(train,numeric,category);b=fit_additive(train,numeric,category,alpha);b['kind']='additive';models[block]=b;alphas[block]={'alpha':alpha,'training_fold_mae':cv}
   except ValueError as exc:failures.append({'group':name,'model':block,'reason':str(exc)})
   print('  feature block',block,'complete',flush=True)
  models['tree']=tree(train)
  vals=[]
  for model,bundle in models.items():
   pred=predict(bundle,val);metric=error_metrics(val.target,pred)
   records.append({'group':name,'model':model,'training_properties':train.property_key.nunique(),'validation_properties':val.property_key.nunique(),**metric})
   vals.extend({'building_id':bid,'property_key':prop,'group':name,'model':model,'target':float(y),'prediction':float(pr)} for bid,prop,y,pr in zip(val.building_id,val.property_key,val.target,pred))
  pd.DataFrame(vals).to_parquet(directory/'validation-predictions.parquet',index=False)
  atomic_json(directory/'training-selection.json',alphas)
  atomic_json(directory/'spatial-check.json',spatial_check(train))
  if 'physical' in models:effect_outputs(models['physical'],train,directory)
  bundles[key]=models
 with (out/'models.pkl').open('wb') as f:pickle.dump(bundles,f)
 atomic_json(out/'metrics.json',records);atomic_json(out/'unsupported-groups.json',failures)
 # Feature blocks are evaluated without sealed test energy. Context is never a
 # physical grouping feature, even if it predicts well.
 import importlib.metadata
 atomic_json(out/'complete.json',{'families_fitted':len(bundles),'holdout_energy_read':False,'package_versions':{name:importlib.metadata.version(name) for name in ['scikit-learn','numpy','scipy','pandas']}})
 return records
