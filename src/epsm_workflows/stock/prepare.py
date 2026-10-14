"""National EPC evidence ingestion; energy is stored separately from assignment features."""
from __future__ import annotations
import csv,json,math,sqlite3
from collections import Counter,defaultdict
from pathlib import Path
import duckdb,numpy as np,pandas as pd
from shapely import from_wkb
from .. import archetype_pilot as pilot,archetype_diagnostics as diagnostic
from ..validation import measurement_period,number
from ..campaign import atomic_json,file_sha256
from ..archetype_membership import assess_membership
from .common import assign_splits,digest
CARRIERS={'Fjarrvarme':'district_heat','Olja':'oil','Gas':'gas','Ved':'wood','Flis':'wood_chips','OvrBiobransle':'other_biomass','ElVatten':'electric_water','ElDirekt':'electric_direct','ElLuft':'electric_air','PumpMark':'heat_pump_ground','PumpFranluft':'heat_pump_exhaust','PumpLuftLuft':'heat_pump_air_air','PumpLuftVatten':'heat_pump_air_water'}
USE_FIELDS={'hotel_percent':'EgenAtempHotell','restaurant_percent':'EgenAtempRestaurang','grocery_percent':'EgenAtempLivsmedel','retail_percent':'EgenAtempButik','shopping_percent':'EgenAtempKopcentrum','healthcare_percent':'EgenAtempVard','daytime_service_percent':'EgenAtempVardDag','bath_percent':'EgenAtempBad','theatre_percent':'EgenAtempTeater','other_use_percent':'EgenAtempOvrig'}
VENT={'VentTypSjalvdrag':'S','VentTypF':'F','VentTypFT':'FT','VentTypFTX':'FTX','VentTypFmed':'Fmed'}
RENOVATION={'reno_window':['ByteFonster','KompFonster','TatFonster'],'reno_wall':['IsolVagg'],'reno_roof':['IsolTak'],'reno_ventilation':['NyVentil','ByteVent','AterVent']}

def optional(r,key):return number(r,key,required=False)
def flag(r,names):
 vals=[r.get('AtgUtforda'+n) for n in names]
 return 1. if 'Ja' in vals else 0. if all(v=='Nej' for v in vals) else np.nan

