"""Generate an empty, PDF-backed native workbook with known page identities.

Native metadata layout adapted from LTong-g/jnote_to_hinote, MIT, Ray L (2026),
commit 532c561b2ad2d53022c53f73ee02662cccfcf298, converter._convert_pdf and
_rewrite_thumbnails. See third_party/jnote_to_hinote/LICENSE and SOURCE.md.
No synthetic handwriting, private database access or tablet UI actions.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import time
import uuid
import zipfile

import fitz
from PIL import Image

from course_workbook import bind, save, sha256, require_answer_free
from coordinate_page_binding import load_binding

UPSTREAM = 'https://github.com/LTong-g/jnote_to_hinote/tree/532c561b2ad2d53022c53f73ee02662cccfcf298'


def packed(value):
    return gzip.compress(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'), mtime=0)


def file_info(name, data):
    return {'name': name, 'hash': hashlib.sha256(data).hexdigest().upper()}


def details(files):
    return json.dumps({n: [file_info(n, data)] for n, data in files.items()}, separators=(',', ':'))


def generate(manifest_path, output, title, pending_binding):
    """Write a new immutable bundle; offline validity never proves device import."""
    manifest_path, output, pending_binding = map(lambda p: Path(p).resolve(), (manifest_path, output, pending_binding))
    manifest = json.loads(manifest_path.read_bytes())
    require_answer_free(manifest)
    pdf = Path(manifest['workbook_pdf'])
    if not manifest.get('deliverable') or sha256(pdf) != manifest['workbook_sha256']:
        raise ValueError('An intact reviewed workbook is required')
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 120:
        raise ValueError('Need an explicit note title')
    if output.suffix.lower() != '.hinote' or output.exists() or pending_binding.exists():
        raise ValueError('Choose new .hinote and pending binding destinations')
    note_id, attachment_id, filename = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex+'_pdf'
    ts = time.time_ns()//1_000_000
    files = {filename: pdf.read_bytes()}; entries = {}; page_ids = []
    with fitz.open(stream=files[filename], filetype='pdf') as document:
        if len(document) != len(manifest['pages']) or not 1 <= len(document) <= 1000:
            raise ValueError('Workbook page count mismatch')
        for index, page in enumerate(document):
            if page.rotation: raise ValueError('Normalize rotated pages before native generation')
            page_id, thumbnail_name = uuid.uuid4().hex, uuid.uuid4().hex+'.jpg'
            page_ids.append(page_id)
            size = page.rect
            scale = 1080/max(size.width, size.height)
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, colorspace=fitz.csRGB)
            thumbnail = io.BytesIO()
            Image.frombytes('RGB', (pix.width, pix.height), pix.samples).save(thumbnail, 'JPEG', quality=100, subsampling=2)
            page_files = {thumbnail_name: thumbnail.getvalue()}; files.update(page_files)
            content = {'pageBookMarks': [], 'attachment': [], 'background': 'base1',
                       'bkgAttachmentId': attachment_id, 'bkgAttachmentIndex': index,
                       'chapterNumber': '', 'cloudSyncState': 0, 'createTime': ts,
                       'guid': '', 'id': page_id, 'isDelete': 0,
                       'lastPageTag': int(index == len(document)-1), 'modifiedTime': ts,
                       'notesId': note_id, 'pageColor': -1, 'pageElement': [], 'pageNumber': index+1,
                       'pageOrientation': int(size.width > size.height),
                       'pageRatio': min(size.width, size.height)/max(size.width, size.height),
                       'pageType': 1, 'thumbnail': '/data/user/0/com.huawei.hinote/files/thumbnail/'+thumbnail_name,
                       'unStructUuid': '',
                       'data1': json.dumps({'detailFileMap': details(page_files), 'thumbnail_area': '', 'book_mark': ''}, separators=(',', ':')),
                       **{f'data{i}': '' for i in range(2, 6)}}
            entries[f'pages/{page_id}.jhinote'] = packed({'customNotePageContent': content,
                                                         'fileList': [file_info(n, d) for n, d in page_files.items()]})
        first = document[0].rect
    outline_name = f'{note_id}_{ts}_outline.json'; files[outline_name] = note_id.encode('ascii')
    top_files = {name: files[name] for name in (filename, outline_name)}
    attachment = {'attachType': 3, 'cloudSyncState': 1, 'createTime': ts,
                  'filePath': '/data/user/0/com.huawei.hinote/files/importfiles/'+filename,
                  'isDelete': 0, 'modifiedTime': ts, 'notesId': note_id, 'playbackProgress': 0,
                  'id': attachment_id, 'notePageId': None,
                  'data1': '{"synDataTpye":"memonote","pageElementId":""}',
                  **{f'data{i}': '' for i in range(2, 6)}, 'unStructUuid': '', 'transText': None, 'contentText': ''}
    content = {'attachment': [attachment], 'background': 'base1',
               'categoryId': 'system_category_uuid_unclassified', 'cloudSyncState': 0, 'createTime': ts,
               'data1': json.dumps({'relationTags': '[]', 'relationPages': '', 'originDeviceType': 'tablet',
                                   'isContentCover': '1', 'isInfNote': '0', 'infStylusPath': '',
                                   'detailFileMap': details(top_files), 'record_item_mapper_key': '', 'localData1': ''}, separators=(',', ':')),
               'deleteTag': 0, 'deleteTime': 0, 'extendFields': 'hinote_1.0.5', 'guid': '', 'hasCover': 0,
               'id': note_id, 'isFavorite': 0, 'isTop': 0, 'modifiedTime': ts,
               'noteIcon': 'import_pdf', 'noteTitle': title.strip(), 'noteType': 101, 'pageColor': -1,
               'pageOrientation': int(first.width > first.height),
               'pageRatio': min(first.width, first.height)/max(first.width, first.height),
               'unStructUuid': '', 'bookIntroduction': '', 'userId': '', 'noteTemplate': '',
               **{f'data{i}': '' for i in range(2, 6)}, 'coverId': '',
               'parentId': 'system_category_uuid_unclassified', 'outLineAttachment': [], 'recordItemEntities': []}
    entries.update({'files/'+n: d for n, d in files.items()})
    entries[note_id+'.jhinote'] = packed({'customNoteContent': content,
                                         'fileList': [file_info(n, d) for n, d in top_files.items()]})
    entries['custom_md.jhinote'] = packed({'customMdContents': [
        {'fileMdStr': hashlib.sha256(d).hexdigest().upper(),
         'fileNameMdStr': hashlib.sha256(n.encode('utf-8')).hexdigest()} for n, d in files.items()]})
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name('.'+output.name+'.'+uuid.uuid4().hex+'.partial')
    try:
        with zipfile.ZipFile(temporary, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
            for n, d in entries.items(): archive.writestr(n, d)
        native = load_binding(temporary, pdf)
        if native['pages'] != {p: n+1 for n, p in enumerate(page_ids)}:
            raise ValueError('Independent native parser rejected page bindings')
        if sha256(pdf) != manifest['workbook_sha256']:
            raise ValueError('Workbook changed during native generation')
        # Hard link commits without replacing another writer's destination.
        import os
        os.link(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    result = bind(manifest_path, output, pending_binding, generated=True)
    save(output.with_suffix('.delivery.json'), {'schema': 'inkbridge-native-delivery-v1',
         'hinote_path': str(output), 'hinote_sha256': sha256(output), 'writer_source': UPSTREAM,
         'format_version': 'hinote_1.0.5', 'native_page_ids': page_ids,
         'device_import_verified': False, 'handwriting_records_generated': 0, 'tablet_ui_actions': []})
    return result


def confirm_import(pending_path, context, output):
    """Exact fresh foreground canvas UUIDs confirm observed pages only.

    Unseen/remapped IDs are never inferred by title or page order. Each later
    contact/geometry result must still independently match its own known UUID.
    """
    from datetime import datetime, timezone
    from course_learning import verified_package
    result, _ = verified_package(pending_path, allow_generated=True)
    if result.get('notes_import_verified') or result.get('binding_origin') != 'generated_native_workbook':
        raise ValueError('Need a pending generated native workbook')
    interval = context.get('observation_interval', {})
    start = datetime.fromisoformat(interval['start_at_pc']).timestamp()
    end = datetime.fromisoformat(interval['end_at_pc']).timestamp()
    before, after = context.get('window_before', {}), context.get('window_after', {})
    stable = (before.get('pid') is not None and all(before.get(k) == after.get(k) for k in ('pid', 'window_id', 'display_id'))
              and before.get('focused') is True and after.get('focused') is True)
    if (not context.get('current_document_valid') or not context.get('active_tab_marker_unique') or
        not context.get('window_identity_stable') or not context.get('foreground_at_both_checks') or not stable or
        context.get('source') != 'local_hidumper_allInfo' or
        context.get('document_title_candidate') != result['native']['title'] or
        not 0 <= end-start <= 3 or not 0 <= time.time()-end <= 10):
        raise ValueError('Fresh unambiguous foreground native context required')
    layers = context.get('page_layers', [])
    ids = [p.get('page_id') for p in layers]
    if not ids or len(ids) > 16 or len(ids) != len(set(ids)) or any(p not in result['page_questions'] for p in ids):
        raise ValueError('Imported page IDs differ; never infer remapped IDs from title/order')
    result = dict(result, notes_import_verified=True,
                  binding_basis='generated_background_hash_and_exact_observed_native_UUID',
                  import_observation={'observed_page_ids': ids, 'all_pages_observed': set(ids) == set(result['page_questions']),
                      'start_at_pc': interval['start_at_pc'], 'end_at_pc': interval['end_at_pc'],
                      'confirmed_at_utc': datetime.now(timezone.utc).isoformat(),
                      'window_before': before, 'window_after': after,
                      'limits': ['Only listed page identities were observed on device; later pages require exact UUID matching.']})
    if Path(output).exists(): raise FileExistsError('Choose a new import confirmation path')
    save(output, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--title', required=True)
    parser.add_argument('--pending-binding', type=Path, required=True)
    args = parser.parse_args()
    result = generate(args.manifest, args.output, args.title, args.pending_binding)
    print(json.dumps({'pages': len(result['page_questions']), 'notes_import_verified': False, 'tablet_ui_actions': []}))


if __name__ == '__main__': main()
