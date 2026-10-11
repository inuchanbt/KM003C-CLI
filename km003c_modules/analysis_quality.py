"""Evidence-based sweep quality. Policy mirrors PowerChecker analysis-quality.js.

No timestamp alignment, interpolation, ADC fallback, or target-error validity gate.
"""
import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

POLICY = dict(version='pd-quality/v1', min_samples=6, min_span_us=200000,
              max_gap_us=100000, tail_us=400000, hold_us=80000,
              std_floor_v=.02, std_fraction=.001, drift_floor_v=.05,
              drift_fraction=.002, pp_floor_v=.1, pp_fraction=.005,
              load_floor_a=.05, load_fraction=.05)
RESETS = {'SOFT_RESET', 'HARD_RESET', 'CONNECT', 'DISCONNECT', 'PR_SWAP', 'FR_SWAP'}
REQUESTS = {'REQUEST', 'EPR_REQUEST'}
def finite(v):
    return isinstance(v, (float, int)) and math.isfinite(v)
def mean(v):
    return statistics.mean(v) if v else None
def clean(v):
    if isinstance(v, float) and not math.isfinite(v): return None
    return v
def resolution(scope):
    dt = [b['t']-a['t'] for a,b in zip(scope,scope[1:]) if b['t']>a['t']]
    return dict(pd_timestamp_resolution_us=1000, scope_interval_median_us=statistics.median(dt) if dt else None,
                scope_interval_max_us=max(dt) if dt else None, settling_precision='sample_bracket_no_interpolation')

def diagnostics(pd):
    rows=sorted(pd,key=lambda r:r['t']); out=[]; previous=None; previous_index=-1
    for i,r in enumerate(rows):
        if r['message'] in RESETS:
            out.append(dict(event=r['message'], row_index=r.get('row_index'), sno=r.get('sno'), timestamp_us=r['t'], sender=r.get('role') or 'unknown',cause_classification='indeterminate',cause_evidence='Reset sender identifies recovery initiator, not root cause.')); previous=None; continue
        if r['message'] not in REQUESTS: continue
        j=i+1
        while j<len(rows) and rows[j]['message'] not in RESETS|REQUESTS: j+=1
        events=rows[i+1:j]
        def find(names): return next((e for e in events if e['message'] in names and e.get('role')=='SRC'),None)
        accept=find({'ACCEPT'}); ps=find({'PS_RDY'}); bad=find({'REJECT','WAIT','NOT_SUPPORTED'})
        reset=rows[j] if j<len(rows) and rows[j]['message'] in RESETS else None
        crc=next((e for e in events if e['message']=='GOODCRC' and e.get('role')!=r.get('role') and finite(r.get('message_id')) and e.get('message_id')==r.get('message_id')),None)
        rdo=json.dumps(r.get('objects',[]),separators=(',',':'))
        duplicate=previous and previous['request_message']==r['message'] and previous['sender']==r.get('role') and previous['rdo']==rdo and r['t']-previous['timestamp_us']<=50000
        kind=('same_id_retry_or_duplicate' if r.get('message_id')==previous['message_id'] else 'new_id_same_rdo') if duplicate else 'none'
        conflict=previous and previous['sender']==r.get('role') and finite(r.get('message_id')) and r['message_id']==previous['message_id'] and previous['rdo']!=rdo and r['t']-previous['timestamp_us']<=50000 and all(e.get('role')!=r.get('role') or e['message']=='GOODCRC' for e in rows[previous_index+1:i])
        if conflict:kind='same_id_changed_rdo'
        confirmed=not reset and not bad and accept and ps and accept['t']>r['t'] and ps['t']>accept['t']
        d=dict(event='request',row_index=r.get('row_index'),sno=r.get('sno'),timestamp_us=r['t'],sender=r.get('role') or 'unknown',request_message=r['message'],message_id=r.get('message_id'),rdo=rdo,duplicate_classification=kind,previous_request_sno=previous['sno'] if duplicate else None,previous_goodcrc_sno=previous['goodcrc_sno'] if duplicate else None,goodcrc_sno=crc.get('sno') if crc else None,goodcrc_status='observed' if crc else 'unobserved',accept_sno=accept.get('sno') if accept else None,ps_rdy_sno=ps.get('sno') if ps else None,accept_us=accept['t'] if accept else None,ps_rdy_us=ps['t'] if ps else None,boundary_us=rows[j]['t'] if j<len(rows) else None,reset_sno=reset.get('sno') if reset else None,reset_sender=reset.get('role') if reset else None,protocol_status='confirmed' if confirmed else 'failed' if bad or reset else 'unconfirmed',protocol_reason='accept_then_ps_rdy' if confirmed else bad['message'].lower() if bad else 'interrupted_by_'+reset['message'].lower() if reset else 'ps_rdy_without_accept' if ps else 'missing_accept_or_ps_rdy',cause_classification='SRC' if bad or ps and not accept else 'indeterminate',cause_evidence='Source explicitly responded '+bad['message'] if bad else 'PS_RDY observed without prior ACCEPT; incomplete capture remains possible.' if ps and not accept else 'Capture does not establish root cause.')
        if conflict:d.update(cause_classification=r.get('role') or 'indeterminate',cause_evidence='Consecutive requests changed payload without advancing Message ID; no intervening sender message or reset observed.')
        out.append(d);previous=d;previous_index=i
    return out

