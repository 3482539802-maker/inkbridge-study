"""Cross-check visual PDF registration against calibrated page-layout evidence.

This is an offline contradiction detector, not a page-coordinate provider.
It learns a robust, page-specific relation from multiple distinct observed poses;
it never interprets an arbitrary UI rectangle as the PDF content rectangle.
"""
from __future__ import annotations
import argparse
from collections import Counter
from copy import deepcopy
import json
import math
from pathlib import Path
from statistics import median


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def matrix(value):
    return (isinstance(value, list) and len(value) == 2 and
            all(isinstance(row, list) and len(row) == 3 and all(map(finite, row)) for row in value))


def describe(frame):
    o, r = frame.get('observation', {}), frame.get('registration', {})
    if not o.get('valid') or not r.get('valid') or not o.get('window', {}).get('focused'):
        raise ValueError('invalid_visual_observation')
    c = o.get('context', {})
    pages = c.get('page_layers', [])
    if not c.get('active_tab_marker_unique') or not c.get('document_title_candidate') or len(pages) != 1:
        raise ValueError('ambiguous_document_or_page')
    page = pages[0]
    rect = page.get('unclipped_window_bounds')
    if (not isinstance(rect, list) or len(rect) != 4 or not all(map(finite, rect)) or
            rect[2] <= rect[0] or rect[3] <= rect[1]):
        raise ValueError('invalid_page_rectangle')
    m, inv = r.get('pdf_to_screen'), r.get('screen_to_pdf')
    hull = r.get('support_hull_screen_px')
    if not matrix(m) or not matrix(inv) or not isinstance(hull, list) or not 3 <= len(hull) <= 512:
        raise ValueError('invalid_matrix_or_support')
    if any(not isinstance(p, list) or len(p) != 2 or not all(map(finite, p)) for p in hull):
        raise ValueError('invalid_support')
    if any(math.dist(apply(m, apply(inv, p)), p) > .1 for p in hull):
        raise ValueError('inconsistent_forward_inverse_matrices')
    size = r.get('screen_size')
    if not isinstance(size, list) or len(size) != 2 or not all(finite(v) and v > 0 for v in size):
        raise ValueError('invalid_screen_size')
    if (not page.get('page_id') or not isinstance(r.get('source_pdf_sha256'), str) or
            len(r['source_pdf_sha256']) != 64 or type(r.get('pdf_page_1based')) is not int or r['pdf_page_1based'] < 1):
        raise ValueError('missing_source_identity')
    # Node IDs and PIDs can change across passive sessions. Such normalization
    # remains diagnostic only until window/display conversion is device-verified.
    identity = [c['document_title_candidate'], page['page_id'], r['source_pdf_sha256'],
                r['pdf_page_1based'], o['window'].get('display_id'), size]
    w, h = rect[2]-rect[0], rect[3]-rect[1]
    normalized = [[m[0][0]/w, m[0][1]/w, (m[0][2]-rect[0])/w],
                  [m[1][0]/h, m[1][1]/h, (m[1][2]-rect[1])/h]]
    return dict(identity=identity, rect=rect, normalized=normalized, inverse=inv, hull=hull)


def apply(m, p):
    return [row[0]*p[0]+row[1]*p[1]+row[2] for row in m]


def disagreement(normalized, sample):
    rect = sample['rect']
    w, h = rect[2]-rect[0], rect[3]-rect[1]
    errors = []
    for p in sample['hull']:
        pdf = apply(sample['inverse'], p)
        q = apply(normalized, pdf)
        errors.append(math.dist(p, [rect[0]+w*q[0], rect[1]+h*q[1]]))
    return max(errors)


def consensus(samples, tolerance):
    # Equal weight per distinct page rectangle; repeated screenshots of one
    # stationary page cannot overwhelm other scales/positions or validate it.
    choices = []
    for sample in samples:
        n = sample['normalized']
        errors = [disagreement(n, other) for other in samples]
        good = [i for i, error in enumerate(errors) if error <= tolerance]
        choices.append((len(good), -median(errors[i] for i in good), n, good))
    _, _, n, good = max(choices, key=lambda x: x[:2])
    return n, good