def parse_record(raw):
 if raw.get('Version') not in diagnostic.FORM_EVIDENCE:raise ValueError('unverified_form_version')
 if raw.get('EgiBeraknatVarde')!='Nej':raise ValueError('calculated_or_unknown_energy')
 start,end=measurement_period(raw['EgiForstaArManad'],raw['EgiSistaArManad'])
 if (start.month,start.day,end.month,end.day)!=(1,1,12,31):raise ValueError('not_calendar_year')
 area=number(raw,'EgenAtemp')
 values={fuel:optional(raw,'Egi'+fuel+'UPPV') for fuel in CARRIERS}
 if any(v is not None and v<0 for v in values.values()):raise ValueError('negative_heating')
 active=[fuel for fuel,v in values.items() if v is not None and v>0]
 if area<=0 or not active:raise ValueError('nonpositive_area_or_heat')
 if (optional(raw,'EgiSolvarme') or 0)>0:raise ValueError('solar_heat_boundary_unresolved')
 heating=sum(values[f] for f in active);family='+'.join(sorted(CARRIERS[f] for f in active))
 vents=[v for k,v in VENT.items() if raw.get(k)=='Ja'];vent=vents[0] if len(vents)==1 else 'multiple' if vents else 'unknown'
 category=raw.get('EgenByggnadsKat','').casefold();res=optional(raw,'EgenAtempBostad');office=optional(raw,'EgenAtempKontor');school=optional(raw,'EgenAtempSkolor')
 use='single_family' if 'tvåbostad' in category or 'tvabostad' in category else 'multi_family' if category=='flerbostadshus' else 'office' if office==100 else 'school' if school==100 else 'mixed_nonresidential'
 extra_use={k:optional(raw,v) for k,v in USE_FIELDS.items()}
 if use=='mixed_nonresidential':
  pure=next((k.removesuffix('_percent') for k,v in extra_use.items() if v==100),None)
  if pure:use=pure
 if use in ('single_family','multi_family') and res is not None and res<100:use='mixed_residential'
 age=optional(raw,'EgenNybyggAr');age=age if age is not None and 1<=age<=end.year else np.nan
 completed_year=optional(raw,'AtgUtfordaUtfortAr')
 timing='during_or_before' if completed_year is not None and completed_year<=end.year else 'after_measurement' if completed_year is not None else 'unknown'
 flow=optional(raw,'EgenProjVentFlode')
 if flow is not None and not 0<=flow<=10:flow=None
 x=dict(formular_id=int(raw['FormularId']),form_version=raw['Version'],measurement_year=end.year,measurement_end=end.isoformat(),
   target=heating/area,energy_fields=[f'Egi{f}UPPV' for f in active],correction_status='normal_use_adjusted_pre_weather_correction',
   heating_family=family,heating_components=values,use_group=use,epc_building_category=raw.get('EgenByggnadsKat'),atemp_m2=area,epc_year=age,
   residential_percent=res,office_percent=office,school_percent=school,
   storeys=optional(raw,'EgenAntalPlan'),apartments=optional(raw,'EgenAntalBolgh'),heated_basement_floors=optional(raw,'EgenAntalKallarplan'),
   garage_percent=100*(optional(raw,'EgenAvarmgarage') or 0)/area,
   designed_flow_l_s_m2=flow,ventilation=vent,building_position=raw.get('EgenByggnadsTyp') or 'unknown',
   renovation_timing=timing,completed_renovation_year=completed_year,
   reported_completed_measures=json.dumps({k:v for k,v in raw.items() if k.startswith('AtgUtforda') and v},ensure_ascii=False))
 x.update(extra_use)
 for key,names in RENOVATION.items():
  x[key]=flag(raw,names) if timing=='during_or_before' else np.nan
 return x


def choose_certificates(rows):
 """Choose by measurement dates, never formular-ID or target values."""
 chosen=[];excluded=[]
 for bid,group in pd.DataFrame(rows).groupby('building_id',sort=True):
  newest=group.measurement_end.max();latest=group[group.measurement_end==newest]
  if len(latest)!=1:
   excluded.extend({'building_id':bid,'formular_id':int(fid),'reason':'conflicting_latest_period_certificates'} for fid in latest.formular_id);continue
  chosen.append(latest.iloc[0].to_dict())
  excluded.extend({'building_id':bid,'formular_id':int(fid),'reason':'older_measurement_period'} for fid in group[group.measurement_end!=newest].formular_id)
 return pd.DataFrame(chosen),excluded


def prior_properties(config,links):
 fids=set();props=set();sources=[];unresolved=[]
 archive=Path(config['archive'])
 for path in sorted([*archive.rglob('*.csv'),*archive.rglob('*.parquet')]):
  try:
   if path.suffix=='.csv':
    with path.open(errors='replace',newline='') as f:
     reader=csv.DictReader(f);field=next((k for k in reader.fieldnames or [] if k.lower() in ('formularid','formular_id','epc_formular_id')),None)
     if field is None:continue
     for row in reader:
      try:fids.add(int(float(row[field])))
      except (TypeError,ValueError):continue
   else:
    import pyarrow.parquet as pq
    field=next((k for k in pq.read_schema(path).names if k.lower() in ('formularid','formular_id','epc_formular_id')),None)
    if field is None:continue
    fids.update(int(x) for x in pd.read_parquet(path,columns=[field])[field].dropna())
   sources.append({'path':str(path),'sha256':file_sha256(path)})
  except Exception as exc:unresolved.append({'path':str(path),'reason':str(exc)})
 for path in sorted(Path(config['recent_studies']).glob('*/cohort.json')):
  try:
   rows=json.loads(path.read_text())
   if not isinstance(rows,list):continue
   for row in rows:
    if row.get('property_key'):props.add(row['property_key'])
    if row.get('formular_id'):fids.add(int(row['formular_id']))
   sources.append({'path':str(path),'sha256':file_sha256(path)})
  except Exception as exc:unresolved.append({'path':str(path),'reason':str(exc)})
 props.update(links[links.formular_id.isin(fids)].property_key.dropna())
 return props,dict(source_files=sources,unreadable_sources=unresolved,form_ids=len(fids),properties=len(props),unmapped_form_ids=len(fids-set(links.formular_id)))