def qualify(p,d,scope):
    why=[]
    p.update(protocol_status=d['protocol_status'] if d else 'unconfirmed',settling_status='unconfirmed',steady_status='unconfirmed',invalid_reasons='',cause_classification=d['cause_classification'] if d else 'indeterminate',soft_reset_sender=d.get('reset_sender') if d else None,pd_event_snos=';'.join(str(d[k]) for k in ['sno','goodcrc_sno','accept_sno','ps_rdy_sno','reset_sno'] if d and d.get(k) is not None),raw_row_number=p.get('first_row_number'),steady_samples=0,steady_voltage_mean=None,steady_voltage_std=None,steady_voltage_drift_v=None,steady_current_mean=None,steady_start_us=None,steady_end_us=None,scope_first_index=None,scope_last_index=None,settling_lower_ms=None,settling_upper_ms=None)
    if p['protocol_status']!='confirmed': why.append('protocol_'+p['protocol_status']+':'+(d['protocol_reason'] if d else p.get('pd_reason') or 'no_matching_pd'))
    if d and finite(d['ps_rdy_us']) and p['protocol_status']=='confirmed':
        samples=[dict(s,index=i) for i,s in enumerate(scope) if s['t']>=d['ps_rdy_us'] and (d['boundary_us'] is None or s['t']<d['boundary_us']) and finite(s['v'])]
        start=0
        for i in range(1,len(samples)):
            if samples[i]['t']-samples[i-1]['t']>POLICY['max_gap_us'] or samples[i]['t']<=samples[i-1]['t']:start=i
        tail=[s for s in samples[start:] if s['t']>=(samples[-1]['t'] if samples else 0)-POLICY['tail_us']]
        v=[s['v'] for s in tail];avg=mean(v);n=len(v);span=tail[-1]['t']-tail[0]['t'] if n else 0
        sd=statistics.stdev(v) if n>1 else None;k=max(2,int(n*.2)) if n>=4 else 0;early=mean(v[:k]) if k else None;late=mean(v[-k:]) if k else None;drift=late-early if k else None;pp=max(v)-min(v) if n else None
        p.update(steady_samples=n,steady_voltage_mean=avg,steady_voltage_std=sd,steady_voltage_min=min(v) if n else None,steady_voltage_max=max(v) if n else None,steady_voltage_early_mean=early,steady_voltage_late_mean=late,steady_voltage_drift_v=drift,steady_voltage_window_samples=k,steady_current_mean=mean([s['a'] for s in tail if finite(s.get('a'))]),steady_start_us=tail[0]['t'] if n else None,steady_end_us=tail[-1]['t'] if n else None,scope_first_index=tail[0]['index'] if n else None,scope_last_index=tail[-1]['index'] if n else None,steady_reason='final_contiguous_400ms_tail',steady_span_us=span,earlier_gap_segments=start>0)
        target=p['target_voltage_v']
        if n<POLICY['min_samples'] or span<POLICY['min_span_us']:why.append('insufficient_steady_samples' if n<POLICY['min_samples'] else 'insufficient_steady_duration')
        elif sd>max(.02,target*.001) or abs(drift)>max(.05,target*.002) or pp>max(.1,target*.005):p.update(steady_status='failed',settling_status='failed');why.append('unstable_steady_window')
        else:
            p['steady_status']='stable';band=max(.02,abs(avg)*.005);at=None
            for i in range(len(samples)-1,-1,-1):
                if abs(samples[i]['v']-avg)>band or i<len(samples)-1 and samples[i+1]['t']-samples[i]['t']>100000:at=i+1;break
                at=i
            if at is not None and at<len(samples) and samples[-1]['t']-samples[at]['t']>=80000:p.update(settling_status='settled',settling_lower_ms=((samples[at-1]['t'] if at else d['ps_rdy_us'])-d['timestamp_us'])/1000,settling_upper_ms=(samples[at]['t']-d['timestamp_us'])/1000)
            else:why.append('transition_completion_unconfirmed')
    else:why.append('native_steady_window_unconfirmed')
    if p.get('sampling_schema') and p.get('measurement_status') and p['measurement_status'] not in ('ok','stability_unchecked'):why.append('recorded_sampling:'+p['measurement_status'])
    p['invalid_reasons']=';'.join(dict.fromkeys(why));p['point_valid']=p['protocol_status']=='confirmed' and p['settling_status']=='settled' and p['steady_status']=='stable' and not why
    return p

