"""Summarize real coordinate coverage without acquiring frames or changing raw data."""
import argparse
from collections import Counter
import json
from pathlib import Path
import statistics

from coordinate_alignment import assess_clock, point_time, valid_frame
from ink_protocol import PEN_TOOLS
from capture_clock_history import load_clock
from multipage_geometry import page_frames


def audit(run, raw):
    frames = [json.loads(line) for line in (run/'geometry.jsonl').read_text(encoding='utf-8').splitlines()]
    clock = assess_clock(load_clock(run/'clock.json'))
    mapped = {p['seq']: p for p in map(json.loads, (run/'points.jsonl').read_text(encoding='utf-8').splitlines())}
    strokes, active = [], {}
    for point in map(json.loads, raw.read_text(encoding='utf-8').splitlines()):
        if point['tool_type'] not in PEN_TOOLS:
            continue
        key = point['window_id'], point['display_id'], point['pointer_id']
        if point['action'] == 1 or key not in active:
            active[key] = []
            strokes.append(active[key])
        active[key].append(point)
        if point['action'] in (0, 3):
            active.pop(key, None)
    stroke_rows = []
    for index, points in enumerate(strokes):
        xs, ys = [[p[axis] for p in points] for axis in ('x', 'y')]
        times = [point_time(p, clock) if clock['usable_candidate'] else None for p in (points[0], points[-1])]
        candidates = [mapped[p['seq']] for p in points if mapped.get(p['seq'], {}).get('status') == 'candidate']
        exposure_points = 0
        for point in points:
            t = point_time(point, clock) if clock['usable_candidate'] else None
            if t is not None and any(isinstance(interval := f['observation'].get('device_capture_wall_interval_ms'), list) and
                                     interval[0]-clock['uncertainty_ms'] <= t <= interval[1]+clock['uncertainty_ms'] for f in frames):
                exposure_points += 1
        stroke_rows.append({'index': index, 'seq_range': [points[0]['seq'], points[-1]['seq']],
                            'wall_time_range_ms_candidate': times, 'points': len(points),
                            'screen_bounds': [min(xs), min(ys), max(xs), max(ys)],
                            'candidate_points': len(candidates),
                            'points_overlapping_measured_capture_interval': exposure_points,
                            'reasons': dict(Counter(mapped.get(p['seq'], {}).get('reason') or mapped.get(p['seq'], {}).get('status') for p in points)),
                            'pdf_bounds_candidate': [min(p['pdf_xy'][0] for p in candidates), min(p['pdf_xy'][1] for p in candidates),
                                                     max(p['pdf_xy'][0] for p in candidates), max(p['pdf_xy'][1] for p in candidates)] if candidates else None})
    frame_rows = []
    for frame in frames:
        o, r = frame['observation'], frame['registration']
        children=page_frames(frame)
        frame_rows.append({'index': frame['index'], 'interval': o.get('device_capture_wall_interval_ms'),
                           'visual_valid': any(c['registration'].get('valid') for c in children),
                           'alignment_valid': any(valid_frame(c) for c in children),
                           'registered_pdf_pages':[c['registration'].get('pdf_page_1based') for c in children if c['registration'].get('valid')],
                           'reason': r.get('reason'), 'layers': o.get('context', {}).get('page_layers'),
                           'pdf_page': r.get('pdf_page_1based'), 'pdf_to_screen': r.get('pdf_to_screen'),
                           'layout_check': r.get('layout_consistency'),
                           'source_ms': frame.get('source_observation_ms'), 'match_ms': frame.get('registration_ms')})
    visual=[f for f,summary in zip(frames,frame_rows) if summary['visual_valid']]
    return {'strokes': stroke_rows, 'frames': frame_rows, 'clock': clock,
            'visual_fit_count':sum(f['visual_valid'] for f in frame_rows),
            'alignment_valid_count':sum(f['alignment_valid'] for f in frame_rows),
            'median_source_ms_visual_valid':statistics.median(f['source_observation_ms'] for f in visual) if visual else None,
            'median_match_ms_visual_valid':statistics.median(f['registration_ms'] for f in visual) if visual else None,
            'physical_mm_accuracy_verified': False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run', type=Path, required=True)
    p.add_argument('--raw', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    report = audit(args.run, args.raw)
    with args.output.open('x', encoding='utf-8') as out:
        json.dump(report, out, ensure_ascii=False, indent=2)
    print(json.dumps({key: value for key, value in report.items() if key != 'frames'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