def enrich(frame,config,root):
 db=duckdb.connect();db.execute('SET threads=4');db.register('ids',frame[['building_id']]);hashes={}
 pieces=[];paths=sorted((Path(config['publication'])/'building_results').glob('*/part.parquet'))
 for i,path in enumerate(paths):
  pieces.extend(db.execute('SELECT b.building_id,b.geometry,b.insamlingslage,b.building_area_m2,b.andamal1,b.andamal2,b.andamal3,b.andamal4,b.andamal5 FROM read_parquet(?) b JOIN ids USING(building_id)',[str(path)]).fetchall())
  hashes[str(path)]=file_sha256(path)
  if (i+1)%50==0:print('Footprint partitions',i+1,'/',len(paths),flush=True)
 geometries={bid:(g,outline,area) for bid,g,outline,area in pieces};del pieces
 fields=[]
 for bid in frame.building_id:
  item=geometries.get(bid);x={'building_id':bid,'geometry_wkb_hex':None,'footprint_area_m2':np.nan,'footprint_perimeter_m':np.nan,'compactness':np.nan,'outline':'unknown','centroid_e':np.nan,'centroid_n':np.nan,'geometry_status':'missing'}
  if item:
   g,outline,_=item[:3]
   x.update(dict(zip(('purpose','purpose2','purpose3','purpose4','purpose5'),item[3:])))
   try:
    poly=from_wkb(g)
    if not poly.is_valid or poly.is_empty or poly.area<=0:raise ValueError('invalid_geometry')
    x.update(geometry_wkb_hex=poly.wkb_hex,footprint_area_m2=poly.area,footprint_perimeter_m=poly.length,compactness=4*math.pi*poly.area/poly.length**2,
      outline=outline,centroid_e=poly.centroid.x,centroid_n=poly.centroid.y,geometry_status='valid')
   except Exception:x['geometry_status']='invalid'
  fields.append(x)
 frame=frame.merge(pd.DataFrame(fields),on='building_id',validate='one_to_one')
 with sqlite3.connect(f"file:{config['age_registry']}?mode=ro",uri=True) as age:
  years={};ids=frame.building_id.tolist()
  for i in range(0,len(ids),500):
   batch=ids[i:i+500];years.update(age.execute('SELECT building_id,construction_year FROM registry_buildings WHERE building_id IN ('+','.join('?' for _ in batch)+')',batch))
 frame['registry_year']=frame.building_id.map(years)
 frame['registry_year']=frame.registry_year.where(frame.registry_year<=frame.measurement_year)
 frame['construction_year']=frame.registry_year.fillna(frame.epc_year)
 frame['age_conflict']=np.where(frame.registry_year.notna()&frame.epc_year.notna()&(frame.registry_year!=frame.epc_year),'conflict','none_or_unknown')
 frame['era']=pd.cut(frame.construction_year,[0,1939,1949,1960,1975,1985,1995,2005,9999],labels=['pre1940','1940s','1950s','SE.02','SE.03','SE.04','SE.05','SE.06']).astype(str)
 heightfiles=sorted((Path(config['height_release'])/'results/buildings').glob('*/part.parquet'));pieces=[]
 for i in range(0,len(heightfiles),500):
  batch=[str(p) for p in heightfiles[i:i+500]]
  pieces.append(db.execute('SELECT h.building_id,h.height_status,h.representative_height_m,h.roof_form,h.eave_height_m,h.ridge_height_m FROM read_parquet(?,union_by_name=true,hive_partitioning=false) h JOIN ids USING(building_id)',[batch]).fetchdf())
  hashes.update({str(p):file_sha256(p) for p in heightfiles[i:i+500]})
  if i%5000==0:print('Height tiles',i,'/',len(heightfiles),flush=True)
 heights=pd.concat(pieces,ignore_index=True)
 if heights.building_id.duplicated().any():raise ValueError('Duplicate promoted height records')
 frame=frame.merge(heights,on='building_id',how='left',validate='one_to_one')
 frame['height_m']=np.where(frame.height_status=='determined',np.where(frame.roof_form=='pitched_ridge',frame.eave_height_m,frame.representative_height_m),np.nan)
 frame['volume_proxy_m3']=frame.footprint_area_m2*frame.height_m
 frame['gross_to_atemp_ratio']=frame.footprint_area_m2*frame.storeys/frame.atemp_m2
 frame['atemp_per_apartment']=frame.atemp_m2/frame.apartments.where(frame.apartments>0)
 # County is a geographic grouping, not a claimed BBR climate-zone mapping.
 frame['climate_region']='county_'+frame.municipality.str[:2]
 frame['roof_form']=frame.roof_form.fillna('unknown')
 frame['registry_year']=frame.registry_year.where(frame.registry_year<=frame.measurement_year)
 atomic_json(root/'geometry-source-hashes.json',hashes)
 return frame,dict(age_query_sha256=digest(sorted(years.items())),height_tile_count=len(heightfiles),geometry_source_hashes_sha256=file_sha256(root/'geometry-source-hashes.json'),climate_note='County proxy; no measurement-year weather descriptors available nationally. Physical calibration requires audited local weather.')


