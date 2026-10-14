"""Diagnostic comparison of frozen MF archetypes across eras and F/FT/FTX."""
from __future__ import annotations
import json,statistics
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from . import archetype_pilot as p, archetype_diagnostics as d, clean_cohort_test as base
from .campaign import atomic_json,file_sha256
ROOT=Path('output/archetype-exploration-2026-10-02').resolve()
COHORT=Path('output/archetype-exploration-selection-2026-10-02/cohort.json').resolve()


def metrics(entries):
    return dict(n=len(entries),properties=len({e['property_key'] for e in entries}),
        mae_kwh_m2=statistics.mean(e['absolute_error_kwh_m2'] for e in entries),
        bias_kwh_m2=statistics.mean(e['signed_error_kwh_m2'] for e in entries),
        median_ape_percent=statistics.median(e['absolute_percentage_error'] for e in entries),
        below_20_percent=sum(e['absolute_percentage_error']<20 for e in entries),
        below_10_percent=sum(e['absolute_percentage_error']<10 for e in entries))


def main():
    from epsm_core import EnergyPlusRunner
    from epsm_data.detailed import load_detailed_catalogue
    if ROOT.exists():raise FileExistsError(ROOT)
    ROOT.mkdir(parents=True)
    rows=p._read(COHORT)
    weather,weather_audit=base.prepare_weather(rows,ROOT)
    sources=d.package_sources()
    assumptions=base.ASSUMPTIONS|dict(archetype='matched era-specific catalogue, not mf_se05 for all',
        ventilation='observed single F/FT/FTX',ftx_sensible_recovery_primary=.7,
        ftx_sensible_recovery_sensitivity=[.5,.8],ftx_latent_recovery=0,
        selection='input-only screening; all prior study properties excluded',
        early_era='mf_1940 decade representative applied to 1940–1949; not the entire pre-1961 stock',
        weather='measurement-year AMY regenerated from pinned raw caches',
        no_calibration=True)
    atomic_json(ROOT/'audit.json',dict(assumptions=assumptions,weather=weather_audit,
        cohort_sha256=file_sha256(COHORT),selection_audit=p._read(COHORT.parent/'audit.json'),
        package_sources=sources,workflow_hashes={str(Path(m.__file__)):file_sha256(m.__file__) for m in (p,d,base)},
        exploration_source_sha256=file_sha256(__file__)))
    catalogue=load_detailed_catalogue()
    compiler=EnergyPlusRunner(energyplus=base.ENGINE).compiler
    specs={};compile_records=[]
    for row in rows:
        recoveries=(.5,.7,.8) if row['ventilation_system']=='FTX' else (0.,)
        states=tuple(catalogue.archetypes[row['archetype_id']]['states'])
        for state in states:
            for recovery in recoveries:
                key=(row['building_id'],state,recovery)
                record=dict(building_id=key[0],archetype_id=row['archetype_id'],ventilation_system=row['ventilation_system'],state=state,recovery=recovery)
                directory=ROOT/'compiled'/key[0]/state/str(recovery)
                directory.mkdir(parents=True)
                try:
                    spec=base.build_spec(row,state,weather,archetype_id=row['archetype_id'],
                        ventilation_system=row['ventilation_system'],recovery_effectiveness=recovery)
                    model,report=compiler.compile(spec)
                    atomic_json(directory/'spec.json',spec.model_dump(mode='json'))
                    atomic_json(directory/'model.epJSON',model)
                    atomic_json(directory/'compilation-report.json',report.data)
                    record|=dict(status='compiled',spec_sha256=d.digest(spec.model_dump(mode='json')),
                        heated_area_m2=spec.geometry.conditioned_floor_area_m2,
                        conditioned_volume_m3=spec.geometry.conditioned_volume_m3,zone_count=len(model['Zone']))
                    specs[key]=spec
                except Exception as exc:
                    record|=dict(status='failed',reason=str(exc))
                compile_records.append(record)
    atomic_json(ROOT/'compilation.json',compile_records)
    print('Compiled',len(specs),'of',len(compile_records),'cases',flush=True)

    def run(key):
        bid,state,recovery=key
        directory=ROOT/'runs'/bid/state/str(recovery);directory.mkdir(parents=True)
        receipt=dict(building_id=bid,state=state,recovery=recovery)
        try:
            result=EnergyPlusRunner(energyplus=base.ENGINE).run(specs[key],directory/'energyplus')
            receipt|=dict(status='accepted',heat_kwh=float(result.scalars['space_heating_demand_kwh']),
                scalars=result.scalars,diagnostics=result.diagnostics)
        except Exception as exc:
            receipt|=dict(status='failed',reason=str(exc))
        atomic_json(directory/'receipt.json',receipt)
        print(bid[:8],state,recovery,receipt['status'],flush=True)
        return receipt

    # Actual annual smoke runs cover each observed ventilation/roof combination.
    smoke=[];seen=set()
    by_id={r['building_id']:r for r in rows}
    for key in specs:
        row=by_id[key[0]];combo=(row['ventilation_system'],row['roof_form'])
        if combo not in seen:
            seen.add(combo);smoke.append(key)
    results=[run(k) for k in smoke]
    atomic_json(ROOT/'smoke.json',results)
    # Failed smoke groups remain explicit; no warnings are waived. Other models
    # still run because isolated numerical failures do not imply group-wide failure.
    with ThreadPoolExecutor(max_workers=3) as pool:
        results.extend(pool.map(run,[k for k in specs if k not in smoke]))
    atomic_json(ROOT/'results.json',results)
    accepted={(r['building_id'],r['state'],r['recovery']):r for r in results if r['status']=='accepted'}
    entries=[];paired=[]
    for row in rows:
        states=tuple(catalogue.archetypes[row['archetype_id']]['states'])
        primary=.7 if row['ventilation_system']=='FTX' else 0.
        if not all((row['building_id'],s,primary) in accepted for s in states):continue
        paired.append(row)
        recoveries=(.5,.7,.8) if row['ventilation_system']=='FTX' else (0.,)
        for recovery in recoveries:
            if not all((row['building_id'],s,recovery) in accepted for s in states):continue
            for state in states:
                heat=accepted[row['building_id'],state,recovery]['heat_kwh']/row['atemp_m2']
                observed=row['heat_kwh']/row['atemp_m2']
                for loss in (0,5,10):
                    prediction=heat+loss
                    entries.append(dict(building_id=row['building_id'],property_key=row['property_key'],
                        archetype_id=row['archetype_id'],ventilation_system=row['ventilation_system'],state=state,
                        recovery=recovery,loss_kwh_m2=loss,observed_kwh_m2=observed,predicted_kwh_m2=prediction,
                        signed_error_kwh_m2=prediction-observed,absolute_error_kwh_m2=abs(prediction-observed),
                        absolute_percentage_error=100*abs(prediction-observed)/observed))
    grouped=defaultdict(list)
    for e in entries:grouped[e['archetype_id'],e['ventilation_system'],e['state'],e['recovery'],e['loss_kwh_m2']].append(e)
    summary=[dict(archetype_id=k[0],ventilation_system=k[1],state=k[2],recovery=k[3],loss_kwh_m2=k[4],**metrics(v)) for k,v in sorted(grouped.items())]
    atomic_json(ROOT/'per-building.json',entries)
    atomic_json(ROOT/'summary.json',dict(attempted_buildings=len(rows),paired_buildings=len(paired),paired_properties=len({r['property_key'] for r in paired}),
        attempted_configurations=len(compile_records),compiled=len(specs),accepted_runs=len(accepted),
        metrics=summary,package_sources_unchanged=sources==d.package_sources()))
    for e in summary:
        if e['loss_kwh_m2']==5 and e['recovery'] in (0,.7):print(json.dumps(e),flush=True)

if __name__=='__main__':main()
