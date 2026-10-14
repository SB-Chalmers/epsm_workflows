"""Scientific guardrails for stock assignment, leakage, bounds and evaluation."""
import json
from pathlib import Path
import numpy as np
import pandas as pd
import pytest
from epsm_workflows.stock import prepare as ingestion
from epsm_workflows.stock.common import assign_splits,development,error_metrics,bootstrap,PHYSICAL,BLOCKS
from epsm_workflows.stock.calibration import fit_state_weights,bounded_spec,counts_pass
from epsm_workflows.campaign import atomic_json,file_sha256


def raw(**kw):
 return {'Version':'2.8','EgiBeraknatVarde':'Nej','EgiForstaArManad':'1901','EgiSistaArManad':'1912','EgenAtemp':'1000','EgenNybyggAr':'1970','EgenByggnadsKat':'Flerbostadshus','FormularId':'1','EgiFjarrvarmeUPPV':'100000','EgenAtempBostad':'100','EgenAntalPlan':'3','EgenProjVentFlode':'0.35','VentTypF':'Ja'}|kw


def test_targets_carriers_tails_versions_and_completed_evidence():
 r=ingestion.parse_record(raw(AtgUtfordaIsolVagg='Ja',AtgUtfordaIsolTak='Ja',AtgUtfordaUtfortAr='2017'))
 assert r['target']==100 and r['designed_flow_l_s_m2']==.35
 assert r['reno_wall']==1 and r['reno_roof']==1
 assert ingestion.parse_record(raw(EgiFjarrvarmeUPPV='99999999'))['target']>90000
 pump=ingestion.parse_record(raw(EgiFjarrvarmeUPPV='',EgiPumpMarkUPPV='12000'))
 assert pump['heating_family']=='heat_pump_ground' and pump['target']==12
 mixed=ingestion.parse_record(raw(EgiElDirektUPPV='1000'))
 assert '+' in mixed['heating_family']
 assert np.isnan(ingestion.parse_record(raw(AtgUtfordaIsolTak='Ja',AtgUtfordaUtfortAr='2020'))['reno_roof'])
 assert np.isnan(ingestion.parse_record(raw(AtgForslagIsolTak='Ja'))['reno_roof'])
 with pytest.raises(ValueError,match='unverified'):ingestion.parse_record(raw(Version='9.9'))
 with pytest.raises(ValueError,match='calendar|twelve'):ingestion.parse_record(raw(EgiForstaArManad='1902'))


def test_certificate_resolution_uses_period_not_id_or_energy():
 records=[{'building_id':'a','formular_id':100,'measurement_end':'2018-12-31','target':500}, {'building_id':'a','formular_id':1,'measurement_end':'2019-12-31','target':50}]
 chosen,excluded=ingestion.choose_certificates(records)
 assert chosen.formular_id.iloc[0]==1
 _,excluded=ingestion.choose_certificates(records+[records[1]|{'formular_id':2}])
 assert any(r['reason']=='conflicting_latest_period_certificates' for r in excluded)


def test_splits_are_property_atomic_energy_blind_and_prior_train_only():
 f=pd.DataFrame([dict(building_id=f'b{i}',property_key=f'p{i//2}',use_group='multi_family',heating_family='district_heat',era='SE.02' if i%3 else np.nan,climate_region='county_14',target=i*100) for i in range(400)])
 a=assign_splits(f,{'p1'});b=assign_splits(f.assign(target=-1).sample(frac=1,random_state=7),{'p1'})
 assert dict(zip(f.building_id,a))==dict(zip(f.sample(frac=1,random_state=7).building_id,b))
 assert a[f.property_key=='p1'].eq('train').all()
 assert f.assign(split=a).groupby('property_key').split.nunique().max()==1
 assert set(PHYSICAL).isdisjoint({'income_median','pop_density_1km','target','ep'})
 assert all('target' not in n+c for n,c in BLOCKS.values())


def test_property_bootstrap_and_strict_thresholds():
 m=error_metrics([100,100,100],[110,120,100]);assert m['below_10']==1/3 and m['below_20']==2/3
 b=bootstrap([100,100],[110,110],['same','same']);assert b['property_count']==1
 assert b['intervals']['mae']==[10,10]
 assert b['intervals']['bias']==[10,10]
 w=fit_state_weights([[10,20,30],[20,40,60]],[20,40]);assert np.all(w>=0) and w.sum()==pytest.approx(1)


