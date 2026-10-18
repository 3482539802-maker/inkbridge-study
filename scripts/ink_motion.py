"""Separate pen contacts and finger gestures without treating gestures as page motion."""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path

from ink_protocol import validate_point

PEN_TOOLS = {1, 3, 4, 5}
TOOL_NAMES = {0: 'finger', 1: 'pen', 2: 'rubber', 3: 'brush', 4: 'pencil',
              5: 'airbrush', 6: 'mouse', 7: 'lens'}


def page_motion(before, after):
    """Relative change in UI-exposed *unclipped* rectangles, not a calibrated PDF transform."""
    changes = []
    if (not before.get('document_title') or before.get('document_title') != after.get('document_title')
            or before.get('error') or after.get('error')
            or before.get('window_id') is None or before.get('display_id') is None
            or before.get('window_id') != after.get('window_id')
            or before.get('display_id') != after.get('display_id')):
        return changes
    def unique(context):
        pages = context.get('visible_pages', [])
        counts = Counter(p.get('page_id') for p in pages)
        return {p['page_id']: p for p in pages if p.get('page_id') and counts[p['page_id']] == 1}
    old, new = unique(before), unique(after)
    for page_id in old.keys() & new.keys():
        a, b = old[page_id].get('original_bounds'), new[page_id].get('original_bounds')
        if not all(isinstance(r, list) and len(r) == 4 and
                   all(isinstance(v, (int, float)) and math.isfinite(v) for v in r) and
                   r[2] > r[0] and r[3] > r[1] for r in (a, b)):
            continue
        sx, sy = (b[2]-b[0])/(a[2]-a[0]), (b[3]-b[1])/(a[3]-a[1])
        changes.append({'page_id': page_id, 'before_rect': a, 'after_rect': b,
                        'origin_delta_screen_px': [b[0]-a[0], b[1]-a[1]],
                        'scale_ratio_xy': [sx, sy],
                        'screen_to_screen_candidate': [sx, 0, b[0]-sx*a[0], 0, sy, b[1]-sy*a[1]],
                        'evidence': 'passive_ui_origBounds', 'pdf_mapping_calibrated': False})
    return changes


