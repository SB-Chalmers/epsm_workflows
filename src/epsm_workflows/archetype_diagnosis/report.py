"""Plain-language diagnostic findings, never labelled untouched accuracy."""
from pathlib import Path
import pandas as pd
from ..stock.common import read


def report(cfg):
    root=Path(cfg['output']);out=root/'report';out.mkdir(exist_ok=True)
    audit=read(root/'audit.json');reg=read(root/'regression/metrics.json');decision=read(root/'regression/decision.json');physics=read(root/'physics/complete.json')
    lines=['# Can building evidence justify better archetypes?','',
           '## What we tested','',
           f"The focused cohort contains **{audit['buildings']:,} buildings on {audit['properties']:,} properties**: 1961–1975 multifamily buildings, district heating, F ventilation, Västra Götaland. All results are development evidence because these source data were previously inspected. No reshuffled split is called an untouched test.",'',
           f"Original EPC evidence was re-read. **{len(audit['corrected_values'])} field values changed** and **{len(audit['excluded'])} records were excluded**. Storeys are floors above ground; basement counts mean floors heated above 10°C. Zero does not establish absence of an unheated basement. Design airflow remains L/s/m², never additional infiltration. Completed component measures are accepted only if dated by the energy measurement year.",'',
           '## Do observable groups improve predictions?','',
           '| Development prediction | MAE kWh/m² | Below 20% | Below 10% | Bias kWh/m² |','|---|---:|---:|---:|---:|']
    for label,title in [('baseline','Typical cohort heating'),('pooled','One attribute regression'),('subgroup_policy','Allow one observable subdivision')]:
        m=reg[label]['metrics'];lines.append(f"| {title} | {m['mae']:.2f} | {100*m['below_20']:.1f}% | {100*m['below_10']:.1f}% | {m['bias']:.2f} |")
    lines+=['','These are five outer property-fold predictions. Ridge penalties and rule choices used five inner property folds. Heating outcomes, errors and neighbourhood context cannot assign a building to a branch. Missing rule inputs receive the pooled prediction.', '']
    if decision['retained']:
        lines += [f"**One provisional subdivision passed the development gates:** `{decision['retained_rule']}`. Assignment stability median ARI was {decision['median_assignment_ari']:.2f}. This supports a candidate distinction for further physical investigation, not a validated catalogue change."]
    else:
        lines += ['**No subdivision earned retention.** The pooled parent remains the assignment. Creating more archetypes is not supported by this experiment.']
    lo,hi=decision['paired_mae_difference_interval'];lines+=[f"The subdivision-selection policy changed MAE by a relative {100*decision['development_gain']:.1f}%; its property-bootstrap paired MAE difference interval was {lo:.2f} to {hi:.2f} kWh/m². Full rule trials and selection failures are preserved in regression/.",'',
        '## What happened in individual physical models?','',
        f"Selected **{physics['buildings']} Gothenburg buildings on {physics['properties']} different properties** using physical inputs and measurement year, without heating errors. There were {physics['planned_cases']} planned cases; **{physics['accepted']} were accepted**, and {physics['failures_or_unattempted']} failed or were stopped. Status: `{physics['status']}`. Failed cases cannot support heating conclusions.",'',
        'The common scenarios compare frozen catalogue states, WWR 0.2 versus 0.3, existing-state window U-value 2.2 W/m²K, and their combination. Infiltration, other constructions, ventilation, gains and Kiva remain fixed. Results below use the declared 5 kWh/m² net delivery loss.','',
        '| State | Common change | Paired buildings | MAE before → after | Improved | Worsened | Below 20% before → after | Below 10% before → after |','|---|---|---:|---:|---:|---:|---:|---:|']
    paired=read(root/'physics/paired-comparisons.json')
    for p in paired:
        if p['loss_kwh_m2']!=5:continue
        b=p['baseline'];a=p['scenario_metrics'];lines.append(f"| {p['state']} | {p['scenario']} | {p['paired']} | {b['mae']:.1f} → {a['mae']:.1f} | {p['improved']} | {p['worsened']} | {100*b['below_20']:.0f}% → {100*a['below_20']:.0f}% | {100*b['below_10']:.0f}% → {100*a['below_10']:.0f}% |")
    lines+=['','Each row uses only accepted baseline/scenario pairs. Detailed heating changes and signed/percentage errors for each building are in physics/per-building.parquet; paired-comparisons.json includes 0/5/10 loss sensitivities and property-bootstrap intervals. No scenario is selected separately for a building.', '',
            '## What remains uncertain?','',
            '| Information | Missing in focused cohort |','|---|---:|']
    for c in ['designed_flow_l_s_m2','reno_window','reno_wall','reno_roof','reno_ventilation','apartments']:
        lines.append(f"| {c} | {100*audit['missing'][c]:.1f}% |")
    lines+=['','Unheated basement presence, heated attic extent, actual ventilation in the measurement year, exposed walls and actual window condition remain unresolved. Core geometry retains the source outline, storeys, measured height and areas. Atemp agreement is not forced. Assumed WWR, roof pitch, cold attic and soil properties are sensitivities or assumptions, not measurements.', '',
            '## What should happen next?','',
            '**No final archetype calibration is authorized by this run.** The screened regional subset still fails the original minimum 120-building and independent-property/evidence requirements. Existing statistical outcomes have been inspected. A genuine confirmation requires new untouched properties plus established building/weather/energy boundaries.', '',
            'If a retained physical distinction survives those checks, use its central development building to explain the archetype and several independent properties to calibrate shared bounded settings. If it does not, retain the pooled archetype and collect the missing information rather than fitting each building separately.', '',
            'Saved evidence includes source and configuration hashes, raw-field audit, corrections/exclusions, nested folds and predictions, model choices, assignments/representatives, all compilation reports, simulation diagnostics, failed cases, conditioned area/volume, delivery-loss sensitivities and per-building comparisons. No catalogue changes or valid-tail trimming were performed.','']
    path=out/'report.md';path.write_text('\n'.join(lines))
    # Direct area comparisons and input-stratified paired diagnostic summaries.
    comp=read(root/'physics/compilation.json');cohort=pd.read_parquet(root/'physics/cohort.parquet')
    area=pd.DataFrame([{k:r.get(k) for k in ['building_id','state','scenario','atemp_m2','conditioned_area_m2','conditioned_volume_m3','zone_count','status']} for r in comp])
    area['conditioned_to_epc_ratio']=area.conditioned_area_m2/area.atemp_m2;area.to_parquet(out/'area-volume-audit.parquet',index=False)
    if (root/'physics/per-building.parquet').exists():
        frame=pd.read_parquet(root/'physics/per-building.parquet').merge(cohort[['building_id','roof_form','height_m','measurement_year']],on='building_id',validate='many_to_one')
        frame['atemp_band']=pd.cut(frame.atemp_m2,[0,1000,3000,float('inf')]).astype(str)
        frame['height_band']=pd.cut(frame.height_m,[0,10,20,float('inf')]).astype(str)
        from ..stock.common import error_metrics
        from ..campaign import atomic_json
        groups=[]
        for field in ['roof_form','atemp_band','height_band','measurement_year']:
            for key,g in frame.groupby(['state','scenario','loss_kwh_m2',field],dropna=False):
                groups.append({'state':key[0],'scenario':key[1],'loss_kwh_m2':int(key[2]),'attribute':field,'value':str(key[3]),**error_metrics(g.target,g.prediction)})
        atomic_json(out/'physical-subgroups.json',groups)
    return path
