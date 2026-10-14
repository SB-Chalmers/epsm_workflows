"""Re-read EPC evidence and prepare a development-only cohort."""
from __future__ import annotations
import codecs,csv,json
from pathlib import Path
import numpy as np
import pandas as pd
from shapely import from_wkb
from ..campaign import atomic_json,file_sha256
from ..stock.common import read,digest,features,freeze_files
from ..stock.prepare import parse_record,RENOVATION

AUDITED=['atemp_m2','storeys','heated_basement_floors','apartments','designed_flow_l_s_m2','completed_renovation_year','renovation_timing','reno_window','reno_wall','reno_roof','reno_ventilation','reported_completed_measures']

def load_config(path):
    path=Path(path).resolve();cfg=read(path)
    if cfg['schema_version']!=1:raise ValueError('Unsupported diagnostic configuration')
    for key in ['source_study','output','weather_directory']:cfg[key]=str((path.parent/Path(cfg[key])).resolve())
    cfg['config_path']=str(path);cfg['config_sha256']=file_sha256(path)
    return cfg

def source_snapshot(root):
    root=Path(root)
    names=['features.parquet','targets-development.parquet','targets-test-sealed.parquet','simulation-candidates.parquet','manifest.json','field-dictionary.json','evaluation-opened.json']
    return {name:file_sha256(root/name) for name in names if (root/name).is_file()}

def verify(cfg):
    root=Path(cfg['output']);manifest=read(root/'manifest.json')
    if manifest['config_sha256']!=cfg['config_sha256']:raise ValueError('Configuration changed; use a new study')
    if manifest['parent_files']!=source_snapshot(cfg['source_study']):raise ValueError('Parent study changed')
    for name,sha in manifest['prepared_files'].items():
        if file_sha256(root/name)!=sha:raise ValueError('Prepared evidence changed: '+name)
    for path,sha in manifest['raw_sources'].items():
        if file_sha256(path)!=sha:raise ValueError('EPC source changed: '+path)
    for path,sha in manifest['workflow_sources'].items():
        if file_sha256(path)!=sha:raise ValueError('Diagnostic implementation changed; use a new study')
    from ..archetype_diagnostics import package_sources
    if manifest['package_sources']!=package_sources():raise ValueError('Core/Data/weather sources changed; use a new study')
    return root

def original_records(frame):
    records={}
    for path,part in frame.groupby('source_file',sort=True):
        wanted={int(r.source_line):int(r.formular_id) for r in part.itertuples()}
        with Path(path).open('rb') as stream:encoding='utf-16' if stream.read(4).startswith((codecs.BOM_UTF16_LE,codecs.BOM_UTF16_BE)) else 'latin-1'
        with Path(path).open(encoding=encoding,newline='') as stream:
            for line,raw in enumerate(csv.DictReader(stream,delimiter='|'),2):
                if line not in wanted:continue
                fid=int(raw['FormularId'])
                if fid!=wanted[line]:raise ValueError('EPC source position no longer matches certificate')
                records[fid]=raw
        print('Audited EPC file',Path(path).name,flush=True)
    if len(records)!=frame.formular_id.nunique():raise ValueError('Missing original EPC records')
    return records

def basement_count(value):
    """EPC is a text field; preserve ambiguous counts as unknown."""
    if value is None or str(value).strip()=='':return np.nan
    try:x=float(str(value).strip().replace(',','.'))
    except ValueError:return np.nan
    return x if np.isfinite(x) and x>=0 and x.is_integer() else np.nan

def audited_record(raw):
    count=basement_count(raw.get('EgenAntalKallarplan'))
    clean=raw|{'EgenAntalKallarplan':str(count) if pd.notna(count) else ''}
    parsed=parse_record(clean)
    parsed['heated_basement_floors']=count
    if parsed['storeys'] is not None and (parsed['storeys']<=0 or not float(parsed['storeys']).is_integer()):parsed['storeys']=np.nan
    return parsed