def context(frame,config):
 frame=frame.copy();frame['pop_density_1km']=np.nan;frame['income_median']=np.nan;frame['regso_code']='unknown';sources=[]
 pop=Path(config['context_population'])
 if pop.exists():
  import geopandas as gpd
  from shapely import points
  grid=gpd.read_file(pop)[['beftotalt','geometry']].to_crs(3006)
  mask=frame.centroid_e.notna()&frame.centroid_n.notna()
  pts=gpd.GeoDataFrame(frame.loc[mask,['building_id']],geometry=points(frame.loc[mask,'centroid_e'].values,frame.loc[mask,'centroid_n'].values),crs=3006)
  joined=gpd.sjoin(pts,grid,how='left',predicate='within').drop_duplicates('building_id')
  frame['pop_density_1km']=frame.building_id.map(joined.set_index('building_id').beftotalt)
  sources.append({'path':str(pop),'sha256':file_sha256(pop),'year':2025,'unit':'people per 1 km grid cell; numerical population/km²'})
 # Archived RegSO/income join exists only for selected cities; re-use only unique
 # consistent coordinate matches, preserving sparse coverage rather than guessing.
 values=defaultdict(set)
 for path in sorted((Path(config['archive'])/'EDA').glob('*_mfh_features.csv')):
  df=pd.read_csv(path)
  if not {'centroid_e','centroid_n','income_median','regso_code'}.issubset(df):continue
  for row in df[['centroid_e','centroid_n','income_median','regso_code']].dropna().itertuples(index=False):values[(round(row[0],1),round(row[1],1))].add((float(row[2]),str(row[3])))
  sources.append({'path':str(path),'sha256':file_sha256(path),'income_year':2024,'regso_boundary_year':2025})
 for idx,row in frame[['centroid_e','centroid_n']].iterrows():
  if pd.isna(row.iloc[0]):continue
  match=values.get((round(row.iloc[0],1),round(row.iloc[1],1)),set())
  if len(match)==1:frame.loc[idx,['income_median','regso_code']]=next(iter(match))
 frame['heating_degree_days']=np.nan
 if config.get('climate_descriptors'):
  weather=pd.read_parquet(config['climate_descriptors'])
  if weather.duplicated(['municipality','measurement_year']).any():raise ValueError('Conflicting climate descriptor rows')
  needed={'municipality','measurement_year','heating_degree_days','source_sha256','base_temperature_c','weather_correction_status'}
  if not needed.issubset(weather):raise ValueError('Climate descriptors require audited source, base temperature and correction status')
  frame=frame.drop(columns='heating_degree_days').merge(weather[list(needed)],on=['municipality','measurement_year'],how='left',validate='many_to_one')
 frame['context_reference_year']=2025
 frame['context_retrospective']=frame.measurement_year<2025
 return frame,dict(sources=sources,income_join='Exact 0.1 m coordinate match to archived joins only; national RegSO boundaries not pinned',population_year=2025,income_year=2024,retrospective_only=True)