def synthetic_study(tmp):
 from epsm_workflows.stock.common import NUMERIC,CATEGORICAL,CONTEXT
 rows=[]
 for i in range(120):
  x={c:np.nan for c in NUMERIC+CONTEXT};x.update({c:'unknown' for c in CATEGORICAL})
  x.update(building_id=f'b{i}',property_key=f'p{i}',use_group='multi_family',heating_family='district_heat',correction_status='normal_use_adjusted_pre_weather_correction',municipality='1480',era='SE.02',climate_region='county_14',construction_year=1970,measurement_year=2019,atemp_m2=500+i*10,footprint_area_m2=200+i,compactness=.5+i/500,height_m=9,storeys=3,heated_basement_floors=0,ventilation='F',roof_form='flat',building_position='Friliggande',age_conflict='none',renovation_timing='unknown')
  rows.append(x)
 f=pd.DataFrame(rows);f['split']=assign_splits(f,set());f.to_parquet(tmp/'features.parquet',index=False)
 t=f[['building_id','property_key','split']].copy();t['target']=40+f.atemp_m2/100
 for split,name in [(t.split!='test','targets-development.parquet'),(t.split=='test','targets-test-sealed.parquet')]:t[split].to_parquet(tmp/name,index=False)
 atomic_json(tmp/'manifest.json',{'features_sha256':file_sha256(tmp/'features.parquet'),'development_targets_sha256':file_sha256(tmp/'targets-development.parquet'),'test_targets_sha256':file_sha256(tmp/'targets-test-sealed.parquet')})
 cfg={'minimum_training_properties':5,'minimum_validation_properties':2,'cluster_minimum_properties':30,'cluster_resamples':2,'bootstrap_resamples':1000,'physics_min_buildings':120,'physics_min_properties':{'train':30,'validation':10,'test':20}}
 atomic_json(tmp/'config.json',cfg)
 pd.DataFrame(columns=f.columns).to_parquet(tmp/'simulation-candidates.parquet',index=False)
 return f


def test_end_to_end_holdout_is_read_only_after_freeze(tmp_path,monkeypatch):
 from epsm_workflows.stock.regression import regress
 from epsm_workflows.stock.clustering import cluster
 from epsm_workflows.stock.calibration import calibrate
 from epsm_workflows.stock.evaluation import evaluate
 synthetic_study(tmp_path)
 original=pd.read_parquet
 def protected(path,*args,**kwargs):
  if 'targets-test-sealed' in str(path) and not (tmp_path/'evaluation-opened.json').exists():raise AssertionError('Holdout energy read before freeze')
  return original(path,*args,**kwargs)
 monkeypatch.setattr(pd,'read_parquet',protected)
 regress(tmp_path);cluster(tmp_path);calibrate(tmp_path)
 assert not (tmp_path/'evaluation-opened.json').exists()
 metrics=evaluate(tmp_path);assert metrics
 assert evaluate(tmp_path)==metrics
 with pytest.raises(ValueError,match='fresh'):regress(tmp_path)
 frozen=tmp_path/'regression/metrics.json';frozen.write_text('[]')
 with pytest.raises(ValueError,match='changed'):evaluate(tmp_path)


def test_missing_development_target_hash_is_rejected(tmp_path):
 synthetic_study(tmp_path);p=tmp_path/'targets-development.parquet';f=pd.read_parquet(p);f['target']=1;f.to_parquet(p,index=False)
 with pytest.raises(ValueError,match='changed'):development(tmp_path)


