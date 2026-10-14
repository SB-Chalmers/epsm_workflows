"""Nested property validation of at most one observable physical subdivision."""
from __future__ import annotations
import pickle
from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.model_selection import GroupKFold
from sklearn.metrics import adjusted_rand_score
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler
from ..campaign import atomic_json
from ..stock.common import read,error_metrics,bootstrap
from ..stock.regression import fit_additive,predict

ALPHAS=(.1,1.,10.,100.)
NUMERIC=['construction_year','measurement_year','atemp_m2','footprint_area_m2','footprint_perimeter_m','compactness','height_m','storeys','apartments','atemp_per_apartment','gross_to_atemp_ratio','heated_basement_floors','designed_flow_l_s_m2','reno_window','reno_wall','reno_roof','reno_ventilation']
CATEGORICAL=['roof_form','building_position','age_conflict','renovation_timing']
RULE_NUMERIC=['height_m','compactness','atemp_per_apartment','gross_to_atemp_ratio','designed_flow_l_s_m2']
RULE_BINARY=['basement_present','reno_window']


def rules(frame):
    result=[]
    for col in RULE_NUMERIC:
        if frame[col].nunique()>1:
            result.extend({'feature':col,'kind':'quantile','quantile':q} for q in [.25,.5,.75])
    if frame.roof_form.isin(['flat','pitched_ridge']).sum()>0 and frame.roof_form[frame.roof_form.isin(['flat','pitched_ridge'])].nunique()>1:
        result.append({'feature':'roof_form','kind':'roof'})
    for col in RULE_BINARY:
        if frame[col].dropna().nunique()>1:result.append({'feature':col,'kind':'binary'})
    return result


def fit_rule(frame,rule):
    result=rule.copy()
    if rule['kind']=='quantile':result['threshold']=float(pd.to_numeric(frame[rule['feature']],errors='coerce').quantile(rule['quantile']))
    return result


def assign(frame,rule):
    if rule is None:return np.zeros(len(frame),int)
    s=frame[rule['feature']];labels=np.full(len(frame),-1,int)
    if rule['kind']=='roof':
        labels[s.eq('flat').to_numpy()]=0;labels[s.eq('pitched_ridge').to_numpy()]=1
    elif rule['kind']=='binary':
        labels[s.eq(0).to_numpy()]=0;labels[s.eq(1).to_numpy()]=1
    else:
        s=pd.to_numeric(s,errors='coerce');known=np.isfinite(s.to_numpy(float));labels[known]=(s.to_numpy(float)[known]>rule['threshold']).astype(int)
    return labels


def enough_branches(train,labels,minimum):
    return all(train.iloc[np.flatnonzero(labels==i)].property_key.nunique()>=minimum for i in (0,1))


def additive_model(train,alpha):
    bundle=fit_additive(train,NUMERIC,CATEGORICAL,alpha);bundle['kind']='additive';return bundle


def fit_choice(train,choice):
    pooled=additive_model(train,choice['alpha']);rule=fit_rule(train,choice['rule']) if choice['rule'] else None
    models={}
    if rule:
        labels=assign(train,rule)
        for i in (0,1):models[i]=additive_model(train.iloc[np.flatnonzero(labels==i)],choice['alpha'])
    return {'pooled':pooled,'rule':rule,'branches':models}


def prediction(bundle,frame):
    values=predict(bundle['pooled'],frame);labels=assign(frame,bundle['rule'])
    for label,model in bundle['branches'].items():
        ix=np.flatnonzero(labels==label)
        if len(ix):values[ix]=predict(model,frame.iloc[ix])
    return values


