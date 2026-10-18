"""Immutable visual recognition exchange with the current chat; no API calls.

Only supplied raw-derived ink is recognized. Question images provide context,
never missing strokes or final edit state. Review is a model claim, not truth.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from html import escape
import json
import math
import os
from pathlib import Path

from course_workbook import sha256
from course_learning import canonical_hash

KINDS = {'formula', 'formula_fragment', 'text', 'diagram', 'annotation', 'mixed', 'unknown'}
STATUSES = {'readable', 'partial', 'uncertain', 'unavailable', 'deferred'}


def write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def load(path):
    return json.loads(Path(path).read_bytes())


def verify_asset(asset):
    path = Path(asset['path'])
    if path.stat().st_size > 8*1024*1024 or sha256(path) != asset['sha256']:
        raise ValueError('Review image identity/size mismatch')


def prepare_review(request_path, recognized, directory):
    """Freeze source/question/ink references, retaining all rejected/deferred groups."""
    from PIL import Image
    import fitz
    from course_learning import verified_package

    request_path = Path(request_path).resolve()
    request = load(request_path)
    if (recognized['request_id'] != request['id'] or
        recognized['activity_evidence_hash'] != request['evidence_hash']):
        raise ValueError('Ink belongs to another activity revision')
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    _, manifest = verified_package(request['source']['binding_path'])
    if sha256(manifest['source_pdf']) != request['source']['original_pdf_sha256']:
        raise ValueError('Original question source changed')
    questions = []
    with fitz.open(manifest['source_pdf']) as document:
        for index, part in enumerate(request['question']['parts']):
            page = document[part['page']-1]
            clip = fitz.Rect(part['box'])
            if clip.is_empty or not page.rect.contains(clip):
                raise ValueError('Question context outside source page')
            if clip.width*clip.height*1.6**2 > 4_000_000:
                raise ValueError('Question context raster budget exceeded')
            target = directory/f'question-{index}.png'
            page.get_pixmap(matrix=fitz.Matrix(1.6, 1.6), clip=clip).save(target)
            questions.append({'path': str(target), 'sha256': sha256(target),
                              'source_pdf_sha256': request['source']['original_pdf_sha256'],
                              'page': part['page'], 'box': part['box'], 'role': 'question_context_only'})
    images = deepcopy(recognized['images'])
    for asset in images:
        verify_asset(asset)
        with Image.open(asset['path']) as image:
            asset['raster_size'] = list(image.size)
        if not asset.get('step_id'):
            matches = [g['step_id'] for g in recognized['groups'] if
                       g['evidence_ids'] == asset['evidence_ids'] and g.get('image_sha256') == asset['sha256']]
            if len(matches) != 1: raise ValueError('Ambiguous ink image/group association')
            asset['step_id'] = matches[0]
    packet = {'schema': 'inkbridge-chat-recognition-review-v1',
              'request_path': str(request_path), 'request_sha256': sha256(request_path),
              'request_id': request['id'], 'activity_evidence_hash': request['evidence_hash'],
              'question_id': request['question']['id'], 'question_images': questions,
              'ink_images': images, 'recognition_input': deepcopy(recognized),
              'content_role': 'untrusted_study_material',
              'recognition_provider': 'current_chat', 'network_calls': False,
              'saved_or_current_state_verified': False, 'accuracy_verified': False,
              'limits': ['Raw contacts do not replay undo, eraser or lasso.',
                         'Question context cannot supply missing handwriting.',
                         'Temporal groups are not semantic steps; classify regions independently.',
                         'Rejected and deferred groups remain explicit; do not drop incomplete strokes.']}
    packet['packet_id'] = canonical_hash(packet)
    packet_path = directory/'packet.json'
    write_new(packet_path, packet)
    by_id = {a['step_id']: a for a in images}
    groups = []
    for group in recognized['groups']:
        available = group['step_id'] in by_id
        groups.append({'step_id': group['step_id'], 'evidence_ids': group['evidence_ids'],
                       'status': 'uncertain' if available else 'unavailable',
                       'semantic_type': 'unknown', 'elements': [],
                       'uncertainties': ['待当前聊天核图' if available else group.get('reason', 'No eligible image')]})
    steps = {s['id']: s for s in request['activity']['step_candidates']}
    for key in recognized['deferred_step_ids']:
        groups.append({'step_id': key, 'evidence_ids': steps[key]['evidence_ids'],
                       'status': 'deferred', 'semantic_type': 'unknown', 'elements': [],
                       'uncertainties': ['超出本轮输入预算，未识别']})
    template = {'schema': 'inkbridge-chat-recognition-response-v1', 'packet_id': packet['packet_id'],
                'request_id': request['id'], 'activity_evidence_hash': request['evidence_hash'],
                'reviewer': {'provider': 'current_chat', 'model': '未声明'}, 'groups': groups}
    write_new(directory/'response-template.json', template)
    prompt = (f'读取 {packet_path}，逐张查看 question_images 和 ink_images。直接识别笔迹，'
              '不要依据题目补写没有看见的内容，不先看公式模型候选。将区域分为 formula、formula_fragment、'
              'text、diagram、annotation、mixed、unknown；不要把图形或圈画强转成公式。'
              '逐组保留 evidence_ids，所有不可用和 deferred 组也必须列出。'
              'elements 为区域数组，每项含 kind、text、latex（无则null）、bbox_px（相对笔迹图的[x0,y0,x1,y1]）、'
              'evidence_ids、uncertainties（字符串数组）。无法确定对应单笔时引用整组，不虚构精确笔画归属。'
              'status 为 readable/partial/uncertain/unavailable/deferred，semantic_type 同区域类别。'
              '原始轨迹未重放撤回/擦除/套索，不是最终答案。不作解题或正确性判断。'
              f'按 {directory / "response-template.json"} 写一份新 response.json，保留所有不确定。')
    (directory/'prompt.txt').write_text(prompt, encoding='utf-8')
    sections = ['<h1>当前聊天识别</h1><p>原始笔迹候选；未确认最终编辑状态。</p>']
    for asset in questions:
        sections.append('<h2>原题上下文</h2><img alt="原题" src="'+escape(Path(os.path.relpath(asset['path'], directory)).as_posix(), quote=True)+'">')
    for group in groups:
        sections.append('<h2>'+escape(group['step_id'])+'</h2>')
        asset = by_id.get(group['step_id'])
        if asset:
            sections.append('<img alt="原始笔迹" src="'+escape(Path(os.path.relpath(asset['path'], directory)).as_posix(), quote=True)+'">')
        else: sections.append('<p>'+escape('; '.join(group['uncertainties']))+'</p>')
    sections.append('<h2>交给当前聊天</h2><pre>'+escape(prompt)+'</pre>')
    (directory/'review.html').write_text('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        '<title>当前聊天识别</title><style>body{font:16px/1.6 system-ui;max-width:1100px;margin:32px auto;padding:20px}'
        'img{max-width:100%;height:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>'+''.join(sections), encoding='utf-8')
    return packet_path


def strings(value, limit=100):
    return (isinstance(value, list) and len(value) <= limit and
            all(isinstance(v, str) and len(v) <= 4000 for v in value))


def validate_review(packet_path, response_path):
    """Read-only verification, also usable for archived reviews without queue writes."""
    if Path(response_path).stat().st_size > 2*1024*1024:
        raise ValueError('Chat response exceeds review budget')
    packet = load(packet_path); response = load(response_path)
    identity = dict(packet); packet_id = identity.pop('packet_id')
    if packet_id != canonical_hash(identity): raise ValueError('Review packet changed')
    request = load(packet['request_path'])
    if sha256(packet['request_path']) != packet['request_sha256']:
        raise ValueError('Review request changed')
    if (response.get('schema') != 'inkbridge-chat-recognition-response-v1' or
        any(response.get(k) != packet[k] for k in ('packet_id', 'request_id', 'activity_evidence_hash'))):
        raise ValueError('Review belongs to another packet/revision')
    reviewer = response.get('reviewer', {})
    if (reviewer.get('provider') != 'current_chat' or not isinstance(reviewer.get('model'), str) or
        not 1 <= len(reviewer['model']) <= 200):
        raise ValueError('Declare current-chat reviewer identity')
    for asset in packet['question_images'] + packet['ink_images']: verify_asset(asset)
    result = deepcopy(packet['recognition_input'])
    expected = {g['step_id']: g for g in result['groups']}
    steps = {s['id']: s for s in request['activity']['step_candidates']}
    for key in result['deferred_step_ids']:
        expected[key] = {'step_id': key, 'evidence_ids': steps[key]['evidence_ids'], 'status': 'deferred'}
    groups = response.get('groups')
    if not isinstance(groups, list) or len(groups) != len(expected):
        raise ValueError('Review must account for every group, including rejected/deferred')
    images = {a['step_id']: a for a in packet['ink_images']}
    seen = set(); reviewed = []
    for group in groups:
        key = group.get('step_id')
        if key not in expected or key in seen: raise ValueError('Unknown or duplicate review group')
        seen.add(key); original = expected[key]; asset = images.get(key)
        if group.get('evidence_ids') != original['evidence_ids']:
            raise ValueError('Review changed contact membership')
        status = group.get('status'); kind = group.get('semantic_type'); elements = group.get('elements')
        if (status not in STATUSES or kind not in KINDS or not strings(group.get('uncertainties')) or
            not isinstance(elements, list) or len(elements) > 64):
            raise ValueError('Invalid recognition status/type/regions')
        if not asset:
            required = 'deferred' if key in result['deferred_step_ids'] else 'unavailable'
            if status != required or elements or kind != 'unknown':
                raise ValueError('Cannot recognize rejected/deferred ink')
        elif status in ('unavailable', 'deferred'):
            raise ValueError('Eligible ink requires an explicit review, even if uncertain')
        if status in ('readable', 'partial') and not elements:
            raise ValueError('Readable/partial recognition needs visible regions')
        for element in elements:
            refs = element.get('evidence_ids'); box = element.get('bbox_px')
            if (element.get('kind') not in KINDS or not isinstance(element.get('text'), str) or
                len(element['text']) > 8000 or (element.get('latex') is not None and
                (not isinstance(element['latex'], str) or len(element['latex']) > 8000)) or
                not strings(element.get('uncertainties')) or not isinstance(refs, list) or not refs or
                not all(isinstance(v, str) for v in refs) or len(refs) != len(set(refs)) or
                not set(refs).issubset(original['evidence_ids'])):
                raise ValueError('Region needs bounded transcription and existing contact references')
            if element['kind'] not in ('formula', 'formula_fragment') and element.get('latex'):
                raise ValueError('Do not coerce text/diagram/annotation into a formula')
            if (not isinstance(box, list) or len(box) != 4 or
                any(type(v) not in (int, float) or not math.isfinite(v) for v in box) or
                not 0 <= box[0] < box[2] <= asset['raster_size'][0] or
                not 0 <= box[1] < box[3] <= asset['raster_size'][1]):
                raise ValueError('Region must be inside its ink image')
        entry = deepcopy(original)
        entry.update(status='chat_candidate' if asset else status, review_status=status,
                     semantic_type=kind, elements=deepcopy(elements), uncertainties=group['uncertainties'],
                     accuracy_verified=False, formula_type_verified=False)
        # Keep formula compatibility only for a single independently classified region.
        entry['latex_candidate'] = (elements[0].get('latex') if len(elements) == 1 and
            kind in ('formula', 'formula_fragment') and elements[0]['kind'] == kind else None)
        if key not in result['deferred_step_ids']: reviewed.append(entry)
    result['groups'] = reviewed
    result['recognition_backend'] = 'current_chat_reviewed'
    result['review_provenance'] = {'packet_path': str(Path(packet_path).resolve()),
        'packet_sha256': sha256(packet_path), 'packet_id': packet_id,
        'response_path': str(Path(response_path).resolve()), 'response_sha256': sha256(response_path),
        'reviewer': reviewer, 'reviewer_identity_independently_verified': False,
        'accuracy_verified': False, 'saved_or_current_state_verified': False}
    result['external_inference'] = False  # This importer makes no inference/network calls.
    result['recognition_execution'] = 'user_invoked_current_chat'
    return result


def import_review(packet_path, response_path, output):
    result = validate_review(packet_path, response_path)
    write_new(output, result)
    return result


def publish_review(store, packet_path, recognized):
    """Advance only the original pending job or its exact prepared-ink revision.

    Preparing ink itself supersedes the original queued request. The review is
    still bound to that immutable packet, so resolve its unique pending child
    rather than reviving the old job or attaching to newer writing.
    """
    from course_tutor import add_answer_evidence
    packet = load(packet_path)
    source = Path(packet['request_path'])
    if sha256(source) != packet['request_sha256']:
        raise ValueError('Review source changed before publication')
    if (recognized.get('review_provenance', {}).get('packet_sha256') != sha256(packet_path) or
        recognized.get('request_id') != packet['request_id'] or
        recognized.get('activity_evidence_hash') != packet['activity_evidence_hash']):
        raise ValueError('Reviewed result does not match this packet')
    row = store.db.execute('SELECT status FROM tutor_jobs WHERE id=?', (packet['request_id'],)).fetchone()
    if row and row[0] == 'pending': return add_answer_evidence(store, source, recognized)
    matches = []
    for (path,) in store.db.execute("SELECT request_path FROM tutor_jobs WHERE status='pending'"):
        request = load(path)
        if (request.get('activity_request_id') == packet['request_id'] and
            request.get('recognition_status') == 'awaiting_current_chat_recognition' and
            request.get('chat_recognition_review', {}).get('sha256') == sha256(packet_path)):
            matches.append((Path(path), request))
    if not matches: return source
    if len(matches) != 1: raise ValueError('Ambiguous pending chat-review revision')
    path, request = matches[0]
    rebased = deepcopy(recognized)
    rebased['request_id'] = request['id']
    rebased['activity_evidence_hash'] = request['evidence_hash']
    rebased['review_provenance']['prepared_request_id'] = request['id']
    return add_answer_evidence(store, path, rebased)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--packet', type=Path, required=True)
    parser.add_argument('--response', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--learning-root', type=Path, help='Publish via the course queue; only a pending revision can advance')
    args = parser.parse_args()
    result = import_review(args.packet, args.response, args.output)
    status = {'output': str(args.output.resolve()), 'groups': len(result['groups'])}
    if args.learning_root:
        from course_learning import LearningStore
        packet = load(args.packet)
        store = LearningStore(args.learning_root)
        try:
            source = Path(packet['request_path'])
            published = publish_review(store, args.packet, result)
            status.update(published_request=str(published), new_revision=published != source)
        finally: store.close()
    print(json.dumps(status, ensure_ascii=False))


if __name__ == '__main__': main()
