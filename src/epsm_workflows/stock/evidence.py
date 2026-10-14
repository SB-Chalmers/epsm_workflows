"""Source audits and field provenance; no heating target is accessed here."""
from pathlib import Path
import re
from .common import read,features
from ..campaign import atomic_json,file_sha256


def archive_audit(config):
 archive=Path(config['archive']);sources=[]
 for name in ['EDA/step_3_clustering_betsi.py','EDA/step_2_scb_socioeconomic.py','gothenburg_paper_v2/tables/step_02_summary.md']:
  path=archive/name
  if path.exists():sources.append({'path':str(path),'sha256':file_sha256(path)})
 priors=[];report=Path(config['betsi_report']) if config.get('betsi_report') else None
 if report and report.is_file():
  text=report.read_text();sources.append({'path':str(report),'sha256':file_sha256(report)})
  # Only the explicitly identified passive-tracer total-air-change survey means.
  match=re.search(r'genomsnittliga luftomsättningen i småhusen är ([\d,]+) oms/h och i\s*flerbostadshuslägenheterna ([\d,]+) oms/h',text)
  if match and 'spårämnesteknik' in text:
   priors=[{'use':use,'mean':float(value.replace(',','.')),'unit':'h^-1','quantity':'total_air_exchange','measurement':'passive tracer','survey_period':'2007–2008','infiltration_prior':False,'application':'population plausibility context only; never added as infiltration'} for use,value in zip(['single_family','multi_family'],match.groups())]
 return {'sources':sources,'verified_population_priors':priors,'archive_templates':'Morphology-to-template assignments are not measured materials for EPC buildings','omitted_variables':['slenderness duplicates compactness','fixed 3 m height','footprint × floors × 0.85 Atemp','archive outcome trimming'],'facade_estimates':'Not used without dated images and measurement-quality review','no_energy_access':True}


def audit_prepared_evidence(root):
 root=Path(root)
 if (root/'evaluation-opened.json').exists():raise ValueError('Evidence audit must precede holdout opening')
 config=read(root/'config.json');frame=features(root)
 atomic_json(root/'archive-audit.json',archive_audit(config))
 dictionary=read(root/'field-dictionary.json');fields={}
 derived={'compactness','volume_proxy_m3','gross_to_atemp_ratio','atemp_per_apartment','era','climate_region','footprint_area_m2','footprint_perimeter_m','centroid_e','centroid_n','use_group','heating_family','assignment_evidence','split'}
 for col in frame:
  source='current metric footprint' if col.startswith('footprint') or col in ['compactness','centroid_e','centroid_n'] else 'promoted LiDAR release' if col in ['height_m','height_status','roof_form'] else 'age registry and EPC' if col in ['construction_year','registry_year','epc_year','age_conflict','era'] else 'archived SCB spatial mapping' if col in ['pop_density_1km','income_median','regso_code'] else 'reported EPC and preparation derivation'
  unit='m²' if col.endswith('_m2') or col=='atemp_per_apartment' else 'L/s/m²' if col=='designed_flow_l_s_m2' else 'm³' if col.endswith('_m3') else 'm' if col.endswith('_m') or col in ['centroid_e','centroid_n'] else 'dimensionless' if col in ['compactness','gross_to_atemp_ratio'] else '%' if col.endswith('_percent') else 'people/km²' if col=='pop_density_1km' else 'SEK/year' if col=='income_median' else 'calendar year' if col.endswith('_year') else 'category, count or identifier'
  reference=2025 if col=='pop_density_1km' else 2024 if col=='income_median' else 'source release / per-building measurement year; system timing unverified'
  fields[col]={'source':source,'unit':unit,'status':'derived' if col in derived else 'observed LiDAR derived' if col=='height_m' else 'reported or missing','reference_date':reference,'nonmissing_fraction':float(frame[col].notna().mean())}
 fields['heating_degree_days']={'source':'optional audited municipality/year weather descriptors','unit':'K·day','status':'derived or missing','reference_date':'measurement year','nonmissing_fraction':float(frame.heating_degree_days.notna().mean())}
 dictionary['stock_fields']=fields;atomic_json(root/'field-dictionary.json',dictionary)