def select(train,cfg):
    """Inner validation chooses both ridge and rule; outer outcomes are absent."""
    if train.property_key.nunique()<cfg['inner_folds']:raise ValueError('Too few inner-fold properties')
    folds=list(GroupKFold(cfg['inner_folds']).split(train,groups=train.property_key))
    pooled={alpha:np.zeros(len(train)) for alpha in ALPHAS}
    cache={}
    for fold,(tr,va) in enumerate(folds):
        a=train.iloc[tr];b=train.iloc[va]
        for alpha in ALPHAS:
            model=additive_model(a,alpha);cache[fold,alpha]=model;pooled[alpha][va]=predict(model,b)
    best_alpha=min(ALPHAS,key=lambda alpha:(np.abs(pooled[alpha]-train.target.to_numpy()).mean(),-alpha))
    parent=error_metrics(train.target,pooled[best_alpha]);choice={'alpha':best_alpha,'rule':None};trials=[];best=parent['mae']
    for rule in rules(train):
        partitions=[];supported=True
        for fold,(tr,va) in enumerate(folds):
            a=train.iloc[tr];rr=fit_rule(a,rule);labels=assign(a,rr)
            if not enough_branches(a,labels,cfg['minimum_branch_properties']):supported=False;break
            partitions.append((tr,va,rr,labels))
        if not supported:
            trials.append({'rule':rule,'rejected':'fewer_than_minimum_independent_training_properties'});continue
        for alpha in ALPHAS:
            values=pooled[alpha].copy()
            for fold,(tr,va,rr,labels) in enumerate(partitions):
                a=train.iloc[tr];b=train.iloc[va];vb=assign(b,rr)
                for i in (0,1):
                    model=additive_model(a.iloc[np.flatnonzero(labels==i)],alpha);ix=np.flatnonzero(vb==i)
                    if len(ix):values[va[ix]]=predict(model,b.iloc[ix])
            metric=error_metrics(train.target,values);gain=1-metric['mae']/parent['mae']
            eligible=gain>=.05 and metric['below_20']>=parent['below_20']
            trials.append({'rule':rule,'alpha':alpha,'inner_metrics':metric,'gain':gain,'eligible':eligible})
            if eligible and metric['mae']<best-1e-9:
                choice={'alpha':alpha,'rule':rule};best=metric['mae']
    return choice,{'pooled_alpha':best_alpha,'pooled_metrics':parent,'trials':trials}


def select_diagnostic(frame,n,seed):
    """Input-only farthest-point selection, one building per property."""
    columns=['atemp_m2','height_m','compactness','measurement_year']
    x=frame.sort_values('building_id').reset_index(drop=True).copy()
    if x.property_key.nunique()<n:raise ValueError('Insufficient independent diagnostic properties')
    z=StandardScaler().fit_transform(SimpleImputer(strategy='median').fit_transform(x[columns]))
    roof=pd.get_dummies(x.roof_form).to_numpy(float);z=np.column_stack([z,roof])
    # Start at a central actual building, then cover physical differences.
    selected=[int(np.argmin(np.linalg.norm(z-z.mean(axis=0),axis=1)))];used={x.iloc[selected[0]].property_key}
    while len(selected)<n:
        distance=np.min(np.linalg.norm(z[:,None,:]-z[selected][None,:,:],axis=2),axis=1)
        distance[x.property_key.isin(used).to_numpy()]=-np.inf
        i=int(np.argmax(distance));selected.append(i);used.add(x.iloc[i].property_key)
    return x.iloc[selected].drop(columns=['target','heat_kwh'],errors='ignore').reset_index(drop=True)


def representatives(frame,rule):
    clean=frame.drop(columns=['target'],errors='ignore').sort_values('building_id').copy();labels=assign(clean,rule);result=[]
    columns=['atemp_m2','height_m','compactness','gross_to_atemp_ratio','atemp_per_apartment']
    for label in sorted(set(labels)-{-1}):
        part=clean.iloc[np.flatnonzero(labels==label)].copy()
        usable=[c for c in columns if part[c].notna().any()]
        z=StandardScaler().fit_transform(SimpleImputer(strategy='median').fit_transform(part[usable]))
        d=np.linalg.norm(z-z.mean(axis=0),axis=1);order=np.argsort(d,kind='stable');chosen=[int(order[0])];used={part.iloc[chosen[0]].property_key}
        for i in np.argsort(-d,kind='stable'):
            if d[i]>np.quantile(d,.95) or part.iloc[i].property_key in used:continue
            chosen.append(int(i));used.add(part.iloc[i].property_key)
            if len(chosen)==5:break
        result.extend({'branch':int(label),'building_id':part.iloc[i].building_id,'property_key':part.iloc[i].property_key,'role':'central' if j==0 else 'contrast','distance':float(d[i])} for j,i in enumerate(chosen))
    return result


