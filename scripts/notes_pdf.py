"""Index an explicit local PDF and resolve a Huawei Notes UI page conservatively.

No fuzzy filename match, device file access, handwriting recognition or model calls.
PyMuPDF is an optional dependency; the raw ink receiver does not need it.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

import fitz


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def index_pdf(pdf, notes_title=None, notes_pages=None, confirm_same_pages=False):
    pdf = Path(pdf).resolve()
    before_hash = sha256(pdf)
    with fitz.open(pdf) as document:
        if document.needs_pass:
            raise ValueError('Encrypted PDF requires an unlocked local copy')
        count = len(document)
        if notes_pages is not None and notes_pages < 1:
            raise ValueError('Notes page count must be positive')
        if confirm_same_pages and (not notes_title or notes_pages != count):
            raise ValueError('Confirming identical page order requires --notes-title and matching --notes-pages')
        pages = []
        for number, page in enumerate(document, 1):
            blocks = [{'bounds_pdf': list(block[:4]), 'text': block[4]}
                      for block in page.get_text('blocks', sort=True) if block[6] == 0]
            text = page.get_text(sort=True)
            pages.append({'pdf_page': number, 'printed_page_label': page.get_label(),
                          'bounds_pdf': list(page.rect), 'text': text, 'text_blocks': blocks,
                          'needs_ocr': not text.strip()})
    if sha256(pdf) != before_hash:
        raise ValueError('Source PDF changed while indexing; retry after saving it')
    return {'type': 'notes_pdf_catalog', 'schema_version': 1,
            'indexed_at_pc': datetime.now(timezone.utc).isoformat(),
            'pdf_path': str(pdf), 'pdf_sha256': before_hash, 'pdf_pages': count,
            'binding': {'notes_title': notes_title, 'notes_total_pages': notes_pages,
                        'page_order': 'user_confirmed_identical' if confirm_same_pages else 'unconfirmed'},
            'pages': pages}


def resolve_context(catalog, context, max_age=10, region=None, question=None):
    result = {'type': 'notes_problem_context', 'status': 'unresolved', 'reasons': [],
              'document_title': context.get('document_title'),
              'notes_page': context.get('current_page'), 'notes_total_pages': context.get('total_pages'),
              'observed_at_pc': context.get('captured_at_pc'),
              'resolved_at_pc': datetime.now(timezone.utc).isoformat(),
              'pdf_path': catalog['pdf_path'], 'pdf_sha256': catalog['pdf_sha256'],
              'question_id': question, 'question_evidence': 'explicit_selection' if question else None,
              'warnings': list(context.get('warnings', [])),
              'limits': ['Notes title does not prove device file identity.',
                         'PDF page number differs from printed labels.',
                         'No raw strokes are assigned to pages by this tool.',
                         'Text extraction may omit formulas and diagrams.']}
    binding = catalog.get('binding', {})
    if not binding.get('notes_title') or binding['notes_title'] != context.get('document_title'):
        result['reasons'].append('document_not_bound')
    same_titles = [t for t in context.get('open_tabs', []) if t.get('title') == binding.get('notes_title')]
    if len(same_titles) > 1:
        result['reasons'].append('duplicate_document_titles')
    try:
        observed = datetime.fromisoformat(context['captured_at_pc'])
        if observed.tzinfo is None:
            raise ValueError('Missing timezone')
        age = (datetime.now(timezone.utc) - observed).total_seconds()
        if max_age > 0 and (age < -2 or age > max_age):
            result['reasons'].append('context_stale_or_clock_invalid')
    except (KeyError, TypeError, ValueError):
        result['reasons'].append('context_has_no_observation_time')
    number = context.get('current_page')
    if type(number) is not int or not 1 <= number <= catalog['pdf_pages']:
        result['reasons'].append('page_missing_or_out_of_range')
    total = context.get('total_pages')
    if total is not None and total != catalog['pdf_pages']:
        result['reasons'].append('notes_pdf_page_count_mismatch')
    if binding.get('notes_total_pages') is not None and binding['notes_total_pages'] != catalog['pdf_pages']:
        result['reasons'].append('binding_page_count_mismatch')
    if sha256(catalog['pdf_path']) != catalog['pdf_sha256']:
        result['reasons'].append('source_pdf_changed_reindex_required')
    if result['reasons']:
        return result
    page_data = catalog['pages'][number - 1]
    result.update({'pdf_page': number, 'printed_page_label': page_data['printed_page_label'],
                   'page_bounds_pdf': page_data['bounds_pdf'], 'page_text': page_data['text'],
                   'text_blocks': page_data['text_blocks'], 'needs_ocr': page_data['needs_ocr']})
    confirmed = binding.get('page_order') == 'user_confirmed_identical'
    result['status'] = 'page_resolved_question_unselected' if confirmed else 'candidate_page_alignment_unconfirmed'
    if not confirmed:
        result['warnings'].append('One-to-one page order is a hypothesis; confirm the original and Notes page order before using this as a question match.')
    if total is None:
        result['warnings'].append('Total page count was hidden in this snapshot.')
    if region is not None:
        if len(region) != 4 or not all(math.isfinite(v) for v in region):
            raise ValueError('Region must have four finite PDF coordinates')
        left, top, right, bottom = region
        pl, pt, pr, pb = page_data['bounds_pdf']
        if not (pl <= left < right <= pr and pt <= top < bottom <= pb):
            raise ValueError('Region must be nonempty and entirely inside the PDF page')
        with fitz.open(catalog['pdf_path']) as document:
            result['region_text'] = document[number - 1].get_text(clip=fitz.Rect(region), sort=True)
        result['region_pdf'] = region
        result['region_evidence'] = 'explicit_pdf_coordinates'
    if confirmed and (region is not None or question):
        result['status'] = 'page_resolved_question_explicitly_selected'
    return result


def resolve_native(catalog, context, native, max_age=10):
    """Use page UUID + exported background index + original PDF hash.

    Does not equate Notes pageNumber with PDF page. Supports multiple canvases.
    Exported background links remain snapshots, not live private DB reads.
    """
    from notes_context import apply_native_mapping
    enriched = apply_native_mapping(context, native)
    report = {'type': 'native_pdf_context', 'status': 'unresolved', 'reasons': [],
              'document_title': context.get('document_title'), 'native_note_id': enriched.get('native_note_id'),
              'pdf_sha256': catalog['pdf_sha256'], 'observed_at_pc': context.get('captured_at_pc'),
              'visible_pdf_pages': [], 'warnings': enriched.get('warnings', []),
              'limits': ['Exported background links must be refreshed after replacing page backgrounds.',
                         'Visible pages do not identify a unique question or assign raw strokes.',
                         'Screen-to-PDF coordinates have not been calibrated.']}
    probe = dict(context, current_page=1, total_pages=None)
    reasons = resolve_context(catalog, probe, max_age)['reasons']
    report['reasons'].extend(reasons)
    if not enriched.get('native_note_id'):
        report['reasons'].append('visible_canvas_uuid_not_in_native_note')
    if report['reasons']:
        return report
    documents = [d for d in native['documents'] if d['note_id'] == enriched['native_note_id']]
    valid_attachments = set()
    for attachment in documents[0].get('attachment') or []:
        filename = Path(attachment.get('filePath', '')).name
        matches = [a for a in native.get('attachment_verification', [])
                   if a.get('name') == filename and a.get('verified') and a.get('sha256') == catalog['pdf_sha256']]
        if matches:
            valid_attachments.add(attachment.get('id'))
    if not valid_attachments:
        report['reasons'].append('embedded_original_pdf_hash_not_matched')
        return report
    for canvas in enriched.get('visible_pages', []):
        index = canvas.get('native_background_attachment_index')
        if canvas.get('native_background_attachment_id') not in valid_attachments or type(index) is not int or not 0 <= index < catalog['pdf_pages']:
            report['reasons'].append('visible_page_background_not_matched')
            continue
        page = catalog['pages'][index]
        report['visible_pdf_pages'].append({'page_id': canvas['page_id'], 'notes_page_at_export': canvas['native_page_number'],
                                           'pdf_page': index + 1, 'bounds_screen': canvas['visible_bounds'],
                                           'page_text': page['text'], 'text_blocks': page['text_blocks'],
                                           'needs_ocr': page['needs_ocr']})
    report['status'] = 'visible_pdf_pages_resolved_from_native_snapshot' if report['visible_pdf_pages'] and not report['reasons'] else 'unresolved'
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    index = commands.add_parser('index', help='Index only the PDF explicitly supplied')
    index.add_argument('--pdf', type=Path, required=True)
    index.add_argument('--notes-title', help='Exact title observed in Notes (can differ from PC filename)')
    index.add_argument('--notes-pages', type=int)
    index.add_argument('--confirm-same-pages', action='store_true', help='Use only after confirming identical page order, with no inserted Notes pages')
    index.add_argument('--output', type=Path, default=Path('output/context/pdf-catalog.json'))
    resolve = commands.add_parser('resolve', help='Read current context and extract a matched/candidate source page')
    resolve.add_argument('--catalog', type=Path, default=Path('output/context/pdf-catalog.json'))
    resolve.add_argument('--context', type=Path, default=Path('output/context/current.json'))
    resolve.add_argument('--native', type=Path, help='Resolve visible page UUIDs via hinote background links and embedded PDF hash')
    resolve.add_argument('--max-age', type=float, default=10, help='Reject context older than this many seconds; 0 is explicit historical analysis')
    resolve.add_argument('--region', type=float, nargs=4, metavar=('LEFT', 'TOP', 'RIGHT', 'BOTTOM'), help='Selected question rectangle in PDF points, not tablet pixels')
    resolve.add_argument('--question', help='Explicit question number/label; never inferred from page number')
    resolve.add_argument('--output', type=Path, default=Path('output/context/problem.json'))
    args = parser.parse_args()
    if args.command == 'index':
        result = index_pdf(args.pdf, args.notes_title, args.notes_pages, args.confirm_same_pages)
        summary = {key: value for key, value in result.items() if key != 'pages'}
    else:
        if args.max_age < 0:
            parser.error('--max-age must be nonnegative')
        if args.native:
            if args.region or args.question:
                parser.error('--native resolves potentially multiple pages; select questions separately')
            result = resolve_native(json.loads(args.catalog.read_bytes()), json.loads(args.context.read_bytes()),
                                    json.loads(args.native.read_bytes()), args.max_age)
        else:
            result = resolve_context(json.loads(args.catalog.read_bytes()), json.loads(args.context.read_bytes()),
                                     args.max_age, args.region, args.question)
        result['context_path'] = str(args.context.resolve())
        summary = {key: value for key, value in result.items() if key not in ('text_blocks', 'page_text')}
        if 'visible_pdf_pages' in summary:
            summary['visible_pdf_pages'] = [{k: v for k, v in p.items() if k not in ('text_blocks', 'page_text')}
                                            for p in summary['visible_pdf_pages']]
    write_json(args.output, result)
    print(json.dumps(summary, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