def fit(frames, tolerance_px=4.):
    if not finite(tolerance_px) or not 0 < tolerance_px <= 10:
        raise ValueError('tolerance must be in (0, 10] screen pixels')
    if not 1 <= len(frames) <= 3600:
        raise ValueError('use 1..3600 bounded observations')
    groups = {}
    for frame in frames:
        try:
            sample = describe(frame)
        except (ValueError, KeyError, TypeError):
            continue
        key = json.dumps(sample['identity'])
        group = groups.setdefault(key, {'identity': sample['identity'], 'poses': {}})
        pose = tuple(sample['rect'])
        # Keep one actual frame per pose, selected by independent visual residual.
        error = frame['registration'].get('heldout_median_error_screen_px', math.inf)
        if not finite(error) or error > 2:
            continue
        sample['residual'] = error
        if pose not in group['poses'] or error < group['poses'][pose]['residual']:
            group['poses'][pose] = sample
        if len(groups) > 64 or len(group['poses']) > 64:
            raise ValueError('use at most 64 identities and 64 distinct poses per identity')
    models = []
    for group in groups.values():
        samples = list(group['poses'].values())
        if len(samples) < 4:
            continue
        normalized, indices = consensus(samples, tolerance_px)
        good = [samples[i] for i in indices]
        if len(good) < 4:
            continue
        widths = [s['rect'][2]-s['rect'][0] for s in good]
        if max(widths)/min(widths) < 1.05:
            continue
        heldout = []
        for i, sample in enumerate(good):
            rest = good[:i]+good[i+1:]
            candidate, _ = consensus(rest, tolerance_px)
            heldout.append(disagreement(candidate, sample))
        if max(heldout) > tolerance_px:
            continue
        models.append({'identity': group['identity'], 'normalized_candidate': normalized,
                       'calibration_poses': len(good), 'rejected_poses': len(samples)-len(good),
                       'calibration_rectangles': [s['rect'] for s in good],
                       'width_range': [min(widths), max(widths)],
                       'pose_holdout_max_disagreement_screen_px': max(heldout),
                       'tolerance_screen_px': tolerance_px,
                       'native_page_mapping_verified': False, 'point_mapping_allowed': False})
    return {'schema': 1, 'role': 'offline_layout_visual_contradiction_detection', 'models': models,
            'pixel_to_mm_verified': False, 'pen_accuracy_verified': False}


def load_calibration(path):
    path = Path(path)
    if path.stat().st_size > 128*1024:
        raise ValueError('layout calibration exceeds 128 KiB')
    value = json.loads(path.read_text(encoding='utf-8'))
    if (value.get('schema') != 1 or value.get('role') != 'offline_layout_visual_contradiction_detection' or
            not isinstance(value.get('models'), list) or len(value['models']) > 64):
        raise ValueError('unsupported layout calibration')
    for model in value['models']:
        if (not isinstance(model.get('identity'), list) or len(model['identity']) != 6 or
                not matrix(model.get('normalized_candidate')) or
                not finite(model.get('tolerance_screen_px')) or not 0 < model['tolerance_screen_px'] <= 10 or
                type(model.get('calibration_poses')) is not int or model['calibration_poses'] < 4 or
                model.get('point_mapping_allowed') is not False):
            raise ValueError('invalid diagnostic layout model')
    return value


def check(frame, calibration):
    try:
        sample = describe(frame)
    except (ValueError, KeyError, TypeError) as exc:
        return {'status': 'unresolved', 'reason': str(exc), 'point_mapping_allowed': False}
    models = [m for m in calibration.get('models', []) if m['identity'] == sample['identity']]
    if len(models) != 1:
        return {'status': 'unresolved', 'reason': 'no_unique_cross_pose_calibration', 'point_mapping_allowed': False}
    model = models[0]
    # Contradictions can be detected at other scales; no mapping extrapolation
    # or confidence in the correctness of the window coordinate frame follows.
    error = disagreement(model['normalized_candidate'], sample)
    return {'status': 'consistent_candidate' if error <= model['tolerance_screen_px'] else 'contradiction',
            'reason': None if error <= model['tolerance_screen_px'] else 'visual_transform_disagrees_with_calibrated_layout',
            'max_disagreement_screen_px': error, 'tolerance_screen_px': model['tolerance_screen_px'],
            'point_mapping_allowed': False, 'pen_accuracy_verified': False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--geometry', type=Path, nargs='+', required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    frames, sources = [], []
    for path in args.geometry:
        if path.stat().st_size > 12*1024*1024:
            p.error('geometry file too large')
        for line, text in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            if len(frames) >= 3600:
                p.error('at most 3600 observations total')
            frames.append(json.loads(text))
            sources.append({'path': str(path.resolve()), 'line': line})
    args.output.mkdir(parents=True, exist_ok=False)
    calibration = fit(frames)
    (args.output/'calibration.json').write_text(json.dumps(calibration, indent=2), encoding='utf-8')
    counts, flagged = Counter(), []
    with (args.output/'checked-geometry.jsonl').open('x', encoding='utf-8') as out:
        for frame, source in zip(frames, sources):
            result = check(frame, calibration)
            counts[result['status']] += 1
            copy = deepcopy(frame)
            copy['layout_consistency_check'] = result
            if result['status'] == 'contradiction':
                flagged.append({**source, **result})
                copy['registration'] = {**copy['registration'], 'valid': False,
                                        'reason': result['reason']}
            out.write(json.dumps(copy, allow_nan=False)+'\n')
    summary = {'counts': dict(counts), 'flagged': flagged, 'model_count': len(calibration['models']),
               'not_live_pen_accuracy_proof': True, 'no_coordinates_inferred_from_layout': True}
    (args.output/'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
