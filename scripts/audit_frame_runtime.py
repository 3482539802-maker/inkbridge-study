"""Bounded numeric runtime diagnostics; no frames requested or images read."""
import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import statistics


def quantiles(values):
    if not values:return {'count':0}
    values=sorted(values)
    return {'count':len(values),'median':statistics.median(values),'p95':values[round((len(values)-1)*.95)],
            'min':values[0],'max':values[-1]}


def audit(directory,limit=256):
    if type(limit) is not int or not 1<=limit<=1024:raise ValueError('Bounded frame sample required')
    directory=Path(directory)
    with closing(sqlite3.connect((directory/'pipeline.sqlite').resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
        db.execute('BEGIN')
        rows=[json.loads(p) for p, in db.execute('SELECT payload FROM geometry ORDER BY id DESC LIMIT ?',(limit,))][::-1]
    stages={};reasons=Counter();previous=None;spans=[];rate_results=Counter();counters=[]
    transports=Counter();fallbacks=Counter();acquisition_modes=Counter()
    motion=Counter();motion_reasons=Counter();anchor_checks=Counter();chain_checks=Counter()
    exposure_checks=Counter()
    for row in rows:
        observation=row.get('observation',{});frame=observation.get('native_frame',{})
        exposure=row.get('motion_exposure_validation')
        if exposure is not None:
            exposure_checks['observations']+=1
            exposure_checks['independently_fitted_frames']+=bool(exposure.get('checked'))
            exposure_checks[exposure.get('reason') or 'unspecified']+=1
            if type(exposure.get('processing_ms')) in (int,float):
                stages.setdefault('page_motion.exposure_validation_ms',[]).append(exposure['processing_ms'])
        hint=observation.get('page_motion')
        if hint is not None:
            motion['observations']+=1
            motion['observations_with_candidate']+=bool(hint.get('candidate'))
            if hint.get('reason'):motion_reasons[hint['reason']]+=1
            if isinstance(hint.get('processing_ms'),(int,float)):
                stages.setdefault('page_motion.processing_ms',[]).append(hint['processing_ms'])
            for page in hint.get('pages',[]):
                motion['page_candidates']+=bool(page.get('candidate'))
                if page.get('reason'):motion_reasons[page['reason']]+=1
        check=row.get('page_motion_anchor_check',{})
        for page in check.get('pages',[]):
            anchor_checks['checked_pages']+=1
            anchor_checks['agreeing_pages']+=bool(page.get('agrees_with_independent_fits'))
            stages.setdefault('page_motion.anchor_disagreement_screen_px',[]).append(page['max_anchor_disagreement_screen_px'])
        chain=row.get('page_motion_chain_check',{})
        for page in chain.get('pages',[]):
            chain_checks['checked_pages']+=1
            chain_checks['checks_across_missing_fit']+=chain.get('steps',0)>1
            chain_checks['agreeing_pages']+=bool(page.get('agrees_with_independent_fits'))
            stages.setdefault('page_motion.chain_disagreement_screen_px',[]).append(page['max_anchor_disagreement_screen_px'])
        acquisition_modes[observation.get('acquisition_mode','serial_or_legacy')]+=1
        reasons[observation.get('reason') or row.get('registration',{}).get('reason') or 'valid']+=1
        if observation.get('native_frame'):transports[observation.get('frame_transport','hdc_file')]+=1
        if observation.get('frame_socket_fallback_reason'):fallbacks[observation['frame_socket_fallback_reason']]+=1
        for field in ('source_observation_ms','registration_queue_ms','registration_ms','processing_ms'):
            if isinstance(row.get(field),(int,float)):stages.setdefault(field,[]).append(row[field])
        for key,value in observation.get('source_stage_ms',{}).items():stages.setdefault('source.'+key,[]).append(value)
        for key,value in observation.get('context_after_profile',{}).items():
            if type(value) in (int,float):stages.setdefault('context.'+key,[]).append(value)
        for key in ('frame_transfer_attempts','frame_transfer_total_ms','frame_decode_ms','frame_socket_attempt_ms',
                    'context_preflight_age_ms','context_bracket_ms'):
            if type(observation.get(key)) in (int,float):stages.setdefault(key,[]).append(observation[key])
        if frame:
            result=frame.get('max_video_fps_result')
            if type(result) is int:rate_results[str(result)]+=1
            for name,a,b in [('native_copy','copy_finished_monotonic_us','callback_monotonic_us'),
                             ('native_encode_and_worker','encoded_monotonic_us','copy_finished_monotonic_us')]:
                if a in frame and b in frame:stages.setdefault(name,[]).append((frame[a]-frame[b])/1000)
            if 'jpeg_bytes' in frame:stages.setdefault('jpeg_bytes',[]).append(frame['jpeg_bytes'])
            stamp=frame.get('callback_monotonic_us')
            if stamp is not None:
                if previous is not None:spans.append((stamp-previous)/1000)
                previous=stamp
                count=frame.get('video_callback_count')
                if type(count) is int and count>0:counters.append((stamp,count))
    stream=None
    if len(counters)>1 and all(b[0]>a[0] and b[1]>a[1] for a,b in zip(counters,counters[1:])):
        seconds=(counters[-1][0]-counters[0][0])/1e6
        stream={'span_seconds':seconds,'delivered_video_callbacks':counters[-1][1]-counters[0][1],
                'mean_delivered_callbacks_per_second':(counters[-1][1]-counters[0][1])/seconds,
                'scope':'Delivered video callbacks between two numeric snapshots; not display refresh or physical exposure.'}
    return {'schema':1,'frame_rows':len(rows),'frame_indices':[r['index'] for r in rows],
        'limit':limit,'reasons':dict(reasons),'metrics':{k:quantiles(v) for k,v in stages.items()},
        'requested_frame_callback_intervals_ms':quantiles(spans),
        'max_video_fps_api_result_counts':dict(rate_results),'delivered_stream_observation':stream,
        'frame_transports':dict(transports),'socket_fallbacks':dict(fallbacks),
        'acquisition_modes':dict(acquisition_modes),
        'page_motion_evidence':{'counts':dict(motion),'reasons':dict(motion_reasons),'anchor_checks':dict(anchor_checks),
            'chain_checks':dict(chain_checks),
            'exposure_validation':dict(exposure_checks),
            'coordinate_mapping_allowed':False,'physical_accuracy_verified':False},
        'screen_capture_stream_fps_verified':False,
        'interval_scope':'Only explicitly requested frames; cannot measure underlying stream fps.',
        'physical_accuracy_verified':False,'screenshots_acquired':0,'tablet_actions':[]}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();result=audit(args.directory)
    with args.output.open('x',encoding='utf-8') as f:json.dump(result,f,indent=2)
    print(json.dumps({k:v for k,v in result.items() if k!='frame_indices'},indent=2))


if __name__=='__main__':main()