def simulation_candidates(frame):
 # Raw EPC labels and detached placement alone do not establish membership.
 # Keep all inventory rows; reject source conflicts before native cohort fitting.
 assessments=[assess_membership(row) for row in frame.to_dict('records')]
 membership=pd.Series([r['eligible'] for r in assessments],index=frame.index)
 gis_use_group=pd.Series([r['gis_assignment']['use_group'] for r in assessments],index=frame.index)
 good=(gis_use_group=='multi_family')&(frame.residential_percent==100)&(frame.heating_family=='district_heat')&frame.ventilation.isin(['F','FT','FTX'])&(frame.heated_basement_floors==0)&(frame.garage_percent==0)&(frame.building_position=='Friliggande')&(frame.height_status=='determined')&frame.roof_form.isin(['flat','pitched_ridge'])&frame.gross_to_atemp_ratio.between(.95,1.2)&frame.storeys.between(2,20)&(frame.geometry_status=='valid')&frame.outline.isin(['Fasad','Takkant'])&(frame.age_conflict!='conflict')
 good &= membership
 rows=[];failed=[]
 for row in frame[good].to_dict('records'):
  try:
   height=dict(height_status=row['height_status'],roof_form=row['roof_form'],representative_height_m=row['representative_height_m'],eave_height_m=row['eave_height_m'],ridge_height_m=row['ridge_height_m'])
   roof,floorheight=pilot._geometry(height,int(row['storeys']));wkb,error=pilot._model_footprint(bytes.fromhex(row['geometry_wkb_hex']),roof)
   aid={'1940s':'mf_1940','1950s':'mf_1950',**{f'SE.0{i}':f'mf_se0{i}' for i in range(2,7)}}.get(row['era'])
   if not aid:raise ValueError('unsupported_catalogue_era')
   rows.append(row|dict(archetype_id=aid,floor_height_m=floorheight,geometry_wkb_hex=wkb,geometry_proxy_area_error=error,designed_ventilation_l_s_m2=row['designed_flow_l_s_m2'] if pd.notna(row['designed_flow_l_s_m2']) else None))
  except Exception as exc:failed.append({'building_id':row['building_id'],'reason':str(exc)})
 return pd.DataFrame(rows),failed


