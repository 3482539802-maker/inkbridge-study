"""Independent controlled-stroke comparison without fitting handwriting transforms.

Correspondence is supplied explicitly for this experiment. PDF placement in the
native page uses a documented contain/centre hypothesis, cross-checked against
the exported thumbnail's original-PDF features. No tablet frames are retained.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import zipfile

import cv2
import fitz
import numpy as np

from coordinate_alignment import affine
from ink_protocol import PEN_TOOLS
from visual_registration import PDFRegistration


def distance_to_polyline(points, path):
    points, path = np.asarray(points, float), np.asarray(path, float)
    a, v = path[:-1], np.diff(path, axis=0)
    length = (v*v).sum(axis=1)
    projection = np.clip(((points[:,None,:]-a)*v).sum(axis=2)/np.maximum(length, 1e-15), 0, 1)
    return np.linalg.norm(points[:,None,:]-(a+projection[:,:,None]*v), axis=2).min(axis=1)


def native_to_pdf(points, native_size, pdf_size):
    scale = min(native_size[i]/pdf_size[i] for i in (0, 1))
    offset = [(native_size[i]-pdf_size[i]*scale)/2 for i in (0,1)]
    return [[(p[i]-offset[i])/scale for i in (0,1)] for p in points]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run', type=Path, required=True)
    p.add_argument('--raw', type=Path, required=True)
    p.add_argument('--native-pen', type=Path, required=True)
    p.add_argument('--hinote', type=Path, required=True)
    p.add_argument('--pdf', type=Path, required=True)
    p.add_argument('--stroke-map', nargs='+', required=True, help='Explicit PDFpage:nativeStrokeIndex in raw stroke order')
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    native = json.loads(args.native_pen.read_bytes())
    mapping = [tuple(map(int, value.split(':'))) for value in args.stroke_map]
    pages = {page['background_index']+1: page for page in native['pages'] if type(page['background_index']) is int}
    raw_strokes = []
    for point in map(json.loads, args.raw.read_text(encoding='utf-8').splitlines()):
        if point['tool_type'] not in PEN_TOOLS or point['action'] == 0:
            continue
        if point['action'] == 1:
            raw_strokes.append([])
        if not raw_strokes:
            raise ValueError('Raw stroke did not start with DOWN')
        raw_strokes[-1].append(point)
    if len(raw_strokes) != len(mapping):
        raise ValueError('Explicit stroke count differs from raw')
    results = {r['seq']:r for r in map(json.loads, (args.run/'points.jsonl').read_text(encoding='utf-8').splitlines())}
    frames = {r['index']:r for r in map(json.loads, (args.run/'geometry.jsonl').read_text(encoding='utf-8').splitlines())}
    args.output.mkdir(parents=True, exist_ok=False)
    report = {'correspondence': 'explicit_controlled_six_stroke_order', 'handwriting_transform_fitted': False,
              'placement_hypothesis': 'native_page_1000_width_full_thumbnail_scale_then_independent_PDF_registration',
              'physical_mm_accuracy_verified': False, 'thumbnail_checks': [], 'strokes': []}
    thumbnail_transforms = {}
    with fitz.open(args.pdf) as pdf, zipfile.ZipFile(args.hinote) as archive:
        for number in sorted(set(v[0] for v in mapping)):
            page = pages[number]
            image = cv2.imdecode(np.frombuffer(archive.read(page['thumbnail_entry']),np.uint8),cv2.IMREAD_COLOR)
            matcher = PDFRegistration(args.pdf, [number-1])
            try:
                fit = matcher.match(image)
            finally:
                matcher.close()
            pw, ph = pdf[number-1].rect.width, pdf[number-1].rect.height
            # Native coordinates -> exported full-page thumbnail; not the live screen.
            sx, sy = image.shape[1]/page['native_size_candidate'][0], image.shape[0]/page['native_size_candidate'][1]
            disagreement = None
            if fit.get('valid'):
                thumbnail_transforms[number] = (fit['screen_to_pdf'], sx, sy)
                errors = []
                for xy in fit['support_hull_screen_px']:
                    source = affine(fit['screen_to_pdf'], *xy)
                    scale = min(page['native_size_candidate'][0]/pw, page['native_size_candidate'][1]/ph)
                    expected = [((page['native_size_candidate'][0]-pw*scale)/2+source[0]*scale)*sx,
                                ((page['native_size_candidate'][1]-ph*scale)/2+source[1]*scale)*sy]
                    errors.append(float(np.linalg.norm(np.array(expected)-xy)))
                disagreement = max(errors)
            report['thumbnail_checks'].append({'pdf_page': number, 'thumbnail_size': [image.shape[1],image.shape[0]],
                'original_pdf_registration': fit, 'placement_max_disagreement_thumbnail_px': disagreement,
                'note': 'Thumbnail supports background placement only; it does not calibrate physical millimetres.'})
        def native_target(number, stroke_index):
            if number not in thumbnail_transforms:
                raise ValueError('No independent original-PDF registration for exported thumbnail')
            transform, sx, sy = thumbnail_transforms[number]
            return [affine(transform,q[0]*sx,q[1]*sy) for q in pages[number]['strokes'][stroke_index]['points_native']]
        for index, (points, (number, stroke_index)) in enumerate(zip(raw_strokes, mapping)):
            page = pages[number]
            stroke = page['strokes'][stroke_index]
            pdf_size = [pdf[number-1].rect.width, pdf[number-1].rect.height]
            target = native_target(number, stroke_index)
            candidates = [results[p['seq']] for p in points if results[p['seq']]['status']=='candidate']
            if any(p['page_uuid']!=page['page_uuid'] or p['pdf_page_1based']!=number for p in candidates):
                raise ValueError('Candidate disagrees with explicit native page')
            row = {'raw_stroke': index, 'native_stroke': stroke_index, 'pdf_page': number, 'raw_points': len(points),
                   'candidate_points': len(candidates), 'rejections': dict(Counter(results[p['seq']]['reason'] for p in points)),
                   'native_pdf_bounds_under_placement_hypothesis': [min(p[0] for p in target), min(p[1] for p in target), max(p[0] for p in target), max(p[1] for p in target)]}
            if candidates:
                d = distance_to_polyline([p['pdf_xy'] for p in candidates], target)
                screen_errors = []
                for candidate in candidates:
                    errors = []
                    for frame_index in candidate['evidence']['geometry_indices']:
                        transform = frames[frame_index]['registration']['pdf_to_screen']
                        rendered = [affine(transform,*q) for q in target]
                        errors.append(float(distance_to_polyline([candidate['screen_xy']], rendered)[0]))
                    screen_errors.append(sum(errors)/len(errors))
                row.update(candidate_to_native_polyline_pdf_pt={'median': float(np.median(d)), 'p95': float(np.percentile(d,95)), 'max': float(max(d))},
                           candidate_to_native_polyline_screen_px={'median': float(np.median(screen_errors)), 'p95': float(np.percentile(screen_errors,95)), 'max': float(max(screen_errors))},
                           metric_limits='One-way distance to known stroke; smoothing and along-stroke displacement are not isolated.')
            report['strokes'].append(row)
        # Export original-PDF crops with independent native lines and available candidates.
        for number in sorted(set(v[0] for v in mapping)):
            target_paths = []
            for i,(n,si) in enumerate(mapping):
                if n == number:
                    target_paths.append((i,native_target(n,si)))
            all_points = np.array([q for _,path in target_paths for q in path])
            clip = fitz.Rect(max(0,float(all_points[:,0].min())-15),max(0,float(all_points[:,1].min())-15),
                             min(pdf[number-1].rect.width,float(all_points[:,0].max())+15),min(pdf[number-1].rect.height,float(all_points[:,1].max())+15))
            zoom = 3.
            pix = pdf[number-1].get_pixmap(matrix=fitz.Matrix(zoom,zoom),clip=clip,alpha=False)
            canvas = cv2.cvtColor(np.frombuffer(pix.samples,np.uint8).reshape(pix.height,pix.width,3),cv2.COLOR_RGB2BGR)
            def xy(q): return tuple(np.rint(np.array(q)*zoom-[pix.x,pix.y]).astype(int))
            for i,path in target_paths:
                cv2.polylines(canvas,[np.array([xy(q) for q in path])],False,(210,80,20),2)
                for point in raw_strokes[i]:
                    candidate = results[point['seq']]
                    if candidate['status']=='candidate': cv2.circle(canvas,xy(candidate['pdf_xy']),2,(30,40,230),-1)
            cv2.imwrite(str(args.output/f'page-{number}-native-blue-candidates-red.png'),canvas)
    (args.output/'comparison.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='thumbnail_checks'},indent=2))
    print(json.dumps({'thumbnail_checks':[{k:v for k,v in row.items() if k!='original_pdf_registration'} for row in report['thumbnail_checks']]},indent=2))


if __name__ == '__main__':
    main()