def prepare(cfg):
    root=Path(cfg['output']);root.mkdir(parents=True,exist_ok=False);parent=Path(cfg['source_study'])
    snapshot=source_snapshot(parent);frame=features(parent)
    if not (parent/'evaluation-opened.json').exists():raise ValueError('Parent was not already inspected; do not unseal a new test cohort')
    if read(parent/'evaluation-opened.json')['frozen_sha256']!=digest(freeze_files(parent)):raise ValueError('Frozen parent choices changed')
    mask=np.ones(len(frame),bool)
    for col,value in cfg['filters'].items():mask&=frame[col].eq(value).to_numpy()
    frame=frame[mask].sort_values('building_id').reset_index(drop=True)
    targets=pd.concat([pd.read_parquet(parent/'targets-development.parquet'),pd.read_parquet(parent/'targets-test-sealed.parquet')],ignore_index=True)
    old_targets=targets.set_index('building_id').target
    records=original_records(frame);changes=[];excluded=[];kept=[]
    for row in frame.to_dict('records'):
        raw=records[int(row['formular_id'])]
        try:
            if digest(raw)!=row['source_row_sha256']:raise ValueError('Original EPC source row hash mismatch')
            x=audited_record(raw)
            if x['heating_family']!=cfg['filters']['heating_family'] or x['ventilation']!=cfg['filters']['ventilation']:raise ValueError('Audited system leaves pilot family')
            if not np.isclose(x['target'],old_targets.loc[row['building_id']],rtol=0,atol=1e-9):raise ValueError('Target definition changed during source audit')
            for col in AUDITED:
                old=row.get(col);new=x[col]
                same=(pd.isna(old) and pd.isna(new)) or old==new
                if not same:changes.append({'building_id':row['building_id'],'field':col,'before':str(old),'after':str(new)})
                row[col]=new
            if row.get('geometry_wkb_hex'):
                geom=from_wkb(bytes.fromhex(row['geometry_wkb_hex']))
                if not geom.is_valid or geom.is_empty:raise ValueError('Invalid source footprint')
                row['footprint_area_m2']=float(geom.area);row['footprint_perimeter_m']=float(geom.length)
                row['compactness']=4*np.pi*geom.area/geom.length**2
            row['gross_to_atemp_ratio']=row['footprint_area_m2']*row['storeys']/row['atemp_m2']
            row['atemp_per_apartment']=row['atemp_m2']/row['apartments'] if pd.notna(row['apartments']) and row['apartments']>0 else np.nan
            row['basement_present']=float(row['heated_basement_floors']>0) if pd.notna(row['heated_basement_floors']) else np.nan
            row['historical_system_evidence']='unverified';row['unheated_basement_evidence']='unknown';row['heated_attic_evidence']='unknown';row['exposed_wall_evidence']='reported_position_only'
            kept.append(row)
        except (ValueError,KeyError) as exc:excluded.append({'building_id':row['building_id'],'reason':str(exc)})
    frame=pd.DataFrame(kept)
    if frame.empty:raise ValueError('No audited buildings remain')
    # Old split labels are provenance, not a fresh independent holdout.
    frame=frame.rename(columns={'split':'parent_split'});frame.to_parquet(root/'features.parquet',index=False)
    target=pd.DataFrame({'building_id':frame.building_id,'property_key':frame.property_key,'target':frame.building_id.map(old_targets)})
    target.to_parquet(root/'targets-development.parquet',index=False)
    candidates=pd.read_parquet(parent/'simulation-candidates.parquet')
    candidates=candidates[candidates.building_id.isin(frame.building_id)].copy()
    for col in AUDITED+['gross_to_atemp_ratio','compactness']:
        candidates[col]=candidates.building_id.map(frame.set_index('building_id')[col])
    candidates['designed_ventilation_l_s_m2']=candidates['designed_flow_l_s_m2']
    candidates.to_parquet(root/'simulation-candidates.parquet',index=False)
    dictionary=read(parent/'field-dictionary.json');rows=[r for r in dictionary['official_mapping'] if any(v in ['EgenAntalKallarplan','EgenAntalPlan','EgenProjVentFlode','AtgUtfordaUtfortAr'] for v in r['values'])]
    audit={'buildings':len(frame),'properties':frame.property_key.nunique(),'simulation_candidates':len(candidates),'simulation_properties':candidates.property_key.nunique(),'corrected_values':changes,'excluded':excluded,'definitions':rows,'above_ground_storeys_exclude_basement':True,'basement_field':'number of basement floors heated above 10 C; zero does not establish absence of unheated basement','renovations':'component checkbox plus completion year no later than measurement year; blanks remain unknown','flow':'reported design L/s/m²; not measured flow or infiltration','target':'measured normal-use-adjusted pre-weather-correction district-heating space heat / EPC Atemp','missing':{c:float(frame[c].isna().mean()) for c in AUDITED},'unresolved':['unheated basement presence','heated attic extent','actual measurement-year ventilation','exposed walls','actual window condition/WWR'], 'fresh_holdout':False,'no_energy_trimming':True}
    atomic_json(root/'audit.json',audit);atomic_json(root/'config.json',cfg)
    prepared=['features.parquet','targets-development.parquet','simulation-candidates.parquet','audit.json','config.json']
    from ..archetype_diagnostics import package_sources
    workflow={str(p.resolve()):file_sha256(p) for p in Path(__file__).parent.glob('*.py')}
    for p in [Path(__file__).parent.parent/'clean_cohort_test.py',Path(__file__).parent.parent/'archetype_pilot.py',Path(__file__).parent.parent/'stock/common.py',Path(__file__).parent.parent/'stock/regression.py',Path(__file__).parent.parent/'stock/prepare.py']:
        workflow[str(p.resolve())]=file_sha256(p)
    raw_sources={p:file_sha256(p) for p in frame.source_file.unique()}
    atomic_json(root/'manifest.json',{'config_sha256':cfg['config_sha256'],'parent_files':snapshot,'prepared_files':{n:file_sha256(root/n) for n in prepared},'workflow_sources':workflow,'raw_sources':raw_sources,'package_sources':package_sources(),'all_records_development_only':True})
    atomic_json(root/'prepare-complete.json',{'buildings':len(frame),'properties':frame.property_key.nunique()})
    print('Prepared focused cohort',len(frame),frame.property_key.nunique(),flush=True)