PAIR_KEYS=['run_started_at','mode','pdo_kind','pdo_raw_u32','pdo_object_number','target_voltage_v','source_name','source_manufacturer','source_model','source_port','cable_name','cable_type','cable_length_m','emarker_claim','meter_chain','test_note','cycle']
def pairs(summary):
    groups=defaultdict(list)
    for p in summary:
        leg=str(p.get('sweep_leg') or '').lower();num=p.get('sweep_pass')
        if leg not in ('outbound','return') or p.get('sweep_axis')!='voltage' or not finite(num) or int(num)!=num or num<1 or num%2!=(1 if leg=='outbound' else 0):continue
        q=dict(p,cycle=math.ceil(num/2));groups[tuple(q.get(k) for k in PAIR_KEYS)].append(q)
    out=[]
    for g in groups.values():
        legs=[[p for p in g if p['sweep_leg']==leg] for leg in ('outbound','return')]
        if not all(legs):continue
        a,b=[v[0] for v in legs];r={k:g[0].get(k) for k in PAIR_KEYS};why=[]
        if any(len(v)!=1 for v in legs):why.append('missing_or_ambiguous_leg')
        for leg,p in [('outbound',a),('return',b)]:
            if not p.get('point_valid'):why.append(leg+':'+(p.get('invalid_reasons') or 'quality_unconfirmed'))
        for k in ['request_current_a','target_load_current_a']:
            if a.get(k)!=b.get(k):why.append('incomparable_'+k)
        ia,ib=a.get('steady_current_mean'),b.get('steady_current_mean')
        if not finite(ia) or not finite(ib):why.append('load_unconfirmed')
        elif abs(abs(ia)-abs(ib))>max(.05,.05*max(abs(ia),abs(ib))):why.append('incomparable_measured_load')
        valid=not why and finite(a.get('steady_voltage_mean')) and finite(b.get('steady_voltage_mean'));diff=b['steady_voltage_mean']-a['steady_voltage_mean'] if valid else None
        r.update(request_current_a=a.get('request_current_a'),target_load_current_a=a.get('target_load_current_a'),sweep_target=r['target_voltage_v'],hysteresis_source='device_steady' if valid else 'excluded',outbound_mean_v=a.get('steady_voltage_mean'),return_mean_v=b.get('steady_voltage_mean'),outbound_samples=a.get('steady_samples',0),return_samples=b.get('steady_samples',0),hysteresis_v=diff,hysteresis_mV=diff*1000 if diff is not None else None,pair_valid=bool(valid),invalid_reasons=';'.join(why),outbound_row=a.get('raw_row_number'),return_row=b.get('raw_row_number'),outbound_pd_events=a.get('pd_event_snos',''),return_pd_events=b.get('pd_event_snos',''));out.append(r)
    return out