def test_spatial_context_join_reference_year_and_missingness(tmp_path):
 import geopandas as gpd
 from shapely.geometry import box
 grid=tmp_path/'population.gpkg';gpd.GeoDataFrame({'beftotalt':[40]},geometry=[box(0,0,10,10)],crs=3006).to_file(grid,driver='GPKG')
 archive=tmp_path/'archive';(archive/'EDA').mkdir(parents=True)
 pd.DataFrame({'centroid_e':[1.0],'centroid_n':[1.0],'income_median':[300.0],'regso_code':['R1']}).to_csv(archive/'EDA/example_mfh_features.csv',index=False)
 f=pd.DataFrame({'building_id':['a','b'],'centroid_e':[1.0,20.0],'centroid_n':[1.0,20.0],'measurement_year':[2019,2020]})
 result,audit=ingestion.context(f,{'context_population':str(grid),'archive':str(archive)})
 assert result.pop_density_1km.iloc[0]==40
 assert np.isnan(result.pop_density_1km.iloc[1])
 assert result.income_median.iloc[0]==300 and np.isnan(result.income_median.iloc[1])
 assert result.context_retrospective.all() and audit['income_year']==2024
 assert audit['population_year']==2025


def test_bounded_physics_keeps_materials_infiltration_windows_and_ground():
 from epsm_workflows.clean_cohort_test import build_spec
 cohort=Path('output/clean-cohort-gothenburg-mf-2026-10-02-v3/cohort.json')
 weather=Path('output/clean-cohort-mf-se05-baseline-2026-10-02-v2/weather')
 if not cohort.exists() or not weather.exists():pytest.skip('Pinned physics fixture unavailable')
 row=json.loads(cohort.read_text())[0];original=build_spec(row,'existing',weather)
 changed=bounded_spec(original,{'wall':.8,'roof':1.2,'flow':.8})
 assert original.airflow.envelope_infiltration_ach==changed.airflow.envelope_infiltration_ach
 assert original.facade==changed.facade and original.envelope.ground==changed.envelope.ground
 for role in ['wall','top']:
  assert [x.material for x in getattr(original.envelope,role).layers]==[x.material for x in getattr(changed.envelope,role).layers]
 assert changed.airflow.mechanical_vent_l_s_m2==pytest.approx(original.airflow.mechanical_vent_l_s_m2*.8)
 with pytest.raises(ValueError,match='bounds'):bounded_spec(original,{'wall':2.,'roof':1.,'flow':1.})


def test_sample_gate_requires_independent_properties_not_building_count():
 cfg={'physics_min_buildings':120,'physics_min_properties':{'train':30,'validation':10,'test':20}}
 f=pd.DataFrame({'building_id':range(150),'property_key':['same']*150,'split':['train']*150})
 assert not counts_pass(f,cfg)[0]


def test_representatives_and_assignments_are_energy_blind(tmp_path):
 from epsm_workflows.stock.clustering import cluster
 f=synthetic_study(tmp_path);cluster(tmp_path)
 a=pd.read_parquet(tmp_path/'clustering/representatives.parquet')
 # Re-run in a separate study with identical inputs and different energy values.
 other=tmp_path/'other';other.mkdir();synthetic_study(other)
 t=pd.read_parquet(other/'targets-development.parquet');t['target']=t.target*100;t.to_parquet(other/'targets-development.parquet',index=False)
 m=json.loads((other/'manifest.json').read_text());m['development_targets_sha256']=file_sha256(other/'targets-development.parquet');atomic_json(other/'manifest.json',m)
 cfg=json.loads((other/'config.json').read_text());cfg['cluster_minimum_properties']=1000;atomic_json(other/'config.json',cfg)
 # Force pooled grouping in both cases: energy affects subdivision eligibility,
 # but never physical representative selection within a fixed grouping.
 first=tmp_path/'pooled';first.mkdir();synthetic_study(first);atomic_json(first/'config.json',cfg);cluster(first);cluster(other)
 assert pd.read_parquet(first/'clustering/representatives.parquet').equals(pd.read_parquet(other/'clustering/representatives.parquet'))


def test_betsi_total_exchange_is_not_an_infiltration_prior(tmp_path):
 from epsm_workflows.stock.evidence import archive_audit
 source=tmp_path/'report.md'
 source.write_text('spårämnesteknik. Den genomsnittliga luftomsättningen i småhusen är 0,40 oms/h och i\nflerbostadshuslägenheterna 0,52 oms/h.')
 audit=archive_audit({'archive':str(tmp_path),'betsi_report':str(source)})
 assert [p['mean'] for p in audit['verified_population_priors']]==[.4,.52]
 assert all(p['quantity']=='total_air_exchange' and not p['infiltration_prior'] for p in audit['verified_population_priors'])