def regress(cfg):
    root=Path(cfg['output']);out=root/'regression';out.mkdir(exist_ok=True)
    frame=pd.read_parquet(root/'features.parquet').merge(pd.read_parquet(root/'targets-development.parquet')[['building_id','target']],on='building_id',validate='one_to_one').reset_index(drop=True)
    values={name:np.zeros(len(frame)) for name in ['baseline','pooled','subgroup_policy']};outer_ids=np.full(len(frame),-1);choices=[];folds=[];models=[]
    for fold,(tr,va) in enumerate(GroupKFold(cfg['outer_folds']).split(frame,groups=frame.property_key)):
        train=frame.iloc[tr];val=frame.iloc[va]
        assert set(train.property_key).isdisjoint(val.property_key)
        choice,audit=select(train,cfg);pooled=fit_choice(train,{'alpha':audit['pooled_alpha'],'rule':None});candidate=fit_choice(train,choice)
        values['baseline'][va]=float(train.target.median());values['pooled'][va]=prediction(pooled,val);values['subgroup_policy'][va]=prediction(candidate,val);outer_ids[va]=fold
        choices.append({'fold':fold,'choice':choice,'fitted_rule':candidate['rule'],'inner_selection':audit});models.append({'fold':fold,'pooled':pooled,'candidate':candidate})
        folds.extend({'property_key':prop,'outer_fold':fold} for prop in sorted(val.property_key.unique()))
        atomic_json(out/f'fold-{fold}-selection.json',choices[-1]);print('Outer property fold',fold+1,'complete; choice',choice,flush=True)
    records=[];metrics={}
    for label,pred in values.items():
        metrics[label]={'metrics':error_metrics(frame.target,pred),'bootstrap':bootstrap(frame.target,pred,frame.property_key,cfg['bootstrap_resamples'],baseline=values['pooled'] if label=='subgroup_policy' else None)}
        records.extend({'building_id':bid,'property_key':prop,'outer_fold':int(fold),'model':label,'target':float(y),'prediction':float(p),'signed_error':float(p-y),'absolute_percentage_error':float(100*abs(p-y)/y)} for bid,prop,fold,y,p in zip(frame.building_id,frame.property_key,outer_ids,frame.target,pred))
    pd.DataFrame(records).to_parquet(out/'out-of-fold.parquet',index=False);pd.DataFrame(folds).to_parquet(out/'fold-properties.parquet',index=False)
    choice,selection=select(frame,cfg);bundle=fit_choice(frame,choice)
    pooled=metrics['pooled']['metrics'];policy=metrics['subgroup_policy']['metrics'];ci=metrics['subgroup_policy']['bootstrap']['intervals']['mae_difference'];gain=1-policy['mae']/pooled['mae']
    performance=bool(choice['rule'] and gain>=.05 and policy['below_20']>=pooled['below_20'] and ci[1]<0)
    stability=[]
    if performance:
        ref=assign(frame,bundle['rule']);props=frame.property_key.unique();rng=np.random.default_rng(cfg['seed'])
        for sample in range(cfg['stability_resamples']):
            ids=rng.choice(props,size=int(.8*len(props)),replace=False);sub=frame[frame.property_key.isin(ids)]
            c,_=select(sub,cfg);r=fit_rule(sub,c['rule']) if c['rule'] else None;other=assign(frame,r);known=(ref!=-1)&(other!=-1)
            ari=float(adjusted_rand_score(ref[known],other[known])) if known.sum()>1 else 0
            stability.append({'sample':sample,'choice':c,'ari':ari});print('Assignment resample',sample+1,ari,flush=True)
    median=float(np.median([r['ari'] for r in stability])) if stability else None
    retained=bool(performance and median is not None and median>=.7)
    retained_rule=bundle['rule'] if retained else None
    final_bundle=bundle if retained else fit_choice(frame,{'alpha':selection['pooled_alpha'],'rule':None})
    labels=assign(frame,retained_rule)
    pd.DataFrame({'building_id':frame.building_id,'property_key':frame.property_key,'branch':labels,'assignment_confidence':np.where(labels==-1,'unknown_rule_input','development_supported' if retained else 'pooled')}).to_parquet(out/'assignments.parquet',index=False)
    pd.DataFrame(representatives(frame,retained_rule)).to_parquet(out/'representatives.parquet',index=False)
    atomic_json(out/'metrics.json',metrics);atomic_json(out/'full-selection.json',selection)
    atomic_json(out/'decision.json',{'proposed_rule':bundle['rule'],'retained_rule':retained_rule,'development_gain':gain,'paired_mae_difference_interval':ci,'median_assignment_ari':median,'resamples':stability,'retained':retained,'fresh_holdout':False,'performance_gate_passed':performance,'assignment_excludes_heating_and_context':True})
    with (out/'models.pkl').open('wb') as f:pickle.dump({'outer_models':models,'final':final_bundle},f)
    atomic_json(out/'complete.json',{'properties':frame.property_key.nunique(),'buildings':len(frame),'nested_property_folds':True,'retained_subdivision':retained,'all_results_development_only':True})