def counts(rows):return dict(total=len(rows),valid=sum(bool(r['pair_valid']) for r in rows),excluded=sum(not r['pair_valid'] for r in rows))
def regulation(points):
    out=[]
    for a,b in zip(points,points[1:]):
        if not a.get('point_valid') or not b.get('point_valid') or any(a.get(k)!=b.get(k) for k in ['run_started_at','pdo_kind','pdo_raw_u32','pdo_object_number','target_voltage_v','source_name','source_port']):continue
        ia,ib=a.get('steady_current_mean'),b.get('steady_current_mean')
        if not finite(ia) or not finite(ib):continue
        di=abs(ib)-abs(ia)
        if abs(di)<.05:continue
        dv=b['steady_voltage_mean']-a['steady_voltage_mean'];out.append(dict(target_voltage_v=a['target_voltage_v'],from_row=a.get('raw_row_number'),to_row=b.get('raw_row_number'),delta_v=dv,delta_load_a=di,delta_v_per_a=dv/di,equivalent_output_resistance_ohm=-dv/di,interpretation='Measured at meter; includes cable and path resistance.'))
    return out

def load_windows(points,scope,diags):
    out=[]
    for p in points:
        d=next((d for d in diags if d['event']=='request' and d['timestamp_us']==p.get('request_start_us')),None)
        if not d or d['protocol_status']!='confirmed':continue
        samples=[s for s in scope if s['t']>=d['ps_rdy_us'] and (d['boundary_us'] is None or s['t']<d['boundary_us']) and finite(s.get('a')) and finite(s['v'])];segments=[];segment=[]
        for s in samples:
            if segment and (s['t']-segment[-1]['t']>100000 or abs(abs(s['a'])-abs(segment[0]['a']))>max(.05,abs(segment[0]['a'])*.05)):segments.append(segment);segment=[]
            segment.append(s)
        if segment:segments.append(segment)
        windows=[qualify(dict(p),dict(d,ps_rdy_us=ss[0]['t'],boundary_us=ss[-1]['t']+1),ss) for ss in segments]
        for a,b in zip(windows,windows[1:]):
            for r in regulation([a,b]):out.append(dict(r,from_scope_us=a['steady_start_us'],to_scope_us=b['steady_end_us'],interval_source='within_request_load_step'))
    return out

def read_evidence(pd_path,scope_path):
    from .transition_analysis import SyncPDSample, decode_programmable_requests
    pd=[]; samples=[]; fingerprint=''
    with Path(pd_path).open(encoding='utf-8-sig',newline='') as f:
        for index,r in enumerate(csv.DictReader(f)):
            if not fingerprint:fingerprint=str(r['Start Time'])+':'+r['Data']
            if r['SOP']!='SOP':continue
            words=[int(w,16) for w in r['Data'].split()];h=words[0] if words else None
            objects=words[1:] if h is not None and not h&0x8000 else []
            row=dict(row_index=index,sno=int(r['Sno']),message=r['Message'],t=int(r['Start Time']),role=r.get('Power Role') or None,message_id=(h>>9)&7 if h is not None else None,objects=objects)
            pd.append(row);samples.append(SyncPDSample(index,row['sno'],row['message'],row['t'],int(r['End Time']),None,b''.join(w.to_bytes(4,'little') for w in objects)))
    scope=[]
    if scope_path:
        with Path(scope_path).open(encoding='utf-8-sig',newline='') as f:
            for r in csv.DictReader(f):
                if not r.get('Timestamp Raw'):raise ValueError('Native device timestamps are required for scope association')
                s=dict(t=int(r['Timestamp(us)']),v=float(r['Vbus(V)']),a=float(r['Ibus(A)']) if r.get('Ibus(A)') else None)
                if not finite(s['v']) or s['a'] is not None and not finite(s['a']):raise ValueError('Nonfinite scope measurement')
                scope.append(s)
    decoded,_=decode_programmable_requests(samples);diags=diagnostics(pd);byrow={d['row_index']:d for d in diags if d['event']=='request'};steps=[];segment=0
    for i,r in enumerate(pd):
        if r['message'] in RESETS or r['message'] in REQUESTS and i not in decoded:segment+=1
        if i not in decoded or r.get('role')!='SNK':continue
        q=decoded[i];d=dict(byrow[r['row_index']],target=q.voltage_V,current=q.current_A,kind=q.supply_type,index=q.object_position,pdo=q.selected_pdo,segment=segment)
        prev=steps[-1] if steps else None
        if prev and d['timestamp_us']-prev['timestamp_us']<=50000 and prev['boundary_us']==d['timestamp_us'] and prev['accept_us'] is None and prev['ps_rdy_us'] is None and prev['rdo']==d['rdo'] and prev['kind']==d['kind']:steps[-1]=d
        else:steps.append(d)
    return dict(pd=pd,scope=sorted(scope,key=lambda s:s['t']),steps=steps,diagnostics=diags,fingerprint=fingerprint)

