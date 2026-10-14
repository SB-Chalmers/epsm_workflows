"""Screen multiple Gothenburg archetype cohorts using source attributes, never energy residuals."""
from __future__ import annotations
import json,sqlite3
from collections import defaultdict,Counter
from pathlib import Path
import duckdb
from . import archetype_pilot as p, archetype_diagnostics as d
from .campaign import atomic_json,file_sha256

ROOT=Path('output/archetype-exploration-selection-2026-10-02').resolve()
PUBLICATION=Path('../epc_join/outputs/releases/2.0.0').resolve()
EPC=Path('../epc_join/data/epc').resolve()
HEIGHT=Path('../footprint_height_extraction/outputs/sweden/harmonized/1.0.0').resolve()
SELECTION=Path('../footprint_height_extraction/outputs/sweden/inventory/selections/gothenburg.buildings.parquet').resolve()
REGISTRY=Path('../building_age/outputs/national/registry.sqlite').resolve()


def main():
    if ROOT.exists():raise FileExistsError(ROOT)
    ROOT.mkdir(parents=True)
    linksfile=PUBLICATION/'catalogue/accepted_links.parquet'
    with duckdb.connect() as db:
        links=db.execute("SELECT formular_id,building_id,property_key,epc_subject_id,epc_building_category FROM read_parquet(?) WHERE building_municipality_code='1480' AND certificate_to_footprint_count=1 AND building_to_certificate_count=1 AND epc_building_category='FLERBOSTADSHUS'",[str(linksfile)]).fetchall()
    linked=defaultdict(list)
    for fid,*fields in links:linked[int(fid)].append(tuple(fields))
    exclusions=[];candidates={};signatures={};conflicts=set();rawfiles=set()
    print('Linked multifamily certificates:',len(linked),flush=True)
    for file,line,raw in p._raw_rows(EPC):
        try:fid=int(raw['FormularId'])
        except (ValueError,KeyError):continue
        if fid not in linked:continue
        rawfiles.add(str(file))
        signature=d.digest({k:v for k,v in raw.items() if k.startswith(('Egen','Egi','Vent','AtgUtforda'))})
        if fid in signatures:
            if signatures[fid]!=signature:conflicts.add(fid);candidates.pop(fid,None)
            continue
        signatures[fid]=signature
        try:
            bid,property_key,subject,category=p._singleton_link(linked[fid])
            vent_fields={'VentTypSjalvdrag':'S','VentTypF':'F','VentTypFT':'FT','VentTypFTX':'FTX','VentTypFmed':'Fmed'}
            vents=[v for k,v in vent_fields.items() if raw.get(k)=='Ja']
            if len(vents)!=1 or vents[0] not in ('F','FT','FTX'):raise ValueError('ventilation_unsupported_or_multiple')
            vent=vents[0]
            observation=p._observation(raw,year_bounds=(1940,2025),ventilation_system=vent)
            # Require explicit use and basement reports, not absent fields.
            if p.number(raw,'EgenAtempBostad')!=100:raise ValueError('not_100_percent_residential')
            if int(raw['EgenAntalKallarplan'])!=0:raise ValueError('reported_heated_basement')
            if raw.get('EgenByggnadsTyp')!='Friliggande':raise ValueError('not_reported_detached')
            if (p.number(raw,'EgenAvarmgarage',required=False) or 0)>0:raise ValueError('heated_garage')
            flow=p.number(raw,'EgenProjVentFlode',required=False)
            if flow is not None and not .2<=flow<=1:raise ValueError('implausible_designed_flow')
            if observation['storeys']<2:raise ValueError('fewer_than_two_storeys')
            fields=d.parse_fields(raw,year_bounds=(1940,2025),ventilation_system=vent)
            candidates[fid]=dict(building_id=bid,property_key=property_key,epc_subject_id=subject,epc_category=category,
                formular_id=fid,source_file=str(file),source_line=line,source_row_sha256=d.digest(raw),**observation,
                **{k:v for k,v in fields.items() if k not in observation},residential_percent=100,ventilation_system=vent,
                building_position='Friliggande',designed_ventilation_l_s_m2=flow,
                reported_completed_measures={k:v for k,v in raw.items() if k.startswith('AtgUtforda') and v},
                apartments=p.number(raw,'EgenAntalBolgh',required=False),staircases=p.number(raw,'EgenAntalTrapphus',required=False))
        except (ValueError,TypeError,KeyError) as e:
            exclusions.append(dict(formular_id=fid,reason=str(e)))
    for fid in conflicts:exclusions.append(dict(formular_id=fid,reason='conflicting_raw_rows'))
    print('Attribute-screened candidates:',len(candidates),flush=True)
    atomic_json(ROOT/'attribute_candidates.json',list(candidates.values()))
    ids=[r['building_id'] for r in candidates.values()]
    buildingsfile=PUBLICATION/'building_results/kommunkod=1480/part.parquet'
    with duckdb.connect() as db:
        db.execute('CREATE TEMP TABLE ids(building_id VARCHAR)')
        db.executemany('INSERT INTO ids VALUES (?)',[(x,) for x in ids])
        buildings=db.execute('SELECT b.building_id,b.geometry,b.building_area_m2,b.insamlingslage,b.andamal1,b.kommunkod FROM read_parquet(?) b JOIN ids USING(building_id)',[str(buildingsfile)]).fetchall()
        tiles=db.execute('SELECT DISTINCT owner_tile_id FROM read_parquet(?) JOIN ids USING(building_id)',[str(SELECTION)]).fetchall()
        heightfiles=[str(HEIGHT/f'results/buildings/owner_tile_id={t}/part.parquet') for (t,) in tiles if (HEIGHT/f'results/buildings/owner_tile_id={t}/part.parquet').is_file()]
        heights=db.execute('SELECT h.building_id,h.height_status,h.representative_height_m,h.roof_form,h.eave_height_m,h.ridge_height_m FROM read_parquet(?,hive_partitioning=false,union_by_name=true) h JOIN ids USING(building_id)',[heightfiles]).fetchall()
    bybuilding={x[0]:x[1:] for x in buildings}
    byheight={x[0]:dict(zip(('height_status','representative_height_m','roof_form','eave_height_m','ridge_height_m'),x[1:])) for x in heights}
    with sqlite3.connect(f'file:{REGISTRY}?mode=ro',uri=True) as db:
        years={}
        for i in range(0,len(ids),500):
            batch=ids[i:i+500]
            years.update(db.execute('SELECT building_id,construction_year FROM registry_buildings WHERE building_id IN ('+','.join('?' for _ in batch)+')',batch))
    oldsource=ROOT.parent/'archetype-diagnostics-mf-se02-2026-10-01-reviewed/cohort.json'
    previous_new=ROOT.parent/'clean-cohort-gothenburg-mf-2026-10-02-v3/eligible.json'
    previous_new_rows=[r for r in p._read(previous_new) if r['archetype_id']=='mf_se05' and not r['previously_examined_property']]
    seenproperties={r['property_key'] for r in p._read(oldsource)+previous_new_rows}
    eras={'mf_1940':(1940,1949),'mf_1950':(1950,1960),'mf_se02':(1961,1975),'mf_se03':(1976,1985),'mf_se04':(1986,1995),'mf_se05':(1996,2005),'mf_se06':(2006,2025)}
    eligible=[]
    for row in candidates.values():
        bid=row['building_id']
        try:
            geom,area,outline,purpose,municipality=bybuilding[bid]
            era=next(k for k,(lo,hi) in eras.items() if lo<=row['epc_year']<=hi)
            lo,hi=eras[era]
            if years.get(bid) is not None and not lo<=years[bid]<=hi:raise ValueError('registry_year_conflicts_with_era')
            if row['epc_year']>row['measurement_year']:raise ValueError('construction_after_measurement_year')
            roof,floorheight=p._geometry(byheight.get(bid),row['storeys'])
            ratio=area*row['storeys']/row['atemp_m2']
            if not .95<=ratio<=1.20:raise ValueError('gross_to_atemp_ratio_outside_095_120')
            if outline not in ('Fasad','Takkant'):raise ValueError('unsupported_outline')
            wkb,proxy_error=p._model_footprint(geom,roof)
            r=row|byheight[bid]|dict(archetype_id=era,registry_year=years.get(bid),construction_year=years.get(bid) or row['epc_year'],
                roof_form=roof,floor_height_m=floorheight,footprint_area_m2=area,outline=outline,purpose=purpose,
                municipality=municipality,geometry_wkb_hex=wkb,geometry_proxy_area_error=proxy_error,
                gross_to_atemp_ratio=ratio,previously_examined_property=row['property_key'] in seenproperties)
            eligible.append(r)
        except (ValueError,KeyError) as e:exclusions.append(dict(building_id=bid,formular_id=row['formular_id'],epc_year=row['epc_year'],reason=str(e)))
    fresh=[r for r in eligible if not r['previously_examined_property']]
    groups=defaultdict(list)
    for row in fresh:groups[(row['archetype_id'],row['ventilation_system'])].append(row)
    group_counts={f'{era}/{vent}':dict(eligible=sum(r['archetype_id']==era and r['ventilation_system']==vent for r in eligible),fresh=len(group),fresh_properties=len({r['property_key'] for r in group})) for (era,vent),group in groups.items()}
    # Input-stratified diagnostic selection; never read heat or residuals here.
    selected=[]
    for key,group in sorted(groups.items()):
        for row in d.select_diagnostics(group,count=6):
            selected.append(row|dict(study_role='diagnostic',amy_weather_already_pinned=2017<=row['measurement_year']<=2024))
    atomic_json(ROOT/'eligible.json',eligible);atomic_json(ROOT/'cohort.json',selected);atomic_json(ROOT/'exclusions.json',exclusions)
    summary=dict(group_counts=group_counts,eligible=len(eligible),fresh=len(fresh),selected=len(selected),properties=len({r['property_key'] for r in selected}),
        existing_properties_excluded=len(eligible)-len(fresh),all_property_keys_disjoint_from_prior_study=not bool({r['property_key'] for r in selected}&seenproperties),
        selection_uses_heating_intensity=False,calibration_permitted=False,
        limitations=['No heated basement reported does not rule out unheated basements','Detached label and attic heating unverified','FTX recovery must be declared as an assumption'],
        filters='Gothenburg; 1940–2025 catalogue-era cohorts; single F/FT/FTX; one-to-one newer measured district heat EPC; pure residential; zero heated basement floors; detached; no heated garage; plausible measured height and roof; gross/Atemp 0.95–1.20; up to six buildings per era and ventilation group')
    atomic_json(ROOT/'summary.json',summary)
    atomic_json(ROOT/'audit.json',dict(publication=str(PUBLICATION),height_release=str(HEIGHT),registry=str(REGISTRY),
        source_hashes={str(f):file_sha256(f) for f in [linksfile,buildingsfile,SELECTION,oldsource,previous_new,Path(__file__),Path(p.__file__),Path(d.__file__),*[Path(x) for x in sorted(rawfiles)],*[Path(x) for x in heightfiles]]},
        registry_query_sha256=d.digest(sorted(years.items())),prior_properties_sha256=d.digest(sorted(seenproperties)),package_sources=d.package_sources()))
    print(json.dumps(summary,indent=2),flush=True)

if __name__=='__main__':main()