def analyze_points(events):
    contacts, fingers, episodes, pair_state = {}, {}, {}, {}
    results, gestures, warnings, pairs = [], [], [], []
    tools = Counter()
    clocks = {}

    def close_contact(key, reason):
        c = contacts.pop(key)
        c['end_reason'] = reason
        c['complete'] = c['started_with_down'] and reason == 'up'
        c['observed_span_seconds'] = max(0, c['end_us']-c['start_us']) / 1e6
        c['delta_screen_px'] = [c['end_screen'][i]-c['start_screen'][i] for i in (0, 1)]
        results.append(c)

    def close_episode(stream, reason):
        if stream in episodes:
            e = episodes.pop(stream)
            e['end_reason'] = reason
            e['complete'] = reason == 'all_fingers_up' and e['started_with_down']
            e['kind'] = ('multi_finger_motion_candidate' if e['max_fingers'] > 1
                         else 'single_finger_contact_candidate')
            gestures.append(e)
        pair_state.pop(stream, None)
        fingers.pop(stream, None)

    def reset(session, reason):
        for key in list(contacts):
            if key[0] == session: close_contact(key, reason)
        for stream in list(episodes):
            if stream[0] == session: close_episode(stream, reason)

    for line_no, event in enumerate(events, 1):
        event = validate_point(event)
        session = event.get('session_id') or 'legacy-source'
        seq, now = event['seq'], event['action_time']
        previous = clocks.get(session)
        if previous and (seq != previous[0]+1 or now < previous[1]):
            reset(session, 'sequence_or_clock_discontinuity')
            warnings.append({'line': line_no, 'reason': 'sequence_or_clock_discontinuity'})
        clocks[session] = (seq, now)
        tool, action = event['tool_type'], event['action']
        tools[TOOL_NAMES[tool]] += 1
        stream = (session, event['display_id'], event['window_id'])
        key = stream + (event['pointer_id'],)
        provenance = {'session_id': session, 'display_id': stream[1], 'window_id': stream[2]}
        if action == 0:
            for candidate in list(contacts):
                if candidate[:3] == stream: close_contact(candidate, 'cancel')
            close_episode(stream, 'cancel')
            continue
        if key in contacts and (action == 1 or contacts[key]['tool_type'] != tool):
            close_contact(key, 'pointer_restarted_or_tool_changed')
            # A repeated DOWN/tool switch makes the current gesture unreliable.
            close_episode(stream, 'pointer_restarted_or_tool_changed')
        if key not in contacts:
            contacts[key] = dict(provenance, pointer_id=key[-1], tool_type=tool, tool=TOOL_NAMES[tool],
                                 kind='pen_path' if tool in PEN_TOOLS else 'eraser_path' if tool == 2 else 'other_contact',
                                 first_seq=seq, last_seq=seq, start_us=now, end_us=now,
                                 start_screen=[event['x'], event['y']], end_screen=[event['x'], event['y']],
                                 started_with_down=action == 1, points=0)
        c = contacts[key]
        c.update(last_seq=seq, end_us=now, end_screen=[event['x'], event['y']], points=c['points']+1)
        if tool == 0:
            live = fingers.setdefault(stream, {})
            e = episodes.setdefault(stream, dict(provenance, first_seq=seq, last_seq=seq,
                start_us=now, end_us=now, started_with_down=action == 1, max_fingers=0,
                pen_overlap_observed=False, page_displacement=None))
            e.update(last_seq=seq, end_us=now)
            e['pen_overlap_observed'] |= any(k[:3] == stream and v['tool_type'] in PEN_TOOLS
                                              for k, v in contacts.items())
            if not c['started_with_down']: e['started_with_down'] = False
            live[key[-1]] = (event['x'], event['y'], now)
            e['max_fingers'] = max(e['max_fingers'], len(live))
            # Positions arrive per changed pointer; do not claim simultaneous sampling.
            if len(live) == 2:
                ids = tuple(sorted(live))
                a, b = (live[i] for i in ids)
                distance = math.hypot(b[0]-a[0], b[1]-a[1])
                center = [(a[i]+b[i])/2 for i in (0, 1)]
                angle = math.atan2(b[1]-a[1], b[0]-a[0])
                state = pair_state.get(stream)
                if state is None or state['pointer_ids'] != list(ids):
                    state = dict(provenance, pointer_ids=list(ids), first_seq=seq, last_seq=seq,
                                 start_distance_px=distance, end_distance_px=distance,
                                 start_center_screen=center, end_center_screen=center,
                                 start_angle=angle, end_angle=angle, max_position_age_us=abs(a[2]-b[2]),
                                 evidence='latest_per_pointer_positions', page_transform=None)
                    pairs.append(state)
                    pair_state[stream] = state
                state.update(last_seq=seq, end_distance_px=distance, end_center_screen=center, end_angle=angle,
                             max_position_age_us=max(state['max_position_age_us'], abs(a[2]-b[2])))
                state['finger_distance_ratio'] = distance/state['start_distance_px'] if state['start_distance_px'] > 0 else None
                state['centroid_delta_screen_px'] = [center[i]-state['start_center_screen'][i] for i in (0, 1)]
                state['rotation_radians'] = math.atan2(math.sin(angle-state['start_angle']), math.cos(angle-state['start_angle']))
            else:
                pair_state.pop(stream, None)
            if action == 3:
                live.pop(key[-1], None)
                pair_state.pop(stream, None)
                if not live: close_episode(stream, 'all_fingers_up')
        elif tool in PEN_TOOLS and stream in episodes:
            episodes[stream]['pen_overlap_observed'] = True
        if action == 3: close_contact(key, 'up')
    for key in list(contacts): close_contact(key, 'end_of_observed_file')
    for stream in list(episodes): close_episode(stream, 'end_of_observed_file')
    return {'tool_point_counts': dict(tools), 'contacts': results, 'finger_gestures': gestures,
            'two_finger_intervals': pairs, 'warnings': warnings,
            'limits': ['Finger tool identity does not prove scrolling: taps, palms and UI operations are possible.',
                       'A pen path is input, not proof of a persisted Notes ink object; tools may erase/select.',
                       'Finger displacement and distance ratios are not measured application page transforms.',
                       'Raw event time has not been aligned with PC UI observation time.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('records', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    data = args.records.read_bytes()
    complete = data[:data.rfind(b'\n')+1] if data else b''
    result = analyze_points(json.loads(line) for line in complete.splitlines() if line.strip())
    result.update(source=str(args.records.resolve()), sha256=hashlib.sha256(data).hexdigest(),
                  incomplete_tail_bytes=len(data)-len(complete), data_source='supplied_journal')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'output': str(args.output), 'tools': result['tool_point_counts'],
                      'contacts': len(result['contacts']), 'gestures': len(result['finger_gestures']),
                      'two_finger_intervals': len(result['two_finger_intervals'])}))


if __name__ == '__main__':
    main()