def prepare(config,root):
 root=Path(root)
 if root.exists():raise FileExistsError(root)
 root.mkdir(parents=True);atomic_json(root/'config.json',config)
 db=duckdb.connect();linksfile=Path(config['publication'])/'catalogue/accepted_links.parquet'
 links=db.execute('SELECT formular_id,building_id,property_key,building_municipality_code AS municipality,epc_building_category,certificate_to_footprint_count,semantic_compatibility FROM read_parquet(?)',[str(linksfile)]).fetchdf()
 inventory=Counter();links=links.drop_duplicates();one=links[links.certificate_to_footprint_count==1];byfid=defaultdict(set)
 for link in one.itertuples(index=False):byfid[int(link.formular_id)].add((link.building_id,link.property_key,str(link.municipality),link.epc_building_category,link.semantic_compatibility))
 rows={};signatures={};conflicts=set();exclusions=[];rawfiles=set()
 print('National accepted links',len(links),'single-building certificates',len(byfid),flush=True)
 for file,line,raw in pilot._raw_rows(config['epc']):
  try:fid=int(raw['FormularId'])
  except (ValueError,KeyError):continue
  if fid not in byfid:continue
  rawfiles.add(str(file));sig=digest({k:v for k,v in raw.items() if k.startswith(('Egen','Egi','Vent','AtgUtforda'))})
  if fid in signatures:
   if signatures[fid]!=sig:conflicts.add(fid);rows.pop(fid,None)
   continue
  signatures[fid]=sig
  try:
   link=byfid[fid]
   if len(link)!=1:raise ValueError('ambiguous_building_link')
   bid,prop,municipality,linked_category,compatibility=next(iter(link))
   if not prop or pd.isna(prop):raise ValueError('missing_property_key')
   x=parse_record(raw)
   x.update(building_id=bid,property_key=prop,municipality=municipality,linked_epc_category=linked_category,semantic_compatibility=compatibility,certificate_to_footprint_count=1,source_file=str(file),source_line=line,source_row_sha256=digest(raw))
   rows[fid]=x
  except Exception as exc:exclusions.append({'formular_id':fid,'reason':str(exc)})
 for fid in conflicts:exclusions.append({'formular_id':fid,'reason':'conflicting_source_rows'})
 inventory.update(e['reason'] for e in exclusions)
 print('Parsed eligible certificates',len(rows),flush=True)
 frame,duplicates=choose_certificates(list(rows.values()));exclusions.extend(duplicates)
 frame,evidence=enrich(frame,config,root);frame,context_audit=context(frame,config)
 membership=[{'building_id':r['building_id'],**assess_membership(r)} for r in frame.to_dict('records')]
 atomic_json(root/'archetype-membership-audit.json',membership)
 frame['archetype_membership_eligible']=[r['eligible'] for r in membership]
 frame['archetype_membership_status']=[r['status'] for r in membership]
 frame['gis_use_group']=[r['gis_assignment']['use_group'] for r in membership]
 frame['gis_assignment_status']=[r['gis_assignment']['status'] for r in membership]
 frame['assigned_archetype_use']=[r['assigned_archetype_use'] for r in membership]
 frame.to_parquet(root/'preparation-checkpoint.parquet',index=False)
 atomic_json(root/'preparation-checkpoint.json',{'geometry':evidence,'context':context_audit})
 prior,prior_audit=prior_properties(config,links)
 for col in ['era','climate_region','ventilation','roof_form','building_position','age_conflict','renovation_timing']:frame[col]=frame[col].fillna('unknown').astype(str)
 frame['prior_study_property']=frame.property_key.isin(prior);frame['split']=assign_splits(frame,prior)
 targets=frame[['building_id','property_key','split','target']].copy();frame=frame.drop(columns=['target','heating_components','energy_fields'])
 frame.to_parquet(root/'features.parquet',index=False)
 targets[targets.split!='test'].to_parquet(root/'targets-development.parquet',index=False)
 targets[targets.split=='test'].to_parquet(root/'targets-test-sealed.parquet',index=False)
 candidates,failed=simulation_candidates(frame);candidates.to_parquet(root/'simulation-candidates.parquet',index=False)
 atomic_json(root/'simulation-preparation-failures.json',failed)
 atomic_json(root/'exclusions.json',exclusions)
 atomic_json(root/'prior-study-registry.json',prior_audit)
 atomic_json(root/'context-audit.json',context_audit)
 atomic_json(root/'field-dictionary.json',{'official_mapping_sha256':file_sha256(config['dictionary']),'official_mapping':json.loads(Path(config['dictionary']).read_text()),'target_evidence':diagnostic.FORM_EVIDENCE,'predictors':{'compactness':'4πA/P²; dimensionless','volume_proxy_m3':'external footprint × measured eave/representative height; not simulated conditioned volume','designed_flow_l_s_m2':'reported design outdoor flow; already per m²; not infiltration','renovations':'unknown unless explicit completed evidence dated by measurement year','climate_region':'county geographic proxy, not BBR climate zone'},'certificate_choice':'latest verified measurement period; certificate dates absent in supplied newer export; ties excluded','temporal_system_status':'Ventilation operational at measurement year unverified unless dated independent evidence supplied'})
 missing={col:float(frame[col].isna().mean()) for col in frame if col not in ['geometry_wkb_hex','reported_completed_measures']}
 atomic_json(root/'coverage.json',{'accepted_links':len(links),'eligible_buildings':len(frame),'single_building_certificates':len(byfid),'older_or_unread_source_certificates':len(byfid)-len(signatures),'excluded_multibuilding_links':int((links.certificate_to_footprint_count!=1).sum()),'splits':frame.split.value_counts().to_dict(),'properties':frame.property_key.nunique(),'uses':frame.use_group.value_counts().to_dict(),'heating_families':frame.heating_family.value_counts().to_dict(),'missing_fraction':missing,'simulation_candidates':len(candidates),'simulation_candidates_not_yet_compiled':True,'limitations':['No national measurement-year weather descriptors; county proxy used in statistical analysis','Income join limited to archived locations','Stock coverage is eligible newer EPCs, not all Swedish buildings','No calibration until measurement-year system/geometry/boundary evidence is approved']})
 atomic_json(root/'manifest.json',dict(schema_version=1,features_sha256=file_sha256(root/'features.parquet'),development_targets_sha256=file_sha256(root/'targets-development.parquet'),test_targets_sha256=file_sha256(root/'targets-test-sealed.parquet'),source_hashes={str(linksfile):file_sha256(linksfile),**{f:file_sha256(f) for f in rawfiles}},geometry_evidence=evidence,prior_registry_sha256=file_sha256(root/'prior-study-registry.json'),workflow_sources={str(p):file_sha256(p) for p in [*Path(__file__).parent.glob('*.py'),Path(__file__).parents[1]/'archetype_membership.py']}))
 (root/'preparation-checkpoint.parquet').unlink()
 refresh_completed_evidence(root)
 from .evidence import audit_prepared_evidence
 audit_prepared_evidence(root)
 print('Prepared',len(frame),'buildings;',len(candidates),'simulation candidates',flush=True)
 return root


