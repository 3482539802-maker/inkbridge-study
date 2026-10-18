"""Split complete monotonic clock histories at wall/suspend discontinuities.

Only final replay uses these partitions. Each partition must independently pass
the original calibration gates. No interpolation crosses the unsampled gap.
"""
from copy import deepcopy

from coordinate_alignment import assess_clock, number, sample_clock

MAX_EPOCHS = 256


def partition_clock(state):
    if state.get('history_lost') or (state.get('history_merged') and not state.get('history_complete')):
        return None
    anchors = state.get('anchors', [])
    if not 2 <= len(anchors) <= (100000 if state.get('history_merged') else 32):
        return None
    groups=[];current=[];wall_low=wall_high=boot_low=boot_high=None;previous=None
    for a in anchors:
        sample_clock(a) # Validate every source anchor, never discard bad evidence.
        lo,hi=a['monotonic_before_us'],a['monotonic_after_us']
        if hi-lo>1000 or (previous is not None and lo<=previous):return None
        previous=hi
        wall=[a['unix_ms']*1000-hi,a['unix_ms']*1000-lo]
        boot=a['boottime_us']-(lo+hi)/2
        if current and (max(wall_high,wall[1])-min(wall_low,wall[0])>2000 or
                        max(boot_high,boot)-min(boot_low,boot)>2000):
            groups.append(current);current=[]
            if len(groups)>=MAX_EPOCHS:return None
        if current:
            wall_low=min(wall_low,wall[0]);wall_high=max(wall_high,wall[1])
            boot_low=min(boot_low,boot);boot_high=max(boot_high,boot)
        else:wall_low,wall_high=wall;boot_low=boot_high=boot
        current.append(a)
    groups.append(current)
    if len(groups)==1:return None
    observations=state.get('observations',[])
    if len(observations)>(100000 if state.get('history_merged') else 32):return None
    previous=None
    for o in observations:
        t,seq=o.get('action_time_us'),o.get('seq')
        if not number(t) or type(seq) is not int or seq<1:return None
        if previous and (t<=previous[0] or seq<=previous[1]):return None
        sample_clock(o['callback_clock'])
        previous=t,seq
    epochs=[]
    for index,group in enumerate(groups):
        low,high=group[0]['monotonic_before_us'],group[-1]['monotonic_after_us']
        # Use the action's epoch even when callback lands beyond its end: keep
        # that contradictory observation so the original gate rejects it.
        obs=[o for o in observations if low<=o['action_time_us']<=high]
        subset=dict(state,anchors=group,observations=obs)
        assessment=assess_clock(subset)
        epochs.append({'index':index,'start_us':low,'end_us':high,
                       'state':subset,'assessment':assessment})
    return epochs


def epoch_for(epochs, start, end=None):
    end=start if end is None else end
    return next((e for e in epochs if e['start_us']<=start<=end<=e['end_us']),None)


def frames_in_epoch(frames, epoch):
    """Outside/straddling frames remain barriers, never disappear from order."""
    result=deepcopy(frames)
    for frame in result:
        obs=frame.get('observation',{})
        native=obs.get('native_frame')
        eligible=False
        if native:
            start=native.get('timestamp_raw',-1)/1000
            end=native.get('copy_finished_monotonic_us',native.get('callback_monotonic_us',-1))
            eligible=epoch['start_us']<=start<=end<=epoch['end_us']
        # Legacy wall-time frames cannot identify an epoch after wall reversal.
        if not eligible:
            obs.pop('native_frame',None)
            obs.update(valid=False,reason='outside_clock_epoch',
                       device_capture_wall_interval_ms=None,timestamp_used_for_coordinates=False)
    return result


def public_epochs(epochs):
    return [{k:v for k,v in e.items() if k!='state'} for e in epochs]