def integrate(summary,pd_path=None,scope_path=None):
    rows=[{k:clean(v) for k,v in r.items()} for r in summary.to_dict('records')]
    evidence=read_evidence(pd_path,scope_path) if pd_path else dict(pd=[],scope=[],steps=[],diagnostics=[],fingerprint='')
    steps=evidence['steps'];method='unmatched';mapped=[None]*len(rows)
    def matches(p,d):
        kind=str(p.get('pdo_kind') or '').lower().replace(' apdo','').replace('_',' ')
        actual=d['kind'].lower().replace('_',' ')
        raw=p.get('pdo_raw_u32');raw=int(raw,0) if isinstance(raw,str) else raw
        return (actual=='spr pps' if kind=='pps' else kind==actual) and p.get('pdo_object_number')==d['index'] and finite(p.get('request_current_a')) and abs(p['request_current_a']-d['current'])<1e-6 and abs(p['target_voltage_v']-d['target'])<1e-6 and (raw is None or raw==d['pdo'])
    bounded=any(p.get('pd_recording_key') for p in rows)
    if bounded:
        method='recording_mismatch'
        if all(p.get('pd_recording_key')==evidence['fingerprint'] and finite(p.get('pd_row_begin')) and finite(p.get('pd_row_end')) and p['pd_row_end']>p['pd_row_begin']>=0 for p in rows):
            method='recorded_bounds'
            for i,p in enumerate(rows):
                found=[d for d in steps if p['pd_row_begin']<=d['row_index']<p['pd_row_end'] and matches(p,d)]
                if len(found)==1:mapped[i]=found[0]
    elif len(rows)>=3 and len({p['target_voltage_v'] for p in rows})>=2 and len({p.get('run_started_at') for p in rows})<=1:
        starts=[i for i in range(len(steps)-len(rows)+1) if all(steps[i+j]['segment']==steps[i]['segment'] and matches(p,steps[i+j]) for j,p in enumerate(rows))]
        if len(starts)==1:method='unique_sequence';mapped=steps[starts[0]:starts[0]+len(rows)]
        elif len(starts)>1:method='ambiguous_sequence'
    for p,d in zip(rows,mapped):
        p.update(pd_association=method,pd_reason=d['protocol_reason'] if d else method,request_sno=d['sno'] if d else None,request_start_us=d['timestamp_us'] if d else None,accept_ms=(d['accept_us']-d['timestamp_us'])/1000 if d and d['accept_us'] is not None else None,ps_rdy_ms=(d['ps_rdy_us']-d['timestamp_us'])/1000 if d and d['ps_rdy_us'] is not None else None)
        qualify(p,d,evidence['scope'])
    hyst=pairs(rows)
    return dict(summary=rows,association=method,hysteresis=hyst,hysteresisCounts=counts(hyst),hysteresisHighVoltageCounts=counts([p for p in hyst if p['target_voltage_v']>=29]),protocolDiagnostics=evidence['diagnostics'],loadRegulation=regulation(rows)+load_windows(rows,evidence['scope'],evidence['diagnostics']),timeResolution=resolution(evidence['scope']),qualityPolicy=POLICY)

