"""Portable course knowledge, immutable practice/attempt history and review plans.

Capture evidence stays read-only. Browser outcomes are self reports; only a
separate tutor review of a submitted answer can advance the learning rubric.
"""
from __future__ import annotations

from collections import defaultdict
from html import escape
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import quote
import uuid

import fitz

from atomic_output import atomic_bytes
from course_learning import canonical_hash
from course_workbook import save, sha256
from recognition_display import recognition_html

HERE = Path(__file__).resolve().parent
ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$')
OUTCOMES = {'independent_correct', 'hinted_correct', 'incorrect', 'uncertain'}
STATE_LABELS = {'unknown': '尚未评估', 'observed': '已有作答，待判断',
                'pending_review': '已提交，待批阅', 'needs_work': '需要补强',
                'with_help': '提示后完成', 'independent': '已独立完成一次',
                'retained': '间隔复练已通过'}


def read(path): return json.loads(Path(path).read_bytes())


def valid_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value): raise ValueError('Invalid knowledge identifier')
    return value


def load_catalog(course, manifest):
    """No subject-specific assumptions in the engine; absent catalogs stay usable."""
    if course.get('curriculum'):
        catalog = read(course['curriculum'])
    else:
        catalog = {'schema': 'inkbridge-curriculum-v1', 'course_id': manifest['course']['id'],
                   'scope_note': '尚未导入课程目录；以下按题目标签临时整理，不能代表完整课程。',
                   'chapters': [{'id': 'unclassified', 'title': '待整理章节', 'goals': ['导入教材目录或课程大纲']}],
                   'knowledge_points': [{'id': 'unclassified', 'chapter_id': 'unclassified',
                                         'title': '待标注知识点', 'prerequisites': []}],
                   'question_knowledge': {q['id']: ['unclassified'] for q in manifest['questions']}}
    if catalog.get('schema') != 'inkbridge-curriculum-v1' or catalog.get('course_id') != manifest['course']['id']:
        raise ValueError('Curriculum/course mismatch')
    for key,value in catalog.get('review_policy',{}).items():
        if key not in ('retry_days','independent_days','retained_days','retention_gap_days') or type(value) is not int or not 1 <= value <= 365:
            raise ValueError('Invalid review scheduling policy')
    chapters = catalog['chapters']; points = catalog['knowledge_points']
    for entries in (chapters, points):
        ids = [valid_id(x['id']) for x in entries]
        if not ids or len(ids) != len(set(ids)): raise ValueError('Duplicate/empty curriculum identifiers')
    chapter_ids = {x['id'] for x in chapters}; point_ids = {x['id'] for x in points}
    for point in points:
        if point['chapter_id'] not in chapter_ids or not set(point.get('prerequisites', [])).issubset(point_ids):
            raise ValueError('Unknown curriculum dependency')
    dependencies = {x['id']: x.get('prerequisites', []) for x in points}
    def visit(key, active, done):
        if key in active: raise ValueError('Cyclic knowledge prerequisites')
        if key in done: return
        for parent in dependencies[key]: visit(parent, active | {key}, done)
        done.add(key)
    done = set()
    for key in dependencies: visit(key, set(), done)
    question_ids = {q['id'] for q in manifest['questions']}
    if set(catalog['question_knowledge']) != question_ids:
        raise ValueError('Curriculum must explicitly map every source question')
    for ids in catalog['question_knowledge'].values():
        if not ids or len(ids) != len(set(ids)) or not set(ids).issubset(point_ids):
            raise ValueError('Unknown/duplicate question knowledge')
    return catalog


def setup(store):
    store.db.executescript('''
        CREATE TABLE IF NOT EXISTS knowledge_events(
            id TEXT PRIMARY KEY, course TEXT NOT NULL, kind TEXT NOT NULL,
            payload TEXT NOT NULL, created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS knowledge_course ON knowledge_events(course,created);
        CREATE TABLE IF NOT EXISTS practice_generations(
            id TEXT PRIMARY KEY, course TEXT NOT NULL, question TEXT NOT NULL,
            context_hash TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS practice_items(
            id TEXT PRIMARY KEY, course TEXT NOT NULL, question TEXT NOT NULL,
            payload TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS practice_batches(
            id TEXT PRIMARY KEY, input_hash TEXT NOT NULL);
    ''')