def refresh_completed_evidence(root):
 """Re-audit completed fields and use definitions before fitting any model.

 Only source usage/AtgUtforda fields are examined. Property assignments are
 rebuilt without target values and the separate targets are re-sealed.
 """
 root=Path(root)
 if (root/'regression').exists() or (root/'evaluation-opened.json').exists():raise ValueError('Evidence refresh must precede modelling')
 config=json.loads((root/'config.json').read_text());frame=pd.read_parquet(root/'features.parquet');lookup=frame.set_index('formular_id');ids=set(lookup.index);updates={}
 for _,_,raw in pilot._raw_rows(config['epc']):
  try:fid=int(raw['FormularId'])
  except (ValueError,KeyError):continue
  if fid not in ids or fid in updates:continue
  timing=lookup.loc[fid,'renovation_timing']
  x={key:flag(raw,names) if timing=='during_or_before' else np.nan for key,names in RENOVATION.items()}
  x.update({key:optional(raw,source) for key,source in USE_FIELDS.items()})
  use=lookup.loc[fid,'use_group']
  if use=='mixed_nonresidential':use=next((k.removesuffix('_percent') for k in USE_FIELDS if x[k]==100),use)
  x['use_group']=use;updates[fid]=x
 if len(updates)!=len(frame):raise ValueError('Missing selected certificate during evidence audit')
 updated=frame.copy();audit=pd.DataFrame.from_dict(updates,orient='index')
 for column in audit:updated[column]=updated.formular_id.map(audit[column])
 if 'heating_degree_days' not in updated:updated['heating_degree_days']=np.nan
 # Source attribute provenance is explicit and does not contain energy outcomes.
 evidence=[]
 for row in updated[['registry_year','height_status','geometry_status','designed_flow_l_s_m2','renovation_timing']].itertuples(index=False):
  evidence.append(json.dumps({'construction_year':'registry_reported' if pd.notna(row.registry_year) else 'epc_reported_or_missing','height':'observed_lidar_derived' if row.height_status=='determined' else 'missing','footprint':row.geometry_status,'ventilation_flow':'reported_design_per_m2' if pd.notna(row.designed_flow_l_s_m2) else 'missing','renovation_timing':row.renovation_timing,'operational_system_at_measurement':'unverified','heated_attic_extent':'unverified'},sort_keys=True))
 updated['assignment_evidence']=evidence
 membership=[{'building_id':r['building_id'],**assess_membership(r)} for r in updated.to_dict('records')]
 updated['archetype_membership_eligible']=[r['eligible'] for r in membership]
 updated['archetype_membership_status']=[r['status'] for r in membership]
 updated['gis_use_group']=[r['gis_assignment']['use_group'] for r in membership]
 updated['gis_assignment_status']=[r['gis_assignment']['status'] for r in membership]
 updated['assigned_archetype_use']=[r['assigned_archetype_use'] for r in membership]
 atomic_json(root/'archetype-membership-audit.json',membership)
 # Resolve prior IDs, plus conservatively quarantine matching archived property
 # names when an old certificate is absent from the promoted linkage catalogue.
 import re
 names=set()
 for path in Path(config['archive']).rglob('*.csv'):
  try:
   with path.open(errors='replace',newline='') as f:
    reader=csv.DictReader(f);field=next((k for k in reader.fieldnames or [] if k.lower() in ('idfastbet','fastighet')),None)
    if field:
     for raw in reader:
      if raw.get(field):names.add(re.sub(r'\s+',' ',raw[field].split('>')[0].strip().upper()))
  except (OSError,csv.Error):continue
 prior=set(updated.loc[updated.prior_study_property,'property_key'])
 extra=set(updated.loc[updated.property_key.str.split('|',n=1).str[-1].isin(names),'property_key']);prior.update(extra)
 updated['prior_study_property']=updated.property_key.isin(prior);updated['split']=assign_splits(updated,prior)
 targets=pd.concat([pd.read_parquet(root/'targets-development.parquet'),pd.read_parquet(root/'targets-test-sealed.parquet')],ignore_index=True)
 targets['split']=targets.building_id.map(updated.set_index('building_id').split)
 targets[targets.split!='test'].to_parquet(root/'targets-development.parquet',index=False)
 targets[targets.split=='test'].to_parquet(root/'targets-test-sealed.parquet',index=False)
 updated.to_parquet(root/'features.parquet',index=False)
 candidates=pd.read_parquet(root/'simulation-candidates.parquet')
 if not candidates.empty:
  for col in ['split',*RENOVATION]:candidates[col]=candidates.building_id.map(updated.set_index('building_id')[col])
  candidates.to_parquet(root/'simulation-candidates.parquet',index=False)
 manifest=json.loads((root/'manifest.json').read_text());manifest.update(features_sha256=file_sha256(root/'features.parquet'),development_targets_sha256=file_sha256(root/'targets-development.parquet'),test_targets_sha256=file_sha256(root/'targets-test-sealed.parquet'))
 manifest['completed_evidence_reaudit']={'fields':RENOVATION,'source_sha256':file_sha256(__file__),'records':len(updates),'target_values_used_for_assignment':False};atomic_json(root/'manifest.json',manifest)
 registry=json.loads((root/'prior-study-registry.json').read_text());registry.update(conservative_property_name_matches=len(extra),properties_in_prepared_stock=len(prior));atomic_json(root/'prior-study-registry.json',registry)
 coverage=json.loads((root/'coverage.json').read_text());coverage.update(splits=updated.split.value_counts().to_dict(),uses=updated.use_group.value_counts().to_dict())
 for col in [*RENOVATION,*USE_FIELDS,'heating_degree_days']:coverage['missing_fraction'][col]=float(updated[col].isna().mean())
 atomic_json(root/'coverage.json',coverage)
 print('Completed dictionary/use/prior-property audit',len(updated),flush=True)