def report(result,lang='ja'):
    en=lang=='en';lines=[]
    for key,label in [('hysteresisCounts','Hysteresis' if en else 'ヒステリシス'),('hysteresisHighVoltageCounts','Hysteresis at ≥29 V' if en else '29V以上のヒステリシス')]:
        c=result[key];lines.append(f"{label}: {c['valid']} valid, {c['excluded']} excluded of {c['total']} pairs" if en else f"{label}: {c['total']}組中{c['valid']}組有効、{c['excluded']}組除外")
    lines.append(('PD association: ' if en else 'PDとの対応付け: ')+result['association'])
    t=result['timeResolution'];lines.append(f"PD timestamp resolution: 1 ms; VBUS median/max intervals: {t['scope_interval_median_us']}/{t['scope_interval_max_us']} µs")
    lines.append('Settling uses sample brackets without interpolation; target deviation does not invalidate stable load drop.' if en else '整定時間は実測サンプル間の範囲で表示し、補間しません。目標電圧偏差は独立評価し、安定した負荷電圧降下だけでは無効にしません。')
    lines.append('Quality: final contiguous 400 ms tail; ≥6 samples, ≥200 ms; gaps ≤100 ms; σ≤max(20 mV,0.1%), drift≤max(50 mV,0.2%), P-P≤max(100 mV,0.5%).')
    lines.append('Soft Reset sender is the recovery initiator, not proof of root cause. SRC classifications describe explicit refusal/observed order; otherwise indeterminate.')
    valid=[p['hysteresis_mV'] for p in result['hysteresis'] if p['pair_valid']];lines.append(f"Valid-pair hysteresis mean/min/max mV: {mean(valid)}/{min(valid) if valid else None}/{max(valid) if valid else None}")
    for d in result['protocolDiagnostics']:
        if d['event']!='request' or d['duplicate_classification']!='none' or d['protocol_status']=='failed':lines.append(f"{d['event']} Sno={d['sno']} sender={d['sender']}; ID={d.get('message_id')}; {d.get('duplicate_classification','')}; GoodCRC={d.get('goodcrc_status')}; previous ACK Sno={d.get('previous_goodcrc_sno')}; Reset={d.get('reset_sender')}; {d['cause_classification']} ({d['cause_evidence']})")
    lines.append(f"Load-regulation intervals: {len(result['loadRegulation'])}")
    for r in result['loadRegulation']:lines.append(f"{r['target_voltage_v']} V: ΔV={r['delta_v']} V, ΔI={r['delta_load_a']} A, ΔV/ΔI={r['delta_v_per_a']} V/A, Rout={r['equivalent_output_resistance_ohm']} Ω (includes cable/path)")
    if not result['loadRegulation']:lines.append('No valid steady load changes at the same voltage in the same series; output resistance unavailable.')
    for p in result['hysteresis']:
        if not p['pair_valid']:lines.append(f"Excluded {p['target_voltage_v']} V: {p['invalid_reasons']}; raw rows={p['outbound_row']}/{p['return_row']}; PD Sno={p['outbound_pd_events']}/{p['return_pd_events']}")
    return '\n'.join(lines)+'\n'

def write_outputs(result,prefix,plots=True):
    def write(name,rows):
        path=Path(str(prefix)+'_'+name+'.csv');keys=list(dict.fromkeys(k for r in rows for k in r)) or ['status']
        with path.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=keys);w.writeheader();w.writerows(rows)
    write('hysteresis',result['hysteresis']);write('pd_checks',result['summary']);write('protocol_diagnostics',result['protocolDiagnostics']);write('load_regulation',result['loadRegulation'])
    Path(str(prefix)+'_analysis-settings.json').write_text(json.dumps(dict(quality=result['qualityPolicy'],time_resolution=result['timeResolution']),ensure_ascii=False,indent=2),encoding='utf-8')
    if plots and result['hysteresis']:
        import matplotlib.pyplot as plt
        fig,ax=plt.subplots();valid=[p for p in result['hysteresis'] if p['pair_valid']];bad=[p for p in result['hysteresis'] if not p['pair_valid']]
        ax.plot([p['target_voltage_v'] for p in valid],[p['hysteresis_mV'] for p in valid],'o-',label='Valid pairs')
        # Axes coordinates keep excluded markers out of voltage statistics and Y scaling.
        ax.scatter([p['target_voltage_v'] for p in bad],[.03]*len(bad),transform=ax.get_xaxis_transform(),marker='x',color='red',label='Excluded (no difference)')
        ax.set(xlabel='Requested voltage (V)',ylabel='Hysteresis (mV)');ax.legend();fig.tight_layout();fig.savefig(str(prefix)+'_hysteresis.png');plt.close(fig)