def events(store, cid):
    return [json.loads(row[0]) for row in store.db.execute(
        'SELECT payload FROM knowledge_events WHERE course=? ORDER BY created,id', (cid,))]


def items(store, cid):
    return [json.loads(row[0]) for row in store.db.execute(
        'SELECT payload FROM practice_items WHERE course=? ORDER BY created,id', (cid,))]


def knowledge_states(catalog, history, exercises, activity, now=None):
    now = time.time() if now is None else now
    by_id = {p['id']: p for p in exercises}
    attempts = {e['id']: e for e in history if e['kind'] == 'attempt'}
    reviews = {e['attempt_id']: e for e in history if e['kind'] == 'review'}
    policy = dict(retry_days=1, independent_days=3, retained_days=7, retention_gap_days=1)
    policy.update(catalog.get('review_policy',{}))
    result = {}
    for point in catalog['knowledge_points']:
        key = point['id']; eligible = []; pending = []; related = []
        for attempt in attempts.values():
            if key not in by_id[attempt['practice_id']]['knowledge_ids']: continue
            related.append(attempt['id'])
            if attempt['id'] in reviews: eligible.append(reviews[attempt['id']])
            else: pending.append(attempt)
        state = 'unknown'; due = None; reason = '还没有可批阅的独立作答。'
        if any(q.get('has_activity') and key in catalog['question_knowledge'].get(q['id'], []) for q in activity):
            state = 'observed'; reason = '已有原题笔迹；尚未确认最终答案与独立完成情况。'
        if eligible:
            latest = eligible[-1]; outcome = latest['outcome']
            successes = []
            for review in reversed(eligible):
                if review['outcome'] != 'independent_correct': break
                successes.append(review)
            retained = any(a['practice_id'] != b['practice_id'] and abs(a['created_at_pc']-b['created_at_pc']) >= policy['retention_gap_days']*86400
                           for a in successes for b in successes)
            state = {'incorrect': 'needs_work', 'uncertain': 'observed', 'hinted_correct': 'with_help',
                     'independent_correct': 'retained' if retained else 'independent'}[outcome]
            interval = {'needs_work': policy['retry_days'], 'observed': policy['retry_days'], 'with_help': policy['retry_days'],
                        'independent': policy['independent_days'], 'retained': policy['retained_days']}[state]
            # Review spacing measures when the learner submitted, not when a delayed tutor ran.
            due = latest['created_at_pc'] + interval*86400
            reason = latest['rationale']
        if pending:
            state = 'pending_review'; reason = '最近一次提交尚未批阅，暂不据此提升掌握状态。'
        result[key] = {'state': state, 'label': STATE_LABELS[state], 'reason': reason,
                       'review_count': len(eligible), 'attempt_ids': related, 'due_at_pc': due,
                       'due': state != 'pending_review' and due is not None and due <= now, 'mastery_percentage': None}
    return result


