"""Scientific guardrails for focused, development-only subgroup diagnosis."""
from pathlib import Path
import json
import numpy as np
import pandas as pd
import pytest
from epsm_workflows.archetype_diagnosis.audit import audited_record,basement_count
from epsm_workflows.archetype_diagnosis.regression import assign,fit_rule,enough_branches,select_diagnostic,representatives,select,NUMERIC,CATEGORICAL
from epsm_workflows.archetype_diagnosis.physics import jobs,build_spec,summarize
from epsm_workflows.stock.common import read


def raw(**updates):
    return {'Version':'2.8','EgiBeraknatVarde':'Nej','EgiForstaArManad':'1901','EgiSistaArManad':'1912','EgenAtemp':'1000','EgenNybyggAr':'1970','EgenByggnadsKat':'Flerbostadshus','FormularId':'1','EgiFjarrvarmeUPPV':'100000','EgenAtempBostad':'100','EgenAntalPlan':'3','EgenAntalKallarplan':'1','EgenProjVentFlode':'0.35','VentTypF':'Ja'}|updates


def fixture(n=180):
    f=pd.DataFrame({c:[np.nan]*n for c in NUMERIC})
    for c in CATEGORICAL:f[c]='unknown'
    f['building_id']=[f'b{i:04}' for i in range(n)];f['property_key']=[f'p{i:04}' for i in range(n)]
    f['atemp_m2']=1000+np.arange(n)*5;f['height_m']=10+np.arange(n)/100;f['compactness']=.4+np.arange(n)/1000;f['gross_to_atemp_ratio']=1.;f['atemp_per_apartment']=80.;f['measurement_year']=2018;f['construction_year']=1970
    f['roof_form']=np.where(np.arange(n)<n/2,'flat','pitched_ridge');f['basement_present']=0.;f['target']=80+np.arange(n)/20
    return f


def test_raw_semantics_dates_and_normalised_flow():
    x=audited_record(raw(AtgUtfordaByteFonster='Ja',AtgUtfordaUtfortAr='2017'))
    assert x['storeys']==3 and x['heated_basement_floors']==1
    assert x['designed_flow_l_s_m2']==.35 and x['reno_window']==1
    assert np.isnan(audited_record(raw(AtgUtfordaByteFonster='Ja',AtgUtfordaUtfortAr='2020'))['reno_window'])
    assert np.isnan(basement_count('1-2')) and np.isnan(basement_count(''))
    assert np.isnan(basement_count('0.5'))


def test_rule_threshold_learns_training_only_and_unknown_routes_parent():
    f=fixture();r=fit_rule(f.iloc[:90],{'feature':'height_m','kind':'quantile','quantile':.5})
    assert r['threshold']==pytest.approx(f.height_m.iloc[:90].median())
    changed=f.copy();changed.loc[90:,'height_m']=1000
    assert fit_rule(changed.iloc[:90],{'feature':'height_m','kind':'quantile','quantile':.5})==r
    g=f.iloc[:3].copy();g.loc[g.index[0],'height_m']=np.nan
    assert assign(g,r)[0]==-1
    assert assign(f.assign(roof_form='unknown'),{'feature':'roof_form','kind':'roof'}).tolist()==[-1]*len(f)
    assert not enough_branches(f.assign(property_key='same'),np.arange(len(f))%2,30)


def test_physical_selection_and_representatives_ignore_energy():
    f=fixture();a=select_diagnostic(f,12,1);b=select_diagnostic(f.assign(target=100000),12,1)
    assert a.equals(b) and a.property_key.nunique()==12 and 'target' not in a
    assert representatives(f,None)==representatives(f.assign(target=-1),None)
    assert set(NUMERIC+CATEGORICAL).isdisjoint({'target','energy_class','income_median','pop_density_1km'})


def test_inner_folds_are_property_separated_and_rule_can_be_declined():
    f=fixture(80);cfg={'inner_folds':5,'minimum_branch_properties':1000}
    choice,audit=select(f,cfg)
    assert choice['rule'] is None
    assert all('rejected' in x for x in audit['trials'])


def test_exact_scenarios_fixed_inputs_and_compile():
    if not Path('output/stock-national-2026-10-02-v2/simulation-candidates.parquet').exists():pytest.skip('Local pinned model/weather fixture unavailable')
    candidates=pd.read_parquet('output/stock-national-2026-10-02-v2/simulation-candidates.parquet')
    row=candidates[(candidates.municipality.astype(str)=='1480')&(candidates.archetype_id=='mf_se02')&(candidates.ventilation=='F')&(candidates.measurement_year==2018)].iloc[0].to_dict()
    if pd.isna(row['designed_ventilation_l_s_m2']):row['designed_ventilation_l_s_m2']=None
    weather=Path('output/archetype-pilot-mf-se02-f-2026-09-30/weather').resolve()
    # A controlled rectangle is the compilation fixture. National geometry is
    # independently compiled by the run, retaining all failures without fallback.
    from shapely.geometry import box
    row['geometry_wkb_hex']=box(320000,6400000,320020,6400016).wkb_hex
    row['roof_form']='flat'
    baseline=build_spec(row,'existing',weather,'frozen')
    for scenario in ['window_22','wwr_03','combined']:
        changed=build_spec(row,'existing',weather,scenario)
        for field in ['geometry','envelope','airflow','use']:assert getattr(changed,field)==getattr(baseline,field)
        if scenario in ['window_22','combined']:assert changed.facade.glazing.u_value_w_m2k==2.2
    with pytest.raises(ValueError):build_spec(row,'advanced',weather,'window_22')
    assert len(jobs([row]*12))==96
    from epsm_core import EnergyPlusRunner
    runner=EnergyPlusRunner(energyplus='/Applications/OpenStudio-3.9.0/EnergyPlus/energyplus')
    model,report=runner.compiler.compile(baseline)
    assert report.data['conditioned_floor_area_m2']==pytest.approx(baseline.geometry.conditioned_floor_area_m2)


def test_paired_metrics_keep_failures_and_delivery_sensitivities(tmp_path):
    results=[]
    for bid,pr in [('a',80),('b',120)]:
        for scenario,delta in [('frozen',0),('window_22',-10)]:
            results.append({'building_id':bid,'property_key':bid,'state':'existing','scenario':scenario,'atemp_m2':1000,'conditioned_area_m2':1100,'conditioned_volume_m3':3000,'zone_count':3,'status':'accepted','demand_kwh_m2':pr+delta})
    results.append({'building_id':'c','state':'existing','scenario':'frozen','status':'failed'})
    summarize([],results,pd.Series({'a':100,'b':100}),{'bootstrap_resamples':20},tmp_path)
    comparisons=read(tmp_path/'paired-comparisons.json')
    assert len(comparisons)==3
    p=next(x for x in comparisons if x['loss_kwh_m2']==0)
    assert p['paired']==2 and p['improved']==1 and p['worsened']==1
    m=next(x for x in read(tmp_path/'metrics.json') if x['state']=='existing' and x['scenario']=='frozen' and x['loss_kwh_m2']==0)
    assert m['attempted']==3 and m['accepted']==2
