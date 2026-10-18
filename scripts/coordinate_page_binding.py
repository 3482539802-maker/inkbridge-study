"""Select a visually matched page using verified hinote background links.

Page rectangles only veto incompatible feature support; they never provide a
screen-to-page transform. Export links remain snapshots, not a live database.
"""
from copy import deepcopy
import hashlib
import math
from pathlib import Path

from inspect_hinote import inspect


def binding_from_inspection(native, pdf_hash, pdf_pages):
    if len(native.get('documents', [])) != 1 or native.get('warnings'):
        raise ValueError('Unique verified native document required')
    if not native.get('attachment_verification') or not all(a.get('verified') for a in native['attachment_verification']):
        raise ValueError('Native attachment verification failed')
    doc = native['documents'][0]
    attachments = set()
    for a in doc.get('attachment') or []:
        name = Path(a.get('filePath', '')).name
        matches = [v for v in native['attachment_verification'] if v.get('name') == name and v.get('verified') and v.get('sha256') == pdf_hash]
        if matches and a.get('isDelete', 0) == 0:
            attachments.add(a['id'])
    if not attachments or not doc.get('title') or not doc.get('note_id'):
        raise ValueError('Native original PDF hash not matched')
    pages = {}
    seen = set()
    for page in native['pages']:
        uuid = page.get('page_id')
        if not uuid or uuid in seen or page.get('note_id') != doc['note_id']:
            raise ValueError('Ambiguous native page identity')
        seen.add(uuid)
        index = page.get('background_attachment_index')
        if page.get('background_attachment_id') in attachments and type(index) is int and 0 <= index < pdf_pages:
            pages[uuid] = index+1
    if not pages or len(pages) > 2000:
        raise ValueError('No bounded native PDF page mapping')
    return {'title': doc['title'], 'note_id': doc['note_id'], 'source_hinote_sha256': native['sha256'],
            'source_pdf_sha256': pdf_hash, 'pages': pages, 'live_database_subscription': False}


def load_binding(hinote, pdf):
    import fitz
    hinote, pdf = Path(hinote), Path(pdf)
    if hinote.stat().st_size > 512*1024*1024:
        raise ValueError('Native export exceeds 512 MiB')
    digest = hashlib.sha256()
    with pdf.open('rb') as source:
        for chunk in iter(lambda: source.read(1024*1024), b''):
            digest.update(chunk)
    with fitz.open(pdf) as document:
        count = len(document)
    return binding_from_inspection(inspect(hinote), digest.hexdigest(), count)


def rectangle(layer):
    rect = layer.get('unclipped_window_bounds')
    if (not isinstance(rect, list) or len(rect) != 4 or
            not all(type(n) in (int, float) and math.isfinite(n) for n in rect) or
            rect[0] >= rect[2] or rect[1] >= rect[3]):
        raise ValueError('Invalid page rectangle')
    return rect


def bind_frame(frame, binding):
    """Return a derived record or leave it invalid; never revive failed frames."""
    result = deepcopy(frame)
    o, r = result.get('observation', {}), result.get('registration', {})
    if not o.get('valid') or not r.get('valid'):
        return result
    c = o.get('context', {})
    layers = c.get('page_layers', [])
    if (c.get('document_title_candidate') != binding['title'] or not c.get('active_tab_marker_unique') or
            r.get('source_pdf_sha256') != binding['source_pdf_sha256']):
        r.update(valid=False, reason='native_background_document_or_pdf_mismatch')
        return result
    ids = [p.get('page_id') for p in layers]
    selected = [p for p in layers if binding['pages'].get(p.get('page_id')) == r.get('pdf_page_1based')]
    if not 1 <= len(layers) <= 16 or len(ids) != len(set(ids)) or None in ids or len(selected) != 1:
        r.update(valid=False, reason='no_unique_native_background_page')
        return result
    try:
        rect = rectangle(selected[0])
        for other in layers:
            if other is selected[0]:
                continue
            q = rectangle(other)
            if max(rect[0], q[0]) < min(rect[2], q[2]) and max(rect[1], q[1]) < min(rect[3], q[3]):
                raise ValueError('Overlapping page rectangles')
        hull = r.get('support_hull_screen_px')
        if not isinstance(hull, list) or not 3 <= len(hull) <= 512:
            raise ValueError('Invalid support')
        for point in hull:
            if (not isinstance(point, list) or len(point) != 2 or
                    not all(type(v) in (int, float) and math.isfinite(v) for v in point) or
                    not rect[0] <= point[0] <= rect[2] or not rect[1] <= point[1] <= rect[3]):
                raise ValueError('Feature support disagrees with selected page rectangle')
    except ValueError:
        r.update(valid=False, reason='native_background_page_support_conflict')
        return result
    c['visible_page_layers'] = layers
    c['page_layers'] = selected
    r['native_background_binding'] = {
        'page_uuid': selected[0]['page_id'], 'pdf_page_1based': r['pdf_page_1based'],
        'note_id': binding['note_id'], 'source_hinote_sha256': binding['source_hinote_sha256'],
        'source_pdf_sha256': binding['source_pdf_sha256'],
        'method': 'exported_background_hash_and_uuid_with_visual_feature_support',
        'live_database_subscription': False, 'native_pen_position_verified': False}
    return result
