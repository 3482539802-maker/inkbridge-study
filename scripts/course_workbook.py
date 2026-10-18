"""Source-preserving question workbooks and verifiable Notes background bindings.

PDF is the immutable background. hinote_workbook can wrap it in a native
bundle with predetermined UUIDs; those remain pending until observed on device.
User-exported hinote binding remains a fallback. Never drive tablet UI.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
from html import escape
import json
import math
from pathlib import Path
import re
import sqlite3

import fitz

from capture_clock_history import atomic_bytes
from coordinate_page_binding import load_binding

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = 'inkbridge-question-plan-v1'


def sha256(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def save(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    atomic_bytes(Path(path), json.dumps(value, ensure_ascii=False, indent=2,
                                     allow_nan=False).encode('utf-8'))


def safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}', value):
        raise ValueError('Use an ASCII course/question ID, at most 80 characters')
    return value


def require_answer_free(value):
    """Legacy manifests remain readable; explicit solution artifacts fail closed."""
    if (value.get('answer_content_included', False) is not False or
            value.get('artifact_role', 'answer_free_workbook') != 'answer_free_workbook' or
            value.get('delivery_scope') == 'computer_only'):
        raise ValueError('Only answer-free workbooks may be built, bound or packaged for Notes')


def cached_spans(cache, pdf_hash, page):
    """Use a verified full-page cache payload, never combine different OCR runs."""
    if not cache or not Path(cache).exists():
        return []
    with closing(sqlite3.connect(Path(cache).resolve().as_uri()+'?mode=ro', uri=True)) as db:
        for payload, payload_hash in db.execute(
                'SELECT payload,payload_hash FROM page_text WHERE pdf_hash=? AND page=? ORDER BY created DESC',
                (pdf_hash, page)):
            value = json.loads(payload)
            from recognition_page_cache import digest
            if digest(value) != payload_hash:
                raise ValueError('Original-text cache integrity mismatch')
            request = value['request']
            size = request['page_size_pt']
            if request.get('region_pdf_pt') == [0., 0., *size]:
                return value.get('spans', [])
    return []


def propose(source, course_id, title, cache=None, pages=None):
    """Offer cuts for review. Numbered prose alone cannot certify question bounds."""
    source = Path(source).resolve(strict=True)
    digest = sha256(source)
    questions = []
    with fitz.open(source) as document:
        selected = pages or list(range(1, len(document)+1))
        if not selected or len(selected) > 400 or len(set(selected)) != len(selected):
            raise ValueError('Select 1 to 400 unique source pages')
        for number in selected:
            if type(number) is not int or not 1 <= number <= len(document):
                raise ValueError('Invalid source page')
            page = document[number-1]
            if page.rotation:
                raise ValueError('Normalize rotated source pages explicitly first')
            spans = [{'text': b[4], 'box_pdf_pt': list(b[:4])}
                     for b in page.get_text('blocks', sort=True) if b[6] == 0]
            if not spans:
                spans = cached_spans(cache, digest, number)
            markers = []
            for s in spans:
                text = s.get('text', '').strip()
                match = re.match(r'^(?:第\s*[一二三四五六七八九十\d]+\s*题|[一二三四五六七八九十]+[、．.]|\d{1,2}[、．.](?!\d)|[（(]\d{1,2}[）)])', text)
                if match and s['box_pdf_pt'][0] < .55*page.rect.width:
                    markers.append((max(0., s['box_pdf_pt'][1]-8), text[:80]))
            markers.sort()
            markers = [v for i, v in enumerate(markers) if not i or v[0]-markers[i-1][0] > 12]
            # Keep an unrecognized prefix visible for review; never silently drop it.
            if not markers or markers[0][0] > 50:
                markers.insert(0, (0., 'Unclassified page prefix' if markers else 'Whole page needs question review'))
            for index, (top, label) in enumerate(markers):
                bottom = markers[index+1][0] if index+1 < len(markers) else page.rect.height
                questions.append({'id': f'p{number:04d}-q{index+1:02d}', 'label': label,
                                  'parts': [{'page': number, 'box': [0., top, page.rect.width, bottom]}],
                                  'tags': [], 'review': 'needs_review'})
    return {'schema': SCHEMA, 'course': {'id': safe_id(course_id), 'title': title},
            'source_pdf': str(source), 'source_pdf_sha256': digest,
            'review': 'needs_review', 'questions': questions,
            'limits': ['Number detection proposes boundaries; verify continuations, diagrams and answer exclusion.',
                       'OCR text is a candidate; the workbook preserves source PDF regions.'],
            'content_role': 'untrusted_study_material'}


def validate(plan, source):
    require_answer_free(plan)
    if plan.get('schema') != SCHEMA:
        raise ValueError('Unsupported question plan')
    safe_id(plan['course']['id'])
    if sha256(source) != plan.get('source_pdf_sha256'):
        raise ValueError('Question plan source hash mismatch')
    questions = plan.get('questions')
    if not isinstance(questions, list) or not 1 <= len(questions) <= 1000:
        raise ValueError('Expected 1 to 1000 questions')
    ids = set()
    references = list(plan.get('excluded_regions', []))
    for question in questions:
        references.extend(question.get('reference_parts', []))
    with fitz.open(source) as document:
        for q in questions:
            key = safe_id(q['id'])
            if key in ids:
                raise ValueError('Duplicate question ID')
            ids.add(key)
            if not 1 <= len(q.get('parts', [])) <= 16:
                raise ValueError('Question needs 1 to 16 source regions')
            for part in q['parts'] + references:
                number, box = part['page'], part['box']
                if type(number) is not int or not 1 <= number <= len(document):
                    raise ValueError('Invalid source page')
                page = document[number-1]
                if page.rotation or list(page.rect[:2]) != [0., 0.]:
                    raise ValueError('Source needs a normalized unrotated coordinate convention')
                if (not isinstance(box, list) or len(box) != 4 or
                    not all(type(n) in (int, float) and math.isfinite(n) for n in box) or
                    not 0 <= box[0] < box[2] <= page.rect.width or
                    not 0 <= box[1] < box[3] <= page.rect.height):
                    raise ValueError('Invalid source question region')
            for part in q['parts']:
                for reference in references:
                    if part['page'] == reference['page'] and (fitz.Rect(part['box']) & fitz.Rect(reference['box'])).get_area() > 0:
                        raise ValueError('Question region intersects excluded answer/reference region')
    expected = plan.get('expected_question_ids')
    if expected is not None and (not isinstance(expected, list) or len(expected) != len(set(expected)) or set(expected) != ids):
        raise ValueError('Question inventory mismatch: missing or unexpected question IDs')
    return plan


def build(plan_path, output, preview=False):
    plan_path, output = Path(plan_path).resolve(), Path(output).resolve()
    plan = json.loads(plan_path.read_bytes())
    source = Path(plan['source_pdf']).resolve(strict=True)
    validate(plan, source)
    approved = plan.get('review') == 'confirmed' and all(q.get('review') == 'confirmed' for q in plan['questions'])
    if not approved and not preview:
        raise ValueError('Question boundaries need review; use --preview for a non-deliverable draft')
    if output.exists():
        raise FileExistsError('A workbook package is immutable; choose a new output directory')
    output.mkdir(parents=True)
    pages = []
    try:
        with fitz.open(source) as original, fitz.open() as workbook:
            for q in plan['questions']:
                # Long/cross-page questions get a continuation sheet for each region;
                # no source line is cut by an automatic pagination heuristic.
                for index, part in enumerate(q['parts']):
                    page = workbook.new_page(width=595., height=842.)
                    page_no = len(workbook)
                    heading = f"{plan['course']['id']} / {q['id']}" + (f' / part {index+1}' if len(q['parts']) > 1 else '')
                    page.insert_text((30, 25), heading, fontsize=10)
                    clip = fitz.Rect(part['box'])
                    scale = min(535/clip.width, 285/clip.height, 1.6)
                    target = fitz.Rect(30, 45, 30+clip.width*scale, 45+clip.height*scale)
                    # show_pdf_page(clip=...) hides the rest visually, but embeds
                    # full source resources (including scanned answer pixels).
                    # Embed ONLY a rendered crop; never package hidden answers.
                    prompt = original[part['page']-1].get_pixmap(
                        matrix=fitz.Matrix(3, 3), clip=clip, alpha=False)
                    page.insert_image(target, stream=prompt.tobytes('png'), keep_proportion=False)
                    answer = [30., max(360., target.y1+30), 565., 798.]
                    page.draw_line((30, answer[1]-12), (565, answer[1]-12), color=(.8, .8, .8), width=.4)
                    # Sparse, page-specific edge anchors improve blank-area geometry
                    # without filling the answer space with misleading handwriting.
                    for y in range(385, 795, 60):
                        page.insert_text((8, y), f'{page_no:03d}.{(y-385)//60}', fontsize=5, color=(.65, .65, .65))
                    page.insert_text((30, 822), f'{q["id"]} / {page_no} / {plan["source_pdf_sha256"][:12]}', fontsize=8, color=(.5, .5, .5))
                    if not approved:
                        page.insert_text((330, 25), 'DRAFT - REVIEW QUESTION CUTS', fontsize=8, color=(.7, .1, .1))
                    pages.append({'workbook_page_1based': page_no, 'question_id': q['id'],
                                  'source_page_1based': part['page'], 'source_box_pdf_pt': part['box'],
                                  'prompt_box_pdf_pt': list(target), 'answer_box_pdf_pt': answer,
                                  'prompt_embedding': 'cropped_pixels_only', 'prompt_render_dpi': 216,
                                  'source_to_workbook_affine': [scale, 0, 0, scale, target.x0-clip.x0*scale, target.y0-clip.y0*scale]})
            workbook.set_metadata({'title': plan['course']['title']+' - '+plan['course']['id'],
                                   'subject': 'InkBridge question workbook; '+plan['source_pdf_sha256'],
                                   'creator': 'InkBridge course_workbook'})
            workbook.save(output/'workbook.pdf', garbage=4, deflate=True)
        if sha256(source) != plan['source_pdf_sha256']:
            raise ValueError('Source changed during workbook generation')
        manifest = {'schema': 'inkbridge-workbook-v1', 'course': plan['course'],
                    'created_at_utc': datetime.now(timezone.utc).isoformat(),
                    'source_pdf': str(source), 'source_pdf_sha256': plan['source_pdf_sha256'],
                    'plan_sha256': sha256(plan_path), 'plan_path': str(plan_path),
                    'workbook_pdf': str(output/'workbook.pdf'), 'workbook_sha256': sha256(output/'workbook.pdf'),
                    'deliverable': approved, 'questions': plan['questions'], 'pages': pages,
                    'artifact_role': 'answer_free_workbook',
                    'answer_content_included': False,
                    'answer_isolation': 'only_reviewed_prompt_pixels_embedded; excluded regions checked when supplied',
                    'notes_import_verified': False, 'status': 'ready_for_user_import' if approved else 'question_review_required',
                    'notes_format': 'PDF background imported by user; native hinote binding follows export',
                    'tablet_ui_actions': []}
        save(output/'manifest.json', manifest)
        cards = []
        with fitz.open(output/'workbook.pdf') as pdf:
            for p in pages:
                n = p['workbook_page_1based']
                pdf[n-1].get_pixmap(matrix=fitz.Matrix(.75, .75)).save(output/f'page-{n:04d}.png')
                cards.append(f'<figure><figcaption>{escape(p["question_id"])} · {n}</figcaption><img loading="lazy" width="446" src="page-{n:04d}.png"></figure>')
        html = '<!doctype html><meta charset="utf-8"><title>题目与留白预览</title><style>body{font:16px system-ui;margin:24px;background:#eee}main{display:flex;flex-wrap:wrap;gap:18px}figure{margin:0}img{background:white;border:1px solid #bbb}</style>'
        html += '<h1>'+escape(plan['course']['title'])+'</h1><p>一题一页；长题有续页。<a href="workbook.pdf">打开作答 PDF</a></p>'
        html += '<p>'+('题目边界已核对；请自行导入华为笔记。' if approved else '草稿：题目边界尚待核对，不能当作正式题册。')+'</p><main>'+''.join(cards)+'</main>'
        atomic_bytes(output/'preview.html', html.encode())
        return manifest
    except Exception:
        # An incomplete package never gets a delivery manifest; preserve evidence.
        if not (output/'manifest.json').exists():
            save(output/'failed.json', {'state': 'generation_failed', 'deliverable': False})
        raise


def bind(manifest_path, hinote, output, generated=False):
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_bytes())
    require_answer_free(manifest)
    if not manifest.get('deliverable') or sha256(manifest['workbook_pdf']) != manifest['workbook_sha256']:
        raise ValueError('Need an intact reviewed workbook')
    native = load_binding(hinote, manifest['workbook_pdf'])
    expected = {p['workbook_page_1based'] for p in manifest['pages']}
    if set(native['pages'].values()) != expected or len(native['pages']) != len(expected):
        raise ValueError('Export must bind every workbook page exactly once')
    result = {'schema': 'inkbridge-course-binding-v1', 'course': manifest['course'],
              'manifest_path': str(manifest_path), 'manifest_sha256': sha256(manifest_path),
              'hinote_path': str(Path(hinote).resolve()), 'native': native,
              'page_questions': {uuid: next(p['question_id'] for p in manifest['pages'] if p['workbook_page_1based'] == n)
                                 for uuid, n in native['pages'].items()},
              'notes_import_verified': not generated,
              'binding_origin': 'generated_native_workbook' if generated else 'user_exported_snapshot',
              'binding_basis': 'generated_pending_device_import' if generated else 'embedded_workbook_sha256_and_native_page_UUIDs',
              'live_database_subscription': False, 'tablet_ui_actions': []}
    if Path(output).exists():
        raise FileExistsError('Binding revisions need a new output path')
    save(output, result)
    return result


def audit(manifest_path):
    """Read back actual prompt images, not just PDF clipping or text visibility.

    Only cropped-pixel packages have this stronger guarantee. Legacy packages
    remain usable, but must not be reported as passing this resource audit.
    """
    manifest = json.loads(Path(manifest_path).read_bytes())
    require_answer_free(manifest)
    plan_path = Path(manifest['plan_path'])
    if sha256(plan_path) != manifest['plan_sha256']:
        raise ValueError('Plan changed since workbook build')
    plan = json.loads(plan_path.read_bytes())
    validate(plan, plan['source_pdf'])
    if (manifest['source_pdf_sha256'] != plan['source_pdf_sha256'] or
            manifest['questions'] != plan['questions'] or
            sha256(manifest['workbook_pdf']) != manifest['workbook_sha256']):
        raise ValueError('Workbook provenance/hash mismatch')
    expected = [(q['id'], part) for q in plan['questions'] for part in q['parts']]
    receipts = []
    with fitz.open(plan['source_pdf']) as source, fitz.open(manifest['workbook_pdf']) as pdf:
        if len(pdf) != len(expected) or len(manifest['pages']) != len(expected):
            raise ValueError('Missing or extra workbook pages')
        for n, ((question, part), record, page) in enumerate(zip(expected, manifest['pages'], pdf), 1):
            if (record['workbook_page_1based'] != n or record['question_id'] != question or
                    record['source_page_1based'] != part['page'] or record['source_box_pdf_pt'] != part['box'] or
                    record.get('prompt_embedding') != 'cropped_pixels_only'):
                raise ValueError('Page source/embedding mismatch; legacy needs separate visual review')
            images = page.get_images(full=True)
            if len(images) != 1:
                raise ValueError('Expected exactly one embedded prompt crop per page')
            actual = fitz.Pixmap(pdf, images[0][0])
            expected_pixels = source[part['page']-1].get_pixmap(matrix=fitz.Matrix(3, 3), clip=fitz.Rect(part['box']), alpha=False)
            if (actual.width, actual.height, actual.n, actual.samples) != (expected_pixels.width, expected_pixels.height, expected_pixels.n, expected_pixels.samples):
                raise ValueError('Embedded prompt contains different/extra pixels')
            rects = page.get_image_rects(images[0][0])
            if len(rects) != 1 or any(abs(a-b) > .01 for a, b in zip(rects[0], record['prompt_box_pdf_pt'])):
                raise ValueError('Prompt display rectangle mismatch')
            clip = fitz.Rect(part['box'])
            a,b,c,d,e,f = record['source_to_workbook_affine']
            mapped = [a*clip.x0+c*clip.y0+e, b*clip.x0+d*clip.y0+f,
                      a*clip.x1+c*clip.y1+e, b*clip.x1+d*clip.y1+f]
            if any(abs(x-y) > .01 for x,y in zip(mapped, record['prompt_box_pdf_pt'])):
                raise ValueError('Source affine mismatch')
            receipts.append({'page': n, 'question_id': question, 'crop_pixels_equal': True,
                             'embedded_size_px': [actual.width, actual.height]})
    return {'schema': 'inkbridge-workbook-audit-v1', 'workbook_sha256': manifest['workbook_sha256'],
            'question_ids': [q['id'] for q in plan['questions']], 'pages': receipts,
            'status': 'passed', 'scope': 'inventory, hashes, embedded prompt pixels and page affines; question boundaries require visual review',
            'tablet_ui_actions': []}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest='command', required=True)
    a = commands.add_parser('propose'); a.add_argument('--source', type=Path, required=True)
    a.add_argument('--course', required=True); a.add_argument('--title', required=True)
    a.add_argument('--cache', type=Path, default=ROOT/'output/recognition/page-text.sqlite')
    a.add_argument('--pages', help='Comma-separated original page numbers'); a.add_argument('--output', type=Path, required=True)
    a = commands.add_parser('build'); a.add_argument('--plan', type=Path, required=True)
    a.add_argument('--output', type=Path, required=True); a.add_argument('--preview', action='store_true')
    a = commands.add_parser('bind'); a.add_argument('--manifest', type=Path, required=True)
    a.add_argument('--hinote', type=Path, required=True); a.add_argument('--output', type=Path, required=True)
    a = commands.add_parser('audit'); a.add_argument('--manifest', type=Path, required=True)
    a.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    if args.command == 'propose':
        if args.output.exists(): raise FileExistsError('Choose a new plan path')
        result = propose(args.source, args.course, args.title, args.cache,
                         [int(n) for n in args.pages.split(',')] if args.pages else None)
        save(args.output, result)
    elif args.command == 'build': result = build(args.plan, args.output, args.preview)
    elif args.command == 'bind': result = bind(args.manifest, args.hinote, args.output)
    else:
        if args.output.exists(): raise FileExistsError('Choose a new audit receipt path')
        result = audit(args.manifest); save(args.output, result)
    print(json.dumps({'schema': result['schema'], 'status': result.get('status'),
                      'questions': len(result.get('questions', result.get('question_ids', []))), 'tablet_ui_actions': []}, ensure_ascii=False))


if __name__ == '__main__':
    main()