def text(value, maximum=8000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError('Missing/oversized learning text')
    return value


def blocks(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 60: raise ValueError('Invalid content blocks')
    for block in value:
        if not isinstance(block, dict) or len(block) != 1 or next(iter(block)) not in ('text', 'math'):
            raise ValueError('Only plain text/math blocks are supported')
        text(next(iter(block.values())))
    return value


def validate_reference(value):
    if not isinstance(value, dict): raise ValueError('Invalid reference solution')
    for field in ('approach', 'result'):
        blocks(value.get(field))
    for field in ('pitfalls',):
        if value.get(field): blocks(value[field])
    steps = value.get('steps')
    if not isinstance(steps, list) or not 1 <= len(steps) <= 40: raise ValueError('Invalid solution steps')
    for step in steps:
        text(step.get('title'), 1000); blocks(step.get('blocks'))
    return value


def context(store, course, manifest):
    catalog = load_catalog(course, manifest)
    activity_path = store.root/(manifest['course']['id']+'-activity.json')
    activity = read(activity_path)['questions'] if activity_path.exists() else []
    history = events(store, manifest['course']['id']); exercises = items(store, manifest['course']['id'])
    states = knowledge_states(catalog, history, exercises, activity)
    # Time ticking/due labels do not invalidate an otherwise unchanged generation.
    fingerprint = canonical_hash([catalog, history, exercises, activity])
    return catalog, history, exercises, states, fingerprint


def generation_context(store, course, manifest, question_id):
    catalog, history, exercises, _, _ = context(store, course, manifest)
    skills = set(catalog['question_knowledge'][question_id])
    related_questions = {q for q, ids in catalog['question_knowledge'].items() if skills.intersection(ids)}
    related_exercises = [p for p in exercises if skills.intersection(p['knowledge_ids'])]
    practice_ids = {p['id'] for p in related_exercises}
    related_history = [e for e in history if e.get('practice_id') in practice_ids]
    activity_path = store.root/(manifest['course']['id']+'-activity.json')
    activity = [q for q in read(activity_path)['questions'] if q['id'] in related_questions] if activity_path.exists() else []
    tutor = [(key,status,request_hash,sha256(result) if result else None)
             for key,qid,status,request_hash,result in store.db.execute(
                 'SELECT id,question,status,request_hash,result_path FROM tutor_jobs WHERE course=? ORDER BY created,id', (course['id'],))
             if qid in related_questions]
    return canonical_hash([catalog, sha256(course['manifest']), related_history, related_exercises, activity, tutor])


def action(store, courses, data):
    """Browser actions only enqueue, reveal or submit; never award mastery."""
    setup(store)
    if not isinstance(data,dict): raise ValueError('Learning action must be an object')
    kind = data.get('kind'); cid = valid_id(data.get('course_id')); key = valid_id(data.get('id'))
    course = next((c for c in courses if c['id'] == cid), None)
    if not course: raise ValueError('Unknown course')
    manifest = read(course['manifest']); catalog, history, exercises, states, fingerprint = context(store, course, manifest)
    prior = store.db.execute('SELECT payload FROM knowledge_events WHERE id=?', (key,)).fetchone()
    if prior:
        prior = json.loads(prior[0])
        if prior['submission_hash'] != canonical_hash(data): raise ValueError('Event identifier reused for different content')
        return prior
    prior = store.db.execute('SELECT payload FROM practice_generations WHERE id=?', (key,)).fetchone()
    if prior:
        prior = json.loads(prior[0])
        if prior['submission_hash'] != canonical_hash(data): raise ValueError('Generation identifier reused')
        return prior
    event = {'id': key, 'course_id': cid, 'kind': kind, 'created_at_pc': time.time(), 'submission_hash': canonical_hash(data)}
    if kind == 'generate':
        qid = data.get('question_id')
        if qid not in catalog['question_knowledge']: raise ValueError('Unknown source question')
        fingerprint = generation_context(store, course, manifest, qid)
        existing = store.db.execute("SELECT payload FROM practice_generations WHERE course=? AND question=? AND context_hash=? AND status='pending'",
                                    (cid,qid,fingerprint)).fetchone()
        if existing: return json.loads(existing[0])
        event.update(question_id=qid, context_hash=fingerprint, knowledge_ids=catalog['question_knowledge'][qid],
                     states={k: states[k] for k in catalog['question_knowledge'][qid]}, status='pending',
                     request='先读原题、历史作答和评语；生成基础诊断、同类变式、迁移题各一题，附可核对解答。不得把推测的弱项当成已确认事实。',
                     response_schema={'schema': 'inkbridge-practice-batch-v1', 'generation_id': key,
                         'context_hash': fingerprint, 'course_id': cid, 'items': [{'id': 'unique-id', 'question_id': qid,
                         'knowledge_ids': catalog['question_knowledge'][qid], 'title': '...', 'level': '基础诊断|同类变式|迁移应用',
                         'reason': '...', 'prompt': [{'text': '...'}], 'hints': ['...'], 'solution': [{'text': '...'}]}]})
        with store.db:
            store.db.execute("UPDATE practice_generations SET status='superseded' WHERE course=? AND question=? AND status='pending'",(cid,qid))
            store.db.execute('INSERT INTO practice_generations VALUES (?,?,?,?,?,?,?)',
                (key, cid, qid, fingerprint, 'pending', json.dumps(event, ensure_ascii=False), event['created_at_pc']))
        save(store.root/'knowledge/queue'/(key+'.request.json'), event)
        return event
    exercise = next((p for p in exercises if p['id'] == data.get('practice_id')), None)
    if not exercise: raise ValueError('Unknown practice item')
    event['practice_id'] = exercise['id']
    if kind == 'reveal':
        if data.get('target') not in ('hint', 'solution'): raise ValueError('Invalid reveal target')
        event['target'] = data['target']
    elif kind == 'attempt':
        event.update(answer=text(data.get('answer')), self_report=data.get('self_report', 'uncertain'),
                     status='awaiting_tutor_review')
        if event['self_report'] not in OUTCOMES: raise ValueError('Invalid self report')
        event['help_seen'] = event['self_report'] == 'hinted_correct' or any(e['kind'] == 'reveal' and e.get('practice_id') == exercise['id'] for e in history)
        event['answer_sha256'] = canonical_hash(event['answer'])
    else: raise ValueError('Unsupported learning action')
    with store.db:
        store.db.execute('INSERT INTO knowledge_events VALUES (?,?,?,?,?)',
                         (key, cid, kind, json.dumps(event, ensure_ascii=False), event['created_at_pc']))
    save(store.root/'knowledge/history'/(key+'.json'), event)
    return event


def accept_batch(store, courses, batch):
    setup(store)
    if batch.get('schema') != 'inkbridge-practice-batch-v1': raise ValueError('Invalid practice batch')
    completed = store.db.execute('SELECT input_hash FROM practice_batches WHERE id=?', (batch.get('generation_id'),)).fetchone()
    if completed:
        if completed[0] != canonical_hash(batch): raise ValueError('Completed generation content changed')
        return {'status': 'accepted', 'practice_ids': [p['id'] for p in batch['items']], 'replayed': True}
    row = store.db.execute('SELECT payload,status FROM practice_generations WHERE id=?', (batch.get('generation_id'),)).fetchone()
    if not row or row[1] != 'pending': raise ValueError('Unknown/completed generation')
    request = json.loads(row[0]); cid = request['course_id']
    course = next((c for c in courses if c['id'] == cid),None)
    if course is None: raise ValueError('Generation course is no longer configured')
    catalog, _, _, _, _ = context(store, course, read(course['manifest']))
    fingerprint = generation_context(store, course, read(course['manifest']), request['question_id'])
    if batch.get('course_id') != cid or batch.get('context_hash') != request['context_hash'] or fingerprint != request['context_hash']:
        raise ValueError('Practice generation context is stale or mismatched')
    entries = batch.get('items')
    if not isinstance(entries, list) or not 1 <= len(entries) <= 12: raise ValueError('Invalid practice count')
    if not all(isinstance(p,dict) for p in entries): raise ValueError('Practice items must be objects')
    ids = [valid_id(p['id']) for p in entries]
    if len(ids) != len(set(ids)): raise ValueError('Duplicate practice IDs')
    validated = []
    for item in entries:
        if item.get('question_id') != request['question_id']: raise ValueError('Practice parent mismatch')
        skills = item.get('knowledge_ids')
        if not isinstance(skills, list) or not skills or not set(skills).issubset(request['knowledge_ids']):
            raise ValueError('Practice knowledge mismatch')
        for field in ('title', 'level', 'reason'): text(item.get(field), 1500)
        blocks(item.get('prompt')); blocks(item.get('solution'))
        if not isinstance(item.get('hints'), list) or len(item['hints']) > 10: raise ValueError('Invalid practice hints')
        for hint in item['hints']: text(hint)
        validated.append(dict(item, course_id=cid, generation_id=request['id'],
                              source_context_hash=fingerprint, validation='current_chat_reviewed', created_at_pc=time.time()))
    with store.db:
        for item in validated:
            store.db.execute('INSERT INTO practice_items VALUES (?,?,?,?,?)',
                (item['id'], cid, item['question_id'], json.dumps(item, ensure_ascii=False), item['created_at_pc']))
        store.db.execute("UPDATE practice_generations SET status='completed' WHERE id=?", (request['id'],))
        store.db.execute('INSERT INTO practice_batches VALUES (?,?)', (request['id'], canonical_hash(batch)))
    for item in validated: save(store.root/'knowledge/practice'/(item['id']+'.json'), item)
    return {'status': 'accepted', 'practice_ids': ids}


def accept_review(store, review):
    """Only local tutor inbox calls this; browser self ratings cannot call it."""
    setup(store)
    valid_id(review.get('id'))
    prior = store.db.execute('SELECT payload FROM knowledge_events WHERE id=?', (review['id'],)).fetchone()
    if prior:
        if json.loads(prior[0]).get('submission_hash') != canonical_hash(review): raise ValueError('Review ID content changed')
        return {'status': 'accepted', 'review_id': review['id'], 'replayed': True}
    row = store.db.execute("SELECT payload FROM knowledge_events WHERE id=? AND kind='attempt'", (review.get('attempt_id'),)).fetchone()
    if not row: raise ValueError('Unknown submitted attempt')
    attempt = json.loads(row[0]); outcome = review.get('outcome')
    if review.get('answer_sha256') != attempt['answer_sha256'] or outcome not in OUTCOMES:
        raise ValueError('Review answer identity/outcome mismatch')
    if attempt['help_seen'] and outcome == 'independent_correct': raise ValueError('Assisted attempt cannot establish independent mastery')
    if store.db.execute("SELECT 1 FROM knowledge_events WHERE kind='review' AND json_extract(payload,'$.attempt_id')=?", (attempt['id'],)).fetchone():
        raise ValueError('Attempt already reviewed; submit a new attempt for a correction')
    event = {'id': review['id'], 'kind': 'review', 'course_id': attempt['course_id'],
             'attempt_id': attempt['id'], 'practice_id': attempt['practice_id'], 'answer_sha256': attempt['answer_sha256'],
             'outcome': outcome, 'rationale': text(review.get('rationale')), 'feedback': blocks(review.get('feedback')),
             'provider': 'current_chat_local_review', 'created_at_pc': attempt['created_at_pc'], 'reviewed_at_pc': time.time(),
             'submission_hash': canonical_hash(review)}
    with store.db:
        store.db.execute('INSERT INTO knowledge_events VALUES (?,?,?,?,?)',
                         (event['id'], event['course_id'], 'review', json.dumps(event, ensure_ascii=False), event['reviewed_at_pc']))
    save(store.root/'knowledge/history'/(event['id']+'.json'), event)
    return {'status': 'accepted', 'review_id': event['id']}


def process_incoming(store, courses):
    setup(store); incoming = store.root/'knowledge/incoming'; incoming.mkdir(parents=True, exist_ok=True)
    # Limit actual inputs, not already-processed receipts, so old files cannot starve new work.
    pending = [p for p in sorted(incoming.glob('*.json'))
               if not p.name.endswith('.receipt.json') and not p.with_suffix('.receipt.json').exists()]
    for path in pending[:16]:
        digest = sha256(path)
        try:
            value = read(path)
            if value.get('schema') == 'inkbridge-practice-batch-v1': result = accept_batch(store, courses, value)
            elif value.get('schema') == 'inkbridge-practice-review-v1': result = accept_review(store, value)
            else: raise ValueError('Unknown learning inbox schema')
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
            result = {'status': 'rejected', 'reason': str(error)[:240]}
        save(path.with_suffix('.receipt.json'), dict(result, input_sha256=digest))


def math_html(latex):
    try:
        from latex2mathml.converter import convert
        return convert(latex)
    except Exception:
        return '<code>'+escape(latex)+'</code>'


def equation_parts(latex):
    """Wrap at top-level relations/operators, preserving fractions and groups.

    Each piece remains valid MathML. Keeping a leading operator on the next
    piece makes wrapped chains readable without shrinking formulas to tiny text.
    """
    parts=[];start=0;depth=0;i=0
    separators=('\\qquad','\\quad','\\Longrightarrow','\\Rightarrow','\\Longleftrightarrow')
    while i<len(latex):
        char=latex[i]
        if depth==0:
            separator=next((s for s in separators if latex.startswith(s,i)),None)
            if separator:
                if latex[start:i].strip():parts.append(latex[start:i].strip())
                if separator in ('\\quad','\\qquad'):i+=len(separator);start=i;continue
                start=i;i+=len(separator);continue
            if char in '=+-' and i>start:
                if latex[start:i].strip():parts.append(latex[start:i].strip())
                start=i
        if char in '{([':depth+=1
        elif char in '})]':depth=max(0,depth-1)
        i+=1
    if latex[start:].strip():parts.append(latex[start:].strip())
    return parts or [latex]


def rendered_blocks(entries):
    return ''.join('<div class="formula">'+''.join('<span class="equation-part">'+math_html(piece)+'</span>' for piece in equation_parts(b['math']))+'</div>'
                   if 'math' in b else '<p>'+escape(b['text'])+'</p>' for b in entries)


def asset(source, directory, expected=None):
    source = Path(source).resolve(); digest = sha256(source)
    if expected and expected != digest: raise ValueError('Knowledge asset changed')
    suffix = source.suffix.lower()
    if suffix not in ('.png', '.jpg', '.jpeg', '.webp'): raise ValueError('Unsupported knowledge image')
    target = directory/'assets'/(digest+suffix)
    if not target.exists(): atomic_bytes(target, source.read_bytes())
    return {'url': 'assets/'+target.name, 'sha256': digest}


def source_images(manifest, question, directory):
    images = []
    with fitz.open(manifest['source_pdf']) as pdf:
        for part in question['parts']:
            # Cache key includes source hash and exact original region, never a tablet screenshot.
            key = canonical_hash([manifest['source_pdf_sha256'], part])
            target = directory/'assets'/(key+'.png')
            if not target.exists():
                atomic_bytes(target, pdf[part['page']-1].get_pixmap(matrix=fitz.Matrix(1.7, 1.7), clip=fitz.Rect(part['box'])).tobytes('png'))
            images.append({'url': 'assets/'+target.name, 'sha256': sha256(target), 'source_page': part['page'], 'source_box': part['box']})
    return images


def build_snapshot(store, course):
    setup(store); manifest = read(course['manifest']); cid = manifest['course']['id']; valid_id(cid)
    if sha256(manifest['source_pdf']) != manifest['source_pdf_sha256']: raise ValueError('Original question source changed')
    catalog, history, exercises, states, fingerprint = context(store, course, manifest)
    directory = store.root/'knowledge'/cid; (directory/'assets').mkdir(parents=True, exist_ok=True)
    solutions = {}
    if course.get('solutions'):
        if sha256(course['solutions']) != course.get('solutions_sha256'): raise ValueError('Unpinned/changed reference solutions')
        reference = read(course['solutions'])
        if reference.get('source_pdf_sha256') != manifest['source_pdf_sha256']: raise ValueError('Solution source mismatch')
        solutions = {q['id']: q for q in reference['questions']}
    activity_path = store.root/(cid+'-activity.json')
    activity = {q['id']: q for q in read(activity_path)['questions']} if activity_path.exists() else {}
    archived = defaultdict(list)
    for entry in course.get('archived_recognition_reviews', []):
        from recognition_chat import validate_review
        for field in ('packet', 'response'):
            if sha256(entry[field]) != entry[field+'_sha256']:
                raise ValueError('Archived recognition identity mismatch')
        packet = read(entry['packet'])
        original = read(packet['request_path'])
        row = store.db.execute('SELECT request_hash FROM tutor_jobs WHERE id=? AND course=?',
                               (packet['request_id'], cid)).fetchone()
        if (not row or row[0] != packet['request_sha256'] or
            original['source']['original_pdf_sha256'] != manifest['source_pdf_sha256']):
            raise ValueError('Archived recognition belongs to another course/source')
        reviewed = validate_review(entry['packet'], entry['response'])
        archived[packet['question_id']].append({
            'historical': True, 'request_id': packet['request_id'],
            'recognition_html': recognition_html(reviewed['groups']),
            'images': [dict(asset(a['path'], directory, a['sha256']), evidence_ids=a['evidence_ids'])
                       for a in reviewed['images']],
            'packet_sha256': entry['packet_sha256'], 'response_sha256': entry['response_sha256']})
    questions = []
    for q in manifest['questions']:
        requests = []; feedback = []
        rows = store.db.execute('SELECT id,status,request_path,result_path,request_hash,created FROM tutor_jobs WHERE course=? AND question=? ORDER BY created,id', (cid, q['id'])).fetchall()
        for key, status, request_path, result_path, request_hash, created in rows:
            if sha256(request_path) != request_hash: raise ValueError('Knowledge request identity mismatch')
            request = read(request_path)
            outcome = Path(request_path).with_suffix('.recognition-outcome.json')
            images = []
            for image in request.get('answer_images', []):
                if Path(image['path']).resolve().parent != Path(request_path).resolve().parent:
                    raise ValueError('Ink image outside request directory')
                images.append(dict(asset(image['path'], directory, image['sha256']), evidence_ids=image['evidence_ids'],
                                   provenance=image.get('provenance', {})))
            requests.append({'id': key, 'status': status, 'created_at_pc': created,
                'evidence_hash': request['evidence_hash'], 'images': images,
                'provisional': request['activity']['provisional'],
                'recognition_outcome': read(outcome) if outcome.exists() else None,
                'recognition_html': recognition_html(request.get('recognition_candidates', [])),
                'recognition_status': request.get('recognition_status'),
                'chat_review_path': request.get('chat_recognition_review', {}).get('path'),
                'writing_seconds': request['activity']['observed_writing_seconds'],
                'url': '../../tutor/'+Path(request_path).with_suffix('.html').name})
            if result_path:
                result = read(result_path)
                if result['request_id'] != key or result['evidence_hash'] != request['evidence_hash']: raise ValueError('Feedback provenance mismatch')
                feedback.append({'request_id': key, 'response': result['response'], 'step_timings': result.get('step_timings', []),
                                 'current': bool(rows and rows[-1][0] == key)})
        latest = requests[-1] if requests else None
        ink = next((r for r in reversed(requests) if r['images']), None)
        reference = next((f['response']['reference_solution'] for f in reversed(feedback)
                          if f['current'] and f['response'].get('reference_solution')), solutions.get(q['id']))
        reference_html = ''
        if reference:
            reference_html = rendered_blocks(reference.get('approach', []))
            for step in reference.get('steps', []):
                reference_html += '<h3>'+escape(step['title'])+'</h3>'+rendered_blocks(step['blocks'])
            reference_html += '<div class="answer-result">'+rendered_blocks(reference.get('result', []))+'</div>'
            reference_html += '<h3>易错点</h3>'+rendered_blocks(reference.get('pitfalls', []))
        skills = catalog['question_knowledge'][q['id']]
        related = [other['id'] for other in manifest['questions'] if other['id'] != q['id'] and set(skills) & set(catalog['question_knowledge'][other['id']])]
        questions.append({'id': q['id'], 'label': q.get('label', q['id']), 'tags': q.get('tags', []),
            'knowledge_ids': skills, 'prompt_images': source_images(manifest, q, directory),
            'latest_request': latest, 'ink_request': ink, 'feedback': feedback, 'requests': requests,
            'archived_recognition': archived[q['id']],
            'activity': activity.get(q['id']), 'reference_html': reference_html,
            'related_question_ids': related, 'practice_ids': [p['id'] for p in exercises if p['question_id'] == q['id']]})
    practices = [dict(p, prompt_html=rendered_blocks(p['prompt']), solution_html=rendered_blocks(p['solution'])) for p in exercises]
    generations = [dict(read_row, status=status) for payload, status in store.db.execute(
        'SELECT payload,status FROM practice_generations WHERE course=? ORDER BY created', (cid,)) for read_row in [json.loads(payload)]]
    return {'schema': 'inkbridge-knowledge-snapshot-v1', 'course': manifest['course'], 'catalog': catalog,
            'states': states, 'questions': questions, 'practice': practices, 'history': history,
            'generations': generations, 'context_hash': fingerprint, 'updated_at_pc': time.time(),
            'source_pdf_sha256': manifest['source_pdf_sha256'], 'solutions_sha256': course.get('solutions_sha256'),
            'storage_root': str(store.root)}


def publish_knowledge(store, courses):
    setup(store); links = []
    for course in courses:
        snapshot = build_snapshot(store, course); cid = snapshot['course']['id']
        directory = store.root/'knowledge'/cid
        # SQLite is durable authority; recover missing projections after a crash
        # between commit and JSON publication, without rewriting existing history.
        projections = [(store.root/'knowledge/history'/(e['id']+'.json'),e) for e in snapshot['history']]
        projections += [(store.root/'knowledge/practice'/(p['id']+'.json'),p) for p in items(store,cid)]
        projections += [(store.root/'knowledge/queue'/(g['id']+'.request.json'),json.loads(payload))
                        for payload, in store.db.execute('SELECT payload FROM practice_generations WHERE course=?',(cid,))
                        for g in [json.loads(payload)]]
        for path,value in projections:
            if not path.exists(): save(path,value)
            elif read(path) != value: raise ValueError('Immutable knowledge history file changed')
        payload = json.dumps(snapshot, ensure_ascii=False, allow_nan=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
        template = (HERE/'course_knowledge.html').read_text(encoding='utf-8')
        html = template.replace('__COURSE_TITLE__', escape(snapshot['course']['title'])).replace('__SNAPSHOT__', payload)
        atomic_bytes(directory/'index.html', html.encode('utf-8'))
        save(directory/'snapshot.json', snapshot)
        links.append({'id': cid, 'title': snapshot['course']['title'], 'url': '/knowledge/'+quote(cid)+'/index.html'})
    return links
